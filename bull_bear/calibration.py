"""阈值校准：三态时间占比、状态切换次数与建议阈值。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import RegimeConfig
from .scoring import state_of

TARGET_SHARES = {"BEAR": 0.275, "RANGE": 0.45, "BULL": 0.275}


def _shares(states: pd.Series) -> dict[str, float]:
    total = max(1, len(states))
    counts = states.value_counts()
    return {key: round(float(counts.get(key, 0)) / total, 4) for key in TARGET_SHARES}


def _switches(states: pd.Series) -> int:
    return int((states != states.shift(1)).sum() - 1) if len(states) > 1 else 0


def _grid_search(values: pd.Series, target: dict[str, float], step: float) -> dict[str, float]:
    best: tuple[float, dict[str, float]] | None = None
    for bear in np.arange(-60.0, -4.0, step):
        for bull in np.arange(4.0, 60.0, step):
            bounds = {"bear": float(bear), "bull": float(bull)}
            shares = _shares(values.apply(lambda value: state_of(value, bounds)))
            loss = sum(abs(shares[key] - target[key]) for key in target)
            if best is None or loss < best[0]:
                best = (loss, bounds)
    return best[1] if best else {"bear": -20.0, "bull": 20.0}


def calibrate_series(
    values: pd.Series,
    current_bounds: dict[str, float],
    target: dict[str, float] | None = None,
    step: float = 1.0,
) -> dict:
    """全样本网格搜索建议阈值，并用 70/30 样本外区间复核。"""
    target = target or TARGET_SHARES
    clean = pd.Series(values).dropna()
    if clean.empty:
        empty = _shares(pd.Series(dtype=str))
        return {
            "shares": empty,
            "switches": 0,
            "suggested": dict(current_bounds),
            "suggested_shares": empty,
            "walk_forward": {"bounds": dict(current_bounds), "shares": empty},
            "samples": 0,
        }

    current_states = clean.apply(lambda value: state_of(value, current_bounds))
    suggested = _grid_search(clean, target, step)
    split = int(len(clean) * 0.7)
    train, test = clean.iloc[:split], clean.iloc[split:]
    wf_bounds = _grid_search(train, target, step)
    test_states = test.apply(lambda value: state_of(value, wf_bounds)) if not test.empty else pd.Series(dtype=str)
    return {
        "shares": _shares(current_states),
        "switches": _switches(current_states),
        "suggested": suggested,
        "suggested_shares": _shares(clean.apply(lambda value: state_of(value, suggested))),
        "walk_forward": {"bounds": wf_bounds, "shares": _shares(test_states)},
        "samples": int(len(clean)),
    }


def calibration_summary(scores: pd.DataFrame, composite: pd.DataFrame, config: RegimeConfig) -> dict[str, dict]:
    """逐个指数、分组和综合分给出校准结果。"""
    summary: dict[str, dict] = {}
    for item in config.indices:
        code = str(item["code"])
        subset = scores[scores["code"] == code].set_index("date")["score"].sort_index()
        summary[config.name_of(code)] = calibrate_series(subset, config.thresholds["index"])
    for group in config.groups:
        summary[_group_label(group)] = calibrate_series(
            composite[f"group_{group}"], config.thresholds.get(group, config.thresholds["index"])
        )
    summary["综合分"] = calibrate_series(composite["score"], config.thresholds["composite"])
    return summary


def _group_label(group: str) -> str:
    return {"large_value": "大盘价值", "mid500": "中证500", "growth": "成长"}.get(group, group)
