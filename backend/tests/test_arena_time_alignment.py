"""Regression tests for explicit Arena calendar alignment and honest gaps."""
from __future__ import annotations

import unittest
from unittest.mock import patch


class TestArenaTimeAlignment(unittest.TestCase):
    @staticmethod
    def _context(run_id: str, *, mixed: bool = True, visibility: str = "private"):
        from app.event_backtest.arena import ArenaRunContext

        return ArenaRunContext(
            run_id=run_id,
            run_info={
                "id": run_id,
                "name": f"策略 {run_id}",
                "engine_mode": "portfolio",
                "visibility": visibility,
                "_arena_allow_mixed_protocols": mixed,
                "_arena_formal_eligible": True,
            },
            metrics={
                "strategy_total_return": {"value": 0.1, "meta": {"n_event_trades": 10}},
            },
        )

    @staticmethod
    def _payload(dates: list[str], values: list[float], *, frequency: str = "1d") -> dict:
        return {
            "status": "available",
            "engine_mode": "portfolio",
            "dataset": {"frequency": frequency},
            "summary": {
                "n_trades": 1,
                "total_return": values[-1] / values[0] - 1,
                "annualized_return": 0.25,
                "max_drawdown": 0.1,
            },
            "equity_curve": [
                {"timestamp": date, "net_value": value}
                for date, value in zip(dates, values)
            ],
        }

    def _build(self, payloads: dict[str, dict], alignment: dict, *, same_protocol: bool = False):
        from app.event_backtest.arena import _build_performance_curves

        contexts = [self._context(run_id, mixed=not same_protocol) for run_id in payloads]
        protocol_hashes = {
            run_id: ("same" if same_protocol else f"protocol-{run_id}")
            for run_id in payloads
        }
        with patch(
            "app.event_backtest.performance.build_unified_performance",
            side_effect=lambda run, include_kline=False: payloads[run["id"]],
        ):
            return _build_performance_curves(
                contexts,
                run_protocol_hashes=protocol_hashes,
                time_alignment=alignment,
            )

    def test_intersection_rebases_each_real_curve_and_separates_metric_views(self):
        result = self._build({
            "a": self._payload(
                ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"],
                [1.0, 1.1, 1.2, 1.3, 1.4],
            ),
            "b": self._payload(
                ["2025-01-03", "2025-01-04", "2025-01-05", "2025-01-06", "2025-01-07"],
                [1.0, 1.1, 1.2, 1.3, 1.4],
            ),
        }, {"mode": "intersection", "frequency": "native", "min_observations": 2})

        self.assertEqual(result["alignment"]["window_start"][:10], "2025-01-03")
        self.assertEqual(result["alignment"]["window_end"][:10], "2025-01-05")
        self.assertTrue(result["alignment"]["no_interpolation"])
        self.assertFalse(result["alignment"]["upsampling_allowed"])
        self.assertFalse(result["alignment"]["ranking_allowed"])
        self.assertIn("total_return", result["native_metrics"]["a"])
        self.assertAlmostEqual(result["aligned_metrics"]["a"]["total_return"], 1.4 / 1.2 - 1)
        self.assertAlmostEqual(result["aligned_metrics"]["b"]["total_return"], 0.2)
        for series in result["series"]:
            self.assertEqual(series["points"][0]["net_value"], 1.0)
            self.assertEqual(len(series["points"]), 3)
            self.assertTrue(all(point.get("timestamp") for point in series["points"]))

    def test_union_preserves_real_coverage_without_filling_outer_ranges(self):
        result = self._build({
            "early": self._payload(["2025-01-01", "2025-01-02", "2025-01-03"], [1.0, 1.1, 1.2]),
            "late": self._payload(["2025-01-03", "2025-01-04", "2025-01-05"], [1.0, 0.9, 1.1]),
        }, {"mode": "union", "frequency": "native", "min_observations": 2})

        early = result["coverage"]["by_run"]["early"]
        late = result["coverage"]["by_run"]["late"]
        self.assertTrue(early["has_trailing_gap"])
        self.assertTrue(late["has_leading_gap"])
        self.assertLess(early["window_coverage_ratio"], 1.0)
        self.assertLess(late["window_coverage_ratio"], 1.0)
        self.assertEqual(len(next(x for x in result["series"] if x["run_id"] == "early")["points"]), 3)
        self.assertEqual(len(next(x for x in result["series"] if x["run_id"] == "late")["points"]), 3)
        self.assertEqual(result["alignment"]["ranking_reason"], "exploration_mode_not_fair_ranking")

    def test_manual_weekly_downsample_keeps_last_genuine_observation_only(self):
        dates = [f"2025-01-{day:02d}" for day in range(1, 16)]
        values = [1.0 + index / 100 for index in range(len(dates))]
        result = self._build(
            {"a": self._payload(dates, values), "b": self._payload(dates, values)},
            {
                "mode": "manual", "frequency": "week",
                "start_date": "2025-01-01", "end_date": "2025-01-15",
                "min_observations": 2,
            },
            same_protocol=True,
        )
        points = result["series"][0]["points"]
        self.assertEqual([point["timestamp"][:10] for point in points], ["2025-01-01", "2025-01-05", "2025-01-12", "2025-01-15"])
        self.assertAlmostEqual(result["aligned_metrics"]["a"]["total_return"], 0.14)
        self.assertEqual(result["coverage"]["by_run"]["a"]["effective_frequency"], "week")
        self.assertTrue(result["alignment"]["ranking_allowed"])
        self.assertEqual(result["aligned_ranking"]["total_return"][0]["rank"], 1)

    def test_display_downsampling_does_not_hide_intraperiod_drawdown(self):
        dates = [f"2025-01-{day:02d}" for day in range(1, 6)]
        values = [1.0, 1.4, 0.7, 1.3, 1.5]
        result = self._build(
            {"a": self._payload(dates, values), "b": self._payload(dates, values)},
            {
                "mode": "manual", "frequency": "month",
                "start_date": "2025-01-01", "end_date": "2025-01-05",
                "min_observations": 2,
            },
            same_protocol=True,
        )
        # The chart only needs the first and month-close observations, but risk
        # is measured on every native point and therefore sees the 50% trough.
        self.assertEqual(len(result["series"][0]["points"]), 2)
        metrics = result["aligned_metrics"]["a"]
        self.assertAlmostEqual(metrics["max_drawdown"], 0.5)
        self.assertEqual(metrics["n_observations"], 5)
        self.assertEqual(metrics["n_display_observations"], 2)
        self.assertEqual(metrics["metric_frequency"], "native")

    def test_event_proxy_undated_baseline_preserves_first_return_and_drawdown(self):
        event_payload = {
            "status": "available",
            "engine_mode": "event_proxy",
            "summary": {
                "n_trades": 2,
                "n_valid_oracle": 2,
                "total_return": 0.0,
                "max_drawdown": 0.2,
            },
            "equity_curve": [
                {"index": 0, "timestamp": None, "net_value": 1.0},
                {"index": 1, "timestamp": "2025-01-01", "net_value": 0.8},
                {"index": 2, "timestamp": "2025-01-02", "net_value": 1.0},
            ],
        }
        result = self._build(
            {"a": event_payload, "b": event_payload},
            {"mode": "intersection", "frequency": "native", "min_observations": 2},
            same_protocol=True,
        )
        self.assertEqual(result["native_metrics"]["a"]["total_return"], 0.0)
        self.assertEqual(result["native_metrics"]["a"]["max_drawdown"], 0.2)
        self.assertAlmostEqual(result["aligned_metrics"]["a"]["total_return"], 0.0)
        self.assertAlmostEqual(result["aligned_metrics"]["a"]["max_drawdown"], 0.2)
        self.assertEqual(
            [point["net_value"] for point in result["series"][0]["points"]],
            [1.0, 0.8, 1.0],
        )

    def test_arena_safe_union_uses_common_opaque_slots_and_keeps_segments(self):
        from app.event_backtest.arena import _build_performance_curves

        contexts = [
            self._context("early", visibility="arena_safe"),
            self._context("late", visibility="arena_safe"),
        ]
        payloads = {
            "early": self._payload(
                ["2025-01-01", "2025-01-02", "2025-01-20"],
                [1.0, 1.1, 1.2],
            ),
            "late": self._payload(
                ["2025-01-02", "2025-01-20", "2025-01-21"],
                [1.0, 0.9, 1.1],
            ),
        }
        with patch(
            "app.event_backtest.performance.build_unified_performance",
            side_effect=lambda run, include_kline=False: payloads[run["id"]],
        ):
            result = _build_performance_curves(
                contexts,
                run_protocol_hashes={"early": "p-early", "late": "p-late"},
                time_alignment={"mode": "union", "frequency": "native", "min_observations": 2},
            )
        early = next(item for item in result["series"] if item["run_id"] == "early")
        late = next(item for item in result["series"] if item["run_id"] == "late")
        # Privacy bucketing keeps endpoints, whose x positions still refer to
        # the same hidden global timeline rather than restarting both at zero.
        self.assertEqual([point["index"] for point in early["points"]], [0, 2])
        self.assertEqual([point["index"] for point in late["points"]], [1, 3])
        self.assertEqual(set(early["points"][-1]), {"index", "net_value", "segment"})
        self.assertEqual(early["points"][-1]["segment"], 1)
        self.assertTrue(all("timestamp" not in point for point in early["points"] + late["points"]))
        self.assertEqual(
            result["privacy"]["alignment_index_policy"],
            "global_opaque_slot_with_segmented_gaps",
        )

    def test_finer_requested_frequency_does_not_upsample_monthly_source(self):
        result = self._build({
            "a": self._payload(["2025-01-31", "2025-02-28", "2025-03-31"], [1.0, 1.1, 1.2], frequency="1mo"),
            "b": self._payload(["2025-01-31", "2025-02-28", "2025-03-31"], [1.0, 0.9, 1.1], frequency="1mo"),
        }, {"mode": "intersection", "frequency": "day", "min_observations": 2})
        coverage = result["coverage"]["by_run"]["a"]
        self.assertEqual(coverage["effective_frequency"], "native")
        self.assertEqual(coverage["aligned_observations"], 3)
        self.assertTrue(any("without_upsampling" in warning for warning in coverage["warnings"]))

    def test_internal_missing_period_starts_a_new_segment_and_short_sample_is_explicit(self):
        result = self._build({
            "a": self._payload(
                ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-20"],
                [1.0, 1.1, 1.2, 1.3, 1.4],
            ),
            "b": self._payload(
                ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-20"],
                [1.0, 1.0, 1.1, 1.2, 1.3],
            ),
        }, {"mode": "intersection", "frequency": "day", "min_observations": 10})
        first = next(item for item in result["series"] if item["run_id"] == "a")
        self.assertEqual([point["segment"] for point in first["points"]], [0, 0, 0, 0, 1])
        self.assertEqual(len(result["coverage"]["by_run"]["a"]["missing_segments"]), 1)
        self.assertEqual(result["coverage"]["by_run"]["a"]["status"], "insufficient")
        self.assertIn("aligned_sample_short", result["coverage"]["by_run"]["a"]["warnings"])
        self.assertEqual(result["aligned_metrics"]["a"]["status"], "insufficient")

    def test_non_overlapping_intersection_is_explicitly_unavailable(self):
        result = self._build({
            "a": self._payload(["2025-01-01", "2025-01-02"], [1.0, 1.1]),
            "b": self._payload(["2025-02-01", "2025-02-02"], [1.0, 1.1]),
        }, {"mode": "intersection", "frequency": "native", "min_observations": 2})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "no_common_time_window")
        self.assertEqual({item["reason"] for item in result["series"]}, {"no_common_time_window"})


if __name__ == "__main__":
    unittest.main()
