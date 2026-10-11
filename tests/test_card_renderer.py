"""Fixed quote checks for the local PNG renderer."""

import builtins
import importlib.util
import io
import logging
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont


class CardRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        package = types.ModuleType("card_fixture_plugin")
        package.__path__ = [str(root)]
        api = types.ModuleType("astrbot.api")
        api.logger = logging.getLogger("card_fixture")
        with patch.dict(sys.modules, {package.__name__: package, "astrbot.api": api}):
            modules = []
            for filename in ("market_api", "card_renderer"):
                name = f"{package.__name__}.{filename}"
                spec = importlib.util.spec_from_file_location(
                    name, root / f"{filename}.py"
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                modules.append(module)
            cls.market, cls.renderer = modules

    def setUp(self):
        self.now = datetime(2026, 10, 9, 15, 1, tzinfo=timezone(timedelta(hours=8)))

    def quote(self, code, pct, **updates):
        data = {
            "code": code,
            "name": "测试指数",
            "latest_price": 3100.126,
            "change": 1.0,
            "change_pct": pct,
            "open_price": None,
            "high_price": None,
            "low_price": None,
            "pre_close": None,
            "volume": None,
            "amount": 1e8,
            "source": "腾讯",
            "quote_time": self.now - timedelta(minutes=1),
            "fetched_at": self.now - timedelta(seconds=30),
            "from_cache": False,
        }
        data.update(updates)
        return self.market.IndexData(**data)

    def render(self, quotes):
        calls = []
        original = ImageDraw.ImageDraw.text

        def record(draw, xy, text, *args, **kwargs):
            calls.append((xy, text, kwargs))
            return original(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record):
            png = self.renderer.render_market_card(
                self.market.MarketOverview(quotes, self.now, "腾讯")
            )
        return png, calls, "\n".join(call[1] for call in calls)

    def test_png_fixed_rows_counts_colors_and_amount(self):
        png, calls, text = self.render(
            [
                self.quote("000001", 1.23),
                self.quote("399001", -2.34),
                self.quote("399006", 0),
                self.quote("000688", None),
                self.quote("999999", 9),
            ]
        )
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.width, 1000)
        for code in ("000001", "399001", "399006", "000688", "000300"):
            self.assertIn(code, text)
        self.assertNotIn("999999", text)
        self.assertIn("上涨 1", text)
        self.assertIn("下跌 1", text)
        self.assertIn("平盘 1", text)
        self.assertIn("未知 1", text)
        self.assertIn("缺失 1", text)
        self.assertIn("监测 5 个指数", text)
        self.assertIn("3100.13", text)
        self.assertIn("+1.23%", text)
        self.assertIn("-2.34%", text)
        self.assertIn("0.00%", text)
        self.assertIn("沪深两市成交额：2.00亿", text)
        fills = {line: options["fill"] for _, line, options in calls}
        self.assertEqual(fills["+1.23%"], self.renderer._UP)
        self.assertEqual(fills["-2.34%"], self.renderer._DOWN)
        self.assertEqual(fills["0.00% 平盘"], self.renderer._NEUTRAL)
        self.assertGreater(
            sum(
                n
                for n, color in image.getcolors(image.width * image.height)
                if color == self.renderer._UP
            ),
            10,
        )

    def test_partial_amount_and_explicit_missing_rows(self):
        _, _, text = self.render([self.quote("000001", None)])
        self.assertEqual(text.count("未获取到行情"), 4)
        self.assertIn("沪深两市成交额：未知", text)
        self.assertIn("未知 1", text)
        self.assertIn("缺失 4", text)

    def test_cached_quote_uses_response_clock(self):
        _, _, text = self.render([self.quote("000001", 1, from_cache=True)])
        self.assertIn("2026-10-09 15:01:00", text)
        self.assertIn("行情时间：2026-10-09 15:00:00", text)
        self.assertIn("采集时间：2026-10-09 15:00:30", text)
        self.assertIn("缓存", text)
        self.assertIn("缓存年龄 30 秒", text)
        self.assertIn("北京时间 UTC+8", text)

    def test_unknown_time_and_naive_time_are_not_claimed_beijing(self):
        _, _, text = self.render(
            [
                self.quote(
                    "000001", 1, quote_time=None, fetched_at=None, from_cache=True
                ),
                self.quote("399001", 0, quote_time=self.now.replace(tzinfo=None)),
            ]
        )
        self.assertIn("行情时间：未知", text)
        self.assertIn("采集时间：未知", text)
        self.assertIn("缓存年龄未知", text)
        self.assertIn("时区未知", text)

    def test_utc_quote_converts_and_future_timestamp_is_not_zero_age(self):
        _, _, text = self.render(
            [
                self.quote(
                    "000001",
                    1,
                    quote_time=datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc),
                    fetched_at=self.now + timedelta(seconds=20),
                ),
            ]
        )
        self.assertIn("行情时间：2026-10-09 15:00:00", text)
        self.assertIn("采集晚于响应 20 秒", text)

    def test_previous_date_history_is_labeled_per_row(self):
        _, calls, text = self.render(
            [
                self.quote("000001", 1, quote_time=self.now - timedelta(days=1)),
                self.quote("399001", 0),
            ]
        )
        titles = [line for _, line, _ in calls if "指数" in line or "成指" in line]
        self.assertTrue(any("000001" in line and "历史行情" in line for line in titles))
        self.assertTrue(
            any("399001" in line and "历史行情" not in line for line in titles)
        )
        self.assertIn("2026-10-08", text)
        self.assertNotIn("实时", text)

    def test_amount_keeps_fractional_unit_precision(self):
        for amount, expected in [
            (123456789, "2.47亿"),
            (12345, "2.47万"),
            (123.45, "246.90元"),
        ]:
            with self.subTest(amount=amount):
                _, _, text = self.render(
                    [
                        self.quote("000001", 1, amount=amount),
                        self.quote("399001", 0, amount=amount),
                    ]
                )
                self.assertIn(f"沪深两市成交额：{expected}", text)

    def test_price_and_change_share_horizontal_line_and_compact_height(self):
        png, calls, _ = self.render(
            [self.quote(code, 1) for code, _ in self.renderer._INDICES]
        )
        prices = [xy for xy, line, _ in calls if line == "3100.13"]
        changes = [xy for xy, line, _ in calls if line == "+1.00%"]
        self.assertEqual(len(prices), 5)
        for price, change in zip(prices, changes):
            self.assertEqual(price[1], change[1])
            self.assertGreater(change[0], price[0])
        self.assertLess(Image.open(io.BytesIO(png)).height, 1700)

    def test_same_source_same_quote_time_has_no_mixing_warning(self):
        _, _, text = self.render(
            [
                self.quote("000001", 1),
                self.quote("399001", -1),
            ]
        )
        self.assertNotIn("来源或报价时间不一致", text)

    def test_mixed_sources_and_times_warning(self):
        _, _, text = self.render(
            [
                self.quote("000001", 1),
                self.quote(
                    "399001",
                    1,
                    source="新浪",
                    quote_time=self.now - timedelta(minutes=2),
                ),
            ]
        )
        self.assertIn("来源或报价时间不一致", text)

    def test_long_metadata_wraps_without_clipping(self):
        _, short_calls, _ = self.render([self.quote("000001", 1)])
        png, calls, text = self.render(
            [self.quote("000001", 1, source="长来源" * 150, name="长名称" * 100)]
        )
        image = Image.open(io.BytesIO(png))
        self.assertGreater(len(calls), len(short_calls))
        self.assertEqual(text.replace("\n", "").count("长来源"), 150)
        for (x, y), line, options in calls:
            bounds = options["font"].getbbox(line, anchor="lt")
            self.assertLessEqual(x + bounds[2], image.width - 30)
            self.assertLessEqual(y + bounds[3], image.height - 20)

    def test_missing_font_raises_instead_of_default_font(self):
        with patch.object(ImageFont, "truetype", side_effect=OSError("missing")):
            with self.assertRaisesRegex(RuntimeError, "Chinese font"):
                self.render([])

    def test_missing_pillow_raises_to_caller(self):
        original = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == "PIL":
                raise ModuleNotFoundError("Pillow missing")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=unavailable):
            with self.assertRaises(ModuleNotFoundError):
                self.renderer.render_market_card(
                    self.market.MarketOverview([], self.now)
                )


if __name__ == "__main__":
    unittest.main()
