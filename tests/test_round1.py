"""Regression checks for scheduling and analysis, without a running bot."""

import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


def load_plugin():
    """Load real plugin code with only the unavailable AstrBot SDK stubbed."""
    package = types.ModuleType("round1_plugin")
    package.__path__ = [str(ROOT)]
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("round1-tests")
    api.logger.addHandler(logging.NullHandler())
    api.logger.propagate = False
    api.AstrBotConfig = dict
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.MessageChain = object
    event.filter = types.SimpleNamespace(command=lambda name: lambda fn: fn)
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = object
    star.StarTools = object
    star.register = lambda *args: lambda cls: cls
    stubs = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "round1_plugin": package,
    }
    with patch.dict(sys.modules, stubs):
        for name in ("market_api", "main"):
            spec = importlib.util.spec_from_file_location(
                f"round1_plugin.{name}", ROOT / f"{name}.py"
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            setattr(package, name, module)
    return package.main, package.market_api


main, market = load_plugin()


def plugin(config=None, context=None):
    """Build a plugin without writing group mappings or starting tasks."""
    instance = main.AStockMarketPlugin.__new__(main.AStockMarketPlugin)
    instance.config = config or {}
    instance.context = context or types.SimpleNamespace()
    instance._startup_task = None
    instance._push_task = None
    instance._last_push_slot = None
    instance._next_push_at = None
    return instance


def overview(quote_time=None):
    """Return a hand-checked market fixture with optional source time."""
    data = market.IndexData(
        "000001",
        "上证指数",
        3100.0,
        100.0,
        3.33,
        3000.0,
        3120.0,
        2980.0,
        3000.0,
        123.0,
        100000000.0,
    )
    data.quote_time = quote_time
    data.source = "fixture"
    data.fetched_at = datetime.now(TZ)
    return market.MarketOverview([data], datetime.now(TZ), "fixture")


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def run_schedule(self, start, config=None, lag=0, update=None):
        """Advance a fake wall clock and capture the first actual push slot."""
        clock = [start]
        sent = []
        sleeps = [0]
        instance = plugin(
            {
                "enable_scheduled_push": True,
                "scheduled_push_groups": ["123"],
                "scheduled_push_times": "09:30,11:30,15:00",
                **(config or {}),
            }
        )

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

        async def sleep(seconds):
            sleeps[0] += 1
            if sleeps[0] > 10000:
                raise asyncio.CancelledError
            clock[0] += timedelta(seconds=seconds)
            if lag and clock[0].hour == 9 and clock[0].minute == 30:
                clock[0] += timedelta(seconds=lag)
            if update:
                update(instance, sleeps[0])

        async def send(groups):
            sent.append((clock[0], list(groups)))
            raise asyncio.CancelledError

        instance._push_to_groups = send
        with (
            patch.object(main, "datetime", Clock),
            patch.object(main.asyncio, "sleep", sleep),
        ):
            await instance._scheduled_push_loop()
        return sent

    def test_push_times_are_sorted_unique_and_invalid_items_ignored(self):
        instance = plugin({"scheduled_push_times": "15:00,09:30,bad,09:30,25:00"})
        self.assertEqual(instance._parse_push_times(), [time(9, 30), time(15)])

    async def test_friday_last_slot_never_sends_on_saturday(self):
        sent = await self.run_schedule(datetime(2026, 10, 9, 15, 1, tzinfo=TZ))
        self.assertEqual(sent, [(datetime(2026, 10, 12, 9, 30, tzinfo=TZ), ["123"])])

    async def test_weekend_wait_keeps_monday_first_slot(self):
        sent = await self.run_schedule(datetime(2026, 10, 10, 12, tzinfo=TZ))
        self.assertEqual(sent, [(datetime(2026, 10, 12, 9, 30, tzinfo=TZ), ["123"])])

    async def test_configured_closed_date_is_skipped(self):
        sent = await self.run_schedule(
            datetime(2026, 10, 9, 15, 1, tzinfo=TZ),
            {"market_closed_dates": ["2026-10-12"]},
        )
        self.assertEqual(sent, [(datetime(2026, 10, 13, 9, 30, tzinfo=TZ), ["123"])])

    async def test_late_wakeup_skips_expired_slot(self):
        sent = await self.run_schedule(datetime(2026, 10, 12, 9, 29, tzinfo=TZ), lag=61)
        self.assertEqual(sent, [(datetime(2026, 10, 12, 11, 30, tzinfo=TZ), ["123"])])

    async def test_normal_wakeup_delay_is_allowed(self):
        sent = await self.run_schedule(datetime(2026, 10, 12, 9, 29, tzinfo=TZ), lag=2)
        self.assertEqual(sent, [(datetime(2026, 10, 12, 9, 30, 2, tzinfo=TZ), ["123"])])

    async def test_groups_are_read_again_before_push(self):
        def update(instance, count):
            if count == 1:
                instance.config["scheduled_push_groups"] = ["456"]

        sent = await self.run_schedule(
            datetime(2026, 10, 12, 9, 29, tzinfo=TZ),
            update=update,
        )
        self.assertEqual(sent[0][1], ["456"])

    async def test_disabled_scheduler_does_not_send(self):
        def update(instance, count):
            instance.config["enable_scheduled_push"] = False
            if count == 2:
                raise asyncio.CancelledError

        sent = await self.run_schedule(
            datetime(2026, 10, 12, 9, 29, tzinfo=TZ),
            update=update,
        )
        self.assertEqual(sent, [])

    async def test_removed_time_does_not_send_pending_slot(self):
        def update(instance, count):
            if count == 1:
                instance.config["scheduled_push_times"] = "11:30"

        sent = await self.run_schedule(
            datetime(2026, 10, 12, 9, 29, tzinfo=TZ),
            update=update,
        )
        self.assertEqual(sent, [(datetime(2026, 10, 12, 11, 30, tzinfo=TZ), ["123"])])

    async def test_new_closed_date_invalidates_pending_slot(self):
        def update(instance, count):
            if count == 1:
                instance.config["market_closed_dates"] = ["2026-10-12"]

        sent = await self.run_schedule(
            datetime(2026, 10, 12, 9, 29, tzinfo=TZ),
            update=update,
        )
        self.assertEqual(sent, [(datetime(2026, 10, 13, 9, 30, tzinfo=TZ), ["123"])])

    async def test_added_later_time_keeps_due_slot(self):
        def update(instance, count):
            if count == 1:
                instance.config["scheduled_push_times"] = "09:30,11:30,13:00,15:00"

        sent = await self.run_schedule(
            datetime(2026, 10, 12, 9, 29, tzinfo=TZ),
            update=update,
        )
        self.assertEqual(sent, [(datetime(2026, 10, 12, 9, 30, tzinfo=TZ), ["123"])])

    async def test_future_closed_date_keeps_due_slot(self):
        def update(instance, count):
            if count == 1:
                instance.config["market_closed_dates"] = ["2026-10-13"]

        sent = await self.run_schedule(
            datetime(2026, 10, 12, 9, 29, tzinfo=TZ),
            update=update,
        )
        self.assertEqual(sent, [(datetime(2026, 10, 12, 9, 30, tzinfo=TZ), ["123"])])

    async def test_startup_does_not_replay_expired_slot(self):
        sent = await self.run_schedule(datetime(2026, 10, 12, 9, 30, 30, tzinfo=TZ))
        self.assertEqual(sent, [(datetime(2026, 10, 12, 11, 30, tzinfo=TZ), ["123"])])


class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    def context(self, text="分析结果"):
        return types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="current"),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text=text)
            ),
            get_using_provider=Mock(return_value=None),
        )

    async def test_success_keeps_original_market_data(self):
        context = self.context()
        result = await plugin(
            {"llm_provider_id": "configured"}, context
        )._generate_llm_analysis(overview())
        self.assertIn("3100.00", result)
        self.assertIn("分析结果", result)

    async def test_whitespace_output_falls_back_to_data(self):
        result = await plugin(
            {"llm_provider_id": "configured"},
            self.context(" \n "),
        )._generate_llm_analysis(overview())
        self.assertIn("3100.00", result)
        self.assertIn("暂不可用", result)

    async def test_previous_date_skips_analysis(self):
        context = self.context()
        result = await plugin(
            {"llm_provider_id": "configured"},
            context,
        )._generate_llm_analysis(overview(datetime.now(TZ) - timedelta(days=1)))
        self.assertIn("3100.00", result)
        self.assertIn("历史", result)
        context.llm_generate.assert_not_awaited()

    async def test_explicit_provider_failure_does_not_switch_provider(self):
        context = self.context()
        context.llm_generate.side_effect = RuntimeError("provider unavailable")
        result = await plugin(
            {"llm_provider_id": "configured"}, context
        )._generate_llm_analysis(overview())
        self.assertIn("3100.00", result)
        context.get_using_provider.assert_not_called()

    async def test_total_budget_includes_provider_resolution(self):
        context = self.context()

        async def slow_provider(umo):
            await asyncio.sleep(1)
            return "late"

        context.get_current_chat_provider_id.side_effect = slow_provider
        with patch.object(main, "LLM_TIMEOUT", 0.01, create=True):
            result = await plugin({}, context)._generate_llm_analysis(
                overview(), "session"
            )
        self.assertIn("3100.00", result)
        context.llm_generate.assert_not_awaited()

    async def test_llm_call_is_cancelled_at_budget(self):
        context = self.context()
        cancelled = asyncio.Event()

        async def slow_llm(**kwargs):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        context.llm_generate.side_effect = slow_llm
        with patch.object(main, "LLM_TIMEOUT", 0.01, create=True):
            result = await plugin(
                {"llm_provider_id": "configured"}, context
            )._generate_llm_analysis(overview())
        self.assertIn("3100.00", result)
        self.assertTrue(cancelled.is_set())

    async def test_empty_overview_never_calls_llm(self):
        context = self.context()
        result = await plugin(
            {"llm_provider_id": "configured"}, context
        )._generate_llm_analysis(
            market.MarketOverview([], datetime.now(TZ)),
        )
        self.assertTrue(result)
        context.llm_generate.assert_not_awaited()

    async def test_scheduler_requests_fresh_data(self):
        instance = plugin({"enable_llm_analysis": False})
        instance._send_group_text = AsyncMock(return_value=True)
        with patch.object(
            main, "fetch_market_overview", AsyncMock(return_value=overview())
        ) as fetch:
            await instance._push_to_groups(["123"])
        fetch.assert_awaited_once_with(force_refresh=True)

    async def test_scheduled_default_provider_does_not_use_empty_session(self):
        context = self.context()
        provider = types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(id="default")
        )
        context.get_using_provider_async = AsyncMock(return_value=provider)
        result = await plugin({}, context)._generate_llm_analysis(overview())
        self.assertIn("3100.00", result)
        self.assertIn("分析结果", result)
        context.get_current_chat_provider_id.assert_not_awaited()
        self.assertEqual(
            context.llm_generate.await_args.kwargs["chat_provider_id"], "default"
        )

    async def test_legacy_provider_preserves_explicit_selection_and_model_override(
        self,
    ):
        selected = types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(id="selected"),
            text_chat=AsyncMock(
                return_value=types.SimpleNamespace(completion_text="旧版解读")
            ),
        )
        other = types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(id="other"),
            text_chat=AsyncMock(),
        )
        context = types.SimpleNamespace(
            get_all_providers=lambda: [other, selected],
            get_using_provider=Mock(return_value=other),
        )
        result = await plugin(
            {"llm_provider_id": "selected", "llm_model": "legacy-model"},
            context,
        )._generate_llm_analysis(overview())
        self.assertIn("3100.00", result)
        self.assertIn("旧版解读", result)
        self.assertEqual(selected.text_chat.await_args.kwargs["model"], "legacy-model")
        other.text_chat.assert_not_awaited()

    async def test_unknown_single_index_fields_do_not_crash_or_list_other_indices(self):
        data = overview().indices[0]
        data.change = None
        data.change_pct = None
        data.pre_close = None
        with patch.object(main, "fetch_single_index", AsyncMock(return_value=data)):
            result = await plugin()._query_index("000001")
        self.assertIn("3100.00", result)
        self.assertIn("未知", result)
        self.assertNotIn("缺失指数", result)

    async def test_terminate_waits_for_owned_tasks_and_closes_market_resources(self):
        instance = plugin()
        instance._startup_task = asyncio.create_task(asyncio.sleep(100))
        instance._push_task = asyncio.create_task(asyncio.sleep(100))
        with patch.object(main, "close_session", AsyncMock()) as close:
            await instance.terminate()
        self.assertTrue(instance._startup_task.cancelled())
        self.assertTrue(instance._push_task.cancelled())
        close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
