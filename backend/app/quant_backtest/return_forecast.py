"""Point-in-time asset-return forecasts, separate from portfolio performance."""

from __future__ import annotations

import statistics
from typing import Any, Mapping, Sequence

from ..return_forecast_statistics import forecast_pair_statistics
from .models import ContextRecord, MarketDataset, Signal, positive_bar_count


FORECAST_CONTRACT = "pronoia.quant-return-forecast.v1"


class HistoricalMeanReturnForecast:
    """Average the latest fully observed N-bar close-to-close asset returns.

    At close t, every sample ends at or before t.  ``lookback`` is the number
    of completed return observations, which requires lookback + N close prices.
    No position is inferred from the forecast; zero weights only satisfy the
    legacy portfolio engine's input contract.
    """

    def __init__(self, *, lookback: int = 20, horizon_bars: int = 3):
        self.lookback = positive_bar_count(lookback, name="lookback")
        self.horizon_bars = positive_bar_count(horizon_bars, name="horizon_bars")

    def describe(self) -> Mapping[str, Any]:
        return {
            "kind": "return_forecast",
            "name": "历史 N 周期收益均值预测",
            "version": "1",
            "source": "builtin",
            "prediction_only": True,
            "point_in_time_enforced": True,
            "forecast_contract": FORECAST_CONTRACT,
            "parameters": {
                "method": "historical_mean",
                "lookback": self.lookback,
                "horizon_bars": self.horizon_bars,
            },
        }

    def generate(self, dataset: MarketDataset, *, context: Sequence[ContextRecord] = ()) -> Sequence[Signal]:
        del context
        output: list[Signal] = []
        horizon = self.horizon_bars
        for index, bar in enumerate(dataset.bars):
            if index < self.lookback + horizon - 1:
                output.append(Signal(bar.timestamp, 0.0, metadata={"forecast_status": "warmup"}))
                continue
            realized_samples = [
                (dataset.bars[end].close / dataset.bars[end - horizon].close - 1.0) * 100.0
                for end in range(index - self.lookback + 1, index + 1)
            ]
            output.append(Signal(
                bar.timestamp,
                0.0,
                expected_return_pct=statistics.fmean(realized_samples),
                horizon_bars=horizon,
                metadata={"forecast_method": "historical_mean", "sample_count": self.lookback,
                          "information_cutoff": bar.timestamp},
            ))
        return output


def evaluate_return_forecasts(
    dataset: MarketDataset,
    signals: Sequence[Signal],
    *,
    strategy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Score explicit forecasts after inference; never derive one from score/weight.

    Coverage is forecasts provided / all timestamps for that horizon. Error
    metrics only use outcomes already present in the frozen dataset; the final
    N bars have pending outcomes and never enter those errors.
    """
    bars = dataset.bars
    indices = {bar.timestamp: index for index, bar in enumerate(bars)}
    rows: list[dict[str, Any]] = []
    horizons: set[int] = set()
    config = (strategy or {}).get("parameters") or {}
    if (strategy or {}).get("kind") == "return_forecast" and config.get("horizon_bars") is not None:
        horizons.add(positive_bar_count(config["horizon_bars"], name="horizon_bars"))
    for signal in signals:
        if signal.expected_return_pct is None:
            continue
        horizon = int(signal.horizon_bars)
        horizons.add(horizon)
        index = indices[signal.timestamp]
        target = bars[index + horizon] if index + horizon < len(bars) else None
        actual = (target.close / bars[index].close - 1.0) * 100.0 if target else None
        rows.append({
            "timestamp": signal.timestamp,
            "horizon_bars": horizon,
            "target_timestamp": target.timestamp if target else None,
            "expected_return_pct": signal.expected_return_pct,
            "actual_return_pct": actual,
            "error_pct": signal.expected_return_pct - actual if actual is not None else None,
            "status": "evaluated" if target else "pending",
        })
    rows.sort(key=lambda row: indices[row["timestamp"]])

    def summarize(subset: list[dict[str, Any]], selected: Sequence[int]) -> dict[str, Any]:
        stats = forecast_pair_statistics(
            (row.get("expected_return_pct"), row.get("actual_return_pct"))
            for row in subset
        )
        evaluated_count = int(stats["n"])
        eligible = sum(max(0, len(bars) - horizon) for horizon in selected)
        total = len(bars) * max(1, len(selected))
        return {
            "status": "provided" if subset else "not_provided",
            **stats,
            "evaluated_count": evaluated_count,
            "n_predictions": total,
            "n_forecasts": len(subset),
            "eligible_count": eligible,
            "pending_count": len(subset) - evaluated_count,
            "coverage": len(subset) / total,
            "evaluation_coverage": evaluated_count / total,
            "eligible_evaluation_coverage": evaluated_count / eligible if eligible else None,
            "unit": "percentage_points",
            "forecast_unit": "percent",
            "horizon_bars": selected[0] if len(selected) == 1 else None,
            "basis": "asset_close_to_close_return",
            "coverage_basis": "explicit_forecasts_over_all_bar_horizon_pairs",
            "error_definition": "expected_minus_actual",
        }

    ordered_horizons = sorted(horizons)
    summary = summarize(rows, ordered_horizons)
    summary["schema_version"] = FORECAST_CONTRACT
    summary["by_horizon"] = [
        summarize([row for row in rows if row["horizon_bars"] == horizon], [horizon])
        for horizon in ordered_horizons
    ]
    return summary, tuple(rows)
