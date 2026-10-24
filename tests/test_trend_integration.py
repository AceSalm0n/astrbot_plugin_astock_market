"""Exercise trend selection and lifecycle with an isolated minimal SDK."""

import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "trend_integration_plugin"
TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 23, 16, tzinfo=TZ)


def load_plugin():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("trend-integration")
    api.AstrBotConfig = dict
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.MessageChain = object
    event.filter = types.SimpleNamespace(command=lambda name: lambda fn: fn)
    star = types.ModuleType("astrbot.api.star")
    star.Context = star.Star = star.StarTools = object
    star.register = lambda *args: lambda cls: cls
    stubs = {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
             "astrbot.api.event": event, "astrbot.api.star": star, PACKAGE: package}
    with patch.dict(sys.modules, stubs):
        for filename in ("market_api", "market_history", "trend_analysis", "main"):
            spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{filename}", ROOT / f"{filename}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            setattr(package, filename, module)
    return package


package = load_plugin()
main, market, history, trend = package.main, package.market_api, package.market_history, package.trend_analysis
CALENDAR = trend.load_trading_calendar(ROOT / "assets/trading_calendar.json")
DATES = CALENDAR.session_dates(date(2026, 8, 1), NOW.date())[-21:]


class Clock:
    value = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.value


def overview(stamp=NOW):
    index = market.IndexData("000001", "上证指数", 120, 1, 1, 119, 121, 118, 119, 1, 100)
    index.quote_time, index.fetched_at, index.source = stamp, NOW, "fixture"
    return market.MarketOverview([index], NOW, "fixture")


def batch(codes=("000001", "399001", "000300")):
    histories = {}
    for code in codes:
        bars = tuple(history.DailyBar(day, 100 + n, 100 + n, 101 + n, 99 + n, 1, None)
                     for n, day in enumerate(DATES))
        histories[code] = history.IndexHistory(code, "fixture", bars, NOW, False, None)
    return history.HistoryBatch(NOW.date(), histories, {})


def plugin(config=None):
    instance = main.AStockMarketPlugin.__new__(main.AStockMarketPlugin)
    instance.config = {"enable_trend_analysis": True, "llm_provider_id": "selected", **(config or {})}
    instance.context = types.SimpleNamespace(llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="有依据的解读")), get_all_providers=lambda: [])
    instance._trend_calendar = CALENDAR
    instance._history_client = types.SimpleNamespace(fetch=AsyncMock(return_value=batch()), close=AsyncMock())
    instance._history_state = "尚未查询"
    instance._terminated = False
    instance._startup_task = instance._push_task = None
    instance._group_umo_map = {}
    return instance


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Clock.value = NOW
        self.clock = patch.object(main, "datetime", Clock)
        self.clock.start()

    async def asyncTearDown(self):
        self.clock.stop()

    async def test_enabled_analysis_contains_history_dates_and_full_snapshot(self):
        instance = plugin()
        text = await instance._generate_llm_analysis(overview(), include_market=False)
        prompt = instance.context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("MA20", prompt)
        self.assertIn("120.00", prompt)
        self.assertIn("2026-10-28", text)
        self.assertIn("趋势基准", text)
        self.assertIn("有依据的解读", text)
        self.assertEqual(instance.context.llm_generate.await_count, 1)

    async def test_disabled_flag_preserves_snapshot_and_never_fetches_history(self):
        instance = plugin({"enable_trend_analysis": False})
        text = await instance._generate_llm_analysis(overview())
        self.assertIn("指数解读", text)
        instance._history_client.fetch.assert_not_awaited()
        self.assertNotIn("MA20", instance.context.llm_generate.await_args.kwargs["prompt"])

    async def test_weekend_uses_friday_history_without_old_snapshot_rejection(self):
        Clock.value = datetime(2026, 10, 24, 12, tzinfo=TZ)
        instance = plugin()
        text = await instance._generate_llm_analysis(overview())
        self.assertIn("趋势基准", text)
        self.assertNotIn("已跳过当日分析", text)

    async def test_total_failure_falls_back_once_and_does_not_claim_trend(self):
        instance = plugin()
        instance._history_client.fetch.return_value = history.HistoryBatch(NOW.date(), {}, {"000001": "更新中"})
        text = await instance._generate_llm_analysis(overview())
        self.assertIn("趋势不可用", text)
        self.assertIn("指数解读", text)
        self.assertNotIn("🤖 趋势解读", text)
        self.assertEqual(instance.context.llm_generate.await_count, 1)

    async def test_failed_history_and_historical_snapshot_skip_model(self):
        instance = plugin()
        instance._history_client.fetch.return_value = history.HistoryBatch(NOW.date(), {}, {})
        text = await instance._generate_llm_analysis(overview(NOW - timedelta(days=1)))
        self.assertIn("已跳过当日分析", text)
        instance.context.llm_generate.assert_not_awaited()

    async def test_core_missing_has_fixed_insufficient_evidence_header(self):
        instance = plugin()
        instance._history_client.fetch.return_value = batch(("000001",))
        text = await instance._generate_llm_analysis(overview())
        self.assertIn("整体证据不足", text)

    async def test_partial_history_failure_is_visible_even_when_llm_is_blank(self):
        instance = plugin()
        good = batch(("000001", "399001", "399006", "000300"))
        instance._history_client.fetch.return_value = history.HistoryBatch(NOW.date(), good.histories, {"000688": "历史源超时"})
        instance.context.llm_generate.return_value.completion_text = " "
        text = await instance._generate_llm_analysis(overview(), include_market=False)
        self.assertIn("科创50", text)
        self.assertIn("历史源超时", text)

    async def test_calendar_conflict_never_generates_trend(self):
        instance = plugin({"market_closed_dates": ["2026-10-28"]})
        text = await instance._generate_llm_analysis(overview())
        self.assertIn("临时休市需核实", text)
        self.assertNotIn("🤖 趋势解读", text)

    async def test_unknown_query_year_does_not_create_history_requests(self):
        Clock.value = datetime(2027, 1, 4, 16, tzinfo=TZ)
        instance = plugin()
        text = await instance._generate_llm_analysis(overview())
        self.assertIn("趋势不可用", text)
        instance._history_client.fetch.assert_not_awaited()

    async def test_unknown_snapshot_time_is_disclosed_in_trend_prompt(self):
        instance = plugin()
        await instance._generate_llm_analysis(overview(None))
        self.assertIn("快照时间未知", instance.context.llm_generate.await_args.kwargs["prompt"])

    async def test_image_mode_and_render_fallback_share_one_interpretation(self):
        for rendering in (b"png", RuntimeError("font")):
            instance = plugin({"enable_image_output": True})
            with patch.object(main, "render_market_card", Mock(side_effect=rendering) if isinstance(rendering, Exception) else Mock(return_value=rendering)):
                image, text, fallback = await instance._prepare_market_output(overview(), analyze=True)
            self.assertIn("有依据的解读", text)
            self.assertIn("120.00", fallback)
            self.assertEqual(instance.context.llm_generate.await_count, 1)
            self.assertEqual(instance._history_client.fetch.await_count, 1)

    async def test_non_analysis_payload_never_fetches_history(self):
        instance = plugin()
        await instance._prepare_market_output(overview(), analyze=False)
        instance._history_client.fetch.assert_not_awaited()

    async def test_trend_can_finish_beyond_snapshot_budget(self):
        async def slow_response(**kwargs):
            await asyncio.sleep(.06)
            return types.SimpleNamespace(completion_text="趋势解读结果")
        with patch.object(main, "LLM_TIMEOUT", .02), patch.object(main, "TREND_LLM_TIMEOUT", .5, create=True):
            snapshot = plugin({"enable_trend_analysis": False})
            snapshot.context.llm_generate.side_effect = slow_response
            snapshot_text = await snapshot._generate_llm_analysis(overview())
            self.assertIn("LLM 分析暂不可用", snapshot_text)
            selected = plugin()
            selected.context.llm_generate.side_effect = slow_response
            trend_text = await selected._generate_llm_analysis(overview())
            self.assertIn("🤖 趋势解读", trend_text)
            self.assertIn("趋势解读结果", trend_text)

    async def test_empty_or_failed_llm_preserves_market_and_trend_header(self):
        for response in (" ", RuntimeError("offline")):
            instance = plugin()
            if isinstance(response, Exception):
                instance.context.llm_generate.side_effect = response
            else:
                instance.context.llm_generate.return_value.completion_text = response
            text = await instance._generate_llm_analysis(overview())
            self.assertIn("120.00", text)
            self.assertIn("暂不可用", text)
            self.assertNotIn("有依据的解读", text)

    async def test_multi_group_image_fallback_reuses_single_history_and_analysis(self):
        instance = plugin({"enable_llm_analysis": True, "enable_image_output": True})
        instance._send_group_text = AsyncMock(side_effect=[False, True, True])
        with patch.object(main, "fetch_market_overview", AsyncMock(return_value=overview())), patch.object(main, "render_market_card", Mock(return_value=b"png")) as render:
            await instance._push_to_groups(["123", "456"])
        self.assertEqual(instance._history_client.fetch.await_count, 1)
        self.assertEqual(instance.context.llm_generate.await_count, 1)
        self.assertEqual(render.call_count, 1)
        self.assertEqual([call.args[0] for call in instance._send_group_text.await_args_list], ["123", "123", "456"])

    async def test_status_reads_no_network_and_includes_last_check_timestamp(self):
        instance = plugin()
        instance._history_state = "2026-10-23 16:00:00：合格历史3/5只"
        event = types.SimpleNamespace(plain_result=lambda text: text)
        text = [item async for item in instance.cmd_status(event)][0]
        self.assertIn("2026-10-23 16:00:00", text)
        self.assertIn("2025", text)
        instance._history_client.fetch.assert_not_awaited()

    async def test_unload_during_provider_resolution_does_not_start_generation(self):
        instance = plugin({"llm_provider_id": ""})
        entered, release = asyncio.Event(), asyncio.Event()
        async def resolve(*args):
            entered.set()
            await release.wait()
            return "selected"
        instance.context.get_current_chat_provider_id = resolve
        work = asyncio.create_task(instance._generate_llm_analysis(overview(), "umo"))
        await entered.wait()
        with patch.object(main, "close_session", AsyncMock()):
            await instance.terminate()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await work
        instance.context.llm_generate.assert_not_awaited()
        instance._history_client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
