"""
A股大盘指数数据接口模块
支持多数据源：东方财富 push2 API、腾讯财经（备用）、新浪财经（备用）
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiohttp

from astrbot.api import logger

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class IndexData:
    """A股指数实时数据

    涨跌幅基于昨收价计算，若昨收为 0 则 fallback 到 API 返回的涨跌额。
    """
    code: str            # 指数代码，如 000001
    name: str            # 指数名称，如 上证指数
    latest_price: float  # 最新点位
    change: float        # 涨跌额
    change_pct: float    # 涨跌幅（百分比）
    open_price: float    # 开盘价
    high_price: float    # 最高价
    low_price: float     # 最低价
    pre_close: float     # 昨收价
    volume: float        # 成交量（手）
    amount: float        # 成交额（元）

    @property
    def change_symbol(self) -> str:
        if self.change_pct > 0:
            return "📈"
        elif self.change_pct < 0:
            return "📉"
        return "➡️"

    @property
    def status_text(self) -> str:
        if self.change_pct >= 3:
            return "🔥 大涨"
        elif self.change_pct >= 1:
            return "↗️ 上涨"
        elif self.change_pct > 0:
            return "↑ 微涨"
        elif self.change_pct <= -3:
            return "💥 大跌"
        elif self.change_pct <= -1:
            return "↘️ 下跌"
        elif self.change_pct < 0:
            return "↓ 微跌"
        return "➡️ 平盘"


@dataclass
class MarketOverview:
    """大盘概览，包含主要指数和全市场概况"""
    indices: list[IndexData]
    fetch_time: datetime
    source: str = ""

    @property
    def total_amount_text(self) -> str:
        total = sum(i.amount for i in self.indices if i.amount)
        if total >= 1e8:
            return f"{total / 1e8:.0f}亿"
        elif total >= 1e4:
            return f"{total / 1e4:.0f}万"
        return f"{total:.0f}"

    @property
    def up_count(self) -> int:
        return sum(1 for i in self.indices if i.change_pct > 0)

    @property
    def down_count(self) -> int:
        return sum(1 for i in self.indices if i.change_pct < 0)

    @property
    def flat_count(self) -> int:
        return sum(1 for i in self.indices if i.change_pct == 0)


# ---------------------------------------------------------------------------
# 指数配置
# 东方财富 secid 规则：沪市 1.{code}，深市 0.{code}
# ---------------------------------------------------------------------------

_INDEX_CONFIG = [
    # (code, name, secid)
    ("000001", "上证指数",  "1.000001"),
    ("399001", "深证成指",  "0.399001"),
    ("399006", "创业板指",  "0.399006"),
    ("000688", "科创50",    "1.000688"),
    ("000300", "沪深300",   "0.000300"),
]

# 东方财富 push2 字段说明（与 fund_analyzer 验证一致）
# f43=最新价  f44=最高  f45=最低  f46=开盘
# f47=成交量(手)  f48=成交额(元)
# f57=代码  f58=名称  f60=昨收
# f168=换手率  f169=涨跌额  f170=涨跌幅(需/100)
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


# ---------------------------------------------------------------------------
# 公共 HTTP session 管理
# ---------------------------------------------------------------------------

_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers=_HEADERS,
        )
    return _session


# ---------------------------------------------------------------------------
# 数据源：东方财富 push2
# ---------------------------------------------------------------------------

async def _fetch_from_eastmoney(code: str, secid: str) -> Optional[dict]:
    """从东方财富 push2 获取单只指数行情"""
    url = (
        f"http://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}&fields={_EM_FIELDS}"
    )
    session = await _get_session()
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.debug(f"东方财富请求失败 HTTP {resp.status} [{code}]")
                return None
            text = await resp.text()
            data = json.loads(text)
            if data.get("data"):
                return data["data"]
    except Exception as e:
        logger.debug(f"东方财富请求异常 [{code}]: {e}")
    return None


def _parse_eastmoney(d: dict) -> Optional[IndexData]:
    """解析东方财富返回的 JSON → IndexData

    注意：东方财富指数行情的价格/涨跌字段均 ×100 返回，需 ÷100。
    """
    try:
        code = str(d.get("f57", ""))
        name = str(d.get("f58", ""))
        price = _to_float(d.get("f43")) / 100
        high = _to_float(d.get("f44")) / 100
        low = _to_float(d.get("f45")) / 100
        open_p = _to_float(d.get("f46")) / 100
        pre_close = _to_float(d.get("f60")) / 100
        change = _to_float(d.get("f169")) / 100
        change_pct = _to_float(d.get("f170")) / 100  # API 返回如 117 表示 1.17%
        volume = _to_float(d.get("f47"))    # 手，无需转换
        amount = _to_float(d.get("f48"))     # 元，无需转换

        if price == 0 and change == 0:
            return None  # 无效数据

        # 如果接口未返回涨跌幅但提供了涨跌额，自行计算
        if change_pct == 0 and pre_close > 0:
            change_pct = (change / pre_close) * 100

        return IndexData(
            code=code, name=name, latest_price=price,
            change=change, change_pct=round(change_pct, 2),
            open_price=open_p, high_price=high, low_price=low,
            pre_close=pre_close, volume=volume, amount=amount,
        )
    except Exception as e:
        logger.warning(f"解析东方财富数据失败: {e}")
        return None


# ---------------------------------------------------------------------------
# 数据源：腾讯财经 qt.gtimg.cn（备用）
# ---------------------------------------------------------------------------

_TENCENT_CODES = {
    "000001": "sh000001",
    "399001": "sz399001",
    "399006": "sz399006",
    "000688": "sh000688",
    "000300": "sz000300",
}


async def _fetch_from_tencent(code: str) -> Optional[IndexData]:
    """从腾讯财经 qt.gtimg.cn 获取指数行情"""
    tc = _TENCENT_CODES.get(code)
    if not tc:
        return None
    url = f"https://qt.gtimg.cn/q={tc}"
    session = await _get_session()
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            text = await resp.text(encoding="gbk")
            # 返回格式: v_sh000001="1...";
            match = re.search(r'"([^"]+)"', text)
            if not match:
                return None
            parts = match.group(1).split("~")
            if len(parts) < 40:
                return None
            # 腾讯字段说明
            name = parts[1]           # 名称
            open_p = _to_float(parts[5])   # 开盘
            pre_close = _to_float(parts[4]) # 昨收
            price = _to_float(parts[3])     # 当前
            high = _to_float(parts[33])     # 最高
            low = _to_float(parts[34])      # 最低
            volume = _to_float(parts[6])    # 成交量(手)
            amount = _to_float(parts[37])   # 成交额(元)

            change = price - pre_close if pre_close > 0 else 0
            change_pct = (change / pre_close * 100) if pre_close > 0 else 0

            return IndexData(
                code=code, name=name, latest_price=price,
                change=round(change, 2), change_pct=round(change_pct, 2),
                open_price=open_p, high_price=high, low_price=low,
                pre_close=pre_close, volume=volume, amount=amount,
            )
    except Exception as e:
        logger.debug(f"腾讯财经请求异常 [{code}]: {e}")
    return None


# ---------------------------------------------------------------------------
# 数据源：新浪财经（备用）
# ---------------------------------------------------------------------------

async def _fetch_from_sina(code: str) -> Optional[IndexData]:
    """从新浪财经 hq.sinajs.cn 获取指数行情"""
    tc = _TENCENT_CODES.get(code)
    if not tc:
        return None
    url = f"https://hq.sinajs.cn/list={tc}"
    session = await _get_session()
    try:
        async with session.get(
            url,
            headers={**_HEADERS, "Referer": "https://finance.sina.com.cn/"},
        ) as resp:
            if resp.status != 200:
                return None
            text = await resp.text(encoding="gbk")
            match = re.search(r'"([^"]+)"', text)
            if not match:
                return None
            parts = match.group(1).split(",")
            if len(parts) < 32:
                return None
            name = parts[0]
            open_p = _to_float(parts[1])
            pre_close = _to_float(parts[2])
            price = _to_float(parts[3])
            high = _to_float(parts[4])
            low = _to_float(parts[5])
            volume = _to_float(parts[8])   # 成交量(手)
            amount = _to_float(parts[9])   # 成交额(元)

            change = price - pre_close if pre_close > 0 else 0
            change_pct = (change / pre_close * 100) if pre_close > 0 else 0

            return IndexData(
                code=code, name=name, latest_price=price,
                change=round(change, 2), change_pct=round(change_pct, 2),
                open_price=open_p, high_price=high, low_price=low,
                pre_close=pre_close, volume=volume, amount=amount,
            )
    except Exception as e:
        logger.debug(f"新浪财经请求异常 [{code}]: {e}")
    return None


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def _to_float(v) -> float:
    """安全转为 float，None／空／非数字均返回 0"""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


async def fetch_market_overview() -> MarketOverview:
    """获取 A 股大盘主要指数数据

    数据源优先级：东方财富 push2 → 腾讯财经 → 新浪财经
    """
    indices: list[IndexData] = []
    sources_used: set[str] = set()

    for code, name, secid in _INDEX_CONFIG:
        data = await _fetch_from_eastmoney(code, secid)
        if data:
            parsed = _parse_eastmoney(data)
            if parsed:
                indices.append(parsed)
                sources_used.add("东方财富")
                continue

        # 备用：腾讯
        parsed = await _fetch_from_tencent(code)
        if parsed:
            indices.append(parsed)
            sources_used.add("腾讯财经")
            continue

        # 备用：新浪
        parsed = await _fetch_from_sina(code)
        if parsed:
            indices.append(parsed)
            sources_used.add("新浪财经")
            continue

        logger.warning(f"所有数据源均无法获取指数 [{code} {name}]")

    source_text = "、".join(sorted(sources_used)) if sources_used else "未知"

    return MarketOverview(
        indices=indices,
        fetch_time=datetime.now(),
        source=source_text,
    )


async def fetch_single_index(code: str) -> Optional[IndexData]:
    """获取单只指数数据"""
    config_map = {c: (n, s) for c, n, s in _INDEX_CONFIG}
    if code not in config_map:
        # 尝试补全：用户可能输入 "000001" 或 "1.000001"
        for c, n, s in _INDEX_CONFIG:
            if code in (c, s):
                code = c  # 统一为短 code
                break
        else:
            return None

    _, secid = config_map[code]

    # 东方财富
    raw = await _fetch_from_eastmoney(code, secid)
    if raw:
        parsed = _parse_eastmoney(raw)
        if parsed:
            return parsed

    # 腾讯
    parsed = await _fetch_from_tencent(code)
    if parsed:
        return parsed

    # 新浪
    parsed = await _fetch_from_sina(code)
    return parsed


def format_market_overview_text(overview: MarketOverview, detail: bool = True) -> str:
    """将 MarketOverview 格式化为可读文本"""
    now = overview.fetch_time.strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 A股大盘数据 ({now})", ""]

    for idx in overview.indices:
        symbol = idx.change_symbol
        lines.append(
            f"{symbol} {idx.name}（{idx.code}）\n"
            f"   点位：{idx.latest_price:.2f}  "
            f"涨跌：{idx.change:+.2f}  "
            f"涨幅：{idx.change_pct:+.2f}%\n"
            f"   最高：{idx.high_price:.2f}  "
            f"最低：{idx.low_price:.2f}  "
            f"昨收：{idx.pre_close:.2f}\n"
            f"   成交额：{_fmt_amount(idx.amount)}"
        )

    if not detail:
        return "\n".join(lines)

    # 全市场概况
    total_amount = sum(i.amount for i in overview.indices if i.amount)
    up = overview.up_count
    down = overview.down_count
    lines.extend([
        "",
        f"📈 综合概况",
        f"   监控指数：{len(overview.indices)} 只 "
        f"（上涨 {up} / 下跌 {down} / 平盘 {overview.flat_count}）",
        f"   合计成交额：{_fmt_amount(total_amount)}",
    ])

    return "\n".join(lines)


def _fmt_amount(amount: float) -> str:
    if amount >= 1e8:
        return f"{amount / 1e8:.2f} 亿元"
    elif amount >= 1e4:
        return f"{amount / 1e4:.2f} 万元"
    return f"{amount:.0f} 元"


# ---------------------------------------------------------------------------
# Session 清理
# ---------------------------------------------------------------------------

async def close_session():
    """关闭全局 HTTP session"""
    global _session
    if _session and not _session.closed:
        await _session.close()
