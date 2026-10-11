"""Fetch and format A-share index quotes with bounded source fallback."""

import asyncio
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from time import monotonic

import aiohttp

from astrbot.api import logger

BEIJING_TZ = timezone(timedelta(hours=8))


@dataclass
class IndexData:
    """A valid latest quote; missing numeric fields remain unknown."""

    code: str
    name: str
    latest_price: float
    change: float | None
    change_pct: float | None
    open_price: float | None
    high_price: float | None
    low_price: float | None
    pre_close: float | None
    volume: float | None
    amount: float | None
    source: str = ""
    quote_time: datetime | None = None
    fetched_at: datetime | None = None
    from_cache: bool = False

    @property
    def change_symbol(self) -> str:
        if self.change_pct is None:
            return "❔"
        if self.change_pct > 0:
            return "📈"
        if self.change_pct < 0:
            return "📉"
        return "➡️"

    @property
    def status_text(self) -> str:
        if self.change_pct is None:
            return "❔ 未知"
        if self.change_pct >= 3:
            return "🔥 大涨"
        if self.change_pct >= 1:
            return "↗️ 上涨"
        if self.change_pct > 0:
            return "↑ 微涨"
        if self.change_pct <= -3:
            return "💥 大跌"
        if self.change_pct <= -1:
            return "↘️ 下跌"
        if self.change_pct < 0:
            return "↓ 微跌"
        return "➡️ 平盘"


@dataclass
class MarketOverview:
    """Available index quotes with the response generation time."""

    indices: list[IndexData]
    fetch_time: datetime
    source: str = ""

    @property
    def total_amount(self) -> float | None:
        amounts = {i.code: i.amount for i in self.indices}
        first, second = amounts.get("000001"), amounts.get("399001")
        if first is None or second is None:
            return None
        return _to_float(first + second)

    @property
    def total_amount_text(self) -> str:
        total = self.total_amount
        if total is None:
            return "未知"
        if total >= 1e8:
            return f"{total / 1e8:.0f}亿"
        if total >= 1e4:
            return f"{total / 1e4:.0f}万"
        return f"{total:.0f}"

    @property
    def up_count(self) -> int:
        return sum(i.change_pct is not None and i.change_pct > 0 for i in self.indices)

    @property
    def down_count(self) -> int:
        return sum(i.change_pct is not None and i.change_pct < 0 for i in self.indices)

    @property
    def flat_count(self) -> int:
        return sum(i.change_pct == 0 for i in self.indices)

    @property
    def unknown_count(self) -> int:
        return sum(i.change_pct is None for i in self.indices)


# Eastmoney secids use 1 for Shanghai and 0 for Shenzhen.
_INDEX_CONFIG = [
    ("000001", "上证指数", "1.000001"),
    ("399001", "深证成指", "0.399001"),
    ("399006", "创业板指", "0.399006"),
    ("000688", "科创50", "1.000688"),
    ("000300", "沪深300", "1.000300"),
]

# Eastmoney prices and changes are scaled by 100; amount is in yuan.
_EM_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://quote.eastmoney.com/",
}
_TENCENT_CODES = {
    "000001": "sh000001",
    "399001": "sz399001",
    "399006": "sz399006",
    "000688": "sh000688",
    "000300": "sh000300",
}

_session: aiohttp.ClientSession | None = None
_closed = False
_closing = False
_close_task: asyncio.Task | None = None
_cache: dict[str, tuple[float, IndexData | None]] = {}
_inflight: dict[str, asyncio.Task] = {}


def reset_market_state() -> None:
    """Reopen fetching for a new plugin instance after completed shutdown."""
    global _closed, _close_task
    if _closing:
        raise RuntimeError("Market shutdown is still in progress")
    _closed = False
    _close_task = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _closed:
        raise RuntimeError("Market fetching is closed")
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5), headers=_HEADERS
        )
    return _session


def _to_float(
    value, scale: float = 1, *, minimum: float | None = None, inclusive: bool = True
) -> float | None:
    """Return a finite number within an optional inclusive or exclusive bound."""
    try:
        number = float(value) / scale
        if not math.isfinite(number):
            return None
        if minimum is not None and (
            number < minimum or (not inclusive and number == minimum)
        ):
            return None
        return number
    except (ValueError, TypeError, OverflowError):
        return None


def _derive_change(price, pre_close, change=None, change_pct=None):
    """Fill missing changes from a valid previous close without replacing zero."""
    if pre_close is not None and pre_close > 0:
        if change is None:
            change = _to_float(price - pre_close)
        if change_pct is None:
            change_pct = _to_float((price - pre_close) / pre_close * 100)
    return (
        round(change, 2) if change is not None else None,
        round(change_pct, 2) if change_pct is not None else None,
    )


async def _fetch_from_eastmoney(code: str, secid: str) -> dict | None:
    """Request one raw Eastmoney quote with the shared bounded session."""
    url = (
        f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={_EM_FIELDS}"
    )
    try:
        session = await _get_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.debug(f"Eastmoney HTTP {resp.status} [{code}]")
                return None
            payload = json.loads(await resp.text())
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                return data
            logger.debug(f"Eastmoney missing quote data [{code}]")
    except Exception as exc:
        logger.debug(f"Eastmoney request failed [{code}]: {exc}")
    return None


def _parse_eastmoney(data: dict) -> IndexData | None:
    """Parse an Eastmoney quote while retaining missing fields.

    Args:
        data: Raw quote dictionary with prices scaled by 100.

    Returns:
        A quote with a positive finite latest price, or None.
    """
    try:
        price = _to_float(data.get("f43"), 100)
        if price is None or price <= 0:
            return None
        pre_close = _to_float(data.get("f60"), 100, minimum=0, inclusive=False)
        change, change_pct = _derive_change(
            price,
            pre_close,
            _to_float(data.get("f169"), 100),
            _to_float(data.get("f170"), 100),
        )
        return IndexData(
            code=str(data.get("f57", "")),
            name=str(data.get("f58") or ""),
            latest_price=price,
            change=change,
            change_pct=change_pct,
            open_price=_to_float(data.get("f46"), 100, minimum=0, inclusive=False),
            high_price=_to_float(data.get("f44"), 100, minimum=0, inclusive=False),
            low_price=_to_float(data.get("f45"), 100, minimum=0, inclusive=False),
            pre_close=pre_close,
            volume=_to_float(data.get("f47"), minimum=0),
            amount=_to_float(data.get("f48"), minimum=0),
            source="东方财富",
        )
    except Exception as exc:
        logger.warning(f"Eastmoney quote parsing failed: {exc}")
        return None


def _parse_quote_time(value: str, date_format: str) -> datetime | None:
    """Parse only the timestamp positions verified against live source data."""
    try:
        return datetime.strptime(value, date_format).replace(tzinfo=BEIJING_TZ)
    except (ValueError, TypeError):
        return None


async def _fetch_from_tencent(code: str) -> IndexData | None:
    """Fetch and validate Tencent's symbol, numeric fields, and quote time."""
    symbol = _TENCENT_CODES.get(code)
    if not symbol:
        return None
    try:
        session = await _get_session()
        async with session.get(f"https://qt.gtimg.cn/q={symbol}") as resp:
            if resp.status != 200:
                logger.debug(f"Tencent HTTP {resp.status} [{code}]")
                return None
            text = await resp.text(encoding="gbk")
        match = re.search(rf'v_{re.escape(symbol)}="([^"\n]+)"', text)
        parts = match.group(1).split("~") if match else []
        if len(parts) < 40 or parts[2] != code:
            logger.debug(f"Tencent malformed or mismatched quote [{code}]")
            return None
        price = _to_float(parts[3])
        if price is None or price <= 0:
            logger.debug(f"Tencent invalid latest price [{code}]")
            return None
        pre_close = _to_float(parts[4], minimum=0, inclusive=False)
        change, change_pct = _derive_change(
            price, pre_close, _to_float(parts[31]), _to_float(parts[32])
        )
        return IndexData(
            code=code,
            name=parts[1],
            latest_price=price,
            change=change,
            change_pct=change_pct,
            open_price=_to_float(parts[5], minimum=0, inclusive=False),
            high_price=_to_float(parts[33], minimum=0, inclusive=False),
            low_price=_to_float(parts[34], minimum=0, inclusive=False),
            pre_close=pre_close,
            volume=_to_float(parts[6], minimum=0),
            amount=_to_float(parts[37], 0.0001, minimum=0),
            source="腾讯财经",
            quote_time=_parse_quote_time(parts[30], "%Y%m%d%H%M%S"),
            fetched_at=datetime.now(BEIJING_TZ),
        )
    except Exception as exc:
        logger.debug(f"Tencent request failed [{code}]: {exc}")
    return None


async def _fetch_from_sina(code: str) -> IndexData | None:
    """Fetch and validate Sina's symbol, numeric fields, and quote time."""
    symbol = _TENCENT_CODES.get(code)
    if not symbol:
        return None
    try:
        session = await _get_session()
        async with session.get(
            f"https://hq.sinajs.cn/list={symbol}",
            headers={**_HEADERS, "Referer": "https://finance.sina.com.cn/"},
        ) as resp:
            if resp.status != 200:
                logger.debug(f"Sina HTTP {resp.status} [{code}]")
                return None
            text = await resp.text(encoding="gbk")
        match = re.search(rf'hq_str_{re.escape(symbol)}="([^"\n]+)"', text)
        parts = match.group(1).split(",") if match else []
        if len(parts) < 32:
            logger.debug(f"Sina malformed or mismatched quote [{code}]")
            return None
        price = _to_float(parts[3])
        if price is None or price <= 0:
            logger.debug(f"Sina invalid latest price [{code}]")
            return None
        pre_close = _to_float(parts[2], minimum=0, inclusive=False)
        change, change_pct = _derive_change(price, pre_close)
        return IndexData(
            code=code,
            name=parts[0],
            latest_price=price,
            change=change,
            change_pct=change_pct,
            open_price=_to_float(parts[1], minimum=0, inclusive=False),
            high_price=_to_float(parts[4], minimum=0, inclusive=False),
            low_price=_to_float(parts[5], minimum=0, inclusive=False),
            pre_close=pre_close,
            volume=_to_float(parts[8], minimum=0),
            amount=_to_float(parts[9], minimum=0),
            source="新浪财经",
            quote_time=_parse_quote_time(
                f"{parts[30]} {parts[31]}", "%Y-%m-%d %H:%M:%S"
            ),
            fetched_at=datetime.now(BEIJING_TZ),
        )
    except Exception as exc:
        logger.debug(f"Sina request failed [{code}]: {exc}")
    return None


async def _fetch_sources(code: str, name: str, secid: str) -> IndexData | None:
    """Try each source in priority order and validate the returned index."""
    raw = await _fetch_from_eastmoney(code, secid)
    parsed = _parse_eastmoney(raw) if raw is not None else None
    if parsed is not None and parsed.code == code:
        return replace(
            parsed, name=parsed.name or name, fetched_at=datetime.now(BEIJING_TZ)
        )
    if raw is not None:
        logger.debug(f"Eastmoney invalid or mismatched quote [{code}]")
    for fetcher in (_fetch_from_tencent, _fetch_from_sina):
        parsed = await fetcher(code)
        if parsed is not None and parsed.code == code:
            return parsed
    logger.warning(f"All market sources failed [{code} {name}]")
    return None


async def _fetch_worker(code: str, name: str, secid: str) -> IndexData | None:
    """Bound one source chain and cache only its completed result."""
    try:
        try:
            result = await asyncio.wait_for(
                _fetch_sources(code, name, secid), timeout=18
            )
        except Exception as exc:
            logger.warning(f"Market source chain failed [{code}]: {exc}")
            result = None
        if not _closed:
            _cache[code] = (monotonic() + (10 if result is not None else 3), result)
        return result
    finally:
        if _inflight.get(code) is asyncio.current_task():
            _inflight.pop(code, None)


async def fetch_single_index(
    code: str, force_refresh: bool = False
) -> IndexData | None:
    """Fetch a configured index, sharing active work across callers.

    Args:
        code: Short index code or its Eastmoney secid.
        force_refresh: Skip cache lookup while still sharing active work.

    Returns:
        An independent quote copy, or None when unavailable or shut down.
    """
    if _closed:
        return None
    config = next(
        (entry for entry in _INDEX_CONFIG if code in (entry[0], entry[2])), None
    )
    if config is None:
        return None
    code, name, secid = config
    if not force_refresh and code in _cache:
        expires_at, cached = _cache[code]
        if monotonic() < expires_at:
            return replace(cached, from_cache=True) if cached is not None else None
        _cache.pop(code, None)
    task = _inflight.get(code)
    if task is None:
        task = asyncio.create_task(_fetch_worker(code, name, secid))
        _inflight[code] = task
    result = await asyncio.shield(task)
    return replace(result) if result is not None else None


async def fetch_market_overview(force_refresh: bool = False) -> MarketOverview:
    """Fetch all five index chains concurrently and retain partial success."""
    results = await asyncio.gather(
        *(fetch_single_index(code, force_refresh) for code, _, _ in _INDEX_CONFIG)
    )
    indices = [result for result in results if result is not None]
    return MarketOverview(
        indices=indices,
        fetch_time=datetime.now(BEIJING_TZ),
        source="、".join(sorted({index.source for index in indices})) or "未知",
    )


def _fmt_number(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "未知"
    return f"{value:+.2f}" if signed else f"{value:.2f}"


def _fmt_time(value: datetime | None) -> str:
    if value is None:
        return "未知"
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"


def _fmt_amount(amount: float | None) -> str:
    if amount is None:
        return "未知"
    if amount >= 1e8:
        return f"{amount / 1e8:.2f} 亿元"
    if amount >= 1e4:
        return f"{amount / 1e4:.2f} 万元"
    return f"{amount:.0f} 元"


def format_market_overview_text(overview: MarketOverview, detail: bool = True) -> str:
    """Show quotes, acquisition provenance, and incomplete overview data."""
    lines = [f"📊 A股大盘数据（响应时间：{_fmt_time(overview.fetch_time)}）", ""]
    for idx in overview.indices:
        percent = f"{idx.change_pct:+.2f}%" if idx.change_pct is not None else "未知"
        cache = "缓存" if idx.from_cache else "本次采集"
        if idx.from_cache and idx.fetched_at is not None:
            age = max(0, int((overview.fetch_time - idx.fetched_at).total_seconds()))
            cache += f"，缓存年龄：{age} 秒"
        lines.append(
            f"{idx.change_symbol} {idx.name}（{idx.code}）\n"
            f"   点位：{idx.latest_price:.2f}  涨跌：{_fmt_number(idx.change, True)}  涨幅：{percent}\n"
            f"   最高：{_fmt_number(idx.high_price)}  最低：{_fmt_number(idx.low_price)}  昨收：{_fmt_number(idx.pre_close)}\n"
            f"   成交额：{_fmt_amount(idx.amount)}\n"
            f"   来源：{idx.source or '未知'}（{cache}）\n"
            f"   行情时间：{_fmt_time(idx.quote_time)}\n"
            f"   采集时间：{_fmt_time(idx.fetched_at)}"
        )
    if detail:
        available = {index.code for index in overview.indices}
        missing = [
            f"{name}（{code}）"
            for code, name, _ in _INDEX_CONFIG
            if code not in available
        ]
        if missing:
            lines.extend(["", "缺失指数：" + "、".join(missing)])
        lines.extend(
            [
                "",
                "📈 综合概况",
                f"   监控指数：{len(overview.indices)} 只（上涨 {overview.up_count} / 下跌 {overview.down_count} / 平盘 {overview.flat_count} / 未知 {overview.unknown_count}）",
                f"   合计成交额：{_fmt_amount(overview.total_amount)}（上证 + 深证）",
            ]
        )
    return "\n".join(lines)


async def _close_resources() -> None:
    """Run exactly one cleanup operation without owning its callers."""
    global _session, _closing
    try:
        tasks = list(_inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _inflight.clear()
        _cache.clear()
        if _session is not None and not _session.closed:
            await _session.close()
        _session = None
    finally:
        _closing = False


async def close_session() -> None:
    """Share shutdown across callers; caller cancellation preserves cleanup."""
    global _closed, _closing, _close_task
    _closed = True
    if _close_task is None:
        _closing = True
        _close_task = asyncio.create_task(_close_resources())
    await asyncio.shield(_close_task)
