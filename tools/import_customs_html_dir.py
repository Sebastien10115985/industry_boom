"""批量导入中文站另存的「重点出口商品量值表」HTML 到海关月度库。

用法：
  1. 把浏览器另存的各期文章 HTML 放入 data/customs_html_backfill/
     （文件名建议含年月，如 2025-12.html 或 2025年12月.html）；
  2. 预览（不写库）：python tools/import_customs_html_dir.py
  3. 确认后写入：python tools/import_customs_html_dir.py --apply

安全：写入前自动备份 customs_monthly.csv；经 merge_customs 合并（只增不删）；
     解析不足 12 项的期数跳过，避免脏数据入库。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from backfill_customs_cn import CN_MATCH_RULES
from customs_provider import (
    CUSTOMS_EXPORT_ITEMS,
    DEFAULT_CUSTOMS_PATH,
    CustomsError,
    _numeric,
    _strip_html,
    load_customs,
    merge_customs,
    save_customs,
)

BACKFILL_DIR = Path(__file__).resolve().parent.parent / "data" / "customs_html_backfill"


def month_from_file(path: Path) -> str | None:
    """从内容标题解析统计月份（YYYY-MM）；文件名是发布时间不可靠，仅作兜底。"""
    if path.suffix.lower() in {".xls", ".xlsx"}:
        return _month_from_xls(path) or _month_from_stem(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    title_match = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
    title = _strip_html(title_match.group(1)) if title_match else ""
    match = re.search(r"(20\d{2})\s*年\s*(1[0-2]|[1-9])\s*月", title + " " + text[:3000])
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return _month_from_stem(path)


def _month_from_stem(path: Path) -> str | None:
    match = re.search(r"(20\d{2})[-_.]?(1[0-2]|[1-9])", path.stem)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return None


def _load_rmb_sheet(path: Path) -> tuple[pd.DataFrame, str]:
    """读取 XLS 中「出口主要商品量值表（人民币值）」sheet，返回 (表, 期数标题文本)。"""
    book = pd.ExcelFile(path, engine="xlrd" if path.suffix.lower() == ".xls" else "openpyxl")
    sheet = next((s for s in book.sheet_names if "人民币值" in s and "出口主要商品" in s), None)
    if sheet is None:
        raise CustomsError(f"{path.name} 中没有「出口主要商品量值表（人民币值）」sheet（现有：{book.sheet_names}）")
    frame = book.parse(sheet, header=None)
    title = ""
    for i in range(min(6, len(frame))):
        joined = " ".join(str(v) for v in frame.iloc[i].tolist())
        match = re.search(r"(20\d{2})\s*年\s*(1[0-2]|[1-9])\s*月", joined)
        if match:
            title = joined
            break
    return frame, title


def _month_from_xls(path: Path) -> str | None:
    _, title = _load_rmb_sheet(path)
    match = re.search(r"(20\d{2})\s*年\s*(1[0-2]|[1-9])\s*月", title)
    return f"{match.group(1)}-{int(match.group(2)):02d}" if match else None


def parse_cn_xls(path: Path) -> dict[str, tuple[str, float, float, float]]:
    """解析海关月报 XLS 人民币值表：商品名按关键词匹配，取当月/累计/累计同比金额。

    版式自适应（1月单月版无累计列：累计取当月、累计同比取当月同比）；
    单位固定「万元人民币」，÷1万 转亿元与本地库一致。
    """
    frame, title = _load_rmb_sheet(path)
    if "万美元" in title:
        raise CustomsError(f"{path.name} 是美元值表；请使用人民币值表")

    header_row = None
    for i in range(len(frame)):
        if any("商品名称" in str(v) for v in frame.iloc[i].tolist()):
            header_row = i
            break
    if header_row is None or header_row + 1 >= len(frame):
        raise CustomsError(f"{path.name} 未找到「商品名称」表头行（可能是反爬挑战页或格式变化）")
    main = frame.iloc[header_row].tolist()
    sub = frame.iloc[header_row + 1].tolist()

    # 合并单元格：主表头向右沿用最近非空文本
    segments, last = [], ""
    for value in main:
        text = str(value).strip()
        if text and text != "nan":
            last = text
        segments.append(last)
    name_col = next((j for j, v in enumerate(main) if "商品名称" in str(v)), 1)

    month_col = cum_col = cum_yoy_col = None
    for j, cell in enumerate(sub):
        if str(cell).strip() != "金额":
            continue
        segment = segments[j]
        if re.search(r"\d{1,2}\s*月\s*$", segment) and month_col is None:
            month_col = j
        elif "累计比去年同期" in segment or ("累计" in segment and "%" in segment):
            cum_yoy_col = j
        elif "累计" in segment and cum_col is None:
            cum_col = j
    has_cum = cum_col is not None

    def num(value) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        return _numeric(str(value))

    matched: dict[str, tuple[str, float, float, float]] = {}
    for i in range(header_row + 2, len(frame)):
        row = frame.iloc[i].tolist()
        if len(row) <= name_col:
            continue
        name = str(row[name_col]).strip()
        if name in {"", "nan"} or name.startswith("注"):
            continue
        needed = max(c for c in (month_col, cum_col, cum_yoy_col) if c is not None)
        if len(row) <= needed:
            continue
        for cn_name, (keywords, excludes) in CN_MATCH_RULES.items():
            if cn_name in matched:
                continue
            if any(kw in name for kw in keywords) and not any(ex in name for ex in excludes):
                matched[cn_name] = (
                    name,
                    num(row[month_col]) / 10000.0,
                    num(row[cum_col]) / 10000.0 if has_cum else num(row[month_col]) / 10000.0,
                    num(row[cum_yoy_col]) if has_cum else num(row[month_col + 1]),
                )
                break
    return matched


def parse_cn_table(html: str) -> dict[str, tuple[str, float, float, float]]:
    """解析中文量值表：商品名按关键词匹配，取金额当月/累计/累计同比三列。

    列定位自适应：先找包含「当月」「累计」的表头行确定金额列位置；
    找不到表头时退回英文站同源结构（第4/6/10列）。
    单位自适应：按表头「金额单位」行换算——万元人民币 ÷1万 转亿元（与本地库一致）；
    美元值表直接报错拒绝，避免污染人民币口径。
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    table = [
        [_strip_html(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        for row in rows
    ]
    table = [cells for cells in table if cells]

    unit_scale = 1.0
    for cells in table:
        joined = "|".join(cells)
        if "金额单位" in joined:
            if "万元人民币" in joined:
                unit_scale = 10000.0
            elif "万美元" in joined or "美元" in joined:
                raise CustomsError("这是美元值表；请下载「人民币值」表，避免与本地库人民币口径混淆")
            break

    month_col, cum_col, yoy_col = 3, 5, 9
    for cells in table:
        joined = "|".join(cells)
        if ("当月" in joined or "本月" in joined) and "累计" in joined:
            for idx, cell in enumerate(cells):
                clean = cell.replace("金额", "").replace("（", "").replace("）", "").replace("(", "").replace(")", "").strip()
                if clean == "当月":
                    month_col = idx
                elif clean == "累计":
                    cum_col = idx
                elif "同比" in clean or ("累计" in joined and "同比" in cell):
                    yoy_col = idx
            break

    matched: dict[str, tuple[str, float, float, float]] = {}
    for cells in table:
        name = cells[0].rstrip("*").strip()
        if name in {"", "商品", "Commodity", "Unit", "计量单位"} or name.startswith(("注", "Quantity", "Scope")):
            continue
        if len(cells) <= max(month_col, cum_col, yoy_col):
            continue
        for cn_name, (keywords, excludes) in CN_MATCH_RULES.items():
            if cn_name in matched:
                continue
            if any(kw in name for kw in keywords) and not any(ex in name for ex in excludes):
                matched[cn_name] = (
                    name,
                    _numeric(cells[month_col]) / unit_scale,
                    _numeric(cells[cum_col]) / unit_scale,
                    _numeric(cells[yoy_col]),
                )
                break
    return matched


def import_dir(directory: Path, apply: bool, path: str | Path | None = None) -> None:
    files = sorted(directory.glob("*.htm*")) + sorted(directory.glob("*.xls*")) if directory.exists() else []
    if not files:
        raise CustomsError(f"目录 {directory} 中没有 HTML/XLS 文件。请先把各期文章页或 XLS 表放入该目录。")

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records: list[dict[str, object]] = []
    skipped: list[str] = []
    for file in files:
        month = month_from_file(file)
        if not month:
            skipped.append(f"{file.name}（无法识别月份）")
            continue
        if file.suffix.lower() in {".xls", ".xlsx"}:
            matched = parse_cn_xls(file)
        else:
            matched = parse_cn_table(file.read_text(encoding="utf-8", errors="ignore"))
        missing = [cn for cn, _ in CUSTOMS_EXPORT_ITEMS if cn not in matched]
        if len(matched) < len(CUSTOMS_EXPORT_ITEMS):
            skipped.append(f"{file.name}（{month}，缺 {len(missing)} 项：{'/'.join(missing)}）")
            continue
        print(f"{month} [{file.name}] OK，样例：稀土={matched['稀土'][1]:.1f}/{matched['稀土'][2]:.1f}/{matched['稀土'][3]:.1f}  "
              f"汽车={matched['汽车'][1]:.1f}/{matched['汽车'][2]:.1f}/{matched['汽车'][3]:.1f}")
        for cn_name, (row_name, month_value, cum_value, cum_yoy) in matched.items():
            records.append(
                {
                    "month": month,
                    "commodity_cn": cn_name,
                    "commodity_en": row_name,
                    "export_month_amount_yi": month_value,
                    "export_cum_amount_yi": cum_value,
                    "export_cum_yoy_pct": cum_yoy,
                    "source_url": "",
                    "fetched_at": fetched_at,
                }
            )
    for item in skipped:
        print("跳过：", item)
    if not records:
        raise CustomsError("没有任何文件成功解析出 12 项完整数据。")

    months = sorted({str(r["month"]) for r in records})
    print(f"共 {len(records)} 条记录、{len(months)} 期：{months[0]} ~ {months[-1]}")
    if apply:
        frame = load_customs(path or DEFAULT_CUSTOMS_PATH)
        target = Path(path or DEFAULT_CUSTOMS_PATH)
        shutil.copy2(target, target.with_suffix(".csv.bak"))
        merged = merge_customs(frame, pd.DataFrame(records))
        save_customs(merged, path or DEFAULT_CUSTOMS_PATH)
        print(f"已写入：{len(frame)} 行（{frame['month'].min()}~{frame['month'].max()}）→ {len(merged)} 行"
              f"（{merged['month'].min()}~{merged['month'].max()}）")
        print(f"原数据备份于 {target.with_suffix('.csv.bak')}")
    else:
        print("dry-run 完成；确认各期数值无误后加 --apply 写入。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=str(BACKFILL_DIR), help="HTML 文件目录")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run 只预览）")
    parser.add_argument("--path", default=None, help="自定义 CSV 路径")
    args = parser.parse_args()
    import_dir(Path(args.dir), args.apply, args.path)


if __name__ == "__main__":
    main()
