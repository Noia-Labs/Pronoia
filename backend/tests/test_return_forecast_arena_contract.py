"""Forecast ranks are lower-MAE on identical prediction targets only."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.event_backtest import arena
from app.event_backtest.metrics_registry import list_metric_defs
from app.event_backtest.protocol import comparison_protocol_hash_for_run, events_snapshot_sha256


def context(run_id, error, *, horizon="t3", basis="asset_return", **metadata):
    metric = {"value": error, "meta": {"n": 10, "unit": "percentage_points", "forecast_unit": "percent", "horizon": horizon, "basis": basis, **metadata}}
    return arena.ArenaRunContext(run_id=run_id, run_info={}, metrics={"return_forecast": metric})


def test_mae_ranks_smaller_first_in_score_and_radar():
    contexts = [context("worse", 4.5), context("better", 1.25)]
    assert arena.resolve_metric_selection(contexts, ["return_forecast"], arena_type="prediction") == ["return_forecast"]
    definition = list_metric_defs()["return_forecast"]
    ranked = arena._rank_metric(contexts, "return_forecast", definition)
    assert [row["run_id"] for row in ranked] == ["better", "worse"]
    assert ranked[0]["rank"] == 1
    composite = arena._build_composite_score(contexts, ["return_forecast"], {"return_forecast": definition})
    assert composite["per_run_score"]["better"]["score"] > composite["per_run_score"]["worse"]["score"]
    radar = arena._normalize_for_radar(contexts, ["return_forecast"], {"return_forecast": definition})
    assert radar["series"][0]["values"] == [0.0]
    assert radar["series"][1]["values"] == [1.0]


@pytest.mark.parametrize("other", [
    {"horizon": "t7"}, {"basis": "asset_close_to_close_return", "horizon_bars": 3},
    {"unit": "decimal"}, {"basis": "strategy_return"},
])
def test_cross_horizon_basis_or_unit_has_no_forecast_ranking(other):
    contexts = [context("a", 1), context("b", 2, **other)]
    with pytest.raises(arena.ArenaMetricValidationError, match="相同预测周期"):
        arena.resolve_metric_selection(contexts, ["return_forecast"], arena_type="mixed")
    with pytest.raises(arena.ArenaMetricValidationError, match="没有共同可评分"):
        arena.resolve_metric_selection(contexts, None, arena_type="prediction")


def test_quant_mixed_horizon_aggregate_is_not_ranked():
    contexts = [context("a", 1, basis="asset_close_to_close_return", horizon_bars=None),
                context("b", 2, basis="asset_close_to_close_return", horizon_bars=3)]
    with pytest.raises(arena.ArenaMetricValidationError):
        arena.resolve_metric_selection(contexts, ["return_forecast"], arena_type="prediction")


def test_prediction_only_legacy_cached_zero_pnl_cannot_be_ranked_or_mutate_history():
    cached = {mid: {"value": 0.0, "meta": {"n": 20}} for mid in (
        "strategy_total_return", "strategy_max_drawdown", "strategy_sharpe_proxy", "strategy_win_rate", "portfolio_metrics")}
    run = {"engine_mode": "portfolio", "strategy_spec": {"kind": "return_forecast"}, "metrics": cached}
    result = arena._compute_run_metrics(run)
    assert all(metric["value"] is None for metric in result.values())
    assert all(metric["value"] == 0.0 for metric in cached.values())


def test_forecast_target_is_frozen_in_quant_comparison_but_model_settings_are_not():
    run = {"dataset_version": "same-bars-v1", "engine_mode": "portfolio", "result_nature": "simulated_from_real_bars",
        "config": {"dataset_snapshot": {"snapshot_hash": "same-bars", "frequency": "1d"}},
        "strategy_spec": {"type": "quant", "kind": "return_forecast", "parameters": {"horizon_bars": 3, "lookback": 20}}}
    first = comparison_protocol_hash_for_run(run)
    longer_lookback = deepcopy(run)
    longer_lookback["strategy_spec"]["parameters"]["lookback"] = 100
    assert comparison_protocol_hash_for_run(longer_lookback) == first
    next_horizon = deepcopy(run)
    next_horizon["strategy_spec"]["parameters"]["horizon_bars"] = 5
    assert comparison_protocol_hash_for_run(next_horizon) != first
    trading = deepcopy(run)
    trading["strategy_spec"] = {"type": "quant", "kind": "buy_hold"}
    assert comparison_protocol_hash_for_run(trading) != first
    hourly = deepcopy(run)
    hourly["config"]["dataset_snapshot"]["frequency"] = "1h"
    assert comparison_protocol_hash_for_run(hourly, externalize_time_axis=True) != comparison_protocol_hash_for_run(run, externalize_time_axis=True)


def test_imported_return_predictions_do_not_change_event_fact_fingerprint(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    event = {"event_id": "e", "title": "Same actual event", "market": "CN"}
    a.write_text(json.dumps({**event, "expected_return_pct": 1.5}) + "\n")
    b.write_text(json.dumps({**event, "analysis_expected_return_pct": -2}) + "\n")
    assert events_snapshot_sha256(a) == events_snapshot_sha256(b)
