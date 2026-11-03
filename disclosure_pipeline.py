from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd

from cninfo_provider import CninfoClient
from data_io import normalize_input
from financial_extract import extract_financial_metrics
from industry_catalog import load_constituents
from ranking import (
    MODEL_SPECS,
    normalize_model_type,
    prepare_growth_metrics,
    score_snapshot,
    select_snapshot,
)


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_PATH = PROJECT_DIR / "data" / "online_financials.csv"

ProgressCallback = Callable[[int, int, str], None]


def load_online_financials(path: str | Path = DEFAULT_DATA_PATH) -> pd.DataFrame:
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(source, encoding="utf-8-sig")


def _save_online_financials(data: pd.DataFrame, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = data.sort_values(["company_id", "report_period", "announcement_date"])
    ordered.to_csv(target, index=False, encoding="utf-8-sig")


def _select_companies(
    scope: str,
    company_ids: list[str] | None,
    max_companies: int | None,
) -> pd.DataFrame:
    constituents = load_constituents()
    if constituents.empty:
        raise ValueError("固定行业目录为空；请先用万得官方数据执行 catalog-freeze 或 catalog-wind")
    if company_ids:
        requested = {str(code).strip().upper() for code in company_ids}
        companies = constituents[constituents["company_id"].str.upper().isin(requested)].copy()
        missing = requested.difference(companies["company_id"].str.upper())
        if missing:
            raise ValueError(f"以下公司不在固定万得成分股目录：{', '.join(sorted(missing))}")
    elif scope == "seed":
        companies = constituents[constituents["is_seed"]].copy()
    elif scope == "all":
        companies = constituents.copy()
    else:
        raise ValueError("scope 只能是 seed 或 all")
    companies = companies.drop_duplicates("company_id").sort_values(["industry_l3_id", "company_id"])
    if max_companies is not None:
        companies = companies.head(max_companies)
    return companies.reset_index(drop=True)


def sync_official_reports(
    as_of_date: str | date,
    scope: str = "seed",
    company_ids: list[str] | None = None,
    data_path: str | Path = DEFAULT_DATA_PATH,
    lookback_days: int = 800,
    max_companies: int | None = None,
    progress: ProgressCallback | None = None,
    client: CninfoClient | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """从固定成分股目录同步巨潮正式定期报告，并保留提取失败清单。"""
    as_of = pd.Timestamp(as_of_date).normalize()
    start = as_of - pd.Timedelta(days=lookback_days)
    companies = _select_companies(scope, company_ids, max_companies)
    existing = load_online_financials(data_path)
    known_ids = set(existing.get("announcement_id", pd.Series(dtype=str)).dropna().astype(str))
    api = client or CninfoClient()
    new_rows: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []

    total = len(companies)
    for position, company in companies.iterrows():
        company_id = str(company["company_id"])
        if progress:
            progress(position + 1, total, f"检索 {company_id} {company['company_name']}")
        try:
            reports = api.search_periodic_reports(company_id, start, as_of)
        except Exception as exc:
            errors.append({"company_id": company_id, "stage": "search", "error": str(exc)})
            continue

        for report in reports:
            if report.announcement_id in known_ids:
                continue
            try:
                pdf_content = api.download_pdf(report.pdf_url)
                row = extract_financial_metrics(pdf_content, report)
                row.update(
                    {
                        "industry_l3_id": company["industry_l3_id"],
                        "industry_l3_name": company["industry_l3_name"],
                        "model_type": company["model_type"],
                        "market_cap": company["market_cap"],
                    }
                )
                new_rows.append(row)
                known_ids.add(report.announcement_id)
            except Exception as exc:
                errors.append(
                    {
                        "company_id": company_id,
                        "announcement_id": report.announcement_id,
                        "report_period": report.report_period,
                        "stage": "extract",
                        "source_url": report.pdf_url,
                        "error": str(exc),
                    }
                )

    additions = pd.DataFrame(new_rows)
    if existing.empty:
        combined = additions
    elif additions.empty:
        combined = existing
    else:
        combined = pd.concat([existing, additions], ignore_index=True, sort=False)
    if not combined.empty:
        combined = combined.drop_duplicates("announcement_id", keep="last")
        _save_online_financials(combined, data_path)
    return combined.reset_index(drop=True), pd.DataFrame(errors)


def choose_latest_report_period(data: pd.DataFrame, as_of_date: str | date) -> pd.Timestamp:
    announcements = pd.to_datetime(data["announcement_date"], errors="coerce")
    periods = pd.to_datetime(data["report_period"], errors="coerce")
    available = periods[announcements.le(pd.Timestamp(as_of_date).normalize())].dropna()
    if available.empty:
        raise ValueError("截止日之前没有可用定期报告")
    return available.max().normalize()


def _apply_financial_report_fallback(
    data: pd.DataFrame,
    minimum_coverage: float,
) -> pd.DataFrame:
    frame = data.copy()
    frame["model_fallback_reason"] = ""
    normalized_models = frame["model_type"].map(normalize_model_type)
    for industry_id, indices in frame.groupby("industry_l3_id").groups.items():
        group_models = normalized_models.loc[indices]
        if group_models.empty:
            continue
        model_name = str(group_models.mode().iloc[0])
        if model_name not in {"bank", "securities", "insurance"}:
            continue
        spec = MODEL_SPECS[model_name]
        coverage = pd.Series(0.0, index=indices)
        for metric, (weight, _) in spec.metrics.items():
            if metric in frame:
                coverage += weight * pd.to_numeric(frame.loc[indices, metric], errors="coerce").notna()
        if coverage.ge(minimum_coverage).mean() < 0.80:
            frame.loc[indices, "model_type"] = "financial_report"
            frame.loc[indices, "model_fallback_reason"] = (
                f"{model_name} 专业指标覆盖不足，统一使用正式报告核心财务模型"
            )
    return frame


def rank_online_financials(
    data: pd.DataFrame,
    as_of_date: str | date,
    report_period: str | date | None = None,
    top_n: int = 20,
    minimum_coverage: float = 0.60,
) -> tuple[pd.Timestamp, pd.DataFrame]:
    if data.empty:
        raise ValueError("在线财务数据为空，请先执行 update")
    normalized = normalize_input(data)
    period = (
        pd.Timestamp(report_period).normalize()
        if report_period is not None
        else choose_latest_report_period(normalized, as_of_date)
    )
    prepared = prepare_growth_metrics(normalized, as_of_date)
    prepared = _apply_financial_report_fallback(prepared, minimum_coverage)
    snapshot = select_snapshot(prepared, as_of_date, period)
    ranking = score_snapshot(snapshot, top_n=top_n, minimum_coverage=minimum_coverage)
    return period, ranking
