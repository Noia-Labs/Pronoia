"""Common-market model replay without models, network, storage or fake P&L."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from app.event_backtest import model_comparison_replay as replay
from app.quant_backtest.models import Bar, MarketDataset


def market(closes=(100, 110, 121, 133.1, 146.41), *, frequency="1d"):
    start = datetime(2025, 1, 1)
    step = timedelta(minutes=1) if frequency == "1m" else timedelta(days=1)
    bars = []
    for index, close in enumerate(closes):
        opening = closes[max(0, index - 1)]
        bars.append(Bar(
            (start + index * step).isoformat(), opening, max(opening, close), min(opening, close), close,
        ))
    return MarketDataset(name="Frozen fixture", symbol="TEST", market="CN", frequency=frequency, bars=tuple(bars))


def contestant(*observations, run_id="one", model_kind="event"):
    return {"run_id": run_id, "name": run_id, "model_kind": model_kind,
            "observations": list(observations), "warnings": []}


def observation(index, direction="up", **kwargs):
    return {"bar_index": index, "direction": direction, **kwargs}


def compare(data, *models, **rules):
    return replay.compare_on_market(data, list(models), {
        "holding_bars": 1, "fee_bps": 0, "slippage_bps": 0, **rules,
    })


def test_entry_is_next_open_and_never_gets_the_pre_entry_price_rise():
    data = MarketDataset(name="Gap", symbol="TEST", market="CN", frequency="1d", bars=(
        Bar("2025-01-01", 100, 200, 100, 200),
        Bar("2025-01-02", 300, 330, 300, 330),
        Bar("2025-01-03", 360, 360, 360, 360),
    ))
    payload = compare(data, contestant(observation(0)))
    model = payload["models"][0]
    assert [point["net_value"] for point in model["curve"]] == pytest.approx([1, 1.1, 1.2])
    assert model["metrics"]["total_return"] == pytest.approx(.2)
    assert payload["benchmark_curve"][-1]["net_value"] == pytest.approx(1.8)


def test_holding_expires_and_new_signal_renews_the_same_position():
    data = market()
    expired = compare(data, contestant(observation(0)))["models"][0]
    renewed = compare(data, contestant(observation(0), observation(1)))["models"][0]
    assert [point["net_value"] for point in expired["curve"]] == pytest.approx([1, 1.1, 1.1, 1.1, 1.1])
    assert renewed["metrics"]["total_return"] == pytest.approx(.21)
    assert renewed["metrics"]["trade_count"] == 2  # One entry and one expiry exit.
    assert renewed["coverage"] == {"signal_count": 2, "covered_bars": 2, "total_bars": 4, "ratio": .5}


@pytest.mark.parametrize("direction", ["down", "neutral"])
def test_bearish_or_neutral_signal_closes_position_without_shorting(direction):
    model = compare(market(), contestant(observation(0), observation(1, direction)), holding_bars=3)["models"][0]
    assert [point["net_value"] for point in model["curve"]] == pytest.approx([1, 1.1, 1.1, 1.1, 1.1])
    assert model["coverage"]["covered_bars"] == 4


def test_numeric_sign_takes_priority_and_explicit_cash_is_valid_model_behavior():
    model = compare(market(), contestant(observation(0, "up", expected_return_pct=-2, horizon_bars=1)))["models"][0]
    assert model["metrics"]["total_return"] == 0
    assert model["metrics"]["trade_count"] == 0
    assert model["coverage"]["signal_count"] == 1
    assert model["coverage"]["covered_bars"] == 1


def test_fee_slippage_and_drawdown_come_from_executed_turnover():
    model = compare(market((100, 100, 100, 100)), contestant(observation(0)), fee_bps=3, slippage_bps=2)["models"][0]
    expected_return = (1 - .0005) ** 2 - 1
    assert model["metrics"]["total_return"] == pytest.approx(expected_return)
    assert model["metrics"]["max_drawdown"] == pytest.approx(-expected_return)
    assert model["metrics"]["total_turnover"] == 2
    assert model["metrics"]["total_cost"] == pytest.approx(-expected_return * 100_000)
    assert model["curve"][-1]["drawdown"] == pytest.approx(expected_return)


def test_uncovered_bars_are_cash_not_benchmark_returns():
    payload = compare(market(), contestant(observation(2)))
    model = payload["models"][0]
    assert [point["net_value"] for point in model["curve"]] == pytest.approx([1, 1, 1, 1.1, 1.1])
    assert model["coverage"]["ratio"] == .25
    assert payload["benchmark_curve"][-1]["net_value"] == pytest.approx(1.4641)
    assert any("空仓" in warning for warning in model["warnings"])
    assert "ranking" not in payload


@pytest.mark.parametrize("observations", [[], [observation(4)]])
def test_no_executable_prediction_has_an_actionable_error(observations):
    with pytest.raises(replay.ModelComparisonError, match="没有可执行信号"):
        compare(market(), contestant(*observations))


@pytest.mark.parametrize("index", [-1, 5, True, 1.5, "1"])
def test_bad_or_out_of_range_indices_are_rejected(index):
    with pytest.raises(replay.ModelComparisonError, match="bar_index"):
        compare(market(), contestant(observation(index)))


def test_duplicate_signal_bar_is_not_silently_resolved_by_input_order():
    with pytest.raises(replay.ModelComparisonError, match="合并规则"):
        compare(market(), contestant(observation(0, "up"), observation(0, "down")))


@pytest.mark.parametrize("forecast", [True, float("nan"), float("inf"), -101])
def test_invalid_numeric_predictions_are_not_treated_as_neutral(forecast):
    with pytest.raises(replay.ModelComparisonError, match="预期收益率"):
        compare(market(), contestant(observation(0, expected_return_pct=forecast, horizon_bars=1)))


def test_mae_and_directional_accuracy_use_common_matured_samples_only():
    first = contestant(
        observation(0, expected_return_pct=15, horizon_bars=1),
        observation(1, expected_return_pct=90, horizon_bars=1),
        run_id="first",
    )
    second = contestant(
        observation(0, expected_return_pct=5, horizon_bars=1),
        observation(4, expected_return_pct=100, horizon_bars=1),
        run_id="second", model_kind="quant",
    )
    models = compare(market(), first, second)["models"]
    for model in models:
        assert model["forecast"]["mae_pct"] == pytest.approx(5)
        assert model["forecast"]["rmse_pct"] == pytest.approx(5)
        assert model["forecast"]["n"] == model["forecast"]["matched_samples"] == 1
        assert model["forecast"]["directional_accuracy"] == 1
    assert [model["forecast"]["own_n"] for model in models] == [2, 1]


@pytest.mark.parametrize("horizon", [None, 2])
def test_missing_or_mismatched_horizon_is_not_a_same_period_forecast_score(horizon):
    first = contestant(observation(0, expected_return_pct=10, horizon_bars=1), run_id="first")
    second = contestant(observation(0, expected_return_pct=10, horizon_bars=horizon), run_id="second")
    for model in compare(market(), first, second)["models"]:
        assert model["forecast"]["mae_pct"] is None
        assert model["forecast"]["n"] == 0
        assert "没有共同的同周期数值预测样本" in model["warnings"]


@pytest.mark.parametrize("basis", ["excess", "market_excess", "target_weight", ""])
def test_excess_or_weight_direction_does_not_claim_asset_direction_accuracy(basis):
    model = compare(market(), contestant(observation(0, direction_basis=basis)))["models"][0]
    assert model["metrics"]["total_return"] == pytest.approx(.1)
    assert model["forecast"]["directional_accuracy"] is None
    assert model["forecast"]["directional_n"] == 0


def test_explicit_asset_direction_can_be_scored_without_inventing_a_numeric_forecast():
    model = compare(market(), contestant(observation(0, direction_basis="asset_return")))["models"][0]
    assert model["forecast"]["directional_accuracy"] == 1
    assert model["forecast"]["mae_pct"] is None


def test_short_high_return_never_overflows_annualization_or_claims_a_sharpe():
    model = compare(market((1, 100, 100)), contestant(observation(0)))["models"][0]
    assert model["metrics"]["total_return"] == 99
    for key in ("annualized_return", "annualized_volatility", "sharpe_ratio", "calmar_ratio"):
        assert model["metrics"][key] is None


def test_long_series_uses_full_resolution_metrics_with_bounded_genuine_chart_data(monkeypatch):
    closes = [100 + index / 100 for index in range(6001)]
    closes[3456] = 50  # Intraperiod loss must survive the metric calculation.
    data = market(closes, frequency="1m")
    candidate = contestant(observation(0))
    original = deepcopy(candidate)
    calls = []
    original_run = replay.run_backtest

    def counted_run(dataset, signals, **kwargs):
        calls.append(len(signals))
        return original_run(dataset, signals, **kwargs)

    monkeypatch.setattr(replay, "run_backtest", counted_run)
    payload = compare(data, candidate, holding_bars=10000)
    model = payload["models"][0]
    assert calls == [6000]  # One linear signal series and one execution pass.
    assert candidate == original
    assert len(model["curve"]) <= 2000
    assert len(payload["benchmark_curve"]) <= 2000
    assert model["curve"][0]["timestamp"] == data.bars[0].timestamp
    assert model["curve"][-1]["timestamp"] == data.bars[-1].timestamp
    assert model["metrics"]["bar_count"] == 6001
    assert model["metrics"]["max_drawdown"] == pytest.approx(1 - 50 / closes[3455])
    assert any(point["timestamp"] == data.bars[3456].timestamp for point in model["curve"])
    assert len(payload["bars"]) == 3000
    assert [bar["timestamp"] for bar in payload["bars"]] == [bar.timestamp for bar in data.bars[-3000:]]
    assert [bar["open"] for bar in payload["bars"]] == [bar.open for bar in data.bars[-3000:]]
    assert payload["market"]["bar_count"] == 6001


@pytest.mark.parametrize("rule", [{"holding_bars": 0}, {"holding_bars": True},
                                  {"fee_bps": -1}, {"initial_capital": 0},
                                  {"position_rule": "long_short"}])
def test_invalid_execution_rules_are_rejected(rule):
    with pytest.raises(replay.ModelComparisonError):
        compare(market(), contestant(observation(0)), **rule)
