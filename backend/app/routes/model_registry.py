"""Save model configurations once, then start any number of data tests."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated, Literal

from .. import model_registry as service

router = APIRouter(prefix="/api/bt/models", tags=["saved-models"])


class SaveModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=120)
    category: Literal["event", "quant"]
    kind: str | None = None
    runner: str | None = None
    profile_id: str | None = None
    strategy_spec: dict | None = None
    model_identity: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")


class RenameModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)


class DeleteModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=1, max_length=100)


class UpdateModelConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=120)
    strategy_spec: dict | None = None


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="模型不存在，请刷新模型列表") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=service._safe(str(exc))) from exc


@router.get("")
def list_models(category: Literal["event", "quant"] | None = Query(None)):
    return _call(service.list_models, category)


@router.post("")
def save_model(payload: SaveModelRequest):
    return _call(service.save_model, payload.model_dump())


@router.post("/match")
def match_model(payload: SaveModelRequest):
    return _call(service.match_model, payload.model_dump())


@router.post("/delete")
def delete_models(payload: DeleteModelsRequest):
    return _call(service.delete_models, payload.model_ids)


@router.get("/{model_id}/setup")
def get_setup(model_id: str):
    return _call(service.public_setup, model_id)


@router.get("/{model_id}/configuration")
def get_configuration(model_id: str):
    return _call(service.get_model, model_id)


@router.put("/{model_id}/configuration")
def update_configuration(model_id: str, payload: UpdateModelConfigurationRequest):
    return _call(service.update_configuration, model_id, payload.model_dump())


@router.patch("/{model_id}")
def rename_model(model_id: str, payload: RenameModelRequest):
    return _call(service.rename_model, model_id, payload.name)


@router.get("/{model_id}")
def get_model(model_id: str):
    return _call(service.get_model, model_id)
