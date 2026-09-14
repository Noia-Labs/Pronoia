"""Meta endpoints: /api/health · /api/skills · /api/agents · /api/cache/* (design.md §8)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from .. import config
from ..agents.roster import roster_public
from ..llm import complete_json_with_web_search
from ..skills.cache import CACHE, TTL_PROFILES, clear_cache, set_cache_disabled
from ..skills.registry import REGISTRY, ensure_skills_loaded

router = APIRouter(prefix="/api", tags=["meta"])


_VALID_MODES = {"auto", "agent", "team"}
_VALID_ICON_HINTS = {"newspaper", "sparkles", "trending", "landmark", "candlestick", "users"}
_VALID_AGENTS = {"event_scout", "market_analyst", "fundamentals_analyst", "predictor"}


def _fallback_suggestions() -> list[dict]:
    return [
        {
            "text": "看下今天的财经快讯",
            "mode": "auto",
            "icon_hint": "newspaper",
            "desc": "全网快讯筛高影响事件",
            "query": "看下今天的重要财经快讯，筛出 3 条对市场影响最大的",
        },
        {
            "text": "分析 A 股今日整体行情",
            "mode": "auto",
            "icon_hint": "candlestick",
            "desc": "指数 + 板块 + 龙虎榜",
            "query": "分析今天 A 股整体行情：主要指数、领涨/领跌板块、龙虎榜异动",
        },
        {
            "text": "用事件猎手扫高影响事件",
            "mode": "agent",
            "agent": "event_scout",
            "icon_hint": "sparkles",
            "desc": "事件猎手 · 新闻 + 公告 + 异动",
            "query": "扫描最近 24 小时的财经新闻和公告，筛出 5 条对市场影响最大的事件",
        },
        {
            "text": "用预测员推演茅台后市",
            "mode": "agent",
            "agent": "predictor",
            "icon_hint": "trending",
            "desc": "预测员 · 3 档情景 + 概率 + 催化",
            "query": "用世界模型推演 600519 贵州茅台后市的 3 种情景（乐观/中性/悲观）以及概率和关键催化",
        },
        {
            "text": "团队研究：分析军工板块异动",
            "mode": "team",
            "icon_hint": "users",
            "desc": "研究团队 · 多专家并行 + 复核",
            "query": "今天军工板块异动原因分析：涉及个股、产业链传导、是否可持续（团队模式）",
        },
        {
            "text": "团队研究：对英伟达做深度研究",
            "mode": "team",
            "icon_hint": "users",
            "desc": "研究团队 · 证据图谱沉淀",
            "query": "对 NVDA 英伟达做深度研究：近期财报、AI 需求、竞争格局、估值（团队模式）",
        },
    ]


def _normalize_suggestions(items: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        mode = str(item.get("mode") or "").strip()
        if mode not in _VALID_MODES:
            continue
        icon_hint = str(item.get("icon_hint") or "").strip()
        if icon_hint not in _VALID_ICON_HINTS:
            icon_hint = "newspaper" if mode == "auto" else ("users" if mode == "team" else "sparkles")
        text = str(item.get("text") or "").strip()
        desc = str(item.get("desc") or "").strip()
        query = str(item.get("query") or "").strip()
        if not text or not desc or not query:
            continue
        row = {
            "text": text[:40],
            "mode": mode,
            "icon_hint": icon_hint,
            "desc": desc[:60],
            "query": query[:180],
        }
        if mode == "agent":
            agent = str(item.get("agent") or "").strip()
            if agent not in _VALID_AGENTS:
                continue
            row["agent"] = agent
        out.append(row)
    return out


@router.get("/health")
def health():
    return {"ok": True, "llm": "configured" if config.LLM_API_KEY else "missing_api_key"}


@router.get("/skills")
def skills():
    ensure_skills_loaded()
    return [
        {
            "name": s.name,
            "description": s.description,
            "parameters": s.parameters,
            "category": s.category,        # "atomic" | "skill"
            "internal": s.internal,        # True: LLM 不可见
            "composes": list(s.composes),  # skill 声明调用的 sub-tool id
        }
        for s in REGISTRY.values()
    ]


@router.get("/agents")
def agents():
    return roster_public()


@router.get("/suggestions")
async def suggestions_refresh():
    fallback = _fallback_suggestions()
    try:
        obj = await asyncio.wait_for(
            complete_json_with_web_search(
                "你是 Pronoia 的推荐生成器。请先用联网搜索获取最新财经/市场热点，再生成 6 条适合金融研究工作台首页展示的推荐问题。"
                "必须只输出 JSON 对象，格式为 {\"items\": [ ... ]}。"
                "要求："
                "1) 恰好 6 条；"
                "2) mode 分布必须为 2 条 auto、2 条 agent、2 条 team；"
                "3) agent 只能是 event_scout / market_analyst / fundamentals_analyst / predictor；"
                "4) 每条必须包含 text/mode/icon_hint/desc/query，agent 仅 agent 模式需要；"
                "5) 内容要尽量贴合最新热点，不要输出空泛模板。",
                "请根据最近的中国市场、美股科技、宏观与政策热点，生成一组新的首页推荐问题。",
                max_keywords=4,
                max_output_tokens=2600,
            ),
            timeout=2.5,
        )
        normalized = _normalize_suggestions((obj or {}).get("items") if isinstance(obj, dict) else None)
        if len(normalized) == 6:
            counts = {
                "auto": sum(1 for x in normalized if x["mode"] == "auto"),
                "agent": sum(1 for x in normalized if x["mode"] == "agent"),
                "team": sum(1 for x in normalized if x["mode"] == "team"),
            }
            if counts == {"auto": 2, "agent": 2, "team": 2}:
                return {"items": normalized, "source": "llm_web_search", "fallback": False}
    except Exception:
        pass
    return {"items": fallback, "source": "fallback_static", "fallback": True}


# ------------------------------------------------------------ 缓存管理 ---


@router.get("/cache/stats")
def cache_stats():
    """缓存命中统计：size / hits / misses / hit_rate / 各 profile 数量。"""
    stats = CACHE.stats()
    stats["ttl_profiles"] = TTL_PROFILES
    return stats


@router.post("/cache/clear")
def cache_clear():
    """手动清空缓存（调试 / 强制刷新用）。"""
    return {"cleared": clear_cache()}


@router.post("/cache/toggle")
def cache_toggle(disabled: bool = Query(False, description="True=禁用缓存，False=启用")):
    """运行时切换缓存开关（仅供调试）。环境变量 FEVER_CACHE_DISABLE 也可在启动时禁用。"""
    set_cache_disabled(disabled)
    return {"disabled": disabled}
