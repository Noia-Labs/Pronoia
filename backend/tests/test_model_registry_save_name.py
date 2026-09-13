"""An existing model changes its display name, never its definition or old runs."""
from copy import deepcopy

import pytest

from app import db, model_registry as registry
from .test_model_registry import setup, quant_payload


@pytest.mark.parametrize("kind,profile_id,identity", [
    ("external_model", "qwen", "raw:qwen"),
    ("pronoia", "qwen", "pronoia:qwen"),
    ("pronoia", "__platform_default__", "pronoia:platform"),
])
def test_profile_existing_card_is_renamed_in_place(setup, monkeypatch, kind, profile_id, identity):
    client, runs, _, _ = setup
    payload = {"name": "test", "category": "event", "kind": kind, "profile_id": profile_id}
    old_catalog = client.get("/api/bt/models?category=event").json()["items"]
    old_model = next(item for item in old_catalog if item["id"] == identity)
    preview = client.post("/api/bt/models/match", json=payload)
    assert preview.status_code == 200 and preview.json()["model"]["name"] == old_model["name"]
    assert registry._rows() == []  # Preview does not materialize virtual cards.
    first = client.post("/api/bt/models", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["id"] == identity and first.json()["name"] == "test"
    assert first.json()["save_disposition"] == "renamed"
    assert len(client.get("/api/bt/models?category=event").json()["items"]) == len(old_catalog)
    runs.append({"id": "old-run", "name": "Original experiment", "config": {"saved_model_id": identity}})
    old_runs = deepcopy(runs)
    row = next(item for item in registry._rows() if item["id"] == identity)
    monkeypatch.setattr(db, "now_iso", lambda: "2030-01-01T00:00:00")
    second = client.post("/api/bt/models", json={**payload, "name": "renamed twice"}).json()
    after = next(item for item in registry._rows() if item["id"] == identity)
    assert second["save_disposition"] == "renamed" and second["id"] == identity
    assert after["definition"] == row["definition"] and after["created_at"] == row["created_at"]
    assert after["updated_at"] == "2030-01-01T00:00:00"
    assert runs == old_runs
    assert client.post("/api/bt/models", json={**payload, "name": "renamed twice"}).json()["save_disposition"] == "existing"


def test_quant_match_and_save_use_same_identity_without_replacing_definition(setup):
    client, _, _, _ = setup
    payload = quant_payload()
    assert client.post("/api/bt/models/match", json=payload).json() == {"model": None}
    assert registry._rows() == []
    first = client.post("/api/bt/models", json=payload).json()
    assert first["save_disposition"] == "created"
    definition = deepcopy(registry._rows()[0]["definition"])
    changed = deepcopy(payload)
    changed["name"] = "new display name"
    changed["strategy_spec"].update(start_date="2025-01-01", name="ignored spec name")
    changed["strategy_spec"]["parameters"]["horizon_bars"] = 7
    assert client.post("/api/bt/models/match", json=changed).json()["model"]["id"] == first["id"]
    second = client.post("/api/bt/models", json=changed).json()
    assert second["name"] == changed["name"] and second["save_disposition"] == "renamed"
    assert registry._rows()[0]["definition"] == definition


def test_match_respects_edited_identity_and_tombstones(setup):
    client, _, _, _ = setup
    original = quant_payload()
    first = client.post("/api/bt/models", json=original).json()
    edited = deepcopy(original)
    edited["strategy_spec"]["parameters"]["long_window"] = 8
    response = client.put(f"/api/bt/models/{first['id']}/configuration", json={"name": first["name"], "strategy_spec": edited["strategy_spec"]})
    assert response.status_code == 200, response.text
    assert client.post("/api/bt/models/match", json=original).json() == {"model": None}
    assert client.post("/api/bt/models/match", json=edited).json()["model"]["id"] == first["id"]
    created = client.post("/api/bt/models", json=original).json()
    assert created["id"] != first["id"] and created["save_disposition"] == "created"
    assert client.post("/api/bt/models/delete", json={"model_ids": [first["id"]]}).status_code == 200
    assert client.post("/api/bt/models/match", json=edited).json() == {"model": None}
    restored = client.post("/api/bt/models", json=edited).json()
    assert restored["id"] == first["id"] and restored["save_disposition"] == "restored"
