"""海关总署月度公报「重点出口商品量值表」抓取与解析。

中文站（www.customs.gov.cn）有瑞数 WAF 与云防御，自动化访问易被拦截；
英文站 english.customs.gov.cn 的 Monthly Bulletin
“(5) China's Major Exports by Quantity and Value (in CNY)”与中文
“重点出口商品量值表（人民币值）”同源同表，可直接 requests 抓取。
金额单位：亿元人民币；同比：累计金额比上年同期（±%）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests

EN_BASE = "http://english.customs.gov.cn"
MONTHLY_BULLETIN_URL = f"{EN_BASE}/statics/report/preliminary.html"

DEFAULT_CUSTOMS_PATH = Path(__file__).parent / "data" / "prosperity" / "customs_monthly.csv"

CUSTOMS_COLUMNS = (
    "month",
    "commodity_cn",
    "commodity_en",
    "export_month_amount_yi",
    "export_cum_amount_yi",
    "export_cum_yoy_pct",
    "source_url",
    "fetched_at",
)

#: 重点跟踪的出口商品：（中文名， 英文行名匹配关键词，不区分大小写）
#: 机电产品为各分项的汇总项，不单独跟踪。
CUSTOMS_EXPORT_ITEMS: tuple[tuple[str, str], ...] = (
    ("稀土", "rare-earth"),
    ("铝材", "unwrought aluminium"),
    ("高新技术产品", "hi-tech products"),
    ("通用机械设备", "general machines"),
    ("自动数据处理设备", "automatic data processing"),
    ("集成电路", "integrated circuit"),
    ("汽车", "motor vehicles"),
    ("汽车零配件", "parts and accessories of veh"),
    ("手机", "mobile phones"),
    ("家用电器", "electric appliances of house"),
    ("医疗仪器及器械", "medical or surgical"),
    ("船舶", "ships"),
)

_MONTH_EN_TO_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}


class CustomsError(Exception):
    """海关数据抓取或解析失败。"""


def _get(url: str, timeout: int = 60) -> str:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        raise CustomsError(f"请求海关网站失败：{url}（{exc}）") from exc
    if resp.status_code != 200:
        raise CustomsError(f"海关网站返回 {resp.status_code}：{url}")
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _strip_html(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _numeric(text: str) -> float:
    value = str(text).replace(",", "").replace("\u00a0", "").strip()
    if not value or value in {"-", "—", "…"} or value.startswith("（"):
        return float("nan")
    try:
        return float(value)
    except ValueError:
        return float("nan")


def find_major_exports_links(html: str) -> dict[str, str]:
    """从月度公报列表页解析“(5) Major Exports (in CNY)”各月份文章链接。

    返回 {月份链接文本(如 "Oct."), 文章绝对 URL}；未发布的月份无链接。
    """
    idx = html.find("(5) China's Major Exports by Quantity and Value (in CNY)")
    if idx < 0:
        raise CustomsError("列表页中未找到重点出口商品量值表栏目")
    segment = html[idx:]
    end = segment.find("(5) China's Major Exports by Quantity and Value (in USD)")
    if end > 0:
        segment = segment[:end]
    pattern = re.compile(
        r"href=(?:\"|')?(http://english\.customs\.gov\.cn/Statics/[a-f0-9-]+\.html)"
        r"[^>]*>\s*(?:<[^>]+>\s*)*((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.)",
        re.I,
    )
    links: dict[str, str] = {}
    for url, month in pattern.findall(segment):
        links[month.rstrip(".")] = url
    if not links:
        raise CustomsError("列表页中未解析到重点出口商品量值表的月份链接")
    return links


def month_from_exports_title(title: str) -> str:
    """从文章标题“(5) ..., Oct 2026 (in CNY)”解析统计月份，返回 YYYY-MM。"""
    match = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+(\d{4})\b", title)
    if not match:
        raise CustomsError(f"无法从标题解析月份：{title[:80]}")
    month_num = _MONTH_EN_TO_NUM[match.group(1).lower()[:3]]
    return f"{int(match.group(2)):04d}-{month_num:02d}"


def parse_major_exports(html: str) -> tuple[str, dict[str, tuple[float, float, float]]]:
    """解析重点出口商品量值表文章页。

    返回 (月份 YYYY-MM, {商品英文行名: (当月金额, 累计金额, 累计金额同比%)}）。
    """
    heading = re.search(r"\(5\)\s*China's Major Exports[^<]*", html)
    if not heading:
        raise CustomsError("文章页中未找到重点出口商品量值表标题")
    month = month_from_exports_title(_strip_html(heading.group(0)))
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    items: dict[str, tuple[float, float, float]] = {}
    for row in rows:
        cells = [_strip_html(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(cells) < 10 or cells[0] in {"", "Commodity"}:
            continue
        if cells[0].startswith("Quantity"):
            continue
        if any(keyword in cells[0].lower() for keyword in ("note:", "scope of commodit")):
            continue
        name = cells[0].rstrip("*").strip()
        month_value = _numeric(cells[3])
        cum_value = _numeric(cells[5])
        cum_yoy = _numeric(cells[9])
        items[name] = (month_value, cum_value, cum_yoy)
    if not items:
        raise CustomsError("重点出口商品量值表中未解析到数据行")
    return month, items


def match_tracked_items(
    items: dict[str, tuple[float, float, float]],
    strict: bool = False,
) -> dict[str, tuple[str, float, float, float]]:
    """将英文商品行名映射为中文重点跟踪商品，返回 {中文名: (英文名, 当月金额, 累计金额, 累计同比)}。

    strict=True 时任一重点商品缺失即抛错；否则跳过缺失项（由调用方提示）。
    """
    matched: dict[str, tuple[str, float, float, float]] = {}
    lowered = {name.lower(): values for name, values in items.items()}
    missing: list[str] = []
    for cn_name, keyword in CUSTOMS_EXPORT_ITEMS:
        for en_name, values in lowered.items():
            if keyword in en_name:
                matched[cn_name] = (en_name, *values)
                break
        else:
            missing.append(cn_name)
    if missing and strict:
        raise CustomsError(f"量值表中未找到重点商品：{'、'.join(missing)}")
    return matched


def merge_customs(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    """按月份与商品合并，新数据覆盖旧数据。"""
    columns = list(CUSTOMS_COLUMNS)

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
    for column in ("export_month_amount_yi", "export_cum_amount_yi", "export_cum_yoy_pct"):
        combined[column] = pd.to_numeric(combined[column], errors="coerce")
    combined = combined.groupby(["month", "commodity_cn"], as_index=False, sort=False).agg(
        lambda series: series.dropna().iloc[-1] if series.notna().any() else pd.NA
    )
    return combined.sort_values(["month", "commodity_cn"]).reset_index(drop=True)


def load_customs(path: str | Path = DEFAULT_CUSTOMS_PATH) -> pd.DataFrame:
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame(columns=list(CUSTOMS_COLUMNS))
    frame = pd.read_csv(source, encoding="utf-8-sig")
    for column in CUSTOMS_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame[list(CUSTOMS_COLUMNS)].sort_values(["month", "commodity_cn"]).reset_index(drop=True)


def save_customs(frame: pd.DataFrame, path: str | Path = DEFAULT_CUSTOMS_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False, encoding="utf-8-sig")
    return target


def update_customs(
    months: int = 12,
    path: str | Path = DEFAULT_CUSTOMS_PATH,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """抓取近 months 期重点出口商品量值表并合并入本地库，返回合并后的完整数据。

    优先使用传入 session（便于外部上传 HTML 解析复用），否则新建请求。
    """
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    getter = (session.get if session is not None else _get)
    if session is not None:
        session.headers.update(_HEADERS)

    def fetch_text(url: str) -> str:
        if session is None:
            return _get(url)
        resp = getter(url, timeout=60)
        if resp.status_code != 200:
            raise CustomsError(f"海关网站返回 {resp.status_code}：{url}")
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    listing = fetch_text(MONTHLY_BULLETIN_URL)
    links = find_major_exports_links(listing)
    # 按日历月份顺序取最近 months 期（未发布的月份在列表中无链接）
    ordered_months = [name for name in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec") if name in links]
    targets = ordered_months[-months:]

    records: list[dict[str, object]] = []
    for month_name in targets:
        url = links[month_name]
        month, items = parse_major_exports(fetch_text(url))
        matched = match_tracked_items(items)
        for cn_name, (en_name, month_value, cum_value, cum_yoy) in matched.items():
            records.append(
                {
                    "month": month,
                    "commodity_cn": cn_name,
                    "commodity_en": en_name,
                    "export_month_amount_yi": month_value,
                    "export_cum_amount_yi": cum_value,
                    "export_cum_yoy_pct": cum_yoy,
                    "source_url": url,
                    "fetched_at": fetched_at,
                }
            )
    if not records:
        raise CustomsError("未能从海关月度公报解析出重点出口商品数据")
    merged = merge_customs(load_customs(path), pd.DataFrame(records))
    save_customs(merged, path)
    return merged


def import_customs_article(html: str, path: str | Path = DEFAULT_CUSTOMS_PATH) -> pd.DataFrame:
    """导入用户上传的重点出口商品量值表文章 HTML（中文站另存或英文站页面均可）。"""
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    month, items = parse_major_exports(html)
    matched = match_tracked_items(items)
    records = [
        {
            "month": month,
            "commodity_cn": cn_name,
            "commodity_en": en_name,
            "export_month_amount_yi": month_value,
            "export_cum_amount_yi": cum_value,
            "export_cum_yoy_pct": cum_yoy,
            "source_url": "",
            "fetched_at": fetched_at,
        }
        for cn_name, (en_name, month_value, cum_value, cum_yoy) in matched.items()
    ]
    merged = merge_customs(load_customs(path), pd.DataFrame(records))
    save_customs(merged, path)
    return merged
