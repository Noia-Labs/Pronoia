"""BacktestOrchestrator: Web 版回测调度器。

核心职责（零侵入，不修改 engine.py / application.py 任何一行）：
1. 包装 3 种 runner（baseline / team_prompt / team_full），统一 per-event callback
2. 回测生命周期管理：pending → running → done/failed，写 bt_runs 表状态
3. 每完成 N 个事件自动调用 compute_metrics → 写 bt_metrics_snapshots + 推送 SSE
4. SSE 多 client 广播：同一 run_id 所有订阅者共享进度流

V8 稳定性约束（project_memory）：
- 单进程，全局 _V8_GUARD_LOCK，同一时刻只有 1 个 active team_full run
- 每 run 内部 concurrency ≤ 2（参数强制 clamp）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import threading
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from .. import db
from .application import load_events, validate_events, write_jsonl
from .cancellation import BacktestCancelled
from .engine import normalize_event_prediction_output, run_baseline
from .metrics import MetricsSummary, compute_metrics
from .models import Direction, EventRecord, TeamPrediction

_CLIENTS_LOCK = threading.RLock()
# Serialize start/pause/resume/cancel with destructive history operations.
# Workers do not hold this lock while running; deletion separately rejects any
# live worker before touching its durable row.
BT_RUN_LIFECYCLE_LOCK = threading.RLock()
# run_id -> list of queues (each queue ~ 1 SSE connection)
_SSE_CLIENT_QUEUES: dict[str, list[asyncio.Queue[dict]]] = {}
# 全局 V8 互斥（team_full runner 用）：防止 py_mini_racer 在多并发/多进程下崩溃
_V8_GUARD_LOCK = threading.Lock()
# 正在 running 的 run_id -> threading.Thread（后台任务）
_RUN_TASKS: dict[str, threading.Thread] = {}
# run_id -> cancel flag (True 表示立刻在回调点抛错取消)
_RUN_CANCEL: dict[str, bool] = {}
# run_id -> threading.Event: 默认 set() 正常跑；pause() clear()；resume() 再 set()
# worker 在每条事件回调点会轮询该 event；未 set 就 sleep 等待
_RUN_RESUME: dict[str, threading.Event] = {}
# ``team_full`` spends several model/tool rounds inside one event before the
# first prediction can be committed.  Keep that *in-event* activity separate
# from ``done_events`` so the UI can show life without pretending an event has
# completed.  This is intentionally process-local: a restarted worker cannot
# still be executing the old in-flight stage.
_RUN_ACTIVITY: dict[str, dict[str, Any]] = {}

TEAM_FULL_STAGE_META: dict[str, tuple[str, int]] = {
    "planning": ("规划", 1),
    "expert_research": ("专家研究", 2),
    "synthesis": ("综合", 3),
    "review": ("复核", 4),
    "hypothesis_extraction": ("假设提取", 5),
}
TEAM_FULL_STAGE_TOTAL = len(TEAM_FULL_STAGE_META)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _activity_snapshot_locked(run_id: str, *, aggregate_only: bool = False) -> Optional[dict[str, Any]]:
    state = _RUN_ACTIVITY.get(run_id)
    if not state:
        return None
    now_mono = time.monotonic()
    rows: list[dict[str, Any]] = []
    for raw in state.get("active_events", {}).values():
        row = dict(raw)
        started_mono = float(row.pop("_started_mono", now_mono) or now_mono)
        row.pop("_stage_started_mono", None)
        row["elapsed_seconds"] = max(0, int(now_mono - started_mono))
        if aggregate_only:
            for key in ("event_id", "symbol", "market", "title"):
                row.pop(key, None)
            row["privacy"] = "arena_safe"
        rows.append(row)
    rows.sort(key=lambda item: (int(item.get("slot") or 0), str(item.get("started_at") or "")))
    return {
        "runner": "team_full",
        "active_count": len(rows),
        "active_events": rows,
        "updated_at": state.get("updated_at"),
        "stage_total": TEAM_FULL_STAGE_TOTAL,
        "note": "单事件内阶段进度；不会计入已完成事件数",
        **({"privacy": "arena_safe"} if aggregate_only else {}),
    }


def get_run_activity(run_id: str, *, aggregate_only: bool = False) -> Optional[dict[str, Any]]:
    """Return a safe snapshot of currently executing ``team_full`` stages.

    The snapshot is advisory UI state only.  It never changes or derives the
    durable ``done_events`` count.
    """
    with _CLIENTS_LOCK:
        return _activity_snapshot_locked(run_id, aggregate_only=aggregate_only)


def record_team_full_stage(run_id: str, payload: dict[str, Any]) -> None:
    """Record and broadcast one real stage transition from the team engine."""
    event_id = str(payload.get("event_id") or "").strip()
    stage = str(payload.get("stage") or "planning").strip()
    label, index = TEAM_FULL_STAGE_META.get(stage, (stage or "处理中", 0))
    now_mono = time.monotonic()
    now_iso = _utc_now_iso()
    with _CLIENTS_LOCK:
        state = _RUN_ACTIVITY.setdefault(run_id, {"active_events": {}, "updated_at": now_iso})
        active = state.setdefault("active_events", {})
        row = dict(active.get(event_id) or {})
        if not row:
            row = {
                "event_id": event_id,
                "symbol": str(payload.get("symbol") or ""),
                "market": str(payload.get("market") or ""),
                "title": str(payload.get("title") or "")[:160],
                "slot": len(active) + 1,
                "started_at": now_iso,
                "_started_mono": now_mono,
            }
        row.update({
            "stage": stage,
            "stage_label": str(payload.get("stage_label") or label),
            "stage_index": int(payload.get("stage_index") or index),
            "stage_total": TEAM_FULL_STAGE_TOTAL,
            "detail": str(payload.get("detail") or "")[:240],
            "agent": str(payload.get("agent") or "")[:80] or None,
            "updated_at": now_iso,
            "_stage_started_mono": now_mono,
        })
        active[event_id] = row
        state["updated_at"] = now_iso
        snapshot = _activity_snapshot_locked(run_id)
        event_elapsed = max(0, int(now_mono - float(row.get("_started_mono", now_mono) or now_mono)))
    sse_broadcast(run_id, {
        "type": "stage_progress",
        "event_id": event_id,
        "symbol": row.get("symbol"),
        "market": row.get("market"),
        "stage": stage,
        "stage_label": row.get("stage_label"),
        "stage_index": row.get("stage_index"),
        "stage_total": TEAM_FULL_STAGE_TOTAL,
        "detail": row.get("detail"),
        "agent": row.get("agent"),
        "event_elapsed_seconds": event_elapsed,
        "activity": snapshot,
    })


def finish_team_full_event_activity(run_id: str, event_id: str) -> None:
    """Remove an event from advisory activity after its prediction commits."""
    with _CLIENTS_LOCK:
        state = _RUN_ACTIVITY.get(run_id)
        if not state:
            return
        state.get("active_events", {}).pop(str(event_id), None)
        state["updated_at"] = _utc_now_iso()


def clear_run_activity(run_id: str) -> None:
    with _CLIENTS_LOCK:
        _RUN_ACTIVITY.pop(run_id, None)


def _get_resume_event(run_id: str) -> threading.Event:
    ev = _RUN_RESUME.get(run_id)
    if ev is None:
        ev = threading.Event()
        ev.set()  # 默认：允许运行
        _RUN_RESUME[run_id] = ev
    return ev


def _wait_if_paused(run_id: str, poll: float = 0.25) -> None:
    """每条事件回调点之前调用：若处于 paused 状态则阻塞等待 resume，同时检查 cancel。

    - poll: 轮询间隔秒；取消请求会在此粒度内响应
    - 若 cancel 被置 True：抛出 RuntimeError，交给上层 _do_run 捕获写 failed
    """
    ev = _get_resume_event(run_id)
    # 加一层 while，防止"伪唤醒"或竞态
    while not ev.is_set():
        if _RUN_CANCEL.get(run_id):
            raise BacktestCancelled("cancelled by user while paused")
        # timeout 轮询 + 顺便让出 CPU；同时 cancel 也能很快响应
        ev.wait(timeout=poll)


# --------------------------------------------------------------------- utils ----

def _dump_event_basic(ev: EventRecord) -> dict:
    return {
        "event_id": getattr(ev, "event_id", None) or (isinstance(ev, dict) and ev.get("event_id")),
        "symbol": getattr(ev, "symbol", "") or (isinstance(ev, dict) and ev.get("symbol") or ""),
        "market": getattr(ev, "market", "") or (isinstance(ev, dict) and ev.get("market") or ""),
        "event_type_l2": getattr(ev, "event_type_l2", "") or (isinstance(ev, dict) and ev.get("event_type_l2") or ""),
        "title": getattr(ev, "title", "") or (isinstance(ev, dict) and ev.get("title") or ""),
    }


def _neutral_fallback_pred(event_id: str, reason: str = "abort/fallback") -> TeamPrediction:
    return TeamPrediction(
        event_id=event_id,
        pred_direction="neutral",  # type: ignore[arg-type]
        confidence=0.50,
        rationale=f"[fallback] {reason}",
        abstain=True,
        run_id="",
        strategy_metadata={
            "prediction_status": "invalid_output",
            "output_failure_kind": "runner_error",
            "output_validation_errors": ["missing_runner_prediction"],
        },
    )


def enforce_binary_prediction(prediction: TeamPrediction, run_config: dict[str, Any]) -> TeamPrediction:
    """Apply the new binary contract only before committing a new result.

    Existing predictions loaded for resume never enter this function. Legacy
    runs without an explicit binary mode retain their original semantics.
    """
    protocol = run_config.get("evaluation_protocol")
    protocol = protocol if isinstance(protocol, dict) else {}
    if str(run_config.get("direction_mode") or protocol.get("direction_mode") or "") != "binary":
        return prediction

    metadata = dict(prediction.strategy_metadata or {})
    status = str(metadata.get("prediction_status") or "").strip().lower()
    raw_errors = metadata.get("output_validation_errors")
    errors = list(raw_errors) if isinstance(raw_errors, list) else []
    validation = metadata.get("validation")
    technical_failure = bool(
        errors or metadata.get("output_failure_kind")
        or metadata.get("completion_quality") == "invalid"
        or (isinstance(validation, dict) and validation.get("valid") is False)
        or status == "invalid_output"
    )

    def without_forecast() -> None:
        metadata.pop("expected_return_pct", None)
        metadata["return_forecast_status"] = "not_provided"

    if not technical_failure and prediction.abstain and status in {"", "voluntary_abstain"}:
        # An explicit strategy abstention is neither a direction nor a claim
        # that input data were missing. Preserve that distinct outcome.
        metadata["prediction_status"] = "voluntary_abstain"
        without_forecast()
        return replace(prediction, strategy_metadata=metadata, expected_return_pct=None)

    normalized = normalize_event_prediction_output({
        "prediction_status": status or "available",
        "pred_direction": prediction.pred_direction,
        "confidence": prediction.confidence,
        "rationale": prediction.rationale,
        "expected_return_pct": prediction.expected_return_pct,
    })
    if not technical_failure:
        errors.extend(normalized["errors"])
        if status == "available" and prediction.abstain:
            errors.append("available_prediction_abstains")
    if technical_failure or errors:
        if not errors:
            errors.append("invalid_model_output")
        metadata.update({
            "prediction_status": "invalid_output",
            "completion_quality": "invalid",
            "output_validation_errors": errors,
            "validation": {"valid": False, "errors": errors},
        })
        without_forecast()
        explanation = "输出不符合本次看涨/看跌预测协议，已记录为无效输出。"
        rationale = str(prediction.rationale or "").strip()
        return replace(
            prediction, pred_direction="neutral", confidence=0.5,
            abstain=True, expected_return_pct=None, strategy_metadata=metadata,
            rationale=(rationale + "\n" + explanation).strip(),
        )

    metadata["prediction_status"] = normalized["prediction_status"]
    if normalized["abstain"]:
        without_forecast()
    return replace(
        prediction, pred_direction=normalized["direction"],
        confidence=normalized["confidence"], rationale=normalized["rationale"],
        abstain=normalized["abstain"], expected_return_pct=normalized["expected_return_pct"],
        strategy_metadata=metadata,
    )


# ------------------------------------------------------------- SSE pub/sub ----

def sse_subscribe(run_id: str) -> asyncio.Queue[dict]:
    """新 SSE 客户端订阅：返回一个 Queue，Orchestrator 会向此 Queue put 事件。"""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=1024)
    with _CLIENTS_LOCK:
        _SSE_CLIENT_QUEUES.setdefault(run_id, [])
        _SSE_CLIENT_QUEUES[run_id].append(q)
    return q


def sse_unsubscribe(run_id: str, q: asyncio.Queue[dict]) -> None:
    with _CLIENTS_LOCK:
        qs = _SSE_CLIENT_QUEUES.get(run_id) or []
        if q in qs:
            qs.remove(q)


def sse_broadcast(run_id: str, evt: dict) -> None:
    """把进度事件广播到所有已订阅的 SSE 队列。"""
    evt = {"run_id": run_id, **evt}
    with _CLIENTS_LOCK:
        qs = list(_SSE_CLIENT_QUEUES.get(run_id) or [])
    for q in qs:
        try:
            q.put_nowait(evt)
        except Exception:
            # 满了就丢弃最旧一条再试（兜底，不阻塞调度线程）
            try:
                q.get_nowait()
                q.put_nowait(evt)
            except Exception:
                pass


# ------------------------------------------------------------- metrics sync ----

def _oracle_values_for_horizon(label: Any, horizon: str) -> tuple[Optional[str], Optional[float]]:
    """Read the run's frozen Oracle window from an ``EventLabel`` safely.

    The database column names retain their historical ``*_t3`` spelling for a
    compatible migration, but their values represent the run's explicitly
    selected primary Oracle horizon.
    """
    from .evaluation_protocol import normalize_evaluation_horizon

    selected = normalize_evaluation_horizon(horizon)
    raw_label = getattr(label, f"label_{selected}", None) if label is not None else None
    direction = str(raw_label or "").strip().lower()
    oracle_label = direction if direction in {"up", "down", "neutral"} else None
    raw_car = getattr(label, f"car_{selected}", None) if label is not None else None
    try:
        oracle_car = float(raw_car) if raw_car is not None else None
    except (TypeError, ValueError):
        oracle_car = None
    if oracle_car is not None and not math.isfinite(oracle_car):
        oracle_car = None
    return oracle_label, oracle_car


def _sync_labels_for_predictions(
    run_id: str,
    predictions: list[TeamPrediction],
    labels_path: Optional[str | Path],
    *,
    primary_oracle_horizon: str = "t3",
) -> None:
    """回填所选主窗口 Oracle；旧 ``*_t3`` DB 列仅作为兼容存储槽。"""
    if not labels_path:
        return
    try:
        from .application import load_labels
        labels_map = {lbl.event_id: lbl for lbl in load_labels(labels_path)}
    except Exception:
        return
    for p in predictions:
        lbl = labels_map.get(p.event_id)
        if not lbl:
            continue
        oracle_label, oracle_car = _oracle_values_for_horizon(lbl, primary_oracle_horizon)
        abstain = bool(getattr(p, "abstain", False))
        is_correct: Optional[bool] = None
        if oracle_label and oracle_car is not None and not abstain:
            # 与 compute_metrics 的 strict 口径完全一致：Oracle neutral
            # 以及预测 neutral 都进入分母且计错；只有明确命中
            # up/down 才计对。
            is_correct = bool(
                oracle_label in {"up", "down"}
                and p.pred_direction in {"up", "down"}
                and p.pred_direction == oracle_label
            )
        updated = db.update_bt_prediction_oracle(
            run_id,
            p.event_id,
            oracle_label_t3=oracle_label,
            oracle_car_t3=oracle_car,
            is_correct_t3=is_correct,
        )
        # A resumed run may have predictions on disk but no corresponding DB row.
        if not updated:
            db.add_bt_prediction(
                run_id=run_id,
                event_id=p.event_id,
                pred_direction=p.pred_direction,
                confidence=p.confidence,
                abstain=abstain,
                rationale=p.rationale,
                oracle_label_t3=oracle_label,
                oracle_car_t3=oracle_car,
                is_correct_t3=is_correct,
                horizon=getattr(p, "horizon", None),
                strategy_metadata=getattr(p, "strategy_metadata", None),
            )


def _maybe_snapshot(
    run_id: str,
    predictions: list[TeamPrediction],
    labels_path: Optional[str | Path],
    *,
    done_count: int,
    force: bool = False,
    primary_oracle_horizon: str = "t3",
    allowed_event_ids: Optional[set[str]] = None,
    oracle_epsilon: float = 0.005,
) -> Optional[MetricsSummary]:
    """每完成 5 个事件（或 force=True）调用 compute_metrics 并写快照 + 推送 SSE。

    核心约束：done_events 必须更新，不能因为 labels / metrics 异常导致进度卡在 0。
    进度写 DB 与 metrics 快照解耦：metrics 失败只影响 acc 字段，done_events 照常推进。
    """
    should = force or (done_count % 5 == 0 and done_count > 0)
    # 进度写盘永远做（只要 done_count > 0 或 force），避免因 metrics 异常进度一直为 0
    if done_count > 0 or force:
        try:
            db.update_bt_run_progress(run_id, done_events=done_count)
        except Exception:
            pass
    if not should:
        return None
    labels: list = []
    if labels_path:
        try:
            from .application import load_labels
            labels = load_labels(labels_path)
        except Exception:
            labels = []
    if allowed_event_ids is not None:
        predictions = [p for p in predictions if p.event_id in allowed_event_ids]
        labels = [label for label in labels if label.event_id in allowed_event_ids]
    summary: Optional[MetricsSummary] = None
    try:
        summary = compute_metrics(
            predictions=predictions,
            labels=labels,
            epsilon=oracle_epsilon,
            primary_oracle_horizon=primary_oracle_horizon,
        )
    except Exception:
        summary = None
    if summary:
        try:
            snap_id = db.add_bt_metrics_snapshot(
                run_id=run_id,
                done_count=done_count,
                acc_t3_strict=summary.acc_t3_strict.acc,
                acc_t3_strict_lo=summary.acc_t3_strict.wilson_lo_95,
                acc_t3_non_neutral=summary.acc_t3_non_neutral.acc,
                neutral_ratio=(summary.n_abstain_pred / summary.n_total) if summary.n_total else 0.0,
            )
            sse_broadcast(run_id, {
                "type": "metrics_snapshot",
                "snapshot_id": snap_id,
                "done_count": done_count,
                "acc_t3_strict": summary.acc_t3_strict.acc,
                "acc_t3_strict_lo": summary.acc_t3_strict.wilson_lo_95,
                "acc_t3_non_neutral": summary.acc_t3_non_neutral.acc,
                "neutral_ratio": (summary.n_abstain_pred / summary.n_total) if summary.n_total else 0.0,
                "primary_oracle_horizon": primary_oracle_horizon,
                "acc_primary_non_neutral": summary.acc_primary_non_neutral.acc,
            })
            db.update_bt_run_progress(
                run_id,
                done_events=done_count,
                acc_t3_strict=summary.acc_t3_strict.acc,
                acc_t3_strict_lo=summary.acc_t3_strict.wilson_lo_95,
                acc_t3_non_neutral=summary.acc_t3_non_neutral.acc,
            )
        except Exception:
            pass
    return summary


# -------------------------------------------------------- per-runner wrappers ----

def _run_baseline_with_cb(
    events: list[EventRecord],
    *,
    run_id: str,
    model_version: str,
    target_horizon: str,
    on_pred: Callable[[TeamPrediction, EventRecord], None],
) -> list[TeamPrediction]:
    """baseline runner 本身是批量的，拆成逐事件调用保证进度可见。

    调试/验证 pause/resume：设置环境变量 FEVER_BT_SLEEP_PER_EVENT=1.2 可让每条事件后 sleep N 秒，
    模拟真实慢回测。
    """
    # baseline 内部其实没有 per-event LLM；但我们对每个事件独立调 run_baseline([ev])
    # 仍然是 O(N) 且不破坏现有返回结构
    import os as _os
    _sleep_s = 0.0
    try:
        _sleep_s = float(_os.environ.get("FEVER_BT_SLEEP_PER_EVENT") or "0")
    except Exception:
        _sleep_s = 0.0
    from .engine import run_baseline
    outs: list[TeamPrediction] = []
    for ev in events:
        ps = run_baseline(
            [ev], run_id=run_id,
            model_version=model_version or "event-baseline-v0",
            target_horizon=target_horizon,
        )
        for p in ps:
            outs.append(p)
            on_pred(p, ev)
        if _sleep_s > 0:
            import time as _time
            _time.sleep(_sleep_s)
    return outs


def _run_provided_analysis_with_cb(
    events: list[EventRecord],
    *,
    run_id: str,
    model_version: str,
    target_horizon: str,
    on_pred: Callable[[TeamPrediction, EventRecord], None],
) -> list[TeamPrediction]:
    """Turn user/third-party analyses embedded in events into standard decisions."""
    from .evaluation_protocol import normalize_evaluation_horizon

    selected_horizon = normalize_evaluation_horizon(target_horizon)
    outs: list[TeamPrediction] = []
    for ev in events:
        direction = str(getattr(ev, "analysis_direction", "") or "").strip().lower()
        if direction not in {"up", "down", "neutral"}:
            raise ValueError(f"event_id={ev.event_id} 缺少有效 analysis_direction")
        confidence = getattr(ev, "analysis_confidence", None)
        if confidence is None or not 0.0 <= float(confidence) <= 1.0:
            raise ValueError(f"event_id={ev.event_id} 缺少 0..1 analysis_confidence")
        rationale = str(getattr(ev, "analysis_rationale", "") or "").strip()
        if not rationale:
            raise ValueError(f"event_id={ev.event_id} 缺少 analysis_rationale")
        declared_raw = getattr(ev, "analysis_horizon", None)
        declared_horizon = normalize_evaluation_horizon(declared_raw or selected_horizon)
        if declared_horizon != selected_horizon:
            raise ValueError(
                f"event_id={ev.event_id} 的 analysis_horizon={declared_horizon}，"
                f"与冻结评测窗口 {selected_horizon} 不一致"
            )
        pred = TeamPrediction(
            event_id=ev.event_id,
            pred_direction=direction,  # type: ignore[arg-type]
            run_id=run_id,
            model_version=model_version or "provided-analysis-v1",
            confidence=float(confidence),
            rationale=rationale,
            # "neutral" is an explicit investment conclusion, not an execution
            # failure. Keep it separate from abstain so coverage and Arena
            # scoring treat human and model neutral decisions consistently.
            abstain=False,
            horizon=selected_horizon,
            expected_return_pct=ev.analysis_expected_return_pct,
        )
        outs.append(pred)
        on_pred(pred, ev)
    return outs


def _run_external_event_http_with_cb(
    events: list[EventRecord],
    *,
    run_id: str,
    model_version: str,
    strategy_spec: dict[str, Any],
    target_horizon: str,
    on_pred: Callable[[TeamPrediction, EventRecord], None],
    before_request: Optional[Callable[[], None]] = None,
) -> list[TeamPrediction]:
    from .external_strategy import run_external_event_strategy

    by_id = {event.event_id: event for event in events}

    def commit_prediction(prediction: TeamPrediction) -> None:
        event = by_id.get(prediction.event_id)
        if event is None:
            raise ValueError(f"external event strategy returned unknown event_id={prediction.event_id}")
        on_pred(prediction, event)

    return run_external_event_strategy(
        events,
        run_id=run_id,
        spec=strategy_spec,
        model_version=model_version or str(strategy_spec.get("version") or "external-event-v1"),
        target_horizon=target_horizon,
        before_request=before_request,
        on_pred=commit_prediction,
    )


def _run_team_prompt_with_cb(
    events: list[EventRecord],
    *,
    run_id: str,
    model_version: str,
    concurrency: int,
    skip_event_ids: set[str],
    system_prompt_variant: str,
    target_horizon: str,
    on_pred: Callable[[TeamPrediction, EventRecord], None],
) -> list[TeamPrediction]:
    """直接复用 engine.run_team_prompt 的 on_pred_callback，再查回 event record。"""
    from .engine import run_team_prompt
    ev_map = {getattr(e, "event_id", ""): e for e in events}

    def _cb(p: TeamPrediction) -> None:
        ev = ev_map.get(p.event_id)
        if ev is None:
            ev = EventRecord(event_id=p.event_id, symbol="", market="", event_time="",
                             event_type_l2="", title="", event_text="", source_url="")
        on_pred(p, ev)

    preds = asyncio.run(
        run_team_prompt(
            events,
            run_id=run_id,
            model_version=model_version,
            concurrency=concurrency,
            skip_event_ids=skip_event_ids,
            system_prompt_variant=system_prompt_variant,
            target_horizon=target_horizon,
            on_pred_callback=_cb,
        )
    )
    return list(preds)


def _run_team_full_with_cb(
    events: list[EventRecord],
    *,
    run_id: str,
    model_version: str,
    concurrency: int,
    skip_event_ids: set[str],
    system_prompt_variant: str,
    target_horizon: str,
    trajectory_ckpt_dir: Path,
    on_pred: Callable[[TeamPrediction, EventRecord], None],
    on_stage: Callable[[dict[str, Any]], None],
    cancel_check: Callable[[], bool],
) -> list[TeamPrediction]:
    """调用 engine.run_team_full_trajectory；通过原生 on_pred_callback 做到每条 case 完成即实时推进度
    （LLM 调用完成 → engine 立即 callback → _on_pred 写 DB / 写盘 / SSE / done_events++，
    而不是等全部跑完再批量 push，解决「一直 0/N → 突然 N/N 跳变」观感问题）。

    pause/cancel 阻塞在 orchestrator 的 _on_pred 内部统一处理（每条 pred 完成后 _wait_if_paused + cancel_flag 检查），
    不会把 pause 逻辑穿进 engine 内部，保持 engine 独立可测。
    """
    from .engine import run_team_full_trajectory

    ev_map = {getattr(e, "event_id", ""): e for e in events}
    ev_map[""] = EventRecord(
        event_id="", symbol="", market="", event_time="",
        event_type_l2="", title="", event_text="", source_url="",
    )

    seen_eids: set[str] = set()
    ordered: list[TeamPrediction] = []

    def _cb(p: TeamPrediction) -> None:
        ev = ev_map.get(p.event_id, ev_map[""])
        seen_eids.add(p.event_id)
        ordered.append(p)  # 按 callback 到达顺序（真实完成顺序，后续 main 返回用 events 原始 order 兜底）
        on_pred(p, ev)

    preds = asyncio.run(
        run_team_full_trajectory(
            events,
            run_id=run_id,
            model_version=model_version,
            concurrency=concurrency,
            skip_event_ids=skip_event_ids,
            system_prompt_variant=system_prompt_variant,
            target_horizon=target_horizon,
            trajectory_ckpt_dir=trajectory_ckpt_dir,
            on_pred_callback=_cb,
            on_stage_callback=on_stage,
            cancel_check=cancel_check,
        )
    )
    # 兜底：skip_event_ids 里的事件没有走 callback（engine 直接跳过），但需要返回给上层让 out_path 完整。
    # 另外，preds 按 remaining 原始 idx 顺序，我们用 preds 做最终返回保证顺序稳定；
    # 对 engine 产出但 callback 没触达（异常边界保护）的那些，再补一次 on_pred 以防漏写 DB。
    remaining = [e for e in events if getattr(e, "event_id", None) not in skip_event_ids]
    pred_map = {p.event_id: p for p in preds}
    final_ordered: list[TeamPrediction] = []
    for ev in remaining:
        eid = getattr(ev, "event_id", None) or ""
        p = pred_map.get(eid)
        if not p:
            p = _neutral_fallback_pred(eid, reason="team_full missing prediction (abstain)")
        final_ordered.append(p)
        if p.event_id and p.event_id not in seen_eids:
            on_pred(p, ev)
    return final_ordered


# --------------------------------------------------------- main entry point ----

@dataclass
class BacktestStartResult:
    ok: bool
    run_id: str
    error: Optional[str] = None


def _is_local_portfolio_run(run: dict[str, Any]) -> bool:
    """Only frozen local numeric strategies can bypass the shared V8 guard."""
    spec = run.get("strategy_spec") or {}
    if run.get("engine_mode") != "portfolio" or spec.get("type") != "quant":
        return False
    kind = spec.get("kind")
    if kind == "return_forecast":
        return spec.get("source", "builtin") in {"builtin", "signal_file"}
    return kind in {"buy_hold", "ma_cross", "momentum", "declarative_rules", "signal_file"}


def start_bt_run(run_id: str) -> BacktestStartResult:
    with BT_RUN_LIFECYCLE_LOCK:
        return _start_bt_run_locked(run_id)


def _start_bt_run_locked(run_id: str) -> BacktestStartResult:
    """启动/恢复一个回测 run：pending / paused / 没有活跃线程的 stuck run 都会起新线程。

    设计目标：
    - 刷新页面后（后台线程仍在跑）：不会重复起线程
    - server 重启后 running/paused stuck run：用户点 Continue → 重新启线程，
      _do_run 会从 out_path + ckpt_dir 扫已完成事件（skip_event_ids），不会从头重算
    """
    run = db.get_bt_run(run_id)
    if not run:
        return BacktestStartResult(ok=False, run_id=run_id, error=f"run_id={run_id} not found")
    from .datasets import verify_run_frozen_inputs
    frozen_ok, frozen_error = verify_run_frozen_inputs(run)
    if not frozen_ok:
        return BacktestStartResult(
            ok=False, run_id=run_id,
            error=f"frozen dataset/protocol verification failed: {frozen_error}",
        )

    current = str(run.get("status") or "")
    active_thread = _RUN_TASKS.get(run_id)
    if active_thread is not None and active_thread.is_alive():
        return BacktestStartResult(ok=True, run_id=run_id, error=f"already active, status={current}")

    # 允许从 pending / paused / running(stuck) / failed / cancelled 启动；
    # done 是终态，不允许再启动。failed/cancelled 启动会走 resume 续算（跳过已完成事件）。
    if current not in {"pending", "paused", "running", "failed", "cancelled"}:
        return BacktestStartResult(ok=False, run_id=run_id, error=f"cannot start from status={current}")

    # 参数 clamp：V8 稳定性硬约束（project_memory）
    effective_concurrency = min(2, max(1, int(run.get("concurrency") or 1)))

    # 初始化 resume 事件：默认 set(允许运行)
    _get_resume_event(run_id).set()
    db.update_bt_run_status(run_id, "running")
    sse_broadcast(run_id, {"type": "run_started", "concurrency": effective_concurrency})
    _RUN_CANCEL[run_id] = False

    def _thread_main() -> None:
        try:
            if _is_local_portfolio_run(run):
                # Frozen CSV + numeric strategies never use a model or JS
                # runtime; a long team run must not block local backtests.
                _do_run(run_id, effective_concurrency)
            else:
                with _V8_GUARD_LOCK:
                    _do_run(run_id, effective_concurrency)
        except BacktestCancelled:
            # cancel_bt_run normally writes this terminal state immediately.
            # Preserve it here instead of misreporting a user cancellation as
            # a failed model run.
            current_run = db.get_bt_run(run_id) or {}
            # History deletion may have removed an acknowledged-cancelled Run
            # while this worker was still returning from an in-flight provider
            # call.  UPDATE would not recreate the row, but avoiding the write
            # and stale SSE makes that absorbing deletion contract explicit.
            if current_run and str(current_run.get("status") or "") != "cancelled":
                db.update_bt_run_status(run_id, "cancelled")
                sse_broadcast(run_id, {"type": "run_cancelled"})
        except Exception as exc:
            with db._lock:
                # A service/calculation can fail while cancellation is already
                # in flight. Keep the user's terminal cancellation state.
                current_run = db.get_bt_run(run_id) or {}
                if not current_run:
                    # An acknowledged cancellation may be deleted before a
                    # slow provider unwinds.  A late exception must stay inert.
                    pass
                elif _RUN_CANCEL.get(run_id) or current_run.get("status") == "cancelled":
                    if current_run.get("status") != "cancelled":
                        db.update_bt_run_status(run_id, "cancelled")
                        sse_broadcast(run_id, {"type": "run_cancelled"})
                else:
                    from ..model_lab.service import _sanitize_text
                    profile = (run.get("config") or {}).get("model_profile_snapshot")
                    error = _sanitize_text(f"{type(exc).__name__}: {exc}", profile)
                    tb = _sanitize_text(traceback.format_exc(), profile)
                    db.update_bt_run_status(run_id, "failed", error_msg=error)
                    sse_broadcast(run_id, {"type": "run_failed", "error": error, "traceback": tb})
        finally:
            clear_run_activity(run_id)
            # Keep registry teardown atomic with start/cancel/history-delete.
            # The identity guard also prevents an old worker from clearing a
            # theoretically reused run id's newer lifecycle state.
            with BT_RUN_LIFECYCLE_LOCK:
                if _RUN_TASKS.get(run_id) is threading.current_thread():
                    _RUN_TASKS.pop(run_id, None)
                    _RUN_CANCEL.pop(run_id, None)
                    _RUN_RESUME.pop(run_id, None)

    t = threading.Thread(target=_thread_main, name=f"bt-run-{run_id}", daemon=True)
    _RUN_TASKS[run_id] = t
    t.start()
    return BacktestStartResult(ok=True, run_id=run_id)


def cancel_bt_run(run_id: str) -> bool:
    with BT_RUN_LIFECYCLE_LOCK:
        return _cancel_bt_run_locked(run_id)


def _cancel_bt_run_locked(run_id: str) -> bool:
    """取消回测：running / paused 状态都允许取消。"""
    # Serialize controls with the portfolio result commit.
    with db._lock:
        run = db.get_bt_run(run_id)
        if not run:
            return False
        status = str(run.get("status") or "")
        if status not in {"running", "paused"}:
            return False
        _RUN_CANCEL[run_id] = True
        # 若当前 paused，先把 resume event set 一下 → wait_if_paused 会立刻抛 cancel，不永久阻塞
        _get_resume_event(run_id).set()
        db.update_bt_run_status(run_id, "cancelled")
        sse_broadcast(run_id, {"type": "run_cancelled"})
        return True


def is_bt_run_worker_active(run_id: str) -> bool:
    """Return whether an in-process worker can still write results for a Run."""
    worker = _RUN_TASKS.get(run_id)
    return bool(worker is not None and worker.is_alive())


def is_bt_run_worker_blocking_history_delete(run_id: str, durable_status: str) -> bool:
    """Return whether a live worker must still block destructive history work.

    The caller holds ``BT_RUN_LIFECYCLE_LOCK``.  ``cancel_bt_run`` publishes the
    process-local cancellation flag and durable ``cancelled`` status while
    holding that same lock plus ``db._lock``.  Event/portfolio result commits
    re-check the flag under ``db._lock``.  Once both signals are visible, the
    worker can only unwind and cannot durably publish another result, so it is
    safe for history deletion to remove the parent row before the provider call
    itself returns.

    A merely cancelled-looking row without this process-local acknowledgement
    remains blocking: it may have been changed by another process or code path
    while the live worker still believes it owns the Run.
    """
    worker = _RUN_TASKS.get(run_id)
    if worker is None or not worker.is_alive():
        return False
    acknowledged_cancel = (
        str(durable_status or "") == "cancelled"
        and _RUN_CANCEL.get(run_id) is True
    )
    return not acknowledged_cancel


def pause_bt_run(run_id: str) -> tuple[bool, str]:
    with BT_RUN_LIFECYCLE_LOCK:
        return _pause_bt_run_locked(run_id)


def _pause_bt_run_locked(run_id: str) -> tuple[bool, str]:
    """暂停：要求 status=running 且线程仍 alive；否则报 409 友好消息。返回 (ok, message)。"""
    # Serialize controls with the portfolio result commit.
    with db._lock:
        run = db.get_bt_run(run_id)
        if not run:
            return False, "run not found"
        status = str(run.get("status") or "")
        if status == "paused":
            return True, "already paused"
        if status != "running":
            return False, f"cannot pause from status={status}"
        t = _RUN_TASKS.get(run_id)
        if t is None or not t.is_alive():
            # 线程已经退出，但 DB 还是 running（stuck）— 直接置为 paused 以便用户后续 Resume
            db.update_bt_run_status(run_id, "paused")
            sse_broadcast(run_id, {"type": "run_status_changed", "from": status, "to": "paused", "reason": "worker_thread_gone"})
            return True, "worker gone, marked paused"
        _get_resume_event(run_id).clear()
        db.update_bt_run_status(run_id, "paused")
        sse_broadcast(run_id, {"type": "run_status_changed", "from": "running", "to": "paused"})
        return True, "paused"


def resume_bt_run(run_id: str) -> tuple[bool, str]:
    with BT_RUN_LIFECYCLE_LOCK:
        return _resume_bt_run_locked(run_id)


def _resume_bt_run_locked(run_id: str) -> tuple[bool, str]:
    """继续：
    - status=paused 且线程 alive → 置位 resume_event
    - status=paused 线程死了 / status=running 线程死了 → 调 start_bt_run 重起线程 + 从 out_path 恢复
    - status=pending → 直接 start_bt_run
    """
    with db._lock:
        run = db.get_bt_run(run_id)
        if not run:
            return False, "run not found"
        status = str(run.get("status") or "")
        if status == "running":
            t = _RUN_TASKS.get(run_id)
            if t and t.is_alive():
                return True, "already running"
            r = start_bt_run(run_id)
            return r.ok, (r.error or "resumed (restart worker, skip done via out_path)")
        if status == "paused":
            t = _RUN_TASKS.get(run_id)
            if t and t.is_alive():
                _get_resume_event(run_id).set()
                db.update_bt_run_status(run_id, "running")
                sse_broadcast(run_id, {"type": "run_status_changed", "from": "paused", "to": "running"})
                return True, "resumed"
            r = start_bt_run(run_id)
            return r.ok, (r.error or "resumed (restart worker, skip done via out_path)")
        if status == "pending":
            r = start_bt_run(run_id)
            return r.ok, (r.error or "started")
        return False, f"cannot resume from status={status}"


def _portfolio_metric_result(
    value: Any,
    *,
    display_name: str,
    higher_is_better: bool,
    sample_size: int,
) -> dict[str, Any]:
    numeric = float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None
    return {
        "value": numeric,
        "display_name": display_name,
        "description": "基于冻结真实 OHLC bar 的单标的 next-open 组合模拟",
        "tier": "extended",
        "higher_is_better": higher_is_better,
        "breakdown": {},
        "meta": {
            "n": max(0, int(sample_size)),
            "method": "single_asset_next_open_portfolio",
            "result_nature": "simulated_from_real_bars",
        },
    }


def _do_portfolio_run(run: dict[str, Any]) -> None:
    """Execute quant/API target-weight adapters inside the unified Run shell."""
    run_id = str(run.get("id") or "")

    def control_check() -> None:
        if _RUN_CANCEL.get(run_id):
            raise BacktestCancelled("cancelled by user")
        _wait_if_paused(run_id)
        if _RUN_CANCEL.get(run_id):
            raise BacktestCancelled("cancelled by user")

    control_check()
    dataset_id = str(run.get("dataset_id") or "")
    dataset_version = str(run.get("dataset_version") or "")
    if not dataset_id or not dataset_version:
        raise ValueError("portfolio Run 缺少冻结 dataset_id/dataset_version")
    dataset = db.get_bt_dataset(dataset_id)
    version = db.get_bt_dataset_version(dataset_id, dataset_version)
    if not dataset or not version:
        raise ValueError("portfolio Run 引用的数据版本不存在")
    if version.get("status") != "available" or not version.get("snapshot_hash"):
        raise ValueError("portfolio Run 的数据版本尚未物化为真实冻结快照")
    contract = dict(dataset)
    for key, value in version.items():
        if key not in {"id", "dataset_id"}:
            contract[key] = value
    contract["dataset_version"] = dataset_version

    execution_contract = run.get("execution_spec") if isinstance(run.get("execution_spec"), dict) else {}
    applied = execution_contract.get("applied") if isinstance(execution_contract.get("applied"), dict) else execution_contract
    from .datasets import load_market_dataset_snapshot
    market_dataset = load_market_dataset_snapshot(
        contract,
        start=str(applied.get("start_date")) if applied.get("start_date") else None,
        end=str(applied.get("end_date")) if applied.get("end_date") else None,
    )
    # Creation only knows the full snapshot size.  Once the applied execution
    # window has been resolved, persist its actual bar count so list progress
    # never shows e.g. 4,800 / 48,480 for a successfully completed window.
    db.update_bt_run_progress(
        run_id,
        done_events=0,
        total_events=len(market_dataset.bars),
    )
    strategy_spec = run.get("strategy_spec") if isinstance(run.get("strategy_spec"), dict) else {}
    native_spec = dict(strategy_spec)
    if strategy_spec.get("type") == "api" and strategy_spec.get("kind") != "return_forecast":
        native_spec["kind"] = "external_http"
    from ..quant_backtest.models import ExecutionConfig
    from ..quant_backtest.strategies import strategy_from_spec
    from ..quant_backtest.engine import run_backtest

    execution = ExecutionConfig(**{
        key: applied.get(key)
        for key in (
            "initial_capital", "commission_bps", "slippage_bps", "stamp_duty_bps",
            "other_cost_bps", "minimum_commission", "max_abs_weight",
            "allow_short", "annualization_periods",
        )
        if applied.get(key) is not None
    })
    strategy = strategy_from_spec(native_spec)
    sse_broadcast(run_id, {
        "type": "run_info", "total_events": len(market_dataset.bars),
        "engine_mode": "portfolio", "dataset_version": dataset_version,
    })
    control_check()
    signals = strategy.generate(market_dataset)
    control_check()
    result = run_backtest(
        market_dataset,
        signals,
        execution=execution,
        strategy=strategy.describe(),
        control_check=control_check,
    )
    control_check()
    # Keep the immutable user request separate from what was actually
    # executable on the frozen snapshot.  In particular, a partially
    # out-of-range date request is valid: the loader intersects it with the
    # available bars, and the result artifact records that effective window
    # instead of presenting the original bounds as though data existed there.
    window_audit = market_dataset.metadata if isinstance(market_dataset.metadata, dict) else {}
    requested_contract = execution_contract.get("requested")
    if not isinstance(requested_contract, dict):
        # Compatibility for legacy portfolio rows that stored a flat contract.
        requested_contract = execution_contract
    recorded_not_applied = execution_contract.get("recorded_not_applied")
    result_execution_contract = {
        "requested": dict(requested_contract),
        "applied": dict(applied),
        "recorded_not_applied": (
            dict(recorded_not_applied) if isinstance(recorded_not_applied, dict) else {}
        ),
    }
    result_applied = result_execution_contract["applied"]
    result_applied.pop("start_date", None)
    result_applied.pop("end_date", None)
    result_applied.update({
        "start_at": window_audit.get("effective_start"),
        "end_at": window_audit.get("effective_end"),
        "available_start": window_audit.get("available_start"),
        "available_end": window_audit.get("available_end"),
        "window_clipped": bool(window_audit.get("window_clipped")),
        "selected_bar_count": int(
            window_audit.get("selected_bar_count") or len(market_dataset.bars)
        ),
    })
    result_dict = result.to_dict()
    result_dict["contract"] = {
        "schema_version": "pronoia-unified-performance-v1",
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "engine_mode": "portfolio",
        "result_nature": "simulated_from_real_bars",
        "execution_protocol": result_execution_contract,
    }
    result_path = Path(str(run.get("result_path") or ""))
    if not str(result_path):
        raise ValueError("portfolio Run 缺少 result_path")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = result_path.with_suffix(result_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(result_dict, ensure_ascii=False, default=str), encoding="utf-8")

    raw_metrics = dict(result.metrics)
    bars_n = int(raw_metrics.get("bar_count") or len(market_dataset.bars))
    trades_n = int(raw_metrics.get("trade_count") or len(result.trades))
    metrics = {
        "return_forecast": {
            "value": (raw_metrics.get("return_forecast") or {}).get("mae_pct"),
            "display_name": "收益率预测误差", "description": "未来 T+N 标的收益率预测误差，单位为百分点",
            "tier": "core", "higher_is_better": False, "breakdown": {},
            "meta": dict(raw_metrics.get("return_forecast") or {}),
        },
        "strategy_total_return": _portfolio_metric_result(
            raw_metrics.get("total_return"), display_name="策略累计收益", higher_is_better=True,
            sample_size=max(1, bars_n - 1),
        ),
        "strategy_max_drawdown": _portfolio_metric_result(
            raw_metrics.get("max_drawdown"), display_name="最大回撤", higher_is_better=False,
            sample_size=max(1, bars_n - 1),
        ),
        "strategy_sharpe_proxy": _portfolio_metric_result(
            raw_metrics.get("sharpe_ratio"), display_name="年化 Sharpe", higher_is_better=True,
            sample_size=max(1, bars_n - 1),
        ),
        "strategy_win_rate": _portfolio_metric_result(
            raw_metrics.get("win_rate"), display_name="活跃周期正收益率", higher_is_better=True,
            sample_size=max(1, int(raw_metrics.get("active_period_count") or trades_n)),
        ),
        "portfolio_metrics": {
            "value": raw_metrics.get("total_return"), "display_name": "组合绩效明细",
            "description": "完整组合指标对象见 meta", "tier": "extended",
            "higher_is_better": True, "breakdown": {},
            "meta": {"n": max(1, bars_n - 1), **raw_metrics},
        },
    }
    if raw_metrics.get("prediction_only"):
        # Zero positions are an implementation detail of a pure forecaster,
        # not a measured trading strategy performance.
        for metric_id in ("strategy_total_return", "strategy_max_drawdown", "strategy_sharpe_proxy", "strategy_win_rate", "portfolio_metrics"):
            metrics[metric_id]["value"] = None
    try:
        while True:
            control_check()
            with db._lock:
                # A pause/cancel may arrive after the preceding check. Their
                # state updates share this lock so a late result never wins.
                if _RUN_CANCEL.get(run_id):
                    raise BacktestCancelled("cancelled by user")
                if not _get_resume_event(run_id).is_set():
                    continue
                temp_path.replace(result_path)
                db.update_bt_run_metrics(run_id, metrics)
                db.update_bt_run_progress(
                    run_id,
                    done_events=len(market_dataset.bars),
                    total_events=len(market_dataset.bars),
                )
                db.update_bt_run_status(run_id, "done")
                sse_broadcast(run_id, {
                    "type": "run_done", "done_count": len(market_dataset.bars),
                    "engine_mode": "portfolio", "metrics": metrics,
                })
                break
    finally:
        temp_path.unlink(missing_ok=True)


def _do_run(run_id: str, effective_concurrency: int) -> None:
    run = db.get_bt_run(run_id)
    if not run:
        raise RuntimeError(f"run_id={run_id} missing in thread")

    if _is_local_portfolio_run(run):
        # Ignore any unrelated model-profile metadata on a numeric Run. This
        # path cannot read or mutate the platform's shared inference context.
        _do_portfolio_run(run)
        return

    run_config = run.get("config") if isinstance(run.get("config"), dict) else {}
    profile = run_config.get("model_profile_snapshot")
    if run.get("runner") == "raw_model":
        # Direct candidates own their transport; they never inherit the
        # platform's LLM context, prompts, agents or mutable default target.
        if not isinstance(profile, dict) or not profile.get("base_url") or not profile.get("model_id"):
            raise ValueError("独立事件模型缺少冻结的模型连接")
        _do_run_loaded(run, effective_concurrency)
        return
    if isinstance(profile, dict) and profile.get("base_url") and profile.get("model_id"):
        # A Model Lab run is pinned to the profile snapshot captured when its
        # batch was created. Changing the platform default or editing the
        # profile later therefore cannot alter a running/historical experiment.
        from ..llm import model_profile_context

        with model_profile_context(profile):
            _do_run_loaded(run, effective_concurrency)
        return
    _do_run_loaded(run, effective_concurrency)


def _do_run_loaded(run: dict[str, Any], effective_concurrency: int) -> None:
    run_id = str(run["id"])

    if str(run.get("engine_mode") or "event_proxy") == "portfolio":
        _do_portfolio_run(run)
        return

    events_path = Path(run["events_path"])
    out_path = Path(run["out_path"])
    labels_path = Path(run["labels_path"]) if run.get("labels_path") else None
    ckpt_dir = Path(run["ckpt_dir"]) if run.get("ckpt_dir") else None

    # --- 1. 加载 & 校验 events ---
    events = load_events(events_path)
    issues = validate_events(events)
    if issues:
        raise ValueError("事件文件校验失败:\n" + "\n".join(issues[:20]))
    from .evaluation_protocol import (
        resolve_run_execution_spec,
        resolve_run_horizon,
        resolve_run_oracle_epsilon,
        select_events_for_execution_window,
    )
    execution_spec = resolve_run_execution_spec(run)
    event_selection = select_events_for_execution_window(events, execution_spec)
    events = event_selection.events
    if event_selection.active and not events:
        raise ValueError("执行日期区间内没有可回测事件")
    primary_oracle_horizon = resolve_run_horizon(run)
    oracle_epsilon = resolve_run_oracle_epsilon(run)
    allowed_event_ids = event_selection.event_ids if event_selection.active else None

    with db._lock:
        current_run = db.get_bt_run(run_id)
        if _RUN_CANCEL.get(run_id) or not current_run or current_run.get("status") == "cancelled":
            raise BacktestCancelled("cancelled by user")
        db.update_bt_run_progress(run_id, done_events=0)
        sse_broadcast(run_id, {"type": "run_info", "total_events": len(events)})

    # --- 2. resume 逻辑（从 out_path + ckpt_dir 扫描已完成） ---
    from .application import load_predictions
    existing: list[TeamPrediction] = []
    skip_event_ids: set[str] = set()
    if out_path.exists():
        try:
            existing = load_predictions(out_path)
            if allowed_event_ids is not None:
                existing = [p for p in existing if p.event_id in allowed_event_ids]
            skip_event_ids = {p.event_id for p in existing if p.event_id}
        except Exception:
            existing, skip_event_ids = [], set()
    if run["runner"] == "team_full" and ckpt_dir and ckpt_dir.exists() and ckpt_dir.is_dir():
        for fn in ckpt_dir.iterdir():
            if fn.is_file() and fn.suffix == ".json" and not fn.name.startswith("."):
                ev_id = fn.stem.split("__")[0]
                if ev_id:
                    skip_event_ids.add(ev_id)

    predictions_buffer: list[TeamPrediction] = list(existing)
    done_count_holder = {"n": len(existing)}
    if existing:
        # Resume must not erase durable progress while waiting for the next
        # request, including when that request fails before another callback.
        with db._lock:
            current_run = db.get_bt_run(run_id)
            if _RUN_CANCEL.get(run_id) or not current_run or current_run.get("status") == "cancelled":
                raise BacktestCancelled("cancelled by user")
            db.update_bt_run_progress(run_id, done_events=len(existing))

    # --- 3. per-pred callback (统一写盘 + 写 DB + SSE + 定时 metrics) ---
    def _flush_all() -> None:
        # 写预测 JSONL
        try:
            write_jsonl(out_path, [p.to_dict() for p in predictions_buffer])
        except Exception:
            pass

    def _on_pred(p: TeamPrediction, ev: EventRecord) -> None:
        # 每条事件回调点先统一处理：pause 阻塞 / cancel 抛错
        _wait_if_paused(run_id)
        if _RUN_CANCEL.get(run_id):
            raise BacktestCancelled("cancelled by user")
        p = enforce_binary_prediction(
            p, run.get("config") if isinstance(run.get("config"), dict) else {},
        )
        # 写 bt_predictions 表（labels 若在则同步回填 oracle）
        labels_map: dict[str, Any] = {}
        if labels_path:
            try:
                from .application import load_labels
                labels_map = {lbl.event_id: lbl for lbl in load_labels(labels_path)}
            except Exception:
                labels_map = {}
        lbl = labels_map.get(p.event_id)
        oracle_label, oracle_car = _oracle_values_for_horizon(lbl, primary_oracle_horizon)
        abstain = bool(getattr(p, "abstain", False))
        is_correct: Optional[bool] = None
        if oracle_label and oracle_car is not None and not abstain:
            is_correct = bool(
                oracle_label in {"up", "down"}
                and p.pred_direction in {"up", "down"}
                and p.pred_direction == oracle_label
            )
        trajectory_path = ckpt_dir / f"{p.event_id}.json" if ckpt_dir else None
        tokens_in = int(getattr(p, "tokens_in", 0) or 0)
        tokens_out = int(getattr(p, "tokens_out", 0) or 0)
        step_ms = int(getattr(p, "step_ms", 0) or 0)
        cost_usd = float(getattr(p, "cost_usd", 0.0) or 0.0)
        if trajectory_path and trajectory_path.is_file():
            try:
                trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
                if not isinstance(trajectory, dict):
                    raise ValueError("trajectory root must be an object")
                usage = trajectory.get("usage") if isinstance(trajectory.get("usage"), dict) else {}
                tokens_in = tokens_in or int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                tokens_out = tokens_out or int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                wall_seconds = trajectory.get("wall_seconds")
                if not step_ms and isinstance(wall_seconds, (int, float)):
                    step_ms = max(0, int(float(wall_seconds) * 1000))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                print(f"[WARN] prediction cost metadata unreadable: {trajectory_path}: {exc}", flush=True)
        # Cancellation and history deletion both synchronize on ``db._lock``.
        # Re-check only when the complete file/DB commit can start; otherwise a
        # provider response may pass the earlier callback check, get cancelled,
        # and still publish a prediction after cancel() returned.
        with db._lock:
            current_run = db.get_bt_run(run_id)
            if _RUN_CANCEL.get(run_id) or not current_run or current_run.get("status") == "cancelled":
                raise BacktestCancelled("cancelled by user")
            predictions_buffer.append(p)
            done_count_holder["n"] += 1
            n = done_count_holder["n"]
            # 每个 pred 落盘
            _flush_all()
            db.add_bt_prediction(
                run_id=run_id,
                event_id=p.event_id,
                pred_direction=p.pred_direction,
                symbol=getattr(ev, "symbol", None),
                market=getattr(ev, "market", None),
                event_type_l2=getattr(ev, "event_type_l2", None),
                confidence=p.confidence,
                abstain=abstain,
                rationale=p.rationale,
                oracle_label_t3=oracle_label,
                oracle_car_t3=oracle_car,
                is_correct_t3=is_correct,
                # Do not persist a plausible-looking checkpoint path until the
                # engine has actually completed and atomically written the file.
                trajectory_ckpt=(
                    str(trajectory_path)
                    if trajectory_path is not None and trajectory_path.is_file()
                    else None
                ),
                horizon=getattr(p, "horizon", None),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                step_ms=step_ms,
                cost_usd=cost_usd,
                strategy_metadata=getattr(p, "strategy_metadata", None),
            )
            # Stop displaying this event as in-flight only after its prediction
            # is durably persisted. This advisory state never increments completion.
            finish_team_full_event_activity(run_id, p.event_id)
            # 推送单事件 SSE
            sse_broadcast(run_id, {
                "type": "prediction",
                "done_count": n,
                "prediction": {
                    "event_id": p.event_id,
                    "pred_direction": p.pred_direction,
                    "confidence": p.confidence,
                    "expected_return_pct": p.expected_return_pct,
                    "abstain": abstain,
                    "strategy_metadata": getattr(p, "strategy_metadata", None),
                },
                "event_basic": _dump_event_basic(ev),
                "activity": get_run_activity(run_id),
            })
            # 每 5 个事件一次 metrics 快照
            _maybe_snapshot(
                run_id,
                predictions_buffer,
                labels_path,
                done_count=n,
                oracle_epsilon=oracle_epsilon,
                primary_oracle_horizon=primary_oracle_horizon,
                allowed_event_ids=allowed_event_ids,
            )
    def _on_team_full_stage(payload: dict[str, Any]) -> None:
        record_team_full_stage(run_id, payload)

    # --- 4. 根据 runner 分派 ---
    variant = run.get("prompt_variant") or "v0"
    model_version = run.get("model_version") or ""
    if run["runner"] == "raw_model":
        from .raw_model import run_raw_model
        profile = (run.get("config") or {}).get("model_profile_snapshot") or {}
        by_event_id = {event.event_id: event for event in events}

        def before_raw_request() -> None:
            _wait_if_paused(run_id)
            if _RUN_CANCEL.get(run_id):
                raise BacktestCancelled("cancelled by user")

        preds = asyncio.run(run_raw_model(
            events, run_id=run_id, profile=profile,
            target_horizon=primary_oracle_horizon, concurrency=effective_concurrency,
            skip_event_ids=skip_event_ids, before_request=before_raw_request,
            on_pred_callback=lambda prediction: _on_pred(prediction, by_event_id[prediction.event_id]),
        ))
    elif run["runner"] == "event_external_http":
        evs_run = [e for e in events if getattr(e, "event_id", None) not in skip_event_ids]
        strategy_spec = run.get("strategy_spec") if isinstance(run.get("strategy_spec"), dict) else {}

        def before_external_request() -> None:
            _wait_if_paused(run_id)
            if _RUN_CANCEL.get(run_id):
                raise BacktestCancelled("cancelled by user")

        preds = _run_external_event_http_with_cb(
            evs_run,
            run_id=run_id,
            model_version=model_version,
            strategy_spec=strategy_spec,
            target_horizon=primary_oracle_horizon,
            on_pred=_on_pred,
            before_request=before_external_request,
        )
    elif run["runner"] == "provided_analysis":
        evs_run = [e for e in events if getattr(e, "event_id", None) not in skip_event_ids]
        preds = _run_provided_analysis_with_cb(
            evs_run,
            run_id=run_id,
            model_version=model_version or "provided-analysis-v1",
            target_horizon=primary_oracle_horizon,
            on_pred=_on_pred,
        )
    elif run["runner"] == "baseline":
        evs_run = [e for e in events if getattr(e, "event_id", None) not in skip_event_ids]
        preds = _run_baseline_with_cb(
            evs_run,
            run_id=run_id,
            model_version=model_version or "event-baseline-v0",
            target_horizon=primary_oracle_horizon,
            on_pred=_on_pred,
        )
    elif run["runner"] == "team_prompt":
        evs_run = events  # skip 在内部用
        preds = _run_team_prompt_with_cb(
            evs_run,
            run_id=run_id,
            model_version=model_version or "team-prompt-v0",
            concurrency=effective_concurrency,
            skip_event_ids=skip_event_ids,
            system_prompt_variant=variant,
            target_horizon=primary_oracle_horizon,
            on_pred=_on_pred,
        )
    elif run["runner"] == "team_full":
        if ckpt_dir is None:
            ckpt_dir = Path("data/_trajectory_ckpt_web")
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        preds = _run_team_full_with_cb(
            events,
            run_id=run_id,
            model_version=model_version or "team-full-trajectory-v1",
            concurrency=effective_concurrency,
            skip_event_ids=skip_event_ids,
            system_prompt_variant=variant,
            target_horizon=primary_oracle_horizon,
            trajectory_ckpt_dir=ckpt_dir,
            on_pred=_on_pred,
            on_stage=_on_team_full_stage,
            cancel_check=lambda: bool(_RUN_CANCEL.get(run_id)),
        )
    else:
        raise ValueError(f"未知 runner={run['runner']}")

    # --- 5. 收尾：强制 metrics 终态 + JSONL 终态写盘 ---
    # The complete terminal publication shares the same lock as cancel().
    # Whichever operation acquires it first wins cleanly: a successful cancel
    # can never be overwritten by a late ``done`` commit.
    with db._lock:
        current_run = db.get_bt_run(run_id)
        if _RUN_CANCEL.get(run_id) or not current_run or current_run.get("status") == "cancelled":
            raise BacktestCancelled("cancelled by user")
        final_n = done_count_holder["n"]
        _flush_all()
        summary = _maybe_snapshot(
            run_id,
            predictions_buffer,
            labels_path,
            done_count=final_n,
            force=True,
            oracle_epsilon=oracle_epsilon,
            primary_oracle_horizon=primary_oracle_horizon,
            allowed_event_ids=allowed_event_ids,
        )
        # labels 全量回填（防止中途 labels_path 变化）
        _sync_labels_for_predictions(
            run_id,
            predictions_buffer,
            labels_path,
            primary_oracle_horizon=primary_oracle_horizon,
        )
        db.update_bt_run_status(run_id, "done")
        sse_broadcast(run_id, {
            "type": "run_done",
            "done_count": final_n,
            "metrics": summary.to_dict() if summary else None,
        })
