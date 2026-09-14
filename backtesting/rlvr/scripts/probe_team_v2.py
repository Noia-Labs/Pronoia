"""probe_team_v2.py — 用 v2 真实事件跑 Team full 前置 Agent（PLAN → FAN-OUT → Synthesize）探针。

选 3 条多样化真实事件（CN 业绩预告首亏 / CN 要约收购 / US FOMC），
FAST 模式（market_analyst + fundamentals_analyst + deep_researcher，跳过 hypothesis/verify），
落盘完整 trajectory ckpt + 打印研究上下文摘要，供人工检查前置推理质量。

用法：
    cd /workspace/backend
    PRONOIA_BT_FAST=1 python3 /workspace/backtesting/rlvr/scripts/probe_team_v2.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND = Path("/workspace/backend")
sys.path.insert(0, str(BACKEND))

EVENTS_JSONL = Path("/workspace/backtesting/rlvr/data/rlvr_train_v2_real/events.jsonl")
CKPT_DIR = Path("/workspace/backtesting/rlvr/data/team_probe_v2_ckpt")
OUT_PREDS = Path("/workspace/backtesting/rlvr/data/probe_v2_preds.jsonl")

# 3 条探针事件（真实 v2 事件流，多样化场景）
PROBE_EVENT_IDS = [
    "trn2_cn_600193_e5298fa5",   # CN 业绩预告（首亏）退市创兴 2024-07-10
    "trn2_cn_300087_2f370b69",   # CN 要约收购完成过户 荃银高科 2026-01-09
    "trn2_us_xlu_878f7f87",      # US FOMC 维持利率 XLU 2024-05-01
]


async def main() -> None:
    os.environ["PRONOIA_BT_FAST"] = "1"  # 前置 Agent 白名单 + 跳过 hypothesis/verify

    from app.event_backtest.engine import run_team_full_one_event
    from app.event_backtest.models import EventRecord

    events = {}
    with open(EVENTS_JSONL, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            events[d["event_id"]] = d

    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    preds = []
    for eid in PROBE_EVENT_IDS:
        raw = events[eid]
        ev = EventRecord.from_dict(raw)
        print(f"\n{'='*100}\n>>> PROBE {eid}  {ev.market}/{ev.symbol}  {ev.event_time}  {ev.event_type_l2}\n    title: {ev.title[:80]}", flush=True)
        p = await run_team_full_one_event(
            ev,
            run_id="rlvr_probe_v2",
            model_version="team-full-v2-probe",
            trajectory_ckpt_dir=CKPT_DIR,
        )
        preds.append({
            "event_id": p.event_id,
            "pred_direction": p.pred_direction,
            "confidence": p.confidence,
            "rationale": p.rationale,
            "abstain": p.abstain,
        })
        print(f"    => direction={p.pred_direction}  conf={p.confidence:.3f}", flush=True)

    with open(OUT_PREDS, "w", encoding="utf-8") as f:
        for p in preds:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"\n已生成 {len(preds)} 条 predictions -> {OUT_PREDS}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
