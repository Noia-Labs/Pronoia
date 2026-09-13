"""Focused regressions for the event backtest's information and scoring contract."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd

from app.event_backtest import labeller, orchestrator
from app.event_backtest.engine import run_team_prompt, validate_event
from app.event_backtest.evaluation_protocol import select_events_for_execution_window
from app.event_backtest.metrics import compute_metrics
from app.event_backtest.models import EventLabel, EventRecord, TeamPrediction


def _event(event_id: str = "evt", **overrides) -> EventRecord:
    row = {
        "event_id": event_id,
        "market": "CN",
        "symbol": "600000",
        "event_time": "2025-01-03T09:00:00+08:00",
        "event_type_l2": "announcement",
        "title": "测试公告",
        "event_text": "测试正文",
        "source_url": "manual://audit",
    }
    row.update(overrides)
    return EventRecord.from_dict(row)


def _prediction(event_id: str, direction: str, *, abstain: bool = False) -> TeamPrediction:
    return TeamPrediction(
        event_id=event_id,
        pred_direction=direction,  # type: ignore[arg-type]
        run_id="audit-run",
        confidence=0.8,
        abstain=abstain,
        horizon="t3",
    )


def test_available_time_is_canonical_and_market_local_for_window_and_oracle(tmp_path: Path):
    event = _event(
        event_time="2025-01-02T14:00:00+08:00",
        occurred_at="2025-01-02T14:00:00+08:00",
        # 16:30 UTC is 00:30 on Jan 3 in Shanghai.
        available_time="2025-01-02T16:30:00Z",
    )
    assert event.event_time == "2025-01-02T16:30:00Z"
    assert event.occurred_at == "2025-01-02T14:00:00+08:00"

    jan2 = select_events_for_execution_window(
        [event], {"start_date": "2025-01-02", "end_date": "2025-01-02"}
    )
    jan3 = select_events_for_execution_window(
        [event], {"start_date": "2025-01-03", "end_date": "2025-01-03"}
    )
    assert jan2.after_count == 0
    assert jan3.event_ids == {"evt"}

    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(event.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
    oracle_event = labeller.load_events(path)[0]
    assert oracle_event.event_date == dt.date(2025, 1, 3)
    assert oracle_event.event_time_raw == event.available_time


def test_available_time_before_occurrence_is_rejected():
    event = _event(
        occurred_at="2025-01-03T10:00:00+08:00",
        available_time="2025-01-03T09:59:59+08:00",
    )
    assert "available_time 不能早于 occurred_at" in validate_event(event)


def test_invalid_llm_output_becomes_neutral_abstain_instead_of_directional_trade():
    event = _event()
    invalid = {"pred_direction": "sideways", "confidence": "NaN", "rationale": "bad schema"}
    with patch("app.event_backtest.engine.complete_json", new=AsyncMock(return_value=invalid)):
        result = asyncio.run(
            run_team_prompt([event], run_id="audit-run", target_horizon="t5")
        )
    assert len(result) == 1
    assert result[0].pred_direction == "neutral"
    assert result[0].abstain is True
    assert result[0].horizon == "t5"
    assert set(result[0].strategy_metadata["output_validation_errors"]) == {
        "invalid_or_missing_direction",
        "invalid_or_missing_confidence",
    }


def test_direction_without_matching_finite_car_is_excluded_from_accuracy():
    predictions = [_prediction("missing", "up"), _prediction("valid", "up")]
    labels = [
        EventLabel.from_dict({
            "event_id": "missing",
            "label_t1": "", "label_t3": "up", "label_t5": "",
            "car_t3": None,
        }),
        EventLabel.from_dict({
            "event_id": "valid",
            "label_t1": "", "label_t3": "up", "label_t5": "",
            "car_t3": 0.02,
        }),
    ]
    metrics = compute_metrics(
        predictions=predictions, labels=labels, primary_oracle_horizon="t3"
    )
    assert metrics.n_total == 2
    assert metrics.n_abstain_oracle == 1
    assert metrics.acc_t3_strict.n == 1
    assert metrics.acc_t3_strict.k == 1


def test_post_close_and_weekend_anchor_once_to_next_observable_close():
    dates = [
        dt.date(2025, 1, 3),  # Friday
        dt.date(2025, 1, 6),  # Monday
        dt.date(2025, 1, 7),
        dt.date(2025, 1, 8),
    ]
    closes = pd.Series([100.0, 110.0, 121.0, 133.1], index=dates)

    # Friday after close: information first enters at Monday close, T+1 is Tuesday.
    post_close = labeller._event_anchor_dates(
        closes, dates[0], 1, "2025-01-03T16:30:00+08:00", "CN"
    )
    assert post_close[:2] == (dates[1], dates[2])
    assert abs(
        labeller._car(closes, dates[0], 1, "2025-01-03T16:30:00+08:00", "CN")
        - 0.1
    ) < 1e-12

    # A Saturday event already advances to Monday; it must not be shifted again.
    weekend = labeller._event_anchor_dates(
        closes, dt.date(2025, 1, 4), 1, "2025-01-04T20:00:00+08:00", "CN"
    )
    assert weekend[:2] == (dates[1], dates[2])

    # Date-only timestamps are conservatively treated as after close/unknown.
    date_only = labeller._event_anchor_dates(closes, dates[0], 1, "2025-01-03", "CN")
    assert date_only[:2] == (dates[1], dates[2])


def test_db_is_correct_matches_strict_metric_for_neutral_abstain_and_missing_car(tmp_path: Path):
    cases = [
        ("hit", "up", False, "up", 0.02, True),
        ("miss", "down", False, "up", 0.02, False),
        ("pred-neutral", "neutral", False, "up", 0.02, False),
        ("oracle-neutral", "neutral", False, "neutral", 0.0, False),
        ("abstain", "up", True, "up", 0.02, None),
        ("missing-car", "up", False, "up", None, None),
    ]
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text(
        "".join(
            json.dumps({
                "event_id": event_id,
                "label_t1": "", "label_t3": oracle, "label_t5": "",
                "car_t3": car,
            }) + "\n"
            for event_id, _, _, oracle, car, _ in cases
        ),
        encoding="utf-8",
    )
    predictions = [
        _prediction(event_id, direction, abstain=abstain)
        for event_id, direction, abstain, _, _, _ in cases
    ]

    with patch.object(
        orchestrator.db, "update_bt_prediction_oracle", return_value=True
    ) as update:
        orchestrator._sync_labels_for_predictions(
            "audit-run", predictions, labels_path, primary_oracle_horizon="t3"
        )

    stored = {
        call.args[1]: call.kwargs["is_correct_t3"] for call in update.call_args_list
    }
    assert stored == {event_id: expected for event_id, *_, expected in cases}
