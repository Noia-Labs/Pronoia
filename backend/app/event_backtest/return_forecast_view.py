"""Read-only per-event forecast errors from a Run's frozen input/output files."""
from __future__ import annotations

from pathlib import Path
import math
from typing import Any, Mapping

from .application import load_events, load_labels, load_predictions
from .evaluation_protocol import (
    chronological_event_ids, resolve_run_execution_spec, resolve_run_horizon,
    select_events_for_execution_window,
)
from .return_forecast import prediction_return_pct, realized_return_pct


def build_event_return_forecasts(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Only explicit, valid forecasts appear; unavailable actuals remain null."""
    events_path = str(run.get("events_path") or "")
    predictions_path = str(run.get("out_path") or "")
    if not events_path or not predictions_path:
        return []
    if not Path(events_path).is_file() or not Path(predictions_path).is_file():
        return []
    events = load_events(events_path)
    selection = select_events_for_execution_window(events, resolve_run_execution_spec(run))
    event_by_id = {event.event_id: event for event in selection.events}
    predictions = {prediction.event_id: prediction for prediction in load_predictions(predictions_path)}
    labels_path = str(run.get("labels_path") or "")
    labels = load_labels(labels_path) if labels_path and Path(labels_path).is_file() else []
    labels_by_id = {label.event_id: label for label in labels}
    horizon = resolve_run_horizon(run)
    rows: list[dict[str, Any]] = []
    for event_id in chronological_event_ids(selection.events):
        prediction = predictions.get(event_id)
        if prediction is None:
            continue
        forecast = prediction_return_pct(prediction, horizon=horizon)
        if forecast is None:
            continue
        actual = realized_return_pct(labels_by_id.get(event_id), horizon=horizon)
        error = forecast - actual if actual is not None else None
        if error is not None and not math.isfinite(error):
            error = None
        event = event_by_id[event_id]
        rows.append({
            "event_id": event_id, "symbol": event.symbol,
            "event_time": event.available_time or event.event_time,
            "horizon": horizon, "expected_return_pct": forecast,
            "actual_return_pct": actual, "error_pct": error,
            "status": "evaluated" if error is not None else "pending",
        })
    return rows
