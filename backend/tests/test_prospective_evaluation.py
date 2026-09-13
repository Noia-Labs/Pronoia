from __future__ import annotations

import datetime as dt
import asyncio
import sys

import pytest

sys.path.insert(0, "backend")

from app import config, db  # noqa: E402
from app.event_backtest.models import EventRecord  # noqa: E402
from app.event_backtest.models import TeamPrediction  # noqa: E402
from app.skills.price_data import resolve_security_ref  # noqa: E402
from app.prospective import service  # noqa: E402
from app.routes.prospective import CreateProspectiveRunRequest, create_run  # noqa: E402
from app.prospective.service import (  # noqa: E402
    _add_business_days,
    _business_dates_lookback,
    _candidate_groups,
    _event_published_before,
    _forward_return,
    _parse_iso,
    _select_candidates,
    _trading_dates_lookback,
)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "prospective.sqlite"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    db._conn = None
    db.init_db()
    yield
    if db._conn is not None:
        db._conn.close()
    db._conn = None


def test_prospective_prediction_is_immutable(isolated_db):
    run = db.create_prospective_run(name="test", capture_at="2026-01-01T00:00:00+00:00", settle_after_seconds=60)
    item = db.create_prospective_item(
        run_id=run["id"], event_id="evt-1", canonical_key="key-1",
        event={"market": "US", "symbol": "AAPL", "event_type_l2": "earnings", "event_time": "2026-01-01", "source_url": "https://example.com"},
    )
    prediction = db.add_prospective_prediction(
        item_id=item["id"], pred_direction="up", confidence=0.7, rationale="r", raw_prediction={"x": 1},
        model_version="m", prompt_version="p", adapter_version="a", git_commit="g",
        as_of_at="2026-01-01T00:00:00+00:00", evidence_hash="h",
    )
    with pytest.raises(Exception, match="immutable"):
        db._get_conn().execute("UPDATE prospective_predictions SET rationale='changed' WHERE id=?", (prediction["id"],))


def test_prospective_analysis_trace_is_append_only(isolated_db):
    run = db.create_prospective_run(name="trace", capture_at="2026-01-01T00:00:00+00:00", settle_after_days=1)
    item = db.create_prospective_item(
        run_id=run["id"], event_id="evt-trace", canonical_key="trace-key",
        event={"market": "CN", "symbol": "600519", "event_type_l2": "股份回购",
               "event_time": "2025-12-31", "source_url": "https://example.com"},
    )
    trace = db.add_prospective_analysis_trace(
        run_id=run["id"], item_id=item["id"], sequence_no=1, stage="evidence_frozen",
        stage_title="证据已冻结", as_of_at="2026-01-01T00:00:00+00:00", output_snapshot={"count": 1},
    )
    assert trace["output_snapshot"] == {"count": 1}
    with pytest.raises(Exception, match="immutable"):
        db._get_conn().execute("UPDATE prospective_analysis_trace SET stage='changed' WHERE id=?", (trace["id"],))


def test_as_of_parser_accepts_announcement_date_only():
    parsed = _parse_iso("2026-09-06")
    assert parsed.isoformat() == "2026-09-06T00:00:00+00:00"


def test_observation_period_uses_weekdays():
    friday = _parse_iso("2026-09-04T10:00:00+08:00")
    assert _add_business_days(friday, 1).date().isoformat() == "2026-09-07"
    assert _add_business_days(friday, 3).date().isoformat() == "2026-09-09"


def test_create_run_uses_one_trade_day_period_for_prediction_and_settlement(isolated_db):
    run = create_run(CreateProspectiveRunRequest(
        name="single-period", capture_at="2026-09-07T16:00:00+08:00", settle_after_days=5,
        source_config={"source": "sample"},
        metric_config={"settlement_mode": "after_trade_days", "horizon": 1, "epsilon": 0.005},
    ))
    assert run["settle_after_days"] == 5
    assert run["metric_config"]["horizon"] == 5


def test_lookback_window_skips_weekend_and_is_chronological():
    sunday = _parse_iso("2026-09-06T17:12:00+08:00")
    assert _business_dates_lookback(sunday, 3) == ["20260902", "20260903", "20260904"]


def test_strict_lookback_excludes_capture_date():
    monday = _parse_iso("2026-09-07T17:12:00+08:00")
    assert _trading_dates_lookback(monday, 3, include_current=False) == ["20260902", "20260903", "20260904"]


def test_date_only_announcement_on_capture_day_is_not_provably_as_of():
    event = EventRecord.from_dict({
        "event_id": "same-day", "market": "CN", "symbol": "600519", "event_time": "2026-09-07",
        "event_type_l2": "股份回购", "title": "回购", "event_text": "回购公告",
        "source_url": "https://example.com/same-day",
    })
    cutoff = _parse_iso("2026-09-07T17:12:00+08:00")
    assert not _event_published_before(event, cutoff, policy="before_capture_date")


def test_precise_announcement_before_cutoff_is_allowed():
    event = EventRecord.from_dict({
        "event_id": "timed", "market": "CN", "symbol": "600519",
        "event_time": "2026-09-07T09:30:00+08:00", "event_type_l2": "股份回购",
        "title": "回购", "event_text": "回购公告", "source_url": "https://example.com/timed",
    })
    cutoff = _parse_iso("2026-09-07T17:12:00+08:00")
    assert _event_published_before(event, cutoff, policy="timestamp_verified")


def test_beijing_stock_uses_bse_prefix():
    ref = resolve_security_ref("920068", "CN")
    assert ref.symbol == "bj920068"
    assert ref.exchange == "BSE"


def test_candidates_group_multiple_announcements_by_company():
    events = [
        EventRecord.from_dict({
            "event_id": "a", "market": "CN", "symbol": "600519", "event_time": "2026-09-03",
            "event_type_l2": "财报超预期/不及预期", "title": "业绩预告", "event_text": "600519 贵州茅台 · 业绩预告",
            "source_url": "https://example.com/a",
        }),
        EventRecord.from_dict({
            "event_id": "b", "market": "CN", "symbol": "600519", "event_time": "2026-09-04",
            "event_type_l2": "公司指引上调/下调", "title": "经营指引", "event_text": "600519 贵州茅台 · 经营指引",
            "source_url": "https://example.com/b",
        }),
    ]
    candidates = _candidate_groups(events, "2026-09-07T17:00:00+08:00")
    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "600519"
    assert candidates[0]["event_count"] == 2
    assert len(candidates[0]["evidence"]) == 2


def test_forward_return_uses_freeze_close_not_announcement_date():
    import pandas as pd

    closes = pd.Series([100.0, 110.0, 120.0], index=[
        dt.date(2026, 9, 3), dt.date(2026, 9, 4), dt.date(2026, 9, 7),
    ])
    value, base, target = _forward_return(closes, "2026-09-04T14:00:00+08:00", "CN", 1)
    assert value == pytest.approx((110 / 100) - 1)
    assert base == "2026-09-03"
    assert target == "2026-09-04"


def test_selector_falls_back_deterministically_when_model_returns_no_ids(monkeypatch):
    async def no_selection(*args, **kwargs):
        return {"selected": []}

    monkeypatch.setattr("app.prospective.service.complete_json", no_selection)
    candidates = [
        {"id": "c1", "market": "CN", "symbol": "600519", "company_name": "A", "event_count": 2,
         "latest_event_at": "2026-09-04", "event_type_l2": "财报", "title": "a"},
        {"id": "c2", "market": "CN", "symbol": "000001", "company_name": "B", "event_count": 1,
         "latest_event_at": "2026-09-03", "event_type_l2": "并购", "title": "b"},
    ]
    selected, decisions = asyncio.run(_select_candidates(candidates, 1, {}))
    assert [row["id"] for row in selected] == ["c1"]
    assert "回退选择" in decisions["c1"]["reason"]


def test_capture_uses_separate_candidate_and_analysis_windows_and_records_trace(isolated_db, monkeypatch):
    event_recent = EventRecord.from_dict({
        "event_id": "recent", "market": "CN", "symbol": "600519", "event_time": "2026-09-04",
        "event_type_l2": "股份回购", "title": "近期回购", "event_text": "600519 贵州茅台 · 近期回购",
        "source_url": "https://example.com/recent",
    })
    event_older = EventRecord.from_dict({
        "event_id": "older", "market": "CN", "symbol": "600519", "event_time": "2026-08-31",
        "event_type_l2": "股份回购", "title": "历史回购", "event_text": "600519 贵州茅台 · 历史回购",
        "source_url": "https://example.com/older",
    })
    windows = []
    prediction_horizons = []

    def fake_discover(source_config, **kwargs):
        windows.append(source_config["lookback_trade_days"])
        return [event_recent] if source_config["lookback_trade_days"] == 3 else [event_recent, event_older]

    async def fake_select(candidates, count, predictor_config):
        return candidates[:1], {candidates[0]["id"]: {"score": 0.9, "reason": "信息明确", "model_version": "selector", "prompt_version": "selector-v1"}}

    async def fake_predict(events, **kwargs):
        prediction_horizons.append(kwargs["target_horizon"])
        event = list(events)[0]
        return [TeamPrediction(event_id=event.event_id, pred_direction="up", run_id=kwargs["run_id"],
                               model_version="model", confidence=0.7, rationale="基于冻结证据")]

    frozen_now = _parse_iso("2026-09-07T17:12:00+08:00")
    monkeypatch.setattr(service, "_now", lambda: frozen_now)
    monkeypatch.setattr(service, "discover_events", fake_discover)
    monkeypatch.setattr(service, "_select_candidates", fake_select)
    monkeypatch.setattr(service, "run_team_prompt", fake_predict)
    monkeypatch.setattr(service, "_as_of_market_features", lambda *args: {"asset": {}, "benchmark": {}, "car": {}, "data_quality": {}})
    run = db.create_prospective_run(
        name="dual-window", capture_at=frozen_now.isoformat(), settle_after_days=1,
        source_config={"source": "cn_announcements", "candidate_lookback_trade_days": 3,
                       "analysis_lookback_trade_days": 20, "selection_count": 1,
                       "evidence_cutoff_policy": "before_capture_date"},
        metric_config={"horizon": 1},
    )
    asyncio.run(service.capture_run(run["id"]))
    detail = service.detail(run["id"])
    assert windows == [3, 20]
    assert prediction_horizons == [1]
    assert len(detail["items"]) == 1
    assert len(detail["items"][0]["evidence"]) == 2
    assert [row["stage"] for row in detail["items"][0]["trace"]] == [
        "candidate_discovered", "evidence_hydrated", "evidence_frozen",
        "market_features_computed", "model_input_frozen", "prediction_generated",
    ]


def test_settle_before_result_available_returns_pending_without_duplicate_attempts(isolated_db, monkeypatch):
    run = db.create_prospective_run(
        name="early-settle", capture_at="2026-09-07T16:17:00+08:00", settle_after_days=1,
        metric_config={"horizon": 1, "epsilon": 0.005},
    )
    db.update_prospective_run(run["id"], status="waiting", target_settle_at="2026-09-08T16:17:00+08:00",
                              settle_at="2026-09-08T16:17:00+08:00")
    item = db.create_prospective_item(
        run_id=run["id"], event_id="early-item", canonical_key="early-key",
        event={"market": "CN", "symbol": "600519", "event_type_l2": "股份回购",
               "event_time": "2026-09-07T16:17:00+08:00", "source_url": "https://example.com"},
        captured_at="2026-09-07T16:17:00+08:00", settle_at="2026-09-08T16:17:00+08:00",
    )
    db.add_prospective_prediction(
        item_id=item["id"], pred_direction="up", confidence=0.7, rationale="r", raw_prediction={},
        model_version="m", prompt_version="p", adapter_version="a", git_commit="g",
        as_of_at="2026-09-07T16:17:00+08:00", evidence_hash="h",
    )
    db.update_prospective_item(item["id"], status="frozen")
    monkeypatch.setattr(service, "_now", lambda: _parse_iso("2026-09-08T14:00:00+08:00"))
    first = service.settle_run(run["id"], force=True)
    second = service.settle_run(run["id"], force=True)
    assert first["status"] == "waiting"
    assert first["settlement_summary"]["settled"] == 0
    assert "尚未收盘" in first["settlement_summary"]["message"]
    assert len(db.list_prospective_settlements(item["id"])) == 0
    assert second["next_check_at"] == first["next_check_at"]


def test_settle_uses_shared_price_route_and_is_idempotent_after_resolution(isolated_db, monkeypatch):
    import pandas as pd

    run = db.create_prospective_run(
        name="ready-settle", capture_at="2026-09-07T16:17:00+08:00", settle_after_days=1,
        metric_config={"horizon": 1, "epsilon": 0.005},
    )
    db.update_prospective_run(run["id"], status="waiting", target_settle_at="2026-09-08T16:17:00+08:00",
                              settle_at="2026-09-08T16:17:00+08:00")
    item = db.create_prospective_item(
        run_id=run["id"], event_id="ready-item", canonical_key="ready-key",
        event={"market": "CN", "symbol": "920068", "event_type_l2": "股份回购",
               "event_time": "2026-09-07T16:17:00+08:00", "source_url": "https://example.com"},
        captured_at="2026-09-07T16:17:00+08:00", settle_at="2026-09-08T16:17:00+08:00",
    )
    db.add_prospective_prediction(
        item_id=item["id"], pred_direction="up", confidence=0.7, rationale="r", raw_prediction={},
        model_version="m", prompt_version="p", adapter_version="a", git_commit="g",
        as_of_at="2026-09-07T16:17:00+08:00", evidence_hash="h",
    )
    db.update_prospective_item(item["id"], status="frozen")
    dates = [dt.date(2026, 9, 7), dt.date(2026, 9, 8)]
    asset = pd.Series([100.0, 110.0], index=dates)
    benchmark = pd.Series([100.0, 105.0], index=dates)
    calls = []
    monkeypatch.setattr(service, "_now", lambda: _parse_iso("2026-09-08T16:00:00+08:00"))
    monkeypatch.setattr(service, "_fetch_closes_with_status", lambda item, end: (calls.append(item["symbol"]) or (asset, benchmark, {})))
    result = service.settle_run(run["id"], force=True)
    again = service.settle_run(run["id"], force=True)
    assert calls == ["920068"]
    assert result["status"] == "completed"
    assert result["settlement_summary"]["settled"] == 1
    assert result["settlement_summary"]["accuracy"] == 1.0
    assert again["status"] == "completed"
    assert len(db.list_prospective_settlements(item["id"])) == 1
