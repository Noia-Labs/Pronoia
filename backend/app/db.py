"""SQLite persistence layer (stdlib sqlite3, single conn + lock, thread-safe).

Schema (design.md §8):
  cases(id, title, created_at, updated_at)
  messages(id, case_id, role, agent, content, tool_trace, created_at)
  artifacts(id, case_id, message_id, kind, title, payload, pinned, created_at)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config

_lock = threading.RLock()
# Guards validate-then-persist relationships whose ids are stored inside JSON
# rather than protected by SQLite foreign keys (notably Arena -> Run).
HISTORY_RELATION_LOCK = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db() -> None:
    with _lock:
        conn = _get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                role TEXT NOT NULL,
                agent TEXT,
                content TEXT,
                tool_trace TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS artifacts(
                id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                message_id TEXT,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                payload TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS simulation_jobs(
                id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                graph_artifact_id TEXT NOT NULL,
                gateway_job_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                request_payload TEXT NOT NULL,
                error TEXT,
                artifact_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE,
                FOREIGN KEY(graph_artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
                FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_case ON messages(case_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_artifacts_case ON artifacts(case_id, pinned DESC, created_at);
            CREATE INDEX IF NOT EXISTS idx_simulation_jobs_case ON simulation_jobs(case_id, created_at);

            -- ==================== Pronoia Backtest tables (P0) ====================
            CREATE TABLE IF NOT EXISTS bt_saved_models(
                id TEXT PRIMARY KEY,
                identity_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                kind TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bt_model_tombstones(
                model_id TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bt_runs(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                runner TEXT NOT NULL,
                strategy_type TEXT DEFAULT 'event',
                strategy_spec_json TEXT,
                engine_mode TEXT DEFAULT 'event_proxy',
                result_nature TEXT DEFAULT 'proxy',
                dataset_id TEXT,
                dataset_name TEXT,
                dataset_version TEXT,
                protocol_hash TEXT,
                visibility TEXT DEFAULT 'private',
                oracle_status TEXT DEFAULT 'unavailable',
                execution_spec_json TEXT,
                prompt_variant TEXT,
                model_version TEXT,
                events_path TEXT NOT NULL,
                labels_path TEXT,
                out_path TEXT NOT NULL,
                result_path TEXT,
                ckpt_dir TEXT,
                concurrency INTEGER DEFAULT 2,
                total_events INTEGER DEFAULT 0,
                done_events INTEGER DEFAULT 0,
                acc_t3_strict REAL,
                acc_t3_strict_lo REAL,
                acc_t3_non_neutral REAL,
                config_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error_msg TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bt_runs_status ON bt_runs(status);
            CREATE INDEX IF NOT EXISTS idx_bt_runs_created ON bt_runs(created_at DESC);

            CREATE TABLE IF NOT EXISTS bt_predictions(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                symbol TEXT,
                market TEXT,
                event_type_l2 TEXT,
                pred_direction TEXT NOT NULL,
                confidence REAL,
                abstain INTEGER DEFAULT 0,
                rationale TEXT,
                oracle_label_t3 TEXT,
                oracle_car_t3 REAL,
                is_correct_t3 INTEGER,
                trajectory_ckpt TEXT,
                horizon TEXT,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                step_ms INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                strategy_metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES bt_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_bt_pred_run ON bt_predictions(run_id);
            CREATE INDEX IF NOT EXISTS idx_bt_pred_event ON bt_predictions(event_id);

            CREATE TABLE IF NOT EXISTS bt_metrics_snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                done_count INTEGER NOT NULL,
                acc_t3_strict REAL,
                acc_t3_strict_lo REAL,
                acc_t3_non_neutral REAL,
                neutral_ratio REAL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES bt_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_bt_snap_run ON bt_metrics_snapshots(run_id, done_count);

            CREATE TABLE IF NOT EXISTS bt_datasets(
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                name TEXT NOT NULL,
                total_events INTEGER,
                by_market_json TEXT,
                by_type_json TEXT,
                by_symbol_json TEXT,
                date_range_json TEXT,
                labels_path TEXT,
                dataset_kind TEXT DEFAULT 'event',
                current_version TEXT,
                snapshot_hash TEXT,
                status TEXT DEFAULT 'available',
                source_json TEXT,
                markets_json TEXT,
                asset_type TEXT,
                frequency TEXT,
                adjustment TEXT,
                calendar TEXT,
                symbols_json TEXT,
                schema_mapping_json TEXT,
                capabilities_json TEXT,
                coverage_json TEXT,
                quality_status TEXT DEFAULT 'unverified',
                quality_report_json TEXT,
                updated_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bt_dataset_versions(
                dataset_id TEXT NOT NULL,
                id TEXT NOT NULL,
                path TEXT,
                labels_path TEXT,
                snapshot_hash TEXT,
                status TEXT NOT NULL,
                source_json TEXT,
                markets_json TEXT,
                asset_type TEXT,
                frequency TEXT,
                adjustment TEXT,
                calendar TEXT,
                symbols_json TEXT,
                schema_mapping_json TEXT,
                capabilities_json TEXT,
                coverage_json TEXT,
                quality_status TEXT,
                quality_report_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(dataset_id, id),
                FOREIGN KEY(dataset_id) REFERENCES bt_datasets(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_bt_dataset_versions_dataset
                ON bt_dataset_versions(dataset_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS evolution_items(
                id TEXT PRIMARY KEY,
                level TEXT NOT NULL,
                category TEXT,
                title TEXT NOT NULL,
                description TEXT,
                trigger_run_ids TEXT,
                before_metrics_json TEXT,
                proposed_change_json TEXT NOT NULL,
                ab_test_run_id TEXT,
                after_metrics_json TEXT,
                status TEXT NOT NULL,
                applied_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_evo_level_status ON evolution_items(level, status);

            -- ==================== Pronoia Arena 横向比对 ====================
            -- arena = 同一数据集 × 多组 Run（不同 Agent/LLM/配置）的比对实验
            CREATE TABLE IF NOT EXISTS bt_arenas(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                arena_type TEXT DEFAULT 'prediction',
                protocol_hash TEXT,
                dataset_id TEXT,                   -- 对应 bt_datasets.id（可选）
                dataset_name TEXT,                 -- 显示用的数据集名（冗余，防止 dataset 被删后丢失名字）
                run_ids_json TEXT NOT NULL,        -- JSON 数组：参与比对的 run_id 列表
                description TEXT,                  -- 自由描述
                config_json TEXT,                  -- 额外配置：选定的 metric 列表、分组维度等
                status TEXT NOT NULL,              -- ready / computing / done / failed
                result_json TEXT,                  -- 完整比对结果（排名、雷达图数据、显著性检验等）
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bt_arenas_created ON bt_arenas(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_bt_arenas_status ON bt_arenas(status);

            -- ==================== Prospective evaluation ====================
            CREATE TABLE IF NOT EXISTS prospective_runs(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                capture_at TEXT NOT NULL,
                settle_after_seconds INTEGER NOT NULL,
                settle_after_days INTEGER NOT NULL DEFAULT 0,
                settle_at TEXT,
                target_settle_at TEXT,
                result_available_at TEXT,
                next_check_at TEXT,
                source_config_json TEXT,
                predictor_config_json TEXT,
                metric_config_json TEXT,
                discovery_diagnostics_json TEXT,
                evidence_cutoff_at TEXT,
                evidence_start_date TEXT,
                evidence_end_date TEXT,
                evidence_policy TEXT,
                actual_capture_at TEXT,
                total_items INTEGER DEFAULT 0,
                candidate_items INTEGER DEFAULT 0,
                selected_items INTEGER DEFAULT 0,
                frozen_items INTEGER DEFAULT 0,
                settled_items INTEGER DEFAULT 0,
                correct_items INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_prospective_runs_status ON prospective_runs(status, capture_at);

            CREATE TABLE IF NOT EXISTS prospective_items(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                candidate_id TEXT,
                question TEXT,
                assertion_text TEXT,
                market TEXT,
                symbol TEXT,
                event_type_l2 TEXT,
                event_time TEXT,
                source_url TEXT,
                captured_at TEXT,
                settle_at TEXT,
                status TEXT NOT NULL,
                prediction_id TEXT,
                settlement_id TEXT,
                actual_label TEXT,
                actual_value REAL,
                is_correct INTEGER,
                resolved_at TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, canonical_key),
                FOREIGN KEY(run_id) REFERENCES prospective_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_prospective_items_run ON prospective_items(run_id, status);

            CREATE TABLE IF NOT EXISTS prospective_candidates(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                market TEXT,
                symbol TEXT,
                company_name TEXT,
                event_count INTEGER NOT NULL DEFAULT 1,
                latest_event_at TEXT,
                event_type_l2 TEXT,
                title TEXT,
                source_url TEXT,
                evidence_json TEXT,
                selected INTEGER NOT NULL DEFAULT 0,
                selection_rank INTEGER,
                selection_score REAL,
                selection_reason TEXT,
                selector_model_version TEXT,
                selector_prompt_version TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, canonical_key),
                FOREIGN KEY(run_id) REFERENCES prospective_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_prospective_candidates_run
                ON prospective_candidates(run_id, selected DESC, selection_rank);

            CREATE TABLE IF NOT EXISTS prospective_predictions(
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                pred_direction TEXT NOT NULL,
                confidence REAL,
                rationale TEXT,
                raw_prediction_json TEXT,
                model_version TEXT,
                prompt_version TEXT,
                adapter_version TEXT,
                git_commit TEXT,
                as_of_at TEXT NOT NULL,
                evidence_hash TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(item_id, version),
                FOREIGN KEY(item_id) REFERENCES prospective_items(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_prospective_predictions_item ON prospective_predictions(item_id, version DESC);

            CREATE TABLE IF NOT EXISTS prospective_evidence(
                id TEXT PRIMARY KEY,
                prediction_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                source_kind TEXT,
                source_url TEXT,
                title TEXT,
                content TEXT,
                published_at TEXT,
                retrieved_at TEXT,
                content_hash TEXT,
                raw_payload_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(prediction_id, ordinal),
                FOREIGN KEY(prediction_id) REFERENCES prospective_predictions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS prospective_settlements(
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                settlement_mode TEXT NOT NULL,
                actual_label TEXT,
                actual_value REAL,
                result_source_json TEXT,
                reasoning TEXT,
                status TEXT NOT NULL,
                settled_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(item_id, attempt_no),
                FOREIGN KEY(item_id) REFERENCES prospective_items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS prospective_analysis_trace(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                item_id TEXT,
                candidate_id TEXT,
                sequence_no INTEGER NOT NULL,
                stage TEXT NOT NULL,
                stage_title TEXT NOT NULL,
                as_of_at TEXT NOT NULL,
                input_snapshot_json TEXT,
                output_snapshot_json TEXT,
                evidence_refs_json TEXT,
                model_version TEXT,
                prompt_version TEXT,
                trace_version TEXT NOT NULL DEFAULT 'v1',
                content_hash TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, item_id, sequence_no),
                FOREIGN KEY(run_id) REFERENCES prospective_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(item_id) REFERENCES prospective_items(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_prospective_trace_item ON prospective_analysis_trace(item_id, sequence_no);

            CREATE TABLE IF NOT EXISTS prospective_metric_snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                calculated_at TEXT NOT NULL,
                n_predicted INTEGER NOT NULL,
                n_settled INTEGER NOT NULL,
                n_correct INTEGER NOT NULL,
                accuracy REAL,
                coverage REAL,
                brier_score REAL,
                ece REAL,
                metrics_json TEXT,
                FOREIGN KEY(run_id) REFERENCES prospective_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS prospective_jobs(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                item_id TEXT,
                job_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 4,
                run_after TEXT NOT NULL,
                locked_by TEXT,
                locked_until TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES prospective_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_prospective_jobs_due ON prospective_jobs(status, run_after, locked_until);

            -- ==================== Pronoia Model Lab ====================
            -- Model credentials are never stored here. ``secret_env_ref`` is
            -- an opaque local credential or validated environment reference.
            CREATE TABLE IF NOT EXISTS ml_model_profiles(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                base_url TEXT NOT NULL,
                model_id TEXT NOT NULL,
                secret_env_ref TEXT NOT NULL,
                max_output_tokens INTEGER NOT NULL DEFAULT 65536,
                timeout_seconds REAL NOT NULL DEFAULT 600,
                thinking_mode TEXT NOT NULL DEFAULT 'auto',
                input_price_per_million REAL NOT NULL DEFAULT 0,
                output_price_per_million REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'CNY',
                is_active INTEGER NOT NULL DEFAULT 1,
                last_validated_at TEXT,
                last_validation_status TEXT,
                last_validation_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ml_profiles_name
                ON ml_model_profiles(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_ml_profiles_updated
                ON ml_model_profiles(updated_at DESC);

            CREATE TABLE IF NOT EXISTS ml_settings(
                key TEXT PRIMARY KEY,
                value_json TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ml_question_sets(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                version TEXT NOT NULL DEFAULT '1',
                is_builtin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ml_questions(
                id TEXT PRIMARY KEY,
                question_set_id TEXT NOT NULL,
                code TEXT NOT NULL,
                display_code TEXT,
                is_archived INTEGER NOT NULL DEFAULT 0,
                experiment TEXT,
                category TEXT,
                role TEXT,
                method TEXT,
                scenario TEXT,
                prompt TEXT NOT NULL,
                skills_json TEXT,
                deliverable TEXT,
                gold_standard TEXT,
                metrics TEXT,
                risk TEXT,
                priority TEXT,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(question_set_id) REFERENCES ml_question_sets(id) ON DELETE CASCADE,
                UNIQUE(question_set_id, code)
            );
            CREATE INDEX IF NOT EXISTS idx_ml_questions_set
                ON ml_questions(question_set_id, position, code);

            CREATE TABLE IF NOT EXISTS ml_batches(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                profile_ids_json TEXT NOT NULL,
                dataset_id TEXT,
                dataset_version TEXT,
                question_set_id TEXT,
                prediction_config_json TEXT,
                qa_config_json TEXT,
                scoring_config_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error_msg TEXT,
                FOREIGN KEY(question_set_id) REFERENCES ml_question_sets(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ml_batches_created
                ON ml_batches(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ml_batches_status
                ON ml_batches(status);

            CREATE TABLE IF NOT EXISTS ml_tasks(
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                status TEXT NOT NULL,
                total_items INTEGER NOT NULL DEFAULT 0,
                done_items INTEGER NOT NULL DEFAULT 0,
                backtest_run_id TEXT,
                config_json TEXT,
                result_json TEXT,
                error_msg TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY(batch_id) REFERENCES ml_batches(id) ON DELETE CASCADE,
                FOREIGN KEY(profile_id) REFERENCES ml_model_profiles(id) ON DELETE RESTRICT,
                FOREIGN KEY(backtest_run_id) REFERENCES bt_runs(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ml_tasks_batch
                ON ml_tasks(batch_id, kind, profile_id);
            CREATE INDEX IF NOT EXISTS idx_ml_tasks_status
                ON ml_tasks(status);

            CREATE TABLE IF NOT EXISTS ml_qa_results(
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                variant TEXT NOT NULL DEFAULT 'pronoia',
                repeat_no INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                answer TEXT,
                usage_json TEXT,
                tool_trace_json TEXT,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                cost REAL,
                auto_score_json TEXT,
                manual_score_json TEXT,
                final_score_json TEXT,
                error_msg TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES ml_batches(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES ml_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(profile_id) REFERENCES ml_model_profiles(id) ON DELETE RESTRICT,
                FOREIGN KEY(question_id) REFERENCES ml_questions(id) ON DELETE RESTRICT,
                UNIQUE(task_id, question_id, variant, repeat_no)
            );
            CREATE INDEX IF NOT EXISTS idx_ml_qa_results_task
                ON ml_qa_results(task_id, question_id, variant, repeat_no);

            CREATE TABLE IF NOT EXISTS ml_event_experiments(
                run_id TEXT PRIMARY KEY,
                qa_batch_id TEXT UNIQUE,
                auto_start INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES bt_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(qa_batch_id) REFERENCES ml_batches(id) ON DELETE SET NULL
            );
            """
        )
        conn.commit()

        # 幂等补充缺失列（bt_runs.metrics_json）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bt_runs)").fetchall()]
        if "metrics_json" not in cols:
            try:
                conn.execute("ALTER TABLE bt_runs ADD COLUMN metrics_json TEXT")
                conn.commit()
            except Exception:
                pass
        # Unified backtest metadata. ALTER TABLE keeps existing user databases intact.
        run_columns = {
            "strategy_type": "TEXT DEFAULT 'event'",
            "strategy_spec_json": "TEXT",
            "engine_mode": "TEXT DEFAULT 'event_proxy'",
            "result_nature": "TEXT DEFAULT 'proxy'",
            "dataset_id": "TEXT",
            "dataset_name": "TEXT",
            "dataset_version": "TEXT",
            "protocol_hash": "TEXT",
            "visibility": "TEXT DEFAULT 'private'",
            "oracle_status": "TEXT DEFAULT 'unavailable'",
            "execution_spec_json": "TEXT",
            "result_path": "TEXT",
        }
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bt_runs)").fetchall()}
        for column, ddl in run_columns.items():
            if column not in cols:
                conn.execute(f"ALTER TABLE bt_runs ADD COLUMN {column} {ddl}")
        # SQLite applies the new default to legacy rows; correct their Oracle
        # status from the already persisted labels_path.
        conn.execute(
            "UPDATE bt_runs SET oracle_status='available' "
            "WHERE labels_path IS NOT NULL AND TRIM(labels_path) <> '' "
            "AND (oracle_status IS NULL OR oracle_status='unavailable')"
        )

        prediction_columns = {
            "horizon": "TEXT",
            "tokens_in": "INTEGER DEFAULT 0",
            "tokens_out": "INTEGER DEFAULT 0",
            "step_ms": "INTEGER DEFAULT 0",
            "cost_usd": "REAL DEFAULT 0",
            "strategy_metadata_json": "TEXT",
        }
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bt_predictions)").fetchall()}
        for column, ddl in prediction_columns.items():
            if column not in cols:
                conn.execute(f"ALTER TABLE bt_predictions ADD COLUMN {column} {ddl}")

        arena_columns = {
            "arena_type": "TEXT DEFAULT 'prediction'",
            "protocol_hash": "TEXT",
        }
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bt_arenas)").fetchall()}
        for column, ddl in arena_columns.items():
            if column not in cols:
                conn.execute(f"ALTER TABLE bt_arenas ADD COLUMN {column} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_runs_protocol ON bt_runs(protocol_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_runs_dataset_version ON bt_runs(dataset_version)")

        dataset_columns = {
            "dataset_kind": "TEXT DEFAULT 'event'",
            "current_version": "TEXT",
            "snapshot_hash": "TEXT",
            "status": "TEXT DEFAULT 'available'",
            "source_json": "TEXT",
            "markets_json": "TEXT",
            "asset_type": "TEXT",
            "frequency": "TEXT",
            "adjustment": "TEXT",
            "calendar": "TEXT",
            "symbols_json": "TEXT",
            "schema_mapping_json": "TEXT",
            "capabilities_json": "TEXT",
            "coverage_json": "TEXT",
            "quality_status": "TEXT DEFAULT 'unverified'",
            "quality_report_json": "TEXT",
            "updated_at": "TEXT",
        }
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bt_datasets)").fetchall()}
        for column, ddl in dataset_columns.items():
            if column not in cols:
                conn.execute(f"ALTER TABLE bt_datasets ADD COLUMN {column} {ddl}")
        conn.execute(
            "UPDATE bt_datasets SET status='available' "
            "WHERE (status IS NULL OR status='') AND path IS NOT NULL AND TRIM(path) <> ''"
        )
        conn.execute(
            "UPDATE bt_datasets SET dataset_kind='event' WHERE dataset_kind IS NULL OR dataset_kind=''"
        )
        # Keep original question references and codes for historical evaluations.
        # A bank revision can retire questions or renumber their visible labels
        # without changing the UNIQUE(question_set_id, code) identity.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ml_questions)").fetchall()}
        for column, ddl in {
            "display_code": "TEXT",
            "is_archived": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in cols:
                conn.execute(f"ALTER TABLE ml_questions ADD COLUMN {column} {ddl}")
        conn.commit()
        # bt_metrics_snapshots.metrics_json（快照也存完整指标，向后兼容）
        cols_snap = [r[1] for r in conn.execute("PRAGMA table_info(bt_metrics_snapshots)").fetchall()]
        if "metrics_json" not in cols_snap:
            try:
                conn.execute("ALTER TABLE bt_metrics_snapshots ADD COLUMN metrics_json TEXT")
                conn.commit()
            except Exception:
                pass

        # Immutable prospective predictions: a new version is the only way to revise a prediction.
        cols_live = [r[1] for r in conn.execute("PRAGMA table_info(prospective_runs)").fetchall()]
        if "settle_after_days" not in cols_live:
            conn.execute("ALTER TABLE prospective_runs ADD COLUMN settle_after_days INTEGER NOT NULL DEFAULT 0")
        if "target_settle_at" not in cols_live:
            conn.execute("ALTER TABLE prospective_runs ADD COLUMN target_settle_at TEXT")
        if "candidate_items" not in cols_live:
            conn.execute("ALTER TABLE prospective_runs ADD COLUMN candidate_items INTEGER DEFAULT 0")
        if "selected_items" not in cols_live:
            conn.execute("ALTER TABLE prospective_runs ADD COLUMN selected_items INTEGER DEFAULT 0")
        if "discovery_diagnostics_json" not in cols_live:
            conn.execute("ALTER TABLE prospective_runs ADD COLUMN discovery_diagnostics_json TEXT")
        for column in ("evidence_cutoff_at", "evidence_start_date", "evidence_end_date", "evidence_policy", "actual_capture_at"):
            if column not in cols_live:
                conn.execute(f"ALTER TABLE prospective_runs ADD COLUMN {column} TEXT")
        if "next_check_at" not in cols_live:
            conn.execute("ALTER TABLE prospective_runs ADD COLUMN next_check_at TEXT")
        if "result_available_at" not in cols_live:
            conn.execute("ALTER TABLE prospective_runs ADD COLUMN result_available_at TEXT")
        cols_items = [r[1] for r in conn.execute("PRAGMA table_info(prospective_items)").fetchall()]
        if "candidate_id" not in cols_items:
            conn.execute("ALTER TABLE prospective_items ADD COLUMN candidate_id TEXT")
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS deny_prospective_prediction_update
               BEFORE UPDATE ON prospective_predictions
               BEGIN SELECT RAISE(ABORT, 'prospective predictions are immutable'); END;"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS deny_prospective_prediction_delete
               BEFORE DELETE ON prospective_predictions
               BEGIN SELECT RAISE(ABORT, 'prospective predictions are immutable'); END;"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS deny_prospective_evidence_update
               BEFORE UPDATE ON prospective_evidence
               BEGIN SELECT RAISE(ABORT, 'prospective evidence is immutable'); END;"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS deny_prospective_evidence_delete
               BEFORE DELETE ON prospective_evidence
               BEGIN SELECT RAISE(ABORT, 'prospective evidence is immutable'); END;"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS deny_prospective_trace_update
               BEFORE UPDATE ON prospective_analysis_trace
               BEGIN SELECT RAISE(ABORT, 'prospective analysis traces are immutable'); END;"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS deny_prospective_trace_delete
               BEFORE DELETE ON prospective_analysis_trace
               BEGIN SELECT RAISE(ABORT, 'prospective analysis traces are immutable'); END;"""
        )
        conn.commit()


# ---------------------------------------------------------------- cases ----

def create_case(title: str = "新研究") -> dict:
    cid, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO cases(id,title,created_at,updated_at) VALUES(?,?,?,?)",
            (cid, title, ts, ts),
        )
        conn.commit()
    return {"id": cid, "title": title, "created_at": ts, "updated_at": ts}


def list_cases() -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.case_id = c.id) AS message_count
            FROM cases c ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_case(case_id: str) -> Optional[dict]:
    with _lock:
        row = _get_conn().execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    return dict(row) if row else None


def update_case_title(case_id: str, title: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE cases SET title=?, updated_at=? WHERE id=?",
            (title, now_iso(), case_id),
        )
        conn.commit()


def touch_case(case_id: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("UPDATE cases SET updated_at=? WHERE id=?", (now_iso(), case_id))
        conn.commit()


def delete_case(case_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM simulation_jobs WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM messages WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM artifacts WHERE case_id=?", (case_id,))
        cur = conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
        conn.commit()
        return cur.rowcount > 0


# ------------------------------------------------------------- messages ----

def add_message(
    case_id: str,
    role: str,
    content: str = "",
    agent: Optional[str] = None,
    tool_trace: Optional[Any] = None,
    message_id: Optional[str] = None,
) -> dict:
    mid, ts = message_id or new_id(), now_iso()
    trace_json = json.dumps(tool_trace, ensure_ascii=False) if tool_trace else None
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO messages(id,case_id,role,agent,content,tool_trace,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (mid, case_id, role, agent, content, trace_json, ts),
        )
        conn.execute("UPDATE cases SET updated_at=? WHERE id=?", (ts, case_id))
        conn.commit()
    return {
        "id": mid, "case_id": case_id, "role": role, "agent": agent,
        "content": content, "tool_trace": tool_trace, "created_at": ts,
    }


def update_message(
    message_id: str,
    *,
    content: str,
    tool_trace: Optional[Any] = None,
    agent: Optional[str] = None,
) -> Optional[dict]:
    """Checkpoint an in-flight assistant message without changing its identity."""

    trace_json = json.dumps(tool_trace, ensure_ascii=False) if tool_trace else None
    ts = now_iso()
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT case_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE messages SET agent=?,content=?,tool_trace=? WHERE id=?",
            (agent, content, trace_json, message_id),
        )
        conn.execute(
            "UPDATE cases SET updated_at=? WHERE id=?", (ts, row["case_id"])
        )
        conn.commit()
    messages = [m for m in list_messages(row["case_id"]) if m["id"] == message_id]
    return messages[0] if messages else None


def list_messages(case_id: str, limit: Optional[int] = None) -> list[dict]:
    with _lock:
        if limit:
            rows = _get_conn().execute(
                "SELECT * FROM messages WHERE case_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (case_id, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = _get_conn().execute(
                "SELECT * FROM messages WHERE case_id=? ORDER BY created_at ASC, rowid ASC",
                (case_id,),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tool_trace"] = json.loads(d["tool_trace"]) if d.get("tool_trace") else None
        out.append(d)
    return out


def count_messages(case_id: str, role: Optional[str] = None) -> int:
    with _lock:
        if role:
            row = _get_conn().execute(
                "SELECT COUNT(*) c FROM messages WHERE case_id=? AND role=?", (case_id, role)
            ).fetchone()
        else:
            row = _get_conn().execute(
                "SELECT COUNT(*) c FROM messages WHERE case_id=?", (case_id,)
            ).fetchone()
    return int(row["c"])


# ------------------------------------------------------------ artifacts ----

def add_artifact(
    case_id: str,
    message_id: Optional[str],
    kind: str,
    title: str,
    payload: Any,
) -> dict:
    aid, ts = new_id(), now_iso()
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO artifacts(id,case_id,message_id,kind,title,payload,pinned,created_at)"
            " VALUES(?,?,?,?,?,?,0,?)",
            (aid, case_id, message_id, kind, title, payload_json, ts),
        )
        conn.execute("UPDATE cases SET updated_at=? WHERE id=?", (ts, case_id))
        conn.commit()
    return {
        "id": aid, "case_id": case_id, "message_id": message_id, "kind": kind,
        "title": title, "payload": payload, "pinned": 0, "created_at": ts,
    }


def list_artifacts(case_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM artifacts WHERE case_id=? ORDER BY pinned DESC, created_at ASC",
            (case_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def get_artifact(case_id: str, artifact_id: str) -> Optional[dict]:
    with _lock:
        row = _get_conn().execute(
            "SELECT * FROM artifacts WHERE case_id=? AND id=?", (case_id, artifact_id)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def toggle_pin(case_id: str, artifact_id: str) -> Optional[dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT pinned FROM artifacts WHERE case_id=? AND id=?", (case_id, artifact_id)
        ).fetchone()
        if not row:
            return None
        new_val = 0 if row["pinned"] else 1
        conn.execute(
            "UPDATE artifacts SET pinned=? WHERE case_id=? AND id=?",
            (new_val, case_id, artifact_id),
        )
        conn.commit()
    return get_artifact(case_id, artifact_id)


# ===================================================== bt_runs (Backtest Run) ====

_BT_RUN_COLUMNS = (
    "id", "name", "status", "runner", "strategy_type", "strategy_spec_json",
    "engine_mode", "result_nature", "dataset_id", "dataset_name", "dataset_version",
    "protocol_hash", "visibility", "oracle_status", "execution_spec_json",
    "prompt_variant", "model_version", "events_path", "labels_path", "out_path", "result_path",
    "ckpt_dir", "concurrency", "total_events", "done_events", "acc_t3_strict",
    "acc_t3_strict_lo", "acc_t3_non_neutral", "config_json", "created_at",
    "updated_at", "started_at", "finished_at", "error_msg",
)
_BT_RUN_FIELDS = ",".join(_BT_RUN_COLUMNS)


def _bt_run_evaluation_horizon(payload: dict[str, Any]) -> str:
    """Derive the canonical horizon for old rows without requiring a migration."""
    try:
        from .event_backtest.evaluation_protocol import resolve_run_horizon

        return resolve_run_horizon(payload)
    except (TypeError, ValueError):
        # Legacy rows may contain experimental/composite horizons. They remain
        # readable, while all newly-created runs are validated at the API edge.
        return "t3"


def _row_to_bt_run(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["config"] = json.loads(d["config_json"]) if d.get("config_json") else None
    d["strategy_spec"] = json.loads(d["strategy_spec_json"]) if d.get("strategy_spec_json") else None
    d["execution_spec"] = json.loads(d["execution_spec_json"]) if d.get("execution_spec_json") else None
    d["evaluation_horizon"] = _bt_run_evaluation_horizon(d)
    d["concurrency"] = int(d["concurrency"] or 2)
    d["total_events"] = int(d["total_events"] or 0)
    d["done_events"] = int(d["done_events"] or 0)
    return d


def create_bt_run(
    *,
    name: str,
    runner: str,
    events_path: str,
    out_path: str,
    run_id: str | None = None,
    labels_path: str | None = None,
    prompt_variant: str | None = None,
    model_version: str | None = None,
    ckpt_dir: str | None = None,
    concurrency: int = 2,
    total_events: int = 0,
    config: dict | None = None,
    strategy_type: str = "event",
    dataset_id: str | None = None,
    dataset_name: str | None = None,
    protocol_hash: str | None = None,
    visibility: str = "private",
    oracle_status: str | None = None,
    execution_spec: dict | None = None,
    dataset_version: str | None = None,
    strategy_spec: dict | None = None,
    engine_mode: str = "event_proxy",
    result_nature: str = "proxy",
    result_path: str | None = None,
    _commit: bool = True,
) -> dict:
    rid, ts = run_id or new_id(), now_iso()
    cfg_json = json.dumps(config or {}, ensure_ascii=False) if config else None
    with _lock:
        conn = _get_conn()
        conn.execute(
            f"INSERT INTO bt_runs({_BT_RUN_FIELDS}) VALUES({','.join('?' for _ in _BT_RUN_COLUMNS)})",
            (
                rid, name, "pending", runner, strategy_type,
                json.dumps(strategy_spec, ensure_ascii=False) if strategy_spec else None,
                engine_mode, result_nature, dataset_id, dataset_name, dataset_version,
                protocol_hash, visibility, oracle_status or ("available" if labels_path else "unavailable"),
                json.dumps(execution_spec, ensure_ascii=False) if execution_spec else None,
                prompt_variant, model_version, events_path, labels_path, out_path, result_path, ckpt_dir,
                concurrency, total_events, 0, None, None, None, cfg_json, ts, ts, None, None, None,
            ),
        )
        if _commit:
            conn.commit()
    row = _get_conn().execute("SELECT * FROM bt_runs WHERE id=?", (rid,)).fetchone()
    return _row_to_bt_run(row) or {"id": rid, "name": name, "status": "pending"}


def list_bt_runs(limit: int = 100, offset: int = 0) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM bt_runs ORDER BY created_at DESC LIMIT ? OFFSET ?", (int(limit), int(offset))
        ).fetchall()
    return [_row_to_bt_run(r) for r in rows if _row_to_bt_run(r)]


def get_bt_run(run_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM bt_runs WHERE id=?", (run_id,)).fetchone()
    return _row_to_bt_run(row)


def update_bt_run_status(run_id: str, status: str, *, error_msg: str | None = None) -> dict | None:
    ts = now_iso()
    fields = ["status = ?", "updated_at = ?"]
    args: list[Any] = [status, ts]
    if status == "running":
        fields.append("started_at = COALESCE(started_at, ?)")
        args.append(ts)
        # 重新运行/恢复运行：清掉上次残留的错误信息
        fields.append("error_msg = NULL")
    if status in {"done", "failed", "cancelled"}:
        fields.append("finished_at = ?")
        args.append(ts)
    if error_msg is not None:
        fields.append("error_msg = ?")
        args.append(error_msg)
    args.append(run_id)
    with _lock:
        conn = _get_conn()
        conn.execute(f"UPDATE bt_runs SET {', '.join(fields)} WHERE id=?", tuple(args))
        conn.commit()
    return get_bt_run(run_id)


def update_bt_run_progress(
    run_id: str,
    *,
    done_events: int,
    total_events: int | None = None,
    acc_t3_strict: float | None = None,
    acc_t3_strict_lo: float | None = None,
    acc_t3_non_neutral: float | None = None,
) -> dict | None:
    ts = now_iso()
    fields = ["done_events = ?", "updated_at = ?"]
    args: list[Any] = [int(done_events), ts]
    if total_events is not None:
        fields.append("total_events = ?"); args.append(max(0, int(total_events)))
    if acc_t3_strict is not None:
        fields.append("acc_t3_strict = ?"); args.append(float(acc_t3_strict))
    if acc_t3_strict_lo is not None:
        fields.append("acc_t3_strict_lo = ?"); args.append(float(acc_t3_strict_lo))
    if acc_t3_non_neutral is not None:
        fields.append("acc_t3_non_neutral = ?"); args.append(float(acc_t3_non_neutral))
    args.append(run_id)
    with _lock:
        conn = _get_conn()
        conn.execute(f"UPDATE bt_runs SET {', '.join(fields)} WHERE id=?", tuple(args))
        conn.commit()
    return get_bt_run(run_id)


def update_bt_run_protocol_hash(run_id: str, protocol_hash: str) -> dict | None:
    """Persist a lazily derived protocol hash for legacy runs."""
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE bt_runs SET protocol_hash=?, updated_at=? WHERE id=?",
            (protocol_hash, now_iso(), run_id),
        )
        conn.commit()
    return get_bt_run(run_id)


def delete_bt_run(run_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM bt_runs WHERE id=?", (run_id,))
        conn.commit()
        return cur.rowcount > 0


# ================================================= bt_predictions (per event) ====

def add_bt_prediction(
    *,
    run_id: str,
    event_id: str,
    pred_direction: str,
    symbol: str | None = None,
    market: str | None = None,
    event_type_l2: str | None = None,
    confidence: float | None = None,
    abstain: bool = False,
    rationale: str | None = None,
    oracle_label_t3: str | None = None,
    oracle_car_t3: float | None = None,
    is_correct_t3: bool | None = None,
    trajectory_ckpt: str | None = None,
    horizon: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    step_ms: int = 0,
    cost_usd: float = 0.0,
    strategy_metadata: dict[str, Any] | None = None,
    pred_id: str | None = None,
) -> dict:
    pid, ts = pred_id or new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO bt_predictions(id,run_id,event_id,symbol,market,event_type_l2,"
            "pred_direction,confidence,abstain,rationale,oracle_label_t3,oracle_car_t3,"
            "is_correct_t3,trajectory_ckpt,horizon,tokens_in,tokens_out,step_ms,cost_usd,"
            "strategy_metadata_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid, run_id, event_id, symbol, market, event_type_l2,
                pred_direction, confidence, 1 if abstain else 0,
                rationale, oracle_label_t3, oracle_car_t3,
                1 if is_correct_t3 else (0 if is_correct_t3 is False else None),
                trajectory_ckpt, horizon, max(0, int(tokens_in or 0)), max(0, int(tokens_out or 0)),
                max(0, int(step_ms or 0)), max(0.0, float(cost_usd or 0.0)),
                json.dumps(strategy_metadata, ensure_ascii=False, separators=(",", ":"))
                if isinstance(strategy_metadata, dict) else None,
                ts,
            ),
        )
        conn.commit()
    return {
        "id": pid, "run_id": run_id, "event_id": event_id,
        "pred_direction": pred_direction, "confidence": confidence,
        "abstain": abstain, "created_at": ts,
    }


def aggregate_bt_prediction_cost(run_id: str) -> dict[str, int | float]:
    """Aggregate persisted run cost; schema problems are allowed to surface to tests/logs."""
    with _lock:
        row = _get_conn().execute(
            """
            SELECT COALESCE(SUM(tokens_in), 0) AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COALESCE(SUM(step_ms), 0) AS step_ms_total,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd
            FROM bt_predictions WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
    return {
        "tokens_in": int(row["tokens_in"] or 0) if row else 0,
        "tokens_out": int(row["tokens_out"] or 0) if row else 0,
        "step_ms_total": int(row["step_ms_total"] or 0) if row else 0,
        "cost_usd": float(row["cost_usd"] or 0.0) if row else 0.0,
    }


def list_bt_predictions(
    run_id: str,
    *,
    offset: int = 0,
    limit: int = 50,
    market: str | None = None,
    event_type_l2: str | None = None,
    only_incorrect: bool = False,
) -> tuple[int, list[dict]]:
    where = ["run_id = ?"]
    args: list[Any] = [run_id]
    if market:
        where.append("market = ?"); args.append(market)
    if event_type_l2:
        where.append("event_type_l2 = ?"); args.append(event_type_l2)
    if only_incorrect:
        where.append("is_correct_t3 = 0")
    where_sql = " AND ".join(where)
    with _lock:
        conn = _get_conn()
        total_row = conn.execute(
            f"SELECT COUNT(*) c FROM bt_predictions WHERE {where_sql}", tuple(args)
        ).fetchone()
        total = int(total_row["c"] if total_row else 0)
        rows = conn.execute(
            f"SELECT * FROM bt_predictions WHERE {where_sql} ORDER BY created_at ASC LIMIT ? OFFSET ?",
            tuple(args + [int(limit), int(offset)]),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["abstain"] = bool(d.get("abstain"))
        d["is_correct_t3"] = bool(d["is_correct_t3"]) if d.get("is_correct_t3") is not None else None
        trajectory_path = str(d.get("trajectory_ckpt") or "").strip()
        try:
            trajectory_available = bool(trajectory_path and Path(trajectory_path).is_file())
        except OSError:
            trajectory_available = False
        d["trajectory_available"] = trajectory_available
        if not trajectory_available:
            d["trajectory_ckpt"] = None
        try:
            d["strategy_metadata"] = (
                json.loads(d["strategy_metadata_json"])
                if d.get("strategy_metadata_json") else None
            )
        except (json.JSONDecodeError, TypeError):
            d["strategy_metadata"] = None
        d.pop("strategy_metadata_json", None)
        d["expected_return_pct"] = (d.get("strategy_metadata") or {}).get("expected_return_pct")
        out.append(d)
    return total, out


def get_bt_prediction(run_id: str, event_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute(
            "SELECT * FROM bt_predictions WHERE run_id=? AND event_id=?", (run_id, event_id)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["abstain"] = bool(d.get("abstain"))
    d["is_correct_t3"] = bool(d["is_correct_t3"]) if d.get("is_correct_t3") is not None else None
    trajectory_path = str(d.get("trajectory_ckpt") or "").strip()
    try:
        trajectory_available = bool(trajectory_path and Path(trajectory_path).is_file())
    except OSError:
        trajectory_available = False
    d["trajectory_available"] = trajectory_available
    if not trajectory_available:
        d["trajectory_ckpt"] = None
    try:
        d["strategy_metadata"] = (
            json.loads(d["strategy_metadata_json"])
            if d.get("strategy_metadata_json") else None
        )
    except (json.JSONDecodeError, TypeError):
        d["strategy_metadata"] = None
    d.pop("strategy_metadata_json", None)
    d["expected_return_pct"] = (d.get("strategy_metadata") or {}).get("expected_return_pct")
    return d


def update_bt_prediction_oracle(
    run_id: str,
    event_id: str,
    *,
    oracle_label_t3: str | None,
    oracle_car_t3: float | None,
    is_correct_t3: bool | None,
) -> bool:
    """Update existing predictions in place instead of duplicating them at run finalization."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE bt_predictions SET oracle_label_t3=?,oracle_car_t3=?,is_correct_t3=? "
            "WHERE run_id=? AND event_id=?",
            (
                oracle_label_t3,
                oracle_car_t3,
                1 if is_correct_t3 else (0 if is_correct_t3 is False else None),
                run_id,
                event_id,
            ),
        )
        conn.commit()
    return cur.rowcount > 0


# ============================================ bt_metrics_snapshots (time series) ====

def add_bt_metrics_snapshot(
    *,
    run_id: str,
    done_count: int,
    acc_t3_strict: float | None = None,
    acc_t3_strict_lo: float | None = None,
    acc_t3_non_neutral: float | None = None,
    neutral_ratio: float | None = None,
) -> int:
    ts = now_iso()
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO bt_metrics_snapshots(run_id,done_count,acc_t3_strict,"
            "acc_t3_strict_lo,acc_t3_non_neutral,neutral_ratio,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (run_id, int(done_count), acc_t3_strict, acc_t3_strict_lo,
             acc_t3_non_neutral, neutral_ratio, ts),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def list_bt_metrics_snapshots(run_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM bt_metrics_snapshots WHERE run_id=? ORDER BY done_count ASC",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================== bt_datasets ====

def upsert_bt_dataset(
    *,
    dataset_id: str,
    path: str,
    name: str,
    total_events: int = 0,
    by_market: dict | None = None,
    by_type: dict | None = None,
    by_symbol: dict | None = None,
    date_range: dict | None = None,
    labels_path: str | None = None,
    dataset_kind: str = "event",
    dataset_version: str | None = None,
    snapshot_hash: str | None = None,
    status: str = "available",
    source: dict | None = None,
    markets: list[str] | None = None,
    asset_type: str | None = None,
    frequency: str | None = None,
    adjustment: str | None = None,
    calendar: str | None = None,
    symbols: list[str] | None = None,
    schema_mapping: dict | None = None,
    capabilities: dict | None = None,
    coverage: dict | None = None,
    quality_status: str = "unverified",
    quality_report: dict | None = None,
) -> dict:
    ts = now_iso()
    def _j(d): return json.dumps(d, ensure_ascii=False) if d is not None else None
    with _lock:
        conn = _get_conn()
        existing = conn.execute("SELECT id FROM bt_datasets WHERE id=?", (dataset_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE bt_datasets SET path=?,name=?,total_events=?,by_market_json=?,"
                "by_type_json=?,by_symbol_json=?,date_range_json=?,labels_path=?,dataset_kind=?,"
                "current_version=?,snapshot_hash=?,status=?,source_json=?,markets_json=?,asset_type=?,"
                "frequency=?,adjustment=?,calendar=?,symbols_json=?,schema_mapping_json=?,"
                "capabilities_json=?,coverage_json=?,quality_status=?,quality_report_json=?,updated_at=? "
                "WHERE id=?",
                (path, name, int(total_events), _j(by_market), _j(by_type), _j(by_symbol),
                 _j(date_range), labels_path, dataset_kind, dataset_version, snapshot_hash, status,
                 _j(source), _j(markets), asset_type, frequency, adjustment, calendar, _j(symbols),
                 _j(schema_mapping), _j(capabilities), _j(coverage), quality_status,
                 _j(quality_report), ts, dataset_id),
            )
        else:
            conn.execute(
                "INSERT INTO bt_datasets(id,path,name,total_events,by_market_json,"
                "by_type_json,by_symbol_json,date_range_json,labels_path,dataset_kind,current_version,"
                "snapshot_hash,status,source_json,markets_json,asset_type,frequency,adjustment,calendar,"
                "symbols_json,schema_mapping_json,capabilities_json,coverage_json,quality_status,"
                "quality_report_json,updated_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dataset_id, path, name, int(total_events), _j(by_market), _j(by_type),
                 _j(by_symbol), _j(date_range), labels_path, dataset_kind, dataset_version,
                 snapshot_hash, status, _j(source), _j(markets), asset_type, frequency, adjustment,
                 calendar, _j(symbols), _j(schema_mapping), _j(capabilities), _j(coverage),
                 quality_status, _j(quality_report), ts, ts),
            )
        if dataset_version:
            conn.execute(
                "INSERT OR IGNORE INTO bt_dataset_versions("
                "id,dataset_id,path,labels_path,snapshot_hash,status,source_json,markets_json,"
                "asset_type,frequency,adjustment,calendar,symbols_json,schema_mapping_json,"
                "capabilities_json,coverage_json,quality_status,quality_report_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dataset_version, dataset_id, path or None, labels_path, snapshot_hash, status,
                    _j(source), _j(markets), asset_type, frequency, adjustment, calendar, _j(symbols),
                    _j(schema_mapping), _j(capabilities), _j(coverage), quality_status,
                    _j(quality_report), ts,
                ),
            )
        conn.commit()
    return get_bt_dataset(dataset_id) or {"id": dataset_id, "path": path, "name": name}


_BT_DATASET_JSON_COLUMNS = {
    "by_market_json": "by_market",
    "by_type_json": "by_type",
    "by_symbol_json": "by_symbol",
    "date_range_json": "date_range",
    "source_json": "source",
    "markets_json": "markets",
    "symbols_json": "symbols",
    "schema_mapping_json": "schema_mapping",
    "capabilities_json": "capabilities",
    "coverage_json": "coverage",
    "quality_report_json": "quality_report",
}


def _row_to_bt_dataset(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    value = dict(row)
    for source_key, target_key in _BT_DATASET_JSON_COLUMNS.items():
        raw = value.pop(source_key, None)
        value[target_key] = json.loads(raw) if raw else None
    value["dataset_version"] = value.get("current_version")
    return value


def list_bt_datasets() -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM bt_datasets ORDER BY created_at DESC"
        ).fetchall()
    return [item for row in rows if (item := _row_to_bt_dataset(row)) is not None]


def get_bt_dataset(dataset_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM bt_datasets WHERE id=?", (dataset_id,)).fetchone()
    return _row_to_bt_dataset(row)


def list_bt_dataset_versions(dataset_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM bt_dataset_versions WHERE dataset_id=? ORDER BY created_at DESC, id DESC",
            (dataset_id,),
        ).fetchall()
    output: list[dict] = []
    json_columns = {
        "source_json": "source", "markets_json": "markets", "symbols_json": "symbols",
        "schema_mapping_json": "schema_mapping", "capabilities_json": "capabilities",
        "coverage_json": "coverage", "quality_report_json": "quality_report",
    }
    for row in rows:
        item = dict(row)
        for source_key, target_key in json_columns.items():
            raw = item.pop(source_key, None)
            item[target_key] = json.loads(raw) if raw else None
        item["dataset_version"] = item.get("id")
        output.append(item)
    return output


def get_bt_dataset_version(dataset_id: str, dataset_version: str) -> dict | None:
    with _lock:
        row = _get_conn().execute(
            "SELECT * FROM bt_dataset_versions WHERE dataset_id=? AND id=?",
            (dataset_id, dataset_version),
        ).fetchone()
    if not row:
        return None
    return next(iter([
        item for item in list_bt_dataset_versions(dataset_id) if item.get("id") == dataset_version
    ]), None)


def update_bt_dataset_labels_path(dataset_id: str, labels_path: str | None) -> dict | None:
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE bt_datasets SET labels_path=? WHERE id=?",
            (labels_path, dataset_id),
        )
        conn.execute(
            "UPDATE bt_dataset_versions SET labels_path=? "
            "WHERE dataset_id=? AND id=(SELECT current_version FROM bt_datasets WHERE id=?)",
            (labels_path, dataset_id, dataset_id),
        )
        conn.commit()
    return get_bt_dataset(dataset_id) if cur.rowcount else None


def prune_missing_bt_datasets() -> int:
    """删除 path 在磁盘上不存在的数据集记录（例如项目目录迁移后残留的旧绝对路径），返回删除条数。"""
    from pathlib import Path

    with _lock:
        rows = _get_conn().execute("SELECT id, path, status FROM bt_datasets").fetchall()
    # Pending API/database contracts intentionally have no local file yet and
    # must survive restarts. Only an entry claiming to be available is stale.
    stale = [
        r["id"] for r in rows
        if str(r["status"] or "available") == "available"
        and (not r["path"] or not Path(r["path"]).is_file())
    ]
    if not stale:
        return 0
    with _lock:
        conn = _get_conn()
        for did in stale:
            conn.execute("DELETE FROM bt_datasets WHERE id=?", (did,))
        conn.commit()
    return len(stale)


# ========================================================== evolution_items ====

def create_evolution_item(
    *,
    level: str,
    title: str,
    proposed_change: dict,
    category: str | None = None,
    description: str | None = None,
    status: str = "proposed",
) -> dict:
    eid, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO evolution_items(id,level,category,title,description,"
            "trigger_run_ids,before_metrics_json,proposed_change_json,ab_test_run_id,"
            "after_metrics_json,status,applied_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid, level, category, title, description, None, None,
                json.dumps(proposed_change, ensure_ascii=False), None, None,
                status, None, ts,
            ),
        )
        conn.commit()
    return get_evolution_item(eid) or {"id": eid, "level": level, "title": title, "status": status}


def list_evolution_items(*, level: str | None = None, status: str | None = None) -> list[dict]:
    where = []
    args: list[Any] = []
    if level:
        where.append("level = ?"); args.append(level)
    if status:
        where.append("status = ?"); args.append(status)
    sql = "SELECT * FROM evolution_items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    with _lock:
        rows = _get_conn().execute(sql, tuple(args)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("before_metrics_json", "after_metrics_json"):
            short = k[:-5]
            d[short] = json.loads(d[k]) if d.get(k) else None
            d.pop(k, None)
        d["proposed_change"] = json.loads(d["proposed_change_json"]) if d.get("proposed_change_json") else None
        d.pop("proposed_change_json", None)
        out.append(d)
    return out


def get_evolution_item(item_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM evolution_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("before_metrics_json", "after_metrics_json"):
        short = k[:-5]
        d[short] = json.loads(d[k]) if d.get(k) else None
        d.pop(k, None)
    d["proposed_change"] = json.loads(d["proposed_change_json"]) if d.get("proposed_change_json") else None
    d.pop("proposed_change_json", None)
    return d


def update_evolution_status(item_id: str, status: str, **kwargs) -> dict | None:
    fields = ["status = ?"]
    args: list[Any] = [status]
    if status == "applied":
        fields.append("applied_at = ?")
        args.append(now_iso())
    allowed = {"ab_test_run_id"}
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        fields.append(f"{k} = ?")
        args.append(v)
    args.append(item_id)
    with _lock:
        conn = _get_conn()
        conn.execute(f"UPDATE evolution_items SET {', '.join(fields)} WHERE id=?", tuple(args))
        conn.commit()
    return get_evolution_item(item_id)


# ============================================================== bt_runs.metrics_json 存取 ====

def _bt_prediction_quality(
    run_id: str,
    *,
    run_status: str,
    engine_mode: str,
    runner: str,
) -> dict[str, Any]:
    """Derive Run-level completion quality from persisted per-event audits.

    Legacy abstentions predate ``strategy_metadata_json``.  They are counted as
    generic invalid outputs so old Runs can still surface a useful warning
    instead of looking silently healthy.
    """
    with _lock:
        rows = _get_conn().execute(
            "SELECT abstain,strategy_metadata_json FROM bt_predictions WHERE run_id=?",
            (run_id,),
        ).fetchall()
    invalid_count = 0
    warning_count = 0
    voluntary_abstain_count = 0
    insufficient_data_count = 0
    for row in rows:
        metadata: dict[str, Any] = {}
        raw = row["strategy_metadata_json"] if "strategy_metadata_json" in row.keys() else None
        if raw:
            try:
                parsed = json.loads(raw)
                metadata = parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        validation = metadata.get("validation")
        errors = metadata.get("output_validation_errors")
        explicitly_invalid = bool(
            metadata.get("prediction_status") == "invalid_output"
            or metadata.get("output_failure_kind")
            or metadata.get("completion_quality") == "invalid"
            or (isinstance(validation, dict) and validation.get("valid") is False)
            or (isinstance(errors, list) and len(errors) > 0)
        )
        legacy_model_abstain = bool(
            row["abstain"]
            and not metadata
            and runner in {"team_prompt", "team_full"}
        )
        invalid = explicitly_invalid or legacy_model_abstain
        if invalid:
            invalid_count += 1
            warning_count += 1
        elif metadata.get("prediction_status") == "insufficient_data":
            insufficient_data_count += 1
            warning_count += 1
        elif bool(row["abstain"]):
            # External/imported strategies may deliberately decline a trade;
            # that is coverage information, not a transport/validation error.
            voluntary_abstain_count += 1

    if warning_count:
        quality = "completed_with_warnings"
    elif rows:
        quality = "valid"
    elif engine_mode != "event_proxy":
        quality = "not_applicable"
    elif run_status in {"pending", "running", "paused"}:
        quality = "pending"
    else:
        quality = "unavailable"
    return {
        "completion_quality": quality,
        "warning_count": warning_count,
        "invalid_output_count": invalid_count,
        "voluntary_abstain_count": voluntary_abstain_count,
        "insufficient_data_count": insufficient_data_count,
    }

def update_bt_run_metrics(run_id: str, metrics_dict: dict | None) -> dict | None:
    """将完整的 metrics_registry 结果 JSON 存入 bt_runs.metrics_json。"""
    ts = now_iso()
    m_json = json.dumps(metrics_dict, ensure_ascii=False) if metrics_dict is not None else None
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE bt_runs SET metrics_json = ?, updated_at = ? WHERE id = ?",
            (m_json, ts, run_id),
        )
        conn.commit()
    return get_bt_run(run_id)


def _row_to_bt_run(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["config"] = json.loads(d["config_json"]) if d.get("config_json") else None
    d["metrics"] = json.loads(d["metrics_json"]) if d.get("metrics_json") else None
    d["execution_spec"] = json.loads(d["execution_spec_json"]) if d.get("execution_spec_json") else None
    d["strategy_spec"] = json.loads(d["strategy_spec_json"]) if d.get("strategy_spec_json") else None
    d["evaluation_horizon"] = _bt_run_evaluation_horizon(d)
    d["concurrency"] = int(d["concurrency"] or 2)
    d["total_events"] = int(d["total_events"] or 0)
    d["done_events"] = int(d["done_events"] or 0)
    d.update(_bt_prediction_quality(
        str(d.get("id") or ""),
        run_status=str(d.get("status") or ""),
        engine_mode=str(d.get("engine_mode") or "event_proxy"),
        runner=str(d.get("runner") or ""),
    ))
    return d


# ============================================================== bt_metrics_snapshots.metrics_json 补存取 ====

def add_bt_metrics_snapshot(
    *,
    run_id: str,
    done_count: int,
    acc_t3_strict: float | None = None,
    acc_t3_strict_lo: float | None = None,
    acc_t3_non_neutral: float | None = None,
    neutral_ratio: float | None = None,
    metrics_dict: dict | None = None,
) -> int:
    ts = now_iso()
    m_json = json.dumps(metrics_dict, ensure_ascii=False) if metrics_dict is not None else None
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO bt_metrics_snapshots(run_id,done_count,acc_t3_strict,"
            "acc_t3_strict_lo,acc_t3_non_neutral,neutral_ratio,created_at,metrics_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (run_id, int(done_count), acc_t3_strict, acc_t3_strict_lo,
             acc_t3_non_neutral, neutral_ratio, ts, m_json),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def list_bt_metrics_snapshots(run_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM bt_metrics_snapshots WHERE run_id=? ORDER BY done_count ASC",
            (run_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("metrics_json"):
            d["metrics"] = json.loads(d["metrics_json"])
        else:
            d["metrics"] = None
        out.append(d)
    return out


# ==================================================================== bt_arenas (Arena CRUD) ====

def _row_to_bt_arena(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["run_ids"] = json.loads(d["run_ids_json"]) if d.get("run_ids_json") else []
    d.pop("run_ids_json", None)
    d["config"] = json.loads(d["config_json"]) if d.get("config_json") else None
    d.pop("config_json", None)
    selected = d["config"].get("selected_metric_ids") if isinstance(d.get("config"), dict) else None
    d["selected_metric_ids"] = list(selected) if isinstance(selected, list) else []
    d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
    d.pop("result_json", None)
    return d


def create_bt_arena(
    *,
    name: str,
    run_ids: list[str],
    dataset_id: str | None = None,
    dataset_name: str | None = None,
    description: str | None = None,
    config: dict | None = None,
    arena_type: str = "prediction",
    protocol_hash: str | None = None,
    arena_id: str | None = None,
) -> dict:
    aid, ts = arena_id or new_id(), now_iso()
    cfg_json = json.dumps(config or {}, ensure_ascii=False) if config else None
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO bt_arenas(id,name,arena_type,protocol_hash,dataset_id,dataset_name,run_ids_json,"
            "description,config_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, name, arena_type, protocol_hash, dataset_id, dataset_name,
             json.dumps(run_ids or [], ensure_ascii=False), description, cfg_json, "ready", ts, ts),
        )
        conn.commit()
    row = _get_conn().execute("SELECT * FROM bt_arenas WHERE id=?", (aid,)).fetchone()
    return _row_to_bt_arena(row) or {"id": aid, "name": name, "run_ids": run_ids or [], "status": "ready"}


def list_bt_arenas(limit: int = 100, offset: int = 0) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM bt_arenas ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ).fetchall()
    return [_row_to_bt_arena(r) for r in rows if _row_to_bt_arena(r)]


def get_bt_arena(arena_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM bt_arenas WHERE id=?", (arena_id,)).fetchone()
    return _row_to_bt_arena(row)


def update_bt_arena_status(
    arena_id: str,
    status: str,
    *,
    result: dict | None = None,
) -> dict | None:
    ts = now_iso()
    fields = ["status = ?", "updated_at = ?"]
    args: list[Any] = [status, ts]
    if status in {"done", "failed", "partial", "cancelled"}:
        fields.append("finished_at = ?")
        args.append(ts)
    if result is not None:
        fields.append("result_json = ?")
        args.append(json.dumps(result, ensure_ascii=False))
    args.append(arena_id)
    with _lock:
        conn = _get_conn()
        conn.execute(f"UPDATE bt_arenas SET {', '.join(fields)} WHERE id=?", tuple(args))
        conn.commit()
    return get_bt_arena(arena_id)


def update_bt_arena_config(arena_id: str, config: dict | None) -> dict | None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE bt_arenas SET config_json=?, updated_at=? WHERE id=?",
            (json.dumps(config or {}, ensure_ascii=False), now_iso(), arena_id),
        )
        conn.commit()
    return get_bt_arena(arena_id)


def delete_bt_arena(arena_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM bt_arenas WHERE id=?", (arena_id,))
        conn.commit()
        return cur.rowcount > 0


# ================================================= prospective evaluation ====

def _json(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False, default=str) if value is not None else None


def _prospective_run(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    for key, short in (("source_config_json", "source_config"), ("predictor_config_json", "predictor_config"),
                       ("metric_config_json", "metric_config"), ("discovery_diagnostics_json", "discovery_diagnostics")):
        d[short] = json.loads(d[key]) if d.get(key) else {}
        d.pop(key, None)
    return d


def create_prospective_run(*, name: str, capture_at: str, settle_after_days: int = 0,
                           settle_after_seconds: int | None = None,
                           source_config: dict | None = None, predictor_config: dict | None = None,
                           metric_config: dict | None = None, run_id: str | None = None) -> dict:
    rid, ts = run_id or new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO prospective_runs(id,name,status,capture_at,settle_after_seconds,settle_after_days,source_config_json,"
            "predictor_config_json,metric_config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (rid, name, "scheduled", capture_at, max(0, int(settle_after_seconds if settle_after_seconds is not None else settle_after_days * 86400)),
             max(0, int(settle_after_days)), _json(source_config or {}), _json(predictor_config or {}),
             _json(metric_config or {}), ts, ts),
        )
        conn.commit()
    return get_prospective_run(rid) or {"id": rid, "status": "scheduled"}


def get_prospective_run(run_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM prospective_runs WHERE id=?", (run_id,)).fetchone()
    return _prospective_run(row)


def list_prospective_runs(limit: int = 100) -> list[dict]:
    with _lock:
        rows = _get_conn().execute("SELECT * FROM prospective_runs ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    return [_prospective_run(r) for r in rows if _prospective_run(r)]


def update_prospective_run(run_id: str, *, status: str | None = None, settle_at: str | None = None,
                           target_settle_at: str | None = None,
                           result_available_at: str | None = None,
                           next_check_at: str | None = None,
                           evidence_cutoff_at: str | None = None,
                           evidence_start_date: str | None = None,
                           evidence_end_date: str | None = None,
                           evidence_policy: str | None = None,
                           actual_capture_at: str | None = None,
                           total_items: int | None = None, frozen_items: int | None = None,
                           candidate_items: int | None = None, selected_items: int | None = None,
                           settled_items: int | None = None, correct_items: int | None = None,
                           error_message: str | None = None,
                           discovery_diagnostics: list[dict] | None = None) -> dict | None:
    fields, args = ["updated_at = ?"], [now_iso()]
    for col, val in (("status", status), ("settle_at", settle_at), ("target_settle_at", target_settle_at),
                     ("next_check_at", next_check_at),
                     ("result_available_at", result_available_at),
                     ("evidence_cutoff_at", evidence_cutoff_at), ("evidence_start_date", evidence_start_date),
                     ("evidence_end_date", evidence_end_date), ("evidence_policy", evidence_policy),
                     ("actual_capture_at", actual_capture_at), ("total_items", total_items),
                     ("candidate_items", candidate_items), ("selected_items", selected_items),
                     ("frozen_items", frozen_items), ("settled_items", settled_items),
                     ("correct_items", correct_items), ("error_message", error_message)):
        if val is not None:
            fields.append(f"{col} = ?"); args.append(val)
    if status == "capturing":
        fields.append("started_at = COALESCE(started_at, ?)"); args.append(now_iso())
    if status in {"capturing", "waiting", "settling"}:
        fields.append("finished_at = NULL")
    if status in {"completed", "partial", "failed", "cancelled"}:
        fields.append("finished_at = COALESCE(finished_at, ?)"); args.append(now_iso())
    if discovery_diagnostics is not None:
        fields.append("discovery_diagnostics_json = ?"); args.append(_json(discovery_diagnostics))
    args.append(run_id)
    with _lock:
        _get_conn().execute(f"UPDATE prospective_runs SET {', '.join(fields)} WHERE id=?", tuple(args))
        _get_conn().commit()
    return get_prospective_run(run_id)


def create_prospective_item(*, run_id: str, event_id: str, canonical_key: str, event: dict,
                            question: str = "", assertion_text: str = "", captured_at: str | None = None,
                            settle_at: str | None = None, candidate_id: str | None = None) -> dict:
    iid, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO prospective_items(id,run_id,event_id,canonical_key,candidate_id,question,assertion_text,"
            "market,symbol,event_type_l2,event_time,source_url,captured_at,settle_at,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, run_id, event_id, canonical_key, candidate_id, question, assertion_text, event.get("market"), event.get("symbol"),
             event.get("event_type_l2"), event.get("event_time"), event.get("source_url"), captured_at, settle_at,
             "pending", ts, ts),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM prospective_items WHERE run_id=? AND canonical_key=?", (run_id, canonical_key)).fetchone()
    return dict(row) if row else {"id": iid, "run_id": run_id, "status": "pending"}


def create_prospective_candidate(*, run_id: str, canonical_key: str, market: str, symbol: str,
                                 company_name: str, event_count: int, latest_event_at: str,
                                 event_type_l2: str, title: str, source_url: str,
                                 evidence: list[dict]) -> dict:
    cid, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO prospective_candidates(id,run_id,canonical_key,market,symbol,company_name,"
            "event_count,latest_event_at,event_type_l2,title,source_url,evidence_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, run_id, canonical_key, market, symbol, company_name, int(event_count), latest_event_at,
             event_type_l2, title, source_url, _json(evidence), ts, ts),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM prospective_candidates WHERE run_id=? AND canonical_key=?",
            (run_id, canonical_key),
        ).fetchone()
    return _prospective_candidate(row) or {"id": cid, "run_id": run_id}


def _prospective_candidate(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["evidence"] = json.loads(d.pop("evidence_json")) if d.get("evidence_json") else []
    d["selected"] = bool(d.get("selected"))
    return d


def list_prospective_candidates(run_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM prospective_candidates WHERE run_id=? "
            "ORDER BY selected DESC, COALESCE(selection_rank, 999999), latest_event_at DESC",
            (run_id,),
        ).fetchall()
    return [_prospective_candidate(row) for row in rows if row]


def update_prospective_candidate_selection(candidate_id: str, *, selected: bool, rank: int | None,
                                             score: float | None, reason: str,
                                             model_version: str, prompt_version: str) -> dict | None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE prospective_candidates SET selected=?,selection_rank=?,selection_score=?,selection_reason=?,"
            "selector_model_version=?,selector_prompt_version=?,updated_at=? WHERE id=?",
            (1 if selected else 0, rank, score, reason, model_version, prompt_version, now_iso(), candidate_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM prospective_candidates WHERE id=?", (candidate_id,)).fetchone()
    return _prospective_candidate(row)


def list_prospective_items(run_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute("SELECT * FROM prospective_items WHERE run_id=? ORDER BY created_at ASC", (run_id,)).fetchall()
    return [dict(r) for r in rows]


def get_prospective_item(item_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM prospective_items WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def update_prospective_item(item_id: str, **values: Any) -> dict | None:
    allowed = {"status", "prediction_id", "settlement_id", "actual_label", "actual_value", "is_correct",
               "resolved_at", "error_message", "captured_at", "settle_at"}
    pairs = [(k, v) for k, v in values.items() if k in allowed]
    if not pairs:
        return get_prospective_item(item_id)
    with _lock:
        conn = _get_conn()
        conn.execute(f"UPDATE prospective_items SET {', '.join(f'{k}=?' for k, _ in pairs)}, updated_at=? WHERE id=?",
                     tuple(v for _, v in pairs) + (now_iso(), item_id))
        conn.commit()
    return get_prospective_item(item_id)


def add_prospective_prediction(*, item_id: str, pred_direction: str, confidence: float | None,
                               rationale: str | None, raw_prediction: dict, model_version: str,
                               prompt_version: str, adapter_version: str, git_commit: str,
                               as_of_at: str, evidence_hash: str, version: int = 1) -> dict:
    pid, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO prospective_predictions(id,item_id,version,pred_direction,confidence,rationale,raw_prediction_json,"
            "model_version,prompt_version,adapter_version,git_commit,as_of_at,evidence_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, item_id, version, pred_direction, confidence, rationale, _json(raw_prediction), model_version,
             prompt_version, adapter_version, git_commit, as_of_at, evidence_hash, ts),
        )
        conn.commit()
    return get_prospective_prediction(pid) or {"id": pid, "item_id": item_id, "version": version}


def get_prospective_prediction(prediction_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM prospective_predictions WHERE id=?", (prediction_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["raw_prediction"] = json.loads(d.pop("raw_prediction_json")) if d.get("raw_prediction_json") else {}
    return d


def get_latest_prospective_prediction(item_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT id FROM prospective_predictions WHERE item_id=? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
    return get_prospective_prediction(row["id"]) if row else None


def add_prospective_evidence(*, prediction_id: str, ordinal: int, evidence: dict) -> dict:
    eid, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO prospective_evidence(id,prediction_id,ordinal,source_kind,source_url,title,content,published_at,"
            "retrieved_at,content_hash,raw_payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, prediction_id, ordinal, evidence.get("source_kind"), evidence.get("source_url"), evidence.get("title"),
             evidence.get("content"), evidence.get("published_at"), evidence.get("retrieved_at"), evidence.get("content_hash"),
             _json(evidence.get("raw_payload", evidence)), ts),
        )
        conn.commit()
    return dict(_get_conn().execute("SELECT * FROM prospective_evidence WHERE id=?", (eid,)).fetchone())


def list_prospective_evidence(prediction_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute("SELECT * FROM prospective_evidence WHERE prediction_id=? ORDER BY ordinal", (prediction_id,)).fetchall()
    return [dict(r) for r in rows]


def add_prospective_analysis_trace(*, run_id: str, sequence_no: int, stage: str, stage_title: str,
                                   as_of_at: str, item_id: str | None = None,
                                   candidate_id: str | None = None,
                                   input_snapshot: dict | list | None = None,
                                   output_snapshot: dict | list | None = None,
                                   evidence_refs: list | None = None,
                                   model_version: str | None = None,
                                   prompt_version: str | None = None,
                                   trace_version: str = "v1", content_hash: str | None = None) -> dict:
    tid, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO prospective_analysis_trace(id,run_id,item_id,candidate_id,sequence_no,stage,stage_title,"
            "as_of_at,input_snapshot_json,output_snapshot_json,evidence_refs_json,model_version,prompt_version,"
            "trace_version,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, run_id, item_id, candidate_id, int(sequence_no), stage, stage_title, as_of_at,
             _json(input_snapshot), _json(output_snapshot), _json(evidence_refs or []), model_version,
             prompt_version, trace_version, content_hash, ts),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM prospective_analysis_trace WHERE run_id=? AND item_id IS ? AND sequence_no=?",
            (run_id, item_id, int(sequence_no)),
        ).fetchone()
    return _prospective_analysis_trace(row) or {"id": tid, "run_id": run_id, "stage": stage}


def _prospective_analysis_trace(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    value = dict(row)
    for key, short, default in (
        ("input_snapshot_json", "input_snapshot", {}),
        ("output_snapshot_json", "output_snapshot", {}),
        ("evidence_refs_json", "evidence_refs", []),
    ):
        value[short] = json.loads(value.pop(key)) if value.get(key) else default
    return value


def list_prospective_analysis_trace(item_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM prospective_analysis_trace WHERE item_id=? ORDER BY sequence_no",
            (item_id,),
        ).fetchall()
    return [_prospective_analysis_trace(row) for row in rows if row]


def add_prospective_settlement(*, item_id: str, settlement_mode: str, status: str,
                               actual_label: str | None = None, actual_value: float | None = None,
                               result_source: dict | None = None, reasoning: str = "",
                               settled_at: str | None = None) -> dict:
    sid, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT COALESCE(MAX(attempt_no),0)+1 n FROM prospective_settlements WHERE item_id=?", (item_id,)).fetchone()
        attempt = int(row["n"])
        conn.execute(
            "INSERT INTO prospective_settlements(id,item_id,attempt_no,settlement_mode,actual_label,actual_value,result_source_json,"
            "reasoning,status,settled_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, item_id, attempt, settlement_mode, actual_label, actual_value, _json(result_source or {}), reasoning, status, settled_at, ts),
        )
        conn.commit()
    return {"id": sid, "item_id": item_id, "attempt_no": attempt, "status": status, "actual_label": actual_label,
            "actual_value": actual_value, "reasoning": reasoning, "settled_at": settled_at, "created_at": ts}


def get_latest_prospective_settlement(item_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute(
            "SELECT * FROM prospective_settlements WHERE item_id=? ORDER BY attempt_no DESC LIMIT 1",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    value = dict(row)
    value["result_source"] = json.loads(value.pop("result_source_json")) if value.get("result_source_json") else {}
    return value


def list_prospective_settlements(item_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute("SELECT * FROM prospective_settlements WHERE item_id=? ORDER BY attempt_no", (item_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["result_source"] = json.loads(d.pop("result_source_json")) if d.get("result_source_json") else {}; out.append(d)
    return out


def add_prospective_metric_snapshot(*, run_id: str, metrics: dict) -> int:
    ts = now_iso()
    with _lock:
        cur = _get_conn().execute(
            "INSERT INTO prospective_metric_snapshots(run_id,calculated_at,n_predicted,n_settled,n_correct,accuracy,coverage,brier_score,ece,metrics_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, ts, metrics.get("n_predicted", 0), metrics.get("n_settled", 0), metrics.get("n_correct", 0),
             metrics.get("accuracy"), metrics.get("coverage"), metrics.get("brier_score"), metrics.get("ece"), _json(metrics)),
        ); _get_conn().commit(); return int(cur.lastrowid or 0)


def list_prospective_metric_snapshots(run_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute("SELECT * FROM prospective_metric_snapshots WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["metrics"] = json.loads(d.pop("metrics_json")) if d.get("metrics_json") else {}; out.append(d)
    return out


def create_prospective_job(*, run_id: str, job_type: str, idempotency_key: str,
                           item_id: str | None = None, run_after: str | None = None) -> dict:
    jid, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO prospective_jobs(id,run_id,item_id,job_type,idempotency_key,status,run_after,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (jid, run_id, item_id, job_type, idempotency_key, "queued", run_after or ts, ts, ts),
        ); conn.commit()
        row = conn.execute("SELECT * FROM prospective_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    return dict(row) if row else {"id": jid, "status": "queued"}


def list_due_prospective_jobs(limit: int = 10) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM prospective_jobs WHERE status='queued' AND run_after<=? AND (locked_until IS NULL OR locked_until<?) ORDER BY run_after LIMIT ?",
            (now_iso(), now_iso(), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def update_prospective_job(job_id: str, *, status: str, attempts: int | None = None, last_error: str | None = None) -> None:
    fields = ["status=?", "updated_at=?"]; args: list[Any] = [status, now_iso()]
    if attempts is not None: fields.append("attempts=?"); args.append(attempts)
    if last_error is not None: fields.append("last_error=?"); args.append(last_error)
    args.append(job_id)
    with _lock:
        _get_conn().execute(f"UPDATE prospective_jobs SET {', '.join(fields)} WHERE id=?", tuple(args)); _get_conn().commit()


# ------------------------------------------------------ simulation jobs ----

def _decode_simulation_job(row: sqlite3.Row | None) -> Optional[dict]:
    if row is None:
        return None
    data = dict(row)
    data["request_payload"] = json.loads(data["request_payload"])
    return data


def create_simulation_job(
    case_id: str,
    graph_artifact_id: str,
    gateway_job: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict:
    job_id, ts = new_id(), now_iso()
    with _lock:
        conn = _get_conn()
        existing = conn.execute(
            "SELECT * FROM simulation_jobs WHERE gateway_job_id=?",
            (str(gateway_job["job_id"]),),
        ).fetchone()
        if existing:
            return _decode_simulation_job(existing) or {}
        conn.execute(
            """
            INSERT INTO simulation_jobs(
                id,case_id,graph_artifact_id,gateway_job_id,status,stage,progress,
                request_payload,error,artifact_id,created_at,updated_at,finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id, case_id, graph_artifact_id, str(gateway_job["job_id"]),
                str(gateway_job.get("status") or "queued"),
                str(gateway_job.get("stage") or "queued"),
                float(gateway_job.get("progress") or 0),
                json.dumps(request_payload, ensure_ascii=False, default=str),
                gateway_job.get("error"), None, ts, ts, gateway_job.get("finished_at"),
            ),
        )
        conn.commit()
    return get_simulation_job(job_id) or {}


def get_simulation_job(job_id: str) -> Optional[dict]:
    with _lock:
        row = _get_conn().execute(
            "SELECT * FROM simulation_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return _decode_simulation_job(row)


def list_simulation_jobs(case_id: str) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT * FROM simulation_jobs WHERE case_id=? ORDER BY created_at DESC",
            (case_id,),
        ).fetchall()
    return [_decode_simulation_job(row) or {} for row in rows]


def update_simulation_job(job_id: str, **values: Any) -> Optional[dict]:
    allowed = {"status", "stage", "progress", "error", "artifact_id", "finished_at"}
    fields = {key: value for key, value in values.items() if key in allowed}
    if not fields:
        return get_simulation_job(job_id)
    fields["updated_at"] = now_iso()
    assignments = ",".join(f"{key}=?" for key in fields)
    with _lock:
        conn = _get_conn()
        conn.execute(
            f"UPDATE simulation_jobs SET {assignments} WHERE id=?",
            (*fields.values(), job_id),
        )
        conn.commit()
    return get_simulation_job(job_id)
