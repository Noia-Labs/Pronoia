#!/usr/bin/env python3
"""Export the frozen 37-event cohort and create leakage-safe T1/T3 labels.

The event ``effective_session`` is used as T0.  Price series are truncated at
``--as-of`` so an in-progress trading day can never become a label.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# The repository's market-data endpoints are direct HTTP sources.  A stale
# desktop proxy can keep the TLS handshake alive indefinitely; match the
# established forward-test labeller and bypass proxy variables here.
for _proxy_key in (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy",
):
    os.environ.pop(_proxy_key, None)

from app.event_backtest.labeller import (  # noqa: E402
    _car, _market_model_car, _yf_benchmark_for, _yf_ticker_for,
)
from app.skills.price_data import PriceFetchError, fetch_price_frame  # noqa: E402


def _event_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE quality_pass=1 ORDER BY cohort_date,event_id"
        ).fetchall()
    finally:
        conn.close()
    events: list[dict[str, Any]] = []
    for row in rows:
        event = json.loads(row["event_json"])
        event["evaluation_status"] = row["evaluation_status"]
        event["evaluation_selection_reason"] = row["evaluation_selection_reason"]
        event["quality_score"] = row["quality_score"]
        event["cohort_date"] = row["cohort_date"]
        event["effective_session"] = row["effective_session"] or row["event_time"]
        # The labeller's event_time is T0.  Preserve the announcement timestamp
        # separately and align event_time to the first tradable session.
        event["announcement_event_time"] = row["event_time"]
        event["event_time"] = event["effective_session"]
        events.append(event)
    return events


def _series_for(ticker: str, market: str, start: str, end: str):
    try:
        fetched = fetch_price_frame(ticker, start, end, market=market, adjust="qfq")
    except (PriceFetchError, ValueError) as exc:
        print(f"[WARN] price {market} {ticker}: {exc}", flush=True)
        return None
    frame = fetched.frame
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    series = pd.Series(frame["close"].astype(float).values, index=dates)
    print(f"[price] {market} {ticker} provider={fetched.provider} rows={len(series)}", flush=True)
    return series[~series.index.duplicated()].sort_index()


def _direction(value: float | None, epsilon: float) -> str:
    if value is None:
        return ""
    if value > epsilon:
        return "up"
    if value < -epsilon:
        return "down"
    return "neutral"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--as-of", required=True, help="last fully closed session date")
    parser.add_argument("--epsilon", type=float, default=0.005)
    args = parser.parse_args()

    db_path = Path(args.db)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cutoff = dt.date.fromisoformat(args.as_of)
    events = _event_rows(db_path)
    if len(events) != 37:
        raise SystemExit(f"expected 37 quality-pass events, got {len(events)}")

    events_path = out_dir / "events_37.jsonl"
    events_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in events),
        encoding="utf-8",
    )

    earliest = min(dt.date.fromisoformat(str(e["effective_session"])[:10]) for e in events)
    start = (earliest - dt.timedelta(days=200)).isoformat()
    end = (cutoff + dt.timedelta(days=1)).isoformat()
    cache: dict[tuple[str, str], Any] = {}

    def prices(ticker: str, market: str):
        key = (ticker, market)
        if key not in cache:
            series = _series_for(ticker, market, start, end)
            if series is not None:
                series = series[series.index <= cutoff]
            cache[key] = series
        return cache[key]

    labels: list[dict[str, Any]] = []
    for index, event in enumerate(events, 1):
        market = str(event.get("market") or "")
        symbol = str(event.get("symbol") or "")
        benchmark = event.get("benchmark")
        asset_ticker = _yf_ticker_for(symbol, market, benchmark)
        benchmark_ticker = _yf_benchmark_for(benchmark, market)
        asset = prices(asset_ticker, market)
        bm = prices(benchmark_ticker, market)
        event_date = dt.date.fromisoformat(str(event["effective_session"])[:10])
        row: dict[str, Any] = {
            "event_id": event["event_id"],
            "market": market,
            "symbol": symbol,
            "event_time": event["announcement_event_time"],
            "effective_session": event["effective_session"],
            "evaluation_status": event["evaluation_status"],
            "label_as_of": cutoff.isoformat(),
            "epsilon": args.epsilon,
            "asset_ticker": asset_ticker,
            "benchmark_ticker": benchmark_ticker,
        }
        for horizon in (1, 3):
            key = f"t{horizon}"
            asset_return = _car(asset, event_date, horizon, "", market) if asset is not None else None
            benchmark_return = _car(bm, event_date, horizon, "", market) if bm is not None else None
            car, tstat, pvalue = (None, None, None)
            if asset is not None and bm is not None:
                car, tstat, pvalue = _market_model_car(asset, bm, event_date, horizon, "", market)
            row[f"ret_{key}"] = asset_return
            row[f"bm_ret_{key}"] = benchmark_return
            row[f"car_{key}"] = car
            row[f"car_{key}_tstat"] = tstat
            row[f"car_{key}_pvalue"] = pvalue
            row[f"label_{key}"] = _direction(car, args.epsilon)
        labels.append(row)
        print(
            f"[{index:02d}/37] {event['event_id']} "
            f"T1={row['label_t1'] or 'pending'} T3={row['label_t3'] or 'pending'}",
            flush=True,
        )

    labels_path = out_dir / "labels_t1_t3_asof_20260907.jsonl"
    labels_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in labels),
        encoding="utf-8",
    )
    summary = {
        "events": len(events),
        "as_of": cutoff.isoformat(),
        "epsilon": args.epsilon,
        "coverage": {
            horizon: sum(bool(row[f"label_{horizon}"]) for row in labels)
            for horizon in ("t1", "t3")
        },
        "strict_coverage": {
            horizon: sum(
                row["evaluation_status"] == "strict" and bool(row[f"label_{horizon}"])
                for row in labels
            )
            for horizon in ("t1", "t3")
        },
    }
    (out_dir / "label_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
