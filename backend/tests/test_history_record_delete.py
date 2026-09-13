"""Deletion contracts for the mixed Run/Model-Lab history page."""
from __future__ import annotations

import threading
from typing import Any

from fastapi.testclient import TestClient

from .test_model_lab import _profile, model_lab_env


def _run(*, name: str, status: str = "done", config: dict[str, Any] | None = None) -> dict[str, Any]:
    from app import db

    row = db.create_bt_run(
        name=name,
        runner="baseline",
        events_path=f"/isolated/{name}-events.jsonl",
        out_path=f"/isolated/{name}-predictions.jsonl",
        config=config,
    )
    if status != "pending":
        row = db.update_bt_run_status(str(row["id"]), status) or row
    return row


def _batch(profile_id: str, *, name: str = "基模评测", status: str = "done") -> dict[str, Any]:
    from app.model_lab import repository as repo

    row = repo.create_batch(
        name=name,
        profile_ids=[profile_id],
        dataset_id=None,
        dataset_version=None,
        question_set_id=None,
        prediction={"enabled": True},
        qa={"enabled": False},
        scoring={"source": "pronoia_base_evaluation"},
        task_specs=[{"kind": "prediction", "profile_id": profile_id, "total_items": 1, "config": {}}],
    )
    if status != "pending":
        row = repo.update_batch_status(str(row["id"]), status) or row
    return row


def test_mixed_history_delete_removes_batch_children_and_an_independent_run(
    model_lab_env: TestClient,
) -> None:
    from app import db
    from app.model_lab import repository as repo

    client = model_lab_env
    profile = _profile(client)
    batch = _batch(str(profile["id"]))
    task = repo.list_tasks(str(batch["id"]))[0]
    child = _run(name="batch-child", config={
        "model_lab_batch_id": batch["id"],
        "model_lab_task_id": task["id"],
    })
    repo.update_task(
        str(task["id"]),
        status="done",
        backtest_run_id=str(child["id"]),
        result_summary={"backtest_run_id": child["id"], "arena_eligible": True},
    )
    independent = _run(name="independent")

    response = client.post("/api/bt/history/delete", json={
        "run_ids": [independent["id"]],
        "batch_ids": [batch["id"]],
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["requested_count"] == 2
    assert set(payload["deleted_run_ids"]) == {child["id"], independent["id"]}
    assert payload["deleted_batch_ids"] == [batch["id"]]
    assert db.get_bt_run(str(child["id"])) is None
    assert db.get_bt_run(str(independent["id"])) is None
    assert repo.get_batch(str(batch["id"])) is None
    assert repo.get_task(str(task["id"])) is None


def test_batch_delete_does_not_trust_a_forged_batch_id_without_matching_task_lineage(
    model_lab_env: TestClient,
) -> None:
    from app import db
    from app.model_lab import repository as repo

    client = model_lab_env
    profile = _profile(client)
    batch = _batch(str(profile["id"]))
    unrelated = _run(name="not-a-batch-child", config={"model_lab_batch_id": batch["id"]})

    response = client.post("/api/bt/history/delete", json={
        "run_ids": [],
        "batch_ids": [batch["id"]],
    })

    assert response.status_code == 200, response.text
    assert repo.get_batch(str(batch["id"])) is None
    assert db.get_bt_run(str(unrelated["id"])) is not None


def test_batch_delete_recovers_a_trusted_run_created_before_its_task_link_committed(
    model_lab_env: TestClient,
) -> None:
    from app import db
    from app.model_lab import repository as repo

    client = model_lab_env
    profile = _profile(client)
    batch = _batch(str(profile["id"]))
    task = repo.list_tasks(str(batch["id"]))[0]
    recovery_gap_run = _run(name="trusted-recovery-gap", config={
        "model_lab_batch_id": batch["id"],
        "model_lab_task_id": task["id"],
        "evaluation_source": "model_lab",
        "model_profile_snapshot": {"id": profile["id"], "model_id": "frozen-v1"},
    })

    response = client.post("/api/bt/history/delete", json={
        "run_ids": [],
        "batch_ids": [batch["id"]],
    })

    assert response.status_code == 200, response.text
    assert db.get_bt_run(str(recovery_gap_run["id"])) is None
    assert repo.get_batch(str(batch["id"])) is None


def test_direct_run_delete_restricts_a_trusted_unlinked_model_lab_run(
    model_lab_env: TestClient,
) -> None:
    from app import db
    from app.model_lab import repository as repo

    client = model_lab_env
    profile = _profile(client)
    batch = _batch(str(profile["id"]), status="running")
    task = repo.list_tasks(str(batch["id"]))[0]
    repo.update_task(str(task["id"]), status="running")
    recovery_gap_run = _run(name="active-recovery-gap", status="pending", config={
        "model_lab_batch_id": batch["id"],
        "model_lab_task_id": task["id"],
        "evaluation_source": "model_lab",
        "model_profile_snapshot": {"id": profile["id"], "model_id": "frozen-v1"},
    })

    response = client.delete(f"/api/bt/runs/{recovery_gap_run['id']}")

    assert response.status_code == 409, response.text
    assert "基模评测批次" in response.text
    assert db.get_bt_run(str(recovery_gap_run["id"])) is not None
    assert repo.get_batch(str(batch["id"])) is not None


def test_public_team_run_cannot_forge_model_lab_task_lineage(
    model_lab_env: TestClient,
) -> None:
    response = model_lab_env.post("/api/bt/runs", json={
        "name": "forged-lineage",
        "runner": "team_full",
        "config": {
            "model_lab_batch_id": "batch-id",
            "model_lab_task_id": "task-id",
            "evaluation_source": "model_lab",
        },
    })
    assert response.status_code == 422, response.text
    assert "关联由服务端生成" in response.text


def test_history_delete_is_atomic_when_one_selected_run_is_active(
    model_lab_env: TestClient,
) -> None:
    from app import db

    client = model_lab_env
    finished = _run(name="finished")
    active = _run(name="active", status="running")
    response = client.post("/api/bt/history/delete", json={
        "run_ids": [finished["id"], active["id"]],
        "batch_ids": [],
    })
    assert response.status_code == 409, response.text
    assert "运行中的记录不能删除" in response.text
    assert db.get_bt_run(str(finished["id"])) is not None
    assert db.get_bt_run(str(active["id"])) is not None


def test_history_delete_restricts_arena_references_without_partial_deletion(
    model_lab_env: TestClient,
) -> None:
    from app import db

    client = model_lab_env
    protected = _run(name="arena-member")
    ordinary = _run(name="ordinary")
    arena = db.create_bt_arena(name="保留对比", run_ids=[str(protected["id"])])
    response = client.post("/api/bt/history/delete", json={
        "run_ids": [protected["id"], ordinary["id"]],
        "batch_ids": [],
    })
    assert response.status_code == 409, response.text
    assert "保留对比" in response.text
    assert db.get_bt_run(str(protected["id"])) is not None
    assert db.get_bt_run(str(ordinary["id"])) is not None
    assert db.get_bt_arena(str(arena["id"])) is not None


def test_history_delete_finds_workspace_source_runs_in_config_and_frozen_result(
    model_lab_env: TestClient,
) -> None:
    from app import db

    client = model_lab_env
    configured = _run(name="workspace-config-source")
    frozen = _run(name="workspace-result-source")
    db.create_bt_arena(
        name="进行中的工作台",
        run_ids=[],
        arena_type="workspace",
        config={"source_run_ids": [configured["id"]]},
    )
    completed = db.create_bt_arena(
        name="历史工作台",
        run_ids=[],
        arena_type="workspace",
        config={"arena_workspace": {"version": 1}},
    )
    db.update_bt_arena_status(
        str(completed["id"]),
        "done",
        result={"contestants": [{"source_run_id": frozen["id"]}]},
    )

    response = client.post("/api/bt/history/delete", json={
        "run_ids": [configured["id"], frozen["id"]],
        "batch_ids": [],
    })

    assert response.status_code == 409, response.text
    assert db.get_bt_run(str(configured["id"])) is not None
    assert db.get_bt_run(str(frozen["id"])) is not None


def test_single_event_run_delete_also_removes_its_terminal_qa_sidecar(
    model_lab_env: TestClient,
) -> None:
    from app import db
    from app.model_lab import repository as repo

    client = model_lab_env
    profile = _profile(client)
    run = _run(name="event-with-qa")
    sidecar = _batch(str(profile["id"]), name="事件问答", status="done")
    with db._lock:
        connection = db._get_conn()
        connection.execute(
            "UPDATE ml_batches SET scoring_config_json=? WHERE id=?",
            ('{"source":"event_experiment"}', sidecar["id"]),
        )
        connection.execute(
            "INSERT INTO ml_event_experiments(run_id,qa_batch_id,auto_start,created_at) VALUES(?,?,0,?)",
            (run["id"], sidecar["id"], db.now_iso()),
        )
        connection.commit()

    response = client.delete(f"/api/bt/runs/{run['id']}")
    assert response.status_code == 200, response.text
    assert db.get_bt_run(str(run["id"])) is None
    assert repo.get_batch(str(sidecar["id"])) is None


def test_history_delete_rejects_stale_selection_without_deleting_existing_rows(
    model_lab_env: TestClient,
) -> None:
    from app import db

    client = model_lab_env
    existing = _run(name="still-here")
    response = client.post("/api/bt/history/delete", json={
        "run_ids": [existing["id"], "missing-run"],
        "batch_ids": [],
    })
    assert response.status_code == 404, response.text
    assert db.get_bt_run(str(existing["id"])) is not None


def test_acknowledged_cancelled_event_run_deletes_before_late_callback_returns(
    model_lab_env: TestClient,
    tmp_path,
    monkeypatch,
) -> None:
    """A provider may outlive cancel/delete, but its callback stays inert."""
    from app import db
    from app.event_backtest import orchestrator as orch
    from app.event_backtest.cancellation import BacktestCancelled
    from app.event_backtest.models import EventRecord, TeamPrediction

    client = model_lab_env
    run = db.create_bt_run(
        name="cancelled-live-event-worker",
        runner="team_full",
        events_path=str(tmp_path / "events.jsonl"),
        out_path=str(tmp_path / "predictions.jsonl"),
        ckpt_dir=str(tmp_path / "trajectory"),
        total_events=1,
    )
    run_id = str(run["id"])
    event = EventRecord(
        event_id="late-event",
        market="CN",
        symbol="600000.SH",
        event_time="2025-01-02T09:30:00+08:00",
        event_type_l2="公告",
        title="迟到回调",
        event_text="仅用于取消并发回归。",
        source_url="manual://late-event",
    )
    prediction = TeamPrediction(
        event_id=event.event_id,
        pred_direction="up",
        run_id=run_id,
        model_version="controlled",
        confidence=0.7,
        rationale="controlled late result",
    )
    callback_passed_first_check = threading.Event()
    release_callback = threading.Event()
    worker_errors: list[BaseException] = []
    original_enforce = orch.enforce_binary_prediction

    def delayed_enforce(item, config):
        # _on_pred has already performed its fast cancellation check here, but
        # has not entered the db-locked persistence section yet.
        callback_passed_first_check.set()
        assert release_callback.wait(timeout=3)
        return original_enforce(item, config)

    def controlled_team_full(_events, **kwargs):
        try:
            kwargs["on_pred"](prediction, event)
        except BacktestCancelled as exc:
            # Simulate a poorly behaved adapter swallowing callback cancellation
            # and returning a late result anyway. Final publication must also
            # independently observe the absorbing cancellation/deletion state.
            worker_errors.append(exc)
        return [prediction]

    monkeypatch.setattr(orch, "load_events", lambda _path: [event])
    monkeypatch.setattr(orch, "validate_events", lambda _events: [])
    monkeypatch.setattr(orch, "enforce_binary_prediction", delayed_enforce)
    monkeypatch.setattr(orch, "_run_team_full_with_cb", controlled_team_full)

    started = orch.start_bt_run(run_id)
    assert started.ok, started.error
    assert callback_passed_first_check.wait(timeout=3)
    worker = orch._RUN_TASKS[run_id]
    assert worker.is_alive()

    try:
        cancelled = client.post(f"/api/bt/runs/{run_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["ok"] is True
        assert db.get_bt_run(run_id)["status"] == "cancelled"
        assert worker.is_alive(), "test must delete while the provider callback is still blocked"

        deleted = client.delete(f"/api/bt/runs/{run_id}")
        assert deleted.status_code == 200, deleted.text
        assert db.get_bt_run(run_id) is None
    finally:
        release_callback.set()
        worker.join(timeout=3)
        orch._RUN_TASKS.pop(run_id, None)
        orch._RUN_CANCEL.pop(run_id, None)
        orch._RUN_RESUME.pop(run_id, None)

    assert not worker.is_alive()
    assert worker_errors and isinstance(worker_errors[0], BacktestCancelled)
    assert db.get_bt_run(run_id) is None
    assert db.list_bt_predictions(run_id)[0] == 0
    assert db.list_bt_metrics_snapshots(run_id) == []
    assert not (tmp_path / "predictions.jsonl").exists()


def test_cancelled_status_without_worker_acknowledgement_still_blocks_delete(
    model_lab_env: TestClient,
) -> None:
    """A durable status alone must not waive protection for an owning worker."""
    from app import db
    from app.event_backtest import orchestrator as orch

    client = model_lab_env
    run = _run(name="unacknowledged-cancel", status="cancelled")
    run_id = str(run["id"])
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(timeout=3), daemon=True)
    orch._RUN_TASKS[run_id] = worker
    orch._RUN_CANCEL[run_id] = False
    worker.start()
    try:
        response = client.delete(f"/api/bt/runs/{run_id}")
        assert response.status_code == 409, response.text
        assert db.get_bt_run(run_id) is not None
    finally:
        release.set()
        worker.join(timeout=3)
        orch._RUN_TASKS.pop(run_id, None)
        orch._RUN_CANCEL.pop(run_id, None)

    assert not worker.is_alive()
