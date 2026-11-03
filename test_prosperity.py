from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from nbs_provider import HISTORY_COLUMNS, import_history, load_history, merge_history, month_from_title
from prosperity import (
    compute_prosperity,
    corresponding_industries,
    top_companies_for_stat_industry,
    top_prosperity,
)
from prosperity_mapping import STAT_TO_SW, mapped_industry_ids, mapping_report


def build_history() -> pd.DataFrame:
    rows = []
    for month, profit_a, profit_b, value_added in (
        ("2026-07", 10.0, 1.0, 5.0),
        ("2026-08", 20.0, 2.0, 6.0),
        ("2026-09", 30.0, 3.0, 7.0),
    ):
        rows.append(
            {
                "month": month,
                "stat_industry": "计算机、通信和其他电子设备制造业",
                "industry_value_added_yoy": value_added,
                "profit_yoy": profit_a,
                "source_value_added": "va",
                "source_profit": "profit",
                "fetched_at": "2026-10-31",
            }
        )
        rows.append(
            {
                "month": month,
                "stat_industry": "金属制品、机械和设备修理业",
                "industry_value_added_yoy": None,
                "profit_yoy": profit_b,
                "source_value_added": "",
                "source_profit": "profit",
                "fetched_at": "2026-10-31",
            }
        )
    return pd.DataFrame(rows)[HISTORY_COLUMNS]


def build_ranking() -> pd.DataFrame:
    mapped = corresponding_industries("计算机、通信和其他电子设备制造业").head(2)
    first_id, second_id = mapped["industry_l3_id"].tolist()
    first_name, second_name = mapped["industry_l3_name"].tolist()
    return pd.DataFrame(
        {
            "industry_l3_id": [first_id] * 3 + [second_id] * 2,
            "industry_l3_name": [first_name] * 3 + [second_name] * 2,
            "company_id": ["1.SZ", "2.SZ", "3.SZ", "4.SH", "5.SH"],
            "company_name": ["甲", "乙", "丙", "丁", "戊"],
            "industry_rank": [3, 1, 2, 2, 1],
            "weighted_score": [60.0, 90.0, 75.0, 50.0, 80.0],
            "data_coverage": [0.9, 1.0, 0.95, 0.8, 0.9],
            "announcement_date": ["2026-10-20"] * 5,
        }
    )


class ProsperityTests(unittest.TestCase):
    def test_month_from_title(self) -> None:
        self.assertEqual(month_from_title("2026年1—9月份全国规模以上工业企业利润增长15.5%"), "2026-09")
        self.assertEqual(month_from_title("2026年9月份规模以上工业增加值增长5.5%"), "2026-09")
        self.assertEqual(month_from_title("2025年1-12月份全国规模以上工业企业利润增长0.6%"), "2025-12")

    def test_compute_prosperity_ranks_higher_growth_first(self) -> None:
        frame = compute_prosperity(build_history(), window=3)
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame.iloc[0]["stat_industry"], "计算机、通信和其他电子设备制造业")
        self.assertGreater(frame.iloc[0]["prosperity_score"], frame.iloc[1]["prosperity_score"])
        self.assertTrue(frame["prosperity_score"].between(0, 100).all())
        self.assertAlmostEqual(frame.iloc[0]["profit_yoy_latest"], 30.0)
        self.assertAlmostEqual(frame.iloc[0]["value_added_yoy_mean"], 6.0)

    def test_missing_value_added_is_neutral_and_noted(self) -> None:
        frame = compute_prosperity(build_history(), window=3)
        row = frame[frame["stat_industry"].eq("金属制品、机械和设备修理业")].iloc[0]
        self.assertTrue(pd.isna(row["value_added_yoy_mean"]))
        self.assertIn("工业增加值未公布", row["data_note"])

    def test_window_limits_months(self) -> None:
        history = build_history()
        history.loc[len(history)] = {
            "month": "2026-06",
            "stat_industry": "计算机、通信和其他电子设备制造业",
            "industry_value_added_yoy": 99.0,
            "profit_yoy": 99.0,
            "source_value_added": "va",
            "source_profit": "profit",
            "fetched_at": "2026-10-31",
        }
        frame = compute_prosperity(history, window=3)
        self.assertEqual(int(frame.iloc[0]["months"]), 3)
        self.assertAlmostEqual(frame.iloc[0]["profit_yoy_mean"], 20.0)

    def test_top_prosperity_skips_industries_without_sw_mapping(self) -> None:
        top = top_prosperity(build_history(), window=3, top_n=5)
        self.assertNotIn("金属制品、机械和设备修理业", top["stat_industry"].tolist())
        self.assertEqual(len(top), 1)

    def test_mapping_resolves_all_stat_industries(self) -> None:
        report = mapping_report()
        self.assertTrue((report["unresolved"].str.len() == 0).all())
        self.assertTrue((report["industry_count"] >= 1).all())
        self.assertGreaterEqual(len(mapped_industry_ids("计算机、通信和其他电子设备制造业")), 15)
        self.assertEqual(len(STAT_TO_SW), len(report))

    def test_corresponding_industries_returns_sw_level3(self) -> None:
        frame = corresponding_industries("计算机、通信和其他电子设备制造业")
        self.assertFalse(frame.empty)
        self.assertIn("半导体设备", frame["industry_l3_name"].tolist())
        self.assertTrue(frame["industry_l3_id"].is_unique)

    def test_top_companies_per_industry(self) -> None:
        ranking = build_ranking()
        picked = top_companies_for_stat_industry(ranking, "计算机、通信和其他电子设备制造业", per_industry=2)
        self.assertEqual(len(picked), 4)
        first_id = ranking["industry_l3_id"].iloc[0]
        first_industry = picked[picked["industry_l3_id"].eq(first_id)]
        self.assertEqual(first_industry["company_name"].tolist(), ["乙", "丙"])

    def test_top_companies_without_mapping_or_data(self) -> None:
        self.assertTrue(top_companies_for_stat_industry(build_ranking(), "不存在的统计行业").empty)
        self.assertTrue(top_companies_for_stat_industry(pd.DataFrame(), "计算机、通信和其他电子设备制造业").empty)

    def test_merge_and_import_history(self) -> None:
        history = build_history()
        newer = history[history["month"].eq("2026-09")].copy()
        newer.loc[:, "profit_yoy"] = 40.0
        merged = merge_history(history, newer)
        self.assertEqual(len(merged), len(history))
        self.assertAlmostEqual(
            float(merged[merged["month"].eq("2026-09") & merged["stat_industry"].str.startswith("计算机")]["profit_yoy"].iloc[0]),
            40.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nbs_monthly.csv"
            imported = import_history(
                pd.DataFrame(
                    {
                        "月份": ["2026年10月份", "2026年9月份"],
                        "统计行业": ["石油和天然气开采业", "石油和天然气开采业"],
                        "利润增长率": [5.0, 4.0],
                        "工业增加值增长率": [1.0, 2.0],
                    }
                ),
                path=path,
            )
            self.assertEqual(sorted(imported["month"].unique()), ["2026-09", "2026-10"])
            reloaded = load_history(path)
            self.assertEqual(len(reloaded), 2)
            self.assertEqual(reloaded["stat_industry"].unique().tolist(), ["石油和天然气开采业"])


if __name__ == "__main__":
    unittest.main()
