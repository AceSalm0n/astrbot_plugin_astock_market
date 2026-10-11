"""Render a fixed five-index overview as an in-memory PNG card."""

from io import BytesIO
from pathlib import Path

from .market_api import BEIJING_TZ, MarketOverview

_UP = (203, 55, 64)
_DOWN = (24, 135, 95)
_NEUTRAL = (77, 88, 105)
_INK = (32, 46, 64)
_MUTED = (98, 113, 130)
_INDICES = (
    ("000001", "上证指数"),
    ("399001", "深证成指"),
    ("399006", "创业板指"),
    ("000688", "科创50"),
    ("000300", "沪深300"),
)
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def render_market_card(overview: MarketOverview) -> bytes:
    """Render quotes with explicit missing rows and provenance.

    Args:
        overview: Existing market snapshot; its response clock anchors quote age.

    Returns:
        PNG bytes without writing runtime files.

    Raises:
        ImportError: Pillow is unavailable.
        RuntimeError: No supported Chinese font can be loaded.
    """
    from PIL import Image, ImageDraw, ImageFont

    fonts = {}
    for candidate in _FONT_CANDIDATES:
        try:
            fonts = {
                size: ImageFont.truetype(str(Path(candidate)), size)
                for size in (22, 26, 30, 36, 40)
            }
            break
        except OSError:
            continue
    if not fonts:
        raise RuntimeError("No supported Chinese font is available")

    def timestamp(value):
        """Display a source time without inventing an absent timezone.

        Args:
            value: A source timestamp or None.

        Returns:
            Beijing time, or an explicit unknown-time label.
        """
        if value is None:
            return "未知"
        if value.utcoffset() is None:
            return f"{value:%Y-%m-%d %H:%M:%S}（时区未知）"
        return f"{value.astimezone(BEIJING_TZ):%Y-%m-%d %H:%M:%S}"

    def age(value, cached):
        """Describe acquisition age relative to this snapshot's response clock.

        Args:
            value: Actual acquisition time, possibly unknown.
            cached: Whether the quote was reused from the local cache.

        Returns:
            An age or an explanation of an unknown or future acquisition time.
        """
        label = "缓存年龄" if cached else "采集距响应"
        if (
            value is None
            or value.utcoffset() is None
            or overview.fetch_time.utcoffset() is None
        ):
            return f"{label}未知"
        seconds = int((overview.fetch_time - value).total_seconds())
        if seconds < 0:
            return f"采集晚于响应 {-seconds} 秒"
        return f"{label} {seconds} 秒"

    quotes = {quote.code: quote for quote in overview.indices}
    monitored = [quotes[code] for code, _ in _INDICES if code in quotes]
    up = sum(q.change_pct is not None and q.change_pct > 0 for q in monitored)
    down = sum(q.change_pct is not None and q.change_pct < 0 for q in monitored)
    flat = sum(q.change_pct == 0 for q in monitored)
    unknown = sum(q.change_pct is None for q in monitored)
    missing = len(_INDICES) - len(monitored)
    amount = overview.total_amount
    if amount is None:
        amount_text = "未知"
    elif amount >= 1e8:
        amount_text = f"{amount / 1e8:.2f}亿"
    elif amount >= 1e4:
        amount_text = f"{amount / 1e4:.2f}万"
    else:
        amount_text = f"{amount:.2f}元"
    sections = [
        [
            ("A股大盘行情", 40, _INK),
            (f"响应：{timestamp(overview.fetch_time)}", 22, _MUTED),
            ("时间口径：北京时间 UTC+8；未带时区的时间单独标注", 22, _MUTED),
        ]
    ]
    for code, name in _INDICES:
        quote = quotes.get(code)
        title = f"{name}  {code}"
        if (
            quote is not None
            and quote.quote_time is not None
            and quote.quote_time.utcoffset() is not None
            and overview.fetch_time.utcoffset() is not None
            and quote.quote_time.astimezone(BEIJING_TZ).date()
            < overview.fetch_time.astimezone(BEIJING_TZ).date()
        ):
            title += "  |  历史行情"
        lines = [(title, 30, _INK)]
        if quote is None:
            lines.append(("未获取到行情", 30, _NEUTRAL))
        else:
            pct = quote.change_pct
            color = (
                _UP
                if pct is not None and pct > 0
                else (_DOWN if pct is not None and pct < 0 else _NEUTRAL)
            )
            lines.extend(
                [
                    [
                        (f"{quote.latest_price:.2f}", 36, _INK),
                        (
                            "涨跌幅未知"
                            if pct is None
                            else ("0.00% 平盘" if pct == 0 else f"{pct:+.2f}%"),
                            30,
                            color,
                        ),
                    ],
                    (
                        f"来源：{quote.source or '未知'}  |  {'缓存' if quote.from_cache else '本次获取'}  |  {age(quote.fetched_at, quote.from_cache)}",
                        22,
                        _MUTED,
                    ),
                    (f"行情时间：{timestamp(quote.quote_time)}", 22, _MUTED),
                    (f"采集时间：{timestamp(quote.fetched_at)}", 22, _MUTED),
                ]
            )
        sections.append(lines)
    sources = {q.source for q in monitored}
    quote_times = {timestamp(q.quote_time) for q in monitored}
    footer = [
        (f"沪深两市成交额：{amount_text}", 26, _INK),
        ("成交额仅汇总上证指数与深证成指，任一缺失则未知", 22, _MUTED),
        (
            f"监测 5 个指数  |  上涨 {up}  下跌 {down}  平盘 {flat}  涨跌未知 {unknown}  缺失 {missing}",
            22,
            _INK,
        ),
        ("仅展示监测指数，涨跌数量不代表全市场股票数量", 22, _MUTED),
    ]
    if len(sources) > 1 or len(quote_times) > 1:
        footer.append(("提示：来源或报价时间不一致，请逐行查看行情时间。", 22, _MUTED))
    if any(q.quote_time is None or q.quote_time.utcoffset() is None for q in monitored):
        footer.append(
            ("提示：部分报价时间或时区未知，无法判断是否为当前行情。", 22, _MUTED)
        )
    sections.append(footer)

    # Measure every line before allocating the final canvas. Long metadata is
    # character-wrapped to work for both CJK text and unbroken provider names.
    width, padding, inset = 1000, 30, 22
    text_width = width - 2 * (padding + inset)
    layouts = []
    y = padding
    for section in sections:
        top = y
        y += inset
        runs = []
        for item in section:
            if isinstance(item, list):
                widths = [
                    max(
                        fonts[size].getlength(text),
                        fonts[size].getbbox(text, anchor="lt")[2],
                    )
                    for text, size, _ in item
                ]
                if sum(widths) + 32 * (len(item) - 1) <= text_width:
                    x = padding + inset
                    for (text, size, color), run_width in zip(item, widths):
                        runs.append((x, y, text, fonts[size], color))
                        x += run_width + 32
                    y += (
                        max(
                            max(size + 5, fonts[size].getbbox(text, anchor="lt")[3] + 5)
                            for text, size, _ in item
                        )
                        + 4
                    )
                    continue
            else:
                item = [item]
            for text, size, color in item:
                font = fonts[size]
                wrapped, line = [], ""
                for character in text:
                    trial = line + character
                    bounds = font.getbbox(trial, anchor="lt")
                    if line and max(font.getlength(trial), bounds[2]) > text_width:
                        wrapped.append(line)
                        line = character
                    else:
                        line = trial
                wrapped.append(line)
                for line in wrapped:
                    runs.append((padding + inset, y, line, font, color))
                    bounds = font.getbbox(line, anchor="lt")
                    y += max(size + 5, bounds[3] + 5)
                y += 4
        y += inset - 4
        layouts.append((top, y, runs))
        y += 16
    image = Image.new("RGB", (width, y + padding - 16), (239, 244, 249))
    draw = ImageDraw.Draw(image)
    for top, bottom, runs in layouts:
        draw.rounded_rectangle(
            (padding, top, width - padding, bottom), radius=18, fill=(255, 255, 255)
        )
        for text_x, text_y, text, font, color in runs:
            draw.text((text_x, text_y), text, font=font, fill=color, anchor="lt")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
