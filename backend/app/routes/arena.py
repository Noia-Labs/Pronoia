"""Arena 横向比对 API。

路由前缀: /api/arena
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..event_backtest import arena as arena_engine

router = APIRouter(prefix="/api/arena", tags=["arena"])


# ============================================================== Schemas ========================

class CreateArenaRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Arena 显示名")
    run_ids: list[str] = Field(..., description="参与比对的 run_id 列表，至少 2 个")
    dataset_id: Optional[str] = Field(None, description="数据集 ID（可选，便于筛选）")
    description: Optional[str] = Field(None, description="备注")
    config: Optional[dict[str, Any]] = Field(default_factory=dict, description="自定义：选定的 metric 列表等")
    selected_metric_ids: Optional[list[str]] = Field(None, description="参与排名/雷达的指标；同时持久化进 config")
    arena_type: Optional[str] = Field(None, min_length=1, max_length=40, description="prediction / investment")
    allow_mixed_protocols: bool = Field(False, description="仅探索模式允许混合协议；正式排名默认禁止")
    time_alignment: Optional[dict[str, Any]] = Field(
        None,
        description="时间对齐：mode=native/intersection/union/manual，frequency=native/day/week/month",
    )


class ComputeArenaRequest(BaseModel):
    run_ids: Optional[list[str]] = Field(None, description="参与比对的 run_id 列表；已保存 Arena 可省略")
    selected_metric_ids: Optional[list[str]] = Field(None, description="自定义参与排名/雷达的指标")
    allow_mixed_protocols: bool = False
    arena_type: str = Field("prediction", min_length=1, max_length=40)
    time_alignment: Optional[dict[str, Any]] = None


def _uses_external_time_alignment(
    *,
    arena_type: str | None,
    time_alignment: dict[str, Any] | None,
) -> bool:
    """Whether curve dates/frequency are resolved by the Arena, not the Runs."""
    if time_alignment is None:
        return False
    try:
        return arena_engine._arena_family(str(arena_type or "prediction")) == "performance"
    except arena_engine.ArenaMetricValidationError:
        return False


def _saved_event_comparison_version(arena: dict[str, Any]) -> str | None:
    """Replay a saved Arena's event comparison contract without migrating it."""
    from ..event_backtest.protocol import (
        COMPARISON_PROTOCOL_VERSION, ALIGNED_COMPARISON_PROTOCOL_VERSION,
        EVENT_COMPARISON_PROTOCOL_VERSION, EVENT_ALIGNED_COMPARISON_PROTOCOL_VERSION,
    )

    config = arena.get("config") if isinstance(arena.get("config"), dict) else {}
    result = arena.get("result") if isinstance(arena.get("result"), dict) else {}
    protocol = config.get("comparison_protocol") or result.get("comparison_protocol")
    if not isinstance(protocol, dict):
        return None
    signatures = protocol.get("comparison_signatures")
    if not isinstance(signatures, dict):
        return None
    versions = {
        str(signature.get("version") or "")
        for signature in signatures.values()
        if isinstance(signature, dict) and signature.get("engine_mode") == "event_proxy"
    }
    supported = {
        COMPARISON_PROTOCOL_VERSION, ALIGNED_COMPARISON_PROTOCOL_VERSION,
        EVENT_COMPARISON_PROTOCOL_VERSION, EVENT_ALIGNED_COMPARISON_PROTOCOL_VERSION,
    }
    return next(iter(versions)) if len(versions) == 1 and versions <= supported else None


def _source_artifact_fingerprints(run_infos: list[dict]) -> dict[str, dict[str, Any]]:
    """Hash result bytes without exposing local paths in an Arena record."""
    from ..event_backtest.protocol import file_sha256

    frozen: dict[str, dict[str, Any]] = {}
    for run in run_infos:
        run_id = str(run.get("id") or "")
        engine_mode = str(run.get("engine_mode") or "event_proxy")
        if engine_mode == "portfolio":
            kind = "portfolio_result"
            artifact_path = run.get("result_path")
        else:
            kind = "event_predictions"
            artifact_path = run.get("out_path")
        digest = file_sha256(artifact_path)
        frozen[run_id] = {
            "kind": kind,
            "sha256": digest,
            "available": digest is not None,
        }
    return frozen


def _assert_source_artifacts_unchanged(
    expected: dict[str, dict[str, Any]],
    actual: dict[str, dict[str, Any]],
) -> None:
    """Reject a recompute before it can overwrite a saved leaderboard."""
    if expected == actual:
        return
    changed: dict[str, dict[str, Any]] = {}
    for run_id in sorted(set(expected) | set(actual)):
        before = expected.get(run_id) or {}
        after = actual.get(run_id) or {}
        if before != after:
            changed[run_id] = {
                "kind": after.get("kind") or before.get("kind"),
                "expected_sha256": before.get("sha256"),
                "actual_sha256": after.get("sha256"),
            }
    raise HTTPException(
        status_code=409,
        detail={
            "message": "Arena 的源结果产物已被覆盖或缺失，已拒绝重算以保护已保存排名",
            "reason": "source_artifact_fingerprint_mismatch",
            "changed_run_artifacts": changed,
            "hint": "请从当前产物创建新的 Run 和 Arena；不要复用已冻结的排名记录。",
        },
    )


def _validate_protocols(
    run_infos: list[dict],
    *,
    allow_mixed: bool = False,
    arena_type: str | None = None,
    time_alignment: dict[str, Any] | None = None,
    event_comparison_version: str | None = None,
) -> dict[str, Any]:
    """Validate input integrity, then compare the strategy-independent Arena contract.

    ``protocol_hash`` remains the historical full Run-integrity fingerprint.
    Changing its semantics would invalidate every persisted Run. Formal Arena
    comparability therefore uses a separate, versioned comparison signature.
    """
    from ..event_backtest.protocol import (
        build_protocol_hash,
        COMPARISON_PROTOCOL_VERSION,
        ALIGNED_COMPARISON_PROTOCOL_VERSION,
        comparison_protocol_hash_for_run,
        comparison_signature_for_run,
        evaluator_version_for_engine,
        events_snapshot_sha256,
    )

    integrity_hashes: dict[str, str] = {}
    comparison_hashes: dict[str, str] = {}
    signatures: dict[str, dict[str, Any]] = {}
    source_time_axes: dict[str, dict[str, Any]] = {}
    stale: dict[str, dict[str, str]] = {}
    missing_snapshots: dict[str, list[str]] = {}
    for run in run_infos:
        if str(run.get("engine_mode") or "event_proxy") != "event_proxy":
            continue
        missing = []
        for field in ("events_path", "labels_path"):
            path = Path(str(run.get(field) or ""))
            if not path.is_file() or path.stat().st_size == 0:
                missing.append("event_facts" if field == "events_path" else "oracle")
        if missing:
            missing_snapshots[str(run.get("id") or "")] = missing
    if missing_snapshots:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "事件 Arena 需要可读取的冻结事件与 Oracle 快照，不能仅凭声明的哈希比较",
                "reason": "missing_event_comparison_snapshots",
                "missing_snapshots": missing_snapshots,
            },
        )
    externalize_time_axis = _uses_external_time_alignment(
        arena_type=arena_type,
        time_alignment=time_alignment,
    )
    for run in run_infos:
        run_id = str(run.get("id") or "")
        config = run.get("config") if isinstance(run.get("config"), dict) else {}
        execution_spec = run.get("execution_spec")
        if not isinstance(execution_spec, dict):
            execution_spec = config.get("execution_spec") if isinstance(config.get("execution_spec"), dict) else {}
        if isinstance(execution_spec.get("applied"), dict):
            execution_spec = dict(execution_spec["applied"])
        evaluation_protocol = config.get("evaluation_protocol")
        if not isinstance(evaluation_protocol, dict):
            evaluation_protocol = config.get("protocol") if isinstance(config.get("protocol"), dict) else {}
        unified_contract = bool(run.get("dataset_version") or run.get("strategy_spec"))
        fingerprint = build_protocol_hash(
            events_path=run.get("events_path"),
            labels_path=run.get("labels_path"),
            execution_spec=execution_spec,
            evaluation_protocol=evaluation_protocol,
            dataset_version=run.get("dataset_version") if unified_contract else None,
            engine_mode=run.get("engine_mode") if unified_contract else None,
        )
        integrity_hashes[run_id] = fingerprint
        persisted = str(run.get("protocol_hash") or "").strip()
        if persisted and persisted != fingerprint:
            stale[run_id] = {"persisted": persisted, "recomputed": fingerprint}
        elif not persisted:
            db.update_bt_run_protocol_hash(run_id, fingerprint)
            run["protocol_hash"] = fingerprint
    if stale:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Run 的底层事件、Oracle 或执行/评价协议已变化，持久化 protocol_hash 失效",
                "stale_protocols": stale,
                "hint": "请基于当前不可变快照新建 Run；Arena 不接受被覆盖后继续冒用旧指纹的结果。",
            },
        )

    # Only after the immutable bytes pass their original integrity check do we
    # derive the narrower Arena contract. Dataset catalogue identities and all
    # strategy/model fields are intentionally absent from this signature.
    for run in run_infos:
        run_id = str(run.get("id") or "")
        dataset_id = str(run.get("dataset_id") or "")
        dataset_version = str(run.get("dataset_version") or "")
        dataset_contract = (
            db.get_bt_dataset_version(dataset_id, dataset_version)
            if dataset_id and dataset_version else None
        )
        full_signature = comparison_signature_for_run(
            run, dataset_contract=dataset_contract,
            event_comparison_version=event_comparison_version,
        )
        signature = comparison_signature_for_run(
            run,
            dataset_contract=dataset_contract,
            externalize_time_axis=externalize_time_axis,
            event_comparison_version=event_comparison_version,
        )
        comparison_hash = comparison_protocol_hash_for_run(
            run,
            dataset_contract=dataset_contract,
            externalize_time_axis=externalize_time_axis,
            event_comparison_version=event_comparison_version,
        )
        signatures[run_id] = signature
        source_time_axes[run_id] = {
            "window": full_signature.get("window"),
            "frequency": full_signature.get("frequency"),
        }
        comparison_hashes[run_id] = comparison_hash
        # Ephemeral fields make the lower-level curve/result builder use exactly
        # the contract validated here. They are never persisted into bt_runs.
        run["comparison_signature"] = signature
        run["comparison_protocol_hash"] = comparison_hash
        run["_arena_allow_mixed_protocols"] = bool(allow_mixed)
        semantic_quality = run.get("_arena_semantic_quality")
        demo_only = (
            isinstance(semantic_quality, dict)
            and str(semantic_quality.get("status") or "") == "demo_only"
        )
        run["_arena_formal_eligible"] = (
            str(signature.get("result_nature") or "") not in {"unverified", "unavailable"}
            and not demo_only
        )

    unique = sorted(set(comparison_hashes.values()))
    effective_dataset_versions = {
        str(run.get("dataset_version") or "legacy_" + str(events_snapshot_sha256(run.get("events_path")) or "missing"))
        for run in run_infos
    }
    dataset_versions = sorted(effective_dataset_versions)
    engine_modes = sorted({str(run.get("engine_mode") or "event_proxy") for run in run_infos})
    result_natures = sorted({
        str(
            run.get("result_nature")
            or ("simulated_from_real_bars" if str(run.get("engine_mode") or "event_proxy") == "portfolio" else "proxy")
        )
        for run in run_infos
    })
    result_nature_verified = not ({"unverified", "unavailable"} & set(result_natures))
    semantic_quality_by_run = {
        str(run.get("id") or ""): dict(run["_arena_semantic_quality"])
        for run in run_infos
        if isinstance(run.get("_arena_semantic_quality"), dict)
    }
    demo_only_run_ids = sorted(
        run_id for run_id, quality in semantic_quality_by_run.items()
        if str(quality.get("status") or "") == "demo_only"
    )
    semantic_formal_eligible = not demo_only_run_ids
    strict = (
        len(unique) == 1 and bool(run_infos)
        # Event facts/Oracle hashes above already establish data equality.
        # Imported prediction provenance may give identical facts a new version.
        # Portfolio comparisons keep their existing version-equality gate.
        and (
            (engine_modes == ["event_proxy"] and event_comparison_version not in {
                COMPARISON_PROTOCOL_VERSION, ALIGNED_COMPARISON_PROTOCOL_VERSION,
            }) or len(dataset_versions) == 1
        )
        and len(engine_modes) == 1
        and len(result_natures) == 1
        and result_nature_verified
    )

    def flatten(value: Any, prefix: str = "") -> dict[str, str]:
        if isinstance(value, dict):
            result: dict[str, str] = {}
            for key, item in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                result.update(flatten(item, path))
            return result
        return {prefix: repr(value)}

    flattened = {run_id: flatten(signature) for run_id, signature in signatures.items()}
    signature_fields = sorted({key for value in flattened.values() for key in value})
    mismatched_fields = [
        field for field in signature_fields
        if len({value.get(field, "<missing>") for value in flattened.values()}) > 1
    ]
    if not result_nature_verified and "result_nature" not in mismatched_fields:
        mismatched_fields.append("result_nature")
    result = {
        "strict_comparable": strict,
        "protocol_hash": unique[0] if strict else None,
        "comparison_protocol_hash": unique[0] if strict else None,
        "run_protocol_hashes": comparison_hashes,
        "run_comparison_protocol_hashes": comparison_hashes,
        "run_integrity_hashes": integrity_hashes,
        "comparison_signatures": signatures,
        "time_axis_externalized": externalize_time_axis,
        "source_time_axes": source_time_axes,
        "mismatched_fields": mismatched_fields,
        "mismatched_run_ids": [] if strict else list(comparison_hashes),
        "dataset_version": dataset_versions[0] if len(dataset_versions) == 1 else None,
        "dataset_versions": dataset_versions,
        "engine_mode": engine_modes[0] if len(engine_modes) == 1 else None,
        "engine_modes": engine_modes,
        "result_nature": result_natures[0] if len(result_natures) == 1 else None,
        "result_natures": result_natures,
        "evaluator_version": evaluator_version_for_engine(engine_modes[0] if len(engine_modes) == 1 else None),
        "formal": strict and semantic_formal_eligible and not allow_mixed,
        "formal_evaluation_eligible": strict and semantic_formal_eligible,
        "comparison_mode": "exploration" if allow_mixed else "formal",
        "semantic_quality": semantic_quality_by_run,
        "demo_only_run_ids": demo_only_run_ids,
        "reason": (
            "unverified_result_nature" if not result_nature_verified
            else "demo_only_event_dataset_exploration" if demo_only_run_ids and allow_mixed
            else "explicit_exploration_mode" if allow_mixed and strict
            else None if strict else "comparison_protocol_mismatch"
        ),
    }
    if not strict and not allow_mixed:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "参与 Run 必须冻结同一数据快照、基准、成交/成本、仓位约束与引擎口径，才能进入正式 Arena 排名",
                "comparison_protocol": result,
                "hint": (
                    "显式选择 performance 时间对齐可协调原始窗口/频率；其余差异请统一，"
                    "或启用探索模式（探索结果不会标记为公平排名）。"
                ),
            },
        )
    return result


def _metric_http_error(exc: arena_engine.ArenaMetricValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail=exc.details)


def _request_fields(req: BaseModel | None) -> set[str]:
    if req is None:
        return set()
    fields = getattr(req, "model_fields_set", None)
    if fields is None:
        fields = getattr(req, "__fields_set__", set())
    return set(fields or set())


def _event_semantic_quality_for_run(run: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve semantic quality for old and new event Runs without rewriting them."""
    if str(run.get("engine_mode") or "event_proxy") != "event_proxy":
        return None
    config = run.get("config") if isinstance(run.get("config"), dict) else {}
    frozen = config.get("dataset_snapshot") if isinstance(config.get("dataset_snapshot"), dict) else {}
    semantic = frozen.get("semantic_quality")
    if isinstance(semantic, dict) and semantic.get("status"):
        return dict(semantic)

    contract: dict[str, Any] = {}
    dataset_id = str(run.get("dataset_id") or "")
    dataset_version = str(run.get("dataset_version") or "")
    if dataset_id:
        dataset = db.get_bt_dataset(dataset_id)
        if dataset:
            contract.update(dataset)
        if dataset_version:
            version = db.get_bt_dataset_version(dataset_id, dataset_version)
            if version:
                for key, value in version.items():
                    if key not in {"id", "dataset_id"}:
                        contract[key] = value
    contract.update({
        "dataset_kind": "event",
        "path": run.get("events_path") or contract.get("path"),
    })
    try:
        from ..event_backtest.datasets import with_semantic_quality

        enriched = with_semantic_quality(contract)
        resolved = enriched.get("semantic_quality")
        return dict(resolved) if isinstance(resolved, dict) else None
    except (OSError, UnicodeError, TypeError, ValueError):
        # Legacy records remain comparable if their semantic scan cannot be
        # reconstructed.  Only positively identified demo snapshots are
        # rejected, avoiding a retroactive reinterpretation of old Runs.
        return None


def _validate_scored_runs(run_infos: list[dict], *, allow_mixed: bool = False) -> None:
    """Require auditable results and keep non-PIT models out of formal ranking."""
    unfinished = [str(run.get("id") or "") for run in run_infos if run.get("status") != "done"]
    if unfinished:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Arena 只接受已完成的 Run",
                "unfinished_run_ids": unfinished,
            },
        )
    unscored = [
        str(run.get("id") or "")
        for run in run_infos
        if (
            str(run.get("engine_mode") or "event_proxy") == "event_proxy"
            and (not run.get("labels_path") or not Path(str(run.get("labels_path"))).is_file())
        ) or (
            str(run.get("engine_mode") or "event_proxy") == "portfolio"
            and (not run.get("result_path") or not Path(str(run.get("result_path"))).is_file())
        )
    ]
    if unscored:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "正式 Arena 需要真实市场 Oracle；仅记录决策的 Run 不参与排名",
                "unscored_run_ids": unscored,
                "hint": "请先为相同事件快照生成 Oracle，再创建 Arena。",
            },
        )

    # Model Lab dry-runs are deliberately workflow demos.  A future worker may
    # choose to materialise a diagnostic bt_run for observability, so enforce
    # this boundary here as well as in the Model Lab service: demo results must
    # never become Arena candidates, including in exploratory comparisons.
    model_lab_demo_runs: list[str] = []
    for run in run_infos:
        config = run.get("config") if isinstance(run.get("config"), dict) else {}
        model_lab = config.get("model_lab") if isinstance(config.get("model_lab"), dict) else {}
        source = str(
            config.get("source")
            or config.get("origin")
            or model_lab.get("source")
            or ""
        ).strip().lower().replace("-", "_")
        is_model_lab = bool(model_lab) or source == "model_lab" or any(
            key in config for key in ("model_lab_batch_id", "model_lab_task_id")
        )
        is_demo = any(
            value is True or str(value or "").strip().lower() in {"demo", "dry_run", "demo_only"}
            for value in (
                config.get("dry_run"), config.get("demo"), config.get("result_mode"),
                model_lab.get("dry_run"), model_lab.get("demo"), model_lab.get("result_mode"),
            )
        )
        if is_model_lab and is_demo:
            model_lab_demo_runs.append(str(run.get("id") or ""))
    if model_lab_demo_runs:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Model Lab 演示或 dry-run 结果不能进入 Arena",
                "reason": "model_lab_demo_result",
                "demo_run_ids": sorted(model_lab_demo_runs),
                "hint": "请完成正式预测评测；只有其生成的真实、可审计 bt_run 才会出现在 Arena。",
            },
        )

    demo_only: dict[str, dict[str, Any]] = {}
    for run in run_infos:
        semantic_quality = _event_semantic_quality_for_run(run)
        if semantic_quality is None:
            continue
        run["_arena_semantic_quality"] = semantic_quality
        if str(semantic_quality.get("status") or "") == "demo_only":
            demo_only[str(run.get("id") or "")] = semantic_quality
    if demo_only and not allow_mixed:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "正式事件 Arena 不接受合成或占位事件集",
                "reason": "demo_only_event_dataset",
                "demo_only_run_ids": sorted(demo_only),
                "semantic_quality": demo_only,
                "hint": "请改用真实、可追溯的历史事件集，或显式选择探索对照；探索结果不会标记为正式排名。",
            },
        )

    unverified_portfolio_runs: list[str] = []
    for run in run_infos:
        if str(run.get("engine_mode") or "event_proxy") != "portfolio":
            continue
        config = run.get("config") if isinstance(run.get("config"), dict) else {}
        strategy = run.get("strategy_spec")
        if not isinstance(strategy, dict):
            strategy = config.get("strategy_spec") if isinstance(config.get("strategy_spec"), dict) else {}
        adapter = str(strategy.get("adapter") or strategy.get("kind") or run.get("runner") or "")
        external_http = (
            adapter == "external_http"
            or str(strategy.get("type") or run.get("strategy_type") or "") == "api"
        )
        result_nature = str(run.get("result_nature") or strategy.get("result_nature") or "")
        point_in_time_enforced = strategy.get("point_in_time_enforced")
        if result_nature == "unverified" or (external_http and point_in_time_enforced is not True):
            unverified_portfolio_runs.append(str(run.get("id") or ""))
    if unverified_portfolio_runs and not allow_mixed:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "正式 Arena 不接受未经 point-in-time 验证的外部组合策略",
                "unverified_run_ids": unverified_portfolio_runs,
                "reason": "external_http 当前一次接收完整历史 bars，平台不能证明信号未使用未来数据",
                "hint": "可在新建 Arena 时选择“探索对照”；探索结果不会标记为公平排名。",
            },
        )


# ============================================================== CRUD ===========================

@router.get("")
def list_arenas(limit: int = Query(100, ge=1, le=500)) -> Any:
    items = db.list_bt_arenas(limit=limit)
    from ..event_backtest.model_comparison import assert_readable
    readable = []
    for item in items:
        try:
            assert_readable(item)
            readable.append(item)
        except ValueError:
            continue
    items = readable
    return {"total": len(items), "items": items}


@router.post("")
def create_arena(req: CreateArenaRequest) -> Any:
    with db.HISTORY_RELATION_LOCK:
        return _create_arena_locked(req)


def _create_arena_locked(req: CreateArenaRequest) -> Any:
    run_ids = list(dict.fromkeys(req.run_ids))
    if len(run_ids) < 2:
        raise HTTPException(status_code=400, detail="run_ids 至少需要 2 个")
    config = dict(req.config or {})
    raw_time_alignment = req.time_alignment
    if raw_time_alignment is None and isinstance(config.get("time_alignment"), dict):
        raw_time_alignment = config.get("time_alignment")
    try:
        time_alignment = arena_engine.normalize_time_alignment_config(raw_time_alignment)
    except arena_engine.ArenaMetricValidationError as exc:
        raise _metric_http_error(exc) from exc
    if time_alignment is not None:
        config["time_alignment"] = time_alignment
    allow_mixed_protocols = req.allow_mixed_protocols or bool(config.get("allow_mixed_protocols"))
    arena_type = str(req.arena_type or config.get("arena_type") or "prediction")
    # 校验 run_id 都存在
    run_infos = []
    for rid in run_ids:
        r = db.get_bt_run(rid)
        if not r:
            raise HTTPException(status_code=404, detail=f"run_id={rid} 不存在")
        run_infos.append(r)
    _validate_scored_runs(run_infos, allow_mixed=allow_mixed_protocols)
    protocol = _validate_protocols(
        run_infos,
        allow_mixed=allow_mixed_protocols,
        arena_type=arena_type,
        time_alignment=time_alignment,
    )
    source_artifacts = _source_artifact_fingerprints(run_infos)
    requested_metrics = req.selected_metric_ids
    if requested_metrics is None and isinstance(config.get("selected_metric_ids"), list):
        requested_metrics = config["selected_metric_ids"]
    ctxs = arena_engine.build_run_contexts(run_infos)
    try:
        selected_metric_ids = arena_engine.resolve_metric_selection(
            ctxs,
            requested_metrics,
            arena_type=arena_type,
        )
    except arena_engine.ArenaMetricValidationError as exc:
        raise _metric_http_error(exc) from exc
    _assert_source_artifacts_unchanged(
        source_artifacts,
        _source_artifact_fingerprints(run_infos),
    )

    known_dataset_ids = sorted({str(r.get("dataset_id")) for r in run_infos if r.get("dataset_id")})
    if req.dataset_id and known_dataset_ids and req.dataset_id not in known_dataset_ids:
        raise HTTPException(status_code=409, detail="dataset_id 不属于参赛 Run 的数据记录")
    # dataset_id is storage identity, while protocol_hash represents semantic
    # identity. Equivalent cloned snapshots may therefore have several IDs.
    dataset_id = known_dataset_ids[0] if len(known_dataset_ids) == 1 else None
    # 尝试取 dataset_name（方便显示）
    dataset_name = None
    if dataset_id:
        ds = db.get_bt_dataset(dataset_id)
        if ds:
            dataset_name = ds.get("name")
    names = sorted({str(r.get("dataset_name")) for r in run_infos if r.get("dataset_name")})
    if not dataset_name:
        dataset_name = names[0] if len(names) == 1 else (
            f"内容等价快照 · {len(known_dataset_ids)} 个数据集别名" if len(known_dataset_ids) > 1 else None
        )
    # The metric list is resolved and frozen at creation, including defaults.
    config["selected_metric_ids"] = selected_metric_ids
    config["arena_type"] = arena_type
    config["allow_mixed_protocols"] = allow_mixed_protocols
    config["comparison_protocol"] = protocol
    config["source_artifact_fingerprints"] = source_artifacts
    config["dataset_ids"] = known_dataset_ids
    config["dataset_names"] = names
    return db.create_bt_arena(
        name=req.name,
        run_ids=run_ids,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        description=req.description,
        config=config,
        arena_type=arena_type,
        protocol_hash=protocol.get("protocol_hash"),
    )


@router.get("/{arena_id}")
def get_arena(arena_id: str) -> Any:
    a = db.get_bt_arena(arena_id)
    if not a:
        raise HTTPException(status_code=404, detail="arena not found")
    from ..event_backtest.model_comparison import assert_readable
    try:
        assert_readable(a)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return a


@router.delete("/{arena_id}")
def delete_arena(arena_id: str) -> Any:
    row = db.get_bt_arena(arena_id)
    if row and row.get("arena_type") == "workspace":
        from ..event_backtest.arena_workspace import ACTIVE, _JOBS
        if row.get("status") in ACTIVE or arena_id in _JOBS:
            raise HTTPException(status_code=409, detail="请先取消本次比较并等待已发请求停止，再删除记录")
    if not db.delete_bt_arena(arena_id):
        raise HTTPException(status_code=404, detail="arena not found")
    return {"ok": True, "arena_id": arena_id}


# ============================================================== 计算引擎 ======================

def _load_labels_for_runs(run_ids: list[str]):
    """尝试用第一个 run 的 labels_path 加载 labels（同数据集共享 labels）。"""
    for rid in run_ids:
        r = db.get_bt_run(rid)
        if not r:
            continue
        lp = r.get("labels_path")
        if lp and Path(lp).is_file():
            try:
                from ..event_backtest.application import load_labels
                return load_labels(lp)
            except Exception:
                return None
    return None


@router.post("/compute")
def compute_arena_inline(req: ComputeArenaRequest) -> Any:
    """即时计算：不落库，直接返回 Arena 比对结果。
    适用于前端「临时选几个 Run 对比看看」场景。"""
    run_ids = list(dict.fromkeys(req.run_ids or []))
    if len(run_ids) < 2:
        raise HTTPException(status_code=400, detail="run_ids 至少需要 2 个")
    run_infos = []
    for rid in run_ids:
        r = db.get_bt_run(rid)
        if not r:
            raise HTTPException(status_code=404, detail=f"run_id={rid} 不存在")
        run_infos.append(r)
    try:
        time_alignment = arena_engine.normalize_time_alignment_config(req.time_alignment)
    except arena_engine.ArenaMetricValidationError as exc:
        raise _metric_http_error(exc) from exc
    _validate_scored_runs(run_infos, allow_mixed=req.allow_mixed_protocols)
    protocol = _validate_protocols(
        run_infos,
        allow_mixed=req.allow_mixed_protocols,
        arena_type=req.arena_type,
        time_alignment=time_alignment,
    )
    source_artifacts = _source_artifact_fingerprints(run_infos)
    ctxs = arena_engine.build_run_contexts(run_infos)
    labels_list = _load_labels_for_runs(run_ids)
    try:
        result = arena_engine.compute_arena_result(
            ctxs,
            selected_metric_ids=req.selected_metric_ids,
            labels_list=labels_list,
            arena_type=req.arena_type,
            time_alignment=time_alignment,
        )
    except arena_engine.ArenaMetricValidationError as exc:
        raise _metric_http_error(exc) from exc
    _assert_source_artifacts_unchanged(
        source_artifacts,
        _source_artifact_fingerprints(run_infos),
    )
    result["comparison_protocol"] = protocol
    result["source_artifacts"] = {
        "immutable": True,
        "fingerprints": source_artifacts,
    }
    return result


@router.post("/{arena_id}/compute")
def compute_arena_and_save(
    arena_id: str,
    req: Optional[ComputeArenaRequest] = None,  # noqa: UP007 - Pydantic 兼容
) -> Any:
    """对已创建的 arena_id 计算比对结果并写回 result_json。"""
    a = db.get_bt_arena(arena_id)
    if not a:
        raise HTTPException(status_code=404, detail="arena not found")
    if a.get("arena_type") in {"model_comparison", "workspace"}:
        if req is not None and _request_fields(req):
            raise HTTPException(status_code=409, detail="本次模型对比的行情和交易规则已保存；修改模型或规则请新建对比")
        return get_arena(arena_id)
    locked_run_ids = list(a.get("run_ids") or [])
    run_ids = list(req.run_ids) if req and req.run_ids else locked_run_ids
    if run_ids != locked_run_ids:
        raise HTTPException(status_code=409, detail="已保存 Arena 的 run_ids 已锁定；请新建 Arena 进行另一组比较")
    arena_config = a.get("config") if isinstance(a.get("config"), dict) else {}
    selected_metric_ids = arena_config.get("selected_metric_ids")
    if not isinstance(selected_metric_ids, list):
        selected_metric_ids = None
    provided_fields = _request_fields(req)
    if req and "selected_metric_ids" in provided_fields:
        requested = list(dict.fromkeys(req.selected_metric_ids or []))
        if requested != (selected_metric_ids or []):
            raise HTTPException(
                status_code=409,
                detail="已保存 Arena 的 selected_metric_ids 已冻结；请新建 Arena 采用另一组排名指标",
            )
    locked_arena_type = str(a.get("arena_type") or arena_config.get("arena_type") or "prediction")
    if req and "arena_type" in provided_fields:
        try:
            requested_family = arena_engine._arena_family(req.arena_type)
            locked_family = arena_engine._arena_family(locked_arena_type)
        except arena_engine.ArenaMetricValidationError as exc:
            raise _metric_http_error(exc) from exc
        if requested_family != locked_family:
            raise HTTPException(status_code=409, detail="已保存 Arena 的 arena_type 已冻结")
    if req and "allow_mixed_protocols" in provided_fields:
        if bool(req.allow_mixed_protocols) != bool(arena_config.get("allow_mixed_protocols")):
            raise HTTPException(status_code=409, detail="已保存 Arena 的协议比较模式已冻结")
    if len(run_ids) < 2:
        raise HTTPException(status_code=400, detail="run_ids 至少需要 2 个")
    run_infos = []
    for rid in run_ids:
        r = db.get_bt_run(rid)
        if not r:
            raise HTTPException(status_code=404, detail=f"run_id={rid} 不存在")
        run_infos.append(r)
    allow_mixed = bool(arena_config.get("allow_mixed_protocols"))
    try:
        time_alignment = arena_engine.normalize_time_alignment_config(
            arena_config.get("time_alignment")
        )
    except arena_engine.ArenaMetricValidationError as exc:
        raise _metric_http_error(exc) from exc
    if req and "time_alignment" in provided_fields:
        try:
            requested_alignment = arena_engine.normalize_time_alignment_config(req.time_alignment)
        except arena_engine.ArenaMetricValidationError as exc:
            raise _metric_http_error(exc) from exc
        if requested_alignment != time_alignment:
            raise HTTPException(status_code=409, detail="已保存 Arena 的 time_alignment 已冻结；请新建 Arena 修改时间对齐")
    _validate_scored_runs(run_infos, allow_mixed=allow_mixed)
    protocol = _validate_protocols(
        run_infos,
        allow_mixed=allow_mixed,
        arena_type=locked_arena_type,
        time_alignment=time_alignment,
        event_comparison_version=_saved_event_comparison_version(a),
    )
    current_source_artifacts = _source_artifact_fingerprints(run_infos)
    frozen_source_artifacts = arena_config.get("source_artifact_fingerprints")
    config_changed = False
    if isinstance(frozen_source_artifacts, dict) and frozen_source_artifacts:
        _assert_source_artifacts_unchanged(
            frozen_source_artifacts,
            current_source_artifacts,
        )
    else:
        # Backward-compatible one-time freeze for Arenas saved before result
        # artifact fingerprints were introduced.
        frozen_source_artifacts = current_source_artifacts
        if any(item.get("available") for item in current_source_artifacts.values()):
            arena_config = dict(arena_config)
            arena_config["source_artifact_fingerprints"] = current_source_artifacts
            config_changed = True
    ctxs = arena_engine.build_run_contexts(run_infos)
    try:
        resolved_metrics = arena_engine.resolve_metric_selection(
            ctxs,
            selected_metric_ids,
            arena_type=locked_arena_type,
        )
    except arena_engine.ArenaMetricValidationError as exc:
        raise _metric_http_error(exc) from exc
    if selected_metric_ids is None:
        # Backward-compatible one-time freeze for Arenas created before defaults
        # were persisted. Request payloads still cannot choose or overwrite it.
        arena_config = dict(arena_config)
        arena_config["selected_metric_ids"] = resolved_metrics
        config_changed = True
        selected_metric_ids = resolved_metrics
    if config_changed:
        db.update_bt_arena_config(arena_id, arena_config)

    db.update_bt_arena_status(arena_id, "computing")
    try:
        labels_list = _load_labels_for_runs(run_ids)
        result = arena_engine.compute_arena_result(
            ctxs,
            selected_metric_ids=selected_metric_ids,
            labels_list=labels_list,
            arena_type=locked_arena_type,
            time_alignment=time_alignment,
        )
        _assert_source_artifacts_unchanged(
            frozen_source_artifacts,
            _source_artifact_fingerprints(run_infos),
        )
        result["comparison_protocol"] = protocol
        result["source_artifacts"] = {
            "immutable": True,
            "fingerprints": frozen_source_artifacts,
        }
        db.update_bt_arena_status(arena_id, "done", result=result)
    except HTTPException:
        # In particular, never replace a prior saved leaderboard when a source
        # result changed between the preflight hash and the completed compute.
        previous_result = a.get("result") if isinstance(a.get("result"), dict) else None
        db.update_bt_arena_status(
            arena_id,
            str(a.get("status") or "ready"),
            result=previous_result,
        )
        raise
    except arena_engine.ArenaMetricValidationError as exc:
        db.update_bt_arena_status(arena_id, "failed", result={"error": exc.details})
        raise _metric_http_error(exc) from exc
    except Exception as exc:
        db.update_bt_arena_status(arena_id, "failed", result={"error": str(exc)})
        raise HTTPException(status_code=500, detail=f"arena compute failed: {exc}")
    return db.get_bt_arena(arena_id)
