import unittest

from customs_provider import (
    CustomsError,
    find_major_exports_links,
    import_customs_article,
    load_customs,
    match_tracked_items,
    merge_customs,
    month_from_exports_title,
    parse_major_exports,
)

ARTICLE_HTML = """
<html><head><title>(5) China's Major Exports by Quantity and Value, Oct 2026 (in CNY)</title></head>
<body>
<p>(5) China's Major Exports by Quantity and Value, Oct 2026 (in CNY) 2026/11/10</p>
<p>Unit: CNY 100 Million</p>
<table>
<tr><td>Commodity</td><td>Quantity Unit</td><td>10 Quantity</td><td>10 Value</td>
<td>1-10 Quantity</td><td>1-10 Value</td><td>1-10 Total, 2025 Quantity</td>
<td>1-10 Total, 2025 Value</td><td>Year-On-Year(%) Quantity</td><td>Year-On-Year(%) Value</td></tr>
<tr><td>Quantity</td><td></td><td>Value</td><td></td><td>Value</td><td></td><td>Value</td><td></td><td>Value</td><td></td></tr>
<tr><td>Rare-earth ore, metals, compounds</td><td>Ton</td><td>4,386.0</td><td>5.6</td><td>48,097.2</td><td>40.1</td><td>52,699.1</td><td>28.6</td><td>-8.7</td><td>40.2</td></tr>
<tr><td>Mechanical and electrical products</td><td>-</td><td>-</td><td>18,813.8</td><td>-</td><td>168,079.6</td><td>-</td><td>134,238.3</td><td>-</td><td>25.2</td></tr>
<tr><td>Electronic integrated circuits</td><td>100 Million PCS</td><td>268.1</td><td>2,677.8</td><td>3,020.3</td><td>23,380.8</td><td>2,926.4</td><td>11,582.6</td><td>3.2</td><td>101.9</td></tr>
<tr><td>Note: 1. "Agricultural products" refers to ...</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>
</body></html>
"""

LISTING_HTML = """
<table>
<tr><td>(4) China's Total Export &amp; Import Values by Country/Region (in CNY)</td>
<td><span>Oct.</span><a href=http://english.customs.gov.cn/Statics/aaa.html> Sep.</a></td></tr>
<tr><td>(5) China's Major Exports by Quantity and Value (in CNY)</td>
<td><span>Jan.</span><a href=http://english.customs.gov.cn/Statics/bbb.html> Feb.</a>
<a href=http://english.customs.gov.cn/Statics/ccc.html> Oct.</a><span>Nov.</span><span>Dec.</span></td></tr>
<tr><td>(5) China's Major Exports by Quantity and Value (in USD)</td>
<td><a href=http://english.customs.gov.cn/Statics/ddd.html> Oct.</a></td></tr>
</table>
"""


class MonthFromTitleTests(unittest.TestCase):
    def test_parses_month_and_year(self):
        self.assertEqual(month_from_exports_title("(5) China's Major Exports by Quantity and Value, Oct 2026 (in CNY)"), "2026-10")
        self.assertEqual(month_from_exports_title("(5) China's Major Exports by Quantity and Value, Mar. 2025 (in CNY)"), "2025-03")

    def test_rejects_unparsable_title(self):
        with self.assertRaises(CustomsError):
            month_from_exports_title("no month here")


class FindLinksTests(unittest.TestCase):
    def test_only_cny_row_and_published_months(self):
        links = find_major_exports_links(LISTING_HTML)
        self.assertEqual(set(links), {"Feb", "Oct"})
        self.assertEqual(links["Oct"], "http://english.customs.gov.cn/Statics/ccc.html")

    def test_missing_section_raises(self):
        with self.assertRaises(CustomsError):
            find_major_exports_links("<html><body>empty</body></html>")


class ParseArticleTests(unittest.TestCase):
    def test_parses_month_and_items(self):
        month, items = parse_major_exports(ARTICLE_HTML)
        self.assertEqual(month, "2026-10")
        self.assertEqual(items["Electronic integrated circuits"], (2677.8, 23380.8, 101.9))
        self.assertEqual(items["Rare-earth ore, metals, compounds"], (5.6, 40.1, 40.2))
        self.assertEqual(items["Mechanical and electrical products"], (18813.8, 168079.6, 25.2))
        self.assertNotIn("Note: 1", items)

    def test_match_tracked_items(self):
        _, items = parse_major_exports(ARTICLE_HTML)
        matched = match_tracked_items(items)
        self.assertIn("稀土", matched)
        self.assertIn("机电产品", matched)
        self.assertIn("集成电路", matched)
        self.assertEqual(matched["集成电路"][1], 2677.8)

    def test_match_missing_item_raises(self):
        with self.assertRaises(CustomsError):
            match_tracked_items({"Unknown product": (1.0, 2.0, 3.0)}, strict=True)

    def test_match_missing_item_skipped_by_default(self):
        matched = match_tracked_items({"Unknown product": (1.0, 2.0, 3.0)})
        self.assertEqual(matched, {})


class MergeAndImportTests(unittest.TestCase):
    def test_merge_prefers_latest_non_null(self):
        base = {
            "month": "2026-10",
            "commodity_cn": "稀土",
            "commodity_en": "Rare-earth",
            "export_month_amount_yi": 5.6,
            "export_cum_amount_yi": 40.1,
            "export_cum_yoy_pct": 40.2,
            "source_url": "",
            "fetched_at": "t1",
        }
        old = {**base, "export_cum_yoy_pct": 30.0}
        merged = merge_customs(__import__("pandas").DataFrame([old]), __import__("pandas").DataFrame([base]))
        self.assertEqual(len(merged), 1)
        self.assertEqual(float(merged.iloc[0]["export_cum_yoy_pct"]), 40.2)

    def test_import_article_roundtrip(self):
        import pandas as pd

        frame = import_customs_article(ARTICLE_HTML, path="data/_tmp_customs_test.csv")
        self.assertTrue((frame["month"] == "2026-10").all())
        loaded = load_customs("data/_tmp_customs_test.csv")
        self.assertFalse(loaded.empty)
        self.assertIn("集成电路", set(loaded[loaded["month"] == "2026-10"]["commodity_cn"]))
        pd.testing.assert_frame_equal(
            frame.astype({"source_url": "object"}).fillna(""),
            loaded.astype({"source_url": "object"}).fillna(""),
        )

    def test_import_bad_article_raises_and_keeps_store(self):
        before = load_customs("data/_tmp_customs_test.csv")
        with self.assertRaises(CustomsError):
            import_customs_article("<html>no table</html>", path="data/_tmp_customs_test.csv")
        after = load_customs("data/_tmp_customs_test.csv")
        self.assertTrue(before.equals(after))


if __name__ == "__main__":
    unittest.main()
