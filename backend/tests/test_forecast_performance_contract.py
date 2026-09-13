"""Forecast values survive public performance without fake trading P&L."""
import json
from pathlib import Path

import pytest

from app.event_backtest.performance import build_portfolio_performance
from app.quant_backtest.engine import run_backtest
from app.quant_backtest.models import Bar, MarketDataset
from app.quant_backtest.strategies import strategy_from_spec


def forecast_run(tmp_path, closes=None):
    closes = closes or [100, 110, 121, 115]
    bars = tuple(Bar(f'2025-01-{i+1:02d}', close, close, close, close) for i, close in enumerate(closes))
    data = MarketDataset(name='performance-test', symbol='TEST', market='CN', frequency='1d', bars=bars)
    spec = {'kind': 'return_forecast', 'parameters': {'lookback': 1, 'horizon_bars': 1}}
    strategy = strategy_from_spec(spec)
    result = run_backtest(data, strategy.generate(data), strategy=strategy.describe())
    path = tmp_path / 'result.json'
    path.write_text(json.dumps(result.to_dict()))
    return {'id': 'forecast', 'result_path': str(path), 'engine_mode': 'portfolio', 'execution_spec': {}, 'visibility': 'private'}


def test_forecast_view_contains_predictions_without_trading_performance(tmp_path):
    payload = build_portfolio_performance(forecast_run(tmp_path))
    assert payload['prediction_only'] is True
    assert payload['return_forecast']['evaluated_count'] > 0
    assert 'direction_accuracy' in payload['return_forecast']
    assert 'pearson_ic' in payload['return_forecast']
    assert 'spearman_rank_ic' in payload['return_forecast']
    assert 'r_squared' in payload['return_forecast']
    assert 'skill_score_vs_zero' in payload['return_forecast']
    assert payload['return_forecasts'][0]['expected_return_pct'] != 0
    assert payload['return_forecasts'][0]['actual_return_pct'] is not None
    assert payload['summary'].get('total_return') is None
    assert payload['initial_value'] is None
    assert payload['equity_curve'] == payload['positions'] == payload['trades'] == []
    assert payload['gross_equity_curve'] == payload['drawdown_curve'] == payload['orders'] == []


def test_forecast_view_preserves_frozen_asset_return_and_ohlc(tmp_path):
    run = forecast_run(tmp_path)
    result_path = Path(run['result_path'])
    original = result_path.read_bytes()
    frozen = json.loads(original)

    payload = build_portfolio_performance(run)

    # This is the asset's observed close-to-close change, not the forecaster's
    # zero-position simulator equity. Reading the view must not rewrite history.
    assert payload['bars'] == frozen['bars']
    closes = [bar['close'] for bar in frozen['bars']]
    assert [point['timestamp'] for point in payload['asset_curve']] == [
        bar['timestamp'] for bar in frozen['bars']
    ]
    assert [point['net_value'] for point in payload['asset_curve']] == pytest.approx(
        [close / closes[0] for close in closes]
    )
    assert payload['benchmark_curve'] == payload['asset_curve']
    assert payload['summary'] == {
        'bar_count': 4,
        'benchmark_total_return': pytest.approx(.15),
    }
    assert payload['equity_curve'] == payload['positions'] == payload['trades'] == []
    assert result_path.read_bytes() == original


def test_arena_safe_hides_forecast_rows_but_keeps_metrics(tmp_path, monkeypatch):
    from app.routes import backtest
    run = forecast_run(tmp_path, [100, 102, 101, 104, 106, 105, 108, 110])
    run['visibility'] = 'arena_safe'
    monkeypatch.setattr(backtest.db, 'get_bt_run', lambda run_id: run)
    payload = backtest.get_run_performance('forecast', include_kline=False, kline_limit=6)
    assert payload['return_forecasts'] == []
    assert payload['return_forecast']['mae_pct'] is not None
    assert 'median_abs_error_pct' in payload['return_forecast']
    assert 'direction_accuracy' in payload['return_forecast']
    assert payload['prediction_only'] is True
    assert payload['bars'] == payload['signals'] == []
    assert payload['summary']['benchmark_total_return'] == pytest.approx(.10)
    for key in ('asset_curve', 'benchmark_curve'):
        assert all(set(point) <= {'index', 'net_value', 'drawdown'} for point in payload[key])


def test_public_event_performance_attaches_explicit_forecasts(monkeypatch):
    from app.routes import backtest
    from app.event_backtest import performance, return_forecast_view
    run = {'id': 'event', 'engine_mode': 'event_proxy', 'visibility': 'private'}
    monkeypatch.setattr(backtest.db, 'get_bt_run', lambda run_id: run)
    monkeypatch.setattr(performance, 'build_unified_performance', lambda *args, **kwargs: {'summary': {}})
    rows = [{'event_id': 'e1', 'expected_return_pct': 5, 'actual_return_pct': 2, 'error_pct': 3}]
    monkeypatch.setattr(return_forecast_view, 'build_event_return_forecasts', lambda run: rows)
    assert backtest.get_run_performance('event', include_kline=False, kline_limit=6)['event_return_forecasts'] == rows
    run['visibility'] = 'arena_safe'
    assert backtest.get_run_performance('event', include_kline=False, kline_limit=6)['event_return_forecasts'] == []
