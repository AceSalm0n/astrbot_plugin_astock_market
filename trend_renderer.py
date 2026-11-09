"""Build comparable close series and draw trend charts with optional Pillow."""

import math
from dataclasses import dataclass
from datetime import date
from io import BytesIO

from .card_renderer import FONT_CANDIDATES
from .market_history import DailyBar, HistoryBatch
from .market_reports import NAMES
from .trading_calendar import TradingCalendar

COLORS = ("#125ecb", "#d13a35", "#00836c", "#8a46ad", "#a05b00")
CANDLE_COLORS = ("#dc2626", "#168a53", "#657384")


@dataclass(frozen=True)
class ChartSeries:
    """A dated plotted series and moving averages for a single index."""

    code: str
    name: str
    source: str
    dates: tuple[date, ...]
    closes: tuple[float, ...]
    values: tuple[float, ...]
    candles: tuple[DailyBar, ...]
    moving_averages: dict[int, tuple[float | None, ...]]


@dataclass(frozen=True)
class TrendChartData:
    """Validated plot inputs shared by PNG and text outputs."""

    mode: str
    requested_sessions: int
    baseline: date
    series: tuple[ChartSeries, ...]
    notes: tuple[str, ...]


def build_chart_data(
    batch: HistoryBatch,
    *,
    sessions: int = 20,
    code: str | None = None,
    calendar: TradingCalendar | None = None,
) -> TrendChartData:
    """Prepare aligned close data without interpolating absent bars.

    Args:
        batch: Verified index histories sharing the same final date.
        sessions: Number of requested close points, 20 or 60.
        code: Optional index code to select a single series.
        calendar: Calendar used to independently validate session continuity.

    Returns:
        A chart with a common date axis and any completeness notes.

    Raises:
        ValueError: Input parameters or the verified series are insufficient.
    """
    if sessions not in (20, 60) or (code is not None and code not in NAMES):
        raise ValueError("指数无效，或交易日数不是 20／60")
    notes = []
    items = []
    for index in (code,) if code is not None else NAMES:
        item = batch.histories.get(index)
        dates = tuple(bar.trade_date for bar in item.bars) if item else ()
        if (
            len(dates) < 21
            or dates[-1] != batch.expected_date
            or any(a >= b for a, b in zip(dates, dates[1:]))
            or any(
                not all(
                    math.isfinite(price) and price > 0
                    for price in (bar.open, bar.close, bar.high, bar.low)
                )
                or not bar.low
                <= min(bar.open, bar.close)
                <= max(bar.open, bar.close)
                <= bar.high
                for bar in item.bars
            )
            or (
                calendar is not None
                and dates != calendar.session_dates(dates[0], dates[-1])
            )
        ):
            notes.append(
                f"{NAMES[index]}缺失：{batch.unavailable.get(index, '日线不连续或样本不足')}"
            )
            continue
        items.append(item)
    if not items:
        raise ValueError("没有合格的已确认日线。" + "；".join(notes))

    length = min(sessions, *(len(item.bars) for item in items))
    axis = tuple(bar.trade_date for bar in items[0].bars[-length:])
    if any(
        tuple(bar.trade_date for bar in item.bars[-length:]) != axis for item in items
    ):
        raise ValueError("日线日期无法按同一连续交易日区间对齐")
    mode = "comparison" if len(items) > 1 else "single"
    if mode == "single" and code is None:
        notes.append("仅一只指数可用，显示单指数走势")
    if length < sessions:
        notes.append(f"请求{sessions}个交易日，实际共同样本{length}个收盘点")
    series = []
    for item in items:
        full = tuple(bar.close for bar in item.bars)
        closes = full[-length:]
        if mode == "single":
            values = closes
            averages = {
                n: tuple(
                    sum(full[i - n + 1 : i + 1]) / n if i >= n - 1 else None
                    for i in range(len(full) - length, len(full))
                )
                for n in (5, 10, 20)
            }
        else:
            values = tuple(100 * (point / closes[0] - 1) for point in closes)
            averages = {}
        series.append(
            ChartSeries(
                item.code,
                NAMES[item.code],
                item.source,
                axis,
                closes,
                values,
                item.bars[-length:] if mode == "single" else (),
                averages,
            )
        )
    return TrendChartData(
        mode, sessions, batch.expected_date, tuple(series), tuple(notes)
    )


def format_chart_summary(data: TrendChartData) -> str:
    """Render the same plotted window as text."""
    axis = data.series[0].dates
    lines = [f"指数走势｜{axis[0]} 至 {axis[-1]}｜{len(axis)} 个已确认收盘点"]
    for series in data.series:
        change = 100 * (series.closes[-1] / series.closes[0] - 1)
        line = f"{series.name} {series.closes[-1]:.2f}｜图示区间{change:+.2f}%｜{series.source}"
        if series.moving_averages:
            bar = series.candles[-1]
            line += f"\n  开{bar.open:.2f} 高{bar.high:.2f} 低{bar.low:.2f} 收{bar.close:.2f}"
            line += "\n  " + "、".join(
                f"MA{n} {values[-1]:.2f}"
                for n, values in series.moving_averages.items()
            )
        lines.append(line)
    return "\n".join((*lines, *data.notes))


def render_trend_chart(data: TrendChartData) -> bytes:
    """Draw a PNG from validated close values, keeping warmup gaps empty."""
    from PIL import Image, ImageDraw, ImageFont

    for candidate in FONT_CANDIDATES:
        try:
            title_font = ImageFont.truetype(candidate, 36)
            label_font = ImageFont.truetype(candidate, 22)
            small_font = ImageFont.truetype(candidate, 19)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("No supported Chinese font is available")

    image = Image.new("RGB", (1200, 900), "#f5f8fc")
    draw = ImageDraw.Draw(image)
    axis = data.series[0].dates
    comparison = data.mode == "comparison"
    draw.text(
        (46, 36),
        f"{len(data.series)}只指数收盘走势对比"
        if comparison
        else f"{data.series[0].name}日 K 线",
        "#18314a",
        font=title_font,
    )
    draw.text(
        (46, 91),
        f"{axis[0]} 至 {axis[-1]}｜{len(axis)} 个已确认收盘点｜北京时间",
        "#445b73",
        font=label_font,
    )

    candles = data.series[0].candles if not comparison else ()
    if comparison:
        lines = [(item.name, item.values) for item in data.series]
        palette = COLORS
    else:
        lines = [
            (f"MA{n}", values) for n, values in data.series[0].moving_averages.items()
        ]
        palette = (COLORS[0], COLORS[3], COLORS[4])
    samples = [point for _, values in lines for point in values if point is not None]
    samples.extend(price for bar in candles for price in (bar.low, bar.high))
    low, high = min(samples), max(samples)
    margin = max((high - low) * 0.09, 0.1)
    low, high = low - margin, high + margin
    left, top, right, bottom = 130, 174, 1130, 625
    draw.rounded_rectangle((46, 136, 1154, 680), radius=18, fill="white")

    def y_at(point):
        return bottom - (point - low) / (high - low) * (bottom - top)

    for i in range(5):
        value = low + (high - low) * i / 4
        y = y_at(value)
        draw.line((left, y, right, y), fill="#e1e8ef", width=1)
        draw.text(
            (left - 12, y),
            f"{value:+.1f}%" if comparison else f"{value:.1f}",
            "#445b73",
            font=small_font,
            anchor="rm",
        )
    for i in (0, len(axis) // 2, len(axis) - 1):
        x = left + i / (len(axis) - 1) * (right - left)
        draw.text(
            (x, bottom + 13),
            axis[i].strftime("%m-%d"),
            "#445b73",
            font=small_font,
            anchor="mt",
        )
    if candles:
        half_width = min(12, max(3, (right - left) / len(axis) * 0.3))
        for i, bar in enumerate(candles):
            x = left + i / (len(axis) - 1) * (right - left)
            color = CANDLE_COLORS[
                0 if bar.close > bar.open else 1 if bar.close < bar.open else 2
            ]
            draw.line((x, y_at(bar.high), x, y_at(bar.low)), fill=color, width=2)
            body_top, body_bottom = sorted((y_at(bar.open), y_at(bar.close)))
            draw.rectangle(
                (
                    x - half_width,
                    body_top,
                    x + half_width,
                    max(body_bottom, body_top + 1),
                ),
                fill=color,
            )
        draw.text(
            (48, 704), f"收盘  {candles[-1].close:.2f}", "#18314a", font=label_font
        )
    for j, (label, values) in enumerate(lines):
        points = []
        for i, value in enumerate(values):
            if value is None:
                if len(points) >= 2:
                    draw.line(points, fill=palette[j], width=3)
                points.clear()
            else:
                points.append(
                    (left + i / (len(axis) - 1) * (right - left), y_at(value))
                )
        if len(points) >= 2:
            draw.line(points, fill=palette[j], width=4 if j == 0 else 3)
        last = values[-1]
        value_label = f"{last:+.2f}%" if comparison else f"{last:.2f}"
        draw.text(
            (48 + (j + bool(candles)) * 225, 704),
            f"{label}  {value_label}",
            palette[j],
            font=label_font,
        )

    footer = "来源：" + "、".join(f"{item.name} {item.source}" for item in data.series)
    notes = [footer, *data.notes]
    if candles:
        notes.insert(0, "日 K 线按开收盘比较着色，红涨、绿跌、灰平。")
    wrapped = []
    for note in notes:
        line = ""
        for char in note:
            if line and small_font.getlength(line + char) > 1100:
                wrapped.append(line)
                line = ""
            line += char
        if line:
            wrapped.append(line)
    height = max(image.height, 786 + len(wrapped) * 27)
    if height > image.height:
        extended = Image.new("RGB", (image.width, height), "#f5f8fc")
        extended.paste(image, (0, 0))
        image, draw = extended, ImageDraw.Draw(extended)
    for i, line in enumerate(wrapped):
        draw.text((48, 754 + i * 27), line, "#445b73", font=small_font)
    result = BytesIO()
    image.save(result, format="PNG")
    return result.getvalue()
