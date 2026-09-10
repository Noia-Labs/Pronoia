#!/usr/bin/env python3
"""Build an auditable >=70 offline direction-prediction event set.

The score measures whether an event packet is fit for a direction decision. It
does not encode the answer (up/down/neutral), and it never upgrades a failed
hard gate merely because the numerical score is high.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


SCORING_VERSION = "direction-readiness-v1"
DATASET_SCOPE = "offline_backfill"

PROCEDURAL = re.compile(
    r"持续督导|法律意见|财务顾问报告|核查意见|会议资料|暂停转股|"
    r"转股价格调整|现金管理|到期赎回|持有比例变动|兑付及摘牌|信托登记",
    re.I,
)
STAGE = re.compile(
    r"预案|草案|报告书|要约收购|发行结果|终止|获准|完成交割|8-K|10-Q|10-K|业绩预告|业绩快报",
    re.I,
)
NUMBER = re.compile(r"(?:\d[\d,.]*\s*(?:%|亿元|万元|美元|元|股|吨|份))")


def has(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text, re.I))


def unified_type(event: dict[str, Any]) -> str:
    legacy = str(event.get("event_type_l2") or "")
    if "财报" in legacy or "业绩" in legacy:
        return "财务业绩与指引"
    title = str(event.get("title") or "")
    if has(title, r"公司债|债券发行|再融资") and not has(title, r"购买资产|重组|收购"):
        return "融资与债务"
    if "并购" in legacy or "分拆" in legacy or "再融资" in legacy:
        return "并购重组与资产交易"
    return legacy or "其他待分类"


def score_event(event: dict[str, Any], threshold: int) -> dict[str, Any]:
    title = str(event.get("title") or "")
    text = str(event.get("event_text") or "")
    pool = f"{title}\n{text}"
    event_type = unified_type(event)
    failures: list[str] = []

    if not event.get("quality_pass"):
        failures.append("source_body_identity_or_packet_quality_failed")
    if str(event.get("evaluation_status") or "") != "strict":
        failures.append("novel_substantive_event_not_confirmed")
    if len(text.strip()) < 160:
        failures.append("body_missing_or_too_short")
    if not event.get("source_url"):
        failures.append("source_not_traceable")
    if not event.get("market") or not event.get("symbol"):
        failures.append("security_identity_missing")
    if not event.get("effective_session") or not event.get("prediction_cutoff_at"):
        failures.append("effective_time_missing")
    if not event.get("benchmark"):
        failures.append("benchmark_or_tradability_missing")
    if str(event.get("evaluation_status") or "") in {"duplicate", "exclude"} or PROCEDURAL.search(title):
        failures.append("duplicate_or_procedural_document")

    # Required-field gate is intentionally semantic but deterministic. It tests
    # for field families, not for a direction implied by those fields.
    if event_type == "并购重组与资产交易":
        field_families = {
            "stage": has(pool, r"预案|草案|报告书|交割|过户|终止|要约"),
            "parties": has(pool, r"交易对方|收购人|转让方|受让方"),
            "asset": has(pool, r"标的(?:资产|公司|股权)?|购买资产|股份变动"),
            "terms": has(pool, r"对价|发行价格|要约价格|支付方式|交易价格|作价"),
        }
        if sum(field_families.values()) < 3:
            failures.append("category_required_fields_incomplete")
    elif event_type == "财务业绩与指引":
        field_families = {
            "period": has(pool, r"quarter|季度|年度|截至|three months|six months"),
            "actual": has(pool, r"revenue|net income|营收|营业收入|净利润"),
            "comparison": has(pool, r"同比|环比|year.over.year|compared with|increase|decrease"),
            "expectation": has(pool, r"guidance|outlook|预计|预期|指引|forecast"),
        }
        if not field_families["period"] or not field_families["actual"]:
            failures.append("category_required_fields_incomplete")
    else:
        field_families = {"title_stage": bool(STAGE.search(title)), "numeric_terms": bool(NUMBER.search(pool))}

    body_points = 12 if len(text) >= 1000 else 10 if len(text) >= 500 else 7
    evidence_integrity = min(20, body_points + (3 if event.get("source_url") else 0)
                             + (3 if event.get("symbol") else 0)
                             + (2 if event.get("source_document_hash") else 0))

    novelty = 0
    if str(event.get("evaluation_status") or "") == "strict":
        novelty += 10
    elif str(event.get("evaluation_status") or "") == "review":
        novelty += 5
    novelty += 6 if STAGE.search(title) else 2
    if has(title, r"差异|修订|更新"):
        novelty += 2
    elif not PROCEDURAL.search(title):
        novelty += 4
    novelty = min(20, novelty)

    if event_type == "并购重组与资产交易":
        materiality = (
            5 * int(field_families.get("parties", False))
            + 5 * int(field_families.get("asset", False))
            + 5 * int(field_families.get("terms", False))
            + 5 * int(has(pool, r"%|占.*比例|控制权|实际控制人"))
        )
        surprise = (
            5 * int(field_families.get("stage", False))
            + 5 * int(has(pool, r"变更|差异|修订|新增|终止|无偿划转|免于发出要约"))
            + 5 * int(has(pool, r"发行价格|要约价格|溢价|折价|控制权"))
        )
        direction_clarity = (
            5 * int(field_families.get("terms", False))
            + 5 * int(has(pool, r"控制权|主营业务|收入|利润|协同|同业竞争"))
            + 5 * int(has(pool, r"发行股份|现金支付|募集配套资金|负债|摊薄"))
        )
    elif event_type == "财务业绩与指引":
        materiality = min(20, 5 * sum(bool(v) for v in field_families.values())
                          + 5 * int(bool(NUMBER.search(pool))))
        surprise = min(15, 5 * int(field_families.get("comparison", False))
                       + 5 * int(field_families.get("expectation", False))
                       + 5 * int(has(pool, r"above|below|超出|低于|上调|下调")))
        direction_clarity = min(15, 5 * int(field_families.get("actual", False))
                                + 5 * int(field_families.get("comparison", False))
                                + 5 * int(field_families.get("expectation", False)))
    else:
        materiality = min(20, 10 * int(bool(NUMBER.search(pool))) + 5 * int(bool(STAGE.search(title))))
        surprise = min(15, 5 * int(has(pool, r"首次|新增|终止|获准|完成|变更")))
        direction_clarity = min(15, 5 * int(has(pool, r"收入|利润|现金流|稀释|负债|控制权|风险")))

    timing = min(10, 4 * int(bool(event.get("published_at") or event.get("event_time")))
                 + 3 * int(bool(event.get("effective_session")))
                 + 3 * int(bool(event.get("benchmark"))))
    breakdown = {
        "evidence_integrity": evidence_integrity,
        "novelty": novelty,
        "materiality": materiality,
        "surprise": surprise,
        "direction_causal_clarity": direction_clarity,
        "timing_and_tradability": timing,
    }
    total = sum(breakdown.values())
    hard_gate_pass = not failures
    return {
        "scoring_version": SCORING_VERSION,
        "dataset_scope": DATASET_SCOPE,
        "normalized_event_type": event_type,
        "hard_gate_pass": hard_gate_pass,
        "hard_gate_failures": sorted(set(failures)),
        "score_breakdown": breakdown,
        "total_score": total,
        "score_threshold": threshold,
        "selected": hard_gate_pass and total >= threshold,
        "live_online_eligible": bool(event.get("prediction_eligible")),
        "category_field_presence": field_families,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--threshold", type=int, default=70)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    events: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM events ORDER BY cohort_date, market, symbol, event_id"):
        event = dict(row)
        event.pop("event_json", None)
        assessment = score_event(event, args.threshold)
        event["direction_readiness"] = assessment
        events.append(event)

    selected = [event for event in events if event["direction_readiness"]["selected"]]
    rejected = [event for event in events if not event["direction_readiness"]["selected"]]
    write_jsonl(out_dir / "all_scored_event_packets.jsonl", events)
    write_jsonl(out_dir / f"direction_prediction_events_ge{args.threshold}.jsonl", selected)
    write_jsonl(out_dir / f"rejected_or_below_{args.threshold}.jsonl", rejected)

    conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    scored_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO prediction_event_eligibility(
            event_id, scoring_version, dataset_scope, hard_gate_pass, total_score,
            score_threshold, selected, score_breakdown_json,
            hard_gate_failures_json, assessment_json, scored_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id, scoring_version) DO UPDATE SET
            hard_gate_pass=excluded.hard_gate_pass,
            total_score=excluded.total_score,
            score_threshold=excluded.score_threshold,
            selected=excluded.selected,
            score_breakdown_json=excluded.score_breakdown_json,
            hard_gate_failures_json=excluded.hard_gate_failures_json,
            assessment_json=excluded.assessment_json,
            scored_at=excluded.scored_at
        """,
        [(
            event["event_id"], SCORING_VERSION, DATASET_SCOPE,
            int(event["direction_readiness"]["hard_gate_pass"]),
            event["direction_readiness"]["total_score"], args.threshold,
            int(event["direction_readiness"]["selected"]),
            json.dumps(event["direction_readiness"]["score_breakdown"], ensure_ascii=False),
            json.dumps(event["direction_readiness"]["hard_gate_failures"], ensure_ascii=False),
            json.dumps(event["direction_readiness"], ensure_ascii=False), scored_at,
        ) for event in events],
    )
    conn.commit()

    failures = Counter(
        reason for event in rejected
        for reason in event["direction_readiness"]["hard_gate_failures"]
    )
    type_counts = Counter(event["direction_readiness"]["normalized_event_type"] for event in selected)
    report = [
        f"# 方向预测事件集（门槛 ≥{args.threshold}）", "",
        f"- 评分版本：`{SCORING_VERSION}`",
        f"- 数据范围：`{DATASET_SCOPE}`（事后回填，只用于离线评测）",
        f"- 已有正文 event packet：**{len(events)}**",
        f"- 通过硬门槛且得分 ≥{args.threshold}：**{len(selected)}**",
        f"- 未入选：**{len(rejected)}**", "",
        "> 该评分只判断 packet 是否足以做方向判断，不生成或暗示 up/down/neutral。", "",
        "## 入选事件", "",
        "| 得分 | 日期 | 市场 | 标的 | 统一类别 | 事件 | Live online 合格 |", "|---:|---|---|---|---|---|---|",
    ]
    for event in sorted(selected, key=lambda e: (-e["direction_readiness"]["total_score"], e["event_id"])):
        a = event["direction_readiness"]
        report.append(
            f"| {a['total_score']} | {event['cohort_date']} | {event['market']} | {event['symbol']} | "
            f"{a['normalized_event_type']} | {event['title']} | {a['live_online_eligible']} |"
        )
    report.extend(["", "## 入选类别", "", "| 类别 | 数量 |", "|---|---:|"])
    report.extend(f"| {name} | {count} |" for name, count in type_counts.most_common())
    report.extend(["", "## 主要硬门槛失败原因", "", "| 原因 | 次数 |", "|---|---:|"])
    report.extend(f"| `{name}` | {count} |" for name, count in failures.most_common())
    report.extend([
        "", "## 使用限制", "",
        "这些记录是事后回填，`live_online_eligible=false`；可用于 prompt/agent 离线 A/B，不能计入正式无泄漏 Online ACC。",
        "目前全量目录中绝大多数记录没有正文，因此本集合只从数据库里已有正文的 event packet 构造。",
    ])
    (out_dir / "direction_prediction_set_report.md").write_text("\n".join(report), encoding="utf-8")
    summary = {
        "scoring_version": SCORING_VERSION,
        "threshold": args.threshold,
        "scored": len(events),
        "selected": len(selected),
        "rejected": len(rejected),
        "selected_types": dict(type_counts),
        "hard_gate_failures": dict(failures),
        "output": str(out_dir / f"direction_prediction_events_ge{args.threshold}.jsonl"),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
