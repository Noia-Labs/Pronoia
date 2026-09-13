"""Exercise saved market dataset -> unified Run -> performance -> forecast Arena."""
from __future__ import annotations

import csv
import json
import socket
import time
from pathlib import Path

import pytest


@pytest.fixture
def client_and_dataset(tmp_path, monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    from app import config, db
    from app.routes import backtest
    from app.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "unified-forecast.db"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(backtest, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "LLM_API_KEY", "synthetic-unused-key")
    db.init_db()
    def no_network(*args, **kwargs):
        raise AssertionError("Unified forecast integration must use only frozen local bars")
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    dates = ["02", "03", "06", "07", "08", "09", "10", "13", "14", "15", "16", "17", "20", "21", "22", "23"]
    closes = [100, 102, 101, 105, 103, 109, 111, 108, 114, 112, 120, 119, 125, 122, 127, 126]
    csv_path = tmp_path / "synthetic-bars.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "high", "low", "close", "volume", "symbol"])
        for day, close in zip(dates, closes):
            writer.writerow([f"2025-01-{day}", close, close + 1, close - 1, close, 1000, "000300.SH"])
    client = TestClient(app)
    response = client.post("/api/bt/datasets/register", json={
        "id": "forecast_integration_bars", "name": "合成行情集成测试", "dataset_kind": "market", "path": str(csv_path),
        "source": {"type": "local_file", "provider": "user_supplied"}, "markets": ["CN"],
        "asset_type": "index", "frequency": "1d", "adjustment": "none", "calendar": "XSHG", "symbols": ["000300.SH"],
    })
    assert response.status_code == 200, response.text
    dataset = response.json()
    assert dataset["status"] == "available" and dataset["dataset_version"]
    assert dataset["coverage"]["bar_count_total"] == 16
    yield client, dataset
    client.close()
    if db._conn is not None:
        db._conn.close()


def finish_run(client, dataset, *, lookback, horizon):
    created = client.post("/api/bt/runs", json={
        "name": f"均值预测 lookback={lookback} horizon={horizon}",
        "dataset_id": dataset["id"], "dataset_version": dataset["dataset_version"],
        "strategy_spec": {"type": "quant", "adapter": "builtin", "kind": "return_forecast", "source": "builtin",
                          "parameters": {"method": "historical_mean", "lookback": lookback, "horizon_bars": horizon}},
        "execution_spec": {"initial_capital": 100000, "fee_bps": 3, "slippage_bps": 2},
    })
    assert created.status_code == 200, created.text
    run = created.json()
    assert run["engine_mode"] == "portfolio"
    assert run["strategy_spec"]["kind"] == "return_forecast"
    assert run["strategy_spec"]["parameters"]["horizon_bars"] == horizon
    start = client.post(f"/api/bt/runs/{run['id']}/start")
    assert start.status_code == 200, start.text
    for _ in range(150):
        read = client.get(f"/api/bt/runs/{run['id']}")
        assert read.status_code == 200, read.text
        run = read.json()
        if run["status"] in {"done", "failed"}:
            break
        time.sleep(.01)
    assert run["status"] == "done", run.get("error_msg")
    return run


def test_unified_quant_forecasts_reach_performance_and_mae_arena(client_and_dataset):
    client, dataset = client_and_dataset
    runs = [finish_run(client, dataset, lookback=lookback, horizon=3) for lookback in (2, 4)]
    maes = {}
    for run in runs:
        response = client.get(f"/api/bt/runs/{run['id']}/performance")
        assert response.status_code == 200, response.text
        performance = response.json()
        assert performance["prediction_only"] is True
        assert performance["return_forecast"]["horizon_bars"] == 3
        assert performance["return_forecast"]["evaluated_count"] > 0
        maes[run["id"]] = performance["return_forecast"]["mae_pct"]
        assert isinstance(maes[run["id"]], float)
        assert performance["return_forecasts"]
        assert any(row["actual_return_pct"] is not None for row in performance["return_forecasts"])
        assert performance["summary"].get("total_return") is None
        assert performance["equity_curve"] == [] and performance["trades"] == []
        assert run["metrics"]["strategy_total_return"]["value"] is None
        assert run["metrics"]["return_forecast"]["value"] == maes[run["id"]]
        # Read the actual frozen Run artifact, not just a response projection.
        persisted = json.loads(Path(run["result_path"]).read_text())
        assert persisted["return_forecasts"] == performance["return_forecasts"]
    assert len(set(maes.values())) == 2
    created = client.post("/api/arena", json={"name": "同周期收益预测对比", "run_ids": [r["id"] for r in runs],
        "arena_type": "prediction", "selected_metric_ids": ["return_forecast"]})
    assert created.status_code == 200, created.text
    computed = client.post(f"/api/arena/{created.json()['id']}/compute", json={})
    assert computed.status_code == 200, computed.text
    result = computed.json()["result"]
    assert result["comparison_protocol"]["strict_comparable"] is True
    assert result["metric_defs"]["return_forecast"]["higher_is_better"] is False
    rows = result["ranking"]["return_forecast"]
    assert [row["run_id"] for row in rows] == sorted(maes, key=maes.get)
    assert rows[0]["rank"] == 1
    other_horizon = finish_run(client, dataset, lookback=2, horizon=5)
    incompatible = client.post("/api/arena/compute", json={"run_ids": [runs[0]["id"], other_horizon["id"]],
        "arena_type": "prediction", "selected_metric_ids": ["return_forecast"]})
    assert incompatible.status_code == 409, incompatible.text
    assert incompatible.json()["detail"]["comparison_protocol"]["strict_comparable"] is False
