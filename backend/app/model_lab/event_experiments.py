"""Event prediction plus independently executable optional QA sidecars."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import config, db
from ..model_endpoint_security import allow_private_model_endpoints, redact_sensitive_text, validate_registered_model_endpoint
from ..schemas import BacktestRunResponse, CreateBacktestRunRequest
from . import repository as repo, service
from .platform_default import PLATFORM_DEFAULT_SELECTOR, environment_profile, is_platform_profile_id
from .providers import profile_snapshot
from .schemas import QATaskConfig

router = APIRouter(prefix="/api", tags=["event-experiments"])


class EventQAConfig(QATaskConfig):
    candidate_profile_id: str | None = Field(None, max_length=120)

    @model_validator(mode="before")
    @classmethod
    def default_judge(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("enabled") and value.get("scoring_mode", "auto") in {"auto", "mixed"}:
            value = dict(value)
            value["judge_profile_id"] = value.get("judge_profile_id") or PLATFORM_DEFAULT_SELECTOR
        return value

    @model_validator(mode="after")
    def distinct_variants(self) -> "EventQAConfig":
        if len(self.variants) != len(set(self.variants)):
            raise ValueError("问答路径不能重复")
        return self


class EventExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prediction: CreateBacktestRunRequest
    prediction_profile_id: str | None = Field(None, max_length=120)
    qa: EventQAConfig | None = None
    auto_start: bool = True


class EventExperimentResponse(BaseModel):
    run: BacktestRunResponse
    qa_batch_id: str | None = None
    started: bool = False
    start_errors: dict[str, str] = Field(default_factory=dict)


def _resolve_profile(selector: str | None, *, label: str) -> dict[str, Any]:
    if not selector or selector == PLATFORM_DEFAULT_SELECTOR:
        selected = repo.get_default_profile(public=False) or environment_profile()
    else:
        if is_platform_profile_id(selector):
            raise HTTPException(status_code=422, detail="请使用平台默认选项，不能直接指定历史平台身份")
        selected = repo.get_profile(selector, public=False)
        if not selected:
            raise HTTPException(status_code=404, detail=f"{label}模型配置不存在")
    if not selected.get("is_active"):
        raise HTTPException(status_code=409, detail=f"{label}模型已停用")
    if not selected.get("secret_configured"):
        raise HTTPException(status_code=409, detail=f"{label}模型的服务端 API 配置未完成")
    try:
        validate_registered_model_endpoint(selected.get("base_url"), label=f"{label}模型 API")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return selected


def _resolve_questions(qa: EventQAConfig) -> list[dict[str, Any]]:
    bank = repo.get_question_set(str(qa.question_set_id or ""))
    if not bank:
        raise HTTPException(status_code=404, detail="问答题库不存在")
    # Empty selection must never silently fan out to every question in a bank.
    if not qa.question_ids:
        raise HTTPException(status_code=422, detail="问答测试至少明确选择一道题")
    questions = repo.selected_questions(str(qa.question_set_id), qa.question_ids)
    valid = {str(value) for question in questions for value in (question["id"], question["code"])}
    missing = [value for value in qa.question_ids if value not in valid]
    if missing:
        raise HTTPException(status_code=422, detail={"message": "部分题目不存在", "items": missing})
    return questions


def _safe_start_error(error: Any) -> str:
    from ..routes.model_lab import _public
    return str(_public(redact_sensitive_text(str(error), config.LLM_API_KEY)))[:800]


def start_qa_for_run(run_id: str) -> tuple[bool, str]:
    """Start only an intentionally pending sidecar; never duplicate or retry it."""
    link = repo.get_event_experiment(run_id)
    batch_id = str((link or {}).get("qa_batch_id") or "")
    if not batch_id:
        return True, "未启用问答"
    batch = repo.get_batch(batch_id)
    if not batch or batch.get("status") != "pending":
        return True, "问答任务已启动或已结束"
    try:
        return service.start_batch(batch_id)
    except Exception as exc:
        message = _safe_start_error(exc)
        repo.update_batch_status(batch_id, "failed", error_msg=message)
        return False, message


@router.get("/model-lab/event-capabilities")
def event_capabilities() -> dict[str, Any]:
    selected = repo.get_default_profile(public=False) or environment_profile()
    available = bool(selected.get("is_active") and selected.get("secret_configured"))
    try:
        validate_registered_model_endpoint(selected.get("base_url"), label="平台默认模型 API")
    except ValueError:
        available = False
    return {
        "platform_default": {
            "available": available,
            "name": str(selected.get("name") or "Pronoia 平台默认"),
            "model_id": selected.get("model_id") or None,
        },
        "qa_variants": ["pronoia", "raw"],
        "scoring_modes": ["auto", "manual", "mixed"],
        "allow_private_model_endpoints": allow_private_model_endpoints(),
    }


@router.post("/bt/event-experiments", response_model=EventExperimentResponse, status_code=201)
def create_event_experiment(payload: EventExperimentCreate) -> dict[str, Any]:
    from ..event_backtest import orchestrator as orch
    from ..event_backtest.strategy_registry import legacy_runner_for, normalize_strategy_spec
    from ..routes import backtest
    from ..routes.model_lab import _public

    request = payload.prediction.model_copy(deep=True)
    reserved_fields = {"model_profile_snapshot", "model_profile_id", "model_lab_batch_id", "model_lab_task_id", "event_qa_batch_id", "evaluation_role", "evaluation_source", "prediction_targets"}
    if reserved_fields.intersection(request.config):
        raise HTTPException(status_code=422, detail="模型快照与实验关联由服务端生成，请只传 prediction_profile_id 或 saved_model_id")
    selected_setup = None
    profile_selector = payload.prediction_profile_id
    if request.config.get("saved_model_id"):
        from ..model_registry import bind_test_request, execution_setup
        try:
            selected_setup = execution_setup(str(request.config["saved_model_id"]))
            if selected_setup["category"] != "event":
                raise ValueError("请选择事件模型；量化模型请在量化测试中运行")
            request = bind_test_request(request)
            profile_selector = selected_setup.get("profile_id")
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=redact_sensitive_text(str(exc))) from exc
    try:
        spec = normalize_strategy_spec(
            request.strategy_spec.model_dump() if request.strategy_spec else None,
            legacy_runner=request.runner, legacy_strategy_type=request.strategy_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if spec.get("type") != "event":
        raise HTTPException(status_code=422, detail="事件实验接口只接受事件模型，量化模型不提供问答测试")
    runner = legacy_runner_for(spec)
    profiles: list[dict[str, Any]] = []
    prediction_profile = None
    platform_profile = None
    if runner in {"team_prompt", "team_full"}:
        if not selected_setup and profile_selector not in {None, "", PLATFORM_DEFAULT_SELECTOR}:
            raise HTTPException(status_code=422, detail="事件测试中的 Pronoia 使用统一平台基模；独立大模型请选择直接模型接口，基模对比请进入 Pronoia 基模评测")
        if runner != "team_full":
            raise HTTPException(status_code=422, detail="Pronoia 选手使用统一多 Agent 流程；独立大模型请使用 raw_model")
        platform_profile = _resolve_profile(profile_selector if selected_setup else None, label="Pronoia")
        prediction_profile = platform_profile
        request.config["evaluation_role"] = "pronoia"
    elif runner == "raw_model":
        if not profile_selector or profile_selector == PLATFORM_DEFAULT_SELECTOR:
            raise HTTPException(status_code=422, detail="请选择独立大模型的 API 连接")
        prediction_profile = _resolve_profile(profile_selector, label="独立大模型")
        request.config["evaluation_role"] = "raw_model"
    elif profile_selector:
        raise HTTPException(status_code=422, detail="第三方预测服务与导入结果不使用聊天模型连接")
    if prediction_profile:
        profiles.append(prediction_profile)
        request.config["model_profile_snapshot"] = profile_snapshot(prediction_profile)
        request.config["model_profile_id"] = prediction_profile["id"]
        request.model_version = str(prediction_profile["model_id"])
    request.config["prediction_targets"] = ["event_direction", "asset_return_pct"]
    request.config["evaluation_source"] = "event_model"
    request.config.setdefault("evaluation_role", "external_service" if runner == "event_external_http" else "imported_predictions")

    qa = payload.qa or EventQAConfig()
    qa_batch = None
    if qa.enabled:
        questions = _resolve_questions(qa)
        candidates: dict[str, dict[str, Any]] = {}
        if "pronoia" in qa.variants:
            platform_profile = platform_profile or _resolve_profile(None, label="Pronoia 问答")
            candidates["pronoia"] = platform_profile
        if "raw" in qa.variants:
            if not qa.candidate_profile_id or qa.candidate_profile_id == PLATFORM_DEFAULT_SELECTOR:
                raise HTTPException(status_code=422, detail="直接模型问答需要明确选择独立模型连接")
            candidates["raw"] = _resolve_profile(qa.candidate_profile_id, label="直接问答")
        judge = _resolve_profile(qa.judge_profile_id, label="评分") if qa.scoring_mode in {"auto", "mixed"} else None
        profiles.extend([*candidates.values(), *([judge] if judge else [])])
        qa_config = qa.model_dump(exclude={"candidate_profile_id"})
        qa_config["judge_profile_id"] = judge["id"] if judge else None
        qa_batch = {
            "name": f"{request.name[:105]} · 问答测试",
            "profile_ids": list(dict.fromkeys(str(candidate["id"]) for candidate in candidates.values())),
            "dataset_id": request.dataset_id,
            "dataset_version": request.dataset_version,
            "question_set_id": qa.question_set_id,
            "prediction": {"enabled": False},
            "qa": qa_config,
            "scoring": {"dry_run": False, "demo": False, "auto_start": payload.auto_start, "source": "event_experiment"},
            "task_specs": [{
                "kind": "qa", "profile_id": candidate["id"],
                "total_items": len(questions) * qa.repeats,
                "config": {**qa_config, "variants": [variant], "profile_snapshot": profile_snapshot(candidate),
                           "judge_profile_snapshot": profile_snapshot(judge) if judge else None,
                           "questions": questions, "dry_run": False},
            } for variant, candidate in candidates.items()],
        }

    # All optional-question/model checks precede even prediction preparation;
    # no Run, private identity, or batch is inserted until every input passes.
    prepared = backtest.prepare_run(request)
    if qa_batch:
        qa_batch["dataset_version"] = prepared.get("dataset_version")
    # A history-delete request must not observe the committed pending rows in the
    # narrow window before auto-start registers their workers.  Keep publication,
    # startup and the final read under the same outer lock used by history delete.
    with db.HISTORY_RELATION_LOCK:
        run, batch_id = repo.create_event_experiment(
            prepared, profiles=profiles, qa_batch=qa_batch, auto_start=payload.auto_start,
        )
        errors: dict[str, str] = {}
        prediction_started = False
        if payload.auto_start:
            try:
                Path(prepared["ckpt_dir"]).mkdir(parents=True, exist_ok=True)
                started = orch.start_bt_run(str(run["id"]))
                prediction_started = bool(started.ok)
                if not started.ok:
                    errors["prediction"] = _safe_start_error(started.error or "预测启动失败")
            except Exception as exc:
                errors["prediction"] = _safe_start_error(exc)
            if errors.get("prediction"):
                db.update_bt_run_status(str(run["id"]), "failed", error_msg=errors["prediction"])
            if batch_id:
                qa_ok, qa_message = start_qa_for_run(str(run["id"]))
                if not qa_ok:
                    errors["qa"] = _safe_start_error(qa_message)
        current = db.get_bt_run(str(run["id"]))
        if not current:
            raise HTTPException(status_code=500, detail="事件实验创建后未能读取持久化记录")
        current = backtest._ensure_protocol_hash(current)
        current = backtest._sanitize_arena_safe_run_response(current)
        return {"run": _public(backtest._share_safe_payload(current)),
                "qa_batch_id": batch_id, "started": prediction_started, "start_errors": errors}


@router.get("/model-lab/backtest-runs/{run_id}/results")
def event_experiment_results(run_id: str) -> dict[str, Any]:
    from ..routes.model_lab import _public
    run = db.get_bt_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="回测实验不存在")
    link = repo.get_event_experiment(run_id)
    run_config = run.get("config") or {}
    batch_id = str((link or {}).get("qa_batch_id") or run_config.get("event_qa_batch_id") or run_config.get("model_lab_batch_id") or "")
    results = service.batch_results(batch_id) if batch_id else None
    enabled = bool(results and any(task.get("kind") == "qa" for task in results["batch"].get("tasks") or []))
    return _public({"enabled": enabled, "batch_id": batch_id or None, "results": results if enabled else None})


def recover_event_predictions() -> list[str]:
    """Repair the durable create/start gap; never restart paused/terminal runs."""
    from ..event_backtest import orchestrator as orch
    restarted = []
    for run_id in repo.interrupted_event_run_ids():
        try:
            result = orch.start_bt_run(run_id)
            if result.ok:
                restarted.append(run_id)
            else:
                db.update_bt_run_status(run_id, "failed", error_msg=_safe_start_error(result.error or "预测恢复失败"))
        except Exception as exc:
            db.update_bt_run_status(run_id, "failed", error_msg=_safe_start_error(exc))
    return restarted
