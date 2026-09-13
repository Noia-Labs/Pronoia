"""Public declarative-factor contract tests (no proprietary strategy logic)."""

from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

from app.quant_backtest.models import Bar, MarketDataset, QuantBacktestError
from app.quant_backtest.strategies import DeclarativeRuleStrategy


def _dataset(size: int = 120) -> MarketDataset:
    start = date(2025, 1, 1)
    bars: list[Bar] = []
    for index in range(size):
        close = 100.0 + 0.18 * index + 4.0 * math.sin(index / 4.0)
        open_price = close - 0.35 * math.sin(index / 3.0)
        bars.append(Bar(
            timestamp=(start + timedelta(days=index)).isoformat(),
            open=open_price,
            high=max(open_price, close) + 1.0 + (index % 3) * 0.1,
            low=min(open_price, close) - 1.0,
            close=close,
            volume=1_000.0 + (index % 11) * 70.0,
        ))
    return MarketDataset(
        name="public-factor-fixture",
        symbol="TEST.INDEX",
        market="CN",
        frequency="1d",
        bars=tuple(bars),
    )


class TestDeclarativePublicFactors(unittest.TestCase):
    def setUp(self) -> None:
        self.dataset = _dataset()

    @staticmethod
    def _strategy(condition: dict, *, combinator: str = "and") -> DeclarativeRuleStrategy:
        return DeclarativeRuleStrategy(
            entry={"combinator": combinator, "conditions": [condition]},
            exit={"combinator": "and", "conditions": [
                {"field": "price", "operator": "below", "lookback": 1, "threshold": 1},
            ]},
            version="factor-contract-test",
        )

    def test_all_public_factor_series_are_really_computed(self) -> None:
        conditions = [
            {"field": "price", "operator": "above", "lookback": 1, "threshold": 0},
            {"field": "bar_return", "operator": "above", "lookback": 1, "threshold": 0},
            {"field": "return", "operator": "above", "lookback": 10, "threshold": 0},
            {"field": "moving_average", "operator": "above", "lookback": 20, "threshold": 0},
            {"field": "ma_cross", "operator": "crosses_above", "lookback": 30, "fast_period": 10, "slow_period": 30, "threshold": 0},
            {"field": "breakout", "operator": "above", "lookback": 20, "threshold": 0},
            {"field": "amplitude", "operator": "above", "lookback": 10, "threshold": 0.01},
            {"field": "volume_ratio", "operator": "above", "lookback": 20, "threshold": 1},
            {"field": "volatility", "operator": "above", "lookback": 20, "threshold": 0.001},
            {"field": "rsi", "operator": "above", "lookback": 14, "threshold": 50},
            {"field": "bollinger_position", "operator": "above", "lookback": 20, "threshold": 0},
            {"field": "macd", "operator": "crosses_above", "lookback": 26, "fast_period": 12, "slow_period": 26, "signal_period": 9, "threshold": 0},
        ]
        for raw in conditions:
            with self.subTest(field=raw["field"]):
                strategy = self._strategy(raw)
                rule = strategy.entry["conditions"][0]
                values = strategy._factor_series(self.dataset, rule)
                self.assertEqual(len(values), len(self.dataset.bars))
                warmup = int(rule["warmup_bars"])
                self.assertTrue(all(item is None for item in values[:warmup]))
                finite = [item for item in values[warmup:] if item is not None]
                self.assertTrue(finite)
                self.assertTrue(all(math.isfinite(item) for item in finite))
                signals = strategy.generate(self.dataset)
                self.assertEqual(len(signals), len(self.dataset.bars))
                self.assertEqual(signals[-1].metadata["factor_contract"], "public_technical_factors-v2")

    def test_volume_ratio_rsi_and_bollinger_units(self) -> None:
        volume_strategy = self._strategy({"field": "volume_ratio", "operator": "above", "lookback": 20, "threshold": 1})
        volume_rule = volume_strategy.entry["conditions"][0]
        volume = volume_strategy._factor_series(self.dataset, volume_rule)
        expected = self.dataset.bars[20].volume / sum(bar.volume for bar in self.dataset.bars[:20]) * 20
        self.assertAlmostEqual(volume[20], expected)

        rsi_strategy = self._strategy({"field": "rsi", "operator": "above", "lookback": 14, "threshold": 50})
        rsi = rsi_strategy._factor_series(self.dataset, rsi_strategy.entry["conditions"][0])
        self.assertTrue(all(0 <= item <= 100 for item in rsi if item is not None))

        boll_strategy = self._strategy({"field": "bollinger_position", "operator": "above", "lookback": 20, "threshold": 2})
        boll = boll_strategy._factor_series(self.dataset, boll_strategy.entry["conditions"][0])
        self.assertTrue(any(abs(item) > 0.01 for item in boll if item is not None))

    def test_or_combinator_and_period_validation(self) -> None:
        strategy = DeclarativeRuleStrategy(
            entry={"combinator": "or", "conditions": [
                {"field": "price", "operator": "above", "lookback": 1, "threshold": 1_000_000},
                {"field": "rsi", "operator": "above", "lookback": 14, "threshold": 0},
            ]},
            exit={"combinator": "and", "conditions": [
                {"field": "price", "operator": "below", "lookback": 1, "threshold": 1},
            ]},
        )
        self.assertEqual(strategy.entry["combinator"], "or")
        signals = strategy.generate(self.dataset)
        self.assertTrue(any(signal.target_weight == 1 for signal in signals[14:]))

        with self.assertRaisesRegex(QuantBacktestError, "快线 < 慢线"):
            self._strategy({
                "field": "macd", "operator": "above", "lookback": 12,
                "fast_period": 26, "slow_period": 12, "signal_period": 9, "threshold": 0,
            })
        with self.assertRaisesRegex(QuantBacktestError, "RSI 阈值"):
            self._strategy({"field": "rsi", "operator": "above", "lookback": 14, "threshold": 101})
        with self.assertRaisesRegex(QuantBacktestError, "and/or"):
            self._strategy({"field": "price", "operator": "above", "lookback": 1, "threshold": 1}, combinator="xor")


if __name__ == "__main__":
    unittest.main()
