"""team_research_batch.py — 全量事件批量 Team full 前置推理（PLAN → FAN-OUT → Synthesize）。

背景：v3 的 research_cache.jsonl 是程序化统计替身，未经 LLM Team 推理。
本脚本用真 Team Agent（FAST 模式：market_analyst + fundamentals_analyst + deep_researcher）
对全量事件离线生成深度研究上下文，供 v4 数据组装与 A/B 对比训练。

特性：
  · 并发：asyncio.Semaphore（--concurrency，默认 20）
  · 断点续跑：输出 jsonl 按 event_id 去重，重启自动跳过已完成
  · 失败重试 1 次，仍失败落 error 行（ok=false），不阻塞整体
  · trajectory ckpt 全量落盘（审计/深挖专家输出用）
  · 进度：每 10 条打印 done/total + 已用时 + ETA

用法：
    cd /workspace/backend
    nohup python3 /workspace/backtesting/rlvr/scripts/team_research_batch.py \
        --events /workspace/backtesting/rlvr/data/audit/events_enriched_v3.jsonl \
        --out /workspace/backtesting/rlvr/data/audit/research_cache_team.jsonl \
        --ckpt-dir /workspace/backtesting/rlvr/data/team_traj_v3 \
        --concurrency 8 > /workspace/backtesting/rlvr/data/audit/team_batch.log 2>&1 &
    # 试跑：--limit 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3] / "backend"  # 自动定位 repo/backend
sys.path.insert(0, str(BACKEND))


def read_jsonl(p: Path) -> list[dict]:
    rows = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows


async def run_one(engine_mod, models_mod, raw: dict, ckpt_dir: Path,
                  run_id: str, sem: asyncio.Semaphore, per_call_timeout: float) -> dict:
    eid = raw["event_id"]
    t0 = time.time()
    async with sem:
        for attempt in (1, 2):  # 重试 1 次
            try:
                ev = models_mod.EventRecord.from_dict(raw)
                p = await asyncio.wait_for(
                    engine_mod.run_team_full_one_event(
                        ev, run_id=run_id, model_version="team-full-v4",
                        trajectory_ckpt_dir=ckpt_dir,
                    ),
                    timeout=per_call_timeout,
                )
                return {
                    "event_id": eid,
                    "ok": True,
                    "direction": str(p.pred_direction),
                    "confidence": p.confidence,
                    "rationale": (p.rationale or "")[:4000],
                    "horizons": p.horizons,
                    "abstain": bool(p.abstain),
                    "wall_sec": round(time.time() - t0, 1),
                    "attempt": attempt,
                    "error": None,
                }
            except asyncio.TimeoutError as e:
                msg = f"TimeoutError: >{per_call_timeout:.0f}s on attempt {attempt}"
                if attempt == 2:
                    return {
                        "event_id": eid, "ok": False, "direction": None,
                        "confidence": None, "rationale": None, "abstain": None,
                        "wall_sec": round(time.time() - t0, 1),
                        "attempt": attempt, "error": msg[:300],
                    }
                await asyncio.sleep(3)
            except Exception as e:
                if attempt == 2:
                    return {
                        "event_id": eid, "ok": False, "direction": None,
                        "confidence": None, "rationale": None, "abstain": None,
                        "wall_sec": round(time.time() - t0, 1),
                        "attempt": attempt, "error": f"{type(e).__name__}: {e}"[:300],
                    }
                await asyncio.sleep(3)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ckpt-dir", required=True)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=3600,
                    help="每attempt LLM调用超时秒（默认3600=1小时，仅砍掉异常慢请求）")
    ap.add_argument("--limit", type=int, default=0, help="0=全量；试跑用")
    ap.add_argument("--only-ids", default="", help="逗号分隔 event_id 白名单（调试用）")
    args = ap.parse_args()

    os.environ["PRONOIA_BT_FAST"] = "1"  # 3 专家白名单 + 跳过 hypothesis/verify

    from app.event_backtest import engine as engine_mod
    from app.event_backtest import models as models_mod

    events = read_jsonl(Path(args.events))
    if args.only_ids:
        allow = set(args.only_ids.split(","))
        events = [e for e in events if e["event_id"] in allow]
    if args.limit > 0:
        events = events[:args.limit]

    out_path = Path(args.out)
    done_ids: set[str] = set()
    if out_path.exists():
        for r in read_jsonl(out_path):
            if r.get("ok"):
                done_ids.add(str(r.get("event_id")))
        print(f"[RESUME] 已完成 {len(done_ids)} 条，跳过", flush=True)

    todo = [e for e in events if e["event_id"] not in done_ids]
    print(f"[PLAN] total={len(events)} todo={len(todo)} "
          f"concurrency={args.concurrency} per_call_timeout={args.timeout:.0f}s", flush=True)
    if not todo:
        print("[DONE] 全部完成", flush=True)
        return

    ckpt_dir = Path(args.ckpt_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(args.concurrency)
    out_f = open(out_path, "a", encoding="utf-8")
    n_ok = n_err = 0
    t_start = time.time()

    async def worker(raw: dict, idx: int):
        nonlocal n_ok, n_err
        r = await run_one(engine_mod, models_mod, raw, ckpt_dir,
                          "rlvr_team_v3", sem, args.timeout)
        out_f.write(json.dumps(r, ensure_ascii=False) + "\n")
        out_f.flush()
        if r["ok"]:
            n_ok += 1
        else:
            n_err += 1
        if (n_ok + n_err) % 10 == 0:
            el = time.time() - t_start
            per = el / (n_ok + n_err)
            eta = per * (len(todo) - n_ok - n_err) / max(args.concurrency, 1)
            print(f"[PROG] {n_ok + n_err}/{len(todo)} ok={n_ok} err={n_err} "
                  f"el={el/60:.1f}min eta={eta/60:.1f}min", flush=True)

    tasks = [worker(e, i) for i, e in enumerate(todo)]
    await asyncio.gather(*tasks)
    out_f.close()
    print(f"[DONE] ok={n_ok} err={n_err} total_wall={(time.time()-t_start)/60:.1f}min",
          flush=True)


if __name__ == "__main__":
    asyncio.run(main())
