"""Deterministic next-open portfolio simulator and performance metrics."""

from __future__ import annotations

import math
import statistics
from typing import Any, Callable, Mapping, Sequence

from .models import BacktestResult, ExecutionConfig, MarketDataset, QuantBacktestError, Signal
from .return_forecast import evaluate_return_forecasts


def _annualization_periods(frequency: str) -> int:
    raw = frequency.strip().lower().replace("min", "m")
    if raw in {"1d", "d", "day", "daily"}:
        return 252
    if raw in {"1w", "w", "week", "weekly"}:
        return 52
    if raw in {"1mo", "month", "monthly"}:
        return 12
    if raw.endswith("m"):
        try:
            minutes = int(raw[:-1])
        except ValueError:
            return 252
        return max(1, round(252 * 240 / minutes))
    return 252


def _validate_signals(
    dataset: MarketDataset,
    signals: Sequence[Signal],
    execution: ExecutionConfig,
) -> dict[str, Signal]:
    valid_timestamps = {bar.timestamp for bar in dataset.bars}
    by_time: dict[str, Signal] = {}
    for signal in signals:
        if signal.timestamp not in valid_timestamps:
            raise QuantBacktestError(f"信号时间不在行情中: {signal.timestamp}")
        if signal.timestamp in by_time:
            raise QuantBacktestError(f"同一时间存在重复信号: {signal.timestamp}")
        if abs(signal.target_weight) > execution.max_abs_weight + 1e-12:
            raise QuantBacktestError(
                f"{signal.timestamp} 的目标仓位 {signal.target_weight:g} 超过上限 {execution.max_abs_weight:g}"
            )
        if not execution.allow_short and signal.target_weight < 0:
            raise QuantBacktestError(f"{signal.timestamp} 出现空头仓位，但当前执行协议禁止做空")
        by_time[signal.timestamp] = signal
    if not by_time:
        raise QuantBacktestError("策略没有生成任何信号")
    return by_time


def run_backtest(
    dataset: MarketDataset,
    signals: Sequence[Signal],
    *,
    execution: ExecutionConfig | None = None,
    strategy: Mapping[str, Any] | None = None,
    control_check: Callable[[], None] | None = None,
) -> BacktestResult:
    """Run a single-asset target-weight backtest.

    A signal stamped at bar ``t`` is generated using information available at
    that bar's close and is executed at bar ``t+1`` open. The carried position
    earns the close-to-next-open return; the new target earns the next bar's
    open-to-close return. Fee and slippage are charged on absolute turnover.
    """

    if control_check:
        control_check()
    execution = execution or ExecutionConfig()
    by_time = _validate_signals(dataset, signals, execution)
    forecast_metrics, forecast_rows = evaluate_return_forecasts(dataset, signals, strategy=strategy)
    prediction_only = bool((strategy or {}).get("prediction_only"))
    bars = dataset.bars
    initial = float(execution.initial_capital)
    equity = initial
    peak = initial
    position = 0.0
    latest_target = 0.0
    cost_is_configured = any(
        value > 0
        for value in (
            float(execution.commission_bps), float(execution.slippage_bps),
            float(execution.stamp_duty_bps), float(execution.other_cost_bps),
            float(execution.minimum_commission),
        )
    )
    curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    total_commission = 0.0
    total_slippage = 0.0
    total_stamp_duty = 0.0
    total_other_cost = 0.0
    total_turnover = 0.0

    first_signal = by_time.get(bars[0].timestamp)
    curve.append(
        {
            "timestamp": bars[0].timestamp,
            "equity": equity,
            "net_value": 1.0,
            "benchmark_net_value": 1.0,
            "drawdown": 0.0,
            "position": position,
            "signal_weight": first_signal.target_weight if first_signal else None,
            "gross_return": 0.0,
            "net_return": 0.0,
            "turnover": 0.0,
            "cost": 0.0,
        }
    )

    for index in range(1, len(bars)):
        if control_check:
            control_check()
        previous, bar = bars[index - 1], bars[index]
        equity_previous_close = equity

        # Existing position remains live until today's open.
        overnight_return = position * (bar.open / previous.close - 1.0)
        equity_at_open = equity_previous_close * (1.0 + overnight_return)
        if equity_at_open <= 0 or not math.isfinite(equity_at_open):
            raise QuantBacktestError(f"{bar.timestamp} 开盘前权益耗尽")

        previous_signal = by_time.get(previous.timestamp)
        if previous_signal is not None:
            latest_target = previous_signal.target_weight
        turnover = abs(latest_target - position)
        traded_notional = equity_at_open * turnover
        direction = "buy" if latest_target > position else "sell"
        proportional_commission = traded_notional * float(execution.commission_bps) / 10_000.0
        commission = (
            max(proportional_commission, float(execution.minimum_commission))
            if turnover > 1e-12 else 0.0
        )
        slippage = traded_notional * float(execution.slippage_bps) / 10_000.0
        stamp_duty = (
            traded_notional * float(execution.stamp_duty_bps) / 10_000.0
            if direction == "sell" and turnover > 1e-12 else 0.0
        )
        other_cost = traded_notional * float(execution.other_cost_bps) / 10_000.0
        cost = commission + slippage + stamp_duty + other_cost
        if turnover > 1e-12:
            effective_price = bar.open * (
                1.0 + float(execution.slippage_bps) / 10_000.0
                if direction == "buy"
                else 1.0 - float(execution.slippage_bps) / 10_000.0
            )
            trades.append(
                {
                    "signal_timestamp": previous.timestamp,
                    "execution_timestamp": bar.timestamp,
                    "side": direction,
                    "from_weight": position,
                    "to_weight": latest_target,
                    "turnover": turnover,
                    "market_price": bar.open,
                    "effective_price": effective_price,
                    "notional": traded_notional,
                    "commission": commission,
                    "proportional_commission": proportional_commission,
                    "minimum_commission_applied": commission > proportional_commission + 1e-12,
                    "slippage": slippage,
                    "stamp_duty": stamp_duty,
                    "other_cost": other_cost,
                    "total_cost": cost,
                }
            )
        total_turnover += turnover
        total_commission += commission
        total_slippage += slippage
        total_stamp_duty += stamp_duty
        total_other_cost += other_cost
        position = latest_target

        intraday_return = position * (bar.close / bar.open - 1.0)
        gross_equity = equity_at_open * (1.0 + intraday_return)
        # Cost is paid at the open and therefore does not earn the intraday return.
        equity = (equity_at_open - cost) * (1.0 + intraday_return)
        if equity <= 0 or not math.isfinite(equity):
            raise QuantBacktestError(f"{bar.timestamp} 收盘权益耗尽")
        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        current_signal = by_time.get(bar.timestamp)
        curve.append(
            {
                "timestamp": bar.timestamp,
                "equity": equity,
                "net_value": equity / initial,
                "benchmark_net_value": bar.close / bars[0].close,
                "drawdown": drawdown,
                "position": position,
                "signal_weight": current_signal.target_weight if current_signal else None,
                "gross_return": gross_equity / equity_previous_close - 1.0,
                "net_return": equity / equity_previous_close - 1.0,
                "turnover": turnover,
                "cost": cost,
            }
        )

    if control_check:
        control_check()
    annual_periods = execution.annualization_periods or _annualization_periods(dataset.frequency)
    returns = [float(point["net_return"]) for point in curve[1:]]
    active_returns = [
        float(point["net_return"])
        for point in curve[1:]
        if abs(float(point["position"])) > 1e-12 or float(point["turnover"]) > 1e-12
    ]
    total_return = equity / initial - 1.0
    period_count = max(1, len(returns))
    annualized_return = (equity / initial) ** (annual_periods / period_count) - 1.0
    volatility = statistics.pstdev(returns) * math.sqrt(annual_periods) if len(returns) > 1 else 0.0
    mean_return = statistics.fmean(returns) if returns else 0.0
    period_std = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = mean_return / period_std * math.sqrt(annual_periods) if period_std > 0 else None
    max_drawdown = abs(min(float(point["drawdown"]) for point in curve))
    positive_sum = sum(value for value in active_returns if value > 0)
    negative_sum = abs(sum(value for value in active_returns if value < 0))
    metrics: dict[str, Any] = {
        "initial_capital": initial,
        "final_equity": equity,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": volatility,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_drawdown,
        "calmar_ratio": annualized_return / max_drawdown if max_drawdown > 0 else None,
        "win_rate": (
            sum(1 for value in active_returns if value > 0) / len(active_returns) if active_returns else None
        ),
        "win_rate_basis": "active_period",
        "profit_factor": positive_sum / negative_sum if negative_sum > 0 else None,
        "benchmark_total_return": bars[-1].close / bars[0].close - 1.0,
        "excess_total_return": total_return - (bars[-1].close / bars[0].close - 1.0),
        "trade_count": len(trades),
        "total_turnover": total_turnover,
        "total_commission": total_commission,
        "total_slippage": total_slippage,
        "total_stamp_duty": total_stamp_duty,
        "total_other_cost": total_other_cost,
        "total_cost": total_commission + total_slippage + total_stamp_duty + total_other_cost,
        "cost_drag_ratio": (
            (total_commission + total_slippage + total_stamp_duty + total_other_cost) / initial
        ),
        "bar_count": len(bars),
        "active_period_count": len(active_returns),
        "annualization_periods": annual_periods,
        "prediction_only": prediction_only,
        "return_forecast": forecast_metrics,
    }
    warnings: list[str] = []
    repair_count = int(dataset.metadata.get("runtime_ohlc_repair_count") or 0)
    if repair_count:
        warnings.append(f"运行时为绘图扩展了 {repair_count} 条异常 OHLC 的最高/最低范围；源文件未修改")
    if not cost_is_configured and not prediction_only:
        warnings.append("本次执行协议未计手续费、滑点、印花税或其他成交成本")
    if bars[-1].timestamp in by_time and not prediction_only:
        warnings.append("最后一根 K 线生成的信号没有下一根开盘价，因此未成交")

    return BacktestResult(
        dataset=dataset.summary(),
        strategy=dict(strategy or {"kind": "unknown", "name": "未命名策略", "version": "1"}),
        execution=execution.to_dict(),
        metrics=metrics,
        bars=tuple(bar.to_dict() for bar in bars),
        signals=tuple(signal.to_dict() for signal in sorted(signals, key=lambda item: item.timestamp)),
        equity_curve=tuple(curve),
        drawdown_curve=tuple(
            {"timestamp": point["timestamp"], "drawdown": point["drawdown"]} for point in curve
        ),
        trades=tuple(trades),
        warnings=tuple(warnings),
        return_forecasts=forecast_rows,
    )
