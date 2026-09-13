"""Market-data adapters for local files and AKShare public endpoints."""

from __future__ import annotations

import csv
import math
import re
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Bar, ContextRecord, MarketDataset, QuantBacktestError, canonical_timestamp, parse_timestamp


_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "datetime", "date", "trade_date", "时间", "日期", "交易日期"),
    "open": ("open", "开盘", "开盘价"),
    "high": ("high", "最高", "最高价"),
    "low": ("low", "最低", "最低价"),
    "close": ("close", "收盘", "收盘价", "最新价"),
    "volume": ("volume", "vol", "成交量"),
}
_DATE_ONLY = re.compile(r"(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4}/\d{2}/\d{2})")
_MARKET_TIMEZONES: dict[str, str] = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "FUTURES": "Asia/Shanghai",
    "US": "America/New_York",
    "CRYPTO": "UTC",
    "FX": "UTC",
}


def _clean_header(value: Any) -> str:
    return str(value or "").strip().lower().replace("\ufeff", "")


def _resolve_columns(headers: Iterable[Any]) -> dict[str, str]:
    original_by_clean = {_clean_header(header): str(header) for header in headers}
    resolved: dict[str, str] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if _clean_header(alias) in original_by_clean:
                resolved[canonical] = original_by_clean[_clean_header(alias)]
                break
    missing = [name for name in ("timestamp", "open", "high", "low", "close") if name not in resolved]
    if missing:
        raise QuantBacktestError(f"行情缺少必要列: {', '.join(missing)}")
    return resolved


def _number(value: Any, *, field: str, timestamp: str, default: float | None = None) -> float:
    if value is None or str(value).strip() in {"", "-", "--", "null", "None", "nan", "NaN"}:
        if default is not None:
            return default
        raise QuantBacktestError(f"{timestamp} 的 {field} 为空")
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise QuantBacktestError(f"{timestamp} 的 {field} 无法转成数值: {value!r}") from exc
    if not math.isfinite(number):
        raise QuantBacktestError(f"{timestamp} 的 {field} 不是有限数值")
    return number


def rows_to_bars(
    rows: Sequence[Mapping[str, Any]],
    *,
    repair_ohlc: bool = True,
) -> tuple[tuple[Bar, ...], dict[str, Any]]:
    """Normalize tabular rows into sorted, unique bars.

    Some public feeds contain a handful of high/low invariant violations.  The
    original file remains untouched; with ``repair_ohlc=True`` only the runtime
    chart copy is expanded to include open and close, and the repair count is
    returned as auditable metadata.
    """

    if not rows:
        raise QuantBacktestError("行情数据为空")
    columns = _resolve_columns(rows[0].keys())
    used_columns = set(columns.values())
    by_timestamp: dict[str, Bar] = {}
    repaired = 0
    duplicate_count = 0
    for source in rows:
        raw_timestamp = source.get(columns["timestamp"])
        timestamp = canonical_timestamp(str(raw_timestamp or ""))
        opn = _number(source.get(columns["open"]), field="open", timestamp=timestamp)
        high = _number(source.get(columns["high"]), field="high", timestamp=timestamp)
        low = _number(source.get(columns["low"]), field="low", timestamp=timestamp)
        close = _number(source.get(columns["close"]), field="close", timestamp=timestamp)
        volume = _number(
            source.get(columns["volume"]) if "volume" in columns else None,
            field="volume",
            timestamp=timestamp,
            default=0.0,
        )
        expected_high, expected_low = max(opn, high, low, close), min(opn, high, low, close)
        if high != expected_high or low != expected_low:
            if not repair_ohlc:
                raise QuantBacktestError(f"{timestamp} 的 OHLC 关系不合法")
            high, low = expected_high, expected_low
            repaired += 1
        fields = {str(k): v for k, v in source.items() if str(k) not in used_columns}
        if timestamp in by_timestamp:
            duplicate_count += 1
        by_timestamp[timestamp] = Bar(timestamp, opn, high, low, close, volume, fields)
    try:
        bars = tuple(sorted(by_timestamp.values(), key=lambda item: parse_timestamp(item.timestamp)))
    except TypeError as exc:
        raise QuantBacktestError("行情时间戳不能混用带时区和不带时区格式") from exc
    if len(bars) < 2:
        raise QuantBacktestError("去重后至少需要 2 根 K 线")
    return bars, {
        "source_row_count": len(rows),
        "duplicate_timestamp_count": duplicate_count,
        "runtime_ohlc_repair_count": repaired,
    }


def select_bars_for_execution_window(
    bars: Sequence[Bar],
    *,
    start: str | None = None,
    end: str | None = None,
    market: str = "",
    timezone_name: str | None = None,
) -> tuple[tuple[Bar, ...], dict[str, Any]]:
    """Intersect sorted bars with one auditable execution window.

    Date-only bounds describe a full calendar day in an explicitly declared
    IANA timezone or, failing that, the market timezone. Exact datetimes retain
    instant semantics. Naive and offset-aware timestamps are never mixed.
    """
    available = tuple(bars)
    if not available:
        raise QuantBacktestError("行情数据为空")
    bar_times = tuple(parse_timestamp(bar.timestamp) for bar in available)

    def is_aware(value: datetime) -> bool:
        return value.tzinfo is not None and value.utcoffset() is not None

    aware_bars = is_aware(bar_times[0])
    if any(is_aware(value) != aware_bars for value in bar_times[1:]):
        raise QuantBacktestError("行情时间戳不能混用带时区和不带时区格式")

    requested_start = str(start).strip() if start is not None and str(start).strip() else None
    requested_end = str(end).strip() if end is not None and str(end).strip() else None
    explicit_timezone = str(timezone_name or "").strip()
    dataset_zone: tzinfo | None = None

    def resolve_dataset_zone() -> tzinfo:
        nonlocal dataset_zone
        if dataset_zone is not None:
            return dataset_zone
        resolved_name = explicit_timezone or _MARKET_TIMEZONES.get(str(market or "").strip().upper(), "")
        if resolved_name:
            try:
                dataset_zone = ZoneInfo(resolved_name)
            except ZoneInfoNotFoundError as exc:
                raise QuantBacktestError(f"行情数据契约的时区无效: {resolved_name}") from exc
        else:
            # A fixed offset remains a safe fallback for an aware legacy file.
            # An explicit IANA zone is preferable when daylight saving applies.
            dataset_zone = bar_times[0].tzinfo
        if dataset_zone is None:
            raise QuantBacktestError("带时区行情缺少可用于日期筛选的市场或数据契约时区")
        return dataset_zone

    def parse_bound(value: str | None, *, end_of_day: bool) -> tuple[datetime | None, bool]:
        if value is None:
            return None, False
        parsed = parse_timestamp(value)
        date_only = bool(_DATE_ONLY.fullmatch(value))
        if date_only:
            parsed = parsed.replace(
                hour=23 if end_of_day else 0,
                minute=59 if end_of_day else 0,
                second=59 if end_of_day else 0,
                microsecond=999999 if end_of_day else 0,
            )
            if aware_bars:
                parsed = parsed.replace(tzinfo=resolve_dataset_zone())
        if is_aware(parsed) != aware_bars:
            raise QuantBacktestError("行情时间与执行区间不能混用带时区和不带时区格式")
        return parsed, date_only

    lower, lower_is_date = parse_bound(requested_start, end_of_day=False)
    upper, upper_is_date = parse_bound(requested_end, end_of_day=True)
    if lower is not None and upper is not None and lower > upper:
        raise QuantBacktestError("执行区间开始时间不能晚于结束时间")

    available_start_time, available_end_time = bar_times[0], bar_times[-1]
    selected = tuple(
        bar
        for bar, timestamp in zip(available, bar_times)
        if (lower is None or timestamp >= lower) and (upper is None or timestamp <= upper)
    )
    available_range = f"{available[0].timestamp} 至 {available[-1].timestamp}"
    requested_range = f"{requested_start or '不限'} 至 {requested_end or '不限'}"
    if not selected:
        raise QuantBacktestError(
            f"请求执行区间 {requested_range} 与行情可用范围 {available_range} 无交集；"
            "请调整日期或导入覆盖该时段的行情"
        )
    if len(selected) < 2:
        raise QuantBacktestError(
            f"请求执行区间 {requested_range} 与行情相交后只有 {len(selected)} 根 K 线，至少需要 2 根；"
            f"行情可用范围为 {available_range}"
        )

    def date_before_available_start() -> bool:
        if lower is None:
            return False
        if not lower_is_date:
            return lower < available_start_time
        if aware_bars:
            zone = resolve_dataset_zone()
            return lower.date() < available_start_time.astimezone(zone).date()
        return lower.date() < available_start_time.date()

    def date_after_available_end() -> bool:
        if upper is None:
            return False
        if not upper_is_date:
            return upper > available_end_time
        if aware_bars:
            zone = resolve_dataset_zone()
            return upper.date() > available_end_time.astimezone(zone).date()
        return upper.date() > available_end_time.date()

    audit = {
        "requested_start": requested_start,
        "requested_end": requested_end,
        "available_start": available[0].timestamp,
        "available_end": available[-1].timestamp,
        "effective_start": selected[0].timestamp,
        "effective_end": selected[-1].timestamp,
        "window_clipped": date_before_available_start() or date_after_available_end(),
        "selected_bar_count": len(selected),
    }
    return selected, audit


def load_csv_dataset(
    path: str | Path,
    *,
    name: str | None = None,
    symbol: str,
    market: str,
    frequency: str = "1d",
    start: str | None = None,
    end: str | None = None,
    timezone_name: str | None = None,
    encoding: str = "utf-8-sig",
    repair_ohlc: bool = True,
) -> MarketDataset:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise QuantBacktestError(f"行情文件不存在: {source}")
    with source.open("r", encoding=encoding, newline="") as handle:
        rows = list(csv.DictReader(handle))
    bars, audit = rows_to_bars(rows, repair_ohlc=repair_ohlc)
    bars, window_audit = select_bars_for_execution_window(
        bars,
        start=start,
        end=end,
        market=market,
        timezone_name=timezone_name,
    )
    return MarketDataset(
        name=name or source.stem,
        symbol=symbol,
        market=market,
        frequency=frequency,
        bars=bars,
        source_type="local_csv",
        source_ref=str(source),
        metadata={**audit, **window_audit},
    )


def load_context_csv(
    path: str | Path,
    *,
    default_series: str = "external",
    encoding: str = "utf-8-sig",
) -> tuple[ContextRecord, ...]:
    """Load point-in-time model features.

    Required time column: ``available_at``/``timestamp``/``date``. Optional
    ``series`` groups independent feature snapshots (for example fundamentals,
    inventory, sentiment, or macro). All remaining columns reach the model.
    """

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise QuantBacktestError(f"外部特征文件不存在: {source}")
    with source.open("r", encoding=encoding, newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return ()
    headers = {_clean_header(key): key for key in rows[0].keys()}
    time_column = next(
        (headers[key] for key in ("available_at", "timestamp", "datetime", "date", "可用时间", "日期") if key in headers),
        None,
    )
    if not time_column:
        raise QuantBacktestError("外部特征必须包含 available_at/timestamp/date 列")
    series_column = headers.get("series") or headers.get("数据系列")
    records: list[ContextRecord] = []
    for row in rows:
        values = {str(k): v for k, v in row.items() if k not in {time_column, series_column}}
        records.append(
            ContextRecord(
                series=str(row.get(series_column) or default_series) if series_column else default_series,
                available_at=str(row.get(time_column) or ""),
                values=values,
            )
        )
    try:
        return tuple(sorted(records, key=lambda record: parse_timestamp(record.available_at)))
    except TypeError as exc:
        raise QuantBacktestError("外部特征时间戳不能混用带时区和不带时区格式") from exc


class AksharePublicDataSource:
    """Thin, lazy adapter over the public market-data functions in AKShare."""

    def fetch(
        self,
        *,
        asset_class: str,
        symbol: str,
        frequency: str = "1d",
        start: str = "19900101",
        end: str = "22220101",
        adjust: str = "",
        name: str | None = None,
        repair_ohlc: bool = True,
    ) -> MarketDataset:
        try:
            import akshare as ak
        except ImportError as exc:  # pragma: no cover - project dependency in production
            raise QuantBacktestError("公开行情需要安装 akshare") from exc

        asset = asset_class.strip().lower()
        freq = frequency.strip().lower()
        clean_symbol = symbol.lower().removeprefix("sh").removeprefix("sz").removeprefix("bj")
        try:
            if asset in {"equity", "stock", "a_share", "a-share"} and freq in {"1d", "daily", "day"}:
                frame = ak.stock_zh_a_hist(
                    symbol=clean_symbol,
                    period="daily",
                    start_date=start.replace("-", "")[:8],
                    end_date=end.replace("-", "")[:8],
                    adjust=adjust,
                )
                market = "CN"
            elif asset in {"equity", "stock", "a_share", "a-share"}:
                period = _minute_period(freq)
                frame = ak.stock_zh_a_hist_min_em(
                    symbol=clean_symbol,
                    start_date=_minute_bound(start, end=False),
                    end_date=_minute_bound(end, end=True),
                    period=period,
                    adjust=adjust,
                )
                market = "CN"
            elif asset in {"futures", "future"} and freq in {"1d", "daily", "day"}:
                frame = ak.futures_main_sina(
                    symbol=symbol.upper(),
                    start_date=start.replace("-", "")[:8],
                    end_date=end.replace("-", "")[:8],
                )
                market = "FUTURES"
            elif asset in {"futures", "future"}:
                period = _minute_period(freq)
                frame = ak.futures_zh_minute_sina(symbol=symbol.upper(), period=period)
                market = "FUTURES"
            else:
                raise QuantBacktestError(f"暂不支持的公开行情类型: {asset_class}/{frequency}")
        except QuantBacktestError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise QuantBacktestError(f"AKShare 获取 {symbol} 行情失败: {exc}") from exc

        records = frame.to_dict(orient="records")
        bars, audit = rows_to_bars(records, repair_ohlc=repair_ohlc)
        if asset in {"futures", "future"} and freq not in {"1d", "daily", "day"}:
            # Sina's minute API ignores a requested date range, so filter again.
            lower, upper = parse_timestamp(start), parse_timestamp(end)
            bars = tuple(bar for bar in bars if lower <= parse_timestamp(bar.timestamp) <= upper)
        metadata = {
            **audit,
            "provider": "akshare",
            "asset_class": asset,
            "adjust": adjust,
            "requested_start": start,
            "requested_end": end,
        }
        return MarketDataset(
            name=name or f"{symbol}-{frequency}",
            symbol=symbol.upper(),
            market=market,
            frequency=frequency,
            bars=bars,
            source_type="akshare_public",
            source_ref=f"akshare:{asset}:{symbol}:{frequency}",
            metadata=metadata,
        )


def _minute_period(frequency: str) -> str:
    raw = frequency.lower().replace("min", "m").removesuffix("m")
    if raw not in {"1", "5", "15", "30", "60"}:
        raise QuantBacktestError("分钟周期仅支持 1m/5m/15m/30m/60m")
    return raw


def _minute_bound(value: str, *, end: bool) -> str:
    raw = str(value).strip()
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if len(raw) <= 10:
        raw += " 23:59:59" if end else " 00:00:00"
    return raw
