from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

from .. import config, db
from ..event_backtest.collector import (
    _call_with_timeout,
    collect_cn_announcement_seeds,
    collect_macro_calendar_seeds,
    collect_us_sec_seeds,
)
from ..event_backtest.engine import run_team_prompt
from ..skills.price_data import PriceFetchError, fetch_price_frame
from ..event_backtest.models import EventRecord
from ..llm import complete_json


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def _iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse_iso(value: str) -> dt.datetime:
    raw = str(value or "").strip()
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
            try:
                parsed = dt.datetime.strptime(raw[:10], fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _add_business_days(value: dt.datetime, days: int) -> dt.datetime:
    """Add Monday-Friday workdays while preserving the local capture time."""
    current = value
    remaining = max(0, int(days))
    while remaining:
        current += dt.timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _business_dates_lookback(value: dt.datetime, days: int) -> list[str]:
    """Return the most recent Monday-Friday dates up to the configured capture date."""
    current = value.date()
    result: list[str] = []
    while len(result) < max(1, int(days)):
        if current.weekday() < 5:
            result.append(current.strftime("%Y%m%d"))
        current -= dt.timedelta(days=1)
    return list(reversed(result))


@lru_cache(maxsize=4)
def _known_trade_dates(year: int) -> set[dt.date]:
    """Load the A-share trading calendar when available; fall back safely to weekdays."""
    try:
        frame = _call_with_timeout(
            ak.tool_trade_date_hist_sina,
            timeout_s=10,
            label=f"trade_calendar:{year}",
        )
        if frame is None or len(frame) == 0:
            return set()
        column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
        dates = {dt.date.fromisoformat(str(item)[:10]) for item in frame[column].tolist() if str(item)[:10]}
        return {value for value in dates if value.year == year}
    except Exception:
        return set()


def _trading_dates_lookback(value: dt.datetime, days: int, *, include_current: bool = True) -> list[str]:
    current = value.date()
    if not include_current:
        current -= dt.timedelta(days=1)
    result: list[str] = []
    while len(result) < max(1, int(days)):
        calendar = _known_trade_dates(current.year)
        if current in calendar or (not calendar and current.weekday() < 5):
            result.append(current.strftime("%Y%m%d"))
        current -= dt.timedelta(days=1)
    return list(reversed(result))


def _add_trading_days(value: dt.datetime, days: int) -> dt.datetime:
    remaining = max(0, int(days))
    current = value
    while remaining:
        current += dt.timedelta(days=1)
        calendar = _known_trade_dates(current.year)
        is_trade = current.date() in calendar if calendar else current.weekday() < 5
        if is_trade:
            remaining -= 1
    return current


def _is_trading_date(value: dt.date) -> bool:
    calendar = _known_trade_dates(value.year)
    return value in calendar if calendar else value.weekday() < 5


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _git_commit() -> str:
    try:
        root = Path(__file__).resolve().parents[3]
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=2).strip()
    except Exception:
        return "unknown"


def _sample_events() -> list[EventRecord]:
    today = _now().date().isoformat()
    return [EventRecord.from_dict({
        "event_id": f"sample_{today.replace('-', '')}_001",
        "market": "US",
        "symbol": "SPY",
        "event_time": f"{today}T09:00:00+08:00",
        "event_type_l2": "公司指引上调/下调",
        "title": "Sample prospective assertion",
        "event_text": "示例任务：未来观察窗口内，标的相对基准的异常收益方向将被结算。",
        "source_url": "https://example.com/prospective-sample",
        "benchmark": "SPY",
    })]


def discover_events(source_config: dict[str, Any], *, as_of: dt.datetime | None = None,
                    diagnostics: list[dict] | None = None) -> list[EventRecord]:
    source = str(source_config.get("source") or "sample").lower()
    cutoff = as_of or _now()
    if source == "sample":
        return _sample_events()
    if source == "cn_announcements":
        dates = source_config.get("dates") or _trading_dates_lookback(
            cutoff,
            int(source_config.get("lookback_trade_days") or 3),
            include_current=bool(source_config.get("include_capture_day", False)),
        )
        events = collect_cn_announcement_seeds(
            dates=[str(x) for x in dates],
            keywords=source_config.get("keywords"),
            limit_per_query=int(source_config.get("limit_per_query") or 30),
            diagnostics=diagnostics,
        )
        symbols = {str(value).strip()[-6:] for value in (source_config.get("symbols") or []) if str(value).strip()}
        return [event for event in events if not symbols or event.symbol in symbols]
    if source == "us_sec":
        return collect_us_sec_seeds(
            symbols=source_config.get("symbols") or ["AAPL", "NVDA", "MSFT"],
            count_per_symbol=int(source_config.get("count_per_symbol") or 10),
            diagnostics=diagnostics,
        )
    if source == "macro":
        return collect_macro_calendar_seeds(limit=int(source_config.get("limit") or 30))
    raise ValueError(f"unsupported prospective source: {source}")


def _published_at_before(value: str, cutoff: dt.datetime, *, policy: str = "timestamp_verified") -> bool:
    try:
        parsed = _parse_iso(value)
        raw = str(value or "")
        if policy == "before_capture_date" and "T" not in raw and " " not in raw:
            return parsed.date() < cutoff.date()
        return parsed <= cutoff
    except (TypeError, ValueError):
        return False


def _event_published_before(event: EventRecord, cutoff: dt.datetime, *, policy: str = "timestamp_verified") -> bool:
    return _published_at_before(event.event_time, cutoff, policy=policy)


def _company_name(event: EventRecord) -> str:
    match = re.search(rf"\b{re.escape(event.symbol)}\b\s+([^·|]+)", event.event_text or "")
    return match.group(1).strip() if match else event.symbol


def _candidate_groups(events: list[EventRecord], captured_at: str, *, evidence_policy: str = "timestamp_verified") -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[EventRecord]] = {}
    cutoff = _parse_iso(captured_at)
    for event in events:
        if _event_published_before(event, cutoff, policy=evidence_policy):
            grouped.setdefault((event.market, event.symbol), []).append(event)
    candidates = []
    for (market, symbol), rows in grouped.items():
        rows.sort(key=lambda row: row.event_time, reverse=True)
        latest = rows[0]
        evidence = []
        for event in rows:
            ev = _as_of_evidence(event, captured_at)
            ev[0]["content_hash"] = _sha(ev[0].get("content", ""))
            evidence.extend(ev)
        candidates.append({
            "market": market,
            "symbol": symbol,
            "company_name": _company_name(latest),
            "event_count": len(rows),
            "latest_event_at": latest.event_time,
            "event_type_l2": latest.event_type_l2,
            "title": latest.title,
            "source_url": latest.source_url,
            "evidence": evidence,
            "representative": latest,
        })
    candidates.sort(key=lambda row: (row["latest_event_at"], row["event_count"]), reverse=True)
    return candidates


async def _select_candidates(candidates: list[dict[str, Any]], count: int,
                             predictor_config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    count = max(1, min(int(count), len(candidates))) if candidates else 0
    eligible = [row for row in candidates if row["symbol"] not in {"", "UNKNOWN"}]
    selected: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, Any]] = {}
    selector_model = str(predictor_config.get("selector_model_version") or config.LLM_MODEL)
    prompt_version = str(predictor_config.get("selector_prompt_version") or "candidate-selector-v1")
    if eligible and count:
        packet = [{
            "id": row["id"], "market": row["market"], "symbol": row["symbol"],
            "company_name": row["company_name"], "event_count": row["event_count"],
            "latest_event_at": row["latest_event_at"], "event_type": row["event_type_l2"],
            "title": row["title"],
        } for row in eligible]
        response = await complete_json(
            "你是前瞻评测候选选择器。只能依据给定候选公告选择最适合做可验证方向预测的公司。"
            "优先信息明确、影响可量化、标的代码有效的事件；不得使用候选包之外的信息。"
            "严格输出 JSON：{\"selected\":[{\"id\":\"...\",\"score\":0-1,\"reason\":\"...\"}]}。",
            json.dumps({"selection_count": min(count, len(eligible)), "candidates": packet}, ensure_ascii=False),
            max_tokens=1200,
        )
        by_id = {row["id"]: row for row in eligible}
        for entry in (response or {}).get("selected", []):
            cid = str(entry.get("id") or "") if isinstance(entry, dict) else ""
            if cid in by_id and by_id[cid] not in selected and len(selected) < count:
                selected.append(by_id[cid])
                try:
                    score = float(entry.get("score"))
                except (TypeError, ValueError):
                    score = None
                decisions[cid] = {"score": score, "reason": str(entry.get("reason") or "模型选择")}
    # A model/network failure must not prevent a scheduled batch from completing.
    for row in eligible:
        if len(selected) >= count:
            break
        if row not in selected:
            selected.append(row)
            decisions[row["id"]] = {"score": None, "reason": "选择器无有效返回，按公告时间和事件数量回退选择"}
    for row in candidates:
        if row["id"] not in decisions:
            reason = "无法识别有效股票代码" if row["symbol"] in {"", "UNKNOWN"} else "未进入配置的选择数量"
            decisions[row["id"]] = {"score": None, "reason": reason}
        decisions[row["id"]]["model_version"] = selector_model
        decisions[row["id"]]["prompt_version"] = prompt_version
    return selected, decisions


def _settlement_target(run: dict[str, Any], captured: dt.datetime) -> dt.datetime:
    cfg = run.get("metric_config") or {}
    if str(cfg.get("settlement_mode") or "after_trade_days") == "absolute_trade_date" and cfg.get("settlement_trade_date"):
        target_date = dt.date.fromisoformat(str(cfg["settlement_trade_date"])[:10])
        return captured.replace(year=target_date.year, month=target_date.month, day=target_date.day)
    return _add_trading_days(captured, _settlement_horizon(run))


def _settlement_horizon(run: dict[str, Any]) -> int:
    """Use one trade-day period for new runs; retain legacy horizon as a fallback."""
    cfg = run.get("metric_config") or {}
    return max(1, int(run.get("settle_after_days") or cfg.get("horizon") or 3))


def _event_dict(event: EventRecord) -> dict[str, Any]:
    return event.to_dict()


def _as_of_evidence(event: EventRecord, captured_at: str) -> list[dict[str, Any]]:
    # The collector's event text is already the evidence returned by the existing source.
    # Keep it verbatim so the prediction can be replayed without a later network read.
    return [{
        "source_kind": "event_source",
        "source_url": event.source_url,
        "title": event.title,
        "content": event.event_text,
        "published_at": event.event_time,
        "retrieved_at": captured_at,
        "raw_payload": _event_dict(event),
    }]


def _evidence_hash(evidence: list[dict[str, Any]]) -> str:
    return _sha(evidence)


def _trailing_return(series: Any, end_date: dt.date, window: int) -> float | None:
    if series is None or len(series) == 0:
        return None
    try:
        usable = series[series.index <= end_date]
        if len(usable) <= int(window):
            return None
        start = float(usable.iloc[-(int(window) + 1)])
        end = float(usable.iloc[-1])
        return (end / start) - 1.0 if start else None
    except Exception:
        return None


def _as_of_market_features(symbol: str, market: str, cutoff: dt.datetime) -> dict[str, Any]:
    """Build only pre-cutoff features; this packet is safe to persist and replay."""
    local_cutoff = _market_local(cutoff, market)
    close_time = dt.time(16, 0) if str(market).upper() == "US" else dt.time(15, 0)
    end_date = local_cutoff.date() if local_cutoff.time() >= close_time else local_cutoff.date() - dt.timedelta(days=1)
    item = {"symbol": symbol, "market": market}
    fetched = _call_with_timeout(
        lambda: _fetch_closes(item, end_date),
        timeout_s=20,
        label=f"prospective_features:{market}:{symbol}",
    )
    asset, benchmark = fetched if fetched is not None else (None, None)
    features: dict[str, Any] = {"as_of_date": end_date.isoformat(), "asset": {}, "benchmark": {}, "car": {}}
    for window in (5, 20):
        asset_ret = _trailing_return(asset, end_date, window)
        benchmark_ret = _trailing_return(benchmark, end_date, window)
        features["asset"][f"pre{window}_return"] = asset_ret
        features["benchmark"][f"pre{window}_return"] = benchmark_ret
        features["car"][f"pre{window}_return"] = (asset_ret - benchmark_ret) if asset_ret is not None and benchmark_ret is not None else None
    features["data_quality"] = {
        "asset_available": bool(asset is not None and len(asset) > 0),
        "benchmark_available": bool(benchmark is not None and len(benchmark) > 0),
    }
    return features


async def capture_run(run_id: str) -> dict[str, Any]:
    run = db.get_prospective_run(run_id)
    if not run:
        raise ValueError("prospective run not found")
    if run["status"] not in {"scheduled", "failed", "capturing"}:
        return run
    captured = _now()
    captured_at = _iso(captured)
    configured_capture = _parse_iso(run["capture_at"])
    discovery_cutoff = min(captured, configured_capture)
    source_cfg = run.get("source_config") or {}
    evidence_policy = str(source_cfg.get("evidence_cutoff_policy") or "before_capture_date")
    candidate_days = max(1, int(source_cfg.get("candidate_lookback_trade_days") or source_cfg.get("lookback_trade_days") or 3))
    analysis_days = max(candidate_days, int(source_cfg.get("analysis_lookback_trade_days") or 20))
    strict_source_cfg = dict(source_cfg)
    strict_source_cfg["lookback_trade_days"] = candidate_days
    strict_source_cfg["include_capture_day"] = evidence_policy != "before_capture_date"
    db.update_prospective_run(run_id, status="capturing", error_message="")
    diagnostics: list[dict] = []
    events = discover_events(strict_source_cfg, as_of=discovery_cutoff, diagnostics=diagnostics)
    candidate_limit = max(1, min(int(source_cfg.get("candidate_limit") or 100), 500))
    grouped = _candidate_groups(events, _iso(discovery_cutoff), evidence_policy=evidence_policy)[:candidate_limit]
    saved_candidates: list[dict[str, Any]] = []
    for candidate in grouped:
        saved = db.create_prospective_candidate(
            run_id=run_id,
            canonical_key=_sha({"market": candidate["market"], "symbol": candidate["symbol"]}),
            market=candidate["market"], symbol=candidate["symbol"], company_name=candidate["company_name"],
            event_count=candidate["event_count"], latest_event_at=candidate["latest_event_at"],
            event_type_l2=candidate["event_type_l2"], title=candidate["title"],
            source_url=candidate["source_url"], evidence=candidate["evidence"],
        )
        saved["representative"] = candidate["representative"]
        saved_candidates.append(saved)
    selection_count = max(1, int(source_cfg.get("selection_count") or source_cfg.get("max_items") or 5))
    predictor_cfg = run.get("predictor_config") or {}
    selected, decisions = await _select_candidates(saved_candidates, selection_count, predictor_cfg)
    analysis_source_cfg = dict(source_cfg)
    analysis_source_cfg["lookback_trade_days"] = analysis_days
    analysis_source_cfg["include_capture_day"] = evidence_policy != "before_capture_date"
    analysis_source_cfg["symbols"] = [candidate["symbol"] for candidate in selected]
    analysis_events = discover_events(analysis_source_cfg, as_of=discovery_cutoff, diagnostics=diagnostics)
    analysis_groups = _candidate_groups(analysis_events, _iso(discovery_cutoff), evidence_policy=evidence_policy)
    evidence_by_symbol = {(row["market"], row["symbol"]): row for row in analysis_groups}
    for candidate in selected:
        hydrated = evidence_by_symbol.get((candidate["market"], candidate["symbol"]))
        if hydrated:
            candidate["evidence"] = hydrated["evidence"]
            candidate["event_count"] = hydrated["event_count"]
            candidate["latest_event_at"] = hydrated["latest_event_at"]
            candidate["representative"] = hydrated["representative"]
        candidate["selection_decision"] = decisions.get(candidate["id"], {})
    selected_ids = {row["id"] for row in selected}
    for candidate in saved_candidates:
        decision = decisions[candidate["id"]]
        db.update_prospective_candidate_selection(
            candidate["id"], selected=candidate["id"] in selected_ids,
            rank=(selected.index(candidate) + 1) if candidate in selected else None,
            score=decision["score"], reason=decision["reason"],
            model_version=decision["model_version"], prompt_version=decision["prompt_version"],
        )
    settle_at = _iso(_settlement_target(run, captured))
    initial_available_at = _iso(_result_available_at(_parse_iso(settle_at), selected[0]["market"] if selected else "CN"))
    evidence_dates = [str(ev.get("published_at"))[:10] for candidate in selected for ev in candidate.get("evidence", []) if ev.get("published_at")]
    db.update_prospective_run(
        run_id, total_items=len(selected), candidate_items=len(saved_candidates), selected_items=len(selected),
        settle_at=settle_at, target_settle_at=settle_at,
        result_available_at=initial_available_at,
        next_check_at=initial_available_at,
        evidence_cutoff_at=_iso(discovery_cutoff),
        evidence_start_date=min(evidence_dates) if evidence_dates else None,
        evidence_end_date=max(evidence_dates) if evidence_dates else None,
        evidence_policy=evidence_policy,
        actual_capture_at=captured_at,
        discovery_diagnostics=diagnostics,
    )
    item_pairs: list[tuple[dict, EventRecord, list[dict[str, Any]]]] = []
    for candidate in selected:
        source_event: EventRecord = candidate["representative"]
        market_features = _as_of_market_features(candidate["symbol"], candidate["market"], discovery_cutoff)
        context_packet = {
            "evidence_cutoff_at": _iso(discovery_cutoff),
            "evidence_policy": evidence_policy,
            "announcement_timeline": candidate.get("evidence") or [],
            "market_features": market_features,
        }
        # The forecast starts when evidence is frozen, not on the older announcement date.
        event = replace(
            source_event,
            event_id=f"live_{run_id}_{source_event.market.lower()}_{source_event.symbol}",
            event_time=captured_at,
            event_text=json.dumps(context_packet, ensure_ascii=False, sort_keys=True)[:24000],
        )
        key = _sha({"candidate_id": candidate["id"], "captured_at": captured_at})
        item = db.create_prospective_item(
            run_id=run_id, event_id=event.event_id, canonical_key=key, event=_event_dict(event),
            assertion_text=(
                f"基于最近公告，预测 {event.symbol} 从冻结时点至指定结算交易日的超额收益方向"
                if str((run.get("metric_config") or {}).get("settlement_mode") or "after_trade_days") == "absolute_trade_date"
                else f"基于最近公告，预测 {event.symbol} 从冻结时点起的 T+{_settlement_horizon(run)} 超额收益方向"
            ),
            captured_at=captured_at, settle_at=settle_at, candidate_id=candidate["id"],
        )
        item_pairs.append((item, event, candidate.get("evidence") or []))
        db.add_prospective_analysis_trace(
            run_id=run_id, item_id=item["id"], candidate_id=candidate["id"], sequence_no=3,
            stage="evidence_frozen", stage_title="证据已冻结", as_of_at=captured_at,
            output_snapshot={"evidence_count": len(candidate.get("evidence") or []),
                             "cutoff_at": _iso(discovery_cutoff), "policy": evidence_policy},
            evidence_refs=[ev.get("source_url") for ev in candidate.get("evidence") or []],
            trace_version="v2",
        )
        db.add_prospective_analysis_trace(
            run_id=run_id, item_id=item["id"], candidate_id=candidate["id"], sequence_no=5,
            stage="model_input_frozen", stage_title="模型输入已冻结", as_of_at=captured_at,
            input_snapshot=context_packet, output_snapshot={"event_id": event.event_id},
            evidence_refs=[ev.get("source_url") for ev in candidate.get("evidence") or []],
            model_version=str(predictor_cfg.get("model_version") or "prospective-team-prompt-v1"),
            prompt_version=str(predictor_cfg.get("prompt_version") or "as-of-v1"), trace_version="v2",
        )
        db.add_prospective_analysis_trace(
            run_id=run_id, item_id=item["id"], candidate_id=candidate["id"], sequence_no=1,
            stage="candidate_discovered", stage_title="候选公司入选", as_of_at=captured_at,
            output_snapshot={"selection_rank": selected.index(candidate) + 1,
                             "selection_score": candidate.get("selection_decision", {}).get("score"),
                             "selection_reason": candidate.get("selection_decision", {}).get("reason"),
                             "event_count": candidate.get("event_count")}, trace_version="v2",
        )
        db.add_prospective_analysis_trace(
            run_id=run_id, item_id=item["id"], candidate_id=candidate["id"], sequence_no=2,
            stage="evidence_hydrated", stage_title="历史证据补全", as_of_at=captured_at,
            output_snapshot={"evidence_count": len(candidate.get("evidence") or []),
                             "analysis_lookback_trade_days": analysis_days},
            evidence_refs=[ev.get("source_url") for ev in candidate.get("evidence") or []], trace_version="v2",
        )
        db.add_prospective_analysis_trace(
            run_id=run_id, item_id=item["id"], candidate_id=candidate["id"], sequence_no=4,
            stage="market_features_computed", stage_title="行情特征计算", as_of_at=captured_at,
            output_snapshot=market_features, trace_version="v2",
        )

    model_version = str(predictor_cfg.get("model_version") or "prospective-team-prompt-v1")
    prompt_version = str(predictor_cfg.get("prompt_version") or "as-of-v1")
    concurrency = max(1, min(int(predictor_cfg.get("concurrency") or 2), 4))
    predictions = await run_team_prompt(
        [event for _, event, _ in item_pairs],
        run_id=run_id,
        model_version=model_version,
        concurrency=concurrency,
        system_prompt_variant=str(predictor_cfg.get("prompt_variant") or "v0"),
        target_horizon=_settlement_horizon(run),
    )
    by_event = {p.event_id: p for p in predictions}
    frozen = 0
    for item, event, frozen_evidence in item_pairs:
        p = by_event.get(event.event_id)
        if p is None:
            db.update_prospective_item(item["id"], status="prediction_failed", error_message="predictor returned no result")
            continue
        valid_evidence = []
        for ev in frozen_evidence:
            if _published_at_before(str(ev.get("published_at") or ""), discovery_cutoff, policy=evidence_policy):
                ev["content_hash"] = _sha(ev.get("content", ""))
                valid_evidence.append(ev)
        if not valid_evidence:
            db.update_prospective_item(item["id"], status="prediction_failed", error_message="no evidence with valid as-of timestamp")
            continue
        pred = db.add_prospective_prediction(
            item_id=item["id"], pred_direction=p.pred_direction, confidence=p.confidence,
            rationale=p.rationale, raw_prediction=p.to_dict(), model_version=model_version,
            prompt_version=prompt_version, adapter_version="prospective-v1", git_commit=_git_commit(),
            as_of_at=captured_at, evidence_hash=_evidence_hash(valid_evidence), version=1,
        )
        for idx, ev in enumerate(valid_evidence):
            db.add_prospective_evidence(prediction_id=pred["id"], ordinal=idx, evidence=ev)
        db.add_prospective_analysis_trace(
            run_id=run_id, item_id=item["id"], candidate_id=item.get("candidate_id"), sequence_no=6,
            stage="prediction_generated", stage_title="预测已生成", as_of_at=captured_at,
            output_snapshot={"prediction_id": pred["id"], "direction": p.pred_direction,
                             "confidence": p.confidence, "rationale": p.rationale},
            evidence_refs=[ev.get("source_url") for ev in valid_evidence], model_version=model_version,
            prompt_version=prompt_version, trace_version="v2",
        )
        db.update_prospective_item(item["id"], status="frozen", prediction_id=pred["id"])
        frozen += 1
    if not saved_candidates:
        failed_queries = [row for row in diagnostics if row.get("status") == "failed"]
        successful_queries = [row for row in diagnostics if row.get("status") in {"ok", "empty"}]
        error_message = (
            "公告数据源抓取失败，请查看抓取诊断并等待重试"
            if failed_queries and not successful_queries
            else "最近配置的工作日窗口内没有找到符合条件的公告"
        )
        return db.update_prospective_run(
            run_id,
            status="failed",
            frozen_items=0,
            error_message=error_message,
        ) or run
    if not item_pairs:
        return db.update_prospective_run(
            run_id, status="failed", frozen_items=0,
            error_message="已抓取候选公告，但没有可用于行情结算的有效股票代码",
        ) or run
    status = "waiting" if frozen == len(item_pairs) else ("partial" if frozen else "failed")
    warning = ""
    if len(item_pairs) < selection_count:
        warning = f"候选公司不足：配置选择 {selection_count} 家，实际选择 {len(item_pairs)} 家"
    return db.update_prospective_run(run_id, status=status, frozen_items=frozen, error_message=warning) or run


def _fetch_closes_with_status(item: dict, end_date: dt.date):
    market = str(item.get("market") or "").upper()
    symbol = str(item.get("symbol") or "")
    benchmark = "SPY" if market == "US" else "sh000300"
    start = (end_date - dt.timedelta(days=220)).isoformat()
    end = (end_date + dt.timedelta(days=2)).isoformat()
    errors: dict[str, str] = {}

    def shared(symbol_value: str, role: str):
        try:
            frame = fetch_price_frame(symbol_value, start, end, market=market, adjust="qfq").frame
            dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
            series = pd.Series(frame["close"].astype(float).values, index=dates)
            return series[~series.index.duplicated()].sort_index()
        except (PriceFetchError, ValueError, TypeError) as exc:
            errors[role] = str(exc)
            return None
    return shared(symbol, "asset"), shared(benchmark, "benchmark"), errors


def _fetch_closes(item: dict, end_date: dt.date):
    asset, benchmark, _errors = _fetch_closes_with_status(item, end_date)
    return asset, benchmark


def _label_for_car(car: float | None, epsilon: float) -> str | None:
    if car is None:
        return None
    if car > epsilon:
        return "up"
    if car < -epsilon:
        return "down"
    return "neutral"


def _market_local(value: dt.datetime, market: str) -> dt.datetime:
    zone = ZoneInfo("America/New_York") if str(market).upper() == "US" else ZoneInfo("Asia/Shanghai")
    aware = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    return aware.astimezone(zone)


def _result_available_at(target: dt.datetime, market: str) -> dt.datetime:
    zone = ZoneInfo("America/New_York") if str(market).upper() == "US" else ZoneInfo("Asia/Shanghai")
    close = dt.time(16, 5) if str(market).upper() == "US" else dt.time(15, 30)
    local = target.astimezone(zone).replace(hour=close.hour, minute=close.minute, second=0, microsecond=0)
    return local.astimezone(target.tzinfo or dt.timezone.utc)


def _run_result_available_at(run: dict[str, Any], items: list[dict[str, Any]]) -> dt.datetime:
    target = _parse_iso(str(run.get("target_settle_at") or run.get("settle_at") or _now().isoformat()))
    market = str((items[0] if items else {}).get("market") or "CN")
    return _result_available_at(target, market)


def _forward_return(closes: Any, captured_at: str, market: str, horizon: int,
                    target_date: dt.date | None = None) -> tuple[float | None, str | None, str | None]:
    """Return from the last close known at freeze time to a later trading-day close."""
    if closes is None or len(closes) == 0:
        return None, None, None
    dates = sorted(closes.index)
    local_capture = _market_local(_parse_iso(captured_at), market)
    close_time = dt.time(16, 0) if str(market).upper() == "US" else dt.time(15, 0)
    baseline_dates = [value for value in dates if value <= local_capture.date()] if local_capture.time() >= close_time else [value for value in dates if value < local_capture.date()]
    if not baseline_dates:
        return None, None, None
    base = baseline_dates[-1]
    base_idx = dates.index(base)
    if target_date is not None:
        if target_date not in dates or target_date <= base:
            return None, str(base), None
        target = target_date
    else:
        target_idx = base_idx + max(1, int(horizon))
        if target_idx >= len(dates):
            return None, str(base), None
        target = dates[target_idx]
    p0, p1 = float(closes.loc[base]), float(closes.loc[target])
    if not p0:
        return None, str(base), str(target)
    return (p1 / p0) - 1.0, str(base), str(target)


def _metrics(run_id: str) -> dict[str, Any]:
    items = db.list_prospective_items(run_id)
    preds = [db.get_latest_prospective_prediction(i["id"]) for i in items]
    rows = [(i, p) for i, p in zip(items, preds) if p]
    settled = [(i, p) for i, p in rows if i.get("actual_label")]
    correct = sum(1 for i, p in settled if p["pred_direction"] == i["actual_label"])
    n = len(rows)
    accuracy = correct / len(settled) if settled else None
    coverage = len(settled) / n if n else 0.0
    buckets: dict[str, dict[str, Any]] = {}
    for item, pred in settled:
        conf = float(pred.get("confidence") or 0.5)
        lo = min(0.9, max(0.5, int(conf * 10) / 10))
        key = f"{lo:.1f}-{lo + 0.1:.1f}"
        b = buckets.setdefault(key, {"n": 0, "correct": 0, "accuracy": None})
        b["n"] += 1; b["correct"] += int(pred["pred_direction"] == item["actual_label"]); b["accuracy"] = b["correct"] / b["n"]
    return {"n_predicted": n, "n_settled": len(settled), "n_correct": correct, "accuracy": accuracy,
            "coverage": coverage, "confidence_buckets": buckets}


def _settlement_summary(run_id: str, *, message: str = "") -> dict[str, Any]:
    run = db.get_prospective_run(run_id) or {}
    items = db.list_prospective_items(run_id)
    metrics = _metrics(run_id)
    settled = sum(1 for item in items if item.get("status") == "settled")
    failed = sum(1 for item in items if item.get("status") in {"failed", "prediction_failed"})
    waiting = max(0, len(items) - settled - failed)
    return {
        "total": len(items), "settled": settled, "waiting": waiting, "failed": failed,
        "accuracy": metrics.get("accuracy"), "coverage": metrics.get("coverage"),
        "result_available_at": run.get("result_available_at"), "next_check_at": run.get("next_check_at"),
        "message": message,
    }


def settle_run(run_id: str, *, force: bool = False) -> dict[str, Any]:
    run = db.get_prospective_run(run_id)
    if not run:
        raise ValueError("prospective run not found")
    if not force and run.get("settle_at") and _parse_iso(run["settle_at"]) > _now():
        return run
    cfg = run.get("metric_config") or {}
    horizon = _settlement_horizon(run)
    epsilon = float(cfg.get("epsilon") or 0.005)
    settlement_mode = str(cfg.get("settlement_mode") or "after_trade_days")
    target_date = None
    if settlement_mode == "absolute_trade_date" and cfg.get("settlement_trade_date"):
        target_date = dt.date.fromisoformat(str(cfg["settlement_trade_date"])[:10])
    items_before = db.list_prospective_items(run_id)
    available_at = _run_result_available_at(run, items_before)
    now = _now()
    if now < available_at:
        reason = f"目标交易日尚未收盘，预计 {available_at.isoformat()} 后可结算"
        updated = db.update_prospective_run(run_id, status="waiting", result_available_at=_iso(available_at), next_check_at=_iso(available_at)) or run
        updated["settlement_summary"] = _settlement_summary(run_id, message=reason)
        return updated
    db.update_prospective_run(run_id, status="settling", result_available_at=_iso(available_at))
    for item in items_before:
        if item.get("status") not in {"frozen", "waiting", "insufficient"}:
            continue
        try:
            end = _now().date()
            asset, benchmark, fetch_errors = _fetch_closes_with_status(item, end)
            asset_return, base_date, actual_date = _forward_return(
                asset, str(item.get("captured_at") or item.get("event_time") or ""),
                str(item.get("market") or ""), horizon, target_date,
            )
            car = None
            if asset is not None and benchmark is not None:
                bm_return, _bm_base_date, bm_actual_date = _forward_return(
                    benchmark, str(item.get("captured_at") or item.get("event_time") or ""),
                    str(item.get("market") or ""), horizon, target_date,
                )
                if asset_return is not None and bm_return is not None and actual_date == bm_actual_date:
                    car = asset_return - bm_return
            label = _label_for_car(car, epsilon)
            if label is None:
                latest = db.get_latest_prospective_settlement(item["id"])
                if fetch_errors.get("asset"):
                    reason = f"标的行情获取失败：{fetch_errors['asset']}"
                elif fetch_errors.get("benchmark"):
                    reason = f"基准行情获取失败：{fetch_errors['benchmark']}"
                else:
                    reason = f"T+{horizon} 目标交易日行情尚未更新，继续观察"
                if latest and latest.get("status") == "insufficient":
                    db.update_prospective_item(item["id"], status="waiting", error_message=reason)
                else:
                    db.add_prospective_settlement(item_id=item["id"], settlement_mode="car", status="insufficient",
                                                  result_source={"target_trade_date": str(target_date or ""), "base_trade_date": base_date},
                                                  reasoning=reason)
                db.update_prospective_item(item["id"], status="waiting", error_message=reason)
                continue
            pred = db.get_latest_prospective_prediction(item["id"])
            is_correct = bool(pred and pred["pred_direction"] == label)
            settlement = db.add_prospective_settlement(item_id=item["id"], settlement_mode="car", status="resolved",
                                                        actual_label=label, actual_value=car,
                                                        result_source={"horizon": horizon, "epsilon": epsilon,
                                                                       "base_trade_date": base_date,
                                                                       "actual_trade_date": actual_date,
                                                                       "settlement_mode": settlement_mode},
                                                       reasoning=f"按冻结时点后的真实行情计算相对收益，结算日 {actual_date}", settled_at=_iso(_now()))
            trace = db.list_prospective_analysis_trace(item["id"])
            db.add_prospective_analysis_trace(
                run_id=run_id, item_id=item["id"], candidate_id=item.get("candidate_id"),
                sequence_no=(max((int(row.get("sequence_no") or 0) for row in trace), default=0) + 1),
                stage="judge_completed", stage_title="Judge 已完成", as_of_at=_iso(_now()),
                output_snapshot={"settlement_id": settlement["id"], "actual_label": label,
                                 "actual_value": car, "is_correct": is_correct},
                model_version=str((db.get_latest_prospective_prediction(item["id"]) or {}).get("model_version") or ""),
                trace_version="v2",
            )
            db.update_prospective_item(item["id"], status="settled", settlement_id=settlement["id"], actual_label=label,
                                       actual_value=car, is_correct=1 if is_correct else 0, resolved_at=_iso(_now()), error_message="")
        except Exception as exc:
            db.update_prospective_item(item["id"], status="failed", error_message=f"settlement: {type(exc).__name__}: {exc}")
    metrics = _metrics(run_id)
    db.add_prospective_metric_snapshot(run_id=run_id, metrics=metrics)
    items = db.list_prospective_items(run_id)
    frozen = sum(1 for i in items if i.get("status") in {"frozen", "waiting", "settled", "insufficient"})
    settled = sum(1 for i in items if i.get("status") == "settled")
    has_waiting = any(i.get("status") == "waiting" for i in items)
    status = "completed" if settled == len(items) and items else ("waiting" if has_waiting else ("partial" if settled or frozen else "failed"))
    next_check = None if status == "completed" else _iso(max(_now() + dt.timedelta(hours=1), available_at))
    updated = db.update_prospective_run(run_id, status=status, next_check_at=next_check, frozen_items=frozen, settled_items=settled,
                                        correct_items=int(metrics.get("n_correct") or 0)) or run
    message = "结算完成" if status == "completed" else f"本次结算 {settled}/{len(items)} 条，剩余结果等待行情源更新"
    updated["settlement_summary"] = _settlement_summary(run_id, message=message)
    return updated


def detail(run_id: str) -> dict[str, Any] | None:
    run = db.get_prospective_run(run_id)
    if not run:
        return None
    items = db.list_prospective_items(run_id)
    enriched = []
    for item in items:
        pred = db.get_latest_prospective_prediction(item["id"])
        evidence = db.list_prospective_evidence(pred["id"]) if pred else []
        settlements = db.list_prospective_settlements(item["id"])
        trace = db.list_prospective_analysis_trace(item["id"])
        if not trace:
            # Existing frozen batches predate trace persistence. Reconstruct only what is
            # verifiable from immutable records and label it explicitly as incomplete.
            trace = []
            if evidence:
                trace.append({"id": f"legacy-evidence-{item['id']}", "run_id": run_id, "item_id": item["id"],
                              "candidate_id": item.get("candidate_id"), "sequence_no": 1,
                              "stage": "evidence_frozen", "stage_title": "已冻结证据（历史兼容）",
                              "as_of_at": (pred or {}).get("as_of_at") or item.get("captured_at") or "",
                              "input_snapshot": {}, "output_snapshot": {"evidence_count": len(evidence)},
                              "evidence_refs": [ev.get("source_url") for ev in evidence],
                              "trace_version": "legacy-reconstructed", "complete": False})
            if pred:
                trace.append({"id": f"legacy-prediction-{item['id']}", "run_id": run_id, "item_id": item["id"],
                              "candidate_id": item.get("candidate_id"), "sequence_no": 2,
                              "stage": "prediction_generated", "stage_title": "最终预测（历史兼容）",
                              "as_of_at": pred.get("as_of_at") or "", "input_snapshot": {},
                              "output_snapshot": {"direction": pred.get("pred_direction"),
                                                   "confidence": pred.get("confidence"),
                                                   "rationale": pred.get("rationale")},
                              "evidence_refs": [ev.get("source_url") for ev in evidence],
                              "trace_version": "legacy-reconstructed", "complete": False})
        enriched.append({"item": item, "prediction": pred, "evidence": evidence,
                         "settlements": settlements, "trace": trace})
    return {"run": run, "candidates": db.list_prospective_candidates(run_id), "items": enriched, "metrics": _metrics(run_id),
            "settlement_summary": _settlement_summary(run_id),
            "snapshots": db.list_prospective_metric_snapshots(run_id)}
