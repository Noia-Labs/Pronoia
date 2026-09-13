"""SQLite repository for Model Lab.

This module is the only Model Lab code allowed to use the database module's
connection/lock primitives.  JSON decoding is defensive so an older or partly
migrated local database remains readable.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from .. import db
from .defaults import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_TIMEOUT_SECONDS
from .secret_store import secret_reference_configured
from .question_bank import (
    BUILTIN_QUESTION_SETS,
    BUILTIN_SET_ID,
)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def _profile(row: sqlite3.Row | Mapping[str, Any] | None, *, public: bool) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["is_active"] = bool(item.get("is_active"))
    ref = str(item.get("secret_env_ref") or "")
    from .platform_default import is_platform_profile_id, matches_environment
    if is_platform_profile_id(item.get("id")):
        from .. import config
        item["secret_configured"] = bool(matches_environment(item) and config.LLM_API_KEY)
    else:
        item["secret_configured"] = secret_reference_configured(ref)
    if not public:
        return item
    # Public DTOs expose only whether a secret is available.  Even the
    # environment-variable name stays server-side so callers cannot enumerate
    # deployment configuration.
    item.pop("secret_env_ref", None)
    return item


def create_profile(data: Mapping[str, Any]) -> dict:
    profile_id, ts = db.new_id(), db.now_iso()
    values = (
        profile_id,
        data["name"],
        data["provider"],
        data["base_url"],
        data["model_id"],
        data["secret_env_ref"],
        int(data.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS),
        float(data.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        data.get("thinking_mode") or "auto",
        float(data.get("input_price_per_million") or 0),
        float(data.get("output_price_per_million") or 0),
        data.get("currency") or "CNY",
        1 if data.get("is_active", True) else 0,
        ts,
        ts,
    )
    with db._lock:
        conn = db._get_conn()
        conn.execute(
            """
            INSERT INTO ml_model_profiles(
                id,name,provider,base_url,model_id,secret_env_ref,
                max_output_tokens,timeout_seconds,thinking_mode,
                input_price_per_million,output_price_per_million,currency,
                is_active,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            values,
        )
        conn.commit()
    return get_profile(profile_id, public=True) or {"id": profile_id}


def list_profiles(*, active_only: bool = False, public: bool = True) -> list[dict]:
    from .platform_default import PLATFORM_PROFILE_PREFIX
    where = "WHERE substr(id,1,?) != ?" + (" AND is_active=1" if active_only else "")
    with db._lock:
        rows = db._get_conn().execute(
            f"SELECT * FROM ml_model_profiles {where} ORDER BY updated_at DESC, name COLLATE NOCASE",
            (len(PLATFORM_PROFILE_PREFIX), PLATFORM_PROFILE_PREFIX),
        ).fetchall()
    return [item for row in rows if (item := _profile(row, public=public)) is not None]


def get_profile(profile_id: str, *, public: bool = True) -> dict | None:
    with db._lock:
        row = db._get_conn().execute(
            "SELECT * FROM ml_model_profiles WHERE id=?", (profile_id,)
        ).fetchone()
    return _profile(row, public=public)


def update_profile(profile_id: str, updates: Mapping[str, Any]) -> dict | None:
    allowed = {
        "name", "provider", "base_url", "model_id", "secret_env_ref",
        "max_output_tokens", "timeout_seconds", "thinking_mode",
        "input_price_per_million", "output_price_per_million", "currency", "is_active",
    }
    fields: list[str] = []
    args: list[Any] = []
    for key, value in updates.items():
        if key not in allowed:
            continue
        fields.append(f"{key}=?")
        args.append(1 if key == "is_active" and bool(value) else 0 if key == "is_active" else value)
    if not fields:
        return get_profile(profile_id, public=True)
    fields.append("updated_at=?")
    args.extend([db.now_iso(), profile_id])
    with db._lock:
        conn = db._get_conn()
        cursor = conn.execute(
            f"UPDATE ml_model_profiles SET {', '.join(fields)} WHERE id=?", tuple(args)
        )
        conn.commit()
    return get_profile(profile_id, public=True) if cursor.rowcount else None


def record_validation(profile_id: str, *, ok: bool, message: str) -> dict | None:
    ts = db.now_iso()
    safe_message = str(message or "")[:500]
    with db._lock:
        conn = db._get_conn()
        conn.execute(
            """UPDATE ml_model_profiles
               SET last_validated_at=?,last_validation_status=?,last_validation_message=?,updated_at=?
               WHERE id=?""",
            (ts, "ok" if ok else "failed", safe_message, ts, profile_id),
        )
        conn.commit()
    return get_profile(profile_id, public=True)


def get_default_profile_id() -> str | None:
    try:
        with db._lock:
            row = db._get_conn().execute(
                "SELECT value_json FROM ml_settings WHERE key='default_profile_id'"
            ).fetchone()
    except sqlite3.OperationalError:
        # Some low-level LLM unit tests intentionally exercise helpers without
        # running FastAPI startup. Preserve the legacy env fallback there.
        return None
    parsed = _loads(row["value_json"], None) if row else None
    return str(parsed) if parsed else None


def get_default_profile(*, public: bool = False) -> dict | None:
    profile_id = get_default_profile_id()
    if not profile_id:
        return None
    profile = get_profile(profile_id, public=public)
    if not profile or not profile.get("is_active"):
        return None
    return profile


def set_default_profile(profile_id: str) -> dict:
    ts = db.now_iso()
    with db._lock:
        conn = db._get_conn()
        conn.execute(
            """INSERT INTO ml_settings(key,value_json,updated_at) VALUES('default_profile_id',?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
            (json.dumps(profile_id), ts),
        )
        conn.commit()
    return get_profile(profile_id, public=True) or {"id": profile_id}


def delete_profile(profile_id: str) -> bool:
    with db._lock:
        conn = db._get_conn()
        cursor = conn.execute("DELETE FROM ml_model_profiles WHERE id=?", (profile_id,))
        conn.commit()
    return cursor.rowcount > 0


def seed_builtin_question_set() -> None:
    """Add missing bundled versioned banks without mutating saved questions.

    Built-in ids include a version so upgrades can add banks while historical
    experiments keep the same question references. Explicit revisions can
    archive questions and change visible codes once; routine reads never
    rewrite saved content, and custom banks remain entirely user-owned.
    """
    ts = db.now_iso()
    with db._lock:
        conn = db._get_conn()
        for question_set in BUILTIN_QUESTION_SETS:
            set_id = question_set["id"]
            conn.execute(
                """INSERT OR IGNORE INTO ml_question_sets(
                    id,name,description,version,is_builtin,created_at,updated_at
                ) VALUES(?,?,?,?,1,?,?)""",
                (
                    set_id, question_set["name"], question_set.get("description"),
                    question_set.get("version") or "1", ts, ts,
                ),
            )
            owner = conn.execute(
                "SELECT is_builtin FROM ml_question_sets WHERE id=?", (set_id,),
            ).fetchone()
            if not owner or not owner["is_builtin"]:
                # Do not adopt or populate an imported custom bank even if its
                # id happens to collide with a newly introduced built-in id.
                continue
            for position, question in enumerate(question_set["questions"], start=1):
                stable_code = question.get("stable_code") or question["code"]
                conn.execute(
                    """INSERT OR IGNORE INTO ml_questions(
                        id,question_set_id,code,experiment,category,role,method,scenario,prompt,
                        skills_json,deliverable,gold_standard,metrics,risk,priority,position,
                        created_at,updated_at,display_code
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{set_id}:{stable_code}", set_id,
                        stable_code, question.get("experiment"), question.get("category"),
                        question.get("role"), question.get("method"), question.get("scenario"),
                        question["prompt"], json.dumps(question.get("skills") or [], ensure_ascii=False),
                        question.get("deliverable"), question.get("gold_standard"),
                        question.get("metrics"), question.get("risk"), question.get("priority"),
                        position, ts, ts, question["code"],
                    ),
                )
            _apply_question_bank_revision(conn, question_set, ts)
        conn.commit()


def _apply_question_bank_revision(conn: sqlite3.Connection, question_set: Mapping[str, Any], ts: str) -> None:
    """Apply only explicit bundled label/retirement changes, preserving history."""
    revision = question_set.get("revision")
    if not revision:
        return
    set_id = question_set["id"]
    key = f"question-bank:{set_id}:{revision}"
    if conn.execute("SELECT 1 FROM ml_settings WHERE key=?", (key,)).fetchone():
        return
    for code in question_set.get("archived_codes") or []:
        conn.execute(
            "UPDATE ml_questions SET is_archived=1 WHERE id=? AND question_set_id=? AND code=?",
            (f"{set_id}:{code}", set_id, code),
        )
    for question in question_set["questions"]:
        stable_code = question.get("stable_code") or question["code"]
        conn.execute(
            "UPDATE ml_questions SET display_code=? WHERE id=? AND question_set_id=? AND code=?",
            (question["code"], f"{set_id}:{stable_code}", set_id, stable_code),
        )
    # Only the known bundled description is upgraded. User-edited bank names,
    # descriptions, question text and all evaluation snapshots remain intact.
    if question_set.get("previous_description"):
        conn.execute(
            "UPDATE ml_question_sets SET description=? WHERE id=? AND description=?",
            (question_set.get("description"), set_id, question_set["previous_description"]),
        )
    conn.execute(
        "INSERT INTO ml_settings(key,value_json,updated_at) VALUES(?,?,?)",
        (key, json.dumps({"revision": revision}), ts),
    )


def _question(row: sqlite3.Row | Mapping[str, Any]) -> dict:
    item = dict(row)
    item["code"] = item.pop("display_code", None) or item["code"]
    item.pop("is_archived", None)
    item["skills"] = _loads(item.pop("skills_json", None), [])
    return item


def list_question_sets() -> list[dict]:
    seed_builtin_question_set()
    with db._lock:
        rows = db._get_conn().execute(
            """SELECT s.*,COUNT(q.id) AS question_count
               FROM ml_question_sets s LEFT JOIN ml_questions q ON q.question_set_id=s.id AND q.is_archived=0
               GROUP BY s.id
               ORDER BY s.is_builtin DESC,CASE WHEN s.id=? THEN 0 ELSE 1 END,
                        s.updated_at DESC,s.name COLLATE NOCASE,s.id""",
            (BUILTIN_SET_ID,),
        ).fetchall()
    output: list[dict] = []
    for row in rows:
        item = dict(row)
        item["is_builtin"] = bool(item.get("is_builtin"))
        item["question_count"] = int(item.get("question_count") or 0)
        output.append(item)
    return output


def get_question_set(question_set_id: str) -> dict | None:
    seed_builtin_question_set()
    with db._lock:
        conn = db._get_conn()
        row = conn.execute("SELECT * FROM ml_question_sets WHERE id=?", (question_set_id,)).fetchone()
        questions = conn.execute(
            "SELECT * FROM ml_questions WHERE question_set_id=? AND is_archived=0 ORDER BY position,code",
            (question_set_id,),
        ).fetchall() if row else []
    if not row:
        return None
    item = dict(row)
    item["is_builtin"] = bool(item.get("is_builtin"))
    item["questions"] = [_question(question) for question in questions]
    item["question_count"] = len(questions)
    return item


def create_question_set(data: Mapping[str, Any]) -> dict:
    set_id, ts = db.new_id(), db.now_iso()
    questions = list(data.get("questions") or [])
    with db._lock:
        conn = db._get_conn()
        conn.execute(
            """INSERT INTO ml_question_sets(id,name,description,version,is_builtin,created_at,updated_at)
               VALUES(?,?,?,?,0,?,?)""",
            (set_id, data["name"], data.get("description"), data.get("version") or "1", ts, ts),
        )
        for position, question in enumerate(questions, start=1):
            conn.execute(
                """INSERT INTO ml_questions(
                    id,question_set_id,code,experiment,category,role,method,scenario,prompt,
                    skills_json,deliverable,gold_standard,metrics,risk,priority,position,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    db.new_id(), set_id, question["code"], question.get("experiment"),
                    question.get("category"), question.get("role"), question.get("method"),
                    question.get("scenario"), question["prompt"],
                    json.dumps(question.get("skills") or [], ensure_ascii=False),
                    question.get("deliverable"), question.get("gold_standard"),
                    question.get("metrics"), question.get("risk"), question.get("priority"),
                    position, ts, ts,
                ),
            )
        conn.commit()
    return get_question_set(set_id) or {"id": set_id}


def selected_questions(question_set_id: str, selectors: Iterable[str]) -> list[dict]:
    question_set = get_question_set(question_set_id)
    if not question_set:
        return []
    questions = list(question_set["questions"])
    selected = {str(item) for item in selectors if str(item)}
    if not selected:
        return questions
    # Resolve IDs before visible short codes. Retired IDs cannot select their
    # replacement, even if an imported custom code happens to equal that ID.
    with db._lock:
        known_ids = {row["id"] for row in db._get_conn().execute("SELECT id FROM ml_questions")}
    selected_ids = selected & known_ids
    selected_codes = selected - known_ids
    return [item for item in questions if item["id"] in selected_ids or item["code"] in selected_codes]


def _batch(row: sqlite3.Row | Mapping[str, Any] | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["profile_ids"] = _loads(item.pop("profile_ids_json", None), [])
    item["prediction"] = _loads(item.pop("prediction_config_json", None), {})
    item["qa"] = _loads(item.pop("qa_config_json", None), {})
    item["scoring"] = _loads(item.pop("scoring_config_json", None), {})
    item["demo"] = bool(item["scoring"].get("dry_run"))
    return item


def _task(row: sqlite3.Row | Mapping[str, Any] | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["config"] = _loads(item.pop("config_json", None), {})
    item["result_summary"] = _loads(item.pop("result_json", None), None)
    total = int(item.get("total_items") or 0)
    done = int(item.get("done_items") or 0)
    item["progress"] = (done / total) if total > 0 else (1.0 if item.get("status") == "done" else 0.0)
    item["demo"] = bool(item["config"].get("dry_run"))
    summary = item.get("result_summary") or {}
    item["warning_count"] = int(summary.get("warning_count") or 0)
    item["completion_quality"] = summary.get("completion_quality")
    item["arena_eligible"] = bool(
        item.get("kind") == "prediction"
        and item.get("backtest_run_id")
        and item.get("status") == "done"
        and not item["demo"]
        and not item["warning_count"]
        and not summary.get("metrics_error")
        and summary.get("arena_eligible", True)
    )
    return item


def prediction_batch_quality(tasks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Expose result quality without changing execution/restart states."""
    summaries = [
        task.get("result_summary") or {}
        for task in tasks if task.get("kind") == "prediction" and not task.get("demo")
    ]
    totals = {
        key: sum(int(summary.get(key) or 0) for summary in summaries)
        for key in ("warning_count", "insufficient_data_count", "invalid_output_count")
    }
    totals["metrics_error_count"] = sum(bool(s.get("metrics_error")) for s in summaries)
    totals["completion_quality"] = (
        "completed_with_warnings" if totals["warning_count"] or totals["metrics_error_count"]
        else "valid" if summaries and all(s.get("completion_quality") == "valid" for s in summaries)
        else None
    )
    return totals


def list_prediction_issues(run_id: str, *, runner: str = "", limit: int = 20) -> list[dict]:
    """Bound the explanation list; authoritative counts come from the Run."""
    issues: list[dict] = []
    with db._lock:
        rows = db._get_conn().execute(
            "SELECT event_id,symbol,rationale,abstain,strategy_metadata_json FROM bt_predictions "
            "WHERE run_id=? ORDER BY created_at,event_id", (run_id,),
        )
        for row in rows:
            metadata = _loads(row["strategy_metadata_json"], {})
            metadata = metadata if isinstance(metadata, dict) else {}
            validation = metadata.get("validation")
            status = str(metadata.get("prediction_status") or "")
            invalid = (
                status == "invalid_output" or metadata.get("output_failure_kind")
                or metadata.get("completion_quality") == "invalid"
                or metadata.get("output_validation_errors")
                or (isinstance(validation, dict) and validation.get("valid") is False)
                or (row["abstain"] and not metadata and runner in {"team_prompt", "team_full"})
            )
            if invalid:
                status = "invalid_output"
            elif status == "insufficient_data":
                pass
            elif row["abstain"]:
                status = "voluntary_abstain"
            else:
                continue
            issues.append({
                "event_id": row["event_id"], "symbol": row["symbol"],
                "prediction_status": status,
                "reason": str(row["rationale"] or "未记录具体原因，请查看运行日志")[:2000],
            })
            if len(issues) >= limit:
                break
    return issues


def _answer(row: sqlite3.Row | Mapping[str, Any]) -> dict:
    item = dict(row)
    for source, target, default in (
        ("usage_json", "usage", None),
        ("tool_trace_json", "tool_trace", []),
        ("auto_score_json", "auto_score", None),
        ("manual_score_json", "manual_score", None),
        ("final_score_json", "final_score", None),
    ):
        item[target] = _loads(item.pop(source, None), default)
    return item


def create_batch(
    *,
    name: str,
    profile_ids: list[str],
    dataset_id: str | None,
    dataset_version: str | None,
    question_set_id: str | None,
    prediction: Mapping[str, Any],
    qa: Mapping[str, Any],
    scoring: Mapping[str, Any],
    task_specs: list[Mapping[str, Any]],
    _commit: bool = True,
) -> dict:
    batch_id, ts = db.new_id(), db.now_iso()
    with db._lock:
        conn = db._get_conn()
        conn.execute(
            """INSERT INTO ml_batches(
                id,name,status,profile_ids_json,dataset_id,dataset_version,question_set_id,
                prediction_config_json,qa_config_json,scoring_config_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                batch_id, name, "pending", json.dumps(profile_ids, ensure_ascii=False),
                dataset_id, dataset_version, question_set_id,
                json.dumps(dict(prediction), ensure_ascii=False),
                json.dumps(dict(qa), ensure_ascii=False),
                json.dumps(dict(scoring), ensure_ascii=False), ts, ts,
            ),
        )
        for spec in task_specs:
            conn.execute(
                """INSERT INTO ml_tasks(
                    id,batch_id,kind,profile_id,status,total_items,done_items,config_json,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    db.new_id(), batch_id, spec["kind"], spec["profile_id"], "pending",
                    int(spec.get("total_items") or 0), 0,
                    json.dumps(dict(spec.get("config") or {}), ensure_ascii=False), ts, ts,
                ),
            )
        if _commit:
            conn.commit()
    return get_batch(batch_id, include_tasks=True) or {"id": batch_id}


def list_batches(limit: int = 100) -> list[dict]:
    with db._lock:
        rows = db._get_conn().execute(
            "SELECT * FROM ml_batches ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
    output: list[dict] = []
    for row in rows:
        item = _batch(row)
        if item:
            item["task_counts"] = task_counts(str(item["id"]))
            item.update(prediction_batch_quality(list_tasks(str(item["id"]))))
            output.append(item)
    return output


def create_event_experiment(
    prepared_run: Mapping[str, Any], *, profiles: list[Mapping[str, Any]],
    qa_batch: Mapping[str, Any] | None, auto_start: bool,
) -> tuple[dict, str | None]:
    """Commit one event Run and its optional QA sidecar as a single unit."""
    from .platform_default import is_platform_profile_id

    run_id = str(prepared_run["run_id"])
    with db._lock:
        conn = db._get_conn()
        try:
            conn.execute("BEGIN")
            for profile in profiles:
                if not is_platform_profile_id(profile.get("id")):
                    continue
                ts = db.now_iso()
                conn.execute(
                    """INSERT OR IGNORE INTO ml_model_profiles(
                        id,name,provider,base_url,model_id,secret_env_ref,
                        max_output_tokens,timeout_seconds,thinking_mode,
                        input_price_per_million,output_price_per_million,currency,
                        is_active,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    tuple(profile.get(key) for key in (
                        "id", "name", "provider", "base_url", "model_id", "secret_env_ref",
                        "max_output_tokens", "timeout_seconds", "thinking_mode",
                        "input_price_per_million", "output_price_per_million", "currency",
                    )) + (ts, ts),
                )
            db.create_bt_run(**dict(prepared_run), _commit=False)
            batch_id = None
            if qa_batch:
                batch = create_batch(**dict(qa_batch), _commit=False)
                batch_id = str(batch["id"])
                config = dict(prepared_run.get("config") or {})
                config["event_qa_batch_id"] = batch_id
                conn.execute(
                    "UPDATE bt_runs SET config_json=? WHERE id=?",
                    (json.dumps(config, ensure_ascii=False), run_id),
                )
            conn.execute(
                "INSERT INTO ml_event_experiments(run_id,qa_batch_id,auto_start,created_at) VALUES(?,?,?,?)",
                (run_id, batch_id, int(auto_start), db.now_iso()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return db.get_bt_run(run_id) or {"id": run_id}, batch_id


def get_event_experiment(run_id: str) -> dict | None:
    with db._lock:
        row = db._get_conn().execute(
            "SELECT * FROM ml_event_experiments WHERE run_id=?", (run_id,),
        ).fetchone()
    return dict(row) if row else None


def _history_id_chunks(values: Iterable[str], size: int = 400) -> Iterable[list[str]]:
    ordered = list(dict.fromkeys(str(value) for value in values if str(value)))
    for index in range(0, len(ordered), size):
        yield ordered[index:index + size]


def _history_existing_ids(conn: sqlite3.Connection, table: str, values: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for chunk in _history_id_chunks(values):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(f"SELECT id FROM {table} WHERE id IN ({placeholders})", tuple(chunk)).fetchall()
        found.update(str(row["id"]) for row in rows)
    return found


def _history_json_contains_identifier(value: Any, identifiers: set[str]) -> bool:
    if isinstance(value, str):
        return value in identifiers
    if isinstance(value, Mapping):
        return any(_history_json_contains_identifier(item, identifiers) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_history_json_contains_identifier(item, identifiers) for item in value)
    return False


def _history_deletion_plan(
    conn: sqlite3.Connection,
    requested_run_ids: Iterable[str],
    requested_batch_ids: Iterable[str],
) -> dict[str, Any]:
    """Resolve cascades and non-FK blockers for an all-or-nothing history delete."""
    requested_runs = list(dict.fromkeys(str(item) for item in requested_run_ids if str(item)))
    requested_batches = list(dict.fromkeys(str(item) for item in requested_batch_ids if str(item)))
    existing_runs = _history_existing_ids(conn, "bt_runs", requested_runs)
    existing_batches = _history_existing_ids(conn, "ml_batches", requested_batches)
    run_ids, batch_ids = set(existing_runs), set(existing_batches)

    # A visible Model Lab batch owns its prediction Runs. An event Run owns its
    # hidden QA sidecar batch. Resolve both directions until the set is stable.
    scanned_runs: set[str] = set()
    scanned_batches: set[str] = set()
    while run_ids - scanned_runs or batch_ids - scanned_batches:
        new_batches = batch_ids - scanned_batches
        for chunk in _history_id_chunks(new_batches):
            placeholders = ",".join("?" for _ in chunk)
            task_rows = conn.execute(
                f"SELECT backtest_run_id FROM ml_tasks WHERE batch_id IN ({placeholders}) "
                "AND backtest_run_id IS NOT NULL",
                tuple(chunk),
            ).fetchall()
            run_ids.update(str(row["backtest_run_id"]) for row in task_rows if row["backtest_run_id"])
            # Close the create-Run -> link-task crash gap, but only for lineage
            # that public Run creation cannot author: a frozen model snapshot,
            # the exact task id, and a still-unlinked task must all agree.
            recovery_rows = conn.execute(
                f"SELECT DISTINCT r.id FROM bt_runs r JOIN ml_tasks t "
                f"ON t.batch_id IN ({placeholders}) AND t.backtest_run_id IS NULL "
                "AND json_valid(r.config_json) "
                "AND json_type(r.config_json,'$.model_profile_snapshot')='object' "
                "AND json_extract(r.config_json,'$.evaluation_source')='model_lab' "
                "AND CAST(json_extract(r.config_json,'$.model_lab_batch_id') AS TEXT)=t.batch_id "
                "AND CAST(json_extract(r.config_json,'$.model_lab_task_id') AS TEXT)=t.id",
                tuple(chunk),
            ).fetchall()
            run_ids.update(str(row["id"]) for row in recovery_rows)
        scanned_batches.update(new_batches)

        new_runs = run_ids - scanned_runs
        for chunk in _history_id_chunks(new_runs):
            placeholders = ",".join("?" for _ in chunk)
            sidecars = conn.execute(
                f"SELECT qa_batch_id FROM ml_event_experiments WHERE run_id IN ({placeholders}) "
                "AND qa_batch_id IS NOT NULL",
                tuple(chunk),
            ).fetchall()
            batch_ids.update(str(row["qa_batch_id"]) for row in sidecars if row["qa_batch_id"])
        scanned_runs.update(new_runs)

    run_rows: list[dict[str, Any]] = []
    batch_rows: list[dict[str, Any]] = []
    for chunk in _history_id_chunks(run_ids):
        placeholders = ",".join("?" for _ in chunk)
        run_rows.extend(dict(row) for row in conn.execute(
            f"SELECT id,name,status FROM bt_runs WHERE id IN ({placeholders})", tuple(chunk)
        ).fetchall())
    for chunk in _history_id_chunks(batch_ids):
        placeholders = ",".join("?" for _ in chunk)
        batch_rows.extend(dict(row) for row in conn.execute(
            f"SELECT id,name,status FROM ml_batches WHERE id IN ({placeholders})", tuple(chunk)
        ).fetchall())

    external_tasks: list[dict[str, Any]] = []
    for chunk in _history_id_chunks(run_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT DISTINCT t.batch_id,b.name AS batch_name,t.backtest_run_id "
            f"FROM ml_tasks t JOIN ml_batches b ON b.id=t.batch_id "
            f"WHERE t.backtest_run_id IN ({placeholders})",
            tuple(chunk),
        ).fetchall()
        external_tasks.extend(dict(row) for row in rows if str(row["batch_id"]) not in batch_ids)
        recovery_rows = conn.execute(
            f"SELECT DISTINCT t.batch_id,b.name AS batch_name,r.id AS backtest_run_id "
            f"FROM bt_runs r JOIN ml_tasks t ON t.backtest_run_id IS NULL "
            "AND json_valid(r.config_json) "
            "AND json_type(r.config_json,'$.model_profile_snapshot')='object' "
            "AND json_extract(r.config_json,'$.evaluation_source')='model_lab' "
            "AND CAST(json_extract(r.config_json,'$.model_lab_batch_id') AS TEXT)=t.batch_id "
            "AND CAST(json_extract(r.config_json,'$.model_lab_task_id') AS TEXT)=t.id "
            f"JOIN ml_batches b ON b.id=t.batch_id WHERE r.id IN ({placeholders})",
            tuple(chunk),
        ).fetchall()
        external_tasks.extend(dict(row) for row in recovery_rows if str(row["batch_id"]) not in batch_ids)

    sidecar_parents: list[dict[str, Any]] = []
    for chunk in _history_id_chunks(batch_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT e.run_id,e.qa_batch_id,r.name AS run_name FROM ml_event_experiments e "
            f"JOIN bt_runs r ON r.id=e.run_id WHERE e.qa_batch_id IN ({placeholders})",
            tuple(chunk),
        ).fetchall()
        sidecar_parents.extend(dict(row) for row in rows if str(row["run_id"]) not in run_ids)

    arena_blockers: list[dict[str, Any]] = []
    if run_ids:
        for row in conn.execute("SELECT id,name,run_ids_json,config_json,result_json FROM bt_arenas").fetchall():
            run_refs = _loads(row["run_ids_json"], [])
            config_refs = _loads(row["config_json"], {})
            result_refs = _loads(row["result_json"], {})
            if (_history_json_contains_identifier(run_refs, run_ids)
                    or _history_json_contains_identifier(config_refs, run_ids)
                    or _history_json_contains_identifier(result_refs, run_ids)):
                arena_blockers.append({"id": str(row["id"]), "name": str(row["name"] or row["id"])})

    return {
        "requested_run_ids": requested_runs,
        "requested_batch_ids": requested_batches,
        "missing_run_ids": [item for item in requested_runs if item not in existing_runs],
        "missing_batch_ids": [item for item in requested_batches if item not in existing_batches],
        "run_ids": sorted(run_ids),
        "batch_ids": sorted(batch_ids),
        "runs": run_rows,
        "batches": batch_rows,
        "external_tasks": external_tasks,
        "sidecar_parents": sidecar_parents,
        "arena_blockers": arena_blockers,
    }


def plan_history_record_deletion(run_ids: Iterable[str], batch_ids: Iterable[str]) -> dict[str, Any]:
    with db._lock:
        return _history_deletion_plan(db._get_conn(), run_ids, batch_ids)


def commit_history_record_deletion(
    run_ids: Iterable[str],
    batch_ids: Iterable[str],
    *,
    expected_run_ids: Iterable[str],
    expected_batch_ids: Iterable[str],
) -> dict[str, Any]:
    """Re-plan under a write transaction and delete only if dependencies stayed unchanged."""
    with db._lock:
        conn = db._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = _history_deletion_plan(conn, run_ids, batch_ids)
            expected_runs = set(str(item) for item in expected_run_ids)
            expected_batches = set(str(item) for item in expected_batch_ids)
            changed = set(plan["run_ids"]) != expected_runs or set(plan["batch_ids"]) != expected_batches
            blocked = any(plan[key] for key in (
                "missing_run_ids", "missing_batch_ids", "external_tasks", "sidecar_parents", "arena_blockers",
            ))
            if changed or blocked:
                conn.rollback()
                return {"ok": False, "reason": "dependencies_changed", "plan": plan}
            for chunk in _history_id_chunks(plan["batch_ids"]):
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM ml_batches WHERE id IN ({placeholders})", tuple(chunk))
            for chunk in _history_id_chunks(plan["run_ids"]):
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM bt_runs WHERE id IN ({placeholders})", tuple(chunk))
            conn.commit()
            return {
                "ok": True,
                "deleted_run_ids": plan["run_ids"],
                "deleted_batch_ids": plan["batch_ids"],
            }
        except Exception:
            conn.rollback()
            raise


def mark_event_launch_intent(run_id: str) -> None:
    with db._lock:
        conn = db._get_conn()
        conn.execute("UPDATE ml_event_experiments SET auto_start=1 WHERE run_id=?", (run_id,))
        conn.commit()


def interrupted_event_run_ids() -> list[str]:
    with db._lock:
        rows = db._get_conn().execute(
            """SELECT e.run_id FROM ml_event_experiments e JOIN bt_runs r ON r.id=e.run_id
               WHERE e.auto_start=1 AND r.status IN ('pending','running')""",
        ).fetchall()
    return [str(row["run_id"]) for row in rows]


def get_batch(batch_id: str, *, include_tasks: bool = False) -> dict | None:
    with db._lock:
        row = db._get_conn().execute("SELECT * FROM ml_batches WHERE id=?", (batch_id,)).fetchone()
    item = _batch(row)
    if item:
        tasks = list_tasks(batch_id)
        item.update(prediction_batch_quality(tasks))
        if include_tasks:
            item["tasks"] = tasks
    return item


def list_tasks(batch_id: str) -> list[dict]:
    with db._lock:
        rows = db._get_conn().execute(
            "SELECT * FROM ml_tasks WHERE batch_id=? ORDER BY created_at,kind,profile_id",
            (batch_id,),
        ).fetchall()
    return [item for row in rows if (item := _task(row)) is not None]


def get_task(task_id: str) -> dict | None:
    with db._lock:
        row = db._get_conn().execute("SELECT * FROM ml_tasks WHERE id=?", (task_id,)).fetchone()
    return _task(row)


def task_counts(batch_id: str) -> dict[str, int]:
    with db._lock:
        rows = db._get_conn().execute(
            "SELECT status,COUNT(*) AS n FROM ml_tasks WHERE batch_id=? GROUP BY status", (batch_id,)
        ).fetchall()
    return {str(row["status"]): int(row["n"] or 0) for row in rows}


def update_batch_status(batch_id: str, status: str, *, error_msg: str | None = None) -> dict | None:
    ts = db.now_iso()
    fields = ["status=?", "updated_at=?"]
    args: list[Any] = [status, ts]
    if status == "running":
        fields.append("started_at=COALESCE(started_at,?)")
        args.append(ts)
        fields.append("error_msg=NULL")
        fields.append("finished_at=NULL")
    if status == "done":
        fields.append("error_msg=NULL")
    if status in {"done", "partial", "failed", "cancelled"}:
        fields.append("finished_at=?")
        args.append(ts)
    if error_msg is not None:
        fields.append("error_msg=?")
        args.append(str(error_msg)[:2_000])
    args.append(batch_id)
    where = "id=?"
    # Cancellation is an absorbing terminal state.  A worker may return from a
    # slow provider call after ``cancel_batch`` committed; that late completion
    # must never resurrect the batch.
    if status != "cancelled":
        where += " AND status!='cancelled'"
    with db._lock:
        conn = db._get_conn()
        conn.execute(f"UPDATE ml_batches SET {', '.join(fields)} WHERE {where}", tuple(args))
        conn.commit()
    return get_batch(batch_id, include_tasks=True)


def update_task(
    task_id: str,
    *,
    status: str | None = None,
    total_items: int | None = None,
    done_items: int | None = None,
    backtest_run_id: str | None = None,
    result_summary: Mapping[str, Any] | None = None,
    error_msg: str | None = None,
) -> dict | None:
    ts = db.now_iso()
    fields = ["updated_at=?"]
    args: list[Any] = [ts]
    if status is not None:
        fields.append("status=?")
        args.append(status)
        if status == "running":
            fields.append("started_at=COALESCE(started_at,?)")
            args.append(ts)
            fields.append("error_msg=NULL")
            fields.append("finished_at=NULL")
        if status == "done":
            fields.append("error_msg=NULL")
        if status in {"done", "partial", "failed", "cancelled"}:
            fields.append("finished_at=?")
            args.append(ts)
    if total_items is not None:
        fields.append("total_items=?")
        args.append(max(0, int(total_items)))
    if done_items is not None:
        fields.append("done_items=?")
        args.append(max(0, int(done_items)))
    if backtest_run_id is not None:
        fields.append("backtest_run_id=?")
        args.append(backtest_run_id)
    if result_summary is not None:
        fields.append("result_json=?")
        args.append(json.dumps(dict(result_summary), ensure_ascii=False))
    if error_msg is not None:
        fields.append("error_msg=?")
        args.append(str(error_msg)[:2_000])
    args.append(task_id)
    where = "id=?"
    if status is not None and status != "cancelled":
        where += " AND status!='cancelled'"
    with db._lock:
        conn = db._get_conn()
        conn.execute(f"UPDATE ml_tasks SET {', '.join(fields)} WHERE {where}", tuple(args))
        conn.commit()
    return get_task(task_id)


def create_qa_result(
    *,
    batch_id: str,
    task_id: str,
    profile_id: str,
    question_id: str,
    variant: str,
    repeat_no: int,
) -> dict:
    result_id, ts = db.new_id(), db.now_iso()
    with db._lock:
        conn = db._get_conn()
        conn.execute(
            """INSERT INTO ml_qa_results(
                id,batch_id,task_id,profile_id,question_id,variant,repeat_no,status,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (result_id, batch_id, task_id, profile_id, question_id, variant, repeat_no, "pending", ts, ts),
        )
        conn.commit()
    return get_qa_result(result_id) or {"id": result_id}


def get_or_create_qa_result(
    *,
    batch_id: str,
    task_id: str,
    profile_id: str,
    question_id: str,
    variant: str,
    repeat_no: int,
) -> dict:
    """Return the durable answer slot for an idempotently resumed QA task."""
    with db._lock:
        row = db._get_conn().execute(
            """SELECT * FROM ml_qa_results
               WHERE task_id=? AND question_id=? AND variant=? AND repeat_no=?""",
            (task_id, question_id, variant, int(repeat_no)),
        ).fetchone()
    if row:
        return _answer(row)
    try:
        return create_qa_result(
            batch_id=batch_id,
            task_id=task_id,
            profile_id=profile_id,
            question_id=question_id,
            variant=variant,
            repeat_no=repeat_no,
        )
    except sqlite3.IntegrityError:
        # A concurrent resume may have won the insert race.
        with db._lock:
            row = db._get_conn().execute(
                """SELECT * FROM ml_qa_results
                   WHERE task_id=? AND question_id=? AND variant=? AND repeat_no=?""",
                (task_id, question_id, variant, int(repeat_no)),
            ).fetchone()
        if not row:
            raise
        return _answer(row)


def get_qa_result(result_id: str) -> dict | None:
    with db._lock:
        row = db._get_conn().execute("SELECT * FROM ml_qa_results WHERE id=?", (result_id,)).fetchone()
    return _answer(row) if row else None


def list_qa_results(*, batch_id: str | None = None, task_id: str | None = None) -> list[dict]:
    if task_id:
        where, args = "task_id=?", (task_id,)
    elif batch_id:
        where, args = "batch_id=?", (batch_id,)
    else:
        raise ValueError("batch_id or task_id is required")
    with db._lock:
        rows = db._get_conn().execute(
            f"SELECT * FROM ml_qa_results WHERE {where} ORDER BY created_at,question_id,variant,repeat_no",
            args,
        ).fetchall()
    return [_answer(row) for row in rows]


def update_qa_result(
    result_id: str,
    *,
    status: str,
    answer: str | None = None,
    usage: Mapping[str, Any] | None = None,
    tool_trace: list[Mapping[str, Any]] | None = None,
    latency_ms: int | None = None,
    cost: float | None = None,
    auto_score: Mapping[str, Any] | None = None,
    final_score: Mapping[str, Any] | None = None,
    error_msg: str | None = None,
) -> dict | None:
    fields = ["status=?", "updated_at=?"]
    args: list[Any] = [status, db.now_iso()]
    if status == "running":
        fields.append("error_msg=NULL")
    optional_json = {
        "usage_json": usage,
        "tool_trace_json": tool_trace,
        "auto_score_json": auto_score,
        "final_score_json": final_score,
    }
    if answer is not None:
        fields.append("answer=?")
        args.append(answer)
    for column, value in optional_json.items():
        if value is not None:
            fields.append(f"{column}=?")
            args.append(json.dumps(value, ensure_ascii=False))
    if latency_ms is not None:
        fields.append("latency_ms=?")
        args.append(max(0, int(latency_ms)))
    if cost is not None:
        fields.append("cost=?")
        args.append(float(cost))
    if error_msg is not None:
        fields.append("error_msg=?")
        args.append(str(error_msg)[:2_000])
    args.append(result_id)
    where = "id=?" if status == "cancelled" else "id=? AND status!='cancelled'"
    with db._lock:
        conn = db._get_conn()
        conn.execute(f"UPDATE ml_qa_results SET {', '.join(fields)} WHERE {where}", tuple(args))
        conn.commit()
    return get_qa_result(result_id)


def set_manual_score(result_id: str, score: Mapping[str, Any]) -> dict | None:
    current = get_qa_result(result_id)
    if not current:
        return None
    value = dict(score)
    value["source"] = "manual"
    value["scored_at"] = db.now_iso()
    value["total"] = sum(
        int(value.get(key) or 0)
        for key in (
            "fact", "evidence", "method", "reasoning", "risk", "usability",
            "reproducibility", "user_value",
        )
    )
    # A human score is authoritative whenever supplied; the automatic score is
    # retained next to it for audit and disagreement analysis.
    with db._lock:
        conn = db._get_conn()
        conn.execute(
            """UPDATE ml_qa_results SET manual_score_json=?,final_score_json=?,status='done',updated_at=?
               WHERE id=? AND status!='cancelled'""",
            (json.dumps(value, ensure_ascii=False), json.dumps(value, ensure_ascii=False), db.now_iso(), result_id),
        )
        conn.commit()
    return get_qa_result(result_id)


def list_interrupted_batch_ids() -> list[str]:
    """Return batches that had durable work in flight when the process stopped.

    A plain ``pending`` batch created with ``auto_start=false`` is not resumed.
    Persisted auto-start intent repairs the create→start crash window, while
    the child-state predicates cover older/inconsistent in-flight records.
    """
    with db._lock:
        rows = db._get_conn().execute(
            """SELECT DISTINCT b.id
               FROM ml_batches b
               WHERE b.status IN ('running','interrupted')
                  OR (
                      b.status='pending'
                      AND (
                          COALESCE(
                              json_extract(
                                  CASE WHEN json_valid(b.scoring_config_json)
                                       THEN b.scoring_config_json ELSE '{}' END,
                                  '$.auto_start'
                              ), 0
                          )=1
                          OR
                          EXISTS(SELECT 1 FROM ml_tasks t WHERE t.batch_id=b.id AND t.status='running')
                          OR EXISTS(SELECT 1 FROM ml_qa_results r WHERE r.batch_id=b.id AND r.status='running')
                      )
                  )
               ORDER BY b.created_at"""
        ).fetchall()
    return [str(row["id"]) for row in rows]


def prepare_interrupted_batch(batch_id: str) -> bool:
    """Atomically make one orphaned worker batch safe to run again.

    Completed QA slots and linked backtest runs are deliberately untouched.
    Only in-flight rows are put back to ``pending``; their answer/score
    checkpoints stay in place and are reused by the resumed worker.
    """
    ts = db.now_iso()
    with db._lock:
        conn = db._get_conn()
        row = conn.execute(
            """SELECT b.status,
                      COALESCE(
                          json_extract(
                              CASE WHEN json_valid(b.scoring_config_json)
                                   THEN b.scoring_config_json ELSE '{}' END,
                              '$.auto_start'
                          ), 0
                      ) AS auto_start,
                      EXISTS(SELECT 1 FROM ml_tasks t WHERE t.batch_id=b.id AND t.status='running') AS task_running,
                      EXISTS(SELECT 1 FROM ml_qa_results r WHERE r.batch_id=b.id AND r.status='running') AS result_running
               FROM ml_batches b WHERE b.id=?""",
            (batch_id,),
        ).fetchone()
        if not row:
            return False
        recoverable = str(row["status"] or "") in {"running", "interrupted"} or (
            str(row["status"] or "") == "pending"
            and (bool(row["auto_start"]) or bool(row["task_running"]) or bool(row["result_running"]))
        )
        if not recoverable:
            return False
        cursor = conn.execute(
            """UPDATE ml_batches
               SET status='interrupted',updated_at=?,finished_at=NULL,
                   error_msg='服务进程中断，正在从持久化检查点恢复'
               WHERE id=? AND status!='cancelled'""",
            (ts, batch_id),
        )
        if cursor.rowcount == 0:
            conn.commit()
            return False
        conn.execute(
            """UPDATE ml_tasks SET status='pending',updated_at=?,finished_at=NULL
               WHERE batch_id=? AND status='running'""",
            (ts, batch_id),
        )
        conn.execute(
            """UPDATE ml_qa_results SET status='pending',updated_at=?
               WHERE batch_id=? AND status='running'""",
            (ts, batch_id),
        )
        conn.commit()
    return True


def prepare_cancelled_batch_for_restart(batch_id: str) -> bool:
    """Explicitly reopen a cancelled batch after its old worker has stopped.

    This is the sole intentional escape from the absorbing cancellation guards.
    The service calls it only after confirming there is no live batch thread.
    Persisted answer checkpoints and the linked backtest run are retained.
    """
    ts = db.now_iso()
    with db._lock:
        conn = db._get_conn()
        cursor = conn.execute(
            """UPDATE ml_batches
               SET status='interrupted',updated_at=?,finished_at=NULL,error_msg=NULL
               WHERE id=? AND status='cancelled'""",
            (ts, batch_id),
        )
        if cursor.rowcount == 0:
            conn.commit()
            return False
        conn.execute(
            """UPDATE ml_tasks SET status='pending',updated_at=?,finished_at=NULL,error_msg=NULL
               WHERE batch_id=? AND status='cancelled'""",
            (ts, batch_id),
        )
        conn.execute(
            """UPDATE ml_qa_results SET status='pending',updated_at=?,error_msg=NULL
               WHERE batch_id=? AND status='cancelled'""",
            (ts, batch_id),
        )
        conn.commit()
    return True


def find_backtest_run_id_for_task(task_id: str) -> str | None:
    """Recover the formal run created just before a process crash linked it.

    ``CreateBacktestRunRequest`` freezes ``model_lab_task_id`` inside the run's
    config in the same database.  Looking it up closes the only transaction gap
    that could otherwise create a duplicate formal run during recovery.
    """
    with db._lock:
        row = db._get_conn().execute(
            """SELECT id FROM bt_runs
               WHERE json_valid(config_json)
                 AND json_extract(config_json, '$.model_lab_task_id')=?
               ORDER BY created_at LIMIT 1""",
            (task_id,),
        ).fetchone()
    return str(row["id"]) if row else None


def cancel_open_tasks(batch_id: str) -> None:
    """Move only non-terminal task rows to cancelled."""
    ts = db.now_iso()
    with db._lock:
        conn = db._get_conn()
        conn.execute(
            """UPDATE ml_tasks SET status='cancelled',finished_at=?,updated_at=?
               WHERE batch_id=? AND status IN ('pending','running')""",
            (ts, ts, batch_id),
        )
        conn.execute(
            """UPDATE ml_qa_results SET status='cancelled',updated_at=?
               WHERE batch_id=? AND status IN ('pending','running')""",
            (ts, batch_id),
        )
        conn.commit()
