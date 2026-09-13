"""Strategy adapter registry and compatibility normalization."""
from __future__ import annotations

import math
from typing import Any, Mapping

from ..model_endpoint_security import (
    validate_registered_model_endpoint,
    validate_secret_env_name,
)
from ..strategy_credentials import validate_strategy_secret_reference, validate_header_name


LEGACY_EVENT_RUNNERS = {"baseline", "team_prompt", "team_full", "provided_analysis"}
QUANT_KINDS = {"buy_hold", "ma_cross", "momentum", "declarative_rules", "signal_file", "return_forecast"}


def list_strategy_adapters() -> list[dict[str, Any]]:
    """Describe executable and planned adapters without claiming missing work."""
    return [
        {
            "type": "event", "adapter": "existing_platform", "engine_mode": "event_proxy",
            "status": "available", "runners": ["baseline", "team_prompt", "team_full"],
            "output_contract": "direction/confidence/rationale/expected_return_pct",
            "result_nature": "proxy",
        },
        {
            "type": "event", "adapter": "raw_model", "engine_mode": "event_proxy",
            "status": "available", "runners": ["raw_model"],
            "output_contract": "direction/confidence/rationale/expected_return_pct",
            "result_nature": "proxy", "contract_version": "raw-event-v1",
        },
        {
            "type": "event", "adapter": "imported_decisions", "engine_mode": "event_proxy",
            "status": "available", "runners": ["provided_analysis"],
            "output_contract": "direction/confidence/rationale/expected_return_pct",
            "result_nature": "proxy",
        },
        {
            "type": "event", "adapter": "external_http", "engine_mode": "event_proxy",
            "status": "available",
            "output_contract": "direction/confidence/rationale/expected_return_pct/asset/target_weight",
            "result_nature": "proxy",
        },
        {
            "type": "event", "adapter": "portfolio_signal_adapter", "engine_mode": "portfolio",
            "status": "pending", "reason": "等待冻结 bar 数据后把事件决策映射为 timestamp/asset/target_weight",
            "output_contract": "timestamp/asset/target_weight",
            "result_nature": "unavailable",
        },
        {
            "type": "quant", "adapter": "builtin", "engine_mode": "portfolio",
            "status": "available", "kinds": ["buy_hold", "ma_cross", "momentum", "declarative_rules", "return_forecast"],
            "output_contract": "timestamp/target_weight",
            "result_nature": "simulated_from_real_bars",
        },
        {
            "type": "quant", "adapter": "signal_file", "engine_mode": "portfolio",
            "status": "available", "kinds": ["signal_file"],
            "output_contract": "timestamp/target_weight",
            "result_nature": "simulated_from_real_bars",
        },
        {
            "type": "api", "adapter": "external_http", "engine_mode": "portfolio",
            "status": "available", "output_contract": "target_weight",
            "decision_timing": "signal_at_bar_close_execute_next_bar_open",
            "point_in_time_enforced": False,
            "result_nature": "unverified",
        },
    ]


def _validate_secret_refs(headers: Mapping[str, Any] | None) -> None:
    for key, value in dict(headers or {}).items():
        validate_header_name(str(key))
        validate_strategy_secret_reference(
            value,
            label=f"外部 API 请求头 {key!r}",
        )


def _validate_parameter_secret_ref(parameters: Mapping[str, Any] | None) -> None:
    value = dict(parameters or {}).get("secret_env_ref")
    if value:
        validate_secret_env_name(
            value,
            namespace="strategy",
            label="外部 API secret_env_ref",
        )


def _validate_public_endpoint(endpoint: Any) -> str:
    return validate_registered_model_endpoint(endpoint, label="外部策略")


def normalize_strategy_spec(
    strategy_spec: Mapping[str, Any] | None,
    *,
    legacy_runner: str | None,
    legacy_strategy_type: str | None,
) -> dict[str, Any]:
    """Return the canonical executable strategy contract or fail at creation."""
    spec = dict(strategy_spec or {})
    if not spec:
        runner = str(legacy_runner or "").strip()
        if runner not in LEGACY_EVENT_RUNNERS | {"raw_model"}:
            raise ValueError(
                "必须提供 strategy_spec，或使用受支持的旧 runner: "
                + ", ".join(sorted(LEGACY_EVENT_RUNNERS))
            )
        adapter = ("raw_model" if runner == "raw_model" else
                   "imported_decisions" if runner == "provided_analysis" else "existing_platform")
        spec = {
            "type": "event", "adapter": adapter, "runner": runner,
            "name": legacy_strategy_type or runner, "version": "legacy-v1", "parameters": {},
        }

    family = str(spec.get("type") or "").strip().lower()
    adapter = str(spec.get("adapter") or "").strip().lower()
    spec["type"] = family
    spec["adapter"] = adapter
    if not spec.get("version"):
        spec["version"] = "1"
    spec.setdefault("parameters", {})
    if not isinstance(spec.get("parameters"), dict):
        raise ValueError("strategy_spec.parameters 必须是对象")

    if family == "event":
        if adapter == "raw_model":
            if spec.get("runner") not in (None, "", "raw_model"):
                raise ValueError("独立事件模型仅支持 runner=raw_model")
            spec.update(runner="raw_model", engine_mode="event_proxy", result_nature="proxy")
            spec["contract_version"] = "raw-event-v1"
            return spec
        if adapter == "portfolio_signal_adapter":
            raise ValueError("事件到组合信号适配器尚未启用；需先提供冻结 bar 数据和可审计映射规则")
        if adapter == "external_http":
            spec["endpoint"] = _validate_public_endpoint(spec.get("endpoint"))
            _validate_secret_refs(spec.get("headers"))
            _validate_parameter_secret_ref(spec.get("parameters"))
            spec["runner"] = "event_external_http"
            spec["engine_mode"] = "event_proxy"
            spec["result_nature"] = "proxy"
            return spec
        if adapter not in {"existing_platform", "imported_decisions"}:
            raise ValueError(f"不支持的事件策略 adapter: {adapter or '(empty)'}")
        runner = str(spec.get("runner") or legacy_runner or "").strip()
        allowed = {"provided_analysis"} if adapter == "imported_decisions" else {
            "baseline", "team_prompt", "team_full"
        }
        if runner not in allowed:
            raise ValueError(f"adapter={adapter} 不支持 runner={runner or '(empty)'}")
        spec["runner"] = runner
        spec["engine_mode"] = "event_proxy"
        spec["result_nature"] = "proxy"
        return spec

    if family == "quant":
        kind = str(spec.get("kind") or spec.get("model_id") or "").strip().lower()
        effective_adapter = adapter or ("signal_file" if kind == "signal_file" else "builtin")
        if kind not in QUANT_KINDS:
            raise ValueError(
                f"不支持的量化策略 kind={kind or '(empty)'}；可用: " + ", ".join(sorted(QUANT_KINDS))
            )
        if effective_adapter == "builtin" and kind == "signal_file":
            effective_adapter = "signal_file"
        if effective_adapter not in {"builtin", "signal_file"}:
            raise ValueError(f"不支持的量化策略 adapter: {effective_adapter}")
        if kind == "signal_file" and not str(spec.get("path") or "").strip():
            raise ValueError("signal_file 策略必须提供 path")
        if kind == "return_forecast":
            source = str(spec.get("source") or "builtin")
            if source not in {"builtin", "signal_file", "external_http"}:
                raise ValueError("return_forecast source 必须是 builtin、signal_file 或 external_http")
            if source == "signal_file" and not str(spec.get("path") or "").strip():
                raise ValueError("收益预测文件必须提供 path")
            if source == "external_http":
                spec["endpoint"] = _validate_public_endpoint(spec.get("endpoint"))
                _validate_secret_refs(spec.get("headers"))
            spec.update(source=source, prediction_only=True,
                        point_in_time_enforced=source != "signal_file",
                        forecast_contract="pronoia.quant-return-forecast.v1")
        spec["adapter"] = effective_adapter
        spec["kind"] = kind
        spec["engine_mode"] = "portfolio"
        spec["result_nature"] = "simulated_from_real_bars"
        return spec

    if family == "api":
        if adapter != "external_http":
            raise ValueError("API 策略当前仅支持 adapter=external_http")
        if str(spec.get("output_contract") or "target_weight") != "target_weight":
            raise ValueError("API 组合回测当前只执行 output_contract=target_weight")
        spec["endpoint"] = _validate_public_endpoint(spec.get("endpoint") or spec.get("api_ref"))
        _validate_secret_refs(spec.get("headers"))
        _validate_parameter_secret_ref(spec.get("parameters"))
        spec["kind"] = "external_http"
        spec["engine_mode"] = "portfolio"
        # This adapter currently sends the complete bar history in one request.
        # The platform therefore cannot prove that returned weights were
        # generated without looking ahead. Keep it executable for exploration,
        # but freeze the non-PIT/unverified nature into the Run contract.
        spec["point_in_time_enforced"] = False
        spec["result_nature"] = "unverified"
        return spec

    raise ValueError("strategy_spec.type 必须是 event、quant 或 api")


def execution_engine_mode(spec: Mapping[str, Any]) -> str:
    return str(spec.get("engine_mode") or ("event_proxy" if spec.get("type") == "event" else "portfolio"))


def legacy_runner_for(spec: Mapping[str, Any]) -> str:
    if spec.get("type") == "event":
        return str(spec.get("runner") or "")
    if spec.get("type") == "api":
        return "external_http"
    return str(spec.get("kind") or "quant")


def normalize_portfolio_execution_spec(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Freeze the subset the current single-asset engine genuinely applies."""
    requested = dict(value or {})
    delay = str(requested.get("execution_delay") or requested.get("entry_rule") or "next_open").strip().lower()
    if delay not in {"next_open", "signal_close_execute_next_open"}:
        raise ValueError("portfolio 引擎当前只支持 signal close 后在 next_open 成交")
    price_field = str(requested.get("price_field") or "open").strip().lower()
    if price_field not in {"open", "next_open"}:
        raise ValueError("portfolio 引擎当前成交价仅支持 next_open")
    benchmark = str(requested.get("benchmark") or "dataset_default").strip()
    if benchmark not in {"dataset_default", "dataset_asset_buy_hold", "asset_buy_hold"}:
        raise ValueError("portfolio MVP 的 benchmark 仅支持 dataset_default（同一资产买入持有）")

    def number(*keys: str, default: float) -> float:
        raw: Any = None
        for key in keys:
            if requested.get(key) is not None:
                raw = requested[key]
                break
        if raw is None:
            raw = default
        try:
            parsed = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{keys[0]} 必须是数值") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError(f"{keys[0]} 必须是有限非负数")
        return parsed

    initial = number("initial_capital", "initial_value", default=1_000_000.0)
    if initial <= 0:
        raise ValueError("initial_capital 必须大于 0")
    max_abs_weight = number("max_abs_weight", "position_limit", default=1.0)
    if not 0 < max_abs_weight <= 10:
        raise ValueError("max_abs_weight 必须在 (0, 10] 内")
    annualization = requested.get("annualization_periods")
    if annualization is not None:
        try:
            annualization = int(annualization)
        except (TypeError, ValueError) as exc:
            raise ValueError("annualization_periods 必须是正整数") from exc
        if annualization <= 0:
            raise ValueError("annualization_periods 必须是正整数")
    applied = {
        "initial_capital": initial,
        "commission_bps": number("commission_bps", "fee_bps", "transaction_cost_bps", default=3.0),
        "slippage_bps": number("slippage_bps", default=2.0),
        "stamp_duty_bps": number("stamp_duty_bps", default=0.0),
        "other_cost_bps": number("other_cost_bps", "exchange_fee_bps", default=0.0),
        "minimum_commission": number("minimum_commission", "min_commission", default=0.0),
        "max_abs_weight": max_abs_weight,
        "allow_short": bool(requested.get("allow_short", True)),
        "annualization_periods": annualization,
        "execution_delay": "next_open",
        "signal_timing": "bar_close",
        "price_field": "next_open",
        "benchmark": "dataset_asset_buy_hold",
    }
    if requested.get("currency"):
        applied["currency"] = str(requested["currency"])
    for date_key in ("start_date", "end_date"):
        if requested.get(date_key) not in (None, ""):
            applied[date_key] = str(requested[date_key])
    inactive: dict[str, Any] = {}
    recognized = {
        "initial_capital", "initial_value", "commission_bps", "fee_bps", "transaction_cost_bps", "slippage_bps",
        "stamp_duty_bps", "other_cost_bps", "exchange_fee_bps", "minimum_commission", "min_commission",
        "max_abs_weight", "position_limit", "allow_short", "annualization_periods",
        "execution_delay", "entry_rule", "price_field", "start_date", "end_date",
        "currency", "benchmark",
    }
    for key, item in requested.items():
        if key not in recognized:
            inactive[key] = {"requested": item, "reason": "当前单资产 next-open 引擎未应用此字段"}
    return {"requested": requested, "applied": applied, "recorded_not_applied": inactive}
