#!/usr/bin/env python3
"""Load frozen daily cohorts and weekly semantic audit into SQLite."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from event_taxonomy import classify_event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:20]}"


def source_key(row: dict[str, Any]) -> str:
    return str(row.get("source_notice_code") or row.get("source_url") or row.get("title") or "")


def days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def upsert_cohort(conn: sqlite3.Connection, manifest: dict[str, Any]) -> None:
    counts = manifest["counts"]
    conn.execute(
        """
        INSERT INTO cohorts(
            cohort_date, collected_at, mode, frozen, raw_count, candidate_count,
            validated_count, quarantine_count, prediction_eligible_count, manifest_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cohort_date) DO UPDATE SET
            collected_at=excluded.collected_at,
            mode=excluded.mode,
            frozen=excluded.frozen,
            raw_count=excluded.raw_count,
            candidate_count=excluded.candidate_count,
            validated_count=excluded.validated_count,
            quarantine_count=excluded.quarantine_count,
            prediction_eligible_count=excluded.prediction_eligible_count,
            manifest_json=excluded.manifest_json
        """,
        (
            manifest["cohort_date"], manifest["collected_at"], manifest["mode"],
            int(bool(manifest["frozen"])), counts["raw"], counts["candidates"],
            counts["validated"], counts["quarantine"], counts["prediction_eligible"],
            json.dumps(manifest, ensure_ascii=False),
        ),
    )


def upsert_source_record(conn: sqlite3.Connection, cohort_date: str, row: dict[str, Any]) -> str:
    key = source_key(row)
    record_id = stable_id("src", cohort_date, row.get("source"), key)
    conn.execute(
        """
        INSERT INTO source_records(
            record_id, cohort_date, source, source_key, market, symbol, issuer_name,
            announcement_date, announcement_type, title, source_url, collected_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(record_id) DO UPDATE SET payload_json=excluded.payload_json
        """,
        (
            record_id, cohort_date, row.get("source") or "unknown", key,
            row.get("market"), row.get("symbol"), row.get("issuer_name"),
            row.get("announcement_date"), row.get("announcement_type"), row.get("title"),
            row.get("source_url"), row.get("collected_at"),
            json.dumps(row, ensure_ascii=False),
        ),
    )
    return record_id


def upsert_catalog_record(
    conn: sqlite3.Connection, cohort_date: str, record_id: str, row: dict[str, Any]
) -> None:
    classified = classify_event(
        str(row.get("announcement_type") or ""),
        str(row.get("title") or ""),
        str(row.get("market") or ""),
    )
    conn.execute(
        """
        INSERT INTO classified_event_catalog(
            catalog_event_id, cohort_date, source_record_id, market, symbol,
            native_event_type, normalized_event_type, catalog_role,
            information_tier, packet_status, required_fields_json,
            decision_checks_json, preferred_horizons_json, classification_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'metadata_only', ?, ?, ?, ?)
        ON CONFLICT(catalog_event_id) DO UPDATE SET
            market=excluded.market,
            symbol=excluded.symbol,
            native_event_type=excluded.native_event_type,
            normalized_event_type=excluded.normalized_event_type,
            catalog_role=excluded.catalog_role,
            information_tier=excluded.information_tier,
            required_fields_json=excluded.required_fields_json,
            decision_checks_json=excluded.decision_checks_json,
            preferred_horizons_json=excluded.preferred_horizons_json,
            classification_json=excluded.classification_json
        """,
        (
            record_id, cohort_date, record_id, row.get("market"), row.get("symbol"),
            row.get("announcement_type"), classified["normalized_event_type"],
            classified["catalog_role"], classified["information_tier"],
            json.dumps(classified["required_fields"], ensure_ascii=False),
            json.dumps(classified["decision_checks"], ensure_ascii=False),
            json.dumps(classified["preferred_horizons"], ensure_ascii=False),
            json.dumps({"taxonomy_match": classified["taxonomy_match"]}, ensure_ascii=False),
        ),
    )


def upsert_candidate(conn: sqlite3.Connection, cohort_date: str, row: dict[str, Any]) -> None:
    record_id = stable_id("src", cohort_date, row.get("source"), source_key(row))
    candidate_id = stable_id("cand", cohort_date, row.get("source"), source_key(row), row.get("event_type_l2"))
    conn.execute(
        """
        INSERT INTO event_candidates(
            candidate_id, cohort_date, source_record_id, market, symbol, event_type_l2,
            title, quality_score, document_fetch_ok, document_fetch_error, candidate_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(candidate_id) DO UPDATE SET
            quality_score=excluded.quality_score,
            document_fetch_ok=excluded.document_fetch_ok,
            document_fetch_error=excluded.document_fetch_error,
            candidate_json=excluded.candidate_json
        """,
        (
            candidate_id, cohort_date, record_id, row.get("market") or "",
            row.get("symbol") or "", row.get("event_type_l2") or "",
            row.get("title") or "", int(row.get("quality_score") or 0),
            int(bool(row.get("document_fetch_ok"))), row.get("document_fetch_error"),
            json.dumps(row, ensure_ascii=False),
        ),
    )


def upsert_event(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO events(
            event_id, cohort_date, market, symbol, issuer_name, event_time, published_at,
            effective_session, prediction_cutoff_at, event_type_l2, title, event_text,
            source_url, benchmark, quality_score, quality_pass, evaluation_status,
            evaluation_selection_reason, prediction_eligible, source_document_hash,
            source_document_count, event_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            evaluation_status=excluded.evaluation_status,
            evaluation_selection_reason=excluded.evaluation_selection_reason,
            prediction_eligible=excluded.prediction_eligible,
            event_json=excluded.event_json
        """,
        (
            row["event_id"], row["cohort_date"], row["market"], row["symbol"],
            row.get("issuer_name"), row.get("event_time") or row["cohort_date"],
            row.get("published_at"), row.get("effective_session"),
            row.get("prediction_cutoff_at"), row["event_type_l2"], row["title"],
            row.get("event_text") or "", row.get("source_url") or "", row.get("benchmark"),
            int(row.get("quality_score") or 0), int(bool(row.get("quality_pass"))),
            row["evaluation_status"], row.get("evaluation_selection_reason"),
            int(bool(row.get("prediction_eligible"))), row.get("source_document_hash"),
            int(row.get("source_document_count") or 1), json.dumps(row, ensure_ascii=False),
        ),
    )
    documents = row.get("related_documents") or [{
        "title": row.get("title"),
        "source_url": row.get("source_url"),
        "source_notice_code": row.get("source_notice_code"),
        "document_fetch_ok": True,
    }]
    for document in documents:
        code = document.get("source_notice_code")
        url = document.get("source_url")
        source_id = stable_id("evsrc", row["event_id"], code, url)
        role = "primary" if code == row.get("source_notice_code") or url == row.get("source_url") else "related"
        conn.execute(
            """
            INSERT INTO event_sources(
                source_id, event_id, source_role, title, source_url,
                source_notice_code, document_fetch_ok
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                source_role=excluded.source_role,
                document_fetch_ok=excluded.document_fetch_ok
            """,
            (
                source_id, row["event_id"], role, document.get("title"), url, code,
                int(bool(document.get("document_fetch_ok"))),
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--input-root",
        default=str(PROJECT_ROOT / "pronoia_run" / "online_test"),
    )
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "pronoia_run" / "online_test" / "online_test.sqlite3"),
    )
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    root, db_path = Path(args.input_root), Path(args.db)
    audit_dir = root / f"week_{start.isoformat()}_{end.isoformat()}"
    if not audit_dir.exists():
        raise SystemExit(f"weekly audit not found: {audit_dir}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn:
            for day in days(start, end):
                cohort_dir = root / day.isoformat()
                manifest_path = cohort_dir / "manifest.json"
                if not manifest_path.exists():
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                upsert_cohort(conn, manifest)
                for row in jsonl(cohort_dir / "raw.jsonl"):
                    record_id = upsert_source_record(conn, day.isoformat(), row)
                    upsert_catalog_record(conn, day.isoformat(), record_id, row)
                for row in jsonl(cohort_dir / "candidates.jsonl"):
                    upsert_candidate(conn, day.isoformat(), row)

            conn.execute(
                """
                UPDATE classified_event_catalog
                SET packet_status = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM event_candidates c
                        WHERE c.source_record_id = classified_event_catalog.source_record_id
                          AND c.document_fetch_ok = 1
                    ) THEN 'document_fetched'
                    WHEN EXISTS (
                        SELECT 1 FROM event_candidates c
                        WHERE c.source_record_id = classified_event_catalog.source_record_id
                          AND c.document_fetch_error = 'skipped_below_quality_threshold'
                    ) THEN 'document_skipped_by_legacy_score'
                    WHEN EXISTS (
                        SELECT 1 FROM event_candidates c
                        WHERE c.source_record_id = classified_event_catalog.source_record_id
                    ) THEN 'document_fetch_failed_or_short'
                    ELSE 'metadata_only'
                END
                """
            )

            event_rows: list[dict[str, Any]] = []
            for name in ("strict_candidates.jsonl", "review_candidates.jsonl", "excluded_or_duplicate.jsonl"):
                event_rows.extend(jsonl(audit_dir / name))
            for row in event_rows:
                upsert_event(conn, row)

            now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            metadata = {
                "schema_version": "online-test-db-v1",
                "built_at": now,
                "cohort_start": start.isoformat(),
                "cohort_end": end.isoformat(),
            }
            conn.executemany(
                "INSERT INTO metadata(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                metadata.items(),
            )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "cohorts", "source_records", "classified_event_catalog",
                "event_candidates", "events", "event_sources",
            )
        }
        counts["offline_evaluation_queue"] = conn.execute(
            "SELECT COUNT(*) FROM v_offline_evaluation_queue"
        ).fetchone()[0]
        counts["review_queue"] = conn.execute("SELECT COUNT(*) FROM v_review_queue").fetchone()[0]
        counts["online_prediction_queue"] = conn.execute(
            "SELECT COUNT(*) FROM v_online_prediction_queue"
        ).fetchone()[0]
        print(json.dumps({
            "db": str(db_path),
            "integrity": integrity,
            "foreign_key_errors": len(foreign_keys),
            "counts": counts,
        }, ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
