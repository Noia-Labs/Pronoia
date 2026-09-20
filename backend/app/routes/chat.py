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


# ---------------------------------------------------------------------------
# Detached generation: 公网预览网关会间歇性掐断 SSE 长连接（浏览器报
# ERR_INCOMPLETE_CHUNKED_ENCODING）。生成任务不能跟随连接一起死：
# _run_chat 作为独立 asyncio.Task 运行，SSE 包装器只是它的一个"订阅者"。
# 客户端断连后任务继续执行并落库，前端通过 /chat/status 轮询取回结果。
# ---------------------------------------------------------------------------

# case_id -> 正在生成的任务（同一 case 同时只允许一条活跃生成）
_ACTIVE_RUNS: dict[str, "asyncio.Task[None]"] = {}

_STOP = object()  # 队列结束哨兵


async def _run_chat(req: ChatRequest, queue: "asyncio.Queue") -> None:
    """执行一次完整生成；事件写入 queue（订阅者已断开时自动丢弃）。

    该函数在独立 Task 中运行：浏览器断连只影响 SSE 订阅者，不影响本任务。
    """
    case_id = req.case_id
    case = db.get_case(case_id) if case_id else None
    if case is None:
        case = db.create_case()
    case_id = case["id"]

    def emit(event: dict) -> None:
        try:
            queue.put_nowait(sse(event))  # type: ignore[arg-type]
        except asyncio.QueueFull:
            pass  # 订阅者断开或积压：落库才是真相源，事件可丢

    # 1) 落库 user message；上下文取最近 12 条（含本条）
    #    断流重试签名：尾部正好是 [user(同文本), assistant(空)] 时，
    #    复用该 assistant 行，不再追加重复的 user 消息。
    msgs = db.list_messages(case_id, limit=3)
    retry_tail = (
        len(msgs) >= 2
        and msgs[-1]["role"] == "assistant"
        and not (msgs[-1].get("content") or "").strip()
        and msgs[-2]["role"] == "user"
        and (msgs[-2].get("content") or "") == req.message
    )
    is_first = db.count_messages(case_id, role="user") == 0
    if not retry_tail:
        db.add_message(case_id, role="user", content=req.message)
    history = _history_for_llm(case_id)

    message_id = db.new_id()
    state = {"content": "", "tool_trace": [], "rounds": 0}
    created_graphs: list[dict] = []
    artifact_cache: dict[str, dict] = {}
    persisted_trace: list[dict] = []
    record_agent = req.agent if req.mode == "agent" and req.agent else "router"
    last_checkpoint = 0.0
    last_snapshot: tuple[int, ...] = (-1, -1, -1)
    stream_completed = False

    # Create the assistant row before any long-running work. Artifacts created
    # during streaming now always refer to a durable message, even if the
    # browser disconnects or the async generator is cancelled.
    if retry_tail:
        # 重试复用上一轮留下的空 assistant 行，避免累积 [user, 空] 垃圾对
        message_id = msgs[-1]["id"]
    else:
        db.add_message(
            case_id,
            role="assistant",
            agent=record_agent,
            content="",
            message_id=message_id,
        )

    def remember_event(event: dict) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "thinking":
            # 推理过程也持久化：断流/纯轮询模式下前端只能依靠落库的
            # tool_trace 还原现场，thinking 不存就会"没有推理过程"。
            # 同一 agent 的连续 delta 合并追加到尾部条目，避免每 token 一行。
            delta = str(event.get("delta") or "")
            if not delta:
                return
            agent = event.get("agent")
            last = persisted_trace[-1] if persisted_trace else None
            if (
                isinstance(last, dict)
                and last.get("type") == "thinking"
                and last.get("agent") == agent
            ):
                last["text"] = str(last.get("text") or "") + delta
            else:
                persisted_trace.append(
                    {"type": "thinking", "agent": agent, "text": delta}
                )
            return
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
        # 末位 thinking 条目是原地追加的，len(persisted_trace) 不变，
        # 因此 snapshot 额外纳入 thinking 总长度，保证思考增量也能触发落库
        thinking_len = sum(
            len(str(item.get("text") or ""))
            for item in persisted_trace
            if item.get("type") == "thinking"
        )
        snapshot = (len(state.get("content") or ""), len(persisted_trace), thinking_len)
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

    emit({"type": "meta", "case_id": case_id, "mode": req.mode, "agent": req.agent,
          "team_members": req.team_members})
    task = asyncio.current_task()
    if task is not None:
        _ACTIVE_RUNS[case_id] = task
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
                emit(ev)
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
                            "note": "证据图已通过校验，单次多智能体推演已在后台启动，聊天结束后仍会继续运行，可在推演窗口查看进度。",
                            "simulation_job_id": handoff_job["id"],
                        }
                        state["tool_trace"].append(handoff_event)
                        remember_event(handoff_event)
                        await checkpoint(force=True)
                        emit(handoff_event)
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
                    emit(skipped_event)
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
                    emit(skipped_event)
        else:
            # mode == "agent" | "auto"：单 Agent 工具循环
            # 优先级：req.agent → "router"（向后兼容）
            agent_id = (req.agent or "").strip() or "router"
            agent_def = get_agent(agent_id)
            if agent_def is None:
                valid = ", ".join(sorted(AGENTS.keys()))
                emit({"type": "error",
                      "message": f"未知 Agent「{agent_id}」。可用: {valid}"})
                return
            messages = [{"role": "system", "content": system_prompt(agent_id)}] + history
            async for ev in run_agent(agent_id, messages, agent_def=agent_def,
                                      state=state, artifact_store=artifact_store,
                                      max_rounds=config.AUTO_MAX_ROUNDS):
                remember_event(ev)
                await checkpoint(force=ev.get("type") in {"artifact", "tool_result"})
                emit(ev)

        # 2) Finalize the durable assistant message.
        await checkpoint(force=True)

        # 3) 首条消息 → 生成 case 标题
        if is_first:
            title = await _gen_title(req.message)
            await asyncio.to_thread(db.update_case_title, case_id, title)
            emit({"type": "case_title", "title": title})

        stream_completed = True
        emit({"type": "done", "case_id": case_id, "message_id": message_id})
    except asyncio.CancelledError:
        # 用户主动停止（/chat/cancel）：尽力保存已产出内容后退出。
        # 客户端单纯断开不再走到这里——生成在独立 Task 中继续。
        try:
            await checkpoint(force=True)
        except BaseException:  # noqa: BLE001
            pass
        print(f"CHAT case={case_id} cancelled by user (rounds={state.get('rounds', 0)}, "
              f"content_len={len(state.get('content', ''))})", flush=True)
        raise
    except Exception as e:  # noqa: BLE001
        remember_event({
            "type": "agent_step",
            "phase": "interrupted",
            "note": redact_runtime_error(f"{type(e).__name__}: {e}"),
        })
        emit({"type": "error", "message": redact_runtime_error(f"{type(e).__name__}: {e}")})
    finally:
        # A final checkpoint preserves partial progress on refresh, tab close,
        # explicit stop, or server-side cancellation.
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
        # 无论正常结束、出错还是被取消：注销活跃任务并通知订阅者收尾
        if _ACTIVE_RUNS.get(case_id) is task:
            del _ACTIVE_RUNS[case_id]
        try:
            queue.put_nowait(_STOP)  # type: ignore[arg-type]
        except asyncio.QueueFull:
            pass


_SSE_HEARTBEAT_SECONDS = 4.0


async def _chat_stream(req: ChatRequest) -> AsyncIterator[str]:
    """SSE 订阅器：生成在独立 Task 中运行，本函数只负责转发 + 心跳。

    客户端断连（网关掐流）只终止本订阅器；_run_chat 继续执行并落库，
    前端随后通过 GET /chat/status/{case_id} 轮询取回结果。
    静默超过 4s 时发送 `data: {"type":"ping"}` 保活帧。
    """

    queue: asyncio.Queue = asyncio.Queue(maxsize=2048)
    # 任务在创建时继承 contextvar，整轮生成固定同一 runtime target
    target = resolve_runtime_target()
    with runtime_target_context(target):
        generation = asyncio.ensure_future(_run_chat(req, queue))
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield 'data: {"type":"ping"}\n\n'
                continue
            if event is _STOP:
                break
            yield event  # type: ignore[misc]
    finally:
        # 不 cancel generation：客户端断开恰恰是 detach 生效的场景。
        # Task 引用由 _ACTIVE_RUNS 持有，不会被 GC。
        _ = generation


@router.post("/chat")
async def chat(req: ChatRequest, detach: bool = False):
    if detach:
        # 纯轮询模式：立即启动 detached 生成并返回 case_id，不建立 SSE 长连接。
        # 用于公网预览网关频繁掐断长连接的环境——短 JSON 响应几乎不会被掐，
        # 前端随后通过 GET /cases/{id} 轮询已落库进度。
        case = db.get_case(req.case_id) if req.case_id else None
        if case is None:
            case = db.create_case()
        req.case_id = case["id"]
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)  # 无人订阅，事件满了即丢
        target = resolve_runtime_target()
        with runtime_target_context(target):
            asyncio.ensure_future(_run_chat(req, queue))
        return {"case_id": case["id"]}
    return StreamingResponse(
        _chat_stream(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sse_probe")
async def sse_probe(seconds: int = 300):
    """诊断端点：每 2s 发一帧 ping 持续 N 秒，用于测量代理层 SSE 硬超时。"""
    async def gen() -> AsyncIterator[str]:
        start = time.time()
        n = 0
        while time.time() - start < seconds:
            n += 1
            yield 'data: {"type":"ping","n":%d,"t":%.1f}\n\n' % (n, time.time() - start)
            await asyncio.sleep(2)
        yield 'data: {"type":"done","frames":%d}\n\n' % n
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
