"""eval_rlvr_vs_baseline.py — Pronoia-RLVR §5 辩证多口径评估（七大类 + 四基线 A/B + Wilson 区间）。

输入：
  · events + labels（评估集 1000 条）
  · 模型 predictions.jsonl（每个 event 输出 direction + confidence + completion + diag）
  · 可选：多个基线 predictions（--baseline NAME=path）

七大类指标（§5.1）：
  ① 定向 primary strict ACC    → label_t{primary_h} 匹配
  ② avg_all strict ACC          → label_avg_all 匹配（和 PRONOIA 旧口径对齐，便于环比）
  ③ 双窗一致率                  → direction=primary 且 direction=secondary[0]
  ④ 量价一致率                  → vol_regime 桶内 ACC 分布（HIGH/NORMAL/LOW 三桶）
  ⑤ MoE 健康度                  → Router 权重 entropy / active experts 分布
  ⑥ RET 独立面板（事件后标的自身收益，5 horizons）→ ACC(vs RET) + RET 均值 + 正收益占比 + RET↔CAR 同号率
  ⑦ 分场景分桶 ACC              → Market × EventType × vol_regime 3D 小格 ACC

四基线：
  B0 majority (只预测 label_avg_all 多数类)
  B1 random 均衡 (up/down/neutral 各 1/3)
  B2 oracle label_primary (上界，不可能达到)
  B3 Tier 1 analyzer 旧版（若有 baseline predictions 提供）

Wilson 95% CI：对所有比例类指标给置信区间（便于 A/B 显著性判断）。

输出：
  evaluation_report.json （指标大字典，给下游 xlsx 面板用）
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import sys
_THIS = Path(__file__).resolve().parent.parent / "scripts"
if str(_THIS) not in sys.path:
    sys.path.insert(0, str(_THIS))
from scene_match import primary_horizon_for, scene_meta_for  # noqa: E402


HORIZONS = ["t3", "t7", "t15", "t30", "t60"]


# ====================== 工具 ======================
def _read_jsonl(p: Path) -> list[dict]:
    rows = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try: rows.append(json.loads(line))
                except Exception: continue
    return rows


def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score 95% CI。返回 (p, lo, hi)。"""
    if n <= 0: return 0.0, 0.0, 0.0
    p = k / n
    denom = 1 + z*z / n
    center = (p + z*z / (2*n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z*z / (4 * n * n)) / denom
    return p, max(0.0, center - margin), min(1.0, center + margin)


def _safe_mean(vals: list[float]) -> float:
    if not vals: return 0.0
    return sum(vals) / len(vals)


# ====================== 解析预测 ======================
def _parse_prediction(pred: dict) -> dict:
    """兼容多种 preds 格式：
      · {event_id, direction, confidence, completion}（RLVR 新格式）
      · {event_id, pred, prediction}（旧 PRONOIA 格式）"""
    direction = (pred.get("direction") or pred.get("pred") or pred.get("prediction") or "neutral")
    direction = str(direction).lower()
    if direction not in ("up", "down", "neutral"):
        direction = "neutral"
    conf = pred.get("confidence")
    if not isinstance(conf, (int, float)):
        conf = 0.5
    return {
        "direction": direction,
        "confidence": max(0.0, min(1.0, float(conf))),
        "completion": pred.get("completion") or "",
        "diag": pred.get("diag") or pred.get("diagnostics") or {},
    }


# ====================== 七大指标计算 ======================
def evaluate(events: list[dict], labels_map: dict[str, dict],
              preds_map: dict[str, dict]) -> dict:
    """preds_map[event_id] = {direction, confidence, completion}。"""
    # ------------ 预处理：拿到 (e, lb, pred) 配对 ------------
    pairs = []
    for e in events:
        eid = str(e.get("event_id") or "")
        lb = labels_map.get(eid); pred = preds_map.get(eid)
        if not lb or not pred: continue
        pairs.append((e, lb, _parse_prediction(pred)))

    n = len(pairs)
    if n == 0:
        return {"error": "no matched (event, label, pred) pairs"}

    # ========== ① 定向 primary strict ACC ==========
    k_primary = 0
    for e, lb, pr in pairs:
        ph = primary_horizon_for(e.get("market",""), e.get("event_type_l2",""))
        gt = str(lb.get(f"label_{ph}") or "neutral").lower()
        if pr["direction"] == gt: k_primary += 1
    p_primary, lo1, hi1 = _wilson_ci(k_primary, n)

    # ========== ② avg_all strict ACC（旧口径） ==========
    k_avgall = 0
    for e, lb, pr in pairs:
        gt = str(lb.get("label_avg_all") or "neutral").lower()
        if pr["direction"] == gt: k_avgall += 1
    p_avgall, lo2, hi2 = _wilson_ci(k_avgall, n)

    # ========== ③ 双窗一致率（预测正确 + 双窗 GT 同号） ==========
    k_double = 0; n_double_valid = 0
    for e, lb, pr in pairs:
        meta = scene_meta_for(e.get("market",""), e.get("event_type_l2",""))
        ph = meta["primary_horizon"]
        secs = meta["secondary_horizons"]
        if not secs: continue
        sec = secs[0]
        car_p = lb.get(f"car_{ph}"); car_s = lb.get(f"car_{sec}")
        if not isinstance(car_p, (int,float)) or not isinstance(car_s, (int,float)): continue
        # GT 双窗同号
        same = (car_p * car_s > 0) or (car_p == 0 and car_s == 0)
        # 预测与 primary 一致
        gt_primary = "up" if car_p > 0 else "down" if car_p < 0 else "neutral"
        correct_and_same = (pr["direction"] == gt_primary) and same
        n_double_valid += 1
        if correct_and_same: k_double += 1
    p_double, lo3, hi3 = _wilson_ci(k_double, n_double_valid)

    # ========== ④ 量价一致率：3 桶 ACC ==========
    bucket_acc = {}
    for regime in ("HIGH", "NORMAL", "LOW"):
        sub = [(e, lb, pr) for (e, lb, pr) in pairs if str(e.get("vol_regime") or lb.get("vol_regime") or "") == regime]
        if not sub: continue
        kk = 0
        for e, lb, pr in sub:
            ph = primary_horizon_for(e.get("market",""), e.get("event_type_l2",""))
            gt = str(lb.get(f"label_{ph}") or "neutral").lower()
            if pr["direction"] == gt: kk += 1
        p_b, lo_b, hi_b = _wilson_ci(kk, len(sub))
        bucket_acc[regime] = {"n": len(sub), "acc": p_b, "ci_lo": lo_b, "ci_hi": hi_b}

    # ========== ⑤ MoE 健康度 ==========
    entropies = []; active_counts = []
    for e, lb, pr in pairs:
        rw = pr["diag"].get("router_weights") if isinstance(pr["diag"], dict) else None
        if not isinstance(rw, dict) or not rw:
            continue
        w_list = list(rw.values())
        H = -sum(w * math.log(max(w, 1e-12)) for w in w_list if w > 0)
        entropies.append(H)
        active = sum(1 for w in w_list if w >= 0.1)
        active_counts.append(active)
    moe = {
        "n_with_router": len(entropies),
        "router_entropy_mean": _safe_mean(entropies),
        "active_experts_mean": _safe_mean([float(x) for x in active_counts]),
        "active_experts_distribution": dict(Counter(active_counts)),
    }

    # ========== ⑥ RET 独立面板（事件后标的自身收益 ret，5 horizons） ==========
    ret_panel = {}
    for h in HORIZONS:
        rets = []; cars = []; agree = 0; valid = 0; pos_ret = 0
        acc_ret_aligned = 0  # 预测方向与 RET 符号一致（alpha 纯度验证）
        for e, lb, pr in pairs:
            r = lb.get(f"ret_{h}"); c = lb.get(f"car_{h}")
            if not isinstance(r,(int,float)) or not isinstance(c,(int,float)): continue
            if abs(r) >= 10 or abs(c) >= 10: continue
            valid += 1; rets.append(float(r)); cars.append(float(c))
            if (r > 0 and c > 0) or (r < 0 and c < 0): agree += 1
            if r > 0: pos_ret += 1
            # 预测方向 vs RET 符号
            ret_sign = "up" if r > 0 else "down" if r < 0 else "neutral"
            if ret_sign != "neutral" and pr["direction"] == ret_sign:
                acc_ret_aligned += 1
        p_agree, _, _ = _wilson_ci(agree, valid)
        p_pos, _, _   = _wilson_ci(pos_ret, valid)
        p_acc, lo6, hi6 = _wilson_ci(acc_ret_aligned, valid)
        ret_panel[h] = {
            "n_valid": valid,
            "RET_mean_pct": round(_safe_mean(rets) * 100, 3),
            "CAR_mean_pct": round(_safe_mean(cars) * 100, 3),
            "RET_positive_ratio": round(p_pos, 4),
            "RET_CAR_agree_ratio":  round(p_agree, 4),
            "ACC_vs_RET_direction":  round(p_acc, 4),
            "ACC_vs_RET_CI": [round(lo6, 4), round(hi6, 4)],
        }

    # ========== ⑦ 分场景分桶 ACC（Market × EventType × vol_regime） ==========
    scene_bucket = defaultdict(list)
    for e, lb, pr in pairs:
        mkt = str(e.get("market") or "").upper()
        el2 = str(e.get("event_type_l2") or "")
        vr  = str(e.get("vol_regime") or lb.get("vol_regime") or "NORMAL")
        scene_bucket[(mkt, el2, vr)].append((e, lb, pr))
    scene_acc = {}
    for (m, e2, vr), sub in sorted(scene_bucket.items()):
        kk = 0
        for ee, ll, ppr in sub:
            ph = primary_horizon_for(ee.get("market",""), ee.get("event_type_l2",""))
            gt = str(ll.get(f"label_{ph}") or "neutral").lower()
            if ppr["direction"] == gt: kk += 1
        p_b, lo_b, hi_b = _wilson_ci(kk, len(sub))
        scene_acc[f"{m}|{e2}|{vr}"] = {"n": len(sub), "acc": round(p_b,4), "CI": [round(lo_b,4), round(hi_b,4)]}

    return {
        "n_matched_pairs": n,
        "primary_strict_ACC":      {"value": round(p_primary,4), "CI": [round(lo1,4), round(hi1,4)], "n_correct": k_primary},
        "avg_all_strict_ACC":      {"value": round(p_avgall,4),  "CI": [round(lo2,4), round(hi2,4)], "n_correct": k_avgall},
        "double_window_consistency": {"value": round(p_double,4), "CI": [round(lo3,4), round(hi3,4)],
                                       "n_valid": n_double_valid, "n_correct_and_same": k_double},
        "volume_3bucket_ACC": bucket_acc,
        "moe_health": moe,
        "ret_panel": ret_panel,
        "scene_bucket_ACC": scene_acc,
        "meta": {
            "primary_h_distribution": dict(Counter(
                primary_horizon_for(e.get("market",""), e.get("event_type_l2","")) for (e,_,_) in pairs
            )),
        },
    }


# ====================== CLI ======================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-events", required=True)
    ap.add_argument("--eval-labels", required=True)
    ap.add_argument("--preds",       required=True, help="RLVR 模型 predictions.jsonl")
    ap.add_argument("--baseline", action="append", default=[],
                    help="基线 predictions：格式 NAME=path（可多次指定）")
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    evs = _read_jsonl(Path(args.eval_events))
    lbs = _read_jsonl(Path(args.eval_labels))
    lb_map = {str(lb.get("event_id") or ""): lb for lb in lbs}
    print(f"[LOAD] eval events={len(evs)} labels={len(lbs)}")

    # RLVR 主模型
    preds_rlvr = _read_jsonl(Path(args.preds))
    pr_map = {str(p.get("event_id") or ""): p for p in preds_rlvr}
    report = {"RLVR": evaluate(evs, lb_map, pr_map)}

    # 基线：--baseline NAME=path（允许多个）
    for b in args.baseline:
        if "=" not in b: continue
        name, path = b.split("=", 1)
        bp = _read_jsonl(Path(path))
        bm = {str(p.get("event_id") or ""): p for p in bp}
        report[name] = evaluate(evs, lb_map, bm)

    # 附加：内置三基线（无需文件）
    # B0 majority（label_avg_all 众数类作为所有预测）
    maj_lbl = Counter(str(lb.get("label_avg_all") or "neutral").lower() for lb in lbs if lb.get("label_avg_all")).most_common(1)[0][0]
    B0_preds = {}
    for e in evs:
        eid = str(e.get("event_id") or "")
        if eid in lb_map: B0_preds[eid] = {"direction": maj_lbl, "confidence": 0.6}
    report["B0_majority"] = evaluate(evs, lb_map, B0_preds)

    # B2 oracle primary（上帝视角上界）
    B2 = {}
    for e in evs:
        eid = str(e.get("event_id") or "")
        lb = lb_map.get(eid)
        if not lb: continue
        ph = primary_horizon_for(e.get("market",""), e.get("event_type_l2",""))
        gt = str(lb.get(f"label_{ph}") or "neutral").lower()
        B2[eid] = {"direction": gt, "confidence": 0.9}
    report["B2_oracle_primary"] = evaluate(evs, lb_map, B2)

    # 写 JSON
    rep_path = Path(args.report)
    rep_path.parent.mkdir(parents=True, exist_ok=True)
    with open(rep_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 终端打印关键数字
    print("\n========== EVAL REPORT (key metrics) ==========")
    for name, rr in report.items():
        if "n_matched_pairs" not in rr: continue
        p1 = rr["primary_strict_ACC"]["value"]
        p2 = rr["avg_all_strict_ACC"]["value"]
        ret_t7 = rr["ret_panel"].get("t7", {}).get("RET_CAR_agree_ratio", None)
        print(f"  {name:<22}  primary ACC={p1*100:5.1f}%  avg_all ACC={p2*100:5.1f}%  RET-CAR t7 agree={ret_t7*100 if ret_t7 else 0:.1f}%")
    print(f"\n[DONE] → {rep_path}")


if __name__ == "__main__":
    main()
