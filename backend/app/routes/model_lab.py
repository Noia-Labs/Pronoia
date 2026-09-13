"""Pronoia Model Lab API: model profiles, question banks and eval batches."""
from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, status

from .. import db
from ..model_endpoint_security import validate_registered_model_endpoint
from ..model_lab import repository as repo
from ..model_lab import service
from ..model_lab.providers import ModelProviderError, profile_snapshot, resolve_profile_secret, validate_profile
from ..model_lab.secret_store import (
    ModelSecretError,
    discard_secret,
    resolve_secret_reference,
    store_secret,
)
from ..model_lab.schemas import (
    EvaluationBatchCreate,
    ManualScoreRequest,
    ModelProfileCreate,
    ModelProfileUpdate,
    QuestionSetCreate,
)


router = APIRouter(prefix="/api/model-lab", tags=["model-lab"])


def _public(value: Any, _secret_values: tuple[str, ...] | None = None) -> Any:
    """Recursively remove credential material and server-side secret refs."""
    if _secret_values is None:
        values: list[str] = []
        from .. import config
        if config.LLM_API_KEY:
            values.append(str(config.LLM_API_KEY))
        def add_secret(reference: Any) -> None:
            try:
                secret = resolve_secret_reference(str(reference or ""))
                if secret and secret not in values:
                    values.append(secret)
            except (ModelSecretError, ValueError):
                pass

        def collect_snapshot_secrets(item: Any) -> None:
            if isinstance(item, Mapping):
                if item.get("secret_env_ref"):
                    add_secret(item["secret_env_ref"])
                for child in item.values():
                    collect_snapshot_secrets(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    collect_snapshot_secrets(child)

        # A rotated connection can still have older keys in frozen snapshots.
        collect_snapshot_secrets(value)
        try:
            for profile in repo.list_profiles(public=False):
                add_secret(profile.get("secret_env_ref"))
        except (sqlite3.Error, RuntimeError):
            pass
        _secret_values = tuple(values)
    if isinstance(value, Mapping):
        return {
            str(key): _public(item, _secret_values)
            for key, item in value.items()
            if str(key).lower() not in {"secret_env_ref", "api_key", "authorization"}
        }
    if isinstance(value, list):
        return [_public(item, _secret_values) for item in value]
    if isinstance(value, tuple):
        return [_public(item, _secret_values) for item in value]
    if isinstance(value, str):
        safe = value
        for secret in _secret_values:
            safe = safe.replace(secret, "[redacted]")
        safe = re.sub(r"\bPRONOIA_MODEL_SECRET_[A-Z0-9_]+\b", "[secret reference hidden]", safe)
        safe = re.sub(r"\blocal-model:[0-9a-f]{32}\b", "[secret reference hidden]", safe)
        return safe
    return value


def _profile_or_404(profile_id: str, *, public: bool = False) -> dict[str, Any]:
    from ..model_lab.platform_default import is_platform_profile_id, PLATFORM_DEFAULT_SELECTOR
    if is_platform_profile_id(profile_id) or profile_id == PLATFORM_DEFAULT_SELECTOR:
        raise HTTPException(status_code=404, detail="平台默认身份由服务端管理，不能作为自定义模型修改")
    profile = repo.get_profile(profile_id, public=public)
    if not profile:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return profile


def _validate_profile_endpoint(data: Mapping[str, Any]) -> None:
    if "base_url" not in data:
        return
    try:
        validate_registered_model_endpoint(data.get("base_url"), label="模型 API")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _discard_uncommitted_secret(reference: str | None) -> None:
    if reference:
        try:
            discard_secret(reference)
        except ModelSecretError:
            # Retaining an unused encrypted entry is safer than masking the
            # original write error or deleting any existing snapshot's key.
            pass


def _profile_origin(value: Any) -> tuple[str, str, int | None]:
    parsed = urlsplit(str(value or "").strip())
    scheme = parsed.scheme.lower()
    return (scheme, str(parsed.hostname or "").encode("idna").decode("ascii").lower(),
            parsed.port if parsed.port is not None else {"http": 80, "https": 443}.get(scheme))


@router.get("/profiles")
def list_profiles(active_only: bool = False) -> dict[str, Any]:
    items = repo.list_profiles(active_only=active_only, public=True)
    return _public({
        "items": items,
        "total": len(items),
        "default_profile_id": repo.get_default_profile_id(),
    })


@router.get("/profiles/default")
def get_default_profile() -> dict[str, Any]:
    profile = repo.get_default_profile(public=True)
    return _public({"default_profile_id": profile.get("id") if profile else None, "profile": profile})


@router.post("/profiles", status_code=status.HTTP_201_CREATED)
def create_profile(payload: ModelProfileCreate) -> dict[str, Any]:
    from ..model_lab.platform_default import PLATFORM_PROVIDER
    if payload.provider == PLATFORM_PROVIDER:
        raise HTTPException(status_code=422, detail="该 provider 为平台保留标识")
    data = payload.model_dump()
    _validate_profile_endpoint(data)
    data["base_url"] = str(data["base_url"]).strip().rstrip("/")
    new_reference = None
    try:
        if payload.api_key is not None:
            new_reference = store_secret(payload.api_key.get_secret_value())
            data["secret_env_ref"] = new_reference
        created = repo.create_profile(data)
    except ModelSecretError as exc:
        _discard_uncommitted_secret(new_reference)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        _discard_uncommitted_secret(new_reference)
        raise HTTPException(status_code=409, detail="模型配置名称已存在") from exc
    except Exception:
        _discard_uncommitted_secret(new_reference)
        raise
    return _public(created)


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: str) -> dict[str, Any]:
    return _public(_profile_or_404(profile_id, public=True))


@router.patch("/profiles/{profile_id}")
def update_profile(profile_id: str, payload: ModelProfileUpdate) -> dict[str, Any]:
    current = _profile_or_404(profile_id, public=False)
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    from ..model_lab.platform_default import PLATFORM_PROVIDER
    if updates.get("provider") == PLATFORM_PROVIDER:
        raise HTTPException(status_code=422, detail="该 provider 为平台保留标识")
    _validate_profile_endpoint(updates)
    if "base_url" in updates:
        updates["base_url"] = str(updates["base_url"]).strip().rstrip("/")
    provider_changed = "provider" in updates and updates["provider"] != current.get("provider")
    origin_changed = "base_url" in updates and _profile_origin(updates["base_url"]) != _profile_origin(current.get("base_url"))
    if current.get("secret_env_ref") and (provider_changed or origin_changed) and payload.api_key is None and not updates.get("secret_env_ref"):
        raise HTTPException(status_code=422, detail="更换模型服务商或 API 地址的域名、协议、端口后，请重新填写该服务的 API Key 或密钥环境变量名")
    if updates.get("is_active") is False and repo.get_default_profile_id() == profile_id:
        raise HTTPException(status_code=409, detail="请先切换默认模型，再停用当前默认模型")
    # Blank credentials preserve the saved reference. Replacements receive a
    # new immutable reference; historical snapshots retain their original key.
    new_reference = None
    try:
        if payload.api_key is not None:
            new_reference = store_secret(payload.api_key.get_secret_value())
            updates["secret_env_ref"] = new_reference
        updated = repo.update_profile(str(current["id"]), updates)
        if updated is None:
            raise HTTPException(status_code=404, detail="模型配置不存在")
    except ModelSecretError as exc:
        _discard_uncommitted_secret(new_reference)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        _discard_uncommitted_secret(new_reference)
        raise HTTPException(status_code=409, detail="模型配置名称已存在") from exc
    except Exception:
        _discard_uncommitted_secret(new_reference)
        raise
    return _public(updated)


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict[str, Any]:
    _profile_or_404(profile_id, public=False)
    if repo.get_default_profile_id() == profile_id:
        raise HTTPException(status_code=409, detail="请先切换默认模型，再删除当前默认模型")
    try:
        deleted = repo.delete_profile(profile_id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="该模型已被历史评测引用；可停用但不能删除") from exc
    return {"ok": bool(deleted)}


@router.post("/profiles/{profile_id}/validate")
async def validate_profile_connection(profile_id: str) -> dict[str, Any]:
    profile = _profile_or_404(profile_id, public=False)
    try:
        result = await asyncio.to_thread(validate_profile, profile)
        public_profile = repo.record_validation(profile_id, ok=True, message=str(result.get("message") or "连接成功"))
        return {**result, "profile": _public(public_profile)}
    except Exception as exc:  # noqa: BLE001
        # Provider errors are already bounded/redacted.  Do not expose the
        # environment-variable reference or any third-party response body.
        if isinstance(exc, ModelProviderError):
            message = str(exc)
        else:
            message = f"连接失败：{type(exc).__name__}"
        try:
            secret_value = resolve_profile_secret(profile)
        except (ModelProviderError, ModelSecretError, ValueError):
            secret_value = ""
        if secret_value:
            message = message.replace(secret_value, "[redacted]")
        message = _public(message, ())
        public_profile = repo.record_validation(profile_id, ok=False, message=message)
        return {"ok": False, "message": message[:500], "profile": _public(public_profile)}


@router.post("/profiles/{profile_id}/set-default")
def set_default_profile(profile_id: str) -> dict[str, Any]:
    profile = _profile_or_404(profile_id, public=False)
    if not profile.get("is_active"):
        raise HTTPException(status_code=409, detail="已停用的模型不能设为默认")
    if not profile.get("secret_configured"):
        raise HTTPException(status_code=409, detail="模型 API Key 尚未保存或不可用")
    selected = repo.set_default_profile(profile_id)
    return {"ok": True, "default_profile_id": profile_id, "profile": _public(selected)}


@router.get("/question-sets")
def list_question_sets() -> dict[str, Any]:
    items = repo.list_question_sets()
    return {"items": items, "total": len(items)}


@router.get("/question-sets/{question_set_id}")
def get_question_set(question_set_id: str) -> dict[str, Any]:
    question_set = repo.get_question_set(question_set_id)
    if not question_set:
        raise HTTPException(status_code=404, detail="题库不存在")
    return question_set


@router.post("/question-sets", status_code=status.HTTP_201_CREATED)
def create_question_set(payload: QuestionSetCreate) -> dict[str, Any]:
    try:
        return repo.create_question_set(payload.model_dump())
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="题库或题目标识重复") from exc


def _resolve_batch_inputs(payload: EvaluationBatchCreate) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for profile_id in payload.profile_ids:
        profile = _profile_or_404(profile_id, public=False)
        if not profile.get("is_active"):
            raise HTTPException(status_code=409, detail=f"模型 {profile.get('name')} 已停用")
        if not payload.dry_run and not profile.get("secret_configured"):
            raise HTTPException(status_code=409, detail=f"模型 {profile.get('name')} 的服务端密钥未配置")
        profiles.append(profile)

    resolved: dict[str, Any] = {}
    if payload.prediction.enabled:
        dataset = db.get_bt_dataset(str(payload.prediction.dataset_id or ""))
        if not dataset:
            raise HTTPException(status_code=404, detail="预测数据集不存在")
        if str(dataset.get("dataset_kind") or "event") != "event":
            raise HTTPException(status_code=422, detail="模型预测评测必须使用事件数据集")
        version_id = (
            payload.prediction.dataset_version
            or dataset.get("current_version")
            or dataset.get("dataset_version")
        )
        if not version_id or not db.get_bt_dataset_version(str(dataset["id"]), str(version_id)):
            raise HTTPException(status_code=409, detail="预测数据集没有可冻结的有效版本")
        resolved["dataset"] = dataset
        resolved["dataset_version"] = str(version_id)

    if payload.qa.enabled:
        question_set = repo.get_question_set(str(payload.qa.question_set_id or ""))
        if not question_set:
            raise HTTPException(status_code=404, detail="问答题库不存在")
        questions = repo.selected_questions(
            str(payload.qa.question_set_id), payload.qa.question_ids,
        )
        if not questions:
            raise HTTPException(status_code=422, detail="问答测试没有选中任何题目")
        if payload.qa.question_ids:
            valid = {
                str(value)
                for question in questions
                for value in (question.get("id"), question.get("code"))
                if value
            }
            missing = [item for item in payload.qa.question_ids if str(item) not in valid]
            if missing:
                raise HTTPException(status_code=422, detail={"message": "部分题目不存在", "items": missing})
        resolved["question_set"] = question_set
        resolved["questions"] = questions
        if payload.qa.scoring_mode in {"auto", "mixed"}:
            judge = _profile_or_404(str(payload.qa.judge_profile_id or ""), public=False)
            if not judge.get("is_active"):
                raise HTTPException(status_code=409, detail="评分模型已停用")
            if not payload.dry_run and not judge.get("secret_configured"):
                raise HTTPException(status_code=409, detail="评分模型的服务端密钥未配置")
            resolved["judge"] = judge
    return profiles, resolved


@router.post("/batches", status_code=status.HTTP_201_CREATED)
def create_batch(payload: EvaluationBatchCreate) -> dict[str, Any]:
    if payload.prediction.enabled and payload.prediction.runner != "team_full":
        raise HTTPException(status_code=422, detail="Pronoia 基模评测固定使用多 Agent 流程，仅更换本批次基模")
    if payload.qa.enabled and payload.qa.variants != ["pronoia"]:
        raise HTTPException(status_code=422, detail="Pronoia 基模评测只测试 Pronoia；独立模型对比请进入事件模型栏目")
    profiles, resolved = _resolve_batch_inputs(payload)
    prediction = payload.prediction.model_dump()
    qa = payload.qa.model_dump()
    if payload.prediction.enabled:
        prediction["dataset_version"] = resolved["dataset_version"]
    question_count = len(resolved.get("questions") or [])
    task_specs: list[dict[str, Any]] = []
    for profile in profiles:
        frozen = profile_snapshot(profile)
        if payload.prediction.enabled:
            task_specs.append({
                "kind": "prediction",
                "profile_id": profile["id"],
                "total_items": int((resolved.get("dataset") or {}).get("total_events") or 0),
                "config": {
                    **prediction,
                    "run_name": f"{payload.name} · Pronoia（{profile.get('name')}）",
                    "profile_snapshot": frozen,
                    "dry_run": payload.dry_run,
                },
            })
        if payload.qa.enabled:
            task_specs.append({
                "kind": "qa",
                "profile_id": profile["id"],
                "total_items": question_count * len(payload.qa.variants) * payload.qa.repeats,
                "config": {
                    **qa,
                    "profile_snapshot": frozen,
                    "judge_profile_snapshot": profile_snapshot(resolved["judge"])
                    if resolved.get("judge") else None,
                    # Freeze question text and rubric metadata so later custom
                    # question-set edits cannot rewrite historical experiments.
                    "questions": resolved.get("questions") or [],
                    "dry_run": payload.dry_run,
                },
            })

    # Keep a newly committed pending batch out of the history-deletion window
    # until its auto-start state is durable.  Deletion takes this same outer
    # lock before the Model Lab worker lock, so the acquisition order is stable.
    with db.HISTORY_RELATION_LOCK:
        batch = repo.create_batch(
            name=payload.name,
            profile_ids=[str(item["id"]) for item in profiles],
            dataset_id=prediction.get("dataset_id") if payload.prediction.enabled else None,
            dataset_version=prediction.get("dataset_version") if payload.prediction.enabled else None,
            question_set_id=qa.get("question_set_id") if payload.qa.enabled else None,
            prediction=prediction,
            qa=qa,
            # Persist launch intent so startup recovery can distinguish an
            # auto-start batch that crashed in the create→start window from one the
            # user deliberately saved as pending.
            scoring={
                "source": "pronoia_base_evaluation",
                "dry_run": payload.dry_run,
                "demo": payload.dry_run,
                "auto_start": payload.auto_start,
            },
            task_specs=task_specs,
        )
        if payload.auto_start:
            started, message = service.start_batch(str(batch["id"]))
            if not started:
                raise HTTPException(status_code=409, detail=message)
            current = repo.get_batch(str(batch["id"]), include_tasks=True)
            if not current:
                raise HTTPException(status_code=409, detail="评测批次启动后未找到持久化记录")
            batch = current
    return _public(batch)


@router.get("/batches")
def list_batches(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    items = repo.list_batches(limit=limit)
    return _public({"items": items, "total": len(items)})


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    batch = repo.get_batch(batch_id, include_tasks=True)
    if not batch:
        raise HTTPException(status_code=404, detail="评测批次不存在")
    return _public(batch)


@router.post("/batches/{batch_id}/start")
def start_batch(batch_id: str) -> dict[str, Any]:
    ok, message = service.start_batch(batch_id)
    if not ok:
        if not repo.get_batch(batch_id):
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=409, detail=message)
    return _public({"ok": True, "message": message, "batch": repo.get_batch(batch_id, include_tasks=True)})


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str) -> dict[str, Any]:
    ok, message = service.cancel_batch(batch_id)
    if not ok:
        if not repo.get_batch(batch_id):
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=409, detail=message)
    return _public({"ok": True, "message": message, "batch": repo.get_batch(batch_id, include_tasks=True)})


@router.get("/batches/{batch_id}/results")
def get_batch_results(batch_id: str) -> dict[str, Any]:
    result = service.batch_results(batch_id)
    if not result:
        raise HTTPException(status_code=404, detail="评测批次不存在")
    return _public(result)


@router.get("/qa-results/{result_id}")
def get_qa_result(result_id: str) -> dict[str, Any]:
    result = repo.get_qa_result(result_id)
    if not result:
        raise HTTPException(status_code=404, detail="问答结果不存在")
    return _public(result)


@router.post("/qa-results/{result_id}/manual-score")
def score_qa_result(result_id: str, payload: ManualScoreRequest) -> dict[str, Any]:
    current = repo.get_qa_result(result_id)
    if not current:
        raise HTTPException(status_code=404, detail="问答结果不存在")
    if current.get("status") in {"pending", "running", "cancelled", "demo"}:
        raise HTTPException(status_code=409, detail="该结果当前不能人工评分")
    if not str(current.get("answer") or "").strip():
        raise HTTPException(status_code=409, detail="该结果没有可供人工评分的答案")
    scored = repo.set_manual_score(result_id, payload.model_dump())
    return _public(scored)
