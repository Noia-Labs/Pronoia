"""Read saved model outputs and align their information times for common replay.

These adapters never call a model, fetch prices, mutate a Run, or read Oracle
outcomes. The caller supplies the one frozen price series used by every model.
"""
from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from ..model_endpoint_security import redact_sensitive_text
from ..quant_backtest.models import Bar, MarketDataset
from .evaluation_protocol import MARKET_TIMEZONES, parse_event_datetime, resolve_run_execution_spec, select_events_for_execution_window
from .models import EventRecord
from .protocol import _DECISION_FIELDS

_DAY = {"1d", "d", "day", "daily"}
_AMBIGUOUS_CN = {"000001", "000016", "000300", "000688", "000852", "000905", "000985"}
_CORE_BAR = {"timestamp", "open", "high", "low", "close", "volume"}
_UTC = dt.timezone.utc


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _market(value: Any) -> str:
    name = str(value or "").strip().upper()
    name = {"USA": "US", "NYSE": "US", "NASDAQ": "US", "A_SHARE": "CN", "A-SHARE": "CN",
            "CHINA": "CN", "HONGKONG": "HK", "HONG_KONG": "HK", "FUT": "FUTURES", "FUTURE": "FUTURES"}.get(name, name)
    if name not in MARKET_TIMEZONES:
        raise ValueError("标的缺少可识别的市场，无法确定交易时区。")
    return name


def normalize_symbol(symbol: Any, market: Any) -> str:
    """Normalize explicit aliases without collapsing different exchanges."""
    market = _market(market)
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        raise ValueError("标的代码不能为空。")
    if market == "CN":
        match = re.fullmatch(r"(SH|SZ|BJ)[.:]?(\d{6})", symbol)
        if match:
            return f"{match[2]}.{match[1]}"
        match = re.fullmatch(r"(\d{6})[.](SH|SS|SZ|BJ)", symbol)
        if match:
            return f"{match[1]}.{'SH' if match[2] == 'SS' else match[2]}"
        if re.fullmatch(r"\d{6}", symbol):
            # A bare 000001/000300 can identify an index or a different exchange's
            # stock. Require an explicit suffix before matching those aliases.
            if symbol in _AMBIGUOUS_CN:
                return symbol
            if symbol.startswith(("6", "5", "900")):
                return symbol + ".SH"
            if symbol.startswith(("0", "3", "1", "2")):
                return symbol + ".SZ"
            if symbol.startswith(("4", "8", "920")):
                return symbol + ".BJ"
        raise ValueError("A 股或指数代码格式无法识别，请明确代码及交易所后缀。")
    if market == "HK":
        cleaned = re.sub(r"^HK[.:]?", "", symbol)
        cleaned = re.sub(r"[.]HK$", "", cleaned)
        if cleaned.isdigit() and len(cleaned) <= 5:
            return cleaned.zfill(5) + ".HK"
    # US share classes and futures contracts retain punctuation and maturities.
    # BRK.B/BRK-B, IF2506/IF0 are not assumed interchangeable.
    if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^/_:-]*", symbol):
        raise ValueError("标的代码格式无法识别。")
    return symbol


def _asset_key(symbol: Any, market: Any) -> str:
    return f"asset:{_market(market)}:{normalize_symbol(symbol, market)}"


def asset_identity(market: Any, symbol: Any) -> tuple[str, str]:
    """Public identity shared by catalogue and local market-snapshot lookup."""
    normalized_market = _market(market)
    return normalized_market, normalize_symbol(symbol, normalized_market)


def _frequency(value: Any) -> str:
    value = str(value or "").strip().lower()
    if value in _DAY:
        return "1d"
    if re.fullmatch(r"(?:1|5|15|30|60)(?:m|min)", value):
        return value.replace("min", "m")
    raise ValueError("共同重放目前只支持日 K 和明确周期的分钟 K。")


def _date_only(value: Any) -> bool:
    return bool(re.fullmatch(r"(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4}/\d{2}/\d{2})", str(value or "").strip()))


def _instant(value: Any, market: str, *, date_end: bool = False) -> dt.datetime:
    parsed = parse_event_datetime(value, market=market)
    if parsed is None:
        raise ValueError("缺少信息可得时间，无法进行共同重放。")
    if date_end and _date_only(value):
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    # Detect imaginary or ambiguous naive wall times rather than choosing an
    # arbitrary side of the US daylight-saving clock transition.
    raw = str(value or "").strip()
    try:
        original = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        original = None
    if original is not None and original.tzinfo is None and not _date_only(value):
        zone = ZoneInfo(MARKET_TIMEZONES[market])
        a, b = original.replace(tzinfo=zone, fold=0), original.replace(tzinfo=zone, fold=1)
        if a.utcoffset() != b.utcoffset():
            raise ValueError("该时间处于夏令时切换区间，请使用带 UTC 偏移的明确时间。")
        if a.astimezone(_UTC).astimezone(zone).replace(tzinfo=None) != original:
            raise ValueError("该本地时间不存在，请检查时区。")
    return parsed.astimezone(_UTC)


def bar_close_times(dataset: MarketDataset) -> list[dt.datetime]:
    market, frequency = _market(dataset.market), _frequency(dataset.frequency)
    closes = []
    for bar in dataset.bars:
        if frequency == "1d":
            local = _instant(bar.timestamp, market).astimezone(ZoneInfo(MARKET_TIMEZONES[market]))
            hour, minute = {"CN": (15, 0), "US": (16, 0), "HK": (16, 0), "FUTURES": (15, 0),
                            "CRYPTO": (23, 59), "FX": (23, 59)}[market]
            local = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            closes.append(local.astimezone(_UTC))
        else:
            if _date_only(bar.timestamp):
                raise ValueError("分钟 K 必须包含明确的收盘时间。")
            closes.append(_instant(bar.timestamp, market))
    if any(after <= before for before, after in zip(closes, closes[1:])):
        raise ValueError("行情收盘时间重复或未按时间排序。")
    return closes


def _read_json(path: Any, *, label: str) -> Any:
    try:
        source = Path(str(path or ""))
        if not source.is_file():
            raise ValueError("missing file")
        return json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label}文件缺失或损坏，无法读取已保存的结果。") from exc


def _read_rows(path: Any, *, label: str) -> list[dict[str, Any]]:
    try:
        source = Path(str(path or ""))
        if not source.is_file():
            raise ValueError("missing file")
        rows = [json.loads(line) for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        if not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError("empty or malformed rows")
        return rows
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label}文件缺失或损坏，无法读取已保存的结果。") from exc


def _quant(run: Mapping[str, Any]) -> bool:
    return str(run.get("engine_mode") or "") == "portfolio" or str(run.get("strategy_type") or "") in {"quant", "api", "signal_import"}


def _model_kind(run: Mapping[str, Any]) -> str:
    if _quant(run):
        return "quant"
    runner = str(run.get("runner") or _mapping(run.get("strategy_spec")).get("runner") or "")
    return {"provided_analysis": "imported_predictions", "raw_model": "external_model",
            "external_http": "external_service", "event_external_http": "external_service"}.get(runner, "pronoia")


def _public_identity(run: Mapping[str, Any]) -> dict[str, str]:
    return {"run_id": str(run.get("id") or run.get("run_id") or ""),
            "name": redact_sensitive_text(str(run.get("name") or run.get("model_version") or "未命名模型")),
            "model_kind": _model_kind(run)}


def _result(run: Mapping[str, Any]) -> dict[str, Any]:
    result = _mapping(run.get("result")) or _mapping(_read_json(run.get("result_path"), label="量化结果"))
    if not result:
        raise ValueError("量化结果格式无效。")
    version = str(run.get("dataset_version") or "")
    actual = str(_mapping(result.get("contract")).get("dataset_version") or "")
    if version and actual != version:
        raise ValueError("量化结果与运行记录的冻结数据版本不一致。")
    return result


def get_run_market(run: dict) -> MarketDataset:
    """Load actual frozen OHLC from a quant result, never an equity curve."""
    if not _quant(run):
        raise ValueError("该事件模型没有已保存的 OHLC 行情，请选择共同评测行情。")
    result = _result(run)
    summary = _mapping(result.get("dataset"))
    rows = result.get("bars")
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("量化结果未保存足够的真实 OHLC 行情。")
    try:
        bars = tuple(Bar(timestamp=row["timestamp"], open=row["open"], high=row["high"], low=row["low"],
                         close=row["close"], volume=row.get("volume", 0),
                         fields={key: value for key, value in row.items() if key not in _CORE_BAR}) for row in rows)
        dataset = MarketDataset(name=str(summary.get("name") or "共同评测行情"), symbol=str(summary.get("symbol") or ""),
                                market=str(summary.get("market") or ""), frequency=str(summary.get("frequency") or ""),
                                bars=bars, source_type="saved_quant_result", metadata=_mapping(summary.get("metadata")))
        _asset_key(dataset.symbol, dataset.market)
        bar_close_times(dataset)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"已保存行情无法用于共同重放：{redact_sensitive_text(exc)}") from exc
    fingerprint = str(summary.get("fingerprint") or "")
    if fingerprint and fingerprint != dataset.fingerprint:
        raise ValueError("量化结果中的行情内容与冻结指纹不一致。")
    metadata_version = str(_mapping(summary.get("metadata")).get("dataset_version") or "")
    if run.get("dataset_version") and metadata_version and metadata_version != str(run["dataset_version"]):
        raise ValueError("行情元数据与运行记录的冻结数据版本不一致。")
    return dataset


def _event_key(row: Mapping[str, Any]) -> str:
    market = _market(row.get("market"))
    event = _event_record(row)
    facts = {key: value for key, value in row.items() if key not in _DECISION_FIELDS | {"event_id", "id", "text", "url", "event_type", "available_at"} and not key.startswith("analysis_")}
    facts.update({"market": market, "symbol": normalize_symbol(event.symbol, market), "title": event.title,
                  "event_text": event.event_text, "source_url": event.source_url, "event_type_l2": event.event_type_l2,
                  "event_time": _instant(row.get("event_time") or event.event_time, market).isoformat(),
                  "available_time": _instant(event.available_time or event.event_time, market, date_end=True).isoformat(),
                  "occurred_at": _instant(event.occurred_at or event.event_time, market).isoformat()})
    try:
        encoded = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ValueError("事件事实包含无法比较的数据。") from exc
    return "event:" + hashlib.sha256(encoded).hexdigest()


def _event_record(row: Mapping[str, Any]) -> EventRecord:
    return EventRecord.from_dict({key: value for key, value in row.items() if key not in _DECISION_FIELDS and not key.startswith("analysis_")})


def _events(run: Mapping[str, Any]) -> list[tuple[dict[str, Any], EventRecord]]:
    rows = _read_rows(run.get("events_path"), label="事件")
    pairs = [(row, _event_record(row)) for row in rows]
    ids = [event.event_id for _, event in pairs]
    if any(not event_id for event_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("事件文件包含缺失或重复的事件 ID。")
    selected = select_events_for_execution_window([event for _, event in pairs], resolve_run_execution_spec(run))
    return [(row, event) for row, event in pairs if event.event_id in selected.event_ids]


def _event_subject(row: Mapping[str, Any], event: EventRecord) -> dict[str, Any]:
    market = _market(event.market)
    key = _event_key(row)
    return {"key": key, "event_key": key, "kind": "event", "title": event.title,
            "symbol": normalize_symbol(event.symbol, market), "market": market,
            "event_time": event.event_time,
            "available_time": _instant(event.available_time or event.event_time, market, date_end=True).isoformat()}


def describe_run(run: dict) -> dict:
    identity = _public_identity(run)
    subjects: dict[str, dict[str, Any]] = {}
    if _quant(run):
        dataset = get_run_market(run)
        key = _asset_key(dataset.symbol, dataset.market)
        subject = {"key": key, "kind": "asset", "title": normalize_symbol(dataset.symbol, dataset.market),
                   "symbol": normalize_symbol(dataset.symbol, dataset.market), "market": _market(dataset.market)}
        return {**identity, "subjects": [subject], "market_source": {"symbol": subject["symbol"], "market": subject["market"],
                "frequency": _frequency(dataset.frequency), "start_at": dataset.bars[0].timestamp, "end_at": dataset.bars[-1].timestamp}}
    for row, event in _events(run):
        subject = _event_subject(row, event)
        subjects[subject["key"]] = subject
        key = _asset_key(event.symbol, event.market)
        subjects[key] = {"key": key, "kind": "asset", "title": subject["symbol"],
                         "symbol": subject["symbol"], "market": subject["market"]}
    return {**identity, "subjects": list(subjects.values())}


def get_event_record(run: dict, event_key: str) -> EventRecord:
    if _quant(run):
        raise ValueError("量化结果不包含事件记录。")
    matches = [event for row, event in _events(run) if _event_key(row) == event_key]
    if len(matches) != 1:
        raise ValueError("该运行中未找到唯一对应的事件事实。")
    return matches[0]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(float(value)) else None
    except (ValueError, OverflowError):
        return None


def _event_observations(run: Mapping[str, Any], target: Mapping[str, Any], dataset: MarketDataset, warnings: list[str]) -> list[dict[str, Any]]:
    predictions = _read_rows(run.get("out_path"), label="事件预测")
    by_id: dict[str, dict[str, Any]] = {}
    for prediction in predictions:
        event_id = str(prediction.get("event_id") or "")
        if not event_id or event_id in by_id:
            raise ValueError("预测文件包含缺失或重复的事件 ID。")
        by_id[event_id] = prediction
    result = []
    for row, event in _events(run):
        if _asset_key(event.symbol, event.market) != _asset_key(target.get("symbol"), target.get("market")):
            continue
        if target.get("kind") == "event" and _event_key(row) != target.get("key"):
            continue
        prediction = by_id.get(event.event_id)
        if not prediction or prediction.get("abstain") is True:
            continue
        direction = str(prediction.get("pred_direction") or prediction.get("direction") or "").strip().lower()
        if direction not in {"up", "down", "neutral"}:
            continue
        forecast = _finite(prediction.get("expected_return_pct", _mapping(prediction.get("strategy_metadata")).get("expected_return_pct")))
        horizon = str(prediction.get("horizon") or "").lower()
        horizon_bars = int(horizon[1:]) if _frequency(dataset.frequency) == "1d" and re.fullmatch(r"t[1-9][0-9]*", horizon) else None
        if forecast is not None and horizon_bars is None:
            warnings.append("事件收益率预测使用日线窗口，与当前 K 线周期不能直接换算；仅参与持仓重放，不计算收益率误差。")
        if _date_only(event.available_time or event.event_time):
            warnings.append("部分事件只有日期，按当地日末可得处理，随后等待下一根可用收盘 K 线。")
        result.append({"_available_at": _instant(event.available_time or event.event_time, _market(event.market), date_end=True),
                       "direction": direction, "expected_return_pct": forecast, "horizon_bars": horizon_bars,
                       "direction_basis": "market_excess", "source": {"kind": "event_prediction", "event_id": event.event_id}})
    return result


def _quant_observations(run: Mapping[str, Any], target: Mapping[str, Any], dataset: MarketDataset, warnings: list[str]) -> list[dict[str, Any]]:
    source_market = get_run_market(dict(run))
    if _asset_key(source_market.symbol, source_market.market) != _asset_key(target.get("symbol"), target.get("market")):
        return []
    result = _result(run)
    signals = result.get("signals")
    if not isinstance(signals, list):
        raise ValueError("量化结果没有保存模型信号。")
    prediction_only = bool(_mapping(result.get("strategy")).get("prediction_only")) or str(_mapping(result.get("strategy")).get("kind") or "") == "return_forecast"
    closes = bar_close_times(source_market)
    index_by_time = {bar.timestamp: index for index, bar in enumerate(source_market.bars)}
    entries = []
    for signal in signals:
        if not isinstance(signal, dict) or str(signal.get("timestamp") or "") not in index_by_time:
            raise ValueError("量化信号时间不在其原始冻结行情中。")
        index = index_by_time[str(signal["timestamp"])]
        forecast = _finite(signal.get("expected_return_pct"))
        raw_horizon = signal.get("horizon_bars")
        horizon = int(raw_horizon) if isinstance(raw_horizon, int) and not isinstance(raw_horizon, bool) and raw_horizon > 0 else None
        if forecast is not None and horizon is None:
            raise ValueError("量化收益预测缺少明确的预测周期。")
        if prediction_only and forecast is None:
            continue  # forecast warmup weight=0 is not a real neutral prediction
        weight = _finite(signal.get("target_weight"))
        if forecast is not None:
            direction, basis = ("up" if forecast > 0 else "down" if forecast < 0 else "neutral"), "asset_return"
        elif weight is not None:
            direction, basis = ("up" if weight > 0 else "down" if weight < 0 else "neutral"), "target_weight"
        else:
            continue
        aligned_horizon = horizon if _frequency(source_market.frequency) == _frequency(dataset.frequency) else None
        if forecast is not None and aligned_horizon is None:
            warnings.append("量化预测与共同行情的 K 线周期不同，不换算预测期限，不计算收益率误差。")
        # For event comparisons a snapshot cannot be carried forward forever.
        # Numeric forecasts expire at their own horizon; target-weight-only
        # signals are considered fresh for one source bar.
        expires = closes[min(index + (horizon or 1), len(closes) - 1)]
        entries.append({"_available_at": closes[index], "_forecast_start": closes[index], "_expires_at": expires,
                        "direction": direction, "expected_return_pct": forecast, "horizon_bars": aligned_horizon,
                        "direction_basis": basis, "source": {"kind": "quant_signal", "timestamp": signal["timestamp"],
                        "frequency": _frequency(source_market.frequency)}})
    if target.get("kind") == "event":
        event_time = _instant(target.get("available_time") or target.get("event_time"), _market(target.get("market")), date_end=True)
        candidates = [entry for entry in entries if entry["_available_at"] <= event_time < entry["_expires_at"]]
        if not candidates:
            raise ValueError("该事件发生前没有仍在有效期内的量化预测，无法参加本次事件对比。")
        latest_time = max(entry["_available_at"] for entry in candidates)
        entries = [entry for entry in candidates if entry["_available_at"] == latest_time]
        for entry in entries:
            entry["source"]["original_available_at"] = entry["_available_at"].isoformat()
            entry["_available_at"] = event_time
        warnings.append("事件对比使用事件可得时刻之前最近且未过期的量化信号；按事件时刻统一等待收盘，再于下一根开盘执行。")
    return entries


def load_contestant(run: dict, target: dict, dataset: MarketDataset) -> dict:
    if target.get("kind") not in {"event", "asset"}:
        raise ValueError("请选择事件或标的作为共同评测对象。")
    target_asset = _asset_key(target.get("symbol"), target.get("market"))
    if target_asset != _asset_key(dataset.symbol, dataset.market):
        raise ValueError("共同行情与评测对象的市场或标的不一致。")
    if target.get("kind") == "asset" and target.get("key") != target_asset:
        raise ValueError("评测对象的代码与标的标识不一致。")
    warnings: list[str] = []
    observations = (_quant_observations if _quant(run) else _event_observations)(run, target, dataset, warnings)
    closes = bar_close_times(dataset)
    by_bar: dict[int, list[dict[str, Any]]] = {}
    for observation in observations:
        available = observation["_available_at"]
        index = bisect.bisect_left(closes, available)
        if index >= len(closes):
            continue
        # A signal from before the selected replay period is not silently
        # carried into its first day. The first daily candle admits same-day
        # pre-open events; intraday inputs use its actual start boundary.
        first_local = closes[0].astimezone(ZoneInfo(MARKET_TIMEZONES[_market(dataset.market)]))
        lower = first_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(_UTC) if _frequency(dataset.frequency) == "1d" else closes[0] - dt.timedelta(minutes=int(_frequency(dataset.frequency)[:-1]))
        if available < lower:
            continue
        by_bar.setdefault(index, []).append(observation)
    selected = []
    for index, entries in sorted(by_bar.items()):
        latest = max(entry["_available_at"] for entry in entries)
        newest = [entry for entry in entries if entry["_available_at"] == latest]
        signatures = {(entry["direction"], entry["expected_return_pct"], entry["horizon_bars"], entry["direction_basis"]) for entry in newest}
        if len(signatures) > 1:
            raise ValueError("同一标的在同一可得时刻存在相互冲突的预测，请先明确合并规则。")
        if len(entries) > 1:
            warnings.append("同一根 K 线收盘前有多条判断，采用信息可得时间最新的一条；同刻相同判断合并。")
        entry = newest[0]
        horizon = entry["horizon_bars"]
        if entry.get("_forecast_start") is not None and entry["_forecast_start"] != closes[index] and entry["expected_return_pct"] is not None:
            horizon = None
            warnings.append("量化收益预测的原始起算时点早于共同重放时点；保留其仓位判断，不将不同起点的数值用于收益率误差比较。")
        selected.append({"bar_index": index, "direction": entry["direction"], "expected_return_pct": entry["expected_return_pct"],
                         "horizon_bars": horizon, "direction_basis": entry["direction_basis"],
                         "source": {**entry["source"], "available_at": latest.isoformat()}})
    if not any(item["bar_index"] < len(dataset.bars) - 1 for item in selected):
        raise ValueError("该模型在共同区间内没有可于下一根开盘执行的有效预测。")
    return {**_public_identity(run), "observations": selected, "warnings": list(dict.fromkeys(warnings))}
