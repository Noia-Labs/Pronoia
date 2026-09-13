"""Canonical event-benchmark resolution shared by prediction and Oracle paths."""
from __future__ import annotations

from typing import Any


ABSOLUTE_RETURN_BENCHMARK = "cash"
_DEFAULT_BY_MARKET = {
    "CN": "sh000300",
    "US": "SPY",
    # These markets do not yet have one verified universal benchmark in the
    # Oracle router.  Freeze an explicit absolute-return sentinel rather than
    # letting an agent silently choose an unrelated equity index.
    "HK": ABSOLUTE_RETURN_BENCHMARK,
    "FUTURES": ABSOLUTE_RETURN_BENCHMARK,
    "CRYPTO": ABSOLUTE_RETURN_BENCHMARK,
    "FX": ABSOLUTE_RETURN_BENCHMARK,
}
_ABSOLUTE_ALIASES = {"cash", "none", "raw", "absolute"}
_DEFAULT_ALIASES = {"", "dataset_default", "event_snapshot_defined"}


def resolve_event_benchmark(market: Any, benchmark: Any = None) -> str:
    """Return the explicit benchmark value to freeze for one event.

    Defaults are deliberately market-only and stable over time.  Strategy code
    may not choose a benchmark from the event title or symbol because Oracle
    scoring must use the exact same contract.
    """
    market_name = str(market or "").strip().upper()
    raw = str(benchmark or "").strip()
    normalized = raw.lower()
    if normalized in _DEFAULT_ALIASES:
        return _DEFAULT_BY_MARKET.get(market_name, ABSOLUTE_RETURN_BENCHMARK)
    if normalized in _ABSOLUTE_ALIASES:
        return ABSOLUTE_RETURN_BENCHMARK
    return raw
