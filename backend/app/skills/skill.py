"""Skill（设计：三层调度模型 tool → skill → agent → team）。

设计目的
========

将现有 atomic tool（akshare 取数 + 9 个 ``_eg_*`` 图操作）编排为 9 个 **LLM 可见的高层
Skill**。Agent 的 skills 列表只放 skill —— LLM 不再直接面对几十个 atomic tool。

Skill 接口规范：

  入参：高层意图（query / symbol / lookback_days / focus），少而精
  出参：{"ok": True,
         "data": 聚合摘要（结构化，便于 LLM 消费）,
         "artifacts": 子 skill 产出的 artifacts（自动落库）,
         "meta": { "composed": ["_eg_add_evidence", "get_stock_daily", ...],
                   "ok_count": N, "fail_count": M }}
  失败：{"ok": False, "error": "..."}

Skill 内部用 ``asyncio.gather`` 并发调子 tool（execute_skill 由 llm.execute_skill 提供，
它同时支持 sync 与 async handler —— 本文件 handler 都是 async，便于 await 子 tool）。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..llm import execute_skill
from .market import is_a_share_index_symbol, is_us_symbol
from .price_data import resolve_security_ref
from .registry import err, meta, ok, skill


# ---------------------------------------------------------------- helpers ---

def _clip(s: str | None, n: int = 200) -> str:
    s = "" if s is None else str(s).replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


async def _gather_sub(name_args: list[tuple[str, dict]]) -> list[dict]:
    """并发调一组 (skill_name, args)，return_exceptions=True 收集所有结果。"""
    if not name_args:
        return []
    tasks = [execute_skill(n, a) for n, a in name_args]
    return await asyncio.gather(*tasks, return_exceptions=False)


def _summarize_subs(results: list[dict]) -> dict:
    """聚合 sub-skill 调用结果：返回 (ok_count, fail_count, errors, data_points, composed)。"""
    ok_count = 0
    fail_count = 0
    errors: list[str] = []
    data_points: list[Any] = []
    composed: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            fail_count += 1
            errors.append(f"非 dict 返回: {type(r).__name__}")
            continue
        if r.get("ok"):
            ok_count += 1
            d = r.get("data")
            if d is not None:
                data_points.append(d)
        else:
            fail_count += 1
            e = r.get("error") or r.get("data") or "未知失败"
            errors.append(_clip(str(e), 200))
    return {
        "ok_count": ok_count,
        "fail_count": fail_count,
        "errors": errors,
        "data_points": data_points,
        "composed": [],  # 由调用方补充
    }


def _finalize_composite(result: dict, summary: dict, name: str) -> dict:
    """Expose partial failure honestly instead of reporting a false success."""
    fail_count = int(summary.get("fail_count") or 0)
    ok_count = int(summary.get("ok_count") or 0)
    if fail_count:
        result["degraded"] = True
    if ok_count == 0:
        result["ok"] = False
        errors = summary.get("errors") or []
        detail = "；".join(str(e) for e in errors[:3]) or "未知失败"
        result["error"] = f"{name} 的所有子技能均失败：{detail}"
    return result


def _collect_artifacts(results: list[dict]) -> list[dict]:
    """从 sub-tool 结果中收集所有 artifacts 列表（artifacts 优先，单 artifact 兜底）。

    skill 内部调子 tool 时，execute_skill 不会自动落库。
    本函数把子结果里的 artifacts/artifact 拍平，统一挂到 skill 的返回上，
    由 llm.run_agent 走 artifact_store 流程落库 —— 这样 LLM 调一次 skill
    也能在前端 artifacts 面板看到所有子数据。
    """
    out: list[dict] = []
    for r in results:
        if not isinstance(r, dict) or not r.get("ok"):
            continue
        if r.get("artifacts"):
            out.extend(r["artifacts"])
        elif r.get("artifact"):
            out.append(r["artifact"])
    return out


# ============================================================ evidence_graph
# 10 个 _eg_* 的高层 dispatcher。LLM 只看到 1 个 evidence_graph skill，
# 通过 action 参数路由到对应 sub-tool。LLM 不需要记住 10 个 sub-tool 名。

_VALID_GRAPH_ACTIONS = {
    "add_evidence", "add_claim", "link", "set_status", "merge",
    "add_missing", "set_sufficient", "audit", "export", "clear",
}

# action -> _eg_* sub-tool 名 的显式映射（sub-tool 名不一定与 action 一一对应，
# 比如 set_status 对应的是 _eg_set_claim_status，merge 对应 _eg_merge_claims）
_GRAPH_ACTION_TO_SUB: dict[str, str] = {
    "add_evidence": "_eg_add_evidence",
    "add_claim": "_eg_add_claim",
    "link": "_eg_link",
    "set_status": "_eg_set_claim_status",
    "merge": "_eg_merge_claims",
    "add_missing": "_eg_add_missing",
    "set_sufficient": "_eg_set_sufficient",
    "audit": "_eg_audit",
    "export": "_eg_export",
    "clear": "_eg_clear",
}


@skill(
    "evidence_graph",
    "证据图操作（建图/编辑/导出）。action 决定子操作："
    "add_evidence / add_claim / link / set_status / merge / add_missing / "
    "set_sufficient / audit / export / clear。"
    "link 使用 source_id/target_id：supports、contradicts、context 为 claim→evidence；"
    "addresses 为 evidence→missing。子操作需要的参数按 action 传递（除 action 外的所有参数透传给对应 sub-tool）。"
    "audit 不修改图，返回孤立节点、缺少实质证据的 Claim、重复边和缺少 note 等质量发现；"
    "export 也会附带相同 audit。导出时返回 markdown 摘要 + JSON 统计，可同时作为 graph 类型的 artifact 沉淀。",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(_VALID_GRAPH_ACTIONS),
                       "description": "图操作类型"},
        },
        "required": ["action"],
        # 透传其他参数：skill 接口故意用宽松 schema，sub-tool 校验
        "additionalProperties": True,
    },
    category="skill",
    composes=[
        "_eg_add_evidence", "_eg_add_claim", "_eg_link",
        "_eg_set_claim_status", "_eg_merge_claims",
        "_eg_add_missing", "_eg_set_sufficient", "_eg_audit", "_eg_export", "_eg_clear",
    ],
)
async def evidence_graph(action: str, **kwargs) -> dict:
    if action not in _VALID_GRAPH_ACTIONS:
        return err(f"未知 action: {action}（允许: {sorted(_VALID_GRAPH_ACTIONS)}）")
    sub_name = _GRAPH_ACTION_TO_SUB[action]
    sub_result = await execute_skill(sub_name, kwargs)
    if not sub_result.get("ok"):
        return sub_result
    # sub-tool 的返回值直接暴露给 LLM；artifact 走 execute_skill 的 artifact_store 流程
    return ok(
        sub_result.get("data"),
        meta("evidence_graph", 1),
        artifact=sub_result.get("artifact"),
        artifacts=sub_result.get("artifacts"),
    )


# ============================================================== market_research
# 行情研究：并发拉个股 K线 + 行业板块 + 资金流向，聚合返回。
# LLM 入参 {symbol, lookback_days, focus?: ["price", "sector", "flow"]}

@skill(
    "market_research",
    "行情综合研究：并发拉取个股 K线 / 行业板块 / 资金流向 / 龙虎榜 等子数据并聚合。"
    "symbol 为 6 位 A 股代码或美股 ticker（如 AAPL/NVDA）；lookback_days 默认 60；"
    "focus 限定子集（默认 ['price', 'sector', 'flow']）。"
    "美股路径自动禁用 sector / flow / lhb（akshare 无 US 行业/资金流接口），只跑 price。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string",
                       "description": "6 位 A 股代码 / 美股 ticker (AAPL/NVDA/TSLA)"},
            "lookback_days": {"type": "integer", "description": "回溯天数，默认 60"},
            "focus": {"type": "array", "items": {"type": "string"},
                      "description": "子集：price / sector / flow / lhb（美股仅 price 有效）"},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    category="skill",
    composes=["get_stock_daily", "list_industry_boards", "get_industry_fund_flow",
              "get_sector_fund_flow_rank", "get_board_change"],
)
async def market_research(symbol: str, lookback_days: int = 60,
                          focus: list[str] | None = None) -> dict:
    raw = (symbol or "").strip()
    try:
        security = resolve_security_ref(raw)
    except ValueError as exc:
        return err(str(exc))
    us = security.market == "US"
    focus = focus or ["price", "sector", "flow"]
    if us:
        # 美股默认只拉单标的历史价格。全市场 spot 和雪球 company-info
        # 都是昂贵/需认证的补充接口，不应拖垮核心行情成功率。
        sym = security.symbol
        focus_eff = ["price"]
    else:
        sym = security.symbol
        focus_eff = focus
    from datetime import datetime, timedelta
    calendar_lookback = lookback_days if us else max(lookback_days * 2 + 30, lookback_days)
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=calendar_lookback)).strftime("%Y%m%d")
    tasks: list[tuple[str, dict]] = []
    if "price" in focus_eff:
        tasks.append(("get_stock_daily", {"symbol": sym, "start_date": start, "end_date": end, "adjust": "qfq"}))
    if not us:
        if "sector" in focus_eff:
            # 这两个 atomic 返回全市场板块上下文，不接受个股 symbol。
            tasks.append(("list_industry_boards", {"sort_by": "涨跌幅", "limit": 30}))
            tasks.append(("get_board_change", {"sort_by": "涨跌幅", "limit": 50}))
        if "flow" in focus_eff:
            tasks.append(("get_industry_fund_flow", {"sort_by": "净额", "limit": 30}))
            tasks.append(("get_sector_fund_flow_rank", {"board_type": "industry", "limit": 30}))
        if "lhb" in focus_eff:
            # 龙虎榜：日期范围需要 sub-tool 支持，先尝试今天
            tasks.append(("get_lhb", {"start_date": start, "end_date": end}))
    if not tasks:
        return err("focus 不能为空")
    results = await _gather_sub(tasks)
    summary = _summarize_subs(results)
    summary["composed"] = [n for n, _ in tasks]
    price_metrics: dict[str, Any] = {}
    for (name, _), result in zip(tasks, results):
        if name != "get_stock_daily" or not isinstance(result, dict) or not result.get("ok"):
            continue
        rows = result.get("data")
        if not isinstance(rows, list) or not rows:
            continue
        closes: list[float] = []
        for row in rows:
            try:
                closes.append(float(row.get("close")))
            except (TypeError, ValueError, AttributeError):
                continue
        if not closes:
            continue
        latest_row = rows[-1]
        price_metrics = {
            "available_trading_days": len(closes),
            "latest_date": latest_row.get("date"),
            "latest_close": closes[-1],
        }
        for window in (20, 60, 120):
            if len(closes) >= window:
                price_metrics[f"ma{window}"] = round(sum(closes[-window:]) / window, 4)
        break
    out: dict = {"symbol": sym, "lookback_days": lookback_days, "focus": focus_eff,
                 "market": "美股" if us else "A股",
                 "security_kind": security.kind,
                 "price_metrics": price_metrics,
                 "sub_results": [{"skill": n, "ok": r.get("ok"),
                                  "preview": _clip(str(r.get("data") or r.get("error")), 200)}
                                 for (n, _), r in zip(tasks, results)],
                 **summary}
    if us:
        # 美股禁用子集提示
        disabled = [x for x in ("sector", "flow", "lhb") if x in focus]
        if disabled:
            out["note"] = f"美股路径不支持 {','.join(disabled)}（akshare 无 US 接口），仅返回价格"
    return _finalize_composite(ok(
        out,
        meta("market_research", len(tasks)),
        artifacts=_collect_artifacts(results) or None,
    ), summary, "market_research")


# ============================================================== post_market_outlook
# 后市推演（事件预测员的核心入口）：
# 并发拉 K线 + 资金流 + 近期新闻 + 板块异动，输出"预测上下文包"。
# 真正的预测交给 predictor Agent 用 LLM 推理（避免在 skill 里硬编码规则）。
# 第一个落地的预测维度：短期量价 / 催化驱动 / 风险情景。

@skill(
    "post_market_outlook",
    "后市推演上下文包：并发拉取个股近期 K线 / 资金流向 / 个股新闻 / 行业板块异动，"
    "输出聚合数据（不做预测本身 —— 预测由 predictor Agent 接管）。"
    "symbol 6 位 A 股代码或美股 ticker（自动识别）；lookback_days 默认 30。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "股票代码（A股 6 位或美股 ticker）"},
            "lookback_days": {"type": "integer", "description": "回溯天数，默认 30"},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    category="skill",
    composes=["get_stock_daily", "get_industry_fund_flow", "get_individual_fund_flow_rank",
              "get_stock_news", "list_industry_boards"],
)
async def post_market_outlook(symbol: str, lookback_days: int = 30) -> dict:
    raw = (symbol or "").strip()
    if not raw:
        return err("symbol 不能为空")
    us = is_us_symbol(raw)
    if us:
        # 美股：ticker 直接传（skill 不解析 6 位 code，atomic 自行判断）
        sym = raw.upper()
    else:
        sym = "".join(ch for ch in raw if ch.isdigit())[-6:]
        if len(sym) != 6:
            return err(f"symbol 不合法: {symbol}")
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    tasks: list[tuple[str, dict]] = [
        ("get_stock_daily", {"symbol": sym, "start_date": start, "end_date": end, "adjust": "qfq"}),
        ("get_industry_fund_flow", {"sort_by": "净额", "limit": 20}),
        ("get_individual_fund_flow_rank", {"limit": 10}),
        (("list_industry_boards", {"sort_by": "涨跌幅", "limit": 30})
         if not us else ("get_global_news", {"limit": 8})),
    ]
    # 个股新闻仅 A 股可拉，美股已在上面用 global news 替代
    if not us:
        tasks.append(("get_stock_news", {"symbol": sym, "limit": 6}))
    results = await _gather_sub(tasks)
    summary = _summarize_subs(results)
    summary["composed"] = [n for n, _ in tasks]
    # 摘要：抽取 K线末尾 5 根的 OHLC + 涨跌幅 + 资金净额 + 板块名
    kline_summary: list[dict] = []
    flow_summary: dict = {}
    news_titles: list[str] = []
    sector_names: list[str] = []
    for (name, _), r in zip(tasks, results):
        if not isinstance(r, dict) or not r.get("ok"):
            continue
        d = r.get("data") or []
        if name == "get_stock_daily" and isinstance(d, list) and d:
            tail = d[-5:]
            for i, row in enumerate(tail):
                close = row.get("close")
                prev_close = tail[i - 1].get("close") if i > 0 else None
                pct = None
                try:
                    if close is not None and prev_close not in (None, 0):
                        pct = round((float(close) - float(prev_close)) / float(prev_close) * 100, 2)
                except (TypeError, ValueError):
                    pct = None
                kline_summary.append({
                    "date": row.get("date"),
                    "close": close,
                    "pct_chg": pct,
                    "volume": row.get("volume"),
                })
        elif name == "get_industry_fund_flow" and isinstance(d, list) and d:
            flow_summary["industry_top"] = [
                {"name": r.get("名称") or r.get("name"), "net": r.get("净额") or r.get("net")}
                for r in d[:5]
            ]
        elif name == "get_individual_fund_flow_rank" and isinstance(d, list) and d:
            flow_summary["individual_top"] = [
                {"name": r.get("名称") or r.get("name"), "net": r.get("净额") or r.get("net")}
                for r in d[:5]
            ]
        elif name == "get_stock_news" and isinstance(d, list) and d:
            news_titles = [(n.get("title") or "")[:80] for n in d[:6]]
        elif name == "list_industry_boards" and isinstance(d, list) and d:
            sector_names = [(r.get("板块名称") or r.get("name") or "") for r in d[:3]]
    result = ok(
        {
            "symbol": sym, "market": "美股" if us else "A股",
            "lookback_days": lookback_days,
            "kline_recent": kline_summary,
            "flow": flow_summary,
            "news_titles": news_titles,
            "sectors": sector_names,
            "data_point_count": summary["ok_count"],
            **summary,
        },
        meta("post_market_outlook", len(tasks)),
        artifacts=_collect_artifacts(results) or None,
    ) | ({"note": "美股 ticker 已用全球快讯替代个股新闻；预测维度在 predictor Agent 中完成"}
         if us else {})
    return _finalize_composite(result, summary, "post_market_outlook")


# ========================================================== financial_research
# 财务研究：并发拉摘要/指标/利润表/业绩预告

@skill(
    "financial_research",
    "财务综合研究：并发拉取财务摘要 / 财务指标 / 利润表 / 业绩预告 四个子数据并聚合。"
    "symbol 为 6 位代码；period='annual'|'quarterly'。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "6 位股票代码"},
            "period": {"type": "string", "enum": ["annual", "quarterly"],
                       "description": "年报/季度报，默认 annual"},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    category="skill",
    composes=["get_financial_abstract", "get_financial_indicator",
              "get_income_statement", "get_profit_forecast",
              "get_us_stock_info", "get_us_stock_finance",
              "get_us_stock_indicator", "get_us_stock_calendar",
              "get_us_stock_analyst"],
)
async def financial_research(symbol: str, period: str = "annual") -> dict:
    raw = (str(symbol) or "").strip()
    if is_us_symbol(raw):
        # 美股：财务报表（东财） + 财务指标（东财） + 财报日历（yfinance） +
        #       公司简介（雪球） + 卖方研报评级 / 目标价（yfinance）
        sym = raw.upper()
        tasks: list[tuple[str, dict]] = [
            ("get_us_stock_finance",   {"symbol": sym, "report_type": "资产负债表", "indicator": "年报"}),
            ("get_us_stock_indicator", {"symbol": sym, "indicator": "年报"}),
            ("get_us_stock_calendar",  {"symbol": sym}),
            ("get_us_stock_info",      {"symbol": sym}),
            ("get_us_stock_analyst",   {"symbol": sym}),
        ]
        market_label = "美股"
    else:
        code = "".join(ch for ch in raw if ch.isdigit())[-6:]
        if len(code) != 6:
            return err(f"symbol 不合法: {symbol}")
        sym = code
        tasks = [
            ("get_financial_abstract", {"symbol": sym}),
            ("get_financial_indicator", {"symbol": sym}),
            ("get_income_statement", {"symbol": sym, "periods": 8 if period == "annual" else 12}),
            ("get_profit_forecast", {"symbol": sym}),
        ]
        market_label = "A股"
    results = await _gather_sub(tasks)
    summary = _summarize_subs(results)
    summary["composed"] = [n for n, _ in tasks]
    return _finalize_composite(ok(
        {"symbol": sym, "period": period, "market": market_label,
         "sub_results": [{"skill": n, "ok": r.get("ok"),
                          "preview": _clip(str(r.get("data") or r.get("error")), 200)}
                         for (n, _), r in zip(tasks, results)],
         **summary},
        meta("financial_research", len(tasks)),
        artifacts=_collect_artifacts(results) or None,
    ), summary, "financial_research")


# ================================================================== news_intel
# 资讯情报：个股新闻 + 全球快讯 + 公告 三路并发

@skill(
    "news_intel",
    "资讯情报综合：个股新闻 + 全球快讯 + 公告 三路并发拉取聚合。"
    "symbol 可选（不传则只拉全球快讯）；kind 限定子集 default=['news','announcement']。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "6 位股票代码（可选）"},
            "kind": {"type": "array", "items": {"type": "string"},
                     "description": "子集：news / global / announcement"},
            "limit": {"type": "integer", "description": "个股新闻条数，默认 8"},
        },
        "required": [],
        "additionalProperties": False,
    },
    category="skill",
    composes=["llm_web_latest", "get_stock_news", "get_global_news", "get_announcements",
              "get_us_stock_news", "get_us_stock_sec_filings"],
)
async def news_intel(symbol: str | None = None,
                     kind: list[str] | None = None,
                     limit: int = 8) -> dict:
    raw_symbol = (symbol or "").strip()
    us_stock = bool(raw_symbol) and is_us_symbol(raw_symbol)
    code = "".join(ch for ch in raw_symbol if ch.isdigit())[-6:] if raw_symbol else ""
    kind = kind or (["news", "announcement"] if code else ["global"])
    tasks: list[tuple[str, dict]] = []
    notes: list[str] = []
    llm_query = ""
    if code:
        llm_query = f"A股 {code} 最新新闻 公告"
    elif us_stock:
        llm_query = f"{raw_symbol.upper()} latest company news filings"
    else:
        llm_query = "今天 最新 财经热点 新闻"
    llm_first = await execute_skill("llm_web_latest", {"query": llm_query, "limit": max(6, limit)})
    if isinstance(llm_first, dict) and llm_first.get("ok") and isinstance(llm_first.get("data"), list) and llm_first["data"]:
        notes.append("LLM 联网搜索优先命中，未再回退内置资讯源")
        return ok(
            {"symbol": code or (raw_symbol or None), "kind": kind,
             "market": "美股" if us_stock else ("A股" if code else "全局"),
             "sub_results": [{"skill": "llm_web_latest", "ok": True,
                              "preview": _clip(str(llm_first.get("data")), 200)}],
             "ok_count": 1, "fail_count": 0, "errors": [], "data_points": [llm_first.get("data")],
             "composed": ["llm_web_latest"]},
            meta("news_intel", 1),
            artifacts=_collect_artifacts([llm_first]) or None,
        ) | {"note": "；".join(notes)}
    if "news" in kind:
        if code:
            # A 股：调个股新闻
            tasks.append(("get_stock_news", {"symbol": code, "limit": limit}))
        elif us_stock:
            # 美股：走 yfinance.Ticker.news（Yahoo Finance，含标题/摘要/来源/链接）
            tasks.append(("get_us_stock_news", {"symbol": raw_symbol, "count": limit}))
    if "global" in kind:
        tasks.append(("get_global_news", {"limit": limit * 2}))
    if "announcement" in kind:
        if code:
            tasks.append(("get_announcements", {"keyword": code, "limit": limit}))
        elif us_stock:
            # 美股：走 yfinance.Ticker.sec_filings（SEC 8-K/10-Q/10-K 原文）
            tasks.append(("get_us_stock_sec_filings", {"symbol": raw_symbol, "count": limit}))
    if not tasks:
        return err("kind 不能为空（至少要一个非空子集）")

    results = await _gather_sub(tasks)
    summary = _summarize_subs(results)
    summary["composed"] = [n for n, _ in tasks]
    result = ok(
        {"symbol": code or (raw_symbol or None), "kind": kind,
         "market": "美股" if us_stock else ("A股" if code else "全局"),
         "sub_results": [{"skill": n, "ok": r.get("ok"),
                          "preview": _clip(str(r.get("data") or r.get("error")), 200)}
                         for (n, _), r in zip(tasks, results)],
         **summary},
        meta("news_intel", len(tasks)),
        artifacts=_collect_artifacts(results) or None,
    ) | ({"note": "；".join(notes)} if notes else {})
    return _finalize_composite(result, summary, "news_intel")


# ============================================================== holder_research
# 股东研究：股东变化 + 解禁

@skill(
    "holder_research",
    "股东综合研究：股东变化 + 解禁信息 两个子数据并发拉取聚合。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "6 位股票代码"},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    category="skill",
    composes=["get_holder_change", "get_restricted_release_detail",
              "get_us_stock_holder"],
)
async def holder_research(symbol: str) -> dict:
    raw = (str(symbol) or "").strip()
    if is_us_symbol(raw):
        # 美股：major_holders / institutional_holders / mutualfund_holders / insider_transactions
        sym = raw.upper()
        tasks: list[tuple[str, dict]] = [("get_us_stock_holder", {"symbol": sym})]
        market_label = "美股"
    else:
        code = "".join(ch for ch in raw if ch.isdigit())[-6:]
        if len(code) != 6:
            return err(f"symbol 不合法: {symbol}")
        sym = code
        tasks = [
            ("get_holder_change", {"symbol": sym}),
            ("get_restricted_release_detail", {"symbol": sym, "limit": 30}),
        ]
        market_label = "A股"
    results = await _gather_sub(tasks)
    summary = _summarize_subs(results)
    summary["composed"] = [n for n, _ in tasks]
    return _finalize_composite(ok(
        {"symbol": sym, "market": market_label,
         "sub_results": [{"skill": n, "ok": r.get("ok"),
                          "preview": _clip(str(r.get("data") or r.get("error")), 200)}
                         for (n, _), r in zip(tasks, results)],
         **summary},
        meta("holder_research", len(tasks)),
        artifacts=_collect_artifacts(results) or None,
    ), summary, "holder_research")


# ================================================================ macro_intel
# 宏观情报

@skill(
    "macro_intel",
    "宏观情报：宏观指标 + 行业资金流（按板块）。"
    "topic 可选（CPI/PPI/GDP/PMI/bond_yield，不传默认 PMI）。",
    {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "宏观主题（可选）"},
        },
        "required": [],
        "additionalProperties": False,
    },
    category="skill",
    composes=["get_macro", "get_sector_fund_flow_rank"],
)
async def macro_intel(topic: str | None = None) -> dict:
    raw_topic = (topic or "PMI").strip().lower()
    aliases = {
        "cpi": "cpi", "ppi": "ppi", "pmi": "pmi", "gdp": "gdp",
        "bond_yield": "bond_yield", "bond yield": "bond_yield",
        "国债": "bond_yield", "十年期国债": "bond_yield",
    }
    indicator = aliases.get(raw_topic)
    if indicator is None:
        return err(f"不支持的宏观 topic: {topic}（支持 CPI/PPI/PMI/GDP/bond_yield）")
    tasks = [
        ("get_macro", {"indicator": indicator}),
        ("get_sector_fund_flow_rank", {"board_type": "industry", "limit": 30}),
    ]
    results = await _gather_sub(tasks)
    summary = _summarize_subs(results)
    summary["composed"] = [n for n, _ in tasks]
    return _finalize_composite(ok(
        {"topic": topic or "PMI", "indicator": indicator,
         "sub_results": [{"skill": n, "ok": r.get("ok"),
                          "preview": _clip(str(r.get("data") or r.get("error")), 200)}
                         for (n, _), r in zip(tasks, results)],
         **summary},
        meta("macro_intel", len(tasks)),
        artifacts=_collect_artifacts(results) or None,
    ), summary, "macro_intel")


# ================================================================ event_study
# 事件研究：先 search_stock 解析 symbol，再调 event_study

@skill(
    "event_study_skill",
    "事件研究：基于 event_study 子能力，分析单次事件前后的异常收益（CAR）。"
    "event_date YYYY-MM-DD，symbol 支持 6 位 A 股代码或美股 ticker（AAPL/NVDA/TSLA）。"
    "如果传 keyword 而无 symbol，先用 search_stock 解析（自动识别美股/ A 股）。"
    "window_days 默认 30；回测/严格 as-of 场景请传 as_of=True，并传 prediction_cutoff_at，"
    "此时仅返回截止时点已经完整收盘的数据（禁止未来函数）。",
    {
        "type": "object",
        "properties": {
            "event_date": {"type": "string", "description": "事件日期 YYYY-MM-DD"},
            "symbol": {"type": "string",
                       "description": "股票代码：A 股 6 位 / 美股 ticker（与 keyword 二选一）"},
            "keyword": {"type": "string", "description": "股票关键词（与 symbol 二选一）"},
            "window_days": {"type": "integer", "description": "事件窗口，默认 30"},
            "benchmark": {"type": "string",
                          "description": "基准指数/ETF：A 股传 sh000300 等；美股传 SPY/QQQ/XLK 等（优先使用调用方指定）"},
            "as_of": {"type": "boolean",
                      "description": "严格 as-of 回测模式：True=只返回预测截止时已完整收盘的数据"},
            "prediction_cutoff_at": {"type": "string",
                      "description": "预测截止时间（ISO 8601，含时区）；开盘前预测会自动排除 T0 收盘数据"},
        },
        "required": ["event_date"],
        "anyOf": [
            {"required": ["symbol"]},
            {"required": ["keyword"]},
        ],
        "additionalProperties": False,
    },
    category="skill",
    composes=["search_stock", "event_study"],
)
async def event_study_skill(event_date: str, symbol: str | None = None,
                            keyword: str | None = None,
                            window_days: int = 30,
                            benchmark: str | None = None,
                            as_of: bool = False,
                            prediction_cutoff_at: str = "") -> dict:
    sym_raw = (symbol or "").strip()
    us = bool(sym_raw) and is_us_symbol(sym_raw)
    if us:
        # 美股：保留原始 ticker（去空白 / 大写），如 AAPL / BRK.B
        sym = sym_raw.upper()
    elif is_a_share_index_symbol(sym_raw):
        # A 股指数：保留 sh000300 / sz399001 形式，不能截成 6 位股票代码
        sym = sym_raw.lower()
    elif sym_raw:
        # A 股：保留显式交易所前缀；若只有 6 位代码，则在这里补正确前缀。
        # 不能先剥 SH/SZ 再交给下游，否则 51/56/58 开头的沪市 ETF 会被误判为深市。
        code6 = "".join(ch for ch in sym_raw if ch.isdigit())[-6:]
        if len(code6) == 6:
            lowered = sym_raw.lower()
            if lowered.startswith(("sh", "sz", "bj")):
                sym = lowered[:2] + code6
            elif code6.startswith(("50", "51", "52", "56", "58")):
                sym = "sh" + code6
            elif code6.startswith("15"):
                sym = "sz" + code6
            else:
                sym = code6
        else:
            sym = ""
    else:
        sym = ""
    if not sym and keyword:
        r = await execute_skill("search_stock", {"keyword": keyword})
        if r.get("ok") and isinstance(r.get("data"), list) and r["data"]:
            d0 = r["data"][0]
            cand = str(d0.get("symbol") or d0.get("代码") or d0.get("code") or "")
            cand = cand.strip()
            if is_us_symbol(cand):
                sym = cand.upper()
                us = True
            elif is_a_share_index_symbol(cand):
                sym = cand.lower()
            else:
                code6 = "".join(ch for ch in cand if ch.isdigit())[-6:]
                if len(code6) == 6:
                    lowered = cand.lower()
                    if lowered.startswith(("sh", "sz", "bj")):
                        sym = lowered[:2] + code6
                    elif code6.startswith(("50", "51", "52", "56", "58")):
                        sym = "sh" + code6
                    elif code6.startswith("15"):
                        sym = "sz" + code6
                    else:
                        sym = code6
    if not sym:
        return err("必须提供 symbol 或 keyword")

    # ===== P1：基准自动绑定（美股科技股 → QQQ/XLK），覆盖 event_study 默认 SPY 的错配 =====
    # XLK 成分股（technology sector 纯科技）
    XLK_COMPONENTS = {"AAPL", "MSFT", "NVDA", "AVGO", "ADBE", "AMAT", "QCOM", "TXN",
                      "AMD", "INTC", "ORCL", "CRM", "NOW", "SNPS", "CDNS", "ANSS",
                      "KLAC", "LRCX", "MRVL", "MU"}
    # QQQ 前 20 大成分股（按权重）
    QQQ_COMPONENTS = {"AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "AVGO",
                      "TSLA", "NFLX", "AMD", "COST", "PEP", "QCOM", "ADBE", "INTU",
                      "AMAT", "CMCSA", "ORCL", "TXN"}
    idx_sym = ""
    if benchmark:
        idx_sym = benchmark.strip()
    elif us:
        if sym in XLK_COMPONENTS:
            idx_sym = "XLK"
        elif sym in QQQ_COMPONENTS:
            idx_sym = "QQQ"
        else:
            idx_sym = "SPY"
    else:
        idx_sym = "sh000300"

    # event_study 子能力用 pre/post 表达事件窗口；window_days 转为单边窗口长度
    pre = max(1, min(int(window_days or 30), 60))
    # as_of=True 时 post 强制 0；prediction_cutoff_at 在 T0 收盘前时底层进一步截断到 T-1。
    post = 0 if as_of else pre
    call_args = {
        "event_date": event_date, "symbol": sym,
        "pre": pre, "post": post,
        "index_symbol": idx_sym,
        "as_of": bool(as_of),
        "prediction_cutoff_at": prediction_cutoff_at,
    }
    result = await execute_skill("event_study", call_args)
    if not result.get("ok"):
        return result
    es_data = result.get("data") or {}
    es_summary = es_data.get("summary") or {}
    es_meta = result.get("meta") or {}
    price_sources = {
        "asset_provider": es_meta.get("asset_provider"),
        "benchmark_provider": es_meta.get("benchmark_provider"),
        "asset_attempts": es_meta.get("asset_attempts") or [],
        "benchmark_attempts": es_meta.get("benchmark_attempts") or [],
        "requested_start_date": es_meta.get("requested_start_date"),
        "requested_end_date": es_meta.get("requested_end_date"),
    }

    # ===== P0：严格 as_of 回测模式下，移除所有泄露未来信息的字段 =====
    if as_of:
        # 确保顶层绝不暴露 T+3 CAR / direction_hint（防止子调用因缓存/配置疏漏返回旧格式）
        return ok(
            {"symbol": sym, "market": "美股" if us else "A股",
             "event_date": event_date, "window_days": window_days,
             "benchmark": idx_sym,
             "as_of_mode": True,
             "prediction_cutoff_at": prediction_cutoff_at or None,
             "data_cutoff_date": es_summary.get("data_cutoff_date"),
             "event_day_data_included": es_summary.get("event_day_data_included"),
             "signal_event_day_change_pct": es_summary.get("event_day_change_pct"),
             "signal_event_day_idx_change_pct": es_summary.get("event_day_idx_change_pct"),
             "signal_event_day_ar_pct": es_summary.get("event_day_ar_pct"),
             "signal_pre5_cum_return_pct": es_summary.get("pre5_cum_return_pct"),
             "signal_pre20_cum_return_pct": es_summary.get("pre20_cum_return_pct"),
             "signal_pre5_cum_ar_pct": es_summary.get("pre5_cum_ar_pct"),
             # 彻底移除未来函数字段：显式置 None，禁止任何条件下出现有效数值
             "benchmark_relative_car_t3_pct": None,
             "direction_hint": None,
             "postN_blocked": True,
             "price_sources": price_sources,
             "event_study": es_data},
            meta("event_study_skill", 1),
            artifact=result.get("artifact"),
            artifacts=result.get("artifacts"),
        )
    else:
        # 非回测场景（正常投研/分析）：保留完整 CAR 数据
        car_t3 = es_summary.get("post3_car_endpoint_pct")
        return ok(
            {"symbol": sym, "market": "美股" if us else "A股",
             "event_date": event_date, "window_days": window_days,
             "benchmark": idx_sym,
             "as_of_mode": False,
             "price_sources": price_sources,
             "benchmark_relative_car_t3_pct": car_t3,
             "direction_hint": ("up" if car_t3 is not None and car_t3 > 0.5
                                else "down" if car_t3 is not None and car_t3 < -0.5
                                else "neutral"),
             "event_study": es_data},
            meta("event_study_skill", 1),
            artifact=result.get("artifact"),
            artifacts=result.get("artifacts"),
        )


_POLICY_EVENT_PRESETS: dict[str, dict[str, Any]] = {
    "资本市场政策": {
        "aliases": ["资本市场", "政策事件", "股市政策", "监管政策", "金融支持", "护市", "样本"],
        "events": [
            {
                "event_name": "一揽子金融支持政策",
                "event_date": "2024-09-24",
                "published_at": "2024-09-24",
                "event_text": "央行与金融监管部门推出一揽子金融支持政策，核心包括降准降息与资本市场流动性支持工具。",
                "symbol": "600030",
                "symbol_name": "中信证券",
            },
            {
                "event_name": "政治局会议提振资本市场表述",
                "event_date": "2024-09-26",
                "published_at": "2024-09-26",
                "event_text": "政治局会议提出努力提振资本市场，强化市场稳定预期与风险偏好修复。",
                "symbol": "300059",
                "symbol_name": "东方财富",
            },
            {
                "event_name": "中央汇金增持 ETF 护市",
                "event_date": "2025-04-07",
                "published_at": "2025-04-07",
                "event_text": "中央汇金增持 ETF 并释放稳定市场信号，市场将其视为政策性护市动作。",
                "symbol": "601688",
                "symbol_name": "华泰证券",
            },
        ],
    },
    "地产政策": {
        "aliases": ["地产", "房地产", "楼市"],
        "events": [
            {
                "event_name": "政治局会议强调稳定房地产市场",
                "event_date": "2024-09-26",
                "published_at": "2024-09-26",
                "event_text": "政治局会议强调促进房地产市场止跌回稳，改善地产链风险偏好。",
                "symbol": "000002",
                "symbol_name": "万科A",
            },
            {
                "event_name": "增量财政政策提振地产链预期",
                "event_date": "2024-10-12",
                "published_at": "2024-10-12",
                "event_text": "增量财政政策发布后，市场上调地产链需求与信用修复预期。",
                "symbol": "600048",
                "symbol_name": "保利发展",
            },
        ],
    },
    "新能源产业政策": {
        "aliases": ["新能源", "光伏", "电动车", "产业政策"],
        "events": [
            {
                "event_name": "新能源产业支持政策预期强化",
                "event_date": "2024-10-12",
                "published_at": "2024-10-12",
                "event_text": "产业支持政策与财政扩张预期提升了新能源链景气度修复想象空间。",
                "symbol": "300750",
                "symbol_name": "宁德时代",
            },
            {
                "event_name": "关税扰动下自主可控与新能源链重估",
                "event_date": "2025-04-02",
                "published_at": "2025-04-02",
                "event_text": "关税扰动抬升自主可控与国产替代预期，新能源龙头相对收益重新定价。",
                "symbol": "002594",
                "symbol_name": "比亚迪",
            },
        ],
    },
}


def _resolve_policy_category(category: str | None) -> tuple[str, bool]:
    raw = (category or "").strip()
    if not raw:
        return "资本市场政策", True
    for canonical, meta_info in _POLICY_EVENT_PRESETS.items():
        haystack = [canonical, *meta_info.get("aliases", [])]
        if any(token and token in raw for token in haystack):
            return canonical, canonical != raw
    return "资本市场政策", True


def _extract_window_metric(window_rows: list[dict], t_value: int) -> float | None:
    for row in window_rows:
        try:
            if int(row.get("t")) == t_value:
                car = row.get("car")
                return None if car is None else round(float(car), 4)
        except (TypeError, ValueError):
            continue
    return None


def _extract_price_rows(result: dict) -> list[dict]:
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return []
    for point in data.get("data_points", []):
        if isinstance(point, list) and point and isinstance(point[0], dict) and "close" in point[0]:
            return point
    return []


def _daily_returns(price_rows: list[dict]) -> dict[str, float]:
    returns: dict[str, float] = {}
    closes: list[tuple[str, float]] = []
    for row in price_rows:
        try:
            closes.append((str(row.get("date")), float(row.get("close"))))
        except (TypeError, ValueError):
            continue
    closes.sort(key=lambda x: x[0])
    for prev, cur in zip(closes, closes[1:]):
        prev_close = prev[1]
        if prev_close == 0:
            continue
        returns[cur[0]] = (cur[1] - prev_close) / prev_close
    return returns


def _pairwise_corr(a: dict[str, float], b: dict[str, float]) -> float | None:
    keys = sorted(set(a) & set(b))
    if len(keys) < 10:
        return None
    xs = [a[k] for k in keys]
    ys = [b[k] for k in keys]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return round(cov / (var_x ** 0.5 * var_y ** 0.5), 4)


@skill(
    "policy_event_dataset",
    "政策事件样本：针对 A 股政策事件样本题，按预置政策类别选取代表性事件与标的，"
    "调用事件研究生成 T+1/T+5/T+20 超额收益，并附去重规则。"
    "category 可选；不传时默认按『资本市场政策』处理并明确写出假设。",
    {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "政策类别，可选，如 资本市场政策 / 地产政策 / 新能源产业政策"},
            "benchmark": {"type": "string", "description": "A股基准指数，默认 sh000300"},
            "max_events": {"type": "integer", "description": "最多返回几个代表事件，默认 3"},
        },
        "required": [],
        "additionalProperties": False,
    },
    category="skill",
    composes=["event_study_skill"],
)
async def policy_event_dataset(category: str | None = None,
                               benchmark: str = "sh000300",
                               max_events: int = 3) -> dict:
    canonical, assumed = _resolve_policy_category(category)
    seeds = _POLICY_EVENT_PRESETS[canonical]["events"][: max(1, min(int(max_events or 3), 5))]
    results: list[dict] = []
    for seed in seeds:
        # 事件研究底层依赖在并发场景下会触发 mini_racer 崩溃，样本构建这里强制串行。
        result = await execute_skill("event_study_skill", {
            "event_date": seed["event_date"],
            "symbol": seed["symbol"],
            "window_days": 20,
        })
        results.append(result)
    rows: list[dict[str, Any]] = []
    artifacts = _collect_artifacts(results)
    for seed, result in zip(seeds, results):
        if not isinstance(result, dict) or not result.get("ok"):
            rows.append({
                "event_name": seed["event_name"],
                "event_date": seed["event_date"],
                "published_at": seed.get("published_at", seed["event_date"]),
                "event_text": seed.get("event_text"),
                "symbol": seed["symbol"],
                "symbol_name": seed["symbol_name"],
                "benchmark": benchmark,
                "trading_day_mapping": None,
                "t_plus_1_excess_pct": None,
                "t_plus_5_excess_pct": None,
                "t_plus_20_excess_pct": None,
                "status": "failed",
                "note": result.get("error") if isinstance(result, dict) else "unknown error",
            })
            continue
        payload = result.get("data") or {}
        event_payload = payload.get("event_study") or {}
        summary = event_payload.get("summary") or {}
        window = event_payload.get("window") or []
        rows.append({
            "event_name": seed["event_name"],
            "event_date": seed["event_date"],
            "published_at": seed.get("published_at", seed["event_date"]),
            "event_text": seed.get("event_text"),
            "event_day": summary.get("event_day"),
            "trading_day_mapping": (
                f"发布时间 {seed.get('published_at', seed['event_date'])} -> "
                f"T0 {summary.get('event_day')}"
            ),
            "symbol": payload.get("symbol"),
            "symbol_name": seed["symbol_name"],
            "benchmark": summary.get("index_symbol", benchmark),
            "t_plus_1_excess_pct": _extract_window_metric(window, 1),
            "t_plus_5_excess_pct": _extract_window_metric(window, 5),
            "t_plus_20_excess_pct": _extract_window_metric(window, 20),
            "status": "ok",
        })
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    if not ok_rows:
        return err(f"{canonical} 样本构建失败：未拿到可用事件研究结果")
    dedup_rules = [
        "同一政策只保留首次正式发布日，解读稿与转述稿不重复入样本。",
        "同一标的在 20 个交易日事件窗口内仅保留一次，避免窗口重叠污染。",
        "若同类政策连续发布，以层级更高、市场影响更强的版本作为主事件。",
    ]
    table = {
        "kind": "table",
        "title": f"{canonical} 事件样本（代表性 {len(rows)} 条）",
        "payload": {
            "columns": [
                "event_name", "event_date", "published_at", "event_day", "trading_day_mapping",
                "symbol_name", "symbol", "benchmark",
                "t_plus_1_excess_pct", "t_plus_5_excess_pct", "t_plus_20_excess_pct", "status",
            ],
            "rows": [
                [row.get(col) for col in (
                    "event_name", "event_date", "published_at", "event_day", "trading_day_mapping",
                    "symbol_name", "symbol", "benchmark",
                    "t_plus_1_excess_pct", "t_plus_5_excess_pct", "t_plus_20_excess_pct", "status",
                )]
                for row in rows
            ],
            "note": "T+1/T+5/T+20 取事件研究窗口内 CAR 值，单位为 %",
        },
    }
    artifacts.append(table)
    return ok(
        {
            "category": canonical,
            "category_assumed": assumed,
            "benchmark": benchmark,
            "rows": rows,
            "dedup_rules": dedup_rules,
        },
        meta("policy_event_dataset", len(rows)),
        artifacts=artifacts or None,
    ) | ({"note": "题面未明确政策类别，已按『资本市场政策』默认假设执行"} if assumed else {})


_PORTFOLIO_REVIEW_DEFAULT = {
    "name": "默认券商集中组合",
    "industry": "证券",
    "holdings": [
        {"symbol": "600030", "name": "中信证券", "weight": 0.30},
        {"symbol": "601688", "name": "华泰证券", "weight": 0.22},
        {"symbol": "300059", "name": "东方财富", "weight": 0.18},
        {"symbol": "601995", "name": "中金公司", "weight": 0.15},
        {"symbol": "600999", "name": "招商证券", "weight": 0.15},
    ],
}


@skill(
    "portfolio_risk_review",
    "组合集中度诊断：分析 5 只 A 股组合的个股集中度、行业集中度、相关性与事件暴露，"
    "并给出分阶段行动建议（不直接替用户下单）。当题面未给持仓细节时，使用默认高集中度组合并明确假设。",
    {
        "type": "object",
        "properties": {
            "portfolio_name": {"type": "string", "description": "组合名称，可选"},
        },
        "required": [],
        "additionalProperties": False,
    },
    category="skill",
    composes=["market_research"],
)
async def portfolio_risk_review(portfolio_name: str | None = None) -> dict:
    portfolio = dict(_PORTFOLIO_REVIEW_DEFAULT)
    if portfolio_name:
        portfolio["name"] = portfolio_name
    holdings = portfolio["holdings"]
    market_results: list[dict] = []
    for item in holdings:
        market_results.append(await execute_skill("market_research", {
            "symbol": item["symbol"],
            "lookback_days": 90,
            "focus": ["price"],
        }))
    artifacts = _collect_artifacts(market_results)
    hhi = round(sum((item["weight"] * 100) ** 2 for item in holdings), 2)
    top3_weight = round(sum(item["weight"] for item in holdings[:3]) * 100, 2)
    return_maps = [_daily_returns(_extract_price_rows(result)) for result in market_results]
    corrs: list[float] = []
    corr_rows: list[list[Any]] = []
    for i, left in enumerate(holdings):
        row = [left["name"]]
        for j, right in enumerate(holdings):
            if i == j:
                row.append(1.0)
                continue
            corr = _pairwise_corr(return_maps[i], return_maps[j])
            row.append(corr)
            if corr is not None and j > i:
                corrs.append(corr)
        corr_rows.append(row)
    avg_corr = round(sum(corrs) / len(corrs), 4) if corrs else None
    event_exposures = [
        {"risk": "资本市场成交额下行", "impact": "券商 beta 同步下修，组合净值波动放大", "evidence_level": "强"},
        {"risk": "监管政策收紧", "impact": "同业盈利预期同步回落，缺少对冲资产", "evidence_level": "中强"},
        {"risk": "单一行业舆情或黑天鹅", "impact": "组合回撤会因高相关性被放大", "evidence_level": "强"},
    ]
    phased_actions = [
        "第 1 阶段（立即）：暂停继续增配同一行业，设定单一个股和单一行业上限，只做风险盘点不直接交易。",
        "第 2 阶段（1-2 周）：把至少 20%-30% 风险预算转向低相关行业，如公用事业、消费或医药，降低组合 beta 聚集。",
        "第 3 阶段（持续）：按周复核相关性矩阵与行业事件日历，若平均相关性再次升破 0.7，触发再平衡讨论。",
    ]
    table = {
        "kind": "table",
        "title": f"{portfolio['name']} 风险诊断",
        "payload": {
            "columns": ["name", "symbol", "weight_pct", "industry"],
            "rows": [[item["name"], item["symbol"], round(item["weight"] * 100, 2), portfolio["industry"]] for item in holdings],
            "note": "题面未提供具体持仓，已按默认高集中度券商组合演示",
        },
    }
    corr_table = {
        "kind": "table",
        "title": f"{portfolio['name']} 近 90 日相关性矩阵",
        "payload": {
            "columns": ["name", *[item["name"] for item in holdings]],
            "rows": corr_rows,
        },
    }
    artifacts.extend([table, corr_table])
    return ok(
        {
            "portfolio_name": portfolio["name"],
            "assumed_portfolio": True,
            "industry": portfolio["industry"],
            "holdings": holdings,
            "metrics": {
                "hhi": hhi,
                "top3_weight_pct": top3_weight,
                "industry_concentration_pct": 100.0,
                "avg_pairwise_corr_90d": avg_corr,
            },
            "event_exposures": event_exposures,
            "phased_actions": phased_actions,
        },
        meta("portfolio_risk_review", len(holdings)),
        artifacts=artifacts or None,
    ) | {"note": "题面未给出具体持仓，已按『默认券商集中组合』演示诊断方法"}


_CHAIN_PRESETS: dict[str, dict[str, Any]] = {
    "碳酸锂": {
        "aliases": ["锂", "碳酸锂", "锂盐"],
        "rows": [
            {
                "layer": "上游资源",
                "symbol": "002460",
                "name": "赣锋锂业",
                "revenue_impact": "锂价上行通常直接抬升锂盐售价与资源利润弹性",
                "cost_impact": "开采与加工成本相对刚性，利润弹性大于成本压力",
                "inventory_impact": "高价周期更容易形成库存重估收益",
                "bargaining_power": "上游资源稀缺时议价权增强",
                "direction": "正向",
                "lag": "0-1 个季度",
                "evidence_level": "强",
            },
            {
                "layer": "中游电池",
                "symbol": "300750",
                "name": "宁德时代",
                "revenue_impact": "收入端受整车需求拉动更大，原料涨价本身不直接增收",
                "cost_impact": "原料涨价先压缩毛利，随后通过调价条款逐步传导",
                "inventory_impact": "备货充足时可短暂缓冲成本冲击，去库期压力更大",
                "bargaining_power": "龙头可向客户部分传导，但滞后于上游",
                "direction": "先负后平",
                "lag": "1-2 个季度",
                "evidence_level": "中强",
            },
            {
                "layer": "下游整车",
                "symbol": "002594",
                "name": "比亚迪",
                "revenue_impact": "终端需求决定收入，原料涨价本身不带来收入提升",
                "cost_impact": "成本端承压最明显，若终端竞争激烈则难以及时提价",
                "inventory_impact": "整车库存与渠道去化会放大原料波动的利润影响",
                "bargaining_power": "对消费者提价能力受竞争格局约束，弱于中上游",
                "direction": "负向",
                "lag": "2-3 个季度",
                "evidence_level": "中",
            },
        ],
    }
}


def _resolve_chain_material(material: str | None) -> tuple[str, bool]:
    raw = (material or "").strip()
    if not raw:
        return "碳酸锂", True
    for canonical, info in _CHAIN_PRESETS.items():
        if any(token and token in raw for token in [canonical, *info.get("aliases", [])]):
            return canonical, canonical != raw
    return "碳酸锂", True


@skill(
    "industry_chain_transmission",
    "产业链传导分析：围绕某项原材料价格变化，输出 A 股三层产业链公司的收入、成本、库存、议价权传导，"
    "并给出方向、时滞与证据等级。题面未指明原材料时默认按『碳酸锂』处理。",
    {
        "type": "object",
        "properties": {
            "material": {"type": "string", "description": "原材料名称，可选，如 碳酸锂 / 铜 / 原油"},
        },
        "required": [],
        "additionalProperties": False,
    },
    category="skill",
    composes=["stock_overview"],
)
async def industry_chain_transmission(material: str | None = None) -> dict:
    canonical, assumed = _resolve_chain_material(material)
    preset = _CHAIN_PRESETS[canonical]
    overview_results: list[dict] = []
    for row in preset["rows"]:
        overview_results.append(await execute_skill("stock_overview", {"keyword": row["name"]}))
    artifacts = _collect_artifacts(overview_results)
    rows = []
    for row in preset["rows"]:
        rows.append({
            **row,
            "trading_implication": f"{row['layer']} 对 {canonical} 涨价的传导方向为 {row['direction']}，主要时滞 {row['lag']}",
        })
    summary_table = {
        "kind": "table",
        "title": f"{canonical} 价格变化的三层传导",
        "payload": {
            "columns": ["layer", "name", "symbol", "direction", "lag", "evidence_level"],
            "rows": [[row["layer"], row["name"], row["symbol"], row["direction"], row["lag"], row["evidence_level"]] for row in rows],
            "note": "题面未指定原材料时，默认按碳酸锂链条处理",
        },
    }
    artifacts.append(summary_table)
    return ok(
        {
            "material": canonical,
            "material_assumed": assumed,
            "rows": rows,
            "core_judgement": [
                "上游资源股通常最先受益于原材料涨价，利润弹性和库存重估最明显。",
                "中游制造环节先承受成本压力，再通过调价与产品结构优化逐步传导。",
                "下游终端企业的提价能力最弱，传导最慢，受终端竞争约束最大。",
            ],
        },
        meta("industry_chain_transmission", len(rows)),
        artifacts=artifacts or None,
    ) | ({"note": "题面未明确原材料，已按『碳酸锂』默认假设执行"} if assumed else {})


# ============================================================== stock_overview
# 概览：先 search_stock 找到 symbol，再返回基础信息（财务摘要）

@skill(
    "stock_overview",
    "股票概览：显式 symbol 优先本地解析；只有 symbol 缺省时才按 keyword 搜索，"
    "随后拉取财务摘要 + 最新行情。支持 A股/ETF/指数及美股 ticker。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "明确证券代码（优先），如 SH513080/600000/MDT"},
            "keyword": {"type": "string", "description": "公司名 / 关键词（symbol 缺省时使用）"},
            "market": {"type": "string", "description": "可选市场提示：CN/US/A股/美股"},
        },
        "required": [],
        "anyOf": [{"required": ["symbol"]}, {"required": ["keyword"]}],
        "additionalProperties": False,
    },
    category="skill",
    composes=["resolve_security", "get_financial_abstract", "get_stock_daily",
              "get_us_stock_spot", "get_us_stock_info",
              "get_us_stock_finance", "get_us_stock_indicator",
              "get_us_stock_calendar"],
)
async def stock_overview(keyword: str | None = None, symbol: str | None = None,
                         market: str | None = None) -> dict:
    resolve_args = {k: v for k, v in {
        "symbol": symbol, "keyword": keyword, "market": market,
    }.items() if v not in (None, "")}
    r = await execute_skill("resolve_security", resolve_args)
    primary = r.get("data") if isinstance(r, dict) else None
    if not r.get("ok") or not isinstance(primary, dict):
        return err(str(r.get("error") or f"未找到股票: {keyword or symbol}"))
    code = str(primary.get("symbol") or "").strip()
    market_code = str(primary.get("market") or "").strip().upper()
    if not code or market_code not in {"CN", "US"}:
        return err(f"解析结果不完整: {primary}")
    is_us = market_code == "US"
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    # 美股没有 A 股财务摘要；并行拉取实时行情 + 基本信息 + 财务三表(资产负债表) + 财务指标 + 日K
    if is_us:
        tasks = [
            ("get_us_stock_spot",     {"symbol": code}),
            ("get_us_stock_info",     {"symbol": code}),
            ("get_us_stock_finance",  {"symbol": code, "report_type": "资产负债表", "indicator": "年报"}),
            ("get_us_stock_indicator", {"symbol": code, "indicator": "年报"}),
            ("get_us_stock_calendar", {"symbol": code}),
            ("get_stock_daily",       {"symbol": code, "start_date": start, "end_date": end, "adjust": "qfq"}),
        ]
    else:
        tasks = [
            ("get_financial_abstract", {"symbol": code}),
            ("get_stock_daily", {"symbol": code, "start_date": start, "end_date": end, "adjust": "qfq"}),
        ]
    results = await _gather_sub(tasks)
    summary = _summarize_subs(results)
    summary["composed"] = ["resolve_security"] + [n for n, _ in tasks]
    summary["market"] = "美股" if is_us else "A股"
    sub_arts = _collect_artifacts(results)
    # search_stock 的 artifact 也捎上
    if r.get("artifact"):
        sub_arts = [r["artifact"]] + sub_arts
    return _finalize_composite(ok(
        {"keyword": keyword, "resolved_symbol": code, "market": market_code,
         "security_kind": primary.get("kind"),
         "resolution_source": primary.get("resolution_source"),
         "matches": [{"name": primary.get("name") or keyword or code,
                      "symbol": code, "market": "美股" if is_us else "A股"}], **summary},
        meta("stock_overview", len(tasks) + 1),
        artifacts=sub_arts or None,
    ), summary, "stock_overview")


@skill(
    "evidence_ledger",
    "证据台账：汇总标的的公告与新闻为统一表格（去重、时间线），并附带行情摘要（MA20/60/120 等）。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A 股 6 位代码或美股 ticker（可选）"},
            "keyword": {"type": "string", "description": "标的名称关键词（symbol 缺省时使用，可选）"},
            "lookback_days": {"type": "integer", "description": "行情回溯天数，默认 60"},
            "limit": {"type": "integer", "description": "最多保留证据条数，默认 20"},
        },
        "required": [],
        "additionalProperties": False,
    },
    category="skill",
    composes=[
        "stock_overview",
        "llm_web_latest",
        "market_research",
        "get_stock_news",
        "get_global_news",
        "get_announcements",
        "get_us_stock_news",
        "get_us_stock_sec_filings",
        "dedupe_event_candidates",
        "build_event_timeline",
        "score_evidence_items",
    ],
)
async def evidence_ledger(symbol: str | None = None,
                          keyword: str | None = None,
                          lookback_days: int = 60,
                          limit: int = 20) -> dict:
    raw_symbol = (symbol or "").strip()
    resolved_symbol: str | None = raw_symbol or None
    resolved_market: str | None = None
    resolved_name: str | None = (keyword or "").strip() or None
    artifacts: list[dict] = []
    if not resolved_symbol and keyword:
        r = await execute_skill("stock_overview", {"keyword": keyword})
        if r.get("ok"):
            payload = r.get("data") or {}
            resolved_symbol = payload.get("resolved_symbol") or None
            resolved_market = payload.get("market") or None
            matches = payload.get("matches") or []
            if matches and isinstance(matches, list) and isinstance(matches[0], dict):
                resolved_name = str(matches[0].get("name") or resolved_name or "").strip() or resolved_name
            artifacts.extend(_collect_artifacts([r]))
        else:
            return r
    us = bool(resolved_symbol) and is_us_symbol(resolved_symbol or "")
    code = "".join(ch for ch in (resolved_symbol or "") if ch.isdigit())[-6:] if resolved_symbol else ""
    if resolved_symbol and not us and len(code) != 6:
        return err(f"symbol 不合法: {resolved_symbol}")
    mr = await execute_skill("market_research", {
        "symbol": (resolved_symbol.upper() if us else code),
        "lookback_days": int(lookback_days or 60),
        "focus": ["price"],
    }) if resolved_symbol else None
    if isinstance(mr, dict):
        artifacts.extend(_collect_artifacts([mr]))
    items: list[dict] = []
    query_target = resolved_name or (resolved_symbol.upper() if us else (code or resolved_symbol or keyword or ""))
    if query_target:
        llm_first = await execute_skill("llm_web_latest", {
            "query": f"{query_target} 最新公告 新闻 事件",
            "limit": max(6, min(int(limit or 20), 20)),
        })
        if isinstance(llm_first, dict) and llm_first.get("ok") and isinstance(llm_first.get("data"), list) and llm_first["data"]:
            items.extend(llm_first["data"])
            artifacts.extend(_collect_artifacts([llm_first]))
    if resolved_symbol:
        if not items:
            if us:
                n1 = await execute_skill("get_us_stock_news", {"symbol": resolved_symbol.upper(), "count": max(6, limit)})
                n2 = await execute_skill("get_us_stock_sec_filings", {"symbol": resolved_symbol.upper(), "count": max(6, limit)})
                for r in (n1, n2):
                    if isinstance(r, dict) and r.get("ok") and isinstance(r.get("data"), list):
                        for it in r["data"]:
                            if isinstance(it, dict):
                                it2 = dict(it)
                                it2["kind"] = it2.get("kind") or ("announcement" if r is n2 else "news")
                                items.append(it2)
                artifacts.extend(_collect_artifacts([n1, n2]))
            else:
                n1 = await execute_skill("get_stock_news", {"symbol": code, "limit": max(8, min(10, limit))})
                n2 = await execute_skill("get_announcements", {"keyword": code, "limit": max(8, min(10, limit))})
                for r in (n1, n2):
                    if isinstance(r, dict) and r.get("ok") and isinstance(r.get("data"), list):
                        for it in r["data"]:
                            if isinstance(it, dict):
                                it2 = dict(it)
                                it2["kind"] = it2.get("kind") or ("announcement" if r is n2 else "news")
                                items.append(it2)
                artifacts.extend(_collect_artifacts([n1, n2]))
    if not items:
        items = []
    deduped = await execute_skill("dedupe_event_candidates", {"items": items, "limit": max(1, int(limit or 20))})
    if not isinstance(deduped, dict) or not deduped.get("ok"):
        return deduped if isinstance(deduped, dict) else err("去重失败")
    scored = await execute_skill("score_evidence_items", {"items": deduped.get("data") or []})
    if not isinstance(scored, dict) or not scored.get("ok"):
        return scored if isinstance(scored, dict) else err("证据打标失败")
    timeline = await execute_skill("build_event_timeline", {"items": scored.get("data") or [], "limit": max(1, int(limit or 20))})
    if not isinstance(timeline, dict) or not timeline.get("ok"):
        return timeline if isinstance(timeline, dict) else err("时间线构建失败")
    rows = timeline.get("data") or []
    price_metrics = (mr.get("data") or {}).get("price_metrics") if isinstance(mr, dict) and mr.get("ok") else {}
    table = {
        "kind": "table",
        "title": f"{(resolved_symbol or keyword or '标的')} 证据台账（{len(rows)} 条）",
        "payload": {
            "columns": ["date", "kind", "label", "title", "source", "url"],
            "rows": [[r.get("date"), r.get("kind"), r.get("label"), r.get("title"), r.get("source"), r.get("url")] for r in rows],
            "note": "证据条目已按日期+标题去重并按时间倒序；label 为系统打标（fact/context），仅用于研究汇总。",
        },
    }
    artifacts.append(table)
    return ok(
        {
            "symbol": (resolved_symbol.upper() if us else (code or resolved_symbol or None)),
            "market": ("美股" if us else ("A股" if (code or resolved_market == "A股") else (resolved_market or None))),
            "price_metrics": price_metrics,
            "rows": rows,
        },
        meta("evidence_ledger", len(rows)),
        artifacts=artifacts or None,
    )


@skill(
    "announcement_onepager",
    "公告/事件一页纸：拉取公告+新闻并整理时间线，输出事实摘要字段（不编造数字），适合 router 直接综合成最终解读。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A 股 6 位代码或美股 ticker（可选）"},
            "keyword": {"type": "string", "description": "标的名称关键词（symbol 缺省时使用，可选）"},
            "limit": {"type": "integer", "description": "最多保留条数，默认 12"},
        },
        "required": [],
        "additionalProperties": False,
    },
    category="skill",
    composes=[
        "stock_overview",
        "llm_web_latest",
        "get_stock_news",
        "get_announcements",
        "get_us_stock_news",
        "get_us_stock_sec_filings",
        "dedupe_event_candidates",
        "build_event_timeline",
        "score_evidence_items",
    ],
)
async def announcement_onepager(symbol: str | None = None,
                                keyword: str | None = None,
                                limit: int = 12) -> dict:
    raw_symbol = (symbol or "").strip()
    resolved_symbol: str | None = raw_symbol or None
    resolved_market: str | None = None
    resolved_name: str | None = (keyword or "").strip() or None
    artifacts: list[dict] = []
    if not resolved_symbol and keyword:
        r = await execute_skill("stock_overview", {"keyword": keyword})
        if r.get("ok"):
            payload = r.get("data") or {}
            resolved_symbol = payload.get("resolved_symbol") or None
            resolved_market = payload.get("market") or None
            matches = payload.get("matches") or []
            if matches and isinstance(matches, list) and isinstance(matches[0], dict):
                resolved_name = str(matches[0].get("name") or resolved_name or "").strip() or resolved_name
            artifacts.extend(_collect_artifacts([r]))
        else:
            return r
    if not resolved_symbol:
        return err("请提供 symbol 或 keyword")
    us = is_us_symbol(resolved_symbol)
    code = "".join(ch for ch in resolved_symbol if ch.isdigit())[-6:] if not us else ""
    if not us and len(code) != 6:
        return err(f"symbol 不合法: {resolved_symbol}")
    items: list[dict] = []
    query_target = resolved_name or (resolved_symbol.upper() if us else code)
    llm_first = await execute_skill("llm_web_latest", {
        "query": f"{query_target} 最新公告 新闻 事件",
        "limit": max(8, int(limit or 12)),
    })
    if isinstance(llm_first, dict) and llm_first.get("ok") and isinstance(llm_first.get("data"), list) and llm_first["data"]:
        items.extend(llm_first["data"])
        artifacts.extend(_collect_artifacts([llm_first]))
    else:
        if us:
            n1 = await execute_skill("get_us_stock_news", {"symbol": resolved_symbol.upper(), "count": max(8, int(limit or 12))})
            n2 = await execute_skill("get_us_stock_sec_filings", {"symbol": resolved_symbol.upper(), "count": max(8, int(limit or 12))})
            for r in (n1, n2):
                if isinstance(r, dict) and r.get("ok") and isinstance(r.get("data"), list):
                    for it in r["data"]:
                        if isinstance(it, dict):
                            it2 = dict(it)
                            it2["kind"] = it2.get("kind") or ("announcement" if r is n2 else "news")
                            items.append(it2)
            artifacts.extend(_collect_artifacts([n1, n2]))
        else:
            n1 = await execute_skill("get_announcements", {"keyword": code, "limit": max(8, min(10, int(limit or 12)))})
            n2 = await execute_skill("get_stock_news", {"symbol": code, "limit": max(8, min(10, int(limit or 12)))})
            for r in (n1, n2):
                if isinstance(r, dict) and r.get("ok") and isinstance(r.get("data"), list):
                    for it in r["data"]:
                        if isinstance(it, dict):
                            it2 = dict(it)
                            it2["kind"] = it2.get("kind") or ("announcement" if r is n1 else "news")
                            items.append(it2)
            artifacts.extend(_collect_artifacts([n1, n2]))
    deduped = await execute_skill("dedupe_event_candidates", {"items": items, "limit": max(1, int(limit or 12))})
    if not isinstance(deduped, dict) or not deduped.get("ok"):
        return deduped if isinstance(deduped, dict) else err("去重失败")
    scored = await execute_skill("score_evidence_items", {"items": deduped.get("data") or []})
    if not isinstance(scored, dict) or not scored.get("ok"):
        return scored if isinstance(scored, dict) else err("证据打标失败")
    timeline = await execute_skill("build_event_timeline", {"items": scored.get("data") or [], "limit": max(1, int(limit or 12))})
    if not isinstance(timeline, dict) or not timeline.get("ok"):
        return timeline if isinstance(timeline, dict) else err("时间线构建失败")
    rows = timeline.get("data") or []
    table = {
        "kind": "table",
        "title": f"{resolved_symbol} 事件时间线（{len(rows)} 条）",
        "payload": {
            "columns": ["date", "kind", "title", "source", "url"],
            "rows": [[r.get("date"), r.get("kind"), r.get("title"), r.get("source"), r.get("url")] for r in rows],
        },
    }
    artifacts.append(table)
    return ok(
        {
            "symbol": resolved_symbol.upper() if us else code,
            "market": "美股" if us else ("A股" if resolved_market in (None, "A股") else (resolved_market or "A股")),
            "timeline": rows,
        },
        meta("announcement_onepager", len(rows)),
        artifacts=artifacts or None,
    )
