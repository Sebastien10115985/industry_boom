from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from sw_constituents import constituents_for, industry_coverage, load_constituents
from tools.freeze_sw_constituents import parse_rows

SAMPLE_HTML = """
<table>
  <tr><th>序号</th><th>股票代码</th><th>股票简称</th><th>纳入时间</th></tr>
  <tr>
    <td>1</td>
    <td><a href="/stockdata/600313.SH">600313.SH</a></td>
    <td>农发种业</td>
    <td>2021-07-30</td>
  </tr>
  <tr><td>2</td><td>000998.SZ</td><td>隆平高科</td><td>2021-07-30</td></tr>
</table>
"""


class ConstituentTest(unittest.TestCase):
    def test_parse_rows(self) -> None:
        rows = parse_rows(SAMPLE_HTML)
        self.assertEqual(
            rows,
            [("600313.SH", "农发种业", "2021-07-30"), ("000998.SZ", "隆平高科", "2021-07-30")],
        )

    def test_parse_rows_ignores_text_without_code(self) -> None:
        self.assertEqual(parse_rows("<table><tr><td>暂无数据</td></tr></table>"), [])

    def test_frozen_snapshot_is_readable(self) -> None:
        frame = load_constituents()
        self.assertFalse(frame.empty)
        self.assertTrue(frame["company_id"].str.match(r"^\d{6}\.(SH|SZ|BJ)$").all())
        self.assertFalse(frame["company_name"].isna().any())

    def test_filter_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "constituents.csv"
            pd.DataFrame(
                {
                    "industry_l3_id": ["850818", "850818", "859611"],
                    "industry_l3_name": ["半导体设备", "半导体设备", "油气开采Ⅲ"],
                    "company_id": ["002371.SZ", "300604.SZ", "601857.SH"],
                    "company_name": ["北方华创", "长川科技", "中国石油"],
                    "include_date": ["2021-07-30"] * 3,
                }
            ).to_csv(path, index=False, encoding="utf-8-sig")
            picked = constituents_for(["850818"], path=path)
            self.assertEqual(picked["company_name"].tolist(), ["北方华创", "长川科技"])
            self.assertTrue(constituents_for(["000000"], path=path).empty)
            coverage = industry_coverage(path=path)
            self.assertEqual(coverage.iloc[0]["company_count"], 2)
            self.assertTrue(load_constituents(Path(directory) / "missing.csv").empty)


if __name__ == "__main__":
    unittest.main()
