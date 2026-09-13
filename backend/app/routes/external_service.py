"""Configure and try one event on an external prediction service."""
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .. import db
from ..event_backtest import external_strategy
from ..event_backtest.application import load_events
from ..event_backtest.evaluation_protocol import normalize_evaluation_horizon
from ..event_backtest.models import EventRecord
from ..model_endpoint_security import redact_sensitive_text, validate_registered_model_endpoint, validate_secret_env_name
from ..strategy_credentials import discard_strategy_secret, store_strategy_secret, validate_header_name

router = APIRouter(prefix="/api/bt/external-service", tags=["backtest"])


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(max_length=4000)
    auth_mode: Literal["none", "bearer", "header", "env"] = "bearer"
    api_key: SecretStr = SecretStr("")
    header_name: str = Field(default="X-API-Key", max_length=128)
    secret_env_ref: str = Field(default="", max_length=256)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    version: str = Field(default="", max_length=80)


class ProbeRequest(ServiceConfig):
    dataset_id: str | None = None
    horizon: str = "t3"


def _event(dataset_id: str | None) -> tuple[EventRecord, bool]:
    if not dataset_id:
        return EventRecord.from_dict({
            "event_id": "connection-test-example", "market": "US", "symbol": "SPY",
            "event_time": "2025-01-10T14:00:00Z", "available_time": "2025-01-10T14:00:00Z",
            "event_type_l2": "接口测试", "title": "虚拟事件：仅验证接口格式",
            "event_text": "这是一条虚拟连接测试事件，请返回符合约定的预测结构。", "source_url": "",
        }), True
    dataset = db.get_bt_dataset(dataset_id)
    if not dataset or dataset.get("dataset_kind") == "market":
        raise ValueError("请选择有效的评测事件集")
    from .backtest import _resolve_path
    version_id = dataset.get("current_version") or dataset.get("dataset_version")
    version = db.get_bt_dataset_version(dataset_id, str(version_id)) if version_id else None
    path = _resolve_path((version or {}).get("path") or dataset.get("path"))
    if not path:
        raise ValueError("所选事件集没有可用的事件文件")
    events = load_events(path)
    if not events:
        raise ValueError("所选事件集为空")
    return events[0], False


def _spec(config: ServiceConfig) -> tuple[dict, str | None]:
    endpoint = validate_registered_model_endpoint(config.endpoint, label="第三方预测服务")
    headers: dict[str, str] = {}
    reference = None
    if config.auth_mode == "env":
        name = validate_secret_env_name(config.secret_env_ref, namespace="strategy", label="密钥变量名")
        headers[validate_header_name(config.header_name)] = "env:" + name
    elif config.auth_mode != "none":
        key = config.api_key.get_secret_value().strip()
        if not key:
            raise ValueError("请填写第三方预测服务的 API Key")
        header = "Authorization" if config.auth_mode == "bearer" else validate_header_name(config.header_name)
        value = key if config.auth_mode == "header" or key.lower().startswith("bearer ") else "Bearer " + key
        reference = store_strategy_secret(value)
        headers[header] = reference
    return {"endpoint": endpoint, "headers": headers, "timeout_seconds": config.timeout_seconds}, reference


@router.post("/credentials")
def save_credentials(config: ServiceConfig):
    try:
        spec, _ = _spec(config)
        return {"headers": spec["headers"]}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, detail=redact_sensitive_text(exc, config.api_key.get_secret_value())) from exc


@router.get("/example")
def request_example(dataset_id: str | None = None, horizon: str = Query("t3")):
    try:
        selected_horizon = normalize_evaluation_horizon(horizon)
        event, synthetic = _event(dataset_id)
        payload = external_strategy.build_event_request(event, {"parameters": {"evaluation_horizon": selected_horizon}}, selected_horizon)
        return {"synthetic": synthetic, "request": payload, "response": {
            "event_id": event.event_id, "direction": "up", "confidence": 0.75,
            "rationale": "格式示例，请由服务返回自己的预测理由。", "horizon": selected_horizon,
            "expected_return_pct": 2.0,
        }}
    except (ValueError, OSError) as exc:
        raise HTTPException(422, detail=redact_sensitive_text(exc)) from exc


@router.post("/test")
def test_service(config: ProbeRequest):
    reference = None
    sensitive_values: list[str] = []
    try:
        horizon = normalize_evaluation_horizon(config.horizon)
        event, synthetic = _event(config.dataset_id)
        spec, reference = _spec(config)
        sensitive_values = [value for key, value in external_strategy._headers(spec).items() if key not in {"Accept", "Content-Type"}]
        spec["parameters"] = {"evaluation_horizon": horizon}
        payload = external_strategy.build_event_request(event, spec, horizon)
        started = time.monotonic()
        response = external_strategy._post(spec["endpoint"], payload, spec)
        row = response.get("decision", response) if isinstance(response, dict) else response
        prediction = external_strategy._prediction(row, event_id=event.event_id, run_id="connection-test", model_version=config.version or "external-http-v1", target_horizon=horizon)
        # Only return the normalized decision, never arbitrary remote metadata.
        result = {"event_id": prediction.event_id, "direction": prediction.pred_direction,
                  "confidence": prediction.confidence, "rationale": prediction.rationale,
                  "horizon": prediction.horizon, "expected_return_pct": prediction.expected_return_pct}
        safe_result = {key: redact_sensitive_text(value, *sensitive_values, config.api_key.get_secret_value()) if isinstance(value, str) else value for key, value in result.items()}
        return {"ok": True, "synthetic": synthetic, "elapsed_seconds": round(time.monotonic() - started, 2), "prediction": safe_result}
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(422, detail=redact_sensitive_text(exc, *sensitive_values, config.api_key.get_secret_value())) from exc
    finally:
        if reference:
            discard_strategy_secret(reference)
