"""Offline regression tests for explicit Oracle market routing."""
from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from app.event_backtest import labeller


def _series(values: list[float], start: str = "2025-01-02") -> pd.Series:
    dates = pd.bdate_range(start, periods=len(values)).date
    return pd.Series(values, index=dates, dtype=float)


class TestOracleMarketRouting(unittest.TestCase):
    def test_prediction_and_oracle_share_market_defaults(self):
        from app.event_backtest.benchmark import resolve_event_benchmark

        self.assertEqual(resolve_event_benchmark("US", None), "SPY")
        self.assertEqual(labeller._yf_benchmark_for(None, "US"), "SPY")
        self.assertEqual(resolve_event_benchmark("US", "QQQ"), "QQQ")
        self.assertEqual(labeller._yf_benchmark_for("QQQ", "US"), "QQQ")

        self.assertEqual(resolve_event_benchmark("HK", None), "cash")
        self.assertIsNone(labeller._yf_benchmark_for(None, "HK"))
        self.assertEqual(resolve_event_benchmark("FUTURES", None), "cash")
        self.assertIsNone(labeller._yf_benchmark_for(None, "FUTURES"))

    def test_cn_benchmark_spellings_and_cash_protocol(self):
        self.assertEqual(labeller._yf_benchmark_for("000300.SH", "CN"), "sh000300")
        self.assertEqual(labeller._yf_benchmark_for("000905.SH", "CN"), "sh000905")
        self.assertIsNone(labeller._yf_benchmark_for("cash", "CN"))

    def test_timezone_aware_event_time_uses_exchange_local_clock(self):
        # 14:00 UTC is 09:00 New York on this winter date.
        value = "2025-01-02T14:00:00Z"
        self.assertEqual(labeller._parse_date(value, "US"), dt.date(2025, 1, 2))
        self.assertEqual(labeller._announcement_tier(value, "US"), "pre_open")
        # 16:30 UTC has already crossed midnight in Shanghai.
        self.assertEqual(
            labeller._parse_date("2025-01-02T16:30:00Z", "CN"),
            dt.date(2025, 1, 3),
        )

    def test_hk_and_futures_provider_frames_are_normalized(self):
        hk_frame = pd.DataFrame({
            "日期": ["2025-01-02", "2025-01-03"],
            "收盘": [100.0, 102.5],
        })
        futures_fallback = pd.DataFrame({
            "date": ["2025-01-02", "2025-01-03"],
            "close": [3500.0, 3542.0],
        })
        fake_ak = SimpleNamespace(
            stock_hk_hist=MagicMock(return_value=hk_frame),
            stock_hk_daily=MagicMock(),
            futures_main_sina=MagicMock(side_effect=RuntimeError("primary unavailable")),
            futures_zh_daily_sina=MagicMock(return_value=futures_fallback),
        )
        with patch.object(labeller, "ak", fake_ak):
            hk = labeller._ak_hk_hist(
                "700.HK", "2025-01-01", "2025-01-04", retries=1, sleep_s=0
            )
            futures = labeller._ak_futures_hist(
                "rb0", "2025-01-01", "2025-01-04", retries=1, sleep_s=0
            )

        self.assertEqual(list(hk.index), [dt.date(2025, 1, 2), dt.date(2025, 1, 3)])
        self.assertEqual(float(hk.iloc[-1]), 102.5)
        fake_ak.stock_hk_hist.assert_called_once_with(
            symbol="00700", period="daily", start_date="20250101",
            end_date="20250104", adjust="qfq",
        )
        self.assertEqual(float(futures.iloc[-1]), 3542.0)
        fake_ak.futures_main_sina.assert_called_once_with(
            symbol="RB0", start_date="20250101", end_date="20250104"
        )
        fake_ak.futures_zh_daily_sina.assert_called_once_with(symbol="RB0")

    def test_hk_benchmark_car_uses_hk_routes_not_us(self):
        asset = _series([100.0, 101.0, 103.0, 110.0] + [110.0] * 67)
        benchmark = _series([100.0, 100.5, 101.0, 102.0] + [102.0] * 67)
        event = labeller.RawEvent(
            event_id="hk-event",
            market="HK",
            symbol="00700",
            event_date=asset.index[0],
            benchmark="HSI",
        )
        with patch.object(labeller, "_ak_hk_hist", return_value=asset) as hk_fetch, \
             patch.object(labeller, "_ak_hk_index_hist", return_value=benchmark) as idx_fetch, \
             patch.object(labeller, "_ak_us_hist", side_effect=AssertionError("US route used")):
            result = labeller._compute_cars_for_events([event])

        row = result[(event.event_id, event.market, event.symbol)]
        self.assertEqual(row["asset_ticker"], "00700.HK")
        self.assertEqual(row["benchmark_ticker"], "HSI")
        self.assertEqual(row["car_method"], "benchmark_relative_return")
        # Date-only publication time is unknown, so the conservative close
        # anchor is the next trading close: 101→110 minus 100.5→102.
        self.assertAlmostEqual(
            row["car_t3"],
            (110.0 / 101.0 - 1.0) - (102.0 / 100.5 - 1.0),
            places=10,
        )
        hk_fetch.assert_called_once()
        idx_fetch.assert_called_once()

    def test_futures_without_benchmark_uses_realized_asset_return(self):
        prices = _series([100.0, 102.0, 104.0, 106.0] + [106.0] * 67)
        event = labeller.RawEvent(
            event_id="future-event",
            market="FUTURES",
            symbol="rb0",
            event_date=prices.index[0],
            benchmark=None,
        )
        with patch.object(labeller, "_ak_futures_hist", return_value=prices) as fetch, \
             patch.object(labeller, "_ak_us_hist", side_effect=AssertionError("US route used")):
            result = labeller._compute_cars_for_events([event])

        row = result[(event.event_id, event.market, event.symbol)]
        self.assertEqual(row["asset_ticker"], "RB0")
        self.assertIsNone(row["benchmark_ticker"])
        self.assertIsNone(row["bm_t3"])
        self.assertEqual(row["car_method"], "raw_asset_return")
        # Unknown publication time: next trading close is T0, then 3 closes.
        self.assertAlmostEqual(row["t3"], 106.0 / 102.0 - 1.0, places=10)
        self.assertAlmostEqual(row["car_t3"], row["t3"], places=12)
        fetch.assert_called_once()

    def test_crypto_and_fx_fail_before_any_provider_call(self):
        event = labeller.RawEvent(
            event_id="crypto-event",
            market="CRYPTO",
            symbol="BTCUSDT",
            event_date=dt.date(2025, 1, 2),
        )
        with patch.object(labeller, "_ak_us_hist") as us_fetch:
            with self.assertRaisesRegex(
                labeller.UnsupportedOracleMarketError,
                "Oracle 暂不支持 CRYPTO",
            ):
                labeller._compute_cars_for_events([event])
        us_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
