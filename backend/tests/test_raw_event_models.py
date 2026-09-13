"""Independent contestants use their frozen direct connection, never team agents."""
from __future__ import annotations

import asyncio
import io
import json
import socket
from dataclasses import replace

import pytest

from app import config, db, llm
from app.event_backtest import engine, orchestrator, raw_model
from app.event_backtest.models import EventRecord, TeamPrediction
from app.event_backtest.strategy_registry import normalize_strategy_spec
from app.model_lab import providers, secret_store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "raw-tests.db"))
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://platform.invalid/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "synthetic-platform-key")
    monkeypatch.setattr(config, "LLM_MODEL", "platform-model")
    monkeypatch.setattr(config, "LLM_RPS_INTERVAL_S", 0, raising=False)
    db.init_db()
    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected network access")
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    yield
    if db._conn is not None:
        db._conn.close()


def event():
    return EventRecord.from_dict({
        "event_id": "evt1", "market": "US", "symbol": "SPY",
        "event_time": "2025-01-03T08:30:00-05:00", "event_type_l2": "macro",
        "title": "CPI", "event_text": "CPI actual 2.8%, expected 3.0%.",
        "source_url": "https://example.invalid/announcement",
        "analysis_direction": "down", "analysis_confidence": .99,
        "analysis_rationale": "secret imported forecast", "expected_return_pct": 987,
        "direction_prior": "down", "event_strength": 3,
        "pre_event_features": {"pre5_drift_pct": 1.2, "ret_t3": 654},
    })


def profile():
    return {"id": "candidate", "provider": "qwen", "base_url": "https://qwen.example.invalid/v1",
            "model_id": "qwen-independent", "secret_env_ref": secret_store.store_secret("synthetic-qwen-key"),
            "max_output_tokens": 4096, "thinking_mode": "disabled"}


def transport(monkeypatch, output=None, fail=False):
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append({"url": request.full_url, "headers": dict(request.header_items()),
                             "body": json.loads(request.data)})
            if fail:
                raise OSError("synthetic network failure")
            answer = output if output is not None else {
                "pred_direction": "up", "confidence": .8, "rationale": "As-of CPI decreased.",
                "expected_return_pct": 2.5,
            }
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 21, "completion_tokens": 9}}).encode())
    monkeypatch.setattr(providers, "_runtime_validate", lambda url: None)
    monkeypatch.setattr(providers, "build_opener", lambda *args: Opener())
    return requests


def test_raw_run_uses_frozen_url_model_encrypted_key_and_persists_forecast(tmp_path, monkeypatch):
    requests = transport(monkeypatch)
    frozen = profile()
    secret_store.store_secret("synthetic-rotated-new-key")
    def forbidden(*args, **kwargs):
        raise AssertionError("Raw contestant must not enter Pronoia")
    monkeypatch.setattr(llm, "model_profile_context", forbidden)
    monkeypatch.setattr(engine, "run_team_prompt", forbidden)
    monkeypatch.setattr(engine, "run_team_full_trajectory", forbidden)
    monkeypatch.setattr("app.agents.team.run_team", forbidden)
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event().to_dict()) + "\n")
    out_path = tmp_path / "out.jsonl"
    run = db.create_bt_run(name="raw", runner="raw_model", events_path=str(events_path),
        out_path=str(out_path), total_events=1,
        config={"model_profile_snapshot": frozen, "evaluation_horizon": "t5"})
    orchestrator._do_run(run["id"], 1)
    assert len(requests) == 1
    request = requests[0]
    assert request["url"] == frozen["base_url"] + "/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer synthetic-qwen-key"
    assert request["body"]["model"] == "qwen-independent"
    assert request["body"]["max_tokens"] == 4096
    assert request["body"]["enable_thinking"] is False
    messages = request["body"]["messages"]
    assert "Pronoia" not in json.dumps(messages)
    packet = json.loads(messages[1]["content"])
    assert packet["evaluation_horizon"] == "t5"
    assert packet["return_forecast_contract"]["unit"] == "percent"
    sent = json.dumps(packet)
    for forbidden_value in ("analysis_direction", "987", "654", "direction_prior", "event_strength", "secret imported"):
        assert forbidden_value not in sent
    saved = TeamPrediction.from_dict(json.loads(out_path.read_text()))
    assert saved.expected_return_pct == 2.5 and saved.horizon == "t5"
    assert saved.tokens_in == 21 and saved.tokens_out == 9
    total, rows = db.list_bt_predictions(run["id"])
    assert total == 1
    assert rows[0]["strategy_metadata"]["expected_return_pct"] == 2.5
    assert rows[0]["expected_return_pct"] == 2.5
    assert TeamPrediction.from_dict(rows[0]).expected_return_pct == 2.5
    assert db.get_bt_prediction(run["id"], "evt1")["expected_return_pct"] == 2.5
    assert "synthetic-qwen-key" not in out_path.read_text()


def test_raw_requires_frozen_profile_and_does_not_fallback_or_retry(monkeypatch):
    requests = transport(monkeypatch, fail=True)
    with pytest.raises(ValueError, match="冻结"):
        asyncio.run(raw_model.run_raw_model([event()], run_id="r", profile={}))
    prediction = asyncio.run(raw_model.run_raw_model([event()], run_id="r", profile=profile()))[0]
    assert len(requests) == 1
    assert prediction.abstain and prediction.expected_return_pct is None
    assert prediction.strategy_metadata["model_call_attempts"] == 1


def test_raw_missing_forecast_stays_missing_and_cancel_skips_request(monkeypatch):
    requests = transport(monkeypatch, {"pred_direction": "down", "confidence": .9, "rationale": "Known facts."})
    p = asyncio.run(raw_model.run_raw_model([event()], run_id="r", profile=profile()))[0]
    assert p.expected_return_pct is None and p.abstain is False
    from app.event_backtest.cancellation import BacktestCancelled
    def cancel():
        raise BacktestCancelled("test")
    with pytest.raises(BacktestCancelled):
        asyncio.run(raw_model.run_raw_model([event()], run_id="r", profile=profile(), before_request=cancel))
    assert len(requests) == 1


def test_raw_registry_and_quant_return_forecast_contracts():
    raw = normalize_strategy_spec({"type": "event", "adapter": "raw_model"}, legacy_runner=None, legacy_strategy_type=None)
    assert raw["runner"] == "raw_model" and raw["contract_version"] == "raw-event-v1"
    quant = normalize_strategy_spec({"type": "quant", "kind": "return_forecast"}, legacy_runner=None, legacy_strategy_type=None)
    assert quant["source"] == "builtin" and quant["prediction_only"] is True
    with pytest.raises(ValueError, match="path"):
        normalize_strategy_spec({"type": "quant", "kind": "return_forecast", "source": "signal_file"}, legacy_runner=None, legacy_strategy_type=None)


def test_team_full_extracts_only_explicit_numeric_forecast(tmp_path, monkeypatch):
    async def fake_team(question, *, state, **kwargs):
        assert "【预期收益率】" in question and "非 CAR" in question
        assert "评估窗口 T+7（事件后7个交易日）" in question
        assert '"target_horizon": "T+7"' in question
        assert '"horizon": "t7"' in question
        assert "T+3" not in question
        state["content"] = "【最终方向】 up\n【置信度】 0.8\n【中文理由】 CPI decreased.\n【依据原文片段】 CPI actual 2.8%.\n【预期收益率】 -1.25"
        yield {"type": "done"}
    monkeypatch.setattr("app.agents.team.run_team", fake_team)
    p = asyncio.run(engine.run_team_full_one_event(event(), run_id="team", model_version="frozen-platform", trajectory_ckpt_dir=tmp_path, target_horizon="t7"))
    assert p.expected_return_pct == -1.25 and p.horizon == "t7"
    ckpt = json.loads((tmp_path / "evt1.json").read_text())
    assert ckpt["structured_extract"]["expected_return_pct"] == -1.25
    for text in ("up .8 implies +4%", "【预期收益率】 null", "【预期收益率】 1e999", "【中文理由】 预期收益率 +5%"):
        assert engine._extract_expected_return_pct(text) is None


def test_legacy_prompt_and_imported_analysis_keep_explicit_return(monkeypatch):
    async def fake_json(*args, **kwargs):
        return {"pred_direction": "up", "confidence": .8, "rationale": "Known CPI.", "expected_return_pct": 3.75}
    monkeypatch.setattr(engine, "complete_json", fake_json)
    p = asyncio.run(engine.run_team_prompt([event()], run_id="legacy", target_horizon="t5"))[0]
    assert p.expected_return_pct == 3.75
    imported = orchestrator._run_provided_analysis_with_cb([replace(event(), analysis_horizon="t5")],
        run_id="import", model_version="supplied", target_horizon="t5", on_pred=lambda *args: None)[0]
    assert imported.expected_return_pct == 987


@pytest.mark.parametrize("runner", ["team_prompt", "team_full", "raw_model"])
@pytest.mark.parametrize("output, expected_status, expected_direction, expected_confidence", [
    ({"prediction_status": "insufficient_data", "pred_direction": None, "confidence": None,
      "rationale": "缺少公告中的实际值和一致预期，无法判断。", "expected_return_pct": 9},
     "insufficient_data", "neutral", 0.0),
    ({"pred_direction": "neutral", "confidence": .5, "rationale": "关键资料缺失"},
     "invalid_output", "neutral", .5),
    ({"prediction_status": "available", "pred_direction": "neutral", "confidence": .5, "rationale": "信号平衡"},
     "invalid_output", "neutral", .5),
    ({"pred_direction": "down", "confidence": .35, "rationale": "已有事实支持偏空，但证据较弱。", "expected_return_pct": -1.2},
     "available", "down", .35),
])
def test_prediction_status_distinguishes_missing_data_from_neutral_and_low_confidence(
    runner, output, expected_status, expected_direction, expected_confidence, tmp_path, monkeypatch,
):
    if runner == "raw_model":
        transport(monkeypatch, output)
        pred = asyncio.run(raw_model.run_raw_model([event()], run_id="status", profile=profile()))[0]
    elif runner == "team_prompt":
        async def fake_json(*args, **kwargs):
            return output
        monkeypatch.setattr(engine, "complete_json", fake_json)
        pred = asyncio.run(engine.run_team_prompt([event()], run_id="status"))[0]
    else:
        text = (
            ("【预测状态】 " + output["prediction_status"] + "\n" if "prediction_status" in output else "")
            + "【最终方向】 " + str(output["pred_direction"]) + "\n"
            + "【置信度】 " + str(output["confidence"]) + "\n"
            + "【中文理由】 " + output["rationale"] + "\n"
            + "【预期收益率】 " + str(output.get("expected_return_pct", "null"))
        )
        async def fake_team(question, *, state, **kwargs):
            state["content"] = text
            yield {"type": "done"}
        monkeypatch.setattr("app.agents.team.run_team", fake_team)
        pred = asyncio.run(engine.run_team_full_one_event(
            event(), run_id="status", model_version="frozen-platform", trajectory_ckpt_dir=tmp_path,
        ))
        checkpoint = json.loads((tmp_path / "evt1.json").read_text())
        assert checkpoint["team_final_state"]["content_full"] == text
        assert checkpoint["structured_extract"]["conf_gate_applied"] is False
    assert pred.strategy_metadata["prediction_status"] == expected_status
    assert pred.pred_direction == expected_direction
    assert pred.confidence == pytest.approx(expected_confidence)
    assert pred.abstain is (expected_status != "available")
    assert bool(pred.strategy_metadata["output_validation_errors"]) is (expected_status == "invalid_output")
    if expected_status == "insufficient_data":
        assert pred.rationale.startswith("缺少公告中的实际值和一致预期")
        assert pred.expected_return_pct is None
    if expected_status == "available":
        assert pred.expected_return_pct == pytest.approx(-1.2)


def test_insufficient_data_requires_reason_and_rejects_contradictory_direction():
    invalid = engine.normalize_event_prediction_output({"prediction_status": "insufficient_data"})
    assert invalid["prediction_status"] == "invalid_output"
    assert "invalid_or_missing_rationale" in invalid["errors"]
    contradictory = engine.normalize_event_prediction_output({
        "prediction_status": "insufficient_data", "pred_direction": "up", "rationale": "缺实际值",
    })
    assert contradictory["prediction_status"] == "invalid_output"
    assert "insufficient_data_has_direction" in contradictory["errors"]


def test_pronoia_packet_preserves_frozen_source_and_availability_without_live_fetch():
    original = event()
    frozen = replace(original, occurred_at="2025-01-03T08:00:00-05:00",
                     available_time="2025-01-03T08:30:00-05:00")
    packet = json.loads(engine._event_prompt(frozen))["as_of_packet"]
    assert packet["source_url"] == frozen.source_url
    assert packet["event_text"] == frozen.event_text
    assert packet["available_time"] == frozen.available_time
    assert packet["occurred_at"] == frozen.occurred_at
    assert packet["constraints"]["web_search_allowed"] is False
    from app.agents.team import STRICT_BACKTEST_SKILL_ALLOWLIST
    assert not {"get_announcements", "get_stock_news", "get_us_stock_sec_filings"} & STRICT_BACKTEST_SKILL_ALLOWLIST
