"""Offline regression for official DeepSeek JSON and multi-turn tool requests."""
import copy
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from app import llm
from app.provider_compat import chat_request_options, direct_deepseek


class DeepSeekCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_transport_is_scoped_and_used_for_default_and_profile_clients(self):
        for profile_id in (None, "synthetic-profile"):
            with self.subTest(profile_id=profile_id), patch.dict("os.environ", {"DEEPSEEK_TRANSPORT": "direct"}):
                self.assertTrue(direct_deepseek("https://api.deepseek.com"))
                self.assertFalse(direct_deepseek("https://api.deepinfra.com/v1/openai"))
                target = llm.LLMRuntimeTarget("https://api.deepseek.com", "synthetic-key", "deepseek-flash", 5,
                                             profile_id=profile_id)
                with llm.runtime_target_context(target), patch.object(llm, "_client", None), patch.object(llm, "_profile_clients", {}):
                    client = llm.get_client()
                    # An explicit transport leaves no environment/system proxy mounts.
                    self.assertFalse(client._client._mounts)
                    await client.close()

    def test_defaults_are_endpoint_scoped_and_honor_explicit_mode(self):
        self.assertEqual(chat_request_options("https://api.deepseek.com/v1"),
                         {"extra_body": {"thinking": {"type": "disabled"}}})
        self.assertEqual(chat_request_options("https://api.deepseek.com", "enabled"),
                         {"extra_body": {"thinking": {"type": "enabled"}}})
        for url in ("https://api.deepinfra.com/v1/openai", "https://api.deepseek.com.example.org"):
            self.assertEqual(chat_request_options(url), {})

    async def test_json_text_and_tool_loop_use_the_same_official_defaults(self):
        for mode in ("auto", "enabled"):
            with self.subTest(mode=mode):
                calls = []

                async def stream(tool):
                    yield NS(choices=[NS(
                        delta=NS(content=None if tool else "完成", reasoning_content="synthetic reasoning" if tool else None,
                                 tool_calls=[NS(index=0, id="test_call", function=NS(name="test_skill", arguments="{}"))] if tool else None),
                        finish_reason="tool_calls" if tool else "stop",
                    )])

                async def create(**kwargs):
                    calls.append(copy.deepcopy(kwargs))
                    if kwargs.get("stream"):
                        return stream(len(calls) == 3)
                    return NS(choices=[NS(message=NS(content='{"ok":true}'), finish_reason="stop")])

                async def skill(name, arguments):
                    return {"ok": True}

                target = llm.LLMRuntimeTarget("https://api.deepseek.com", "synthetic-key", "deepseek-flash", 5,
                                             thinking_mode=mode)
                client = NS(chat=NS(completions=NS(create=create)))
                state = {"content": "", "tool_trace": [], "rounds": 0}
                messages = [{"role": "user", "content": "synthetic event"}]
                with llm.runtime_target_context(target), patch.object(llm, "get_client", return_value=client), \
                        patch.object(llm, "tools_for_agent", return_value=[{"type": "function", "function": {"name": "test_skill"}}]):
                    self.assertEqual(await llm.complete_text("system", "user"), '{"ok":true}')
                    self.assertEqual(await llm.complete_json("system", "user"), {"ok": True})
                    events = [event async for event in llm.run_agent("predictor", messages, agent_def={"skills": []},
                              state=state, max_rounds=2, emit_thinking=False, skill_executor=skill)]
                self.assertEqual(len(calls), 4)
                self.assertTrue(all(call["extra_body"] == {"thinking": {"type": "enabled" if mode == "enabled" else "disabled"}} for call in calls))
                assistant = next(m for m in calls[-1]["messages"] if m["role"] == "assistant")
                self.assertEqual(assistant["reasoning_content"], "synthetic reasoning")
                self.assertTrue(any(m["role"] == "tool" for m in calls[-1]["messages"]))
                self.assertEqual(state["content"], "完成")
                self.assertFalse(any(e["type"] == "thinking" for e in events))
