from __future__ import annotations

from datetime import date
from typing import Iterable

import pandas as pd


DEFAULT_ALL_A_SECTOR = "a001010100000000"


class WindProviderError(RuntimeError):
    pass


def _wind_result_to_frame(result: object) -> pd.DataFrame:
    error_code = getattr(result, "ErrorCode", -1)
    if error_code != 0:
        raise WindProviderError(f"Wind API 返回错误码 {error_code}")
    fields = [str(field).lower() for field in getattr(result, "Fields", [])]
    data = getattr(result, "Data", [])
    if not fields or not data:
        return pd.DataFrame()
    return pd.DataFrame({field: values for field, values in zip(fields, data)})


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def fetch_official_wind_snapshot(
    effective_date: str | date,
    industry_id_field: str,
    industry_name_field: str,
    market_cap_field: str = "mkt_cap_ard",
    all_a_sector: str = DEFAULT_ALL_A_SECTOR,
    chunk_size: int = 1000,
) -> pd.DataFrame:
    """通过已授权 WindPy 获取全部 A 股及三级行业；字段名须从当前终端代码生成器确认。"""
    if not industry_id_field or not industry_name_field:
        raise ValueError("必须提供当前 Wind 许可中的三级行业 ID 字段和名称字段")
    try:
        from WindPy import w  # type: ignore[import-not-found]
    except ImportError as exc:
        raise WindProviderError("未安装 WindPy；请在已安装并授权万得终端的 Python 环境运行") from exc

    started = w.start()
    if getattr(started, "ErrorCode", -1) != 0:
        raise WindProviderError(f"WindPy 启动失败，错误码 {getattr(started, 'ErrorCode', -1)}")

    trade_date = pd.Timestamp(effective_date).strftime("%Y-%m-%d")
    sector_result = w.wset(
        "sectorconstituent",
        f"date={trade_date};windcode={all_a_sector}",
    )
    sector = _wind_result_to_frame(sector_result)
    code_column = next((column for column in sector.columns if column in {"wind_code", "windcode"}), None)
    name_column = next((column for column in sector.columns if column in {"sec_name", "secname"}), None)
    if code_column is None:
        raise WindProviderError(f"Wind 全 A 成分接口未返回证券代码，实际字段：{list(sector.columns)}")

    codes = sector[code_column].dropna().astype(str).tolist()
    names = dict(zip(sector[code_column].astype(str), sector[name_column].astype(str))) if name_column else {}
    rows: list[pd.DataFrame] = []
    requested_fields = [industry_id_field, industry_name_field, market_cap_field]
    for batch in _chunks(codes, chunk_size):
        result = w.wss(
            ",".join(batch),
            ",".join(requested_fields),
            f"tradeDate={trade_date};unit=1",
        )
        frame = _wind_result_to_frame(result)
        returned_codes = [str(code) for code in getattr(result, "Codes", batch)]
        frame["company_id"] = returned_codes
        rows.append(frame)

    if not rows:
        raise WindProviderError("Wind API 未返回任何 A 股公司")
    output = pd.concat(rows, ignore_index=True)
    output = output.rename(
        columns={
            industry_id_field.lower(): "industry_l3_id",
            industry_name_field.lower(): "industry_l3_name",
            market_cap_field.lower(): "market_cap",
        }
    )
    output["company_name"] = output["company_id"].map(names).fillna(output["company_id"])
    required = {"industry_l3_id", "industry_l3_name", "company_id", "company_name"}
    missing = required.difference(output.columns)
    if missing:
        raise WindProviderError(f"Wind 指标字段无效，缺少结果列：{', '.join(sorted(missing))}")
    return output
