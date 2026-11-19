"""宏观风险投票与评论（风险预警页）的服务器全局存储。

存储为 JSON 文件（同一服务器上的所有 Streamlit 会话共享）：
- macro_risk_votes.json：四项宏观风险分值（默认 80/80/80/50），用户认同 +0.1、不认同 -0.1，
  每个会话对每项限投一票，防刷票。
- macro_comments.json：评论双栈结构——
  buffer（缓冲栈）：所有提交的评论，仅作者会话与管理员可见；
  global（全局栈）：管理员精选后的评论，对所有人公开展示。
写盘采用临时文件原子替换，避免并发写入损坏。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data" / "prosperity"
DEFAULT_VOTES_PATH = DATA_DIR / "macro_risk_votes.json"
DEFAULT_COMMENTS_PATH = DATA_DIR / "macro_comments.json"
DEFAULT_REGIME_PATH = DATA_DIR / "market_regime.json"

#: 宏观风险项与默认分值
DEFAULT_RISK_VALUES: dict[str, float] = {
    "战争": 80.0,
    "美元紧缩": 80.0,
    "中国通缩": 80.0,
    "其它": 50.0,
}
VOTE_STEP = 0.1
VOTE_MIN, VOTE_MAX = 0.0, 100.0


def new_session_id() -> str:
    return uuid.uuid4().hex


def _atomic_write_json(payload: dict, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=1)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _read_json(path: str | Path, default: dict) -> dict:
    source = Path(path)
    if not source.exists():
        return default
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


# ---- 宏观风险投票 ----


def load_votes(path: str | Path = DEFAULT_VOTES_PATH) -> dict[str, float]:
    data = _read_json(path, {"values": DEFAULT_RISK_VALUES, "votes": []})
    values = dict(DEFAULT_RISK_VALUES)
    for key, value in (data.get("values") or {}).items():
        if key in values and isinstance(value, (int, float)):
            values[key] = float(min(VOTE_MAX, max(VOTE_MIN, value)))
    return values


def load_voters(path: str | Path = DEFAULT_VOTES_PATH) -> dict[str, set[str]]:
    """返回 {风险项: 已投票会话集合}。"""
    data = _read_json(path, {"values": DEFAULT_RISK_VALUES, "votes": []})
    voters: dict[str, set[str]] = {item: set() for item in DEFAULT_RISK_VALUES}
    for vote in data.get("votes") or []:
        item = vote.get("item")
        session = vote.get("session_id")
        if item in voters and session:
            voters[item].add(session)
    return voters


def vote_risk(
    item: str,
    agree: bool,
    session_id: str,
    path: str | Path = DEFAULT_VOTES_PATH,
) -> dict[str, float]:
    """对某风险项投票：认同 +0.1，不认同 -0.1（不限次数，返回最新分值表）。"""
    if item not in DEFAULT_RISK_VALUES:
        return load_votes(path)
    data = _read_json(path, {"values": DEFAULT_RISK_VALUES, "votes": []})
    values = {key: float(value) for key, value in (data.get("values") or DEFAULT_RISK_VALUES).items()}
    votes: list[dict[str, object]] = list(data.get("votes") or [])
    delta = VOTE_STEP if agree else -VOTE_STEP
    values[item] = min(VOTE_MAX, max(VOTE_MIN, values.get(item, DEFAULT_RISK_VALUES[item]) + delta))
    votes.append(
        {
            "item": item,
            "session_id": session_id,
            "agree": agree,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _atomic_write_json({"values": values, "votes": votes}, path)
    return {key: values.get(key, DEFAULT_RISK_VALUES[key]) for key in DEFAULT_RISK_VALUES}


# ---- 评论（缓冲栈 / 全局栈） ----


def load_comments(path: str | Path = DEFAULT_COMMENTS_PATH) -> dict[str, list[dict[str, object]]]:
    data = _read_json(path, {"buffer": [], "global": []})
    return {"buffer": list(data.get("buffer") or []), "global": list(data.get("global") or [])}


def add_comment(content: str, session_id: str, ip: str, path: str | Path = DEFAULT_COMMENTS_PATH) -> dict[str, object]:
    text = content.strip()
    if not text:
        raise ValueError("评论内容不能为空")
    if len(text) > 500:
        text = text[:500]
    data = load_comments(path)
    record = {
        "id": uuid.uuid4().hex,
        "session_id": session_id,
        "ip": ip,
        "content": text,
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    data["buffer"].insert(0, record)
    _atomic_write_json(data, path)
    return record


def delete_comment(
    comment_id: str,
    session_id: str,
    path: str | Path = DEFAULT_COMMENTS_PATH,
    is_admin: bool = False,
) -> bool:
    """删除评论：普通用户仅能删除缓冲栈中自己的评论；管理员可删任意栈任意评论。"""
    data = load_comments(path)
    changed = False
    for scope in ("buffer", "global"):
        kept = []
        for record in data[scope]:
            if record.get("id") == comment_id:
                if is_admin or (scope == "buffer" and record.get("session_id") == session_id):
                    changed = True
                    continue
            kept.append(record)
        data[scope] = kept
    if changed:
        _atomic_write_json(data, path)
    return changed


def feature_comment(comment_id: str, path: str | Path = DEFAULT_COMMENTS_PATH, is_admin: bool = False) -> bool:
    """管理员把缓冲栈评论精选入全局栈（公开展示）。"""
    if not is_admin:
        return False
    data = load_comments(path)
    moved = None
    for record in data["buffer"]:
        if record.get("id") == comment_id:
            moved = record
            break
    if moved is None:
        return False
    data["buffer"] = [r for r in data["buffer"] if r.get("id") != comment_id]
    data["global"].insert(0, moved)
    _atomic_write_json(data, path)
    return True


# ---- 市场状态游标（IF趋势上方卡尺，管理员可设） ----

REGIME_MIN, REGIME_MAX = -100, 100


def load_regime(path: str | Path = DEFAULT_REGIME_PATH) -> float:
    """当前市场状态游标值（-100 深熊 ~ 100 深牛），默认 -80。"""
    data = _read_json(path, {"value": -80.0})
    try:
        return float(min(REGIME_MAX, max(REGIME_MIN, data.get("value", -80.0))))
    except (TypeError, ValueError):
        return -80.0


def save_regime(value: float, path: str | Path = DEFAULT_REGIME_PATH, is_admin: bool = False) -> bool:
    """管理员设置游标值；非管理员拒绝。"""
    if not is_admin:
        return False
    clamped = min(REGIME_MAX, max(REGIME_MIN, float(value)))
    _atomic_write_json({"value": clamped, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}, path)
    return True
