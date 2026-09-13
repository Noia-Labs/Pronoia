"""Model Lab API, execution isolation and credential-boundary tests."""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def model_lab_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app import config, db, llm
    from app.main import app
    from app.model_lab import service
    from app.routes import backtest

    # Never let a previous suite's singleton connection point at its closed
    # temporary database.
    if db._conn is not None:
        db._conn.close()
    db._conn = None
    old_db, old_data, old_route_data = config.DB_PATH, config.DATA_DIR, backtest.DATA_DIR
    config.DB_PATH = str(tmp_path / "model-lab.db")
    config.DATA_DIR = str(tmp_path / "data")
    backtest.DATA_DIR = str(tmp_path / "data")
    monkeypatch.setenv("PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS", "1")
    monkeypatch.setenv("PRONOIA_MODEL_SECRET_CANDIDATE", "candidate-secret-must-not-leak")
    monkeypatch.setenv("PRONOIA_MODEL_SECRET_ROTATED", "rotated-secret-must-not-leak")
    db.init_db()
    llm._profile_clients.clear()
    yield TestClient(app)

    # Every test waits for its worker. This assertion prevents a future test
    # from accidentally tearing down SQLite under a live daemon thread.
    deadline = time.time() + 3
    while time.time() < deadline:
        with service._WORKERS_LOCK:
            if not any(thread.is_alive() for thread in service._BATCH_THREADS.values()):
                break
        time.sleep(0.02)
    with service._WORKERS_LOCK:
        assert not any(thread.is_alive() for thread in service._BATCH_THREADS.values())
    if db._conn is not None:
        db._conn.close()
    db._conn = None
    config.DB_PATH, config.DATA_DIR, backtest.DATA_DIR = old_db, old_data, old_route_data
    llm._profile_clients.clear()


def _profile(client: TestClient, *, name: str = "候选模型") -> dict[str, Any]:
    response = client.post("/api/model-lab/profiles", json={
        "name": name,
        "provider": "openai-compatible",
        "base_url": "http://127.0.0.1:9/v1",
        "model_id": "candidate-v1",
        "secret_env_ref": "PRONOIA_MODEL_SECRET_CANDIDATE",
        "input_price_per_million": 1.5,
        "output_price_per_million": 3,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _dataset(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/bt/datasets/manual", json={
        "name": "Model Lab 事件集",
        "events": [{
            "market": "CN",
            "symbol": "600000",
            "event_time": "2025-01-10T09:00:00+08:00",
            "available_time": "2025-01-10T09:05:00+08:00",
            "title": "正式公告",
            "event_text": "公司发布可追溯的正式公告。",
        }],
    })
    assert response.status_code == 200, response.text
    return response.json()


def _wait_batch(client: TestClient, batch_id: str, timeout: float = 5) -> dict[str, Any]:
    deadline = time.time() + timeout
    current: dict[str, Any] = {}
    while time.time() < deadline:
        response = client.get(f"/api/model-lab/batches/{batch_id}")
        assert response.status_code == 200, response.text
        current = response.json()
        if current.get("status") in {"done", "partial", "failed", "cancelled"}:
            return current
        time.sleep(0.02)
    raise AssertionError(f"batch did not finish: {current}")


def _wait_worker_exit(batch_id: str, timeout: float = 5) -> None:
    from app.model_lab import service

    deadline = time.time() + timeout
    while time.time() < deadline:
        with service._WORKERS_LOCK:
            worker = service._BATCH_THREADS.get(batch_id)
            if worker is None or not worker.is_alive():
                return
        time.sleep(0.02)
    raise AssertionError(f"worker did not exit: {batch_id}")


def test_profiles_redact_secrets_and_default_is_snapshotted(
    model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from app import db, llm
    from app.model_lab import repository as repo
    from app import model_endpoint_security

    client = model_lab_env
    pasted = client.post("/api/model-lab/profiles", json={
        "name": "错误密钥输入",
        "provider": "openai-compatible",
        "base_url": "http://127.0.0.1:9/v1",
        "model_id": "candidate-v1",
        "secret_env_ref": "sk-user-accidentally-pasted-this",
        "api_key": "another-plaintext-secret",
    })
    assert pasted.status_code == 422
    assert "sk-user-accidentally-pasted-this" not in pasted.text
    assert "another-plaintext-secret" not in pasted.text

    for index, bad_url in enumerate((
        "https://user:embedded-secret@example.com/v1",
        "https://example.com/v1?api_key=embedded-secret",
        "https://example.com/v1#embedded-secret",
    )):
        rejected = client.post("/api/model-lab/profiles", json={
            "name": f"URL 中的错误密钥 {index}",
            "provider": "openai-compatible",
            "base_url": bad_url,
            "model_id": "candidate-v1",
            "secret_env_ref": "PRONOIA_MODEL_SECRET_CANDIDATE",
        })
        assert rejected.status_code == 422
        assert "embedded-secret" not in rejected.text

    profile = _profile(client)
    profile_id = profile["id"]

    for response in (
        client.get("/api/model-lab/profiles"),
        client.get(f"/api/model-lab/profiles/{profile_id}"),
    ):
        assert response.status_code == 200
        assert "candidate-secret-must-not-leak" not in response.text
        assert "PRONOIA_MODEL_SECRET_CANDIDATE" not in response.text
        assert "secret_env_ref" not in response.text
        assert "secret_configured" in response.text

    # Blank means keep the server-side reference.
    updated = client.patch(f"/api/model-lab/profiles/{profile_id}", json={
        "secret_env_ref": "",
    })
    assert updated.status_code == 200, updated.text
    assert repo.get_profile(profile_id, public=False)["secret_env_ref"] == "PRONOIA_MODEL_SECRET_CANDIDATE"

    selected = client.post(f"/api/model-lab/profiles/{profile_id}/set-default")
    assert selected.status_code == 200, selected.text
    assert "secret_env_ref" not in selected.text

    # Avoid a real DNS lookup while exercising default resolution and client
    # cache identity. The credential itself remains process-only.
    monkeypatch.setattr(model_endpoint_security, "validate_runtime_model_endpoint", lambda *a, **k: None)
    target = llm.resolve_runtime_target()
    assert target.model_id == "candidate-v1"
    assert target.profile_id == profile_id

    first_dataset = _dataset(client)
    first = client.post("/api/bt/runs", json={
        "name": "默认模型快照 v1",
        "runner": "team_full",
        "dataset_id": first_dataset["id"],
    })
    assert first.status_code == 200, first.text
    assert "secret_env_ref" not in first.text
    assert "PRONOIA_MODEL_SECRET_CANDIDATE" not in first.text
    first_run = db.get_bt_run(first.json()["id"])
    assert first_run["config"]["model_profile_snapshot"]["model_id"] == "candidate-v1"

    changed = client.patch(f"/api/model-lab/profiles/{profile_id}", json={
        "model_id": "candidate-v2",
        "secret_env_ref": "PRONOIA_MODEL_SECRET_ROTATED",
    })
    assert changed.status_code == 200, changed.text
    second = client.post("/api/bt/runs", json={
        "name": "默认模型快照 v2",
        "runner": "team_full",
        "dataset_id": first_dataset["id"],
    })
    assert second.status_code == 200, second.text
    assert "secret_env_ref" not in second.text
    assert "PRONOIA_MODEL_SECRET_ROTATED" not in second.text
    second_run = db.get_bt_run(second.json()["id"])
    assert first_run["config"]["model_profile_snapshot"]["model_id"] == "candidate-v1"
    assert second_run["config"]["model_profile_snapshot"]["model_id"] == "candidate-v2"
    assert second_run["model_version"] == "candidate-v2"

    # Changing the env reference under the same profile id cannot reuse a
    # client that captured the previous API key.
    created_clients: list[object] = []

    def fake_client(**_kwargs):
        instance = object()
        created_clients.append(instance)
        return instance

    monkeypatch.setattr(llm, "AsyncOpenAI", fake_client)
    llm._profile_clients.clear()
    current_client = llm.get_client()
    client.patch(f"/api/model-lab/profiles/{profile_id}", json={
        "secret_env_ref": "PRONOIA_MODEL_SECRET_CANDIDATE",
    })
    next_client = llm.get_client()
    assert current_client is not next_client
    assert len(created_clients) == 2


def test_builtin_bank_and_dry_run_never_creates_arena_run(model_lab_env: TestClient):
    from app import db

    client = model_lab_env
    profile = _profile(client)
    question_sets = client.get("/api/model-lab/question-sets")
    assert question_sets.status_code == 200
    builtin = question_sets.json()["items"][0]
    assert builtin["is_builtin"] is True
    assert builtin["question_count"] == 17
    dataset = _dataset(client)

    response = client.post("/api/model-lab/batches", json={
        "name": "仅验证编排",
        "profile_ids": [profile["id"]],
        "auto_start": True,
        "dry_run": True,
        "prediction": {"enabled": True, "dataset_id": dataset["id"], "runner": "team_full"},
        "qa": {
            "enabled": True,
            "question_set_id": builtin["id"],
            "question_ids": ["V01"],
            "variants": ["pronoia"],
            "scoring_mode": "auto",
            "judge_profile_id": profile["id"],
        },
    })
    assert response.status_code == 201, response.text
    batch = _wait_batch(client, response.json()["id"])
    assert batch["status"] == "done"
    assert db.list_bt_runs() == []

    results = client.get(f"/api/model-lab/batches/{batch['id']}/results")
    assert results.status_code == 200, results.text
    payload = results.json()
    assert payload["summary"]["demo"] is True
    assert payload["summary"]["formal_results"] is False
    assert payload["summary"]["prediction_runs"][0]["bt_run_id"] is None
    assert payload["summary"]["prediction_runs"][0]["arena_eligible"] is False
    assert {item["status"] for item in payload["qa_results"]} == {"demo"}
    assert all("不得作为正式评测结果" in item["answer"] for item in payload["qa_results"])
    assert "candidate-secret-must-not-leak" not in results.text
    assert "PRONOIA_MODEL_SECRET_CANDIDATE" not in results.text


def test_question_library_banks_preserve_source_fields_and_scoring_caveats(
    model_lab_env: TestClient,
):
    from app.model_lab.question_bank import BUILTIN_QUESTION_SETS, BUILTIN_SET_ID

    client = model_lab_env
    response = client.get("/api/model-lab/question-sets")
    assert response.status_code == 200
    banks = response.json()["items"]
    assert banks[0]["id"] == BUILTIN_SET_ID
    assert {bank["id"]: bank["question_count"] for bank in banks} == {
        "pronoia-core-finance-v1": 17,
        "pronoia-finance-comprehensive-v1": 41,
        "pronoia-common-comparison-v1": 18,
        "pronoia-special-capabilities-v1": 6,
    }
    assert all(bank["is_builtin"] for bank in banks)

    all_ids: list[str] = []
    for source in BUILTIN_QUESTION_SETS:
        detail = client.get(f"/api/model-lab/question-sets/{source['id']}")
        assert detail.status_code == 200, detail.text
        questions = detail.json()["questions"]
        assert [item["code"] for item in questions] == [item["code"] for item in source["questions"]]
        for actual, expected in zip(questions, source["questions"]):
            assert actual["id"] == f"{source['id']}:{expected.get('stable_code') or expected['code']}"
            all_ids.append(actual["id"])
            for field in (
                "code", "prompt", "experiment", "category", "role", "method", "scenario",
                "deliverable", "gold_standard", "metrics", "risk", "priority",
            ):
                assert actual[field] == expected.get(field), (source["id"], expected["code"], field)
            assert actual["skills"] == (expected.get("skills") or [])
    # Shared short codes across curated/full/common banks are separate stable
    # references, not collisions or duplicate inserts in a single bank.
    assert len(all_ids) == len(set(all_ids)) == 82
    full = client.get("/api/model-lab/question-sets/pronoia-finance-comprehensive-v1").json()
    assert {item["code"] for item in full["questions"]} >= {"V02", "Q05", "X01", "X06"}
    special = client.get("/api/model-lab/question-sets/pronoia-special-capabilities-v1").json()
    evidence_graph = next(item for item in special["questions"] if item["code"] == "N01")
    assert "不进入共同主榜" in evidence_graph["metrics"]


def test_question_library_reseeding_never_rewrites_existing_or_custom_content(
    model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from app import db
    from app.model_lab import repository as repo
    from app.model_lab.question_bank import BUILTIN_SET_ID

    client = model_lab_env
    initial = client.get("/api/model-lab/question-sets").json()
    original_details = {
        item["id"]: client.get(f"/api/model-lab/question-sets/{item['id']}").json()
        for item in initial["items"]
    }
    monkeypatch.setattr(db, "now_iso", lambda: "2099-01-01T00:00:00+00:00")
    for _ in range(3):
        repo.seed_builtin_question_set()
        assert client.get("/api/model-lab/question-sets").json() == initial
    for bank_id, original in original_details.items():
        assert client.get(f"/api/model-lab/question-sets/{bank_id}").json() == original

    created = client.post("/api/model-lab/question-sets", json={
        "name": "我的专题题集", "description": "保留用户创建的内容", "version": "1",
        "questions": [{"code": "V01", "prompt": "这是自定义问题，不应被同编码内置题覆盖。"}],
    })
    assert created.status_code == 201, created.text
    custom = created.json()
    # A future built-in accidentally claiming a user-owned id must not adopt
    # that bank or inject questions into it.
    monkeypatch.setattr(repo, "BUILTIN_QUESTION_SETS", [*repo.BUILTIN_QUESTION_SETS, {
        "id": custom["id"], "name": "不应覆盖", "version": "1",
        "questions": [{"code": "NEW", "prompt": "不应插入用户题集"}],
    }])
    with db._lock:
        conn = db._get_conn()
        conn.execute("UPDATE ml_question_sets SET name=? WHERE id=?", ("已有核心题集名称", BUILTIN_SET_ID))
        conn.execute(
            "UPDATE ml_questions SET prompt=? WHERE id=?",
            ("已有问题内容保留", f"{BUILTIN_SET_ID}:V03"),
        )
        conn.commit()
    repo.seed_builtin_question_set()
    assert client.get(f"/api/model-lab/question-sets/{custom['id']}").json() == custom
    core = client.get(f"/api/model-lab/question-sets/{BUILTIN_SET_ID}").json()
    assert core["name"] == "已有核心题集名称"
    assert core["questions"][0]["prompt"] == "已有问题内容保留"
    assert core["question_count"] == 17


@pytest.mark.parametrize(("bank_id", "next_stable_code", "expected_count"), [
    ("pronoia-core-finance-v1", "V03", 17),
    ("pronoia-finance-comprehensive-v1", "V02", 41),
])
def test_retiring_original_v01_preserves_frozen_history_and_stable_ids(
    model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch,
    bank_id: str, next_stable_code: str, expected_count: int,
):
    import copy
    from app import db
    from app.model_lab import repository as repo

    # Reconstruct the previous bundled version in an isolated database, then
    # create a persisted answer before applying the new bank revision.
    current = copy.deepcopy(repo.BUILTIN_QUESTION_SETS)
    legacy = copy.deepcopy(current)
    old_prompt = "旧 V01：最近5年600519的ROIC、自由现金流与估值。"
    for bank in legacy:
        if not bank.get("revision"):
            continue
        bank.pop("revision")
        bank["description"] = bank.pop("previous_description")
        bank["questions"].insert(0, {"code": "V01", "prompt": old_prompt})
        for question in bank["questions"]:
            question["code"] = question.pop("stable_code", question["code"])
    client = model_lab_env
    with monkeypatch.context() as previous:
        previous.setattr(repo, "BUILTIN_QUESTION_SETS", legacy)
        profile = _profile(client)
        response = client.post("/api/model-lab/batches", json={
            "name": "历史 V01 回答", "profile_ids": [profile["id"]], "auto_start": False,
            "prediction": {"enabled": False},
            "qa": {"enabled": True, "question_set_id": bank_id,
                   "question_ids": ["V01"], "variants": ["pronoia"], "scoring_mode": "manual"},
        })
        assert response.status_code == 201, response.text
        batch = response.json()
        task = batch["tasks"][0]
        result = repo.create_qa_result(
            batch_id=batch["id"], task_id=task["id"], profile_id=profile["id"],
            question_id=f"{bank_id}:V01", variant="pronoia", repeat_no=1,
        )
        repo.update_qa_result(result["id"], status="done", answer="历史完整回答",
                              final_score={"total": 92})
        frozen_task = repo.get_task(task["id"])
        frozen_answer = repo.get_qa_result(result["id"])
        with db._lock:
            conn = db._get_conn()
            conn.execute("UPDATE ml_questions SET prompt=? WHERE id=?",
                         ("用户编辑后的内容应保留", f"{bank_id}:{next_stable_code}"))
            conn.commit()

    revised = repo.get_question_set(bank_id)
    assert revised["question_count"] == expected_count
    value_questions = [q for q in revised["questions"] if q["code"].startswith("V")]
    assert [q["code"] for q in value_questions] == [f"V{i:02d}" for i in range(1, len(value_questions) + 1)]
    assert value_questions[0]["id"] == f"{bank_id}:{next_stable_code}"
    assert value_questions[0]["prompt"] == "用户编辑后的内容应保留"
    assert repo.selected_questions(bank_id, ["V01"])[0]["id"] == value_questions[0]["id"]
    assert repo.selected_questions(bank_id, [value_questions[0]["id"]]) == [value_questions[0]]
    assert repo.selected_questions(bank_id, [f"{bank_id}:V01"]) == []
    assert repo.get_task(task["id"]) == frozen_task
    assert repo.get_qa_result(result["id"]) == frozen_answer
    assert frozen_task["config"]["questions"][0]["code"] == "V01"
    assert frozen_task["config"]["questions"][0]["prompt"] == old_prompt
    with db._lock:
        old = dict(db._get_conn().execute("SELECT * FROM ml_questions WHERE id=?", (f"{bank_id}:V01",)).fetchone())
    assert old["is_archived"] == 1 and old["code"] == "V01" and old["prompt"] == old_prompt
    assert str(expected_count) in revised["description"]
    for _ in range(2):
        repo.seed_builtin_question_set()
        assert repo.get_question_set(bank_id) == revised
    common = repo.get_question_set("pronoia-common-comparison-v1")
    assert common["question_count"] == 18
    assert any(q["code"] == "C01" for q in common["questions"])


def test_question_schema_upgrade_keeps_legacy_records(model_lab_env: TestClient):
    from app import db
    from app.model_lab import repository as repo

    with db._lock:
        conn = db._get_conn()
        conn.execute("ALTER TABLE ml_questions DROP COLUMN display_code")
        conn.execute("ALTER TABLE ml_questions DROP COLUMN is_archived")
        conn.commit()
    db.init_db()
    bank = repo.get_question_set("pronoia-core-finance-v1")
    assert bank["question_count"] == 17
    assert bank["questions"][0]["code"] == "V01"
    assert bank["questions"][0]["id"].endswith(":V03")


@pytest.mark.parametrize(("bank_id", "code"), [
    ("pronoia-finance-comprehensive-v1", "V02"),
    ("pronoia-common-comparison-v1", "C01"),
    ("pronoia-special-capabilities-v1", "N01"),
])
def test_question_library_selection_freezes_only_selected_bank_questions(
    model_lab_env: TestClient, bank_id: str, code: str,
):
    client = model_lab_env
    profile = _profile(client)
    bank = client.get(f"/api/model-lab/question-sets/{bank_id}").json()
    selected = next(item for item in bank["questions"] if item["code"] == code)
    response = client.post("/api/model-lab/batches", json={
        "name": "从问题库选题", "profile_ids": [profile["id"]], "auto_start": False,
        "prediction": {"enabled": False},
        "qa": {
            "enabled": True, "question_set_id": bank_id,
            "question_ids": [selected["id"]], "variants": ["pronoia"], "scoring_mode": "manual",
        },
    })
    assert response.status_code == 201, response.text
    tasks = response.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "qa"
    assert tasks[0]["config"]["questions"] == [selected]
    assert tasks[0]["total_items"] == 1


def test_prediction_and_qa_run_independently_with_real_bt_lineage(
    model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from app import db
    from app.event_backtest import orchestrator as orch
    from app.model_lab import service

    client = model_lab_env
    profile = _profile(client)
    dataset = _dataset(client)
    question_set_id = client.get("/api/model-lab/question-sets").json()["items"][0]["id"]

    async def fake_answer(_profile_data, _question):
        return {
            "answer": "基于已知材料的正式测试答案。",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "tool_trace": [],
            "latency_ms": 4,
        }

    def fake_score(_judge, _question, _answer):
        return {
            "fact": 20, "evidence": 10, "method": 10, "reasoning": 8,
            "risk": 8, "usability": 4, "reproducibility": 4, "user_value": 4,
            "major_error": False, "total": 68,
            "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
            "latency_ms": 2,
        }

    def hold_prediction(run_id: str):
        db.update_bt_run_status(run_id, "running")
        return orch.BacktestStartResult(ok=True, run_id=run_id)

    monkeypatch.setattr(service, "_pronoia_answer", fake_answer)
    monkeypatch.setattr(service, "score_answer", fake_score)
    monkeypatch.setattr(orch, "start_bt_run", hold_prediction)

    response = client.post("/api/model-lab/batches", json={
        "name": "独立双轨正式评测",
        "profile_ids": [profile["id"]],
        "auto_start": True,
        "prediction": {"enabled": True, "dataset_id": dataset["id"], "runner": "team_full"},
        "qa": {
            "enabled": True,
            "question_set_id": question_set_id,
            "question_ids": ["V01"],
            "variants": ["pronoia"],
            "scoring_mode": "auto",
            "judge_profile_id": profile["id"],
        },
    })
    assert response.status_code == 201, response.text
    batch_id = response.json()["id"]

    # QA completes while prediction is deliberately held open. This guards the
    # product requirement that one track never gates visibility of the other.
    deadline = time.time() + 3
    current: dict[str, Any] = {}
    while time.time() < deadline:
        current = client.get(f"/api/model-lab/batches/{batch_id}").json()
        task_by_kind = {item["kind"]: item for item in current["tasks"]}
        if task_by_kind.get("qa", {}).get("status") == "done" and task_by_kind.get("prediction", {}).get("backtest_run_id"):
            break
        time.sleep(0.02)
    assert task_by_kind["qa"]["status"] == "done"
    assert task_by_kind["prediction"]["status"] == "running"
    run_id = task_by_kind["prediction"]["backtest_run_id"]
    run = db.get_bt_run(run_id)
    assert run["visibility"] == "arena_safe"
    assert run["config"]["model_lab_batch_id"] == batch_id
    assert run["config"]["model_profile_snapshot"]["id"] == profile["id"]
    assert run["config"]["model_profile_snapshot"]["model_id"] == profile["model_id"]
    assert run["config"]["demo"] is False

    db.update_bt_run_progress(run_id, done_events=1, total_events=1)
    db.update_bt_run_status(run_id, "done")
    finished = _wait_batch(client, batch_id)
    assert finished["status"] == "done"
    results = client.get(f"/api/model-lab/batches/{batch_id}/results").json()
    prediction = results["summary"]["prediction_runs"][0]
    assert prediction["bt_run_id"] == run_id
    assert prediction["arena_eligible"] is True
    assert prediction["demo"] is False
    assert results["summary"]["formal_results"] is True
    assert results["summary"]["qa_comparison"][0]["average_total"] == 68


def test_manual_scoring_calculates_authoritative_total(
    model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from app.model_lab import service

    client = model_lab_env
    profile = _profile(client)
    question_set_id = client.get("/api/model-lab/question-sets").json()["items"][0]["id"]

    async def fake_answer(_profile_data, _question):
        return {
            # A malicious provider may reflect the bearer value in content;
            # persistence and API serialization must still redact it.
            "answer": "等待人工复核：candidate-secret-must-not-leak",
            "usage": {"basis": "unavailable"},
            "tool_trace": [],
            "latency_ms": 1,
        }

    monkeypatch.setattr(service, "_pronoia_answer", fake_answer)
    response = client.post("/api/model-lab/batches", json={
        "name": "人工评分",
        "profile_ids": [profile["id"]],
        "auto_start": True,
        "prediction": {"enabled": False},
        "qa": {
            "enabled": True,
            "question_set_id": question_set_id,
            "question_ids": ["V01"],
            "variants": ["pronoia"],
            "scoring_mode": "manual",
        },
    })
    assert response.status_code == 201, response.text
    batch = _wait_batch(client, response.json()["id"])
    result_response = client.get(f"/api/model-lab/batches/{batch['id']}/results")
    assert "candidate-secret-must-not-leak" not in result_response.text
    results = result_response.json()
    answer = results["qa_results"][0]
    assert answer["status"] == "awaiting_manual"
    scored = client.post(f"/api/model-lab/qa-results/{answer['id']}/manual-score", json={
        "fact": 30, "evidence": 20, "method": 15, "reasoning": 10,
        "risk": 10, "usability": 5, "reproducibility": 5, "user_value": 5,
        "major_error": False, "rationale": "人工复核完成", "reviewer": "reviewer-1",
    })
    assert scored.status_code == 200, scored.text
    assert scored.json()["status"] == "done"
    assert scored.json()["final_score"]["total"] == 100
    assert scored.json()["final_score"]["source"] == "manual"


def test_partial_child_task_cannot_promote_batch_to_done(monkeypatch: pytest.MonkeyPatch):
    from app.model_lab import repository as repo
    from app.model_lab import service

    snapshots = iter([
        [{"id": "task-1", "status": "pending", "kind": "qa"}],
        [{"id": "task-1", "status": "partial", "kind": "qa"}],
    ])
    updates: list[tuple[str, str]] = []

    monkeypatch.setattr(repo, "list_tasks", lambda _batch_id: next(snapshots))
    monkeypatch.setattr(repo, "update_batch_status", lambda batch_id, status, **_kwargs: updates.append((batch_id, status)))

    async def finish_partial(_task, _cancel):
        return None

    monkeypatch.setattr(service, "_run_one_task", finish_partial)
    asyncio.run(service._run_batch_async("batch-partial", service.threading.Event()))
    assert updates == [("batch-partial", "partial")]


@pytest.mark.parametrize("blocked_stage", ["answer", "score"])
def test_cancelled_provider_sidecar_releases_batch_for_history_delete_before_call_returns(
    model_lab_env: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    blocked_stage: str,
):
    """Cancellation detaches a blocked provider without permitting late writes."""
    from app.model_lab import repository as repo
    from app.model_lab import service

    client = model_lab_env
    profile = _profile(client)
    question_set_id = client.get("/api/model-lab/question-sets").json()["items"][0]["id"]
    answer_entered = threading.Event()
    score_entered = threading.Event()
    answer_finished = threading.Event()
    score_finished = threading.Event()
    release = threading.Event()

    async def controlled_answer(_profile_data, _question):
        answer_entered.set()
        try:
            while blocked_stage == "answer" and not release.is_set():
                await asyncio.sleep(0.01)
            return {
                "answer": "取消竞态测试答案",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                "tool_trace": [],
                "latency_ms": 1,
            }
        finally:
            answer_finished.set()

    def controlled_score(_judge, _question, _answer):
        score_entered.set()
        try:
            if blocked_stage == "score":
                assert release.wait(timeout=3)
            return {"total": 80, "usage": {"total_tokens": 1}}
        finally:
            score_finished.set()

    monkeypatch.setattr(service, "_pronoia_answer", controlled_answer)
    monkeypatch.setattr(service, "score_answer", controlled_score)
    created = client.post("/api/model-lab/batches", json={
        "name": f"取消竞态-{blocked_stage}",
        "profile_ids": [profile["id"]],
        "auto_start": True,
        "prediction": {"enabled": False},
        "qa": {
            "enabled": True,
            "question_set_id": question_set_id,
            "question_ids": ["V01"],
            "variants": ["pronoia"],
            "scoring_mode": "auto",
            "judge_profile_id": profile["id"],
        },
    })
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    gate = {
        "answer": answer_entered,
        "score": score_entered,
    }[blocked_stage]
    assert gate.wait(timeout=3), f"worker never reached {blocked_stage}"

    sidecar_finished = {
        "answer": answer_finished,
        "score": score_finished,
    }[blocked_stage]
    try:
        started = time.monotonic()
        cancelled = client.post(f"/api/model-lab/batches/{batch_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        _wait_worker_exit(batch_id, timeout=0.8)
        assert time.monotonic() - started < 0.8

        # The provider/score blocker has deliberately not been released yet.
        # Only its isolated sidecar may remain; the durable batch worker must no
        # longer prevent an immediate all-or-nothing history deletion.
        deleted = client.post("/api/bt/history/delete", json={
            "run_ids": [],
            "batch_ids": [batch_id],
        })
        assert deleted.status_code == 200, deleted.text
        assert repo.get_batch(batch_id) is None
        assert repo.list_qa_results(batch_id=batch_id) == []
    finally:
        release.set()
        assert sidecar_finished.wait(timeout=3), f"sidecar never finished: {blocked_stage}"

    # A late synchronous score result (or async cancellation cleanup) has no
    # repository handle and cannot recreate the deleted batch or QA rows.
    assert repo.get_batch(batch_id) is None
    assert repo.list_qa_results(batch_id=batch_id) == []


def test_async_raw_answer_sidecar_does_not_pin_caller_on_sync_transport(
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancelling an async raw wrapper need not await its to_thread transport."""
    from app.model_lab import service

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_transport() -> dict[str, Any]:
        entered.set()
        try:
            assert release.wait(timeout=3)
            return {"answer": "late raw answer"}
        finally:
            finished.set()

    async def controlled_raw_answer(_profile_data, _question):
        return await asyncio.to_thread(blocked_transport)

    monkeypatch.setattr(service, "_raw_answer", controlled_raw_answer)

    async def exercise() -> None:
        cancel = threading.Event()
        task = asyncio.create_task(service._run_provider_sidecar(
            lambda: service._raw_answer({}, {}),
            cancel,
            label="raw-cancel-test",
        ))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        cancel.set()
        with pytest.raises(service._SidecarCallCancelled):
            await asyncio.wait_for(task, timeout=0.5)

    try:
        asyncio.run(exercise())
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(timeout=3)


def test_cancel_serializes_prediction_run_creation_link_and_history_delete(
    model_lab_env: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancellation cannot leave a Run inserted after its batch was deleted."""
    from app import db
    from app.event_backtest import orchestrator as orch
    from app.model_lab import repository as repo
    from app.routes import backtest as backtest_route

    client = model_lab_env
    profile = _profile(client)
    dataset = _dataset(client)
    created = client.post("/api/model-lab/batches", json={
        "name": "创建关联取消竞态",
        "profile_ids": [profile["id"]],
        "auto_start": False,
        "prediction": {
            "enabled": True,
            "dataset_id": dataset["id"],
            "runner": "team_full",
        },
        "qa": {"enabled": False},
    })
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]

    create_entered = threading.Event()
    release_create = threading.Event()
    cancel_done = threading.Event()
    responses: dict[str, Any] = {}
    original_create = backtest_route.create_run_from_trusted_request

    def held_create(request):
        create_entered.set()
        assert release_create.wait(timeout=3)
        return original_create(request)

    def fake_start(run_id: str):
        db.update_bt_run_status(run_id, "running")
        return orch.BacktestStartResult(ok=True, run_id=run_id)

    def cancel() -> None:
        responses["cancel"] = client.post(f"/api/model-lab/batches/{batch_id}/cancel")
        cancel_done.set()

    monkeypatch.setattr(backtest_route, "create_run_from_trusted_request", held_create)
    monkeypatch.setattr(orch, "start_bt_run", fake_start)
    started = client.post(f"/api/model-lab/batches/{batch_id}/start")
    assert started.status_code == 200, started.text
    assert create_entered.wait(timeout=3)

    cancel_thread = threading.Thread(target=cancel)
    cancel_thread.start()
    try:
        # resolve_or_create_run holds the history relationship lock across both
        # publication and task linking, so cancellation cannot pass between them.
        assert not cancel_done.wait(timeout=0.1)
    finally:
        release_create.set()
    cancel_thread.join(timeout=3)
    assert not cancel_thread.is_alive()
    cancelled = responses["cancel"]
    assert cancelled.status_code == 200, cancelled.text
    _wait_worker_exit(batch_id, timeout=0.8)

    task = repo.list_tasks(batch_id)[0]
    run_id = str(task["backtest_run_id"] or "")
    assert run_id
    assert db.get_bt_run(run_id)["status"] == "cancelled"
    deleted = client.post("/api/bt/history/delete", json={
        "run_ids": [],
        "batch_ids": [batch_id],
    })
    assert deleted.status_code == 200, deleted.text
    assert repo.get_batch(batch_id) is None
    assert db.get_bt_run(run_id) is None


def test_startup_recovery_reuses_qa_checkpoints_and_formal_run(
    model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """Restart recovery is idempotent across QA slots and prediction lineage."""
    from app import db
    from app.event_backtest import orchestrator as orch
    from app.model_lab import repository as repo
    from app.model_lab import service

    client = model_lab_env
    profile = _profile(client)
    dataset = _dataset(client)
    question_set_id = client.get("/api/model-lab/question-sets").json()["items"][0]["id"]
    created = client.post("/api/model-lab/batches", json={
        "name": "进程恢复检查点",
        "profile_ids": [profile["id"]],
        "auto_start": False,
        "prediction": {"enabled": True, "dataset_id": dataset["id"], "runner": "team_full"},
        "qa": {
            "enabled": True,
            "question_set_id": question_set_id,
            "question_ids": ["V01", "V02"],
            "variants": ["pronoia"],
            "scoring_mode": "auto",
            "judge_profile_id": profile["id"],
        },
    })
    assert created.status_code == 201, created.text
    batch = created.json()
    batch_id = batch["id"]
    task_by_kind = {task["kind"]: task for task in batch["tasks"]}
    prediction_task = task_by_kind["prediction"]
    qa_task = task_by_kind["qa"]

    # Simulate the crash window after a formal run commits but before its id is
    # linked back to ml_tasks. Recovery locates it by frozen task lineage.
    from app.schemas import CreateBacktestRunRequest
    from app.routes import backtest
    formal = backtest.create_run_from_trusted_request(CreateBacktestRunRequest(**{
        "name": "已完成正式 Run",
        "runner": "team_full",
        "dataset_id": dataset["id"],
        "visibility": "arena_safe",
        "config": {
            "model_lab_task_id": prediction_task["id"],
            "model_lab_batch_id": batch_id,
            "evaluation_source": "model_lab",
        },
    }))
    run_id = formal["id"]
    db.update_bt_run_progress(run_id, done_events=1, total_events=1)
    db.update_bt_run_status(run_id, "done")

    questions = list(qa_task["config"]["questions"])
    first = repo.get_or_create_qa_result(
        batch_id=batch_id, task_id=qa_task["id"], profile_id=profile["id"],
        question_id=questions[0]["id"], variant="pronoia", repeat_no=1,
    )
    repo.update_qa_result(
        first["id"], status="done", answer="已经完成，不应重算",
        final_score={"total": 90}, auto_score={"total": 90},
    )
    second = repo.get_or_create_qa_result(
        batch_id=batch_id, task_id=qa_task["id"], profile_id=profile["id"],
        question_id=questions[1]["id"], variant="pronoia", repeat_no=1,
    )
    repo.update_qa_result(
        second["id"], status="running", answer="已持久化的候选答案",
        usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )
    repo.update_task(prediction_task["id"], status="running")
    repo.update_task(qa_task["id"], status="running", done_items=1)
    repo.update_batch_status(batch_id, "running")

    async def must_not_regenerate(_profile_data, _question):
        raise AssertionError("durable QA answers must be reused")

    def fake_score(_judge, _question, answer):
        assert answer == "已持久化的候选答案"
        return {"total": 75, "usage": {"total_tokens": 1}}

    def must_not_restart_done_run(_run_id: str):
        raise AssertionError("completed formal bt_run must be reused")

    monkeypatch.setattr(service, "_pronoia_answer", must_not_regenerate)
    monkeypatch.setattr(service, "score_answer", fake_score)
    monkeypatch.setattr(orch, "start_bt_run", must_not_restart_done_run)

    assert service.recover_interrupted_batches() == [batch_id]
    finished = _wait_batch(client, batch_id)
    _wait_worker_exit(batch_id)
    assert finished["status"] == "done"
    assert len(db.list_bt_runs()) == 1
    recovered_prediction = next(task for task in finished["tasks"] if task["kind"] == "prediction")
    assert recovered_prediction["backtest_run_id"] == run_id
    qa_results = client.get(f"/api/model-lab/batches/{batch_id}/results").json()["qa_results"]
    assert {item["answer"] for item in qa_results} == {
        "已经完成，不应重算", "已持久化的候选答案",
    }
    assert {item["status"] for item in qa_results} == {"done"}


def test_partial_batch_can_be_resumed(model_lab_env: TestClient):
    from app.model_lab import repository as repo
    from app.model_lab import service

    client = model_lab_env
    profile = _profile(client)
    question_set_id = client.get("/api/model-lab/question-sets").json()["items"][0]["id"]
    created = client.post("/api/model-lab/batches", json={
        "name": "partial 可重入",
        "profile_ids": [profile["id"]],
        "auto_start": False,
        "dry_run": True,
        "prediction": {"enabled": False},
        "qa": {
            "enabled": True, "question_set_id": question_set_id,
            "question_ids": ["V01"], "variants": ["pronoia"], "scoring_mode": "manual",
        },
    })
    batch_id = created.json()["id"]
    task_id = created.json()["tasks"][0]["id"]
    repo.update_task(task_id, status="done", done_items=1, total_items=1)
    repo.update_batch_status(batch_id, "partial")
    ok, _message = service.start_batch(batch_id)
    assert ok is True
    assert _wait_batch(client, batch_id)["status"] == "done"
    _wait_worker_exit(batch_id)


def test_startup_recovery_respects_pending_auto_start_intent(model_lab_env: TestClient):
    import json

    from app import db
    from app.model_lab import repository as repo
    from app.model_lab import service

    client = model_lab_env
    profile = _profile(client)
    question_set_id = client.get("/api/model-lab/question-sets").json()["items"][0]["id"]

    def pending_batch(name: str) -> str:
        response = client.post("/api/model-lab/batches", json={
            "name": name, "profile_ids": [profile["id"]], "auto_start": False, "dry_run": True,
            "prediction": {"enabled": False},
            "qa": {
                "enabled": True, "question_set_id": question_set_id,
                "question_ids": ["V01"], "variants": ["pronoia"], "scoring_mode": "manual",
            },
        })
        assert response.status_code == 201, response.text
        return response.json()["id"]

    intentional_id = pending_batch("用户主动保存待启动")
    interrupted_id = pending_batch("模拟 create-to-start 中断")
    # Simulate the durable create commit from an auto-start request before its
    # synchronous start_batch call got CPU time.
    with db._lock:
        conn = db._get_conn()
        scoring = {"dry_run": True, "demo": True, "auto_start": True}
        conn.execute(
            "UPDATE ml_batches SET scoring_config_json=? WHERE id=?",
            (json.dumps(scoring), interrupted_id),
        )
        conn.commit()

    assert repo.list_interrupted_batch_ids() == [interrupted_id]
    assert service.recover_interrupted_batches() == [interrupted_id]
    assert _wait_batch(client, interrupted_id)["status"] == "done"
    assert client.get(f"/api/model-lab/batches/{intentional_id}").json()["status"] == "pending"


def test_historical_results_use_frozen_profile_and_judge_provenance(
    model_lab_env: TestClient,
):
    client = model_lab_env
    profile = _profile(client, name="历史候选")
    dataset = _dataset(client)
    question_set_id = client.get("/api/model-lab/question-sets").json()["items"][0]["id"]
    created = client.post("/api/model-lab/batches", json={
        "name": "历史溯源不漂移",
        "profile_ids": [profile["id"]],
        "auto_start": True,
        "dry_run": True,
        "prediction": {"enabled": True, "dataset_id": dataset["id"], "runner": "team_full"},
        "qa": {
            "enabled": True, "question_set_id": question_set_id,
            "question_ids": ["V01"], "variants": ["pronoia"], "scoring_mode": "auto",
            "judge_profile_id": profile["id"],
        },
    })
    batch_id = created.json()["id"]
    assert _wait_batch(client, batch_id)["status"] == "done"

    renamed = client.patch(f"/api/model-lab/profiles/{profile['id']}", json={
        "name": "后来改名", "model_id": "candidate-v99",
    })
    assert renamed.status_code == 200, renamed.text
    results = client.get(f"/api/model-lab/batches/{batch_id}/results").json()
    comparison = results["summary"]["qa_comparison"][0]
    prediction = results["summary"]["prediction_runs"][0]
    answer = results["qa_results"][0]
    for item in (comparison, prediction, answer):
        assert item["profile_name"] == "历史候选"
        assert item["model_id"] == "candidate-v1"
        assert item["provider"] == "openai-compatible"
    assert comparison["judge_profile_name"] == "历史候选"
    assert comparison["judge_model_id"] == "candidate-v1"
    assert answer["judge_profile_name"] == "历史候选"
    assert answer["judge_model_id"] == "candidate-v1"


def test_process_global_provider_concurrency_cap(
    model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from app.model_lab import service

    client = model_lab_env
    first = _profile(client, name="并发候选 A")
    second = _profile(client, name="并发候选 B")
    question_set_id = client.get("/api/model-lab/question-sets").json()["items"][0]["id"]
    monkeypatch.setattr(service, "_GLOBAL_CALL_SLOTS", threading.BoundedSemaphore(1))
    lock = threading.Lock()
    active = 0
    maximum = 0

    async def measured_answer(_profile_data, _question):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        await asyncio.sleep(0.06)
        with lock:
            active -= 1
        return {"answer": "并发上限", "usage": {"total_tokens": 1}, "tool_trace": [], "latency_ms": 1}

    monkeypatch.setattr(service, "_pronoia_answer", measured_answer)
    created = client.post("/api/model-lab/batches", json={
        "name": "进程全局并发",
        "profile_ids": [first["id"], second["id"]],
        "auto_start": True,
        "prediction": {"enabled": False},
        "qa": {
            "enabled": True, "question_set_id": question_set_id,
            "question_ids": ["V01"], "variants": ["pronoia"], "scoring_mode": "manual",
            "concurrency": 2,
        },
    })
    assert created.status_code == 201, created.text
    assert _wait_batch(client, created.json()["id"])["status"] == "done"
    assert maximum == 1


def test_auto_start_batch_creation_serializes_history_delete(
    model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from app.model_lab import repository as repo
    from app.model_lab import service

    client = model_lab_env
    profile = _profile(client, name="创建启动竞态")
    dataset = _dataset(client)
    start_entered = threading.Event()
    allow_start = threading.Event()
    delete_entered = threading.Event()
    delete_done = threading.Event()
    ids: dict[str, str] = {}
    responses: dict[str, Any] = {}

    def controlled_start(batch_id: str) -> tuple[bool, str]:
        ids["batch_id"] = batch_id
        start_entered.set()
        assert allow_start.wait(3)
        assert repo.update_batch_status(batch_id, "running") is not None
        return True, "评测批次已启动"

    original_delete = service.delete_history_records

    def tracked_delete(run_ids: list[str], batch_ids: list[str]):
        delete_entered.set()
        return original_delete(run_ids, batch_ids)

    monkeypatch.setattr(service, "start_batch", controlled_start)
    monkeypatch.setattr(service, "delete_history_records", tracked_delete)

    def create_batch() -> None:
        responses["create"] = client.post("/api/model-lab/batches", json={
            "name": "创建后立即启动",
            "profile_ids": [profile["id"]],
            "auto_start": True,
            "dry_run": True,
            "prediction": {
                "enabled": True,
                "dataset_id": dataset["id"],
                "runner": "team_full",
            },
            "qa": {"enabled": False},
        })

    def delete_batch() -> None:
        responses["delete"] = client.post("/api/bt/history/delete", json={
            "run_ids": [],
            "batch_ids": [ids["batch_id"]],
        })
        delete_done.set()

    creator = threading.Thread(target=create_batch)
    deleter = threading.Thread(target=delete_batch)
    creator.start()
    try:
        assert start_entered.wait(3)
        deleter.start()
        assert delete_entered.wait(3)
        assert not delete_done.wait(0.1), "delete slipped into the create-to-start window"
    finally:
        allow_start.set()
    creator.join(3)
    deleter.join(3)
    assert not creator.is_alive()
    assert not deleter.is_alive()

    created = responses["create"]
    deleted = responses["delete"]
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "running"
    assert deleted.status_code == 409, deleted.text
    assert repo.get_batch(ids["batch_id"])["status"] == "running"


def test_auto_start_batch_creation_surfaces_start_failure(
    model_lab_env: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from app.model_lab import repository as repo
    from app.model_lab import service

    client = model_lab_env
    profile = _profile(client, name="启动失败候选")
    dataset = _dataset(client)
    monkeypatch.setattr(service, "start_batch", lambda _batch_id: (False, "controlled start failure"))

    response = client.post("/api/model-lab/batches", json={
        "name": "不得虚假返回已创建",
        "profile_ids": [profile["id"]],
        "auto_start": True,
        "dry_run": True,
        "prediction": {
            "enabled": True,
            "dataset_id": dataset["id"],
            "runner": "team_full",
        },
        "qa": {"enabled": False},
    })

    assert response.status_code == 409, response.text
    assert "controlled start failure" in response.text
    batches = repo.list_batches()
    assert len(batches) == 1
    assert batches[0]["status"] == "pending"
    assert batches[0]["scoring"]["auto_start"] is True
