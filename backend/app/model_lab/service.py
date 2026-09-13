"""Durable, independently scheduled prediction and QA evaluation jobs.

The HTTP layer only validates requests and persists immutable task snapshots.
Workers run in the background, and prediction workers create ordinary
``bt_runs`` instead of inventing a second backtest implementation.  This gives
Model Lab the same metrics, audit trail and Arena eligibility as the existing
event backtester.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import Any

from .. import db
from . import repository as repo
from .providers import ModelProviderError, estimate_cost, profile_snapshot, score_answer


logger = logging.getLogger(__name__)

_WORKERS_LOCK = threading.RLock()
_BATCH_THREADS: dict[str, threading.Thread] = {}
_BATCH_CANCEL: dict[str, threading.Event] = {}
_TERMINAL_TASK_STATUSES = {"done", "failed", "cancelled"}
_TERMINAL_QA_STATUSES = {"done", "demo", "awaiting_manual", "cancelled"}
_HISTORY_DELETE_BLOCKED_STATUSES = {"running", "paused", "interrupted", "starting", "queued", "cancelling"}


class _SidecarCallCancelled(Exception):
    """The batch stopped waiting for an isolated provider call."""


def _configured_global_concurrency() -> int:
    try:
        value = int(os.getenv("PRONOIA_MODEL_LAB_GLOBAL_CONCURRENCY", "8"))
    except (TypeError, ValueError):
        value = 8
    return max(1, min(value, 64))


# This is deliberately a threading semaphore: every batch owns a separate
# asyncio loop in a daemon thread, while the provider budget is process-wide.
_GLOBAL_CALL_SLOTS = threading.BoundedSemaphore(_configured_global_concurrency())


async def _acquire_global_call_slot(
    cancel: threading.Event,
    slots: threading.BoundedSemaphore | None = None,
) -> bool:
    """Acquire a process-wide provider slot without blocking an event loop."""
    slot_pool = slots or _GLOBAL_CALL_SLOTS
    while not cancel.is_set():
        if slot_pool.acquire(blocking=False):
            return True
        await asyncio.sleep(0.025)
    return False


async def _run_provider_sidecar(
    operation: Callable[[], Any],
    cancel: threading.Event,
    *,
    label: str,
) -> Any:
    """Run one provider-only operation without pinning the batch worker.

    Pronoia answers are async while raw answers and judges may enter synchronous
    ``to_thread`` work.  Cancelling only the outer asyncio Future is insufficient:
    ``asyncio.run`` waits for its default executor during shutdown.  A dedicated
    daemon sidecar owns the complete call instead.  The batch coroutine polls its
    durable cancellation signal and can return immediately; an async sidecar is
    also asked to cancel its task, while an already-running synchronous transport
    remains bounded by its configured timeout.

    The sidecar receives only frozen provider/question values and must never call
    Model Lab or backtest repositories.  It owns the global slot until its *real*
    completion, preventing repeated cancel/restart cycles from bypassing the
    process-wide provider concurrency limit.
    """
    slot_pool = _GLOBAL_CALL_SLOTS
    if not await _acquire_global_call_slot(cancel, slot_pool):
        raise _SidecarCallCancelled()

    done = threading.Event()
    state_lock = threading.Lock()
    state: dict[str, Any] = {
        "loop": None,
        "task": None,
        "result": None,
        "error": None,
        "cancelled": False,
    }

    def request_async_cancel() -> None:
        with state_lock:
            loop = state.get("loop")
            task = state.get("task")
        if loop is None or task is None:
            return
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            # The call won the race and closed its event loop.
            pass

    async def await_operation(value: Any) -> Any:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        with state_lock:
            state["loop"] = loop
            state["task"] = task
        if cancel.is_set() and task is not None:
            task.cancel()
        return await value

    def sidecar_main() -> None:
        try:
            if cancel.is_set():
                state["cancelled"] = True
                return
            value = operation()
            if inspect.isawaitable(value):
                try:
                    state["result"] = asyncio.run(await_operation(value))
                except asyncio.CancelledError:
                    state["cancelled"] = True
            else:
                state["result"] = value
        except BaseException as exc:  # noqa: BLE001 - transfer to the batch thread
            state["error"] = exc
        finally:
            slot_pool.release()
            done.set()

    worker = threading.Thread(
        target=sidecar_main,
        name=f"model-lab-call-{label[:48]}",
        daemon=True,
    )
    try:
        worker.start()
    except BaseException:
        slot_pool.release()
        raise

    try:
        while not done.is_set():
            if cancel.is_set():
                request_async_cancel()
                raise _SidecarCallCancelled()
            await asyncio.sleep(0.025)
    except asyncio.CancelledError:
        request_async_cancel()
        raise

    # Cancellation is absorbing even when the provider won the completion race.
    if cancel.is_set():
        request_async_cancel()
        raise _SidecarCallCancelled()
    if state["cancelled"]:
        raise RuntimeError("provider sidecar cancelled unexpectedly")
    error = state["error"]
    if error is not None:
        if isinstance(error, Exception):
            raise error
        raise RuntimeError(f"provider sidecar failed: {type(error).__name__}")
    return state["result"]


def _qa_was_cancelled(cancel: threading.Event, result_id: str) -> bool:
    """Observe both the in-memory signal and the durable absorbing state."""
    if cancel.is_set():
        repo.update_qa_result(result_id, status="cancelled")
        return True
    current = repo.get_qa_result(result_id)
    return bool(current and str(current.get("status") or "") == "cancelled")


def _sanitize_text(text: Any, *snapshots: Mapping[str, Any] | None) -> str:
    from .. import config
    from ..model_endpoint_security import redact_sensitive_text
    from .providers import ModelProviderError, resolve_profile_secret
    secrets = [config.LLM_API_KEY]
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        try:
            secrets.append(resolve_profile_secret(snapshot))
        except (ModelProviderError, ValueError):
            # A missing/corrupt credential must not hide the original failure.
            pass
    return redact_sensitive_text(text, *secrets)


def _scrub_payload(value: Any, *snapshots: Mapping[str, Any] | None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _scrub_payload(item, *snapshots)
            for key, item in value.items()
            if str(key).lower() not in {"api_key", "authorization", "secret_env_ref"}
        }
    if isinstance(value, list):
        return [_scrub_payload(item, *snapshots) for item in value]
    if isinstance(value, tuple):
        return [_scrub_payload(item, *snapshots) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, *snapshots)
    return value


def _safe_error(exc: BaseException, *snapshots: Mapping[str, Any] | None) -> str:
    """Bound persisted errors and remove credentials without logging them."""
    return _sanitize_text(f"{type(exc).__name__}: {exc}", *snapshots)[:1_500]


def _decode_metrics(run: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = run.get("metrics") or run.get("metrics_json")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


def _prediction_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    status = str(run.get("status") or "unknown")
    metrics = _decode_metrics(run)
    computed: dict[str, Any] = {}
    metrics_error = None
    if status == "done" and run.get("id"):
        # Event metrics are computed from frozen files, without model/network calls.
        from ..routes.backtest import get_run_metrics_internal
        try:
            computed = get_run_metrics_internal(str(run["id"]))
            if not isinstance(computed, dict) or not isinstance(computed.get("metrics"), dict):
                raise ValueError("invalid metrics response")
            metrics = computed["metrics"]
        except Exception as exc:
            # Missing historical files must not fail the batch or invent scores.
            logger.warning("Prediction metrics unavailable for run %s (%s)", run["id"], type(exc).__name__)
            computed = {}
            metrics = None
            metrics_error = "指标暂不可用，请打开运行结果检查冻结的预测文件和实际收益标签。"
    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    # Keep the same frozen dataset classification used by event results/Arena.
    # A real model call on placeholder facts is still a workflow demonstration.
    from ..routes.arena import _event_semantic_quality_for_run
    semantic_quality = _event_semantic_quality_for_run(dict(run)) or {}
    semantic_status = semantic_quality.get("status")
    dataset_demo = semantic_status == "demo_only"
    from ..event_backtest.evaluation_protocol import resolve_run_oracle_epsilon
    try:
        oracle_epsilon = resolve_run_oracle_epsilon(run)
    except ValueError:
        oracle_epsilon = None
        metrics = None
        metrics_error = "冻结的 Oracle 收益阈值无效，请检查本次运行的评测设置。"
    quality: dict[str, Any] = {
        key: int(computed.get(key, run.get(key)) or 0)
        for key in ("insufficient_data_count", "invalid_output_count", "voluntary_abstain_count")
    }
    quality["warning_count"] = max(
        int(run.get("warning_count") or 0),
        quality["insufficient_data_count"] + quality["invalid_output_count"],
    )
    quality["completion_quality"] = (
        "completed_with_warnings" if quality["warning_count"] or metrics_error
        else run.get("completion_quality")
    )
    quality["direction_mode"] = computed.get("direction_mode") or config.get("direction_mode") or "ternary"
    quality["epsilon"] = computed.get("epsilon", oracle_epsilon)
    for key in ("valid_output_count", "neutral_count", "n_outputs"):
        quality[key] = computed.get(key)
    issues_total = sum(quality[key] for key in (
        "insufficient_data_count", "invalid_output_count", "voluntary_abstain_count",
    ))
    issues = repo.list_prediction_issues(
        str(run["id"]), runner=str(run.get("runner") or ""),
    ) if issues_total and run.get("id") else []
    for issue in issues:
        issue["reason"] = _sanitize_text(issue["reason"], config.get("model_profile_snapshot"))[:1200]
    return {
        **quality,
        "metrics_error": metrics_error,
        "prediction_issues": issues,
        "issues_total": issues_total,
        "issues_truncated": issues_total > len(issues),
        "demo": False,
        "dataset_semantic_quality": semantic_status,
        "dataset_demo": dataset_demo,
        "formal_evaluation_eligible": not dataset_demo,
        "backtest_run_id": run.get("id"),
        "bt_run_id": run.get("id"),
        "arena_eligible": status == "done" and not dataset_demo and not quality["warning_count"] and not metrics_error
        and str(run.get("visibility") or "") == "arena_safe",
        "status": status,
        "done_items": int(run.get("done_events") or 0),
        "total_items": int(run.get("total_events") or 0),
        "dataset_id": run.get("dataset_id"),
        "dataset_version": run.get("dataset_version"),
        "model_version": run.get("model_version"),
        "protocol_hash": run.get("protocol_hash"),
        "metrics": metrics,
        "error": run.get("error_msg"),
    }


async def _run_prediction_task(task: Mapping[str, Any], cancel: threading.Event) -> None:
    task_id = str(task["id"])
    config = dict(task.get("config") or {})
    snapshot = dict(config.get("profile_snapshot") or {})
    if cancel.is_set() or str((repo.get_task(task_id) or {}).get("status") or "") == "cancelled":
        repo.update_task(task_id, status="cancelled")
        return
    if config.get("dry_run"):
        repo.update_task(
            task_id,
            status="done",
            done_items=1,
            total_items=1,
            result_summary={
                "demo": True,
                "arena_eligible": False,
                "backtest_run_id": None,
                "bt_run_id": None,
                "message": "演示运行只验证编排，不调用模型，也不生成可进入 Arena 的结果。",
            },
        )
        return

    started_task = repo.update_task(task_id, status="running")
    if cancel.is_set() or str((started_task or {}).get("status") or "") == "cancelled":
        repo.update_task(task_id, status="cancelled")
        return
    try:
        from ..event_backtest import orchestrator as orch
        from ..routes import backtest as backtest_route
        from ..schemas import CreateBacktestRunRequest

        runner = str(config.get("runner") or "team_prompt")
        request = CreateBacktestRunRequest(
            name=str(config.get("run_name") or f"模型评测 · {snapshot.get('name') or snapshot.get('model_id')}"),
            runner=runner,
            dataset_id=str(config.get("dataset_id") or ""),
            dataset_version=str(config.get("dataset_version") or "") or None,
            prompt_variant=str(config.get("prompt_variant") or "v0"),
            model_version=str(snapshot.get("model_id") or "") or None,
            horizon=str(config.get("horizon") or "t3"),
            concurrency=int(config.get("concurrency") or 2),
            strategy_spec={
                "type": "event",
                "adapter": "existing_platform",
                "runner": runner,
                "name": str(snapshot.get("name") or snapshot.get("model_id") or "Pronoia"),
                "version": str(snapshot.get("updated_at") or "snapshot"),
                "parameters": {"model_profile_id": snapshot.get("id")},
            },
            visibility="arena_safe",
            config={
                "model_profile_snapshot": snapshot,
                "model_lab_batch_id": task.get("batch_id"),
                "model_lab_task_id": task_id,
                "evaluation_source": "model_lab",
                "evaluation_role": "pronoia_base_model",
                "prediction_targets": ["event_direction", "asset_return_pct"],
                "demo": False,
            },
        )

        def resolve_or_create_run() -> tuple[str, dict[str, Any] | None]:
            # Run publication and the durable task link form one history
            # relationship.  Cancellation/deletion take this same outer lock,
            # so neither can remove the task between the insert and its link.
            with db.HISTORY_RELATION_LOCK:
                current_task = repo.get_task(task_id)
                if (
                    cancel.is_set()
                    or current_task is None
                    or str(current_task.get("status") or "") == "cancelled"
                ):
                    return "", None
                current_run_id = str(current_task.get("backtest_run_id") or "")
                if not current_run_id:
                    # Recovery closes the transaction gap where create_run
                    # committed but the task link did not before termination.
                    current_run_id = str(repo.find_backtest_run_id_for_task(task_id) or "")
                current_run = db.get_bt_run(current_run_id) if current_run_id else None
                if current_run is None:
                    # This snapshot is frozen by Model Lab on the server. Public
                    # Run creation rejects authored snapshots at its HTTP edge.
                    created = backtest_route.create_run_from_trusted_request(request)
                    current_run_id = str(created["id"])
                    current_run = db.get_bt_run(current_run_id)
                linked = repo.update_task(task_id, backtest_run_id=current_run_id)
                if linked is None:
                    raise RuntimeError("基模评测任务在关联 Run 前已不存在")
                return current_run_id, current_run

        run_id, run = await asyncio.to_thread(resolve_or_create_run)
        if not run_id:
            repo.update_task(task_id, status="cancelled")
            return

        # The backtest may have committed its terminal state just before the
        # old process died.  Reuse it instead of calling start (or creating a
        # duplicate run) during recovery.
        if run and str(run.get("status") or "") == "done":
            summary = _prediction_summary(run)
            repo.update_task(
                task_id,
                status="done",
                total_items=int(run.get("total_events") or 0),
                done_items=int(run.get("done_events") or 0),
                result_summary=summary,
            )
            return

        # Keep the final cancellation recheck and Run start in the same
        # relationship window.  Otherwise cancellation could mark the pending
        # Run terminal immediately before start_bt_run intentionally resumes it.
        with db.HISTORY_RELATION_LOCK:
            current_task = repo.get_task(task_id)
            run = db.get_bt_run(run_id)
            if (
                cancel.is_set()
                or current_task is None
                or str(current_task.get("status") or "") == "cancelled"
            ):
                if run and str(run.get("status") or "") == "pending":
                    db.update_bt_run_status(run_id, "cancelled")
                repo.update_task(task_id, status="cancelled")
                return
            started = orch.start_bt_run(run_id)
        if not started.ok:
            raise RuntimeError(started.error or "回测任务无法启动")

        while True:
            if cancel.is_set():
                current = db.get_bt_run(run_id) or {}
                if str(current.get("status") or "") == "pending":
                    db.update_bt_run_status(run_id, "cancelled")
                else:
                    orch.cancel_bt_run(run_id)
                repo.update_task(task_id, status="cancelled")
                return
            current = db.get_bt_run(run_id)
            if not current:
                raise RuntimeError("关联的回测 Run 已不存在")
            repo.update_task(
                task_id,
                total_items=int(current.get("total_events") or 0),
                done_items=int(current.get("done_events") or 0),
            )
            status = str(current.get("status") or "")
            if status in {"done", "failed", "cancelled"}:
                if cancel.is_set():
                    repo.update_task(task_id, status="cancelled")
                    return
                summary = _prediction_summary(current)
                repo.update_task(
                    task_id,
                    status=status,
                    total_items=int(current.get("total_events") or 0),
                    done_items=int(current.get("done_events") or 0),
                    result_summary=summary,
                    error_msg=_safe_error(RuntimeError(current.get("error_msg") or "回测失败"), snapshot)
                    if status == "failed" else None,
                )
                return
            await asyncio.sleep(0.35)
    except Exception as exc:  # noqa: BLE001
        if cancel.is_set():
            repo.update_task(task_id, status="cancelled")
        else:
            repo.update_task(task_id, status="failed", error_msg=_safe_error(exc, snapshot))


async def _pronoia_answer(profile: Mapping[str, Any], question: Mapping[str, Any]) -> dict[str, Any]:
    """Run the actual Pronoia team path and capture its visible audit trace."""
    from ..agents.team import run_team
    from ..llm import model_profile_context, noop_artifact_store

    state: dict[str, Any] = {"content": "", "tool_trace": [], "rounds": 0}
    started = time.monotonic()
    with model_profile_context(dict(profile)):
        async for _event in run_team(
            str(question.get("prompt") or ""),
            [],
            state,
            noop_artifact_store,
        ):
            # The Model Lab persists the final answer and compact tool trace;
            # it does not mirror transient SSE tokens into another job table.
            pass
    answer = str(state.get("content") or "").strip()
    if not answer:
        raise ModelProviderError("Pronoia 没有生成可评分答案")
    return {
        "answer": answer[:200_000],
        "usage": {"basis": "unavailable"},
        "tool_trace": list(state.get("tool_trace") or [])[:500],
        "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
    }


async def _raw_answer(profile: Mapping[str, Any], question: Mapping[str, Any]) -> dict[str, Any]:
    from .providers import call_chat

    result = await asyncio.to_thread(
        call_chat,
        profile,
        [
            {
                "role": "system",
                "content": (
                    "你正在参加统一金融问答评测。直接完成用户任务；不得声称调用了未实际调用的工具或数据，"
                    "无法获得实时资料时必须明确限制。"
                ),
            },
            {"role": "user", "content": str(question.get("prompt") or "")},
        ],
    )
    return {
        "answer": str(result["answer"])[:200_000],
        "usage": result.get("usage") or {"basis": "unavailable"},
        "tool_trace": [],
        "latency_ms": int(result.get("latency_ms") or 0),
    }


async def _run_qa_item(
    *,
    result: Mapping[str, Any],
    profile: Mapping[str, Any],
    judge_profile: Mapping[str, Any] | None,
    question: Mapping[str, Any],
    variant: str,
    scoring_mode: str,
    cancel: threading.Event,
) -> str:
    result_id = str(result["id"])
    if _qa_was_cancelled(cancel, result_id):
        return "cancelled"
    current = repo.update_qa_result(result_id, status="running")
    if not current or str(current.get("status") or "") == "cancelled":
        return "cancelled"
    try:
        # A process may stop after the candidate answer is durably checkpointed
        # but before judging finishes. Reuse that answer on resume.
        if current.get("answer"):
            generated = {
                "answer": str(current.get("answer") or ""),
                "usage": current.get("usage") or {"basis": "unavailable"},
                "tool_trace": current.get("tool_trace") or [],
                "latency_ms": int(current.get("latency_ms") or 0),
            }
        else:
            try:
                generated = await _run_provider_sidecar(
                    (lambda: _pronoia_answer(profile, question))
                    if variant == "pronoia"
                    else (lambda: _raw_answer(profile, question)),
                    cancel,
                    label=f"answer-{result_id}",
                )
            except _SidecarCallCancelled:
                repo.update_qa_result(result_id, status="cancelled")
                return "cancelled"
            if _qa_was_cancelled(cancel, result_id):
                return "cancelled"
            generated = _scrub_payload(generated, profile, judge_profile)
            checkpoint = repo.update_qa_result(
                result_id,
                status="running",
                answer=generated["answer"],
                usage=generated.get("usage") or {"basis": "unavailable"},
                tool_trace=generated.get("tool_trace") or [],
                latency_ms=int(generated.get("latency_ms") or 0),
            )
            if not checkpoint or str(checkpoint.get("status") or "") == "cancelled":
                return "cancelled"

        if _qa_was_cancelled(cancel, result_id):
            return "cancelled"

        usage = generated.get("usage") or {"basis": "unavailable"}
        auto_score_raw = current.get("auto_score")
        auto_score = dict(auto_score_raw) if isinstance(auto_score_raw, Mapping) else None
        score_error: str | None = None
        if scoring_mode in {"auto", "mixed"} and auto_score is None:
            if not judge_profile:
                raise RuntimeError("自动评分缺少已冻结的评分模型")
            try:
                try:
                    auto_score = await _run_provider_sidecar(
                        lambda: score_answer(judge_profile, question, generated["answer"]),
                        cancel,
                        label=f"score-{result_id}",
                    )
                except _SidecarCallCancelled:
                    repo.update_qa_result(result_id, status="cancelled")
                    return "cancelled"
                if _qa_was_cancelled(cancel, result_id):
                    return "cancelled"
                auto_score = _scrub_payload(auto_score, profile, judge_profile)
                checkpoint = repo.update_qa_result(
                    result_id,
                    status="running",
                    auto_score=auto_score,
                )
                if not checkpoint or str(checkpoint.get("status") or "") == "cancelled":
                    return "cancelled"
            except Exception as exc:  # noqa: BLE001
                if _qa_was_cancelled(cancel, result_id):
                    return "cancelled"
                score_error = _safe_error(exc, judge_profile)
                if scoring_mode == "auto":
                    if _qa_was_cancelled(cancel, result_id):
                        return "cancelled"
                    updated = repo.update_qa_result(
                        result_id,
                        status="partial",
                        answer=generated["answer"],
                        usage=usage,
                        tool_trace=generated.get("tool_trace") or [],
                        latency_ms=int(generated.get("latency_ms") or 0),
                        cost=estimate_cost(profile, usage),
                        error_msg=f"答案已生成，但自动评分失败：{score_error}",
                    )
                    return "cancelled" if updated and updated.get("status") == "cancelled" else "partial"

        candidate_cost = estimate_cost(profile, usage)
        judge_cost = (
            estimate_cost(judge_profile or {}, auto_score.get("usage") or {})
            if auto_score else 0.0
        )
        awaiting_manual = scoring_mode in {"manual", "mixed"}
        if _qa_was_cancelled(cancel, result_id):
            return "cancelled"
        updated = repo.update_qa_result(
            result_id,
            status="awaiting_manual" if awaiting_manual else "done",
            answer=generated["answer"],
            usage=usage,
            tool_trace=generated.get("tool_trace") or [],
            latency_ms=int(generated.get("latency_ms") or 0),
            cost=round(candidate_cost + judge_cost, 8),
            auto_score=auto_score,
            final_score=auto_score,
            error_msg=(f"自动评分失败，等待人工评分：{score_error}" if score_error else None),
        )
        if updated and str(updated.get("status") or "") == "cancelled":
            return "cancelled"
        return "awaiting_manual" if awaiting_manual else "done"
    except Exception as exc:  # noqa: BLE001
        if _qa_was_cancelled(cancel, result_id):
            return "cancelled"
        updated = repo.update_qa_result(
            result_id, status="failed", error_msg=_safe_error(exc, profile, judge_profile),
        )
        return "cancelled" if updated and updated.get("status") == "cancelled" else "failed"


async def _run_qa_task(task: Mapping[str, Any], cancel: threading.Event) -> None:
    task_id = str(task["id"])
    batch_id = str(task["batch_id"])
    config = dict(task.get("config") or {})
    profile = dict(config.get("profile_snapshot") or {})
    judge_profile_raw = config.get("judge_profile_snapshot")
    judge_profile = dict(judge_profile_raw) if isinstance(judge_profile_raw, dict) else None
    questions = [dict(item) for item in (config.get("questions") or [])]
    variants = [str(item) for item in (config.get("variants") or ["pronoia"])]
    repeats = max(1, int(config.get("repeats") or 1))
    total = len(questions) * len(variants) * repeats
    started_task = repo.update_task(task_id, status="running", total_items=total)
    if cancel.is_set() or str((started_task or {}).get("status") or "") == "cancelled":
        repo.update_task(task_id, status="cancelled")
        return

    slots: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for question in questions:
        for variant in variants:
            for repeat_no in range(1, repeats + 1):
                result = repo.get_or_create_qa_result(
                    batch_id=batch_id,
                    task_id=task_id,
                    profile_id=str(task["profile_id"]),
                    question_id=str(question["id"]),
                    variant=variant,
                    repeat_no=repeat_no,
                )
                slots.append((result, question, variant))

    if config.get("dry_run"):
        for result, question, variant in slots:
            if cancel.is_set():
                repo.update_qa_result(str(result["id"]), status="cancelled")
                continue
            repo.update_qa_result(
                str(result["id"]),
                status="demo",
                answer=(
                    f"[DEMO] 已验证 {variant} 编排：题目 {question.get('code') or question.get('id')}。"
                    "演示运行未调用候选模型或评分模型，本答案不得作为正式评测结果。"
                ),
                usage={"basis": "demo", "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                tool_trace=[],
                latency_ms=0,
                cost=0.0,
            )
        repo.update_task(
            task_id,
            status="done",
            total_items=total,
            done_items=total,
            result_summary={
                "demo": True,
                "arena_eligible": False,
                "answers": total,
                "message": "演示问答仅验证任务矩阵，未调用任何模型。",
            },
        )
        return

    scoring_mode = str(config.get("scoring_mode") or "auto")
    semaphore = asyncio.Semaphore(max(1, min(int(config.get("concurrency") or 2), 8)))
    completed = sum(1 for result, _, _ in slots if str(result.get("status") or "") in _TERMINAL_QA_STATUSES)
    failed = 0
    partial = 0
    counter_lock = asyncio.Lock()
    repo.update_task(task_id, done_items=completed)

    async def execute_slot(result: dict[str, Any], question: dict[str, Any], variant: str) -> None:
        nonlocal completed, failed, partial
        if str(result.get("status") or "") in _TERMINAL_QA_STATUSES:
            return
        async with semaphore:
            outcome = await _run_qa_item(
                result=result,
                profile=profile,
                judge_profile=judge_profile,
                question=question,
                variant=variant,
                scoring_mode=scoring_mode,
                cancel=cancel,
            )
        async with counter_lock:
            if outcome not in {"cancelled"}:
                completed += 1
            if outcome == "failed":
                failed += 1
            elif outcome == "partial":
                partial += 1
            repo.update_task(task_id, done_items=completed, total_items=total)

    await asyncio.gather(*(execute_slot(*slot) for slot in slots))
    if cancel.is_set():
        repo.update_task(task_id, status="cancelled", done_items=completed, total_items=total)
        return
    task_status = "failed" if failed == total and total else "partial" if failed or partial else "done"
    repo.update_task(
        task_id,
        status=task_status,
        done_items=completed,
        total_items=total,
        result_summary={
            "demo": False,
            "arena_eligible": False,
            "answers": completed,
            "failed": failed,
            "partial": partial,
            "awaiting_manual": sum(
                1 for item in repo.list_qa_results(task_id=task_id)
                if item.get("status") == "awaiting_manual"
            ),
        },
    )


async def _run_one_task(task: Mapping[str, Any], cancel: threading.Event) -> None:
    if cancel.is_set():
        return
    if str(task.get("status") or "") in {"done", "cancelled"}:
        return
    if task.get("kind") == "prediction":
        await _run_prediction_task(task, cancel)
    elif task.get("kind") == "qa":
        await _run_qa_task(task, cancel)
    else:
        repo.update_task(str(task["id"]), status="failed", error_msg="未知 Model Lab 子任务类型")


async def _run_batch_async(batch_id: str, cancel: threading.Event) -> None:
    tasks = repo.list_tasks(batch_id)
    await asyncio.gather(*(_run_one_task(task, cancel) for task in tasks))
    if cancel.is_set():
        repo.cancel_open_tasks(batch_id)
        repo.update_batch_status(batch_id, "cancelled")
        return
    final_tasks = repo.list_tasks(batch_id)
    statuses = [str(item.get("status") or "") for item in final_tasks]
    if statuses and all(status == "done" for status in statuses):
        final = "done"
    elif statuses and all(status == "failed" for status in statuses):
        final = "failed"
    elif any(status in {"partial", "failed", "cancelled"} for status in statuses):
        final = "partial"
    else:
        # A batch is complete only when every independent child task completed.
        # Unknown/non-terminal child states must never be promoted to success.
        final = "partial"
    repo.update_batch_status(batch_id, final)


def _thread_main(batch_id: str, cancel: threading.Event) -> None:
    try:
        asyncio.run(_run_batch_async(batch_id, cancel))
    except Exception as exc:  # noqa: BLE001
        repo.update_batch_status(batch_id, "failed", error_msg=_safe_error(exc))
    finally:
        with _WORKERS_LOCK:
            _BATCH_THREADS.pop(batch_id, None)
            _BATCH_CANCEL.pop(batch_id, None)


def start_batch(batch_id: str) -> tuple[bool, str]:
    batch = repo.get_batch(batch_id, include_tasks=True)
    if not batch:
        return False, "评测批次不存在"
    with _WORKERS_LOCK:
        # Re-read under the lifecycle lock so a concurrent cancel/start pair
        # cannot make a decision from stale status.
        batch = repo.get_batch(batch_id, include_tasks=True)
        if not batch:
            return False, "评测批次不存在"
        status = str(batch.get("status") or "")
        if status == "done":
            return False, f"status={status} 的批次不能重复启动"
        current = _BATCH_THREADS.get(batch_id)
        if current is not None and current.is_alive():
            if status == "cancelled":
                return False, "取消中的 worker 尚未退出，请稍后重试"
            return True, "批次已在运行"
        # A direct resume request must also repair stale in-flight rows; startup
        # normally does this first, but this keeps the service API self-contained.
        if status in {"running", "interrupted"}:
            repo.prepare_interrupted_batch(batch_id)
        elif status == "cancelled":
            if not repo.prepare_cancelled_batch_for_restart(batch_id):
                return False, "已取消批次暂时无法重新启动"
        cancel = threading.Event()
        worker = threading.Thread(
            target=_thread_main,
            args=(batch_id, cancel),
            name=f"model-lab-{batch_id}",
            daemon=True,
        )
        _BATCH_CANCEL[batch_id] = cancel
        _BATCH_THREADS[batch_id] = worker
        running = repo.update_batch_status(batch_id, "running")
        if not running or str(running.get("status") or "") != "running":
            _BATCH_CANCEL.pop(batch_id, None)
            _BATCH_THREADS.pop(batch_id, None)
            return False, "批次状态已变化，请刷新后重试"
        worker.start()
    return True, "评测批次已启动"


def recover_interrupted_batches() -> list[str]:
    """Resume durable Model Lab work orphaned by a previous process.

    Intentionally pending (never-started) batches are left alone.  Each claimed
    batch is checkpoint-normalized before a worker is launched, and completed
    child results are skipped by the regular idempotent execution path.
    """
    resumed: list[str] = []
    for batch_id in repo.list_interrupted_batch_ids():
        with _WORKERS_LOCK:
            current = _BATCH_THREADS.get(batch_id)
            if current is not None and current.is_alive():
                continue
        if not repo.prepare_interrupted_batch(batch_id):
            continue
        ok, _message = start_batch(batch_id)
        if ok:
            resumed.append(batch_id)
    return resumed


def cancel_batch(batch_id: str) -> tuple[bool, str]:
    from ..event_backtest import orchestrator as orch

    worker: threading.Thread | None = None
    # Match history deletion's lock order.  Publishing cancellation and
    # cancelling every currently-linked Run are one relationship transition;
    # deletion cannot observe the half-applied state between those operations.
    with db.HISTORY_RELATION_LOCK, _WORKERS_LOCK, orch.BT_RUN_LIFECYCLE_LOCK:
        batch = repo.get_batch(batch_id, include_tasks=True)
        if not batch:
            return False, "评测批次不存在"
        if str(batch.get("status") or "") in {"done", "failed", "cancelled"}:
            return False, f"status={batch.get('status')} 的批次不能取消"
        cancel = _BATCH_CANCEL.get(batch_id)
        if cancel:
            cancel.set()
        worker = _BATCH_THREADS.get(batch_id)
        repo.cancel_open_tasks(batch_id)
        repo.update_batch_status(batch_id, "cancelled")
        for task in batch.get("tasks") or []:
            run_id = str(task.get("backtest_run_id") or "")
            if not run_id:
                continue
            run = db.get_bt_run(run_id) or {}
            if str(run.get("status") or "") == "pending":
                db.update_bt_run_status(run_id, "cancelled")
            else:
                # BT_RUN_LIFECYCLE_LOCK is an RLock, so the public lifecycle
                # entry remains safe while preserving the global lock order.
                orch.cancel_bt_run(run_id)
    # Give the now-cooperative batch coroutine a short chance to unregister so
    # an immediately following history deletion does not see a phantom worker.
    # Never join while holding _WORKERS_LOCK: _thread_main.finally needs it.
    if worker is not None and worker is not threading.current_thread():
        worker.join(timeout=0.5)
    if worker is not None and worker.is_alive():
        return True, "取消信号已发送，后台调用正在清理"
    return True, "评测批次已取消"


def delete_history_records(run_ids: list[str], batch_ids: list[str]) -> tuple[bool, str, dict[str, Any]]:
    """Delete a mixed Run/batch selection after one all-or-nothing preflight."""
    from ..event_backtest import orchestrator as orch

    with db.HISTORY_RELATION_LOCK, _WORKERS_LOCK, orch.BT_RUN_LIFECYCLE_LOCK:
        plan = repo.plan_history_record_deletion(run_ids, batch_ids)
        if plan["missing_run_ids"] or plan["missing_batch_ids"]:
            return False, "部分记录已不存在，请刷新列表后重新选择", {"reason": "not_found", **plan}

        active_batches = [
            item for item in plan["batches"]
            if str(item.get("status") or "") in _HISTORY_DELETE_BLOCKED_STATUSES
            or bool((worker := _BATCH_THREADS.get(str(item.get("id") or ""))) and worker.is_alive())
        ]
        active_runs = [
            item for item in plan["runs"]
            if str(item.get("status") or "") in _HISTORY_DELETE_BLOCKED_STATUSES
            or orch.is_bt_run_worker_blocking_history_delete(
                str(item.get("id") or ""), str(item.get("status") or ""),
            )
        ]
        if active_runs or active_batches:
            names = [str(item.get("name") or item.get("id")) for item in [*active_runs, *active_batches]][:3]
            suffix = " 等" if len(active_runs) + len(active_batches) > len(names) else ""
            return False, f"运行中的记录不能删除：{'、'.join(names)}{suffix}。请先取消并等待任务停止", {"reason": "active", **plan}
        if plan["external_tasks"]:
            names = list(dict.fromkeys(str(item.get("batch_name") or item.get("batch_id")) for item in plan["external_tasks"]))
            return False, f"所选运行属于基模评测批次「{'、'.join(names[:3])}」，请在运行记录页选择对应批次后删除", {"reason": "model_lab_dependency", **plan}
        if plan["sidecar_parents"]:
            names = [str(item.get("run_name") or item.get("run_id")) for item in plan["sidecar_parents"]]
            return False, f"问答批次属于事件运行「{'、'.join(names[:3])}」，请删除对应事件运行记录", {"reason": "event_dependency", **plan}
        if plan["arena_blockers"]:
            names = [str(item.get("name") or item.get("id")) for item in plan["arena_blockers"]]
            suffix = " 等" if len(names) > 3 else ""
            return False, f"所选记录正在被 Arena「{'、'.join(names[:3])}{suffix}」引用，请先删除对应 Arena", {"reason": "arena_dependency", **plan}

        committed = repo.commit_history_record_deletion(
            run_ids,
            batch_ids,
            expected_run_ids=plan["run_ids"],
            expected_batch_ids=plan["batch_ids"],
        )
        if not committed.get("ok"):
            return False, "记录关联关系刚刚发生变化，请刷新列表后重新选择", {"reason": "dependencies_changed", **committed}
        return True, "运行记录已删除", committed


_SCORE_DIMENSIONS = (
    "fact", "evidence", "method", "reasoning", "risk", "usability",
    "reproducibility", "user_value",
)


def batch_results(batch_id: str) -> dict[str, Any] | None:
    batch = repo.get_batch(batch_id, include_tasks=True)
    if not batch:
        return None
    tasks = list(batch.get("tasks") or [])
    task_by_id = {str(item.get("id") or ""): item for item in tasks}
    profiles = {str(item["id"]): item for item in repo.list_profiles(public=True)}

    def task_provenance(task: Mapping[str, Any] | None) -> dict[str, Any]:
        task = task or {}
        config = task.get("config") if isinstance(task.get("config"), Mapping) else {}
        snapshot = config.get("profile_snapshot") if isinstance(config.get("profile_snapshot"), Mapping) else {}
        profile_id = str(snapshot.get("id") or task.get("profile_id") or "")
        fallback = profiles.get(profile_id) or {}
        return {
            "profile_id": profile_id,
            "profile_name": snapshot.get("name") or fallback.get("name") or profile_id,
            "model_id": snapshot.get("model_id") or fallback.get("model_id"),
            "provider": snapshot.get("provider") or fallback.get("provider"),
        }

    def judge_provenance(task: Mapping[str, Any] | None) -> dict[str, Any]:
        task = task or {}
        config = task.get("config") if isinstance(task.get("config"), Mapping) else {}
        snapshot = (
            config.get("judge_profile_snapshot")
            if isinstance(config.get("judge_profile_snapshot"), Mapping) else {}
        )
        judge_id = str(snapshot.get("id") or "")
        fallback = profiles.get(judge_id) or {}
        return {
            "judge_profile_id": judge_id or None,
            "judge_profile_name": snapshot.get("name") or fallback.get("name"),
            "judge_model_id": snapshot.get("model_id") or fallback.get("model_id"),
            "judge_provider": snapshot.get("provider") or fallback.get("provider"),
        }

    answers: list[dict[str, Any]] = []
    for raw_answer in repo.list_qa_results(batch_id=batch_id):
        answer = dict(raw_answer)
        task = task_by_id.get(str(answer.get("task_id") or ""))
        answer.update(task_provenance(task))
        answer.update(judge_provenance(task))
        task_config = (task or {}).get("config") or {}
        answer["question"] = next((
            dict(question) for question in task_config.get("questions") or []
            if str(question.get("id") or "") == str(answer.get("question_id") or "")
        ), None)
        answers.append(answer)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for answer in answers:
        grouped[(str(answer.get("profile_id") or ""), str(answer.get("variant") or ""))].append(answer)

    qa_comparison: list[dict[str, Any]] = []
    for (profile_id, variant), rows in sorted(grouped.items()):
        scored = [row for row in rows if isinstance(row.get("final_score"), dict)]
        dimension_averages = {
            key: round(sum(float(row["final_score"].get(key) or 0) for row in scored) / len(scored), 3)
            for key in _SCORE_DIMENSIONS
        } if scored else {key: None for key in _SCORE_DIMENSIONS}
        latencies = [int(row.get("latency_ms") or 0) for row in rows if row.get("latency_ms") is not None]
        provenance = {
            key: rows[0].get(key)
            for key in (
                "profile_name", "model_id", "provider", "judge_profile_id",
                "judge_profile_name", "judge_model_id", "judge_provider",
            )
        }
        qa_comparison.append({
            "profile_id": profile_id,
            **provenance,
            "variant": variant,
            "answer_count": len(rows),
            "scored_count": len(scored),
            "failed_count": sum(1 for row in rows if row.get("status") in {"failed", "partial"}),
            "awaiting_manual_count": sum(1 for row in rows if row.get("status") == "awaiting_manual"),
            "average_total": round(
                sum(float(row["final_score"].get("total") or 0) for row in scored) / len(scored), 3
            ) if scored else None,
            "dimension_averages": dimension_averages,
            "major_error_rate": round(
                sum(1 for row in scored if row["final_score"].get("major_error")) / len(scored), 4
            ) if scored else None,
            "average_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "total_cost": round(sum(float(row.get("cost") or 0) for row in rows), 8),
        })

    predictions = []
    for task in tasks:
        if task.get("kind") != "prediction":
            continue
        summary = dict(task.get("result_summary") or {})
        linked_run_id = summary.get("backtest_run_id") or summary.get("bt_run_id") or task.get("backtest_run_id")
        if linked_run_id and not summary.get("demo"):
            linked_run = db.get_bt_run(str(linked_run_id))
            if linked_run:
                refreshed = _prediction_summary(linked_run)
                summary.update(refreshed)
                # Refresh the response only; never rewrite historical task
                # snapshots or restart a completed model while viewing results.
                task["result_summary"] = summary
                task["warning_count"] = summary.get("warning_count", 0)
                task["completion_quality"] = summary.get("completion_quality")
                task["arena_eligible"] = bool(summary.get("arena_eligible"))
        is_demo = bool(summary.get("demo", task.get("demo", False)))
        arena_eligible = bool(
            summary.get("arena_eligible", task.get("arena_eligible", False))
            and linked_run_id
            and not is_demo
        )
        predictions.append({
            **summary,
            "task_id": task.get("id"),
            **task_provenance(task),
            "status": task.get("status"),
            "backtest_run_id": linked_run_id,
            "bt_run_id": linked_run_id,
            "arena_eligible": arena_eligible,
            "demo": is_demo,
            "metrics": summary.get("metrics"),
            "protocol_hash": summary.get("protocol_hash"),
        })

    batch.update(repo.prediction_batch_quality(tasks))
    batch_is_demo = bool(batch.get("demo", (batch.get("scoring") or {}).get("dry_run", False)))
    return {
        "batch": batch,
        "tasks": tasks,
        "qa_results": answers,
        "summary": {
            "demo": batch_is_demo,
            "formal_results": not batch_is_demo,
            "prediction_runs": predictions,
            "qa_comparison": qa_comparison,
            "score_dimensions": [
                {"key": "fact", "label": "事实与数值", "max": 30},
                {"key": "evidence", "label": "证据质量", "max": 20},
                {"key": "method", "label": "方法适配", "max": 15},
                {"key": "reasoning", "label": "推理", "max": 10},
                {"key": "risk", "label": "风险边界", "max": 10},
                {"key": "usability", "label": "可用性", "max": 5},
                {"key": "reproducibility", "label": "可复现性", "max": 5},
                {"key": "user_value", "label": "用户价值", "max": 5},
            ],
        },
    }
