#!/usr/bin/env python3
"""Screen the complete event catalog without reading announcement bodies."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


SCREENING_VERSION = "metadata-direction-screen-v1"

NOVEL_HIGH = re.compile(
    r"首次|预案|草案|要约收购|收购报告书|重大资产重组|终止|处罚决定|立案|"
    r"中标|重大合同|业绩预告|业绩快报|上调|下调|控制权变更|退市",
    re.I,
)
NOVEL_MEDIUM = re.compile(r"完成|获准|批准|注册证|发行结果|签订|计划|拟|申请|增持|减持", re.I)
ROUTINE = re.compile(
    r"进展公告|进展情况|实施公告|提示性公告|会议资料|决议公告|法律意见|"
    r"核查意见|专项说明|调研活动|说明会|持续督导|现金管理|到期赎回|英文|"
    r"承诺函|评估报告|估值报告|修订说明|更正公告|更新后|第.{0,4}次.*风险提示|"
    r"票面利率公告|簿记建档|债券持有人.*比例变动|路演公告",
    re.I,
)
HIGH_MATERIALITY = re.compile(
    r"重大|控制权|实际控制人|实控人|要约|重组|业绩|处罚|退市|中标|重大合同|终止",
    re.I,
)
MEDIUM_MATERIALITY = re.compile(r"发行|融资|减持|增持|回购|分红|诉讼|仲裁|注册证|获批", re.I)

CATEGORY_CHANNEL = {
    "财务业绩与指引": 18,
    "并购重组与资产交易": 18,
    "融资与债务": 14,
    "股东持股与控制权": 14,
    "回购分红与股本": 12,
    "经营订单与项目": 15,
    "产品研发与许可": 8,
    "监管法律与重大风险": 17,
    "治理人事与激励": 5,
    "交易状态与市场异动": 3,
    "宏观政策与行业冲击": 18,
    "信息沟通与程序文件": 0,
    "其他待分类": 0,
}


def score(row: dict[str, Any], threshold: int) -> dict[str, Any]:
    title = str(row.get("title") or "")
    native = str(row.get("native_event_type") or "")
    category = str(row.get("normalized_event_type") or "其他待分类")
    pool = f"{native} {title}"

    metadata_integrity = (
        5 * int(bool(title))
        + 5 * int(bool(row.get("source_url")))
        + 5 * int(bool(row.get("market") and row.get("symbol")))
        + 5 * int(bool(row.get("announcement_date") and row.get("issuer_name")))
    )
    role = str(row.get("catalog_role") or "")
    category_predictability = {"prediction_candidate": 20, "review": 10, "evidence_only": 0}.get(role, 0)

    if ROUTINE.search(pool):
        novelty = 2
    elif NOVEL_HIGH.search(pool):
        novelty = 20
    elif NOVEL_MEDIUM.search(pool):
        novelty = 14
    else:
        novelty = 8

    direction_channel = CATEGORY_CHANNEL.get(category, 0)
    if re.search(r"上调|下调|增持|减持|中标|重大合同|终止|处罚|立案|要约|收购|回购|分红|获批", pool, re.I):
        direction_channel = min(20, direction_channel + 3)
    if category == "产品研发与许可" and re.search(r"上市批准|注册批准|主要终点.*(?:达到|未达到)|III期.*结果|许可协议", pool, re.I):
        direction_channel = min(20, direction_channel + 7)
    if category == "治理人事与激励" and re.search(r"董事长|总经理|CEO|CFO|财务负责人|实际控制人|控制权", pool, re.I):
        direction_channel = min(20, direction_channel + 7)

    if HIGH_MATERIALITY.search(pool):
        materiality_proxy = 10
    elif MEDIUM_MATERIALITY.search(pool):
        materiality_proxy = 6
    else:
        materiality_proxy = 2
    timing_tradability = (
        4 * int(bool(row.get("announcement_date")))
        + 4 * int(bool(row.get("market") and row.get("symbol")))
        + 2 * int(bool(row.get("source_url")))
    )

    breakdown = {
        "metadata_integrity": metadata_integrity,
        "category_predictability": category_predictability,
        "novelty_stage": novelty,
        "direction_channel_clarity": direction_channel,
        "materiality_proxy": materiality_proxy,
        "timing_and_tradability": timing_tradability,
    }
    failures: list[str] = []
    if metadata_integrity < 20:
        failures.append("metadata_identity_incomplete")
    if role == "evidence_only" or ROUTINE.search(pool):
        failures.append("routine_or_evidence_only")
    if category == "其他待分类":
        failures.append("event_category_unresolved")
    if direction_channel < 10:
        failures.append("direction_channel_too_weak_from_metadata")
    total = sum(breakdown.values())
    hard_gate_pass = not failures
    return {
        "screening_version": SCREENING_VERSION,
        "body_used": False,
        "score_threshold": threshold,
        "score_breakdown": breakdown,
        "total_score": total,
        "hard_gate_pass": hard_gate_pass,
        "hard_gate_failures": sorted(set(failures)),
        "selected": hard_gate_pass and total >= threshold,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def collapse_document_families(rows: list[dict[str, Any]]) -> None:
    """Collapse obvious multi-document disclosures without reading bodies.

    This is intentionally conservative: only M&A and financial-report records
    for the same security/date are grouped. Other classes may contain multiple
    genuinely different events on one day.
    """
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if not row["metadata_screening"]["selected"]:
            continue
        category = str(row.get("normalized_event_type") or "")
        if category not in {"并购重组与资产交易", "财务业绩与指引"}:
            continue
        key = (
            str(row.get("market") or ""), str(row.get("symbol") or ""),
            str(row.get("announcement_date") or ""), category,
        )
        groups.setdefault(key, []).append(row)

    def rank(row: dict[str, Any]) -> tuple[int, int, int]:
        title = str(row.get("title") or "")
        penalty = 0
        if re.search(r"摘要", title):
            penalty += 10
        if re.search(r"承诺函|评估报告|估值报告|财务顾问|法律意见|修订说明|差异.*说明|问询.*回复", title):
            penalty += 30
        return (-penalty, row["metadata_screening"]["total_score"], len(title))

    for members in groups.values():
        if len(members) < 2:
            continue
        primary = max(members, key=rank)
        related = [row["catalog_event_id"] for row in members if row is not primary]
        primary["metadata_related_records"] = related
        for duplicate in members:
            if duplicate is primary:
                continue
            screening = duplicate["metadata_screening"]
            screening["selected"] = False
            screening["hard_gate_pass"] = False
            screening["hard_gate_failures"] = sorted(
                set(screening["hard_gate_failures"] + ["metadata_event_family_duplicate"])
            )
            screening["duplicate_of"] = primary["catalog_event_id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--threshold", type=int, default=50)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows: list[dict[str, Any]] = []
    for record in conn.execute("SELECT * FROM v_all_event_catalog ORDER BY cohort_date, market, symbol, catalog_event_id"):
        item = dict(record)
        item["metadata_screening"] = score(item, args.threshold)
        rows.append(item)
    collapse_document_families(rows)
    selected = [row for row in rows if row["metadata_screening"]["selected"]]

    write_jsonl(out_dir / "all_metadata_scored_events.jsonl", rows)
    write_jsonl(out_dir / f"metadata_direction_candidates_ge{args.threshold}.jsonl", selected)

    conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    scored_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO metadata_event_screening(
            catalog_event_id, screening_version, score_threshold, total_score,
            hard_gate_pass, selected, score_breakdown_json,
            hard_gate_failures_json, screening_json, scored_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(catalog_event_id, screening_version) DO UPDATE SET
            score_threshold=excluded.score_threshold,
            total_score=excluded.total_score,
            hard_gate_pass=excluded.hard_gate_pass,
            selected=excluded.selected,
            score_breakdown_json=excluded.score_breakdown_json,
            hard_gate_failures_json=excluded.hard_gate_failures_json,
            screening_json=excluded.screening_json,
            scored_at=excluded.scored_at
        """,
        [(
            row["catalog_event_id"], SCREENING_VERSION, args.threshold,
            row["metadata_screening"]["total_score"],
            int(row["metadata_screening"]["hard_gate_pass"]),
            int(row["metadata_screening"]["selected"]),
            json.dumps(row["metadata_screening"]["score_breakdown"], ensure_ascii=False),
            json.dumps(row["metadata_screening"]["hard_gate_failures"], ensure_ascii=False),
            json.dumps(row["metadata_screening"], ensure_ascii=False), scored_at,
        ) for row in rows],
    )
    conn.commit()

    by_category = Counter(row["normalized_event_type"] for row in selected)
    by_market = Counter(row["market"] for row in selected)
    failure_counts = Counter(
        reason for row in rows if not row["metadata_screening"]["selected"]
        for reason in row["metadata_screening"]["hard_gate_failures"]
    )
    report = [
        f"# 元数据方向候选集（≥{args.threshold}，不读取正文）", "",
        f"- 全量目录：**{len(rows)}**",
        f"- 入选：**{len(selected)}**",
        f"- 未入选：**{len(rows) - len(selected)}**",
        f"- 评分版本：`{SCREENING_VERSION}`", "",
        "> 本集合只用于决定哪些事件值得进入 Agent/正文补齐流程；不能据此直接给出方向，也不能作为证据充分度分数。", "",
        "## 评分维度", "",
        "| 维度 | 满分 |", "|---|---:|",
        "| 元数据与证券身份完整性 | 20 |",
        "| 类别可预测性 | 20 |",
        "| 标题所示新颖阶段 | 20 |",
        "| 方向传导机制清晰度 | 20 |",
        "| 实质性标题代理 | 10 |",
        "| 时间与可交易性 | 10 |", "",
        "## 入选分布", "", "| 类别 | 数量 |", "|---|---:|",
    ]
    report.extend(f"| {name} | {count} |" for name, count in by_category.most_common())
    report.extend(["", "| 市场 | 数量 |", "|---|---:|"])
    report.extend(f"| {name} | {count} |" for name, count in by_market.most_common())
    report.extend(["", "## 硬排除原因", "", "| 原因 | 次数 |", "|---|---:|"])
    report.extend(f"| `{name}` | {count} |" for name, count in failure_counts.most_common())
    report.extend([
        "", "## 重要限制", "",
        "标题中的“重大”等词只是实质性代理，不能替代金额/营收、市值、净资产等正文分母。",
        "因此该集合适合扩大召回和安排后续研究，不适合直接计算方向 ACC。",
    ])
    (out_dir / "metadata_screening_report.md").write_text("\n".join(report), encoding="utf-8")
    summary = {
        "screening_version": SCREENING_VERSION,
        "threshold": args.threshold,
        "body_used": False,
        "catalog_events": len(rows),
        "selected": len(selected),
        "rejected": len(rows) - len(selected),
        "selected_by_category": dict(by_category),
        "selected_by_market": dict(by_market),
        "output": str(out_dir / f"metadata_direction_candidates_ge{args.threshold}.jsonl"),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
