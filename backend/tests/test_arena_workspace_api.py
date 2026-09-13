"""Real durable job + replay integration, with no external model calls."""
import copy
import datetime as dt
import json
import threading
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, db
from app.event_backtest import arena_workspace as subject
from app.event_backtest.models import EventRecord
from app.quant_backtest.models import Bar, MarketDataset
from app.routes import arena, arena_workspace


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fixture.db"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(subject, "_JOBS", {})
    db.init_db()
    models = [{"id": identity, "kind": "quant", "track": "quant", "name": identity, "available": True,
        "supported_target_kinds": ["asset"],
        "profile_snapshot": {"model_id": "fixture", "base_url": "https://fixture.invalid", "secret_env_ref": "env:PRONOIA_MODEL_SECRET_FIXTURE"}}
        for identity in ("quant-a", "quant-b", "quant-c")]
    target = {"id": "dataset:fixture", "kind": "asset", "name": "Fixture market", "symbol": "600000.SH", "market": "CN"}
    market = MarketDataset(name="Fixture", symbol="600000.SH", market="CN", frequency="1d",
        bars=tuple(Bar((dt.date(2026, 1, 1)+dt.timedelta(days=i)).isoformat(), 100+i, 105+i, 95+i, 102+i) for i in range(40)))
    resolved = {"dataset": market, "history_dataset": market, "event": None, "target": target}
    monkeypatch.setattr(subject.catalog_service, "catalog", lambda: {"models": models, "targets": [target]})
    monkeypatch.setattr(subject.catalog_service, "resolve_model", lambda id: copy.deepcopy(next(m for m in models if m["id"] == id)))
    monkeypatch.setattr(subject.catalog_service, "resolve_target", lambda *args, **kwargs: resolved)
    monkeypatch.setattr(subject, "_launch", lambda *args: None)
    monkeypatch.setattr(subject, "estimate_model_work", lambda *args: 2)
    calls = []
    async def evaluate(model, dataset, event, settings, cancel, progress):
        calls.append(model["id"])
        progress(2, 2)
        return {"run_id": model["id"], "name": model["name"], "model_kind": model["kind"], "warnings": [],
            "observations": [{"bar_index": i, "direction": "up", "expected_return_pct": 2,
                "horizon_bars": 3, "direction_basis": "asset_return"} for i in (0, 4)], "predictions": []}
    monkeypatch.setattr(subject, "evaluate_model", evaluate)
    app = FastAPI()
    app.include_router(arena_workspace.router)
    app.include_router(arena.router)
    client = TestClient(app)
    request = {"model_ids": [m["id"] for m in models], "target_id": target["id"],
        "start_date": "2026-01-01", "end_date": "2026-02-09", "request_id": "a"*32}
    yield client, request, models, resolved, calls, tmp_path
    client.close()
    db._conn.close()


def run_worker(setup, request=None, cancel=None):
    client, original, models, _, _, _ = setup
    request = request or original
    response = client.post("/api/arena/workspace", json=request)
    assert response.status_code == 200, response.text
    aid = response.json()["id"]
    parsed = arena_workspace.WorkspaceRequest(**request).model_dump(mode="json")
    rules = response.json()["config"]["arena_workspace"]["rules"]
    subject._worker(aid, parsed, copy.deepcopy(models), rules, cancel or threading.Event())
    return client.get("/api/arena/workspace/"+aid).json()


def test_workspace_request_accepts_new_track_scope_and_shared_frequency(setup):
    request = arena_workspace.WorkspaceRequest(**{
        **setup[1], "track": "quant", "target_scope": "asset", "frequency": "5min",
    }).model_dump(mode="json")
    assert request["track"] == "quant" and request["target_scope"] == "asset"
    assert request["frequency"] == "5m"
    with pytest.raises(ValueError, match="K 线周期"):
        arena_workspace.WorkspaceRequest(**{**setup[1], "frequency": "2h"})


def test_models_run_on_one_snapshot_and_saved_results_are_readable(setup):
    client, request, models, resolved, calls, root = setup
    original = copy.deepcopy(models)
    row = run_worker(setup)
    assert row["status"] == "done"
    assert sorted(calls) == sorted(request["model_ids"])
    assert models == original  # no global or saved base-model mutation
    result = row["result"]
    assert len(result["models"]) == 3
    assert all(m["prediction_quality"]["n_numeric"] == 2 for m in result["models"])
    assert all(m["metrics"]["total_return"] > 0 for m in result["models"])
    assert len(result["bars"]) == len(resolved["dataset"].bars)
    assert result["rules"]["position_rule"] == "long_flat"
    assert row["run_ids"] == []
    assert client.get("/api/arena/"+row["id"]).json()["result"] == result
    assert client.get("/api/arena").json()["total"] == 1
    encoded = json.dumps(row)
    assert "secret_env_ref" not in encoded and "fixture.invalid" not in encoded
    snapshot = root / "data" / "arena_workspace" / row["id"] / "input.json"
    assert snapshot.is_file() and snapshot.stat().st_mode & 0o777 == 0o600
    assert json.loads(snapshot.read_text())["dataset"]["bars"][0]["close"] == 102


def test_retry_same_submission_does_not_repeat_models(setup):
    client, request, _, _, calls, _ = setup
    row = run_worker(setup)
    response = client.post("/api/arena/workspace", json=request)
    assert response.json()["id"] == row["id"] and len(calls) == 3
    assert client.post("/api/arena/workspace", json={**request, "fee_bps": 6}).status_code == 409


def test_invalid_dates_and_duplicate_models_rejected_before_execution(setup):
    client, request, _, _, calls, _ = setup
    assert client.post("/api/arena/workspace", json={**request, "start_date": "2027-01-01"}).status_code == 422
    assert client.post("/api/arena/workspace", json={**request, "model_ids": ["quant-a", "quant-a"]}).status_code == 422
    assert not calls


def test_preflight_all_models_before_any_inference(setup, monkeypatch):
    def preflight(model, *args):
        if model["id"] == "quant-b":
            raise ValueError("行情不足")
        return 1
    monkeypatch.setattr(subject, "estimate_model_work", preflight)
    row = run_worker(setup)
    assert row["status"] == "failed" and not setup[4]
    assert "行情不足" in row["config"]["progress"]["message"]
    assert all(m["status"] == "failed" for m in row["config"]["progress"]["models"])


def test_failed_model_does_not_destroy_other_model_results(setup, monkeypatch):
    evaluate = subject.evaluate_model
    async def partial(model, *args):
        if model["id"] == "quant-b":
            raise ValueError("缺少历史数据 env:PRONOIA_MODEL_SECRET_FIXTURE")
        return await evaluate(model, *args)
    monkeypatch.setattr(subject, "evaluate_model", partial)
    row = run_worker(setup)
    assert row["status"] == "partial" and row["finished_at"]
    assert len(row["result"]["models"]) == 2
    failed = next(m for m in row["config"]["progress"]["models"] if m["id"] == "quant-b")
    assert failed["status"] == "failed" and "PRONOIA_MODEL_SECRET" not in failed["error"]


def test_one_success_is_retained_without_pretending_comparison(setup, monkeypatch):
    evaluate = subject.evaluate_model
    async def partial(model, *args):
        if model["id"] != "quant-b":
            raise ValueError("接口不可用")
        return await evaluate(model, *args)
    monkeypatch.setattr(subject, "evaluate_model", partial)
    row = run_worker(setup)
    assert row["status"] == "partial" and len(row["result"]["models"]) == 1
    assert any("仅一个模型" in note for note in row["result"]["notes"])


def test_cancelled_pending_job_and_restart_never_call_models(setup):
    client, request, models, _, calls, _ = setup
    row = client.post("/api/arena/workspace", json=request).json()
    assert client.delete("/api/arena/"+row["id"]).status_code == 409
    response = client.post("/api/arena/workspace/"+row["id"]+"/cancel")
    assert response.json()["status"] == "cancelled"
    assert all(m["status"] == "cancelled" for m in response.json()["config"]["progress"]["models"])
    subject.recover_interrupted()
    assert not calls
    other = client.post("/api/arena/workspace", json={**request, "request_id": "b"*32}).json()
    assert subject.recover_interrupted() == [other["id"]]
    assert subject.get(other["id"])["status"] == "failed"
    assert not calls


def test_legacy_compute_cannot_overwrite_workspace_or_call_models(setup):
    client = setup[0]
    row = run_worker(setup)
    assert client.post("/api/arena/"+row["id"]+"/compute").json()["result"] == row["result"]
    assert client.post("/api/arena/"+row["id"]+"/compute", json={"run_ids": ["a", "b"]}).status_code == 409
    assert len(setup[4]) == 3


def test_own_quality_excludes_missing_future_and_mismatched_horizon(setup):
    market = setup[3]["dataset"]
    contestant = {"observations": [
        {"bar_index": 0, "direction": "up", "direction_basis": "market_excess", "expected_return_pct": None},
        {"bar_index": 1, "direction": "down", "direction_basis": "market_excess", "expected_return_pct": 1, "horizon_bars": 7},
        {"bar_index": 2, "direction": "up", "direction_basis": "asset_return", "expected_return_pct": 2, "horizon_bars": 3},
        {"bar_index": 39, "direction": "up", "direction_basis": "asset_return", "expected_return_pct": 2, "horizon_bars": 3}]}
    quality = subject.prediction_quality(market, contestant, 3)
    assert quality["n_numeric"] == quality["n_direction"] == quality["pending_count"] == 1
    assert quality["directional_accuracy"] == 1
    assert quality["accuracy_ci95"][0] < 0.3


def test_event_truth_is_scored_separately_from_asset_returns(setup):
    market = setup[3]["dataset"]
    contestant = {"observations": [{"bar_index": 0, "direction": "down", "direction_basis": "market_excess", "horizon_bars": 3, "expected_return_pct": None}],
        "predictions": [{"pred_direction": "down", "horizon": "t3"}]}
    quality = subject.prediction_quality(market, contestant, 3,
        {"truth_basis": "market_excess", "horizons": {"t3": {"direction": "down"}}})
    assert quality["directional_accuracy"] == 1
    assert quality["direction_basis"] == "market_excess" and quality["n_numeric"] == 0


def test_event_truth_cannot_score_beyond_selected_dates_or_require_prediction_audit(setup):
    market = setup[3]["dataset"]
    contestant = {"observations": [{"bar_index": 39, "direction": "down", "direction_basis": "market_excess", "horizon_bars": 3}]}
    truth = {"truth_basis": "market_excess", "horizons": {"t3": {"direction": "down"}}}
    quality = subject.prediction_quality(market, contestant, 3, truth)
    assert quality["directional_accuracy"] is None and quality["pending_count"] == 1
    contestant["observations"][0]["bar_index"] = 0
    quality = subject.prediction_quality(market, contestant, 3, truth)
    assert quality["directional_accuracy"] == 1 and quality["direction_basis"] == "market_excess"


def test_position_only_strategy_is_not_counted_as_pending_forecast(setup):
    contestant = {"observations": [{"bar_index": 39, "direction": "up", "direction_basis": "position", "horizon_bars": None}]}
    quality = subject.prediction_quality(setup[3]["dataset"], contestant, 3)
    assert quality["pending_count"] == 0 and quality["n_numeric"] == 0 and quality["n_direction"] == 0


def test_cancel_wins_late_progress_update_race(setup):
    client, request, *_ = setup
    row = client.post("/api/arena/workspace", json=request).json()
    subject.cancel(row["id"])
    subject._progress(row["id"], stage="predicting", model_id="quant-a", changes={"status": "running", "done": 2})
    cfg = subject.get(row["id"])["config"]
    assert cfg["progress"]["stage"] == "cancelled"
    assert all(m["status"] == "cancelled" for m in cfg["progress"]["models"])


def test_partial_prediction_points_keep_result_with_partial_status(setup, monkeypatch):
    evaluate = subject.evaluate_model
    async def partial(model, *args):
        result = await evaluate(model, *args)
        result["failed_prediction_count"] = 1
        return result
    monkeypatch.setattr(subject, "evaluate_model", partial)
    row = run_worker(setup)
    assert row["status"] == "partial"
    assert all(m["execution"]["failed_items"] == 1 for m in row["result"]["models"])


@pytest.mark.parametrize("explicit", [False, True])
def test_track_rejects_mixed_quant_and_event_models_even_when_omitted(setup, explicit):
    client, request, models, *_ = setup
    event_model = {"id": "event-model", "kind": "external_model", "track": "event",
                   "name": "event", "available": True, "supported_target_kinds": ["event", "event_set"]}
    models.append(event_model)
    payload = {**request, "model_ids": ["quant-a", "event-model"], "request_id": "f" * 32}
    if explicit:
        payload.update(track="quant", target_scope="asset", frequency="1d")
    response = client.post("/api/arena/workspace", json=payload)
    assert response.status_code == 422
    assert "不能混选" in response.json()["detail"]


def test_event_target_rejects_quant_models(setup, monkeypatch):
    client, request, _models, *_ = setup
    target = {"id": "event:fixture", "kind": "event", "target_scope": "single_event",
              "track": "event", "name": "event", "symbol": "600000.SH", "market": "CN"}
    monkeypatch.setattr(subject.catalog_service, "catalog", lambda: {"models": [], "targets": [target]})
    monkeypatch.setattr(subject.catalog_service, "target_source_run_ids", lambda identity: [])
    response = client.post("/api/arena/workspace", json={
        **request, "track": "event", "target_scope": "single_event",
        "target_id": target["id"], "model_ids": ["quant-a", "quant-b"], "request_id": "d" * 32,
    })
    assert response.status_code == 422
    assert "不能混选" in response.json()["detail"]


def test_unavailable_event_set_is_rejected_before_model_resolution(setup, monkeypatch):
    client, request, *_ = setup
    target = {"id": "event-set:missing-oracle", "kind": "event_set", "target_scope": "event_set",
              "track": "event", "name": "missing", "available": False,
              "reason": "事件集缺少冻结 Oracle 标签"}
    monkeypatch.setattr(subject.catalog_service, "catalog", lambda: {"models": [], "targets": [target]})
    resolver = Mock()
    monkeypatch.setattr(subject.catalog_service, "resolve_model", resolver)
    response = client.post("/api/arena/workspace", json={
        **request, "track": "event", "target_scope": "event_set",
        "target_id": target["id"], "model_ids": ["event-a", "event-b"], "request_id": "c" * 32,
    })
    assert response.status_code == 422
    assert "冻结 Oracle" in response.json()["detail"]
    resolver.assert_not_called()


def test_event_set_worker_keeps_frozen_set_and_directional_returns(setup, monkeypatch):
    client, original, *_rest = setup
    events = [
        EventRecord(event_id="e1", market="CN", symbol="600000.SH",
                    event_time="2026-01-05T09:00:00+08:00", event_type_l2="notice",
                    title="first", event_text="facts", source_url="arena:test"),
        EventRecord(event_id="e2", market="US", symbol="AAPL",
                    event_time="2026-01-06T09:00:00-05:00", event_type_l2="earnings",
                    title="second", event_text="facts", source_url="arena:test"),
    ]
    target = {"id": "event-set:fixture", "kind": "event_set", "target_scope": "event_set",
              "track": "event", "name": "Fixture event set", "symbol": "MULTI",
              "market": "MULTI", "frequency": "1d"}
    models = [{"id": name, "name": name, "kind": "external_model", "track": "event",
               "available": True, "supported_target_kinds": ["event", "event_set"],
               "profile_snapshot": {"model_id": "fixture", "base_url": "https://fixture.invalid"}}
              for name in ("event-a", "event-b")]

    def case(event, direction, car):
        return {"target": {"id": f"event:{event.event_id}", "key": f"event:{event.event_id}",
                           "kind": "event", "market": event.market, "symbol": event.symbol},
                "event": event, "dataset": None, "history_dataset": None,
                "event_truth": {"horizons": {"t3": {"direction": direction,
                    "oracle_return": car, "asset_return": car, "benchmark_return": 0}}}}

    resolved = {"target": target, "event": None, "events": events,
                "event_cases": [case(events[0], "down", -.03), case(events[1], "up", .02)],
                "event_truth": None, "notes": [],
                "event_set_snapshot": {"snapshot_hash": "frozen", "labels_sha256": "labels"}}
    monkeypatch.setattr(subject.catalog_service, "catalog", lambda: {"models": models, "targets": [target]})
    monkeypatch.setattr(subject.catalog_service, "resolve_model",
                        lambda identity: copy.deepcopy(next(item for item in models if item["id"] == identity)))
    monkeypatch.setattr(subject.catalog_service, "resolve_target", lambda *args, **kwargs: resolved)
    monkeypatch.setattr(subject.catalog_service, "target_source_run_ids", lambda identity: [])
    monkeypatch.setattr(subject, "estimate_event_cases_work", lambda *args: 2)

    async def evaluate(model, cases, settings, cancel, progress):
        progress(2, 2)
        predictions = [
            {"event_id": "e1", "pred_direction": "down", "confidence": .8,
             "horizon": "t3", "run_id": model["id"], "abstain": False},
            {"event_id": "e2", "pred_direction": "up", "confidence": .7,
             "horizon": "t3", "run_id": model["id"], "abstain": False},
        ]
        return {"run_id": model["id"], "name": model["name"], "model_kind": model["kind"],
                "warnings": [], "observations": [
                    {"event_id": item["event_id"], "bar_index": 0,
                     "direction": item["pred_direction"], "horizon_bars": 3,
                     "direction_basis": "market_excess"} for item in predictions
                ], "predictions": predictions, "failed_prediction_count": 0}

    monkeypatch.setattr(subject, "evaluate_event_cases_model", evaluate)
    request = {**original, "track": "event", "target_scope": "event_set", "frequency": "1d",
               "model_ids": [item["id"] for item in models], "target_id": target["id"],
               "request_id": "e" * 32}
    response = client.post("/api/arena/workspace", json=request)
    assert response.status_code == 200, response.text
    aid = response.json()["id"]
    parsed = arena_workspace.WorkspaceRequest(**request).model_dump(mode="json")
    rules = response.json()["config"]["arena_workspace"]["rules"]
    subject._worker(aid, parsed, copy.deepcopy(models), rules, threading.Event())
    row = client.get("/api/arena/workspace/" + aid).json()
    assert row["status"] == "done"
    result = row["result"]
    assert result["track"] == "event" and len(result["events"]) == 2
    first = result["models"][0]["event_details"][0]
    assert first["direction"] == "down"
    assert first["actual_return"] == pytest.approx(-.03)
    assert first["directional_return"] == pytest.approx(.03)
    snapshot = setup[-1] / "data" / "arena_workspace" / aid / "input.json"
    saved = json.loads(snapshot.read_text())
    assert saved["event_cases"][0]["dataset"] is None
    assert saved["events"][1]["event_id"] == "e2"
