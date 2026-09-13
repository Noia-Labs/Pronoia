"""Detailed portfolio finance statistics use the full frozen result."""
from __future__ import annotations

import json
import math

from app.event_backtest import performance


def _frozen_result() -> dict:
    timestamps = [f"2025-01-0{index + 1}T15:00:00+08:00" for index in range(6)]
    net_values = [1.0, 1.10, 0.99, 1.10, 1.045, 1.15]
    benchmark_values = [1.0, 1.05, 1.0, 1.10, 1.0, 1.20]
    positions = [0.0, 1.0, 1.0, 0.0, -0.5, 0.5]
    turnovers = [0.0, 1.0, 0.0, 1.0, 0.5, 1.0]
    curve = []
    peak = 1.0
    for index, net_value in enumerate(net_values):
        peak = max(peak, net_value)
        curve.append({
            "timestamp": timestamps[index],
            "equity": 100_000 * net_value,
            "net_value": net_value,
            "benchmark_net_value": benchmark_values[index],
            "drawdown": net_value / peak - 1.0,
            "position": positions[index],
            "turnover": turnovers[index],
            "net_return": 0.0 if index == 0 else net_value / net_values[index - 1] - 1.0,
        })
    trades = [
        {"execution_timestamp": timestamps[1], "side": "buy", "from_weight": 0.0,
         "to_weight": 1.0, "turnover": 1.0, "notional": 100_000.0, "total_cost": 2.0},
        {"execution_timestamp": timestamps[3], "side": "sell", "from_weight": 1.0,
         "to_weight": 0.0, "turnover": 1.0, "notional": 110_000.0, "total_cost": 3.0},
        {"execution_timestamp": timestamps[4], "side": "sell", "from_weight": 0.0,
         "to_weight": -0.5, "turnover": 0.5, "notional": 52_250.0, "total_cost": 2.0},
        {"execution_timestamp": timestamps[5], "side": "buy", "from_weight": -0.5,
         "to_weight": 0.5, "turnover": 1.0, "notional": 115_000.0, "total_cost": 3.0},
    ]
    return {
        "contract": {"dataset_version": "dsv_test"},
        "dataset": {
            "name": "测试指数", "symbol": "000300.SH", "market": "CN", "frequency": "1d",
            "start_at": timestamps[0], "end_at": timestamps[-1], "source_type": "test",
        },
        "strategy": {"kind": "test"},
        "metrics": {
            "initial_capital": 100_000.0,
            "final_equity": 115_000.0,
            "total_return": 0.15,
            "annualized_return": 0.2,
            "annualized_volatility": 0.3,
            "sharpe_ratio": 0.7,
            "max_drawdown": 0.1,
            "calmar_ratio": 2.0,
            "win_rate": 0.6,
            "profit_factor": 1.5,
            "benchmark_total_return": 0.2,
            "excess_total_return": -0.05,
            "trade_count": 4,
            "total_turnover": 3.5,
            "total_commission": 4.0,
            "total_slippage": 3.0,
            "total_stamp_duty": 2.0,
            "total_other_cost": 1.0,
            "total_cost": 10.0,
            "cost_drag_ratio": 0.0001,
            "bar_count": 6,
            "active_period_count": 5,
            "annualization_periods": 252,
            "prediction_only": False,
        },
        "bars": [{"timestamp": value} for value in timestamps],
        "signals": [],
        "equity_curve": curve,
        "drawdown_curve": [
            {"timestamp": item["timestamp"], "drawdown": item["drawdown"]} for item in curve
        ],
        "trades": trades,
        "warnings": [],
    }


def _run(tmp_path, payload: dict) -> dict:
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "id": "finance", "engine_mode": "portfolio", "result_path": str(result_path),
        "dataset_version": "dsv_test",
        "execution_spec": {"requested": {"currency": "CNY"}, "applied": {"currency": "CNY"}},
    }


def _assert_no_lists(value):
    assert not isinstance(value, list)
    if isinstance(value, dict):
        for child in value.values():
            _assert_no_lists(child)


def _analysis_from_returns(returns: list[float]) -> dict:
    net_value = 1.0
    curve = [{
        "timestamp": "t0", "net_value": net_value, "benchmark_net_value": 1.0,
        "position": 0.0, "turnover": 0.0, "net_return": 0.0,
    }]
    for index, value in enumerate(returns, start=1):
        net_value *= 1.0 + value
        curve.append({
            "timestamp": f"t{index}", "net_value": net_value,
            "benchmark_net_value": 1.0, "position": 1.0,
            "turnover": 1.0 if index == 1 else 0.0, "net_return": value,
        })
    return performance._portfolio_financial_analysis(
        metrics={
            "annualization_periods": 252, "initial_capital": 1.0,
            "final_equity": net_value, "total_return": net_value - 1.0,
            "prediction_only": False,
        },
        dataset={"frequency": "1d"},
        curve=curve,
        trades=[],
    )


def test_financial_analysis_has_auditable_return_risk_trade_and_exposure_metrics(tmp_path):
    result = performance.build_portfolio_performance(_run(tmp_path, _frozen_result()))
    analysis = result["financial_analysis"]

    assert analysis["status"] == "available"
    assert analysis["metrics_basis"] == "full_frozen_result"
    assert analysis["units"]["money"] == "CNY"
    assert analysis["period"] == {
        "start_at": "2025-01-01T15:00:00+08:00",
        "end_at": "2025-01-06T15:00:00+08:00",
        "frequency": "1d",
        "bar_count": 6,
        "return_observation_count": 5,
        "annualization_periods": 252,
        "estimated_years": 5 / 252,
    }
    assert analysis["returns"]["total_return"] == 0.15
    assert analysis["returns"]["best_bar_return"] > 0.11
    assert math.isclose(analysis["returns"]["worst_bar_return"], -0.1)
    assert analysis["risk"]["historical_var_95_one_bar"] > 0
    assert analysis["risk"]["historical_cvar_95_one_bar"] >= analysis["risk"]["historical_var_95_one_bar"]
    assert math.isfinite(analysis["risk"]["sortino_ratio"])
    assert analysis["risk"]["peak_timestamp"] == "2025-01-02T15:00:00+08:00"
    assert analysis["risk"]["trough_timestamp"] == "2025-01-03T15:00:00+08:00"
    assert analysis["risk"]["recovery_timestamp"] == "2025-01-04T15:00:00+08:00"
    assert analysis["risk"]["peak_to_trough_bars"] == 1
    assert analysis["risk"]["recovery_bars"] == 1
    assert analysis["risk"]["reported_max_drawdown"] == 0.1
    assert analysis["risk"]["matches_reported_max_drawdown"] is True

    benchmark = analysis["benchmark"]
    assert benchmark["aligned_return_count"] == 5
    assert benchmark["aligned_start_at"] == "2025-01-01T15:00:00+08:00"
    assert benchmark["aligned_end_at"] == "2025-01-06T15:00:00+08:00"
    assert -1 <= benchmark["correlation"] <= 1
    assert all(math.isfinite(benchmark[key]) for key in (
        "beta", "annualized_alpha_rf0", "annualized_tracking_error", "information_ratio",
    ))

    assert analysis["trading"]["position_change_count"] == 4
    assert analysis["trading"]["buy_count"] == 2
    assert analysis["trading"]["sell_count"] == 2
    assert analysis["trading"]["entry_count"] == 2
    assert analysis["trading"]["exit_count"] == 1
    assert analysis["trading"]["reversal_count"] == 1
    assert analysis["trading"]["total_traded_notional"] == 377_250.0
    assert analysis["trading"]["notional_observation_count"] == 4
    assert analysis["trading"]["notional_coverage_ratio"] == 1.0
    assert analysis["trading"]["notional_coverage_complete"] is True
    assert analysis["exposure"]["observation_count"] == 5
    assert analysis["exposure"]["time_in_market_ratio"] == 4 / 5
    assert analysis["exposure"]["long_exposure_ratio"] == 3 / 5
    assert analysis["exposure"]["short_exposure_ratio"] == 1 / 5
    assert analysis["exposure"]["flat_ratio"] == 1 / 5
    assert analysis["costs"]["total_cost"] == 10.0
    assert analysis["costs"]["total_cost_basis"] == "reported_metric"
    assert analysis["costs"]["trade_cost_observation_count"] == 4
    assert analysis["costs"]["trade_cost_coverage_ratio"] == 1.0
    assert analysis["costs"]["trade_cost_coverage_complete"] is True
    assert analysis["costs"]["cost_to_traded_notional_ratio"] == 10 / 377_250
    assert analysis["methodology"]["risk_free_rate"] == 0.0
    assert "one-bar" in analysis["methodology"]["var_basis"]
    _assert_no_lists(analysis)


def test_cvar_uses_exact_tail_mass_when_quantile_boundary_has_ties():
    one_loss = _analysis_from_returns([-0.10] + [0.0] * 99)
    four_losses = _analysis_from_returns([-0.10] * 4 + [0.0] * 96)

    assert one_loss["risk"]["historical_var_95_one_bar"] == 0.0
    assert math.isclose(one_loss["risk"]["historical_cvar_95_one_bar"], 0.02)
    assert four_losses["risk"]["historical_var_95_one_bar"] == 0.0
    assert math.isclose(four_losses["risk"]["historical_cvar_95_one_bar"], 0.08)


def test_exposure_statistics_exclude_initial_no_return_snapshot():
    analysis = _analysis_from_returns([0.01, 0.02])

    assert analysis["period"]["return_observation_count"] == 2
    assert analysis["exposure"]["observation_count"] == 2
    assert analysis["exposure"]["time_in_market_ratio"] == 1.0
    assert analysis["exposure"]["long_exposure_ratio"] == 1.0
    assert analysis["exposure"]["flat_ratio"] == 0.0


def test_drawdown_episode_uses_latest_near_equal_high_water_mark():
    curve = [
        {"timestamp": "peak-old", "net_value": 1.0},
        {"timestamp": "dip-old", "net_value": 0.9},
        # A reconstructed curve can revisit its high with sub-picounit drift.
        {"timestamp": "peak-latest", "net_value": 1.0 - 5e-13},
        {"timestamp": "trough", "net_value": 0.8},
        {"timestamp": "recovered", "net_value": 1.0},
    ]

    analysis = performance._portfolio_drawdown_analysis(curve)

    assert analysis["peak_timestamp"] == "peak-latest"
    assert analysis["trough_timestamp"] == "trough"
    assert analysis["recovery_timestamp"] == "recovered"
    assert analysis["peak_to_trough_bars"] == 1
    assert analysis["recovery_bars"] == 1
    assert analysis["underwater_bars"] == 2
    assert analysis["longest_underwater_bars"] == 1
    assert math.isclose(analysis["max_drawdown"], 0.2)


def test_zero_drawdown_does_not_fabricate_peak_trough_or_recovery_episode():
    for net_values in ([1.0, 1.0, 1.0], [1.0, 1.1, 1.2]):
        curve = [
            {"timestamp": f"t{index}", "net_value": net_value}
            for index, net_value in enumerate(net_values)
        ]

        analysis = performance._portfolio_drawdown_analysis(curve)

        assert analysis["max_drawdown"] == 0.0
        assert analysis["peak_timestamp"] is None
        assert analysis["trough_timestamp"] is None
        assert analysis["recovery_timestamp"] is None
        assert analysis["peak_to_trough_bars"] is None
        assert analysis["recovery_bars"] is None
        assert analysis["underwater_bars"] == 0
        assert analysis["longest_underwater_bars"] == 0
        assert analysis["recovered"] is None


def test_prediction_only_does_not_fabricate_portfolio_financial_metrics(tmp_path):
    frozen = _frozen_result()
    frozen["metrics"]["prediction_only"] = True
    result = performance.build_portfolio_performance(_run(tmp_path, frozen))

    assert result["financial_analysis"]["status"] == "not_applicable"
    assert result["financial_analysis"]["metrics_basis"] == "full_frozen_result"
    assert result["equity_curve"] == []
    assert result["trades"] == []
    assert "risk" not in result["financial_analysis"]


def test_prediction_only_uses_strict_booleans_and_legacy_strategy_fallback(tmp_path):
    ordinary = _frozen_result()
    ordinary["metrics"]["prediction_only"] = "false"
    ordinary["strategy"] = {"kind": "declarative_rules", "prediction_only": "false"}

    ordinary_result = performance.build_portfolio_performance(_run(tmp_path, ordinary))

    assert ordinary_result["prediction_only"] is False
    assert ordinary_result["financial_analysis"]["status"] == "available"
    assert ordinary_result["equity_curve"]

    for legacy_strategy in (
        {"kind": "return_forecast"},
        {"kind": "legacy_forecaster", "prediction_only": True},
    ):
        legacy = _frozen_result()
        legacy["metrics"].pop("prediction_only")
        legacy["strategy"] = legacy_strategy

        legacy_result = performance.build_portfolio_performance(_run(tmp_path, legacy))

        assert legacy_result["prediction_only"] is True
        assert legacy_result["financial_analysis"]["status"] == "not_applicable"
        assert legacy_result["equity_curve"] == []
        assert legacy_result["trades"] == []


def test_incomplete_trade_fields_report_coverage_without_partial_totals(tmp_path):
    frozen = _frozen_result()
    frozen["trades"][0].pop("notional")
    frozen["trades"][1].pop("total_cost")

    analysis = performance.build_portfolio_performance(_run(tmp_path, frozen))["financial_analysis"]
    trading = analysis["trading"]
    costs = analysis["costs"]

    assert trading["notional_observation_count"] == 3
    assert trading["notional_coverage_ratio"] == 3 / 4
    assert trading["notional_coverage_complete"] is False
    assert trading["covered_traded_notional"] == 277_250.0
    assert trading["total_traded_notional"] is None
    assert trading["average_traded_notional"] is None
    assert trading["maximum_traded_notional"] is None
    assert costs["total_cost"] == 10.0
    assert costs["total_cost_basis"] == "reported_metric"
    assert costs["trade_cost_observation_count"] == 3
    assert costs["trade_cost_coverage_ratio"] == 3 / 4
    assert costs["trade_cost_coverage_complete"] is False
    assert costs["covered_trade_cost"] == 7.0
    assert costs["cost_to_traded_notional_ratio"] is None
    assert costs["average_cost_per_change"] is None
    assert costs["maximum_cost_per_change"] is None


def test_complete_trade_costs_can_supply_missing_legacy_total(tmp_path):
    frozen = _frozen_result()
    frozen["metrics"].pop("total_cost")

    costs = performance.build_portfolio_performance(_run(tmp_path, frozen))["financial_analysis"]["costs"]

    assert costs["total_cost"] == 10.0
    assert costs["total_cost_basis"] == "complete_trade_records"
    assert costs["cost_to_traded_notional_ratio"] == 10 / 377_250


def test_unified_event_proxy_contract_is_not_changed(monkeypatch):
    expected = {"mode": "event_proxy", "summary": {"total_return": 0.1}}
    monkeypatch.setattr(performance, "build_event_proxy_performance", lambda *args, **kwargs: expected)

    assert performance.build_unified_performance({"engine_mode": "event_proxy"}) is expected
    assert "financial_analysis" not in expected
