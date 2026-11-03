from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ModelSpec:
    label: str
    metrics: Mapping[str, tuple[float, bool]]


MODEL_SPECS: dict[str, ModelSpec] = {
    "general": ModelSpec(
        label="通用行业",
        metrics={
            "revenue_yoy": (0.125, True),
            "profit_growth_adjusted": (0.125, True),
            "gross_margin": (0.25, True),
            "market_cap": (0.10, True),
            "revenue": (0.20, True),
            "net_profit": (0.20, True),
        },
    ),
    "bank": ModelSpec(
        label="银行",
        metrics={
            "roe": (0.20, True),
            "net_interest_margin": (0.10, True),
            "revenue_yoy": (0.075, True),
            "profit_growth_adjusted": (0.125, True),
            "npl_ratio": (0.15, False),
            "provision_coverage": (0.10, True),
            "cet1_ratio": (0.10, True),
            "cost_income_ratio": (0.05, False),
            "pb": (0.075, False),
            "market_cap": (0.025, True),
        },
    ),
    "securities": ModelSpec(
        label="证券",
        metrics={
            "roe": (0.25, True),
            "revenue_yoy": (0.10, True),
            "profit_growth_adjusted": (0.15, True),
            "net_capital_ratio": (0.15, True),
            "risk_coverage_ratio": (0.10, True),
            "operating_margin": (0.10, True),
            "pb": (0.10, False),
            "market_cap": (0.05, True),
        },
    ),
    "insurance": ModelSpec(
        label="保险",
        metrics={
            "roe": (0.20, True),
            "premium_growth": (0.15, True),
            "profit_growth_adjusted": (0.10, True),
            "solvency_ratio": (0.15, True),
            "combined_ratio": (0.15, False),
            "investment_yield": (0.10, True),
            "pb": (0.10, False),
            "market_cap": (0.05, True),
        },
    ),
    "financial_report": ModelSpec(
        label="金融公告基础模型",
        metrics={
            "revenue": (0.20, True),
            "net_profit": (0.30, True),
            "revenue_yoy": (0.20, True),
            "profit_growth_adjusted": (0.30, True),
        },
    ),
    "growth": ModelSpec(
        label="成长型行业",
        metrics={
            "revenue": (0.10, True),
            "net_profit": (0.15, True),
            "revenue_yoy": (0.20, True),
            "profit_growth_adjusted": (0.30, True),
            "gross_margin": (0.25, True),
        },
    ),
    "innovative_drug": ModelSpec(
        label="创新药行业（对称利润增长）",
        metrics={
            "revenue": (0.10, True),
            "net_profit": (0.15, True),
            "revenue_yoy": (0.20, True),
            # 权重与成长型一致，只把利润增长率换成对称形式，避免跨盈亏时失真
            "profit_growth_symmetric": (0.30, True),
            "gross_margin": (0.25, True),
        },
    ),
    "quality": ModelSpec(
        label="质量型行业",
        metrics={
            "revenue": (0.10, True),
            "net_profit": (0.25, True),
            "revenue_yoy": (0.10, True),
            "profit_growth_adjusted": (0.15, True),
            "gross_margin": (0.40, True),
        },
    ),
    "cyclical": ModelSpec(
        label="周期型行业",
        metrics={
            "revenue": (0.20, True),
            "net_profit": (0.30, True),
            "revenue_yoy": (0.10, True),
            "profit_growth_adjusted": (0.25, True),
            "gross_margin": (0.15, True),
        },
    ),
    "scale": ModelSpec(
        label="规模型行业",
        metrics={
            "revenue": (0.30, True),
            "net_profit": (0.30, True),
            "revenue_yoy": (0.10, True),
            "profit_growth_adjusted": (0.15, True),
            "gross_margin": (0.15, True),
        },
    ),
}

MODEL_ALIASES = {
    "general": "general",
    "通用": "general",
    "通用行业": "general",
    "bank": "bank",
    "银行": "bank",
    "securities": "securities",
    "security": "securities",
    "broker": "securities",
    "证券": "securities",
    "券商": "securities",
    "insurance": "insurance",
    "保险": "insurance",
    "financial_report": "financial_report",
    "金融公告基础模型": "financial_report",
    "growth": "growth",
    "成长": "growth",
    "成长型行业": "growth",
    "innovative_drug": "innovative_drug",
    "创新药": "innovative_drug",
    "创新药行业": "innovative_drug",
    "创新药行业（对称利润增长）": "innovative_drug",
    "quality": "quality",
    "质量": "quality",
    "质量型行业": "quality",
    "cyclical": "cyclical",
    "周期": "cyclical",
    "周期型行业": "cyclical",
    "scale": "scale",
    "规模": "scale",
    "规模型行业": "scale",
}


def normalize_model_type(value: object) -> str:
    if pd.isna(value):
        return "general"
    return MODEL_ALIASES.get(str(value).strip().lower(), "general")


def prepare_growth_metrics(
    data: pd.DataFrame,
    as_of_date: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """用截止日已知的上年同期数据补齐增长率，并修正利润跨越盈亏时的失真。"""
    frame = data.copy()
    frame["report_period"] = pd.to_datetime(frame["report_period"], errors="coerce")
    if as_of_date is not None:
        if "announcement_date" not in frame:
            raise ValueError("按截止日计算增长率时缺少 announcement_date 字段")
        frame["announcement_date"] = pd.to_datetime(frame["announcement_date"], errors="coerce")
        frame = frame[
            frame["announcement_date"].notna()
            & frame["announcement_date"].le(pd.Timestamp(as_of_date).normalize())
        ].copy()
    for column in ("revenue", "net_profit", "revenue_yoy", "profit_yoy"):
        if column not in frame:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["_year"] = frame["report_period"].dt.year
    frame["_quarter"] = frame["report_period"].dt.quarter
    prior_source = frame.sort_values("announcement_date") if "announcement_date" in frame else frame
    prior = prior_source[["company_id", "_year", "_quarter", "revenue", "net_profit"]].copy()
    prior["_year"] = prior["_year"] + 1
    prior = prior.rename(columns={"revenue": "_prior_revenue", "net_profit": "_prior_profit"})
    prior = prior.drop_duplicates(["company_id", "_year", "_quarter"], keep="last")
    frame = frame.merge(prior, on=["company_id", "_year", "_quarter"], how="left")

    valid_revenue_base = frame["_prior_revenue"].notna() & frame["_prior_revenue"].ne(0)
    calculated_revenue_yoy = pd.Series(np.nan, index=frame.index, dtype=float)
    calculated_revenue_yoy.loc[valid_revenue_base] = (
        frame.loc[valid_revenue_base, "revenue"] - frame.loc[valid_revenue_base, "_prior_revenue"]
    ) / frame.loc[valid_revenue_base, "_prior_revenue"].abs()
    frame["revenue_yoy"] = calculated_revenue_yoy.combine_first(frame["revenue_yoy"])

    current_profit = frame["net_profit"]
    prior_profit = frame["_prior_profit"]
    valid_profit = current_profit.notna() & prior_profit.notna()
    normal_base = valid_profit & prior_profit.gt(0)
    nonpositive_base = valid_profit & prior_profit.le(0)
    calculated_profit_growth = pd.Series(np.nan, index=frame.index, dtype=float)
    calculated_profit_growth.loc[normal_base] = (
        current_profit.loc[normal_base] - prior_profit.loc[normal_base]
    ) / prior_profit.loc[normal_base].abs()
    denominator = current_profit.abs() + prior_profit.abs()
    symmetric_base = nonpositive_base & denominator.gt(0)
    calculated_profit_growth.loc[symmetric_base] = (
        2
        * (current_profit.loc[symmetric_base] - prior_profit.loc[symmetric_base])
        / denominator.loc[symmetric_base]
    )
    frame["profit_growth_adjusted"] = calculated_profit_growth.combine_first(frame["profit_yoy"])

    # 对称利润增长率：无论上年同期正负，统一用 2(a-b)/(|a|+|b|)，
    # 取值落在 [-2, 2]，亏损收窄与盈利增长可比，适合创新药这类经常跨盈亏的行业。
    symmetric_growth = pd.Series(np.nan, index=frame.index, dtype=float)
    symmetric_base = valid_profit & denominator.gt(0)
    symmetric_growth.loc[symmetric_base] = (
        2
        * (current_profit.loc[symmetric_base] - prior_profit.loc[symmetric_base])
        / denominator.loc[symmetric_base]
    )
    frame["profit_growth_symmetric"] = symmetric_growth

    return frame.drop(columns=["_year", "_quarter", "_prior_revenue", "_prior_profit"])


def select_snapshot(
    data: pd.DataFrame,
    as_of_date: str | pd.Timestamp,
    report_period: str | pd.Timestamp,
) -> pd.DataFrame:
    """严格按公告日截取指定报告期，避免使用当时尚未披露的数据。"""
    required = {"company_id", "report_period", "announcement_date"}
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(f"缺少快照字段：{', '.join(sorted(missing))}")

    frame = data.copy()
    frame["report_period"] = pd.to_datetime(frame["report_period"], errors="coerce")
    frame["announcement_date"] = pd.to_datetime(frame["announcement_date"], errors="coerce")
    as_of = pd.Timestamp(as_of_date).normalize()
    period = pd.Timestamp(report_period).normalize()
    frame = frame[
        frame["announcement_date"].notna()
        & frame["announcement_date"].le(as_of)
        & frame["report_period"].eq(period)
    ]
    frame = frame.sort_values("announcement_date")
    return frame.drop_duplicates(["company_id", "report_period"], keep="last").reset_index(drop=True)


def _percentile_score(values: pd.Series, higher_is_better: bool) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.dropna()
    result = pd.Series(np.nan, index=values.index, dtype=float)
    if valid.empty:
        return result
    if len(valid) == 1 or valid.nunique() == 1:
        result.loc[valid.index] = 50.0
        return result
    ranks = valid.rank(method="average", ascending=higher_is_better)
    result.loc[valid.index] = (ranks - 1) / (len(valid) - 1) * 100
    return result


def score_snapshot(
    snapshot: pd.DataFrame,
    top_n: int = 20,
    minimum_coverage: float = 0.60,
) -> pd.DataFrame:
    """在每个万得三级行业、每种模型内部进行横截面百分位评分。"""
    required = {"company_id", "industry_l3_id"}
    missing = required.difference(snapshot.columns)
    if missing:
        raise ValueError(f"缺少排名字段：{', '.join(sorted(missing))}")
    if not 0 <= minimum_coverage <= 1:
        raise ValueError("minimum_coverage 必须在 0 到 1 之间")
    if top_n < 1:
        raise ValueError("top_n 必须大于 0")

    frame = snapshot.copy()
    if "model_type" not in frame:
        frame["model_type"] = "general"
    frame["rank_model"] = frame["model_type"].map(normalize_model_type)

    scored_groups: list[pd.DataFrame] = []
    for (_, model_name), group in frame.groupby(
        ["industry_l3_id", "rank_model"], dropna=False, sort=False
    ):
        group = group.copy()
        spec = MODEL_SPECS[model_name]
        weighted_score = pd.Series(0.0, index=group.index)
        coverage = pd.Series(0.0, index=group.index)
        missing_names: dict[int, list[str]] = {index: [] for index in group.index}

        for metric, (weight, higher_is_better) in spec.metrics.items():
            values = (
                pd.to_numeric(group[metric], errors="coerce")
                if metric in group
                else pd.Series(np.nan, index=group.index)
            )
            metric_score = _percentile_score(values, higher_is_better)
            group[f"score_{metric}"] = metric_score
            weighted_score += weight * metric_score.fillna(50.0)
            coverage += weight * values.notna().astype(float)
            for index in group.index[values.isna()]:
                missing_names[index].append(metric)

        group["raw_weighted_score"] = weighted_score
        group["data_coverage"] = coverage
        group["weighted_score"] = weighted_score * (0.80 + 0.20 * coverage)
        group["eligible"] = coverage.ge(minimum_coverage)
        group["industry_company_count"] = len(group)
        group["rank_model_label"] = spec.label
        group["missing_metrics"] = [", ".join(missing_names[index]) for index in group.index]
        group["industry_rank"] = (
            group["weighted_score"].where(group["eligible"]).rank(method="min", ascending=False)
        )
        group["industry_rank"] = group["industry_rank"].astype("Int64")
        group["in_watchlist"] = group["eligible"] & group["industry_rank"].le(top_n)
        scored_groups.append(group)

    if not scored_groups:
        return frame.assign(
            raw_weighted_score=pd.Series(dtype=float),
            data_coverage=pd.Series(dtype=float),
            weighted_score=pd.Series(dtype=float),
            eligible=pd.Series(dtype=bool),
            industry_company_count=pd.Series(dtype=int),
            industry_rank=pd.Series(dtype="Int64"),
            in_watchlist=pd.Series(dtype=bool),
        )

    result = pd.concat(scored_groups, ignore_index=True)
    return result.sort_values(
        ["industry_l3_id", "rank_model", "industry_rank", "weighted_score"],
        ascending=[True, True, True, False],
        na_position="last",
    ).reset_index(drop=True)


def model_weight_table() -> pd.DataFrame:
    rows = []
    for model, spec in MODEL_SPECS.items():
        for metric, (weight, higher_is_better) in spec.metrics.items():
            rows.append(
                {
                    "model": model,
                    "model_label": spec.label,
                    "metric": metric,
                    "weight": weight,
                    "direction": "越高越好" if higher_is_better else "越低越好",
                }
            )
    return pd.DataFrame(rows)
