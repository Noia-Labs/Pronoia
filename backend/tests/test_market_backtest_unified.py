"""Unified market-dataset, portfolio Run, and Arena contract tests."""
from __future__ import annotations

import csv
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class TestUnifiedMarketBacktest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temp.name)
        from app import config, db

        cls._config_db, cls._config_data = config.DB_PATH, config.DATA_DIR
        cls._old_db = os.environ.get("FEVER_DB_PATH")
        cls._old_data = os.environ.get("FEVER_DATA_DIR")
        os.environ["FEVER_DB_PATH"] = str(cls.root / "market.db")
        os.environ["FEVER_DATA_DIR"] = str(cls.root / "data")
        from app.routes import backtest

        config.DB_PATH = os.environ["FEVER_DB_PATH"]
        config.DATA_DIR = os.environ["FEVER_DATA_DIR"]
        backtest.DATA_DIR = os.environ["FEVER_DATA_DIR"]
        db._conn = None
        db.init_db()
        cls.csv_path = cls.root / "bars.csv"
        rows = [
            ("2025-01-02", 100, 101, 98, 99, 1000),
            ("2025-01-03", 99, 100, 96, 97, 1100),
            ("2025-01-06", 97, 99, 96, 98, 1200),
            ("2025-01-07", 99, 105, 99, 104, 1800),
            ("2025-01-08", 104, 107, 103, 106, 1700),
            ("2025-01-09", 106, 107, 100, 101, 1600),
            ("2025-01-10", 100, 101, 95, 96, 2000),
            ("2025-01-13", 96, 103, 96, 102, 1900),
        ]
        with cls.csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "open", "high", "low", "close", "volume", "symbol"])
            for row in rows:
                writer.writerow([*row, "000300.SH"])

    @classmethod
    def tearDownClass(cls):
        from app import config, db
        from app.routes import backtest

        if db._conn is not None:
            db._conn.close()
            db._conn = None
        config.DB_PATH, config.DATA_DIR = cls._config_db, cls._config_data
        backtest.DATA_DIR = cls._config_data
        if cls._old_db is None:
            os.environ.pop("FEVER_DB_PATH", None)
        else:
            os.environ["FEVER_DB_PATH"] = cls._old_db
        if cls._old_data is None:
            os.environ.pop("FEVER_DATA_DIR", None)
        else:
            os.environ["FEVER_DATA_DIR"] = cls._old_data
        cls._temp.cleanup()

    def test_01_pending_source_is_honest_and_market_snapshot_is_versioned(self):
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        pending = client.post("/api/bt/datasets/register", json={
            "id": "pending_api", "name": "等待导入的分钟数据", "dataset_kind": "market",
            "source": {"type": "api", "provider": "future-provider", "ref": "dataset://minute-v1"},
            "markets": ["CN"], "asset_type": "index", "frequency": "1m",
            "adjustment": "none", "calendar": "XSHG", "symbols": ["000300.SH"],
            "schema_mapping": {"timestamp": "datetime", "open": "open", "high": "high", "low": "low", "close": "close"},
        })
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertEqual(pending.json()["status"], "pending")
        self.assertFalse(pending.json()["capabilities"]["portfolio_execution"])
        blocked = client.post("/api/bt/runs", json={
            "name": "不得造数据", "dataset_id": "pending_api",
            "strategy_spec": {"type": "quant", "adapter": "builtin", "kind": "buy_hold"},
        })
        self.assertEqual(blocked.status_code, 409, blocked.text)

        registered = client.post("/api/bt/datasets/register", json={
            "id": "cn_index_daily", "name": "真实本地指数日线", "dataset_kind": "market",
            "path": str(self.csv_path),
            "source": {"type": "local_file", "provider": "user_supplied"},
            "markets": ["CN"], "asset_type": "index", "frequency": "1d",
            "adjustment": "none", "calendar": "XSHG", "symbols": ["000300.SH"],
        })
        self.assertEqual(registered.status_code, 200, registered.text)
        type(self).dataset = registered.json()
        self.assertEqual(self.dataset["status"], "available")
        self.assertEqual(self.dataset["quality_status"], "passed")
        self.assertEqual(self.dataset["coverage"]["bar_count_total"], 8)
        self.assertEqual(len(self.dataset["dataset_version"]), 36)
        versions = client.get("/api/bt/datasets/cn_index_daily/versions").json()
        self.assertEqual(versions["total"], 1)
        self.assertEqual(versions["current_version"], self.dataset["dataset_version"])

    def _create_and_finish(self, client, *, name: str, strategy_spec: dict) -> dict:
        created = client.post("/api/bt/runs", json={
            "name": name,
            "dataset_id": "cn_index_daily",
            "dataset_version": self.dataset["dataset_version"],
            "strategy_type": "signal_import",
            "strategy_spec": strategy_spec,
            "execution_spec": {
                "initial_capital": 100_000, "fee_bps": 3, "slippage_bps": 2,
                "execution_delay": "next_open", "price_field": "open",
            },
        })
        self.assertEqual(created.status_code, 200, created.text)
        run = created.json()
        self.assertEqual(run["strategy_type"], "quant")
        self.assertEqual(run["engine_mode"], "portfolio")
        self.assertEqual(run["dataset_version"], self.dataset["dataset_version"])
        started = client.post(f"/api/bt/runs/{run['id']}/start")
        self.assertEqual(started.status_code, 200, started.text)
        for _ in range(100):
            run = client.get(f"/api/bt/runs/{run['id']}").json()
            if run["status"] in {"done", "failed"}:
                break
            time.sleep(.02)
        self.assertEqual(run["status"], "done", run.get("error_msg"))
        return run

    def test_02_declarative_rules_and_buy_hold_share_unified_performance_and_arena(self):
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        rules = self._create_and_finish(client, name="通用规则", strategy_spec={
            "type": "quant", "adapter": "builtin", "model_id": "declarative_rules",
            "parameters": {
                "entry": {"combinator": "and", "conditions": [
                    {"field": "moving_average", "operator": "crosses_above", "lookback": 2, "threshold": 0},
                ]},
                "exit": {"combinator": "and", "conditions": [
                    {"field": "moving_average", "operator": "crosses_below", "lookback": 2, "threshold": 0},
                ]},
            },
        })
        hold = self._create_and_finish(client, name="买入持有", strategy_spec={
            "type": "quant", "adapter": "builtin", "kind": "buy_hold",
        })
        type(self).rules_run = rules
        perf = client.get(f"/api/bt/runs/{rules['id']}/performance").json()
        self.assertEqual(perf["mode"], "portfolio")
        self.assertEqual(perf["result_nature"], "simulated_from_real_bars")
        self.assertEqual(len(perf["bars"]), 8)
        self.assertEqual(len(perf["equity_curve"]), 8)
        self.assertTrue(perf["trades"])
        self.assertIsNotNone(perf["summary"]["total_return"])
        exported = client.get(f"/api/bt/runs/{rules['id']}/trades.csv")
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertIn("execution_timestamp", exported.text)

        arena = client.post("/api/arena", json={
            "name": "同一指数同协议", "run_ids": [rules["id"], hold["id"]],
            "arena_type": "performance",
            "selected_metric_ids": ["strategy_total_return", "strategy_max_drawdown"],
        })
        self.assertEqual(arena.status_code, 200, arena.text)
        self.assertEqual(arena.json()["config"]["comparison_protocol"]["engine_mode"], "portfolio")
        computed = client.post(f"/api/arena/{arena.json()['id']}/compute", json={})
        self.assertEqual(computed.status_code, 200, computed.text)
        self.assertTrue(computed.json()["result"]["comparison_protocol"]["strict_comparable"])

    def test_03_multi_symbol_snapshot_is_valid_data_but_not_mislabelled_executable(self):
        from app.main import app
        from fastapi.testclient import TestClient

        multi = self.root / "multi.csv"
        with multi.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "open", "high", "low", "close", "symbol"])
            for symbol, shift in (("A", 0), ("B", 10)):
                writer.writerow(["2025-01-02", 10 + shift, 11 + shift, 9 + shift, 10 + shift, symbol])
                writer.writerow(["2025-01-03", 10 + shift, 12 + shift, 9 + shift, 11 + shift, symbol])
        response = TestClient(app).post("/api/bt/datasets/register", json={
            "id": "multi", "name": "多标行情", "dataset_kind": "market", "path": str(multi),
            "markets": ["CN"], "asset_type": "equity", "frequency": "1d", "symbols": ["A", "B"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["coverage"]["bar_count_total"], 4)
        self.assertFalse(payload["capabilities"]["portfolio_execution"])
        self.assertTrue(payload["capabilities"]["multi_symbol_data"])

    def test_04_external_portfolio_model_rechecks_dns_before_request(self):
        from unittest.mock import patch

        from app.quant_backtest.models import Bar, MarketDataset, QuantBacktestError
        from app.quant_backtest.strategies import ExternalHTTPStrategy

        dataset = MarketDataset(
            name="runtime DNS guard", symbol="TEST", market="US", frequency="1d",
            bars=(
                Bar("2025-01-02", 10, 11, 9, 10),
                Bar("2025-01-03", 10, 12, 9, 11),
            ),
        )
        strategy = ExternalHTTPStrategy("https://model.example/v1/signals")
        with patch(
            "app.quant_backtest.strategies.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ), patch("app.quant_backtest.strategies.build_opener") as opener:
            with self.assertRaisesRegex(QuantBacktestError, "私网、回环或保留地址"):
                strategy.generate(dataset)
            opener.assert_not_called()

    def test_04b_external_portfolio_model_revalidates_redirects_and_is_unverified(self):
        from urllib.request import Request
        from unittest.mock import patch

        from app.event_backtest.strategy_registry import normalize_strategy_spec
        from app.quant_backtest.models import QuantBacktestError
        from app.quant_backtest.strategies import (
            ExternalHTTPStrategy,
            _validate_runtime_http_endpoint,
            _ValidatedRedirectHandler,
        )

        normalized = normalize_strategy_spec(
            {
                "type": "api",
                "adapter": "external_http",
                "endpoint": "https://model.example/v1/signals",
            },
            legacy_runner=None,
            legacy_strategy_type=None,
        )
        self.assertIs(normalized["point_in_time_enforced"], False)
        self.assertEqual(normalized["result_nature"], "unverified")
        described = ExternalHTTPStrategy("https://model.example/v1/signals").describe()
        self.assertIs(described["point_in_time_enforced"], False)
        self.assertEqual(described["result_nature"], "unverified")

        with patch.dict(os.environ, {"PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS": "1"}), patch(
            "app.quant_backtest.strategies.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ):
            with self.assertRaisesRegex(QuantBacktestError, "公网.*仍必须使用 HTTPS"):
                _validate_runtime_http_endpoint("http://public.example/signals")
            _validate_runtime_http_endpoint("https://public.example/signals")

        handler = _ValidatedRedirectHandler()
        source = Request("https://public.example/model", method="POST")
        with patch(
            "app.quant_backtest.strategies.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            for target in (
                "https://127.0.0.1/admin",
                "https://model.internal.local/signals",
            ):
                with self.subTest(target=target), self.assertRaisesRegex(
                    QuantBacktestError, "私网、回环或保留地址",
                ):
                    handler.redirect_request(source, None, 302, "Found", {}, target)
        with self.assertRaisesRegex(QuantBacktestError, r"http\(s\)"):
            handler.redirect_request(source, None, 302, "Found", {}, "file:///etc/passwd")
        with patch(
            "app.quant_backtest.strategies.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ):
            redirected = handler.redirect_request(
                source, None, 302, "Found", {}, "https://model-v2.example/signals",
            )
        self.assertEqual(redirected.full_url, "https://model-v2.example/signals")
        self.assertEqual(handler.max_redirections, 5)

    def test_05_outside_window_is_clipped_and_persists_actual_execution_audit(self):
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        created = client.post("/api/bt/runs", json={
            "name": "超界请求取三日真实交集",
            "dataset_id": "cn_index_daily",
            "dataset_version": self.dataset["dataset_version"],
            "strategy_spec": {"type": "quant", "adapter": "builtin", "kind": "buy_hold"},
            "execution_spec": {
                "start_date": "2024-12-01", "end_date": "2025-01-06",
                "execution_delay": "next_open", "price_field": "open",
            },
        })
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["id"]
        self.assertEqual(client.post(f"/api/bt/runs/{run_id}/start").status_code, 200)
        for _ in range(100):
            run = client.get(f"/api/bt/runs/{run_id}").json()
            if run["status"] in {"done", "failed"}:
                break
            time.sleep(.02)
        self.assertEqual(run["status"], "done", run.get("error_msg"))
        self.assertEqual(run["done_events"], 3)
        self.assertEqual(run["total_events"], 3)
        performance = client.get(f"/api/bt/runs/{run_id}/performance").json()
        protocol = performance["effective_protocol"]
        self.assertEqual(protocol["requested"]["start_date"], "2024-12-01")
        self.assertEqual(protocol["requested"]["end_date"], "2025-01-06")
        self.assertNotIn("start_date", protocol["applied"])
        self.assertNotIn("end_date", protocol["applied"])
        self.assertEqual(protocol["applied"]["start_at"], "2025-01-02")
        self.assertEqual(protocol["applied"]["end_at"], "2025-01-06")
        self.assertEqual(protocol["applied"]["available_start"], "2025-01-02")
        self.assertEqual(protocol["applied"]["available_end"], "2025-01-13")
        self.assertIs(protocol["applied"]["window_clipped"], True)
        self.assertEqual(protocol["applied"]["selected_bar_count"], 3)
        self.assertEqual(performance["dataset"]["metadata"]["effective_start"], "2025-01-02")
        self.assertIs(performance["dataset"]["metadata"]["window_clipped"], True)

    def test_06_each_actual_rebalance_charges_bps_and_sell_stamp_duty(self):
        from app.quant_backtest.engine import run_backtest
        from app.quant_backtest.models import Bar, ExecutionConfig, MarketDataset, Signal

        dataset = MarketDataset(
            name="constant prices", symbol="000300.SH", market="CN", frequency="1d",
            bars=tuple(
                Bar(f"2025-02-0{day}", 100, 100, 100, 100, 1_000)
                for day in range(1, 5)
            ),
        )
        result = run_backtest(
            dataset,
            [Signal("2025-02-01", 1.0), Signal("2025-02-02", 0.0)],
            execution=ExecutionConfig(
                initial_capital=100_000,
                commission_bps=3,
                slippage_bps=2,
                stamp_duty_bps=10,
                other_cost_bps=1,
                minimum_commission=50,
            ),
        )
        self.assertEqual(len(result.trades), 2)
        buy, sell = result.trades
        self.assertEqual(buy["side"], "buy")
        self.assertEqual(sell["side"], "sell")
        self.assertEqual(buy["stamp_duty"], 0.0)
        self.assertGreater(sell["stamp_duty"], 0.0)
        self.assertTrue(buy["minimum_commission_applied"])
        self.assertTrue(sell["minimum_commission_applied"])
        self.assertAlmostEqual(result.metrics["total_commission"], 100.0, places=6)
        component_total = sum(
            float(result.metrics[key])
            for key in ("total_commission", "total_slippage", "total_stamp_duty", "total_other_cost")
        )
        self.assertAlmostEqual(result.metrics["total_cost"], component_total, places=8)
        self.assertAlmostEqual(sum(float(item["total_cost"]) for item in result.trades), component_total, places=8)

    def test_07_cost_scenario_clones_frozen_run_without_mutating_source(self):
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        source_before = client.get(f"/api/bt/runs/{self.rules_run['id']}").json()
        response = client.post(
            f"/api/bt/runs/{self.rules_run['id']}/cost-scenario",
            json={
                "name": "通用规则 · 成本压力测试",
                "commission_bps": 7,
                "slippage_bps": 4,
                "stamp_duty_bps": 10,
                "other_cost_bps": 1,
                "minimum_commission": 5,
                "auto_start": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        scenario = response.json()
        self.assertNotEqual(scenario["id"], source_before["id"])
        self.assertNotEqual(scenario["protocol_hash"], source_before["protocol_hash"])
        self.assertEqual(scenario["config"]["scenario_of"], source_before["id"])
        applied = scenario["execution_spec"]["applied"]
        self.assertEqual(applied["commission_bps"], 7.0)
        self.assertEqual(applied["slippage_bps"], 4.0)
        self.assertEqual(applied["stamp_duty_bps"], 10.0)
        self.assertEqual(applied["other_cost_bps"], 1.0)
        self.assertEqual(applied["minimum_commission"], 5.0)
        source_after = client.get(f"/api/bt/runs/{source_before['id']}").json()
        self.assertEqual(source_after["protocol_hash"], source_before["protocol_hash"])
        self.assertEqual(source_after["execution_spec"], source_before["execution_spec"])

    def test_08_cost_scenario_auto_start_serializes_history_delete(self):
        from app import db
        from app.main import app
        from app.model_lab import service as model_service
        from app.routes import backtest
        from fastapi.testclient import TestClient

        setup_client = TestClient(app)
        registered = setup_client.post("/api/bt/datasets/register", json={
            "id": "cost_scenario_race_dataset",
            "name": "成本情景竞态数据",
            "dataset_kind": "market",
            "path": str(self.csv_path),
            "source": {"type": "local_file", "provider": "user_supplied"},
            "markets": ["CN"],
            "asset_type": "index",
            "frequency": "1d",
            "adjustment": "none",
            "calendar": "XSHG",
            "symbols": ["000300.SH"],
        })
        self.assertEqual(registered.status_code, 200, registered.text)
        dataset = registered.json()
        created_source = setup_client.post("/api/bt/runs", json={
            "name": "成本情景竞态源 Run",
            "dataset_id": dataset["id"],
            "dataset_version": dataset["dataset_version"],
            "strategy_spec": {"type": "quant", "adapter": "builtin", "kind": "buy_hold"},
        })
        self.assertEqual(created_source.status_code, 200, created_source.text)
        source_id = created_source.json()["id"]
        self.assertEqual(setup_client.post(f"/api/bt/runs/{source_id}/start").status_code, 200)
        for _ in range(100):
            source = setup_client.get(f"/api/bt/runs/{source_id}").json()
            if source["status"] in {"done", "failed"}:
                break
            time.sleep(0.02)
        self.assertEqual(source["status"], "done", source.get("error_msg"))

        start_entered = threading.Event()
        allow_start = threading.Event()
        delete_entered = threading.Event()
        delete_done = threading.Event()
        ids = {}
        responses = {}

        def controlled_start(run_id):
            ids["run_id"] = run_id
            start_entered.set()
            self.assertTrue(allow_start.wait(3))
            self.assertIsNotNone(db.update_bt_run_status(run_id, "running"))
            return {"ok": True, "run_id": run_id, "message": "started"}

        original_delete = model_service.delete_history_records

        def tracked_delete(run_ids, batch_ids):
            delete_entered.set()
            return original_delete(run_ids, batch_ids)

        def create_scenario():
            client = TestClient(app)
            responses["create"] = client.post(
                f"/api/bt/runs/{source_id}/cost-scenario",
                json={
                    "name": "成本情景创建启动竞态",
                    "commission_bps": 17,
                    "auto_start": True,
                },
            )

        def delete_scenario():
            client = TestClient(app)
            responses["delete"] = client.post("/api/bt/history/delete", json={
                "run_ids": [ids["run_id"]],
                "batch_ids": [],
            })
            delete_done.set()

        creator = threading.Thread(target=create_scenario)
        deleter = threading.Thread(target=delete_scenario)
        with patch.object(backtest, "start_run", controlled_start), patch.object(
            model_service, "delete_history_records", tracked_delete,
        ):
            creator.start()
            try:
                self.assertTrue(start_entered.wait(3))
                deleter.start()
                self.assertTrue(delete_entered.wait(3))
                self.assertFalse(
                    delete_done.wait(0.1),
                    "delete slipped into the cost-scenario create-to-start window",
                )
            finally:
                allow_start.set()
            creator.join(3)
            deleter.join(3)

        self.assertFalse(creator.is_alive())
        self.assertFalse(deleter.is_alive())
        self.assertEqual(responses["create"].status_code, 200, responses["create"].text)
        self.assertEqual(responses["create"].json()["status"], "running")
        self.assertEqual(responses["delete"].status_code, 409, responses["delete"].text)
        self.assertEqual(db.get_bt_run(ids["run_id"])["status"], "running")


if __name__ == "__main__":
    unittest.main()
