"""按 config/bull_bear.json 的 target_shares 重算并写回阈值。

修改三态目标占比后运行：
    python tools/recalibrate_bull_bear.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bull_bear import (  # noqa: E402
    compute_composite,
    compute_factors,
    compute_group_scores,
    compute_index_scores,
    load_config,
    load_prices,
)
from bull_bear.calibration import suggest_thresholds  # noqa: E402


def main() -> None:
    config = load_config()
    bundle = load_prices(config)
    factors = compute_factors(bundle, config)
    scores = compute_index_scores(factors, config)
    groups = compute_group_scores(scores, config)
    composite = compute_composite(scores, factors, groups, config)

    suggestions = suggest_thresholds(scores, composite, config)
    config.thresholds.update(suggestions)
    config.save()

    print("目标三态占比：", config.target_shares)
    for key, value in suggestions.items():
        print(f"{key}: bear={value['bear']:.0f}, bull={value['bull']:.0f}")
    print("已写回配置：config/bull_bear.json")


if __name__ == "__main__":
    main()
