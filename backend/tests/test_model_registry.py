"""Model definitions persist without a run; tests retain independent inputs."""
from copy import deepcopy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import model_registry as registry


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from app import config, db
    from app.event_backtest import arena_workspace_catalog as catalog
    from app.routes.model_registry import router
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "registry.sqlite"))
    db.init_db()
    profiles = [{"id": "qwen", "name": "Qwen connection", "provider": "Qwen", "model_id": "qwen-test",
                 "base_url": "https://api.example.com/v1", "secret_env_ref": "PRONOIA_MODEL_SECRET_TEST",
                 "is_active": True, "secret_configured": True}]
    monkeypatch.setattr(catalog.profiles, "list_profiles", lambda **kw: profiles)
    monkeypatch.setattr(catalog.profiles, "get_profile", lambda profile_id, **kw: next((profile for profile in profiles if profile["id"] == profile_id), None))
    monkeypatch.setattr(catalog.profiles, "get_default_profile", lambda **kw: profiles[0])
    runs = []
    monkeypatch.setattr(db, "list_bt_runs", lambda **kw: runs)
    monkeypatch.setattr(db, "list_bt_datasets", lambda: [])
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, runs, profiles, tmp_path
    db._conn.close()


def quant_payload(**changes):
    return {"name": "MA model", "category": "quant", "strategy_spec": {
        "type": "quant", "adapter": "builtin", "kind": "ma_cross", "parameters": {"short_window": 2, "long_window": 5},
        **changes}}


def test_saving_without_any_run_appears_in_same_arena_model_catalog(setup):
    from app.event_backtest.arena_workspace_catalog import catalog, resolve_model
    client, runs, _, _ = setup
    response = client.post("/api/bt/models", json=quant_payload())
    assert response.status_code == 200, response.text
    model = response.json()
    assert model["run_count"] == 0 and model["run_ids"] == [] and model["can_test"]
    assert runs == []
    assert model["id"] in {m["id"] for m in catalog()["models"]}
    assert resolve_model(model["id"])["strategy_spec"]["parameters"]["long_window"] == 5
    assert client.get("/api/bt/models", params={"category": "quant"}).json()["items"][0]["id"] == model["id"]


def test_same_algorithm_different_names_data_dates_costs_and_horizons_deduplicate(setup):
    client, _, _, _ = setup
    first = client.post("/api/bt/models", json=quant_payload()).json()
    changed = quant_payload(dataset_id="other-data", start_date="2020-01-01", end_date="2025-01-01",
        market="US", symbol="SPY", benchmark="QQQ", execution_spec={"fee_bps": 99})
    changed["name"] = "Renamed experiment"
    changed["strategy_spec"]["parameters"].update(horizon_bars=7, fee_bps=1, initial_capital=1000)
    second = client.post("/api/bt/models", json=changed)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first["id"]
    assert second.json()["name"] == "Renamed experiment"
    assert second.json()["save_disposition"] == "renamed"
    assert len(client.get("/api/bt/models?category=quant").json()["items"]) == 1
    stored = registry.execution_setup(first["id"])["strategy_spec"]
    assert not {"start_date", "dataset_id", "execution_spec"}.intersection(stored)


def test_algorithm_parameter_change_creates_new_model(setup):
    client, _, _, _ = setup
    one = client.post("/api/bt/models", json=quant_payload()).json()
    second = quant_payload()
    second["strategy_spec"]["parameters"]["long_window"] = 8
    two = client.post("/api/bt/models", json=second).json()
    assert one["id"] != two["id"]


def test_rule_confirmation_counts_persist_and_default_one_keeps_model_identity(setup):
    client, _, _, _ = setup
    payload = {"name": "Volume confirmations", "category": "quant", "strategy_spec": {
        "type": "quant", "adapter": "builtin", "kind": "declarative_rules", "parameters": {
            "entry": {"conditions": [{"field": "volume_ratio", "operator": "above", "lookback": 20, "threshold": 1.5}]},
            "exit": {"conditions": [{"field": "volume_ratio", "operator": "below", "lookback": 20, "threshold": 0.8}]},
        },
    }}
    baseline = client.post("/api/bt/models", json=payload)
    assert baseline.status_code == 200, baseline.text
    explicit_default = deepcopy(payload)
    for phase in ("entry", "exit"):
        explicit_default["strategy_spec"]["parameters"][phase]["conditions"][0]["consecutive_count"] = 1
    default_response = client.post("/api/bt/models", json=explicit_default)
    assert default_response.status_code == 200, default_response.text
    assert default_response.json()["id"] == baseline.json()["id"]

    confirmed = deepcopy(payload)
    confirmed["strategy_spec"]["parameters"]["entry"]["conditions"][0]["consecutive_count"] = 3
    confirmed["strategy_spec"]["parameters"]["exit"]["conditions"][0]["consecutive_count"] = 2
    saved = client.post("/api/bt/models", json=confirmed)
    assert saved.status_code == 200, saved.text
    assert saved.json()["id"] != baseline.json()["id"]
    stored = registry.execution_setup(saved.json()["id"])["strategy_spec"]["parameters"]
    assert stored["entry"]["conditions"][0]["consecutive_count"] == 3
    assert stored["exit"]["conditions"][0]["consecutive_count"] == 2
    from app.event_backtest.arena_workspace_catalog import resolve_model
    arena_rules = resolve_model(saved.json()["id"])["strategy_spec"]["parameters"]
    assert arena_rules["entry"]["conditions"][0]["consecutive_count"] == 3
    assert arena_rules["exit"]["conditions"][0]["consecutive_count"] == 2


def test_rename_persists_without_new_model_or_history_mutation(setup):
    client, _, _, _ = setup
    one = client.post("/api/bt/models", json=quant_payload()).json()
    response = client.patch(f"/api/bt/models/{one['id']}", json={"name": "Research MA"})
    assert response.status_code == 200 and response.json()["name"] == "Research MA"
    assert len(client.get("/api/bt/models?category=quant").json()["items"]) == 1


def test_existing_runs_group_by_model_and_keep_test_identity(setup):
    client, runs, _, _ = setup
    payload = quant_payload()
    runs.extend([{"id": str(index), "name": f"Test {index}", "status": "done", "created_at": f"2026-09-0{index+1}",
                  "engine_mode": "portfolio", "strategy_spec": {**payload["strategy_spec"], "name": f"Experiment {index}", "start_date": f"202{index}-01-01"},
                  "config": {}} for index in range(3)])
    before = deepcopy(runs)
    items = client.get("/api/bt/models?category=quant").json()["items"]
    assert len(items) == 1 and items[0]["run_count"] == 3
    model = client.post("/api/bt/models", json=payload).json()
    assert model["id"] == items[0]["id"]
    assert runs == before


def test_profile_variants_and_platform_are_distinct_stable_models(setup):
    client, runs, _, _ = setup
    for index, runner in enumerate(["raw_model", "raw_model", "team_full"]):
        runs.append({"id": str(index), "runner": runner, "config": {"model_profile_snapshot": {"id": "qwen"}}})
    models = client.get("/api/bt/models?category=event").json()["items"]
    by_id = {m["id"]: m for m in models}
    assert by_id["raw:qwen"]["run_count"] == 2
    assert by_id["pronoia:qwen"]["run_count"] == 1
    response = client.post("/api/bt/models", json={"name": "Pronoia Qwen", "category": "event", "kind": "pronoia", "profile_id": "qwen"})
    assert response.json()["id"] == "pronoia:qwen"
    assert client.get("/api/bt/models/pronoia:qwen/setup").json()["profile_id"] == "qwen"
    assert "PRONOIA_MODEL_SECRET_TEST" not in json.dumps(models)


def test_quant_test_binding_locks_model_rules_but_allows_selected_horizon(setup):
    from app.schemas import CreateBacktestRunRequest
    client, _, _, _ = setup
    payload = {"name": "Mean predictor", "category": "quant", "strategy_spec": {"type": "quant", "adapter": "builtin", "kind": "return_forecast", "parameters": {"lookback": 20, "horizon_bars": 3}}}
    model = client.post("/api/bt/models", json=payload).json()
    request = CreateBacktestRunRequest(name="New data", dataset_id="new-dataset", config={"saved_model_id": model["id"]},
        strategy_spec={"type": "quant", "kind": "return_forecast", "parameters": {"lookback": 999, "horizon_bars": 7}},
        execution_spec={"start_date": "2026-01-01", "fee_bps": 10})
    bound = registry.bind_test_request(request)
    assert bound.strategy_spec.parameters == {"lookback": 20, "horizon_bars": 7}
    assert bound.dataset_id == "new-dataset" and bound.execution_spec == request.execution_spec
    assert registry.execution_setup(model["id"])["strategy_spec"]["parameters"] == {"lookback": 20}


def test_saved_external_service_setup_hides_refs_but_server_binding_retains_them(setup):
    from app.schemas import CreateBacktestRunRequest
    client, _, _, _ = setup
    payload = {"name": "Financial service", "category": "event", "kind": "external_service",
               "strategy_spec": {"type": "event", "adapter": "external_http", "endpoint": "https://example.com/predict",
                                 "headers": {"Authorization": "env:PRONOIA_STRATEGY_SECRET_TEST"}}}
    response = client.post("/api/bt/models", json=payload)
    assert response.status_code == 200, response.text
    model = response.json()
    response = client.get(f"/api/bt/models/{model['id']}/setup")
    assert "SECRET_TEST" not in response.text and "Authorization" not in response.text
    request = CreateBacktestRunRequest(name="Test", config={"saved_model_id": model["id"]},
        strategy_spec={"type": "event", "adapter": "external_http", "endpoint": "https://attacker.example/predict", "headers": {}})
    bound = registry.bind_test_request(request)
    assert bound.strategy_spec.endpoint == "https://example.com/predict"
    assert bound.strategy_spec.headers["Authorization"] == "env:PRONOIA_STRATEGY_SECRET_TEST"


def test_service_model_parameter_is_part_of_identity_and_execution(setup):
    client, _, _, _ = setup
    spec = {"type": "event", "adapter": "external_http", "endpoint": "https://example.com/predict",
            "parameters": {"model_id": "finance-a"}}
    first = client.post("/api/bt/models", json={"name": "A", "category": "event", "strategy_spec": spec}).json()
    second = client.post("/api/bt/models", json={"name": "B", "category": "event",
        "strategy_spec": {**spec, "parameters": {"model_id": "finance-b"}}}).json()
    assert first["id"] != second["id"]
    assert registry.execution_setup(first["id"])["strategy_spec"]["parameters"]["model_id"] == "finance-a"


def test_imported_quant_file_is_replaceable_test_input(setup):
    from app.schemas import CreateBacktestRunRequest
    client, _, _, tmp_path = setup
    source = tmp_path / "first.csv"
    source.write_text("timestamp,target_weight\n2026-01-01,1\n2026-01-02,0\n")
    saved = client.post("/api/bt/models", json={"name": "Imported financial model", "category": "quant",
        "strategy_spec": {"type": "quant", "kind": "signal_file", "path": str(source)}})
    assert saved.status_code == 200, saved.text
    request = CreateBacktestRunRequest(name="Different data test", dataset_id="other-data",
        config={"saved_model_id": saved.json()["id"]},
        strategy_spec={"type": "quant", "kind": "buy_hold", "path": str(tmp_path / "second.csv")})
    bound = registry.bind_test_request(request)
    assert bound.strategy_spec.kind == "signal_file"
    assert bound.strategy_spec.path == str(tmp_path / "second.csv")
    assert registry.execution_setup(saved.json()["id"])["strategy_spec"]["path"] == str(source)


def test_import_model_can_be_saved_before_selecting_data_or_file(setup):
    client, _, _, _ = setup
    response = client.post("/api/bt/models", json={"name": "Uploaded predictor", "category": "event", "kind": "imported_predictions",
        "strategy_spec": {"type": "event", "adapter": "imported_decisions", "runner": "provided_analysis"}})
    assert response.status_code == 200, response.text
    assert response.json()["can_test"] and not response.json()["available"]
    assert client.get(f"/api/bt/models/{response.json()['id']}/setup").status_code == 200


def test_unrelated_imported_models_are_distinct_and_retry_identity_is_stable(setup):
    client, _, _, _ = setup
    payload = {"name": "Financial model A", "category": "event", "kind": "imported_predictions", "model_identity": "a" * 32,
        "strategy_spec": {"type": "event", "adapter": "imported_decisions", "runner": "provided_analysis"}}
    first = client.post("/api/bt/models", json=payload).json()
    retry = client.post("/api/bt/models", json=payload).json()
    other = client.post("/api/bt/models", json={**payload, "name": "Financial model B", "model_identity": "b" * 32}).json()
    assert first["id"] == retry["id"] != other["id"]
    renamed = client.patch(f"/api/bt/models/{first['id']}", json={"name": "Financial model A renamed"}).json()
    assert renamed["id"] == first["id"] and renamed["name"] == "Financial model A renamed"


def test_multiple_import_test_datasets_share_saved_model_identity(setup):
    from app.event_backtest.arena_workspace_catalog import resolve_model
    client, runs, _, tmp_path = setup
    spec = {"type": "event", "adapter": "imported_decisions", "runner": "provided_analysis"}
    model = client.post("/api/bt/models", json={"name": "Financial model", "category": "event", "kind": "imported_predictions", "strategy_spec": spec}).json()
    for index in range(2):
        path = tmp_path / f"pred-{index}.jsonl"
        path.write_text('{"event_id":"one","pred_direction":"up"}\n')
        runs.append({"id": f"r-{index}", "name": f"Different object {index}", "status": "done", "visibility": "private", "runner": "provided_analysis",
                     "strategy_spec": spec, "events_path": f"different-event-{index}", "out_path": str(path), "config": {"saved_model_id": model["id"]}})
    listed = client.get(f"/api/bt/models/{model['id']}").json()
    assert listed["run_count"] == 2
    assert resolve_model(model["id"])["source_run_ids"] == ["r-0", "r-1"]


def test_invalid_quant_parameters_are_rejected_without_creating_model(setup):
    client, _, _, _ = setup
    payload = quant_payload()
    payload["strategy_spec"]["parameters"] = {"short_window": 9, "long_window": 2}
    assert client.post("/api/bt/models", json=payload).status_code == 422
    assert client.get("/api/bt/models?category=quant").json()["items"] == []


def test_direct_key_in_service_spec_is_rejected(setup):
    client, _, _, _ = setup
    response = client.post("/api/bt/models", json={"name": "Bad key", "category": "event", "strategy_spec": {
        "type": "event", "adapter": "external_http", "endpoint": "https://example.com/predict", "headers": {"Authorization": "Bearer sk-not-a-reference"}}})
    assert response.status_code == 422
