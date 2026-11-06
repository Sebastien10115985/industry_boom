"""行业景气度：基于国家统计局分行业月度数据的客观景气排序。

输入是国家统计局每月公布的两组官方数据：

- 规模以上工业分行业增加值同比增长速度（当月）
- 规模以上工业分行业利润总额同比增长速度（年初至当月累计）

景气度只用官方口径的增长率，不使用网络文章的主观评价。近 N 个月
（默认 6 个月）按下列权重合成为 0—100 的景气度：

| 分项 | 权重 | 含义 |
|---|---:|---|
| 近 N 月利润增长率均值 | 45% | 盈利趋势的中枢 |
| 近 N 月工业增加值增长率均值 | 25% | 产出景气 |
| 最新月利润增长率 | 15% | 当期盈利 |
| 利润增长动量（最新 - 均值） | 15% | 边际变化 |

缺少的分项按 50 分中性值计入，并在 `data_note` 中说明，避免缺数据
被误判为高景气或低景气。
"""

from __future__ import annotations

import pandas as pd

from prosperity_mapping import mapped_industry_frame, mapped_industry_ids


DEFAULT_WINDOW = 6
DEFAULT_TOP_N = 5
DEFAULT_WEIGHTS = {
    "profit_mean": 0.45,
    "value_added_mean": 0.25,
    "profit_latest": 0.15,
    "profit_momentum": 0.15,
}
NEUTRAL_SCORE = 50.0

RESULT_COLUMNS = [
    "stat_industry",
    "prosperity_score",
    "profit_yoy_latest",
    "profit_yoy_mean",
    "value_added_yoy_latest",
    "value_added_yoy_mean",
    "profit_momentum",
    "months",
    "latest_month",
    "data_note",
]


def _last_valid(series: pd.Series) -> float:
    cleaned = pd.to_numeric(series, errors="coerce").dropna()
    return float(cleaned.iloc[-1]) if not cleaned.empty else float("nan")


def compute_prosperity(
    history: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    weights: dict[str, float] | None = None,
    min_months: int = 2,
) -> pd.DataFrame:
    """按近 window 个月的官方月度数据计算每个统计行业的景气度。"""
    if window < 1:
        raise ValueError("window 不能小于 1")
    weights = dict(weights or DEFAULT_WEIGHTS)
    frame = history.copy()
    if frame.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    frame["month"] = frame["month"].astype(str)
    frame["stat_industry"] = frame["stat_industry"].astype(str)
    months = sorted(frame["month"].unique())[-window:]
    scoped = frame[frame["month"].isin(months)]

    rows = []
    for name, group in scoped.groupby("stat_industry", sort=False):
        group = group.sort_values("month")
        profit = pd.to_numeric(group["profit_yoy"], errors="coerce")
        value_added = pd.to_numeric(group["industry_value_added_yoy"], errors="coerce")
        profit_mean = float(profit.mean()) if profit.notna().any() else float("nan")
        value_added_mean = float(value_added.mean()) if value_added.notna().any() else float("nan")
        profit_latest = _last_valid(profit)
        value_added_latest = _last_valid(value_added)
        months_count = int(group["month"].nunique())
        if pd.isna(profit_mean):
            continue
        if months_count < min_months:
            continue
        notes = []
        if pd.isna(value_added_mean):
            notes.append("工业增加值未公布该行业")
        if months_count < window:
            notes.append(f"仅 {months_count}/{window} 个月有数据")
        rows.append(
            {
                "stat_industry": name,
                "months": months_count,
                "latest_month": str(group["month"].iloc[-1]),
                "profit_yoy_latest": profit_latest,
                "profit_yoy_mean": profit_mean,
                "value_added_yoy_latest": value_added_latest,
                "value_added_yoy_mean": value_added_mean,
                "profit_momentum": (profit_latest - profit_mean) if pd.notna(profit_latest) else float("nan"),
                "data_note": "；".join(notes),
            }
        )
    scored = pd.DataFrame(rows)
    if scored.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    components = {
        "profit_mean": scored["profit_yoy_mean"],
        "value_added_mean": scored["value_added_yoy_mean"],
        "profit_latest": scored["profit_yoy_latest"],
        "profit_momentum": scored["profit_momentum"],
    }
    total = 0.0
    for key, series in components.items():
        weight = float(weights.get(key, 0.0))
        percentile = series.rank(pct=True, ascending=True) * 100.0
        total += weight * percentile.fillna(NEUTRAL_SCORE)
    weight_sum = sum(float(weights.get(key, 0.0)) for key in components)
    scored["prosperity_score"] = (total / weight_sum if weight_sum else NEUTRAL_SCORE).round(1)
    return (
        scored.sort_values(["prosperity_score", "profit_yoy_mean"], ascending=[False, False])
        .reset_index(drop=True)[RESULT_COLUMNS]
    )


def top_prosperity(
    history: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    top_n: int = DEFAULT_TOP_N,
    weights: dict[str, float] | None = None,
    only_mapped: bool = True,
) -> pd.DataFrame:
    """返回最景气的 top_n 个统计行业。

    only_mapped 为真时，只保留能对应到申万三级行业的统计行业，
    避免“金属制品、机械和设备修理业”这类没有对应标的的类别占位。
    """
    if top_n < 1:
        raise ValueError("top_n 不能小于 1")
    frame = compute_prosperity(history, window=window, weights=weights)
    if only_mapped and not frame.empty:
        frame = frame[frame["stat_industry"].map(lambda name: bool(mapped_industry_ids(name)))]
    return frame.head(top_n).reset_index(drop=True)


def prosperity_board(
    history: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    top_n: int = DEFAULT_TOP_N,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (最景气统计行业, 完整景气度表)。"""
    full = compute_prosperity(history, window=window)
    return top_prosperity(history, window=window, top_n=top_n), full


def corresponding_industries(stat_industry: str) -> pd.DataFrame:
    """返回该统计行业对应的申万三级行业。"""
    return mapped_industry_frame(stat_industry)


# 除自动选出的 TOP5 外，固定跟踪的行业大类（统计局标准名称）。
EXTRA_STAT_INDUSTRIES: tuple[str, ...] = (
    "有色金属矿采选业",
    "非金属矿采选业",
    "酒、饮料和精制茶制造业",
    "有色金属冶炼和压延加工业",
    "通用设备制造业",
    "专用设备制造业",
    "汽车制造业",
    "电气机械和器材制造业",
    "仪器仪表制造业",
    "电力、热力生产和供应业",
)


def prosperity_for_industries(
    history: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    names: tuple[str, ...] = EXTRA_STAT_INDUSTRIES,
) -> pd.DataFrame:
    """取指定统计行业大类的景气行；统计局缺数的行业补占位行，便于固定展示。"""
    full = compute_prosperity(history, window=window)
    indexed = full.set_index("stat_industry") if not full.empty else full
    rows = []
    for name in names:
        if name in indexed.index:
            row = indexed.loc[name].to_dict()
            row["stat_industry"] = name
            rows.append(row)
        else:
            rows.append({"stat_industry": name, "data_note": "统计局暂无该行业的月度利润数据或有效月数不足"})
    return pd.DataFrame(rows)


def top_companies_for_industry_ids(
    ranking: pd.DataFrame,
    industry_ids,
    per_industry: int = 3,
) -> pd.DataFrame:
    """在给定申万三级行业集合中，取每个三级行业排名靠前的公司。"""
    if per_industry < 1:
        raise ValueError("per_industry 不能小于 1")
    if ranking is None or ranking.empty or "industry_l3_id" not in ranking.columns:
        return pd.DataFrame()
    ids = {str(value) for value in industry_ids}
    if not ids:
        return pd.DataFrame()
    selected = ranking[ranking["industry_l3_id"].astype(str).isin(ids)].copy()
    if selected.empty:
        return pd.DataFrame()
    score = pd.to_numeric(selected.get("weighted_score"), errors="coerce")
    rank = pd.to_numeric(selected.get("industry_rank"), errors="coerce")
    selected["_score"] = score.fillna(-1e18)
    selected["_rank"] = rank.fillna(1e18)
    selected = selected.sort_values(["industry_l3_id", "_score", "_rank"], ascending=[True, False, True])
    picked = selected.groupby("industry_l3_id", sort=False).head(per_industry)
    picked = picked.sort_values(["_score"], ascending=False)
    return picked.drop(columns=["_score", "_rank"]).reset_index(drop=True)


def top_companies_for_stat_industry(
    ranking: pd.DataFrame,
    stat_industry: str,
    per_industry: int = 3,
) -> pd.DataFrame:
    """在该统计行业对应的申万三级行业中，取每个三级行业排名靠前的公司。"""
    if ranking is None or ranking.empty or "industry_l3_id" not in ranking.columns:
        return pd.DataFrame()
    return top_companies_for_industry_ids(ranking, mapped_industry_ids(stat_industry), per_industry)
