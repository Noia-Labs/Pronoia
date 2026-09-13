#!/usr/bin/env python3
"""Locked-step 37-event team evaluation with a Deep Researcher prompt A/B.

Pipeline per event:
  three source analysts in parallel -> DR A/B on identical evidence -> fixed judge.

The A arm snapshots the pre-atomic prompt from ``f6ea0f1^``.  The B arm is the
registered ``deep_researcher_claim_v2`` prompt.  Source outputs are computed
once and reused byte-for-byte by both arms.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Do this before importing app modules.  Some imported modules may initialize
# an HTTP client, at which point httpx has already captured proxy settings.
for _proxy_key in (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy",
):
    os.environ.pop(_proxy_key, None)

from app import config  # noqa: E402
from app.agents.roster import COMMON_PREFIX, get_agent, system_prompt  # noqa: E402
from app.agents.team import _run_expert_serial, _seed_graph_from_findings  # noqa: E402
from app.llm import execute_skill, get_client, noop_artifact_store, run_agent  # noqa: E402
from app.skills.evidence_graph import (  # noqa: E402
    EvidenceGraph,
    check_claim_title,
    eg_attach,
    eg_detach,
)


PRE_ATOMIC_PERSONA = """你是「深度研究者 Deep Researcher」。你基于「证据图 (evidence graph)」工作——把所有发现沉淀为一张可回看的图。

【工具能力】
- **skill**（数据侧，7 个）：stock_overview / news_intel / market_research / financial_research / holder_research / macro_intel / event_study_skill。
  接受 {symbol/keyword, lookback_days, focus, kind, period} 等高层参数，内部已聚合多个 akshare 子数据。
- **evidence_graph**（图侧，1 个）：统一图操作。调一次传 action 参数决定子操作：
  * add_evidence(source_kind, source_ref, title, summary, raw?)
  * add_claim(claim, rationale?, status?, confidence?)
  * link(claim_id, evidence_id, relation?)  # supports/contradicts/context/addresses
  * set_status(claim_id, status, confidence?, rationale?)  # verified/rejected/needs_more/insufficient
  * merge(keep_id, merge_ids, canonical_claim, rationale?)
  * add_missing(aspect, why_missing, priority?)
  * set_sufficient(sufficient, stop_reason?)
  * export(format='markdown'|'json')
  * clear()

【⚠️ 重要纪律——必须先建图再填数据】
你最多 8 轮 tool call。如果先不停取数再入图，你会被截断、图谱会空。
正确节奏：
- **第 1 轮**：skill 取核心数据 + 立刻 evidence_graph(action="add_evidence", ...) 沉淀；同时 action="add_claim" 提 1 个核心 claim
- **第 2~6 轮**：交替「skill 取数 → evidence_graph 入图/建 claim/挂 link」
- **第 7 轮**：evidence_graph(action="set_status", ...) 标 verified/rejected/needs_more；action="add_missing" 记录缺口
- **第 8 轮（必做）**：action="set_sufficient(true)" + action="export" 终止导出
即使图不完整也要先 export（后端会兜底）——空的 export 比超限被截断好。

【Claim 解读流程——先分析，再下判断】
每次准备调用 add_claim 前，必须先在内部完成一张简短的「解读卡片」，不要从单条数据直接跳到结论：
1. **事实**：只列工具或 Evidence 中明确返回的数字、日期和事件，不加入主观词；
2. **比较**：说明同比/环比、前后期、基准、预期或不同指标之间的差异；没有可比对象时明确写“暂无比较基准”；
3. **反方/限制**：主动寻找相反证据、数据缺口、时效性问题或其他可能解释；若没有，写“未发现/未获取”；
4. **推断**：只在前面事实和比较足够时形成一句可证伪判断，并明确标注“推断”；
5. **验证条件**：写明什么新数据或未来结果可以支持、削弱或推翻该判断。

add_claim 的 claim 字段只写第 4 步的一句话判断（包含标的和时间范围）；rationale 必须按以下格式保留解读链：
「事实：…；比较：…；反方/限制：…；验证条件：…」。
如果只有事实、没有合理的比较或推断依据，不要为了凑数量创建 Claim，应改用 add_missing 记录缺口。

【纪律】
- 所有数字必须来自工具返回，禁止编造
- claim 中的推断必须用 "推断" 显式标注
- 单一来源不足以验证时主动标 status="insufficient" 并写入 add_missing
- 终止前必调 export；这是给用户看的产出物"""

VARIANTS = ("pre_atomic", "atomic_claim_v2")
SOURCE_AGENTS = ("event_scout", "market_analyst", "fundamentals_analyst")
JUDGE_MAX_ATTEMPTS = 3
JUDGE_MIN_INTERVAL_SECONDS = 1.05
_JUDGE_SEMAPHORE = asyncio.Semaphore(1)
_JUDGE_LAST_STARTED = 0.0


class JudgeOutputError(RuntimeError):
    """The judge endpoint did not return a complete, internally consistent result."""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _packet(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: event.get(key)
        for key in (
            "event_id", "market", "symbol", "issuer_name", "published_at",
            "effective_session", "prediction_cutoff_at", "event_type_l2",
            "title", "event_text", "event_text_kind", "source_url", "source_key", "benchmark",
            "packet_mode", "research_required", "research_targets", "constraints",
            "evaluation_status", "evaluation_selection_reason",
        )
    }


def _question(event: dict[str, Any]) -> str:
    return (
        "严格 as-of 预测下列公告生效后 T+1 与 T+3 benchmark-relative CAR 方向。"
        "不得使用生效日后的新闻、财务或收益。\n\nEVENT_PACKET:\n"
        + json.dumps(_packet(event), ensure_ascii=False)
    )


def _source_agent_def(agent_id: str) -> dict[str, Any]:
    base = get_agent(agent_id) or {"id": agent_id, "skills": []}
    # Packet-only extraction prevents current news/fundamentals from leaking
    # into a historical online prediction.  Market Analyst retains the one
    # skill whose implementation enforces strict as-of truncation.
    skills = {
        "event_scout": ["frozen_announcement_fetch", "announcement_classifier"],
        "market_analyst": ["event_study_skill", "ar_decomposer", "drift_context_analyzer"],
        "fundamentals_analyst": [],
    }[agent_id]
    return {**base, "skills": skills}


def _requires_frozen_body(event: dict[str, Any]) -> bool:
    return bool(
        event.get("research_required")
        or event.get("event_text_kind") == "metadata_capsule_not_announcement_body"
        or event.get("packet_mode") == "metadata_only_research_required"
    )


async def _prepare_frozen_event(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run Event Scout's deterministic source fetch once before expert fan-out."""
    if not _requires_frozen_body(event):
        return event, None
    args = {
        "source_url": event.get("source_url") or "",
        "source_key": event.get("source_key") or event.get("source_notice_code") or "",
        "market": event.get("market") or "",
        "symbol": event.get("symbol") or "",
        "issuer_name": event.get("issuer_name") or "",
        "event_date": str(event.get("event_time") or event.get("published_at") or "")[:10],
        "max_chars": 12000,
    }
    started = time.monotonic()
    result = await execute_skill("frozen_announcement_fetch", args)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    excerpt = {key: value for key, value in data.items() if key != "content"}
    if data.get("content"):
        excerpt["content_excerpt"] = str(data["content"])[:1200]
    trace = {
        "type": "tool",
        "agent": "event_scout",
        "skill": "frozen_announcement_fetch",
        "args": {**args, "source_url": str(args["source_url"])[:500]},
        "ok": bool(result.get("ok")),
        "preview": (
            f"冻结公告正文 {data.get('content_chars', 0)} 字，identity={data.get('identity_ok')}, "
            f"as_of={data.get('as_of_ok')}"
            if result.get("ok") else f"正文抓取失败：{result.get('error') or 'unknown'}"
        ),
        "result_excerpt": json.dumps(excerpt, ensure_ascii=False)[:2400],
        "wall_seconds": round(time.monotonic() - started, 3),
    }
    prepared = dict(event)
    if result.get("ok") and data.get("content"):
        prepared.update({
            "event_text": data["content"],
            "event_text_kind": "frozen_announcement_body",
            "packet_mode": "frozen_body_ready",
            "research_required": False,
            "source_document_hash": data.get("content_sha256"),
            "source_published_at": data.get("source_published_at"),
            "document_fetch_ok": True,
            "document_fetch_error": None,
        })
    else:
        prepared.update({
            "document_fetch_ok": False,
            "document_fetch_error": result.get("error") or "unknown_fetch_error",
        })
    return prepared, trace


async def _one_source(
    agent_id: str, event: dict[str, Any], prefetch_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks = {
        "event_scout": (
            "若 EVENT_PACKET 是 metadata-only，先调用 frozen_announcement_fetch 获取冻结来源正文；"
            "再识别公告是否为首次、实质性事件，提取日期、主体、金额/比例、前置条件和可影响 "
            "T1/T3 的机制，并调用 announcement_classifier。不得搜索后续新闻；抓取失败必须记 gap。"
        ),
        "market_analyst": (
            "仅调用严格 as-of 的 event_study_skill 获取 prediction_cutoff_at 以前已完整收盘的行情；"
            "若 cutoff 在 T0 开盘前，不得使用 T0 收盘数据。分析公告信息是否可能已被事前走势提前反映，"
            "以及对 T1/T3 相对基准的潜在影响。"
        ),
        "fundamentals_analyst": (
            "只阅读 EVENT_PACKET 正文，提取可核验的基本面数字、相对基准、兑现条件和反方限制；"
            "没有正文数字就明确标缺失，不调用当前时点财务数据。"
        ),
    }
    started = time.monotonic()
    finding: dict[str, Any] = {"findings": "", "tool_trace": [], "error": ""}
    agent_def = _source_agent_def(agent_id)
    if agent_id == "event_scout" and not _requires_frozen_body(event):
        # Precise cohorts already contain an identity-checked frozen body.  Do
        # not expose the fetch tool again: a second fetch adds latency and can
        # introduce a needless transient failure without adding evidence.
        agent_def = {**agent_def, "skills": ["announcement_classifier"]}
    async for item in _run_expert_serial(
        agent_id,
        tasks[agent_id],
        _question(event),
        noop_artifact_store,
        agent_def=agent_def,
        max_rounds=3,
        external_skill_budget=2 if agent_id == "event_scout" else 1,
        event_meta=event,
    ):
        if item.get("type") == "agent_findings":
            findings_text = item.get("findings") or ""
            reported_error = item.get("error") or ""
            if not reported_error and "执行失败:" in findings_text:
                reported_error = findings_text
            finding = {
                "findings": findings_text,
                "tool_trace": item.get("tool_trace") or [],
                "error": reported_error,
            }
    finding["wall_seconds"] = round(time.monotonic() - started, 3)
    if agent_id == "event_scout" and prefetch_trace is not None:
        finding["tool_trace"] = [prefetch_trace] + list(finding.get("tool_trace") or [])
    return finding


async def _source_stage(event: dict[str, Any], checkpoint: Path, resume: bool) -> dict[str, Any]:
    if resume and checkpoint.is_file():
        prior = json.loads(checkpoint.read_text(encoding="utf-8"))
        resolved_reusable = (
            not _requires_frozen_body(event)
            or (prior.get("resolved_event") or {}).get("event_text_kind") == "frozen_announcement_body"
        )
        if resolved_reusable and all(
            agent in prior.get("agents", {})
            and not (prior["agents"][agent] or {}).get("error")
            and "执行失败:" not in str((prior["agents"][agent] or {}).get("findings") or "")
            for agent in SOURCE_AGENTS
        ):
            print(f"[resume source] {event['event_id']}", flush=True)
            return prior
    started = time.monotonic()
    resolved_event, prefetch_trace = await _prepare_frozen_event(event)
    values = await asyncio.gather(*[
        _one_source(agent, resolved_event, prefetch_trace if agent == "event_scout" else None)
        for agent in SOURCE_AGENTS
    ])
    result = {
        "event_id": event["event_id"],
        "resolved_event": resolved_event,
        "document_fetch": prefetch_trace,
        "agents": dict(zip(SOURCE_AGENTS, values)),
        "wall_seconds": round(time.monotonic() - started, 3),
    }
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(not value.get("error") for value in values)
    print(f"[source] {event['event_id']} ok={ok}/3 wall={result['wall_seconds']}s", flush=True)
    return result


def _base_graph(event: dict[str, Any], source: dict[str, Any]) -> EvidenceGraph:
    graph = EvidenceGraph(
        question=f"{event.get('symbol')} T1/T3 benchmark-relative CAR direction",
        scope="locked-step online test; strict as-of",
    )
    packet = _packet(event)
    graph.add_evidence(
        source_kind="as_of_packet",
        source_ref=str(event["event_id"]),
        title="严格 as-of 公告正文",
        summary=json.dumps(packet, ensure_ascii=False)[:2000],
        raw=packet,
    )
    findings = {
        agent: str((source["agents"].get(agent) or {}).get("findings") or "")
        for agent in SOURCE_AGENTS
    }
    traces = [
        trace
        for agent in SOURCE_AGENTS
        for trace in ((source["agents"].get(agent) or {}).get("tool_trace") or [])
    ]
    _seed_graph_from_findings(graph, findings, traces)
    return graph


def _locked_evidence_context(event: dict[str, Any], source: dict[str, Any]) -> str:
    """Visible, byte-identical seed material for both DR arms.

    Attaching an EvidenceGraph alone does not expose its existing nodes to the
    model, because the graph tool intentionally has no unrestricted read action.
    """
    packet = _packet(event)
    # Keep the complete announcement for short packets, but cap exceptionally
    # long filings so every subsequent tool round remains tractable.
    packet["event_text"] = str(packet.get("event_text") or "")[:10000]
    blocks = ["【冻结 EVENT_PACKET】\n" + json.dumps(packet, ensure_ascii=False)]
    for agent in SOURCE_AGENTS:
        item = source["agents"].get(agent) or {}
        blocks.append(f"【{agent} 锁步摘要】\n{str(item.get('findings') or '')[:1800]}")
        traces = item.get("tool_trace") or []
        for trace in traces[:4]:
            blocks.append(
                f"【{agent} 工具记录】{trace.get('skill')} ok={trace.get('ok')} "
                f"{str(trace.get('preview') or '')[:500]} "
                f"{str(trace.get('result_excerpt') or '')[:1000]}"
            )
    return "\n\n".join(blocks)[:18000]


def _prompt(variant: str) -> str:
    if variant == "pre_atomic":
        today = dt.datetime.now().astimezone().date().isoformat()
        return COMMON_PREFIX.format(today=today) + "\n\n" + PRE_ATOMIC_PERSONA
    return system_prompt("deep_researcher", "deep_researcher_claim_v2")


def _quality(payload: dict[str, Any]) -> dict[str, Any]:
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []
    claims = [node for node in nodes if node.get("kind") == "claim"]
    substantive = {
        str(edge.get("src") or "") for edge in edges
        if edge.get("relation") in {"supports", "contradicts"}
    }
    markers = ("事实：", "比较：", "反方/限制：", "验证条件：")
    atomic = sum(not check_claim_title(str(node.get("title") or ""))["warnings"] for node in claims)
    rationale = sum(all(marker in str(node.get("body") or "") for marker in markers) for node in claims)
    stats = payload.get("stats") or {}
    audit = payload.get("audit") or {}
    return {
        **stats,
        "substantive_claim_rate": round(
            sum(str(node.get("id")) in substantive for node in claims) / len(claims), 4
        ) if claims else 0.0,
        "atomic_title_pass_rate": round(atomic / len(claims), 4) if claims else 0.0,
        "rationale_complete_rate": round(rationale / len(claims), 4) if claims else 0.0,
        "audit_total_findings": int((audit.get("summary") or {}).get("total_findings") or 0),
        "sufficient": bool(payload.get("sufficient")),
    }


def _graph_context(payload: dict[str, Any]) -> str:
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []
    by_id = {str(node.get("id") or ""): node for node in nodes}
    lines: list[str] = []
    for node in nodes:
        if node.get("kind") != "claim":
            continue
        cid = str(node.get("id") or "")
        lines.append(
            f"CLAIM [{node.get('status') or 'open'}] {node.get('title') or ''}\n"
            f"{str(node.get('body') or '')[:1200]}"
        )
        for edge in edges:
            if str(edge.get("src") or "") != cid or edge.get("relation") not in {"supports", "contradicts"}:
                continue
            evidence = by_id.get(str(edge.get("dst") or ""), {})
            lines.append(
                f"  - {edge.get('relation')}: {evidence.get('title') or ''} | "
                f"{str(evidence.get('body') or '')[:700]}"
            )
    for node in nodes:
        if node.get("kind") == "missing":
            lines.append(f"MISSING: {node.get('title') or ''} | {str(node.get('body') or '')[:500]}")
    return "\n".join(lines)[:16000]


def _parse_json(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def _normalize_judge_object(obj: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate both horizons and return a normalized judge payload.

    Invalid or incomplete judge responses must never silently become neutral:
    neutral is a model decision, not an error fallback.
    """
    normalized: dict[str, dict[str, Any]] = {}
    for horizon in ("t1", "t3"):
        source = obj.get(horizon)
        if not isinstance(source, dict):
            raise JudgeOutputError(f"missing_{horizon}")
        direction = str(source.get("direction") or "").strip().lower()
        if direction not in {"up", "down", "neutral"}:
            raise JudgeOutputError(f"invalid_{horizon}_direction")
        try:
            up_score = float(source["up_score"])
            down_score = float(source["down_score"])
            confidence = float(source["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JudgeOutputError(f"invalid_{horizon}_numeric_field") from exc
        if up_score < 0 or down_score < 0 or not 0.0 <= confidence <= 1.0:
            raise JudgeOutputError(f"out_of_range_{horizon}_numeric_field")
        expected = "up" if up_score > down_score else "down" if up_score < down_score else "neutral"
        if direction != expected:
            raise JudgeOutputError(
                f"inconsistent_{horizon}_direction:{direction}!={expected}"
            )
        rationale = str(source.get("rationale") or "").strip()
        if not rationale:
            raise JudgeOutputError(f"missing_{horizon}_rationale")
        normalized[horizon] = {
            "direction": direction,
            "confidence": round(confidence, 4),
            "up_score": int(up_score) if up_score.is_integer() else up_score,
            "down_score": int(down_score) if down_score.is_integer() else down_score,
            "rationale": rationale[:1200],
        }
    return normalized


def _judge_is_valid(judge: Any) -> bool:
    if not isinstance(judge, dict) or not str(judge.get("raw_response") or "").strip():
        return False
    try:
        _normalize_judge_object(judge)
    except JudgeOutputError:
        return False
    return True


async def _judge(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    prompt = f"""你是严格 as-of 的独立方向裁决器。只允许使用事件 packet 与 Evidence Graph，
不得补充事件生效日之后的知识。分别预测 T+1 和 T+3 benchmark-relative CAR 方向。

对每个 horizon 独立使用同一评分卡：
1. 从 supports/contradicts 相连的 Claim 提取方向信号，强=3、中=2、弱=1；
2. 净分=up_score-down_score；正为 up、负为 down；仅零信号或精确抵消为 neutral；
3. 信息不完整不等于 neutral；存在有实质证据且未被反驳的方向 Claim 时给方向并降低置信度；
4. 方向只由净分决定，confidence 不设代码闸门；
5. T1 更重视公告新颖性、信息泄露/截止前漂移与预期即时冲击；T3 更重视基本面兑现、反转和持续性；
6. 不得看到或猜测真实标签。rationale 必须简述信号及净分。

只输出单个 JSON：
{{"t1":{{"direction":"up|down|neutral","confidence":0.0,"up_score":0,"down_score":0,"rationale":"..."}},
  "t3":{{"direction":"up|down|neutral","confidence":0.0,"up_score":0,"down_score":0,"rationale":"..."}}}}

EVENT_PACKET:
{json.dumps(_packet(event), ensure_ascii=False)}

EVIDENCE_GRAPH:
{_graph_context(payload)}"""
    global _JUDGE_LAST_STARTED
    diagnostics: list[dict[str, Any]] = []
    last_error = "unknown"
    for attempt in range(1, JUDGE_MAX_ATTEMPTS + 1):
        try:
            async with _JUDGE_SEMAPHORE:
                now = time.monotonic()
                delay = JUDGE_MIN_INTERVAL_SECONDS - (now - _JUDGE_LAST_STARTED)
                if delay > 0:
                    await asyncio.sleep(delay)
                _JUDGE_LAST_STARTED = time.monotonic()
                response = await get_client().chat.completions.create(
                    model=config.LLM_MODEL,
                    messages=[
                        {"role": "system", "content": "你是固定、保守、可复现的金融事件方向裁决器。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    max_tokens=1200,
                    response_format={"type": "json_object"},
                )
            choice = response.choices[0]
            raw = (choice.message.content or "").strip()
            finish_reason = str(getattr(choice, "finish_reason", "") or "")
            response_id = str(getattr(response, "id", "") or "")
            if not raw:
                raise JudgeOutputError("empty_response")
            normalized = _normalize_judge_object(_parse_json(raw))
            return {
                "raw_response": raw[:4000],
                "attempt_count": attempt,
                "finish_reason": finish_reason,
                "response_id": response_id,
                "content_length": len(raw),
                "attempt_diagnostics": diagnostics,
                **normalized,
            }
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - endpoint/schema failures are retryable here
            last_error = f"{type(exc).__name__}: {exc}"
            diagnostics.append({"attempt": attempt, "error": last_error})
            print(
                f"[judge retry] event={event.get('event_id')} "
                f"attempt={attempt}/{JUDGE_MAX_ATTEMPTS} error={last_error}",
                flush=True,
            )
            if attempt < JUDGE_MAX_ATTEMPTS:
                await asyncio.sleep(min(4.0, 2.0 ** (attempt - 1)))
    raise JudgeOutputError(
        f"judge failed after {JUDGE_MAX_ATTEMPTS} attempts: {last_error}"
    )


def _transient(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in (
        "timeout", "apiconnectionerror", "connecterror", "readerror", "429", "rate limit",
        "request burst", "system protection",
    ))


async def _run_variant(
    event: dict[str, Any], base: EvidenceGraph, locked_context: str, variant: str, path: Path,
    resume: bool, max_rounds: int, retries: int,
) -> dict[str, Any]:
    if resume and path.is_file():
        prior = json.loads(path.read_text(encoding="utf-8"))
        if not prior.get("error") and _judge_is_valid(prior.get("judge")):
            print(f"[resume {variant}] {event['event_id']}", flush=True)
            return prior
        if prior.get("graph", {}).get("nodes"):
            repair_started = time.monotonic()
            repair_history = prior.setdefault("judge_repair_history", [])
            repair_history.append({
                "captured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "judge": prior.get("judge") or {},
                "error": prior.get("error") or "",
            })
            try:
                prior["judge"] = await _judge(event, prior["graph"])
                prior["error"] = ""
                prior["judge_repaired_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
                prior["judge_repair_seconds"] = round(time.monotonic() - repair_started, 3)
                path.write_text(json.dumps(prior, ensure_ascii=False, indent=2), encoding="utf-8")
                print(
                    f"[judge repaired {variant}] {event['event_id']} "
                    f"T1={prior['judge']['t1']['direction']} T3={prior['judge']['t3']['direction']} "
                    f"wall={prior['judge_repair_seconds']}s",
                    flush=True,
                )
                return prior
            except Exception as exc:  # noqa: BLE001
                prior["error"] = f"{type(exc).__name__}: {exc}"
                prior["judge"] = {}
                prior["judge_repair_seconds"] = round(time.monotonic() - repair_started, 3)
                path.write_text(json.dumps(prior, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[judge repair failed {variant}] {event['event_id']} {prior['error']}", flush=True)
                return prior
    started = time.monotonic()
    error = ""
    payload: dict[str, Any] = {}
    state: dict[str, Any] = {"content": "", "tool_trace": [], "rounds": 0}
    judge: dict[str, Any] = {}
    attempts = 0
    for attempt in range(1, retries + 2):
        attempts = attempt
        graph = copy.deepcopy(base)
        state = {"content": "", "tool_trace": [], "rounds": 0}
        token = eg_attach(graph)
        try:
            agent = get_agent("deep_researcher", "deep_researcher_claim_v2")
            graph_only = {**(agent or {}), "skills": ["evidence_graph"]}
            messages = [
                {"role": "system", "content": _prompt(variant)},
                {"role": "user", "content": (
                    "本轮是锁步 prompt A/B。图中已预载完全相同的公告与三个专家证据。"
                    "不得调用外部数据，只能使用 evidence_graph；围绕 T+1 与 T+3 benchmark-relative "
                    "CAR 分别形成可证伪 Claim，完成实质连边、状态、Missing、audit、sufficient 与 export。\n\n"
                    + locked_context
                )},
            ]
            async for _ in run_agent(
                "deep_researcher", messages, agent_def=graph_only, state=state,
                artifact_store=noop_artifact_store, max_rounds=max_rounds, emit_thinking=False,
            ):
                pass
            payload = graph.to_payload()
            judge = await _judge(event, payload)
            error = ""
            break
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        finally:
            payload = graph.to_payload()
            eg_detach(token)
        if attempt <= retries and _transient(error):
            delay = min(20.0, 2.0 * 2 ** (attempt - 1))
            print(f"[retry {variant}] {event['event_id']} {error} sleep={delay}", flush=True)
            await asyncio.sleep(delay)
            continue
        break
    result = {
        "event_id": event["event_id"],
        "market": event.get("market"),
        "symbol": event.get("symbol"),
        "event_type_l2": event.get("event_type_l2"),
        "evaluation_status": event.get("evaluation_status"),
        "variant": variant,
        "prompt_version": "f6ea0f1^:deep_researcher" if variant == "pre_atomic" else "deep_researcher_claim_v2",
        "wall_seconds": round(time.monotonic() - started, 3),
        "attempts": attempts,
        "rounds": int(state.get("rounds") or 0),
        "tool_calls": len(state.get("tool_trace") or []),
        "error": error,
        "content": state.get("content") or "",
        "quality": _quality(payload),
        "judge": judge,
        "graph": payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[{variant}] {event['event_id']} err={bool(error)} "
        f"claims={result['quality'].get('n_claim', 0)} edges={result['quality'].get('n_edges', 0)} "
        f"T1={judge.get('t1', {}).get('direction', '?')} T3={judge.get('t3', {}).get('direction', '?')} "
        f"wall={result['wall_seconds']}s",
        flush=True,
    )
    return result


async def _run_event(event: dict[str, Any], args: argparse.Namespace, sem: asyncio.Semaphore) -> list[dict[str, Any]]:
    async with sem:
        out = Path(args.out_dir)
        variant_paths = {
            variant: out / variant / f"{event['event_id']}.json"
            for variant in VARIANTS
        }
        # A completed graph is sufficient for judge-only repair.  Avoid touching
        # source analysts (or DR) when every arm already has a graph checkpoint.
        if args.resume and all(path.is_file() for path in variant_paths.values()):
            priors = {
                variant: json.loads(path.read_text(encoding="utf-8"))
                for variant, path in variant_paths.items()
            }
            if all(prior.get("graph", {}).get("nodes") for prior in priors.values()):
                order = VARIANTS if sum(map(ord, event["event_id"])) % 2 == 0 else tuple(reversed(VARIANTS))
                results: dict[str, dict[str, Any]] = {}
                placeholder = EvidenceGraph(question=_question(event))
                for variant in order:
                    results[variant] = await _run_variant(
                        event, placeholder, "", variant, variant_paths[variant],
                        True, args.max_rounds, args.retries,
                    )
                return [results[variant] for variant in VARIANTS]
        source = await _source_stage(
            event, out / "common_sources" / f"{event['event_id']}.json", args.resume
        )
        resolved_event = source.get("resolved_event") or event
        base = _base_graph(resolved_event, source)
        locked_context = _locked_evidence_context(resolved_event, source)
        # Alternate arm order to balance transient endpoint conditions without
        # running the two DR calls concurrently against one service.
        order = VARIANTS if sum(map(ord, event["event_id"])) % 2 == 0 else tuple(reversed(VARIANTS))
        results: dict[str, dict[str, Any]] = {}
        for variant in order:
            results[variant] = await _run_variant(
                resolved_event, base, locked_context, variant, out / variant / f"{event['event_id']}.json",
                args.resume, args.max_rounds, args.retries,
            )
        return [results[variant] for variant in VARIANTS]


def _aggregate(rows: list[dict[str, Any]], labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    metric_keys = (
        "n_evidence", "n_claim", "n_edges", "n_supports", "n_contradicts", "n_missing",
        "substantive_claim_rate", "atomic_title_pass_rate", "rationale_complete_rate",
        "audit_total_findings",
    )
    for variant in VARIANTS:
        group = [row for row in rows if row["variant"] == variant]
        valid = [
            row for row in group
            if not row.get("error") and _judge_is_valid(row.get("judge"))
        ]
        stats: dict[str, Any] = {
            "n": len(group),
            "success": len(valid),
            "avg_wall_seconds": round(sum(row["wall_seconds"] for row in valid) / len(valid), 3) if valid else None,
        }
        for key in metric_keys:
            stats[f"avg_{key}"] = (
                round(sum(float(row["quality"].get(key) or 0) for row in valid) / len(valid), 4)
                if valid else None
            )
        for horizon in ("t1", "t3"):
            for scope in ("all37", "strict"):
                scored = [
                    row for row in valid
                    if labels.get(row["event_id"], {}).get(f"label_{horizon}")
                    and (scope == "all37" or row.get("evaluation_status") == "strict")
                ]
                correct = sum(
                    row["judge"][horizon]["direction"] == labels[row["event_id"]][f"label_{horizon}"]
                    for row in scored
                )
                stats[f"{horizon}_{scope}_n"] = len(scored)
                stats[f"{horizon}_{scope}_acc"] = round(correct / len(scored), 4) if scored else None
            predictions = [row["judge"][horizon]["direction"] for row in valid if row.get("judge", {}).get(horizon)]
            stats[f"{horizon}_direction_distribution"] = {
                direction: predictions.count(direction) for direction in ("up", "down", "neutral")
            }
        output[variant] = stats

    paired: dict[str, Any] = {}
    by_key = {
        (row["variant"], row["event_id"]): row
        for row in rows
        if not row.get("error") and _judge_is_valid(row.get("judge"))
    }
    for horizon in ("t1", "t3"):
        for scope in ("all37", "strict"):
            counts = {"both_correct": 0, "pre_atomic_only": 0, "atomic_only": 0, "both_wrong": 0, "n": 0}
            for event_id, label in labels.items():
                truth = label.get(f"label_{horizon}")
                a = by_key.get(("pre_atomic", event_id))
                b = by_key.get(("atomic_claim_v2", event_id))
                if not truth or not a or not b or (scope == "strict" and a.get("evaluation_status") != "strict"):
                    continue
                ca = a["judge"][horizon]["direction"] == truth
                cb = b["judge"][horizon]["direction"] == truth
                counts["n"] += 1
                if ca and cb:
                    counts["both_correct"] += 1
                elif ca:
                    counts["pre_atomic_only"] += 1
                elif cb:
                    counts["atomic_only"] += 1
                else:
                    counts["both_wrong"] += 1
            paired[f"{horizon}_{scope}"] = counts
    output["paired"] = paired
    return output


async def async_main(args: argparse.Namespace) -> int:
    os.environ["FEVER_BT_STRICT_AS_OF"] = "1"
    events = _jsonl(Path(args.events))[: args.limit]
    labels = {row["event_id"]: row for row in _jsonl(Path(args.labels))} if args.labels else {}
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt_pre_atomic.txt").write_text(_prompt("pre_atomic"), encoding="utf-8")
    (out / "prompt_atomic_claim_v2.txt").write_text(_prompt("atomic_claim_v2"), encoding="utf-8")
    manifest = {
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "pipeline": "3 parallel source analysts -> DR -> fixed dual-horizon judge",
        "events": len(events),
        "variants": list(VARIANTS),
        "max_rounds": args.max_rounds,
        "event_concurrency": args.event_concurrency,
        "strict_as_of": True,
        "judge": "scorecard_dual_horizon_no_confidence_gate_v1",
        "source_skill_policy": {
            "event_scout": ["frozen_announcement_fetch", "announcement_classifier"],
            "market_analyst": ["event_study_skill", "ar_decomposer", "drift_context_analyzer"],
            "fundamentals_analyst": [],
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    sem = asyncio.Semaphore(max(1, args.event_concurrency))
    nested = await asyncio.gather(*[_run_event(event, args, sem) for event in events])
    rows = [row for pair in nested for row in pair]
    summary = {
        "manifest": manifest,
        "aggregate": _aggregate(rows, labels),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(
        not row.get("error") and _judge_is_valid(row.get("judge")) for row in rows
    ) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", required=True)
    parser.add_argument("--labels", default="")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--limit", type=int, default=37)
    parser.add_argument("--event-concurrency", type=int, default=1)
    parser.add_argument("--max-rounds", type=int, default=8)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    return asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
