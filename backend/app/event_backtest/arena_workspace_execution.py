"""Execute saved model definitions on one Arena input without changing defaults.

All outputs use close-time observations; the common replay owns next-open
execution and costs. Missing model answers stay missing, never neutral scores.
"""
from __future__ import annotations

import asyncio
import bisect
import copy
import json
import math
import statistics
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import config, db
from ..llm import model_profile_context
from ..quant_backtest.models import Bar, MarketDataset, Signal
from ..quant_backtest.strategies import strategy_from_spec
from ..quant_backtest.return_forecast import FORECAST_CONTRACT
from .application import load_predictions
from .cancellation import BacktestCancelled
from .engine import run_team_full_trajectory
from .evaluation_protocol import (
    chronological_event_ids,
    event_proxy_cost_spec,
    normalize_evaluation_horizon,
)
from .external_strategy import run_external_event_strategy
from .metrics_registry import compute_all_metrics
from .model_comparison_adapters import (
    _event_key, _instant, asset_identity, bar_close_times, get_event_record,
    get_run_market, load_contestant,
)
from .models import ALL_HORIZONS, EventLabel, EventRecord, TeamPrediction
from .raw_model import run_raw_model

MAX_MODEL_PREDICTIONS = 100
_DAILY = {"1d", "d", "daily", "day"}
EVENT_RESULT_SCHEMA = "arena-event-comparison-v1"
UNIFIED_RECORD_SCHEMA = "arena-result-record-v1"


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise BacktestCancelled("Arena 已取消，未完成的预测不会计为零分。")


def _integer(settings: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or not low <= value <= high:
        raise ValueError(f"{key} 必须是 {low}–{high} 的整数。")
    return int(value)


def _horizon(settings: Mapping[str, Any]) -> int:
    return _integer(settings, "holding_bars", 3, 1, 1000)


def _event_index(dataset: MarketDataset, event: EventRecord) -> tuple[int, int]:
    if asset_identity(dataset.market, dataset.symbol) != asset_identity(event.market, event.symbol):
        raise ValueError("事件标的与所选行情不一致。")
    closes = bar_close_times(dataset)
    available = _instant(event.available_time or event.event_time, event.market, date_end=True)
    execution_index = bisect.bisect_left(closes, available)
    if execution_index >= len(closes) - 1:
        raise ValueError("所选时间段没有事件发生后可供下一根开盘成交的行情，请延长结束日期。")
    # Quant inputs must stop before the actual announcement, not at the later
    # daily close to which the event's execution is aligned.
    history_index = bisect.bisect_right(closes, available) - 1
    return execution_index, history_index


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _return_direction(value: float | None) -> str | None:
    if value is None:
        return None
    return "up" if value > 0 else "down" if value < 0 else "neutral"


def _event_case_target(case: Mapping[str, Any], event: EventRecord) -> dict[str, Any]:
    target = dict(case.get("target") or {})
    if target.get("kind") == "event" and (target.get("key") or target.get("id")):
        target.setdefault("key", target.get("id"))
        return target
    key = _event_key(event.to_dict())
    return {
        "id": key,
        "key": key,
        "kind": "event",
        "name": event.title or event.event_id,
        "event_id": event.event_id,
        "market": event.market,
        "symbol": event.symbol,
        "event_time": event.event_time,
        "available_time": event.available_time or event.event_time,
    }


def _case_label(case: Mapping[str, Any], event: EventRecord) -> EventLabel | None:
    """Normalize the catalogue's frozen label without exposing it to a model.

    Event-set catalogue entries may carry either the ordinary ``EventLabel``
    object, its raw JSON row, or the older ``event_truth.horizons`` summary.
    Supporting all three keeps saved single-event Arena jobs readable while the
    event-set path reuses the normal backtest's frozen label rows.
    """
    value = case.get("label") or case.get("event_label")
    if isinstance(value, EventLabel):
        return value
    raw: dict[str, Any] = dict(value) if isinstance(value, Mapping) else {}
    truth = case.get("event_truth") or case.get("truth")
    if isinstance(truth, EventLabel):
        return truth
    if isinstance(truth, Mapping):
        for key, item in truth.items():
            if key.startswith(("label_", "car_", "ret_", "bm_ret_")) and key not in raw:
                raw[key] = item
        horizons = truth.get("horizons")
        if isinstance(horizons, Mapping):
            for horizon, item in horizons.items():
                name = str(horizon).lower()
                if name not in ALL_HORIZONS or not isinstance(item, Mapping):
                    continue
                raw.setdefault(f"label_{name}", item.get("direction") or item.get("label"))
                for field, aliases in {
                    f"car_{name}": ("car", "oracle_car", "excess_return", "oracle_return"),
                    f"ret_{name}": ("asset_return", "actual_return", "ret", "market_return"),
                    f"bm_ret_{name}": ("benchmark_return", "bm_return"),
                }.items():
                    if field in raw:
                        continue
                    for alias in aliases:
                        if alias in item:
                            raw[field] = item.get(alias)
                            break
    raw.setdefault("event_id", event.event_id)
    label = EventLabel.from_dict(raw)
    if not any(
        str(getattr(label, f"label_{horizon}", "") or "")
        or _finite(getattr(label, f"car_{horizon}", None)) is not None
        for horizon in ALL_HORIZONS
    ):
        return None
    return label


def normalize_event_cases(resolved: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return one immutable execution packet per selected event.

    The ordinary single-event shape remains supported.  Event sets are never
    flattened into unrelated asset observations: each event retains its own
    frozen facts, label and matching market snapshot.
    """
    raw_cases = resolved.get("event_cases")
    if isinstance(raw_cases, Sequence) and not isinstance(raw_cases, (str, bytes)):
        candidates = list(raw_cases)
    elif isinstance(resolved.get("event"), EventRecord):
        candidates = [{
            "event": resolved["event"],
            "dataset": resolved.get("dataset"),
            "history_dataset": resolved.get("history_dataset") or resolved.get("dataset"),
            "event_truth": resolved.get("event_truth"),
            "label": resolved.get("label"),
            "target": resolved.get("target"),
        }]
    else:
        candidates = []
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(candidates, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"事件集第 {index} 条执行输入格式无效。")
        event_value = raw.get("event")
        event = event_value if isinstance(event_value, EventRecord) else (
            EventRecord.from_dict(dict(event_value)) if isinstance(event_value, Mapping) else None
        )
        dataset = raw.get("dataset") or raw.get("market_dataset")
        history = raw.get("history_dataset") or raw.get("history") or dataset
        if event is None or not event.event_id:
            raise ValueError(f"事件集第 {index} 条缺少有效事件事实。")
        if event.event_id in seen:
            raise ValueError(f"事件集包含重复 event_id={event.event_id}。")
        if dataset is not None and not isinstance(dataset, MarketDataset):
            raise ValueError(f"事件 {event.event_id} 的行情快照格式无效。")
        if history is not None and not isinstance(history, MarketDataset):
            raise ValueError(f"事件 {event.event_id} 的历史行情快照格式无效。")
        seen.add(event.event_id)
        case = dict(raw)
        case.update(event=event, dataset=dataset, history_dataset=history)
        case["target"] = _event_case_target(case, event)
        case["label"] = _case_label(case, event)
        cases.append(case)
    return cases


def _model_kind(model: Mapping[str, Any]) -> str:
    return str(model.get("kind") or model.get("model_kind") or "")


def _remote_quant(spec: Mapping[str, Any]) -> bool:
    return spec.get("kind") == "external_http" or (
        spec.get("kind") == "return_forecast" and spec.get("source") == "external_http"
    )


def _points(dataset: MarketDataset, settings: Mapping[str, Any], *, remote: bool) -> list[int]:
    warmup = _integer(settings, "warmup_bars", 20, 2, 10000)
    first = max(warmup - 1, int(settings.get("_comparison_first_index", 0)))
    points = list(range(first, len(dataset.bars) - 1, _horizon(settings)))
    if not points:
        raise ValueError(f"所选区间不足以提供 {warmup} 根历史 K 线和下一根成交行情，请扩大时间段。")
    if remote and len(points) > MAX_MODEL_PREDICTIONS:
        raise ValueError(
            f"该区间需要每个模型生成 {len(points)} 次预测，超过单次 Arena 的 {MAX_MODEL_PREDICTIONS} 次上限。"
            "请缩短时间段或增大预测周期；系统不会静默跳过日期。"
        )
    return points


def _execution_input(model: dict, dataset: MarketDataset, settings: dict) -> tuple[MarketDataset, dict]:
    """Optional earlier history informs models but never enters result curves."""
    history = settings.get("history_dataset")
    prepared = {key: value for key, value in settings.items() if key != "history_dataset"}
    if history is None or _model_kind(model) == "imported_predictions":
        return dataset, prepared
    if not isinstance(history, MarketDataset):
        raise ValueError("模型热身行情必须是已冻结的行情数据。")
    if asset_identity(history.market, history.symbol) != asset_identity(dataset.market, dataset.symbol) or history.frequency != dataset.frequency:
        raise ValueError("热身行情与比较行情的标的或周期不一致。")
    closes, selected_closes = bar_close_times(history), bar_close_times(dataset)
    first, last = bisect.bisect_left(closes, selected_closes[0]), bisect.bisect_right(closes, selected_closes[-1])
    segment = history.bars[first:last]
    if len(segment) != len(dataset.bars) or any(
        any(getattr(a, key) != getattr(b, key) for key in ("timestamp", "open", "high", "low", "close", "volume"))
        for a, b in zip(segment, dataset.bars)
    ):
        raise ValueError("热身行情与选中区间的 K 线不一致，请重新加载行情。")
    prepared["_comparison_first_index"] = first
    return _safe_dataset(history, through=last - 1), prepared


def estimate_model_work(model: dict, dataset: MarketDataset, event: EventRecord | None, settings: dict) -> int:
    """Validate contracts and count tasks before any selected model is called."""
    dataset, settings = _execution_input(model, dataset, settings)
    kind, horizon = _model_kind(model), _horizon(settings)
    if kind not in {"pronoia", "external_model", "quant", "external_service", "imported_predictions"}:
        raise ValueError("该模型类型尚不能执行 Arena 预测。")
    bar_close_times(dataset)
    spec = dict(model.get("strategy_spec") or {})
    event_service = kind == "external_service" and spec.get("type") == "event"
    if kind in {"pronoia", "external_model"} or event_service:
        if dataset.frequency.lower() not in _DAILY:
            raise ValueError("Pronoia、独立大模型和事件预测服务当前按交易日预测，请为混合对比选择日 K 行情。")
        normalize_evaluation_horizon(f"t{horizon}")
        if kind in {"pronoia", "external_model"}:
            profile = model.get("profile_snapshot") or {}
            if not profile.get("base_url") or not profile.get("model_id"):
                raise ValueError("该模型缺少保存的 API 地址或模型 ID，请先完善模型连接。")
    if event is not None:
        index, history = _event_index(dataset, event)
        if index < int(settings.get("_comparison_first_index", 0)):
            raise ValueError("事件发生时间不在所选比较区间内，请提前开始日期。")
        if kind == "quant" or (kind == "external_service" and not event_service):
            if history < 1:
                raise ValueError("事件公布前不足两根已收盘 K 线，量化模型无法生成当时可得的预测，请提前开始日期。")
            _preflight_quant(model, dataset, event, settings)
        if kind == "imported_predictions":
            _load_imported_model(model, dataset, event, settings)
        return 1
    if kind == "imported_predictions":
        _load_imported_model(model, dataset, event, settings)
        return 1
    if kind in {"pronoia", "external_model"} or event_service or _remote_quant(spec):
        return len(_points(dataset, settings, remote=True))
    _preflight_quant(model, dataset, event, settings)
    return max(1, len(dataset.bars) - 1 - int(settings.get("_comparison_first_index", 0)))


def _safe_dataset(dataset: MarketDataset, *, through: int | None = None) -> MarketDataset:
    """Remove arbitrary CSV fields, metadata and full-range source references."""
    bars = dataset.bars if through is None else dataset.bars[:through + 1]
    return MarketDataset(
        name=f"{dataset.symbol} 历史行情", market=dataset.market, symbol=dataset.symbol,
        frequency=dataset.frequency,
        bars=tuple(Bar(**{key: getattr(bar, key) for key in ("timestamp", "open", "high", "low", "close", "volume")}) for bar in bars),
        source_type="arena_asof",
    )


def _market_observation(dataset: MarketDataset, index: int, horizon: int, lookback: int) -> EventRecord:
    closes = bar_close_times(dataset)
    as_of = closes[index].isoformat()
    history = dataset.bars[max(0, index + 1 - lookback):index + 1]
    packet = {
        "input_type": "historical_market_observation", "as_of": as_of,
        "symbol": dataset.symbol, "frequency": dataset.frequency, "horizon_bars": horizon,
        "bars": [{key: getattr(bar, key) for key in ("timestamp", "open", "high", "low", "close", "volume")} for bar in history],
    }
    text = (
        "这是截止所列收盘时点的行情观察，不是新闻事件。仅依据下面已收盘的 OHLCV 进行预测，"
        "不要虚构公告、财报或其他外部事实。预测从最后一根 K 线收盘价开始，"
        f"未来 {horizon} 个交易日的标的自身收益率 expected_return_pct（2 代表 +2%）。"
        "无法给出数值时请返回 null，不能从方向或置信度换算数值。\n"
        + json.dumps(packet, ensure_ascii=False, allow_nan=False)
    )
    return EventRecord(
        event_id=f"market_{index}", market=dataset.market, symbol=dataset.symbol,
        event_time=as_of, available_time=as_of, occurred_at=as_of,
        event_type_l2="历史行情观察", title=f"{dataset.symbol} 收盘行情观察 · {dataset.bars[index].timestamp}",
        event_text=text, source_url="arena:historical-market-observation",
    )


async def _cancellable(awaitable, cancel: threading.Event):
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            _check(cancel)
            await asyncio.wait({task}, timeout=0.2)
        _check(cancel)
        return task.result()
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


def _prediction_observation(prediction: TeamPrediction, index: int, horizon: int) -> dict | None:
    if prediction.abstain or prediction.pred_direction not in {"up", "down", "neutral"}:
        return None
    if prediction.horizon not in {None, "", f"t{horizon}"}:
        return None
    if prediction.expected_return_pct is not None and prediction.expected_return_pct < -100:
        return None
    return {
        "bar_index": index, "direction": prediction.pred_direction,
        "expected_return_pct": prediction.expected_return_pct,
        "horizon_bars": horizon, "direction_basis": "market_excess",
        "source": {"kind": "arena_prediction", "event_id": prediction.event_id},
    }


def _quant_spec(model: dict, horizon: int) -> dict:
    spec = copy.deepcopy(model.get("strategy_spec") or {})
    if spec.get("kind") == "return_forecast":
        if spec.get("source") != "signal_file":
            spec.setdefault("parameters", {})["horizon_bars"] = horizon
    return spec


def _validate_imported_quant(model: dict, spec: dict, dataset: MarketDataset) -> None:
    if spec.get("kind") != "signal_file" and spec.get("source") != "signal_file":
        return
    source = db.get_bt_run(str(model.get("source_run_id") or ""))
    if not source:
        raise ValueError("导入信号缺少原始标的记录，不能确认是否适用于本次标的。")
    source_market = get_run_market(source)
    if asset_identity(source_market.market, source_market.symbol) != asset_identity(dataset.market, dataset.symbol):
        raise ValueError("导入信号属于其他标的，不能将同一天的信号套用到本次标的。")
    if source_market.frequency != dataset.frequency:
        raise ValueError("导入信号的原始 K 线周期与本次行情不同，不能直接换算。")


def _load_imported_model(model: dict, dataset: MarketDataset, event: EventRecord | None, settings: dict) -> dict:
    target = dict(settings.get("target") or {})
    if event is not None and target.get("kind") != "event":
        raise ValueError("导入事件预测缺少明确的事件标识，请重新选择目标。")
    if event is None:
        market, symbol = asset_identity(dataset.market, dataset.symbol)
        target = {"kind": "asset", "key": f"asset:{market}:{symbol}", "market": market, "symbol": symbol}
    # The registered model may have been evaluated on several datasets. Only
    # its frozen, declared lineage is eligible; never search unrelated Runs.
    lineage = model.get("source_run_ids")
    source_ids = list(dict.fromkeys(str(value).strip() for value in lineage if isinstance(value, str) and value.strip())) if isinstance(lineage, list) else []
    if not source_ids:
        source_ids = [str(model.get("source_run_id") or "").strip()]
    runs = [run for source_id in source_ids if source_id and (run := db.get_bt_run(source_id))]
    completed = [run for run in runs if run.get("status") == "done"]
    if not completed:
        raise ValueError("导入预测对应的已完成记录不存在。")
    private = [run for run in completed if str(run.get("visibility") or "private") == "private"]
    if not private:
        raise ValueError("该记录仅公开安全摘要，不能读取逐条预测进行新的 Arena 对比。")
    private.sort(key=lambda run: (str(run.get("updated_at") or run.get("created_at") or ""),
                                  str(run.get("created_at") or "")), reverse=True)
    failures: list[ValueError] = []
    for run in private:
        try:
            loaded = load_contestant(run, target, dataset)
        except ValueError as exc:
            # This source may concern another event, asset or date window. The
            # adapter verifies those facts and actual saved predictions before
            # any source can be selected. Results from sources are never merged.
            failures.append(exc)
            continue
        return {**loaded, "source_run_id": str(run["id"])}
    if len(private) == 1 and failures:
        raise failures[0]
    raise ValueError("该导入模型的已保存来源在所选事件、标的与时间段内没有可执行预测，请选择已有预测覆盖的目标和日期。")


def _preflight_quant(model: dict, dataset: MarketDataset, event: EventRecord | None, settings: dict) -> None:
    """Detect local warmup/file incompatibility before another model spends API calls."""
    spec = _quant_spec(model, _horizon(settings))
    _validate_imported_quant(model, spec, dataset)
    strategy = strategy_from_spec(spec)
    if _remote_quant(spec):
        return
    history = _event_index(dataset, event)[1] if event else None
    inputs = _safe_dataset(dataset, through=history)
    signals = strategy.generate(inputs)
    indices = {bar.timestamp: i for i, bar in enumerate(inputs.bars)}
    valid = [signal for signal in signals if signal.timestamp in indices and _valid_signal(signal, spec)]
    if event is not None:
        eligible = [signal for signal in valid if signal.expected_return_pct is None or
                    indices[signal.timestamp] + int(signal.horizon_bars) > history]
    else:
        first = int(settings.get("_comparison_first_index", 0))
        eligible = [signal for signal in valid if indices[signal.timestamp] < len(inputs.bars) - 1 and
                    (indices[signal.timestamp] >= first or signal.expected_return_pct is None)]
    if not eligible:
        raise ValueError("该量化模型在目标时点历史不足或没有有效预测，请提前开始日期、补充历史行情或选择其他模型。")


def _valid_signal(signal: Signal, spec: Mapping[str, Any]) -> bool:
    metadata = signal.metadata
    if metadata.get("forecast_status") in {"warmup", "abstained"}:
        return False
    if spec.get("kind") == "return_forecast":
        return signal.expected_return_pct is not None
    if spec.get("kind") == "ma_cross" and metadata.get("short_ma") is None:
        return False
    if spec.get("kind") == "momentum" and metadata.get("momentum") is None:
        return False
    return True


def _signal_observation(signal: Signal, index: int, *, numeric_anchor_valid: bool = True) -> dict:
    number = signal.expected_return_pct
    weight = number if number is not None else signal.target_weight
    return {
        "bar_index": index, "direction": "up" if weight > 0 else "down" if weight < 0 else "neutral",
        "expected_return_pct": number,
        "horizon_bars": signal.horizon_bars if numeric_anchor_valid else None,
        "direction_basis": "asset_return" if number is not None else "position",
        "source": {"kind": "arena_quant_prediction", "timestamp": signal.timestamp},
    }


def _generate_current(strategy, spec: dict, history: MarketDataset) -> list[Signal]:
    """The forecast HTTP adapter is called once for the current decision only."""
    if spec.get("kind") != "return_forecast" or spec.get("source") != "external_http":
        return list(strategy.generate(history))
    bar = history.bars[-1]
    payload = {
        "schema_version": FORECAST_CONTRACT, "as_of": bar.timestamp,
        "symbol": history.symbol, "market": history.market, "frequency": history.frequency,
        "horizon_bars": strategy.horizon_bars, "return_unit": "percent",
        "target": "asset_close_to_close_return", "point_in_time_enforced": True,
        "bars": [item.to_dict() for item in history.bars[-strategy.lookback:]],
        "context": {"timestamp": bar.timestamp, "series": {}}, "parameters": strategy.parameters,
    }
    row = strategy._post(payload)
    if not isinstance(row, dict) or "expected_return_pct" not in row or row.get("horizon_bars") != strategy.horizon_bars:
        raise ValueError("外部收益预测必须返回 expected_return_pct 与本次相同的 horizon_bars。")
    if row.get("timestamp") not in (None, bar.timestamp):
        raise ValueError("外部收益预测时间必须等于本次历史收盘时点。")
    value = row["expected_return_pct"]
    return [Signal(bar.timestamp, 0.0, expected_return_pct=value,
                   horizon_bars=strategy.horizon_bars if value is not None else None,
                   metadata={"forecast_status": "provided" if value is not None else "abstained"})]


async def _evaluate_quant(model: dict, dataset: MarketDataset, event: EventRecord | None,
                          settings: dict, cancel: threading.Event, progress,
                          audit: list[dict]) -> tuple[list[dict], list[str]]:
    spec = _quant_spec(model, _horizon(settings))
    strategy = strategy_from_spec(spec)
    warnings: list[str] = []
    if spec.get("kind") == "signal_file" or spec.get("source") == "signal_file":
        _validate_imported_quant(model, spec, dataset)
        warnings.append("导入信号仅按原始时间和标的使用；文件生成过程的历史信息边界无法由 Arena 验证。")

    async def generate(through: int | None):
        _check(cancel)
        history = _safe_dataset(dataset, through=through)
        return await _cancellable(asyncio.to_thread(_generate_current, strategy, spec, history), cancel)

    if event is not None:
        index, history = _event_index(dataset, event)
        signals = await generate(history)
        by_time = {bar.timestamp: i for i, bar in enumerate(dataset.bars[:history + 1])}
        eligible = [signal for signal in signals if signal.timestamp in by_time and _valid_signal(signal, spec)]
        if not eligible:
            raise ValueError("该量化模型在事件公布前历史不足或未提供有效预测，请提前开始日期。")
        signal = max(eligible, key=lambda item: by_time[item.timestamp])
        signal_index = by_time[signal.timestamp]
        if signal.expected_return_pct is not None and signal_index + int(signal.horizon_bars) <= history:
            raise ValueError("该量化模型在事件发生时只有已到期预测，没有仍有效的判断。")
        anchor_valid = signal_index == index
        if signal.expected_return_pct is not None and not anchor_valid:
            warnings.append("量化预测依据事件公布前收盘价产生，数值起点与事件评测起点不同；保留交易方向，不计入共同收益率误差。")
        progress(1, 1)
        return [_signal_observation(signal, index, numeric_anchor_valid=anchor_valid)], warnings

    if _remote_quant(spec):
        points = _points(dataset, settings, remote=True)
        output = []
        # Only the last close's response is used. Earlier rows from this remote
        # call are never retroactively accepted as historical predictions.
        for count, index in enumerate(points, 1):
            try:
                signals = await generate(index)
            except BacktestCancelled:
                raise
            except Exception:
                _check(cancel)
                warnings.append("部分时点外部量化服务请求失败，已保留成功预测，失败时点不补零。")
                audit.append({"bar_index": index, "timestamp": dataset.bars[index].timestamp,
                              "abstain": True, "error": "外部量化请求失败或返回无效。"})
                progress(count, len(points))
                continue
            latest = [signal for signal in signals if signal.timestamp == dataset.bars[index].timestamp and _valid_signal(signal, spec)]
            if len(latest) > 1:
                raise ValueError("外部量化服务返回了重复的当前时点预测。")
            if latest:
                output.append(_signal_observation(latest[0], index))
                audit.append({**latest[0].to_dict(), "bar_index": index, "abstain": False})
            else:
                warnings.append("部分时点外部量化服务未返回当前时点预测，已保留为缺失。")
                audit.append({"bar_index": index, "timestamp": dataset.bars[index].timestamp,
                              "abstain": True, "error": "未返回当前时点预测。"})
            progress(count, len(points))
        return output, warnings

    signals = await generate(None)
    indices = {bar.timestamp: index for index, bar in enumerate(dataset.bars)}
    by_index: dict[int, Signal] = {}
    for signal in signals:
        if signal.timestamp not in indices or not _valid_signal(signal, spec):
            continue
        index = indices[signal.timestamp]
        if index in by_index:
            raise ValueError("量化模型在同一时点返回了重复预测。")
        by_index[index] = signal
    output = []
    latest = None
    first = int(settings.get("_comparison_first_index", 0))
    earlier = [index for index in by_index if index < first]
    latest = by_index[max(earlier)] if earlier else None
    for index in range(first, len(dataset.bars) - 1):
        _check(cancel)
        if index in by_index:
            latest = by_index[index]
            output.append(_signal_observation(latest, index))
        elif latest is not None and latest.expected_return_pct is None:
            # Position strategies maintain their declared weight until a change.
            # Numerical forecasts, in contrast, are never extended or re-dated.
            output.append(_signal_observation(latest, index, numeric_anchor_valid=False))
        progress(index + 1 - first, len(dataset.bars) - 1 - first)
    return output, warnings


async def evaluate_model(model: dict, dataset: MarketDataset, event: EventRecord | None,
                         settings: dict, cancel: threading.Event,
                         progress: Callable[[int, int], None] | None = None) -> dict:
    """Run a frozen contestant using only the selected target's historical input."""
    _check(cancel)
    total = estimate_model_work(model, dataset, event, settings)
    dataset, settings = _execution_input(model, dataset, settings)
    callback = progress or (lambda done, count: None)
    callback(0, total)
    kind, horizon = _model_kind(model), _horizon(settings)
    result = {"run_id": str(model["id"]), "name": str(model.get("name") or model["id"]),
              "model_kind": kind, "observations": [], "warnings": [], "predictions": []}
    if kind == "imported_predictions":
        loaded = await _cancellable(asyncio.to_thread(_load_imported_model, model, dataset, event, settings), cancel)
        result.update(observations=loaded["observations"], warnings=loaded["warnings"], source_run_id=loaded["source_run_id"])
        callback(1, 1)
    elif kind == "quant" or (kind == "external_service" and (model.get("strategy_spec") or {}).get("type") != "event"):
        observations, warnings = await _evaluate_quant(model, dataset, event, settings, cancel, callback, result["predictions"])
        result.update(observations=observations, warnings=warnings)
    else:
        points = [_event_index(dataset, event)[0]] if event else _points(dataset, settings, remote=True)
        lookback = _integer(settings, "warmup_bars", 20, 2, 10000)
        events = ([replace(event, analysis_direction=None, analysis_confidence=None, analysis_rationale=None,
                           analysis_horizon=None, analysis_expected_return_pct=None, direction_prior=None)]
                  if event else [_market_observation(dataset, point, horizon, lookback) for point in points])
        work_id = f"arena_{uuid.uuid4().hex}"
        profile = dict(model.get("profile_snapshot") or {})
        for count, (point, observation) in enumerate(zip(points, events), 1):
            _check(cancel)
            try:
                if kind == "pronoia":
                    trajectory = Path(config.DATA_DIR) / "arena_workspace" / "trajectories" / work_id
                    trajectory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with model_profile_context(profile):
                        predictions = await _cancellable(run_team_full_trajectory(
                            [observation], run_id=work_id, model_version=str(profile["model_id"]),
                            concurrency=1, trajectory_ckpt_dir=trajectory, target_horizon=f"t{horizon}",
                            system_prompt_variant="v0", cancel_check=cancel.is_set,
                        ), cancel)
                elif kind == "external_model":
                    predictions = await _cancellable(run_raw_model(
                        [observation], run_id=work_id, profile=profile, target_horizon=f"t{horizon}",
                        concurrency=1, before_request=lambda: _check(cancel),
                    ), cancel)
                else:
                    predictions = await _cancellable(asyncio.to_thread(
                        run_external_event_strategy, [observation], run_id=work_id,
                        spec=copy.deepcopy(model.get("strategy_spec") or {}),
                        model_version=str(model.get("version") or "arena-external"),
                        target_horizon=f"t{horizon}", before_request=lambda: _check(cancel),
                    ), cancel)
                if len(predictions) != 1 or predictions[0].event_id != observation.event_id:
                    raise ValueError("模型返回的预测数量或事件标识与本次请求不一致。")
            except BacktestCancelled:
                raise
            except Exception as exc:
                _check(cancel)
                result["predictions"].append({"event_id": observation.event_id,
                    "bar_index": point, "abstain": True, "error_type": type(exc).__name__,
                    "error": "本次模型请求失败或返回格式无效，未生成预测。"})
                result["warnings"].append("部分请求失败，已保留成功预测；失败时点不补为中性或零分。")
                callback(count, total)
                continue
            prediction = predictions[0]
            normalized = _prediction_observation(prediction, point, horizon)
            audit = {**prediction.to_dict(), "bar_index": point}
            if normalized is None:
                audit.update(abstain=True, error="预测未通过方向、周期或收益率取值校验。")
            result["predictions"].append(audit)
            if normalized:
                result["observations"].append(normalized)
            else:
                result["warnings"].append("部分请求未返回可验证的预测，已保留为缺失，不补为中性或零分。")
            callback(count, total)
    _check(cancel)
    first = int(settings.get("_comparison_first_index", 0))
    result["observations"] = [
        {**item, "bar_index": item["bar_index"] - first}
        for item in result["observations"] if item["bar_index"] >= first
    ]
    result["predictions"] = [{**item, "bar_index": item["bar_index"] - first}
                             if "bar_index" in item else item for item in result["predictions"]]
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    result["prediction_count"] = len(result["predictions"]) or len(result["observations"])
    result["failed_prediction_count"] = sum(bool(item.get("abstain")) for item in result["predictions"])
    if not any(item["bar_index"] < len(dataset.bars) - first - 1 for item in result["observations"]):
        raise ValueError("该模型没有生成可用于比较的有效预测，请检查 API 返回或增加历史行情。")
    return result


def _validate_event_contestant(model: Mapping[str, Any], horizon: int) -> None:
    kind = _model_kind(model)
    spec = model.get("strategy_spec") or {}
    is_event_service = kind == "external_service" and spec.get("type") == "event"
    if kind not in {"pronoia", "external_model", "imported_predictions"} and not is_event_service:
        raise ValueError("Event Arena 只能选择事件模型，不能混入量化策略。")
    normalize_evaluation_horizon(f"t{horizon}")
    if kind in {"pronoia", "external_model"}:
        profile = model.get("profile_snapshot") or {}
        if not profile.get("base_url") or not profile.get("model_id"):
            raise ValueError("该事件模型缺少保存的 API 地址或模型 ID，请先完善模型连接。")


def _load_imported_event_prediction(model: Mapping[str, Any], event: EventRecord,
                                    target: Mapping[str, Any]) -> tuple[TeamPrediction, str]:
    lineage = model.get("source_run_ids")
    source_ids = list(dict.fromkeys(
        str(value).strip() for value in lineage
        if isinstance(value, str) and value.strip()
    )) if isinstance(lineage, list) else []
    if not source_ids:
        source_ids = [str(model.get("source_run_id") or "").strip()]
    runs = [run for source_id in source_ids if source_id and (run := db.get_bt_run(source_id))]
    private = [
        run for run in runs
        if run.get("status") == "done" and str(run.get("visibility") or "private") == "private"
    ]
    private.sort(key=lambda run: (
        str(run.get("updated_at") or run.get("created_at") or ""),
        str(run.get("created_at") or ""),
    ), reverse=True)
    if not private:
        raise ValueError("导入事件预测对应的完整私有结果不存在。")
    key = str(target.get("key") or target.get("id") or "")
    for run in private:
        try:
            frozen_event = get_event_record(run, key)
            if frozen_event.event_id != event.event_id:
                continue
            predictions = load_predictions(str(run.get("out_path") or ""))
        except (OSError, ValueError, UnicodeError):
            continue
        matches = [prediction for prediction in predictions if prediction.event_id == event.event_id]
        if len(matches) == 1:
            return matches[0], str(run["id"])
        if len(matches) > 1:
            raise ValueError("导入事件预测包含重复 event_id。")
    raise ValueError("导入模型的冻结结果不包含该事件预测。")


async def _evaluate_event_record(model: dict, event: EventRecord, settings: dict,
                                 cancel: threading.Event) -> dict:
    """Predict one frozen event without requiring or fabricating OHLC bars."""
    _check(cancel)
    horizon = _horizon(settings)
    _validate_event_contestant(model, horizon)
    kind = _model_kind(model)
    observation = replace(
        event,
        analysis_direction=None,
        analysis_confidence=None,
        analysis_rationale=None,
        analysis_horizon=None,
        analysis_expected_return_pct=None,
        direction_prior=None,
    )
    result = {
        "run_id": str(model["id"]),
        "name": str(model.get("name") or model["id"]),
        "model_kind": kind,
        "observations": [],
        "warnings": [],
        "predictions": [],
    }
    try:
        source_run_id = None
        if kind == "imported_predictions":
            prediction, source_run_id = await _cancellable(asyncio.to_thread(
                _load_imported_event_prediction, model, observation,
                dict(settings.get("target") or {}),
            ), cancel)
        else:
            work_id = f"arena_{uuid.uuid4().hex}"
            profile = dict(model.get("profile_snapshot") or {})
            if kind == "pronoia":
                trajectory = Path(config.DATA_DIR) / "arena_workspace" / "trajectories" / work_id
                trajectory.mkdir(parents=True, exist_ok=True, mode=0o700)
                with model_profile_context(profile):
                    predictions = await _cancellable(run_team_full_trajectory(
                        [observation], run_id=work_id, model_version=str(profile["model_id"]),
                        concurrency=1, trajectory_ckpt_dir=trajectory,
                        target_horizon=f"t{horizon}", system_prompt_variant="v0",
                        cancel_check=cancel.is_set,
                    ), cancel)
            elif kind == "external_model":
                predictions = await _cancellable(run_raw_model(
                    [observation], run_id=work_id, profile=profile,
                    target_horizon=f"t{horizon}", concurrency=1,
                    before_request=lambda: _check(cancel),
                ), cancel)
            else:
                predictions = await _cancellable(asyncio.to_thread(
                    run_external_event_strategy, [observation], run_id=work_id,
                    spec=copy.deepcopy(model.get("strategy_spec") or {}),
                    model_version=str(model.get("version") or "arena-external"),
                    target_horizon=f"t{horizon}", before_request=lambda: _check(cancel),
                ), cancel)
            if len(predictions) != 1 or predictions[0].event_id != observation.event_id:
                raise ValueError("模型返回的预测数量或事件标识与本次请求不一致。")
            prediction = predictions[0]
        normalized = _prediction_observation(prediction, 0, horizon)
        audit = {**prediction.to_dict(), "bar_index": 0}
        if normalized is None:
            audit.update(abstain=True, error="预测未通过方向、周期或收益率取值校验。")
        result["predictions"].append(audit)
        if normalized:
            result["observations"].append(normalized)
        else:
            result["warnings"].append("该事件未返回可验证预测，保留为缺失，不补为中性或零分。")
        if source_run_id:
            result["source_run_id"] = source_run_id
    except BacktestCancelled:
        raise
    except Exception as exc:
        _check(cancel)
        result["predictions"].append({
            "event_id": observation.event_id,
            "bar_index": 0,
            "abstain": True,
            "error_type": type(exc).__name__,
            "error": "本次事件模型请求失败或返回格式无效，未生成预测。",
        })
        result["warnings"].append("该事件请求失败，已保留为缺失，不补为中性或零分。")
    result["prediction_count"] = len(result["predictions"])
    result["failed_prediction_count"] = sum(bool(item.get("abstain")) for item in result["predictions"])
    return result


def estimate_event_cases_work(model: dict, cases: Sequence[Mapping[str, Any]], settings: dict) -> int:
    """Preflight every frozen event before any contestant incurs model usage."""
    if not cases:
        raise ValueError("事件集在所选时间区间内没有可评测事件。")
    if _model_kind(model) != "imported_predictions" and len(cases) > MAX_MODEL_PREDICTIONS:
        raise ValueError(
            f"事件集包含 {len(cases)} 个事件，超过单个模型每场 Arena 的 {MAX_MODEL_PREDICTIONS} 次预测上限。"
            "请缩短事件时间区间。"
        )
    horizon = _horizon(settings)
    horizon_name = normalize_evaluation_horizon(f"t{horizon}")
    _validate_event_contestant(model, horizon)
    for case in cases:
        event = case.get("event")
        if not isinstance(event, EventRecord):
            raise ValueError("事件集包含未冻结的事件事实。")
        label = case.get("label")
        direction = str(getattr(label, f"label_{horizon_name}", "") or "") if isinstance(label, EventLabel) else ""
        actual = _finite(getattr(label, f"car_{horizon_name}", None)) if isinstance(label, EventLabel) else None
        if direction not in {"up", "down", "neutral"} or actual is None:
            raise ValueError(
                f"事件 {event.event_id} 缺少共同 {horizon_name.upper()} 标签或 Oracle CAR；"
                "请改用该事件集已冻结的可用预测窗口。"
            )
        if _model_kind(model) == "imported_predictions":
            _load_imported_event_prediction(model, event, _event_case_target(case, event))
    return len(cases)


async def evaluate_event_cases_model(
    model: dict,
    cases: Sequence[Mapping[str, Any]],
    settings: dict,
    cancel: threading.Event,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Evaluate one event-lane contestant across a frozen single event or set.

    A failed event remains an explicit missing prediction and does not erase
    the contestant's successful events.  The set itself is kept intact in
    ``event_results`` so result construction can drill down without attempting
    to align unrelated symbols onto one artificial market curve.
    """
    total = estimate_event_cases_work(model, cases, settings)
    callback = progress or (lambda done, count: None)
    callback(0, total)
    completed = 0
    aggregate = {
        "run_id": str(model["id"]),
        "name": str(model.get("name") or model["id"]),
        "model_kind": _model_kind(model),
        "observations": [],
        "warnings": [],
        "predictions": [],
        "event_results": [],
    }
    for case in cases:
        _check(cancel)
        event = case["event"]
        case_settings = {
            **settings,
            "target": _event_case_target(case, event),
        }
        case_total = 1

        def report(done: int, _count: int, *, offset: int = completed) -> None:
            callback(min(total, offset + done), total)

        try:
            output = await _evaluate_event_record(model, event, case_settings, cancel)
            report(1, 1)
        except BacktestCancelled:
            raise
        except Exception as exc:
            _check(cancel)
            aggregate["predictions"].append({
                "event_id": event.event_id,
                "abstain": True,
                "error_type": type(exc).__name__,
                "error": "该事件未生成有效预测。",
            })
            aggregate["event_results"].append({
                "event_id": event.event_id,
                "status": "failed",
                "observations": [],
                "predictions": [],
            })
            aggregate["warnings"].append("部分事件未生成有效预测，已保留为缺失，不补为中性或零分。")
            completed += case_total
            callback(completed, total)
            continue
        observations = [
            {**item, "event_id": event.event_id}
            for item in output.get("observations") or []
        ]
        predictions = [
            {**item, "event_id": str(item.get("event_id") or event.event_id)}
            for item in output.get("predictions") or []
        ]
        aggregate["observations"].extend(observations)
        aggregate["predictions"].extend(predictions)
        aggregate["warnings"].extend(output.get("warnings") or [])
        aggregate["event_results"].append({
            "event_id": event.event_id,
            "status": "partial" if output.get("failed_prediction_count") else "done",
            "observations": observations,
            "predictions": predictions,
            **({"source_run_id": output["source_run_id"]} if output.get("source_run_id") else {}),
        })
        completed += case_total
        callback(completed, total)
    aggregate["warnings"] = list(dict.fromkeys(str(item) for item in aggregate["warnings"]))
    aggregate["prediction_count"] = len(aggregate["predictions"]) or len(aggregate["observations"])
    aggregate["failed_prediction_count"] = sum(
        bool(item.get("abstain")) for item in aggregate["predictions"]
    )
    if not aggregate["observations"]:
        raise ValueError("该模型没有在事件集生成可评测预测，请检查连接、预测格式或事件覆盖。")
    return aggregate


def unified_quant_records(dataset: MarketDataset, contestant: Mapping[str, Any], horizon: int) -> list[dict[str, Any]]:
    """Expose the common result-record contract without changing quant replay."""
    confidence_by_index: dict[int, float | None] = {}
    for item in contestant.get("predictions") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("bar_index"), int):
            continue
        confidence_by_index[int(item["bar_index"])] = _finite(item.get("confidence"))
    records = []
    for item in contestant.get("observations") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("bar_index"), int):
            continue
        index = int(item["bar_index"])
        if not 0 <= index < len(dataset.bars):
            continue
        direction = str(item.get("direction") or "")
        actual = (
            dataset.bars[index + horizon].close / dataset.bars[index].close - 1.0
            if index + horizon < len(dataset.bars) else None
        )
        # Quant Arena deliberately retains the existing long/flat replay.
        directional = actual if actual is not None and direction == "up" else (
            0.0 if actual is not None and direction in {"down", "neutral"} else None
        )
        actual_direction = _return_direction(actual)
        records.append({
            "schema_version": UNIFIED_RECORD_SCHEMA,
            "time": dataset.bars[index].timestamp,
            "timestamp": dataset.bars[index].timestamp,
            "market": dataset.market,
            "symbol": dataset.symbol,
            "event_id": None,
            "label": None,
            "direction": direction,
            "confidence": confidence_by_index.get(index),
            "actual_direction": actual_direction,
            "actual_return": actual,
            "directional_return": directional,
            "net_directional_return": directional,
            "return_unit": "decimal",
            "return_basis": "asset_close_to_close",
            "metrics": {
                "is_correct": direction == actual_direction if actual_direction is not None else None,
                "matured": actual is not None,
                "horizon_bars": horizon,
                "position_rule": "long_flat",
            },
        })
    return records


def _prediction_for_event(contestant: Mapping[str, Any], event_id: str) -> tuple[str | None, float | None, bool]:
    candidates = [
        item for item in contestant.get("predictions") or []
        if isinstance(item, Mapping) and str(item.get("event_id") or "") == event_id
    ]
    valid = [item for item in candidates if not item.get("abstain") and str(item.get("pred_direction") or "") in {"up", "down", "neutral"}]
    if valid:
        item = valid[-1]
        return str(item["pred_direction"]), _finite(item.get("confidence")), False
    observations = [
        item for item in contestant.get("observations") or []
        if isinstance(item, Mapping) and str(item.get("event_id") or "") == event_id
        and str(item.get("direction") or "") in {"up", "down", "neutral"}
    ]
    if observations:
        return str(observations[-1]["direction"]), None, False
    return None, None, bool(candidates)


def _case_returns(case: Mapping[str, Any], horizon: str) -> tuple[float | None, float | None, float | None, str]:
    label = case.get("label")
    if isinstance(label, EventLabel):
        car = _finite(getattr(label, f"car_{horizon}", None))
        asset = _finite(getattr(label, f"ret_{horizon}", None))
        benchmark = _finite(getattr(label, f"bm_ret_{horizon}", None))
        if car is not None:
            return car, asset, benchmark, "oracle_car"
        if asset is not None and benchmark is not None:
            return asset - benchmark, asset, benchmark, "oracle_asset_minus_benchmark"
    event, dataset = case.get("event"), case.get("dataset")
    if isinstance(event, EventRecord) and isinstance(dataset, MarketDataset) and horizon.startswith("t"):
        try:
            bars = int(horizon[1:])
            index, _ = _event_index(dataset, event)
            if index + bars < len(dataset.bars):
                asset = dataset.bars[index + bars].close / dataset.bars[index].close - 1.0
                return asset, asset, None, "asset_close_to_close_fallback"
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            pass
    return None, None, None, "unavailable"


def compare_event_models(cases: Sequence[Mapping[str, Any]], contestants: list[dict], rules: dict) -> dict:
    """Build an event-only Arena using the ordinary Oracle-CAR proxy protocol."""
    if not contestants:
        raise ValueError("请至少选择一个成功返回事件预测的模型。")
    horizon = normalize_evaluation_horizon(f"t{_horizon(rules)}")
    costs = event_proxy_cost_spec(rules)
    initial = costs["initial_value"]
    ordered_ids = chronological_event_ids(case["event"] for case in cases)
    case_by_id = {str(case["event"].event_id): case for case in cases}
    models = []
    for contestant in contestants:
        predictions: list[TeamPrediction] = []
        details: list[dict[str, Any]] = []
        labels: list[EventLabel] = []
        net_value = peak = 1.0
        curve = [{
            "timestamp": None, "event_id": None, "net_value": 1.0,
            "equity": initial, "drawdown": 0.0,
        }]
        active_returns: list[float] = []
        realized_returns: list[float] = []
        for event_id in ordered_ids:
            case = case_by_id[event_id]
            event: EventRecord = case["event"]
            label = case.get("label")
            if isinstance(label, EventLabel):
                labels.append(label)
            direction, confidence, had_failure = _prediction_for_event(contestant, event_id)
            if direction is not None:
                predictions.append(TeamPrediction(
                    event_id=event_id,
                    pred_direction=direction,  # type: ignore[arg-type]
                    run_id=str(contestant.get("run_id") or "arena"),
                    confidence=confidence,
                    horizon=horizon,
                ))
            elif had_failure:
                predictions.append(TeamPrediction(
                    event_id=event_id, pred_direction="neutral",
                    run_id=str(contestant.get("run_id") or "arena"),
                    abstain=True, horizon=horizon,
                ))
            actual, asset_return, benchmark_return, basis = _case_returns(case, horizon)
            label_direction = (
                str(getattr(label, f"label_{horizon}", "") or "")
                if isinstance(label, EventLabel) else ""
            )
            if label_direction not in {"up", "down", "neutral"}:
                label_direction = ""
            if direction == "up" and actual is not None:
                directional = actual
            elif direction == "down" and actual is not None:
                directional = -actual
            elif direction == "neutral":
                directional = 0.0
            else:
                directional = None
            active = direction in {"up", "down"} and directional is not None
            net_return = max(-0.999999, directional - costs["round_trip_cost_rate"]) if active else (
                0.0 if direction == "neutral" else None
            )
            before = net_value
            if net_return is not None:
                net_value *= 1.0 + net_return
                peak = max(peak, net_value)
                curve.append({
                    "timestamp": event.available_time or event.event_time,
                    "event_id": event_id,
                    "net_value": net_value,
                    "equity": initial * net_value,
                    "drawdown": net_value / peak - 1.0,
                })
            if active and net_return is not None:
                active_returns.append(net_return)
            if net_return is not None:
                # Neutral is an explicit flat position, so it contributes a
                # genuine zero to per-event averages and to an all-neutral
                # model's cumulative return. Missing predictions stay absent.
                realized_returns.append(net_return)
            correct = (
                direction == label_direction
                if direction in {"up", "down", "neutral"} and label_direction else None
            )
            details.append({
                "schema_version": UNIFIED_RECORD_SCHEMA,
                "event_id": event_id,
                "title": event.title,
                "time": event.available_time or event.event_time,
                "timestamp": event.available_time or event.event_time,
                "occurred_at": event.occurred_at or event.event_time,
                "market": event.market,
                "symbol": event.symbol,
                "label": label_direction or None,
                "direction": direction,
                "prediction": direction,
                "confidence": confidence,
                "actual_direction": _return_direction(actual),
                "actual_return": actual,
                "asset_return": asset_return,
                "benchmark_return": benchmark_return,
                "directional_return": directional,
                "net_directional_return": net_return,
                "pnl": initial * before * net_return if net_return is not None else None,
                "return_unit": "decimal",
                "return_basis": basis,
                "metrics": {
                    "is_correct": correct,
                    "is_win": net_return > 0 if active and net_return is not None else None,
                    "active_trade": active,
                    "horizon": horizon,
                },
                "drilldown": {
                    "event_id": event_id,
                    "target_id": str((case.get("target") or {}).get("id") or (case.get("target") or {}).get("key") or ""),
                },
            })

        metric_objects = compute_all_metrics(
            predictions=predictions,
            labels=labels,
            primary_oracle_horizon=horizon,  # type: ignore[arg-type]
            enabled_metrics={
                "acc_primary_three_class", "acc_primary_directional_trade",
                "strategy_win_rate", "strategy_total_return", "strategy_max_drawdown",
            },
            execution_spec=rules,
            allowed_event_ids=set(ordered_ids),
            event_order=ordered_ids,
        )
        formal = {key: value.to_dict() for key, value in metric_objects.items()}

        def formal_value(key: str) -> Any:
            metric = metric_objects.get(key)
            return metric.value if metric is not None else None

        total_return = formal_value("strategy_total_return")
        max_drawdown = formal_value("strategy_max_drawdown")
        win_rate = formal_value("strategy_win_rate")
        accuracy = formal_value("acc_primary_three_class")
        if total_return is None and realized_returns:
            total_return = math.prod(1.0 + value for value in realized_returns) - 1.0
        if max_drawdown is None and len(curve) > 1:
            max_drawdown = abs(min(float(point["drawdown"]) for point in curve))
        if win_rate is None and active_returns:
            win_rate = sum(value > 0 for value in active_returns) / len(active_returns)
        average_return = statistics.fmean(realized_returns) if realized_returns else None
        metrics = {
            "accuracy": accuracy,
            "directional_accuracy": formal_value("acc_primary_directional_trade"),
            "win_rate": win_rate,
            "average_return": average_return,
            "average_directional_return": average_return,
            "total_return": total_return,
            "cumulative_return": total_return,
            "max_drawdown": max_drawdown,
            "event_count": len(cases),
            "prediction_count": len([item for item in details if item["direction"] is not None]),
            "active_trade_count": len(active_returns),
            "neutral_count": sum(item["direction"] == "neutral" for item in details),
            "missing_return_count": sum(item["actual_return"] is None for item in details),
            "return_basis": "oracle_car",
            "return_unit": "decimal",
            "annualized_return": None,
            "annualized_volatility": None,
            "sharpe_ratio": None,
            "calmar_ratio": None,
            "annualization_status": "single_event" if len(cases) == 1 else "event_sequence_not_annualized",
            "trade_count": len(active_returns),
            "total_turnover": None,
            "total_cost": costs["round_trip_cost_rate"] * len(active_returns) * initial,
            "excess_total_return": None,
        }
        accuracy_metric = metric_objects.get("acc_primary_three_class")
        accuracy_interval = None
        if accuracy_metric:
            wilson = accuracy_metric.breakdown.get("wilson") or {}
            if _finite(wilson.get("lo_95")) is not None and _finite(wilson.get("hi_95")) is not None:
                accuracy_interval = [float(wilson["lo_95"]), float(wilson["hi_95"])]
        predicted_count = len([item for item in details if item["direction"] is not None])
        models.append({
            "run_id": str(contestant.get("run_id") or ""),
            "name": str(contestant.get("name") or contestant.get("run_id") or "模型"),
            "model_kind": str(contestant.get("model_kind") or "unknown"),
            "metrics": metrics,
            "prediction_quality": {
                "n_direction": int((accuracy_metric.meta if accuracy_metric else {}).get("n") or 0),
                "correct_direction": int((accuracy_metric.meta if accuracy_metric else {}).get("k") or 0),
                "directional_accuracy": accuracy,
                "accuracy_ci95": accuracy_interval,
                "direction_basis": "oracle_label",
                "direction_label": f"Oracle {horizon} 标签",
                "n_numeric": 0,
                "mae_pct": None,
                "rmse_pct": None,
                "bias_pct": None,
                "pending_count": sum(item["actual_return"] is None for item in details),
            },
            "curve": curve,
            "records": details,
            "event_details": details,
            "formal_metrics": formal,
            "forecast": {
                "n": 0,
                "matched_samples": 0,
                "own_n": 0,
                "directional_accuracy": accuracy,
                "directional_n": int((accuracy_metric.meta if accuracy_metric else {}).get("n") or 0),
                "directional_own_n": predicted_count,
                "horizon_bars": int(horizon[1:]),
                "unit": "percentage_points",
                "basis": "oracle_label_and_car",
            },
            "coverage": {
                "signal_count": predicted_count,
                "covered_bars": predicted_count,
                "total_bars": len(cases),
                "predicted_events": predicted_count,
                "total_events": len(cases),
                "ratio": predicted_count / len(cases) if cases else 0.0,
            },
            "warnings": list(dict.fromkeys(str(item) for item in contestant.get("warnings") or [])),
            **({"execution": contestant["execution"]} if contestant.get("execution") else {}),
        })
    event_times = [
        str(case_by_id[event_id]["event"].available_time or case_by_id[event_id]["event"].event_time)
        for event_id in ordered_ids
    ]
    assets = {
        (str(case_by_id[event_id]["event"].market), str(case_by_id[event_id]["event"].symbol))
        for event_id in ordered_ids
    }
    market_name, symbol_name = (next(iter(assets)) if len(assets) == 1 else ("MULTI", "EVENT_SET"))
    return {
        "schema_version": EVENT_RESULT_SCHEMA,
        "record_schema_version": UNIFIED_RECORD_SCHEMA,
        "track": "event",
        "market": {
            "symbol": symbol_name,
            "market": market_name,
            "frequency": "1d",
            "start_at": min(event_times) if event_times else None,
            "end_at": max(event_times) if event_times else None,
            "bar_count": len(cases),
        },
        "rules": {
            **rules,
            "evaluation_horizon": horizon,
            "position_rule": "long_short_flat_event_proxy",
            "return_basis": "prediction_direction_x_oracle_car",
            "round_trip_cost_bps": costs["round_trip_cost_bps"],
        },
        "models": models,
        "benchmark_curve": [],
        "bars": [],
        "events": [{
            "event_id": event_id,
            "title": case_by_id[event_id]["event"].title,
            "time": case_by_id[event_id]["event"].available_time or case_by_id[event_id]["event"].event_time,
            "market": case_by_id[event_id]["event"].market,
            "symbol": case_by_id[event_id]["event"].symbol,
        } for event_id in ordered_ids],
        "notes": [
            "事件赛道沿用普通事件回测口径：标签作为方向真值，实际收益为所选窗口冻结 Oracle CAR。",
            "看涨收益等于实际 CAR，看跌收益为 CAR 的相反数，中性为空仓；活跃事件按一次往返扣除共同手续费与滑点。",
            "事件序列按信息可得时间和 event_id 排序复利；这是等名义事件收益代理，不模拟重叠持仓或订单撮合。",
        ],
    }
