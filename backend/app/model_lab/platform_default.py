"""Trusted platform-default identity without copying credentials into SQLite.

The selector is public, but the reserved profile rows are server-owned. An
environment snapshot can only resolve the key for its exact configured target;
changing deployment endpoint/model requires a new experiment, never redirecting
an old experiment's credential to a different server.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .. import config
from .defaults import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_TIMEOUT_SECONDS

PLATFORM_DEFAULT_SELECTOR = "__platform_default__"
PLATFORM_PROFILE_PREFIX = "__platform_env__:"
PLATFORM_PROVIDER = "pronoia-platform-environment"
PLATFORM_SECRET_REF = "__server_platform_credentials__"


def is_platform_profile_id(profile_id: Any) -> bool:
    return str(profile_id or "").startswith(PLATFORM_PROFILE_PREFIX)


def _target_identity(base_url: Any, model_id: Any) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[:-len("/chat/completions")].rstrip("/")
    raw = json.dumps([base, str(model_id or "").strip()], ensure_ascii=False)
    return PLATFORM_PROFILE_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def environment_profile() -> dict[str, Any]:
    """Read configuration metadata only; never return a resolved API key."""
    identity = _target_identity(config.LLM_BASE_URL, config.LLM_MODEL)
    return {
        "id": identity,
        "name": f"Pronoia 平台配置 · {config.LLM_MODEL} · {identity[-8:]}",
        "provider": PLATFORM_PROVIDER,
        "base_url": str(config.LLM_BASE_URL or "").strip().rstrip("/"),
        "model_id": str(config.LLM_MODEL or "").strip(),
        "secret_env_ref": PLATFORM_SECRET_REF,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "timeout_seconds": max(DEFAULT_TIMEOUT_SECONDS, float(config.LLM_TIMEOUT)),
        "thinking_mode": "auto",
        "input_price_per_million": 0,
        "output_price_per_million": 0,
        "currency": "CNY",
        "is_active": True,
        "secret_configured": bool(config.LLM_API_KEY and config.LLM_BASE_URL and config.LLM_MODEL),
    }


def matches_environment(profile: Mapping[str, Any]) -> bool:
    return (
        is_platform_profile_id(profile.get("id"))
        and profile.get("provider") == PLATFORM_PROVIDER
        and profile.get("secret_env_ref") == PLATFORM_SECRET_REF
        and str(profile.get("id")) == _target_identity(profile.get("base_url"), profile.get("model_id"))
        and str(profile.get("id")) == _target_identity(config.LLM_BASE_URL, config.LLM_MODEL)
    )


def resolve_environment_secret(profile: Mapping[str, Any]) -> str:
    from .providers import ModelProviderError

    if not matches_environment(profile):
        raise ModelProviderError("平台 API 配置已变化，历史实验不会改用新目标；请重新创建实验")
    if not config.LLM_API_KEY:
        raise ModelProviderError("Pronoia 平台 API 密钥未配置")
    return str(config.LLM_API_KEY)
