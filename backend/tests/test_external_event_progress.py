"""External event services retain progress without retrying paid requests."""
from __future__ import annotations

import threading

import pytest

from app import config, db
from app.event_backtest import external_strategy, orchestrator
from app.event_backtest.application import load_predictions, write_jsonl
from app.event_backtest.cancellation import BacktestCancelled
from app.event_backtest.models import EventRecord


def event(event_id):
    return EventRecord.from_dict({
        "event_id": event_id, "market": "US", "symbol": "SPY",
        "event_time": "2025-01-03T08:30:00-05:00", "event_type_l2": "macro",
        "title": "CPI release", "event_text": "CPI actual 2.8%, expected 3.0%.",
        "source_url": "https://example.invalid/cpi",
    })


def decision(event_id):
    return {"event_id": event_id, "direction": "up", "confidence": .8,
            "rationale": "Lower reported inflation", "expected_return_pct": 1.5}


SPEC = {"type": "event", "adapter": "external_http", "endpoint": "https://example.invalid/signal"}


@pytest.mark.parametrize("failure", ["http", "malformed"])
def test_success_commits_before_next_request_and_failure_does_not_retry(monkeypatch, failure):
    trace = []

    def post(endpoint, payload, spec):
        event_id = payload["event"]["event_id"]
        trace.append(("post", event_id))
        if event_id == "e2":
            if failure == "http":
                raise ValueError("provider failed")
            return {"direction": "wrong"}
        return decision(event_id)

    monkeypatch.setattr(external_strategy, "_post", post)
    with pytest.raises(ValueError):
        external_strategy.run_external_event_strategy(
            [event("e1"), event("e2"), event("e3")], run_id="isolated", spec=SPEC,
            model_version="service-v1", before_request=lambda: trace.append(("check", None)),
            on_pred=lambda pred: trace.append(("saved", pred.event_id)),
        )
    assert trace == [("check", None), ("post", "e1"), ("saved", "e1"),
                     ("check", None), ("post", "e2")]


def test_callback_failure_stops_before_another_provider_request(monkeypatch):
    requests = []

    def post(endpoint, payload, spec):
        requests.append(payload["event"]["event_id"])
        return decision(requests[-1])

    def cannot_commit(pred):
        raise OSError("checkpoint unavailable")

    monkeypatch.setattr(external_strategy, "_post", post)
    with pytest.raises(OSError, match="checkpoint"):
        external_strategy.run_external_event_strategy(
            [event("e1"), event("e2")], run_id="isolated", spec=SPEC,
            model_version="service-v1", on_pred=cannot_commit,
        )
    assert requests == ["e1"]


@pytest.mark.parametrize("batch", [False, True])
def test_empty_resume_and_cancel_before_request_never_contact_service(monkeypatch, batch):
    def forbidden_post(*args):
        pytest.fail("No request should be sent")

    def cancelled():
        raise BacktestCancelled("cancelled")

    monkeypatch.setattr(external_strategy, "_post", forbidden_post)
    assert external_strategy.run_external_event_strategy(
        [], run_id="isolated", spec={**SPEC, "batch": batch}, model_version="v1",
    ) == []
    with pytest.raises(BacktestCancelled):
        external_strategy.run_external_event_strategy(
            [event("e1")], run_id="isolated", spec={**SPEC, "batch": batch},
            model_version="v1", before_request=cancelled,
        )


@pytest.mark.parametrize("bad_rows", [
    [decision("e1"), {**decision("e2"), "confidence": 2}],
    [decision("e1"), decision("e1")],
    [decision("e1"), decision("outside")],
])
def test_batch_must_validate_all_rows_before_committing(monkeypatch, bad_rows):
    saved = []
    monkeypatch.setattr(external_strategy, "_post", lambda *args: {"decisions": bad_rows})
    with pytest.raises(ValueError):
        external_strategy.run_external_event_strategy(
            [event("e1"), event("e2")], run_id="isolated", spec={**SPEC, "batch": True},
            model_version="v1", on_pred=saved.append,
        )
    assert saved == []


def test_shared_request_builder_matches_runtime_payload(monkeypatch):
    current = event("e1")
    spec = {**SPEC, "parameters": {"custom_option": "x"}}
    expected = external_strategy.build_event_request(current, spec, "t5")
    sent = []
    monkeypatch.setattr(external_strategy, "_post", lambda endpoint, payload, spec: sent.append(payload) or decision("e1"))
    external_strategy.run_external_event_strategy(
        [current], run_id="isolated", spec=spec, model_version="v1", target_horizon="t5",
    )
    assert sent == [expected]
    assert expected["evaluation_horizon"] == "t5"
    assert expected["return_forecast_contract"]["horizon"] == "t5"
    assert "analysis_direction" not in expected["event"]


@pytest.fixture
def isolated_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(db, "_conn", None)
    db.init_db()
    events_path = tmp_path / "events.jsonl"
    write_jsonl(events_path, [event("e1").to_dict(), event("e2").to_dict()])
    run = db.create_bt_run(
        name="isolated external progress", runner="event_external_http",
        events_path=str(events_path), out_path=str(tmp_path / "predictions.jsonl"),
        total_events=2, strategy_spec=SPEC,
    )
    try:
        yield run
    finally:
        orchestrator._RUN_CANCEL.pop(run["id"], None)
        orchestrator._RUN_RESUME.pop(run["id"], None)
        orchestrator.clear_run_activity(run["id"])
        if db._conn is not None:
            db._conn.close()


def test_orchestrator_persists_each_success_and_resume_skips_finished_requests(isolated_run, monkeypatch):
    run = isolated_run
    requests = []
    should_fail = True
    broadcasts = []

    def post(endpoint, payload, spec):
        event_id = payload["event"]["event_id"]
        requests.append(event_id)
        if event_id == "e2":
            assert db.get_bt_run(run["id"])["done_events"] == 1
            assert db.get_bt_prediction(run["id"], "e1") is not None
            assert [p.event_id for p in load_predictions(run["out_path"])] == ["e1"]
            if should_fail:
                raise ValueError("provider failed")
        return decision(event_id)

    monkeypatch.setattr(external_strategy, "_post", post)
    monkeypatch.setattr(orchestrator, "sse_broadcast", lambda rid, payload: broadcasts.append(payload))
    with pytest.raises(ValueError, match="provider failed"):
        orchestrator._do_run(run["id"], 4)
    assert requests == ["e1", "e2"]
    assert db.get_bt_run(run["id"])["done_events"] == 1
    # A failed resumed request preserves the pre-existing completed count.
    with pytest.raises(ValueError, match="provider failed"):
        orchestrator._do_run(run["id"], 4)
    assert requests == ["e1", "e2", "e2"]
    should_fail = False
    orchestrator._do_run(run["id"], 4)
    assert requests == ["e1", "e2", "e2", "e2"]
    assert db.get_bt_run(run["id"])["status"] == "done"
    assert db.get_bt_run(run["id"])["done_events"] == 2
    assert [p.event_id for p in load_predictions(run["out_path"])] == ["e1", "e2"]
    assert [p["prediction"]["event_id"] for p in broadcasts if p["type"] == "prediction"] == ["e1", "e2"]


@pytest.mark.parametrize("cancel", [False, True])
def test_pause_between_requests_blocks_next_call_and_can_cancel(isolated_run, monkeypatch, cancel):
    run_id = isolated_run["id"]
    requests = []
    errors = []
    waiting = threading.Event()
    resume = orchestrator._get_resume_event(run_id)
    original_wait = orchestrator._wait_if_paused

    def post(endpoint, payload, spec):
        event_id = payload["event"]["event_id"]
        requests.append(event_id)
        return decision(event_id)

    def broadcast(rid, payload):
        if payload["type"] == "prediction" and payload["prediction"]["event_id"] == "e1":
            resume.clear()

    def wait_if_paused(rid):
        if not resume.is_set():
            waiting.set()
        original_wait(rid)

    def work():
        try:
            orchestrator._do_run(run_id, 4)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(external_strategy, "_post", post)
    monkeypatch.setattr(orchestrator, "sse_broadcast", broadcast)
    monkeypatch.setattr(orchestrator, "_wait_if_paused", wait_if_paused)
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        assert waiting.wait(2), "Worker should pause before requesting the second event"
        assert requests == ["e1"]
        assert db.get_bt_run(run_id)["done_events"] == 1
        if cancel:
            orchestrator._RUN_CANCEL[run_id] = True
        resume.set()
        worker.join(2)
        assert not worker.is_alive()
        if cancel:
            assert len(errors) == 1 and isinstance(errors[0], BacktestCancelled)
            assert requests == ["e1"]
            assert db.get_bt_run(run_id)["done_events"] == 1
        else:
            assert errors == []
            assert requests == ["e1", "e2"]
            assert db.get_bt_run(run_id)["status"] == "done"
    finally:
        resume.set()
        worker.join(2)
