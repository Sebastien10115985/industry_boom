from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from industry_match import (
    SOURCE_URL as PUBLIC_SOURCE_URL,
    SNAPSHOT_DATE as PUBLIC_SNAPSHOT_DATE,
    builtin_industry_frame,
    map_industry_names,
)


PROJECT_DIR = Path(__file__).resolve().parent
CATALOG_DIR = PROJECT_DIR / "catalog"
CATALOG_PATH = CATALOG_DIR / "wind_l3_catalog.csv"
CONSTITUENTS_PATH = CATALOG_DIR / "wind_l3_constituents.csv"
METADATA_PATH = CATALOG_DIR / "catalog_metadata.json"

CATALOG_COLUMNS = [
    "industry_l3_id",
    "industry_l3_name",
    "model_type",
    "effective_date",
    "source",
]
CONSTITUENT_COLUMNS = [
    "industry_l3_id",
    "industry_l3_name",
    "model_type",
    "company_id",
    "company_name",
    "market_cap",
    "is_seed",
    "effective_date",
]

EXPORT_ALIASES = {
    "三级行业id": "industry_l3_id",
    "万得三级行业id": "industry_l3_id",
    "行业id": "industry_l3_id",
    "三级行业代码": "industry_l3_id",
    "三级行业名称": "industry_l3_name",
    "万得三级行业名称": "industry_l3_name",
    "证券代码": "company_id",
    "wind代码": "company_id",
    "万得代码": "company_id",
    "证券简称": "company_name",
    "公司名称": "company_name",
    "总市值": "market_cap",
    "市值": "market_cap",
    "模型类型": "model_type",
}


def suggest_model_type(industry_name: object) -> str:
    name = str(industry_name or "")
    # 创新药相关三级行业常跨盈亏，利润增长率改用对称增长率
    if any(
        word in name
        for word in ("制药生命科学工具", "生物科技", "化学制剂", "生物制品", "医疗研发外包")
    ):
        return "innovative_drug"
    if "银行" in name:
        return "bank"
    if "证券" in name or "券商" in name:
        return "securities"
    if "保险" in name:
        return "insurance"
    if any(word in name for word in ("软件", "半导体", "芯片", "集成电路", "生物科技", "互联网", "信息技术", "电子设备")):
        return "growth"
    if any(word in name for word in ("食品", "饮料", "家庭用品", "个人用品", "酒店", "休闲", "零售")):
        return "quality"
    if any(word in name for word in ("金属", "矿业", "化工", "煤炭", "石油", "天然气", "造纸")):
        return "cyclical"
    if any(word in name for word in ("公用事业", "运输", "建筑", "工程", "港口", "机场", "铁路")):
        return "scale"
    return "general"


def resolve_model_type(
    industry_l3_name: object,
    industry_l2_name: object = None,
    industry_l1_name: object = None,
) -> str:
    """三级行业自身能识别时直接用它，否则逐级上溯到二级、一级行业名称。"""
    direct = suggest_model_type(industry_l3_name)
    if direct != "general":
        return direct
    for name in (industry_l2_name, industry_l1_name):
        if not name or pd.isna(name):
            continue
        inferred = suggest_model_type(name)
        if inferred != "general":
            return inferred
    return "general"


def builtin_catalog() -> pd.DataFrame:
    """内置公开三级行业目录；没有万得授权时程序默认使用它。"""
    frame = builtin_industry_frame()
    catalog = pd.DataFrame(
        {
            "industry_l3_id": frame["industry_l3_id"],
            "industry_l3_name": frame["industry_l3_name"],
            "model_type": frame["industry_l3_name"].map(suggest_model_type),
        }
    )
    catalog["effective_date"] = PUBLIC_SNAPSHOT_DATE
    catalog["source"] = PUBLIC_SOURCE_URL
    return catalog[CATALOG_COLUMNS]


def load_catalog(path: str | Path = CATALOG_PATH) -> pd.DataFrame:
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return builtin_catalog()
    frame = pd.read_csv(source, dtype={"industry_l3_id": str}, encoding="utf-8-sig")
    for column in CATALOG_COLUMNS:
        if column not in frame:
            frame[column] = pd.NA
    frozen = frame[CATALOG_COLUMNS]
    if frozen.empty or frozen["industry_l3_id"].isna().all():
        return builtin_catalog()
    return frozen


def load_constituents(path: str | Path = CONSTITUENTS_PATH) -> pd.DataFrame:
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame(columns=CONSTITUENT_COLUMNS)
    frame = pd.read_csv(
        source,
        dtype={"industry_l3_id": str, "company_id": str},
        encoding="utf-8-sig",
    )
    for column in CONSTITUENT_COLUMNS:
        if column not in frame:
            frame[column] = pd.NA
    frame["is_seed"] = frame["is_seed"].astype(str).str.lower().isin({"true", "1", "yes"})
    frame["market_cap"] = pd.to_numeric(frame["market_cap"], errors="coerce")
    return frame[CONSTITUENT_COLUMNS]


def _rename_export_columns(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.copy()
    lower_to_source = {str(column).strip().lower(): column for column in frame.columns}
    rename_map: dict[object, str] = {}
    for alias, canonical in EXPORT_ALIASES.items():
        source = lower_to_source.get(alias.lower())
        if source is not None and canonical not in frame.columns:
            rename_map[source] = canonical
    return frame.rename(columns=rename_map)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def freeze_catalog(
    official_export: pd.DataFrame,
    effective_date: str | date,
    source: str = "Wind official export",
    seed_count: int = 5,
    catalog_path: str | Path = CATALOG_PATH,
    constituents_path: str | Path = CONSTITUENTS_PATH,
    metadata_path: str | Path = METADATA_PATH,
    model_resolver: Callable[[object], str] = suggest_model_type,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """校验官方导出并固化行业、成分股及市值前 N 的客观种子公司。"""
    if seed_count < 0:
        raise ValueError("seed_count 不能小于 0")
    frame = _rename_export_columns(official_export)
    required = {"industry_l3_name", "company_id", "company_name"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"成分股表缺少字段：{', '.join(sorted(missing))}")
    if "industry_l3_id" not in frame:
        mapped = map_industry_names(frame["industry_l3_name"])
        frame["industry_l3_id"] = mapped["industry_l3_id"].to_numpy()
        matched_names = mapped["industry_l3_name"].to_numpy()
        frame["industry_l3_name"] = frame["industry_l3_name"].where(matched_names == "", matched_names)
    frame = frame[frame["industry_l3_id"].astype(str).ne("")].copy()
    if frame.empty:
        raise ValueError("成分股表的行业名称无法匹配到内置三级行业；请检查行业名称列")

    for column in required:
        frame[column] = frame[column].astype(str).str.strip()
    frame = frame[
        frame["industry_l3_id"].ne("")
        & frame["industry_l3_name"].ne("")
        & frame["company_id"].ne("")
    ].copy()
    if frame.empty:
        raise ValueError("万得官方导出没有有效的三级行业成分股")

    conflicts = frame.groupby("industry_l3_id")["industry_l3_name"].nunique()
    if conflicts.gt(1).any():
        ids = ", ".join(conflicts[conflicts.gt(1)].index.astype(str))
        raise ValueError(f"同一三级行业 ID 对应多个名称：{ids}")

    if "model_type" not in frame:
        frame["model_type"] = frame["industry_l3_name"].map(model_resolver)
    else:
        inferred = frame["industry_l3_name"].map(model_resolver)
        frame["model_type"] = frame["model_type"].replace("", pd.NA).fillna(inferred)
    frame["market_cap"] = pd.to_numeric(frame.get("market_cap"), errors="coerce")
    effective = pd.Timestamp(effective_date).strftime("%Y-%m-%d")
    frame["effective_date"] = effective

    if seed_count and frame["market_cap"].notna().sum() == 0:
        raise ValueError("生成知名公司种子库需要官方导出包含市值字段")
    frame["is_seed"] = False
    if seed_count:
        ranked_indices = (
            frame.dropna(subset=["market_cap"])
            .sort_values(["industry_l3_id", "market_cap", "company_id"], ascending=[True, False, True])
            .groupby("industry_l3_id", sort=False)
            .head(seed_count)
            .index
        )
        frame.loc[ranked_indices, "is_seed"] = True

    catalog = (
        frame[["industry_l3_id", "industry_l3_name", "model_type"]]
        .drop_duplicates("industry_l3_id")
        .sort_values("industry_l3_id")
    )
    catalog["effective_date"] = effective
    catalog["source"] = source
    constituents = frame[
        [
            "industry_l3_id",
            "industry_l3_name",
            "model_type",
            "company_id",
            "company_name",
            "market_cap",
            "is_seed",
            "effective_date",
        ]
    ].drop_duplicates(["industry_l3_id", "company_id"], keep="last")
    constituents = constituents.sort_values(["industry_l3_id", "is_seed", "market_cap"], ascending=[True, False, False])

    catalog_target = Path(catalog_path)
    constituents_target = Path(constituents_path)
    metadata_target = Path(metadata_path)
    for target in (catalog_target, constituents_target, metadata_target):
        target.parent.mkdir(parents=True, exist_ok=True)
    catalog.to_csv(catalog_target, index=False, encoding="utf-8-sig")
    constituents.to_csv(constituents_target, index=False, encoding="utf-8-sig")
    metadata = {
        "source": source,
        "effective_date": effective,
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "industry_count": int(len(catalog)),
        "company_count": int(len(constituents)),
        "seed_count": int(constituents["is_seed"].sum()),
        "catalog_sha256": _sha256(catalog_target),
        "constituents_sha256": _sha256(constituents_target),
    }
    metadata_target.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return catalog.reset_index(drop=True), constituents.reset_index(drop=True)


def attach_catalog(data: pd.DataFrame) -> pd.DataFrame:
    """补齐行业名称与模型；缺失或无法识别的行业 ID 用名称模糊匹配兜底。"""
    catalog = load_catalog()
    frame = data.copy()
    if catalog.empty:
        return frame
    lookup = catalog.set_index("industry_l3_id")
    ids = frame["industry_l3_id"].astype(str) if "industry_l3_id" in frame else pd.Series("", index=frame.index)
    names = (
        frame["industry_l3_name"].fillna("").astype(str)
        if "industry_l3_name" in frame
        else pd.Series("", index=frame.index)
    )

    unmatched = ~ids.isin(lookup.index)
    if unmatched.any() and names.ne("").any():
        mapped = map_industry_names(names[unmatched])
        ids = ids.mask(unmatched, mapped["industry_l3_id"])
        names = names.mask(unmatched & mapped["industry_l3_name"].ne(""), mapped["industry_l3_name"])
    frame["industry_l3_id"] = ids

    catalog_names = ids.map(lookup["industry_l3_name"])
    frame["industry_l3_name"] = names.where(names.ne(""), catalog_names)
    if "model_type" not in frame:
        frame["model_type"] = ids.map(lookup["model_type"]).fillna(frame["industry_l3_name"].map(suggest_model_type))
    else:
        inferred = ids.map(lookup["model_type"]).fillna(frame["industry_l3_name"].map(suggest_model_type))
        frame["model_type"] = frame["model_type"].replace("", pd.NA).fillna(inferred)
    return frame
