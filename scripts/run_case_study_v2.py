"""Run case_study_v2 19 条：走 team_full 新管线，落盘 preds + trajectory ckpt。

用法：
  cd PRONOIA
  PYTHONPATH=$(pwd)/backend backend/.venv/bin/python scripts/run_case_study_v2.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "backend"))

from app.event_backtest.engine import run_team_full_trajectory   # noqa: E402
from app.event_backtest.models import EventRecord               # noqa: E402


def load_jsonl(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


async def main() -> None:
    events_path = DATA / "events_case_study_v2.jsonl"
    labels_path = DATA / "labels_case_study_v2.jsonl"
    out_preds = DATA / "preds_case_study_v2_new_pipeline.jsonl"
    ckpt_dir = DATA / "_trajectory_ckpt_case_study_v2"
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    events_list = load_jsonl(events_path)
    labels_list = load_jsonl(labels_path)
    labels = {l["event_id"]: l for l in labels_list}
    print(f"加载事件 {len(events_list)} 条, labels {len(labels)} 条")

    records = []
    for e in events_list:
        kw = {k: v for k, v in e.items() if k in EventRecord.__dataclass_fields__}
        # label 字段也塞进 EventRecord（供某些路由使用）
        if e["event_id"] in labels and "label" in EventRecord.__dataclass_fields__:
            kw["label"] = labels[e["event_id"]]
        records.append(EventRecord(**kw))

    t0 = time.time()
    print(f"开始跑 team_full_trajectory, concurrency=3, ckpt_dir={ckpt_dir}")

    preds = await run_team_full_trajectory(
        records,
        run_id="case_study_v2_tf",
        model_version="team-full-v2-skills",
        concurrency=3,
        trajectory_ckpt_dir=ckpt_dir,
        system_prompt_variant="v0",
    )

    # 写 preds jsonl
    with open(out_preds, "w", encoding="utf-8") as f:
        for p in preds:
            f.write(json.dumps({
                "event_id": p.event_id,
                "pred_direction": p.pred_direction,
                "confidence": p.confidence,
                "run_id": p.run_id,
                "model_version": p.model_version,
                "reasoning": (p.rationale or "")[:300],
            }, ensure_ascii=False) + "\n")

    dt = time.time() - t0
    print(f"\n完成: {len(preds)} 条预测, 耗时 {dt:.1f}s")
    print(f"preds 输出: {out_preds}")
    print(f"ckpt 输出: {ckpt_dir} (存在 {sum(1 for _ in ckpt_dir.glob('*.json'))} 个)")


if __name__ == "__main__":
    asyncio.run(main())
