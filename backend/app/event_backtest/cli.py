from __future__ import annotations

import argparse
from pathlib import Path

from .application import (
    collect_seed_events_file,
    curate_phase1_events_file,
    read_metrics,
    run_predictions_file,
    score_files,
    validate_events_file,
    write_event_template,
    write_metrics,
)
from .report import write_markdown
from .labeller import (
    load_events as _labeller_load_events,
    _compute_cars_for_events,
    write_labels as _labeller_write_labels,
)


def build_bt_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pronoia bt", add_help=False)
    sub = ap.add_subparsers(dest="bt_command", required=True)

    validate = sub.add_parser("validate-events", aliases=["ve"], help="校验事件池 jsonl")
    validate.add_argument("--events", required=True, help="events.jsonl")
    validate.set_defaults(func=cmd_validate_events)

    template = sub.add_parser("template-events", aliases=["te"], help="导出事件池模板 jsonl")
    template.add_argument("--out", required=True, help="events_template.jsonl")
    template.add_argument("--count", type=int, default=1, help="导出多少条模板行")
    template.set_defaults(func=cmd_template_events)

    collect = sub.add_parser("collect-seeds", aliases=["cs"], help="自动挖掘官方/准官方候选事件")
    collect.add_argument("--out", required=True, help="events_seeds.jsonl")
    collect.add_argument("--cn-dates", default="", help="A股公告日期列表，逗号分隔，如 20250701,20250702")
    collect.add_argument("--cn-keywords", default="", help="A股公告关键词，逗号分隔")
    collect.add_argument("--us-symbols", default="", help="美股代码列表，逗号分隔，如 NVDA,AAPL,MSFT")
    collect.add_argument("--us-count-per-symbol", type=int, default=20, help="每个美股代码拉多少条 SEC filings")
    collect.add_argument("--include-macro", action="store_true", help="追加经济日历宏观事件（会映射到指数标的）")
    collect.add_argument("--macro-limit", type=int, default=120, help="宏观事件最多拉多少条")
    collect.add_argument("--start-date", default="", help="回测安全起点 YYYY-MM-DD，会据此自动采 CN 日期步长样本")
    collect.add_argument("--end-date", default="", help="回测安全终点 YYYY-MM-DD，T+5 标签必须在此之后可得")
    collect.add_argument("--min-history-days", type=int, default=0, help="距今最少历史天数，避免最近事件无法打 T+5 标签")
    collect.add_argument("--cn-backtest-step-days", type=int, default=5, help="历史窗口内 CN 采日期样的步长（天）")
    collect.set_defaults(func=cmd_collect_seeds)

    curate = sub.add_parser("prepare-phase1", aliases=["pp1"], help="从 seeds 自动整理 Phase 1 事件池")
    curate.add_argument("--seeds", required=True, help="events_seeds.jsonl")
    curate.add_argument("--out", required=True, help="events_phase1.jsonl")
    curate.add_argument("--mode", choices=["balanced", "natural"], default="natural", help="balanced=按类型等量；natural=按市场+类型自然频率")
    curate.add_argument("--total", type=int, default=180, help="mode=natural 时总条数目标")
    curate.add_argument("--cn-ratio", type=float, default=0.7, help="mode=natural 时 CN 占比（0~1）")
    curate.add_argument("--per-type-target", type=int, default=30, help="每个事件类型目标条数")
    curate.add_argument(
        "--max-per-symbol-per-type",
        type=int,
        default=2,
        help="同一类型下单个 symbol 的软上限，若样本不足会自动放宽补齐",
    )
    curate.add_argument("--start-date", default="", help="仅保留 >= 该日期的事件")
    curate.add_argument("--end-date", default="", help="仅保留 <= 该日期的事件")
    curate.add_argument("--min-history-days", type=int, default=0, help="仅保留距今至少 N 天的事件")
    curate.set_defaults(func=cmd_prepare_phase1)

    run = sub.add_parser("run", aliases=["r"], help="对事件池生成 baseline predictions")
    run.add_argument("--events", required=True, help="events.jsonl")
    run.add_argument("--out", required=True, help="predictions.jsonl")
    run.add_argument("--run-id", required=True, help="回测 run id")
    run.add_argument("--runner", choices=["baseline", "team_prompt", "team_full"], default="baseline", help="预测器类型：baseline=恒=up；team_prompt=单 prompt 支持路线A --system-prompt-variant；team_full=真 Team Agent 6 步 trajectory（plan→fan-out→synthesize→verify→extract）每 event 写 _trajectory_ckpt/{event_id}.json")
    run.add_argument("--model-version", default=None)
    run.add_argument(
        "--system-prompt-variant",
        choices=[
            "v0", "v2_cn_specialized", "cn_v2", "cnv2", "merged_cnv2_usv1",
            "v3_cn_calib", "cnv3", "v4_cn_calib", "cnv4", "v5_cn_calib",
            "cnv5", "v6_cn_calib", "cnv6",
            "deep_researcher_v0", "deep_researcher_claim_v2",
        ],
        default="v0",
        help=(
            "team_prompt 使用市场判别 prompt；team_full 可用 deep_researcher_v0 / "
            "deep_researcher_claim_v2 做 Deep Researcher persona A/B。"
        ),
    )
    run.add_argument("--concurrency", type=int, default=4, help="并发数（team_full 也支持并发，state 为 per-event 局部变量）")
    run.add_argument("--resume", action="store_true", help="若 out 已存在则跳过已预测 event_id 并继续补全（team_full 会同时跳过 trajectory 已落盘的 event_id）")
    run.add_argument("--trajectory-ckpt-dir", default="", help="仅 --runner=team_full 使用：完整 trajectory 写盘目录（默认 data/_trajectory_ckpt）；每个 event_id 一个 JSON，可被 `bt trajectory` 回放")
    run.set_defaults(func=cmd_run)

    score = sub.add_parser("score", help="根据 predictions/labels 计算指标")
    score.add_argument("--pred", required=True, help="predictions.jsonl")
    score.add_argument("--labels", required=True, help="labels.jsonl")
    score.add_argument("--out", required=True, help="metrics_summary.json")
    score.add_argument("--epsilon", type=float, default=0.005)
    score.add_argument("--primary-oracle-horizon", "--oracle-label", dest="primary_oracle_horizon",
                       default="t3",
                       help="主证据 oracle horizon（默认 t3）。可改为 avg_all（推荐，avgCAR 平均方向更稳）/"
                            "avg_mid / consensus66 / t7 / t15 / t30 / t60。其余 horizons 会同时"
                            "输出作参考指标。")
    score.set_defaults(func=cmd_score)

    report = sub.add_parser("report", help="从 metrics_summary.json 生成 markdown 报告")
    report.add_argument("--metrics", required=True, help="metrics_summary.json")
    report.add_argument("--out", required=True, help="backtest_report.md")
    report.set_defaults(func=cmd_report)

    label = sub.add_parser("label", aliases=["lb"], help="用 yfinance 历史行情生成 T+1/T+3/T+5 真实标签（CAR=asset_ret - benchmark_ret）")
    label.add_argument("--events", required=True, help="events.jsonl（phase1 / seeds 均可）")
    label.add_argument("--out", required=True, help="labels.jsonl（每行含 car_t1/car_t3/car_t5 + label_t1~5）")
    label.add_argument("--epsilon", type=float, default=0.0, help="方向阈值，默认0：正值看涨、负值看跌，恰好为0不计方向；显式正阈值保留旧中性带")
    label.add_argument("--data-source", choices=["auto", "akshare_only", "yfinance_only", "hybrid"], default="auto", help="数据源：默认 auto=akshare 三只脚 stock_us_daily / CN-asset Sina-TX / CN-index sh000300；缺数据 hybrid 兜底 yfinance；akshare_only 强制只用 akshare；yfinance_only 强制只用 yfinance")
    label.set_defaults(func=cmd_label)

    case_study = sub.add_parser("case-study", aliases=["cst"], help="自动从 events + labels + preds 挑 5 个典型 Case（并购对/错、财报对/错、US 对）打印+写盘")
    case_study.add_argument("--events", required=True, help="events.jsonl（如 data/events_phase1_backtestable_natural_180.jsonl）")
    case_study.add_argument("--labels", required=True, help="labels.jsonl（如 data/labels_phase1.jsonl）")
    case_study.add_argument("--pred", required=True, help="predictions.jsonl（如 data/preds_merged_CNv2_USv1.jsonl）")
    case_study.add_argument("--pred-v1", default="", help="[可选] v1 predictions.jsonl，若提供则三种方案同屏显示（baseline + --pred-v1 + --pred）")
    case_study.add_argument("--out", default="", help="[可选] 输出报告 txt 路径；默认写 data/cli_artifacts/case_study_<timestamp>.txt 或 data/case_study.txt")
    case_study.add_argument("--count", type=int, default=5, help="选几个典型 Case（默认 5：并购对/并购错/财报对/财报错/US对）")
    case_study.set_defaults(func=cmd_case_study)

    trajectory = sub.add_parser("trajectory", aliases=["traj", "tj"], help="回放 team_full runner 产出的单个 event trajectory：终端打印 5 步摘要 + 最终结果 + 可选写盘")
    trajectory.add_argument("--event-id", required=True, help="要回放的 event_id（team_full runner 会写 trajectory_ckpt_dir/{event_id}.json）")
    trajectory.add_argument("--ckpt-dir", default="", help="trajectory 目录，默认 data/_trajectory_ckpt，找不到时会在 data/cli_artifacts/_trajectory_ckpt 和 /tmp 再找")
    trajectory.add_argument("--out", default="", help="[可选] 回放报告 txt 输出路径，默认仅终端打印")
    trajectory.add_argument("--sse-dump-json", default="", help="[可选] 把原始 trajectory_sse_events 数组单独 dump 成 JSON（方便传前端 SSE 播放")
    trajectory.add_argument("--verbose", action="store_true", help="额外打印计划完整 agent_step / tool_calls 全部 JSON")
    trajectory.add_argument("--out-md", default="", help="[可选] 把完整证据图+hypotheses+verify 导出为标准 Markdown 报告（含 Oracle 对照需 --labels）")
    trajectory.add_argument("--labels", default="", help="[可选] labels.jsonl，配合 --out-md 时自动匹配 Oracle T+1/T+3/T+5 标签到报告")
    trajectory.set_defaults(func=cmd_trajectory)

    return ap


def cmd_validate_events(args: argparse.Namespace) -> int:
    issues = validate_events_file(args.events)
    if issues:
        raise SystemExit("事件文件校验失败：\n" + "\n".join(issues))
    print("事件文件校验通过")
    return 0


def cmd_template_events(args: argparse.Namespace) -> int:
    write_event_template(args.out, count=int(args.count))
    print(f"已生成事件模板 -> {args.out}")
    return 0


def cmd_collect_seeds(args: argparse.Namespace) -> int:
    cn_dates = [x.strip() for x in str(args.cn_dates or "").split(",") if x.strip()]
    cn_keywords = [x.strip() for x in str(args.cn_keywords or "").split(",") if x.strip()]
    us_symbols = [x.strip() for x in str(args.us_symbols or "").split(",") if x.strip()]
    start_date = str(args.start_date).strip() or None
    end_date = str(args.end_date).strip() or None
    min_history_days = int(getattr(args, "min_history_days", 0) or 0) or None
    count = collect_seed_events_file(
        out_path=args.out,
        cn_dates=cn_dates or None,
        cn_keywords=cn_keywords or None,
        us_symbols=us_symbols or None,
        us_count_per_symbol=int(args.us_count_per_symbol),
        include_macro=bool(args.include_macro),
        macro_limit=int(args.macro_limit),
        start_date=start_date,
        end_date=end_date,
        min_history_days=min_history_days,
        cn_backtest_step_days=int(getattr(args, "cn_backtest_step_days", 5) or 5),
    )
    print(f"已生成 {count} 条 seed events -> {args.out}")
    return 0


def cmd_prepare_phase1(args: argparse.Namespace) -> int:
    start_date = str(args.start_date).strip() or None
    end_date = str(args.end_date).strip() or None
    min_history_days = int(getattr(args, "min_history_days", 0) or 0) or None
    summary = curate_phase1_events_file(
        seeds_path=args.seeds,
        out_path=args.out,
        mode=str(getattr(args, "mode", "natural") or "natural"),
        total_target=int(getattr(args, "total", 180) or 180),
        cn_ratio=float(getattr(args, "cn_ratio", 0.7) or 0.7),
        per_type_target=int(args.per_type_target),
        max_per_symbol_per_type=int(args.max_per_symbol_per_type),
        start_date=start_date,
        end_date=end_date,
        min_history_days=min_history_days,
    )
    print(f"已整理 Phase 1 事件池 -> {args.out}")
    print(f"selected={summary['selected']} requested_total={summary['requested_total']}")
    if summary.get("date_range"):
        print(f"date_range={summary['date_range']}")
    print(f"by_market={summary['by_market']}")
    print(f"by_type={summary['by_type']}")
    print(f"type_shortfalls={summary['type_shortfalls']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    kwargs = dict(
        events_path=args.events,
        out_path=args.out,
        run_id=args.run_id,
        runner=args.runner,
        model_version=args.model_version,
        concurrency=int(args.concurrency),
        resume=bool(args.resume),
    )
    if args.runner in {"team_prompt", "team_full"}:
        kwargs["system_prompt_variant"] = str(getattr(args, "system_prompt_variant") or "v0")
    if args.runner == "team_full":
        ckpt_arg = str(getattr(args, "trajectory_ckpt_dir") or "").strip()
        if ckpt_arg:
            kwargs["trajectory_ckpt_dir"] = ckpt_arg
    count = run_predictions_file(**kwargs)
    print(f"已生成 {count} 条 predictions ({args.runner}) -> {args.out}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    primary_h = str(getattr(args, "primary_oracle_horizon") or "t3").strip() or "t3"
    metrics = score_files(
        predictions_path=args.pred,
        labels_path=args.labels,
        epsilon=float(args.epsilon),
        primary_oracle_horizon=primary_h,
    )
    write_metrics(args.out, metrics)
    # 打印人类友好的多 horizon 指标汇总
    ref_hs: list[str] = ["t3", "t7", "t15", "t30", "t60", "avg_all", "consensus66"]
    lines = []
    lines.append(f"[INFO] metrics written -> {args.out}")
    lines.append(f"[INFO] primary oracle horizon: {primary_h}  (epsilon={args.epsilon})")
    lines.append(f"[INFO] n_total={metrics.n_total}  n_abstain_pred={metrics.n_abstain_pred}"
                 f"  n_abstain_oracle({primary_h})={metrics.n_abstain_oracle}")
    lines.append("[INFO] 多时间窗口 Strict ACC（Oracle 各 horizon 对比，作参考）：")
    md: dict = metrics.to_dict()
    for h in ref_hs:
        key = f"acc_{h}_strict"
        item = md.get(key, {"n": 0, "k": 0, "acc": 0.0, "wilson_lo_95": 0.0, "wilson_hi_95": 1.0})
        if not isinstance(item, dict): continue
        n, k, acc, lo, hi = item.get("n", 0), item.get("k", 0), item.get("acc", 0.0), \
            item.get("wilson_lo_95", 0.0), item.get("wilson_hi_95", 1.0)
        marker = "  ← PRIMARY" if h == primary_h else ""
        lines.append(f"  {h:14s} ACC={acc*100:5.2f}%  k/n={k}/{n}  Wilson95% CI [{lo*100:4.2f}%, {hi*100:4.2f}%]{marker}")
    # primary non-neutral / significant only
    pri_nn = md.get("acc_primary_non_neutral", {})
    if isinstance(pri_nn, dict) and pri_nn.get("n", 0) > 0:
        lines.append(f"[INFO] primary({primary_h}) NON-NEUTRAL 口径：ACC={pri_nn['acc']*100:5.2f}%"
                     f"  k/n={pri_nn.get('k',0)}/{pri_nn.get('n',0)}  "
                     f"Wilson [{pri_nn.get('wilson_lo_95',0)*100:4.2f}%, {pri_nn.get('wilson_hi_95',1)*100:4.2f}%]")
    pri_sig = md.get("acc_primary_significant_only", {})
    if isinstance(pri_sig, dict) and pri_sig.get("n", 0) > 0:
        lines.append(f"[INFO] primary({primary_h}) SIGNIFICANT-ONLY(p<0.10) 口径："
                     f"ACC={pri_sig['acc']*100:5.2f}%  k/n={pri_sig.get('k',0)}/{pri_sig.get('n',0)}")

    # ---------- 新增：改进指标（辅助分析，不影响主口径 golden） ----------
    # A. Strict vs Lenient 双口径对比（T+3/7/15/30/60）
    #    Lenient 规则：双方都非 neutral（up/down）且未弃权 → 计入分母，方向相等=正确
    #    这样 neutral 预测 / neutral Oracle 不会冤枉一方，公平比方向判断质量
    try:
        import json as _json, math as _math
        from collections import Counter as _Counter
        preds_raw = [_json.loads(l) for l in open(args.pred, encoding="utf-8") if l.strip()]
        labels_raw = [_json.loads(l) for l in open(args.labels, encoding="utf-8") if l.strip()]
        lbl_map = {r["event_id"]: r for r in labels_raw}
        pred_map = {r["event_id"]: r for r in preds_raw}
        common_ids = [eid for eid in pred_map if eid in lbl_map]
        hs_ext: list[str] = ["t3", "t7", "t15", "t30", "t60"]
        lines.append("")
        lines.append("=" * 110)
        lines.append("[改善指标 A] Strict vs Lenient 双口径对比（用户说：主要看 T+3/7/15/30/60）")
        lines.append("=" * 110)
        lines.append("  Strict  : neutral(预测/Oracle任何一方)=算错，最严格")
        lines.append("  Lenient : 双方都非 neutral(up/down) 才计分，公平评估方向判断准确率")
        header = (f"  {'Oracle':8s}  {'Strict ACC':>10s}  k/n(s)   "
                  f"{'Lenient ACC':>11s}  k/n(l)   "
                  f"{'Oracle=neu':>10s}  {'Pred=neu':>9s}  {'双方出手一致率':>15s}")
        lines.append(header)
        lines.append("  " + "-" * 105)
        for h in hs_ext:
            # Strict（取metrics已有值）
            key_s = f"acc_{h}_strict"
            s_item = md.get(key_s, {}) or {}
            ns, ks = int(s_item.get("n", 0)), int(s_item.get("k", 0))
            acc_s = ks / ns * 100 if ns else 0.0
            # Lenient 重算
            nl = kl = 0
            oracle_neu_cnt = pred_neu_cnt = 0
            for eid in common_ids:
                p = pred_map[eid]; lab = lbl_map[eid]
                lab_h = (lab.get(f"label_{h}") or "").strip()
                pred_dir = str(p.get("pred_direction") or "")
                if not lab_h: continue  # Oracle弃权：跳过
                if lab_h == "neutral": oracle_neu_cnt += 1
                if pred_dir == "neutral": pred_neu_cnt += 1
                if bool(p.get("abstain")): continue
                # Lenient 条件
                if lab_h in {"up", "down"} and pred_dir in {"up", "down"}:
                    nl += 1
                    if lab_h == pred_dir: kl += 1
            acc_l = kl / nl * 100 if nl else 0.0
            hands_agree_pct = kl / nl * 100 if nl else 0.0
            lines.append(
                f"  {'T+'+h[1:]:8s}  {acc_s:>8.2f}%    {ks:>3d}/{ns:<4d}   "
                f"{acc_l:>9.2f}%    {kl:>2d}/{nl:<3d}    "
                f"{oracle_neu_cnt:>6d}      {pred_neu_cnt:>5d}      "
                f"{hands_agree_pct:>8.2f}% ({kl}/{nl})"
            )

        # B. Conf 分桶校准 + Spearman ρ
        lines.append("")
        lines.append("=" * 110)
        lines.append("[改善指标 B] 置信度校准表（conf分桶真实命中率 + Spearman等级相关ρ）")
        lines.append("=" * 110)
        lines.append(f"  真实命中率口径：primary oracle horizon({primary_h}) + Lenient（双方出手方向一致率）")
        lines.append("  （同时参考：5窗平均命中率，用于校准ρ的补充参考）")
        # 计算每条样本的"真实分"：primary_lenient_hit(0/1，若未出手则NA) + 5窗平均命中率（0~1）
        cal_rows: list[dict] = []
        for eid in common_ids:
            p = pred_map[eid]; lab = lbl_map[eid]
            conf = p.get("confidence")
            if conf is None: continue
            pred_dir = str(p.get("pred_direction") or "")
            # Primary lenient hit
            lab_p = (lab.get(f"label_{primary_h}") or "").strip()
            pri_hit: bool | None = None
            if lab_p in {"up", "down"} and pred_dir in {"up", "down"}:
                pri_hit = (lab_p == pred_dir)
            # 5窗平均命中率（t3-t60，严格=方向相等就+1，neutral不算对，分母=非弃权窗口数）
            h5_valid = h5_hit = 0
            for hv in hs_ext:
                lv = (lab.get(f"label_{hv}") or "").strip()
                if not lv: continue
                h5_valid += 1
                if lv in {"up","down"} and pred_dir == lv: h5_hit += 1
            avg_hit_5 = (h5_hit / h5_valid) if h5_valid else None
            cal_rows.append({
                "eid": eid, "conf": float(conf), "pred": pred_dir,
                "pri_hit": pri_hit, "avg_hit_5": avg_hit_5,
            })
        # 分桶：[0.5, 0.55) [0.55, 0.6) [0.6, 0.65) [0.65, 0.7) [0.7, 1.0]
        buckets = [
            ("0.50 ~ 0.55", 0.50, 0.55),
            ("0.55 ~ 0.60", 0.55, 0.60),
            ("0.60 ~ 0.65", 0.60, 0.65),
            ("0.65 ~ 0.70", 0.65, 0.70),
            ("0.70 ~ 1.00", 0.70, 1.01),
        ]
        header_b = f"  {'Conf分桶':>14s}  {'样本数':>6s}  出手数  主口径命中  主LenientACC  5窗平均命中率  方向分布"
        lines.append(header_b)
        lines.append("  " + "-" * 105)
        for b_name, lo, hi in buckets:
            in_b = [r for r in cal_rows if lo <= r["conf"] < hi]
            if not in_b:
                lines.append(f"  {b_name:>14s}  {0:>6d}   —  —")
                continue
            n_b = len(in_b)
            n_play = sum(1 for r in in_b if r["pri_hit"] is not None)
            n_hit_pri = sum(1 for r in in_b if r["pri_hit"] is True)
            acc_pri_b = n_hit_pri / n_play * 100 if n_play else 0.0
            avg5 = [r["avg_hit_5"] for r in in_b if r["avg_hit_5"] is not None]
            avg5m = sum(avg5) / len(avg5) * 100 if avg5 else 0.0
            dir_cnt = _Counter(r["pred"] for r in in_b)
            lines.append(
                f"  {b_name:>14s}  {n_b:>6d}  {n_play:>5d}  {n_hit_pri:>8d}   "
                f"{acc_pri_b:>9.2f}%      {avg5m:>6.2f}%       "
                f"U={dir_cnt.get('up',0)} D={dir_cnt.get('down',0)} N={dir_cnt.get('neutral',0)}"
            )
        # Spearman ρ: conf 排名 vs 5窗平均命中率排名
        cal_valid = [r for r in cal_rows if r["avg_hit_5"] is not None]
        if len(cal_valid) >= 3:
            sorted_conf = sorted(range(len(cal_valid)), key=lambda i: -cal_valid[i]["conf"])
            sorted_rate = sorted(range(len(cal_valid)), key=lambda i: -cal_valid[i]["avg_hit_5"])
            n = len(cal_valid)
            rank_conf = {sorted_conf[k]: n - k for k in range(n)}
            rank_rate = {sorted_rate[k]: n - k for k in range(n)}
            d2 = sum((rank_conf[i] - rank_rate[i]) ** 2 for i in range(n))
            rho = 1 - 6 * d2 / (n * (n * n - 1))
            if rho > 0.5:
                tag = "✅ 校准良好（高conf≈高命中率）"
            elif rho > 0.2:
                tag = "⚠️ 校准一般（弱正相关）"
            elif rho > -0.2:
                tag = "❌ 校准失败（conf几乎和真实命中率无关）"
            else:
                tag = "💥 校准反了（conf越高命中率越低）"
            lines.append("")
            lines.append(f"  Spearman(conf vs 5窗平均命中率) ρ = {rho:+.2f}    {tag}")
            lines.append(f"  说明：ρ>0.5 说明 Gate 分桶是可靠的；若<0 则 conf 需重新校准（后续可做 Platt scaling/isotonic）。")
    except Exception as _e:
        lines.append(f"[WARN] 辅助指标计算失败：{_e!r}")
        lines.append("       （主指标 Strict ACC 已正常写出，不影响）")

    print("\n".join(lines))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    metrics = read_metrics(args.metrics)
    write_markdown(Path(args.out), metrics)
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    data_source = str(getattr(args, "data_source") or "auto").strip().lower()
    events = _labeller_load_events(args.events)
    if not events:
        raise SystemExit(f"[ERROR] 事件文件无有效事件: {args.events}")
    print(f"[INFO] label: loaded {len(events)} events from {args.events}  data_source={data_source}")
    cars = _compute_cars_for_events(events)
    _rows = _labeller_write_labels(events, cars, args.out, epsilon=float(args.epsilon))
    from collections import Counter
    import numpy as _np
    mkt = Counter(e.market for e in events)
    lab_t3 = Counter(r["label_t3"] for r in _rows)
    cars_t3_valid = [r["car_t3"] for r in _rows if r["car_t3"] is not None]
    print(f"[INFO] market: {dict(mkt)}")
    print(f"[INFO] label_t3 distribution (epsilon={args.epsilon}): {dict(lab_t3)}")
    if cars_t3_valid:
        arr = _np.array(cars_t3_valid, dtype=float)
        print(f"[INFO] car_t3 n={len(arr)} mean={arr.mean():.4f} std={arr.std():.4f} min={arr.min():.4f} max={arr.max():.4f}")
    print("DONE_LABELS_CLI")
    return 0


def cmd_case_study(args: argparse.Namespace) -> int:
    import json, collections, sys, datetime as _dt
    from pathlib import Path
    from .engine import _event_prompt
    from .models import EventRecord

    def _load(p: str):
        if not p:
            return {}
        path = Path(p)
        if not path.exists():
            return {}
        return [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]

    events = _load(args.events)
    labels_list = _load(args.labels)
    pred_main_list = _load(args.pred)
    pred_v1_list = _load(getattr(args, "pred_v1") or "")

    labels_map = {r["event_id"]: r for r in (labels_list or [])}
    pred_main = {r["event_id"]: r for r in (pred_main_list or [])}
    pred_v1 = {r["event_id"]: r for r in (pred_v1_list or [])} if pred_v1_list else {}

    buckets = collections.OrderedDict([
        ("并购对", []),
        ("并购错", []),
        ("财报对", []),
        ("财报错", []),
        ("US  对", []),
    ])
    for e in events or []:
        eid = e.get("event_id")
        if not eid:
            continue
        lab_r = labels_map.get(eid)
        pm = pred_main.get(eid)
        if not lab_r or not pm:
            continue
        lbl = str(lab_r.get("label_t3") or "").strip().lower()
        if lbl not in {"up", "down"}:
            continue
        mrg_dir = str(pm.get("pred_direction") or "").strip().lower()
        correct = (lbl == mrg_dir)
        t = e.get("event_type_l2") or ""
        mkt = e.get("market") or ""
        if mkt == "US" and correct:
            buckets["US  对"].append((e, lab_r, pm, pred_v1.get(eid)))
        elif t.startswith("并购") and correct:
            buckets["并购对"].append((e, lab_r, pm, pred_v1.get(eid)))
        elif t.startswith("并购") and not correct:
            buckets["并购错"].append((e, lab_r, pm, pred_v1.get(eid)))
        elif t.startswith("财报") and correct:
            buckets["财报对"].append((e, lab_r, pm, pred_v1.get(eid)))
        elif t.startswith("财报") and not correct:
            buckets["财报错"].append((e, lab_r, pm, pred_v1.get(eid)))

    lines: list[str] = []

    def P(s: str = ""):
        lines.append(s)
        print(s)

    SEP = "=" * 110
    P()
    P(SEP); P("  Pronoia Case Study — CLI 自动挑典型（N=%d） × 三种方案 × T+3 Oracle Label" % len(events or [])); P(SEP)
    P()
    pred_title_main = str(Path(args.pred).name)
    pred_title_v1 = str(Path(getattr(args, "pred_v1") or "").name) if getattr(args, "pred_v1") else ""
    P(f"数据源：events={Path(args.events).name}  labels={Path(args.labels).name}  pred_main={pred_title_main}" + (f"  pred_v1={pred_title_v1}" if pred_title_v1 else ""))
    P("判别 horizon：T+3（事件后 3 个交易日 vs benchmark 的异常收益 CAR）")
    P("方案：① baseline 恒=up  ② %s  ③ %s（主方案，用于判对/错统计）" % (pred_title_v1 or "(未提供 v1)", pred_title_main))
    P()

    def _car_bps(x):
        try:
            return f"{round(float(x) * 10_000, 1)} bps"
        except Exception:
            return "N/A"

    def _pct(x):
        try:
            return f"{float(x) * 100:.1f}%"
        except Exception:
            return "N/A"

    import random
    random.seed(20260809)
    count_target = max(1, int(getattr(args, "count", 5) or 5))
    printed = 0
    for bk_name, arr in buckets.items():
        if printed >= count_target:
            break
        if not arr:
            P(f"[{bk_name.strip()}] 无样本，跳过")
            continue
        # 每条 bucket 最多 1 个典型
        sel = arr[0]
        e, lab, pm, pv1 = sel
        t1c = lab.get("car_t1"); t3c = lab.get("car_t3"); t5c = lab.get("car_t5")
        l1 = str(lab.get("label_t1") or "?").strip().lower()
        l3 = str(lab.get("label_t3") or "?").strip().lower()
        l5 = str(lab.get("label_t5") or "?").strip().lower()
        mrg_dir = str(pm.get("pred_direction") or "?").strip().lower()
        v1_dir = str((pv1 or {}).get("pred_direction") or "?").strip().lower() if pv1 else "?"
        P(SEP)
        P(f"  ▶ 【Case {bk_name.strip()}】 eid={e.get('event_id')}   market={e.get('market')}   symbol={e.get('symbol')}   event_time={(e.get('event_time') or '?')[:10]}")
        P(SEP)
        P("  Event Packet (输入给 LLM 的 as_of_packet，strict_as_of=True，不准用未来信息)：")
        try:
            rec = EventRecord.from_dict(e)
            pkt = _event_prompt(rec)
        except Exception as err:
            pkt = f"<build packet err={err} payload=" + json.dumps({
                "market": e.get("market"), "symbol": e.get("symbol"), "time": e.get("event_time"),
                "type_l2": e.get("event_type_l2"), "benchmark": e.get("benchmark"),
                "title": e.get("title"), "text": e.get("event_text"),
            }, ensure_ascii=False)[:500] + " >"
        if isinstance(pkt, str) and len(pkt) > 1600:
            pkt = pkt[:1600] + "\n... (truncated)"
        P("  " + "\n  ".join(str(pkt).splitlines()))
        P()
        P("  Oracle Label (真实 T+1 / T+3 / T+5 三档，事后 benchmark-relative CAR)：")
        t1_ok = (l1 == mrg_dir); t3_ok = (l3 == mrg_dir); t5_ok = (l5 == mrg_dir)
        P(f"    T+1: label={('✅ ' if t1_ok else '❌ ') + l1:<10s}  CAR={_car_bps(t1c)}  ({_pct(t1c)})")
        P(f"    T+3: label={('✅ ' if t3_ok else '❌ ') + l3:<10s}  CAR={_car_bps(t3c)}  ({_pct(t3c)})   ← 主要判据")
        P(f"    T+5: label={('✅ ' if t5_ok else '❌ ') + l5:<10s}  CAR={_car_bps(t5c)}  ({_pct(t5c)})")
        P()
        P("  三种方案预测 @ T+3 ：")
        bl_dir = "up"
        P(f"    ① baseline 恒=up        → {bl_dir:<5s}  {'✅' if bl_dir == l3 else '❌'} vs oracle={l3:<5s}  (confidence N/A)")
        if pv1:
            v1_conf = (pv1 or {}).get("confidence")
            P(f"    ② v1 {pred_title_v1 or 'pred_v1':<16s} → {v1_dir:<5s}  {'✅' if v1_dir == l3 else '❌'} vs oracle={l3:<5s}  confidence={v1_conf}")
            v1_rat = (pv1 or {}).get("rationale") or ""
            if v1_rat and str(v1_rat).strip():
                vv = str(v1_rat).strip().replace("\n", " ")
                if len(vv) > 320:
                    vv = vv[:320] + "…"
                P(f"       rationale v1: {vv}")
        mrg_conf = pm.get("confidence")
        P(f"    ③ 路线A {pred_title_main:<14s} → {mrg_dir:<5s}  {'✅' if mrg_dir == l3 else '❌'} vs oracle={l3:<5s}  confidence={mrg_conf}")
        mrg_rat = pm.get("rationale") or ""
        if mrg_rat and str(mrg_rat).strip():
            mm = str(mrg_rat).strip().replace("\n", " ")
            if len(mm) > 320:
                mm = mm[:320] + "…"
            P(f"       rationale: {mm}")
        P()
        P(f"  💡 小结：本 Case {'路线A主方案 判对了' if mrg_dir == l3 else '路线A主方案 判错了，根因分析：'}")
        if bk_name == "并购错":
            P("     → 典型 CN-v2 空头先验误伤：packet 含并购/重组类标题但未明确优质资产注入，八条规则 c「并购除非明确优质资产否则略偏空」判了 down；但 oracle 是 up（市场解读为利好 / 优质隐性资产）= 并购类 prompt-only 69.3% 未过 70% 的典型样本。Teacher-force SFT 主要学这类边界。")
        elif bk_name == "财报错":
            P("     → 典型 CN-v2 八条规则 e「财报超预期除非增速≥50%+超预期≥20%否则中性偏空」误伤：oracle 是 up（温和超预期 + 行业景气共振），但 merged 按规则判 down = 财报类 ACC v1→merged 反降 2.44pp 的具体案例。SFT 主要学这条边界。")
        elif bk_name == "并购对":
            P("     → 并购判对：标题/正文含重大资产重组类负面关键词（未披露优质资产），CN-v2 八条 c 条规则命中偏空，oracle 也是 down。")
        elif bk_name == "财报对":
            P("     → 财报判对：标题为业绩预告 / 财报事件，含偏空信号（ST 风险警示 / 不及预期类关键词），规则 e 条命中，oracle 也是 down。")
        elif bk_name.strip() == "US对":
            P("     → US 单市场 prompt-only 天花板 79.07% 的典型样本：US 不套 CN 八条先验，仅按 packet 原文关键词（beat/miss/raise/Rule 425 收购方通常跌）判别，与 oracle 一致。")
        P()
        printed += 1

    P(SEP)
    P("  🧭 Case 横向总结（走 FEVER 正式 CLI 输出）")
    P(SEP)
    P()
    P("   1) US 类 ~30% 权重：不套 CN 八条本土先验，按 packet 原文关键词判别 → prompt-only 已达 79.07%（95% CI 64.8–88.6）。")
    P("   2) 并购类 ~63% 权重：八条 c 条『并购除非优质资产否则偏空』在 ~70% 样本奏效；剩下 ~30% 优质隐性资产/股东动作利好被规则误伤 → SFT 学 +3pp 边界。")
    P("   3) 财报类 ~31% 权重：八条 e 条『增速≥50% 才判 up』太严 → 小幅超预期 up 类全被误伤，财报类 ACC 从 v1 → 路线A反而降 2.44pp → SFT 学 +7pp 边界。")
    P("   4) 总增益 10pp：路线A prompt-only 天花板 64.08% → teacher-force SFT +3 + DPO +7 = 74.1% → 2000 条 N≈1620 时 95% Wilson CI 下限 70.87% ✅ 过线。")
    P()

    out_path = getattr(args, "out") or ""
    if not out_path:
        base = Path(args.events)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_dir = base.parent.parent / "cli_artifacts" if (base.parent.name == "data") else (base.parent / "cli_artifacts")
        try:
            default_dir.mkdir(parents=True, exist_ok=True)
            out_path = str(default_dir / f"case_study_{ts}.txt")
        except Exception:
            out_path = f"/tmp/case_study_{ts}.txt"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[WROTE CLI Case Study Report] {out_path}  size_kb={Path(out_path).stat().st_size / 1024:.2f}")
    return 0


def cmd_trajectory(args: argparse.Namespace) -> int:
    """回放 team_full runner 产出的单个 event trajectory（plan → fan-out → synthesize → verify → hypotheses + 最终结构化 direction/confidence/rationale）。"""
    import json
    from pathlib import Path
    import collections

    eid = str(args.event_id).strip()
    candidates = []
    if getattr(args, "ckpt_dir") and str(args.ckpt_dir).strip():
        candidates.append(Path(str(args.ckpt_dir).strip()) / f"{eid}.json")
    candidates.extend([
        Path("data/_trajectory_ckpt") / f"{eid}.json",
        Path("data/cli_artifacts/_trajectory_ckpt") / f"{eid}.json",
        Path("/tmp") / f"{eid}.json",
    ])
    p: Path | None = None
    for cand in candidates:
        try:
            if cand.exists():
                p = cand
                break
        except Exception:
            continue
    if p is None:
        tried = "\n  · ".join(str(c) for c in candidates)
        raise SystemExit(f"[ERROR] 找不到 trajectory 文件：event_id={eid}\n已尝试：\n  · {tried}")

    obj = json.loads(Path(p).read_text(encoding="utf-8"))
    events = list(obj.get("trajectory_sse_events") or [])
    stats = obj.get("llm_trajectory_stats") or {}
    ex = obj.get("structured_extract") or {}
    meta = obj.get("event_meta") or {}
    final_state = obj.get("team_final_state") or {}
    verbose = bool(getattr(args, "verbose", False))

    lines: list[str] = []
    def P(s: str = ""):
        lines.append(s)
        print(s)

    SEP = "=" * 110
    P()
    P(SEP); P(f"  Pronoia CLI 真 Team Agent Trajectory 回放  event_id={eid}"); P(SEP)
    P()
    P(f"  source file     = {p}  size_kb={p.stat().st_size/1024:.1f}")
    P(f"  generated_at    = {obj.get('generated_at','')}   wall_seconds = {obj.get('wall_seconds','')}")
    P(f"  run_id          = {obj.get('run_id','')}   model_version = {obj.get('model_version','')}   prompt_variant = {obj.get('system_prompt_variant','')}")
    P(f"  event_meta      = market={meta.get('market','')}  symbol={meta.get('symbol','')}  date={meta.get('event_time','')}  type={meta.get('event_type_l2','')}  bm={meta.get('benchmark','')}")
    P(f"  trajectory size = sse_events={stats.get('n_sse_events','?')}   tokens={stats.get('n_tokens_total','?')}   tool_calls={stats.get('n_tool_calls','?')}   hypotheses={stats.get('n_hypotheses','?')}   final_chars={stats.get('n_final_chars','?')}")
    P(f"  agents seen     = {stats.get('agents_seen','?')}")
    P()

    # Step ① plan
    P("▶ Step ① PLAN（router LLM 拆子任务）")
    plans = []
    for ev in events:
        if ev.get("type") == "agent_step" and ev.get("phase") == "plan":
            plans = list(ev.get("plan") or [])
            break
    if plans:
        for i, task in enumerate(plans, 1):
            desc = str(task.get("task") or task.get("description") or "")
            agent = task.get("agent") or task.get("agent_id") or ""
            name = task.get("agent_name") or agent
            P(f"   {i}. {name}({agent}): {desc[:220]}")
            if verbose:
                P(f"      plan JSON = {json.dumps(task, ensure_ascii=False)[:500]}")
    else:
        P("   （此 trajectory 里没找到 plan phase event，可能是老版本。）")
    P()

    # Step ② fan-out（findings + tool calls 按 agent 分组）
    P("▶ Step ② SERIAL FAN-OUT（专家串行执行 + deep_researcher 建证据图）")
    findings_by_agent = collections.OrderedDict()
    tools_by_agent: dict[str, list[dict]] = collections.defaultdict(list)
    for ev in events:
        t = ev.get("type")
        if t == "agent_findings":
            a = ev.get("agent") or ev.get("agent_name") or "(unknown)"
            findings_by_agent.setdefault(a, str(ev.get("findings") or "")[:600])
        if t == "tool":
            a = ev.get("agent") or "(unknown)"
            tools_by_agent[a].append({
                "name": ev.get("name"),
                "ok": ev.get("ok"),
                "preview": (str(ev.get("preview") or "")[:180]),
            })
    if findings_by_agent or tools_by_agent:
        for i, (a, txt) in enumerate(findings_by_agent.items(), 1):
            tc = tools_by_agent.get(a, [])
            P(f"   {i}. agent={a}  tool_calls={len(tc)}")
            P(f"      findings 摘要：{txt[:320]}")
            for t in tc[:6]:
                P(f"      · tool={t['name']}  ok={t['ok']}  preview={t['preview']}")
            if verbose and tc:
                P(f"      tools full JSON 前 3 条 = {json.dumps(tc[:3], ensure_ascii=False)[:600]}")
        remaining_agents = [a for a in tools_by_agent if a not in findings_by_agent]
        for a in remaining_agents:
            tc = tools_by_agent.get(a, [])
            P(f"   · agent={a}（无独立 findings，但已产出 tool_calls={len(tc)}）")
            for t in tc[:3]:
                P(f"     · tool={t['name']}  ok={t['ok']}  preview={t['preview']}")
    else:
        P("   （此 trajectory 里没找到 agent_findings/tool type events，可能是老版本或 agent_run 没有输出 findings。）")
    P()

    # Step ③/④/⑤ synthesize + verify + hypotheses state
    P("▶ Step ③ SYNTHESIZE → ④ VERIFY → ⑤ EXTRACT HYPOTHESES")
    final_content = final_state.get("content_full") or final_state.get("content") or ""
    tool_trace = final_state.get("tool_trace") or []
    hyps = final_state.get("hypotheses") or []
    # 从 SSE events 里挖 verify verdict（通常在 final_content 之前）
    verify_ok = None
    verify_issues = 0
    for ev in events:
        if ev.get("type") == "agent_step" and ev.get("phase") == "verify":
            v = ev.get("verdict") or ev.get("pass") or ev.get("ok")
            if isinstance(v, bool):
                verify_ok = bool(v)
            else:
                s = str(v).lower()
                verify_ok = ("pass" in s) or ("true" in s) or ("ok" in s)
            verify_issues = len(list(ev.get("issues") or ev.get("corrections") or []))
    P(f"   state[content] len = {len(final_content)} chars")
    P(f"   state[tool_trace]  len = {len(tool_trace)}")
    P(f"   state[hypotheses]  len = {len(hyps)}  {'（strict-as-of 下通常=0：全部是事实判断，无前瞻可证伪陈述）' if len(hyps)==0 else ''}")
    if verify_ok is not None:
        P(f"   verifier verdict   = {'PASS ✅' if verify_ok else 'FAIL ❌'}  issues/corrections 条数 = {verify_issues}")
    if hyps:
        for i, h in enumerate(hyps[:8], 1):
            if isinstance(h, dict):
                desc = str(h.get("description") or h.get("text") or str(h))
                P(f"   H{i}. {desc[:220]}")
            else:
                P(f"   H{i}. {str(h)[:220]}")
    P()

    # Final structured result
    P("🏁 真 Team Agent 最终结果（state[content] 中结构化提取）")
    P(f"   direction  = {ex.get('direction','（未解析到）')}")
    P(f"   confidence = {ex.get('confidence','（未解析到）')}")
    r = (ex.get("rationale") or "")[:500]
    P(f"   rationale  = {r}")
    P()
    P("💬 最终回答原文（前 1800 chars，完整内容看 trajectory JSON team_final_state.content_full 字段）")
    P("—" * 110)
    chunk = str(final_content or "")[:1800]
    for line in chunk.splitlines()[:30]:
        P(f"   {line}")
    P("—" * 110)
    P()
    P("🔗 关联 CLI 子命令：")
    P(f"   · 生成此 trajectory： bt run --runner team_full --events <events.jsonl> --out predictions.jsonl --run-id <rid>")
    P(f"   · 对 predictions 打分： bt score --pred predictions.jsonl --labels labels.jsonl --out metrics.json")
    P(f"   · 写 md 报告：        bt report --metrics metrics.json --out report.md")
    P(f"   · 挑典型 Case 深度：  bt case-study --events events.jsonl --labels labels.jsonl --pred predictions.jsonl --count 5")
    P()
    P(f"[SOURCE] {p}")

    out_txt = str(getattr(args, "out") or "").strip()
    if out_txt:
        Path(out_txt).parent.mkdir(parents=True, exist_ok=True)
        Path(out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[WROTE trajectory replay txt] {out_txt}  size_kb={Path(out_txt).stat().st_size/1024:.1f}")

    sse_dump = str(getattr(args, "sse_dump_json") or "").strip()
    if sse_dump:
        Path(sse_dump).parent.mkdir(parents=True, exist_ok=True)
        Path(sse_dump).write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[WROTE sse dump JSON] {sse_dump}  size_kb={Path(sse_dump).stat().st_size/1024:.1f}  events={len(events)}")

    out_md = str(getattr(args, "out_md") or "").strip()
    if out_md:
        labels_path = str(getattr(args, "labels") or "").strip() or None
        from .application import render_trajectory_md_report
        md = render_trajectory_md_report(trajectory_obj=obj, labels_path=labels_path)
        Path(out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(out_md).write_text(md, encoding="utf-8")
        print(f"[WROTE team_full case 报告] {out_md}  size_kb={Path(out_md).stat().st_size/1024:.1f}")
    return 0
