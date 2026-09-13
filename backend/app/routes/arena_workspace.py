"""Select saved models, a common target and dates, then run one Arena."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..event_backtest import arena_workspace as service
from ..event_backtest.arena_workspace_catalog import catalog

router = APIRouter(prefix="/api/arena/workspace", tags=["arena"])


class WorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    # Optional for backward compatibility with clients created before Arena was
    # split into two tracks.  New clients always send it; the service derives
    # and validates the same value when it is absent.
    track: Literal["quant", "event"] | None = None
    target_scope: Literal["asset", "single_event", "event_set"] | None = None
    model_ids: list[str] = Field(min_length=2, max_length=8)
    target_id: str = Field(min_length=1, max_length=250)
    frequency: str | None = Field(None, min_length=1, max_length=16)
    start_date: date
    end_date: date
    name: str | None = Field(None, max_length=120)
    holding_bars: int = Field(3, ge=1, le=1000)
    fee_bps: float = Field(3, ge=0, le=1000)
    slippage_bps: float = Field(2, ge=0, le=1000)
    initial_capital: float = Field(100000, gt=0, le=1e12)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")

    @field_validator("frequency")
    @classmethod
    def normalize_frequency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower().replace("minute", "min").replace("min", "m")
        normalized = {"d": "1d", "day": "1d", "daily": "1d"}.get(normalized, normalized)
        if normalized not in {"1d", "1m", "5m", "15m", "30m", "60m"}:
            raise ValueError("K 线周期仅支持 1d、1m、5m、15m、30m、60m")
        return normalized


@router.get("/catalog")
def workspace_catalog():
    return catalog()


@router.post("")
def create_workspace(request: WorkspaceRequest):
    try:
        return service.create(request.model_dump(mode="json"))
    except service.WorkspaceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{arena_id}")
def get_workspace(arena_id: str):
    try:
        return service.get(arena_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Arena 不存在") from exc


@router.post("/{arena_id}/cancel")
def cancel_workspace(arena_id: str):
    try:
        return service.cancel(arena_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Arena 不存在") from exc
