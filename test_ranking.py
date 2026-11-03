from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from ranking import MODEL_SPECS, prepare_growth_metrics, score_snapshot, select_snapshot


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


if __name__ == "__main__":
    unittest.main()
