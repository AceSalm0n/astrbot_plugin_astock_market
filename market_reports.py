"""Build and archive deterministic reports from verified session data."""

import math
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time

from .market_api import BEIJING_TZ, IndexData, MarketOverview
from .market_history import INDEX_CONFIG, DailyBar, HistoryBatch, IndexHistory
from .trend_analysis import calculate_indicators

MIDDAY_WINDOW = (time(11, 25), time(11, 30, 59, 999999))
REPORT_TIMES = {
    "midday": ((time(11, 35), time(11, 37), time(11, 39)), time(11, 40)),
    "close": ((time(15, 20), time(15, 25), time(15, 30)), time(15, 35)),
}
NAMES = {code: name for code, name, *_ in INDEX_CONFIG}
REPORT_LABELS = {"midday": "午间小结", "close": "收盘复盘"}
CLOSE_PROOF = "独立盘后报价时间与四价校验通过；成交额终值未核实"


@dataclass(frozen=True)
class MarketReport:
    """Hold one frozen source batch and its optional interpretation."""

    kind: str
    trade_date: date
    generated_at: datetime
    finalized: bool
    quotes: tuple[IndexData, ...]
    histories: dict[str, IndexHistory]
    missing: dict[str, str]
    analysis: str | None = None

    @property
    def count(self) -> int:
        return len(self.quotes) if self.kind == "midday" else len(self.histories)


def build_midday_report(
    overview: MarketOverview, trade_date: date, now: datetime
) -> MarketReport:
    """Filter a quote batch to the requested morning cutoff."""
    accepted = {}
    for quote in overview.indices:
        stamp = quote.quote_time
        if quote.code not in NAMES or stamp is None or stamp.utcoffset() is None:
            continue
        stamp = stamp.astimezone(BEIJING_TZ)
        if (
            stamp.date() == trade_date
            and MIDDAY_WINDOW[0] <= stamp.time() <= MIDDAY_WINDOW[1]
        ):
            accepted[quote.code] = quote
    missing = {
        code: "无符合午间时间口径的报价" for code in NAMES if code not in accepted
    }
    return MarketReport(
        "midday",
        trade_date,
        now,
        False,
        tuple(accepted[code] for code in NAMES if code in accepted),
        {},
        missing,
    )


def build_close_report(batch: HistoryBatch, now: datetime) -> MarketReport:
    """Keep only complete daily bars with the required baseline and close proof."""
    accepted = {}
    for code in NAMES:
        item = batch.histories.get(code)
        if (
            item is not None
            and len(item.bars) >= 21
            and item.bars[-1].trade_date == batch.expected_date
            and (
                batch.expected_date < now.astimezone(BEIJING_TZ).date()
                or item.completion_note == CLOSE_PROOF
            )
        ):
            accepted[code] = item
    missing = {
        code: batch.unavailable.get(code, "日线缺失或收盘未确认")
        for code in NAMES
        if code not in accepted
    }
    return MarketReport("close", batch.expected_date, now, False, (), accepted, missing)


def format_report(report: MarketReport) -> str:
    """Format audited market facts, with missing fields labeled explicitly."""
    lines = [f"📊 {REPORT_LABELS[report.kind]}｜{report.trade_date}（北京时间）"]
    if not report.finalized:
        lines.append("临时报告，待数据补齐")
    if not report.count:
        lines.append("本次报告数据不可用")
    ranking = []
    if report.kind == "midday":
        for q in report.quotes:
            pct = "未知" if q.change_pct is None else f"{q.change_pct:+.2f}%"
            movement = (
                f"{100 * (q.latest_price / q.open_price - 1):+.2f}%"
                if q.open_price
                else "未知"
            )
            bounds = (
                "未知"
                if q.low_price is None or q.high_price is None
                else f"{q.low_price:.2f}—{q.high_price:.2f}"
            )
            stamp = q.quote_time.astimezone(BEIJING_TZ)
            lines.append(
                f"{NAMES[q.code]} {q.latest_price:.2f}（{pct}）｜上午区间{bounds}｜较开盘{movement}｜{stamp:%H:%M:%S} {q.source}"
            )
            if q.change_pct is not None:
                ranking.append((q.change_pct, NAMES[q.code]))
        if report.count:
            overview = MarketOverview(list(report.quotes), report.generated_at)
            lines.append(
                f"午间沪深成交额：{overview.total_amount_text}（上证与深证快照合计）"
            )
    else:
        for code, item in report.histories.items():
            previous, bar = item.bars[-2:]
            change = 100 * (bar.close / previous.close - 1)
            amplitude = 100 * (bar.high - bar.low) / previous.close
            metrics = calculate_indicators(item)
            ma = " / ".join(
                f"MA{n} {metrics[f'ma_{n}']:.2f}（偏离{metrics[f'deviation_{n}']:+.2f}%）"
                for n in (5, 10, 20)
            )
            lines.extend(
                (
                    f"{NAMES[code]} {bar.close:.2f}（{change:+.2f}%）｜开{bar.open:.2f} 高{bar.high:.2f} 低{bar.low:.2f}｜振幅{amplitude:.2f}%",
                    f"  5日{metrics['return_5']:+.2f}%／20日{metrics['return_20']:+.2f}%｜{ma}｜{item.source}",
                )
            )
            ranking.append((change, NAMES[code]))
    if len(ranking) >= 2:
        lines.append(
            "已知指数表现："
            + "、".join(
                f"{name} {pct:+.2f}%" for pct, name in sorted(ranking, reverse=True)
            )
        )
    if report.missing:
        lines.append(
            "缺失："
            + "、".join(
                f"{NAMES[code]}（{reason}）" for code, reason in report.missing.items()
            )
        )
    if report.analysis:
        lines.append("🤖 报告解读\n" + report.analysis)
    return "\n".join(lines)


def report_to_dict(report: MarketReport) -> dict:
    """Encode dates explicitly for the versioned local JSON archive."""

    def encode(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [encode(item) for item in value]
        return value

    return {"version": 1, **encode(asdict(report))}


def report_from_dict(data: dict) -> MarketReport:
    """Load an archive and reject mismatched dates, malformed data or numbers."""
    try:
        if (
            data["version"] != 1
            or data["kind"] not in REPORT_TIMES
            or type(data["finalized"]) is not bool
        ):
            raise ValueError("Unsupported report schema")
        day = date.fromisoformat(data["trade_date"])
        stamp = datetime.fromisoformat(data["generated_at"])
        if stamp.utcoffset() is None:
            raise ValueError("Naive report timestamp")
        quotes = []
        for raw in data["quotes"]:
            row = raw.copy()
            for field in ("quote_time", "fetched_at"):
                if row[field] is not None:
                    row[field] = datetime.fromisoformat(row[field])
                    if row[field].utcoffset() is None:
                        raise ValueError("Naive quote timestamp")
            quote = IndexData(**row)
            if quote.latest_price <= 0 or any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in (
                    quote.latest_price,
                    quote.change,
                    quote.change_pct,
                    quote.open_price,
                    quote.high_price,
                    quote.low_price,
                    quote.pre_close,
                    quote.amount,
                    quote.volume,
                )
                if value is not None
            ):
                raise ValueError("Invalid archived quote")
            quotes.append(quote)
        histories = {}
        for code, raw in data["histories"].items():
            row = raw.copy()
            row["fetched_at"] = datetime.fromisoformat(row["fetched_at"])
            row["bars"] = tuple(
                DailyBar(**{**bar, "trade_date": date.fromisoformat(bar["trade_date"])})
                for bar in row["bars"]
            )
            series = IndexHistory(**row)
            if (
                code not in NAMES
                or series.code != code
                or series.fetched_at.utcoffset() is None
                or not 21 <= len(series.bars) <= 60
                or series.bars[-1].trade_date != day
                or any(
                    a.trade_date >= b.trade_date
                    for a, b in zip(series.bars, series.bars[1:])
                )
                or any(
                    not all(
                        type(v) in (int, float) and math.isfinite(v) and v > 0
                        for v in (b.open, b.high, b.low, b.close)
                    )
                    or not b.low
                    <= min(b.open, b.close)
                    <= max(b.open, b.close)
                    <= b.high
                    for b in series.bars
                )
            ):
                raise ValueError("Invalid archived daily bars")
            histories[code] = series
        missing = data["missing"]
        if not isinstance(missing, dict) or any(
            code not in NAMES or not isinstance(reason, str)
            for code, reason in missing.items()
        ):
            raise ValueError("Invalid report missing entries")
        analysis = data["analysis"]
        if analysis is not None and not isinstance(analysis, str):
            raise ValueError("Invalid report analysis")
        if data["kind"] == "midday":
            if histories:
                raise ValueError("Unexpected history in midday report")
            checked = build_midday_report(MarketOverview(quotes, stamp), day, stamp)
        else:
            if quotes:
                raise ValueError("Unexpected quotes in close report")
            checked = build_close_report(HistoryBatch(day, histories, missing), stamp)
        if checked.count != len(quotes) + len(histories) or set(checked.missing) != set(
            missing
        ):
            raise ValueError("Report archive completeness mismatch")
        return replace(
            checked, finalized=data["finalized"], missing=missing, analysis=analysis
        )
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("Malformed report archive") from exc
