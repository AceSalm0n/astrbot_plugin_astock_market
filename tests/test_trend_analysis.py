"""Verify trading-date and indicator behavior using hand-checked fixtures."""

import importlib.util
import math
import sys
import types
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "trend_contract_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
for filename in ("market_history", "trend_analysis"):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{filename}", ROOT / f"{filename}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    setattr(package, filename, module)

history, trend = package.market_history, package.trend_analysis
TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 23, 16, tzinfo=TZ)
T = NOW.date()
CALENDAR = trend.load_trading_calendar(ROOT / "assets/trading_calendar.json")
DATES = CALENDAR.session_dates(date(2026, 8, 1), T)[-21:]


def item(code="000001", prices=None, amounts=None):
    prices = prices or list(range(100, 121))
    amounts = amounts or [100] * 21
    bars = tuple(history.DailyBar(day, p, p, p + 1, p - 1, 1, a)
                 for day, p, a in zip(DATES, prices, amounts))
    return history.IndexHistory(code, "fixture", bars, NOW, False, "fixture_scope")


class TrendTests(unittest.TestCase):
    def test_prompt_locks_session_dates_and_unknown_amount_boundary(self):
        prompt = trend.TREND_ANALYSIS_PROMPT
        self.assertIn("原样沿用程序给出的T+3/T+5交易日期", prompt)
        self.assertIn("成交额比较未知时，只能写", prompt)
        self.assertIn("单日绝对成交额不能证明放量", prompt)
        self.assertIn("每只指数最多一句", prompt)

    def test_cumulative_returns_and_means_are_not_summed_percentages(self):
        values = trend.calculate_indicators(item())
        self.assertAlmostEqual(values["return_5"], 100 * (120 / 115 - 1))
        self.assertAlmostEqual(values["return_20"], 20)
        self.assertAlmostEqual(values["ma_5"], 118)
        self.assertAlmostEqual(values["ma_20"], 110.5)
        self.assertAlmostEqual(values["deviation_5"], 100 * (120 / 118 - 1))
        self.assertEqual(values["range_low"], 100)
        self.assertEqual(values["range_high"], 121)

    def test_volatility_is_sample_daily_percent_not_annualized(self):
        prices = [100]
        for n in range(20):
            prices.append(prices[-1] * (1.01 if n % 2 == 0 else .99))
        self.assertAlmostEqual(trend.calculate_indicators(item(prices=prices))["volatility_20"], math.sqrt(20 / 19))
        self.assertEqual(trend.calculate_indicators(item(prices=[100] * 21))["volatility_20"], 0)

    def test_amount_ratio_requires_known_complete_same_scope(self):
        self.assertEqual(trend.calculate_indicators(item())["amount_ratio"], 1)
        self.assertEqual(trend.calculate_indicators(item(amounts=[100] * 16 + [200] * 5))["amount_ratio"], 1.6)
        for candidate in (item(amounts=[0] * 21), item(amounts=[None] + [None] + [100] * 19), replace(item(), amount_scope=None)):
            self.assertIsNone(trend.calculate_indicators(candidate)["amount_ratio"])

    def test_baseline_respects_cutoff_weekends_and_exchange_holidays(self):
        cases = [(datetime(2026, 10, 23, 15, 14, tzinfo=TZ), date(2026, 10, 22)),
                 (datetime(2026, 10, 23, 15, 15, tzinfo=TZ), T),
                 (datetime(2026, 10, 24, 10, tzinfo=TZ), T),
                 (datetime(2026, 10, 8, 9, tzinfo=TZ), date(2026, 9, 30)),
                 (datetime(2026, 10, 10, 10, tzinfo=TZ), date(2026, 10, 9))]
        for now, expected in cases:
            self.assertEqual(trend.resolve_baseline(now, CALENDAR), expected)
        self.assertIsNone(trend.resolve_baseline(datetime(2027, 1, 4, 16, tzinfo=TZ), CALENDAR))

    def test_context_exposes_partial_coverage_and_program_owned_dates(self):
        batch = history.HistoryBatch(T, {"000001": item()}, {"399001": "missing"})
        context = trend.build_trend_context(batch, NOW, CALENDAR, [])
        self.assertFalse(context.overall_allowed)
        self.assertIn("整体证据不足", context.header)
        self.assertEqual(context.target_dates, (date(2026, 10, 28), date(2026, 10, 30)))
        self.assertIn("2026-10-23", context.header)
        self.assertIn("21", context.history_text)
        self.assertIn("缺失", context.quality_notes)
        self.assertIn("近期10日", context.history_text)
        prompt = trend.TREND_ANALYSIS_PROMPT.format(current_time="now", baseline=context.baseline,
            horizon=context.header, market_data="snapshot", trend_data=context.history_text, quality_notes=context.quality_notes)
        self.assertIn("snapshot", prompt)

    def test_core_indices_allow_overall_and_mix_sources_is_disclosed(self):
        histories = {code: item(code) for code in ("000001", "399001", "000300")}
        histories["399001"] = replace(histories["399001"], source="other", from_cache=True)
        context = trend.build_trend_context(history.HistoryBatch(T, histories, {}), NOW, CALENDAR, [])
        self.assertTrue(context.overall_allowed)
        self.assertIn("混合来源", context.quality_notes)
        self.assertIn("缓存", context.history_text)

    def test_related_manual_closure_blocks_but_unrelated_does_not(self):
        batch = history.HistoryBatch(T, {"000001": item()}, {})
        dates = ["2026-10-28"]
        context = trend.build_trend_context(batch, NOW, CALENDAR, dates)
        self.assertFalse(context.eligible_codes)
        self.assertIn("临时休市需核实", context.quality_notes)
        self.assertEqual(dates, ["2026-10-28"])
        good = trend.build_trend_context(batch, NOW, CALENDAR, ["2026-01-05", "2026-10-01", "bad"])
        self.assertEqual(good.eligible_codes, ("000001",))

    def test_unknown_future_year_keeps_relative_targets(self):
        end = date(2026, 12, 31)
        dates = CALENDAR.session_dates(date(2026, 11, 1), end)[-21:]
        candidate = item()
        candidate = replace(candidate, bars=tuple(replace(bar, trade_date=day) for bar, day in zip(candidate.bars, dates)))
        context = trend.build_trend_context(history.HistoryBatch(end, {"000001": candidate}, {}), datetime(2026, 12, 31, 16, tzinfo=TZ), CALENDAR, ["2027-01-04"])
        self.assertIsNone(context.target_dates)
        self.assertIn("T+3", context.header)
        self.assertEqual(context.eligible_codes, ("000001",))

    def test_cross_year_target_still_checks_known_future_closure(self):
        end = date(2026, 12, 28)
        dates = CALENDAR.session_dates(date(2026, 11, 1), end)[-21:]
        candidate = item()
        candidate = replace(candidate, bars=tuple(replace(bar, trade_date=day) for bar, day in zip(candidate.bars, dates)))
        context = trend.build_trend_context(history.HistoryBatch(end, {"000001": candidate}, {}), datetime(2026, 12, 28, 16, tzinfo=TZ), CALENDAR, ["2026-12-29"])
        self.assertIsNone(context.target_dates)
        self.assertFalse(context.eligible_codes)
        self.assertIn("临时休市需核实", context.quality_notes)


if __name__ == "__main__":
    unittest.main()
