"""从公开行业成分股页面固化申万三级行业的成分公司。

数据来源：乐咕乐股「成分股」页（与内置行业目录同源，公开可查）
https://www.legulegu.com/stockdata/index-composition?industryCode={三级行业ID}.SI

取到的成分公司写入：

- `catalog/sw_l3_constituents.csv`：可人工核对的成分股表
- `sw_constituents.py`：快照日期、来源与读取函数，代码里直接使用，无需联网

用法：

```powershell
python tools/freeze_sw_constituents.py                      # 只固化当前最景气 5 个统计行业对应的三级行业
python tools/freeze_sw_constituents.py --top-n 10           # 扩大到最景气 10 个统计行业
python tools/freeze_sw_constituents.py --all                # 固化全部三级行业
python tools/freeze_sw_constituents.py --industry-ids 850111,850112
```
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
CSV_PATH = PROJECT_DIR / "catalog" / "sw_l3_constituents.csv"
MODULE_PATH = PROJECT_DIR / "sw_constituents.py"

URL = "https://www.legulegu.com/stockdata/index-composition"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.legulegu.com/stockdata/sw-industry-overview",
}
ROW = re.compile(r"<tr[^>]*>(?:(?!</tr>).)*?</tr>", re.S)
CELL = re.compile(r"<t[dh][^>]*>(?:(?!</t[dh]>).)*?</t[dh]>", re.S)
TAG = re.compile(r"<[^>]+>")
CODE = re.compile(r"^\s*(\d{6})\.(SH|SZ|BJ)\s*$")
COLUMNS = ["industry_l3_id", "industry_l3_name", "company_id", "company_name", "include_date"]


def fetch_industry(session: requests.Session, industry_id: str, retries: int = 6) -> list[tuple[str, str, str]]:
    """抓取单个三级行业的成分股，返回 (公司代码, 名称, 纳入时间)。

    公开页面偶发 504/限流，这里按递增间隔重试；仍失败时返回空列表，
    调用方会保留上一次已固化的该行业数据。
    """
    for attempt in range(retries):
        try:
            response = session.get(
                URL, params={"industryCode": f"{industry_id}.SI"}, headers=HEADERS, timeout=60
            )
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            return parse_rows(response.text)
        except Exception:
            if attempt == retries - 1:
                return []
            time.sleep(5 + attempt * 15)
    return []


def parse_rows(html: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for row in ROW.findall(html):
        cells = [re.sub(r"\s+", " ", TAG.sub("", cell)).strip() for cell in CELL.findall(row)]
        for index, cell in enumerate(cells):
            matched = CODE.match(cell)
            if not matched:
                continue
            name = cells[index + 1] if index + 1 < len(cells) else ""
            include_date = cells[index + 2] if index + 2 < len(cells) and re.match(r"\d{4}-\d{2}-\d{2}", cells[index + 2]) else ""
            if name and not name.startswith("股票"):
                rows.append((f"{matched.group(1)}.{matched.group(2)}", name, include_date))
            break
    return rows


def target_industries(top_n: int, all_industries: bool, raw_ids: str) -> pd.DataFrame:
    """确定要固化的三级行业。"""
    from industry_match import builtin_industry_frame

    frame = builtin_industry_frame()
    if raw_ids:
        wanted = [item.strip() for item in raw_ids.split(",") if item.strip()]
        return frame[frame["industry_l3_id"].isin(wanted)]
    if all_industries:
        return frame
    from nbs_provider import load_history
    from prosperity import top_prosperity
    from prosperity_mapping import mapped_industry_ids

    history = load_history()
    if history.empty:
        raise SystemExit("景气历史为空，请先执行 python online_cli.py prosperity --months 6")
    top = top_prosperity(history, window=6, top_n=top_n)
    ids: list[str] = []
    for stat_industry in top["stat_industry"]:
        ids.extend(mapped_industry_ids(str(stat_industry)))
    print(f"最景气 {len(top)} 个统计行业 -> {len(set(ids))} 个申万三级行业")
    return frame[frame["industry_l3_id"].isin(set(ids))]


def merge_existing(incoming: pd.DataFrame, fetched_ids: set[str]) -> pd.DataFrame:
    """保留未重新抓取的行业，避免偶发失败丢失上次已固化的成分股。"""
    if CSV_PATH.exists() and CSV_PATH.stat().st_size:
        try:
            previous = pd.read_csv(CSV_PATH, dtype={"industry_l3_id": str}, encoding="utf-8-sig")
        except Exception:
            previous = pd.DataFrame(columns=COLUMNS)
        kept = previous[~previous["industry_l3_id"].astype(str).isin(fetched_ids)]
        combined = pd.concat([kept[COLUMNS], incoming[COLUMNS]], ignore_index=True)
    else:
        combined = incoming[COLUMNS]
    return combined.sort_values(["industry_l3_id", "company_id"]).reset_index(drop=True)


def write_outputs(records: pd.DataFrame, snapshot: str, covered: int) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    records.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    module = f'''"""内置申万三级行业成分公司（公开来源快照）。

数据来源：{URL}?industryCode={{三级行业ID}}.SI
快照日期：{snapshot}

固化在仓库内，程序无需联网即可列出每个三级行业的成分公司。
数据文件：`catalog/sw_l3_constituents.csv`（{len(records)} 条，覆盖 {covered} 个三级行业）。
补充来源：有色金属 14 个三级行业（铜/铝/铅锌/黄金/白银/稀土/钨/钼/其他小金属/锂/钴/镍/
其他金属新材料/磁性材料）由申万宏源研究官网分类表整理，原始文件见 `data/swresearch/`，
按主营业务归入三级行业。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

SOURCE_URL = "{URL}"
SNAPSHOT_DATE = "{snapshot}"
CONSTITUENTS_PATH = Path(__file__).resolve().parent / "catalog" / "sw_l3_constituents.csv"

COLUMNS = ["industry_l3_id", "industry_l3_name", "company_id", "company_name", "include_date"]


def load_constituents(path: str | Path = CONSTITUENTS_PATH) -> pd.DataFrame:
    """读取固化的成分公司表。"""
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.read_csv(source, dtype={{"industry_l3_id": str, "company_id": str}}, encoding="utf-8-sig")
    for column in COLUMNS:
        if column not in frame:
            frame[column] = pd.NA
    return frame[COLUMNS]


def constituents_for(industry_ids, path: str | Path = CONSTITUENTS_PATH) -> pd.DataFrame:
    """取指定三级行业的成分公司。"""
    frame = load_constituents(path)
    if frame.empty:
        return frame
    wanted = {{str(value) for value in industry_ids}}
    return frame[frame["industry_l3_id"].astype(str).isin(wanted)].reset_index(drop=True)


def industry_coverage(path: str | Path = CONSTITUENTS_PATH) -> pd.DataFrame:
    """每个三级行业的成分公司数量，便于核对覆盖情况。"""
    frame = load_constituents(path)
    if frame.empty:
        return pd.DataFrame(columns=["industry_l3_id", "industry_l3_name", "company_count"])
    grouped = (
        frame.groupby(["industry_l3_id", "industry_l3_name"], as_index=False)
        .size()
        .rename(columns={{"size": "company_count"}})
    )
    return grouped.sort_values("company_count", ascending=False).reset_index(drop=True)
'''
    MODULE_PATH.write_text(module, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="固化申万三级行业成分公司")
    parser.add_argument("--top-n", type=int, default=5, help="按最景气统计行业数量确定范围")
    parser.add_argument("--all", action="store_true", help="固化全部三级行业")
    parser.add_argument("--industry-ids", help="逗号分隔的三级行业 ID")
    parser.add_argument("--sleep", type=float, default=2.0, help="每次请求间隔秒数")
    args = parser.parse_args()

    targets = target_industries(args.top_n, args.all, args.industry_ids)
    print(f"待固化三级行业：{len(targets)} 个")
    session = requests.Session()
    records: list[dict[str, str]] = []
    fetched_ids: set[str] = set()
    failed: list[str] = []
    for position, row in enumerate(targets.itertuples(index=False), start=1):
        industry_id = str(row.industry_l3_id)
        constituents = fetch_industry(session, industry_id)
        if constituents:
            fetched_ids.add(industry_id)
        else:
            failed.append(f"{row.industry_l3_name}({industry_id})")
        for company_id, company_name, include_date in constituents:
            records.append(
                {
                    "industry_l3_id": industry_id,
                    "industry_l3_name": row.industry_l3_name,
                    "company_id": company_id,
                    "company_name": company_name,
                    "include_date": include_date,
                }
            )
        if position % 10 == 0 or position == len(targets):
            print(f"  {position}/{len(targets)} {row.industry_l3_name} {len(constituents)} 家")
        time.sleep(args.sleep)

    frame = pd.DataFrame(records, columns=COLUMNS)
    frame = merge_existing(frame, fetched_ids)
    snapshot = date.today().isoformat()
    write_outputs(frame, snapshot, int(frame["industry_l3_id"].nunique()))
    print(f"已固化 {len(frame)} 条成分公司，覆盖 {frame['industry_l3_id'].nunique()} 个三级行业，快照 {snapshot}")
    if failed:
        print(f"本次未取到（保留上次数据或下次重跑）：{len(failed)} 个 -> " + "、".join(failed))
    print(f"  {CSV_PATH}")
    print(f"  {MODULE_PATH}")


if __name__ == "__main__":
    main()
