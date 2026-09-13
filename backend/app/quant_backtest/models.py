"""Typed contracts shared by the quantitative backtest modules."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping


class QuantBacktestError(ValueError):
    """A user-actionable dataset, strategy, or execution-contract error."""


def parse_timestamp(value: str | date | datetime) -> datetime:
    """Parse a market timestamp without silently changing its timezone."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        raw = str(value).strip()
        if not raw:
            raise QuantBacktestError("行情时间不能为空")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            for fmt in ("%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise QuantBacktestError(f"无法解析行情时间: {value!r}") from exc
    return parsed


def canonical_timestamp(value: str | date | datetime) -> str:
    parsed = parse_timestamp(value)
    if parsed.hour == parsed.minute == parsed.second == parsed.microsecond == 0 and parsed.tzinfo is None:
        return parsed.date().isoformat()
    return parsed.isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


@dataclass(frozen=True)
class Bar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", canonical_timestamp(self.timestamp))
        for name in ("open", "high", "low", "close", "volume"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise QuantBacktestError(f"{self.timestamp} 的 {name} 不是有限数值")
            object.__setattr__(self, name, value)
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise QuantBacktestError(f"{self.timestamp} 的价格必须大于 0")
        if self.volume < 0:
            raise QuantBacktestError(f"{self.timestamp} 的成交量不能为负")
        # Bad OHLC rows make PnL and candlestick rendering ambiguous, so fail
        # loudly instead of repairing vendor data inside a backtest.
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise QuantBacktestError(f"{self.timestamp} 的 OHLC 关系不合法")
        object.__setattr__(self, "fields", {str(k): _json_safe(v) for k, v in dict(self.fields).items()})

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            **dict(self.fields),
        }


@dataclass(frozen=True)
class ContextRecord:
    """One point-in-time non-price observation available to a model."""

    series: str
    available_at: str
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not str(self.series).strip():
            raise QuantBacktestError("上下文 series 不能为空")
        object.__setattr__(self, "series", str(self.series).strip())
        object.__setattr__(self, "available_at", canonical_timestamp(self.available_at))
        object.__setattr__(self, "values", {str(k): _json_safe(v) for k, v in dict(self.values).items()})

    def to_dict(self) -> dict[str, Any]:
        return {"series": self.series, "available_at": self.available_at, "values": dict(self.values)}


@dataclass(frozen=True)
class Signal:
    timestamp: str
    target_weight: float
    score: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Explicit asset-return forecasts use percent, not a decimal return or a
    # portfolio weight.  Existing strategies may leave both fields absent.
    expected_return_pct: float | None = None
    horizon_bars: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", canonical_timestamp(self.timestamp))
        weight = float(self.target_weight)
        if not math.isfinite(weight):
            raise QuantBacktestError(f"{self.timestamp} 的目标仓位不是有限数值")
        object.__setattr__(self, "target_weight", weight)
        if self.score is not None:
            object.__setattr__(self, "score", float(self.score))
        if (self.expected_return_pct is None) != (self.horizon_bars is None):
            raise QuantBacktestError("收益预测必须同时提供 expected_return_pct 和 horizon_bars")
        if self.expected_return_pct is not None:
            try:
                forecast = float(self.expected_return_pct)
            except (TypeError, ValueError) as exc:
                raise QuantBacktestError("expected_return_pct 必须是有限数值（单位 %）") from exc
            if isinstance(self.expected_return_pct, bool) or not math.isfinite(forecast) or forecast < -100:
                raise QuantBacktestError("expected_return_pct 必须是大于等于 -100 的有限数值（单位 %）")
            horizon = positive_bar_count(self.horizon_bars, name="horizon_bars")
            object.__setattr__(self, "expected_return_pct", forecast)
            object.__setattr__(self, "horizon_bars", horizon)
        object.__setattr__(self, "metadata", {str(k): _json_safe(v) for k, v in dict(self.metadata).items()})

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "target_weight": self.target_weight,
            "score": self.score,
            "metadata": dict(self.metadata),
            "expected_return_pct": self.expected_return_pct,
            "horizon_bars": self.horizon_bars,
        }


def positive_bar_count(value: Any, *, name: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QuantBacktestError(f"{name} 必须是 1..10000 的整数（K 线数）") from exc
    if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer() or not 1 <= number <= 10_000:
        raise QuantBacktestError(f"{name} 必须是 1..10000 的整数（K 线数）")
    return int(number)


@dataclass(frozen=True)
class MarketDataset:
    name: str
    symbol: str
    market: str
    frequency: str
    bars: tuple[Bar, ...]
    source_type: str = "local_csv"
    source_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if len(self.bars) < 2:
            raise QuantBacktestError("量化回测至少需要 2 根 K 线")
        ordered = tuple(sorted(self.bars, key=lambda bar: parse_timestamp(bar.timestamp)))
        timestamps = [bar.timestamp for bar in ordered]
        if len(timestamps) != len(set(timestamps)):
            raise QuantBacktestError("行情数据存在重复时间戳")
        object.__setattr__(self, "bars", ordered)
        object.__setattr__(self, "metadata", {str(k): _json_safe(v) for k, v in dict(self.metadata).items()})
        if not self.fingerprint:
            payload = {
                "symbol": self.symbol,
                "market": self.market,
                "frequency": self.frequency,
                "bars": [bar.to_dict() for bar in ordered],
            }
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            object.__setattr__(self, "fingerprint", hashlib.sha256(raw.encode("utf-8")).hexdigest())

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "symbol": self.symbol,
            "market": self.market,
            "frequency": self.frequency,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "bar_count": len(self.bars),
            "start_at": self.bars[0].timestamp,
            "end_at": self.bars[-1].timestamp,
            "fingerprint": self.fingerprint,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ExecutionConfig:
    initial_capital: float = 1_000_000.0
    commission_bps: float = 3.0
    slippage_bps: float = 2.0
    stamp_duty_bps: float = 0.0
    other_cost_bps: float = 0.0
    minimum_commission: float = 0.0
    max_abs_weight: float = 1.0
    allow_short: bool = True
    annualization_periods: int | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.initial_capital)) or self.initial_capital <= 0:
            raise QuantBacktestError("初始资金必须大于 0")
        cost_values = (
            float(self.commission_bps), float(self.slippage_bps),
            float(self.stamp_duty_bps), float(self.other_cost_bps),
            float(self.minimum_commission),
        )
        if not all(math.isfinite(value) for value in cost_values) or min(cost_values) < 0:
            raise QuantBacktestError("手续费、滑点、印花税、其他费用和最低佣金必须是非负有限数值")
        if not 0 < float(self.max_abs_weight) <= 10:
            raise QuantBacktestError("最大绝对仓位必须在 (0, 10] 内")
        if self.annualization_periods is not None and self.annualization_periods <= 0:
            raise QuantBacktestError("年化周期数必须大于 0")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BacktestResult:
    dataset: Mapping[str, Any]
    strategy: Mapping[str, Any]
    execution: Mapping[str, Any]
    metrics: Mapping[str, Any]
    bars: tuple[Mapping[str, Any], ...]
    signals: tuple[Mapping[str, Any], ...]
    equity_curve: tuple[Mapping[str, Any], ...]
    drawdown_curve: tuple[Mapping[str, Any], ...]
    trades: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...] = ()
    return_forecasts: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": dict(self.dataset),
            "strategy": dict(self.strategy),
            "execution": dict(self.execution),
            "metrics": dict(self.metrics),
            "bars": [dict(x) for x in self.bars],
            "signals": [dict(x) for x in self.signals],
            "equity_curve": [dict(x) for x in self.equity_curve],
            "drawdown_curve": [dict(x) for x in self.drawdown_curve],
            "trades": [dict(x) for x in self.trades],
            "warnings": list(self.warnings),
            "return_forecasts": [dict(x) for x in self.return_forecasts],
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
