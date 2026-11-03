"""总市值（公开行情接口 + 本地缓存）。

巨潮资讯提供的是上市公司直接上报的公告原文，公告接口本身不提供市值字段，
因此总市值取自公开行情接口（东方财富 push2），并缓存到本地，供排名按截止日使用。

缓存文件：`data/market_cap.csv`（列名沿用项目规范：company_id / market_cap）。
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pandas as pd

try:
    import requests
except ImportError:  # pragma: no cover - 依赖缺失时由调用方提示
    requests = None  # type: ignore[assignment]


QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
QUOTE_SOURCE = "公开行情接口（东方财富 push2）"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / "data" / "market_cap.csv"
DEFAULT_BATCH_SIZE = 40
DEFAULT_MAX_AGE_DAYS = 1
COLUMNS = ["company_id", "company_name", "market_cap", "price", "quote_date", "source"]


class MarketCapError(RuntimeError):
    pass


def _split_code(company_id: object) -> tuple[str, str]:
    """把 603986.SH / 603986 拆成 (6 位代码, 交易所后缀)。"""
    text = str(company_id).strip().upper()
    code, _, market = text.partition(".")
    code = code.strip().zfill(6)
    if not market and code:
        market = _guess_market(code)
    return code, market


def _guess_market(code: str) -> str:
    if code.startswith(("6", "9")):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SH"


def _secid(company_id: object) -> str:
    code, market = _split_code(company_id)
    prefix = "1" if market == "SH" else "0"
    return f"{prefix}.{code}"


def _request_quote(secids: list[str], timeout: float = 20.0) -> list[dict]:
    if requests is None:
        raise MarketCapError("缺少 requests，请先安装 requirements.txt")
    response = requests.get(
        QUOTE_URL,
        params={
            "fltt": "2",
            "secids": ",".join(secids),
            "fields": "f12,f13,f14,f20,f2",
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        },
        headers={"User-Agent": "Mozilla/5.0 (compatible; IndustryResearchTool/1.0)"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json() or {}
    data = payload.get("data") or {}
    rows = data.get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    return [row for row in rows if isinstance(row, dict)]


def _to_number(value: object) -> float:
    number = pd.to_numeric(value, errors="coerce")
    return float(number) if pd.notna(number) else float("nan")


def fetch_market_caps(
    company_ids: list[str],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> pd.DataFrame:
    """批量取总市值；f20 为总市值（元），f2 为最新价。"""
    wanted = [str(value).strip().upper() for value in company_ids if str(value).strip()]
    if not wanted:
        return pd.DataFrame(columns=COLUMNS)
    records: list[dict[str, object]] = []
    for start in range(0, len(wanted), max(1, batch_size)):
        batch = wanted[start : start + max(1, batch_size)]
        rows: list[dict] = []
        last_error: Exception | None = None
        # 行情接口偶发直接断开连接，重试几次再放弃，避免整个行业市值全部为空
        for attempt in range(3):
            try:
                rows = _request_quote([_secid(item) for item in batch])
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(3 + attempt * 7)
        if last_error is not None:
            raise MarketCapError(f"行情接口请求失败：{last_error}") from last_error
        for row in rows:
            code = str(row.get("f12") or "").strip()
            market = "SH" if str(row.get("f13")) == "1" else "SZ"
            market_cap = _to_number(row.get("f20"))
            if pd.isna(market_cap) or market_cap <= 0:
                continue
            records.append(
                {
                    "company_id": f"{code}.{market}",
                    "company_name": str(row.get("f14") or "").strip(),
                    "market_cap": market_cap,
                    "price": _to_number(row.get("f2")),
                    "quote_date": date.today().isoformat(),
                    "source": QUOTE_SOURCE,
                }
            )
    return pd.DataFrame(records, columns=COLUMNS)


def load_market_caps(path: str | Path = DEFAULT_CACHE_PATH) -> pd.DataFrame:
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.read_csv(source, dtype={"company_id": str}, encoding="utf-8-sig")
    for column in COLUMNS:
        if column not in frame:
            frame[column] = pd.NA
    frame["market_cap"] = pd.to_numeric(frame["market_cap"], errors="coerce")
    return frame[COLUMNS]


def save_market_caps(data: pd.DataFrame, path: str | Path = DEFAULT_CACHE_PATH) -> pd.DataFrame:
    """写入市值缓存。

    写入前先合并磁盘上的现有内容，避免多个行业并行同步时后写的进程覆盖掉前一个进程的结果。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = data.copy()
    if target.exists() and target.stat().st_size > 0:
        try:
            existing = pd.read_csv(target, dtype={"company_id": str}, encoding="utf-8-sig")
        except Exception:
            existing = pd.DataFrame(columns=COLUMNS)
        if not existing.empty:
            frame = pd.concat([existing, frame], ignore_index=True, sort=False)
    ordered = (
        frame.drop_duplicates("company_id", keep="last")
        .sort_values("company_id")
        .reset_index(drop=True)
    )
    ordered.to_csv(target, index=False, encoding="utf-8-sig")
    return ordered


def sync_market_caps(
    company_ids: list[str],
    path: str | Path = DEFAULT_CACHE_PATH,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    refresh: bool = False,
) -> pd.DataFrame:
    """取总市值；已有缓存且在有效期内则直接复用，避免重复请求。"""
    cached = load_market_caps(path)
    cached = cached[cached["market_cap"].notna()]
    wanted = sorted({str(value).strip().upper() for value in company_ids if str(value).strip()})
    if not wanted:
        return cached.reset_index(drop=True)
    if not refresh and max_age_days > 0 and not cached.empty:
        today = pd.Timestamp(date.today())
        ages = pd.to_datetime(cached["quote_date"], errors="coerce")
        fresh = ages.notna() & (today - ages).dt.days.le(max_age_days)
        fresh_ids = set(cached.loc[fresh, "company_id"].astype(str).str.upper())
        wanted = [item for item in wanted if item.upper() not in fresh_ids]
    if not wanted:
        return cached.reset_index(drop=True)
    try:
        fetched = fetch_market_caps(wanted)
    except MarketCapError:
        if cached.empty:
            raise
        return cached.reset_index(drop=True)
    if fetched.empty:
        return cached.reset_index(drop=True)
    combined = fetched if cached.empty else pd.concat([cached, fetched], ignore_index=True, sort=False)
    return save_market_caps(combined, path)


def attach_market_cap(
    frame: pd.DataFrame,
    path: str | Path = DEFAULT_CACHE_PATH,
) -> pd.DataFrame:
    """把缓存中的总市值补进财报记录，已有值优先保留。"""
    cached = load_market_caps(path)
    result = frame.copy()
    if cached.empty:
        return result
    if "market_cap" not in result:
        result["market_cap"] = pd.NA
    result["market_cap"] = pd.to_numeric(result["market_cap"], errors="coerce")
    lookup = cached.set_index("company_id")["market_cap"]
    ids = result["company_id"].astype(str).str.upper()
    filled = ids.map(lookup)
    result["market_cap"] = result["market_cap"].fillna(filled)
    return result
