"""User cancellation/pause cannot be overwritten by late portfolio results."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app import db
from app.event_backtest import orchestrator as orch
from app.event_backtest.cancellation import BacktestCancelled
from app.quant_backtest import engine
from .test_quant_forecast_unified_integration import client_and_dataset


def prepared(client_and_dataset):
    client, dataset = client_and_dataset
    response = client.post('/api/bt/runs', json={
        'name': 'Portfolio control fixture', 'dataset_id': dataset['id'],
        'strategy_spec': {'type': 'quant', 'kind': 'buy_hold'},
    })
    assert response.status_code == 200, response.text
    run = response.json()
    db.update_bt_run_status(run['id'], 'running')
    return run


def test_cancel_after_simulation_cannot_publish_completed_result(client_and_dataset, monkeypatch):
    run = prepared(client_and_dataset)
    simulate = engine.run_backtest

    def cancel_after_simulation(*args, **kwargs):
        result = simulate(*args, **kwargs)
        assert orch.cancel_bt_run(run['id'])
        return result

    monkeypatch.setattr(engine, 'run_backtest', cancel_after_simulation)
    try:
        with pytest.raises(BacktestCancelled):
            orch._do_portfolio_run(run)
        stored = db.get_bt_run(run['id'])
        assert stored['status'] == 'cancelled' and not stored['metrics']
        assert not Path(run['result_path']).is_file()
    finally:
        orch._RUN_CANCEL.pop(run['id'], None)
        orch._RUN_RESUME.pop(run['id'], None)


def test_paused_simulation_waits_for_resume_before_completion(client_and_dataset, monkeypatch):
    run = prepared(client_and_dataset)
    simulate = engine.run_backtest
    reached, release, finished = threading.Event(), threading.Event(), threading.Event()
    failures = []

    def block_at_simulation_end(*args, **kwargs):
        result = simulate(*args, **kwargs)
        reached.set()
        assert release.wait(5)
        return result

    def worker():
        try:
            orch._do_portfolio_run(run)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(engine, 'run_backtest', block_at_simulation_end)
    thread = threading.Thread(target=worker)
    orch._RUN_TASKS[run['id']] = thread
    thread.start()
    try:
        assert reached.wait(5)
        assert orch.pause_bt_run(run['id'])[0]
        release.set()
        assert not finished.wait(.15)
        assert db.get_bt_run(run['id'])['status'] == 'paused'
        assert not Path(run['result_path']).is_file()
        assert orch.resume_bt_run(run['id'])[0]
        assert finished.wait(5)
        assert not failures
        assert db.get_bt_run(run['id'])['status'] == 'done'
    finally:
        release.set()
        orch._get_resume_event(run['id']).set()
        thread.join(5)
        orch._RUN_TASKS.pop(run['id'], None)
        orch._RUN_RESUME.pop(run['id'], None)
        orch._RUN_CANCEL.pop(run['id'], None)


def test_late_engine_error_after_cancel_keeps_cancelled_status(client_and_dataset, monkeypatch):
    client, _ = client_and_dataset
    run = prepared(client_and_dataset)
    entered, release = threading.Event(), threading.Event()

    def failing_simulation(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise ValueError('A late calculation error after cancellation')

    monkeypatch.setattr(engine, 'run_backtest', failing_simulation)
    db.update_bt_run_status(run['id'], 'pending')
    response = client.post(f"/api/bt/runs/{run['id']}/start")
    assert response.status_code == 200, response.text
    try:
        assert entered.wait(5)
        cancelled = client.post(f"/api/bt/runs/{run['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()['ok']
        release.set()
        deadline = time.monotonic() + 5
        while run['id'] in orch._RUN_TASKS and time.monotonic() < deadline:
            time.sleep(.01)
        assert run['id'] not in orch._RUN_TASKS
        assert db.get_bt_run(run['id'])['status'] == 'cancelled'
    finally:
        release.set()
        thread = orch._RUN_TASKS.get(run['id'])
        if thread:
            thread.join(5)


@pytest.mark.parametrize('kind,parameters', [
    ('buy_hold', {}),
    ('ma_cross', {'short_window': 2, 'long_window': 5}),
    ('momentum', {'lookback': 3, 'negative_weight': 0}),
    ('declarative_rules', {'entry': {'conditions': [{'field': 'volume_ratio', 'operator': 'above', 'lookback': 3, 'threshold': .5}]},
                           'exit': {'conditions': [{'field': 'volume_ratio', 'operator': 'below', 'lookback': 3, 'threshold': .1}]}}),
    ('return_forecast', {'lookback': 3, 'horizon_bars': 3}),
])
def test_local_quant_finishes_while_team_v8_runtime_is_busy(client_and_dataset, monkeypatch, kind, parameters):
    client, dataset = client_and_dataset
    response = client.post('/api/bt/runs', json={
        'name': 'Local quant independent from V8', 'dataset_id': dataset['id'],
        'strategy_spec': {'type': 'quant', 'kind': kind, 'parameters': parameters},
    })
    assert response.status_code == 200, response.text
    run = response.json()
    from app import llm
    monkeypatch.setattr(llm, 'model_profile_context', lambda *args: pytest.fail('Local quant must not use global model configuration'))
    thread = None
    try:
        with orch._V8_GUARD_LOCK:
            assert client.post(f"/api/bt/runs/{run['id']}/start").status_code == 200
            thread = orch._RUN_TASKS.get(run['id'])
            assert thread is not None
            thread.join(2)
            assert not thread.is_alive(), 'A local quant run must not queue behind a slow team_full run'
            assert db.get_bt_run(run['id'])['status'] == 'done'
    finally:
        if thread:
            thread.join(5)
