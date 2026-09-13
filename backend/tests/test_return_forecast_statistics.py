"""Financial comparison statistics for numeric return forecasts."""

from __future__ import annotations

import math

import pytest

from app.return_forecast_statistics import enrich_forecast_summary, forecast_pair_statistics


def test_perfect_forecast_metrics_and_zero_direction_policy():
    metrics = forecast_pair_statistics([(1, 1), (2, 2), (-1, -1), (0, 0)])
    assert metrics["n"] == 4
    assert metrics["mae_pct"] == metrics["rmse_pct"] == 0
    assert metrics["median_abs_error_pct"] == metrics["p90_abs_error_pct"] == 0
    assert metrics["direction_accuracy"] == 1
    assert metrics["direction_evaluated_count"] == 3
    assert metrics["direction_coverage"] == pytest.approx(3 / 4)
    assert metrics["up_call_hit_rate"] == metrics["down_call_hit_rate"] == 1
    assert metrics["pearson_ic"] == metrics["spearman_rank_ic"] == 1
    assert metrics["r_squared"] == metrics["skill_score_vs_zero"] == 1
    assert metrics["zero_baseline_rmse_pct"] == pytest.approx(math.sqrt(1.5))


def test_direction_counts_neutral_calls_and_zero_outcomes_explicitly():
    metrics = forecast_pair_statistics([(0, 1), (1, 0), (-1, -2), (1, -1)])
    assert metrics["direction_evaluated_count"] == 2
    assert metrics["direction_accuracy"] == pytest.approx(0.5)
    assert metrics["up_call_count"] == 1
    assert metrics["up_call_hit_rate"] == 0
    assert metrics["down_call_count"] == 1
    assert metrics["down_call_hit_rate"] == 1


def test_constant_actual_return_has_no_fabricated_ic_or_r_squared():
    metrics = forecast_pair_statistics([(1, 2), (2, 2), (3, 2)])
    assert metrics["pearson_ic"] is None
    assert metrics["spearman_rank_ic"] is None
    assert metrics["r_squared"] is None
    assert metrics["skill_score_vs_zero"] is not None


def test_spearman_uses_average_rank_for_ties():
    metrics = forecast_pair_statistics([(1, 1), (1, 2), (2, 3)])
    assert metrics["spearman_rank_ic"] == pytest.approx(math.sqrt(3) / 2)


def test_enrich_legacy_summary_uses_full_rows_and_keeps_horizons_separate():
    rows = [
        {"horizon_bars": 1, "expected_return_pct": 1, "actual_return_pct": 1},
        {"horizon_bars": 1, "expected_return_pct": -1, "actual_return_pct": -2},
        {"horizon_bars": 3, "expected_return_pct": 4, "actual_return_pct": 2},
        {"horizon_bars": 3, "expected_return_pct": 9, "actual_return_pct": None},
    ]
    legacy = {
        "n_forecasts": 4,
        "coverage": 0.5,
        "by_horizon": [
            {"horizon_bars": 1, "n_forecasts": 2},
            {"horizon_bars": 3, "n_forecasts": 2},
        ],
    }
    enriched = enrich_forecast_summary(legacy, rows, horizon_key="horizon_bars")
    assert enriched["evaluated_count"] == 3
    assert enriched["coverage"] == 0.5
    assert enriched["by_horizon"][0]["evaluated_count"] == 2
    assert enriched["by_horizon"][0]["mae_pct"] == pytest.approx(0.5)
    assert enriched["by_horizon"][1]["evaluated_count"] == 1
    assert enriched["by_horizon"][1]["mae_pct"] == pytest.approx(2)


def test_empty_rows_do_not_erase_a_frozen_summary():
    legacy = {"n": 8, "evaluated_count": 8, "mae_pct": 1.25}
    assert enrich_forecast_summary(legacy, [])["mae_pct"] == 1.25


def test_large_finite_forecasts_do_not_overflow_the_metric_pipeline():
    metrics = forecast_pair_statistics([(1e308, 1.0), (1e308, 2.0)])
    assert metrics["n"] == 2
    assert metrics["mae_pct"] == pytest.approx(1e308)
    assert metrics["pearson_ic"] is None
    assert metrics["r_squared"] is None
