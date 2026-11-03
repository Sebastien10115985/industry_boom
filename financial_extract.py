from __future__ import annotations

import re
from io import BytesIO
from typing import Iterable

import numpy as np

from cninfo_provider import ReportRecord


NUMBER_PATTERN = re.compile(r"(?<![\d.])[-－—]?\s*\(?\d[\d,，]*(?:\.\d+)?\)?(?![\d.%％])")

# 合并利润表里科目常带附注列，例如「其中：营业成本 七、61 9,988,691,327.59」，
# 其中的「七、61」不是金额，必须先剥掉，否则会被当成数值。
NOTE_REFERENCE_PATTERN = re.compile(r"[\u4e00-\u9fa5]{1,4}、\d{1,4}(?![\d,.])")

MAX_GROSS_MARGIN = 0.995


def _parse_number(token: str) -> float | None:
    cleaned = token.replace(",", "").replace("，", "").replace(" ", "").replace("－", "-").replace("—", "-")
    negative_parentheses = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative_parentheses else value


def _numbers_after_label(text: str, labels: Iterable[str], width: int = 260) -> list[float]:
    normalized = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    for label in labels:
        position = normalized.find(label)
        if position < 0:
            continue
        segment = normalized[position + len(label) : position + len(label) + width]
        segment = NOTE_REFERENCE_PATTERN.sub(" ", segment)
        values = []
        for token in NUMBER_PATTERN.findall(segment):
            parsed = _parse_number(token)
            if parsed is not None:
                values.append(parsed)
        if values:
            return values
    return []


def _unit_multiplier(text: str) -> float:
    header = text[:1200].replace(" ", "")
    if re.search(r"单位[:：](?:人民币)?百万元", header):
        return 1_000_000.0
    if re.search(r"单位[:：](?:人民币)?万元", header):
        return 10_000.0
    return 1.0


def _statement_candidates(page_texts: list[str]) -> list[str]:
    candidates: list[str] = []
    for index, text in enumerate(page_texts):
        compact = text.replace(" ", "")
        if any(
            marker in compact
            for marker in ("合并利润表", "合并年初到报告期末利润表", "合并及公司利润表")
        ):
            joined = "\n".join(page_texts[index : index + 3])
            if NUMBER_PATTERN.search(joined):
                candidates.append(joined)
    return candidates


def _extract_pages(pdf_content: bytes, maximum_pages: int | None = None) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("缺少 pypdf，请先安装 requirements.txt") from exc
    reader = PdfReader(BytesIO(pdf_content))
    page_count = len(reader.pages) if maximum_pages is None else min(len(reader.pages), maximum_pages)
    texts: list[str] = []
    for page in reader.pages[:page_count]:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")
    return texts


def extract_financial_metrics(
    pdf_content: bytes,
    report: ReportRecord,
    maximum_pages: int | None = None,
) -> dict[str, object]:
    """从正式定期报告提取合并口径财务值；返回来源与置信度供人工复核。"""
    page_texts = _extract_pages(pdf_content, maximum_pages=maximum_pages)
    statements = _statement_candidates(page_texts)
    whole_text = "\n".join(page_texts)
    search_blocks = statements + [whole_text]

    revenue_values: list[float] = []
    profit_values: list[float] = []
    cost_values: list[float] = []
    multiplier = 1.0
    for block in search_blocks:
        revenue_values = _numbers_after_label(block, ("营业总收入", "营业收入"))
        profit_values = _numbers_after_label(
            block,
            (
                "归属于母公司所有者的净利润",
                "归属于母公司股东的净利润",
                "归属于上市公司股东的净利润",
            ),
        )
        cost_values = _numbers_after_label(block, ("其中：营业成本", "营业成本"))
        if revenue_values and profit_values:
            multiplier = _unit_multiplier(block)
            break

    revenue = revenue_values[0] * multiplier if revenue_values else np.nan
    prior_revenue = revenue_values[1] * multiplier if len(revenue_values) > 1 else np.nan
    net_profit = profit_values[0] * multiplier if profit_values else np.nan
    prior_profit = profit_values[1] * multiplier if len(profit_values) > 1 else np.nan
    operating_cost = cost_values[0] * multiplier if cost_values else np.nan

    gross_margin = np.nan
    if np.isfinite(revenue) and revenue != 0 and np.isfinite(operating_cost):
        gross_margin = (revenue - operating_cost) / revenue
        # 毛利率不可能接近 100%，出现这种值说明营业成本没抽对，宁可留空也不要给出错误数
        if not -2 <= gross_margin <= MAX_GROSS_MARGIN:
            gross_margin = np.nan

    revenue_yoy = np.nan
    if np.isfinite(revenue) and np.isfinite(prior_revenue) and prior_revenue != 0:
        revenue_yoy = (revenue - prior_revenue) / abs(prior_revenue)
    profit_yoy = np.nan
    if np.isfinite(net_profit) and np.isfinite(prior_profit):
        if prior_profit > 0:
            profit_yoy = (net_profit - prior_profit) / abs(prior_profit)
        elif abs(net_profit) + abs(prior_profit) > 0:
            profit_yoy = 2 * (net_profit - prior_profit) / (abs(net_profit) + abs(prior_profit))

    found = sum(np.isfinite(value) for value in (revenue, net_profit, gross_margin))
    confidence = found / 3
    return {
        "company_id": report.company_id,
        "company_name": report.company_name,
        "report_period": report.report_period,
        "announcement_date": report.announcement_date,
        "revenue": revenue,
        "net_profit": net_profit,
        "revenue_yoy": revenue_yoy,
        "profit_yoy": profit_yoy,
        "gross_margin": gross_margin,
        "source_url": report.pdf_url,
        "source_title": report.title,
        "announcement_id": report.announcement_id,
        "extraction_confidence": confidence,
        "needs_review": confidence < 2 / 3,
    }
