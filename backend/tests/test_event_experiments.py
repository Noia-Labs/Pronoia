"""Unified event/QA creation, platform fallback and independent lifecycle."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .test_model_lab import model_lab_env, _dataset, _profile, _wait_batch, _wait_worker_exit


@pytest.fixture()
def event_env(model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app import config
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://platform.example.invalid/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "platform-frozen-model")
    monkeypatch.setattr(config, "LLM_API_KEY", "trusted-platform-key-never-persist")
    return model_lab_env


def _payload(client: TestClient, *, qa: bool = True, auto_start: bool = False) -> dict[str, Any]:
    dataset = _dataset(client)
    return {
        "prediction": {"name": "事件与问答实验", "runner": "team_full", "dataset_id": dataset["id"]},
        "prediction_profile_id": "__platform_default__",
        "auto_start": auto_start,
        "qa": {"enabled": qa, "question_set_id": "pronoia-finance-comprehensive-v1",
               "question_ids": ["V02", "Q05"], "variants": ["pronoia"], "scoring_mode": "manual"},
    }


def _hold_prediction(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False):
    from app import db
    from app.event_backtest import orchestrator as orch
    started = []

    def start(run_id: str):
        started.append(run_id)
        if fail:
            return orch.BacktestStartResult(ok=False, run_id=run_id, error="controlled prediction failure")
        db.update_bt_run_status(run_id, "running")
        return orch.BacktestStartResult(ok=True, run_id=run_id)

    monkeypatch.setattr(orch, "start_bt_run", start)
    return started


def test_environment_default_is_server_owned_frozen_and_hidden(event_env: TestClient):
    from app import db, config
    from app.model_lab import repository as repo
    from app.model_lab.providers import resolve_profile_secret

    client = event_env
    capability = client.get("/api/model-lab/event-capabilities")
    assert capability.status_code == 200
    assert capability.json()["platform_default"]["available"] is True
    assert capability.json()["platform_default"]["model_id"] == "platform-frozen-model"
    assert config.LLM_API_KEY not in capability.text
    assert repo.list_profiles() == []

    created = client.post("/api/bt/event-experiments", json=_payload(client))
    assert created.status_code == 201, created.text
    result = created.json()
    assert result["started"] is False
    assert result["run"]["status"] == "pending"
    assert result["qa_batch_id"]
    assert len(db.list_bt_runs()) == 1
    snapshot = db.get_bt_run(result["run"]["id"])["config"]["model_profile_snapshot"]
    assert snapshot["base_url"] == "https://platform.example.invalid/v1"
    assert snapshot["model_id"] == "platform-frozen-model"
    assert resolve_profile_secret(snapshot) == config.LLM_API_KEY
    assert config.LLM_API_KEY not in created.text
    assert "secret_env_ref" not in created.text
    assert client.get("/api/model-lab/profiles").json()["items"] == []
    for action in (
        client.get(f"/api/model-lab/profiles/{snapshot['id']}"),
        client.patch(f"/api/model-lab/profiles/{snapshot['id']}", json={"model_id": "tampered"}),
        client.delete(f"/api/model-lab/profiles/{snapshot['id']}"),
        client.post(f"/api/model-lab/profiles/{snapshot['id']}/set-default"),
    ):
        assert action.status_code == 404, action.text
    with db._lock:
        for table in ("ml_model_profiles", "bt_runs", "ml_batches", "ml_tasks"):
            rows = [dict(row) for row in db._get_conn().execute(f"SELECT * FROM {table}").fetchall()]
            assert config.LLM_API_KEY not in json.dumps(rows)
    linked = client.get(f"/api/model-lab/backtest-runs/{result['run']['id']}/results").json()
    assert linked["enabled"] is True
    assert linked["batch_id"] == result["qa_batch_id"]
    tasks = linked["results"]["batch"]["tasks"]
    assert [task["kind"] for task in tasks] == ["qa"]
    assert [question["code"] for question in tasks[0]["config"]["questions"]] == ["V02", "Q05"]


def test_arena_safe_event_experiment_create_returns_only_public_run_contract(event_env: TestClient):
    payload = _payload(event_env, qa=False)
    payload["prediction"]["visibility"] = "arena_safe"
    payload["prediction"]["config"] = {"private_note": "SECRET_EXPERIMENT_CONFIG"}
    payload["prediction"]["execution_spec"] = {
        "start_date": "2025-01-10", "end_date": "2025-01-10",
    }

    response = event_env.post("/api/bt/event-experiments", json=payload)

    assert response.status_code == 201, response.text
    run = response.json()["run"]
    assert run["visibility"] == "arena_safe"
    assert run["strategy_spec"] is None
    assert run["execution_spec"] is None
    assert run["labels_path"] is None
    assert run["result_path"] is None
    assert set(run["config"]) == {"arena_eligibility", "arena_comparison"}
    assert "SECRET_EXPERIMENT_CONFIG" not in response.text
    assert "2025-01-10" not in response.text


def test_platform_secret_cannot_follow_a_changed_or_forged_target(event_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app import config
    from app.model_lab.platform_default import environment_profile
    from app.model_lab.providers import ModelProviderError, resolve_profile_secret
    snapshot = environment_profile()
    forged = {**snapshot, "base_url": "https://attacker.example.invalid/v1"}
    with pytest.raises(ModelProviderError):
        resolve_profile_secret(forged)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://new-platform.example.invalid/v1")
    with pytest.raises(ModelProviderError):
        resolve_profile_secret(snapshot)


def test_pronoia_answer_uses_trusted_platform_snapshot_without_registered_profile(event_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app import config, llm, model_endpoint_security
    from app.agents import team
    from app.model_lab import service
    from app.model_lab.platform_default import environment_profile
    captured = []
    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", lambda *_args, **_kwargs: None)

    async def fake_team(prompt, _history, state, _artifacts):
        target = llm.resolve_runtime_target()
        assert target.api_key == config.LLM_API_KEY
        assert target.model_id == "platform-frozen-model"
        assert target.base_url == "https://platform.example.invalid/v1"
        captured.append(prompt)
        state["content"] = "actual team-path answer"
        state["tool_trace"] = [{"name": "controlled-evidence-tool"}]
        yield {"type": "done"}

    monkeypatch.setattr(team, "run_team", fake_team)
    result = asyncio.run(service._pronoia_answer(environment_profile(), {"prompt": "default platform question"}))
    assert result["answer"] == "actual team-path answer"
    assert result["tool_trace"] == [{"name": "controlled-evidence-tool"}]
    assert captured == ["default platform question"]


def test_platform_selector_prefers_registered_default(event_env: TestClient):
    from app import db
    from app.model_lab import repository as repo
    client = event_env
    selected = _profile(client)
    repo.set_default_profile(selected["id"])
    response = client.post("/api/bt/event-experiments", json=_payload(client, qa=False))
    assert response.status_code == 201, response.text
    snapshot = db.get_bt_run(response.json()["run"]["id"])["config"]["model_profile_snapshot"]
    assert snapshot["id"] == selected["id"]
    assert client.get("/api/model-lab/event-capabilities").json()["platform_default"]["model_id"] == selected["model_id"]


@pytest.mark.parametrize("failure", ["question", "candidate", "judge", "snapshot", "dataset", "empty-selection"])
def test_invalid_optional_qa_or_prediction_creates_no_experiment_state(event_env: TestClient, failure: str):
    from app import db
    client = event_env
    payload = _payload(client)
    if failure == "question":
        payload["qa"]["question_ids"] = ["missing-question"]
    elif failure == "candidate":
        payload["qa"].update(variants=["raw"], candidate_profile_id="missing-profile")
    elif failure == "judge":
        payload["qa"].update(scoring_mode="auto", judge_profile_id="missing-judge")
    elif failure == "snapshot":
        payload["prediction"]["config"] = {"model_profile_snapshot": {"base_url": "https://attacker.invalid", "api_key": "pasted-sensitive-input"}}
    elif failure == "dataset":
        payload["prediction"]["dataset_id"] = "missing-dataset"
    else:
        payload["qa"]["question_ids"] = []
    response = client.post("/api/bt/event-experiments", json=payload)
    assert response.status_code in {404, 409, 422}, response.text
    assert "pasted-sensitive-input" not in response.text
    with db._lock:
        for table in ("bt_runs", "ml_batches", "ml_tasks", "ml_event_experiments", "ml_model_profiles"):
            assert db._get_conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_atomic_event_creation_rolls_back_run_and_platform_identity(event_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app import db
    from app.model_lab import repository as repo
    payload = _payload(event_env)

    def fail_batch(**_kwargs):
        raise RuntimeError("controlled batch insertion failure")

    monkeypatch.setattr(repo, "create_batch", fail_batch)
    with pytest.raises(RuntimeError, match="controlled batch insertion failure"):
        event_env.post("/api/bt/event-experiments", json=payload)
    with db._lock:
        for table in ("bt_runs", "ml_batches", "ml_model_profiles", "ml_event_experiments"):
            assert db._get_conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_auto_start_publish_window_cannot_be_deleted(event_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from app import db
    from app.event_backtest import orchestrator as orch
    from app.model_lab import service

    payload = _payload(event_env, qa=False, auto_start=True)
    start_entered = Event()
    release_start = Event()
    delete_entered = Event()

    def held_start(run_id: str):
        start_entered.set()
        assert release_start.wait(5), "test did not release auto-start"
        db.update_bt_run_status(run_id, "running")
        return orch.BacktestStartResult(ok=True, run_id=run_id)

    original_delete = service.delete_history_records

    def traced_delete(*args, **kwargs):
        delete_entered.set()
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(orch, "start_bt_run", held_start)
    monkeypatch.setattr(service, "delete_history_records", traced_delete)

    with ThreadPoolExecutor(max_workers=2) as pool:
        create_future = pool.submit(event_env.post, "/api/bt/event-experiments", json=payload)
        assert start_entered.wait(5), "auto-start was not reached"
        run_id = str(db.list_bt_runs()[0]["id"])
        delete_future = pool.submit(
            event_env.post, "/api/bt/history/delete", json={"run_ids": [run_id], "batch_ids": []},
        )
        assert delete_entered.wait(5), "delete endpoint was not reached"
        with pytest.raises(TimeoutError):
            delete_future.result(timeout=0.1)
        release_start.set()
        created = create_future.result(timeout=5)
        deleted = delete_future.result(timeout=5)

    assert created.status_code == 201, created.text
    assert created.json()["run"]["id"] == run_id
    assert deleted.status_code == 409, deleted.text
    assert db.get_bt_run(run_id)["status"] == "running"


@pytest.mark.parametrize("prediction_fails", [False, True])
def test_prediction_and_qa_finish_independently_and_preserve_answers(
    event_env: TestClient, monkeypatch: pytest.MonkeyPatch, prediction_fails: bool,
):
    from app import config, db
    from app.model_lab import service
    client = event_env
    starts = _hold_prediction(monkeypatch, fail=prediction_fails)

    async def answer(_profile, question):
        return {"answer": f"完整回答 {question['code']} {config.LLM_API_KEY}", "usage": {}, "latency_ms": 2, "tool_trace": []}

    monkeypatch.setattr(service, "_pronoia_answer", answer)
    response = client.post("/api/bt/event-experiments", json=_payload(client, auto_start=True))
    assert response.status_code == 201, response.text
    data = response.json()
    run_id, batch_id = data["run"]["id"], data["qa_batch_id"]
    _wait_batch(client, batch_id)
    _wait_worker_exit(batch_id)
    assert starts == [run_id]
    assert len(db.list_bt_runs()) == 1
    assert data["started"] is not prediction_fails
    assert ("prediction" in data["start_errors"]) is prediction_fails
    assert db.get_bt_run(run_id)["status"] == ("failed" if prediction_fails else "running")
    response = client.get(f"/api/model-lab/backtest-runs/{run_id}/results")
    assert config.LLM_API_KEY not in response.text
    results = response.json()["results"]["qa_results"]
    assert len(results) == 2
    assert {result["status"] for result in results} == {"awaiting_manual"}
    assert {result["question"]["code"] for result in results} == {"V02", "Q05"}
    assert all("[redacted]" in result["answer"] for result in results)
    with db._lock:
        assert all(config.LLM_API_KEY not in row[0] for row in db._get_conn().execute("SELECT answer FROM ml_qa_results").fetchall())


def test_saved_event_starts_linked_qa_once_using_existing_run_start(event_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app import db
    from app.model_lab import repository as repo, service
    client = event_env
    starts = _hold_prediction(monkeypatch)

    async def answer(_profile, _question):
        return {"answer": "saved experiment answer", "usage": {}, "tool_trace": [], "latency_ms": 1}

    monkeypatch.setattr(service, "_pronoia_answer", answer)
    data = client.post("/api/bt/event-experiments", json=_payload(client)).json()
    run_id, batch_id = data["run"]["id"], data["qa_batch_id"]
    assert repo.get_batch(batch_id)["status"] == "pending"
    assert client.post(f"/api/bt/runs/{run_id}/start").status_code == 200
    _wait_batch(client, batch_id)
    _wait_worker_exit(batch_id)
    assert client.post(f"/api/bt/runs/{run_id}/start").status_code == 200
    assert starts == [run_id]
    assert len(repo.list_batches()) == 1
    assert len(repo.list_qa_results(batch_id=batch_id)) == 2
    assert repo.get_event_experiment(run_id)["auto_start"] == 1
    assert db.get_bt_run(run_id)["status"] == "running"


def test_external_decision_endpoint_needs_separate_qa_profile(event_env: TestClient):
    client = event_env
    payload = _payload(client)
    payload["prediction_profile_id"] = None
    payload["prediction"].pop("runner")
    payload["prediction"]["strategy_spec"] = {"type": "event", "adapter": "external_http", "endpoint": "https://decision.example.invalid/predict"}
    payload["qa"]["variants"] = ["raw"]
    rejected = client.post("/api/bt/event-experiments", json=payload)
    assert rejected.status_code == 422, rejected.text
    assert "明确选择独立模型" in rejected.text
    payload["qa"]["candidate_profile_id"] = _profile(client)["id"]
    accepted = client.post("/api/bt/event-experiments", json=payload)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["run"]["runner"] == "event_external_http"


def test_explicit_raw_profile_is_frozen_without_modifying_platform_default(event_env: TestClient):
    from app import db
    from app.model_lab import repository as repo
    client = event_env
    selected = _profile(client)
    payload = _payload(client, qa=False)
    payload["prediction_profile_id"] = selected["id"]
    payload["prediction"]["runner"] = "raw_model"
    response = client.post("/api/bt/event-experiments", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["qa_batch_id"] is None
    run = db.get_bt_run(data["run"]["id"])
    assert run["config"]["model_profile_snapshot"]["id"] == selected["id"]
    assert run["model_version"] == selected["model_id"]
    assert repo.get_default_profile_id() is None
    detail = client.get(f"/api/model-lab/backtest-runs/{run['id']}/results").json()
    assert detail == {"enabled": False, "batch_id": None, "results": None}


def test_auto_judge_platform_fallback_keeps_answer_when_judge_fails(event_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app import config
    from app.model_lab import service, repository as repo
    client = event_env
    _hold_prediction(monkeypatch)

    async def answer(_profile, _question):
        return {"answer": "durable full answer", "usage": {}, "tool_trace": [], "latency_ms": 1}

    def fail_judge(*_args):
        raise RuntimeError(f"provider reflected {config.LLM_API_KEY}")

    monkeypatch.setattr(service, "_pronoia_answer", answer)
    monkeypatch.setattr(service, "score_answer", fail_judge)
    payload = _payload(client, auto_start=True)
    payload["qa"].update(scoring_mode="auto", judge_profile_id=None)
    data = client.post("/api/bt/event-experiments", json=payload).json()
    _wait_batch(client, data["qa_batch_id"])
    _wait_worker_exit(data["qa_batch_id"])
    rows = repo.list_qa_results(batch_id=data["qa_batch_id"])
    assert {row["status"] for row in rows} == {"partial"}
    assert all(row["answer"] == "durable full answer" for row in rows)
    assert all(config.LLM_API_KEY not in row["error_msg"] and "[redacted]" in row["error_msg"] for row in rows)


def test_qa_cancellation_does_not_cancel_prediction_or_restart_it_implicitly(event_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app import db
    from app.model_lab import repository as repo
    client = event_env
    starts = _hold_prediction(monkeypatch)
    data = client.post("/api/bt/event-experiments", json=_payload(client)).json()
    run_id, batch_id = data["run"]["id"], data["qa_batch_id"]
    cancelled = client.post(f"/api/model-lab/batches/{batch_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert db.get_bt_run(run_id)["status"] == "pending"
    assert client.post(f"/api/bt/runs/{run_id}/start").status_code == 200
    assert starts == [run_id]
    assert db.get_bt_run(run_id)["status"] == "running"
    assert repo.get_batch(batch_id)["status"] == "cancelled"
    assert repo.list_qa_results(batch_id=batch_id) == []


def test_recovery_reuses_run_and_respects_saved_or_terminal_states(event_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app import db
    from app.model_lab.event_experiments import recover_event_predictions
    client = event_env
    starts = _hold_prediction(monkeypatch)
    saved = client.post("/api/bt/event-experiments", json=_payload(client, qa=False)).json()
    assert recover_event_predictions() == []
    active = client.post("/api/bt/event-experiments", json=_payload(client, qa=False, auto_start=True)).json()
    run_id = active["run"]["id"]
    assert recover_event_predictions() == [run_id]
    assert starts == [run_id, run_id]
    assert len(db.list_bt_runs()) == 2
    assert db.get_bt_run(saved["run"]["id"])["status"] == "pending"
    for terminal in ("paused", "done", "failed", "cancelled"):
        db.update_bt_run_status(run_id, terminal)
        assert recover_event_predictions() == []


def test_private_http_is_only_enabled_by_existing_admin_opt_in(event_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    client = event_env
    payload = _payload(client, qa=False)
    payload["prediction_profile_id"] = None
    payload["prediction"].pop("runner")
    payload["prediction"]["strategy_spec"] = {"type": "event", "adapter": "external_http", "endpoint": "http://127.0.0.1:9000/predict"}
    monkeypatch.delenv("PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS", raising=False)
    assert client.get("/api/model-lab/event-capabilities").json()["allow_private_model_endpoints"] is False
    assert client.post("/api/bt/event-experiments", json=payload).status_code == 422
    monkeypatch.setenv("PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS", "1")
    assert client.get("/api/model-lab/event-capabilities").json()["allow_private_model_endpoints"] is True
    accepted = client.post("/api/bt/event-experiments", json=payload)
    assert accepted.status_code == 201, accepted.text


def test_existing_model_lab_run_link_is_visible_in_event_results(event_env: TestClient):
    from app import db
    client = event_env
    profile = _profile(client)
    payload = _payload(client)
    created_batch = client.post("/api/model-lab/batches", json={
        "name": "旧 Model Lab 批次", "profile_ids": [profile["id"]], "auto_start": False,
        "prediction": {"enabled": False}, "qa": payload["qa"],
    })
    assert created_batch.status_code == 201, created_batch.text
    batch_id = created_batch.json()["id"]
    run = client.post("/api/bt/runs", json={
        **payload["prediction"], "config": {"model_lab_batch_id": batch_id},
    }).json()
    assert db.get_bt_run(run["id"])
    linked = client.get(f"/api/model-lab/backtest-runs/{run['id']}/results").json()
    assert linked["enabled"] is True
    assert linked["batch_id"] == batch_id
