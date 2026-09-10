"""team 模式编排 (design.md §6.3): plan → fan-out(串行) → synthesize → verify → extract.

修订: 改为串行执行 experts，避免并发 reasoning_content 交错污染前端。
新增: 5) extract —— 从最终结论中抽取「待验证推演」作为研究逻辑库条目。
新增: deep_researcher 作为专家（基于证据图的多轮研究），跑前 attach 证据图、跑后 export 为图谱 artifact。
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from .. import config
from ..llm import ArtifactStore, complete_json, run_agent
from .evidence_navigator import EvidenceNavigator
from .research_context import ResearchContext
from ..skills.evidence_graph import (
    EvidenceGraph, eg_attach, eg_defer_export_artifact, eg_detach,
    eg_restore_export_artifact, get_current_graph,
)
from .roster import AGENTS, get_agent, system_prompt

EXPERT_IDS = ["event_scout", "market_analyst", "fundamentals_analyst", "deep_researcher", "predictor"]

# ======================================================================
# Pronoia-RLVR v1 · Tier 1.5 接入预留（design §3.3.1 / §3.4 · 先训练后接入）
# ======================================================================
# - Tier 1.5 RLVR_ANALYZER 作为独立 agent 与 Tier 1 analyzer 并行输出（可选，默认关闭）。
# - 开关 ENABLE_RLVR_TIER15 默认 False（因为需要先跑完 §6 路线图的模型训练才能加载权重）。
# - 合成阶段把 Tier 1.5 的 7 段 CoT 结果（【0.预判时间窗口】+【0.5 量价 regime 校验】
#   + … + 最终方向 + 置信度 + RET↔CAR 是否一致标志）拼进 synthesis 上下文，
#   按方案 3 信号加权融合，不取代人工分析。
ENABLE_RLVR_TIER15: bool = False

RLVR_ANALYZER_INSTRUCTION_IF_ENABLED = """【Tier 1.5 · Pronoia-RLVR 可验证奖励分析器（并行注入）】
- 先调用 rlvr_predictor(event_text, symbol, event_date) 拿 7 段推理链输出，
  得到：primary_horizon、vol_regime、direction_pred、confidence、
        car_t3_expected_pct、ret_car_agree_flag、融合权重来源说明。
- 只把【0.预判时间窗口】【0.5 量价 regime 校验】两段的数值，以及最终方向 +
  置信度 + RET↔CAR 一致标志拼进 synthesis；不整段复制避免污染。
- 若 ENABLE_RLVR_TIER15=False，则本节静默跳过，不影响默认 Team Pipeline 行为。"""

PLANNER_INSTRUCTION = """你是任务规划器。把用户问题拆成 2~4 个子任务，每个子任务指定一个专家 Agent：
- event_scout（事件猎手：新闻/公告/快讯检索，筛高影响事件）
- market_analyst（行情分析师：K线/指数/板块/龙虎榜/融资融券/事件研究）
- fundamentals_analyst（基本面分析师：财务摘要/财务指标/研报评级/宏观）
- deep_researcher（深度研究者：基于证据图的多轮研究 —— 适合需要从多个数据源反复验证假设的复杂问题；
  会把研究过程沉淀为一张可回看的证据图谱作为产出物。问题较深、可量化、可分多轮验证时优先用）
- predictor（事件预测员：后市推演协调者 —— 基于近期 K线/资金/新闻/板块输出
  「乐观/中性/悲观」候选情景 + 相对倾向 + 关键催化 + 可证伪假设；深度研究者导出
  证据图后，可由其发起独立的异步多智能体事件推演。
  适合「接下来怎么走 / 后市如何 / 某事件后行情」类前瞻问题）

【硬规则 —— 深度研究团队必派 deep_researcher】
无论问题类型，plan 列表里 **必须** 包含 deep_researcher —— 它是"研究主控"，负责：
  1) 在 expert 各自完成片段研究后，把所有 evidence/claim 串到一张证据图上
  2) 在合成阶段提供图谱视角，让 router 拿到结构化的 claim/evidence 集合
  3) 标记 research gap（add_missing），让团队下一步 follow-up 有据可依

【软规则 —— 涉及预测/后市时加派 predictor】
当用户问题含「预测 / 后市 / 怎么看 / 接下来 / 会不会 / 行情推演 / 短期 / 未来 X 天 / 下周」等
前瞻关键词时，plan 列表里 **应当** 包含 predictor —— 它专门做多情景推演，比 deep_researcher
更轻、更有针对性（不沉淀证据图；先输出候选情景，再由证据图支持异步多智能体推演）。

通常结构（推荐 3~5 任务）：
- event_scout：扫事件/公告/快讯
- market_analyst 或 fundamentals_analyst：行情/基本面之一（按问题倾向选一个）
- deep_researcher：放在资料型专家之后，把已有发现建图、标 claim、最后 export
- （预测类问题）predictor：**放在 deep_researcher 之后**，读取研究交接并协调异步推演

简单事实型问题也至少派 1 个 expert + deep_researcher 两任务。

严格输出 JSON：{"tasks": [{"agent": "<id>", "task": "<具体子问题，含标的与时间范围>"}]}
同一个专家最多出现一次；deep_researcher 必出现一次。"""

HYPOTHESIS_EXTRACT_INSTRUCTION = """你是「研究逻辑提炼员」。从下面的研究结论中抽取**待市场验证的推演/假设/情景/条件预测**（不是已经成立的事实）。

每条是一个可证伪的论断，例如：
- "情景A：军工板块脉冲式反弹（概率60%）"
- "若央行 7 月降息 10bp，则地产/银行股短期跑赢大盘"
- "如果 Q3 业绩同比 < 0%，则估值切换压制股价"

严格输出 JSON：{"items": [
  {
    "hypothesis": "（一句话陈述，可证伪）",
    "category": "情景 | 条件预测 | 时间窗口 | 反方观点 | 量化阈值",
    "probability": "可选，如 60% / 0.6 / 中等概率；无则空串",
    "scope": "涉及的标的/板块/指数，如 '军工板块' '600519' '沪深300'",
    "horizon": "验证时间窗口，如 '未来 5 个交易日' '2026Q3 财报' '1 个月内'",
    "check": "（一句话说明如何用市场数据验证，例如「观察 5 个交易日内板块累计涨跌幅 > 0%」「财报发布后 EPS 是否 > 0.5 元」）"
  }
]}

- 最多 5 条
- 若结论中没有可证伪的推演/假设/情景，返回 {"items": []}
- 不要把已发生的事实或数据陈述（如「今日银行涨 1.2%」）当成 hypothesis
- 严格只输出 JSON，不要其他内容。"""


def _evidence_digest(tool_trace: list[dict], max_chars: int = 3000) -> str:
    lines = []
    for t in tool_trace:
        if t.get("type") != "tool":
            continue
        args = ",".join(f"{k}={v}" for k, v in (t.get("args") or {}).items())
        status = "OK" if t.get("ok") else "FAIL"
        detail = str(t.get("result_excerpt") or "").strip()
        suffix = f"\n  DATA: {detail}" if detail else ""
        lines.append(f"[{t.get('agent')}] {t.get('skill')}({args}) [{status}] {t.get('preview')}{suffix}")
    text = "\n".join(lines)
    return text[:max_chars]


def _short_summary(text: str, max_chars: int = 120) -> str:
    """从 expert findings 中提取一句简短总结：取首段（首个 \\n\\n 之前），
    过长则按句号/换行截到 max_chars 之前，避免 agent_done 出现长 markdown。"""
    if not text:
        return ""
    first = text.split("\n\n", 1)[0].strip()
    # 去掉行内换行（让单段更紧凑）
    first = " ".join(first.splitlines()).strip()
    if len(first) <= max_chars:
        return first
    truncated = first[:max_chars]
    for sep in ["。", "？", "！", ".", "?", "!", ";", "；", "\n"]:
        idx = truncated.rfind(sep)
        if idx > max_chars // 2:
            return truncated[: idx + 1].strip() + ("…" if sep not in "。？！.?!；" else "")
    return truncated.rstrip() + "…"


def _inject_event_skill_defaults(
    name: str,
    args: dict,
    event_meta: dict | None,
) -> dict:
    """Fill deterministic event identity fields that the LLM occasionally omits.

    This does not add knowledge or future data; it only reuses metadata already
    supplied to the team.  It prevents invalid event-study calls with a date but
    no target symbol (571 failures in the 1000-sample trajectory).
    """
    normalized = dict(args or {})
    if not event_meta:
        return normalized
    if name == "frozen_announcement_fetch":
        defaults = {
            "source_url": event_meta.get("source_url"),
            "source_key": event_meta.get("source_key") or event_meta.get("source_notice_code"),
            "market": event_meta.get("market"),
            "symbol": event_meta.get("symbol"),
            "issuer_name": event_meta.get("issuer_name"),
            "event_date": event_meta.get("event_time") or event_meta.get("published_at"),
        }
        for key, value in defaults.items():
            if not normalized.get(key) and value:
                normalized[key] = str(value)[:10] if key == "event_date" else value
        return normalized
    if name != "event_study_skill":
        return normalized
    if not normalized.get("symbol") and not normalized.get("keyword"):
        symbol = str(event_meta.get("symbol") or "").strip()
        if symbol:
            normalized["symbol"] = symbol
    if not normalized.get("event_date"):
        event_date = str(event_meta.get("event_time") or "").strip()[:10]
        if event_date:
            normalized["event_date"] = event_date
    if not normalized.get("benchmark"):
        benchmark = str(event_meta.get("benchmark") or "").strip()
        if benchmark:
            normalized["benchmark"] = benchmark
    if not normalized.get("prediction_cutoff_at"):
        cutoff = str(event_meta.get("prediction_cutoff_at") or "").strip()
        if cutoff:
            normalized["prediction_cutoff_at"] = cutoff
    return normalized


def _seed_graph_from_findings(
    graph: EvidenceGraph,
    findings: dict[str, str],
    tool_trace: list[dict],
) -> int:
    """把前序专家产出预灌进 evidence graph，确保 deep_researcher 有图可继承。"""
    added = 0
    for aid, text in findings.items():
        if not text.strip():
            continue
        graph.add_evidence(
            source_kind="expert_finding",
            source_ref=f"{aid}.findings",
            title=f"{aid} 研究摘要",
            summary=text.strip()[:1600],
            raw={"agent": aid, "kind": "finding"},
        )
        added += 1
    seen: set[tuple[str, str, str]] = set()
    for t in tool_trace:
        if t.get("type") != "tool":
            continue
        agent = str(t.get("agent") or "").strip()
        skill = str(t.get("skill") or "").strip()
        preview = str(t.get("preview") or "").strip()
        detail = str(t.get("result_excerpt") or "").strip()
        if not agent or not skill or not preview:
            continue
        key = (agent, skill, preview[:120])
        if key in seen:
            continue
        seen.add(key)
        graph.add_evidence(
            source_kind="tool_trace",
            source_ref=f"{agent}.{skill}",
            title=f"{agent} · {skill}",
            summary=(preview + (f"\n结构化结果：{detail}" if detail else ""))[:2000],
            raw={
                "agent": agent,
                "skill": skill,
                "ok": bool(t.get("ok")),
                "args": t.get("args") or {},
                "result_excerpt": detail[:2000],
            },
        )
        added += 1
        if added >= 12:
            break
    return added


def _ensure_minimum_graph_structure(graph: EvidenceGraph, question: str) -> None:
    """Keep a tool-budget-limited graph honest and structurally reviewable.

    This fallback never upgrades evidence or invents a directional forecast. It
    creates one explicitly insufficient placeholder claim so loose evidence is
    not silently presented as a completed research argument.
    """
    counts = graph._counts()
    evidence_ids = [node.id for node in graph.nodes if node.kind == "evidence"]
    if counts["n_claim"] == 0:
        claim_id = graph.add_claim(
            f"推断：围绕“{question[:120]}”的未来路径仍需条件情景检验",
            rationale=(
                "研究轮次内只沉淀了背景证据，尚未形成可由这些证据直接验证的方向性结论；"
                "该节点是保守的结构占位，不是预测结果。"
            ),
            status="insufficient",
            confidence=0.0,
        )
        for evidence_id in evidence_ids[:3]:
            graph.link(
                claim_id,
                evidence_id,
                relation="context",
                note="仅作为情景背景，不构成方向性验证",
            )
    if graph._counts()["n_missing"] == 0:
        graph.add_missing(
            "可证伪的情景触发条件与反证数据",
            "当前图未在研究轮次内完成方向性 claim 的支持/反驳闭环，需由后续情景推演与市场数据补充。",
            priority=5,
        )


async def _run_expert_serial(
    agent_id: str,
    task_text: str,
    question: str,
    artifact_store: ArtifactStore,
    *,
    prior_findings: dict[str, str] | None = None,
    prior_tool_trace: list[dict] | None = None,
    research_context: ResearchContext | None = None,
    prompt_variant: str | None = None,
    graph: EvidenceGraph | None = None,
    export_graph: bool = True,
    agent_def: dict | None = None,
    max_rounds: int | None = None,
    external_skill_budget: int | None = None,
    extra_context: str = "",
    event_meta: dict | None = None,
    prior_conversation: str = "",
) -> AsyncIterator[dict]:
    """单 expert 串行执行：agent_start → events → agent_done 收尾。失败也收尾。

    对 deep_researcher：进入前 attach 一张证据图（ContextVar 持有）。默认创建
    并导出图谱；Evidence Navigator 可传入已有图并延迟导出，以便多个补证阶段
    共同修改同一张图，只落库最终快照。
    """
    yield {"type": "agent_step", "phase": "agent_start", "agent": agent_id, "note": task_text}
    expert_state: dict[str, Any] = {"content": "", "tool_trace": [], "rounds": 0}
    token = None
    graph_export_token = None
    if agent_id == "deep_researcher":
        if graph is None:
            graph = EvidenceGraph(question=question, scope=task_text[:200])
            _seed_graph_from_findings(graph, prior_findings or {}, prior_tool_trace or [])
        token = eg_attach(graph)
        graph_export_token = eg_defer_export_artifact(not export_graph)
    try:
        if agent_id == "predictor" and prior_findings:
            findings = (
                "深度研究证据图已完成交接。事件预测员在团队流程中只协调独立的后台多智能体推演，"
                "不在聊天阶段重复取数、等待任务或预先编造模拟结论。系统仅在证据图通过入口校验时"
                "自动启动；运行状态和最终情景卡片会显示在右侧产出物中。当前 quick 模式是单次"
                "情景发现，分支频率与置信度均不代表校准概率。"
            )
            yield {"type": "token", "agent": agent_id, "delta": findings}
            yield {"type": "agent_step", "phase": "agent_done", "agent": agent_id,
                   "note": _short_summary(findings)}
            yield {"type": "agent_findings", "agent": agent_id, "findings": findings,
                   "tool_trace": []}
            return
        evidence_context = ""
        if agent_id == "deep_researcher":
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
                    "当前 evidence graph 已经预载入前序专家的 findings / tool trace，"
                    "你必须优先围绕这张图工作：先补 claim / link / status / missing，再决定是否继续取数。\n\n"
                    + "\n\n".join(digest_blocks)
                )
            else:
                evidence_context = (
                    "\n\n【本轮没有前序专家交接】\n"
                    "禁止声称 event_scout / market_analyst / fundamentals_analyst 已经执行。"
                    "你需要自行获取最少量核心事实，并优先完成 claim、link、status 和 missing；"
                    "不要把全部轮次都消耗在重复取数上。"
                )
        elif agent_id == "predictor":
            digest_blocks = []
            for aid, txt in (prior_findings or {}).items():
                if txt.strip():
                    digest_blocks.append(f"【{aid} 研究交接】\n{txt[:800]}")
            if digest_blocks:
                evidence_context = (
                    "\n\n【证据图后的研究交接】\n"
                    "深度研究者已在上一阶段整理证据图。以下内容用于判断情景和协调后续推演；"
                    "不得把模拟判断写成已观察证据。\n\n"
                    + "\n\n".join(digest_blocks)
                )
        messages = [
            {"role": "system", "content": system_prompt(agent_id, prompt_variant)},
            {"role": "user", "content":
                f"【用户原始问题】{question}\n\n【你的子任务】{task_text}\n\n"
                "请调用你的技能获取真实数据后作答；最后用不超过600字总结发现（含关键数字+来源）。"
                + prior_conversation
                + evidence_context
                + (f"\n\n【Evidence Navigator 图谱上下文】\n{extra_context}" if extra_context else "")},
        ]
        external_skill_calls = 0

        async def team_skill_executor(name: str, args: dict) -> dict:
            nonlocal external_skill_calls
            args = _inject_event_skill_defaults(name, args, event_meta)
            if name != "evidence_graph" and external_skill_budget is not None:
                if external_skill_calls >= external_skill_budget:
                    return {
                        "ok": False,
                        "error": (
                            "Evidence Navigator 的本轮外部 Skill 预算已用完；"
                            "请基于已获得的数据写入图谱、建立关系并总结。"
                        ),
                    }
                external_skill_calls += 1
            if research_context is None:
                from ..llm import execute_skill
                return await execute_skill(name, args)
            from ..llm import execute_skill
            return await research_context.execute(execute_skill, name, args)

        resolved_agent_def = agent_def or get_agent(agent_id, prompt_variant)
        if agent_id == "deep_researcher" and prior_findings:
            # Source experts have already fetched the evidence and the graph is
            # pre-seeded above. Keeping all seven data skills available caused
            # the model to fetch the same tables again and run out of graph
            # construction rounds.
            resolved_agent_def = {**resolved_agent_def, "skills": ["evidence_graph"]}
        elif agent_id == "predictor" and prior_findings:
            # In team mode the predictor is a scenario synthesizer. The actual
            # multi-agent run starts asynchronously from the exported graph, so
            # repeating source data calls here only delays that handoff.
            resolved_agent_def = {**resolved_agent_def, "skills": []}
        async for ev in run_agent(agent_id, messages, agent_def=resolved_agent_def,
                                  state=expert_state, artifact_store=artifact_store,
                                  skill_executor=team_skill_executor,
                                  max_rounds=max_rounds or config.TEAM_MAX_ROUNDS):
            yield ev
        findings = expert_state["content"].strip()
        # deep_researcher 收尾：若 LLM 没显式 export，把当前图强制导出为图谱 artifact
        if agent_id == "deep_researcher" and graph is not None and export_graph:
            try:
                cur = get_current_graph()
                if cur is not None:
                    _ensure_minimum_graph_structure(cur, question)
                    payload = cur.to_payload()
                    row = await artifact_store("graph", "证据图", payload)
                    yield {"type": "artifact", "agent": agent_id, "artifact": row}
                    expert_state["tool_trace"].append({
                        "type": "tool", "agent": agent_id, "id": "evidence_graph_export",
                        "skill": "evidence_graph", "args": {"action": "export"},
                        "ok": True, "preview": f"导出图谱 {payload['stats']}",
                        "artifact_ids": [row.get("id")],
                    })
                    # 附加一段文字总结到 findings（前端能看到图谱已沉淀）
                    stats = payload["stats"]
                    tail = (f"\n\n**证据图已沉淀**：evidence {stats['n_evidence']} 条、"
                            f"claim {stats['n_claim']} 条（"
                            f"{'、'.join(f'{k} {v}' for k, v in stats['claim_status'].items()) or '无'}）、"
                            f"边 {stats['n_edges']} 条（supports {stats['n_supports']} / "
                            f"contradicts {stats['n_contradicts']}）"
                            f"{'，已标记充分' if payload['sufficient'] else '，尚未充分'}")
                    if not findings.endswith(tail):
                        findings = (findings + tail) if findings else tail.lstrip("\n")
                        # 同步流给前端（让用户看到导出动作）
                        yield {"type": "token", "agent": agent_id, "delta": tail}
                        expert_state["content"] += tail
            except Exception as e:  # noqa: BLE001
                yield {"type": "thinking", "agent": agent_id,
                       "delta": f"\n[deep_researcher 导出图谱失败: {type(e).__name__}: {e}]\n"}
        yield {"type": "agent_step", "phase": "agent_done", "agent": agent_id,
               "note": _short_summary(findings)}
        yield {"type": "agent_findings", "agent": agent_id, "findings": findings[:600],
               "tool_trace": expert_state["tool_trace"]}
    except Exception as e:  # noqa: BLE001
        findings = f"（{agent_id} 执行失败: {type(e).__name__}: {e}）"
        yield {"type": "agent_step", "phase": "agent_done", "agent": agent_id,
               "note": "执行失败"}
        yield {"type": "agent_findings", "agent": agent_id, "findings": findings,
               "tool_trace": expert_state["tool_trace"], "error": str(e)}
    finally:
        if graph_export_token is not None:
            try:
                eg_restore_export_artifact(graph_export_token)
            except Exception:  # noqa: BLE001
                pass
        if token is not None:
            try:
                eg_detach(token)
            except Exception:  # noqa: BLE001
                pass


# ============================================================ signal routing
# Tier 1/2 分析推理 skill 集成：Fan-out 后、Synthesize 前调用。

import re as _re

_TIER2_SKILLS = {"announcement_classifier", "ar_decomposer", "drift_context_analyzer"}
_TIER1_MAP = {
    ("CN", "并购/分拆/再融资"): "cn_ma_analyzer",
    ("CN", "财报超预期/不及预期"): "cn_earnings_analyzer",
    ("CN", "公司指引上调/下调"): "cn_ma_analyzer",   # 数据池里不少 M&A 合规公告被误标成「公司指引」，兜底走 MA 框架
    ("CN", "增减持"): "cn_ma_analyzer",              # 同属「公司行为」类，走 MA 框架（公告分类+漂移）
    ("US", "并购/分拆/再融资"): "us_ma_analyzer",
    ("US", "增减持/回购"): "us_ma_analyzer",        # 回购属公司行为，子类型映射会区分
}


def _extract_tier2_from_trace(tool_trace: list[dict]) -> dict:
    """从 tool_trace 中提取 Tier 2 skill 调用的 args 和 preview。"""
    results: dict[str, list[dict]] = {}
    for t in tool_trace:
        if not isinstance(t, dict):
            continue
        if t.get("type") != "tool":
            continue
        skill = t.get("skill") or t.get("name") or ""
        if skill not in _TIER2_SKILLS:
            continue
        results.setdefault(skill, []).append({
            "args": t.get("args") or {},
            "preview": t.get("preview") or "",
            "ok": t.get("ok", False),
        })
    return results


def _parse_pct_from_text(text: str, pattern: str) -> float | None:
    """从文本中用正则提取百分比数值。"""
    m = _re.search(pattern, text, _re.IGNORECASE)
    if not m:
        return None
    try:
        raw = m.group(1)
        val = float(raw)
        # 如果捕获组已含符号（+/-），直接用
        if raw.startswith(("+", "-")):
            return val
        # 否则从文本上下文推断符号
        full = m.group(0)
        if "跌" in full or "降" in full or "负" in full:
            return -val
        if "-" in full and "+" not in full:
            return -val
        return val
    except (ValueError, IndexError):
        return None


async def _route_signals(
    event_meta: dict | None,
    findings: dict[str, str],
    tool_trace: list[dict],
    *,
    fallback_text: str = "",
) -> dict | None:
    """信号路由：提取 Tier 2 信号 → 调用 Tier 1 analyzer → 返回 scorecard。

    返回 None 表示市场×类型未覆盖（不在 _TIER1_MAP），调用方 fallback 到纯 LLM。
    只要在 _TIER1_MAP 里，即使 signals 为空，也会基于 announcement_subtype prior + 弱规则
    调 analyzer（给出弱方向），避免路由覆盖率低。
    """
    if not event_meta:
        return None

    market = str(event_meta.get("market", "")).upper()
    event_type = str(event_meta.get("event_type_l2", ""))
    key = (market, event_type)
    analyzer_name = _TIER1_MAP.get(key)
    if not analyzer_name:
        return None

    # 从 tool_trace 提取 Tier 2 skill 调用结果
    tier2 = _extract_tier2_from_trace(tool_trace)

    # 合并 findings 文本；若 findings 空（如 LLM 连接错误）回退到 fallback_text
    all_text_parts = [v for v in findings.values() if v]
    if not all_text_parts and fallback_text:
        all_text_parts = [fallback_text]
    all_text = " ".join(all_text_parts)

    # 提取信号
    signals: dict[str, dict] = {}

    # 1. announcement_classifier 信号
    ann_subtype = ""
    ann_info_tier = ""
    ann_calls = tier2.get("announcement_classifier", [])
    if ann_calls:
        # 用最后一次调用的 args（可能有多次，取最后一次的）
        last_args = ann_calls[-1]["args"]
        ann_subtype = last_args.get("subtype", "")
        # 如果 args 里没有 subtype，从 preview 中尝试提取
        if not ann_subtype:
            preview = ann_calls[-1].get("preview", "")
            for st in ["report_draft", "compliance_reply", "first_disclosure",
                       "intermediary_opinion", "progress_update", "completion",
                       "termination", "us_rule_425", "us_8k_material",
                       "earnings_notice_meeting", "earnings_actual_report"]:
                if st in preview:
                    ann_subtype = st
                    break
    # 如果 subtype 仍旧空（无论是 LLM 没调用 / 调用了但 args 没 subtype / preview 提取失败），
    # 一律从 event_meta 提取 title/text 自己跑一遍 classifier 做 backfill，保证 cn_ma_analyzer 的
    # termination/compliance 强方向性信号至少有一次机会被注入。
    if not ann_subtype:
        title = str(event_meta.get("title", ""))
        if title:
            try:
                from ..skills.registry import REGISTRY
                if "announcement_classifier" in REGISTRY:
                    r = await REGISTRY["announcement_classifier"].handler(
                        title=title,
                        text=str(event_meta.get("event_text", ""))[:2000],
                        market=market,
                    )
                    if r.get("ok"):
                        ann_subtype = r["data"].get("subtype", "")
                        ann_info_tier = r["data"].get("info_tier", "")
            except Exception:
                pass

    # 2. ar_decomposer 信号
    ar_calls = tier2.get("ar_decomposer", [])
    if ar_calls:
        last_args = ar_calls[-1]["args"]
        stock_ret = last_args.get("stock_return_pct")
        bench_ret = last_args.get("benchmark_return_pct")
        if stock_ret is not None and bench_ret is not None:
            try:
                from ..skills.registry import REGISTRY
                if "ar_decomposer" in REGISTRY:
                    r = await REGISTRY["ar_decomposer"].handler(
                        stock_return_pct=float(stock_ret),
                        benchmark_return_pct=float(bench_ret),
                    )
                    if r.get("ok"):
                        d = r["data"]
                        if d.get("signal_valid"):
                            relative_return = float(
                                d.get("relative_return_pct", d.get("ar_pct", 0.0))
                            )
                            signals["t0_active_return"] = {
                                "direction": d.get("signal_direction") or d.get("active_direction", "neutral"),
                                "strength": min(3, abs(relative_return) / 1.5),
                            }
            except Exception:
                pass
    else:
        # 从 findings 文本提取 T0 个股/基准涨跌
        stock_ret = _parse_pct_from_text(all_text, r"个股.*?[涨跌]([0-9.]+)%")
        bench_ret = _parse_pct_from_text(all_text, r"基准.*?[涨跌]([0-9.]+)%")
        if stock_ret is not None and bench_ret is not None:
            try:
                from ..skills.registry import REGISTRY
                if "ar_decomposer" in REGISTRY:
                    r = await REGISTRY["ar_decomposer"].handler(
                        stock_return_pct=stock_ret,
                        benchmark_return_pct=bench_ret,
                    )
                    if r.get("ok") and r["data"].get("signal_valid"):
                        d = r["data"]
                        relative_return = float(
                            d.get("relative_return_pct", d.get("ar_pct", 0.0))
                        )
                        signals["t0_active_return"] = {
                            "direction": d.get("signal_direction") or d.get("active_direction", "neutral"),
                            "strength": min(3, abs(relative_return) / 1.5),
                        }
            except Exception:
                pass

    # 3. drift_context_analyzer 信号
    drift_calls = tier2.get("drift_context_analyzer", [])
    if drift_calls:
        last_args = drift_calls[-1]["args"]
        pre5 = last_args.get("pre5_pct")
        pre20 = last_args.get("pre20_pct")
        # 尝试从 args 或 findings 文本提取更多 horizons（pre1/pre10/pre66 为新增可选参数）
        pre1 = last_args.get("pre1_pct")
        pre10 = last_args.get("pre10_pct")
        pre66 = last_args.get("pre66_pct")
        if pre1 is None:
            pre1 = _parse_pct_from_text(all_text, r"pre1[^0-9]?.*?([+-]?[0-9.]+)%")
        if pre10 is None:
            pre10 = _parse_pct_from_text(all_text, r"pre10.*?([+-]?[0-9.]+)%")
        if pre66 is None:
            pre66 = _parse_pct_from_text(all_text, r"pre66.*?([+-]?[0-9.]+)%")
        if pre5 is not None and pre20 is not None:
            try:
                from ..skills.registry import REGISTRY
                if "drift_context_analyzer" in REGISTRY:
                    kw = dict(pre5_pct=float(pre5), pre20_pct=float(pre20))
                    if pre1 is not None: kw["pre1_pct"] = float(pre1)
                    if pre10 is not None: kw["pre10_pct"] = float(pre10)
                    if pre66 is not None: kw["pre66_pct"] = float(pre66)
                    r = await REGISTRY["drift_context_analyzer"].handler(**kw)
                    if r.get("ok"):
                        d = r["data"]
                        if d.get("drift_score", 0) >= 2:
                            base_str = int(d.get("drift_score", 1))
                            # 一致性 / 单调放大：多窗口同向漂移 → 趋势更可信
                            consistency = d.get("horizon_consistency") or 0.0
                            if isinstance(consistency, (int, float)) and consistency >= 0.8:
                                base_str += 1
                            if d.get("drift_monotonic"):
                                base_str += 1
                            signals["pre_drift"] = {
                                "direction": d.get("direction_hint", "neutral"),
                                "strength": min(4, base_str),
                                "consistency": consistency,
                                "monotonic": bool(d.get("drift_monotonic")),
                                "long_term": d.get("long_term_persistence_direction"),
                            }
                        if d.get("sell_the_news_triggered"):
                            coeff = float(d.get("sell_the_news_coefficient", 0) or 0)
                            s_str = min(4, int(round(coeff)))
                            signals["sell_the_news"] = {
                                "direction": d.get("sell_the_news_direction", "neutral"),
                                "strength": s_str,
                                "coefficient": round(coeff, 3),
                            }
            except Exception:
                pass
    else:
        # 从 findings 文本提取 pre5/pre20 + 扩展 horizons
        pre5 = _parse_pct_from_text(all_text, r"pre5.*?([+-]?[0-9.]+)%")
        pre20 = _parse_pct_from_text(all_text, r"pre20.*?([+-]?[0-9.]+)%")
        pre1 = _parse_pct_from_text(all_text, r"pre1[^0-9]?.*?([+-]?[0-9.]+)%")
        pre10 = _parse_pct_from_text(all_text, r"pre10.*?([+-]?[0-9.]+)%")
        pre66 = _parse_pct_from_text(all_text, r"pre66.*?([+-]?[0-9.]+)%")
        if pre5 is not None and pre20 is not None:
            try:
                from ..skills.registry import REGISTRY
                if "drift_context_analyzer" in REGISTRY:
                    kw = dict(pre5_pct=pre5, pre20_pct=pre20)
                    if pre1 is not None: kw["pre1_pct"] = float(pre1)
                    if pre10 is not None: kw["pre10_pct"] = float(pre10)
                    if pre66 is not None: kw["pre66_pct"] = float(pre66)
                    r = await REGISTRY["drift_context_analyzer"].handler(**kw)
                    if r.get("ok"):
                        d = r["data"]
                        if d.get("drift_score", 0) >= 2:
                            base_str = int(d.get("drift_score", 1))
                            consistency = d.get("horizon_consistency") or 0.0
                            if isinstance(consistency, (int, float)) and consistency >= 0.8:
                                base_str += 1
                            if d.get("drift_monotonic"):
                                base_str += 1
                            signals["pre_drift"] = {
                                "direction": d.get("direction_hint", "neutral"),
                                "strength": min(4, base_str),
                                "consistency": consistency,
                                "monotonic": bool(d.get("drift_monotonic")),
                                "long_term": d.get("long_term_persistence_direction"),
                            }
                        if d.get("sell_the_news_triggered"):
                            coeff = float(d.get("sell_the_news_coefficient", 0) or 0)
                            signals["sell_the_news"] = {
                                "direction": d.get("sell_the_news_direction", "neutral"),
                                "strength": min(4, int(round(coeff))),
                                "coefficient": round(coeff, 3),
                            }
            except Exception:
                pass

    # 4. 基本面信号（从 findings 文本提取）
    if "亏损" in all_text or "净利.*?负" in all_text:
        signals["fundamentals"] = {"direction": "down", "strength": 2}
    elif "扭亏" in all_text:
        signals["fundamentals"] = {"direction": "up", "strength": 3}
    yoy_match = _re.search(r"同比.*?([+-]?\d+(?:\.\d+)?)\s*%", all_text)
    if yoy_match and "fundamentals" not in signals:
        try:
            yoy = float(yoy_match.group(1))
            if yoy >= 50:
                signals["fundamentals"] = {"direction": "up", "strength": 3}
            elif yoy > 0:
                signals["fundamentals"] = {"direction": "up", "strength": 1}
            elif yoy < -20:
                signals["fundamentals"] = {"direction": "down", "strength": 3}
            else:
                signals["fundamentals"] = {"direction": "down", "strength": 1}
        except ValueError:
            pass

    # 5. 公告文本信号（从 findings 提取简单语义）
    if "利好" in all_text and "利空" not in all_text:
        signals.setdefault("announcement_text", {"direction": "up", "strength": 1})
    elif "利空" in all_text and "利好" not in all_text:
        signals.setdefault("announcement_text", {"direction": "down", "strength": 1})

    # 兜底：哪怕 signals 空，只要命中了 _TIER1_MAP 就走 analyzer。
    # analyzer 内部会基于 announcement_subtype 给一个弱先验（progress_update→neutral,
    # report_draft→down 等），这已经比让 LLM 瞎猜强。signals 为空时 analyzer 仍会
    # 根据 subtype prior / info_tier 出弱方向。
    #
    # 调用 Tier 1 analyzer
    try:
        import inspect as _inspect
        from ..skills.registry import REGISTRY
        if analyzer_name not in REGISTRY:
            return None
        handler = REGISTRY[analyzer_name].handler
        # 只传 handler 真正接受的参数（不同 analyzer 签名不同，避免 TypeError 被静默吞掉）
        sig_params = set(_inspect.signature(handler).parameters.keys())
        kwargs: dict = {"signals": signals}
        if "announcement_subtype" in sig_params and ann_subtype:
            kwargs["announcement_subtype"] = ann_subtype
        if "announcement_info_tier" in sig_params and ann_info_tier:
            kwargs["announcement_info_tier"] = ann_info_tier
        r = await handler(**kwargs)
        if r.get("ok"):
            r["data"]["signals_extracted"] = list(signals.keys())
            return r["data"]
    except Exception:
        pass

    return None


async def run_team(
    question: str,
    history: list[dict],
    state: dict,
    artifact_store: ArtifactStore,
    team_members: list[str] | None = None,
    skip_hypothesis: bool = False,
    skip_verify: bool = False,
    event_meta: dict | None = None,
    agent_prompt_variants: dict[str, str] | None = None,
) -> AsyncIterator[dict]:
    """Yield SSE events for the whole team-mode flow. state['content'] = final answer.

    team_members: 前端可选的专家白名单（仅 EXPERT_IDS 内的子集生效）。
                  None / 空 = 全部可调度。deep_researcher 是硬规则（不剔除）。
    skip_hypothesis: 跳过 hypothesis 抽取（1 次 complete_json + 0 事实输出，回测场景下 hypothesis 不参与 ACC）。
    skip_verify: 跳过 verifier 复核（1 次 complete_json + 可能 1 次 router fix，用于纯 speed 场景）。
    event_meta: 事件元信息（market, event_type_l2, symbol, benchmark 等），
                用于 Synthesize 阶段路由到对应 Tier 1 analyzer skill。
    agent_prompt_variants: 按 agent_id 指定 persona 变体；回测 A/B 当前用于 deep_researcher。
    """
    # ------------------------------------------------------------ 1) plan --
    # 构建历史上下文摘要（最近 3 轮），让 router 知道之前聊了什么
    history_ctx = ""
    if history:
        recent = [m for m in history[-6:] if (m.get("content") or "").strip()]
        if recent:
            history_ctx = "\n\n【之前的对话记录（供参考，不要重复已有结论，聚焦用户当前追问）】\n" + "\n".join(
                f"{m['role']}: {m['content'][:300]}" for m in recent
            )

    plan: list[dict] = []
    try:
        yield {"type": "thinking", "agent": "router",
               "delta": "正在拆解研究任务，规划专家分工…"}
        plan_json = await complete_json(
            system_prompt("router") + "\n\n" + PLANNER_INSTRUCTION,
            f"用户问题：{question}{history_ctx}",
            max_tokens=2000,
        )
        if plan_json:
            for t in plan_json.get("tasks", []):
                aid = str(t.get("agent", "")).strip()
                task_text = str(t.get("task", "")).strip()
                if aid in EXPERT_IDS and task_text and all(p["agent"] != aid for p in plan):
                    plan.append({"agent": aid, "task": task_text})
    except Exception:  # noqa: BLE001
        plan = []
    if not plan:  # fallback：行情+基本面双视角 + deep_researcher 必派
        plan = [
            {"agent": "market_analyst", "task": f"围绕「{question}」分析行情、资金与关键事件的价格反应"},
            {"agent": "fundamentals_analyst", "task": f"围绕「{question}」分析基本面、财务与机构观点"},
            {"agent": "deep_researcher", "task":
             f"把 market_analyst / fundamentals_analyst 的发现沉淀到证据图：每条关键数字作为 evidence，"
             f"每条可证伪推断作为 claim，标 supports/contradicts 关系，最后 export 证据图"},
        ]
    # Planner occasionally violates the hard rule despite the prompt. Enforce
    # it in orchestration so a selected predictor can never run without the
    # evidence-graph stage merely because of one malformed plan.
    if not any(p["agent"] == "deep_researcher" for p in plan):
        plan.append({
            "agent": "deep_researcher",
            "task": (
                f"把本轮其他专家围绕「{question}」的发现沉淀到证据图："
                "关键事实作为 evidence，可证伪推断作为 claim，标注 "
                "supports/contradicts、研究缺口与状态，最后 export 证据图"
            ),
        })

    # 应用 team_members 白名单：
    # 1) 剔除未勾选的专家（deep_researcher 永不被剔除，硬规则）
    # 2) 若剔除后没有任何 deep_researcher 之外的任务则保留 deep_researcher 单跑
    # 3) 若剔除后 plan 为空，退化为仅 deep_researcher
    if team_members is not None:
        allow = set(team_members) | {"deep_researcher"}
        before = [p["agent"] for p in plan]
        plan = [p for p in plan if p["agent"] in allow]
        removed = set(before) - set(p["agent"] for p in plan)
        if removed:
            yield {"type": "agent_step", "phase": "plan_filter",
                   "note": f"已按 team_members 筛选：剔除 {sorted(removed)}（剩余 {len(plan)} 个子任务）"}
        if not plan:
            plan = [{"agent": "deep_researcher", "task":
                     f"围绕「{question}」直接沉淀到证据图：每条关键数字作为 evidence，"
                     f"每条可证伪推断作为 claim，最后 export"}]
    # Source-oriented experts run first, graph synthesis follows, and predictor
    # receives that handoff last. This prevents simulated forecasts from being
    # seeded back into the evidence graph as if they were observations.
    source_tasks = [
        p for p in plan
        if p["agent"] not in {"deep_researcher", "predictor"}
    ][:3]
    graph_task = next(p for p in plan if p["agent"] == "deep_researcher")
    predictor_task = next(
        (p for p in plan if p["agent"] == "predictor"), None
    )
    plan = source_tasks + [graph_task]
    if predictor_task is not None:
        plan.append(predictor_task)
    plan_public = [{**p, "agent_name": AGENTS[p["agent"]]["name"]} for p in plan]
    # The chat orchestrator uses the final filtered plan as the main agent's
    # explicit decision about whether an asynchronous predictor run is useful.
    state["team_plan"] = plan_public
    yield {"type": "agent_step", "phase": "plan", "note": f"拆解为 {len(plan)} 个子任务",
           "plan": plan_public}
    state["tool_trace"].append({"type": "plan", "plan": plan_public})

    # -------------------------------------------------------- 2) serial fan
    findings: dict[str, str] = {}
    research_context = ResearchContext()
    team_graph: EvidenceGraph | None = None
    total_experts = len(plan)
    for ei, p in enumerate(plan):
        yield {"type": "thinking", "agent": "router",
               "delta": f"正在调度第 {ei+1}/{total_experts} 位专家：{AGENTS.get(p['agent'], {}).get('name', p['agent'])}…"}
        is_deep_researcher = p["agent"] == "deep_researcher"
        if is_deep_researcher:
            # Keep this graph alive through the Navigator's verification pass.
            # Earlier experts have already finished, so their findings can be
            # seeded once before the first graph-aware researcher starts.
            team_graph = EvidenceGraph(question=question, scope=p["task"][:200])
            _seed_graph_from_findings(team_graph, findings, state["tool_trace"])
        async for ev in _run_expert_serial(
            p["agent"], p["task"], question, artifact_store,
            prior_findings=(
                findings
                if p["agent"] in {"deep_researcher", "predictor"}
                else None
            ),
            prior_tool_trace=(
                state["tool_trace"]
                if p["agent"] in {"deep_researcher", "predictor"}
                else None
            ),
            research_context=research_context,
            event_meta=event_meta,
            prompt_variant=(agent_prompt_variants or {}).get(p["agent"]),
            graph=team_graph if is_deep_researcher else None,
            export_graph=not is_deep_researcher,
            prior_conversation=history_ctx,
        ):
            # 提取 agent_findings 写入 state + findings；其余原样 yield
            if ev.get("type") == "agent_findings":
                aid = ev["agent"]
                findings[aid] = ev.get("findings", "")
                if ev.get("tool_trace"):
                    state["tool_trace"].extend(ev["tool_trace"])
                # 不把 agent_findings 推给前端（前端用 agent_step agent_done 已经知道）
                continue
            yield ev

    # ------------------------------------------------- 2.5) navigate gaps --
    # Argus-style loop, deliberately bounded in this first implementation:
    # observe the shared graph → dispatch one targeted follow-up → observe
    # again.  No free-form retries and no unbounded fan-out.
    if (team_graph is not None and config.EVIDENCE_NAVIGATOR_ENABLED
            and config.EVIDENCE_NAVIGATOR_MAX_ROUNDS > 0):
        navigator = EvidenceNavigator(
            max_dispatches=config.EVIDENCE_NAVIGATOR_MAX_DISPATCHES,
        )
        for nav_round in range(1, config.EVIDENCE_NAVIGATOR_MAX_ROUNDS + 1):
            decision = navigator.plan(team_graph)
            state["tool_trace"].append({
                "type": "evidence_navigator",
                "round": nav_round,
                **decision.to_dict(),
            })
            yield {
                "type": "agent_step",
                "phase": "evidence_navigator",
                "agent": "evidence_navigator",
                "note": f"第 {nav_round} 轮：{decision.reason}",
            }
            if decision.action != "dispatch":
                break

            graph_before = (len(team_graph.nodes), len(team_graph.edges))
            for dispatch in decision.dispatches:
                base_def = get_agent(
                    "deep_researcher",
                    (agent_prompt_variants or {}).get("deep_researcher"),
                )
                followup_def = {
                    **(base_def or {}),
                    "skills": list(dispatch.allowed_skills),
                }
                graph_context = navigator.compact_context(team_graph, max_chars=5000)
                async for ev in _run_expert_serial(
                    "deep_researcher",
                    dispatch.task,
                    question,
                    artifact_store,
                    research_context=research_context,
                    prompt_variant=(agent_prompt_variants or {}).get("deep_researcher"),
                    graph=team_graph,
                    export_graph=False,
                    agent_def=followup_def,
                    max_rounds=config.EVIDENCE_NAVIGATOR_FOLLOWUP_MAX_ROUNDS,
                    external_skill_budget=config.EVIDENCE_NAVIGATOR_EXTERNAL_SKILL_BUDGET,
                    extra_context=graph_context,
                    event_meta=event_meta,
                ):
                    if ev.get("type") == "agent_findings":
                        followup = str(ev.get("findings") or "").strip()
                        if followup:
                            previous = findings.get("deep_researcher", "").strip()
                            findings["deep_researcher"] = (
                                f"{previous}\n\n【Evidence Navigator 补证】\n{followup}".strip()
                            )
                        if ev.get("tool_trace"):
                            state["tool_trace"].extend(ev["tool_trace"])
                        continue
                    yield ev
            graph_after = (len(team_graph.nodes), len(team_graph.edges))
            if graph_after == graph_before:
                state["tool_trace"].append({
                    "type": "evidence_navigator",
                    "round": nav_round,
                    "action": "stop",
                    "reason": "定向补证未形成新的图节点或关系，停止以避免无效循环",
                })
                yield {
                    "type": "agent_step",
                    "phase": "evidence_navigator",
                    "agent": "evidence_navigator",
                    "note": "定向补证未更新图谱，停止后续循环",
                }
                break

    # Persist one final graph snapshot after all graph editing. Intermediate
    # `export` calls are deferred while the Navigator is active.
    if team_graph is not None:
        try:
            payload = team_graph.to_payload()
            row = await artifact_store("graph", "证据图", payload)
            yield {"type": "artifact", "agent": "deep_researcher", "artifact": row}
            state["tool_trace"].append({
                "type": "tool", "agent": "deep_researcher", "id": "evidence_graph_export_final",
                "skill": "evidence_graph", "args": {"action": "export", "final": True},
                "ok": True, "preview": f"导出最终图谱 {payload['stats']}",
                "artifact_ids": [row.get("id")],
            })
            stats = payload["stats"]
            tail = (f"\n\n**证据图已沉淀**：evidence {stats['n_evidence']} 条、"
                    f"claim {stats['n_claim']} 条、边 {stats['n_edges']} 条"
                    f"（supports {stats['n_supports']} / contradicts {stats['n_contradicts']}）"
                    f"；Navigator 审计发现 {payload['audit']['summary']['total_findings']} 项待处理。")
            findings["deep_researcher"] = (findings.get("deep_researcher", "") + tail).strip()
        except Exception as e:  # noqa: BLE001
            yield {"type": "thinking", "agent": "deep_researcher",
                   "delta": f"\n[最终证据图导出失败: {type(e).__name__}: {e}]\n"}

    # ---------------------------------------------------------- 3) synthesize
    # 信号路由：调用 Tier 1 analyzer 产出结构化 scorecard，注入 synthesize prompt
    analyzer_scorecard: dict | None = None
    if event_meta:
        try:
            # 构造 fallback_text：专家 findings 空时，用 question + as_of_packet 原文兜底
            fb_parts = []
            if "content_full" in state and isinstance(state["content_full"], str):
                fb_parts.append(state["content_full"][:4000])
            fb_text = "\n".join(fb_parts)
            analyzer_scorecard = await _route_signals(
                event_meta, findings, state["tool_trace"],
                fallback_text=fb_text,
            )
            if analyzer_scorecard:
                yield {"type": "agent_step", "phase": "signal_routing",
                       "note": (f"Tier 1 analyzer={analyzer_scorecard.get('analyzer','?')} "
                                f"→ dir={analyzer_scorecard.get('direction','?')} "
                                f"conf={analyzer_scorecard.get('confidence','?')} "
                                f"net={analyzer_scorecard.get('net_score','?')}")}
                state["tool_trace"].append({
                    "type": "signal_routing", "analyzer": analyzer_scorecard.get("analyzer"),
                    "direction": analyzer_scorecard.get("direction"),
                    "confidence": analyzer_scorecard.get("confidence"),
                    "net_score": analyzer_scorecard.get("net_score"),
                    "signals": analyzer_scorecard.get("signals_extracted"),
                })
        except Exception:  # noqa: BLE001
            pass

    digest = "\n\n".join(
        f"【{AGENTS[aid]['name']}({aid}) 发现】\n{txt or '（无有效产出）'}"
        for aid, txt in findings.items()
    )

    # 构造 synthesize prompt（注入 analyzer scorecard 作为结构化参考）
    analyzer_context = ""
    if analyzer_scorecard:
        sc = analyzer_scorecard
        signal_lines = []
        for s in (sc.get("signal_detail") or [])[:6]:
            signal_lines.append(
                f"  · {s['signal']}: 方向={s['direction']}, 强度={s['strength']}, "
                f"权重={s['weight']}, 贡献={s['contribution']:+.1f}"
            )
        analyzer_context = (
            f"\n\n【结构化信号分析（Tier 1 analyzer 自动产出，供参考）】"
            f"\n分析器：{sc.get('analyzer','?')}（{sc.get('market','?')} {sc.get('event_type','?')}）"
            f"\n自动判断：方向={sc.get('direction','?')}, 置信度={sc.get('confidence','?')}, 净分={sc.get('net_score','?')}"
            f"\n信号明细：\n" + "\n".join(signal_lines) +
            "\n\n【指导】以上结构化分析基于 Tier 2 信号处理 skill（AR分解/漂移出尽系数/公告分类）自动计算。"
            "请将其作为重要参考——如果分析师团队发现中的信号与结构化分析一致，提高 confidence；"
            "如果不一致，请仔细检查是否遗漏了关键信号（如出尽效应、被动AR等）。"
            "结构化分析的净分方向应有较高权重。"
            "\n【neutral 约束（必须遵守）】 neutral 仅用于 |T+3 CAR|<50bps 的纯噪声事件（即方向完全随机）。"
            "在以下情况才允许判 neutral：①结构化分析的 |净分|<1.0 且无任何一条信号强度≥2；②存在 ≥2 条方向相反且力度相当的矛盾证据；③关键价格数据/公告数值完全缺失。"
            "否则必须在 up / down 中给出明确方向，不要用 neutral 逃避判断。"
        )

    synth_messages = [{"role": "system", "content": system_prompt("router")}]
    synth_messages.extend(history)
    synth_messages.append({
        "role": "user",
        "content": (
            f"【用户问题】{question}\n\n"
            f"【专家团队发现】\n{digest}"
            f"{analyzer_context}\n\n"
            "请综合以上专家发现，给出结构化的最终回答（先结论后依据，标注来源与推断）。"
            "【重要】不要调用任何工具！专家已经查过所有数据。你必须直接输出文字回答，不要 function/tool call。"
            "【neutral 约束（必须遵守）】 neutral 仅用于以下严格条件："
            "(a) T+3 方向信号极弱（|CAR|预计<50bps，方向纯噪声）；"
            "(b) 存在 ≥2 条方向相反且力度相当的矛盾证据；"
            "(c) 关键价格数据/公告数值完全缺失无法判断。"
            "否则必须在 up / down 中给出明确方向，不要用 neutral 逃避判断。"
            "输出格式要求（与 parser 对齐，必须严格包含中文标签行）：\n"
            "【最终方向】 up / down / neutral 三选一\n"
            "【置信度】 0.xx（0.50-0.99）\n"
            "【中文理由】 不超过 300 字，列举 2-4 条关键支撑信号。"
            "专家文字中没有对应工具证据的数字不得继承为事实。事件预测员的后台推演尚未作为"
            "本轮聊天输入返回，不得声称已经看到模拟结果；quick 单次推演不得输出百分比概率，"
            "只能使用高/中/低等未校准的相对倾向。"
        ),
    })
    # synthesize 阶段：禁用所有 skill，强制纯文字总结，避免 deepseek-v4-flash 发起无意义 tool call 导致 max_rounds 耗尽 content 为空
    synth_agent_def = {**get_agent("router"), "skills": []}
    async for ev in run_agent("router", synth_messages, agent_def=synth_agent_def,
                              state=state, artifact_store=artifact_store,
                              max_rounds=2):
        yield ev

    state["tool_trace"].append({"type": "research_context", **research_context.stats()})

    # ------------------------------------------------------------- 4) verify
    draft = state["content"].strip()
    if draft and not skip_verify:
        try:
            evidence = _evidence_digest(state["tool_trace"])
            yield {"type": "thinking", "agent": "verifier",
                   "delta": "正在复核研究结论的事实性与逻辑一致性…"}
            verdict_json = await complete_json(
                system_prompt("verifier"),
                f"【分析草稿】\n{draft[:4000]}\n\n【证据摘要（工具调用记录）】\n{evidence}",
                max_tokens=3000,
            )
        except Exception:  # noqa: BLE001
            verdict_json = None
        verdict = "pass"
        issues: list[str] = []
        corrected = ""
        if verdict_json:
            verdict = str(verdict_json.get("verdict") or "pass")
            issues = [str(i) for i in (verdict_json.get("issues") or [])][:5]
            corrected = str(verdict_json.get("corrected") or "")
        note = "；".join(issues)[:300] if issues else "未发现事实性错误"
        yield {"type": "agent_step", "phase": "verified", "agent": "verifier",
               "note": f"verdict={verdict} · {note}"}
        state["tool_trace"].append({"type": "verify", "verdict": verdict,
                                    "issues": issues, "corrected": corrected[:1000]})
        if issues:
            fix_messages = [
                {"role": "system", "content": system_prompt("router")},
                {"role": "user", "content": (
                    f"【你的草稿】\n{draft[:3000]}\n\n"
                    f"【复核员意见】\n问题：{note}\n修正建议：{corrected[:1500]}\n\n"
                    "请直接输出修正后的关键内容（先一句话承认并更正问题，再给出修正后的关键段落）。"
                    "不要重复外层已注入的「## 复核修正」标题；开头不要以 # 标题开头，"
                    "直接以陈述句或「经核实…」之类的过渡句起笔即可。")},
            ]
            fix_state: dict[str, Any] = {"content": "", "tool_trace": state["tool_trace"]}
            header = "\n\n## 复核修正\n"
            state["content"] += header
            yield {"type": "token", "agent": "router", "delta": header}
            async for ev in run_agent("router", fix_messages,
                                      agent_def={**get_agent("router"), "skills": []},
                                      state=fix_state, artifact_store=artifact_store,
                                      max_rounds=1, emit_thinking=False):
                if ev.get("type") == "token":
                    delta = ev["delta"]
                    # 兜底：若 LLM 仍以 ## 复核修正 开头，剥掉首个重复标题
                    if not fix_state["content"] and delta.lstrip().startswith("#"):
                        lines = delta.lstrip().split("\n", 1)
                        first = lines[0].strip().lower()
                        if "复核修正" in first or first.startswith("#"):
                            delta = lines[1] if len(lines) > 1 else ""
                            if delta:
                                delta = "\n" + delta
                    state["content"] += delta
                yield ev

    # -------------------------------------------------- 5) extract hypotheses
    import time as _t
    final_answer = state["content"].strip()
    if final_answer and not skip_hypothesis:
        try:
            yield {"type": "thinking", "agent": "router",
                   "delta": "正在提炼可证伪的研究假设…"}
            extracted = await complete_json(
                system_prompt("router") + "\n\n" + HYPOTHESIS_EXTRACT_INSTRUCTION,
                f"用户原始问题：{question}\n\n【研究结论】\n{final_answer[:3500]}",
                max_tokens=2000,
            )
        except Exception:  # noqa: BLE001
            extracted = None
        items: list[dict] = []
        if extracted:
            for j, it in enumerate((extracted.get("items") or [])[:5]):
                h = str(it.get("hypothesis") or "").strip()
                if not h:
                    continue
                items.append({
                    "id": f"h{int(_t.time() * 1000) % 1_000_000}_{j}",
                    "hypothesis": h[:300],
                    "category": str(it.get("category") or "").strip()[:30],
                    "probability": str(it.get("probability") or "").strip()[:20],
                    "scope": str(it.get("scope") or "").strip()[:80],
                    "horizon": str(it.get("horizon") or "").strip()[:50],
                    "check": str(it.get("check") or "").strip()[:200],
                })
        if items:
            yield {"type": "logic_items", "items": items}
            state["tool_trace"].append({"type": "logic_items", "count": len(items),
                                        "items": items})
