"""cffex_provider 离线单测：月度 zip 解析、主力连续选取、缓存合并。"""

from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cffex_provider import (
    CffexError,
    _IF_COLUMNS,
    _parse_daily_csv,
    dominant_series,
    merge_if_daily,
    parse_month_zip,
)

HEADER = "合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,今收盘,今结算,前结算,涨跌1,涨跌2,Delta"

SAMPLE_20260105 = (
    HEADER + "\n"
    "IH2601,3000,3010,2990,5000,750000,30000,10,3005,3004.2,3002,3,3.2,--\n"
    "IF2601,4200,4210,4190,8000,1680000,50000,20,4205,4204.5,4200,5,4.5,--\n"
    "IF2603,4150,4160,4140,3000,630000,48000,5,4155,4154,4150,5,4,--\n"
    "IC2601,6000,6010,5990,7000,1050000,40000,15,6005,6004,6000,5,4,--\n"
    "IM2601,6500,6510,6490,6000,975000,35000,10,6505,6504,6500,5,4,--\n"
)

SAMPLE_20260106 = (
    HEADER + "\n"
    "IF2601,4210,4230,4205,9000,1900000,51000,-10,4228,4227,4205,23,22,--\n"
    "IF2603,4160,4170,4150,3200,672000,48500,5,4165,4164,4155,10,9,--\n"
)


def build_zip(daily: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in daily.items():
            bundle.writestr(name, content.encode("gb18030"))
    return buffer.getvalue()


class CffexProviderTest(unittest.TestCase):
    def test_parse_month_zip_dominant(self) -> None:
        raw = build_zip(
            {
                "20260105_1.csv": SAMPLE_20260105,
                "20260106_1.csv": SAMPLE_20260106,
                "readme.txt": "other",
            }
        )
        frame = parse_month_zip(raw)
        self.assertEqual(len(frame), 4)
        self.assertEqual(set(frame.columns), set(_IF_COLUMNS))
        dominant = dominant_series(frame)
        # 每日主力 = 成交量最大的 IF 合约
        self.assertEqual(dominant.iloc[0]["contract"], "IF2601")
        self.assertEqual(dominant.iloc[0]["volume"], 8000)
        self.assertEqual(dominant.iloc[0]["close"], 4205.0)
        self.assertEqual(dominant.iloc[1]["contract"], "IF2601")
        self.assertEqual(dominant.iloc[1]["close"], 4228.0)
        self.assertEqual(len(dominant), 2)

    def test_parse_ignores_non_if(self) -> None:
        records = _parse_daily_csv(SAMPLE_20260105.encode("gb18030"), "20260105")
        self.assertTrue(all(r["contract"].startswith("IF") for r in records))
        self.assertEqual(len(records), 2)

    def test_parse_rejects_bad_header(self) -> None:
        with self.assertRaises(CffexError):
            _parse_daily_csv("合约,价格\nA,1\n".encode("gb18030"), "20260105")

    def test_merge_overwrites_same_date(self) -> None:
        base = pd_like = parse_month_zip(
            build_zip({"20260105_1.csv": SAMPLE_20260105, "20260106_1.csv": SAMPLE_20260106})
        )
        base = dominant_series(base)
        # 新数据修正 1 月 5 日主力记录
        revised = parse_month_zip(
            build_zip({"20260105_1.csv": SAMPLE_20260105.replace(",8000,1680000", ",9500,2000000")})
        )
        revised = dominant_series(revised)
        merged = merge_if_daily(base, revised)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged.iloc[0]["volume"], 9500)
        self.assertEqual(merged.iloc[1]["close"], 4228.0)

    def test_merge_keeps_other_dates(self) -> None:
        base = dominant_series(
            parse_month_zip(build_zip({"20260106_1.csv": SAMPLE_20260106}))
        )
        fresh = dominant_series(
            parse_month_zip(build_zip({"20260107_1.csv": SAMPLE_20260105.replace("IF2601,4200", "IF2603,4100")}))
        )
        merged = merge_if_daily(base, fresh)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged.iloc[0]["date"], "20260106")


if __name__ == "__main__":
    unittest.main()
