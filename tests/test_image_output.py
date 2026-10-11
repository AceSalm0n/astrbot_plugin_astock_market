"""Exercise image commands and push fallbacks without external sends."""

import asyncio
import base64
import builtins
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

from test_round1 import TZ, load_plugin, main, market, overview, plugin


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, data):
        self.data = data

    @staticmethod
    def fromBytes(data):
        return Image(data)


class Chain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])

    def message(self, text):
        self.chain.append(Plain(text))
        return self


class Event:
    unified_msg_origin = "test:GroupMessage:123"

    def plain_result(self, text):
        return Chain().message(text)

    def chain_result(self, chain):
        return Chain(chain)


class ImageOutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = overview()
        self.instance = plugin({"enable_image_output": True})
        self.instance._group_umo_map = {}
        self.instance._learn_group_mapping = Mock()
        self.api = types.ModuleType("astrbot.api.message_components")
        self.api.Image = Image
        self.api.Plain = Plain
        self.modules = patch.dict(
            sys.modules, {"astrbot.api.message_components": self.api}
        )
        self.modules.start()
        self.chain_patch = patch.object(main, "MessageChain", Chain)
        self.chain_patch.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.chain_patch.stop)

    async def command(self, name):
        with patch.object(
            main, "fetch_market_overview", AsyncMock(return_value=self.data)
        ):
            return [item async for item in getattr(self.instance, name)(Event())][-1]

    def context(self, completion="指数表现有限"):
        self.instance.config["llm_provider_id"] = "selected"
        self.instance.context = types.SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text=completion)
            )
        )
        return self.instance.context

    def direct_sender(self, fail=None):
        self.sent = []

        async def call(action, **kwargs):
            self.sent.append((action, kwargs))
            if fail and fail(kwargs):
                raise RuntimeError("simulated transport rejection")

        platform = types.SimpleNamespace(client=types.SimpleNamespace(call_action=call))
        self.instance.context.platform_manager = types.SimpleNamespace(
            get_insts=lambda: [platform]
        )

    async def test_image_status_and_single_index_remain_text(self):
        for enabled in (False, True):
            self.instance.config["enable_image_output"] = enabled
            result = [item async for item in self.instance.cmd_status(Event())][-1]
            self.assertIsInstance(result.chain[0], Plain)
            self.assertIn("图片输出配置", result.chain[0].text)
            self.assertIn("已开启" if enabled else "已关闭", result.chain[0].text)
        with patch.object(
            main, "fetch_single_index", AsyncMock(return_value=self.data.indices[0])
        ):
            result = [item async for item in self.instance.cmd_shanghai(Event())][-1]
        self.assertIsInstance(result.chain[0], Plain)
        self.assertIn("最高", result.chain[0].text)

    async def test_all_overview_commands_use_image_only_when_enabled(self):
        for name in ("cmd_market_overview", "cmd_market_quick"):
            with (
                self.subTest(name=name),
                patch.object(
                    main, "render_market_card", return_value=b"png", create=True
                ),
            ):
                result = await self.command(name)
                self.assertEqual(len(result.chain), 1)
                self.assertIsInstance(result.chain[0], Image)
                self.assertEqual(result.chain[0].data, b"png")

    async def test_disabled_image_mode_keeps_complete_text_and_never_renders(self):
        self.instance.config["enable_image_output"] = False
        self.context()
        with patch.object(
            main,
            "render_market_card",
            side_effect=AssertionError("unexpected render"),
            create=True,
        ):
            for name in (
                "cmd_market_overview",
                "cmd_market_quick",
                "cmd_market_analysis",
            ):
                with self.subTest(name=name):
                    result = await self.command(name)
                    self.assertTrue(
                        all(isinstance(item, Plain) for item in result.chain)
                    )
                    self.assertIn("3100.00", result.chain[0].text)
                    self.assertIn("最高", result.chain[0].text)

    async def test_rendering_runs_outside_event_loop_thread(self):
        loop_thread = threading.get_ident()
        drawing_threads = []

        def render(data):
            drawing_threads.append(threading.get_ident())
            return b"png"

        with patch.object(main, "render_market_card", side_effect=render, create=True):
            await self.command("cmd_market_quick")
        self.assertEqual(len(drawing_threads), 1)
        self.assertNotEqual(drawing_threads[0], loop_thread)

    async def test_render_failure_returns_complete_market_and_analysis_once(self):
        context = self.context()
        for failure in (
            ImportError("Pillow absent"),
            OSError("CJK font absent"),
            RuntimeError("drawing failed"),
        ):
            with (
                self.subTest(failure=failure),
                patch.object(
                    main, "render_market_card", side_effect=failure, create=True
                ),
            ):
                result = await self.command("cmd_market_analysis")
                self.assertEqual(len(result.chain), 1)
                self.assertIn("3100.00", result.chain[0].text)
                self.assertIn("指数表现有限", result.chain[0].text)
        self.assertEqual(context.llm_generate.await_count, 3)

    async def test_image_construction_failure_returns_full_text_without_second_llm(
        self,
    ):
        context = self.context()
        with (
            patch.object(main, "render_market_card", return_value=b"png", create=True),
            patch.object(
                Image, "fromBytes", side_effect=ValueError("invalid component")
            ),
        ):
            result = await self.command("cmd_market_analysis")
        self.assertEqual(len(result.chain), 1)
        self.assertIn("3100.00", result.chain[0].text)
        self.assertIn("指数表现有限", result.chain[0].text)
        self.assertEqual(context.llm_generate.await_count, 1)

    async def test_analysis_image_precedes_text_without_duplicate_market(self):
        self.context()
        with patch.object(main, "render_market_card", return_value=b"png", create=True):
            result = await self.command("cmd_market_analysis")
        self.assertIsInstance(result.chain[0], Image)
        self.assertIsInstance(result.chain[1], Plain)
        self.assertIn("指数表现有限", result.chain[1].text)
        self.assertNotIn("3100.00", result.chain[1].text)
        self.assertIn(
            "3100.00", self.instance.context.llm_generate.await_args.kwargs["prompt"]
        )

    async def test_llm_failure_still_sends_image(self):
        context = self.context()
        context.llm_generate.side_effect = RuntimeError("provider unavailable")
        with patch.object(main, "render_market_card", return_value=b"png", create=True):
            result = await self.command("cmd_market_analysis")
        self.assertIsInstance(result.chain[0], Image)
        self.assertIn("暂不可用", result.chain[1].text)
        self.assertNotIn("以上为原始行情", result.chain[1].text)

    async def test_empty_overview_does_not_render_or_push(self):
        self.data.indices = []
        self.direct_sender()
        with (
            patch.object(
                main,
                "render_market_card",
                side_effect=AssertionError("unexpected render"),
                create=True,
            ),
            patch.object(
                main, "fetch_market_overview", AsyncMock(return_value=self.data)
            ),
        ):
            result = await self.command("cmd_market_quick")
            await self.instance._push_to_groups(["123"])
        self.assertIn("失败", result.chain[0].text)
        self.assertEqual(self.sent, [])

    async def test_batch_draws_and_analyzes_once_with_ordered_onebot_segments(self):
        context = self.context()
        self.instance.config["enable_llm_analysis"] = True
        self.direct_sender()
        with (
            patch.object(
                main, "fetch_market_overview", AsyncMock(return_value=self.data)
            ) as fetch,
            patch.object(
                main, "render_market_card", return_value=b"png", create=True
            ) as render,
        ):
            await self.instance._push_to_groups(["123", "456"])
        self.assertEqual(render.call_count, 1)
        self.assertEqual(context.llm_generate.await_count, 1)
        fetch.assert_awaited_once_with(force_refresh=True)
        self.assertEqual([call[1]["group_id"] for call in self.sent], [123, 456])
        for action, payload in self.sent:
            self.assertEqual(action, "send_group_msg")
            self.assertEqual(
                payload["message"][0],
                {
                    "type": "image",
                    "data": {"file": "base64://" + base64.b64encode(b"png").decode()},
                },
            )
            self.assertEqual(payload["message"][1]["type"], "text")
            self.assertNotIn("3100.00", payload["message"][1]["data"]["text"])

    async def test_mapping_fallback_keeps_image_and_uses_distinct_chains(self):
        self.direct_sender(fail=lambda payload: True)
        self.instance._group_umo_map = {"123": "first", "456": "second"}
        self.instance.context.send_message = AsyncMock(return_value=True)
        with (
            patch.object(
                main, "fetch_market_overview", AsyncMock(return_value=self.data)
            ),
            patch.object(main, "render_market_card", return_value=b"png", create=True),
        ):
            await self.instance._push_to_groups(["123", "456"])
        calls = self.instance.context.send_message.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual([call.args[0] for call in calls], ["first", "second"])
        self.assertIsNot(calls[0].args[1], calls[1].args[1])
        self.assertIsInstance(calls[0].args[1].chain[0], Image)
        self.assertEqual(len(self.sent), 2)

    async def test_mapping_false_triggers_text_only_for_failed_group(self):
        self.context()
        self.instance.config["enable_llm_analysis"] = True
        self.direct_sender(
            fail=lambda payload: (
                payload["group_id"] == 123 and payload["message"][0]["type"] == "image"
            )
        )
        self.instance._group_umo_map = {"123": "first"}
        self.instance.context.send_message = AsyncMock(return_value=False)
        with (
            patch.object(
                main, "fetch_market_overview", AsyncMock(return_value=self.data)
            ),
            patch.object(
                main, "render_market_card", return_value=b"png", create=True
            ) as render,
        ):
            await self.instance._push_to_groups(["123", "456"])
        self.assertEqual(
            [(p["group_id"], p["message"][0]["type"]) for _, p in self.sent],
            [(123, "image"), (123, "text"), (456, "image")],
        )
        self.assertIn("3100.00", self.sent[1][1]["message"][0]["data"]["text"])
        self.assertIn("指数表现有限", self.sent[1][1]["message"][0]["data"]["text"])
        self.assertEqual(render.call_count, 1)
        self.assertEqual(self.instance.context.llm_generate.await_count, 1)

    async def test_mapping_exception_still_attempts_text_fallback_and_next_group(self):
        self.direct_sender(fail=lambda payload: payload["group_id"] == 123)
        self.instance._group_umo_map = {"123": "first"}
        self.instance.context.send_message = AsyncMock(
            side_effect=[RuntimeError("image rejected"), True]
        )
        with (
            patch.object(
                main, "fetch_market_overview", AsyncMock(return_value=self.data)
            ),
            patch.object(main, "render_market_card", return_value=b"png", create=True),
        ):
            await self.instance._push_to_groups(["123", "456"])
        calls = self.instance.context.send_message.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertIsInstance(calls[1].args[1].chain[0], Plain)
        self.assertIn("3100.00", calls[1].args[1].chain[0].text)
        self.assertEqual(self.sent[-1][1]["group_id"], 456)

    async def test_failed_text_mapping_is_not_counted_as_success(self):
        self.instance.config["enable_image_output"] = False
        self.direct_sender(fail=lambda payload: True)
        self.instance._group_umo_map = {"123": "first"}
        self.instance.context.send_message = AsyncMock(return_value=False)
        with (
            patch.object(
                main, "fetch_market_overview", AsyncMock(return_value=self.data)
            ),
            patch.object(main.logger, "info") as info,
        ):
            await self.instance._push_to_groups(["123"])
        self.assertIn("0/1", info.call_args.args[0])
        self.assertEqual(len(self.sent), 1)

    async def test_render_failure_batch_sends_complete_text(self):
        self.direct_sender()
        with (
            patch.object(
                main, "fetch_market_overview", AsyncMock(return_value=self.data)
            ),
            patch.object(
                main,
                "render_market_card",
                side_effect=OSError("font absent"),
                create=True,
            ),
        ):
            await self.instance._push_to_groups(["123", "456"])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(
            all(payload["message"][0]["type"] == "text" for _, payload in self.sent)
        )
        self.assertIn("3100.00", self.sent[0][1]["message"][0]["data"]["text"])


class AnalysisOnlyTests(unittest.IsolatedAsyncioTestCase):
    def test_plugin_can_import_without_pillow(self):
        original = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ModuleNotFoundError("Pillow missing")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=unavailable):
            loaded, _ = load_plugin()
        self.assertTrue(callable(loaded.AStockMarketPlugin.cmd_market_overview))

    async def test_analysis_only_success_still_supplies_full_data_to_model(self):
        context = types.SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text="解释")
            )
        )
        result = await plugin(
            {"llm_provider_id": "selected"}, context
        )._generate_llm_analysis(overview(), include_market=False)
        self.assertIn("解释", result)
        self.assertNotIn("3100.00", result)
        self.assertIn("3100.00", context.llm_generate.await_args.kwargs["prompt"])

    async def test_analysis_only_failure_branches_never_duplicate_market(self):
        for response in (" \n", RuntimeError("provider absent"), "slow"):
            context = types.SimpleNamespace(
                llm_generate=AsyncMock(
                    return_value=types.SimpleNamespace(completion_text=response)
                )
            )
            if isinstance(response, Exception):
                context.llm_generate.side_effect = response
            if response == "slow":

                async def slow(**kwargs):
                    await asyncio.sleep(1)

                context.llm_generate.side_effect = slow
            with (
                self.subTest(response=response),
                patch.object(main, "LLM_TIMEOUT", 0.01),
            ):
                result = await plugin(
                    {"llm_provider_id": "selected"}, context
                )._generate_llm_analysis(overview(), include_market=False)
                self.assertIn("暂不可用", result)
                self.assertNotIn("3100.00", result)
                self.assertNotIn("以上为原始行情", result)

    async def test_analysis_only_empty_and_historical_skip_without_llm(self):
        for data in (
            market.MarketOverview([], datetime.now(TZ)),
            overview(datetime.now(TZ) - timedelta(days=1)),
        ):
            context = types.SimpleNamespace(llm_generate=AsyncMock())
            result = await plugin(
                {"llm_provider_id": "selected"}, context
            )._generate_llm_analysis(data, include_market=False)
            self.assertIn("跳过", result)
            self.assertNotIn("3100.00", result)
            self.assertEqual(context.llm_generate.await_count, 0)


if __name__ == "__main__":
    unittest.main()
