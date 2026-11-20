"""三因子计算与滚动分位标准化（严格只用当日及之前数据）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import RegimeConfig
from .data_loader import PriceBundle

FACTOR_COLUMNS = [
    "date",
    "code",
    "name",
    "close",
    "ma_short",
    "ma_long",
    "f1",
    "f2",
    "f3",
    "z1",
    "z2",
    "z3",
    "above_ma60",
    "insufficient",
]


def _rolling_z(series: pd.Series, window: int) -> pd.Series:
    """当前值在过去 window 根（含当日）中的百分位，映射到 [-1, 1]。"""
    percentile = series.rolling(window=window, min_periods=1).rank(pct=True)
    return 2.0 * percentile - 1.0


def compute_factors(bundle: PriceBundle, config: RegimeConfig) -> pd.DataFrame:
    """按 4.1~4.2 节计算 f1/f2/f3 与标准化后的 z1/z2/z3。"""
    windows = config.windows
    ma_short = int(windows["ma_short"])
    ma_long = int(windows["ma_long"])
    slope_short = int(windows["slope_short"])
    slope_long = int(windows["slope_long"])
    percentile_window = int(windows["percentile"])
    warmup = max(ma_long + slope_long - 1, percentile_window - 1)

    rows: list[pd.DataFrame] = []
    for item in config.indices:
        code = str(item["code"])
        frame = bundle.prices[code]
        close = frame["close"]
        fast = close.rolling(ma_short, min_periods=ma_short).mean()
        slow = close.rolling(ma_long, min_periods=ma_long).mean()
        f1 = (close - fast) / fast
        f2 = fast / fast.shift(slope_short) - 1.0
        f3 = slow / slow.shift(slope_long) - 1.0
        block = pd.DataFrame(
            {
                "date": frame.index,
                "code": code,
                "name": config.name_of(code),
                "close": close.to_numpy(),
                "ma_short": fast.to_numpy(),
                "ma_long": slow.to_numpy(),
                "f1": f1.to_numpy(),
                "f2": f2.to_numpy(),
                "f3": f3.to_numpy(),
                "above_ma60": (close > fast).fillna(False).to_numpy(),
            }
        )
        for key, series in (("z1", f1), ("z2", f2), ("z3", f3)):
            block[key] = _rolling_z(series, percentile_window).to_numpy()
        block["insufficient"] = np.arange(len(block)) < warmup
        rows.append(block[FACTOR_COLUMNS])

    result = pd.concat(rows, ignore_index=True)
    result["date"] = pd.to_datetime(result["date"])
    return result.sort_values(["date", "code"]).reset_index(drop=True)
