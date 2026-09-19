import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import backend.app.agents.team as team


class HypothesisCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, response=None, error=None):
        state = {"content": "已完成的研究正文", "tool_trace": []}
        with patch.object(team, "complete_json", AsyncMock(return_value=response, side_effect=error)):
            events = [event async for event in team._extract_hypotheses("问题", state["content"], state)]
        self.assertEqual(state["content"], "已完成的研究正文")
        self.assertEqual(events[0]["verdict"], "running")
        self.assertEqual(events[-1], state["tool_trace"][-1])
        return events

    async def test_empty_result_finishes_without_an_endless_thinking_label(self):
        events = await self.collect({"items": []})
        self.assertEqual(events[-1]["verdict"], "empty")
        self.assertIn("未新增", events[-1]["note"])
        self.assertFalse(any(event["type"] == "thinking" for event in events))

    async def test_valid_items_finish_and_are_retained(self):
        events = await self.collect({"items": [{"hypothesis": "若公开撤回交易方案，则并购情景失效"}]})
        self.assertEqual(events[-1]["verdict"], "completed")
        self.assertEqual(len(next(event["items"] for event in events if event["type"] == "logic_items")), 1)

    async def test_provider_or_schema_failure_keeps_the_answer(self):
        for response in (None, {"items": "wrong"}, {"items": [None, 42]}):
            with self.subTest(response=response):
                events = await self.collect(response)
                self.assertEqual(events[-1]["verdict"], "failed")
        events = await self.collect(error=RuntimeError("provider failure"))
        self.assertEqual(events[-1]["verdict"], "failed")

    async def test_timeout_cancels_optional_request_and_finishes(self):
        cancelled = asyncio.Event()

        async def stuck(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        state = {"tool_trace": []}
        with patch.object(team, "complete_json", stuck), patch.object(team, "HYPOTHESIS_TIMEOUT_SECONDS", .01):
            events = [event async for event in team._extract_hypotheses("问题", "正文", state)]
        self.assertTrue(cancelled.is_set())
        self.assertEqual(events[-1]["verdict"], "timeout")

    async def test_user_cancellation_is_not_reported_as_success(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.collect(error=asyncio.CancelledError())
