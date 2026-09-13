"""Offline checks that newly generated Oracle directions match the model task."""
from __future__ import annotations

import json
import math
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.event_backtest import labeller


def _prices():
    dates = pd.bdate_range("2024-01-02", periods=200).date
    benchmark_returns = np.array([0] + [.002 + .004 * math.sin(i * .47) for i in range(1, 200)])
    asset_returns = np.array([0] + [.001 + 2 * benchmark_returns[i] + .00002 * math.cos(i * .79)
                                  for i in range(1, 200)])
    # A high-beta asset beats the benchmark but underperforms its OLS expectation.
    benchmark_returns[131:134] = .006
    asset_returns[131:134] = .01
    asset = pd.Series(100 * np.cumprod(1 + asset_returns), index=dates)
    benchmark = pd.Series(100 * np.cumprod(1 + benchmark_returns), index=dates)
    event = labeller.RawEvent(
        event_id="target-contract", market="CN", symbol="600519", benchmark="000300.SH",
        event_date=dates[130], event_time_raw=str(dates[130]) + "T09:00:00+08:00",
    )
    return event, asset, benchmark


def _compute(event, asset, benchmark, **kwargs):
    with patch.object(labeller, "_ak_cn_hist", return_value=asset), \
         patch.object(labeller, "_ak_cn_index_hist", return_value=benchmark):
        return labeller._compute_cars_for_events([event], **kwargs)[
            (event.event_id, event.market, event.symbol)
        ]


def test_default_matches_endpoint_outperformance_when_ols_direction_is_opposite(tmp_path):
    event, asset, benchmark = _prices()
    default = _compute(event, asset, benchmark)
    legacy = _compute(event, asset, benchmark, car_method="market_model")
    assert default["car_t3"] == pytest.approx(default["t3"] - default["bm_t3"])
    assert default["car_t3"] > .005
    assert legacy["car_t3"] < -.005
    assert default["car_method"] == "benchmark_relative_return"
    assert legacy["car_method"] == "market_model"
    assert default["car_t3_pvalue"] is None
    assert default["car_t3_tstat"] is None
    assert default["t3"] == legacy["t3"]  # Numeric asset-return evaluation is unchanged.
    assert legacy["car_methods"]["t3"] == "market_model"
    path = tmp_path / "new.labels.jsonl"
    rows = labeller.write_labels([event], {(event.event_id, event.market, event.symbol): default}, path)
    assert rows[0]["label_t3"] == "up"
    saved = json.loads(path.read_text())
    assert saved["car_method"] == "benchmark_relative_return"
    assert saved["car_methods"]["t3"] == "benchmark_relative_return"


@pytest.mark.parametrize("missing_index", [130, 133])
@pytest.mark.parametrize("method", ["benchmark_relative_return", "market_model"])
def test_missing_exact_benchmark_endpoint_does_not_fabricate_car(missing_index, method):
    event, asset, benchmark = _prices()
    benchmark = benchmark.drop(benchmark.index[missing_index])
    row = _compute(event, asset, benchmark, car_method=method)
    assert row["t3"] is not None
    assert row["bm_t3"] is None
    assert row["car_t3"] is None
    assert "t3" not in row["car_methods"]


def test_explicit_ols_fallback_records_actual_endpoint_method():
    event, asset, benchmark = _prices()
    # Keep the event window, remove the history needed to fit alpha and beta.
    row = _compute(event, asset.iloc[130:], benchmark.iloc[130:], car_method="market_model")
    assert row["car_t3"] == pytest.approx(row["t3"] - row["bm_t3"])
    assert row["car_method_requested"] == "market_model"
    assert row["car_method"] == "benchmark_relative_return"
    assert row["car_methods"]["t3"] == "benchmark_relative_return"
    assert row["car_t3_pvalue"] is None


def test_ols_mixed_horizon_fallback_methods_are_not_hidden():
    event, asset, benchmark = _prices()
    benchmark = benchmark.drop(benchmark.index[135])
    row = _compute(event, asset, benchmark, car_method="market_model")
    assert row["car_methods"]["t3"] == "market_model"
    assert row["car_methods"]["t7"] == "benchmark_relative_return"
    assert row["car_method"] == "market_model_with_endpoint_fallback"


@pytest.mark.parametrize("method", ["benchmark_relative_return", "market_model"])
def test_cash_benchmark_still_uses_asset_return(method):
    event, asset, benchmark = _prices()
    event.benchmark = "cash"
    with patch.object(labeller, "_ak_cn_hist", return_value=asset), \
         patch.object(labeller, "_ak_cn_index_hist", side_effect=AssertionError("unexpected benchmark fetch")):
        row = labeller._compute_cars_for_events([event], car_method=method)[
            (event.event_id, event.market, event.symbol)
        ]
    assert row["car_method"] == "raw_asset_return"
    assert row["car_t3"] == row["t3"]
    assert row["bm_t3"] is None
    assert row["benchmark_ticker"] is None


def test_unknown_method_rejected_before_fetch():
    event, _, _ = _prices()
    with patch.object(labeller, "_ak_cn_hist", side_effect=AssertionError("unexpected fetch")):
        with pytest.raises(ValueError, match="car_method"):
            labeller._compute_cars_for_events([event], car_method="unknown")


def test_cli_help_exposes_method_choice(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["labeller", "--help"])
    with pytest.raises(SystemExit) as exit_result:
        labeller.main()
    assert exit_result.value.code == 0
    output = capsys.readouterr().out
    assert "--car-method" in output
    assert "benchmark_relative_return" in output


def test_binary_generation_preserves_flat_outcome_without_inventing_direction(tmp_path):
    event, _, _ = _prices()
    key = (event.event_id, event.market, event.symbol)
    cars = {key: {"car_method": "benchmark_relative_return", "car_t1": .001,
                  "car_t3": 0, "car_t5": -.001}}
    row = labeller.write_labels([event], cars, tmp_path / "binary.jsonl", epsilon=0)[0]
    assert row["epsilon"] == 0
    assert row["label_t1"] == "up"
    assert row["label_t3"] == ""
    assert row["car_t3"] == 0
    assert row["label_t5"] == "down"
    legacy = labeller.write_labels([event], cars, tmp_path / "legacy.jsonl", epsilon=.005)[0]
    assert legacy["epsilon"] == .005
    assert legacy["label_t1"] == legacy["label_t3"] == legacy["label_t5"] == "neutral"


def test_user_facing_label_cli_defaults_zero_and_allows_legacy_override():
    from app.event_backtest.cli import build_bt_parser
    parser = build_bt_parser()
    args = parser.parse_args(["label", "--events", "events.jsonl", "--out", "labels.jsonl"])
    assert args.epsilon == 0
    args = parser.parse_args(["label", "--events", "events.jsonl", "--out", "labels.jsonl", "--epsilon", ".005"])
    assert args.epsilon == .005


def test_labeller_main_default_zero_reaches_output(monkeypatch, tmp_path):
    event, _, _ = _prices()
    out = tmp_path / "cli-labels.jsonl"
    monkeypatch.setattr("sys.argv", ["labeller", "--events", "events.jsonl", "--out", str(out)])
    cars = {(event.event_id, event.market, event.symbol): {"car_t3": .001}}
    with patch.object(labeller, "load_events", return_value=[event]), \
         patch.object(labeller, "_compute_cars_for_events", return_value=cars):
        labeller.main()
    row = json.loads(out.read_text())
    assert row["epsilon"] == 0
    assert row["label_t3"] == "up"
