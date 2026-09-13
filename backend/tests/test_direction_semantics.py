import json
import types
import unittest
from unittest.mock import patch

import backend.app.agents.team as team
import backend.app.event_backtest.engine as engine
import backend.scripts.smoke_deep_researcher_prompt_ab as smoke_ab
from backend.app.event_backtest.models import EventRecord
from backend.app.skills.analyzers import ar_decomposer
from backend.app.skills.registry import ensure_skills_loaded


class _FakeJudgeCompletions:
    async def create(self, **kwargs):
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=json.dumps({
                "pred_direction": "down",
                "confidence": 0.55,
                "rationale": "weak signed score",
            }))
        )])


class TestDirectionSemantics(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        ensure_skills_loaded()

    async def test_ar_direction_is_benchmark_relative(self):
        result = await ar_decomposer(stock_return_pct=0.8, benchmark_return_pct=1.5)

        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["data"]["relative_return_pct"], -0.7)
        self.assertTrue(result["data"]["signal_valid"])
        self.assertEqual(result["data"]["signal_direction"], "down")

    async def test_signal_router_uses_relative_direction_and_strength(self):
        routed = await team._route_signals(
            {"market": "CN", "event_type_l2": "并购/分拆/再融资", "title": ""},
            {},
            [{
                "type": "tool",
                "skill": "ar_decomposer",
                "args": {"stock_return_pct": 0.8, "benchmark_return_pct": 1.5},
                "ok": True,
                "preview": "ok",
            }],
        )

        self.assertIsNotNone(routed)
        signal = next(
            item for item in routed["signal_detail"]
            if item["signal"] == "t0_active_return"
        )
        self.assertEqual(signal["direction"], "down")
        self.assertAlmostEqual(signal["strength"], 0.7 / 1.5)

    async def test_team_prompt_preserves_low_confidence_direction(self):
        async def fake_complete_json(system, user, *, max_tokens=900):
            return {
                "pred_direction": "up",
                "confidence": 0.55,
                "rationale": "weak but signed",
            }

        event = EventRecord(
            event_id="low_conf_direction_smoke",
            market="CN",
            symbol="600000",
            event_time="2025-01-01",
            event_type_l2="测试",
            title="测试",
            event_text="测试",
            source_url="local",
            benchmark="sh000300",
        )
        with patch.object(engine, "complete_json", fake_complete_json):
            predictions = await engine.run_team_prompt(
                [event], run_id="direction-semantics", concurrency=1
            )

        # Confidence quantifies uncertainty; it must not erase a supported direction.
        self.assertEqual(predictions[0].pred_direction, "up")
        self.assertAlmostEqual(predictions[0].confidence or 0, 0.55)

    async def test_smoke_judge_preserves_low_confidence_direction(self):
        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=_FakeJudgeCompletions())
        )
        with patch.object(smoke_ab, "get_client", return_value=client):
            result = await smoke_ab._judge_direction(
                {"market": "CN", "symbol": "600000"},
                {"nodes": [], "edges": []},
            )

        self.assertEqual(result["pred_direction"], "down")
        self.assertFalse(result["confidence_gate_applied"])


if __name__ == "__main__":
    unittest.main()
