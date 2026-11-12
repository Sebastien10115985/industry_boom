"""事件时间线导出：Markdown / 纯文本 / Word（.docx）/ JSON 备份。"""

from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO

from storage import EVENT_SOURCES, IMPORTANCE_LABELS, TOPIC_KINDS


def _topic_header_lines(topic: dict) -> list[str]:
    kind = TOPIC_KINDS.get(str(topic.get("kind")), "其它主题")
    lines = [f"主题：{topic.get('name', '')}", f"类型：{kind}"]
    if topic.get("industry_name"):
        lines.append(f"关联行业：{topic['industry_name']}（{topic.get('industry_id', '')}）")
    if topic.get("company_ids"):
        names = topic.get("company_names") or []
        pairs = [
            f"{name}（{code}）" if name else code
            for name, code in zip(names or [""] * len(topic["company_ids"]), topic["company_ids"])
        ]
        lines.append("关联公司：" + "、".join(pairs))
    if topic.get("description"):
        lines.append(f"说明：{topic['description']}")
    return lines


def _group_by_year(events: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for event in sorted(events, key=lambda item: str(item.get("date") or "")):
        year = str(event.get("date") or "")[:4] or "未知"
        grouped.setdefault(year, []).append(event)
    return grouped


def to_markdown(topic: dict, events: list[dict]) -> str:
    lines = [f"# {topic.get('name', '')} · 事件时间线", ""]
    lines += [f"- {line}" for line in _topic_header_lines(topic)]
    lines += [
        f"- 事件数：{len(events)}",
        f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    for year, items in _group_by_year(events).items():
        lines += [f"## {year} 年", ""]
        for event in items:
            tags = "、".join(str(tag) for tag in event.get("tags") or [])
            meta = [IMPORTANCE_LABELS.get(int(event.get("importance") or 3), "")]
            meta.append(EVENT_SOURCES.get(str(event.get("source") or "manual"), "手工录入"))
            if tags:
                meta.append(tags)
            lines.append(f"### {event.get('date', '')}　{event.get('title', '')}")
            lines.append("")
            lines.append("　".join(part for part in meta if part))
            lines.append("")
            if event.get("content"):
                lines += [str(event["content"]).strip(), ""]
            if event.get("source_url"):
                lines += [f"来源：{event['source_url']}", ""]
    return "\n".join(lines).strip() + "\n"


def to_plain_text(topic: dict, events: list[dict]) -> str:
    lines = [topic.get("name", ""), "=" * min(48, max(8, len(str(topic.get("name", ""))))) , ""]
    lines += _topic_header_lines(topic)
    lines += ["", f"事件数：{len(events)}", ""]
    for year, items in _group_by_year(events).items():
        lines += [f"【{year} 年】", ""]
        for event in items:
            tags = "、".join(str(tag) for tag in event.get("tags") or [])
            lines.append(f"{event.get('date', '')}  {event.get('title', '')}")
            if tags:
                lines.append(f"    标签：{tags}")
            if event.get("content"):
                for row in str(event["content"]).strip().splitlines():
                    lines.append("    " + row.strip())
            if event.get("source_url"):
                lines.append(f"    来源：{event['source_url']}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def to_docx_bytes(topic: dict, events: list[dict]) -> bytes:
    """生成 Word 文档：标题 + 主题信息 + 按年分节的事件条目。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    document = Document()
    document.styles["Normal"].font.name = "Microsoft YaHei"
    document.styles["Normal"].font.size = Pt(10.5)

    heading = document.add_heading(f"{topic.get('name', '')} · 事件时间线", level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    info = document.add_paragraph()
    info.paragraph_format.space_after = Pt(2)
    for line in _topic_header_lines(topic):
        info.add_run(line + "\n").font.size = Pt(9)
    tail = document.add_paragraph()
    tail.paragraph_format.space_after = Pt(10)
    tail.add_run(
        f"事件数：{len(events)}　导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ).font.size = Pt(9)

    for year, items in _group_by_year(events).items():
        document.add_heading(f"{year} 年", level=1)
        for event in items:
            title_paragraph = document.add_paragraph()
            title_paragraph.paragraph_format.space_before = Pt(8)
            title_paragraph.paragraph_format.space_after = Pt(2)
            date_run = title_paragraph.add_run(f"{event.get('date', '')}　")
            date_run.bold = True
            date_run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
            title_run = title_paragraph.add_run(str(event.get("title", "")))
            title_run.bold = True

            meta_parts = [
                IMPORTANCE_LABELS.get(int(event.get("importance") or 3), ""),
                EVENT_SOURCES.get(str(event.get("source") or "manual"), "手工录入"),
            ]
            tags = "、".join(str(tag) for tag in event.get("tags") or [])
            if tags:
                meta_parts.append("标签：" + tags)
            meta = document.add_paragraph()
            meta.paragraph_format.space_after = Pt(2)
            run = meta.add_run("　".join(part for part in meta_parts if part))
            run.font.size = Pt(8.5)
            run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

            if event.get("content"):
                body = document.add_paragraph(str(event["content"]).strip())
                body.paragraph_format.space_after = Pt(2)
            if event.get("source_url"):
                link = document.add_paragraph()
                link.paragraph_format.space_after = Pt(4)
                run = link.add_run(f"来源：{event['source_url']}")
                run.font.size = Pt(8.5)
                run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def to_json_bytes(topic: dict) -> bytes:
    return json.dumps(topic, ensure_ascii=False, indent=2).encode("utf-8")
