"""海关重点出口商品量值表历史回填工具（2023 年以来的中文站数据）。

背景：英文站月度公报列表页只保留当年期数，2023—2025 年历史期仅存在于
中文站「海关统计月报」栏目；中文站有瑞数动态安全（纯 requests 返回 412），
但带有效浏览器 Cookie 的请求可直接通过。

用法：
  1. 用本机浏览器打开 http://www.customs.gov.cn 任意页面；
  2. F12 → 网络(Network) → 任意 www.customs.gov.cn 请求 → 复制「Cookie」整串；
  3. 运行（先 dry-run 预览，确认无误后加 --apply 写入）：
     python tools/backfill_customs_cn.py --cookie "粘贴整串Cookie"
     python tools/backfill_customs_cn.py --cookie "..." --apply

安全设计：
  - 写入前自动备份 data/prosperity/customs_monthly.csv；
  - 通过 merge_customs 合并（同月同商品新数据优先，已有数据不丢失）；
  - 每期解析出不足 12 项时跳过并记录，避免脏数据入库。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from customs_provider import (
    CUSTOMS_EXPORT_ITEMS,
    CustomsError,
    _HEADERS,
    _numeric,
    _strip_html,
    load_customs,
    merge_customs,
    save_customs,
)

HOME = "http://www.customs.gov.cn"
#: 历史回填范围（含端点）
START_MONTH = (2023, 1)
END_MONTH = (2025, 12)

#: 中文站商品行名匹配：（中文名, 包含关键词列表, 排除关键词列表）。
#: 关键词按列表顺序尝试；排除词用于区分「汽车」与「汽车零配件」等。
CN_MATCH_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "稀土": (("稀土",), ()),
    "铝材": (("未锻轧铝", "铝材"), ()),
    "高新技术产品": (("高新技术产品",), ()),
    "通用机械设备": (("通用机械设备", "通用机械"), ()),
    "自动数据处理设备": (("自动数据处理设备",), ()),
    "集成电路": (("集成电路",), ()),
    "汽车": (("汽车（含底盘", "汽车(含底盘", "汽车含底盘", "汽车"), ("零配件", "零件")),
    "汽车零配件": (("汽车零配件", "汽车零件"), ()),
    "手机": (("手机", "手持无线电话"), ()),
    "家用电器": (("家用电器",), ()),
    "医疗仪器及器械": (("医疗仪器及器械", "医疗仪器", "医疗器械"), ()),
    "船舶": (("船舶",), ()),
}


def make_session(cookie: str, user_agent: str | None = None) -> requests.Session:
    """瑞数 Cookie 与浏览器 User-Agent 绑定，UA 必须与取 Cookie 的浏览器一致。"""
    session = requests.Session()
    session.headers.update({
        **_HEADERS,
        "User-Agent": user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": HOME + "/",
        "Cookie": cookie.strip(),
    })
    return session


def get_text(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=60)
    if resp.status_code == 412:
        raise CustomsError(
            f"412（Cookie 失效、过期或 User-Agent 不匹配）：{url}\n"
            "请重新取 Cookie，并用 --ua 传入与浏览器完全一致的 User-Agent"
            "（浏览器 F12 Console 执行 navigator.userAgent 复制输出）。"
        )
    if resp.status_code != 200:
        raise CustomsError(f"返回 {resp.status_code}：{url}")
    resp.encoding = resp.apparent_encoding or "utf-8"
    text = resp.text
    if len(text) < 2500 or "$_ts" in text[:4000]:
        raise CustomsError(f"Cookie 失效或被 WAF 拦截：{url}")
    return text


def find_report_section(session: requests.Session) -> str:
    """从首页导航找「海关统计月报」栏目地址。"""
    home = get_text(session, HOME)
    for text_hint in ("统计月报", "统计快讯", "统计"):
        pattern = re.compile(
            r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>[^<]*" + text_hint + r"[^<]*</a>", re.I
        )
        for match in pattern.finditer(home):
            url = urljoin(HOME, match.group(1))
            if "/customs/" in url and "index" in url:
                return url
    raise CustomsError("首页未找到统计月报栏目链接，请手动确认栏目地址")


def collect_article_links(session: requests.Session, section_url: str) -> dict[str, str]:
    """翻页采集栏目内 {标题: 文章URL}，返回全部文章。"""
    articles: dict[str, str] = {}
    page_url: str | None = section_url
    seen_pages: set[str] = set()
    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        html = get_text(session, page_url)
        base = page_url
        for match in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, re.S):
            title = _strip_html(match.group(2))
            if not title or len(title) < 8:
                continue
            articles.setdefault(title, urljoin(base, match.group(1)))
        # 静态分页：index_N.html（N 递增）
        next_match = re.search(r"href=[\"']([^\"']*index_(\d+)\.html)[\"']", html)
        if next_match:
            candidate = urljoin(base, next_match.group(1))
            page_url = candidate if int(next_match.group(2)) <= 60 else None
        else:
            page_url = None
    return articles


def month_from_cn_title(title: str) -> str | None:
    match = re.search(r"(20\d{2})\s*年\s*(1[0-2]|[1-9])\s*月", title)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return None


def month_in_range(month: str) -> bool:
    y, m = (int(part) for part in month.split("-"))
    return (START_MONTH[0], START_MONTH[1]) <= (y, m) <= (END_MONTH[0], END_MONTH[1])


def select_backfill_articles(articles: dict[str, str]) -> dict[str, str]:
    """筛选标题含量值表特征且月份落在回填区间的文章。"""
    picked: dict[str, str] = {}
    for title, url in articles.items():
        if not any(kw in title for kw in ("量值表", "重点商品", "Major Exports")):
            continue
        month = month_from_cn_title(title)
        if month and month_in_range(month):
            picked.setdefault(month, url)
    return picked


def parse_cn_exports(html: str, month: str) -> dict[str, tuple[str, float, float, float]]:
    """解析中文站量值表文章页，返回 {中文名: (中文行名, 当月, 累计, 累计同比)}。

    表结构与英文站同源：商品行含 10+ 单元格，金额当月/累计/累计同比分别位于
    第4、6、10列（0 起始 3/5/9）；商品名匹配按 CN_MATCH_RULES。
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    table: list[list[str]] = []
    for row in rows:
        cells = [_strip_html(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(cells) >= 10:
            table.append(cells)
    matched: dict[str, tuple[str, float, float, float]] = {}
    for cells in table:
        name = cells[0].rstrip("*").strip()
        if name in {"", "商品", "Commodity", "Quantity", "Unit"} or name.startswith(("注", "Quantity", "Scope")):
            continue
        for cn_name, (keywords, excludes) in CN_MATCH_RULES.items():
            if cn_name in matched:
                continue
            if any(kw in name for kw in keywords) and not any(ex in name for ex in excludes):
                values = (_numeric(cells[3]), _numeric(cells[5]), _numeric(cells[9]))
                matched[cn_name] = (name, *values)
                break
    return matched


def backfill(cookie: str, apply: bool, path: str | Path | None = None, user_agent: str | None = None) -> None:
    session = make_session(cookie, user_agent)
    section = find_report_section(session)
    print(f"统计栏目：{section}")
    articles = collect_article_links(session, section)
    print(f"栏目共采集到 {len(articles)} 个文章链接")
    targets = select_backfill_articles(articles)
    print(f"回填区间 {START_MONTH[0]}-{START_MONTH[1]:02d} ~ {END_MONTH[0]}-{END_MONTH[1]:02d}："
          f"命中 {len(targets)} 期")
    if not targets:
        raise CustomsError("未在栏目中找到目标期数文章，请检查 Cookie 或栏目地址")

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records: list[dict[str, object]] = []
    for month in sorted(targets):
        url = targets[month]
        try:
            matched = parse_cn_exports(get_text(session, url), month)
        except CustomsError as exc:
            print(f"  跳过 {month}：{exc}")
            continue
        short = {name: f"{v[1]:.0f}/{v[2]:.0f}/{v[3]:.1f}" for name, v in matched.items()}
        missing = [cn for cn, _, _ in CUSTOMS_EXPORT_ITEMS if cn not in matched]
        status = "OK " if not missing else f"缺{len(missing)}项:{'/'.join(missing)}"
        print(f"  {month} [{status}] 样例：稀土={short.get('稀土')} 汽车={short.get('汽车')}")
        if len(matched) < len(CUSTOMS_EXPORT_ITEMS) and apply:
            print(f"    （不完整，跳过入库）")
            continue
        for cn_name, (row_name, month_value, cum_value, cum_yoy) in matched.items():
            records.append(
                {
                    "month": month,
                    "commodity_cn": cn_name,
                    "commodity_en": row_name,
                    "export_month_amount_yi": month_value,
                    "export_cum_amount_yi": cum_value,
                    "export_cum_yoy_pct": cum_yoy,
                    "source_url": url,
                    "fetched_at": fetched_at,
                }
            )

    if apply:
        import pandas as pd

        frame = load_customs(path)
        before_rows, before_min, before_max = len(frame), frame["month"].min(), frame["month"].max()
        backup = Path(path or __import__("customs_provider", fromlist=["DEFAULT_CUSTOMS_PATH"]).DEFAULT_CUSTOMS_PATH)
        shutil.copy2(backup, backup.with_suffix(".csv.bak"))
        merged = merge_customs(frame, pd.DataFrame(records))
        save_customs(merged, path)
        print(f"已写入：{before_rows} 行（{before_min}~{before_max}）→ {len(merged)} 行（{merged['month'].min()}~{merged['month'].max()}）")
        print(f"原数据备份于 {backup.with_suffix('.csv.bak')}")
    else:
        print(f"dry-run 完成，共 {len(records)} 条记录待写入；确认后加 --apply 执行。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookie", required=True, help="浏览器中 www.customs.gov.cn 的完整 Cookie 串")
    parser.add_argument("--ua", default=None, help="与取 Cookie 的浏览器完全一致的 User-Agent（Console 执行 navigator.userAgent）")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run 只预览）")
    parser.add_argument("--path", default=None, help="自定义 CSV 路径（默认 data/prosperity/customs_monthly.csv）")
    args = parser.parse_args()
    backfill(args.cookie, args.apply, args.path, args.ua)


if __name__ == "__main__":
    main()
