"""SQLite persistence isolated from the legacy event-backtest tables."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
import zlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from .comparison import compare_results
from .models import BacktestResult, MarketDataset, QuantBacktestError, utc_now_iso


class QuantStore:
    """Persist datasets, runs, metrics, trades, and chart series in SQLite.

    It can point at Pronoia's existing ``FEVER_DB_PATH``. All table names use a
    ``quant_`` prefix and never alter ``bt_runs`` or event predictions.
    """

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            from .. import config

            db_path = config.DB_PATH
        self.db_path = str(Path(db_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quant_datasets(
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    frequency TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    bar_count INTEGER NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quant_datasets_symbol
                    ON quant_datasets(symbol, frequency, created_at DESC);

                CREATE TABLE IF NOT EXISTS quant_runs(
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    dataset_fingerprint TEXT NOT NULL,
                    comparison_hash TEXT NOT NULL,
                    strategy_json TEXT NOT NULL,
                    execution_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error_msg TEXT,
                    FOREIGN KEY(dataset_id) REFERENCES quant_datasets(id)
                );
                CREATE INDEX IF NOT EXISTS idx_quant_runs_created ON quant_runs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_quant_runs_dataset ON quant_runs(dataset_fingerprint, comparison_hash);

                CREATE TABLE IF NOT EXISTS quant_results(
                    run_id TEXT PRIMARY KEY,
                    metrics_json TEXT NOT NULL,
                    result_zlib BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES quant_runs(id) ON DELETE CASCADE
                );
                """
            )

    def save_dataset(self, dataset: MarketDataset) -> dict[str, Any]:
        summary = dataset.summary()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM quant_datasets WHERE fingerprint=?", (dataset.fingerprint,)
            ).fetchone()
            if existing:
                return self._dataset_row(existing)
            dataset_id, now = "qd_" + uuid.uuid4().hex[:12], utc_now_iso()
            conn.execute(
                """INSERT INTO quant_datasets(
                    id,name,symbol,market,frequency,source_type,source_ref,fingerprint,
                    bar_count,start_at,end_at,metadata_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    dataset_id,
                    dataset.name,
                    dataset.symbol,
                    dataset.market,
                    dataset.frequency,
                    dataset.source_type,
                    dataset.source_ref,
                    dataset.fingerprint,
                    len(dataset.bars),
                    dataset.bars[0].timestamp,
                    dataset.bars[-1].timestamp,
                    json.dumps(summary.get("metadata") or {}, ensure_ascii=False),
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM quant_datasets WHERE id=?", (dataset_id,)).fetchone()
        return self._dataset_row(row)

    def create_run(
        self,
        *,
        name: str,
        dataset_id: str,
        dataset_fingerprint: str,
        strategy: Mapping[str, Any],
        execution: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not str(name).strip():
            raise QuantBacktestError("回测名称不能为空")
        comparison_payload = {"dataset_fingerprint": dataset_fingerprint, "execution": dict(execution)}
        comparison_hash = hashlib.sha256(
            json.dumps(comparison_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        run_id, now = "qr_" + uuid.uuid4().hex[:12], utc_now_iso()
        with self._lock, self._connect() as conn:
            if not conn.execute("SELECT 1 FROM quant_datasets WHERE id=?", (dataset_id,)).fetchone():
                raise QuantBacktestError(f"量化数据集不存在: {dataset_id}")
            conn.execute(
                """INSERT INTO quant_runs(
                    id,name,status,dataset_id,dataset_fingerprint,comparison_hash,
                    strategy_json,execution_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    name.strip(),
                    "pending",
                    dataset_id,
                    dataset_fingerprint,
                    comparison_hash,
                    json.dumps(dict(strategy), ensure_ascii=False),
                    json.dumps(dict(execution), ensure_ascii=False),
                    now,
                ),
            )
        return self.get_run(run_id) or {"id": run_id, "status": "pending"}

    def mark_running(self, run_id: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE quant_runs SET status='running',started_at=COALESCE(started_at,?),error_msg=NULL WHERE id=?",
                (now, run_id),
            )
            if not cursor.rowcount:
                raise QuantBacktestError(f"量化回测不存在: {run_id}")
        return self.get_run(run_id) or {}

    def save_result(self, run_id: str, result: BacktestResult) -> dict[str, Any]:
        now = utc_now_iso()
        result_dict = result.to_dict()
        raw = json.dumps(result_dict, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        compressed = zlib.compress(raw, level=6)
        with self._lock, self._connect() as conn:
            if not conn.execute("SELECT 1 FROM quant_runs WHERE id=?", (run_id,)).fetchone():
                raise QuantBacktestError(f"量化回测不存在: {run_id}")
            conn.execute(
                """INSERT INTO quant_results(run_id,metrics_json,result_zlib,created_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     metrics_json=excluded.metrics_json,
                     result_zlib=excluded.result_zlib,
                     created_at=excluded.created_at""",
                (run_id, json.dumps(dict(result.metrics), ensure_ascii=False), compressed, now),
            )
            conn.execute(
                "UPDATE quant_runs SET status='done',finished_at=?,error_msg=NULL WHERE id=?",
                (now, run_id),
            )
        return self.get_run(run_id, include_result=True) or {}

    def mark_failed(self, run_id: str, error: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE quant_runs SET status='failed',finished_at=?,error_msg=? WHERE id=?",
                (now, str(error)[:4000], run_id),
            )
            if not cursor.rowcount:
                raise QuantBacktestError(f"量化回测不存在: {run_id}")
        return self.get_run(run_id) or {}

    def get_run(self, run_id: str, *, include_result: bool = False) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM quant_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return None
            result_row = (
                conn.execute("SELECT * FROM quant_results WHERE run_id=?", (run_id,)).fetchone()
                if include_result
                else None
            )
        return self._run_row(row, result_row)

    def list_runs(self, *, limit: int = 100, include_metrics: bool = True) -> list[dict[str, Any]]:
        limit = min(500, max(1, int(limit)))
        with self._lock, self._connect() as conn:
            if include_metrics:
                rows = conn.execute(
                    """SELECT r.*,q.metrics_json AS result_metrics_json
                       FROM quant_runs r LEFT JOIN quant_results q ON q.run_id=r.id
                       ORDER BY r.created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM quant_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        output = []
        for row in rows:
            item = self._run_row(row)
            if include_metrics and row["result_metrics_json"]:
                item["metrics"] = json.loads(row["result_metrics_json"])
            output.append(item)
        return output

    def compare(self, run_ids: Sequence[str]) -> dict[str, Any]:
        runs = []
        for run_id in run_ids:
            run = self.get_run(str(run_id), include_result=True)
            if not run:
                raise QuantBacktestError(f"量化回测不存在: {run_id}")
            runs.append(run)
        return compare_results(runs)

    def delete_run(self, run_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM quant_runs WHERE id=?", (run_id,))
            return bool(cursor.rowcount)

    @staticmethod
    def _dataset_row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    @staticmethod
    def _run_row(row: sqlite3.Row, result_row: sqlite3.Row | None = None) -> dict[str, Any]:
        item = dict(row)
        item["strategy"] = json.loads(item.pop("strategy_json"))
        item["execution"] = json.loads(item.pop("execution_json"))
        item.pop("result_metrics_json", None)
        if result_row is not None:
            try:
                raw = zlib.decompress(result_row["result_zlib"])
                item["result"] = json.loads(raw.decode("utf-8"))
            except (zlib.error, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise QuantBacktestError(f"回测结果损坏: {item['id']}") from exc
        return item
