from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, db
from app.routes import chat as chat_mod
from app.schemas import ChatRequest


def _event(frame: str) -> dict:
    return json.loads(frame.removeprefix("data: ").strip())


class ChatSimulationHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        config.DB_PATH = str(Path(self.temporary.name) / "pronoia.db")
        db.init_db()
        self.case = db.create_case("handoff test")

    async def asyncTearDown(self):
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        self.temporary.cleanup()

    async def test_predictor_plan_starts_from_created_graph(self):
        async def fake_title(question):
            return "测试标题"

        async def fake_run_team(question, history, state, artifact_store, team_members=None):
            graph = await artifact_store(
                "graph",
                "证据图",
                {
                    "question": question,
                    "nodes": [{"id": "E1", "kind": "evidence", "title": "公告"}],
                    "edges": [],
                },
            )
            state["team_plan"] = [{"agent": "predictor"}, {"agent": "deep_researcher"}]
            state["content"] = "研究完成"
            yield {"type": "artifact", "agent": "deep_researcher", "artifact": graph}
            yield {"type": "agent_step", "phase": "agent_start", "agent": "predictor"}

        captured = {}

        def fake_start(case_id, request):
            captured["case_id"] = case_id
            captured["graph_id"] = request.source_graph_artifact_id
            return {"id": "local_job_1", "status": "queued"}

        with (
            patch.object(chat_mod, "run_team", fake_run_team),
            patch.object(chat_mod, "start_simulation_service", fake_start),
            patch.object(chat_mod, "_gen_title", fake_title),
        ):
            frames = [
                _event(frame)
                async for frame in chat_mod._chat_stream(ChatRequest(
                    case_id=self.case["id"],
                    message="研究 600519 未来30天",
                    mode="team",
                    team_members=["predictor"],
                ))
            ]

        started = next(item for item in frames if item.get("phase") == "simulation_started")
        self.assertEqual(started["simulation_job_id"], "local_job_1")
        self.assertLess(
            next(i for i, item in enumerate(frames) if item.get("phase") == "simulation_started"),
            next(i for i, item in enumerate(frames) if item.get("phase") == "agent_start" and item.get("agent") == "predictor"),
        )
        self.assertEqual(captured["case_id"], self.case["id"])
        self.assertTrue(captured["graph_id"])
        assistant = next(
            item for item in reversed(db.list_messages(self.case["id"]))
            if item["role"] == "assistant"
        )
        self.assertTrue(any(
            item.get("phase") == "simulation_started"
            and item.get("simulation_job_id") == "local_job_1"
            for item in assistant["tool_trace"]
        ))

    async def test_predictor_plan_without_graph_is_safely_skipped(self):
        async def fake_title(question):
            return "测试标题"

        async def fake_run_team(question, history, state, artifact_store, team_members=None):
            state["team_plan"] = [{"agent": "predictor"}]
            state["content"] = "证据不足"
            if False:
                yield {}

        with (
            patch.object(chat_mod, "run_team", fake_run_team),
            patch.object(chat_mod, "_gen_title", fake_title),
        ):
            frames = [
                _event(frame)
                async for frame in chat_mod._chat_stream(ChatRequest(
                    case_id=self.case["id"],
                    message="预测一个没有证据的事件",
                    mode="team",
                    team_members=["predictor"],
                ))
            ]

        skipped = next(item for item in frames if item.get("phase") == "simulation_skipped")
        self.assertIn("没有生成可用证据图", skipped["note"])

    async def test_cancelled_stream_keeps_partial_assistant_progress(self):
        async def fake_run_team(question, history, state, artifact_store, team_members=None):
            state["team_plan"] = [{"agent": "deep_researcher"}]
            yield {
                "type": "agent_step",
                "phase": "plan",
                "note": "开始研究",
                "plan": [{"agent": "deep_researcher", "task": "建立证据图"}],
            }
            yield {
                "type": "agent_step",
                "phase": "agent_start",
                "agent": "deep_researcher",
            }

        with patch.object(chat_mod, "run_team", fake_run_team):
            stream = chat_mod._chat_stream(ChatRequest(
                case_id=self.case["id"],
                message="研究中途刷新",
                mode="team",
                team_members=["deep_researcher"],
            ))
            await anext(stream)  # meta
            await anext(stream)  # persisted plan
            await stream.aclose()

        messages = db.list_messages(self.case["id"])
        assistant = next(item for item in messages if item["role"] == "assistant")
        self.assertEqual(len(messages), 2)
        self.assertTrue(any(
            item.get("phase") == "plan"
            for item in assistant["tool_trace"]
        ))
        self.assertTrue(any(
            item.get("phase") == "interrupted"
            for item in assistant["tool_trace"]
        ))


if __name__ == "__main__":
    unittest.main()
