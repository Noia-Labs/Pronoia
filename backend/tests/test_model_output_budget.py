"""Output budgets apply to new requests while explicit historical limits survive."""
from __future__ import annotations

import io
import json
import socket
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dotenv
import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch):
    # Import config without loading the project's real credentials. This file
    # also runs safely beside suites which have already imported config.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    for name, value in {
        "ARK_API_URL": "https://platform.example.invalid/v1",
        "ARK_API_KEY": "synthetic-platform-secret",
        "ARK_MODEL": "platform-test",
        "MAAS_API_URL": "",
        "MAAS_API_KEY": "",
        "MAAS_MODEL": "",
        "PRONOIA_MODEL_SECRET_OUTPUT_BUDGET_TEST": "synthetic-model-secret",
    }.items():
        monkeypatch.setenv(name, value)

    from app import config

    monkeypatch.setattr(config, "LLM_BASE_URL", "https://platform.example.invalid/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "synthetic-platform-secret")
    monkeypatch.setattr(config, "LLM_MODEL", "platform-test")

    def no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Output budget tests must not access the network")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)


@pytest.fixture()
def isolated_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app import config, db
    from app.model_lab import repository

    # Preserve any singleton owned by another suite without touching its DB.
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "output-budget.db"))
    db.init_db()
    yield repository
    if db._conn is not None:
        db._conn.close()


def profile_data(**updates: Any) -> dict[str, Any]:
    return {
        "name": "Output budget test",
        "provider": "custom",
        "base_url": "https://models.example.invalid/v1",
        "model_id": "model-test",
        "secret_env_ref": "PRONOIA_MODEL_SECRET_OUTPUT_BUDGET_TEST",
        **updates,
    }


@pytest.fixture()
def captured_requests(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from app.model_lab import providers

    requests: list[dict[str, Any]] = []

    class MockOpener:
        def open(self, request: Any, *, timeout: float) -> io.BytesIO:
            assert request.full_url == "https://models.example.invalid/v1/chat/completions"
            assert request.get_header("Authorization") == "Bearer synthetic-model-secret"
            requests.append({**json.loads(request.data), "_transport_timeout": timeout})
            return io.BytesIO(json.dumps({
                "model": "model-test",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }).encode())

    monkeypatch.setattr(providers, "_runtime_validate", lambda _url: None)
    monkeypatch.setattr(providers, "build_opener", lambda *_handlers: MockOpener())
    return requests


@pytest.mark.parametrize(("old_limit", "old_timeout"), [(4096, 180), (8192, 900)])
def test_new_profile_defaults_preserve_explicit_saved_settings(
    isolated_profiles, old_limit: int, old_timeout: float,
):
    from app.model_lab.schemas import ModelProfileCreate

    old = isolated_profiles.create_profile(profile_data(
        name="Existing profile", max_output_tokens=old_limit, timeout_seconds=old_timeout,
    ))
    parsed = ModelProfileCreate(**profile_data())
    assert parsed.max_output_tokens == 65536
    assert parsed.timeout_seconds == 600
    new = isolated_profiles.create_profile(parsed.model_dump())
    assert new["max_output_tokens"] == 65536
    assert new["timeout_seconds"] == 600
    updated = isolated_profiles.update_profile(old["id"], {"name": "Renamed old profile"})
    assert updated["max_output_tokens"] == old_limit
    assert updated["timeout_seconds"] == old_timeout


def test_repository_creation_without_schema_uses_same_default(isolated_profiles):
    created = isolated_profiles.create_profile(profile_data())
    assert created["max_output_tokens"] == 65536
    assert created["timeout_seconds"] == 600


def test_new_database_column_defaults_match_profile_defaults(isolated_profiles):
    from app import db

    columns = {
        row["name"]: row["dflt_value"]
        for row in db._get_conn().execute("PRAGMA table_info(ml_model_profiles)")
    }
    assert int(columns["max_output_tokens"]) == 65536
    assert float(columns["timeout_seconds"]) == 600


@pytest.mark.parametrize(("configured_timeout", "expected_timeout"), [(180, 600), (750, 750)])
def test_platform_defaults_preserve_larger_timeout_and_existing_snapshot(
    monkeypatch: pytest.MonkeyPatch, configured_timeout: float, expected_timeout: float,
):
    from app import config
    from app.model_lab.platform_default import environment_profile
    from app.model_lab.providers import profile_snapshot

    monkeypatch.setattr(config, "LLM_TIMEOUT", configured_timeout)
    frozen = profile_snapshot(profile_data(
        id="old-profile", max_output_tokens=8192, timeout_seconds=180,
    ))
    original = dict(frozen)
    current = environment_profile()
    assert current["max_output_tokens"] == 65536
    assert current["timeout_seconds"] == expected_timeout
    assert frozen == original


@pytest.mark.parametrize(
    ("profile_limit", "request_limit", "expected"),
    [(None, None, 65536), (4096, None, 4096), (4096, 8192, 4096),
     (8192, None, 8192), (65536, None, 65536), (None, 1024, 1024)],
)
def test_chat_sends_budget_once_and_respects_explicit_limits(
    captured_requests: list[dict[str, Any]],
    profile_limit: int | None,
    request_limit: int | None,
    expected: int,
):
    from app.model_lab.providers import call_chat, profile_snapshot

    profile = profile_data(id="frozen-profile")
    if profile_limit is not None:
        profile["max_output_tokens"] = profile_limit
    frozen = profile_snapshot(profile)
    call_chat(frozen, [{"role": "user", "content": "Test"}], max_tokens=request_limit)
    assert len(captured_requests) == 1
    assert captured_requests[0]["max_tokens"] == expected
    assert frozen.get("max_output_tokens") == profile_limit


@pytest.mark.parametrize(("profile_timeout", "expected"), [(None, 600), (180, 180), (900, 900)])
def test_chat_timeout_defaults_only_when_missing(
    captured_requests: list[dict[str, Any]], profile_timeout: float | None, expected: float,
):
    from app.model_lab.providers import call_chat, profile_snapshot

    profile = profile_data(id="timeout-profile")
    if profile_timeout is not None:
        profile["timeout_seconds"] = profile_timeout
    frozen = profile_snapshot(profile)
    original = dict(frozen)
    call_chat(frozen, [{"role": "user", "content": "Test"}])
    assert len(captured_requests) == 1
    assert captured_requests[0]["_transport_timeout"] == expected
    assert frozen == original


@pytest.mark.parametrize(
    ("profile_limit", "expected"),
    [(None, 32768), (4096, 4096), (8192, 8192), (12000, 12000), (65536, 32768)],
)
def test_score_sends_minimum_of_structured_and_profile_budget_once(
    captured_requests: list[dict[str, Any]], profile_limit: int | None, expected: int,
):
    from app.model_lab.providers import score_answer

    profile = profile_data()
    if profile_limit is not None:
        profile["max_output_tokens"] = profile_limit
    result = score_answer(profile, {"prompt": "Fixture question"}, "Fixture answer")
    assert len(captured_requests) == 1
    assert captured_requests[0]["max_tokens"] == expected
    assert captured_requests[0]["response_format"] == {"type": "json_object"}
    assert result["judge_model_id"] == "model-test"


def test_agent_stage_budget_is_not_replaced_by_profile_default(monkeypatch: pytest.MonkeyPatch):
    from app import llm, model_endpoint_security
    from app.model_lab.schemas import ModelProfileCreate

    requests: list[dict[str, Any]] = []

    async def complete(**kwargs: Any):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="stage answer"))])

    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=complete)),
    ))
    profile = ModelProfileCreate(**profile_data()).model_dump()
    with llm.model_profile_context(profile):
        assert asyncio.run(llm.complete_text("Fixture system", "Fixture task", max_tokens=1800)) == "stage answer"
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 1800


@pytest.mark.parametrize("profile_limit", [8192, 65536])
@pytest.mark.parametrize("mode", ["final", "round_limit", "failed_tools"])
def test_all_agent_streams_use_frozen_connection_budget(monkeypatch, profile_limit, mode):
    from app import llm, model_endpoint_security

    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        tool = mode != "final" and len(calls) <= (3 if mode == "failed_tools" else 1)
        tool_calls = [SimpleNamespace(index=0, id=f"call-{len(calls)}", function=SimpleNamespace(
            name="fixture_skill", arguments="{}"))] if tool else None
        async def stream():
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None if tool else "Final answer", reasoning_content=None, tool_calls=tool_calls),
                finish_reason="tool_calls" if tool else "stop")])
        return stream()

    async def execute(*args):
        return {"ok": mode != "failed_tools", "data": {}}

    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm, "ensure_skills_loaded", lambda: None)
    monkeypatch.setattr(llm, "tools_for_agent", lambda *args: [])
    profile = profile_data(max_output_tokens=profile_limit)
    state = {"content": "", "tool_trace": []}
    async def run():
        with llm.model_profile_context(profile):
            # A later profile mutation cannot change this logical run.
            profile["max_output_tokens"] = 32
            async for _ in llm.run_agent("fixture", [{"role": "user", "content": "V01"}],
                                        agent_def={"skills": []}, state=state, skill_executor=execute,
                                        max_rounds=3 if mode == "failed_tools" else 1):
                pass
    asyncio.run(run())
    assert state["content"] == "Final answer"
    assert len(calls) == {"final": 1, "round_limit": 2, "failed_tools": 4}[mode]
    assert all(call["max_tokens"] == profile_limit for call in calls)


def test_json_defaults_and_retry_respect_lower_frozen_profile_limit(monkeypatch):
    from app import llm, model_endpoint_security

    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=None if len(calls) == 1 else '{"ok":true}', reasoning_content="reasoning"),
            finish_reason="length" if len(calls) == 1 else "stop")], usage=None)
    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with llm.model_profile_context(profile_data(max_output_tokens=8192)):
        result = asyncio.run(llm.complete_json_diagnostic("Fixture", "Question"))
    assert result.value == {"ok": True}
    assert [call["max_tokens"] for call in calls] == [8192, 8192]
    assert result.output_budget_escalated is False


@pytest.mark.parametrize(("method", "expected"), [("complete_text", 65536), ("complete_json", 32768)])
def test_one_shot_defaults_have_reasoning_room(monkeypatch, method, expected):
    from app import llm

    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"ok":true}', reasoning_content=None), finish_reason="stop")], usage=None)
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    target = llm.LLMRuntimeTarget("https://example.invalid", "synthetic", "model", 600)
    with llm.runtime_target_context(target):
        asyncio.run(getattr(llm, method)("Fixture", "Question"))
    assert calls[0]["max_tokens"] == expected
