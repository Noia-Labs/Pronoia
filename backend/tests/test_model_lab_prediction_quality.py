"""Base-model evaluation keeps the event contract and exposes honest outcomes.

All model execution is replaced with persisted fixture outputs. The integration
tests exercise actual batch creation, frozen Run creation and result projection.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .test_model_lab import model_lab_env, _profile, _wait_batch, _wait_worker_exit


_SECRET = "candidate-secret-must-not-leak"


def _three_event_dataset(client: TestClient, root: Path) -> dict[str, Any]:
    from app.event_backtest.application import write_jsonl

    events_path, labels_path = root / "events.jsonl", root / "labels.jsonl"
    write_jsonl(events_path, [{
        "event_id": f"quality-{i}", "market": "CN", "symbol": "SH512800",
        "event_time": f"2025-01-{10 + i}T09:00:00+08:00",
        "available_time": f"2025-01-{10 + i}T09:00:00+08:00",
        "title": f"利率公告 {i}", "event_type_l2": "政策利率调整",
        "event_text": "官方公布本期利率为 3.10%，上期为 3.35%，下降 25 个基点。",
        "source_url": "https://example.com/official/rate",
        "benchmark_ticker": "sh000300",
    } for i in range(3)])
    write_jsonl(labels_path, [{
        "event_id": f"quality-{i}", "epsilon": 0,
        "label_t1": "up", "label_t3": "up", "label_t5": "up",
        "car_t1": .001, "car_t3": .003, "car_t5": .005,
        "car_method": "benchmark_relative_return",
    } for i in range(3)])
    response = client.post("/api/bt/datasets/register", json={
        "id": "base-quality-fixture", "name": "基模质量回归事件集",
        "dataset_kind": "event", "path": str(events_path),
        "labels_path": str(labels_path), "source": {"type": "local_file"},
    })
    assert response.status_code == 200, response.text
    return response.json()


def _batch(client: TestClient, dataset: dict, profile_ids: list[str], *, auto_start: bool) -> dict:
    response = client.post("/api/model-lab/batches", json={
        "name": "基模预测质量验证", "profile_ids": profile_ids,
        "auto_start": auto_start,
        "prediction": {"enabled": True, "dataset_id": dataset["id"],
                       "runner": "team_full", "horizon": "t3"},
        "qa": {"enabled": False},
    })
    assert response.status_code == 201, response.text
    return response.json()


def _persist_outcomes(run_id: str) -> None:
    from app import db
    from app.event_backtest.application import write_jsonl
    from app.event_backtest.models import TeamPrediction

    run = db.get_bt_run(run_id)
    assert run is not None
    cases = [
        ("up", False, "available", "公告利率由 3.35% 降至 3.10%。"),
        ("neutral", True, "insufficient_data", "缺少公告发布前的标的行情，无法核实既有价格反应。"),
        ("neutral", True, "invalid_output", f"API 响应格式错误，调试密钥 {_SECRET}"),
    ]
    output = []
    for i, (direction, abstain, status, reason) in enumerate(cases):
        metadata = {"prediction_status": status}
        if status == "invalid_output":
            metadata["output_validation_errors"] = ["invalid_direction"]
        pred = TeamPrediction(event_id=f"quality-{i}", pred_direction=direction,
                              confidence=.7 if not abstain else 0,
                              rationale=reason, abstain=abstain, run_id=run_id,
                              strategy_metadata=metadata)
        output.append(pred.to_dict())
        db.add_bt_prediction(run_id=run_id, event_id=pred.event_id,
                             symbol="SH512800", market="CN", pred_direction=direction,
                             confidence=pred.confidence, abstain=abstain, rationale=reason,
                             strategy_metadata=metadata)
    Path(run["out_path"]).parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(run["out_path"], output)
    db.update_bt_run_progress(run_id, done_events=3, total_events=3)
    db.update_bt_run_status(run_id, "done")


def _assert_quality(summary: dict[str, Any]) -> None:
    assert summary["direction_mode"] == "binary"
    assert summary["epsilon"] == 0
    assert summary["completion_quality"] == "completed_with_warnings"
    assert summary["warning_count"] == 2
    assert summary["n_outputs"] == 3
    assert summary["valid_output_count"] == 1
    assert summary["insufficient_data_count"] == 1
    assert summary["invalid_output_count"] == 1
    assert summary["neutral_count"] == 0
    assert summary["voluntary_abstain_count"] == 0
    assert summary["arena_eligible"] is False
    assert not summary.get("metrics_error")
    assert summary["issues_total"] == 2
    assert summary["issues_truncated"] is False
    issues = {item["prediction_status"]: item for item in summary["prediction_issues"]}
    assert set(issues) == {"insufficient_data", "invalid_output"}
    assert "公告发布前" in issues["insufficient_data"]["reason"]
    assert issues["invalid_output"]["reason"]
    assert _SECRET not in json.dumps(summary, ensure_ascii=False)


def test_profiles_share_binary_dataset_but_keep_separate_frozen_models(
    model_lab_env: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from app import db
    from app.event_backtest import orchestrator as orch

    client = model_lab_env
    profiles = [_profile(client, name="基模 A"), _profile(client, name="基模 B")]
    changed = client.patch(f"/api/model-lab/profiles/{profiles[1]['id']}", json={"model_id": "candidate-v2"})
    assert changed.status_code == 200, changed.text
    profiles[1] = changed.json()
    dataset = _three_event_dataset(client, tmp_path)
    created_runs: list[str] = []

    def fake_start(run_id: str):
        created_runs.append(run_id)
        _persist_outcomes(run_id)
        return orch.BacktestStartResult(ok=True, run_id=run_id)

    monkeypatch.setattr(orch, "start_bt_run", fake_start)
    batch = _batch(client, dataset, [p["id"] for p in profiles], auto_start=True)
    finished = _wait_batch(client, batch["id"])
    _wait_worker_exit(batch["id"])
    assert finished["status"] == "done"  # execution completion, not a claim of valid forecasts
    assert finished["completion_quality"] == "completed_with_warnings"
    assert finished["warning_count"] == 4
    assert finished["insufficient_data_count"] == finished["invalid_output_count"] == 2
    assert len(created_runs) == 2
    runs = [db.get_bt_run(run_id) for run_id in created_runs]
    assert {run["dataset_id"] for run in runs} == {dataset["id"]}
    assert len({run["dataset_version"] for run in runs}) == 1
    assert {run["config"]["model_profile_snapshot"]["id"] for run in runs} == {p["id"] for p in profiles}
    assert {run["config"]["model_profile_snapshot"]["model_id"] for run in runs} == {"candidate-v1", "candidate-v2"}
    for run in runs:
        assert run["config"]["direction_mode"] == "binary"
        assert run["config"]["evaluation_protocol"]["oracle_epsilon"] == 0
        assert run["config"]["evaluation_protocol"]["evaluation_horizon"] == "t3"
    response = client.get(f"/api/model-lab/batches/{batch['id']}/results")
    assert response.status_code == 200, response.text
    results = response.json()
    for prediction in results["summary"]["prediction_runs"]:
        _assert_quality(prediction)
        assert prediction["metrics"]["acc_primary_directional_trade"]["meta"]["n"] == 1
    assert _SECRET not in response.text
    listed = client.get("/api/model-lab/batches").json()
    matching = next(item for item in listed["items"] if item["id"] == batch["id"])
    assert matching["completion_quality"] == "completed_with_warnings"
    assert matching["warning_count"] == 4


def test_recovered_completed_prediction_reuses_run_and_retains_quality(
    model_lab_env: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from app import db
    from app.event_backtest import orchestrator as orch
    from app.model_lab import repository as repo, service
    from app.routes import backtest
    from app.schemas import CreateBacktestRunRequest

    client = model_lab_env
    profile = _profile(client)
    dataset = _three_event_dataset(client, tmp_path)
    batch = _batch(client, dataset, [profile["id"]], auto_start=False)
    task = batch["tasks"][0]
    created = backtest.create_run_from_trusted_request(CreateBacktestRunRequest(
        name="恢复前已完成的预测", runner="team_full", dataset_id=dataset["id"],
        dataset_version=task["config"]["dataset_version"], visibility="arena_safe",
        config={"model_lab_batch_id": batch["id"], "model_lab_task_id": task["id"],
                "model_profile_snapshot": task["config"]["profile_snapshot"]},
    ))
    _persist_outcomes(created["id"])
    repo.update_task(task["id"], status="running")  # missing linked id is the real crash gap
    repo.update_batch_status(batch["id"], "running")

    def must_not_start(_run_id: str):
        raise AssertionError("recovery must not call a model again for a completed prediction")

    monkeypatch.setattr(orch, "start_bt_run", must_not_start)
    assert service.recover_interrupted_batches() == [batch["id"]]
    finished = _wait_batch(client, batch["id"])
    _wait_worker_exit(batch["id"])
    assert finished["status"] == "done"
    assert len(db.list_bt_runs()) == 1
    assert finished["tasks"][0]["backtest_run_id"] == created["id"]
    results = client.get(f"/api/model-lab/batches/{batch['id']}/results").json()
    _assert_quality(results["summary"]["prediction_runs"][0])


def test_legacy_neutral_run_keeps_original_three_class_semantics(model_lab_env, tmp_path):
    from app import db
    from app.event_backtest.application import write_jsonl
    from app.event_backtest.models import TeamPrediction
    from app.model_lab import service

    dataset = _three_event_dataset(model_lab_env, tmp_path)
    predictions = tmp_path / "legacy-predictions.jsonl"
    pred = TeamPrediction(event_id="quality-0", pred_direction="neutral", confidence=.5,
                          rationale="历史有效中性判断", abstain=False, run_id="legacy")
    write_jsonl(predictions, [pred.to_dict()])
    before = predictions.read_bytes()
    run = db.create_bt_run(name="旧版评测", runner="team_full", events_path=dataset["path"],
                           labels_path=dataset["labels_path"], out_path=str(predictions), total_events=1)
    db.add_bt_prediction(run_id=run["id"], event_id="quality-0", pred_direction="neutral",
                         confidence=.5, abstain=False, rationale=pred.rationale)
    db.update_bt_run_status(run["id"], "done")
    summary = service._prediction_summary(db.get_bt_run(run["id"]))
    assert summary["direction_mode"] == "ternary"
    assert summary["neutral_count"] == 1
    assert summary["insufficient_data_count"] == summary["invalid_output_count"] == 0
    assert "direction_mode" not in (db.get_bt_run(run["id"])["config"] or {})
    assert db.get_bt_prediction(run["id"], "quality-0")["pred_direction"] == "neutral"
    assert predictions.read_bytes() == before


def test_metrics_failure_is_visible_and_never_arena_eligible(
    model_lab_env, tmp_path, monkeypatch,
):
    from app import db
    from app.model_lab import service
    from app.routes import backtest

    profile = _profile(model_lab_env)
    from app.model_lab.providers import profile_snapshot
    from app.model_lab import repository as repo
    snapshot = profile_snapshot(repo.get_profile(profile["id"], public=False))
    run = db.create_bt_run(name="指标计算失败", runner="team_full",
                           events_path=str(tmp_path / "missing.jsonl"),
                           out_path=str(tmp_path / "predictions.jsonl"), total_events=1,
                           visibility="arena_safe", config={"direction_mode": "binary",
                           "model_profile_snapshot": snapshot})
    db.add_bt_prediction(run_id=run["id"], event_id="one", pred_direction="up", confidence=.8,
                         strategy_metadata={"prediction_status": "available"})
    db.update_bt_run_status(run["id"], "done")

    def broken_metrics(_run_id):
        raise RuntimeError(f"指标文件损坏 {_SECRET}")

    monkeypatch.setattr(backtest, "get_run_metrics", broken_metrics)
    summary = service._prediction_summary(db.get_bt_run(run["id"]))
    assert summary["metrics_error"]
    assert summary["arena_eligible"] is False
    assert summary["completion_quality"] != "valid"
    assert _SECRET not in json.dumps(summary, ensure_ascii=False)


def test_real_inference_on_demo_facts_is_not_a_formal_arena_result(model_lab_env, tmp_path):
    from app import db
    from app.event_backtest.application import write_jsonl
    from app.event_backtest.models import TeamPrediction
    from app.model_lab import service

    dataset = _three_event_dataset(model_lab_env, tmp_path)
    path = tmp_path / "demo-facts-predictions.jsonl"
    run = db.create_bt_run(
        name="真实调用但事件事实仅供演示", runner="team_full",
        events_path=dataset["path"], labels_path=dataset["labels_path"],
        out_path=str(path), total_events=3, visibility="arena_safe",
        config={"demo": False, "direction_mode": "binary",
                "evaluation_protocol": {"oracle_epsilon": 0},
                "dataset_snapshot": {"semantic_quality": {
                    "status": "demo_only", "formal_evaluation_eligible": False,
                }}},
    )
    outputs = []
    for i in range(3):
        pred = TeamPrediction(event_id=f"quality-{i}", pred_direction="up", confidence=.7,
                              rationale="根据输入资料形成判断。", abstain=False, run_id=run["id"],
                              strategy_metadata={"prediction_status": "available"})
        outputs.append(pred.to_dict())
        db.add_bt_prediction(run_id=run["id"], event_id=pred.event_id,
                             pred_direction="up", confidence=.7, abstain=False,
                             strategy_metadata=pred.strategy_metadata)
    write_jsonl(path, outputs)
    db.update_bt_run_progress(run["id"], done_events=3, total_events=3)
    db.update_bt_run_status(run["id"], "done")
    summary = service._prediction_summary(db.get_bt_run(run["id"]))
    assert summary["demo"] is False  # real inference differs from dry-run orchestration
    assert summary["completion_quality"] == "valid"
    assert summary["valid_output_count"] == 3
    assert summary["warning_count"] == 0
    assert not summary["metrics_error"]
    assert summary["dataset_semantic_quality"] == "demo_only"
    assert summary["dataset_demo"] is True
    assert summary["formal_evaluation_eligible"] is False
    assert summary["arena_eligible"] is False
