"""Research logic verification endpoints (design.md §6.4 §8 扩展).

提供「一键深度验证」能力：
  POST /api/logic/auto_check

  输入：研究逻辑条目（hypothesis/scope/horizon/check/category）
  流程：
    1) LLM 解析 horizon → next_check_at（带原因）
    2) 若 now < next_check_at → verdict=pending_scheduled（窗口未到）
    3) 否则 run_agent 跑小型 verifier：调取行情/资金/财报等数据 → 对照 hypothesis
    4) 输出 verdict ∈ {verified, rejected, inconclusive, pending_scheduled}
  输出：{verdict, status, reasoning, evidence, data_summary, next_check_at, ran_at}
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..llm import complete_json, execute_skill
from ..skills.registry import REGISTRY, ensure_skills_loaded

router = APIRouter(prefix="/api/logic", tags=["logic"])

# ----------------------------------------------------------------- 数据模型
class LogicCheckRequest(BaseModel):
    hypothesis: str = Field(..., min_length=2)
    category: str = ""
    probability: str = ""
    scope: str = ""
    horizon: str = ""
    check: str = ""
    question: str = ""
    case_id: str | None = None
    # 可选：上一次研究中的 tool_trace 摘要（前端透传），作为信息源参考
    tool_trace_summary: str = ""


class CheckHistoryEntry(BaseModel):
    at: str
    verdict: str
    reasoning: str
    evidence: list[dict] = Field(default_factory=list)
    data_summary: str = ""
    next_check_at: str | None = None


class LogicCheckResponse(BaseModel):
    verdict: str  # verified / rejected / inconclusive / pending_scheduled / error
    status: str  # 同上
    reasoning: str
    evidence: list[dict] = Field(default_factory=list)  # [{skill, args, ok, preview}]
    data_summary: str = ""
    next_check_at: str | None = None
    ran_at: str


# ----------------------------------------------------------------- 提示词
_PARSE_HORIZON_INSTRUCTION = """你是「时间窗口解析员」。把研究逻辑中的 horizon 字段解析为"下一次应该重新验证的 UTC+8 ISO datetime"。

horizon 示例：
- "未来 5 个交易日" → 下一个 A 股交易日 16:00（上海时区）
- "未来 1 个月" → 当前时刻 + 30 天
- "未来 1 周" → 当前时刻 + 7 天
- "2026Q3 财报" → 2026-10-31 18:00（季报披露截止）
- "2026-08-17" → 2026-08-17 16:00
- "立即" / "无" / "现在" → 立即可验证（next_check_at = null）

当前时间（上海）：{now}

严格输出 JSON：{{
  "next_check_at": "<ISO datetime, 上海时区; 或 null>",
  "window_text": "<人类可读的时间窗口描述>"
}}
若 horizon 缺失/无法解析 → next_check_at=null（立即可验证）。
仅输出 JSON。"""


_VERIFIER_INSTRUCTION = """你是「研究逻辑验证员」。你拿到了一条待验证的研究推演，需要调取真实市场数据来判断它当前是否成立。

策略：
1. 先看 hypothesis / scope / check → 判断需要哪些数据：
   - 涉及"涨跌幅 / 走势" → 调 get_kline（K线） / list_industry_boards
   - 涉及"资金流入流出" → 调 get_industry_fund_flow / get_individual_fund_flow_rank
   - 涉及"估值 / 财务" → 调 get_income_statement / get_financial_summary
   - 涉及"新闻 / 事件" → 调 get_stock_news / get_global_news
   - 涉及"所属板块" → 调 get_stock_industry_info
2. 调取数据后，对照 hypothesis 与 check 字段给出明确判断
3. 严格输出 JSON：
{{
  "verdict": "verified | rejected | inconclusive",
  "reasoning": "<一段话中文推理，引用具体数字与来源>",
  "evidence": [{{"skill": "<被调用的技能名>", "summary": "<一行结果摘要>"}}],
  "data_summary": "<一句话总结当前数据是否支持 hypothesis>",
  "next_check_at": null
}}

- verified: 数据明确支持 hypothesis
- rejected: 数据明确反驳 hypothesis
- inconclusive: 数据不足以判断（如：时间窗口未到、数据缺失、对比基准无法计算等）
- 严格只输出 JSON，不要其他内容。"""


# ----------------------------------------------------------------- 工具函数
def _now_shanghai() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))


def _iso(dt: _dt.datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _safe_iso(d: Any) -> str | None:
    if not d:
        return None
    try:
        if isinstance(d, str):
            # try parse
            _dt.datetime.fromisoformat(d)
            return d
        if isinstance(d, _dt.datetime):
            return d.isoformat(timespec="seconds")
    except Exception:
        return None
    return None


def _next_trading_day(after: _dt.datetime) -> _dt.datetime:
    """下一交易日（粗略：跳过周末），设置为 16:00（收盘后）。"""
    d = after
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d = d + _dt.timedelta(days=1)
    d = d.replace(hour=16, minute=0, second=0, microsecond=0)
    if after.hour >= 16:
        d = d + _dt.timedelta(days=1)
        while d.weekday() >= 5:
            d = d + _dt.timedelta(days=1)
    return d


def _regex_parse_horizon(text: str, now: _dt.datetime) -> dict | None:
    """正则兜底：处理最常见的 horizon 表达。返回 {next_check_at, window_text} 或 None。"""
    import re
    t = (text or "").strip()
    if not t:
        return None

    # 立即 / 现在
    if re.search(r"立即|现在|无$|^$", t):
        return {"next_check_at": None, "window_text": "立即可验证"}

    # 具体日期：YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD / YYYYMMDD
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", t)
    if m:
        try:
            dt = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                              16, 0, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
            return {"next_check_at": dt.isoformat(timespec="seconds"),
                    "window_text": m.group(0)}
        except Exception:
            pass

    # 中文日期：至2026年8月17日 / 2026年8月17日
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", t)
    if m:
        try:
            dt = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                              16, 0, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
            return {"next_check_at": dt.isoformat(timespec="seconds"),
                    "window_text": m.group(0)}
        except Exception:
            pass

    # 季度财报：2026Q3 / 2026Q4
    m = re.search(r"(\d{4})Q([1-4])", t)
    if m:
        year, q = int(m.group(1)), int(m.group(2))
        # 季报披露截止：Q1=4-30, Q2=8-31, Q3=10-31, Q4=次年4-30
        end_month = {1: 4, 2: 8, 3: 10, 4: 4}[q]
        end_year = year if q < 4 else year + 1
        try:
            dt = _dt.datetime(end_year, end_month, 30, 18, 0, 0,
                              tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
            return {"next_check_at": dt.isoformat(timespec="seconds"),
                    "window_text": f"{year}年Q{q}财报披露截止"}
        except Exception:
            pass

    # 未来 N 个交易日
    m = re.search(r"未来\s*(\d+)\s*个交易日", t)
    if m:
        n = int(m.group(1))
        d = now
        added = 0
        while added < n:
            d = d + _dt.timedelta(days=1)
            if d.weekday() < 5:
                added += 1
        d = d.replace(hour=16, minute=0, second=0, microsecond=0)
        return {"next_check_at": d.isoformat(timespec="seconds"),
                "window_text": f"未来 {n} 个交易日"}

    # 未来 N 天 / N 周 / N 月
    m = re.search(r"未来\s*(\d+)\s*(天|日|周|月|年)", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit in ("天", "日"):
            d = now + _dt.timedelta(days=n)
        elif unit == "周":
            d = now + _dt.timedelta(weeks=n)
        elif unit == "月":
            # 简单加月份
            month = now.month + n
            year = now.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            d = now.replace(year=year, month=month)
        else:  # 年
            d = now.replace(year=now.year + n)
        return {"next_check_at": d.isoformat(timespec="seconds"),
                "window_text": f"未来 {n} {unit}"}

    # 1 周 / 1 个月（无"未来"前缀）
    m = re.search(r"^(\d+)\s*(周|月|个交易日)$", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "周":
            d = now + _dt.timedelta(weeks=n)
        elif unit == "月":
            month = now.month + n
            year = now.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            d = now.replace(year=year, month=month)
        else:  # 个交易日
            d = now
            added = 0
            while added < n:
                d = d + _dt.timedelta(days=1)
                if d.weekday() < 5:
                    added += 1
        return {"next_check_at": d.isoformat(timespec="seconds"),
                "window_text": f"{n} {unit}"}

    # 半年报 / 一季报 / 三季报 / 年报
    if "半年报" in t or "中报" in t:
        # 8-31 截止
        y = now.year if now.month <= 8 else now.year + 1
        dt = _dt.datetime(y, 8, 31, 18, 0, 0,
                          tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        return {"next_check_at": dt.isoformat(timespec="seconds"),
                "window_text": f"{y}年半年报披露截止"}
    if "一季报" in t or "Q1" in t.upper():
        y = now.year if now.month <= 4 else now.year + 1
        dt = _dt.datetime(y, 4, 30, 18, 0, 0,
                          tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        return {"next_check_at": dt.isoformat(timespec="seconds"),
                "window_text": f"{y}年一季报披露截止"}
    if "三季报" in t or "Q3" in t.upper():
        y = now.year if now.month <= 10 else now.year + 1
        dt = _dt.datetime(y, 10, 31, 18, 0, 0,
                          tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        return {"next_check_at": dt.isoformat(timespec="seconds"),
                "window_text": f"{y}年三季报披露截止"}
    if "年报" in t or "Q4" in t.upper() or "年度" in t:
        y = now.year if now.month <= 4 else now.year + 1
        dt = _dt.datetime(y, 4, 30, 18, 0, 0,
                          tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        return {"next_check_at": dt.isoformat(timespec="seconds"),
                "window_text": f"{y}年年报披露截止"}

    return None


# ----------------------------------------------------------------- 路由
@router.post("/auto_check", response_model=LogicCheckResponse)
async def auto_check(req: LogicCheckRequest) -> LogicCheckResponse:
    """一键深度验证：先看时间窗口，再决定立即验证 / 等待 / 取数判断。"""
    ensure_skills_loaded()
    now = _now_shanghai()
    ran_at_iso = _iso(now)

    # ---- 1) parse horizon：正则优先，LLM 兜底
    horizon_text = (req.horizon or "").strip()
    next_check_at: _dt.datetime | None = None
    horizon_window_text = "立即可验证"

    if horizon_text:
        parsed = _regex_parse_horizon(horizon_text, now)
        # 正则未命中，或正则给出"立即"但 LLM 可能更精准 → 让 LLM 再核
        if parsed is None or (parsed.get("next_check_at") is None):
            try:
                llm_parsed = await complete_json(
                    _PARSE_HORIZON_INSTRUCTION.format(now=now.isoformat(timespec="seconds")),
                    f"horizon: {horizon_text}",
                    max_tokens=400,
                )
                if llm_parsed and llm_parsed.get("next_check_at"):
                    parsed = llm_parsed
            except Exception:
                pass

        if parsed:
            nca = _safe_iso(parsed.get("next_check_at"))
            if nca:
                try:
                    nca_dt = _dt.datetime.fromisoformat(nca)
                    if nca_dt.tzinfo is None:
                        nca_dt = nca_dt.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
                    next_check_at = nca_dt
                    horizon_window_text = str(parsed.get("window_text") or horizon_text)
                except Exception:
                    next_check_at = None

    # ---- 2) 窗口未到 → pending_scheduled
    if next_check_at is not None and next_check_at > now:
        return LogicCheckResponse(
            verdict="pending_scheduled",
            status="pending_scheduled",
            reasoning=(f"时间窗口「{horizon_text}」尚未到达；将在 {_iso(next_check_at)}（{horizon_window_text}）"
                       f"之后重新验证。"),
            evidence=[],
            data_summary="暂未取数（窗口未到）",
            next_check_at=_iso(next_check_at),
            ran_at=ran_at_iso,
        )

    # ---- 3) 立即验证：先做一次 budget 小的工具调用
    # 构建 verifier 专用的"迷你工具集"——只暴露与验证相关的技能
    verifier_tools = [
        "get_kline", "get_financial_summary", "get_industry_boards",
        "get_stock_news", "get_global_news", "get_announcements",
        "get_industry_fund_flow", "get_individual_fund_flow_rank",
        "get_stock_industry_info", "get_income_statement",
    ]
    available = [s for s in verifier_tools if s in REGISTRY]

    # 准备 system + user messages（不直接 run_agent，因为其 SSE 状态机太重；改用 N 轮 JSON 工具调用循环）
    messages: list[dict] = [
        {"role": "system", "content": _VERIFIER_INSTRUCTION
            + "\n\n可调用的技能（严格限定，请只使用下列技能）：" + ", ".join(available)
            + "\n若都不需要/数据不足，直接给 inconclusive 即可。"},
        {"role": "user", "content": json.dumps({
            "hypothesis": req.hypothesis,
            "category": req.category,
            "scope": req.scope,
            "check": req.check,
            "question": req.question,
            "previous_data_summary": (req.tool_trace_summary or "")[:1500],
            "current_time_shanghai": now.isoformat(timespec="seconds"),
        }, ensure_ascii=False)},
    ]

    evidence: list[dict] = []
    result: dict[str, Any] = {"verdict": "inconclusive",
                              "reasoning": "验证器未在 3 轮内产出结论。",
                              "evidence": [], "data_summary": ""}
    # 最多跑 3 轮工具调用
    try:
        from openai import AsyncOpenAI  # noqa
        from .. import config as _cfg
        from ..skills.registry import tool_schema_subset
        client = _llm_client()
        for round_no in range(1, 5):
            # 注意：不使用 response_format，让模型自由选择 content 或 tool_calls
            resp = await client.chat.completions.create(
                model=_cfg.LLM_MODEL,
                messages=messages,
                tools=tool_schema_subset(available) if available else None,
                max_tokens=2000,
            )
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []
            content = (msg.content or "").strip()
            # ---- 情况 A: 有内容 + 无 tool_calls → 终态
            if content and not tool_calls:
                # 兼容 ```json ... ``` / 纯 JSON 两种格式
                txt = content
                if txt.startswith("```"):
                    # 去掉首尾 ```
                    txt = txt.strip("`").strip()
                    if txt.lower().startswith("json"):
                        txt = txt[4:].lstrip()
                try:
                    result = json.loads(txt)
                except Exception:
                    # 兜底：找第一个 { 到最后一个 } 的片段
                    s, e = txt.find("{"), txt.rfind("}")
                    if s >= 0 and e > s:
                        try:
                            result = json.loads(txt[s:e + 1])
                        except Exception:
                            result = {
                                "verdict": "inconclusive",
                                "reasoning": "模型返回非 JSON 内容：" + content[:200],
                                "evidence": evidence, "data_summary": "",
                            }
                    else:
                        result = {
                            "verdict": "inconclusive",
                            "reasoning": "模型返回非 JSON 内容：" + content[:200],
                            "evidence": evidence, "data_summary": "",
                        }
                break
            # ---- 情况 B: 仅有 tool_calls → 执行并续
            if tool_calls:
                tool_msgs = []
                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}
                    result_skill = await execute_skill(name, args)
                    preview = result_skill.get("data")
                    if isinstance(preview, list) and len(preview) > 5:
                        preview = preview[:5]
                    evidence.append({
                        "skill": name,
                        "args": args,
                        "ok": result_skill.get("ok", False),
                        "summary": (f"{result_skill.get('meta', {}).get('rows', '?')} 行 · "
                                    f"{result_skill.get('meta', {}).get('source', '')}"
                                    if result_skill.get("ok")
                                    else result_skill.get("error", "")[:120]),
                    })
                    tool_msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(
                            {"ok": result_skill.get("ok"), "preview": preview,
                             "meta": result_skill.get("meta"),
                             "note": result_skill.get("note", "")},
                            ensure_ascii=False, default=str
                        )[:3000],
                    })
                messages.append({
                    "role": "assistant", "content": content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name,
                                      "arguments": tc.function.arguments or "{}"}}
                        for tc in tool_calls
                    ],
                })
                messages.extend(tool_msgs)
                continue
            # ---- 情况 C: 都没 → 兜底
            result = {
                "verdict": "inconclusive",
                "reasoning": f"第 {round_no} 轮：模型未产出 JSON 也未调用工具。",
                "evidence": evidence, "data_summary": "",
            }
            break
        else:
            # 4 轮还没结论 → 强制收尾一次（去掉 tools）
            try:
                final = await client.chat.completions.create(
                    model=_cfg.LLM_MODEL,
                    messages=messages + [{"role": "user",
                                          "content": "请基于以上工具结果，立即给出最终 JSON 结论。"
                                                     "不要再调用任何工具。只输出 JSON 本身。"}],
                    max_tokens=1500,
                )
                txt = (final.choices[0].message.content or "").strip()
                if txt.startswith("```"):
                    txt = txt.strip("`").strip()
                    if txt.lower().startswith("json"):
                        txt = txt[4:].lstrip()
                if txt:
                    try:
                        result = json.loads(txt)
                    except Exception:
                        s, e = txt.find("{"), txt.rfind("}")
                        if s >= 0 and e > s:
                            try:
                                result = json.loads(txt[s:e + 1])
                            except Exception:
                                result = {
                                    "verdict": "inconclusive",
                                    "reasoning": "工具调用已耗尽，强制收尾非 JSON。",
                                    "evidence": evidence, "data_summary": "",
                                }
                        else:
                            result = {
                                "verdict": "inconclusive",
                                "reasoning": "工具调用已耗尽，强制收尾为空。",
                                "evidence": evidence, "data_summary": "",
                            }
                else:
                    result = {
                        "verdict": "inconclusive",
                        "reasoning": "工具调用已耗尽，强制收尾为空。",
                        "evidence": evidence, "data_summary": "",
                    }
            except Exception as e:  # noqa: BLE001
                result = {
                    "verdict": "inconclusive",
                    "reasoning": f"工具调用超限且收尾失败: {e}",
                    "evidence": evidence, "data_summary": "",
                }
    except Exception as e:  # noqa: BLE001
        return LogicCheckResponse(
            verdict="error",
            status="inconclusive",
            reasoning=f"验证流程异常: {type(e).__name__}: {e}",
            evidence=evidence,
            data_summary="",
            next_check_at=None,
            ran_at=ran_at_iso,
        )

    verdict = str(result.get("verdict") or "inconclusive").lower()
    if verdict not in ("verified", "rejected", "inconclusive"):
        verdict = "inconclusive"
    status = verdict
    return LogicCheckResponse(
        verdict=verdict,
        status=status,
        reasoning=str(result.get("reasoning") or "").strip()[:2000],
        evidence=evidence,
        data_summary=str(result.get("data_summary") or "").strip()[:500],
        next_check_at=None,
        ran_at=ran_at_iso,
    )


# 单次 client 复用
_CLIENT = None
def _llm_client():
    global _CLIENT
    if _CLIENT is None:
        from openai import AsyncOpenAI
        from .. import config
        _CLIENT = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            timeout=config.LLM_TIMEOUT,
        )
    return _CLIENT
