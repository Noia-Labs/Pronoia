"""Object-first comparison is executable, read-only for runs, and snapshot based."""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from app import db, config
    from app.event_backtest import model_comparison as service
    from app.quant_backtest.models import Bar, MarketDataset
    from app.routes import arena, arena_model_comparison
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "arena.db"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    db.init_db()
    market = MarketDataset(name="Fixture prices", symbol="600000.SH", market="CN", frequency="1d",
        bars=tuple(Bar(f"2026-09-{day:02d}", price, price + 5, price - 5, price + 2)
                   for day, price in zip(range(1, 9), [100, 102, 99, 106, 107, 103, 109, 108])))
    result = {"dataset": market.summary(), "bars": [bar.to_dict() for bar in market.bars],
              "contract": {"dataset_version": "market-v1"}, "strategy": {"kind": "return_forecast", "prediction_only": True},
              "metrics": {"prediction_only": True},
              "signals": [{"timestamp": bar.timestamp, "target_weight": 0, "expected_return_pct": -2, "horizon_bars": 3}
                          for bar in market.bars]}
    result_path = tmp_path / "quant.json"
    result_path.write_text(json.dumps(result))
    event = {"event_id": "event-one", "market": "CN", "symbol": "600000", "title": "订单公告", "event_text": "已公布的订单数据",
             "event_time": "2026-09-02T09:00:00+08:00", "available_time": "2026-09-02T10:00:00+08:00",
             "event_type_l2": "公司新闻", "source_url": "https://fixture.invalid/news"}
    events, predictions = tmp_path / "events.jsonl", tmp_path / "predictions.jsonl"
    events.write_text(json.dumps(event) + "\n")
    predictions.write_text(json.dumps({"event_id": "event-one", "pred_direction": "up", "horizon": "t3", "expected_return_pct": 2}) + "\n")
    records = {
        "event": {"id": "event", "name": "Event model", "engine_mode": "event_proxy", "status": "done", "visibility": "private",
                  "events_path": str(events), "out_path": str(predictions), "config": {}, "runner": "raw_model"},
        "quant": {"id": "quant", "name": "Quant model", "engine_mode": "portfolio", "status": "done", "visibility": "private",
                  "result_path": str(result_path), "dataset_version": "market-v1", "strategy_spec": {"kind": "return_forecast"}, "config": {}},
    }
    monkeypatch.setattr(db, "list_bt_runs", lambda **kwargs: list(records.values()))
    monkeypatch.setattr(db, "get_bt_run", records.get)
    monkeypatch.setattr(db, "list_bt_datasets", lambda: [])
    app = FastAPI()
    app.include_router(arena_model_comparison.router)
    app.include_router(arena.router)
    client = TestClient(app)
    subject = next(item for item in service.catalog()["subjects"] if item["kind"] == "event")
    request = {"subject_key": subject["key"], "market_source_id": "run:quant", "run_ids": ["event", "quant"], "holding_bars": 3}
    yield client, records, request, market, tmp_path
    client.close()
    db._conn.close()


def test_event_and_quant_share_one_replay_and_save_is_idempotent(setup):
    client, records, request, market, root = setup
    before = {path: path.read_bytes() for path in root.glob("*.json*")}
    response = client.post("/api/arena/model-comparison/preview", json=request)
    assert response.status_code == 200, response.text
    preview = response.json()
    result = preview["result"]
    assert result["schema_version"] == "arena-model-comparison-v1"
    assert {item["run_id"] for item in result["models"]} == {"event", "quant"}
    by_id = {item["run_id"]: item for item in result["models"]}
    assert by_id["quant"]["metrics"]["total_return"] == 0  # explicitly bearish; valid flat decision
    assert by_id["event"]["metrics"]["total_return"] != 0
    assert result["bars"][0]["open"] == market.bars[0].open
    assert result["rules"]["position_rule"] == "long_flat"
    assert all(item["forecast"]["n"] == 0 for item in result["models"])  # different forecast origins
    assert client.get("/api/arena").json()["total"] == 0  # preview is not a saved experiment
    saved = client.post("/api/arena/model-comparison/save", json={"name": "Cross-model replay", "preview_id": preview["preview_id"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "done"
    arena_id = saved.json()["id"]
    again = client.post("/api/arena/model-comparison/save", json={"name": "Again", "preview_id": preview["preview_id"]})
    assert again.json()["id"] == arena_id
    assert client.get("/api/arena").json()["total"] == 1
    assert client.post(f"/api/arena/{arena_id}/compute").json()["result"] == result
    assert client.post(f"/api/arena/{arena_id}/compute", json={"run_ids": ["quant", "event"]}).status_code == 409
    assert all(path.read_bytes() == content for path, content in before.items())
    assert all(record["status"] == "done" for record in records.values())


def test_changed_source_after_preview_cannot_be_saved(setup):
    client, records, request, _, _ = setup
    response = client.post("/api/arena/model-comparison/preview", json=request)
    assert response.status_code == 200, response.text
    path = Path(records["event"]["out_path"])
    path.write_text(path.read_text() + "\n")
    saved = client.post("/api/arena/model-comparison/save", json={"name": "Changed", "preview_id": response.json()["preview_id"]})
    assert saved.status_code == 409 and "变化" in saved.json()["detail"]


def test_safe_market_source_not_participating_cannot_leak_later(setup):
    client, records, request, _, _ = setup
    records["market-only"] = {**records["quant"], "id": "market-only", "name": "Market-only source"}
    request["market_source_id"] = "run:market-only"
    response = client.post("/api/arena/model-comparison/preview", json=request)
    assert response.status_code == 200, response.text
    payload = {"name": "Private source", "preview_id": response.json()["preview_id"]}
    records["market-only"]["visibility"] = "arena_safe"
    assert client.post("/api/arena/model-comparison/save", json=payload).status_code == 409
    records["market-only"]["visibility"] = "private"
    saved = client.post("/api/arena/model-comparison/save", json=payload).json()
    records["market-only"]["visibility"] = "arena_safe"
    assert client.get(f"/api/arena/{saved['id']}").status_code == 403
    assert client.post(f"/api/arena/{saved['id']}/compute").status_code == 403
    assert client.get("/api/arena").json()["items"] == []


def test_rejects_incompatible_and_incomplete_records(setup):
    client, records, request, _, _ = setup
    records["quant"]["status"] = "running"
    assert client.post("/api/arena/model-comparison/preview", json=request).status_code == 422
    records["quant"]["status"] = "done"
    assert client.post("/api/arena/model-comparison/preview", json={**request, "run_ids": ["quant", "quant"]}).status_code == 422
    assert client.post("/api/arena/model-comparison/preview", json={**request, "market_source_id": "dataset:unrelated"}).status_code == 422
    assert client.post("/api/arena/model-comparison/preview", json={**request, "holding_bars": 0}).status_code == 422
    assert client.post("/api/arena/model-comparison/preview", json={**request, "start_date": "2026-12-01", "end_date": "2026-01-01"}).status_code == 422


def test_auto_market_rejects_close_only_and_wrong_asset(setup, monkeypatch):
    from app.event_backtest import performance
    client, _, request, _, _ = setup
    request["market_source_id"] = "auto:" + request["subject_key"]
    monkeypatch.setattr(performance, "fetch_event_market_series", lambda *args, **kwargs: {
        "symbol": "600000", "market": "CN", "dates": ["2026-09-01", "2026-09-02"], "closes": [100, 102], "series_type": "line_only"})
    response = client.post("/api/arena/model-comparison/preview", json=request)
    assert response.status_code == 422 and "开盘价" in response.json()["detail"]
    monkeypatch.setattr(performance, "fetch_event_market_series", lambda *args, **kwargs: {
        "symbol": "600001", "market": "CN", "dates": ["2026-09-01", "2026-09-02"], "ohlc": [[100, 102, 99, 105]] * 2, "series_type": "ohlc"})
    response = client.post("/api/arena/model-comparison/preview", json=request)
    assert response.status_code == 422 and "其他标的" in response.json()["detail"]


def test_asset_mode_and_local_dataset_source(setup, monkeypatch):
    from app import db
    client, _, request, market, root = setup
    path = root / "bars.csv"
    path.write_text("timestamp,open,high,low,close\n" + "\n".join(
        f"{bar.timestamp},{bar.open},{bar.high},{bar.low},{bar.close}" for bar in market.bars))
    entry = {"id": "local", "name": "Local OHLC", "dataset_kind": "market", "symbols": ["600000"], "markets": ["CN"], "path": str(path), "frequency": "1d"}
    monkeypatch.setattr(db, "list_bt_datasets", lambda: [entry])
    monkeypatch.setattr(db, "get_bt_dataset", lambda value: entry if value == "local" else None)
    request.update(subject_key="asset:CN:600000.SH", market_source_id="dataset:local")
    response = client.post("/api/arena/model-comparison/preview", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["subject"]["kind"] == "asset"
    assert len(response.json()["result"]["models"]) == 2
