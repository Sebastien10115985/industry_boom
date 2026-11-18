from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from nbs_provider import (
    HISTORY_COLUMNS,
    import_history,
    load_history,
    merge_history,
    month_from_title,
    parse_profit_revenue,
)
from prosperity import PRIMARY_STAT_INDUSTRIES
from prosperity import (
    compute_prosperity,
    prosperity_history,
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
    frame = pd.DataFrame(rows)
    for column in HISTORY_COLUMNS:
        if column not in frame:
            frame[column] = None
    return frame[HISTORY_COLUMNS]


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
        self.assertEqual(
            month_from_title("2025年全国规模以上工业企业利润增长0.6%"), "2025-12"
        )
        self.assertEqual(
            month_from_title("2024年全国规模以上工业企业利润下降3.3%"), "2024-12"
        )

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


class ProsperityHistoryTests(unittest.TestCase):
    """逐月回看的历史景气度：每月用截至当月的窗口数据横截面计算。"""

    def test_points_grow_monthly_and_scores_in_range(self):
        history = build_history()
        result = prosperity_history(history, window=2, points=3)
        # 首月只有 1 个月数据（低于 min_months=2）被跳过，后续月份逐月累计
        self.assertEqual(sorted(result["month"].unique()), ["2026-08", "2026-09"])
        self.assertTrue(result["prosperity_score"].between(0, 100).all())
        self.assertTrue(set(result["stat_industry"]) <= set(history["stat_industry"]))

    def test_score_series_changes_over_months(self):
        history = build_history()
        result = prosperity_history(history, window=2, points=3)
        picked = result[result["stat_industry"].eq("计算机、通信和其他电子设备制造业")]
        self.assertGreaterEqual(len(picked), 2)
        # 行业 A 利润增速每月抬升，其横截面分位也应上升
        scores = picked.sort_values("month")["prosperity_score"].tolist()
        self.assertGreaterEqual(scores[-1], scores[0])

    def test_empty_history_returns_empty_frame(self):
        result = prosperity_history(pd.DataFrame(), window=2, points=3)
        self.assertTrue(result.empty)


class PrimaryStatIndustriesTests(unittest.TestCase):
    """景气度仅跟踪 25 个主要行业（20 个统计局主要行业 + 5 个补充行业）。"""

    def test_exactly_25_unique_industries(self):
        self.assertEqual(len(PRIMARY_STAT_INDUSTRIES), 25)
        self.assertEqual(len(set(PRIMARY_STAT_INDUSTRIES)), 25)

    def test_contains_supplementary_high_revenue_industries(self):
        for name in ("烟草制品业", "造纸和纸制品业", "化学纤维制造业", "石油、煤炭及其他燃料加工业", "燃气生产和供应业"):
            self.assertIn(name, PRIMARY_STAT_INDUSTRIES)



class ParseProfitRevenueTests(unittest.TestCase):
    """利润附件应能提取各行业大类的营业收入绝对值（亿元）。"""

    @staticmethod
    def _workbook():
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["2026年1—9月份规模以上工业企业主要财务指标（分行业）"])
        sheet.append(["行业", None, "营业收入", None, "利润总额", None])
        sheet.append([None, None, "绝对值", "同比增长", "绝对值", "同比增长"])
        sheet.append(["煤炭开采和洗选业", None, 12345.6, -3.3, 4567.8, -10.2])
        sheet.append(["汽车制造业", None, 65432.1, 12.5, 2345.6, 8.9])
        sheet.append(["其中：国有控股企业", None, 99999.0, 1.0, 111.0, 2.0])
        sheet.append(["注：数据为累计值", None, None, None, None, None])
        return workbook

    def test_extracts_revenue_by_industry(self):
        values = parse_profit_revenue(self._workbook())
        self.assertEqual(values["煤炭开采和洗选业"], 12345.6)
        self.assertEqual(values["汽车制造业"], 65432.1)

    def test_skips_aggregate_and_note_rows(self):
        values = parse_profit_revenue(self._workbook())
        self.assertEqual(len(values), 2)

    def test_raises_without_revenue_column(self):
        from openpyxl import Workbook
        from nbs_provider import NbsError

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["无关表"])
        sheet.append(["行业", "利润总额"])
        sheet.append(["煤炭开采和洗选业", 1.0])
        with self.assertRaises(NbsError):
            parse_profit_revenue(workbook)


class ParseProfitWorkbookTests(unittest.TestCase):
    """利润同比解析需兼容月度（同比增长）与全年（比上年增长）附件表头。"""

    @staticmethod
    def _workbook(sub_header_word: str) -> object:
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["2026年1—9月份规模以上工业企业主要财务指标（分行业）"])
        sheet.append(["行业", None, "营业收入", None, "利润总额", None])
        sheet.append([None, None, "绝对值", "同比增长", "绝对值", f"{sub_header_word}"])
        sheet.append(["煤炭开采和洗选业", None, 12345.6, -3.3, 4567.8, -10.2])
        return workbook

    def test_monthly_sub_header(self):
        from nbs_provider import parse_profit_workbook

        values = parse_profit_workbook(self._workbook("同比增长"))
        self.assertEqual(values["煤炭开采和洗选业"], -10.2)

    def test_full_year_sub_header(self):
        from nbs_provider import parse_profit_workbook

        values = parse_profit_workbook(self._workbook("比上年增长"))
        self.assertEqual(values["煤炭开采和洗选业"], -10.2)
