#!/usr/bin/env python3
"""Build a strict offline cohort with exact publication time and T1/T3 labels."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

for _key in (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy",
):
    os.environ.pop(_key, None)

from app.event_backtest.labeller import _car, _market_model_car  # noqa: E402
from app.skills.frozen_document import frozen_announcement_fetch  # noqa: E402
from app.skills.price_data import PriceFetchError, fetch_price_frame  # noqa: E402


CN_TZ = ZoneInfo("Asia/Shanghai")
PREOPEN_CUTOFF = dt.time(9, 15)
EXACT_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )


def _score(row: dict[str, Any]) -> int:
    return int(((row.get("metadata_screening") or {}).get("total_score") or 0))


def _stratified_order(rows: list[dict[str, Any]], cached_ids: set[str]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("event_type_l2") or "unknown")].append(row)
    for values in buckets.values():
        values.sort(key=lambda row: (
            0 if str(row.get("event_id")) in cached_ids else 1,
            -_score(row),
            str(row.get("published_at") or ""),
            str(row.get("event_id") or ""),
        ))
    keys = sorted(buckets, key=lambda key: (-len(buckets[key]), key))
    ordered: list[dict[str, Any]] = []
    while any(buckets.values()):
        for key in keys:
            if buckets[key]:
                ordered.append(buckets[key].pop(0))
    return ordered


def _series(frame: pd.DataFrame, cutoff: dt.date) -> pd.Series:
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    series = pd.Series(frame["close"].astype(float).values, index=dates)
    series = series[~series.index.duplicated()].sort_index()
    return series[series.index <= cutoff]


def _parse_cn_time(value: str) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not EXACT_TIME.match(raw):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or CN_TZ).astimezone(CN_TZ)


def effective_session(published: dt.datetime, trading_days: list[dt.date]) -> dt.date | None:
    """First session whose 09:15 decision cutoff follows publication."""
    pub_day = published.date()
    if pub_day in trading_days and published.timetz().replace(tzinfo=None) <= PREOPEN_CUTOFF:
        return pub_day
    return next((day for day in trading_days if day > pub_day), None)


def _direction(value: float | None, epsilon: float) -> str:
    if value is None or not math.isfinite(value):
        return ""
    if value > epsilon:
        return "up"
    if value < -epsilon:
        return "down"
    return "neutral"


def _cached_documents(path: Path | None) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if path is None or not path.is_dir():
        return output
    for file in path.glob("*.json"):
        try:
            row = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        event = row.get("resolved_event") or {}
        if (
            event.get("document_fetch_ok")
            and event.get("event_text_kind") == "frozen_announcement_body"
            and _parse_cn_time(str(event.get("source_published_at") or ""))
        ):
            output[str(row.get("event_id") or file.stem)] = event
    return output


async def main_async(args: argparse.Namespace) -> int:
    cutoff = dt.date.fromisoformat(args.as_of)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    packets = _read_jsonl(Path(args.events))
    cached = _cached_documents(Path(args.reuse_documents) if args.reuse_documents else None)
    candidates = _stratified_order(
        [row for row in packets if str(row.get("market") or "").upper() == "CN"],
        set(cached),
    )

    start = (cutoff - dt.timedelta(days=230)).isoformat()
    benchmark_fetch = fetch_price_frame("sh000300", start, cutoff.isoformat(), market="CN", adjust="qfq")
    benchmark = _series(benchmark_fetch.frame, cutoff)
    trading_days = sorted(benchmark.index)
    price_cache: dict[str, tuple[pd.Series | None, str, str]] = {}

    def asset_prices(symbol: str) -> tuple[pd.Series | None, str, str]:
        if symbol in price_cache:
            return price_cache[symbol]
        try:
            fetched = fetch_price_frame(symbol, start, cutoff.isoformat(), market="CN", adjust="qfq")
            result = (_series(fetched.frame, cutoff), fetched.provider, "")
        except (PriceFetchError, ValueError) as exc:
            result = (None, "", str(exc))
        price_cache[symbol] = result
        return result

    selected: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    seen_symbol_sessions: set[tuple[str, str]] = set()

    for index, packet in enumerate(candidates, 1):
        event_id = str(packet.get("event_id") or "")
        resolved = dict(cached.get(event_id) or {})
        fetch_source = "checkpoint" if resolved else "live"
        fetch_error = ""
        if not resolved:
            result = await frozen_announcement_fetch(
                source_url=str(packet.get("source_url") or ""),
                source_key=str(packet.get("source_key") or ""),
                market="CN",
                symbol=str(packet.get("symbol") or ""),
                issuer_name=str(packet.get("issuer_name") or ""),
                event_date=str(packet.get("published_at") or packet.get("event_time") or "")[:10],
                max_chars=12000,
            )
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            if result.get("ok") and data.get("content"):
                resolved = {
                    **packet,
                    "event_text": data["content"],
                    "event_text_kind": "frozen_announcement_body",
                    "source_document_hash": data.get("content_sha256"),
                    "source_published_at": data.get("source_published_at"),
                    "document_fetch_ok": True,
                }
            else:
                fetch_error = str(result.get("error") or "document_fetch_failed")

        published = _parse_cn_time(str(resolved.get("source_published_at") or ""))
        t0 = effective_session(published, trading_days) if published else None
        reason = ""
        if not resolved:
            reason = "document_fetch_failed"
        elif not published:
            reason = "publication_time_not_exact"
        elif t0 is None:
            reason = "no_effective_session_before_cutoff"
        elif trading_days.index(t0) + 3 >= len(trading_days):
            reason = "t3_not_observable_at_cutoff"

        symbol = str(packet.get("symbol") or "")
        dedupe_key = (symbol, t0.isoformat() if t0 else "")
        if not reason and dedupe_key in seen_symbol_sessions:
            reason = "duplicate_symbol_effective_session"

        asset: pd.Series | None = None
        provider = ""
        price_error = ""
        values: dict[str, Any] = {}
        if not reason and t0 is not None:
            asset, provider, price_error = asset_prices(symbol)
            for horizon in (1, 3):
                key = f"t{horizon}"
                values[f"ret_{key}"] = _car(asset, t0, horizon, "", "CN") if asset is not None else None
                values[f"bm_ret_{key}"] = _car(benchmark, t0, horizon, "", "CN")
                car, tstat, pvalue = (None, None, None)
                if asset is not None:
                    car, tstat, pvalue = _market_model_car(asset, benchmark, t0, horizon, "", "CN")
                values[f"car_{key}"] = car
                values[f"car_{key}_tstat"] = tstat
                values[f"car_{key}_pvalue"] = pvalue
            if not all(
                values.get(key) is not None
                for key in ("ret_t1", "ret_t3", "bm_ret_t1", "bm_ret_t3", "car_t1", "car_t3")
            ):
                reason = "incomplete_t1_t3_prices"

        accepted = not reason
        audit.append({
            "event_id": event_id,
            "symbol": symbol,
            "event_type_l2": packet.get("event_type_l2"),
            "accepted": accepted,
            "rejection_reason": reason,
            "fetch_source": fetch_source,
            "fetch_error": fetch_error,
            "source_published_at": resolved.get("source_published_at"),
            "effective_session": t0.isoformat() if t0 else None,
            "asset_provider": provider,
            "price_error": price_error,
            **values,
        })
        if not accepted or published is None or t0 is None:
            print(f"[SKIP {index}] {event_id} {reason} {fetch_error or price_error}", flush=True)
            continue

        seen_symbol_sessions.add(dedupe_key)
        precise_time = published.isoformat(timespec="seconds")
        prepared = {
            **resolved,
            "event_time": precise_time,
            "published_at": precise_time,
            "timestamp_precision": "second",
            "effective_session": t0.isoformat(),
            "effective_session_is_estimate": False,
            "prediction_cutoff_at": dt.datetime.combine(t0, PREOPEN_CUTOFF, tzinfo=CN_TZ).isoformat(),
            "packet_mode": "frozen_body_ready",
            "research_required": False,
            "evaluation_status": "strict",
            "evaluation_selection_reason": (
                "冻结公告秒级发布时间、证券身份及正文均已核验；T0 由沪深300实际交易日与09:15截止点确定；"
                "T1/T3 行情在冻结 as-of 前完整可得"
            ),
            "dataset_scope": "offline_backfill",
            "prediction_eligible": False,
            "online_ineligibility_reasons": ["historical_backfill"],
        }
        selected.append(prepared)
        labels.append({
            "event_id": event_id,
            "market": "CN",
            "symbol": symbol,
            "event_time": precise_time,
            "effective_session": t0.isoformat(),
            "event_type_l2": packet.get("event_type_l2"),
            "evaluation_status": "strict",
            "label_as_of": cutoff.isoformat(),
            "epsilon": args.epsilon,
            "asset_provider": provider,
            "benchmark_provider": benchmark_fetch.provider,
            **values,
            "label_t1": _direction(values["car_t1"], args.epsilon),
            "label_t3": _direction(values["car_t3"], args.epsilon),
        })
        print(
            f"[SELECT {len(selected):03d}/{args.target}] {event_id} pub={precise_time} T0={t0} "
            f"T1={labels[-1]['label_t1']} T3={labels[-1]['label_t3']} provider={provider}",
            flush=True,
        )
        if len(selected) >= args.target:
            break

    events_path = out_dir / f"events_precise_t1_t3_{args.target}.jsonl"
    labels_path = out_dir / f"labels_precise_t1_t3_{args.target}.jsonl"
    _write_jsonl(events_path, selected)
    _write_jsonl(labels_path, labels)
    _write_jsonl(out_dir / "selection_audit.jsonl", audit)
    summary = {
        "source_events": len(packets),
        "target": args.target,
        "selected": len(selected),
        "complete": len(selected) == args.target,
        "label_as_of": cutoff.isoformat(),
        "publication_precision": "second",
        "t0_rule": "first actual CN benchmark session whose 09:15 cutoff follows publication",
        "unique_symbol_effective_session": True,
        "attempted": len(audit),
        "rejections": dict(Counter(row["rejection_reason"] for row in audit if not row["accepted"])),
        "document_sources": dict(Counter(row["fetch_source"] for row in audit if row["accepted"])),
        "by_category": dict(Counter(row["event_type_l2"] for row in selected)),
        "by_effective_session": dict(Counter(row["effective_session"] for row in selected)),
        "t1_labels": dict(Counter(row["label_t1"] for row in labels)),
        "t3_labels": dict(Counter(row["label_t3"] for row in labels)),
        "asset_providers": dict(Counter(row["asset_provider"] for row in labels)),
        "events_file": str(events_path),
        "labels_file": str(labels_path),
    }
    (out_dir / "selection_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["complete"] else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--reuse-documents", default="")
    parser.add_argument("--target", type=int, default=100)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--epsilon", type=float, default=0.005)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
