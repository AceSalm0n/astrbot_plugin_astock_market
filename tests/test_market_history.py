"""Check history contracts without network access or a running bot."""

import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


def load_history():
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("history-tests")
    name = "history_contract_tests"
    spec = importlib.util.spec_from_file_location(name, ROOT / "market_history.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with patch.dict(sys.modules, {"astrbot.api": api}):
        spec.loader.exec_module(module)
    return module


history = load_history()
T = date(2026, 10, 23)
NOW = datetime(2026, 10, 23, 16, tzinfo=TZ)
CLOSED = frozenset(date(2026, 10, n) for n in range(1, 8))
CALENDAR = history.TradingCalendar({2026: CLOSED})
DATES = CALENDAR.session_dates(date(2026, 8, 1), T)[-25:]


def payload(code="000001", dates=DATES):
    return {"rc": 0, "data": {"code": code, "market": 1 if code.startswith("0") else 0,
            "klines": [f"{d},100,102,103,99,12,1200,0,0,0,0" for d in dates]}}


def quote(code="000001", stamp="20261023150003", close="102"):
    fields = [""] * 40
    for index, value in {2: code, 3: close, 5: "100", 30: stamp, 33: "103", 34: "99"}.items():
        fields[index] = value
    symbol = ("sh" if code.startswith("0") else "sz") + code
    return f'v_{symbol}="' + "~".join(fields) + '";'


class ParsingTests(unittest.TestCase):
    def test_sort_deduplicate_and_preserve_optional_unknown(self):
        raw = payload()
        raw["data"]["klines"].reverse()
        raw["data"]["klines"].append(raw["data"]["klines"][0])
        raw["data"]["klines"][1] = raw["data"]["klines"][1].replace(",12,1200,", ",NaN,-1,")
        result = history.parse_history("eastmoney", "000001", raw, T, NOW, CALENDAR)
        self.assertEqual(len(result.bars), 25)
        self.assertEqual(result.bars[-1].trade_date, T)
        self.assertIsNone(result.bars[-2].amount_yuan)
        self.assertIsNone(result.bars[-2].volume)
        self.assertIsNone(result.amount_scope)

    def test_invalid_source_is_not_repaired_by_dropping_rows(self):
        for variant in ("missing", "conflict", "bad_price", "symbol", "future", "short"):
            with self.subTest(variant=variant):
                raw = payload()
                if variant == "missing":
                    raw["data"]["klines"].pop(-3)
                elif variant == "conflict":
                    raw["data"]["klines"].append(f"{T},100,101,103,99,12,1")
                elif variant == "bad_price":
                    raw["data"]["klines"][-1] = f"{T},100,102,101,99,12,1"
                elif variant == "symbol":
                    raw["data"]["code"] = "399001"
                elif variant == "future":
                    raw["data"]["klines"].append("2026-10-26,100,102,103,99,12,1")
                else:
                    raw = payload(dates=DATES[-20:])
                with self.assertRaises(ValueError):
                    history.parse_history("eastmoney", "000001", raw, T, NOW, CALENDAR)

    def test_21_closes_are_enough_and_intraday_row_is_excluded(self):
        raw = payload(dates=DATES[-21:])
        result = history.parse_history("eastmoney", "000001", raw, T, NOW, CALENDAR)
        self.assertEqual(len(result.bars), 21)
        result = history.parse_history("eastmoney", "000001", payload(), DATES[-2], NOW, CALENDAR)
        self.assertEqual(result.bars[-1].trade_date, DATES[-2])

    def test_tencent_accepts_only_unadjusted_day_and_has_unknown_amount(self):
        rows = [line.split(",")[:6] for line in payload()["data"]["klines"]]
        raw = {"code": 0, "data": {"sh000001": {"day": rows}}}
        result = history.parse_history("tencent", "000001", raw, T, NOW, CALENDAR)
        self.assertIsNone(result.bars[-1].amount_yuan)
        raw["data"]["sh000001"] = {"qfqday": rows}
        with self.assertRaises(ValueError):
            history.parse_history("tencent", "000001", raw, T, NOW, CALENDAR)

    def test_today_requires_timestamped_independent_ohlc_match(self):
        bar = history.parse_history("eastmoney", "000001", payload(), T, NOW, CALENDAR).bars[-1]
        self.assertTrue(history.confirm_close("eastmoney", "000001", bar, quote(), NOW))
        for text in (quote(stamp="20261023145959"), quote(close="101"), quote(code="399001"),
                     quote(stamp="20261022150000"), quote(stamp="20261023170000"), "bad"):
            self.assertFalse(history.confirm_close("eastmoney", "000001", bar, text, NOW))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = history.HistoryClient(CALENDAR)
        self.patcher = patch.object(history, "INDEX_CONFIG", (("000001", "上证指数", "1.000001", "sh000001"),))
        self.patcher.start()

    async def asyncTearDown(self):
        await self.client.close()
        self.patcher.stop()

    async def test_primary_finality_failure_falls_back_as_whole_series(self):
        primary = history.parse_history("eastmoney", "000001", payload(), T, NOW, CALENDAR)
        backup = history.IndexHistory("000001", "腾讯财经", primary.bars, NOW, False, None)
        self.client._fetch_source = AsyncMock(side_effect=[ValueError("unconfirmed"), backup])
        with patch.object(history, "now_beijing", return_value=NOW):
            result = await self.client.fetch(T)
        self.assertEqual(result.histories["000001"].source, "腾讯财经")
        self.assertEqual(self.client._fetch_source.await_count, 2)

    async def test_same_key_shared_and_caller_cancel_does_not_cancel_worker(self):
        entered, release = asyncio.Event(), asyncio.Event()
        item = history.parse_history("eastmoney", "000001", payload(), T, NOW, CALENDAR)
        async def source(*args):
            entered.set()
            await release.wait()
            return item
        self.client._fetch_source = AsyncMock(side_effect=source)
        with patch.object(history, "now_beijing", return_value=NOW):
            first = asyncio.create_task(self.client.fetch(T))
            await entered.wait()
            second = asyncio.create_task(self.client.fetch(T))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            result = await second
            cached = await self.client.fetch(T)
        self.assertIn("000001", result.histories)
        self.assertTrue(cached.histories["000001"].from_cache)
        self.assertEqual(self.client._fetch_source.await_count, 1)

    async def test_source_timeout_still_tries_backup_on_python310(self):
        item = history.parse_history("eastmoney", "000001", payload(), T, NOW, CALENDAR)
        self.client._fetch_source = AsyncMock(side_effect=[asyncio.TimeoutError(), item])
        with patch.object(history, "now_beijing", return_value=NOW):
            result = await self.client.fetch(T)
        self.assertIn("000001", result.histories)

    async def test_failure_cooldown_and_retry_after_expiry(self):
        self.client._fetch_source = AsyncMock(side_effect=ValueError("bad"))
        with patch.object(history, "now_beijing", return_value=NOW), patch.object(history, "monotonic", return_value=10):
            await self.client.fetch(T)
            await self.client.fetch(T)
        self.assertEqual(self.client._fetch_source.await_count, 2)
        with patch.object(history, "now_beijing", return_value=NOW), patch.object(history, "monotonic", return_value=71):
            await self.client.fetch(T)
        self.assertEqual(self.client._fetch_source.await_count, 4)

    async def test_old_worker_cannot_restore_evicted_cache_and_is_closed(self):
        old_date = DATES[-2]
        old_entered, old_release = asyncio.Event(), asyncio.Event()
        async def source(source, code, expected, now):
            if expected == old_date:
                old_entered.set()
                await old_release.wait()
            return history.parse_history("eastmoney", code, payload(), expected, now, CALENDAR)
        self.client._fetch_source = AsyncMock(side_effect=source)
        with patch.object(history, "now_beijing", return_value=NOW):
            old = asyncio.create_task(self.client.fetch(old_date))
            await old_entered.wait()
            await self.client.fetch(T)
            old_release.set()
            await old
        self.assertNotIn(("000001", old_date), self.client._cache)
        self.assertIn(("000001", T), self.client._cache)
        self.assertEqual(self.client._active_date, T)

    async def test_close_tracks_old_and_new_workers_and_is_idempotent(self):
        entered = asyncio.Event()
        async def source(*args):
            entered.set()
            await asyncio.Event().wait()
        self.client._fetch_source = AsyncMock(side_effect=source)
        with patch.object(history, "now_beijing", return_value=NOW):
            old = asyncio.create_task(self.client.fetch(DATES[-2]))
            await entered.wait()
            new = asyncio.create_task(self.client.fetch(T))
            await asyncio.sleep(0)
            await self.client.close()
            await asyncio.gather(old, new, return_exceptions=True)
        self.assertFalse(self.client._inflight)
        await self.client.close()
        result = await self.client.fetch(T)
        self.assertFalse(result.histories)
        self.assertIsNone(self.client._session)

    async def test_batch_timeout_retains_completed_indices(self):
        item = history.parse_history("eastmoney", "000001", payload(), T, NOW, CALENDAR)
        async def source(source, code, *args):
            if code != "000001":
                await asyncio.Event().wait()
            return item
        self.client._fetch_source = AsyncMock(side_effect=source)
        with patch.object(history, "INDEX_CONFIG", (("000001", "上证", "1.000001", "sh000001"), ("399001", "深证", "0.399001", "sz399001"))), patch.object(history, "BATCH_TIMEOUT", .3), patch.object(history, "now_beijing", return_value=NOW):
            result = await self.client.fetch(T)
        self.assertIn("000001", result.histories)
        self.assertIn("399001", result.unavailable)

    async def test_cancelled_first_close_caller_does_not_interrupt_cleanup(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def finish():
            entered.set()
            await release.wait()
        session = types.SimpleNamespace(close=AsyncMock(side_effect=finish))
        self.client._session = session
        closing = asyncio.create_task(self.client.close())
        await entered.wait()
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        release.set()
        await self.client.close()
        self.assertIsNone(self.client._session)
        session.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
