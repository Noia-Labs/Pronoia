"""Causal per-condition confirmations for the public quant rule builder."""
from dataclasses import replace
from datetime import date, timedelta
import json

import pytest

from app.quant_backtest.engine import run_backtest
from app.quant_backtest.models import Bar, ExecutionConfig, MarketDataset, QuantBacktestError
from app.quant_backtest.strategies import DeclarativeRuleStrategy, strategy_from_spec


def _strategy(rule, *, exit_rule=None):
    return DeclarativeRuleStrategy(
        entry={"conditions": [rule]},
        exit={"conditions": [exit_rule or {"field": "price", "operator": "below", "threshold": 1}]},
    )


def _rule(operator="above", count=3, **extra):
    return {"field": "price", "operator": operator, "threshold": 100,
            "consecutive_count": count, **extra}


def _matches(rule, values):
    strategy = _strategy(rule)
    return strategy._confirmed_matches(strategy.entry["conditions"][0], values)


def _dataset(closes, *, opens=None):
    opens = opens or closes
    return MarketDataset(name="Rule confirmation fixture", symbol="TEST", market="US", frequency="1d",
        bars=tuple(Bar(timestamp=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            open=opening, close=closing, high=max(opening, closing) + 1,
            low=min(opening, closing) - 1, volume=1000)
            for index, (opening, closing) in enumerate(zip(opens, closes))))


@pytest.mark.parametrize("operator", ["above", "below", "crosses_above", "crosses_below"])
def test_default_one_is_exactly_existing_behavior_and_has_same_serialization(operator):
    values = [None, 100, 101, 102, 100, 99, 98, None, 101]
    without_count = _rule(operator)
    without_count.pop("consecutive_count")
    original = _strategy(without_count)
    explicit = _strategy({**without_count, "consecutive_count": 1})
    assert original.describe() == explicit.describe()
    rule = explicit.entry["conditions"][0]
    assert "consecutive_count" not in rule
    confirmed = explicit._confirmed_matches(rule, values)
    assert [item[0] for item in confirmed] == [explicit._matches(index, rule, values)[0] for index in range(len(values))]


@pytest.mark.parametrize("operator,values", [
    ("above", [None, 101, 102, 103, 104, 100, 101, None, 101, 99, 101, 102, 103]),
    ("below", [None, 99, 98, 97, 96, 100, 99, None, 99, 101, 99, 98, 97]),
])
def test_level_comparisons_confirm_then_reset_on_equal_missing_or_opposite(operator, values):
    checks = _matches(_rule(operator), values)
    assert [index for index, (matched, _) in enumerate(checks) if matched] == [3, 4, 12]
    assert [audit["confirmation_count"] for _, audit in checks] == [0, 1, 2, 3, 4, 0, 1, 0, 1, 0, 1, 2, 3]
    assert all(audit["required_count"] == 3 for _, audit in checks)


@pytest.mark.parametrize("operator,values", [
    ("crosses_above", [101, 102, 100, 101, 102, 103, 104, 100, 101, 100, 101, 102, None, 101, 100, 101, 102, 103]),
    ("crosses_below", [99, 98, 100, 99, 98, 97, 96, 100, 99, 100, 99, 98, None, 99, 100, 99, 98, 97]),
])
def test_crossing_requires_observed_cross_then_emits_once_after_confirmation(operator, values):
    checks = _matches(_rule(operator), values)
    assert [index for index, (matched, _) in enumerate(checks) if matched] == [5, 17]
    assert [audit["confirmation_count"] for _, audit in checks[:7]] == [0, 0, 0, 1, 2, 3, 4]
    # An initial value or a first value after missing data is not a crossing.
    assert checks[0][1]["confirmation_count"] == checks[13][1]["confirmation_count"] == 0


@pytest.mark.parametrize("combinator,expected", [("and", [False, False, True, False]), ("or", [False, True, True, True])])
def test_each_condition_confirms_independently_before_and_or(combinator, expected):
    strategy = DeclarativeRuleStrategy(entry={"combinator": combinator, "conditions": [
        _rule(count=2), _rule("below", count=3, threshold=105)]},
        exit={"conditions": [{"field": "price", "operator": "below", "threshold": 1}]})
    signals = strategy.generate(_dataset([101, 102, 103, 106]))
    assert [signal.metadata["entry_match"] for signal in signals] == expected
    audits = [strategy._confirmed_matches(rule, strategy._factor_series(_dataset([101, 102, 103, 106]), rule))[-1][1]
              for rule in strategy.entry["conditions"]]
    assert [check["confirmation_count"] for check in audits] == [4, 0]


def test_exit_condition_has_its_own_confirmation_state():
    strategy = _strategy(_rule(count=1), exit_rule=_rule("below", count=2))
    signals = strategy.generate(_dataset([101, 99, 100, 99, 98]))
    assert [signal.target_weight for signal in signals] == [1, 1, 1, 1, 0]


def test_zero_percent_bar_return_threshold_is_a_real_zero_line():
    rule = {
        "field": "bar_return",
        "operator": "crosses_above",
        "threshold": 0,
        "consecutive_count": 3,
    }
    strategy = _strategy(rule)
    normalized = strategy.entry["conditions"][0]
    checks = strategy._confirmed_matches(
        normalized,
        [None, -0.01, 0.01, 0.02, 0.03, 0.0, 0.01, 0.02, 0.03],
    )

    assert normalized["threshold"] == 0.0
    assert [index for index, (matched, _) in enumerate(checks) if matched] == [4, 8]
    assert [checks[index][1]["confirmation_count"] for index in (2, 3, 4)] == [1, 2, 3]
    # Equality is not "above" the threshold and therefore resets the streak;
    # the following positive value starts a new crossing from the zero line.
    assert checks[5][1]["confirmation_count"] == 0
    assert [checks[index][1]["confirmation_count"] for index in (6, 7, 8)] == [1, 2, 3]


def test_future_bars_cannot_change_previous_confirmations_or_signals():
    data = _dataset([99, 101, 102, 103, 104, 98, 101, 102, 103])
    strategy = _strategy(_rule("crosses_above"))
    full = strategy.generate(data)
    for length in range(2, len(data.bars)):
        prefix = replace(data, bars=data.bars[:length], fingerprint="")
        assert strategy.generate(prefix) == full[:length]


def test_third_matching_close_executes_only_at_next_open():
    data = _dataset([99, 101, 102, 103, 104], opens=[99, 101, 102, 103, 120])
    strategy = _strategy(_rule())
    signals = strategy.generate(data)
    assert [signal.target_weight for signal in signals] == [0, 0, 0, 1, 1]
    result = run_backtest(data, signals, execution=ExecutionConfig(commission_bps=0, slippage_bps=0), strategy=strategy.describe())
    assert len(result.trades) == 1
    assert result.trades[0]["signal_timestamp"] == data.bars[3].timestamp
    assert result.trades[0]["execution_timestamp"] == data.bars[4].timestamp
    assert result.trades[0]["market_price"] == 120
    assert all(point["position"] == 0 for point in result.equity_curve[:4])


def test_maximum_count_is_supported_without_a_rolling_window_cap():
    checks = _matches(_rule(count=10_000), [101] * 10_002)
    assert not checks[9998][0] and checks[9999][0] and checks[-1][0]
    assert checks[-1][1]["confirmation_count"] == 10_002


@pytest.mark.parametrize("invalid", [0, -1, 10_001, 1.5, True, False, None, "bad", float("inf"), float("nan")])
def test_invalid_confirmation_counts_are_rejected(invalid):
    with pytest.raises(QuantBacktestError, match="连续满足次数"):
        _strategy(_rule(count=invalid))


def test_serialized_model_roundtrip_preserves_confirmation_semantics():
    strategy = _strategy(_rule("crosses_above", count=3))
    spec = json.loads(json.dumps({"type": "quant", "adapter": "builtin", **strategy.describe()}))
    restored = strategy_from_spec(spec)
    assert restored.describe() == strategy.describe()
    data = _dataset([99, 101, 102, 103, 104])
    assert restored.generate(data) == strategy.generate(data)


def test_standalone_streak_and_new_comparison_operators_are_not_part_of_contract():
    with pytest.raises(QuantBacktestError, match="field"):
        _strategy({**_rule(), "field": "consecutive_changes"})
    for operator in ("at_least", "at_most"):
        with pytest.raises(QuantBacktestError, match="operator"):
            _strategy(_rule(operator))
