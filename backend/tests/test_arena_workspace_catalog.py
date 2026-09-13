"""Model-first discovery is read-only and date filtering cannot be cosmetic."""
import copy
import json

import pytest

from app.event_backtest import arena_workspace_catalog as subject


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from app import config, db
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "arena-catalog.sqlite"))
    db.init_db()
    profile = {"id": "connection", "name": "Qwen test", "model_id": "qwen-test", "provider": "Qwen",
               "base_url": "https://api.fixture.invalid/v1", "secret_env_ref": "server-ref",
               "secret_configured": True, "is_active": True, "max_output_tokens": 65536}
    monkeypatch.setattr(subject.profiles, "get_default_profile", lambda **kw: profile)
    monkeypatch.setattr(subject.profiles, "list_profiles", lambda **kw: [profile])
    event = {"event_id": "one", "market": "CN", "symbol": "600000", "title": "Actual order announcement",
             "event_time": "2026-09-03T10:00:00+08:00", "available_time": "2026-09-03T10:00:00+08:00",
             "event_text": "Published orders increased.", "source_url": "https://news.fixture.invalid/item",
             "event_type_l2": "company"}
    event_path = tmp_path / "events.jsonl"
    event_path.write_text(json.dumps(event) + "\n")
    market_path = tmp_path / "market.csv"
    market_path.write_text("timestamp,open,high,low,close,volume\n" + "\n".join(
        f"2026-09-{day:02d},100,110,90,{100 + day},1000" for day in range(1, 8)))
    datasets = [
        {"id": "event-dataset", "name": "Events with no run", "dataset_kind": "event", "path": str(event_path)},
        {"id": "market-dataset", "name": "Market", "dataset_kind": "market", "path": str(market_path),
         "symbols": ["600000.SH"], "markets": ["CN"], "frequency": "1d",
         "coverage": {"start_at": "2026-09-01", "end_at": "2026-09-07"}},
    ]
    runs = [{"id": "pending-quant", "name": "Saved MA", "status": "running", "engine_mode": "portfolio",
             "strategy_spec": {"type": "quant", "adapter": "builtin", "kind": "ma_cross",
                               "parameters": {"short_window": 2, "long_window": 3}}}]
    monkeypatch.setattr(subject.db, "list_bt_runs", lambda **kw: runs)
    monkeypatch.setattr(subject.db, "list_bt_datasets", lambda: datasets)
    yield profile, datasets, runs, event, tmp_path
    db._conn.close()


def test_models_are_configurations_and_no_finished_run_is_required(setup):
    _, _, _, _, _ = setup
    result = subject.catalog()
    assert [m["kind"] for m in result["models"]] == ["pronoia", "external_model", "pronoia", "quant"]
    assert all(m["available"] for m in result["models"])
    assert {t["kind"] for t in result["targets"]} == {"event", "event_set", "asset"}
    assert {m["track"] for m in result["models"] if m["kind"] != "quant"} == {"event"}
    assert next(m for m in result["models"] if m["kind"] == "quant")["track"] == "quant"
    assert next(m for m in result["models"] if m["kind"] == "quant")["supported_target_kinds"] == ["asset"]
    encoded = json.dumps(result)
    for sensitive in ["https://", "server-ref", "events.jsonl", "market.csv", "base_url", "secret_env_ref"]:
        assert sensitive not in encoded


def test_profiles_freeze_independent_pronoia_variants_without_changing_default(setup):
    profile, _, _, _, _ = setup
    before = copy.deepcopy(profile)
    raw = subject.resolve_model("raw:connection")
    variant = subject.resolve_model("pronoia:connection")
    platform = subject.resolve_model("pronoia:platform")
    assert raw["profile_snapshot"] == variant["profile_snapshot"] == platform["profile_snapshot"]
    raw["profile_snapshot"]["model_id"] = "changed-copy"
    assert profile == before and variant["profile_snapshot"]["model_id"] == "qwen-test"
    profile["is_active"] = False
    assert all(not m["available"] for m in subject.catalog()["models"][:3])
    with pytest.raises(ValueError, match="停用"):
        subject.resolve_model("raw:connection")


def test_strategy_deduplication_ignores_run_name_but_preserves_parameters(setup):
    _, _, runs, _, _ = setup
    original = copy.deepcopy(runs[0])
    duplicate = copy.deepcopy(original)
    duplicate.update(id="other", name="Different run")
    duplicate["strategy_spec"].update(name="Display-only name", rules_summary="Display", model_id="display-id",
                                      frequency="5m")
    other = copy.deepcopy(original)
    other.update(id="distinct")
    other["strategy_spec"]["parameters"]["long_window"] = 5
    runs.extend([duplicate, other])
    models = [m for m in subject.catalog()["models"] if m["kind"] == "quant"]
    assert len(models) == 2
    assert subject.resolve_model(models[0]["id"])["strategy_spec"] == original["strategy_spec"]
    assert runs[0] == original


def test_same_event_facts_with_different_ids_and_predictions_deduplicate(setup):
    _, datasets, _, event, tmp_path = setup
    duplicate = {**event, "event_id": "other-id", "direction": "down", "expected_return_pct": -4}
    path = tmp_path / "duplicate.jsonl"
    path.write_text(json.dumps(duplicate) + "\n")
    datasets.append({"id": "duplicate", "dataset_kind": "event", "path": str(path)})
    assert len([t for t in subject.catalog()["targets"] if t["kind"] == "event"]) == 1


def test_selected_dates_filter_evaluation_but_preserve_warmup_without_future_bars(setup):
    result = subject.resolve_target("dataset:market-dataset", "2026-09-03", "2026-09-05")
    assert [bar.timestamp for bar in result["dataset"].bars] == ["2026-09-03", "2026-09-04", "2026-09-05"]
    assert len(result["history_dataset"].bars) == 5
    assert result["history_dataset"].bars[-1].timestamp == "2026-09-05"
    assert result["event"] is None
    assert result["dataset"].fingerprint != result["history_dataset"].fingerprint


def test_end_date_includes_intraday_end_day_and_respects_market_timezone(setup):
    _, datasets, _, _, tmp_path = setup
    path = tmp_path / "minute.csv"
    path.write_text("timestamp,open,high,low,close\n"
        "2026-09-02T23:31:00Z,100,110,90,101\n"
        "2026-09-03T07:00:00Z,100,110,90,102\n"
        "2026-09-04T02:00:00Z,100,110,90,103\n")
    datasets[1].update(path=str(path), frequency="1m")
    result = subject.resolve_target("dataset:market-dataset", "2026-09-03", "2026-09-03")
    assert len(result["dataset"].bars) == 2
    assert result["dataset"].bars[-1].timestamp.startswith("2026-09-03T07:00")


def test_asset_target_groups_frequencies_but_legacy_dataset_id_still_resolves(setup):
    _, datasets, _, _, tmp_path = setup
    path = tmp_path / "five-minute.csv"
    path.write_text("timestamp,open,high,low,close\n"
        "2026-09-03T01:35:00Z,100,101,99,100.5\n"
        "2026-09-03T01:40:00Z,100.5,102,100,101\n"
        "2026-09-03T01:45:00Z,101,103,100,102\n")
    datasets.append({"id": "market-five", "name": "Market · 5m", "dataset_kind": "market", "path": str(path),
        "symbols": ["600000.SH"], "markets": ["CN"], "frequency": "5m",
        "coverage": {"start_at": "2026-09-03T01:35:00Z", "end_at": "2026-09-03T01:45:00Z"}})
    assets = [target for target in subject.catalog()["targets"] if target["kind"] == "asset"]
    assert len(assets) == 1
    assert assets[0]["id"] == "asset:CN:600000.SH"
    assert assets[0]["frequency"] is None
    assert [item["frequency"] for item in assets[0]["available_frequencies"]] == ["1d", "5m"]
    minute = subject.resolve_target(assets[0]["id"], "2026-09-03", "2026-09-03", frequency="5m")
    assert minute["dataset"].frequency == minute["target"]["frequency"] == "5m"
    daily = subject.resolve_target("dataset:market-dataset", "2026-09-01", "2026-09-03")
    assert daily["dataset"].frequency == "1d"
    with pytest.raises(ValueError, match="绑定其他"):
        subject.resolve_target("dataset:market-dataset", "2026-09-01", "2026-09-03", frequency="5m")


@pytest.mark.parametrize(("name", "expected"), [
    ("沪深300日K（180根）", "沪深300"),
    ("中证1000分钟K（1200根）", "中证1000"),
    ("中证A500 · 日 K", "中证A500"),
    ("Market · 5m", "Market"),
])
def test_grouped_asset_name_does_not_imply_a_locked_frequency(name, expected):
    assert subject._asset_name(name, "000300.SH") == expected


def test_event_set_reuses_frozen_events_and_oracle_without_fetching_n_markets(setup, monkeypatch):
    _, datasets, _, _, tmp_path = setup
    labels = tmp_path / "set-labels.jsonl"
    labels.write_text(json.dumps({"event_id": "one", "label_t3": "down", "ret_t3": -0.04,
                                  "bm_ret_t3": -0.01, "car_t3": -0.03, "epsilon": 0.005}) + "\n")
    datasets[0]["labels_path"] = str(labels)
    event_set = next(target for target in subject.catalog()["targets"] if target["kind"] == "event_set")
    assert event_set["available"] is True
    assert event_set["oracle"]["available_horizons"] == ["t3"]
    monkeypatch.setattr(subject, "_fetch_event_market", lambda *args: pytest.fail("event set must use frozen labels"))
    result = subject.resolve_target(event_set["id"], "2026-09-01", "2026-09-30", frequency="1d")
    assert len(result["events"]) == len(result["event_cases"]) == 1
    case = result["event_cases"][0]
    assert case["dataset"] is case["history_dataset"] is None
    truth = case["event_truth"]["horizons"]["t3"]
    assert truth["direction"] == "down"
    assert truth["asset_return"] == truth["actual_return"] == -0.04
    assert truth["benchmark_return"] == -0.01
    assert truth["oracle_return"] == truth["excess_return"] == -0.03
    assert result["event_set_snapshot"]["labels_sha256"] == event_set["oracle"]["labels_sha256"]


def test_event_set_without_frozen_oracle_is_listed_but_not_executable(setup):
    event_set = next(target for target in subject.catalog()["targets"] if target["kind"] == "event_set")
    assert event_set["available"] is False and event_set["oracle"]["status"] == "unavailable"
    with pytest.raises(ValueError, match="Oracle"):
        subject.resolve_target(event_set["id"], "2026-09-01", "2026-09-30")


def test_event_targets_remain_daily_only(setup):
    target = next(target for target in subject.catalog()["targets"] if target["kind"] == "event")
    with pytest.raises(ValueError, match="不能改为分钟"):
        subject.resolve_target(target["id"], "2026-09-01", "2026-09-07", frequency="5m")


@pytest.mark.parametrize("start,end,match", [
    ("2026-09-05", "2026-09-01", "开始日期"),
    ("2026-12-01", "2026-12-10", "2 根"),
    ("2026-09-05", "2026-09-05", "2 根"),
    ("not-a-date", "2026-09-05", "YYYY-MM-DD"),
])
def test_invalid_or_empty_date_range_is_never_ignored(setup, start, end, match):
    with pytest.raises(ValueError, match=match):
        subject.resolve_target("dataset:market-dataset", start, end)


def test_event_prefers_local_real_ohlc_and_strips_embedded_prediction(setup, monkeypatch):
    _, _, _, _, _ = setup
    target = next(t for t in subject.catalog()["targets"] if t["kind"] == "event")
    monkeypatch.setattr(subject, "_fetch_event_market", lambda *args: pytest.fail("local prices should be preferred"))
    result = subject.resolve_target(target["id"], "2026-09-01", "2026-09-07")
    assert result["event"].title == "Actual order announcement"
    assert result["dataset"].symbol == "600000.SH"
    assert result["dataset"].bars[0].open == 100
    with pytest.raises(ValueError, match="必须包含"):
        subject.resolve_target(target["id"], "2026-09-04", "2026-09-07")


def test_local_event_can_start_on_event_day_without_network(setup, monkeypatch):
    target = next(t for t in subject.catalog()["targets"] if t["kind"] == "event")
    monkeypatch.setattr(subject, "_fetch_event_market", lambda *args: pytest.fail("local prices cover event"))
    result = subject.resolve_target(target["id"], "2026-09-03", "2026-09-07")
    assert len(result["dataset"].bars) == 5


def test_existing_event_truth_is_separate_and_matched_to_event_facts(setup):
    _, datasets, _, _, tmp_path = setup
    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps({"event_id": "one", "label_t3": "down", "benchmark_ticker": "000300.SH", "car_method": "market_adjusted"}) + "\n")
    datasets[0]["labels_path"] = str(path)
    target = next(t for t in subject.catalog()["targets"] if t["kind"] == "event")
    result = subject.resolve_target(target["id"], "2026-09-01", "2026-09-07")
    assert result["event_truth"]["truth_basis"] == "market_excess"
    assert result["event_truth"]["horizons"]["t3"]["direction"] == "down"
    assert "label_t3" not in result["event"].to_dict()
    assert "event_truth" not in json.dumps(subject.catalog())


def test_conflicting_event_truth_does_not_silently_pick_first_label(setup):
    _, datasets, runs, _, tmp_path = setup
    for name, label in [("a", "up"), ("b", "down")]:
        path = tmp_path / (name + ".jsonl")
        path.write_text(json.dumps({"event_id": "one", "label_t3": label}) + "\n")
        runs.append({"id": name, "events_path": datasets[0]["path"], "labels_path": str(path), "visibility": "private"})
    target = next(t for t in subject.catalog()["targets"] if t["kind"] == "event")
    result = subject.resolve_target(target["id"], "2026-09-01", "2026-09-07")
    assert result["event_truth"] is None
    assert any("冲突" in note for note in result["notes"])


def test_same_id_in_different_event_facts_does_not_attach_wrong_truth(setup):
    _, _, runs, event, tmp_path = setup
    path = tmp_path / "unrelated.jsonl"
    path.write_text(json.dumps({**event, "title": "Different facts"}) + "\n")
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps({"event_id": "one", "label_t3": "down"}) + "\n")
    runs.append({"id": "unrelated", "events_path": str(path), "labels_path": str(labels), "visibility": "private"})
    target = next(t for t in subject.catalog()["targets"] if t["name"] == event["title"])
    result = subject.resolve_target(target["id"], "2026-09-01", "2026-09-07")
    assert result["event_truth"] is None


@pytest.mark.parametrize("problem", ["line_only", "wrong_asset", "incomplete"])
def test_remote_event_ohlc_must_be_real_complete_and_same_asset(setup, monkeypatch, problem):
    from app.event_backtest import performance
    _, datasets, _, _, _ = setup
    datasets.pop()
    payload = {"market": "CN", "symbol": "600000", "series_type": "ohlc",
               "dates": ["2026-09-02", "2026-09-03", "2026-09-04"],
               "ohlc": [[100, 101, 99, 102], [101, 102, 100, 103], [102, 103, 101, 104]]}
    if problem == "line_only":
        payload.update(series_type="line_only", ohlc=[])
    elif problem == "wrong_asset":
        payload["symbol"] = "600036"
    else:
        payload["ohlc"][0] = [100, 101, 99]
    monkeypatch.setattr(performance, "fetch_event_market_series", lambda *a, **kw: payload)
    target = subject.catalog()["targets"][0]
    with pytest.raises(ValueError):
        subject.resolve_target(target["id"], "2026-09-02", "2026-09-04")


def test_remote_event_market_is_filtered_and_preserves_true_open(setup, monkeypatch):
    from app.event_backtest import performance
    _, datasets, _, _, _ = setup
    datasets.pop()
    payload = {"market": "CN", "symbol": "600000", "series_type": "ohlc",
               "dates": ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"],
               "ohlc": [[100, 101, 99, 102]] * 5}
    monkeypatch.setattr(performance, "fetch_event_market_series", lambda *a, **kw: payload)
    target = subject.catalog()["targets"][0]
    result = subject.resolve_target(target["id"], "2026-09-02", "2026-09-04")
    assert [b.timestamp for b in result["dataset"].bars] == payload["dates"][1:4]
    assert result["dataset"].bars[0].open == 100
    assert result["dataset"].bars[0].close == 101


def test_imported_prediction_is_explicitly_limited_and_not_fake_executable(setup):
    _, _, runs, _, tmp_path = setup
    path = tmp_path / "predictions.jsonl"
    path.write_text('{"event_id":"one","pred_direction":"up"}\n')
    runs.append({"id": "import", "name": "Imported model", "status": "done", "runner": "provided_analysis",
                 "visibility": "private", "events_path": str(tmp_path / "events.jsonl"), "out_path": str(path),
                 "strategy_spec": {"type": "event", "adapter": "imported_decisions", "runner": "provided_analysis"}})
    entry = next(m for m in subject.catalog()["models"] if m["kind"] == "imported_predictions")
    assert entry["available"] and "不会生成" in entry["description"]
    frozen = subject.resolve_model(entry["id"])
    assert frozen["execution_mode"] == "saved_predictions" and frozen["source_run_id"] == "import"
    runs[-1]["visibility"] = "arena_safe"
    with pytest.raises(ValueError, match="完整私有结果"):
        subject.resolve_model(entry["id"])


def test_saved_external_services_do_not_expose_connection_details(setup):
    _, _, runs, _, _ = setup
    runs.extend([
        {"id": "event-service", "name": "Financial predictor", "engine_mode": "event_proxy",
         "strategy_spec": {"type": "event", "adapter": "external_http", "endpoint": "https://service.fixture.invalid/predict",
                           "headers": {"Authorization": "secret:server-only"}}},
        {"id": "quant-service", "name": "Quant predictor", "engine_mode": "portfolio",
         "strategy_spec": {"type": "api", "adapter": "external_http", "kind": "external_http",
                           "endpoint": "https://service.fixture.invalid/weights"}},
    ])
    models = subject.catalog()["models"]
    event_service = next(m for m in models if m["kind"] == "external_service")
    quant_service = next(m for m in models if m["name"] == "Quant predictor")
    assert event_service["available"] and quant_service["available"]
    assert "逐时点" in quant_service["description"]
    assert "service.fixture.invalid" not in json.dumps(models)
    assert "server-only" not in json.dumps(models)
    assert subject.resolve_model(event_service["id"])["strategy_spec"]["endpoint"].endswith("/predict")
