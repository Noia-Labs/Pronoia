from __future__ import annotations

import base64
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.main import ShareAuthMiddleware, _startup


def _test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ShareAuthMiddleware)

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/private")
    def private() -> dict[str, bool]:
        return {"ok": True}

    return app


def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def test_share_auth_disabled_preserves_local_access() -> None:
    with patch.object(config, "SHARE_USER", ""), patch.object(config, "SHARE_PASSWORD", ""):
        response = TestClient(_test_app()).get("/private")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_share_auth_rejects_missing_and_wrong_credentials() -> None:
    with patch.object(config, "SHARE_USER", "colleague"), patch.object(
        config, "SHARE_PASSWORD", "correct-horse-12"
    ):
        client = TestClient(_test_app())
        missing = client.get("/private")
        wrong = client.get(
            "/private", headers={"Authorization": _basic("colleague", "wrong")}
        )

    for response in (missing, wrong):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Basic realm="Pronoia", charset="UTF-8"'
        assert response.headers["cache-control"] == "no-store"


def test_share_auth_accepts_correct_utf8_credentials() -> None:
    with patch.object(config, "SHARE_USER", "同事"), patch.object(
        config, "SHARE_PASSWORD", "口令:安全而且足够长123"
    ):
        response = TestClient(_test_app()).get(
            "/private", headers={"Authorization": _basic("同事", "口令:安全而且足够长123")}
        )
    assert response.status_code == 200


def test_health_remains_public_when_share_auth_enabled() -> None:
    with patch.object(config, "SHARE_USER", "colleague"), patch.object(
        config, "SHARE_PASSWORD", "health-secret-12"
    ):
        response = TestClient(_test_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize(
    ("user", "password"),
    [("colleague", ""), ("", "health-secret-12")],
)
def test_startup_rejects_half_configured_share_auth(user: str, password: str) -> None:
    with patch.object(config, "SHARE_USER", user), patch.object(
        config, "SHARE_PASSWORD", password
    ):
        with pytest.raises(RuntimeError, match="must be configured together"):
            _startup()


@pytest.mark.parametrize(
    ("user", "password", "message"),
    [
        ("bad:user", "long-enough-password", "must not contain a colon"),
        ("colleague", "too-short", "at least 12 characters"),
        ("colleague", "long-password\n", "control characters"),
    ],
)
def test_startup_rejects_unsafe_share_credentials(
    user: str, password: str, message: str
) -> None:
    with patch.object(config, "SHARE_USER", user), patch.object(
        config, "SHARE_PASSWORD", password
    ):
        with pytest.raises(RuntimeError, match=message):
            _startup()
