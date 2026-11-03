from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import pandas as pd


SEARCH_URL = "https://www.cninfo.com.cn/new/fulltextSearch/full"
ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
# 定期报告类别：年报、半年报、一季报、三季报
PERIODIC_REPORT_CATEGORIES = (
    "category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh"
)
MAX_SEARCH_PAGES = 20


def exchange_column(code: str) -> str:
    """巨潮公告查询按交易所分栏。"""
    if code.startswith("6"):
        return "sse"
    if code.startswith(("4", "8", "920")):
        return "bj"
    return "szse"
STATIC_BASE_URL = "https://static.cninfo.com.cn/"
FULL_REPORT_PATTERN = re.compile(
    r"(20\d{2})年(年度|半年度|第一季度|第三季度|一季度|三季度)报告"
)
EXCLUDED_TITLE_WORDS = ("摘要", "提示性公告", "英文版", "外文版", "取消")


class CninfoError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportRecord:
    company_id: str
    company_name: str
    announcement_id: str
    announcement_date: str
    report_period: str
    report_type: str
    title: str
    pdf_url: str
    source: str = "CNINFO"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def normalize_company_code(company_id: object) -> str:
    return str(company_id).strip().upper().split(".")[0].zfill(6)


def report_period_from_title(title: str) -> tuple[str, str] | None:
    match = FULL_REPORT_PATTERN.search(title.replace(" ", ""))
    if not match:
        return None
    year = int(match.group(1))
    report_type = match.group(2)
    month_day = {
        "第一季度": "03-31",
        "一季度": "03-31",
        "半年度": "06-30",
        "第三季度": "09-30",
        "三季度": "09-30",
        "年度": "12-31",
    }[report_type]
    return f"{year}-{month_day}", report_type


class CninfoClient:
    """巨潮资讯网页公告查询适配器；使用低频访问并保留来源 URL。"""

    def __init__(self, timeout: float = 30.0, minimum_interval: float = 0.8) -> None:
        try:
            import requests
        except ImportError as exc:
            raise CninfoError("缺少 requests，请先安装 requirements.txt") from exc
        self._requests = requests
        self.timeout = timeout
        self.minimum_interval = minimum_interval
        self._last_request_at = 0.0
        self._org_ids: dict[str, str | None] = {}
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; IndustryResearchTool/1.0; local research)",
                "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
                "Origin": "https://www.cninfo.com.cn",
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.minimum_interval:
            time.sleep(self.minimum_interval - elapsed)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        self._throttle()
        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            self._last_request_at = time.monotonic()
            response.raise_for_status()
            return response
        except self._requests.RequestException as exc:
            raise CninfoError(f"巨潮资讯请求失败：{exc}") from exc

    def resolve_org_id(self, code: str) -> str | None:
        """取巨潮内部机构 ID，用于精确定位该公司的公告列表。"""
        cached = self._org_ids.get(code)
        if cached is not None:
            return cached
        form = {
            "searchkey": code,
            "pageNum": 1,
            "pageSize": 5,
            "sortName": "pubdate",
            "sortType": "desc",
        }
        payload = self._request("POST", SEARCH_URL, data=form).json()
        found: str | None = None
        for item in payload.get("announcements") or []:
            if str(item.get("secCode") or "").strip() == code and item.get("orgId"):
                found = str(item["orgId"])
                break
        self._org_ids[code] = found
        return found

    def search_periodic_reports(
        self,
        company_id: object,
        start_date: str | date,
        end_date: str | date,
        page_size: int = 30,
    ) -> list[ReportRecord]:
        """检索该公司在窗口内的定期报告。

        优先用「机构 ID + 定期报告类别」精确查询该公司公告（可直接翻到窗口起点）；
        拿不到机构 ID 时退回全文检索并按日期倒序翻页。
        公告多的公司（大型药企、银行）一年几百条临时公告，定期报告常排在好几页之后，
        只看前一两页会漏掉半年报、季报，导致算不出上年同期增长率。
        """
        code = normalize_company_code(company_id)
        start = pd.Timestamp(start_date).normalize()
        end = pd.Timestamp(end_date).normalize()
        org_id = self.resolve_org_id(code)
        page = 1
        records: list[ReportRecord] = []
        while page <= MAX_SEARCH_PAGES:
            if org_id:
                form = {
                    "stock": f"{code},{org_id}",
                    "pageNum": page,
                    "pageSize": page_size,
                    "sortName": "pubdate",
                    "sortType": "desc",
                    "column": exchange_column(code),
                    "tabName": "fulltext",
                    "isHLtitle": "true",
                    "category": PERIODIC_REPORT_CATEGORIES,
                }
                url = ANNOUNCEMENT_URL
            else:
                form = {
                    "searchkey": code,
                    "pageNum": page,
                    "pageSize": page_size,
                    "sortName": "pubdate",
                    "sortType": "desc",
                }
                url = SEARCH_URL
            payload = self._request("POST", url, data=form).json()
            announcements = payload.get("announcements") or []
            page_dates: list[pd.Timestamp] = []
            for item in announcements:
                if str(item.get("secCode") or "").strip() != code:
                    continue
                timestamp = item.get("announcementTime")
                if timestamp is None:
                    continue
                announced_at = pd.to_datetime(float(timestamp), unit="ms").normalize()
                page_dates.append(announced_at)
                if announced_at < start or announced_at > end:
                    continue
                title = re.sub(r"<[^>]+>", "", str(item.get("announcementTitle") or "")).strip()
                parsed = report_period_from_title(title)
                if not parsed or any(word in title for word in EXCLUDED_TITLE_WORDS):
                    continue
                report_period, report_type = parsed
                adjunct_url = str(item.get("adjunctUrl") or "").lstrip("/")
                if not adjunct_url:
                    continue
                records.append(
                    ReportRecord(
                        company_id=str(company_id).strip().upper(),
                        company_name=str(item.get("secName") or code),
                        announcement_id=str(item.get("announcementId") or adjunct_url),
                        announcement_date=announced_at.date().isoformat(),
                        report_period=report_period,
                        report_type=report_type,
                        title=title,
                        pdf_url=STATIC_BASE_URL + adjunct_url,
                    )
                )
            total_records = int(payload.get("totalAnnouncement") or payload.get("totalRecordNum") or 0)
            total_pages = max(1, (total_records + page_size - 1) // page_size)
            # 已翻到窗口起点之前的公告，再往前只会更早，可以停止
            reached_start = bool(page_dates) and min(page_dates) <= start
            if reached_start or page >= total_pages or not announcements:
                break
            page += 1
        unique = {record.announcement_id: record for record in records}
        return list(unique.values())

    def download_pdf(self, url: str) -> bytes:
        response = self._request("GET", url, headers={"Referer": "https://www.cninfo.com.cn/"})
        content = response.content
        if not content.startswith(b"%PDF"):
            raise CninfoError(f"公告文件不是有效 PDF：{url}")
        return content
