"""Connection probes distinguish a tiny exhausted budget from broken credentials."""
from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from app.model_lab import providers
from .test_direct_model_credentials import direct_client, create_direct


PROFILE = {
    "provider": "DeepSeek", "base_url": "https://models.example.invalid/v1",
    "model_id": "reasoning-model", "max_output_tokens": 65536,
    "timeout_seconds": 600, "thinking_mode": "auto",
}


@pytest.fixture
def response_transport(monkeypatch):
    responses, requests = [], []

    class Opener:
        def open(self, request, *, timeout):
            requests.append(json.loads(request.data))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(providers, "_runtime_validate", lambda url: None)
    monkeypatch.setattr(providers, "resolve_profile_secret", lambda profile: "synthetic-probe-key")
    monkeypatch.setattr(providers, "build_opener", lambda *args: Opener())
    return responses, requests


def completion(content=None, finish_reason="length", **message_fields):
    return {
        "model": "reasoning-model",
        "choices": [{"message": {"role": "assistant", "content": content, **message_fields},
                     "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 32, "total_tokens": 44},
    }


@pytest.mark.parametrize("reasoning", [None, "private synthetic reasoning"])
def test_reasoning_budget_exhaustion_confirms_connection_without_claiming_answer(response_transport, reasoning):
    responses, requests = response_transport
    responses.append(completion(reasoning_content=reasoning))
    result = providers.validate_profile(PROFILE)
    assert result["ok"] is True
    assert result["status"] == "connected_probe_incomplete"
    assert result["answer_verified"] is False
    assert "尚未验证完整回答" in result["message"]
    assert "private synthetic reasoning" not in json.dumps(result)
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 32
    assert PROFILE["max_output_tokens"] == 65536


def test_reasoning_evidence_without_usage_can_confirm_connection(response_transport):
    responses, _ = response_transport
    response = completion(reasoning_content="synthetic reasoning")
    response.pop("usage")
    responses.append(response)
    assert providers.validate_profile(PROFILE)["status"] == "connected_probe_incomplete"


def test_complete_answer_confirms_answer_and_truncated_answer_does_not(response_transport):
    responses, _ = response_transport
    responses.extend([completion("OK", "stop"), completion("O", "length")])
    assert providers.validate_profile(PROFILE)["answer_verified"] is True
    assert providers.validate_profile(PROFILE)["answer_verified"] is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
def test_http_failures_never_become_successful_probes(response_transport, status):
    responses, _ = response_transport
    responses.append(HTTPError("https://models.example.invalid/v1", status, "bad model or key", {},
                               io.BytesIO(b'{"error":"bad model or key"}')))
    with pytest.raises(providers.ModelProviderError, match=f"HTTP {status}"):
        providers.validate_profile(PROFILE)


@pytest.mark.parametrize("body", [
    {"error": {"message": "invalid model"}},
    {**completion(), "error": {"message": "invalid model"}},
    {"choices": [{"finish_reason": "length", "message": {}}]},
    {**completion(), "usage": {}},
    {**completion(), "choices": [{"finish_reason": "length", "message": {"content": {}}}]},
    completion(None, "stop"),
    completion(None, "content_filter"),
])
def test_malformed_error_or_unverified_empty_responses_stay_failed(response_transport, body):
    responses, _ = response_transport
    responses.append(body)
    with pytest.raises(providers.ModelProviderError):
        providers.validate_profile(PROFILE)


def test_normal_answer_and_scoring_calls_still_reject_empty_truncated_answers(response_transport):
    responses, requests = response_transport
    responses.extend([completion(), completion()])
    with pytest.raises(providers.ModelProviderError, match="空答案"):
        providers.call_chat(PROFILE, [{"role": "user", "content": "V01"}])
    with pytest.raises(providers.ModelProviderError, match="空答案"):
        providers.score_answer(PROFILE, {"prompt": "V01"}, "a final answer")
    assert requests[0]["max_tokens"] == 65536
    assert requests[1]["max_tokens"] == 32768


def test_validation_api_persists_incomplete_probe_explanation(direct_client, response_transport):
    from app.model_lab import repository

    profile = create_direct(direct_client)
    responses, _ = response_transport
    responses.append(completion())
    response = direct_client.post(f"/api/model-lab/profiles/{profile['id']}/validate")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["answer_verified"] is False
    saved = repository.get_profile(profile["id"], public=False)
    assert saved["last_validation_status"] == "ok"
    assert saved["last_validation_message"] == payload["message"]
