"""Validate offline predictions and freeze a separate, evaluation-ready dataset.

Preview never persists state. Confirmation resubmits the file and binds it to the
exact event snapshot, Oracle bytes and metadata that the user previewed.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .. import config, db
from .datasets import materialize_contract, verify_materialized_snapshot
from .evaluation_protocol import normalize_evaluation_horizon

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
FIELDS = ("event_id", "direction", "confidence", "rationale", "horizon", "expected_return_pct")
REQUIRED_FIELDS = FIELDS[:-1]


class PredictionImportError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _source(dataset_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], bytes, bytes | None]:
    dataset = db.get_bt_dataset(dataset_id)
    if dataset is None:
        raise PredictionImportError("所选事件集不存在，请重新选择。", 404)
    if str(dataset.get("dataset_kind") or "event") != "event":
        raise PredictionImportError("预测结果只能匹配事件数据集。")
    path = Path(str(dataset.get("path") or ""))
    if not path.is_file() or dataset.get("status") not in (None, "available"):
        raise PredictionImportError("所选事件集没有可读取的本地快照。")
    if dataset.get("snapshot_hash"):
        valid, _ = verify_materialized_snapshot(dataset)
        if not valid:
            raise PredictionImportError("所选事件集的文件已发生变化，请先重新注册数据版本。", 409)
    try:
        raw = path.read_bytes()
        rows = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
        if not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError("empty or non-object events")
        ids = [str(row.get("event_id") or row.get("id") or "").strip() for row in rows]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("missing or duplicate event ID")
        labels_path = dataset.get("labels_path")
        labels = Path(str(labels_path)).read_bytes() if labels_path else None
    except (OSError, UnicodeError, ValueError) as exc:
        raise PredictionImportError("事件集或关联行情标签无法读取，请先检查原数据。") from exc
    return dataset, rows, raw, labels


def _parse(content: bytes, filename: str) -> list[tuple[int, Any]]:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("请选择非空文件，大小不能超过 10 MiB。")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("文件编码需为 UTF-8；CSV 可在导出时选择 UTF-8 编码。") from exc
    extension = Path(filename).suffix.lower()
    if extension == ".csv":
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = reader.fieldnames
        if not headers:
            raise ValueError("CSV 缺少列名。")
        if len(headers) != len(set(headers)):
            raise ValueError("CSV 列名重复，请确保每个字段只有一列。")
        absent = set(REQUIRED_FIELDS) - set(headers)
        if absent:
            raise ValueError("CSV 缺少必填列：" + ", ".join(sorted(absent)))
        rows = []
        for row in reader:
            rows.append((reader.line_num, row))
        return rows
    if extension == ".json":
        value = json.loads(text, object_pairs_hook=_unique_object)
        if isinstance(value, dict) and set(value) == {"predictions"}:
            value = value["predictions"]
        if not isinstance(value, list):
            raise ValueError("JSON 应为预测对象数组，或仅包含 predictions 数组的对象。")
        return list(enumerate(value, start=1))
    if extension == ".jsonl":
        return [
            (number, json.loads(line, object_pairs_hook=_unique_object))
            for number, line in enumerate(text.splitlines(), start=1) if line.strip()
        ]
    raise ValueError("请选择 .csv、.json 或 .jsonl 文件。")


def _number(value: Any, *, optional: bool = False) -> float | None:
    if optional and (value is None or (isinstance(value, str) and not value.strip())):
        return None
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float)):
        raise ValueError("必须填写有限数值")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("必须填写有限数值，不能带百分号") from exc
    if not math.isfinite(result):
        raise ValueError("必须填写有限数值，不能是 NaN 或 Infinity")
    return result


def inspect_predictions(
    *, dataset_id: str, horizon: str, filename: str, content: bytes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a bounded user preview and private validated data for commit."""
    try:
        horizon = normalize_evaluation_horizon(horizon)
    except ValueError as exc:
        raise PredictionImportError(str(exc)) from exc
    dataset, events, event_bytes, labels_bytes = _source(dataset_id)
    event_ids = [str(event.get("event_id") or event.get("id") or "").strip() for event in events]
    expected = set(event_ids)
    errors: list[dict[str, Any]] = []
    error_count = 0
    format_error_count = 0

    def error(message: str, *, row: int | None = None, event_id: str | None = None,
              field: str | None = None, format_error: bool = True) -> None:
        nonlocal error_count, format_error_count
        error_count += 1
        format_error_count += int(format_error)
        if len(errors) < 50:
            errors.append({"row": row, "event_id": event_id, "field": field, "message": message})

    try:
        rows = _parse(content, filename)
    except (ValueError, csv.Error) as exc:
        error(str(exc))
        rows = []
    if not rows and not errors:
        error("文件没有预测记录。")
    seen: Counter[str] = Counter()
    predictions: dict[str, dict[str, Any]] = {}
    for number, row in rows:
        if not isinstance(row, dict):
            error("每条预测必须是对象。", row=number)
            continue
        row_errors = error_count
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            error("event_id 必须填写事件集中的完整 ID。", row=number, field="event_id")
            event_id = None
        else:
            event_id = event_id.strip()
            seen[event_id] += 1
        unknown_fields = set(row) - set(FIELDS)
        if unknown_fields:
            names = ", ".join(sorted(str(key) for key in unknown_fields))
            error(f"存在不支持的列或字段：{names}。请使用下载的模板。", row=number, event_id=event_id)
        direction = row.get("direction")
        if not isinstance(direction, str) or direction.strip().lower() not in {"up", "down", "neutral"}:
            error("direction 只能为 up、down 或 neutral。", row=number, event_id=event_id, field="direction")
        confidence = None
        try:
            confidence = _number(row.get("confidence"))
            if confidence is None or not 0 <= confidence <= 1:
                raise ValueError("必须在 0 到 1 之间")
        except ValueError as exc:
            error(f"confidence {exc}。", row=number, event_id=event_id, field="confidence")
        rationale = row.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            error("rationale 必须填写预测理由。", row=number, event_id=event_id, field="rationale")
        row_horizon = row.get("horizon")
        try:
            if not isinstance(row_horizon, str) or not row_horizon.strip():
                raise ValueError("horizon 必须明确填写预测窗口。")
            if normalize_evaluation_horizon(row_horizon) != horizon:
                raise ValueError(f"horizon 必须与本次评价窗口 {horizon} 一致。")
        except ValueError as exc:
            error(str(exc), row=number, event_id=event_id, field="horizon")
        expected_return = None
        try:
            expected_return = _number(row.get("expected_return_pct"), optional=True)
        except ValueError as exc:
            error(f"expected_return_pct {exc}；2 表示 +2%。", row=number, event_id=event_id, field="expected_return_pct")
        if error_count == row_errors and event_id:
            predictions[event_id] = {
                "event_id": event_id, "direction": direction.strip().lower(),
                "confidence": confidence, "rationale": rationale.strip(),
                "horizon": horizon, "expected_return_pct": expected_return,
            }
    missing = [event_id for event_id in event_ids if event_id not in seen]
    unknown = sorted(set(seen) - expected)
    duplicate = sorted(event_id for event_id, count in seen.items() if count > 1)
    for ids, description in ((missing, "缺少所选事件的预测"), (unknown, "event_id 不在所选事件集中"),
                             (duplicate, "event_id 重复，每条事件只能提交一个预测")):
        for event_id in ids:
            error(description, event_id=event_id, field="event_id", format_error=False)
    valid = error_count == 0
    manifest = {
        "version": "prediction-import-v1", "dataset": dataset,
        "events_sha256": _digest(event_bytes),
        "labels_sha256": _digest(labels_bytes) if labels_bytes is not None else None,
        "prediction_sha256": _digest(content), "format": Path(filename).suffix.lower(), "horizon": horizon,
    }
    token = _digest(_json(manifest).encode("utf-8")) if valid else None
    ordered_predictions = [predictions[event_id] for event_id in event_ids if event_id in predictions]
    preview = {
        "valid": valid, "dataset_id": dataset_id, "horizon": horizon, "unit": "percent",
        "event_count": len(events), "total_rows": len(rows),
        "matched_count": len(set(seen) & expected),
        "return_forecast_count": sum(row["expected_return_pct"] is not None for row in ordered_predictions),
        "missing_ids": missing, "unknown_ids": unknown, "duplicate_ids": duplicate,
        "missing_count": len(missing), "unknown_count": len(unknown), "duplicate_count": len(duplicate),
        "format_error_count": format_error_count, "error_count": error_count,
        "errors": errors, "preview": ordered_predictions[:5], "preview_token": token,
    }
    private = {
        "dataset": dataset, "events": events, "predictions": predictions,
        "labels_bytes": labels_bytes, "manifest": manifest,
    }
    return preview, private


def prediction_template(*, dataset_id: str, horizon: str, format: str) -> tuple[bytes, str]:
    try:
        horizon = normalize_evaluation_horizon(horizon)
    except ValueError as exc:
        raise PredictionImportError(str(exc)) from exc
    _, events, _, _ = _source(dataset_id)
    rows = [dict.fromkeys(FIELDS, "") for _ in events]
    for row, event in zip(rows, events):
        row["event_id"] = str(event.get("event_id") or event.get("id") or "")
        row["horizon"] = horizon
    if format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode("utf-8-sig"), "text/csv"
    if format == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"
    if format == "jsonl":
        return ("\n".join(_json(row) for row in rows) + "\n").encode("utf-8"), "application/x-ndjson"
    raise PredictionImportError("模板仅支持 csv、json 和 jsonl。")


def commit_predictions(
    *, dataset_id: str, horizon: str, filename: str, content: bytes,
    preview_token: str, name: str = "",
) -> dict[str, Any]:
    preview, private = inspect_predictions(dataset_id=dataset_id, horizon=horizon, filename=filename, content=content)
    if not preview["valid"]:
        raise PredictionImportError("预测文件未通过校验，请重新预览并修正错误。")
    if not preview_token or preview_token != preview["preview_token"]:
        raise PredictionImportError("文件、事件集或评价窗口已变化，请重新预览后确认导入。", 409)
    name = name.strip()
    if len(name) > 200:
        raise PredictionImportError("导入名称不能超过 200 字。")
    original = private["dataset"]
    new_id = f"predictions_{db.new_id()}"
    target = Path(config.DATA_DIR) / "backtests" / "prediction_imports" / new_id
    target.mkdir(parents=True, exist_ok=False)
    events_path = target / "events.jsonl"
    labels_path = target / "labels.jsonl" if private["labels_bytes"] is not None else None
    try:
        with events_path.open("x", encoding="utf-8") as handle:
            for source_event in private["events"]:
                row = dict(source_event)
                event_id = str(row.get("event_id") or row.get("id") or "").strip()
                prediction = private["predictions"][event_id]
                # Whitelist prediction fields: uploaded facts/Oracle columns can
                # never overwrite the original event facts or available_time.
                for field in ("direction", "confidence", "rationale", "horizon", "expected_return_pct"):
                    row[f"analysis_{field}"] = prediction[field]
                handle.write(_json(row) + "\n")
        if labels_path:
            labels_path.write_bytes(private["labels_bytes"])
        source = {
            "type": "local_file", "provider": "imported_event_predictions", "ref": str(events_path),
            "metadata": {
                "contains_imported_decisions": True, "import_id": new_id, "source_dataset_id": dataset_id,
                "source_dataset_version": original.get("dataset_version") or original.get("current_version"),
                "source_snapshot_hash": original.get("snapshot_hash"),
                "source_provenance": original.get("source") or {},
                "prediction_filename": Path(filename).name,
                "prediction_sha256": private["manifest"]["prediction_sha256"],
                "prediction_horizon": preview["horizon"], "prediction_unit": "percent",
            },
        }
        contract = materialize_contract({**original, "path": str(events_path), "source": source})
        stored = db.upsert_bt_dataset(
            dataset_id=new_id, path=str(events_path), name=name or f"{original.get('name') or dataset_id} · 导入预测",
            total_events=len(private["events"]), labels_path=str(labels_path) if labels_path else None,
            by_market=original.get("by_market"), by_type=original.get("by_type"),
            by_symbol=original.get("by_symbol"), date_range=original.get("date_range"),
            dataset_kind="event", dataset_version=contract["dataset_version"],
            snapshot_hash=contract.get("snapshot_hash"), status=contract["status"], source=source,
            markets=contract.get("markets"), symbols=contract.get("symbols"), asset_type=contract.get("asset_type"),
            frequency=contract.get("frequency"), adjustment=contract.get("adjustment"), calendar=contract.get("calendar"),
            schema_mapping=contract.get("schema_mapping"), capabilities=contract.get("capabilities"),
            coverage=contract.get("coverage"), quality_status=contract.get("quality_status") or "unverified",
            quality_report=contract.get("quality_report"),
        )
        return stored
    except Exception:
        if db.get_bt_dataset(new_id) is None:
            shutil.rmtree(target, ignore_errors=True)
        raise
