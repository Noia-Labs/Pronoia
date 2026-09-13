"""Encrypted local credentials for prediction services, with legacy env support."""
from __future__ import annotations

import re
from typing import Any

from .model_endpoint_security import resolve_secret_env_reference, validate_secret_env_reference
from .model_lab import secret_store

_LOCAL_REF = re.compile(r"local-strategy:[0-9a-f]{32}\Z")
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED = {"host", "content-type", "content-length", "accept", "connection", "transfer-encoding", "cookie", "proxy-authorization"}


def validate_header_name(value: str) -> str:
    name = str(value or "").strip()
    if not _HEADER.fullmatch(name) or name.lower() in _RESERVED:
        raise ValueError("请输入有效的认证请求头名，例如 Authorization 或 X-API-Key")
    return name


def validate_strategy_secret_reference(value: Any, *, label: str) -> str:
    reference = str(value or "").strip()
    if not _LOCAL_REF.fullmatch(reference):
        validate_secret_env_reference(reference, namespace="strategy", label=label)
    return reference


def resolve_strategy_secret_reference(value: Any, *, label: str) -> str:
    reference = validate_strategy_secret_reference(value, label=label)
    if _LOCAL_REF.fullmatch(reference):
        return secret_store.resolve_secret_reference(reference.replace("local-strategy:", "local-model:", 1))
    return resolve_secret_env_reference(reference, namespace="strategy", label=label)


def store_strategy_secret(value: str) -> str:
    return secret_store.store_secret(value).replace("local-model:", "local-strategy:", 1)


def discard_strategy_secret(reference: str) -> None:
    if _LOCAL_REF.fullmatch(reference):
        secret_store.discard_secret(reference.replace("local-strategy:", "local-model:", 1))
