"""Arena：同一数据集 × 多 Run（不同 Agent/LLM/配置）的横向比对引擎。

输入：一组 run_id（对应 bt_runs，必须共享同一 events/labels 数据集才比得有意义）
输出：
  - per_run_metrics  ：每个 run 的完整指标集（metrics_registry 结果）
  - ranking           ：每个指标的升/降排名 {metric_id: [{run_id, value, rank}]}
  - radar_chart       ：归一化的雷达图数据（每条 run 对应一个多边形）
  - pairwise_tests    ：两两 run 的配对显著性检验（预测用 exact McNemar；收益用配对置换）
  - win_loss_table    ：两两 run 的胜负统计表
  - shared_events     ：参与 run 之间共有的事件集合（便于统计"同事件上的直接对决"）
"""
from __future__ import annotations

import datetime as dt
import math
import random
import statistics
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional


PERFORMANCE_METRIC_IDS = {
    "strategy_total_return",
    "strategy_max_drawdown",
    "strategy_sharpe_proxy",
    "strategy_win_rate",
}

ARENA_SAFE_MIN_METRIC_SAMPLE = 5


class ArenaMetricValidationError(ValueError):
    """A caller supplied an invalid or unscorable Arena metric contract."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = {"message": message, **details}


def _arena_family(arena_type: str) -> str:
    value = str(arena_type or "").strip().lower()
    if value in {"prediction", "forecast"}:
        return "forecast"
    if value in {"investment", "performance"}:
        return "performance"
    if value in {"mixed", "exploratory"}:
        return "mixed"
    raise ArenaMetricValidationError(
        "未知 Arena 类型；仅支持 forecast/prediction、performance/investment 或 mixed/exploratory",
        arena_type=arena_type,
    )


def _metric_family(metric_id: str) -> str:
    # Keep this explicit: a newly registered metric must be intentionally added
    # to the performance family before it can affect a performance leaderboard.
    return "performance" if metric_id in PERFORMANCE_METRIC_IDS else "forecast"


def _metric_sample_size(metric: dict[str, Any]) -> Optional[int]:
    meta = metric.get("meta") if isinstance(metric.get("meta"), dict) else {}
    for key in ("n", "n_valid", "n_event_trades", "n_total", "total_n"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
    return None


def _arena_safe_metric_sample_size(metric_id: str, metric: dict[str, Any]) -> Optional[int]:
    """Return the effective denominator that actually determines a metric.

    Arena-safe publication must not fall back to a larger, unrelated count.
    For example, a return forecast may have ten predictions but only one
    realised return, while an event strategy may cover ten events but execute
    only one trade.  In both cases the public metric is a one-sample result.
    """
    meta = metric.get("meta") if isinstance(metric.get("meta"), dict) else {}
    if metric_id in PERFORMANCE_METRIC_IDS:
        keys = (
            "n_event_trades", "active_bar_count", "active_period_count",
            "n_active", "trade_count", "n",
        )
    elif metric_id == "return_forecast":
        keys = ("evaluated_count", "n")
    elif metric_id == "direction_bias":
        keys = ("pred_total",)
    elif metric_id in {"coverage_rate", "abstain_rate"}:
        keys = ("n_total", "n")
    else:
        keys = ("n", "n_valid", "total_n")
    for key in keys:
        value = meta.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            return max(0, int(value))
    return None


def _is_valid_metric(metric: dict[str, Any]) -> bool:
    value = metric.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        return False
    sample_size = _metric_sample_size(metric)
    return sample_size is not None and sample_size > 0


def _return_forecast_contract_key(metric: dict[str, Any]) -> tuple | None:
    meta = metric.get("meta") if isinstance(metric.get("meta"), dict) else {}
    if meta.get("unit") != "percentage_points" or meta.get("forecast_unit") != "percent":
        return None
    basis = meta.get("basis")
    if basis == "asset_return":
        horizon = meta.get("horizon")
        from .evaluation_protocol import SUPPORTED_EVALUATION_HORIZONS
        if horizon not in SUPPORTED_EVALUATION_HORIZONS:
            return None
    elif basis == "asset_close_to_close_return":
        horizon = meta.get("horizon_bars")
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
            return None
    else:
        return None
    return basis, horizon, meta["unit"], meta["forecast_unit"]


def _return_forecasts_comparable(ctxs: list["ArenaRunContext"]) -> bool:
    keys = [_return_forecast_contract_key(c.metrics.get("return_forecast") or {}) for c in ctxs]
    return bool(keys) and None not in keys and len(set(keys)) == 1


def resolve_metric_selection(
    ctxs: list["ArenaRunContext"],
    selected_metric_ids: Optional[list[str]],
    *,
    arena_type: str,
) -> list[str]:
    """Resolve defaults and enforce a frozen, scoreable metric-family contract.

    Formal leaderboards only contain metrics that have a finite scalar and a
    positive effective sample for *every* participant. Cross-family metrics are
    reserved for explicitly named mixed/exploratory Arenas.
    """
    from .metrics_registry import list_metric_defs

    all_defs = list_metric_defs()
    family = _arena_family(arena_type)
    if selected_metric_ids is None:
        candidates = [
            mid for mid, definition in all_defs.items()
            if definition.get("tier") in {"core", "extended"}
            and (family == "mixed" or _metric_family(mid) == family)
        ]
        selected = [
            mid for mid in candidates
            if ctxs and all(_is_valid_metric(c.metrics.get(mid) or {}) for c in ctxs)
            and (mid != "return_forecast" or _return_forecasts_comparable(ctxs))
        ]
    else:
        selected = list(dict.fromkeys(str(mid) for mid in selected_metric_ids))
        if not selected:
            raise ArenaMetricValidationError("selected_metric_ids 不能为空")

    unknown = [mid for mid in selected if mid not in all_defs]
    if unknown:
        raise ArenaMetricValidationError(
            "包含未注册指标，无法创建可复现排名",
            unknown_metric_ids=unknown,
        )
    wrong_family = [mid for mid in selected if family != "mixed" and _metric_family(mid) != family]
    if wrong_family:
        raise ArenaMetricValidationError(
            "所选指标与 Arena 赛道不一致；跨预测/收益指标只能使用 mixed 或 exploratory Arena",
            arena_family=family,
            incompatible_metric_ids=wrong_family,
        )
    if not selected:
        raise ArenaMetricValidationError(
            "参赛 Run 没有共同可评分指标，不能生成正式排名",
            arena_family=family,
        )

    invalid: dict[str, list[str]] = {}
    for c in ctxs:
        missing = [mid for mid in selected if not _is_valid_metric(c.metrics.get(mid) or {})]
        if missing:
            invalid[c.run_id] = missing
    if invalid:
        raise ArenaMetricValidationError(
            "每个参赛 Run 的排名指标都必须有有限数值和正样本量",
            invalid_metrics_by_run=invalid,
        )
    if "return_forecast" in selected and not _return_forecasts_comparable(ctxs):
        raise ArenaMetricValidationError(
            "收益率预测误差只能比较相同预测周期、标的收益口径与单位；混合周期汇总不能排名",
            incompatible_metric_ids=["return_forecast"],
        )
    return selected


# ============================================================== 核心 Arena =====================

@dataclass
class ArenaRunContext:
    """每个被比 run 的上下文数据。"""
    run_id: str
    run_info: dict[str, Any]                       # bt_runs 行
    # 关键属性，便于前端直接展示：
    runner: str = ""
    prompt_variant: str = ""
    model_version: str = ""
    status: str = ""
    done_events: int = 0
    total_events: int = 0
    # 成本 / 耗时（从 bt_predictions 聚合）
    tokens_in: int = 0
    tokens_out: int = 0
    step_ms_total: int = 0
    cost_usd: float = 0.0
    # 指标：从 metrics_registry 结果读取
    metrics: dict[str, dict] = field(default_factory=dict)
    # prediction 明细（按需加载，用于 pairwise 同事件直接对决）
    predictions_by_eid: dict[str, dict] = field(default_factory=dict)


def _is_arena_safe_context(ctx: ArenaRunContext) -> bool:
    return (
        str(ctx.run_info.get("visibility") or "")
        .strip()
        .lower()
        .replace("-", "_")
        == "arena_safe"
    )


def _arena_safe_context_metric_sample_size(
    ctx: ArenaRunContext,
    metric_id: str,
    metric: dict[str, Any],
) -> Optional[int]:
    if (
        metric_id in PERFORMANCE_METRIC_IDS
        and str(ctx.run_info.get("engine_mode") or "event_proxy") == "portfolio"
    ):
        metric_meta = metric.get("meta") if isinstance(metric.get("meta"), dict) else {}
        portfolio_metric = (
            ctx.metrics.get("portfolio_metrics")
            if isinstance(ctx.metrics.get("portfolio_metrics"), dict)
            else {}
        )
        portfolio_meta = (
            portfolio_metric.get("meta")
            if isinstance(portfolio_metric.get("meta"), dict)
            else {}
        )
        # A hundred market bars with only one active exposure is still a
        # one-observation strategy outcome.  Never substitute total bar_count.
        for source in (metric_meta, portfolio_meta):
            for key in ("active_bar_count", "active_period_count", "n_active"):
                value = source.get(key)
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                ):
                    return max(0, int(value))
        return None
    return _arena_safe_metric_sample_size(metric_id, metric)


def _public_arena_contexts(ctxs: list[ArenaRunContext]) -> list[ArenaRunContext]:
    """Create the only metric view allowed to feed public Arena aggregates."""
    public: list[ArenaRunContext] = []
    for ctx in ctxs:
        if not _is_arena_safe_context(ctx):
            public.append(ctx)
            continue
        metrics = {
            metric_id: metric
            for metric_id, metric in ctx.metrics.items()
            if isinstance(metric, dict)
            and (_arena_safe_context_metric_sample_size(ctx, metric_id, metric) or 0)
            >= ARENA_SAFE_MIN_METRIC_SAMPLE
        }
        public.append(replace(ctx, metrics=metrics))
    return public


def _run_event_selection(run_info: dict):
    from .application import load_events
    from .evaluation_protocol import EventSelection, resolve_run_execution_spec, select_events_for_execution_window

    if str(run_info.get("engine_mode") or "event_proxy") == "portfolio":
        return EventSelection(
            events=[], event_ids=set(), active=False, start_date=None, end_date=None,
            before_count=0, after_count=0, missing_time_count=0,
        )

    events_path = Path(str(run_info.get("events_path") or ""))
    events = load_events(events_path) if events_path.is_file() else []
    return select_events_for_execution_window(events, resolve_run_execution_spec(run_info))


def _load_run_predictions(
    run_info: dict,
    *,
    allowed_event_ids: Optional[set[str]] = None,
) -> dict[str, dict]:
    """从 run 的 out_path 读取 predictions JSONL，返回 {event_id: pred_dict}。"""
    out_path = run_info.get("out_path") or ""
    if not out_path or not Path(out_path).is_file():
        return {}
    preds: dict[str, dict] = {}
    try:
        from .application import load_predictions
        for p in load_predictions(out_path):
            eid = getattr(p, "event_id", None)
            if not eid:
                continue
            if allowed_event_ids is not None and str(eid) not in allowed_event_ids:
                continue
            preds[eid] = {
                "pred_direction": getattr(p, "pred_direction", None),
                "confidence": getattr(p, "confidence", None),
                "abstain": bool(getattr(p, "abstain", False)),
                "rationale": getattr(p, "rationale", None),
            }
    except Exception:
        pass
    return preds


def _compute_run_metrics(run_info: dict) -> dict[str, dict]:
    """计算或从 DB 读取一个 run 的完整 metrics_registry 结果。
    优先走 DB.metrics_json；没有则现场计算并回写。"""
    # Cached metrics created by an older server may have silently used T3 and
    # zero friction. Recompute whenever frozen predictions and labels are
    # available; only fall back to the cache when source files are absent.
    cached = run_info.get("metrics") if isinstance(run_info.get("metrics"), dict) else {}
    if str(run_info.get("engine_mode") or "event_proxy") == "portfolio":
        strategy = run_info.get("strategy_spec") if isinstance(run_info.get("strategy_spec"), dict) else {}
        portfolio_meta = (cached.get("portfolio_metrics") or {}).get("meta") or {}
        if strategy.get("kind") == "return_forecast" or portfolio_meta.get("prediction_only"):
            cached = deepcopy(cached)
            for mid in ("strategy_total_return", "strategy_max_drawdown", "strategy_sharpe_proxy", "strategy_win_rate", "portfolio_metrics"):
                if isinstance(cached.get(mid), dict):
                    cached[mid]["value"] = None
        return cached
    # 现场计算：load preds + labels
    from pathlib import Path
    out_path = run_info.get("out_path") or ""
    labels_path = run_info.get("labels_path") or None
    preds_list = []
    labels_list = []
    try:
        from .application import load_predictions, load_labels
        if out_path and Path(out_path).is_file():
            preds_list = load_predictions(out_path)
        if labels_path and Path(labels_path).is_file():
            labels_list = load_labels(labels_path)
    except Exception:
        preds_list, labels_list = [], []
    if not preds_list:
        return cached
    from .evaluation_protocol import chronological_event_ids, resolve_run_execution_spec, resolve_run_horizon, resolve_run_oracle_epsilon
    from .metrics_registry import compute_all_metrics
    selection = _run_event_selection(run_info)
    results = compute_all_metrics(
        predictions=preds_list,
        labels=labels_list,
        epsilon=resolve_run_oracle_epsilon(run_info),
        primary_oracle_horizon=resolve_run_horizon(run_info),
        execution_spec=resolve_run_execution_spec(run_info),
        allowed_event_ids=selection.event_ids if selection.active else None,
        event_order=chronological_event_ids(selection.events),
    )
    computed = {mid: mr.to_dict() for mid, mr in results.items()}
    merged = {**cached, **computed}
    run_id = str(run_info.get("id") or "")
    if run_id:
        try:
            from .. import db as _db
            _db.update_bt_run_metrics(run_id, merged)
        except Exception as exc:
            print(f"[WARN] Arena metric cache update failed for run={run_id}: {exc}", flush=True)
    return merged


def _aggregate_run_cost(run_id: str) -> dict:
    """Aggregate explicit cost columns migrated by db.init_db()."""
    from .. import db as _db
    return _db.aggregate_bt_prediction_cost(run_id)


def build_run_contexts(run_infos: list[dict]) -> list[ArenaRunContext]:
    """把一组 bt_runs 行转成 ArenaRunContext（带指标、predictions、tokens/step_ms）。"""
    ctxs: list[ArenaRunContext] = []
    for r in run_infos:
        rid = str(r.get("id") or "")
        metrics = _compute_run_metrics(r)
        selection = _run_event_selection(r)
        preds = _load_run_predictions(
            r,
            allowed_event_ids=selection.event_ids if selection.active else None,
        )
        cost = _aggregate_run_cost(rid)
        ctxs.append(ArenaRunContext(
            run_id=rid,
            run_info=r,
            runner=str(r.get("runner") or ""),
            prompt_variant=str(r.get("prompt_variant") or ""),
            model_version=str(r.get("model_version") or ""),
            status=str(r.get("status") or ""),
            done_events=int(r.get("done_events") or 0),
            total_events=int(r.get("total_events") or 0),
            tokens_in=cost["tokens_in"],
            tokens_out=cost["tokens_out"],
            step_ms_total=cost["step_ms_total"],
            cost_usd=float(cost.get("cost_usd") or 0.0),
            metrics=metrics,
            predictions_by_eid=preds,
        ))
    return ctxs


# ============================================================== 聚合：排名 / 雷达 / 配对 ======

def _rank_metric(ctxs: list[ArenaRunContext], metric_id: str,
                 metric_def: Optional[dict] = None) -> list[dict]:
    """对单个指标的所有 run 排序并打 rank。返回 [{run_id, value, rank, display_name}]。"""
    higher = True
    display = metric_id
    if metric_def:
        higher = bool(metric_def.get("higher_is_better", True))
        display = metric_def.get("display_name") or metric_id
    rows = []
    for c in ctxs:
        mr = c.metrics.get(metric_id) or {}
        val = mr.get("value")
        rows.append({"run_id": c.run_id, "value": val, "display_name": display})
    # 排序：None 放最后
    def _sort_key(r):
        v = r["value"]
        if v is None:
            return (1, 0.0)
        return (0, -float(v) if higher else float(v))
    rows.sort(key=_sort_key)
    # 排名（同分同 rank：dense）
    last_v = None
    last_rank = 0
    for i, r in enumerate(rows):
        v = r["value"]
        if v is None:
            r["rank"] = None
            continue
        if last_v is None or v != last_v:
            last_rank = i + 1
            last_v = v
        r["rank"] = last_rank
    return rows


def _normalize_for_radar(
    ctxs: list[ArenaRunContext],
    metric_ids: list[str],
    metric_defs: dict[str, dict],
) -> dict[str, Any]:
    """为雷达图归一化指标到 [0, 1]：
      - higher_is_better=True : (v - min) / (max - min)
      - higher_is_better=False: 1 - (v - min)/(max - min)
      - 若 max==min：都给 0.5
    返回 {
      axes: [{metric_id, display_name}],
      series: [{run_id, label, values: [0..1 对应 axes 顺序]}]
    }
    """
    axes = []
    for mid in metric_ids:
        md = metric_defs.get(mid, {})
        axes.append({"metric_id": mid, "display_name": md.get("display_name") or mid})

    # 收集每个指标的 [value, None 跳过]
    per_metric_vals: dict[str, list[float]] = {mid: [] for mid in metric_ids}
    per_run_vals: dict[str, list[Optional[float]]] = {c.run_id: [] for c in ctxs}
    for mid in metric_ids:
        for c in ctxs:
            v = (c.metrics.get(mid) or {}).get("value")
            per_run_vals[c.run_id].append(v)
            if v is not None:
                per_metric_vals[mid].append(float(v))

    # 求 min/max
    bounds: dict[str, tuple[float, float]] = {}
    for mid in metric_ids:
        vals = per_metric_vals[mid]
        if not vals:
            bounds[mid] = (0.0, 1.0)
        else:
            lo, hi = min(vals), max(vals)
            if abs(hi - lo) < 1e-9:
                # 单值：取 [lo - 10%, lo + 10%]（至少非零区间）以便非 0.5
                if abs(lo) < 1e-9:
                    lo, hi = 0.0, 1.0
                else:
                    eps = abs(lo) * 0.1
                    lo -= eps
                    hi += eps
            bounds[mid] = (lo, hi)

    series = []
    for c in ctxs:
        label = _run_display_name(c)
        norm = []
        for i, mid in enumerate(metric_ids):
            v = per_run_vals[c.run_id][i]
            if v is None:
                norm.append(None)
                continue
            lo, hi = bounds[mid]
            frac = (float(v) - lo) / (hi - lo)
            frac = max(0.0, min(1.0, frac))
            md = metric_defs.get(mid, {})
            if not md.get("higher_is_better", True):
                frac = 1.0 - frac
            norm.append(float(f"{frac:.4f}"))
        series.append({"run_id": c.run_id, "label": label, "values": norm})
    return {"axes": axes, "series": series}


def _run_display_name(ctx: ArenaRunContext) -> str:
    if _is_arena_safe_context(ctx):
        return f"Arena Run · {ctx.run_id[:8]}"
    base = str(ctx.run_info.get("name") or "").strip()
    if not base:
        base = str(ctx.model_version or ctx.runner or "Run").strip()
        if ctx.prompt_variant:
            base += f" · {ctx.prompt_variant}"
    return f"{base} · {ctx.run_id[:8]}"


def _labels_by_event(labels_list: Optional[list]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label in labels_list or []:
        event_id = label.get("event_id") if isinstance(label, dict) else getattr(label, "event_id", None)
        if event_id:
            out[str(event_id)] = label
    return out


def _label_field(label: Any, field_name: str) -> Any:
    return label.get(field_name) if isinstance(label, dict) else getattr(label, field_name, None)


def _mcnemar_exact(b_only: int, c_only: int, paired_n: int) -> dict[str, Any]:
    discordant = b_only + c_only
    if paired_n <= 0:
        return {"ok": False, "reason": "no_common_scored_events", "n_paired": 0}
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(math.comb(discordant, k) for k in range(min(b_only, c_only) + 1)) / (2 ** discordant)
        p_value = min(1.0, 2.0 * tail)
    return {
        "ok": True,
        "p_value": float(f"{p_value:.6f}"),
        "n_paired": paired_n,
        "b_only_a_correct": b_only,
        "c_only_b_correct": c_only,
        "discordant": discordant,
        "paired_accuracy_diff": float(f"{(b_only - c_only) / paired_n:.6f}"),
    }


def _forecast_paired_test(
    ci: ArenaRunContext,
    cj: ArenaRunContext,
    metric_id: str,
    label_by_eid: dict[str, Any],
) -> dict[str, Any]:
    meta = ci.metrics.get(metric_id, {}).get("meta") or {}
    horizon = str(meta.get("horizon") or meta.get("primary_horizon") or "")
    if not horizon and metric_id.startswith("acc_") and metric_id.endswith("_strict"):
        horizon = metric_id[len("acc_"):-len("_strict")]
    horizon = horizon or "t3"
    require_non_neutral = metric_id in {
        "acc_primary_non_neutral",
        "acc_primary_significant_only",
        "acc_primary_directional_trade",
    }
    directional_trade = metric_id == "acc_primary_directional_trade"
    three_class = metric_id == "acc_primary_three_class"
    b_only = c_only = paired_n = 0
    shared = set(ci.predictions_by_eid) & set(cj.predictions_by_eid) & set(label_by_eid)
    for event_id in shared:
        label = label_by_eid[event_id]
        oracle = _label_field(label, f"label_{horizon}")
        if not oracle or (require_non_neutral and oracle not in {"up", "down"}):
            continue
        if metric_id == "acc_primary_significant_only":
            p_value = _label_field(label, f"car_{horizon}_pvalue")
            if not isinstance(p_value, (int, float)) or float(p_value) >= 0.10:
                continue
        pi = ci.predictions_by_eid[event_id]
        pj = cj.predictions_by_eid[event_id]
        if bool(pi.get("abstain")) or bool(pj.get("abstain")):
            continue
        if directional_trade and (
            pi.get("pred_direction") not in {"up", "down"}
            or pj.get("pred_direction") not in {"up", "down"}
        ):
            # The paired population must be the intersection of the two Runs'
            # actual directional decisions, matching the metric denominator.
            continue
        if three_class:
            i_correct = pi.get("pred_direction") == oracle
            j_correct = pj.get("pred_direction") == oracle
        else:
            # Legacy strict metrics intentionally count a neutral Oracle as an
            # incorrect scored event.  The explicit three-class metric above
            # is the view in which neutral==neutral is a correct prediction.
            i_correct = oracle != "neutral" and pi.get("pred_direction") == oracle
            j_correct = oracle != "neutral" and pj.get("pred_direction") == oracle
        paired_n += 1
        if i_correct and not j_correct:
            b_only += 1
        elif j_correct and not i_correct:
            c_only += 1
    return _mcnemar_exact(b_only, c_only, paired_n)


def _event_return(prediction: dict[str, Any], car: Any) -> Optional[float]:
    if not isinstance(car, (int, float)) or isinstance(car, bool) or not math.isfinite(float(car)):
        return None
    direction = str(prediction.get("pred_direction") or "")
    if prediction.get("abstain") or direction == "neutral":
        return 0.0
    if direction == "up":
        return max(-0.999999, float(car))
    if direction == "down":
        return max(-0.999999, -float(car))
    return None


def _percentile(values: list[float], probability: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    pos = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _performance_paired_test(
    ci: ArenaRunContext,
    cj: ArenaRunContext,
    label_by_eid: dict[str, Any],
    *,
    primary_horizon: str = "t3",
) -> dict[str, Any]:
    differences: list[float] = []
    shared = sorted(set(ci.predictions_by_eid) & set(cj.predictions_by_eid) & set(label_by_eid))
    for event_id in shared:
        label = label_by_eid[event_id]
        car = _label_field(label, f"car_{primary_horizon}")
        ri = _event_return(ci.predictions_by_eid[event_id], car)
        rj = _event_return(cj.predictions_by_eid[event_id], car)
        if ri is not None and rj is not None:
            differences.append(ri - rj)
    n = len(differences)
    if n <= 0:
        return {
            "ok": False,
            "reason": "paired_event_returns_unavailable",
            "n_paired": 0,
            "estimand": "mean_paired_event_return",
        }

    observed = sum(differences) / n
    # Deterministic sign-flip permutation: exact for small n, seeded Monte Carlo
    # for larger samples. A paired bootstrap supplies a directly interpretable CI.
    if n <= 16:
        permutation_means = []
        for mask in range(1 << n):
            total = sum(value if mask & (1 << i) else -value for i, value in enumerate(differences))
            permutation_means.append(total / n)
        extreme = sum(1 for value in permutation_means if abs(value) >= abs(observed) - 1e-15)
        p_value = extreme / len(permutation_means)
        permutations = len(permutation_means)
        exact = True
    else:
        rng = random.Random(20260906)
        permutations = 10_000
        extreme = 0
        for _ in range(permutations):
            value = sum(item if rng.random() < 0.5 else -item for item in differences) / n
            if abs(value) >= abs(observed) - 1e-15:
                extreme += 1
        p_value = (extreme + 1) / (permutations + 1)
        exact = False

    rng = random.Random(20260907)
    bootstrap_means = [
        sum(differences[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(2_000)
    ]
    return {
        "ok": True,
        "p_value": float(f"{p_value:.6f}"),
        "n_paired": n,
        "mean_paired_return_diff": float(f"{observed:.8f}"),
        "ci95_lo": _percentile(bootstrap_means, 0.025),
        "ci95_hi": _percentile(bootstrap_means, 0.975),
        "permutations": permutations,
        "exact": exact,
        "estimand": "mean_paired_event_return",
    }


def _pairwise_compare(
    ctxs: list[ArenaRunContext],
    metric_ids_for_test: list[str],
    metric_defs: dict[str, dict],
    *,
    labels_list: Optional[list] = None,
    arena_family: str = "forecast",
    protected_run_ids: Optional[set[str]] = None,
    primary_horizon: str = "t3",
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return paired-event evidence; never use independent-sample z tests."""
    label_by_eid = _labels_by_event(labels_list)
    protected_run_ids = protected_run_ids or set()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for i in range(len(ctxs)):
        for j in range(i + 1, len(ctxs)):
            ci, cj = ctxs[i], ctxs[j]
            per_metric = {}
            reverse_per_metric = {}
            for mid in metric_ids_for_test:
                mi = ci.metrics.get(mid) or {}
                mj = cj.metrics.get(mid) or {}
                if (
                    ci.run_id in protected_run_ids
                    and (_arena_safe_metric_sample_size(mid, mi) or 0)
                    < ARENA_SAFE_MIN_METRIC_SAMPLE
                ) or (
                    cj.run_id in protected_run_ids
                    and (_arena_safe_metric_sample_size(mid, mj) or 0)
                    < ARENA_SAFE_MIN_METRIC_SAMPLE
                ):
                    # The metric itself is not publishable, so even an
                    # unavailable/delta-free pairwise shell would contradict
                    # the "metric omitted" contract.
                    continue
                md = metric_defs.get(mid, {})
                vi, vj = mi.get("value"), mj.get("value")
                delta = (float(vi) - float(vj)) if isinstance(vi, (int, float)) and isinstance(vj, (int, float)) else None
                higher = bool(md.get("higher_is_better", True))
                winner: str | None = None
                if delta is not None:
                    advantage = delta if higher else -delta
                    winner = ci.run_id if advantage > 0 else (cj.run_id if advantage < 0 else "tie")
                metric_family = _metric_family(mid) if arena_family == "mixed" else arena_family
                test_res = (
                    _performance_paired_test(
                        ci, cj, label_by_eid, primary_horizon=primary_horizon,
                    )
                    if metric_family == "performance"
                    else _forecast_paired_test(ci, cj, mid, label_by_eid)
                )
                shared = len(set(ci.predictions_by_eid) & set(cj.predictions_by_eid))
                paired_sample = test_res.get("n_paired")
                privacy_suppressed = (
                    (not isinstance(paired_sample, (int, float)) or paired_sample < 5)
                    and bool({ci.run_id, cj.run_id} & protected_run_ids)
                )
                if privacy_suppressed:
                    test_res = {"ok": False, "reason": "arena_safe_small_sample", "n_paired": None}
                test_name = (
                    "paired_permutation_bootstrap"
                    if metric_family == "performance" and test_res.get("ok")
                    else "mcnemar_exact_binomial"
                    if metric_family != "performance" and test_res.get("ok")
                    else "unavailable"
                )
                per_metric[mid] = {
                    "display_name": md.get("display_name") or mid,
                    "p_value": test_res.get("p_value") if test_res.get("ok") else None,
                    "delta": None if privacy_suppressed else delta,
                    "winner": None if privacy_suppressed else winner,
                    "test_name": test_name,
                    "test_meta": test_res,
                }
                reverse_meta = deepcopy(test_res)
                if reverse_meta.get("ok"):
                    if "b_only_a_correct" in reverse_meta:
                        reverse_meta["b_only_a_correct"], reverse_meta["c_only_b_correct"] = (
                            reverse_meta["c_only_b_correct"], reverse_meta["b_only_a_correct"]
                        )
                        reverse_meta["paired_accuracy_diff"] = -float(reverse_meta["paired_accuracy_diff"])
                    if "mean_paired_return_diff" in reverse_meta:
                        reverse_meta["mean_paired_return_diff"] = -float(reverse_meta["mean_paired_return_diff"])
                        lo, hi = reverse_meta.get("ci95_lo"), reverse_meta.get("ci95_hi")
                        reverse_meta["ci95_lo"] = -float(hi) if isinstance(hi, (int, float)) else None
                        reverse_meta["ci95_hi"] = -float(lo) if isinstance(lo, (int, float)) else None
                reverse_per_metric[mid] = {
                    "display_name": md.get("display_name") or mid,
                    "p_value": reverse_meta.get("p_value") if reverse_meta.get("ok") else None,
                    "delta": (-delta if delta is not None else None) if not privacy_suppressed else None,
                    "winner": winner if not privacy_suppressed else None,
                    "test_name": test_name,
                    "test_meta": reverse_meta,
                }
            shared = len(set(ci.predictions_by_eid) & set(cj.predictions_by_eid))
            out.setdefault(ci.run_id, {})[cj.run_id] = {
                "shared_events": shared,
                "per_metric": per_metric,
            }
            out.setdefault(cj.run_id, {})[ci.run_id] = {
                "shared_events": shared,
                "per_metric": reverse_per_metric,
            }
    return out


def _shared_event_head_to_head(
    ctxs: list[ArenaRunContext],
    labels_list: Optional[list] = None,
    primary_horizon: str = "t3",
) -> dict[str, Any]:
    """同事件直接对决：在参与 run 共同覆盖的事件子集上，两两 run 统计谁更准。

    labels_list 可选：如果提供，还能计算"谁与 oracle 更一致"的胜场。
    Shape matches the frontend ``head_to_head`` contract.
    """
    # 1) 所有 run 都预测了的 eid 交集
    eid_sets = [set(c.predictions_by_eid.keys()) for c in ctxs if c.predictions_by_eid]
    if not eid_sets:
        return {"summary": {}, "event_level": []}
    shared: set[str] = set.intersection(*eid_sets) if len(eid_sets) > 1 else eid_sets[0]
    shared_count = len(shared)

    # 2) 两两 win/loss/tie（按 primary oracle，若 labels_list 提供）
    label_by_eid = {}
    if labels_list:
        for label in labels_list:
            if isinstance(label, dict):
                eid, oracle = label.get("event_id"), label.get(f"label_{primary_horizon}")
            else:
                eid = getattr(label, "event_id", None)
                oracle = getattr(label, f"label_{primary_horizon}", None)
            if eid:
                label_by_eid[str(eid)] = oracle

    summary: dict[str, dict[str, Any]] = {}
    for i in range(len(ctxs)):
        for j in range(i + 1, len(ctxs)):
            ci, cj = ctxs[i], ctxs[j]
            iw = jw = ties = scored = 0
            for eid in shared:
                pi = ci.predictions_by_eid.get(eid) or {}
                pj = cj.predictions_by_eid.get(eid) or {}
                di, dj = pi.get("pred_direction"), pj.get("pred_direction")
                oracle = label_by_eid.get(eid)
                if not oracle:
                    # No Oracle means no scored paired outcome, not a tie.
                    continue
                scored += 1
                ci_correct = (
                    oracle in {"up", "down"}
                    and not bool(pi.get("abstain"))
                    and di in {"up", "down"}
                    and di == oracle
                )
                cj_correct = (
                    oracle in {"up", "down"}
                    and not bool(pj.get("abstain"))
                    and dj in {"up", "down"}
                    and dj == oracle
                )
                if ci_correct and not cj_correct:
                    iw += 1
                elif cj_correct and not ci_correct:
                    jw += 1
                else:
                    ties += 1
            key = f"{ci.run_id} vs {cj.run_id}"
            exact = _mcnemar_exact(iw, jw, scored)
            summary[key] = {
                "a_wins": iw,
                "b_wins": jw,
                "ties": ties,
                "shared": shared_count,
                "scored": scored,
                "test_name": "mcnemar_exact_binomial" if exact.get("ok") else "unavailable",
                "p_value": exact.get("p_value") if exact.get("ok") else None,
            }

    event_level = []
    for eid in sorted(shared):
        oracle = label_by_eid.get(eid)
        per_run: dict[str, dict[str, Any]] = {}
        best_runs: list[str] = []
        for c in ctxs:
            p = c.predictions_by_eid.get(eid) or {}
            prediction = p.get("pred_direction")
            correct = (
                not bool(p.get("abstain"))
                and prediction in {"up", "down"}
                and oracle in {"up", "down"}
                and prediction == oracle
            ) if oracle in {"up", "down", "neutral"} else None
            per_run[c.run_id] = {
                "prediction": prediction,
                "confidence": p.get("confidence"),
                "abstain": bool(p.get("abstain")),
                "correct": correct,
            }
            if correct is True:
                best_runs.append(c.run_id)
        event_level.append({
            "event_id": eid,
            "oracle": oracle,
            "per_run": per_run,
            "best_runs": best_runs,
        })
    return {"summary": summary, "event_level": event_level}


def _privacy_bucket_curve_points(
    points: list[dict[str, Any]],
    *,
    min_bucket_size: int = 5,
) -> list[dict[str, Any]]:
    """Reduce an exact path to boundaries of privacy-sized observation buckets."""
    bucket_size = max(2, int(min_bucket_size))
    observation_count = max(0, len(points) - 1)
    if observation_count <= 0:
        return points
    if observation_count < bucket_size:
        return [points[0], points[-1]]
    boundaries = list(range(bucket_size, observation_count + 1, bucket_size))
    if not boundaries:
        boundaries = [observation_count]
    elif boundaries[-1] != observation_count:
        # Merge the short remainder into the preceding bucket.
        boundaries[-1] = observation_count
    return [points[0], *(points[index] for index in boundaries)]


_ALIGNMENT_MODES = {"native", "intersection", "union", "manual"}
_ALIGNMENT_FREQUENCIES = {"native", "day", "week", "month"}


def normalize_time_alignment_config(value: Any) -> dict[str, Any] | None:
    """Validate and canonicalise the optional Arena time-alignment contract.

    ``None`` deliberately means legacy behaviour.  Saved Arenas created before
    this contract was introduced must keep their historical observation-index
    curves instead of being silently reinterpreted under a new date window.
    """
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ArenaMetricValidationError("time_alignment 必须是对象")
    mode_aliases = {
        "common": "intersection", "overlap": "intersection",
        "full": "union", "complete": "union",
    }
    frequency_aliases = {
        "original": "native", "raw": "native",
        "daily": "day", "1d": "day",
        "weekly": "week", "1w": "week",
        "monthly": "month", "1mo": "month",
    }
    mode = mode_aliases.get(str(value.get("mode") or "intersection").strip().lower(), str(value.get("mode") or "intersection").strip().lower())
    frequency = frequency_aliases.get(
        str(value.get("frequency") or "native").strip().lower(),
        str(value.get("frequency") or "native").strip().lower(),
    )
    if mode not in _ALIGNMENT_MODES:
        raise ArenaMetricValidationError(
            "未知时间对齐模式；仅支持 native/intersection/union/manual",
            time_alignment_mode=mode,
        )
    if frequency not in _ALIGNMENT_FREQUENCIES:
        raise ArenaMetricValidationError(
            "未知展示频率；仅支持 native/day/week/month",
            time_alignment_frequency=frequency,
        )
    start_date = str(value.get("start_date") or "").strip() or None
    end_date = str(value.get("end_date") or "").strip() or None
    if mode == "manual" and (not start_date or not end_date):
        raise ArenaMetricValidationError(
            "手动时间对齐必须同时提供 start_date 和 end_date",
            time_alignment_mode=mode,
        )
    start_bound = _parse_alignment_timestamp(start_date, end_of_day=False) if start_date else None
    end_bound = _parse_alignment_timestamp(end_date, end_of_day=True) if end_date else None
    if start_date and start_bound is None:
        raise ArenaMetricValidationError("start_date 不是有效日期", start_date=start_date)
    if end_date and end_bound is None:
        raise ArenaMetricValidationError("end_date 不是有效日期", end_date=end_date)
    if start_bound and end_bound and start_bound > end_bound:
        raise ArenaMetricValidationError(
            "时间对齐的开始日期不能晚于结束日期",
            start_date=start_date,
            end_date=end_date,
        )
    min_observations_raw = value.get("min_observations", 20)
    try:
        min_observations = int(min_observations_raw)
    except (TypeError, ValueError) as exc:
        raise ArenaMetricValidationError("min_observations 必须是整数") from exc
    if not 2 <= min_observations <= 10_000:
        raise ArenaMetricValidationError("min_observations 必须在 2 到 10000 之间")
    return {
        "version": "pronoia-arena-time-alignment-v1",
        "mode": mode,
        "frequency": frequency,
        "start_date": start_date,
        "end_date": end_date,
        "min_observations": min_observations,
    }


def _parse_alignment_timestamp(value: Any, *, end_of_day: bool = False) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed_date = dt.date.fromisoformat(normalized[:10].replace("/", "-"))
        except ValueError:
            return None
        parsed = dt.datetime.combine(
            parsed_date,
            dt.time.max if end_of_day else dt.time.min,
        )
    if len(raw) <= 10 and end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _iso_alignment_timestamp(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    utc_value = value.astimezone(dt.timezone.utc)
    if utc_value.time() in {dt.time.min, dt.time.max}:
        return utc_value.date().isoformat()
    return utc_value.isoformat().replace("+00:00", "Z")


def _frequency_seconds(value: Any) -> float | None:
    raw = str(value or "").strip().lower().replace("minute", "m").replace(" ", "")
    aliases = {
        "d": "1d", "day": "1d", "daily": "1d",
        "w": "1w", "week": "1w", "weekly": "1w",
        "1mo": "month", "monthly": "month",
    }
    raw = aliases.get(raw, raw)
    if raw.endswith("m") and raw[:-1].isdigit():
        return max(1, int(raw[:-1])) * 60.0
    if raw.endswith("h") and raw[:-1].isdigit():
        return max(1, int(raw[:-1])) * 3_600.0
    if raw == "1d":
        return 86_400.0
    if raw == "1w":
        return 7 * 86_400.0
    if raw in {"month", "1month"}:
        return 30.4375 * 86_400.0
    return None


def _infer_curve_frequency(
    points: list[dict[str, Any]],
    declared_frequency: Any,
) -> tuple[str, float | None]:
    declared_seconds = _frequency_seconds(declared_frequency)
    if declared_seconds is not None:
        return str(declared_frequency or "native"), declared_seconds
    stamps = [point["_time"] for point in points if isinstance(point.get("_time"), dt.datetime)]
    deltas = [
        (right - left).total_seconds()
        for left, right in zip(stamps, stamps[1:])
        if right > left
    ]
    if not deltas:
        return "unknown", None
    median = statistics.median(deltas)
    if median < 86_400:
        minutes = max(1, round(median / 60))
        return f"~{minutes}m", median
    if median <= 3 * 86_400:
        return "~1d", median
    if median <= 14 * 86_400:
        return "~1w", median
    return "~1mo", median


def _target_frequency_seconds(frequency: str) -> float | None:
    return {
        "day": 86_400.0,
        "week": 7 * 86_400.0,
        "month": 30.4375 * 86_400.0,
    }.get(frequency)


def _resample_curve_points(
    points: list[dict[str, Any]],
    *,
    requested_frequency: str,
    source_seconds: float | None,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Keep real closing observations only; never manufacture or forward-fill."""
    if requested_frequency == "native" or len(points) < 2:
        return list(points), "native", []
    target_seconds = _target_frequency_seconds(requested_frequency)
    if source_seconds is not None and target_seconds is not None and target_seconds < source_seconds * 0.75:
        return (
            list(points),
            "native",
            [f"requested_{requested_frequency}_is_finer_than_source; kept_native_without_upsampling"],
        )

    buckets: dict[tuple[int, ...], dict[str, Any]] = {}
    for point in points:
        timestamp = point.get("_time")
        if not isinstance(timestamp, dt.datetime):
            continue
        if requested_frequency == "day":
            key = (timestamp.year, timestamp.month, timestamp.day)
        elif requested_frequency == "week":
            iso_year, iso_week, _ = timestamp.isocalendar()
            key = (iso_year, iso_week)
        else:
            key = (timestamp.year, timestamp.month)
        # Last genuine observation in each bucket is the close of that bucket.
        buckets[key] = point
    sampled = sorted(buckets.values(), key=lambda item: item["_time"])
    # Preserve the first genuine point as the evaluation baseline.  A plain
    # period-close resample would otherwise discard the first partial bucket
    # and silently omit its return from aligned total/annualised performance.
    # This is not an interpolated point: it is the original first observation.
    if sampled and points and sampled[0]["_time"] != points[0]["_time"]:
        sampled.insert(0, points[0])
    return sampled, requested_frequency, []


def _aligned_curve_metrics(
    points: list[dict[str, Any]],
    *,
    effective_frequency: str,
    min_observations: int,
) -> dict[str, Any]:
    values = [float(point["net_value"]) for point in points]
    times = [point["_time"] for point in points]
    sample_sufficient = len(values) >= min_observations
    if len(values) < 2 or values[0] <= 0:
        return {
            "status": "insufficient" if values else "unavailable",
            "sample_sufficient": False,
            "min_observations": min_observations,
            "n_observations": len(values),
            "start_at": _iso_alignment_timestamp(times[0]) if times else None,
            "end_at": _iso_alignment_timestamp(times[-1]) if times else None,
            "total_return": None,
            "annualized_return": None,
            "max_drawdown": None,
            "sharpe_ratio": None,
            "annualized_volatility": None,
            "calmar_ratio": None,
        }
    rebased = [value / values[0] for value in values]
    period_returns = [right / left - 1.0 for left, right in zip(values, values[1:]) if left > 0]
    total_return = rebased[-1] - 1.0
    peak = rebased[0]
    worst_drawdown = 0.0
    for value in rebased:
        peak = max(peak, value)
        if peak > 0:
            worst_drawdown = min(worst_drawdown, value / peak - 1.0)
    duration_days = max(0.0, (times[-1] - times[0]).total_seconds() / 86_400.0)
    annualized_return: float | None = None
    if duration_days > 0 and rebased[-1] > 0:
        try:
            annualized_return = rebased[-1] ** (365.2425 / duration_days) - 1.0
        except (OverflowError, ValueError):
            annualized_return = None
    periods_per_year = {
        "day": 252.0, "week": 52.0, "month": 12.0,
    }.get(effective_frequency)
    if periods_per_year is None and len(times) >= 2 and duration_days > 0:
        periods_per_year = max(1.0, (len(times) - 1) * 365.2425 / duration_days)
    volatility: float | None = None
    sharpe: float | None = None
    if len(period_returns) >= 2 and periods_per_year:
        deviation = statistics.stdev(period_returns)
        volatility = deviation * math.sqrt(periods_per_year)
        if deviation > 0:
            sharpe = statistics.mean(period_returns) / deviation * math.sqrt(periods_per_year)
    drawdown = abs(worst_drawdown)
    calmar = annualized_return / drawdown if annualized_return is not None and drawdown > 0 else None

    def finite_or_none(number: float | None) -> float | None:
        return float(number) if isinstance(number, (int, float)) and math.isfinite(float(number)) else None

    return {
        "status": "available" if sample_sufficient else "insufficient",
        "sample_sufficient": sample_sufficient,
        "min_observations": min_observations,
        "n_observations": len(values),
        "start_at": _iso_alignment_timestamp(times[0]),
        "end_at": _iso_alignment_timestamp(times[-1]),
        "duration_days": float(f"{duration_days:.6f}"),
        "total_return": finite_or_none(total_return),
        "annualized_return": finite_or_none(annualized_return),
        "max_drawdown": finite_or_none(drawdown),
        "sharpe_ratio": finite_or_none(sharpe),
        "annualized_volatility": finite_or_none(volatility),
        "calmar_ratio": finite_or_none(calmar),
        "annualization_basis": "actual_elapsed_calendar_time",
    }


def _build_performance_curves_legacy(
    ctxs: list[ArenaRunContext],
    *,
    run_protocol_hashes: dict[str, str],
) -> dict[str, Any]:
    """Build a privacy-safe, run-level equity overlay for performance Arenas.

    The detailed performance endpoint contains event identifiers, timestamps,
    individual returns and trades. Arena always removes identifiers, individual
    returns and trades. Private exploratory curves may retain timestamps so
    different windows can be shown honestly; ``arena_safe`` curves remain
    bucketed observation-index paths without dates.
    """
    unique_protocols = sorted(set(run_protocol_hashes.values()))
    protocol_comparable = (
        bool(ctxs) and len(unique_protocols) == 1
        and all(ctx.run_info.get("_arena_formal_eligible") is not False for ctx in ctxs)
    )
    allow_mixed = bool(ctxs) and all(
        bool(ctx.run_info.get("_arena_allow_mixed_protocols")) for ctx in ctxs
    )
    safe_run_ids = {
        ctx.run_id
        for ctx in ctxs
        if str(ctx.run_info.get("visibility") or "").strip().lower().replace("-", "_")
        == "arena_safe"
    }

    def unavailable_series(ctx: ArenaRunContext, reason: str) -> dict[str, Any]:
        display_name = _run_display_name(ctx)
        return {
            "run_id": ctx.run_id,
            "name": display_name,
            "display_name": display_name,
            "status": "unavailable",
            "reason": reason,
            "aggregate_only": True,
            "privacy_aggregated": ctx.run_id in safe_run_ids,
            "points": [],
        }

    if not protocol_comparable and not allow_mixed:
        return {
            "status": "unavailable",
            "reason": "protocol_mismatch",
            "strict_comparable": False,
            "axis": {
                "type": "event_observation_index",
                "label": "事件收益观察序号",
            },
            "series": [unavailable_series(ctx, "protocol_mismatch") for ctx in ctxs],
            "privacy": {
                "aggregation_level": "run_equity_curve",
                "arena_safe_run_ids": sorted(safe_run_ids),
                "downsampled_run_ids": sorted(safe_run_ids),
                "min_bucket_size": 5,
                "excluded_fields": ["event_id", "period_return", "trade"],
                "arena_safe_excluded_fields": ["timestamp"],
                "timestamp_policy": "unavailable_for_protocol_mismatch",
            },
            "disclaimer": "协议不同的 Run 不绘制正式叠加收益曲线；请统一数据、Oracle 与执行口径，或明确创建探索对比。",
        }

    from .performance import build_unified_performance

    series: list[dict[str, Any]] = []
    for ctx in ctxs:
        try:
            payload = build_unified_performance(ctx.run_info, include_kline=False)
        except Exception:  # noqa: BLE001 - a supplementary chart must degrade per Run
            series.append(unavailable_series(ctx, "performance_data_unavailable"))
            continue

        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        is_portfolio = payload.get("engine_mode") == "portfolio"
        n_valid_oracle = int(summary.get("n_valid_oracle") or 0)
        n_trades = int(summary.get("n_trades") or 0)
        if payload.get("status") == "unavailable" or (not is_portfolio and n_valid_oracle <= 0):
            series.append(unavailable_series(ctx, "no_valid_oracle_returns"))
            continue
        if n_trades <= 0:
            series.append(unavailable_series(ctx, "no_active_event_returns"))
            continue

        points: list[dict[str, Any]] = []
        for fallback_index, point in enumerate(payload.get("equity_curve") or []):
            if not isinstance(point, dict):
                continue
            index = point.get("index", fallback_index)
            net_value = point.get("net_value")
            if (
                not isinstance(index, (int, float))
                or isinstance(index, bool)
                or not math.isfinite(float(index))
                or not isinstance(net_value, (int, float))
                or isinstance(net_value, bool)
                or not math.isfinite(float(net_value))
            ):
                continue
            curve_point: dict[str, Any] = {"index": int(index), "net_value": float(net_value)}
            timestamp = point.get("timestamp") or point.get("date") or point.get("event_time")
            if allow_mixed and timestamp not in (None, ""):
                curve_point["timestamp"] = str(timestamp)
            points.append(curve_point)

        if len(points) < 2:
            series.append(unavailable_series(ctx, "equity_curve_unavailable"))
            continue

        display_name = _run_display_name(ctx)
        privacy_aggregated = ctx.run_id in safe_run_ids
        public_points = points
        if privacy_aggregated:
            # A published Arena-safe path remains observation-index only. Date
            # stamps can reveal the exact events/trades behind sparse changes.
            public_points = [
                {"index": point["index"], "net_value": point["net_value"]}
                for point in points
            ]
            public_points = _privacy_bucket_curve_points(public_points)
        series.append({
            "run_id": ctx.run_id,
            "name": display_name,
            "display_name": display_name,
            "status": "partial" if payload.get("status") == "partial" else "available",
            "reason": None,
            "aggregate_only": True,
            "privacy_aggregated": privacy_aggregated,
            "points": public_points,
        })

    available_count = sum(item["status"] in {"available", "partial"} for item in series)
    if available_count == 0:
        status = "unavailable"
        reason = "no_performance_curves"
    elif available_count < len(series) or any(item["status"] == "partial" for item in series):
        status = "partial"
        reason = "some_curves_unavailable_or_partial"
    else:
        status = "available"
        reason = None
    if available_count > 0 and allow_mixed and not protocol_comparable:
        reason = "mixed_protocols_exploration"
    return {
        "status": status,
        "reason": reason,
        "strict_comparable": protocol_comparable,
        "axis": {
            "type": "timestamp_or_privacy_index",
            "label": "真实交易日期（Arena-safe 使用观察序号）",
        },
        "series": series,
        "privacy": {
            "aggregation_level": "run_equity_curve",
            "arena_safe_run_ids": sorted(safe_run_ids),
            "downsampled_run_ids": sorted(safe_run_ids),
            "min_bucket_size": 5,
            "excluded_fields": ["event_id", "period_return", "trade"],
            "arena_safe_excluded_fields": ["timestamp"],
            "timestamp_policy": (
                "private_runs_only_in_exploration; arena_safe_always_redacted"
                if allow_mixed and not protocol_comparable else
                "not_embedded; private_client_may_load_its_own_run_performance"
            ),
        },
        "disclaimer": (
            "曲线来自相同冻结数据与执行协议；横轴优先使用真实交易日期。"
            "Arena 仅返回累计净值，不返回策略信号、事件或交易明细。"
            if protocol_comparable else
            "这是显式启用的探索对比：各策略保留自己的真实日期与协议，日期/频率/成本等可能不同，"
            "不得解释为公平排名。Arena-safe 参与者仍只返回隐私聚合观察序号。"
        ),
    }


def _build_performance_curves(
    ctxs: list[ArenaRunContext],
    *,
    run_protocol_hashes: dict[str, str],
    time_alignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build aligned, gap-aware curves while preserving legacy Arenas.

    The explicit alignment path operates only on genuine dated observations.
    It may discard observations outside a window or retain the last genuine
    observation in a wider calendar bucket.  It never interpolates, stretches,
    forward-fills, or converts low-frequency data into higher-frequency data.
    """
    alignment = normalize_time_alignment_config(time_alignment)
    if alignment is None:
        return _build_performance_curves_legacy(
            ctxs,
            run_protocol_hashes=run_protocol_hashes,
        )

    unique_protocols = sorted(set(run_protocol_hashes.values()))
    protocol_comparable = (
        bool(ctxs) and len(unique_protocols) == 1
        and all(ctx.run_info.get("_arena_formal_eligible") is not False for ctx in ctxs)
    )
    allow_mixed = bool(ctxs) and all(
        bool(ctx.run_info.get("_arena_allow_mixed_protocols")) for ctx in ctxs
    )
    safe_run_ids = {
        ctx.run_id
        for ctx in ctxs
        if str(ctx.run_info.get("visibility") or "").strip().lower().replace("-", "_")
        == "arena_safe"
    }
    dates_redacted = bool(safe_run_ids)

    def unavailable_series(ctx: ArenaRunContext, reason: str) -> dict[str, Any]:
        display_name = _run_display_name(ctx)
        return {
            "run_id": ctx.run_id,
            "name": display_name,
            "display_name": display_name,
            "status": "unavailable",
            "reason": reason,
            "aggregate_only": True,
            "privacy_aggregated": ctx.run_id in safe_run_ids,
            "points": [],
        }

    if not protocol_comparable and not allow_mixed:
        # Keep the old safety boundary. Time alignment only reconciles dates and
        # display frequency; it does not make different costs/benchmarks honest.
        legacy = _build_performance_curves_legacy(
            ctxs,
            run_protocol_hashes=run_protocol_hashes,
        )
        legacy["alignment"] = {
            "status": "unavailable",
            "reason": "protocol_mismatch",
            "requested": alignment,
            "ranking_allowed": False,
        }
        legacy["native_metrics"] = {}
        legacy["aligned_metrics"] = {}
        legacy["coverage"] = {"by_run": {}}
        return legacy

    from .performance import build_unified_performance

    raw_by_run: dict[str, dict[str, Any]] = {}
    native_metrics: dict[str, dict[str, Any]] = {}
    base_series: dict[str, dict[str, Any]] = {}
    native_metric_keys = (
        "total_return", "annualized_return", "max_drawdown", "sharpe_ratio",
        "sharpe_proxy", "calmar_ratio", "annualized_volatility", "win_rate",
        "n_trades", "trade_count", "bar_count", "total_turnover", "total_cost",
    )
    for ctx in ctxs:
        try:
            payload = build_unified_performance(ctx.run_info, include_kline=False)
        except Exception:  # noqa: BLE001 - each Run degrades independently
            base_series[ctx.run_id] = unavailable_series(ctx, "performance_data_unavailable")
            continue
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        native_metrics[ctx.run_id] = {
            key: summary.get(key)
            for key in native_metric_keys
            if key in summary
        }
        is_portfolio = payload.get("engine_mode") == "portfolio"
        n_valid_oracle = int(summary.get("n_valid_oracle") or 0)
        n_trades = int(summary.get("n_trades") or 0)
        if payload.get("status") == "unavailable" or (not is_portfolio and n_valid_oracle <= 0):
            base_series[ctx.run_id] = unavailable_series(ctx, "no_valid_oracle_returns")
            continue
        if n_trades <= 0:
            base_series[ctx.run_id] = unavailable_series(ctx, "no_active_event_returns")
            continue

        parsed_points: list[dict[str, Any]] = []
        undated_baseline_value: float | None = None
        for fallback_index, point in enumerate(payload.get("equity_curve") or []):
            if not isinstance(point, dict):
                continue
            net_value = point.get("net_value")
            timestamp_raw = point.get("timestamp") or point.get("date") or point.get("event_time")
            timestamp = _parse_alignment_timestamp(timestamp_raw)
            if (
                not isinstance(net_value, (int, float))
                or isinstance(net_value, bool)
                or not math.isfinite(float(net_value))
                or float(net_value) <= 0
            ):
                continue
            if timestamp is None:
                # Event-proxy curves deliberately carry an undated 1.0 point
                # before the first event return.  Keep the last valid prefix
                # value as a locatable anchor instead of dropping it and then
                # rebasing the first post-return observation back to 1.0.
                if not parsed_points:
                    undated_baseline_value = float(net_value)
                continue
            parsed_points.append({
                "_time": timestamp,
                "_timestamp": str(timestamp_raw),
                "index": int(point.get("index", fallback_index))
                if isinstance(point.get("index", fallback_index), (int, float))
                else fallback_index,
                "net_value": float(net_value),
            })
        # Multiple intraday observations with the exact same timestamp have no
        # unambiguous order on a calendar axis. Retain only the last real point.
        deduplicated = {point["_time"]: point for point in parsed_points}
        parsed_points = sorted(deduplicated.values(), key=lambda item: item["_time"])
        if not parsed_points:
            base_series[ctx.run_id] = unavailable_series(ctx, "dated_equity_curve_unavailable")
            continue
        dataset = payload.get("dataset") if isinstance(payload.get("dataset"), dict) else {}
        data_quality = payload.get("data_quality") if isinstance(payload.get("data_quality"), dict) else {}
        signature = ctx.run_info.get("comparison_signature") if isinstance(ctx.run_info.get("comparison_signature"), dict) else {}
        declared_frequency = dataset.get("frequency") or data_quality.get("frequency") or signature.get("frequency")
        source_frequency, source_seconds = _infer_curve_frequency(parsed_points, declared_frequency)
        raw_by_run[ctx.run_id] = {
            "ctx": ctx,
            "payload_status": payload.get("status"),
            "points": parsed_points,
            "source_frequency": source_frequency,
            "source_seconds": source_seconds,
            "native_start": parsed_points[0]["_time"],
            "native_end": parsed_points[-1]["_time"],
            "undated_baseline_value": undated_baseline_value,
            "is_event_proxy": not is_portfolio,
        }

    available = list(raw_by_run.values())
    if available:
        union_start = min(item["native_start"] for item in available)
        union_end = max(item["native_end"] for item in available)
        intersection_start = max(item["native_start"] for item in available)
        intersection_end = min(item["native_end"] for item in available)
    else:
        union_start = union_end = intersection_start = intersection_end = None

    mode = alignment["mode"]
    if mode == "intersection":
        window_start, window_end = intersection_start, intersection_end
    elif mode == "union":
        window_start, window_end = union_start, union_end
    elif mode == "manual":
        window_start = _parse_alignment_timestamp(alignment.get("start_date"), end_of_day=False)
        window_end = _parse_alignment_timestamp(alignment.get("end_date"), end_of_day=True)
    else:
        # Native mode retains each Run's own full range. The global union is
        # metadata only and must not be mistaken for a common evaluation span.
        window_start, window_end = union_start, union_end

    no_common_window = (
        mode == "intersection"
        and (window_start is None or window_end is None or window_start > window_end)
    )
    series: list[dict[str, Any]] = []
    aligned_metrics: dict[str, dict[str, Any]] = {}
    coverage_by_run: dict[str, dict[str, Any]] = {}
    alignment_warnings: list[str] = []

    for ctx in ctxs:
        raw = raw_by_run.get(ctx.run_id)
        if raw is None:
            series.append(base_series.get(ctx.run_id) or unavailable_series(ctx, "performance_data_unavailable"))
            coverage_by_run[ctx.run_id] = {
                "status": "unavailable",
                "reason": (base_series.get(ctx.run_id) or {}).get("reason") or "performance_data_unavailable",
                "sample_sufficient": False,
                "native_observations": 0,
                "aligned_observations": 0,
            }
            continue
        if no_common_window:
            series.append(unavailable_series(ctx, "no_common_time_window"))
            coverage_by_run[ctx.run_id] = {
                "status": "unavailable", "reason": "no_common_time_window",
                "sample_sufficient": False,
                "native_observations": len(raw["points"]), "aligned_observations": 0,
            }
            continue

        effective_start = raw["native_start"] if mode == "native" else window_start
        effective_end = raw["native_end"] if mode == "native" else window_end
        selected = [
            point for point in raw["points"]
            if (effective_start is None or point["_time"] >= effective_start)
            and (effective_end is None or point["_time"] <= effective_end)
        ]
        sampled_observations, effective_frequency, frequency_warnings = _resample_curve_points(
            selected,
            requested_frequency=alignment["frequency"],
            source_seconds=raw["source_seconds"],
        )
        metric_points = list(selected)
        sampled = list(sampled_observations)
        baseline_value = raw.get("undated_baseline_value")
        baseline_applies = False
        if raw.get("is_event_proxy") and selected:
            first_selected_index = next(
                (
                    index for index, point in enumerate(raw["points"])
                    if point["_time"] == selected[0]["_time"]
                ),
                0,
            )
            if first_selected_index > 0:
                # Event observations are returns *at* their timestamps.  For an
                # inclusive clipped window, the immediately preceding equity is
                # the anchor needed to retain the first in-window event return.
                baseline_value = raw["points"][first_selected_index - 1]["net_value"]
            baseline_applies = baseline_value is not None
        if baseline_applies:
            # The anchor shares the first event timestamp but has an earlier
            # sequence phase.  This preserves the first event return without
            # inventing a calendar observation or extending the native range.
            anchor = {
                "_time": selected[0]["_time"],
                "_timestamp": selected[0]["_timestamp"],
                "_anchor": True,
                "index": -1,
                "net_value": float(baseline_value),
            }
            metric_points.insert(0, anchor)
            sampled.insert(0, anchor)
        alignment_warnings.extend(f"{ctx.run_id}: {warning}" for warning in frequency_warnings)
        metrics = _aligned_curve_metrics(
            metric_points,
            # Display downsampling is independent from risk measurement.  The
            # latter always consumes every genuine observation in the window.
            effective_frequency="native",
            min_observations=alignment["min_observations"],
        )
        metrics["metric_frequency"] = "native"
        metrics["display_frequency"] = effective_frequency
        metrics["n_display_observations"] = len(sampled)
        aligned_metrics[ctx.run_id] = metrics

        missing_segments: list[dict[str, Any]] = []
        positive_deltas = [
            (right["_time"] - left["_time"]).total_seconds()
            for left, right in zip(sampled, sampled[1:])
            if right["_time"] > left["_time"]
        ]
        requested_seconds = _target_frequency_seconds(effective_frequency)
        observed_median_delta = statistics.median(positive_deltas) if positive_deltas else None
        expected_delta = requested_seconds or raw["source_seconds"] or observed_median_delta or 86_400
        # Trading weekends and short market holidays are not missing data. A
        # discontinuity must exceed four normal observations (at least 7 days
        # for daily data) before the chart is split into separate segments.
        gap_threshold = max(
            expected_delta * 4,
            7 * 86_400 if effective_frequency == "day" else 0,
        )
        public_points: list[dict[str, Any]] = []
        base_value = float(sampled[0]["net_value"]) if sampled else None
        segment = 0
        previous: dict[str, Any] | None = None
        for index, point in enumerate(sampled):
            if previous is not None:
                gap_seconds = (point["_time"] - previous["_time"]).total_seconds()
                if gap_seconds > gap_threshold:
                    segment += 1
                    missing_segments.append({
                        "after": _iso_alignment_timestamp(previous["_time"]),
                        "before": _iso_alignment_timestamp(point["_time"]),
                        "gap_days": float(f"{gap_seconds / 86_400:.3f}"),
                    })
            public_points.append({
                "index": index,
                "net_value": float(point["net_value"]) / base_value if base_value else float(point["net_value"]),
                "timestamp": _iso_alignment_timestamp(point["_time"]),
                "segment": segment,
                "_time": point["_time"],
                "_slot_phase": 0 if point.get("_anchor") else 1,
            })
            previous = point

        window_seconds = (
            max(0.0, (effective_end - effective_start).total_seconds())
            if effective_start is not None and effective_end is not None else 0.0
        )
        covered_start = max(raw["native_start"], effective_start) if effective_start is not None else raw["native_start"]
        covered_end = min(raw["native_end"], effective_end) if effective_end is not None else raw["native_end"]
        covered_seconds = max(0.0, (covered_end - covered_start).total_seconds())
        coverage_ratio = 1.0 if mode == "native" else (
            min(1.0, covered_seconds / window_seconds) if window_seconds > 0 else 0.0
        )
        has_leading_gap = bool(effective_start and raw["native_start"] > effective_start)
        has_trailing_gap = bool(effective_end and raw["native_end"] < effective_end)
        coverage_status = (
            "unavailable" if not sampled
            else "insufficient" if not metrics.get("sample_sufficient")
            else "partial" if has_leading_gap or has_trailing_gap or missing_segments
            else "complete"
        )
        coverage_warnings = list(frequency_warnings)
        if sampled and not metrics.get("sample_sufficient"):
            coverage_warnings.append("aligned_sample_short")
        if has_leading_gap:
            coverage_warnings.append("history_starts_after_selected_window")
        if has_trailing_gap:
            coverage_warnings.append("history_ends_before_selected_window")
        if missing_segments:
            coverage_warnings.append("internal_missing_periods")
        coverage_by_run[ctx.run_id] = {
            "status": coverage_status,
            "reason": "no_observations_in_selected_window" if not sampled else (
                "sample_too_short" if not metrics.get("sample_sufficient") else None
            ),
            "native_start": None if ctx.run_id in safe_run_ids else _iso_alignment_timestamp(raw["native_start"]),
            "native_end": None if ctx.run_id in safe_run_ids else _iso_alignment_timestamp(raw["native_end"]),
            "aligned_start": None if ctx.run_id in safe_run_ids else metrics.get("start_at"),
            "aligned_end": None if ctx.run_id in safe_run_ids else metrics.get("end_at"),
            "dates_redacted": ctx.run_id in safe_run_ids,
            "source_frequency": raw["source_frequency"],
            "requested_frequency": alignment["frequency"],
            "effective_frequency": effective_frequency,
            "native_observations": len(raw["points"]) + (1 if baseline_value is not None else 0),
            "aligned_observations": len(metric_points),
            "display_observations": len(sampled),
            "metric_frequency": "native",
            "min_observations": alignment["min_observations"],
            "sample_sufficient": bool(metrics.get("sample_sufficient")),
            "window_coverage_ratio": float(f"{coverage_ratio:.6f}"),
            "has_leading_gap": has_leading_gap,
            "has_trailing_gap": has_trailing_gap,
            "missing_segments": [] if ctx.run_id in safe_run_ids else missing_segments,
            "warnings": coverage_warnings,
        }

        display_name = _run_display_name(ctx)
        privacy_aggregated = ctx.run_id in safe_run_ids
        series_status = (
            "unavailable" if len(sampled) < 2
            else "partial" if raw["payload_status"] == "partial" or coverage_status in {"partial", "insufficient"}
            else "available"
        )
        series.append({
            "run_id": ctx.run_id,
            "name": display_name,
            "display_name": display_name,
            "status": series_status,
            "reason": (
                "no_observations_in_selected_window" if not sampled
                else "only_one_observation_in_selected_window" if len(sampled) == 1
                else "sample_too_short" if not metrics.get("sample_sufficient")
                else None
            ),
            "aggregate_only": True,
            "privacy_aggregated": privacy_aggregated,
            "points": public_points,
        })

    # Give every series the same opaque alignment coordinate before stripping
    # private timestamps.  In a union/manual Arena this keeps leading gaps
    # visible for Arena-safe curves instead of restarting every path at x=0.
    # The phase distinguishes an event-proxy baseline from the first post-event
    # value at the same timestamp.
    slot_keys = {
        (point["_time"], int(point.get("_slot_phase") or 0))
        for item in series
        for point in item.get("points", [])
        if isinstance(point.get("_time"), dt.datetime)
    }
    if slot_keys and mode in {"union", "manual"} and window_start is not None:
        first_slot_time = min(key[0] for key in slot_keys)
        if window_start < first_slot_time:
            slot_keys.add((window_start, -1))
    slot_by_key = {
        key: index
        for index, key in enumerate(sorted(slot_keys, key=lambda item: (item[0], item[1])))
    }
    for item in series:
        internal_points = item.get("points") if isinstance(item.get("points"), list) else []
        for point in internal_points:
            point["index"] = slot_by_key.get(
                (point.get("_time"), int(point.get("_slot_phase") or 0)),
                point.get("index", 0),
            )
        if item.get("privacy_aggregated"):
            safe_points = [
                {
                    "index": point["index"],
                    "net_value": point["net_value"],
                    "segment": point.get("segment", 0),
                }
                for point in internal_points
            ]
            item["points"] = _privacy_bucket_curve_points(safe_points)
        else:
            for point in internal_points:
                point.pop("_time", None)
                point.pop("_slot_phase", None)

    available_count = sum(item["status"] in {"available", "partial"} for item in series)
    if available_count == 0:
        status = "unavailable"
        reason = "no_common_time_window" if no_common_window else "no_aligned_performance_curves"
    elif available_count < len(series) or any(item["status"] == "partial" for item in series):
        status = "partial"
        reason = "some_curves_unavailable_partial_or_short"
    else:
        status = "available"
        reason = None
    if available_count > 0 and allow_mixed and not protocol_comparable:
        reason = "mixed_protocols_exploration"

    all_samples_sufficient = bool(aligned_metrics) and all(
        metrics.get("sample_sufficient") for metrics in aligned_metrics.values()
    ) and len(aligned_metrics) == len(ctxs)
    common_window_mode = mode in {"intersection", "manual"}
    ranking_allowed = bool(
        protocol_comparable and common_window_mode and all_samples_sufficient
    )
    aligned_ranking: dict[str, list[dict[str, Any]]] = {}
    if ranking_allowed:
        lower_is_better = {"max_drawdown", "annualized_volatility"}
        for metric_id in (
            "total_return", "annualized_return", "max_drawdown",
            "sharpe_ratio", "calmar_ratio", "annualized_volatility",
        ):
            rows = [
                {
                    "run_id": ctx.run_id,
                    "display_name": _run_display_name(ctx),
                    "value": aligned_metrics.get(ctx.run_id, {}).get(metric_id),
                }
                for ctx in ctxs
            ]
            valid_rows = [
                row for row in rows
                if isinstance(row["value"], (int, float))
                and not isinstance(row["value"], bool)
                and math.isfinite(float(row["value"]))
            ]
            valid_rows.sort(
                key=lambda row: float(row["value"]),
                reverse=metric_id not in lower_is_better,
            )
            previous_value: float | None = None
            previous_rank = 0
            for index, row in enumerate(valid_rows):
                value = float(row["value"])
                rank = previous_rank if previous_value is not None and value == previous_value else index + 1
                row["rank"] = rank
                previous_value, previous_rank = value, rank
            aligned_ranking[metric_id] = valid_rows
    public_window_start = None if dates_redacted else _iso_alignment_timestamp(window_start)
    public_window_end = None if dates_redacted else _iso_alignment_timestamp(window_end)
    public_intersection_start = None if dates_redacted else _iso_alignment_timestamp(intersection_start)
    public_intersection_end = None if dates_redacted else _iso_alignment_timestamp(intersection_end)
    return {
        "status": status,
        "reason": reason,
        "strict_comparable": protocol_comparable,
        "axis": {
            "type": "privacy_observation_index" if dates_redacted else "calendar_time",
            "label": "Arena-safe 隐私观察序号" if dates_redacted else "真实交易日期（缺失区间留白）",
        },
        "series": series,
        "alignment": {
            "status": "unavailable" if status == "unavailable" else "available",
            "requested": alignment,
            "mode": mode,
            "frequency": alignment["frequency"],
            "window_start": public_window_start,
            "window_end": public_window_end,
            "intersection_start": public_intersection_start,
            "intersection_end": public_intersection_end,
            "dates_redacted": dates_redacted,
            "no_interpolation": True,
            "no_forward_fill": True,
            "upsampling_allowed": False,
            "ranking_allowed": ranking_allowed,
            "ranking_reason": (
                None if ranking_allowed
                else "exploration_mode_not_fair_ranking" if allow_mixed and not protocol_comparable
                else "union_or_native_ranges_are_not_a_common_window" if not common_window_mode
                else "one_or_more_runs_have_insufficient_aligned_samples"
            ),
            "warnings": sorted(set(alignment_warnings)),
        },
        "coverage": {
            "union_start": None if dates_redacted else _iso_alignment_timestamp(union_start),
            "union_end": None if dates_redacted else _iso_alignment_timestamp(union_end),
            "intersection_start": public_intersection_start,
            "intersection_end": public_intersection_end,
            "dates_redacted": dates_redacted,
            "by_run": coverage_by_run,
        },
        "native_metrics": native_metrics,
        "aligned_metrics": aligned_metrics,
        "aligned_ranking": aligned_ranking,
        "privacy": {
            "aggregation_level": "run_equity_curve",
            "arena_safe_run_ids": sorted(safe_run_ids),
            "downsampled_run_ids": sorted(safe_run_ids),
            "min_bucket_size": 5,
            "excluded_fields": ["event_id", "period_return", "trade"],
            "arena_safe_excluded_fields": ["timestamp", "coverage_dates", "missing_segments"],
            "timestamp_policy": "explicit_alignment_private_only; arena_safe_always_redacted",
            "alignment_index_policy": "global_opaque_slot_with_segmented_gaps",
        },
        "disclaimer": (
            "时间对齐只裁剪真实观测并允许向日/周/月降采样；不插值、不前向填充，也不把低频数据伪造成高频。"
            + (
                " 当前为探索对照：原始周期指标可并列查看，对齐指标也不构成公平排名。"
                if allow_mixed and not protocol_comparable else
                " 只有共同区间且样本充足时，对齐指标才可用于正式排名。"
            )
        ),
    }


def compute_arena_result(
    ctxs: list[ArenaRunContext],
    *,
    selected_metric_ids: Optional[list[str]] = None,
    labels_list: Optional[list] = None,
    arena_type: str = "prediction",
    time_alignment: dict[str, Any] | None = None,
) -> dict:
    """主入口：计算 Arena 的完整比对结果。"""
    from .metrics_registry import list_metric_defs
    all_defs = list_metric_defs()
    arena_family = _arena_family(arena_type)
    investment_track = arena_family == "performance"
    selected_metric_ids = resolve_metric_selection(
        ctxs,
        selected_metric_ids,
        arena_type=arena_type,
    )
    redacted_run_ids = [c.run_id for c in ctxs if _is_arena_safe_context(c)]
    protected_run_ids = set(redacted_run_ids)
    # Rankings, radar values, composite scores and Pareto effects must all read
    # from this privacy-filtered view.  Keeping one public source of truth
    # prevents a newly added aggregate from accidentally reading raw metrics.
    public_ctxs = _public_arena_contexts(ctxs)

    # 1) per-run-metrics（按 selected_metric_ids 过滤）
    per_run = {}
    for c in public_ctxs:
        display_name = _run_display_name(c)
        visible_metrics: dict[str, dict[str, Any]] = {}
        for mid in selected_metric_ids:
            metric = c.metrics.get(mid)
            if not metric:
                continue
            visible_metrics[mid] = deepcopy(metric)
        protected = c.run_id in protected_run_ids
        per_run[c.run_id] = {
            "run_id": c.run_id,
            "display_name": display_name,
            "runner": None if protected else c.runner,
            "prompt_variant": None if protected else c.prompt_variant,
            "model_version": None if protected else c.model_version,
            "status": c.status,
            "done_events": c.done_events,
            "total_events": c.total_events,
            "metrics": visible_metrics,
        }
        run_config = c.run_info.get("config") if isinstance(c.run_info.get("config"), dict) else {}
        model_lab = run_config.get("model_lab") if isinstance(run_config.get("model_lab"), dict) else {}
        profile = (
            run_config.get("model_profile_snapshot")
            if isinstance(run_config.get("model_profile_snapshot"), dict)
            else model_lab.get("profile_snapshot")
            if isinstance(model_lab.get("profile_snapshot"), dict)
            else {}
        )
        lineage = {
            "source": str(run_config.get("source") or run_config.get("origin") or model_lab.get("source") or "").strip() or None,
            "experiment_id": str(run_config.get("experiment_id") or model_lab.get("experiment_id") or "").strip() or None,
            "batch_id": str(run_config.get("model_lab_batch_id") or model_lab.get("batch_id") or "").strip() or None,
            "task_id": str(run_config.get("model_lab_task_id") or model_lab.get("task_id") or "").strip() or None,
            "profile_id": str(profile.get("id") or model_lab.get("profile_id") or "").strip() or None,
            "profile_name": str(profile.get("name") or model_lab.get("profile_name") or "").strip() or None,
            "model": str(profile.get("model") or c.model_version or "").strip() or None,
        }
        if not protected and any(value is not None for value in lineage.values()):
            # Deliberately return identifiers/display names only.  Profile
            # endpoint URLs and secret references stay outside Arena results.
            per_run[c.run_id]["lineage"] = lineage

    # 2) ranking（每个指标）
    ranking = {}
    for mid in selected_metric_ids:
        ranking[mid] = _rank_metric(public_ctxs, mid, all_defs.get(mid))

    # 3) 雷达图（只取有 value 的"核心几个" + 手动挑的扩展指标）
    radar_metrics = []
    preferred = (
        ["strategy_total_return", "strategy_max_drawdown", "strategy_sharpe_proxy", "strategy_win_rate", "coverage_rate"]
        if investment_track
        else [
            "acc_avg_all_strict", "acc_t3_strict", "acc_primary_non_neutral",
            "acc_primary_significant_only", "coverage_rate", "abstain_rate",
            "calibration_mse", "direction_bias", "acc_high_confidence",
        ]
    )
    for mid in preferred:
        if mid in selected_metric_ids:
            radar_metrics.append(mid)
    # 不够 5 个时用 selected 的前几个补齐
    if len(radar_metrics) < 5:
        for mid in selected_metric_ids:
            if mid not in radar_metrics:
                radar_metrics.append(mid)
                if len(radar_metrics) >= 7:
                    break
    radar = _normalize_for_radar(public_ctxs, radar_metrics, all_defs)

    from .evaluation_protocol import resolve_run_horizon
    primary_horizon = resolve_run_horizon(public_ctxs[0].run_info) if public_ctxs else "t3"
    # 4) 同一事件上的配对检验。收益赛道复用逐事件收益差；预测赛道用准确率类指标。
    pairwise_targets = (
        # 当前稳健性检验的 estimand 是同事件净收益差的均值，只能为累计
        # 收益提供统计证据；不能把同一 p-value 冒充为回撤、Sharpe 或胜率
        # 各自的检验结果。后续若新增 block bootstrap，再逐项开放。
        [mid for mid in selected_metric_ids if mid == "strategy_total_return"]
        if arena_family == "performance"
        else [mid for mid in selected_metric_ids if mid.startswith("acc_")]
        if arena_family == "forecast"
        else [mid for mid in selected_metric_ids if mid.startswith("acc_") or _metric_family(mid) == "performance"]
    )
    pairwise = _pairwise_compare(
        public_ctxs,
        pairwise_targets,
        all_defs,
        labels_list=labels_list,
        arena_family=arena_family,
        protected_run_ids=protected_run_ids,
        primary_horizon=primary_horizon,
    )
    head_to_head = _shared_event_head_to_head(
        public_ctxs,
        labels_list=labels_list,
        primary_horizon=primary_horizon,
    )
    if redacted_run_ids:
        # Event-level rows can reveal an Arena-safe participant's exact trades
        # directly, or indirectly through winners/oracle outcomes. Keep only
        # aggregate pairwise evidence when any participant is aggregate-only.
        h2h_summary = head_to_head.get("summary", {}) if isinstance(head_to_head, dict) else {}
        small_sample_summary = any(
            not isinstance(item, dict) or int(item.get("scored") or 0) < 5
            for item in h2h_summary.values()
        ) or not h2h_summary
        head_to_head = {
            "summary": {} if small_sample_summary else h2h_summary,
            "event_level": [],
            "privacy": {
                "event_level_redacted": True,
                "redacted_run_ids": redacted_run_ids,
                "small_sample_summary_redacted": small_sample_summary,
                "minimum_aggregate_sample": 5,
            },
        }
    composite = _build_composite_score(public_ctxs, selected_metric_ids, all_defs)

    from .protocol import EVALUATOR_VERSION, comparison_protocol_hash_for_run
    run_protocol_hashes = {
        c.run_id: (
            str(c.run_info.get("comparison_protocol_hash") or "").strip()
            or comparison_protocol_hash_for_run(c.run_info)
        )
        for c in public_ctxs
    }
    unique_protocols = sorted(set(run_protocol_hashes.values()))
    comparison_protocol = {
        "strict_comparable": bool(ctxs) and len(unique_protocols) == 1,
        "protocol_hash": unique_protocols[0] if len(unique_protocols) == 1 else None,
        "run_protocol_hashes": run_protocol_hashes,
        "mismatched_run_ids": [] if len(unique_protocols) <= 1 else list(run_protocol_hashes),
        "primary_oracle_horizon": primary_horizon,
        "event_window": _run_event_selection(ctxs[0].run_info).to_dict() if ctxs else None,
        "evaluator_version": EVALUATOR_VERSION,
    }
    performance_curves = (
        _build_performance_curves(
            public_ctxs,
            run_protocol_hashes=run_protocol_hashes,
            time_alignment=time_alignment,
        )
        if investment_track
        else None
    )

    # 把成本信息合并进 per_run（便于前端任意 Tab 中复用）
    # 成本估算：按商用 LLM 通用中位数 $0.50 / 1M in, $1.50 / 1M out 计算
    PRICE_IN_PER_M = 0.50
    PRICE_OUT_PER_M = 1.50
    for c in public_ctxs:
        rid = c.run_id
        if rid in per_run:
            tokens_in = int(c.tokens_in or 0)
            tokens_out = int(c.tokens_out or 0)
            step_ms = int(c.step_ms_total or 0)
            estimated_cost = (tokens_in / 1_000_000) * PRICE_IN_PER_M + (tokens_out / 1_000_000) * PRICE_OUT_PER_M
            cost_known = bool(c.cost_usd) or (tokens_in + tokens_out) > 0
            cost_usd = (float(c.cost_usd or 0.0) or estimated_cost) if cost_known else None
            per_run[rid]["tokens_in"] = tokens_in
            per_run[rid]["tokens_out"] = tokens_out
            per_run[rid]["tokens_total"] = tokens_in + tokens_out
            per_run[rid]["step_ms_total"] = step_ms
            per_run[rid]["cost_usd_estimate"] = round(cost_usd, 6) if cost_usd is not None else None
            per_run[rid]["cost_source"] = (
                "reported" if c.cost_usd else "token_estimate" if cost_known else "unavailable"
            )

    # 7) Pareto 成本/效果 散点数据（给前端帕累托 Tab 直接用）
    pareto_points: list[dict] = []
    for c in public_ctxs:
        rid = c.run_id
        tokens_total = int(c.tokens_in or 0) + int(c.tokens_out or 0)
        cost_known = bool(c.cost_usd) or tokens_total > 0
        if not cost_known:
            # A numeric zero produced by missing telemetry is not a free run.
            # Keep it null in per_run and exclude it from cost/effect Pareto math.
            continue
        estimated_cost = (
            (int(c.tokens_in or 0) / 1_000_000) * PRICE_IN_PER_M
            + (int(c.tokens_out or 0) / 1_000_000) * PRICE_OUT_PER_M
        )
        cost_usd = float(c.cost_usd or 0.0) or estimated_cost
        # 效果 Y：优先用 primary_non_neutral；否则用 acc_t3_strict；否则 composite_score (若有)
        effect_y = None
        effect_metric_id = "composite_score"
        effect_candidates = (
            ("strategy_total_return", "strategy_sharpe_proxy", "strategy_win_rate")
            if investment_track
            else ("acc_primary_non_neutral", "acc_t3_strict", "acc_avg_all_strict")
        )
        for mid in effect_candidates:
            v = (c.metrics.get(mid) or {}).get("value")
            if isinstance(v, (int, float)):
                effect_y = float(v)
                effect_metric_id = mid
                break
        composite_lookup = {
            run_id: float(item["score"])
            for run_id, item in (composite.get("per_run_score") or {}).items()
            if isinstance(item, dict) and isinstance(item.get("score"), (int, float))
        } if isinstance(composite, dict) else {}
        if effect_y is None and rid in composite_lookup:
            effect_y = composite_lookup[rid]
        display = per_run.get(rid, {}).get("display_name") or _run_display_name(c)
        pareto_points.append({
            "run_id": rid,
            "label": display,
            "runner": None if rid in protected_run_ids else c.runner,
            "prompt_variant": None if rid in protected_run_ids else c.prompt_variant,
            "model_version": None if rid in protected_run_ids else c.model_version,
            "effect": effect_y,
            "effect_metric_id": effect_metric_id,
            "cost_tokens": tokens_total,
            "cost_usd": round(cost_usd, 6),
            "step_ms": int(c.step_ms_total or 0),
            "composite_score": composite_lookup.get(rid),
        })
    # 计算非支配（帕累托）前沿：最大化 effect，最小化 cost_tokens
    def _dominates(a: dict, b: dict) -> bool:
        # a dominates b ⇔ a.effect >= b.effect AND a.cost_tokens <= b.cost_tokens，且至少一个严格不等
        eff_ok = (a.get("effect") or 0) >= (b.get("effect") or 0)
        cost_ok = (a.get("cost_tokens") or 0) <= (b.get("cost_tokens") or 0)
        strict = (
            (a.get("effect") or 0) > (b.get("effect") or 0)
            or (a.get("cost_tokens") or 0) < (b.get("cost_tokens") or 0)
        )
        return eff_ok and cost_ok and strict
    frontier_ids: set[str] = set()
    for i, p in enumerate(pareto_points):
        dominated = False
        for j, q in enumerate(pareto_points):
            if i == j:
                continue
            if _dominates(q, p):
                dominated = True
                break
        if not dominated:
            frontier_ids.add(str(p.get("run_id") or ""))
    for p in pareto_points:
        p["on_pareto_frontier"] = str(p.get("run_id") or "") in frontier_ids
    # 把 frontier 按 cost 排序后得到连线路径（从"最便宜 → 最有效但最贵"单调）
    frontier_pts = sorted(
        [p for p in pareto_points if p.get("on_pareto_frontier")],
        key=lambda p: (p.get("cost_tokens") or 0, -(p.get("effect") or 0)),
    )
    pareto_chart = {
        "points": pareto_points,
        "frontier_run_ids": [p.get("run_id") for p in frontier_pts],
        "frontier_line": [
            {"run_id": p.get("run_id"), "cost_tokens": p.get("cost_tokens"), "effect": p.get("effect")}
            for p in frontier_pts
        ],
        "defaults": {
            "y_axis": "effect",  # acc_primary_non_neutral
            "x_axis": "cost_tokens",  # tokens_total (in+out)
        },
    }

    return {
        "run_count": len(ctxs),
        "arena_type": arena_type,
        "selected_metric_ids": selected_metric_ids,
        "metric_defs": {mid: all_defs[mid] for mid in selected_metric_ids if mid in all_defs},
        "per_run": per_run,
        "ranking": ranking,
        "radar_chart": radar,
        "pairwise_tests": pairwise,
        "head_to_head": head_to_head,
        "composite_score": composite,
        "pareto_chart": pareto_chart,
        "performance_curves": performance_curves,
        "comparison_protocol": comparison_protocol,
    }


def _build_composite_score(
    ctxs: list[ArenaRunContext],
    metric_ids: list[str],
    metric_defs: dict[str, dict],
) -> dict[str, Any] | None:
    """Weighted rank-points score with an explicit, inspectable weight contract."""
    if len(ctxs) <= 1 or not metric_ids:
        return None
    # 每个指标先 rank
    per_metric_rank: dict[str, dict[str, Optional[int]]] = {}
    for mid in metric_ids:
        rows = _rank_metric(ctxs, mid, metric_defs.get(mid))
        per_metric_rank[mid] = {r["run_id"]: r["rank"] for r in rows}
    # Core metrics receive 2 raw units and extended metrics 1 raw unit.
    scores: dict[str, float] = {c.run_id: 0.0 for c in ctxs}
    weights_sum = 0.0
    raw_weights = {
        mid: (2.0 if metric_defs.get(mid, {}).get("tier") == "core" else 1.0)
        for mid in metric_ids
    }
    for mid in metric_ids:
        w = raw_weights[mid]
        # 对"higher_is_better"，rank=1 给最高分；对 "lower_is_better"也一样（_rank_metric 已经排序好了）
        N = len(ctxs)
        for c in ctxs:
            rk = per_metric_rank[mid].get(c.run_id)
            if rk is None:
                # 无值：给倒数第一
                s = 0.0
            else:
                # 线性映射：rk=1 → N points, rk=N → 1 point；None → 0
                s = max(0.0, N + 1 - float(rk))
            scores[c.run_id] += s * w
        weights_sum += w * N
    # 归一化到 0..100
    max_possible = weights_sum if weights_sum > 0 else 1.0
    norm = {rid: float(f"{100.0 * s / max_possible:.3f}") for rid, s in scores.items()}
    rows = [{"run_id": rid, "score": s} for rid, s in norm.items()]
    rows.sort(key=lambda x: -x["score"])
    for i, r in enumerate(rows):
        if i == 0:
            r["rank"] = 1
        else:
            r["rank"] = (i + 1) if r["score"] != rows[i - 1]["score"] else rows[i - 1]["rank"]
    return {
        "method": "weighted_rank_points",
        "description": "每个指标按方向转为名次分；core 原始权重 2、extended 原始权重 1，再按权重总和归一化至 0–100。",
        "metric_weights": {
            mid: {
                "raw_weight": raw_weights[mid],
                "normalized_weight": float(f"{raw_weights[mid] / sum(raw_weights.values()):.6f}"),
                "tier": metric_defs.get(mid, {}).get("tier"),
            }
            for mid in metric_ids
        },
        "score_formula": "100 * Σ(weight_i * (N + 1 - dense_rank_i)) / (N * Σ weight_i)",
        "per_run_score": {
            str(row["run_id"]): {"score": row["score"], "rank": row["rank"]}
            for row in rows
        },
    }
