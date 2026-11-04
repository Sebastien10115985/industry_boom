"""流动性指数计算与缓存的单元测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import liquidity as lq
import liquidity_sources as ls


def make_weekly_series(days: int = 400, seed: int = 7) -> pd.Series:
    index = pd.date_range("2025-01-03", periods=days, freq="D")
    values = pd.Series(np.sin(np.arange(days) / 20) * 2 + 2, index=index)
    values.index.name = "date"
    return values


def make_monthly_series(months: int = 48, seed: int = 3) -> pd.Series:
    index = pd.date_range("2022-01-31", periods=months, freq="ME")
    values = pd.Series(np.linspace(8, 6, months) + np.sin(np.arange(months)) * 0.5, index=index)
    values.index.name = "date"
    return values


class DirectionAndScalingTests(unittest.TestCase):
    def test_directional_inverts_rates(self):
        series = pd.Series([1.5, 2.0], index=pd.date_range("2026-01-02", periods=2))
        inverted = lq.directional(series, -1)
        self.assertTrue(np.allclose(inverted.to_numpy(), [-1.5, -2.0]))
        kept = lq.directional(series, 1)
        self.assertTrue(np.allclose(kept.to_numpy(), [1.5, 2.0]))

    def test_rolling_zscore_standardizes(self):
        series = make_weekly_series()
        zscore = lq.rolling_zscore(series, window=52, min_periods=26)
        self.assertEqual(zscore.first_valid_index(), series.index[25])
        recent = zscore.iloc[100:]
        self.assertLess(abs(recent.mean()), 0.3)
        self.assertGreater(recent.std(), 0.2)

    def test_rolling_zscore_constant_series_is_empty(self):
        index = pd.date_range("2026-01-02", periods=60, freq="D")
        series = pd.Series(2.0, index=index)
        zscore = lq.rolling_zscore(series, window=20, min_periods=10)
        self.assertTrue(zscore.dropna().empty)


class ResamplingTests(unittest.TestCase):
    def test_resample_weekly_takes_last_trading_day(self):
        series = make_weekly_series(60)
        weekly = lq.resample_weekly(series)
        self.assertEqual(weekly.index.dayofweek.unique().tolist(), [4])
        self.assertEqual(len(weekly), series.index.isocalendar().week.nunique())

    def test_weekly_to_yoy(self):
        index = pd.date_range("2024-01-05", periods=80, freq="W-FRI")
        values = pd.Series(100.0, index=index)
        values.iloc[52:] = 110.0
        yoy = lq.weekly_to_yoy(values)
        self.assertTrue(np.isclose(yoy.dropna().iloc[0], 10.0))
        self.assertTrue(yoy.iloc[:51].isna().all())

    def test_monthly_alignment_delays_publication(self):
        series = make_monthly_series(6)
        aligned = lq.align_monthly_to_weekly(series, effective_days=10)
        self.assertEqual(aligned.index[0], series.index[0] + pd.DateOffset(days=10))

    def test_monthly_values_not_dropped_on_non_friday_effective_days(self):
        """生效日不是周五时，月份值也不能丢：对齐到周五日历后每个周五都有值。"""
        series = make_monthly_series(24)
        effective = lq.align_monthly_to_weekly(series)
        # 月末+10 天大多不是周五，reindex 精确匹配会把它们整月丢掉
        friday_calendar = pd.date_range(effective.index.min(), effective.index.max(), freq="W-FRI")
        aligned = effective.reindex(friday_calendar, method="ffill")
        self.assertFalse(aligned.isna().any())
        # 每个生效月都应体现在对齐后的序列里
        covered_months = {day.to_period("M") for day in aligned.dropna().index}
        expected_months = {day.to_period("M") for day in effective.index}
        self.assertTrue(expected_months.issubset(covered_months))

    def test_monthly_zscore_carried_to_calendar_unchanged(self):
        """月度 z 按月观测算好后搬上周五日历：对齐只做搬运，不得改变数值或放大跳变。"""
        noise = np.sin(np.arange(30)) * 0.2
        index = pd.date_range("2024-01-31", periods=30, freq="ME")
        values = pd.Series(7.0 + noise, index=index)
        values.iloc[-3:] = values.iloc[-3:] + 0.8
        effective = lq.align_monthly_to_weekly(values)
        friday_calendar = pd.date_range(effective.index.min(), effective.index.max(), freq="W-FRI")
        z_monthly = lq.rolling_zscore(effective, lq.ZSCORE_WINDOW_MONTHS, lq.ZSCORE_MIN_MONTHS)
        z_aligned = z_monthly.reindex(friday_calendar, method="ffill")
        # 月度 z 窗口未满的早期为空，窗口满后日历上不应再有空洞
        self.assertFalse(z_aligned.loc[z_monthly.first_valid_index():].isna().any())
        # 每个周五的 z 必须等于其生效月的月度 z
        for day in friday_calendar[friday_calendar >= z_monthly.first_valid_index()]:
            effective_day = z_monthly.index[z_monthly.index <= day][-1]
            self.assertAlmostEqual(z_aligned[day], z_monthly[effective_day], places=10)
        # 正常月间波动下，单月跳档的 z 在 4σ 左右属真实信号；
        # 若错按周五观测数滚动，同一路径会被放大到 8σ 以上
        self.assertLess(z_aligned.loc[z_monthly.first_valid_index():].abs().max(), 5.0)


class WeightAndSynthesisTests(unittest.TestCase):
    def test_metric_weights_sum_to_one(self):
        total = sum(float(spec["weight"]) for spec in lq.METRICS.values())
        self.assertAlmostEqual(total, 1.0)
        for group in ("short_term", "money"):
            group_total = sum(
                float(spec["weight"])
                for spec in lq.METRICS.values()
                if spec["group"] == group
            )
            self.assertAlmostEqual(group_total, 0.52 if group == "short_term" else 0.48)

    def test_compute_indices_without_social_financing(self):
        metrics = {
            "dr007": make_weekly_series(400, seed=1),
            "bond_10y": make_weekly_series(400, seed=2),
            "margin_balance_yoy": make_weekly_series(400, seed=3),
            "m1_m2_gap": make_monthly_series(48),
            "m2_yoy": make_monthly_series(48),
        }
        indices, components, notes = lq.compute_liquidity_indices(metrics)
        self.assertFalse(indices.empty)
        self.assertIn("社融存量同比", "".join(notes))
        self.assertIn("liquidity_index", indices.columns)
        self.assertIn("short_term_index", indices.columns)
        self.assertIn("money_index", indices.columns)
        short_term_rows = components[components["分组"].eq("短端资金价格指数")]
        self.assertEqual(len(short_term_rows), 3)
        money_rows = components[components["分组"].eq("货币总量指数")]
        self.assertEqual(len(money_rows), 3)
        manual_row = components[components["metric"].eq("social_financing_yoy")].iloc[0]
        self.assertEqual(manual_row["数据区间"], "无数据")

    def test_total_equals_weighted_children_when_complete(self):
        metrics = {
            "dr007": make_weekly_series(400, seed=1),
            "bond_10y": make_weekly_series(400, seed=2),
            "margin_balance_yoy": make_weekly_series(400, seed=3),
            "m1_m2_gap": make_monthly_series(48),
            "m2_yoy": make_monthly_series(48),
        }
        indices, _, _ = lq.compute_liquidity_indices(metrics)
        check = indices.dropna()
        self.assertFalse(check.empty)
        rebuilt = 0.52 * check["short_term_index"] + 0.48 * check["money_index"]
        self.assertTrue(np.allclose(rebuilt, check["liquidity_index"], atol=1e-6))

    def test_empty_metrics_returns_empty_frame(self):
        indices, components, notes = lq.compute_liquidity_indices({})
        self.assertTrue(indices.empty)
        self.assertEqual(len(components), len(lq.METRICS))
        self.assertTrue(notes)

    def test_summarize_index_trend(self):
        index = pd.date_range("2026-08-07", periods=6, freq="W-FRI")
        series = pd.Series([0.0, 0.1, 0.2, 0.3, 0.4, 0.6], index=index)
        summary = lq.summarize_index(series)
        self.assertEqual(summary["trend"], "较上期明显宽松化")
        self.assertTrue(np.isclose(summary["change"], 0.2))


class CacheTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._original_dir = ls.LIQUIDITY_DATA_DIR
        ls.LIQUIDITY_DATA_DIR = Path(self._tmp.name)

    def tearDown(self):
        ls.LIQUIDITY_DATA_DIR = self._original_dir
        self._tmp.cleanup()

    def test_save_and_load_roundtrip(self):
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-10-30", "2026-10-31"]),
                "value": [1.38, 1.40],
                "source_url": ["x"] * 2,
            }
        )
        count = ls.save_metric("dr007", frame, retrieved_at="2026-11-04")
        self.assertEqual(count, 2)
        loaded = ls.load_cached_metric("dr007")
        self.assertEqual(len(loaded), 2)
        self.assertTrue((loaded["retrieved_at"] == "2026-11-04").all())

    def test_save_merges_and_keeps_newest(self):
        first = pd.DataFrame(
            {"date": pd.to_datetime(["2026-10-30"]), "value": [1.38], "source_url": ["x"]}
        )
        ls.save_metric("dr007", first, retrieved_at="2026-11-01")
        second = pd.DataFrame(
            {"date": pd.to_datetime(["2026-10-30"]), "value": [1.40], "source_url": ["y"]}
        )
        ls.save_metric("dr007", second, retrieved_at="2026-11-04")
        loaded = ls.load_cached_metric("dr007")
        self.assertEqual(len(loaded), 1)
        self.assertAlmostEqual(loaded["value"].iloc[0], 1.40)
        self.assertEqual(loaded["retrieved_at"].iloc[0], "2026-11-04")


if __name__ == "__main__":
    unittest.main()
