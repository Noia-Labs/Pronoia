import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    # Normal project invocation: ``cd backend && python -m pytest``.
    from app import llm as llm_mod
except ModuleNotFoundError:
    # Also support running the same test from the repository root.
    import backend.app.llm as llm_mod


class _CaptureCompletions:
    def __init__(self, *actions: object):
        self._actions = list(actions)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        action = self._actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        message = SimpleNamespace(content=action)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_client(completions: _CaptureCompletions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


class _StatusError(Exception):
    def __init__(self, message: str, *, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class TestCompleteJsonCompatibility(unittest.IsolatedAsyncioTestCase):
    async def test_adds_english_json_instruction_without_mutating_business_prompts(self):
        system = "你是事件判断器。只能根据冻结事件内容判断方向。"
        user = "事件：示例公司发布公告；内部标记=do-not-log-this-marker"
        completions = _CaptureCompletions(
            '{"pred_direction":"up","confidence":0.72}'
        )

        stdout = io.StringIO()
        with (
            patch.object(llm_mod, "get_client", return_value=_fake_client(completions)),
            patch.object(llm_mod, "publish") as publish,
            redirect_stdout(stdout),
        ):
            result = await llm_mod.complete_json(system, user, max_tokens=321)

        self.assertEqual(result, {"pred_direction": "up", "confidence": 0.72})
        self.assertEqual(len(completions.calls), 1)
        payload = completions.calls[0]
        self.assertEqual(payload["model"], llm_mod.config.LLM_MODEL)
        self.assertEqual(payload["max_tokens"], 321)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["messages"][0], {"role": "system", "content": system})
        self.assertEqual(payload["messages"][-1], {"role": "user", "content": user})
        self.assertEqual(payload["messages"][1]["role"], "system")
        self.assertIn("json", payload["messages"][1]["content"].lower())
        self.assertIn("return", payload["messages"][1]["content"].lower())
        self.assertNotIn("do-not-log-this-marker", stdout.getvalue())
        self.assertTrue(all(
            "do-not-log-this-marker" not in str(call)
            for call in publish.call_args_list
        ))

    async def test_preserves_existing_json_prompt_and_recovers_embedded_object(self):
        system = "Return a json object with the requested fields.\n保留这段业务规则。"
        user = "只判断目标事件。"
        completions = _CaptureCompletions(
            'model preface\n{"pred_direction":"neutral","confidence":0.5}\nend'
        )

        with (
            patch.object(llm_mod, "get_client", return_value=_fake_client(completions)),
            patch.object(llm_mod, "publish"),
        ):
            result = await llm_mod.complete_json(system, user)

        self.assertEqual(result, {"pred_direction": "neutral", "confidence": 0.5})
        messages = completions.calls[0]["messages"]
        self.assertEqual(messages[0]["content"], system)
        self.assertEqual(messages[-1]["content"], user)
        self.assertTrue(any(
            message["role"] == "system"
            and "json" in message["content"].lower()
            for message in messages
        ))

    async def test_deterministic_4xx_fails_once_across_common_error_shapes(self):
        def caused_404():
            outer = RuntimeError("request wrapper")
            outer.__cause__ = _StatusError("missing model", status_code=404)
            return outer

        cases = {
            "direct-400": lambda: _StatusError(
                "Error code: 400 - Prompt must contain the word 'json' to use "
                "response_format of type json_object",
                status_code=400,
            ),
            "response-401": lambda: _StatusError(
                "authentication failed",
                response=SimpleNamespace(status_code=401),
            ),
            "message-403": lambda: _StatusError("HTTP status: 403 forbidden"),
            "cause-404": caused_404,
        }

        for label, error_factory in cases.items():
            with self.subTest(label=label):
                error = error_factory()
                completions = _CaptureCompletions(error, '{"unexpected":true}')
                sleep = AsyncMock()
                with (
                    patch.object(llm_mod, "get_client", return_value=_fake_client(completions)),
                    patch.object(llm_mod, "publish"),
                    patch.object(llm_mod.asyncio, "sleep", sleep),
                ):
                    with self.assertRaises(Exception) as raised:
                        await llm_mod.complete_json("system", "user")

                self.assertIs(raised.exception, error)
                self.assertEqual(len(completions.calls), 1)
                sleep.assert_not_awaited()

    async def test_explicit_unsupported_response_format_falls_back_once(self):
        unsupported = _StatusError(
            "Error code: 400 - Unknown parameter: response_format; "
            "json_object is not supported by this local model",
            status_code=400,
        )
        completions = _CaptureCompletions(
            unsupported,
            'prefix {"pred_direction":"down","confidence":0.64} suffix',
        )
        sleep = AsyncMock()

        with (
            patch.object(llm_mod, "get_client", return_value=_fake_client(completions)),
            patch.object(llm_mod, "publish"),
            patch.object(llm_mod.asyncio, "sleep", sleep),
        ):
            result = await llm_mod.complete_json("业务系统提示", "事件正文")

        self.assertEqual(result, {"pred_direction": "down", "confidence": 0.64})
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(
            completions.calls[0]["response_format"], {"type": "json_object"}
        )
        self.assertNotIn("response_format", completions.calls[1])
        self.assertEqual(
            completions.calls[1]["messages"], completions.calls[0]["messages"]
        )
        self.assertIn(
            "json", " ".join(
                message["content"] for message in completions.calls[1]["messages"]
            ).lower(),
        )
        sleep.assert_not_awaited()

    async def test_arbitrary_400_never_uses_plain_fallback(self):
        error = _StatusError(
            "Error code: 400 - invalid event payload for response_format",
            status_code=400,
        )
        completions = _CaptureCompletions(error, '{"unexpected":true}')
        sleep = AsyncMock()

        with (
            patch.object(llm_mod, "get_client", return_value=_fake_client(completions)),
            patch.object(llm_mod, "publish"),
            patch.object(llm_mod.asyncio, "sleep", sleep),
        ):
            with self.assertRaises(_StatusError):
                await llm_mod.complete_json("system", "user")

        self.assertEqual(len(completions.calls), 1)
        self.assertIn("response_format", completions.calls[0])
        sleep.assert_not_awaited()

    async def test_plain_fallback_cannot_repeat_or_loop(self):
        first = _StatusError(
            "Error code: 400 - response_format is unsupported",
            status_code=400,
        )
        second = _StatusError(
            "Error code: 400 - response_format is unsupported",
            status_code=400,
        )
        completions = _CaptureCompletions(first, second, '{"unexpected":true}')
        sleep = AsyncMock()

        with (
            patch.object(llm_mod, "get_client", return_value=_fake_client(completions)),
            patch.object(llm_mod, "publish"),
            patch.object(llm_mod.asyncio, "sleep", sleep),
        ):
            with self.assertRaises(_StatusError) as raised:
                await llm_mod.complete_json("system", "user")

        self.assertIs(raised.exception, second)
        self.assertEqual(len(completions.calls), 2)
        self.assertIn("response_format", completions.calls[0])
        self.assertNotIn("response_format", completions.calls[1])
        sleep.assert_not_awaited()

    async def test_429_5xx_connection_and_timeout_still_retry(self):
        cases = {
            "rate-limit": _StatusError("rate limited", status_code=429),
            "server-error": _StatusError(
                "upstream unavailable",
                response={"status_code": 503},
            ),
            "connection": ConnectionError("connection reset"),
            "timeout": TimeoutError("request timed out"),
        }
        for label, error in cases.items():
            with self.subTest(label=label):
                completions = _CaptureCompletions(error, '{"ok":true}')
                sleep = AsyncMock()
                with (
                    patch.object(llm_mod, "get_client", return_value=_fake_client(completions)),
                    patch.object(llm_mod, "publish"),
                    patch.object(llm_mod.asyncio, "sleep", sleep),
                ):
                    result = await llm_mod.complete_json("system", "user")

                self.assertEqual(result, {"ok": True})
                self.assertEqual(len(completions.calls), 2)
                self.assertIn("response_format", completions.calls[0])
                self.assertIn("response_format", completions.calls[1])
                self.assertEqual(sleep.await_count, 1)


if __name__ == "__main__":
    unittest.main()
