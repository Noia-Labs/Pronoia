from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Callable, Iterable, Optional

from ..llm import JSONCompletionResult, complete_json, complete_json_diagnostic
from ..model_lab.defaults import DEFAULT_STRUCTURED_OUTPUT_TOKENS
from .cancellation import BacktestCancelled
from .market import resolve_benchmark
from .models import (
    Direction,
    EventRecord,
    TeamPrediction,
    finite_expected_return_pct,
    return_forecast_contract,
    sanitize_event_facts,
    sanitize_pre_event_features,
)


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
    "改善",
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

# Keep the legacy module symbol patchable for existing integrations/tests while
# production calls use the richer diagnostic API.
_ORIGINAL_COMPLETE_JSON = complete_json


def normalize_event_prediction_output(obj: dict | None) -> dict:
    """Validate new predictions without changing the legacy stored schema.

    A neutral direction is only a storage sentinel for an abstention. Explicit
    insufficient_data is a valid non-answer, distinct from malformed output.
    Legacy up/down responses need not provide the new status field.
    """
    errors: list[str] = []
    if obj is None:
        errors.append("completion_failed")
        obj = {}
    status = str(obj.get("prediction_status") or "available").strip().lower()
    insufficient = status == "insufficient_data"
    direction = str(obj.get("pred_direction") or "").strip().lower()
    rationale = obj.get("rationale")
    if not errors:
        if status not in {"available", "insufficient_data"}:
            errors.append("invalid_prediction_status")
        if insufficient:
            if direction not in {"", "null", "none", "neutral"}:
                errors.append("insufficient_data_has_direction")
        elif direction not in {"up", "down"}:
            errors.append("invalid_or_missing_direction")
        confidence = finite_expected_return_pct(obj.get("confidence"))
        if not insufficient and (confidence is None or not 0 <= confidence <= 1):
            errors.append("invalid_or_missing_confidence")
        if not isinstance(rationale, str) or not rationale.strip():
            errors.append("invalid_or_missing_rationale")
    else:
        confidence = None
    if errors:
        status = "invalid_output"
    return {
        "prediction_status": status,
        "direction": direction if status == "available" else "neutral",
        "confidence": confidence if status == "available" else (0.0 if insufficient and not errors else 0.50),
        "rationale": rationale.strip() if isinstance(rationale, str) and rationale.strip() else ("模型输出缺少独立的中文理由字段，已按无效输出处理。" if "invalid_or_missing_rationale" in errors else "模型输出缺少有效的预测字段，已按无效输出处理。"),
        "errors": errors,
        "abstain": status != "available",
        "expected_return_pct": finite_expected_return_pct(obj.get("expected_return_pct")) if status == "available" else None,
    }


async def _complete_event_json(
    system: str,
    user: str,
    *,
    max_tokens: int,
    request_gate=None,
) -> JSONCompletionResult:
    if complete_json is not _ORIGINAL_COMPLETE_JSON:
        if request_gate is not None:
            await request_gate()
        value = await complete_json(system, user, max_tokens=max_tokens)
        return JSONCompletionResult(
            value=value,
            failure_kind="empty_response" if value is None else None,
            attempts=1,
        )
    return await complete_json_diagnostic(
        system,
        user,
        max_tokens=max_tokens,
        request_gate=request_gate,
    )


def validate_event(event: EventRecord) -> list[str]:
    issues: list[str] = []
    if not event.event_id:
        issues.append("event_id 不能为空")
    if event.market not in ("CN", "US", "HK", "FUTURES", "CRYPTO", "FX"):
        issues.append(
            f"market 非法: {event.market!r} "
            "(允许 CN / US / HK / FUTURES / CRYPTO / FX)"
        )
    if not event.symbol:
        issues.append("symbol 不能为空")
    if not event.event_time:
        issues.append("event_time 不能为空")
    else:
        from .evaluation_protocol import parse_event_datetime

        try:
            available_at = parse_event_datetime(
                event.available_time or event.event_time,
                market=event.market,
                field_name="available_time/event_time",
            )
        except ValueError as exc:
            available_at = None
            issues.append(str(exc))
        try:
            occurred_at = parse_event_datetime(
                event.occurred_at,
                market=event.market,
                field_name="occurred_at",
            ) if event.occurred_at else None
        except ValueError as exc:
            occurred_at = None
            issues.append(str(exc))
        if available_at is not None and occurred_at is not None and available_at < occurred_at:
            issues.append("available_time 不能早于 occurred_at")
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
    if pos > neg:
        return "up"
    if neg > pos:
        return "down"
    return "neutral"


def run_baseline(
    events: Iterable[EventRecord],
    *,
    run_id: str,
    model_version: str = "event-baseline-v0",
    target_horizon: str = "t3",
) -> list[TeamPrediction]:
    from .evaluation_protocol import normalize_evaluation_horizon

    selected_horizon = normalize_evaluation_horizon(target_horizon)
    preds: list[TeamPrediction] = []
    for event in events:
        benchmark = resolve_benchmark(event)
        # direction_prior/event_strength are dataset-construction metadata and
        # may encode hindsight. Baselines must use the same as-of packet as all
        # other runners, never label-side priors.
        direction = _keyword_direction(f"{event.title}\n{event.event_text}")
        preds.append(
            TeamPrediction(
                event_id=event.event_id,
                pred_direction=direction,
                run_id=run_id,
                model_version=model_version,
                confidence=0.55,
                rationale=f"baseline:keyword benchmark={benchmark}",
                abstain=False,
                horizon=selected_horizon,
            )
        )
    return preds


def _event_prompt(event: EventRecord, *, target_horizon: str = "t3") -> str:
    from .evaluation_protocol import normalize_evaluation_horizon

    selected_horizon = normalize_evaluation_horizon(target_horizon)
    horizon_days = int(selected_horizon[1:])
    packet = {
        "as_of_packet": {
            "market": event.market,
            "symbol": event.symbol,
            "event_time": event.event_time,
            "available_time": event.available_time or event.event_time,
            "occurred_at": event.occurred_at,
            "event_type_l2": event.event_type_l2,
            "benchmark": resolve_benchmark(event),
            "sector_etf": event.sector_etf,
            # 注意：移除 direction_prior / event_strength —— 这两个字段是事件集构造阶段的标签侧元数据，
            # 可能隐含事件级的后验统计（弱未来信息泄露），严格 as-of 回测下不应暴露给模型。
            "title": event.title,
            "event_text": event.event_text,
            # A provenance reference, never permission for a live fetch during
            # historical replay. The frozen event_text remains the evidence.
            "source_url": event.source_url,
            # These optional fields must be explicitly supplied by the event
            # source.  They are never derived from Oracle labels or future
            # returns inside the backtest engine.
            "event_facts": sanitize_event_facts(event.event_facts),
            "pre_event_features": sanitize_pre_event_features(event.pre_event_features),
            "constraints": {
                "strict_as_of": True,
                "web_search_allowed": False,
                "future_information_allowed": False,
                "must_predict": False,
                "target_horizon": f"T+{horizon_days}",
                "return_forecast_contract": return_forecast_contract(selected_horizon),
                "neutral_allowed": False,
                "prediction_statuses": ["available", "insufficient_data"],
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
    target_horizon: str = "t3",
) -> list[TeamPrediction]:
    from .evaluation_protocol import normalize_evaluation_horizon

    selected_horizon = normalize_evaluation_horizon(target_horizon)
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))
    skip = skip_event_ids or set()
    _rl_next_start: list[float] = [0.0]
    # A semaphore limits in-flight requests but does not rate-limit request
    # starts. This lock reserves distinct start slots while allowing responses
    # to remain in flight concurrently.
    _rate_limit_lock = asyncio.Lock()

    async def _request_gate() -> None:
        from ..llm import config as _llm_cfg
        import time as _t

        interval = max(0.0, float(getattr(_llm_cfg, "LLM_RPS_INTERVAL_S", 1.15)))
        async with _rate_limit_lock:
            now = _t.monotonic()
            need = max(0.0, _rl_next_start[0] - now)
            if need > 0:
                await asyncio.sleep(need)
            started = _t.monotonic()
            _rl_next_start[0] = started + interval

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

        eff_system = _build_system_prompt(effective_variant, target_horizon=selected_horizon)
        async with sem:
            completion = await _complete_event_json(
                eff_system,
                _event_prompt(event, target_horizon=selected_horizon),
                # Leave room for reasoning before the structured prediction;
                # the transport still respects the frozen connection limit.
                max_tokens=DEFAULT_STRUCTURED_OUTPUT_TOKENS,
                request_gate=_request_gate,
            )
            print(f"[PROGRESS] {event.event_id} done ({event.market}/{event.event_type_l2})")

        obj = completion.value
        normalized = normalize_event_prediction_output(obj)
        direction = normalized["direction"]
        confidence = normalized["confidence"]
        rationale = normalized["rationale"]
        invalid_output_reasons = normalized["errors"]
        diagnostic_meta = completion.audit_metadata()
        diagnostic_meta.update({
            "prediction_status": normalized["prediction_status"],
            "completion_quality": "invalid" if invalid_output_reasons else "valid",
            "validation": {
                "valid": not invalid_output_reasons,
                "errors": list(invalid_output_reasons),
            },
            # Legacy flat key retained for existing clients.
            "output_validation_errors": list(invalid_output_reasons),
        })
        usage = completion.usage
        pred = TeamPrediction(
            event_id=event.event_id,
            pred_direction=direction,
            run_id=run_id,
            model_version=model_version,
            confidence=confidence,
            rationale=rationale,
            abstain=normalized["abstain"],
            horizon=selected_horizon,
            strategy_metadata=diagnostic_meta,
            expected_return_pct=normalized["expected_return_pct"],
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            step_ms=completion.elapsed_ms,
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
4. 禁止引用/推断任何 post-event CAR 或事件后收益。你的判断是前瞻预判，不是事后归因。

【判别方法论 — 信号加权评分卡】
从 as_of_packet 中提取各类信号，每条信号有方向（up/down）和强度：
- 基本面信号：业绩变化、资产重组、监管政策、回购/增减持
- 行情信号：仅限信息日前一交易日收盘可得的 pre5/pre20 漂移（T0 当日涨跌不可见）
- 结构性信号：事件阶段（意向/受理/正式/终止）、市场微观结构

评分方式：
- 强信号 = 3分（如：净利润亏损/扭亏为盈、并购终止、Rule 425明确稀释）
- 中等信号 = 2分（如：减持公告、定增预案、业绩略增/略降、pre5同向漂移≥5%）
- 弱信号 = 1分（如：模糊利好/利空表述、程序性公告、pre5<5%漂移）
- 总分 = Σ(up信号分) − Σ(down信号分)
- |总分|≥6 → 方向明确；|总分|3~5 → 方向偏强；|总分|1~2 → 方向偏弱；总分为零时复核证据，不能据此生成中性结果
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

【预测状态与方向】
- 资料足以支持判断时 prediction_status=available，只能给出 up 或 down。
- 关键事实、公告数值或必要事前行情缺失，无法支持判断时 prediction_status=insufficient_data，pred_direction=null，confidence=null，expected_return_pct=null，并明确列出缺失资料。
- 不生成 neutral，不设置看涨/看跌或弃权的目标比例，不因置信度低而把方向改为中性。
- 置信度反映证据强弱；证据不足时不得猜测方向或用默认收益率代替缺失值。

【Confidence 参考区间 — 非硬规则，根据信号质量灵活调整】
- |总分|≥6 且无反向信号 → 0.70~0.85
- |总分|3~5 且≤1条反向 → 0.58~0.68
- |总分|1~2 或≥2条反向 → 0.50~0.55
- confidence 可在 0~1 内表达不确定性，不能代替资料是否充分的判断

【泛化原则】
当 packet 中的事件不属于上述先验覆盖的典型场景时，请基于事件本身的语义和行情信号做独立判断。
不要强行套用不适用的先验。你的金融推理能力是核心资产，先验只是参考。

【输出格式】
严格只输出 JSON（json object），不要输出 Markdown 或其他文字。
{"prediction_status":"available|insufficient_data","pred_direction":"up|down（数据不足时null）","confidence":0.0,"rationale":"中文理由，引用packet证据；数据不足时列出缺失资料","expected_return_pct":null}
expected_return_pct 为同一 T+N 窗口的标的自身累计收益率预测（不是 CAR），2 表示 +2%。
收益起点为事件交易日收盘；盘后或发布时间不明确时顺延至下一交易日收盘，终点为起点后 N 个交易日收盘。
数值必须是基于事前信息的独立前瞻预测，不能把真实事后收益当作已知事实，也不能由方向或置信度套公式得出；无法提供时填 null。
""".strip()


# V0-V6 已统一为 V7（软先验 + 评分卡），保留旧常量名仅为向后兼容引用
SYSTEM_PROMPT_V0_COMMON = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V1_US_SPECIALIZED = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V2_CN_SPECIALIZED = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V3_CN_CALIB = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V4_CN_CALIB = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V5_CN_CALIB = SYSTEM_PROMPT_V7_UNIFIED
SYSTEM_PROMPT_V6_CN_CALIB = SYSTEM_PROMPT_V7_UNIFIED


def _build_system_prompt(variant: str, *, target_horizon: str = "t3") -> str:
    # V7 统一 prompt：所有 variant 走同一套软先验 + 评分卡逻辑。
    # The catalog remains T+3 by default, while each frozen run substitutes its
    # selected numeric horizon so the model and Oracle score the same question.
    from .evaluation_protocol import normalize_evaluation_horizon

    selected = normalize_evaluation_horizon(target_horizon)
    days = int(selected[1:])
    return SYSTEM_PROMPT_V7_UNIFIED.replace(
        "评估窗口 T+3（事件后3个交易日）",
        f"评估窗口 T+{days}（事件后{days}个交易日）",
    )


def _extract_expected_return_pct(text: str) -> Optional[float]:
    """Read only the explicitly named final numeric forecast; never infer it."""
    match = re.search(
        r"^[ \t]*[【\[]\s*预期收益率\s*[】\]][ \t]*[:：]?[ \t]*"
        r"(?:expected_return_pct[ \t]*[:=][ \t]*)?"
        r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)[ \t]*%?[ \t]*$",
        text, flags=re.MULTILINE,
    )
    if not match:
        return None
    return finite_expected_return_pct(float(match.group(1)))


TEAM_FULL_QUESTION_TEMPLATE = """
你现在执行严格 as-of 事件回测任务（禁止联网，禁止使用 event_time 之后的任何外部知识、实时数据、补充记忆），
只能基于下面给你的 as_of_packet 原文，做单一事件的方向判别。请按真实团队 Agent 流程走完：
plan → expert fan-out → deep researcher 建证据图 → synthesize → verify → extract hypotheses。

【核心约束 — 红线】
1. 方向 = benchmark-relative CAR（超额收益）方向，非绝对收益。个股涨但跑输基准 → down；个股跌但跑赢基准 → up。
2. 评估窗口 {target_horizon_label}（事件后{target_horizon_days}个交易日），CAR = 个股累计收益 − 基准累计收益。
3. 严格禁止未来函数：event_study_skill 在 as_of=True 下仅返回事件日前一交易日收盘及更早数据（pre5/pre20漂移），
   绝不包含任何事件后收益或 CAR。你的判断是前瞻预判，禁止引用/推断任何 post-event CAR。
   如工具返回中出现 post-event CAR 数值，必须忽略——那是工具故障泄露。

【信号加权评分卡 — 核心判别方法论】
从 as_of_packet 中提取信号，每条信号有方向（up/down）和强度（3/2/1分）：
- 强信号(3分)：净利润亏损/扭亏为盈、并购终止、Rule 425明确稀释、监管处罚
- 中等信号(2分)：减持公告、定增预案、业绩略增/略降、pre5同向漂移≥5%
- 弱信号(1分)：模糊利好/利空表述、程序性公告、pre5<5%漂移

计算：总分 = Σ(up信号分) − Σ(down信号分)
- |总分|≥6 → 方向明确，confidence 0.70~0.85
- |总分|3~5 → 方向偏强，confidence 0.58~0.68
- |总分|1~2 → 方向偏弱，confidence 0.50~0.55
- 净分接近零时复核证据与不确定性；不得自动生成中性结果
- 反向信号不是截断而是加权抵消：如有2条up(共5分)+1条down(2分)→净分3→偏up

【市场先验 — 软先验，可被量化信号覆盖】
A股(CN)：减持/定增偏空、财报略增有利好出尽效应(增速≥50%或扭亏除外)、并购轻度偏空、回购需≥2%已实施
美股(US)：Rule 425偏空、财报直接反应(beat→up/miss→down)、降息→up/加息→down
当量化信号与先验方向冲突时，以量化信号为准。

【预测状态与方向】
- 资料足以判断时输出 available，并选择 up 或 down。
- 关键事实、公告数值或必要事前行情缺失，无法支持判断时输出 insufficient_data（数据不足），方向、置信度和预期收益率均填 null；理由必须说明具体缺失资料。
- 不输出 neutral，不设置中性比例，不因低置信度改变方向，也不要为满足格式强猜方向。

【泛化原则】
当事件不属于上述先验覆盖的典型场景时，基于事件本身的语义和行情信号做独立判断。
不要强行套用不适用的先验。

另外预测同一窗口的标的自身累计收益率（非 CAR）。收益起点为事件交易日收盘；盘后或发布时间不明确时顺延至下一交易日收盘，终点为起点后 N 个交易日收盘。
这是基于事前信息的独立前瞻估计，不能引用真实事后收益，不能由方向或置信度换算；无法估计时写 null。
最后在你的最终回答里必须清晰给出（必须是6行格式，便于脚本解析）：
【预测状态】 available 或 insufficient_data
【最终方向】 up 或 down；数据不足时 null
【置信度】 0.0~1.0 之间一个小数；数据不足时 null
【中文理由】 1~3 句中文，引用公告正文基本面证据 + 事前行情信号；严禁引用 T0 当日涨跌或 T+N 事后 CAR
【依据原文片段】 直接 1:1 复制 as_of_packet 里支持你判断的 1~2 句原文
【预期收益率】 一个有限数值或 null，2 表示 +2%，-1.5 表示 -1.5%

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
    target_horizon: str = "t3",
    on_stage_callback: Optional[Callable[[dict], None]] = None,
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
    from app.agents.team import (
        STRICT_BACKTEST_SKILL_ALLOWLIST,
        run_team as _real_run_team,
    )
    from .models import TeamPrediction

    from .evaluation_protocol import normalize_evaluation_horizon

    selected_horizon = normalize_evaluation_horizon(target_horizon)
    target_horizon_days = int(selected_horizon[1:])
    eid = getattr(event, "event_id", None) or event["event_id"]
    packet = _event_prompt(event, target_horizon=selected_horizon)

    # system prompt variant 目前不影响真 Team Agent（真 Team Agent 自己有 PLANNER_INSTRUCTION + 各专家 skill instruction），
    # 但我们把 variant 塞进 trajectory 元信息，便于后续审计 / case-study 分类。
    variant_effective = str(system_prompt_variant or "v0")

    market = getattr(event, "market", "")
    symbol = getattr(event, "symbol", "")
    event_time = getattr(event, "event_time", "")
    event_type_l2 = getattr(event, "event_type_l2", "")
    benchmark = resolve_benchmark(event)

    question = TEAM_FULL_QUESTION_TEMPLATE.format(
        packet=packet,
        event_id=eid,
        market=market,
        symbol=symbol,
        event_time=str(event_time),
        event_type_l2=event_type_l2,
        benchmark=benchmark,
        run_id=str(run_id),
        target_horizon_label=f"T+{target_horizon_days}",
        target_horizon_days=target_horizon_days,
    )

    import os as _os
    FAST = _os.environ.get("FEVER_BT_FAST", "").strip() in ("1", "true", "yes")

    state = {"content": "", "tool_trace": [], "hypotheses": []}
    t0 = dt.datetime.now()
    traj_events = []
    n_sse_events_total = 0
    n_tokens_total = 0
    agent_names_seen = []
    current_stage_key = ""
    current_stage_detail = ""

    def _emit_stage(
        stage: str,
        stage_label: str,
        stage_index: int,
        detail: str,
        *,
        agent: str = "",
    ) -> None:
        """Emit advisory in-event progress without affecting scoring/state.

        The callback is deliberately best-effort: a disconnected UI must never
        fail a model run.  Completion remains owned by ``on_pred_callback``.
        """
        nonlocal current_stage_key, current_stage_detail
        normalized_detail = str(detail or "")[:240]
        dedupe_key = f"{stage}:{agent}"
        if dedupe_key == current_stage_key and normalized_detail == current_stage_detail:
            return
        current_stage_key = dedupe_key
        current_stage_detail = normalized_detail
        if on_stage_callback is None:
            return
        try:
            on_stage_callback({
                "event_id": str(eid),
                "symbol": str(symbol or ""),
                "market": str(market or ""),
                "title": str(getattr(event, "title", "") or "")[:160],
                "stage": stage,
                "stage_label": stage_label,
                "stage_index": stage_index,
                "stage_total": 5,
                "detail": normalized_detail,
                "agent": str(agent or ""),
            })
        except Exception:
            # Progress reporting is observability only; it must not alter the
            # investment decision or turn a healthy model response into error.
            pass

    # --- FAST 模式优化（回测专用，不影响 accuracy）---
    # 1) team_members 白名单：只保留 market_analyst + fundamentals_analyst + deep_researcher
    #    （剔除 predictor，避免多跑 1 轮 LLM + 无意义多情景）
    # 2) question 中追加「预解上下文」：明确告诉专家 symbol/benchmark/事件类型、
    #    以及 as_of_packet 已经包含原文，让 expert 少做 stock_overview 解析型工具调用
    # Historical replay must never inherit the generic research agents' live
    # news/price/fundamental skills.  The allowlist leaves only as-of/local
    # analysis helpers callable; the event packet remains the source of facts.
    team_kwargs: dict = {
        "skill_allowlist": STRICT_BACKTEST_SKILL_ALLOWLIST,
    }
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
            f"\n  ⚠️  **as_of=True 时 event_study_skill 仅返回事件日前一交易日收盘及更早数据**（pre5/pre20 漂移），"
            f"绝不包含任何事件后未来 CAR；禁止引用/推断任何 post-event 收益或 CAR。"
            f"\n  ⚠️  你的方向判断必须**仅基于 as_of_packet 公告正文（基本面语义）+ 前一交易日收盘前可得行情信号**"
            f"（pre5/pre20 漂移），做前瞻预判；禁止使用/提及 T0 当日涨跌、post3_car_endpoint_pct / post5_cum_return 等未来字段。"
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
        "available_time": str(getattr(event, "available_time", None) or event_time),
        "occurred_at": str(getattr(event, "occurred_at", None) or ""),
        "title": getattr(event, "title", ""),
        "event_text": getattr(event, "event_text", ""),
    }

    _emit_stage("planning", "规划", 1, "正在拆解事件并规划专家分工", agent="router")
    planned_experts: list[str] = []
    completed_experts: set[str] = set()

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
                    if p.get("agent"):
                        planned_experts.append(str(p["agent"]))
                _emit_stage(
                    "expert_research", "专家研究", 2,
                    f"已规划 {len(planned_experts)} 位专家，开始逐项研究",
                )
            elif ev.get("phase") == "agent_start":
                agent = str(ev.get("agent") or "")
                _emit_stage(
                    "expert_research", "专家研究", 2,
                    f"正在运行 {agent or '专家'}",
                    agent=agent,
                )
            elif ev.get("phase") == "agent_done":
                agent = str(ev.get("agent") or "")
                if agent:
                    completed_experts.add(agent)
                if planned_experts and len(completed_experts) >= len(set(planned_experts)):
                    _emit_stage("synthesis", "综合", 3, "专家研究完成，正在综合证据", agent="router")
            elif ev.get("phase") == "signal_routing":
                _emit_stage("synthesis", "综合", 3, str(ev.get("note") or "正在整合结构化信号"), agent="router")
            elif ev.get("phase") == "verified":
                _emit_stage("review", "复核", 4, str(ev.get("note") or "复核完成"), agent="verifier")
        if t == "thinking":
            agent = str(ev.get("agent") or "")
            detail = str(ev.get("delta") or "")
            if agent == "verifier" or "复核" in detail:
                _emit_stage("review", "复核", 4, detail or "正在复核事实与逻辑", agent="verifier")
            elif "假设" in detail or "提炼" in detail:
                _emit_stage("hypothesis_extraction", "假设提取", 5, detail or "正在提炼可证伪假设", agent="router")
        if t == "logic_items":
            _emit_stage("hypothesis_extraction", "假设提取", 5, "可证伪假设提取完成", agent="router")
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
    invalid_output_reasons: list[str] = []

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
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                confidence = None
        except Exception:
            confidence = None
    if confidence is None:
        m = re.search(r"置信度[:：\s]+?(\d(?:\.\d+)?|1\.0|0?\.\d+)", final_txt)
        if m:
            try:
                parsed_confidence = float(m.group(1))
                confidence = parsed_confidence if math.isfinite(parsed_confidence) and 0.0 <= parsed_confidence <= 1.0 else None
            except Exception:
                confidence = None

    m3 = re.search(r"[【\[]\s*中文理由\s*[】\]]((?:[^\n]+\n?){1,3})", final_txt)
    if m3:
        rationale = re.sub(r"\s+", " ", m3.group(1)).strip()[:800]
    if not rationale:
        m = re.search(r"中文理由[:：]\s*((?:[^\n]+\n?){1,3})", final_txt)
        if m:
            rationale = re.sub(r"\s+", " ", m.group(1)).strip()[:800]
    status_match = re.search(
        r"[【\[]\s*预测状态\s*[】\]][ \t]*[:：]?[ \t*`]*([a-z_]+)",
        final_txt, flags=re.I,
    )
    declared_status = status_match.group(1).lower() if status_match else None
    normalized = normalize_event_prediction_output({
        "prediction_status": declared_status,
        "pred_direction": direction,
        "confidence": confidence,
        "rationale": rationale,
        "expected_return_pct": _extract_expected_return_pct(final_txt),
    })
    direction = normalized["direction"]
    confidence = normalized["confidence"]
    rationale = normalized["rationale"]
    invalid_output_reasons = normalized["errors"]
    expected_return_pct = normalized["expected_return_pct"]
    # Historical readers still accept this flag. New predictions never force
    # low-confidence up/down answers into a neutral opinion.
    applied_gate = False

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
            "output_validation_errors": invalid_output_reasons,
            "abstain": normalized["abstain"],
            "prediction_status": normalized["prediction_status"],
            "target_horizon": selected_horizon,
            "expected_return_pct": expected_return_pct,
            "return_forecast_contract": return_forecast_contract(selected_horizon),
        },
        "team_final_state": {
            "content_full": final_txt,
            "tool_trace": state.get("tool_trace") or [],
            "hypotheses": state.get("hypotheses") or [],
        },
        "trajectory_sse_events": traj_events,
    }
    (ckpt_p / f"{eid}.json").write_text(_json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    return TeamPrediction(
        event_id=eid,
        run_id=str(run_id),
        model_version=str(model_version),
        pred_direction=direction,
        confidence=float(confidence),
        rationale=str(rationale),
        abstain=normalized["abstain"],
        horizon=selected_horizon,
        expected_return_pct=expected_return_pct,
        strategy_metadata={
            "prediction_status": normalized["prediction_status"],
            "output_validation_errors": invalid_output_reasons,
            "completion_quality": "invalid" if invalid_output_reasons else "valid",
        },
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
    on_stage_callback: "Optional[Callable[[dict], None]]" = None,
    target_horizon: str = "t3",
    cancel_check: "Optional[Callable[[], bool]]" = None,
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

    def _raise_if_cancelled() -> None:
        if cancel_check is not None and cancel_check():
            raise BacktestCancelled("cancelled by user")

    async def _run_one_event_cancellable(ev):
        """Await one full-team event while polling the cross-thread cancel flag.

        The orchestrator owns the flag in its worker thread, while the actual
        model work lives in this asyncio loop.  Polling here lets us cancel an
        in-flight streaming request instead of waiting several minutes for the
        next completed-prediction callback.
        """
        if cancel_check is None:
            call_kwargs = {
                "run_id": run_id,
                "model_version": model_version,
                "trajectory_ckpt_dir": trajectory_ckpt_dir,
                "system_prompt_variant": system_prompt_variant,
                "target_horizon": target_horizon,
            }
            if on_stage_callback is not None:
                call_kwargs["on_stage_callback"] = on_stage_callback
            return await run_team_full_one_event(ev, **call_kwargs)
        call_kwargs = {
            "run_id": run_id,
            "model_version": model_version,
            "trajectory_ckpt_dir": trajectory_ckpt_dir,
            "system_prompt_variant": system_prompt_variant,
            "target_horizon": target_horizon,
        }
        if on_stage_callback is not None:
            call_kwargs["on_stage_callback"] = on_stage_callback
        task = asyncio.create_task(
            run_team_full_one_event(ev, **call_kwargs)
        )
        try:
            while not task.done():
                if cancel_check():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise BacktestCancelled("cancelled by user")
                await asyncio.wait({task}, timeout=0.20)
            return task.result()
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    async def _run_one(idx: int, ev):
        eid = getattr(ev, "event_id", None) or ev.get("event_id", "?")
        symbol = getattr(ev, "symbol", "")
        market = getattr(ev, "market", "")
        tag = f"[{idx}/{total}] eid={eid}  {market}/{symbol}"
        MAX_ATTEMPTS = 2  # 第 1 次正常跑，第 2 次失败重试（指数退避）
        last_exc: Optional[Exception] = None
        last_tb = ""
        _raise_if_cancelled()
        async with sem:
            _raise_if_cancelled()
            for attempt in range(1, MAX_ATTEMPTS + 1):
                _raise_if_cancelled()
                print(
                    f"\n========== team_full start(attempt={attempt}/{MAX_ATTEMPTS}) {tag} ==========",
                    file=_sys.stderr, flush=True,
                )
                try:
                    p = await _run_one_event_cancellable(ev)
                    results[idx] = p
                    print(
                        f"========== team_full ok    {tag}  direction={p.pred_direction}  conf={p.confidence:.3f}  ckpt wrote ==========",
                        file=_sys.stderr,
                        flush=True,
                    )
                    if on_pred_callback:
                        on_pred_callback(p)
                    return
                except BacktestCancelled:
                    # User cancellation is a terminal control signal, never a
                    # model/runner failure eligible for whole-event retry.
                    raise
                except asyncio.CancelledError:
                    # Batch cancellation cancels every active/pending task.
                    # Never translate it into a neutral prediction or retry.
                    raise
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
            # 所有尝试失败：保留 run=done 的逐事件可审计失败，不伪造方向观点。
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
                horizon=target_horizon,
                strategy_metadata={
                    "prediction_status": "invalid_output",
                    "output_failure_kind": "runner_error",
                    "output_attempts": MAX_ATTEMPTS,
                    "completion_quality": "invalid",
                    "validation": {
                        "valid": False,
                        "errors": ["team_full_runner_failed"],
                    },
                    "output_validation_errors": ["team_full_runner_failed"],
                },
            )
            results[idx] = fallback_p
            if on_pred_callback:
                on_pred_callback(fallback_p)

    tasks = [
        asyncio.create_task(_run_one(i, ev))
        for i, ev in enumerate(remaining, 1)
    ]
    try:
        await asyncio.gather(*tasks)
    except (BacktestCancelled, asyncio.CancelledError):
        # asyncio.gather does not cancel sibling tasks when one raises an
        # ordinary exception.  Explicitly stop both active requests and all
        # tasks still waiting on the concurrency semaphore.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
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
        "description": "v2 基础上校准 Confidence 区间，区分方向预测与数据不足，避免过度自信",
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
  · 资料不足时明确弃权并列出缺失信息；不设置预测方向或弃权比例目标
  · Confidence 区间收紧：避免过度自信 — |总分|≥6 才到 0.70-0.85（cnv2 可能 ≥ 4 就 0.70+）
- 适用：需要更谨慎评估证据质量和置信度的场景
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
