"""Deterministic comparison protocol fingerprints for backtest and Arena runs.

The strategy implementation is deliberately excluded from the fingerprint: Arena is
supposed to compare different strategies.  Only the data snapshot and evaluation /
execution assumptions are included, so equal hashes mean the runs are meaningfully
comparable under the same external protocol.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "pronoia-backtest-protocol-v2"
COMPARISON_PROTOCOL_VERSION = "pronoia-arena-comparison-v1"
ALIGNED_COMPARISON_PROTOCOL_VERSION = "pronoia-arena-comparison-v2-aligned-time"
EVENT_COMPARISON_PROTOCOL_VERSION = "pronoia-event-arena-comparison-v2"
EVENT_ALIGNED_COMPARISON_PROTOCOL_VERSION = "pronoia-event-arena-comparison-v3-aligned-time"
EVALUATOR_VERSION = "event-oracle-metrics-v2"
PORTFOLIO_EVALUATOR_VERSION = "single-asset-next-open-engine-v1"


def evaluator_version_for_engine(engine_mode: str | None) -> str:
    return PORTFOLIO_EVALUATOR_VERSION if str(engine_mode or "") == "portfolio" else EVALUATOR_VERSION


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def file_sha256(path: str | Path | None) -> str | None:
    """Return a content digest without loading a potentially large dataset in memory."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_DECISION_FIELDS = {
    "analysis_direction", "analysis_confidence", "analysis_rationale", "analysis_horizon",
    "pred_direction", "direction", "confidence", "rationale", "horizon", "action",
    "target_weight", "expected_return", "stop_loss", "take_profit", "direction_prior",
    "event_strength", "expected_return_pct", "analysis_expected_return_pct",
}

_PROTOCOL_METADATA_FIELDS = {
    # Human/database identity does not change the evaluated sample. The content
    # snapshot below is the source of truth, so cloned datasets remain comparable.
    "dataset_id", "dataset_name", "name", "description", "protocol_hash",
    # Execution assumptions are fingerprinted in their own canonical section.
    "execution_spec",
}


def normalize_evaluation_protocol(value: dict[str, Any] | None) -> dict[str, Any]:
    """Remove display/storage metadata from the semantic evaluation contract."""
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if key not in _PROTOCOL_METADATA_FIELDS
    }


def events_snapshot_sha256(path: str | Path | None) -> str | None:
    """Hash event facts/order while excluding submitted strategy decisions.

    This lets two analysts enter different opinions for the exact same events and
    still join a strict Arena. Invalid/non-JSONL files fall back to their raw digest.
    """
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with p.open("r", encoding="utf-8") as stream:
            for raw_line in stream:
                if not raw_line.strip():
                    continue
                row = json.loads(raw_line)
                if not isinstance(row, dict):
                    return file_sha256(p)
                facts = {key: value for key, value in row.items() if key not in _DECISION_FIELDS}
                digest.update(_canonical(facts).encode("utf-8"))
                digest.update(b"\n")
        return digest.hexdigest()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return file_sha256(p)


def build_protocol_hash(
    *,
    events_path: str | Path | None,
    labels_path: str | Path | None = None,
    execution_spec: dict[str, Any] | None = None,
    evaluation_protocol: dict[str, Any] | None = None,
    dataset_version: str | None = None,
    engine_mode: str | None = None,
) -> str:
    """Build a stable hash for all assumptions that must be equal in strict Arena mode."""
    payload = {
        "version": PROTOCOL_VERSION,
        "evaluator_version": evaluator_version_for_engine(engine_mode),
        "events_sha256": events_snapshot_sha256(events_path),
        "labels_sha256": file_sha256(labels_path),
        "execution_spec": execution_spec or {},
        "evaluation_protocol": normalize_evaluation_protocol(evaluation_protocol),
    }
    # Optional keys preserve the exact v2 fingerprint for legacy callers while
    # all new unified Runs freeze both benchmark version and execution family.
    if dataset_version:
        payload["dataset_version"] = str(dataset_version)
    if engine_mode:
        payload["engine_mode"] = str(engine_mode)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def recompute_protocol_hash_for_run(run: dict[str, Any]) -> str:
    """Recompute from current bytes; use this at execution/Arena boundaries."""
    config = run.get("config") if isinstance(run.get("config"), dict) else {}
    execution_spec = run.get("execution_spec")
    if not isinstance(execution_spec, dict):
        execution_spec = config.get("execution_spec") if isinstance(config.get("execution_spec"), dict) else {}
    if isinstance(execution_spec.get("applied"), dict):
        execution_spec = dict(execution_spec["applied"])
    evaluation_protocol = config.get("evaluation_protocol")
    if not isinstance(evaluation_protocol, dict):
        evaluation_protocol = config.get("protocol") if isinstance(config.get("protocol"), dict) else {}
    unified_contract = bool(run.get("dataset_version") or run.get("strategy_spec"))
    return build_protocol_hash(
        events_path=run.get("events_path"),
        labels_path=run.get("labels_path"),
        execution_spec=execution_spec,
        evaluation_protocol=evaluation_protocol,
        dataset_version=run.get("dataset_version") if unified_contract else None,
        engine_mode=run.get("engine_mode") if unified_contract else None,
    )


def protocol_hash_for_run(run: dict[str, Any]) -> str:
    """Return a persisted fingerprint, or derive one for a legacy run."""
    persisted = str(run.get("protocol_hash") or "").strip()
    return persisted or recompute_protocol_hash_for_run(run)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _run_execution_spec(run: dict[str, Any]) -> dict[str, Any]:
    config = _mapping(run.get("config"))
    value = run.get("execution_spec")
    if not isinstance(value, dict):
        value = config.get("execution_spec")
    execution = _mapping(value)
    return _mapping(execution.get("applied")) or execution


def _run_evaluation_protocol(run: dict[str, Any]) -> dict[str, Any]:
    config = _mapping(run.get("config"))
    value = config.get("evaluation_protocol")
    if not isinstance(value, dict):
        value = config.get("protocol")
    return _mapping(value)


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _number(value: Any, *, default: float) -> float:
    try:
        parsed = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        parsed = float(default)
    # A canonical float makes semantically equal representations (3, 3.0,
    # "3") hash identically. Invalid values are rejected at Run creation.
    return float(parsed)


def _boolean(value: Any, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "0", "no", "off"}:
            return False
        if normalized in {"true", "1", "yes", "on"}:
            return True
    return bool(value)


def _date(value: Any) -> str | None:
    """Canonicalise the inclusive comparison window at the platform's date granularity."""
    raw = str(value or "").strip()
    if not raw:
        return None
    # The current creation UI and both engines apply start/end as inclusive
    # trading dates. Keeping YYYY-MM-DD also makes an explicit full-coverage
    # window equal to the otherwise identical implicit full window.
    return raw.replace("/", "-")[:10]


def _frequency(value: Any) -> str | None:
    raw = str(value or "").strip().lower().replace("minute", "min")
    if not raw:
        return None
    aliases = {
        "d": "1d", "day": "1d", "daily": "1d",
        "w": "1w", "week": "1w", "weekly": "1w",
        "1min": "1m", "5min": "5m", "15min": "15m",
        "30min": "30m", "60min": "60m",
    }
    return aliases.get(raw, raw)


def _annualization_periods(frequency: str | None) -> int:
    raw = _frequency(frequency) or "1d"
    if raw == "1d":
        return 252
    if raw == "1w":
        return 52
    if raw in {"1mo", "month", "monthly"}:
        return 12
    if raw.endswith("m") and raw[:-1].isdigit():
        return max(1, round(252 * 240 / int(raw[:-1])))
    return 252


def comparison_signature_for_run(
    run: dict[str, Any],
    *,
    dataset_contract: dict[str, Any] | None = None,
    externalize_time_axis: bool = False,
    event_comparison_version: str | None = None,
) -> dict[str, Any]:
    """Return the canonical Arena fairness contract for one unified Run.

    This is deliberately narrower than :func:`build_protocol_hash`. The latter
    remains the immutable-input/tamper fingerprint for existing Runs. Arena's
    signature contains only the conditions that must be equal while comparing
    *different* strategies: frozen data, effective window/frequency/benchmark,
    applied execution timing/costs, evaluator family and result nature.

    A performance Arena with an explicit calendar-alignment contract may set
    ``externalize_time_axis``.  In that mode the source window, source/display
    frequency and their derived annualisation convention are intentionally
    removed from the equality gate: the Arena engine clips every genuine
    observation to its frozen common window and recomputes aligned metrics from
    the native curve.  Dataset content, benchmark, costs, execution timing,
    engine family and result nature remain strict fairness requirements.

    Strategy rules, model/provider, prompt, visibility, display metadata and
    mutable catalogue IDs never enter this signature.

    ``event_comparison_version`` is an internal replay option for an already
    saved Arena. Old v1 event comparisons keep their original version identity;
    new comparisons always use file-verified fact/Oracle equality.
    """
    config = _mapping(run.get("config"))
    execution = _run_execution_spec(run)
    evaluation = _run_evaluation_protocol(run)
    data_basis = _mapping(evaluation.get("data_basis"))
    if not data_basis:
        data_basis = _mapping(config.get("data_basis"))
    stored_snapshot = _mapping(config.get("dataset_snapshot"))
    contract = {**stored_snapshot, **_mapping(dataset_contract)}
    coverage = _mapping(contract.get("coverage")) or _mapping(stored_snapshot.get("coverage"))

    engine_mode = str(run.get("engine_mode") or config.get("engine_mode") or "event_proxy")
    dataset_version = str(
        run.get("dataset_version") or config.get("dataset_version")
        or contract.get("dataset_version") or contract.get("id") or ""
    ) or None
    if engine_mode == "event_proxy":
        # Imported opinions create distinct catalogue versions/provenance while
        # leaving the evaluated facts unchanged. The comparable identity must
        # come from actual frozen files, never a caller's claimed snapshot hash.
        # Full Run integrity still includes its original dataset_version.
        dataset_identity = {
            "snapshot_hash": events_snapshot_sha256(run.get("events_path")),
            "labels_sha256": file_sha256(run.get("labels_path")),
        }
        comparison_version = (
            EVENT_ALIGNED_COMPARISON_PROTOCOL_VERSION
            if externalize_time_axis else EVENT_COMPARISON_PROTOCOL_VERSION
        )
        if event_comparison_version in {
            COMPARISON_PROTOCOL_VERSION, ALIGNED_COMPARISON_PROTOCOL_VERSION,
        }:
            dataset_identity["dataset_version"] = dataset_version
            comparison_version = (
                ALIGNED_COMPARISON_PROTOCOL_VERSION
                if externalize_time_axis else COMPARISON_PROTOCOL_VERSION
            )
    else:
        dataset_identity = {
            "dataset_version": dataset_version,
            "snapshot_hash": str(
                contract.get("snapshot_hash") or config.get("snapshot_hash")
                or data_basis.get("snapshot_hash") or ""
            ) or events_snapshot_sha256(run.get("events_path")),
            "labels_sha256": None,
        }
        comparison_version = (
            ALIGNED_COMPARISON_PROTOCOL_VERSION
            if externalize_time_axis else COMPARISON_PROTOCOL_VERSION
        )

    start_date = _date(_first(
        execution.get("start_date"), evaluation.get("start_date"), evaluation.get("date_from"),
        config.get("start_date"), coverage.get("start_at"), coverage.get("start_date"),
    ))
    end_date = _date(_first(
        execution.get("end_date"), evaluation.get("end_date"), evaluation.get("date_to"),
        config.get("end_date"), coverage.get("end_at"), coverage.get("end_date"),
    ))
    frequency = _frequency(_first(
        contract.get("frequency"), stored_snapshot.get("frequency"), execution.get("frequency"),
        data_basis.get("frequency"), evaluation.get("frequency"), config.get("frequency"),
    ))

    result_nature = str(
        run.get("result_nature") or config.get("result_nature")
        or ("simulated_from_real_bars" if engine_mode == "portfolio" else "proxy")
    )
    if engine_mode == "portfolio":
        benchmark_raw = str(_first(execution.get("benchmark"), evaluation.get("benchmark")) or "dataset_asset_buy_hold")
        benchmark = (
            "dataset_asset_buy_hold"
            if benchmark_raw in {"dataset_default", "asset_buy_hold", "dataset_asset_buy_hold"}
            else benchmark_raw
        )
        timing = {
            # These are the *applied* semantics of the current portfolio engine,
            # not aliases or unsupported values a caller may have recorded.
            "signal_timing": str(execution.get("signal_timing") or "bar_close"),
            "execution_delay": "next_open",
            "price_field": "next_open",
        }
        execution_constraints: dict[str, Any] | None = {
            "max_abs_weight": _number(
                _first(execution.get("max_abs_weight"), execution.get("position_limit")),
                default=1.0,
            ),
            "allow_short": _boolean(execution.get("allow_short"), default=True),
        }
        costs: dict[str, Any] = {
            "commission_bps": _number(_first(execution.get("commission_bps"), execution.get("fee_bps")), default=3.0),
            "slippage_bps": _number(execution.get("slippage_bps"), default=2.0),
            "stamp_duty_bps": _number(execution.get("stamp_duty_bps"), default=0.0),
            "other_cost_bps": _number(_first(execution.get("other_cost_bps"), execution.get("exchange_fee_bps")), default=0.0),
            "minimum_commission": _number(_first(execution.get("minimum_commission"), execution.get("min_commission")), default=0.0),
        }
        # A flat minimum fee changes percentage returns as a function of account
        # size. Initial capital is otherwise a display scale and must not split a
        # percentage-return Arena.
        if costs["minimum_commission"] > 0:
            costs["initial_capital"] = _number(
                _first(execution.get("initial_capital"), execution.get("initial_value")),
                default=1_000_000.0,
            )
        annualization = int(_first(
            execution.get("annualization_periods"), _annualization_periods(frequency),
        ))
        evaluation_contract: dict[str, Any] = (
            {} if externalize_time_axis else {"annualization_periods": annualization}
        )
        strategy = _mapping(run.get("strategy_spec")) or _mapping(config.get("strategy_spec"))
        if str(strategy.get("kind") or "") == "return_forecast":
            # A forecast horizon is the target being measured, not a private
            # model parameter. Different lookbacks/models may compete, while
            # different N-bar targets must never share a formal MAE ranking.
            parameters = _mapping(strategy.get("parameters"))
            evaluation_contract["return_forecast_target"] = {
                "prediction_only": True,
                "basis": "asset_close_to_close_return", "forecast_unit": "percent",
                "error_unit": "percentage_points",
                "horizon_bars": int(parameters.get("horizon_bars") or 3),
                # Keep native frequency even when a performance Arena asks to
                # align display curves; 3 hourly bars is not 3 daily bars.
                "frequency": frequency,
            }
    else:
        # Event performance is an Oracle-window return proxy. Unsupported order
        # timing fields are intentionally ignored because they were not applied.
        benchmark_setting = str(_first(
            execution.get("benchmark"), evaluation.get("benchmark"), data_basis.get("benchmark"),
        ) or "event_snapshot_defined")
        resolved_benchmarks = evaluation.get("resolved_benchmarks")
        if benchmark_setting in {"dataset_default", "event_snapshot_defined"} and isinstance(resolved_benchmarks, list):
            resolved = sorted({str(item) for item in resolved_benchmarks if str(item)})
            benchmark = resolved[0] if len(resolved) == 1 else "event_snapshot:" + ",".join(resolved)
        else:
            benchmark = benchmark_setting
        timing = {
            "signal_timing": "event_available_time",
            "execution_delay": "information_close_window",
            "price_field": "oracle_close_window",
        }
        execution_constraints = None
        costs = {
            "fee_bps": _number(execution.get("fee_bps"), default=0.0),
            "slippage_bps": _number(execution.get("slippage_bps"), default=0.0),
        }
        evaluation_contract = {
            "evaluation_horizon": str(_first(
                evaluation.get("evaluation_horizon"), config.get("evaluation_horizon"),
                config.get("primary_oracle_horizon"), run.get("evaluation_horizon"), "t3",
            )).lower(),
        }

    signature = {
        "version": comparison_version,
        "dataset": dataset_identity,
        "benchmark": benchmark,
        "execution_timing": timing,
        "execution_constraints": execution_constraints,
        "costs": costs,
        "evaluation": {
            "evaluator_version": evaluator_version_for_engine(engine_mode),
            **evaluation_contract,
        },
        "engine_mode": engine_mode,
        "result_nature": result_nature,
    }
    if externalize_time_axis:
        signature["time_axis"] = {
            "comparison_basis": "arena_explicit_calendar_alignment",
            "source_window_externalized": True,
            "source_frequency_externalized": True,
        }
    else:
        signature["window"] = {"start_date": start_date, "end_date": end_date}
        signature["frequency"] = frequency
    return signature


def comparison_protocol_hash_for_run(
    run: dict[str, Any],
    *,
    dataset_contract: dict[str, Any] | None = None,
    externalize_time_axis: bool = False,
    event_comparison_version: str | None = None,
) -> str:
    """Return the strategy-independent Arena comparison fingerprint.

    Historical event Runs can use the same fact/Oracle comparison when their
    actual frozen files remain available. Their stored integrity fingerprints
    remain untouched. Legacy in-memory/portfolio callers without those files
    retain their previous fingerprint behavior; the Arena route separately
    rejects event Runs whose required snapshots are missing.
    """
    unified = bool(run.get("dataset_version") or run.get("strategy_spec"))
    event_files_available = (
        str(run.get("engine_mode") or "event_proxy") == "event_proxy"
        and events_snapshot_sha256(run.get("events_path")) is not None
        and file_sha256(run.get("labels_path")) is not None
    )
    legacy_event_replay = event_comparison_version in {
        COMPARISON_PROTOCOL_VERSION, ALIGNED_COMPARISON_PROTOCOL_VERSION,
    }
    if not unified and not externalize_time_axis and (not event_files_available or legacy_event_replay):
        return protocol_hash_for_run(run)
    signature = comparison_signature_for_run(
        run,
        dataset_contract=dataset_contract,
        externalize_time_axis=externalize_time_axis,
        event_comparison_version=event_comparison_version,
    )
    return hashlib.sha256(_canonical(signature).encode("utf-8")).hexdigest()
