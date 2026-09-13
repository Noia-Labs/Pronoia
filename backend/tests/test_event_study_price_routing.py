import unittest
from unittest.mock import patch

import pandas as pd

from backend.app.skills import cache
from backend.app.skills import analysis
from backend.app.skills.price_data import PriceFrame, resolve_security_ref


class TestEventStudyPriceRouting(unittest.TestCase):
    def setUp(self):
        cache.clear_cache()

    def tearDown(self):
        cache.clear_cache()

    @staticmethod
    def _price_frame(symbol, start_date=None, end_date=None, *, market=None, adjust="qfq"):
        dates = pd.bdate_range("2026-05-01", end_date).strftime("%Y-%m-%d")
        base = 10.0 if "000300" not in symbol else 20.0
        frame = pd.DataFrame({
            "date": dates,
            "open": [base + i * 0.1 for i in range(len(dates))],
            "close": [base + i * 0.1 for i in range(len(dates))],
            "high": [base + i * 0.1 for i in range(len(dates))],
            "low": [base + i * 0.1 for i in range(len(dates))],
            "volume": [1000] * len(dates),
        })
        return PriceFrame(
            security=resolve_security_ref(symbol, market),
            provider="bounded.test",
            frame=frame,
            attempts=("bounded.test",),
        )

    def test_as_of_routes_asset_and_benchmark_through_shared_fetcher(self):
        calls = []

        def fake_fetch(*args, **kwargs):
            calls.append((args, kwargs))
            return self._price_frame(*args, **kwargs)

        with patch.object(analysis, "fetch_price_frame", side_effect=fake_fetch):
            result = analysis.event_study(
                "000425", "2026-09-01", pre=30, post=30,
                index_symbol="sh000300", as_of=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(calls), 2)
        self.assertEqual({call[0][0] for call in calls}, {"sz000425", "sh000300"})
        self.assertEqual({call[1]["end_date"] for call in calls}, {"20260901"})
        self.assertEqual(result["meta"]["asset_provider"], "bounded.test")
        self.assertEqual(result["meta"]["benchmark_provider"], "bounded.test")
        self.assertIsNotNone(result["data"]["summary"]["pre5_cum_ar_pct"])
        self.assertEqual(result["data"]["summary"]["post3_car_endpoint_pct"], None)
        self.assertTrue(result["data"]["summary"]["postN_as_of_blocked"])
        self.assertLessEqual(
            max(row["date"] for row in result["data"]["window"]), "2026-09-01"
        )

    def test_preopen_cutoff_excludes_event_day_close(self):
        calls = []

        def fake_fetch(*args, **kwargs):
            calls.append((args, kwargs))
            return self._price_frame(*args, **kwargs)

        with patch.object(analysis, "fetch_price_frame", side_effect=fake_fetch):
            result = analysis.event_study(
                "000425", "2026-09-01", pre=30, post=30,
                index_symbol="sh000300", as_of=True,
                prediction_cutoff_at="2026-09-01T09:15:00+08:00",
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual({call[1]["end_date"] for call in calls}, {"20260831"})
        summary = result["data"]["summary"]
        self.assertFalse(summary["event_day_data_included"])
        self.assertEqual(summary["data_cutoff_date"], "2026-08-31")
        self.assertIsNone(summary["event_day_change_pct"])
        self.assertIsNone(summary["event_day_idx_change_pct"])
        self.assertIsNone(summary["event_day_ar_pct"])
        self.assertEqual(max(row["t"] for row in result["data"]["window"]), -1)
        self.assertLess(max(row["date"] for row in result["data"]["window"]), "2026-09-01")
        self.assertIsNotNone(summary["pre5_cum_ar_pct"])

    def test_identical_benchmark_request_uses_kline_cache(self):
        calls = []

        def fake_fetch(*args, **kwargs):
            calls.append(args[0])
            return self._price_frame(*args, **kwargs)

        with patch.object(analysis, "fetch_price_frame", side_effect=fake_fetch):
            first = analysis.event_study(
                "000425", "2026-09-01", pre=30,
                index_symbol="sh000300", as_of=True,
            )
            second = analysis.event_study(
                "000610", "2026-09-01", pre=30,
                index_symbol="sh000300", as_of=True,
            )

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(calls.count("sh000300"), 1)
        self.assertEqual(calls.count("sz000425"), 1)
        self.assertEqual(calls.count("sz000610"), 1)


if __name__ == "__main__":
    unittest.main()
