"""三级行业财报管线。

以申万三级行业为单位：

1. 取该行业全部成份公司（公开快照，无需授权）；
2. 逐个到巨潮资讯检索截止日之前近 N 天（默认 365 天，即近一年）的正式定期报告；
3. 下载公告 PDF 并抽取财报信息：营收、净利润、营收增长率、利润增长率、毛利率；
4. 总市值由公开行情接口补齐（公告接口不提供市值字段）；
5. 落盘到 `data/industries/<三级行业ID>.csv`，可直接用 Excel 打开核对；
6. 复用 `ranking.py` 的现有算法，在该三级行业内部做横截面百分位排名。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd

from cninfo_provider import CninfoClient, ReportRecord
from disclosure_pipeline import rank_online_financials
from financial_extract import MAX_GROSS_MARGIN, extract_financial_metrics
from industry_catalog import resolve_model_type
from industry_match import builtin_industry_frame, match_industry_name
from market_cap_provider import attach_market_cap, sync_market_caps
from sw_constituents import constituents_for, industry_coverage

PROJECT_DIR = Path(__file__).resolve().parent
INDUSTRY_DATA_DIR = PROJECT_DIR / "data" / "industries"
DEFAULT_LOOKBACK_DAYS = 365
DEFAULT_TOP_N = 20
DEFAULT_SEARCH_COOLDOWN_DAYS = 7
DEFAULT_PERIOD_COVERAGE_RATIO = 0.5
DEFAULT_PERIOD_MIN_COMPANIES = 5

# 预先同步、常驻展示的「景气」三级行业，按主题分组，组名即页面上的分组标题，
# 组内顺序即展示顺序
FOCUS_INDUSTRY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "AI硬件",
        (
            "850814",  # 数字芯片设计
            "850817",  # 集成电路封测
            "850822",  # 印制电路板
            "851024",  # 通信网络设备及器件
            "850818",  # 半导体设备
            "850816",  # 集成电路制造
            "850813",  # 半导体材料
            "851025",  # 通信线缆及配套
            "850861",  # 电子化学品Ⅲ
        ),
    ),
    (
        "创新药",
        (
            # 对应万得「化学制剂Ⅲ」「生物科技Ⅲ」「制药生命科学工具和服务Ⅲ」，
            # 排名用 innovative_drug 模型（利润增长率取对称增长率）
            "851512",  # 化学制剂
            "851524",  # 其他生物制品
            "851563",  # 医疗研发外包
        ),
    ),
)

# 扁平化的展示顺序，供只关心顺序的场景使用
AI_FOCUS_INDUSTRIES: tuple[str, ...] = tuple(
    industry_id for _, industry_ids in FOCUS_INDUSTRY_GROUPS for industry_id in industry_ids
)

# 「行业详情」页按统计局制造业大类拆成子 tab：
# - title：tab 标题
# - l1_names：该 tab 认领的申万一级行业
# - claimed_l3_ids：仅认领这些三级行业（用于把一个一级行业拆到两个 tab），空表示整一级归属本 tab
# - featured：重点子行业（已预拉取或主推），排在 tab 内最前
# - compact_focus：重点子行业只显示一行文字说明，明细并入下方选择器（tab 内控件少、页面短）
# 按列表顺序「先到先得」认领，未被认领的三级行业归入「其它」。
DETAIL_SECTOR_TABS: tuple[dict[str, object], ...] = (
    {
        "title": "电子通信",
        "l1_names": ("电子", "通信", "计算机"),
        "claimed_l3_ids": (),
        "featured": FOCUS_INDUSTRY_GROUPS[0][1],  # AI硬件 9 个
        "compact_focus": True,
    },
    {
        "title": "创新药",
        "l1_names": ("医药生物",),
        "claimed_l3_ids": (),
        "featured": FOCUS_INDUSTRY_GROUPS[1][1],  # 化学制剂、其他生物制品、医疗研发外包
        "compact_focus": True,
    },
    {
        "title": "有色金属",
        "l1_names": ("有色金属",),
        "claimed_l3_ids": (),
        "featured": ("850543", "850531", "850552"),  # 锂、黄金、铜
    },
    {
        "title": "国防军工",
        "l1_names": ("国防军工", "机械设备"),
        "claimed_l3_ids": (
            "857421",  # 航空装备Ⅲ
            "857411",  # 航天装备Ⅲ
            "850935",  # 航海装备Ⅲ
            "857431",  # 地面兵装Ⅲ
            "857451",  # 军工电子Ⅲ
            "850936",  # 轨交设备Ⅲ（属机械设备一级）
        ),
        "featured": (
            "857451",  # 军工电子Ⅲ
            "857421",  # 航空装备Ⅲ
            "857411",  # 航天装备Ⅲ
            "850935",  # 航海装备Ⅲ
            "857431",  # 地面兵装Ⅲ
            "850936",  # 轨交设备Ⅲ
        ),
    },
    {
        "title": "化工",
        "l1_names": ("石油石化", "基础化工"),
        "claimed_l3_ids": (),
        "featured": (),
    },
    {
        "title": "机械设备",
        "l1_names": ("机械设备",),
        "claimed_l3_ids": (),
        "featured": ("850781", "850771", "850782"),  # 机器人、工程机械整机、工控设备
    },
)


def focus_industry_groups() -> list[dict[str, object]]:
    """精选行业按主题分组；目录里不存在的行业会被跳过，空组不返回。"""
    groups: list[dict[str, object]] = []
    for group_name, industry_ids in FOCUS_INDUSTRY_GROUPS:
        members: list[dict[str, str]] = []
        for industry_id in industry_ids:
            info = resolve_industry(industry_id)
            if info is None:
                continue
            members.append(
                {
                    "industry_l3_id": industry_id,
                    "industry_l3_name": str(info["industry_l3_name"]),
                }
            )
        if members:
            groups.append({"group_name": group_name, "industries": members})
    return groups


def focus_industries() -> list[dict[str, str]]:
    """精选行业的 ID 与名称，按展示顺序；目录里不存在的会被跳过。"""
    return [
        member
        for group in focus_industry_groups()
        for member in group["industries"]  # type: ignore[union-attr]
    ]
SEARCH_LOG_FILE = "_search_log.csv"
SEARCH_LOG_COLUMNS = ["industry_l3_id", "company_id", "as_of_date", "searched_at", "found"]

ProgressCallback = Callable[[int, int, str], None]

STORED_COLUMNS = [
    "company_id",
    "company_name",
    "industry_l3_id",
    "industry_l3_name",
    "model_type",
    "report_period",
    "announcement_date",
    "market_cap",
    "revenue",
    "net_profit",
    "revenue_yoy",
    "profit_yoy",
    "gross_margin",
    "source_title",
    "source_url",
    "announcement_id",
    "extraction_confidence",
    "needs_review",
]


def resolve_industry(query: object) -> dict[str, object] | None:
    """按行业 ID 或名称（支持模糊）定位三级行业。"""
    text = str(query or "").strip()
    if not text:
        return None
    frame = builtin_industry_frame()
    by_id = frame[frame["industry_l3_id"].astype(str).eq(text)]
    by_name = frame[frame["industry_l3_name"].astype(str).eq(text)]
    target = by_id if not by_id.empty else by_name
    if target.empty:
        matched = match_industry_name(text)
        if not matched["industry_l3_id"]:
            return None
        target = frame[frame["industry_l3_id"].eq(matched["industry_l3_id"])]
    if target.empty:
        return None
    row = target.iloc[0]
    return {
        "industry_l3_id": str(row["industry_l3_id"]),
        "industry_l3_name": str(row["industry_l3_name"]),
        "industry_l2_name": str(row["industry_l2_name"]),
        "industry_l1_name": str(row["industry_l1_name"]),
    }


def industry_data_path(industry_l3_id: object, directory: str | Path = INDUSTRY_DATA_DIR) -> Path:
    return Path(directory) / f"{str(industry_l3_id).strip()}.csv"


def load_industry_financials(
    industry_l3_id: object,
    directory: str | Path = INDUSTRY_DATA_DIR,
) -> pd.DataFrame:
    source = industry_data_path(industry_l3_id, directory)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame(columns=STORED_COLUMNS)
    return pd.read_csv(source, dtype={"industry_l3_id": str}, encoding="utf-8-sig")


def save_industry_financials(
    industry_l3_id: object,
    data: pd.DataFrame,
    directory: str | Path = INDUSTRY_DATA_DIR,
) -> pd.DataFrame:
    target = industry_data_path(industry_l3_id, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = [column for column in STORED_COLUMNS if column in data.columns]
    columns += [column for column in data.columns if column not in columns]
    ordered = data[columns].copy()
    ordered["report_period"] = pd.to_datetime(ordered["report_period"], errors="coerce").dt.strftime("%Y-%m-%d")
    ordered["announcement_date"] = pd.to_datetime(
        ordered["announcement_date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    ordered = ordered.sort_values(["company_id", "report_period", "announcement_date"])
    ordered.to_csv(target, index=False, encoding="utf-8-sig")
    return ordered.reset_index(drop=True)


def load_all_industry_financials(directory: str | Path = INDUSTRY_DATA_DIR) -> pd.DataFrame:
    """读取已同步的全部三级行业财报，供按行业分组展示。"""
    root = Path(directory)
    if not root.exists():
        return pd.DataFrame(columns=STORED_COLUMNS)
    frames = []
    for source in sorted(root.glob("*.csv")):
        # 下划线开头的是检索台账等辅助文件，不是行业财报
        if source.name.startswith("_") or source.stat().st_size == 0:
            continue
        frames.append(pd.read_csv(source, dtype={"industry_l3_id": str}, encoding="utf-8-sig"))
    if not frames:
        return pd.DataFrame(columns=STORED_COLUMNS)
    combined = pd.concat(frames, ignore_index=True, sort=False)
    for column in STORED_COLUMNS:
        if column not in combined:
            combined[column] = pd.NA
    # 个别 CSV 末行可能是不完整的空行，丢掉没有公司和行业标识的记录
    combined = combined[
        combined["company_id"].notna() & combined["industry_l3_id"].notna()
    ]
    return combined.reset_index(drop=True)


def _search_log_path(directory: str | Path = INDUSTRY_DATA_DIR) -> Path:
    return Path(directory) / SEARCH_LOG_FILE


def load_search_log(directory: str | Path = INDUSTRY_DATA_DIR) -> pd.DataFrame:
    """检索台账：记录每家公司上次联网核对的时间，避免短期内重复检索。"""
    source = _search_log_path(directory)
    if not source.exists() or source.stat().st_size == 0:
        return pd.DataFrame(columns=SEARCH_LOG_COLUMNS)
    frame = pd.read_csv(source, dtype={"industry_l3_id": str}, encoding="utf-8-sig")
    for column in SEARCH_LOG_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame


def record_search(
    industry_l3_id: object,
    company_id: str,
    as_of_date: str | date,
    found: bool,
    directory: str | Path = INDUSTRY_DATA_DIR,
    searched_at: date | None = None,
) -> None:
    log = load_search_log(directory)
    industry_id = str(industry_l3_id).strip()
    code = str(company_id).strip().upper()
    as_of = pd.Timestamp(as_of_date).normalize().date().isoformat()
    stamp = (searched_at or date.today()).isoformat()
    keep = log[
        ~log["industry_l3_id"].astype(str).eq(industry_id)
        | ~log["company_id"].astype(str).str.upper().eq(code)
    ]
    updated = pd.concat(
        [
            keep,
            pd.DataFrame(
                [
                    {
                        "industry_l3_id": industry_id,
                        "company_id": code,
                        "as_of_date": as_of,
                        "searched_at": stamp,
                        "found": bool(found),
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    target = _search_log_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    updated.to_csv(target, index=False, encoding="utf-8-sig")


def synced_industries(directory: str | Path = INDUSTRY_DATA_DIR) -> pd.DataFrame:
    """已同步财报的三级行业清单与覆盖情况。"""
    data = load_all_industry_financials(directory)
    if data.empty:
        return pd.DataFrame(
            columns=["industry_l3_id", "industry_l3_name", "company_count", "record_count", "latest_announcement"]
        )
    grouped = data.groupby(["industry_l3_id", "industry_l3_name"], as_index=False).agg(
        company_count=("company_id", "nunique"),
        record_count=("company_id", "size"),
        latest_announcement=("announcement_date", "max"),
    )
    grouped["industry_l3_id"] = grouped["industry_l3_id"].astype(str)
    return grouped.sort_values("industry_l3_id").reset_index(drop=True)


@dataclass(frozen=True)
class IndustryCacheStatus:
    """某三级行业本地财报缓存的新鲜度，用于避免重复联网。"""

    industry_l3_id: str
    industry_l3_name: str
    has_cache: bool
    is_fresh: bool
    covered: int
    total: int
    latest_period: pd.Timestamp | None = None
    latest_announcement: pd.Timestamp | None = None
    missing_ids: tuple[str, ...] = ()
    missing_names: tuple[str, ...] = ()
    pending_ids: tuple[str, ...] = ()
    pending_names: tuple[str, ...] = ()
    recently_checked: tuple[str, ...] = ()

    @property
    def needs_network(self) -> bool:
        """是否真的需要联网：无缓存，或有公司既缺数据又不在检索冷却期内。"""
        return not self.has_cache or bool(self.pending_ids)

    @property
    def message(self) -> str:
        period = f"{self.latest_period:%Y-%m-%d}" if self.latest_period is not None else "—"
        if not self.has_cache:
            return f"{self.industry_l3_name} 本地尚无财报缓存，需要联网拉取。"
        if self.is_fresh:
            announced = (
                f"{self.latest_announcement:%Y-%m-%d}"
                if self.latest_announcement is not None
                else "—"
            )
            return (
                f"{self.industry_l3_name} 本地已是最新：{self.covered}/{self.total} 家成份公司"
                f"都已取得 {period} 报告期数据，最新公告日 {announced}，"
                "本次未联网、未重复下载。"
            )
        if not self.pending_ids:
            return (
                f"{self.industry_l3_name}：{self.covered}/{self.total} 家公司已取得 {period} "
                f"报告期数据，其余 {len(self.recently_checked)} 家在冷却期内刚核对过，"
                "公开渠道暂无该报告期公告，本次未联网。"
            )
        preview = "、".join(self.pending_names[:8])
        if len(self.pending_names) > 8:
            preview += f" 等 {len(self.pending_names)} 家"
        text = (
            f"{self.industry_l3_name} 本地有 {self.covered}/{self.total} 家公司"
            f"已取得 {period} 报告期数据，将只补拉缺少的 {len(self.pending_ids)} 家：{preview}。"
        )
        if self.recently_checked:
            text += f"（另有 {len(self.recently_checked)} 家近期已核对，本次跳过。）"
        return text


def _latest_covered(
    constituents: pd.DataFrame,
    existing: pd.DataFrame,
    as_of: pd.Timestamp,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None, set[str]]:
    """返回（截止日之前的最新报告期、最新公告日、已有该报告期数据的公司）。"""
    if existing.empty:
        return None, None, set()
    frame = existing.copy()
    frame["report_period"] = pd.to_datetime(frame.get("report_period"), errors="coerce")
    frame["announcement_date"] = pd.to_datetime(frame.get("announcement_date"), errors="coerce")
    usable = frame[frame["announcement_date"].notna() & frame["announcement_date"].le(as_of)]
    if usable.empty:
        return None, None, set()
    latest_period = usable["report_period"].max()
    if pd.isna(latest_period):
        return None, usable["announcement_date"].max(), set()
    covered = set(
        usable.loc[usable["report_period"].eq(latest_period), "company_id"]
        .astype(str)
        .str.upper()
    )
    return latest_period, usable["announcement_date"].max(), covered


def check_industry_cache(
    industry: str,
    as_of_date: str | date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    directory: str | Path = INDUSTRY_DATA_DIR,
    cooldown_days: int = DEFAULT_SEARCH_COOLDOWN_DAYS,
    today: date | None = None,
) -> IndustryCacheStatus:
    """纯本地判断：该行业各成份公司是否都已取得最新报告期数据。

    不联网。若判定为最新（或缺少的公司都还在检索冷却期内），调用方可以直接跳过同步并提示用户。
    """
    info = resolve_industry(industry)
    if info is None:
        raise ValueError(f"未匹配到三级行业：{industry}")
    industry_id = info["industry_l3_id"]
    industry_name = info["industry_l3_name"]
    constituents = constituents_for([industry_id]).drop_duplicates("company_id")
    existing = load_industry_financials(industry_id, directory)
    log = load_search_log(directory)
    return _build_cache_status(
        industry_id,
        industry_name,
        constituents,
        existing,
        as_of_date,
        log,
        cooldown_days,
        today,
    )


def _build_cache_status(
    industry_id: str,
    industry_name: str,
    constituents: pd.DataFrame,
    existing: pd.DataFrame,
    as_of_date: str | date,
    search_log: pd.DataFrame | None = None,
    cooldown_days: int = DEFAULT_SEARCH_COOLDOWN_DAYS,
    today: date | None = None,
) -> IndustryCacheStatus:
    as_of = pd.Timestamp(as_of_date).normalize()
    latest_period, latest_announcement, covered = _latest_covered(constituents, existing, as_of)
    ids = constituents["company_id"].astype(str).str.upper()
    names = constituents["company_name"].astype(str)
    missing_mask = ~ids.isin(covered)
    missing_ids = tuple(ids[missing_mask].tolist())
    missing_names = tuple(names[missing_mask].tolist())

    pending: list[str] = []
    checked: list[str] = []
    for code in missing_ids:
        if _recently_searched(search_log, industry_id, code, as_of, cooldown_days, today):
            checked.append(code)
        else:
            pending.append(code)
    name_of = dict(zip(ids.tolist(), names.tolist()))

    return IndustryCacheStatus(
        industry_l3_id=industry_id,
        industry_l3_name=industry_name,
        has_cache=not existing.empty,
        is_fresh=bool(covered) and not missing_ids,
        covered=int(len(covered)),
        total=int(len(constituents)),
        latest_period=latest_period,
        latest_announcement=latest_announcement,
        missing_ids=missing_ids,
        missing_names=missing_names,
        pending_ids=tuple(pending),
        pending_names=tuple(name_of.get(code, code) for code in pending),
        recently_checked=tuple(checked),
    )


def _recently_searched(
    search_log: pd.DataFrame | None,
    industry_id: str,
    company_id: str,
    as_of: pd.Timestamp,
    cooldown_days: int,
    today: date | None = None,
) -> bool:
    """该公司是否在冷却期内刚联网核对过（同一截止日）。"""
    if search_log is None or search_log.empty or cooldown_days <= 0:
        return False
    matched = search_log[
        search_log["industry_l3_id"].astype(str).eq(str(industry_id))
        & search_log["company_id"].astype(str).str.upper().eq(str(company_id).upper())
    ]
    if matched.empty:
        return False
    as_of_text = as_of.date().isoformat()
    matched = matched[matched["as_of_date"].astype(str).eq(as_of_text)]
    if matched.empty:
        return False
    stamps = pd.to_datetime(matched["searched_at"], errors="coerce")
    latest = stamps.max()
    if pd.isna(latest):
        return False
    reference = pd.Timestamp(today or date.today())
    return (reference.normalize() - latest.normalize()).days <= cooldown_days


def sync_industry_reports(
    industry: str,
    as_of_date: str | date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_companies: int | None = None,
    progress: ProgressCallback | None = None,
    client: CninfoClient | None = None,
    refresh_market_cap: bool = False,
    directory: str | Path = INDUSTRY_DATA_DIR,
    maximum_pages: int | None = None,
    company_ids: list[str] | None = None,
    only_missing: bool = False,
    reextract: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """拉取某三级行业全部成份公司近 N 天的正式定期报告并抽取财报指标。

    `company_ids` 只用于补拉个别公司，重跑时不会重复下载已有公告。
    `only_missing=True` 时先做本地判断，已齐全则直接返回不联网，否则只补拉缺最新报告期的公司。
    `reextract=True` 时忽略已有公告，重新下载并解析（抽取逻辑修正后用于回填历史数据）。
    """
    info = resolve_industry(industry)
    if info is None:
        raise ValueError(f"未匹配到三级行业：{industry}")
    industry_id = info["industry_l3_id"]
    industry_name = info["industry_l3_name"]

    constituents = constituents_for([industry_id])
    if constituents.empty:
        raise ValueError(
            f"{industry_name} 尚未固化成份公司，"
            "可运行 `python tools/freeze_sw_constituents.py --industry-ids " + str(industry_id) + "` 补齐"
        )
    constituents = constituents.drop_duplicates("company_id").sort_values("company_id")
    all_company_ids = constituents["company_id"].tolist()
    if company_ids:
        requested = {str(value).strip().upper() for value in company_ids}
        known = set(constituents["company_id"].astype(str).str.upper())
        unknown = requested.difference(known)
        if unknown:
            raise ValueError(f"以下公司不属于 {industry_name}：{', '.join(sorted(unknown))}")
        constituents = constituents[
            constituents["company_id"].astype(str).str.upper().isin(requested)
        ].copy()
    if max_companies is not None:
        constituents = constituents.head(max_companies).copy()
    model_type = resolve_model_type(
        industry_name, info["industry_l2_name"], info["industry_l1_name"]
    )

    as_of = pd.Timestamp(as_of_date).normalize()
    start = as_of - pd.Timedelta(days=lookback_days)

    if only_missing and not company_ids:
        status = _build_cache_status(
            industry_id,
            industry_name,
            constituents,
            load_industry_financials(industry_id, directory),
            as_of,
            load_search_log(directory),
        )
        if not status.needs_network:
            # 各成份公司都已取得最新报告期数据，或缺的公司都在检索冷却期内，无需联网
            return load_industry_financials(industry_id, directory).reset_index(drop=True), pd.DataFrame()
        if status.has_cache and status.pending_ids:
            company_ids = list(status.pending_ids)
            constituents = constituents[
                constituents["company_id"].astype(str).str.upper().isin(
                    {str(item).upper() for item in company_ids}
                )
            ].copy()

    if progress:
        progress(0, len(constituents), "取总市值")
    errors: list[dict[str, object]] = []
    try:
        # 始终按全行业取市值，保证补拉个别公司时也能补齐整个行业的市值
        sync_market_caps(all_company_ids, refresh=refresh_market_cap)
    except Exception as exc:
        # 市值缺失只影响市值这一项指标的完整度，不应中断财报同步
        errors.append({"company_id": "", "stage": "market_cap", "error": str(exc)})

    existing = load_industry_financials(industry_id, directory)
    known_ids = (
        set()
        if reextract
        else set(existing.get("announcement_id", pd.Series(dtype=str)).dropna().astype(str))
    )
    api = client or CninfoClient()
    new_rows: list[dict[str, object]] = []
    total = len(constituents)
    for position, company in enumerate(constituents.itertuples()):
        company_id = str(company.company_id).strip().upper()
        company_name = str(company.company_name)
        if progress:
            progress(position + 1, total, f"{company_id} {company_name}")
        try:
            reports = api.search_periodic_reports(company_id, start, as_of)
        except Exception as exc:
            errors.append({"company_id": company_id, "stage": "search", "error": str(exc)})
            continue
        record_search(industry_id, company_id, as_of, bool(reports), directory)
        for report in reports:
            if report.announcement_id in known_ids:
                continue
            try:
                pdf_content = api.download_pdf(report.pdf_url)
                row = extract_financial_metrics(pdf_content, report, maximum_pages=maximum_pages)
            except Exception as exc:
                errors.append(
                    {
                        "company_id": company_id,
                        "announcement_id": report.announcement_id,
                        "report_period": report.report_period,
                        "stage": "extract",
                        "source_url": report.pdf_url,
                        "error": str(exc),
                    }
                )
                continue
            row.update(
                {
                    "industry_l3_id": industry_id,
                    "industry_l3_name": industry_name,
                    "model_type": model_type,
                }
            )
            new_rows.append(row)
            known_ids.add(report.announcement_id)

    additions = pd.DataFrame(new_rows)
    if existing.empty:
        combined = additions
    elif additions.empty:
        combined = existing
    else:
        combined = pd.concat([existing, additions], ignore_index=True, sort=False)
    if not combined.empty:
        combined = combined.drop_duplicates("announcement_id", keep="last")
        combined = attach_market_cap(combined)
        combined = save_industry_financials(industry_id, combined, directory)
    else:
        combined = pd.DataFrame(columns=STORED_COLUMNS)
    return combined.reset_index(drop=True), pd.DataFrame(errors)


def reparse_abnormal_records(
    industry: str,
    directory: str | Path = INDUSTRY_DATA_DIR,
    progress: ProgressCallback | None = None,
    client: CninfoClient | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """只重新解析本地异常记录（毛利率接近 100% 或成本没抽到）。

    用记录里已保存的公告原文链接重新下载解析，不重新检索公告列表，
    用于抽取逻辑修正后精准回填那些"这次没被数据源返回"的历史记录。
    """
    info = resolve_industry(industry)
    if info is None:
        raise ValueError(f"未匹配到三级行业：{industry}")
    industry_id = info["industry_l3_id"]
    data = load_industry_financials(industry_id, directory)
    if data.empty:
        return data, []
    margins = pd.to_numeric(data.get("gross_margin"), errors="coerce")
    revenues = pd.to_numeric(data.get("revenue"), errors="coerce")
    abnormal = margins.ge(MAX_GROSS_MARGIN).fillna(False) | (
        revenues.notna() & revenues.ne(0) & margins.isna()
    )
    targets = data[abnormal]
    if targets.empty:
        return data, []
    api = client or CninfoClient()
    failed: list[str] = []
    total = len(targets)
    for position, (index, row) in enumerate(targets.iterrows(), start=1):
        label = f"{row.get('company_name', '')} {row.get('report_period', '')}"
        if progress:
            progress(position, total, str(label))
        report = ReportRecord(
            company_id=str(row.get("company_id", "")),
            company_name=str(row.get("company_name", "")),
            announcement_id=str(row.get("announcement_id", "")),
            announcement_date=str(row.get("announcement_date", "")),
            report_period=str(row.get("report_period", "")),
            report_type=str(row.get("report_type") or "定期报告"),
            title=str(row.get("source_title", "")),
            pdf_url=str(row.get("source_url", "")),
        )
        try:
            metrics = extract_financial_metrics(api.download_pdf(report.pdf_url), report)
        except Exception as exc:
            failed.append(f"{label}：{exc}")
            continue
        for key, value in metrics.items():
            data.loc[index, key] = value
    data = attach_market_cap(data)
    return save_industry_financials(industry_id, data, directory), failed


def choose_common_report_period(
    group: pd.DataFrame,
    as_of_date: str | date,
    minimum_ratio: float = DEFAULT_PERIOD_COVERAGE_RATIO,
    minimum_companies: int = DEFAULT_PERIOD_MIN_COMPANIES,
) -> tuple[pd.Timestamp | None, str]:
    """为该行业挑一个"共同"报告期。

    默认取截止日之前最新的报告期；若该报告期披露的公司太少（不足行业公司数的
    `minimum_ratio` 或不足 `minimum_companies` 家），则回退到下一个报告期，
    避免用个位数样本做横截面排名。返回（报告期，说明）。
    """
    announcements = pd.to_datetime(group.get("announcement_date"), errors="coerce")
    periods = pd.to_datetime(group.get("report_period"), errors="coerce")
    as_of = pd.Timestamp(as_of_date).normalize()
    codes = group["company_id"].astype(str).str.upper() if "company_id" in group else None
    frame = pd.DataFrame({"company_id": codes, "period": periods})
    frame = frame[announcements.notna() & announcements.le(as_of)].dropna(subset=["period"])
    frame["period"] = frame["period"].dt.normalize()
    if frame.empty:
        return None, ""
    # 同一公司同一报告期可能有更正公告，按公司去重后再数
    counts = frame.groupby("period")["company_id"].nunique()
    total = int(frame["company_id"].nunique())
    threshold = max(minimum_companies, math.ceil(total * minimum_ratio))
    ordered = sorted(counts.index, reverse=True)
    for period in ordered:
        if int(counts[period]) >= threshold:
            note = ""
            if period != ordered[0]:
                note = (
                    f"最新报告期 {ordered[0]:%Y-%m-%d} 仅 {int(counts[ordered[0]])} 家公司披露，"
                    f"样本过少，已改用覆盖 {int(counts[period])}/{total} 家的 {period:%Y-%m-%d} 报告期排名。"
                )
            return period, note
    latest = ordered[0]
    return latest, (
        f"各报告期披露的公司都不多，仍取最新报告期 {latest:%Y-%m-%d}"
        f"（{int(counts[latest])}/{total} 家），排名稳定性较弱。"
    )


def rank_industry_financials(
    data: pd.DataFrame,
    as_of_date: str | date,
    report_period: str | date | None = None,
    top_n: int = DEFAULT_TOP_N,
    minimum_coverage: float = 0.60,
    auto_period: bool = True,
) -> tuple[pd.DataFrame, list[str]]:
    """按三级行业分组调用现有算法；每个行业各自取最新共同报告期。"""
    if data.empty:
        return pd.DataFrame(), []
    frames: list[pd.DataFrame] = []
    notes: list[str] = []
    frame = data.copy()
    frame["industry_l3_id"] = frame["industry_l3_id"].astype(str)
    for industry_id, group in frame.groupby("industry_l3_id", sort=True):
        chosen = report_period
        if chosen is None and auto_period:
            chosen, note = choose_common_report_period(group, as_of_date)
            if note:
                name = (
                    str(group["industry_l3_name"].iloc[0])
                    if "industry_l3_name" in group
                    else industry_id
                )
                notes.append(f"{name}：{note}")
        try:
            _, ranking = rank_online_financials(
                group,
                as_of_date=as_of_date,
                report_period=chosen,
                top_n=top_n,
                minimum_coverage=minimum_coverage,
            )
        except ValueError as exc:
            name = str(group["industry_l3_name"].iloc[0]) if "industry_l3_name" in group else industry_id
            notes.append(f"{name}：{exc}")
            continue
        frames.append(ranking)
    if not frames:
        return pd.DataFrame(), notes
    return pd.concat(frames, ignore_index=True, sort=False), notes


def available_report_periods(data: pd.DataFrame, as_of_date: str | date) -> list[pd.Timestamp]:
    announcements = pd.to_datetime(data["announcement_date"], errors="coerce")
    periods = pd.to_datetime(data["report_period"], errors="coerce")
    usable = periods[announcements.le(pd.Timestamp(as_of_date).normalize())].dropna().unique()
    return sorted((pd.Timestamp(item).normalize() for item in usable), reverse=True)
