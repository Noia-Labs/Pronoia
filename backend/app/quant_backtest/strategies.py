"""Built-in, file-based, and external HTTP quantitative strategies."""

from __future__ import annotations

import csv
import json
import math
import socket
import statistics
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

from ..model_endpoint_security import (
    ValidatedModelRedirectHandler,
    allow_private_model_endpoints,
    redact_sensitive_text,
    validate_runtime_model_endpoint,
)
from ..strategy_credentials import resolve_strategy_secret_reference, validate_strategy_secret_reference, validate_header_name

from .models import (
    Bar,
    ContextRecord,
    MarketDataset,
    QuantBacktestError,
    Signal,
    canonical_timestamp,
    parse_timestamp,
    positive_bar_count,
)
from .return_forecast import FORECAST_CONTRACT, HistoricalMeanReturnForecast


def _allow_private_model_endpoints() -> bool:
    return allow_private_model_endpoints()


def _validate_runtime_http_endpoint(endpoint: str) -> None:
    """Resolve immediately before a request to prevent DNS-rebinding SSRF."""
    try:
        validate_runtime_model_endpoint(
            endpoint,
            label="外部模型",
            resolver=socket.getaddrinfo,
            allow_private=_allow_private_model_endpoints(),
        )
    except ValueError as exc:
        raise QuantBacktestError(str(exc)) from exc


class _ValidatedRedirectHandler(ValidatedModelRedirectHandler):
    """Apply the same runtime endpoint policy to each portfolio redirect."""

    def __init__(self, *, sensitive_headers: set[str] | frozenset[str] = frozenset()):
        super().__init__(_validate_runtime_http_endpoint, sensitive_headers=sensitive_headers)


class QuantStrategy(Protocol):
    def describe(self) -> Mapping[str, Any]: ...

    def generate(
        self,
        dataset: MarketDataset,
        *,
        context: Sequence[ContextRecord] = (),
    ) -> Sequence[Signal]: ...


class BuyAndHoldStrategy:
    """Enter at the second bar open (after the first close signal), then hold."""

    def describe(self) -> Mapping[str, Any]:
        return {"kind": "buy_hold", "name": "买入并持有", "version": "1", "parameters": {}}

    def generate(
        self,
        dataset: MarketDataset,
        *,
        context: Sequence[ContextRecord] = (),
    ) -> Sequence[Signal]:
        del context
        return (Signal(dataset.bars[0].timestamp, 1.0, metadata={"rule": "enter_next_open"}),)


class MovingAverageCrossStrategy:
    """Small deterministic baseline useful for smoke tests and comparison."""

    def __init__(self, short_window: int = 5, long_window: int = 20, *, short_weight: float = -1.0):
        if short_window < 1 or long_window <= short_window:
            raise QuantBacktestError("均线参数必须满足 1 <= short_window < long_window")
        self.short_window = int(short_window)
        self.long_window = int(long_window)
        self.short_weight = float(short_weight)

    def describe(self) -> Mapping[str, Any]:
        return {
            "kind": "ma_cross",
            "name": f"MA{self.short_window}/MA{self.long_window}",
            "version": "1",
            "parameters": {
                "short_window": self.short_window,
                "long_window": self.long_window,
                "short_weight": self.short_weight,
            },
        }

    def generate(
        self,
        dataset: MarketDataset,
        *,
        context: Sequence[ContextRecord] = (),
    ) -> Sequence[Signal]:
        del context
        closes = [bar.close for bar in dataset.bars]
        signals: list[Signal] = []
        for index, bar in enumerate(dataset.bars):
            if index + 1 < self.long_window:
                weight, short_ma, long_ma = 0.0, None, None
            else:
                short_ma = sum(closes[index + 1 - self.short_window : index + 1]) / self.short_window
                long_ma = sum(closes[index + 1 - self.long_window : index + 1]) / self.long_window
                weight = 1.0 if short_ma > long_ma else self.short_weight
            signals.append(
                Signal(
                    timestamp=bar.timestamp,
                    target_weight=weight,
                    score=(short_ma / long_ma - 1.0) if short_ma is not None and long_ma else None,
                    metadata={"short_ma": short_ma, "long_ma": long_ma},
                )
            )
        return signals


class MomentumStrategy:
    """Long positive lookback momentum and optionally short negative momentum."""

    def __init__(self, lookback: int = 20, *, negative_weight: float = -1.0):
        if lookback < 1:
            raise QuantBacktestError("动量回看周期必须大于 0")
        self.lookback = int(lookback)
        self.negative_weight = float(negative_weight)

    def describe(self) -> Mapping[str, Any]:
        return {
            "kind": "momentum",
            "name": f"{self.lookback} 周期动量",
            "version": "1",
            "parameters": {"lookback": self.lookback, "negative_weight": self.negative_weight},
        }

    def generate(
        self,
        dataset: MarketDataset,
        *,
        context: Sequence[ContextRecord] = (),
    ) -> Sequence[Signal]:
        del context
        output: list[Signal] = []
        for index, bar in enumerate(dataset.bars):
            if index < self.lookback:
                momentum, weight = None, 0.0
            else:
                momentum = bar.close / dataset.bars[index - self.lookback].close - 1.0
                weight = 1.0 if momentum > 0 else self.negative_weight
            output.append(Signal(bar.timestamp, weight, score=momentum, metadata={"momentum": momentum}))
        return output


class DeclarativeRuleStrategy:
    """Long/flat close-time rule builder with next-open execution.

    Factors use only observations available at the current bar close.
    """

    _FIELDS = {
        "price",
        "bar_return",
        "return",
        "moving_average",
        "ma_cross",
        "breakout",
        "amplitude",
        "volume",  # v1 compatibility: relative volume minus one
        "volume_ratio",
        "volatility",
        "rsi",
        "bollinger_position",
        "macd",
    }
    _OPERATORS = {"above", "below", "crosses_above", "crosses_below"}

    def __init__(self, *, entry: Mapping[str, Any], exit: Mapping[str, Any], version: str = "1"):
        self.entry = self._validate_group(entry, name="entry")
        self.exit = self._validate_group(exit, name="exit")
        self.version = str(version or "1")

    @classmethod
    def _validate_group(cls, group: Mapping[str, Any], *, name: str) -> dict[str, Any]:
        if not isinstance(group, Mapping):
            raise QuantBacktestError(f"{name} 必须是规则对象")
        combinator = str(group.get("combinator") or "and").strip().lower()
        if combinator not in {"and", "all", "or", "any"}:
            raise QuantBacktestError(f"{name}.combinator 仅支持 and/or")
        raw_rules = group.get("conditions") if isinstance(group.get("conditions"), list) else group.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise QuantBacktestError(f"{name}.conditions 至少需要 1 条")
        if len(raw_rules) > 50:
            raise QuantBacktestError(f"{name}.conditions 最多 50 条")
        rules: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_rules, start=1):
            if not isinstance(raw, Mapping):
                raise QuantBacktestError(f"{name} 第 {index} 条规则必须是对象")
            field = str(raw.get("field") or "").strip().lower()
            operator = str(raw.get("operator") or "").strip().lower()
            if field not in cls._FIELDS:
                raise QuantBacktestError(f"{name} 第 {index} 条不支持 field={field or '(empty)'}")
            if operator not in cls._OPERATORS:
                raise QuantBacktestError(f"{name} 第 {index} 条不支持 operator={operator or '(empty)'}")
            try:
                lookback = int(raw.get("lookback", 1))
                threshold = float(raw.get("threshold", 0.0))
            except (TypeError, ValueError, OverflowError) as exc:
                raise QuantBacktestError(f"{name} 第 {index} 条 lookback/threshold 无效") from exc
            if not 1 <= lookback <= 10_000:
                raise QuantBacktestError(f"{name} 第 {index} 条 lookback 必须在 1..10000")
            if not math.isfinite(threshold):
                raise QuantBacktestError(f"{name} 第 {index} 条 threshold 必须是有限数值")
            normalized: dict[str, Any] = {
                "field": field,
                "operator": operator,
                "lookback": lookback,
                "threshold": threshold,
            }
            raw_count = raw.get("consecutive_count", 1)
            try:
                count = float(raw_count)
            except (TypeError, ValueError, OverflowError) as exc:
                raise QuantBacktestError(f"{name} 第 {index} 条连续满足次数必须是 1..10000 的整数") from exc
            if isinstance(raw_count, bool) or not math.isfinite(count) or not count.is_integer() or not 1 <= count <= 10_000:
                raise QuantBacktestError(f"{name} 第 {index} 条连续满足次数必须是 1..10000 的整数")
            if count > 1:
                # Omission preserves the serialized identity of existing rules.
                normalized["consecutive_count"] = int(count)
            if field in {"ma_cross", "macd"}:
                try:
                    fast_period = int(raw.get("fast_period", max(1, lookback // 2)))
                    slow_period = int(raw.get("slow_period", lookback))
                except (TypeError, ValueError) as exc:
                    raise QuantBacktestError(f"{name} 第 {index} 条 fast_period/slow_period 无效") from exc
                if not 1 <= fast_period < slow_period <= 10_000:
                    raise QuantBacktestError(f"{name} 第 {index} 条周期必须满足 1 <= 快线 < 慢线 <= 10000")
                normalized.update({"fast_period": fast_period, "slow_period": slow_period, "lookback": slow_period})
            if field == "macd":
                try:
                    signal_period = int(raw.get("signal_period", 9))
                except (TypeError, ValueError) as exc:
                    raise QuantBacktestError(f"{name} 第 {index} 条 signal_period 无效") from exc
                if not 1 <= signal_period <= 10_000:
                    raise QuantBacktestError(f"{name} 第 {index} 条 signal_period 必须在 1..10000")
                normalized["signal_period"] = signal_period
            if field == "rsi" and not 0 <= threshold <= 100:
                raise QuantBacktestError(f"{name} 第 {index} 条 RSI 阈值必须在 0..100")
            if field in {"volume_ratio", "amplitude", "volatility"} and threshold < 0:
                raise QuantBacktestError(f"{name} 第 {index} 条 {field} 阈值不能为负")
            normalized["warmup_bars"] = cls._warmup_bars(normalized)
            rules.append(normalized)
        return {"combinator": "or" if combinator in {"or", "any"} else "and", "conditions": rules}

    @staticmethod
    def _warmup_bars(rule: Mapping[str, Any]) -> int:
        field = str(rule["field"])
        lookback = int(rule["lookback"])
        if field == "price":
            return 0
        if field == "bar_return":
            return 1
        if field == "macd":
            return int(rule["slow_period"]) + int(rule["signal_period"]) - 2
        if field == "ma_cross":
            return int(rule["slow_period"]) - 1
        if field in {"moving_average", "amplitude", "bollinger_position"}:
            return lookback - 1
        return lookback

    def describe(self) -> Mapping[str, Any]:
        return {
            "kind": "declarative_rules",
            "name": "通用条件策略",
            "version": self.version,
            "parameters": {"entry": self.entry, "exit": self.exit},
            "decision_timing": "bar_close",
            "execution_timing": "next_open",
            "position_model": "long_or_flat",
            "factor_contract": "public_technical_factors-v2",
        }

    @staticmethod
    def _factor_series(dataset: MarketDataset, rule: Mapping[str, Any]) -> list[float | None]:
        bars = dataset.bars
        size = len(bars)
        lookback = int(rule["lookback"])
        field = str(rule["field"])
        closes = [bar.close for bar in bars]
        close_prefix = [0.0]
        square_prefix = [0.0]
        for close in closes:
            close_prefix.append(close_prefix[-1] + close)
            square_prefix.append(square_prefix[-1] + close * close)

        def rolling_mean(end: int, window: int) -> float | None:
            start = end + 1 - window
            if start < 0:
                return None
            return (close_prefix[end + 1] - close_prefix[start]) / window

        if field == "price":
            return list(closes)
        if field == "bar_return":
            return [None] + [closes[index] / closes[index - 1] - 1.0 for index in range(1, size)]
        if field == "moving_average":
            output: list[float | None] = []
            for index in range(size):
                mean = rolling_mean(index, lookback)
                output.append(closes[index] / mean - 1.0 if mean else None)
            return output
        if field == "ma_cross":
            fast_period = int(rule["fast_period"])
            slow_period = int(rule["slow_period"])
            output = []
            for index in range(size):
                fast = rolling_mean(index, fast_period)
                slow = rolling_mean(index, slow_period)
                output.append(fast / slow - 1.0 if fast is not None and slow else None)
            return output
        if field == "return":
            return [
                None if index < lookback else closes[index] / closes[index - lookback] - 1.0
                for index in range(size)
            ]
        if field == "breakout":
            operator = str(rule["operator"])
            output = []
            for index in range(size):
                if index < lookback:
                    output.append(None)
                    continue
                prior = bars[index - lookback:index]
                reference = max(bar.high for bar in prior) if operator in {"above", "crosses_above"} else min(bar.low for bar in prior)
                output.append(closes[index] / reference - 1.0 if reference else None)
            return output
        if field == "amplitude":
            output = []
            for index in range(size):
                if index + 1 < lookback:
                    output.append(None)
                    continue
                window = bars[index + 1 - lookback:index + 1]
                highest = max(bar.high for bar in window)
                lowest = min(bar.low for bar in window)
                output.append(highest / lowest - 1.0 if lowest else None)
            return output
        if field == "volume":
            # Kept only so older frozen v1 protocols remain replayable.
            ratio = DeclarativeRuleStrategy._factor_series(dataset, {**dict(rule), "field": "volume_ratio"})
            return [None if item is None else item - 1.0 for item in ratio]
        if field == "volume_ratio":
            volume_prefix = [0.0]
            for bar in bars:
                volume_prefix.append(volume_prefix[-1] + bar.volume)
            output = []
            for index in range(size):
                if index < lookback:
                    output.append(None)
                    continue
                average = (volume_prefix[index] - volume_prefix[index - lookback]) / lookback
                output.append(bars[index].volume / average if average > 0 else None)
            return output
        if field == "volatility":
            returns = [0.0] + [closes[index] / closes[index - 1] - 1.0 for index in range(1, size)]
            return_prefix = [0.0]
            return_square_prefix = [0.0]
            for item in returns:
                return_prefix.append(return_prefix[-1] + item)
                return_square_prefix.append(return_square_prefix[-1] + item * item)
            output = []
            for index in range(size):
                if index < lookback:
                    output.append(None)
                    continue
                start = index - lookback + 1
                count = lookback
                total = return_prefix[index + 1] - return_prefix[start]
                total_square = return_square_prefix[index + 1] - return_square_prefix[start]
                variance = max(0.0, total_square / count - (total / count) ** 2)
                output.append(math.sqrt(variance))
            return output
        if field == "rsi":
            gains = [0.0]
            losses = [0.0]
            for index in range(1, size):
                change = closes[index] - closes[index - 1]
                gains.append(max(change, 0.0))
                losses.append(max(-change, 0.0))
            gain_prefix = [0.0]
            loss_prefix = [0.0]
            for gain, loss in zip(gains, losses):
                gain_prefix.append(gain_prefix[-1] + gain)
                loss_prefix.append(loss_prefix[-1] + loss)
            output = []
            for index in range(size):
                if index < lookback:
                    output.append(None)
                    continue
                start = index - lookback + 1
                average_gain = (gain_prefix[index + 1] - gain_prefix[start]) / lookback
                average_loss = (loss_prefix[index + 1] - loss_prefix[start]) / lookback
                if average_gain == average_loss == 0:
                    output.append(50.0)
                elif average_loss == 0:
                    output.append(100.0)
                else:
                    relative_strength = average_gain / average_loss
                    output.append(100.0 - 100.0 / (1.0 + relative_strength))
            return output
        if field == "bollinger_position":
            output = []
            for index in range(size):
                start = index + 1 - lookback
                if start < 0:
                    output.append(None)
                    continue
                mean = (close_prefix[index + 1] - close_prefix[start]) / lookback
                second_moment = (square_prefix[index + 1] - square_prefix[start]) / lookback
                deviation = math.sqrt(max(0.0, second_moment - mean * mean))
                output.append((closes[index] - mean) / deviation if deviation > 0 else 0.0)
            return output
        if field == "macd":
            fast_period = int(rule["fast_period"])
            slow_period = int(rule["slow_period"])
            signal_period = int(rule["signal_period"])

            def ema(period: int) -> list[float]:
                alpha = 2.0 / (period + 1.0)
                values = [closes[0]]
                for close in closes[1:]:
                    values.append(alpha * close + (1.0 - alpha) * values[-1])
                return values

            fast_ema = ema(fast_period)
            slow_ema = ema(slow_period)
            macd_line = [fast - slow for fast, slow in zip(fast_ema, slow_ema)]
            signal_alpha = 2.0 / (signal_period + 1.0)
            signal_line = [macd_line[0]]
            for value in macd_line[1:]:
                signal_line.append(signal_alpha * value + (1.0 - signal_alpha) * signal_line[-1])
            warmup = int(rule["warmup_bars"])
            return [
                None if index < warmup else (macd_line[index] - signal_line[index]) / closes[index]
                for index in range(size)
            ]
        raise QuantBacktestError(f"不支持的规则因子: {field}")

    @staticmethod
    def _matches(index: int, rule: Mapping[str, Any], values: Sequence[float | None]) -> tuple[bool, dict[str, Any]]:
        current = values[index]
        previous = values[index - 1] if index > 0 else None
        threshold = float(rule["threshold"])
        operator = str(rule["operator"])
        matched = False
        if current is not None:
            if operator == "above":
                matched = current > threshold
            elif operator == "below":
                matched = current < threshold
            elif operator == "crosses_above" and previous is not None:
                matched = previous <= threshold < current
            elif operator == "crosses_below" and previous is not None:
                matched = previous >= threshold > current
        audit = {**dict(rule), "value": current, "previous_value": previous, "matched": matched}
        return matched, audit

    @classmethod
    def _iter_confirmed_matches(
        cls, rule: Mapping[str, Any], values: Sequence[float | None],
    ) -> Iterator[tuple[bool, dict[str, Any]]]:
        """Confirm each condition causally in one pass, before AND/OR groups.

        A crossing starts a streak at the crossing bar; staying strictly on
        its new side confirms it once on the Nth close. Level comparisons
        stay matched after N consecutive qualifying closes.
        """
        required = int(rule.get("consecutive_count", 1))
        operator = str(rule["operator"])
        crossing = operator in {"crosses_above", "crosses_below"}
        threshold = float(rule["threshold"])
        count = 0
        for index, current in enumerate(values):
            immediate, audit = cls._matches(index, rule, values)
            if crossing:
                same_side = current is not None and (
                    current > threshold if operator == "crosses_above" else current < threshold)
                if immediate:
                    count = 1
                elif count and same_side:
                    count += 1
                else:
                    count = 0
                matched = count == required
            else:
                count = count + 1 if immediate else 0
                matched = count >= required
            audit.update(matched=matched, confirmation_count=count, required_count=required)
            yield matched, audit

    @classmethod
    def _confirmed_matches(
        cls, rule: Mapping[str, Any], values: Sequence[float | None],
    ) -> list[tuple[bool, dict[str, Any]]]:
        """Materialized helper for inspecting an individual factor in tests."""
        return list(cls._iter_confirmed_matches(rule, values))

    def generate(
        self,
        dataset: MarketDataset,
        *,
        context: Sequence[ContextRecord] = (),
    ) -> Sequence[Signal]:
        del context
        target = 0.0
        signals: list[Signal] = []
        entry_matches = [self._iter_confirmed_matches(rule, self._factor_series(dataset, rule)) for rule in self.entry["conditions"]]
        exit_matches = [self._iter_confirmed_matches(rule, self._factor_series(dataset, rule)) for rule in self.exit["conditions"]]
        for bar in dataset.bars:
            entry_checks = [next(checks) for checks in entry_matches]
            exit_checks = [next(checks) for checks in exit_matches]
            entry_match = (all if self.entry["combinator"] == "and" else any)(item[0] for item in entry_checks)
            exit_match = (all if self.exit["combinator"] == "and" else any)(item[0] for item in exit_checks)
            previous_target = target
            if target > 0 and exit_match:
                target = 0.0
            elif target == 0 and entry_match:
                target = 1.0
            signals.append(Signal(
                bar.timestamp,
                target,
                metadata={
                    "rule_engine": "declarative_rules-v2",
                    "factor_contract": "public_technical_factors-v2",
                    "entry_match": entry_match,
                    "exit_match": exit_match,
                    "state_changed": target != previous_target,
                    "entry_audit": [item[1] for item in entry_checks],
                    "exit_audit": [item[1] for item in exit_checks],
                },
            ))
        return signals


class PrecomputedSignalStrategy:
    def __init__(self, signals: Sequence[Signal], *, name: str = "导入信号", version: str = "1"):
        if not signals:
            raise QuantBacktestError("导入信号为空")
        self.signals = tuple(signals)
        self.name = name
        self.version = version

    def describe(self) -> Mapping[str, Any]:
        return {"kind": "precomputed", "name": self.name, "version": self.version, "parameters": {}}

    def generate(
        self,
        dataset: MarketDataset,
        *,
        context: Sequence[ContextRecord] = (),
    ) -> Sequence[Signal]:
        del dataset, context
        return self.signals


class PrecomputedReturnForecastStrategy(PrecomputedSignalStrategy):
    """Keep imported numerical forecasts without turning them into positions."""

    def __init__(self, signals: Sequence[Signal], *, horizon_bars: int, name: str = "导入收益率预测", version: str = "1"):
        horizon = positive_bar_count(horizon_bars, name="horizon_bars")
        provided = [signal for signal in signals if signal.expected_return_pct is not None]
        if not provided:
            raise QuantBacktestError("收益预测文件未提供 expected_return_pct 与 horizon_bars")
        if any(signal.horizon_bars != horizon for signal in provided):
            raise QuantBacktestError("收益预测文件的 horizon_bars 必须与本次预测周期一致")
        super().__init__([
            Signal(signal.timestamp, 0.0, metadata=signal.metadata,
                   expected_return_pct=signal.expected_return_pct, horizon_bars=signal.horizon_bars)
            for signal in signals
        ], name=name, version=version)
        self.horizon_bars = horizon

    def describe(self) -> Mapping[str, Any]:
        return {
            "kind": "return_forecast", "source": "signal_file", "name": self.name, "version": self.version,
            "prediction_only": True, "point_in_time_enforced": False,
            "forecast_contract": FORECAST_CONTRACT, "parameters": {"horizon_bars": self.horizon_bars},
        }


def load_signal_csv(path: str | Path) -> tuple[Signal, ...]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise QuantBacktestError(f"信号文件不存在: {source}")
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise QuantBacktestError("信号文件为空")
    headers = {str(key).strip().lower(): key for key in rows[0].keys()}
    timestamp_key = next((headers[x] for x in ("timestamp", "datetime", "date", "时间", "日期") if x in headers), None)
    weight_key = next((headers[x] for x in ("target_weight", "weight", "position", "目标仓位") if x in headers), None)
    forecast_key = headers.get("expected_return_pct")
    horizon_key = headers.get("horizon_bars")
    if bool(forecast_key) != bool(horizon_key):
        raise QuantBacktestError("收益预测文件必须同时包含 expected_return_pct 和 horizon_bars 列")
    if not timestamp_key or not (weight_key or forecast_key):
        raise QuantBacktestError("信号文件必须包含 timestamp/date 和 target_weight/weight，或显式收益预测两列")
    output = []
    for row in rows:
        forecast = row.get(forecast_key) if forecast_key else None
        horizon = row.get(horizon_key) if horizon_key else None
        forecast = None if forecast in (None, "") else forecast
        horizon = None if horizon in (None, "") else horizon
        try:
            weight = float(row[weight_key]) if weight_key else 0.0
        except (TypeError, ValueError) as exc:
            raise QuantBacktestError(f"无法解析目标仓位: {row.get(weight_key)!r}") from exc
        metadata = {str(k): v for k, v in row.items() if k not in {timestamp_key, weight_key, forecast_key, horizon_key}}
        output.append(Signal(str(row[timestamp_key]), weight, metadata=metadata,
                             expected_return_pct=forecast, horizon_bars=horizon))
    return tuple(output)


def align_context_asof(
    bars: Sequence[Bar],
    records: Sequence[ContextRecord],
) -> list[dict[str, Any]]:
    """As-of join non-price features, preventing post-decision observations."""

    if not records:
        return [{"timestamp": bar.timestamp, "series": {}} for bar in bars]
    try:
        ordered = sorted(records, key=lambda record: parse_timestamp(record.available_at))
        latest: dict[str, ContextRecord] = {}
        cursor = 0
        snapshots: list[dict[str, Any]] = []
        for bar in bars:
            bar_time = parse_timestamp(bar.timestamp)
            while cursor < len(ordered) and parse_timestamp(ordered[cursor].available_at) <= bar_time:
                latest[ordered[cursor].series] = ordered[cursor]
                cursor += 1
            snapshots.append(
                {
                    "timestamp": bar.timestamp,
                    "series": {
                        series: {"available_at": record.available_at, **dict(record.values)}
                        for series, record in sorted(latest.items())
                    },
                }
            )
        return snapshots
    except TypeError as exc:
        raise QuantBacktestError("K 线与外部特征不能混用带时区和不带时区格式") from exc


class ExternalHTTPStrategy:
    """Call a user-managed model through an explicitly unverified contract.

    The endpoint currently receives the complete canonical bar history in one
    request plus aligned external series. Although returned weights execute at
    the next bar open, the platform cannot prove the remote model generated each
    weight without observing later bars. Runs therefore remain exploratory until
    a per-decision point-in-time request contract exists.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        name: str = "外部模型",
        version: str = "external-v1",
        timeout_seconds: float = 30.0,
        headers: Mapping[str, str] | None = None,
        parameters: Mapping[str, Any] | None = None,
        max_bars: int = 20_000,
    ):
        parsed = urlparse(str(endpoint).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise QuantBacktestError("外部模型 endpoint 必须是有效的 http(s) 地址")
        if not 0 < float(timeout_seconds) <= 300:
            raise QuantBacktestError("外部模型超时必须在 (0, 300] 秒内")
        self.endpoint = str(endpoint).strip()
        self.name = name
        self.version = version
        self.timeout_seconds = float(timeout_seconds)
        self.headers = dict(headers or {})
        try:
            for key, value in self.headers.items():
                validate_header_name(str(key))
                validate_strategy_secret_reference(
                    value,
                    label=f"外部模型请求头 {key}",
                )
        except ValueError as exc:
            raise QuantBacktestError(
                str(exc)
            ) from exc
        self.parameters = dict(parameters or {})
        self.max_bars = int(max_bars)

    def describe(self) -> Mapping[str, Any]:
        # Deliberately omit header values: they may contain credentials.
        return {
            "kind": "external_http",
            "name": self.name,
            "version": self.version,
            "endpoint": self.endpoint,
            "parameters": self.parameters,
            "header_names": sorted(self.headers),
            "point_in_time_enforced": False,
            "result_nature": "unverified",
        }

    def generate(
        self,
        dataset: MarketDataset,
        *,
        context: Sequence[ContextRecord] = (),
    ) -> Sequence[Signal]:
        if len(dataset.bars) > self.max_bars:
            raise QuantBacktestError(
                f"外部模型单次最多接收 {self.max_bars} 根 K 线，当前为 {len(dataset.bars)} 根"
            )
        payload = {
            "schema_version": "pronoia.quant-model.v1",
            "decision_timing": "signal_at_bar_close_execute_next_bar_open",
            "point_in_time_enforced": False,
            "dataset": dataset.summary(),
            "bars": [bar.to_dict() for bar in dataset.bars],
            "context": align_context_asof(dataset.bars, context),
            "parameters": self.parameters,
        }
        response_obj = self._post(payload)
        signal_rows = response_obj.get("signals") if isinstance(response_obj, dict) else None
        if not isinstance(signal_rows, list) or not signal_rows:
            raise QuantBacktestError("外部模型响应必须包含非空 signals 数组")
        valid_timestamps = {bar.timestamp for bar in dataset.bars}
        signals: list[Signal] = []
        seen: set[str] = set()
        for row in signal_rows:
            if not isinstance(row, dict):
                raise QuantBacktestError("signals 的每一项必须是 JSON 对象")
            timestamp = canonical_timestamp(str(row.get("timestamp") or ""))
            if timestamp not in valid_timestamps:
                raise QuantBacktestError(f"外部模型返回了数据集外的时间戳: {timestamp}")
            if timestamp in seen:
                raise QuantBacktestError(f"外部模型返回重复信号: {timestamp}")
            seen.add(timestamp)
            try:
                weight = float(row["target_weight"])
            except (KeyError, TypeError, ValueError) as exc:
                raise QuantBacktestError(f"{timestamp} 缺少合法 target_weight") from exc
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            signals.append(Signal(timestamp, weight, score=row.get("score"), metadata=metadata,
                                  expected_return_pct=row.get("expected_return_pct"), horizon_bars=row.get("horizon_bars")))
        return signals

    def _post(self, payload: Mapping[str, Any]) -> Any:
        # Resolve each request, including each point-in-time forecast; DNS may
        # change after strategy registration or between requests.
        _validate_runtime_http_endpoint(self.endpoint)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        for key, value in self.headers.items():
            try:
                request_headers[validate_header_name(str(key))] = resolve_strategy_secret_reference(
                    value,
                    label=f"外部模型请求头 {key}",
                )
            except ValueError as exc:
                raise QuantBacktestError(str(exc)) from exc
        request = Request(self.endpoint, data=raw, headers=request_headers, method="POST")
        try:
            sensitive_headers = set(request_headers) - {"Accept", "Content-Type"}
            opener = build_opener(_ValidatedRedirectHandler(sensitive_headers=sensitive_headers))
            with opener.open(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - every hop is validated
                response_raw = response.read(10_000_001)
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            sensitive_values = [
                value for key, value in request_headers.items()
                if key not in {"Accept", "Content-Type"}
            ]
            raise QuantBacktestError(
                redact_sensitive_text(f"外部模型返回 HTTP {exc.code}: {detail}", *sensitive_values)
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            sensitive_values = [
                value for key, value in request_headers.items()
                if key not in {"Accept", "Content-Type"}
            ]
            raise QuantBacktestError(
                redact_sensitive_text(f"外部模型连接失败: {exc}", *sensitive_values)
            ) from exc
        if len(response_raw) > 10_000_000:
            raise QuantBacktestError("外部模型响应超过 10 MB")
        try:
            response_obj = json.loads(response_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QuantBacktestError("外部模型没有返回合法 JSON") from exc
        return response_obj


class ExternalReturnForecastStrategy(ExternalHTTPStrategy):
    """Request one numerical forecast with only information available at t."""

    def __init__(self, endpoint: str, *, horizon_bars: int = 3, lookback: int = 20, **kwargs: Any):
        super().__init__(endpoint, **kwargs)
        self.horizon_bars = positive_bar_count(horizon_bars, name="horizon_bars")
        self.lookback = positive_bar_count(lookback, name="lookback")

    def describe(self) -> Mapping[str, Any]:
        return {
            **dict(super().describe()), "kind": "return_forecast", "source": "external_http",
            "prediction_only": True, "point_in_time_enforced": True,
            "result_nature": "evaluated_from_real_bars", "forecast_contract": FORECAST_CONTRACT,
            "parameters": {**self.parameters, "horizon_bars": self.horizon_bars, "lookback": self.lookback},
        }

    def generate(self, dataset: MarketDataset, *, context: Sequence[ContextRecord] = ()) -> Sequence[Signal]:
        if len(dataset.bars) > self.max_bars:
            raise QuantBacktestError(f"外部收益预测最多处理 {self.max_bars} 根 K 线")
        contexts = align_context_asof(dataset.bars, context)
        output: list[Signal] = []
        for index, bar in enumerate(dataset.bars):
            history = dataset.bars[max(0, index + 1 - self.lookback):index + 1]
            # No full-dataset metadata, fingerprints, future end dates, or extra
            # CSV fields (which may contain future labels) enter this request.
            payload = {
                "schema_version": FORECAST_CONTRACT,
                "as_of": bar.timestamp,
                "symbol": dataset.symbol, "market": dataset.market, "frequency": dataset.frequency,
                "horizon_bars": self.horizon_bars,
                "return_unit": "percent", "target": "asset_close_to_close_return",
                "point_in_time_enforced": True,
                "bars": [
                    {key: getattr(item, key) for key in ("timestamp", "open", "high", "low", "close", "volume")}
                    for item in history
                ],
                "context": contexts[index], "parameters": self.parameters,
            }
            row = self._post(payload)
            if not isinstance(row, dict) or "expected_return_pct" not in row or "horizon_bars" not in row:
                raise QuantBacktestError("收益预测响应必须包含 expected_return_pct（可为 null）和 horizon_bars")
            if positive_bar_count(row["horizon_bars"], name="horizon_bars") != self.horizon_bars:
                raise QuantBacktestError("外部模型返回的 horizon_bars 与请求不一致")
            if row.get("timestamp") is not None and canonical_timestamp(row["timestamp"]) != bar.timestamp:
                raise QuantBacktestError("外部收益预测的 timestamp 必须等于本次 as_of")
            forecast = row["expected_return_pct"]
            output.append(Signal(
                bar.timestamp, 0.0, expected_return_pct=forecast,
                horizon_bars=self.horizon_bars if forecast is not None else None,
                metadata={"information_cutoff": bar.timestamp, "forecast_status": "provided" if forecast is not None else "abstained"},
            ))
        return output


def strategy_from_spec(spec: Mapping[str, Any]) -> QuantStrategy:
    kind = str(spec.get("kind") or "").strip().lower()
    params = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {}
    if kind == "return_forecast":
        source = str(spec.get("source") or spec.get("adapter") or "builtin")
        horizon = positive_bar_count(params.get("horizon_bars", 3), name="horizon_bars")
        if source == "signal_file":
            return PrecomputedReturnForecastStrategy(
                load_signal_csv(str(spec.get("path") or "")), horizon_bars=horizon,
                name=str(spec.get("name") or "导入收益率预测"), version=str(spec.get("version") or "1"),
            )
        if source == "external_http":
            return ExternalReturnForecastStrategy(
                str(spec.get("endpoint") or ""), horizon_bars=horizon, lookback=params.get("lookback", 20),
                name=str(spec.get("name") or "外部收益率预测"), version=str(spec.get("version") or "1"),
                timeout_seconds=float(spec.get("timeout_seconds", 30)),
                headers=spec.get("headers") if isinstance(spec.get("headers"), dict) else {},
                parameters=params, max_bars=int(spec.get("max_bars", 20_000)),
            )
        if source != "builtin":
            raise QuantBacktestError("收益预测 source 仅支持 builtin、signal_file 或 external_http")
        if str(params.get("method") or "historical_mean") != "historical_mean":
            raise QuantBacktestError("内置收益预测当前仅支持 historical_mean")
        return HistoricalMeanReturnForecast(lookback=params.get("lookback", 20), horizon_bars=horizon)
    if kind == "buy_hold":
        return BuyAndHoldStrategy()
    if kind == "ma_cross":
        return MovingAverageCrossStrategy(
            short_window=int(params.get("short_window", 5)),
            long_window=int(params.get("long_window", 20)),
            short_weight=float(params.get("short_weight", -1.0)),
        )
    if kind == "momentum":
        return MomentumStrategy(
            lookback=int(params.get("lookback", 20)),
            negative_weight=float(params.get("negative_weight", -1.0)),
        )
    if kind == "declarative_rules":
        entry = params.get("entry") if isinstance(params.get("entry"), dict) else {}
        exit_group = params.get("exit") if isinstance(params.get("exit"), dict) else {}
        return DeclarativeRuleStrategy(entry=entry, exit=exit_group, version=str(spec.get("version") or "1"))
    if kind == "signal_file":
        return PrecomputedSignalStrategy(
            load_signal_csv(str(spec.get("path") or "")),
            name=str(spec.get("name") or "导入信号"),
            version=str(spec.get("version") or "1"),
        )
    if kind == "external_http":
        headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else {}
        return ExternalHTTPStrategy(
            str(spec.get("endpoint") or ""),
            name=str(spec.get("name") or "外部模型"),
            version=str(spec.get("version") or "external-v1"),
            timeout_seconds=float(spec.get("timeout_seconds", 30)),
            headers=headers,
            parameters=params,
            max_bars=int(spec.get("max_bars", 20_000)),
        )
    raise QuantBacktestError(f"不支持的量化策略类型: {kind or '(empty)'}")
