"""Skill result TTL cache (design.md §11 性能与限流).

- 仅缓存 `ok=True` 的结果（错误让调用方重试，规避瞬时网络问题被永久化）
- profile → TTL 预设，按数据时效性区分（财报 6h / 行业列表 1h / 资金流 2min）
- key 由 (skill_name, args, kwargs) 哈希生成；同参数同 TTL 内直接返回缓存
- 线程安全；LRU 简化版（满容量时按 expire_at 最早淘汰）
- 支持 `PRONOIA_CACHE_DISABLE=1` 环境变量整库关闭
- 暴露 `stats()` 与 `clear()` 供 `/api/cache/stats` 与调试使用
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import threading
import time
from typing import Any, Callable

# ---------------------------------------------------------------- TTL profiles
# 设计原则：时效性越低的数据 TTL 越长；同质参数越稳定的越值得缓存
TTL_PROFILES: dict[str, int] = {
    # 行业 / 板块 / 股票所属
    "industry_boards":   60 * 60,         # 1h   - 行业列表结构基本不变，盘中变化以"快照"维度
    "industry_info":     60 * 30,         # 30m  - 板块实时概况（PE/PB/换手）
    "board_history":     60 * 30,         # 30m  - 历史 K 线
    "stock_industry":    60 * 60 * 24,    # 24h  - 个股所属行业
    # K 线 / 行情（team_full 同会话内多 expert 重复拉同一 symbol，30m TTL 消除重复网络请求）
    "kline":             60 * 30,         # 30m  - 个股/指数日K线
    "us_stock":          60 * 30,         # 30m  - 美股行情/财务/指标
    "sector_spot":       60 * 2,          # 2min - 板块实时快照
    # 事件研究（同一 symbol+event_date 被 event_scout/market_analyst/deep_researcher 重复调）
    "event_study":       60 * 30,         # 30m  - 事件研究法 AR/CAR
    # 股票搜索（代码映射基本不变）
    "search":            60 * 60,         # 1h   - search_stock 代码搜索
    # 资金流（盘中变化快）
    "fund_flow_rank":    60 * 2,          # 2min
    "fund_flow":         60 * 5,          # 5min
    # 异动 / 龙虎榜 / 涨停
    "board_change":      60 * 2,          # 2min
    # 财报（季报更新，6h 内不需要重拉）
    "fundamentals":      60 * 60 * 6,     # 6h
    "profit_forecast":   60 * 60 * 12,    # 12h
    # 新闻 / 公告
    "news":              60 * 5,          # 5min
    "announcements":     60 * 30,         # 30m
    # 股东 / 解禁（结构化日数据）
    "holders":           60 * 60 * 6,     # 6h
    "restricted":        60 * 60 * 24,    # 24h
    # 全球市场
    "global_markets":    60,              # 1min
    "default":           60,
}


def ttl_for(profile: str) -> int:
    return TTL_PROFILES.get(profile, TTL_PROFILES["default"])


# ----------------------------------------------------------------- store
class _Entry:
    __slots__ = ("value", "expire_at", "profile", "key")

    def __init__(self, value: dict, ttl: int, profile: str, key: str):
        self.value = value
        self.expire_at = time.time() + ttl
        self.profile = profile
        self.key = key


class TTLCache:
    def __init__(self, max_size: int = 1024):
        self._lock = threading.RLock()
        self._store: dict[str, _Entry] = {}
        self._max = max_size
        self._hits = 0
        self._misses = 0
        self._stores = 0
        self._errors_skipped = 0
        self._disabled = os.environ.get("PRONOIA_CACHE_DISABLE", "").strip() in ("1", "true", "yes")

    # ---------- introspection ----------
    @property
    def disabled(self) -> bool:
        return self._disabled

    def set_disabled(self, v: bool) -> None:
        with self._lock:
            self._disabled = bool(v)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total) if total > 0 else 0.0
            profiles: dict[str, int] = {}
            for e in self._store.values():
                profiles[e.profile] = profiles.get(e.profile, 0) + 1
            return {
                "size": len(self._store),
                "max": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "stores": self._stores,
                "errors_skipped": self._errors_skipped,
                "hit_rate": round(hit_rate, 4),
                "profiles": profiles,
                "disabled": self._disabled,
            }

    def clear(self) -> int:
        with self._lock:
            n = len(self._store)
            self._store.clear()
            return n

    # ---------- core ----------
    def get(self, key: str) -> dict | None:
        if self._disabled:
            return None
        with self._lock:
            e = self._store.get(key)
            if not e:
                self._misses += 1
                return None
            if e.expire_at < time.time():
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            return e.value

    def set(self, key: str, value: dict, ttl: int, profile: str) -> None:
        if self._disabled:
            return
        with self._lock:
            # 满容量：按 expire_at 最早淘汰
            if key not in self._store and len(self._store) >= self._max:
                victim_key = min(self._store.items(), key=lambda kv: kv[1].expire_at)[0]
                self._store.pop(victim_key, None)
            self._store[key] = _Entry(value=value, ttl=ttl, profile=profile, key=key)
            self._stores += 1


# ----------------------------------------------------------------- singleton
CACHE = TTLCache(max_size=int(os.environ.get("PRONOIA_CACHE_MAX", "1024")))


def set_cache_disabled(v: bool) -> None:
    """全局启用/禁用缓存。"""
    CACHE.set_disabled(v)


def clear_cache() -> int:
    """清空缓存，返回清理条数。"""
    return CACHE.clear()


# ----------------------------------------------------------------- key hashing
def _json_default(o: Any) -> Any:
    # 避免某些非标准对象在 json.dumps 中抛错
    try:
        return str(o)
    except Exception:
        return None


def make_key(fn_name: str, args: tuple, kwargs: dict) -> str:
    """稳定的 key：fn_name + 规范化参数。"""
    payload = {"fn": fn_name, "a": list(args), "k": kwargs}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default)
    return f"{fn_name}:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ----------------------------------------------------------------- decorator
def cached(profile: str = "default"):
    """装饰器：包装一个 skill handler，命中缓存直接返回。

    Usage:
        @skill("list_industry_boards", ...,
    internal=True,)
        @cache.cached("industry_boards")
        def list_industry_boards(...): ...

    仅缓存 `ok=True` 的结果，错误一律放行。
    """
    ttl = ttl_for(profile)

    def deco(fn: Callable[..., dict]) -> Callable[..., dict]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> dict:
            key = make_key(fn.__name__, args, kwargs)
            cached_val = CACHE.get(key)
            if cached_val is not None:
                # 标记命中（前端可识别 _cache_hit）
                if isinstance(cached_val, dict):
                    cached_val = {**cached_val}
                    meta = cached_val.get("meta") or {}
                    if isinstance(meta, dict) and not meta.get("_cache_hit"):
                        meta = {**meta, "_cache_hit": True, "_cache_ttl": ttl,
                                "_cache_profile": profile}
                        cached_val["meta"] = meta
                return cached_val
            rv = fn(*args, **kwargs)
            if isinstance(rv, dict) and rv.get("ok") is True:
                CACHE.set(key, rv, ttl, profile)
            else:
                with CACHE._lock:
                    CACHE._errors_skipped += 1
            return rv

        # 调试用：暴露 profile / key_fn
        wrapper.__pronoia_cache_profile__ = profile  # type: ignore[attr-defined]
        wrapper.__pronoia_cache_ttl__ = ttl  # type: ignore[attr-defined]
        return wrapper

    return deco
