"""Prediction service credentials/probes use an isolated vault and mocked HTTP."""
from pathlib import Path

import dotenv
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    from app import config
    from app.routes import external_service as routes
    from app.model_lab import secret_store
    from app.main import _safe_model_lab_validation_error
    from fastapi.exceptions import RequestValidationError
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "isolated.db"))
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, _safe_model_lab_validation_error)
    app.include_router(routes.router)
    return TestClient(app), routes, secret_store


def payload(**kw):
    return {"endpoint": "https://8.8.8.8/predict", "api_key": 'synthetic-key-"quoted"', **kw}


def test_bearer_is_encrypted_and_resolves_for_runtime(service):
    client, _, store = service
    from app.event_backtest.external_strategy import _headers
    from app.event_backtest.strategy_registry import normalize_strategy_spec
    response = client.post("/api/bt/external-service/credentials", json=payload())
    assert response.status_code == 200
    headers = response.json()["headers"]
    assert headers["Authorization"].startswith("local-strategy:")
    assert payload()["api_key"] not in response.text
    normalized = normalize_strategy_spec({"type": "event", "adapter": "external_http", "endpoint": payload()["endpoint"], "headers": headers}, legacy_runner=None, legacy_strategy_type=None)
    assert _headers(normalized)["Authorization"] == "Bearer " + payload()["api_key"]
    for path in store.storage_directory().iterdir():
        assert payload()["api_key"].encode() not in path.read_bytes()


@pytest.mark.parametrize("mode,header,key,expected", [
    ("bearer", "Authorization", "Bearer existing-token", "Bearer existing-token"),
    ("header", "X-API-Key", "synthetic-custom-token", "synthetic-custom-token"),
])
def test_auth_modes_use_correct_header(service, mode, header, key, expected):
    client, _, _ = service
    from app.event_backtest.external_strategy import _headers
    response = client.post("/api/bt/external-service/credentials", json=payload(auth_mode=mode, header_name=header, api_key=key))
    assert response.status_code == 200
    assert _headers(response.json())[header] == expected


def test_no_auth_and_legacy_env(service, monkeypatch):
    client, _, store = service
    from app.event_backtest.external_strategy import _headers
    noauth = client.post("/api/bt/external-service/credentials", json=payload(auth_mode="none", api_key=""))
    assert noauth.json() == {"headers": {}}
    assert not store.storage_directory().exists()
    monkeypatch.setenv("PRONOIA_STRATEGY_SECRET_TEST", "Bearer legacy-token")
    legacy = client.post("/api/bt/external-service/credentials", json=payload(auth_mode="env", header_name="Authorization", secret_env_ref="PRONOIA_STRATEGY_SECRET_TEST"))
    assert _headers(legacy.json())["Authorization"] == "Bearer legacy-token"


def test_example_excludes_existing_predictions_without_network(service, monkeypatch):
    client, routes, _ = service
    event, _ = routes._event(None)
    from dataclasses import replace
    monkeypatch.setattr(routes, "_event", lambda _: (replace(event, analysis_direction="up", analysis_expected_return_pct=2, direction_prior="up"), False))
    monkeypatch.setattr(routes.external_strategy, "_post", lambda *a, **kw: pytest.fail("Example must never call HTTP"))
    result = client.get("/api/bt/external-service/example?horizon=t5").json()
    assert result["request"]["evaluation_horizon"] == "t5"
    assert "analysis_direction" not in result["request"]["event"]
    assert "direction_prior" not in result["request"]["event"]
    assert "analysis_expected_return_pct" not in result["request"]["event"]


def test_probe_validates_response_does_not_persist_credentials_or_echo_secret(service, monkeypatch):
    client, routes, store = service
    seen = []
    def post(endpoint, body, spec):
        seen.append((body, routes.external_strategy._headers(spec)))
        return {"decision": {"event_id": body["event"]["event_id"], "direction": "up", "confidence": .7, "rationale": payload()["api_key"], "horizon": "t3", "expected_return_pct": 2}}
    monkeypatch.setattr(routes.external_strategy, "_post", post)
    response = client.post("/api/bt/external-service/test", json=payload())
    assert response.status_code == 200
    assert response.json()["prediction"]["expected_return_pct"] == 2
    assert response.json()["prediction"]["rationale"] == "[redacted]"
    assert len(seen) == 1
    assert seen[0][1]["Authorization"] == "Bearer " + payload()["api_key"]
    assert list(store.storage_directory().glob("*.enc")) == []


def test_failed_probe_discards_key_and_reports_error(service, monkeypatch):
    client, routes, store = service
    monkeypatch.setattr(routes.external_strategy, "_post", lambda *a: {"direction": "unknown"})
    response = client.post("/api/bt/external-service/test", json=payload())
    assert response.status_code == 422
    assert "direction" in response.json()["detail"]
    assert list(store.storage_directory().glob("*.enc")) == []


@pytest.mark.parametrize("change", [{"auth_mode": "header", "header_name": "Host"}, {"auth_mode": "header", "header_name": "X-Key\r\nInjected"}, {"auth_mode": "env", "secret_env_ref": "PATH"}, {"timeout_seconds": 999}, {"auth_mode": "bad"}])
def test_invalid_config_does_not_echo_secret(service, change):
    client, _, store = service
    response = client.post("/api/bt/external-service/credentials", json=payload(**change))
    assert response.status_code == 422
    assert payload()["api_key"] not in response.text
    assert "input" not in response.json().get("detail", [])
    assert not store.storage_directory().exists()


def test_saved_ref_is_redacted_from_run_dto(service):
    from app.routes.backtest import _share_safe_payload
    assert _share_safe_payload({"headers": {"X-Custom-Credential": "local-strategy:" + "a" * 32}})["headers"]["X-Custom-Credential"] == "[secret reference hidden]"


def test_invalid_remote_decision_does_not_echo_env_key(service, monkeypatch):
    client, routes, _ = service
    secret = "synthetic-env-token-do-not-echo"
    monkeypatch.setenv("PRONOIA_STRATEGY_SECRET_TEST", "Bearer " + secret)
    monkeypatch.setattr(routes.external_strategy, "_post", lambda *a: {"event_id": secret})
    response = client.post("/api/bt/external-service/test", json=payload(auth_mode="env", header_name="Authorization", secret_env_ref="PRONOIA_STRATEGY_SECRET_TEST", api_key=""))
    assert response.status_code == 422
    assert secret not in response.text


def test_runtime_redacts_credential_echo_before_prediction_parsing(service, monkeypatch):
    import io
    import json
    from types import SimpleNamespace
    _, routes, _ = service
    secret = 'synthetic-raw-token-"quoted"'
    monkeypatch.setenv("PRONOIA_STRATEGY_SECRET_TEST", "Bearer " + secret)
    monkeypatch.setattr(routes.external_strategy, "_validate_runtime_endpoint", lambda *_: None)
    response = json.dumps({"event_id": secret, "rationale": secret, "metadata": [secret]}).encode()
    monkeypatch.setattr(routes.external_strategy, "build_opener", lambda *_: SimpleNamespace(open=lambda *a, **kw: io.BytesIO(response)))
    result = routes.external_strategy._post("https://8.8.8.8/predict", {}, {"headers": {"Authorization": "env:PRONOIA_STRATEGY_SECRET_TEST"}})
    assert result == {"event_id": "[redacted]", "rationale": "[redacted]", "metadata": ["[redacted]"]}
