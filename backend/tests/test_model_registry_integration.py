"""Existing test endpoints bind saved configurations without changing defaults."""
from .test_model_lab import model_lab_env, _profile
from .test_event_experiments import event_env, _payload


def test_saved_pronoia_variant_controls_prediction_and_qa_without_global_change(event_env):
    from app import db
    from app.model_lab import repository as repo
    client = event_env
    selected = _profile(client, name="Qwen variant")
    saved = client.post("/api/bt/models", json={"name": "Pronoia Qwen", "category": "event", "kind": "pronoia", "profile_id": selected["id"]})
    assert saved.status_code == 200, saved.text
    payload = _payload(client)
    payload["prediction"]["config"] = {"saved_model_id": saved.json()["id"]}
    # A caller cannot override the saved model by selecting another connection
    # or supplying a different runner in the test form.
    payload["prediction_profile_id"] = "__platform_default__"
    payload["prediction"]["runner"] = "raw_model"
    created = client.post("/api/bt/event-experiments", json=payload)
    assert created.status_code == 201, created.text
    row = db.get_bt_run(created.json()["run"]["id"])
    assert row["runner"] == "team_full"
    assert row["config"]["model_profile_snapshot"]["id"] == selected["id"]
    batch = repo.get_batch(created.json()["qa_batch_id"], include_tasks=True)
    assert batch["tasks"][0]["config"]["profile_snapshot"]["id"] == selected["id"]
    assert repo.get_default_profile_id() is None
    assert client.get("/api/model-lab/event-capabilities").json()["platform_default"]["model_id"] == "platform-frozen-model"
    assert client.get(f"/api/bt/models/{saved.json()['id']}").json()["run_ids"] == [row["id"]]


def test_saved_raw_model_preserves_explicit_qa_candidate(event_env):
    from app import db
    from app.model_lab import repository as repo
    client = event_env
    prediction = _profile(client, name="Predictor")
    answering = _profile(client, name="Separate answering model")
    payload = _payload(client)
    payload["prediction"]["config"] = {"saved_model_id": "raw:" + prediction["id"]}
    payload["qa"].update(variants=["raw"], candidate_profile_id=answering["id"])
    created = client.post("/api/bt/event-experiments", json=payload)
    assert created.status_code == 201, created.text
    row = db.get_bt_run(created.json()["run"]["id"])
    assert row["runner"] == "raw_model" and row["config"]["model_profile_snapshot"]["id"] == prediction["id"]
    task = repo.get_batch(created.json()["qa_batch_id"], include_tasks=True)["tasks"][0]
    assert task["profile_id"] == answering["id"]


def test_direct_test_endpoint_rejects_client_authored_snapshot_even_with_saved_id(event_env):
    client = event_env
    payload = _payload(client, qa=False)["prediction"]
    payload["config"] = {"saved_model_id": "pronoia:platform", "model_profile_snapshot": {"base_url": "https://attacker.example/v1"}}
    assert client.post("/api/bt/runs", json=payload).status_code == 422
    assert client.post("/api/bt/event-experiments", json={"prediction": payload, "auto_start": False}).status_code == 422


def test_qa_only_batches_are_linked_to_both_profile_execution_variants(event_env):
    from app.model_lab import repository as repo
    client = event_env
    profile = _profile(client)
    batch = repo.create_batch(name="QA only", profile_ids=[profile["id"]], dataset_id=None,
        dataset_version=None, question_set_id=None, prediction={"enabled": False},
        qa={"enabled": True}, scoring={},
        task_specs=[{"kind": "qa", "profile_id": profile["id"], "config": {"variants": ["pronoia", "raw"]}}])
    items = client.get("/api/bt/models?category=event").json()["items"]
    by_id = {item["id"]: item for item in items}
    assert batch["id"] in by_id["pronoia:" + profile["id"]]["batch_ids"]
    assert batch["id"] in by_id["raw:" + profile["id"]]["batch_ids"]
