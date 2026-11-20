"""市场牛熊状态：后台算法入口。

按 doc/A股牛熊状态评分算法.md 计算五个宽基指数的分值并合成综合分。
页面只读取结果，不再提供人工设置入口。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bull_bear import (
    DEFAULT_CONFIG_PATH,
    compute_composite,
    compute_factors,
    compute_group_scores,
    compute_index_scores,
    load_config,
    load_prices,
)
from bull_bear.report import build_report
from macro_risk import load_regime, save_regime

DEFAULT_REGIME = -80.0


def _persist(value: float) -> None:
    """把自动计算结果写回全局缓存，供其他页面与会话读取。"""
    try:
        if abs(value - load_regime()) >= 0.5:
            save_regime(value, is_admin=True)
    except Exception:
        # 缓存写失败不影响本次计算结果展示
        pass


def compute_full_report(
    force_refresh: bool = False,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """拉取/读取指数数据，完整计算并返回结果表、历史序列与校准摘要。"""
    config = load_config(config_path)
    bundle = load_prices(config, force_refresh=force_refresh)
    factors = compute_factors(bundle, config)
    scores = compute_index_scores(factors, config)
    groups = compute_group_scores(scores, config)
    composite = compute_composite(scores, factors, groups, config)
    report = build_report(bundle, factors, scores, groups, composite, config)
    _persist(float(report["latest"]["score"]))
    return report


def compute_regime(force_refresh: bool = False) -> float:
    """返回当前全市场综合分（-100 ~ 100）。"""
    return float(compute_full_report(force_refresh=force_refresh)["latest"]["score"])
