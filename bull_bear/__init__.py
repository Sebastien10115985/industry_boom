"""A股牛熊状态评分算法（五宽基指数、三因子、分位标准化、广度调节）。"""

from .config import DEFAULT_CONFIG_PATH, RegimeConfig, load_config
from .data_loader import IndexDataError, PriceBundle, load_prices
from .factors import compute_factors
from .scoring import (
    compute_composite,
    compute_group_scores,
    compute_index_scores,
    state_of,
)
from .calibration import calibrate_series
from .report import build_report

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "RegimeConfig",
    "load_config",
    "IndexDataError",
    "PriceBundle",
    "load_prices",
    "compute_factors",
    "compute_index_scores",
    "compute_group_scores",
    "compute_composite",
    "state_of",
    "calibrate_series",
    "build_report",
]
