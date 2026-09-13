"""Portfolio performance responses stay bounded while frozen results stay full."""
from __future__ import annotations

import copy

from app.routes import backtest


def _rows(size: int, *, special_index: int | None = None) -> list[dict]:
    rows = [
        {
            "index": index,
            "timestamp": f"bar-{index}",
            "net_value": 1 + index / max(1, size),
            "equity": 100_000 + index,
            "drawdown": -0.01,
            "benchmark_net_value": 1 + index / max(1, size * 2),
            "open": 100 + index,
            "high": 101 + index,
            "low": 99 + index,
            "close": 100.5 + index,
            "volume": 1_000 + index,
            "weight": index % 2,
            "target_weight": index % 2,
            "market_value": index * 10,
            "expected_return_pct": index / 100,
            "actual_return_pct": index / 200,
            "error_pct": index / 300,
            "total_cost": index / 10,
        }
        for index in range(size)
    ]
    if special_index is not None:
        rows[special_index]["drawdown"] = -0.95
    return rows


def _portfolio_payload(size: int) -> dict:
    rows = _rows(size, special_index=size // 3)
    payload = {
        "run_id": "large",
        "engine_mode": "portfolio",
        "status": "available",
        "summary": {"bar_count": size, "trade_count": size, "max_drawdown": 0.95},
        "data_quality": {"bar_count": size, "frozen_bars_embedded_in_result": True},
        "effective_protocol": {"applied": {"engine": "single_asset_next_open"}},
    }
    for field in backtest._PORTFOLIO_PERFORMANCE_LIMITS:
        payload[field] = list(rows)
    return payload


def test_large_portfolio_performance_is_bounded_with_explicit_sampling_metadata(monkeypatch):
    from app.event_backtest import performance

    size = 5_001
    frozen_view = _portfolio_payload(size)
    monkeypatch.setattr(
        backtest.db,
        "get_bt_run",
        lambda run_id: {"id": run_id, "engine_mode": "portfolio", "visibility": "private"},
    )
    monkeypatch.setattr(performance, "build_unified_performance", lambda *args, **kwargs: frozen_view)

    response = backtest.get_run_performance("large", include_kline=False, kline_limit=6)

    sampling = response["response_sampling"]
    assert sampling["applied"] is True
    assert sampling["scope"] == "display_only"
    assert sampling["source_result_preserved"] is True
    assert sampling["metrics_basis"] == "full_frozen_result"
    assert response["summary"] == frozen_view["summary"]
    assert response["data_quality"] == frozen_view["data_quality"]
    assert response["effective_protocol"] == frozen_view["effective_protocol"]
    for field, limit in backtest._PORTFOLIO_PERFORMANCE_LIMITS.items():
        metadata = sampling["series"][field]
        assert len(response[field]) == limit
        assert metadata == {
            "total_count": size,
            "returned_count": limit,
            "omitted_count": size - limit,
            "limit": limit,
            "sampled": True,
        }
        assert response[field][0]["index"] == 0
        assert response[field][-1]["index"] == size - 1
    # Every displayed execution must have an exact minute/time anchor in each
    # non-empty portfolio timeline; same-day fallback would be misleading.
    trade_timestamps = {row["timestamp"] for row in response["trades"]}
    for field in backtest._PORTFOLIO_TIMELINE_FIELDS:
        if response[field]:
            assert trade_timestamps <= {row["timestamp"] for row in response[field]}
    assert any(point["drawdown"] == -0.95 for point in response["equity_curve"])
    # The endpoint view must not truncate or rewrite the builder's full data.
    assert all(len(frozen_view[field]) == size for field in backtest._PORTFOLIO_PERFORMANCE_LIMITS)
    exported = backtest.export_run_trades("large")
    assert len(bytes(exported.body).decode("utf-8-sig").splitlines()) - 1 == size


def test_small_portfolio_performance_lists_are_unchanged(monkeypatch):
    from app.event_backtest import performance

    frozen_view = _portfolio_payload(8)
    monkeypatch.setattr(
        backtest.db,
        "get_bt_run",
        lambda run_id: {"id": run_id, "engine_mode": "portfolio", "visibility": "private"},
    )
    monkeypatch.setattr(performance, "build_unified_performance", lambda *args, **kwargs: frozen_view)

    response = backtest.get_run_performance("small", include_kline=False, kline_limit=6)

    assert response["response_sampling"]["applied"] is False
    for field in backtest._PORTFOLIO_PERFORMANCE_LIMITS:
        assert response[field] == frozen_view[field]
        assert response["response_sampling"]["series"][field]["omitted_count"] == 0


def test_arena_safe_financial_analysis_uses_server_side_allowlist(monkeypatch):
    from app.event_backtest import performance

    frozen_view = _portfolio_payload(8)
    frozen_view["financial_analysis"] = {
        "schema_version": "finance-v1",
        "status": "available",
        "metrics_basis": "full_frozen_result",
        "units": {"money": "CNY", "duration": "bars", "future_unit": "private-only"},
        "period": {
            "start_at": "2025-01-01T09:31:00+08:00",
            "end_at": "2025-01-01T09:38:00+08:00",
            "frequency": "1m", "bar_count": 8, "return_observation_count": 7,
            "annualization_periods": 60_480, "estimated_years": 7 / 60_480,
        },
        "returns": {"total_return": 0.12, "annualized_return": 0.2, "future_metric": 99},
        "risk": {
            "max_drawdown": 0.08, "sharpe_ratio": 1.2,
            "peak_timestamp": "2025-01-01T09:32:00+08:00",
            "trough_timestamp": "2025-01-01T09:34:00+08:00",
            "recovery_timestamp": "2025-01-01T09:36:00+08:00",
            "peak_to_trough_bars": 2, "future_metric": 99,
        },
        "benchmark": {
            "correlation": 0.5, "aligned_return_count": 7,
            "aligned_start_at": "2025-01-01T09:31:00+08:00",
            "aligned_end_at": "2025-01-01T09:38:00+08:00",
            "future_metric": 99,
        },
        "trading": {"position_change_count": 4, "buy_count": 2, "active_bar_count": 7},
        "exposure": {"current_exposure": -1.0, "long_exposure_ratio": 0.5},
        "costs": {"total_cost": 12.0, "cost_drag_ratio_to_initial_capital": 0.0012},
        "methodology": {"risk_free_rate": 0.0, "trade_basis": "private detail", "future_method": "private-only"},
        "future_private_section": {"future_metric": "must not leak"},
    }
    frozen_view["summary"].update({
        "total_return": 0.12,
        "trade_count": 4,
        "total_cost": 12.0,
        "future_private_metric": 99,
    })
    frozen_view["dataset"] = {
        "name": "safe benchmark", "symbol": "000300.SH", "market": "CN",
        "frequency": "1m", "source_type": "local_file",
        "source_ref": "/private/frozen.csv", "fingerprint": "secret-fingerprint",
        "metadata": {
            "requested_start": "2024-01-01", "available_start": "2025-01-01",
            "effective_start": "2025-01-01", "window_clipped": True,
        },
    }
    frozen_view["strategy"] = {
        "kind": "declarative_rules", "parameters": {"secret_threshold": 1.5},
    }
    frozen_view["effective_protocol"] = {
        "requested": {"start_date": "2024-01-01", "secret": "rule"},
        "applied": {
            "engine": "single_asset_next_open", "dataset_version": "dsv-safe",
            "benchmark": "dataset_asset_buy_hold", "start_at": "2025-01-01",
            "end_at": "2025-01-08", "window_clipped": True,
            "selected_bar_count": 8, "commission_bps": 3,
        },
        "recorded_not_applied": {"private_option": {"requested": 1}},
    }
    frozen_view["data_quality"].update({
        "source_ref": "/private/frozen.csv", "warnings": ["secret warning"],
    })
    visibility = {"value": "private"}
    monkeypatch.setattr(
        backtest.db,
        "get_bt_run",
        lambda run_id: {
            "id": run_id, "engine_mode": "portfolio", "visibility": visibility["value"],
        },
    )
    monkeypatch.setattr(
        performance,
        "build_unified_performance",
        lambda *args, **kwargs: copy.deepcopy(frozen_view),
    )

    private = backtest.get_run_performance("private", include_kline=False, kline_limit=6)
    assert private["financial_analysis"] == frozen_view["financial_analysis"]

    visibility["value"] = "arena_safe"
    protected = backtest.get_run_performance("protected", include_kline=False, kline_limit=6)
    safe = protected["financial_analysis"]

    assert protected["privacy"]["financial_analysis_redacted"] is True
    assert protected["privacy"]["financial_analysis_policy"] == "aggregate_allowlist_v1"
    assert safe["returns"] == {"total_return": 0.12, "annualized_return": 0.2}
    assert safe["risk"] == {
        "max_drawdown": 0.08, "sharpe_ratio": 1.2, "peak_to_trough_bars": 2,
    }
    assert safe["benchmark"] == {"aligned_return_count": 7, "correlation": 0.5}
    assert safe["costs"] == {
        "total_cost": 12.0, "cost_drag_ratio_to_initial_capital": 0.0012,
    }
    assert safe["period"] == {
        "frequency": "1m", "bar_count": 8, "return_observation_count": 7,
        "annualization_periods": 60_480, "estimated_years": 7 / 60_480,
    }
    assert safe["units"] == {"money": "CNY", "duration": "bars"}
    assert safe["methodology"] == {"risk_free_rate": 0.0, "trade_basis": "private detail"}
    assert "trading" not in safe
    assert "exposure" not in safe
    assert "future_private_section" not in safe
    assert protected["summary"]["total_return"] == 0.12
    assert protected["summary"]["total_cost"] == 12.0
    assert "trade_count" not in protected["summary"]
    assert "future_private_metric" not in protected["summary"]
    assert protected["dataset"] == {
        "name": "safe benchmark", "symbol": "000300.SH", "market": "CN",
        "frequency": "1m", "source_type": "local_file",
    }
    assert protected["strategy"] == {
        "details_redacted": True, "visibility": "arena_safe",
    }
    assert protected["effective_protocol"] == {
        "requested": {},
        "applied": {
            "engine": "single_asset_next_open", "dataset_version": "dsv-safe",
            "benchmark": "dataset_asset_buy_hold",
        },
        "recorded_not_applied": {},
        "privacy": "arena_safe",
    }
    assert "source_ref" not in protected["data_quality"]
    assert "warnings" not in protected["data_quality"]
    serialized = repr(protected)
    for secret in (
        "2024-01-01", "2025-01-01", "2025-01-08", "secret_threshold",
        "private/frozen.csv", "secret-fingerprint", "secret warning", "private_option",
    ):
        assert secret not in serialized
    assert protected["privacy"]["run_contract_redacted"] is True
    assert protected["privacy"]["performance_policy"] == "aggregate_root_allowlist_v1"


def test_arena_safe_run_dto_redacts_strategy_window_config_and_paths(tmp_path):
    from app.schemas import BacktestRunResponse

    events_path = tmp_path / "private-events.csv"
    labels_path = tmp_path / "private-labels.jsonl"
    out_path = tmp_path / "private-output.jsonl"
    result_path = tmp_path / "private-result.json"
    for path in (events_path, labels_path, out_path, result_path):
        path.write_text("private-artifact", encoding="utf-8")

    private = {
        "id": "safe-run", "name": "published", "status": "done",
        "runner": "declarative_rules", "evaluation_horizon": "t3",
        "strategy_type": "quant", "engine_mode": "portfolio",
        "result_nature": "simulated_from_real_bars", "dataset_id": "index",
        "dataset_name": "benchmark", "dataset_version": "dsv-safe",
        "protocol_hash": "protocol", "comparison_protocol_hash": "comparison",
        "comparison_signature": {
            "version": "pronoia-arena-comparison-v2",
            "dataset": {"dataset_version": "dsv-safe", "snapshot_hash": "a" * 64},
            "window": {"start_date": "SECRET_DATE"}, "frequency": "1m",
            "benchmark": "dataset_asset_buy_hold",
            "execution_timing": {"signal_timing": "bar_close", "execution_delay": "next_open", "price_field": "next_open"},
            "execution_constraints": {"max_abs_weight": 1.0, "allow_short": True},
            "costs": {"commission_bps": 3.0, "private_cost": "SECRET_COST"},
            "evaluation": {"evaluator_version": "portfolio-v1", "annualization_periods": 12096},
            "engine_mode": "portfolio", "result_nature": "simulated_from_real_bars",
        },
        "visibility": "arena_safe", "oracle_status": "unavailable",
        "strategy_spec": {"parameters": {"threshold": "SECRET_RULE"}},
        "execution_spec": {"requested": {"start_date": "SECRET_DATE"}},
        "prompt_variant": "SECRET_PROMPT", "model_version": "SECRET_MODEL",
        "events_path": str(events_path), "labels_path": str(labels_path),
        "out_path": str(out_path), "result_path": str(result_path),
        "ckpt_dir": "/private/ckpt", "concurrency": 4, "total_events": 10,
        "done_events": 10, "completion_quality": "complete", "warning_count": 0,
        "invalid_output_count": 0, "voluntary_abstain_count": 0,
        "insufficient_data_count": 0, "acc_t3_strict": 0.5,
        "acc_t3_strict_lo": 0.4, "acc_t3_non_neutral": 0.5,
        "config": {"strategy_spec": {"secret": "SECRET_CONFIG"}},
        "metrics": {"private_breakdown": {"event_id": "SECRET_EVENT"}},
        "created_at": "2025-01-01", "updated_at": "2025-01-02",
        "started_at": "2025-01-01", "finished_at": "2025-01-02",
        "error_msg": "SECRET_ERROR", "activity": None,
    }

    protected = backtest._sanitize_arena_safe_run_response(private)

    assert protected["strategy_spec"] is None
    assert protected["execution_spec"] is None
    assert protected["comparison_signature"] is None
    assert protected["config"]["arena_eligibility"] == {
        "eligible": True, "formal_eligible": True,
        "has_event_snapshot": True, "has_labels_snapshot": True,
        "has_result_artifact": True, "event_content_verified": False,
        "prediction_only": False, "has_numeric_forecast": False,
        "evaluated_forecast_count": None, "point_in_time_enforced": False,
        "performance_sample_sufficient": False,
        "forecast_sample_sufficient": False,
        "external_portfolio": False, "model_lab_demo": False,
        "small_sample_redacted": False,
    }
    public_comparison = protected["config"]["arena_comparison"]
    assert public_comparison["dataset"] == {
        "dataset_version": "dsv-safe", "snapshot_hash": "a" * 64,
    }
    assert public_comparison["costs"] == {"commission_bps": 3.0}
    assert "window" not in public_comparison
    assert "frequency" not in public_comparison
    assert "annualization_periods" not in public_comparison["evaluation"]
    assert protected["metrics"] is None
    assert protected["events_path"] == "[arena-safe-redacted]"
    assert protected["out_path"] == "[arena-safe-redacted]"
    assert protected["error_msg"] == "运行失败；Arena-safe 详细错误已隐藏。"
    serialized = repr(protected)
    for secret in (
        "SECRET_DATE", "SECRET_RULE", "SECRET_PROMPT", "SECRET_MODEL",
        "SECRET_CONFIG", "SECRET_EVENT", "SECRET_ERROR", "SECRET_COST",
        str(tmp_path),
    ):
        assert secret not in serialized
    # Legacy response-model fields receive safe placeholders rather than
    # turning an Arena-safe GET into a response-validation 500.
    BacktestRunResponse(**protected)


def test_arena_safe_metrics_and_sse_use_strict_aggregate_allowlists():
    metrics = {
        "strategy_total_return": {
            "value": 0.1, "display_name": "return", "description": "aggregate",
            "tier": "core", "higher_is_better": True,
            "breakdown": {"secret_trade": "SECRET_TRADE"},
            "meta": {"n": 10, "method": "SECRET_METHOD"},
        },
        "portfolio_metrics": {
            "value": 0.1,
            "meta": {"trade_count": 7, "profit_factor": 2, "total_turnover": 9},
        },
        "private_future_metric": {"value": "SECRET_FUTURE"},
    }
    payload = {
        "run_id": "safe", "engine_mode": "portfolio", "status": "available",
        "dataset_version": "SECRET_VERSION", "metrics": metrics,
        "event_window": {"start_date": "SECRET_DATE"},
    }

    protected = backtest._sanitize_arena_safe_metrics_response(payload)

    assert protected["metrics"]["strategy_total_return"]["value"] == 0.1
    assert protected["metrics"]["strategy_total_return"]["meta"] == {"n": 10}
    assert "portfolio_metrics" not in protected["metrics"]
    assert "event_window" not in protected
    assert "dataset_version" not in protected
    serialized = repr(protected)
    for secret in ("SECRET_TRADE", "SECRET_METHOD", "SECRET_FUTURE", "SECRET_VERSION", "SECRET_DATE"):
        assert secret not in serialized

    done = backtest._sanitize_arena_safe_sse_event({
        "type": "run_done", "done_count": 10, "metrics": metrics,
    }, run_id="safe")
    failed = backtest._sanitize_arena_safe_sse_event({
        "type": "run_failed", "error": "SECRET_ERROR", "traceback": "SECRET_TRACE",
    }, run_id="safe")
    assert done == {
        "type": "run_done", "run_id": "safe", "done_count": 10,
        "privacy": "arena_safe",
    }
    assert failed["error"] == "运行失败；Arena-safe 详细错误已隐藏。"
    assert "SECRET" not in repr(done) + repr(failed)


def test_arena_safe_small_samples_hide_outcomes_and_sampling_trade_counts():
    small_metrics = backtest._sanitize_arena_safe_metrics_response({
        "run_id": "small", "engine_mode": "event_proxy", "status": "available",
        "n_outputs": 1, "neutral_count": 0, "acc_t3_strict": {"acc": 1.0},
        "metrics": {"strategy_total_return": {"value": 0.25, "meta": {"n": 1}}},
        "event_window": {"start_date": "SECRET_DATE"},
    })
    assert small_metrics["metrics"] == {}
    assert "acc_t3_strict" not in small_metrics
    assert "n_outputs" not in small_metrics
    assert small_metrics["privacy"]["small_sample_redacted"] is True

    snapshot = backtest._sanitize_arena_safe_snapshot({
        "done_count": 1, "acc_t3_strict": 1.0, "neutral_ratio": 0.0,
    })
    assert snapshot["done_count"] == 1
    assert "acc_t3_strict" not in snapshot
    assert "neutral_ratio" not in snapshot

    performance = backtest._sanitize_arena_safe_performance_response({
        "run_id": "small", "engine_mode": "event_proxy", "status": "available",
        "summary": {"n_predictions": 1, "total_return": 0.25},
        "financial_analysis": {"status": "available", "returns": {"total_return": 0.25}},
        "return_forecast": {"status": "available", "mae_pct": 0.01},
        "equity_curve": [{"index": 0, "net_value": 1.25}],
        "response_sampling": {
            "series": {"trades": {"total_count": 1, "returned_count": 1}},
        },
    })
    assert performance["summary"] == {}
    assert performance["financial_analysis"] == {
        "status": "redacted", "reason": "arena_safe_small_sample",
    }
    assert performance["return_forecast"] == {}
    assert performance["equity_curve"] == []
    assert "response_sampling" not in performance
    assert performance["privacy"]["small_sample_redacted"] is True

    large_performance = backtest._sanitize_arena_safe_performance_response({
        "run_id": "large", "engine_mode": "portfolio", "status": "available",
        "summary": {"bar_count": 100, "total_return": 0.25},
        "financial_analysis": {"trading": {"active_bar_count": 50}},
        "response_sampling": {
            "series": {"trades": {"total_count": 7, "returned_count": 7}},
        },
    })
    assert large_performance["summary"]["total_return"] == 0.25
    assert "response_sampling" not in large_performance


def test_arena_safe_uses_effective_metric_and_trade_samples_not_total_events():
    protected_metrics = backtest._sanitize_arena_safe_metrics_response({
        "run_id": "thin", "engine_mode": "event_proxy", "status": "available",
        "n_outputs": 10, "total": 10, "acc_t3_strict": 0.0,
        "acc_t3_non_neutral": 0.0,
        "metrics": {
            "acc_t3_strict": {"value": 0.0, "meta": {"n": 1, "k": 0}},
            "acc_primary_non_neutral": {"value": 0.0, "meta": {"n": 1, "k": 0}},
            "return_forecast": {
                "value": 0.65,
                "meta": {"n_predictions": 10, "evaluated_count": 1},
            },
            "coverage_rate": {"value": 0.1, "meta": {"n_total": 10, "n_covered": 1}},
            "strategy_total_return": {
                "value": -0.0439,
                "meta": {"n_event_trades": 1, "n": 10},
            },
        },
    })

    assert "acc_t3_strict" not in protected_metrics
    assert "acc_t3_non_neutral" not in protected_metrics
    assert set(protected_metrics["metrics"]) == {"coverage_rate"}
    assert protected_metrics["privacy"]["small_sample_redacted"] is True

    protected_performance = backtest._sanitize_arena_safe_performance_response({
        "run_id": "thin", "engine_mode": "event_proxy", "status": "available",
        "summary": {
            "n_predictions": 10, "n_valid_oracle": 10, "n_trades": 1,
            "total_return": -0.0439,
        },
        "return_forecast": {
            "status": "available", "n_forecasts": 10,
            "evaluated_count": 1, "mae_pct": 0.65,
        },
        "equity_curve": [{"index": 0, "net_value": 1.0}, {"index": 1, "net_value": 0.9561}],
    })
    assert protected_performance["summary"] == {}
    assert protected_performance["return_forecast"] == {}
    assert protected_performance["equity_curve"] == []
    assert protected_performance["privacy"]["performance_sample_sufficient"] is False
    assert protected_performance["privacy"]["forecast_sample_sufficient"] is False


def test_arena_safe_portfolio_performance_uses_active_bars_not_total_bars():
    protected = backtest._sanitize_arena_safe_performance_response({
        "run_id": "thin-portfolio", "engine_mode": "portfolio", "status": "available",
        "summary": {"bar_count": 100_000, "n_trades": 1, "total_return": 0.2},
        "financial_analysis": {
            "status": "available",
            "trading": {"active_bar_count": 1},
            "returns": {"total_return": 0.2},
        },
        "equity_curve": [{"index": 0, "net_value": 1.0}, {"index": 99_999, "net_value": 1.2}],
    })
    assert protected["summary"] == {}
    assert protected["financial_analysis"] == {
        "status": "redacted", "reason": "arena_safe_small_sample",
    }
    assert protected["equity_curve"] == []


def test_arena_safe_run_hides_accuracy_when_only_one_prediction_was_scored(tmp_path):
    events = tmp_path / "events.jsonl"
    labels = tmp_path / "labels.jsonl"
    output = tmp_path / "predictions.jsonl"
    for path in (events, labels, output):
        path.write_text("frozen", encoding="utf-8")
    run = {
        "id": "thin-run", "name": "thin-run", "status": "done",
        "runner": "raw_model", "evaluation_horizon": "t3",
        "strategy_type": "event", "engine_mode": "event_proxy",
        "result_nature": "proxy", "visibility": "arena_safe",
        "events_path": str(events), "labels_path": str(labels), "out_path": str(output),
        "result_path": None, "done_events": 10, "total_events": 10,
        "acc_t3_strict": 0.0, "acc_t3_strict_lo": 0.0,
        "acc_t3_non_neutral": 0.0, "completion_quality": "partial",
        "warning_count": 9, "invalid_output_count": 0,
        "voluntary_abstain_count": 0, "insufficient_data_count": 9,
        "metrics": {
            "acc_t3_strict": {"value": 0.0, "meta": {"n": 1, "k": 0}},
            "acc_primary_non_neutral": {"value": 0.0, "meta": {"n": 1, "k": 0}},
            "return_forecast": {"value": 0.65, "meta": {"evaluated_count": 1}},
            "strategy_total_return": {"value": -0.0439, "meta": {"n_event_trades": 1}},
        },
        "config": {}, "created_at": "2026-01-01", "updated_at": "2026-01-01",
    }

    protected = backtest._sanitize_arena_safe_run_response(run)
    eligibility = protected["config"]["arena_eligibility"]
    assert protected["acc_t3_strict"] is None
    assert protected["acc_t3_strict_lo"] is None
    assert protected["acc_t3_non_neutral"] is None
    # Quality counts are still ten-event aggregates; only the one-sample
    # outcomes are suppressed.
    assert protected["insufficient_data_count"] == 9
    assert eligibility["performance_sample_sufficient"] is False
    assert eligibility["forecast_sample_sufficient"] is False
    assert eligibility["has_numeric_forecast"] is False
    assert eligibility["small_sample_redacted"] is True
