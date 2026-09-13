"""Full-sample statistics for explicit asset-return forecasts.

The browser intentionally receives sampled rows for large minute datasets, so
all aggregates used for model/strategy comparison live on the backend.  The
helpers in this module are shared by event and portfolio forecasts to keep the
definitions identical.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence


FORECAST_COMPARISON_FIELDS = (
    "median_abs_error_pct",
    "p90_abs_error_pct",
    "error_std_pct",
    "mean_expected_return_pct",
    "mean_actual_return_pct",
    "direction_accuracy",
    "direction_evaluated_count",
    "direction_coverage",
    "up_call_hit_rate",
    "up_call_count",
    "down_call_hit_rate",
    "down_call_count",
    "pearson_ic",
    "spearman_rank_ic",
    "r_squared",
    "zero_baseline_rmse_pct",
    "skill_score_vs_zero",
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _root_mean_square(values: list[float]) -> float | None:
    """Return a stable RMS without overflowing while squaring large values."""
    if not values:
        return None
    scale = max(abs(value) for value in values)
    if scale == 0:
        return 0.0
    return scale * math.sqrt(math.fsum((value / scale) ** 2 for value in values) / len(values))


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    scale = max(abs(value) for value in values)
    if scale == 0:
        return 0.0
    value = scale * (math.fsum(item / scale for item in values) / len(values))
    return value if math.isfinite(value) else None


def _population_std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    scale = max(abs(value) for value in values)
    if scale == 0:
        return 0.0
    normalized = [value / scale for value in values]
    mean = math.fsum(normalized) / len(normalized)
    rms = _root_mean_square([value - mean for value in normalized])
    if rms is None:
        return None
    result = scale * rms
    return result if math.isfinite(result) else None


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(1.0, max(0.0, probability)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _one_minus_squared_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0.0):
        return None
    ratio = numerator / denominator
    if not math.isfinite(ratio) or abs(ratio) > math.sqrt(float.fromhex("0x1.fffffffffffffp+1023")):
        return None
    value = 1.0 - ratio * ratio
    return value if math.isfinite(value) else None


def _rank(values: list[float]) -> list[float]:
    """Average ranks for ties, with ranks starting at one."""
    output = [0.0] * len(values)
    ordered = sorted(range(len(values)), key=values.__getitem__)
    start = 0
    while start < len(ordered):
        end = start + 1
        value = values[ordered[start]]
        while end < len(ordered) and values[ordered[end]] == value:
            end += 1
        average_rank = (start + 1 + end) / 2.0
        for index in ordered[start:end]:
            output[index] = average_rank
        start = end
    return output


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_scale = max(abs(value) for value in left)
    right_scale = max(abs(value) for value in right)
    if left_scale == 0 or right_scale == 0:
        return None
    left_normalized = [value / left_scale for value in left]
    right_normalized = [value / right_scale for value in right]
    left_mean = math.fsum(left_normalized) / len(left_normalized)
    right_mean = math.fsum(right_normalized) / len(right_normalized)
    left_centered = [value - left_mean for value in left_normalized]
    right_centered = [value - right_mean for value in right_normalized]
    left_rms = _root_mean_square(left_centered)
    right_rms = _root_mean_square(right_centered)
    if left_rms in (None, 0.0) or right_rms in (None, 0.0):
        return None
    value = math.fsum(
        (a / left_rms) * (b / right_rms) / len(left_centered)
        for a, b in zip(left_centered, right_centered, strict=True)
    )
    return max(-1.0, min(1.0, value)) if math.isfinite(value) else None


def forecast_pair_statistics(pairs: Iterable[tuple[Any, Any]]) -> dict[str, Any]:
    """Compute comparison statistics from finite ``(forecast, actual)`` pairs.

    Returns are percentages, therefore error values are percentage points.  A
    directional observation requires both the forecast and realised return to
    be non-zero; a zero forecast is a neutral call rather than an up/down hit.
    """
    clean: list[tuple[float, float]] = []
    for forecast, actual in pairs:
        forecast_value = _finite(forecast)
        actual_value = _finite(actual)
        if (
            forecast_value is not None
            and actual_value is not None
            and math.isfinite(forecast_value - actual_value)
        ):
            clean.append((forecast_value, actual_value))

    forecasts = [forecast for forecast, _ in clean]
    actuals = [actual for _, actual in clean]
    errors = [forecast - actual for forecast, actual in clean]
    absolute_errors = [abs(error) for error in errors]
    count = len(clean)

    directional = [
        (forecast, actual)
        for forecast, actual in clean
        if forecast != 0.0 and actual != 0.0
    ]
    direction_hits = sum(forecast * actual > 0 for forecast, actual in directional)
    up_calls = [(forecast, actual) for forecast, actual in clean if forecast > 0.0 and actual != 0.0]
    down_calls = [(forecast, actual) for forecast, actual in clean if forecast < 0.0 and actual != 0.0]

    pearson = _correlation(forecasts, actuals)
    spearman = _correlation(_rank(forecasts), _rank(actuals)) if count >= 2 else None
    actual_mean = _mean(actuals)
    residual_rms = _root_mean_square(errors)
    actual_rms = _root_mean_square(actuals)
    centered_actual_rms = _population_std(actuals)
    r_squared = _one_minus_squared_ratio(residual_rms, centered_actual_rms)
    zero_skill = _one_minus_squared_ratio(residual_rms, actual_rms)

    return {
        "n": count,
        "mae_pct": _mean(absolute_errors),
        "rmse_pct": residual_rms,
        "median_abs_error_pct": _quantile(absolute_errors, 0.50),
        "p90_abs_error_pct": _quantile(absolute_errors, 0.90),
        "bias_pct": _mean(errors),
        "error_std_pct": _population_std(errors),
        "mean_expected_return_pct": _mean(forecasts),
        "mean_actual_return_pct": actual_mean,
        "direction_accuracy": direction_hits / len(directional) if directional else None,
        "direction_evaluated_count": len(directional),
        "direction_coverage": len(directional) / count if count else None,
        "up_call_hit_rate": sum(actual > 0 for _, actual in up_calls) / len(up_calls) if up_calls else None,
        "up_call_count": len(up_calls),
        "down_call_hit_rate": sum(actual < 0 for _, actual in down_calls) / len(down_calls) if down_calls else None,
        "down_call_count": len(down_calls),
        "pearson_ic": pearson,
        "spearman_rank_ic": spearman,
        "r_squared": r_squared,
        "zero_baseline_rmse_pct": actual_rms,
        "skill_score_vs_zero": zero_skill,
        "statistics_basis": "all_finite_evaluated_forecast_actual_pairs",
        "direction_definition": "matching_non_zero_signs",
        "skill_score_definition": "1_minus_model_mse_over_zero_forecast_mse",
    }


def enrich_forecast_summary(
    summary: Mapping[str, Any] | None,
    rows: Sequence[Mapping[str, Any]],
    *,
    horizon_key: str | None = None,
) -> dict[str, Any]:
    """Add full-sample statistics to a frozen legacy summary.

    Existing count/contract fields remain authoritative.  Statistics are only
    replaced when at least one finite evaluated row exists, so an old artifact
    that omitted row details is never silently turned into an empty result.
    """
    output = dict(summary or {})
    by_horizon = output.get("by_horizon")
    summary_complete = all(field in output for field in FORECAST_COMPARISON_FIELDS)
    horizons_complete = not (horizon_key and isinstance(by_horizon, list)) or all(
        not isinstance(item, Mapping)
        or all(field in item for field in FORECAST_COMPARISON_FIELDS)
        for item in by_horizon
    )
    if summary_complete and horizons_complete:
        return output

    pairs = [
        (row.get("expected_return_pct"), row.get("actual_return_pct"))
        for row in rows
        if isinstance(row, Mapping)
    ]
    stats = forecast_pair_statistics(pairs)
    if int(stats["n"]) > 0 or not output:
        output.update(stats)
        output["evaluated_count"] = int(stats["n"])

    by_horizon = output.get("by_horizon")
    if horizon_key and isinstance(by_horizon, list):
        enriched_horizons: list[Any] = []
        for item in by_horizon:
            if not isinstance(item, Mapping):
                enriched_horizons.append(item)
                continue
            horizon = item.get(horizon_key)
            horizon_rows = [row for row in rows if row.get(horizon_key) == horizon]
            enriched_horizons.append(
                enrich_forecast_summary(item, horizon_rows)
                if horizon_rows else dict(item)
            )
        output["by_horizon"] = enriched_horizons
    return output
