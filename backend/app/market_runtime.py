"""Serialize Sina history decoders that share a process-wide native V8 runtime."""
from __future__ import annotations

from threading import RLock
from typing import Any, Callable, TypeVar


_Result = TypeVar("_Result")
_SINA_HISTORY_LOCK = RLock()


def call_sina_history(fetch: Callable[..., _Result], *args: Any, **kwargs: Any) -> _Result:
    # AkShare mixes HTTP requests and MiniRacer decoding inside these calls.
    # Concurrent decoders can abort the interpreter on some native builds,
    # bypassing Python exception/fallback handling. Research and Oracle share
    # this lock; alternate providers and model requests remain concurrent.
    with _SINA_HISTORY_LOCK:
        return fetch(*args, **kwargs)
