"""Canonical event-backtest evaluation settings.

This module is deliberately small and dependency-light so routes, the runner and
Arena all resolve the *same* horizon, event universe and transaction-cost model.
Keeping those rules in one place prevents a chart from silently describing a
different experiment than the metrics used for ranking.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


SUPPORTED_EVALUATION_HORIZONS: tuple[str, ...] = (
    "t1", "t3", "t5", "t7", "t15", "t30", "t60",
)

MARKET_TIMEZONES: dict[str, str] = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "FUTURES": "Asia/Shanghai",
    "US": "America/New_York",
    "CRYPTO": "UTC",
    "FX": "UTC",
}


def normalize_evaluation_horizon(value: Any, *, default: str = "t3") -> str:
    """Return a supported numeric Oracle horizon or raise a user-facing error."""
    candidate = str(value or default).strip().lower().replace("t+", "t")
    if candidate not in SUPPORTED_EVALUATION_HORIZONS:
        supported = ", ".join(SUPPORTED_EVALUATION_HORIZONS)
        raise ValueError(f"不支持的评价窗口 {value!r}；仅支持 {supported}（例如 t20 会被拒绝）")
    return candidate


def resolve_run_execution_spec(run: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(run, Mapping):
        return {}
    direct = run.get("execution_spec")
    if isinstance(direct, Mapping):
        return dict(direct)
    config = run.get("config")
    if isinstance(config, Mapping):
        nested = config.get("execution_spec")
        if isinstance(nested, Mapping):
            return dict(nested)
    return {}


def resolve_run_horizon(run: Mapping[str, Any] | None, *, default: str = "t3") -> str:
    """Resolve the frozen run horizon, including legacy config spellings."""
    if not isinstance(run, Mapping):
        return normalize_evaluation_horizon(default)
    direct = run.get("evaluation_horizon")
    if direct:
        return normalize_evaluation_horizon(direct, default=default)
    config = run.get("config")
    if isinstance(config, Mapping):
        for key in ("evaluation_horizon", "primary_oracle_horizon", "horizon"):
            if config.get(key):
                return normalize_evaluation_horizon(config[key], default=default)
        for container_key in ("evaluation_protocol", "protocol"):
            protocol = config.get(container_key)
            if isinstance(protocol, Mapping):
                for key in ("evaluation_horizon", "primary_oracle_horizon", "horizon"):
                    if protocol.get(key):
                        return normalize_evaluation_horizon(protocol[key], default=default)
    spec = resolve_run_execution_spec(run)
    if spec.get("holding_horizon"):
        return normalize_evaluation_horizon(spec["holding_horizon"], default=default)
    return normalize_evaluation_horizon(default)


def resolve_run_oracle_epsilon(run: Mapping[str, Any] | None) -> float:
    """Read the frozen Oracle threshold without reclassifying stored labels.

    An explicit zero is meaningful. Never infer a historical Run's threshold
    from the current dataset catalogue or a subsequently replaced label file.
    Runs predating this setting retain the historical 0.5% declaration.
    """
    if not isinstance(run, Mapping):
        return 0.005
    config = run.get("config")
    config = config if isinstance(config, Mapping) else {}
    for container in (config.get("evaluation_protocol"), run.get("evaluation_protocol"), config):
        if not isinstance(container, Mapping) or container.get("oracle_epsilon") is None:
            continue
        value = container["oracle_epsilon"]
        try:
            epsilon = float(value)
        except (TypeError, ValueError):
            raise ValueError("冻结的 oracle_epsilon 必须是非负有限数值") from None
        if isinstance(value, bool) or not math.isfinite(epsilon) or epsilon < 0:
            raise ValueError("冻结的 oracle_epsilon 必须是非负有限数值")
        return epsilon
    return 0.005


def parse_event_datetime(value: Any, *, market: str = "", field_name: str = "event_time") -> dt.datetime | None:
    """Parse one event timestamp and normalize it to its declared market clock.

    Offset-aware inputs are converted to the exchange timezone before deriving
    a calendar date. Naive inputs are interpreted in that market's timezone,
    rather than silently as UTC.
    """
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        parsed = None
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y%m%d"):
            try:
                parsed = dt.datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"{field_name} 不是有效 ISO 日期/时间: {value!r}") from exc
    timezone_name = MARKET_TIMEZONES.get(str(market or "").strip().upper(), "UTC")
    timezone = ZoneInfo(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    else:
        parsed = parsed.astimezone(timezone)
    return parsed


def _parse_date(value: Any, *, field_name: str, market: str = "") -> dt.date | None:
    parsed = parse_event_datetime(value, market=market, field_name=field_name)
    return parsed.date() if parsed is not None else None


def validate_execution_window(execution_spec: Mapping[str, Any] | None) -> tuple[dt.date | None, dt.date | None]:
    spec = execution_spec if isinstance(execution_spec, Mapping) else {}
    start = _parse_date(spec.get("start_date"), field_name="start_date")
    end = _parse_date(spec.get("end_date"), field_name="end_date")
    if start is not None and end is not None and start > end:
        raise ValueError("start_date 不能晚于 end_date")
    return start, end


def _event_value(event: Any, key: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key, None)


def event_information_time(event: Any) -> Any:
    """Return the strategy-observable timestamp, with legacy fallback."""
    return _event_value(event, "available_time") or _event_value(event, "event_time")


def _chronology_key(event: Any) -> tuple[int, float, str, str]:
    raw = str(event_information_time(event) or "")
    event_id = str(_event_value(event, "event_id") or "")
    market = str(_event_value(event, "market") or "")
    if raw:
        try:
            parsed = parse_event_datetime(raw, market=market)
            if parsed is None:
                raise ValueError("missing event timestamp")
            return 0, parsed.timestamp(), raw, event_id
        except ValueError:
            pass
    return 1, 0.0, raw, event_id


def chronological_event_ids(events: Sequence[Any] | Iterable[Any]) -> list[str]:
    """Stable event chronology shared by metrics, performance and Arena."""
    return [
        str(_event_value(event, "event_id") or "")
        for event in sorted(list(events), key=_chronology_key)
        if str(_event_value(event, "event_id") or "")
    ]


@dataclass(frozen=True)
class EventSelection:
    events: list[Any]
    event_ids: set[str]
    active: bool
    start_date: str | None
    end_date: str | None
    before_count: int
    after_count: int
    missing_time_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "before_count": self.before_count,
            "after_count": self.after_count,
            "excluded_count": self.before_count - self.after_count,
            "missing_time_count": self.missing_time_count,
            "time_field": "available_time (fallback: event_time)",
        }


def select_events_for_execution_window(
    events: Sequence[Any] | Iterable[Any],
    execution_spec: Mapping[str, Any] | None,
) -> EventSelection:
    """Apply the frozen inclusive date window to EventRecords or dictionaries."""
    materialized = list(events)
    start, end = validate_execution_window(execution_spec)
    active = start is not None or end is not None
    selected: list[Any] = []
    missing = 0
    for event in materialized:
        raw_time = event_information_time(event)
        market = str(_event_value(event, "market") or "")
        try:
            event_date = _parse_date(raw_time, field_name="available_time/event_time", market=market)
        except ValueError:
            event_date = None
        if event_date is None:
            missing += 1
            if active:
                continue
        if start is not None and event_date is not None and event_date < start:
            continue
        if end is not None and event_date is not None and event_date > end:
            continue
        selected.append(event)
    event_ids = {
        str(_event_value(event, "event_id") or "")
        for event in selected
        if str(_event_value(event, "event_id") or "")
    }
    return EventSelection(
        events=selected,
        event_ids=event_ids,
        active=active,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat() if end else None,
        before_count=len(materialized),
        after_count=len(selected),
        missing_time_count=missing,
    )


def _finite_nonnegative(value: Any, *, field_name: str, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数值") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} 必须是有限非负数")
    return number


def event_proxy_cost_spec(execution_spec: Mapping[str, Any] | None) -> dict[str, float]:
    """Resolve the event proxy's explicit one-round-trip friction assumption."""
    spec = execution_spec if isinstance(execution_spec, Mapping) else {}
    fee_bps = _finite_nonnegative(spec.get("fee_bps"), field_name="fee_bps", default=0.0)
    slippage_bps = _finite_nonnegative(
        spec.get("slippage_bps"), field_name="slippage_bps", default=0.0
    )
    initial_value = _finite_nonnegative(
        spec.get("initial_capital", spec.get("initial_value")),
        field_name="initial_capital",
        default=1_000_000.0,
    )
    if initial_value <= 0:
        raise ValueError("initial_capital 必须大于 0")
    round_trip_cost_bps = fee_bps + slippage_bps
    return {
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "round_trip_cost_bps": round_trip_cost_bps,
        "round_trip_cost_rate": round_trip_cost_bps / 10_000.0,
        "initial_value": initial_value,
    }
