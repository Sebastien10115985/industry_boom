"""把 data/swresearch 下申万官网分类表的归属结果并入 catalog 快照。

用法：
    python tools/merge_swresearch_constituents.py

读取 data/swresearch/*三级归属.csv（由东财细分行业板块归类生成），
对东财未细分（或口径不同）的公司按主营业务手工兜底，
再幂等地并入 catalog/sw_l3_constituents.csv 并重建 sw_constituents.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from freeze_sw_constituents import COLUMNS, write_outputs  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
CSV_PATH = PROJECT / "catalog" / "sw_l3_constituents.csv"
MAPPING_DIR = PROJECT / "data" / "swresearch"

# 东财板块体系未覆盖、按主营业务归入的公司：代码 -> (三级ID, 三级行业)
MANUAL: dict[str, tuple[str, str]] = {
    # ---- 国防军工表 ----
    "000638": ("857451", "军工电子Ⅲ"),  # 万方发展，军工电子与智慧城市
    "002231": ("857451", "军工电子Ⅲ"),  # 奥维通信，军工通信
    "300114": ("857421", "航空装备Ⅲ"),  # 中航成飞，歼击机总装
    "301707": ("857451", "军工电子Ⅲ"),  # 展芯股份，军工芯片
    "600520": ("857451", "军工电子Ⅲ"),  # 三佳科技（文一科技），半导体封测模具
    "600590": ("857451", "军工电子Ⅲ"),  # 泰豪科技，军工信息化电源
    "601989": ("850935", "航海装备Ⅲ"),  # 中国重工，船舶总装
    "688287": ("857421", "航空装备Ⅲ"),  # 观典防务，无人机
    # ---- 机械设备表：轨交设备Ⅲ（东财无对应三级板块）----
    "000008": ("850936", "轨交设备Ⅲ"),
    "000925": ("850936", "轨交设备Ⅲ"),
    "000927": ("850936", "轨交设备Ⅲ"),
    "001324": ("850936", "轨交设备Ⅲ"),
    "002972": ("850936", "轨交设备Ⅲ"),
    "300011": ("850936", "轨交设备Ⅲ"),
    "300351": ("850936", "轨交设备Ⅲ"),
    "300594": ("850936", "轨交设备Ⅲ"),
    "300851": ("850936", "轨交设备Ⅲ"),
    "300923": ("850936", "轨交设备Ⅲ"),
    "300960": ("850936", "轨交设备Ⅲ"),
    "301016": ("850936", "轨交设备Ⅲ"),
    "301048": ("850936", "轨交设备Ⅲ"),
    "600458": ("850936", "轨交设备Ⅲ"),  # 时代新材，轨交减振
    "600495": ("850936", "轨交设备Ⅲ"),  # 晋西车轴
    "601766": ("850936", "轨交设备Ⅲ"),  # 中国中车
    "603111": ("850936", "轨交设备Ⅲ"),  # 康尼机电，轨交门系统
    "603500": ("850936", "轨交设备Ⅲ"),  # 祥和实业，轨交扣件
    "605001": ("850936", "轨交设备Ⅲ"),  # 威奥股份，轨交内饰
    "605298": ("850936", "轨交设备Ⅲ"),  # 必得科技，轨交配件
    "688009": ("850936", "轨交设备Ⅲ"),  # 中国通号
    "688015": ("850936", "轨交设备Ⅲ"),  # 交控科技
    "688033": ("850936", "轨交设备Ⅲ"),  # 天宜上佳，高铁摩擦材料
    "688187": ("850936", "轨交设备Ⅲ"),  # 时代电气，轨交牵引
    "688285": ("850936", "轨交设备Ⅲ"),  # 高铁电气
    "688367": ("850936", "轨交设备Ⅲ"),  # 工大高科
    "688459": ("850936", "轨交设备Ⅲ"),  # 哈铁科技
    "688485": ("850936", "轨交设备Ⅲ"),  # 九州一轨
    "688569": ("850936", "轨交设备Ⅲ"),  # 铁科轨道
    # ---- 机械设备表：农用机械（东财归"专用设备"粗类）----
    "002890": ("850723", "农用机械"),  # 弘宇股份，农机配件
    "300159": ("850723", "农用机械"),  # 新研股份，农牧机械
    "300879": ("850723", "农用机械"),  # 大叶股份，园林机械
    "301260": ("850723", "农用机械"),  # 格力博，锂电园林机械
    "301533": ("850723", "农用机械"),  # 威马农机
    "601038": ("850723", "农用机械"),  # 一拖股份，拖拉机
    "603029": ("850723", "农用机械"),  # 天鹅股份，棉花机械
    "603789": ("850723", "农用机械"),  # 星光农机
    # ---- 机械设备表：零星单家 ----
    "000584": ("850727", "其他专用设备"),  # 工智退
    "600421": ("850727", "其他专用设备"),  # 退市华嵘
    "002552": ("850727", "其他专用设备"),  # 宝鼎科技，船用大型铸锻件
    "300430": ("850727", "其他专用设备"),  # 益通，信息不足兜底
    "000595": ("850716", "其他通用设备"),  # 新能股份（原宝塔实业），泵类机械
    "002058": ("850731", "仪器仪表"),  # 紫竹高科（原威尔泰），自动化仪表
    "688450": ("850731", "仪器仪表"),  # 光格科技，光纤传感监测
    "002150": ("850751", "金属制品"),  # 正泰电源（原通润装备），钣金工具箱
    "301307": ("850751", "金属制品"),  # 美利信，铝合金压铸
    "600528": ("850725", "能源及重型设备"),  # 中铁工业，盾构机
    "603960": ("850784", "其他自动化设备"),  # 克来机电，柔性自动化装备
    "688826": ("850783", "激光设备"),  # 频准激光
}


def to_full(code: str) -> str:
    """纯 6 位代码补交易所后缀，与快照格式一致；重复运行幂等。"""
    code = str(code).split(".")[0].zfill(6)
    if code.startswith(("60", "68", "90")):
        return f"{code}.SH"
    if code.startswith(("43", "45", "83", "87", "88", "92")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def load_frame(csv_name: str) -> pd.DataFrame:
    path = MAPPING_DIR / csv_name
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.read_csv(path, dtype={"industry_l3_id": str, "company_id": str}, encoding="utf-8-sig")
    return frame[COLUMNS]


def load_company_names() -> dict[str, str]:
    """从申万官网原始分类表读取 代码 -> 公司名。"""
    names: dict[str, str] = {}
    for xlsx in MAPPING_DIR.glob("*分类表.xlsx"):
        frame = pd.read_excel(xlsx)
        codes = frame["股票代码"].astype(str).str.split(".").str[0].str.zfill(6)
        names.update(dict(zip(codes, frame["股票名称"].astype(str))))
    return names


def main() -> None:
    parts = [load_frame(name) for name in
             ("有色金属三级归属.csv", "国防军工三级归属.csv", "机械设备三级归属.csv")]
    new = pd.concat(parts, ignore_index=True)
    new["company_id"] = new["company_id"].map(to_full)
    new["company_name"] = new["company_name"].astype(str)

    # 手工兜底：覆盖同名公司的旧归类，并补充未归属公司
    official_names = load_company_names()
    raw = new.set_index("company_id", drop=False)
    for code, (l3_id, l3_name) in MANUAL.items():
        full_code = to_full(code)
        raw = raw.drop(index=full_code, errors="ignore")
        raw.loc[full_code] = {
            "industry_l3_id": l3_id,
            "industry_l3_name": l3_name,
            "company_id": full_code,
            "company_name": official_names.get(code, ""),
            "include_date": "2026-11-06",
        }
    new = raw.reset_index(drop=True)
    new = new.drop_duplicates(["industry_l3_id", "company_id"]).sort_values(
        ["industry_l3_id", "company_id"]
    ).reset_index(drop=True)
    missing = new[new["company_name"].eq("")]
    if not missing.empty:
        raise SystemExit(f"缺少公司名，请检查: {missing['company_id'].tolist()}")

    old = pd.read_csv(CSV_PATH, dtype={"industry_l3_id": str, "company_id": str}, encoding="utf-8-sig")
    covered = set(new["industry_l3_id"])
    kept = old[~old["industry_l3_id"].isin(covered)]
    combined = (
        pd.concat([kept[COLUMNS], new[COLUMNS]], ignore_index=True)
        .drop_duplicates(["industry_l3_id", "company_id"])
        .sort_values(["industry_l3_id", "company_id"])
        .reset_index(drop=True)
    )
    write_outputs(combined, "2026-11-06", int(combined["industry_l3_id"].nunique()))
    print(f"并入 {len(new)} 条（覆盖 {new['industry_l3_id'].nunique()} 个三级行业）；"
          f"catalog 现有 {len(combined)} 条 / {combined['industry_l3_id'].nunique()} 个三级行业")


if __name__ == "__main__":
    main()
