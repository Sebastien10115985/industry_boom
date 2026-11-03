from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache

import pandas as pd

from public_industry_taxonomy import SW_L3_INDUSTRIES, SNAPSHOT_DATE, SOURCE_URL


ROMAN_NUMERALS = "ⅠⅡⅢⅣⅤⅥⅦⅧ"
NOISE_WORDS = ("申万", "三级", "二级", "一级", "行业", "板块", "级", "指数", "Ⅲ", "Ⅱ", "Ⅰ")
PUNCTUATION = re.compile(r"[\s\-_/\\()（）\[\]【】、,，.。:：;；*·&+]")


def normalize_industry_name(name: object) -> str:
    """归一化行业名称：去除罗马数字、空白、标点与无区分度的词。"""
    text = str(name or "").strip()
    text = PUNCTUATION.sub("", text)
    text = text.replace("Ⅲ", "").replace("Ⅱ", "").replace("Ⅰ", "")
    for word in NOISE_WORDS:
        text = text.replace(word, "")
    return text


@lru_cache(maxsize=1)
def builtin_industry_frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        SW_L3_INDUSTRIES,
        columns=[
            "industry_l3_id",
            "industry_l3_name",
            "industry_l2_id",
            "industry_l2_name",
            "industry_l1_id",
            "industry_l1_name",
            "company_count",
        ],
    )
    frame["industry_l3_id"] = frame["industry_l3_id"].astype(str)
    frame["match_key"] = frame["industry_l3_name"].map(normalize_industry_name)
    return frame


def industry_options() -> pd.DataFrame:
    """返回可直接用于界面的固定三级行业清单。"""
    return builtin_industry_frame().copy()


def industry_label(row: pd.Series) -> str:
    return f"{row['industry_l3_name']}（{row['industry_l2_name']} / {row['industry_l1_name']}）"


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.92
    return SequenceMatcher(None, left, right).ratio()


def match_industry_name(name: object, minimum_score: float = 0.60) -> dict[str, object]:
    """把任意来源的行业名称语义模糊匹配到内置三级行业。"""
    query = normalize_industry_name(name)
    frame = builtin_industry_frame()
    if not query:
        return {
            "industry_l3_id": "",
            "industry_l3_name": "",
            "match_score": 0.0,
            "match_method": "empty",
        }

    exact = frame[frame["industry_l3_id"].eq(str(name).strip()) | frame["industry_l3_name"].eq(str(name).strip())]
    if not exact.empty:
        row = exact.iloc[0]
        return {
            "industry_l3_id": row["industry_l3_id"],
            "industry_l3_name": row["industry_l3_name"],
            "match_score": 1.0,
            "match_method": "exact",
        }

    scores = frame["match_key"].map(lambda key: _similarity(query, key))
    position = int(scores.idxmax())
    best_score = float(scores.max())
    row = frame.loc[position]
    if best_score >= 0.92:
        method = "contain"
    elif best_score >= minimum_score:
        method = "fuzzy"
    else:
        method = "unmatched"
    return {
        "industry_l3_id": "" if method == "unmatched" else row["industry_l3_id"],
        "industry_l3_name": "" if method == "unmatched" else row["industry_l3_name"],
        "match_score": round(best_score, 4),
        "match_method": method,
    }


def map_industry_names(values: pd.Series, minimum_score: float = 0.60) -> pd.DataFrame:
    """批量把行业名称映射为内置三级行业 ID，并保留匹配置信度。"""
    unique = pd.Series(values.dropna().astype(str).unique())
    mapping = pd.DataFrame(
        [match_industry_name(value, minimum_score) for value in unique],
        index=unique,
    )
    result = pd.DataFrame(index=values.index)
    keys = values.fillna("").astype(str)
    result["industry_l3_id"] = keys.map(mapping["industry_l3_id"])
    result["industry_l3_name"] = keys.map(mapping["industry_l3_name"])
    result["industry_match_score"] = keys.map(mapping["match_score"]).astype(float)
    result["industry_match_method"] = keys.map(mapping["match_method"])
    return result


def search_industries(keyword: str, limit: int = 50) -> pd.DataFrame:
    """按关键字做模糊检索，供界面快速定位行业。"""
    frame = builtin_industry_frame()
    query = normalize_industry_name(keyword)
    if not query:
        return frame.head(limit).assign(match_score=1.0).copy()
    scores = frame["match_key"].map(lambda key: _similarity(query, key))
    ranked = frame.assign(match_score=scores.round(4)).sort_values(
        ["match_score", "company_count"], ascending=[False, False]
    )
    return ranked.head(limit).copy()


TAXONOMY_SOURCE = {
    "source": SOURCE_URL,
    "snapshot_date": SNAPSHOT_DATE,
    "industry_count": len(SW_L3_INDUSTRIES),
}
