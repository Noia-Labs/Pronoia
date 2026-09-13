"""Numerical return prediction contracts and leakage checks, without network I/O."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest

from app.quant_backtest.engine import run_backtest
from app.quant_backtest.models import Bar, ContextRecord, ExecutionConfig, MarketDataset, QuantBacktestError, Signal
from app.quant_backtest.return_forecast import FORECAST_CONTRACT, HistoricalMeanReturnForecast
from app.quant_backtest.service import QuantBacktestService
from app.quant_backtest.store import QuantStore
from app.quant_backtest.strategies import (
    ExternalHTTPStrategy, ExternalReturnForecastStrategy, MomentumStrategy,
    load_signal_csv, strategy_from_spec,
)


def dataset(closes=(100.0, 110.0, 99.0, 108.9, 108.9)):
    start = date(2025, 1, 1)
    return MarketDataset(
        name="numerical-forecast-test", symbol="TEST", market="CN", frequency="1d",
        bars=tuple(Bar((start + timedelta(days=index)).isoformat(), close, close, close, close,
                       fields={"future_label": "MUST_NOT_SEND"}) for index, close in enumerate(closes)),
        metadata={"future_dataset_detail": "MUST_NOT_SEND"},
    )


def signal(data, index, forecast, horizon=1, weight=0):
    return Signal(data.bars[index].timestamp, weight, expected_return_pct=forecast, horizon_bars=horizon)


@pytest.mark.parametrize("forecast,horizon", [
    (1, None), (None, 1), (float("nan"), 1), (float("inf"), 1), (-100.01, 1),
    (1, 0), (1, -1), (1, 1.5), (1, True), (True, 1), (1, 10001), ("invalid", 1),
])
def test_reject_ambiguous_or_invalid_explicit_forecasts(forecast, horizon):
    with pytest.raises(QuantBacktestError):
        Signal("2025-01-01", 0, expected_return_pct=forecast, horizon_bars=horizon)


def test_numeric_errors_are_percentage_points_not_portfolio_returns():
    data = dataset()
    signals = [signal(data, 0, 5), signal(data, 1, -8), signal(data, 2, 10), signal(data, 4, 1)]
    result = run_backtest(data, signals)
    forecast = result.metrics["return_forecast"]
    assert forecast["evaluated_count"] == 3
    assert forecast["n_predictions"] == 5
    assert forecast["n_forecasts"] == 4
    assert forecast["pending_count"] == 1
    assert forecast["coverage"] == pytest.approx(4 / 5)
    assert forecast["evaluation_coverage"] == pytest.approx(3 / 5)
    assert forecast["eligible_evaluation_coverage"] == pytest.approx(3 / 4)
    assert forecast["mae_pct"] == pytest.approx(7 / 3)
    assert forecast["rmse_pct"] == pytest.approx(math.sqrt(29 / 3))
    assert forecast["bias_pct"] == pytest.approx(-1)
    assert forecast["median_abs_error_pct"] == pytest.approx(2)
    assert forecast["p90_abs_error_pct"] == pytest.approx(4.4)
    assert forecast["direction_accuracy"] == 1
    assert forecast["direction_evaluated_count"] == 3
    assert forecast["pearson_ic"] is not None
    assert forecast["spearman_rank_ic"] is not None
    assert forecast["r_squared"] == pytest.approx(1 - (29 / 3) / (800 / 9))
    assert forecast["skill_score_vs_zero"] == pytest.approx(1 - 29 / 300)
    assert forecast["unit"] == "percentage_points"
    assert result.return_forecasts[0]["actual_return_pct"] == pytest.approx(10)
    assert result.return_forecasts[0]["error_pct"] == pytest.approx(-5)
    assert result.return_forecasts[-1]["status"] == "pending"
    assert result.return_forecasts[-1]["actual_return_pct"] is None
    assert result.metrics["total_return"] == 0  # zero compatibility weights, not forecast performance
    assert result.to_dict()["return_forecasts"] == list(result.return_forecasts)


def test_missing_prediction_is_not_inferred_from_score_or_position():
    data = dataset()
    result = run_backtest(data, MomentumStrategy(lookback=1).generate(data))
    assert any(row["score"] is not None for row in result.signals)
    assert result.return_forecasts == ()
    summary = result.metrics["return_forecast"]
    assert summary["status"] == "not_provided"
    assert summary["evaluated_count"] == 0
    assert summary["mae_pct"] is summary["rmse_pct"] is summary["bias_pct"] is None


def test_horizon_is_bar_count_with_distinct_per_horizon_metrics():
    data = dataset()
    result = run_backtest(data, [signal(data, 0, 0, 2), signal(data, 1, -10, 1), signal(data, 4, 0, 2)])
    rows = result.return_forecasts
    assert rows[0]["target_timestamp"] == data.bars[2].timestamp
    assert rows[0]["actual_return_pct"] == pytest.approx(-1)
    by_horizon = result.metrics["return_forecast"]["by_horizon"]
    assert [item["horizon_bars"] for item in by_horizon] == [1, 2]
    assert by_horizon[0]["mae_pct"] == pytest.approx(0)
    assert by_horizon[1]["mae_pct"] == pytest.approx(1)


def test_history_mean_uses_only_fully_realized_horizon_returns():
    data = dataset((100, 110, 132, 145.2, 200, 80))
    model = HistoricalMeanReturnForecast(lookback=2, horizon_bars=2)
    signals = model.generate(data)
    assert all(item.expected_return_pct is None for item in signals[:3])
    assert signals[3].expected_return_pct == pytest.approx(32)
    assert signals[3].metadata["information_cutoff"] == data.bars[3].timestamp
    assert all(item.target_weight == 0 for item in signals)
    altered_future = model.generate(dataset((100, 110, 132, 145.2, 1_000_000, 1)))
    assert signals[:4] == altered_future[:4]
    assert signals[4].expected_return_pct != altered_future[4].expected_return_pct
    result = run_backtest(data, signals, strategy=model.describe())
    assert result.metrics["prediction_only"] is True
    assert result.metrics["return_forecast"]["n_forecasts"] == 3
    assert result.metrics["return_forecast"]["evaluated_count"] == 1
    assert result.metrics["return_forecast"]["pending_count"] == 2
    assert result.trades == ()


def test_insufficient_warmup_has_no_fabricated_predictions():
    data = dataset()
    model = HistoricalMeanReturnForecast(lookback=20, horizon_bars=3)
    result = run_backtest(data, model.generate(data), strategy=model.describe())
    assert result.metrics["return_forecast"]["status"] == "not_provided"
    assert result.metrics["return_forecast"]["horizon_bars"] == 3
    assert result.metrics["return_forecast"]["mae_pct"] is None


@pytest.mark.parametrize("params", [{"horizon_bars": 1.5}, {"lookback": 0}, {"method": "guess_from_position"}])
def test_forecast_strategy_parameters_fail_explicitly(params):
    with pytest.raises(QuantBacktestError):
        strategy_from_spec({"kind": "return_forecast", "parameters": params})


def test_csv_forecast_only_contract_and_pure_import_preserves_numbers(tmp_path):
    path = tmp_path / "predictions.csv"
    path.write_text("timestamp,expected_return_pct,horizon_bars\n2025-01-01,2.5,3\n2025-01-02,-1.5,3\n", encoding="utf8")
    signals = load_signal_csv(path)
    assert [item.expected_return_pct for item in signals] == [2.5, -1.5]
    assert [item.horizon_bars for item in signals] == [3, 3]
    assert all(item.target_weight == 0 for item in signals)
    model = strategy_from_spec({"kind": "return_forecast", "source": "signal_file", "path": str(path), "parameters": {"horizon_bars": 3}})
    assert model.describe()["prediction_only"] is True
    assert model.describe()["point_in_time_enforced"] is False
    assert model.generate(dataset()) == signals


def test_legacy_csv_and_explicit_forecasts_do_not_change_weights(tmp_path):
    path = tmp_path / "legacy.csv"
    path.write_text("timestamp,target_weight,score\n2025-01-01,0.7,2.5\n", encoding="utf8")
    old = load_signal_csv(path)[0]
    assert old.target_weight == .7 and old.expected_return_pct is None
    path.write_text("timestamp,target_weight,expected_return_pct,horizon_bars\n2025-01-01,0.7,2.5,3\n", encoding="utf8")
    new = load_signal_csv(path)[0]
    assert new.target_weight == .7 and new.expected_return_pct == 2.5
    imported = strategy_from_spec({"kind": "return_forecast", "source": "signal_file", "path": str(path), "parameters": {"horizon_bars": 3}})
    assert imported.generate(dataset())[0].target_weight == 0


def test_csv_rejects_missing_forecast_contract_and_mismatched_horizon(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("timestamp,expected_return_pct\n2025-01-01,2.5\n", encoding="utf8")
    with pytest.raises(QuantBacktestError, match="同时包含"):
        load_signal_csv(path)
    path.write_text("timestamp,expected_return_pct,horizon_bars\n2025-01-01,2.5,2\n", encoding="utf8")
    with pytest.raises(QuantBacktestError, match="周期一致"):
        strategy_from_spec({"kind": "return_forecast", "source": "signal_file", "path": str(path), "parameters": {"horizon_bars": 3}})


def mock_http(monkeypatch, response_factory):
    import app.quant_backtest.strategies as module

    requests = []
    validations = []

    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return json.dumps(self.data).encode()[:limit]

    class Opener:
        def open(self, request, timeout):
            payload = json.loads(request.data)
            requests.append(payload)
            return Response(response_factory(payload))

    monkeypatch.setattr(module, "_validate_runtime_http_endpoint", lambda endpoint: validations.append(endpoint))
    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    return requests, validations


def test_http_each_request_is_point_in_time_and_never_contains_future_labels(monkeypatch):
    data = dataset()
    requests, validations = mock_http(monkeypatch, lambda payload: {"expected_return_pct": 2.5, "horizon_bars": payload["horizon_bars"]})
    context = [
        ContextRecord("macro", "2025-01-02", {"value": 2}),
        ContextRecord("macro", "2025-01-05", {"value": 5}),
    ]
    model = strategy_from_spec({"kind": "return_forecast", "source": "external_http", "endpoint": "https://forecast.example/predict", "parameters": {"horizon_bars": 2, "lookback": 3}})
    signals = model.generate(data, context=context)
    assert len(requests) == len(validations) == len(data.bars)
    assert all(item.expected_return_pct == 2.5 and item.horizon_bars == 2 and item.target_weight == 0 for item in signals)
    for index, payload in enumerate(requests):
        assert payload["schema_version"] == FORECAST_CONTRACT
        assert payload["as_of"] == data.bars[index].timestamp
        assert len(payload["bars"]) == min(index + 1, 3)
        assert payload["bars"][-1]["timestamp"] == payload["as_of"]
        assert all(row["timestamp"] <= payload["as_of"] for row in payload["bars"])
        assert "MUST_NOT_SEND" not in json.dumps(payload)
        assert "dataset" not in payload
    assert requests[0]["context"]["series"] == {}
    assert requests[1]["context"]["series"]["macro"]["value"] == 2
    assert requests[3]["context"]["series"]["macro"]["value"] == 2
    assert requests[4]["context"]["series"]["macro"]["value"] == 5
    assert model.describe()["point_in_time_enforced"] is True


@pytest.mark.parametrize("response", [
    {"target_weight": .7, "score": 2.5},
    {"expected_return_pct": 2.5},
    {"expected_return_pct": 2.5, "horizon_bars": 2},
    {"expected_return_pct": 2.5, "horizon_bars": 3, "timestamp": "2025-01-05"},
    {"expected_return_pct": "up", "horizon_bars": 3},
])
def test_http_rejects_missing_or_ambiguous_numerical_contract(monkeypatch, response):
    mock_http(monkeypatch, lambda _: response)
    with pytest.raises(QuantBacktestError):
        ExternalReturnForecastStrategy("https://forecast.example/predict").generate(dataset())


def test_http_explicit_null_is_abstention_and_not_zero_prediction(monkeypatch):
    mock_http(monkeypatch, lambda _: {"expected_return_pct": None, "horizon_bars": 3})
    model = ExternalReturnForecastStrategy("https://forecast.example/predict")
    data = dataset()
    result = run_backtest(data, model.generate(data), strategy=model.describe())
    assert result.metrics["return_forecast"]["n_forecasts"] == 0
    assert result.metrics["return_forecast"]["mae_pct"] is None


def test_old_http_can_read_explicit_forecasts_but_stays_unverified(monkeypatch):
    mock_http(monkeypatch, lambda _: {"signals": [{"timestamp": "2025-01-01", "target_weight": .7, "expected_return_pct": 2.5, "horizon_bars": 3}]})
    model = ExternalHTTPStrategy("https://forecast.example/predict")
    signals = model.generate(dataset())
    assert signals[0].target_weight == .7
    assert signals[0].expected_return_pct == 2.5
    assert model.describe()["point_in_time_enforced"] is False


def test_service_persists_forecast_rows_and_metrics_separately_from_equity(tmp_path):
    store = QuantStore(tmp_path / "quant.sqlite")
    service = QuantBacktestService(store=store)
    model = HistoricalMeanReturnForecast(lookback=2, horizon_bars=1)
    run = service.execute_loaded(name="收益数值评估", dataset=dataset(), strategy=model, execution=ExecutionConfig())
    assert run["status"] == "done"
    loaded = store.get_run(run["id"], include_result=True)
    assert loaded["result"]["return_forecasts"]
    assert loaded["result"]["metrics"]["prediction_only"] is True
    assert loaded["result"]["metrics"]["return_forecast"]["evaluated_count"] == 2
    assert loaded["result"]["trades"] == []
    assert store.list_runs()[0]["metrics"]["return_forecast"]["evaluated_count"] == 2


@pytest.mark.parametrize("mixed", [False, True])
def test_legacy_portfolio_comparison_rejects_pure_forecasts(tmp_path, mixed):
    service = QuantBacktestService(store=QuantStore(tmp_path / "comparison.sqlite"))
    pure = service.execute_loaded(name="数值预测", dataset=dataset(), strategy=HistoricalMeanReturnForecast(lookback=2, horizon_bars=1))
    other = service.execute_loaded(name="另一模型", dataset=dataset(), strategy=(
        MomentumStrategy(lookback=1) if mixed else HistoricalMeanReturnForecast(lookback=1, horizon_bars=1)
    ))
    with pytest.raises(QuantBacktestError, match="纯收益率预测不能进入组合总收益排名"):
        service.compare([pure["id"], other["id"]])


def test_legacy_portfolio_comparison_remains_available_for_trading_strategies(tmp_path):
    service = QuantBacktestService(store=QuantStore(tmp_path / "legacy-comparison.sqlite"))
    runs = [service.execute_loaded(name=f"动量{lookback}", dataset=dataset(), strategy=MomentumStrategy(lookback=lookback))
            for lookback in (1, 2)]
    comparison = service.compare([run["id"] for run in runs])
    assert comparison["strict_comparable"] is True
    assert len(comparison["rankings"]["total_return"]) == 2
