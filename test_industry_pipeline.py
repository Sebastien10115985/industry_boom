from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from cninfo_provider import ReportRecord
from market_cap_provider import attach_market_cap, save_market_caps, sync_market_caps
from industry_pipeline import (
    AI_FOCUS_INDUSTRIES,
    available_report_periods,
    check_industry_cache,
    focus_industries,
    load_all_industry_financials,
    load_industry_financials,
    load_search_log,
    rank_industry_financials,
    record_search,
    resolve_industry,
    save_industry_financials,
    synced_industries,
    sync_industry_reports,
)

CONSTITUENTS = pd.DataFrame(
    {
        "industry_l3_id": ["850814", "850814"],
        "industry_l3_name": ["数字芯片设计", "数字芯片设计"],
        "company_id": ["A1.SZ", "B2.SH"],
        "company_name": ["甲公司", "乙公司"],
        "include_date": ["2020-01-01", "2021-01-01"],
    }
)


def _cache_rows(rows: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "company_id": [row[0] for row in rows],
            "company_name": [row[1] for row in rows],
            "report_period": [row[2] for row in rows],
            "announcement_date": [row[3] for row in rows],
            "announcement_id": [f"{row[0]}-{row[2]}" for row in rows],
        }
    )


class SpyCninfo:
    """记录被检索的公司，用于验证只补拉缺的公司。"""

    def __init__(self, records: list[ReportRecord]) -> None:
        self._inner = FakeCninfo(records)
        self.calls: list[str] = []

    def search_periodic_reports(self, company_id, start_date, end_date):
        self.calls.append(company_id)
        return self._inner.search_periodic_reports(company_id, start_date, end_date)

    def download_pdf(self, url: str) -> bytes:
        return self._inner.download_pdf(url)

def _statement(revenue: str, prior_revenue: str, cost: str, prior_cost: str, profit: str) -> str:
    return f"""
合并利润表
单位：元
项目 本期 上期
一、营业总收入 {revenue} {prior_revenue}
其中：营业收入 {revenue} {prior_revenue}
二、营业总成本 900.00 800.00
其中：营业成本 {cost} {prior_cost}
归属于母公司所有者的净利润 {profit} 100.00
"""


SMALL_STATEMENT = _statement("1,200.00", "1,000.00", "720.00", "650.00", "120.00")
LARGE_STATEMENT = _statement("2,400.00", "2,000.00", "1,200.00", "1,300.00", "240.00")


def _report(company_id: str, company_name: str, period: str, announced: str) -> ReportRecord:
    return ReportRecord(
        company_id=company_id,
        company_name=company_name,
        announcement_id=f"{company_id}-{period}",
        announcement_date=announced,
        report_period=period,
        report_type="第三季度",
        title=f"{company_name}{period}报告",
        pdf_url=f"https://static.cninfo.com.cn/{company_id}-{period}.PDF",
    )


def _fake_pages(content: bytes, maximum_pages: int | None = None) -> list[str]:
    return [LARGE_STATEMENT if content == b"%PDF-large" else SMALL_STATEMENT]


class FakeCninfo:
    """离线替身：只返回预置公告，避免测试联网。"""

    def __init__(self, records: list[ReportRecord]) -> None:
        self._records = records

    def search_periodic_reports(self, company_id, start_date, end_date):
        return [item for item in self._records if item.company_id == company_id]

    def download_pdf(self, url: str) -> bytes:
        return b"%PDF-large" if "A1.SZ" in url else b"%PDF-small"


def _attach(frame: pd.DataFrame, caps: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    lookup = caps.set_index("company_id")["market_cap"]
    if "market_cap" not in result:
        result["market_cap"] = pd.NA
    result["market_cap"] = pd.to_numeric(result["market_cap"], errors="coerce").fillna(
        result["company_id"].astype(str).str.upper().map(lookup)
    )
    return result


class MarketCapCacheTests(unittest.TestCase):
    def _frame(self, rows: list[tuple[str, float, str]]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "company_id": [row[0] for row in rows],
                "company_name": ["名称"] * len(rows),
                "market_cap": [row[1] for row in rows],
                "price": [10.0] * len(rows),
                "quote_date": [row[2] for row in rows],
                "source": ["test"] * len(rows),
            }
        )

    def test_fresh_cache_is_reused_and_only_missing_codes_are_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "caps.csv"
            save_market_caps(
                self._frame([("A1.SZ", 1.0e10, date.today().isoformat())]), path
            )
            fetched = self._frame([("B2.SH", 2.0e10, date.today().isoformat())])
            with patch("market_cap_provider.fetch_market_caps", return_value=fetched) as mock:
                result = sync_market_caps(["A1.SZ", "B2.SH"], path=path)
            self.assertEqual(mock.call_args[0][0], ["B2.SH"])
            self.assertEqual(sorted(result["company_id"]), ["A1.SZ", "B2.SH"])
            reloaded = pd.read_csv(path, dtype={"company_id": str}, encoding="utf-8-sig")
            self.assertEqual(len(reloaded), 2)

    def test_stale_cache_is_refetched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "caps.csv"
            save_market_caps(self._frame([("A1.SZ", 1.0e10, "2020-01-01")]), path)
            fetched = self._frame([("A1.SZ", 3.0e10, date.today().isoformat())])
            with patch("market_cap_provider.fetch_market_caps", return_value=fetched) as mock:
                result = sync_market_caps(["A1.SZ"], path=path)
            self.assertEqual(mock.call_args[0][0], ["A1.SZ"])
            self.assertAlmostEqual(float(result["market_cap"].iloc[0]), 3.0e10)

    def test_attach_market_cap_keeps_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "caps.csv"
            save_market_caps(self._frame([("A1.SZ", 1.0e10, date.today().isoformat())]), path)
            frame = pd.DataFrame(
                {
                    "company_id": ["A1.SZ", "B2.SH", "C3.SH"],
                    "market_cap": [pd.NA, pd.NA, 5.0e10],
                }
            )
            result = attach_market_cap(frame, path)
            self.assertAlmostEqual(result["market_cap"].iloc[0], 1.0e10)
            self.assertTrue(pd.isna(result["market_cap"].iloc[1]))
            self.assertAlmostEqual(result["market_cap"].iloc[2], 5.0e10)


class IndustryPipelineTests(unittest.TestCase):
    def test_resolve_industry_by_id_and_name(self) -> None:
        by_name = resolve_industry("数字芯片设计")
        self.assertIsNotNone(by_name)
        self.assertEqual(by_name["industry_l3_id"], "850814")
        self.assertEqual(by_name["industry_l2_name"], "半导体")
        by_id = resolve_industry("850814")
        self.assertEqual(by_id["industry_l3_name"], "数字芯片设计")
        self.assertIsNone(resolve_industry("不存在的行业名称XYZ"))
        self.assertIsNone(resolve_industry(""))

    def test_sync_all_constituents_and_rank_inside_industry(self) -> None:
        reports = [
            _report("A1.SZ", "甲公司", "2026-06-30", "2026-08-20"),
            _report("A1.SZ", "甲公司", "2026-09-30", "2026-10-25"),
            _report("B2.SH", "乙公司", "2026-09-30", "2026-10-26"),
        ]
        constituents = pd.DataFrame(
            {
                "industry_l3_id": ["850814", "850814"],
                "industry_l3_name": ["数字芯片设计", "数字芯片设计"],
                "company_id": ["A1.SZ", "B2.SH"],
                "company_name": ["甲公司", "乙公司"],
                "include_date": ["2020-01-01", "2021-01-01"],
            }
        )
        caps = pd.DataFrame(
            {"company_id": ["A1.SZ", "B2.SH"], "market_cap": [3.0e10, 1.0e10]}
        )
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch("industry_pipeline.constituents_for", return_value=constituents), patch(
                "industry_pipeline.sync_market_caps", return_value=caps
            ), patch(
                "industry_pipeline.attach_market_cap", side_effect=lambda frame, path=None: _attach(frame, caps)
            ), patch(
                "financial_extract._extract_pages", side_effect=_fake_pages
            ):
                data, errors = sync_industry_reports(
                    industry="数字芯片设计",
                    as_of_date="2026-11-02",
                    lookback_days=365,
                    client=FakeCninfo(reports),
                    directory=data_dir,
                )
            self.assertEqual(len(data), 3)
            self.assertTrue(errors.empty)
            self.assertTrue(data["model_type"].eq("growth").all())
            self.assertEqual(data.loc[data["company_id"].eq("A1.SZ"), "market_cap"].iloc[0], 3.0e10)
            stored = load_industry_financials("850814", data_dir)
            self.assertEqual(len(stored), 3)
            self.assertTrue((data_dir / "850814.csv").exists())

            # 同一报告期口径：9 月 30 日只保留两家公司，2026 半年报不进入排名
            ranking, notes = rank_industry_financials(
                data, "2026-11-02", top_n=20, auto_period=False
            )
            self.assertEqual(notes, [])
            self.assertEqual(sorted(ranking["company_id"].tolist()), ["A1.SZ", "B2.SH"])
            periods = pd.to_datetime(ranking["report_period"], errors="coerce")
            self.assertTrue(periods.eq(pd.Timestamp("2026-09-30")).all())

            ranked = ranking.set_index("company_id")
            self.assertEqual(ranked.loc["A1.SZ", "industry_rank"], 1)
            self.assertEqual(ranked.loc["B2.SH", "industry_rank"], 2)
            self.assertAlmostEqual(ranked.loc["A1.SZ", "gross_margin"], 0.5)
            self.assertAlmostEqual(ranked.loc["B2.SH", "gross_margin"], 0.4)
            self.assertAlmostEqual(ranked.loc["A1.SZ", "revenue_yoy"], 0.2)
            self.assertTrue(ranking["profit_growth_adjusted"].notna().all())
            self.assertTrue(ranking["in_watchlist"].all())

    def test_cache_status_when_nothing_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS):
                status = check_industry_cache("850814", "2026-11-02", 365, directory)
        self.assertFalse(status.has_cache)
        self.assertFalse(status.is_fresh)
        self.assertEqual(status.total, 2)
        self.assertEqual(status.missing_ids, ("A1.SZ", "B2.SH"))
        self.assertIn("尚无财报缓存", status.message)

    def test_cache_status_fresh_when_all_covered(self) -> None:
        rows = _cache_rows(
            [
                ("A1.SZ", "甲公司", "2026-09-30", "2026-10-30"),
                ("B2.SH", "乙公司", "2026-09-30", "2026-10-28"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            save_industry_financials("850814", rows, directory)
            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS):
                status = check_industry_cache("850814", "2026-11-02", 365, directory)
        self.assertTrue(status.is_fresh)
        self.assertEqual(status.covered, 2)
        self.assertEqual(status.missing_ids, ())
        self.assertEqual(status.latest_period, pd.Timestamp("2026-09-30"))
        self.assertIn("本地已是最新", status.message)

    def test_cache_status_reports_missing_companies(self) -> None:
        rows = _cache_rows([("A1.SZ", "甲公司", "2026-09-30", "2026-10-30")])
        with tempfile.TemporaryDirectory() as directory:
            save_industry_financials("850814", rows, directory)
            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS):
                status = check_industry_cache("850814", "2026-11-02", 365, directory)
        self.assertFalse(status.is_fresh)
        self.assertEqual(status.covered, 1)
        self.assertEqual(status.missing_ids, ("B2.SH",))
        self.assertEqual(status.missing_names, ("乙公司",))
        self.assertIn("乙公司", status.message)

    def test_cache_status_ignores_announcements_after_as_of(self) -> None:
        rows = _cache_rows(
            [
                ("A1.SZ", "甲公司", "2026-09-30", "2026-12-01"),
                ("B2.SH", "乙公司", "2026-09-30", "2026-12-05"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            save_industry_financials("850814", rows, directory)
            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS):
                status = check_industry_cache("850814", "2026-11-02", 365, directory)
        self.assertTrue(status.has_cache)
        self.assertFalse(status.is_fresh)
        self.assertEqual(status.missing_ids, ("A1.SZ", "B2.SH"))

    def test_only_missing_skips_network_when_fresh(self) -> None:
        rows = _cache_rows(
            [
                ("A1.SZ", "甲公司", "2026-09-30", "2026-10-30"),
                ("B2.SH", "乙公司", "2026-09-30", "2026-10-28"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            save_industry_financials("850814", rows, directory)
            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS), patch(
                "industry_pipeline.sync_market_caps"
            ) as caps_mock, patch("industry_pipeline.CninfoClient") as client_mock:
                data, errors = sync_industry_reports(
                    "850814", "2026-11-02", only_missing=True, directory=directory
                )
        self.assertEqual(len(data), 2)
        self.assertTrue(errors.empty)
        caps_mock.assert_not_called()
        client_mock.assert_not_called()

    def test_only_missing_fetches_only_the_gaps(self) -> None:
        rows = _cache_rows([("A1.SZ", "甲公司", "2026-09-30", "2026-10-30")])
        reports = [_report("B2.SH", "乙公司", "2026-09-30", "2026-10-26")]
        caps = pd.DataFrame({"company_id": ["A1.SZ", "B2.SH"], "market_cap": [3.0e10, 1.0e10]})
        with tempfile.TemporaryDirectory() as directory:
            save_industry_financials("850814", rows, directory)
            spy = SpyCninfo(reports)
            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS), patch(
                "industry_pipeline.sync_market_caps", return_value=caps
            ), patch(
                "industry_pipeline.attach_market_cap",
                side_effect=lambda frame, path=None: _attach(frame, caps),
            ), patch(
                "financial_extract._extract_pages", side_effect=_fake_pages
            ):
                data, errors = sync_industry_reports(
                    "850814", "2026-11-02", client=spy, directory=directory, only_missing=True
                )
        self.assertEqual(spy.calls, ["B2.SH"])
        self.assertTrue(errors.empty)
        self.assertEqual(sorted(data["company_id"]), ["A1.SZ", "B2.SH"])
        self.assertEqual(len(data), 2)

    def test_focus_industries_keep_defined_order(self) -> None:
        focus = focus_industries()
        ids = [item["industry_l3_id"] for item in focus]
        self.assertEqual(ids, list(AI_FOCUS_INDUSTRIES))
        for item in focus:
            self.assertTrue(item["industry_l3_name"])
            self.assertNotEqual(item["industry_l3_name"], item["industry_l3_id"])

    def test_search_cooldown_avoids_repeated_lookup(self) -> None:
        # 日期相对系统今天动态构造，避免测试随日历流逝失效
        today = date.today()
        rows = _cache_rows(
            [
                (
                    "A1.SZ",
                    "甲公司",
                    str(today - timedelta(days=33)),
                    str(today - timedelta(days=3)),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            save_industry_financials("850814", rows, directory)
            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS):
                before = check_industry_cache(
                    "850814", today, 365, directory, today=today
                )
            self.assertTrue(before.needs_network)
            self.assertEqual(before.pending_ids, ("B2.SH",))

            # 刚核对过但公开渠道没有该报告期公告：冷却期内不再联网
            record_search("850814", "B2.SH", today, False, directory, today)
            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS):
                after = check_industry_cache(
                    "850814", today, 365, directory, today=today
                )
            self.assertFalse(after.needs_network)
            self.assertEqual(after.pending_ids, ())
            self.assertEqual(after.recently_checked, ("B2.SH",))
            self.assertIn("冷却期", after.message)

            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS), patch(
                "industry_pipeline.sync_market_caps"
            ) as caps_mock, patch("industry_pipeline.CninfoClient") as client_mock:
                sync_industry_reports(
                    "850814", today, only_missing=True, directory=directory
                )
            caps_mock.assert_not_called()
            client_mock.assert_not_called()

    def test_cooldown_expires_and_as_of_change_forces_lookup(self) -> None:
        today = date.today()
        rows = _cache_rows(
            [
                (
                    "A1.SZ",
                    "甲公司",
                    str(today - timedelta(days=33)),
                    str(today - timedelta(days=3)),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            save_industry_financials("850814", rows, directory)
            record_search("850814", "B2.SH", today, False, directory, today - timedelta(days=30))
            with patch("industry_pipeline.constituents_for", return_value=CONSTITUENTS):
                expired = check_industry_cache(
                    "850814", today, 365, directory, today=today
                )
                moved = check_industry_cache(
                    "850814", today + timedelta(days=59), 365, directory, today=today
                )
            self.assertTrue(expired.needs_network)
            self.assertEqual(expired.pending_ids, ("B2.SH",))
            self.assertTrue(moved.needs_network)

    def test_sync_records_search_log(self) -> None:
        reports = [_report("A1.SZ", "甲公司", "2026-09-30", "2026-10-25")]
        constituents = CONSTITUENTS[CONSTITUENTS["company_id"].eq("A1.SZ")]
        caps = pd.DataFrame({"company_id": ["A1.SZ"], "market_cap": [1.0e10]})
        with tempfile.TemporaryDirectory() as directory:
            with patch("industry_pipeline.constituents_for", return_value=constituents), patch(
                "industry_pipeline.sync_market_caps", return_value=caps
            ), patch(
                "industry_pipeline.attach_market_cap",
                side_effect=lambda frame, path=None: _attach(frame, caps),
            ), patch(
                "financial_extract._extract_pages", side_effect=_fake_pages
            ):
                sync_industry_reports(
                    "850814", "2026-11-02", client=FakeCninfo(reports), directory=directory
                )
            log = load_search_log(directory)
        self.assertEqual(len(log), 1)
        self.assertEqual(log["company_id"].iloc[0], "A1.SZ")
        self.assertTrue(bool(log["found"].iloc[0]))

    def test_incremental_sync_skips_known_announcements(self) -> None:
        reports = [_report("A1.SZ", "甲公司", "2026-09-30", "2026-10-25")]
        constituents = pd.DataFrame(
            {
                "industry_l3_id": ["850814"],
                "industry_l3_name": ["数字芯片设计"],
                "company_id": ["A1.SZ"],
                "company_name": ["甲公司"],
                "include_date": ["2020-01-01"],
            }
        )
        caps = pd.DataFrame({"company_id": ["A1.SZ"], "market_cap": [1.0e10]})
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            patches = (
                patch("industry_pipeline.constituents_for", return_value=constituents),
                patch("industry_pipeline.sync_market_caps", return_value=caps),
                patch(
                    "industry_pipeline.attach_market_cap",
                    side_effect=lambda frame, path=None: _attach(frame, caps),
                ),
                patch("financial_extract._extract_pages", side_effect=_fake_pages),
            )
            for item in patches:
                item.start()
            try:
                first, _ = sync_industry_reports(
                    "850814", "2026-11-02", client=FakeCninfo(reports), directory=data_dir
                )
                second, _ = sync_industry_reports(
                    "850814", "2026-11-02", client=FakeCninfo(reports), directory=data_dir
                )
            finally:
                for item in patches:
                    item.stop()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)

    def test_rank_groups_each_industry_separately(self) -> None:
        rows = []
        for industry_id, name in (("850814", "数字芯片设计"), ("859611", "油气开采Ⅲ")):
            for score, (company_id, cap) in enumerate(
                [("A1.SZ", 3.0e10), ("B2.SH", 2.0e10), ("C3.SH", 1.0e10)]
            ):
                rows.append(
                    {
                        "company_id": company_id,
                        "company_name": company_id,
                        "industry_l3_id": industry_id,
                        "industry_l3_name": name,
                        "model_type": "general" if industry_id == "859611" else "growth",
                        "report_period": "2026-09-30",
                        "announcement_date": "2026-10-25",
                        "market_cap": cap,
                        "revenue": 100.0 + score * 10,
                        "net_profit": 10.0 + score,
                        "revenue_yoy": 0.10 + score * 0.05,
                        "profit_yoy": 0.10 + score * 0.05,
                        "gross_margin": 0.30 + score * 0.05,
                    }
                )
        frame = pd.DataFrame(rows)
        ranking, notes = rank_industry_financials(
            frame, "2026-11-02", top_n=2, auto_period=False
        )
        self.assertEqual(notes, [])
        self.assertEqual(len(ranking), 6)
        for _, group in ranking.groupby("industry_l3_id"):
            self.assertEqual(group["industry_rank"].min(), 1)
            self.assertEqual(int(group["in_watchlist"].sum()), 2)
        self.assertEqual(set(ranking["rank_model"]), {"growth", "general"})

    def test_auto_period_falls_back_when_latest_is_thin(self) -> None:
        """最新报告期只有个别公司披露时，改用覆盖率足够的报告期。"""
        rows = []
        codes = ["A1.SZ", "B2.SH", "C3.SH", "D4.SH", "E5.SH"]
        for code in codes:
            rows.append(
                {
                    "company_id": code,
                    "company_name": code,
                    "industry_l3_id": "850814",
                    "industry_l3_name": "数字芯片设计",
                    "model_type": "growth",
                    "report_period": "2026-06-30",
                    "announcement_date": "2026-08-20",
                    "market_cap": 1.0e10,
                    "revenue": 100.0,
                    "net_profit": 10.0,
                    "revenue_yoy": 0.10,
                    "profit_yoy": 0.10,
                    "gross_margin": 0.30,
                }
            )
        # 只有一家披露三季报
        rows.append(dict(rows[0], report_period="2026-09-30", announcement_date="2026-10-25"))
        frame = pd.DataFrame(rows)
        ranking, notes = rank_industry_financials(frame, "2026-11-02", top_n=2)
        self.assertEqual(len(notes), 1)
        self.assertIn("样本过少", notes[0])
        self.assertEqual(len(ranking), 5)
        periods = pd.to_datetime(ranking["report_period"], errors="coerce")
        self.assertTrue(periods.eq(pd.Timestamp("2026-06-30")).all())

    def test_auto_period_keeps_latest_when_coverage_is_enough(self) -> None:
        rows = []
        for code in ["A1.SZ", "B2.SH", "C3.SH", "D4.SH", "E5.SH"]:
            for period, announced in (("2026-06-30", "2026-08-20"), ("2026-09-30", "2026-10-25")):
                rows.append(
                    {
                        "company_id": code,
                        "company_name": code,
                        "industry_l3_id": "850814",
                        "industry_l3_name": "数字芯片设计",
                        "model_type": "growth",
                        "report_period": period,
                        "announcement_date": announced,
                        "market_cap": 1.0e10,
                        "revenue": 100.0,
                        "net_profit": 10.0,
                        "revenue_yoy": 0.10,
                        "profit_yoy": 0.10,
                        "gross_margin": 0.30,
                    }
                )
        ranking, notes = rank_industry_financials(pd.DataFrame(rows), "2026-11-02", top_n=2)
        self.assertEqual(notes, [])
        periods = pd.to_datetime(ranking["report_period"], errors="coerce")
        self.assertTrue(periods.eq(pd.Timestamp("2026-09-30")).all())

    def test_available_report_periods(self) -> None:
        frame = pd.DataFrame(
            {
                "company_id": ["A1.SZ", "A1.SZ"],
                "report_period": ["2026-06-30", "2026-09-30"],
                "announcement_date": ["2026-08-20", "2026-12-05"],
            }
        )
        periods = available_report_periods(frame, "2026-11-02")
        self.assertEqual(periods, [pd.Timestamp("2026-06-30")])

    def test_synced_industries_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            frame = pd.DataFrame(
                {
                    "company_id": ["A1.SZ", "B2.SH"],
                    "company_name": ["甲", "乙"],
                    "industry_l3_id": ["850814", "850814"],
                    "industry_l3_name": ["数字芯片设计", "数字芯片设计"],
                    "report_period": ["2026-09-30", "2026-09-30"],
                    "announcement_date": ["2026-10-25", "2026-10-26"],
                }
            )
            frame.to_csv(data_dir / "850814.csv", index=False, encoding="utf-8-sig")
            summary = synced_industries(data_dir)
            self.assertEqual(len(summary), 1)
            self.assertEqual(int(summary.iloc[0]["company_count"]), 2)
            self.assertEqual(int(summary.iloc[0]["record_count"]), 2)
            combined = load_all_industry_financials(data_dir)
            self.assertEqual(len(combined), 2)
        self.assertTrue(synced_industries(Path(directory) / "empty").empty)


if __name__ == "__main__":
    unittest.main()
