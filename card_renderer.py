"""Render a fixed five-index overview as an in-memory PNG card."""

import warnings
from io import BytesIO
from pathlib import Path

from astrbot.api import logger

from .market_api import BEIJING_TZ, MarketOverview

_UP = (230, 24, 47)
_DOWN = (0, 155, 83)
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


def render_market_card(
    overview: MarketOverview, *, background_path: Path | None = None
) -> bytes:
    """Render quotes with explicit missing rows and provenance.

    Args:
        overview: Existing market snapshot; its response clock anchors quote age.
        background_path: Validated uploaded image path, or None for the light card.
            Invalid images are ignored without discarding the market card.

    Returns:
        PNG bytes without writing runtime files.

    Raises:
        ImportError: Pillow is unavailable.
        RuntimeError: No supported Chinese font can be loaded.
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

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

    background = None
    if background_path is not None:
        try:
            # Bound the actual read rather than trusting a prior file size check.
            with background_path.open("rb") as source:
                encoded = source.read(10 * 1024 * 1024 + 1)
            if len(encoded) > 10 * 1024 * 1024:
                raise ValueError("Background exceeds the 10 MiB limit")
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(encoded)) as source:
                    if source.format not in {"JPEG", "PNG", "WEBP"}:
                        raise ValueError("Unsupported background image format")
                    if source.width * source.height > 20_000_000:
                        raise ValueError(
                            "Background exceeds the 20 million pixel limit"
                        )
                    if getattr(source, "n_frames", 1) != 1:
                        raise ValueError("Animated backgrounds are unsupported")
                    background = ImageOps.exif_transpose(source).convert("RGBA")
        except Exception as error:
            logger.warning(
                "Card background unavailable; using the default background (%s)",
                type(error).__name__,
            )

    # Measure every line before allocating the final canvas. Long metadata is
    # character-wrapped to work for both CJK text and unbroken provider names.
    width = 1000
    padding, inset, gap = (40, 24, 22) if background is not None else (30, 22, 16)
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
        y += gap
    image = Image.new("RGB", (width, y + padding - gap + 50), (239, 244, 249))
    if background is not None:
        background = ImageOps.fit(
            background, image.size, method=Image.Resampling.LANCZOS
        )
        image = Image.alpha_composite(image.convert("RGBA"), background)
        frosted = image.filter(ImageFilter.GaussianBlur(12))
        shadow = Image.new("RGBA", image.size)
        shadow_draw = ImageDraw.Draw(shadow)
        for top, bottom, _ in layouts:
            shadow_draw.rounded_rectangle(
                (padding + 2, top + 8, width - padding + 2, bottom + 8),
                radius=26,
                fill=(9, 19, 38, 70),
            )
        image = Image.alpha_composite(
            image, shadow.filter(ImageFilter.GaussianBlur(12))
        )
    draw = ImageDraw.Draw(image)
    for top, bottom, runs in layouts:
        if background is None:
            draw.rounded_rectangle(
                (padding, top, width - padding, bottom), radius=18, fill=(255, 255, 255)
            )
        else:
            panel_width, panel_height = width - 2 * padding, bottom - top
            # Magnify the blurred backdrop slightly to suggest glass refraction.
            panel = frosted.crop((padding, top, width - padding, bottom))
            enlarged = panel.resize(
                (int(panel_width * 1.035), int(panel_height * 1.035)),
                Image.Resampling.BICUBIC,
            )
            offset_x = (enlarged.width - panel_width) // 2
            offset_y = (enlarged.height - panel_height) // 2
            panel = enlarged.crop(
                (offset_x, offset_y, offset_x + panel_width, offset_y + panel_height)
            )
            sheen = Image.new("RGBA", panel.size)
            sheen_draw = ImageDraw.Draw(sheen)
            for row in range(panel_height):
                position = row / max(panel_height - 1, 1)
                alpha = int(170 - 35 * min(position * 3, 1) + 12 * position)
                sheen_draw.line((0, row, panel_width, row), fill=(255, 255, 255, alpha))
            panel = Image.alpha_composite(panel, sheen)
            rim = ImageDraw.Draw(panel)
            rim.rounded_rectangle(
                (0, 0, panel_width - 1, panel_height - 1),
                radius=24,
                outline=(255, 255, 255, 220),
                width=2,
            )
            rim.arc((2, 2, 50, 50), 180, 270, fill=(255, 255, 255, 255), width=2)
            mask = Image.new("L", panel.size)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, panel_width - 1, panel_height - 1), radius=24, fill=255
            )
            image.paste(panel, (padding, top), mask)
        for text_x, text_y, text, font, color in runs:
            if background is not None:
                color = {
                    _INK: (18, 29, 43),
                    _NEUTRAL: (35, 45, 58),
                    _MUTED: (27, 38, 51),
                }.get(color, color)
            draw.text((text_x, text_y), text, font=font, fill=color, anchor="lt")

    # Reserve a separate footer so the repository credit never covers market data.
    repository = "astrbot_plugin_astock_market"
    credit_font = fonts[22]
    credit_width = int(credit_font.getlength(repository) + 0.999)
    with Image.open(Path(__file__).parent / "assets" / "github-mark.png") as source:
        icon = source.convert("RGBA")
    icon.thumbnail((26, 26), Image.Resampling.LANCZOS)
    badge_width = credit_width + icon.width + 10 + 28
    badge_left = width - padding - badge_width
    badge_top = image.height - 52
    credit = Image.new("RGBA", image.size)
    credit_draw = ImageDraw.Draw(credit)
    credit_draw.rounded_rectangle(
        (badge_left, badge_top, width - padding, image.height - 14),
        radius=14,
        fill=(255, 255, 255, 235),
    )
    credit.paste(icon, (badge_left + 14, badge_top + (38 - icon.height) // 2), icon)
    credit_draw.text(
        (badge_left + 14 + icon.width + 10, badge_top + 8),
        repository,
        font=credit_font,
        fill=_INK,
        anchor="lt",
    )
    image = Image.alpha_composite(image.convert("RGBA"), credit).convert("RGB")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
