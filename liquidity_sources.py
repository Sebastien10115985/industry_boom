"""流动性指标数据抓取与本地缓存。

每个指标一个 CSV（date,value,source_url,retrieved_at），落在 data/liquidity/ 下；
数据源登记在 SOURCE_REGISTRY，页面据此展示来源、链接与最近更新时间。

自动抓取源：
- DR007：中国货币网（全国银行间同业拆借中心）回购定盘利率 FDR007，即 DR007 的定盘价
- 十年期国债收益率：中债国债收益率曲线（中央结算公司），每日估值
- 两融余额：沪深两融余额（东方财富数据中心汇总）
- M1/M2：货币供应量同比（东方财富数据中心，口径同央行月度统计）
- 社融存量与存量同比：央行「调查统计司-统计数据-社会融资规模」年度栏目，
  逐月解析《社会融资规模存量统计表》（htm 附件），与央行公众号月度报告同源。
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
    "margin_balance": {
        "label": "两融余额",
        "frequency": "周频（日度余额取每周最后交易日）",
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
    "social_financing_stock": {
        "label": "社会融资规模存量",
        "frequency": "月频",
        "provider": "央行《社会融资规模存量统计表》（调查统计司统计数据栏目）",
        "url": PBC_SOCIAL_FINANCE_PAGE_URL,
        "mode": "自动",
    },
    "social_financing_yoy": {
        "label": "社融存量同比",
        "frequency": "月频",
        "provider": "央行《社会融资规模存量统计表》（调查统计司统计数据栏目）",
        "url": PBC_SOCIAL_FINANCE_PAGE_URL,
        "mode": "自动",
    },
}

METRIC_LABELS = {
    "dr007": "DR007",
    "bond_10y": "十年期国债收益率",
    "margin_balance": "两融余额",
    "margin_balance_yoy": "两融余额同比",
    "m1_m2_gap": "M1-M2剪刀差",
    "m2_yoy": "M2同比增速",
    "social_financing_stock": "社会融资规模存量",
    "social_financing_yoy": "社融存量同比",
}

MANUAL_IMPORT_NOTE = (
    "社融存量与存量同比已改为自动抓取：从央行「调查统计司-统计数据-社会融资规模」"
    "年度栏目逐月解析《社会融资规模存量统计表》，无需手动导入。"
    "若央行页面改版导致抓取失败，可临时把数值写入 "
    "data/liquidity/social_financing_yoy.csv（列：date,value,source_url,retrieved_at）应急。"
)

PBC_BASE = "http://www.pbc.gov.cn"
PBC_STATS_INDEX_URL = f"{PBC_BASE}/diaochatongjisi/116219/116319/index.html"
DEFAULT_PBC_YEARS = 5  # 抓最近 5 年的社融存量表，覆盖 36 个月 Z 窗口


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
    existing = load_cached_metric(metric_id)
    merged = frame.copy() if existing.empty else pd.concat([existing, frame], ignore_index=True)
    if "retrieved_at" not in merged.columns:
        merged["retrieved_at"] = pd.NA
    stamp = retrieved_at or pd.Timestamp.now().strftime("%Y-%m-%d")
    merged["retrieved_at"] = merged["retrieved_at"].replace("", pd.NA).fillna(stamp)
    merged = merged.sort_values(["date", "retrieved_at"]).drop_duplicates("date", keep="last")
    merged = merged.sort_values("date").reset_index(drop=True)
    merged["date"] = pd.to_datetime(merged["date"]).dt.strftime("%Y-%m-%d")
    merged.to_csv(target, index=False, encoding="utf-8-sig")
    return len(merged)


_DECODE_MARKERS = ("中国", "人民", "银行", "统计", "数据", "融资", "存量", "货币")


def _decode_best(raw: bytes) -> str:
    """在 utf-8 与 gb18030 之间择优选解码结果。

    央行站点既有 UTF-8 页面（栏目页），也有 GB18030 页面（附件表），
    个别页面还夹带非法字节导致严格解码失败；用常见中文词命中数打分，
    比统计汉字个数更可靠（GB18030 错解会产出同样落在汉字区的生僻字）。
    """

    def score(text: str) -> int:
        return sum(text.count(marker) for marker in _DECODE_MARKERS)

    best_text, best_score = "", -1
    for encoding in ("utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            text = raw.decode(encoding, errors="ignore")
        current = score(text)
        if current > best_score:
            best_text, best_score = text, current
    return best_text


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
    """取网页文本，自动处理 GB18030 / UTF-8 混合编码。"""
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
            return _decode_best(response.content)
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
    frame["source_url"] = SOURCE_REGISTRY["margin_balance"]["url"]
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


def _pbc_social_financing_archives(years: int = DEFAULT_PBC_YEARS) -> list[tuple[int, str]]:
    """解析央行「统计数据」栏目，返回 [(年份, 社融栏目地址)]，按年份降序截取。"""
    page = _request_text(PBC_STATS_INDEX_URL)
    pattern = re.compile(r"<a[^>]+href='([^']+)'[^>]*>([^<]+)</a>")
    found: list[tuple[int, str]] = []
    current_year: int | None = None
    for href, text in pattern.findall(page):
        label = text.strip()
        match = re.match(r"(\d{4})年统计数据", label)
        if match:
            current_year = int(match.group(1))
            continue
        if label == "社会融资规模" and current_year is not None:
            found.append((current_year, href))
    # 去重（同一年份可能多次出现），保留首次出现的地址
    unique: dict[int, str] = {}
    for year, href in found:
        unique.setdefault(year, href)
    ordered = sorted(unique.items(), reverse=True)
    return ordered[:years]


def _pbc_stock_table_url(archive_url: str) -> str | None:
    """从社融栏目页中取「社会融资规模存量统计表」的 HTML 附件地址。"""
    page = _request_text(archive_url)
    anchor = page.find("社会融资规模存量统计表")
    segment = page[anchor:] if anchor >= 0 else page
    match = re.search(r'href="([^"]+\.htm)"', segment, flags=re.I)
    if match:
        return match.group(1)
    # 兜底：栏目页固定两个 htm 附件（增量在前、存量在后）
    links = re.findall(r'href="([^"]+\.htm)"', page, flags=re.I)
    return links[-1] if links else None


def parse_afre_stock_table(html: str) -> list[tuple[float, float]]:
    """解析《社会融资规模存量统计表》：返回 [(存量万亿元, 同比%)]，按 1 月起逐月。"""
    row = re.search(r"<tr[^>]*>(?:(?!</tr>).)*AFRE\(stock\).*?</tr>", html, flags=re.S)
    if not row:
        return []
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row.group(0), flags=re.S)
    texts = [re.sub(r"<[^>]+>", "", cell).replace("&nbsp;", " ").strip() for cell in cells]
    start = next((index for index, text in enumerate(texts) if "AFRE(stock)" in text), -1) + 1
    if start <= 0:
        return []
    values = texts[start:]
    series: list[tuple[float, float]] = []
    for index in range(0, len(values) - 1, 2):
        stock, yoy = values[index], values[index + 1]
        if not stock or not yoy:
            break  # 未发布月份留空，其后的月份同样未发布
        try:
            series.append((float(stock), float(yoy)))
        except ValueError:
            break
    return series


def fetch_social_financing(
    years: int = DEFAULT_PBC_YEARS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """抓取社融存量（万亿元）与存量同比（%），返回 (存量, 同比) 两个 DataFrame。"""
    stock_rows: list[dict] = []
    yoy_rows: list[dict] = []
    for year, archive in _pbc_social_financing_archives(years):
        table_url = _pbc_stock_table_url(f"{PBC_BASE}{archive}" if archive.startswith("/") else archive)
        if not table_url:
            continue
        source_url = f"{PBC_BASE}{table_url}" if table_url.startswith("/") else table_url
        series = parse_afre_stock_table(_request_text(source_url))
        for month, (stock, yoy) in enumerate(series, start=1):
            day = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)
            stock_rows.append({"date": day, "value": stock, "source_url": source_url})
            yoy_rows.append({"date": day, "value": yoy, "source_url": source_url})
        time.sleep(0.3)
    stock_frame = pd.DataFrame(stock_rows).drop_duplicates("date").sort_values("date")
    yoy_frame = pd.DataFrame(yoy_rows).drop_duplicates("date").sort_values("date")
    return stock_frame.reset_index(drop=True), yoy_frame.reset_index(drop=True)


def update_liquidity_cache(progress=None) -> dict[str, int]:
    """抓取全部自动源并合并入本地缓存；返回各指标缓存后的总行数。"""

    def report(step: int, total: int, message: str) -> None:
        if progress is not None:
            progress(step, total, message)

    jobs = [
        ("dr007", lambda: fetch_dr007()),
        ("bond_10y", lambda: fetch_bond_10y()),
        ("margin_balance", lambda: fetch_margin_balance()),
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

    report(total, total, "正在更新社融存量与同比")
    try:
        stock_frame, yoy_frame = fetch_social_financing()
        save_metric("social_financing_stock", stock_frame)
        save_metric("social_financing_yoy", yoy_frame)
    except Exception as exc:  # noqa: BLE001
        report(total, total, f"社融数据更新失败：{exc}")
    counts["social_financing_stock"] = len(load_cached_metric("social_financing_stock"))
    counts["social_financing_yoy"] = len(load_cached_metric("social_financing_yoy"))

    report(total, total, "流动性数据更新完成")
    return counts


def load_all_metrics() -> dict[str, pd.Series]:
    """读全部指标缓存，返回 {metric_id: 以日期为索引的 Series}。

    兼容旧版命名：两融余额曾存于 margin_balance_yoy.csv（内容是水平值）。
    """
    result: dict[str, pd.Series] = {}
    for metric_id in SOURCE_REGISTRY:
        frame = load_cached_metric(metric_id)
        if frame.empty and metric_id == "margin_balance":
            frame = load_cached_metric("margin_balance_yoy")
        if frame.empty:
            continue
        series = frame.set_index("date")["value"].sort_index()
        series = series[~series.index.duplicated(keep="last")]
        result[metric_id] = series
    return result
