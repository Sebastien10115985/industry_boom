"""事件自动采集：巨潮公告（默认）+ 可选 LLM 摘要/脉络整理。

采集只取「标题 + 日期 + 原文链接」，不解析 PDF，因此速度可控；
需要更深的内容时，可在页面上手工补写正文，或配置 LLM 由标题生成摘要。

LLM 为可选增强，通过环境变量启用（OpenAI 兼容接口）：
    EVT_LLM_BASE_URL  例如 https://api.deepseek.com/v1
    EVT_LLM_API_KEY   接口密钥
    EVT_LLM_MODEL     例如 deepseek-chat
未配置时，脉络整理走规则化模板，功能不受影响。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PARENT_DIR = Path(__file__).resolve().parent.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

from cninfo_provider import (  # noqa: E402
    ANNOUNCEMENT_URL,
    STATIC_BASE_URL,
    CninfoClient,
    CninfoError,
    exchange_column,
    normalize_company_code,
)

# 采集时跳过的例行公告（对时间线意义不大，且量大）
NOISE_PATTERNS = (
    r"董事会.*会议决议",
    r"监事会.*会议决议",
    r"股东大会.*(通知|决议)",
    r"关于召开",
    r"独立董事",
    r"月报表|证券变动",
    r"管理办法|管理制度|工作制度|公司章程|章程修正案",
    r"投资者关系活动记录",
    r"持续督导",
    r"更正公告|补充公告",
    r"股权激励计划草案",
    r"简式权益变动报告书",
)


def is_noise_title(title: str) -> bool:
    """例行公告判定：命中任一模式即视为噪音。"""
    return any(re.search(pattern, title) for pattern in NOISE_PATTERNS)


def search_announcements(
    code: str,
    start_date: str | date,
    end_date: str | date,
    page_size: int = 30,
    max_pages: int = 6,
    skip_noise: bool = True,
    keyword: str = "",
) -> list[dict]:
    """检索某公司在窗口内的全部公告（含临时公告），按日期倒序返回。"""
    normalized = normalize_company_code(code)
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    client = CninfoClient()
    org_id = client.resolve_org_id(normalized)
    records: list[dict] = []
    page = 1
    while page <= max_pages:
        if org_id:
            form = {
                "stock": f"{normalized},{org_id}",
                "pageNum": page,
                "pageSize": page_size,
                "sortName": "pubdate",
                "sortType": "desc",
                "column": exchange_column(normalized),
                "tabName": "fulltext",
                "isHLtitle": "true",
            }
            url = ANNOUNCEMENT_URL
        else:
            form = {
                "searchkey": normalized,
                "pageNum": page,
                "pageSize": page_size,
                "sortName": "pubdate",
                "sortType": "desc",
            }
            url = ANNOUNCEMENT_URL
        try:
            payload = client._request("POST", url, data=form).json()  # noqa: SLF001
        except CninfoError:
            break
        announcements = payload.get("announcements") or []
        page_dates: list[pd.Timestamp] = []
        for item in announcements:
            if str(item.get("secCode") or "").strip() != normalized:
                continue
            timestamp = item.get("announcementTime")
            if timestamp is None:
                continue
            announced_at = pd.to_datetime(float(timestamp), unit="ms").normalize()
            page_dates.append(announced_at)
            if announced_at < start or announced_at > end:
                continue
            title = re.sub(r"<[^>]+>", "", str(item.get("announcementTitle") or "")).strip()
            if not title:
                continue
            if skip_noise and is_noise_title(title):
                continue
            if keyword and keyword.lower() not in title.lower():
                continue
            adjunct_url = str(item.get("adjunctUrl") or "").lstrip("/")
            records.append(
                {
                    "date": announced_at.date().isoformat(),
                    "title": title,
                    "company_id": normalized,
                    "company_name": str(item.get("secName") or normalized),
                    "source_url": (STATIC_BASE_URL + adjunct_url) if adjunct_url else "",
                    "announcement_id": str(item.get("announcementId") or adjunct_url or title),
                }
            )
        total = int(payload.get("totalAnnouncement") or payload.get("totalRecordNum") or 0)
        total_pages = max(1, (total + page_size - 1) // page_size)
        reached_start = bool(page_dates) and min(page_dates) <= start
        if reached_start or page >= min(total_pages, max_pages) or not announcements:
            break
        page += 1
    return records


def announcements_to_events(records: list[dict], tags: list[str] | None = None) -> list[dict]:
    """把公告记录转成时间线事件草稿。"""
    default_tags = ["公告"] + [tag for tag in (tags or [])]
    events: list[dict] = []
    for record in records:
        events.append(
            {
                "date": record["date"],
                "title": record["title"],
                "content": f"{record.get('company_name', '')} {record['date']} 披露公告：{record['title']}",
                "tags": list(dict.fromkeys(default_tags)),
                "importance": 2,
                "source": "cninfo",
                "source_url": record.get("source_url", ""),
                "company_id": record.get("company_id", ""),
            }
        )
    return events


# ---------------------------------------------------------------- 可选 LLM


def llm_config() -> dict:
    base = os.environ.get("EVT_LLM_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("EVT_LLM_API_KEY", "").strip()
    model = os.environ.get("EVT_LLM_MODEL", "").strip() or "gpt-4o-mini"
    return {"base_url": base, "api_key": key, "model": model, "enabled": bool(base and key)}


def _call_llm(system: str, user: str, timeout: int = 60) -> str:
    """OpenAI 兼容 chat/completions 调用；失败时抛出异常，由调用方降级。"""
    import requests

    config = llm_config()
    if not config["enabled"]:
        raise RuntimeError("未配置 EVT_LLM_BASE_URL / EVT_LLM_API_KEY")
    response = requests.post(
        config["base_url"] + "/chat/completions",
        headers={
            "Authorization": "Bearer " + config["api_key"],
            "Content-Type": "application/json",
        },
        data=json.dumps(
            {
                "model": config["model"],
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload["choices"][0]["message"]["content"]).strip()


def _events_digest(topic: dict, events: list[dict], limit: int = 120) -> str:
    lines = [
        f"{event.get('date', '')}｜{event.get('title', '')}"
        + (
            "｜" + "、".join(str(tag) for tag in event.get("tags") or [])
            if event.get("tags")
            else ""
        )
        for event in events[:limit]
    ]
    header = f"主题：{topic.get('name', '')}（{topic.get('description', '') or '无描述'}）"
    return header + "\n" + "\n".join(lines)


def build_thread_text(topic: dict, events: list[dict]) -> str:
    """整理事件脉络：优先 LLM，未配置时按年份规则化归纳。"""
    if not events:
        return "该主题暂无可整理的事件。"
    if llm_config()["enabled"]:
        try:
            return _call_llm(
                "你是产业与公司研究助理。基于给定的事件清单，输出结构化的脉络整理："
                "先按阶段（萌芽/上行/过热/回落/出清/复苏等，视内容而定）划分，"
                "每个阶段说明时间范围、关键事件与因果逻辑，最后给出 3-5 条观察要点。"
                "只使用给定事实，不要编造，中文输出，用 Markdown。",
                _events_digest(topic, events),
            )
        except Exception as exc:  # noqa: BLE001 - LLM 失败不影响功能
            return _thread_fallback(topic, events) + f"\n\n（LLM 调用失败，已降级为规则化整理：{exc}）"
    return _thread_fallback(topic, events)


def _thread_fallback(topic: dict, events: list[dict]) -> str:
    """无 LLM 时的规则化脉络：按年份分组 + 标签分布 + 高优事件摘要。"""
    by_year: dict[str, list[dict]] = {}
    for event in sorted(events, key=lambda item: str(item.get("date") or "")):
        year = str(event.get("date") or "")[:4] or "未知"
        by_year.setdefault(year, []).append(event)

    lines = [f"# {topic.get('name', '')} · 事件脉络", ""]
    if topic.get("description"):
        lines += [topic["description"], ""]
    lines += [f"共 {len(events)} 条事件，覆盖 {min(by_year)}–{max(by_year)} 年。", ""]
    for year, items in by_year.items():
        lines.append(f"## {year} 年（{len(items)} 条）")
        for event in items:
            tags = "、".join(str(tag) for tag in event.get("tags") or [])
            suffix = f"（{tags}）" if tags else ""
            lines.append(f"- {event.get('date', '')} {event.get('title', '')}{suffix}")
        lines.append("")

    tag_count: dict[str, int] = {}
    for event in events:
        for tag in event.get("tags") or []:
            tag_count[str(tag)] = tag_count.get(str(tag), 0) + 1
    if tag_count:
        ranking = sorted(tag_count.items(), key=lambda item: item[1], reverse=True)
        lines += ["## 标签分布", ""]
        lines += [f"- {tag}：{count} 条" for tag, count in ranking[:10]]
        lines.append("")

    milestones = [
        event
        for event in events
        if int(event.get("importance") or 3) >= 4
    ]
    if milestones:
        lines += ["## 关键节点（重要性 ≥ 4）", ""]
        lines += [
            f"- {event.get('date', '')} {event.get('title', '')}" for event in milestones
        ]
        lines.append("")
    lines += [
        "> 以上脉络由规则化模板整理（按年分组 + 标签分布 + 高重要性事件）。",
        "> 配置 EVT_LLM_BASE_URL / EVT_LLM_API_KEY 后，可生成带阶段划分与因果分析的版本。",
    ]
    return "\n".join(lines)


def default_range(days: int = 90) -> tuple[str, str]:
    end = date.today()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


# ---------------------------------------------------------------- 行业维度


def industry_constituents(industry_id: str) -> pd.DataFrame:
    """取三级行业成分公司（复用 IndustryBoom 的固化快照）。"""
    try:
        from sw_constituents import constituents_for

        return constituents_for([str(industry_id)])
    except Exception:  # noqa: BLE001 - 缺少快照时返回空表
        return pd.DataFrame(columns=["company_id", "company_name"])


def collect_for_companies(
    company_ids: list[str],
    start_date: str,
    end_date: str,
    keyword: str = "",
    skip_noise: bool = True,
    max_companies: int = 5,
) -> tuple[list[dict], list[str]]:
    """逐家采集公告，返回（公告记录, 失败提示）。"""
    records: list[dict] = []
    errors: list[str] = []
    for code in company_ids[:max_companies]:
        try:
            records.extend(
                search_announcements(
                    code,
                    start_date,
                    end_date,
                    skip_noise=skip_noise,
                    keyword=keyword,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单家失败不阻断
            errors.append(f"{code}: {str(exc)[:120]}")
    records.sort(key=lambda item: item.get("date", ""), reverse=True)
    return records, errors
