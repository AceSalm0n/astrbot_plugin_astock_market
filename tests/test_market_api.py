"""Behavior checks for source validation, shared work, and market formatting."""

import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


def load_market_module():
    """Load an isolated module without requiring the AstrBot runtime."""
    name = f"market_under_test_{len(sys.modules)}"
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger(name)
    api.logger.addHandler(logging.NullHandler())
    api.logger.propagate = False
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / "market_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with patch.dict(sys.modules, {"astrbot.api": api}):
        spec.loader.exec_module(module)
    return module


def eastmoney(code="000001", **updates):
    """Return a complete fixed Eastmoney quote with prices scaled by 100."""
    data = {
        "f43": 310000,
        "f44": 315000,
        "f45": 298000,
        "f46": 299000,
        "f47": 1000,
        "f48": 100000000,
        "f57": code,
        "f58": "测试指数",
        "f60": 300000,
        "f169": 10000,
        "f170": 333,
    }
    data.update(updates)
    return data


def tencent(code="000001", price="3100", pre_close="3000"):
    parts = [""] * 50
    for position, value in {
        0: "1",
        1: "测试指数",
        2: code,
        3: price,
        4: pre_close,
        5: "2990",
        6: "1000",
        30: "20261009150000",
        31: "100",
        32: "3.33",
        33: "3150",
        34: "2980",
        37: "10000",
    }.items():
        parts[position] = value
    prefix = "sh" if code.startswith("0") else "sz"
    return f'v_{prefix}{code}="' + "~".join(parts) + '";'


def sina(code="000001", price="3100", pre_close="3000"):
    parts = [""] * 33
    for position, value in {
        0: "测试指数",
        1: "2990",
        2: pre_close,
        3: price,
        4: "3150",
        5: "2980",
        8: "1000",
        9: "100000000",
        30: "2026-10-09",
        31: "15:00:00",
        32: "00",
    }.items():
        parts[position] = value
    prefix = "sh" if code.startswith("0") else "sz"
    return f'var hq_str_{prefix}{code}="' + ",".join(parts) + '";'


class Response:
    def __init__(self, text, status=200, gate=None):
        self.body = text
        self.status = status
        self.gate = gate

    async def __aenter__(self):
        if self.gate is not None:
            await self.gate.wait()
        return self

    async def __aexit__(self, *_):
        return False

    async def text(self, **_):
        return self.body


class Session:
    """Fake only HTTP I/O; keep production parsing and orchestration real."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responder(url)

    async def close(self):
        self.closed = True


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.market = load_market_module()

    def test_missing_and_nonfinite_numbers_remain_unknown(self):
        for value in (None, "", "-", "NaN", "inf", float("-inf")):
            with self.subTest(value=value):
                self.assertIsNone(self.market._to_float(value))
        self.assertEqual(self.market._to_float("0"), 0)

    def test_rejects_nonpositive_and_nonfinite_latest_price(self):
        for price in (0, -100, None, "NaN", "inf"):
            with self.subTest(price=price):
                self.assertIsNone(self.market._parse_eastmoney(eastmoney(f43=price)))

    def test_missing_change_is_derived_only_with_valid_previous_close(self):
        data = self.market._parse_eastmoney(
            eastmoney(f169="-", f170="-", f48=None, f44=None)
        )
        self.assertEqual(data.change, 100)
        self.assertEqual(data.change_pct, 3.33)
        self.assertIsNone(data.amount)
        self.assertIsNone(data.high_price)
        unknown = self.market._parse_eastmoney(
            eastmoney(f169=None, f170=None, f60=None)
        )
        self.assertIsNone(unknown.change_pct)
        self.assertIsNone(unknown.change)
        flat = self.market._parse_eastmoney(eastmoney(f170=0))
        self.assertEqual(flat.change_pct, 0)

    def test_invalid_optional_prices_and_negative_totals_remain_unknown(self):
        for value in (-100, 0):
            with self.subTest(value=value):
                data = self.market._parse_eastmoney(
                    eastmoney(
                        f44=value,
                        f45=value,
                        f46=value,
                        f60=value,
                        f47=value,
                        f48=value,
                        f169=-100,
                        f170=-1,
                    )
                )
                self.assertEqual(data.latest_price, 3100)
                self.assertEqual(data.change, -1)
                self.assertEqual(data.change_pct, -0.01)
                for field in ("open_price", "high_price", "low_price", "pre_close"):
                    self.assertIsNone(getattr(data, field))
                self.assertEqual(data.volume, 0 if value == 0 else None)
                self.assertEqual(data.amount, 0 if value == 0 else None)
                second = self.market._parse_eastmoney(eastmoney("399001", f48=0))
                overview = self.market.MarketOverview(
                    [data, second], datetime.now(self.market.BEIJING_TZ)
                )
                self.assertEqual(overview.total_amount, 0 if value == 0 else None)

    def test_unknown_change_and_incomplete_amount_are_exposed_in_text(self):
        data = self.market._parse_eastmoney(
            eastmoney(f169=None, f170=None, f60=None, f48=None)
        )
        overview = self.market.MarketOverview(
            [data], datetime(2026, 10, 10, 9, tzinfo=timezone(timedelta(hours=8)))
        )
        self.assertEqual(overview.unknown_count, 1)
        self.assertEqual(overview.flat_count, 0)
        text = self.market.format_market_overview_text(overview)
        self.assertIn("涨幅：未知", text)
        self.assertIn("合计成交额：未知", text)
        self.assertIn("缺失指数", text)
        self.assertIn("行情时间：未知", text)
        self.assertIn("采集时间：未知", text)
        self.assertNotIn(
            "缺失指数", self.market.format_market_overview_text(overview, False)
        )

    def test_amount_total_requires_both_major_indices_and_excludes_subindices(self):
        first = self.market._parse_eastmoney(eastmoney(f48=100000000))
        second = self.market._parse_eastmoney(eastmoney("399001", f48=200000000))
        sub = self.market._parse_eastmoney(eastmoney("399006", f48=900000000))
        now = datetime.now(timezone(timedelta(hours=8)))
        complete = self.market.MarketOverview([first, second, sub], now)
        self.assertEqual(complete.total_amount_text, "3亿")
        self.assertIn(
            "合计成交额：3.00 亿元", self.market.format_market_overview_text(complete)
        )
        self.assertEqual(
            self.market.MarketOverview([first, sub], now).total_amount_text, "未知"
        )
        second.amount = None
        self.assertEqual(complete.total_amount_text, "未知")


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1
        self.market = load_market_module()

    async def asyncTearDown(self):
        await self.market.close_session()

    def install_http(self, responder):
        session = Session(responder)
        self.market._session = session
        return session

    def json_response(self, data):
        import json

        return Response(json.dumps({"data": data}))

    async def test_invalid_or_mismatched_eastmoney_falls_back_to_tencent(self):
        for raw in (eastmoney(f43="NaN"), eastmoney("399001")):
            with self.subTest(raw=raw):
                module = load_market_module()
                session = Session(
                    lambda url: (
                        self.json_response(raw)
                        if "eastmoney" in url
                        else Response(tencent())
                    )
                )
                module._session = session
                result = await module.fetch_single_index("000001")
                self.assertEqual(result.code, "000001")
                self.assertEqual(result.source, "腾讯财经")
                self.assertEqual(result.latest_price, 3100)
                self.assertEqual(len(session.requests), 2)
                await module.close_session()

    async def test_mismatched_tencent_and_nan_price_fall_back_to_sina(self):
        for quote in (tencent("399001"), tencent(price="NaN")):
            with self.subTest(quote=quote):
                module = load_market_module()
                session = Session(
                    lambda url: (
                        Response("{}")
                        if "eastmoney" in url
                        else Response(quote if "gtimg" in url else sina())
                    )
                )
                module._session = session
                result = await module.fetch_single_index("000001")
                self.assertEqual(result.source, "新浪财经")
                self.assertEqual(result.latest_price, 3100)
                self.assertEqual(result.amount, 100000000)
                self.assertEqual(len(session.requests), 3)
                await module.close_session()

    async def test_cache_preserves_collection_time_and_returns_independent_copy(self):
        session = self.install_http(lambda _: self.json_response(eastmoney()))
        first = await self.market.fetch_single_index("000001")
        second = await self.market.fetch_single_index("1.000001")
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(first.fetched_at, second.fetched_at)
        self.assertEqual(first.fetched_at.utcoffset(), timedelta(hours=8))
        self.assertIsNone(first.quote_time)
        cached_text = self.market.format_market_overview_text(
            self.market.MarketOverview(
                [second], first.fetched_at + timedelta(seconds=8), second.source
            ),
            False,
        )
        self.assertIn("缓存年龄：8 秒", cached_text)
        self.assertEqual(first.fetched_at, second.fetched_at)
        second.latest_price = 1
        third = await self.market.fetch_single_index("000001")
        self.assertEqual(third.latest_price, 3100)
        self.assertEqual(len(session.requests), 1)

    async def test_force_refresh_bypasses_cache_and_old_cache_is_never_failure_fallback(
        self,
    ):
        session = self.install_http(lambda _: self.json_response(eastmoney()))
        with patch.object(self.market, "monotonic", return_value=100):
            await self.market.fetch_single_index("000001")
            session.responder = lambda _: Response("{}")
            result = await self.market.fetch_single_index("000001", force_refresh=True)
            self.assertIsNone(result)
            self.assertIsNone(await self.market.fetch_single_index("000001"))
        self.assertEqual(len(session.requests), 4)

    async def test_success_and_failure_cache_expire_on_monotonic_clock(self):
        session = self.install_http(lambda _: self.json_response(eastmoney()))
        with patch.object(self.market, "monotonic", return_value=100):
            await self.market.fetch_single_index("000001")
        with patch.object(self.market, "monotonic", return_value=109.9):
            self.assertTrue((await self.market.fetch_single_index("000001")).from_cache)
        with patch.object(self.market, "monotonic", return_value=110):
            session.responder = lambda _: Response("{}")
            self.assertIsNone(await self.market.fetch_single_index("000001"))
        with patch.object(self.market, "monotonic", return_value=112.9):
            self.assertIsNone(await self.market.fetch_single_index("000001"))
        self.assertEqual(len(session.requests), 4)
        with patch.object(self.market, "monotonic", return_value=113):
            session.responder = lambda _: self.json_response(eastmoney())
            self.assertEqual(
                (await self.market.fetch_single_index("000001")).latest_price, 3100
            )
        self.assertEqual(len(session.requests), 5)

    async def test_same_code_shares_worker_even_for_force_refresh_and_caller_cancellation(
        self,
    ):
        gate = asyncio.Event()
        response = self.json_response(eastmoney())
        response.gate = gate
        session = self.install_http(lambda _: response)
        first = asyncio.create_task(self.market.fetch_single_index("000001"))
        for _ in range(4):
            await asyncio.sleep(0)
        other = asyncio.create_task(self.market.fetch_single_index("000001", True))
        for _ in range(4):
            await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        gate.set()
        self.assertEqual((await other).latest_price, 3100)
        self.assertEqual(len(session.requests), 1)

    async def test_overview_starts_all_five_indices_and_retains_partial_data(self):
        gate = asyncio.Event()
        started = set()

        def responder(url):
            if "eastmoney" not in url:
                return Response("")
            code = url.split("secid=")[1].split("&")[0].split(".")[1]
            started.add(code)
            response = self.json_response(eastmoney(code) if code == "000001" else None)
            response.gate = gate
            if len(started) == 5:
                gate.set()
            return response

        self.install_http(responder)
        overview = await asyncio.wait_for(self.market.fetch_market_overview(), 0.5)
        self.assertEqual([data.code for data in overview.indices], ["000001"])
        self.assertEqual(overview.source, "东方财富")
        self.assertEqual(overview.fetch_time.utcoffset(), timedelta(hours=8))
        self.assertLessEqual(overview.indices[0].fetched_at, overview.fetch_time)
        cached = await self.market.fetch_market_overview()
        self.assertTrue(cached.indices[0].from_cache)
        self.assertEqual(cached.indices[0].fetched_at, overview.indices[0].fetched_at)

    async def test_shutdown_cancels_workers_and_prevents_reopening_until_reset(self):
        gate = asyncio.Event()
        response = self.json_response(eastmoney())
        response.gate = gate
        session = self.install_http(lambda _: response)
        caller = asyncio.create_task(self.market.fetch_single_index("000001"))
        for _ in range(4):
            await asyncio.sleep(0)
        await self.market.close_session()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 0.1)
        self.assertTrue(session.closed)
        self.assertIsNone(await self.market.fetch_single_index("000001"))
        with self.assertRaises(RuntimeError):
            await self.market._get_session()
        self.market.reset_market_state()
        self.install_http(lambda _: self.json_response(eastmoney()))
        self.assertFalse((await self.market.fetch_single_index("000001")).from_cache)

    async def test_unknown_codes_do_not_create_requests(self):
        session = self.install_http(lambda _: Response("{}"))
        self.assertIsNone(await self.market.fetch_single_index("999999"))
        self.assertEqual(session.requests, [])

    async def test_backup_optional_numeric_validation_preserves_valid_quote(self):
        for source, body, separator, price_fields, total_fields in (
            ("tencent", tencent(), "~", (4, 5, 33, 34), (6, 37)),
            ("sina", sina(), ",", (1, 2, 4, 5), (8, 9)),
        ):
            for value in ("-1", "0"):
                with self.subTest(source=source, value=value):
                    parts = body.split('"')[1].split(separator)
                    for position in (*price_fields, *total_fields):
                        parts[position] = value
                    if source == "tencent":
                        parts[31], parts[32] = "-1", "-0.01"
                    quote = body.split('"')[0] + '"' + separator.join(parts) + '";'
                    self.install_http(lambda _: Response(quote))
                    data = await getattr(self.market, f"_fetch_from_{source}")("000001")
                    self.assertEqual(data.latest_price, 3100)
                    for field in ("open_price", "high_price", "low_price", "pre_close"):
                        self.assertIsNone(getattr(data, field))
                    self.assertEqual(data.volume, 0 if value == "0" else None)
                    self.assertEqual(data.amount, 0 if value == "0" else None)
                    if source == "tencent":
                        self.assertEqual(data.change, -1)
                        self.assertEqual(data.change_pct, -0.01)

    async def test_concurrent_shutdown_shares_cleanup_and_survives_caller_cancellation(
        self,
    ):
        close_started = asyncio.Event()
        release_close = asyncio.Event()

        class BlockingSession(Session):
            def __init__(self):
                super().__init__(lambda _: Response("{}"))
                self.close_count = 0

            async def close(self):
                self.close_count += 1
                close_started.set()
                await release_close.wait()
                self.closed = True

        session = BlockingSession()
        self.market._session = session
        first = asyncio.create_task(self.market.close_session())
        await close_started.wait()
        second = asyncio.create_task(self.market.close_session())
        for _ in range(4):
            await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        try:
            with self.assertRaises(RuntimeError):
                self.market.reset_market_state()
        finally:
            release_close.set()
            await second
        self.assertEqual(session.close_count, 1)
        self.assertTrue(session.closed)
        self.market.reset_market_state()
        new_session = await self.market._get_session()
        self.assertFalse(new_session.closed)
        self.assertIs(self.market._session, new_session)

    async def test_request_error_falls_back_without_losing_cancellation(self):
        def responder(url):
            if "eastmoney" in url:
                raise TimeoutError("fixed source timeout")
            return Response(tencent())

        self.install_http(responder)
        result = await self.market.fetch_single_index("000001")
        self.assertEqual(result.source, "腾讯财经")
        self.assertEqual(result.latest_price, 3100)

    async def test_worker_deadline_returns_failure_and_does_not_leave_active_work(self):
        gate = asyncio.Event()
        response = self.json_response(eastmoney())
        response.gate = gate
        self.install_http(lambda _: response)
        original_wait_for = asyncio.wait_for

        async def short_deadline(awaitable, timeout):
            self.assertEqual(timeout, 18)
            return await original_wait_for(awaitable, 0.01)

        local_asyncio = types.SimpleNamespace(**vars(asyncio))
        local_asyncio.wait_for = short_deadline
        with patch.object(self.market, "asyncio", local_asyncio):
            self.assertIsNone(await self.market.fetch_single_index("000001"))
        gate.set()
        self.install_http(lambda _: self.json_response(eastmoney()))
        result = await self.market.fetch_single_index("000001", True)
        self.assertEqual(result.latest_price, 3100)

    async def test_reset_does_not_discard_active_worker(self):
        gate = asyncio.Event()
        response = self.json_response(eastmoney())
        response.gate = gate
        session = self.install_http(lambda _: response)
        first = asyncio.create_task(self.market.fetch_single_index("000001"))
        for _ in range(4):
            await asyncio.sleep(0)
        self.market.reset_market_state()
        second = asyncio.create_task(self.market.fetch_single_index("000001", True))
        for _ in range(4):
            await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(first, second)
        self.assertEqual([data.latest_price for data in results], [3100, 3100])
        self.assertEqual(len(session.requests), 1)

    async def test_request_timeout_is_configured_on_real_session(self):
        session = await self.market._get_session()
        self.assertEqual(session.timeout.total, 5)

    async def test_invalid_quote_time_is_unknown_without_discarding_valid_price(self):
        self.install_http(lambda _: Response(sina().replace("2026-10-09", "invalid")))
        data = await self.market._fetch_from_sina("000001")
        self.assertEqual(data.latest_price, 3100)
        self.assertIsNone(data.quote_time)

    async def test_verified_tencent_and_sina_quote_times_use_beijing_timezone(self):
        for source, body, expected in (
            (
                "tencent",
                tencent(),
                datetime(2026, 10, 9, 15, tzinfo=timezone(timedelta(hours=8))),
            ),
            (
                "sina",
                sina(),
                datetime(2026, 10, 9, 15, tzinfo=timezone(timedelta(hours=8))),
            ),
        ):
            with self.subTest(source=source):
                self.install_http(lambda _: Response(body))
                fetcher = getattr(self.market, f"_fetch_from_{source}")
                data = await fetcher("000001")
                self.assertEqual(data.quote_time, expected)

    async def test_scaling_overflow_remains_unknown_and_does_not_break_formatting(self):
        quote = tencent(pre_close="1e-308")
        parts = quote.split('"')[1].split("~")
        parts[31] = ""
        parts[32] = ""
        parts[37] = "1e308"
        self.install_http(lambda _: Response('v_sh000001="' + "~".join(parts) + '";'))
        result = await self.market._fetch_from_tencent("000001")
        self.assertIsNone(result.amount)
        self.assertIsNone(result.change_pct)
        self.assertNotIn(
            "inf",
            self.market.format_market_overview_text(
                self.market.MarketOverview(
                    [result], datetime.now(self.market.BEIJING_TZ)
                ),
                False,
            ),
        )


if __name__ == "__main__":
    unittest.main()
