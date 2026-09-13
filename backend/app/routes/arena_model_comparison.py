"""Same-event / same-asset Arena; no model inference is triggered here."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ConfigDict

from ..event_backtest import model_comparison

router = APIRouter(prefix="/api/arena/model-comparison", tags=["arena"])


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    subject_key: str = Field(min_length=1, max_length=200)
    run_ids: list[str] = Field(min_length=2, max_length=8)
    market_source_id: str = Field(min_length=1, max_length=250)
    start_date: date | None = None
    end_date: date | None = None
    holding_bars: int = Field(3, ge=1, le=1000)
    fee_bps: float = Field(3, ge=0, le=1000)
    slippage_bps: float = Field(2, ge=0, le=1000)
    initial_capital: float = Field(100000, gt=0, le=1e12)
    position_rule: Literal["long_flat"] = "long_flat"


class SaveComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    preview_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@router.get("/catalog")
def comparison_catalog():
    return model_comparison.catalog()


@router.post("/preview")
def comparison_preview(request: ComparisonRequest):
    try:
        return model_comparison.preview(request.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/save")
def save_comparison(request: SaveComparisonRequest):
    if not request.name.strip():
        raise HTTPException(status_code=422, detail="请填写比较名称")
    try:
        return model_comparison.save(request.preview_id, request.name.strip())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
