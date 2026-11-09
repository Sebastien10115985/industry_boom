"""内置申万三级行业成分公司（公开来源快照）。

数据来源：https://www.legulegu.com/stockdata/index-composition?industryCode={三级行业ID}.SI
快照日期：2026-11-09

固化在仓库内，程序无需联网即可列出每个三级行业的成分公司。
数据文件：`catalog/sw_l3_constituents.csv`（2413 条，覆盖 111 个三级行业）。
补充来源：有色金属 14 个三级行业（铜/铝/铅锌/黄金/白银/稀土/钨/钼/其他小金属/锂/钴/镍/
其他金属新材料/磁性材料）与医药生物的医疗耗材Ⅲ，由申万宏源研究官网分类表整理，
原始文件见 `data/swresearch/`，按主营业务归入三级行业。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

SOURCE_URL = "https://www.legulegu.com/stockdata/index-composition"
SNAPSHOT_DATE = "2026-11-09"
CONSTITUENTS_PATH = Path(__file__).resolve().parent / "catalog" / "sw_l3_constituents.csv"

COLUMNS = ["industry_l3_id", "industry_l3_name", "company_id", "company_name", "include_date"]


def load_constituents(path: str | Path = CONSTITUENTS_PATH) -> pd.DataFrame:
    """读取固化的成分公司表。"""
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.read_csv(source, dtype={"industry_l3_id": str, "company_id": str}, encoding="utf-8-sig")
    for column in COLUMNS:
        if column not in frame:
            frame[column] = pd.NA
    return frame[COLUMNS]


def constituents_for(industry_ids, path: str | Path = CONSTITUENTS_PATH) -> pd.DataFrame:
    """取指定三级行业的成分公司。"""
    frame = load_constituents(path)
    if frame.empty:
        return frame
    wanted = {str(value) for value in industry_ids}
    return frame[frame["industry_l3_id"].astype(str).isin(wanted)].reset_index(drop=True)


def industry_coverage(path: str | Path = CONSTITUENTS_PATH) -> pd.DataFrame:
    """每个三级行业的成分公司数量，便于核对覆盖情况。"""
    frame = load_constituents(path)
    if frame.empty:
        return pd.DataFrame(columns=["industry_l3_id", "industry_l3_name", "company_count"])
    grouped = (
        frame.groupby(["industry_l3_id", "industry_l3_name"], as_index=False)
        .size()
        .rename(columns={"size": "company_count"})
    )
    return grouped.sort_values("company_count", ascending=False).reset_index(drop=True)
