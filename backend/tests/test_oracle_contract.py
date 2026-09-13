"""Run creation must describe the frozen labels, including a zero threshold."""
import json

import pytest

from app.event_backtest.oracle_contract import freeze_oracle_epsilon


def labels(tmp_path, rows):
    path = tmp_path / "labels.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    return str(path)


def test_zero_threshold_is_preserved_from_snapshot(tmp_path):
    path = labels(tmp_path, [{"event_id": "one", "epsilon": 0}, {"event_id": "two", "epsilon": 0}])
    assert freeze_oracle_epsilon(path) == 0
    assert freeze_oracle_epsilon(path, 0) == 0
    with pytest.raises(ValueError, match="不一致"):
        freeze_oracle_epsilon(path, .005)


def test_new_unlabelled_and_legacy_label_defaults(tmp_path):
    assert freeze_oracle_epsilon(None) == 0
    path = labels(tmp_path, [{"event_id": "legacy", "label_t3": "neutral"}])
    assert freeze_oracle_epsilon(path) == .005
    assert freeze_oracle_epsilon(path, 0) == 0
    assert freeze_oracle_epsilon(None, .01) == .01


def test_mixed_or_invalid_thresholds_are_rejected(tmp_path):
    path = labels(tmp_path, [{"epsilon": 0}, {"epsilon": .005}])
    with pytest.raises(ValueError, match="不同收益阈值"):
        freeze_oracle_epsilon(path)
    for value in [True, float("nan"), float("inf"), -.1, .3, "bad"]:
        with pytest.raises(ValueError):
            freeze_oracle_epsilon(None, value)


def test_does_not_mutate_label_file(tmp_path):
    path = labels(tmp_path, [{"event_id": "one", "epsilon": 0}])
    from pathlib import Path
    before = Path(path).read_bytes()
    freeze_oracle_epsilon(path)
    assert Path(path).read_bytes() == before
