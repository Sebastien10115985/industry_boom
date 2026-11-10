"""融资分红（东方财富 F10 + 本地缓存）。

巨潮的定期报告公告不含公司历史融资与分红汇总，因此取自东方财富：
- F10「融资分红」页（BonusFinancing/PageAjax）：历年分红合计、增发/配股明细；
- 数据中心「IPO 申购」表（RPTA_APP_IPOAPPLY）：首发募集资金总额。

三项指标：
- 历史融资总额 = IPO 募资 + 增发净募资 + 配股募资（元）；
- 历史分红总额 = 历年实施分红合计（元）；
- 分红融资比 = 分红总额 / 融资总额（越小说明从市场拿得多、回报得少，<1 表示拿的比还的多）；
- 上市日期：用于「上市满 10 年且分红融资比 < 1」的灰化提示。

缓存文件：`data/financing_dividend.csv`。此类数据变化极低频（分红按年实施），
缓存默认 90 天有效；页面展示只读缓存，联网拉取走命令行或财报同步流程。
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

try:
    import requests
except ImportError:  # pragma: no cover - 依赖缺失时由调用方提示
    requests = None  # type: ignore[assignment]

from market_cap_provider import _split_code

F10_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/BonusFinancing/PageAjax"
IPO_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE = "东方财富 F10 融资分红"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / "data" / "financing_dividend.csv"
DEFAULT_MAX_AGE_DAYS = 90
REQUEST_GAP_SECONDS = 0.4
COLUMNS = [
    "company_id",
    "company_name",
    "total_funding",
    "total_dividend",
    "dividend_funding_ratio",
    "listing_date",
    "update_date",
    "source",
]


class FinancingDividendError(RuntimeError):
    pass


def _f10_code(company_id: object) -> str:
    """603986.SH -> SH600519 风格的 F10 代码。"""
    code, market = _split_code(company_id)
    return f"{market}{code}"


def _f10_payload(code: str) -> dict:
    if requests is None:
        raise FinancingDividendError("缺少 requests，请先安装 requirements.txt")
    response = requests.get(
        F10_URL,
        params={"code": code},
        headers={"User-Agent": "Mozilla/5.0 (compatible; IndustryResearchTool/1.0)"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json() or {}


def fetch_financing_dividend(company_id: object, company_name: str = "") -> dict:
    """取单家公司的融资分红三项指标（单位：元）。"""
    code, _ = _split_code(company_id)
    payload = _f10_payload(_f10_code(company_id))

    yearly = payload.get("lnfhrz") or []
    total_dividend = 0.0
    for row in yearly:
        total_dividend += float(row.get("TOTAL_DIVIDEND") or 0)

    total_funding = 0.0
    for row in payload.get("zfmx") or []:  # 增发：净募资额（元）
        total_funding += float(row.get("NET_RAISE_FUNDS") or 0)
    for row in payload.get("pgmx") or []:  # 配股：募资总额（元）
        total_funding += float(row.get("TOTAL_RAISE_FUNDS") or 0)

    # 首发：IPO 募资总额（亿元），查不到时按 0 处理（部分老股/转板股无记录）
    listing_date = ""
    if requests is not None:
        try:
            response = requests.get(
                IPO_URL,
                params={
                    "reportName": "RPTA_APP_IPOAPPLY",
                    "columns": "ALL",
                    "filter": '(SECURITY_CODE="' + code + '")',
                    "pageNumber": "1",
                    "pageSize": "1",
                },
                headers={"User-Agent": "Mozilla/5.0 (compatible; IndustryResearchTool/1.0)"},
                timeout=20,
            )
            rows = (response.json().get("result") or {}).get("data") or []
            if rows:
                total_funding += float(rows[0].get("TOTAL_RAISE_FUNDS") or 0) * 1e8
                listing_date = str(rows[0].get("LISTING_DATE") or "")[:10]
        except Exception:  # noqa: BLE001 - IPO 记录缺失不影响另两项
            pass

    ratio = None
    if total_funding > 0:
        ratio = round(total_dividend / total_funding, 4)
    return {
        "company_id": code,
        "company_name": company_name,
        "total_funding": total_funding,
        "total_dividend": total_dividend,
        "dividend_funding_ratio": ratio,
        "listing_date": listing_date,
        "update_date": date.today().isoformat(),
        "source": SOURCE,
    }


def load_financing_dividend(path: Path | str | None = None) -> pd.DataFrame:
    frame = pd.read_csv(path or DEFAULT_CACHE_PATH, dtype={"company_id": str})
    # 旧版缓存只有融资分红比（融资/分红），换算成分红/融资并补空上市日期
    if "dividend_funding_ratio" not in frame.columns:
        if "funding_dividend_ratio" in frame.columns:
            old = pd.to_numeric(frame["funding_dividend_ratio"], errors="coerce")
            frame["dividend_funding_ratio"] = old.map(
                lambda v: round(1 / v, 4) if pd.notna(v) and v > 0 else None
            )
        else:
            frame["dividend_funding_ratio"] = None
    if "listing_date" not in frame.columns:
        frame["listing_date"] = ""
    return frame


def sync_financing_dividend(
    company_ids,
    refresh: bool = False,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    path: Path | str | None = None,
    progress=None,
    names: dict[str, str] | None = None,
) -> pd.DataFrame:
    """补齐缓存里缺失（或过期）公司的融资分红记录。"""
    path = Path(path or DEFAULT_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = (
        load_financing_dividend(path)
        if path.exists()
        else pd.DataFrame(columns=COLUMNS)
    )

    stale_cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    known: dict[str, object] = {}
    if not cache.empty:
        for row in cache.itertuples(index=False):
            known[str(row.company_id).split(".")[0]] = row

    todo: list[str] = []
    for item in company_ids:
        key = str(item).split(".")[0]
        row = known.get(key)
        if refresh or row is None or str(row.update_date) < stale_cutoff:
            todo.append(key)
        elif not str(getattr(row, "listing_date", "") or ""):
            # 旧版缓存缺上市日期，重拉一次补齐
            todo.append(key)
    todo = list(dict.fromkeys(todo))

    if todo:
        new_rows: list[dict] = []
        for position, code in enumerate(todo, start=1):
            if progress:
                label = names.get(code, "") if names else ""
                progress(position, len(todo), code + " " + label)
            try:
                new_rows.append(
                    fetch_financing_dividend(code, names.get(code, "") if names else "")
                )
            except Exception:  # noqa: BLE001 - 单家失败不阻断整批
                pass
            time.sleep(REQUEST_GAP_SECONDS)
        if new_rows:
            cache = pd.concat([cache, pd.DataFrame(new_rows)], ignore_index=True)
            # 同一公司只保留最新一条
            cache = cache.sort_values("update_date").drop_duplicates(
                subset=["company_id"], keep="last"
            )
            cache[COLUMNS].to_csv(path, index=False)

    return load_financing_dividend(path) if path.exists() else cache


def attach_financing_dividend(
    frame: pd.DataFrame, path: Path | str | None = None
) -> pd.DataFrame:
    """把融资分红三列并到公司级 DataFrame（只读缓存，不联网）。"""
    path = Path(path or DEFAULT_CACHE_PATH)
    if not path.exists() or frame.empty:
        return frame
    cache = load_financing_dividend(path)
    if cache.empty:
        return frame
    cache = cache.drop_duplicates(subset=["company_id"], keep="last")
    cache = cache.rename(
        columns={
            "total_funding": "financing_total",
            "total_dividend": "dividend_total",
            "funding_dividend_ratio": "funding_dividend_ratio",
        }
    )
    keep = ["company_id", "financing_total", "dividend_total", "dividend_funding_ratio", "listing_date"]
    cache = cache[keep].copy()
    # 两边代码口径统一成 6 位数字：缓存存 600519，排名表可能是 600519.SH
    cache["company_id"] = (
        cache["company_id"].astype(str).str.split(".").str[0].str.zfill(6)
    )
    merged = frame.copy()
    merged["_join_code"] = (
        merged["company_id"].astype(str).str.split(".").str[0].str.zfill(6)
    )
    cache_join = cache.rename(columns={"company_id": "_cache_code"})
    merged = merged.merge(
        cache_join,
        left_on="_join_code",
        right_on="_cache_code",
        how="left",
        validate="many_to_one",
    )
    return merged.drop(columns=["_join_code", "_cache_code"])
