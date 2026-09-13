#!/usr/bin/env python3
"""Convert selected metadata records into runner-compatible Event Packets."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from pandas.tseries.holiday import USFederalHolidayCalendar


PACKET_VERSION = "metadata-event-packet-v1"
SCREENING_VERSION = "metadata-direction-screen-v1"
CN_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")


def next_estimated_session(day: date, market: str) -> date:
    candidate = day + timedelta(days=1)
    us_holidays: set[date] = set()
    if market == "US":
        calendar = USFederalHolidayCalendar()
        holidays = calendar.holidays(
            start=(day - timedelta(days=7)).isoformat(),
            end=(day + timedelta(days=14)).isoformat(),
        )
        us_holidays = {stamp.date() for stamp in holidays}
    while candidate.weekday() >= 5 or candidate in us_holidays:
        candidate += timedelta(days=1)
    return candidate


def event_id(row: dict[str, Any]) -> str:
    raw = str(row["catalog_event_id"])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    day = str(row.get("announcement_date") or row.get("cohort_date") or "").replace("-", "")
    return f"meta_{day}_{str(row.get('market') or '').lower()}_{row.get('symbol')}_{digest}"


def packet_from_row(row: dict[str, Any]) -> dict[str, Any]:
    market = str(row.get("market") or "")
    event_day = date.fromisoformat(str(row.get("announcement_date") or row["cohort_date"])[:10])
    effective = next_estimated_session(event_day, market)
    tz = CN_TZ if market == "CN" else US_TZ
    cutoff = datetime.combine(effective, time(9, 15), tzinfo=tz)
    required_fields = json.loads(row.get("required_fields_json") or "[]")
    decision_checks = json.loads(row.get("decision_checks_json") or "[]")
    preferred_horizons = json.loads(row.get("preferred_horizons_json") or "[]")
    screening = json.loads(row.get("screening_json") or "{}")
    eid = event_id(row)
    title = str(row.get("title") or "")
    native = str(row.get("native_event_type") or "")
    metadata_capsule = (
        "[METADATA_ONLY_PACKET]\n"
        f"发行人: {row.get('issuer_name') or ''}\n"
        f"证券: {market} {row.get('symbol') or ''}\n"
        f"公告日期: {event_day.isoformat()}\n"
        f"原生公告类型: {native}\n"
        f"统一事件类型: {row.get('normalized_event_type') or ''}\n"
        f"公告标题: {title}\n"
        "公告正文未预载。不得把标题当作已核验事实；研究阶段须从冻结 source_url/source_key 补证据。"
    )
    return {
        "schema_version": PACKET_VERSION,
        "event_id": eid,
        "source_record_id": row["catalog_event_id"],
        "market": market,
        "symbol": str(row.get("symbol") or ""),
        "issuer_name": row.get("issuer_name"),
        "event_time": event_day.isoformat(),
        "published_at": event_day.isoformat(),
        "timestamp_precision": "date",
        "effective_session": effective.isoformat(),
        "effective_session_is_estimate": True,
        "prediction_cutoff_at": cutoff.isoformat(),
        "event_type_l2": row.get("normalized_event_type"),
        "native_event_type": native,
        "title": title,
        "event_text": metadata_capsule,
        "event_text_kind": "metadata_capsule_not_announcement_body",
        "source_url": row.get("source_url"),
        "source_key": row.get("source_key"),
        "source": row.get("source"),
        "benchmark": "sh000300" if market == "CN" else "SPY",
        "sector_etf": None,
        "direction_prior": None,
        "event_strength": None,
        "packet_mode": "metadata_only_research_required",
        "research_required": True,
        "research_targets": {
            "required_fields": required_fields,
            "decision_checks": decision_checks,
            "preferred_horizons": preferred_horizons or ["T1", "T3"],
        },
        "metadata_screening": screening,
        "constraints": {
            "strict_as_of": True,
            "future_information_allowed": False,
            "open_web_search_allowed": False,
            "frozen_source_document_fetch_allowed": True,
            "tool_data_must_be_as_of_event": True,
            "must_predict": False,
            "neutral_allowed": True,
            "target_horizons": ["T1", "T3"],
        },
        "evaluation_status": "metadata_candidate",
        "evaluation_selection_reason": "通过 metadata-direction-screen-v1；正文未参与筛选",
        "dataset_scope": "offline_backfill",
        "prediction_eligible": False,
        "online_ineligibility_reasons": [
            "historical_backfill",
            "publication_time_has_date_precision_only",
            "effective_session_requires_exchange_calendar_confirmation",
        ],
        "related_source_record_ids": row.get("metadata_related_records") or [],
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def stratified_smoke(rows: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("event_type_l2") or "")].append(row)
    result: list[dict[str, Any]] = []
    ordered = sorted(buckets, key=lambda key: (-len(buckets[key]), key))
    while len(result) < limit and any(buckets.values()):
        for category in ordered:
            if buckets[category] and len(result) < limit:
                result.append(buckets[category].pop(0))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--screening-version", default=SCREENING_VERSION)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    records = conn.execute(
        """
        SELECT c.*, s.screening_json
        FROM v_all_event_catalog AS c
        JOIN metadata_event_screening AS s ON s.catalog_event_id = c.catalog_event_id
        WHERE s.screening_version = ? AND s.selected = 1
        ORDER BY c.cohort_date, c.market, c.symbol, c.catalog_event_id
        """,
        (args.screening_version,),
    ).fetchall()
    packets = [packet_from_row(dict(row)) for row in records]
    write_jsonl(out_dir / "event_packets_metadata_1161.jsonl", packets)
    smoke = stratified_smoke([dict(row) for row in packets], 20)
    write_jsonl(out_dir / "event_packets_smoke_20.jsonl", smoke)

    conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    created_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO metadata_event_packets(
            event_id, catalog_event_id, packet_version, market, symbol,
            event_time, event_type_l2, packet_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            packet_version=excluded.packet_version,
            event_type_l2=excluded.event_type_l2,
            packet_json=excluded.packet_json,
            created_at=excluded.created_at
        """,
        [(
            packet["event_id"], packet["source_record_id"], PACKET_VERSION,
            packet["market"], packet["symbol"], packet["event_time"],
            packet["event_type_l2"], json.dumps(packet, ensure_ascii=False), created_at,
        ) for packet in packets],
    )
    conn.commit()
    by_category = Counter(packet["event_type_l2"] for packet in packets)
    by_market = Counter(packet["market"] for packet in packets)
    manifest = {
        "packet_version": PACKET_VERSION,
        "screening_version": args.screening_version,
        "events": len(packets),
        "body_preloaded": 0,
        "research_required": len(packets),
        "dataset_scope": "offline_backfill",
        "by_market": dict(by_market),
        "by_category": dict(by_category),
        "events_file": str(out_dir / "event_packets_metadata_1161.jsonl"),
        "smoke_file": str(out_dir / "event_packets_smoke_20.jsonl"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        "# 1,161 条元数据 Event Packet", "",
        f"- Packet 版本：`{PACKET_VERSION}`",
        f"- Event 数：**{len(packets)}**",
        "- 预载公告正文：**0**",
        f"- 需要研究取证：**{len(packets)}**",
        "- 范围：历史回填离线测试；不得计入正式 Online ACC", "",
        "## 与现有 Runner 的兼容性", "",
        "每条均包含 `event_id / market / symbol / event_time / event_type_l2 / title / event_text / source_url / benchmark`。",
        "其中 `event_text` 是明确标记的元数据胶囊，不是公告正文；Agent 必须将正文缺失记录为 gap，",
        "只能访问冻结来源，并对所有工具执行严格 as-of 限制。", "",
        "## 时点限制", "",
        "原始来源只有公告日期，没有精确发布时间。`effective_session` 是估算值，正式打标签前必须用交易所日历和发布时间复核。", "",
        "## 文件", "",
        "- `event_packets_metadata_1161.jsonl`：全量测试包",
        "- `event_packets_smoke_20.jsonl`：按事件类别轮转抽取的 smoke test",
        "- `manifest.json`：版本与分布", "",
    ]
    (out_dir / "README.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
