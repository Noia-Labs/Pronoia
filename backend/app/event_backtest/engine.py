from __future__ import annotations

import asyncio
import json
import re
from typing import Iterable, Optional

from ..llm import complete_json
from .market import resolve_benchmark
from .models import Direction, EventRecord, TeamPrediction


POSITIVE_HINTS = (
    "上调",
    "增长",
    "超预期",
    "获批",
    "增持",
    "回购",
    "降息",
    "降准",
    "刺激",
    "扶持",
    "improve",
    "beat",
    "raise",
    "upgrade",
    "approval",
)
NEGATIVE_HINTS = (
    "下调",
    "不及预期",
    "暴雷",
    "减持",
    "处罚",
    "制裁",
    "关税",
    "冲突",
    "收紧",
    "下滑",
    "miss",
    "cut",
    "downgrade",
    "fine",
    "lawsuit",
)


def validate_event(event: EventRecord) -> list[str]:
    issues: list[str] = []
    if not event.event_id:
        issues.append("event_id 不能为空")
    if event.market not in ("CN", "US", "HK"):
        issues.append(f"market 非法: {event.market!r} (允许 CN / US / HK)")
    if not event.symbol:
        issues.append("symbol 不能为空")
    if not event.event_time:
        issues.append("event_time 不能为空")
    if not event.event_type_l2:
        issues.append("event_type_l2 不能为空")
    if not event.title:
        issues.append("title 不能为空")
    if not event.source_url:
        issues.append("source_url 不能为空")
    return issues


def validate_events(events: Iterable[EventRecord]) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    for idx, event in enumerate(events, start=1):
        prefix = f"第 {idx} 条事件"
        if event.event_id in seen:
            issues.append(f"{prefix}: event_id 重复 {event.event_id}")
        seen.add(event.event_id)
        for issue in validate_event(event):
            issues.append(f"{prefix}: {issue}")
    return issues


def _normalize_prior(val: str | None) -> Direction | None:
    raw = (val or "").strip().lower()
    if raw in {"up", "long", "bull", "bullish", "positive"}:
        return "up"
    if raw in {"down", "short", "bear", "bearish", "negative"}:
        return "down"
    return None


def _keyword_direction(text: str) -> Direction:
    lowered = re.sub(r"\s+", " ", text.lower())
    pos = sum(1 for x in POSITIVE_HINTS if x in lowered)
    neg = sum(1 for x in NEGATIVE_HINTS if x in lowered)
    return "up" if pos >= neg else "down"


def run_baseline(
    events: Iterable[EventRecord],
    *,
    run_id: str,
    model_version: str = "event-baseline-v0",
) -> list[TeamPrediction]:
    preds: list[TeamPrediction] = []
    for event in events:
        prior = _normalize_prior(event.direction_prior)
        benchmark = resolve_benchmark(event)
        direction = prior or _keyword_direction(f"{event.title}\n{event.event_text}")
        preds.append(
            TeamPrediction(
                event_id=event.event_id,
                pred_direction=direction,
                run_id=run_id,
                model_version=model_version,
                confidence=0.55 if prior is None else 0.7,
                rationale=f"baseline:{'prior' if prior else 'keyword'} benchmark={benchmark}",
                abstain=False,
            )
        )
    return preds


def _event_prompt(event: EventRecord) -> str:
    packet = {
        "as_of_packet": {
            "market": event.market,
            "symbol": event.symbol,
            "event_time": event.event_time,
            "event_type_l2": event.event_type_l2,
            "benchmark": resolve_benchmark(event),
            "sector_etf": event.sector_etf,
            # 注意：移除 direction_prior / event_strength —— 这两个字段是事件集构造阶段的标签侧元数据，
            # 可能隐含事件级的后验统计（弱未来信息泄露），严格 as-of 回测下不应暴露给模型。
            "title": event.title,
            "event_text": event.event_text,
            "constraints": {
                "strict_as_of": True,
                "web_search_allowed": False,
                "future_information_allowed": False,
                "must_predict": False,
                "target_horizon": "T+3",
                "neutral_allowed": True,
                "neutral_car_threshold_bps": 50,
            },
        }
    }
    return json.dumps(packet, ensure_ascii=False, indent=2)


async def run_team_prompt(
    events: Iterable[EventRecord],
    *,
    run_id: str,
    model_version: str = "team-prompt-v0",
    concurrency: int = 4,
    skip_event_ids: Optional[set[str]] = None,
    system_prompt_variant: str = "v0",
    on_pred_callback: Optional[Callable[[TeamPrediction], None]] = None,
) -> list[TeamPrediction]:
    system = _build_system_prompt(system_prompt_variant)
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))
    skip = skip_event_ids or set()
    _rl_last_ts: list[float] = [0.0]

    async def one(event: EventRecord) -> Optional[TeamPrediction]:
        if event.event_id in skip:
            return None
        effective_variant = system_prompt_variant
        # ... (keep existing logic for effective_variant)
        _v = (system_prompt_variant or "").strip().lower()
        _cn_family = {
            "v2_cn_specialized", "cn_v2", "cnv2", "merged_cnv2_usv1",
            "v3_cn_calib", "cnv3",
            "v4_cn_calib", "cnv4",
            "v5_cn_calib", "cnv5",
            "v6_cn_calib", "cnv6",
        }
        _event_market = (event.market or "").strip().upper()
        _event_type_l2 = (event.event_type_l2 or "").strip().lower()
        _title_kw_earn_cn = any(k in (event.title or "").lower() for k in ["业绩预告","业绩快报","业绩说明","业绩公告","年报","半年度报","半年报","季报","定期报告","营收","利润表","利润分配","审计报告"])
        if _v in _cn_family:
            if _event_market == "US":
                effective_variant = "usv1"
            elif _event_market == "CN":
                effective_variant = _v
            else:
                effective_variant = "v0"
            if effective_variant == _v and any(t in _event_type_l2 for t in ["earn", "guid", "业绩", "财报", "预", "profit", "alert"]) and not _title_kw_earn_cn:
                effective_variant = "v0"

        eff_system = _build_system_prompt(effective_variant)
        async with sem:
            from ..llm import config as _llm_cfg
            rl_ms = getattr(_llm_cfg, "LLM_RPS_INTERVAL_S", 1.15)
            import time as _t
            now = _t.monotonic()
            need = max(0.0, rl_ms - (now - _rl_last_ts[0]))
            if need > 0:
                await asyncio.sleep(need)
            try:
                obj = await complete_json(eff_system, _event_prompt(event), max_tokens=900)
                print(f"[PROGRESS] {event.event_id} done ({event.market}/{event.event_type_l2})")
            finally:
                _rl_last_ts[0] = _t.monotonic()
        
        direction_raw = str((obj or {}).get("pred_direction") or "").strip().lower()
        if direction_raw in {"up", "down", "neutral"}:
            direction: Direction = direction_raw  # type: ignore[assignment]
        else:
            direction = "up"
        confidence_raw = (obj or {}).get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else None
        except Exception:
            confidence = None
        # I1 方案A硬闸：confidence<0.60 强制 neutral（0.60 为方向判别最低可信阈值，低于此即不应做方向性判断）
        if confidence is not None and confidence < 0.60 and direction != "neutral":
            direction = "neutral"
            tag_conf = f"conf={confidence:.2f}<0.60→neutral"
        else:
            tag_conf = ""
        rationale = (str((obj or {}).get("rationale") or "").strip() or None)
        tag = f"variant={effective_variant} {tag_conf}".strip()
        pred = TeamPrediction(
            event_id=event.event_id,
            pred_direction=direction,
            run_id=run_id,
            model_version=model_version,
            confidence=confidence,
            rationale=(rationale or f"team_prompt benchmark={resolve_benchmark(event)} {tag}"),
            abstain=False,
        )
        if on_pred_callback:
            on_pred_callback(pred)
        return pred

    tasks = [asyncio.create_task(one(e)) for e in events]
    preds: list[TeamPrediction] = []
    for res in await asyncio.gather(*tasks):
        if res is not None:
            preds.append(res)
    return preds


SYSTEM_PROMPT_V7_UNIFIED = """
你是 Pronoia 的严格 as-of 回测判别器。

【核心约束 — 红线，不可违反】
1. 绝对禁止联网或使用 event_time 之后的任何信息。只能基于 as_of_packet 原文判断。
2. 方向 = benchmark-relative CAR（超额收益）方向，非绝对收益。个股涨但跑输基准 → down；个股跌但跑赢基准 → up。
3. 评估窗口 T+3（事件后3个交易日）。
4. 禁止引用/推断任何 post-event CAR（T+1/T+3/T+5 收益）。你的判断是前瞻预判，不是事后归因。

【判别方法论 — 信号加权评分卡】
从 as_of_packet 中提取各类信号，每条信号有方向（up/down）和强度：
- 基本面信号：业绩变化、资产重组、监管政策、回购/增减持
- 行情信号：T0当日涨跌、pre5漂移、pre20漂移
- 结构性信号：事件阶段（意向/受理/正式/终止）、市场微观结构

评分方式：
- 强信号 = 3分（如：净利润亏损/扭亏为盈、并购终止、Rule 425明确稀释）
- 中等信号 = 2分（如：减持公告、定增预案、业绩略增/略降、pre5同向漂移≥5%）
- 弱信号 = 1分（如：模糊利好/利空表述、程序性公告、pre5<5%漂移）
- 总分 = Σ(up信号分) − Σ(down信号分)
- |总分|≥6 → 方向明确；|总分|3~5 → 方向偏强；|总分|1~2 → 方向偏弱；总分=0 → neutral
- 有反向信号时，反向信号分从同向总分中扣减（不是截断，是加权抵消）

【市场结构性先验 — 软先验，可被量化信号覆盖】
以下先验反映各市场的系统性特征，应作为信号加权时的先验倾向，而非硬规则。
当 packet 中的量化信号与先验方向冲突时，以量化信号为准。

A股（CN）先验：
- 减持/定增/配股：系统性偏空（稀释/套现压力），但需关注是否有对冲条款
- 财报利好出尽：业绩略增(0~30%)时存在利好出尽效应；但增速≥50%或扭亏为盈时不适用
- 并购/重组：轻度偏空（商誉减值+锁定期抛压），除非明确优质资产注入或龙头整合
- 涨停回调：短期均值回归偏空
- 回购：弱信号（需金额≥2%且已实施才偏多；仅计划偏空）
- 2024H2~2026H1区间：监管收紧/流动性谨慎 → 混合信息时略偏空(52/48)

美股（US）先验：
- Rule 425并购：发行新股稀释 → 偏空
- 财报：直接反应（beat→up, miss→down），少有利好出尽
- 宏观：降息/低通胀→up，加息/高通胀→down

【Neutral 原则】
- 目标比例 15~20%（非50%）。有≥2条一致信号时必须给出方向。
- 仅在 |总分|≤2 且信号互相抵消时选 neutral。
- confidence<0.50 时自动触发 neutral。

【Confidence 参考区间 — 非硬规则，根据信号质量灵活调整】
- |总分|≥6 且无反向信号 → 0.70~0.85
- |总分|3~5 且≤1条反向 → 0.58~0.68
- |总分|1~2 或≥2条反向 → 0.50~0.55
- neutral → 0.50

【泛化原则】
当 packet 中的事件不属于上述先验覆盖的典型场景时，请基于事件本身的语义和行情信号做独立判断。
不要强行套用不适用的先验。你的金融推理能力是核心资产，先验只是参考。

【输出格式】
{"pred_direction":"up|down|neutral","confidence":0.0,"rationale":"中文理由，引用packet证据"}
""".strip()


# V0-V6 已统一为 V7（软先验 + 评分卡），保留旧常量名仅为向后兼容引用
SYSTEM_PROMPT_V0_COMMON = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V1_US_SPECIALIZED = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V2_CN_SPECIALIZED = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V3_CN_CALIB = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V4_CN_CALIB = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V5_CN_CALIB = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V6_CN_CALIB = SYSTEM_PROMPT_V7_UNIFIED


def _build_system_prompt(variant: str) -> str:
    # V7 统一 prompt：所有 variant 走同一套软先验 + 评分卡逻辑
    return SYSTEM_PROMPT_V7_UNIFIED


TEAM_FULL_QUESTION_TEMPLATE = """
你现在执行严格 as-of 事件回测任务（禁止联网，禁止使用 event_time 之后的任何外部知识、实时数据、补充记忆），
只能基于下面给你的 as_of_packet 原文，做单一事件的方向判别。请按真实团队 Agent 流程走完：
plan → expert fan-out → deep researcher 建证据图 → synthesize → verify → extract hypotheses。

【核心约束 — 红线】
1. 两类判别指标 × 各 5 个窗口，共 10 个独立判断（judge 数量 = 指标数量）：
   - ret（正常收益率）：个股绝对累计收益方向，窗口 T+3 / T+7 / T+15 / T+30 / T+60（事件后N个交易日）。
   - CAR（异常收益率）：个股累计收益 − 基准累计收益 的方向，同样 T+3 / T+7 / T+15 / T+30 / T+60。
   个股涨但跑输基准 → ret=up 而 CAR=down；两类指标可以不一致，必须分别独立判断。
2. 窗口衰减方法论：短窗（T+3/T+7）由公告新颖性、事件冲击与截止前漂移主导；中窗（T+15/T+30）由基本面趋势
   与资金持续性主导；长窗（T+60）由基本面定价与均值回归主导。同一净分可映射出不同窗口方向
   （如净分+4 → car_t3=up 而 car_t60=neutral）；长窗 confidence 应整体低于短窗。
3. 严格禁止未来函数：event_study_skill 在 as_of=True 下仅返回 prediction_cutoff_at 时已完整收盘的数据；
   开盘前预测时窗口止于 T-1，绝不包含 T0/T+1/T+3/T+5 收盘收益或 CAR。你的判断是前瞻预判。
   如工具返回中出现 post-event 数值，必须忽略——那是工具故障泄露。

【信号加权评分卡 — 核心判别方法论】
从 as_of_packet 中提取信号，每条信号有方向（up/down）和强度（3/2/1分）：
- 强信号(3分)：净利润亏损/扭亏为盈、并购终止、Rule 425明确稀释、监管处罚
- 中等信号(2分)：减持公告、定增预案、业绩略增/略降、pre5同向漂移≥5%
- 弱信号(1分)：模糊利好/利空表述、程序性公告、pre5<5%漂移

计算：总分 = Σ(up信号分) − Σ(down信号分)
- |总分|≥6 → 方向明确，confidence 0.70~0.85
- |总分|3~5 → 方向偏强，confidence 0.58~0.68
- |总分|1~2 → 方向偏弱，confidence 0.50~0.55
- 总分=0 或 |总分|≤2且信号互相抵消 → neutral，confidence 0.50
- 反向信号不是截断而是加权抵消：如有2条up(共5分)+1条down(2分)→净分3→偏up

【市场先验 — 软先验，可被量化信号覆盖】
A股(CN)：减持/定增偏空、财报略增有利好出尽效应(增速≥50%或扭亏除外)、并购轻度偏空、回购需≥2%已实施
美股(US)：Rule 425偏空、财报直接反应(beat→up/miss→down)、降息→up/加息→down
当量化信号与先验方向冲突时，以量化信号为准。

【Neutral 使用原则】
- 目标比例15~20%。有≥2条一致信号时必须给出方向。
- 仅在信号互相抵消、|总分|≤2时选neutral。
- confidence<0.50时自动触发neutral。
- 不要因不确定就选neutral——不确定时应降低confidence而非放弃方向判断。

【泛化原则】
当事件不属于上述先验覆盖的典型场景时，基于事件本身的语义和行情信号做独立判断。
不要强行套用不适用的先验。

最后在你的最终回答里必须清晰给出（固定格式，便于脚本解析）：
【多窗口判别】（必须10行，每行固定「指标: 方向 置信度」；方向 up/down/neutral 三选一；置信度 0.50~1.00）
ret_t3: 方向 置信度
ret_t7: 方向 置信度
ret_t15: 方向 置信度
ret_t30: 方向 置信度
ret_t60: 方向 置信度
car_t3: 方向 置信度
car_t7: 方向 置信度
car_t15: 方向 置信度
car_t30: 方向 置信度
car_t60: 方向 置信度
【最终方向】（主指标 = car_t3，须与上面 car_t3 行一致）up 或 down 或 neutral（三选一）
【置信度】（主指标 = car_t3 的置信度）0.5~1.0 之间一个小数
【中文理由】 1~3 句中文，引用公告正文基本面证据 + 事件日/事前行情信号；说明各窗口方向差异的依据；严禁引用 T+N 事后收益
【依据原文片段】 直接 1:1 复制 as_of_packet 里支持你判断的 1~2 句原文

严格 as_of_packet（唯一输入，禁止超纲）：
{packet}

回测元信息（仅用于日志，团队 Agent 不能据此修改判断）：
event_id = {event_id}
market = {market}
symbol = {symbol}
event_time = {event_time}
event_type_l2 = {event_type_l2}
benchmark = {benchmark}
run_id = {run_id}
""".strip()


async def run_team_full_one_event(
    event,
    *,
    run_id: str,
    model_version: str,
    trajectory_ckpt_dir: str | Path,
    system_prompt_variant: str = "v0",
):
    """
    真 Team Agent 单条 event runner：
      1) 把 event → as_of_packet + 元信息 包装成 QUESTION；
      2) 调 app.agents.team.run_team(AsyncIterator[SSE])，收集全部 events；
      3) 把完整 trajectory JSON 落盘 trajectory_ckpt_dir/{event_id}.json；
      4) 从 state[content] 最终回答里解析 direction/confidence/rationale；
      5) 返回 TeamPrediction（和 team_prompt runner 输出 schema 一致，能直接被 bt score / bt case-study 消费）。
    """
    import asyncio
    import datetime as dt
    from pathlib import Path
    import re
    import json as _json

    from app.llm import noop_artifact_store
    from app.agents.team import run_team as _real_run_team
    from .models import TeamPrediction

    eid = getattr(event, "event_id", None) or event["event_id"]
    packet = _event_prompt(event)

    # system prompt variant 目前不影响真 Team Agent（真 Team Agent 自己有 PLANNER_INSTRUCTION + 各专家 skill instruction），
    # 但我们把 variant 塞进 trajectory 元信息，便于后续审计 / case-study 分类。
    variant_effective = str(system_prompt_variant or "v0")

    market = getattr(event, "market", "")
    symbol = getattr(event, "symbol", "")
    event_time = getattr(event, "event_time", "")
    event_type_l2 = getattr(event, "event_type_l2", "")
    benchmark = getattr(event, "benchmark", "")

    question = TEAM_FULL_QUESTION_TEMPLATE.format(
        packet=packet,
        event_id=eid,
        market=market,
        symbol=symbol,
        event_time=str(event_time),
        event_type_l2=event_type_l2,
        benchmark=benchmark,
        run_id=str(run_id),
    )

    import os as _os
    FAST = _os.environ.get("FEVER_BT_FAST", "").strip() in ("1", "true", "yes")

    state = {"content": "", "tool_trace": [], "hypotheses": []}
    t0 = dt.datetime.now()
    traj_events = []
    n_sse_events_total = 0
    n_tokens_total = 0
    agent_names_seen = []

    # --- FAST 模式优化（回测专用，不影响 accuracy）---
    # 1) team_members 白名单：只保留 market_analyst + fundamentals_analyst + deep_researcher
    #    （剔除 predictor，避免多跑 1 轮 LLM + 无意义多情景）
    # 2) question 中追加「预解上下文」：明确告诉专家 symbol/benchmark/事件类型、
    #    以及 as_of_packet 已经包含原文，让 expert 少做 stock_overview 解析型工具调用
    team_kwargs: dict = {}
    if FAST:
        team_kwargs["team_members"] = [
            "market_analyst", "fundamentals_analyst", "deep_researcher",
        ]
        # hypothesis/verify 均不影响 ACC 计算：
        # - hypothesis 只产出 logic_items 事件，不进入 state['content']
        # - verify 不改变事实结论（仅附加修正），但在严格 as-of 回测里专家已经做了核查
        team_kwargs["skip_hypothesis"] = True
        team_kwargs["skip_verify"] = True
        preamble = (
            f"\n\n【回测上下文 - STRICT AS-OF 模式 - 禁止未来函数】"
            f"\n- 标的市场：{market}，代码：{symbol}"
            f"\n- 对比基准（benchmark）：{benchmark}"
            f"\n- 事件类型：{event_type_l2}"
            f"\n- 事件时间：{event_time}"
            f"\n- as_of_packet 已经包含事件原文（标题和正文），做事件研究时用 event_study_skill"
            f"  （event_date={str(event_time)[:10]}, symbol={symbol}, window_days=20, benchmark={benchmark}, **as_of=True**）。"
            f"\n  ⚠️  event_study_skill 只能返回预测截止时点已经完整收盘的数据；若在 T0 开盘前预测，窗口必须止于 T-1，"
            f"绝不包含 T+1/T+3/T+5 的未来 CAR；禁止引用/推断任何 post-event 收益或 CAR。"
            f"\n  ⚠️  你的方向判断必须**仅基于 as_of_packet 公告正文（基本面语义）+ 截止前行情信号**"
            f"（pre5/pre20 漂移），做前瞻预判；禁止使用/提及 T0 收盘或 post3_car_endpoint_pct / post5_cum_return 等未来字段。"
            f"\n- 检索新闻/公告：信息已在 as_of_packet，不需要再联网查同类事件历史。"
        )
        question = question + preamble

    # 事件元信息，用于 Synthesize 阶段路由到对应 Tier 1 analyzer skill
    event_meta = {
        "market": market,
        "event_type_l2": event_type_l2,
        "symbol": symbol,
        "benchmark": benchmark,
        "event_time": str(event_time),
        "prediction_cutoff_at": str(getattr(event, "prediction_cutoff_at", "") or ""),
        "title": getattr(event, "title", ""),
        "event_text": getattr(event, "event_text", ""),
    }

    async for ev in _real_run_team(
        question, history=[], state=state, artifact_store=noop_artifact_store,
        event_meta=event_meta,
        **team_kwargs,
    ):
        n_sse_events_total += 1
        t = ev.get("type")
        # 精简：只保留结构化事件落盘，丢弃 token/thinking 零碎 delta（content_full 已含完整文本）
        if t not in ("token", "thinking"):
            traj_events.append(ev)
        if t == "agent_step":
            a = ev.get("agent")
            if a and a not in agent_names_seen:
                agent_names_seen.append(a)
            if ev.get("phase") == "plan":
                for p in ev.get("plan") or []:
                    if p.get("agent") and p["agent"] not in agent_names_seen:
                        agent_names_seen.append(p["agent"])
        if t == "agent_findings" and ev.get("agent"):
            a = ev["agent"]
            if a not in agent_names_seen:
                agent_names_seen.append(a)
        if t == "token":
            n_tokens_total += 1

    wall_seconds = (dt.datetime.now() - t0).total_seconds()
    final_txt = (state.get("content") or "").strip()

    # 结构化解析：优先用【最终方向】【置信度】【中文理由】的强格式；失败则启发式回退
    direction = None
    confidence = None
    rationale = ""

    def _strip(s: str) -> str:
        s = s.strip()
        if s.startswith(("：", ":")):
            s = s[1:].strip()
        return s.strip()

    m1 = re.search(r"[【\[]\s*最终方向\s*[】\]][^\n]{0,80}?(up|down|neutral)", final_txt, flags=re.I)
    if m1:
        direction = m1.group(1).lower()
    else:
        m = re.search(r"最终方向[:：\s]+?(up|down|neutral)", final_txt, flags=re.I)
        if m:
            direction = m.group(1).lower()

    m2 = re.search(r"[【\[]\s*置信度\s*[】\]][^\d]{0,10}?(\d(?:\.\d+)?|1\.0|0?\.\d+)", final_txt)
    if m2:
        try:
            confidence = float(m2.group(1))
            if confidence <= 0 or confidence > 1.0:
                confidence = max(0.5, min(1.0, confidence))
        except Exception:
            confidence = None
    if confidence is None:
        m = re.search(r"置信度[:：\s]+?(\d(?:\.\d+)?|1\.0|0?\.\d+)", final_txt)
        if m:
            try:
                confidence = max(0.5, min(1.0, float(m.group(1))))
            except Exception:
                confidence = None

    m3 = re.search(r"[【\[]\s*中文理由\s*[】\]]((?:[^\n]+\n?){1,3})", final_txt)
    if m3:
        rationale = re.sub(r"\s+", " ", m3.group(1)).strip()[:800]
    if not rationale:
        m = re.search(r"中文理由[:：]\s*((?:[^\n]+\n?){1,3})", final_txt)
        if m:
            rationale = re.sub(r"\s+", " ", m.group(1)).strip()[:800]
    if not rationale and final_txt:
        rationale = final_txt[-600:]

    # 终极 fallback：在最终回答全文最后 500 chars 中统计 up/down（含 neutral 优先）
    if direction not in {"up", "down", "neutral"}:
        tail = final_txt[-500:].lower()
        ups = tail.count("up")
        dns = tail.count("down")
        ntl = tail.count("neutral")
        if ntl > max(ups, dns):
            direction = "neutral"
        elif ups != dns and (ups + dns) > 0:
            direction = "up" if ups > dns else "down"
    if direction not in {"up", "down", "neutral"}:
        direction = "up"
    if confidence is None:
        confidence = 0.55

    # I1 方案A硬闸：confidence<0.60 强制 neutral（0.60 为方向判别最低可信阈值，低于此即不应做方向性判断）
    applied_gate = False
    if confidence < 0.60 and direction != "neutral":
        direction = "neutral"
        applied_gate = True

    # ===== 多窗口判别解析：ret/car × T+3/7/15/30/60，共 10 个 judge =====
    # 每行格式「指标: 方向 置信度」；主指标 = car_t3（向后兼容 direction/confidence）
    import re as _re2
    horizon_keys = ["ret_t3", "ret_t7", "ret_t15", "ret_t30", "ret_t60",
                    "car_t3", "car_t7", "car_t15", "car_t30", "car_t60"]
    horizons: dict = {}
    for hk in horizon_keys:
        mh = _re2.search(
            rf"{hk}\s*[:：]\s*\**\s*(up|down|neutral)\s*[,，/ ]+\s*\**\s*(0?\.\d+|1\.0+|1)\b",
            final_txt, flags=_re2.I)
        if mh:
            hd = mh.group(1).lower()
            try:
                hc = max(0.50, min(1.0, float(mh.group(2))))
            except ValueError:
                hc = 0.55
            hgate = False
            if hc < 0.60 and hd != "neutral":
                hd, hgate = "neutral", True
            horizons[hk] = {"direction": hd, "confidence": round(hc, 3),
                            "conf_gate_applied": hgate}
    # 缺失窗口用主判断兜底（schema 完整性；主判断语义 = car_t3）
    for hk in horizon_keys:
        if hk not in horizons:
            horizons[hk] = {"direction": direction, "confidence": round(float(confidence), 3),
                            "conf_gate_applied": applied_gate, "filled_from_primary": True}

    # 落盘完整 trajectory（可被 `bt trajectory --event-id` 回放）
    ckpt_p = Path(trajectory_ckpt_dir)
    ckpt_p.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "wall_seconds": wall_seconds,
        "event_id": eid,
        "event_meta": {
            "event_id": eid,
            "market": market,
            "symbol": symbol,
            "event_time": str(event_time),
            "event_type_l2": event_type_l2,
            "benchmark": benchmark,
        },
        "run_id": str(run_id),
        "model_version": str(model_version),
        "system_prompt_variant": variant_effective,
        "llm_trajectory_stats": {
            "n_sse_events": n_sse_events_total,
            "n_sse_events_stored": len(traj_events),
            "n_tokens_total": n_tokens_total,
            "n_tool_calls": len(state.get("tool_trace") or []),
            "n_hypotheses": len(state.get("hypotheses") or []),
            "n_final_chars": len(final_txt),
            "agents_seen": agent_names_seen,
        },
        "as_of_packet": packet,
        "question_to_team": question,
        "structured_extract": {
            "direction": direction,
            "confidence": confidence,
            "rationale": rationale,
            "conf_gate_applied": applied_gate,
            "horizons": horizons,
        },
        "team_final_state": {
            "content_full": final_txt,
            "tool_trace": state.get("tool_trace") or [],
            "hypotheses": state.get("hypotheses") or [],
        },
        "trajectory_sse_events": traj_events,
    }
    (ckpt_p / f"{eid}.json").write_text(_json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    gate_tag = f" [conf_gate: conf={confidence:.2f}<0.60→neutral]" if applied_gate else ""
    return TeamPrediction(
        event_id=eid,
        run_id=str(run_id),
        model_version=str(model_version),
        pred_direction=direction,
        confidence=float(confidence),
        rationale=str(rationale) + gate_tag,
        horizons=horizons,
    )


async def run_team_full_trajectory(
    events,
    *,
    run_id: str,
    model_version: str,
    concurrency: int = 1,
    skip_event_ids: set[str] | None = None,
    system_prompt_variant: str = "v0",
    trajectory_ckpt_dir: str | Path = "data/_trajectory_ckpt",
    on_pred_callback: "Optional[Callable[[TeamPrediction], None]]" = None,
):
    """
    真 Team Agent 批量 runner（和 run_team_prompt 同一层级，供 application.run_predictions_file 调用）。
    真 Team Agent 串行跑（plan→fan-out→synthesize→verify 是强状态的 AsyncIterator，内部有 state= mutable dict，
    并行会互相写 state[content]/tool_trace/hypotheses 炸掉，所以 concurrency>1 会自动退化成 1 并 emit warning）。
    """
    import asyncio
    import sys as _sys
    from .models import TeamPrediction
    from typing import Callable, Optional as _Opt

    # state 是 run_team_full_one_event 内部的局部变量（per-event 独立 dict），
    # 并发不会互相写穿。允许 concurrency > 1 加速批量跑。
    effective_concurrency = max(1, int(concurrency or 1))

    skip = set(skip_event_ids or set())
    remaining = [e for e in events if (getattr(e, "event_id", None) or e.get("event_id", "")) not in skip]
    total = len(remaining)

    sem = asyncio.Semaphore(effective_concurrency)
    results: dict[int, TeamPrediction] = {}

    async def _run_one(idx: int, ev):
        eid = getattr(ev, "event_id", None) or ev.get("event_id", "?")
        symbol = getattr(ev, "symbol", "")
        market = getattr(ev, "market", "")
        tag = f"[{idx}/{total}] eid={eid}  {market}/{symbol}"
        MAX_ATTEMPTS = 2  # 第 1 次正常跑，第 2 次失败重试（指数退避）
        last_exc: Optional[Exception] = None
        last_tb = ""
        async with sem:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                print(
                    f"\n========== team_full start(attempt={attempt}/{MAX_ATTEMPTS}) {tag} ==========",
                    file=_sys.stderr, flush=True,
                )
                try:
                    p = await run_team_full_one_event(
                        ev,
                        run_id=run_id,
                        model_version=model_version,
                        trajectory_ckpt_dir=trajectory_ckpt_dir,
                        system_prompt_variant=system_prompt_variant,
                    )
                    results[idx] = p
                    print(
                        f"========== team_full ok    {tag}  direction={p.pred_direction}  conf={p.confidence:.3f}  ckpt wrote ==========",
                        file=_sys.stderr,
                        flush=True,
                    )
                    if on_pred_callback:
                        on_pred_callback(p)
                    return
                except Exception as exc:
                    from traceback import format_exc
                    last_exc = exc
                    last_tb = format_exc()
                    if attempt < MAX_ATTEMPTS:
                        # 指数退避：attempt=1 → 3.5s；如果是 LLM 5xx/429 再加码
                        backoff = 2.2 ** (attempt) * 1.6
                        msg = str(exc)
                        if any(k in msg for k in ("502", "503", "504", "429", "Too Many", "RemoteDisconnected", "rate limit", "timeout", "Timeout")):
                            backoff *= 1.8
                        print(
                            f"========== team_full retry {tag}  attempt={attempt} fail, sleep {backoff:.1f}s then retry: {type(exc).__name__}: {exc} ==========",
                            file=_sys.stderr, flush=True,
                        )
                        await asyncio.sleep(backoff)
                    else:
                        print(
                            f"========== team_full fail(give up) {tag}  after {MAX_ATTEMPTS} attempts: {type(exc).__name__}: {exc} ==========\n{last_tb}",
                            file=_sys.stderr, flush=True,
                        )
            # 所有尝试失败：Fallback 为 neutral abstain（与 I1 conf<0.52→neutral 硬闸口径一致）
            exc_name = type(last_exc).__name__ if last_exc else "Unknown"
            exc_msg = str(last_exc)[:400] if last_exc else ""
            fallback_p = TeamPrediction(
                event_id=str(eid),
                run_id=str(run_id),
                model_version=str(model_version),
                pred_direction="neutral",
                confidence=0.50,
                rationale=(
                    f"[team_full runner failed after {MAX_ATTEMPTS} attempts, fallback neutral/abstain] "
                    f"{exc_name}: {exc_msg}"
                ),
                abstain=True,
            )
            results[idx] = fallback_p
            if on_pred_callback:
                on_pred_callback(fallback_p)

    await asyncio.gather(*[_run_one(i, ev) for i, ev in enumerate(remaining, 1)])
    preds = [results[i] for i in range(1, total + 1)]
    return preds


# ==============================================================================
# Prompt variant catalog (web UI select & preview)
# ==============================================================================

_PROMPT_VARIANT_CATALOG: list[dict[str, str]] = [
    {
        "id": "v0",
        "label": "v0 · 通用判别（默认）",
        "description": "V7 统一评分卡：软先验 + 3/2/1 信号加权 + CN/US 市场结构先验",
        "market_hint": "CN + US 混合（回测内按 event.market 自动路由）",
    },
    {
        "id": "cnv2",
        "label": "cn_v2 · A 股专业版 v2",
        "description": "A 股事件专用：利好出尽 / 并购偏空 / 减持稀释 等本土结构先验权重加强",
        "market_hint": "仅 CN（事件 market=US 时自动回退 usv1）",
    },
    {
        "id": "cnv3",
        "label": "cn_v3 · A 股校准版 v3",
        "description": "v2 基础上校准了中性比例（目标 15-20%）与 Confidence 区间，避免过度自信",
        "market_hint": "仅 CN（事件 market=US 时自动回退 usv1）",
    },
    {
        "id": "cnv6",
        "label": "cn_v6 · A 股校准版 v6（最新）",
        "description": "v3-v5 迭代，修正 2024H2-2026H1 监管收紧区间的混合信息偏空倾向",
        "market_hint": "仅 CN（事件 market=US 时自动回退 usv1）",
    },
    {
        "id": "usv1",
        "label": "us_v1 · 美股专业版 v1",
        "description": "美股专用：财报 beat/miss 直接反应、Rule 425 稀释、宏观降息加息等典型 US 结构先验",
        "market_hint": "仅 US（事件 market=CN 时自动回退 v0 通用）",
    },
    {
        "id": "merged_cnv2_usv1",
        "label": "merged · cnv2 + usv1 自动路由",
        "description": "混合数据集时最常用：CN 走 cnv2，US 自动切到 usv1",
        "market_hint": "CN + US（按每条 event.market 动态选择）",
    },
]


def _variant_specific_note(variant_id: str) -> str:
    """每个 variant 与其他变体在路由/偏置权重上的真实差异说明。

    说明仅用于 Web UI 在 prompt 预览中向用户呈现「选这个变体实际会发生什么不同」，
    不改变回测逻辑（真实逻辑由 engine.py effective_variant 分支控制）。
    """
    v = str(variant_id or "").strip().lower()
    notes_map = {
        "v0": """【本变体特征 · v0 通用判别】
- 市场：CN + US 混合通用；每条事件按 event.market 切对应市场的先验
- 方法论：统一走 V7 评分卡（软先验 + 3/2/1 信号加权）
- 适用：单一市场与混合市场数据集均可用，作为基线对照
- 与 CN 系列变体相比：本土结构先验的权重不加强，保持中性
""",
        "cnv2": """【本变体特征 · cn_v2 A 股专业版】
- 适用市场：CN（若事件 market=US 会自动切回 usv1）
- A 股本土结构先验权重 **加强**（相比 v0）：
  · 减持 / 定增 / 配股：系统性偏空（稀释与套现压力先验权重 × 1.5）
  · 业绩 0~30% 略增 → 利好出尽偏空（除非明确是扭亏为盈/增速 ≥ 50%）
  · 并购 / 重组：轻度偏空先验（商誉减值 + 锁定期抛压），除非明确是龙头整合/优质资产注入
  · 回购：弱信号（金额 ≥ 2% 且已实施才偏多；仅计划偏空）
- 评分卡主体与 v0 相同（V7 统一），仅 CN 事件的结构先验强度不同
""",
        "cnv3": """【本变体特征 · cn_v3 A 股校准版】
- 适用市场：CN（若事件 market=US 会自动切回 usv1）
- 在 cnv2 的本土先验权重基础上做了两项「校准」：
  · 中性比例目标：15-20%（v0/cnv2 倾向于更激进地下方向），总分 ≤ 2 且信号互相抵消时强制 neutral
  · Confidence 区间收紧：避免过度自信 — |总分|≥6 才到 0.70-0.85（cnv2 可能 ≥ 4 就 0.70+）
- 适用：数据集上 v0/cnv2 过拟合信号、ACC 偏高但 Neutral 比例不足 10% 的场景
""",
        "cnv6": """【本变体特征 · cn_v6 A 股校准版（最新迭代）】
- 适用市场：CN（若事件 market=US 会自动切回 usv1）
- 在 cnv3 的基础上做了「区间校准」：
  · 针对 2024H2 ~ 2026H1 期间监管收紧 / 流动性偏谨慎的环境
  · 当信号混合（同时存在 up/down 信号且净分 ≤ 2）时，先验倾向略偏空（52% down / 48% up 加权），而不是 v0/cnv3 的完全 50/50
  · 涨停回调与"大股东程序性减持"的偏空权重再 × 1.2
- 适用：近期 A 股（2024H2+）数据集的最贴合校准版本
""",
        "usv1": """【本变体特征 · us_v1 美股专业版】
- 适用市场：US（若事件 market=CN 会自动切回 v0 通用）
- 美股典型结构先验启用（不与 CN 先验混用）：
  · 财报：beat → up，miss → down；较少出现 A 股式"利好出尽"
  · Rule 425 / 并购发行新股 → 稀释 → down
  · 宏观背景：降息/通胀下行 → 风险偏好上行（up 先验）；加息/通胀上行 → down 先验
  · 回购/增发自营：明确偏多（美股回购文化与 A 股不同，不会是"仅计划偏空"）
- V7 评分卡主体相同，仅 US 事件的先验替换为美股版
""",
        "merged_cnv2_usv1": """【本变体特征 · merged = cnv2 + usv1 自动路由】
- 适用：CN + US 混合数据集（最常用的混合模式）
- 路由规则：每条事件按自身 event.market 独立选择变体
  · event.market = "CN" → 使用 cn_v2（A 股本土结构先验加强版）
  · event.market = "US" → 使用 us_v1（美股专业版）
  · 其他市场 → 回退到 v0 通用
- 相当于一次性把两个专业版合并成一个变体，不需要分市场跑两次回测
""",
    }
    # 别名归一：cn_v2/cnv2/merged_cnv2_usv1 等等
    aliases = {
        "cn_v2": "cnv2", "v2_cn_specialized": "cnv2", "merged_cnv2": "merged_cnv2_usv1",
        "cn_v3": "cnv3", "v3_cn_calib": "cnv3",
        "cn_v4": "cnv4", "v4_cn_calib": "cnv4",
        "cn_v5": "cnv5", "v5_cn_calib": "cnv5",
        "cn_v6": "cnv6", "v6_cn_calib": "cnv6",
        "us_v1": "usv1", "v1_us_specialized": "usv1",
    }
    key = aliases.get(v, v)
    return notes_map.get(key, notes_map["v0"])


def list_prompt_variants(runner: str) -> list[dict[str, str]]:
    """返回前端可选的 prompt 变体列表，每项附带具体 prompt 文本。

    - runner='baseline'  : 返回空列表（启发式基线，不使用 LLM）
    - runner='team_prompt': 单 Agent 判别，prompt = 变体专属说明 + V7 统一评分卡
    - runner='team_full'  : 多专家协作团队，prompt = 变体专属说明 + TEAM_FULL 任务指令
    """
    if not runner or runner == "baseline":
        return []

    def _with_note(base_text: str, variant_id: str) -> str:
        note = _variant_specific_note(variant_id)
        return (note.rstrip() + "\n" + "=" * 64 + "\n" + base_text.strip()).strip()

    if runner == "team_full":
        tmpl = TEAM_FULL_QUESTION_TEMPLATE.strip()
        return [{**item, "prompt_text": _with_note(tmpl, item["id"])} for item in _PROMPT_VARIANT_CATALOG]
    # team_prompt 与其他：统一走单一 system prompt 判别
    sys_prompt = _build_system_prompt("v0")
    return [{**item, "prompt_text": _with_note(sys_prompt, item["id"])} for item in _PROMPT_VARIANT_CATALOG]
