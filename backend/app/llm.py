"""Ark LLM client + streaming tool-call loop (design.md §2/§6.2).

实现约定：所有轮次都用 stream=True，累积 tool_calls deltas；本轮有 tool_calls
则执行技能并继续循环，无则为最终答复（token 已流式发出）。
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from openai import AsyncOpenAI
import httpx

from . import config
from .log_bus import publish
from .skills.registry import REGISTRY, ensure_skills_loaded, serialize_tool_result, tool_schema_subset, tools_for_agent

_client: Optional[AsyncOpenAI] = None
_skill_depth: ContextVar[int] = ContextVar("skill_execution_depth", default=0)
_SLOW_NESTED_SKILLS = frozenset({"event_study"})


def _skill_timeout(name: str, category: str, depth: int) -> float:
    """Choose a deadline that leaves the calling composite time to aggregate.

    Root calls preserve the historical FEVER_SKILL_TIMEOUT.  Nested composite
    skills get a near-root budget, ordinary atomic calls get a shorter budget,
    and known multi-provider atomics such as event_study get a small extension.
    """
    if depth <= 0:
        return config.SKILL_TIMEOUT
    if category == "skill":
        return min(config.SKILL_COMPOSITE_SUB_TIMEOUT, config.SKILL_TIMEOUT)
    if name in _SLOW_NESTED_SKILLS:
        return min(config.SKILL_SLOW_SUB_TIMEOUT, config.SKILL_TIMEOUT)
    return min(config.SKILL_SUB_TIMEOUT, config.SKILL_TIMEOUT)


async def _create_with_hard_timeout(awaitable):
    """Cap the SDK await even when a local proxy keeps the socket alive."""
    return await asyncio.wait_for(awaitable, timeout=config.LLM_TIMEOUT)


async def _stream_with_hard_timeout(stream):
    """Cap a complete streaming round instead of only individual socket reads."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.LLM_TIMEOUT
    iterator = stream.__aiter__()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                f"LLM streaming round exceeded {config.LLM_TIMEOUT:.0f}s"
            )
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        yield chunk


def get_client() -> AsyncOpenAI:
    """返回 AsyncOpenAI client。优先 MAAS，其次 ARK。统一 model_name 用 config.LLM_MODEL。"""
    global _client
    if _client is None:
        transport = None
        if config.LLM_FORCE_IPV4:
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2)
        # Do not inherit desktop HTTP(S)_PROXY values.  A stale local proxy can
        # accept TCP and then hang forever during the CONNECT TLS handshake,
        # making every concurrent expert fail at the same hard deadline.
        http_client = httpx.AsyncClient(
            transport=transport,
            timeout=config.LLM_TIMEOUT,
            trust_env=False,
        )
        _client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            timeout=config.LLM_TIMEOUT,
            http_client=http_client,
        )
    return _client


ArtifactStore = Callable[[str, str, Any], Awaitable[dict]]
SkillExecutor = Callable[[str, dict], Awaitable[dict]]


async def noop_artifact_store(kind: str, title: str, payload: Any) -> dict:
    return {"id": None, "kind": kind, "title": title, "payload": payload}


def _is_retryable_stream_error(error: BaseException) -> bool:
    """Recognize transport failures that are safe to retry before side effects."""

    return type(error).__name__ in {
        "RemoteProtocolError",
        "ReadError",
        "ReadTimeout",
        "APIConnectionError",
        "APITimeoutError",
    }


async def execute_skill(name: str, args: dict) -> dict:
    """Run a skill handler with timeout; never raises.

    Supports both sync and async handlers:
    - async handler: 直接 await（skill 内部 await sub-tool）
    - sync handler:  to_thread 跑（保持原行为）

    P0 未来函数防护（STRICT AS-OF）：当 FEVER_BT_STRICT_AS_OF=1 时，
    对 event_study_skill / event_study 强制注入 as_of=True + 正确 benchmark，
    防止 LLM 因 prompt 遗漏而暴露 post-event CAR。

    日志：每次调用打印 SKILL 行，含 name / ok / dur / [SLOW|VSLOW|TIMEOUT] 标签。
    """
    import os as _os
    _t0 = time.time()
    strict_as_of = _os.environ.get("FEVER_BT_STRICT_AS_OF", "").strip() in ("1", "true", "yes")
    if strict_as_of:
        args = dict(args or {})
        if name == "event_study_skill":
            args["as_of"] = True
            # 若调用方未显式传 benchmark 则留空（skill 内部会按 symbol 自动绑定 QQQ/XLK/SPY/sh000300）
        elif name == "event_study":
            args["as_of"] = True
            # event_study 是 internal skill，也强制 as_of（即使 skill.py 的 wrapper 没拦住）
    ensure_skills_loaded()
    sd = REGISTRY.get(name)
    if sd is None:
        print(f"SKILL name={name} ok=false err=unknown dur={time.time() - _t0:.2f}s", flush=True)
        publish(f"SKILL name={name} ok=false err=unknown dur={time.time() - _t0:.2f}s")
        return {"ok": False, "error": f"未知技能: {name}"}
    depth = _skill_depth.get()
    timeout = _skill_timeout(name, sd.category, depth)
    depth_token = _skill_depth.set(depth + 1)
    try:
        if asyncio.iscoroutinefunction(sd.handler):
            result = await asyncio.wait_for(
                sd.handler(**args), timeout=timeout
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(sd.handler, **args), timeout=timeout
            )
        _dur = time.time() - _t0
        _tag = " [VSLOW]" if _dur > 10 else (" [SLOW]" if _dur > 3 else "")
        print(f"SKILL name={name} ok={bool(result.get('ok'))} dur={_dur:.2f}s{_tag}", flush=True)
        publish(f"SKILL name={name} ok={bool(result.get('ok'))} dur={_dur:.2f}s{_tag}")
        return result
    except asyncio.TimeoutError:
        _dur = time.time() - _t0
        scope = "顶层" if depth == 0 else "子技能"
        print(f"SKILL name={name} ok=false err=timeout dur={_dur:.2f}s [VSLOW]", flush=True)
        publish(f"SKILL name={name} ok=false err=timeout dur={_dur:.2f}s [VSLOW]")
        return {"ok": False, "error": f"技能 {name} 执行超时（>{timeout:g}s，{scope}）"}
    except TypeError as e:
        _dur = time.time() - _t0
        print(f"SKILL name={name} ok=false err=type_error dur={_dur:.2f}s", flush=True)
        publish(f"SKILL name={name} ok=false err=type_error dur={_dur:.2f}s")
        return {"ok": False, "error": f"技能参数错误: {e}"}
    except Exception as e:  # noqa: BLE001
        _dur = time.time() - _t0
        print(f"SKILL name={name} ok=false err={type(e).__name__} dur={_dur:.2f}s", flush=True)
        publish(f"SKILL name={name} ok=false err={type(e).__name__} dur={_dur:.2f}s")
        return {"ok": False, "error": f"技能执行失败: {type(e).__name__}: {e}"}
    finally:
        _skill_depth.reset(depth_token)


def _preview(result: dict) -> str:
    if not result.get("ok"):
        return f"失败: {result.get('error', '未知错误')}"
    meta = result.get("meta") or {}
    rows = meta.get("rows")
    src = meta.get("source", "")
    if isinstance(result.get("data"), list):
        rows = rows if rows is not None else len(result["data"])
    parts = []
    if rows is not None:
        parts.append(f"返回 {rows} 行")
    if src:
        parts.append(f"来源 {src}")
    if result.get("note"):
        parts.append(str(result["note"]))
    if result.get("truncated"):
        parts.append("已截断")
    return ", ".join(parts) or "成功"


async def run_agent(
    agent_id: str,
    messages: list[dict],
    *,
    agent_def: dict,
    state: dict,
    artifact_store: ArtifactStore = noop_artifact_store,
    skill_executor: SkillExecutor = execute_skill,
    max_rounds: int = 8,
    emit_thinking: bool = True,
) -> AsyncIterator[dict]:
    """Streaming tool-call loop. Yields SSE event dicts (each with 'agent' field).

    state (mutated): {"content": str, "tool_trace": [..], "rounds": int}
    """
    ensure_skills_loaded()
    # 三层模型：自动过滤 internal=True 的 atomic tool（LLM 不可见）
    # skill 走 LLM 可见
    configured_skills = agent_def.get("skills")
    tools = tools_for_agent(
        configured_skills if configured_skills is not None else agent_id
    )
    client = get_client()
    consecutive_failures: dict[str, int] = {}

    for round_no in range(1, max_rounds + 1):
        state["rounds"] = round_no
        kwargs: dict[str, Any] = {
            "model": config.LLM_MODEL,
            "messages": messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        if config.AGENT_MAX_TOKENS > 0:
            kwargs["max_tokens"] = config.AGENT_MAX_TOKENS
        _llm_t0 = time.time()
        tc_acc: dict[int, dict] = {}
        saw_content = False
        round_content = ""
        finish_reason = None
        for transport_attempt in range(2):
            tc_acc = {}
            round_content = ""
            saw_content = False
            finish_reason = None
            try:
                stream = await _create_with_hard_timeout(client.chat.completions.create(**kwargs))
                async for chunk in _stream_with_hard_timeout(stream):
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    rc = getattr(delta, "reasoning_content", None)
                    if rc and emit_thinking:
                        yield {"type": "thinking", "agent": agent_id, "delta": rc}
                    if delta.content:
                        saw_content = True
                        round_content += delta.content
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            slot = tc_acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    slot["name"] += tc.function.name
                                if tc.function.arguments:
                                    slot["arguments"] += tc.function.arguments
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                break
            except BaseException as error:  # noqa: BLE001
                if (
                    transport_attempt >= 1
                    or not _is_retryable_stream_error(error)
                ):
                    raise
                await asyncio.sleep(0.5)

        tool_calls = [tc_acc[i] for i in sorted(tc_acc)]
        _llm_dur = time.time() - _llm_t0
        _llm_tag = " [VSLOW]" if _llm_dur > 15 else (" [SLOW]" if _llm_dur > 5 else "")
        _tc_names = ",".join(t["name"] for t in tool_calls) if tool_calls else "-"
        print(
            f"LLM agent={agent_id} round={round_no} finish={finish_reason} "
            f"tool_calls={len(tool_calls)}({_tc_names}) content={saw_content} "
            f"dur={_llm_dur:.2f}s{_llm_tag}",
            flush=True,
        )
        publish(
            f"LLM agent={agent_id} round={round_no} finish={finish_reason} "
            f"tool_calls={len(tool_calls)}({_tc_names}) content={saw_content} "
            f"dur={_llm_dur:.2f}s{_llm_tag}"
        )
        if not tool_calls:
            # 最终答复轮（token 已流式发出）
            if round_content:
                state["content"] += round_content
                yield {"type": "token", "agent": agent_id, "delta": round_content}
            break

        # 有 tool_calls：执行技能并继续循环
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": t["id"] or f"call_{round_no}_{i}", "type": "function",
                 "function": {"name": t["name"], "arguments": t["arguments"] or "{}"}}
                for i, t in enumerate(tool_calls)
            ],
        })
        for i, t in enumerate(tool_calls):
            tc_id = t["id"] or f"call_{round_no}_{i}"
            name = t["name"]
            try:
                args = json.loads(t["arguments"]) if t["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool_call", "agent": agent_id, "id": tc_id,
                   "skill": name, "args": args}
            result = await skill_executor(name, args)

            artifact_ids: list[str] = []
            reused = bool(result.get("_team_shared"))
            # A reused result remains available to the model, but its artifact
            # has already been persisted by the first team member that fetched it.
            if result.get("ok") and not reused:
                arts = result.get("artifacts") or ([result["artifact"]] if result.get("artifact") else [])
                for art in arts:
                    try:
                        row = await artifact_store(art.get("kind", "table"),
                                                   art.get("title", name),
                                                   art.get("payload", {}))
                        artifact_ids.append(row.get("id"))
                        if not row.get("_reused"):
                            yield {"type": "artifact", "agent": agent_id, "artifact": row}
                    except Exception as e:  # noqa: BLE001
                        yield {"type": "thinking", "agent": agent_id,
                               "delta": f"\n[artifact 落库失败: {e}]\n"}

            preview = ("复用团队数据；" if reused else "") + _preview(result)
            serialized_result = serialize_tool_result(result, config.TOOL_RESULT_MAX_CHARS)
            # preview 只包含“返回 N 行”，不足以让后续 Deep Researcher 构图。
            # 保存一个有界、已通过 strict-as-of skill 清洗的结果片段，供团队证据预灌；
            # 避免保存 evidence_graph 自身结果造成图谱递归膨胀。
            result_excerpt = ""
            if result.get("ok") and name not in {"evidence_graph", "evidence_ledger"}:
                result_excerpt = serialized_result[:2000]
            yield {"type": "tool_result", "agent": agent_id, "id": tc_id,
                   "skill": name, "ok": bool(result.get("ok")), "preview": preview,
                   "artifact_id": artifact_ids[0] if artifact_ids else None,
                   "reused": reused}
            state["tool_trace"].append({
                "type": "tool", "agent": agent_id, "id": tc_id, "skill": name,
                "args": args, "ok": bool(result.get("ok")), "preview": preview,
                "result_excerpt": result_excerpt,
                "artifact_ids": artifact_ids,
                "reused": reused,
            })
            if result.get("ok"):
                consecutive_failures[name] = 0
            else:
                consecutive_failures[name] = consecutive_failures.get(name, 0) + 1
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": serialized_result,
            })
        repeated_failure_skill = next((skill for skill, count in consecutive_failures.items() if count >= 3), None)
        if repeated_failure_skill:
            summary_kwargs: dict[str, Any] = {
                "model": config.LLM_MODEL,
                "messages": messages + [{
                    "role": "user",
                    "content": (
                        f"技能 {repeated_failure_skill} 已连续失败 {consecutive_failures[repeated_failure_skill]} 次。"
                        "请停止重复调用失败技能，明确说明当前缺失的数据与限制，只基于已获得的信息给出最优回答。"
                    ),
                }],
                "stream": True,
            }
            if config.AGENT_MAX_TOKENS > 0:
                summary_kwargs["max_tokens"] = config.AGENT_MAX_TOKENS
            summary_stream = await _create_with_hard_timeout(
                client.chat.completions.create(**summary_kwargs)
            )
            async for chunk in _stream_with_hard_timeout(summary_stream):
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                rc = getattr(delta, "reasoning_content", None)
                if rc and emit_thinking:
                    yield {"type": "thinking", "agent": agent_id, "delta": rc}
                if delta.content:
                    state["content"] += delta.content
                    yield {"type": "token", "agent": agent_id, "delta": delta.content}
            break
        # 继续下一轮
    else:
        # 达到最大轮数仍有 tool_calls —— 让模型做一次无工具总结
        state["truncated_by_rounds"] = True
        summary_kwargs: dict[str, Any] = {
            "model": config.LLM_MODEL,
            "messages": messages + [{"role": "user", "content": "工具轮次已用完，请基于已获得的信息直接给出最终回答。"}],
            "stream": True,
        }
        if config.AGENT_MAX_TOKENS > 0:
            summary_kwargs["max_tokens"] = config.AGENT_MAX_TOKENS
        stream = await _create_with_hard_timeout(client.chat.completions.create(**summary_kwargs))
        async for chunk in _stream_with_hard_timeout(stream):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            rc = getattr(delta, "reasoning_content", None)
            if rc and emit_thinking:
                yield {"type": "thinking", "agent": agent_id, "delta": rc}
            if delta.content:
                state["content"] += delta.content
                yield {"type": "token", "agent": agent_id, "delta": delta.content}


# ------------------------------------------------------- one-shot helpers ---


async def complete_text(system: str, user: str, *, max_tokens: int = 2000) -> str:
    """Non-streaming single completion (returns content only)."""
    client = get_client()
    resp = await _create_with_hard_timeout(client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        max_tokens=max_tokens,
    ))
    msg = resp.choices[0].message
    return (msg.content or "").strip()


async def complete_json(system: str, user: str, *, max_tokens: int = 2000) -> Optional[dict]:
    """Non-streaming completion forced to JSON object; returns parsed dict or None.
       MAAS/Ark 有 1 RPS + 首包慢 + 429；内部自动最多 3 次指数退避重试。

       优化（2026-08-19）：
       - max_tokens 3000 → 2000（路由判断 / labeller 均不需要 3k tokens，减生成时间）
       - 重试 4 → 3 次（最坏总等待 9s→5s，避免单请求阻塞太久）
       - sleep 上限 9s → 5s（同上）
       - 日志加 [SLOW|VSLOW] 标签（>5s / >15s）

       日志：每次尝试打印 LLM_JSON 行，含 attempt / ok / dur / [SLOW|VSLOW|RETRY|GIVEUP]。
    """
    client = get_client()
    import asyncio as _ai
    last_err: Optional[BaseException] = None
    _MAX_ATTEMPTS = 3
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        _t0 = time.time()
        try:
            resp = await _create_with_hard_timeout(client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            ))
            _dur = time.time() - _t0
            _tag = " [VSLOW]" if _dur > 15 else (" [SLOW]" if _dur > 5 else "")
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                print(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=false err=empty dur={_dur:.2f}s{_tag}", flush=True)
                publish(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=false err=empty dur={_dur:.2f}s{_tag}")
                return None
            try:
                result = json.loads(text)
                print(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=true dur={_dur:.2f}s{_tag}", flush=True)
                publish(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=true dur={_dur:.2f}s{_tag}")
                return result
            except json.JSONDecodeError:
                print(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=false err=json_decode dur={_dur:.2f}s{_tag}", flush=True)
                publish(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=false err=json_decode dur={_dur:.2f}s{_tag}")
                m = text[text.find("{"): text.rfind("}") + 1]
                try:
                    result = json.loads(m)
                    print(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=true(recovered) dur={_dur:.2f}s{_tag}", flush=True)
                    publish(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=true(recovered) dur={_dur:.2f}s{_tag}")
                    return result
                except Exception:  # noqa: BLE001
                    return None
        except BaseException as e:  # noqa: BLE001
            # 前端断开 / 主动取消 → 立刻传播，不当普通错误重试
            # （否则前端断了后端还在跑 LLM，白白浪费配额 + 阻塞 worker）
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                _dur = time.time() - _t0
                print(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} cancelled dur={_dur:.2f}s [CANCELLED]", flush=True)
                publish(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} cancelled dur={_dur:.2f}s [CANCELLED]")
                raise
            _dur = time.time() - _t0
            last_err = e
            if attempt >= _MAX_ATTEMPTS:
                print(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=false err={type(e).__name__} dur={_dur:.2f}s [GIVEUP]", flush=True)
                publish(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} ok=false err={type(e).__name__} dur={_dur:.2f}s [GIVEUP]")
                break
            # 429 / 超时 / 远端连接失败 → 指数退避，更长等待
            msg = str(e)
            sleep_s = min(5.0, (2.0 ** (attempt - 1)) * 1.0 + 0.3)
            if "429" in msg or "TooManyRequests" in msg or "RateLimit" in msg or "rate limit" in msg:
                sleep_s += 0.5
            print(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} err={type(e).__name__} dur={_dur:.2f}s sleep={sleep_s:.1f}s [RETRY]", flush=True)
            publish(f"LLM_JSON attempt={attempt}/{_MAX_ATTEMPTS} err={type(e).__name__} dur={_dur:.2f}s sleep={sleep_s:.1f}s [RETRY]")
            await _ai.sleep(sleep_s)
    if last_err is not None:
        raise last_err
    return None


def _response_output_text(resp: Any) -> str:
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    try:
        dump = resp.model_dump()
    except Exception:
        dump = {}
    output = dump.get("output") or []
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            for key in ("text", "output_text"):
                val = content.get(key)
                if isinstance(val, str) and val.strip():
                    chunks.append(val.strip())
    return "\n".join(chunks).strip()


async def complete_json_with_web_search(
    system: str,
    user: str,
    *,
    max_keywords: int = 3,
    max_output_tokens: int = 2500,
) -> Optional[dict]:
    """Responses API + Ark web_search, returning parsed JSON or None."""
    client = get_client()
    resp = await client.responses.create(
        model=config.LLM_MODEL,
        instructions=system,
        input=user,
        tools=[{"type": "web_search", "max_keyword": max(1, min(int(max_keywords or 3), 10))}],
        max_output_tokens=max_output_tokens,
    )
    text = _response_output_text(resp)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = text[text.find("{"): text.rfind("}") + 1]
        if not m:
            return None
        try:
            return json.loads(m)
        except Exception:  # noqa: BLE001
            return None
