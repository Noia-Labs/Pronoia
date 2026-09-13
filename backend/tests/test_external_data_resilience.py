import unittest
from unittest.mock import patch

from backend.app.agents.team import _inject_event_skill_defaults, _seed_graph_from_findings
from backend.app.skills.evidence_graph import EvidenceGraph
from backend.app.skills.market import norm_symbol
from backend.app.skills import skill as skill_mod


class TestExternalDataResilience(unittest.IsolatedAsyncioTestCase):
    def test_event_metadata_fills_omitted_event_study_identity(self):
        args = _inject_event_skill_defaults("event_study_skill", {"window_days": 20}, {
            "symbol": "SH516160",
            "event_time": "2025-01-22T09:30:00+08:00",
            "benchmark": "sh000300",
            "prediction_cutoff_at": "2025-01-22T09:15:00+08:00",
        })
        self.assertEqual(args["symbol"], "SH516160")
        self.assertEqual(args["event_date"], "2025-01-22")
        self.assertEqual(args["benchmark"], "sh000300")
        self.assertEqual(args["prediction_cutoff_at"], "2025-01-22T09:15:00+08:00")

    def test_norm_symbol_preserves_shanghai_etf_family(self):
        self.assertEqual(norm_symbol("516160"), "sh516160")
        self.assertEqual(norm_symbol("SH516160"), "sh516160")
        self.assertEqual(norm_symbol("510300"), "sh510300")
        self.assertEqual(norm_symbol("159915"), "sz159915")
        self.assertEqual(norm_symbol("300750"), "sz300750")

    async def test_event_study_preserves_explicit_etf_exchange(self):
        seen = {}

        async def fake_execute(name, args):
            seen.update(args)
            return {"ok": False, "error": "stop-after-argument-check"}

        with patch.object(skill_mod, "execute_skill", fake_execute):
            result = await skill_mod.event_study_skill(
                event_date="2025-01-22",
                symbol="SH516160",
                benchmark="sh000300",
                as_of=True,
                prediction_cutoff_at="2025-01-22T09:15:00+08:00",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(seen["symbol"], "sh516160")
        self.assertEqual(seen["index_symbol"], "sh000300")
        self.assertTrue(seen["as_of"])
        self.assertEqual(seen["prediction_cutoff_at"], "2025-01-22T09:15:00+08:00")

    async def test_event_study_infers_exchange_for_bare_etf(self):
        seen = {}

        async def fake_execute(name, args):
            seen.update(args)
            return {"ok": False, "error": "stop-after-argument-check"}

        with patch.object(skill_mod, "execute_skill", fake_execute):
            await skill_mod.event_study_skill(
                event_date="2025-01-22", symbol="516160", as_of=True
            )
        self.assertEqual(seen["symbol"], "sh516160")

    def test_graph_seed_uses_bounded_structured_tool_excerpt(self):
        graph = EvidenceGraph(question="q", scope="s")
        added = _seed_graph_from_findings(graph, {}, [{
            "type": "tool",
            "agent": "market_analyst",
            "skill": "event_study_skill",
            "ok": True,
            "args": {"symbol": "SH516160"},
            "preview": "返回 1 行, 来源 event_study_skill",
            "result_excerpt": '{"data":{"t0_return_pct":1.2,"pre5_pct":-3.4}}',
        }])
        self.assertEqual(added, 1)
        payload = graph.to_payload()
        evidence = [n for n in payload["nodes"] if n["kind"] == "evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertIn("t0_return_pct", evidence[0]["body"])
        self.assertIn("pre5_pct", evidence[0]["body"])


if __name__ == "__main__":
    unittest.main()
