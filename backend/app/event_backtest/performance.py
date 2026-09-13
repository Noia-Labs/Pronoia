"""Auditable chart data for event-decision backtests.

An event model does not currently have a portfolio/fill simulator.  The result
below is therefore intentionally named ``event_proxy``: one equal-notional
round-trip observation per active prediction, using direction × the selected
Oracle CAR and the explicitly configured friction.  It never invents missing
returns or OHLC bars.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from ..return_forecast_statistics import enrich_forecast_summary
from .application import load_events, load_predictions
from .evaluation_protocol import (
    chronological_event_ids,
    event_proxy_cost_spec,
    resolve_run_execution_spec,
    resolve_run_horizon,
    select_events_for_execution_window,
)


def _read_jsonl_objects(path: str | Path, *, kind: str) -> list[dict[str, Any]]:
    source = Path(path)
    rows: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{kind} 第 {line_number} 行不是合法 JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{kind} 第 {line_number} 行必须是 JSON object")
            rows.append(row)
    return rows


def _unique_index(rows: list[dict[str, Any]], *, kind: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        event_id = str(row.get("event_id") or row.get("id") or "")
        if not event_id:
            raise ValueError(f"{kind} 存在缺失 event_id 的记录")
        if event_id in out:
            raise ValueError(f"{kind} 存在重复 event_id={event_id}")
        out[event_id] = row
    return out


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _compound_curve(
    observations: list[dict[str, Any]],
    *,
    return_key: str,
    initial_value: float,
    include_drawdown: bool,
) -> list[dict[str, Any]]:
    curve: list[dict[str, Any]] = [{
        "index": 0,
        "timestamp": None,
        "event_id": None,
        "net_value": 1.0,
        "equity": initial_value,
        "period_return": 0.0,
        **({"drawdown": 0.0} if include_drawdown else {}),
    }]
    net_value = peak = 1.0
    for index, observation in enumerate(observations, start=1):
        period_return = float(observation[return_key])
        net_value *= 1.0 + period_return
        point: dict[str, Any] = {
            "index": index,
            "timestamp": observation.get("event_time"),
            "event_id": observation.get("event_id"),
            "net_value": net_value,
            "equity": initial_value * net_value,
            "period_return": period_return,
        }
        if include_drawdown:
            peak = max(peak, net_value)
            point["drawdown"] = (net_value / peak - 1.0) if peak > 0 else None
        curve.append(point)
    return curve


def _proxy_sharpe(returns: list[float]) -> Optional[float]:
    if len(returns) < 2:
        return None
    deviation = statistics.stdev(returns)
    if deviation <= 0:
        return None
    # Observation-scaled, deliberately not presented as calendar annualized.
    return statistics.mean(returns) / deviation * math.sqrt(len(returns))


def _event_date(event: Any) -> dt.date:
    raw = str(getattr(event, "event_time", "") or "")
    try:
        from .evaluation_protocol import parse_event_datetime

        parsed = parse_event_datetime(
            getattr(event, "available_time", None) or raw,
            market=str(getattr(event, "market", "") or ""),
            field_name="available_time/event_time",
        )
        if parsed is None:
            raise ValueError("missing event timestamp")
        return parsed.date()
    except ValueError as exc:
        raise ValueError(f"事件 {getattr(event, 'event_id', '')} 缺少有效 event_time") from exc


def fetch_event_market_series(
    event: Any,
    *,
    days_before: int = 120,
    days_after: int = 120,
) -> dict[str, Any]:
    """Fetch genuine daily bars/close data for CN/US/HK/FUTURES.

    CN/US first reuse the existing OHLC skill. If that is unavailable, and for
    HK/FUTURES, this function reuses the exact close-price route used by the
    Oracle labeller. A close-only response is marked ``line_only`` and never
    expanded into fictitious candles.
    """
    market = str(getattr(event, "market", "") or "").strip().upper()
    symbol = str(getattr(event, "symbol", "") or "").strip()
    if not symbol:
        raise ValueError("event has no symbol")
    event_date = _event_date(event)
    start = (event_date - dt.timedelta(days=max(1, int(days_before)))).isoformat()
    end = (event_date + dt.timedelta(days=max(1, int(days_after)))).isoformat()
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    if market in {"CN", "US"}:
        try:
            from ..skills.market import get_stock_daily

            result = get_stock_daily(symbol, start_date=start, end_date=end, adjust="qfq")
            artifact = result.get("artifact") if isinstance(result, dict) else None
            payload = artifact.get("payload") if isinstance(artifact, dict) else None
            if isinstance(result, dict) and result.get("ok") and isinstance(payload, dict):
                dates = payload.get("dates")
                ohlc = payload.get("ohlc")
                if (
                    isinstance(dates, list)
                    and isinstance(ohlc, list)
                    and dates
                    and len(dates) == len(ohlc)
                    and all(isinstance(item, (list, tuple)) and len(item) == 4 for item in ohlc)
                ):
                    verified = dict(payload)
                    verified.update({
                        "symbol": payload.get("symbol") or symbol,
                        "market": market,
                        "event_date": event_date.isoformat(),
                        "series_type": "ohlc",
                        "source": payload.get("source") or "skills.market.get_stock_daily",
                        "fetched_at": fetched_at,
                        "adjustment": payload.get("adjustment") or "qfq_requested",
                    })
                    return verified
        except Exception:
            # The verified Oracle route below is the explicit fallback. Its
            # output shape makes the loss of OHLC/volume capability visible.
            pass

    from .labeller import (
        _ak_cn_hist,
        _ak_cn_index_hist,
        _ak_futures_hist,
        _ak_hk_hist,
        _ak_hk_index_hist,
        _ak_us_hist,
        _price_route_for,
        _yf_ticker_for,
    )

    canonical = _yf_ticker_for(symbol, market, getattr(event, "benchmark", None))
    route = _price_route_for(canonical, market)
    fetcher = {
        "us": _ak_us_hist,
        "cn_asset": _ak_cn_hist,
        "cn_index": _ak_cn_index_hist,
        "hk": _ak_hk_hist,
        "hk_index": _ak_hk_index_hist,
        "futures": _ak_futures_hist,
    }[route.provider]
    series = fetcher(route.symbol, start, end, retries=1, sleep_s=0.0)
    if series is None or len(series) == 0:
        raise ValueError(f"真实行情源未返回 {market}/{symbol} 的日线数据")
    points: list[tuple[str, float]] = []
    for date_value, close_value in series.items():
        close = _finite(close_value)
        if close is not None:
            points.append((str(date_value), close))
    points.sort(key=lambda item: item[0])
    if not points:
        raise ValueError(f"真实行情源返回的 {market}/{symbol} close 序列无有限值")
    adjustment = "qfq_requested" if route.provider in {"us", "cn_asset", "hk"} else "provider_default"
    return {
        "symbol": symbol,
        "canonical_symbol": route.canonical,
        "market": market,
        "dates": [item[0] for item in points],
        "closes": [item[1] for item in points],
        "ohlc": [],
        "volumes": [],
        "event_date": event_date.isoformat(),
        "series_type": "line_only",
        "source": f"akshare:{route.provider}",
        "provider_route": route.provider,
        "fetched_at": fetched_at,
        "adjustment": adjustment,
    }


def build_event_proxy_performance(
    run: Mapping[str, Any],
    *,
    include_kline: bool = False,
    kline_limit: int = 6,
    kline_loader: Callable[[Any], dict[str, Any]] = fetch_event_market_series,
) -> dict[str, Any]:
    from .protocol import EVALUATOR_VERSION

    run_id = str(run.get("id") or "")
    events_path = Path(str(run.get("events_path") or ""))
    out_path = Path(str(run.get("out_path") or ""))
    labels_path_raw = run.get("labels_path")
    labels_path = Path(str(labels_path_raw)) if labels_path_raw else None
    if not events_path.is_file():
        raise ValueError(f"events_path not found: {events_path}")

    events = load_events(events_path)
    execution_spec = resolve_run_execution_spec(run)
    selection = select_events_for_execution_window(events, execution_spec)
    selected_events = selection.events
    event_by_id = {str(event.event_id): event for event in selected_events}
    primary_horizon = resolve_run_horizon(run)
    costs = event_proxy_cost_spec(execution_spec)
    initial_value = costs["initial_value"]

    predictions = load_predictions(out_path) if out_path.is_file() else []
    prediction_by_id: dict[str, Any] = {}
    duplicate_predictions: set[str] = set()
    for prediction in predictions:
        event_id = str(prediction.event_id or "")
        if event_id not in event_by_id:
            continue
        if event_id in prediction_by_id:
            duplicate_predictions.add(event_id)
        prediction_by_id[event_id] = prediction

    labels: list[dict[str, Any]] = []
    if labels_path is not None and labels_path.is_file():
        labels = _read_jsonl_objects(labels_path, kind="labels")
    label_by_id = _unique_index(labels, kind="labels") if labels else {}

    ordered_ids = [
        event_id for event_id in chronological_event_ids(selected_events)
        if event_id in prediction_by_id
    ]
    label_key = f"label_{primary_horizon}"
    car_key = f"car_{primary_horizon}"
    ret_key = f"ret_{primary_horizon}"
    benchmark_return_key = f"bm_ret_{primary_horizon}"

    valid_oracle_ids: set[str] = set()
    trades: list[dict[str, Any]] = []
    event_markers: list[dict[str, Any]] = []
    strategy_observations: list[dict[str, Any]] = []
    gross_observations: list[dict[str, Any]] = []
    asset_observations: list[dict[str, Any]] = []
    benchmark_observations: list[dict[str, Any]] = []
    return_floor_count = 0
    running_net_value = 1.0
    peak_net_value = 1.0
    total_cost_amount_proxy = 0.0

    for event_id in ordered_ids:
        event = event_by_id[event_id]
        prediction = prediction_by_id[event_id]
        event_time = str(getattr(event, "event_time", "") or "")
        occurred_at = getattr(event, "occurred_at", None) or event_time
        available_time = getattr(event, "available_time", None) or event_time
        label_row = label_by_id.get(event_id) or {}
        oracle_label = str(label_row.get(label_key) or "")
        oracle_car = _finite(label_row.get(car_key))
        asset_return = _finite(label_row.get(ret_key))
        benchmark_return = _finite(label_row.get(benchmark_return_key))
        oracle_valid = oracle_car is not None and oracle_label in {"up", "down", "neutral"}
        if oracle_valid:
            valid_oracle_ids.add(event_id)
        direction = str(getattr(prediction, "pred_direction", "") or "")
        abstain = bool(getattr(prediction, "abstain", False))
        active = not abstain and direction in {"up", "down"}
        marker: dict[str, Any] = {
            "event_id": event_id,
            "timestamp": event_time,
            "occurred_at": occurred_at,
            "available_time": available_time,
            "symbol": getattr(event, "symbol", None),
            "market": getattr(event, "market", None),
            "title": getattr(event, "title", None),
            "direction": direction,
            "abstain": abstain,
            "confidence": getattr(prediction, "confidence", None),
            "strategy_metadata": getattr(prediction, "strategy_metadata", None),
            "oracle_label": oracle_label or None,
            "oracle_car": oracle_car,
            "oracle_valid": oracle_valid,
            "trade_index": None,
            "net_proxy_return": None,
        }

        if oracle_valid and asset_return is not None:
            asset_observations.append({
                "event_id": event_id,
                "event_time": event_time,
                "asset_return": max(-0.999999, asset_return),
            })
        if oracle_valid and benchmark_return is not None:
            benchmark_observations.append({
                "event_id": event_id,
                "event_time": event_time,
                "benchmark_return": max(-0.999999, benchmark_return),
            })

        if active and oracle_valid:
            signed_car = oracle_car if direction == "up" else -oracle_car
            gross_return = float(signed_car)
            raw_net_return = gross_return - costs["round_trip_cost_rate"]
            net_return = raw_net_return
            return_floor_applied = False
            if net_return <= -1.0:
                net_return = -0.999999
                return_floor_applied = True
                return_floor_count += 1
            cost_amount_proxy = initial_value * running_net_value * costs["round_trip_cost_rate"]
            total_cost_amount_proxy += cost_amount_proxy
            running_net_value *= 1.0 + net_return
            peak_net_value = max(peak_net_value, running_net_value)
            drawdown = running_net_value / peak_net_value - 1.0 if peak_net_value > 0 else None
            trade_index = len(trades) + 1
            trade = {
                "index": trade_index,
                "event_id": event_id,
                "symbol": getattr(event, "symbol", None),
                "market": getattr(event, "market", None),
                "event_time": event_time,
                "occurred_at": occurred_at,
                "available_time": available_time,
                "direction": direction,
                "confidence": getattr(prediction, "confidence", None),
                "strategy_metadata": getattr(prediction, "strategy_metadata", None),
                "prediction_horizon": getattr(prediction, "horizon", None),
                "horizon": primary_horizon,
                "oracle_label": oracle_label,
                "oracle_car": oracle_car,
                "asset_return": asset_return,
                "benchmark_return": benchmark_return,
                "gross_proxy_return": gross_return,
                "cost_rate": costs["round_trip_cost_rate"],
                "cost_basis": "all_in_round_trip_per_event_observation",
                "cost_amount_proxy": cost_amount_proxy,
                "net_proxy_return": net_return,
                "raw_net_proxy_return": raw_net_return,
                "return_floor_applied": return_floor_applied,
                "net_value": running_net_value,
                "equity": initial_value * running_net_value,
                "drawdown": drawdown,
                "entry_model": "oracle_close_window_proxy",
            }
            trades.append(trade)
            strategy_observations.append({
                "event_id": event_id,
                "event_time": event_time,
                "net_proxy_return": net_return,
            })
            gross_observations.append({
                "event_id": event_id,
                "event_time": event_time,
                "gross_proxy_return": max(-0.999999, gross_return),
            })
            marker["trade_index"] = trade_index
            marker["net_proxy_return"] = net_return
        event_markers.append(marker)

    equity_curve = _compound_curve(
        strategy_observations,
        return_key="net_proxy_return",
        initial_value=initial_value,
        include_drawdown=True,
    )
    gross_equity_curve = _compound_curve(
        gross_observations,
        return_key="gross_proxy_return",
        initial_value=initial_value,
        include_drawdown=False,
    )
    asset_curve = _compound_curve(
        asset_observations,
        return_key="asset_return",
        initial_value=initial_value,
        include_drawdown=False,
    )
    benchmark_curve = _compound_curve(
        benchmark_observations,
        return_key="benchmark_return",
        initial_value=initial_value,
        include_drawdown=False,
    )
    drawdown_curve = [
        {
            "index": point["index"],
            "timestamp": point["timestamp"],
            "event_id": point["event_id"],
            "drawdown": point.get("drawdown"),
        }
        for point in equity_curve
    ]
    net_returns = [float(item["net_proxy_return"]) for item in strategy_observations]
    final_net_value = float(equity_curve[-1]["net_value"])
    gross_final_net_value = float(gross_equity_curve[-1]["net_value"])
    has_oracle = bool(label_by_id)
    has_valid_oracle = bool(valid_oracle_ids)
    total_return: Optional[float]
    gross_total_return: Optional[float]
    if has_valid_oracle:
        total_return = final_net_value - 1.0
        gross_total_return = gross_final_net_value - 1.0
    else:
        total_return = None
        gross_total_return = None
    max_drawdown = min((float(point.get("drawdown") or 0.0) for point in equity_curve), default=0.0)
    wins = sum(1 for item in net_returns if item > 0)
    summary = {
        "initial_value": initial_value,
        "final_value": initial_value * final_net_value if total_return is not None else None,
        "total_return": total_return,
        "gross_total_return": gross_total_return,
        "asset_total_return": (
            float(asset_curve[-1]["net_value"]) - 1.0 if asset_observations else None
        ),
        "benchmark_total_return": (
            float(benchmark_curve[-1]["net_value"]) - 1.0 if benchmark_observations else None
        ),
        "max_drawdown": abs(max_drawdown) if total_return is not None else None,
        "sharpe_proxy": _proxy_sharpe(net_returns),
        "win_rate": wins / len(net_returns) if net_returns else (0.0 if has_valid_oracle else None),
        "n_events_in_protocol": len(selected_events),
        "n_predictions": len(prediction_by_id),
        "n_trades": len(trades),
        "n_valid_oracle": len(valid_oracle_ids),
        "n_missing_oracle": len(prediction_by_id) - len(valid_oracle_ids),
        "asset_return_coverage": len(asset_observations) / len(prediction_by_id) if prediction_by_id else 0.0,
        "benchmark_return_coverage": len(benchmark_observations) / len(prediction_by_id) if prediction_by_id else 0.0,
        "fee_bps": costs["fee_bps"],
        "slippage_bps": costs["slippage_bps"],
        "round_trip_cost_bps": costs["round_trip_cost_bps"],
        "total_cost_rate_sum": len(trades) * costs["round_trip_cost_rate"],
        "total_cost_amount_proxy": total_cost_amount_proxy,
        "return_floor_count": return_floor_count,
    }

    if not has_oracle or not has_valid_oracle:
        status = "unavailable"
    elif len(valid_oracle_ids) < len(prediction_by_id):
        status = "partial"
    else:
        status = "available"

    kline_refs = [
        {
            "event_id": event_id,
            "url": f"/api/bt/runs/{quote(run_id, safe='')}/events/{quote(event_id, safe='')}/kline",
        }
        for event_id in ordered_ids
    ]
    kline_by_event: dict[str, dict[str, Any]] = {}
    if include_kline:
        kline_event_ids = [item["event_id"] for item in trades[:kline_limit]]
        if len(kline_event_ids) < kline_limit:
            kline_event_ids.extend(
                event_id for event_id in ordered_ids
                if event_id not in kline_event_ids
            )
        for event_id in kline_event_ids[:kline_limit]:
            try:
                kline_by_event[event_id] = {
                    "ok": True,
                    "payload": kline_loader(event_by_id[event_id]),
                }
            except Exception as exc:  # noqa: BLE001 - per-event degradation is part of the contract
                kline_by_event[event_id] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    requested_delay = execution_spec.get("execution_delay", execution_spec.get("entry_rule"))
    requested_price = execution_spec.get("price_field")
    recorded_not_applied: dict[str, Any] = {}
    applied_execution_delay: dict[str, Any] = {}
    if requested_delay is not None:
        normalized_delay = str(requested_delay).strip().lower().replace("-", "_").replace(" ", "_")
        if normalized_delay in {"event_close", "information_close_window"}:
            applied_execution_delay["execution_delay"] = {
                "requested": requested_delay,
                "effective": "information_close_window",
                "status": "applied_as_oracle_return_anchor",
            }
        else:
            recorded_not_applied["execution_delay"] = {
                "requested": requested_delay,
                "reason": "事件 Oracle 当前只有收盘窗口收益，不能证明 next_open/next_close 成交价已执行",
            }
    if requested_price not in (None, "close"):
        recorded_not_applied["price_field"] = {
            "requested": requested_price,
            "reason": "Oracle 标签由真实 close 序列计算；未使用 open/vwap 成交撮合",
        }
    for inactive_key in ("leverage", "futures_roll_rule", "target_weight", "position_limit"):
        if execution_spec.get(inactive_key) is not None:
            recorded_not_applied[inactive_key] = {
                "requested": execution_spec.get(inactive_key),
                "reason": "event_proxy 不模拟仓位、资金占用或换月",
            }

    event_decisions = [
        {
            **marker,
            "is_correct": (
                marker.get("oracle_label") != "neutral"
                and marker.get("direction") == marker.get("oracle_label")
                if marker.get("oracle_valid") is True
                and not marker.get("abstain")
                and marker.get("direction") in {"up", "down", "neutral"}
                else None
            ),
        }
        for marker in event_markers
    ]
    correct_count = sum(1 for item in event_decisions if item.get("is_correct") is True)
    incorrect_count = sum(1 for item in event_decisions if item.get("is_correct") is False)
    non_neutral_decisions = [
        item for item in event_decisions
        if item.get("oracle_valid") is True
        and item.get("oracle_label") in {"up", "down"}
        and not item.get("abstain")
        and item.get("direction") in {"up", "down", "neutral"}
    ]
    non_neutral_correct = sum(
        1 for item in non_neutral_decisions
        if item.get("direction") == item.get("oracle_label")
    )

    return {
        "run_id": run_id,
        "mode": "event_proxy",
        "engine_mode": "event_proxy",
        "result_nature": "proxy",
        "status": status,
        "primary_horizon": primary_horizon,
        "currency": execution_spec.get("currency"),
        "initial_value": initial_value,
        "as_of": run.get("finished_at") or run.get("updated_at"),
        "data_frozen": bool(run.get("protocol_hash")),
        "summary": summary,
        "equity_curve": equity_curve,
        "gross_equity_curve": gross_equity_curve,
        "drawdown_curve": drawdown_curve,
        "asset_curve": asset_curve,
        "benchmark_curve": benchmark_curve,
        "trades": trades,
        "event_markers": event_markers,
        "event_decisions": event_decisions,
        "correctness": {
            "horizon": primary_horizon,
            "policy": "strict: abstain/missing CAR excluded; any neutral outcome is incorrect",
            "scored": correct_count + incorrect_count,
            "correct": correct_count,
            "incorrect": incorrect_count,
            "accuracy": correct_count / (correct_count + incorrect_count)
            if correct_count + incorrect_count else None,
            "oracle_non_neutral": {
                "scored": len(non_neutral_decisions),
                "correct": non_neutral_correct,
                "incorrect": len(non_neutral_decisions) - non_neutral_correct,
                "accuracy": non_neutral_correct / len(non_neutral_decisions)
                if non_neutral_decisions else None,
            },
            "abstain": sum(1 for marker in event_markers if marker.get("abstain")),
            "neutral_predictions": sum(
                1 for marker in event_markers
                if not marker.get("abstain") and marker.get("direction") == "neutral"
            ),
        },
        "kline_by_event": kline_by_event,
        "kline_refs": kline_refs,
        "effective_protocol": {
            "requested": execution_spec,
            "applied": {
                "evaluation_horizon": primary_horizon,
                "evaluator_version": EVALUATOR_VERSION,
                "event_window": selection.to_dict(),
                "return_basis": "prediction direction × selected-horizon Oracle CAR",
                "ordering": "event_time then event_id",
                "positioning": "one equal-notional observation per active event",
                "cost_model": "fee_bps + slippage_bps interpreted as one all-in round-trip event-window proxy charge; no order legs are fabricated",
                "fee_bps": costs["fee_bps"],
                "slippage_bps": costs["slippage_bps"],
                "round_trip_cost_bps": costs["round_trip_cost_bps"],
                "initial_capital_use": "display amount only; percentage returns do not depend on it",
                "oracle_price_basis": "real close series; event-day close for pre-open/intraday and next trading close after post-close events",
                **applied_execution_delay,
            },
            "recorded_not_applied": recorded_not_applied,
        },
        "data_quality": {
            "labels_file_present": labels_path is not None and labels_path.is_file(),
            "duplicate_prediction_event_ids": sorted(duplicate_predictions),
            "valid_oracle_event_ids": len(valid_oracle_ids),
            "missing_oracle_event_ids": sorted(set(prediction_by_id) - valid_oracle_ids),
            "asset_curve_is_event_compounded": True,
            "benchmark_curve_is_event_compounded": True,
            "kline_is_fetched_on_request_not_frozen": bool(include_kline),
            "accuracy_requires_matching_finite_car": True,
        },
        "proxy_disclaimer": (
            "这是事件逐笔收益代理，不是完整组合回测。净收益按预测方向乘以所选窗口的真实 Oracle CAR，"
            "每个非中性事件按一次全程往返代理扣除 fee_bps+slippage_bps（不是逐订单成交成本）；日期窗口已生效。未模拟重叠持仓、资金占用、"
            "目标权重、杠杆、订单级开盘/VWAP 成交、期货换月或订单撮合。资产/基准曲线也是事件序列复利，"
            "不是连续可投资组合净值；缺失 CAR 不会按 0 处理。"
        ),
    }


def _quantile(values: list[float], probability: float) -> Optional[float]:
    """Return a linearly interpolated sample quantile for finite values."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(1.0, max(0.0, probability)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _lower_tail_mean(values: list[float], probability: float) -> Optional[float]:
    """Return the mean of an exact lower-tail probability mass.

    Selecting every observation below a quantile threshold makes a discrete
    expected-shortfall estimate depend on how many observations tie at that
    threshold.  Weighting the boundary observation fractionally keeps the tail
    mass fixed (five observations for a 100-row sample at probability 5%).
    """
    if not values or probability <= 0:
        return None
    ordered = sorted(values)
    tail_mass = min(1.0, probability) * len(ordered)
    whole_observations = min(len(ordered), math.floor(tail_mass))
    fractional_observation = tail_mass - whole_observations
    weighted_sum = sum(ordered[:whole_observations])
    if fractional_observation > 0 and whole_observations < len(ordered):
        weighted_sum += ordered[whole_observations] * fractional_observation
    return weighted_sum / tail_mass


def _portfolio_prediction_only(
    metrics: Mapping[str, Any],
    strategy: Mapping[str, Any] | None = None,
) -> bool:
    """Recognize portfolio-inapplicable forecasts without truthy coercion."""
    strategy = strategy or {}
    return (
        metrics.get("prediction_only") is True
        or strategy.get("prediction_only") is True
        or str(strategy.get("kind") or "").strip().lower() == "return_forecast"
    )


def _longest_true_run(flags: list[bool]) -> int:
    longest = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


def _portfolio_drawdown_analysis(curve: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe the worst full-curve drawdown without returning another series."""
    valid: list[tuple[int, dict[str, Any], float]] = []
    for index, point in enumerate(curve):
        net_value = _finite(point.get("net_value"))
        if net_value is not None and net_value > 0:
            valid.append((index, point, net_value))
    if not valid:
        return {
            "max_drawdown": None,
            "peak_timestamp": None,
            "trough_timestamp": None,
            "recovery_timestamp": None,
            "peak_to_trough_bars": None,
            "recovery_bars": None,
            "underwater_bars": None,
            "longest_underwater_bars": None,
            "current_drawdown": None,
            "recovered": None,
        }

    running_peak = valid[0][2]
    running_peak_position = 0
    worst_drawdown = 0.0
    worst_peak_position = worst_trough_position = 0
    worst_peak_net_value = running_peak
    drawdown_flags: list[bool] = []
    for position, (_, _point, net_value) in enumerate(valid):
        if net_value > running_peak:
            running_peak = net_value
            running_peak_position = position
            at_peak = True
        elif math.isclose(net_value, running_peak, rel_tol=1e-12, abs_tol=1e-15):
            # Attribute a later drawdown to the most recent revisit of the
            # high-water mark, rather than stretching its duration back to an
            # older equal peak.  The tolerance also absorbs harmless floating
            # point drift in a reconstructed net-value curve.
            running_peak_position = position
            at_peak = True
        else:
            at_peak = False
        drawdown = 0.0 if at_peak else net_value / running_peak - 1.0
        drawdown_flags.append(not at_peak)
        if drawdown < worst_drawdown:
            worst_drawdown = drawdown
            worst_peak_position = running_peak_position
            worst_trough_position = position
            worst_peak_net_value = running_peak

    if worst_drawdown == 0.0:
        return {
            "max_drawdown": 0.0,
            "peak_timestamp": None,
            "trough_timestamp": None,
            "recovery_timestamp": None,
            "peak_to_trough_bars": None,
            "recovery_bars": None,
            "underwater_bars": 0,
            "longest_underwater_bars": 0,
            "current_drawdown": 0.0,
            "recovered": None,
        }

    peak_row = valid[worst_peak_position]
    trough_row = valid[worst_trough_position]
    recovery_position: int | None = None
    peak_net_value = worst_peak_net_value
    for position in range(worst_trough_position + 1, len(valid)):
        if valid[position][2] >= peak_net_value * (1.0 - 1e-12):
            recovery_position = position
            break
    current_peak = max(item[2] for item in valid)
    current_drawdown = (
        0.0
        if math.isclose(valid[-1][2], current_peak, rel_tol=1e-12, abs_tol=1e-15)
        else valid[-1][2] / current_peak - 1.0
    )
    return {
        "max_drawdown": abs(worst_drawdown),
        "peak_timestamp": peak_row[1].get("timestamp"),
        "trough_timestamp": trough_row[1].get("timestamp"),
        "recovery_timestamp": (
            valid[recovery_position][1].get("timestamp") if recovery_position is not None else None
        ),
        "peak_to_trough_bars": worst_trough_position - worst_peak_position,
        "recovery_bars": (
            recovery_position - worst_trough_position if recovery_position is not None else None
        ),
        "underwater_bars": (
            recovery_position - worst_peak_position
            if recovery_position is not None else len(valid) - 1 - worst_peak_position
        ),
        "longest_underwater_bars": _longest_true_run(drawdown_flags),
        "current_drawdown": abs(min(0.0, current_drawdown)),
        "recovered": recovery_position is not None,
    }


def _portfolio_financial_analysis(
    *,
    metrics: Mapping[str, Any],
    dataset: Mapping[str, Any],
    curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    currency: Any = None,
    strategy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute compact, auditable finance statistics from the full frozen result.

    This deliberately returns only scalars and short methodology metadata.  It
    is calculated before the browser response is sampled, so old and new Run
    artifacts receive the same detailed analysis without rewriting the source.
    """
    prediction_only = _portfolio_prediction_only(metrics, strategy)
    if prediction_only:
        return {
            "schema_version": "pronoia.portfolio-financial-analysis.v1",
            "status": "not_applicable",
            "reason": "prediction_only Run 没有组合仓位、模拟成交或策略净值，不能生成组合金融指标",
            "metrics_basis": "full_frozen_result",
        }

    full_curve = [point for point in curve if isinstance(point, dict)]
    period_returns: list[float] = []
    active_returns: list[float] = []
    positions: list[float] = []
    paired_strategy_returns: list[float] = []
    paired_benchmark_returns: list[float] = []
    aligned_start_at: Any = None
    aligned_end_at: Any = None
    for index, point in enumerate(full_curve):
        position = _finite(point.get("position"))
        turnover = _finite(point.get("turnover")) or 0.0
        net_return = _finite(point.get("net_return"))
        benchmark_value = _finite(point.get("benchmark_net_value"))
        # curve[0] is the engine's initial state, not a return/exposure
        # interval.  Including it makes short runs look artificially flat.
        if index > 0 and position is not None:
            positions.append(position)
        if index > 0:
            if net_return is None:
                current_net_value = _finite(point.get("net_value"))
                previous_net_value = _finite(full_curve[index - 1].get("net_value"))
                if (
                    current_net_value is not None and previous_net_value is not None
                    and previous_net_value > 0
                ):
                    net_return = current_net_value / previous_net_value - 1.0
            if net_return is not None:
                period_returns.append(net_return)
                if (position is not None and abs(position) > 1e-12) or turnover > 1e-12:
                    active_returns.append(net_return)
            previous_benchmark = _finite(full_curve[index - 1].get("benchmark_net_value"))
            if (
                net_return is not None and benchmark_value is not None
                and previous_benchmark is not None and previous_benchmark > 0
                and point.get("timestamp") is not None
                and full_curve[index - 1].get("timestamp") is not None
            ):
                paired_strategy_returns.append(net_return)
                paired_benchmark_returns.append(benchmark_value / previous_benchmark - 1.0)
                if aligned_start_at is None:
                    aligned_start_at = full_curve[index - 1].get("timestamp")
                aligned_end_at = point.get("timestamp")

    annual_periods = int(_finite(metrics.get("annualization_periods")) or 0)
    period_count = len(period_returns)
    mean_return = statistics.fmean(period_returns) if period_returns else None
    downside_deviation = None
    sortino_ratio = None
    if period_returns and annual_periods > 0:
        downside_deviation = math.sqrt(
            statistics.fmean(min(value, 0.0) ** 2 for value in period_returns)
        ) * math.sqrt(annual_periods)
        if downside_deviation > 0 and mean_return is not None:
            sortino_ratio = mean_return * annual_periods / downside_deviation

    percentile_5 = _quantile(period_returns, 0.05)
    lower_tail_mean = _lower_tail_mean(period_returns, 0.05)
    historical_var_95 = max(0.0, -percentile_5) if percentile_5 is not None else None
    historical_cvar_95 = (
        max(0.0, -lower_tail_mean) if lower_tail_mean is not None else None
    )

    correlation = beta = alpha = tracking_error = information_ratio = None
    aligned_count = len(paired_strategy_returns)
    if aligned_count >= 2 and annual_periods > 0:
        strategy_mean = statistics.fmean(paired_strategy_returns)
        benchmark_mean = statistics.fmean(paired_benchmark_returns)
        strategy_deviation = statistics.pstdev(paired_strategy_returns)
        benchmark_deviation = statistics.pstdev(paired_benchmark_returns)
        covariance = statistics.fmean(
            (strategy_value - strategy_mean) * (benchmark_value - benchmark_mean)
            for strategy_value, benchmark_value in zip(
                paired_strategy_returns, paired_benchmark_returns, strict=True
            )
        )
        if strategy_deviation > 0 and benchmark_deviation > 0:
            correlation = max(
                -1.0,
                min(1.0, covariance / (strategy_deviation * benchmark_deviation)),
            )
        benchmark_variance = benchmark_deviation ** 2
        if benchmark_variance > 0:
            beta = covariance / benchmark_variance
            # Arithmetic single-period CAPM alpha, annualized; risk-free rate is 0.
            alpha = (strategy_mean - beta * benchmark_mean) * annual_periods
        active_differences = [
            strategy_value - benchmark_value
            for strategy_value, benchmark_value in zip(
                paired_strategy_returns, paired_benchmark_returns, strict=True
            )
        ]
        active_mean = statistics.fmean(active_differences)
        active_deviation = statistics.pstdev(active_differences)
        tracking_error = active_deviation * math.sqrt(annual_periods)
        if active_deviation > 0:
            information_ratio = active_mean / active_deviation * math.sqrt(annual_periods)

    exposure_count = len(positions)
    long_count = sum(1 for value in positions if value > 1e-12)
    short_count = sum(1 for value in positions if value < -1e-12)
    flat_count = exposure_count - long_count - short_count
    active_flags = [abs(value) > 1e-12 for value in positions]
    absolute_positions = [abs(value) for value in positions]

    buy_count = sum(1 for item in trades if str(item.get("side") or "").lower() == "buy")
    sell_count = sum(1 for item in trades if str(item.get("side") or "").lower() == "sell")
    entry_count = exit_count = reversal_count = 0
    trade_notionals: list[float] = []
    trade_turnovers: list[float] = []
    trade_costs: list[float] = []
    for item in trades:
        before = _finite(item.get("from_weight"))
        after = _finite(item.get("to_weight"))
        if before is not None and after is not None:
            if abs(before) <= 1e-12 and abs(after) > 1e-12:
                entry_count += 1
            if abs(before) > 1e-12 and abs(after) <= 1e-12:
                exit_count += 1
            if before * after < 0:
                reversal_count += 1
        if (value := _finite(item.get("notional"))) is not None:
            trade_notionals.append(value)
        if (value := _finite(item.get("turnover"))) is not None:
            trade_turnovers.append(value)
        if (value := _finite(item.get("total_cost"))) is not None:
            trade_costs.append(value)

    drawdown = _portfolio_drawdown_analysis(full_curve)
    max_drawdown_metric = _finite(metrics.get("max_drawdown"))
    drawdown["reported_max_drawdown"] = max_drawdown_metric
    drawdown["matches_reported_max_drawdown"] = (
        math.isclose(
            float(drawdown["max_drawdown"]), max_drawdown_metric,
            rel_tol=1e-9, abs_tol=1e-12,
        )
        if drawdown.get("max_drawdown") is not None and max_drawdown_metric is not None
        else None
    )

    positive_active = sum(1 for value in active_returns if value > 0)
    negative_active = sum(1 for value in active_returns if value < 0)
    zero_active = len(active_returns) - positive_active - negative_active
    trade_count = len(trades)
    notional_observation_count = len(trade_notionals)
    trade_cost_observation_count = len(trade_costs)
    notional_coverage_complete = notional_observation_count == trade_count
    trade_cost_coverage_complete = trade_cost_observation_count == trade_count
    notional_coverage_ratio = (
        notional_observation_count / trade_count if trade_count else 1.0
    )
    trade_cost_coverage_ratio = (
        trade_cost_observation_count / trade_count if trade_count else 1.0
    )
    covered_notional = sum(trade_notionals)
    covered_trade_cost = sum(trade_costs)
    total_notional = covered_notional if notional_coverage_complete else None
    reported_total_cost = _finite(metrics.get("total_cost"))
    total_cost = (
        reported_total_cost
        if reported_total_cost is not None
        else covered_trade_cost if trade_cost_coverage_complete else None
    )
    total_cost_basis = (
        "reported_metric"
        if reported_total_cost is not None
        else "complete_trade_records" if trade_cost_coverage_complete else "unavailable"
    )
    current_exposure = (
        _finite(full_curve[-1].get("position")) if full_curve else None
    )
    return {
        "schema_version": "pronoia.portfolio-financial-analysis.v1",
        "status": "available",
        "metrics_basis": "full_frozen_result",
        "units": {
            "returns_and_ratios": "decimal",
            "money": str(currency or dataset.get("currency") or "unspecified"),
            "duration": "bars",
            "turnover": "absolute_target_weight_change",
        },
        "period": {
            "start_at": dataset.get("start_at") or (full_curve[0].get("timestamp") if full_curve else None),
            "end_at": dataset.get("end_at") or (full_curve[-1].get("timestamp") if full_curve else None),
            "frequency": dataset.get("frequency"),
            "bar_count": int(metrics.get("bar_count") or len(full_curve)),
            "return_observation_count": period_count,
            "annualization_periods": annual_periods or None,
            "estimated_years": period_count / annual_periods if annual_periods > 0 else None,
        },
        "returns": {
            "initial_capital": _finite(metrics.get("initial_capital")),
            "final_equity": _finite(metrics.get("final_equity")),
            "total_return": _finite(metrics.get("total_return")),
            "annualized_return": _finite(metrics.get("annualized_return")),
            "benchmark_total_return": _finite(metrics.get("benchmark_total_return")),
            "excess_total_return": _finite(metrics.get("excess_total_return")),
            "average_bar_return": mean_return,
            "best_bar_return": max(period_returns) if period_returns else None,
            "worst_bar_return": min(period_returns) if period_returns else None,
        },
        "risk": {
            "annualized_volatility": _finite(metrics.get("annualized_volatility")),
            "annualized_downside_deviation": downside_deviation,
            "sharpe_ratio": _finite(metrics.get("sharpe_ratio")),
            "sortino_ratio": sortino_ratio,
            "calmar_ratio": _finite(metrics.get("calmar_ratio")),
            "historical_var_95_one_bar": historical_var_95,
            "historical_cvar_95_one_bar": historical_cvar_95,
            "max_consecutive_positive_bars": _longest_true_run([value > 0 for value in period_returns]),
            "max_consecutive_negative_bars": _longest_true_run([value < 0 for value in period_returns]),
            **drawdown,
        },
        "benchmark": {
            "aligned_return_count": aligned_count,
            "aligned_start_at": aligned_start_at,
            "aligned_end_at": aligned_end_at,
            "correlation": correlation,
            "beta": beta,
            "annualized_alpha_rf0": alpha,
            "annualized_tracking_error": tracking_error,
            "information_ratio": information_ratio,
            "alignment_basis": "strategy and benchmark returns from the same equity_curve timestamp",
        },
        "trading": {
            "position_change_count": trade_count,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "entry_count": entry_count,
            "exit_count": exit_count,
            "reversal_count": reversal_count,
            "active_bar_count": len(active_returns),
            "positive_active_bar_count": positive_active,
            "negative_active_bar_count": negative_active,
            "zero_active_bar_count": zero_active,
            "active_bar_win_rate": _finite(metrics.get("win_rate")),
            "active_bar_profit_factor": _finite(metrics.get("profit_factor")),
            "total_turnover": _finite(metrics.get("total_turnover")),
            "average_turnover_per_change": statistics.fmean(trade_turnovers) if trade_turnovers else None,
            "maximum_turnover_per_change": max(trade_turnovers) if trade_turnovers else None,
            "notional_observation_count": notional_observation_count,
            "notional_coverage_ratio": notional_coverage_ratio,
            "notional_coverage_complete": notional_coverage_complete,
            "covered_traded_notional": covered_notional,
            "total_traded_notional": total_notional,
            "average_traded_notional": (
                statistics.fmean(trade_notionals)
                if notional_coverage_complete and trade_notionals else None
            ),
            "maximum_traded_notional": (
                max(trade_notionals)
                if notional_coverage_complete and trade_notionals else None
            ),
        },
        "exposure": {
            "observation_count": exposure_count,
            "time_in_market_ratio": (
                (long_count + short_count) / exposure_count if exposure_count else None
            ),
            "long_exposure_ratio": long_count / exposure_count if exposure_count else None,
            "short_exposure_ratio": short_count / exposure_count if exposure_count else None,
            "flat_ratio": flat_count / exposure_count if exposure_count else None,
            "average_signed_exposure": statistics.fmean(positions) if positions else None,
            "average_absolute_exposure": statistics.fmean(absolute_positions) if positions else None,
            "maximum_absolute_exposure": max(absolute_positions) if positions else None,
            "current_exposure": current_exposure,
            "longest_in_market_bars": _longest_true_run(active_flags),
        },
        "costs": {
            "total_cost": total_cost,
            "total_cost_basis": total_cost_basis,
            "total_commission": _finite(metrics.get("total_commission")),
            "total_slippage": _finite(metrics.get("total_slippage")),
            "total_stamp_duty": _finite(metrics.get("total_stamp_duty")),
            "total_other_cost": _finite(metrics.get("total_other_cost")),
            "cost_drag_ratio_to_initial_capital": _finite(metrics.get("cost_drag_ratio")),
            "trade_cost_observation_count": trade_cost_observation_count,
            "trade_cost_coverage_ratio": trade_cost_coverage_ratio,
            "trade_cost_coverage_complete": trade_cost_coverage_complete,
            "covered_trade_cost": covered_trade_cost,
            "cost_to_traded_notional_ratio": (
                total_cost / total_notional
                if total_cost is not None and total_notional is not None and total_notional > 0
                else None
            ),
            "average_cost_per_change": (
                statistics.fmean(trade_costs)
                if trade_cost_coverage_complete and trade_costs else None
            ),
            "maximum_cost_per_change": (
                max(trade_costs)
                if trade_cost_coverage_complete and trade_costs else None
            ),
        },
        "methodology": {
            "risk_free_rate": 0.0,
            "sharpe_basis": "arithmetic mean of all bar returns / population standard deviation, annualized",
            "sortino_basis": "annualized arithmetic mean / annualized downside RMS around 0",
            "var_basis": "historical one-bar return distribution at 95% confidence; loss is reported positive",
            "cvar_basis": "mean loss over exactly 5% historical one-bar left-tail probability mass; boundary observations are fractionally weighted and loss is reported positive",
            "alpha_basis": "same-timestamp arithmetic bar returns, CAPM beta, rf=0, annualized",
            "trade_basis": "target-weight changes at next bar open; not paired round trips",
            "exposure_basis": "post-open target exposure for return intervals; initial no-return snapshot excluded",
            "trade_coverage_basis": "totals and averages requiring per-trade fields are unavailable unless every trade is covered",
            "win_rate_basis": "positive net-return share of active bars; not completed-trade win rate",
            "source_arrays_returned": False,
        },
    }


def build_portfolio_performance(run: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt a frozen quant/API result to the same chart/export contract."""
    result_path = Path(str(run.get("result_path") or ""))
    if not result_path.is_file():
        return {
            "run_id": str(run.get("id") or ""),
            "mode": "portfolio",
            "engine_mode": "portfolio",
            "result_nature": "unavailable",
            "status": "unavailable",
            "dataset_version": run.get("dataset_version"),
            "summary": {}, "equity_curve": [], "drawdown_curve": [], "trades": [],
            "orders": [], "positions": [], "bars": [], "signals": [],
            "event_markers": [], "event_decisions": [], "correctness": None,
            "data_frozen": bool(run.get("dataset_version")),
            "effective_protocol": {
                "requested": (run.get("execution_spec") or {}).get("requested", {}),
                "applied": (run.get("execution_spec") or {}).get("applied", {}),
                "recorded_not_applied": (run.get("execution_spec") or {}).get("recorded_not_applied", {}),
            },
            "proxy_disclaimer": "该 Run 尚无组合执行结果；系统没有补造收益、成交或行情。",
        }
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"portfolio result 无法解析: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("portfolio result 根节点必须是对象")
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else {}
    expected_version = str(run.get("dataset_version") or "")
    actual_version = str(contract.get("dataset_version") or "")
    if expected_version and actual_version != expected_version:
        raise ValueError("portfolio result 的 dataset_version 与 Run 冻结版本不一致")
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    dataset = payload.get("dataset") if isinstance(payload.get("dataset"), dict) else {}
    strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
    return_forecasts = [
        dict(item)
        for item in (payload.get("return_forecasts") or [])
        if isinstance(item, dict)
    ]
    return_forecast = enrich_forecast_summary(
        metrics.get("return_forecast") if isinstance(metrics.get("return_forecast"), dict) else {},
        return_forecasts,
        horizon_key="horizon_bars",
    )
    dataset_symbol = str(dataset.get("symbol") or "") or None
    dataset_market = str(dataset.get("market") or "") or None
    curve = [dict(point) for point in (payload.get("equity_curve") or []) if isinstance(point, dict)]
    drawdown = [dict(point) for point in (payload.get("drawdown_curve") or []) if isinstance(point, dict)]
    trades = [
        {
            **dict(item),
            "symbol": item.get("symbol") or dataset_symbol,
            "market": item.get("market") or dataset_market,
        }
        for item in (payload.get("trades") or []) if isinstance(item, dict)
    ]
    orders = [{**item, "status": "filled", "order_type": "target_weight_at_next_open"} for item in trades]
    positions = [
        {
            "timestamp": point.get("timestamp"),
            "symbol": dataset_symbol, "market": dataset_market,
            "weight": point.get("position"), "target_weight": point.get("position"),
            "equity": point.get("equity"), "net_value": point.get("net_value"),
            "market_value": (
                float(point.get("equity")) * float(point.get("position"))
                if isinstance(point.get("equity"), (int, float)) and isinstance(point.get("position"), (int, float))
                else None
            ),
        }
        for point in curve
    ]
    initial = metrics.get("initial_capital")
    final = metrics.get("final_equity")
    summary = {
        "initial_value": initial,
        "final_value": final,
        "total_return": metrics.get("total_return"),
        "annualized_return": metrics.get("annualized_return"),
        "annualized_volatility": metrics.get("annualized_volatility"),
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "sharpe_proxy": metrics.get("sharpe_ratio"),
        "max_drawdown": metrics.get("max_drawdown"),
        "calmar_ratio": metrics.get("calmar_ratio"),
        "profit_factor": metrics.get("profit_factor"),
        "win_rate": metrics.get("win_rate"),
        "win_rate_basis": metrics.get("win_rate_basis") or "active_period",
        "benchmark_total_return": metrics.get("benchmark_total_return"),
        "excess_total_return": metrics.get("excess_total_return"),
        "n_trades": int(metrics.get("trade_count") or len(trades)),
        "trade_count": int(metrics.get("trade_count") or len(trades)),
        "bar_count": int(metrics.get("bar_count") or len(payload.get("bars") or [])),
        "total_turnover": metrics.get("total_turnover"),
        "total_commission": metrics.get("total_commission"),
        "total_slippage": metrics.get("total_slippage"),
        "total_stamp_duty": metrics.get("total_stamp_duty"),
        "total_other_cost": metrics.get("total_other_cost"),
        "total_cost": metrics.get("total_cost"),
        "cost_drag_ratio": metrics.get("cost_drag_ratio"),
    }
    # The frozen benchmark follows the asset's closes regardless of whether
    # the model produced positions. Keep it before suppressing the zero-weight
    # simulator output of a prediction-only run.
    asset_curve = [
        {"timestamp": point.get("timestamp"), "net_value": point.get("benchmark_net_value")}
        for point in curve
    ]
    prediction_only = _portfolio_prediction_only(metrics, strategy)
    artifact_execution_contract = (
        contract.get("execution_protocol")
        if isinstance(contract.get("execution_protocol"), dict) else {}
    )
    execution_contract = (
        artifact_execution_contract
        if artifact_execution_contract
        else run.get("execution_spec") if isinstance(run.get("execution_spec"), dict) else {}
    )
    requested_execution = (
        execution_contract.get("requested")
        if isinstance(execution_contract.get("requested"), dict) else {}
    )
    applied_execution = (
        execution_contract.get("applied")
        if isinstance(execution_contract.get("applied"), dict) else {}
    )
    financial_analysis = _portfolio_financial_analysis(
        metrics=metrics,
        dataset=dataset,
        curve=curve,
        trades=trades,
        currency=applied_execution.get("currency") or requested_execution.get("currency"),
        strategy=strategy,
    )
    if prediction_only:
        summary = {
            "bar_count": summary["bar_count"],
            "benchmark_total_return": summary["benchmark_total_return"],
        }
        curve, drawdown, trades, orders, positions = [], [], [], [], []
    return {
        "run_id": str(run.get("id") or ""),
        "mode": "portfolio",
        "engine_mode": "portfolio",
        "result_nature": "simulated_from_real_bars",
        "status": "available",
        "dataset_version": expected_version or None,
        "dataset": dataset,
        "strategy": strategy,
        "currency": applied_execution.get("currency") or requested_execution.get("currency"),
        "initial_value": None if prediction_only else initial,
        "as_of": run.get("finished_at") or run.get("updated_at"),
        "data_frozen": True,
        "summary": summary,
        "financial_analysis": financial_analysis,
        "prediction_only": prediction_only,
        "return_forecast": return_forecast,
        "return_forecasts": return_forecasts,
        "equity_curve": curve,
        "gross_equity_curve": [],
        "drawdown_curve": drawdown,
        "asset_curve": asset_curve,
        "benchmark_curve": [dict(point) for point in asset_curve],
        "trades": trades,
        "orders": orders,
        "positions": positions,
        "bars": [dict(item) for item in (payload.get("bars") or []) if isinstance(item, dict)],
        "signals": [dict(item) for item in (payload.get("signals") or []) if isinstance(item, dict)],
        "event_markers": [],
        "event_decisions": [],
        "correctness": None,
        "kline_by_event": {}, "kline_refs": [],
        "effective_protocol": {
            "requested": execution_contract.get("requested", {}),
            "applied": {
                **execution_contract.get("applied", {}),
                "engine": "single_asset_next_open",
                "dataset_version": expected_version,
            },
            "recorded_not_applied": execution_contract.get("recorded_not_applied", {}),
        },
        "data_quality": {
            "source": dataset.get("source_type"),
            "source_ref": dataset.get("source_ref"),
            "frequency": dataset.get("frequency"),
            "bar_count": len(payload.get("bars") or []),
            "frozen_bars_embedded_in_result": True,
            "warnings": list(payload.get("warnings") or []),
        },
        "proxy_disclaimer": "基于冻结行情的收益率数值预测，以 T+N 标的收盘收益评估误差；本次不产生交易或策略净值。" if prediction_only else (
            "这是基于冻结真实 OHLC bar 的单标的 target-weight 模拟，不是券商实盘成交。信号在 bar 收盘形成，"
            "下一根 bar 开盘按配置手续费和滑点成交；不支持多资产资金分配、期货合约乘数/保证金/换月或订单簿冲击。"
            "win_rate 字段表示持仓活跃 bar 的正收益比例，不是配对完成交易的胜率。"
        ),
    }


def build_unified_performance(
    run: Mapping[str, Any],
    *,
    include_kline: bool = False,
    kline_limit: int = 6,
) -> dict[str, Any]:
    if str(run.get("engine_mode") or "event_proxy") == "portfolio":
        return build_portfolio_performance(run)
    return build_event_proxy_performance(run, include_kline=include_kline, kline_limit=kline_limit)
