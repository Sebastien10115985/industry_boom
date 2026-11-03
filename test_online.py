from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from cninfo_provider import ReportRecord, report_period_from_title
from disclosure_pipeline import rank_online_financials
from financial_extract import extract_financial_metrics
from industry_catalog import freeze_catalog, load_catalog
from industry_match import (
    builtin_industry_frame,
    match_industry_name,
    normalize_industry_name,
    search_industries,
)
from public_industry_taxonomy import SW_L3_INDUSTRIES
from industry_catalog import freeze_catalog
from user_commands import parse_update_command


class OnlinePipelineTests(unittest.TestCase):
    def test_freeze_catalog_selects_market_cap_seeds(self) -> None:
        export = pd.DataFrame(
            {
                "三级行业ID": ["I1", "I1", "I1", "I2"],
                "三级行业名称": ["软件", "软件", "软件", "银行"],
                "证券代码": ["A.SZ", "B.SZ", "C.SZ", "D.SH"],
                "证券简称": ["A", "B", "C", "D"],
                "总市值": [100, 300, 200, 500],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, constituents = freeze_catalog(
                export,
                effective_date="2026-10-31",
                seed_count=2,
                catalog_path=root / "catalog.csv",
                constituents_path=root / "constituents.csv",
                metadata_path=root / "metadata.json",
            )
        self.assertEqual(len(catalog), 2)
        self.assertEqual(catalog.set_index("industry_l3_id").loc["I1", "model_type"], "growth")
        seeds = set(constituents.loc[constituents["is_seed"], "company_id"])
        self.assertEqual(seeds, {"B.SZ", "C.SZ", "D.SH"})

    def test_builtin_taxonomy_is_usable_without_wind(self) -> None:
        frame = builtin_industry_frame()
        self.assertGreaterEqual(len(frame), 300)
        self.assertEqual(frame["industry_l3_id"].is_unique, True)
        self.assertTrue(frame["industry_l3_name"].str.len().gt(0).all())
        self.assertEqual(frame["industry_l1_name"].nunique(), 31)
        self.assertEqual(len(SW_L3_INDUSTRIES), len(frame))
        catalog = load_catalog()
        self.assertFalse(catalog.empty)
        self.assertTrue(
            catalog["model_type"]
            .isin(
                {
                    "general",
                    "growth",
                    "quality",
                    "cyclical",
                    "scale",
                    "bank",
                    "securities",
                    "insurance",
                    "innovative_drug",
                }
            )
            .all()
        )

    def test_semantic_fuzzy_industry_matching(self) -> None:
        self.assertEqual(normalize_industry_name("种子(申万三级行业)"), "种子")
        exact = match_industry_name("种子")
        self.assertEqual(exact["industry_l3_name"], "种子")
        self.assertEqual(exact["match_score"], 1.0)
        self.assertIn(match_industry_name("白酒")["industry_l3_name"], {"白酒", "白酒Ⅲ"})
        self.assertEqual(match_industry_name("半导体设备")["industry_l3_name"], "半导体设备")
        unknown = match_industry_name("不存在的行业名称XYZ")
        self.assertEqual(unknown["match_method"], "unmatched")
        self.assertEqual(unknown["industry_l3_id"], "")

    def test_search_industries_always_returns_scores(self) -> None:
        keyword = search_industries("半导体", limit=10)
        self.assertIn("match_score", keyword.columns)
        self.assertGreaterEqual(float(keyword.iloc[0]["match_score"]), 0.6)
        self.assertTrue(keyword.iloc[0]["industry_l3_name"].startswith("半导体"))
        self.assertIn("match_score", search_industries("", limit=5).columns)

    def test_freeze_catalog_from_industry_names_only(self) -> None:
        export = pd.DataFrame(
            {
                "三级行业名称": ["种子", "种子", "生猪养殖"],
                "证券代码": ["A.SZ", "B.SZ", "C.SZ"],
                "证券简称": ["A", "B", "C"],
                "总市值": [10, 30, 20],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, constituents = freeze_catalog(
                export,
                effective_date="2026-10-31",
                seed_count=1,
                catalog_path=root / "catalog.csv",
                constituents_path=root / "constituents.csv",
                metadata_path=root / "metadata.json",
            )
        self.assertEqual(len(catalog), 2)
        self.assertTrue(catalog["industry_l3_id"].ne("").all())
        self.assertEqual(int(constituents["is_seed"].sum()), 2)

    def test_report_title_to_period(self) -> None:
        self.assertEqual(
            report_period_from_title("某公司2026年第三季度报告"),
            ("2026-09-30", "第三季度"),
        )
        self.assertIsNone(report_period_from_title("关于三季度业绩的提示性公告"))

    def test_extract_statement_metrics(self) -> None:
        report = ReportRecord(
            company_id="000001.SZ",
            company_name="示例公司",
            announcement_id="A1",
            announcement_date="2026-10-30",
            report_period="2026-09-30",
            report_type="第三季度",
            title="示例公司2026年第三季度报告",
            pdf_url="https://static.cninfo.com.cn/demo.pdf",
        )
        statement = """
合并年初到报告期末利润表
单位：元
一、营业总收入 1,200.00 1,000.00
其中：营业收入 1,200.00 1,000.00
其中：营业成本 720.00 650.00
归属于母公司所有者的净利润 120.00 100.00
"""
        with patch("financial_extract._extract_pages", return_value=[statement]):
            result = extract_financial_metrics(b"%PDF-demo", report)
        self.assertAlmostEqual(result["revenue"], 1200.0)
        self.assertAlmostEqual(result["net_profit"], 120.0)
        self.assertAlmostEqual(result["revenue_yoy"], 0.20)
        self.assertAlmostEqual(result["profit_yoy"], 0.20)
        self.assertAlmostEqual(result["gross_margin"], 0.40)
        self.assertFalse(result["needs_review"])

    def test_extract_skips_note_reference_column(self) -> None:
        """利润表科目后常跟附注列（如「七、61」），不能把它当金额。"""
        report = ReportRecord(
            company_id="603501.SH",
            company_name="示例集团",
            announcement_id="A2",
            announcement_date="2026-08-25",
            report_period="2026-06-30",
            report_type="半年报",
            title="示例集团2026年半年度报告",
            pdf_url="https://static.cninfo.com.cn/demo2.pdf",
        )
        statement = """
合并利润表
2026 年 1—6 月
单位：元 币种：人民币
项目 附注 2026 年半年度 2025 年半年度
一、营业总收入 14,024,560,916.08 13,955,815,139.72
其中：营业收入 七、61 14,024,560,916.08 13,955,815,139.72
二、营业总成本 12,197,987,377.26 11,686,967,104.49
其中：营业成本 七、61 9,988,691,327.59 9,702,120,251.97
归属于母公司所有者的净利润 1,219,762,603.08 2,027,226,387.11
"""
        with patch("financial_extract._extract_pages", return_value=[statement]):
            result = extract_financial_metrics(b"%PDF-demo", report)
        self.assertAlmostEqual(result["revenue"], 14_024_560_916.08)
        self.assertAlmostEqual(result["net_profit"], 1_219_762_603.08)
        self.assertAlmostEqual(result["gross_margin"], 0.287771, places=5)

    def test_gross_margin_is_not_derived_from_a_wrong_cost(self) -> None:
        """营业成本抽错成极小值时，毛利率宁可留空也不要给出接近 100% 的数。"""
        report = ReportRecord(
            company_id="000002.SZ",
            company_name="示例二",
            announcement_id="A3",
            announcement_date="2026-08-25",
            report_period="2026-06-30",
            report_type="半年报",
            title="示例二2026年半年度报告",
            pdf_url="https://static.cninfo.com.cn/demo3.pdf",
        )
        statement = """
合并利润表
单位：元
一、营业总收入 14,024,560,916.08 13,955,815,139.72
其中：营业成本 61 9,988,691,327.59
归属于母公司所有者的净利润 1,219,762,603.08 2,027,226,387.11
"""
        with patch("financial_extract._extract_pages", return_value=[statement]):
            result = extract_financial_metrics(b"%PDF-demo", report)
        self.assertTrue(np.isnan(result["gross_margin"]))
        self.assertLess(result["extraction_confidence"], 1.0)

    def test_parse_chinese_update_commands(self) -> None:
        seed = parse_update_command("更新知名公司 截止 2026-10-31")
        self.assertEqual(seed.scope, "seed")
        self.assertEqual(seed.as_of_date, "2026-10-31")
        company = parse_update_command("更新公司 600000.SH 000001.SZ", "2026-10-31")
        self.assertEqual(company.company_ids, ["600000.SH", "000001.SZ"])

    def test_financial_reports_fall_back_as_one_peer_group(self) -> None:
        data = pd.DataFrame(
            {
                "company_id": ["A.SH", "B.SH"],
                "company_name": ["A银行", "B银行"],
                "industry_l3_id": ["BANK", "BANK"],
                "industry_l3_name": ["银行", "银行"],
                "model_type": ["bank", "bank"],
                "report_period": ["2026-09-30", "2026-09-30"],
                "announcement_date": ["2026-10-20", "2026-10-21"],
                "revenue": [120.0, 100.0],
                "net_profit": [20.0, 10.0],
                "revenue_yoy": [0.20, 0.10],
                "profit_yoy": [0.30, 0.05],
            }
        )
        period, ranking = rank_online_financials(data, "2026-10-31")
        self.assertEqual(period, pd.Timestamp("2026-09-30"))
        self.assertEqual(set(ranking["rank_model"]), {"financial_report"})
        self.assertTrue(ranking["eligible"].all())
        self.assertTrue(ranking["model_fallback_reason"].str.contains("专业指标覆盖不足").all())


if __name__ == "__main__":
    unittest.main()
