from __future__ import annotations

from datetime import date
from io import BytesIO

import altair as alt
import pandas as pd
import streamlit as st

from data_io import (
    dataframe_to_excel_bytes,
    normalize_input,
    read_uploaded_file,
    template_to_excel_bytes,
)
from disclosure_pipeline import DEFAULT_DATA_PATH, load_online_financials, sync_official_reports
from industry_catalog import load_catalog, suggest_model_type
from industry_pipeline import (
    DEFAULT_LOOKBACK_DAYS,
    available_report_periods,
    check_industry_cache,
    focus_industry_groups,
    focus_industries,
    load_all_industry_financials,
    rank_industry_financials,
    sync_industry_reports,
)
from industry_match import (
    SOURCE_URL,
    SNAPSHOT_DATE,
    industry_label,
    industry_options,
    search_industries,
)
from liquidity import (
    INDEX_LABELS,
    REFERENCE_MIN_MONTHS,
    REFERENCE_MIN_WEEKS,
    REFERENCE_WINDOW_MONTHS,
    REFERENCE_WINDOW_WEEKS,
    compute_liquidity_indices,
    derive_margin_yoy,
    prepare_metric_display_series,
    rolling_reference,
    summarize_index,
)
from liquidity_sources import (
    MANUAL_IMPORT_NOTE,
    METRIC_LABELS,
    SOURCE_REGISTRY,
    load_all_metrics,
    load_cached_metric,
    update_liquidity_cache,
)
from nbs_provider import DEFAULT_HISTORY_PATH, load_history, update_history
from prosperity import (
    DEFAULT_TOP_N,
    DEFAULT_WINDOW,
    corresponding_industries,
    prosperity_board,
    top_companies_for_stat_industry,
)
from ranking import MODEL_SPECS, model_weight_table, prepare_growth_metrics, score_snapshot, select_snapshot
from sw_constituents import SNAPSHOT_DATE, constituents_for, industry_coverage
from user_commands import parse_update_command

st.set_page_config(page_title="万得三级行业观察库", page_icon=None, layout="wide")
st.markdown(
    """
    <style>
    .constituent-tip {
        background: #fff3cd;
        border-left: 4px solid #f0b429;
        color: #6b4e00;
        padding: 6px 12px;
        border-radius: 4px;
        font-size: 0.92rem;
        margin: 4px 0 8px 0;
    }
    /* 只高亮折叠面板里的“三级行业成分公司”下拉菜单，不影响侧栏其它菜单 */
    div[data-testid="stExpander"] div[data-testid="stSelectbox"] label {
        color: #6b4e00;
        font-weight: 600;
    }
    div[data-testid="stExpander"] div[data-baseweb="select"] > div {
        background-color: #ffe9a8;
        border: 1px solid #e8a100;
        border-radius: 6px;
        color: #3d2e00;
        font-weight: 600;
    }
    div[data-testid="stExpander"] div[data-baseweb="select"] > div:focus-within {
        border-color: #c27c00;
        box-shadow: 0 0 0 2px rgba(232, 161, 0, 0.35);
    }
    div[data-testid="stExpander"] div[data-baseweb="select"] svg {
        fill: #6b4e00;
    }
    div[data-baseweb="popover"] ul[role="listbox"] li[role="option"]:hover {
        background-color: #ffe9a8;
    }
    div[data-baseweb="popover"] ul[role="listbox"] li[aria-selected="true"] {
        background-color: #ffd54f;
        color: #3d2e00;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
st.title("万得三级行业动态观察库")
st.caption("按公告日生成点时排名；默认每个万得三级行业取前 20 名。结果仅用于量化研究，不构成投资建议。")

with st.sidebar:
    st.header("数据与参数")
    uploaded = st.file_uploader("上传万得季度数据", type=["csv", "xlsx", "xlsm"])
    st.download_button(
        "下载数据模板",
        data=template_to_excel_bytes(),
        file_name="wind_industry_ranking_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    top_n = int(st.number_input("每行业观察数量", min_value=1, max_value=100, value=20, step=1))
    minimum_coverage = float(
        st.slider("最低数据完整度", min_value=0.0, max_value=1.0, value=0.60, step=0.05)
    )
    model_override = st.selectbox(
        "排名模型",
        options=["auto", *MODEL_SPECS.keys()],
        format_func=lambda value: "自动识别" if value == "auto" else MODEL_SPECS[value].label,
    )
    st.caption("数据源：巨潮资讯正式定期报告的本地提取缓存，不含演示数据。")
    st.divider()
    catalog = load_catalog()
    taxonomy = industry_options()
    st.caption(
        f"内置申万三级行业 {len(taxonomy)} 个（快照 {SNAPSHOT_DATE}）。"
        "没有万得授权时直接使用该固定列表；行业名称支持语义模糊匹配。"
    )
    industry_keyword = st.text_input("三级行业模糊搜索", placeholder="例如：白酒、锂电池、半导体设备")
    matched = search_industries(industry_keyword, limit=200)
    selected_industry_id = st.selectbox(
        "定位三级行业",
        options=matched["industry_l3_id"].tolist(),
        format_func=lambda value: industry_label(matched[matched["industry_l3_id"].eq(value)].iloc[0]),
    )
    if catalog.empty:
        st.warning("尚未固化行业目录。")
    update_command = st.text_input(
        "更新命令",
        placeholder="更新知名公司 截止 2026-10-31",
        help="支持更新知名公司、更新全部公司、更新公司 600000.SH、更新行业景气度",
    )
    run_update = st.button("执行更新命令", use_container_width=True)

if run_update:
    try:
        parsed_command = parse_update_command(update_command)
        progress_bar = st.progress(0.0, text="准备更新")
        if parsed_command.scope == "prosperity":
            with st.spinner("正在从国家统计局检索分行业工业增加值与利润数据。"):
                history = update_history(months=6)
            progress_bar.progress(1.0, text="更新完成")
            months = sorted(history["month"].astype(str).unique())
            st.success(
                f"景气数据已更新，本地共 {len(history)} 条月度记录，最新月份 {months[-1] if months else '无'}。"
            )
        else:
            progress_bar.progress(0.0, text="准备同步官方定期报告")

            def show_progress(current: int, total: int, message: str) -> None:
                progress_bar.progress(current / max(total, 1), text=message)

            with st.spinner("正在从巨潮资讯检索并解析正式定期报告，请勿关闭页面。"):
                updated_data, update_errors = sync_official_reports(
                    as_of_date=parsed_command.as_of_date,
                    scope=parsed_command.scope,
                    company_ids=parsed_command.company_ids or None,
                    progress=show_progress,
                )
            progress_bar.progress(1.0, text="更新完成")
            st.success(f"本地已累计 {len(updated_data)} 条正式定期报告记录。")
            if not update_errors.empty:
                st.warning(f"有 {len(update_errors)} 条检索或提取失败，请使用命令行查看错误明细。")
    except Exception as exc:
        st.error(f"更新失败：{exc}")

try:
    if uploaded is not None:
        raw_data = read_uploaded_file(uploaded, uploaded.name)
    else:
        raw_data = load_online_financials(DEFAULT_DATA_PATH)
        if raw_data.empty:
            # 官方公告缓存为空时，改用三级行业财报缓存（真实公告抽取结果）
            raw_data = load_all_industry_financials()
        if raw_data.empty:
            raise ValueError(
                "本地还没有正式定期报告数据。请先在「行业详情」页签拉取近一年财报，"
                "或上传万得季度数据。"
            )
        st.info("当前使用巨潮资讯正式定期报告的本地提取缓存。")

    normalized = normalize_input(raw_data)
    if model_override != "auto":
        normalized["model_type"] = model_override
except Exception as exc:
    st.error(f"读取数据失败：{exc}")
    st.stop()

valid_announcement_dates = normalized["announcement_date"].dropna()
if valid_announcement_dates.empty:
    st.error("没有有效的公告日期。")
    st.stop()

default_as_of = valid_announcement_dates.max().date()
as_of = st.sidebar.date_input("信息截止日", value=default_as_of, max_value=date(2100, 12, 31))
known_at_as_of = normalized[normalized["announcement_date"].le(pd.Timestamp(as_of))]
periods = sorted(known_at_as_of["report_period"].dropna().dt.normalize().unique(), reverse=True)
if not periods:
    st.warning("该截止日之前没有已公告的报告期数据。")
    st.stop()
report_period = st.sidebar.selectbox(
    "报告期",
    options=periods,
    format_func=lambda value: pd.Timestamp(value).strftime("%Y-%m-%d"),
)

try:
    prepared = prepare_growth_metrics(normalized, as_of)
    snapshot = select_snapshot(prepared, as_of, pd.Timestamp(report_period))
    ranking = score_snapshot(snapshot, top_n=top_n, minimum_coverage=minimum_coverage)
except Exception as exc:
    st.error(f"计算排名失败：{exc}")
    st.stop()

watchlist = ranking[ranking["in_watchlist"]].copy()
industry_count = ranking["industry_l3_id"].nunique() if not ranking.empty else 0
eligible_count = int(ranking["eligible"].sum()) if not ranking.empty else 0
metric_columns = st.columns(4)
metric_columns[0].metric("三级行业", f"{industry_count}")
metric_columns[1].metric("本期已公告公司", f"{len(ranking)}")
metric_columns[2].metric("符合完整度要求", f"{eligible_count}")
metric_columns[3].metric("进入观察库", f"{len(watchlist)}")

if ranking.empty:
    st.warning("当前截止日与报告期组合没有可排名公司。")
    st.stop()

small_industries = (
    ranking.loc[ranking["industry_company_count"].lt(5), "industry_l3_name"].dropna().unique().tolist()
)
if small_industries:
    st.warning(f"以下行业样本少于 5 家，相对排名稳定性较弱：{'、'.join(map(str, small_industries))}")

all_industry_financials = load_all_industry_financials()

DISPLAY_COLUMNS = [
    "industry_l3_id",
    "industry_l3_name",
    "company_id",
    "company_name",
    "rank_model_label",
    "industry_rank",
    "weighted_score",
    "data_coverage",
    "market_cap",
    "revenue",
    "net_profit",
    "revenue_yoy",
    "profit_growth_adjusted",
    "gross_margin",
    "announcement_date",
    "missing_metrics",
]
COLUMN_LABELS = {
    "industry_l3_id": "三级行业ID",
    "industry_l3_name": "三级行业",
    "company_id": "证券代码",
    "company_name": "公司名称",
    "rank_model_label": "模型",
    "industry_rank": "行业排名",
    "weighted_score": "综合得分",
    "data_coverage": "数据完整度",
    "market_cap": "市值(亿元)",
    "revenue": "营收(千万元)",
    "net_profit": "净利润(千万元)",
    "revenue_yoy": "营收增长率(%)",
    "profit_growth_adjusted": "利润增长率(%)",
    "gross_margin": "毛利率(%)",
    "announcement_date": "公告日",
    "missing_metrics": "缺失指标",
}

# 表格排序依据：界面标签 → 原始指标列，统一按从高到低排列
SORT_METRICS: dict[str, str] = {
    "综合得分": "weighted_score",
    "市值": "market_cap",
    "营收": "revenue",
    "净利润": "net_profit",
    "营收增长率": "revenue_yoy",
    "利润增长率": "profit_growth_adjusted",
    "毛利率": "gross_margin",
}

UNIT_CAPTION = (
    "单位：市值亿元，营收与净利润千万元，增长率与毛利率为百分比。"
    "上方选定排序依据后表格按该项从高到低排列，缺失该指标的公司排在最后；"
    "点击表头也可临时改排序。"
)

# 流动性原始曲线的展示顺序：先短端三项，再信用货币增速四项
METRIC_CHART_ORDER: tuple[str, ...] = (
    "dr007",
    "bond_10y",
    "margin_balance",
    "m1_m2_gap",
    "m2_yoy",
    "social_financing_stock",
    "social_financing_yoy",
    "margin_balance_yoy",
)

METRIC_CHART_NOTES: dict[str, str] = {
    "dr007": (
        "单位 %。银行间存款类机构 7 天回购利率（FDR007 定盘价），"
        "季末、年末等跨期时点常见脉冲式冲高。越高越紧。"
    ),
    "bond_10y": "单位 %。中债十年期国债到期收益率，反映长端无风险利率水平。越高越紧。",
    "margin_balance": (
        "单位 万亿元。沪深两融余额水平，衡量场内杠杆资金的体量。越高越宽松。"
    ),
    "m1_m2_gap": (
        "单位 百分点。M1 同比 − M2 同比，剪刀差走阔代表资金活化、交易需求上升。越高越宽松。"
    ),
    "m2_yoy": "单位 %。广义货币供应量同比增速，月频公布，次月 10 日左右生效。",
    "social_financing_stock": (
        "单位 万亿元。社会融资规模存量余额，衡量实体信用总量，"
        "从央行《社会融资规模存量统计表》按月解析。越高越宽松。"
    ),
    "social_financing_yoy": (
        "单位 %。社融存量同比增速，直接取央行存量统计表中的「增速」列，"
        "与公众号月度报告同源，参与信用货币增速指数合成。越高越宽松。"
    ),
    "margin_balance_yoy": (
        "单位 %。两融余额相对 52 周前的同比增速（由两融余额水平派生），"
        "属增速类指标，参与信用货币增速指数合成。越高越宽松。"
    ),
}


def sort_by_metric(frame: pd.DataFrame, metric_column: str | None) -> pd.DataFrame:
    """按指定指标从高到低排序；缺少该指标的行排在最后。"""
    if frame.empty or not metric_column or metric_column not in frame.columns:
        return frame
    values = pd.to_numeric(frame[metric_column], errors="coerce")
    ordered = frame.assign(_sort_value=values).sort_values(
        "_sort_value", ascending=False, na_position="last", kind="mergesort"
    )
    return ordered.drop(columns="_sort_value")


def apply_display_units(shown: pd.DataFrame) -> pd.DataFrame:
    """把原始数值换算成表格展示单位并统一小数位。"""
    shown = shown.copy()
    for column in ("市值(亿元)", "总市值(亿元)"):
        if column in shown:
            shown[column] = (pd.to_numeric(shown[column], errors="coerce") / 1e8).round(2)
    for column in ("营收(千万元)", "净利润(千万元)"):
        if column in shown:
            shown[column] = (pd.to_numeric(shown[column], errors="coerce") / 1e7).round(2)
    for column in ("营收增长率(%)", "利润增长率(%)", "毛利率(%)"):
        if column in shown:
            shown[column] = (pd.to_numeric(shown[column], errors="coerce") * 100).round(2)
    if "综合得分" in shown:
        shown["综合得分"] = pd.to_numeric(shown["综合得分"], errors="coerce").round(2)
    if "数据完整度" in shown:
        shown["数据完整度"] = (
            pd.to_numeric(shown["数据完整度"], errors="coerce") * 100
        ).round(1).astype(str) + "%"
    for column in ("报告期", "公告日"):
        if column in shown:
            shown[column] = pd.to_datetime(shown[column], errors="coerce").dt.strftime("%Y-%m-%d")
    return shown


def align_profit_growth(frame: pd.DataFrame) -> pd.DataFrame:
    """创新药行业的利润增长率换成参与排名的对称增长率，保证各处展示口径一致。"""
    prepared = frame.copy()
    if "profit_growth_symmetric" not in prepared.columns:
        return prepared
    model_column = "rank_model" if "rank_model" in prepared.columns else "model_type"
    if model_column not in prepared.columns:
        return prepared
    symmetric_rows = prepared[model_column].astype(str).eq("innovative_drug")
    if symmetric_rows.any():
        prepared.loc[symmetric_rows, "profit_growth_adjusted"] = prepared.loc[
            symmetric_rows, "profit_growth_symmetric"
        ]
    return prepared


def display_frame(frame: pd.DataFrame, sort_by: str | None = None) -> pd.DataFrame:
    prepared = align_profit_growth(frame)
    # 排序放在口径替换之后：按利润增长率排序时用的就是表格里展示的那个值
    prepared = sort_by_metric(prepared, sort_by)
    columns = [column for column in DISPLAY_COLUMNS if column in prepared.columns]
    shown = prepared[columns].rename(columns=COLUMN_LABELS)
    return apply_display_units(shown)


INDUSTRY_DETAIL_COLUMNS = [
    "industry_rank",
    "company_id",
    "company_name",
    "market_cap",
    "revenue",
    "net_profit",
    "revenue_yoy",
    "profit_growth_adjusted",
    "gross_margin",
    "weighted_score",
    "data_coverage",
    "report_period",
    "announcement_date",
    "missing_metrics",
    "source_url",
]
INDUSTRY_DETAIL_LABELS = {
    "industry_rank": "行业排名",
    "company_id": "证券代码",
    "company_name": "公司名称",
    "market_cap": "总市值(亿元)",
    "revenue": "营收(千万元)",
    "net_profit": "净利润(千万元)",
    "revenue_yoy": "营收增长率(%)",
    "profit_growth_adjusted": "利润增长率(%)",
    "gross_margin": "毛利率(%)",
    "weighted_score": "综合得分",
    "data_coverage": "数据完整度",
    "report_period": "报告期",
    "announcement_date": "公告日",
    "missing_metrics": "缺失指标",
    "source_url": "公告原文",
}
def industry_display_frame(frame: pd.DataFrame, sort_by: str | None = None) -> pd.DataFrame:
    # 创新药行业参与排名的利润增长是对称增长率，表格里同步展示同一口径，避免与得分对不上
    prepared = align_profit_growth(frame)
    # 排序放在口径替换之后：按利润增长率排序时用的就是表格里展示的那个值
    prepared = sort_by_metric(prepared, sort_by)
    columns = [column for column in INDUSTRY_DETAIL_COLUMNS if column in prepared.columns]
    shown = prepared[columns].rename(columns=INDUSTRY_DETAIL_LABELS)
    shown = apply_display_units(shown)
    if "公告原文" in shown:
        shown["公告原文"] = shown["公告原文"].fillna("")
    return shown


def _industry_excel_bytes(ranking: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        ranking.to_excel(writer, sheet_name="三级行业排名", index=False)
        model_weight_table().to_excel(writer, sheet_name="模型权重", index=False)
    output.seek(0)
    return output.getvalue()


def render_industry_group(
    group: pd.DataFrame,
    industry_key: str,
    sort_by: str | None = None,
) -> None:
    """渲染单个三级行业的排名卡片（指标、明细表、缺漏提示）。"""
    group = group.sort_values("industry_rank", na_position="last")
    group_name = str(group["industry_l3_name"].iloc[0])
    group_period = pd.to_datetime(group["report_period"], errors="coerce").max()
    group_model = str(group["rank_model_label"].iloc[0])
    missing = group[~group["eligible"]]
    with st.expander(
        f"{group_name}（{industry_key}）· 报告期 {group_period:%Y-%m-%d} · {group_model}",
        expanded=True,
    ):
        summary = st.columns(4)
        summary[0].metric("成份公司", f"{len(group)}")
        summary[1].metric("进入观察库", f"{int(group['in_watchlist'].sum())}")
        summary[2].metric("完整度不足", f"{len(missing)}")
        summary[3].metric(
            "市值合计（亿元）",
            f"{pd.to_numeric(group['market_cap'], errors='coerce').sum() / 1e8:,.0f}",
        )
        if "创新药" in group_model:
            st.caption(
                "该行业用创新药模型排名：利润增长率取对称增长率 "
                "2×(本期−上年同期)/(|本期|+|上年同期|)，"
                "亏损收窄与盈利增长可以同尺度比较，避免上年同期为负时同比失真。"
            )
        st.dataframe(
            industry_display_frame(group, sort_by),
            use_container_width=True,
            hide_index=True,
        )
        low_confidence = group[
            pd.to_numeric(group.get("extraction_confidence"), errors="coerce").lt(2 / 3)
        ]
        if not low_confidence.empty:
            st.caption(
                f"{len(low_confidence)} 条记录提取置信度较低，"
                "应打开原公告人工复核后再使用（见「公告证据」页签）。"
            )
        covered_ids = set(group["company_id"].astype(str))
        group_constituents = constituents_for([industry_key])
        absent = group_constituents[
            ~group_constituents["company_id"].astype(str).isin(covered_ids)
        ]
        if not absent.empty:
            st.caption(
                f"该行业共 {len(group_constituents)} 家成份公司，"
                f"其中 {len(absent)} 家本期未取得定期报告："
                + "、".join(absent["company_name"].astype(str).tolist())
            )


def render_industry_ranking(
    ranking: pd.DataFrame,
    notes: list[str],
    *,
    as_of_date: date,
    order: dict[str, int] | None = None,
    group_titles: dict[str, str] | None = None,
    group_captions: dict[str, str] | None = None,
    sort_by: str | None = None,
    download_label: str = "下载三级行业财报排名",
    download_key: str = "industry_ranking_download",
) -> None:
    """渲染一组三级行业排名。

    `order` 给出行业 ID 的展示顺序；`group_titles` 给出行业 ID 所属主题，
    主题切换处插入分组标题并隔开，`group_captions` 为主题下的一句说明。
    """
    for note in notes:
        st.info(note)
    if ranking.empty:
        st.warning("截止日之前没有可排名的定期报告。")
        return
    groups = list(ranking.groupby("industry_l3_id", sort=True))
    if order:
        groups.sort(key=lambda item: order.get(str(item[0]), len(order) + 1))
    current_group = None
    for industry_key, group in groups:
        title = (group_titles or {}).get(str(industry_key))
        if title and title != current_group:
            if current_group is not None:
                st.divider()
            st.markdown(f"#### {title}")
            caption = (group_captions or {}).get(title)
            if caption:
                st.caption(caption)
            current_group = title
        render_industry_group(group, industry_key, sort_by)
    highest_period = pd.to_datetime(ranking["report_period"], errors="coerce").max()
    st.download_button(
        download_label,
        data=_industry_excel_bytes(ranking),
        file_name=(
            f"industry_ranking_{pd.Timestamp(highest_period):%Y%m%d}"
            f"_asof_{as_of_date:%Y%m%d}.xlsx"
        ),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        key=download_key,
    )


watch_tab, prosperity_tab, liquidity_tab, detail_tab, taxonomy_tab, quality_tab, evidence_tab, method_tab = st.tabs(
    ["观察库", "行业景气度", "流动性分析", "行业详情", "行业目录", "数据质量", "公告证据", "算法说明"]
)

with watch_tab:
    industries = sorted(watchlist["industry_l3_name"].dropna().astype(str).unique())
    watch_controls = st.columns([3, 1])
    selected_industries = watch_controls[0].multiselect("筛选行业", industries, default=[])
    watch_sort_label = watch_controls[1].selectbox(
        "排序依据（从高到低）", list(SORT_METRICS), index=0, key="watch_sort"
    )
    shown_watchlist = watchlist
    if selected_industries:
        shown_watchlist = watchlist[watchlist["industry_l3_name"].isin(selected_industries)]
    st.caption(UNIT_CAPTION)
    st.dataframe(
        display_frame(shown_watchlist, SORT_METRICS[watch_sort_label]),
        use_container_width=True,
        hide_index=True,
    )

with prosperity_tab:
    st.caption(
        "数据来自国家统计局每月公布的规模以上工业分行业增加值同比增长与利润总额同比增长。"
        "景气度只使用官方口径增长率，不做主观评价。每月统计局公布后点击更新即可滚动最近 6 个月。"
    )
    control_columns = st.columns([2, 1, 1])
    prosperity_window = int(control_columns[1].slider("滚动窗口（月）", 3, 12, DEFAULT_WINDOW, 1))
    prosperity_top_n = int(control_columns[2].slider("最景气行业数", 3, 10, DEFAULT_TOP_N, 1))
    if control_columns[0].button("从国家统计局更新月度景气数据", use_container_width=True):
        try:
            with st.spinner("正在从国家统计局检索分行业工业增加值与利润总额数据。"):
                update_history(months=prosperity_window)
            st.success("景气数据已更新。")
        except Exception as exc:
            st.error(f"景气数据更新失败：{exc}")

    history = load_history()
    if history.empty:
        st.info(
            "尚未获取景气数据。点击上方按钮从国家统计局更新，或用 "
            "`python online_cli.py prosperity --months 6` 生成 "
            f"{DEFAULT_HISTORY_PATH}。"
        )
    else:
        months = sorted(history["month"].astype(str).unique())
        st.write(f"本地月度数据：{months[0]} 至 {months[-1]}，共 {len(months)} 个月、{len(history)} 条记录。")
        top_table, full_table = prosperity_board(history, window=prosperity_window, top_n=prosperity_top_n)
        if top_table.empty:
            st.warning("可用数据不足，无法计算景气度。")
        else:
            shown = top_table.rename(
                columns={
                    "stat_industry": "统计行业大类",
                    "prosperity_score": "景气度",
                    "profit_yoy_latest": "利润增长率（最新月）",
                    "profit_yoy_mean": "利润增长率（窗口均值）",
                    "value_added_yoy_latest": "工业增加值增长率（最新月）",
                    "value_added_yoy_mean": "工业增加值增长率（窗口均值）",
                    "profit_momentum": "利润增长动量",
                    "months": "有效月数",
                    "latest_month": "最新月份",
                    "data_note": "数据说明",
                }
            )
            st.dataframe(shown, use_container_width=True, hide_index=True)
            st.caption("单位为同比增长百分数；景气度为 0—100 的综合分，越高越景气。")

            st.subheader("对应申万三级行业与排名靠前公司")
            for row in top_table.itertuples(index=False):
                industries = corresponding_industries(row.stat_industry)
                with st.expander(
                    f"{row.stat_industry}：景气度 {row.prosperity_score}，"
                    f"利润增长 {row.profit_yoy_latest:.1f}%，对应 {len(industries)} 个申万三级行业",
                    expanded=True,
                ):
                    if industries.empty:
                        st.write("该统计行业没有对应的申万三级行业。")
                        continue
                    st.dataframe(
                        industries.rename(
                            columns={
                                "industry_l3_id": "三级行业ID",
                                "industry_l3_name": "三级行业",
                                "industry_l2_name": "二级行业",
                                "industry_l1_name": "一级行业",
                                "company_count": "成分公司数",
                            }
                        )[["三级行业ID", "三级行业", "二级行业", "一级行业", "成分公司数"]],
                        use_container_width=True,
                        hide_index=True,
                    )
                    constituents = constituents_for(industries["industry_l3_id"])
                    if constituents.empty:
                        st.caption(
                            "这些三级行业尚未固化成分公司，可运行 "
                            "`python tools/freeze_sw_constituents.py --top-n 5` 从公开来源补齐。"
                        )
                    else:
                        available = constituents.groupby(["industry_l3_id", "industry_l3_name"]).size()
                        st.caption(
                            f"已固化 {len(available)}/{len(industries)} 个三级行业的全部成分公司，"
                            f"共 {len(constituents)} 家（公开来源快照 {SNAPSHOT_DATE}）。"
                        )
                        labels = {
                            f"{industry_name}（{count} 家）": industry_id
                            for (industry_id, industry_name), count in available.items()
                        }
                        st.markdown(
                            '<div class="constituent-tip">包含多个三级行业，点击下面菜单，可选择某个三级子行业。</div>',
                            unsafe_allow_html=True,
                        )
                        chosen = st.selectbox(
                            "查看某个三级行业的全部成分公司",
                            list(labels),
                            key=f"constituents_{row.stat_industry}",
                        )
                        selected_companies = constituents[
                            constituents["industry_l3_id"].eq(labels[chosen])
                        ]
                        st.dataframe(
                            selected_companies.rename(
                                columns={
                                    "company_id": "公司代码",
                                    "company_name": "公司名称",
                                    "include_date": "纳入时间",
                                }
                            )[["公司代码", "公司名称", "纳入时间"]],
                            use_container_width=True,
                            hide_index=True,
                        )
                    companies = top_companies_for_stat_industry(ranking, row.stat_industry, per_industry=3)
                    if companies.empty:
                        st.caption("当前数据源中没有这些三级行业的公司排名，可先上传数据或同步官方公告。")
                    else:
                        st.dataframe(display_frame(companies), use_container_width=True, hide_index=True)

def render_liquidity_history_chart(overlay: pd.DataFrame, label: str) -> None:
    """指数主线 + 滚动均值虚线 + 80% 分位带。"""
    data = overlay.reset_index()
    x = alt.X("date:T", title=None)
    band = alt.Chart(data).mark_area(opacity=0.18, color="#64748b").encode(
        x=x,
        y=alt.Y("区间下轨(10%分位):Q", title=label),
        y2="区间上轨(90%分位):Q",
    )
    mean_line = alt.Chart(data).mark_line(color="#d97706", strokeDash=[6, 4]).encode(
        x=x, y=alt.Y("滚动均值:Q", title=label)
    )
    index_line = alt.Chart(data).mark_line(color="#2563eb").encode(
        x=x, y=alt.Y(f"{label}:Q", title=label)
    )
    chart = (
        alt.layer(band, mean_line, index_line)
        .resolve_scale(y="shared")
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)


def render_liquidity_block(
    series: pd.Series,
    *,
    label: str,
    compare_window: int,
    monthly: bool,
) -> None:
    """渲染单个流动性指数：指标行、带参考区间的折线图与趋势说明。"""
    if monthly:
        series = series.resample("ME").last().dropna()
    summary = summarize_index(series, window=compare_window)
    if summary is None:
        st.info("该指数还没有足够的历史数据。")
        return
    summary_row = st.columns(4)
    summary_row[0].metric("最新值", f"{summary['latest']:.2f}")
    summary_row[1].metric("较上期", "—" if summary["change"] is None else f"{summary['change']:+.2f}")
    summary_row[2].metric(
        "近一年分位", "—" if summary["percentile"] is None else f"{summary['percentile']:.0%}"
    )
    summary_row[3].metric("数据截至", f"{summary['as_of']:%Y-%m-%d}")
    if monthly:
        window, min_periods = REFERENCE_WINDOW_MONTHS, REFERENCE_MIN_MONTHS
        span_note = "过去 12 个月"
    else:
        window, min_periods = REFERENCE_WINDOW_WEEKS, REFERENCE_MIN_WEEKS
        span_note = "过去 52 周（约一年）"
    overlay = rolling_reference(series, window, min_periods).assign(**{label: series})
    render_liquidity_history_chart(overlay, label)
    st.caption(
        f"趋势：{summary['trend']}。指数是同向化指标的加权 Z 分，0 代表近一年均值水平，越大越宽松。"
        f"灰带为{span_note}的 10%~90% 分位区间（80% 波动范围），虚线为同期滚动均值。"
    )


with liquidity_tab:
    st.caption(
        "流动性指数把指标同向化（宽松=数值变大）后按滚动窗口 Z-score 标准化，再分组合成。"
        "短端只放价格与体量类指标，两融余额同比属增速类，与货币信贷增速同组："
        "DR007 20%、十年期国债收益率 20%、两融余额 6% 合成短端资金价格指数（周度更新）；"
        "M1-M2 剪刀差 18%、M2 同比 12%、社融存量同比 18%、两融余额同比 6% "
        "合成信用货币增速指数（月频更新，两融同比周频）；"
        "流动性指数 = 0.46×短端 + 0.54×信用货币增速。"
        "月度数据按次月 10 日生效，避免使用未公布数据。"
        "社融存量与存量同比从央行「调查统计司-统计数据-社会融资规模」年度栏目自动解析，"
        "与央行公众号月度报告同源。"
    )
    if st.button("从公开数据源更新流动性数据", use_container_width=False, key="liquidity_update"):
        liquidity_progress = st.progress(0.0, text="准备更新")

        def show_liquidity_progress(step: int, total: int, message: str) -> None:
            liquidity_progress.progress(step / max(total, 1), text=message)

        try:
            with st.spinner("正在更新 DR007、国债收益率、两融余额、货币数据与社融存量表，约 1-2 分钟。"):
                liquidity_counts = update_liquidity_cache(progress=show_liquidity_progress)
            liquidity_progress.progress(1.0, text="更新完成")
            st.success(
                "流动性数据已更新："
                + "、".join(f"{METRIC_LABELS[key]} {count} 条" for key, count in liquidity_counts.items())
            )
        except Exception as exc:
            st.error(f"流动性数据更新失败：{exc}")

    liquidity_metrics = load_all_metrics()
    if not liquidity_metrics:
        st.info("本地还没有流动性数据。点击上方按钮联网更新（含央行社融存量与同比）。")
        st.caption(MANUAL_IMPORT_NOTE)
    else:
        (
            liquidity_indices,
            liquidity_components,
            liquidity_notes,
        ) = compute_liquidity_indices(liquidity_metrics)
        for note in liquidity_notes:
            st.warning(note)
        if liquidity_indices.empty:
            st.warning("缓存数据还不足以计算指数，请先更新数据。")
        else:
            st.subheader("流动性指数")
            render_liquidity_block(
                liquidity_indices["liquidity_index"],
                label=INDEX_LABELS["liquidity_index"],
                compare_window=52,
                monthly=False,
            )

            st.divider()
            st.subheader("短端资金价格指数（周度更新）")
            if "short_term_index" in liquidity_indices:
                render_liquidity_block(
                    liquidity_indices["short_term_index"],
                    label=INDEX_LABELS["short_term_index"],
                    compare_window=52,
                    monthly=False,
                )
            short_term_components = liquidity_components[
                liquidity_components["分组"].eq("短端资金价格指数")
            ].drop(columns=["metric", "分组"])
            st.dataframe(short_term_components, use_container_width=True, hide_index=True)
            st.caption("「最新值」为原始单位：DR007 与十年期国债收益率为 %，两融余额为亿元。")

            st.divider()
            st.subheader("信用货币增速指数（月频更新）")
            if "money_index" in liquidity_indices:
                render_liquidity_block(
                    liquidity_indices["money_index"],
                    label=INDEX_LABELS["money_index"],
                    compare_window=12,
                    monthly=True,
                )
            money_components = liquidity_components[
                liquidity_components["分组"].eq("信用货币增速指数")
            ].drop(columns=["metric", "分组"])
            st.dataframe(money_components, use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("原始指标走势")
            st.caption(
                "各指标的原始值曲线（未同向化、未标准化），直接读取本地留存数据，离线可查看；"
                "原始来源链接见下方「数据源留存」表。两融余额同比由两融余额水平派生。"
            )
            for metric_id in METRIC_CHART_ORDER:
                st.markdown(f"**{METRIC_LABELS[metric_id]}**")
                raw_series = liquidity_metrics.get(metric_id)
                if metric_id == "margin_balance_yoy" and (
                    raw_series is None or raw_series.empty
                ):
                    level = liquidity_metrics.get("margin_balance")
                    if level is not None and not level.empty:
                        raw_series = derive_margin_yoy(level)
                if raw_series is None or raw_series.empty:
                    st.info("本地暂无该指标数据，点上方按钮联网更新即可（社融存量与同比自动从央行统计数据栏目抓取）。")
                    continue
                display_series = prepare_metric_display_series(metric_id, raw_series)
                if metric_id == "margin_balance":
                    display_series = display_series / 1e4  # 亿元 → 万亿元（1 万亿 = 10000 亿）
                st.line_chart(display_series, use_container_width=True)
                st.caption(METRIC_CHART_NOTES[metric_id])

            st.divider()
            st.subheader("数据源留存")
            source_rows = []
            for metric_id, info in SOURCE_REGISTRY.items():
                cached = load_cached_metric(metric_id)
                latest_fetch = "—"
                if not cached.empty and cached["retrieved_at"].notna().any():
                    latest_fetch = str(cached["retrieved_at"].dropna().max())[:10]
                source_rows.append(
                    {
                        "指标": METRIC_LABELS[metric_id],
                        "频率": info["frequency"],
                        "来源": info["provider"],
                        "更新方式": info["mode"],
                        "缓存条数": len(cached),
                        "最近抓取": latest_fetch,
                        "来源链接": info["url"],
                    }
                )
            st.dataframe(
                pd.DataFrame(source_rows),
                column_config={"来源链接": st.column_config.LinkColumn("来源链接")},
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "全部原始值按日留存于 data/liquidity/ 下，每条记录带来源链接与抓取时间，可随时回放变化趋势。"
            )
            st.caption(MANUAL_IMPORT_NOTE)

with detail_tab:
    st.caption(
        "以三级行业为组：先取该行业全部成份公司，再从巨潮资讯逐个拉取近一年的正式定期报告，"
        "从中抽取总市值、营收、净利润、增长率与毛利率，最后用现有算法在行业内部排名。"
        "总市值公告原文不提供，由公开行情接口按最新收盘补齐。"
    )
    coverage = industry_coverage()
    if coverage.empty:
        st.warning(
            "尚未固化任何三级行业成份公司。可运行 "
            "`python tools/freeze_sw_constituents.py --all` 从公开来源补齐。"
        )
    else:
        focus_groups = focus_industry_groups()
        focus = focus_industries()
        focus_ids = [item["industry_l3_id"] for item in focus]
        focus_order = {industry_id: position for position, industry_id in enumerate(focus_ids)}
        focus_group_titles = {
            str(member["industry_l3_id"]): str(group["group_name"])
            for group in focus_groups
            for member in group["industries"]  # type: ignore[union-attr]
        }
        focus_group_captions: dict[str, str] = {}
        for group in focus_groups:
            group_name = str(group["group_name"])
            members = list(group["industries"])  # type: ignore[arg-type]
            text = (
                f"{len(members)} 个行业："
                + "、".join(str(member["industry_l3_name"]) for member in members)
                + "。"
            )
            if group_name == "创新药":
                text += (
                    f"这 {len(members)} 个行业跨盈亏频繁，利润增长率取对称增长率 "
                    "2×(本期−上年同期)/(|本期|+|上年同期|)。"
                )
            focus_group_captions[group_name] = text

        header = st.columns([1, 1, 1, 3])
        detail_as_of = header[0].date_input(
            "信息截止日", value=date.today(), max_value=date(2100, 12, 31), key="detail_as_of"
        )
        lookback_days = int(
            header[1].number_input("回看天数", 180, 730, DEFAULT_LOOKBACK_DAYS, 30)
        )
        detail_sort_label = header[2].selectbox(
            "排序依据（从高到低）", list(SORT_METRICS), index=0, key="industry_sort"
        )
        detail_sort_by = SORT_METRICS[detail_sort_label]

        # ---------- 上半部分：景气行业，按主题分组 ----------
        st.subheader("景气行业")
        st.caption(
            f"下列 {len(focus)} 个三级行业已预先拉取近一年财报，各自按最新共同报告期排名，"
            "按主题分组、组内按行业景气度顺次排列。"
            "每个行业内部用同一报告期做横截面比较，不跨行业混排。"
        )
        st.caption(UNIT_CAPTION)
        focus_raw = all_industry_financials[
            all_industry_financials["industry_l3_id"].astype(str).isin(focus_ids)
        ].copy()
        if focus_raw.empty:
            st.info(
                "这些子行业还没有财报缓存。可在命令行执行 "
                f"`python online_cli.py industry sync --industry {focus_ids[0] if focus_ids else ''} "
                f"--as-of {detail_as_of:%Y-%m-%d}` 拉取。"
            )
        else:
            try:
                focus_ranking, focus_notes = rank_industry_financials(
                    focus_raw,
                    as_of_date=detail_as_of,
                    top_n=top_n,
                    minimum_coverage=minimum_coverage,
                )
            except Exception as exc:
                st.error(f"计算行业排名失败：{exc}")
                focus_ranking, focus_notes = pd.DataFrame(), []
            ranked_ids = (
                set(focus_ranking["industry_l3_id"].astype(str))
                if not focus_ranking.empty
                else set()
            )
            unranked = [item for item in focus if item["industry_l3_id"] not in ranked_ids]
            if unranked:
                st.warning(
                    "以下子行业本地还没有可排名的财报数据，可先拉取："
                    + "、".join(item["industry_l3_name"] for item in unranked)
                    + "（命令行示例：`python online_cli.py industry sync --industry "
                    + unranked[0]["industry_l3_id"]
                    + f" --as-of {detail_as_of:%Y-%m-%d}`）"
                )
            render_industry_ranking(
                focus_ranking,
                focus_notes,
                as_of_date=detail_as_of,
                order=focus_order,
                group_titles=focus_group_titles,
                group_captions=focus_group_captions,
                sort_by=detail_sort_by,
                download_label="下载这些景气行业的财报排名",
                download_key="focus_ranking_download",
            )

        st.divider()

        # ---------- 下半部分：其它行业，按需手动拉取 ----------
        st.subheader("其它行业（按需拉取）")
        st.caption(
            "上方未列出的三级行业默认不占用联网额度：选定一个行业后点「拉取近一年财报」"
            "才会去巨潮检索并解析，随后立即在该行业内部排名。"
        )
        sorted_coverage = coverage.sort_values("industry_l3_id").reset_index(drop=True)
        others_coverage = sorted_coverage[
            ~sorted_coverage["industry_l3_id"].astype(str).isin(focus_ids)
        ].reset_index(drop=True)
        industry_labels = {
            f"{row.industry_l3_name}（{row.industry_l3_id}，{row.company_count} 家）": row.industry_l3_id
            for row in others_coverage.itertuples()
        }
        label_list = list(industry_labels)

        with st.container(border=True):
            controls = st.columns([3, 1, 1])
            chosen_label = controls[0].selectbox("三级行业", label_list, index=0)
            detail_industry_id = industry_labels[chosen_label]
            detail_industry_name = str(
                others_coverage.loc[
                    others_coverage["industry_l3_id"].eq(detail_industry_id), "industry_l3_name"
                ].iloc[0]
            )
            detail_constituents = constituents_for([detail_industry_id])
            run_industry_sync = controls[2].button(
                "拉取近一年财报", type="primary", use_container_width=True
            )
            force_resync = st.checkbox(
                "忽略本地缓存，强制重新联网核对", value=False, key="detail_force_resync"
            )
            st.caption(
                f"点上面的按钮会先在本地检查「{detail_industry_name}」的 "
                f"{len(detail_constituents)} 家成份公司是否都已取得最新报告期数据："
                "齐全则不联网并提示，只缺哪几家就只补拉那几家。"
                "勾选上面的复选框可强制重新联网核对（仍不会重复解析已缓存的公告）。"
                f"联网时到巨潮资讯检索 {detail_as_of:%Y-%m-%d} 之前 {lookback_days} 天内披露的"
                "正式定期报告（年报/半年报/季报），下载公告原文抽取营收、净利润、增长率、毛利率，"
                "总市值由公开行情接口补齐，再按现有算法排名。"
            )

        if run_industry_sync:
            should_sync = False
            targets: list[str] | None = None
            if force_resync:
                should_sync = True
            else:
                cache_status = check_industry_cache(
                    detail_industry_id, detail_as_of, lookback_days
                )
                if cache_status.needs_network:
                    st.info(cache_status.message)
                    should_sync = True
                    targets = list(cache_status.pending_ids) or None
                else:
                    st.success(cache_status.message)
                    st.caption(
                        "若怀疑公开渠道已披露更新的定期报告，勾选「忽略本地缓存，"
                        "强制重新联网核对」再点一次。"
                    )
            if should_sync:
                detail_progress = st.progress(0.0, text="准备拉取")

                def show_industry_progress(current: int, total: int, message: str) -> None:
                    detail_progress.progress(current / max(total, 1), text=message)

                try:
                    with st.spinner("正在从巨潮资讯检索并解析正式定期报告，请勿关闭页面。"):
                        synced, industry_errors = sync_industry_reports(
                            industry=detail_industry_id,
                            as_of_date=detail_as_of,
                            lookback_days=lookback_days,
                            progress=show_industry_progress,
                            company_ids=targets or None,
                        )
                    detail_progress.progress(1.0, text="拉取完成")
                    st.success(
                        f"{detail_industry_name}：本地累计 {len(synced)} 条定期报告记录，"
                        f"覆盖 {synced['company_id'].nunique() if not synced.empty else 0} 家公司。"
                    )
                    if not industry_errors.empty:
                        st.warning(f"有 {len(industry_errors)} 条检索或提取失败，请用命令行查看明细。")
                    all_industry_financials = load_all_industry_financials()
                except Exception as exc:
                    st.error(f"拉取失败：{exc}")

        industry_raw = all_industry_financials[
            all_industry_financials["industry_l3_id"].astype(str).eq(str(detail_industry_id))
        ].copy()

        if industry_raw.empty:
            st.info(
                f"{detail_industry_name} 还没有财报缓存。点击上方「拉取近一年财报」，"
                f"或在命令行执行 `python online_cli.py industry sync --industry {detail_industry_id} "
                f"--as-of {detail_as_of:%Y-%m-%d}`。"
            )
        else:
            latest_announcement = pd.to_datetime(
                industry_raw["announcement_date"], errors="coerce"
            ).max()
            st.caption(
                f"本地已缓存 {len(industry_raw)} 条定期报告记录，覆盖 "
                f"{industry_raw['company_id'].nunique()} 家公司，"
                f"最新公告日 {latest_announcement:%Y-%m-%d}。"
                "再次点击「拉取近一年财报」只会补拉新披露的公告，不会重复解析已有公告；"
                "公司较多时耗时较长，建议改用命令行后台执行："
                f"`python online_cli.py industry sync --industry {detail_industry_id} "
                f"--as-of {detail_as_of:%Y-%m-%d}`"
            )
            period_options = available_report_periods(industry_raw, detail_as_of)
            period_choice = st.selectbox(
                "报告期",
                options=["自动（最新共同报告期）", *period_options],
                format_func=lambda value: (
                    "自动（最新共同报告期）"
                    if value == "自动（最新共同报告期）"
                    else pd.Timestamp(value).strftime("%Y-%m-%d")
                ),
                key="detail_period_choice",
            )
            chosen_period = None if period_choice == "自动（最新共同报告期）" else period_choice
            try:
                industry_ranking, industry_notes = rank_industry_financials(
                    industry_raw,
                    as_of_date=detail_as_of,
                    report_period=chosen_period,
                    top_n=top_n,
                    minimum_coverage=minimum_coverage,
                )
            except Exception as exc:
                st.error(f"计算行业排名失败：{exc}")
                industry_ranking = pd.DataFrame()
                industry_notes = []
            render_industry_ranking(
                industry_ranking,
                industry_notes,
                as_of_date=detail_as_of,
                sort_by=detail_sort_by,
                download_label="下载该行业财报排名",
                download_key="detail_ranking_download",
            )

        if not detail_constituents.empty:
            st.subheader(f"行业成分公司（{len(detail_constituents)} 家，公开快照 {SNAPSHOT_DATE}）")
            st.dataframe(
                detail_constituents.rename(
                    columns={"company_id": "公司代码", "company_name": "公司名称", "include_date": "纳入时间"}
                )[["公司代码", "公司名称", "纳入时间"]],
                use_container_width=True,
                hide_index=True,
            )

with taxonomy_tab:
    st.caption(
        f"内置申万三级行业共 {len(taxonomy)} 个，快照日期 {SNAPSHOT_DATE}，"
        f"来源 {SOURCE_URL}。行业名称与官方编码不必完全一致，模糊匹配会自动对齐。"
    )
    shown_taxonomy = matched.copy()
    shown_taxonomy["model_type"] = shown_taxonomy["industry_l3_name"].map(suggest_model_type)
    st.dataframe(
        shown_taxonomy.rename(
            columns={
                "industry_l3_id": "三级行业ID",
                "industry_l3_name": "三级行业",
                "industry_l2_name": "二级行业",
                "industry_l1_name": "一级行业",
                "company_count": "成分公司数",
                "match_score": "匹配度",
            }
        )[
            [
                "三级行业ID",
                "三级行业",
                "二级行业",
                "一级行业",
                "成分公司数",
                "model_type",
                "匹配度",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

with quality_tab:
    ineligible = ranking[~ranking["eligible"]]
    st.write(f"因数据完整度低于 {minimum_coverage:.0%} 而未参与排名：{len(ineligible)} 家。")
    if not ineligible.empty:
        st.dataframe(display_frame(ineligible), use_container_width=True, hide_index=True)
    st.caption("缺失指标按 50 分中性值计入，并乘以完整度惩罚；达到最低完整度后才有行业名次。")

with evidence_tab:
    evidence_columns = [
        column
        for column in [
            "company_id",
            "company_name",
            "report_period",
            "announcement_date",
            "source_title",
            "source_url",
            "extraction_confidence",
            "needs_review",
        ]
        if column in ranking.columns
    ]
    if "source_url" not in evidence_columns:
        st.info("演示或上传数据未包含公告来源；在线公告缓存会显示可追溯链接。")
    else:
        evidence = ranking[evidence_columns].copy()
        if "extraction_confidence" in evidence:
            evidence["extraction_confidence"] = (evidence["extraction_confidence"] * 100).round(1)
        st.dataframe(evidence, use_container_width=True, hide_index=True)
        review_count = int(ranking.get("needs_review", pd.Series(False, index=ranking.index)).fillna(False).sum())
        if review_count:
            st.warning(f"有 {review_count} 条记录提取置信度较低，应打开原公告人工复核后再使用。")

    if not all_industry_financials.empty:
        st.subheader("三级行业财报公告原文")
        industry_evidence = all_industry_financials[
            [
                column
                for column in [
                    "industry_l3_name",
                    "company_id",
                    "company_name",
                    "report_period",
                    "announcement_date",
                    "source_title",
                    "source_url",
                    "extraction_confidence",
                    "needs_review",
                ]
                if column in all_industry_financials.columns
            ]
        ].copy()
        if "extraction_confidence" in industry_evidence:
            industry_evidence["extraction_confidence"] = (
                industry_evidence["extraction_confidence"] * 100
            ).round(1)
        st.dataframe(
            industry_evidence.sort_values(["industry_l3_name", "company_id", "report_period"]),
            use_container_width=True,
            hide_index=True,
        )
        st.caption("公告原文均来自巨潮资讯正式披露，链接可直接点开核对。")

with method_tab:
    st.markdown(
        """
**通用模型**：营收增长 12.5%、修正利润增长 12.5%、毛利率 25%、市值 10%、营收 20%、净利润 20%。

每项指标只与同一三级行业、同一模型的公司比较，转换成 0—100 的横截面百分位分。综合分为：

`原始分 = Σ(指标权重 × 指标百分位分)`

`最终分 = 原始分 × (0.8 + 0.2 × 数据完整度)`

利润上年同期为正时使用普通同比；上年同期不为正时使用对称增长率，避免亏损转盈利时出现无穷大。固定万得目录会按行业特征选择通用、成长、质量、周期、规模、银行、证券或保险模型。银行、证券和保险不使用毛利率，改用各自的盈利、风险、资本充足和估值指标。

在线数据来自巨潮资讯正式定期报告。为保证横向可比性，排名使用同一报告期，而不是把不同公司的年报、半年报和季报混在一起；截止日之后披露或修订的数据不会进入历史排名。

「行业详情」页签按三级行业分组：取该行业全部成份公司，拉取近一年正式定期报告，
从合并利润表抽取营收、净利润、增长率与毛利率，总市值由公开行情接口补齐（公告原文不提供市值），
再在每个三级行业内部各自按最新共同报告期转成百分位分。
"""
    )
    weights = model_weight_table().copy()
    weights["weight"] = (weights["weight"] * 100).round(1).astype(str) + "%"
    st.dataframe(weights, use_container_width=True, hide_index=True)

excel_bytes = dataframe_to_excel_bytes(ranking, watchlist, model_weight_table())
st.download_button(
    "下载本期观察库与完整排名",
    data=excel_bytes,
    file_name=f"industry_watchlist_{pd.Timestamp(report_period):%Y%m%d}_asof_{as_of:%Y%m%d}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)
