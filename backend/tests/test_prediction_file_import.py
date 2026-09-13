"""Offline result import preserves facts/Oracle and rejects ambiguous matches."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def import_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app import config, db
    from app.event_backtest.datasets import materialize_contract
    from app.routes.prediction_import import router

    if db._conn is not None:
        db._conn.close()
    db._conn = None
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    db.init_db()
    events = [
        {"event_id": event_id, "market": "US", "symbol": "TEST", "event_time": "2025-06-02T10:00:00",
         "available_time": "2025-06-02T10:30:00", "occurred_at": "2025-06-02T09:30:00",
         "event_type_l2": "earnings", "title": "Earnings release", "event_text": "Reported sales rose 12%.",
         "source_url": "https://example.invalid/release", "benchmark": "SPY",
         "event_facts": {"actual_value": 12, "value_unit": "%"},
         "analysis_direction": "down", "analysis_confidence": .1, "analysis_rationale": "Old opinion",
         "analysis_expected_return_pct": -99, "expected_return_pct": -88,
         "custom_source_metadata": {"note": "original fact field"}}
        for event_id in ("event-a", "event-b")
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text("\n".join(json.dumps({"event_id": row["event_id"], "ret_t3": .01}) for row in events), encoding="utf-8")
    contract = materialize_contract({"dataset_kind": "event", "path": str(events_path),
                                     "source": {"type": "local_file", "provider": "test_original"},
                                     "frequency": "1d", "adjustment": "split", "calendar": "US"})
    dataset = db.upsert_bt_dataset(
        dataset_id="original", path=str(events_path), name="Original", labels_path=str(labels_path),
        total_events=2, by_market={"US": 2}, by_symbol={"TEST": 2}, by_type={"earnings": 2},
        **{key: contract.get(key) for key in ("dataset_version", "snapshot_hash", "source", "markets", "symbols",
                                             "coverage", "capabilities", "quality_report", "frequency", "adjustment", "calendar")},
    )
    app = FastAPI()
    app.include_router(router)
    yield TestClient(app), dataset, tmp_path
    if db._conn is not None:
        db._conn.close()
    db._conn = None


def _rows():
    return [
        {"event_id": "event-b", "direction": "neutral", "confidence": 0, "rationale": "Stay flat", "horizon": "t3"},
        {"event_id": "event-a", "direction": "up", "confidence": .75,
         "rationale": '收入增长，解释包含 "引号"\n第二行', "horizon": "t3", "expected_return_pct": 2},
    ]


def _file(rows, format="json"):
    if format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=["event_id", "direction", "confidence", "rationale", "horizon", "expected_return_pct"])
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode("utf-8-sig")
    if format == "jsonl":
        return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode("utf-8-sig")
    return json.dumps(rows, ensure_ascii=False).encode("utf-8-sig")


def _request(client, action, content, format="json", **data):
    return client.post(f"/api/bt/prediction-import/{action}",
                       data={"dataset_id": "original", "horizon": "t3", **data},
                       files={"file": (f"results.{format}", content)})


@pytest.mark.parametrize("format", ["csv", "json", "jsonl"])
def test_preview_commit_freezes_prediction_only_clone(import_env, format):
    from app import db
    from app.event_backtest.application import load_events
    from app.event_backtest.orchestrator import _run_provided_analysis_with_cb
    from app.event_backtest.protocol import events_snapshot_sha256

    client, original, root = import_env
    source_bytes = Path(original["path"]).read_bytes()
    oracle_bytes = Path(original["labels_path"]).read_bytes()
    content = _file(_rows(), format)
    preview = _request(client, "preview", content, format)
    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert value["valid"] and value["matched_count"] == 2 and value["return_forecast_count"] == 1
    assert value["preview"][0]["event_id"] == "event-a"  # source order, never CSV order
    assert value["unit"] == "percent" and value["preview"][0]["expected_return_pct"] == 2
    assert len(db.list_bt_datasets()) == 1
    assert not (root / "data").exists()  # no staging directory or preview data persisted
    result = _request(client, "commit", content, format, preview_token=value["preview_token"], name="My model")
    assert result.status_code == 201, result.text
    imported = result.json()
    assert imported["id"] != original["id"] and imported["name"] == "My model"
    assert imported["decision_ready"] and imported["input_contract"] == "event_plus_provided_analysis"
    assert imported["frequency"] == "1d" and imported["adjustment"] == "split" and imported["calendar"] == "US"
    assert Path(original["path"]).read_bytes() == source_bytes
    assert Path(original["labels_path"]).read_bytes() == oracle_bytes
    assert imported["labels_path"] != original["labels_path"]
    assert Path(imported["labels_path"]).read_bytes() == oracle_bytes
    assert events_snapshot_sha256(imported["path"]) == events_snapshot_sha256(original["path"])
    before = [json.loads(line) for line in source_bytes.decode().splitlines()]
    after = [json.loads(line) for line in Path(imported["path"]).read_text().splitlines()]
    for source, clone in zip(before, after):
        assert {k: v for k, v in source.items() if not k.startswith("analysis_")} == {
            k: v for k, v in clone.items() if not k.startswith("analysis_")}
    emitted = []
    predictions = _run_provided_analysis_with_cb(load_events(imported["path"]), run_id="offline", model_version="offline-v1",
                                                 target_horizon="t3", on_pred=lambda pred, event: emitted.append(pred))
    assert len(emitted) == 2
    assert predictions[0].expected_return_pct == 2
    assert predictions[1].expected_return_pct is None  # old numeric opinion must not leak through
    assert predictions[1].pred_direction == "neutral" and not predictions[1].abstain

    # The real import API creates a new provenance/version; that must not
    # prevent comparing its opinions with Pronoia on the original event facts.
    from app.event_backtest.protocol import build_protocol_hash
    from app.routes.arena import _validate_protocols
    from fastapi import HTTPException

    runs = []
    for index, dataset in enumerate((original, imported)):
        execution = {"fee_bps": 3, "slippage_bps": 2}
        evaluation = {"evaluation_horizon": "t3"}
        contract = db.get_bt_dataset_version(dataset["id"], dataset["dataset_version"])
        runner = "team_full" if index == 0 else "provided_analysis"
        run = db.create_bt_run(
            name=f"Compare {index}", runner=runner,
            events_path=dataset["path"], labels_path=dataset["labels_path"],
            out_path=str(root / f"compare-{index}.jsonl"), total_events=2,
            dataset_id=dataset["id"], dataset_version=dataset["dataset_version"],
            strategy_spec={"type": "event", "runner": runner,
                           "adapter": "existing_platform" if index == 0 else "imported_decisions"},
            execution_spec=execution,
            config={"dataset_snapshot": contract, "evaluation_protocol": evaluation},
            protocol_hash=build_protocol_hash(
                events_path=dataset["path"], labels_path=dataset["labels_path"],
                execution_spec=execution, evaluation_protocol=evaluation,
                dataset_version=dataset["dataset_version"], engine_mode="event_proxy",
            ),
        )
        runs.append(run)
    assert runs[0]["dataset_version"] != runs[1]["dataset_version"]
    result = _validate_protocols(runs)
    assert result["strict_comparable"] and result["mismatched_fields"] == []
    assert len(set(result["run_comparison_protocol_hashes"].values())) == 1
    assert len(set(result["run_integrity_hashes"].values())) == 2
    # An overwrite still invalidates the imported Run's original fingerprint.
    Path(imported["labels_path"]).write_text('{"event_id":"event-a","ret_t3":0.99}\n')
    with pytest.raises(HTTPException) as changed_oracle:
        _validate_protocols(runs)
    assert runs[1]["id"] in changed_oracle.value.detail["stale_protocols"]


@pytest.mark.parametrize("format", ["csv", "json", "jsonl"])
def test_template_has_all_ids_but_no_fabricated_predictions(import_env, format):
    client, _, _ = import_env
    response = client.get(f"/api/bt/prediction-import/template?dataset_id=original&horizon=t5&format={format}")
    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    if format == "csv":
        rows = list(csv.DictReader(io.StringIO(text)))
    elif format == "json":
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines()]
    assert [row["event_id"] for row in rows] == ["event-a", "event-b"]
    assert all(row["horizon"] == "t5" and row["direction"] == row["confidence"] == row["expected_return_pct"] == "" for row in rows)


def test_matching_errors_are_explicit_and_block_commit(import_env):
    client, _, _ = import_env
    rows = [_rows()[0], _rows()[0], {**_rows()[1], "event_id": "unknown-id"}]
    response = _request(client, "preview", _file(rows))
    result = response.json()
    assert not result["valid"] and result["preview_token"] is None
    assert result["duplicate_ids"] == ["event-b"]
    assert result["missing_ids"] == ["event-a"] and result["unknown_ids"] == ["unknown-id"]
    assert result["duplicate_count"] == result["missing_count"] == result["unknown_count"] == 1
    assert result["format_error_count"] == 0
    assert _request(client, "commit", _file(rows), preview_token="pretend").status_code == 422


@pytest.mark.parametrize("field,value", [
    ("direction", "buy"), ("confidence", -1), ("confidence", 2), ("confidence", True),
    ("confidence", "NaN"), ("rationale", ""), ("rationale", 123),
    ("horizon", "t5"), ("horizon", ""), ("horizon", "t20"),
    ("expected_return_pct", "2%"), ("expected_return_pct", "Infinity"),
    ("expected_return_pct", "NaN"), ("expected_return_pct", False), ("expected_return_pct", "1e999"),
    ("available_time", "2099-01-01"), ("ret_t3", 1),
])
def test_invalid_fields_and_uploaded_facts_are_rejected(import_env, field, value):
    client, _, _ = import_env
    rows = _rows()
    rows[0][field] = value
    result = _request(client, "preview", _file(rows)).json()
    assert not result["valid"] and result["format_error_count"] >= 1


@pytest.mark.parametrize("change", ["file", "horizon", "events", "oracle", "metadata"])
def test_commit_revalidates_preview_context(import_env, change):
    from app import db

    client, dataset, _ = import_env
    content = _file(_rows())
    token = _request(client, "preview", content).json()["preview_token"]
    if change == "file":
        rows = _rows()
        rows[0]["rationale"] = "Changed after preview"
        content = _file(rows)
    elif change == "horizon":
        rows = _rows()
        for row in rows:
            row["horizon"] = "t5"
        content = _file(rows)
    elif change == "events":
        path = Path(dataset["path"])
        path.write_text(path.read_text().replace("Reported sales", "Changed sales"))
    elif change == "oracle":
        path = Path(dataset["labels_path"])
        path.write_text(path.read_text().replace("0.01", "0.04"))
    elif change == "metadata":
        with db._lock:
            db._get_conn().execute("UPDATE bt_datasets SET name='Renamed' WHERE id='original'")
            db._get_conn().commit()
    response = _request(client, "commit", content, preview_token=token, horizon="t5" if change == "horizon" else "t3")
    assert response.status_code == 409, response.text
    assert len(db.list_bt_datasets()) == 1


def test_repeated_imports_have_separate_frozen_versions(import_env):
    from app import db

    client, _, _ = import_env
    content = _file(_rows())
    token = _request(client, "preview", content).json()["preview_token"]
    first = _request(client, "commit", content, preview_token=token).json()
    second = _request(client, "commit", content, preview_token=token).json()
    assert first["dataset_version"] != second["dataset_version"]
    assert db.get_bt_dataset_version(first["id"], first["dataset_version"])
    assert db.get_bt_dataset_version(second["id"], second["dataset_version"])


@pytest.mark.parametrize("filename,content", [
    ("results.json", b"{broken"), ("results.json", b'[{"event_id":"a","event_id":"b"}]'),
    ("results.jsonl", b"[]\n"), ("results.csv", b"event_id,event_id\na,b"),
    ("results.csv", b"event_id,direction\na,up"), ("results.csv", b"\xff\xfe"),
    ("results.txt", b"not an allowed format"), ("results.json", b"[]"),
])
def test_invalid_file_format_is_previewed_without_writes(import_env, filename, content):
    from app import db

    client, _, _ = import_env
    response = client.post("/api/bt/prediction-import/preview", data={"dataset_id": "original", "horizon": "t3"},
                           files={"file": (filename, content)})
    assert response.status_code == 200 and not response.json()["valid"]
    assert len(db.list_bt_datasets()) == 1


def test_oversized_upload_is_rejected(import_env, monkeypatch):
    from app.routes import prediction_import

    client, _, _ = import_env
    monkeypatch.setattr(prediction_import, "MAX_UPLOAD_BYTES", 64)
    assert _request(client, "preview", b"a" * 65).status_code == 413
