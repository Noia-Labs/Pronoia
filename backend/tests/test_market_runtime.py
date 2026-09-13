"""Regression coverage for shared Sina native-runtime protection."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
import time
import unittest

from app.market_runtime import call_sina_history
from app.skills import analysis, fundamentals, market
from app.event_backtest import labeller


class TestMarketRuntime(unittest.TestCase):
    def test_research_and_oracle_share_the_same_native_guard(self):
        for module in (analysis, fundamentals, market, labeller):
            self.assertIs(module.call_sina_history, call_sina_history)
        state = {"active": 0, "maximum": 0}
        observed = Lock()
        start = Barrier(4)

        def native_fetch(value):
            with observed:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            try:
                time.sleep(0.005)
                return value
            finally:
                with observed:
                    state["active"] -= 1

        def execute(n):
            start.wait(timeout=2)
            module = (analysis, fundamentals, market, labeller)[n]
            return module.call_sina_history(native_fetch, n)

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sorted(pool.map(execute, range(4))), list(range(4)))
        self.assertEqual(state["maximum"], 1)

    def test_error_releases_guard_and_nested_calls_do_not_deadlock(self):
        def failure():
            raise ValueError("Sina unavailable")

        with self.assertRaisesRegex(ValueError, "Sina unavailable"):
            call_sina_history(failure)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(call_sina_history, call_sina_history, lambda: "ok")
            self.assertEqual(future.result(timeout=2), "ok")
