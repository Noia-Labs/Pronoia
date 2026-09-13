"""Catalog removals persist without removing connections or test evidence."""
from copy import deepcopy

import pytest

from app import db
from app import model_registry as registry
from app.event_backtest import arena_workspace_catalog as catalog
from .test_model_registry import quant_payload, setup
from .test_model_lab import model_lab_env, _profile
from .test_event_experiments import event_env, _payload


def test_deleted_profile_variant_stays_removed_from_lists_and_arena(setup):
    client, runs, profiles, _ = setup
    runs.append({"id": "historical-raw", "runner": "raw_model", "config": {"model_profile_id": "qwen"}})
    before = deepcopy(profiles)
    response = client.post("/api/bt/models/delete", json={"model_ids": ["raw:qwen", "raw:qwen"]})
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted_ids": ["raw:qwen"]}
    assert "raw:qwen" not in {item["id"] for item in client.get("/api/bt/models").json()["items"]}
    assert "raw:qwen" not in {item["id"] for item in catalog.catalog()["models"]}
    assert {"pronoia:qwen", "pronoia:platform"} <= {item["id"] for item in catalog.catalog()["models"]}
    assert client.get("/api/bt/models/raw:qwen/setup").status_code == 404
    with pytest.raises(ValueError):
        catalog.resolve_model("raw:qwen")
    assert profiles == before
    assert len(runs) == 1
    # A new run or a fresh schema initialization must not recreate it.
    runs.append({"id": "later-raw", "runner": "raw_model", "config": {"model_profile_id": "qwen"}})
    db.init_db()
    assert "raw:qwen" not in {item["id"] for item in client.get("/api/bt/models").json()["items"]}
    assert client.post("/api/bt/models/delete", json={"model_ids": ["raw:qwen"]}).json() == response.json()


def test_deleted_run_derived_quant_model_does_not_return_and_can_be_saved_again(setup):
    client, runs, _, _ = setup
    runs.append({"id": "historical-quant", "engine_mode": "portfolio", "status": "done",
                 "strategy_spec": quant_payload()["strategy_spec"], "config": {}})
    before = deepcopy(runs)
    identity = client.get("/api/bt/models?category=quant").json()["items"][0]["id"]
    assert client.post("/api/bt/models/delete", json={"model_ids": [identity]}).status_code == 200
    assert client.get("/api/bt/models?category=quant").json()["items"] == []
    assert identity not in {model["id"] for model in catalog.catalog()["models"]}
    restored = client.post("/api/bt/models", json=quant_payload())
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == identity and restored.json()["run_ids"] == ["historical-quant"]
    assert runs == before


@pytest.mark.parametrize("kind,identity,profile_id", [
    ("external_model", "raw:qwen", "qwen"),
    ("pronoia", "pronoia:qwen", "qwen"),
    ("pronoia", "pronoia:platform", "__platform_default__"),
])
def test_saving_removed_automatic_and_persisted_profiles_restores_only_selected_identity(setup, kind, identity, profile_id):
    client, _, _, _ = setup
    all_ids = {model["id"] for model in client.get("/api/bt/models").json()["items"]}
    assert client.post("/api/bt/models/delete", json={"model_ids": sorted(all_ids)}).status_code == 200
    payload = {"name": "Added again", "category": "event", "kind": kind, "profile_id": profile_id}
    restored = client.post("/api/bt/models", json=payload)
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == identity
    assert {model["id"] for model in client.get("/api/bt/models").json()["items"]} == {identity}
    assert client.post("/api/bt/models/delete", json={"model_ids": [identity]}).status_code == 200
    payload["name"] = "Restored saved definition"
    restored_again = client.post("/api/bt/models", json=payload)
    assert restored_again.status_code == 200, restored_again.text
    assert restored_again.json()["name"] == payload["name"]


def test_missing_connection_cannot_be_restored_from_stale_saved_definition(setup):
    client, _, profiles, _ = setup
    payload = {"name": "Raw", "category": "event", "kind": "external_model", "profile_id": "qwen"}
    assert client.post("/api/bt/models", json=payload).status_code == 200
    assert client.post("/api/bt/models/delete", json={"model_ids": ["raw:qwen"]}).status_code == 200
    profiles.clear()
    assert client.post("/api/bt/models", json=payload).status_code == 422
    assert "raw:qwen" in registry._deleted_model_ids()


def test_bulk_delete_validates_entire_batch_before_mutating_and_is_idempotent(setup):
    client, _, _, _ = setup
    initial = client.get("/api/bt/models").json()
    response = client.post("/api/bt/models/delete", json={"model_ids": ["raw:qwen", "missing:model"]})
    assert response.status_code == 404, response.text
    assert client.get("/api/bt/models").json() == initial
    assert registry._deleted_model_ids() == set()
    assert client.post("/api/bt/models/delete", json={"model_ids": ["raw:qwen"]}).status_code == 200
    assert client.post("/api/bt/models/delete", json={"model_ids": ["pronoia:qwen", "raw:qwen"]}).json() == {
        "deleted_ids": ["pronoia:qwen", "raw:qwen"]}
    assert registry._deleted_model_ids() == {"raw:qwen", "pronoia:qwen"}


@pytest.mark.parametrize("payload", [{}, {"model_ids": []}, {"model_ids": ["raw:qwen"] * 101},
    {"model_ids": "raw:qwen"}, {"model_ids": [123]}, {"model_ids": [""]},
    {"model_ids": ["   "]}, {"model_ids": ["x" * 201]}, {"model_ids": ["raw:qwen"], "delete_history": True}])
def test_delete_endpoint_rejects_invalid_selection_without_changes(setup, payload):
    client, _, _, _ = setup
    assert client.post("/api/bt/models/delete", json=payload).status_code == 422
    assert registry._deleted_model_ids() == set()


def test_deleting_catalog_entries_preserves_real_history_api_profile_and_platform_default(event_env):
    from app.model_lab import repository as repo
    client = event_env
    profile = _profile(client)
    repo.set_default_profile(profile["id"])
    identity = "pronoia:" + profile["id"]
    payload = _payload(client)
    payload["prediction"]["config"] = {"saved_model_id": identity}
    result = client.post("/api/bt/event-experiments", json=payload)
    assert result.status_code == 201, result.text
    run_id = result.json()["run"]["id"]
    batch_id = result.json()["qa_batch_id"]
    task = repo.get_batch(batch_id, include_tasks=True)["tasks"][0]
    repo.create_qa_result(batch_id=batch_id, task_id=task["id"], profile_id=profile["id"],
                         question_id=task["config"]["questions"][0]["id"], variant="pronoia", repeat_no=1)
    tables = ("bt_runs", "bt_predictions", "ml_batches", "ml_tasks", "ml_qa_results",
              "ml_event_experiments", "ml_model_profiles", "ml_settings", "ml_questions")
    def evidence():
        with db._lock:
            return {table: [tuple(row) for row in db._get_conn().execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in tables}
    before = evidence()
    response = client.post("/api/bt/models/delete", json={"model_ids": [identity, "pronoia:platform"]})
    assert response.status_code == 200, response.text
    assert evidence() == before
    assert db.get_bt_run(run_id)["config"]["saved_model_id"] == identity
    assert repo.get_default_profile_id() == profile["id"]
    linked_results = client.get(f"/api/model-lab/backtest-runs/{run_id}/results")
    assert linked_results.status_code == 200 and linked_results.json()["batch_id"] == batch_id
    assert "raw:" + profile["id"] in {model["id"] for model in client.get("/api/bt/models").json()["items"]}
