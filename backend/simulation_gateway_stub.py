"""MiroFish 推演网关 stub — 实现必需的 API 契约，不依赖外部运行时。

当真实 MiroFish / OASIS 网关不可用时，此 stub 提供最小可用响应，
使 Pronoia 推演功能不因连接拒绝而中断。

启动: python -m simulation_gateway_stub  (或 uvicorn simulation_gateway_stub:app --port 5010)
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Pronoia Simulation Gateway (stub)")

# In-memory job store
_jobs: dict[str, dict[str, Any]] = {}
_followups: dict[str, dict[str, Any]] = {}


@app.get("/health")
def health():
    return {"status": "ok", "mode": "stub"}


@app.post("/v1/simulations/preview")
async def preview(request: Request):
    body = await request.json()
    max_actors = body.get("max_actors")
    recommended = min(max_actors or 4, 6)
    actors = [
        {
            "id": f"actor_{i}",
            "label": f"参与方 {i + 1}",
            "kind": "issuer" if i == 0 else "stakeholder",
            "selection_reason": "基于证据图自动推荐",
        }
        for i in range(recommended)
    ]
    return {
        "actor_selection": {
            "mode": "auto",
            "recommended_count": recommended,
            "applied_limit": recommended,
            "configured_count": recommended,
            "rationale": "stub 预览：基于证据图节点数量推荐",
        },
        "actors": actors,
    }


@app.post("/v1/simulations")
async def create_simulation(request: Request):
    body = await request.json()
    rerun = body.get("rerun", False)
    if not rerun:
        # 查找已存在的同 case + graph 任务
        for job in _jobs.values():
            if (
                job.get("case_id") == body.get("case_id")
                and job.get("source_graph_artifact_id") == body.get("source_graph_artifact_id")
                and job.get("status") not in {"failed", "cancelled"}
            ):
                return job
    job_id = f"stub_{uuid.uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "error": None,
        "finished_at": None,
        "result": None,
        "case_id": body.get("case_id"),
        "source_graph_artifact_id": body.get("source_graph_artifact_id"),
        "created_at": time.time(),
    }
    _jobs[job_id] = job
    # stub: 立即标记完成
    job["status"] = "completed"
    job["stage"] = "completed"
    job["progress"] = 1
    job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    job["result"] = {
        "schema_version": "0.1.0",
        "execution": {"configured_actor_count": 4},
        "scenarios": [
            {
                "id": "branch-1",
                "label": "基准情景",
                "actors": [],
                "actions": [],
                "triggers": [],
                "invalidators": [],
            }
        ],
        "warnings": ["stub 网关：此为占位结果，未运行真实多智能体推演"],
    }
    return job


@app.get("/v1/simulations/{job_id}")
async def get_simulation(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"detail": "任务不存在"})
    return job


@app.post("/v1/simulations/{job_id}/cancel")
async def cancel_simulation(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"detail": "任务不存在"})
    job["status"] = "cancelled"
    job["stage"] = "cancelled"
    job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    return job


@app.post("/v1/simulations/{job_id}/resume")
async def resume_simulation(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"detail": "任务不存在"})
    job["status"] = "queued"
    job["stage"] = "resuming"
    job["progress"] = 0.2
    job["error"] = None
    job["finished_at"] = None
    # stub: 立即完成
    job["status"] = "completed"
    job["stage"] = "completed"
    job["progress"] = 1
    job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    return job


@app.get("/v1/simulations/{job_id}/followup")
async def get_followup(job_id: str):
    fu = _followups.get(job_id, {"snapshot": None, "journal": [], "checkpoints": []})
    return fu


@app.post("/v1/simulations/{job_id}/followup")
async def save_followup(job_id: str, request: Request):
    body = await request.json()
    _followups[job_id] = {
        "snapshot": body.get("snapshot"),
        "journal": body.get("journal", []),
        "checkpoints": [],
    }
    return _followups[job_id]


@app.post("/v1/simulations/{job_id}/followup/entries")
async def add_followup_entry(job_id: str, request: Request):
    body = await request.json()
    fu = _followups.setdefault(job_id, {"snapshot": None, "journal": [], "checkpoints": []})
    if "journal" not in fu or fu["journal"] is None:
        fu["journal"] = []
    fu["journal"].append(body)
    return fu


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PRONOIA_SIMULATION_GATEWAY_PORT", "5010"))
    host = os.getenv("PRONOIA_SIMULATION_GATEWAY_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port)
