"""Safe OpenAI-compatible transport and answer scoring for Model Lab."""
from __future__ import annotations

import json
import re
import socket
import time
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

from ..model_endpoint_security import (
    ValidatedModelRedirectHandler,
    allow_private_model_endpoints,
    validate_runtime_model_endpoint,
    redact_sensitive_text,
)
from .defaults import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_STRUCTURED_OUTPUT_TOKENS, DEFAULT_TIMEOUT_SECONDS


class ModelProviderError(RuntimeError):
    """A bounded, user-safe model transport or output error."""


class _TruncatedEmptyAnswer(ModelProviderError):
    """Valid completion exhausted its budget before emitting a final answer.

    Still an error for predictions, answers and scoring. A connection probe can
    report that the endpoint accepted the request without claiming a usable
    answer was generated. No answer or private reasoning is retained here.
    """

    def __init__(self, *, model_id: str, latency_ms: int) -> None:
        super().__init__("模型 API 返回空答案（finish_reason=length）")
        self.model_id = model_id
        self.latency_ms = latency_ms


def profile_snapshot(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze all execution-relevant fields without resolving the secret."""
    return {
        key: profile.get(key)
        for key in (
            "id", "name", "provider", "base_url", "model_id", "secret_env_ref",
            "max_output_tokens", "timeout_seconds", "thinking_mode",
            "input_price_per_million", "output_price_per_million", "currency", "updated_at",
        )
    }


def resolve_profile_secret(profile: Mapping[str, Any]) -> str:
    from .platform_default import is_platform_profile_id, resolve_environment_secret
    from .secret_store import ModelSecretError, resolve_secret_reference

    if is_platform_profile_id(profile.get("id")):
        return resolve_environment_secret(profile)
    try:
        return resolve_secret_reference(str(profile.get("secret_env_ref") or ""))
    except ModelSecretError as exc:
        raise ModelProviderError(str(exc)) from exc


def chat_completions_url(base_url: Any) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _runtime_validate(url: str) -> None:
    validate_runtime_model_endpoint(
        url,
        label="模型 API",
        resolver=socket.getaddrinfo,
        allow_private=allow_private_model_endpoints(),
    )


class _RedirectHandler(ValidatedModelRedirectHandler):
    def __init__(self) -> None:
        super().__init__(_runtime_validate, sensitive_headers={"Authorization"})


def _extract_content(message: Any) -> str:
    if isinstance(message, Mapping):
        content = message.get("content")
    else:
        content = None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts).strip()
    return ""


def _bounded_error(exc: BaseException | str, *secret_values: str) -> str:
    raw = redact_sensitive_text(exc or "模型调用失败", *secret_values)
    # Never persist a bearer credential even when a third-party error happens
    # to reflect a request header.
    raw = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", raw)
    raw = re.sub(r"(?i)(api[_ -]?key[=: ]+)[^\s,;]+", r"\1[redacted]", raw)
    return raw[:800]


def call_chat(
    profile: Mapping[str, Any],
    messages: Sequence[Mapping[str, str]],
    *,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Call one non-streaming OpenAI-compatible chat endpoint.

    Creation-time URL checks happen in the route. DNS is checked immediately
    before opening the request and every redirect is revalidated. Cross-origin
    redirects have Authorization stripped by ``ValidatedModelRedirectHandler``.
    """
    url = chat_completions_url(profile.get("base_url"))
    _runtime_validate(url)
    secret = resolve_profile_secret(profile)
    limit = max(32, min(
        int(max_tokens or profile.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS),
        int(profile.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS),
    ))
    payload: dict[str, Any] = {
        "model": str(profile.get("model_id") or ""),
        "messages": [dict(item) for item in messages],
        "max_tokens": limit,
        "stream": False,
    }
    provider = str(profile.get("provider") or "").lower()
    thinking = str(profile.get("thinking_mode") or "auto")
    if thinking == "disabled" and "kimi" in provider:
        payload["thinking"] = {"type": "disabled"}
    if thinking == "disabled" and any(name in provider for name in ("qwen", "dashscope")):
        payload["enable_thinking"] = False
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    timeout = max(0.1, min(float(profile.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), 900.0))
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with build_opener(_RedirectHandler()).open(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(10_000_001)
    except HTTPError as exc:
        # Read a small response for diagnostics but never expose request data.
        detail = exc.read(1_001).decode("utf-8", errors="replace")
        raise ModelProviderError(_bounded_error(f"模型 API HTTP {exc.code}: {detail}", secret)) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ModelProviderError(_bounded_error(f"模型 API 调用失败: {exc}", secret)) from exc
    latency_ms = max(0, int((time.monotonic() - started) * 1000))
    if len(raw) > 10_000_000:
        raise ModelProviderError("模型 API 响应超过 10 MB")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelProviderError("模型 API 未返回合法 JSON") from exc
    if isinstance(body, Mapping) and body.get("error"):
        raise ModelProviderError("模型 API 返回错误响应")
    choices = body.get("choices") if isinstance(body, Mapping) else None
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, Mapping) else {}
    answer = _extract_content(message)
    if not answer:
        finish_reason = choice.get("finish_reason") if isinstance(choice, Mapping) else None
        usage = body.get("usage") if isinstance(body, Mapping) else None
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens")) if isinstance(usage, Mapping) else None
        # Only a structurally valid successful generation is evidence of a
        # working connection. Never infer success from an error envelope or
        # the text of an exception (which may include a provider error body).
        has_output_usage = isinstance(output_tokens, (int, float)) and not isinstance(output_tokens, bool) and output_tokens > 0
        has_reasoning = isinstance(message, Mapping) and isinstance(message.get("reasoning_content"), str) and bool(message["reasoning_content"].strip())
        valid_message = (
            isinstance(message, Mapping)
            and message.get("role", "assistant") == "assistant"
            and "content" in message
            and (message["content"] is None or isinstance(message["content"], (str, list)))
        )
        if finish_reason == "length" and valid_message and (has_output_usage or has_reasoning) and not body.get("error"):
            raise _TruncatedEmptyAnswer(
                model_id=str(body.get("model") or profile.get("model_id") or ""),
                latency_ms=latency_ms,
            )
        raise ModelProviderError(f"模型 API 返回空答案（finish_reason={finish_reason or 'unknown'}）")
    usage_raw = body.get("usage") if isinstance(body, Mapping) else {}
    usage_raw = usage_raw if isinstance(usage_raw, Mapping) else {}
    input_tokens = usage_raw.get("prompt_tokens", usage_raw.get("input_tokens"))
    output_tokens = usage_raw.get("completion_tokens", usage_raw.get("output_tokens"))
    total_tokens = usage_raw.get("total_tokens")
    usage = {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "total_tokens": int(total_tokens or ((input_tokens or 0) + (output_tokens or 0))),
        "basis": "api_usage" if usage_raw else "unavailable",
    }
    return {
        "answer": answer,
        "model_id": str(body.get("model") or profile.get("model_id") or ""),
        "finish_reason": choice.get("finish_reason") if isinstance(choice, Mapping) else None,
        "usage": usage,
        "latency_ms": latency_ms,
    }


def validate_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    probe_tokens = 32
    try:
        result = call_chat(
            profile,
            [
                {"role": "system", "content": "You are a connection test. Reply with exactly OK."},
                {"role": "user", "content": "OK"},
            ],
            max_tokens=probe_tokens,
        )
    except _TruncatedEmptyAnswer as exc:
        result = {"model_id": exc.model_id, "latency_ms": exc.latency_ms, "finish_reason": "length"}
    probe_incomplete = result.get("finish_reason") == "length"
    return {
        "ok": True,
        "latency_ms": result["latency_ms"],
        "model_id": result["model_id"],
        "status": "connected_probe_incomplete" if probe_incomplete else "connected",
        "answer_verified": not probe_incomplete,
        "message": (
            f"连接已确认；{probe_tokens} tokens 小额探测预算已用尽，尚未验证完整回答。正式测试使用已保存的输出上限。"
            if probe_incomplete else "连接成功，已收到模型回答"
        ),
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ModelProviderError("评分模型没有返回有效 JSON")
        try:
            value = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ModelProviderError("评分模型没有返回有效 JSON") from exc
    if not isinstance(value, dict):
        raise ModelProviderError("评分模型必须返回 JSON 对象")
    return value


def _score_int(value: Any, maximum: int) -> int:
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(maximum, int(number)))


def score_answer(
    judge_profile: Mapping[str, Any],
    question: Mapping[str, Any],
    anonymous_answer: str,
) -> dict[str, Any]:
    """Apply Pronoia's eight-dimensional anonymous evaluation rubric."""
    system = (
        "你是金融研究评测员。只评估匿名答案，不猜测答案来自哪个模型。"
        "按上限给整数分：事实数值30、证据20、方法适配15、推理10、风险10、"
        "可用性5、可复现性5、用户价值5。不要因篇幅长而加分。无法获得实时数据时，"
        "明确限制优于编造；未完成任务仍应降低事实和证据分。重大错误仅包括伪造来源或"
        "关键数值、把未来当事实、泄露秘密/隐私、危险的无条件交易指令。"
        "严格返回一个 JSON 对象，不要 Markdown。"
    )
    user = json.dumps(
        {
            "question_id": question.get("code") or question.get("id"),
            "user_role": question.get("role"),
            "method": question.get("method"),
            "task": question.get("prompt"),
            "expected_deliverable": question.get("deliverable"),
            "gold_standard": question.get("gold_standard"),
            "core_metrics": question.get("metrics"),
            "anonymous_answer": str(anonymous_answer or "")[:18_000],
            "required_json": {
                "fact": "0-30", "evidence": "0-20", "method": "0-15",
                "reasoning": "0-10", "risk": "0-10", "usability": "0-5",
                "reproducibility": "0-5", "user_value": "0-5",
                "major_error": "boolean", "confidence": "low|medium|high",
                "rationale": "不超过120字",
            },
        },
        ensure_ascii=False,
    )
    response = call_chat(
        judge_profile,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=min(DEFAULT_STRUCTURED_OUTPUT_TOKENS, int(judge_profile.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS)),
        json_mode=True,
    )
    raw = _parse_json_object(response["answer"])
    score = {
        "fact": _score_int(raw.get("fact"), 30),
        "evidence": _score_int(raw.get("evidence"), 20),
        "method": _score_int(raw.get("method"), 15),
        "reasoning": _score_int(raw.get("reasoning"), 10),
        "risk": _score_int(raw.get("risk"), 10),
        "usability": _score_int(raw.get("usability"), 5),
        "reproducibility": _score_int(raw.get("reproducibility"), 5),
        "user_value": _score_int(raw.get("user_value", raw.get("userValue")), 5),
        "major_error": bool(raw.get("major_error", raw.get("majorError", False))),
        "confidence": str(raw.get("confidence") or "")[:40],
        "rationale": str(raw.get("rationale") or "")[:500],
        "judge_profile_id": judge_profile.get("id"),
        "judge_model_id": response["model_id"],
        "source": "auto",
    }
    score["total"] = sum(
        score[key]
        for key in (
            "fact", "evidence", "method", "reasoning", "risk", "usability",
            "reproducibility", "user_value",
        )
    )
    score["usage"] = response["usage"]
    score["latency_ms"] = response["latency_ms"]
    return score


def estimate_cost(profile: Mapping[str, Any], usage: Mapping[str, Any]) -> float:
    input_cost = float(usage.get("input_tokens") or 0) * float(
        profile.get("input_price_per_million") or 0
    ) / 1_000_000
    output_cost = float(usage.get("output_tokens") or 0) * float(
        profile.get("output_price_per_million") or 0
    ) / 1_000_000
    return round(input_cost + output_cost, 8)
