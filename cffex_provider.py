"""中金所沪深300股指期货（IF）日K线采集与缓存。

数据源：中金所官网「历史数据下载」月度包
  http://www.cffex.com.cn/sj/historysj/{YYYYMM}/zip/{YYYYMM}.zip
  包内每个交易日一个 {YYYYMMDD}_1.csv（股指期货全部合约，GBK 编码），
  列：合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,今收盘,今结算,前结算,涨跌1,涨跌2,Delta
官网公开数据、无反爬拦截，可直接 requests 采集。

主力连续定义：每日 IF 各合约中成交量最大者。缓存于
  data/prosperity/cffex_if_daily.csv（同日记录按最新更新覆盖）。
"""

from __future__ import annotations

import io
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

DEFAULT_IF_PATH = Path(__file__).parent / "data" / "prosperity" / "cffex_if_daily.csv"

_IF_COLUMNS = (
    "date",
    "contract",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover_yi",
    "open_interest",
    "settle",
    "prev_settle",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class CffexError(Exception):
    """中金所数据采集或解析失败。"""


def _month_zip_url(year_month: int) -> str:
    return f"http://www.cffex.com.cn/sj/historysj/{year_month}/zip/{year_month}.zip"


def _parse_daily_csv(raw: bytes, trade_date: str) -> list[dict[str, object]]:
    """解析单日股指期货 CSV，返回全部 IF 合约记录。"""
    text = raw.decode("gb18030", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    header = [cell.strip() for cell in lines[0].split(",")]
    required = {"合约代码", "今开盘", "最高价", "最低价", "成交量", "成交金额", "持仓量", "今收盘", "今结算", "前结算"}
    if not required.issubset(set(header)):
        raise CffexError(f"{trade_date} CSV 表头不符：{header[:8]}...")
    idx = {name: header.index(name) for name in required}

    records: list[dict[str, object]] = []
    for line in lines[1:]:
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) <= max(idx.values()) or not cells[idx["合约代码"]].startswith("IF"):
            continue
        try:
            records.append(
                {
                    "date": trade_date,
                    "contract": cells[idx["合约代码"]],
                    "open": float(cells[idx["今开盘"]]),
                    "high": float(cells[idx["最高价"]]),
                    "low": float(cells[idx["最低价"]]),
                    "close": float(cells[idx["今收盘"]]),
                    "volume": int(float(cells[idx["成交量"]])),
                    "turnover_yi": float(cells[idx["成交金额"]]) / 1e4 if cells[idx["成交金额"]] else 0.0,
                    "open_interest": int(float(cells[idx["持仓量"]])),
                    "settle": float(cells[idx["今结算"]]),
                    "prev_settle": float(cells[idx["前结算"]]),
                }
            )
        except (ValueError, IndexError):
            continue
    return records


def parse_month_zip(raw: bytes) -> pd.DataFrame:
    """解析月度 zip，返回该月 IF 各合约日行情。"""
    records: list[dict[str, object]] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        for name in sorted(bundle.namelist()):
            stem = Path(name).stem
            trade_date = stem[:-2] if stem.endswith("_1") else stem
            if not trade_date.isdigit() or len(trade_date) != 8:
                continue
            records.extend(_parse_daily_csv(bundle.read(name), trade_date))
    if not records:
        raise CffexError("月度包中没有 IF 行情记录")
    return pd.DataFrame(records, columns=_IF_COLUMNS)


def dominant_series(frame: pd.DataFrame) -> pd.DataFrame:
    """按日选主力合约（成交量最大），返回主力连续日K线。"""
    if frame.empty:
        return frame
    frame = frame.sort_values(["date", "volume"], ascending=[True, False])
    dominant = frame.drop_duplicates("date", keep="first").sort_values("date")
    return dominant.reset_index(drop=True)


def load_if_daily(path: str | Path = DEFAULT_IF_PATH) -> pd.DataFrame:
    source = Path(path)
    if not source.exists():
        return pd.DataFrame(columns=_IF_COLUMNS)
    frame = pd.read_csv(source, dtype={"date": str, "contract": str})
    return frame


def merge_if_daily(base: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合并缓存：同日新记录覆盖（主力合约换月会改写历史主力），其余保留。"""
    if new.empty:
        return base
    if base.empty:
        return new.sort_values("date").reset_index(drop=True)
    merged = pd.concat([base[~base["date"].isin(set(new["date"]))], new], ignore_index=True)
    return merged.sort_values("date").reset_index(drop=True)


def save_if_daily(frame: pd.DataFrame, path: str | Path = DEFAULT_IF_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False)
    return target


def update_if_daily(months: int = 24, path: str | Path = DEFAULT_IF_PATH) -> pd.DataFrame:
    """回补最近 months 个月的月度包并合并入缓存。返回更新后的主力连续日K线。"""
    today = datetime.now()
    year_months: list[int] = []
    for offset in range(months):
        month_index = today.year * 12 + today.month - 1 - offset
        year_months.append(month_index // 12 * 100 + month_index % 12 + 1)

    session = requests.Session()
    session.headers.update(_HEADERS)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    frame = load_if_daily(path)
    before_rows, before_min, before_max = len(frame), frame["date"].min(), frame["date"].max()

    for year_month in sorted(year_months):
        url = _month_zip_url(year_month)
        try:
            resp = session.get(url, timeout=60)
        except requests.RequestException as exc:
            raise CffexError(f"下载失败：{url}（{exc}）") from exc
        if resp.status_code != 200 or resp.content[:2] != b"PK":
            print(f"  {year_month}：无月度包（{resp.status_code}），跳过")
            continue
        month_frame = parse_month_zip(resp.content)
        daily = dominant_series(month_frame)
        daily["fetched_at"] = fetched_at
        frame = merge_if_daily(frame, daily)
        print(f"  {year_month}：{len(daily)} 个交易日主力记录")

    target = Path(path or DEFAULT_IF_PATH)
    if before_rows and target.exists():
        shutil.copy2(target, target.with_suffix(".csv.bak"))
    save_if_daily(frame, path)
    print(f"已写入：{before_rows} 行（{before_min}~{before_max}）→ {len(frame)} 行"
          f"（{frame['date'].min()}~{frame['date'].max()}）")
    if before_rows and target.exists():
        print(f"原数据备份于 {target.with_suffix('.csv.bak')}")
    return frame
