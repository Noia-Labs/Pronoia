"""Strict HTTP adapter for user-managed event decision models."""
from __future__ import annotations

import json
import socket
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from ..model_endpoint_security import (
    ValidatedModelRedirectHandler,
    allow_private_model_endpoints,
    redact_sensitive_text,
    validate_runtime_model_endpoint,
)
from .models import EventRecord, TeamPrediction, finite_expected_return_pct, return_forecast_contract
from ..strategy_credentials import resolve_strategy_secret_reference, validate_header_name


def _allow_private() -> bool:
    return allow_private_model_endpoints()


def _validate_runtime_endpoint(endpoint: str) -> None:
    validate_runtime_model_endpoint(
        endpoint,
        label="event external_http",
        resolver=socket.getaddrinfo,
        allow_private=_allow_private(),
    )


class _ValidatedRedirectHandler(ValidatedModelRedirectHandler):
    """Re-apply the external-model SSRF policy to every redirect target."""

    def __init__(self, *, sensitive_headers: set[str] | frozenset[str] = frozenset()):
        super().__init__(_validate_runtime_endpoint, sensitive_headers=sensitive_headers)


def _event_payload(event: EventRecord) -> dict[str, Any]:
    from .market import resolve_benchmark

    row = event.to_dict()
    for key in (
        "analysis_direction", "analysis_confidence", "analysis_rationale", "analysis_horizon", "analysis_expected_return_pct",
        # Dataset-construction priors are deliberately label-side metadata. The
        # built-in LLM runner never sees them; external models must not either.
        "direction_prior", "event_strength",
    ):
        row.pop(key, None)
    row["benchmark"] = resolve_benchmark(event)
    return row


def _headers(spec: Mapping[str, Any]) -> dict[str, str]:
    output = {"Accept": "application/json", "Content-Type": "application/json"}
    for key, reference in dict(spec.get("headers") or {}).items():
        output[validate_header_name(str(key))] = resolve_strategy_secret_reference(
            reference,
            label=f"请求头 {key}",
        )
    return output


def _post(endpoint: str, payload: dict[str, Any], spec: Mapping[str, Any]) -> Any:
    _validate_runtime_endpoint(endpoint)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request_headers = _headers(spec)
    request = Request(endpoint, data=raw, headers=request_headers, method="POST")
    timeout = float(spec.get("timeout_seconds") or 30.0)
    if not 0 < timeout <= 300:
        raise ValueError("event external_http timeout_seconds 必须在 (0,300]")
    try:
        sensitive_headers = set(request_headers) - {"Accept", "Content-Type"}
        opener = build_opener(_ValidatedRedirectHandler(sensitive_headers=sensitive_headers))
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - every hop is validated
            response_raw = response.read(10_000_001)
    except HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        sensitive_values = [
            value for key, value in request_headers.items()
            if key not in {"Accept", "Content-Type"}
        ]
        raise ValueError(
            redact_sensitive_text(f"event external_http 返回 HTTP {exc.code}: {detail}", *sensitive_values)
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        sensitive_values = [
            value for key, value in request_headers.items()
            if key not in {"Accept", "Content-Type"}
        ]
        raise ValueError(
            redact_sensitive_text(f"event external_http 调用失败: {exc}", *sensitive_values)
        ) from exc
    if len(response_raw) > 10_000_000:
        raise ValueError("event external_http 响应超过 10 MB")
    try:
        decoded = json.loads(response_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("event external_http 未返回合法 JSON") from exc
    # An echoing service must not write its credential into a decision, error,
    # or exported run. Redact decoded strings (including escaped characters).
    sensitive_values = [value for key, value in request_headers.items() if key not in {"Accept", "Content-Type"}]
    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return redact_sensitive_text(value, *sensitive_values)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value
    return clean(decoded)


def _prediction(
    row: Any,
    *,
    event_id: str,
    run_id: str,
    model_version: str,
    target_horizon: str,
) -> TeamPrediction:
    from .evaluation_protocol import normalize_evaluation_horizon

    if not isinstance(row, Mapping):
        raise ValueError(f"event_id={event_id} 的 decision 必须是对象")
    returned_id = str(row.get("event_id") or event_id)
    if returned_id != event_id:
        raise ValueError(f"external_http 返回越界 event_id={returned_id}，期望 {event_id}")
    direction = str(row.get("direction") or row.get("pred_direction") or "").strip().lower()
    if direction not in {"up", "down", "neutral"}:
        raise ValueError(f"event_id={event_id} 缺少合法 direction")
    try:
        confidence = float(row.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"event_id={event_id} 缺少合法 confidence") from exc
    if not 0 <= confidence <= 1:
        raise ValueError(f"event_id={event_id} confidence 必须在 0..1")
    rationale = str(row.get("rationale") or "").strip()
    if not rationale:
        raise ValueError(f"event_id={event_id} 缺少 rationale")
    selected_horizon = normalize_evaluation_horizon(target_horizon)
    declared_horizon = normalize_evaluation_horizon(row.get("horizon") or selected_horizon)
    if declared_horizon != selected_horizon:
        raise ValueError(
            f"event_id={event_id} 返回 horizon={declared_horizon}，"
            f"但冻结评测窗口为 {selected_horizon}"
        )
    metadata = {
        key: row[key]
        for key in ("asset", "target_weight", "action", "expected_return", "stop_loss", "take_profit")
        if key in row
    }
    if "target_weight" in metadata:
        try:
            metadata["target_weight"] = float(metadata["target_weight"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"event_id={event_id} target_weight 不是数值") from exc
        metadata["target_weight_status"] = "recorded_not_applied_in_event_proxy"
    forecast = finite_expected_return_pct(row.get("expected_return_pct"))
    if row.get("expected_return_pct") is not None and forecast is None:
        raise ValueError(f"event_id={event_id} expected_return_pct 必须是有限数值或 null")
    return TeamPrediction(
        event_id=event_id, pred_direction=direction, run_id=run_id,
        model_version=model_version, confidence=confidence, rationale=rationale,
        abstain=bool(row.get("abstain") is True), horizon=selected_horizon,
        strategy_metadata=metadata or None,
        expected_return_pct=forecast,
    )


def _event_request_common(spec: Mapping[str, Any], target_horizon: str) -> dict[str, Any]:
    from .evaluation_protocol import normalize_evaluation_horizon

    selected_horizon = normalize_evaluation_horizon(target_horizon)
    return {
        "schema_version": "pronoia.event-model.v1",
        "decision_time_field": "available_time",
        "legacy_decision_time_field": "event_time",
        "evaluation_horizon": selected_horizon,
        "return_forecast_contract": return_forecast_contract(selected_horizon),
        "constraints": {
            "strict_as_of": True,
            "future_information_allowed": False,
            "direction_prior_exposed": False,
        },
        "parameters": dict(spec.get("parameters") or {}),
    }


def build_event_request(
    event: EventRecord,
    spec: Mapping[str, Any],
    target_horizon: str = "t3",
) -> dict[str, Any]:
    """The actual single-event contract, shared by runs and connection checks."""
    return {**_event_request_common(spec, target_horizon), "event": _event_payload(event)}


def run_external_event_strategy(
    events: Sequence[EventRecord],
    *,
    run_id: str,
    spec: Mapping[str, Any],
    model_version: str,
    target_horizon: str = "t3",
    before_request: Callable[[], None] | None = None,
    on_pred: Callable[[TeamPrediction], None] | None = None,
) -> list[TeamPrediction]:
    """Call each event serially and commit it before sending the next request.

    Exceptions propagate without retrying the provider. Successfully delivered
    callbacks remain durable in the caller, allowing resume to send only the
    unfinished events. Pause/cancel checks run immediately before every request;
    an HTTP request already in flight is bounded by the configured timeout.
    """
    from .evaluation_protocol import normalize_evaluation_horizon

    if not events:
        return []
    endpoint = str(spec.get("endpoint") or "").strip()
    selected_horizon = normalize_evaluation_horizon(target_horizon)
    output: list[TeamPrediction] = []

    def deliver(prediction: TeamPrediction) -> None:
        if on_pred is not None:
            on_pred(prediction)
        output.append(prediction)

    if bool(spec.get("batch")):
        if before_request is not None:
            before_request()
        response = _post(endpoint, {
            **_event_request_common(spec, selected_horizon),
            "events": [_event_payload(event) for event in events],
        }, spec)
        rows = response.get("decisions") if isinstance(response, Mapping) else None
        if not isinstance(rows, list):
            raise ValueError("批量 event external_http 响应必须包含 decisions 数组")
        by_id: dict[str, Any] = {}
        for row in rows:
            event_id = str(row.get("event_id") or "") if isinstance(row, Mapping) else ""
            if not event_id or event_id in by_id:
                raise ValueError("批量 event external_http 响应含空或重复 event_id")
            by_id[event_id] = row
        expected = {event.event_id for event in events}
        if set(by_id) != expected:
            raise ValueError("批量 event external_http 必须为每个请求事件返回且只能返回一条 decision")
        # A batch is one response: validate every row before committing any,
        # so a duplicate, out-of-scope or malformed decision rejects the batch.
        predictions = [_prediction(
            by_id[event.event_id], event_id=event.event_id, run_id=run_id,
            model_version=model_version, target_horizon=selected_horizon,
        ) for event in events]
        for prediction in predictions:
            deliver(prediction)
        return output

    for event in events:
        if before_request is not None:
            before_request()
        response = _post(endpoint, build_event_request(event, spec, selected_horizon), spec)
        row = response.get("decision") if isinstance(response, Mapping) and isinstance(response.get("decision"), Mapping) else response
        deliver(_prediction(
            row, event_id=event.event_id, run_id=run_id,
            model_version=model_version, target_horizon=selected_horizon,
        ))
    return output
