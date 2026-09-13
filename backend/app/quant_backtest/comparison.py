"""Strict multi-strategy performance comparison on one frozen dataset."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .models import QuantBacktestError


_HIGHER_IS_BETTER = ("total_return", "annualized_return", "sharpe_ratio", "win_rate", "profit_factor")
_LOWER_IS_BETTER = ("max_drawdown", "total_cost")


def compare_results(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(runs) < 2:
        raise QuantBacktestError("策略对比至少需要 2 个回测结果")
    missing = [str(run.get("id")) for run in runs if not isinstance(run.get("result"), dict)]
    if missing:
        raise QuantBacktestError(f"以下回测尚无结果: {', '.join(missing)}")
    if any(
        (run["result"].get("metrics") or {}).get("prediction_only")
        or (run["result"].get("strategy") or {}).get("prediction_only")
        or (run["result"].get("strategy") or {}).get("kind") == "return_forecast"
        for run in runs
    ):
        raise QuantBacktestError(
            "纯收益率预测不能进入组合总收益排名；请按相同标的、行情与预测周期比较收益预测误差（MAE/RMSE）"
        )
    fingerprints = {str(run.get("dataset_fingerprint") or "") for run in runs}
    protocols = {str(run.get("comparison_hash") or "") for run in runs}
    if len(fingerprints) != 1 or "" in fingerprints:
        raise QuantBacktestError("只能对比同一行情快照上的策略")
    if len(protocols) != 1 or "" in protocols:
        raise QuantBacktestError("只能对比手续费、滑点和成交时点一致的策略")

    run_items: list[dict[str, Any]] = []
    curve_maps: dict[str, dict[str, Mapping[str, Any]]] = {}
    common_timestamps: set[str] | None = None
    for run in runs:
        run_id = str(run["id"])
        result = run["result"]
        curve = result.get("equity_curve") or []
        by_timestamp = {str(point["timestamp"]): point for point in curve}
        curve_maps[run_id] = by_timestamp
        common_timestamps = set(by_timestamp) if common_timestamps is None else common_timestamps & set(by_timestamp)
        run_items.append(
            {
                "id": run_id,
                "name": run.get("name"),
                "strategy": result.get("strategy"),
                "metrics": result.get("metrics"),
            }
        )
    timestamps = sorted(common_timestamps or [])
    series = []
    first_run = str(runs[0]["id"])
    for timestamp in timestamps:
        series.append(
            {
                "timestamp": timestamp,
                "benchmark_net_value": curve_maps[first_run][timestamp].get("benchmark_net_value"),
                "net_values": {
                    str(run["id"]): curve_maps[str(run["id"])][timestamp].get("net_value") for run in runs
                },
                "drawdowns": {
                    str(run["id"]): curve_maps[str(run["id"])][timestamp].get("drawdown") for run in runs
                },
            }
        )

    rankings: dict[str, list[dict[str, Any]]] = {}
    for metric in (*_HIGHER_IS_BETTER, *_LOWER_IS_BETTER):
        values = []
        for run in runs:
            result = run["result"]
            value = (result.get("metrics") or {}).get(metric)
            if isinstance(value, (int, float)):
                values.append({"run_id": str(run["id"]), "value": value})
        rankings[metric] = sorted(
            values,
            key=lambda item: item["value"],
            reverse=metric in _HIGHER_IS_BETTER,
        )

    return {
        "strict_comparable": True,
        "dataset_fingerprint": next(iter(fingerprints)),
        "comparison_hash": next(iter(protocols)),
        "runs": run_items,
        "series": series,
        "rankings": rankings,
    }
