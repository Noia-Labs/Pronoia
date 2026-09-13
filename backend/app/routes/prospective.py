from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..prospective import service

router = APIRouter(prefix="/api/prospective", tags=["prospective"])


class CreateProspectiveRunRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    capture_at: str
    settle_after_days: int = Field(default=3, ge=0, le=365)
    source_config: dict[str, Any] = Field(default_factory=lambda: {"source": "sample", "max_items": 1})
    predictor_config: dict[str, Any] = Field(default_factory=dict)
    metric_config: dict[str, Any] = Field(default_factory=lambda: {"epsilon": 0.005})


@router.get("/runs")
def list_runs(limit: int = 100) -> dict[str, Any]:
    items = db.list_prospective_runs(limit=max(1, min(limit, 500)))
    return {"total": len(items), "items": items}


@router.post("/runs")
def create_run(req: CreateProspectiveRunRequest) -> dict[str, Any]:
    try:
        capture_at = service._parse_iso(req.capture_at)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"capture_at must be ISO datetime: {exc}") from exc
    source_config = dict(req.source_config)
    source = str(source_config.get("source") or "sample")
    if source not in {"sample", "cn_announcements", "us_sec", "macro"}:
        raise HTTPException(status_code=400, detail=f"unsupported source: {source}")
    source_config.setdefault("candidate_lookback_trade_days", source_config.get("lookback_trade_days") or 3)
    source_config.setdefault("analysis_lookback_trade_days", 20)
    source_config.setdefault("evidence_cutoff_policy", "before_capture_date")
    for key, default, upper in (
        ("candidate_lookback_trade_days", 3, 60), ("analysis_lookback_trade_days", 20, 250),
        ("candidate_limit", 100, 500), ("selection_count", 5, 100),
    ):
        try:
            value = int(source_config.get(key) or default)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{key} must be an integer") from exc
        if value < 1 or value > upper:
            raise HTTPException(status_code=400, detail=f"{key} must be between 1 and {upper}")
    for key in ("keywords", "symbols"):
        value = source_config.get(key)
        if value is not None and not isinstance(value, list):
            raise HTTPException(status_code=400, detail=f"{key} must be an array")
    if int(source_config["analysis_lookback_trade_days"]) < int(source_config["candidate_lookback_trade_days"]):
        raise HTTPException(status_code=400, detail="analysis_lookback_trade_days must be at least candidate_lookback_trade_days")
    if source_config["evidence_cutoff_policy"] not in {"before_capture_date", "timestamp_verified"}:
        raise HTTPException(status_code=400, detail="unsupported evidence_cutoff_policy")
    metric_config = dict(req.metric_config)
    settlement_mode = str(metric_config.get("settlement_mode") or "after_trade_days")
    if settlement_mode not in {"after_trade_days", "absolute_trade_date"}:
        raise HTTPException(status_code=400, detail="unsupported settlement_mode")
    if settlement_mode == "after_trade_days" and req.settle_after_days < 1:
        raise HTTPException(status_code=400, detail="settle_after_days must be at least 1")
    if settlement_mode == "after_trade_days":
        # The configured settlement period is also the prediction horizon.
        # Override legacy clients that still send a separate horizon value.
        metric_config["horizon"] = req.settle_after_days
    else:
        # A fixed settlement date determines the horizon; do not persist a second one.
        metric_config.pop("horizon", None)
    if settlement_mode == "absolute_trade_date":
        try:
            target = service._parse_iso(str(req.metric_config.get("settlement_trade_date") or ""))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="settlement_trade_date must be YYYY-MM-DD") from exc
        if target.date() <= capture_at.date():
            raise HTTPException(status_code=400, detail="settlement_trade_date must be after capture date")
        if not service._is_trading_date(target.date()):
            raise HTTPException(status_code=400, detail="settlement_trade_date must be an exchange trading day")
    return db.create_prospective_run(
        name=req.name, capture_at=req.capture_at, settle_after_days=req.settle_after_days,
        source_config=source_config, predictor_config=req.predictor_config, metric_config=metric_config,
    )


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    result = service.detail(run_id)
    if not result:
        raise HTTPException(status_code=404, detail="prospective run not found")
    return result


@router.post("/runs/{run_id}/capture")
async def capture_run(run_id: str) -> dict[str, Any]:
    try:
        return await service.capture_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        db.update_prospective_run(run_id, status="failed", error_message=f"capture: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/runs/{run_id}/settle")
def settle_run(run_id: str, force: bool = False) -> dict[str, Any]:
    try:
        return service.settle_run(run_id, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict[str, Any]:
    run = db.get_prospective_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="prospective run not found")
    return db.update_prospective_run(run_id, status="cancelled") or run


@router.get("/runs/{run_id}/metrics")
def get_metrics(run_id: str) -> dict[str, Any]:
    if not db.get_prospective_run(run_id):
        raise HTTPException(status_code=404, detail="prospective run not found")
    return {"metrics": service._metrics(run_id), "snapshots": db.list_prospective_metric_snapshots(run_id)}


@router.get("/runs/{run_id}/items/{item_id}/trace")
def get_item_trace(run_id: str, item_id: str) -> dict[str, Any]:
    detail = service.detail(run_id)
    row = next((value for value in (detail or {}).get("items", []) if value["item"]["id"] == item_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="prospective item not found")
    return {"run_id": run_id, "item_id": item_id, "trace": row.get("trace") or []}
