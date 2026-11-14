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

    def test_rolling_reference_band(self):
        series = make_weekly_series(300)
        reference = lq.rolling_reference(series, lq.REFERENCE_WINDOW_WEEKS, lq.REFERENCE_MIN_WEEKS)
        self.assertEqual(
            list(reference.columns),
            ["滚动均值", "区间上轨(90%分位)", "区间下轨(10%分位)"],
        )
        valid = reference.dropna()
        self.assertFalse(valid.empty)
        self.assertTrue((valid["区间下轨(10%分位)"] <= valid["滚动均值"]).all())
        self.assertTrue((valid["滚动均值"] <= valid["区间上轨(90%分位)"]).all())
        # 窗口未满时留空，第 min_periods 个观测起才有值
        self.assertEqual(reference.first_valid_index(), series.index[lq.REFERENCE_MIN_WEEKS - 1])

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


def make_source_metrics() -> dict[str, pd.Series]:
    """按缓存口径构造六项来源数据（两融为水平值，同比由 compute 派生）。"""
    levels = pd.Series(1.5e12 + np.sin(np.arange(400)) * 1e11, index=make_weekly_series(400).index)
    return {
        "dr007": make_weekly_series(400, seed=1),
        "bond_10y": make_weekly_series(400, seed=2),
        "margin_balance": levels,
        "m1_m2_gap": make_monthly_series(48),
        "m2_yoy": make_monthly_series(48),
    }


class WeightAndSynthesisTests(unittest.TestCase):
    def test_metric_weights_sum_to_one(self):
        total = sum(float(spec["weight"]) for spec in lq.METRICS.values())
        self.assertAlmostEqual(total, 1.0)
        group_totals = {
            group: sum(
                float(spec["weight"])
                for spec in lq.METRICS.values()
                if spec["group"] == group
            )
            for group in ("short_term", "money")
        }
        self.assertAlmostEqual(group_totals["short_term"], 0.41)
        self.assertAlmostEqual(group_totals["money"], 0.59)

    def test_compute_indices_without_social_financing(self):
        indices, components, notes = lq.compute_liquidity_indices(make_source_metrics())
        self.assertFalse(indices.empty)
        self.assertIn("社融存量同比", "".join(notes))
        self.assertIn("liquidity_index", indices.columns)
        self.assertIn("short_term_index", indices.columns)
        self.assertIn("money_index", indices.columns)
        short_term_rows = components[components["分组"].eq("短端资金价格指数")]
        self.assertEqual(len(short_term_rows), 3)
        money_rows = components[components["分组"].eq("信用货币增速指数")]
        self.assertEqual(len(money_rows), 4)
        manual_row = components[components["metric"].eq("social_financing_yoy")].iloc[0]
        self.assertEqual(manual_row["数据区间"], "无数据")
        # 两融余额同比由两融余额水平派生，自动参与货币组合成
        yoy_row = components[components["metric"].eq("margin_balance_yoy")].iloc[0]
        self.assertNotEqual(yoy_row["数据区间"], "无数据")

    def test_total_equals_weighted_children_when_complete(self):
        indices, _, _ = lq.compute_liquidity_indices(make_source_metrics())
        check = indices.dropna()
        self.assertFalse(check.empty)
        rebuilt = 0.41 * check["short_term_index"] + 0.59 * check["money_index"]
        self.assertTrue(np.allclose(rebuilt, check["liquidity_index"], atol=1e-6))

    def test_custom_weights_change_index_and_tables(self):
        metrics = make_source_metrics()
        base_indices, _, _ = lq.compute_liquidity_indices(metrics)
        custom = {metric_id: float(spec["weight"]) for metric_id, spec in lq.METRICS.items()}
        custom["dr007"] = 0.30
        custom["bond_10y"] = 0.10
        custom["margin_balance"] = 0.01
        custom["margin_balance_yoy"] = 0.25
        custom["m1_m2_gap"] = 0.05
        custom["m2_yoy"] = 0.20
        custom["social_financing_yoy"] = 0.09
        custom_indices, custom_components, _ = lq.compute_liquidity_indices(
            metrics, weights=custom
        )
        check = custom_indices.dropna()
        # 自定义权重下，组权重 = 组内自定义权重之和（本例仍为 0.41/0.59），组合式仍成立
        rebuilt = 0.41 * check["short_term_index"] + 0.59 * check["money_index"]
        self.assertTrue(np.allclose(rebuilt, check["liquidity_index"], atol=1e-6))
        # 指数数值确实随权重变化
        self.assertFalse(
            np.allclose(
                base_indices["liquidity_index"].dropna(),
                custom_indices["liquidity_index"].dropna(),
            )
        )
        # 成分表权重列反映自定义权重（无数据行同样生效）
        dr_row = custom_components[custom_components["metric"].eq("dr007")].iloc[0]
        self.assertEqual(dr_row["权重"], "30%")
        sf_row = custom_components[custom_components["metric"].eq("social_financing_yoy")].iloc[0]
        self.assertEqual(sf_row["权重"], "9%")
        # 权重缺省（None）时回退默认
        fallback = lq.effective_weight("dr007", {"dr007": None})
        self.assertAlmostEqual(fallback, float(lq.METRICS["dr007"]["weight"]))

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


class SocialFinancingParseTests(unittest.TestCase):
    """央行《社会融资规模存量统计表》解析。"""

    HTML = (
        "<table><tr><td>项目</td><td>&nbsp;</td><td>Items</td>"
        "<td>存量</td><td>增速（%）</td><td>存量</td><td>增速（%）</td></tr>"
        "<tr><td>社会融资规模存量</td><td>&nbsp;</td><td>AFRE(stock)</td>"
        "<td>449.11 </td><td>8.2 </td><td>451.40 </td><td>8.2 </td>"
        "<td></td><td></td></tr>"
        "<tr><td>人民币贷款</td><td>&nbsp;</td><td>RMB loans</td>"
        "<td>273.30 </td><td>6.1 </td><td>274.15 </td><td>6.1 </td></tr></table>"
    )

    def test_parse_keeps_only_stats_and_skips_unpublished_months(self):
        pairs = ls.parse_afre_stock_table(self.HTML)
        self.assertEqual(pairs, [(449.11, 8.2), (451.40, 8.2)])

    def test_parse_returns_empty_when_row_missing(self):
        self.assertEqual(ls.parse_afre_stock_table("<table></table>"), [])

    def test_decode_best_handles_both_encodings(self):
        text = "中国人民银行统计数据社会融资规模"
        self.assertEqual(ls._decode_best(text.encode("gb18030")), text)
        self.assertEqual(ls._decode_best(text.encode("utf-8")), text)


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
