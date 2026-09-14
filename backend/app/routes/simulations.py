"""Asynchronous scenario simulation routes backed by PRONOIA-MiroFish gateway."""
from __future__ import annotations

import threading
import time
from typing import Any, Literal

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import config, db

router = APIRouter(prefix="/api", tags=["simulations"])
TERMINAL = {"completed", "partial", "failed", "cancelled"}
_sync_lock = threading.RLock()
_watch_lock = threading.RLock()
_watched_jobs: set[str] = set()


class StartSimulationRequest(BaseModel):
    source_graph_artifact_id: str = Field(..., min_length=1)
    question: str | None = None
    as_of: str | None = None
    horizon_days: int = Field(default=30, ge=1, le=365)
    mode: Literal["quick"] = "quick"
    max_actors: int | None = Field(default=None, ge=4, le=10)
    market: dict[str, Any] | None = None


def _gateway(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = requests.request(
            method,
            f"{config.SIMULATION_GATEWAY_URL}{path}",
            timeout=config.SIMULATION_GATEWAY_TIMEOUT,
            **kwargs,
        )
    except requests.RequestException as error:
        raise HTTPException(status_code=503, detail=f"推演网关不可用: {error}") from error
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = response.text[:300]
        raise HTTPException(status_code=502, detail=f"推演网关拒绝请求: {detail}")
    return response.json()


def _public(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key != "request_payload"}


def _build_gateway_payload(
    case_id: str, request: StartSimulationRequest
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not db.get_case(case_id):
        raise HTTPException(status_code=404, detail="case not found")
    graph = db.get_artifact(case_id, request.source_graph_artifact_id)
    if not graph or graph.get("kind") != "graph":
        raise HTTPException(status_code=404, detail="source evidence graph not found")
    payload = request.model_dump()
    payload["case_id"] = case_id
    payload["evidence_graph"] = graph["payload"]
    # Stable default is essential for idempotency: the same immutable graph
    # and parameters must compile to the same spec hash on repeated clicks.
    payload["as_of"] = request.as_of or graph["created_at"]
    return payload, graph


def _sync(job: dict[str, Any]) -> dict[str, Any]:
    # Multiple tabs may poll together. Serialize the terminal write so one
    # gateway result creates exactly one PRONOIA artifact.
    with _sync_lock:
        job = db.get_simulation_job(job["id"]) or job
        if job["status"] in TERMINAL and (job["status"] != "completed" or job.get("artifact_id")):
            return job
        remote = _gateway("GET", f"/v1/simulations/{job['gateway_job_id']}")
        artifact_id = job.get("artifact_id")
        if remote.get("status") in {"completed", "partial"} and remote.get("result") and not artifact_id:
            request_payload = job["request_payload"]
            mode_cn = "单次" if request_payload.get("mode") == "quick" else "校准"
            artifact = db.add_artifact(
                job["case_id"], None, "simulation", f"{mode_cn}多智能体情景推演", remote["result"]
            )
            artifact_id = artifact["id"]
        return db.update_simulation_job(
            job["id"],
            status=remote.get("status", job["status"]),
            stage=remote.get("stage", job["stage"]),
            progress=float(remote.get("progress", job["progress"])),
            error=remote.get("error"),
            artifact_id=artifact_id,
            finished_at=remote.get("finished_at"),
        ) or job


def _watch_until_terminal(job_id: str) -> None:
    try:
        while True:
            job = db.get_simulation_job(job_id)
            if not job or job.get("status") in TERMINAL:
                return
            try:
                synced = _sync(job)
            except HTTPException:
                time.sleep(3)
                continue
            if synced.get("status") in TERMINAL:
                return
            time.sleep(2)
    finally:
        with _watch_lock:
            _watched_jobs.discard(job_id)


def _schedule_sync(job_id: str) -> None:
    """Keep automatic runs durable even when no graph panel is open."""

    with _watch_lock:
        if job_id in _watched_jobs:
            return
        _watched_jobs.add(job_id)
    threading.Thread(
        target=_watch_until_terminal,
        args=(job_id,),
        name=f"simulation-sync-{job_id[:8]}",
        daemon=True,
    ).start()


def start_simulation_service(
    case_id: str, request: StartSimulationRequest
) -> dict[str, Any]:
    payload, _ = _build_gateway_payload(case_id, request)
    remote = _gateway("POST", "/v1/simulations", json=payload)
    job = db.create_simulation_job(
        case_id,
        request.source_graph_artifact_id,
        remote,
        {key: value for key, value in payload.items() if key != "evidence_graph"},
    )
    synced = _sync(job)
    if synced.get("status") not in TERMINAL:
        _schedule_sync(synced["id"])
    return _public(synced)


@router.post("/cases/{case_id}/simulations", status_code=202)
def start_simulation(case_id: str, request: StartSimulationRequest):
    return start_simulation_service(case_id, request)


@router.post("/cases/{case_id}/simulations/preview")
def preview_simulation(case_id: str, request: StartSimulationRequest):
    payload, _ = _build_gateway_payload(case_id, request)
    return _gateway("POST", "/v1/simulations/preview", json=payload)


@router.get("/simulations/{job_id}")
def get_simulation(job_id: str):
    job = db.get_simulation_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="simulation job not found")
    return _public(_sync(job))


@router.post("/simulations/{job_id}/cancel")
def cancel_simulation(job_id: str):
    job = db.get_simulation_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="simulation job not found")
    remote = _gateway("POST", f"/v1/simulations/{job['gateway_job_id']}/cancel")
    updated = db.update_simulation_job(
        job_id,
        status=remote.get("status", job["status"]),
        stage=remote.get("stage", job["stage"]),
        progress=float(remote.get("progress", job["progress"])),
        error=remote.get("error"),
        finished_at=remote.get("finished_at"),
    )
    return _public(updated or job)


@router.post("/simulations/{job_id}/resume", status_code=202)
def resume_simulation(job_id: str):
    job = db.get_simulation_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="simulation job not found")
    remote = _gateway("POST", f"/v1/simulations/{job['gateway_job_id']}/resume")
    updated = db.update_simulation_job(
        job_id,
        status=remote.get("status", "queued"),
        stage=remote.get("stage", "resuming"),
        progress=float(remote.get("progress", job["progress"])),
        error=remote.get("error"),
        finished_at=remote.get("finished_at"),
    ) or job
    _schedule_sync(job_id)
    return _public(updated)


@router.get("/cases/{case_id}/simulations")
def list_simulations(case_id: str):
    if not db.get_case(case_id):
        raise HTTPException(status_code=404, detail="case not found")
    return [_public(job) for job in db.list_simulation_jobs(case_id)]
