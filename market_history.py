"""Fetch complete index day bars with bounded fallback and isolated resources."""

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic

import aiohttp

logger = logging.getLogger(__name__)
BEIJING_TZ = timezone(timedelta(hours=8))
INDEX_CONFIG = (
    ("000001", "上证指数", "1.000001", "sh000001"),
    ("399001", "深证成指", "0.399001", "sz399001"),
    ("399006", "创业板指", "0.399006", "sz399006"),
    ("000688", "科创50", "1.000688", "sh000688"),
    ("000300", "沪深300", "1.000300", "sh000300"),
)
SOURCE_TIMEOUT = 4
INDEX_TIMEOUT = 9
BATCH_TIMEOUT = 12
SUCCESS_TTL = 900
FAILURE_TTL = 60


def now_beijing() -> datetime:
    """Return an aware time for source completion checks."""
    return datetime.now(BEIJING_TZ)


@dataclass(frozen=True)
class TradingCalendar:
    """Store verified annual weekday closures; uncovered years are unknown."""

    years: dict[int, frozenset[date]]

    def session_dates(self, start: date, end: date) -> tuple[date, ...]:
        """List sessions without treating missing annual data as open days.

        Args:
            start: Inclusive first date.
            end: Inclusive last date.

        Returns:
            Known trading dates in chronological order.

        Raises:
            ValueError: Any requested year is not verified.
        """
        if any(year not in self.years for year in range(start.year, end.year + 1)):
            raise ValueError("Trading calendar year is not covered")
        return tuple(
            current
            for offset in range(max(0, (end - start).days + 1))
            if (current := start + timedelta(days=offset)).weekday() < 5
            and current not in self.years[current.year]
        )


@dataclass(frozen=True)
class DailyBar:
    """Represent one unadjusted day bar with unknown optional values."""

    trade_date: date
    open: float
    close: float
    high: float
    low: float
    volume: float | None
    amount_yuan: float | None


@dataclass(frozen=True)
class IndexHistory:
    """Keep an entire single-source series and its acquisition metadata."""

    code: str
    source: str
    bars: tuple[DailyBar, ...]
    fetched_at: datetime
    from_cache: bool
    amount_scope: str | None
    completion_note: str = "归档日线"


@dataclass(frozen=True)
class HistoryBatch:
    """Return partial successes without disguising failed indices."""

    expected_date: date
    histories: dict[str, IndexHistory]
    unavailable: dict[str, str]


def _number(value, *, positive: bool = False) -> float | None:
    """Validate the shared numeric boundary for prices and optional amounts."""
    try:
        parsed = float(value) if not isinstance(value, bool) else math.nan
        if math.isfinite(parsed) and (parsed > 0 if positive else parsed >= 0):
            return parsed
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def parse_history(
    source: str,
    code: str,
    payload: dict,
    expected_date: date,
    now: datetime,
    calendar: TradingCalendar,
) -> IndexHistory:
    """Normalize a whole response without dropping holes or invalid prices.

    Args:
        source: Eastmoney or Tencent adapter name.
        code: Expected configured index code.
        payload: Parsed source response.
        expected_date: Required last completed trading day.
        now: Acquisition time, with timezone.
        calendar: Verified sessions for continuity checks.

    Returns:
        The latest 21 to 60 valid, continuous bars through the required date.

    Raises:
        ValueError: Identity, prices, dates, coverage, or continuity is invalid.
    """
    try:
        if source == "eastmoney":
            data = payload["data"]
            if payload.get("rc") != 0 or str(data["code"]) != code:
                raise ValueError("Mismatched history identity")
            if data.get("market") != (1 if code.startswith("0") else 0):
                raise ValueError("Mismatched history exchange")
            rows = [row.split(",") for row in data["klines"]]
        elif source == "tencent":
            symbol = next(item[3] for item in INDEX_CONFIG if item[0] == code)
            if payload.get("code") != 0:
                raise ValueError("Tencent source returned an error")
            # Live index responses use unadjusted day, never silently qfqday.
            rows = payload["data"][symbol]["day"]
        else:
            raise ValueError("Unknown history source")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ValueError("Invalid history row count")
        unique = {}
        today = now.astimezone(BEIJING_TZ).date()
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                raise ValueError("Malformed history row")
            day = date.fromisoformat(row[0])
            if day.isoformat() != row[0] or day > today:
                raise ValueError("Invalid or future history date")
            prices = tuple(_number(value, positive=True) for value in row[1:5])
            if any(value is None for value in prices):
                raise ValueError("Invalid history price")
            opening, closing, high, low = prices
            if not low <= min(opening, closing) <= max(opening, closing) <= high:
                raise ValueError("Inconsistent history OHLC")
            bar = DailyBar(
                day, opening, closing, high, low, _number(row[5]),
                _number(row[6]) if source == "eastmoney" and len(row) > 6 else None,
            )
            if day in unique and unique[day] != bar:
                raise ValueError("Conflicting duplicate history date")
            unique[day] = bar
        bars = tuple(unique[day] for day in sorted(unique) if day <= expected_date)[-60:]
        if len(bars) < 21 or bars[-1].trade_date != expected_date:
            raise ValueError("Missing required day or fewer than 21 closes")
        sessions = calendar.session_dates(bars[0].trade_date, expected_date)
        if tuple(bar.trade_date for bar in bars) != sessions:
            raise ValueError("Missing or unexpected trading date")
        return IndexHistory(
            code, "东方财富" if source == "eastmoney" else "腾讯财经", bars,
            now, False, None,
        )
    except (KeyError, TypeError, StopIteration, IndexError, AttributeError) as exc:
        raise ValueError("Malformed history payload") from exc


def confirm_close(
    source: str, code: str, bar: DailyBar, text: str, now: datetime
) -> bool:
    """Check same-day OHLC against an independent timestamped closing quote.

    Args:
        source: History source; selects a different quote provider.
        code: Expected index.
        bar: Candidate completed bar.
        text: Tencent quote for Eastmoney, or Sina quote for Tencent.
        now: Current acquisition time.

    Returns:
        Whether all four prices match within source rounding and quote time
        is between 15:00 and acquisition on the required date.
    """
    symbol = ("sh" if code.startswith("0") else "sz") + code
    try:
        if source == "eastmoney":
            match = re.search(rf'v_{re.escape(symbol)}="([^"\n]+)"', text)
            parts = match.group(1).split("~")
            if parts[2] != code:
                return False
            stamp = datetime.strptime(parts[30], "%Y%m%d%H%M%S").replace(tzinfo=BEIJING_TZ)
            values = (parts[5], parts[3], parts[33], parts[34])
        elif source == "tencent":
            match = re.search(rf'hq_str_{re.escape(symbol)}="([^"\n]+)"', text)
            parts = match.group(1).split(",")
            stamp = datetime.strptime(parts[30] + " " + parts[31], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
            values = (parts[1], parts[3], parts[4], parts[5])
        else:
            return False
        return (
            stamp.date() == bar.trade_date
            and stamp.time() >= time(15)
            and stamp <= now
            and all(
                (parsed := _number(raw, positive=True)) is not None
                and abs(parsed - expected) <= 0.011
                for raw, expected in zip(values, (bar.open, bar.close, bar.high, bar.low))
            )
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


class HistoryClient:
    """Own bounded historical requests independently of the live quote layer."""

    def __init__(self, calendar: TradingCalendar):
        self.calendar = calendar
        self._session = None
        self._closed = False
        self._close_task = None
        self._active_date = None
        self._cache = {}
        self._inflight = {}

    async def _fetch_source(
        self, source: str, code: str, expected_date: date, now: datetime
    ) -> IndexHistory:
        """Request one complete source and, when needed, independent close proof.

        Args:
            source: Adapter to request.
            code: Configured index.
            expected_date: Required final day.
            now: Shared acquisition timestamp.

        Returns:
            Validated single-source history.

        Raises:
            ValueError: Source data or same-day completion is unconfirmed.
            aiohttp.ClientError: The public endpoint is unavailable.
        """
        if self._closed:
            raise ValueError("History client is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=SOURCE_TIMEOUT))
        _, _, secid, symbol = next(item for item in INDEX_CONFIG if item[0] == code)
        if source == "eastmoney":
            url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {"secid": secid, "klt": "101", "fqt": "0", "lmt": "80",
                      "end": now.strftime("%Y%m%d"), "fields1": "f1,f2,f3,f4,f5,f6",
                      "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"}
            proof_url = "https://qt.gtimg.cn/q=" + symbol
        else:
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            params = {"param": symbol + ",day,,,80,qfq"}
            proof_url = "https://hq.sinajs.cn/list=" + symbol

        async def request(target, query=None):
            async with self._session.get(
                target, params=query,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
            ) as response:
                response.raise_for_status()
                return await response.text(encoding="gbk" if query is None else "utf-8")

        # Bound both operations together; cancellation also reclaims proof work.
        today = now.astimezone(BEIJING_TZ).date()
        requests = [asyncio.create_task(request(url, params))]
        if expected_date == today:
            requests.append(asyncio.create_task(request(proof_url)))
        try:
            texts = await asyncio.wait_for(asyncio.gather(*requests), SOURCE_TIMEOUT)
        finally:
            for task in requests:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
        result = parse_history(source, code, json.loads(texts[0]), expected_date, now, self.calendar)
        if expected_date == today:
            if not confirm_close(source, code, result.bars[-1], texts[1], now):
                raise ValueError("Same-day close is unconfirmed")
            result = replace(result, completion_note="独立盘后报价时间与四价校验通过；成交额终值未核实")
        return result

    async def _worker(self, code: str, expected_date: date) -> tuple[IndexHistory | None, str]:
        """Run one fallback chain and cache only the active baseline generation."""
        key = (code, expected_date)
        result, reason = None, "历史行情暂不可用"
        try:
            async def sources():
                nonlocal result, reason
                for source in ("eastmoney", "tencent"):
                    try:
                        result = await self._fetch_source(source, code, expected_date, now_beijing())
                        return
                    except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                        logger.warning("History source failed [%s %s]: %s", code, source, exc)
                        reason = "日线缺失、未更新、完成状态未确认或历史源不可用"
            try:
                await asyncio.wait_for(sources(), INDEX_TIMEOUT)
            except asyncio.TimeoutError:
                reason = "历史行情获取超时"
            if not self._closed and expected_date == self._active_date:
                self._cache[key] = (monotonic() + (SUCCESS_TTL if result else FAILURE_TTL), result, reason)
            return result, reason
        finally:
            self._inflight.pop(key, None)

    async def fetch(self, expected_date: date) -> HistoryBatch:
        """Share workers and retain partial success within the batch budget.

        Args:
            expected_date: Required completed trading date.

        Returns:
            Independent result dictionaries, including explicit failures.
        """
        if self._closed:
            return HistoryBatch(expected_date, {}, {item[0]: "历史客户端已关闭" for item in INDEX_CONFIG})
        if self._active_date is None or expected_date > self._active_date:
            self._active_date = expected_date
            self._cache.clear()
        histories, unavailable, pending = {}, {}, {}
        for code, *_ in INDEX_CONFIG:
            key = (code, expected_date)
            cached = self._cache.get(key)
            if cached and monotonic() < cached[0]:
                if cached[1] is not None:
                    item = cached[1]
                    if (item.bars[-1].trade_date == expected_date
                            and tuple(bar.trade_date for bar in item.bars)
                            == self.calendar.session_dates(item.bars[0].trade_date, expected_date)):
                        histories[code] = replace(item, from_cache=True)
                        continue
                else:
                    unavailable[code] = cached[2]
                    continue
            if key not in self._inflight:
                self._inflight[key] = asyncio.create_task(self._worker(code, expected_date))
            pending[code] = self._inflight[key]
        if pending:
            done, _ = await asyncio.wait(set(pending.values()), timeout=BATCH_TIMEOUT)
            for code, task in pending.items():
                if task not in done:
                    unavailable[code] = "历史行情批次超时"
                elif task.cancelled():
                    unavailable[code] = "历史行情请求已取消"
                else:
                    result, reason = task.result()
                    if result is not None:
                        histories[code] = result
                    else:
                        unavailable[code] = reason
        return HistoryBatch(expected_date, histories, unavailable)

    async def _close_resources(self):
        """Cancel and await every generation before releasing HTTP resources."""
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._inflight.clear()
            self._cache.clear()
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def close(self) -> None:
        """Share idempotent shutdown, surviving cancellation of a caller."""
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._close_task)
