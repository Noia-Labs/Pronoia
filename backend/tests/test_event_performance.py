"""Regression coverage for selected-horizon event performance data."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestEventPerformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temp.name)
        cls._old_env_db = os.environ.get("FEVER_DB_PATH")
        cls._old_env_data = os.environ.get("FEVER_DATA_DIR")
        os.environ["FEVER_DB_PATH"] = str(cls.root / "performance.db")
        os.environ["FEVER_DATA_DIR"] = str(cls.root / "data")
        from app import config, db
        from app.routes import backtest

        cls._old_config_db = config.DB_PATH
        cls._old_config_data = config.DATA_DIR
        config.DB_PATH = os.environ["FEVER_DB_PATH"]
        config.DATA_DIR = os.environ["FEVER_DATA_DIR"]
        backtest.DATA_DIR = os.environ["FEVER_DATA_DIR"]
        db._conn = None
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        from app import config, db
        from app.routes import backtest

        if db._conn is not None:
            db._conn.close()
            db._conn = None
        config.DB_PATH = cls._old_config_db
        config.DATA_DIR = cls._old_config_data
        backtest.DATA_DIR = cls._old_config_data
        if cls._old_env_db is None:
            os.environ.pop("FEVER_DB_PATH", None)
        else:
            os.environ["FEVER_DB_PATH"] = cls._old_env_db
        if cls._old_env_data is None:
            os.environ.pop("FEVER_DATA_DIR", None)
        else:
            os.environ["FEVER_DATA_DIR"] = cls._old_env_data
        cls._temp.cleanup()

    def _write_jsonl(self, name: str, rows: list[dict]) -> Path:
        path = self.root / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def _create_run(self, *, visibility: str = "private", oracle_epsilon: float | None = None) -> dict:
        from app import db
        from app.event_backtest.protocol import build_protocol_hash

        events = self._write_jsonl("events.jsonl", [
            {
                "event_id": "jan", "market": "CN", "symbol": "600000",
                "event_time": "2025-01-10T09:05:00+08:00",
                "occurred_at": "2025-01-10T09:00:00+08:00",
                "available_time": "2025-01-10T09:05:00+08:00",
                "event_type_l2": "公告", "title": "一月事件", "event_text": "x",
                "source_url": "manual://jan", "analysis_direction": "up",
                "analysis_confidence": .8, "analysis_rationale": "现金流改善",
                "analysis_horizon": "t5",
            },
            {
                "event_id": "feb", "market": "CN", "symbol": "600001",
                "event_time": "2025-02-10T09:05:00+08:00",
                "event_type_l2": "公告", "title": "二月事件", "event_text": "y",
                "source_url": "manual://feb", "analysis_direction": "down",
                "analysis_confidence": .7, "analysis_rationale": "盈利恶化",
                "analysis_horizon": "t5",
            },
        ])
        labels = self._write_jsonl("labels.jsonl", [
            {
                "event_id": "jan", "market": "CN", "symbol": "600000",
                "event_time": "2025-01-10", "label_t1": "down", "label_t3": "down",
                "label_t5": "up", "car_t1": -.01, "car_t3": -.02, "car_t5": .10,
                "ret_t5": .12, "bm_ret_t5": .02,
            },
            {
                "event_id": "feb", "market": "CN", "symbol": "600001",
                "event_time": "2025-02-10", "label_t1": "up", "label_t3": "up",
                "label_t5": "down", "car_t1": .01, "car_t3": .02, "car_t5": -.05,
                "ret_t5": -.04, "bm_ret_t5": .01,
            },
        ])
        predictions = self._write_jsonl("predictions.jsonl", [
            {"event_id": "jan", "pred_direction": "up", "run_id": "r", "confidence": .8, "horizon": "t5"},
            {"event_id": "feb", "pred_direction": "down", "run_id": "r", "confidence": .7, "horizon": "t5"},
        ])
        execution = {
            "initial_capital": 100_000,
            "fee_bps": 10,
            "slippage_bps": 5,
            "start_date": "2025-01-01",
            "end_date": "2025-01-31",
            "execution_delay": "next_open",
        }
        evaluation = {"evaluation_horizon": "t5"}
        if oracle_epsilon is not None:
            evaluation["oracle_epsilon"] = oracle_epsilon
        run = db.create_bt_run(
            name=f"performance-{visibility}",
            runner="provided_analysis",
            events_path=str(events),
            labels_path=str(labels),
            out_path=str(predictions),
            total_events=1,
            config={"evaluation_horizon": "t5", "evaluation_protocol": evaluation},
            execution_spec=execution,
            visibility=visibility,
            protocol_hash=build_protocol_hash(
                events_path=events,
                labels_path=labels,
                execution_spec=execution,
                evaluation_protocol=evaluation,
            ),
        )
        db.update_bt_run_status(run["id"], "done")
        return db.get_bt_run(run["id"])

    def test_missing_base_car_stays_missing_and_zero_stays_zero(self):
        from app.event_backtest.models import EventLabel

        missing = EventLabel.from_dict({
            "event_id": "missing", "label_t1": "", "label_t3": "", "label_t5": "",
        })
        self.assertIsNone(missing.car_t1)
        self.assertIsNone(missing.car_t3)
        self.assertIsNone(missing.car_t5)
        actual_zero = EventLabel.from_dict({
            "event_id": "zero", "label_t1": "neutral", "label_t3": "neutral",
            "label_t5": "neutral", "car_t1": 0, "car_t3": 0.0, "car_t5": "0",
        })
        self.assertEqual(actual_zero.car_t3, 0.0)

    def test_frozen_zero_threshold_reaches_metrics_api_and_arena_without_changing_labels(self):
        from app.main import app
        from app.event_backtest import arena, metrics_registry
        from fastapi.testclient import TestClient

        run = self._create_run(oracle_epsilon=0)
        labels_path = Path(run["labels_path"])
        original_labels = labels_path.read_bytes()
        with patch.object(metrics_registry, "compute_all_metrics", wraps=metrics_registry.compute_all_metrics) as compute:
            response = TestClient(app).get(f"/api/bt/runs/{run['id']}/metrics")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["epsilon"], 0)
            self.assertEqual(compute.call_args.kwargs["epsilon"], 0)
            arena._compute_run_metrics(run)
            self.assertEqual(compute.call_args.kwargs["epsilon"], 0)
        self.assertEqual(labels_path.read_bytes(), original_labels)

    def test_flat_zero_threshold_oracle_is_covered_but_not_direction_scored(self):
        from app import db
        from app.main import app
        from fastapi.testclient import TestClient

        run = self._create_run(oracle_epsilon=0)
        # A completed legacy scalar must not override a real zero denominator.
        db.update_bt_run_progress(run["id"], done_events=1, acc_t3_non_neutral=1.0)
        labels_path = Path(run["labels_path"])
        rows = [json.loads(line) for line in labels_path.read_text().splitlines() if line]
        rows[0]["car_t5"] = 0
        rows[0]["label_t5"] = ""
        labels_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        response = TestClient(app).get(f"/api/bt/runs/{run['id']}/metrics")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["oracle_coverage"]["coverage_rate"], 1.0)
        self.assertEqual(payload["oracle_coverage"]["missing_labels"], 0)
        self.assertEqual(payload["acc_t3_non_neutral"]["n"], 0)

    def test_selected_horizon_cost_and_date_window_reach_metrics_and_curves(self):
        from app.main import app
        from fastapi.testclient import TestClient

        run = self._create_run()
        client = TestClient(app)
        run_payload = client.get(f"/api/bt/runs/{run['id']}").json()
        self.assertEqual(run_payload["evaluation_horizon"], "t5")

        metrics_response = client.get(f"/api/bt/runs/{run['id']}/metrics")
        self.assertEqual(metrics_response.status_code, 200, metrics_response.text)
        metrics = metrics_response.json()
        self.assertEqual(metrics["primary_oracle_horizon"], "t5")
        self.assertEqual(metrics["epsilon"], .005)
        self.assertEqual(metrics["event_window"]["after_count"], 1)
        self.assertEqual(metrics["metrics"]["acc_primary_non_neutral"]["value"], 1.0)
        self.assertAlmostEqual(
            metrics["metrics"]["strategy_total_return"]["value"],
            .10 - .0015,
        )

        response = client.get(f"/api/bt/runs/{run['id']}/performance")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["mode"], "event_proxy")
        self.assertEqual(result["primary_horizon"], "t5")
        self.assertEqual(result["summary"]["n_events_in_protocol"], 1)
        self.assertEqual(result["summary"]["n_trades"], 1)
        self.assertAlmostEqual(result["summary"]["total_return"], .0985)
        self.assertAlmostEqual(result["summary"]["final_value"], 109_850.0)
        self.assertAlmostEqual(result["summary"]["asset_total_return"], .12)
        self.assertAlmostEqual(result["summary"]["benchmark_total_return"], .02)
        self.assertEqual(result["trades"][0]["event_id"], "jan")
        self.assertEqual(result["effective_protocol"]["applied"]["event_window"]["after_count"], 1)
        self.assertIn("execution_delay", result["effective_protocol"]["recorded_not_applied"])
        self.assertEqual(len(result["asset_curve"]), 2)
        self.assertEqual(len(result["benchmark_curve"]), 2)

    def test_orchestrator_persists_selected_horizon_in_legacy_oracle_slots(self):
        from app import db
        from app.event_backtest import orchestrator
        from app.main import app
        from fastapi.testclient import TestClient

        events = self._write_jsonl("orchestrator-events.jsonl", [{
            "event_id": "selected-window", "market": "CN", "symbol": "600000",
            "event_time": "2025-01-10T09:05:00+08:00",
            "event_type_l2": "公告", "title": "主窗口测试", "event_text": "x",
            "source_url": "manual://selected-window", "analysis_direction": "up",
            "analysis_confidence": .8, "analysis_rationale": "主窗口应使用 t5",
            "analysis_horizon": "t5",
        }])
        labels = self._write_jsonl("orchestrator-labels.jsonl", [{
            "event_id": "selected-window",
            "label_t1": "down", "label_t3": "down", "label_t5": "up",
            "car_t1": -.01, "car_t3": -.02, "car_t5": .08,
        }])
        out_path = self.root / "orchestrator-predictions.jsonl"
        run = db.create_bt_run(
            name="selected-horizon-orchestrator",
            runner="provided_analysis",
            events_path=str(events),
            labels_path=str(labels),
            out_path=str(out_path),
            total_events=1,
            config={"evaluation_horizon": "t5"},
        )

        orchestrator._do_run(run["id"], 1)

        stored = db.get_bt_prediction(run["id"], "selected-window")
        self.assertEqual(stored["oracle_label_t3"], "up")
        self.assertAlmostEqual(stored["oracle_car_t3"], .08)
        self.assertTrue(stored["is_correct_t3"])
        response = TestClient(app).get(f"/api/bt/runs/{run['id']}/events").json()
        self.assertEqual(response["primary_oracle_horizon"], "t5")
        self.assertEqual(response["items"][0]["oracle_horizon"], "t5")

    def test_arena_safe_performance_redacts_event_level_data(self):
        from app.main import app
        from fastapi.testclient import TestClient

        run = self._create_run(visibility="arena_safe")
        response = TestClient(app).get(
            f"/api/bt/runs/{run['id']}/performance?include_kline=true"
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["trades"], [])
        self.assertEqual(payload["event_markers"], [])
        self.assertEqual(payload["kline_refs"], [])
        self.assertTrue(payload["privacy"]["event_level_redacted"])
        self.assertEqual(payload["privacy"]["min_bucket"], 5)
        self.assertLessEqual(len(payload["equity_curve"]), 2)
        for curve_name in (
            "equity_curve", "gross_equity_curve", "drawdown_curve",
            "asset_curve", "benchmark_curve",
        ):
            for point in payload[curve_name]:
                self.assertTrue(set(point).issubset({"index", "net_value", "drawdown"}))
                self.assertNotIn("period_return", point)
                self.assertNotIn("equity", point)
                self.assertNotIn("event_id", point)
                self.assertNotIn("timestamp", point)

    def test_arena_safe_curve_checkpoints_use_five_event_buckets(self):
        from app.routes.backtest import _sanitize_arena_safe_curve

        raw = [
            {
                "index": index, "net_value": 1 + index / 100,
                "drawdown": -index / 1000, "period_return": .01,
                "equity": 100_000 + index, "event_id": f"secret-{index}",
                "timestamp": f"2025-01-{index + 1:02d}",
            }
            for index in range(13)
        ]
        safe = _sanitize_arena_safe_curve(raw)
        self.assertEqual([point["index"] for point in safe], [0, 5, 10, 12])
        self.assertTrue(all(set(point) == {"index", "net_value", "drawdown"} for point in safe))

    def test_event_close_is_reported_as_applied_oracle_anchor(self):
        from app.event_backtest.performance import build_event_proxy_performance

        run = self._create_run()
        run["execution_spec"] = {
            **(run.get("execution_spec") or {}),
            "execution_delay": "event_close",
        }
        payload = build_event_proxy_performance(run)
        effective = payload["effective_protocol"]
        self.assertEqual(
            effective["applied"]["execution_delay"]["effective"],
            "information_close_window",
        )
        self.assertNotIn("execution_delay", effective["recorded_not_applied"])

    def test_t20_is_rejected_and_oracle_snapshots_are_content_addressed(self):
        from app import db
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        dataset_response = client.post("/api/bt/datasets/manual", json={
            "name": "oracle immutable",
            "events": [{
                "market": "CN", "symbol": "600000",
                "event_time": "2025-01-10T09:00:00+08:00", "title": "x",
                "analysis_direction": "up", "confidence": .8,
                "rationale": "真实测试", "horizon": "t5",
            }],
        })
        self.assertEqual(dataset_response.status_code, 200, dataset_response.text)
        dataset = dataset_response.json()
        self.assertTrue(dataset["decision_ready"])

        rejected = client.post("/api/bt/runs", json={
            "name": "bad horizon", "runner": "provided_analysis",
            "dataset_id": dataset["id"], "horizon": "t20",
        })
        self.assertEqual(rejected.status_code, 422, rejected.text)

        state = {"car": .03}

        def fake_write(events, cars, out_path, epsilon=.005):
            state["epsilon"] = epsilon
            car = state["car"]
            row = {
                "event_id": events[0].event_id,
                "label_t1": "up", "label_t3": "up", "label_t5": "up",
                "car_t1": car, "car_t3": car, "car_t5": car,
            }
            Path(out_path).write_text(json.dumps(row) + "\n", encoding="utf-8")
            return [row]

        with patch("app.event_backtest.labeller._compute_cars_for_events", return_value={}) as compute_cars, \
             patch("app.event_backtest.labeller.write_labels", side_effect=fake_write):
            first = client.post(
                f"/api/bt/datasets/{dataset['id']}/oracle?required_horizon=t5"
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(compute_cars.call_args.kwargs["car_method"], "benchmark_relative_return")
            self.assertEqual(state["epsilon"], 0)
            first_path = Path(first.json()["labels_path"])
            state["car"] = .04
            second = client.post(
                f"/api/bt/datasets/{dataset['id']}/oracle?required_horizon=t5&car_method=market_model&epsilon=0.005"
            )
            self.assertEqual(compute_cars.call_args.kwargs["car_method"], "market_model")
            self.assertEqual(state["epsilon"], .005)
        self.assertEqual(second.status_code, 200, second.text)
        second_path = Path(second.json()["labels_path"])
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertEqual(db.get_bt_dataset(dataset["id"])["labels_path"], str(second_path))

    def test_zero_oracle_return_is_covered_without_inventing_direction(self):
        from app.main import app
        from fastapi.testclient import TestClient
        from app.event_backtest import labeller

        client = TestClient(app)
        response = client.post("/api/bt/datasets/manual", json={
            "name": "flat oracle",
            "events": [{"market": "CN", "symbol": "600000",
                        "event_time": "2025-01-10T09:00:00+08:00", "title": "flat test"}],
        })
        self.assertEqual(response.status_code, 200, response.text)
        dataset = response.json()

        def flat_cars(events, **kwargs):
            return {(e.event_id, e.market, e.symbol): {
                "car_method": "benchmark_relative_return", "t3": .01,
                "bm_t3": .01, "car_t3": 0,
            } for e in events}

        with patch.object(labeller, "_compute_cars_for_events", side_effect=flat_cars):
            generated = client.post(f"/api/bt/datasets/{dataset['id']}/oracle")
        self.assertEqual(generated.status_code, 200, generated.text)
        row = json.loads(Path(generated.json()["labels_path"]).read_text())
        self.assertEqual(row["epsilon"], 0)
        self.assertEqual(row["car_t3"], 0)
        self.assertEqual(row["label_t3"], "")

    def test_oracle_rejects_incomplete_required_horizon_coverage(self):
        from app import db
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        created = client.post("/api/bt/datasets/manual", json={
            "name": "oracle coverage gate",
            "events": [
                {
                    "market": "CN", "symbol": "600000",
                    "event_time": "2025-01-10T09:00:00+08:00", "title": "a",
                    "analysis_direction": "up", "confidence": .8,
                    "rationale": "事件一", "horizon": "t5",
                },
                {
                    "market": "CN", "symbol": "600001",
                    "event_time": "2025-01-11T09:00:00+08:00", "title": "b",
                    "analysis_direction": "down", "confidence": .7,
                    "rationale": "事件二", "horizon": "t5",
                },
            ],
        })
        self.assertEqual(created.status_code, 200, created.text)
        dataset = created.json()

        def incomplete_write(events, cars, out_path, epsilon=.005):
            rows = [
                {
                    "event_id": events[0].event_id,
                    "label_t1": "up", "label_t3": "up", "label_t5": "up",
                    "car_t1": .01, "car_t3": .02, "car_t5": .03,
                },
                {
                    "event_id": events[1].event_id,
                    "label_t1": "down", "label_t3": "down", "label_t5": "",
                    "car_t1": -.01, "car_t3": -.02, "car_t5": None,
                },
            ]
            Path(out_path).write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            return rows

        with patch("app.event_backtest.labeller._compute_cars_for_events", return_value={}), \
             patch("app.event_backtest.labeller.write_labels", side_effect=incomplete_write):
            response = client.post(
                f"/api/bt/datasets/{dataset['id']}/oracle?required_horizon=t5&min_coverage=1"
            )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Oracle 覆盖不足", response.json()["detail"])
        self.assertIsNone(db.get_bt_dataset(dataset["id"])["labels_path"])


if __name__ == "__main__":
    unittest.main()
