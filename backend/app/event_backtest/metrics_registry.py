"""可插拔指标注册系统（Metrics Registry）。

设计目标：
  - 指标不再硬编码在 MetricsSummary dataclass 里；每个指标是一个独立的 Calculator
  - 新增指标只需写一个函数 + @register_metric，无需改动 dataclass / DB schema / schemas
  - 返回结构统一为 {metric_id: MetricResult}，天然支持前端按需渲染

指标分层（tier）：
  - core     ：核心必算（如 ACC 全时间窗口），无论配置都会计算
  - extended ：扩展指标（如校准度、覆盖度、收益分布），默认算，可配置关闭
  - custom   ：用户/实验性指标，需显式启用

每个 MetricResult 含：
  value        : 主值（scalar 或 dict）
  display_name : 显示名
  description  : 一句话说明
  tier         : core/extended/custom
  higher_is_better : True=越大越好（排名时用）
  breakdown    : 可选的细分（如按 market/type 分组、置信区间等）
  meta         : 自由元信息（如样本数 n、配置参数）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Iterable, Optional

from .models import EventLabel, Label, TeamPrediction, Horizon, ALL_HORIZONS


# ======================================================== 基础工具（沿用旧 metrics.py） ========

def _wilson_lower(n: int, k: int, *, z: float = 1.959963984540054) -> float:
    if n <= 0:
        return 0.0
    k = max(0, min(n, k))
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    lo = center - margin
    return 0.0 if lo < 0 else (1.0 if lo > 1 else lo)


def _wilson_upper(n: int, k: int, *, z: float = 1.959963984540054) -> float:
    if n <= 0:
        return 1.0
    k = max(0, min(n, k))
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    hi = center + margin
    return 1.0 if hi > 1 else (0.0 if hi < 0 else hi)


def _mk_acc_stat(n: int, k: int) -> dict:
    acc = (k / n) if n > 0 else 0.0
    return {
        "n": n, "k": k, "acc": float(f"{acc:.6f}"),
        "wilson_lo_95": float(f"{_wilson_lower(n, k):.6f}"),
        "wilson_hi_95": float(f"{_wilson_upper(n, k):.6f}"),
    }


# ======================================================== 数据结构定义 =========================

@dataclass
class MetricResult:
    """单个指标的计算结果。"""
    value: Any                                    # 主值：scalar 或 dict
    display_name: str                             # 前端显示名
    description: str                              # 一句话说明
    tier: str = "extended"                        # core / extended / custom
    higher_is_better: bool = True                 # 排名时方向
    breakdown: dict[str, Any] = field(default_factory=dict)  # 细分：分组、CI、分布
    meta: dict[str, Any] = field(default_factory=dict)       # 元信息：样本数、配置等

    def to_dict(self) -> dict:
        return asdict(self)


# ======================================================== 计算上下文 ===========================

@dataclass
class ComputeContext:
    """传给每个指标计算器的上下文。"""
    pairs: list[tuple[TeamPrediction, EventLabel]]   # pred ∩ labels 的配对
    n_total: int
    primary_oracle_horizon: Horizon
    epsilon: float
    exclude_non_significant: bool
    non_sig_event_ids: set[str]                       # 被显著性过滤掉的 event_id
    execution_spec: dict[str, Any] = field(default_factory=dict)
    allowed_event_ids: Optional[set[str]] = None
    event_order: Optional[list[str]] = None
    predictions: list[TeamPrediction] = field(default_factory=list)

    def label_of(self, lab: EventLabel, h: str) -> Label:
        direction = str(getattr(lab, f"label_{h}", "") or "").strip().lower()
        if direction not in {"up", "down", "neutral"}:
            return ""
        if h == "consensus66":
            component_cars = [getattr(lab, f"car_{name}", None) for name in ("t3", "t7", "t15", "t30", "t60")]
            if not any(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) for value in component_cars):
                return ""
        else:
            car = getattr(lab, f"car_{h}", None)
            if not isinstance(car, (int, float)) or isinstance(car, bool) or not math.isfinite(float(car)):
                return ""
        return direction  # type: ignore[return-value]


# ======================================================== 注册表 ==============================

MetricFn = Callable[[ComputeContext], MetricResult]

_REGISTRY: dict[str, dict] = {}  # metric_id → {fn, display_name, description, tier, higher_is_better}


def register_metric(
    metric_id: str,
    *,
    display_name: str,
    description: str,
    tier: str = "extended",
    higher_is_better: bool = True,
) -> Callable[[MetricFn], MetricFn]:
    """注册一个指标。

    用法：
        @register_metric("acc_t3_strict", display_name="T3 准确率(严格)",
                         description="T3 窗口，abstain/neutral 算错", tier="core")
        def calc_acc_t3_strict(ctx: ComputeContext) -> MetricResult:
            ...
    """
    def deco(fn: MetricFn) -> MetricFn:
        _REGISTRY[metric_id] = {
            "fn": fn,
            "display_name": display_name,
            "description": description,
            "tier": tier,
            "higher_is_better": higher_is_better,
        }
        return fn
    return deco


def list_metric_defs(*, tier: Optional[str] = None) -> dict[str, dict]:
    """列出已注册的指标元信息（不含 fn）。"""
    out = {}
    for mid, info in _REGISTRY.items():
        if tier and info["tier"] != tier:
            continue
        out[mid] = {k: v for k, v in info.items() if k != "fn"}
    return out


def get_metric_def(metric_id: str) -> Optional[dict]:
    info = _REGISTRY.get(metric_id)
    if not info:
        return None
    return {k: v for k, v in info.items() if k != "fn"}


# ======================================================== 核心计算引擎 ========================

def _build_context(
    *,
    predictions: list[TeamPrediction],
    labels: list[EventLabel],
    epsilon: float,
    exclude_non_significant: bool,
    primary_oracle_horizon: Horizon,
    execution_spec: Optional[dict[str, Any]],
    allowed_event_ids: Optional[set[str]],
    event_order: Optional[list[str]],
) -> ComputeContext:
    if primary_oracle_horizon not in ALL_HORIZONS:
        primary_oracle_horizon = "t3"  # type: ignore[assignment]

    pred_by_id = {p.event_id: p for p in predictions if p.event_id}
    pairs: list[tuple[TeamPrediction, EventLabel]] = []
    for lab in labels:
        if allowed_event_ids is not None and lab.event_id not in allowed_event_ids:
            continue
        p = pred_by_id.get(lab.event_id)
        if p is not None:
            pairs.append((p, lab))
    if event_order is not None:
        event_rank = {event_id: index for index, event_id in enumerate(event_order)}
        pairs.sort(key=lambda pair: (event_rank.get(pair[1].event_id, len(event_rank)), pair[1].event_id))

    non_sig_event_ids: set[str] = set()
    if exclude_non_significant:
        for _, lab in pairs:
            pkey = f"car_{primary_oracle_horizon}_pvalue"
            p_val = getattr(lab, pkey, None)
            if p_val is None and primary_oracle_horizon == "t3":
                p_val = getattr(lab, "car_t3_pvalue", None)
            if p_val is not None and p_val > 0.10:
                non_sig_event_ids.add(lab.event_id)

    return ComputeContext(
        pairs=pairs,
        n_total=len(pairs),
        predictions=[p for p in predictions if allowed_event_ids is None or p.event_id in allowed_event_ids],
        primary_oracle_horizon=primary_oracle_horizon,
        epsilon=float(epsilon),
        exclude_non_significant=exclude_non_significant,
        non_sig_event_ids=non_sig_event_ids,
        execution_spec=dict(execution_spec or {}),
        allowed_event_ids=set(allowed_event_ids) if allowed_event_ids is not None else None,
        event_order=list(event_order) if event_order is not None else None,
    )


def compute_all_metrics(
    *,
    predictions: list[TeamPrediction],
    labels: list[EventLabel],
    epsilon: float = 0.005,
    exclude_non_significant: bool = False,
    primary_oracle_horizon: Horizon = "t3",
    enabled_metrics: Optional[Iterable[str]] = None,
    disabled_metrics: Optional[Iterable[str]] = None,
    execution_spec: Optional[dict[str, Any]] = None,
    allowed_event_ids: Optional[set[str]] = None,
    event_order: Optional[list[str]] = None,
) -> dict[str, MetricResult]:
    """计算所有（或指定的）已注册指标。

    规则：
      - tier=core 的指标永远启用
      - enabled_metrics=None → 启用全部非 custom
      - disabled_metrics 优先级最高
    """
    ctx = _build_context(
        predictions=predictions, labels=labels, epsilon=epsilon,
        exclude_non_significant=exclude_non_significant,
        primary_oracle_horizon=primary_oracle_horizon,
        execution_spec=execution_spec,
        allowed_event_ids=allowed_event_ids,
        event_order=event_order,
    )

    # 决定启用哪些 metric_id
    enabled: set[str] = set()
    for mid, info in _REGISTRY.items():
        if info["tier"] == "core":
            enabled.add(mid)
        elif enabled_metrics is None and info["tier"] != "custom":
            enabled.add(mid)
        elif enabled_metrics is not None and mid in enabled_metrics:
            enabled.add(mid)

    if disabled_metrics:
        enabled -= set(disabled_metrics)

    results: dict[str, MetricResult] = {}
    for mid in sorted(enabled):
        info = _REGISTRY[mid]
        try:
            res = info["fn"](ctx)
            # 兜底：填充显示名等（如果 Calculator 自己没填）
            if not res.display_name:
                res.display_name = info["display_name"]
            if not res.description:
                res.description = info["description"]
            if not res.tier:
                res.tier = info["tier"]
            results[mid] = res
        except Exception as exc:  # noqa: BLE001
            # 单个指标失败不影响其他
            results[mid] = MetricResult(
                value=None,
                display_name=info["display_name"],
                description=f"[计算失败] {exc}",
                tier=info["tier"],
                higher_is_better=info["higher_is_better"],
                meta={"error": str(exc)},
            )
    return results


# ======================================================== 指标定义 ============================

def _evaluate_horizon_strict(ctx: ComputeContext, h: str) -> dict:
    """对指定 horizon 计算严格口径 ACC：
    剔除 pred_abstain ∨ oracle_abstain；neutral=算错。返回 _mk_acc_stat dict。
    """
    n_s, k_s = 0, 0
    for p, lab in ctx.pairs:
        lab_h = ctx.label_of(lab, h)
        pred_abstain = bool(p.abstain)
        oracle_abstain = (not lab_h.strip())
        if ctx.exclude_non_significant and lab.event_id in ctx.non_sig_event_ids:
            oracle_abstain = True
        if pred_abstain or oracle_abstain:
            continue
        n_s += 1
        if lab_h == "neutral":
            continue
        if p.pred_direction == lab_h:
            k_s += 1
    return _mk_acc_stat(n_s, k_s)


def _evaluate_primary_non_neutral(ctx: ComputeContext, h: str) -> dict:
    """只对 primary horizon：剔除 abstain 且 oracle∈{up,down} 才计分。"""
    n_nn, k_nn = 0, 0
    for p, lab in ctx.pairs:
        lab_h = ctx.label_of(lab, h)
        if bool(p.abstain) or (not lab_h.strip()):
            continue
        if lab_h not in {"up", "down"}:
            continue
        n_nn += 1
        if p.pred_direction == lab_h:
            k_nn += 1
    return _mk_acc_stat(n_nn, k_nn)


def _evaluate_primary_directional_trades(ctx: ComputeContext, h: str) -> dict:
    """Accuracy conditional on an actual directional decision and label.

    Coverage/abstention is reported separately.  This view answers the trading
    question "when the model chose long or short, how often was the direction
    right?" without silently counting a valid neutral/no-trade as a bad trade.
    """
    n_directional, k_directional = 0, 0
    for prediction, label in ctx.pairs:
        if prediction.abstain:
            continue
        oracle_direction = ctx.label_of(label, h)
        if oracle_direction not in {"up", "down"}:
            continue
        if prediction.pred_direction not in {"up", "down"}:
            continue
        n_directional += 1
        if prediction.pred_direction == oracle_direction:
            k_directional += 1
    return _mk_acc_stat(n_directional, k_directional)


def _evaluate_primary_three_class(ctx: ComputeContext, h: str) -> dict:
    """Exact-match accuracy for up/down/neutral, excluding invalid abstentions."""
    n_valid, k_valid = 0, 0
    for prediction, label in ctx.pairs:
        if prediction.abstain:
            continue
        oracle_direction = ctx.label_of(label, h)
        if oracle_direction not in {"up", "down", "neutral"}:
            continue
        if prediction.pred_direction not in {"up", "down", "neutral"}:
            continue
        n_valid += 1
        if prediction.pred_direction == oracle_direction:
            k_valid += 1
    return _mk_acc_stat(n_valid, k_valid)


def _evaluate_primary_significant_only(ctx: ComputeContext, h: str) -> dict:
    """只对 primary horizon：pvalue<0.10 且非 abstain 且非 neutral。"""
    n_sig, k_sig = 0, 0
    for p, lab in ctx.pairs:
        if p.abstain:
            continue
        pkey = f"car_{h}_pvalue"
        p_val = getattr(lab, pkey, None)
        if h == "t3" and p_val is None:
            p_val = getattr(lab, "car_t3_pvalue", None)
        if p_val is None or p_val >= 0.10:
            continue
        lh = ctx.label_of(lab, h)
        if not lh.strip():
            continue
        if lh == "neutral":
            continue
        n_sig += 1
        if p.pred_direction == lh:
            k_sig += 1
    return _mk_acc_stat(n_sig, k_sig)


# --- CORE: 12 个 horizon 的严格 ACC（与旧 MetricsSummary 对齐） ---

def _register_acc_strict(h: Horizon, display_name: str, order: int = 100) -> None:
    """生成并注册一个 horizon 的 strict ACC 指标。"""
    @register_metric(
        f"acc_{h}_strict",
        display_name=display_name,
        description=f"{display_name}：剔除 abstain/缺失标签，neutral 留在分母并按错计",
        tier="core",
        higher_is_better=True,
    )
    def _calc(ctx: ComputeContext, _h=h) -> MetricResult:
        stat = _evaluate_horizon_strict(ctx, _h)
        return MetricResult(
            value=stat["acc"],
            display_name=display_name,
            description=f"{display_name}：剔除 abstain/缺失标签，neutral 留在分母并按错计",
            tier="core",
            higher_is_better=True,
            breakdown={"wilson": {"lo_95": stat["wilson_lo_95"], "hi_95": stat["wilson_hi_95"]}},
            meta={"n": stat["n"], "k": stat["k"], "horizon": _h, "mode": "strict"},
        )


for _h, _dn in [
    ("t1", "T1 准确率"),
    ("t3", "T3 准确率"),
    ("t5", "T5 准确率"),
    ("t7", "T7 准确率"),
    ("t15", "T15 准确率"),
    ("t30", "T30 准确率"),
    ("t60", "T60 准确率"),
    ("avg_short", "短期平均准确率"),
    ("avg_mid", "中期平均准确率"),
    ("avg_long", "长期平均准确率"),
    ("avg_all", "全周期平均准确率"),
    ("consensus66", "共识66准确率"),
]:
    _register_acc_strict(_h, _dn)  # type: ignore[arg-type]


# --- CORE: Primary horizon 专项口径 ---

@register_metric(
    "acc_primary_non_neutral",
    display_name="主口径 非中性准确率",
    description="Primary horizon：剔除 abstain 后，只在 oracle∈{up,down} 上计分（贴近实战）",
    tier="core",
    higher_is_better=True,
)
def calc_acc_primary_non_neutral(ctx: ComputeContext) -> MetricResult:
    h = ctx.primary_oracle_horizon
    stat = _evaluate_primary_non_neutral(ctx, h)
    return MetricResult(
        value=stat["acc"],
        display_name="主口径 非中性准确率",
        description=f"Primary({h}) 只在 up/down 标签上计分",
        tier="core",
        higher_is_better=True,
        breakdown={"wilson": {"lo_95": stat["wilson_lo_95"], "hi_95": stat["wilson_hi_95"]}},
        meta={"n": stat["n"], "k": stat["k"], "primary_horizon": h, "mode": "non_neutral"},
    )


@register_metric(
    "acc_primary_directional_trade",
    display_name="方向交易准确率",
    description="Primary horizon：仅统计模型实际给出 up/down 且 Oracle 也为 up/down 的样本",
    tier="core",
    higher_is_better=True,
)
def calc_acc_primary_directional_trade(ctx: ComputeContext) -> MetricResult:
    h = ctx.primary_oracle_horizon
    stat = _evaluate_primary_directional_trades(ctx, h)
    return MetricResult(
        value=stat["acc"] if stat["n"] > 0 else None,
        display_name="方向交易准确率",
        description=f"Primary({h}) 在实际 up/down 决策上的命中率；中性与弃权不进入分母",
        tier="core",
        higher_is_better=True,
        breakdown={"wilson": {"lo_95": stat["wilson_lo_95"], "hi_95": stat["wilson_hi_95"]}},
        meta={"n": stat["n"], "k": stat["k"], "primary_horizon": h, "mode": "directional_trade"},
    )


@register_metric(
    "acc_primary_three_class",
    display_name="三分类准确率",
    description="Primary horizon：up/down/neutral 三分类完全一致；输出无效与弃权不计分",
    tier="core",
    higher_is_better=True,
)
def calc_acc_primary_three_class(ctx: ComputeContext) -> MetricResult:
    h = ctx.primary_oracle_horizon
    stat = _evaluate_primary_three_class(ctx, h)
    return MetricResult(
        value=stat["acc"] if stat["n"] > 0 else None,
        display_name="三分类准确率",
        description=f"Primary({h}) 的 up/down/neutral 完全匹配率；弃权不进入分母",
        tier="core",
        higher_is_better=True,
        breakdown={"wilson": {"lo_95": stat["wilson_lo_95"], "hi_95": stat["wilson_hi_95"]}},
        meta={"n": stat["n"], "k": stat["k"], "primary_horizon": h, "mode": "three_class"},
    )


@register_metric(
    "acc_primary_significant_only",
    display_name="主口径 显著事件准确率",
    description="Primary horizon：仅 oracle pvalue<0.10 的显著事件计分（剔除噪声事件）",
    tier="core",
    higher_is_better=True,
)
def calc_acc_primary_significant_only(ctx: ComputeContext) -> MetricResult:
    h = ctx.primary_oracle_horizon
    stat = _evaluate_primary_significant_only(ctx, h)
    return MetricResult(
        value=stat["acc"],
        display_name="主口径 显著事件准确率",
        description=f"Primary({h}) 仅 car pvalue<0.10 的事件",
        tier="core",
        higher_is_better=True,
        breakdown={"wilson": {"lo_95": stat["wilson_lo_95"], "hi_95": stat["wilson_hi_95"]}},
        meta={"n": stat["n"], "k": stat["k"], "primary_horizon": h, "mode": "significant_only"},
    )


# --- EXTENDED: 覆盖度 / 弃权率 ---

@register_metric(
    "coverage_rate",
    display_name="预测覆盖率",
    description="(pred ∩ labels 且 非 abstain) / 总 labels 数；越高代表越敢给结论",
    tier="extended",
    higher_is_better=True,
)
def calc_coverage_rate(ctx: ComputeContext) -> MetricResult:
    n_labels = len({lab.event_id for _, lab in ctx.pairs})
    n_covered = sum(1 for p, _ in ctx.pairs if not p.abstain)
    rate = (n_covered / n_labels) if n_labels > 0 else 0.0
    return MetricResult(
        value=rate,
        display_name="预测覆盖率",
        description="非 abstain 的预测数 / 总事件数",
        tier="extended",
        higher_is_better=True,
        meta={"n_total": n_labels, "n_covered": n_covered, "n_abstain": n_labels - n_covered},
    )


@register_metric(
    "abstain_rate",
    display_name="弃权率",
    description="pred_abstain=True 的比例；越低越好（但过低可能导致 accuracy 下降）",
    tier="extended",
    higher_is_better=False,
)
def calc_abstain_rate(ctx: ComputeContext) -> MetricResult:
    n = len(ctx.pairs)
    k = sum(1 for p, _ in ctx.pairs if p.abstain)
    rate = (k / n) if n > 0 else 0.0
    return MetricResult(
        value=rate,
        display_name="弃权率",
        description="abstain / 总配对数",
        tier="extended",
        higher_is_better=False,
        meta={"n": n, "abstain": k},
    )


# --- EXTENDED: 校准度（Confidence Calibration）---

@register_metric(
    "calibration_mse",
    display_name="置信度校准 MSE",
    description="(confidence - actual_accuracy)^2 的均值；越小代表置信度越准",
    tier="extended",
    higher_is_better=False,
)
def calc_calibration_mse(ctx: ComputeContext) -> MetricResult:
    """简单的置信度校准：
    将 prediction 按 confidence 分桶（如果有的话），每桶 (实际正确率 - avg_confidence)^2。
    无 confidence 时返回 None。
    """
    buckets: dict[int, list[int]] = {}  # bucket_key(0..9) → [1=correct, 0=wrong]
    h = ctx.primary_oracle_horizon
    n_valid = 0
    sq_sum = 0.0
    confidences: list[float] = []
    actuals: list[int] = []
    for p, lab in ctx.pairs:
        if p.abstain or p.confidence is None:
            continue
        lab_h = ctx.label_of(lab, h)
        if not lab_h.strip() or lab_h == "neutral":
            continue
        actual = 1 if (p.pred_direction == lab_h) else 0
        conf = max(0.0, min(1.0, float(p.confidence)))
        confidences.append(conf)
        actuals.append(actual)
        n_valid += 1
        sq_sum += (conf - actual) ** 2
        bk = int(conf * 10)
        if bk > 9:
            bk = 9
        buckets.setdefault(bk, []).append(actual)

    if n_valid == 0:
        return MetricResult(
            value=None,
            display_name="置信度校准 MSE",
            description="无 confidence 数据或无有效样本",
            tier="extended",
            higher_is_better=False,
            meta={"n_valid": 0},
        )

    mse = sq_sum / n_valid
    # 分桶详情
    bucket_stats = {}
    for bk in range(10):
        arr = buckets.get(bk, [])
        if arr:
            bucket_stats[f"{bk*10}-{(bk+1)*10}%"] = {
                "n": len(arr),
                "acc": sum(arr) / len(arr),
                "confidence_mid": (bk + 0.5) * 0.1,
            }
    return MetricResult(
        value=float(f"{mse:.6f}"),
        display_name="置信度校准 MSE",
        description="(confidence - actual_accuracy)^2 均值",
        tier="extended",
        higher_is_better=False,
        breakdown={"buckets": bucket_stats},
        meta={"n_valid": n_valid},
    )


# --- EXTENDED: 方向偏差（是否系统性偏向 up/down）---

@register_metric(
    "direction_bias",
    display_name="方向偏差",
    description="预测 up 的比例 vs oracle up 的比例；绝对值越大代表系统偏差越严重",
    tier="extended",
    higher_is_better=False,
)
def calc_direction_bias(ctx: ComputeContext) -> MetricResult:
    h = ctx.primary_oracle_horizon
    preds_up, preds_down, pred_total = 0, 0, 0
    oracle_up, oracle_down, oracle_total = 0, 0, 0
    for p, lab in ctx.pairs:
        if not p.abstain:
            pred_total += 1
            if p.pred_direction == "up":
                preds_up += 1
            elif p.pred_direction == "down":
                preds_down += 1
        lab_h = ctx.label_of(lab, h)
        if lab_h in {"up", "down"}:
            oracle_total += 1
            if lab_h == "up":
                oracle_up += 1
            elif lab_h == "down":
                oracle_down += 1
    pred_up_ratio = (preds_up / pred_total) if pred_total > 0 else 0.0
    oracle_up_ratio = (oracle_up / oracle_total) if oracle_total > 0 else 0.0
    bias = pred_up_ratio - oracle_up_ratio
    return MetricResult(
        value=float(f"{abs(bias):.6f}"),
        display_name="方向偏差",
        description="|pred_up_ratio - oracle_up_ratio|",
        tier="extended",
        higher_is_better=False,
        breakdown={
            "pred_up_ratio": pred_up_ratio,
            "oracle_up_ratio": oracle_up_ratio,
            "signed_bias": float(f"{bias:.6f}"),
        },
        meta={
            "pred_total": pred_total, "preds_up": preds_up, "preds_down": preds_down,
            "oracle_total": oracle_total, "oracle_up": oracle_up, "oracle_down": oracle_down,
        },
    )


# --- EXTENDED: 平均 Oracle CAR（用于判断数据集本身的"可预测性"）---

@register_metric(
    "avg_car_primary",
    display_name="主窗口平均 CAR",
    description="Primary horizon 上所有有效标签的平均累计异常收益率；参考基准",
    tier="extended",
    higher_is_better=True,
)
def calc_avg_car_primary(ctx: ComputeContext) -> MetricResult:
    h = ctx.primary_oracle_horizon
    cars: list[float] = []
    for _, lab in ctx.pairs:
        c = getattr(lab, f"car_{h}", None)
        lab_h = ctx.label_of(lab, h)
        if (lab_h or "").strip() and isinstance(c, (int, float)):
            cars.append(float(c))
    avg = (sum(cars) / len(cars)) if cars else None
    return MetricResult(
        value=float(f"{avg:.6f}") if avg is not None else None,
        display_name="主窗口平均 CAR",
        description=f"Primary({h}) 有效标签平均累计异常收益率",
        tier="extended",
        higher_is_better=True,
        meta={"n": len(cars), "horizon": h, "min": min(cars) if cars else None, "max": max(cars) if cars else None},
    )


# --- EXTENDED: 分组准确率（按 market / type）---

@register_metric(
    "acc_by_market",
    display_name="按市场分组准确率",
    description="Primary horizon 严格口径，按 CN/US/HK 等市场分组的准确率",
    tier="extended",
    higher_is_better=True,
)
def calc_acc_by_market(ctx: ComputeContext) -> MetricResult:
    h = ctx.primary_oracle_horizon
    grouped: dict[str, list[tuple[TeamPrediction, EventLabel]]] = {}
    for p, lab in ctx.pairs:
        if p.abstain:
            continue
        k = (lab.market or "UNK")
        grouped.setdefault(k, []).append((p, lab))
    breakdown: dict[str, Any] = {}
    for k, items in grouped.items():
        n_s, k_s = 0, 0
        for p, lab in items:
            lab_h = ctx.label_of(lab, h)
            if not lab_h.strip():
                continue
            n_s += 1
            if lab_h == "neutral":
                continue
            if p.pred_direction == lab_h:
                k_s += 1
        breakdown[k] = _mk_acc_stat(n_s, k_s)
    # 主值 = 各组 n 加权平均 acc
    total_n = sum(v["n"] for v in breakdown.values())
    weighted_avg = (
        sum(v["acc"] * v["n"] for v in breakdown.values()) / total_n
        if total_n > 0 else 0.0
    )
    return MetricResult(
        value=float(f"{weighted_avg:.6f}"),
        display_name="按市场分组准确率",
        description=f"Primary({h}) 按市场分组严格口径",
        tier="extended",
        higher_is_better=True,
        breakdown=breakdown,
        meta={"n_groups": len(breakdown), "total_n": total_n, "horizon": h},
    )


@register_metric(
    "acc_by_type",
    display_name="按事件类型分组准确率",
    description="Primary horizon 严格口径，按 event_type_l2 分组的准确率",
    tier="extended",
    higher_is_better=True,
)
def calc_acc_by_type(ctx: ComputeContext) -> MetricResult:
    h = ctx.primary_oracle_horizon
    grouped: dict[str, list[tuple[TeamPrediction, EventLabel]]] = {}
    for p, lab in ctx.pairs:
        if p.abstain:
            continue
        k = (lab.event_type_l2 or "UNK")
        grouped.setdefault(k, []).append((p, lab))
    breakdown: dict[str, Any] = {}
    for k, items in grouped.items():
        n_s, k_s = 0, 0
        for p, lab in items:
            lab_h = ctx.label_of(lab, h)
            if not lab_h.strip():
                continue
            n_s += 1
            if lab_h == "neutral":
                continue
            if p.pred_direction == lab_h:
                k_s += 1
        breakdown[k] = _mk_acc_stat(n_s, k_s)
    total_n = sum(v["n"] for v in breakdown.values())
    weighted_avg = (
        sum(v["acc"] * v["n"] for v in breakdown.values()) / total_n
        if total_n > 0 else 0.0
    )
    return MetricResult(
        value=float(f"{weighted_avg:.6f}"),
        display_name="按事件类型分组准确率",
        description=f"Primary({h}) 按 event_type_l2 分组严格口径",
        tier="extended",
        higher_is_better=True,
        breakdown=breakdown,
        meta={"n_groups": len(breakdown), "total_n": total_n, "horizon": h},
    )


# --- EXTENDED: Top-box / Bottom-box 置信度表现 ---

@register_metric(
    "acc_high_confidence",
    display_name="高置信度准确率",
    description="confidence ≥ 0.8 的样本准确率；越高代表高置信区间越可靠",
    tier="extended",
    higher_is_better=True,
)
def calc_acc_high_confidence(ctx: ComputeContext) -> MetricResult:
    h = ctx.primary_oracle_horizon
    n_s, k_s = 0, 0
    for p, lab in ctx.pairs:
        if p.abstain or p.confidence is None or p.confidence < 0.8:
            continue
        lab_h = ctx.label_of(lab, h)
        if not lab_h.strip():
            continue
        n_s += 1
        if lab_h == "neutral":
            continue
        if p.pred_direction == lab_h:
            k_s += 1
    stat = _mk_acc_stat(n_s, k_s)
    return MetricResult(
        value=stat["acc"],
        display_name="高置信度准确率",
        description="confidence ≥ 0.8 的严格准确率",
        tier="extended",
        higher_is_better=True,
        breakdown={"wilson": {"lo_95": stat["wilson_lo_95"], "hi_95": stat["wilson_hi_95"]}},
        meta={"n": stat["n"], "k": stat["k"], "threshold": 0.8, "horizon": h},
    )


# --- EXTENDED: 与 Prior 方向一致度 ---

@register_metric(
    "prior_alignment_rate",
    display_name="先验方向一致率",
    description="pred_direction 与 direction_prior 相同的比例；高代表模型会独立思考，低代表盲从先验（需结合准确率看）",
    tier="extended",
    higher_is_better=True,  # 非单调：过高代表不思考，过低代表不尊重先验
)
def calc_prior_alignment_rate(ctx: ComputeContext) -> MetricResult:
    n, k = 0, 0
    for p, _ in ctx.pairs:
        if p.abstain:
            continue
        # direction_prior 在 prediction 里没有，是 events 里的字段；
        # 这里从 ctx.pairs 无法直接取到，因此需要通过 run 的 events 文件。
        # 为了不让指标卡住，这里只统计那些已经"在 p 或 lab 上暴露了 prior"的情况：
        prior = getattr(p, "direction_prior", None)
        if prior is None:
            # 从 lab 上试试（EventLabel 没有 direction_prior，但兼容一些扩展字段）
            continue
        n += 1
        if str(prior) == str(p.pred_direction):
            k += 1
    if n == 0:
        return MetricResult(
            value=None,
            display_name="先验方向一致率",
            description="本回测无 direction_prior 数据",
            tier="extended",
            higher_is_better=True,
            meta={"n_valid": 0},
        )
    rate = k / n
    return MetricResult(
        value=rate,
        display_name="先验方向一致率",
        description="pred == direction_prior 的比例",
        tier="extended",
        higher_is_better=True,
        meta={"n": n, "aligned": k},
    )


# --- EXTENDED: event-decision investment performance proxies ---

def _event_trade_proxy_returns(ctx: ComputeContext, *, gross: bool = False) -> list[float]:
    """Approximate one equal-notional trade per label in label-file order.

    This intentionally is not presented as a portfolio backtest: overlapping
    positions, capital constraints and fills are not modelled. The run's explicit
    fee_bps + slippage_bps is interpreted as one all-in round-trip amount and
    subtracted once per active event. No entry/exit order legs are fabricated.
    """
    from .evaluation_protocol import event_proxy_cost_spec

    cost = event_proxy_cost_spec(ctx.execution_spec)["round_trip_cost_rate"]
    returns: list[float] = []
    car_key = f"car_{ctx.primary_oracle_horizon}"
    ordered_pairs = list(ctx.pairs) if ctx.event_order is not None else sorted(
        ctx.pairs,
        key=lambda pair: (str(getattr(pair[1], "event_time", "") or ""), pair[1].event_id),
    )
    for prediction, label in ordered_pairs:
        car = getattr(label, car_key, None)
        if car is None and ctx.primary_oracle_horizon == "t3":
            car = getattr(label, "car_t3", None)
        label_value = ctx.label_of(label, ctx.primary_oracle_horizon)
        if car is None or not isinstance(car, (int, float)) or not math.isfinite(float(car)):
            continue
        if label_value not in {"up", "down", "neutral"}:
            continue
        direction = str(prediction.pred_direction or "")
        if prediction.abstain or direction == "neutral":
            continue
        elif direction == "up":
            trade_return = float(car)
        elif direction == "down":
            trade_return = -float(car)
        else:
            continue
        if not gross:
            trade_return -= cost
        # Keep the proxy equity curve defined for extreme but finite CAR inputs.
        returns.append(max(-0.999999, trade_return))
    return returns


def _event_trade_proxy_meta(ctx: ComputeContext, count: int) -> dict[str, Any]:
    from .evaluation_protocol import event_proxy_cost_spec

    costs = event_proxy_cost_spec(ctx.execution_spec)
    return {
        "n_event_trades": count,
        "horizon": ctx.primary_oracle_horizon,
        "method": "event_trade_proxy",
        "ordering": "event_time_then_event_id",
        "positioning": "equal_notional_one_trade_per_event",
        "fee_bps": costs["fee_bps"],
        "slippage_bps": costs["slippage_bps"],
        "round_trip_cost_bps": costs["round_trip_cost_bps"],
        "total_cost_rate_sum": count * costs["round_trip_cost_rate"],
        "cost_application": "fee_bps + slippage_bps as one all-in round-trip event-window proxy charge",
        "date_filter_applied": ctx.allowed_event_ids is not None,
        "evaluated_event_universe_size": len(ctx.allowed_event_ids) if ctx.allowed_event_ids is not None else None,
        "disclaimer": "事件逐笔代理指标；成本是每个事件窗口的一次全程往返假设，不是逐订单费用，也不是包含资金占用、重叠持仓与真实成交撮合的完整组合回测。",
    }


@register_metric(
    "strategy_total_return",
    display_name="策略累计收益",
    description="累计收益；具体为事件代理或 bar 组合模拟，以结果 meta.method 为准",
    tier="extended",
    higher_is_better=True,
)
def calc_strategy_total_return(ctx: ComputeContext) -> MetricResult:
    returns = _event_trade_proxy_returns(ctx)
    gross_returns = _event_trade_proxy_returns(ctx, gross=True)
    if not returns:
        value = None
    else:
        equity = 1.0
        for item in returns:
            equity *= 1.0 + item
        value = equity - 1.0
    meta = _event_trade_proxy_meta(ctx, len(returns))
    if gross_returns:
        gross_equity = 1.0
        for item in gross_returns:
            gross_equity *= 1.0 + item
        meta["gross_total_return"] = gross_equity - 1.0
    else:
        meta["gross_total_return"] = None
    return MetricResult(
        value=value,
        display_name="策略累计收益",
        description="方向调整后的事件 CAR 按标签顺序逐笔复利",
        tier="extended",
        higher_is_better=True,
        meta=meta,
    )


@register_metric(
    "strategy_max_drawdown",
    display_name="最大回撤",
    description="净值曲线峰谷最大跌幅；具体引擎口径见结果 meta.method",
    tier="extended",
    higher_is_better=False,
)
def calc_strategy_max_drawdown(ctx: ComputeContext) -> MetricResult:
    returns = _event_trade_proxy_returns(ctx)
    max_drawdown: float | None = None
    if returns:
        equity = peak = 1.0
        max_drawdown = 0.0
        for item in returns:
            equity *= 1.0 + item
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 0.0)
    return MetricResult(
        value=max_drawdown,
        display_name="最大回撤",
        description="方向调整后的事件 CAR 代理净值最大回撤",
        tier="extended",
        higher_is_better=False,
        meta=_event_trade_proxy_meta(ctx, len(returns)),
    )


@register_metric(
    "strategy_sharpe_proxy",
    display_name="Sharpe / 收益信噪比",
    description="事件代理为非年化逐笔信噪比；portfolio 为 bar 频率年化 Sharpe，见 meta.method",
    tier="extended",
    higher_is_better=True,
)
def calc_strategy_sharpe_proxy(ctx: ComputeContext) -> MetricResult:
    returns = _event_trade_proxy_returns(ctx)
    value: float | None = None
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
        if variance > 0:
            value = mean / math.sqrt(variance) * math.sqrt(len(returns))
    return MetricResult(
        value=value,
        display_name="Sharpe / 收益信噪比",
        description="非年化事件收益信噪比，仅用于相同协议 Run 横向比较",
        tier="extended",
        higher_is_better=True,
        meta=_event_trade_proxy_meta(ctx, len(returns)),
    )


@register_metric(
    "strategy_win_rate",
    display_name="正收益观察占比",
    description="事件代理为正收益事件占比；portfolio 为活跃 bar 正收益占比，不冒充配对交易胜率",
    tier="extended",
    higher_is_better=True,
)
def calc_strategy_win_rate(ctx: ComputeContext) -> MetricResult:
    returns = _event_trade_proxy_returns(ctx)
    active = list(returns)
    value = (sum(1 for item in active if item > 0) / len(active)) if active else None
    meta = _event_trade_proxy_meta(ctx, len(returns))
    meta.update({"n_active": len(active), "n_wins": sum(1 for item in active if item > 0)})
    return MetricResult(
        value=value,
        display_name="正收益观察占比",
        description="方向调整后的非零事件 CAR 胜率",
        tier="extended",
        higher_is_better=True,
        meta=meta,
    )


@register_metric(
    "return_forecast",
    display_name="收益率预测误差",
    description="未来 T+N 标的自身收益率数值预测；MAE、RMSE 与偏差以百分点计",
    tier="core",
    higher_is_better=False,
)
def calc_return_forecast(ctx: ComputeContext) -> MetricResult:
    from .return_forecast import compute_return_forecast

    summary = compute_return_forecast(
        ctx.predictions or [prediction for prediction, _ in ctx.pairs],
        [label for _, label in ctx.pairs],
        horizon=ctx.primary_oracle_horizon,
    )
    return MetricResult(
        value=summary["mae_pct"], display_name="收益率预测误差",
        description="预测减实际收益率；缺少显式数值预测的历史记录不计为零预测",
        tier="core", higher_is_better=False, meta=summary,
    )
