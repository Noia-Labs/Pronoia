"""Pure, common-market replay of already point-in-time aligned model signals.

The caller verifies the asset, freezes genuine OHLC, and maps each observation
to the first bar close at which it was known. This module performs no IO and
never consumes a historical Run's event-CAR or portfolio return curve.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from ..quant_backtest.engine import _annualization_periods, run_backtest
from ..quant_backtest.models import ExecutionConfig, MarketDataset, QuantBacktestError, Signal
from ..return_forecast_statistics import forecast_pair_statistics

SCHEMA_VERSION = "arena-model-comparison-v1"
_MAX_CURVE_POINTS = 2000
_MAX_DISPLAY_BARS = 3000
_MIN_ANNUALIZATION_PERIODS = 20


class ModelComparisonError(ValueError):
    """An invalid or unexecutable contestant/rule, suitable for an API error."""


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelComparisonError(f"{label} 必须是大于等于 {minimum} 的整数")
    return value


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelComparisonError(f"{label} 必须是有限数值")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ModelComparisonError(f"{label} 必须是大于等于 {minimum:g} 的有限数值")
    return number


def _rules(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("position_rule", "long_flat") != "long_flat":
        raise ModelComparisonError("当前共同回放只支持 long_flat：看涨持有，看跌或中性空仓")
    initial = _number(value.get("initial_capital", 100_000), "初始资金")
    if initial <= 0:
        raise ModelComparisonError("初始资金必须大于 0")
    return {
        "holding_bars": _integer(value.get("holding_bars", 3), "持有 K 线数", minimum=1),
        "fee_bps": _number(value.get("fee_bps", 3), "手续费 bps"),
        "slippage_bps": _number(value.get("slippage_bps", 2), "滑点 bps"),
        "initial_capital": initial,
        "position_rule": "long_flat",
    }


def _direction(number: float) -> str:
    return "up" if number > 0 else "down" if number < 0 else "neutral"


def _observations(contestant: Mapping[str, Any], bar_count: int) -> dict[int, dict[str, Any]]:
    name = str(contestant.get("name") or contestant.get("run_id") or "选手")
    result: dict[int, dict[str, Any]] = {}
    observations = contestant.get("observations") or []
    if not isinstance(observations, list):
        raise ModelComparisonError(f"{name} 的 observations 必须是列表")
    for item in observations:
        if not isinstance(item, Mapping):
            raise ModelComparisonError(f"{name} 包含格式无效的预测")
        index = _integer(item.get("bar_index"), f"{name} 的 bar_index")
        if index >= bar_count:
            raise ModelComparisonError(f"{name} 的 bar_index={index} 超出共同行情范围，请重新对齐信号")
        if index in result:
            raise ModelComparisonError(f"{name} 在第 {index} 根 K 线有多个预测，请先明确合并规则")
        forecast = item.get("expected_return_pct")
        if forecast is not None:
            forecast = _number(forecast, f"{name} 的预期收益率", minimum=-100.0)
            direction = _direction(forecast)
        else:
            direction = str(item.get("direction") or "").lower()
            if direction not in {"up", "down", "neutral"}:
                raise ModelComparisonError(f"{name} 的预测缺少有效方向或数值收益率")
        horizon = item.get("horizon_bars")
        if horizon is not None:
            horizon = _integer(horizon, f"{name} 的预测周期", minimum=1)
        result[index] = {
            "direction": direction,
            "expected_return_pct": forecast,
            "horizon_bars": horizon,
            "direction_basis": str(item.get("direction_basis") or "").lower(),
        }
    if not any(index < bar_count - 1 for index in result):
        raise ModelComparisonError(
            f"{name} 在共同区间没有可执行信号；请延长行情至信号之后至少一根 K 线，或选择有预测记录的区间"
        )
    return result


def _signals(
    dataset: MarketDataset, observations: Mapping[int, Mapping[str, Any]], holding_bars: int,
) -> tuple[list[Signal], int]:
    """One linear pass; expiry is a close signal executed at the next open."""
    signals: list[Signal] = []
    expiry = -1
    weight = 0.0
    covered = 0
    for index, bar in enumerate(dataset.bars[:-1]):
        observation = observations.get(index)
        if observation is not None:
            weight = 1.0 if observation["direction"] == "up" else 0.0
            expiry = index + holding_bars
        elif index >= expiry:
            weight = 0.0
        if index < expiry:
            # Includes an explicit bearish/neutral decision to stay in cash.
            covered += 1
        signals.append(Signal(timestamp=bar.timestamp, target_weight=weight))
    return signals, covered


def _safe_metrics(result: Any, frequency: str) -> dict[str, Any]:
    """Keep full-resolution trading metrics, with bounded annualization."""
    metrics = dict(result.metrics)
    period_count = max(0, len(result.equity_curve) - 1)
    annual_periods = _annualization_periods(frequency)
    metrics["annualization_periods"] = annual_periods
    metrics["annualization_min_observations"] = _MIN_ANNUALIZATION_PERIODS
    # No explicit forecasts are sent to the execution engine: its auxiliary
    # per-run forecast score would use a different sample from the common set.
    metrics.pop("return_forecast", None)
    annual_keys = ("annualized_return", "annualized_volatility", "sharpe_ratio", "calmar_ratio")
    if period_count < _MIN_ANNUALIZATION_PERIODS:
        for key in annual_keys:
            metrics[key] = None
        metrics["annualization_status"] = "insufficient_observations"
        return metrics
    try:
        final_value = float(result.equity_curve[-1]["net_value"])
        annual_return = math.expm1(math.log(final_value) * annual_periods / period_count)
    except (OverflowError, ValueError):
        annual_return = None
    metrics["annualized_return"] = annual_return if annual_return is not None and math.isfinite(annual_return) else None
    for key in ("annualized_volatility", "sharpe_ratio"):
        value = metrics.get(key)
        scaled = float(value) * math.sqrt(annual_periods) if value is not None else None
        metrics[key] = scaled if scaled is not None and math.isfinite(scaled) else None
    drawdown = metrics.get("max_drawdown")
    calmar = metrics["annualized_return"] / drawdown if metrics["annualized_return"] is not None and drawdown else None
    metrics["calmar_ratio"] = calmar if calmar is not None and math.isfinite(calmar) else None
    metrics["annualization_status"] = "available" if metrics["annualized_return"] is not None else "unavailable"
    return metrics


def _display_indices(length: int, *, extra: set[int] | None = None) -> list[int]:
    if length <= _MAX_CURVE_POINTS:
        return list(range(length))
    extra = set(extra or ()) | {0, length - 1}
    budget = _MAX_CURVE_POINTS - len(extra)
    indices = {round(i * (length - 1) / max(1, budget - 1)) for i in range(budget)}
    return sorted(indices | extra)


def _forecast_sets(
    dataset: MarketDataset, observations: Mapping[int, Mapping[str, Any]], horizon: int,
) -> tuple[dict[int, float], dict[int, str]]:
    numeric: dict[int, float] = {}
    directions: dict[int, str] = {}
    for index, item in observations.items():
        if index + horizon >= len(dataset.bars):
            continue
        value = item["expected_return_pct"]
        if value is not None:
            if item["horizon_bars"] == horizon:
                numeric[index] = value
                directions[index] = _direction(value)
        elif item["direction_basis"] in {"asset", "asset_return"} and item["horizon_bars"] in {None, horizon}:
            directions[index] = item["direction"]
    return numeric, directions


def compare_on_market(dataset: MarketDataset, contestants: list[dict], rules: dict) -> dict:
    """Replay normalized observations on one immutable OHLC series.

    MAE and direction accuracy each use their common, matured sample across
    all contestants. Trading P&L covers the common market window with cash in
    uncovered periods; coverage is reported separately and no ranking is made.
    """
    normalized_rules = _rules(rules)
    if not contestants:
        raise ModelComparisonError("请至少选择一个有预测记录的模型")
    ids = [str(contestant.get("run_id") or "") for contestant in contestants]
    if any(not run_id for run_id in ids) or len(ids) != len(set(ids)):
        raise ModelComparisonError("每个参赛模型必须有唯一且非空的 run_id")
    horizon = normalized_rules["holding_bars"]
    prepared = [_observations(contestant, len(dataset.bars)) for contestant in contestants]
    forecast_sets = [_forecast_sets(dataset, observation, horizon) for observation in prepared]
    numeric_common = set(forecast_sets[0][0]).intersection(*(set(item[0]) for item in forecast_sets[1:]))
    direction_common = set(forecast_sets[0][1]).intersection(*(set(item[1]) for item in forecast_sets[1:]))
    actual_returns = {
        index: (dataset.bars[index + horizon].close / dataset.bars[index].close - 1.0) * 100.0
        for index in numeric_common | direction_common
    }
    execution = ExecutionConfig(
        initial_capital=normalized_rules["initial_capital"],
        commission_bps=normalized_rules["fee_bps"],
        slippage_bps=normalized_rules["slippage_bps"],
        max_abs_weight=1.0,
        allow_short=False,
        # Avoid the old engine's unguarded power for very short experiments.
        # Annualized fields are restored safely from full-resolution results.
        annualization_periods=1,
    )
    models = []
    for contestant, observations, (numeric, directions) in zip(contestants, prepared, forecast_sets):
        run_id = str(contestant["run_id"])
        signals, covered_bars = _signals(dataset, observations, horizon)
        try:
            result = run_backtest(dataset, signals, execution=execution, strategy={
                "kind": "model_comparison_long_flat", "name": str(contestant.get("name") or run_id),
                "version": SCHEMA_VERSION,
            })
        except (QuantBacktestError, OverflowError, ValueError) as exc:
            raise ModelComparisonError(f"{contestant.get('name') or run_id} 无法完成共同回放：{exc}") from exc
        metrics = _safe_metrics(result, dataset.frequency)
        warnings = [str(value) for value in contestant.get("warnings", [])] + list(result.warnings)
        if len(dataset.bars) - 1 < _MIN_ANNUALIZATION_PERIODS:
            warnings.append("行情收益区间少于 20 个，年化收益、波动率和 Sharpe 暂不展示；累计收益与回撤按完整行情计算")
        if covered_bars < len(dataset.bars) - 1:
            warnings.append("没有有效预测覆盖的 K 线保持空仓，请结合覆盖率比较结果")
        if len(dataset.bars) - 1 in observations:
            warnings.append("最后一根 K 线的预测没有后续开盘价，未执行")
        if not numeric_common:
            warnings.append("没有共同的同周期数值预测样本")
        forecast_stats = forecast_pair_statistics(
            (numeric[index], actual_returns[index]) for index in numeric_common
        )
        numeric_direction_accuracy = forecast_stats.pop("direction_accuracy")
        numeric_direction_count = forecast_stats.pop("direction_evaluated_count")
        worst_drawdown = min(range(len(result.equity_curve)), key=lambda index: result.equity_curve[index]["drawdown"])
        indices = _display_indices(len(result.equity_curve), extra={worst_drawdown})
        models.append({
            "run_id": run_id,
            "name": str(contestant.get("name") or run_id),
            "model_kind": str(contestant.get("model_kind") or "unknown"),
            "metrics": metrics,
            "curve": [{
                "timestamp": result.equity_curve[index]["timestamp"],
                "net_value": result.equity_curve[index]["net_value"],
                "drawdown": result.equity_curve[index]["drawdown"],
            } for index in indices],
            "forecast": {
                **forecast_stats,
                "matched_samples": forecast_stats["n"],
                "own_n": len(numeric),
                "numeric_direction_accuracy": numeric_direction_accuracy,
                "numeric_direction_count": numeric_direction_count,
                "directional_accuracy": (
                    sum(directions[index] == _direction(actual_returns[index]) for index in direction_common) / len(direction_common)
                    if direction_common else None
                ),
                "directional_n": len(direction_common), "directional_own_n": len(directions),
                "horizon_bars": horizon, "unit": "percentage_points",
                "basis": "common_bar_close_to_close_return",
            },
            "coverage": {
                "signal_count": sum(index < len(dataset.bars) - 1 for index in observations),
                "covered_bars": covered_bars, "total_bars": len(dataset.bars) - 1,
                "ratio": covered_bars / (len(dataset.bars) - 1),
            },
            "warnings": list(dict.fromkeys(warnings)),
        })
    benchmark_indices = _display_indices(len(dataset.bars))
    displayed_bars = dataset.bars[-_MAX_DISPLAY_BARS:]
    notes = [
        "共同回放使用同一段冻结 OHLC；数值预测正负优先，否则采用方向。看涨持有 100%，看跌或中性空仓，不做空。",
        "信号在指定 K 线收盘后生效，于下一根开盘执行；持有指定数量 K 线后下一根开盘平仓，新信号会更新到期时间。",
        "手续费与滑点按每次实际仓位变动扣除；区间结束时按最后收盘价计价，不额外假设平仓。",
        "基准曲线为标的收盘价相对首根收盘价的变化，未扣交易成本；模型收益由其持仓与实际行情计算。",
        "收益预测误差只评估所有选手共同拥有、同周期且实际已到期的数值预测；超额收益方向不作为标的方向正确率。",
        "覆盖率以首根之后可执行的 K 线为分母，包含明确空仓判断；未覆盖期间保持现金，不生成跨覆盖率排名。",
        "图表可抽取真实观测点，指标始终使用完整分辨率计算。",
    ]
    if len(displayed_bars) < len(dataset.bars):
        notes.append(f"K 线仅返回共同区间最后 {_MAX_DISPLAY_BARS} 根原始 OHLC，未合成或插值；收益指标仍覆盖完整区间")
    return {
        "schema_version": SCHEMA_VERSION,
        "market": {
            "symbol": dataset.symbol, "market": dataset.market, "frequency": dataset.frequency,
            "start_at": dataset.bars[0].timestamp, "end_at": dataset.bars[-1].timestamp,
            "bar_count": len(dataset.bars),
        },
        "rules": normalized_rules,
        "models": models,
        "benchmark_curve": [{
            "timestamp": dataset.bars[index].timestamp,
            "net_value": dataset.bars[index].close / dataset.bars[0].close,
        } for index in benchmark_indices],
        "bars": [{
            "timestamp": bar.timestamp, "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close, "volume": bar.volume,
        } for bar in displayed_bars],
        "notes": notes,
    }
