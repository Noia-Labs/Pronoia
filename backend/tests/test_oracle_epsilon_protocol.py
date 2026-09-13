"""Frozen Oracle metadata is independent of mutable datasets and label scoring."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.event_backtest.evaluation_protocol import resolve_run_oracle_epsilon
from app.event_backtest.models import EventLabel, TeamPrediction


@pytest.mark.parametrize("value", [0, 0.002, "0"])
def test_frozen_protocol_threshold_wins_including_zero(value):
    run = {"config": {"evaluation_protocol": {"oracle_epsilon": value}, "oracle_epsilon": .02},
           "dataset": {"epsilon": .05}}
    assert resolve_run_oracle_epsilon(run) == float(value)


@pytest.mark.parametrize("run", [None, {}, {"config": {}}, {"dataset": {"epsilon": 0}},
                                  {"config": {"dataset_snapshot": {"epsilon": 0}}}])
def test_legacy_default_never_reads_current_dataset(run):
    assert resolve_run_oracle_epsilon(run) == .005


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "invalid"])
def test_malformed_frozen_threshold_is_not_silently_replaced(value):
    with pytest.raises(ValueError, match="oracle_epsilon"):
        resolve_run_oracle_epsilon({"config": {"evaluation_protocol": {"oracle_epsilon": value}}})


def test_metadata_threshold_does_not_reclassify_saved_labels():
    from app.event_backtest.metrics_registry import compute_all_metrics
    labels = [EventLabel.from_dict({"event_id": "small", "car_t3": .001, "label_t3": "up"})]
    predictions = [TeamPrediction.from_dict({"event_id": "small", "pred_direction": "up", "confidence": .8})]
    for epsilon in (0, .005):
        results = compute_all_metrics(predictions=predictions, labels=labels, epsilon=epsilon)
        assert results["acc_primary_non_neutral"].value == 1
        assert labels[0].label_t3 == "up"


def test_runner_summary_receives_explicit_zero_threshold():
    from app.event_backtest import orchestrator
    with patch.object(orchestrator.db, "update_bt_run_progress"), \
         patch.object(orchestrator.db, "add_bt_metrics_snapshot", return_value="snapshot"), \
         patch.object(orchestrator, "sse_broadcast"):
        summary = orchestrator._maybe_snapshot(
            "offline", [], None, done_count=0, force=True, oracle_epsilon=0,
        )
    assert summary is not None
    assert summary.epsilon == 0
