"""单指数分值、分组分值、广度因子与综合分。"""

from __future__ import annotations

import pandas as pd

from .config import RegimeConfig

STATE_LABELS = {
    "BEAR": "熊市",
    "RANGE": "震荡市",
    "BULL": "牛市",
    "INSUFFICIENT": "样本不足",
}


def state_of(value: float, bounds: dict[str, float]) -> str:
    """按阈值判定状态；训练不足时由调用方改为 INSUFFICIENT。"""
    if pd.isna(value):
        return "INSUFFICIENT"
    if value < float(bounds["bear"]):
        return "BEAR"
    if value > float(bounds["bull"]):
        return "BULL"
    return "RANGE"


def compute_index_scores(factors: pd.DataFrame, config: RegimeConfig) -> pd.DataFrame:
    """按 4.3 节计算每个指数每天的 S_i。"""
    result = factors.copy()
    weights = config.weights
    result["score"] = 100.0 * (
        float(weights["f1"]) * result["z1"]
        + float(weights["f2"]) * result["z2"]
        + float(weights["f3"]) * result["z3"]
    )
    result["score"] = result["score"].clip(-100.0, 100.0)
    bounds = config.thresholds["index"]
    result["state"] = [
        "INSUFFICIENT" if insufficient else state_of(value, bounds)
        for value, insufficient in zip(result["score"], result["insufficient"])
    ]
    return result


def compute_group_scores(scores: pd.DataFrame, config: RegimeConfig) -> pd.DataFrame:
    """大盘价值 / 中证500 / 成长三个分组各自的分值与状态。"""
    wide = scores.pivot(index="date", columns="code", values="score")
    frames: list[pd.DataFrame] = []
    for group, members in config.groups.items():
        present = [code for code in members if code in wide.columns]
        if not present:
            continue
        value = wide[present].mean(axis=1, skipna=True)
        bounds = config.thresholds.get(group, config.thresholds["index"])
        frames.append(
            pd.DataFrame(
                {
                    "date": wide.index,
                    "group": group,
                    "label": _group_label(group),
                    "score": value.to_numpy(),
                    "state": [state_of(item, bounds) for item in value],
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=["date", "group", "label", "score", "state"])
    return pd.concat(frames, ignore_index=True).sort_values(["date", "group"]).reset_index(drop=True)


def _group_label(group: str) -> str:
    return {"large_value": "大盘价值", "mid500": "中证500", "growth": "成长"}.get(group, group)


def _confirm_states(states: pd.Series, days: int) -> pd.Series:
    """新状态连续保持 days 根后才确认切换，避免频繁翻转。"""
    days = max(1, int(days))
    confirmed: list[str] = []
    current = states.iloc[0]
    streak = 0
    previous = current
    for value in states:
        streak = streak + 1 if value == previous else 1
        previous = value
        if streak >= days:
            current = value
        confirmed.append(current)
    return pd.Series(confirmed, index=states.index)


def compute_composite(
    scores: pd.DataFrame,
    factors: pd.DataFrame,
    groups: pd.DataFrame,
    config: RegimeConfig,
) -> pd.DataFrame:
    """按 4.4~4.6 节计算 S_base、广度、z_b 与最终综合分。"""
    group_wide = groups.pivot(index="date", columns="group", values="score")
    s_base = pd.Series(0.0, index=group_wide.index)
    for group, weight in config.group_weights.items():
        if group in group_wide.columns:
            s_base += float(weight) * group_wide[group].fillna(0.0)

    breadth = factors.groupby("date")["above_ma60"].mean().astype(float)
    z_breadth = 2.0 * breadth - 1.0
    composite = s_base * (1.0 + float(config.breadth_multiplier) * z_breadth)
    composite = composite.clip(-100.0, 100.0)

    smoothing = config.smoothing
    if bool(smoothing.get("ema_enabled", False)):
        composite = composite.ewm(span=int(smoothing.get("ema_days", 5)), adjust=False).mean()

    bounds = config.thresholds["composite"]
    states = composite.apply(lambda value: state_of(value, bounds))

    insufficient = scores.pivot(index="date", columns="code", values="insufficient").any(axis=1)
    states = states.where(~insufficient, "INSUFFICIENT")
    if int(smoothing.get("confirm_days", 0)) > 0:
        states = _confirm_states(states, int(smoothing["confirm_days"]))

    result = pd.DataFrame(
        {
            "date": composite.index,
            "s_base": s_base.to_numpy(),
            "breadth": breadth.reindex(composite.index).to_numpy(),
            "z_b": z_breadth.reindex(composite.index).to_numpy(),
            "score": composite.to_numpy(),
            "state": states.to_numpy(),
        }
    )
    for group in config.groups:
        if group in group_wide.columns:
            result[f"group_{group}"] = group_wide[group].reindex(result["date"]).to_numpy()
    return result.sort_values("date").reset_index(drop=True)
