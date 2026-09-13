"""Configuration editing changes future execution while keeping model history."""
from copy import deepcopy

import pytest

from app import db, model_registry as registry
from app.event_backtest import arena_workspace_catalog as catalog
from .test_model_registry import quant_payload, setup
from .test_model_lab import model_lab_env, _profile
from .test_event_experiments import event_env


def test_quant_configuration_update_keeps_identity_and_updates_arena_and_test_binding(setup):
    from app.schemas import CreateBacktestRunRequest
    client, _, _, _ = setup
    model = client.post("/api/bt/models", json=quant_payload()).json()
    spec = deepcopy(model["setup"]["strategy_spec"])
    spec["parameters"]["long_window"] = 12
    updated = client.put(f"/api/bt/models/{model['id']}/configuration", json={"name": "Updated MA", "strategy_spec": spec})
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == model["id"] and updated.json()["name"] == "Updated MA"
    assert client.get(f"/api/bt/models/{model['id']}/configuration").json() == updated.json()
    assert catalog.resolve_model(model["id"])["strategy_spec"]["parameters"]["long_window"] == 12
    request = CreateBacktestRunRequest(name="New run", config={"saved_model_id": model["id"]})
    assert registry.bind_test_request(request).strategy_spec.parameters["long_window"] == 12
    same = client.post("/api/bt/models", json={"name": "Same edited rules", "category": "quant", "strategy_spec": spec})
    assert same.status_code == 200 and same.json()["id"] == model["id"]
    original = client.post("/api/bt/models", json=quant_payload())
    assert original.status_code == 200 and original.json()["id"] != model["id"]
    assert registry.execution_setup(model["id"])["strategy_spec"]["parameters"]["long_window"] == 12
    assert registry.execution_setup(original.json()["id"])["strategy_spec"]["parameters"]["long_window"] == 5
    assert len(client.get("/api/bt/models?category=quant").json()["items"]) == 2


@pytest.mark.parametrize("bad_spec", [
    {"type": "quant", "kind": "ma_cross", "parameters": {"short_window": 5, "long_window": 2}},
    {"type": "quant", "kind": "momentum", "parameters": {"lookback": 3}},
    {"type": "event", "adapter": "imported_decisions", "runner": "provided_analysis"},
])
def test_invalid_or_cross_kind_configuration_does_not_partially_update(setup, bad_spec):
    client, _, _, _ = setup
    model = client.post("/api/bt/models", json=quant_payload()).json()
    before = registry._rows()
    result = client.put(f"/api/bt/models/{model['id']}/configuration", json={"name": "Should not persist", "strategy_spec": bad_spec})
    assert result.status_code == 422, result.text
    assert registry._rows() == before


@pytest.mark.parametrize("kind,parameters,weight_field", [
    ("ma_cross", {"short_window": 2, "long_window": 5, "short_weight": -1}, "short_weight"),
    ("momentum", {"lookback": 20, "negative_weight": -1}, "negative_weight"),
])
@pytest.mark.parametrize("invalid_weight", [None, {}, [], "not-a-number"])
def test_invalid_quant_weight_returns_validation_error_without_changing_configuration(setup, kind, parameters, weight_field, invalid_weight):
    client, _, _, _ = setup
    created = client.post("/api/bt/models", json={"name": "Valid weights", "category": "quant",
        "strategy_spec": {"type": "quant", "kind": kind, "parameters": parameters}})
    assert created.status_code == 200, created.text
    model = created.json()
    changed = deepcopy(model["setup"]["strategy_spec"])
    changed["parameters"][weight_field] = invalid_weight
    before = registry._rows()
    response = client.put(f"/api/bt/models/{model['id']}/configuration", json={
        "name": "Should not persist", "strategy_spec": changed,
    })
    assert response.status_code == 422, response.text
    assert registry._rows() == before
    assert client.get(f"/api/bt/models/{model['id']}/configuration").json() == {key: value for key, value in model.items() if key != "save_disposition"}


def test_edit_rejects_collision_with_another_saved_configuration(setup):
    client, _, _, _ = setup
    first = client.post("/api/bt/models", json=quant_payload()).json()
    other_payload = quant_payload()
    other_payload["strategy_spec"]["parameters"]["long_window"] = 10
    second = client.post("/api/bt/models", json=other_payload).json()
    before = registry._rows()
    response = client.put(f"/api/bt/models/{first['id']}/configuration", json={"name": "Conflict", "strategy_spec": second["setup"]["strategy_spec"]})
    assert response.status_code == 422
    assert registry._rows() == before


def test_edit_rejects_collision_with_another_run_derived_configuration(setup):
    client, runs, _, _ = setup
    first = client.post("/api/bt/models", json=quant_payload()).json()
    other_payload = quant_payload()
    other_payload["strategy_spec"]["parameters"]["long_window"] = 10
    runs.append({"id": "other", "engine_mode": "portfolio", "status": "done", "config": {},
                 "strategy_spec": other_payload["strategy_spec"]})
    before = registry._rows()
    response = client.put(f"/api/bt/models/{first['id']}/configuration", json={"name": "Conflict", "strategy_spec": other_payload["strategy_spec"]})
    assert response.status_code == 422
    assert registry._rows() == before


def test_external_configuration_retains_auth_and_requires_explicit_auth_on_new_endpoint(setup):
    client, _, _, _ = setup
    model = client.post("/api/bt/models", json={"name": "Service", "category": "event", "strategy_spec": {
        "type": "event", "adapter": "external_http", "endpoint": "https://example.com/signal",
        "timeout_seconds": 30, "headers": {"Authorization": "env:PRONOIA_STRATEGY_SECRET_TEST"},
    }}).json()
    public = client.get(f"/api/bt/models/{model['id']}/configuration")
    assert "headers" not in public.text and "SECRET_TEST" not in public.text
    spec = public.json()["setup"]["strategy_spec"]
    spec["timeout_seconds"] = 90
    response = client.put(f"/api/bt/models/{model['id']}/configuration", json={"name": "Service updated", "strategy_spec": spec})
    assert response.status_code == 200, response.text
    server_spec = registry.execution_setup(model["id"])["strategy_spec"]
    assert server_spec["headers"] == {"Authorization": "env:PRONOIA_STRATEGY_SECRET_TEST"}
    assert server_spec["timeout_seconds"] == 90
    spec["endpoint"] = "https://other.example/signal"
    before = registry._rows()
    rejected = client.put(f"/api/bt/models/{model['id']}/configuration", json={"name": "Unsafe reuse", "strategy_spec": spec})
    assert rejected.status_code == 422 and "重新设置 API 认证" in rejected.text
    assert registry._rows() == before
    spec["headers"] = {}
    accepted = client.put(f"/api/bt/models/{model['id']}/configuration", json={"name": "Unauthenticated service", "strategy_spec": spec})
    assert accepted.status_code == 200, accepted.text
    assert registry.execution_setup(model["id"])["strategy_spec"]["headers"] == {}
    spec["timeout_seconds"] = -1
    assert client.put(f"/api/bt/models/{model['id']}/configuration", json={"name": "Invalid timeout", "strategy_spec": spec}).status_code == 422


def test_profile_configuration_edit_is_name_only_and_does_not_modify_shared_connection(setup):
    client, _, profiles, _ = setup
    before = deepcopy(profiles)
    response = client.put("/api/bt/models/raw:qwen/configuration", json={"name": "Raw display name"})
    assert response.status_code == 200 and response.json()["name"] == "Raw display name"
    assert client.get("/api/bt/models/pronoia:qwen").json()["name"] != "Raw display name"
    assert profiles == before
    invalid = client.put("/api/bt/models/raw:qwen/configuration", json={"name": "Invalid", "strategy_spec": quant_payload()["strategy_spec"]})
    assert invalid.status_code == 422 and profiles == before
    assert client.put("/api/bt/models/pronoia:platform/configuration", json={"name": "Platform display name"}).status_code == 200


def test_cannot_edit_deleted_model_until_it_is_explicitly_added_again(setup):
    client, _, _, _ = setup
    client.post("/api/bt/models/delete", json={"model_ids": ["raw:qwen"]})
    assert client.get("/api/bt/models/raw:qwen/configuration").status_code == 404
    assert client.put("/api/bt/models/raw:qwen/configuration", json={"name": "Invisible edit"}).status_code == 404
    assert "raw:qwen" in registry._deleted_model_ids()


def test_editing_run_derived_quant_locks_history_identity_without_changing_frozen_strategy(event_env):
    client = event_env
    old_spec = {"type": "quant", "kind": "ma_cross", "parameters": {"short_window": 2, "long_window": 5}}
    run = db.create_bt_run(name="Original", runner="ma_cross", events_path="unused.csv", out_path="result.json",
                           engine_mode="portfolio", strategy_type="quant", strategy_spec=old_spec,
                           config={"important_snapshot": {"benchmark": "original"}})
    model = client.get("/api/bt/models?category=quant").json()["items"][0]
    new_spec = deepcopy(old_spec)
    new_spec["parameters"]["long_window"] = 12
    edited = client.put(f"/api/bt/models/{model['id']}/configuration", json={"name": "Edited", "strategy_spec": new_spec})
    assert edited.status_code == 200, edited.text
    historical = db.get_bt_run(run["id"])
    assert historical["strategy_spec"] == run["strategy_spec"]
    assert historical["config"] == {**run["config"], "saved_model_id": model["id"]}
    assert historical["updated_at"] == run["updated_at"]
    old_again = client.post("/api/bt/models", json={"name": "Original algorithm again", "category": "quant", "strategy_spec": old_spec})
    assert old_again.status_code == 200 and old_again.json()["id"] != model["id"]
    assert client.get(f"/api/bt/models/{model['id']}").json()["run_ids"] == [run["id"]]
    assert old_again.json()["run_ids"] == []
    assert catalog.resolve_model(model["id"])["strategy_spec"]["parameters"]["long_window"] == 12


def test_existing_profile_endpoint_updates_both_execution_variants_without_altering_old_snapshot(event_env):
    from app.model_lab import repository as profiles
    client = event_env
    profile = _profile(client)
    profiles.set_default_profile(profile["id"])
    before = catalog.resolve_model("raw:" + profile["id"])["profile_snapshot"]
    response = client.patch("/api/model-lab/profiles/" + profile["id"], json={"model_id": "updated-model", "max_output_tokens": 12345})
    assert response.status_code == 200, response.text
    for identity in ("raw:" + profile["id"], "pronoia:" + profile["id"], "pronoia:platform"):
        latest = catalog.resolve_model(identity)["profile_snapshot"]
        assert latest["model_id"] == "updated-model" and latest["max_output_tokens"] == 12345
    assert before["model_id"] == "candidate-v1"


@pytest.mark.parametrize("updates", [
    {"base_url": "https://other.example/v1"},
    {"base_url": "http://example.com/v1"},
    {"base_url": "https://example.com:444/v1"},
    {"provider": "another-provider"},
    {"base_url": "https://other.example/v1", "api_key": "   "},
    {"base_url": "https://other.example/v1", "secret_env_ref": ""},
])
def test_profile_origin_or_provider_change_requires_explicit_new_auth(event_env, updates):
    from app.model_lab import repository as repo
    client = event_env
    created = client.post("/api/model-lab/profiles", json={
        "name": "Origin-bound", "provider": "vendor", "base_url": "https://example.com/v1",
        "model_id": "test", "secret_env_ref": "PRONOIA_MODEL_SECRET_CANDIDATE",
    })
    assert created.status_code == 201
    profile_id = created.json()["id"]
    before = repo.get_profile(profile_id, public=False)
    rejected = client.patch(f"/api/model-lab/profiles/{profile_id}", json=updates)
    assert rejected.status_code == 422, rejected.text
    assert "重新填写" in rejected.text or "HTTPS" in rejected.text
    assert repo.get_profile(profile_id, public=False) == before


def test_profile_same_origin_path_default_port_case_and_model_edits_keep_auth(event_env):
    from app.model_lab import repository as repo
    client = event_env
    created = client.post("/api/model-lab/profiles", json={
        "name": "Same origin", "provider": "vendor", "base_url": "https://example.com/v1",
        "model_id": "test", "secret_env_ref": "PRONOIA_MODEL_SECRET_CANDIDATE",
    })
    profile_id = created.json()["id"]
    updated = client.patch(f"/api/model-lab/profiles/{profile_id}", json={
        "base_url": "HTTPS://EXAMPLE.COM:443/v2/chat/completions/", "model_id": "new-model",
        "max_output_tokens": 32768, "api_key": "",
    })
    assert updated.status_code == 200, updated.text
    assert repo.get_profile(profile_id, public=False)["secret_env_ref"] == "PRONOIA_MODEL_SECRET_CANDIDATE"


@pytest.mark.parametrize("credential", [
    {"secret_env_ref": "PRONOIA_MODEL_SECRET_ROTATED"},
    {"api_key": "synthetic-credential-for-new-origin"},
])
def test_profile_origin_change_with_explicit_auth_keeps_old_snapshots_frozen(event_env, credential):
    from app.model_lab import repository as repo
    client = event_env
    profile = _profile(client)
    frozen = catalog.resolve_model("raw:" + profile["id"])["profile_snapshot"]
    previous_ref = repo.get_profile(profile["id"], public=False)["secret_env_ref"]
    updated = client.patch("/api/model-lab/profiles/" + profile["id"], json={
        "provider": "another-provider", "base_url": "https://other.example/v1", **credential,
    })
    assert updated.status_code == 200, updated.text
    assert repo.get_profile(profile["id"], public=False)["secret_env_ref"] != previous_ref
    assert frozen["base_url"] == "http://127.0.0.1:9/v1"
    assert frozen["secret_env_ref"] == previous_ref
    assert "synthetic-credential-for-new-origin" not in updated.text
