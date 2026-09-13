"""Backtest API (P0): runs CRUD / start / cancel / SSE stream / metrics.

路由前缀: /api/bt
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import heapq
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from .. import db
from ..config import DATA_DIR, PROJECT_ROOT
from ..event_backtest.application import load_predictions
from ..event_backtest.metrics import compute_metrics
from ..event_backtest import orchestrator as orch
from ..model_endpoint_security import is_credential_field_name
from ..schemas import (
    ActionResponse,
    BacktestMetricsSnapshotList,
    BacktestRunResponse,
    BTDatasetResponse,
    CreateBacktestRunRequest,
    CreateCostScenarioRequest,
    CreateBTDatasetVersionRequest,
    CreateManualDatasetRequest,
    DeleteBacktestHistoryRequest,
    EventsCountResponse,
    ListBacktestRunsResponse,
    ListEventCatalogResponse,
    ListPredictionsResponse,
    PromptVariantItem,
    RegisterBTDatasetRequest,
    sse as _sse,
)

router = APIRouter(prefix="/api/bt", tags=["backtest"])

_INTERNAL_METRICS_ACCESS: ContextVar[bool] = ContextVar(
    "pronoia_internal_metrics_access", default=False,
)


_SHARE_MODE_VALUES = {"1", "true", "yes", "on", "public", "share"}
_PATH_RESPONSE_KEYS = {
    "path",
    "ref",
    "events_path",
    "labels_path",
    "out_path",
    "result_path",
    "ckpt_dir",
    "trajectory_ckpt",
    "trajectory_ckpt_dir",
}
_TEXT_RESPONSE_KEYS = {"detail", "error", "error_msg", "message", "reason"}


def _share_mode_enabled() -> bool:
    """Read share mode dynamically so launchers/tests can set it at runtime."""
    return str(os.getenv("PRONOIA_SHARE_MODE") or "").strip().lower() in _SHARE_MODE_VALUES


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _trusted_local_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    # User supplied paths must never reach source code, .env or the SQLite
    # database.  Keep the compatibility exception narrowly scoped to the two
    # project directories that are intentionally used for importable seeds.
    for candidate in (
        Path(DATA_DIR),
        Path(PROJECT_ROOT) / "backtesting",
        Path(PROJECT_ROOT) / "seed_data",
    ):
        resolved = candidate.expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _public_path_label(raw: str | Path | None) -> str | None:
    """Return a non-absolute display handle for a server-managed local path."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or not _share_mode_enabled():
        return value
    # Provider URLs and opaque dataset identifiers are not workstation paths.
    if "://" in value and not value.lower().startswith("file://"):
        return value
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return value
    resolved = candidate.resolve()
    data_root = Path(DATA_DIR).expanduser().resolve()
    project_root = Path(PROJECT_ROOT).expanduser().resolve()
    if _path_is_within(resolved, data_root):
        relative = resolved.relative_to(data_root).as_posix()
        return f"data://{relative}" if relative != "." else "data://"
    if _path_is_within(resolved, project_root):
        relative = resolved.relative_to(project_root).as_posix()
        return f"project://{relative}" if relative != "." else "project://"
    return "[redacted-local-path]"


_ABSOLUTE_PATH_IN_TEXT = re.compile(
    r"(?<![:/\\])(?:/[A-Za-z0-9_.~@+,%=():\-\u0080-\uffff]+)+/?"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*)"
)


def _share_safe_text(value: str) -> str:
    """Redact path fragments from error/status text returned by a shared API."""
    if not _share_mode_enabled():
        return value
    return _ABSOLUTE_PATH_IN_TEXT.sub("[redacted-local-path]", value)


def _share_safe_payload(value: Any, *, key: str | None = None) -> Any:
    """Recursively sanitize public DTOs while keeping persisted paths intact."""
    if isinstance(value, Mapping):
        return {
            str(child_key): _share_safe_payload(child, key=str(child_key))
            for child_key, child in value.items()
            if not is_credential_field_name(child_key)
        }
    if isinstance(value, (list, tuple)):
        return [_share_safe_payload(item) for item in value]
    if isinstance(value, str):
        from .. import config as app_config
        if app_config.LLM_API_KEY:
            value = value.replace(str(app_config.LLM_API_KEY), "[redacted]")
        safe = re.sub(
            r"(?i)\b(?:env:)?PRONOIA_(?:MODEL|STRATEGY|DATA)_SECRET_[A-Z0-9_]+\b",
            "[secret reference hidden]",
            value,
        )
        safe = re.sub(r"(?i)\blocal-(?:model|strategy):[0-9a-f]{32}\b", "[secret reference hidden]", safe)
        safe = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", safe)
        safe = re.sub(r"(?i)(api[_ -]?key[=: ]+)[^\s,;]+", r"\1[redacted]", safe)
        if _share_mode_enabled():
            if key in _PATH_RESPONSE_KEYS or str(key or "").endswith(("_path", "_dir")):
                return _public_path_label(safe)
            if Path(safe).expanduser().is_absolute():
                return _public_path_label(safe)
            if key in _TEXT_RESPONSE_KEYS:
                return _share_safe_text(safe)
        return safe
    return value


def _model_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump(exclude_none=True))
    return dict(value)


def _resolve_path(raw: str | None) -> str | None:
    """Resolve a local input path and enforce the public-sharing boundary."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    p = Path(s)
    if not _share_mode_enabled():
        # Preserve the historical local-first contract exactly: absolute input
        # is returned verbatim and relative input is rooted at PROJECT_ROOT.
        if p.is_absolute():
            return str(p)
        return str((PROJECT_ROOT / p).resolve())
    p = p.expanduser()
    resolved = p.resolve() if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    if not any(
        _path_is_within(resolved, root) for root in _trusted_local_roots()
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "message": "分享模式不允许访问受管数据或公开种子目录之外的本机路径",
                "hint": "请先将数据导入 FEVER_DATA_DIR、backtesting 或 seed_data，再通过 dataset_id 引用。",
            },
        )
    return str(resolved)


def _dataset_decision_contract(path: str | Path | None) -> tuple[bool, str]:
    """Validate every JSONL record required by the imported-decisions adapter."""
    if not path or not Path(path).is_file():
        return False, "events_only"
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        if not rows or any(not isinstance(row, dict) for row in rows):
            return False, "events_only"
        def valid(row: dict[str, Any]) -> bool:
            direction = row.get("analysis_direction")
            confidence = row.get("analysis_confidence", row.get("confidence"))
            rationale = row.get("analysis_rationale", row.get("rationale"))
            try:
                confidence_number = float(confidence)
            except (TypeError, ValueError):
                return False
            return (
                str(direction or "").strip().lower() in {"up", "down", "neutral"}
                and math.isfinite(confidence_number)
                and 0.0 <= confidence_number <= 1.0
                and bool(str(rationale or "").strip())
            )
        ready = all(valid(row) for row in rows)
        return ready, "event_plus_provided_analysis" if ready else "events_only"
    except (OSError, UnicodeError, json.JSONDecodeError, StopIteration, TypeError, ValueError):
        return False, "events_only"


def _event_benchmark_contract(path: str | Path | None) -> list[str]:
    """Resolve the stable per-market benchmark values represented by a dataset."""
    if not path or not Path(path).is_file():
        return []
    from ..event_backtest.application import load_events
    from ..event_backtest.market import resolve_benchmark

    return sorted({resolve_benchmark(event) for event in load_events(path)})


def _evaluation_selection_for_run(run: dict[str, Any]):
    """Load the run's event universe and apply its frozen inclusive date window."""
    from ..event_backtest.application import load_events
    from ..event_backtest.evaluation_protocol import (
        resolve_run_execution_spec,
        select_events_for_execution_window,
    )

    events_path = Path(str(run.get("events_path") or ""))
    events = load_events(events_path) if events_path.is_file() else []
    return select_events_for_execution_window(events, resolve_run_execution_spec(run))


def _is_arena_safe(run: dict[str, Any] | None) -> bool:
    """Whether a Run is intentionally published as aggregate-only."""
    if not run:
        return False
    value = str(run.get("visibility") or "").strip().lower().replace("-", "_")
    return value == "arena_safe"


def _primary_horizon_for_response(run: dict[str, Any]) -> str:
    """Expose the semantic horizon behind legacy ``*_t3`` prediction columns."""
    from ..event_backtest.evaluation_protocol import resolve_run_horizon

    return resolve_run_horizon(run)


def _require_event_level_access(run: dict[str, Any]) -> None:
    """Keep Arena-safe publications from leaking decisions through direct APIs."""
    if _is_arena_safe(run):
        raise HTTPException(
            status_code=403,
            detail="Arena 安全摘要仅开放聚合指标，事件、决策、Prompt 与执行轨迹已在服务端关闭。",
        )


_ARENA_SAFE_CURVE_MIN_BUCKET = 5

# The frozen portfolio artifact remains the complete audit record.  These caps
# only apply to the browser-oriented performance response: a multi-year minute
# run can otherwise expand into several duplicate 100k/500k-row arrays and a
# response hundreds of MiB in size.
_PORTFOLIO_PERFORMANCE_LIMITS: dict[str, int] = {
    "equity_curve": 2_000,
    "gross_equity_curve": 2_000,
    "drawdown_curve": 2_000,
    "asset_curve": 2_000,
    "benchmark_curve": 2_000,
    "bars": 2_000,
    "positions": 2_000,
    "signals": 2_000,
    "trades": 1_000,
    "orders": 1_000,
    "return_forecasts": 1_000,
}

_PORTFOLIO_PERFORMANCE_EXTREMA_FIELDS: dict[str, tuple[str, ...]] = {
    "equity_curve": ("net_value", "equity", "drawdown", "benchmark_net_value"),
    "gross_equity_curve": ("net_value", "equity", "drawdown"),
    "drawdown_curve": ("drawdown",),
    "asset_curve": ("net_value",),
    "benchmark_curve": ("net_value",),
    "bars": ("high", "low", "close", "volume"),
    "positions": ("weight", "target_weight", "equity", "net_value", "market_value"),
    "signals": ("target_weight", "expected_return_pct"),
    "trades": ("net_proxy_return", "realized_return", "pnl", "total_cost"),
    "orders": ("net_proxy_return", "realized_return", "pnl", "total_cost"),
    "return_forecasts": ("expected_return_pct", "actual_return_pct", "error_pct"),
}

_PORTFOLIO_TIMELINE_FIELDS = (
    "equity_curve", "gross_equity_curve", "drawdown_curve",
    "asset_curve", "benchmark_curve", "bars", "positions", "signals",
)

_ARENA_SAFE_FINANCIAL_ANALYSIS_FIELDS: dict[str, tuple[str, ...]] = {
    "units": ("returns_and_ratios", "money", "duration", "turnover"),
    "period": (
        "frequency", "bar_count", "return_observation_count",
        "annualization_periods", "estimated_years",
    ),
    "returns": (
        "initial_capital", "final_equity", "total_return", "annualized_return",
        "benchmark_total_return", "excess_total_return", "average_bar_return",
        "best_bar_return", "worst_bar_return",
    ),
    "risk": (
        "annualized_volatility", "annualized_downside_deviation", "sharpe_ratio",
        "sortino_ratio", "calmar_ratio", "historical_var_95_one_bar",
        "historical_cvar_95_one_bar", "max_drawdown", "reported_max_drawdown",
        "matches_reported_max_drawdown", "peak_to_trough_bars", "recovery_bars",
        "underwater_bars", "longest_underwater_bars", "current_drawdown",
        "recovered", "max_consecutive_positive_bars",
        "max_consecutive_negative_bars",
    ),
    "benchmark": (
        "aligned_return_count", "correlation", "beta", "annualized_alpha_rf0",
        "annualized_tracking_error", "information_ratio", "alignment_basis",
    ),
    "costs": (
        "total_cost", "total_commission", "total_slippage", "total_stamp_duty",
        "total_other_cost", "cost_drag_ratio_to_initial_capital",
        "cost_to_traded_notional_ratio", "average_cost_per_change",
        "maximum_cost_per_change",
    ),
    "methodology": (
        "risk_free_rate", "sharpe_basis", "sortino_basis", "var_basis",
        "cvar_basis", "alpha_basis", "trade_basis", "win_rate_basis",
        "source_arrays_returned",
    ),
}


def _performance_row_timestamp(row: Any, *, trade: bool = False) -> str | None:
    if not isinstance(row, Mapping):
        return None
    fields = (
        ("execution_timestamp", "entry_time", "timestamp", "date", "event_time")
        if trade else
        ("timestamp", "date", "event_time")
    )
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip().replace(" ", "T")
    return None


def _extrema_timestamps(rows: list[Any], fields: tuple[str, ...]) -> set[str]:
    """Return the real timestamps of finite global extrema in one series."""
    timestamps: set[str] = set()
    for field in fields:
        values: list[tuple[float, int]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            raw = row.get(field)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            numeric = float(raw)
            if math.isfinite(numeric):
                values.append((numeric, index))
        if values:
            for _, index in (min(values), max(values)):
                timestamp = _performance_row_timestamp(rows[index])
                if timestamp:
                    timestamps.add(timestamp)
    return timestamps


def _bounded_performance_rows(
    rows: list[Any],
    *,
    limit: int,
    extrema_fields: tuple[str, ...] = (),
    required_timestamps: set[str] | None = None,
) -> list[Any]:
    """Return deterministic display points while retaining endpoints/extrema."""
    if len(rows) <= limit:
        return list(rows)
    if limit <= 0:
        return []
    if limit == 1:
        return [rows[0]]

    selected = {0, len(rows) - 1}
    if required_timestamps:
        for index, row in enumerate(rows):
            timestamp = _performance_row_timestamp(row)
            if timestamp in required_timestamps:
                selected.add(index)
    for field in extrema_fields:
        values: list[tuple[float, int]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            raw = row.get(field)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            numeric = float(raw)
            if math.isfinite(numeric):
                values.append((numeric, index))
        if values:
            selected.add(min(values)[1])
            selected.add(max(values)[1])

    # The production limits leave ample room for all extrema anchors.  Keep
    # the helper well-defined for smaller test/custom limits too.
    if len(selected) > limit:
        interior = sorted(selected - {0, len(rows) - 1})
        selected = {0, len(rows) - 1, *interior[: max(0, limit - 2)]}

    # Repeatedly split the largest unrepresented time/index gap.  This yields
    # stable, broadly even coverage without scanning or returning every row.
    gaps: list[tuple[int, int, int]] = []

    def add_gap(left: int, right: int) -> None:
        if right - left > 1:
            heapq.heappush(gaps, (-(right - left), left, right))

    ordered = sorted(selected)
    for left, right in zip(ordered, ordered[1:]):
        add_gap(left, right)
    while len(selected) < limit and gaps:
        _, left, right = heapq.heappop(gaps)
        middle = (left + right) // 2
        if middle in selected:
            continue
        selected.add(middle)
        add_gap(left, middle)
        add_gap(middle, right)
    return [rows[index] for index in sorted(selected)]


def _bound_portfolio_performance_response(result: Mapping[str, Any]) -> dict[str, Any]:
    """Bound display-only arrays without changing full-result metrics/audit."""
    bounded = dict(result)
    series_metadata: dict[str, dict[str, Any]] = {}
    any_sampled = False

    # Sample executions first, then force their exact execution bars into all
    # portfolio timeline series. Global extrema from every curve are shared as
    # anchors too. This keeps independently derived series aligned at minute
    # precision and prevents a trade from being drawn on another bar that merely
    # happens to fall on the same calendar day.
    trade_rows = result.get("trades") if isinstance(result.get("trades"), list) else []
    trade_limit = _PORTFOLIO_PERFORMANCE_LIMITS["trades"]
    display_trades = _bounded_performance_rows(
        trade_rows,
        limit=trade_limit,
        extrema_fields=_PORTFOLIO_PERFORMANCE_EXTREMA_FIELDS["trades"],
    )
    trade_timestamps = {
        timestamp
        for row in display_trades
        if (timestamp := _performance_row_timestamp(row, trade=True))
    }
    shared_timestamps = set(trade_timestamps)
    for field in _PORTFOLIO_TIMELINE_FIELDS:
        raw = result.get(field)
        rows = raw if isinstance(raw, list) else []
        shared_timestamps.update(
            _extrema_timestamps(rows, _PORTFOLIO_PERFORMANCE_EXTREMA_FIELDS.get(field, ()))
        )

    prebounded: dict[str, list[Any]] = {"trades": display_trades}
    for field in _PORTFOLIO_TIMELINE_FIELDS:
        raw = result.get(field)
        rows = raw if isinstance(raw, list) else []
        prebounded[field] = _bounded_performance_rows(
            rows,
            limit=_PORTFOLIO_PERFORMANCE_LIMITS[field],
            extrema_fields=_PORTFOLIO_PERFORMANCE_EXTREMA_FIELDS.get(field, ()),
            required_timestamps=shared_timestamps,
        )

    for field, limit in _PORTFOLIO_PERFORMANCE_LIMITS.items():
        raw = result.get(field)
        rows = raw if isinstance(raw, list) else []
        if field in prebounded:
            display_rows = prebounded[field]
        else:
            display_rows = _bounded_performance_rows(
                rows,
                limit=limit,
                extrema_fields=_PORTFOLIO_PERFORMANCE_EXTREMA_FIELDS.get(field, ()),
                required_timestamps=trade_timestamps if field == "orders" else None,
            )
        sampled = len(display_rows) < len(rows)
        any_sampled = any_sampled or sampled
        bounded[field] = display_rows
        series_metadata[field] = {
            "total_count": len(rows),
            "returned_count": len(display_rows),
            "omitted_count": len(rows) - len(display_rows),
            "limit": limit,
            "sampled": sampled,
        }
    bounded["response_sampling"] = {
        "scope": "display_only",
        "method": "endpoints_global_extrema_even_v1",
        "applied": any_sampled,
        "source_result_preserved": True,
        "metrics_basis": "full_frozen_result",
        "preserves": ["first", "last", "finite_global_extrema"],
        "series": series_metadata,
    }
    return bounded


def _sanitize_arena_safe_curve(
    points: Any,
    *,
    min_bucket: int = _ARENA_SAFE_CURVE_MIN_BUCKET,
) -> list[dict[str, Any]]:
    """Remove per-event deltas and retain only bucketed aggregate checkpoints."""
    if not isinstance(points, list) or not points:
        return []
    bucket = max(_ARENA_SAFE_CURVE_MIN_BUCKET, int(min_bucket))
    final_position = len(points) - 1
    keep_positions = {0, final_position}
    keep_positions.update(range(bucket, final_position, bucket))
    redacted: list[dict[str, Any]] = []
    for position, point in enumerate(points):
        if position not in keep_positions or not isinstance(point, dict):
            continue
        safe_point: dict[str, Any] = {
            "index": point.get("index", position),
        }
        for key in ("net_value", "drawdown"):
            if key in point:
                safe_point[key] = point[key]
        redacted.append(safe_point)
    return redacted


def _sanitize_arena_safe_financial_analysis(value: Any) -> dict[str, Any]:
    """Keep aggregate portfolio facts while excluding strategy-timing details.

    Use an explicit allow-list so later additions to the private financial
    contract cannot silently become public in Arena-safe responses.  Exact
    period/drawdown/benchmark timestamps, trading activity and exposure are
    intentionally absent.
    """
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key in ("schema_version", "status", "reason", "metrics_basis"):
        if key in value:
            safe[key] = value[key]
    for section, allowed_fields in _ARENA_SAFE_FINANCIAL_ANALYSIS_FIELDS.items():
        source = value.get(section)
        if not isinstance(source, Mapping):
            continue
        safe[section] = {
            field: source[field]
            for field in allowed_fields
            if field in source
        }
    return safe


_ARENA_SAFE_PERFORMANCE_SUMMARY_FIELDS = (
    "initial_value", "final_value", "total_return", "gross_total_return",
    "annualized_return", "annualized_volatility", "asset_total_return",
    "benchmark_total_return", "excess_total_return", "max_drawdown",
    "sharpe_ratio", "sharpe_proxy", "sortino_ratio", "calmar_ratio",
    "bar_count", "fee_bps", "slippage_bps", "round_trip_cost_bps",
    "total_cost_rate_sum", "total_cost_amount_proxy", "total_commission",
    "total_slippage", "total_stamp_duty", "total_other_cost", "total_cost",
    "cost_drag_ratio",
)

_ARENA_SAFE_RETURN_FORECAST_FIELDS = (
    "status", "horizon_bars", "n_forecasts", "evaluated_count",
    "pending_count", "coverage", "mae_pct", "rmse_pct", "bias_pct",
    "evaluation_coverage", "eligible_evaluation_coverage", "eligible_count",
    "median_abs_error_pct", "p90_abs_error_pct", "error_std_pct",
    "mean_expected_return_pct", "mean_actual_return_pct",
    "direction_accuracy", "direction_evaluated_count", "direction_coverage",
    "up_call_hit_rate", "up_call_count", "down_call_hit_rate", "down_call_count",
    "pearson_ic", "spearman_rank_ic", "r_squared", "zero_baseline_rmse_pct",
    "skill_score_vs_zero", "statistics_basis", "direction_definition",
    "skill_score_definition", "unit", "forecast_unit", "horizon",
    "basis", "actual_field", "error_definition",
)

_ARENA_SAFE_METRIC_IDS = (
    "abstain_rate", "acc_avg_all_strict", "acc_avg_long_strict",
    "acc_avg_mid_strict", "acc_avg_short_strict", "acc_consensus66_strict",
    "acc_high_confidence", "acc_primary_directional_trade",
    "acc_primary_non_neutral", "acc_primary_strict", "acc_t1_strict",
    "acc_t3_strict", "acc_t5_strict", "acc_t7_strict", "acc_t15_strict",
    "acc_t30_strict", "acc_t60_strict", "avg_car_primary",
    "calibration_mse", "coverage_rate", "direction_bias",
    "prior_alignment_rate", "return_forecast", "strategy_total_return",
    "strategy_max_drawdown", "strategy_sharpe_proxy",
)

_ARENA_SAFE_METRIC_META_FIELDS = (
    "status", "n", "k", "n_valid", "n_total", "n_covered", "n_abstain",
    "evaluated_count", "n_predictions", "n_forecasts", "eligible_count",
    "n_event_trades", "active_bar_count", "active_period_count", "n_active",
    "pred_total", "oracle_total", "total_n",
    "pending_count", "coverage", "evaluation_coverage", "mae_pct",
    "eligible_evaluation_coverage", "median_abs_error_pct", "p90_abs_error_pct",
    "error_std_pct", "rmse_pct", "bias_pct", "mean_expected_return_pct",
    "mean_actual_return_pct", "direction_accuracy", "direction_evaluated_count",
    "direction_coverage", "up_call_hit_rate", "up_call_count",
    "down_call_hit_rate", "down_call_count", "pearson_ic", "spearman_rank_ic",
    "r_squared", "zero_baseline_rmse_pct", "skill_score_vs_zero",
    "statistics_basis", "direction_definition", "skill_score_definition",
    "unit", "forecast_unit", "horizon",
    "horizon_bars", "primary_horizon", "basis", "error_definition",
)


def _allowlisted_mapping(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {field: value[field] for field in fields if field in value}


def _sanitize_arena_safe_metric_entry(value: Any) -> dict[str, Any]:
    """Return one aggregate metric without future nested-detail leakage."""
    if not isinstance(value, Mapping):
        return {}
    safe = _allowlisted_mapping(
        value,
        ("value", "display_name", "description", "tier", "higher_is_better"),
    )
    meta = _allowlisted_mapping(value.get("meta"), _ARENA_SAFE_METRIC_META_FIELDS)
    if meta:
        safe["meta"] = meta
    breakdown = value.get("breakdown")
    if isinstance(breakdown, Mapping):
        wilson = _allowlisted_mapping(breakdown.get("wilson"), ("lo_95", "hi_95"))
        if wilson:
            safe["breakdown"] = {"wilson": wilson}
    return safe


def _arena_safe_count(value: Any) -> int | None:
    """Return a trustworthy non-negative integer count, never treating bool as one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return int(numeric)


def _arena_safe_metric_sample_count(
    metric_id: str,
    value: Any,
    *,
    all_metrics: Mapping[str, Any] | None = None,
) -> int | None:
    """Return the observations that actually determine one published metric.

    Total events/bars are not a privacy denominator when only one prediction,
    event trade, or active portfolio bar contributed to the displayed value.
    Unknown effective counts intentionally fail closed.
    """
    if not isinstance(value, Mapping):
        return None
    meta = value.get("meta") if isinstance(value.get("meta"), Mapping) else value

    def first(*fields: str) -> int | None:
        for field in fields:
            count = _arena_safe_count(meta.get(field))
            if count is not None:
                return count
        return None

    if metric_id.startswith("strategy_"):
        count = first("n_event_trades", "active_bar_count", "active_period_count", "n_active")
        if count is not None:
            return count
        portfolio_metric = (
            all_metrics.get("portfolio_metrics")
            if isinstance(all_metrics, Mapping) else None
        )
        portfolio_meta = (
            portfolio_metric.get("meta")
            if isinstance(portfolio_metric, Mapping)
            and isinstance(portfolio_metric.get("meta"), Mapping)
            else {}
        )
        for field in ("active_bar_count", "active_period_count", "n_active"):
            count = _arena_safe_count(portfolio_meta.get(field))
            if count is not None:
                return count
        # New portfolio metrics identify their method.  A plain bar count is
        # not an effective sample count when most bars may have zero exposure.
        if str(meta.get("method") or "") == "single_asset_next_open_portfolio":
            return None
        return first("n")

    if metric_id == "return_forecast":
        return first("evaluated_count", "n")
    if metric_id == "direction_bias":
        pred_count = first("pred_total")
        oracle_count = first("oracle_total")
        if pred_count is None:
            return None
        return min(pred_count, oracle_count) if oracle_count is not None else pred_count
    if metric_id == "coverage_rate":
        return first("n_total", "n")
    if metric_id == "abstain_rate":
        return first("n", "n_total")
    return first("n", "n_valid", "total_n", "eligible_count")


def _sanitize_arena_safe_metrics(value: Any) -> dict[str, Any]:
    """Strict metric-id allow-list shared by HTTP metrics and live updates."""
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for metric_id in _ARENA_SAFE_METRIC_IDS:
        metric = value.get(metric_id)
        if not isinstance(metric, Mapping):
            continue
        sample_count = _arena_safe_metric_sample_count(
            metric_id,
            metric,
            all_metrics=value,
        )
        if sample_count is None or sample_count < _ARENA_SAFE_CURVE_MIN_BUCKET:
            continue
        safe[metric_id] = _sanitize_arena_safe_metric_entry(metric)
    return safe


def _arena_safe_metrics_observation_count(value: Mapping[str, Any]) -> int | None:
    for field in ("n_outputs", "total", "n_total"):
        count = _arena_safe_count(value.get(field))
        if count is not None:
            return count
    return None


def _arena_safe_performance_observation_count(value: Mapping[str, Any]) -> int | None:
    summary = value.get("summary") if isinstance(value.get("summary"), Mapping) else {}
    engine_mode = str(value.get("engine_mode") or "event_proxy")
    if engine_mode != "portfolio":
        return _arena_safe_count(summary.get("n_trades"))
    if value.get("prediction_only") is True:
        # A pure forecaster has no strategy exposure.  Its public asset/
        # benchmark context is determined by market bars, not fake trades.
        return _arena_safe_count(summary.get("bar_count"))

    finance = (
        value.get("financial_analysis")
        if isinstance(value.get("financial_analysis"), Mapping) else {}
    )
    trading = finance.get("trading") if isinstance(finance.get("trading"), Mapping) else {}
    for field in ("active_bar_count", "active_period_count"):
        count = _arena_safe_count(trading.get(field))
        if count is not None:
            return count
    # Compatibility with result artifacts that expose the engine's effective
    # activity count in their summary.  Do not fall back to total bar_count.
    for field in ("active_bar_count", "active_period_count"):
        count = _arena_safe_count(summary.get(field))
        if count is not None:
            return count
    return None


def _arena_safe_return_forecast_observation_count(value: Mapping[str, Any]) -> int | None:
    forecast = value.get("return_forecast")
    if not isinstance(forecast, Mapping):
        return None
    for field in ("evaluated_count", "n"):
        count = _arena_safe_count(forecast.get(field))
        if count is not None:
            return count
    return None


def _arena_safe_prediction_observation_count(metrics: Mapping[str, Any]) -> int | None:
    counts: list[int] = []
    for metric_id in (
        "acc_t3_strict", "acc_primary_strict", "acc_primary_non_neutral",
        "acc_primary_directional_trade", "return_forecast",
    ):
        metric = metrics.get(metric_id)
        if not isinstance(metric, Mapping):
            continue
        count = _arena_safe_metric_sample_count(
            metric_id,
            metric,
            all_metrics=metrics,
        )
        if count is not None:
            counts.append(count)
    return max(counts) if counts else None


def _arena_artifact_present(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        path = Path(raw)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _arena_safe_public_comparison(run: Mapping[str, Any]) -> dict[str, Any]:
    """Publish a strategy-independent fairness key without its time window."""
    signature = run.get("comparison_signature")
    if not isinstance(signature, Mapping):
        return {}
    dataset = _allowlisted_mapping(
        signature.get("dataset"),
        ("dataset_version", "snapshot_hash", "labels_sha256"),
    )
    timing = _allowlisted_mapping(
        signature.get("execution_timing"),
        ("signal_timing", "execution_delay", "price_field"),
    )
    constraints = _allowlisted_mapping(
        signature.get("execution_constraints"),
        ("max_abs_weight", "allow_short"),
    )
    costs = _allowlisted_mapping(
        signature.get("costs"),
        (
            "fee_bps", "commission_bps", "slippage_bps", "stamp_duty_bps",
            "other_cost_bps", "minimum_commission", "initial_capital",
        ),
    )
    evaluation = _allowlisted_mapping(
        signature.get("evaluation"),
        ("evaluator_version", "evaluation_horizon"),
    )
    raw_evaluation = signature.get("evaluation")
    if isinstance(raw_evaluation, Mapping):
        forecast_target = _allowlisted_mapping(
            raw_evaluation.get("return_forecast_target"),
            (
                "prediction_only", "basis", "forecast_unit", "error_unit",
                "horizon_bars", "frequency",
            ),
        )
        if forecast_target:
            evaluation["return_forecast_target"] = forecast_target
    return {
        "version": signature.get("version"),
        "dataset": dataset,
        "benchmark": signature.get("benchmark"),
        "execution_timing": timing,
        "execution_constraints": constraints or None,
        "costs": costs,
        "evaluation": evaluation,
        "engine_mode": signature.get("engine_mode"),
        "result_nature": signature.get("result_nature"),
        "comparison_protocol_hash": run.get("comparison_protocol_hash"),
    }


def _arena_safe_eligibility(run: Mapping[str, Any]) -> dict[str, Any]:
    """Derive browser-safe Arena gates without exposing local artifact paths."""
    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    strategy = run.get("strategy_spec")
    if not isinstance(strategy, Mapping):
        strategy = config.get("strategy_spec") if isinstance(config.get("strategy_spec"), Mapping) else {}
    metrics = run.get("metrics") if isinstance(run.get("metrics"), Mapping) else {}
    forecast_metric = metrics.get("return_forecast") if isinstance(metrics.get("return_forecast"), Mapping) else {}
    forecast_meta = forecast_metric.get("meta") if isinstance(forecast_metric.get("meta"), Mapping) else forecast_metric
    portfolio_metric = metrics.get("portfolio_metrics") if isinstance(metrics.get("portfolio_metrics"), Mapping) else {}
    portfolio_meta = portfolio_metric.get("meta") if isinstance(portfolio_metric.get("meta"), Mapping) else {}

    prediction_only = bool(
        strategy.get("prediction_only") is True
        or str(strategy.get("kind") or "") == "return_forecast"
        or str(run.get("runner") or "") == "return_forecast"
        or config.get("prediction_only") is True
        or portfolio_meta.get("prediction_only") is True
    )
    forecast_count = _arena_safe_count(forecast_meta.get("n_forecasts"))
    evaluated_count = _arena_safe_metric_sample_count(
        "return_forecast",
        forecast_metric,
        all_metrics=metrics,
    )
    return_forecast_sample_sufficient = bool(
        evaluated_count is not None
        and evaluated_count >= _ARENA_SAFE_CURVE_MIN_BUCKET
    )
    numeric_forecast_present = bool(
        isinstance(forecast_count, (int, float)) and math.isfinite(float(forecast_count))
        and float(forecast_count) > 0
    ) or bool(
        isinstance(forecast_metric.get("value"), (int, float))
        and math.isfinite(float(forecast_metric["value"]))
        and isinstance(evaluated_count, (int, float))
        and float(evaluated_count) > 0
    )
    # A single numeric prediction must not make an Arena-safe Run selectable
    # for forecast comparison or advertise a public, scoreable forecast.
    numeric_forecast = bool(numeric_forecast_present and return_forecast_sample_sufficient)

    performance_count = _arena_safe_metric_sample_count(
        "strategy_total_return",
        metrics.get("strategy_total_return"),
        all_metrics=metrics,
    )
    prediction_count = _arena_safe_prediction_observation_count(metrics)
    performance_sample_sufficient = bool(
        performance_count is not None
        and performance_count >= _ARENA_SAFE_CURVE_MIN_BUCKET
    )
    forecast_sample_sufficient = bool(
        prediction_count is not None
        and prediction_count >= _ARENA_SAFE_CURVE_MIN_BUCKET
    )

    engine_mode = str(run.get("engine_mode") or "event_proxy")
    result_nature = str(run.get("result_nature") or "")
    adapter = str(strategy.get("adapter") or strategy.get("kind") or run.get("runner") or "")
    external_portfolio = engine_mode == "portfolio" and (
        str(strategy.get("type") or run.get("strategy_type") or "") == "api"
        or adapter == "external_http"
    )
    point_in_time_enforced = strategy.get("point_in_time_enforced") is True
    has_event_snapshot = _arena_artifact_present(run.get("events_path"))
    has_labels_snapshot = _arena_artifact_present(run.get("labels_path"))
    has_result_artifact = _arena_artifact_present(
        run.get("result_path") if engine_mode == "portfolio" else run.get("out_path")
    )

    comparison = _arena_safe_public_comparison(run)
    comparison_dataset = comparison.get("dataset") if isinstance(comparison.get("dataset"), Mapping) else {}
    event_content_verified = bool(
        engine_mode == "event_proxy"
        and re.fullmatch(r"[a-fA-F0-9]{64}", str(comparison_dataset.get("snapshot_hash") or ""))
        and re.fullmatch(r"[a-fA-F0-9]{64}", str(comparison_dataset.get("labels_sha256") or ""))
    )

    model_lab = config.get("model_lab") if isinstance(config.get("model_lab"), Mapping) else {}
    source = str(config.get("source") or config.get("origin") or model_lab.get("source") or "")
    source = source.strip().lower().replace("-", "_")
    is_model_lab = bool(model_lab) or source == "model_lab" or any(
        key in config for key in ("model_lab_batch_id", "model_lab_task_id")
    )
    model_lab_demo = is_model_lab and any(
        value is True or str(value or "").strip().lower() in {"demo", "dry_run", "demo_only"}
        for value in (
            config.get("dry_run"), config.get("demo"), config.get("result_mode"),
            model_lab.get("dry_run"), model_lab.get("demo"), model_lab.get("result_mode"),
        )
    )
    dataset_snapshot = config.get("dataset_snapshot") if isinstance(config.get("dataset_snapshot"), Mapping) else {}
    semantic_quality = (
        dataset_snapshot.get("semantic_quality")
        if isinstance(dataset_snapshot.get("semantic_quality"), Mapping) else {}
    )
    demo_only_dataset = str(semantic_quality.get("status") or "") == "demo_only"
    supported = engine_mode in {"event_proxy", "portfolio"}
    auditable = (
        run.get("status") == "done"
        and supported
        and result_nature != "unavailable"
        and not model_lab_demo
        and (
            (engine_mode == "event_proxy" and has_event_snapshot and has_labels_snapshot)
            or (engine_mode == "portfolio" and has_result_artifact)
        )
    )
    formal_eligible = bool(
        auditable
        and not demo_only_dataset
        and (engine_mode != "event_proxy" or event_content_verified)
        and result_nature != "unverified"
        and not (external_portfolio and not point_in_time_enforced)
    )
    outcome_metric_ids = (
        "acc_t3_strict", "acc_primary_strict", "acc_primary_non_neutral",
        "acc_primary_directional_trade", "return_forecast",
        "strategy_total_return", "strategy_max_drawdown", "strategy_sharpe_proxy",
    )
    small_sample_redacted = any(
        metric_id in metrics
        and (
            (count := _arena_safe_metric_sample_count(
                metric_id,
                metrics.get(metric_id),
                all_metrics=metrics,
            )) is None
            or count < _ARENA_SAFE_CURVE_MIN_BUCKET
        )
        for metric_id in outcome_metric_ids
    )
    return {
        "eligible": bool(auditable),
        "formal_eligible": formal_eligible,
        "has_event_snapshot": has_event_snapshot,
        "has_labels_snapshot": has_labels_snapshot,
        "has_result_artifact": has_result_artifact,
        "event_content_verified": event_content_verified,
        "prediction_only": prediction_only,
        "has_numeric_forecast": numeric_forecast,
        "evaluated_forecast_count": evaluated_count,
        "performance_sample_sufficient": performance_sample_sufficient,
        "forecast_sample_sufficient": forecast_sample_sufficient,
        "point_in_time_enforced": point_in_time_enforced,
        "external_portfolio": external_portfolio,
        "model_lab_demo": bool(model_lab_demo),
        "small_sample_redacted": small_sample_redacted,
    }


def _sanitize_arena_safe_metrics_response(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Return aggregate metrics while dropping windows, versions and nested rows."""
    fields = (
        "run_id", "engine_mode", "result_nature", "status",
        "event_metrics_applicable", "primary_oracle_horizon", "direction_mode",
        "n_total", "total", "neutral_count", "neutral_ratio", "abstain_count",
        "valid_output_count", "invalid_output_count", "voluntary_abstain_count",
        "insufficient_data_count", "n_outputs", "acc_t3_strict",
        "acc_t3_non_neutral",
    )
    observation_count = _arena_safe_metrics_observation_count(value)
    aggregate_sample_sufficient = bool(
        observation_count is not None
        and observation_count >= _ARENA_SAFE_CURVE_MIN_BUCKET
    )
    raw_metrics = value.get("metrics") if isinstance(value.get("metrics"), Mapping) else {}
    safe_metrics = _sanitize_arena_safe_metrics(raw_metrics)
    present_metric_ids = {
        metric_id for metric_id in _ARENA_SAFE_METRIC_IDS
        if isinstance(raw_metrics.get(metric_id), Mapping)
    }
    redacted_metric_ids = present_metric_ids - set(safe_metrics)
    small_sample = (not aggregate_sample_sufficient) or bool(redacted_metric_ids)
    safe = {field: value[field] for field in fields if field in value}
    if not aggregate_sample_sufficient:
        for field in (
            "direction_mode", "n_total", "total", "neutral_count", "neutral_ratio",
            "abstain_count", "valid_output_count", "invalid_output_count",
            "voluntary_abstain_count", "insufficient_data_count", "n_outputs",
        ):
            safe.pop(field, None)

    strict_metric = raw_metrics.get("acc_t3_strict")
    strict_count = _arena_safe_metric_sample_count(
        "acc_t3_strict",
        strict_metric,
        all_metrics=raw_metrics,
    )
    if strict_count is None or strict_count < _ARENA_SAFE_CURVE_MIN_BUCKET:
        safe.pop("acc_t3_strict", None)
    non_neutral_metric = raw_metrics.get("acc_primary_non_neutral")
    non_neutral_count = _arena_safe_metric_sample_count(
        "acc_primary_non_neutral",
        non_neutral_metric,
        all_metrics=raw_metrics,
    )
    if non_neutral_count is None or non_neutral_count < _ARENA_SAFE_CURVE_MIN_BUCKET:
        safe.pop("acc_t3_non_neutral", None)

    safe["metrics"] = safe_metrics
    coverage = _allowlisted_mapping(
        value.get("oracle_coverage"),
        ("required_horizon", "eligible_labels", "valid_labels", "missing_labels", "coverage_rate"),
    )
    coverage_count = _arena_safe_count(coverage.get("eligible_labels"))
    if coverage and coverage_count is not None and coverage_count >= _ARENA_SAFE_CURVE_MIN_BUCKET:
        safe["oracle_coverage"] = coverage
    safe["privacy"] = {
        "visibility": "arena_safe",
        "event_level_redacted": True,
        "metrics_policy": "aggregate_allowlist_v1",
        "small_sample_redacted": small_sample,
        "redacted_metric_count": len(redacted_metric_ids),
        "minimum_aggregate_observations": _ARENA_SAFE_CURVE_MIN_BUCKET,
    }
    return safe


def _sanitize_arena_safe_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    done_count = value.get("done_count")
    count = _arena_safe_count(done_count)
    aggregate_sample_sufficient = bool(
        count is not None and count >= _ARENA_SAFE_CURVE_MIN_BUCKET
    )
    # Legacy snapshots contain only total progress, not the effective accuracy
    # denominator.  Accuracy therefore fails closed even after five events;
    # neutral_ratio is safe once its explicit done-event denominator is large.
    fields = (
        "id", "snapshot_id", "run_id", "done_count", "created_at", "from_catchup",
    )
    if aggregate_sample_sufficient:
        fields += ("neutral_ratio", "primary_oracle_horizon")
    safe = _allowlisted_mapping(
        value,
        fields,
    )
    safe["privacy"] = "arena_safe"
    safe["small_sample_redacted"] = True
    return safe


def _sanitize_arena_safe_sse_event(
    value: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    """Build terminal/progress frames from scratch for Arena-safe streams."""
    event_type = str(value.get("type") or "progress")
    if event_type == "metrics_snapshot":
        return {
            "type": "metrics_snapshot",
            **_sanitize_arena_safe_snapshot(value),
            "run_id": run_id,
        }
    if event_type == "run_started":
        return {"type": "run_started", "run_id": run_id, "privacy": "arena_safe"}
    if event_type == "run_info":
        return {
            "type": "run_info", "run_id": run_id,
            "total_events": value.get("total_events"), "privacy": "arena_safe",
        }
    if event_type == "run_status_changed":
        return {
            "type": "run_status_changed", "run_id": run_id,
            "from": value.get("from"), "to": value.get("to"),
            "privacy": "arena_safe",
        }
    if event_type == "run_done":
        return {
            "type": "run_done", "run_id": run_id,
            "done_count": value.get("done_count"), "privacy": "arena_safe",
        }
    if event_type == "run_failed":
        return {
            "type": "run_failed", "run_id": run_id,
            "error": "运行失败；Arena-safe 详细错误已隐藏。",
            "privacy": "arena_safe",
        }
    if event_type == "run_cancelled":
        return {"type": "run_cancelled", "run_id": run_id, "privacy": "arena_safe"}
    return {"type": "progress", "run_id": run_id, "privacy": "arena_safe"}


def _sanitize_arena_safe_performance_response(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a future-proof aggregate-only performance DTO.

    The browser must never receive a private field merely because the current
    React view happens not to render it. This root allow-list complements the
    existing curve/financial sanitizers and protects Network/API consumers too.
    """
    root_fields = (
        "run_id", "mode", "engine_mode", "result_nature", "status",
        "primary_horizon", "dataset_version", "currency", "initial_value",
        "data_frozen", "prediction_only", "proxy_disclaimer", "privacy",
    )
    observation_count = _arena_safe_performance_observation_count(value)
    performance_sample_sufficient = bool(
        observation_count is not None
        and observation_count >= _ARENA_SAFE_CURVE_MIN_BUCKET
    )
    forecast_count = _arena_safe_return_forecast_observation_count(value)
    forecast_sample_sufficient = bool(
        forecast_count is not None
        and forecast_count >= _ARENA_SAFE_CURVE_MIN_BUCKET
    )
    forecast = value.get("return_forecast")
    forecast_present = bool(
        isinstance(forecast, Mapping)
        and str(forecast.get("status") or "").strip().lower()
        not in {"", "not_provided", "not_applicable", "unavailable"}
    )
    small_sample = (
        not performance_sample_sufficient
        or (forecast_present and not forecast_sample_sufficient)
    )
    safe = {field: value[field] for field in root_fields if field in value}
    summary_fields = (
        ("bar_count", "asset_total_return", "benchmark_total_return")
        if value.get("prediction_only") is True
        else _ARENA_SAFE_PERFORMANCE_SUMMARY_FIELDS
    )
    safe["summary"] = {} if not performance_sample_sufficient else _allowlisted_mapping(
        value.get("summary"), summary_fields,
    )
    safe["financial_analysis"] = (
        {"status": "redacted", "reason": "arena_safe_small_sample"}
        if not performance_sample_sufficient
        else _sanitize_arena_safe_financial_analysis(value.get("financial_analysis"))
    )
    safe["return_forecast"] = {} if not forecast_sample_sufficient else _allowlisted_mapping(
        value.get("return_forecast"), _ARENA_SAFE_RETURN_FORECAST_FIELDS,
    )

    safe["dataset"] = _allowlisted_mapping(
        value.get("dataset"),
        ("name", "symbol", "market", "frequency", "source_type", "bar_count"),
    )
    safe["strategy"] = {"details_redacted": True, "visibility": "arena_safe"}

    protocol = value.get("effective_protocol")
    protocol = protocol if isinstance(protocol, Mapping) else {}
    safe["effective_protocol"] = {
        "requested": {},
        "applied": _allowlisted_mapping(
            protocol.get("applied"),
            ("engine", "dataset_version", "benchmark", "evaluation_horizon", "evaluator_version"),
        ),
        "recorded_not_applied": {},
        "privacy": "arena_safe",
    }
    safe["data_quality"] = _allowlisted_mapping(
        value.get("data_quality"),
        (
            "labels_file_present", "valid_oracle_count", "frequency", "bar_count",
            "frozen_bars_embedded_in_result", "event_level_redacted",
        ),
    )
    safe["data_quality"]["event_level_redacted"] = True

    for curve_key in (
        "equity_curve", "gross_equity_curve", "drawdown_curve",
        "asset_curve", "benchmark_curve",
    ):
        safe[curve_key] = (
            [] if not performance_sample_sufficient
            else _sanitize_arena_safe_curve(value.get(curve_key))
        )
    for empty_key in (
        "trades", "orders", "positions", "signals", "bars",
        "return_forecasts", "event_return_forecasts", "event_markers",
        "event_decisions", "kline_refs",
    ):
        safe[empty_key] = []
    safe["kline_by_event"] = {}
    safe["correctness"] = None
    privacy = safe.get("privacy")
    privacy = dict(privacy) if isinstance(privacy, Mapping) else {}
    privacy.update({
        "visibility": "arena_safe",
        "event_level_redacted": True,
        "run_contract_redacted": True,
        "performance_policy": "aggregate_root_allowlist_v1",
        "small_sample_redacted": small_sample,
        "performance_sample_sufficient": performance_sample_sufficient,
        "forecast_sample_sufficient": forecast_sample_sufficient,
        "minimum_aggregate_observations": _ARENA_SAFE_CURVE_MIN_BUCKET,
    })
    safe["privacy"] = privacy
    return safe


_ARENA_SAFE_RUN_FIELDS = (
    "id", "name", "status", "evaluation_horizon", "strategy_type",
    "engine_mode", "result_nature", "comparison_protocol_hash", "visibility",
    "oracle_status", "total_events", "done_events", "completion_quality",
    "warning_count", "invalid_output_count", "voluntary_abstain_count",
    "insufficient_data_count", "acc_t3_strict", "acc_t3_strict_lo",
    "acc_t3_non_neutral", "created_at", "updated_at", "started_at",
    "finished_at",
)


def _sanitize_arena_safe_run_response(run: Mapping[str, Any]) -> dict[str, Any]:
    """Hide frozen strategy, execution window and local artifacts server-side."""
    if not _is_arena_safe(dict(run)):
        return dict(run)
    eligibility = _arena_safe_eligibility(run)
    public_config = {
        "arena_eligibility": eligibility,
        "arena_comparison": _arena_safe_public_comparison(run),
    }
    safe = {field: run.get(field) for field in _ARENA_SAFE_RUN_FIELDS}
    safe.update({
        # BacktestRunResponse has legacy required fields. Safe placeholders
        # preserve that contract without exposing the underlying implementation.
        "runner": "arena_safe",
        "dataset_id": None,
        "dataset_name": None,
        "dataset_version": None,
        "protocol_hash": None,
        "strategy_spec": None,
        "comparison_signature": None,
        "execution_spec": None,
        "prompt_variant": None,
        "model_version": None,
        "events_path": "[arena-safe-redacted]",
        "labels_path": None,
        "out_path": "[arena-safe-redacted]",
        "result_path": None,
        "ckpt_dir": None,
        "concurrency": 0,
        "config": public_config,
        "metrics": None,
        "error_msg": "运行失败；Arena-safe 详细错误已隐藏。" if run.get("error_msg") else None,
        "activity": None,
    })
    done_count = _arena_safe_count(run.get("done_events"))
    if done_count is None or done_count < _ARENA_SAFE_CURVE_MIN_BUCKET:
        safe.update({
            "completion_quality": (
                "redacted_small_sample" if str(run.get("status") or "") == "done"
                else run.get("completion_quality")
            ),
            "warning_count": 0,
            "invalid_output_count": 0,
            "voluntary_abstain_count": 0,
            "insufficient_data_count": 0,
        })

    metrics = run.get("metrics") if isinstance(run.get("metrics"), Mapping) else {}
    strict_count = _arena_safe_metric_sample_count(
        "acc_t3_strict",
        metrics.get("acc_t3_strict"),
        all_metrics=metrics,
    )
    if strict_count is None or strict_count < _ARENA_SAFE_CURVE_MIN_BUCKET:
        safe["acc_t3_strict"] = None
        safe["acc_t3_strict_lo"] = None
    non_neutral_count = _arena_safe_metric_sample_count(
        "acc_primary_non_neutral",
        metrics.get("acc_primary_non_neutral"),
        all_metrics=metrics,
    )
    if non_neutral_count is None or non_neutral_count < _ARENA_SAFE_CURVE_MIN_BUCKET:
        safe["acc_t3_non_neutral"] = None
    return safe


def _ensure_protocol_hash(run: dict[str, Any]) -> dict[str, Any]:
    """Expose both the legacy integrity hash and strategy-independent Arena signature."""
    migrated = dict(run)
    if str(migrated.get("engine_mode") or "event_proxy") == "event_proxy":
        config = dict(migrated.get("config") or {})
        frozen = dict(config.get("dataset_snapshot") or {})
        try:
            from ..event_backtest.datasets import with_semantic_quality

            enriched = with_semantic_quality({
                **frozen,
                "dataset_kind": "event",
                "path": migrated.get("events_path"),
            })
            semantic_quality = enriched.get("semantic_quality")
            if isinstance(semantic_quality, dict):
                frozen["semantic_quality"] = semantic_quality
                config["dataset_snapshot"] = frozen
                migrated["config"] = config
        except (OSError, UnicodeError, TypeError, ValueError):
            # A legacy Run must remain readable even if its archived source has
            # disappeared. Arena performs its own strict gate at comparison time.
            pass
    if not migrated.get("protocol_hash"):
        events_path = Path(str(migrated.get("events_path") or ""))
        if events_path.is_file():
            from ..event_backtest.protocol import protocol_hash_for_run

            fingerprint = protocol_hash_for_run(migrated)
            db.update_bt_run_protocol_hash(str(migrated.get("id") or ""), fingerprint)
            migrated["protocol_hash"] = fingerprint

    from ..event_backtest.protocol import (
        comparison_protocol_hash_for_run,
        comparison_signature_for_run,
    )

    dataset_contract = None
    dataset_id = str(migrated.get("dataset_id") or "")
    dataset_version = str(migrated.get("dataset_version") or "")
    if dataset_id and dataset_version:
        dataset_contract = db.get_bt_dataset_version(dataset_id, dataset_version)
    try:
        migrated["comparison_signature"] = comparison_signature_for_run(
            migrated, dataset_contract=dataset_contract,
        )
        migrated["comparison_protocol_hash"] = comparison_protocol_hash_for_run(
            migrated, dataset_contract=dataset_contract,
        )
    except (OSError, TypeError, ValueError):
        # A malformed legacy row remains readable; Arena validation will return
        # the actionable protocol error at the comparison boundary.
        pass
    return migrated


# ================================================================= runs CRUD ---

def _with_runtime_activity(run: dict[str, Any]) -> dict[str, Any]:
    """Attach advisory live stages without mutating the persisted Run row."""
    enriched = _ensure_protocol_hash(run)
    if str(enriched.get("runner") or "") == "team_full" and str(enriched.get("status") or "") in {"running", "paused"}:
        enriched["activity"] = orch.get_run_activity(
            str(enriched.get("id") or ""),
            aggregate_only=_is_arena_safe(enriched),
        )
    else:
        enriched["activity"] = None
    return enriched

@router.get("/runs", response_model=ListBacktestRunsResponse)
def list_runs(limit: int = Query(100, ge=1, le=500)) -> Any:
    items = [
        _sanitize_arena_safe_run_response(_with_runtime_activity(run))
        for run in db.list_bt_runs(limit=limit)
    ]
    return _share_safe_payload({"total": len(items), "items": items})


def _auto_paths(name: str, run_id: str, *, allocate: bool = True) -> tuple[str, str, str]:
    """Allocate legacy predictions plus a unified result artifact path."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name[:30]) or "bt"
    out_dir = Path(DATA_DIR) / "backtests"
    ckpt_dir = out_dir / f"_trajectory_{run_id}"
    if allocate:
        ckpt_dir.mkdir(parents=True, exist_ok=True)
    return (
        str(out_dir / f"preds_{safe}_{run_id}.jsonl"),
        str(ckpt_dir),
        str(out_dir / f"result_{safe}_{run_id}.json"),
    )


@router.post("/runs", response_model=BacktestRunResponse)
def create_run(req: CreateBacktestRunRequest) -> Any:
    # ``model_lab_batch_id`` alone remains readable for legacy event-result
    # links. A task id, however, would let a public Run impersonate the narrow
    # Model Lab create->link recovery gap and must never be client-authored.
    server_owned_lineage = {"model_profile_snapshot", "model_profile_id", "model_lab_task_id"}
    if server_owned_lineage.intersection(req.config):
        raise HTTPException(status_code=422, detail="模型快照与测试关联由服务端生成，请选择已保存模型")
    created = create_run_from_trusted_request(req)
    persisted = db.get_bt_run(str(created.get("id") or "")) or created
    return _share_safe_payload(
        _sanitize_arena_safe_run_response(_ensure_protocol_hash(persisted))
    )


def create_run_from_trusted_request(req: CreateBacktestRunRequest) -> Any:
    """Internal entry for snapshots assembled by Model Lab or stored Runs.

    HTTP callers must enter through create_run, which rejects authored model
    snapshots before invoking this shared persistence path.
    """
    prepared = prepare_run(req)
    Path(prepared["ckpt_dir"]).mkdir(parents=True, exist_ok=True)
    run = db.create_bt_run(**prepared)
    return _share_safe_payload(_ensure_protocol_hash(run))


def prepare_run(req: CreateBacktestRunRequest) -> dict[str, Any]:
    """Validate and freeze inputs without inserting a Run or allocating outputs."""
    from ..event_backtest.datasets import materialize_contract
    from ..event_backtest.strategy_registry import (
        execution_engine_mode,
        legacy_runner_for,
        normalize_portfolio_execution_spec,
        normalize_strategy_spec,
    )
    if (req.config or {}).get("saved_model_id"):
        from ..model_registry import bind_test_request
        try:
            req = bind_test_request(req)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_share_safe_text(str(exc))) from exc

    # --- Resolve and freeze one dataset version. ---
    events_path = _resolve_path(req.events_path)
    labels_path = _resolve_path(req.labels_path)
    dataset_name = None
    dataset_version: str | None = None
    dataset_kind = "event"
    dataset_snapshot: dict[str, Any] | None = None
    if req.dataset_id:
        ds = db.get_bt_dataset(req.dataset_id)
        if not ds:
            raise HTTPException(status_code=404, detail=f"dataset_id={req.dataset_id} not found")
        try:
            ds = _ensure_dataset_contract(ds)
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=_share_safe_text(f"数据集版本化失败: {exc}"),
            ) from exc
        selected_version = req.dataset_version or ds.get("dataset_version") or ds.get("current_version")
        if not selected_version:
            raise HTTPException(status_code=409, detail="数据集尚无可冻结的 dataset_version")
        version = db.get_bt_dataset_version(req.dataset_id, str(selected_version))
        if not version:
            raise HTTPException(
                status_code=409,
                detail=f"dataset_version={selected_version} 不属于 dataset_id={req.dataset_id}",
            )
        dataset_snapshot = dict(ds)
        for key, value in version.items():
            if key not in {"id", "dataset_id"}:
                dataset_snapshot[key] = value
        from ..event_backtest.datasets import with_semantic_quality
        dataset_snapshot = with_semantic_quality(dataset_snapshot)
        dataset_version = str(selected_version)
        dataset_kind = str(ds.get("dataset_kind") or "event")
        if str(version.get("status") or "") != "available" or not version.get("snapshot_hash"):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "数据版本尚未物化，不能创建可执行 Run",
                    "dataset_id": req.dataset_id,
                    "dataset_version": dataset_version,
                    "status": version.get("status") or ds.get("status") or "pending",
                    "hint": "请先通过 versions 接口导入并校验真实本地快照；系统不会在线补数据。",
                },
            )
        events_path = events_path or _resolve_path(version.get("path") or ds.get("path"))
        # Oracle labels belong to the selected immutable version.  A dataset's
        # top-level labels_path points at its *current* version and must never
        # leak into a historical version that has no Oracle snapshot.
        version_labels_path = version.get("labels_path")
        current_version = ds.get("current_version") or ds.get("dataset_version")
        if not version_labels_path and str(selected_version) == str(current_version or ""):
            # Compatibility for catalogs created before labels_path was copied
            # into bt_dataset_versions.
            version_labels_path = ds.get("labels_path")
        labels_path = labels_path or _resolve_path(version_labels_path)
        dataset_name = str(ds.get("name") or req.dataset_id)
    elif events_path:
        # Direct-path legacy event calls remain supported and receive a frozen,
        # content-addressed version even though they are not in the catalog.
        try:
            direct = materialize_contract({
                "dataset_kind": "event", "path": events_path,
                "source": {"type": "local_file", "provider": "legacy_direct_path", "ref": events_path},
            })
            dataset_version = str(direct["dataset_version"])
            dataset_snapshot = direct
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=_share_safe_text(f"events_path 数据校验失败: {exc}"),
            ) from exc

    if dataset_snapshot is not None:
        from ..event_backtest.datasets import verify_materialized_snapshot
        verified, verify_error = verify_materialized_snapshot(dataset_snapshot)
        if not verified:
            raise HTTPException(
                status_code=409,
                detail={"message": "dataset_version 对应的冻结快照不可用", "reason": verify_error},
            )

    if not events_path or not Path(events_path).is_file():
        detail = "events_path 无效或不存在"
        if not _share_mode_enabled():
            detail = f"{detail}: {req.events_path}"
        raise HTTPException(status_code=400, detail=detail)
    if labels_path and not Path(labels_path).is_file():
        detail = "labels_path 不存在"
        if not _share_mode_enabled():
            detail = f"{detail}: {req.labels_path}"
        raise HTTPException(status_code=400, detail=detail)

    raw_strategy_spec = _model_dict(req.strategy_spec)
    try:
        strategy_spec = normalize_strategy_spec(
            raw_strategy_spec,
            legacy_runner=req.runner,
            legacy_strategy_type=req.strategy_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_share_safe_text(f"strategy_spec 无效: {exc}")) from exc
    engine_mode = execution_engine_mode(strategy_spec)
    strategy_type = str(strategy_spec.get("type") or "event")
    runner = legacy_runner_for(strategy_spec)
    if strategy_spec.get("type") == "quant" and strategy_spec.get("kind") == "signal_file":
        signal_path = _resolve_path(str(strategy_spec.get("path") or ""))
        if not signal_path or not Path(signal_path).is_file():
            raise HTTPException(status_code=422, detail="signal_file 策略的 path 不存在")
        strategy_spec["path"] = signal_path
    if strategy_spec.get("type") in {"quant", "api"}:
        from ..quant_backtest.models import QuantBacktestError
        from ..quant_backtest.strategies import strategy_from_spec
        native_strategy_spec = dict(strategy_spec)
        if strategy_spec.get("type") == "api":
            native_strategy_spec["kind"] = "external_http"
        try:
            strategy_from_spec(native_strategy_spec)
        except (QuantBacktestError, OSError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=_share_safe_text(f"strategy_spec 无法执行: {exc}"),
            ) from exc

    if engine_mode == "event_proxy" and dataset_kind != "event":
        raise HTTPException(status_code=422, detail="事件策略必须使用 dataset_kind=event 的数据集")
    if engine_mode == "portfolio":
        if dataset_kind != "market":
            raise HTTPException(status_code=422, detail="量化/API 组合策略必须使用 dataset_kind=market 的冻结 bar 数据")
        capabilities = (dataset_snapshot or {}).get("capabilities") or {}
        symbols = (dataset_snapshot or {}).get("symbols") or []
        if not bool(capabilities.get("portfolio_execution")) or len(symbols) != 1:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "当前 portfolio MVP 仅执行单标的 OHLC 数据；该数据集仍可作为多标的数据基准保存",
                    "symbols": symbols,
                    "capabilities": capabilities,
                },
            )

    decision_ready, input_contract = _dataset_decision_contract(events_path)
    if runner == "provided_analysis" and not decision_ready:
        strategy_label = strategy_type
        detail = (
            f"strategy_type={strategy_label} 使用 provided_analysis 时，数据集每条记录都必须含有效的 "
            "analysis_direction、analysis_confidence/confidence 与 analysis_rationale/rationale；"
            f"当前识别为 {input_contract}。"
        )
        raise HTTPException(status_code=422, detail=detail)

    from ..event_backtest.evaluation_protocol import (
        event_proxy_cost_spec,
        normalize_evaluation_horizon,
        select_events_for_execution_window,
    )

    config = dict(req.config or {})
    effective_model_version = req.model_version
    if runner in {"team_prompt", "team_full"} and not isinstance(
        config.get("model_profile_snapshot"), dict
    ):
        # Freeze the platform default when the Run is created.  Selecting a new
        # default affects subsequent runs, while retries/resumes of this Run
        # keep using the original model endpoint and model id.
        from ..model_lab import repository as model_repo
        from ..model_lab.providers import profile_snapshot

        selected_profile = model_repo.get_default_profile(public=False)
        if selected_profile is not None:
            config["model_profile_snapshot"] = profile_snapshot(selected_profile)
            config["model_profile_id"] = selected_profile.get("id")
            if not effective_model_version:
                effective_model_version = str(selected_profile.get("model_id") or "") or None
    raw_evaluation_protocol = config.get("evaluation_protocol")
    if not isinstance(raw_evaluation_protocol, dict):
        raw_evaluation_protocol = config.get("protocol") if isinstance(config.get("protocol"), dict) else {}
    evaluation_protocol = dict(raw_evaluation_protocol)
    explicit_horizon_candidate = (
        req.horizon
        or evaluation_protocol.get("evaluation_horizon")
        or config.get("evaluation_horizon")
        or config.get("primary_oracle_horizon")
        or req.execution_spec.get("holding_horizon")
    )
    try:
        declared_analysis_horizon: str | None = None
        if runner == "provided_analysis":
            from ..event_backtest.application import load_events

            declared_horizons = {
                normalize_evaluation_horizon(getattr(event, "analysis_horizon", None))
                for event in load_events(events_path)
                if getattr(event, "analysis_horizon", None)
            }
            if len(declared_horizons) > 1:
                raise ValueError(
                    "provided_analysis 数据集包含多个 analysis_horizon；请按统一评价窗口拆分数据集"
                )
            declared_analysis_horizon = next(iter(declared_horizons), None)
        horizon_candidate = explicit_horizon_candidate or declared_analysis_horizon or "t3"
        evaluation_horizon = normalize_evaluation_horizon(horizon_candidate)
        if (
            declared_analysis_horizon is not None
            and explicit_horizon_candidate is not None
            and evaluation_horizon != declared_analysis_horizon
        ):
            raise ValueError(
                f"冻结评价窗口 {evaluation_horizon} 与数据集 analysis_horizon="
                f"{declared_analysis_horizon} 不一致"
            )
        # Validate all currently effective proxy settings at creation rather than
        # letting metrics fail after a long-running model job.
        if engine_mode == "event_proxy":
            event_proxy_cost_spec(req.execution_spec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    evaluation_protocol["evaluation_horizon"] = evaluation_horizon
    if engine_mode == "event_proxy":
        # Freeze the output contract for new runs; legacy records keep their
        # original three-class interpretation and are never relabelled.
        config["direction_mode"] = "binary"
        evaluation_protocol["direction_mode"] = "binary"
        from ..event_backtest.oracle_contract import freeze_oracle_epsilon
        try:
            evaluation_protocol["oracle_epsilon"] = freeze_oracle_epsilon(
                labels_path, evaluation_protocol.get("oracle_epsilon"),
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_share_safe_text(str(exc))) from exc
        evaluation_protocol["resolved_benchmarks"] = _event_benchmark_contract(events_path)
    config["evaluation_horizon"] = evaluation_horizon
    config["primary_oracle_horizon"] = evaluation_horizon
    config["evaluation_protocol"] = evaluation_protocol

    run_id = db.new_id()
    out_path, ckpt_dir, result_path = _auto_paths(req.name, run_id, allocate=False)

    # 估算 total_events
    total = 0
    try:
        if engine_mode == "event_proxy":
            from ..event_backtest.application import load_events
            loaded_events = load_events(events_path)
            selection = select_events_for_execution_window(loaded_events, req.execution_spec)
            total = len(selection.events)
            if selection.active and loaded_events and not selection.events:
                raise HTTPException(
                    status_code=422,
                    detail={"message": "执行日期区间内没有可回测事件", "event_window": selection.to_dict()},
                )
        else:
            total = int(((dataset_snapshot or {}).get("coverage") or {}).get("row_count") or 0)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        total = 0

    if engine_mode == "portfolio":
        try:
            portfolio_execution = normalize_portfolio_execution_spec(req.execution_spec)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_share_safe_text(f"execution_spec 无效: {exc}")) from exc
        stored_execution_spec: dict[str, Any] = portfolio_execution
        hash_execution_spec = portfolio_execution["applied"]
    else:
        stored_execution_spec = dict(req.execution_spec)
        hash_execution_spec = stored_execution_spec

    evaluation_protocol["engine_mode"] = engine_mode
    evaluation_protocol["dataset_version"] = dataset_version
    config["strategy_spec"] = strategy_spec
    config["engine_mode"] = engine_mode
    config["dataset_version"] = dataset_version
    config["dataset_snapshot"] = {
        "snapshot_hash": (dataset_snapshot or {}).get("snapshot_hash"),
        "dataset_kind": dataset_kind,
        "status": (dataset_snapshot or {}).get("status"),
        "frequency": (dataset_snapshot or {}).get("frequency"),
        "coverage": (dataset_snapshot or {}).get("coverage") or {},
        "markets": (dataset_snapshot or {}).get("markets") or [],
        "symbols": (dataset_snapshot or {}).get("symbols") or [],
        # ``quality_status`` only means schema validation.  Semantic quality is
        # frozen separately so an Arena can distinguish demo facts from real
        # historical events without reinterpreting the old field.
        "quality_status": (dataset_snapshot or {}).get("quality_status"),
        "semantic_quality": (dataset_snapshot or {}).get("semantic_quality") or {},
    }

    from ..event_backtest.protocol import build_protocol_hash

    derived_protocol_hash = build_protocol_hash(
        events_path=events_path,
        labels_path=labels_path,
        execution_spec=hash_execution_spec,
        evaluation_protocol=evaluation_protocol,
        dataset_version=dataset_version,
        engine_mode=engine_mode,
    )
    supplied_protocol_hash = (req.protocol_hash or "").strip()
    if supplied_protocol_hash and supplied_protocol_hash != derived_protocol_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "protocol_hash 与服务端根据数据快照和执行口径计算的指纹不一致",
                "provided": supplied_protocol_hash,
                "expected": derived_protocol_hash,
                "hint": "请留空由服务端生成，或同步更新已冻结的数据与评测协议。",
            },
        )
    protocol_hash = derived_protocol_hash

    return dict(
        name=req.name,
        runner=runner,
        events_path=events_path,
        labels_path=labels_path,
        out_path=out_path,
        ckpt_dir=ckpt_dir,
        run_id=run_id,
        prompt_variant=req.prompt_variant,
        model_version=effective_model_version,
        concurrency=int(req.concurrency),
        total_events=total,
        config=config,
        strategy_type=strategy_type,
        strategy_spec=strategy_spec,
        engine_mode=engine_mode,
        result_nature=str(strategy_spec.get("result_nature") or "proxy"),
        dataset_id=req.dataset_id,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        protocol_hash=protocol_hash,
        visibility="arena_safe" if req.visibility == "arena-safe" else req.visibility,
        oracle_status="available" if labels_path else "unavailable",
        execution_spec=stored_execution_spec,
        result_path=result_path,
    )


@router.get("/runs/{run_id}", response_model=BacktestRunResponse)
def get_run(run_id: str) -> Any:
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return _share_safe_payload(
        _sanitize_arena_safe_run_response(_with_runtime_activity(run))
    )


@router.post("/runs/{run_id}/cost-scenario", response_model=BacktestRunResponse)
def create_cost_scenario(run_id: str, req: CreateCostScenarioRequest) -> Any:
    """Create a traceable sibling Run instead of rewriting frozen results.

    Costs are charged by the portfolio engine on every non-zero change in
    target weight.  Commission/slippage/other costs apply to both directions;
    stamp duty applies only to sells; minimum commission is applied per order.
    """
    source = db.get_bt_run(run_id)
    if not source:
        raise HTTPException(status_code=404, detail="run not found")
    if str(source.get("engine_mode") or "") != "portfolio":
        raise HTTPException(status_code=422, detail="成本情景仅适用于有真实成交账本的量化/API portfolio Run")
    if _is_arena_safe(source):
        raise HTTPException(status_code=403, detail="Arena 安全摘要不能反向创建含交易明细的成本情景")
    if str(source.get("status") or "") != "done" or not source.get("result_path"):
        raise HTTPException(status_code=409, detail="请先完成原始 portfolio Run，再创建成本情景")

    from ..event_backtest.strategy_registry import normalize_portfolio_execution_spec

    stored_execution = source.get("execution_spec")
    stored_execution = dict(stored_execution) if isinstance(stored_execution, dict) else {}
    requested = stored_execution.get("requested")
    if isinstance(requested, dict):
        source_execution_input = dict(requested)
    else:
        applied = stored_execution.get("applied")
        source_execution_input = dict(applied) if isinstance(applied, dict) else dict(stored_execution)
    cloned_execution = dict(source_execution_input)

    cost_updates = {
        "commission_bps": req.commission_bps,
        "slippage_bps": req.slippage_bps,
        "stamp_duty_bps": req.stamp_duty_bps,
        "other_cost_bps": req.other_cost_bps,
        "minimum_commission": req.minimum_commission,
    }
    for key, value in cost_updates.items():
        if value is not None:
            cloned_execution[key] = float(value)

    try:
        previous_applied = normalize_portfolio_execution_spec(source_execution_input)["applied"]
        next_applied = normalize_portfolio_execution_spec(cloned_execution)["applied"]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_share_safe_text(f"成本情景无效: {exc}")) from exc
    cost_keys = (
        "commission_bps", "slippage_bps", "stamp_duty_bps",
        "other_cost_bps", "minimum_commission",
    )
    if all(float(previous_applied.get(key) or 0.0) == float(next_applied.get(key) or 0.0) for key in cost_keys):
        raise HTTPException(status_code=409, detail="成本参数没有变化；请至少修改一个 bp 或最低佣金字段")

    source_config = source.get("config")
    scenario_config = dict(source_config) if isinstance(source_config, dict) else {}
    scenario_config.update({
        "scenario_of": run_id,
        "scenario_type": "cost",
        "source_protocol_hash": source.get("protocol_hash"),
        "cost_scenario": {key: next_applied.get(key) for key in cost_keys},
    })
    strategy_spec = source.get("strategy_spec")
    if not isinstance(strategy_spec, dict):
        strategy_spec = (scenario_config.get("strategy_spec")
                         if isinstance(scenario_config.get("strategy_spec"), dict) else None)
    if not strategy_spec:
        raise HTTPException(status_code=409, detail="原 Run 缺少可复用的冻结 strategy_spec")

    scenario_name = (req.name or f"{source.get('name') or run_id} · 成本情景")[:120]
    # Publish the cloned Run and its auto-start state as one lifecycle action.
    # Otherwise history deletion can remove the pending row between the insert
    # and start_run(), making this create request fail for a Run it just created.
    with db.HISTORY_RELATION_LOCK:
        if not db.get_bt_run(run_id):
            raise HTTPException(status_code=409, detail="原 Run 已被删除，请刷新后重试")
        cloned = create_run_from_trusted_request(CreateBacktestRunRequest(
            name=scenario_name,
            runner=str(source.get("runner") or "quant"),
            dataset_id=source.get("dataset_id"),
            dataset_version=source.get("dataset_version"),
            events_path=None if source.get("dataset_id") else source.get("events_path"),
            labels_path=None if source.get("dataset_id") else source.get("labels_path"),
            prompt_variant=str(source.get("prompt_variant") or "v0"),
            model_version=source.get("model_version"),
            horizon=_primary_horizon_for_response(source),
            concurrency=int(source.get("concurrency") or 1),
            strategy_type=str(source.get("strategy_type") or "quant"),
            strategy_spec=strategy_spec,
            visibility="private",
            execution_spec=cloned_execution,
            config=scenario_config,
        ))
        new_run_id = str(cloned.get("id") or "")
        if req.auto_start and new_run_id:
            start_run(new_run_id)
        current = db.get_bt_run(new_run_id)
        if not current:
            raise HTTPException(status_code=409, detail="成本情景 Run 未能持久化，请重试")
    return _share_safe_payload(current)


@router.post("/history/delete")
def delete_history_records(req: DeleteBacktestHistoryRequest) -> Any:
    from ..model_lab import service as model_service

    ok, message, result = model_service.delete_history_records(req.run_ids, req.batch_ids)
    if not ok:
        raise HTTPException(status_code=404 if result.get("reason") == "not_found" else 409, detail=message)
    return {
        "ok": True,
        "message": message,
        "requested_count": len(req.run_ids) + len(req.batch_ids),
        **result,
    }


@router.delete("/runs/{run_id}", response_model=ActionResponse)
def delete_run(run_id: str) -> Any:
    from ..model_lab import service as model_service

    ok, message, result = model_service.delete_history_records([run_id], [])
    if not ok:
        raise HTTPException(status_code=404 if result.get("reason") == "not_found" else 409, detail=message)
    return {"ok": True, "run_id": run_id, "message": message}


# ============================================================= start/cancel ---

@router.post("/runs/{run_id}/start", response_model=ActionResponse)
def start_run(run_id: str) -> Any:
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run["status"] in {"running"}:
        return {"ok": False, "run_id": run_id, "message": "already running"}
    from ..event_backtest.datasets import verify_run_frozen_inputs
    frozen_ok, frozen_error = verify_run_frozen_inputs(run)
    if not frozen_ok:
        raise HTTPException(
            status_code=409,
            detail={"message": "Run 的冻结数据或执行协议已失效", "reason": frozen_error},
        )
    # P0 严格默认启用 as_of 防作弊（与 CLI 的 FEVER_BT_STRICT_AS_OF=1 一致）
    import os
    os.environ.setdefault("FEVER_BT_STRICT_AS_OF", "1")
    from ..model_lab.repository import mark_event_launch_intent
    mark_event_launch_intent(run_id)
    res = orch.start_bt_run(run_id)
    qa_message = None
    if run["status"] == "pending":
        from ..model_lab.event_experiments import start_qa_for_run
        qa_ok, linked_message = start_qa_for_run(run_id)
        if not qa_ok:
            qa_message = f"；问答未启动：{linked_message}"
    if not res.ok:
        raise HTTPException(status_code=400, detail=res.error or "start failed")
    return {"ok": True, "run_id": run_id, "message": "started" + (qa_message or "")}


@router.post("/runs/{run_id}/cancel", response_model=ActionResponse)
def cancel_run(run_id: str) -> Any:
    if not orch.cancel_bt_run(run_id):
        run = db.get_bt_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        return {"ok": False, "run_id": run_id, "message": f"not running/paused, status={run['status']}"}
    return {"ok": True, "run_id": run_id, "message": "cancelled"}


@router.post("/runs/{run_id}/pause", response_model=ActionResponse)
def pause_run(run_id: str) -> Any:
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    ok, msg = orch.pause_bt_run(run_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "run_id": run_id, "message": msg}


@router.post("/runs/{run_id}/resume", response_model=ActionResponse)
def resume_run(run_id: str) -> Any:
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    # P0 严格默认启用 as_of 防作弊（与 CLI 的 FEVER_BT_STRICT_AS_OF=1 一致）
    import os
    os.environ.setdefault("FEVER_BT_STRICT_AS_OF", "1")
    ok, msg = orch.resume_bt_run(run_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "run_id": run_id, "message": msg}


# =========================================================== predictions list ---

@router.get("/runs/{run_id}/events", response_model=ListPredictionsResponse)
def list_run_events(
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    market: str | None = None,
    event_type_l2: str | None = None,
    only_incorrect: bool = Query(False),
) -> Any:
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    primary_horizon = _primary_horizon_for_response(run)
    if _is_arena_safe(run):
        return {
            "total": 0,
            "page": page,
            "page_size": page_size,
            "items": [],
            "primary_oracle_horizon": primary_horizon,
        }
    offset = (page - 1) * page_size
    total, items = db.list_bt_predictions(
        run_id,
        offset=offset,
        limit=page_size,
        market=market,
        event_type_l2=event_type_l2,
        only_incorrect=only_incorrect,
    )
    for item in items:
        item["oracle_horizon"] = primary_horizon
    return _share_safe_payload({
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
        "primary_oracle_horizon": primary_horizon,
    })


@router.get("/runs/{run_id}/events-catalog", response_model=ListEventCatalogResponse)
def list_run_event_catalog(
    run_id: str,
    market: str | None = None,
    event_type_l2: str | None = None,
    only_incorrect: bool = Query(False),
    status: str | None = None,
) -> Any:
    """Detail 页「事件目录」：从 events JSONL 直接取完整事件清单 + 状态 + 已完成 prediction。

    与 /events 的差异：
    - /events 只返回 DB 已写入 bt_predictions 的（即已完成的）；
    - /events-catalog 返回 events_path 文件里定义的全部事件（即本来就是 N 条），
      每条都有 status=pending/processing/done，Detail 页 render 后立刻看到 N 条待处理事件。
    """
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    primary_horizon = _primary_horizon_for_response(run)
    if _is_arena_safe(run):
        return {"total": 0, "items": [], "primary_oracle_horizon": primary_horizon}
    events_path = run.get("events_path") or ""
    if not Path(events_path).is_file():
        detail = "events_path not found on disk"
        if not _share_mode_enabled():
            detail = f"{detail}: {events_path}"
        raise HTTPException(status_code=404, detail=detail)
    try:
        from ..event_backtest.application import load_events

        evs = load_events(events_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=_share_safe_text(f"load_events failed: {exc}"))
    # 取全量已完成 predictions（catalog 场景 events ≤1000，不取分页）
    _total, pred_rows = db.list_bt_predictions(
        run_id,
        offset=0,
        limit=100000,
        market=market,
        event_type_l2=event_type_l2,
        only_incorrect=only_incorrect,
    )
    for pred_row in pred_rows:
        pred_row["oracle_horizon"] = primary_horizon
    pred_by_eid: dict[str, dict] = {p["event_id"]: p for p in pred_rows}
    # processing：ckpt_dir 下存在 {event_id}__{run_id}.json（或临时 tmp），但 prediction 未写入 DB
    processing_eids: set[str] = set()
    ckpt_dir = run.get("ckpt_dir") or run.get("trajectory_ckpt_dir")
    if ckpt_dir:
        cdp = Path(ckpt_dir)
        if cdp.is_dir():
            rid_suffix = f"__{run_id}"
            for p in cdp.iterdir():
                if not p.is_file():
                    continue
                if p.suffix not in {".json", ".tmp"}:
                    continue
                stem = p.stem
                if stem.endswith(rid_suffix):
                    processing_eids.add(stem[: -len(rid_suffix)])
                else:
                    processing_eids.add(stem)
    ev_eid_set = {getattr(e, "event_id", "") for e in evs}
    processing_eids &= ev_eid_set  # 过滤非本 events 的残留文件误判
    items: list[dict] = []
    for e in evs:
        eid = getattr(e, "event_id", "")
        ev_market = getattr(e, "market", "") or None
        ev_type = getattr(e, "event_type_l2", "") or None
        if market and ev_market != market:
            continue
        if event_type_l2 and ev_type != event_type_l2:
            continue
        pred = pred_by_eid.get(eid)
        if only_incorrect:
            # only_incorrect 只保留 prediction 存在且 is_correct_t3 == False 的
            if pred is None or bool(pred.get("is_correct_t3")) is not False:
                continue
        st: str
        if pred is not None:
            st = "done"
        elif eid in processing_eids:
            st = "processing"
        else:
            st = "pending"
        if status and st != status:
            continue
        title = (
            getattr(e, "title", None)
            or getattr(e, "event_title", None)
            or getattr(e, "headline", None)
            or None
        )
        raw_event_text = getattr(e, "event_text", None) or None
        # 文本可能非常长（完整公告），catalog 列表截断 300 字够用；完整正文在事件详情 panel 从 dataset 直接读
        event_text_catalog: str | None = None
        if isinstance(raw_event_text, str) and raw_event_text.strip():
            event_text_catalog = raw_event_text[:300] + ("…" if len(raw_event_text) > 300 else "")
        items.append(
            {
                "event_id": eid,
                "symbol": getattr(e, "symbol", None) or None,
                "market": ev_market,
                "event_type_l2": ev_type,
                "title": title,
                "event_time": getattr(e, "event_time", None) or None,
                "occurred_at": getattr(e, "occurred_at", None) or None,
                "available_time": getattr(e, "available_time", None) or None,
                "source_url": getattr(e, "source_url", None) or None,
                "event_text": event_text_catalog,
                "status": st,
                "prediction": pred,
            }
        )
    return _share_safe_payload({
        "total": len(items),
        "items": items,
        "primary_oracle_horizon": primary_horizon,
    })


@router.get("/runs/{run_id}/events/{event_id}")
def get_run_event(run_id: str, event_id: str) -> Any:
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    _require_event_level_access(run)
    pred = db.get_bt_prediction(run_id, event_id)
    if not pred:
        raise HTTPException(status_code=404, detail="prediction not found")
    primary_horizon = _primary_horizon_for_response(run)
    pred["oracle_horizon"] = primary_horizon
    ckpt_dir = Path(run.get("ckpt_dir") or run.get("trajectory_ckpt_dir") or "") if run else None
    # trajectory ckpt 查找：按候选优先级逐一尝试（engine 实际写 {eid}.json，
    # 旧版 orchestrator 曾把 DB 路径误写为 {eid}__{run_id}.json，这里两边都兼容）
    import json as _json
    candidates: list[Path] = []
    db_path = pred.get("trajectory_ckpt")
    if db_path:
        candidates.append(Path(db_path))
    if ckpt_dir and ckpt_dir.is_dir():
        candidates.append(ckpt_dir / f"{event_id}.json")
        candidates.append(ckpt_dir / f"{event_id}__{run_id}.json")
    ckpt: dict | None = None
    for cand in candidates:
        try:
            if cand.is_file():
                with open(cand) as f:
                    ckpt = _json.load(f)
                if ckpt is not None:
                    break
        except Exception:
            continue
    # 返回结构：prediction + trajectory + event_meta（从 events_path 补全事件说明，用于前端「事件信息」区块）
    result: dict[str, Any] = {
        "prediction": pred,
        "trajectory": ckpt,
        "primary_oracle_horizon": primary_horizon,
    }
    event_meta: dict[str, Any] = {}
    if run:
        try:
            events_path = Path(str(run.get("events_path") or ""))
            if events_path.is_file():
                from ..event_backtest.application import load_events
                source_event = next(
                    (event for event in load_events(events_path) if getattr(event, "event_id", "") == event_id),
                    None,
                )
                if source_event is not None:
                    event_meta.update(source_event.to_dict())
        except (OSError, ValueError, TypeError):
            pass
    if ckpt and isinstance(ckpt.get("event_meta"), dict):
        event_meta.update(ckpt["event_meta"])
    # 从 as_of_packet 中提取 event_text / title 补全事件说明（比 event_meta 更完整）
    if ckpt:
        aop = ckpt.get("as_of_packet")
        if isinstance(aop, str):
            try:
                aop_obj = _json.loads(aop)
                if isinstance(aop_obj, dict):
                    for k in ("event_text", "event_title", "title", "headline", "source_url"):
                        v = aop_obj.get(k)
                        if isinstance(v, str) and v.strip():
                            event_meta.setdefault(k, v)
            except Exception:
                pass
    if not event_meta.get("event_id"):
        event_meta["event_id"] = event_id
    for k in ("symbol", "market", "event_type_l2"):
        if not event_meta.get(k):
            v = pred.get(k)
            if v is not None:
                event_meta[k] = v
    result["event_meta"] = event_meta
    return _share_safe_payload(result)


@router.get("/runs/{run_id}/events/{event_id}/kline")
def get_run_event_kline(run_id: str, event_id: str) -> Any:
    """Genuine event-market series for CN/US/HK/FUTURES.

    When the verified provider only exposes closes, ``series_type=line_only``
    is returned. OHLC is never synthesized from a close.
    """
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    _require_event_level_access(run)
    events_path = run.get("events_path") or ""
    if not Path(events_path).is_file():
        detail = "events_path not found on disk"
        if not _share_mode_enabled():
            detail = f"{detail}: {events_path}"
        raise HTTPException(status_code=404, detail=detail)
    from ..event_backtest.application import load_events

    evs = load_events(events_path)
    ev = next((e for e in evs if getattr(e, "event_id", "") == event_id), None)
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    try:
        from ..event_backtest.performance import fetch_event_market_series

        return _share_safe_payload({"ok": True, "payload": fetch_event_market_series(ev)})
    except Exception as exc:  # noqa: BLE001 - explicit per-market degradation contract
        return {
            "ok": False,
            "error": _share_safe_text(f"{type(exc).__name__}: {exc}"),
        }


@router.get("/runs/{run_id}/performance")
def get_run_performance(
    run_id: str,
    include_kline: bool = Query(False, description="是否同步拉取真实事件行情；默认关闭以避免大量远端请求"),
    kline_limit: int = Query(6, ge=1, le=20, description="同步行情的最多事件数"),
) -> Any:
    """Return chart-ready event-proxy performance with explicit methodology."""
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        from ..event_backtest.performance import build_unified_performance

        arena_safe = _is_arena_safe(run)
        result = build_unified_performance(
            run,
            include_kline=include_kline and not arena_safe,
            kline_limit=kline_limit,
        )
        if str(run.get("engine_mode") or "event_proxy") != "portfolio":
            from ..event_backtest.return_forecast_view import build_event_return_forecasts
            from ..return_forecast_statistics import enrich_forecast_summary

            metric_item = (
                run.get("metrics", {}).get("return_forecast", {})
                if isinstance(run.get("metrics"), Mapping) else {}
            )
            stored_summary = (
                metric_item.get("meta", {})
                if isinstance(metric_item, Mapping) and isinstance(metric_item.get("meta"), Mapping)
                else {}
            )
            event_forecasts = [] if arena_safe else build_event_return_forecasts(run)
            result["return_forecast"] = enrich_forecast_summary(stored_summary, event_forecasts)
            result["event_return_forecasts"] = event_forecasts
        if arena_safe:
            # Keep the full in-memory result intact until the strict sanitizer
            # has read effective sample counts (event trades / active bars).
            # The sanitizer below constructs the browser DTO from scratch.
            result["privacy"] = {
                "visibility": "arena_safe",
                "event_level_redacted": True,
                "include_kline_forced_false": bool(include_kline),
                "aggregation": "event_observation_buckets",
                "min_bucket": _ARENA_SAFE_CURVE_MIN_BUCKET,
                "curve_fields": ["index", "net_value", "drawdown"],
                "financial_analysis_redacted": isinstance(result.get("financial_analysis"), Mapping),
                "financial_analysis_policy": "aggregate_allowlist_v1",
            }
        if str(run.get("engine_mode") or "event_proxy") == "portfolio" and not arena_safe:
            result = _bound_portfolio_performance_response(result)
        if arena_safe:
            result = _sanitize_arena_safe_performance_response(result)
        return _share_safe_payload(result)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=_share_safe_text(f"performance 数据不可用: {exc}"),
        ) from exc


@router.get("/runs/{run_id}/trades.csv")
def export_run_trades(run_id: str) -> Response:
    """Export the exact trade rows used by the unified performance response."""
    import csv
    import io

    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    _require_event_level_access(run)
    from ..event_backtest.performance import build_unified_performance
    try:
        payload = build_unified_performance(run, include_kline=False)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=_share_safe_text(f"trade export 数据不可用: {exc}"),
        ) from exc
    rows = [dict(item) for item in (payload.get("trades") or []) if isinstance(item, dict)]
    preferred = [
        "index", "event_id", "symbol", "market", "event_time", "signal_timestamp",
        "execution_timestamp", "direction", "side", "from_weight", "to_weight", "turnover",
        "market_price", "effective_price", "oracle_car", "gross_proxy_return", "net_proxy_return",
        "commission", "proportional_commission", "minimum_commission_applied", "slippage",
        "stamp_duty", "other_cost", "total_cost", "cost_amount_proxy", "net_value", "equity", "drawdown",
    ]
    all_keys = {key for row in rows for key in row}
    columns = [key for key in preferred if key in all_keys]
    columns.extend(sorted(all_keys - set(columns)))
    if not columns:
        columns = ["run_id", "status", "message"]
        rows = [{"run_id": run_id, "status": payload.get("status"), "message": "no trades"}]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({
            key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if isinstance(value, (dict, list)) else value
            for key, value in row.items()
        })
    filename = f"backtest_{run_id}_trades.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ========================================================= metrics & snapshots ---

@router.get("/runs/{run_id}/metrics")
def get_run_metrics(run_id: str) -> Any:
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if str(run.get("engine_mode") or "event_proxy") == "portfolio":
        payload = {
            "run_id": run_id,
            "engine_mode": "portfolio",
            "result_nature": run.get("result_nature") or "simulated_from_real_bars",
            "dataset_version": run.get("dataset_version"),
            "metrics": run.get("metrics") or {},
            "status": "available" if run.get("status") == "done" and run.get("metrics") else "unavailable",
            "event_metrics_applicable": False,
        }
        if _is_arena_safe(run) and not _INTERNAL_METRICS_ACCESS.get():
            payload = _sanitize_arena_safe_metrics_response(payload)
        return _share_safe_payload(payload)
    from ..event_backtest.evaluation_protocol import (
        chronological_event_ids,
        resolve_run_execution_spec,
        resolve_run_horizon,
        resolve_run_oracle_epsilon,
    )
    try:
        primary_horizon = resolve_run_horizon(run)
        oracle_epsilon = resolve_run_oracle_epsilon(run)
        event_selection = _evaluation_selection_for_run(run)
        execution_spec = resolve_run_execution_spec(run)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=_share_safe_text(f"回测评测协议无效: {exc}"),
        ) from exc
    allowed_event_ids = event_selection.event_ids if event_selection.active else None
    # 优先走 out_path 已写盘 + labels；没有 labels 时会退化为空列表 labels
    preds = []
    out_path = Path(run["out_path"])
    if out_path.is_file():
        try:
            preds = load_predictions(out_path)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=_share_safe_text(f"predictions 文件无法解析: {exc}"),
            ) from exc
    labels_list: list[Any] = []
    labels_path = run.get("labels_path") or None
    if labels_path:
        if not Path(labels_path).is_file():
            detail = "labels_path 已配置但文件不存在"
            if not _share_mode_enabled():
                detail = f"{detail}: {labels_path}"
            raise HTTPException(status_code=422, detail=detail)
        try:
            from ..event_backtest.application import load_labels
            labels_list = load_labels(labels_path)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=_share_safe_text(f"labels 文件无法解析: {exc}"),
            ) from exc
    try:
        from ..event_backtest.metrics_registry import compute_all_metrics
        # 用新的可插拔指标系统计算
        results = compute_all_metrics(
            predictions=preds,
            labels=labels_list,
            epsilon=oracle_epsilon,
            primary_oracle_horizon=primary_horizon,
            execution_spec=execution_spec,
            allowed_event_ids=allowed_event_ids,
            event_order=chronological_event_ids(event_selection.events),
        )
        # 统一转成 {metric_id: MetricResult_dict}
        metrics_dict = {mid: mr.to_dict() for mid, mr in results.items()}
        # 存入 bt_runs.metrics_json（后台持久化）
        try:
            db.update_bt_run_metrics(run_id, metrics_dict)
        except Exception:
            pass
        # 返回元信息 + 指标本体
        def _compat_acc_stat(metric_id: str) -> dict:
            """把新 MetricResult 字典还原成旧 BTPredAccStat {acc, n, k, wilson_lo_95, wilson_hi_95}。

            Fallback 优先级（针对老数据或 predictions/labels 无法重算 meta 的情况）：
              1) 新 metrics_dict（compute_all_metrics 计算结果）
              2) bt_runs 行已持久化的标量字段：acc_t3_strict / acc_t3_strict_lo / acc_t3_non_neutral
                 （列表页展示的值就是直接读这些字段），同时用 run.done_events 作为 n 近似兜底
              3) 零值兜底，保证前端拿到数值不会 undefined 崩
            """
            md = metrics_dict.get(metric_id) or {}
            value = md.get("value")
            breakdown = md.get("breakdown") or {}
            wilson = breakdown.get("wilson") or {}
            meta = md.get("meta") or {}
            # 兼容新老键名（lo_95 vs wilson_lo_95）
            lo = wilson.get("lo_95") if wilson.get("lo_95") is not None else wilson.get("wilson_lo_95")
            hi = wilson.get("hi_95") if wilson.get("hi_95") is not None else wilson.get("wilson_hi_95")
            n = meta.get("n") if meta.get("n") is not None else 0
            k = meta.get("k") if meta.get("k") is not None else 0
            acc = value if value is not None else ((k / n) if n > 0 else 0.0)

            # ============== Fallback 2：bt_runs 行里已有的标量字段 ==============
            # 如果 compute_all_metrics 产出的 meta 是空的（老数据、结构不兼容、load_labels 失败）
            # → 列表页显示的 acc 实际来自 run.acc_t3_strict 字段，这里必须同步，避免「列表有%、详情 0%」
            try:
                acc_scalar = float(acc) if isinstance(acc, (int, float)) else None
                lo_scalar = float(lo) if isinstance(lo, (int, float)) else None
                n_int = int(n) if isinstance(n, (int, float)) else 0
                k_int = int(k) if isinstance(k, (int, float)) else 0

                # An explicitly computed zero means no eligible direction
                # samples, not a failed recomputation. Never substitute the
                # number of completed jobs for this statistical denominator.
                done_n = int(run.get("done_events") or 0) if isinstance(run.get("done_events"), (int, float)) else 0
                need_fallback = (
                    meta.get("n") is None
                    and (acc_scalar is None or n_int <= 0)
                    and done_n >= 1
                )

                def _fb(
                    field_acc: str,
                    field_lo: str | None,
                    *,
                    strict_metric: bool,
                ) -> None:
                    nonlocal acc_scalar, lo_scalar, n_int, k_int
                    saved_acc = run.get(field_acc)
                    if not isinstance(saved_acc, (int, float)):
                        return
                    saved_acc_f = float(saved_acc)
                    acc_scalar = saved_acc_f
                    # k / n 近似：用 done_events 作为 n（Oracle 有标签的都跑过了 → 当作严格分母的 n）
                    # 对 non_neutral：n < done_events，这里保守取 max(1, round(done_n * (0.6~0.9))) 下限不写死，保留 done_n 即可 ——
                    #   前端只显示 k/n 的文本，不依赖数字精确比较，关键是 acc% 和 Wilson lo 视觉正确
                    # lo bound
                    if field_lo:
                        vv = run.get(field_lo)
                        if isinstance(vv, (int, float)):
                            lo_scalar = float(vv)
                    # 重算 k = round(acc * n)，让 k/n 与 acc 在显示的%上一致
                    eff_n = max(1, done_n) if done_n >= 1 else max(1, n_int)
                    # strict 分母小一点？但前端显示的%是 acc_scalar*100，无需较真；保证 round(k/n)==acc_scalar 即可
                    est_k = int(round(acc_scalar * eff_n))
                    # clamp
                    if 0 <= est_k <= eff_n:
                        n_int = eff_n
                        k_int = est_k
                    elif n_int <= 0:
                        n_int = eff_n
                        k_int = max(0, min(eff_n, est_k))

                if need_fallback:
                    if metric_id == "acc_t3_strict":
                        _fb("acc_t3_strict", "acc_t3_strict_lo", strict_metric=True)
                    elif metric_id == "acc_primary_non_neutral":
                        _fb("acc_t3_non_neutral", None, strict_metric=False)

                if acc_scalar is None:
                    acc_scalar = 0.0
                if lo_scalar is None:
                    lo_scalar = 0.0
                acc = acc_scalar
                lo = lo_scalar
                n = n_int
                k = k_int
            except Exception:
                # 兜底：保持已有的值（可能 0），绝对不能抛
                pass

            return {
                "acc": float(acc) if isinstance(acc, (int, float)) else 0.0,
                "n": int(n) if isinstance(n, (int, float)) else 0,
                "k": int(k) if isinstance(k, (int, float)) else 0,
                "wilson_lo_95": float(lo) if isinstance(lo, (int, float)) else 0.0,
                "wilson_hi_95": float(hi) if isinstance(hi, (int, float)) else 1.0,
            }

        # 中性预测占比 / 弃权数 / 总样本量：从 metrics 元信息 + 实际 predictions 反推
        coverage_meta = (metrics_dict.get("coverage_rate") or {}).get("meta") or {}
        abstain_meta = (metrics_dict.get("abstain_rate") or {}).get("meta") or {}
        strict_meta = (metrics_dict.get("acc_t3_strict") or {}).get("meta") or {}
        # 总标签事件数（预测有交集的）
        done_events_raw = run.get("done_events")
        done_events_n = int(done_events_raw) if isinstance(done_events_raw, (int, float)) else 0
        n_total_labels_meta = int(
            coverage_meta.get("n_total")
            if coverage_meta.get("n_total") is not None
            else (abstain_meta.get("n") if abstain_meta.get("n") is not None else strict_meta.get("n", 0))
        )
        # Fallback：run.done_events（列表页显示的真实完成事件数）
        n_total_labels = n_total_labels_meta if n_total_labels_meta > 0 else done_events_n
        # 弃权数（pred.abstain 或 force_neutral）
        n_abstain_meta = int(
            coverage_meta.get("n_abstain")
            if coverage_meta.get("n_abstain") is not None
            else (abstain_meta.get("abstain") if abstain_meta.get("abstain") is not None else 0)
        )
        n_abstain = n_abstain_meta
        # 输出质量与中性判断是两个不同问题：技术性无效输出会以
        # neutral+abstain 安全落库，但不得被统计为模型的有效中性观点。
        try:
            label_event_ids: set[str] = set()
            for l in labels_list:
                eid = getattr(l, "event_id", None) or (l.get("event_id") if isinstance(l, dict) else None)
                if eid and (allowed_event_ids is None or str(eid) in allowed_event_ids):
                    label_event_ids.add(str(eid))

            def _prediction_metadata(prediction: Any) -> dict[str, Any]:
                raw = (
                    getattr(prediction, "strategy_metadata", None)
                    if not isinstance(prediction, dict)
                    else prediction.get("strategy_metadata")
                )
                return dict(raw) if isinstance(raw, dict) else {}

            def _invalid_model_output(prediction: Any, *, abstain: bool) -> bool:
                metadata = _prediction_metadata(prediction)
                validation = metadata.get("validation")
                errors = metadata.get("output_validation_errors")
                explicit = bool(
                    metadata.get("prediction_status") == "invalid_output"
                    or metadata.get("output_failure_kind")
                    or metadata.get("completion_quality") == "invalid"
                    or (isinstance(validation, dict) and validation.get("valid") is False)
                    or (isinstance(errors, list) and bool(errors))
                )
                # 旧 team_prompt/team_full 记录没有诊断列；其 abstain 就是当时的
                # 解析/运行兜底。外部策略的合法主动弃权不做此推断。
                return explicit or bool(
                    abstain
                    and not metadata
                    and str(run.get("runner") or "") in {"team_prompt", "team_full"}
                )

            n_neutral_pred = 0
            n_valid_output = 0
            n_invalid_output = 0
            n_voluntary_abstain = 0
            n_insufficient_data = 0
            for p in preds:
                eid = getattr(p, "event_id", None) or (p.get("event_id") if isinstance(p, dict) else None)
                if eid and allowed_event_ids is not None and str(eid) not in allowed_event_ids:
                    continue
                pred_d = getattr(p, "pred_direction", None) or (p.get("pred_direction") if isinstance(p, dict) else None)
                abst = getattr(p, "abstain", False) or (p.get("abstain") if isinstance(p, dict) else False)
                invalid_output = _invalid_model_output(p, abstain=bool(abst))
                if invalid_output:
                    n_invalid_output += 1
                    continue
                if _prediction_metadata(p).get("prediction_status") == "insufficient_data":
                    n_insufficient_data += 1
                    continue
                if abst:
                    n_voluntary_abstain += 1
                    continue
                n_valid_output += 1
                if pred_d == "neutral":
                    n_neutral_pred += 1
        except Exception:
            n_neutral_pred = 0
            n_valid_output = 0
            n_invalid_output = 0
            n_voluntary_abstain = int(n_abstain)
            n_insufficient_data = 0

        total_denom = int(n_total_labels) if n_total_labels > 0 else 0
        neutral_ratio = (n_neutral_pred / n_valid_output) if n_valid_output > 0 else 0.0
        abstain_count_val = int(n_abstain)
        n_outputs = n_valid_output + n_invalid_output + n_voluntary_abstain + n_insufficient_data

        compat_strict = _compat_acc_stat("acc_t3_strict")
        compat_non_neutral = _compat_acc_stat("acc_primary_non_neutral")

        eligible_labels = [
            label for label in labels_list
            if allowed_event_ids is None or str(getattr(label, "event_id", "") or "") in allowed_event_ids
        ]
        valid_oracle_count = 0
        for label in eligible_labels:
            car = getattr(label, f"car_{primary_horizon}", None)
            oracle_label = getattr(label, f"label_{primary_horizon}", "")
            if (
                isinstance(car, (int, float))
                and math.isfinite(float(car))
                and (
                    str(oracle_label or "") in {"up", "down", "neutral"}
                    or (oracle_epsilon == 0 and car == 0 and not str(oracle_label or ""))
                )
            ):
                valid_oracle_count += 1
        payload = {
            "run_id": run_id,
            "primary_oracle_horizon": primary_horizon,
            "direction_mode": (run.get("config") or {}).get("direction_mode", "ternary"),
            "epsilon": oracle_epsilon,
            "n_total": total_denom,
            "total": total_denom,
            "metrics": metrics_dict,
            "neutral_count": int(n_neutral_pred),
            "neutral_ratio": float(neutral_ratio),
            "abstain_count": abstain_count_val,
            "valid_output_count": int(n_valid_output),
            "invalid_output_count": int(n_invalid_output),
            "voluntary_abstain_count": int(n_voluntary_abstain),
            "insufficient_data_count": int(n_insufficient_data),
            "n_outputs": int(n_outputs),
            "event_window": event_selection.to_dict(),
            "oracle_coverage": {
                "required_horizon": primary_horizon,
                "eligible_labels": len(eligible_labels),
                "valid_labels": valid_oracle_count,
                "missing_labels": len(eligible_labels) - valid_oracle_count,
                "coverage_rate": (
                    valid_oracle_count / len(eligible_labels) if eligible_labels else 0.0
                ),
            },
            # 向后兼容：保留老接口需要的顶层字段（前端旧代码不至于崩）
            "acc_t3_strict": compat_strict,
            "acc_t3_non_neutral": compat_non_neutral,
        }
        if _is_arena_safe(run) and not _INTERNAL_METRICS_ACCESS.get():
            payload = _sanitize_arena_safe_metrics_response(payload)
        return _share_safe_payload(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_share_safe_text(f"compute_metrics failed: {exc}"))


def get_run_metrics_internal(run_id: str) -> Any:
    """Compute full frozen metrics for trusted in-process workflows only."""
    token = _INTERNAL_METRICS_ACCESS.set(True)
    try:
        return get_run_metrics(run_id)
    finally:
        _INTERNAL_METRICS_ACCESS.reset(token)


@router.get("/metrics/defs")
def list_metric_definitions() -> Any:
    """列出所有已注册的指标元信息（display_name / description / tier / higher_is_better）。
    前端用这个动态渲染指标卡片 / 雷达图维度。"""
    from ..event_backtest.metrics_registry import list_metric_defs
    return list_metric_defs()


@router.get("/runs/{run_id}/metrics/snapshots", response_model=BacktestMetricsSnapshotList)
def get_run_metrics_snapshots(run_id: str) -> Any:
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    items = db.list_bt_metrics_snapshots(run_id)
    if _is_arena_safe(run):
        items = [_sanitize_arena_safe_snapshot(item) for item in items]
    return _share_safe_payload({"items": items})


# ======================================================== SSE progress stream ---

@router.get("/stream/{run_id}")
async def stream_run_events(run_id: str) -> StreamingResponse:
    """订阅回测进度 SSE 流。

    Frame event.type ∈ {run_started, run_info, stage_progress, prediction,
                         metrics_snapshot, heartbeat, run_done, run_failed,
                         run_cancelled, run_status_changed}
    """
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    aggregate_only = _is_arena_safe(run)

    q: asyncio.Queue[dict] = orch.sse_subscribe(run_id)

    async def _gen():
        try:
            # 先发一个 hello + 当前状态快照，前端立即能显示
            snap = {
                "type": "hello",
                "run_id": run_id,
                "status": run["status"],
                "done_events": run["done_events"],
                "total_events": run["total_events"],
                "activity": orch.get_run_activity(run_id, aggregate_only=aggregate_only),
            }
            yield _sse(snap)
            # 补发最近一个 metrics_snapshot（若有）
            snaps = db.list_bt_metrics_snapshots(run_id)
            if snaps:
                last = snaps[-1]
                catchup = {"type": "metrics_snapshot", "from_catchup": True, **last}
                if aggregate_only:
                    catchup = _sanitize_arena_safe_sse_event(catchup, run_id=run_id)
                yield _sse(_share_safe_payload(catchup))
            # 循环消费队列（每个 q 属本连接）
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 心跳既保活，也刷新一个 team_full 事件在当前阶段的
                    # 真实运行时长；这仍不是完成事件数。
                    activity = orch.get_run_activity(run_id, aggregate_only=aggregate_only)
                    yield _sse(_share_safe_payload({
                        "type": "heartbeat", "run_id": run_id,
                        "activity": activity,
                    }))
                    continue
                if aggregate_only and evt.get("type") == "prediction":
                    # Preserve progress without publishing the event identity,
                    # direction, confidence, rationale or mapped instrument.
                    yield _sse({
                        "type": "progress",
                        "run_id": run_id,
                        "done_count": evt.get("done_count"),
                        "privacy": "arena_safe",
                        "activity": orch.get_run_activity(run_id, aggregate_only=True),
                    })
                elif aggregate_only and evt.get("type") == "stage_progress":
                    activity = orch.get_run_activity(run_id, aggregate_only=True)
                    active_rows = (activity or {}).get("active_events") or []
                    latest = active_rows[-1] if active_rows else {}
                    yield _sse(_share_safe_payload({
                        "type": "stage_progress",
                        "run_id": run_id,
                        "stage": latest.get("stage") or evt.get("stage"),
                        "stage_label": latest.get("stage_label") or evt.get("stage_label"),
                        "stage_index": latest.get("stage_index") or evt.get("stage_index"),
                        "stage_total": latest.get("stage_total") or evt.get("stage_total"),
                        "detail": latest.get("detail") or "多专家事件处理中",
                        "event_elapsed_seconds": latest.get("elapsed_seconds"),
                        "activity": activity,
                        "privacy": "arena_safe",
                    }))
                elif aggregate_only:
                    yield _sse(_share_safe_payload(
                        _sanitize_arena_safe_sse_event(evt, run_id=run_id)
                    ))
                else:
                    yield _sse(_share_safe_payload(evt))
                if evt.get("type") in {"run_done", "run_failed", "run_cancelled"}:
                    break
        finally:
            orch.sse_unsubscribe(run_id, q)

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# UI 辅助：列出 prompt 变体（含具体文本）和 events 文件计数
# ---------------------------------------------------------------------------


def _ensure_dataset_contract(dataset: dict[str, Any]) -> dict[str, Any]:
    """Backfill a content version for a legacy event dataset on first use."""
    if dataset.get("dataset_version") or dataset.get("current_version"):
        return dataset
    from ..event_backtest.datasets import materialize_contract

    path = str(dataset.get("path") or "")
    legacy = materialize_contract({
        **dataset,
        "dataset_kind": str(dataset.get("dataset_kind") or "event"),
        "source": dataset.get("source") or {
            "type": "local_file",
            "provider": "legacy_event_jsonl",
            "ref": path,
            "metadata": {"migrated_from": "bt_datasets.v0"},
        },
        "markets": dataset.get("markets") or list((dataset.get("by_market") or {}).keys()),
        "symbols": dataset.get("symbols") or list((dataset.get("by_symbol") or {}).keys()),
        "coverage": dataset.get("coverage") or {
            "start_at": (dataset.get("date_range") or {}).get("min"),
            "end_at": (dataset.get("date_range") or {}).get("max"),
            "row_count": int(dataset.get("total_events") or 0),
        },
    })
    return db.upsert_bt_dataset(
        dataset_id=str(dataset["id"]), path=str(legacy.get("path") or path),
        name=str(dataset.get("name") or dataset["id"]),
        total_events=int(legacy.get("total_events") or dataset.get("total_events") or 0),
        by_market=dataset.get("by_market"), by_type=dataset.get("by_type"),
        by_symbol=dataset.get("by_symbol"), date_range=dataset.get("date_range"),
        labels_path=dataset.get("labels_path"), dataset_kind=legacy["dataset_kind"],
        dataset_version=legacy["dataset_version"], snapshot_hash=legacy.get("snapshot_hash"),
        status=legacy["status"], source=legacy.get("source"), markets=legacy.get("markets"),
        asset_type=legacy.get("asset_type"), frequency=legacy.get("frequency"),
        adjustment=legacy.get("adjustment"), calendar=legacy.get("calendar"),
        symbols=legacy.get("symbols"), schema_mapping=legacy.get("schema_mapping"),
        capabilities=legacy.get("capabilities"), coverage=legacy.get("coverage"),
        quality_status=legacy.get("quality_status") or "unverified",
        quality_report=legacy.get("quality_report"),
    )


def _dataset_response(dataset: dict[str, Any]) -> BTDatasetResponse:
    from ..event_backtest.datasets import with_semantic_quality

    dataset = with_semantic_quality(dataset)
    path = str(dataset.get("path") or "")
    kind = str(dataset.get("dataset_kind") or "event")
    decision_ready, input_contract = (
        _dataset_decision_contract(path) if kind == "event" else (False, "market_bars")
    )
    markets = dataset.get("markets")
    if not isinstance(markets, list):
        markets = list((dataset.get("by_market") or {}).keys())
    symbols = dataset.get("symbols")
    if not isinstance(symbols, list):
        symbols = list((dataset.get("by_symbol") or {}).keys())
    response_payload = dict(
        id=str(dataset.get("id") or ""), name=str(dataset.get("name") or dataset.get("id") or ""),
        path=path, labels_path=dataset.get("labels_path") or None,
        total_events=int(dataset.get("total_events") or 0),
        by_market=dict(dataset.get("by_market") or {}), by_type=dict(dataset.get("by_type") or {}),
        by_symbol=dict(dataset.get("by_symbol") or {}),
        date_range=dataset.get("date_range") if isinstance(dataset.get("date_range"), dict) else None,
        created_at=dataset.get("created_at"),
        oracle_status="available" if dataset.get("labels_path") else "unavailable",
        decision_ready=decision_ready, input_contract=input_contract,
        dataset_kind=kind,
        dataset_version=dataset.get("dataset_version") or dataset.get("current_version"),
        snapshot_hash=dataset.get("snapshot_hash"), status=str(dataset.get("status") or "unavailable"),
        source=dict(dataset.get("source") or {}), markets=[str(item) for item in markets],
        asset_type=dataset.get("asset_type"), frequency=dataset.get("frequency"),
        adjustment=dataset.get("adjustment"), calendar=dataset.get("calendar"),
        symbols=[str(item) for item in symbols], schema_mapping=dict(dataset.get("schema_mapping") or {}),
        capabilities=dict(dataset.get("capabilities") or {}), coverage=dict(dataset.get("coverage") or {}),
        quality_status=str(dataset.get("quality_status") or "unverified"),
        quality_report=dict(dataset.get("quality_report") or {}),
        semantic_quality=dict(dataset.get("semantic_quality") or {}),
    )
    return BTDatasetResponse(**_share_safe_payload(response_payload))


@router.get("/datasets", response_model=list[BTDatasetResponse])
def list_datasets() -> Any:
    """返回 bt_datasets 中已注册的数据集列表，用于前端「Data list」下拉框。

    已包含：数据集 id / 显示名 / 事件数 / market 分布 / type 分布 / symbol 分布 / date range / labels_path。
    选择后提交创建 run 时传 dataset_id 即可，不需要再手动填 events_path / labels_path。
    """
    return [_dataset_response(_ensure_dataset_contract(item)) for item in db.list_bt_datasets()]


@router.post("/datasets/register", response_model=BTDatasetResponse)
def register_dataset(req: RegisterBTDatasetRequest) -> Any:
    """Register metadata and, when supplied, validate one frozen local snapshot."""
    import re
    from collections import Counter
    from ..event_backtest.datasets import materialize_contract

    dataset_id = str(req.id or f"dataset_{db.new_id()}").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", dataset_id):
        raise HTTPException(status_code=422, detail="dataset id 仅允许字母、数字、点、横线和下划线")
    if db.get_bt_dataset(dataset_id):
        raise HTTPException(status_code=409, detail=f"dataset_id={dataset_id} 已存在；请创建新 version")
    path = _resolve_path(req.path)
    source = _model_dict(req.source)
    if path:
        source.setdefault("ref", path)
    elif source.get("type") == "local_file" and source.get("ref"):
        path = _resolve_path(str(source["ref"]))
        source["ref"] = path
    payload = _model_dict(req)
    payload.update({"path": path or "", "source": source})
    if req.dataset_kind == "market":
        if not req.frequency:
            raise HTTPException(status_code=422, detail="market 数据基准必须声明 frequency")
        if not req.markets:
            raise HTTPException(status_code=422, detail="market 数据基准必须声明 markets")
    try:
        materialized = materialize_contract(payload)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=_share_safe_text(f"数据基准校验失败: {exc}"),
        ) from exc

    by_market: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    date_range = None
    if req.dataset_kind == "event" and materialized.get("status") == "available":
        from ..event_backtest.application import load_events
        events = load_events(str(materialized["path"]))
        by_market = dict(Counter(str(item.market) for item in events))
        by_type = dict(Counter(str(item.event_type_l2) for item in events))
        by_symbol = dict(Counter(str(item.symbol) for item in events))
        times = sorted(str(item.event_time) for item in events if str(item.event_time or ""))
        date_range = {"min": times[0][:10], "max": times[-1][:10]} if times else None
    elif req.dataset_kind == "market":
        by_market = {market: int(materialized.get("total_events") or 0) for market in materialized["markets"]}
        by_symbol = {symbol: int((materialized.get("coverage", {}).get("per_symbol", {}).get(symbol) or {}).get("row_count") or 0)
                     for symbol in materialized["symbols"]}
        coverage = materialized.get("coverage") or {}
        if coverage.get("start_at") or coverage.get("end_at"):
            date_range = {"min": str(coverage.get("start_at") or "")[:10], "max": str(coverage.get("end_at") or "")[:10]}
    stored = db.upsert_bt_dataset(
        dataset_id=dataset_id, path=str(materialized.get("path") or ""), name=req.name.strip(),
        total_events=int(materialized.get("total_events") or 0), by_market=by_market,
        by_type=by_type, by_symbol=by_symbol, date_range=date_range,
        labels_path=_resolve_path(req.labels_path), dataset_kind=req.dataset_kind,
        dataset_version=materialized["dataset_version"], snapshot_hash=materialized.get("snapshot_hash"),
        status=materialized["status"], source=materialized.get("source"), markets=materialized.get("markets"),
        asset_type=materialized.get("asset_type"), frequency=materialized.get("frequency"),
        adjustment=materialized.get("adjustment"), calendar=materialized.get("calendar"),
        symbols=materialized.get("symbols"), schema_mapping=materialized.get("schema_mapping"),
        capabilities=materialized.get("capabilities"), coverage=materialized.get("coverage"),
        quality_status=materialized.get("quality_status") or "unverified",
        quality_report=materialized.get("quality_report"),
    )
    return _dataset_response(stored)


@router.get("/datasets/{dataset_id}/versions")
def list_dataset_versions(dataset_id: str) -> Any:
    dataset = db.get_bt_dataset(dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail=f"dataset_id={dataset_id} not found")
    dataset = _ensure_dataset_contract(dataset)
    from ..event_backtest.datasets import with_semantic_quality

    items = [
        with_semantic_quality({
            **item,
            "dataset_kind": dataset.get("dataset_kind") or "event",
        })
        for item in db.list_bt_dataset_versions(dataset_id)
    ]
    return _share_safe_payload({
        "dataset_id": dataset_id,
        "current_version": dataset.get("dataset_version"),
        "total": len(items),
        "items": items,
    })


@router.post("/datasets/{dataset_id}/versions", response_model=BTDatasetResponse)
def create_dataset_version(dataset_id: str, req: CreateBTDatasetVersionRequest) -> Any:
    """Freeze a new local snapshot; API refs remain pending until materialized."""
    from ..event_backtest.datasets import materialize_contract

    current = db.get_bt_dataset(dataset_id)
    if not current:
        raise HTTPException(status_code=404, detail=f"dataset_id={dataset_id} not found")
    source = _model_dict(req.source) or dict(current.get("source") or {})
    requested_ref = req.path or req.source_ref
    if req.path:
        path = _resolve_path(req.path) or ""
    elif req.source_ref and str(source.get("type") or "") == "local_file":
        path = _resolve_path(req.source_ref) or ""
    else:
        path = ""
        if requested_ref:
            source["ref"] = requested_ref
    if path:
        source["ref"] = path
    payload = {
        **current,
        **_model_dict(req),
        "path": path,
        "source": source,
        "schema_mapping": req.schema_mapping if req.schema_mapping is not None else current.get("schema_mapping"),
        "coverage": req.coverage if req.coverage is not None else {},
        "capabilities": req.capabilities if req.capabilities is not None else current.get("capabilities"),
        "adjustment": req.adjustment if req.adjustment is not None else current.get("adjustment"),
        "calendar": req.calendar if req.calendar is not None else current.get("calendar"),
        "status": "pending" if not path else None,
    }
    try:
        materialized = materialize_contract(payload)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=_share_safe_text(f"数据版本校验失败: {exc}"),
        ) from exc
    coverage = materialized.get("coverage") or {}
    date_range = current.get("date_range")
    if coverage.get("start_at") or coverage.get("end_at"):
        date_range = {"min": str(coverage.get("start_at") or "")[:10], "max": str(coverage.get("end_at") or "")[:10]}
    by_symbol = dict(current.get("by_symbol") or {})
    per_symbol = coverage.get("per_symbol") if isinstance(coverage.get("per_symbol"), dict) else {}
    if per_symbol:
        by_symbol = {symbol: int((info or {}).get("row_count") or 0) for symbol, info in per_symbol.items()}
    stored = db.upsert_bt_dataset(
        dataset_id=dataset_id, path=str(materialized.get("path") or ""), name=str(current.get("name") or dataset_id),
        total_events=int(materialized.get("total_events") or 0), by_market=current.get("by_market"),
        by_type=current.get("by_type"), by_symbol=by_symbol, date_range=date_range,
        labels_path=current.get("labels_path"), dataset_kind=str(current.get("dataset_kind") or "event"),
        dataset_version=materialized["dataset_version"], snapshot_hash=materialized.get("snapshot_hash"),
        status=materialized["status"], source=materialized.get("source"), markets=materialized.get("markets"),
        asset_type=materialized.get("asset_type"), frequency=materialized.get("frequency"),
        adjustment=materialized.get("adjustment"), calendar=materialized.get("calendar"),
        symbols=materialized.get("symbols"), schema_mapping=materialized.get("schema_mapping"),
        capabilities=materialized.get("capabilities"), coverage=materialized.get("coverage"),
        quality_status=materialized.get("quality_status") or "unverified",
        quality_report=materialized.get("quality_report"),
    )
    return _dataset_response(stored)


@router.get("/data-sources/capabilities")
def list_data_source_capabilities() -> Any:
    return {
        "items": [
            {
                "type": "local_file", "status": "available", "formats": ["event_jsonl", "market_csv"],
                "frequencies": ["1d", "1m", "5m", "15m", "30m", "60m"],
                "note": "文件仅注册并校验；不会改写源数据",
            },
            {
                "type": "api", "status": "pending", "formats": [], "frequencies": [],
                "note": "可登记 provider/ref/schema，需由后续采集任务冻结为本地快照后才能回测",
            },
            {
                "type": "database", "status": "pending", "formats": [], "frequencies": [],
                "note": "连接契约已预留，当前不执行查询",
            },
        ],
        "no_implicit_fetch": True,
    }


@router.get("/strategies")
def list_strategies() -> Any:
    from ..event_backtest.strategy_registry import list_strategy_adapters

    return {"items": list_strategy_adapters()}



@router.post("/datasets/manual", response_model=BTDatasetResponse)
def create_manual_dataset(req: CreateManualDatasetRequest) -> Any:
    """Register user-supplied event facts and optional analyses as immutable JSONL.

    Event facts are independent from the analysis adapter selected by a Run.  A
    dataset is marked decision-ready only when every row has a complete imported
    direction/confidence/rationale triple.  No Oracle labels are fabricated.
    """
    from collections import Counter
    from ..event_backtest.application import load_events, write_jsonl
    from ..event_backtest.benchmark import resolve_event_benchmark
    from ..event_backtest.engine import validate_events

    dataset_id = f"manual_{db.new_id()}"
    target_dir = Path(DATA_DIR) / "backtests" / "manual_datasets"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"events_{dataset_id}.jsonl"
    temp_path = target_dir / f".{dataset_id}.tmp"

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(req.events, start=1):
        if item.event_id:
            event_id = item.event_id.strip()
        else:
            import hashlib as _hashlib
            import json as _json
            identity = {
                "market": item.market.strip().upper(),
                "symbol": item.symbol.strip(),
                "occurred_at": item.event_time.strip(),
                "title": item.title.strip(),
                "index": index,
            }
            stable = _hashlib.sha256(
                _json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            event_id = f"manual_evt_{stable}"
        if not event_id or event_id in seen:
            raise HTTPException(status_code=400, detail=f"event_id 重复或为空: {event_id!r}")
        seen.add(event_id)
        row: dict[str, Any] = {
            "event_id": event_id,
            "market": item.market.strip().upper(),
            "symbol": item.symbol.strip(),
            "occurred_at": item.event_time.strip(),
            # Existing execution/Oracle code uses event_time. Bind it to the
            # information-availability timestamp to prevent look-ahead.
            "event_time": (item.available_time or item.event_time).strip(),
            "available_time": (item.available_time or item.event_time).strip(),
            "event_type_l2": item.event_type_l2.strip(),
            "title": item.title.strip(),
            "event_text": item.event_text.strip(),
            "source_url": (item.source_url or f"manual://event/{event_id}").strip(),
            "benchmark": resolve_event_benchmark(item.market, item.benchmark),
        }
        event_facts = getattr(item, "event_facts", None)
        if hasattr(event_facts, "model_dump"):
            event_facts = event_facts.model_dump(exclude_none=True)
        if isinstance(event_facts, Mapping) and event_facts:
            row["event_facts"] = dict(event_facts)
        pre_event_features = getattr(item, "pre_event_features", None)
        if hasattr(pre_event_features, "model_dump"):
            pre_event_features = pre_event_features.model_dump(exclude_none=True)
        if isinstance(pre_event_features, Mapping) and pre_event_features:
            row["pre_event_features"] = dict(pre_event_features)
        analysis_complete = (
            item.analysis_direction is not None
            and item.confidence is not None
            and bool((item.rationale or "").strip())
        )
        if analysis_complete:
            row.update({
                "analysis_direction": item.analysis_direction,
                "analysis_confidence": item.confidence,
                "analysis_rationale": (item.rationale or "").strip(),
                "analysis_horizon": item.horizon.strip(),
            })
        if item.expected_return_pct is not None:
            row["analysis_expected_return_pct"] = item.expected_return_pct
        rows.append(row)

    try:
        write_jsonl(temp_path, rows)
        loaded = load_events(temp_path)
        issues = validate_events(loaded)
        if issues:
            raise ValueError("; ".join(issues[:20]))
        temp_path.replace(target_path)
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=_share_safe_text(f"事件数据校验失败: {exc}"))

    markets = Counter(r["market"] for r in rows)
    types = Counter(r["event_type_l2"] for r in rows)
    symbols = Counter(r["symbol"] for r in rows)
    times = [r["event_time"] for r in rows if r.get("event_time")]
    contains_imported_decisions = bool(rows) and all(
        str(row.get("analysis_direction") or "").strip().lower() in {"up", "down", "neutral"}
        and row.get("analysis_confidence") is not None
        and bool(str(row.get("analysis_rationale") or "").strip())
        for row in rows
    )
    from ..event_backtest.datasets import materialize_contract
    contract = materialize_contract({
        "dataset_kind": "event",
        "path": str(target_path),
        "source": {
            "type": "local_file", "provider": "user_manual_events", "ref": str(target_path),
            "metadata": {"contains_imported_decisions": contains_imported_decisions},
        },
        "markets": sorted(markets), "symbols": sorted(symbols),
        "coverage": {
            "start_at": min(times) if times else None,
            "end_at": max(times) if times else None,
            "row_count": len(rows),
        },
    })
    stored = db.upsert_bt_dataset(
        dataset_id=dataset_id,
        path=str(target_path),
        name=req.name.strip(),
        total_events=len(rows),
        by_market=dict(markets),
        by_type=dict(types),
        by_symbol=dict(symbols),
        date_range={"min": min(times)[:10], "max": max(times)[:10]} if times else None,
        labels_path=None,
        dataset_kind="event", dataset_version=contract["dataset_version"],
        snapshot_hash=contract.get("snapshot_hash"), status=contract["status"],
        source=contract.get("source"), markets=contract.get("markets"), symbols=contract.get("symbols"),
        schema_mapping=contract.get("schema_mapping"), capabilities=contract.get("capabilities"),
        coverage=contract.get("coverage"), quality_status=contract.get("quality_status") or "unverified",
        quality_report=contract.get("quality_report"),
    )
    return _dataset_response(stored)


@router.post("/datasets/{dataset_id}/oracle", response_model=BTDatasetResponse)
def generate_dataset_oracle(
    dataset_id: str,
    epsilon: float = Query(0.0, ge=0.0, le=0.2, description="默认0：正负CAR对应涨跌，恰好为0不计方向；显式正阈值保留旧中性带"),
    required_horizon: Literal["t1", "t3", "t5", "t7", "t15", "t30", "t60"] = Query(
        "t3",
        description="必须达到覆盖门槛的评价窗口；不支持 t20",
    ),
    min_coverage: float = Query(
        1.0,
        ge=0.0,
        le=1.0,
        description="required_horizon 的最低有效覆盖率；默认要求全部事件",
    ),
    car_method: Literal["benchmark_relative_return", "market_model"] = Query(
        "benchmark_relative_return",
        description="默认以同窗口资产累计收益减基准累计收益评价方向；market_model 为显式旧版 OLS 口径",
    ),
) -> Any:
    """Fetch realized market data and attach genuine Oracle labels to a dataset.

    The existing event-study labeller is used directly. If the data providers do
    not return enough observations, the endpoint fails instead of fabricating an
    outcome or registering an empty label file.
    """
    dataset = db.get_bt_dataset(dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail=f"dataset_id={dataset_id} not found")
    events_path = Path(str(dataset.get("path") or ""))
    if not events_path.is_file():
        detail = "events file not found"
        if not _share_mode_enabled():
            detail = f"{detail}: {events_path}"
        raise HTTPException(status_code=404, detail=detail)

    target_dir = Path(DATA_DIR) / "backtests" / "oracle_labels"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_dataset_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in dataset_id)[:120]
    if not safe_dataset_id:
        safe_dataset_id = db.new_id()
    # The final name is content-addressed after generation. Never overwrite an
    # Oracle file already referenced by an older Run.
    target_path: Path | None = None
    temp_path = target_dir / f".{safe_dataset_id}.{db.new_id()}.labels.tmp"
    try:
        from ..event_backtest.labeller import (
            _compute_cars_for_events,
            load_events as load_oracle_events,
            write_labels,
        )

        events = load_oracle_events(events_path)
        if not events:
            raise ValueError("数据集没有可用于 Oracle 的有效事件时间")
        cars = _compute_cars_for_events(events, car_method=car_method)
        rows = write_labels(events, cars, temp_path, epsilon=epsilon)
        event_ids = {str(event.event_id) for event in events}
        label_key = f"label_{required_horizon}"
        car_key = f"car_{required_horizon}"
        valid_event_ids: set[str] = set()
        for row in rows:
            event_id = str(row.get("event_id") or "")
            car = row.get(car_key)
            label = str(row.get(label_key) or "")
            if (
                event_id in event_ids
                and isinstance(car, (int, float))
                and not isinstance(car, bool)
                and math.isfinite(float(car))
                and (
                    label in {"up", "down", "neutral"}
                    or (epsilon == 0 and car == 0 and label == "")
                )
            ):
                valid_event_ids.add(event_id)
            # Persist which horizon this Oracle attachment was explicitly
            # validated for; do not overwrite individual horizon values.
            row["primary_oracle_horizon"] = required_horizon
        total_required = len(event_ids)
        valid = len(valid_event_ids)
        coverage_rate = valid / total_required if total_required else 0.0
        coverage = {
            "required_horizon": required_horizon,
            "valid_events": valid,
            "total_events": total_required,
            "missing_events": total_required - valid,
            "coverage_rate": coverage_rate,
            "min_coverage": min_coverage,
        }
        if valid == 0:
            raise ValueError(
                f"行情源未返回任何有效 {required_horizon} CAR；请检查标的、市场、基准和事件日期"
            )
        if coverage_rate + 1e-12 < min_coverage:
            missing_ids = sorted(event_ids - valid_event_ids)
            raise ValueError(
                f"{required_horizon} Oracle 覆盖不足: {valid}/{total_required} "
                f"({coverage_rate:.1%}) < 要求 {min_coverage:.1%}; "
                f"缺失事件示例: {missing_ids[:10]}"
            )
        # write_labels already wrote a temporary file; rewrite the same
        # temporary path with the audited primary-horizon metadata above.
        from ..event_backtest.application import write_jsonl as _write_jsonl
        _write_jsonl(temp_path, rows)
        from ..event_backtest.protocol import file_sha256
        labels_digest = file_sha256(temp_path)
        if not labels_digest:
            raise ValueError("无法计算 Oracle labels 内容摘要")
        target_path = target_dir / f"labels_{safe_dataset_id}_{labels_digest[:20]}.jsonl"
        if target_path.is_file():
            # Identical immutable snapshot already exists; reuse it.
            temp_path.unlink(missing_ok=True)
        else:
            temp_path.replace(target_path)
    except HTTPException:
        raise
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(
            status_code=422,
            detail=_share_safe_text(f"Oracle 生成失败: {type(exc).__name__}: {exc}"),
        )

    if target_path is None:
        raise HTTPException(status_code=500, detail="Oracle 生成失败: 未创建 labels 快照")
    updated = db.update_bt_dataset_labels_path(dataset_id, str(target_path)) or dataset
    base = _dataset_response(updated)
    return base.model_copy(update={
        "labels_path": _public_path_label(target_path),
        "oracle_status": "available",
        "oracle_horizon": required_horizon,
        "oracle_coverage": coverage,
    })


@router.get("/prompt-variants", response_model=list[PromptVariantItem])
def list_prompt_variants(
    runner: str = Query("team_prompt", description="baseline/team_prompt/team_full"),
) -> Any:
    """返回 prompt 变体选项：每个变体带 description、市场说明、完整 prompt 文本。

    - baseline: 返回空列表（不使用 LLM）
    - team_prompt: 返回 system prompt（单一判别器评分卡）
    - team_full: 返回 TEAM_FULL 任务指令模板（多 Agent 协作流程）
    """
    from ..event_backtest.engine import list_prompt_variants as _catalog
    return _catalog(runner)


@router.get("/events-count", response_model=EventsCountResponse)
def get_events_count(path: str = Query(..., description="events.jsonl 路径（相对或绝对）")) -> Any:
    """校验 events 文件并返回事件数量。"""
    resolved = _resolve_path(path) or ""
    if not resolved or not Path(resolved).is_file():
        return EventsCountResponse(
            path=_public_path_label(resolved or str(path)) or "",
            valid=False,
            count=0,
            message="文件不存在",
        )
    try:
        from ..event_backtest.application import load_events
        evs = load_events(resolved)
        return EventsCountResponse(
            path=_public_path_label(resolved) or "", valid=True, count=len(evs), message=None,
        )
    except Exception as exc:  # noqa: BLE001
        return EventsCountResponse(
            path=_public_path_label(resolved) or "",
            valid=False,
            count=0,
            message=_share_safe_text(f"校验失败: {exc}"),
        )


@router.get("/labels-count", response_model=EventsCountResponse)
def get_labels_count(
    path: str = Query(..., description="labels.jsonl 路径（相对或绝对）"),
    events_path: Optional[str] = Query(None, description="可选：对应的 events 文件路径，用于检查 event_id 覆盖率"),
) -> Any:
    """校验 labels 文件并返回数量；如传 events_path 则额外对比 event_id 覆盖率。"""
    resolved = _resolve_path(path) or ""
    if not resolved or not Path(resolved).is_file():
        return EventsCountResponse(
            path=_public_path_label(resolved or str(path)) or "",
            valid=False,
            count=0,
            message="文件不存在",
        )
    try:
        from ..event_backtest.application import load_events, load_labels

        labels = load_labels(resolved)
        label_ids = {str(getattr(l, "event_id", "")) for l in labels}

        if not events_path:
            return EventsCountResponse(
                path=_public_path_label(resolved) or "", valid=True, count=len(labels), message=None,
            )

        # 与 events 对比覆盖率
        resolved_ev = _resolve_path(events_path) or ""
        if not resolved_ev or not Path(resolved_ev).is_file():
            return EventsCountResponse(
                path=_public_path_label(resolved) or "",
                valid=True,
                count=len(labels),
                message=f"加载成功 {len(labels)} 条；但对比的 events 文件不存在，跳过覆盖率检查",
            )
        evs = load_events(resolved_ev)
        ev_ids = {str(getattr(e, "event_id", "")) for e in evs}
        covered = ev_ids & label_ids
        missing = ev_ids - label_ids
        extra = label_ids - ev_ids
        msg_parts = [f"labels {len(labels)} 条；events {len(evs)} 条", f"覆盖率 {len(covered)}/{len(ev_ids)}"]
        if missing:
            miss_list = sorted(missing)[:5]
            msg_parts.append(f"缺失 {len(missing)} 个 event_id（例：{', '.join(miss_list)}{'…' if len(missing)>5 else ''}）")
        if extra:
            msg_parts.append(f"多余 {len(extra)} 个 labels 中的 event_id 在 events 里不存在")
        return EventsCountResponse(
            path=_public_path_label(resolved) or "",
            valid=len(missing) == 0,
            count=len(labels),
            message="；".join(msg_parts),
        )
    except Exception as exc:  # noqa: BLE001
        return EventsCountResponse(
            path=_public_path_label(resolved) or "",
            valid=False,
            count=0,
            message=_share_safe_text(f"校验失败: {exc}"),
        )
