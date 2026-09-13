"""Regression tests for strategy-independent Arena fairness signatures."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestArenaComparisonProtocol(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.events = self.root / "events.jsonl"
        self.labels = self.root / "labels.jsonl"
        self.events.write_text(
            json.dumps({
                "event_id": "e1", "market": "CN", "symbol": "600000",
                "event_time": "2025-01-10T09:00:00+08:00", "title": "同一事件",
                "benchmark": "sh000300",
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.labels.write_text(
            json.dumps({"event_id": "e1", "label_t3": "up", "car_t3": 0.03}) + "\n",
            encoding="utf-8",
        )
        from app.event_backtest.protocol import events_snapshot_sha256

        self.contract = {
            "id": "dsv_same",
            "dataset_version": "dsv_same",
            "snapshot_hash": events_snapshot_sha256(self.events),
            "frequency": None,
            "coverage": {"start_at": "2025-01-10", "end_at": "2025-01-10"},
        }

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _event_run(self, run_id: str, **overrides):
        run = {
            "id": run_id,
            "events_path": str(self.events),
            "labels_path": str(self.labels),
            "dataset_version": "dsv_same",
            "engine_mode": "event_proxy",
            "result_nature": "proxy",
            "strategy_spec": {"type": "event", "runner": "baseline"},
            "runner": "baseline",
            "model_version": "model-a",
            "prompt_variant": "prompt-a",
            "visibility": "private",
            "execution_spec": {},
            "config": {
                "evaluation_protocol": {
                    "evaluation_horizon": "t3",
                    "data_basis": {
                        "dataset_id": "display-id-a",
                        "dataset_name": "展示名称 A",
                        "status": "available",
                        "source_mode": "local_file",
                    },
                },
                "dataset_snapshot": self.contract,
            },
        }
        run.update(overrides)
        return run

    def test_strategy_and_catalog_metadata_do_not_split_formal_group(self):
        from app.event_backtest.protocol import comparison_protocol_hash_for_run

        first = self._event_run("a")
        second = self._event_run(
            "b",
            strategy_spec={"type": "event", "runner": "team_full", "private_rules": ["secret"]},
            runner="team_full",
            model_version="another-model",
            prompt_variant="another-prompt",
            visibility="arena_safe",
            config={
                "evaluation_protocol": {
                    "evaluation_horizon": "t3",
                    "data_basis": {
                        "dataset_id": "clone-id-b",
                        "dataset_name": "克隆展示名称 B",
                        "status": "verified",
                        "source_mode": "api_import",
                    },
                },
                "dataset_snapshot": self.contract,
            },
        )
        self.assertEqual(
            comparison_protocol_hash_for_run(first, dataset_contract=self.contract),
            comparison_protocol_hash_for_run(second, dataset_contract=self.contract),
        )

    def test_effective_window_defaults_and_numeric_aliases_are_canonical(self):
        from app.event_backtest.protocol import comparison_protocol_hash_for_run

        implicit = self._event_run("implicit")
        explicit = self._event_run(
            "explicit",
            execution_spec={
                "start_date": "2025/01/10", "end_date": "2025-01-10T23:59:59+08:00",
                "fee_bps": "0", "slippage_bps": 0.0,
            },
        )
        self.assertEqual(
            comparison_protocol_hash_for_run(implicit, dataset_contract=self.contract),
            comparison_protocol_hash_for_run(explicit, dataset_contract=self.contract),
        )

    def test_fairness_inputs_and_result_nature_change_signature(self):
        from app.event_backtest.protocol import comparison_protocol_hash_for_run

        base = self._event_run("base")
        baseline = comparison_protocol_hash_for_run(base, dataset_contract=self.contract)
        changes = [
            {"execution_spec": {"fee_bps": 1}},
            {"execution_spec": {"start_date": "2025-01-11"}},
            {"result_nature": "unavailable"},
            {"config": {**base["config"], "evaluation_horizon": "t5", "evaluation_protocol": {"evaluation_horizon": "t5"}}},
        ]
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                changed = self._event_run(f"changed-{index}", **change)
                self.assertNotEqual(
                    baseline,
                    comparison_protocol_hash_for_run(changed, dataset_contract=self.contract),
                )

    def test_performance_alignment_externalizes_only_window_and_frequency(self):
        from fastapi import HTTPException
        from app.event_backtest.protocol import build_protocol_hash
        from app.routes.arena import _validate_protocols

        first = self._event_run(
            "aligned-a",
            execution_spec={"start_date": "2025-01-01", "end_date": "2025-01-31"},
        )
        second_contract = {**self.contract, "frequency": "1w"}
        second = self._event_run(
            "aligned-b",
            execution_spec={"start_date": "2025-01-10", "end_date": "2025-02-28"},
            config={
                "evaluation_protocol": {"evaluation_horizon": "t3"},
                "dataset_snapshot": second_contract,
            },
        )
        for run in (first, second):
            run["protocol_hash"] = build_protocol_hash(
                events_path=run["events_path"],
                labels_path=run["labels_path"],
                execution_spec=run["execution_spec"],
                evaluation_protocol=run["config"]["evaluation_protocol"],
                dataset_version=run["dataset_version"],
                engine_mode=run["engine_mode"],
            )

        # Without an explicit performance alignment, the historical strict
        # contract still treats source windows/frequencies as different.
        with self.assertRaises(HTTPException):
            _validate_protocols([first, second], arena_type="performance")

        aligned = _validate_protocols(
            [first, second],
            arena_type="performance",
            time_alignment={"mode": "intersection", "frequency": "week"},
        )
        self.assertTrue(aligned["strict_comparable"])
        self.assertTrue(aligned["time_axis_externalized"])
        self.assertNotEqual(
            aligned["source_time_axes"]["aligned-a"],
            aligned["source_time_axes"]["aligned-b"],
        )

        unfair_cost = {**second, "execution_spec": {**second["execution_spec"], "fee_bps": 2}}
        unfair_cost["protocol_hash"] = build_protocol_hash(
            events_path=unfair_cost["events_path"],
            labels_path=unfair_cost["labels_path"],
            execution_spec=unfair_cost["execution_spec"],
            evaluation_protocol=unfair_cost["config"]["evaluation_protocol"],
            dataset_version=unfair_cost["dataset_version"],
            engine_mode=unfair_cost["engine_mode"],
        )
        with self.assertRaises(HTTPException) as rejected:
            _validate_protocols(
                [first, unfair_cost],
                arena_type="performance",
                time_alignment={"mode": "intersection", "frequency": "week"},
            )
        self.assertIn("costs.fee_bps", rejected.exception.detail["comparison_protocol"]["mismatched_fields"])

    def test_portfolio_position_constraints_are_part_of_fairness_contract(self):
        from app.event_backtest.protocol import comparison_protocol_hash_for_run

        market = self.root / "market.csv"
        market.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2025-01-02,100,102,99,101,1000\n"
            "2025-01-03,101,103,100,102,1100\n",
            encoding="utf-8",
        )
        base = {
            "id": "portfolio-a", "events_path": str(market),
            "dataset_version": "dsv_market", "engine_mode": "portfolio",
            "result_nature": "simulated_from_real_bars",
            "strategy_spec": {"type": "quant", "kind": "buy_hold"},
            "execution_spec": {"applied": {"max_abs_weight": 1.0, "allow_short": True}},
            "config": {},
        }
        contract = {
            "dataset_version": "dsv_market", "snapshot_hash": "market-snapshot",
            "frequency": "1d", "coverage": {"start_at": "2025-01-02", "end_at": "2025-01-03"},
        }
        baseline = comparison_protocol_hash_for_run(base, dataset_contract=contract)
        different_short = {
            **base, "id": "portfolio-b",
            "strategy_spec": {"type": "quant", "kind": "another_strategy"},
            "execution_spec": {"applied": {"max_abs_weight": 1.0, "allow_short": False}},
        }
        different_limit = {
            **base, "id": "portfolio-c",
            "execution_spec": {"applied": {"max_abs_weight": 0.5, "allow_short": True}},
        }
        self.assertNotEqual(baseline, comparison_protocol_hash_for_run(different_short, dataset_contract=contract))
        self.assertNotEqual(baseline, comparison_protocol_hash_for_run(different_limit, dataset_contract=contract))

    def test_route_uses_comparison_signature_but_keeps_integrity_validation(self):
        from app.event_backtest.protocol import build_protocol_hash
        from app.routes.arena import _validate_protocols

        first = self._event_run("formal-a")
        second = self._event_run(
            "formal-b",
            strategy_spec={"type": "event", "runner": "team_full", "rules": [1, 2, 3]},
            runner="team_full",
            config={
                "evaluation_protocol": {
                    "evaluation_horizon": "t3",
                    "data_basis": {"dataset_id": "another-catalog-id", "status": "verified"},
                },
                "dataset_snapshot": self.contract,
            },
        )
        for run in (first, second):
            run["protocol_hash"] = build_protocol_hash(
                events_path=run["events_path"],
                labels_path=run["labels_path"],
                execution_spec=run["execution_spec"],
                evaluation_protocol=run["config"]["evaluation_protocol"],
                dataset_version=run["dataset_version"],
                engine_mode=run["engine_mode"],
            )
        result = _validate_protocols([first, second])
        self.assertTrue(result["strict_comparable"])
        self.assertEqual(len(set(result["run_integrity_hashes"].values())), 2)
        self.assertEqual(len(set(result["run_comparison_protocol_hashes"].values())), 1)

        second["result_nature"] = "unavailable"
        exploratory = _validate_protocols([first, second], allow_mixed=True)
        self.assertFalse(exploratory["strict_comparable"])
        self.assertEqual(exploratory["comparison_mode"], "exploration")
        self.assertIn("result_nature", exploratory["mismatched_fields"])

    def test_unverified_external_portfolio_is_exploration_only(self):
        from fastapi import HTTPException
        from app.routes.arena import _validate_scored_runs

        result = self.root / "unverified-result.json"
        result.write_text("{}", encoding="utf-8")
        run = {
            "id": "external-unverified",
            "status": "done",
            "engine_mode": "portfolio",
            "result_nature": "unverified",
            "result_path": str(result),
            "strategy_type": "api",
            "strategy_spec": {
                "type": "api", "adapter": "external_http",
                "point_in_time_enforced": False, "result_nature": "unverified",
            },
        }
        with self.assertRaises(HTTPException) as rejected:
            _validate_scored_runs([run])
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(
            rejected.exception.detail["unverified_run_ids"],
            ["external-unverified"],
        )
        _validate_scored_runs([run], allow_mixed=True)

    def test_model_lab_dry_run_is_never_an_arena_candidate(self):
        from fastapi import HTTPException
        from app.routes.arena import _validate_scored_runs

        result = self.root / "model-lab-demo-result.json"
        result.write_text("{}", encoding="utf-8")
        run = {
            "id": "model-lab-dry-run",
            "status": "done",
            "engine_mode": "portfolio",
            "result_nature": "simulated_from_real_bars",
            "result_path": str(result),
            "config": {
                "source": "model_lab",
                "model_lab": {"batch_id": "batch-demo", "dry_run": True},
            },
        }
        for allow_mixed in (False, True):
            with self.subTest(allow_mixed=allow_mixed), self.assertRaises(HTTPException) as rejected:
                _validate_scored_runs([run], allow_mixed=allow_mixed)
            self.assertEqual(rejected.exception.status_code, 409)
            self.assertEqual(rejected.exception.detail["reason"], "model_lab_demo_result")

    def test_all_three_arena_routes_forward_the_frozen_comparison_mode(self):
        from app.routes.arena import (
            ComputeArenaRequest,
            CreateArenaRequest,
            compute_arena_and_save,
            compute_arena_inline,
            create_arena,
        )

        run_a = {"id": "a", "dataset_id": "dataset", "dataset_name": "数据", "status": "done"}
        run_b = {"id": "b", "dataset_id": "dataset", "dataset_name": "数据", "status": "done"}
        runs = {"a": run_a, "b": run_b}
        protocol = {
            "strict_comparable": False, "comparison_mode": "exploration",
            "protocol_hash": None,
        }
        checked_modes: list[bool] = []

        def record_mode(_runs, *, allow_mixed=False):
            checked_modes.append(bool(allow_mixed))

        common = [
            patch("app.routes.arena.db.get_bt_run", side_effect=lambda run_id: runs.get(run_id)),
            patch("app.routes.arena._validate_scored_runs", side_effect=record_mode),
            patch("app.routes.arena._validate_protocols", return_value=protocol),
            patch("app.routes.arena.arena_engine.build_run_contexts", return_value=[]),
            patch("app.routes.arena.arena_engine.resolve_metric_selection", return_value=[]),
        ]
        with common[0], common[1], common[2], common[3], common[4], patch(
            "app.routes.arena.db.get_bt_dataset", return_value={"name": "数据"}
        ), patch(
            "app.routes.arena.db.create_bt_arena", return_value={"id": "created"}
        ):
            created = create_arena(CreateArenaRequest(
                name="探索", run_ids=["a", "b"], arena_type="performance",
                allow_mixed_protocols=True,
            ))
        self.assertEqual(created["id"], "created")

        with patch("app.routes.arena.db.get_bt_run", side_effect=lambda run_id: runs.get(run_id)), patch(
            "app.routes.arena._validate_scored_runs", side_effect=record_mode
        ), patch("app.routes.arena._validate_protocols", return_value=protocol), patch(
            "app.routes.arena.arena_engine.build_run_contexts", return_value=[]
        ), patch("app.routes.arena.arena_engine.compute_arena_result", return_value={}):
            inline = compute_arena_inline(ComputeArenaRequest(
                run_ids=["a", "b"], arena_type="performance", allow_mixed_protocols=True,
            ))
        self.assertEqual(inline["comparison_protocol"], protocol)

        saved = {
            "id": "saved", "run_ids": ["a", "b"], "arena_type": "performance",
            "config": {
                "selected_metric_ids": [], "arena_type": "performance",
                "allow_mixed_protocols": True,
            },
        }
        with patch("app.routes.arena.db.get_bt_arena", side_effect=[saved, {"id": "saved", "status": "done"}]), patch(
            "app.routes.arena.db.get_bt_run", side_effect=lambda run_id: runs.get(run_id)
        ), patch("app.routes.arena._validate_scored_runs", side_effect=record_mode), patch(
            "app.routes.arena._validate_protocols", return_value=protocol
        ), patch("app.routes.arena.arena_engine.build_run_contexts", return_value=[]), patch(
            "app.routes.arena.arena_engine.resolve_metric_selection", return_value=[]
        ), patch("app.routes.arena.arena_engine.compute_arena_result", return_value={}), patch(
            "app.routes.arena.db.update_bt_arena_status"
        ):
            saved_result = compute_arena_and_save("saved")
        self.assertEqual(saved_result["status"], "done")
        self.assertEqual(checked_modes, [True, True, True])

    def test_saved_arena_recompute_rejects_overwritten_result_artifact(self):
        from fastapi import HTTPException
        from app.routes.arena import (
            _source_artifact_fingerprints,
            compute_arena_and_save,
        )

        result_path = self.root / "frozen-result.json"
        result_path.write_text('{"equity_curve": [1]}', encoding="utf-8")
        runs = {
            run_id: {
                "id": run_id,
                "status": "done",
                "engine_mode": "portfolio",
                "result_nature": "simulated_from_real_bars",
                "result_path": str(result_path),
            }
            for run_id in ("artifact-a", "artifact-b")
        }
        frozen = _source_artifact_fingerprints(list(runs.values()))
        saved = {
            "id": "saved-artifacts",
            "status": "done",
            "result": {"ranking": {"strategy_total_return": [{"run_id": "artifact-a", "rank": 1}]}},
            "run_ids": ["artifact-a", "artifact-b"],
            "arena_type": "performance",
            "config": {
                "selected_metric_ids": ["strategy_total_return"],
                "arena_type": "performance",
                "allow_mixed_protocols": False,
                "source_artifact_fingerprints": frozen,
            },
        }
        result_path.write_text('{"equity_curve": [999]}', encoding="utf-8")

        with patch("app.routes.arena.db.get_bt_arena", return_value=saved), patch(
            "app.routes.arena.db.get_bt_run", side_effect=lambda run_id: runs[run_id]
        ), patch("app.routes.arena._validate_scored_runs"), patch(
            "app.routes.arena._validate_protocols", return_value={"strict_comparable": True}
        ), patch("app.routes.arena.db.update_bt_arena_status") as update_status, patch(
            "app.routes.arena.arena_engine.build_run_contexts"
        ) as build_contexts:
            with self.assertRaises(HTTPException) as rejected:
                compute_arena_and_save("saved-artifacts")
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(rejected.exception.detail["reason"], "source_artifact_fingerprint_mismatch")
        self.assertEqual(set(rejected.exception.detail["changed_run_artifacts"]), {"artifact-a", "artifact-b"})
        build_contexts.assert_not_called()
        update_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
