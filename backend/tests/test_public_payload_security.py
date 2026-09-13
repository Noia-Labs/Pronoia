"""Credential-shaped fields never cross dataset or Backtest public boundaries."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.main import app
from backend.app.routes.backtest import _share_safe_payload
from backend.app.schemas import DatasetSourceSpec
from backend.app.model_endpoint_security import redact_sensitive_text


@pytest.mark.parametrize("source", [
    {"type": "api", "provider": "vendor", "ref": "dataset://safe", "token": "plaintext"},
    {"type": "api", "provider": "vendor", "ref": "dataset://safe", "metadata": {"password": "plaintext"}},
    {"type": "api", "provider": "vendor", "ref": "dataset://safe", "metadata": {"nested": {"client_secret": "plaintext"}}},
])
def test_dataset_source_rejects_plaintext_credentials(source):
    with pytest.raises(ValidationError):
        DatasetSourceSpec.model_validate(source)


def test_dataset_source_allows_non_sensitive_metadata():
    source = DatasetSourceSpec.model_validate({
        "type": "local_file",
        "provider": "official_archive",
        "ref": "dataset://safe",
        "metadata": {"license": "internal", "contains_imported_decisions": False},
    })
    assert source.metadata["license"] == "internal"


def test_backtest_public_payload_recursively_scrubs_credentials():
    public = _share_safe_payload({
        "config": {
            "profile": {
                "secret_env_ref": "PRONOIA_MODEL_SECRET_VENDOR",
                "model_id": "model-v1",
            },
            "metadata": {"token": "plaintext", "safe": "kept"},
            "headers": {"Authorization": "env:PRONOIA_STRATEGY_SECRET_VENDOR"},
        },
        "message": "using PRONOIA_DATA_SECRET_VENDOR",
    })
    rendered = repr(public)
    assert "plaintext" not in rendered
    assert "PRONOIA_" not in rendered
    assert public["config"]["profile"]["model_id"] == "model-v1"
    assert public["config"]["metadata"]["safe"] == "kept"


def test_transport_diagnostics_remove_reflected_header_secret():
    secret = "vendor-secret-reflected-by-error"
    safe = redact_sensitive_text(
        f"HTTP 401: request Authorization=Bearer {secret}; token={secret}",
        secret,
    )
    assert secret not in safe
    assert "Bearer [redacted]" in safe


def test_unknown_api_route_is_not_masked_by_spa_fallback():
    response = TestClient(app).get("/api/definitely-not-a-real-route")
    assert response.status_code == 404
    assert "text/html" not in response.headers.get("content-type", "")
