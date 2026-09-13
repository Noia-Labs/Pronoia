"""Local prediction-file preview, template download and explicit confirmation."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from ..event_backtest.prediction_import import (
    MAX_UPLOAD_BYTES, PredictionImportError, commit_predictions,
    inspect_predictions, prediction_template,
)
from ..schemas import BTDatasetResponse

router = APIRouter(prefix="/api/bt/prediction-import", tags=["backtest"])


def _error(exc: PredictionImportError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


async def _read_upload(file: UploadFile) -> bytes:
    try:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await file.close()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="预测文件不能超过 10 MiB。")
    return content


@router.get("/template")
def template(dataset_id: str = Query(...), horizon: str = Query(...), format: str = Query("csv")):
    try:
        content, media_type = prediction_template(dataset_id=dataset_id, horizon=horizon, format=format)
    except PredictionImportError as exc:
        raise _error(exc) from exc
    return Response(content=content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="predictions-template.{format}"',
        "Cache-Control": "no-store",
    })


@router.post("/preview")
async def preview(file: UploadFile = File(...), dataset_id: str = Form(...), horizon: str = Form(...)):
    content = await _read_upload(file)
    try:
        result, _ = inspect_predictions(dataset_id=dataset_id, horizon=horizon,
                                        filename=file.filename or "", content=content)
        return result
    except PredictionImportError as exc:
        raise _error(exc) from exc


@router.post("/commit", response_model=BTDatasetResponse, status_code=201)
async def commit(file: UploadFile = File(...), dataset_id: str = Form(...), horizon: str = Form(...),
                 preview_token: str = Form(...), name: str = Form("")):
    from .backtest import _dataset_response

    content = await _read_upload(file)
    try:
        result = commit_predictions(dataset_id=dataset_id, horizon=horizon, filename=file.filename or "",
                                    content=content, preview_token=preview_token, name=name)
        return _dataset_response(result)
    except PredictionImportError as exc:
        raise _error(exc) from exc
