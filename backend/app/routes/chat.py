"""POST /api/chat — SSE 对话流 (design.md §6.2/§7)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import config, db
from ..agents.roster import AGENTS, get_agent, system_prompt
from ..agents.team import run_team
from ..llm import complete_text, redact_runtime_error, resolve_runtime_target, run_agent, runtime_target_context
from ..schemas import ChatRequest, sse
from .simulations import StartSimulationRequest, start_simulation_service

router = APIRouter(prefix="/api", tags=["chat"])


def _history_for_llm(case_id: str) -> list[dict]:
    """该 case 最近 CONTEXT_MESSAGES 条消息 → LLM 上下文（user/assistant 纯文本）。"""
    msgs = db.list_messages(case_id, limit=config.CONTEXT_MESSAGES)
    out = []
    for m in msgs:
        if m["role"] in ("user", "assistant") and (m.get("content") or "").strip():
            out.append({"role": m["role"], "content": m["content"]})
    return out


async def _gen_title(question: str) -> str:
    import re

    fallback = re.sub(r"\s+", "", question or "")[:15] or "未命名研究"
    try:
        title = await complete_text(
            "你是标题生成器。用不超过15个字的中文概括用户的研究问题，只输出标题本身，不要标点收尾。",
            question[:300],
            max_tokens=800,  # reasoning 模型会消耗部分预算在思考上，给足余量
        )
        title = (title or "").strip().strip("「」\"'。 \n")
        return title[:20] or fallback
    except Exception:  # noqa: BLE001
        return fallback


async def _chat_stream_pinned(req: ChatRequest) -> AsyncIterator[str]:
    case_id = req.case_id
    case = db.get_case(case_id) if case_id else None
    if case is None:
        case = db.create_case()
    case_id = case["id"]

    # 1) 落库 user message；上下文取最近 12 条（含本条）
    is_first = db.count_messages(case_id, role="user") == 0
    db.add_message(case_id, role="user", content=req.message)
    history = _history_for_llm(case_id)

    message_id = db.new_id()
    state = {"content": "", "tool_trace": [], "rounds": 0}
    created_graphs: list[dict] = []
    artifact_cache: dict[str, dict] = {}
    persisted_trace: list[dict] = []
    record_agent = req.agent if req.mode == "agent" and req.agent else "router"
    last_checkpoint = 0.0
    last_snapshot: tuple[int, int] = (-1, -1)
    stream_completed = False

    # Create the assistant row before any long-running work. Artifacts created
    # during streaming now always refer to a durable message, even if the
    # browser disconnects or the async generator is cancelled.
    db.add_message(
        case_id,
        role="assistant",
        agent=record_agent,
        content="",
        message_id=message_id,
    )

    def remember_event(event: dict) -> None:
        event_type = str(event.get("type") or "")
        if event_type not in {
            "tool_call", "tool_result", "artifact", "agent_step", "logic_items"
        }:
            return
        if event_type == "artifact":
            artifact = event.get("artifact") or {}
            persisted_trace.append({
                "type": "artifact",
                "agent": event.get("agent"),
                "artifact_id": artifact.get("id"),
                "kind": artifact.get("kind"),
                "title": artifact.get("title"),
            })
            return
        persisted_trace.append(dict(event))

    async def checkpoint(*, force: bool = False) -> None:
        nonlocal last_checkpoint, last_snapshot
        snapshot = (len(state.get("content") or ""), len(persisted_trace))
        now = time.monotonic()
        if snapshot == last_snapshot:
            return
        if not force and now - last_checkpoint < 0.75:
            return
        db.update_message(
            message_id,
            content=state.get("content") or "",
            tool_trace=persisted_trace or None,
            agent=record_agent,
        )
        last_checkpoint = now
        last_snapshot = snapshot

    async def artifact_store(kind: str, title: str, payload):
        fingerprint = hashlib.sha256(
            json.dumps(
                {"kind": kind, "title": title, "payload": payload},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        cached = artifact_cache.get(fingerprint)
        if cached is not None:
            return {**cached, "_reused": True}
        row = await asyncio.to_thread(
            db.add_artifact, case_id, message_id, kind, title, payload
        )
        artifact_cache[fingerprint] = row
        if kind == "graph":
            created_graphs.append(row)
        return row

    yield sse({"type": "meta", "case_id": case_id, "mode": req.mode, "agent": req.agent,
               "team_members": req.team_members})
    try:
        if req.mode == "team":
            # team 模式：history 传给 synthesize；问题原文作为规划输入
            hist_for_team = history[:-1] if history else []  # 排除当前 user 消息
            handoff_job: dict | None = None
            handoff_error = ""
            attempted_graph_ids: set[str] = set()
            async for ev in run_team(req.message, hist_for_team, state, artifact_store,
                                     team_members=req.team_members):
                remember_event(ev)
                await checkpoint(force=ev.get("type") in {"artifact", "agent_step"})
                yield sse(ev)
                planned_agents = {
                    str(item.get("agent") or "")
                    for item in state.get("team_plan", [])
                }
                artifact = ev.get("artifact") or {}
                graph_id = str(artifact.get("id") or "")
                if (
                    handoff_job is None
                    and "predictor" in planned_agents
                    and artifact.get("kind") == "graph"
                    and graph_id
                    and graph_id not in attempted_graph_ids
                ):
                    attempted_graph_ids.add(graph_id)
                    try:
                        handoff_job = await asyncio.to_thread(
                            start_simulation_service,
                            case_id,
                            StartSimulationRequest(
                                source_graph_artifact_id=graph_id,
                                question=(artifact.get("payload") or {}).get("question") or req.message,
                            ),
                        )
                        handoff_event = {
                            "type": "agent_step",
                            "phase": "simulation_started",
                            "agent": "predictor",
                            "note": "证据图已通过校验，单次多智能体推演已在后台启动，通常需要 5～10 分钟；聊天结束后仍会继续运行。",
                            "simulation_job_id": handoff_job["id"],
                        }
                        state["tool_trace"].append(handoff_event)
                        remember_event(handoff_event)
                        await checkpoint(force=True)
                        yield sse(handoff_event)
                    except Exception as error:  # noqa: BLE001
                        handoff_error = (
                            str(error.detail)
                            if isinstance(error, HTTPException)
                            else f"{type(error).__name__}: {error}"
                        )
            planned_agents = {
                str(item.get("agent") or "")
                for item in state.get("team_plan", [])
            }
            if "predictor" in planned_agents and handoff_job is None:
                if not created_graphs:
                    skipped_event = {
                        "type": "agent_step",
                        "phase": "simulation_skipped",
                        "agent": "predictor",
                        "note": "主 Agent 已派出事件预测员，但本轮没有生成可用证据图，已安全跳过多智能体推演。",
                    }
                    state["tool_trace"].append(skipped_event)
                    remember_event(skipped_event)
                    await checkpoint(force=True)
                    yield sse(skipped_event)
                else:
                    skipped_event = {
                        "type": "agent_step",
                        "phase": "simulation_skipped",
                        "agent": "predictor",
                        "note": f"推演未能安全启动，研究结果不受影响：{handoff_error or '证据图未通过入口校验'}",
                    }
                    state["tool_trace"].append(skipped_event)
                    remember_event(skipped_event)
                    await checkpoint(force=True)
                    yield sse(skipped_event)
        else:
            # mode == "agent" | "auto"：单 Agent 工具循环
            # 优先级：req.agent → "router"（向后兼容）
            agent_id = (req.agent or "").strip() or "router"
            agent_def = get_agent(agent_id)
            if agent_def is None:
                valid = ", ".join(sorted(AGENTS.keys()))
                yield sse({"type": "error",
                           "message": f"未知 Agent「{agent_id}」。可用: {valid}"})
                return
            messages = [{"role": "system", "content": system_prompt(agent_id)}] + history
            async for ev in run_agent(agent_id, messages, agent_def=agent_def,
                                      state=state, artifact_store=artifact_store,
                                      max_rounds=config.AUTO_MAX_ROUNDS):
                remember_event(ev)
                await checkpoint(force=ev.get("type") in {"artifact", "tool_result"})
                yield sse(ev)

        # 2) Finalize the durable assistant message.
        await checkpoint(force=True)

        # 3) 首条消息 → 生成 case 标题
        if is_first:
            title = await _gen_title(req.message)
            await asyncio.to_thread(db.update_case_title, case_id, title)
            yield sse({"type": "case_title", "title": title})

        stream_completed = True
        yield sse({"type": "done", "case_id": case_id, "message_id": message_id})
    except asyncio.CancelledError:
        # 前端断开 / 用户主动停止：尽力保存已产出内容，然后让 ASGI 正常收尾
        # 不 yield error（连接已断，前端收不到），只落库 + 日志
        try:
            if state["content"] or state["tool_trace"]:
                err_agent = req.agent if req.mode == "agent" and req.agent else "router"
                db.add_message(case_id, role="assistant", agent=err_agent,
                               content=state["content"],
                               tool_trace=state["tool_trace"] or None,
                               message_id=message_id)
        except Exception:  # noqa: BLE001
            pass
        print(f"CHAT case={case_id} cancelled by client (rounds={state.get('rounds', 0)}, "
              f"content_len={len(state.get('content', ''))})", flush=True)
        raise
    except Exception as e:  # noqa: BLE001
        remember_event({
            "type": "agent_step",
            "phase": "interrupted",
            "note": redact_runtime_error(f"{type(e).__name__}: {e}"),
        })
        yield sse({"type": "error", "message": redact_runtime_error(f"{type(e).__name__}: {e}")})
    finally:
        # asyncio.CancelledError / GeneratorExit are not reliably caught by an
        # Exception handler. A final checkpoint preserves partial progress on
        # refresh, tab close, explicit stop, or server-side cancellation.
        try:
            if not stream_completed and not any(
                item.get("phase") == "interrupted" for item in persisted_trace
            ):
                remember_event({
                    "type": "agent_step",
                    "phase": "interrupted",
                    "note": "研究连接在完成前结束；以下为已保存的阶段性进展，不能视为完整结论。",
                })
            await checkpoint(force=True)
        except BaseException:  # noqa: BLE001
            pass


async def _chat_stream(req: ChatRequest) -> AsyncIterator[str]:
    """Keep endpoint, credential and model id consistent for one chat turn."""

    target = resolve_runtime_target()
    with runtime_target_context(target):
        stream = _chat_stream_pinned(req)
        try:
            async for event in stream:
                yield event
        finally:
            # Explicitly close the delegated async generator so its durability
            # checkpoint finishes before callers tear down the database.
            await stream.aclose()


@router.post("/chat")
async def chat(req: ChatRequest):
    return StreamingResponse(
        _chat_stream(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
