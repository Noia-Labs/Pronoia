"""Regression tests for point-in-time event evaluation semantics."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd

from app.event_backtest import engine, labeller
from app.event_backtest.evaluation_protocol import select_events_for_execution_window
from app.event_backtest.external_strategy import (
    _event_payload,
    _validate_runtime_endpoint,
    _ValidatedRedirectHandler,
)
from app.event_backtest.metrics import compute_metrics
from app.event_backtest.models import EventLabel, EventRecord, TeamPrediction


def _event(**overrides) -> EventRecord:
    values = {
        "event_id": "evt-1",
        "market": "CN",
        "symbol": "600000",
        "event_time": "2025-01-02T14:00:00+08:00",
        "event_type_l2": "公告",
        "title": "公司发布公告",
        "event_text": "经营情况改善",
        "source_url": "manual://evt-1",
    }
    values.update(overrides)
    return EventRecord.from_dict(values)


class TestEventLogicSafety(unittest.TestCase):
    def test_external_http_requires_https_except_explicit_private_development_targets(self):
        from app.event_backtest.strategy_registry import _validate_public_endpoint

        public_dns = [(2, 1, 6, "", ("8.8.8.8", 443))]
        private_dns = [(2, 1, 6, "", ("127.0.0.1", 80))]
        with patch.dict(os.environ, {"PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS": ""}):
            with self.assertRaisesRegex(ValueError, "必须使用 HTTPS"):
                _validate_public_endpoint("http://8.8.8.8/model")
            self.assertEqual(
                _validate_public_endpoint("https://8.8.8.8/model"),
                "https://8.8.8.8/model",
            )
            with patch(
                "app.event_backtest.external_strategy.socket.getaddrinfo",
                return_value=public_dns,
            ):
                with self.assertRaisesRegex(ValueError, "必须使用 HTTPS"):
                    _validate_runtime_endpoint("http://public.example.com/model")
                _validate_runtime_endpoint("https://public.example.com/model")
            with self.assertRaisesRegex(ValueError, "PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS"):
                _validate_public_endpoint("http://127.0.0.1:8000/model")

        with patch.dict(os.environ, {"PRONOIA_ALLOW_PRIVATE_MODEL_ENDPOINTS": "1"}):
            with self.assertRaisesRegex(ValueError, "公网.*仍必须使用 HTTPS"):
                _validate_public_endpoint("http://8.8.8.8/model")
            self.assertEqual(
                _validate_public_endpoint("http://127.0.0.1:8000/model"),
                "http://127.0.0.1:8000/model",
            )
            with patch(
                "app.event_backtest.external_strategy.socket.getaddrinfo",
                return_value=public_dns,
            ):
                with self.assertRaisesRegex(ValueError, "公网.*仍必须使用 HTTPS"):
                    _validate_runtime_endpoint("http://public.example.com/model")
            with patch(
                "app.event_backtest.external_strategy.socket.getaddrinfo",
                return_value=private_dns,
            ):
                _validate_runtime_endpoint("http://127.0.0.1:8000/model")

    def test_external_http_revalidates_every_redirect_target(self):
        from urllib.request import Request

        handler = _ValidatedRedirectHandler()
        source = Request("https://public.example.com/model", method="POST")

        for target in (
            "http://127.0.0.1/admin",
            "https://model.internal.local/decision",
            "file:///etc/passwd",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                handler.redirect_request(source, None, 302, "Found", {}, target)

        with patch(
            "app.event_backtest.external_strategy.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ):
            redirected = handler.redirect_request(
                source, None, 302, "Found", {},
                "https://redirect.example.org/model-v2",
            )
        self.assertEqual(redirected.full_url, "https://redirect.example.org/model-v2")
        self.assertEqual(handler.max_redirections, 5)

        credentialed = Request(
            "https://public.example.com/model",
            headers={"Authorization": "Bearer secret", "X-API-Key": "secret", "Accept": "application/json"},
            method="POST",
        )
        credential_handler = _ValidatedRedirectHandler(sensitive_headers={"X-API-Key"})
        with patch(
            "app.event_backtest.external_strategy.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ):
            cross_origin = credential_handler.redirect_request(
                credentialed, None, 302, "Found", {}, "https://other.example.org/model-v2",
            )
            same_origin = credential_handler.redirect_request(
                credentialed, None, 302, "Found", {}, "https://public.example.com/model-v2",
            )
        cross_headers = {key.lower(): value for key, value in cross_origin.header_items()}
        same_headers = {key.lower(): value for key, value in same_origin.header_items()}
        self.assertNotIn("authorization", cross_headers)
        self.assertNotIn("x-api-key", cross_headers)
        self.assertEqual(cross_headers["accept"], "application/json")
        self.assertEqual(same_headers["authorization"], "Bearer secret")
        self.assertEqual(same_headers["x-api-key"], "secret")

    def test_new_manual_event_timestamps_are_offset_aware_and_ordered_by_instant(self):
        from app import schemas

        base = {
            "market": "CN",
            "symbol": "600000",
            "title": "带明确时区的事件",
        }
        for field in ("event_time", "available_time"):
            for invalid in (
                "2025-01-02T14:00:00",
                "2025-01-02T14:00:00+0800",
                "2025-01-02 14:00:00+08:00",
            ):
                payload = {
                    **base,
                    "event_time": "2025-01-02T14:00:00+08:00",
                    "available_time": "2025-01-02T14:05:00+08:00",
                    field: invalid,
                }
                with self.subTest(field=field, invalid=invalid), self.assertRaisesRegex(ValueError, "UTC offset"):
                    schemas.ManualBacktestEvent.model_validate(payload)

        # Wall-clock strings sort in the opposite order, but their absolute
        # instants are 06:00Z then 06:30Z and therefore valid.
        accepted = schemas.ManualBacktestEvent.model_validate({
            **base,
            "event_time": "2025-01-02T14:00:00+08:00",
            "available_time": "2025-01-02T01:30:00-05:00",
        })
        self.assertEqual(accepted.available_time, "2025-01-02T01:30:00-05:00")

        with self.assertRaisesRegex(ValueError, "不能早于"):
            schemas.ManualBacktestEvent.model_validate({
                **base,
                "event_time": "2025-01-02T14:00:00+08:00",
                "available_time": "2025-01-02T14:00:00+09:00",
            })

        utc = schemas.ManualBacktestEvent.model_validate({
            **base,
            "event_time": "2025-01-02T06:00:00Z",
        })
        self.assertEqual(utc.event_time, "2025-01-02T06:00:00Z")

    def test_manual_event_facts_are_independent_from_imported_decisions(self):
        from app import schemas
        from app.routes import backtest

        raw_request = schemas.CreateManualDatasetRequest.model_validate({
            "name": "raw facts",
            "events": [{
                "market": "CN",
                "symbol": "600000",
                "event_time": "2025-01-02T14:00:00+08:00",
                "available_time": "2025-01-02T14:05:00+08:00",
                "title": "公司发布公告",
                "event_text": "经营数据已经披露。",
            }],
        })
        self.assertIsNone(raw_request.events[0].analysis_direction)
        self.assertIsNone(raw_request.events[0].confidence)
        self.assertIsNone(raw_request.events[0].rationale)
        with self.assertRaisesRegex(ValueError, "必须同时完整"):
            schemas.ManualBacktestEvent.model_validate({
                "market": "CN",
                "symbol": "600000",
                "event_time": "2025-01-02T14:00:00+08:00",
                "title": "不完整决策",
                "analysis_direction": "up",
            })

        captured: dict = {}

        def fake_upsert(**kwargs):
            captured.clear()
            captured.update(kwargs)
            return {"id": kwargs["dataset_id"], **kwargs}

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(backtest, "DATA_DIR", temp_dir), \
             patch.object(backtest.db, "new_id", side_effect=["raw", "ready"]), \
             patch.object(backtest.db, "upsert_bt_dataset", side_effect=fake_upsert):
            raw = backtest.create_manual_dataset(raw_request)
            raw_row = json.loads(Path(raw.path).read_text(encoding="utf-8").strip())
            self.assertFalse(raw.decision_ready)
            self.assertEqual(raw.input_contract, "events_only")
            self.assertFalse(captured["source"]["metadata"]["contains_imported_decisions"])
            self.assertNotIn("analysis_direction", raw_row)
            self.assertNotIn("analysis_confidence", raw_row)
            self.assertNotIn("analysis_rationale", raw_row)
            self.assertEqual(raw_row["benchmark"], "sh000300")

            ready_request = schemas.CreateManualDatasetRequest.model_validate({
                "name": "ready decisions",
                "events": [{
                    "market": "CN",
                    "symbol": "600000",
                    "event_time": "2025-01-02T14:00:00+08:00",
                    "title": "公司发布公告",
                    "analysis_direction": "up",
                    "confidence": 0.75,
                    "rationale": "事件改善未来现金流。",
                    "horizon": "t5",
                }],
            })
            ready = backtest.create_manual_dataset(ready_request)
            self.assertTrue(ready.decision_ready)
            self.assertEqual(ready.input_contract, "event_plus_provided_analysis")
            self.assertTrue(captured["source"]["metadata"]["contains_imported_decisions"])

    def test_manual_dataset_freezes_one_shared_benchmark_contract(self):
        from app import schemas
        from app.routes import backtest

        request = schemas.CreateManualDatasetRequest.model_validate({
            "name": "cross-market benchmark contract",
            "events": [
                {"market": "US", "symbol": "AAPL", "event_time": "2025-01-02T09:00:00-05:00", "title": "US default"},
                {"market": "US", "symbol": "NVDA", "event_time": "2025-01-02T09:00:00-05:00", "title": "US explicit", "benchmark": "QQQ"},
                {"market": "HK", "symbol": "00700", "event_time": "2025-01-02T09:00:00+08:00", "title": "HK absolute"},
                {"market": "FUTURES", "symbol": "RB0", "event_time": "2025-01-02T09:00:00+08:00", "title": "Futures absolute"},
            ],
        })

        def fake_upsert(**kwargs):
            return {"id": kwargs["dataset_id"], **kwargs}

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(backtest, "DATA_DIR", temp_dir), \
             patch.object(backtest.db, "new_id", return_value="benchmarks"), \
             patch.object(backtest.db, "upsert_bt_dataset", side_effect=fake_upsert):
            dataset = backtest.create_manual_dataset(request)
            rows = [json.loads(line) for line in Path(dataset.path).read_text(encoding="utf-8").splitlines()]
            benchmark_contract = backtest._event_benchmark_contract(dataset.path)

        self.assertEqual([row["benchmark"] for row in rows], ["SPY", "QQQ", "cash", "cash"])
        self.assertEqual(benchmark_contract, ["QQQ", "SPY", "cash"])
        events = [EventRecord.from_dict(row) for row in rows]
        self.assertEqual(
            [json.loads(engine._event_prompt(event))["as_of_packet"]["benchmark"] for event in events],
            ["SPY", "QQQ", "cash", "cash"],
        )
        self.assertEqual([_event_payload(event)["benchmark"] for event in events], ["SPY", "QQQ", "cash", "cash"])

    def test_team_full_historical_replay_enforces_safe_skill_allowlist(self):
        from app.agents.roster import get_agent
        from app.agents.team import (
            STRICT_BACKTEST_SKILL_ALLOWLIST,
            _agent_def_with_skill_allowlist,
        )

        original = get_agent("market_analyst")
        self.assertIn("market_research", original["skills"])
        restricted = _agent_def_with_skill_allowlist(
            "market_analyst", STRICT_BACKTEST_SKILL_ALLOWLIST
        )
        self.assertIn("event_study_skill", restricted["skills"])
        self.assertIn("ar_decomposer", restricted["skills"])
        self.assertNotIn("market_research", restricted["skills"])
        self.assertTrue(set(restricted["skills"]).issubset(STRICT_BACKTEST_SKILL_ALLOWLIST))
        self.assertIn("market_research", get_agent("market_analyst")["skills"])

        skill_args: dict = {}
        executed_skills: list[str] = []
        forbidden_results: list[dict] = []

        async def fake_execute_skill(name, args):
            executed_skills.append(name)
            skill_args.update(args)
            return {"ok": True, "data": {}}

        async def fake_run_agent(
            agent_id, messages, *, agent_def, state, artifact_store,
            skill_executor, max_rounds,
        ):
            forbidden_results.append(await skill_executor("market_research", {"symbol": "600000"}))
            await skill_executor("event_study_skill", {
                "as_of": False,
                "event_date": "2035-12-31",
                "symbol": "AAPL",
                "market": "US",
                "benchmark": "QQQ",
            })
            state["content"] = "安全历史结果"
            yield {"type": "token", "agent": agent_id, "delta": state["content"]}

        async def exercise_restricted_executor():
            from app.agents.team import _run_expert_serial
            from app.llm import noop_artifact_store

            async for _ in _run_expert_serial(
                "market_analyst",
                "历史事件分析",
                "只使用事前信息",
                noop_artifact_store,
                skill_allowlist=STRICT_BACKTEST_SKILL_ALLOWLIST,
                frozen_event_meta={
                    "market": "CN",
                    "symbol": "600000",
                    "event_time": "2025-01-02T14:00:00+08:00",
                    "available_time": "2025-01-03T09:00:00+08:00",
                    "benchmark": "sh000300",
                },
            ):
                pass

        with patch("app.agents.team.run_agent", new=fake_run_agent), \
             patch("app.llm.execute_skill", new=fake_execute_skill):
            asyncio.run(exercise_restricted_executor())
        self.assertIs(skill_args["as_of"], True)
        self.assertEqual(skill_args["event_date"], "2025-01-03")
        self.assertEqual(skill_args["symbol"], "600000")
        self.assertEqual(skill_args["market"], "CN")
        self.assertEqual(skill_args["benchmark"], "sh000300")
        self.assertEqual(executed_skills, ["event_study_skill"])
        self.assertFalse(forbidden_results[0]["ok"])
        self.assertEqual(
            forbidden_results[0]["code"],
            "skill_not_allowed_in_historical_replay",
        )

        captured: dict = {}

        async def fake_team(question, history, state, artifact_store, **kwargs):
            captured.update(kwargs)
            state["content"] = (
                "【最终方向】 up\n"
                "【置信度】 0.70\n"
                "【中文理由】 公告正文显示经营情况改善。"
            )
            yield {"type": "agent_step", "phase": "done", "agent": "router"}

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch("app.agents.team.run_team", new=fake_team):
            prediction = asyncio.run(engine.run_team_full_one_event(
                _event(),
                run_id="strict-team-run",
                model_version="test-model",
                trajectory_ckpt_dir=temp_dir,
            ))
        self.assertEqual(prediction.pred_direction, "up")
        self.assertEqual(
            captured["skill_allowlist"], STRICT_BACKTEST_SKILL_ALLOWLIST
        )

    def test_team_full_missing_rationale_is_invalid_not_trailing_prose(self):
        async def fake_team(question, history, state, artifact_store, **kwargs):
            state["content"] = (
                "【最终方向】 up\n"
                "【置信度】 0.70\n"
                "这是一段没有中文理由字段的自由文本。"
            )
            yield {"type": "agent_step", "phase": "done", "agent": "router"}

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.agents.team.run_team", new=fake_team
        ):
            prediction = asyncio.run(engine.run_team_full_one_event(
                _event(),
                run_id="missing-rationale",
                model_version="test-model",
                trajectory_ckpt_dir=temp_dir,
            ))

            checkpoint = json.loads(
                (Path(temp_dir) / "evt-1.json").read_text(encoding="utf-8")
            )

        self.assertTrue(prediction.abstain)
        self.assertEqual(prediction.pred_direction, "neutral")
        self.assertIn(
            "invalid_or_missing_rationale",
            prediction.strategy_metadata["output_validation_errors"],
        )
        self.assertIn("缺少独立的中文理由字段", prediction.rationale)
        self.assertIn("自由文本", checkpoint["team_final_state"]["content_full"])
        self.assertNotIn("自由文本", checkpoint["structured_extract"]["rationale"])

    def test_available_time_is_the_decision_clock_and_cannot_precede_occurrence(self):
        event = _event(
            event_time="2025-01-02T14:00:00+08:00",
            available_time="2025-01-03T09:00:00+08:00",
        )
        self.assertEqual(event.event_time, "2025-01-03T09:00:00+08:00")
        self.assertEqual(event.available_time, "2025-01-03T09:00:00+08:00")
        self.assertEqual(event.occurred_at, "2025-01-02T14:00:00+08:00")
        self.assertEqual(engine.validate_event(event), [])

        invalid = _event(
            event_time="2025-01-03T14:00:00+08:00",
            available_time="2025-01-02T09:00:00+08:00",
        )
        self.assertIn("available_time 不能早于 occurred_at", engine.validate_event(invalid))

    def test_date_window_uses_market_local_available_date(self):
        # 01:00 UTC on Jan 3 is still Jan 2 in New York.
        event = _event(
            market="US",
            event_time="2025-01-02T19:00:00-05:00",
            available_time="2025-01-03T01:00:00Z",
        )
        selected = select_events_for_execution_window(
            [event], {"start_date": "2025-01-02", "end_date": "2025-01-02"}
        )
        self.assertEqual(selected.event_ids, {event.event_id})
        self.assertEqual(selected.to_dict()["time_field"], "available_time (fallback: event_time)")

    def test_oracle_anchor_does_not_double_shift_weekends(self):
        dates = pd.bdate_range("2025-01-02", periods=6).date
        closes = pd.Series([100, 101, 102, 103, 104, 105], index=dates, dtype=float)
        pre = labeller._event_anchor_dates(
            closes, dt.date(2025, 1, 2), 1, "2025-01-02T08:00:00+08:00", "CN"
        )
        post = labeller._event_anchor_dates(
            closes, dt.date(2025, 1, 2), 1, "2025-01-02T16:00:00+08:00", "CN"
        )
        weekend = labeller._event_anchor_dates(
            closes, dt.date(2025, 1, 4), 1, "2025-01-04T16:00:00+08:00", "CN"
        )
        self.assertEqual(pre[:2], (dt.date(2025, 1, 2), dt.date(2025, 1, 3)))
        self.assertEqual(post[:2], (dt.date(2025, 1, 3), dt.date(2025, 1, 6)))
        self.assertEqual(weekend[:2], (dt.date(2025, 1, 6), dt.date(2025, 1, 7)))

    def test_missing_car_is_oracle_abstain_and_neutral_is_strictly_incorrect(self):
        predictions = [
            TeamPrediction("missing", "up", "run", abstain=False),
            TeamPrediction("neutral", "neutral", "run", abstain=False),
        ]
        labels = [
            EventLabel.from_dict({"event_id": "missing", "label_t3": "up", "car_t3": None}),
            EventLabel.from_dict({"event_id": "neutral", "label_t3": "neutral", "car_t3": 0.0}),
        ]
        summary = compute_metrics(predictions=predictions, labels=labels, primary_oracle_horizon="t3")
        self.assertEqual(summary.n_total, 2)
        self.assertEqual(summary.n_abstain_oracle, 1)
        self.assertEqual(summary.acc_t3_strict.n, 1)
        self.assertEqual(summary.acc_t3_strict.k, 0)

    def test_model_contract_is_horizon_aware_and_invalid_output_abstains(self):
        event = _event(direction_prior="up", event_strength=3)
        packet = json.loads(engine._event_prompt(event, target_horizon="t5"))
        self.assertEqual(packet["as_of_packet"]["constraints"]["target_horizon"], "T+5")
        self.assertNotIn("direction_prior", packet["as_of_packet"])
        external = _event_payload(event)
        self.assertNotIn("direction_prior", external)
        self.assertNotIn("event_strength", external)

        with patch.object(engine, "complete_json", new=AsyncMock(return_value={"unexpected": True})):
            result = asyncio.run(engine.run_team_prompt(
                [event], run_id="safe-run", concurrency=1, target_horizon="t5"
            ))
        self.assertEqual(result[0].pred_direction, "neutral")
        self.assertTrue(result[0].abstain)
        self.assertEqual(result[0].horizon, "t5")
        self.assertIn(
            "invalid_or_missing_direction",
            result[0].strategy_metadata["output_validation_errors"],
        )


if __name__ == "__main__":
    unittest.main()
