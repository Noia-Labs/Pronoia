from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.event_backtest import arena_workspace_execution as execution
from app.event_backtest.cancellation import BacktestCancelled
from app.event_backtest.models import EventRecord, TeamPrediction
from app.event_backtest.model_comparison_adapters import describe_run
from app.quant_backtest.models import Bar, MarketDataset, Signal


def market(n=40, *, start=dt.date(2025, 1, 1)):
    return MarketDataset(name="fixture", symbol="600000.SH", market="CN", frequency="1d", bars=tuple(
        Bar(timestamp=(start + dt.timedelta(days=i)).isoformat(), open=100 + i, high=102 + i,
            low=99 + i, close=101 + i, volume=100, fields={"future_label": "do-not-expose"})
        for i in range(n)
    ), metadata={"secret_future": "do-not-expose"})


def event(day="2025-01-25T09:00:00+08:00"):
    return EventRecord(event_id="ev1", symbol="600000.SH", market="CN", event_time=day,
                       title="fixture", event_text="Only available facts", event_type_l2="announcement",
                       source_url="https://example.test/facts", analysis_direction="up", direction_prior="up",
                       analysis_expected_return_pct=99)


def model(kind="external_model", **extra):
    return {"id": "m1", "name": "fixture model", "kind": kind,
            "profile_snapshot": {"model_id": "qwen", "base_url": "https://example.test/v1"}, **extra}


def prediction(ev, *, forecast=2.0, abstain=False):
    return TeamPrediction(event_id=ev.event_id, pred_direction="up", run_id="test",
                          expected_return_pct=forecast, horizon="t3", abstain=abstain)


def run(model_value=None, data=None, ev=None, settings=None, cancel=None, progress=None):
    return asyncio.run(execution.evaluate_model(model_value or model(), data or market(), ev,
                       settings or {}, cancel or threading.Event(), progress))


def test_raw_event_same_facts_no_provided_predictions(monkeypatch):
    seen = []
    async def raw(events, **kwargs):
        seen.append((events[0], kwargs))
        return [prediction(events[0])]
    monkeypatch.setattr(execution, "run_raw_model", raw)
    result = run(ev=event())
    supplied, kwargs = seen[0]
    assert supplied.event_text == "Only available facts"
    assert supplied.analysis_direction is None and supplied.direction_prior is None
    assert supplied.analysis_expected_return_pct is None
    assert kwargs["profile"] == model()["profile_snapshot"]
    assert result["observations"][0] == {
        "bar_index": 24, "direction": "up", "expected_return_pct": 2.0, "horizon_bars": 3,
        "direction_basis": "market_excess", "source": {"kind": "arena_prediction", "event_id": "ev1"}}


def test_market_observations_are_asof_and_do_not_fake_news(monkeypatch):
    seen = []
    async def raw(events, **kwargs):
        ev = events[0]
        packet = json.loads(ev.event_text.split("\n", 1)[1])
        seen.append(packet)
        assert "不是新闻事件" in ev.event_text
        assert "do-not-expose" not in ev.event_text
        assert ev.available_time.endswith("07:00:00+00:00")
        return [prediction(ev)]
    monkeypatch.setattr(execution, "run_raw_model", raw)
    data = market(26)
    result = run(data=data)
    assert [o["bar_index"] for o in result["observations"]] == [19, 22]
    assert [p["bars"][-1]["timestamp"] for p in seen] == [data.bars[19].timestamp, data.bars[22].timestamp]
    assert all(len(p["bars"]) == 20 for p in seen)


def test_earlier_history_warms_models_but_observations_use_selected_indices(monkeypatch):
    history = market(40)
    selected = replace(history, bars=history.bars[30:], fingerprint="")
    seen = []
    async def raw(events, **kwargs):
        seen.append(json.loads(events[0].event_text.split("\n", 1)[1]))
        return [prediction(events[0])]
    monkeypatch.setattr(execution, "run_raw_model", raw)
    result = run(data=selected, settings={"history_dataset": history})
    assert [o["bar_index"] for o in result["observations"]] == [0, 3, 6]
    assert seen[0]["bars"][-1]["timestamp"] == selected.bars[0].timestamp
    assert seen[0]["bars"][0]["timestamp"] == history.bars[11].timestamp
    assert execution.estimate_model_work(model(), selected, None, {"history_dataset": history}) == 3


def test_more_than_100_points_rejected_before_calls(monkeypatch):
    raw = AsyncMock()
    monkeypatch.setattr(execution, "run_raw_model", raw)
    with pytest.raises(ValueError, match="超过.*100"):
        run(data=market(400))
    raw.assert_not_called()


@pytest.mark.parametrize("settings", [{"holding_bars": 2}, {"holding_bars": float("nan")}, {"holding_bars": True}])
def test_bad_horizon_rejected_before_calls(monkeypatch, settings):
    raw = AsyncMock()
    monkeypatch.setattr(execution, "run_raw_model", raw)
    with pytest.raises(ValueError):
        run(settings=settings)
    raw.assert_not_called()


def test_minute_llm_rejected_instead_of_wrong_horizon():
    data = MarketDataset(name="1m", symbol="600000.SH", market="CN", frequency="1m", bars=tuple(
        Bar(f"2025-01-01T10:{i:02d}:00+08:00", 10, 11, 9, 10) for i in range(30)))
    with pytest.raises(ValueError, match="日 K"):
        run(data=data)


def test_partial_failure_keeps_success_and_missing_not_zero(monkeypatch):
    count = 0
    async def raw(events, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise RuntimeError("secret-like content must not be copied")
        return [prediction(events[0], forecast=None)]
    monkeypatch.setattr(execution, "run_raw_model", raw)
    result = run(data=market(26))
    assert len(result["observations"]) == 1
    assert result["observations"][0]["bar_index"] == 22
    assert result["observations"][0]["expected_return_pct"] is None
    assert result["failed_prediction_count"] == 1
    assert "secret-like" not in json.dumps(result)


def test_all_abstentions_raise_instead_of_zero_score(monkeypatch):
    async def raw(events, **kwargs):
        return [prediction(events[0], abstain=True)]
    monkeypatch.setattr(execution, "run_raw_model", raw)
    with pytest.raises(ValueError, match="没有生成.*有效预测"):
        run(ev=event())


def test_pronoia_pins_snapshot_and_fixed_team_without_global_mutation(monkeypatch, tmp_path):
    selected = model("pronoia")
    original = copy.deepcopy(selected)
    context_states = []
    @contextmanager
    def profile_context(profile):
        context_states.append(profile)
        yield
        context_states.append("restored")
    async def team(events, **kwargs):
        assert context_states == [selected["profile_snapshot"]]
        assert kwargs["system_prompt_variant"] == "v0"
        assert kwargs["target_horizon"] == "t3"
        assert kwargs["concurrency"] == 1
        return [prediction(events[0])]
    monkeypatch.setattr(execution, "model_profile_context", profile_context)
    monkeypatch.setattr(execution, "run_team_full_trajectory", team)
    monkeypatch.setattr(execution.config, "DATA_DIR", str(tmp_path))
    result = run(selected, ev=event())
    assert context_states[-1] == "restored" and selected == original
    assert result["model_kind"] == "pronoia"


def test_cancel_before_model_calls(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    raw = AsyncMock()
    monkeypatch.setattr(execution, "run_raw_model", raw)
    with pytest.raises(BacktestCancelled):
        run(ev=event(), cancel=cancel)
    raw.assert_not_called()


def test_cancel_inflight_stops_next_request(monkeypatch):
    cancel = threading.Event()
    calls = []
    async def raw(events, **kwargs):
        calls.append(events[0].event_id)
        cancel.set()
        await asyncio.sleep(10)
    monkeypatch.setattr(execution, "run_raw_model", raw)
    with pytest.raises(BacktestCancelled):
        run(cancel=cancel)
    assert len(calls) == 1


def test_quant_event_excludes_later_same_day_close(monkeypatch):
    data = market()
    seen = []
    class Strategy:
        def generate(self, history):
            seen.append(history)
            return [Signal(history.bars[-1].timestamp, 0.0, expected_return_pct=1.5, horizon_bars=3)]
    monkeypatch.setattr(execution, "strategy_from_spec", lambda spec: Strategy())
    result = run(model("quant", strategy_spec={"kind": "return_forecast"}), data=data, ev=event())
    assert seen[0].bars[-1].timestamp == "2025-01-24"
    assert all(not bar.fields for bar in seen[0].bars)
    obs = result["observations"][0]
    assert obs["bar_index"] == 24 and obs["horizon_bars"] is None
    assert obs["expected_return_pct"] == 1.5
    assert result["warnings"]


def test_quant_event_uses_preselection_history(monkeypatch):
    history = market()
    selected = replace(history, bars=history.bars[24:], fingerprint="")
    class Strategy:
        def generate(self, data):
            assert data.bars[-1].timestamp == "2025-01-24"
            return [Signal(data.bars[-1].timestamp, 1.0)]
    monkeypatch.setattr(execution, "strategy_from_spec", lambda spec: Strategy())
    result = run(model("quant", strategy_spec={"kind": "buy_hold"}), data=selected, ev=event(),
                 settings={"history_dataset": history})
    assert result["observations"][0]["bar_index"] == 0


def test_buy_hold_continues_holding_without_fabricated_forecasts():
    result = run(model("quant", strategy_spec={"kind": "buy_hold"}), data=market(10))
    assert len(result["observations"]) == 9
    assert all(o["direction"] == "up" and o["expected_return_pct"] is None for o in result["observations"])


def test_quant_forecast_warmup_not_neutral_and_horizon_frozen():
    value = model("quant", strategy_spec={"kind": "return_forecast", "source": "builtin",
                  "parameters": {"lookback": 2, "horizon_bars": 1}})
    original = copy.deepcopy(value)
    result = run(value, data=market(10))
    assert result["observations"][0]["bar_index"] == 4
    assert all(o["horizon_bars"] == 3 for o in result["observations"])
    assert value == original


def test_remote_forecast_only_calls_current_point(monkeypatch):
    calls = []
    class Strategy:
        horizon_bars, lookback, parameters = 3, 20, {}
        def _post(self, payload):
            calls.append(payload)
            return {"expected_return_pct": 1.0, "horizon_bars": 3}
        def generate(self, data):
            raise AssertionError("must not resubmit every earlier date")
    monkeypatch.setattr(execution, "strategy_from_spec", lambda spec: Strategy())
    value = model("quant", strategy_spec={"kind": "return_forecast", "source": "external_http"})
    result = run(value, data=market(26))
    assert len(calls) == 2
    assert [p["as_of"] for p in calls] == ["2025-01-20", "2025-01-23"]
    assert all(p["bars"][-1]["timestamp"] == p["as_of"] for p in calls)
    assert [o["bar_index"] for o in result["observations"]] == [19, 22]


def test_imported_predictions_use_saved_target_without_model_calls(monkeypatch):
    saved = {"id": "saved", "status": "done", "visibility": "private"}
    target = {"kind": "event", "key": "event:fixture", "market": "CN", "symbol": "600000.SH"}
    loader = Mock(return_value={"observations": [{"bar_index": 24, "direction": "up"}], "warnings": ["saved only"]})
    monkeypatch.setattr(execution.db, "get_bt_run", lambda rid: saved)
    monkeypatch.setattr(execution, "load_contestant", loader)
    raw = AsyncMock()
    monkeypatch.setattr(execution, "run_raw_model", raw)
    data = market()
    result = run(model("imported_predictions", source_run_id="saved"), data=data, ev=event(), settings={"target": target})
    assert loader.call_count == 2  # read-only preflight, then the actual frozen execution
    loader.assert_called_with(saved, target, data)
    raw.assert_not_called()
    assert result["warnings"] == ["saved only"]


def test_external_event_service_receives_same_selected_event(monkeypatch):
    received = []
    def external(events, **kwargs):
        received.append((events, kwargs))
        return [prediction(events[0])]
    monkeypatch.setattr(execution, "run_external_event_strategy", external)
    spec = {"type": "event", "adapter": "external_http", "endpoint": "https://example.test/predict"}
    result = run(model("external_service", strategy_spec=spec), ev=event())
    assert len(received) == 1 and received[0][0][0].event_id == "ev1"
    assert received[0][1]["spec"] == spec
    assert result["observations"][0]["expected_return_pct"] == 2.0


def test_history_price_mismatch_rejected_before_call(monkeypatch):
    history = market()
    selected = replace(history, bars=history.bars[24:], fingerprint="")
    changed = replace(selected.bars[0], close=selected.bars[0].close + 0.5)
    selected = replace(selected, bars=(changed, *selected.bars[1:]), fingerprint="")
    raw = AsyncMock()
    monkeypatch.setattr(execution, "run_raw_model", raw)
    with pytest.raises(ValueError, match="K 线不一致"):
        run(data=selected, ev=event(), settings={"history_dataset": history})
    raw.assert_not_called()


def test_event_outside_selected_window_rejected_without_call(monkeypatch):
    history = market()
    selected = replace(history, bars=history.bars[30:], fingerprint="")
    raw = AsyncMock()
    monkeypatch.setattr(execution, "run_raw_model", raw)
    with pytest.raises(ValueError, match="事件发生时间不在"):
        run(data=selected, ev=event(), settings={"history_dataset": history})
    raw.assert_not_called()


def test_invalid_minus_100_forecast_is_missing_not_replay_failure(monkeypatch):
    calls = 0
    async def raw(events, **kwargs):
        nonlocal calls
        calls += 1
        return [prediction(events[0], forecast=-110 if calls == 1 else 2)]
    monkeypatch.setattr(execution, "run_raw_model", raw)
    result = run(data=market(26))
    assert len(result["observations"]) == 1
    assert result["failed_prediction_count"] == 1


def test_remote_quant_partial_failure_records_audit(monkeypatch):
    class Strategy:
        horizon_bars, lookback, parameters = 3, 20, {}
        calls = 0
        def _post(self, payload):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("secret-like content")
            return {"expected_return_pct": 1, "horizon_bars": 3}
    monkeypatch.setattr(execution, "strategy_from_spec", lambda spec: Strategy())
    result = run(model("quant", strategy_spec={"kind": "return_forecast", "source": "external_http"}), data=market(26))
    assert result["failed_prediction_count"] == 1 and len(result["observations"]) == 1
    assert result["predictions"][0]["abstain"] is True
    assert "secret-like" not in json.dumps(result)


def test_remote_weight_api_never_retroactively_accepts_old_timestamps(monkeypatch):
    prefixes = []
    class Strategy:
        def generate(self, dataset):
            prefixes.append(dataset)
            return [Signal(dataset.bars[0].timestamp, 1), Signal(dataset.bars[-1].timestamp, -1)]
    monkeypatch.setattr(execution, "strategy_from_spec", lambda spec: Strategy())
    result = run(model("external_service", strategy_spec={"type": "api", "kind": "external_http"}), data=market(26))
    assert [len(p.bars) for p in prefixes] == [20, 23]
    assert [o["bar_index"] for o in result["observations"]] == [19, 22]
    assert all(o["direction"] == "down" for o in result["observations"])
    assert all(not bar.fields for p in prefixes for bar in p.bars)


def test_imported_quant_does_not_reassign_signal_to_another_asset(monkeypatch):
    class Strategy:
        def generate(self, dataset):
            raise AssertionError("must reject mismatched asset first")
    monkeypatch.setattr(execution, "strategy_from_spec", lambda spec: Strategy())
    monkeypatch.setattr(execution.db, "get_bt_run", lambda rid: {"id": rid})
    monkeypatch.setattr(execution, "get_run_market", lambda run: replace(market(), symbol="600519.SH"))
    with pytest.raises(ValueError, match="属于其他标的"):
        run(model("quant", source_run_id="saved", strategy_spec={"kind": "signal_file"}))


def test_preflight_rejects_quant_without_required_lookback():
    value = model("quant", strategy_spec={"kind": "ma_cross", "parameters": {"short_window": 20, "long_window": 60}})
    with pytest.raises(ValueError, match="历史不足"):
        execution.estimate_model_work(value, market(), event("2025-01-04T09:00:00+08:00"), {})


def test_preflight_rejects_import_without_matching_saved_predictions(monkeypatch):
    monkeypatch.setattr(execution.db, "get_bt_run", lambda rid: {"id": rid, "status": "done", "visibility": "private"})
    def missing(*args):
        raise ValueError("该模型在共同区间内没有有效预测")
    monkeypatch.setattr(execution, "load_contestant", missing)
    value = model("imported_predictions", source_run_id="saved")
    with pytest.raises(ValueError, match="没有有效预测"):
        execution.estimate_model_work(value, market(), None, {})


def saved_import(root: Path, ev: EventRecord, *, value=2.0, updated="2025-02-01T10:00:00+08:00", **updates):
    root.mkdir()
    events_path, out_path = root / "events.jsonl", root / "predictions.jsonl"
    events_path.write_text(json.dumps(ev.to_dict()), encoding="utf-8")
    out_path.write_text(json.dumps(prediction(ev, forecast=value).to_dict()), encoding="utf-8")
    return {"id": root.name, "name": root.name, "status": "done", "visibility": "private",
            "runner": "provided_analysis", "engine_mode": "event_proxy", "events_path": str(events_path),
            "out_path": str(out_path), "created_at": updated, "updated_at": updated, **updates}


def import_target(source):
    return next(item for item in describe_run(source)["subjects"] if item["kind"] == "event")


def test_imported_model_selects_matching_dataset_not_latest_unrelated_event(tmp_path, monkeypatch):
    wanted = event()
    original = saved_import(tmp_path / "matching", wanted, value=1.25)
    unrelated = saved_import(tmp_path / "other_event", replace(wanted, title="Different announcement"),
                             value=99, updated="2025-03-01T10:00:00+08:00")
    sources = {item["id"]: item for item in (original, unrelated)}
    monkeypatch.setattr(execution.db, "get_bt_run", sources.get)
    raw = AsyncMock()
    monkeypatch.setattr(execution, "run_raw_model", raw)
    result = run(model("imported_predictions", source_run_id="other_event", source_run_ids=["other_event", "matching"]),
                 ev=wanted, settings={"target": import_target(original)})
    assert result["source_run_id"] == "matching"
    assert len(result["observations"]) == 1 and result["observations"][0]["expected_return_pct"] == 1.25
    assert result["run_id"] == "m1"
    raw.assert_not_called()


def test_imported_model_same_target_chooses_latest_success_without_merging(tmp_path, monkeypatch):
    old = saved_import(tmp_path / "old", event(), value=1)
    new = saved_import(tmp_path / "new", event(), value=-2, updated="2025-02-02T10:00:00+08:00")
    sources = {item["id"]: item for item in (old, new)}
    monkeypatch.setattr(execution.db, "get_bt_run", sources.get)
    result = run(model("imported_predictions", source_run_ids=["old", "new"]),
                 ev=event(), settings={"target": import_target(old)})
    assert result["source_run_id"] == "new"
    assert len(result["observations"]) == 1
    assert result["observations"][0]["expected_return_pct"] == -2


def test_imported_lineage_excludes_safe_and_unfinished_sources(tmp_path, monkeypatch):
    private = saved_import(tmp_path / "private", event(), value=1)
    safe = {**private, "id": "safe", "visibility": "arena_safe", "updated_at": "2025-04-01",
            "out_path": "/must-not-read-safe-predictions"}
    failed = {**private, "id": "failed", "status": "failed", "updated_at": "2025-05-01"}
    running = {**private, "id": "running", "status": "running", "updated_at": "2025-06-01"}
    sources = {item["id"]: item for item in (private, safe, failed, running)}
    monkeypatch.setattr(execution.db, "get_bt_run", sources.get)
    result = run(model("imported_predictions", source_run_ids=["safe", "failed", "running", "private"]),
                 ev=event(), settings={"target": import_target(private)})
    assert result["source_run_id"] == "private"


def test_imported_model_does_not_expand_frozen_lineage_to_legacy_source(tmp_path, monkeypatch):
    available_but_unrelated = saved_import(tmp_path / "outside", event())
    declared = saved_import(tmp_path / "declared", replace(event(), title="Other event"))
    sources = {item["id"]: item for item in (available_but_unrelated, declared)}
    read_ids = []
    def read(rid):
        read_ids.append(rid)
        return sources.get(rid)
    monkeypatch.setattr(execution.db, "get_bt_run", read)
    with pytest.raises(ValueError, match="没有.*有效预测"):
        run(model("imported_predictions", source_run_ids=["declared"], source_run_id="outside"),
            ev=event(), settings={"target": import_target(available_but_unrelated)})
    assert read_ids == ["declared"]


def test_imported_asset_selects_source_covering_requested_dates(tmp_path, monkeypatch):
    january = saved_import(tmp_path / "january", event(), value=1)
    march = saved_import(tmp_path / "march", event("2025-03-20T09:00:00+08:00"), value=3,
                        updated="2025-04-01T10:00:00+08:00")
    other_asset = saved_import(tmp_path / "other_asset", replace(event(), symbol="600519.SH"), value=9,
                              updated="2025-05-01T10:00:00+08:00")
    sources = {item["id"]: item for item in (january, march, other_asset)}
    monkeypatch.setattr(execution.db, "get_bt_run", sources.get)
    result = run(model("imported_predictions", source_run_ids=["other_asset", "march", "january"]))
    assert result["source_run_id"] == "january"
    assert [item["expected_return_pct"] for item in result["observations"]] == [1]


def test_imported_model_all_safe_never_reads_predictions(monkeypatch):
    monkeypatch.setattr(execution.db, "get_bt_run", lambda rid: {"id": rid, "status": "done", "visibility": "arena_safe"})
    loader = Mock()
    monkeypatch.setattr(execution, "load_contestant", loader)
    with pytest.raises(ValueError, match="仅公开安全摘要"):
        run(model("imported_predictions", source_run_ids=["safe-a", "safe-b"]))
    loader.assert_not_called()


def test_empty_lineage_retains_legacy_source_id(tmp_path, monkeypatch):
    legacy = saved_import(tmp_path / "legacy", event())
    monkeypatch.setattr(execution.db, "get_bt_run", lambda rid: legacy if rid == "legacy" else None)
    result = run(model("imported_predictions", source_run_ids=[], source_run_id="legacy"))
    assert result["source_run_id"] == "legacy"


def event_case(ev: EventRecord, *, label="down", car=-.04, asset=-.03, benchmark=.01):
    return {
        "target": {"id": f"event:{ev.event_id}", "key": f"event:{ev.event_id}",
                   "kind": "event", "market": ev.market, "symbol": ev.symbol},
        "event": ev,
        # Event sets intentionally evaluate from frozen Oracle rows and must
        # not trigger one live OHLC request per event.
        "dataset": None,
        "history_dataset": None,
        "event_truth": {"truth_basis": "market_excess", "return_unit": "decimal", "horizons": {
            "t3": {"direction": label, "asset_return": asset,
                   "benchmark_return": benchmark, "oracle_return": car,
                   "return_unit": "decimal"},
        }},
    }


def test_single_event_normalizes_to_the_same_event_lane_contract():
    ev = event()
    frozen = event_case(ev, label="up", car=.02, asset=.03, benchmark=.01)
    cases = execution.normalize_event_cases({
        "target": frozen["target"],
        "event": ev,
        "dataset": None,
        "history_dataset": None,
        "event_truth": frozen["event_truth"],
    })
    assert len(cases) == 1
    assert cases[0]["event"].event_id == ev.event_id
    assert cases[0]["label"].label_t3 == "up"
    assert cases[0]["label"].car_t3 == pytest.approx(.02)


def test_event_set_predicts_frozen_facts_without_per_event_market(monkeypatch):
    events = [event("2025-01-25T09:00:00+08:00"), replace(
        event("2025-01-27T09:00:00+08:00"), event_id="ev2", title="second",
    )]
    cases = [event_case(events[0]), event_case(events[1], label="up", car=.02, asset=.03)]
    received = []

    async def raw(items, **kwargs):
        supplied = items[0]
        received.append(supplied)
        assert supplied.analysis_direction is None and supplied.direction_prior is None
        return [TeamPrediction(event_id=supplied.event_id,
                               pred_direction="down" if supplied.event_id == "ev1" else "up",
                               confidence=.8, run_id="test", horizon="t3")]

    monkeypatch.setattr(execution, "run_raw_model", raw)
    selected = model("external_model")
    normalized = execution.normalize_event_cases({"event_cases": cases})
    assert execution.estimate_event_cases_work(selected, normalized, {"holding_bars": 3}) == 2
    result = asyncio.run(execution.evaluate_event_cases_model(
        selected, normalized, {"holding_bars": 3}, threading.Event(),
    ))
    assert [item.event_id for item in received] == ["ev1", "ev2"]
    assert [item["event_id"] for item in result["observations"]] == ["ev1", "ev2"]
    assert result["failed_prediction_count"] == 0


def test_event_set_missing_selected_oracle_horizon_fails_before_model_call(monkeypatch):
    raw = AsyncMock()
    monkeypatch.setattr(execution, "run_raw_model", raw)
    cases = execution.normalize_event_cases({"event_cases": [event_case(event())]})
    with pytest.raises(ValueError, match="T1.*标签.*Oracle CAR"):
        asyncio.run(execution.evaluate_event_cases_model(
            model("external_model"), cases, {"holding_bars": 1}, threading.Event(),
        ))
    raw.assert_not_called()


def test_event_set_partial_failure_keeps_success_without_neutral_fill(monkeypatch):
    first = event()
    second = replace(first, event_id="ev2", event_time="2025-01-27T09:00:00+08:00")
    calls = 0

    async def raw(items, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private failure detail")
        return [TeamPrediction(event_id=items[0].event_id, pred_direction="up", run_id="test", horizon="t3")]

    monkeypatch.setattr(execution, "run_raw_model", raw)
    result = asyncio.run(execution.evaluate_event_cases_model(
        model("external_model"),
        execution.normalize_event_cases({"event_cases": [event_case(first), event_case(second)]}),
        {"holding_bars": 3}, threading.Event(),
    ))
    assert len(result["observations"]) == 1
    assert result["observations"][0]["event_id"] == "ev2"
    assert result["failed_prediction_count"] == 1
    assert "private failure detail" not in json.dumps(result)


def test_event_bearish_prediction_turns_negative_oracle_return_positive():
    ev = event()
    cases = execution.normalize_event_cases({"event_cases": [event_case(ev, label="down", car=-.04)]})
    contestant = {
        "run_id": "bear", "name": "bear", "model_kind": "external_model", "warnings": [],
        "observations": [{"event_id": "ev1", "bar_index": 0, "direction": "down",
                          "horizon_bars": 3, "direction_basis": "market_excess"}],
        "predictions": [{"event_id": "ev1", "pred_direction": "down", "confidence": .9,
                         "horizon": "t3", "run_id": "bear", "abstain": False}],
    }
    result = execution.compare_event_models(cases, [contestant], {
        "holding_bars": 3, "fee_bps": 3, "slippage_bps": 2,
        "initial_capital": 100_000, "position_rule": "long_short_flat_event_proxy",
    })
    output = result["models"][0]
    detail = output["event_details"][0]
    assert detail["label"] == detail["prediction"] == "down"
    assert detail["actual_return"] == pytest.approx(-.04)
    assert detail["asset_return"] == pytest.approx(-.03)
    assert detail["directional_return"] == pytest.approx(.04)
    assert detail["net_directional_return"] == pytest.approx(.0395)
    assert detail["metrics"]["is_correct"] is True
    assert output["metrics"]["accuracy"] == 1
    assert output["metrics"]["win_rate"] == 1
    assert output["metrics"]["total_return"] == pytest.approx(.0395)
    assert output["metrics"]["max_drawdown"] == 0
    assert set(("time", "symbol", "direction", "confidence", "actual_return",
                "directional_return", "metrics")).issubset(detail)


def test_event_neutral_is_explicit_zero_return_and_no_trade_cost():
    ev = event()
    cases = execution.normalize_event_cases({"event_cases": [event_case(ev, label="neutral", car=.001)]})
    contestant = {
        "run_id": "neutral", "name": "neutral", "model_kind": "external_model", "warnings": [],
        "observations": [{"event_id": "ev1", "bar_index": 0, "direction": "neutral",
                          "horizon_bars": 3, "direction_basis": "market_excess"}],
        "predictions": [{"event_id": "ev1", "pred_direction": "neutral", "confidence": .7,
                         "horizon": "t3", "run_id": "neutral", "abstain": False}],
    }
    result = execution.compare_event_models(cases, [contestant], {
        "holding_bars": 3, "fee_bps": 10, "slippage_bps": 10, "initial_capital": 100_000,
    })
    detail = result["models"][0]["records"][0]
    assert detail["directional_return"] == detail["net_directional_return"] == 0
    assert detail["metrics"]["active_trade"] is False
    metrics = result["models"][0]["metrics"]
    assert metrics["trade_count"] == 0
    assert metrics["average_return"] == metrics["total_return"] == 0
    assert metrics["max_drawdown"] == 0
    assert metrics["win_rate"] is None
