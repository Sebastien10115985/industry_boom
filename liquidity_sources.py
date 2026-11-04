"""流动性指标数据抓取与本地缓存。

每个指标一个 CSV（date,value,source_url,retrieved_at），落在 data/liquidity/ 下；
数据源登记在 SOURCE_REGISTRY，页面据此展示来源、链接与最近更新时间。

自动抓取源：
- DR007：中国货币网（全国银行间同业拆借中心）回购定盘利率 FDR007，即 DR007 的定盘价
- 十年期国债收益率：中债国债收益率曲线（中央结算公司），每日估值
- 两融余额：沪深两融余额（东方财富数据中心汇总）
- M1/M2：货币供应量同比（东方财富数据中心，口径同央行月度统计）

手动导入源：
- 社融存量同比：央行每月《社会融资规模存量统计数据报告》无公开接口，
  按手动导入格式更新 data/liquidity/social_financing_yoy.csv 即可。
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

PROJECT_DIR = Path(__file__).resolve().parent
LIQUIDITY_DATA_DIR = PROJECT_DIR / "data" / "liquidity"
DEFAULT_HISTORY_YEARS = 3
REQUEST_TIMEOUT = 30
REQUEST_RETRIES = 3
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
EASTMONEY_UT = "b2884a393a59ad64002292a3e90d46a5"

CHINAMONEY_FRR_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/FrrHis"
CHINAMONEY_PAGE_URL = "https://www.chinamoney.com.cn/chinese/bkfrr/"
CHINABOND_YIELD_URL = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
CHINABOND_PAGE_URL = "https://yield.chinabond.com.cn/cbweb-mn/yc/searchYc?locale=zh_CN"
EASTMONEY_API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PBC_SOCIAL_FINANCE_PAGE_URL = "http://www.pbc.gov.cn/diaochatongjisi/135146/index.html"

# 数据源登记：页面上展示，也是数据留存清单
SOURCE_REGISTRY: dict[str, dict[str, str]] = {
    "dr007": {
        "label": "DR007（存款类机构7天回购定盘利率）",
        "frequency": "周频（日度取每周最后交易日）",
        "provider": "中国货币网·全国银行间同业拆借中心",
        "url": CHINAMONEY_PAGE_URL,
        "mode": "自动",
    },
    "bond_10y": {
        "label": "十年期国债收益率",
        "frequency": "周频（日度取每周最后交易日）",
        "provider": "中债国债收益率曲线（中央结算公司）",
        "url": CHINABOND_PAGE_URL,
        "mode": "自动",
    },
    "margin_balance_yoy": {
        "label": "两融余额同比",
        "frequency": "周频（日度余额取同比）",
        "provider": "沪深交易所融资融券余额（东方财富数据中心汇总）",
        "url": "https://data.eastmoney.com/rgrz/",
        "mode": "自动",
    },
    "m1_m2_gap": {
        "label": "M1-M2 剪刀差",
        "frequency": "月频",
        "provider": "央行货币供应量统计（东方财富数据中心转载）",
        "url": PBC_SOCIAL_FINANCE_PAGE_URL,
        "mode": "自动",
    },
    "m2_yoy": {
        "label": "M2 同比增速",
        "frequency": "月频",
        "provider": "央行货币供应量统计（东方财富数据中心转载）",
        "url": PBC_SOCIAL_FINANCE_PAGE_URL,
        "mode": "自动",
    },
    "social_financing_yoy": {
        "label": "社融存量同比",
        "frequency": "月频",
        "provider": "央行《社会融资规模存量统计数据报告》（手动导入）",
        "url": PBC_SOCIAL_FINANCE_PAGE_URL,
        "mode": "手动",
    },
}

METRIC_LABELS = {
    "dr007": "DR007",
    "bond_10y": "十年期国债收益率",
    "margin_balance_yoy": "两融余额同比",
    "m1_m2_gap": "M1-M2剪刀差",
    "m2_yoy": "M2同比增速",
    "social_financing_yoy": "社融存量同比",
}

MANUAL_IMPORT_NOTE = (
    "社融存量同比按手动导入更新：每月央行发布《社会融资规模存量统计数据报告》后，"
    f"从 {PBC_SOCIAL_FINANCE_PAGE_URL} 取「同比增长」数值，"
    "写入 data/liquidity/social_financing_yoy.csv（列：date,value,source_url,retrieved_at），"
    "date 填报告期末月月末（如 2026-09-30）。"
)


def _cached_path(metric_id: str) -> Path:
    return Path(LIQUIDITY_DATA_DIR) / f"{metric_id}.csv"


def load_cached_metric(metric_id: str) -> pd.DataFrame:
    """读单个指标缓存；列固定为 date,value,source_url,retrieved_at。"""
    source = _cached_path(metric_id)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame(columns=["date", "value", "source_url", "retrieved_at"])
    frame = pd.read_csv(source, encoding="utf-8-sig")
    for column in ("date", "value", "source_url", "retrieved_at"):
        if column not in frame.columns:
            frame[column] = pd.NA
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    return frame.dropna(subset=["date"]).reset_index(drop=True)


def save_metric(metric_id: str, frame: pd.DataFrame, *, retrieved_at: str | None = None) -> int:
    """合并写入缓存（按日期去重，新抓取覆盖旧行）；返回缓存总行数。"""
    if frame.empty:
        return len(load_cached_metric(metric_id))
    target = _cached_path(metric_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = pd.concat([load_cached_metric(metric_id), frame], ignore_index=True)
    stamp = retrieved_at or pd.Timestamp.now().strftime("%Y-%m-%d")
    merged["retrieved_at"] = merged["retrieved_at"].replace("", pd.NA).fillna(stamp)
    merged = merged.sort_values(["date", "retrieved_at"]).drop_duplicates("date", keep="last")
    merged = merged.sort_values("date").reset_index(drop=True)
    merged["date"] = pd.to_datetime(merged["date"]).dt.strftime("%Y-%m-%d")
    merged.to_csv(target, index=False, encoding="utf-8-sig")
    return len(merged)


def _request_json(url: str, *, params: dict | None = None, method: str = "GET") -> dict:
    last_error: Exception | None = None
    for attempt in range(REQUEST_RETRIES):
        try:
            response = requests.request(
                method,
                url,
                params=params,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
            last_error = exc
            time.sleep(2 + attempt * 3)
    raise ConnectionError(f"请求失败 {url}：{last_error}")


def _request_text(url: str, *, params: dict | None = None) -> str:
    last_error: Exception | None = None
    for attempt in range(REQUEST_RETRIES):
        try:
            response = requests.get(
                url,
                params=params,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(2 + attempt * 3)
    raise ConnectionError(f"请求失败 {url}：{last_error}")


def _month_starts(start: date, end: date) -> list[tuple[date, date]]:
    """按自然月切分 [start, end]，返回 [(月首, 月末)]。"""
    months: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            nxt = date(cursor.year + 1, 1, 1)
        else:
            nxt = date(cursor.year, cursor.month + 1, 1)
        months.append((cursor, min(nxt - timedelta(days=1), end)))
        cursor = nxt
    return months


def _period_chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """把 [start, end] 切成不超过 days 天的区间（中债接口对区间长度有限制）。"""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def fetch_dr007(years: int = DEFAULT_HISTORY_YEARS) -> pd.DataFrame:
    """中国货币网回购定盘利率：FDR007（DR007 的定盘价），单位 %。"""
    end = date.today()
    start = end - timedelta(days=int(years * 365.25) + 10)
    rows: list[dict] = []
    for month_start, month_end in _month_starts(start, end):
        if month_start > end:
            break
        data = _request_json(
            CHINAMONEY_FRR_URL,
            params={
                "lang": "CN",
                "startDate": month_start.isoformat(),
                "endDate": month_end.isoformat(),
            },
            method="POST",
        )
        for record in data.get("records") or []:
            value_map = record.get("frValueMap") or {}
            raw = value_map.get("FDR007")
            if raw in (None, ""):
                continue
            rows.append(
                {
                    "date": pd.to_datetime(value_map.get("date"), errors="coerce"),
                    "value": pd.to_numeric(raw, errors="coerce"),
                }
            )
        time.sleep(0.3)
    frame = pd.DataFrame(rows).dropna()
    frame["source_url"] = CHINAMONEY_PAGE_URL
    return frame[["date", "value", "source_url"]]


def _parse_chinabond_table(html: str) -> pd.DataFrame:
    """解析中债 historyQuery 返回的 HTML 表格，取「中债国债收益率曲线」的 10 年值。"""
    rows: list[list[str]] = []
    for row_text in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        cells = [
            re.sub(r"<[^>]+>", "", cell).strip()
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_text, flags=re.S | re.I)
        ]
        if cells:
            rows.append(cells)
    header: list[str] = []
    data_rows: list[list[str]] = []
    for cells in rows:
        if any("曲线名称" in cell for cell in cells):
            header = cells
            continue
        if len(cells) >= 9 and re.match(r"^\d{4}-\d{2}-\d{2}$", cells[1] or ""):
            data_rows.append(cells)
    if not header or not data_rows:
        return pd.DataFrame(columns=["date", "value", "source_url"])
    date_index = next(i for i, cell in enumerate(header) if "日期" in cell)
    ten_year_index = next(i for i, cell in enumerate(header) if cell.strip().startswith("10年"))
    records: list[dict] = []
    for cells in data_rows:
        raw = cells[ten_year_index].strip() if ten_year_index < len(cells) else ""
        value = pd.to_numeric(raw, errors="coerce")
        day = pd.to_datetime(cells[date_index], errors="coerce")
        if pd.isna(value) or pd.isna(day):
            continue
        records.append({"date": day, "value": value})
    frame = pd.DataFrame(records).dropna()
    frame["source_url"] = CHINABOND_PAGE_URL
    return frame[["date", "value", "source_url"]]


def fetch_bond_10y(years: int = DEFAULT_HISTORY_YEARS) -> pd.DataFrame:
    """中债国债收益率曲线 10 年期到期收益率，单位 %。接口对区间长度有限制，分段拉取。"""
    end = date.today()
    start = end - timedelta(days=int(years * 365.25) + 10)
    frames: list[pd.DataFrame] = []
    for chunk_start, chunk_end in _period_chunks(start, end, days=300):
        html = _request_text(
            CHINABOND_YIELD_URL,
            params={
                "startDate": chunk_start.isoformat(),
                "endDate": chunk_end.isoformat(),
                "gjqx": "0",
                "qxId": "ycqx",
                "locale": "cn_ZH",
            },
        )
        frames.append(_parse_chinabond_table(html.replace("&nbsp", "")))
        time.sleep(0.4)
    if not frames:
        return pd.DataFrame(columns=["date", "value", "source_url"])
    frame = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    return frame.sort_values("date").reset_index(drop=True)


def fetch_margin_balance(years: int = DEFAULT_HISTORY_YEARS) -> pd.DataFrame:
    """沪深两融余额（融资融券余额合计），单位亿元，日度。"""
    end = date.today()
    start = end - timedelta(days=int(years * 365.25) + 10)
    rows: list[dict] = []
    page = 1
    while True:
        data = _request_json(
            EASTMONEY_API_URL,
            params={
                "reportName": "RPTA_WEB_MARGIN_DAILYTRADE",
                "columns": "ALL",
                "pageSize": 500,
                "pageNumber": page,
                "sortColumns": "STATISTICS_DATE",
                "sortTypes": "-1",
                "filter": f"(STATISTICS_DATE>='{start.isoformat()}')",
                "ut": EASTMONEY_UT,
                "source": "WEB",
                "client": "WEB",
            },
        )
        result = data.get("result") or {}
        batch = result.get("data") or []
        if not batch:
            break
        for row in batch:
            raw = row.get("MARGIN_BALANCE")
            if raw in (None, ""):
                continue
            rows.append(
                {
                    "date": pd.to_datetime(row.get("STATISTICS_DATE"), errors="coerce"),
                    "value": pd.to_numeric(raw, errors="coerce"),
                }
            )
        total_pages = int(result.get("pages") or 1)
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.3)
    frame = pd.DataFrame(rows).dropna().drop_duplicates("date")
    frame["source_url"] = SOURCE_REGISTRY["margin_balance_yoy"]["url"]
    return frame.sort_values("date").reset_index(drop=True)


def fetch_money_supply() -> pd.DataFrame:
    """央行口径货币供应量同比：M2、M1，单位 %。"""
    data = _request_json(
        EASTMONEY_API_URL,
        params={
            "reportName": "RPT_ECONOMY_CURRENCY_SUPPLY",
            "columns": "REPORT_DATE,BASIC_CURRENCY_SAME,CURRENCY_SAME",
            "pageSize": 200,
            "pageNumber": 1,
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "ut": EASTMONEY_UT,
            "source": "WEB",
            "client": "WEB",
        },
    )
    rows: list[dict] = []
    for row in (data.get("result") or {}).get("data") or []:
        m2_yoy = pd.to_numeric(row.get("BASIC_CURRENCY_SAME"), errors="coerce")
        m1_yoy = pd.to_numeric(row.get("CURRENCY_SAME"), errors="coerce")
        day = pd.to_datetime(row.get("REPORT_DATE"), errors="coerce")
        if pd.isna(day) or pd.isna(m2_yoy):
            continue
        month_end = day + pd.offsets.MonthEnd(0)
        rows.append({"date": month_end, "m2_yoy": m2_yoy, "m1_yoy": m1_yoy})
    frame = pd.DataFrame(rows).dropna(subset=["m2_yoy"]).drop_duplicates("date")
    return frame.sort_values("date").reset_index(drop=True)


def update_liquidity_cache(progress=None) -> dict[str, int]:
    """抓取全部自动源并合并入本地缓存；返回各指标缓存后的总行数。"""

    def report(step: int, total: int, message: str) -> None:
        if progress is not None:
            progress(step, total, message)

    jobs = [
        ("dr007", lambda: fetch_dr007()),
        ("bond_10y", lambda: fetch_bond_10y()),
        ("margin_balance_yoy", lambda: fetch_margin_balance()),
    ]
    total = len(jobs) + 1
    counts: dict[str, int] = {}
    for index, (metric_id, fetcher) in enumerate(jobs, start=1):
        report(index - 1, total, f"正在更新 {METRIC_LABELS[metric_id]}")
        try:
            save_metric(metric_id, fetcher())
        except Exception as exc:  # noqa: BLE001 - 单项失败不影响其它指标
            report(index, total, f"{METRIC_LABELS[metric_id]} 更新失败：{exc}")
        counts[metric_id] = len(load_cached_metric(metric_id))

    report(len(jobs), total, "正在更新 M1/M2 与剪刀差")
    try:
        supply = fetch_money_supply()
        save_metric(
            "m2_yoy",
            supply.rename(columns={"m2_yoy": "value"})[["date", "value"]]
            .assign(source_url=PBC_SOCIAL_FINANCE_PAGE_URL),
        )
        gap = supply.assign(value=supply["m1_yoy"] - supply["m2_yoy"])
        gap = gap.dropna(subset=["value"])[["date", "value"]]
        save_metric("m1_m2_gap", gap.assign(source_url=PBC_SOCIAL_FINANCE_PAGE_URL))
    except Exception as exc:  # noqa: BLE001
        report(total, total, f"M1/M2 更新失败：{exc}")
    counts["m2_yoy"] = len(load_cached_metric("m2_yoy"))
    counts["m1_m2_gap"] = len(load_cached_metric("m1_m2_gap"))
    counts["social_financing_yoy"] = len(load_cached_metric("social_financing_yoy"))

    report(total, total, "流动性数据更新完成")
    return counts


def load_all_metrics() -> dict[str, pd.Series]:
    """读全部指标缓存，返回 {metric_id: 以日期为索引的 Series}。"""
    result: dict[str, pd.Series] = {}
    for metric_id in SOURCE_REGISTRY:
        frame = load_cached_metric(metric_id)
        if frame.empty:
            continue
        series = frame.set_index("date")["value"].sort_index()
        series = series[~series.index.duplicated(keep="last")]
        result[metric_id] = series
    return result
