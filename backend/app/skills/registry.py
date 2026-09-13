"""Skill registry: @skill decorator + REGISTRY (design.md §4).

三层调度模型 tool → skill → agent → team
========================================

- **Tool（atomic）**：单一原子操作，直接对接 akshare / yfinance / DB / 内部 API。
  默认对 LLM 不可见（`internal=True`），仅供 skill 内部调用。
  例如：``get_stock_daily``、``_eg_add_evidence``。

- **Skill**：可调度多个 tool 的复合能力，对 LLM 暴露为一个高层 tool。
  例如：``market_research``（内部调 get_stock_daily / get_index_daily / get_industry_fund_flow …）、
  ``evidence_graph``（dispatch 到 9 个 ``_eg_*`` sub-tool）。

- **Agent**：调度 skill（不再直接调 atomic）。
  例如：``deep_researcher`` 调 ``evidence_graph`` + 一组 research skill。

- **Team**：调度不同 agent。

LLM 视角下，agent 的 tools 列表只包含 skill + 白名单的 atomic。
Atomic 默认 internal=True；skill 默认 internal=False。
``tools_for_agent(agent_id)`` 会自动过滤掉 internal 的 atomic。

每个 handler 仍然返回统一 dict：
  {"ok": True, "data": ..., "meta": {...}, "artifact": {...} / "artifacts": [...]}
  {"ok": False, "error": "human readable"}
"""
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable


@dataclass
class SkillDef:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict]
    # 新增：分类
    category: str = "atomic"          # "atomic" | "skill"（atomic 即 internal tool；skill 聚合多 atomic，对 LLM 可见）
    internal: bool = False            # True: LLM 不可见（仅可被 skill 或代码内调用）
    composes: list[str] = field(default_factory=list)  # skill 模式下声明调用的 sub-tool id 列表（仅做文档/校验用）
    emit_artifact: bool = field(default=True)

    def openai_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, SkillDef] = {}


def skill(name: str, description: str, parameters: dict[str, Any],
          *, category: str = "atomic", internal: bool = False, composes: list[str] | None = None):
    """Register a skill handler.

    Args:
        name: skill id (e.g. "market_research" / "_eg_add_evidence")
        description: 一句话描述（暴露给 LLM）
        parameters: JSON schema
        category: "atomic" (默认) / "skill"（前者即 atomic 工具，后者聚合多 atomic 暴露给 LLM）
        internal: True 时 LLM 不可见（atomic 默认 False 保持后向兼容；新建 atomic 建议 True）
        composes: skill 模式下声明它调用的 sub-tool id 列表（仅做文档/校验用）
    """

    def deco(fn: Callable[..., dict]):
        REGISTRY[name] = SkillDef(
            name=name, description=description, parameters=parameters, handler=fn,
            category=category, internal=internal,
            composes=list(composes or []),
        )
        return fn

    return deco


def ok(data: Any, meta: dict[str, Any], artifact: dict | None = None,
       artifacts: list[dict] | None = None) -> dict:
    out: dict[str, Any] = {"ok": True, "data": data, "meta": meta}
    if artifacts:
        out["artifacts"] = artifacts
    elif artifact:
        out["artifact"] = artifact
    return out


def err(message: str) -> dict:
    return {"ok": False, "error": message}


def meta(source: str, rows: int, url: str | None = None) -> dict:
    m: dict[str, Any] = {
        "source": source,
        "rows": rows,
        "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if url:
        m["url"] = url
    return m


def json_safe(obj: Any) -> Any:
    """Convert pandas/numpy/datetime values into JSON-safe builtins."""
    import math

    import pandas as pd

    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (datetime, date, pd.Timestamp)):
        return obj.isoformat()[:10] if not isinstance(obj, datetime) else obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    # numpy scalars / others
    try:
        import numpy as np

        if isinstance(obj, np.generic):
            return json_safe(obj.item())
    except Exception:
        pass
    return str(obj)


def df_records(df, columns: list[str] | None = None, limit: int | None = None) -> tuple[list[dict], bool]:
    """DataFrame -> list[dict] (JSON-safe). Returns (records, truncated?)."""
    if columns:
        cols = [c for c in columns if c in df.columns]
        df = df[cols]
    truncated = False
    if limit is not None and len(df) > limit:
        df = df.tail(limit)
        truncated = True
    recs = json_safe(df.to_dict(orient="records"))
    return recs, truncated


def serialize_tool_result(result: dict, max_chars: int) -> str:
    """Serialize a skill result for the LLM tool message (design.md §4: ≤4000 chars)."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    # Progressively shrink `data` until it fits.
    if isinstance(result.get("data"), list) and result["data"]:
        data = result["data"]
        keep = len(data)
        while keep > 1:
            keep = keep // 2
            candidate = dict(result)
            candidate["data"] = data[:keep]
            candidate["truncated"] = True
            candidate["note"] = f"结果过大，已截断为前 {keep}/{len(data)} 条"
            text = json.dumps(candidate, ensure_ascii=False, default=str)
            if len(text) <= max_chars:
                return text
    return text[: max_chars - 60] + '..."(已截断)"}'


def tool_schema_subset(names: list[str]) -> list[dict]:
    return [REGISTRY[n].openai_tool() for n in names if n in REGISTRY]


def tools_for_agent(agent_id_or_skill_names, *, include_internal: bool = False) -> list[dict]:
    """给某个 agent 看到的 tool schema 列表。

    Args:
        agent_id_or_skill_names: agent_id 字符串（查 roster），或 skill 名列表
        include_internal: True 时不过滤 internal（仅给需要直接调 atomic 的 agent 用）

    三层模型下，agent 默认只看 skill + 白名单 atomic。
    """
    from ..agents.roster import get_agent  # 延迟导入避免循环

    if isinstance(agent_id_or_skill_names, str):
        names = get_agent(agent_id_or_skill_names)["skills"]
    else:
        names = list(agent_id_or_skill_names)

    out: list[dict] = []
    for n in names:
        sd = REGISTRY.get(n)
        if sd is None:
            continue
        if not include_internal and sd.internal:
            continue
        out.append(sd.openai_tool())
    return out


def get_skill_meta(name: str) -> dict | None:
    """返回 skill 的元信息（用于 /api/skills 等接口）。"""
    sd = REGISTRY.get(name)
    if sd is None:
        return None
    return {
        "name": sd.name,
        "description": sd.description,
        "parameters": sd.parameters,
        "category": sd.category,
        "internal": sd.internal,
        "composes": sd.composes,
    }


def ensure_skills_loaded() -> None:
    """Import skill modules so decorators run (idempotent)."""
    from . import (
        analysis, fundamentals, market, news,
        fundamentals_detail, boards, flows, holders, global_markets,
        research_methods,
        evidence_graph,  # 9 个 _eg_* sub-tool（被 skill evidence_graph 内部 dispatch）
        skill,            # 8 个高层 skill（composite 改名为 skill，对 LLM 可见）
        analyzers,        # 7 个分析推理 skill（Tier 2 信号处理 + Tier 1 市场分析思维）
        frozen_document,  # 严格 allowlist 的冻结公告正文读取
    )  # noqa: F401
