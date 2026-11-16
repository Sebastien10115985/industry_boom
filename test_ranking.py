from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from ranking import (
    MODEL_SPECS,
    effective_model_metrics,
    prepare_growth_metrics,
    score_snapshot,
    select_snapshot,
)


class RankingTests(unittest.TestCase):
    def test_all_model_weights_sum_to_one(self) -> None:
        for model in MODEL_SPECS.values():
            self.assertAlmostEqual(sum(weight for weight, _ in model.metrics.values()), 1.0)

    def test_general_ranking_and_watchlist(self) -> None:
        frame = pd.DataFrame(
            {
                "company_id": ["A", "B", "C"],
                "industry_l3_id": ["I1", "I1", "I1"],
                "model_type": ["general"] * 3,
                "revenue_yoy": [0.30, 0.20, 0.10],
                "profit_growth_adjusted": [0.30, 0.20, 0.10],
                "gross_margin": [0.50, 0.40, 0.30],
                "market_cap": [300, 200, 100],
                "revenue": [300, 200, 100],
                "net_profit": [30, 20, 10],
            }
        )
        result = score_snapshot(frame, top_n=2)
        ranked = result.set_index("company_id")
        self.assertEqual(ranked.loc["A", "industry_rank"], 1)
        self.assertEqual(ranked.loc["C", "industry_rank"], 3)
        self.assertTrue(ranked.loc["A", "in_watchlist"])
        self.assertFalse(ranked.loc["C", "in_watchlist"])

    def test_snapshot_excludes_future_announcement(self) -> None:
        frame = pd.DataFrame(
            {
                "company_id": ["A", "B"],
                "industry_l3_id": ["I1", "I1"],
                "report_period": ["2026-09-30", "2026-09-30"],
                "announcement_date": ["2026-10-20", "2026-11-01"],
            }
        )
        snapshot = select_snapshot(frame, "2026-10-31", "2026-09-30")
        self.assertEqual(snapshot["company_id"].tolist(), ["A"])

    def test_loss_to_profit_uses_symmetric_growth(self) -> None:
        frame = pd.DataFrame(
            {
                "company_id": ["A", "A"],
                "report_period": ["2025-09-30", "2026-09-30"],
                "revenue": [100.0, 120.0],
                "net_profit": [-10.0, 10.0],
                "revenue_yoy": [np.nan, np.nan],
                "profit_yoy": [np.nan, np.nan],
            }
        )
        prepared = prepare_growth_metrics(frame)
        current = prepared.loc[prepared["report_period"].eq(pd.Timestamp("2026-09-30"))].iloc[0]
        self.assertAlmostEqual(current["revenue_yoy"], 0.20)
        self.assertAlmostEqual(current["profit_growth_adjusted"], 2.0)

    def test_symmetric_growth_column_always_uses_symmetric_formula(self) -> None:
        """上年同期为正时，普通同比与对称增长率不同，创新药模型取后者。"""
        frame = pd.DataFrame(
            {
                "company_id": ["A", "A"],
                "report_period": ["2025-09-30", "2026-09-30"],
                "revenue": [100.0, 100.0],
                "net_profit": [100.0, 120.0],
                "revenue_yoy": [np.nan, np.nan],
                "profit_yoy": [np.nan, np.nan],
            }
        )
        prepared = prepare_growth_metrics(frame)
        current = prepared.loc[prepared["report_period"].eq(pd.Timestamp("2026-09-30"))].iloc[0]
        self.assertAlmostEqual(current["profit_growth_adjusted"], 0.20)
        # 2 * (120 - 100) / (120 + 100)
        self.assertAlmostEqual(current["profit_growth_symmetric"], 2 * 20 / 220)

    def test_innovative_drug_model_uses_symmetric_growth(self) -> None:
        rows = []
        for code, profit in (("A", 120.0), ("B", 60.0)):
            rows.append(
                {
                    "company_id": code,
                    "company_name": code,
                    "industry_l3_id": "851512",
                    "industry_l3_name": "化学制剂",
                    "model_type": "innovative_drug",
                    "report_period": "2026-09-30",
                    "announcement_date": "2026-10-25",
                    "market_cap": 1.0e10,
                    "revenue": 100.0,
                    "net_profit": profit,
                    "revenue_yoy": 0.10,
                    "profit_growth_adjusted": 5.0,
                    "profit_growth_symmetric": [0.18, 0.09][0 if code == "A" else 1],
                    "gross_margin": 0.30,
                }
            )
        ranking = score_snapshot(pd.DataFrame(rows), top_n=2)
        self.assertEqual(ranking["rank_model"].unique().tolist(), ["innovative_drug"])
        # 利润增长率被对称增长率替换：得分应与对称值同序
        self.assertEqual(ranking.sort_values("industry_rank")["company_id"].tolist(), ["A", "B"])

    def test_growth_excludes_future_revision(self) -> None:
        frame = pd.DataFrame(
            {
                "company_id": ["A", "A", "A"],
                "report_period": ["2025-09-30", "2025-09-30", "2026-09-30"],
                "announcement_date": ["2025-10-20", "2026-12-01", "2026-10-20"],
                "revenue": [100.0, 80.0, 120.0],
                "net_profit": [10.0, 8.0, 12.0],
                "revenue_yoy": [np.nan, np.nan, np.nan],
                "profit_yoy": [np.nan, np.nan, np.nan],
            }
        )
        prepared = prepare_growth_metrics(frame, "2026-10-31")
        current = prepared.loc[prepared["report_period"].eq(pd.Timestamp("2026-09-30"))].iloc[0]
        self.assertAlmostEqual(current["revenue_yoy"], 0.20)
        self.assertAlmostEqual(current["profit_growth_adjusted"], 0.20)
        self.assertEqual(len(prepared), 2)

    def test_custom_weights_change_ranking(self) -> None:
        """自定义权重：营收增长权重拉满时名次由营收决定，净利润权重拉满时由净利润决定。"""
        rows = []
        growth = [100.0, 75.0, 50.0, 1.0]
        profit = [1.0, 25.0, 50.0, 100.0]
        for index in range(4):
            rows.append(
                {
                    "company_id": f"C{index}",
                    "industry_l3_id": "X",
                    "model_type": "general",
                    "revenue_yoy": growth[index],
                    "profit_growth_adjusted": 50.0,
                    "gross_margin": 50.0,
                    "market_cap": 100.0,
                    "revenue": 500.0,
                    "net_profit": profit[index],
                }
            )
        snapshot = pd.DataFrame(rows)
        base = score_snapshot(snapshot, top_n=4)
        tiny = 0.02
        growth_heavy = score_snapshot(
            snapshot,
            top_n=4,
            weights={
                "general": {
                    "revenue_yoy": 0.90,
                    "profit_growth_adjusted": tiny,
                    "gross_margin": tiny,
                    "market_cap": tiny,
                    "revenue": tiny,
                    "net_profit": tiny,
                }
            },
        )
        profit_heavy = score_snapshot(
            snapshot,
            top_n=4,
            weights={
                "general": {
                    "revenue_yoy": tiny,
                    "profit_growth_adjusted": tiny,
                    "gross_margin": tiny,
                    "market_cap": tiny,
                    "revenue": tiny,
                    "net_profit": 0.90,
                }
            },
        )
        self.assertEqual(growth_heavy.sort_values("industry_rank").iloc[0]["company_id"], "C0")
        self.assertEqual(profit_heavy.sort_values("industry_rank").iloc[0]["company_id"], "C3")
        # 名次序列是按公司排的，所以要比较排序后的公司顺序而不是 rank 数值
        base_order = base.sort_values("industry_rank")["company_id"].tolist()
        growth_order = growth_heavy.sort_values("industry_rank")["company_id"].tolist()
        self.assertNotEqual(base_order, growth_order)

    def test_custom_weights_are_normalized(self) -> None:
        """自定义权重自动归一到 1；全零时回退默认权重。"""
        metrics = effective_model_metrics(
            "general", {"general": {"revenue_yoy": 0.60, "net_profit": 0.40}}
        )
        self.assertAlmostEqual(sum(weight for weight, _ in metrics.values()), 1.0, places=9)
        fallback = effective_model_metrics(
            "general",
            {"general": {metric: 0.0 for metric in MODEL_SPECS["general"].metrics}},
        )
        self.assertAlmostEqual(
            sum(weight for weight, _ in fallback.values()), 1.0, places=9
        )
        self.assertGreater(fallback["gross_margin"][0], 0.0)


if __name__ == "__main__":
    unittest.main()
