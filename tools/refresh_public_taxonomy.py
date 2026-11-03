from __future__ import annotations

import re
import urllib.request
from datetime import date
from pathlib import Path

URL = "https://legulegu.com/stockdata/sw-industry-overview"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
ITEM = re.compile(
    r'<div id="(\d+)\.SI"[^>]*class="lg-industries-item[^"]*">'
    r"(?:(?!<div id=\"\d+\.SI\").)*?"
    r'<div class="lg-industries-item-number">(.*?)</div>',
    re.S,
)
NAME_COUNT = re.compile(r"^\s*(.+?)\s*\((\d+)\)\s*(?:<span[^>]*>\[(.*?)\]</span>)?\s*$", re.S)
PROJECT_DIR = Path(__file__).resolve().parent.parent


def fetch_text() -> str:
    request = urllib.request.Request(URL, headers=HEADERS)
    raw = urllib.request.urlopen(request, timeout=60).read()
    return raw.decode("utf-8", errors="ignore")


def section(text: str, marker: str) -> str:
    start = text.find(marker)
    if start < 0:
        raise RuntimeError(f"未找到页面节点 {marker}，公开页面结构可能已变化")
    candidates = [
        text.find('id="level2Items"', start + 1),
        text.find('id="level3Items"', start + 1),
        len(text),
    ]
    end = min(position for position in candidates if position > 0)
    return text[start:end]


def parse(block: str) -> list[tuple[str, str, str, int]]:
    rows: list[tuple[str, str, str, int]] = []
    for code, raw in ITEM.findall(block):
        match = NAME_COUNT.match(raw)
        if not match:
            continue
        name = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        rows.append((code, name, (match.group(3) or "").strip(), int(match.group(2))))
    return rows


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main() -> None:
    text = fetch_text()
    level1 = parse(section(text, 'id="level1Items"'))
    level2 = parse(section(text, 'id="level2Items"'))
    level3 = parse(section(text, 'id="level3Items"'))
    if not (level1 and level2 and level3):
        raise RuntimeError("解析结果为空，请检查公开页面结构")

    l1_name = {row[0]: row[1] for row in level1}
    l2_info = {row[1]: (row[0], row[2]) for row in level2}
    records = []
    for code, name, parent_name, count in level3:
        l2_id, l1_name_value = l2_info.get(parent_name, ("", ""))
        l1_id = next(
            (candidate for candidate, value in l1_name.items() if value == l1_name_value),
            "",
        )
        records.append((code, name, l2_id, parent_name, l1_id, l1_name_value, count))

    snapshot = date.today().isoformat()
    csv_path = PROJECT_DIR / "catalog" / "sw_l3_taxonomy.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "industry_l3_id,industry_l3_name,industry_l2_id,industry_l2_name,"
        "industry_l1_id,industry_l1_name,company_count"
    ]
    lines.extend(",".join(str(value) for value in row) for row in records)
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    body = ",\n".join(
        '    ("{0}", "{1}", "{2}", "{3}", "{4}", "{5}", {6})'.format(
            escape(row[0]), escape(row[1]), escape(row[2]), escape(row[3]),
            escape(row[4]), escape(row[5]), row[6],
        )
        for row in records
    )
    module = f'''"""内置申万三级行业目录（申万 2021 版）。

数据来源：公开行业总览页 {URL}
快照日期：{snapshot}

该列表固定在代码中，程序无需万得终端或 Wind Client API 即可直接使用。
字段顺序：三级行业 ID、三级行业名称、二级行业 ID、二级行业名称、
一级行业 ID、一级行业名称、成分公司数量。
"""

from __future__ import annotations

SOURCE_URL = "{URL}"
SNAPSHOT_DATE = "{snapshot}"

SW_L3_INDUSTRIES: tuple[tuple[str, str, str, str, str, str, int], ...] = (
{body},
)


def builtin_industry_records() -> list[dict[str, object]]:
    """返回内置三级行业记录，字段与 catalog 目录表一致。"""
    return [
        {{
            "industry_l3_id": row[0],
            "industry_l3_name": row[1],
            "industry_l2_id": row[2],
            "industry_l2_name": row[3],
            "industry_l1_id": row[4],
            "industry_l1_name": row[5],
            "company_count": row[6],
            "model_type": "",
            "source": SOURCE_URL,
        }}
        for row in SW_L3_INDUSTRIES
    ]
'''
    module_path = PROJECT_DIR / "public_industry_taxonomy.py"
    module_path.write_text(module, encoding="utf-8")
    print(f"已更新内置三级行业目录：{len(records)} 个，快照 {snapshot}")
    print(f"  {module_path}")
    print(f"  {csv_path}")


if __name__ == "__main__":
    main()
