"""五个宽基指数的日线拉取、缓存、代码校验与日期对齐。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import RegimeConfig

INDEX_COLUMNS = ["open", "high", "low", "close", "volume"]
_COLUMN_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
}


class IndexDataError(RuntimeError):
    """指数数据采集、校验或对齐失败。"""


@dataclass
class PriceBundle:
    prices: dict[str, pd.DataFrame]
    fills: dict[str, pd.Series]
    warnings: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)


Fetcher = Callable[[str, str, str | None], pd.DataFrame]


def _normalise(frame: pd.DataFrame, code: str) -> pd.DataFrame:
    data = frame.rename(columns=_COLUMN_MAP).copy()
    if "date" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
        data = data.reset_index()
        if "date" not in data.columns:
            data = data.rename(columns={data.columns[0]: "date"})
    missing = [column for column in ["date", *INDEX_COLUMNS] if column not in data.columns]
    if missing:
        raise IndexDataError(f"{code} 返回字段缺失：{missing}")
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data = data.dropna(subset=["date"])
    for column in INDEX_COLUMNS:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.sort_values("date").drop_duplicates("date", keep="last").set_index("date")
    if data.empty:
        raise IndexDataError(f"{code} 返回空数据")
    return data[INDEX_COLUMNS]


def default_fetcher(code: str, start_date: str, end_date: str | None = None) -> pd.DataFrame:
    """akshare 东方财富指数日线；end_date 为空表示今天。"""
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise IndexDataError("缺少 akshare，请先安装 requirements.txt") from exc
    end = end_date or date.today().strftime("%Y%m%d")
    try:
        frame = ak.index_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end,
        )
    except Exception as exc:
        # 只保留原因，代码与中文注释由 load_prices 统一添加，避免重复前缀
        raise IndexDataError(str(exc)) from exc
    return _normalise(frame, code)


def validate_index_names(config: RegimeConfig) -> list[str]:
    """用 akshare 指数名录核对「代码 ↔ 名称」；无法访问名录时返回警告而不阻断缓存读取。"""
    try:
        import akshare as ak
    except ImportError:  # pragma: no cover
        return ["缺少 akshare，跳过指数代码名称校验"]
    try:
        info = ak.index_stock_info()
    except Exception as exc:
        return [f"指数名录暂不可用，跳过代码名称校验：{exc}"]
    mapping = dict(zip(info["index_code"].astype(str), info["display_name"].astype(str)))
    problems: list[str] = []
    for item in config.indices:
        actual = mapping.get(str(item["code"]))
        if actual is None:
            problems.append(f"指数名录缺少代码 {item['code']}")
        elif actual != item["name"]:
            problems.append(f"指数代码错配：{item['code']} 名录为 {actual}，配置为 {item['name']}")
    if problems:
        raise IndexDataError("；".join(problems))
    return []


def _cache_path(config: RegimeConfig, code: str) -> Path:
    return config.cache_dir / f"{code}.csv"


def _read_cache(path: Path, code: str) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, dtype={"date": str}, encoding="utf-8-sig")
    except Exception as exc:
        raise IndexDataError(f"{code} 本地缓存读取失败：{exc}") from exc
    return _normalise(frame, code)


def _write_cache(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = frame.reset_index()[["date", *INDEX_COLUMNS]]
    out.to_csv(path, index=False, encoding="utf-8-sig")


def _long_gaps(mask: pd.Series, limit: int = 5) -> list[str]:
    gaps: list[str] = []
    run = 0
    start: pd.Timestamp | None = None
    for timestamp, missing in mask.items():
        if bool(missing):
            if run == 0:
                start = timestamp
            run += 1
        elif run > limit:
            gaps.append(f"{start.date()} 起连续缺失 {run} 日")
            run = 0
        else:
            run = 0
    if run > limit and start is not None:
        gaps.append(f"{start.date()} 起连续缺失 {run} 日")
    return gaps


def load_prices(
    config: RegimeConfig,
    force_refresh: bool = False,
    fetcher: Fetcher | None = None,
    check_names: bool = True,
) -> PriceBundle:
    """读取（或拉取）五个指数，取交易日交集并前向填充，返回对齐后的价格。

    force_refresh 为 True 时优先重新拉取；若行情服务器限流/节假日无数据导致拉取失败，
    且本地已有该指数缓存，则改用历史缓存并在 warnings 中说明；只有本地也没有缓存时才抛错。
    """
    fetch = fetcher or default_fetcher
    warnings: list[str] = []
    if check_names:
        warnings.extend(validate_index_names(config))

    raw: dict[str, pd.DataFrame] = {}
    fetched: list[str] = []
    end = date.today().strftime("%Y%m%d")
    for item in config.indices:
        code = str(item["code"])
        path = _cache_path(config, code)
        if not force_refresh and path.exists():
            raw[code] = _read_cache(path, code)
            continue
        label = f"`{code}`（{config.name_of(code)}）"
        try:
            frame = fetch(code, config.start_date, end)
        except Exception as exc:
            if path.exists():
                raw[code] = _read_cache(path, code)
                warnings.append(f"{label} 行情暂不可用，已使用本地历史缓存：{exc}")
                continue
            raise IndexDataError(f"{label} 行情拉取失败：{exc}") from exc
        raw[code] = _normalise(frame, code)
        _write_cache(path, raw[code])
        fetched.append(code)

    common = None
    for frame in raw.values():
        common = frame.index if common is None else common.intersection(frame.index)
    if common is None or common.empty:
        raise IndexDataError("五个指数没有共同交易日，无法对齐")
    common = common.sort_values()

    prices: dict[str, pd.DataFrame] = {}
    fills: dict[str, pd.Series] = {}
    for code, frame in raw.items():
        aligned = frame.reindex(common)
        missing = aligned[INDEX_COLUMNS].isna().any(axis=1)
        fills[code] = missing
        prices[code] = aligned.ffill()
        if prices[code][INDEX_COLUMNS].isna().any().any():
            raise IndexDataError(f"{code} 前向填充后仍有缺失值")
        warnings.extend(f"{config.name_of(code)}：{gap}" for gap in _long_gaps(missing))

    warmup = config.windows["ma_long"] + config.windows["slope_long"]
    if len(common) < max(warmup, config.windows["percentile"]):
        warnings.append(
            f"样本不足：共 {len(common)} 根K线，分位窗口需 {config.windows['percentile']} 根，已标记但继续计算"
        )
    return PriceBundle(prices=prices, fills=fills, warnings=warnings, fetched=fetched)
