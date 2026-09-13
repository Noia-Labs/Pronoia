"""Numeric forecasts compare to actual asset returns, not CAR or position PnL."""
from __future__ import annotations

import math
from dataclasses import replace

import pytest

from app.event_backtest import external_strategy, metrics, metrics_registry
from app.event_backtest.models import EventLabel, EventRecord, TeamPrediction, finite_expected_return_pct
from app.event_backtest.return_forecast import compute_return_forecast
from app.schemas import ManualBacktestEvent


def pred(event_id, value, **updates):
    return TeamPrediction(event_id, "up", run_id="r", confidence=.9, rationale="As-of facts", horizon="t3", expected_return_pct=value, **updates)


def label(event_id, actual):
    return EventLabel(event_id, "up", "down", "up", car_t3=-.5, ret_t3=actual)


def test_asset_returns_percent_units_and_missing_forecast_coverage():
    predictions = [pred("a", 3), pred("b", 0), pred("c", -2), pred("missing", None), pred("pending", 5)]
    labels = [label("a", .02), label("b", 0), label("c", -.01), label("missing", .05)]
    summary = compute_return_forecast(predictions, labels, horizon="t3")
    assert summary["mae_pct"] == pytest.approx(2 / 3)
    assert summary["rmse_pct"] == pytest.approx(math.sqrt(2 / 3))
    assert summary["bias_pct"] == 0
    assert summary["median_abs_error_pct"] == 1
    assert summary["p90_abs_error_pct"] == 1
    assert summary["direction_accuracy"] == 1
    assert summary["direction_evaluated_count"] == 2
    assert summary["up_call_hit_rate"] == summary["down_call_hit_rate"] == 1
    assert summary["pearson_ic"] is not None
    assert summary["spearman_rank_ic"] is not None
    assert summary["r_squared"] is not None
    assert summary["skill_score_vs_zero"] is not None
    assert summary["n"] == 3 and summary["n_forecasts"] == 4
    assert summary["coverage"] == .8 and summary["evaluation_coverage"] == .6
    assert summary["pending_count"] == 1 and summary["basis"] == "asset_return"
    registered = metrics_registry.compute_all_metrics(predictions=predictions, labels=labels)["return_forecast"]
    assert registered.value == summary["mae_pct"]
    assert registered.meta == summary
    assert metrics.compute_metrics(predictions=predictions, labels=labels).return_forecast == summary


def test_historical_missing_invalid_abstain_and_wrong_horizon_are_not_zero():
    predictions = [pred("missing", None), pred("abstain", 7, abstain=True),
        replace(pred("wrong_horizon", 8), horizon="t7"),
        pred("bad_unit", 9, strategy_metadata={"return_forecast_contract": {"horizon": "t3", "unit": "decimal", "basis": "asset_return"}})]
    summary = compute_return_forecast(predictions, [label(p.event_id, .01) for p in predictions], horizon="t3")
    assert summary["status"] == "not_provided"
    assert summary["n_forecasts"] == 0 and summary["coverage"] == 0
    assert summary["mae_pct"] is summary["rmse_pct"] is summary["bias_pct"] is None
    for value in (True, False, "2.5", "NaN", float("nan"), float("inf"), 10 ** 1000):
        assert finite_expected_return_pct(value) is None
    assert TeamPrediction.from_dict({"event_id": "old", "pred_direction": "up", "strategy_metadata": "legacy"}).expected_return_pct is None


def test_forecast_serialization_and_metadata_backwards_compatibility():
    p = pred("a", -1.3)
    assert TeamPrediction.from_dict(p.to_dict()) == p
    old_database_row = {"event_id": "a", "pred_direction": "up", "horizon": "t3", "strategy_metadata": p.strategy_metadata}
    assert TeamPrediction.from_dict(old_database_row).expected_return_pct == -1.3
    legacy = TeamPrediction.from_dict({"event_id": "b", "pred_direction": "up", "confidence": .7})
    assert legacy.expected_return_pct is None


def test_external_http_contract_allows_numeric_prediction_without_exposing_imports(monkeypatch):
    event = EventRecord.from_dict({"event_id": "a", "market": "US", "symbol": "SPY", "event_time": "2025-01-03T08:30:00-05:00",
        "event_type_l2": "macro", "title": "CPI", "event_text": "CPI 2.8%", "source_url": "https://example.invalid/cpi",
        "analysis_direction": "down", "expected_return_pct": 987})
    captured = []
    def post(endpoint, payload, spec):
        captured.append(payload)
        return {"event_id": "a", "direction": "up", "confidence": .8, "rationale": "CPI", "expected_return_pct": -2.3}
    monkeypatch.setattr(external_strategy, "_post", post)
    prediction = external_strategy.run_external_event_strategy([event], run_id="r", spec={"endpoint": "https://example.invalid/signal"}, model_version="custom", target_horizon="t5")[0]
    assert prediction.expected_return_pct == -2.3
    assert prediction.strategy_metadata["return_forecast_contract"]["horizon"] == "t5"
    assert captured[0]["return_forecast_contract"]["unit"] == "percent"
    assert "analysis_expected_return_pct" not in captured[0]["event"]
    for value in ("2.3", float("nan"), True):
        with pytest.raises(ValueError, match="有限数值"):
            external_strategy._prediction({"direction": "up", "confidence": .8, "rationale": "Known facts", "expected_return_pct": value}, event_id="a", run_id="r", model_version="custom", target_horizon="t5")


def test_manual_input_accepts_optional_finite_number():
    row = dict(market="US", symbol="SPY", event_time="2025-01-03T08:30:00-05:00", title="CPI")
    assert ManualBacktestEvent(**row).expected_return_pct is None
    assert ManualBacktestEvent(**row, expected_return_pct=2.1).expected_return_pct == 2.1
    with pytest.raises(ValueError):
        ManualBacktestEvent(**row, expected_return_pct=float("inf"))


def test_frozen_forecast_view_uses_matching_horizon_asset_return_and_execution_window(tmp_path):
    from app.event_backtest.application import write_jsonl
    from app.event_backtest.return_forecast_view import build_event_return_forecasts

    events = []
    for event_id, day in (("outside", 2), ("a", 3), ("missing", 4), ("pending", 5), ("wrong", 6)):
        events.append({"event_id": event_id, "market": "US", "symbol": "SPY",
            "event_time": f"2025-01-{day:02d}T08:30:00-05:00", "event_type_l2": "macro",
            "title": "CPI", "event_text": "Known information", "source_url": "https://example.invalid/cpi"})
    predictions = [pred("outside", 100), pred("a", 3), pred("missing", None), pred("pending", 0), replace(pred("wrong", 9), horizon="t7")]
    write_jsonl(tmp_path / "events.jsonl", list(reversed(events)))
    write_jsonl(tmp_path / "predictions.jsonl", [p.to_dict() for p in reversed(predictions)])
    write_jsonl(tmp_path / "labels.jsonl", [label("outside", .01).to_dict(), label("a", .02).to_dict(), label("missing", -.5).to_dict()])
    run = {"events_path": str(tmp_path / "events.jsonl"), "out_path": str(tmp_path / "predictions.jsonl"),
        "labels_path": str(tmp_path / "labels.jsonl"), "config": {"evaluation_horizon": "t3"},
        "execution_spec": {"start_date": "2025-01-03", "end_date": "2025-01-06"}}
    rows = build_event_return_forecasts(run)
    assert [row["event_id"] for row in rows] == ["a", "pending"]
    assert rows[0]["expected_return_pct"] == 3
    assert rows[0]["actual_return_pct"] == 2 and rows[0]["error_pct"] == 1
    assert rows[0]["status"] == "evaluated" and rows[0]["horizon"] == "t3"
    assert rows[1]["expected_return_pct"] == 0
    assert rows[1]["actual_return_pct"] is rows[1]["error_pct"] is None
    assert rows[1]["status"] == "pending"
    write_jsonl(tmp_path / "labels.jsonl", [label("pending", 2.0).to_dict()])
    large_error = build_event_return_forecasts(run)[1]
    assert large_error["actual_return_pct"] == 200.0
    assert large_error["error_pct"] == -200.0
    run["labels_path"] = ""
    assert all(row["actual_return_pct"] is None for row in build_event_return_forecasts(run))
    run["out_path"] = str(tmp_path / "not-started.jsonl")
    assert build_event_return_forecasts(run) == []
