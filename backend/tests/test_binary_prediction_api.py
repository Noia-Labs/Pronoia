"""Regression coverage for binary event run status and honest denominators."""
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from app import config, db
from app.event_backtest.application import write_jsonl
from app.event_backtest.models import EventLabel, EventRecord, TeamPrediction
from app.routes import backtest
from app.schemas import BacktestRunResponse, CreateBacktestRunRequest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "_conn", None)
    db.init_db()
    yield tmp_path
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def event(eid):
    return EventRecord.from_dict(dict(event_id=eid, market="US", symbol="SPY",
        event_time="2025-01-03T08:30:00-05:00", event_type_l2="macro",
        title="CPI release", event_text="Actual CPI 2.8%, expected 3.0%.",
        source_url="manual://test"))


def test_new_event_creation_freezes_binary_contract(isolated_db, monkeypatch):
    root = isolated_db
    events = root / "events.jsonl"
    write_jsonl(events, [event("e1").to_dict()])
    monkeypatch.setattr(backtest, "_auto_paths", lambda *a, **kw:
        (str(root / "preds.jsonl"), str(root / "ckpt"), str(root / "result.json")))
    prepared = backtest.prepare_run(CreateBacktestRunRequest(
        name="Binary", runner="baseline", events_path=str(events),
        config={"direction_mode": "ternary", "evaluation_protocol": {"direction_mode": "ternary"}},
    ))
    assert prepared["config"]["direction_mode"] == "binary"
    assert prepared["config"]["evaluation_protocol"]["direction_mode"] == "binary"
    from app.event_backtest.protocol import build_protocol_hash
    legacy_protocol = dict(prepared["config"]["evaluation_protocol"])
    legacy_protocol.pop("direction_mode")
    legacy_hash = build_protocol_hash(events_path=str(events), labels_path=None,
        execution_spec=prepared["execution_spec"], evaluation_protocol=legacy_protocol,
        dataset_version=prepared["dataset_version"], engine_mode="event_proxy")
    assert legacy_hash != prepared["protocol_hash"]


def test_quality_counts_insufficient_separately_and_keeps_history(isolated_db):
    root = isolated_db
    run = db.create_bt_run(name="Quality", runner="team_full", events_path=str(root / "events.jsonl"),
        out_path=str(root / "preds.jsonl"), total_events=4, config={"direction_mode": "binary"})
    outcomes = [("up", False, "available"), ("neutral", True, "insufficient_data"),
                ("neutral", True, "invalid_output"), ("neutral", True, "voluntary_abstain")]
    for i, (direction, abstain, status) in enumerate(outcomes):
        db.add_bt_prediction(run_id=run["id"], event_id=str(i), pred_direction=direction,
            abstain=abstain, strategy_metadata={"prediction_status": status})
    result = db.get_bt_run(run["id"])
    assert result["insufficient_data_count"] == 1
    assert result["invalid_output_count"] == 1
    assert result["voluntary_abstain_count"] == 1
    assert result["warning_count"] == 2
    assert result["completion_quality"] == "completed_with_warnings"
    assert BacktestRunResponse(**result).insufficient_data_count == 1
    legacy = db.create_bt_run(name="Legacy", runner="team_full", events_path=str(root / "events.jsonl"),
        out_path=str(root / "old.jsonl"), total_events=1)
    db.add_bt_prediction(run_id=legacy["id"], event_id="old", pred_direction="neutral", confidence=.5)
    assert db.get_bt_run(legacy["id"])["completion_quality"] == "valid"
    assert db.get_bt_prediction(legacy["id"], "old")["pred_direction"] == "neutral"


@pytest.mark.parametrize("with_labels", [True, False])
def test_metrics_do_not_confuse_missing_data_with_accuracy(isolated_db, with_labels):
    root = isolated_db
    events, preds, labels = root / "events.jsonl", root / "preds.jsonl", root / "labels.jsonl"
    write_jsonl(events, [event(str(i)).to_dict() for i in range(4)])
    outcomes = [("up", False, "available"), ("up", False, "available"),
                ("neutral", True, "insufficient_data"), ("neutral", True, "invalid_output")]
    write_jsonl(preds, [TeamPrediction(event_id=str(i), pred_direction=d, confidence=.6,
        rationale="Actual evidence" if not abst else "缺少公告原文", run_id="test", abstain=abst,
        strategy_metadata={"prediction_status": s}).to_dict()
        for i, (d, abst, s) in enumerate(outcomes)])
    write_jsonl(labels, [EventLabel(event_id=str(i), label_t1=d, label_t3=d, label_t5=d,
        car_t1=.02 if d == "up" else -.02, car_t3=.02 if d == "up" else -.02,
        car_t5=.02 if d == "up" else -.02).to_dict()
        for i, d in enumerate(["up", "down", "up", "down"])])
    run = db.create_bt_run(name="Metrics", runner="team_full", events_path=str(events),
        out_path=str(preds), labels_path=str(labels) if with_labels else None,
        total_events=4, config={"direction_mode": "binary"})
    result = backtest.get_run_metrics(run["id"])
    assert result["direction_mode"] == "binary"
    assert result["n_outputs"] == 4
    assert result["insufficient_data_count"] == 1
    assert result["invalid_output_count"] == 1
    assert result["valid_output_count"] == 2
    assert result["voluntary_abstain_count"] == result["neutral_count"] == 0
    accuracy = result["metrics"]["acc_primary_directional_trade"]
    assert accuracy["value"] == (.5 if with_labels else None)
    assert accuracy["meta"]["n"] == (2 if with_labels else 0)
