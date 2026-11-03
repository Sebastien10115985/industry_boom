"""从国家统计局官方发布中获取分行业月度景气数据。

数据来源（均为官方公开发布页，无需授权）：

- 规模以上工业生产发布：分行业工业增加值当月/累计同比增长速度（Excel 附件）
- 规模以上工业企业利润发布：分行业利润总额同比增长速度（Excel 附件“表3 …（分行业）”）

发布列表页可分页回溯，因此能够回补近若干个月的月度观测值。
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests

from openpyxl import load_workbook


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_HISTORY_PATH = PROJECT_DIR / "data" / "prosperity" / "nbs_monthly.csv"

LISTING_URL = "https://www.stats.gov.cn/sj/zxfb/"
PROFIT_TITLE_KEY = "规模以上工业企业利润"
VALUE_ADDED_TITLE_KEY = "规模以上工业增加值增长"

HISTORY_COLUMNS = [
    "month",
    "stat_industry",
    "industry_value_added_yoy",
    "profit_yoy",
    "source_value_added",
    "source_profit",
    "fetched_at",
]

# 统计行业大类名称中需要剔除的合计与门类行
AGGREGATE_ROWS = {
    "总计",
    "其中：采矿业",
    "制造业",
    "电力、热力、燃气及水生产和供应业",
    "其中：国有控股企业",
    "其中：股份制企业",
    "其中：私营企业",
    "外商及港澳台投资企业",
    "外商及港澳台商投资企业",
}


class NbsError(RuntimeError):
    """国家统计局数据获取或解析失败。"""


@dataclass(frozen=True)
class Release:
    month: str
    title: str
    url: str


class NbsClient:
    """低频串行的国家统计局发布页访问客户端。"""

    def __init__(self, minimum_interval: float = 1.5, timeout: float = 60.0) -> None:
        self.minimum_interval = minimum_interval
        self.timeout = timeout
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.stats.gov.cn/",
            }
        )

    def _get(self, url: str) -> requests.Response:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.minimum_interval:
            time.sleep(self.minimum_interval - elapsed)
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise NbsError(f"访问国家统计局失败：{exc}") from exc
        self._last_call = time.monotonic()
        if response.status_code != 200:
            raise NbsError(f"国家统计局返回 {response.status_code}：{url}")
        return response

    def _text(self, url: str) -> str:
        response = self._get(url)
        content = response.content
        for encoding in ("utf-8", "gbk", "gb18030"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="ignore")

    def _workbook(self, url: str):
        response = self._get(url)
        if "sheet" not in (response.headers.get("content-type") or ""):
            raise NbsError(f"附件不是 Excel：{url}")
        return load_workbook(io.BytesIO(response.content), data_only=True)


def month_from_title(title: str) -> str:
    """从发布标题中解析统计月份，返回 YYYY-MM。"""
    text = title.replace("—", "-").replace("～", "-").replace("~", "-")
    range_match = re.search(r"(\d{4})年(\d{1,2})-(\d{1,2})月", text)
    if range_match:
        year, _, end_month = range_match.groups()
        return f"{int(year):04d}-{int(end_month):02d}"
    single = re.search(r"(\d{4})年(\d{1,2})月", text)
    if single:
        year, month = single.groups()
        return f"{int(year):04d}-{int(month):02d}"
    raise NbsError(f"无法从标题解析月份：{title}")


def _listing_urls(max_pages: int) -> list[str]:
    urls = [LISTING_URL]
    urls.extend(f"{LISTING_URL}index_{page}.html" for page in range(1, max_pages))
    return urls


def _parse_listing(html: str) -> list[tuple[str, str]]:
    pattern = re.compile(r'href="(\./\d{6}/t\d{8}_\d+\.html)"[^>]*title=\'([^\']+)\'')
    return [(title, urljoin(LISTING_URL, href[2:])) for href, title in pattern.findall(html)]


def find_releases(client: NbsClient, kind: str, months: int, max_pages: int = 12) -> list[Release]:
    """在发布列表中查找最近若干个月的指定类型发布。"""
    keyword = PROFIT_TITLE_KEY if kind == "profit" else VALUE_ADDED_TITLE_KEY
    found: dict[str, Release] = {}
    for url in _listing_urls(max_pages):
        try:
            items = _parse_listing(client._text(url))
        except NbsError:
            break
        for title, href in items:
            if keyword not in title:
                continue
            try:
                month = month_from_title(title)
            except NbsError:
                continue
            found.setdefault(month, Release(month=month, title=title, url=href))
        if len(found) >= months:
            break
    return sorted(found.values(), key=lambda item: item.month, reverse=True)[:months]


def _cell(row: tuple, index: int) -> str:
    if index >= len(row):
        return ""
    value = row[index]
    return "" if value is None else str(value).strip()


def _numeric(value: str) -> float:
    text = str(value).replace(",", "").replace("%", "").replace("…", "").strip()
    if not text or text.startswith("（"):
        return float("nan")
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _attachment_urls(client: NbsClient, article_url: str) -> list[str]:
    html = client._text(article_url)
    return [urljoin(article_url, href) for href in re.findall(r'href="([^"]+\.xlsx?)"', html)]


def parse_profit_workbook(workbook) -> dict[str, float]:
    """解析“规模以上工业企业主要财务指标（分行业）”中的利润总额同比增长。"""
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        title = " ".join(_cell(row, 0) for row in rows[:2])
        if "分行业" not in title and not any("利润总额" in _cell(row, i) for row in rows[:6] for i in range(len(row))):
            continue
        header_index = -1
        industry_col = 0
        for index, row in enumerate(rows[:8]):
            cells = [_cell(row, i) for i in range(len(row))]
            normalized = [cell.replace(" ", "") for cell in cells]
            if "行业" in normalized or "分组" in normalized:
                header_index = index
                industry_col = normalized.index("行业") if "行业" in normalized else normalized.index("分组")
                break
        if header_index < 0:
            continue
        header = [_cell(rows[header_index], i) for i in range(len(rows[header_index]))]
        if "利润总额" not in header:
            continue
        profit_col = header.index("利润总额")
        value_col = profit_col
        for offset in (1, 2):
            sub_header = [_cell(rows[header_index + offset], i) for i in range(len(rows[header_index + offset]))]
            if profit_col + 1 < len(sub_header) and "同比增长" in sub_header[profit_col + 1]:
                value_col = profit_col + 1
                break
            if "同比增长" in sub_header[profit_col]:
                value_col = profit_col
                break
        result: dict[str, float] = {}
        for row in rows[header_index + 2 :]:
            name = _cell(row, industry_col).replace(" ", "")
            if not name or name.startswith("注") or name in AGGREGATE_ROWS or name.startswith("其中："):
                continue
            value = _numeric(_cell(row, value_col))
            if pd.isna(value):
                continue
            result[name] = value
        if result:
            return result
    raise NbsError("未在工业企业利润附件中找到分行业利润总额数据")


def parse_value_added_workbook(workbook) -> dict[str, float]:
    """解析“规模以上工业生产主要数据”中主要行业增加值的当月同比增长。"""
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        start_index = -1
        growth_cols: list[int] = []
        for index, row in enumerate(rows[:8]):
            cells = [_cell(row, i) for i in range(len(row))]
            growth = [i for i, cell in enumerate(cells) if "同比增长" in cell]
            if len(growth) >= 2:
                growth_cols = growth
                header_index = index
                break
        if not growth_cols:
            continue
        for index, row in enumerate(rows):
            if _cell(row, 1).replace(" ", "") == "主要行业增加值":
                start_index = index + 1
                break
        if start_index < 0:
            continue
        value_col = growth_cols[0]
        result: dict[str, float] = {}
        for row in rows[start_index:]:
            name = _cell(row, 1).replace(" ", "")
            if not name or name.startswith("规模以上工业主要产品") or name.startswith("注"):
                break
            if name in AGGREGATE_ROWS or name.startswith("其中："):
                continue
            value = _numeric(_cell(row, value_col))
            if pd.isna(value):
                continue
            result[name] = value
        if result:
            return result
    raise NbsError("未在工业生产附件中找到主要行业增加值数据")


def collect_monthly_records(
    months: int = 6,
    client: NbsClient | None = None,
    max_pages: int = 12,
) -> pd.DataFrame:
    """抓取近 months 个月的分行业工业增加值同比增长与利润总额同比增长。"""
    client = client or NbsClient()
    profit_releases = find_releases(client, "profit", months, max_pages)
    value_added_releases = find_releases(client, "value_added", months, max_pages)
    if not profit_releases and not value_added_releases:
        raise NbsError("未找到规模以上工业增加值或工业企业利润的月度发布")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    records: dict[tuple[str, str], dict[str, object]] = {}

    def put(month: str, industry: str, field: str, value: float, source: str, source_field: str) -> None:
        key = (month, industry)
        record = records.setdefault(
            key,
            {
                "month": month,
                "stat_industry": industry,
                "industry_value_added_yoy": float("nan"),
                "profit_yoy": float("nan"),
                "source_value_added": "",
                "source_profit": "",
                "fetched_at": fetched_at,
            },
        )
        record[field] = value
        record[source_field] = source

    for release in value_added_releases:
        for url in _attachment_urls(client, release.url):
            try:
                values = parse_value_added_workbook(client._workbook(url))
            except NbsError:
                continue
            for industry, value in values.items():
                put(release.month, industry, "industry_value_added_yoy", value, release.url, "source_value_added")
            break

    for release in profit_releases:
        for url in _attachment_urls(client, release.url):
            try:
                values = parse_profit_workbook(client._workbook(url))
            except NbsError:
                continue
            for industry, value in values.items():
                put(release.month, industry, "profit_yoy", value, release.url, "source_profit")
            break

    if not records:
        raise NbsError("未能从国家统计局发布中解析出分行业数据")
    frame = pd.DataFrame(list(records.values()))
    return frame[HISTORY_COLUMNS].sort_values(["month", "stat_industry"]).reset_index(drop=True)


def load_history(path: str | Path = DEFAULT_HISTORY_PATH) -> pd.DataFrame:
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    frame = pd.read_csv(source, encoding="utf-8-sig")
    for column in HISTORY_COLUMNS:
        if column not in frame:
            frame[column] = pd.NA
    return frame[HISTORY_COLUMNS]


def merge_history(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    """按月份与统计行业合并，新数据覆盖旧数据。"""
    columns = HISTORY_COLUMNS

    def align(frame: pd.DataFrame) -> pd.DataFrame:
        prepared = frame.copy()
        for column in columns:
            if column not in prepared.columns:
                prepared[column] = pd.NA
        return prepared[columns]

    frames = [align(frame) for frame in (existing, incoming) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=columns)
    combined = pd.concat(frames, ignore_index=True)
    for column in ("industry_value_added_yoy", "profit_yoy"):
        combined[column] = pd.to_numeric(combined[column], errors="coerce")
    combined = combined.drop_duplicates(["month", "stat_industry"], keep="last")
    return combined.sort_values(["month", "stat_industry"]).reset_index(drop=True)


def save_history(frame: pd.DataFrame, path: str | Path = DEFAULT_HISTORY_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False, encoding="utf-8-sig")
    return target


def update_history(
    months: int = 6,
    path: str | Path = DEFAULT_HISTORY_PATH,
    client: NbsClient | None = None,
    max_pages: int = 12,
) -> pd.DataFrame:
    """抓取近 months 个月数据并入本地历史库，返回合并后的完整历史。"""
    incoming = collect_monthly_records(months=months, client=client, max_pages=max_pages)
    merged = merge_history(load_history(path), incoming)
    save_history(merged, path)
    return merged


def import_history(
    table: pd.DataFrame,
    path: str | Path = DEFAULT_HISTORY_PATH,
) -> pd.DataFrame:
    """导入人工整理的分行业月度景气数据。"""
    frame = table.copy()
    rename = {}
    for column in frame.columns:
        key = str(column).strip().lower()
        if key in {"月份", "统计月份", "month"}:
            rename[column] = "month"
        elif key in {"统计行业", "行业", "行业大类", "stat_industry"}:
            rename[column] = "stat_industry"
        elif key in {"工业增加值增长率", "工业增加值同比", "industry_value_added_yoy"}:
            rename[column] = "industry_value_added_yoy"
        elif key in {"利润增长率", "利润总额同比增长", "profit_yoy"}:
            rename[column] = "profit_yoy"
    frame = frame.rename(columns=rename)
    missing = {"month", "stat_industry"}.difference(frame.columns)
    if missing:
        raise ValueError(f"景气数据表缺少字段：{', '.join(sorted(missing))}")
    frame["month"] = frame["month"].map(lambda value: month_from_title(str(value)) if not re.fullmatch(r"\d{4}-\d{2}", str(value).strip()) else str(value).strip())
    for column in ("industry_value_added_yoy", "profit_yoy"):
        if column not in frame:
            frame[column] = pd.NA
    merged = merge_history(load_history(path), frame)
    save_history(merged, path)
    return merged
