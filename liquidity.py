"""流动性指数：指标同向化 + 滚动 Z-score 标准化 + 权重合成。

方向约定：宽松 = 数值变大，收紧 = 数值变小。
- DR007、十年期国债收益率是资金价格，利率越高越紧，取负向（direction = -1）；
- 两融余额（水平）、两融余额同比、M1-M2 剪刀差、M2 同比、社融存量同比
  越高越宽松，取正向（direction = +1）。

标准化用滚动窗口 Z-score：周度指标 52 周（至少 26 周），月度指标 36 个月（至少 18 个月），
避免全样本标准化带来的前视偏差。

分层依据：短端只放价格类指标（利率与两融余额体量）；
两融余额同比是增速类（变化率），与货币信贷增速同类，归入信用货币增速指数。

合成权重（合计 1.0）：
- 短端资金价格指数（周度更新）：DR007 20%、十年国债 20%、两融余额 6%
- 信用货币增速指数（月频更新）：M1-M2 剪刀差 18%、M2 同比 12%、社融存量同比 18%、
  两融余额同比 6%
- 流动性指数 = 0.46×短端 + 0.54×信用货币增速（组权重由组内指标权重求和自动得出）。
组内某指标缺失时，该组用现有指标按权重归一化补位，并在 notes 中提示。
"""

from __future__ import annotations

import pandas as pd

GROUP_LABELS = {
    "short_term": "短端资金价格指数",
    "money": "信用货币增速指数",
}
INDEX_LABELS = {
    "liquidity_index": "流动性指数",
    "short_term_index": "短端资金价格指数",
    "money_index": "信用货币增速指数",
}

METRICS: dict[str, dict[str, object]] = {
    "dr007": {
        "label": "DR007",
        "frequency": "weekly",
        "direction": -1,
        "weight": 0.20,
        "group": "short_term",
    },
    "bond_10y": {
        "label": "十年期国债收益率",
        "frequency": "weekly",
        "direction": -1,
        "weight": 0.20,
        "group": "short_term",
    },
    "margin_balance": {
        "label": "两融余额",
        "frequency": "weekly",
        "direction": 1,
        "weight": 0.06,
        "group": "short_term",
    },
    "margin_balance_yoy": {
        "label": "两融余额同比",
        "frequency": "weekly",
        "direction": 1,
        "weight": 0.06,
        "group": "money",
    },
    "m1_m2_gap": {
        "label": "M1-M2剪刀差",
        "frequency": "monthly",
        "direction": 1,
        "weight": 0.18,
        "group": "money",
    },
    "m2_yoy": {
        "label": "M2同比增速",
        "frequency": "monthly",
        "direction": 1,
        "weight": 0.12,
        "group": "money",
    },
    "social_financing_yoy": {
        "label": "社融存量同比",
        "frequency": "monthly",
        "direction": 1,
        "weight": 0.18,
        "group": "money",
    },
}

ZSCORE_WINDOW_WEEKS = 52
ZSCORE_MIN_WEEKS = 26
ZSCORE_WINDOW_MONTHS = 36
ZSCORE_MIN_MONTHS = 18
MONTHLY_EFFECTIVE_DAYS = 10  # 月度数据在次月 10 日后视作已公布，避免前视

WEEKLY_YOY_LAGS = 52  # 两融余额同比：相对 52 周前


def resample_weekly(series: pd.Series) -> pd.Series:
    """日度序列转周度：取每周最后交易日（周五日历）的值。"""
    if series.empty:
        return series
    cleaned = series.sort_index()
    cleaned = cleaned[~cleaned.index.duplicated(keep="last")]
    return cleaned.resample("W-FRI").last().dropna()


def weekly_to_yoy(series: pd.Series, lags: int = WEEKLY_YOY_LAGS) -> pd.Series:
    """周度水平值转同比（%），相对 lags 周前。"""
    shifted = series.shift(lags)
    yoy = (series - shifted) / shifted.abs() * 100
    return yoy.where(shifted.notna() & shifted.ne(0))


def align_monthly_to_weekly(series: pd.Series, effective_days: int = MONTHLY_EFFECTIVE_DAYS) -> pd.Series:
    """月度序列（月末值）搬到周度日历：次月 effective_days 日后视作已公布。"""
    if series.empty:
        return series
    cleaned = series.sort_index()
    cleaned = cleaned[~cleaned.index.duplicated(keep="last")]
    effective = cleaned.copy()
    effective.index = effective.index + pd.DateOffset(days=effective_days)
    return effective


def directional(series: pd.Series, direction: int) -> pd.Series:
    """同向化：宽松 = 变大。利率类乘 -1。"""
    factor = 1.0 if int(direction) >= 0 else -1.0
    return series * factor


def rolling_zscore(
    series: pd.Series,
    window: int,
    min_periods: int | None = None,
) -> pd.Series:
    """滚动 Z-score：(x - 滚动均值) / 滚动标准差；标准差为 0 时留空。"""
    min_periods = min_periods or window
    rolling = series.rolling(window=window, min_periods=min_periods)
    mean = rolling.mean()
    std = rolling.std(ddof=1)
    zscore = (series - mean) / std
    return zscore.where(std.gt(0))


REFERENCE_BAND_LOW = 0.10
REFERENCE_BAND_HIGH = 0.90
REFERENCE_WINDOW_WEEKS = 52
REFERENCE_MIN_WEEKS = 26
REFERENCE_WINDOW_MONTHS = 12
REFERENCE_MIN_MONTHS = 6


def rolling_reference(series: pd.Series, window: int, min_periods: int) -> pd.DataFrame:
    """滚动均值与 80% 区间（10%~90% 分位），作为指数图上的参考带。"""
    rolling = series.rolling(window=window, min_periods=min_periods)
    return pd.DataFrame(
        {
            "滚动均值": rolling.mean(),
            "区间上轨(90%分位)": rolling.quantile(REFERENCE_BAND_HIGH),
            "区间下轨(10%分位)": rolling.quantile(REFERENCE_BAND_LOW),
        }
    )


def prepare_metric_series(metric_id: str, series: pd.Series) -> pd.Series:
    """把原始序列整理为周度、同向化后的口径（未标准化）。

    两融余额同比是派生指标：输入应是 derive_margin_yoy 产出的周度同比序列，
    这里只做重采样与同向化。
    """
    spec = METRICS[metric_id]
    frequency = str(spec["frequency"])
    if str(frequency) == "weekly":
        series = resample_weekly(series)
    else:
        series = align_monthly_to_weekly(series)
    return directional(series, int(spec["direction"]))


def derive_margin_yoy(level_series: pd.Series) -> pd.Series:
    """由两融余额（日度水平，亿元）派生周度同比（%）。"""
    return weekly_to_yoy(resample_weekly(level_series))


def prepare_metric_display_series(metric_id: str, series: pd.Series) -> pd.Series:
    """整理为与指数同源的口径，但保留原始方向与单位，用于展示原始曲线。

    仅用于展示、不参与合成的指标（如社融存量）不在 METRICS 中，按月原值返回。
    """
    spec = METRICS.get(metric_id)
    if spec is None:
        return series.sort_index()
    return prepare_metric_series(metric_id, series) * int(spec["direction"])


def _weighted_sum(weights: dict[str, float], zscores: dict[str, pd.Series]) -> pd.Series | None:
    """按归一化权重合成；全部缺失时返回 None。"""
    available = {key: value for key, value in zscores.items() if value is not None and not value.dropna().empty}
    if not available:
        return None
    total_weight = sum(weights[key] for key in available)
    combined: pd.Series | None = None
    for key, zscore in available.items():
        part = zscore * (weights[key] / total_weight)
        combined = part if combined is None else combined.add(part, fill_value=float("nan"))
    return combined


def compute_liquidity_indices(
    metrics: dict[str, pd.Series],
    *,
    now: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """计算三个指数。

    返回 (indices, components, notes)：
    - indices：周五日历，列 liquidity_index / short_term_index / money_index
    - components：每个指标一行，含最新原始值、Z 值、数据覆盖与缺失标记
    - notes：缺失指标、窗口不足等提示
    """
    notes: list[str] = []
    working = dict(metrics)
    # 两融余额同比从两融余额（水平）派生；直接传入同比序列时使用原值
    if "margin_balance_yoy" not in working and "margin_balance" in working:
        working["margin_balance_yoy"] = derive_margin_yoy(working["margin_balance"])
    prepared: dict[str, pd.Series] = {}
    for metric_id, series in working.items():
        if metric_id not in METRICS or series is None or series.dropna().empty:
            continue
        prepared[metric_id] = prepare_metric_series(metric_id, series)

    missing = [metric_id for metric_id in METRICS if metric_id not in prepared]
    for metric_id in missing:
        label = str(METRICS[metric_id]["label"])
        mode = "待手动导入" if metric_id == "social_financing_yoy" else "暂无缓存"
        notes.append(f"{label}：{mode}，参与合成时其权重已在组内按现有指标归一化。")

    weekly_frames = {
        metric_id: series
        for metric_id, series in prepared.items()
        if str(METRICS[metric_id]["frequency"]) == "weekly"
    }
    if not weekly_frames:
        components = build_component_table({}, {}, prepared)
        return pd.DataFrame(), components, notes

    calendar = pd.DatetimeIndex(sorted(set().union(*(series.index for series in weekly_frames.values()))))
    aligned: dict[str, pd.Series] = {}
    monthly_zscores: dict[str, pd.Series] = {}
    for metric_id, series in prepared.items():
        if str(METRICS[metric_id]["frequency"]) == "weekly":
            aligned[metric_id] = series.reindex(calendar)
            continue
        # 月度生效日大多不落在周五上，reindex 精确匹配会把这些月份整月丢掉，
        # 必须用 method="ffill" 按「生效日 ≤ 本周五」取最近一个月的值
        aligned[metric_id] = series.reindex(calendar, method="ffill")
        # Z 值在月度生效序列（每月一个观测）上按 36 个月窗口计算，再对齐到周五日历；
        # 若对齐后再按周五观测数滚动，月内重复观测会把 std 压到接近 0，
        # 新月份数据一生效 z 就会被放大成 5σ 以上的假跳变
        monthly_zscores[metric_id] = rolling_zscore(
            series, ZSCORE_WINDOW_MONTHS, ZSCORE_MIN_MONTHS
        )

    zscores: dict[str, pd.Series] = {}
    for metric_id, series in aligned.items():
        if str(METRICS[metric_id]["frequency"]) == "weekly":
            zscores[metric_id] = rolling_zscore(
                series, ZSCORE_WINDOW_WEEKS, ZSCORE_MIN_WEEKS
            )
        else:
            zscores[metric_id] = monthly_zscores[metric_id].reindex(calendar, method="ffill")

    group_frames: dict[str, pd.Series] = {}
    for group in ("short_term", "money"):
        weights = {
            metric_id: float(METRICS[metric_id]["weight"])
            for metric_id in METRICS
            if METRICS[metric_id]["group"] == group
        }
        zscores_in_group = {
            metric_id: zscores.get(metric_id) for metric_id in weights if metric_id in zscores
        }
        combined = _weighted_sum(weights, zscores_in_group)
        if combined is not None:
            group_frames[group] = combined.dropna()
        else:
            notes.append(f"{GROUP_LABELS[group]}：组内还没有可通过窗口检验的数据。")

    # 总指数固定为两个子指数的组合，组权重 = 组内指标权重之和（当前 0.46 短端 + 0.54 信用），
    # 保证口径恒一致；组内某指标缺失时该组已按现有指标归一化，因此组合式始终成立
    group_weights = {
        group: sum(
            float(spec["weight"]) for spec in METRICS.values() if str(spec["group"]) == group
        )
        for group in ("short_term", "money")
    }
    group_parts = []
    if "short_term" in group_frames:
        group_parts.append((group_weights["short_term"], group_frames["short_term"]))
    if "money" in group_frames:
        group_parts.append((group_weights["money"], group_frames["money"]))
    liquidity = None
    if group_parts:
        total_weight = sum(weight for weight, _ in group_parts)
        for weight, series in group_parts:
            part = series * (weight / total_weight)
            liquidity = part if liquidity is None else liquidity.add(part, fill_value=float("nan"))

    indices = pd.DataFrame(index=calendar)
    indices.index.name = "date"
    if liquidity is not None and not liquidity.dropna().empty:
        indices["liquidity_index"] = liquidity
    if "short_term" in group_frames:
        indices["short_term_index"] = group_frames["short_term"]
    if "money" in group_frames:
        indices["money_index"] = group_frames["money"]
    indices = indices.dropna(how="all")

    components = build_component_table(aligned, zscores, prepared, now=now)
    return indices, components, notes


def build_component_table(
    aligned: dict[str, pd.Series],
    zscores: dict[str, pd.Series],
    prepared: dict[str, pd.Series],
    *,
    now: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """每个指标一行的成分表：权重、方向、最新原始值、Z 值、数据区间。"""
    rows: list[dict] = []
    for metric_id, spec in METRICS.items():
        raw_series = prepared.get(metric_id)
        if raw_series is None or raw_series.dropna().empty:
            rows.append(
                {
                    "metric": metric_id,
                    "指标": str(spec["label"]),
                    "分组": GROUP_LABELS[str(spec["group"])],
                    "方向": "越高越宽松" if int(spec["direction"]) >= 0 else "越高越紧",
                    "权重": f"{float(spec['weight']):.0%}",
                    "最新值": "—",
                    "Z值": "—",
                    "数据区间": "无数据",
                }
            )
            continue
        raw_series = raw_series.dropna()
        if now is not None:
            raw_series = raw_series[raw_series.index.le(pd.Timestamp(now))]
        latest_raw = raw_series.iloc[-1]
        zscore = zscores.get(metric_id)
        latest_z = "—" if zscore is None or zscore.dropna().empty else f"{zscore.dropna().iloc[-1]:.2f}"
        rows.append(
            {
                "metric": metric_id,
                "指标": str(spec["label"]),
                "分组": GROUP_LABELS[str(spec["group"])],
                "方向": "越高越宽松" if int(spec["direction"]) >= 0 else "越高越紧",
                "权重": f"{float(spec['weight']):.0%}",
                "最新值": f"{latest_raw:.2f}",
                "Z值": latest_z,
                "数据区间": f"{raw_series.index.min():%Y-%m-%d} → {raw_series.index.max():%Y-%m-%d}",
            }
        )
    return pd.DataFrame(rows)


def summarize_index(series: pd.Series, *, window: int = 52) -> dict[str, object] | None:
    """单个指数的最新值、前值、变化、近 window 期分位与趋势描述。"""
    cleaned = series.dropna()
    if cleaned.empty:
        return None
    latest = float(cleaned.iloc[-1])
    previous = float(cleaned.iloc[-2]) if len(cleaned) >= 2 else None
    recent = cleaned.iloc[-window:]
    percentile = float((recent <= latest).mean()) if len(recent) >= 5 else None
    change = None if previous is None else latest - previous
    if change is None:
        trend = "样本不足，暂无法判断方向"
    elif change > 0.05:
        trend = "较上期明显宽松化"
    elif change < -0.05:
        trend = "较上期明显收紧"
    else:
        trend = "较上期基本持平"
    return {
        "latest": latest,
        "previous": previous,
        "change": change,
        "percentile": percentile,
        "trend": trend,
        "as_of": cleaned.index[-1],
    }
