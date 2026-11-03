"""国家统计局行业大类与申万行业的对应关系。

统计局的一个行业大类通常覆盖若干个申万三级行业，例如
“计算机、通信和其他电子设备制造业”覆盖半导体、消费电子、光学光电子、
元件、通信设备等多个申万三级行业。本模块只做行业层面的客观对应，
不做任何主观挑选。

映射优先写申万**二级行业**名称，程序自动展开为二级行业下的全部三级行业，
这样既完整也避免单个三级行业名称近似导致的误匹配；少数在申万中只有三级
口径的行业（如金属制品、仪器仪表、水务及水治理）直接写三级行业名称。
"""

from __future__ import annotations

import pandas as pd

from industry_match import builtin_industry_frame, match_industry_name


# 统计行业大类 -> 申万二级行业名称（少数为三级行业名称）
STAT_TO_SW: dict[str, tuple[str, ...]] = {
    "煤炭开采和洗选业": ("煤炭开采", "焦炭Ⅱ"),
    "石油和天然气开采业": ("油气开采Ⅱ",),
    "黑色金属矿采选业": ("冶钢原料",),
    "有色金属矿采选业": ("工业金属", "贵金属", "小金属", "能源金属"),
    "非金属矿采选业": ("非金属材料Ⅱ",),
    "开采专业及辅助性活动": ("油服工程",),
    "农副食品加工业": ("农产品加工", "食品加工"),
    "食品制造业": ("食品加工", "调味发酵品Ⅱ", "休闲食品"),
    "酒、饮料和精制茶制造业": ("白酒Ⅱ", "非白酒", "饮料乳品"),
    "纺织业": ("纺织制造",),
    "纺织服装、服饰业": ("服装家纺",),
    "皮革、毛皮、羽毛及其制品和制鞋业": ("服装家纺",),
    "木材加工和木、竹、藤、棕、草制品业": ("家居用品",),
    "家具制造业": ("家居用品",),
    "造纸和纸制品业": ("造纸", "包装印刷"),
    "印刷和记录媒介复制业": ("包装印刷",),
    "文教、工美、体育和娱乐用品制造业": ("文娱用品",),
    "石油、煤炭及其他燃料加工业": ("炼化及贸易", "化学原料"),
    "化学原料和化学制品制造业": ("化学原料", "化学制品", "塑料", "橡胶", "农化制品"),
    "医药制造业": ("化学制药", "中药Ⅱ", "生物制品"),
    "化学纤维制造业": ("化学纤维",),
    "橡胶和塑料制品业": ("橡胶", "塑料"),
    "非金属矿物制品业": ("水泥", "玻璃玻纤", "装修建材"),
    "黑色金属冶炼和压延加工业": ("普钢",),
    "有色金属冶炼和压延加工业": ("工业金属", "贵金属", "小金属", "能源金属", "金属新材料"),
    "金属制品业": ("金属制品",),
    "通用设备制造业": ("通用设备",),
    "专用设备制造业": ("专用设备", "工程机械", "医疗器械"),
    "汽车制造业": ("乘用车", "商用车", "汽车零部件", "汽车服务", "摩托车及其他"),
    "铁路、船舶、航空航天和其他运输设备制造业": (
        "轨交设备Ⅱ",
        "航海装备Ⅱ",
        "航空装备Ⅱ",
        "航天装备Ⅱ",
        "地面兵装Ⅱ",
    ),
    "电气机械和器材制造业": (
        "电机Ⅱ",
        "电网设备",
        "电池",
        "光伏设备",
        "风电设备",
        "其他电源设备Ⅱ",
        "照明设备Ⅱ",
    ),
    "计算机、通信和其他电子设备制造业": (
        "半导体",
        "元件",
        "光学光电子",
        "其他电子Ⅱ",
        "消费电子",
        "电子化学品Ⅱ",
        "通信设备",
        "计算机设备",
    ),
    "仪器仪表制造业": ("自动化设备", "仪器仪表"),
    "电力、热力生产和供应业": ("电力",),
    "燃气生产和供应业": ("燃气Ⅱ",),
    "水的生产和供应业": ("水务及水治理",),
}


def stat_industry_names() -> list[str]:
    return list(STAT_TO_SW)


def mapped_industry_names(stat_industry: str) -> list[str]:
    """返回该统计行业对应的申万行业名称（二级或三级）。"""
    return list(STAT_TO_SW.get(str(stat_industry).strip(), ()))


def _resolve_ids(names: list[str]) -> tuple[list[str], list[str]]:
    """把申万行业名称解析为三级行业 ID；返回 (已解析 ID, 未解析名称)。"""
    frame = builtin_industry_frame()
    ids: list[str] = []
    unresolved: list[str] = []
    for name in names:
        key = str(name).replace(" ", "")
        by_level2 = frame[frame["industry_l2_name"].map(lambda value: str(value).replace(" ", "")) == key]
        if not by_level2.empty:
            ids.extend(by_level2["industry_l3_id"].tolist())
            continue
        result = match_industry_name(name, minimum_score=0.85)
        if result["industry_l3_id"] and result["match_score"] >= 0.85:
            ids.append(str(result["industry_l3_id"]))
        else:
            unresolved.append(str(name))
    return ids, unresolved


def mapped_industry_frame(stat_industry: str) -> pd.DataFrame:
    """返回该统计行业对应的申万三级行业完整记录。"""
    frame = builtin_industry_frame()
    ids, _ = _resolve_ids(mapped_industry_names(stat_industry))
    if not ids:
        return frame.iloc[0:0].assign(stat_industry=pd.Series(dtype=str)).copy()
    ordered = {industry_id: position for position, industry_id in enumerate(dict.fromkeys(ids))}
    selected = frame[frame["industry_l3_id"].isin(ordered)].copy()
    selected["order"] = selected["industry_l3_id"].map(ordered)
    selected["stat_industry"] = str(stat_industry)
    return (
        selected.sort_values("order")
        .drop(columns=["order", "match_key"])
        .reset_index(drop=True)
    )


def mapped_industry_ids(stat_industry: str) -> list[str]:
    ids, _ = _resolve_ids(mapped_industry_names(stat_industry))
    return list(dict.fromkeys(ids))


def mapping_report() -> pd.DataFrame:
    """列出每个统计行业落地到多少个三级行业，便于核对映射是否合理。"""
    rows = []
    for stat_industry, names in STAT_TO_SW.items():
        ids, unresolved = _resolve_ids(list(names))
        rows.append(
            {
                "stat_industry": stat_industry,
                "mapped_sw_names": "、".join(names),
                "industry_count": len(set(ids)),
                "unresolved": "、".join(unresolved),
            }
        )
    return pd.DataFrame(rows)
