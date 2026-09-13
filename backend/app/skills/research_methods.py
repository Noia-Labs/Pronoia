"""Research-method atomic tools (internal).

These tools are internal building blocks for composite research skills.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .registry import err, meta, ok, skill


def _norm_title(title: str) -> str:
    s = (title or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)
    return s


def _parse_dt(s: str | None) -> datetime | None:
    raw = (s or "").strip()
    if not raw:
        return None
    candidates = [raw, raw[:19], raw[:10], raw[:8]]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        for cand in candidates:
            try:
                return datetime.strptime(cand, fmt)
            except Exception:
                continue
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


@skill(
    "dedupe_event_candidates",
    "对事件候选进行去重（按日期+标题规范化），返回去重后的列表。",
    {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object"}, "description": "事件候选列表"},
            "limit": {"type": "integer", "description": "最多保留条数（可选）"},
        },
        "required": ["items"],
        "additionalProperties": False,
    },
    internal=True,
)
def dedupe_event_candidates(items: list[dict], limit: int | None = None) -> dict:
    if not isinstance(items, list):
        return err("items 必须为 list")
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        date_key = str(it.get("date") or "")[:10]
        title_key = _norm_title(str(it.get("title") or ""))
        if not title_key:
            continue
        key = f"{date_key}|{title_key}"
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if limit is not None and len(out) >= int(limit):
            break
    return ok(out, meta("dedupe_event_candidates", len(out)))


@skill(
    "build_event_timeline",
    "将新闻/公告候选整理为时间线（按时间倒序），输出标准化事件行。",
    {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object"}, "description": "候选事件列表"},
            "limit": {"type": "integer", "description": "最多保留条数（可选）"},
        },
        "required": ["items"],
        "additionalProperties": False,
    },
    internal=True,
)
def build_event_timeline(items: list[dict], limit: int | None = None) -> dict:
    if not isinstance(items, list):
        return err("items 必须为 list")
    rows: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        dt = _parse_dt(str(it.get("date") or ""))
        rows.append({
            "date": (dt.isoformat(sep=" ", timespec="minutes") if dt else (str(it.get("date") or "")[:19] or None)),
            "title": title,
            "source": it.get("source") or None,
            "url": it.get("url") or None,
            "snippet": it.get("snippet") or None,
            "kind": it.get("kind") or None,
        })
    rows.sort(key=lambda r: _parse_dt(str(r.get("date") or "")) or datetime(1970, 1, 1), reverse=True)
    if limit is not None:
        rows = rows[: max(0, int(limit))]
    return ok(rows, meta("build_event_timeline", len(rows)))


@skill(
    "score_evidence_items",
    "对证据条目打标签（fact/interpretation/gap/contradiction），用于研究台账汇总。",
    {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object"}, "description": "证据条目列表"},
            "default_label": {"type": "string", "description": "默认标签（可选）"},
        },
        "required": ["items"],
        "additionalProperties": False,
    },
    internal=True,
)
def score_evidence_items(items: list[dict], default_label: str | None = None) -> dict:
    if not isinstance(items, list):
        return err("items 必须为 list")
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        src = str(it.get("source") or "").lower()
        label = (default_label or "").strip()
        if not label:
            if "notice" in src or "公告" in src or "sec" in src or "filing" in src:
                label = "fact"
            elif "news" in src or "快讯" in src or "新闻" in src:
                label = "fact"
            else:
                label = "context"
        it2 = dict(it)
        it2["label"] = label
        out.append(it2)
    return ok(out, meta("score_evidence_items", len(out)))


@skill(
    "llm_web_latest",
    "调用 LLM 联网搜索获取最新公开信息；若返回为空，由上层 skill 自行回退内置数据源。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索问题或主题"},
            "limit": {"type": "integer", "description": "最多保留条数，默认 8"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    internal=True,
)
async def llm_web_latest(query: str, limit: int = 8) -> dict:
    from ..llm import complete_json_with_web_search

    q = (query or "").strip()
    if not q:
        return err("query 不能为空")
    obj = await complete_json_with_web_search(
        "你是金融研究助手。请优先用联网搜索获取最新公开信息，只输出 JSON 对象。"
        "格式必须为 {\"items\": [{\"title\": str, \"date\": str|null, \"source\": str|null, "
        "\"url\": str|null, \"snippet\": str|null, \"kind\": str|null}], \"summary\": str|null}。"
        "items 最多 8 条；拿不到时返回 {\"items\": []}，不要输出 markdown。",
        f"请围绕这个查询抓取最新公开信息：{q}",
        max_keywords=3,
        max_output_tokens=2200,
    )
    if not isinstance(obj, dict):
        return ok([], meta("ark.responses.web_search", 0)) | {"note": "LLM 联网搜索未返回结构化结果"}
    raw_items = obj.get("items") or []
    items: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        items.append({
            "title": title,
            "date": (str(it.get("date") or "").strip() or None),
            "source": (str(it.get("source") or "").strip() or None),
            "url": (str(it.get("url") or "").strip() or None),
            "snippet": (str(it.get("snippet") or "").strip() or None),
            "kind": (str(it.get("kind") or "").strip() or "news"),
        })
        if len(items) >= max(1, min(int(limit or 8), 20)):
            break
    artifact = {
        "kind": "evidence",
        "title": f"LLM 联网搜索结果（{len(items)} 条）",
        "payload": {"query": q, "items": items, "summary": obj.get("summary")},
    }
    return ok(items, meta("ark.responses.web_search", len(items)), artifact=artifact) | (
        {"note": "LLM 联网搜索无结果"} if not items else {}
    )
