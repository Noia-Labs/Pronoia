#!/usr/bin/env python3
"""Select a stratified cohort whose T1 and T3 market labels are observable.

The selector never moves the label cutoff forward.  It only accepts events
whose effective T0, asset prices, benchmark prices, and market-model CAR are
all available through the requested as-of date.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

for _proxy_key in (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy",
):
    os.environ.pop(_proxy_key, None)

from app.event_backtest.labeller import _car, _market_model_car  # noqa: E402
from app.skills.price_data import PriceFetchError, fetch_price_frame  # noqa: E402


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )


def _round_robin(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("event_type_l2") or "unknown")].append(row)
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


def _direction(value: float | None, epsilon: float) -> str:
    if value is None or not math.isfinite(value):
        return ""
    if value > epsilon:
        return "up"
    if value < -epsilon:
        return "down"
    return "neutral"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--target", type=int, default=50)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--epsilon", type=float, default=0.005)
    args = parser.parse_args()

    cutoff = dt.date.fromisoformat(args.as_of)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = _jsonl(Path(args.events))

    # T3 requires T0 plus three later trading closes.  Restricting to the
    # earliest effective session avoids selection attempts that cannot yet be
    # observed at the frozen cutoff.
    eligible_dates = sorted({str(row.get("effective_session") or "")[:10] for row in rows})
    if not eligible_dates:
        raise SystemExit("no effective_session in input")
    earliest_t0 = eligible_dates[0]
    candidates = [row for row in rows if str(row.get("effective_session") or "")[:10] == earliest_t0]
    candidates = _round_robin(candidates)

    earliest = dt.date.fromisoformat(earliest_t0)
    start = (earliest - dt.timedelta(days=200)).isoformat()
    end = cutoff.isoformat()
    cache: dict[tuple[str, str], tuple[pd.Series | None, str, str]] = {}

    def prices(symbol: str, market: str) -> tuple[pd.Series | None, str, str]:
        key = (market, symbol)
        if key in cache:
            return cache[key]
        try:
            fetched = fetch_price_frame(symbol, start, end, market=market, adjust="qfq")
            result = (_series(fetched.frame, cutoff), fetched.provider, "")
        except (PriceFetchError, ValueError) as exc:
            result = (None, "", str(exc))
        cache[key] = result
        return result

    benchmark_cache: dict[tuple[str, str], tuple[pd.Series | None, str, str]] = {}
    selected: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []

    for index, event in enumerate(candidates, 1):
        market = str(event.get("market") or "")
        symbol = str(event.get("symbol") or "")
        benchmark = str(event.get("benchmark") or ("sh000300" if market == "CN" else "SPY"))
        asset, asset_provider, asset_error = prices(symbol, market)
        benchmark_key = (market, benchmark)
        if benchmark_key not in benchmark_cache:
            benchmark_cache[benchmark_key] = prices(benchmark, market)
        bm, bm_provider, bm_error = benchmark_cache[benchmark_key]
        event_date = dt.date.fromisoformat(str(event["effective_session"])[:10])
        values: dict[str, Any] = {}
        for horizon in (1, 3):
            key = f"t{horizon}"
            values[f"ret_{key}"] = _car(asset, event_date, horizon, "", market) if asset is not None else None
            values[f"bm_ret_{key}"] = _car(bm, event_date, horizon, "", market) if bm is not None else None
            car, tstat, pvalue = (None, None, None)
            if asset is not None and bm is not None:
                car, tstat, pvalue = _market_model_car(asset, bm, event_date, horizon, "", market)
            values[f"car_{key}"] = car
            values[f"car_{key}_tstat"] = tstat
            values[f"car_{key}_pvalue"] = pvalue

        complete = all(
            values.get(key) is not None
            for key in ("ret_t1", "ret_t3", "bm_ret_t1", "bm_ret_t3", "car_t1", "car_t3")
        )
        audit_row = {
            "event_id": event["event_id"],
            "market": market,
            "symbol": symbol,
            "event_type_l2": event.get("event_type_l2"),
            "effective_session": event.get("effective_session"),
            "selected": complete,
            "asset_provider": asset_provider,
            "benchmark_provider": bm_provider,
            "asset_error": asset_error,
            "benchmark_error": bm_error,
            **values,
        }
        audit.append(audit_row)
        if complete:
            selected.append(event)
            labels.append({
                "event_id": event["event_id"],
                "market": market,
                "symbol": symbol,
                "event_time": event.get("event_time"),
                "effective_session": event.get("effective_session"),
                "event_type_l2": event.get("event_type_l2"),
                "label_as_of": cutoff.isoformat(),
                "epsilon": args.epsilon,
                "asset_provider": asset_provider,
                "benchmark_provider": bm_provider,
                **values,
                "label_t1": _direction(values["car_t1"], args.epsilon),
                "label_t3": _direction(values["car_t3"], args.epsilon),
            })
            print(
                f"[SELECT {len(selected):02d}/{args.target}] {event['event_id']} "
                f"{event.get('event_type_l2')} T1={labels[-1]['label_t1']} T3={labels[-1]['label_t3']} "
                f"provider={asset_provider}", flush=True,
            )
        else:
            print(f"[SKIP {index}] {event['event_id']} price coverage incomplete", flush=True)
        if len(selected) >= args.target:
            break

    _write_jsonl(out_dir / f"events_price_labelable_{args.target}.jsonl", selected)
    _write_jsonl(out_dir / f"labels_t1_t3_{args.target}.jsonl", labels)
    _write_jsonl(out_dir / "selection_audit.jsonl", audit)
    summary = {
        "source_events": len(rows),
        "eligible_earliest_t0": earliest_t0,
        "eligible_candidates": len(candidates),
        "label_as_of": cutoff.isoformat(),
        "attempted": len(audit),
        "selected": len(selected),
        "target": args.target,
        "complete": len(selected) == args.target,
        "by_market": dict(Counter(row["market"] for row in selected)),
        "by_category": dict(Counter(row["event_type_l2"] for row in selected)),
        "t1_labels": dict(Counter(row["label_t1"] for row in labels)),
        "t3_labels": dict(Counter(row["label_t3"] for row in labels)),
        "asset_providers": dict(Counter(row["asset_provider"] for row in labels)),
    }
    (out_dir / "selection_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
