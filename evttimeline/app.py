"""事件时间线 · 独立 Streamlit 小程序

启动：
    streamlit run evttimeline/app.py --server.port 8502

功能：
- 主题管理（行业发展周期 / 公司经营状态 / 其它）
- 手工录入与编辑事件，形成可回溯的时间线
- 自动采集巨潮公告（含临时公告），勾选后入栈
- 关键词 / 标签 / 时间范围检索，按年分组的脉络整理
- 导出 Markdown、纯文本、Word（.docx）、JSON 备份
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import collector  # noqa: E402
import exporter  # noqa: E402
import storage  # noqa: E402

st.set_page_config(page_title="事件时间线", layout="wide")

# ---------------------------------------------------------------- 样式


st.markdown(
    """
    <style>
    .evt-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
    .evt-head h1 { margin-bottom: 0; }
    .evt-meta { color: #6b7280; font-size: 13px; }
    .evt-tag { display: inline-block; padding: 1px 8px; margin-right: 4px; border-radius: 10px;
      background: #eef2f7; color: #334155; font-size: 12px; }
    .evt-imp-4, .evt-imp-5 { color: #b45309; font-weight: 600; }
    .evt-body { color: #374151; font-size: 14px; line-height: 1.6; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _tag_html(tags: list[str]) -> str:
    return "".join(f'<span class="evt-tag">{tag}</span>' for tag in tags or [])


def _split_tags(text: str) -> list[str]:
    parts = []
    for chunk in str(text or "").replace("，", ",").replace("、", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return list(dict.fromkeys(parts))


def _importance_label(value: int) -> str:
    return storage.IMPORTANCE_LABELS.get(int(value or 3), "")


# ---------------------------------------------------------------- 侧边栏


topics = storage.list_topics()

with st.sidebar:
    st.header("主题")
    if topics:
        options = [item["topic_id"] for item in topics]
        labels = {
            item["topic_id"]: f"{item['name']}（{item['event_count']}）" for item in topics
        }
        current_id = st.session_state.get("evt_topic_id")
        if current_id not in options:
            current_id = options[0]
        selected_id = st.selectbox(
            "选择主题",
            options,
            index=options.index(current_id),
            format_func=lambda value: labels.get(value, value),
            key="evt_topic_select",
        )
        if selected_id != st.session_state.get("evt_topic_id"):
            st.session_state["evt_topic_id"] = selected_id
            st.session_state.pop("evt_collect_records", None)
            st.session_state.pop("evt_thread_text", None)
    else:
        st.caption("还没有主题，先在下方新建一个。")

    with st.expander("新建主题", expanded=not topics):
        with st.form("create_topic_form", clear_on_submit=True):
            new_name = st.text_input("主题名称", placeholder="例如：光伏行业产能周期")
            new_kind = st.selectbox(
                "主题类型",
                options=list(storage.TOPIC_KINDS.keys()),
                format_func=lambda value: storage.TOPIC_KINDS[value],
            )
            new_desc = st.text_area("主题说明", placeholder="关注什么、判断口径等（可留空）", height=68)
            new_industry = st.text_input("关联申万三级行业 ID（可选）", placeholder="例如 851811")
            new_companies = st.text_input(
                "关联公司代码（可选，逗号分隔）", placeholder="例如 000002.SZ, 600048.SH"
            )
            submitted = st.form_submit_button("创建主题", width="stretch")
        if submitted:
            if not new_name.strip():
                st.warning("主题名称不能为空。")
            else:
                codes = [chunk.strip() for chunk in new_companies.replace("，", ",").split(",") if chunk.strip()]
                industry_name = ""
                if new_industry.strip():
                    try:
                        from industry_pipeline import resolve_industry

                        info = resolve_industry(new_industry.strip())
                        industry_name = str(info.get("industry_l3_name", "")) if info else ""
                    except Exception:  # noqa: BLE001 - 关联行业仅用于展示
                        industry_name = ""
                topic = storage.create_topic(
                    new_name,
                    kind=new_kind,
                    description=new_desc,
                    industry_id=new_industry.strip(),
                    industry_name=industry_name,
                    company_ids=codes,
                )
                st.session_state["evt_topic_id"] = topic["topic_id"]
                st.rerun()

    st.divider()
    st.header("检索")
    keyword = st.text_input("关键词（标题/正文/标签）", placeholder="例如：涨价、产能、减持")
    sort_desc = st.radio("排序", ["时间倒序", "时间正序"], horizontal=True) == "时间倒序"
    date_range = st.date_input(
        "时间范围",
        value=(),
        help="留空表示不限时间",
    )

st.title("事件时间线")
st.caption(
    "把某个主题的关键事件按时间入栈：可手工录入，也可从巨潮公告自动采集；"
    "支持检索、脉络整理与 Word 导出。数据保存在 evttimeline/data/topics 下。"
)

topic_id = st.session_state.get("evt_topic_id")
topic = storage.load_topic(topic_id) if topic_id else None

if topic is None:
    st.info("还没有主题。请在左侧「新建主题」创建一个，例如「光伏行业产能周期」或「某公司经营状态」。")
    st.stop()

# ---------------------------------------------------------------- 过滤


tags_options = storage.all_tags(topic)
selected_tags = st.sidebar.multiselect("标签筛选", tags_options) if tags_options else []

start_date = end_date = ""
if isinstance(date_range, tuple) and len(date_range) >= 1:
    start_date = date_range[0].isoformat() if date_range[0] else ""
    if len(date_range) >= 2 and date_range[1]:
        end_date = date_range[1].isoformat()

events = storage.filter_events(
    topic,
    keyword=keyword,
    tags=selected_tags,
    start=start_date,
    end=end_date,
    descending=sort_desc,
)
stats = storage.topic_stats(topic)
kind_label = storage.TOPIC_KINDS.get(str(topic.get("kind")), "其它主题")

header_left, header_right = st.columns([3, 2])
with header_left:
    st.subheader(topic["name"])
    meta = [kind_label, f"共 {stats['count']} 条事件"]
    if stats["count"]:
        meta.append(f"{min(stats['by_year'])}–{max(stats['by_year'])}")
    if topic.get("industry_name"):
        meta.append(f"行业：{topic['industry_name']}")
    if topic.get("company_ids"):
        meta.append(f"公司：{'、'.join(topic['company_ids'][:3])}")
    st.markdown(f'<div class="evt-meta">{"　·　".join(meta)}</div>', unsafe_allow_html=True)
    if topic.get("description"):
        st.caption(topic["description"])
with header_right:
    if keyword or selected_tags or start_date:
        st.caption(f"当前筛选命中 {len(events)} 条")

tab_timeline, tab_add, tab_collect, tab_thread, tab_export = st.tabs(
    ["时间线", "新增事件", "自动采集", "脉络整理", "导出"]
)

# ---------------------------------------------------------------- 时间线


def _render_event(event: dict) -> None:
    with st.container(border=True):
        left, right = st.columns([5, 1])
        with left:
            importance = int(event.get("importance") or 3)
            star = "★" * importance
            st.markdown(
                f"**{event.get('date', '')}　{event.get('title', '')}**　"
                f'<span class="evt-imp-{importance}">{star}</span>',
                unsafe_allow_html=True,
            )
            chips = _tag_html([str(tag) for tag in event.get("tags") or []])
            source_label = storage.EVENT_SOURCES.get(str(event.get("source") or "manual"), "")
            chips += f'<span class="evt-tag">{source_label}</span>'
            st.markdown(chips, unsafe_allow_html=True)
            if event.get("content"):
                st.markdown(f'<div class="evt-body">{str(event["content"])}</div>', unsafe_allow_html=True)
            if event.get("source_url"):
                st.caption(f"来源：{event['source_url']}")
        with right:
            with st.popover("编辑", width="stretch"):
                with st.form(f"edit_{event['event_id']}"):
                    edit_date = st.date_input(
                        "日期",
                        value=pd.to_datetime(event.get("date") or date.today().isoformat()).date(),
                        key=f"d_{event['event_id']}",
                    )
                    edit_title = st.text_input("标题", value=event.get("title", ""), key=f"t_{event['event_id']}")
                    edit_content = st.text_area(
                        "正文", value=event.get("content", ""), height=110, key=f"c_{event['event_id']}"
                    )
                    edit_tags = st.text_input(
                        "标签（逗号分隔）",
                        value="、".join(str(tag) for tag in event.get("tags") or []),
                        key=f"g_{event['event_id']}",
                    )
                    edit_importance = st.select_slider(
                        "重要性",
                        options=[1, 2, 3, 4, 5],
                        value=int(event.get("importance") or 3),
                        format_func=_importance_label,
                        key=f"i_{event['event_id']}",
                    )
                    edit_url = st.text_input("来源链接", value=event.get("source_url", ""), key=f"u_{event['event_id']}")
                    saved = st.form_submit_button("保存修改", width="stretch")
                if saved:
                    storage.update_event(
                        topic,
                        event["event_id"],
                        {
                            "date": edit_date.isoformat(),
                            "title": edit_title.strip(),
                            "content": edit_content.strip(),
                            "tags": _split_tags(edit_tags),
                            "importance": int(edit_importance),
                            "source_url": edit_url.strip(),
                        },
                    )
                    st.rerun()
            if st.button("删除", key=f"del_{event['event_id']}", width="stretch"):
                storage.delete_event(topic, event["event_id"])
                st.rerun()


with tab_timeline:
    if not events:
        st.info("没有符合条件的事件。可以在「新增事件」里录入，或在「自动采集」里抓公告。")
    else:
        current_year = None
        for event in events:
            year = str(event.get("date") or "")[:4] or "未知"
            if year != current_year:
                current_year = year
                st.markdown(f"#### {year} 年")
            _render_event(event)

# ---------------------------------------------------------------- 新增事件

with tab_add:
    with st.form("add_event_form", clear_on_submit=True):
        cols = st.columns([1, 2, 1])
        with cols[0]:
            add_date = st.date_input("日期", value=date.today())
        with cols[1]:
            add_title = st.text_input("事件标题", placeholder="例如：住建部发布收储存量房政策")
        with cols[2]:
            add_importance = st.select_slider(
                "重要性", options=[1, 2, 3, 4, 5], value=3, format_func=_importance_label
            )
        add_content = st.text_area("正文 / 说明", height=120, placeholder="事件内容、影响判断、数据出处等")
        add_tags = st.text_input("标签（逗号分隔）", placeholder="例如：政策, 供给, 价格")
        add_url = st.text_input("来源链接（可选）")
        added = st.form_submit_button("保存事件", width="stretch")
    if added:
        if not add_title.strip():
            st.warning("事件标题不能为空。")
        else:
            topic, count, skipped = storage.add_events(
                topic,
                [
                    {
                        "date": add_date.isoformat(),
                        "title": add_title.strip(),
                        "content": add_content.strip(),
                        "tags": _split_tags(add_tags),
                        "importance": int(add_importance),
                        "source": "manual",
                        "source_url": add_url.strip(),
                    }
                ],
            )
            if count:
                st.success("事件已保存。")
            else:
                st.info("该事件已存在（日期与标题相同）。")
            st.rerun()

# ---------------------------------------------------------------- 自动采集

with tab_collect:
    st.caption(
        "从巨潮资讯采集指定公司在窗口内的公告（含临时公告），勾选后入栈。"
        "采集只取标题与原文链接，不解析 PDF。"
    )
    default_codes = ", ".join(topic.get("company_ids") or [])
    with st.form("collect_form"):
        codes_input = st.text_input(
            "公司代码（逗号分隔，建议不超过 5 家）",
            value=default_codes,
            placeholder="例如：000002.SZ, 600048.SH",
        )
        range_cols = st.columns([1, 1, 1])
        with range_cols[0]:
            collect_start = st.date_input("开始日期", value=pd.Timestamp(date.today()) - pd.Timedelta(days=180))
        with range_cols[1]:
            collect_end = st.date_input("结束日期", value=date.today())
        with range_cols[2]:
            collect_keyword = st.text_input("标题包含（可选）", placeholder="例如：中标、减持")
        options_cols = st.columns([1, 1])
        with options_cols[0]:
            skip_noise = st.checkbox("过滤例行公告（董事会议案、股东大会通知等）", value=True)
        with options_cols[1]:
            collect_tags = st.text_input("入栈标签（可选，逗号分隔）", placeholder="例如：公告, 地产")
        run_collect = st.form_submit_button("检索公告", width="stretch")

    if run_collect:
        codes = [chunk.strip() for chunk in codes_input.replace("，", ",").split(",") if chunk.strip()]
        if not codes:
            st.warning("请至少填写一个公司代码。")
        elif collect_start > collect_end:
            st.warning("开始日期不能晚于结束日期。")
        else:
            with st.spinner("正在检索巨潮公告…"):
                records, errors = collector.collect_for_companies(
                    codes,
                    collect_start.isoformat(),
                    collect_end.isoformat(),
                    keyword=collect_keyword.strip(),
                    skip_noise=skip_noise,
                    max_companies=5,
                )
            st.session_state["evt_collect_records"] = records
            st.session_state["evt_collect_tags"] = _split_tags(collect_tags)
            st.session_state["evt_collect_errors"] = errors
            if not records:
                st.info("窗口内没有检索到公告。可放宽日期范围或关闭例行公告过滤再试。")

    records = st.session_state.get("evt_collect_records") or []
    if records:
        st.write(f"共检索到 {len(records)} 条公告，勾选后点「加入时间线」：")
        # 逐条勾选（普通 checkbox），比 data_editor 在表单交互上更可靠
        picks: list[dict] = []
        for index, record in enumerate(records):
            label = f"{record.get('date', '')}　{record.get('company_name', '')}　{record.get('title', '')}"
            if st.checkbox(label, value=True, key=f"evt_pick_{index}"):
                picks.append(record)
        st.caption(f"已勾选 {len(picks)} / {len(records)} 条。")
        if st.button("加入时间线", type="primary", width="stretch"):
            if not picks:
                st.warning("没有勾选任何公告。")
            else:
                try:
                    pending = collector.announcements_to_events(
                        picks, tags=st.session_state.get("evt_collect_tags") or []
                    )
                    topic, added, skipped = storage.add_events(topic, pending)
                    st.success(f"新增 {added} 条，跳过重复 {skipped} 条。")
                    st.session_state.pop("evt_collect_records", None)
                    for index in range(len(records)):
                        st.session_state.pop(f"evt_pick_{index}", None)
                except Exception as exc:  # noqa: BLE001 - 出错要可见
                    st.error(f"入栈失败：{str(exc)[:200]}")
    errors = st.session_state.get("evt_collect_errors") or []
    if errors:
        st.warning("部分公司采集失败：" + "；".join(errors))

# ---------------------------------------------------------------- 脉络整理

with tab_thread:
    st.caption(
        "按阶段梳理事件脉络。未配置 LLM 时使用规则化模板（按年分组 + 标签分布 + 关键节点）；"
        "配置环境变量 EVT_LLM_BASE_URL / EVT_LLM_API_KEY / EVT_LLM_MODEL 后可用模型生成带因果分析的版本。"
    )
    config = collector.llm_config()
    st.caption(
        ("已启用 LLM：" + config["model"]) if config["enabled"] else "当前为规则化模板模式（未配置 LLM）"
    )
    if st.button("整理事件脉络", type="primary", width="stretch"):
        with st.spinner("正在整理…"):
            st.session_state["evt_thread_text"] = collector.build_thread_text(topic, events)
    thread_text = st.session_state.get("evt_thread_text")
    if thread_text:
        st.markdown(thread_text)
        st.download_button(
            "下载脉络（Markdown）",
            data=thread_text.encode("utf-8"),
            file_name=f"{topic['name']}_事件脉络.md",
            mime="text/markdown",
            width="stretch",
        )
    else:
        st.info("点上方按钮生成脉络整理结果。")

# ---------------------------------------------------------------- 导出

with tab_export:
    st.write(f"当前主题共 {stats['count']} 条事件，导出范围与「时间线」页签的筛选一致（当前 {len(events)} 条）。")
    markdown_text = exporter.to_markdown(topic, events)
    plain_text = exporter.to_plain_text(topic, events)
    docx_bytes = exporter.to_docx_bytes(topic, events)
    stamp = datetime.now().strftime("%Y%m%d")
    safe_name = "".join(char for char in topic["name"] if char not in '\\/:*?"<>|') or "事件时间线"

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "下载 Word（.docx）",
            data=docx_bytes,
            file_name=f"{safe_name}_{stamp}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            width="stretch",
        )
        st.download_button(
            "下载 Markdown",
            data=markdown_text.encode("utf-8"),
            file_name=f"{safe_name}_{stamp}.md",
            mime="text/markdown",
            width="stretch",
        )
    with col2:
        st.download_button(
            "下载纯文本",
            data=plain_text.encode("utf-8"),
            file_name=f"{safe_name}_{stamp}.txt",
            mime="text/plain",
            width="stretch",
        )
        st.download_button(
            "下载 JSON 备份",
            data=exporter.to_json_bytes(topic),
            file_name=f"{safe_name}_{stamp}.json",
            mime="application/json",
            width="stretch",
        )
    with st.expander("预览 Markdown"):
        st.code(markdown_text, language="markdown")

    st.divider()
    with st.expander("危险操作：删除该主题"):
        st.caption("会连同该主题的全部事件一起删除，且不可恢复。建议先下载 JSON 备份。")
        confirm = st.checkbox(f"我确认删除「{topic['name']}」", key="evt_delete_confirm")
        if st.button("删除主题", disabled=not confirm):
            storage.delete_topic(topic["topic_id"])
            st.session_state.pop("evt_topic_id", None)
            st.rerun()
