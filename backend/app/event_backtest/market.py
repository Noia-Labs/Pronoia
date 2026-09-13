from __future__ import annotations

from .benchmark import resolve_event_benchmark
from .models import EventRecord


def resolve_benchmark(event: EventRecord) -> str:
    return resolve_event_benchmark(event.market, event.benchmark)
