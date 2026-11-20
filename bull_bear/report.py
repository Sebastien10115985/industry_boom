"""结果表、历史序列导出与最新结果持久化。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .config import RegimeConfig
from .data_loader import PriceBundle
from .scoring import STATE_LABELS

LATEST_COLUMNS = ["指数", "f1(趋势)", "f2(动量)", "f3(长周期)", "分值 S_i", "状态", "是否站上MA60"]


def latest_table(scores: pd.DataFrame, config: RegimeConfig) -> pd.DataFrame:
    """取最新交易日的五个指数明细表。"""
    latest_date = scores["date"].max()
    rows: list[dict[str, Any]] = []
    for item in config.indices:
        code = str(item["code"])
        subset = scores[(scores["date"] == latest_date) & (scores["code"] == code)]
        if subset.empty:
            continue
        record = subset.iloc[0]
        rows.append(
            {
                "指数": config.name_of(code),
                "f1(趋势)": round(float(record["f1"]) * 100, 2),
                "f2(动量)": round(float(record["f2"]) * 100, 2),
                "f3(长周期)": round(float(record["f3"]) * 100, 2),
                "分值 S_i": round(float(record["score"]), 1),
                "状态": STATE_LABELS.get(str(record["state"]), str(record["state"])),
                "是否站上MA60": "是" if bool(record["above_ma60"]) else "否",
            }
        )
    return pd.DataFrame(rows, columns=LATEST_COLUMNS)


def save_history(scores: pd.DataFrame, composite: pd.DataFrame, config: RegimeConfig) -> Path:
    """导出 date + 五个 S_i + S 的历史序列 CSV。"""
    wide = scores.pivot(index="date", columns="code", values="score")
    history = pd.DataFrame({"date": wide.index})
    for item in config.indices:
        code = str(item["code"])
        history[f"S_{config.name_of(code)}"] = wide[code].to_numpy() if code in wide.columns else None
    history["S_base"] = composite["s_base"].to_numpy()
    history["breadth"] = composite["breadth"].to_numpy()
    history["S"] = composite["score"].to_numpy()
    history["state"] = composite["state"].map(STATE_LABELS).fillna(composite["state"]).to_numpy()
    target = config.history_path
    target.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(target, index=False, encoding="utf-8-sig")
    return target


def build_report(
    bundle: PriceBundle,
    factors: pd.DataFrame,
    scores: pd.DataFrame,
    groups: pd.DataFrame,
    composite: pd.DataFrame,
    config: RegimeConfig,
) -> dict[str, Any]:
    """组装完整结果，落盘历史序列与最新 JSON。"""
    history_path = save_history(scores, composite, config)
    latest_date = composite["date"].max()
    last = composite.iloc[-1]
    group_latest: dict[str, dict[str, Any]] = {}
    for record in groups[groups["date"] == latest_date].to_dict("records"):
        group_latest[str(record["group"])] = {
            "label": str(record["label"]),
            "score": round(float(record["score"]), 1),
            "state": str(record["state"]),
            "state_label": STATE_LABELS.get(str(record["state"]), str(record["state"])),
        }
    latest = {
        "date": pd.Timestamp(latest_date).date().isoformat(),
        "s_base": round(float(last["s_base"]), 2),
        "breadth": round(float(last["breadth"]), 4),
        "z_b": round(float(last["z_b"]), 4),
        "score": round(float(last["score"]), 2),
        "state": str(last["state"]),
        "state_label": STATE_LABELS.get(str(last["state"]), str(last["state"])),
        "groups": group_latest,
        "indices": latest_table(scores, config).to_dict("records"),
        "warnings": list(bundle.warnings),
        "fetched": list(bundle.fetched),
    }
    target = config.latest_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "bundle": bundle,
        "factors": factors,
        "scores": scores,
        "groups": groups,
        "composite": composite,
        "latest": latest,
        "table": latest_table(scores, config),
        "history_path": history_path,
    }
