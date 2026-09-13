"""Saved provider connections drive real event runners through a mocked transport."""
from __future__ import annotations

import io
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .test_model_lab import model_lab_env


@pytest.mark.parametrize(
    ("provider", "base_url", "model_id", "completion_url"),
    [
        ("qwen", "https://qwen.example.invalid/compatible-mode/v1", "qwen-test", "https://qwen.example.invalid/compatible-mode/v1/chat/completions"),
        ("deepseek", "https://deepseek.example.invalid", "deepseek-test", "https://deepseek.example.invalid/chat/completions"),
        ("kimi", "https://kimi.example.invalid/v1/", "kimi-test", "https://kimi.example.invalid/v1/chat/completions"),
        ("doubao", "https://doubao.example.invalid/api/v3", "ep-deployment-test", "https://doubao.example.invalid/api/v3/chat/completions"),
        ("custom", "https://gateway.example.invalid/team/models/chat/completions", "private-model-2026", "https://gateway.example.invalid/team/models/chat/completions"),
    ],
    ids=["qwen", "deepseek", "kimi", "doubao", "custom-full-endpoint"],
)
def test_saved_provider_executes_frozen_endpoint_after_default_and_profile_change(
    model_lab_env: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    base_url: str,
    model_id: str,
    completion_url: str,
) -> None:
    from app import config, db, llm, model_endpoint_security
    from app.event_backtest import orchestrator
    from app.event_backtest.application import load_predictions
    from app.model_lab import providers, repository

    # Use only synthetic credentials and an isolated database from model_lab_env.
    secret_ref = "PRONOIA_MODEL_SECRET_MULTI_PROVIDER_TEST"
    monkeypatch.setenv(secret_ref, "synthetic-multi-provider-credential")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://platform.example.invalid/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "platform-should-not-be-called")
    monkeypatch.setattr(config, "LLM_API_KEY", "synthetic-platform-credential")
    checked_urls: list[str] = []
    requests: list[Any] = []

    def check_endpoint(url: str, **_kwargs: Any) -> None:
        checked_urls.append(url)

    def completion_response(request: Any) -> io.BytesIO:
        requests.append(request)
        return io.BytesIO(json.dumps({
            "id": "mock-completion", "object": "chat.completion", "created": 0,
            "model": model_id,
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps({
                    "pred_direction": "up", "confidence": 0.72,
                    "rationale": "Based only on the supplied historical announcement.",
                    "expected_return_pct": 2.5,
                })},
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        }).encode())

    class MockOpener:
        def open(self, request: Any, *, timeout: float) -> io.BytesIO:
            return completion_response(request)

    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", check_endpoint)
    monkeypatch.setattr(providers, "_runtime_validate", check_endpoint)
    monkeypatch.setattr(providers, "build_opener", lambda *_handlers: MockOpener())
    client = model_lab_env

    selected_response = client.post("/api/model-lab/profiles", json={
        "name": f"{provider} custom connection", "provider": provider,
        "base_url": base_url, "model_id": model_id, "secret_env_ref": secret_ref,
    })
    assert selected_response.status_code == 201, selected_response.text
    selected = selected_response.json()
    assert selected["provider"] == provider
    assert selected["secret_configured"] is True

    dataset_response = client.post("/api/bt/datasets/manual", json={
        "name": "Provider transport fixture",
        "events": [{
            "market": "CN", "symbol": "600000",
            "event_time": "2025-01-10T09:00:00+08:00",
            "available_time": "2025-01-10T09:05:00+08:00",
            "event_type_l2": "业绩公告", "title": "Historical company announcement",
            "event_text": "The company announced an increase in its reported earnings.",
            "source_url": "https://announcements.example.invalid/2025/01/10",
        }],
    })
    assert dataset_response.status_code == 200, dataset_response.text
    experiment_response = client.post("/api/bt/event-experiments", json={
        "prediction": {
            "name": f"Frozen {provider} prediction", "runner": "raw_model",
            "dataset_id": dataset_response.json()["id"],
        },
        "prediction_profile_id": selected["id"], "auto_start": False,
        "qa": {"enabled": False},
    })
    assert experiment_response.status_code == 201, experiment_response.text
    run_id = experiment_response.json()["run"]["id"]
    frozen = db.get_bt_run(run_id)["config"]["model_profile_snapshot"]
    assert frozen["provider"] == provider
    assert frozen["base_url"] == base_url.rstrip("/")
    assert frozen["model_id"] == model_id

    replacement_response = client.post("/api/model-lab/profiles", json={
        "name": "New platform default", "provider": "custom",
        "base_url": "https://replacement.example.invalid/v1",
        "model_id": "replacement-default", "secret_env_ref": secret_ref,
    })
    assert replacement_response.status_code == 201, replacement_response.text
    replacement_id = replacement_response.json()["id"]
    default_response = client.post(f"/api/model-lab/profiles/{replacement_id}/set-default")
    assert default_response.status_code == 200, default_response.text
    edited_response = client.patch(f"/api/model-lab/profiles/{selected['id']}", json={
        "base_url": "https://edited.example.invalid/v2", "model_id": "edited-after-creation",
        "secret_env_ref": secret_ref,
    })
    assert edited_response.status_code == 200, edited_response.text

    # Exercise the actual Raw runner, prompt builder, HTTP request, output parser,
    # and persisted prediction. Only the network transport and DNS are mocked.
    try:
        orchestrator._do_run(run_id, effective_concurrency=1)
        completed = db.get_bt_run(run_id)
        assert completed["status"] == "done"
        assert completed["done_events"] == 1
        predictions = load_predictions(completed["out_path"])
        assert len(predictions) == 1
        assert predictions[0].pred_direction == "up"
        assert predictions[0].model_version == model_id
        assert predictions[0].expected_return_pct == 2.5
        assert completed["config"]["evaluation_role"] == "raw_model"
        assert completed["config"]["model_profile_snapshot"] == frozen
        assert len(requests) == 1
        assert requests[0].full_url == completion_url
        assert requests[0].get_header("Authorization") == "Bearer synthetic-multi-provider-credential"
        request_body = json.loads(requests[0].data)
        assert request_body["model"] == model_id
        packet = json.loads(request_body["messages"][-1]["content"])
        event_input = packet["as_of_event"]
        assert event_input["title"] == "Historical company announcement"
        assert packet["contract_version"] == "raw-event-v1"
        assert "Do not use later knowledge, search, tools" in request_body["messages"][0]["content"]
        assert checked_urls == [completion_url]
        assert repository.get_default_profile_id() == replacement_id
        # An independent Raw contestant leaves Pronoia's ambient model unchanged.
        assert llm.resolve_runtime_target().model_id == "replacement-default"
    finally:
        orchestrator._RUN_RESUME.pop(run_id, None)
        orchestrator.clear_run_activity(run_id)
