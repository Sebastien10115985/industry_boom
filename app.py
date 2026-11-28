from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from html import escape
from io import BytesIO
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from streamlit.components.v1 import html as components_html

from financing_dividend_provider import attach_financing_dividend
from data_io import (
    dataframe_to_excel_bytes,
    normalize_input,
)
from disclosure_pipeline import DEFAULT_DATA_PATH, load_online_financials
from industry_catalog import suggest_model_type
from industry_match import builtin_industry_frame
from industry_pipeline import (
    DEFAULT_LOOKBACK_DAYS,
    DETAIL_SECTOR_TABS,
    available_report_periods,
    check_industry_cache,
    focus_industry_groups,
    focus_industries,
    l2_focus_l3_map,
    l2_focus_names,
    l2_group_l3_ids,
    load_all_industry_financials,
    rank_industry_financials,
    sync_industry_reports,
)
from industry_match import (
    SOURCE_URL,
    SNAPSHOT_DATE,
    industry_options,
    search_industries,
)
from liquidity import (
    INDEX_LABELS,
    METRICS,
    REFERENCE_MIN_MONTHS,
    REFERENCE_MIN_WEEKS,
    REFERENCE_WINDOW_MONTHS,
    REFERENCE_WINDOW_WEEKS,
    compute_liquidity_indices,
    derive_margin_yoy,
    effective_weight,
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
from customs_provider import (
    CUSTOMS_GROUPS,
    CustomsError,
    WIND_INDUSTRY_MAP,
    import_customs_article,
    load_customs,
    update_customs,
)
from cffex_provider import CffexError, load_if_daily, update_if_daily
from macro_risk import (
    DEFAULT_RISK_VALUES,
    add_comment,
    delete_comment,
    feature_comment,
    load_comments,
    load_regime,
    load_votes,
    new_session_id,
    vote_risk,
)
from bull_bear.config import load_config as load_regime_config, ruler_bands
from bull_bear.scoring import STATE_LABELS
from regime_provider import compute_or_cached_report, load_cached_report
from prosperity import (
    DEFAULT_TOP_N,
    DEFAULT_WINDOW,
    PRIMARY_STAT_INDUSTRIES,
    corresponding_industries,
    prosperity_board,
    prosperity_for_industries,
    prosperity_history,
    top_companies_for_industry_ids,
)
from admin_auth import verify_admin_key
from prosperity_mapping import mapped_industry_ids
from ranking import MODEL_SPECS, model_weight_table, prepare_growth_metrics, score_snapshot, select_snapshot
from sw_constituents import SNAPSHOT_DATE, constituents_for, industry_coverage

st.set_page_config(page_title="宏观与行业景气动态观察看板", page_icon=None, layout="wide")
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
WECHAT_QR_PATH = Path(__file__).resolve().parent / "data" / "开发者微信.png"

# ---- 行业详情各 tab 的自定义评分权重 ----
MODEL_WEIGHTS_FILE = Path(__file__).resolve().parent / "data" / "model_weights.json"
MODEL_WEIGHT_SLIDER_MAX = 0.60

MODEL_METRIC_LABELS: dict[str, str] = {
    "revenue_yoy": "营收增长",
    "profit_growth_adjusted": "修正利润增长",
    "profit_growth_symmetric": "利润增长（对称）",
    "gross_margin": "毛利率",
    "market_cap": "市值",
    "revenue": "营收",
    "net_profit": "净利润",
    "roe": "净资产收益率",
    "net_interest_margin": "净息差",
    "npl_ratio": "不良贷款率",
    "provision_coverage": "拨备覆盖率",
    "cet1_ratio": "核心一级资本充足率",
    "cost_income_ratio": "成本收入比",
    "pb": "市净率",
    "net_capital_ratio": "净资本比率",
    "risk_coverage_ratio": "风险覆盖率",
    "operating_margin": "营业利润率",
    "premium_growth": "保费增长",
    "solvency_ratio": "偿付能力充足率",
    "combined_ratio": "综合成本率",
    "investment_yield": "投资收益率",
}


# 「自定义评分权重」/「自定义指标权重」折叠区的强调样式：琥珀描边 + 悬浮阴影 + 加粗标题，暗色主题自动适配
CUSTOM_WEIGHT_EXPANDER_CSS = """
<style>
div[class*="st-key-mw_box_"] details,
div[class*="st-key-liq_weight_box"] details {
    border: 2px solid #f59e0b !important;
    border-radius: 12px !important;
    background: linear-gradient(180deg, #fffaf0 0%, #ffffff 85%) !important;
    box-shadow: 0 2px 6px rgba(180, 83, 9, 0.12) !important;
    transition: border-color .15s ease, box-shadow .15s ease;
}
div[class*="st-key-mw_box_"] details:hover,
div[class*="st-key-liq_weight_box"] details:hover {
    border-color: #d97706 !important;
    box-shadow: 0 4px 14px rgba(217, 119, 6, 0.28) !important;
}
div[class*="st-key-mw_box_"] summary,
div[class*="st-key-mw_box_"] summary p,
div[class*="st-key-liq_weight_box"] summary,
div[class*="st-key-liq_weight_box"] summary p {
    font-weight: 700 !important;
    color: #b45309 !important;
}
div[class*="st-key-mw_box_"] summary:hover,
div[class*="st-key-mw_box_"] summary:hover p,
div[class*="st-key-liq_weight_box"] summary:hover,
div[class*="st-key-liq_weight_box"] summary:hover p {
    color: #92400e !important;
}
div[class*="st-key-mw_box_"] summary svg,
div[class*="st-key-liq_weight_box"] summary svg {
    color: #d97706 !important;
}
@media (prefers-color-scheme: dark) {
    div[class*="st-key-mw_box_"] details,
    div[class*="st-key-liq_weight_box"] details {
        background: linear-gradient(180deg, rgba(245,158,11,.16) 0%, rgba(245,158,11,.04) 85%) !important;
        border-color: #fbbf24 !important;
    }
    div[class*="st-key-mw_box_"] summary,
    div[class*="st-key-mw_box_"] summary p,
    div[class*="st-key-liq_weight_box"] summary,
    div[class*="st-key-liq_weight_box"] summary p {
        color: #fbbf24 !important;
    }
    div[class*="st-key-mw_box_"] summary svg,
    div[class*="st-key-liq_weight_box"] summary svg {
        color: #fbbf24 !important;
    }
}
</style>
"""


def _load_model_weights() -> dict[str, dict[str, dict[str, float]]]:
    """自定义评分权重：{tab 名: {模型: {指标: 权重}}}，按 tab 隔离。"""
    try:
        payload = json.loads(MODEL_WEIGHTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    store: dict[str, dict[str, dict[str, float]]] = {}
    for tab_name, models in payload.items():
        if not isinstance(models, dict):
            continue
        cleaned: dict[str, dict[str, float]] = {}
        for model, metrics in models.items():
            if model not in MODEL_SPECS or not isinstance(metrics, dict):
                continue
            cleaned[str(model)] = {
                metric: float(value)
                for metric, value in metrics.items()
                if metric in MODEL_SPECS[model].metrics and value is not None
            }
        if cleaned:
            store[str(tab_name)] = cleaned
    return store


def _persist_model_weights(store: dict[str, dict[str, dict[str, float]]]) -> None:
    if not store:
        MODEL_WEIGHTS_FILE.unlink(missing_ok=True)
        return
    MODEL_WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODEL_WEIGHTS_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _model_weight_key(sector_title: str, model: str, metric: str) -> str:
    return f"mw_{sector_title}_{model}_{metric}"


def _save_tab_model_weights(sector_title: str, model: str) -> None:
    """滑块 on_change：把该 tab 该模型的权重写盘，全部等于默认时清除该模型覆盖。"""
    spec = MODEL_SPECS[model]
    weights = {
        metric: float(st.session_state[_model_weight_key(sector_title, model, metric)])
        for metric in spec.metrics
    }
    defaults = {metric: float(value[0]) for metric, value in spec.metrics.items()}
    store = _load_model_weights()
    if all(abs(weights[metric] - defaults[metric]) < 1e-9 for metric in weights):
        store.get(sector_title, {}).pop(model, None)
        if not store.get(sector_title):
            store.pop(sector_title, None)
    else:
        store.setdefault(sector_title, {})[model] = weights
    _persist_model_weights(store)


def _reset_tab_model_weights(sector_title: str, models: list[str]) -> None:
    """恢复本 tab 默认权重：清文件条目并把滑块 session_state 重置为默认值。"""
    store = _load_model_weights()
    store.pop(sector_title, None)
    _persist_model_weights(store)
    for model in models:
        for metric, (weight, _) in MODEL_SPECS[str(model)].metrics.items():
            st.session_state[_model_weight_key(sector_title, str(model), metric)] = float(weight)


def _latest_data_date() -> str:
    """本地数据缓存的最新更新日期（流动性/景气/财报缓存文件中最新的修改日期）。"""
    candidates: list[Path] = []
    for folder in ("data/liquidity", "data/industries", "data"):
        directory = Path(__file__).resolve().parent / folder
        if directory.is_dir():
            candidates.extend(path for path in directory.glob("*.csv") if path.is_file())
    if not candidates:
        return ""
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return date.fromtimestamp(newest.stat().st_mtime).isoformat()


# ---- 访问统计：访客按 IP/设备哈希去重（UV），会话次数保留（PV），JSON 文件存储 ----
VISIT_STATS_PATH = Path(__file__).resolve().parent / "data" / "visit_stats.json"
VISIT_HISTORY_DAYS = 90


def _visitor_hash() -> str:
    """访客指纹：代理转发的真实 IP + User-Agent + 语言偏好，SHA256 截断 16 位。

    Streamlit Cloud 等部署在反向代理后，真实 IP 在 X-Forwarded-For 首段。
    拿不到请求头（本地裸跑/测试环境）时返回空串，调用方按单访客处理。
    """
    try:
        headers = st.context.headers
    except Exception:
        return ""
    forwarded = str(headers.get("X-Forwarded-For", "") or "")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else str(headers.get("X-Real-Ip", "") or "").strip()
    )
    ua = str(headers.get("User-Agent", "") or "")
    lang = str(headers.get("Accept-Language", "") or "")
    raw = f"{ip}|{ua}|{lang}".strip("|")
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_daily(daily: dict) -> dict[str, dict]:
    """把每日明细统一为 {day: {"visitors": [...], "visits": n}} 结构。"""
    out: dict[str, dict] = {}
    for day, value in (daily or {}).items():
        if isinstance(value, dict):
            out[str(day)] = {
                "visitors": [str(v) for v in (value.get("visitors") or [])],
                "visits": int(value.get("visits") or 0),
            }
        else:
            out[str(day)] = {"visitors": [], "visits": int(value or 0)}
    return out


def _load_visit_stats() -> dict:
    try:
        raw = json.loads(VISIT_STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"visitors": [], "daily": {}}
    if isinstance(raw, dict) and isinstance(raw.get("visitors"), list):
        return {
            "visitors": [str(v) for v in raw["visitors"]],
            "daily": _normalize_daily(raw.get("daily") or {}),
        }
    # 旧版格式（{"total": n, "daily": {day: count}}）：PV 保留并入每日次数，UV 重新统计
    if isinstance(raw, dict):
        return {"visitors": [], "daily": _normalize_daily(raw.get("daily") or {})}
    return {"visitors": [], "daily": {}}


def _record_visit() -> dict[str, int]:
    """每个浏览器会话记一次访问：当日次数 +1（PV），访客哈希按 UV 去重。"""
    today = date.today().isoformat()
    if st.session_state.get("visit_counted"):
        stats = _load_visit_stats()
        day = stats["daily"].get(today) or {"visitors": [], "visits": 0}
        return {
            "visitors": len(stats["visitors"]),
            "today_visitors": len(day["visitors"]),
            "today_visits": int(day["visits"]),
        }
    st.session_state["visit_counted"] = True
    stats = _load_visit_stats()
    visitor_id = _visitor_hash() or "local"
    visitors = list(stats["visitors"])
    if visitor_id not in visitors:
        visitors.append(visitor_id)
    daily = stats["daily"]
    day = daily.setdefault(today, {"visitors": [], "visits": 0})
    if visitor_id not in day["visitors"]:
        day["visitors"].append(visitor_id)
    day["visits"] = int(day["visits"]) + 1
    # 只保留最近一段时间的每日明细，避免文件无限增长
    if len(daily) > VISIT_HISTORY_DAYS:
        for old_day in sorted(daily)[: len(daily) - VISIT_HISTORY_DAYS]:
            daily.pop(old_day, None)
    payload = {"visitors": visitors, "daily": daily}
    VISIT_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = VISIT_STATS_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp_path.replace(VISIT_STATS_PATH)  # 原子替换，避免并发写坏文件
    return {
        "visitors": len(visitors),
        "today_visitors": len(day["visitors"]),
        "today_visits": int(day["visits"]),
    }

# ---- 免责声明：首次打开必须确认，页面常驻醒目提示条 ----
DISCLAIMER_TEXT = (
    "本应用基于国家统计局公布的规模以上工业生产状况信息、海关统计月报、上市公司公开披露的财报，进行数据整理与可视化展示。\n"
    "相关数据存在发布延迟、缺失或统计口径差异，图表与排名不代表对未来走势的判断。仅供研究与学习交流之用。\n"
    "不构成任何投资建议、收益承诺或买卖依据。股市风险大，投资需谨慎。收益和风险自担。\n"
    "本站点是宏观经济与行业数据观察工具，纯数据展示，无营销、无资金、无中奖领取、无诈骗内容。"
)

RISK_BANNER_HTML = (
    '<div style="margin-top:6px; padding:10px 14px; background:#fff8e6; '
    'width:fit-content; max-width:100%; '
    'border-left:4px solid #f59e0b; border-radius:6px; font-size:13px; '
    'line-height:1.8; color:#7a4d00; font-weight:600;">'
    '<span style="color:#b45309; font-weight:800; margin-right:8px;">&#9888; 免责声明</span>'
    + DISCLAIMER_TEXT.replace("\n", "<br>")
    + "</div>"
)


def _require_disclaimer() -> None:
    """首次打开应用时先渲染声明页，点击「同意」后本会话内不再提示。"""
    with st.container(border=True):
        st.subheader("免责声明与使用条款")
        st.markdown(DISCLAIMER_TEXT.replace("\n", "  \n"))
        st.caption("点击「同意」表示您已阅读、理解并接受上述内容，方可进入使用本应用。")
        if st.button("同意并继续", type="primary", width="stretch"):
            st.session_state["disclaimer_agreed"] = True
            st.rerun()


if not st.session_state.get("disclaimer_agreed"):
    _require_disclaimer()
    st.stop()

# ---- 功能开关：宏观风险投票板块暂时隐藏（改为 True 恢复显示） ----
SHOW_MACRO_VOTE = False

# ---- 显示模式：录屏演示时隐藏证券代码并把公司名称替换为示例企业，防止录屏视频违规 ----
DISPLAY_MODE_FILE = Path(__file__).resolve().parent / "data" / "display_mode.json"
DISPLAY_MODE_LABELS = {
    "normal": "正常显示",
    "alias": "演示：名称替换为示例企业",
}
LABEL_TO_MODE = {label: mode for mode, label in DISPLAY_MODE_LABELS.items()}


def _load_display_mode() -> str:
    try:
        # utf-8-sig：兼容手工编辑工具写入的 BOM 头
        mode = json.loads(DISPLAY_MODE_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return "normal"
    return mode if mode in DISPLAY_MODE_LABELS else "normal"


def _persist_display_mode(mode: str) -> None:
    if mode == "normal":
        DISPLAY_MODE_FILE.unlink(missing_ok=True)
        return
    DISPLAY_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DISPLAY_MODE_FILE.write_text(json.dumps(mode, ensure_ascii=False), encoding="utf-8")


if "display_mode_label" not in st.session_state:
    st.session_state["display_mode_label"] = DISPLAY_MODE_LABELS[_load_display_mode()]
display_mode = LABEL_TO_MODE[st.session_state["display_mode_label"]]

# 公司匿名映射：按 company_id 全集排序稳定编号，跨会话、跨表格保持一致
COMPANY_ALIASES: dict[str, str] = {}
NAME_TO_ALIAS: dict[str, str] = {}


def _build_company_aliases(frames: list[pd.DataFrame]) -> None:
    """演示模式下为全部公司生成「示例企业N」别名。"""
    global COMPANY_ALIASES, NAME_TO_ALIAS
    COMPANY_ALIASES, NAME_TO_ALIAS = {}, {}
    if display_mode == "normal":
        return
    ids: set[str] = set()
    names: dict[str, str] = {}
    for frame in frames:
        if frame is None or frame.empty or "company_id" not in frame.columns:
            continue
        pairs = frame[["company_id", "company_name"]].dropna(subset=["company_id"])
        for company_id, name in pairs.itertuples(index=False):
            key = str(company_id)
            ids.add(key)
            names.setdefault(key, str(name) if pd.notna(name) else "")
    for index, company_id in enumerate(sorted(ids), 1):
        COMPANY_ALIASES[company_id] = f"示例企业{index}"
        if names.get(company_id):
            NAME_TO_ALIAS[names[company_id]] = COMPANY_ALIASES[company_id]


def _demo_mask_text(text: str) -> str:
    """把文本里出现的真实公司名替换为演示占位（公告标题等自由文本用）。"""
    for name, alias in NAME_TO_ALIAS.items():
        if name and name in text:
            text = text.replace(name, alias)
    return text


def _demo_mask_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """数据层脱敏：公司名统一替换、公告标题同步脱敏、公告原文链接（含证券代码）删除。

    company_id 保留原值：内部去重、分组、组件 key 都依赖它，展示层再隐藏。
    """
    if display_mode == "normal" or frame is None or frame.empty:
        return frame
    out = frame.copy()
    if "company_name" in out.columns and "company_id" in out.columns:
        out["company_name"] = (
            out["company_id"].astype(str).map(COMPANY_ALIASES).fillna(out["company_name"])
        )
    if "source_title" in out.columns:
        out["source_title"] = out["source_title"].astype(str).map(_demo_mask_text)
    if "source_url" in out.columns:
        out = out.drop(columns=["source_url"])
    return out


def _demo_visible_columns(columns: list[str]) -> list[str]:
    """显示层列过滤：演示模式下隐藏证券代码列。"""
    if display_mode == "normal":
        return columns
    return [column for column in columns if column != "company_id"]


def _demo_constituents_for(industry_ids) -> pd.DataFrame:
    """成分公司表脱敏后的读取入口（内部逻辑仍用未掩码的 company_id）。"""
    return _demo_mask_frame(constituents_for(industry_ids))


def _demo_visible_labels(labels: list[str]) -> list[str]:
    """显示层列过滤（中文列名版）：演示模式隐藏证券代码/公司代码列。"""
    if display_mode == "normal":
        return labels
    hidden_labels = {"公司代码", "证券代码"}
    return [column for column in labels if column not in hidden_labels]


AD_CARD_CSS = """
<style>
:root { --ad-shift: -140px; --ad-y-up: 0px; --ad-y-down: 0px; }
@keyframes ad-card-float {
  0%   { transform: translate(0px, 0px) rotate(0deg); }
  23%  { transform: translate(calc(var(--ad-shift) * 0.5), calc(var(--ad-y-up) * -0.6)) rotate(-0.5deg); }
  46%  { transform: translate(var(--ad-shift), calc(var(--ad-y-up) * -0.25)) rotate(0.4deg); }
  50%  { transform: translate(var(--ad-shift), calc(var(--ad-y-down) * 0.25)) rotate(-0.3deg); }
  54%  { transform: translate(var(--ad-shift), calc(var(--ad-y-down) * 0.3)) rotate(0.3deg); }
  77%  { transform: translate(calc(var(--ad-shift) * 0.5), calc(var(--ad-y-down) * 0.6)) rotate(0.5deg); }
  100% { transform: translate(0px, 0px) rotate(0deg); }
}
div[class*="st-key-ad_card"] {
  width: max-content !important;
  min-width: 180px !important;
  max-width: 210px !important;
  box-sizing: border-box !important;
  margin-left: auto !important;
  margin-right: auto !important;
  animation: ad-card-float 22s linear infinite;
  will-change: transform;
}
div[class*="st-key-ad_card"]:hover { animation-play-state: paused; }
div[class*="st-key-ad_card"] img {
  display: block;
  margin: 0 auto 12px;
  border-radius: 8px;
  border: 1px solid #dcdfe4;
}
</style>
"""
st.markdown(AD_CARD_CSS, unsafe_allow_html=True)

AD_FIT_JS = """
<script>
(function () {
  var doc = window.parent.document;
  var GAP = 20;
  var Y_MAX = 320;
  function fit() {
    var card = doc.querySelector('[class*="st-key-ad_card"]');
    var banner = null;
    var divs = doc.querySelectorAll('div');
    for (var i = 0; i < divs.length; i++) {
      var d = divs[i];
      if (d.style && d.style.width === 'fit-content' && d.innerText &&
          d.innerText.indexOf('免责声明') >= 0) { banner = d; break; }
    }
    var title = doc.querySelector('h1');
    if (!card || !banner || !title) { return; }
    var cs = getComputedStyle(card);
    var matrix = new DOMMatrixReadOnly(cs.transform === 'none' ? '' : cs.transform);
    var cr = card.getBoundingClientRect();
    var cardLeft = cr.left - matrix.m41;
    var cardTop = cr.top - matrix.m42;
    var bannerRight = banner.getBoundingClientRect().right;
    var titleTop = title.getBoundingClientRect().top;
    var shift = cardLeft - bannerRight - GAP;
    if (shift < 0) { shift = 0; }
    var up = cardTop - titleTop;
    var down = banner.getBoundingClientRect().bottom - (cardTop + cr.height);
    if (up < 0) { up = 0; } if (up > Y_MAX) { up = Y_MAX; }
    if (down < 0) { down = 0; } if (down > Y_MAX) { down = Y_MAX; }
    doc.documentElement.style.setProperty('--ad-shift', '-' + Math.round(shift) + 'px');
    doc.documentElement.style.setProperty('--ad-y-up', Math.round(up) + 'px');
    doc.documentElement.style.setProperty('--ad-y-down', Math.round(down) + 'px');
  }
  window.parent.addEventListener('resize', fit);
  setInterval(fit, 2000);
  setTimeout(fit, 200);
  setTimeout(fit, 1200);
  setTimeout(fit, 3000);
})();
</script>
"""

_title_col, _ad_col = st.columns([5, 1], vertical_alignment="center")
with _title_col:
    st.title("宏观与行业景气动态观察看板")
    st.caption("A 股牛熊估算｜行业景气度｜海关进出口｜市场流动性跟踪")
    st.markdown(RISK_BANNER_HTML, unsafe_allow_html=True)
with _ad_col:
    if WECHAT_QR_PATH.exists():
        with st.container(key="ad_card", border=True):
            st.markdown(
                '<div style="text-align:center;font-size:13px;font-weight:700;color:#475569;line-height:1.5;white-space:nowrap;">'
                "广告招租，请联系开发者</div>",
                unsafe_allow_html=True,
            )
            qr_data = base64.b64encode(WECHAT_QR_PATH.read_bytes()).decode("ascii")
            st.markdown(
                '<div style="display:flex;justify-content:center;width:100%;">'
                f'<img src="data:image/png;base64,{qr_data}" '
                'style="width:132px;max-width:100%;display:block;border-radius:8px;border:1px solid #dcdfe4;">'
                "</div>",
                unsafe_allow_html=True,
            )

components_html(AD_FIT_JS, height=0)

with st.sidebar:

    # ---- 管理员登录：显示模式（演示/正常切换）仅管理员可见可用 ----
    st.subheader("管理员")
    is_admin = bool(st.session_state.get("is_admin", False))
    if is_admin:
        st.success("管理员已登录。")
        if st.button("退出管理员", key="admin_logout", width="stretch"):
            st.session_state["is_admin"] = False
            st.rerun()
    else:
        admin_key_input = st.text_input("管理员密钥", type="password", key="admin_key_input")
        if st.button("管理员登录", key="admin_login", width="stretch", type="primary"):
            if verify_admin_key(admin_key_input):
                st.session_state["is_admin"] = True
                st.rerun()
            else:
                st.error("密钥不正确。")

    st.header("数据与参数")
    # 显示模式：录屏/演示时隐藏公司身份信息，防止视频违规（仅管理员）
    if is_admin:
        st.radio(
            "显示模式",
            list(DISPLAY_MODE_LABELS.values()),
            key="display_mode_label",
            on_change=lambda: _persist_display_mode(
                LABEL_TO_MODE[st.session_state["display_mode_label"]]
            ),
            help=(
                "录屏防违规：演示模式会隐藏证券代码，并把公司名称替换为示例企业，"
                "公告原文链接一并隐藏；设置自动保存，刷新后仍生效。"
            ),
        )
    top_n = int(st.number_input("每行业观察数量", min_value=1, max_value=100, value=20, step=1))
    minimum_coverage = float(
        st.slider("最低数据完整度", min_value=0.0, max_value=1.0, value=0.60, step=0.05)
    )
    st.caption("数据源：巨潮资讯正式定期报告的本地提取缓存，不含演示数据。")
    st.divider()
    taxonomy = industry_options()
    st.caption(f"内置申万三级行业 {len(taxonomy)} 个（快照 {SNAPSHOT_DATE}）。")

try:
    raw_data = load_online_financials(DEFAULT_DATA_PATH)
    if raw_data.empty:
        # 官方公告缓存为空时，改用三级行业财报缓存（真实公告抽取结果）
        raw_data = load_all_industry_financials()
    if raw_data.empty:
        raise ValueError(
            "本地还没有正式定期报告数据。请先在「行业详情」页签拉取近一年财报。"
        )

    normalized = normalize_input(raw_data)
    # 演示模式：别名要覆盖全部公司，先加载行业财报全量缓存再统一脱敏
    all_industry_financials = load_all_industry_financials()
    _build_company_aliases([normalized, all_industry_financials])
    normalized = _demo_mask_frame(normalized)
    all_industry_financials = _demo_mask_frame(all_industry_financials)
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

# 侧栏最底部：先报告日期，再访问统计（必须放在所有侧栏控件之后，保证排在最下面）
st.sidebar.divider()
_data_date = _latest_data_date()
if _data_date:
    st.sidebar.caption(f"数据更新至 {_data_date}")
visit_stats = _record_visit()
st.sidebar.caption(
    f"累计访客 {visit_stats['visitors']} · 今日访客 {visit_stats['today_visitors']}"
    f" · 今日访问 {visit_stats['today_visits']} 次"
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

if ranking.empty:
    st.warning("当前截止日与报告期组合没有可排名公司。")
    st.stop()

all_industry_financials = _demo_mask_frame(all_industry_financials)

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

# 流动性原始曲线的展示顺序：先短期资金价格与市场风偏三项，再信用货币长期利率与增速四项
METRIC_CHART_ORDER: tuple[str, ...] = (
    "dr007",
    "margin_balance",
    "margin_balance_yoy",
    "bond_10y",
    "m1_m2_gap",
    "m2_yoy",
    "social_financing_stock",
    "social_financing_yoy",
)

METRIC_CHART_NOTES: dict[str, str] = {
    "dr007": (
        "单位 %。银行间存款类机构 7 天回购利率（FDR007 定盘价），"
        "季末、年末等跨期时点常见脉冲式冲高。越高越紧。"
    ),
    "bond_10y": (
        "单位 %。中债十年期国债到期收益率，反映长端无风险利率水平，"
        "参与信用货币长期利率与增速指数合成。越高越紧。"
    ),
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
        "与公众号月度报告同源，参与信用货币长期利率与增速指数合成。越高越宽松。"
    ),
    "margin_balance_yoy": (
        "单位 %。两融余额相对 52 周前的同比增速（由两融余额水平派生），"
        "参与短期资金价格与市场风偏指数合成。越高越宽松。"
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
    for column in ("市值(亿元)", "总市值(亿元)", "历史融资(亿元)", "累计分红(亿元)"):
        if column in shown:
            shown[column] = (pd.to_numeric(shown[column], errors="coerce") / 1e8).round(2)
    if "融资分红比" in shown:
        shown["融资分红比"] = pd.to_numeric(shown["融资分红比"], errors="coerce").round(2)
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
    columns = _demo_visible_columns(
        [column for column in DISPLAY_COLUMNS if column in prepared.columns]
    )
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
    "financing_total",
    "dividend_total",
    "dividend_funding_ratio",
    "listing_date",
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
    "financing_total": "历史融资(亿元)",
    "dividend_total": "累计分红(亿元)",
    "dividend_funding_ratio": "分红融资比",
    "listing_date": "上市日期",
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
    columns = _demo_visible_columns(
        [column for column in INDUSTRY_DETAIL_COLUMNS if column in prepared.columns]
    )
    shown = prepared[columns].rename(columns=INDUSTRY_DETAIL_LABELS)
    shown = apply_display_units(shown)
    if "上市日期" in shown:
        shown["上市日期"] = shown["上市日期"].fillna("").astype(str).str.slice(0, 10)
    if "公告原文" in shown:
        shown["公告原文"] = shown["公告原文"].fillna("")
    return shown


def _low_return_grey_mask(group: pd.DataFrame, index) -> pd.Series:
    """上市满 10 年且分红融资比 < 1 的公司行（整行灰化提示分红回报不足）。"""
    ratio = pd.to_numeric(group.get("dividend_funding_ratio"), errors="coerce")
    listing = pd.to_datetime(group.get("listing_date"), errors="coerce")
    ten_years_ago = pd.Timestamp.today() - pd.DateOffset(years=10)
    grey = listing.notna() & listing.lt(ten_years_ago) & ratio.lt(1)
    return grey.reindex(index).fillna(False)


def _grey_low_payers(shown: pd.DataFrame, group: pd.DataFrame):
    """给需要灰化的行整行加灰色文字样式，其余保持原样。"""
    grey = _low_return_grey_mask(group, shown.index)
    if not grey.any():
        return shown

    def _style(row: pd.Series) -> list[str]:
        if bool(grey.loc[row.name]):
            return ["color: #9e9e9e;"] * len(row)
        return [""] * len(row)

    return shown.style.apply(_style, axis=1)


def _industry_excel_bytes(ranking: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        ranking.to_excel(writer, sheet_name="三级行业排名", index=False)
        model_weight_table().to_excel(writer, sheet_name="模型权重", index=False)
    output.seek(0)
    return output.getvalue()


# ---- 历史趋势：企业财务多期曲线（迷你图 + 点击行看大图） ----
# 表格迷你趋势列（横截面表格里嵌每行的 sparkline）；五项指标的大图见点击行展开
TREND_TABLE_METRICS: dict[str, str] = {
    "revenue": "营收趋势",
    "net_profit": "净利润趋势",
    "gross_margin": "毛利率趋势",
}
TREND_MAX_POINTS = 8


# 迷你趋势列默认紧跟在各自对应的数据列后面
TREND_ANCHOR_COLUMNS: dict[str, str] = {
    "营收趋势": "营收(千万元)",
    "净利润趋势": "净利润(千万元)",
    "毛利率趋势": "毛利率(%)",
}


def _place_trend_after(frame: pd.DataFrame, anchor_map: dict[str, str]) -> pd.DataFrame:
    """把迷你趋势列移动到各自锚点数据列的后面，其余列保持原顺序。"""
    valid = {
        label: anchor
        for label, anchor in anchor_map.items()
        if label in frame.columns and anchor in frame.columns
    }
    if not valid:
        return frame
    trend_labels = set(valid)
    order: list[str] = []
    for column in frame.columns:
        if column in trend_labels:
            continue
        order.append(column)
        for label, anchor in valid.items():
            if anchor == column:
                order.append(label)
    return frame[order]


def _company_trend_map(
    source: pd.DataFrame, metric: str, company_ids: set[str]
) -> dict[str, list[float]]:
    """按报告期升序取每家公司最近 N 期的指标序列，供迷你趋势列使用。"""
    if source is None or source.empty or metric not in source.columns:
        return {}
    frame = source[["company_id", "report_period", metric]].copy()
    frame = frame[frame["company_id"].astype(str).isin(company_ids)]
    if frame.empty:
        return {}
    frame["_period"] = pd.to_datetime(frame["report_period"], errors="coerce")
    frame = frame.dropna(subset=["_period"]).sort_values(["company_id", "_period"])
    result: dict[str, list[float]] = {}
    for company_id, group in frame.groupby(frame["company_id"].astype(str), sort=False):
        values = (
            pd.to_numeric(group[metric], errors="coerce").dropna().tail(TREND_MAX_POINTS)
        )
        result[company_id] = [float(value) for value in values]
    return result


def _attach_trend_columns(
    shown: pd.DataFrame, row_source: pd.DataFrame, history: pd.DataFrame
) -> pd.DataFrame:
    """给重命名后的展示表追加迷你趋势列（行 index 对齐原数据以定位公司）。"""
    if shown.empty or row_source.empty or history.empty:
        return shown
    try:
        company_ids = row_source.loc[shown.index, "company_id"].astype(str)
    except Exception:  # noqa: BLE001 - index 不对齐时直接不展示趋势列
        return shown
    result = shown.copy()
    for metric, label in TREND_TABLE_METRICS.items():
        trend_map = _company_trend_map(history, metric, set(company_ids))
        result[label] = [trend_map.get(company_id, []) for company_id in company_ids]
    return _place_trend_after(result, TREND_ANCHOR_COLUMNS)


def _trend_column_config(columns: list[str]) -> dict:
    return {
        label: st.column_config.LineChartColumn(label, width="small")
        for label in columns
        if label in TREND_TABLE_METRICS.values()
    }


def _company_history_chart(history: pd.DataFrame, company_id: str) -> pd.DataFrame:
    """取一家公司五项指标的全部历史期，转成绘趋势图用的长表。"""
    if history.empty:
        return pd.DataFrame()
    frame = history[history["company_id"].astype(str).eq(str(company_id))].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["_period"] = pd.to_datetime(frame["report_period"], errors="coerce")
    frame = frame.dropna(subset=["_period"]).sort_values("_period")
    available = [metric for metric in TREND_TABLE_METRICS if metric in frame.columns]
    long = frame.melt(
        id_vars=["_period"],
        value_vars=available,
        var_name="指标",
        value_name="数值",
    )
    long["指标"] = long["指标"].map(
        {"revenue": "营收(千万)", "net_profit": "净利润(千万)", "revenue_yoy": "营收增长率(%)",
         "profit_yoy": "利润增长率(%)", "gross_margin": "毛利率(%)"}
    )
    return long.rename(columns={"_period": "报告期"})


def _render_prosperity_trend_charts(
    trend_rows: pd.DataFrame, revenue_rows: pd.DataFrame | None = None
) -> None:
    """渲染一个统计行业大类的月度趋势：景气度（0-100）、两项增长率与营业收入。"""
    if trend_rows.empty:
        st.caption("该大类暂无足够的历史月度数据。")
        return
    score_chart = (
        alt.Chart(trend_rows)
        .mark_line(point=True)
        .encode(
            x=alt.X("month:T", title=None),
            y=alt.Y("prosperity_score:Q", title="景气度(0-100)", scale=alt.Scale(domain=[0, 100])),
            tooltip=[
                alt.Tooltip("month:T", title="月份"),
                alt.Tooltip("prosperity_score:Q", title="景气度"),
            ],
        )
        .properties(width="container", height=220)
        .configure_view(stroke=None)
    )
    profit_chart = (
        alt.Chart(trend_rows.dropna(subset=["profit_yoy_latest"]))
        .mark_line(point=True)
        .encode(
            x=alt.X("month:T", title=None),
            y=alt.Y("profit_yoy_latest:Q", title="利润同比增长(%)"),
            tooltip=[
                alt.Tooltip("month:T", title="月份"),
                alt.Tooltip("profit_yoy_latest:Q", title="利润增长率(%)", format=",.1f"),
            ],
        )
        .properties(width="container", height=220)
        .configure_view(stroke=None)
    )
    added_chart = (
        alt.Chart(trend_rows.dropna(subset=["value_added_yoy_latest"]))
        .mark_line(point=True)
        .encode(
            x=alt.X("month:T", title=None),
            y=alt.Y("value_added_yoy_latest:Q", title="增加值同比增长(%)"),
            tooltip=[
                alt.Tooltip("month:T", title="月份"),
                alt.Tooltip("value_added_yoy_latest:Q", title="增加值增长率(%)", format=",.1f"),
            ],
        )
        .properties(width="container", height=220)
        .configure_view(stroke=None)
    )
    has_revenue = revenue_rows is not None and not revenue_rows.empty
    if has_revenue:
        revenue_chart = (
            alt.Chart(revenue_rows)
            .mark_line(point=True)
            .encode(
                x=alt.X("month:T", title=None),
                y=alt.Y("revenue_yi:Q", title="营业收入(亿元)"),
                tooltip=[
                    alt.Tooltip("month:T", title="月份"),
                    alt.Tooltip("revenue_yi:Q", title="营业收入(亿元)", format=",.1f"),
                ],
            )
            .properties(width="container", height=220)
            .configure_view(stroke=None)
        )
    # 上排：景气度趋势 + 营业收入趋势
    top_left, top_right = st.columns(2 if has_revenue else 1, gap="medium")
    with top_left:
        st.altair_chart(score_chart, width="stretch")
    if has_revenue:
        with top_right:
            st.altair_chart(revenue_chart, width="stretch")
        st.caption(
            "营业收入随统计局利润发布逐月更新，为 1—N 月累计值（跨月单调上升属正常口径表现）；"
            "该行业发布覆盖期之外的月份暂无数据。"
        )
    # 下排：利润同比增长 + 工业增加值同比增长
    bottom_left, bottom_right = st.columns(2, gap="medium")
    with bottom_left:
        st.altair_chart(profit_chart, width="stretch")
    with bottom_right:
        st.altair_chart(added_chart, width="stretch")
    st.caption(
        "景气度每月都用截至当月的滚动窗口横截面计算，反映该行业在全部行业中的相对位置；"
        "利润增长率与工业增加值增长率为统计局公布的当月同比。"
    )


def _render_company_trend_chart(history: pd.DataFrame, company_id: str, label: str) -> None:
    """点击表格行后，在下方渲染该公司五项指标的历史趋势大图。"""
    long = _company_history_chart(history, company_id)
    if long.empty:
        st.caption(f"{label} 暂无跨报告期历史数据，仅能看到当期值。")
        return
    st.markdown(f"**历史趋势：{label}**")
    periods = sorted(long["报告期"].dt.strftime("%Y-%m-%d").unique())
    st.caption("选中表格行或使用「查看大图」下拉框，可切换查看该公司近 " + str(len(periods)) + " 个报告期（" + "、".join(periods) + "）的五项指标走势。")
    chart = (
        alt.Chart(long)
        .mark_line(point=True)
        .encode(
            x=alt.X("报告期:T", title=None),
            y=alt.Y("数值:Q", title=None),
            color=alt.Color("指标:N", legend=None),
            tooltip=[
                alt.Tooltip("报告期:T", title="报告期"),
                alt.Tooltip("指标:N", title="指标"),
                alt.Tooltip("数值:Q", title="数值", format=",.2f"),
            ],
        )
        .properties(width=230, height=150)
        .facet(facet=alt.Facet("指标:N", title=None), columns=5)
        .resolve_scale(y="independent")
        .configure_view(stroke=None)
    )
    st.altair_chart(chart, width="stretch")


_COMPANY_PICK_PLACEHOLDER = "（请选择公司查看大图）"


def _company_pick_label(company_id: str, company_name: str) -> str:
    if display_mode == "normal":
        return f"{company_name}（{company_id}）"
    return str(company_name)


def _resolve_company_trend_selection(
    shown: pd.DataFrame,
    source: pd.DataFrame,
    event,
    picker_key: str,
) -> tuple[str, str] | None:
    """合并表格行选择与「查看大图」下拉框，返回 (company_id, 展示名)。"""
    if source is None or source.empty or "company_id" not in source.columns:
        return None
    names = source["company_name"] if "company_name" in source.columns else source["company_id"]
    labels: dict[str, str] = {}
    for company_id, company_name in zip(source["company_id"].astype(str), names.astype(str)):
        labels.setdefault(_company_pick_label(company_id, company_name), company_id)
    if not labels:
        return None

    options = [_COMPANY_PICK_PLACEHOLDER, *labels]
    if st.session_state.get(picker_key) not in options:
        st.session_state[picker_key] = _COMPANY_PICK_PLACEHOLDER
    current_pick = str(st.session_state.get(picker_key))

    selected_rows = list(getattr(event, "selection", {}).get("rows", []))
    row_id: str | None = None
    row_label: str | None = None
    if selected_rows:
        position = int(selected_rows[0])
        if 0 <= position < len(shown.index):
            index = shown.index[position]
            if index in source.index:
                row_id = str(source.loc[index, "company_id"])
                name = str(source.loc[index, "company_name"]) if "company_name" in source.columns else row_id
                row_label = _company_pick_label(row_id, name)

    last_row_key = f"{picker_key}_last_row"
    last_pick_key = f"{picker_key}_last_pick"
    last_row = st.session_state.get(last_row_key)
    last_pick = st.session_state.get(last_pick_key, _COMPANY_PICK_PLACEHOLDER)
    row_changed = row_id != last_row
    pick_changed = current_pick != last_pick

    if row_changed:
        chosen_id = row_id
        chosen_label = row_label
        st.session_state[picker_key] = chosen_label if chosen_label in labels else _COMPANY_PICK_PLACEHOLDER
    elif pick_changed:
        chosen_id = labels.get(current_pick)
        chosen_label = current_pick if chosen_id else None
    else:
        chosen_id = labels.get(current_pick)
        chosen_label = current_pick if chosen_id else None
        if not chosen_id and row_id:
            chosen_id = row_id
            chosen_label = row_label
            if row_label in labels:
                st.session_state[picker_key] = row_label

    st.session_state[last_row_key] = row_id
    st.session_state[last_pick_key] = st.session_state.get(picker_key, _COMPANY_PICK_PLACEHOLDER)

    st.selectbox("查看大图", options, key=picker_key)
    if not chosen_id:
        return None
    return chosen_id, str(chosen_label or chosen_id)


def _grey_low_payers(shown: pd.DataFrame, group: pd.DataFrame):
    """给需要灰化的行整行加灰色文字样式，其余保持原样。"""
    grey = _low_return_grey_mask(group, shown.index)
    if not grey.any():
        return shown

    def _style(row: pd.Series) -> list[str]:
        if bool(grey.loc[row.name]):
            return ["color: #9e9e9e;"] * len(row)
        return [""] * len(row)

    return shown.style.apply(_style, axis=1)


# 个别行业的展示名（仅用于重点子行业卡片标题，不改行业目录）
INDUSTRY_DISPLAY_NAMES: dict[str, str] = {
    "857421": "航空装备Ⅲ（大飞机）",
}


def render_industry_group(
    group: pd.DataFrame,
    industry_key: str,
    sort_by: str | None = None,
    note: str | None = None,
) -> None:
    """渲染单个三级行业的排名卡片（指标、明细表、缺漏提示）。"""
    # 融资分红三项（历史融资/累计分红/融资分红比）来自本地缓存，供用户参考
    group = attach_financing_dividend(group)
    group = group.sort_values("industry_rank", na_position="last")
    group_name = INDUSTRY_DISPLAY_NAMES.get(industry_key) or str(
        group["industry_l3_name"].iloc[0]
    )
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
        table = _attach_trend_columns(
            industry_display_frame(group, sort_by), group, all_industry_financials
        )
        event = st.dataframe(
            _grey_low_payers(table, group),
            width="stretch",
            hide_index=True,
            column_config=_trend_column_config(list(table.columns)),
            on_select="rerun",
            selection_mode="single-row",
            key=f"company_trend_{industry_key}",
        )
        company_pick = _resolve_company_trend_selection(
            table, group, event, f"company_pick_{industry_key}"
        )
        if company_pick:
            company_id, company_label = company_pick
            _render_company_trend_chart(all_industry_financials, company_id, company_label)
        else:
            st.caption("提示：点击表格行首复选框，或使用上方「查看大图」下拉框，查看历史趋势曲线。")
        if _low_return_grey_mask(group, group.index).any():
            st.caption("灰色行：上市满 10 年且分红融资比低于 1，累计分红仍低于历史融资总额。")
        low_confidence = group[
            pd.to_numeric(group.get("extraction_confidence"), errors="coerce").lt(2 / 3)
        ]
        if not low_confidence.empty:
            st.caption(
                f"{len(low_confidence)} 条记录提取置信度较低，"
                "应打开原公告人工复核后再使用（见「公告证据」页签）。"
            )
        covered_ids = set(group["company_id"].astype(str))
        # 二级聚合行业（如工业金属）的成分 = 组内全部三级行业成分之和
        group_constituents = _demo_constituents_for(
            list(l2_group_l3_ids().get(industry_key, (industry_key,)))
        )
        absent = group_constituents[
            ~group_constituents["company_id"].astype(str).isin(covered_ids)
        ]
        if not absent.empty:
            st.caption(
                f"该行业共 {len(group_constituents)} 家成份公司，"
                f"其中 {len(absent)} 家本期未取得定期报告："
                + "、".join(absent["company_name"].astype(str).tolist())
            )
        if note:
            st.caption(note)


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
    inline_notes: bool = False,
) -> None:
    """渲染一组三级行业排名。

    `order` 给出行业 ID 的展示顺序；`group_titles` 给出行业 ID 所属主题，
    主题切换处插入分组标题并隔开，`group_captions` 为主题下的一句说明。
    `inline_notes=True` 时把「行业名：说明」式的备注放进该行业框内最下方，
    未能对上行业名的备注仍在顶部以提示条显示。
    """
    notes_by_name: dict[str, str] = {}
    if inline_notes:
        for note in notes:
            name, separator, detail = note.partition("：")
            if separator:
                notes_by_name[name.strip()] = detail.strip()
    if ranking.empty:
        for note in notes:
            st.info(note)
        st.warning("截止日之前没有可排名的定期报告。")
        return
    groups = list(ranking.groupby("industry_l3_id", sort=True))
    if order:
        groups.sort(key=lambda item: order.get(str(item[0]), len(order) + 1))
    if not inline_notes:
        for note in notes:
            st.info(note)
    else:
        group_names = {
            str(group["industry_l3_name"].iloc[0]) for _, group in groups
        }
        for note in notes:
            name = note.partition("：")[0].strip()
            if name not in group_names:
                st.info(note)
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
        group_name = str(group["industry_l3_name"].iloc[0])
        render_industry_group(group, industry_key, sort_by, note=notes_by_name.get(group_name))
    highest_period = pd.to_datetime(ranking["report_period"], errors="coerce").max()
    st.download_button(
        download_label,
        data=_industry_excel_bytes(ranking),
        file_name=(
            f"industry_ranking_{pd.Timestamp(highest_period):%Y%m%d}"
            f"_asof_{as_of_date:%Y%m%d}.xlsx"
        ),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
        key=download_key,
        type="primary",
    )


def merge_stat_items(
    items: list[dict[str, object]],
) -> list[list[dict[str, object]]]:
    """把对应申万三级行业互为子集的统计局大类合并成一组。

    例如「有色金属矿采选业」的 12 个三级行业完全包含于「有色金属冶炼和压延加工业」
    的 14 个，成分公司 100% 重叠，合并后只显示一个展开框。
    """
    groups: list[list[dict[str, object]]] = []
    for item in sorted(items, key=lambda it: -len(tuple(it["l3_ids"]))):
        ids = set(item["l3_ids"])  # type: ignore[arg-type]
        for group in groups:
            if ids <= set(group[0]["l3_ids"]):  # type: ignore[arg-type]
                group.append(item)
                break
        else:
            groups.append([item])
    return groups


def render_stat_industry_block(
    *,
    stat_items: list[dict[str, object]],
    ranking_frame: pd.DataFrame,
    key_suffix: str,
    expanded: bool = True,
    show_constituents: bool = True,
    show_l3_table: bool = True,
    footer_notes: bool = False,
    group_title: str | None = None,
    extra_industries: pd.DataFrame | None = None,
    extra_group_label: str | None = None,
    extra_group_count: int | None = None,
) -> None:
    """渲染统计行业大类（可多个合并）：对应三级行业、成分公司与排名靠前公司。

    `stat_items` 是同一组的大类列表，对应三级行业取并集；
    `show_constituents=False` 时省略成分公司选择与拉取按钮（该 tab 下方已有同样面板）；
    `show_l3_table=False` 时连三级行业清单也省略（组内行业已列在下方面板，避免重复）；
    `footer_notes=True` 时标题只写大类数量，各大类景气度改为表格后文字注释；
    `extra_industries` 补充映射未覆盖的行业（如军工电子Ⅲ）；
    `extra_group_label`/`extra_group_count` 在标题末尾追加行业组标注（如国防军工）。
    """
    industry_frames = [
        corresponding_industries(str(item["stat_industry"])) for item in stat_items
    ]
    if extra_industries is not None and not extra_industries.empty:
        industry_frames.append(extra_industries)
    if len(industry_frames) == 1:
        industries = industry_frames[0]
    else:
        industries = (
            pd.concat(industry_frames, ignore_index=True)
            .drop_duplicates("industry_l3_id")
            .sort_values("industry_l3_id")
            .reset_index(drop=True)
        )
    parts: list[str] = []
    for item in stat_items:
        score = item["score"]
        profit_latest = item["profit_latest"]
        name = str(item["stat_industry"])
        if len(stat_items) > 1 and profit_latest is not None:
            # 合并框内并列多个大类，逐个标注景气度与利润增长
            parts.append(
                f"{name}：景气度 {score if score is not None else '—'}，"
                f"利润增长 {profit_latest:.1f}%"
            )
        elif score is None:
            parts.append(f"{name}：统计局暂无月度景气数据")
        else:
            parts.append(
                f"{name}：景气度 {score}，利润增长 {profit_latest:.1f}%"
            )
    if footer_notes:
        label = group_title or f"{len(stat_items)} 个统计局大类"
        title = f"{label}，对应 {len(industries)} 个申万三级行业"
    else:
        title = "；".join(parts) + f"，对应 {len(industries)} 个申万三级行业"
    if extra_group_label and extra_group_count:
        title += f"；{extra_group_label}，包含 {extra_group_count} 个申万三级行业"
    with st.expander(title, expanded=expanded):
        if industries.empty:
            st.write("该统计行业没有对应的申万三级行业。")
            return
        if footer_notes:
            # 各大类景气度统一放在框内顶部，紧接注释列出三级行业清单
            st.caption("；".join(parts))
        if show_l3_table:
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
                width="stretch",
                hide_index=True,
            )
        if show_l3_table and show_constituents:
            constituents = _demo_constituents_for(industries["industry_l3_id"])
            if constituents.empty:
                st.caption(
                    "这些三级行业尚未固化成分公司，可运行 "
                    "`python tools/freeze_sw_constituents.py --top-n 5` 从公开来源补齐。"
                )
                return
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
                key=f"constituents_{key_suffix}",
            )
            selected_id = labels[chosen]
            selected_name = chosen.split("（")[0]
            selected_companies = constituents[
                constituents["industry_l3_id"].eq(selected_id)
            ]
            st.dataframe(
                selected_companies.rename(
                    columns={
                        "company_id": "公司代码",
                        "company_name": "公司名称",
                        "include_date": "纳入时间",
                    }
                )[_demo_visible_labels(["公司代码", "公司名称", "纳入时间"])],
                width="stretch",
                hide_index=True,
            )
            if st.button(
                f"拉取「{selected_name}」全部成分公司近一年财报",
                key=f"sync_{key_suffix}_{selected_id}",
                type="primary",
            ):
                sync_progress = st.progress(0.0, text="准备拉取")

                def show_sync_progress(current: int, total: int, message: str) -> None:
                    sync_progress.progress(current / max(total, 1), text=message)

                try:
                    with st.spinner("正在从巨潮资讯检索并解析正式定期报告，请勿关闭页面。"):
                        synced, sync_errors = sync_industry_reports(
                            industry=selected_id,
                            as_of_date=date.today(),
                            only_missing=True,
                            progress=show_sync_progress,
                        )
                    sync_progress.progress(1.0, text="拉取完成")
                    st.success(
                        f"{selected_name}：本地累计 {len(synced)} 条定期报告记录，"
                        f"覆盖 {synced['company_id'].nunique() if not synced.empty else 0} 家公司。"
                    )
                    if not sync_errors.empty:
                        st.warning(
                            f"有 {len(sync_errors)} 条检索或解析失败，明细见 data/industries/errors.csv。"
                        )
                except Exception as exc:
                    st.error(f"拉取失败：{exc}")
        companies = top_companies_for_industry_ids(
            ranking_frame, industries["industry_l3_id"], per_industry=3
        )
        if companies.empty:
            st.caption("当前数据源中没有这些三级行业的公司排名，可先上传数据或同步官方公告。")
        else:
            st.dataframe(display_frame(companies), width="stretch", hide_index=True)

risk_tab, watch_tab, prosperity_tab, detail_tab, customs_tab, liquidity_tab, quality_tab, evidence_tab, method_tab = st.tabs(
    ["风险预警", "观察库", "行业景气度", "行业详情", "海关数据", "流动性分析", "数据质量", "公告证据", "算法说明"]
)

with risk_tab:
    st.subheader("A股牛熊估算")
    refresh_regime = bool(st.session_state.pop("regime_refresh", False))
    try:
        if refresh_regime:
            with st.spinner("正在更新五指数日线并重新估算..."):
                regime_report = compute_or_cached_report(force_refresh=True)
        else:
            regime_report = load_cached_report()
            if regime_report is None:
                with st.spinner("首次估算中，结果将写入缓存..."):
                    regime_report = compute_or_cached_report()
    except Exception as exc:
        st.error(f"牛熊估算失败：{exc}")
        regime_report = None

    if regime_report is not None:
        latest = regime_report["latest"]
        regime_value = float(latest["score"])
        regime_state = str(latest["state"])
        regime_config = load_regime_config()
        bounds = regime_config.thresholds["composite"]
        score_bear = float(bounds["bear"])
        score_bull = float(bounds["bull"])
        ruler_bear, ruler_bull = ruler_bands(regime_config)
        target_bear = float(regime_config.target_shares["BEAR"])
        target_range = float(regime_config.target_shares["RANGE"])
        target_bull = float(regime_config.target_shares["BULL"])

        metric_cols = st.columns(5)
        metric_cols[0].metric("综合分", f"{regime_value:.1f}")
        metric_cols[1].metric("状态", STATE_LABELS.get(regime_state, regime_state))
        metric_cols[2].metric("广度", f"{float(latest['breadth']) * 100:.0f}%")
        metric_cols[3].metric("大盘价值", f"{float(latest['groups'].get('large_value', {}).get('score', 0)):.1f}")
        metric_cols[4].metric("成长", f"{float(latest['groups'].get('growth', {}).get('score', 0)):.1f}")
        st.caption(
            f"数据截至 {latest['date']}：上证50、沪深300、中证500、创业板指、科创50；"
            "三因子按1250日滚动分位标准化，广度以乘法调节置信度。状态基于量化信号。"
        )

        # ---- 市场状态游标卡尺（后台算法自动计算，页面只读） ----
        def _lerp_hex(c1: tuple, c2: tuple, t: float) -> str:
            return "#%02x%02x%02x" % tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))

        ruler_rows = []
        for x0 in range(-100, 100):
            mid = x0 + 0.5
            if mid <= ruler_bear:
                t = min(1.0, max(0.0, (ruler_bear - mid) / (ruler_bear + 100)))
                color = _lerp_hex((134, 201, 138), (13, 90, 24), t)
            elif mid >= ruler_bull:
                t = min(1.0, max(0.0, (mid - ruler_bull) / (100 - ruler_bull)))
                color = _lerp_hex((229, 139, 125), (126, 21, 11), t)
            else:
                color = "#c2c2c2"
            ruler_rows.append({"x0": x0, "x1": x0 + 1, "y0": 0.7, "y1": 1.7, "色": color})
        ruler_frame = pd.DataFrame(ruler_rows)
        regime_base = alt.Chart(ruler_frame).encode(
            x=alt.X(
                "x0:Q",
                title=None,
                scale=alt.Scale(domain=[-100, 100]),
                axis=alt.Axis(values=[-100, int(ruler_bear), 0, int(ruler_bull), 100]),
            ),
        )
        regime_ruler = regime_base.mark_rect().encode(
            x2="x1:Q",
            y=alt.Y("y0:Q", axis=None, scale=alt.Scale(domain=[0, 2.0])),
            y2="y1:Q",
            color=alt.Color("色:N", scale=None, legend=None),
        )
        regime_notes = (
            alt.Chart(
                pd.DataFrame(
                    {
                        "刻度": [
                            (-100 + ruler_bear) / 2,
                            (ruler_bear + ruler_bull) / 2,
                            (ruler_bull + 100) / 2,
                        ],
                        "注释": ["熊市", "震荡市", "牛市"],
                        "note_y": [0.3, 0.3, 0.3],
                    }
                )
            )
            .mark_text(dy=0, fontSize=15, fontWeight=600, color="#000000")
            .encode(
                x=alt.X("刻度:Q", scale=alt.Scale(domain=[-100, 100])),
                y=alt.Y("note_y:Q", axis=None, scale=alt.Scale(domain=[0, 2.0])),
                text=alt.Text("注释:N"),
            )
        )
        regime_cursor = (
            alt.Chart(pd.DataFrame({"游标": [regime_value]}))
            .mark_rule(color="#1f2937", size=3)
            .encode(x=alt.X("游标:Q", scale=alt.Scale(domain=[-100, 100])))
        )
        regime_label = (
            alt.Chart(pd.DataFrame({"游标": [regime_value], "标签": [f"{regime_value:.0f}"], "label_y": [1.92]}))
            .mark_text(dy=-6, fontSize=13, fontWeight=700, color="#1f2937")
            .encode(
                x=alt.X("游标:Q", scale=alt.Scale(domain=[-100, 100])),
                y=alt.Y("label_y:Q", axis=None, scale=alt.Scale(domain=[0, 2.0])),
                text=alt.Text("标签:N"),
            )
        )
        regime_chart = (
            regime_ruler + regime_notes + regime_cursor + regime_label
        ).properties(width="container", height=150).configure(background="white")
        st.altair_chart(regime_chart, width="stretch")
        st.caption(
            f"卡尺色带按目标占比划分：熊市 {target_bear:.0%}（{int(ruler_bear + 100)} 格）、"
            f"震荡 {target_range:.0%}（{int(100 + ruler_bull - (ruler_bear + 100))} 格）、"
            f"牛市 {target_bull:.0%}（{int(100 - ruler_bull)} 格）。"
        )

        action_cols = st.columns([1, 4])
        if action_cols[0].button("更新五指数日线", key="regime_refresh_btn", type="primary"):
            st.session_state["regime_refresh"] = True
            st.rerun()
        if latest.get("warnings"):
            action_cols[1].warning("；".join(latest["warnings"][:3]))
        st.caption("页面默认读取已缓存的估算结果；只有点击「更新五指数日线」才会重新拉取数据并计算。")

        st.markdown("**历史综合分曲线**")
        history = regime_report["composite"][["date", "score", "state"]].copy()
        history["state"] = history["state"].map(STATE_LABELS).fillna(history["state"])
        line = alt.Chart(history).mark_line(color="#1d4ed8", strokeWidth=1.5).encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("score:Q", title="综合分", scale=alt.Scale(domain=[-100, 100])),
        )
        points = alt.Chart(history.iloc[::20]).mark_circle(size=18).encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("score:Q", scale=alt.Scale(domain=[-100, 100])),
            color=alt.Color(
                "state:N",
                title="状态",
                scale=alt.Scale(
                    domain=["熊市", "震荡市", "牛市"],
                    range=["#16a34a", "#9ca3af", "#dc2626"],
                ),
            ),
        )
        rules = alt.Chart(pd.DataFrame({"y": [score_bear, score_bull]})).mark_rule(
            color="#6b7280", strokeDash=[4, 3]
        ).encode(y="y:Q")
        st.altair_chart((line + points + rules).properties(height=240, width="container"), width="stretch")

        st.markdown("**分组状态**")
        group_rows = []
        for key, item in latest["groups"].items():
            members = regime_config.groups.get(key, [])
            member_names = " + ".join(regime_config.name_of(code) for code in members)
            group_rows.append(
                {
                    "分组": f"{item['label']}（{member_names}）" if member_names else item["label"],
                    "分值": item["score"],
                    "状态": item["state_label"],
                }
            )
        st.dataframe(pd.DataFrame(group_rows), width="stretch", hide_index=True)

        st.markdown("**当日五指数明细**")
        st.dataframe(regime_report["table"], width="stretch", hide_index=True)
        st.markdown("**估算方法与当前阈值**")
        factor_weights = regime_config.weights
        group_weights = regime_config.group_weights
        group_bounds = regime_config.thresholds
        st.caption(
            "取上证50、沪深300、中证500、创业板指、科创50五个宽基指数日线，"
            "分别计算趋势偏离、中期均线斜率、长周期方向三个因子，"
            "按过去 1250 个交易日滚动分位标准化到 [-1, 1] 后加权成各指数分值；"
            f"三因子权重 {float(factor_weights['f1']):.0%} / {float(factor_weights['f2']):.0%} / "
            f"{float(factor_weights['f3']):.0%}，"
            f"大盘价值 / 中证500 / 成长权重 {float(group_weights['large_value']):.0%} / "
            f"{float(group_weights['mid500']):.0%} / {float(group_weights['growth']):.0%}，"
            f"广度以 {float(regime_config.breadth_multiplier):.1f} 倍乘调节置信度。"
        )
        st.caption(
            f"当前状态阈值：单指数 {float(group_bounds['index']['bear']):.0f} / "
            f"{float(group_bounds['index']['bull']):.0f}，"
            f"大盘价值 {float(group_bounds['large_value']['bear']):.0f} / "
            f"{float(group_bounds['large_value']['bull']):.0f}，"
            f"中证500 {float(group_bounds['mid500']['bear']):.0f} / "
            f"{float(group_bounds['mid500']['bull']):.0f}，"
            f"成长 {float(group_bounds['growth']['bear']):.0f} / "
            f"{float(group_bounds['growth']['bull']):.0f}，"
            f"综合分 {score_bear:.0f} / {score_bull:.0f}。"
            "分值低于下限为熊市，高于上限为牛市，之间为震荡市。"
            f"阈值按目标占比 熊 {target_bear:.0%} / 震 {target_range:.0%} / 牛 {target_bull:.0%} 校准；"
            "如需调整，修改 config/bull_bear.json 的 target_shares 后运行 tools/recalibrate_bull_bear.py。"
        )
    st.divider()

    st.caption(
        "沪深300股指期货（IF）主力连续日K线，来自中金所官网历史数据包（每日成交量最大的 IF 合约作为主力）。"
    )
    risk_cols = st.columns([3, 1])
    with risk_cols[1]:
        if risk_cols[1].button("从中金所更新日K线", key="risk_update", type="primary"):
            try:
                update_if_daily(months=3)
                st.success("已更新至最新交易日。")
            except CffexError as exc:
                st.error(f"更新失败：{exc}")
    if_daily = load_if_daily()
    if if_daily.empty:
        st.info("本地还没有中金所日K线缓存，点击右上角按钮从中金所下载近 3 个月数据。")
    else:
        risk_frame = if_daily.copy()
        risk_frame["date"] = pd.to_datetime(risk_frame["date"], format="%Y%m%d")
        risk_frame = risk_frame.sort_values("date").reset_index(drop=True)
        closes = risk_frame["close"]
        latest = risk_frame.iloc[-1]

        def _period_return(days: int) -> float | None:
            if len(risk_frame) <= days:
                return None
            return closes.iloc[-1] / closes.iloc[-1 - days] - 1

        ret5 = _period_return(5)
        ret20 = _period_return(20)
        daily_ret = closes.pct_change()
        vol20_annual = daily_ret.rolling(20).std().iloc[-1] * (252 ** 0.5) if len(risk_frame) > 21 else None
        lookback = min(60, len(risk_frame))
        rolling_high = closes.iloc[-lookback:].max()
        drawdown = closes.iloc[-1] / rolling_high - 1
        down_streak = 0
        for change in daily_ret.iloc[::-1]:
            if change < 0:
                down_streak += 1
            else:
                break

        metric_cols = st.columns(5)
        metric_cols[0].metric(
            "最新收盘（主力）",
            f"{latest['close']:,.1f}",
            None if ret5 is None else f"{ret5:+.2%}",
            help=f"合约 {latest['contract']}，日期 {latest['date']:%Y-%m-%d}",
        )
        metric_cols[1].metric("近5日涨跌", None if ret5 is None else f"{ret5:+.2%}")
        metric_cols[2].metric("近20日涨跌", None if ret20 is None else f"{ret20:+.2%}")
        metric_cols[3].metric(
            "20日年化波动率",
            None if vol20_annual is None else f"{vol20_annual:.1%}",
            None if vol20_annual is None else ("偏高" if vol20_annual > 0.30 else ("中性" if vol20_annual > 0.20 else "偏低")),
        )
        metric_cols[4].metric(
            f"距{lookback}日高点回撤",
            f"{drawdown:+.2%}",
        )

        alerts = []
        if drawdown <= -0.05:
            alerts.append(("error", f"深度回撤：距近 {lookback} 日高点已下跌 {abs(drawdown):.1%}，注意系统性风险。"))
        elif drawdown <= -0.03:
            alerts.append(("warning", f"回撤提醒：距近 {lookback} 日高点下跌 {abs(drawdown):.1%}。"))
        if vol20_annual is not None and vol20_annual > 0.30:
            alerts.append(("warning", f"波动放大：20日年化波动率 {vol20_annual:.1%}，市场情绪剧烈。"))
        if down_streak >= 3:
            alerts.append(("warning", f"连续下跌：主力连续 {down_streak} 个交易日收跌。"))
        if not alerts:
            st.success("风险指标平稳：未触发回撤、波动与连跌预警。")
        else:
            for level, message in alerts:
                (st.error if level == "error" else st.warning)(message)

        risk_frame["ema60"] = closes.ewm(span=60, adjust=False).mean()
        window = risk_frame
        # vconcat 合并为单一图表组件，zoom 参数在 K 线与成交量之间真正联动
        zoom = alt.selection_interval(encodings=["x"], bind="scales", name="时间轴平移缩放")
        candle_base = alt.Chart(window).encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(labels=False, ticks=False, domain=False))
        )
        candle_range = candle_base.mark_bar(size=1.5).encode(
            y=alt.Y("low:Q", title=None, scale=alt.Scale(zero=False)),
            y2="high:Q",
        )
        candle_body = candle_base.mark_bar(size=1).encode(
            y=alt.Y("open:Q", scale=alt.Scale(zero=False)),
            y2="close:Q",
            color=alt.condition(
                alt.datum.close >= alt.datum.open,
                alt.value("#e05a4e"),
                alt.value("#3d9c5f"),
            ),
        )
        ema60_line = candle_base.mark_line(color="#c98a1c", size=2).encode(
            y=alt.Y("ema60:Q", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T", title="日期"), alt.Tooltip("ema60:Q", title="60日EMA", format=",.1f")],
        )
        ema_note_frame = window.iloc[[-1]][["date", "ema60"]].copy()
        ema_note_frame["文字"] = "60日EMA →"
        ema_note = (
            alt.Chart(ema_note_frame)
            .mark_text(dx=-8, dy=-10, align="right", fontSize=12, fontWeight=600, color="#c98a1c")
            .encode(
                x=alt.X("date:T", axis=alt.Axis(labels=False, ticks=False, domain=False)),
                y=alt.Y("ema60:Q", scale=alt.Scale(zero=False), axis=None),
                text=alt.Text("文字:N"),
            )
        )
        candle_panel = (
            (candle_range + candle_body + ema60_line + ema_note)
            .add_params(zoom)
            .properties(width="container", height=280)
        )
        volume_base = alt.Chart(window).encode(x=alt.X("date:T", title=None))
        volume_chart = volume_base.mark_bar(color="#9fb3c8").encode(
            y=alt.Y("volume:Q", title="成交量(手)"),
            tooltip=[
                alt.Tooltip("date:T", title="日期"),
                alt.Tooltip("contract:N", title="主力合约"),
                alt.Tooltip("open:Q", title="开盘", format=","),
                alt.Tooltip("close:Q", title="收盘", format=","),
                alt.Tooltip("volume:Q", title="成交量", format=","),
                alt.Tooltip("turnover_yi:Q", title="成交额(亿)", format=",.1f"),
            ],
        )
        volume_panel = volume_chart.add_params(zoom).properties(width="container", height=110)
        st.altair_chart(alt.vconcat(candle_panel, volume_panel), width="stretch")
        st.caption(
            f"共 {len(window)} 个交易日（{window['date'].min():%Y-%m-%d} ~ {window['date'].max():%Y-%m-%d}），"
            "红涨绿跌，黄色实线为60日EMA（指数移动平均，用于观察大趋势）；"
            "鼠标在图上左右拖拽可平移时间轴、滚轮缩放（K线与成交量联动）。"
            "成交额按日汇总该主力合约。数据源：中金所官网历史数据包。"
        )

        st.divider()

        macro_session = st.session_state.setdefault("macro_session_id", new_session_id())
        if SHOW_MACRO_VOTE:
            # ---- 宏观风险投票（红柱，全局共享分值；暂隐藏，SHOW_MACRO_VOTE=True 恢复） ----
            st.markdown("##### 宏观风险投票")
            vote_values = load_votes()
            vote_frame = pd.DataFrame(
                {
                    "风险项": list(vote_values),
                    "分值": list(vote_values.values()),
                    "标签": [f"{item}，{value:.1f}分" for item, value in vote_values.items()],
                }
            )
            vote_base = alt.Chart(vote_frame).encode(
                x=alt.X(
                    "风险项:N",
                    sort=list(DEFAULT_RISK_VALUES),
                    title=None,
                    axis=alt.Axis(labels=False, ticks=False),
                ),
            )
            vote_bar = vote_base.mark_bar(color="#e05a4e", size=48).encode(
                y=alt.Y("分值:Q", title="风险分值", scale=alt.Scale(domain=[0, 100])),
                tooltip=[alt.Tooltip("风险项:N", title="风险项"), alt.Tooltip("分值:Q", title="当前分值", format=".1f")],
            )
            vote_label = vote_base.mark_text(dy=-8, color="#a03a30", fontSize=13, fontWeight=600).encode(
                y=alt.Y("分值:Q"),
                text=alt.Text("标签:N"),
            )
            vote_chart = (
                (vote_bar + vote_label)
                .properties(width="container", height=320)
                .configure_view(stroke=None)
            )
            st.altair_chart(vote_chart, width="stretch")
            icon_cols = st.columns(4)
            for slot, item in zip(icon_cols, vote_values):
                _, up_col, name_col, down_col, _ = slot.columns(
                    [0.85, 0.5, 1.1, 0.5, 0.85], gap="small"
                )
                if up_col.button("👍", key=f"vote_up_{item}", help="认同"):
                    vote_risk(item, True, macro_session)
                    st.rerun()
                name_col.markdown(
                    f"<div style='text-align:center;white-space:nowrap;font-size:0.92rem;font-weight:600;padding-top:6px'>{item}</div>",
                    unsafe_allow_html=True,
                )
                if down_col.button("👎", key=f"vote_down_{item}", help="不认同"):
                    vote_risk(item, False, macro_session)
                    st.rerun()

            st.divider()

        # ---- 评论板块（缓冲栈/全局栈） ----
        st.markdown("##### 评论")
        client_ip = "未知"
        try:
            client_ip = st.context.headers.get("X-Forwarded-For", "未知") or "未知"
        except Exception:
            pass
        comment_input = st.text_area("发表评论（500 字以内）", key="macro_comment_input", height=90)
        if st.button("提交评论", key="macro_comment_submit", type="primary"):
            try:
                add_comment(comment_input, macro_session, client_ip)
                st.success("已提交，等待管理员精选后公开。")
            except ValueError as exc:
                st.error(str(exc))
        comments_data = load_comments()
        if comments_data["global"]:
            st.markdown("**精选评论**")
            for record in comments_data["global"]:
                posted = str(record["ts"])[:16].replace("T", " ")
                body_cols = st.columns([12, 1])
                body_cols[0].markdown(
                    f"<div style='font-size:0.9rem'>{escape(str(record['content']))}</div>"
                    f"<div style='font-size:0.75rem;color:#888'>网友 · {escape(posted)}</div>",
                    unsafe_allow_html=True,
                )
                if is_admin and body_cols[1].button("删除", key=f"del_global_{record['id']}"):
                    delete_comment(record["id"], macro_session, is_admin=True)
                    st.rerun()
        else:
            st.caption("暂无精选评论。")
        my_comments = [r for r in comments_data["buffer"] if r.get("session_id") == macro_session]
        if my_comments:
            st.markdown("**我的评论（待精选）**")
            for record in my_comments:
                posted = str(record["ts"])[:16].replace("T", " ")
                my_cols = st.columns([12, 1])
                my_cols[0].markdown(
                    f"<div style='font-size:0.9rem'>{escape(str(record['content']))}</div>"
                    f"<div style='font-size:0.75rem;color:#888'>{escape(posted)}</div>",
                    unsafe_allow_html=True,
                )
                if my_cols[1].button("删除", key=f"del_mine_{record['id']}"):
                    delete_comment(record["id"], macro_session, is_admin=False)
                    st.rerun()
        if is_admin and comments_data["buffer"]:
            st.markdown(f"**管理：缓冲栈全部评论（{len(comments_data['buffer'])} 条，含 IP）**")
            for record in comments_data["buffer"]:
                posted = str(record["ts"])[:16].replace("T", " ")
                admin_cols = st.columns([10, 1, 1])
                admin_cols[0].markdown(
                    f"<div style='font-size:0.9rem'>{escape(str(record['content']))}</div>"
                    f"<div style='font-size:0.75rem;color:#888'>IP {escape(str(record['ip']))} · {escape(posted)}</div>",
                    unsafe_allow_html=True,
                )
                if admin_cols[1].button("精选", key=f"feat_{record['id']}"):
                    feature_comment(record["id"], is_admin=True)
                    st.rerun()
                if admin_cols[2].button("删除", key=f"del_admin_{record['id']}"):
                    delete_comment(record["id"], macro_session, is_admin=True)
                    st.rerun()

with watch_tab:
    st.info("当前使用巨潮资讯正式定期报告的本地提取缓存。")
    watch_metric_columns = st.columns(4)
    watch_metric_columns[0].metric("三级行业", f"{industry_count}")
    watch_metric_columns[1].metric("本期已公告公司", f"{len(ranking)}")
    watch_metric_columns[2].metric("符合完整度要求", f"{eligible_count}")
    watch_metric_columns[3].metric("进入观察库", f"{len(watchlist)}")
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
    watch_table = _attach_trend_columns(
        display_frame(shown_watchlist, SORT_METRICS[watch_sort_label]),
        shown_watchlist,
        all_industry_financials,
    )
    watch_event = st.dataframe(
        watch_table,
        width="stretch",
        hide_index=True,
        column_config=_trend_column_config(list(watch_table.columns)),
        on_select="rerun",
        selection_mode="single-row",
        key="watch_company_trend",
    )
    watch_company_pick = _resolve_company_trend_selection(
        watch_table, shown_watchlist, watch_event, "watch_company_pick"
    )
    if watch_company_pick:
        company_id, company_label = watch_company_pick
        _render_company_trend_chart(all_industry_financials, company_id, company_label)
    else:
        st.caption("提示：点击表格行首复选框，或使用上方「查看大图」下拉框，查看历史趋势曲线。")
    excel_bytes = dataframe_to_excel_bytes(ranking, watchlist, model_weight_table())
    st.download_button(
        "下载本期观察库与完整排名",
        data=excel_bytes,
        file_name=f"industry_watchlist_{pd.Timestamp(report_period):%Y%m%d}_asof_{as_of:%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
        type="primary",
    )

with prosperity_tab:
    st.caption(
        "数据来自国家统计局每月公布的规模以上工业分行业增加值同比增长与利润总额同比增长。"
        "景气度只使用官方口径增长率，不做主观评价。每月统计局公布后点击更新即可滚动最近 6 个月。"
    )
    control_columns = st.columns([2, 1, 1])
    prosperity_window = int(control_columns[1].slider("滚动窗口（月）", 3, 12, DEFAULT_WINDOW, 1))
    prosperity_top_n = int(control_columns[2].slider("最景气行业数", 3, 10, DEFAULT_TOP_N, 1))
    if control_columns[0].button(
        "从国家统计局更新月度景气数据",
        width="stretch",
        key="prosperity_update",
        type="primary",
    ):
        try:
            with st.spinner("正在从国家统计局检索分行业工业增加值与利润总额数据。"):
                update_history(months=prosperity_window)
            st.success("景气数据已更新。")
        except Exception as exc:
            st.error(f"景气数据更新失败：{exc}")

    history = load_history()
    if not history.empty:
        history = history[history["stat_industry"].isin(PRIMARY_STAT_INDUSTRIES)]
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
            shown = top_table.drop(
                columns=["profit_yoy_mean", "value_added_yoy_mean"], errors="ignore"
            ).rename(
                columns={
                    "stat_industry": "统计行业大类",
                    "prosperity_score": "景气度",
                    "profit_yoy_latest": "利润增长率（最新月）",
                    "value_added_yoy_latest": "工业增加值增长率（最新月）",
                    "profit_momentum": "利润增长动量",
                    "months": "有效月数",
                    "latest_month": "最新月份",
                    "data_note": "数据说明",
                }
            )
            # 历史趋势：每月用截至当月的窗口数据横截面计算，逐月回看
            p_hist = prosperity_history(history, window=prosperity_window, points=12)
            if not p_hist.empty:
                score_trend = p_hist.groupby("stat_industry")["prosperity_score"].apply(list).to_dict()
                profit_trend = p_hist.groupby("stat_industry")["profit_yoy_latest"].apply(list).to_dict()
                added_trend = (
                    p_hist.groupby("stat_industry")["value_added_yoy_latest"].apply(list).to_dict()
                )
                shown["景气度趋势"] = shown["统计行业大类"].map(
                    lambda name: [float(v) for v in score_trend.get(name, [])]
                )
                shown["利润增长趋势"] = shown["统计行业大类"].map(
                    lambda name: [float(v) for v in profit_trend.get(name, []) if pd.notna(v)]
                )
                shown["增加值趋势"] = shown["统计行业大类"].map(
                    lambda name: [float(v) for v in added_trend.get(name, []) if pd.notna(v)]
                )
                shown = _place_trend_after(
                    shown,
                    {
                        "景气度趋势": "景气度",
                        "利润增长趋势": "利润增长率（最新月）",
                        "增加值趋势": "工业增加值增长率（最新月）",
                    },
                )
            # 统计局利润表各行业最新一期累计营业收入（亿元），插在景气度趋势列后
            revenue_map = (
                history.dropna(subset=["revenue_yi"])
                .sort_values("month")
                .groupby("stat_industry")["revenue_yi"]
                .last()
                .to_dict()
            )
            if revenue_map:
                shown["营业收入（亿元）"] = shown["统计行业大类"].map(revenue_map)
                if "景气度趋势" in shown.columns:
                    column = shown.pop("营业收入（亿元）")
                    shown.insert(shown.columns.get_loc("景气度趋势") + 1, "营业收入（亿元）", column)
                # 营业收入累计同比增长（统计局利润发布现成列，增加值未公布行业可用）
                revenue_yoy_map = (
                    history.dropna(subset=["revenue_yoy"])
                    .sort_values("month")
                    .groupby("stat_industry")["revenue_yoy"]
                    .last()
                    .to_dict()
                )
                if revenue_yoy_map:
                    shown["营收增长率（最新月）"] = shown["统计行业大类"].map(revenue_yoy_map)
                    if "营业收入（亿元）" in shown.columns:
                        column = shown.pop("营收增长率（最新月）")
                        shown.insert(
                            shown.columns.get_loc("营业收入（亿元）") + 1,
                            "营收增长率（最新月）",
                            column,
                        )
                revenue_trend = (
                    history.dropna(subset=["revenue_yi"])
                    .sort_values("month")
                    .groupby("stat_industry")["revenue_yi"]
                    .apply(lambda series: [float(v) for v in series.dropna().tolist()[-12:]])
                    .to_dict()
                )
                shown["营收趋势"] = shown["统计行业大类"].map(revenue_trend)
                shown = _place_trend_after(shown, {"营收趋势": "营业收入（亿元）"})
            trend_config = {
                name: st.column_config.LineChartColumn(name, width="small")
                for name in ("景气度趋势", "利润增长趋势", "增加值趋势", "营收趋势")
                if name in shown.columns
            }
            table_kwargs = {"width": "stretch", "hide_index": True, "column_config": trend_config}
            if not p_hist.empty:
                table_kwargs.update(
                    {"on_select": "rerun", "selection_mode": "single-row", "key": "prosperity_top_select"}
                )
            prosperity_event = st.dataframe(shown, **table_kwargs)
            picked_from_table = ""
            if not p_hist.empty:
                picked_rows = list(getattr(prosperity_event, "selection", {}).get("rows", []))
                if picked_rows:
                    picked_from_table = str(shown.iloc[picked_rows[0]]["统计行业大类"])
                    st.caption(
                        f"已联动：{picked_from_table}（点击行首复选框可切换，"
                        "趋势图见下方「单行业景气趋势」）"
                    )
            st.caption("单位为同比增长百分数；景气度为 0—100 的综合分，越高越景气。")
            st.caption(
                "景气度算法：取近 "
                + str(prosperity_window)
                + " 个月数据，对每个行业计算利润增长率均值、工业增加值增长率均值、"
                "最新月利润增长率、利润增长动量（最新月减均值）四项指标；"
                "每项在当期全部行业中换算成 0—100 的横截面百分位，"
                "再按 45%、25%、15%、15% 加权平均得到综合分。"
                "某项指标缺失时按中性分 50 计。"
            )
            st.caption(
                "各大类对应的申万三级行业、全部成分公司与排名靠前公司，已按大类归入"
                "「行业详情」对应子 tab 的「对应统计局大类」区块。"
            )

            if not p_hist.empty:
                st.divider()
                st.markdown("**单行业景气趋势**")
                p_hist_full = prosperity_history(history, window=prosperity_window, points=120)
                pick_options = sorted(p_hist_full["stat_industry"].unique())
                # selectbox 完全由 Session State 控制：默认取 TOP 首行，表格选中行会覆盖
                if "prosperity_trend_pick" not in st.session_state:
                    default_pick = str(shown["统计行业大类"].iloc[0])
                    st.session_state["prosperity_trend_pick"] = (
                        default_pick if default_pick in pick_options else pick_options[0]
                    )
                if picked_from_table and picked_from_table in pick_options:
                    # 仅当表格选中行发生变化时同步一次，避免覆盖用户在选择器里的手动切换
                    if st.session_state.get("prosperity_last_table_pick") != picked_from_table:
                        st.session_state["prosperity_last_table_pick"] = picked_from_table
                        st.session_state["prosperity_trend_pick"] = picked_from_table
                picked = st.selectbox(
                    "选择统计行业大类", pick_options, key="prosperity_trend_pick"
                )
                st.caption(f"当前查看：{picked}；点击上方 TOP 表行首复选框可联动切换。")
                _render_prosperity_trend_charts(
                    p_hist_full[p_hist_full["stat_industry"].eq(picked)].sort_values("month"),
                    revenue_rows=(
                        history[history["stat_industry"].eq(picked)]
                        .dropna(subset=["revenue_yi"])
                        .sort_values("month")[["month", "revenue_yi"]]
                    ),
                )

            st.divider()
            st.subheader("其它重点跟踪行业大类")
            latest_revenue_map = (
                history.dropna(subset=["revenue_yi"])
                .sort_values("month")
                .groupby("stat_industry")["revenue_yi"]
                .last()
            )
            top_by_revenue = (
                latest_revenue_map.sort_values(ascending=False).index.tolist()[:20]
            )
            top5_set = set(top_table["stat_industry"].head(5))
            hard_excluded = {"黑色金属冶炼和压延加工业", "农副食品加工业", "纺织业"}
            tracked_extra = tuple(
                name
                for name in top_by_revenue
                if name not in top5_set and name not in hard_excluded
            )[:12]
            st.caption(
                "除自动选出的最景气 TOP5 外，按最新一期营业收入排名 TOP20，"
                "剔除景气 TOP5 与黑色金属冶炼和压延加工业、农副食品加工业、纺织业，"
                "再按营收高低取前 12 个跟踪（明细同样见「行业详情」）："
            )
            extra_rows = prosperity_for_industries(
                history, window=prosperity_window, names=tracked_extra
            )
            extra_columns = [
                column
                for column in (
                    "stat_industry",
                    "prosperity_score",
                    "profit_yoy_latest",
                    "value_added_yoy_latest",
                    "profit_momentum",
                    "months",
                    "latest_month",
                    "data_note",
                )
                if column in extra_rows.columns
            ]
            st.dataframe(
                extra_rows[extra_columns].rename(
                    columns={
                        "stat_industry": "统计行业大类",
                        "prosperity_score": "景气度",
                        "profit_yoy_latest": "利润增长率（最新月）",
                        "value_added_yoy_latest": "工业增加值增长率（最新月）",
                        "profit_momentum": "利润增长动量",
                        "months": "有效月数",
                        "latest_month": "最新月份",
                        "data_note": "数据说明",
                    }
                ),
                width="stretch",
                hide_index=True,
            )

    st.divider()
    st.subheader("申万三级行业目录")
    st.caption(
        f"内置申万三级行业共 {len(taxonomy)} 个，快照日期 {SNAPSHOT_DATE}，"
        f"来源 {SOURCE_URL}。行业名称与官方编码不必完全一致，模糊匹配会自动对齐。"
    )
    shown_taxonomy = search_industries("", limit=1000).copy()
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
        width="stretch",
        hide_index=True,
    )


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
    st.altair_chart(chart, width="stretch")


LIQUIDITY_WEIGHTS_FILE = (
    Path(__file__).resolve().parent / "data" / "liquidity" / "custom_weights.json"
)
LIQUIDITY_WEIGHT_SLIDER_MAX = 0.30


def _load_custom_liquidity_weights() -> dict[str, float] | None:
    """读取持久化的自定义权重；与默认完全一致或文件缺失时返回 None。"""
    if not LIQUIDITY_WEIGHTS_FILE.exists():
        return None
    try:
        payload = json.loads(LIQUIDITY_WEIGHTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    weights = {
        metric_id: float(payload[metric_id])
        for metric_id in METRICS
        if metric_id in payload and payload[metric_id] is not None
    }
    if len(weights) != len(METRICS):
        return None
    if all(
        abs(weights[metric_id] - float(spec["weight"])) < 1e-9
        for metric_id, spec in METRICS.items()
    ):
        return None
    return weights


def _persist_custom_liquidity_weights(weights: dict[str, float] | None) -> None:
    """保存自定义权重；传 None 表示回到默认（删除文件）。"""
    if weights is None:
        LIQUIDITY_WEIGHTS_FILE.unlink(missing_ok=True)
        return
    LIQUIDITY_WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIQUIDITY_WEIGHTS_FILE.write_text(
        json.dumps(weights, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _weight_slider_key(metric_id: str) -> str:
    return f"liq_w_{metric_id}"


def _persist_weight_sliders() -> None:
    """slider on_change 回调：把当前滑块值写盘，全部等于默认时清除文件。"""
    weights = {
        metric_id: float(st.session_state[_weight_slider_key(metric_id)])
        for metric_id in METRICS
    }
    defaults = {metric_id: float(spec["weight"]) for metric_id, spec in METRICS.items()}
    if all(abs(weights[key] - defaults[key]) < 1e-9 for key in weights):
        _persist_custom_liquidity_weights(None)
    else:
        _persist_custom_liquidity_weights(weights)


def _reset_liquidity_weights() -> None:
    """恢复默认：清文件、把每个滑块的 session_state 重置为默认值。"""
    _persist_custom_liquidity_weights(None)
    for metric_id, spec in METRICS.items():
        st.session_state[_weight_slider_key(metric_id)] = float(spec["weight"])


def _total_weight_table(weights: dict[str, float] | None = None) -> pd.DataFrame:
    """流动性总指数的全部成分及其权重（绝对权重，不分列组内占比）。"""
    rows = [
        {"指标": str(spec["label"]), "权重": f"{effective_weight(metric_id, weights):.0%}"}
        for metric_id, spec in METRICS.items()
    ]
    return pd.DataFrame(rows)


def _attach_group_share(
    components: pd.DataFrame, group: str, weights: dict[str, float] | None = None
) -> pd.DataFrame:
    """在子指数成分表中并入组内占比：在「方向」列后插入「在本指数中权重」。"""
    specs = {mid: spec for mid, spec in METRICS.items() if str(spec["group"]) == group}
    group_total = sum(effective_weight(mid, weights) for mid in specs)
    shares = {
        str(spec["label"]): effective_weight(mid, weights) / group_total
        for mid, spec in specs.items()
    }
    frame = components.copy().rename(columns={"权重": "在流动性总指数中权重"})
    frame.insert(
        frame.columns.get_loc("方向") + 1,
        "在本指数中权重",
        frame["指标"].map(shares).map(lambda value: f"{value:.0%}" if pd.notna(value) else "—"),
    )
    return frame


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


with customs_tab:
    st.subheader("海关重点出口商品量值")
    st.caption(
        "数据源：海关总署月度公报「(5) China's Major Exports by Quantity and Value（人民币值）」，"
        "与中文站「重点出口商品量值表」同源，金额单位亿元；同比为累计金额比上年同期（±%）。"
        "中文站有反爬限制，自动抓取走同源英文站；若失败可下载中文站文章页另存为 HTML 上传导入。"
    )
    customs_frame = load_customs()
    customs_cols = st.columns([1, 2, 1])
    with customs_cols[0]:
        if st.button(
            "从海关总署更新",
            key="customs_update",
            use_container_width=True,
            type="primary",
        ):
            with st.spinner("正在抓取海关月度公报……"):
                try:
                    customs_frame = update_customs(months=13)
                    latest_month = customs_frame["month"].max()
                    st.success(f"已更新至 {latest_month}，共 {len(customs_frame)} 行。")
                except CustomsError as exc:
                    st.warning(f"自动抓取失败：{exc}")
    with customs_cols[2]:
        customs_upload = st.file_uploader(
            "上传量值表文章 HTML（兜底）", type=("html", "htm"), key="customs_upload"
        )
        if customs_upload is not None:
            try:
                customs_frame = import_customs_article(
                    customs_upload.getvalue().decode("utf-8", errors="ignore")
                )
                st.success("已导入上传文件。")
            except (CustomsError, UnicodeDecodeError) as exc:
                st.error(f"导入失败：{exc}")
    if customs_frame.empty:
        st.info("暂无海关数据：点击「从海关总署更新」抓取，或上传文章页 HTML 导入。")
    else:
        customs_months = sorted(customs_frame["month"].unique())
        # 同比曲线固定展示重点科技与装备类商品，避免图例过多
        yoy_focus_items = (
            "集成电路", "光电技术", "印刷电路", "生命科学技术",
            "医疗仪器及器械", "变压器", "蓄电池", "汽车", "通用机械设备",
        )
        yoy_long = customs_frame[
            customs_frame["commodity_cn"].isin(yoy_focus_items)
        ].dropna(subset=["export_cum_yoy_pct"])
        yoy_chart = (
            alt.Chart(yoy_long)
            .mark_line(point=True)
            .encode(
                x=alt.X("month:T", title=None),
                y=alt.Y("export_cum_yoy_pct:Q", title="累计金额同比(%)"),
                color=alt.Color("commodity_cn:N", title="商品", sort=list(yoy_focus_items)),
                tooltip=[
                    alt.Tooltip("month:T", title="月份"),
                    alt.Tooltip("commodity_cn:N", title="商品"),
                    alt.Tooltip("export_cum_yoy_pct:Q", title="累计金额同比(%)", format="+.1f"),
                ],
            )
            .properties(width="container", height=360)
            .configure_view(stroke=None)
        )
        st.altair_chart(yoy_chart, width="stretch")
        st.caption(
            "重点出口商品累计金额同比走势（1—N 月累计，跨年 2 月重新累计；"
            "固定展示集成电路、光电技术、印刷电路、生命科学技术、医疗仪器及器械、"
            "变压器、蓄电池、汽车、通用机械设备）。"
        )
        customs_pick_cols = st.columns([2, 1])
        with customs_pick_cols[0]:
            picked_commodities = st.multiselect(
                "查看累计金额趋势的商品",
                sorted(customs_frame["commodity_cn"].unique()),
                default=["高新技术产品", "集成电路", "汽车"],
                key="customs_amount_pick",
            )
        if picked_commodities:
            amount_long = customs_frame[customs_frame["commodity_cn"].isin(picked_commodities)]
            amount_chart = (
                alt.Chart(amount_long)
                .mark_line(point=True)
                .encode(
                    x=alt.X("month:T", title=None),
                    y=alt.Y("export_cum_amount_yi:Q", title="累计金额(亿元)"),
                    color=alt.Color("commodity_cn:N", title="商品"),
                    tooltip=[
                        alt.Tooltip("month:T", title="月份"),
                        alt.Tooltip("commodity_cn:N", title="商品"),
                        alt.Tooltip("export_cum_amount_yi:Q", title="累计金额(亿元)", format=",.1f"),
                    ],
                )
                .properties(width="container", height=300)
                .configure_view(stroke=None)
            )
            st.altair_chart(amount_chart, width="stretch")
        latest_customs = customs_frame[customs_frame["month"] == customs_months[-1]].copy()
        latest_customs["wind_industry"] = latest_customs["commodity_cn"].map(WIND_INDUSTRY_MAP)
        # 同类相邻：组间按 CUSTOMS_GROUPS 固定顺序，组内按当月出口额从大到小
        group_of = {
            commodity: group_name
            for group_name, members in CUSTOMS_GROUPS
            for commodity in members
        }
        group_order = {
            commodity: index
            for index, (_, members) in enumerate(CUSTOMS_GROUPS)
            for commodity in members
        }
        latest_customs["category"] = latest_customs["commodity_cn"].map(group_of)
        latest_customs["_group_order"] = latest_customs["commodity_cn"].map(group_order)
        latest_customs = latest_customs.sort_values(
            ["_group_order", "export_month_amount_yi"],
            ascending=[True, False],
        )
        st.markdown(
            f"**最新一期明细（{customs_months[-1]}，同类相邻、组内按当月出口额从大到小）**"
        )
        st.dataframe(
            latest_customs[
                [
                    "category",
                    "commodity_cn",
                    "export_month_amount_yi",
                    "export_cum_amount_yi",
                    "export_cum_yoy_pct",
                    "wind_industry",
                ]
            ].rename(
                columns={
                    "category": "类别",
                    "commodity_cn": "商品",
                    "export_month_amount_yi": "当月金额（亿元）",
                    "export_cum_amount_yi": "累计金额（亿元）",
                    "export_cum_yoy_pct": "累计金额同比（%）",
                    "wind_industry": "Wind行业映射",
                }
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "当月金额（亿元）": st.column_config.NumberColumn(format="%.1f"),
                "累计金额（亿元）": st.column_config.NumberColumn(format="%.1f"),
                "累计金额同比（%）": st.column_config.NumberColumn(format="+%.1f"),
            },
        )
        st.caption(
            "覆盖商品：稀土、铝材、高新技术产品、通用机械设备、自动数据处理设备、"
            "集成电路、汽车、汽车零配件、手机、家用电器、医疗仪器及器械、船舶（机电产品为上述分项的汇总项，不单列）。"
            f"当前覆盖月份：{customs_months[0]} 至 {customs_months[-1]}。"
        )
        st.caption(
            "「高新技术产品」口径说明：《高新技术产品目录》九个大类的汇总项，（海关统计商品目录分类码 187—195）——187 生物技术"
            "；188 生命科学技术；189 光电技术；190 计算机与通信技术；191 电子技术；192 计算机集成制造技术；193 材料技术；194 航空航天技术；"
            "195 其他技术。注意其与机电产品、集成电路、自动数据处理设备等分项存在交叉重叠，不能直接加总。"
        )
        st.caption(
            "官方完整 HS 编码清单参见链接 "
            "[http://www.customs.gov.cn/customs/2026-03/10/article_2026031011493759836.html]"
            "(http://www.customs.gov.cn/customs/2026-03/10/article_2026031011493759836.html)"
        )


with liquidity_tab:
    st.caption(
        "流动性指数把指标同向化（宽松=数值变大）后按滚动窗口 Z-score 标准化，再分组合成。"
        "短期资金价格与市场风偏纳入 DR007、两融余额及其同比，反映银行间短端资金价格与场内杠杆；"
        "信用货币长期利率与增速纳入 M1-M2 剪刀差、M2 同比、社融存量同比和十年期国债收益率，"
        "分别反映实体货币信用扩张与长端无风险利率。"
        "流动性指数 = 短期 × 组权重 + 信用货币长期利率与增速 × 组权重，组权重为组内指标默认权重之和"
        "（默认 0.37×短期 + 0.63×信用货币长期利率与增速）。"
        "各指标默认权重见下方「成分参数及权重」表，可在「自定义指标权重」中动态调整，"
        "调整后所有指数与成分表即时按新权重重算。"
        "月度数据按次月 10 日生效，避免使用未公布数据。"
        "社融存量与存量同比从央行「调查统计司-统计数据-社会融资规模」年度栏目自动解析，"
        "与央行公众号月度报告同源。"
    )
    if st.button(
        "从公开数据源更新流动性数据",
        width="content",
        key="liquidity_update",
        type="primary",
    ):
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
        # ---- 自定义权重：滑块初值 = 持久化文件或默认值；改动即写盘，全部默认时清文件 ----
        custom_weights = _load_custom_liquidity_weights()
        with st.container(key="liq_weight_box"):
            with st.expander(
                "自定义指标权重" + ("（已启用自定义）" if custom_weights else ""),
                expanded=custom_weights is not None,
            ):
                st.caption(
                    "拖动调整各指标权重（0 ~ 0.30，组内自动归一化），"
                    "三个指数与成分表即时按新权重重算；设置自动保存到本机，刷新后仍生效。"
                )
                for metric_id, spec in METRICS.items():
                    if st.session_state.get(_weight_slider_key(metric_id)) is None:
                        st.session_state[_weight_slider_key(metric_id)] = (
                            custom_weights or {}).get(metric_id, float(spec["weight"])
                        )
                    st.slider(
                        f"{spec['label']}（{'越高越宽松' if int(spec['direction']) >= 0 else '越低越宽松'}）",
                        min_value=0.0,
                        max_value=LIQUIDITY_WEIGHT_SLIDER_MAX,
                        step=0.01,
                        key=_weight_slider_key(metric_id),
                        on_change=_persist_weight_sliders,
                    )
                st.button(
                    "恢复默认权重",
                    key="liq_w_reset",
                    on_click=_reset_liquidity_weights,
                    width="content",
                    type="primary",
                )
        custom_weights = _load_custom_liquidity_weights() or None
        if custom_weights:
            st.info(
                "已启用自定义权重（自动保存，重启后仍生效）："
                + "、".join(
                    f"{METRICS[mid]['label']} {effective_weight(mid, custom_weights):.0%}"
                    for mid in METRICS
                )
            )

        (
            liquidity_indices,
            liquidity_components,
            liquidity_notes,
        ) = compute_liquidity_indices(liquidity_metrics, weights=custom_weights)
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
            st.markdown("**成分参数及权重**")
            st.dataframe(_total_weight_table(custom_weights), width="stretch", hide_index=True)
            st.caption(
                "权重为该指标在流动性总指数中的绝对权重；"
                "子指数内部占比见下方两个子指数的成分表。"
            )

            st.divider()
            st.subheader("短期资金价格与市场风偏")
            if "short_term_index" in liquidity_indices:
                render_liquidity_block(
                    liquidity_indices["short_term_index"],
                    label=INDEX_LABELS["short_term_index"],
                    compare_window=52,
                    monthly=False,
                )
            short_term_components = _attach_group_share(
                liquidity_components[
                    liquidity_components["分组"].eq("短期资金价格与市场风偏")
                ].drop(columns=["metric", "分组"]),
                "short_term",
                custom_weights,
            )
            st.dataframe(short_term_components, width="stretch", hide_index=True)
            st.caption("「最新值」为原始单位：DR007、两融余额同比为 %，两融余额为亿元。")

            st.divider()
            st.subheader("信用货币长期利率与增速")
            if "money_index" in liquidity_indices:
                render_liquidity_block(
                    liquidity_indices["money_index"],
                    label=INDEX_LABELS["money_index"],
                    compare_window=12,
                    monthly=True,
                )
            money_components = _attach_group_share(
                liquidity_components[
                    liquidity_components["分组"].eq("信用货币长期利率与增速")
                ].drop(columns=["metric", "分组"]),
                "money",
                custom_weights,
            )
            st.dataframe(money_components, width="stretch", hide_index=True)
            st.caption("十年期国债收益率为周频数据，其余三项为月频数据；月度数据按次月 10 日生效。")

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
                st.line_chart(display_series, width="stretch")
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
                width="stretch",
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

        # ---------- 按统计局制造业大类分 tab：重点子行业 + 其余子行业按需拉取 ----------
        st.caption(UNIT_CAPTION)
        # 三级公司太少的行业按二级聚合（如工业金属=铝+铜+铅锌）作为一个重点子行业排名
        l2_l3_map = l2_focus_l3_map()
        l2_names = l2_focus_names()
        l2_group_ids = l2_group_l3_ids()
        l2_l3_ids = set(l2_l3_map)
        focus_raw = all_industry_financials[
            all_industry_financials["industry_l3_id"].astype(str).isin(focus_ids + list(l2_l3_ids))
        ].copy()
        l2_mask = focus_raw["industry_l3_id"].astype(str).isin(l2_l3_ids)
        focus_raw.loc[l2_mask, "industry_l3_id"] = focus_raw.loc[
            l2_mask, "industry_l3_id"
        ].astype(str).map(lambda value: l2_l3_map[value][1])
        focus_raw.loc[l2_mask, "industry_l3_name"] = focus_raw.loc[
            l2_mask, "industry_l3_id"
        ].map(lambda value: l2_names.get(value, value))
        focus_id_set = {str(item) for item in focus_ids} | set(l2_names)
        focus_ranking = pd.DataFrame()
        focus_notes: list[str] = []
        if not focus_raw.empty:
            try:
                focus_ranking, focus_notes = rank_industry_financials(
                    focus_raw,
                    as_of_date=detail_as_of,
                    top_n=top_n,
                    minimum_coverage=minimum_coverage,
                )
            except Exception as exc:
                st.error(f"计算行业排名失败：{exc}")

        catalog_frame = builtin_industry_frame()
        l1_by_l3 = dict(
            zip(
                catalog_frame["industry_l3_id"].astype(str),
                catalog_frame["industry_l1_name"].astype(str),
            )
        )
        sector_titles = [str(spec["title"]) for spec in DETAIL_SECTOR_TABS] + ["其它行业"]
        # 各 tab 的「自定义评分权重」折叠区共用这套强调样式
        st.markdown(CUSTOM_WEIGHT_EXPANDER_CSS, unsafe_allow_html=True)
        sector_specs = {str(spec["title"]): spec for spec in DETAIL_SECTOR_TABS}

        def claim_tab(l3_id: str) -> str:
            """三级行业归属的子 tab：按列表顺序先到先得，未命中归「其它行业」。"""
            l1_name = l1_by_l3.get(l3_id, "")
            for spec in DETAIL_SECTOR_TABS:
                whitelist = tuple(str(item) for item in spec["claimed_l3_ids"])  # type: ignore[union-attr]
                if whitelist:
                    if l3_id in whitelist:
                        return str(spec["title"])
                elif l1_name in tuple(spec["l1_names"]):  # type: ignore[arg-type]
                    return str(spec["title"])
            return "其它行业"

        sector_members: dict[str, list[dict[str, object]]] = {title: [] for title in sector_titles}
        member_ids: set[str] = set()

        def add_member(l3_id: str, name: str, count: int) -> None:
            """登记一个三级行业；同一行业只登记一次，二级聚合的行业不进面板。"""
            if l3_id in member_ids or l3_id in l2_l3_ids:
                return
            member_ids.add(l3_id)
            sector_members[claim_tab(l3_id)].append(
                {"id": l3_id, "name": name, "count": count}
            )

        for row in coverage.sort_values("industry_l3_id").itertuples():
            add_member(
                str(row.industry_l3_id), str(row.industry_l3_name), int(row.company_count)
            )

        # 统计局大类对应的三级行业：还没有成分快照的也列出来（标注「暂无成分」）
        pending_members: dict[str, tuple[str, str]] = {}

        # 统计局各大类按「其映射的三级行业多数落在哪个 tab」归位
        stat_records: dict[str, list[dict[str, object]]] = {title: [] for title in sector_titles}
        stat_history = load_history()
        if not stat_history.empty:
            stat_history = stat_history[stat_history["stat_industry"].isin(PRIMARY_STAT_INDUSTRIES)]
        if not stat_history.empty:
            _, stat_full = prosperity_board(
                stat_history, window=prosperity_window, top_n=prosperity_top_n
            )
            for stat_row in stat_full.itertuples(index=False):
                stat_name = str(stat_row.stat_industry)
                votes: dict[str, int] = {}
                for l3_id in mapped_industry_ids(stat_name):
                    claim = claim_tab(str(l3_id))
                    votes[claim] = votes.get(claim, 0) + 1
                # 大类的对应三级行业即使还没有成分缓存，也列出来供按需拉取
                for industry_row in corresponding_industries(stat_name).itertuples(
                    index=False
                ):
                    l3_id = str(industry_row.industry_l3_id)
                    claim = claim_tab(l3_id)
                    # 只有紧凑 tab（电子通信/创新药）补全到全部对应行业，
                    # 其它 tab 仍只列已有成分快照的行业，避免下拉里堆满不可用项
                    if not sector_specs.get(claim, {}).get("compact_focus"):
                        continue
                    if l3_id in l2_l3_ids:
                        continue
                    if l3_id in member_ids or l3_id in pending_members:
                        continue
                    pending_members[l3_id] = (str(industry_row.industry_l3_name), claim)
                if not votes:
                    continue
                tab_of = max(votes, key=lambda key: votes[key])
                stat_records[tab_of].append(
                    {
                        "stat_industry": stat_name,
                        "l3_ids": tuple(mapped_industry_ids(stat_name)),
                        "score": None
                        if pd.isna(stat_row.prosperity_score)
                        else float(stat_row.prosperity_score),
                        "profit_latest": None
                        if pd.isna(stat_row.profit_yoy_latest)
                        else float(stat_row.profit_yoy_latest),
                        "sort_key": 0.0
                        if pd.isna(stat_row.prosperity_score)
                        else float(stat_row.prosperity_score),
                    }
                )
            for items in stat_records.values():
                items.sort(key=lambda item: -float(item["sort_key"]))
        if pending_members:
            snapshot_counts = (
                _demo_constituents_for(list(pending_members))
                .groupby("industry_l3_id")
                .size()
                .to_dict()
            )
            for l3_id, (name, claim) in pending_members.items():
                add_member(l3_id, name, int(snapshot_counts.get(l3_id, 0)))

        def render_sector_member_panel(
            *,
            members: list[dict[str, object]],
            featured: tuple[str, ...],
            key_prefix: str,
            panel_key: str,
        ) -> None:
            """本大类其余子行业：选择、按需拉取、行业内部排名与成分表。"""
            global all_industry_financials
            member_map = {str(member["id"]): member for member in members}
            ordered_ids = [mid for mid in featured if mid in member_map]
            # 已有成分公司的排在前面，尚无成分快照的（可选但暂时拉不了）排后面
            ordered_ids += [
                mid
                for mid in member_map
                if mid not in ordered_ids and int(member_map[mid]["count"]) > 0
            ]
            ordered_ids += [mid for mid in member_map if mid not in ordered_ids]
            industry_labels = {
                ("★ " if mid in featured else "")
                + f"{member_map[mid]['name']}（{mid}，"
                + (
                    f"{int(member_map[mid]['count'])} 家）"
                    if int(member_map[mid]["count"]) > 0
                    else "暂无成分）"
                ): mid
                for mid in ordered_ids
            }
            with st.container(border=True, key=panel_key):
                st.markdown(
                    """<style>
                    .st-key-PANELKEY [data-testid="stSelectbox"] div[data-baseweb="select"] {
                        border: 2px solid #2563eb !important;
                        border-radius: 0.6rem;
                        background-color: #eff6ff;
                        box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
                    }
                    .st-key-PANELKEY [data-testid="stSelectbox"] label p {
                        font-weight: 700;
                        color: #1d4ed8;
                    }
                    </style>""".replace("PANELKEY", panel_key),
                    unsafe_allow_html=True,
                )
                controls = st.columns([3, 1, 1])
                chosen_label = controls[0].selectbox(
                    "选择子行业", list(industry_labels), key=f"{key_prefix}_choice"
                )
                detail_industry_id = industry_labels[chosen_label]
                detail_industry_name = str(member_map[detail_industry_id]["name"])
                detail_constituents = _demo_constituents_for([detail_industry_id])
                run_industry_sync = controls[2].button(
                    "拉取近一年财报",
                    type="primary",
                    width="stretch",
                    key=f"{key_prefix}_sync_btn",
                    disabled=detail_constituents.empty,
                )
                force_resync = st.checkbox(
                    "忽略本地缓存，强制重新联网核对",
                    value=False,
                    key=f"{key_prefix}_force",
                    disabled=detail_constituents.empty,
                )
                if detail_constituents.empty:
                    st.caption(
                        f"「{detail_industry_name}」还没有成分公司快照，暂时无法按公司拉取财报；"
                        "可先运行 `python tools/freeze_sw_constituents.py` 从公开来源补齐。"
                    )
                else:
                    st.caption(
                        f"点上面的按钮会先在本地检查「{detail_industry_name}」的 "
                        f"{len(detail_constituents)} 家成份公司是否都已取得最新报告期数据："
                        "齐全则不联网并提示，只缺哪几家就只补拉那几家。"
                        "勾选上面的复选框可强制重新联网核对（仍不会重复解析已缓存的公告）。"
                    )

                if not detail_constituents.empty:
                    st.markdown(
                        f"**行业成分公司（{len(detail_constituents)} 家，公开快照 {SNAPSHOT_DATE}）**"
                    )
                    st.dataframe(
                        detail_constituents.rename(
                            columns={
                                "company_id": "公司代码",
                                "company_name": "公司名称",
                                "include_date": "纳入时间",
                            }
                        )[_demo_visible_labels(["公司代码", "公司名称", "纳入时间"])],
                        width="stretch",
                        hide_index=True,
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
                            st.warning(
                                f"有 {len(industry_errors)} 条检索或提取失败，请用命令行查看明细。"
                            )
                        all_industry_financials = load_all_industry_financials()
                    except Exception as exc:
                        st.error(f"拉取失败：{exc}")

            industry_raw = all_industry_financials[
                all_industry_financials["industry_l3_id"].astype(str).eq(str(detail_industry_id))
            ].copy()
            if industry_raw.empty:
                if detail_constituents.empty:
                    st.info(
                        f"{detail_industry_name} 还没有成分公司快照，补齐成分后才能拉取财报。"
                    )
                else:
                    st.info(
                        f"{detail_industry_name} 还没有财报缓存。点击上方「拉取近一年财报」，"
                        f"或在命令行执行 `python online_cli.py industry sync --industry "
                        f"{detail_industry_id} --as-of {detail_as_of:%Y-%m-%d}`。"
                    )
            else:
                latest_announcement = pd.to_datetime(
                    industry_raw["announcement_date"], errors="coerce"
                ).max()
                st.caption(
                    f"本地已缓存 {len(industry_raw)} 条定期报告记录，覆盖 "
                    f"{industry_raw['company_id'].nunique()} 家公司，"
                    f"最新公告日 {latest_announcement:%Y-%m-%d}。"
                )
                st.caption(f"排名按公告日生成点时计算，默认每个三级行业取前 {top_n} 名。")
                period_options = available_report_periods(industry_raw, detail_as_of)
                period_choice = st.selectbox(
                    "报告期",
                    options=["自动（最新共同报告期）", *period_options],
                    format_func=lambda value: (
                        "自动（最新共同报告期）"
                        if value == "自动（最新共同报告期）"
                        else pd.Timestamp(value).strftime("%Y-%m-%d")
                    ),
                    key=f"{key_prefix}_period",
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
                    download_key=f"detail_ranking_{key_prefix}",
                )

            # 面板内已拉取财报的其它子行业常驻展示，切换选择框不会消失；
            # 数据都在本地缓存里，点开即可重看排名
            cached_members = [
                mid
                for mid in ordered_ids
                if mid != detail_industry_id
                and not all_industry_financials[
                    all_industry_financials["industry_l3_id"].astype(str).eq(mid)
                ].empty
            ]
            if cached_members:
                st.markdown("**已拉取财报的其它子行业（常驻显示）**")
                for member_id in cached_members:
                    member_raw = all_industry_financials[
                        all_industry_financials["industry_l3_id"].astype(str).eq(member_id)
                    ].copy()
                    member_latest = pd.to_datetime(
                        member_raw["announcement_date"], errors="coerce"
                    ).max()
                    member_name = str(member_map[member_id]["name"])
                    with st.expander(
                        f"{member_name}（{member_id}）· "
                        f"{member_raw['company_id'].nunique()} 家 · "
                        f"最新公告 {member_latest:%Y-%m-%d}",
                        expanded=False,
                    ):
                        try:
                            member_ranking, member_notes = rank_industry_financials(
                                member_raw,
                                as_of_date=detail_as_of,
                                top_n=top_n,
                                minimum_coverage=minimum_coverage,
                            )
                            render_industry_ranking(
                                member_ranking,
                                member_notes,
                                as_of_date=detail_as_of,
                                sort_by=detail_sort_by,
                                download_label=f"下载 {member_name} 财报排名",
                                download_key=f"detail_ranking_{key_prefix}_{member_id}",
                            )
                        except Exception as exc:
                            st.error(f"计算 {member_name} 排名失败：{exc}")

        for tab_index, (tab_ctx, sector_title) in enumerate(
            zip(st.tabs(sector_titles), sector_titles)
        ):
            with tab_ctx:
                spec = sector_specs.get(sector_title, {})
                featured = tuple(str(item) for item in spec.get("featured", ()))  # type: ignore[union-attr]
                compact_focus = bool(spec.get("compact_focus"))
                tab_focus_ids = [fid for fid in featured if fid in focus_id_set]

                # ---- 本 tab 的自定义评分权重（按 tab 隔离，改动即时重算本页排名） ----
                tab_industry_ids = {str(item["id"]) for item in sector_members.get(sector_title, [])} | {
                    str(item) for item in tab_focus_ids
                }
                tab_model_names = sorted(
                    {
                        str(model)
                        for model in ranking.loc[
                            ranking["industry_l3_id"].astype(str).isin(tab_industry_ids), "rank_model"
                        ].dropna()
                    }
                ) or ["general"]
                tab_weight_store = _load_model_weights()
                tab_override = tab_weight_store.get(sector_title) or {}
                with st.container(key=f"mw_box_{tab_index}"):
                    with st.expander(
                        "自定义评分权重" + ("（本页已启用自定义）" if tab_override else ""),
                        expanded=bool(tab_override),
                        icon=":material/tune:",
                    ):
                        st.caption(
                            "调整本页公司排名的指标权重，各模型内自动归一到 100%；改动即时重算本页"
                            "「重点子行业」与「对应统计局大类」的综合得分与名次，不影响其它 tab。"
                            "设置自动保存到本机。"
                        )
                        for model in tab_model_names:
                            st.markdown(f"**{MODEL_SPECS[model].label}**")
                            for metric, (default_weight, higher_is_better) in MODEL_SPECS[
                                model
                            ].metrics.items():
                                key = _model_weight_key(sector_title, model, metric)
                                if st.session_state.get(key) is None:
                                    st.session_state[key] = float(
                                        (tab_override.get(model) or {}).get(metric, default_weight)
                                    )
                                st.slider(
                                    f"{MODEL_METRIC_LABELS.get(metric, metric)}"
                                    f"（{'越高越好' if higher_is_better else '越低越好'}）",
                                    min_value=0.0,
                                    max_value=MODEL_WEIGHT_SLIDER_MAX,
                                    step=0.01,
                                    key=key,
                                    on_change=_save_tab_model_weights,
                                    args=(sector_title, model),
                                )
                    st.button(
                        "恢复本页默认权重",
                        key=f"mw_reset_{sector_title}",
                        on_click=_reset_tab_model_weights,
                        args=(sector_title, tab_model_names),
                        width="content",
                        type="primary",
                    )
                tab_override = _load_model_weights().get(sector_title) or {}
                if tab_override:
                    st.info(
                        "本页已启用自定义评分权重，下方排名按新权重重算（其它 tab 不受影响）。"
                    )
                    tab_ranking = score_snapshot(
                        snapshot,
                        top_n=top_n,
                        minimum_coverage=minimum_coverage,
                        weights=tab_override,
                    )
                    tab_focus_raw = (
                        focus_raw[focus_raw["industry_l3_id"].astype(str).isin(tab_focus_ids)]
                        if tab_focus_ids and not focus_raw.empty
                        else pd.DataFrame()
                    )
                    if tab_focus_raw.empty:
                        tab_focus_ranking = pd.DataFrame()
                        tab_focus_notes: list[str] = []
                    else:
                        tab_focus_ranking, tab_focus_notes = rank_industry_financials(
                            tab_focus_raw,
                            as_of_date=detail_as_of,
                            top_n=top_n,
                            minimum_coverage=minimum_coverage,
                            weights=tab_override,
                        )
                else:
                    tab_ranking = ranking
                    tab_focus_ranking = focus_ranking
                    tab_focus_notes = focus_notes

                def render_focus_rankings() -> None:
                    """本 tab 已预拉取财报的重点子行业，按展示顺序依次排开。"""
                    subset = tab_focus_ranking[
                        tab_focus_ranking["industry_l3_id"].astype(str).isin(tab_focus_ids)
                    ]
                    if subset.empty:
                        st.info("这些子行业还没有可排名的财报缓存，可在下方逐个拉取。")
                        return
                    tab_group_titles = {
                        fid: focus_group_titles[fid]
                        for fid in tab_focus_ids
                        if fid in focus_group_titles
                    }
                    tab_group_captions = {
                        title: focus_group_captions[title]
                        for title in set(tab_group_titles.values())
                        if title in focus_group_captions
                    }
                    # 只有一个主题（如「AI硬件」「创新药」）时标题与 tab 重复，省略
                    if len(set(tab_group_titles.values())) <= 1:
                        tab_group_titles = {}
                        tab_group_captions = {}
                    # 只保留本 tab 这些行业的备注，避免跨 tab 串条
                    subset_names = set(subset["industry_l3_name"].astype(str))
                    tab_notes = [
                        note
                        for note in tab_focus_notes
                        if note.partition("：")[0].strip() in subset_names
                    ]
                    render_industry_ranking(
                        subset,
                        tab_notes,
                        as_of_date=detail_as_of,
                        order={fid: position for position, fid in enumerate(tab_focus_ids)},
                        group_titles=tab_group_titles,
                        group_captions=tab_group_captions,
                        sort_by=detail_sort_by,
                        download_label="下载这些子行业的财报排名",
                        download_key=f"focus_ranking_{sector_title}",
                        # 报告期等备注放进各自子行业框内，避免堆在标题下
                        inline_notes=True,
                    )

                stat_items = stat_records.get(sector_title, [])
                if not stat_items:
                    st.markdown("**对应统计局大类（景气度与排名靠前公司）**")
                    st.caption("本地暂无该大类对应的统计局月度景气数据。")
                else:
                    st.markdown("**对应统计局大类（景气度与排名靠前公司）**")
                    st.caption(
                        "统计行业口径来自国家统计局，展开可见对应申万三级行业、"
                        "全部成分公司与该大类内排名靠前的公司；景气更新在「行业景气度」页签。"
                    )
                    # 对应三级行业互为子集的大类（如有色采选 ⊂ 冶炼）合并成一个框；
                    # spec 指定 merge_all_stats 时（如化工）全部大类合并，景气度以表后注释呈现
                    if spec.get("merge_all_stats"):
                        stat_groups: list[list[dict[str, object]]] = [stat_items]
                    else:
                        stat_groups = merge_stat_items(stat_items)
                    # 归属其它 tab 的统计大类（如军工电子Ⅲ 所在大类归了电子通信）
                    # 文档罗列的行业组（如国防军工 5 个，以
                    # data/swresearch/国防军工三级归属.csv 为准）补进框内清单：
                    # 只补统计大类映射未覆盖的行业，标题追加组标注
                    extra_group = spec.get("extra_l3_group") or {}  # type: ignore[union-attr]
                    extra_label = str(extra_group.get("label", "")) if extra_group else ""
                    extra_ids = tuple(extra_group.get("l3_ids", ())) if extra_group else ()
                    extra_industries = None
                    if extra_ids:
                        have_ids: set[str] = set()
                        for group in stat_groups:
                            for item in group:
                                frame = corresponding_industries(str(item["stat_industry"]))
                                have_ids.update(frame["industry_l3_id"].astype(str))
                        missing = [i for i in extra_ids if str(i) not in have_ids]
                        if missing:
                            catalog_frame = builtin_industry_frame()
                            extra_industries = catalog_frame[
                                catalog_frame["industry_l3_id"].astype(str).isin(missing)
                            ].copy()
                    for group in stat_groups:
                        render_stat_industry_block(
                            stat_items=group,
                            ranking_frame=tab_ranking,
                            key_suffix=(
                                f"detail_{sector_title}_"
                                + "_".join(str(item["stat_industry"]) for item in group)[:60]
                            ),
                            # 电子通信 tab 只有一个大类，直接展开，不再要求点击；
                            # 化工的合并大框同理默认展开，成分选择交给下方面板
                            expanded=compact_focus or bool(spec.get("merge_all_stats")),
                            show_constituents=not (
                                compact_focus or bool(spec.get("merge_all_stats"))
                            ),
                            # spec 指定的大类（如化学纤维）或整 tab（如机械设备）
                            # 不在框内重复列三级行业
                            show_l3_table=not (
                                bool(spec.get("hide_stat_l3_all"))
                                or any(
                                    str(item["stat_industry"])
                                    in spec.get("hide_stat_l3_tables", ())  # type: ignore[union-attr]
                                    for item in group
                                )
                            ),
                            footer_notes=bool(spec.get("merge_all_stats")),
                            group_title=spec.get("stat_group_title"),  # type: ignore[arg-type]
                            extra_industries=extra_industries,
                            extra_group_label=extra_label or None,
                            extra_group_count=len(extra_ids) if extra_ids else None,
                        )
                st.divider()
                if tab_focus_ids:
                    focus_header = st.columns([3, 1.2])
                    focus_header[0].markdown("**重点子行业（已预拉取财报）**")
                    if focus_header[1].button(
                        "刷新最新财报",
                        width="stretch",
                        key=f"focus_refresh_{sector_title}",
                        type="primary",
                        help=(
                            "重新联网核对这些子行业：哪家公司新披露了定期报告（如百济神州刚发财报），"
                            "就只补拉那几家，已有缓存不会重复解析。"
                        ),
                    ):
                        focus_refresh_progress = st.progress(0.0, text="准备刷新")
                        refreshed_industries: list[str] = []
                        refresh_error_count = 0
                        for position, focus_id in enumerate(tab_focus_ids, start=1):
                            group_l3_ids = l2_group_ids.get(focus_id)
                            focus_name = (
                                l2_names.get(focus_id)
                                or coverage_name_by_id.get(focus_id, focus_id)
                            )
                            focus_refresh_progress.progress(
                                (position - 1) / len(tab_focus_ids),
                                text=f"核对 {focus_name}（{position}/{len(tab_focus_ids)}）",
                            )
                            # 二级聚合行业逐三级核对刷新；普通行业按单个行业核对
                            for l3_id in group_l3_ids or (focus_id,):
                                status = check_industry_cache(l3_id, detail_as_of, lookback_days)
                                if not status.needs_network:
                                    continue
                                try:
                                    _, industry_errors = sync_industry_reports(
                                        industry=l3_id,
                                        as_of_date=detail_as_of,
                                        lookback_days=lookback_days,
                                        company_ids=list(status.pending_ids) or None,
                                    )
                                    if str(focus_name) not in refreshed_industries:
                                        refreshed_industries.append(str(focus_name))
                                    refresh_error_count += len(industry_errors)
                                except Exception as exc:
                                    st.warning(f"{focus_name} 刷新失败：{exc}")
                        focus_refresh_progress.progress(1.0, text="刷新完成")
                        if refreshed_industries:
                            tail = (
                                f"（{refresh_error_count} 条检索或解析失败，可用命令行查看明细）"
                                if refresh_error_count
                                else ""
                            )
                            refresh_message = (
                                "已补拉新披露财报的子行业：" + "、".join(refreshed_industries) + tail
                            )
                        else:
                            refresh_message = "这些子行业的财报缓存已是最新，没有需要补拉的公司。"
                        # rerun 会重建页面，先把结果存起来，重跑后展示
                        st.session_state[f"focus_refresh_msg_{sector_title}"] = refresh_message
                        st.rerun()
                    refresh_notice = st.session_state.pop(
                        f"focus_refresh_msg_{sector_title}", None
                    )
                    if refresh_notice:
                        st.success(refresh_notice)
                    render_focus_rankings()
                    st.divider()
                members = [
                    member
                    for member in sector_members[sector_title]
                    if str(member["id"]) not in focus_id_set
                ]
                if not members:
                    st.caption("该大类暂无已固化成分公司的子行业。")
                    continue
                st.markdown("**其余子行业（按需拉取）**")
                render_sector_member_panel(
                    members=members,
                    featured=featured,
                    key_prefix=f"sector_{sector_title}",
                    panel_key=f"sector_panel_{tab_index}",
                )


with quality_tab:
    ineligible = ranking[~ranking["eligible"]]
    st.write(f"因数据完整度低于 {minimum_coverage:.0%} 而未参与排名：{len(ineligible)} 家。")
    if not ineligible.empty:
        st.dataframe(display_frame(ineligible), width="stretch", hide_index=True)
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
    evidence_columns = _demo_visible_columns(evidence_columns)
    if "source_url" not in evidence_columns:
        if display_mode == "normal":
            st.info("演示或上传数据未包含公告来源；在线公告缓存会显示可追溯链接。")
        else:
            st.caption("演示模式下公告原文链接已隐藏，防止证券代码出现在录屏中。")
    else:
        evidence = ranking[evidence_columns].copy()
        if "extraction_confidence" in evidence:
            evidence["extraction_confidence"] = (evidence["extraction_confidence"] * 100).round(1)
        st.dataframe(evidence, width="stretch", hide_index=True)
        needs_review = ranking.get("needs_review", pd.Series(False, index=ranking.index))
        review_count = int(needs_review.astype("boolean").fillna(False).sum())
        if review_count:
            st.warning(f"有 {review_count} 条记录提取置信度较低，应打开原公告人工复核后再使用。")

    if not all_industry_financials.empty:
        st.subheader("三级行业财报公告原文")
        industry_evidence = all_industry_financials.sort_values(
            ["industry_l3_name", "company_id", "report_period"]
        )[
            _demo_visible_columns(
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
            )
        ].copy()
        if "extraction_confidence" in industry_evidence:
            industry_evidence["extraction_confidence"] = (
                industry_evidence["extraction_confidence"] * 100
            ).round(1)
        st.dataframe(
            industry_evidence,
            width="stretch",
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
    st.dataframe(weights, width="stretch", hide_index=True)
