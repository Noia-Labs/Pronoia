"""New binary runs validate every adapter at the shared commit boundary."""
from __future__ import annotations

import json
import socket

import pytest

from app import config, db
from app.event_backtest import orchestrator
from app.event_backtest.models import EventRecord, TeamPrediction


def prediction(**overrides):
    fields = dict(event_id="event", run_id="run", pred_direction="up", confidence=.4,
                  rationale="已有公告事实支持看涨，但置信度较低。", horizon="t3")
    fields.update(overrides)
    return TeamPrediction(**fields)


@pytest.mark.parametrize("cfg", [{}, {"direction_mode": "three_class"}])
def test_legacy_runs_are_not_reinterpreted(cfg):
    original = prediction(pred_direction="neutral", confidence=.5)
    assert orchestrator.enforce_binary_prediction(original, cfg) is original


@pytest.mark.parametrize("cfg", [{"direction_mode": "binary"}, {"evaluation_protocol": {"direction_mode": "binary"}}])
def test_binary_accepts_old_directional_format_and_retains_low_confidence(cfg):
    original = prediction(expected_return_pct=2.5)
    result = orchestrator.enforce_binary_prediction(original, cfg)
    assert result.pred_direction == "up" and result.confidence == .4
    assert result.abstain is False and result.expected_return_pct == 2.5
    assert result.strategy_metadata["prediction_status"] == "available"
    assert original.strategy_metadata.get("prediction_status") is None


def test_explicit_insufficient_data_is_abstention_without_forecast():
    original = prediction(pred_direction="neutral", confidence=None, abstain=True,
        rationale="缺少公告中的实际值与一致预期。", expected_return_pct=7,
        strategy_metadata={"prediction_status": "insufficient_data", "adapter": "external"})
    result = orchestrator.enforce_binary_prediction(original, {"direction_mode": "binary"})
    assert result.abstain and result.rationale == original.rationale
    assert result.expected_return_pct is None
    assert "expected_return_pct" not in result.strategy_metadata
    assert result.strategy_metadata["prediction_status"] == "insufficient_data"
    assert not result.strategy_metadata.get("output_validation_errors")
    assert original.expected_return_pct == 7


@pytest.mark.parametrize("metadata", [None, {"prediction_status": "available"}])
def test_unqualified_neutral_cannot_become_valid_binary_prediction(metadata):
    original = prediction(pred_direction="neutral", strategy_metadata=metadata,
                          rationale="资料缺失", expected_return_pct=3)
    result = orchestrator.enforce_binary_prediction(original, {"direction_mode": "binary"})
    assert result.abstain and result.expected_return_pct is None
    assert result.strategy_metadata["prediction_status"] == "invalid_output"
    assert "invalid_or_missing_direction" in result.strategy_metadata["output_validation_errors"]
    assert "无效输出" in result.rationale


@pytest.mark.parametrize("direction, rationale", [("up", "缺资料"), ("neutral", "")])
def test_malformed_insufficient_data_stays_technical_failure(direction, rationale):
    result = orchestrator.enforce_binary_prediction(prediction(
        pred_direction=direction, rationale=rationale, abstain=True,
        strategy_metadata={"prediction_status": "insufficient_data"},
    ), {"direction_mode": "binary"})
    assert result.strategy_metadata["prediction_status"] == "invalid_output"
    assert result.abstain and result.strategy_metadata["output_validation_errors"]


@pytest.mark.parametrize("failure", [
    {"output_failure_kind": "timeout"},
    {"output_validation_errors": ["invalid_json"]},
    {"completion_quality": "invalid"},
    {"validation": {"valid": False}},
])
def test_technical_failure_takes_precedence_over_insufficient_data(failure):
    result = orchestrator.enforce_binary_prediction(prediction(
        pred_direction="neutral", abstain=True, rationale="请求失败，资料缺失",
        strategy_metadata={"prediction_status": "insufficient_data", **failure},
    ), {"direction_mode": "binary"})
    assert result.strategy_metadata["prediction_status"] == "invalid_output"
    assert result.abstain


def test_voluntary_strategy_abstention_is_not_relabelled_as_missing_data():
    result = orchestrator.enforce_binary_prediction(prediction(
        pred_direction="neutral", abstain=True, rationale="策略主动跳过本次信号",
        expected_return_pct=1.5, strategy_metadata={"adapter": "external"},
    ), {"direction_mode": "binary"})
    assert result.strategy_metadata["prediction_status"] == "voluntary_abstain"
    assert result.abstain and result.expected_return_pct is None
    assert not result.strategy_metadata.get("output_validation_errors")


def test_binary_commit_applies_to_baseline_and_preserves_resume_records(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "isolated.db"))
    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected network access")
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    db.init_db()
    try:
        events = [EventRecord.from_dict({
            "event_id": eid, "market": "CN", "symbol": "600000",
            "event_time": "2025-01-03T09:00:00+08:00", "event_type_l2": "macro",
            "title": "公告发布", "event_text": "仅有标题，无数值。", "source_url": "manual://test",
        }) for eid in ("old", "new")]
        source, output = tmp_path / "events.jsonl", tmp_path / "predictions.jsonl"
        source.write_text("".join(json.dumps(event.to_dict()) + "\n" for event in events))
        run = db.create_bt_run(name="binary boundary", runner="baseline", events_path=str(source),
            out_path=str(output), total_events=2, config={"direction_mode": "binary"})
        old = prediction(event_id="old", run_id=run["id"], pred_direction="neutral", confidence=.5)
        output.write_text(json.dumps(old.to_dict()) + "\n")
        orchestrator._do_run(run["id"], 1)
        rows = {row["event_id"]: row for row in map(json.loads, output.read_text().splitlines())}
        assert rows["old"] == old.to_dict()
        assert rows["new"]["abstain"] is True
        assert rows["new"]["strategy_metadata"]["prediction_status"] == "invalid_output"
        persisted = db.get_bt_prediction(run["id"], "new")
        assert persisted["abstain"]
        assert persisted["strategy_metadata"]["prediction_status"] == "invalid_output"
    finally:
        if db._conn is not None:
            db._conn.close()
            db._conn = None
