"""Durable model-first Arena jobs with one frozen evaluation market."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Mapping

from .. import config, db
from ..model_endpoint_security import redact_sensitive_text
from . import arena_workspace_catalog as catalog_service
from .arena_workspace_execution import (
    compare_event_models,
    estimate_event_cases_work,
    estimate_model_work,
    evaluate_event_cases_model,
    evaluate_model,
    normalize_event_cases,
    unified_quant_records,
)
from .cancellation import BacktestCancelled
from .model_comparison_replay import compare_on_market

_LOCK = threading.RLock()
_JOBS: dict[str, tuple[threading.Thread, threading.Event]] = {}
ACTIVE = {"pending", "running"}


class WorkspaceConflict(ValueError):
    pass


def _safe_text(value: Any) -> str:
    return re.sub(r"https?://[^\s）)]+", "[模型服务地址]", redact_sensitive_text(value))[:1000]


def _jsonable(value: Any) -> Any:
    """Recursively serialize nested frozen event-set inputs for audit."""
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _model_track(model: Mapping[str, Any]) -> str | None:
    explicit = str(model.get("track") or "").lower()
    if explicit in {"quant", "event"}:
        return explicit
    kind = str(model.get("kind") or model.get("model_kind") or "")
    if kind == "quant":
        return "quant"
    if kind in {"pronoia", "external_model"}:
        return "event"
    if kind == "external_service":
        return "event" if (model.get("strategy_spec") or {}).get("type") == "event" else "quant"
    return None


def _target_track(target: Mapping[str, Any]) -> str:
    kind = str(target.get("kind") or "")
    if kind == "asset":
        return "quant"
    if kind in {"event", "event_set"}:
        return "event"
    raise ValueError("比较对象必须是行情、单事件或事件集。")


def _target_scope(target: Mapping[str, Any]) -> str:
    return {"asset": "asset", "event": "single_event", "event_set": "event_set"}.get(
        str(target.get("kind") or ""), ""
    )


def _write(aid: str, name: str, payload: dict) -> None:
    # Only server-generated identifiers and fixed filenames reach this helper.
    directory = Path(config.DATA_DIR) / "arena_workspace" / aid
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    target = directory / name
    temp = directory / (name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
    os.replace(temp, target)


def get(aid: str) -> dict:
    row = db.get_bt_arena(aid)
    if not row or row.get("arena_type") != "workspace":
        raise KeyError("Arena 不存在")
    return row


def _progress(aid: str, *, stage: str | None = None, message: str | None = None,
              model_id: str | None = None, changes: dict | None = None) -> None:
    with _LOCK:
        row = get(aid)
        if row["status"] in {"cancelled", "failed", "done", "partial"}:
            return
        cfg = deepcopy(row.get("config") or {})
        progress = cfg["progress"]
        if stage is not None:
            progress["stage"] = stage
        if message is not None:
            progress["message"] = _safe_text(message)
        if model_id:
            for model in progress["models"]:
                if model["id"] == model_id:
                    model.update(changes or {})
        db.update_bt_arena_config(aid, cfg)


def create(request: dict) -> dict:
    request = deepcopy(request)
    aid = "aw_" + request["request_id"]
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    ids = request["model_ids"]
    if len(set(ids)) != len(ids):
        raise ValueError("请选择不同的模型，同一模型不能重复加入")
    if request["start_date"] > request["end_date"]:
        raise ValueError("开始日期不能晚于结束日期")
    with db.HISTORY_RELATION_LOCK, _LOCK:
        existing = db.get_bt_arena(aid)
        if existing:
            if (existing.get("config") or {}).get("request_fingerprint") != fingerprint:
                raise WorkspaceConflict("本次提交标识已用于其他配置，请重新开始比较")
            return existing
        public = catalog_service.catalog()
        target = next((item for item in public["targets"] if (
            item["id"] == request["target_id"]
            or request["target_id"] in (item.get("legacy_target_ids") or [])
        )), None)
        if not target:
            raise ValueError("所选事件或指数已不存在，请刷新后重新选择")
        if target.get("available") is False:
            raise ValueError(str(target.get("reason") or "所选对象缺少可用的冻结数据或 Oracle，请重新选择。"))
        target_track = _target_track(target)
        requested_track = str(request.get("track") or target_track).lower()
        if requested_track not in {"quant", "event"}:
            raise ValueError("请选择 Quant Arena 或 Event Arena。")
        if requested_track != target_track:
            raise ValueError("所选赛道与比较对象不一致：Quant 只能选择行情，Event 只能选择事件。")
        requested_scope = str(request.get("target_scope") or _target_scope(target))
        if requested_scope != _target_scope(target):
            raise ValueError("目标范围与所选对象不一致，请重新选择单事件、事件集或行情。")
        frequency = str(request.get("frequency") or target.get("frequency") or "").strip().lower()
        if requested_track == "event" and frequency not in {"", "1d", "d", "day", "daily"}:
            raise ValueError("事件模型当前使用日 K Oracle；分钟周期仅可用于纯量化赛道。")
        target_source_run_ids = (
            catalog_service.target_source_run_ids(str(request["target_id"]))
            if target.get("kind") in {"event", "event_set"} else []
        )
        models = [catalog_service.resolve_model(identity) for identity in ids]
        for model in models:
            model_track = _model_track(model)
            if model_track is None:
                raise ValueError(f"{model['name']} 缺少明确赛道，无法参加 Arena。")
            if model_track != requested_track:
                raise ValueError("同一场 Arena 只能选择同一赛道的模型或策略，不能混选量化与事件模型。")
            supported = set(model.get("supported_target_kinds") or ["event", "asset"])
            required_kinds = {target["kind"]} | ({"event"} if target["kind"] == "event_set" else set())
            if supported.isdisjoint(required_kinds):
                raise ValueError(f"{model['name']} 不支持当前比较对象")
            compatible = model.get("compatible_target_ids")
            if compatible is not None and target["id"] not in compatible:
                raise ValueError(f"{model['name']} 的已有预测不适用于所选对象，请选择原对象或导入对应预测")
        rules = {key: request[key] for key in ("holding_bars", "fee_bps", "slippage_bps", "initial_capital")}
        rules["position_rule"] = "long_flat" if requested_track == "quant" else "long_short_flat_event_proxy"
        public_models = [{"id": model["id"], "name": model["name"], "kind": model["kind"],
                          "track": _model_track(model) or requested_track} for model in models]
        source_run_ids = list(dict.fromkeys(
            str(source_id).strip()
            for model in models
            for source_id in (
                model.get("source_run_ids")
                if isinstance(model.get("source_run_ids"), list)
                else [model.get("source_run_id")]
            )
            if isinstance(source_id, str) and source_id.strip()
        ))
        cfg = {
            "arena_workspace": {"version": 2, "track": requested_track,
                "target_scope": requested_scope, "frequency": frequency or None,
                "model_ids": ids, "models": public_models,
                "target": target, "start_date": request["start_date"], "end_date": request["end_date"], "rules": rules},
            "source_run_ids": source_run_ids,
            "target_source_run_ids": target_source_run_ids,
            "request_fingerprint": fingerprint,
            "progress": {"stage": "preparing", "message": "正在读取共同数据并检查全部模型",
                "models": [{**model, "status": "pending", "done": 0, "total": 0} for model in public_models]},
        }
        name = str(request.get("name") or "").strip() or f"模型对比 · {target['name']}"
        _write(aid, "job.json", {"request": request, "models": models})
        row = db.create_bt_arena(arena_id=aid, name=_safe_text(name)[:120], run_ids=[],
            arena_type="workspace", dataset_name=target["name"], config=cfg,
            description="同一对象与时间段生成预测，并按统一规则比较预测、收益和风险")
        db.update_bt_arena_status(aid, "pending")
        _launch(aid, request, models, rules)
        return get(aid)


def _launch(aid: str, request: dict, models: list[dict], rules: dict) -> None:
    cancel = threading.Event()
    thread = threading.Thread(target=_worker, args=(aid, request, models, rules, cancel), daemon=True, name=aid)
    _JOBS[aid] = (thread, cancel)
    thread.start()


def cancel(aid: str) -> dict:
    with _LOCK:
        row = get(aid)
        if row["status"] not in ACTIVE:
            return row
        job = _JOBS.get(aid)
        if job:
            job[1].set()
        _progress(aid, stage="cancelled", message="已停止安排新预测；已发出的请求可能已产生用量")
        cfg = get(aid)["config"]
        for model in cfg["progress"]["models"]:
            if model["status"] in ACTIVE:
                model["status"] = "cancelled"
        db.update_bt_arena_config(aid, cfg)
        return db.update_bt_arena_status(aid, "cancelled") or row


def recover_interrupted() -> list[str]:
    """Never automatically repeat paid inference after a process restart."""
    changed = []
    with _LOCK:
        for row in db.list_bt_arenas(limit=10000):
            aid = row["id"]
            if row.get("arena_type") != "workspace" or row["status"] not in ACTIVE | {"ready"} or aid in _JOBS:
                continue
            _progress(aid, stage="failed", message="服务重启中断了本次比较。已保存的数据仍保留；重新运行请新建比较。")
            cfg = get(aid)["config"]
            for model in cfg["progress"]["models"]:
                if model["status"] in ACTIVE:
                    model.update(status="failed", error="运行被服务重启中断")
            db.update_bt_arena_config(aid, cfg)
            db.update_bt_arena_status(aid, "failed")
            changed.append(aid)
    return changed


def prediction_quality(dataset, contestant: dict, horizon: int, event_truth: dict | None = None) -> dict:
    errors, correct, pending = [], [], 0
    by_index = {item["bar_index"]: item for item in contestant["observations"]}
    for index, item in by_index.items():
        number = item.get("expected_return_pct")
        numeric = isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and item.get("horizon_bars") == horizon
        direction_forecast = item.get("direction_basis") in {"asset", "asset_return", "market_excess"} and item.get("horizon_bars") in {None, horizon}
        if index + horizon >= len(dataset.bars):
            pending += int(numeric or direction_forecast)
            continue
        actual = (dataset.bars[index + horizon].close / dataset.bars[index].close - 1) * 100
        if numeric:
            errors.append(number - actual)
            correct.append((number > 0) - (number < 0) == (actual > 0) - (actual < 0))
        elif item.get("direction_basis") in {"asset", "asset_return"} and item.get("horizon_bars") in {None, horizon}:
            correct.append(item.get("direction") == ("up" if actual > 0 else "down" if actual < 0 else "neutral"))
    basis, label = "asset_return", "标的涨跌"
    truth = (event_truth or {}).get("horizons", {}).get(f"t{horizon}")
    event_predictions = [item for index, item in by_index.items()
                         if index + horizon < len(dataset.bars) and item.get("horizon_bars") == horizon
                         and item.get("direction_basis") == "market_excess" and item.get("direction") in {"up", "down", "neutral"}]
    if (event_truth or {}).get("truth_basis") == "market_excess" and truth and truth.get("direction") in {"up", "down", "neutral"} and event_predictions:
        correct = [p["direction"] == truth["direction"] for p in event_predictions]
        basis, label = "market_excess", "事件相对基准方向"
    n, successes = len(correct), sum(correct)
    accuracy, interval = successes / n if n else None, None
    if n:
        z = 1.959963984540054
        center = (accuracy + z*z/(2*n)) / (1 + z*z/n)
        radius = z * math.sqrt(accuracy*(1-accuracy)/n + z*z/(4*n*n)) / (1 + z*z/n)
        interval = [max(0, center-radius), min(1, center+radius)]
    count = len(errors)
    return {"n_direction": n, "correct_direction": successes, "directional_accuracy": accuracy,
        "accuracy_ci95": interval, "direction_basis": basis, "direction_label": label,
        "n_numeric": count, "mae_pct": math.fsum(abs(e)/count for e in errors) if count else None,
        "rmse_pct": math.hypot(*(e/math.sqrt(count) for e in errors)) if count else None,
        "bias_pct": math.fsum(e/count for e in errors) if count else None,
        "pending_count": pending, "neutral_threshold_pct": 0}


async def _execute(aid: str, models: list[dict], resolved: dict, rules: dict, cancel_event: threading.Event) -> list[dict]:
    track = _target_track(resolved["target"])
    cases = normalize_event_cases(resolved) if track == "event" else []
    dataset = resolved.get("dataset")
    event = resolved.get("event")
    if track == "quant" and not hasattr(dataset, "bars"):
        raise ValueError("Quant Arena 缺少已冻结的共同 K 线行情。")
    settings = {**rules, "target": resolved["target"]}
    if track == "quant":
        settings["history_dataset"] = resolved.get("history_dataset", dataset)
    # Preflight EVERY model before any inference; incompatible horizons or
    # incomplete inputs should not consume the other participants' API quota.
    for model in models:
        total = (estimate_event_cases_work(model, cases, settings) if track == "event"
                 else estimate_model_work(model, dataset, event, settings))
        _progress(aid, model_id=model["id"], changes={"total": total})
    semaphore = asyncio.Semaphore(2)

    async def run(model: dict):
        async with semaphore:
            if cancel_event.is_set():
                return None
            identity, started = model["id"], time.monotonic()
            _progress(aid, model_id=identity, changes={"status": "running"})
            last_report = 0.0
            def report(done: int, total: int):
                nonlocal last_report
                now = time.monotonic()
                if not cancel_event.is_set() and (done == 0 or done >= total or now-last_report >= 0.35):
                    last_report = now
                    _progress(aid, model_id=identity, changes={"done": done, "total": total})
            try:
                result = (await evaluate_event_cases_model(model, cases, settings, cancel_event, report)
                          if track == "event"
                          else await evaluate_model(model, dataset, event, settings, cancel_event, report))
                elapsed = time.monotonic()-started
                rows = get(aid)["config"]["progress"]["models"]
                row = next(item for item in rows if item["id"] == identity)
                failed_items = int(result.get("failed_prediction_count") or 0)
                result["execution"] = {"elapsed_seconds": elapsed, "items_done": row["done"], "items_total": row["total"], "failed_items": failed_items}
                _write(aid, hashlib.sha256(identity.encode()).hexdigest()[:16]+".json", result)
                if not cancel_event.is_set():
                    _progress(aid, model_id=identity, changes={"status": "partial" if failed_items else "done", "elapsed_seconds": elapsed,
                        **({"error": f"{failed_items} 个预测时点失败，已保留其余结果"} if failed_items else {})})
                return result
            except BacktestCancelled:
                return None
            except Exception as exc:
                if not cancel_event.is_set():
                    message = _safe_text(exc) if isinstance(exc, ValueError) else f"模型执行失败（{type(exc).__name__}），请检查连接或输入数据"
                    _progress(aid, model_id=identity, changes={"status": "failed", "error": message, "elapsed_seconds": time.monotonic()-started})
                return None
    return [result for result in await asyncio.gather(*(run(model) for model in models)) if result is not None]


def _worker(aid: str, request: dict, models: list[dict], rules: dict, cancel_event: threading.Event) -> None:
    try:
        if cancel_event.is_set():
            return
        with _LOCK:
            if cancel_event.is_set():
                return
            db.update_bt_arena_status(aid, "running")
        frequency = request.get("frequency")
        if frequency:
            resolved = catalog_service.resolve_target(
                request["target_id"], request["start_date"], request["end_date"], frequency=frequency,
            )
        else:
            resolved = catalog_service.resolve_target(request["target_id"], request["start_date"], request["end_date"])
        if cancel_event.is_set():
            return
        track = _target_track(resolved["target"])
        if str(request.get("track") or track) != track:
            raise ValueError("解析后的冻结对象与所选赛道不一致，请刷新数据后重试。")
        if track == "quant" and frequency and str(resolved["dataset"].frequency).lower() != str(frequency).lower():
            raise ValueError("冻结行情周期与 Quant Arena 所选周期不一致。")
        snapshot = _jsonable(resolved)
        _write(aid, "input.json", snapshot)
        _progress(aid, stage="predicting", message=(
            "各事件模型正在同一冻结事件集、标签和 Oracle 口径上预测"
            if track == "event" else "各量化策略正在同一份冻结行情上运行，随后统一计算收益与风险"
        ))
        successful = asyncio.run(_execute(aid, models, resolved, rules, cancel_event))
        result = None
        if successful:
            if track == "event":
                cases = normalize_event_cases(resolved)
                result = compare_event_models(cases, successful, rules)
            else:
                result = compare_on_market(resolved["dataset"], successful, rules)
                result["track"] = "quant"
                result["record_schema_version"] = "arena-result-record-v1"
            result["subject"] = {**resolved["target"], "title": resolved["target"]["name"], "key": resolved["target"]["id"]}
            result["data_snapshot_hash"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            result["requested_range"] = {"start_date": request["start_date"], "end_date": request["end_date"]}
            for output, contestant in zip(result["models"], successful):
                if track == "quant":
                    output["prediction_quality"] = prediction_quality(
                        resolved["dataset"], contestant, rules["holding_bars"], resolved.get("event_truth")
                    )
                    output["records"] = unified_quant_records(resolved["dataset"], contestant, rules["holding_bars"])
                output["execution"] = contestant["execution"]
                output["warnings"] = [_safe_text(message) for message in output["warnings"]]
            result["notes"].extend(resolved.get("notes") or [])
            result["notes"].append(
                "事件标签和实际收益缺失时不补零；模型请求失败也不会被改写为中性预测。"
                if track == "event" else
                "各策略自身准确率与误差使用其有效样本；共同样本指标单独展示。缺失预测不补零。"
            )
            if len(successful) == 1:
                result["notes"].append("仅一个模型成功，当前只展示该模型结果，不能形成模型间比较。")
            _write(aid, "result.json", result)
        with _LOCK:
            if cancel_event.is_set():
                if result:
                    db.update_bt_arena_status(aid, "cancelled", result=result)
                return
            complete = len(successful) == len(models) and not any(item.get("failed_prediction_count") for item in successful)
            status = "done" if complete else "partial" if successful else "failed"
            message = f"{len(successful)} / {len(models)} 个模型返回结果" if successful else "所有模型均未生成可用预测，请查看各模型原因"
            if any(item.get("failed_prediction_count") for item in successful):
                message += "，部分预测时点失败，已保留成功部分"
            _progress(aid, stage=status, message=message)
            db.update_bt_arena_status(aid, status, result=result)
    except Exception as exc:
        with _LOCK:
            if not cancel_event.is_set():
                message = _safe_text(exc) if isinstance(exc, ValueError) else f"比较未完成（{type(exc).__name__}），请检查行情数据后重试"
                _progress(aid, stage="failed", message=message)
                cfg = get(aid)["config"]
                for model in cfg["progress"]["models"]:
                    if model["status"] in ACTIVE:
                        model.update(status="failed", error=message)
                db.update_bt_arena_config(aid, cfg)
                db.update_bt_arena_status(aid, "failed")
    finally:
        with _LOCK:
            _JOBS.pop(aid, None)
