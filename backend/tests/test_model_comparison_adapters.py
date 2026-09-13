"""Common-replay adapters cannot use future quant closes or Oracle returns."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app.event_backtest.model_comparison_adapters import (
    asset_identity, bar_close_times, describe_run, get_event_record, get_run_market, load_contestant,
)
from app.event_backtest.model_comparison_replay import compare_on_market
from app.quant_backtest.models import Bar, MarketDataset


def _market(*, symbol="600000.SH", market="CN", frequency="1d", stamps=None, metadata=None):
    stamps = stamps or ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"]
    return MarketDataset(name="Frozen prices", symbol=symbol, market=market, frequency=frequency,
                         bars=tuple(Bar(stamp, 100 + index, 105 + index, 99 + index, 102 + index) for index, stamp in enumerate(stamps)),
                         metadata=metadata or {})


def _event(event_id="one", **updates):
    return {"event_id": event_id, "market": "CN", "symbol": "600000", "event_time": "2026-09-02T09:00:00+08:00",
            "available_time": "2026-09-02T10:00:00+08:00", "title": "订单披露", "event_text": "新增订单同比增长 12%。",
            "source_url": "https://example.invalid/report", "event_type_l2": "业绩公告", "event_facts": {"actual_value": 12},
            **updates}


def _event_run(root: Path, rows=None, predictions=None, **updates):
    rows = rows or [_event()]
    predictions = predictions if predictions is not None else [{"event_id": row["event_id"], "pred_direction": "up", "horizon": "t3",
                                                               "expected_return_pct": 2, "confidence": .7, "rationale": "prediction"} for row in rows]
    root.mkdir(parents=True, exist_ok=True)
    events_path, out_path = root / "events.jsonl", root / "predictions.jsonl"
    events_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    out_path.write_text("\n".join(json.dumps(row) for row in predictions), encoding="utf-8")
    return {"id": root.name, "name": "Pronoia test", "engine_mode": "event_proxy", "runner": "team_full", "status": "done",
            "events_path": str(events_path), "out_path": str(out_path), **updates}


def _quant_run(root: Path, *, dataset=None, signals=None, prediction_only=True):
    dataset = dataset or _market(metadata={"dataset_version": "version-1"})
    signals = signals if signals is not None else [{"timestamp": dataset.bars[0].timestamp, "target_weight": 0,
                                                   "expected_return_pct": 3, "horizon_bars": 3}]
    result = {"dataset": dataset.summary(), "bars": [bar.to_dict() for bar in dataset.bars], "signals": signals,
              "strategy": {"kind": "return_forecast" if prediction_only else "declarative_rules", "prediction_only": prediction_only},
              "contract": {"dataset_version": "version-1"},
              "equity_curve": [{"net_value": 987654321}], "return_forecasts": [{"actual_return_pct": 987654321}]}
    root.mkdir(parents=True, exist_ok=True)
    path = root / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return {"id": root.name, "name": "Quant test", "engine_mode": "portfolio", "status": "done",
            "dataset_version": "version-1", "result_path": str(path)}


def _event_target(run):
    return next(subject for subject in describe_run(run)["subjects"] if subject["kind"] == "event")


def _asset_target(symbol="600000.SH", market="CN"):
    return {"key": f"asset:{market}:{symbol}", "kind": "asset", "symbol": symbol, "market": market}


@pytest.mark.parametrize("value", ["600000", "SH600000", "sh.600000", "600000.SH", "600000.SS"])
def test_common_cn_stock_aliases_match(value):
    assert asset_identity("CN", value) == ("CN", "600000.SH")


def test_market_and_exchange_ambiguity_is_not_erased():
    assert asset_identity("CN", "000300.SH") != asset_identity("CN", "000300.SZ")
    assert asset_identity("CN", "000300") not in {asset_identity("CN", "000300.SH"), asset_identity("CN", "000300.SZ")}
    assert asset_identity("CN", "SH000001") != asset_identity("CN", "SZ000001")
    assert asset_identity("US", "AAPL") != asset_identity("HK", "AAPL")
    assert asset_identity("HK", "700") == asset_identity("HK", "00700.HK")
    assert asset_identity("FUTURES", "IF2506") != asset_identity("FUTURES", "IF0")


def test_event_identity_ignores_prediction_and_id_but_keeps_facts(tmp_path):
    original = _event_run(tmp_path / "original")
    imported = _event_run(tmp_path / "imported", [_event("different", symbol="SH600000", analysis_direction="down",
                                                        analysis_confidence="not a number", analysis_expected_return_pct=-99)])
    left, right = _event_target(original), _event_target(imported)
    assert left["key"] == right["key"] and left["key"].startswith("event:")
    assert left["available_time"] == "2026-09-02T02:00:00+00:00"
    assert get_event_record(imported, right["key"]).event_id == "different"
    assert get_event_record(imported, right["key"]).analysis_direction is None
    for key, value in (("title", "Different title"), ("event_text", "Different fact"),
                       ("available_time", "2026-09-02T11:00:00+08:00"), ("event_time", "2026-09-01T09:00:00+08:00"),
                       ("event_facts", {"actual_value": 13}), ("symbol", "600001")):
        other = _event_run(tmp_path / key, [_event(**{key: value})])
        assert _event_target(other)["key"] != left["key"]


def test_event_catalog_has_event_and_asset_and_respects_run_window(tmp_path):
    run = _event_run(tmp_path, [_event("a"), _event("b", event_time="2026-09-04T09:00:00+08:00", available_time="2026-09-04T10:00:00+08:00")],
                     execution_spec={"start_date": "2026-09-02", "end_date": "2026-09-02"},
                     config={"model_profile_snapshot": {"api_key": "must-never-be-returned", "base_url": "https://secret.invalid"}})
    result = describe_run(run)
    assert len(result["subjects"]) == 2
    assert "must-never" not in json.dumps(result) and "secret.invalid" not in json.dumps(result)


def test_quant_market_reads_real_bars_and_binds_version_and_fingerprint(tmp_path):
    run = _quant_run(tmp_path)
    market = get_run_market(run)
    assert market.bars[0].open == 100
    assert describe_run(run)["market_source"]["frequency"] == "1d"
    with pytest.raises(ValueError, match="版本"):
        get_run_market({**run, "dataset_version": "wrong-version"})
    path = Path(run["result_path"])
    result = json.loads(path.read_text())
    result["bars"][0]["open"] = 101
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="指纹"):
        get_run_market(run)


def test_event_maps_available_time_to_close_then_next_open(tmp_path):
    run = _event_run(tmp_path)
    result = load_contestant(run, _event_target(run), _market())
    row = result["observations"][0]
    assert row["bar_index"] == 1 and row["direction_basis"] == "market_excess"
    assert row["expected_return_pct"] == 2 and row["horizon_bars"] == 3
    assert row["source"]["available_at"] == "2026-09-02T02:00:00+00:00"


@pytest.mark.parametrize("timestamp,index", [("2026-09-02T16:00:00+08:00", 2), ("2026-09-05T09:00:00+08:00", 4), ("2026-09-02", 2)])
def test_after_close_weekend_and_date_only_are_conservative(tmp_path, timestamp, index):
    run = _event_run(tmp_path, [_event(available_time=timestamp)])
    result = load_contestant(run, _event_target(run), _market())
    assert result["observations"][0]["bar_index"] == index


def test_minute_closed_bar_mapping_does_not_round_backward(tmp_path):
    market = _market(frequency="1m", stamps=["2026-09-02T10:00:00+08:00", "2026-09-02T10:01:00+08:00", "2026-09-02T10:02:00+08:00"])
    run = _event_run(tmp_path, [_event(available_time="2026-09-02T10:00:30+08:00")])
    result = load_contestant(run, _event_target(run), market)
    assert result["observations"][0]["bar_index"] == 1
    assert result["observations"][0]["horizon_bars"] is None
    assert any("日线窗口" in text for text in result["warnings"])


def test_us_timezone_and_dst_are_not_browser_timezone():
    summer = _market(symbol="AAPL", market="US", stamps=["2026-06-01", "2026-06-02"])
    winter = _market(symbol="AAPL", market="US", stamps=["2026-01-05", "2026-01-06"])
    assert bar_close_times(summer)[0].hour == 20
    assert bar_close_times(winter)[0].hour == 21
    with pytest.raises(ValueError, match="夏令时"):
        bar_close_times(_market(symbol="AAPL", market="US", frequency="1m", stamps=["2026-11-01T01:30:00", "2026-11-01T01:31:00"]))


def test_quant_event_comparison_uses_latest_known_forecast_not_same_day_close(tmp_path):
    event_run = _event_run(tmp_path / "event")
    quant = _quant_run(tmp_path / "quant", signals=[
        {"timestamp": "2026-09-01", "expected_return_pct": -2, "horizon_bars": 3, "target_weight": 0},
        {"timestamp": "2026-09-02", "expected_return_pct": 99, "horizon_bars": 3, "target_weight": 0},
    ])
    result = load_contestant(quant, _event_target(event_run), _market())
    row = result["observations"][0]
    assert row["bar_index"] == 1 and row["expected_return_pct"] == -2 and row["direction"] == "down"
    assert row["source"]["original_available_at"] == "2026-09-01T07:00:00+00:00"
    assert row["horizon_bars"] is None  # old numerical forecast did not start at the event's close
    assert any("起算时点" in text for text in result["warnings"])


def test_expired_quant_forecast_is_not_carried_indefinitely(tmp_path):
    event_run = _event_run(tmp_path / "event", [_event(available_time="2026-09-07T10:00:00+08:00")])
    quant = _quant_run(tmp_path / "quant", signals=[{"timestamp": "2026-09-01", "expected_return_pct": 2, "horizon_bars": 1, "target_weight": 0}])
    with pytest.raises(ValueError, match="有效期"):
        load_contestant(quant, _event_target(event_run), _market())


def test_prediction_only_warmup_zero_weight_is_not_a_neutral_signal(tmp_path):
    quant = _quant_run(tmp_path, signals=[{"timestamp": "2026-09-01", "target_weight": 0},
                                        {"timestamp": "2026-09-02", "expected_return_pct": 0, "horizon_bars": 3, "target_weight": 0}])
    rows = load_contestant(quant, _asset_target(), _market())["observations"]
    assert len(rows) == 1 and rows[0]["bar_index"] == 1 and rows[0]["direction"] == "neutral"
    assert rows[0]["horizon_bars"] == 3


def test_target_weight_maps_to_direction_but_not_a_return_forecast(tmp_path):
    quant = _quant_run(tmp_path, signals=[{"timestamp": "2026-09-01", "target_weight": 0.7},
                                        {"timestamp": "2026-09-02", "target_weight": 0}], prediction_only=False)
    rows = load_contestant(quant, _asset_target(), _market())["observations"]
    assert [row["direction"] for row in rows] == ["up", "neutral"]
    assert all(row["direction_basis"] == "target_weight" and row["expected_return_pct"] is None for row in rows)


def test_quant_frequency_change_never_reinterprets_horizon(tmp_path):
    source = _market(frequency="1m", stamps=["2026-09-02T09:31:00", "2026-09-02T09:32:00", "2026-09-02T09:33:00"])
    quant = _quant_run(tmp_path, dataset=source, signals=[{"timestamp": source.bars[0].timestamp, "expected_return_pct": 2, "horizon_bars": 2, "target_weight": 0}])
    row = load_contestant(quant, _asset_target(), _market())["observations"][0]
    assert row["horizon_bars"] is None


def test_same_close_uses_latest_information_and_conflicting_same_time_rejected(tmp_path):
    rows = [_event("a", available_time="2026-09-02T10:00:00+08:00"), _event("b", available_time="2026-09-02T12:00:00+08:00")]
    predictions = [{"event_id": "a", "pred_direction": "up", "horizon": "t3"}, {"event_id": "b", "pred_direction": "down", "horizon": "t3"}]
    run = _event_run(tmp_path, rows, predictions)
    result = load_contestant(run, _asset_target(), _market())
    assert len(result["observations"]) == 1 and result["observations"][0]["direction"] == "down"
    assert any("最新" in warning for warning in result["warnings"])
    rows[1]["available_time"] = rows[0]["available_time"]
    run = _event_run(tmp_path, rows, predictions)
    with pytest.raises(ValueError, match="冲突"):
        load_contestant(run, _asset_target(), _market())


def test_no_executable_result_is_not_replaced_by_fake_zero(tmp_path):
    run = _event_run(tmp_path, [_event(available_time="2026-09-08T10:00:00+08:00")])
    with pytest.raises(ValueError, match="下一根开盘"):
        load_contestant(run, _event_target(run), _market())


def test_market_mismatch_rejected_and_oracle_path_never_read(tmp_path):
    run = _event_run(tmp_path, labels_path="/must-not-read/oracle.jsonl")
    target = _event_target(run)
    load_contestant(run, target, _market())
    with pytest.raises(ValueError, match="不一致"):
        load_contestant(run, target, _market(symbol="600001.SH"))


def test_saved_quant_adapters_replay_on_common_prices_and_common_forecast_samples(tmp_path):
    market = _market()
    left = _quant_run(tmp_path / "left", signals=[
        {"timestamp": "2026-09-01", "expected_return_pct": 3, "horizon_bars": 3, "target_weight": 0},
        {"timestamp": "2026-09-02", "expected_return_pct": 2, "horizon_bars": 3, "target_weight": 0},
    ])
    right = _quant_run(tmp_path / "right", signals=[
        {"timestamp": "2026-09-01", "expected_return_pct": -2, "horizon_bars": 3, "target_weight": 0},
    ])
    contestants = [load_contestant(run, _asset_target(), market) for run in (left, right)]
    result = compare_on_market(market, contestants, {"holding_bars": 3, "fee_bps": 0, "slippage_bps": 0})
    assert result["models"][0]["forecast"]["own_n"] == 2
    assert all(model["forecast"]["n"] == 1 for model in result["models"])
    actual = (market.bars[3].close / market.bars[0].close - 1) * 100
    assert result["models"][0]["forecast"]["mae_pct"] == pytest.approx(abs(3 - actual))
    assert all(point["net_value"] == 1 for point in result["models"][1]["curve"])
    assert result["benchmark_curve"][0]["net_value"] == 1


def test_event_and_earlier_quant_forecast_replay_without_false_common_mae(tmp_path):
    market = _market()
    event = _event_run(tmp_path / "event")
    quant = _quant_run(tmp_path / "quant", signals=[
        {"timestamp": "2026-09-01", "expected_return_pct": -2, "horizon_bars": 3, "target_weight": 0},
        {"timestamp": "2026-09-02", "expected_return_pct": 99, "horizon_bars": 3, "target_weight": 0},
    ])
    target = _event_target(event)
    contestants = [load_contestant(run, target, market) for run in (event, quant)]
    result = compare_on_market(market, contestants, {"holding_bars": 3, "fee_bps": 0, "slippage_bps": 0})
    assert all(model["forecast"]["n"] == 0 and model["forecast"]["mae_pct"] is None for model in result["models"])
    assert result["models"][0]["forecast"]["own_n"] == 1
    assert result["models"][1]["forecast"]["own_n"] == 0
    assert all(point["net_value"] == 1 for point in result["models"][1]["curve"])
    assert any("起算时点" in text for text in result["models"][1]["warnings"])
