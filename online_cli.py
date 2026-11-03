from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from data_io import load_path
from disclosure_pipeline import (
    DEFAULT_DATA_PATH,
    load_online_financials,
    rank_online_financials,
    sync_official_reports,
)
from industry_catalog import freeze_catalog
from industry_pipeline import (
    DEFAULT_LOOKBACK_DAYS,
    INDUSTRY_DATA_DIR,
    load_all_industry_financials,
    load_industry_financials,
    rank_industry_financials,
    resolve_industry,
    sync_industry_reports,
    synced_industries,
)
from nbs_provider import (
    DEFAULT_HISTORY_PATH as DEFAULT_PROSPERITY_PATH,
    import_history,
    load_history,
    update_history,
)
from prosperity import corresponding_industries, prosperity_board
from ranking import model_weight_table
from sw_constituents import SNAPSHOT_DATE, constituents_for, industry_coverage
from wind_provider import DEFAULT_ALL_A_SECTOR, fetch_official_wind_snapshot


def _add_catalog_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--effective-date", required=True, help="行业成分股快照日期")
    parser.add_argument("--seed-count", type=int, default=5, help="每行业按市值选取的默认种子公司数")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="官方行业目录与定期报告在线更新")
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("catalog-freeze", help="从万得官方导出固化行业目录")
    freeze.add_argument("--input", required=True, help="万得官方 CSV/XLSX 成分股导出")
    _add_catalog_output_arguments(freeze)

    wind = subparsers.add_parser("catalog-wind", help="通过已授权 WindPy 固化行业目录")
    _add_catalog_output_arguments(wind)
    wind.add_argument("--industry-id-field", required=True, help="当前 Wind 许可的三级行业 ID 字段")
    wind.add_argument("--industry-name-field", required=True, help="当前 Wind 许可的三级行业名称字段")
    wind.add_argument("--market-cap-field", default="mkt_cap_ard")
    wind.add_argument("--all-a-sector", default=DEFAULT_ALL_A_SECTOR)

    update = subparsers.add_parser("update", help="从巨潮资讯同步正式定期报告")
    update.add_argument("--as-of", default=date.today().isoformat())
    update.add_argument("--scope", choices=["seed", "all"], default="seed")
    update.add_argument("--company", action="append", default=[], help="仅更新指定万得代码，可重复")
    update.add_argument("--data-file", default=str(DEFAULT_DATA_PATH))
    update.add_argument("--lookback-days", type=int, default=800)
    update.add_argument("--max-companies", type=int)

    rank = subparsers.add_parser("rank", help="按最新共同报告期或指定报告期排名")
    rank.add_argument("--as-of", default=date.today().isoformat())
    rank.add_argument("--report-period")
    rank.add_argument("--data-file", default=str(DEFAULT_DATA_PATH))
    rank.add_argument("--output", required=True)
    rank.add_argument("--top-n", type=int, default=20)
    rank.add_argument("--minimum-coverage", type=float, default=0.60)

    prosperity = subparsers.add_parser("prosperity", help="从国家统计局更新并展示行业景气度")
    prosperity.add_argument("--months", type=int, default=6, help="回看并抓取的月数")
    prosperity.add_argument("--window", type=int, default=6, help="景气度计算的滚动窗口月数")
    prosperity.add_argument("--top-n", type=int, default=5, help="展示最景气的统计行业数")
    prosperity.add_argument("--history-file", default=str(DEFAULT_PROSPERITY_PATH))
    prosperity.add_argument("--input", help="可选：手工整理的分行业月度景气 CSV/XLSX")
    prosperity.add_argument("--offline", action="store_true", help="不联网，只用本地已有历史")
    prosperity.add_argument("--output")

    constituents = subparsers.add_parser("constituents", help="查看已固化的三级行业成分公司")
    constituents.add_argument("--industry", help="三级行业名称（支持部分匹配）或行业 ID")
    constituents.add_argument("--stat-industry", help="统计行业大类，列出其对应三级行业的成分公司")
    constituents.add_argument("--output", help="可选：导出 CSV/XLSX")

    industry = subparsers.add_parser("industry", help="按三级行业拉取全部成份公司财报并排名")
    industry_actions = industry.add_subparsers(dest="action", required=True)

    industry_sync = industry_actions.add_parser("sync", help="从巨潮拉取该行业全部成份公司近一年财报")
    industry_sync.add_argument("--industry", required=True, help="三级行业名称（支持模糊）或行业 ID")
    industry_sync.add_argument("--as-of", default=date.today().isoformat())
    industry_sync.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    industry_sync.add_argument("--max-companies", type=int)
    industry_sync.add_argument(
        "--company", action="append", default=[], help="仅补拉指定证券代码，可重复"
    )
    industry_sync.add_argument("--refresh-market-cap", action="store_true", help="强制刷新总市值缓存")
    industry_sync.add_argument(
        "--reextract",
        action="store_true",
        help="忽略已有公告，重新下载并解析（抽取逻辑修正后用于回填历史数据）",
    )
    industry_sync.add_argument(
        "--only-missing",
        action="store_true",
        help="先本地判断，各成份公司都已取得最新报告期数据则不联网，否则只补拉缺的公司",
    )
    industry_sync.add_argument("--data-dir", default=str(INDUSTRY_DATA_DIR))

    industry_rank = industry_actions.add_parser("rank", help="用现有算法计算该子行业排名")
    industry_rank.add_argument("--industry", help="不填则排名全部已同步行业")
    industry_rank.add_argument("--as-of", default=date.today().isoformat())
    industry_rank.add_argument("--report-period")
    industry_rank.add_argument("--data-dir", default=str(INDUSTRY_DATA_DIR))
    industry_rank.add_argument("--output")
    industry_rank.add_argument("--top-n", type=int, default=20)
    industry_rank.add_argument("--minimum-coverage", type=float, default=0.60)

    industry_actions.add_parser("list", help="列出已同步财报的三级行业")
    return parser


def _write_ranking(output_path: str, ranking: pd.DataFrame) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    watchlist = ranking[ranking["in_watchlist"]]
    if output.suffix.lower() == ".csv":
        watchlist.to_csv(output, index=False, encoding="utf-8-sig")
        return
    if output.suffix.lower() != ".xlsx":
        raise ValueError("排名输出只支持 .xlsx 或 .csv")
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        watchlist.to_excel(writer, sheet_name="观察库", index=False)
        ranking.to_excel(writer, sheet_name="完整排名", index=False)
        model_weight_table().to_excel(writer, sheet_name="模型权重", index=False)


def _write_industry_ranking(output_path: str, ranking: pd.DataFrame) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    watchlist = ranking[ranking["in_watchlist"]]
    if output.suffix.lower() == ".csv":
        ranking.to_csv(output, index=False, encoding="utf-8-sig")
        return
    if output.suffix.lower() != ".xlsx":
        raise ValueError("排名输出只支持 .xlsx 或 .csv")
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        watchlist.to_excel(writer, sheet_name="观察库", index=False)
        ranking.to_excel(writer, sheet_name="完整排名", index=False)
        model_weight_table().to_excel(writer, sheet_name="模型权重", index=False)


def _print_table(frame: pd.DataFrame, columns: list[str]) -> None:
    print(frame[[column for column in columns if column in frame.columns]].to_string(index=False))


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "catalog-freeze":
        catalog, constituents = freeze_catalog(
            load_path(args.input),
            effective_date=args.effective_date,
            source=f"Wind official export: {Path(args.input).name}",
            seed_count=args.seed_count,
        )
        print(f"已固化 {len(catalog)} 个三级行业、{len(constituents)} 家成分股。")
        return

    if args.command == "catalog-wind":
        snapshot = fetch_official_wind_snapshot(
            effective_date=args.effective_date,
            industry_id_field=args.industry_id_field,
            industry_name_field=args.industry_name_field,
            market_cap_field=args.market_cap_field,
            all_a_sector=args.all_a_sector,
        )
        catalog, constituents = freeze_catalog(
            snapshot,
            effective_date=args.effective_date,
            source="Wind Client API",
            seed_count=args.seed_count,
        )
        print(f"已从 Wind API 固化 {len(catalog)} 个三级行业、{len(constituents)} 家成分股。")
        return

    if args.command == "update":
        def progress(current: int, total: int, message: str) -> None:
            print(f"[{current}/{total}] {message}")

        data, errors = sync_official_reports(
            as_of_date=args.as_of,
            scope=args.scope,
            company_ids=args.company or None,
            data_path=args.data_file,
            lookback_days=args.lookback_days,
            max_companies=args.max_companies,
            progress=progress,
        )
        print(f"本地累计 {len(data)} 条定期报告财务记录；本次错误 {len(errors)} 条。")
        if not errors.empty:
            error_path = Path(args.data_file).with_name("update_errors.csv")
            errors.to_csv(error_path, index=False, encoding="utf-8-sig")
            print(f"错误明细：{error_path}")
        return

    if args.command == "rank":
        period, ranking = rank_online_financials(
            load_online_financials(args.data_file),
            as_of_date=args.as_of,
            report_period=args.report_period,
            top_n=args.top_n,
            minimum_coverage=args.minimum_coverage,
        )
        _write_ranking(args.output, ranking)
        print(
            f"报告期 {period:%Y-%m-%d}：{ranking['industry_l3_id'].nunique()} 个行业，"
            f"{int(ranking['in_watchlist'].sum())} 家进入观察库。"
        )
        return

    if args.command == "prosperity":
        if args.input:
            history = import_history(load_path(args.input), path=args.history_file)
            print(f"已导入手工景气数据，历史共 {len(history)} 条。")
        elif args.offline:
            history = load_history(args.history_file)
        else:
            history = update_history(months=args.months, path=args.history_file)
            print(f"已从国家统计局更新，历史共 {len(history)} 条。")
        if history.empty:
            print("景气历史为空，请去掉 --offline 或提供 --input。")
            return
        top, full = prosperity_board(history, window=args.window, top_n=args.top_n)
        months = sorted(history["month"].unique())
        print(f"数据月份：{months[-args.window:]}")
        print(top.to_string(index=False))
        for row in top.itertuples(index=False):
            industries = corresponding_industries(row.stat_industry)
            print(f"\n{row.stat_industry} 对应 {len(industries)} 个申万三级行业：")
            print("  " + "、".join(industries["industry_l3_name"].tolist()))
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                top.to_excel(writer, sheet_name="最景气行业", index=False)
                full.to_excel(writer, sheet_name="景气度全表", index=False)
                history.to_excel(writer, sheet_name="月度原始数据", index=False)
            print(f"\n已写出 {output}")
        return

    if args.command == "industry":
        if args.action == "sync":
            def progress(current: int, total: int, message: str) -> None:
                print(f"[{current}/{total}] {message}")

            data, errors = sync_industry_reports(
                industry=args.industry,
                as_of_date=args.as_of,
                lookback_days=args.lookback_days,
                max_companies=args.max_companies,
                progress=progress,
                refresh_market_cap=args.refresh_market_cap,
                directory=args.data_dir,
                company_ids=args.company or None,
                only_missing=args.only_missing,
                reextract=args.reextract,
            )
            print(
                f"{args.industry}：本地累计 {len(data)} 条定期报告记录，"
                f"覆盖 {data['company_id'].nunique()} 家公司；本次失败 {len(errors)} 条。"
            )
            if not errors.empty:
                error_path = Path(args.data_dir) / "errors.csv"
                errors.to_csv(error_path, index=False, encoding="utf-8-sig")
                print(f"错误明细：{error_path}")
            return

        if args.action == "rank":
            if args.industry:
                info = resolve_industry(args.industry)
                if info is None:
                    raise ValueError(f"未匹配到三级行业：{args.industry}")
                data = load_industry_financials(info["industry_l3_id"], args.data_dir)
            else:
                data = load_all_industry_financials(args.data_dir)
            if data.empty:
                print("没有已同步的行业财报，请先执行 industry sync。")
                return
            ranking, notes = rank_industry_financials(
                data,
                as_of_date=args.as_of,
                report_period=args.report_period,
                top_n=args.top_n,
                minimum_coverage=args.minimum_coverage,
            )
            for note in notes:
                print(note)
            if ranking.empty:
                print("截止日之前没有可排名的报告期。")
                return
            _print_table(
                ranking,
                [
                    "industry_l3_name",
                    "industry_rank",
                    "company_id",
                    "company_name",
                    "weighted_score",
                    "market_cap",
                    "revenue",
                    "net_profit",
                    "revenue_yoy",
                    "profit_growth_adjusted",
                    "gross_margin",
                    "data_coverage",
                    "report_period",
                ],
            )
            if args.output:
                _write_industry_ranking(args.output, ranking)
                print(f"\n已写出 {args.output}")
            return

        if args.action == "list":
            frame = synced_industries()
            if frame.empty:
                print("尚未同步任何行业财报。")
                return
            _print_table(
                frame,
                ["industry_l3_id", "industry_l3_name", "company_count", "record_count", "latest_announcement"],
            )
        return

    if args.command == "constituents":
        coverage = industry_coverage()
        print(f"已固化 {coverage['industry_l3_id'].nunique()} 个三级行业、{int(coverage['company_count'].sum())} 家成分公司（快照 {SNAPSHOT_DATE}）")
        if args.industry:
            matched = coverage[
                coverage["industry_l3_name"].str.contains(args.industry, na=False)
                | coverage["industry_l3_id"].astype(str).eq(args.industry)
            ]
            if matched.empty:
                print("未匹配到已固化行业，可用行业：")
                print(coverage.to_string(index=False))
                return
            frame = constituents_for(matched["industry_l3_id"])
        elif args.stat_industry:
            industries = corresponding_industries(args.stat_industry)
            frame = constituents_for(industries["industry_l3_id"])
            print(f"{args.stat_industry} 对应 {len(industries)} 个三级行业，其中 {frame['industry_l3_id'].nunique()} 个已固化成分公司")
        else:
            print(coverage.to_string(index=False))
            return
        print(frame.to_string(index=False))
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.suffix.lower() == ".csv":
                frame.to_csv(output, index=False, encoding="utf-8-sig")
            else:
                frame.to_excel(output, index=False)
            print(f"已写出 {output}")


if __name__ == "__main__":
    main()
