"""Build dated trend context from verified bars without network or LLM calls."""

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean, stdev

from astrbot.api import logger

from .market_history import (
    BEIJING_TZ,
    INDEX_CONFIG,
    HistoryBatch,
    IndexHistory,
    TradingCalendar,
)

TREND_ANALYSIS_PROMPT = """你是一名谨慎、客观的A股指数分析助手。仅依据下面的数据，分析基准交易日
之后第3至第5个交易日的趋势倾向。此任务不要求次日涨跌预测。

优先遵守以下事实边界，无法证实时省略结论：
1. 原样沿用程序给出的T+3/T+5交易日期；周末和休市日不计入，不能称为自然日或日历日。
2. 成交额比较未知时，只能写“成交额比较未知，无法判断量能”。单日绝对成交额不能证明放量、
缩量、成交活跃、买盘支撑或增量资金；价格涨跌也不能证明资金流入流出。
3. 输入仅有基准日均线值，不能据此声称均线已经拐头或连续多日上行/下行。
累计涨跌不是连续涨跌天数；只引用能由输入直接支持的数字和比较，不补全故事。

查询时间（北京时间）：{current_time}
基准交易日与收盘口径：{baseline}
观察区间及当前已进入的阶段：{horizon}
当前行情（独立快照，不作为已完成日线）：
{market_data}
历史日线摘要、来源、样本量与已计算指标：
{trend_data}
程序判定的数据质量及整体分析权限：
{quality_notes}

输出约500—700字纯文本，按以下标题分段，不使用加粗或表格。各段简洁，避免重复论据：

市场状态
用约70字简述当前表现和近期趋势，区分已完成日线与当前快照；不要逐项复述行情。

3—5交易日趋势
对有有效历史的指数分别给出偏强、偏弱、震荡或证据不足的判断，每只指数最多一句。
仅在程序允许时给出整体倾向；否则写“整体证据不足”。解释指数分化。
本段合计约300字，引用最关键的两项历史依据，并明确至少一项反向证据或尚未满足的条件。
判断属于推测，不将均线排列、连续上涨或历史趋势写成未来必然延续。

观察条件
用约150字说明到第3个交易日应观察什么，到第5个交易日哪些表现支持或否定原判断。
可以引用输入中的均线和历史区间作为参考；不得虚构价格目标、支撑阻力、
未来涨幅或买卖点。若基准区间已开始，不得把它说成从查询时刻重新计时。

数据局限
用约80字如实披露缺失指数、样本不足、缺失成交额、混合来源、时间未知或缓存状态。
没有已知反向证据时说明数据维度有限，不得编造反向事实凑段落。

约束：不要自行补查或假装查到数据；不编造新闻、政策、行业板块、资金流向，
不把指数涨跌数量当作全市场股票涨跌家数。缺失值不是零。
不得输出数字概率、胜率、主观置信度评分或具体投资操作建议。
数据不足时允许不作方向判断。优先保证依据、反证和局限完整，不机械截断正文。
结尾注明“趋势研判仅供参考，不构成投资建议”。"""


@dataclass(frozen=True)
class TrendContext:
    """Keep program-owned dates and availability separate from generated prose."""

    baseline: date
    target_dates: tuple[date, date] | None
    history_text: str
    quality_notes: str
    eligible_codes: tuple[str, ...]
    overall_allowed: bool
    header: str


def load_trading_calendar(path: Path) -> TradingCalendar:
    """Read only explicit, sourced annual closures; never fill unknown years.

    Args:
        path: Bundled annual calendar JSON.

    Returns:
        Validated common Shanghai/Shenzhen calendar.

    Raises:
        ValueError: Metadata or closure dates are invalid.
        OSError: The calendar cannot be read.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("years"), dict):
        raise ValueError("Invalid trading calendar schema")
    years = {}
    for raw_year, annual in data["years"].items():
        year = int(raw_year)
        if not annual.get("sources") or not isinstance(annual.get("closed_weekdays"), list):
            raise ValueError("Missing annual calendar metadata")
        date.fromisoformat(annual["verified_at"])
        days = frozenset(date.fromisoformat(value) for value in annual["closed_weekdays"])
        if any(day.year != year or day.weekday() >= 5 for day in days):
            raise ValueError("Invalid weekday closure")
        years[year] = days
    if not years:
        raise ValueError("Empty trading calendar")
    return TradingCalendar(years)


def resolve_baseline(now: datetime, calendar: TradingCalendar) -> date | None:
    """Resolve the required daily baseline with a 15:15 source-update buffer.

    Args:
        now: Aware query time.
        calendar: Verified annual dates.

    Returns:
        Last required completed session, or None if coverage is unknown.
    """
    if now.tzinfo is None:
        return None
    local = now.astimezone(BEIJING_TZ)
    current = local.date()
    if current.year not in calendar.years:
        return None
    if local.time() < time(15, 15):
        current -= timedelta(days=1)
    for _ in range(370):
        try:
            if calendar.session_dates(current, current):
                return current
        except ValueError:
            return None
        current -= timedelta(days=1)
    return None


def calculate_indicators(history: IndexHistory) -> dict[str, float | None]:
    """Compute exact close-based metrics with no imputation or annualization.

    Args:
        history: At least 21 complete ordered bars from one source.

    Returns:
        Returns and daily volatility in percent, means in price units,
        and an optional dimensionless turnover ratio.

    Raises:
        ValueError: The sample is too short or prices are nonfinite.
    """
    bars = history.bars
    if len(bars) < 21 or any(not math.isfinite(bar.close) or bar.close <= 0 for bar in bars):
        raise ValueError("Indicators require 21 valid closes")
    result = {}
    for n in (5, 10, 20):
        average = mean(bar.close for bar in bars[-n:])
        result[f"return_{n}"] = 100 * (bars[-1].close / bars[-n - 1].close - 1)
        result[f"ma_{n}"] = average
        result[f"deviation_{n}"] = 100 * (bars[-1].close / average - 1)
    result["volatility_20"] = stdev(
        100 * (second.close / first.close - 1)
        for first, second in zip(bars[-21:-1], bars[-20:])
    )
    result["range_low"] = min(bar.low for bar in bars[-20:])
    result["range_high"] = max(bar.high for bar in bars[-20:])
    amounts = [bar.amount_yuan for bar in bars[-20:]]
    result["amount_ratio"] = None
    if history.amount_scope and all(value is not None and math.isfinite(value) and value >= 0 for value in amounts):
        denominator = mean(amounts)
        if denominator > 0:
            result["amount_ratio"] = mean(amounts[-5:]) / denominator
    return result


def build_trend_context(
    batch: HistoryBatch, now: datetime, calendar: TradingCalendar,
    manual_closed_dates: list[str],
) -> TrendContext:
    """Build bounded per-index evidence and fixed trend permissions.

    Args:
        batch: Validated histories or explicit source failures.
        now: Aware query time.
        calendar: Verified session dates.
        manual_closed_dates: Read-only scheduler hints about possible closures.

    Returns:
        Context with relative targets if future calendar years are unknown.
    """
    baseline = batch.expected_date
    notes, sections, eligible, missing = [], [], [], []
    future = []
    cursor = baseline
    try:
        while len(future) < 5:
            cursor += timedelta(days=1)
            if calendar.session_dates(cursor, cursor):
                future.append(cursor)
        targets = (future[2], future[4])
    except ValueError:
        targets = None
        notes.append("未来年度日历未覆盖，观察区间仅使用相对交易日T+3至T+5")
    known_target_end = targets[1] if targets else cursor - timedelta(days=1)
    start = min((item.bars[0].trade_date for item in batch.histories.values() if item.bars), default=baseline)
    conflict = False
    if not isinstance(manual_closed_dates, list):
        logger.warning("Ignoring malformed manual closure configuration")
        manual_closed_dates = []
    for raw in manual_closed_dates:
        try:
            day = date.fromisoformat(raw)
        except (ValueError, TypeError):
            logger.warning("Ignoring invalid manual closure date")
            continue
        if day.year not in calendar.years:
            if day > baseline:
                notes.append("手动未来休市日期在未知年份，未据此推算目标日期")
        elif start <= day <= known_target_end and calendar.session_dates(day, day):
            conflict = True
    if conflict:
        notes.append("临时休市需核实：手动配置与本次相关官方交易日冲突，趋势不可用")
    if resolve_baseline(now, calendar) != baseline:
        conflict = True
        notes.append("日线基准与查询时刻要求不一致，趋势不可用")
    for code, name, *_ in INDEX_CONFIG:
        item = batch.histories.get(code)
        if item is None:
            reason = f"缺失{name}历史：{batch.unavailable.get(code, '未获取')}"
            notes.append(reason)
            missing.append(reason)
            continue
        if conflict:
            continue
        try:
            if (not item.bars or item.bars[-1].trade_date != baseline
                    or tuple(bar.trade_date for bar in item.bars)
                    != calendar.session_dates(item.bars[0].trade_date, baseline)):
                raise ValueError("History dates differ from verified sessions")
            metrics = calculate_indicators(item)
        except ValueError:
            reason = f"缺失{name}合格历史：样本或交易日完整性不足"
            notes.append(reason)
            missing.append(reason)
            continue
        eligible.append(code)
        age = max(0, int((now - item.fetched_at).total_seconds()))
        lines = [f"{name}（{code}），{len(item.bars)}日样本，来源：{item.source}，"
                 f"{'缓存' if item.from_cache else '本次获取'}，采集时间：{item.fetched_at.astimezone(BEIJING_TZ):%Y-%m-%d %H:%M:%S}，年龄{age}秒",
                 f"完成依据：{item.completion_note}"]
        for n in (5, 10, 20):
            lines.append(f"{n}日累计涨幅{metrics[f'return_{n}']:+.2f}%，MA{n}={metrics[f'ma_{n}']:.2f}，收盘偏离{metrics[f'deviation_{n}']:+.2f}%")
        lines.append(f"20日日收益样本波动{metrics['volatility_20']:.2f}%，20日观察区间{metrics['range_low']:.2f}—{metrics['range_high']:.2f}")
        ratio = metrics["amount_ratio"]
        lines.append(f"5日/20日平均成交额比：{ratio:.2f}" if ratio is not None else "成交额比较未知：缺失金额或统计范围/终值未核实")
        if len(item.bars) < 60:
            notes.append(f"{name}实际样本{len(item.bars)}日，少于目标60日")
        lines.append("近期10日：日期、收盘、当日涨跌幅")
        for i in range(len(item.bars) - 10, len(item.bars)):
            bar = item.bars[i]
            change = 100 * (bar.close / item.bars[i - 1].close - 1)
            lines.append(f"{bar.trade_date} {bar.close:.2f} {change:+.2f}%")
        sections.append("\n".join(lines))
    overall = all(code in eligible for code in ("000001", "399001", "000300"))
    if len({batch.histories[code].source for code in eligible}) > 1:
        notes.append("混合来源：各指数使用整段单一来源，不跨源拼接量额")
    notes.append("允许讨论整体倾向，但不代表预测可信度" if overall else "整体证据不足：上证/深证/沪深300历史未全部合格")
    horizon = f"T+3至T+5交易日（{targets[0]}至{targets[1]}）" if targets else "T+3至T+5交易日（未来年度日历未知）"
    header = f"趋势基准：{baseline}日收盘\n观察区间：{horizon}\n历史可用：{len(eligible)}/5只指数\n"
    if not overall:
        header += "整体证据不足\n"
    if missing:
        header += "\n".join(missing) + "\n"
    if now.astimezone(BEIJING_TZ).date() > baseline:
        notes.append("查询已在基准T之后；交易日盘中为T+1观察，日线判断仍基于T收盘，不从查询时刻重新计时")
    header += "方法说明：LLM定性趋势研判，预测准确率未经验证。"
    return TrendContext(baseline, targets, "\n\n".join(sections), "\n".join(notes), tuple(eligible), overall, header)
