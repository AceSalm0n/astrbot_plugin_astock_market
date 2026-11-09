"""Render session reports and index alerts as self-contained cards."""

from datetime import datetime
from pathlib import Path

from astrbot.api import logger

from . import card_renderer as card
from .market_alerts import AlertEvent
from .market_api import BEIJING_TZ, MarketOverview
from .market_history import HistoryBatch
from .market_reports import NAMES, REPORT_LABELS, MarketReport, report_index_rows
from .trading_calendar import TradingCalendar
from .trend_renderer import build_chart_data, render_trend_plot

_TITLE = (23, 53, 89)
_TEXT = (32, 46, 64)
_NOTE = (84, 101, 121)
_ACCENT = (32, 93, 159)


def render_report_card(
    report: MarketReport,
    *,
    calendar: TradingCalendar | None = None,
    background_path: Path | None = None,
) -> bytes:
    """Lay out one report with the chart and interpretation inside the card.

    Args:
        report: Verified morning quotes or completed closing history.
        calendar: Verified session calendar for the embedded closing chart.
        background_path: Optional upload for the glass background.

    Returns:
        One PNG containing the report's data.
    """
    state = "正式报告" if report.finalized else "临时报告，待数据补齐"
    sections: list[card.CardSection | bytes] = [
        [
            ("A 股市场简报", 26, _ACCENT),
            (REPORT_LABELS[report.kind], 48, _TITLE),
            (f"{report.trade_date}  北京时间", 30, _NOTE),
            (f"{state}  ·  {report.count}/{len(NAMES)} 只指数可用", 26, _NOTE),
        ]
    ]
    rows = report_index_rows(report)
    ranked = sorted(
        rows,
        key=lambda row: (row.change_pct is None, -(row.change_pct or 0)),
    )
    summary: card.CardSection = [("指数速览", 34, _TITLE)]
    for row in ranked:
        pct = "涨跌未知" if row.change_pct is None else f"{row.change_pct:+.2f}%"
        summary.append(
            [
                (NAMES[row.code], 30, _TEXT),
                (f"{row.price:.2f}", 30, _TEXT),
                (pct, 32, card.change_color(row.change_pct)),
            ]
        )
    if not rows:
        summary.append(("本次报告数据不可用", 30, _NOTE))
    if report.kind == "midday" and rows:
        overview = MarketOverview(list(report.quotes), report.generated_at)
        summary.append((f"午间沪深成交额  {overview.total_amount_text}", 28, _TEXT))
        summary.append(("仅合计上证与深证的午间快照", 24, _NOTE))
    sections.append(summary)

    chart_note = ""
    if report.kind == "close" and rows:
        try:
            code = "000001" if "000001" in report.histories else rows[0].code
            chart_data = build_chart_data(
                HistoryBatch(report.trade_date, report.histories, report.missing),
                sessions=60,
                code=code,
                calendar=calendar,
            )
            sections.append(
                render_trend_plot(chart_data, transparent=background_path is not None)
            )
        except Exception as exc:
            logger.warning("Embedded report chart unavailable: %s", type(exc).__name__)
            chart_note = "日 K 线暂不可用，指数明细见下方。"
    if chart_note:
        sections.append([(chart_note, 28, _NOTE)])

    for row in rows:
        pct = "未知" if row.change_pct is None else f"{row.change_pct:+.2f}%"
        panel: card.CardSection = [
            [
                (NAMES[row.code], 36, _TITLE),
                (row.code, 26, _NOTE),
                (f"涨跌  {pct}", 30, card.change_color(row.change_pct)),
            ]
        ]
        details = [
            (
                f"{metric.label}  {metric.value}",
                30,
                card.change_color(metric.change_pct)
                if metric.change_pct is not None or metric.label == "较开盘"
                else _TEXT,
            )
            for metric in row.metrics
        ]
        if report.kind == "close":
            for start, stop in ((0, 2), (2, 4), (4, 6)):
                panel.append(details[start:stop])
            for metric, detail in zip(row.metrics[6:], details[6:]):
                deviation = metric.deviation_pct
                panel.append(
                    [
                        detail,
                        (
                            f"偏离  {deviation:+.2f}%"
                            if deviation is not None
                            else "偏离  未知",
                            28,
                            card.change_color(deviation),
                        ),
                    ]
                )
        else:
            panel.extend(details)
        panel.append((f"来源  {row.provenance or '未知'}", 25, _NOTE))
        sections.append(panel)

    if report.missing:
        sections.append(
            [
                ("缺失数据", 34, _TITLE),
                *(
                    (f"{NAMES[code]}  {reason}", 28, _NOTE)
                    for code, reason in report.missing.items()
                ),
            ]
        )
    if report.analysis:
        sections.append(
            [
                ("报告解读", 34, _TITLE),
                *((line, 30, _TEXT) for line in report.analysis.splitlines()),
            ]
        )
    return card.render_card_sections(sections, background_path=background_path)


def render_alert_card(
    events: tuple[AlertEvent, ...],
    now: datetime,
    *,
    background_path: Path | None = None,
) -> bytes:
    """Show sampled alerts as separate panels in one image."""
    sections: list[card.CardSection | bytes] = [
        [
            ("A 股指数异动", 46, _TITLE),
            (f"{now.astimezone(BEIJING_TZ):%Y-%m-%d %H:%M}  北京时间", 28, _NOTE),
        ]
    ]
    for event in events:
        quote = event.quote
        pct = "涨跌未知" if quote.change_pct is None else f"{quote.change_pct:+.2f}%"
        stamp = quote.quote_time
        source_time = (
            stamp.astimezone(BEIJING_TZ).strftime("%H:%M:%S")
            if stamp is not None
            else "未知"
        )
        sections.append(
            [
                (event.name, 36, _TITLE),
                [
                    (f"{quote.latest_price:.2f}", 40, _TEXT),
                    (pct, 36, card.change_color(quote.change_pct)),
                ],
                (event.description, 30, _TEXT),
                (f"报价  {source_time}  ·  {quote.source or '未知'}", 26, _NOTE),
            ]
        )
    sections.append([("基于定时采样，非逐笔监控。", 26, _NOTE)])
    return card.render_card_sections(sections, background_path=background_path)
