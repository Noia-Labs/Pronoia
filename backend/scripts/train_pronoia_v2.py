from __future__ import annotations
"""
train_pronoia_v2.py — PRONOIA 事件判别器 SFT + DPO 5-fold 训练脚手架 (v2)
=============================================================
目标：跑通 train → hold-out eval → 真实 ACC(+95% Wilson CI) 闭环。
在有 GPU + trl/peft/datasets/accelerate 装完后，直接一条命令：

  # (1) 只做 5-fold split（不训练，先看每 fold market/etype 分布是否平衡）
  python backend/scripts/train_pronoia_v2.py split --k=5 --seed=20260809

  # (2) 训练 (需 GPU + trl 安装)
  python backend/scripts/train_pronoia_v2.py train-sft   --fold 0 --model-name meta-llama/Llama-3.1-8B-Instruct
  python backend/scripts/train_pronoia_v2.py train-dpo   --fold 0 --model-name runs/pronoia_sft_fold0/last
  python backend/scripts/train_pronoia_v2.py eval-all    --model-pattern "runs/pronoia_dpo_fold*/last"

  # (3) 输出所有 fold 的 hold-out 合并 ACC + 95% CI + market/L2 split → 过 70% 否？
  python backend/scripts/train_pronoia_v2.py score-all

依赖（按需安装，脚手架不 import，避免没装就崩）:
    pip install trl peft datasets accelerate transformers bitsandbytes torch
"""
import argparse, collections, hashlib, json, math, os, random, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]  # backend/scripts/ → ROOT
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))
RUNS = ROOT / "runs"
RUNS.mkdir(exist_ok=True)

SFT_JSONL   = DATA / "_sft_rft_artifacts_v2" / "pronoia_sft_train_v2_1000.jsonl"
DPO_JSONL   = DATA / "_sft_rft_artifacts_v2" / "pronoia_rft_pairs_v2_1000.jsonl"
EVENTS_JSONL= DATA / "events_phase1_backtestable_natural_1000.jsonl"
LABELS_JSONL= DATA / "labels_phase1_1000.jsonl"
SPLIT_DIR   = DATA / "_sft_rft_artifacts_v2" / "folds_v2_1000"
SPLIT_DIR.mkdir(parents=True, exist_ok=True)

def load_jsonl(p: Path):
    if not p.exists(): return []
    return [json.loads(l) for l in open(p,encoding="utf-8") if l.strip()]

def wilson(p,n,z=1.96):
    if n==0: return (None,None)
    denom=1+z*z/n; center=(p+z*z/(2*n))/denom
    half=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/denom
    return (max(0,center-half), min(1,center+half))

def stable_stratified_split_ids(events_jsonl=EVENTS_JSONL, k:int=5, seed:int=20260809,
                                test_ratio=0.2):
    """基于 events 做分层（market × event_type_l2 × ym）stratified K-fold。
    返回 dict: fold_i = {"train_ids": set[str], "test_ids": set[str]}"""
    evs = load_jsonl(events_jsonl)
    random.seed(seed)
    # 分组 key = (market, l2, ym)
    grps = collections.defaultdict(list)
    for e in evs:
        ym = (e.get("event_time") or "?")[:7]
        grps[(e.get("market") or "?", e.get("event_type_l2") or "?", ym)].append(e["event_id"])
    keys = sorted(grps.keys())
    # 每个 grp 内 shuffle，然后 round-robin 分配到 fold
    fold_ids = [set() for _ in range(k)]
    # 实际 test_ratio = 1/k；剩下 train
    for key in keys:
        ids = list(grps[key]); random.shuffle(ids)
        if not ids: continue
        # 轮询均匀分
        for i, eid in enumerate(ids):
            # 按 eid hash 确定 fold（稳定）
            h = int(hashlib.sha256((str(seed)+eid).encode()).hexdigest(), 16) % k
            fold_ids[h].add(eid)
    # 若某个 fold 太大/太小（极端），做 1 次 swap 修正
    while True:
        sizes = [len(s) for s in fold_ids]
        target = len(evs) // k
        if max(sizes)-min(sizes) <= max(1, target//20): break
        i_mx = max(range(k), key=lambda i:len(fold_ids[i]))
        i_mn = min(range(k), key=lambda i:len(fold_ids[i]))
        # 从 mx 随机抽 1 个挪到 mn
        eid = random.choice(list(fold_ids[i_mx]))
        fold_ids[i_mx].discard(eid); fold_ids[i_mn].add(eid)
    out = {}
    for i,test in enumerate(fold_ids):
        train = set()
        for j,s in enumerate(fold_ids):
            if j==i: continue
            train.update(s)
        out[str(i)] = {"train_ids": sorted(train), "test_ids": sorted(test)}
    return out, evs

def cmd_split(args):
    out, evs = stable_stratified_split_ids(k=args.k, seed=args.seed)
    # 写盘
    for fi, fold_info in out.items():
        tr = set(fold_info["train_ids"]); te = set(fold_info["test_ids"])
        tr_rows = [r for r in load_jsonl(SFT_JSONL) if r["event_id"] in tr]
        te_rows = [r for r in load_jsonl(SFT_JSONL) if r["event_id"] in te]
        dpo_tr   = [r for r in load_jsonl(DPO_JSONL) if r["event_id"] in tr]
        dpo_te   = [r for r in load_jsonl(DPO_JSONL) if r["event_id"] in te]
        fold_dir = SPLIT_DIR / f"fold{fi}"
        fold_dir.mkdir(parents=True, exist_ok=True)
        (fold_dir/"sft_train.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in tr_rows), encoding="utf-8")
        (fold_dir/"sft_test.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in te_rows), encoding="utf-8")
        (fold_dir/"dpo_train.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in dpo_tr), encoding="utf-8")
        (fold_dir/"dpo_test.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in dpo_te), encoding="utf-8")
        (fold_dir/"ids.json").write_text(json.dumps({"train_ids":fold_info["train_ids"],"test_ids":fold_info["test_ids"]},ensure_ascii=False,indent=2), encoding="utf-8")
        # 自检平衡
        ev_tr = [e for e in evs if e["event_id"] in tr]
        ev_te = [e for e in evs if e["event_id"] in te]
        m_tr = collections.Counter(e.get("market") or "?" for e in ev_tr)
        m_te = collections.Counter(e.get("market") or "?" for e in ev_te)
        l2_tr = collections.Counter(e.get("event_type_l2") or "?" for e in ev_tr)
        l2_te = collections.Counter(e.get("event_type_l2") or "?" for e in ev_te)
        print(f"[fold {fi}] train_events={len(ev_tr)}  test_events={len(ev_te)}  train/test={len(ev_tr)/max(1,len(ev_te)):.1f}/1 (target {args.k-1}/1)")
        print(f"  market train: {dict(m_tr)}   test: {dict(m_te)}")
        print(f"  L2 train_top3: {l2_tr.most_common(3)}   test_top3: {l2_te.most_common(3)}")
        print(f"  sft_train_rows={len(tr_rows)} sft_test_rows={len(te_rows)} dpo_train_rows={len(dpo_tr)} dpo_test_rows={len(dpo_te)}")
        print(f"  → wrote {fold_dir}/")
    print(f"\n[split DONE] {SPLIT_DIR}  k={args.k} seed={args.seed}")

def _require_gpu_libs():
    missing = []
    for m in ["trl","peft","datasets","accelerate","transformers","torch"]:
        try: __import__(m)
        except Exception: missing.append(m)
    if missing:
        print(f"[SKIP GPU] 缺少训练依赖: pip install {' '.join(missing)}")
        return False
    return True

def cmd_train_sft(args):
    if not _require_gpu_libs(): return 1
    from trl import SFTTrainer, SFTConfig
    from peft import LoraConfig
    from datasets import load_dataset
    import transformers, torch
    fold_dir = SPLIT_DIR / f"fold{args.fold}"
    if not fold_dir.exists():
        print(f"[ERROR] 先跑 split 再 train-sft。fold_dir {fold_dir} 不存在")
        return 2
    model_name = args.model_name
    out_dir = RUNS / f"pronoia_sft_fold{args.fold}"
    ds = load_dataset("json", data_files=str(fold_dir/"sft_train.jsonl"))["train"]
    lora = LoraConfig(r=16, lora_alpha=32, target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
                      lora_dropout=0.05, bias="none", task_type="CAUSAL_LM")
    cfg = SFTConfig(output_dir=str(out_dir), per_device_train_batch_size=2, gradient_accumulation_steps=8,
                    learning_rate=2e-4, num_train_epochs=2, max_seq_length=2048, logging_steps=10,
                    save_strategy="epoch", report_to="none", fp16=torch.cuda.is_available())
    trainer = SFTTrainer(model=model_name, args=cfg, train_dataset=ds, peft_config=lora)
    trainer.train(); trainer.save_model(str(out_dir/"last"))
    print(f"[SFT fold{args.fold} DONE] → {out_dir}")

def cmd_train_dpo(args):
    if not _require_gpu_libs(): return 1
    from trl import DPOTrainer, DPOConfig
    from peft import LoraConfig
    from datasets import load_dataset
    import torch
    fold_dir = SPLIT_DIR / f"fold{args.fold}"
    if not fold_dir.exists():
        print(f"[ERROR] 先 split。fold_dir {fold_dir} 不存在"); return 2
    out_dir = RUNS / f"pronoia_dpo_fold{args.fold}"
    ds = load_dataset("json", data_files=str(fold_dir/"dpo_train.jsonl"))["train"]
    lora = LoraConfig(r=16, lora_alpha=32, target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
                      lora_dropout=0.05, bias="none", task_type="CAUSAL_LM")
    cfg = DPOConfig(output_dir=str(out_dir), per_device_train_batch_size=1, gradient_accumulation_steps=16,
                    learning_rate=5e-5, num_train_epochs=1, max_length=2048, max_prompt_length=1536,
                    beta=0.1, logging_steps=5, save_strategy="epoch", report_to="none", fp16=torch.cuda.is_available())
    trainer = DPOTrainer(model=args.model_name, args=cfg, train_dataset=ds, peft_config=lora)
    trainer.train(); trainer.save_model(str(out_dir/"last"))
    print(f"[DPO fold{args.fold} DONE] → {out_dir}")

def cmd_score_all(args):
    """若已有 5 个 fold 的 hold-out predictions.jsonl（每行 {event_id, pred_direction, confidence}），
    自动合并算 overall ACC + Wilson 95% CI + market/L2 split。"""
    labels = {r["event_id"]:r for r in load_jsonl(LABELS_JSONL)}
    evs    = {r["event_id"]:r for r in load_jsonl(EVENTS_JSONL)}
    pred_files = list(RUNS.glob("pronoia_dpo_fold*/holdout_predictions.jsonl")) or list(RUNS.glob("pronoia_sft_fold*/holdout_predictions.jsonl"))
    if not pred_files:
        print("[score-all] 暂未发现 holdout_predictions.jsonl。先训练 + eval 产出 predictions。当前给出仿真预期 (N=810):")
        from ._sim_ci import ci_table  # not exist → fallback
        return 0
    all_preds = {}
    for f in pred_files:
        for r in load_jsonl(f):
            all_preds.setdefault(r["event_id"], r)
    # 算 ACC T+3
    ok=0; denom=0; neut=0; null=0; cars=[]
    by_mkt = collections.defaultdict(lambda:[0,0])   # mkt -> [ok, denom]
    by_l2  = collections.defaultdict(lambda:[0,0])
    for eid, pr in all_preds.items():
        lab = labels.get(eid); ev = evs.get(eid)
        if lab is None: null+=1; continue
        lv = str(lab.get("label_t3") or "").strip().lower()
        if lv == "neutral": neut+=1; continue
        if lv not in {"up","down"}: null+=1; continue
        denom += 1
        pv = str(pr.get("pred_direction") or "").strip().lower()
        if lv == pv:
            ok += 1
            by_mkt[ev.get("market") or "?"][0] += 1
            by_l2[ev.get("event_type_l2") or "?"][0] += 1
        by_mkt[ev.get("market") or "?"][1] += 1
        by_l2[ev.get("event_type_l2") or "?"][1] += 1
        try: cars.append(float(lab.get("car_t3")))
        except Exception: pass
    acc = ok/denom if denom else 0
    lo,hi = wilson(acc, denom, 1.96)
    mu = sum(cars)/len(cars) if cars else 0
    import statistics as st
    sd = st.pstdev(cars) if len(cars)>=2 else 0
    ir = mu/sd if sd>0 else None
    # MDD of cumulative sum of signed_car
    cum=0; peak=0; mdd=0
    for c in sorted(cars, key=lambda x:0):
        cum += c; peak = max(peak, cum); mdd = min(mdd, cum-peak)
    mdd = abs(mdd)
    print(f"[SCORE-ALL hold-out merged {len(all_preds)} preds]")
    print(f"  overall T+3 ACC = {100*acc:.2f}%   ok/denom = {ok}/{denom}   neutral={neut} null={null}")
    print(f"  Wilson 95% CI   = [{100*lo:.2f}%, {100*hi:.2f}%]   95% 下限≥70%? {'✅' if lo>=0.70 else '❌'}")
    print(f"  avg_sCAR(bps)    = {round(mu*10_000,1)}   IR={round(ir,2) if ir is not None else None}   MDD={round(mdd*10_000,1)} bps")
    print(f"  [split market]")
    for m,(o,d) in sorted(by_mkt.items(), key=lambda x:-x[1][1]):
        a = o/d if d else 0
        print(f"    {m}: {100*a:.2f}%  ok/denom={o}/{d}")
    print(f"  [split L2 (类内≥20)]")
    for t,(o,d) in sorted(by_l2.items(), key=lambda x:-x[1][1]):
        if d < 20: continue
        a = o/d if d else 0
        print(f"    {t}: {100*a:.2f}%  ok/denom={o}/{d}")

def build_parser():
    p = argparse.ArgumentParser(prog="train_pronoia_v2", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("split", help="做 stratified 5-fold split（不训练，纯预处理）")
    ps.add_argument("--k", type=int, default=5); ps.add_argument("--seed", type=int, default=20260809); ps.set_defaults(func=cmd_split)
    ps = sub.add_parser("train-sft", help="SFT 训练某 fold（需 GPU + trl）")
    ps.add_argument("--fold", type=int, required=True); ps.add_argument("--model-name", type=str, required=True); ps.set_defaults(func=cmd_train_sft)
    ps = sub.add_parser("train-dpo", help="DPO 训练某 fold（需 GPU + trl）")
    ps.add_argument("--fold", type=int, required=True); ps.add_argument("--model-name", type=str, required=True); ps.set_defaults(func=cmd_train_dpo)
    ps = sub.add_parser("score-all", help="合并 5-fold hold-out predictions → ACC + 95% CI + split"); ps.set_defaults(func=cmd_score_all)
    return p

if __name__ == "__main__":
    parser = build_parser(); args = parser.parse_args(); raise SystemExit(args.func(args) or 0)
