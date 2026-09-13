"""Regression tests for portfolio snapshot execution-window filtering."""
from __future__ import annotations

import pytest

from app.event_backtest.datasets import load_market_dataset_snapshot
from app.quant_backtest.data import load_csv_dataset
from app.quant_backtest.models import QuantBacktestError


def _contract(tmp_path, timestamps: list[str], *, frequency: str = "1m", market: str = "CN") -> dict:
    path = tmp_path / f"bars-{frequency}.csv"
    rows = ["timestamp,open,high,low,close,volume"]
    rows.extend(
        f"{timestamp},{100 + index},{102 + index},{99 + index},{101 + index},1000"
        for index, timestamp in enumerate(timestamps)
    )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return {
        "name": "minute index fixture",
        "path": str(path),
        "symbols": ["000852.SH"],
        "markets": [market],
        "frequency": frequency,
    }


def _load_csv(contract: dict, *, start=None, end=None, timezone_name=None):
    return load_csv_dataset(
        contract["path"],
        name=contract["name"],
        symbol=contract["symbols"][0],
        market=(contract.get("markets") or [""])[0],
        frequency=contract["frequency"],
        start=start,
        end=end,
        timezone_name=timezone_name,
        repair_ohlc=False,
    )


@pytest.mark.parametrize("frequency", ["1m", "5m"])
def test_date_only_window_uses_market_day_includes_end_day_and_clips_to_coverage(tmp_path, frequency):
    contract = _contract(tmp_path, [
        "2026-09-02T23:31:00Z",  # 2026-09-03 07:31 in Shanghai
        "2026-09-03T01:30:00Z",  # 2026-09-03 09:30 in Shanghai
        "2026-09-03T07:00:00Z",  # 2026-09-03 15:00 in Shanghai
        "2026-09-04T01:30:00Z",
    ], frequency=frequency)

    snapshot = load_market_dataset_snapshot(contract, start="2026-09-01", end="2026-09-03")
    direct = _load_csv(contract, start="2026-09-01", end="2026-09-03")

    # The requested start predates the file and is automatically intersected
    # with real coverage.  Every bar on the Shanghai end date is retained.
    expected_bars = [
        "2026-09-02T23:31:00+00:00",
        "2026-09-03T01:30:00+00:00",
        "2026-09-03T07:00:00+00:00",
    ]
    assert [bar.timestamp for bar in snapshot.bars] == expected_bars
    assert [bar.timestamp for bar in direct.bars] == expected_bars
    expected_audit = {
        "requested_start": "2026-09-01",
        "requested_end": "2026-09-03",
        "available_start": "2026-09-02T23:31:00+00:00",
        "available_end": "2026-09-04T01:30:00+00:00",
        "effective_start": "2026-09-02T23:31:00+00:00",
        "effective_end": "2026-09-03T07:00:00+00:00",
        "window_clipped": True,
        "selected_bar_count": 3,
    }
    for key, value in expected_audit.items():
        assert snapshot.metadata[key] == value
        assert direct.metadata[key] == value
    assert "applied_start" not in snapshot.metadata
    assert "applied_end" not in snapshot.metadata


def test_aware_datetime_bounds_keep_exact_instant_semantics(tmp_path):
    contract = _contract(tmp_path, [
        "2026-09-03T09:30:00+08:00",
        "2026-09-03T09:31:00+08:00",
        "2026-09-03T09:32:00+08:00",
        "2026-09-03T09:33:00+08:00",
    ])

    snapshot = load_market_dataset_snapshot(
        contract,
        start="2026-09-03T01:31:30+00:00",
        end="2026-09-03T09:33:00+08:00",
    )
    direct = _load_csv(
        contract,
        start="2026-09-03T01:31:30+00:00",
        end="2026-09-03T09:33:00+08:00",
    )

    expected = [
        "2026-09-03T09:32:00+08:00",
        "2026-09-03T09:33:00+08:00",
    ]
    assert [bar.timestamp for bar in snapshot.bars] == expected
    assert [bar.timestamp for bar in direct.bars] == expected
    assert snapshot.metadata["window_clipped"] is False
    assert direct.metadata["window_clipped"] is False
    assert snapshot.metadata["effective_start"] == expected[0]
    assert snapshot.metadata["effective_end"] == expected[-1]


def test_date_only_window_can_use_explicit_dataset_contract_timezone(tmp_path):
    contract = _contract(tmp_path, [
        "2026-09-02T23:31:00Z",
        "2026-09-03T07:00:00Z",
        "2026-09-03T16:01:00Z",
    ], market="")
    contract["markets"] = []
    contract["source"] = {
        "type": "local_file",
        "metadata": {"time_zone": "Asia/Shanghai"},
    }

    snapshot = load_market_dataset_snapshot(contract, start="2026-09-03", end="2026-09-03")
    direct = _load_csv(
        contract,
        start="2026-09-03",
        end="2026-09-03",
        timezone_name="Asia/Shanghai",
    )

    expected = [
        "2026-09-02T23:31:00+00:00",
        "2026-09-03T07:00:00+00:00",
    ]
    assert [bar.timestamp for bar in snapshot.bars] == expected
    assert [bar.timestamp for bar in direct.bars] == expected


def test_us_date_only_window_uses_exchange_day_across_utc_date_boundary(tmp_path):
    contract = _contract(tmp_path, [
        "2026-03-09T00:30:00Z",  # March 8 in New York
        "2026-03-09T13:30:00Z",  # March 9 09:30 EDT
        "2026-03-09T20:00:00Z",  # March 9 16:00 EDT
        "2026-03-10T00:30:00Z",  # still March 9 in New York
        "2026-03-10T13:30:00Z",  # March 10 in New York
    ], market="US")

    snapshot = load_market_dataset_snapshot(contract, start="2026-03-09", end="2026-03-09")
    direct = _load_csv(contract, start="2026-03-09", end="2026-03-09")

    expected = [
        "2026-03-09T13:30:00+00:00",
        "2026-03-09T20:00:00+00:00",
        "2026-03-10T00:30:00+00:00",
    ]
    assert [bar.timestamp for bar in snapshot.bars] == expected
    assert [bar.timestamp for bar in direct.bars] == expected
    assert snapshot.metadata["window_clipped"] is False
    assert direct.metadata["window_clipped"] is False


def test_fully_disjoint_window_reports_actual_coverage(tmp_path):
    contract = _contract(tmp_path, [
        "2026-09-03T09:30:00+08:00",
        "2026-09-03T09:31:00+08:00",
    ])

    with pytest.raises(QuantBacktestError) as caught:
        load_market_dataset_snapshot(contract, start="2026-08-01", end="2026-08-31")

    message = str(caught.value)
    assert "无交集" in message
    assert "2026-09-03T09:30:00+08:00" in message
    assert "2026-09-03T09:31:00+08:00" in message

    with pytest.raises(QuantBacktestError) as direct_caught:
        _load_csv(contract, start="2026-08-01", end="2026-08-31")
    assert str(direct_caught.value) == message


def test_single_selected_bar_reports_request_and_actual_coverage(tmp_path):
    contract = _contract(tmp_path, [
        "2026-09-03T09:30:00+08:00",
        "2026-09-03T09:31:00+08:00",
        "2026-09-03T09:32:00+08:00",
    ])
    start = end = "2026-09-03T09:31:00+08:00"

    for load in (
        lambda: load_market_dataset_snapshot(contract, start=start, end=end),
        lambda: _load_csv(contract, start=start, end=end),
    ):
        with pytest.raises(QuantBacktestError) as caught:
            load()
        message = str(caught.value)
        assert "只有 1 根 K 线" in message
        assert start in message
        assert "2026-09-03T09:30:00+08:00" in message
        assert "2026-09-03T09:32:00+08:00" in message


def test_exact_bound_awareness_must_match_bar_timestamps_for_both_loaders(tmp_path):
    contract = _contract(tmp_path, [
        "2026-09-03T09:30:00+08:00",
        "2026-09-03T09:31:00+08:00",
        "2026-09-03T09:32:00+08:00",
    ])

    for load in (
        lambda: load_market_dataset_snapshot(contract, start="2026-09-03T09:30:00"),
        lambda: _load_csv(contract, start="2026-09-03T09:30:00"),
    ):
        with pytest.raises(QuantBacktestError, match="不能混用带时区和不带时区"):
            load()


def test_naive_daily_bars_keep_date_only_filtering_semantics(tmp_path):
    contract = _contract(
        tmp_path,
        ["2026-09-01", "2026-09-02", "2026-09-03"],
        frequency="1d",
    )

    dataset = load_market_dataset_snapshot(contract, start="2026-09-02", end="2026-09-30")
    direct = _load_csv(contract, start="2026-09-02", end="2026-09-30")

    assert [bar.timestamp for bar in dataset.bars] == ["2026-09-02", "2026-09-03"]
    assert [bar.timestamp for bar in direct.bars] == ["2026-09-02", "2026-09-03"]
    assert dataset.metadata["requested_start"] == "2026-09-02"
    assert dataset.metadata["available_start"] == "2026-09-01"
    assert dataset.metadata["available_end"] == "2026-09-03"
    assert dataset.metadata["effective_start"] == "2026-09-02"
    assert dataset.metadata["effective_end"] == "2026-09-03"
    assert dataset.metadata["window_clipped"] is True
    assert dataset.metadata["selected_bar_count"] == 2


def test_unbounded_window_records_full_available_range_without_clipping(tmp_path):
    contract = _contract(
        tmp_path,
        ["2026-09-01", "2026-09-02", "2026-09-03"],
        frequency="1d",
    )

    snapshot = load_market_dataset_snapshot(contract)
    direct = _load_csv(contract)

    expected = {
        "requested_start": None,
        "requested_end": None,
        "available_start": "2026-09-01",
        "available_end": "2026-09-03",
        "effective_start": "2026-09-01",
        "effective_end": "2026-09-03",
        "window_clipped": False,
        "selected_bar_count": 3,
    }
    for key, value in expected.items():
        assert snapshot.metadata[key] == value
        assert direct.metadata[key] == value
