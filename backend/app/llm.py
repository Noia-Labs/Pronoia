"""Ark LLM client + streaming tool-call loop (design.md §2/§6.2).

实现约定：所有轮次都用 stream=True，累积 tool_calls deltas；本轮有 tool_calls
则执行技能并继续循环，无则为最终答复（token 已流式发出）。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from openai import AsyncOpenAI
import httpx

from . import config
from .model_lab.defaults import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_STRUCTURED_OUTPUT_TOKENS
from .log_bus import publish
from .skills.registry import REGISTRY, ensure_skills_loaded, serialize_tool_result, tool_schema_subset, tools_for_agent

_client: Optional[AsyncOpenAI] = None
_skill_depth: ContextVar[int] = ContextVar("skill_execution_depth", default=0)
_SLOW_NESTED_SKILLS = frozenset({"event_study"})
_profile_clients: dict[tuple[str, str, str, float, str, int, int], AsyncOpenAI] = {}


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
    return await asyncio.wait_for(
        awaitable, timeout=resolve_runtime_target().timeout_seconds
    )


async def _stream_with_hard_timeout(stream):
    """Cap a complete streaming round instead of only individual socket reads."""
    timeout_seconds = resolve_runtime_target().timeout_seconds
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    iterator = stream.__aiter__()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                f"LLM streaming round exceeded {timeout_seconds:.0f}s"
            )
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        yield chunk
@dataclass(frozen=True)
class LLMRuntimeTarget:
    """Resolved transport for one logical request/run.

    ``api_key`` exists only in process memory. Model Lab persists an opaque
    local credential or environment reference and resolves it per request.
    """

    base_url: str
    api_key: str = field(repr=False)
    model_id: str
    timeout_seconds: float
    profile_id: Optional[str] = None
    secret_env_ref: Optional[str] = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


_runtime_override: ContextVar[Optional[LLMRuntimeTarget]] = ContextVar(
    "pronoia_llm_runtime_override", default=None
)


def _target_from_profile(profile: dict[str, Any]) -> LLMRuntimeTarget:
    from .model_endpoint_security import validate_runtime_model_endpoint
    from .model_lab.providers import chat_completions_url, resolve_profile_secret

    base_url = str(profile.get("base_url") or "").strip().rstrip("/")
    # Re-check DNS at the beginning of every new logical request.  The OpenAI
    # client is configured not to retry at this layer; higher-level bounded
    # retry policy remains in this module.
    validate_runtime_model_endpoint(
        chat_completions_url(base_url), label="默认模型 API"
    )
    # AsyncOpenAI expects the API root and appends /chat/completions itself;
    # profiles may also be used by the raw urllib adapter, which accepts either
    # representation.
    suffix = "/chat/completions"
    client_base_url = base_url[:-len(suffix)].rstrip("/") if base_url.endswith(suffix) else base_url
    return LLMRuntimeTarget(
        base_url=client_base_url,
        api_key=resolve_profile_secret(profile),
        model_id=str(profile.get("model_id") or "").strip(),
        timeout_seconds=float(profile.get("timeout_seconds") or config.LLM_TIMEOUT),
        profile_id=str(profile.get("id") or "") or None,
        secret_env_ref=str(profile.get("secret_env_ref") or "") or None,
        max_output_tokens=int(profile.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS),
    )


def resolve_runtime_target() -> LLMRuntimeTarget:
    """Resolve the immutable override, then the current default, then legacy env.

    Ordinary new chat/backtest calls dynamically observe a newly-selected
    default profile.  Model Lab workers enter ``model_profile_context`` with a
    frozen snapshot, so a later default/profile edit cannot alter an existing
    batch.
    """
    overridden = _runtime_override.get()
    if overridden is not None:
        return overridden
    # Lazy import avoids an import cycle while db.py initializes the schema.
    from .model_lab import repository as model_repo

    selected = model_repo.get_default_profile(public=False)
    if selected is not None:
        return _target_from_profile(selected)
    return LLMRuntimeTarget(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        model_id=config.LLM_MODEL,
        timeout_seconds=config.LLM_TIMEOUT,
        profile_id=None,
        secret_env_ref=None,
    )


def get_model_name() -> str:
    return resolve_runtime_target().model_id


def output_token_limit(requested: int | None = None) -> int:
    """Respect the frozen connection limit for every nested Agent stage."""
    target = resolve_runtime_target()
    return max(32, min(
        int(requested or DEFAULT_MAX_OUTPUT_TOKENS),
        int(target.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS),
    ))


def redact_runtime_error(text: Any) -> str:
    """Scrub diagnostics with the already-pinned key without another lookup."""
    from .model_endpoint_security import redact_sensitive_text

    target = _runtime_override.get()
    return redact_sensitive_text(text, target.api_key if target else config.LLM_API_KEY)


@contextmanager
def model_profile_context(profile_snapshot: dict[str, Any]):
    """Pin one profile snapshot for all nested Agent/backtest model calls."""
    token = _runtime_override.set(_target_from_profile(dict(profile_snapshot)))
    try:
        yield
    finally:
        _runtime_override.reset(token)


@contextmanager
def runtime_target_context(target: LLMRuntimeTarget):
    """Pin one already-resolved target across an entire logical chat request."""

    token = _runtime_override.set(target)
    try:
        yield
    finally:
        _runtime_override.reset(token)


def get_client() -> AsyncOpenAI:
    """Return the client for an explicit snapshot/current default/legacy env."""
    global _client
    target = resolve_runtime_target()
    if target.profile_id is None:
        if _client is None:
            http_client = None
            if config.LLM_FORCE_IPV4:
                http_client = httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(
                        local_address="0.0.0.0", retries=2
                    ),
                    timeout=target.timeout_seconds,
                )
            client_kwargs: dict[str, Any] = dict(
                base_url=target.base_url,
                api_key=target.api_key,
                timeout=target.timeout_seconds,
            )
            if http_client is not None:
                client_kwargs["http_client"] = http_client
            _client = AsyncOpenAI(**client_kwargs)
        return _client
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    cache_key = (
        target.profile_id or "",
        target.base_url,
        target.model_id,
        target.timeout_seconds,
        target.secret_env_ref or "",
        threading.get_ident(),
        loop_id,
    )
    client = _profile_clients.get(cache_key)
    if client is None:
        http_client = None
        if config.LLM_FORCE_IPV4:
            http_client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(
                    local_address="0.0.0.0", retries=2
                ),
                timeout=target.timeout_seconds,
            )
        client_kwargs = dict(
            base_url=target.base_url,
            api_key=target.api_key,
            timeout=target.timeout_seconds,
            max_retries=0,
        )
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        client = AsyncOpenAI(**client_kwargs)
        _profile_clients[cache_key] = client
    return client


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
            "model": get_model_name(),
            "messages": messages,
            "stream": True,
            "max_tokens": output_token_limit(),
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
                "model": get_model_name(),
                "messages": messages + [{
                    "role": "user",
                    "content": (
                        f"技能 {repeated_failure_skill} 已连续失败 {consecutive_failures[repeated_failure_skill]} 次。"
                        "请停止重复调用失败技能，明确说明当前缺失的数据与限制，只基于已获得的信息给出最优回答。"
                    ),
                }],
                "stream": True,
                "max_tokens": output_token_limit(),
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
            "model": get_model_name(),
            "messages": messages + [{"role": "user", "content": "工具轮次已用完，请基于已获得的信息直接给出最终回答。"}],
            "stream": True,
            "max_tokens": output_token_limit(),
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


# Keep the transport-level JSON instruction separate from every business
# prompt.  Some OpenAI-compatible endpoints reject ``json_object`` requests
# unless the messages explicitly contain the English word "json".  A
# dedicated system message satisfies that protocol requirement without
# rewriting (or logging) the caller's system/user content.
_JSON_OBJECT_INSTRUCTION = (
    "Return exactly one valid json object. Do not include Markdown, code "
    "fences, or any text outside the json object."
)

# Reasoning-capable OpenAI-compatible models may spend the entire completion
# budget in ``reasoning_content`` and return an empty ``content`` with
# ``finish_reason=length``.  Only the already-bounded output retry may receive
# a larger budget, and it may never grow past this cap.
_JSON_OUTPUT_RETRY_MAX_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS


def _json_output_retry_budget(current: int) -> int:
    """Return one bounded, monotonic budget increase for a length retry."""
    try:
        budget = int(current)
    except (TypeError, ValueError, OverflowError):
        return current
    if budget <= 0 or budget >= _JSON_OUTPUT_RETRY_MAX_TOKENS:
        return current
    return min(_JSON_OUTPUT_RETRY_MAX_TOKENS, max(budget + 1, budget * 2))


def _token_budget_audit(value: Any) -> int:
    """Normalize a token budget for prompt-safe diagnostic persistence."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _json_completion_messages(system: str, user: str) -> list[dict[str, str]]:
    """Build JSON-mode messages while preserving both business prompts."""
    return [
        {"role": "system", "content": system},
        {"role": "system", "content": _JSON_OBJECT_INSTRUCTION},
        {"role": "user", "content": user},
    ]


def _coerce_http_status_code(value: Any) -> Optional[int]:
    """Return a plausible HTTP status without depending on an SDK type."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        code = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return code if 100 <= code <= 599 else None


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Collect a bounded, cycle-safe cause/context chain."""
    chain: list[BaseException] = []
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending and len(chain) < 8:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for linked in (getattr(current, "__cause__", None),
                       getattr(current, "__context__", None)):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return chain


def _http_status_code(exc: BaseException) -> Optional[int]:
    """Extract an HTTP code from common exception/response shapes or text."""
    chain = _exception_chain(exc)
    for current in chain:
        candidates: list[Any] = [current]
        try:
            response = getattr(current, "response", None)
        except Exception:  # pragma: no cover - defensive property access
            response = None
        if response is not None:
            candidates.append(response)
        for candidate in candidates:
            if isinstance(candidate, dict):
                values = (candidate.get("status_code"), candidate.get("status"))
            else:
                values = []
                for attr in ("status_code", "status"):
                    try:
                        values.append(getattr(candidate, attr, None))
                    except Exception:  # pragma: no cover - defensive property access
                        values.append(None)
            for value in values:
                code = _coerce_http_status_code(value)
                if code is not None:
                    return code

    # Lightweight/local compatibility servers sometimes expose only a message.
    status_pattern = re.compile(
        r"\b(?:http(?:\s+status)?|status(?:_code|\s+code)?|error\s+code)"
        r"\s*[:=]?\s*([45]\d{2})\b",
        re.IGNORECASE,
    )
    for current in chain:
        match = status_pattern.search(str(current))
        if match:
            return int(match.group(1))
    return None


def _unsupported_json_response_format(exc: BaseException, status_code: Optional[int]) -> bool:
    """Detect only an explicit 400 rejection of JSON response-format support."""
    if status_code != 400:
        return False
    text = " ".join(str(item) for item in _exception_chain(exc)).lower()
    mentions_format = any(token in text for token in (
        "response_format", "response format", "json_object", "json object mode",
    ))
    explicitly_unsupported = any(token in text for token in (
        "unsupported", "not supported", "does not support", "unknown parameter",
        "unknown field", "unrecognized parameter", "unrecognized field",
    ))
    return mentions_format and explicitly_unsupported


def _is_deterministic_client_error(status_code: Optional[int]) -> bool:
    """Mirror conventional retry semantics: only transient 4xx remain retryable."""
    return (
        status_code is not None
        and 400 <= status_code < 500
        and status_code not in {408, 409, 425, 429}
    )


@dataclass(frozen=True)
class JSONCompletionResult:
    """JSON completion plus a prompt-safe, request-local audit record.

    The object deliberately contains neither prompts nor raw model output.  It
    is therefore safe to persist on a prediction even when the model returned
    malformed content.  Keeping diagnostics in the return value (instead of a
    module global) also makes concurrent event runs deterministic and
    race-free.
    """

    value: Optional[dict[str, Any]]
    failure_kind: Optional[str] = None
    attempts: int = 0
    finish_reason: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_length: int = 0
    output_length: int = 0
    recovered_json: bool = False
    response_format_fallback: bool = False
    max_tokens_initial: int = 0
    max_tokens_peak: int = 0
    output_budget_escalated: bool = False
    elapsed_ms: int = 0

    def audit_metadata(self) -> dict[str, Any]:
        """Return the bounded subset intended for prediction persistence."""
        return {
            "output_failure_kind": self.failure_kind,
            "output_attempts": max(0, int(self.attempts)),
            "finish_reason": self.finish_reason,
            "usage": dict(self.usage),
            "reasoning_length": max(0, int(self.reasoning_length)),
            "output_length": max(0, int(self.output_length)),
            "recovered_json": bool(self.recovered_json),
            "response_format_fallback": bool(self.response_format_fallback),
            "max_tokens_initial": max(0, int(self.max_tokens_initial)),
            "max_tokens_peak": max(0, int(self.max_tokens_peak)),
            "output_budget_escalated": bool(self.output_budget_escalated),
            "elapsed_ms": max(0, int(self.elapsed_ms)),
        }


def _response_usage(resp: Any) -> dict[str, int]:
    """Normalize token usage without retaining provider-specific payloads."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        raw = usage
    else:
        try:
            raw = usage.model_dump()
        except Exception:
            raw = {
                key: getattr(usage, key, None)
                for key in (
                    "prompt_tokens", "completion_tokens", "total_tokens",
                    "input_tokens", "output_tokens",
                )
            }
    if not isinstance(raw, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    result: dict[str, int] = {}
    for canonical, keys in aliases.items():
        value: Any = None
        for key in keys:
            if raw.get(key) is not None:
                value = raw.get(key)
                break
        try:
            parsed = max(0, int(value)) if value is not None else 0
        except (TypeError, ValueError, OverflowError):
            parsed = 0
        if parsed:
            result[canonical] = parsed
    if "total_tokens" not in result and (
        "input_tokens" in result or "output_tokens" in result
    ):
        result["total_tokens"] = (
            result.get("input_tokens", 0) + result.get("output_tokens", 0)
        )
    return result


def _merge_usage(total: dict[str, int], current: dict[str, int]) -> None:
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if key in current:
            total[key] = total.get(key, 0) + max(0, int(current[key]))


def _parse_json_object(text: str) -> tuple[Optional[dict[str, Any]], bool]:
    """Parse an object, allowing one bounded extraction from prose/fences."""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, False
    except json.JSONDecodeError:
        pass
    first, last = text.find("{"), text.rfind("}")
    if first < 0 or last <= first:
        return None, False
    try:
        parsed = json.loads(text[first:last + 1])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, False
    return (parsed, True) if isinstance(parsed, dict) else (None, False)


async def complete_text(system: str, user: str, *, max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS) -> str:
    """Non-streaming single completion (returns content only)."""
    client = get_client()
    resp = await _create_with_hard_timeout(client.chat.completions.create(
        model=get_model_name(),
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        max_tokens=output_token_limit(max_tokens),
    ))
    msg = resp.choices[0].message
    return (msg.content or "").strip()


async def complete_json_diagnostic(
    system: str,
    user: str,
    *,
    max_tokens: int = DEFAULT_STRUCTURED_OUTPUT_TOKENS,
    request_gate: Optional[Callable[[], Awaitable[None]]] = None,
) -> JSONCompletionResult:
    """Complete one JSON object and return bounded, concurrency-safe diagnostics.

    A successful HTTP response with empty or malformed content receives one
    additional output attempt.  This retry budget is independent from the
    existing transient-error budget (429/5xx/timeout: at most three attempts),
    so neither path can loop indefinitely.

       MAAS/Ark 有 1 RPS + 首包慢 + 429；内部自动最多 3 次指数退避重试。

       优化（2026-08-19）：
       - 输出预算按任务分配，仍以冻结的模型连接上限为界；思考与最终 JSON 共享额度。
       - 重试 4 → 3 次（最坏总等待 9s→5s，避免单请求阻塞太久）
       - sleep 上限 9s → 5s（同上）
       - 日志加 [SLOW|VSLOW] 标签（>5s / >15s）

       日志：每次尝试打印 LLM_JSON 行，含 attempt / ok / dur / [SLOW|VSLOW|RETRY|GIVEUP]。
    """
    client = get_client()
    import asyncio as _ai
    last_err: Optional[BaseException] = None
    _MAX_TRANSIENT_ATTEMPTS = 3
    _MAX_OUTPUT_ATTEMPTS = 2
    transient_attempts = 0
    output_attempts = 0
    request_no = 0
    use_response_format = True
    format_fallback_used = False
    request_max_tokens = output_token_limit(max_tokens)
    max_tokens_initial = _token_budget_audit(request_max_tokens)
    max_tokens_peak = max_tokens_initial
    output_budget_escalated = False
    usage_total: dict[str, int] = {}
    reasoning_length_total = 0
    call_started = time.monotonic()
    last_finish_reason: Optional[str] = None
    last_output_length = 0
    last_failure_kind: Optional[str] = None
    last_recovered = False
    while transient_attempts < _MAX_TRANSIENT_ATTEMPTS:
        request_no += 1
        _t0 = time.time()
        try:
            request_kwargs: dict[str, Any] = {
                "model": get_model_name(),
                "messages": _json_completion_messages(system, user),
                "max_tokens": request_max_tokens,
            }
            if use_response_format:
                request_kwargs["response_format"] = {"type": "json_object"}
            # Optional per-run scheduler used by bulk event backtests.  It is
            # invoked for every actual transport attempt, including format,
            # malformed-output, and transient-error retries.
            if request_gate is not None:
                await request_gate()
            resp = await _create_with_hard_timeout(
                client.chat.completions.create(**request_kwargs)
            )
            _dur = time.time() - _t0
            _tag = " [VSLOW]" if _dur > 15 else (" [SLOW]" if _dur > 5 else "")
            choices = getattr(resp, "choices", None) or []
            choice = choices[0] if choices else None
            msg = getattr(choice, "message", None) if choice is not None else None
            raw_finish = getattr(choice, "finish_reason", None) if choice is not None else None
            last_finish_reason = str(raw_finish) if raw_finish is not None else None
            _merge_usage(usage_total, _response_usage(resp))
            reasoning = getattr(msg, "reasoning_content", None) if msg is not None else None
            if isinstance(reasoning, str):
                reasoning_length_total += len(reasoning)
            raw_content = getattr(msg, "content", None) if msg is not None else None
            text = raw_content.strip() if isinstance(raw_content, str) else ""
            last_output_length = len(text)
            output_attempts += 1
            if not text:
                last_failure_kind = "empty_response"
                print(f"LLM_JSON attempt={request_no} ok=false err=empty dur={_dur:.2f}s{_tag}", flush=True)
                publish(f"LLM_JSON attempt={request_no} ok=false err=empty dur={_dur:.2f}s{_tag}")
            else:
                result, recovered = _parse_json_object(text)
                if result is not None:
                    last_recovered = recovered
                    suffix = "(recovered)" if recovered else ""
                    print(f"LLM_JSON attempt={request_no} ok=true{suffix} dur={_dur:.2f}s{_tag}", flush=True)
                    publish(f"LLM_JSON attempt={request_no} ok=true{suffix} dur={_dur:.2f}s{_tag}")
                    return JSONCompletionResult(
                        value=result,
                        attempts=request_no,
                        finish_reason=last_finish_reason,
                        usage=usage_total,
                        reasoning_length=reasoning_length_total,
                        output_length=last_output_length,
                        recovered_json=recovered,
                        response_format_fallback=format_fallback_used,
                        max_tokens_initial=max_tokens_initial,
                        max_tokens_peak=max_tokens_peak,
                        output_budget_escalated=output_budget_escalated,
                        elapsed_ms=max(0, int((time.monotonic() - call_started) * 1000)),
                    )
                last_failure_kind = "json_decode_error"
                print(f"LLM_JSON attempt={request_no} ok=false err=json_decode dur={_dur:.2f}s{_tag}", flush=True)
                publish(f"LLM_JSON attempt={request_no} ok=false err=json_decode dur={_dur:.2f}s{_tag}")

            if output_attempts < _MAX_OUTPUT_ATTEMPTS:
                previous_budget = request_max_tokens
                if last_finish_reason == "length":
                    request_max_tokens = output_token_limit(_json_output_retry_budget(request_max_tokens))
                if _token_budget_audit(request_max_tokens) > _token_budget_audit(previous_budget):
                    output_budget_escalated = True
                    max_tokens_peak = max(
                        max_tokens_peak,
                        _token_budget_audit(request_max_tokens),
                    )
                print(
                    f"LLM_JSON attempt={request_no} output_retry=1 "
                    f"reason={last_failure_kind} finish={last_finish_reason} "
                    f"next_max_tokens={_token_budget_audit(request_max_tokens)} "
                    f"budget_escalated={str(output_budget_escalated).lower()} [RETRY]",
                    flush=True,
                )
                publish(
                    f"LLM_JSON attempt={request_no} output_retry=1 "
                    f"reason={last_failure_kind} finish={last_finish_reason} "
                    f"next_max_tokens={_token_budget_audit(request_max_tokens)} "
                    f"budget_escalated={str(output_budget_escalated).lower()} [RETRY]"
                )
                continue
            return JSONCompletionResult(
                value=None,
                failure_kind=last_failure_kind,
                attempts=request_no,
                finish_reason=last_finish_reason,
                usage=usage_total,
                reasoning_length=reasoning_length_total,
                output_length=last_output_length,
                recovered_json=last_recovered,
                response_format_fallback=format_fallback_used,
                max_tokens_initial=max_tokens_initial,
                max_tokens_peak=max_tokens_peak,
                output_budget_escalated=output_budget_escalated,
                elapsed_ms=max(0, int((time.monotonic() - call_started) * 1000)),
            )
        except BaseException as e:  # noqa: BLE001
            # 前端断开 / 主动取消 → 立刻传播，不当普通错误重试
            # （否则前端断了后端还在跑 LLM，白白浪费配额 + 阻塞 worker）
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                _dur = time.time() - _t0
                print(f"LLM_JSON attempt={request_no} cancelled dur={_dur:.2f}s [CANCELLED]", flush=True)
                publish(f"LLM_JSON attempt={request_no} cancelled dur={_dur:.2f}s [CANCELLED]")
                raise
            _dur = time.time() - _t0
            status_code = _http_status_code(e)

            # A few local/OpenAI-compatible servers do not implement
            # response_format.  Only an explicit unsupported-format 400 earns
            # one plain request; the independent JSON instruction remains in
            # messages and parsing stays local.  Any other 400 fails fast.
            if (
                use_response_format
                and not format_fallback_used
                and _unsupported_json_response_format(e, status_code)
            ):
                format_fallback_used = True
                use_response_format = False
                print(
                    f"LLM_JSON attempt={request_no} ok=false status=400 "
                    f"err={type(e).__name__} dur={_dur:.2f}s [FORMAT_FALLBACK]",
                    flush=True,
                )
                publish(
                    f"LLM_JSON attempt={request_no} ok=false status=400 "
                    f"err={type(e).__name__} dur={_dur:.2f}s [FORMAT_FALLBACK]"
                )
                continue

            if _is_deterministic_client_error(status_code):
                print(
                    f"LLM_JSON attempt={request_no} ok=false status={status_code} "
                    f"err={type(e).__name__} dur={_dur:.2f}s [GIVEUP]",
                    flush=True,
                )
                publish(
                    f"LLM_JSON attempt={request_no} ok=false status={status_code} "
                    f"err={type(e).__name__} dur={_dur:.2f}s [GIVEUP]"
                )
                raise

            last_err = e
            transient_attempts += 1
            if transient_attempts >= _MAX_TRANSIENT_ATTEMPTS:
                status_part = f" status={status_code}" if status_code is not None else ""
                print(f"LLM_JSON attempt={request_no} ok=false{status_part} err={type(e).__name__} dur={_dur:.2f}s [GIVEUP]", flush=True)
                publish(f"LLM_JSON attempt={request_no} ok=false{status_part} err={type(e).__name__} dur={_dur:.2f}s [GIVEUP]")
                break
            # 429 / 超时 / 远端连接失败 → 指数退避，更长等待
            msg = str(e)
            sleep_s = min(5.0, (2.0 ** (transient_attempts - 1)) * 1.0 + 0.3)
            if status_code == 429 or "TooManyRequests" in msg or "RateLimit" in msg or "rate limit" in msg:
                sleep_s += 0.5
            status_part = f" status={status_code}" if status_code is not None else ""
            print(f"LLM_JSON attempt={request_no}{status_part} err={type(e).__name__} dur={_dur:.2f}s sleep={sleep_s:.1f}s [RETRY]", flush=True)
            publish(f"LLM_JSON attempt={request_no}{status_part} err={type(e).__name__} dur={_dur:.2f}s sleep={sleep_s:.1f}s [RETRY]")
            await _ai.sleep(sleep_s)
    if last_err is not None:
        raise last_err
    return JSONCompletionResult(
        value=None,
        failure_kind=last_failure_kind,
        attempts=request_no,
        finish_reason=last_finish_reason,
        usage=usage_total,
        reasoning_length=reasoning_length_total,
        output_length=last_output_length,
        recovered_json=last_recovered,
        response_format_fallback=format_fallback_used,
        max_tokens_initial=max_tokens_initial,
        max_tokens_peak=max_tokens_peak,
        output_budget_escalated=output_budget_escalated,
        elapsed_ms=max(0, int((time.monotonic() - call_started) * 1000)),
    )


async def complete_json(system: str, user: str, *, max_tokens: int = DEFAULT_STRUCTURED_OUTPUT_TOKENS) -> Optional[dict]:
    """Backward-compatible JSON helper returning only the parsed object."""
    result = await complete_json_diagnostic(system, user, max_tokens=max_tokens)
    return result.value


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
    max_output_tokens: int = DEFAULT_STRUCTURED_OUTPUT_TOKENS,
) -> Optional[dict]:
    """Responses API + Ark web_search, returning parsed JSON or None."""
    client = get_client()
    resp = await client.responses.create(
        model=get_model_name(),
        instructions=system,
        input=user,
        tools=[{"type": "web_search", "max_keyword": max(1, min(int(max_keywords or 3), 10))}],
        max_output_tokens=output_token_limit(max_output_tokens),
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
