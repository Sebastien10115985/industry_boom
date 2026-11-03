from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd


COLUMN_ALIASES = {
    "证券代码": "company_id",
    "股票代码": "company_id",
    "wind代码": "company_id",
    "万得代码": "company_id",
    "公司名称": "company_name",
    "证券简称": "company_name",
    "三级行业id": "industry_l3_id",
    "万得三级行业id": "industry_l3_id",
    "行业id": "industry_l3_id",
    "三级行业名称": "industry_l3_name",
    "万得三级行业名称": "industry_l3_name",
    "报告期": "report_period",
    "公告日期": "announcement_date",
    "市值": "market_cap",
    "总市值": "market_cap",
    "营业收入": "revenue",
    "营收": "revenue",
    "净利润": "net_profit",
    "营收同比": "revenue_yoy",
    "营业收入同比增长率": "revenue_yoy",
    "利润同比": "profit_yoy",
    "净利润同比增长率": "profit_yoy",
    "毛利率": "gross_margin",
    "模型类型": "model_type",
    "公司类型": "model_type",
    "净资产收益率": "roe",
    "roe": "roe",
    "净息差": "net_interest_margin",
    "不良贷款率": "npl_ratio",
    "拨备覆盖率": "provision_coverage",
    "核心一级资本充足率": "cet1_ratio",
    "成本收入比": "cost_income_ratio",
    "市净率": "pb",
    "净资本比率": "net_capital_ratio",
    "风险覆盖率": "risk_coverage_ratio",
    "营业利润率": "operating_margin",
    "保费增长率": "premium_growth",
    "综合偿付能力充足率": "solvency_ratio",
    "综合成本率": "combined_ratio",
    "投资收益率": "investment_yield",
}

NUMERIC_COLUMNS = {
    "market_cap",
    "revenue",
    "net_profit",
    "revenue_yoy",
    "profit_yoy",
    "gross_margin",
    "roe",
    "net_interest_margin",
    "npl_ratio",
    "provision_coverage",
    "cet1_ratio",
    "cost_income_ratio",
    "pb",
    "net_capital_ratio",
    "risk_coverage_ratio",
    "operating_margin",
    "premium_growth",
    "solvency_ratio",
    "combined_ratio",
    "investment_yield",
}

BASE_COLUMNS = [
    "company_id",
    "company_name",
    "industry_l3_id",
    "industry_l3_name",
    "model_type",
    "report_period",
    "announcement_date",
    "market_cap",
    "revenue",
    "net_profit",
    "revenue_yoy",
    "profit_yoy",
    "gross_margin",
]

FINANCIAL_COLUMNS = [
    "roe",
    "net_interest_margin",
    "npl_ratio",
    "provision_coverage",
    "cet1_ratio",
    "cost_income_ratio",
    "pb",
    "net_capital_ratio",
    "risk_coverage_ratio",
    "operating_margin",
    "premium_growth",
    "solvency_ratio",
    "combined_ratio",
    "investment_yield",
]


def _read_csv(source: str | Path | BinaryIO) -> pd.DataFrame:
    try:
        return pd.read_csv(source, encoding="utf-8-sig")
    except UnicodeDecodeError:
        if hasattr(source, "seek"):
            source.seek(0)
        return pd.read_csv(source, encoding="gb18030")


def load_path(path: str | Path) -> pd.DataFrame:
    source = Path(path)
    if source.suffix.lower() in {".xlsx", ".xlsm"}:
        return pd.read_excel(source)
    if source.suffix.lower() == ".csv":
        return _read_csv(source)
    raise ValueError("仅支持 .csv、.xlsx 或 .xlsm 文件")


def read_uploaded_file(uploaded_file: BinaryIO, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return pd.read_excel(uploaded_file)
    if suffix == ".csv":
        return _read_csv(uploaded_file)
    raise ValueError("仅支持 .csv、.xlsx 或 .xlsm 文件")


def normalize_input(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.copy()
    normalized_names = {str(column).strip().lower(): column for column in frame.columns}
    rename_map: dict[object, str] = {}
    for alias, canonical in COLUMN_ALIASES.items():
        source = normalized_names.get(alias.lower())
        if source is not None and canonical not in frame.columns:
            rename_map[source] = canonical
    frame = frame.rename(columns=rename_map)

    required = {"company_id", "report_period", "announcement_date"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"缺少必需字段：{', '.join(sorted(missing))}")
    if "industry_l3_id" not in frame and "industry_l3_name" not in frame:
        raise ValueError("缺少三级行业字段：请提供 industry_l3_id 或 industry_l3_name")

    frame["company_id"] = frame["company_id"].astype(str).str.strip()
    if "industry_l3_id" in frame:
        frame["industry_l3_id"] = frame["industry_l3_id"].astype(str).str.strip()
    from industry_catalog import attach_catalog, suggest_model_type

    frame = attach_catalog(frame)
    if "company_name" not in frame:
        frame["company_name"] = frame["company_id"]
    if "industry_l3_name" not in frame:
        frame["industry_l3_name"] = frame["industry_l3_id"]
    else:
        frame["industry_l3_name"] = frame["industry_l3_name"].fillna(frame["industry_l3_id"])
    inferred_models = frame["industry_l3_name"].map(suggest_model_type)
    if "model_type" not in frame:
        frame["model_type"] = inferred_models
    else:
        frame["model_type"] = frame["model_type"].replace("", pd.NA).fillna(inferred_models)

    frame["report_period"] = pd.to_datetime(frame["report_period"], errors="coerce")
    frame["announcement_date"] = pd.to_datetime(frame["announcement_date"], errors="coerce")
    for column in NUMERIC_COLUMNS.intersection(frame.columns):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def template_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=BASE_COLUMNS + FINANCIAL_COLUMNS)


def dataframe_to_excel_bytes(
    ranking: pd.DataFrame,
    watchlist: pd.DataFrame,
    weights: pd.DataFrame,
) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        watchlist.to_excel(writer, sheet_name="观察库", index=False)
        ranking.to_excel(writer, sheet_name="完整排名", index=False)
        weights.to_excel(writer, sheet_name="模型权重", index=False)
    output.seek(0)
    return output.getvalue()


def template_to_excel_bytes() -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        template_frame().to_excel(writer, sheet_name="季度数据", index=False)
        pd.DataFrame(
            [
                {"model_type": "general", "说明": "一般行业默认模型"},
                {"model_type": "growth", "说明": "成长型行业"},
                {"model_type": "quality", "说明": "质量型行业"},
                {"model_type": "cyclical", "说明": "周期型行业"},
                {"model_type": "scale", "说明": "规模型行业"},
                {"model_type": "bank", "说明": "银行"},
                {"model_type": "securities", "说明": "证券公司"},
                {"model_type": "insurance", "说明": "保险公司"},
                {"model_type": "financial_report", "说明": "金融专业指标不足时自动使用的公告基础模型"},
            ]
        ).to_excel(writer, sheet_name="模型类型", index=False)
    output.seek(0)
    return output.getvalue()
