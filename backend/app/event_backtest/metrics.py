from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .models import EventLabel, Label, TeamPrediction, Horizon, ALL_HORIZONS


def _wilson_lower(n: int, k: int, *, z: float = 1.959963984540054) -> float:
    """Wilson 95% CI lower bound (n=样本数, k=正确数). n=0 → 0"""
    if n <= 0: return 0.0
    if k < 0: k = 0
    if k > n: k = n
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    lo = center - margin
    return 0.0 if lo < 0 else (1.0 if lo > 1 else lo)


def _wilson_upper(n: int, k: int, *, z: float = 1.959963984540054) -> float:
    if n <= 0: return 1.0
    if k < 0: k = 0
    if k > n: k = n
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    hi = center + margin
    return 1.0 if hi > 1 else (0.0 if hi < 0 else hi)


@dataclass(frozen=True)
class AccWithWilson:
    """单一口径的 ACC + Wilson 95% CI。n=分母数, k=正确数, acc=k/n, lo/hi=Wilson 95% 上下限"""
    n: int
    k: int
    acc: float
    wilson_lo_95: float
    wilson_hi_95: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "k": self.k, "acc": float(f"{self.acc:.6f}"),
            "wilson_lo_95": float(f"{self.wilson_lo_95:.6f}"),
            "wilson_hi_95": float(f"{self.wilson_hi_95:.6f}"),
        }


@dataclass(frozen=True)
class MetricsSummary:
    epsilon: float
    n_total: int                              # pairs 总数（pred∩labels）
    n_abstain_pred: int                       # 预测器 abstain=True 计数
    n_abstain_oracle: int                     # primary oracle horizon label 为空计数
    n_non_significant: int = 0                # 因 primary car_pvalue>0.10 被剔除的标签数（exclude_non_significant=True 时）
    primary_oracle_horizon: str = "t3"        # 用作主证据的 horizon（label_avg_all / t3 / ...）
    # 主口径：剔除 (pred_abstain ∨ oracle_abstain)；neutral=算错（最严格，设计文档 §5 默认口径）
    # 为所有 12 个 horizons 都计算 strict（主+参考指标）
    acc_t1_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_t3_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_t5_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_t7_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_t15_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_t30_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_t60_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_avg_short_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_avg_mid_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_avg_long_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_avg_all_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_consensus66_strict: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    # 次口径：针对 primary oracle horizon —— 剔除 abstain 且 oracle∈{up,down} 才计分（更贴近实战使用）
    acc_primary_non_neutral: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    # 兼容旧字段：acc_t3_non_neutral / acc_t3_significant_only
    acc_t3_non_neutral: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    # 仅 primary horizon car_pvalue<0.10 的显著事件口径
    acc_primary_significant_only: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    acc_t3_significant_only: AccWithWilson = field(default_factory=lambda: AccWithWilson(0,0,0.0,0.0,1.0))
    # 保留老字段（向后兼容）：严格口径的 acc 值（前三个基础 horizons）
    acc_t1: float = 0.0
    acc_t3: float = 0.0
    acc_t5: float = 0.0
    acc_t7: float = 0.0
    acc_t15: float = 0.0
    acc_t30: float = 0.0
    acc_t60: float = 0.0
    acc_avg_all: float = 0.0    # 重点字段：avgCAR(all) 口径严格 ACC
    acc_consensus66: float = 0.0
    avg_car_t1: float = 0.0
    avg_car_t3: float = 0.0
    avg_car_t5: float = 0.0
    avg_car_avg_all: float = 0.0
    acc_by_market: dict[str, dict[str, Any]] = field(default_factory=dict)
    acc_by_type: dict[str, dict[str, Any]] = field(default_factory=dict)
    return_forecast: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # 把顶层的 AccWithWilson 对象展开成纯 dict（含 float 格式化）
        for k in list(d.keys()):
            v = d[k]
            if isinstance(v, AccWithWilson):
                d[k] = v.to_dict()
        # 处理 group dicts (acc_by_market / acc_by_type) 中嵌套的 AccWithWilson：
        # asdict 已把这些递归转成 plain dict，这里从 self 取原始对象调用 to_dict()
        # 以应用统一的 float 格式化
        for group_field in ("acc_by_market", "acc_by_type"):
            original = getattr(self, group_field, None)
            if not isinstance(original, dict):
                continue
            new_group: dict[str, Any] = {}
            for g_k, g_v in original.items():
                if isinstance(g_v, dict):
                    new_inner: dict[str, Any] = {}
                    for i_k, i_v in g_v.items():
                        new_inner[i_k] = i_v.to_dict() if isinstance(i_v, AccWithWilson) else i_v
                    new_group[g_k] = new_inner
                else:
                    new_group[g_k] = g_v
            d[group_field] = new_group
        return d


def label_from_car(car: float, *, epsilon: float) -> Label:
    if car > epsilon:
        return "up"
    if car < -epsilon:
        return "down"
    return "neutral"


def _mk_wilson(n: int, k: int) -> AccWithWilson:
    acc = (k / n) if n > 0 else 0.0
    return AccWithWilson(n=n, k=k, acc=acc, wilson_lo_95=_wilson_lower(n,k), wilson_hi_95=_wilson_upper(n,k))


def compute_metrics(
    *,
    predictions: list[TeamPrediction],
    labels: list[EventLabel],
    epsilon: float = 0.005,
    exclude_non_significant: bool = False,
    primary_oracle_horizon: str = "t3",
) -> MetricsSummary:
    """
    primary_oracle_horizon: 用作"主证据方向"的标签字段（默认 t3，可改为 "avg_all"）。
        这个字段用来：
          ① 决定 oracle_abstain（缺失=弃权）；
          ② 计算 non_neutral / significant_only 两个实战口径；
          ③ 作为 n_abstain_oracle / n_non_significant 的统计基础。
        其他所有 horizons（t3/t7/.../consensus66）都会同时计算 strict ACC，
        用作"多时间窗口参考指标"。
    """
    # Validate primary_oracle_horizon is a valid Horizon
    if primary_oracle_horizon not in ALL_HORIZONS:
        primary_oracle_horizon = "t3"

    pred_by_id = {p.event_id: p for p in predictions if p.event_id}
    from .return_forecast import compute_return_forecast
    return_forecast = compute_return_forecast(predictions, labels, horizon=primary_oracle_horizon)
    pairs: list[tuple[TeamPrediction, EventLabel]] = []
    for lab in labels:
        p = pred_by_id.get(lab.event_id)
        if p is None:
            continue
        pairs.append((p, lab))

    n_total = len(pairs)
    n_abstain_pred = sum(1 for p, _ in pairs if p.abstain)

    def _label_of(lab: EventLabel, h: str) -> Label:
        direction = str(getattr(lab, f"label_{h}", "") or "").strip().lower()
        if direction not in {"up", "down", "neutral"}:
            return ""
        # A direction string without the matching realized return is not an
        # Oracle outcome. Counting it would make missing market data look like
        # scored accuracy. consensus66 has no dedicated CAR field, so require
        # at least one genuine component horizon instead.
        if h == "consensus66":
            component_cars = [getattr(lab, f"car_{name}", None) for name in ("t3", "t7", "t15", "t30", "t60")]
            if not any(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) for value in component_cars):
                return ""
        else:
            car = getattr(lab, f"car_{h}", None)
            if not isinstance(car, (int, float)) or isinstance(car, bool) or not math.isfinite(float(car)):
                return ""
        return direction  # type: ignore[return-value]

    n_abstain_oracle = sum(1 for _, lab in pairs if not _label_of(lab, primary_oracle_horizon).strip())

    # 显著性过滤：只对 primary horizon 检查 p 值（如果该 horizon 有 pvalue 字段）
    def _is_non_significant(lab: EventLabel) -> bool:
        # primary horizon car_pvalue key: car_{h}_pvalue
        pkey = f"car_{primary_oracle_horizon}_pvalue"
        p_val = getattr(lab, pkey, None)
        if p_val is None and primary_oracle_horizon == "t3":
            # backward-compat fallback
            p_val = getattr(lab, "car_t3_pvalue", None)
        return p_val is not None and p_val > 0.10

    non_sig_event_ids: set[str] = set()
    if exclude_non_significant:
        for p, lab in pairs:
            if _is_non_significant(lab):
                non_sig_event_ids.add(lab.event_id)
    n_non_significant = len(non_sig_event_ids)

    def _evaluate(h: str):
        """Return strict/non-neutral/significant stats for one horizon.

        Older code only populated the latter two when ``h`` was the primary
        horizon. That made the backwards-compatible T3 fields empty as soon as a
        user selected T5/T15/etc. Computing each horizon independently preserves
        both the selected evaluation and the explicit T3 compatibility fields.
        """
        rows = []
        for p, lab in pairs:
            lab_h: Label = _label_of(lab, h)
            pred_abstain = bool(p.abstain)
            oracle_abstain = (not lab_h.strip())
            if exclude_non_significant and lab.event_id in non_sig_event_ids:
                oracle_abstain = True
            rows.append((p, lab_h, pred_abstain, oracle_abstain))

        n_s = 0; k_s = 0
        for p, lab_h, pa, oa in rows:
            if pa or oa: continue
            n_s += 1
            if lab_h == "neutral": continue
            if p.pred_direction == lab_h: k_s += 1
        strict = _mk_wilson(n_s, k_s)

        n_nn = 0; k_nn = 0
        for p, lab_h, pa, oa in rows:
            if pa or oa: continue
            if lab_h not in {"up","down"}: continue
            n_nn += 1
            if p.pred_direction == lab_h: k_nn += 1
        non_neutral: AccWithWilson | None = _mk_wilson(n_nn, k_nn)

        n_sig = 0; k_sig = 0
        for p, lab in pairs:
            if p.abstain: continue
            pkey = f"car_{h}_pvalue"
            p_val = getattr(lab, pkey, None)
            if h == "t3" and p_val is None:
                p_val = getattr(lab, "car_t3_pvalue", None)
            if p_val is None or p_val >= 0.10: continue
            lh = _label_of(lab, h)
            if not lh.strip(): continue
            n_sig += 1
            if lh == "neutral": continue
            if p.pred_direction == lh: k_sig += 1
        significant_only: AccWithWilson | None = _mk_wilson(n_sig, k_sig)
        return strict, non_neutral, significant_only

    # Evaluate all 12 horizons
    results: dict[str, tuple[AccWithWilson, AccWithWilson | None, AccWithWilson | None]] = {}
    for h in ALL_HORIZONS:
        results[h] = _evaluate(h)

    def _strict(h: str) -> AccWithWilson: return results[h][0]
    def _strict_acc(h: str) -> float: return _strict(h).acc

    # Primary-specific special values
    primary_nn, primary_sig = results[primary_oracle_horizon][1], results[primary_oracle_horizon][2]
    empty = _mk_wilson(0, 0)
    primary_nn = primary_nn or empty
    primary_sig = primary_sig or empty
    # Compat fields
    t3_nn = results["t3"][1] or empty
    t3_sig = results["t3"][2] or empty

    # avg cars (reference values)
    def car_for(h: str) -> list[float]:
        out = []
        for _, lab in pairs:
            c = getattr(lab, f"car_{h}", None)
            lab_h = _label_of(lab, h)
            if (lab_h or "").strip() and isinstance(c, (int, float)):
                out.append(float(c))
        return out

    avg_car_t1 = sum(car_for("t1")) / len(car_for("t1")) if car_for("t1") else 0.0
    avg_car_t3 = sum(car_for("t3")) / len(car_for("t3")) if car_for("t3") else 0.0
    avg_car_t5 = sum(car_for("t5")) / len(car_for("t5")) if car_for("t5") else 0.0
    avg_car_avg_all_list = car_for("avg_all")
    avg_car_avg_all = sum(avg_car_avg_all_list) / len(avg_car_avg_all_list) if avg_car_avg_all_list else 0.0

    def group_key_market(lab: EventLabel) -> str:
        return lab.market or "UNK"

    def group_key_type(lab: EventLabel) -> str:
        return lab.event_type_l2 or "UNK"

    # group 级 ACC：对每个分组，输出 t3 / t7 / t15 / t30 / avg_all / consensus66 的 strict
    GROUP_HS: tuple[str, ...] = ("t3", "t7", "t15", "t30", "avg_all", "consensus66")
    def group_acc(key_fn) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[tuple[TeamPrediction, EventLabel]]] = {}
        for p, lab in pairs:
            if p.abstain: continue
            k = key_fn(lab)
            grouped.setdefault(k, []).append((p, lab))
        out: dict[str, dict[str, Any]] = {}
        for k, items in grouped.items():
            per: dict[str, Any] = {"n": int(len(items))}
            for h in GROUP_HS:
                n_s, k_s = 0, 0
                for p, lab in items:
                    lab_h = _label_of(lab, h)
                    if not lab_h.strip(): continue
                    n_s += 1
                    if lab_h == "neutral": continue
                    if p.pred_direction == lab_h: k_s += 1
                per[f"acc_{h}"] = _mk_wilson(n_s, k_s)
            out[k] = per
        return out

    return MetricsSummary(
        epsilon=float(epsilon),
        n_total=n_total,
        n_abstain_pred=n_abstain_pred,
        n_abstain_oracle=n_abstain_oracle,
        n_non_significant=n_non_significant,
        primary_oracle_horizon=primary_oracle_horizon,
        acc_t1_strict=_strict("t1"),
        acc_t3_strict=_strict("t3"),
        acc_t5_strict=_strict("t5"),
        acc_t7_strict=_strict("t7"),
        acc_t15_strict=_strict("t15"),
        acc_t30_strict=_strict("t30"),
        acc_t60_strict=_strict("t60"),
        acc_avg_short_strict=_strict("avg_short"),
        acc_avg_mid_strict=_strict("avg_mid"),
        acc_avg_long_strict=_strict("avg_long"),
        acc_avg_all_strict=_strict("avg_all"),
        acc_consensus66_strict=_strict("consensus66"),
        acc_primary_non_neutral=primary_nn,
        acc_t3_non_neutral=t3_nn,
        acc_primary_significant_only=primary_sig,
        acc_t3_significant_only=t3_sig,
        # Backward compat: top-level .acc_t* floats (same values as strict.acc)
        acc_t1=_strict_acc("t1"),
        acc_t3=_strict_acc("t3"),
        acc_t5=_strict_acc("t5"),
        acc_t7=_strict_acc("t7"),
        acc_t15=_strict_acc("t15"),
        acc_t30=_strict_acc("t30"),
        acc_t60=_strict_acc("t60"),
        acc_avg_all=_strict_acc("avg_all"),
        acc_consensus66=_strict_acc("consensus66"),
        avg_car_t1=avg_car_t1,
        avg_car_t3=avg_car_t3,
        avg_car_t5=avg_car_t5,
        avg_car_avg_all=avg_car_avg_all,
        acc_by_market=group_acc(group_key_market),
        acc_by_type=group_acc(group_key_type),
        return_forecast=return_forecast,
    )
