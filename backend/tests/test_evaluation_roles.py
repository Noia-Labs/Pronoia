"""Keep event contestants and Pronoia base-model evaluation roles distinct."""

from __future__ import annotations

import pytest
from pathlib import Path

from .test_model_lab import model_lab_env, _dataset, _profile, _wait_batch, _wait_worker_exit
from .test_event_experiments import event_env, _payload


def make_profile(client, *, name, model):
    profile = _profile(client, name=name)
    response = client.patch(f"/api/model-lab/profiles/{profile['id']}", json={"model_id": model})
    assert response.status_code == 200, response.text
    return response.json()


def test_event_pronoia_freezes_current_platform_and_never_accepts_candidate_override(event_env):
    from app import db
    from app.model_lab import repository as repo

    platform = make_profile(event_env, name="平台基模", model="platform-model-a")
    external = make_profile(event_env, name="外部选手", model="external-model-b")
    repo.set_default_profile(platform["id"])
    payload = _payload(event_env, qa=False)
    payload["prediction_profile_id"] = external["id"]
    rejected = event_env.post("/api/bt/event-experiments", json=payload)
    assert rejected.status_code == 422
    assert db.list_bt_runs() == []
    assert repo.list_batches() == []

    payload["prediction_profile_id"] = "__platform_default__"
    accepted = event_env.post("/api/bt/event-experiments", json=payload)
    assert accepted.status_code == 201, accepted.text
    run_id = accepted.json()["run"]["id"]
    event_env.patch(f"/api/model-lab/profiles/{platform['id']}", json={"model_id": "later-platform-model"})
    repo.set_default_profile(external["id"])
    run = db.get_bt_run(run_id)
    assert run["runner"] == "team_full"
    assert run["config"]["evaluation_role"] == "pronoia"
    assert run["config"]["model_profile_snapshot"]["id"] == platform["id"]
    assert run["config"]["model_profile_snapshot"]["model_id"] == "platform-model-a"
    assert run["model_version"] == "platform-model-a"
    assert repo.get_default_profile_id() == external["id"]


def test_event_pronoia_rejects_single_prompt_role(event_env):
    from app import db

    payload = _payload(event_env, qa=False)
    payload["prediction"]["runner"] = "team_prompt"
    response = event_env.post("/api/bt/event-experiments", json=payload)
    assert response.status_code == 422
    assert "统一多 Agent" in response.text
    assert db.list_bt_runs() == []


@pytest.mark.parametrize("selector", [None, "", "__platform_default__", "missing-profile"])
def test_event_raw_requires_explicit_existing_profile_without_platform_fallback(event_env, selector):
    from app import db
    from app.model_lab import repository as repo

    platform = make_profile(event_env, name="可用默认", model="platform-ready")
    repo.set_default_profile(platform["id"])
    payload = _payload(event_env, qa=False)
    payload["prediction"]["runner"] = "raw_model"
    payload["prediction_profile_id"] = selector
    response = event_env.post("/api/bt/event-experiments", json=payload)
    assert response.status_code in {404, 422}
    assert db.list_bt_runs() == []
    assert repo.get_default_profile_id() == platform["id"]


@pytest.mark.parametrize("unavailable", ["inactive", "missing-secret"])
def test_event_raw_unavailable_profile_does_not_fallback(event_env, monkeypatch, unavailable):
    from app import db
    from app.model_lab import repository as repo

    platform = make_profile(event_env, name="可用本方", model="platform-ready")
    external = make_profile(event_env, name="不可用外部", model="external")
    repo.set_default_profile(platform["id"])
    monkeypatch.delenv("PRONOIA_MODEL_SECRET_UNAVAILABLE_TEST", raising=False)
    patch = {"is_active": False} if unavailable == "inactive" else {"secret_env_ref": "PRONOIA_MODEL_SECRET_UNAVAILABLE_TEST"}
    assert event_env.patch(f"/api/model-lab/profiles/{external['id']}", json=patch).status_code == 200
    payload = _payload(event_env, qa=False)
    payload["prediction"]["runner"] = "raw_model"
    payload["prediction_profile_id"] = external["id"]
    rejected = event_env.post("/api/bt/event-experiments", json=payload)
    assert rejected.status_code == 409, rejected.text
    assert db.list_bt_runs() == []
    assert repo.get_default_profile_id() == platform["id"]


def test_event_raw_runtime_uses_frozen_external_profile_and_leaves_default_untouched(event_env, monkeypatch):
    from app import db, llm, model_endpoint_security
    from app.event_backtest import orchestrator
    from app.model_lab import repository as repo

    platform = make_profile(event_env, name="固定本方", model="platform-a")
    external = make_profile(event_env, name="外部连接", model="external-b")
    repo.set_default_profile(platform["id"])
    payload = _payload(event_env, qa=False)
    payload["prediction"]["runner"] = "raw_model"
    payload["prediction_profile_id"] = external["id"]
    created = event_env.post("/api/bt/event-experiments", json=payload)
    assert created.status_code == 201, created.text
    run = db.get_bt_run(created.json()["run"]["id"])
    assert run["config"]["evaluation_role"] == "raw_model"
    snapshot = run["config"]["model_profile_snapshot"]
    assert snapshot["id"] == external["id"]
    event_env.patch(f"/api/model-lab/profiles/{external['id']}", json={"model_id": "edited-after-freeze"})
    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", lambda *args, **kwargs: None)
    captured = []

    def execute_loaded(current_run, _concurrency):
        # raw_model has no Pronoia context injection; its runner consumes its
        # own frozen profile, while the ambient platform remains unchanged.
        captured.append(current_run["config"]["model_profile_snapshot"])
        assert llm.resolve_runtime_target().profile_id == platform["id"]

    monkeypatch.setattr(orchestrator, "_do_run_loaded", execute_loaded)
    orchestrator._do_run(run["id"], effective_concurrency=1)
    assert captured[0]["model_id"] == "external-b"
    assert repo.get_default_profile_id() == platform["id"]


@pytest.mark.parametrize("raw_selector", [None, "", "__platform_default__"])
def test_event_raw_qa_never_inherits_prediction_or_platform_profile(event_env, raw_selector):
    from app import db
    from app.model_lab import repository as repo

    external = make_profile(event_env, name="事件外部预测", model="raw-predictor")
    payload = _payload(event_env)
    payload["prediction"]["runner"] = "raw_model"
    payload["prediction_profile_id"] = external["id"]
    payload["qa"].update(variants=["raw"], candidate_profile_id=raw_selector)
    response = event_env.post("/api/bt/event-experiments", json=payload)
    assert response.status_code == 422
    assert db.list_bt_runs() == []
    assert repo.list_batches() == []


def test_event_qa_each_variant_uses_its_own_frozen_profile_at_runtime(event_env, monkeypatch):
    from app import llm, model_endpoint_security
    from app.agents import team
    from app.model_lab import repository as repo, providers, service

    platform = make_profile(event_env, name="Pronoia基模A", model="platform-a")
    raw = make_profile(event_env, name="Raw独立B", model="raw-b")
    later = make_profile(event_env, name="下一次默认C", model="later-c")
    repo.set_default_profile(platform["id"])
    payload = _payload(event_env)
    payload["qa"].update(variants=["pronoia", "raw"], candidate_profile_id=raw["id"], question_ids=["V02"])
    response = event_env.post("/api/bt/event-experiments", json=payload)
    assert response.status_code == 201, response.text
    batch_id = response.json()["qa_batch_id"]
    tasks = repo.get_batch(batch_id, include_tasks=True)["tasks"]
    frozen = {task["config"]["variants"][0]: task["config"]["profile_snapshot"] for task in tasks}
    assert len(tasks) == 2
    assert frozen["pronoia"]["id"] == platform["id"]
    assert frozen["raw"]["id"] == raw["id"]
    repo.set_default_profile(later["id"])
    event_env.patch(f"/api/model-lab/profiles/{raw['id']}", json={"model_id": "raw-edited-later"})
    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", lambda *a, **k: None)
    calls = []

    async def fake_team(_prompt, _history, state, _artifacts):
        target = llm.resolve_runtime_target()
        calls.append(("pronoia", target.profile_id, target.model_id))
        state["content"] = "Pronoia multi-agent answer"
        yield {"type": "done"}

    def fake_chat(profile, _messages):
        calls.append(("raw", profile["id"], profile["model_id"]))
        return {"answer": "Independent raw answer", "usage": {}, "latency_ms": 1}

    monkeypatch.setattr(team, "run_team", fake_team)
    monkeypatch.setattr(providers, "call_chat", fake_chat)
    assert service.start_batch(batch_id)[0]
    _wait_batch(event_env, batch_id)
    _wait_worker_exit(batch_id)
    assert sorted(calls) == sorted([("pronoia", platform["id"], "platform-a"), ("raw", raw["id"], "raw-b")])
    assert repo.get_default_profile_id() == later["id"]
    results = event_env.get(f"/api/model-lab/backtest-runs/{response.json()['run']['id']}/results")
    assert {row["variant"] for row in results.json()["results"]["qa_results"]} == {"pronoia", "raw"}
    assert "candidate-secret-must-not-leak" not in results.text
    assert "secret_env_ref" not in results.text


@pytest.mark.parametrize("forged_role", ["pronoia", "raw_model", "pronoia_base_model"])
def test_event_external_service_cannot_forge_its_role(event_env, forged_role):
    from app import db

    payload = _payload(event_env, qa=False)
    payload["prediction_profile_id"] = None
    payload["prediction"].pop("runner")
    payload["prediction"]["strategy_spec"] = {"type": "event", "adapter": "external_http", "endpoint": "https://independent.example/predict"}
    payload["prediction"]["config"] = {"evaluation_role": forged_role}
    response = event_env.post("/api/bt/event-experiments", json=payload)
    assert response.status_code == 422
    assert db.list_bt_runs() == []


@pytest.mark.parametrize("invalid", ["team_prompt", "raw_qa", "both_qa"])
def test_model_lab_refuses_alternative_pipelines(event_env, invalid):
    from app import db
    from app.model_lab import repository as repo

    profile = make_profile(event_env, name="候选基模", model="candidate")
    data = _dataset(event_env)
    payload = {
        "name": "固定Pronoia流程", "profile_ids": [profile["id"]], "auto_start": False,
        "prediction": {"enabled": True, "dataset_id": data["id"], "runner": "team_full"},
        "qa": {"enabled": True, "question_set_id": "pronoia-finance-comprehensive-v1", "question_ids": ["V02"], "variants": ["pronoia"], "scoring_mode": "manual"},
    }
    if invalid == "team_prompt":
        payload["prediction"]["runner"] = "team_prompt"
    else:
        payload["qa"]["variants"] = ["raw"] if invalid == "raw_qa" else ["pronoia", "raw"]
    rejected = event_env.post("/api/model-lab/batches", json=payload)
    assert rejected.status_code == 422
    assert repo.list_batches() == []
    assert db.list_bt_runs() == []


def test_model_lab_runs_each_frozen_base_through_same_pronoia_without_setting_default(event_env, monkeypatch):
    from app import db, llm, model_endpoint_security
    from app.agents import team
    from app.event_backtest import orchestrator
    from app.model_lab import repository as repo, service

    platform = make_profile(event_env, name="本方默认", model="platform-fixed")
    candidates = [make_profile(event_env, name=f"候选{index}", model=f"candidate-{index}") for index in (1, 2)]
    repo.set_default_profile(platform["id"])
    data = _dataset(event_env)
    response = event_env.post("/api/model-lab/batches", json={
        "name": "同一Pronoia换基模", "profile_ids": [item["id"] for item in candidates], "auto_start": False,
        "prediction": {"enabled": True, "dataset_id": data["id"]},
        "qa": {"enabled": True, "question_set_id": "pronoia-finance-comprehensive-v1", "question_ids": ["V02"], "scoring_mode": "manual"},
    })
    assert response.status_code == 201, response.text
    batch_id = response.json()["id"]
    assert repo.get_batch(batch_id)["scoring"]["source"] == "pronoia_base_evaluation"
    for candidate in candidates:
        assert event_env.patch(f"/api/model-lab/profiles/{candidate['id']}", json={"model_id": "edited-later"}).status_code == 200
    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", lambda *a, **k: None)
    prediction_calls, qa_calls = [], []

    def loaded(run, _concurrency):
        target = llm.resolve_runtime_target()
        assert run["runner"] == "team_full"
        assert run["config"]["evaluation_role"] == "pronoia_base_model"
        assert run["config"]["evaluation_source"] == "model_lab"
        prediction_calls.append((target.profile_id, target.model_id))
        db.update_bt_run_progress(run["id"], done_events=1, total_events=1)
        db.update_bt_run_status(run["id"], "done")

    def start(run_id):
        orchestrator._do_run(run_id, effective_concurrency=1)
        return orchestrator.BacktestStartResult(ok=True, run_id=run_id)

    async def fake_team(_prompt, _history, state, _artifacts):
        target = llm.resolve_runtime_target()
        qa_calls.append((target.profile_id, target.model_id))
        state["content"] = "Same Pronoia pipeline"
        yield {"type": "done"}

    async def forbidden_raw(*args, **kwargs):
        raise AssertionError("基模评测不能调用Raw问答")

    monkeypatch.setattr(orchestrator, "_do_run_loaded", loaded)
    monkeypatch.setattr(orchestrator, "start_bt_run", start)
    monkeypatch.setattr(team, "run_team", fake_team)
    monkeypatch.setattr(service, "_raw_answer", forbidden_raw)
    assert service.start_batch(batch_id)[0]
    finished = _wait_batch(event_env, batch_id)
    _wait_worker_exit(batch_id)
    assert finished["status"] == "done"
    expected = sorted((item["id"], item["model_id"]) for item in candidates)
    assert sorted(prediction_calls) == sorted(qa_calls) == expected
    assert repo.get_default_profile_id() == platform["id"]
    assert llm.resolve_runtime_target().model_id == "platform-fixed"
    results = event_env.get(f"/api/model-lab/batches/{batch_id}/results")
    assert {row["variant"] for row in results.json()["qa_results"]} == {"pronoia"}
    assert "candidate-secret-must-not-leak" not in results.text


@pytest.mark.parametrize("stale_cache", [False, True], ids=["no-cache", "empty-cache-from-running"])
def test_batch_results_computes_return_forecast_without_visiting_run_detail(event_env, tmp_path, stale_cache):
    from app import db
    from app.event_backtest.application import load_events, write_jsonl
    from app.event_backtest.models import EventLabel, TeamPrediction
    from app.model_lab import repository as repo
    from app.routes.backtest import create_run_from_trusted_request
    from app.schemas import CreateBacktestRunRequest

    profile = make_profile(event_env, name="汇总基模", model="summary-model")
    dataset = _dataset(event_env)
    event = load_events(db.get_bt_dataset(dataset["id"])["path"])[0]
    labels_path = tmp_path / "frozen-labels.jsonl"
    write_jsonl(labels_path, [EventLabel(event.event_id, "up", "up", "up", ret_t3=.01).to_dict()])
    db.update_bt_dataset_labels_path(dataset["id"], str(labels_path))
    batch_response = event_env.post("/api/model-lab/batches", json={
        "name": "直接读取批次成绩", "profile_ids": [profile["id"]], "auto_start": False,
        "prediction": {"enabled": True, "dataset_id": dataset["id"]}, "qa": {"enabled": False},
    })
    assert batch_response.status_code == 201, batch_response.text
    batch = batch_response.json()
    task = repo.get_task(batch["tasks"][0]["id"])
    # Model Lab assembles snapshots on the server. Public clients must not
    # author snapshots, so build this frozen-result fixture through the same
    # trusted entry point used by the batch worker.
    created_run = create_run_from_trusted_request(CreateBacktestRunRequest(**{
        "name": "冻结数值结果", "runner": "team_full", "dataset_id": dataset["id"],
        "horizon": "t3", "visibility": "arena_safe",
        "config": {"model_lab_batch_id": batch["id"], "model_lab_task_id": task["id"],
                   "model_profile_snapshot": task["config"]["profile_snapshot"]},
    }))
    run = db.get_bt_run(created_run["id"])
    if stale_cache:
        db.update_bt_run_status(run["id"], "running")
        db.update_bt_run_metrics(run["id"], {"return_forecast": {
            "value": None,
            "meta": {"status": "not_provided", "coverage": 0, "evaluated_count": 0, "mae_pct": None},
        }})
    write_jsonl(Path(run["out_path"]), [TeamPrediction(
        event.event_id, "up", run_id=run["id"], confidence=.8, rationale="Frozen facts",
        horizon="t3", expected_return_pct=2,
    ).to_dict()])
    db.update_bt_run_progress(run["id"], done_events=1, total_events=1)
    db.update_bt_run_status(run["id"], "done")
    repo.update_task(task["id"], status="done", backtest_run_id=run["id"], result_summary={
        "backtest_run_id": run["id"], "arena_eligible": True, "demo": False, "metrics": None,
    })
    repo.update_batch_status(batch["id"], "done")
    cached = db.get_bt_run(run["id"]).get("metrics")
    if stale_cache:
        assert cached["return_forecast"]["meta"]["coverage"] == 0
        assert cached["return_forecast"]["value"] is None
    else:
        assert not cached
    # First read is the batch summary itself; no /bt/runs/:id or metrics call.
    result = event_env.get(f"/api/model-lab/batches/{batch['id']}/results")
    assert result.status_code == 200, result.text
    forecast = result.json()["summary"]["prediction_runs"][0]["metrics"]["return_forecast"]
    assert forecast["value"] == pytest.approx(1)
    assert forecast["meta"]["evaluated_count"] == 1
    assert forecast["meta"]["mae_pct"] == pytest.approx(1)
    assert forecast["meta"]["coverage"] == 1
    assert forecast["meta"]["unit"] == "percentage_points"
    assert db.get_bt_run(run["id"])["metrics"]["return_forecast"]["value"] == pytest.approx(1)


def test_missing_historical_metric_files_do_not_crash_summary(event_env, monkeypatch):
    from app.model_lab import service
    from app.routes import backtest

    def unavailable(_run_id):
        raise FileNotFoundError("test frozen labels unavailable")

    monkeypatch.setattr(backtest, "get_run_metrics", unavailable)
    summary = service._prediction_summary({"id": "missing-historical-files", "status": "done", "metrics": None})
    assert summary["status"] == "done"
    assert summary["metrics"] is None
