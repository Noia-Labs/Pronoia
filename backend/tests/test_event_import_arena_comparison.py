"""Imported opinions compare on verified event facts and frozen Oracle data."""
from __future__ import annotations

import copy
import json

import pytest
from fastapi import HTTPException

from app.event_backtest.datasets import materialize_contract
from app.event_backtest.protocol import (
    EVENT_COMPARISON_PROTOCOL_VERSION,
    build_protocol_hash,
    comparison_protocol_hash_for_run,
    comparison_signature_for_run,
    events_snapshot_sha256,
)
from app.routes.arena import _validate_protocols


@pytest.fixture
def make_run(tmp_path, monkeypatch):
    from app.routes import arena

    # These tests already provide persisted fingerprints and frozen metadata.
    # Any database lookup/write would be an unintended dependency on user data.
    def unexpected_database_call(*args, **kwargs):
        pytest.fail("This comparison should use only its isolated frozen files")

    monkeypatch.setattr(arena.db, "get_bt_dataset_version", unexpected_database_call)
    monkeypatch.setattr(arena.db, "update_bt_run_protocol_hash", unexpected_database_call)

    def make(run_id, *, facts=None, labels=None, prediction="up", source="registered_events"):
        events = tmp_path / f"{run_id}-events.jsonl"
        oracle = tmp_path / f"{run_id}-oracle.jsonl"
        row = {
            "event_id": "same-event", "market": "US", "symbol": "SPY",
            "event_time": "2025-01-03T08:30:00-05:00",
            "available_time": "2025-01-03T08:30:00-05:00",
            "event_type_l2": "macro", "title": "CPI release",
            "event_text": "CPI 2.8%, compared with 3.0% expected.",
            "source_url": "https://example.invalid/cpi", "benchmark": "SPY",
            **(facts or {}), "analysis_direction": prediction,
            "analysis_confidence": .8, "analysis_rationale": f"Opinion by {run_id}",
            "analysis_expected_return_pct": 2 if prediction == "up" else -1,
        }
        events.write_text(json.dumps(row) + "\n", encoding="utf-8")
        oracle.write_text(json.dumps(labels or {
            "event_id": "same-event", "label_t3": "up", "car_t3": .01, "ret_t3": .02,
        }) + "\n", encoding="utf-8")
        contract = materialize_contract({
            "dataset_kind": "event", "path": str(events), "frequency": "1d",
            "source": {"type": "local_file", "provider": source,
                       "metadata": {"import_id": run_id}},
        })
        run = {
            "id": run_id, "status": "done", "events_path": str(events),
            "labels_path": str(oracle), "dataset_version": contract["dataset_version"],
            "engine_mode": "event_proxy", "result_nature": "proxy",
            "strategy_spec": {"type": "event", "adapter": "imported_decisions",
                              "runner": "provided_analysis"},
            "execution_spec": {"fee_bps": 3, "slippage_bps": 2},
            "config": {"dataset_snapshot": contract,
                       "evaluation_protocol": {"evaluation_horizon": "t3"}},
        }
        freeze(run)
        return run

    return make


def freeze(run):
    run["protocol_hash"] = build_protocol_hash(
        events_path=run["events_path"], labels_path=run.get("labels_path"),
        execution_spec=run["execution_spec"],
        evaluation_protocol=run["config"]["evaluation_protocol"],
        dataset_version=run.get("dataset_version"),
        engine_mode=run.get("engine_mode") if run.get("dataset_version") or run.get("strategy_spec") else None,
    )


def test_imported_predictions_with_distinct_versions_compare_without_changing_integrity(make_run):
    original = make_run("pronoia")
    imported = make_run("imported", prediction="down", source="imported_event_predictions")
    assert original["dataset_version"] != imported["dataset_version"]
    assert events_snapshot_sha256(original["events_path"]) == events_snapshot_sha256(imported["events_path"])
    integrity = [original["protocol_hash"], imported["protocol_hash"]]
    assert integrity[0] != integrity[1]
    protocol = _validate_protocols([original, imported])
    assert protocol["strict_comparable"] is True
    assert protocol["mismatched_fields"] == []
    assert len(protocol["dataset_versions"]) == 2
    assert [original["protocol_hash"], imported["protocol_hash"]] == integrity
    assert len(set(protocol["run_integrity_hashes"].values())) == 2
    assert len(set(protocol["run_comparison_protocol_hashes"].values())) == 1
    signature = protocol["comparison_signatures"]["imported"]
    assert signature["version"] == EVENT_COMPARISON_PROTOCOL_VERSION
    assert "dataset_version" not in signature["dataset"]


@pytest.mark.parametrize("changed", ["facts", "oracle", "available_time"])
def test_actual_file_differences_reject_even_if_metadata_claims_same_hash(make_run, changed):
    original = make_run("original")
    different = make_run(
        "different", prediction="down",
        facts={"title": "Another release"} if changed == "facts" else
              {"available_time": "2025-01-03T16:30:00-05:00"} if changed == "available_time" else None,
        labels={"event_id": "same-event", "label_t3": "down", "car_t3": -.01, "ret_t3": -.02}
               if changed == "oracle" else None,
    )
    # This forged metadata used to take precedence over the actual file hash.
    different["config"]["dataset_snapshot"]["snapshot_hash"] = original["config"]["dataset_snapshot"]["snapshot_hash"]
    different["config"]["snapshot_hash"] = original["config"]["dataset_snapshot"]["snapshot_hash"]
    with pytest.raises(HTTPException) as rejected:
        _validate_protocols([original, different])
    fields = rejected.value.detail["comparison_protocol"]["mismatched_fields"]
    assert ("dataset.labels_sha256" if changed == "oracle" else "dataset.snapshot_hash") in fields


@pytest.mark.parametrize("field,value,expected_difference", [
    ("fee_bps", 5, "costs.fee_bps"),
    ("slippage_bps", 8, "costs.slippage_bps"),
    ("start_date", "2025-01-02", "window.start_date"),
    ("end_date", "2025-01-04", "window.end_date"),
    ("benchmark", "QQQ", "benchmark"),
])
def test_import_does_not_relax_execution_or_time_window_contract(make_run, field, value, expected_difference):
    original = make_run("original")
    imported = make_run("imported")
    imported["execution_spec"][field] = value
    freeze(imported)
    with pytest.raises(HTTPException) as rejected:
        _validate_protocols([original, imported])
    assert expected_difference in rejected.value.detail["comparison_protocol"]["mismatched_fields"]


def test_import_does_not_relax_forecast_horizon(make_run):
    original = make_run("original")
    imported = make_run("imported")
    imported["config"]["evaluation_protocol"]["evaluation_horizon"] = "t5"
    freeze(imported)
    with pytest.raises(HTTPException) as rejected:
        _validate_protocols([original, imported])
    assert "evaluation.evaluation_horizon" in rejected.value.detail["comparison_protocol"]["mismatched_fields"]


@pytest.mark.parametrize("field,empty", [("events_path", False), ("labels_path", False), ("labels_path", True)])
def test_missing_snapshots_cannot_be_replaced_by_claimed_hashes(make_run, field, empty):
    from pathlib import Path

    original = make_run("original")
    imported = make_run("imported")
    if empty:
        Path(imported[field]).write_bytes(b"")
    else:
        Path(imported[field]).unlink()
    # Even a newly frozen fingerprint of missing files must not pass.
    freeze(imported)
    with pytest.raises(HTTPException) as rejected:
        _validate_protocols([original, imported])
    assert rejected.value.detail["reason"] == "missing_event_comparison_snapshots"


def test_historical_run_uses_actual_files_without_rewriting_its_original_fingerprint(make_run):
    historical = make_run("historical")
    historical.pop("dataset_version")
    historical.pop("strategy_spec")
    freeze(historical)
    stored = historical["protocol_hash"]
    imported = make_run("imported", prediction="down")
    assert _validate_protocols([historical, imported])["strict_comparable"] is True
    assert historical["protocol_hash"] == stored


def test_tampered_history_still_fails_original_integrity_check(make_run):
    from pathlib import Path

    original = make_run("original")
    imported = make_run("imported")
    path = Path(imported["events_path"])
    path.write_text(path.read_text().replace("CPI release", "Changed fact"))
    with pytest.raises(HTTPException) as rejected:
        _validate_protocols([original, imported])
    assert "imported" in rejected.value.detail["stale_protocols"]


def test_quantitative_comparison_still_requires_same_dataset_version(tmp_path):
    bars = tmp_path / "bars.csv"
    bars.write_text("timestamp,open,high,low,close,volume\n2025-01-03,1,2,1,2,10\n")
    first = {
        "id": "first", "events_path": str(bars), "labels_path": None,
        "dataset_version": "bars-v1", "engine_mode": "portfolio",
        "result_nature": "simulated_from_real_bars", "execution_spec": {},
        "strategy_spec": {"type": "quant", "kind": "buy_hold"},
        "config": {"evaluation_protocol": {}, "dataset_snapshot": {"frequency": "1d"}},
    }
    second = copy.deepcopy(first)
    second.update(id="second", dataset_version="bars-v2")
    for run in (first, second):
        freeze(run)
    assert comparison_protocol_hash_for_run(first) != comparison_protocol_hash_for_run(second)
    assert "dataset_version" in comparison_signature_for_run(first)["dataset"]
    with pytest.raises(HTTPException) as rejected:
        _validate_protocols([first, second])
    assert "dataset.dataset_version" in rejected.value.detail["comparison_protocol"]["mismatched_fields"]


@pytest.mark.parametrize("aligned", [False, True])
def test_saved_arena_replays_its_original_event_comparison_version(make_run, monkeypatch, aligned):
    from app.event_backtest.protocol import COMPARISON_PROTOCOL_VERSION, ALIGNED_COMPARISON_PROTOCOL_VERSION
    from app.routes import arena

    first = make_run("first")
    second = make_run("second", prediction="down")
    # A formal Arena created under v1 required identical dataset versions.
    second["dataset_version"] = first["dataset_version"]
    freeze(second)
    version = ALIGNED_COMPARISON_PROTOCOL_VERSION if aligned else COMPARISON_PROTOCOL_VERSION
    family = "performance" if aligned else "prediction"
    alignment = {"mode": "intersection", "frequency": "week"} if aligned else None
    old_protocol = _validate_protocols(
        [first, second], arena_type=family, time_alignment=alignment,
        event_comparison_version=version,
    )
    old_ranking = {"leaderboard": [{"run_id": "first", "rank": 1}, {"run_id": "second", "rank": 2}]}
    saved = {
        "id": "saved-arena", "run_ids": ["first", "second"], "status": "done",
        "arena_type": family, "protocol_hash": old_protocol["comparison_protocol_hash"],
        "config": {"comparison_protocol": old_protocol, "selected_metric_ids": ["acc_t3_strict"],
                   "time_alignment": alignment},
        "result": {**copy.deepcopy(old_ranking), "comparison_protocol": old_protocol},
    }
    original_saved = copy.deepcopy(saved)
    writes = []
    monkeypatch.setattr(arena.db, "get_bt_arena", lambda arena_id: saved)
    monkeypatch.setattr(arena.db, "get_bt_run", lambda run_id: {"first": first, "second": second}[run_id])
    monkeypatch.setattr(arena.db, "update_bt_arena_status", lambda *args, **kwargs: writes.append((args, kwargs)))
    monkeypatch.setattr(arena.arena_engine, "build_run_contexts", lambda runs: [])
    monkeypatch.setattr(arena.arena_engine, "resolve_metric_selection", lambda *args, **kwargs: ["acc_t3_strict"])
    monkeypatch.setattr(arena.arena_engine, "compute_arena_result", lambda *args, **kwargs: copy.deepcopy(old_ranking))
    arena.compute_arena_and_save("saved-arena")
    computed = writes[-1][1]["result"]
    assert computed["leaderboard"] == old_ranking["leaderboard"]
    assert computed["comparison_protocol"]["comparison_protocol_hash"] == saved["protocol_hash"]
    assert all(signature["version"] == version for signature in computed["comparison_protocol"]["comparison_signatures"].values())
    assert saved == original_saved
