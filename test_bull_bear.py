"""A股牛熊状态评分算法单元测试。"""

from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bull_bear import (  # noqa: E402
    IndexDataError,
    PriceBundle,
    RegimeConfig,
    calibrate_series,
    compute_composite,
    compute_factors,
    compute_group_scores,
    compute_index_scores,
    load_prices,
    ruler_bands,
    state_of,
)
from regime_provider import compute_or_cached_report, load_cached_report  # noqa: E402


def _price_frame(count: int, offset: float = 0.0) -> pd.DataFrame:
    index = pd.bdate_range("2020-01-01", periods=count)
    steps = np.arange(count, dtype=float)
    close = 3000.0 + offset + steps * 2.0 + np.sin(steps / 7.0) * 30.0
    return pd.DataFrame(
        {
            "open": close - 1.0,
            "high": close + 2.0,
            "low": close - 2.0,
            "close": close,
            "volume": np.full(count, 1000.0),
        },
        index=index,
    )


def _bundle(config: RegimeConfig, count: int) -> PriceBundle:
    prices = {code: _price_frame(count, index * 7.0) for index, code in enumerate(config.codes)}
    fills = {code: pd.Series(False, index=frame.index) for code, frame in prices.items()}
    return PriceBundle(prices=prices, fills=fills)


def _rank_pct(values: np.ndarray) -> float:
    current = values[-1]
    less = int(np.sum(values < current))
    equal = int(np.sum(values == current))
    return (less + (equal + 1) / 2.0) / len(values)


class BullBearAlgorithmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = RegimeConfig()

    def test_factors_match_manual_formula(self) -> None:
        bundle = _bundle(self.config, 1600)
        factors = compute_factors(bundle, self.config)
        code = "000016"
        close = bundle.prices[code]["close"].to_numpy()
        t = 1500
        ma60 = close[t - 59 : t + 1].mean()
        ma60_prev = close[t - 20 - 59 : t - 20 + 1].mean()
        ma120 = close[t - 119 : t + 1].mean()
        ma120_prev = close[t - 60 - 119 : t - 60 + 1].mean()
        expected_f1 = (close[t] - ma60) / ma60
        expected_f2 = ma60 / ma60_prev - 1.0
        expected_f3 = ma120 / ma120_prev - 1.0

        row = factors[(factors["code"] == code) & (factors["date"] == bundle.prices[code].index[t])].iloc[0]
        self.assertAlmostEqual(float(row["f1"]), expected_f1, places=10)
        self.assertAlmostEqual(float(row["f2"]), expected_f2, places=10)
        self.assertAlmostEqual(float(row["f3"]), expected_f3, places=10)

        for key, expected in (("z1", expected_f1), ("z2", expected_f2), ("z3", expected_f3)):
            if key == "z1":
                window = ((close - pd.Series(close).rolling(60).mean()) / pd.Series(close).rolling(60).mean()).to_numpy()
            elif key == "z2":
                fast = pd.Series(close).rolling(60).mean()
                window = (fast / fast.shift(20) - 1.0).to_numpy()
            else:
                slow = pd.Series(close).rolling(120).mean()
                window = (slow / slow.shift(60) - 1.0).to_numpy()
            percentile = _rank_pct(window[t - 1249 : t + 1])
            self.assertAlmostEqual(float(row[key]), 2.0 * percentile - 1.0, places=6)

    def test_no_lookahead_when_future_data_removed(self) -> None:
        bundle = _bundle(self.config, 1400)
        factors = compute_factors(bundle, self.config)
        full_scores = compute_index_scores(factors, self.config)

        cutoff = bundle.prices["000016"].index[1300]
        trimmed_prices = {code: frame.loc[:cutoff] for code, frame in bundle.prices.items()}
        trimmed = PriceBundle(
            prices=trimmed_prices,
            fills={code: pd.Series(False, index=frame.index) for code, frame in trimmed_prices.items()},
        )
        trimmed_scores = compute_index_scores(compute_factors(trimmed, self.config), self.config)

        before = full_scores[full_scores["date"] <= cutoff].reset_index(drop=True)
        after = trimmed_scores.reset_index(drop=True)
        self.assertEqual(len(before), len(after))
        pd.testing.assert_frame_equal(before, after, check_dtype=False)

    def test_breadth_and_composite_formula(self) -> None:
        date = pd.Timestamp("2026-01-05")
        scores = pd.DataFrame(
            [
                {"date": date, "code": "000016", "score": 50.0, "state": "BULL", "insufficient": False},
                {"date": date, "code": "000300", "score": 30.0, "state": "BULL", "insufficient": False},
                {"date": date, "code": "000905", "score": -10.0, "state": "RANGE", "insufficient": False},
                {"date": date, "code": "399006", "score": 60.0, "state": "BULL", "insufficient": False},
                {"date": date, "code": "000688", "score": 40.0, "state": "BULL", "insufficient": False},
            ]
        )
        above = [True, True, False, True, True]
        factors = pd.DataFrame(
            {
                "date": [date] * 5,
                "code": scores["code"],
                "above_ma60": above,
            }
        )
        groups = compute_group_scores(scores, self.config)
        composite = compute_composite(scores, factors, groups, self.config)
        row = composite.iloc[0]
        self.assertAlmostEqual(float(row["s_base"]), 34.0, places=8)
        self.assertAlmostEqual(float(row["breadth"]), 0.8, places=8)
        self.assertAlmostEqual(float(row["z_b"]), 0.6, places=8)
        self.assertAlmostEqual(float(row["score"]), 40.12, places=8)
        self.assertEqual(str(row["state"]), "BULL")

    def test_state_thresholds_are_configurable(self) -> None:
        bounds = {"bear": -26.0, "bull": 16.0}
        self.assertEqual(state_of(-30.0, bounds), "BEAR")
        self.assertEqual(state_of(0.0, bounds), "RANGE")
        self.assertEqual(state_of(30.0, bounds), "BULL")
        self.assertEqual(state_of(22.0, {"bear": -20.0, "bull": 30.0}), "RANGE")

    def test_cache_reuse_and_fetch_failure(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        config = replace(self.config, cache_dir=Path(temp_dir.name) / "regime")
        calls: list[str] = []

        def fetcher(code: str, start: str, end: str | None) -> pd.DataFrame:
            calls.append(code)
            return _price_frame(300, float(len(calls)))

        first = load_prices(config, force_refresh=True, fetcher=fetcher, check_names=False)
        self.assertEqual(len(first.prices), 5)
        self.assertEqual(len(calls), 5)

        def broken(code: str, start: str, end: str | None) -> pd.DataFrame:
            raise RuntimeError("network down")

        second = load_prices(config, force_refresh=False, fetcher=broken, check_names=False)
        self.assertEqual(len(second.prices), 5)
        self.assertEqual(second.fetched, [])

        third = load_prices(config, force_refresh=True, fetcher=broken, check_names=False)
        self.assertEqual(len(third.prices), 5)
        self.assertEqual(third.fetched, [])
        self.assertTrue(
            any("`000016`（上证50） 行情暂不可用，已使用本地历史缓存" in item for item in third.warnings)
        )

        empty_dir = Path(temp_dir.name) / "empty_regime"
        empty_config = replace(config, cache_dir=empty_dir)
        with self.assertRaises(IndexDataError) as context:
            load_prices(empty_config, force_refresh=True, fetcher=broken, check_names=False)
        self.assertIn("`000016`（上证50） 行情拉取失败", str(context.exception))

    def test_ruler_bands_follow_target_shares(self) -> None:
        config = replace(
            self.config,
            target_shares={"BEAR": 0.20, "RANGE": 0.65, "BULL": 0.15},
        )
        bear, bull = ruler_bands(config)
        self.assertAlmostEqual(bear, -60.0)
        self.assertAlmostEqual(bull, 70.0)
        self.assertAlmostEqual(bear + 100.0, 40.0)
        self.assertAlmostEqual(100.0 - bull, 30.0)

    def test_calibration_shares_and_suggestion(self) -> None:
        values = pd.Series(np.linspace(-100.0, 100.0, 401))
        result = calibrate_series(values, {"bear": -50.0, "bull": 50.0}, step=5.0)
        self.assertAlmostEqual(result["shares"]["BEAR"], 0.25, delta=0.01)
        self.assertAlmostEqual(result["shares"]["BULL"], 0.25, delta=0.01)
        self.assertIn("suggested", result)
        self.assertIn("walk_forward", result)

    def test_cached_report_loads_without_recalculation(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        base = Path(temp_dir.name)
        latest_path = base / "latest.json"
        history_path = base / "history.csv"
        config_path = base / "config.json"

        payload = self.config.to_dict()
        payload["latest_path"] = str(latest_path)
        payload["history_path"] = str(history_path)
        config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        self.assertIsNone(load_cached_report(config_path))

        latest_path.write_text(json.dumps({"score": 12.3, "indices": []}), encoding="utf-8")
        pd.DataFrame(
            {"date": ["2026-01-05", "2026-01-06"], "S": [10.0, 12.3], "state": ["震荡市", "震荡市"]}
        ).to_csv(history_path, index=False, encoding="utf-8-sig")

        cached = load_cached_report(config_path)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertTrue(cached["cached"])
        self.assertAlmostEqual(float(cached["latest"]["score"]), 12.3)
        self.assertEqual(len(cached["composite"]), 2)
        self.assertIn("score", cached["composite"].columns)

    def test_compute_falls_back_to_cached_report(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        base = Path(temp_dir.name)
        latest_path = base / "latest.json"
        history_path = base / "history.csv"
        config_path = base / "config.json"

        payload = self.config.to_dict()
        payload["latest_path"] = str(latest_path)
        payload["history_path"] = str(history_path)
        config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        latest_path.write_text(json.dumps({"score": 12.3, "indices": []}), encoding="utf-8")
        pd.DataFrame(
            {"date": ["2026-01-05"], "S": [12.3], "state": ["震荡市"]}
        ).to_csv(history_path, index=False, encoding="utf-8-sig")

        with patch(
            "regime_provider.compute_full_report",
            side_effect=RuntimeError("`000016`（上证50）行情拉取失败：限流"),
        ):
            report = compute_or_cached_report(config_path=config_path)

        self.assertTrue(report["cached"])
        self.assertIn("fallback_reason", report)
        self.assertIn("`000016`（上证50）", report["fallback_reason"])
        self.assertIn("改用已缓存的上次估算结果", report["latest"]["warnings"][0])


if __name__ == "__main__":
    unittest.main()
