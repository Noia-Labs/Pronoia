"""Web-entered model keys stay local, opaque in SQLite, and frozen per snapshot."""
from __future__ import annotations

import json
import asyncio
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import dotenv
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError


KEY_ONE = "synthetic-direct-model-key-one"
KEY_TWO = "synthetic-direct-model-key-two"
ENV_REF = "PRONOIA_MODEL_SECRET_DIRECT_TEST"


@pytest.fixture()
def direct_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    monkeypatch.setenv(ENV_REF, "synthetic-legacy-key")
    for name in ("ARK_API_KEY", "MAAS_API_KEY", "PRONOIA_SHARE_USER", "PRONOIA_SHARE_PASSWORD"):
        monkeypatch.setenv(name, "")

    from app import config, db
    from app.main import app

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "direct-keys.db"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "SHARE_USER", "")
    monkeypatch.setattr(config, "SHARE_PASSWORD", "")
    monkeypatch.setattr(db, "_conn", None)
    db.init_db()

    def no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Credential tests must not call real models or DNS")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    yield TestClient(app)
    if db._conn is not None:
        db._conn.close()


def profile_payload(**updates: Any) -> dict[str, Any]:
    return {
        "name": "Direct credential test", "provider": "qwen",
        "model_id": "test-model", "base_url": "https://model.example.invalid/v1",
        "api_key": KEY_ONE, **updates,
    }


def create_direct(client: TestClient, **updates: Any) -> dict[str, Any]:
    response = client.post("/api/model-lab/profiles", json=profile_payload(**updates))
    assert response.status_code == 201, response.text
    assert KEY_ONE not in response.text
    assert KEY_TWO not in response.text
    assert "api_key" not in response.text
    assert "secret_env_ref" not in response.text
    assert "local-model:" not in response.text
    return response.json()


def test_create_encrypts_key_and_resolves_after_process_restart(direct_client: TestClient):
    from app import config, db
    from app.model_lab import repository, secret_store

    profile = create_direct(direct_client, api_key=f"  {KEY_ONE}  ")
    assert profile["secret_configured"] is True
    saved = repository.get_profile(profile["id"], public=False)
    reference = saved["secret_env_ref"]
    assert reference.startswith("local-model:")
    assert secret_store.resolve_secret_reference(reference) == KEY_ONE
    assert KEY_ONE not in "\n".join(db._get_conn().iterdump())
    assert all(KEY_ONE.encode() not in item.read_bytes()
               for item in secret_store.storage_directory().rglob("*") if item.is_file())

    # A fresh interpreter must decrypt from these same files without an env
    # credential, an in-memory cache, or loading the real project .env.
    check = """
import os, sys, dotenv
os.environ.clear()
dotenv.load_dotenv = lambda *args, **kwargs: False
from app import config
config.DB_PATH = sys.argv[1]
from app.model_lab.secret_store import resolve_secret_reference
assert resolve_secret_reference(sys.argv[2]) == 'synthetic-direct-model-key-one'
"""
    restarted = subprocess.run(
        [sys.executable, "-c", check, config.DB_PATH, reference],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
    )
    assert restarted.returncode == 0, restarted.stderr
    for endpoint in ("/profiles", f"/profiles/{profile['id']}", "/profiles/default"):
        response = direct_client.get("/api/model-lab" + endpoint)
        assert response.status_code == 200
        assert KEY_ONE not in response.text
        assert reference not in response.text


def test_rotation_keeps_old_snapshot_and_blank_update_keeps_new_key(direct_client: TestClient):
    from app.model_lab import repository
    from app.model_lab.providers import profile_snapshot, resolve_profile_secret

    profile = create_direct(direct_client)
    frozen = profile_snapshot(repository.get_profile(profile["id"], public=False))
    response = direct_client.patch(f"/api/model-lab/profiles/{profile['id']}", json={"api_key": KEY_TWO})
    assert response.status_code == 200, response.text
    assert KEY_TWO not in response.text
    current = repository.get_profile(profile["id"], public=False)
    assert current["secret_env_ref"] != frozen["secret_env_ref"]
    assert resolve_profile_secret(frozen) == KEY_ONE
    assert resolve_profile_secret(current) == KEY_TWO
    preserved = direct_client.patch(
        f"/api/model-lab/profiles/{profile['id']}", json={"api_key": "  ", "name": "Renamed"},
    )
    assert preserved.status_code == 200
    assert repository.get_profile(profile["id"], public=False)["secret_env_ref"] == current["secret_env_ref"]


def test_legacy_environment_key_and_switching_credentials_remain_supported(direct_client: TestClient):
    from app.model_lab import repository
    from app.model_lab.providers import profile_snapshot, resolve_profile_secret

    legacy = profile_payload()
    legacy.pop("api_key")
    legacy["secret_env_ref"] = ENV_REF
    created = direct_client.post("/api/model-lab/profiles", json=legacy)
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]
    assert created.json()["secret_configured"] is True
    assert resolve_profile_secret(repository.get_profile(profile_id, public=False)) == "synthetic-legacy-key"
    patched = direct_client.patch(f"/api/model-lab/profiles/{profile_id}", json={"api_key": KEY_ONE})
    assert patched.status_code == 200
    frozen = profile_snapshot(repository.get_profile(profile_id, public=False))
    reverted = direct_client.patch(
        f"/api/model-lab/profiles/{profile_id}", json={"secret_env_ref": ENV_REF, "api_key": ""},
    )
    assert reverted.status_code == 200
    assert resolve_profile_secret(repository.get_profile(profile_id, public=False)) == "synthetic-legacy-key"
    assert resolve_profile_secret(frozen) == KEY_ONE


@pytest.mark.parametrize("updates", [
    {"api_key": None}, {"api_key": " "}, {"api_key": KEY_ONE + "\ninside"},
    {"api_key": KEY_ONE * 700}, {"api_key": 1234},
    {"secret_env_ref": ENV_REF}, {"secret_env_ref": "local-model:" + "a" * 32},
    {"unexpected_credential": KEY_TWO},
])
def test_invalid_credentials_never_echo_input(direct_client: TestClient, updates: dict[str, Any]):
    response = direct_client.post("/api/model-lab/profiles", json=profile_payload(**updates))
    assert response.status_code == 422, response.text
    assert KEY_ONE not in response.text
    assert KEY_TWO not in response.text
    for error in response.json()["detail"]:
        assert "input" not in error
        assert "ctx" not in error


def test_schema_repr_and_dumps_never_include_key(direct_client: TestClient):
    from app.model_lab.schemas import ModelProfileCreate

    schema = ModelProfileCreate(**profile_payload())
    assert KEY_ONE not in repr(schema)
    assert "api_key" not in schema.model_dump()
    with pytest.raises(ValidationError) as raised:
        ModelProfileCreate(**profile_payload(api_key=KEY_ONE + "\ninside"))
    assert KEY_ONE not in str(raised.value)


@pytest.mark.parametrize("operation", ["create", "patch"])
def test_failed_db_write_discards_only_the_new_key(
    direct_client: TestClient, monkeypatch: pytest.MonkeyPatch, operation: str,
):
    from app.model_lab import repository, secret_store
    from app.routes import model_lab

    original = create_direct(direct_client)
    old_reference = repository.get_profile(original["id"], public=False)["secret_env_ref"]
    references: list[str] = []
    store = model_lab.store_secret

    def record_new_secret(secret: str) -> str:
        reference = store(secret)
        references.append(reference)
        return reference

    monkeypatch.setattr(model_lab, "store_secret", record_new_secret)
    if operation == "create":
        response = direct_client.post("/api/model-lab/profiles", json=profile_payload(api_key=KEY_TWO))
    else:
        other = create_direct(direct_client, name="Conflicting name")
        references.clear()
        response = direct_client.patch(
            f"/api/model-lab/profiles/{original['id']}", json={"name": other["name"], "api_key": KEY_TWO},
        )
    assert response.status_code == 409, response.text
    assert len(references) == 1
    with pytest.raises(secret_store.ModelSecretError):
        secret_store.resolve_secret_reference(references[0])
    assert secret_store.resolve_secret_reference(old_reference) == KEY_ONE
    assert repository.get_profile(original["id"], public=False)["secret_env_ref"] == old_reference


def test_old_snapshot_and_validation_errors_are_redacted(direct_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app.model_lab import repository
    from app.model_lab.providers import ModelProviderError, profile_snapshot, resolve_profile_secret
    from app.routes import model_lab

    profile = create_direct(direct_client)
    frozen = profile_snapshot(repository.get_profile(profile["id"], public=False))
    direct_client.patch(f"/api/model-lab/profiles/{profile['id']}", json={"api_key": KEY_TWO})
    serialized = json.dumps(model_lab._public({
        "snapshot": frozen, "answer": f"{KEY_ONE} {KEY_TWO} {frozen['secret_env_ref']}",
    }))
    assert KEY_ONE not in serialized
    assert KEY_TWO not in serialized
    assert "local-model:" not in serialized

    def reflected_error(saved_profile: dict[str, Any]) -> None:
        raise ModelProviderError("Rejected credential " + resolve_profile_secret(saved_profile))

    monkeypatch.setattr(model_lab, "validate_profile", reflected_error)
    response = direct_client.post(f"/api/model-lab/profiles/{profile['id']}/validate")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert KEY_TWO not in response.text
    assert KEY_TWO not in repository.get_profile(profile["id"], public=False)["last_validation_message"]


def test_web_key_can_be_selected_as_default(direct_client: TestClient):
    profile = create_direct(direct_client)
    response = direct_client.post(f"/api/model-lab/profiles/{profile['id']}/set-default")
    assert response.status_code == 200, response.text
    assert response.json()["default_profile_id"] == profile["id"]
    assert KEY_ONE not in response.text


def test_default_llm_uses_web_key_and_rotation_does_not_reuse_old_client(
    direct_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    import httpx
    from openai import AsyncOpenAI
    from app import llm, model_endpoint_security
    from app.model_lab import repository
    from app.model_lab.providers import profile_snapshot

    profile = create_direct(direct_client)
    selected = direct_client.post(f"/api/model-lab/profiles/{profile['id']}/set-default")
    assert selected.status_code == 200
    frozen = profile_snapshot(repository.get_profile(profile["id"], public=False))
    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", lambda *_a, **_k: None)
    monkeypatch.setattr(llm, "_profile_clients", {})
    requests: list[httpx.Request] = []
    clients: list[AsyncOpenAI] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "test-completion", "object": "chat.completion", "created": 0,
            "model": "test-model", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Mock result"}}],
        })

    def mock_client(**kwargs: Any) -> AsyncOpenAI:
        client = AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)))
        clients.append(client)
        return client

    monkeypatch.setattr(llm, "AsyncOpenAI", mock_client)

    async def exercise() -> None:
        try:
            assert await llm.complete_text("Test", "Test", max_tokens=64) == "Mock result"
            first_client = llm.get_client()
            rotated = direct_client.patch(
                f"/api/model-lab/profiles/{profile['id']}", json={"api_key": KEY_TWO},
            )
            assert rotated.status_code == 200
            assert await llm.complete_text("Test", "Test", max_tokens=64) == "Mock result"
            assert llm.get_client() is not first_client
            with llm.model_profile_context(frozen):
                assert llm.get_client() is first_client
                assert await llm.complete_text("Test", "Test", max_tokens=64) == "Mock result"
            assert len(clients) == 2
        finally:
            for client in clients:
                await client.close()

    asyncio.run(exercise())
    assert len(requests) == 3
    assert [request.headers["Authorization"] for request in requests] == [
        f"Bearer {KEY_ONE}", f"Bearer {KEY_TWO}", f"Bearer {KEY_ONE}",
    ]
    assert all(str(request.url) == "https://model.example.invalid/v1/chat/completions" for request in requests)
