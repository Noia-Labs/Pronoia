"""Regression tests for the unified backtest/Arena MVP contracts."""
from __future__ import annotations

import os
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path


class TestUnifiedBacktestArena(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temp.name)
        cls._old_env_db = os.environ.get("FEVER_DB_PATH")
        cls._old_env_data = os.environ.get("FEVER_DATA_DIR")
        os.environ["FEVER_DB_PATH"] = str(cls.root / "unified.db")
        os.environ["FEVER_DATA_DIR"] = str(cls.root / "data")
        from app import config, db

        cls._old_config_db = config.DB_PATH
        cls._old_config_data = config.DATA_DIR
        config.DB_PATH = os.environ["FEVER_DB_PATH"]
        config.DATA_DIR = os.environ["FEVER_DATA_DIR"]
        # routes.backtest imports DATA_DIR by value, so align it for this isolated test.
        from app.routes import backtest
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

    def test_01_manual_dataset_and_provided_analysis_runner(self):
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.post("/api/bt/datasets/manual", json={
            "name": "人工事件判断集",
            "events": [{
                "market": "CN",
                "symbol": "600000",
                "event_time": "2025-01-10T09:00:00+08:00",
                "available_time": "2025-01-10T09:05:00+08:00",
                "title": "测试新闻",
                "event_text": "公司发布正式公告。",
                "analysis_direction": "up",
                "confidence": 0.78,
                "rationale": "公告改善未来现金流预期。",
                "horizon": "t5",
            }],
        })
        self.assertEqual(response.status_code, 200, response.text)
        dataset = response.json()
        self.assertEqual(dataset["oracle_status"], "unavailable")
        self.assertTrue(Path(dataset["path"]).is_file())

        spoofed = client.post("/api/bt/runs", json={
            "name": "不可伪造的协议",
            "runner": "provided_analysis",
            "dataset_id": dataset["id"],
            "protocol_hash": "client-controlled-fingerprint",
        })
        self.assertEqual(spoofed.status_code, 409, spoofed.text)

        created = client.post("/api/bt/runs", json={
            "name": "用户分析回测",
            "runner": "provided_analysis",
            "dataset_id": dataset["id"],
            "strategy_type": "event",
            "visibility": "private",
            "execution_spec": {"delay": "next_bar", "fee_bps": 3},
        })
        self.assertEqual(created.status_code, 200, created.text)
        run = created.json()
        self.assertEqual(run["dataset_id"], dataset["id"])
        self.assertEqual(run["dataset_name"], "人工事件判断集")
        self.assertEqual(run["visibility"], "private")
        self.assertEqual(run["oracle_status"], "unavailable")
        self.assertEqual(run["evaluation_horizon"], "t5")
        self.assertEqual(len(run["protocol_hash"]), 64)

        started = client.post(f"/api/bt/runs/{run['id']}/start")
        self.assertEqual(started.status_code, 200, started.text)
        for _ in range(100):
            current = client.get(f"/api/bt/runs/{run['id']}").json()
            if current["status"] in {"done", "failed"}:
                break
            time.sleep(0.02)
        self.assertEqual(current["status"], "done", current.get("error_msg"))
        predictions = client.get(f"/api/bt/runs/{run['id']}/events").json()["items"]
        self.assertEqual(len(predictions), 1)
        self.assertEqual(predictions[0]["pred_direction"], "up")
        self.assertEqual(predictions[0]["horizon"], "t5")

        # Point-in-time availability survives the JSONL -> EventRecord boundary.
        from app.event_backtest.application import load_events
        event = load_events(dataset["path"])[0]
        self.assertEqual(event.available_time, "2025-01-10T09:05:00+08:00")
        self.assertEqual(event.occurred_at, "2025-01-10T09:00:00+08:00")
        self.assertEqual(event.event_time, event.available_time)
        catalog = client.get(f"/api/bt/runs/{run['id']}/events-catalog").json()["items"][0]
        self.assertEqual(catalog["occurred_at"], event.occurred_at)
        self.assertEqual(catalog["available_time"], event.available_time)
        detail = client.get(f"/api/bt/runs/{run['id']}/events/{event.event_id}").json()
        self.assertEqual(detail["event_meta"]["occurred_at"], event.occurred_at)
        self.assertEqual(detail["event_meta"]["analysis_horizon"], "t5")

        # The same event facts submitted with another opinion keep a stable ID
        # and protocol fingerprint, so analysts can meet in a strict Arena.
        alternate_response = client.post("/api/bt/datasets/manual", json={
            "name": "人工事件判断集 · 观点 B",
            "events": [{
                "market": "CN",
                "symbol": "600000",
                "event_time": "2025-01-10T09:00:00+08:00",
                "available_time": "2025-01-10T09:05:00+08:00",
                "title": "测试新闻",
                "event_text": "公司发布正式公告。",
                "analysis_direction": "down",
                "confidence": 0.61,
                "rationale": "另一分析者认为市场已提前定价。",
                "horizon": "t5",
            }],
        })
        self.assertEqual(alternate_response.status_code, 200, alternate_response.text)
        alternate = alternate_response.json()
        alternate_event = load_events(alternate["path"])[0]
        self.assertEqual(event.event_id, alternate_event.event_id)
        from app.event_backtest.protocol import build_protocol_hash
        self.assertEqual(
            build_protocol_hash(events_path=dataset["path"]),
            build_protocol_hash(events_path=alternate["path"]),
        )

    def test_01b_manual_event_facts_can_choose_the_analysis_adapter(self):
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.post("/api/bt/datasets/manual", json={
            "name": "仅事件事实",
            "events": [{
                "market": "CN",
                "symbol": "600000",
                "event_time": "2025-02-10T09:00:00+08:00",
                "available_time": "2025-02-10T09:05:00+08:00",
                "title": "测试公告",
                "event_text": "这是当时可以获得的公告正文。",
            }],
        })
        self.assertEqual(response.status_code, 200, response.text)
        dataset = response.json()
        self.assertFalse(dataset["decision_ready"])
        self.assertEqual(dataset["input_contract"], "events_only")
        self.assertFalse(dataset["source"]["metadata"]["contains_imported_decisions"])

        platform = client.post("/api/bt/runs", json={
            "name": "手工事实 · 平台历史模型",
            "runner": "team_prompt",
            "dataset_id": dataset["id"],
            "dataset_version": dataset["dataset_version"],
            "strategy_type": "event",
            "strategy_spec": {
                "type": "event",
                "adapter": "existing_platform",
                "runner": "team_prompt",
                "model_id": "team_prompt",
                "version": "platform-history-v1",
                "input_contract": "events_only",
            },
        })
        self.assertEqual(platform.status_code, 200, platform.text)
        self.assertEqual(platform.json()["runner"], "team_prompt")

        external = client.post("/api/bt/runs", json={
            "name": "手工事实 · 外部事件模型",
            "runner": "external_http",
            "dataset_id": dataset["id"],
            "dataset_version": dataset["dataset_version"],
            "strategy_type": "event",
            "model_version": "decision-signal-v7",
            "strategy_spec": {
                "type": "event",
                "adapter": "external_http",
                "model_id": "decision.v2",
                "version": "decision-signal-v7",
                "input_contract": "events_only",
                "endpoint": "https://models.example.com/event-decision",
            },
        })
        self.assertEqual(external.status_code, 200, external.text)
        self.assertEqual(external.json()["runner"], "event_external_http")
        self.assertEqual(external.json()["strategy_spec"]["model_id"], "decision.v2")
        self.assertEqual(external.json()["strategy_spec"]["version"], "decision-signal-v7")

        rejected = client.post("/api/bt/runs", json={
            "name": "手工事实不可冒充已分析决策",
            "runner": "provided_analysis",
            "dataset_id": dataset["id"],
        })
        self.assertEqual(rejected.status_code, 422, rejected.text)

    def test_02_arena_engine_contract_and_ranking_direction(self):
        from app.event_backtest.arena import ArenaRunContext, compute_arena_result
        from app.event_backtest.protocol import build_protocol_hash

        facts = {
            "event_id": "stable-event", "market": "CN", "symbol": "600000",
            "event_time": "2025-01-10T09:05:00+08:00", "title": "同一事件",
        }
        path_a, path_b = self.root / "opinion-a.jsonl", self.root / "opinion-b.jsonl"
        path_a.write_text(json.dumps({**facts, "analysis_direction": "up", "confidence": .8}) + "\n")
        path_b.write_text(json.dumps({**facts, "analysis_direction": "down", "confidence": .6}) + "\n")
        self.assertEqual(
            build_protocol_hash(
                events_path=path_a,
                evaluation_protocol={"dataset_id": "opinion-a", "dataset_name": "观点 A", "evaluation_horizon": "t3"},
            ),
            build_protocol_hash(
                events_path=path_b,
                evaluation_protocol={"dataset_id": "opinion-b", "dataset_name": "观点 B", "evaluation_horizon": "t3"},
            ),
            "strategy opinions must not contaminate the comparison protocol",
        )

        def metric(value, n, k):
            return {"value": value, "meta": {"n": n, "k": k}, "breakdown": {}}

        a = ArenaRunContext(
            run_id="run-a", run_info={"protocol_hash": "same"}, runner="a",
            metrics={"acc_t3_strict": metric(0.8, 100, 80), "calibration_mse": metric(0.12, 100, 0)},
            predictions_by_eid={"e1": {"pred_direction": "up", "confidence": .8}},
            tokens_in=100, tokens_out=20,
        )
        b = ArenaRunContext(
            run_id="run-b", run_info={"protocol_hash": "same"}, runner="b",
            metrics={"acc_t3_strict": metric(0.6, 100, 60), "calibration_mse": metric(0.20, 100, 0)},
            predictions_by_eid={"e1": {"pred_direction": "down", "confidence": .7}},
            tokens_in=50, tokens_out=10,
        )
        result = compute_arena_result(
            [a, b],
            selected_metric_ids=["acc_t3_strict", "calibration_mse"],
            labels_list=[{"event_id": "e1", "label_t3": "up"}],
        )
        self.assertEqual(result["ranking"]["acc_t3_strict"][0]["run_id"], "run-a")
        self.assertEqual(result["ranking"]["calibration_mse"][0]["run_id"], "run-a")
        pair = result["pairwise_tests"]["run-a"]["run-b"]
        self.assertIn("per_metric", pair)
        self.assertEqual(pair["per_metric"]["acc_t3_strict"]["winner"], "run-a")
        self.assertAlmostEqual(
            result["pairwise_tests"]["run-b"]["run-a"]["per_metric"]["acc_t3_strict"]["delta"],
            -0.2,
        )
        self.assertEqual(result["head_to_head"]["event_level"][0]["best_runs"], ["run-a"])
        self.assertIn("run-a", result["composite_score"]["per_run_score"])
        self.assertTrue(result["comparison_protocol"]["strict_comparable"])

        a.run_info["visibility"] = "arena_safe"
        protected = compute_arena_result(
            [a, b],
            selected_metric_ids=["acc_t3_strict", "calibration_mse"],
            labels_list=[{"event_id": "e1", "label_t3": "up"}],
        )
        self.assertEqual(protected["head_to_head"]["event_level"], [])
        self.assertTrue(protected["head_to_head"]["privacy"]["event_level_redacted"])
        self.assertEqual(protected["head_to_head"]["privacy"]["redacted_run_ids"], ["run-a"])

    def test_03_saved_arena_locks_protocol_and_metrics(self):
        from app import db
        from app.main import app
        from fastapi.testclient import TestClient

        source_unscored = next(x for x in db.list_bt_runs() if x["runner"] == "provided_analysis")
        from app.event_backtest.application import load_events
        from app.event_backtest.protocol import build_protocol_hash
        source_event_id = load_events(source_unscored["events_path"])[0].event_id
        labels_path = self.root / "arena-labels.jsonl"
        labels_path.write_text(json.dumps({
            "event_id": source_event_id,
            "label_t1": "up", "label_t3": "up", "label_t5": "up",
            "car_t1": .01, "car_t3": .02, "car_t5": .03,
        }) + "\n", encoding="utf-8")
        scored_protocol = build_protocol_hash(
            events_path=source_unscored["events_path"],
            labels_path=labels_path,
        )
        source = db.create_bt_run(
            name="scored source",
            runner="provided_analysis",
            events_path=source_unscored["events_path"],
            labels_path=str(labels_path),
            out_path=source_unscored["out_path"],
            dataset_id=source_unscored["dataset_id"],
            dataset_name=source_unscored["dataset_name"],
            protocol_hash=scored_protocol,
        )
        db.update_bt_run_status(source["id"], "done")
        same = db.create_bt_run(
            name="same protocol",
            runner="provided_analysis",
            events_path=source["events_path"],
            labels_path=str(labels_path),
            out_path=source["out_path"],
            dataset_id=source["dataset_id"],
            dataset_name=source["dataset_name"],
            protocol_hash=source["protocol_hash"],
        )
        db.update_bt_run_status(same["id"], "done")
        db.add_bt_prediction(
            run_id=same["id"], event_id="cost-e1", pred_direction="up",
            tokens_in=1_000, tokens_out=200, step_ms=321, cost_usd=.0123,
        )
        different = db.create_bt_run(
            name="different protocol", runner="provided_analysis",
            events_path=source["events_path"], out_path=str(self.root / "different.jsonl"),
            protocol_hash="different-protocol",
        )
        protected = db.create_bt_run(
            name="aggregate only", runner="provided_analysis",
            events_path=source["events_path"], out_path=str(self.root / "protected.jsonl"),
            protocol_hash=source["protocol_hash"], visibility="arena_safe",
        )
        client = TestClient(app)
        protected_events = client.get(f"/api/bt/runs/{protected['id']}/events").json()
        self.assertEqual(protected_events["items"], [])
        protected_catalog = client.get(f"/api/bt/runs/{protected['id']}/events-catalog").json()
        self.assertEqual(protected_catalog["items"], [])
        protected_detail = client.get(f"/api/bt/runs/{protected['id']}/events/any-event")
        self.assertEqual(protected_detail.status_code, 403, protected_detail.text)
        ok = client.post("/api/arena", json={
            "name": "公平对比",
            "run_ids": [source["id"], same["id"]],
            "arena_type": "prediction",
            "selected_metric_ids": ["acc_t3_strict", "calibration_mse"],
        })
        self.assertEqual(ok.status_code, 200, ok.text)
        arena = ok.json()
        self.assertEqual(arena["selected_metric_ids"], ["acc_t3_strict", "calibration_mse"])
        self.assertEqual(arena["config"]["arena_type"], "prediction")
        # Arena uses the event-fact/Oracle comparison signature; the original
        # Run integrity fingerprint remains frozen independently.
        from app.event_backtest.protocol import comparison_protocol_hash_for_run
        self.assertEqual(arena["protocol_hash"], comparison_protocol_hash_for_run(source))
        self.assertEqual(db.get_bt_run(source["id"])["protocol_hash"], source["protocol_hash"])
        computed = client.post(f"/api/arena/{arena['id']}/compute", json={})
        self.assertEqual(computed.status_code, 200, computed.text)
        self.assertEqual(computed.json()["status"], "done")
        self.assertTrue(computed.json()["result"]["comparison_protocol"]["strict_comparable"])
        self.assertIsInstance(computed.json()["result"]["pairwise_tests"], dict)
        same_cost = computed.json()["result"]["per_run"][same["id"]]
        self.assertEqual(same_cost["tokens_total"], 1_200)
        self.assertEqual(same_cost["step_ms_total"], 321)
        self.assertAlmostEqual(same_cost["cost_usd_estimate"], .0123)

        rejected = client.post("/api/arena", json={
            "name": "不公平对比",
            "run_ids": [source["id"], different["id"]],
        })
        self.assertEqual(rejected.status_code, 409, rejected.text)

    def test_04_event_trade_proxy_metrics(self):
        from app.event_backtest.metrics_registry import compute_all_metrics
        from app.event_backtest.models import EventLabel, TeamPrediction

        predictions = [
            TeamPrediction(event_id="e1", pred_direction="up", run_id="r"),
            TeamPrediction(event_id="e2", pred_direction="down", run_id="r"),
        ]
        labels = [
            EventLabel(event_id="e1", label_t1="up", label_t3="up", label_t5="up", car_t3=.10),
            EventLabel(event_id="e2", label_t1="up", label_t3="up", label_t5="up", car_t3=.05),
        ]
        metrics = compute_all_metrics(predictions=predictions, labels=labels)
        self.assertAlmostEqual(metrics["strategy_total_return"].value, 1.10 * .95 - 1.0)
        self.assertAlmostEqual(metrics["strategy_max_drawdown"].value, .05)
        self.assertAlmostEqual(metrics["strategy_win_rate"].value, .5)
        self.assertEqual(metrics["strategy_total_return"].meta["method"], "event_trade_proxy")

    def test_05_oracle_endpoint_registers_only_real_nonempty_output(self):
        from app import db
        from app.main import app
        from fastapi.testclient import TestClient

        dataset = next(x for x in db.list_bt_datasets() if x["name"] == "人工事件判断集")

        def fake_write(events, cars, out_path, epsilon=.005):
            rows = [{
                "event_id": events[0].event_id,
                "label_t1": "up", "label_t3": "up", "label_t5": "up",
                "car_t1": .01, "car_t3": .03, "car_t5": .04,
            }]
            Path(out_path).write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
            return rows

        with patch("app.event_backtest.labeller._compute_cars_for_events", return_value={}), \
             patch("app.event_backtest.labeller.write_labels", side_effect=fake_write):
            response = TestClient(app).post(f"/api/bt/datasets/{dataset['id']}/oracle")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["oracle_status"], "available")
        self.assertTrue(Path(response.json()["labels_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
