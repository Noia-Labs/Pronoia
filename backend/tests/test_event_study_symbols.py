"""CN exchange identity must survive the event-study skill/tool boundary."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from app.skills import analysis, market
from app.skills import skill as composite


class TestEventStudySymbols(unittest.IsolatedAsyncioTestCase):
    def test_normalization_preserves_explicit_exchange_and_resolves_etfs(self):
        cases = {
            "SH510410": "sh510410", "510410.SH": "sh510410",
            "510410": "sh510410", "516300": "sh516300",
            "563220": "sh563220", "588000": "sh588000",
            "159915": "sz159915", "159915.SZ": "sz159915",
            "600519": "sh600519", "600519.SH": "sh600519",
            "300750": "sz300750", "000001": "sz000001",
            "BJ920000": "bj920000", "920000.BJ": "bj920000",
            "sh000300": "sh000300", "sz399005": "sz399005",
        }
        for raw, expected in cases.items():
            with self.subTest(symbol=raw):
                self.assertEqual(market.norm_symbol(raw), expected)

    async def _assert_route(self, symbol, expected, *, index=False, keyword=False):
        dates = pd.bdate_range("2024-01-01", periods=30).strftime("%Y-%m-%d")
        prices = pd.DataFrame({"date": dates, "open": range(100, 130),
                               "high": range(101, 131), "low": range(99, 129),
                               "close": range(100, 130), "volume": [1000] * 30})

        async def dispatch(name, args):
            if name == "search_stock":
                return {"ok": True, "data": [{"symbol": symbol}]}
            self.assertEqual(name, "event_study")
            self.assertEqual(args["symbol"], expected)
            self.assertTrue(args["as_of"])
            self.assertEqual(args["post"], 0)
            # Bypass the cache so each alias exercises the actual tool route.
            return analysis.event_study.__wrapped__(**args)

        with (
            patch.object(composite, "execute_skill", dispatch),
            patch.object(analysis, "_fetch_stock_close", return_value=(prices, "offline-stock")) as stock,
            patch.object(analysis, "_fetch_a_share_index_close", return_value=(prices, "offline-index")) as idx,
            patch.object(analysis.ak, "stock_zh_index_daily", return_value=prices),
            patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")),
        ):
            result = await composite.event_study_skill(
                "2024-01-15", symbol=None if keyword else symbol,
                keyword="test ETF" if keyword else None,
                window_days=3, market="CN", as_of=True,
            )
        self.assertTrue(result["ok"], result)
        fetch = idx if index else stock
        self.assertEqual(fetch.call_args.args[0], expected)
        self.assertEqual((stock.call_count, idx.call_count), (0, 1) if index else (1, 0))
        # Fixing identifiers must not expose event-day or future prices.
        data = result["data"]
        self.assertTrue(data["postN_blocked"])
        self.assertIsNone(data["benchmark_relative_car_t3_pct"])
        self.assertIsNone(data["signal_event_day_change_pct"])
        self.assertTrue(all(row["date"] < "2024-01-15" for row in data["event_study"]["window"]))

    async def test_shanghai_etf_aliases_reach_shanghai_fetch(self):
        for raw, expected in (
            ("SH510410", "sh510410"), ("510410.SH", "sh510410"),
            ("510410", "sh510410"), ("SH516300", "sh516300"),
            ("516300", "sh516300"), ("SH513050", "sh513050"),
            ("SH563220", "sh563220"),
        ):
            with self.subTest(symbol=raw):
                await self._assert_route(raw, expected)

    async def test_other_explicit_exchanges_and_stocks_keep_their_routes(self):
        for raw, expected in (
            ("SZ159915", "sz159915"), ("159915.SZ", "sz159915"),
            ("600519", "sh600519"), ("SH600519", "sh600519"),
            ("300750", "sz300750"), ("SZ000001", "sz000001"),
            ("BJ920000", "bj920000"), ("920000.BJ", "bj920000"),
        ):
            with self.subTest(symbol=raw):
                await self._assert_route(raw, expected)

    async def test_explicit_indices_keep_index_fetch(self):
        for raw, expected in (
            ("SH000300", "sh000300"), ("000300.SH", "sh000300"),
            ("SZ399005", "sz399005"), ("399005.SZ", "sz399005"),
        ):
            with self.subTest(symbol=raw):
                await self._assert_route(raw, expected, index=True)

    async def test_keyword_search_preserves_resolved_exchange(self):
        for raw, expected in (("SH510410", "sh510410"), ("920000.BJ", "bj920000")):
            with self.subTest(symbol=raw):
                await self._assert_route(raw, expected, keyword=True)
