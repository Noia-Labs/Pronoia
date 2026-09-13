from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

from app.event_backtest.cancellation import BacktestCancelled
from app.event_backtest import engine


def _event(idx: int) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=f"event-{idx}",
        symbol=f"symbol-{idx}",
        market="CN",
    )


def test_team_full_cancel_stops_active_and_pending_tasks(monkeypatch, tmp_path):
    started: list[str] = []
    cancelled: list[str] = []
    calls: Counter[str] = Counter()
    two_started = asyncio.Event()
    cancel_state = {"value": False}

    async def fake_one(event, **_kwargs):
        calls[event.event_id] += 1
        started.append(event.event_id)
        if len(started) == 2:
            two_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(event.event_id)
            raise

    monkeypatch.setattr(engine, "run_team_full_one_event", fake_one)

    async def scenario() -> None:
        batch = asyncio.create_task(
            engine.run_team_full_trajectory(
                [_event(i) for i in range(5)],
                run_id="cancel-test",
                model_version="fake",
                concurrency=2,
                trajectory_ckpt_dir=tmp_path,
                cancel_check=lambda: cancel_state["value"],
            )
        )
        await asyncio.wait_for(two_started.wait(), timeout=1.0)
        cancel_state["value"] = True
        with pytest.raises(BacktestCancelled, match="cancelled by user"):
            await asyncio.wait_for(batch, timeout=2.0)

    asyncio.run(scenario())

    assert started == ["event-0", "event-1"]
    assert set(cancelled) == {"event-0", "event-1"}
    assert calls == Counter({"event-0": 1, "event-1": 1})


def test_team_full_callback_cancel_is_never_retried(monkeypatch, tmp_path):
    calls = 0

    async def fake_one(event, **kwargs):
        nonlocal calls
        calls += 1
        return engine.TeamPrediction(
            event_id=event.event_id,
            pred_direction="up",
            confidence=0.6,
            rationale="test",
            run_id=kwargs["run_id"],
            model_version=kwargs["model_version"],
        )

    def cancel_in_callback(_prediction):
        raise BacktestCancelled("cancelled by user")

    monkeypatch.setattr(engine, "run_team_full_one_event", fake_one)

    async def scenario() -> None:
        with pytest.raises(BacktestCancelled, match="cancelled by user"):
            await engine.run_team_full_trajectory(
                [_event(0)],
                run_id="callback-cancel-test",
                model_version="fake",
                concurrency=1,
                trajectory_ckpt_dir=tmp_path,
                on_pred_callback=cancel_in_callback,
            )

    asyncio.run(scenario())
    assert calls == 1
