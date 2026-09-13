"""Score explicit asset-return forecasts against the matching realized horizon."""
from __future__ import annotations

from typing import Any, Sequence

from ..return_forecast_statistics import forecast_pair_statistics
from .models import EventLabel, TeamPrediction, finite_expected_return_pct


def prediction_return_pct(prediction: TeamPrediction, *, horizon: str) -> float | None:
    value = finite_expected_return_pct(prediction.expected_return_pct)
    if value is None or prediction.abstain or prediction.horizon != horizon:
        return None
    contract = (prediction.strategy_metadata or {}).get("return_forecast_contract") or {}
    if not isinstance(contract, dict) or (contract and (
        contract.get("horizon") != horizon or contract.get("unit") != "percent"
        or contract.get("basis") != "asset_return"
    )):
        return None
    return value


def realized_return_pct(label: EventLabel | None, *, horizon: str) -> float | None:
    """The Oracle stores decimal asset returns, never strategy or CAR returns."""
    actual = finite_expected_return_pct(getattr(label, f"ret_{horizon}", None))
    return finite_expected_return_pct(actual * 100.0) if actual is not None else None


def compute_return_forecast(
    predictions: Sequence[TeamPrediction], labels: Sequence[EventLabel], *, horizon: str,
) -> dict[str, Any]:
    by_id = {prediction.event_id: prediction for prediction in predictions if prediction.event_id}
    label_by_id = {label.event_id: label for label in labels if label.event_id}
    forecasts = 0
    pairs: list[tuple[float, float]] = []
    for event_id, prediction in by_id.items():
        value = prediction_return_pct(prediction, horizon=horizon)
        if value is None:
            continue
        forecasts += 1
        label = label_by_id.get(event_id)
        # ret_tN stores the asset return as a decimal. CAR and direction labels
        # are never substitutes for this numeric realized outcome.
        actual = realized_return_pct(label, horizon=horizon)
        if actual is None:
            continue
        pairs.append((value, actual))
    stats = forecast_pair_statistics(pairs)
    n = int(stats["n"])
    total = len(by_id)
    return {
        "status": "provided" if forecasts else "not_provided",
        **stats,
        "evaluated_count": n, "n_predictions": total, "n_forecasts": forecasts,
        "coverage": forecasts / total if total else 0.0,
        "evaluation_coverage": n / total if total else 0.0,
        "pending_count": forecasts - n,
        "unit": "percentage_points", "forecast_unit": "percent",
        "horizon": horizon, "basis": "asset_return",
        "actual_field": f"ret_{horizon}", "error_definition": "forecast_minus_actual",
    }
