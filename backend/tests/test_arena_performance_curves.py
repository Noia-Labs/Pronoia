"""Regression coverage for Arena's privacy-safe performance curve overlay."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class TestArenaPerformanceCurves(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.events = self._write_jsonl("events.jsonl", [
            {
                "event_id": "e1", "market": "CN", "symbol": "600000",
                "event_time": "2025-01-10T09:00:00+08:00", "event_type_l2": "公告",
                "title": "事件一", "event_text": "事实一", "source_url": "manual://e1",
            },
            {
                "event_id": "e2", "market": "CN", "symbol": "600001",
                "event_time": "2025-02-10T09:00:00+08:00", "event_type_l2": "公告",
                "title": "事件二", "event_text": "事实二", "source_url": "manual://e2",
            },
        ])
        self.label_rows = [
            {"event_id": "e1", "label_t1": "up", "label_t3": "up", "label_t5": "up", "car_t3": .10},
            {"event_id": "e2", "label_t1": "up", "label_t3": "up", "label_t5": "up", "car_t3": .05},
        ]
        self.labels = self._write_jsonl("labels.jsonl", self.label_rows)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write_jsonl(self, name: str, rows: list[dict]) -> Path:
        path = self.root / name
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return path

    def _context(self, run_id: str, directions: list[str], *, visibility: str = "private"):
        from app.event_backtest.arena import ArenaRunContext
        from app.event_backtest.protocol import build_protocol_hash

        predictions = self._write_jsonl(f"{run_id}.jsonl", [
            {
                "event_id": event_id,
                "pred_direction": direction,
                "run_id": run_id,
                "confidence": .8,
                "horizon": "t3",
            }
            for event_id, direction in zip(("e1", "e2"), directions)
        ])
        execution_spec = {"fee_bps": 0, "slippage_bps": 0, "initial_capital": 100_000}
        evaluation_protocol = {"evaluation_horizon": "t3"}
        protocol_hash = build_protocol_hash(
            events_path=self.events,
            labels_path=self.labels,
            execution_spec=execution_spec,
            evaluation_protocol=evaluation_protocol,
        )
        return ArenaRunContext(
            run_id=run_id,
            run_info={
                "id": run_id,
                "name": f"策略 {run_id}",
                "events_path": str(self.events),
                "labels_path": str(self.labels),
                "out_path": str(predictions),
                "protocol_hash": protocol_hash,
                "visibility": visibility,
                "execution_spec": execution_spec,
                "config": {"evaluation_horizon": "t3", "evaluation_protocol": evaluation_protocol},
            },
            metrics={
                "strategy_total_return": {
                    "value": .10 if directions[0] == "up" else -.10,
                    "meta": {"n_event_trades": 2},
                },
            },
            predictions_by_eid={
                event_id: {"pred_direction": direction, "abstain": False}
                for event_id, direction in zip(("e1", "e2"), directions)
            },
        )

    def test_performance_curves_are_comparable_and_aggregate_only(self):
        from app.event_backtest.arena import compute_arena_result

        private = self._context("private-run", ["up", "up"])
        protected = self._context("safe-run", ["down", "down"], visibility="arena_safe")
        result = compute_arena_result(
            [private, protected],
            selected_metric_ids=["strategy_total_return"],
            labels_list=self.label_rows,
            arena_type="performance",
        )
        curves = result["performance_curves"]
        self.assertEqual(curves["status"], "available")
        self.assertTrue(curves["strict_comparable"])
        self.assertEqual(curves["privacy"]["arena_safe_run_ids"], ["safe-run"])
        self.assertEqual(curves["privacy"]["downsampled_run_ids"], ["safe-run"])
        self.assertEqual(curves["privacy"]["min_bucket_size"], 5)
        self.assertEqual(len(curves["series"]), 2)
        for item in curves["series"]:
            self.assertEqual(item["status"], "available")
            self.assertGreaterEqual(len(item["points"]), 2)
            self.assertTrue(item["aggregate_only"])
            for point in item["points"]:
                self.assertEqual(set(point), {"index", "net_value"})
        private_curve = next(item for item in curves["series"] if item["run_id"] == "private-run")
        safe_curve = next(item for item in curves["series"] if item["run_id"] == "safe-run")
        self.assertEqual(len(private_curve["points"]), 3)
        self.assertEqual([point["index"] for point in safe_curve["points"]], [0, 2])
        self.assertFalse(private_curve["privacy_aggregated"])
        self.assertTrue(safe_curve["privacy_aggregated"])
        serialized = json.dumps(curves["series"])
        self.assertNotIn("event_id", serialized)
        self.assertNotIn("timestamp", serialized)
        self.assertNotIn("period_return", serialized)

    def test_arena_safe_curve_uses_minimum_five_observation_buckets(self):
        from app.event_backtest.arena import _privacy_bucket_curve_points

        twelve_observations = [
            {"index": index, "net_value": 1 + index / 100}
            for index in range(13)
        ]
        reduced = _privacy_bucket_curve_points(twelve_observations)
        self.assertEqual([point["index"] for point in reduced], [0, 5, 12])
        self.assertGreaterEqual(reduced[1]["index"] - reduced[0]["index"], 5)
        self.assertGreaterEqual(reduced[2]["index"] - reduced[1]["index"], 5)

        four_observations = twelve_observations[:5]
        self.assertEqual(
            [point["index"] for point in _privacy_bucket_curve_points(four_observations)],
            [0, 4],
        )

    def test_protocol_mismatch_returns_explicit_unavailable_series(self):
        from app.event_backtest.arena import compute_arena_result
        from app.event_backtest.protocol import recompute_protocol_hash_for_run

        first = self._context("first", ["up", "up"])
        second = self._context("second", ["down", "down"])
        # A real cost difference changes the Arena comparison contract. Merely
        # replacing the full Run fingerprint does not change that contract;
        # forged full fingerprints are rejected separately by the API route.
        second.run_info["execution_spec"]["fee_bps"] = 3
        second.run_info["protocol_hash"] = recompute_protocol_hash_for_run(second.run_info)
        result = compute_arena_result(
            [first, second],
            selected_metric_ids=["strategy_total_return"],
            labels_list=self.label_rows,
            arena_type="performance",
        )
        curves = result["performance_curves"]
        self.assertEqual(curves["status"], "unavailable")
        self.assertEqual(curves["reason"], "protocol_mismatch")
        self.assertFalse(curves["strict_comparable"])
        self.assertEqual([item["points"] for item in curves["series"]], [[], []])
        self.assertEqual(
            [item["reason"] for item in curves["series"]],
            ["protocol_mismatch", "protocol_mismatch"],
        )

    def test_same_facts_and_costs_compare_across_distinct_valid_run_fingerprints(self):
        from app.event_backtest.arena import compute_arena_result
        from app.event_backtest.protocol import comparison_protocol_hash_for_run, recompute_protocol_hash_for_run

        first = self._context("original-catalogue", ["up", "up"])
        second = self._context("imported-catalogue", ["down", "down"])
        for ctx, version in ((first, "dsv-original"), (second, "dsv-imported")):
            ctx.run_info.update(dataset_version=version, engine_mode="event_proxy")
            ctx.run_info["protocol_hash"] = recompute_protocol_hash_for_run(ctx.run_info)
        self.assertNotEqual(first.run_info["protocol_hash"], second.run_info["protocol_hash"])
        self.assertEqual(
            comparison_protocol_hash_for_run(first.run_info),
            comparison_protocol_hash_for_run(second.run_info),
        )

        result = compute_arena_result(
            [first, second], selected_metric_ids=["strategy_total_return"],
            labels_list=self.label_rows, arena_type="performance",
        )
        curves = result["performance_curves"]
        self.assertEqual(curves["status"], "available")
        self.assertTrue(curves["strict_comparable"])
        self.assertTrue(all(item["points"] for item in curves["series"]))

    def test_explicit_mixed_mode_keeps_real_dated_curves_as_exploration(self):
        from app.event_backtest.arena import compute_arena_result

        first = self._context("mixed-first", ["up", "up"])
        second = self._context("mixed-second", ["down", "down"])
        first.run_info["comparison_protocol_hash"] = "comparison-a"
        second.run_info["comparison_protocol_hash"] = "comparison-b"
        first.run_info["_arena_allow_mixed_protocols"] = True
        second.run_info["_arena_allow_mixed_protocols"] = True
        result = compute_arena_result(
            [first, second],
            selected_metric_ids=["strategy_total_return"],
            labels_list=self.label_rows,
            arena_type="performance",
        )
        curves = result["performance_curves"]
        self.assertEqual(curves["status"], "available")
        self.assertEqual(curves["reason"], "mixed_protocols_exploration")
        self.assertFalse(curves["strict_comparable"])
        self.assertTrue(all(item["points"] for item in curves["series"]))
        self.assertTrue(all(
            any(point.get("timestamp") for point in item["points"])
            for item in curves["series"]
        ))
        self.assertIn("不得解释为公平排名", curves["disclaimer"])


if __name__ == "__main__":
    unittest.main()
