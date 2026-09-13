"""Shared transport and SSRF policy for user-managed model endpoints."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlparse
from urllib.request import HTTPRedirectHandler


STRATEGY_SECRET_ENV_PREFIX = "PRONOIA_STRATEGY_SECRET_"
DATA_SECRET_ENV_PREFIX = "PRONOIA_DATA_SECRET_"
MODEL_SECRET_ENV_PREFIX = "PRONOIA_MODEL_SECRET_"
_SECRET_ENV_PREFIXES = {
    "strategy": STRATEGY_SECRET_ENV_PREFIX,
    "data": DATA_SECRET_ENV_PREFIX,
    "model": MODEL_SECRET_ENV_PREFIX,
}
_SECRET_ENV_SUFFIX_RE = re.compile(r"^[A-Z0-9_]+$")
_CREDENTIAL_QUERY_KEYS = {
    "apikey", "key", "token", "accesstoken", "authtoken", "auth",
    "authorization", "secret", "clientsecret", "password", "credential",
    "signature", "sig",
}
_CREDENTIAL_FIELD_KEYS = {
    "apikey", "xapikey", "key", "token", "accesstoken", "refreshtoken",
    "authtoken", "auth", "authorization", "proxyauthorization", "secret",
    "clientsecret", "password", "passwd", "credential", "cookie", "setcookie",
    "secretenvref",
}


def is_credential_field_name(value: Any) -> bool:
    """Identify fields that must never be persisted or serialized as metadata."""

    normalized = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    if normalized in _CREDENTIAL_FIELD_KEYS:
        return True
    return normalized.endswith(("password", "passwd", "secret", "accesstoken", "refreshtoken", "apikey"))


def redact_sensitive_text(value: Any, *secret_values: Any) -> str:
    """Remove resolved credentials and common authorization forms from diagnostics."""

    safe = str(value or "")
    for secret in secret_values:
        secret_text = str(secret or "")
        if secret_text:
            safe = safe.replace(secret_text, "[redacted]")
            if secret_text.lower().startswith("bearer "):
                token = secret_text[7:].strip()
                if token:
                    safe = safe.replace(token, "[redacted]")
    safe = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", safe)
    safe = re.sub(r"(?i)(api[_ -]?key[=: ]+)[^\s,;]+", r"\1[redacted]", safe)
    safe = re.sub(
        r"(?i)\b(?:env:)?PRONOIA_(?:MODEL|STRATEGY|DATA)_SECRET_[A-Z0-9_]+\b",
        "[secret reference hidden]",
        safe,
    )
    safe = re.sub(r"(?i)\blocal-(?:model|strategy):[0-9a-f]{32}\b", "[secret reference hidden]", safe)
    return safe


def validate_secret_env_name(value: Any, *, namespace: str, label: str) -> str:
    """Return a validated, purpose-scoped environment variable name.

    User-managed integrations must never be able to reference application
    credentials (for example ARK/MAAS keys), the share password, PATH, or any
    other process environment variable.  Separate namespaces also prevent a
    data connector from borrowing a strategy credential and vice versa.
    """

    try:
        prefix = _SECRET_ENV_PREFIXES[namespace]
    except KeyError as exc:
        raise ValueError(f"未知的密钥环境变量命名空间: {namespace}") from exc
    name = str(value or "").strip()
    suffix = name[len(prefix):] if name.startswith(prefix) else ""
    if not suffix or not _SECRET_ENV_SUFFIX_RE.fullmatch(suffix):
        raise ValueError(f"{label} 只能使用 {prefix}* 环境变量（后缀仅限 A-Z、0-9、下划线）")
    return name


def validate_secret_env_reference(value: Any, *, namespace: str, label: str) -> str:
    """Validate an ``env:NAME`` reference and return its variable name."""

    reference = str(value or "").strip()
    if not reference.startswith("env:"):
        prefix = _SECRET_ENV_PREFIXES.get(namespace, "PRONOIA_*_SECRET_")
        raise ValueError(f"{label} 只能使用 env:{prefix}* 引用；不能保存明文密钥")
    return validate_secret_env_name(reference[4:], namespace=namespace, label=label)


def resolve_secret_env_reference(value: Any, *, namespace: str, label: str) -> str:
    """Resolve a validated reference without permitting arbitrary env access."""

    name = validate_secret_env_reference(value, namespace=namespace, label=label)
    secret = os.getenv(name, "")
    if not secret:
        raise ValueError(f"{label} 引用的环境变量未设置")
    return secret


def allow_private_model_endpoints() -> bool:
    """Return whether the explicit local-development endpoint escape hatch is on."""

    return str(os.getenv("PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS") or "").strip().lower() in {
        "1", "true", "yes",
    }


def _parsed_endpoint(endpoint: Any, *, label: str):
    raw = str(endpoint or "").strip()
    try:
        parsed = urlparse(raw)
        hostname = parsed.hostname
        # Accessing ``port`` performs urllib's range/integer validation.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} endpoint 必须是有效的 http(s) 地址: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError(f"{label} endpoint 必须是有效的 http(s) 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} endpoint 不能在 URL 中包含用户名或密钥")
    if parsed.fragment:
        raise ValueError(f"{label} endpoint 不能包含 URL fragment")
    for query_key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = re.sub(r"[^a-z0-9]", "", query_key.lower())
        if normalized in _CREDENTIAL_QUERY_KEYS:
            raise ValueError(f"{label} endpoint 不能通过 URL 查询参数携带密钥")
    return raw, parsed, hostname.strip().lower()


def _is_local_name(host: str) -> bool:
    return host in {"localhost", "localhost.localdomain"} or host.endswith(".local")


def validate_registered_model_endpoint(
    endpoint: Any,
    *,
    label: str,
    allow_private: bool | None = None,
) -> str:
    """Apply the creation-time scheme and obvious-address policy.

    DNS is intentionally checked again at execution.  For HTTP, the explicit
    development escape hatch only accepts a target whose private nature is
    evident from localhost/.local naming or a literal non-public IP; it never
    turns into a blanket public-HTTP opt-in.
    """

    raw, parsed, host = _parsed_endpoint(endpoint, label=label)
    allow_private = allow_private_model_endpoints() if allow_private is None else allow_private
    local_name = _is_local_name(host)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    private_literal = address is not None and not address.is_global

    if parsed.scheme == "http":
        if not allow_private:
            if local_name or private_literal:
                raise ValueError(
                    f"{label} 默认禁止私网/回环 HTTP；可信开发模型需显式设置 "
                    "PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS=1"
                )
            raise ValueError(f"{label} 的公网 endpoint 必须使用 HTTPS")
        if not (local_name or private_literal):
            raise ValueError(
                "即使启用 PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS，"
                f"{label} 的公网 endpoint 仍必须使用 HTTPS；"
                "开发 HTTP endpoint 请使用 localhost、.local 或明确的私网/回环 IP"
            )
        return raw

    if not allow_private and local_name:
        raise ValueError(f"{label} endpoint 不允许指向本机或 .local 地址")
    if not allow_private and private_literal:
        raise ValueError(f"{label} endpoint 不允许指向私网、回环或保留地址")
    return raw


def validate_runtime_model_endpoint(
    endpoint: str,
    *,
    label: str,
    resolver: Callable[..., list[Any]] = socket.getaddrinfo,
    allow_private: bool | None = None,
) -> None:
    """Resolve and validate an endpoint immediately before each network hop."""

    _, parsed, host = _parsed_endpoint(endpoint, label=label)
    allow_private = allow_private_model_endpoints() if allow_private is None else allow_private
    try:
        addresses = {
            item[4][0]
            for item in resolver(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise ValueError(f"{label} endpoint 无法解析: {exc}") from exc
    if not addresses:
        raise ValueError(f"{label} endpoint 未解析到任何地址")
    try:
        non_global = [address for address in addresses if not ipaddress.ip_address(address).is_global]
    except ValueError as exc:
        raise ValueError(f"{label} endpoint 解析到非法地址: {exc}") from exc

    if parsed.scheme == "http":
        if not allow_private:
            if non_global:
                raise ValueError(
                    f"{label} 默认禁止私网/回环 HTTP；可信开发模型需显式设置 "
                    "PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS=1"
                )
            raise ValueError(f"{label} 的公网 endpoint 必须使用 HTTPS")
        # The development escape hatch only covers endpoints whose *every*
        # resolved address is non-public. Mixed/public DNS stays HTTPS-only.
        if len(non_global) != len(addresses):
            raise ValueError(
                "即使启用 PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS，"
                f"{label} 的公网 endpoint 仍必须使用 HTTPS"
            )
        return

    if non_global and not allow_private:
        raise ValueError(f"{label} endpoint 解析到私网、回环或保留地址")


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    default_port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port


class ValidatedModelRedirectHandler(HTTPRedirectHandler):
    """Validate every resolved Location before urllib opens the next request."""

    max_redirections = 5
    max_repeats = 2

    def __init__(
        self,
        validator: Callable[[str], None],
        *,
        sensitive_headers: set[str] | frozenset[str] = frozenset(),
    ):
        super().__init__()
        self._validator = validator
        self._sensitive_headers = {
            "authorization", "proxy-authorization", "cookie",
            *(str(item).lower() for item in sensitive_headers),
        }

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        # urllib has already resolved relative Location values at this point.
        self._validator(str(newurl))
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _origin(req.full_url) != _origin(str(newurl)):
            # urllib otherwise forwards every request header except Content-* to
            # another origin. Strip both conventional credentials and every
            # caller-declared env-backed header; the redirect itself remains
            # usable, but authentication must not silently cross trust domains.
            for header_name in list(redirected.headers):
                if header_name.lower() in self._sensitive_headers:
                    redirected.remove_header(header_name)
            for header_name in list(redirected.unredirected_hdrs):
                if header_name.lower() in self._sensitive_headers:
                    redirected.remove_header(header_name)
        return redirected
