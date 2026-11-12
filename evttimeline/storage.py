"""事件时间线的本地存储层（每主题一个 JSON 文件）。

设计原则：
- 单机自用，不引入数据库；每个主题一个 JSON，便于备份、diff、迁移；
- 事件按日期排序展示，写入时不做排序，读时统一整理；
- 去重看「来源链接」与「日期 + 标题」，避免同一公告重复入栈。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data" / "topics"

TOPIC_KINDS = {
    "industry": "行业发展周期",
    "company": "公司经营状态",
    "other": "其它主题",
}

EVENT_SOURCES = {
    "manual": "手工录入",
    "cninfo": "巨潮公告",
    "llm": "AI 整理",
    "import": "批量导入",
}

IMPORTANCE_LABELS = {1: "1 一般", 2: "2 关注", 3: "3 重要", 4: "4 关键", 5: "5 里程碑"}


# ---------------------------------------------------------------- 基础读写


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _topic_path(topic_id: str) -> Path:
    return DATA_DIR / f"{topic_id}.json"


def _atomic_write(path: Path, payload: dict) -> None:
    """先写临时文件再替换，避免中断导致 JSON 损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def list_topics() -> list[dict]:
    """主题列表（含事件数量），按更新时间倒序。"""
    if not DATA_DIR.exists():
        return []
    topics: list[dict] = []
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        events = payload.get("events") or []
        topics.append(
            {
                "topic_id": payload.get("topic_id", path.stem),
                "name": payload.get("name", path.stem),
                "kind": payload.get("kind", "other"),
                "description": payload.get("description", ""),
                "event_count": len(events),
                "first_date": min((e.get("date", "") for e in events), default=""),
                "last_date": max((e.get("date", "") for e in events), default=""),
                "updated_at": payload.get("updated_at", ""),
            }
        )
    return sorted(topics, key=lambda item: item.get("updated_at", ""), reverse=True)


def load_topic(topic_id: str) -> dict | None:
    path = _topic_path(topic_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    payload.setdefault("events", [])
    return payload


def save_topic(topic: dict) -> dict:
    topic["updated_at"] = _now()
    topic.setdefault("created_at", topic["updated_at"])
    _atomic_write(_topic_path(topic["topic_id"]), topic)
    return topic


def create_topic(
    name: str,
    kind: str = "other",
    description: str = "",
    industry_id: str = "",
    industry_name: str = "",
    company_ids: list[str] | None = None,
    company_names: list[str] | None = None,
) -> dict:
    topic = {
        "topic_id": uuid.uuid4().hex[:12],
        "name": name.strip(),
        "kind": kind if kind in TOPIC_KINDS else "other",
        "description": description.strip(),
        "industry_id": str(industry_id or "").strip(),
        "industry_name": str(industry_name or "").strip(),
        "company_ids": [str(code).strip() for code in (company_ids or []) if str(code).strip()],
        "company_names": [str(name).strip() for name in (company_names or [])],
        "created_at": _now(),
        "updated_at": _now(),
        "events": [],
    }
    return save_topic(topic)


def delete_topic(topic_id: str) -> bool:
    path = _topic_path(topic_id)
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------- 事件操作


def _event_key(event: dict) -> str:
    url = str(event.get("source_url") or "").strip()
    if url:
        return "url:" + url
    return "text:" + str(event.get("date") or "") + "|" + str(event.get("title") or "").strip()


def event_exists(topic: dict, event: dict) -> bool:
    key = _event_key(event)
    return any(_event_key(item) == key for item in topic.get("events") or [])


def add_events(topic: dict, events: list[dict]) -> tuple[dict, int, int]:
    """批量入栈，返回（主题, 新增数, 跳过重复数）。"""
    added = 0
    skipped = 0
    stored = topic.setdefault("events", [])
    known = {_event_key(item) for item in stored}
    for raw in events:
        event = dict(raw)
        event.setdefault("event_id", uuid.uuid4().hex[:12])
        event.setdefault("tags", [])
        event.setdefault("importance", 3)
        event.setdefault("source", "manual")
        event.setdefault("source_url", "")
        event.setdefault("content", "")
        event["date"] = str(event.get("date") or date.today().isoformat())[:10]
        key = _event_key(event)
        if key in known:
            skipped += 1
            continue
        known.add(key)
        event["created_at"] = event.get("created_at") or _now()
        event["updated_at"] = _now()
        stored.append(event)
        added += 1
    return save_topic(topic), added, skipped


def update_event(topic: dict, event_id: str, changes: dict) -> dict:
    for event in topic.get("events") or []:
        if event.get("event_id") == event_id:
            event.update(changes)
            event["date"] = str(event.get("date") or "")[:10]
            event["importance"] = int(event.get("importance") or 3)
            event["updated_at"] = _now()
            break
    return save_topic(topic)


def delete_event(topic: dict, event_id: str) -> dict:
    topic["events"] = [
        event for event in topic.get("events") or [] if event.get("event_id") != event_id
    ]
    return save_topic(topic)


def sorted_events(topic: dict, descending: bool = False) -> list[dict]:
    events = list(topic.get("events") or [])
    return sorted(events, key=lambda item: str(item.get("date") or ""), reverse=descending)


def filter_events(
    topic: dict,
    keyword: str = "",
    tags: list[str] | None = None,
    start: str = "",
    end: str = "",
    descending: bool = False,
) -> list[dict]:
    """关键词（标题/正文/标签）+ 标签 + 时间范围过滤。"""
    events = sorted_events(topic, descending=descending)
    word = (keyword or "").strip()
    wanted_tags = {tag for tag in (tags or []) if tag}
    result: list[dict] = []
    for event in events:
        event_date = str(event.get("date") or "")
        if start and event_date < start:
            continue
        if end and event_date > end:
            continue
        if wanted_tags and not wanted_tags.intersection(set(event.get("tags") or [])):
            continue
        if word:
            haystack = " ".join(
                [
                    str(event.get("title") or ""),
                    str(event.get("content") or ""),
                    " ".join(str(tag) for tag in event.get("tags") or []),
                ]
            )
            if word.lower() not in haystack.lower():
                continue
        result.append(event)
    return result


def all_tags(topic: dict) -> list[str]:
    tags: set[str] = set()
    for event in topic.get("events") or []:
        tags.update(str(tag) for tag in event.get("tags") or [] if str(tag).strip())
    return sorted(tags)


def topic_stats(topic: dict) -> dict:
    events = topic.get("events") or []
    by_year: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    for event in events:
        year = str(event.get("date") or "")[:4] or "未知"
        by_year[year] = by_year.get(year, 0) + 1
        for tag in event.get("tags") or []:
            by_tag[str(tag)] = by_tag.get(str(tag), 0) + 1
    return {
        "count": len(events),
        "by_year": dict(sorted(by_year.items())),
        "by_tag": dict(sorted(by_tag.items(), key=lambda item: item[1], reverse=True)),
        "sources": sorted({str(event.get("source") or "manual") for event in events}),
    }
