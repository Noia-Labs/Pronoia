"""Focused regression tests for formal Arena ranking and privacy contracts."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestArenaHardening(unittest.TestCase):
    @staticmethod
    def _metric(value: float | None, n: int, *, tier: str = "core") -> dict:
        return {
            "value": value,
            "meta": {"n": n},
            "breakdown": {"sensitive_slice": {"n": n}},
            "tier": tier,
        }

    def test_metric_families_unknown_and_empty_samples_are_rejected(self):
        from app.event_backtest.arena import (
            ArenaMetricValidationError,
            ArenaRunContext,
            resolve_metric_selection,
        )

        ctx = ArenaRunContext(
            run_id="run-one",
            run_info={"protocol_hash": "same"},
            metrics={
                "acc_t3_strict": self._metric(.7, 10),
                "strategy_total_return": {
                    "value": .1,
                    "meta": {"n_event_trades": 10},
                },
            },
        )
        with self.assertRaises(ArenaMetricValidationError) as wrong_family:
            resolve_metric_selection([ctx], ["strategy_total_return"], arena_type="forecast")
        self.assertIn("incompatible_metric_ids", wrong_family.exception.details)

        self.assertEqual(
            resolve_metric_selection(
                [ctx], ["acc_t3_strict", "strategy_total_return"], arena_type="mixed",
            ),
            ["acc_t3_strict", "strategy_total_return"],
        )
        with self.assertRaises(ArenaMetricValidationError) as unknown:
            resolve_metric_selection([ctx], ["does_not_exist"], arena_type="forecast")
        self.assertEqual(unknown.exception.details["unknown_metric_ids"], ["does_not_exist"])

        ctx.metrics["acc_t3_strict"] = self._metric(0.0, 0)
        with self.assertRaises(ArenaMetricValidationError) as empty:
            resolve_metric_selection([ctx], ["acc_t3_strict"], arena_type="forecast")
        self.assertEqual(empty.exception.details["invalid_metrics_by_run"], {"run-one": ["acc_t3_strict"]})

    def test_forecast_uses_common_event_exact_mcnemar_and_named_runs(self):
        from app.event_backtest.arena import ArenaRunContext, compute_arena_result

        labels = []
        pred_a, pred_b = {}, {}
        for index in range(6):
            event_id = f"e{index}"
            labels.append({"event_id": event_id, "label_t3": "up", "car_t3": .01})
            pred_a[event_id] = {"pred_direction": "up", "abstain": False}
            pred_b[event_id] = {
                "pred_direction": "down" if index < 4 else "up",
                "abstain": False,
            }
        a = ArenaRunContext(
            run_id="aaaaaaaa1111", run_info={"protocol_hash": "same", "name": "稳健策略"},
            metrics={"acc_t3_strict": self._metric(1.0, 6)}, predictions_by_eid=pred_a,
            tokens_in=100,
        )
        b = ArenaRunContext(
            run_id="bbbbbbbb2222", run_info={"protocol_hash": "same", "name": "快速策略"},
            metrics={"acc_t3_strict": self._metric(2 / 6, 6)}, predictions_by_eid=pred_b,
            tokens_in=50,
        )
        result = compute_arena_result(
            [a, b], selected_metric_ids=["acc_t3_strict"], labels_list=labels,
            arena_type="forecast",
        )
        evidence = result["pairwise_tests"][a.run_id][b.run_id]["per_metric"]["acc_t3_strict"]
        self.assertEqual(evidence["test_name"], "mcnemar_exact_binomial")
        self.assertEqual(evidence["test_meta"]["n_paired"], 6)
        self.assertEqual(evidence["test_meta"]["b_only_a_correct"], 4)
        self.assertNotIn("z_score", evidence["test_meta"])
        self.assertEqual(result["per_run"][a.run_id]["display_name"], "稳健策略 · aaaaaaaa")
        self.assertEqual(
            result["composite_score"]["metric_weights"]["acc_t3_strict"]["raw_weight"],
            2.0,
        )

    def test_performance_uses_paired_returns_and_unknown_cost_is_not_pareto(self):
        from app.event_backtest.arena import ArenaRunContext, compute_arena_result

        labels = [
            {"event_id": f"e{i}", "label_t3": "up", "car_t3": value}
            for i, value in enumerate((.02, -.01, .03))
        ]
        a = ArenaRunContext(
            run_id="run-a", run_info={"protocol_hash": "same", "name": "A"},
            metrics={
                "strategy_total_return": {"value": .04, "meta": {"n_event_trades": 3}},
                "strategy_max_drawdown": {"value": .02, "meta": {"n_event_trades": 3}},
            },
            predictions_by_eid={f"e{i}": {"pred_direction": "up"} for i in range(3)},
        )
        b = ArenaRunContext(
            run_id="run-b", run_info={"protocol_hash": "same", "name": "B"},
            metrics={
                "strategy_total_return": {"value": -.04, "meta": {"n_event_trades": 3}},
                "strategy_max_drawdown": {"value": .08, "meta": {"n_event_trades": 3}},
            },
            predictions_by_eid={f"e{i}": {"pred_direction": "down"} for i in range(3)},
        )
        result = compute_arena_result(
            [a, b], selected_metric_ids=["strategy_total_return", "strategy_max_drawdown"], labels_list=labels,
            arena_type="performance",
        )
        evidence = result["pairwise_tests"]["run-a"]["run-b"]["per_metric"]["strategy_total_return"]
        self.assertEqual(evidence["test_name"], "paired_permutation_bootstrap")
        self.assertEqual(evidence["test_meta"]["n_paired"], 3)
        self.assertNotIn(
            "strategy_max_drawdown",
            result["pairwise_tests"]["run-a"]["run-b"]["per_metric"],
        )
        self.assertEqual(result["per_run"]["run-a"]["cost_usd_estimate"], None)
        self.assertEqual(result["per_run"]["run-a"]["cost_source"], "unavailable")
        self.assertEqual(result["pareto_chart"]["points"], [])

    def test_arena_safe_suppresses_small_sample_detail_and_summary(self):
        from app.event_backtest.arena import ArenaRunContext, compute_arena_result

        metric = self._metric(1.0, 1)
        a = ArenaRunContext(
            run_id="safe", run_info={"protocol_hash": "same", "visibility": "arena_safe"},
            metrics={"acc_t3_strict": metric},
            predictions_by_eid={"e1": {"pred_direction": "up"}},
        )
        b = ArenaRunContext(
            run_id="open", run_info={"protocol_hash": "same"},
            metrics={"acc_t3_strict": self._metric(0.0, 1)},
            predictions_by_eid={"e1": {"pred_direction": "down"}},
        )
        result = compute_arena_result(
            [a, b], selected_metric_ids=["acc_t3_strict"],
            labels_list=[{"event_id": "e1", "label_t3": "up"}],
        )
        self.assertEqual(result["head_to_head"]["summary"], {})
        self.assertTrue(result["head_to_head"]["privacy"]["small_sample_summary_redacted"])
        self.assertNotIn("acc_t3_strict", result["per_run"]["safe"]["metrics"])
        self.assertNotIn(
            "acc_t3_strict",
            result["pairwise_tests"]["safe"]["open"]["per_metric"],
        )

    def test_arena_safe_small_metrics_and_identity_cannot_leak_through_derived_views(self):
        from app.event_backtest.arena import ArenaRunContext, compute_arena_result

        secret_accuracy = 0.987654321
        secret_return = 0.424242424
        safe = ArenaRunContext(
            run_id="safe-secret-1234",
            run_info={
                "protocol_hash": "same",
                "visibility": "arena_safe",
                "name": "SECRET STRATEGY NAME",
                "config": {
                    "source": "SECRET SOURCE",
                    "experiment_id": "SECRET EXPERIMENT",
                    "model_profile_snapshot": {
                        "id": "SECRET PROFILE ID",
                        "name": "SECRET PROFILE NAME",
                        "model": "SECRET PROFILE MODEL",
                    },
                },
            },
            runner="SECRET RUNNER",
            prompt_variant="SECRET PROMPT",
            model_version="SECRET MODEL",
            metrics={
                "acc_t3_strict": self._metric(secret_accuracy, 1),
                # The larger generic n must not override the one trade that
                # actually determines this performance metric.
                "strategy_total_return": {
                    "value": secret_return,
                    "meta": {"n": 100, "n_event_trades": 1},
                },
            },
            predictions_by_eid={"e0": {"pred_direction": "up", "abstain": False}},
            tokens_in=100,
        )
        public = ArenaRunContext(
            run_id="public-run-1234",
            run_info={"protocol_hash": "same", "name": "Public strategy"},
            runner="public-runner",
            prompt_variant="public-prompt",
            model_version="public-model",
            metrics={
                "acc_t3_strict": self._metric(0.5, 6),
                "strategy_total_return": {
                    "value": 0.1,
                    "meta": {"n": 6, "n_event_trades": 6},
                },
            },
            predictions_by_eid={
                f"e{index}": {"pred_direction": "down", "abstain": False}
                for index in range(6)
            },
            tokens_in=50,
        )
        labels = [
            {"event_id": f"e{index}", "label_t3": "up", "car_t3": 0.01}
            for index in range(6)
        ]

        result = compute_arena_result(
            [safe, public],
            selected_metric_ids=["acc_t3_strict", "strategy_total_return"],
            labels_list=labels,
            arena_type="mixed",
        )

        safe_result = result["per_run"][safe.run_id]
        self.assertEqual(safe_result["display_name"], "Arena Run · safe-sec")
        self.assertIsNone(safe_result["runner"])
        self.assertIsNone(safe_result["prompt_variant"])
        self.assertIsNone(safe_result["model_version"])
        self.assertNotIn("lineage", safe_result)
        self.assertEqual(safe_result["metrics"], {})

        for metric_id in ("acc_t3_strict", "strategy_total_return"):
            safe_rank = next(
                row for row in result["ranking"][metric_id]
                if row["run_id"] == safe.run_id
            )
            self.assertIsNone(safe_rank["value"])
            self.assertIsNone(safe_rank["rank"])
        safe_radar = next(
            row for row in result["radar_chart"]["series"]
            if row["run_id"] == safe.run_id
        )
        self.assertEqual(safe_radar["label"], "Arena Run · safe-sec")
        self.assertEqual(safe_radar["values"], [None, None])
        self.assertNotIn(
            "acc_t3_strict",
            result["pairwise_tests"][safe.run_id][public.run_id]["per_metric"],
        )
        self.assertNotIn(
            "strategy_total_return",
            result["pairwise_tests"][safe.run_id][public.run_id]["per_metric"],
        )

        safe_pareto = next(
            row for row in result["pareto_chart"]["points"]
            if row["run_id"] == safe.run_id
        )
        self.assertEqual(safe_pareto["label"], "Arena Run · safe-sec")
        self.assertIsNone(safe_pareto["runner"])
        self.assertIsNone(safe_pareto["prompt_variant"])
        self.assertIsNone(safe_pareto["model_version"])

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(secret_accuracy), serialized)
        self.assertNotIn(str(secret_return), serialized)
        for secret in (
            "SECRET STRATEGY NAME", "SECRET SOURCE", "SECRET EXPERIMENT",
            "SECRET PROFILE ID", "SECRET PROFILE NAME", "SECRET PROFILE MODEL",
            "SECRET RUNNER", "SECRET PROMPT", "SECRET MODEL",
        ):
            self.assertNotIn(secret, serialized)

    def test_arena_safe_effective_sample_uses_forecast_evaluated_count_and_unknown_fails_closed(self):
        from app.event_backtest.arena import ArenaRunContext, _public_arena_contexts

        safe = ArenaRunContext(
            run_id="safe",
            run_info={"visibility": "arena_safe"},
            metrics={
                "return_forecast": {
                    "value": 9.99,
                    "meta": {"n": 100, "evaluated_count": 1},
                },
                "calibration_mse": {
                    "value": 0.123,
                    "meta": {"unrelated_total": 100},
                },
            },
        )
        [public] = _public_arena_contexts([safe])
        self.assertEqual(public.metrics, {})

        portfolio = ArenaRunContext(
            run_id="safe-portfolio",
            run_info={"visibility": "arena_safe", "engine_mode": "portfolio"},
            metrics={
                "strategy_total_return": {
                    "value": 0.5,
                    "meta": {"n": 100, "bar_count": 101},
                },
                "portfolio_metrics": {
                    "value": 0.5,
                    "meta": {"n": 100, "active_period_count": 1},
                },
            },
        )
        [public_portfolio] = _public_arena_contexts([portfolio])
        self.assertNotIn("strategy_total_return", public_portfolio.metrics)

    def test_head_to_head_uses_strict_abstain_and_neutral_semantics(self):
        from app.event_backtest.arena import ArenaRunContext, compute_arena_result

        a = ArenaRunContext(
            run_id="a", run_info={"protocol_hash": "same"},
            metrics={"acc_t3_strict": self._metric(0.0, 2)},
            predictions_by_eid={
                "neutral": {"pred_direction": "neutral", "abstain": False},
                "abstain": {"pred_direction": "up", "abstain": True},
            },
        )
        b = ArenaRunContext(
            run_id="b", run_info={"protocol_hash": "same"},
            metrics={"acc_t3_strict": self._metric(0.5, 2)},
            predictions_by_eid={
                "neutral": {"pred_direction": "up", "abstain": False},
                "abstain": {"pred_direction": "up", "abstain": False},
            },
        )
        result = compute_arena_result(
            [a, b], selected_metric_ids=["acc_t3_strict"],
            labels_list=[
                {"event_id": "neutral", "label_t3": "neutral"},
                {"event_id": "abstain", "label_t3": "up"},
            ],
        )
        events = {row["event_id"]: row for row in result["head_to_head"]["event_level"]}
        self.assertFalse(events["neutral"]["per_run"]["a"]["correct"])
        self.assertFalse(events["neutral"]["per_run"]["b"]["correct"])
        self.assertEqual(events["neutral"]["best_runs"], [])
        self.assertFalse(events["abstain"]["per_run"]["a"]["correct"])
        self.assertTrue(events["abstain"]["per_run"]["b"]["correct"])
        summary = result["head_to_head"]["summary"]["a vs b"]
        self.assertEqual(summary["a_wins"], 0)
        self.assertEqual(summary["b_wins"], 1)
        self.assertEqual(summary["ties"], 1)

    def test_protocol_is_recomputed_and_saved_metric_override_is_rejected(self):
        from fastapi import HTTPException
        from app.event_backtest.protocol import build_protocol_hash
        from app.routes.arena import ComputeArenaRequest, _validate_protocols, compute_arena_and_save

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            events = root / "events.jsonl"
            labels = root / "labels.jsonl"
            events.write_text(json.dumps({"event_id": "e1", "title": "fact"}) + "\n")
            labels.write_text(json.dumps({"event_id": "e1", "label_t3": "up"}) + "\n")
            fingerprint = build_protocol_hash(events_path=events, labels_path=labels)
            run = {
                "id": "run-a", "events_path": str(events), "labels_path": str(labels),
                "protocol_hash": fingerprint, "config": {}, "execution_spec": {},
            }
            self.assertTrue(_validate_protocols([run])["strict_comparable"])
            events.write_text(json.dumps({"event_id": "e1", "title": "overwritten"}) + "\n")
            with self.assertRaises(HTTPException) as stale:
                _validate_protocols([run])
            self.assertEqual(stale.exception.status_code, 409)
            self.assertIn("run-a", stale.exception.detail["stale_protocols"])

        saved = {
            "id": "arena-a", "run_ids": ["a", "b"], "arena_type": "forecast",
            "config": {"selected_metric_ids": ["acc_t3_strict"]},
        }
        with patch("app.routes.arena.db.get_bt_arena", return_value=saved), \
             patch("app.routes.arena.db.update_bt_arena_config") as update_config:
            with self.assertRaises(HTTPException) as frozen:
                compute_arena_and_save(
                    "arena-a",
                    ComputeArenaRequest(selected_metric_ids=["calibration_mse"]),
                )
        self.assertEqual(frozen.exception.status_code, 409)
        update_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
