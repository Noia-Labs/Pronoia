"""Step-by-step debug for team_full runner.

Measures per-step wall time:
  1. LLM basic call (sanity check)
  2. Router/plan JSON call
  3. Market analyst run_agent (multi-round tool-call loop)
  4. Fundamentals analyst run_agent
  5. Deep researcher run_agent (with evidence graph)
  6. Synthesizer (final JSON extraction)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("PRONOIA_BT_FAST", "1")
os.environ.setdefault("PRONOIA_BT_STRICT_AS_OF", "1")
os.environ.setdefault("TQDM_DISABLE", "1")

from app import config  # noqa: E402
from app.agents.roster import AGENTS, get_agent, system_prompt  # noqa: E402
from app.agents.team import (  # noqa: E402
    EXPERT_IDS,
    PLANNER_INSTRUCTION,
    run_team,
)
from app.event_backtest.engine import TEAM_FULL_QUESTION_TEMPLATE, _event_prompt  # noqa: E402
from app.event_backtest.market import resolve_benchmark  # noqa: E402
from app.event_backtest.models import EventRecord  # noqa: E402
from app.llm import complete_json, noop_artifact_store, run_agent  # noqa: E402


def log(step: str, msg: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    if msg:
        print(f"[{ts}] STEP {step}: {msg}", flush=True)
    else:
        print(f"[{ts}] STEP {step}", flush=True)


def elapsed(t0: float, step: str) -> None:
    dt = time.time() - t0
    print(f"    ⏱ {step} took {dt:.1f}s", flush=True)


# ---------------------------------------------------- event loading
def load_event(jsonl_path: str, idx: int = 0) -> EventRecord:
    events = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    print(f"Loaded {len(events)} events from {jsonl_path}")
    e = events[idx]
    ev = EventRecord(**e)
    bm = resolve_benchmark(ev)
    object.__setattr__(ev, "benchmark", bm)
    return ev


# ---------------------------------------------------- Step 1: raw LLM
async def step1_raw_llm() -> None:
    log("1", f"Raw LLM sanity call (model={config.LLM_MODEL})")
    t0 = time.time()
    r = await complete_json(
        "你是一个测试助手，仅回复JSON。",
        '请返回 {"ok": true, "ping": "pong"}',
        max_tokens=200,
    )
    elapsed(t0, "complete_json (5 token)")
    print(f"    Result: {r}", flush=True)


# ---------------------------------------------------- Step 2: plan
async def step2_plan(question: str) -> list[dict]:
    log("2", "Router plan JSON call")
    t0 = time.time()
    plan_json = await complete_json(
        system_prompt("router") + "\n\n" + PLANNER_INSTRUCTION,
        f"用户问题：{question}",
        max_tokens=2000,
    )
    elapsed(t0, "router complete_json plan")
    plan: list[dict] = []
    if plan_json:
        for t in plan_json.get("tasks", []):
            aid = str(t.get("agent", "")).strip()
            task_text = str(t.get("task", "")).strip()
            if aid in EXPERT_IDS and task_text and all(p["agent"] != aid for p in plan):
                plan.append({"agent": aid, "task": task_text})
    print(f"    Plan tasks: {len(plan)}")
    for p in plan:
        print(f"      - {p['agent']}: {p['task'][:80]}...")
    # FAST 模式 + 白名单
    plan = [p for p in plan if p["agent"] in {"market_analyst", "fundamentals_analyst", "deep_researcher"}]
    if not plan:
        plan = [
            {"agent": "market_analyst", "task": f"围绕行情、资金、关键事件价格反应分析：{question[:200]}"},
            {"agent": "fundamentals_analyst", "task": f"围绕基本面、财务、机构观点分析：{question[:200]}"},
            {"agent": "deep_researcher", "task": "构建证据图并给出综合判断"},
        ]
    # deep_researcher 放到最后
    plan = [p for p in plan if p["agent"] != "deep_researcher"] + [p for p in plan if p["agent"] == "deep_researcher"]
    plan = plan[:4]
    return plan


# ---------------------------------------------------- Step 3: one expert
async def step3_expert(
    seq: int,
    agent_id: str,
    task_text: str,
    question: str,
    prior_findings: dict | None = None,
    prior_tool_trace: list | None = None,
) -> tuple[str, list]:
    log(f"3.{seq}", f"Expert {agent_id} | run_agent max_rounds={config.TEAM_MAX_ROUNDS}")
    from app.agents.team import EvidenceGraph, eg_attach, eg_detach, _seed_graph_from_findings, _evidence_digest, _short_summary

    graph = None
    token = None
    evidence_context = ""
    if agent_id == "deep_researcher":
        graph = EvidenceGraph(question=question, scope=task_text[:200])
        token = eg_attach(graph)
        _seed_graph_from_findings(graph, prior_findings or {}, prior_tool_trace or [])
        digest_blocks: list[str] = []
        for aid, txt in (prior_findings or {}).items():
            if txt.strip():
                digest_blocks.append(f"【{aid} 既有发现】\n{txt[:800]}")
        tool_digest = _evidence_digest(prior_tool_trace or [], max_chars=1800)
        if tool_digest:
            digest_blocks.append(f"【已有工具证据摘要】\n{tool_digest}")
        if digest_blocks:
            evidence_context = (
                "\n\n【你必须先继承这些既有证据并补全证据图】\n"
                + "\n\n".join(digest_blocks)
            )

    messages = [
        {"role": "system", "content": system_prompt(agent_id)},
        {"role": "user", "content":
            f"【用户原始问题】{question}\n\n【你的子任务】{task_text}\n\n"
            "请调用你的技能获取真实数据后作答；最后用不超过600字总结发现（含关键数字+来源）。"
            + evidence_context},
    ]
    expert_state: dict = {"content": "", "tool_trace": [], "rounds": 0}
    t0 = time.time()
    n_rounds = 0
    n_tool_calls = 0
    try:
        async for ev in run_agent(agent_id, messages, agent_def=get_agent(agent_id),
                                  state=expert_state, artifact_store=noop_artifact_store,
                                  max_rounds=config.TEAM_MAX_ROUNDS):
            t = ev.get("type")
            if t == "tool_call":
                n_tool_calls += 1
                print(f"    tool call #{n_tool_calls}: {ev.get('name')} {str(ev.get('args'))[:120]}", flush=True)
            if t == "tool_result":
                ok = ev.get("ok", "?")
                preview = str(ev.get("preview", ""))[:100]
                dt = ev.get("elapsed_ms", -1)
                print(f"    tool result: ok={ok} {dt}ms preview={preview}", flush=True)
            if t == "agent_step" and ev.get("phase") == "agent_done":
                break
        n_rounds = expert_state.get("rounds", 0)
    finally:
        if token is not None:
            try:
                eg_detach(token)
            except Exception:
                pass
    dt = time.time() - t0
    findings = expert_state["content"].strip()[:600]
    print(f"    rounds={n_rounds}, tool_calls={n_tool_calls}, took {dt:.1f}s", flush=True)
    print(f"    findings preview: {findings[:200]}...", flush=True)
    return findings, expert_state["tool_trace"]


# ---------------------------------------------------- Step 4: synthesize
async def step4_synthesize(question: str, findings_by_agent: dict) -> dict:
    log("4", "Synthesizer JSON final answer")
    system = (
        "你是综合研判专家。基于前序多位分析师的发现，给出最终 T+3 方向判断。\n"
        "输出 JSON: {\"pred_direction\": \"up|down|neutral\", \"confidence\": 0.xx, \"rationale\": \"不超过300字\"}\n"
        "pred_direction 选择规则：\n"
        "- 明确上涨驱动且无重大对冲 → up\n"
        "- 明确下跌驱动且无重大对冲 → down\n"
        "- 证据不足 / 对冲严重 / |CAR|<50bps 高概率 → neutral"
    )
    blocks = []
    for aid, f in findings_by_agent.items():
        if f.strip():
            blocks.append(f"【{aid} 发现】\n{f}")
    user = f"{question}\n\n" + "\n\n".join(blocks) + "\n\n请给出最终方向判断（JSON）。"
    t0 = time.time()
    r = await complete_json(system, user, max_tokens=800)
    elapsed(t0, "synthesizer complete_json")
    print(f"    Result: {r}", flush=True)
    return r or {}


# ---------------------------------------------------- full pipeline
async def debug_event(ev: EventRecord) -> None:
    print("\n" + "=" * 80, flush=True)
    print(f" EVENT {ev.event_id}  |  {ev.market}/{ev.symbol}  |  {ev.event_type_l2}", flush=True)
    print(f" Title: {ev.title[:100]}...", flush=True)
    print(f" Benchmark: {ev.benchmark}  EventTime: {ev.event_time}", flush=True)
    print("=" * 80, flush=True)

    packet = _event_prompt(ev)
    FAST = True
    preamble = ""
    if FAST:
        preamble = (
            f"\n\n【回测上下文 - STRICT AS-OF 模式 - 禁止未来函数】"
            f"\n- 标的市场：{ev.market}，代码：{ev.symbol}"
            f"\n- 对比基准（benchmark）：{ev.benchmark}"
            f"\n- 事件类型：{ev.event_type_l2}"
            f"\n- 事件时间：{ev.event_time}"
            f"\n- as_of_packet 已经包含事件原文（标题和正文），做事件研究时用 event_study_skill"
            f"  （event_date={str(ev.event_time)[:10]}, symbol={ev.symbol}, window_days=20, benchmark={ev.benchmark}, **as_of=True**）。"
            f"\n  ⚠️  as_of=True 时 event_study_skill 仅返回事件日及以前的数据。"
        )
    question = TEAM_FULL_QUESTION_TEMPLATE.format(
        packet=packet,
        event_id=ev.event_id,
        market=ev.market,
        symbol=ev.symbol,
        event_time=str(ev.event_time),
        event_type_l2=ev.event_type_l2,
        benchmark=ev.benchmark,
        run_id="debug_single",
    ) + preamble

    COOLDOWN = float(os.environ.get("DEBUG_STEP_COOLDOWN", "0"))
    T0_total = time.time()
    await step1_raw_llm()
    print(f"    (cooldown {COOLDOWN:.0f}s before step2)", flush=True)
    await asyncio.sleep(COOLDOWN)

    plan = await step2_plan(question)
    print(f"    (cooldown {COOLDOWN:.0f}s before step3)", flush=True)
    await asyncio.sleep(COOLDOWN)

    findings_by_agent: dict[str, str] = {}
    all_tool_trace: list = []
    seq = 1
    for p in plan:
        aid = p["agent"]
        findings, tt = await step3_expert(
            seq, aid, p["task"], question,
            prior_findings=findings_by_agent if aid == "deep_researcher" else None,
            prior_tool_trace=all_tool_trace if aid == "deep_researcher" else None,
        )
        findings_by_agent[aid] = findings
        all_tool_trace.extend(tt)
        seq += 1
        if seq <= len(plan) + 0:  # 每个 expert 之间也冷却
            print(f"    (cooldown {COOLDOWN:.0f}s before next expert)", flush=True)
            await asyncio.sleep(COOLDOWN)

    print(f"    (cooldown {COOLDOWN:.0f}s before synthesizer)", flush=True)
    await asyncio.sleep(COOLDOWN)
    final = await step4_synthesize(question, findings_by_agent)
    total = time.time() - T0_total
    print("\n" + "-" * 60, flush=True)
    print(f"✅ 总耗时 {total:.1f}s  |  方向={final.get('pred_direction')}  conf={final.get('confidence')}", flush=True)
    print(f"   理由: {str(final.get('rationale', ''))[:200]}", flush=True)


async def main():
    events_path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "events_phase1_p0fix_sample10.jsonl")
    ev_idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    print(f"config.LLM_MODEL = {config.LLM_MODEL}")
    print(f"config.LLM_BASE_URL = {config.LLM_BASE_URL}")
    ev = load_event(events_path, ev_idx)
    await debug_event(ev)


if __name__ == "__main__":
    asyncio.run(main())
