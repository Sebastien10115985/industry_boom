from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from data_io import load_path, normalize_input
from ranking import MODEL_SPECS, model_weight_table, prepare_growth_metrics, score_snapshot, select_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成万得三级行业季度观察库")
    parser.add_argument("--input", required=True, help="输入 CSV/XLSX 文件")
    parser.add_argument("--as-of", required=True, help="信息截止日，例如 2026-10-31")
    parser.add_argument("--report-period", required=True, help="报告期，例如 2026-09-30")
    parser.add_argument("--output", required=True, help="输出 .xlsx 或 .csv 文件")
    parser.add_argument("--top-n", type=int, default=20, help="每个三级行业观察数量")
    parser.add_argument("--minimum-coverage", type=float, default=0.60, help="最低数据完整度")
    parser.add_argument(
        "--model",
        choices=["auto", *MODEL_SPECS.keys()],
        default="auto",
        help="强制使用指定模型，默认按 model_type 自动识别",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = normalize_input(load_path(args.input))
    if args.model != "auto":
        data["model_type"] = args.model
    prepared = prepare_growth_metrics(data, args.as_of)
    snapshot = select_snapshot(prepared, args.as_of, args.report_period)
    ranking = score_snapshot(snapshot, args.top_n, args.minimum_coverage)
    watchlist = ranking[ranking["in_watchlist"]].copy()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".csv":
        watchlist.to_csv(output, index=False, encoding="utf-8-sig")
    elif output.suffix.lower() == ".xlsx":
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            watchlist.to_excel(writer, sheet_name="观察库", index=False)
            ranking.to_excel(writer, sheet_name="完整排名", index=False)
            model_weight_table().to_excel(writer, sheet_name="模型权重", index=False)
    else:
        raise ValueError("输出仅支持 .xlsx 或 .csv")

    print(
        f"已生成 {output}：{ranking['industry_l3_id'].nunique()} 个三级行业，"
        f"{len(watchlist)} 家公司进入观察库。"
    )


if __name__ == "__main__":
    main()
