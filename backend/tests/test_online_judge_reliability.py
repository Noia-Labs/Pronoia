import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "pipelines" / "online" / "run_agent_prompt_ab.py"
SPEC = importlib.util.spec_from_file_location("online_prompt_ab", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def _valid_payload():
    return {
        "t1": {
            "direction": "up",
            "confidence": 0.7,
            "up_score": 2,
            "down_score": 1,
            "rationale": "净分为正。",
        },
        "t3": {
            "direction": "neutral",
            "confidence": 0.5,
            "up_score": 1,
            "down_score": 1,
            "rationale": "信号精确抵消。",
        },
    }


class JudgeValidationTests(unittest.TestCase):
    def test_empty_fallback_is_not_valid(self):
        self.assertFalse(MODULE._judge_is_valid({
            "raw_response": "",
            "t1": {"direction": "neutral"},
            "t3": {"direction": "neutral"},
        }))

    def test_missing_horizon_is_rejected(self):
        with self.assertRaises(MODULE.JudgeOutputError):
            MODULE._normalize_judge_object({"t1": _valid_payload()["t1"]})

    def test_score_direction_mismatch_is_rejected(self):
        payload = _valid_payload()
        payload["t1"]["direction"] = "neutral"
        with self.assertRaises(MODULE.JudgeOutputError):
            MODULE._normalize_judge_object(payload)


class JudgeRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_response_is_retried(self):
        valid = _valid_payload()

        class Create:
            def __init__(self):
                self.calls = 0

            async def __call__(self, **_kwargs):
                self.calls += 1
                text = "" if self.calls == 1 else json.dumps(valid, ensure_ascii=False)
                choice = SimpleNamespace(
                    message=SimpleNamespace(content=text),
                    finish_reason="stop",
                )
                return SimpleNamespace(choices=[choice], id=f"response-{self.calls}")

        create = Create()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        original_get_client = MODULE.get_client
        original_interval = MODULE.JUDGE_MIN_INTERVAL_SECONDS
        MODULE.get_client = lambda: client
        MODULE.JUDGE_MIN_INTERVAL_SECONDS = 0
        MODULE._JUDGE_LAST_STARTED = 0
        MODULE._JUDGE_SEMAPHORE = asyncio.Semaphore(1)
        try:
            result = await MODULE._judge(
                {"event_id": "retry-smoke", "symbol": "000001"},
                {"nodes": [], "edges": []},
            )
        finally:
            MODULE.get_client = original_get_client
            MODULE.JUDGE_MIN_INTERVAL_SECONDS = original_interval

        self.assertEqual(create.calls, 2)
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["t1"]["direction"], "up")
        self.assertIn("empty_response", result["attempt_diagnostics"][0]["error"])


if __name__ == "__main__":
    unittest.main()
