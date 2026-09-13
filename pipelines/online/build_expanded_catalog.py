#!/usr/bin/env python3
"""Classify every stored source record into an all-event research catalog."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
from typing import Any

from event_taxonomy import MATERIALITY_ANCHORS, PROFILES, classify_event


CATALOG_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS classified_event_catalog (
    catalog_event_id TEXT PRIMARY KEY,
    cohort_date TEXT NOT NULL,
    source_record_id TEXT NOT NULL UNIQUE,
    market TEXT,
    symbol TEXT,
    native_event_type TEXT,
    normalized_event_type TEXT NOT NULL,
    catalog_role TEXT NOT NULL
        CHECK (catalog_role IN ('prediction_candidate', 'review', 'evidence_only')),
    information_tier TEXT NOT NULL,
    packet_status TEXT NOT NULL,
    required_fields_json TEXT NOT NULL,
    decision_checks_json TEXT NOT NULL,
    preferred_horizons_json TEXT NOT NULL,
    classification_json TEXT NOT NULL,
    FOREIGN KEY (cohort_date) REFERENCES cohorts(cohort_date),
    FOREIGN KEY (source_record_id) REFERENCES source_records(record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_classified_catalog_queue
    ON classified_event_catalog(catalog_role, normalized_event_type, cohort_date);
CREATE INDEX IF NOT EXISTS idx_classified_catalog_symbol
    ON classified_event_catalog(market, symbol, cohort_date);
CREATE VIEW IF NOT EXISTS v_all_event_catalog AS
SELECT c.*, s.source, s.source_key, s.issuer_name, s.announcement_date,
       s.title, s.source_url
FROM classified_event_catalog AS c
JOIN source_records AS s ON s.record_id = c.source_record_id;
CREATE VIEW IF NOT EXISTS v_prediction_candidate_catalog AS
SELECT * FROM v_all_event_catalog WHERE catalog_role = 'prediction_candidate';
CREATE VIEW IF NOT EXISTS v_event_catalog_review_queue AS
SELECT * FROM v_all_event_catalog WHERE catalog_role = 'review';
CREATE VIEW IF NOT EXISTS v_evidence_only_catalog AS
SELECT * FROM v_all_event_catalog WHERE catalog_role = 'evidence_only';
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
            """
            SELECT s.*, c.event_type_l2 AS legacy_event_type_l2,
                   c.quality_score AS legacy_quality_score,
                   c.document_fetch_ok AS document_fetch_ok,
                   c.document_fetch_error AS document_fetch_error
            FROM source_records AS s
            LEFT JOIN event_candidates AS c ON c.source_record_id = s.record_id
            ORDER BY s.cohort_date, s.record_id
            """
        ).fetchall()

    catalog: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        native = str(row["announcement_type"] or "")
        classified = classify_event(native, str(row["title"] or ""), str(row["market"] or ""))
        if row["legacy_event_type_l2"] is None:
            packet_status = "metadata_only"
        elif row["document_fetch_ok"]:
            packet_status = "document_fetched"
        elif row["document_fetch_error"] == "skipped_below_quality_threshold":
            packet_status = "document_skipped_by_legacy_score"
        else:
            packet_status = "document_fetch_failed_or_short"
        catalog.append({
            "catalog_event_id": row["record_id"],
            "cohort_date": row["cohort_date"],
            "source": row["source"],
            "source_key": row["source_key"],
            "market": row["market"],
            "symbol": row["symbol"],
            "issuer_name": row["issuer_name"],
            "announcement_date": row["announcement_date"],
            "native_event_type": native,
            "title": row["title"],
            "source_url": row["source_url"],
            "normalized_event_type": classified["normalized_event_type"],
            "catalog_role": classified["catalog_role"],
            "information_tier": classified["information_tier"],
            "taxonomy_match": classified["taxonomy_match"],
            "required_fields": classified["required_fields"],
            "decision_checks": classified["decision_checks"],
            "preferred_horizons": classified["preferred_horizons"],
            "packet_status": packet_status,
            "legacy_event_type_l2": row["legacy_event_type_l2"],
            "legacy_quality_score": row["legacy_quality_score"],
            "source_payload": payload,
        })

    path = out_dir / "all_event_catalog.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in catalog),
        encoding="utf-8",
    )

    conn.executescript(CATALOG_SCHEMA)
    conn.executemany(
        """
        INSERT INTO classified_event_catalog (
            catalog_event_id, cohort_date, source_record_id, market, symbol,
            native_event_type, normalized_event_type, catalog_role,
            information_tier, packet_status, required_fields_json,
            decision_checks_json, preferred_horizons_json, classification_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(catalog_event_id) DO UPDATE SET
            cohort_date=excluded.cohort_date,
            source_record_id=excluded.source_record_id,
            market=excluded.market,
            symbol=excluded.symbol,
            native_event_type=excluded.native_event_type,
            normalized_event_type=excluded.normalized_event_type,
            catalog_role=excluded.catalog_role,
            information_tier=excluded.information_tier,
            packet_status=excluded.packet_status,
            required_fields_json=excluded.required_fields_json,
            decision_checks_json=excluded.decision_checks_json,
            preferred_horizons_json=excluded.preferred_horizons_json,
            classification_json=excluded.classification_json
        """,
        [
            (
                row["catalog_event_id"], row["cohort_date"], row["catalog_event_id"],
                row["market"], row["symbol"], row["native_event_type"],
                row["normalized_event_type"], row["catalog_role"],
                row["information_tier"], row["packet_status"],
                json.dumps(row["required_fields"], ensure_ascii=False),
                json.dumps(row["decision_checks"], ensure_ascii=False),
                json.dumps(row["preferred_horizons"], ensure_ascii=False),
                json.dumps({
                    "taxonomy_match": row["taxonomy_match"],
                    "legacy_event_type_l2": row["legacy_event_type_l2"],
                    "legacy_quality_score": row["legacy_quality_score"],
                }, ensure_ascii=False),
            )
            for row in catalog
        ],
    )
    conn.commit()
    db_catalog_rows = conn.execute(
        "SELECT COUNT(*) FROM classified_event_catalog"
    ).fetchone()[0]
    conn.close()
    by_category = Counter(row["normalized_event_type"] for row in catalog)
    by_role = Counter(row["catalog_role"] for row in catalog)
    by_packet = Counter(row["packet_status"] for row in catalog)
    by_native = Counter(row["native_event_type"] for row in catalog)

    report = [
        "# 全事件目录与模型判断标准",
        "",
        f"- 目录事件：**{len(catalog)}**",
        f"- 原生类别数：**{len(by_native)}**",
        f"- 统一大类数：**{len(PROFILES)}**",
        "",
        "> 全部事件进入目录，不代表全部直接进入方向预测。目录层负责不丢数据；",
        "> 预测层仍需通过正文、时点、新颖性、实质性和可交易性门槛。",
        "",
        "## 统一大类分布",
        "",
        "| 统一类别 | 数量 |",
        "|---|---:|",
    ]
    report.extend(f"| {name} | {count} |" for name, count in by_category.most_common())
    report.extend([
        "",
        "## 目录角色",
        "",
        "| 角色 | 数量 | 解释 |",
        "|---|---:|---|",
    ])
    role_help = {
        "prediction_candidate": "具备潜在方向机制，补齐正文与关键字段后可预测",
        "review": "类别或影响不明确，先分类/人工复核",
        "evidence_only": "程序性或辅助文件，默认只挂到主事件作为 Evidence",
    }
    report.extend(f"| `{name}` | {count} | {role_help[name]} |" for name, count in by_role.most_common())
    report.extend([
        "",
        "## 当前 packet 状态",
        "",
        "| 状态 | 数量 |",
        "|---|---:|",
    ])
    report.extend(f"| `{name}` | {count} |" for name, count in by_packet.most_common())
    report.extend([
        "",
        "## 进入模型前的硬门槛",
        "",
        "1. 来源可追溯，正文与证券代码/发行人一致；",
        "2. 以首次可交易时点作为 effective session，预测时不得使用之后的信息；",
        "3. 同一主事件的报告、摘要、意见和进展文件必须聚合去重；",
        "4. 必须识别首次披露、修订、进展、完成或终止，程序文件默认不独立预测；",
        "5. 必须取得该类事件的 required fields，关键字段缺失时只能 review 或 evidence-only；",
        "6. 停牌、无交易日或无法取得基准行情时不进入普通 T1/T3 ACC。",
        "",
        "## 建议的可解释评分（0–100）",
        "",
        "| 维度 | 分值 | 模型需回答的问题 |",
        "|---|---:|---|",
        "| 证据完整与身份一致 | 20 | 正文、主体、标的和来源是否可核验？ |",
        "| 新颖性 | 20 | 是首次信息，还是已知事件的重复/程序进展？ |",
        "| 财务实质性 | 20 | 金额/比例相对营收、利润、净资产或股本是否足够大？ |",
        "| 预期差 | 15 | 相对市场预期、前值、原方案或指引发生了什么变化？ |",
        "| 方向因果清晰度 | 15 | 能否明确传导到收入、利润、现金流、稀释、控制权或风险？ |",
        "| 时点与可交易性 | 10 | 发布时间、生效交易日、停复牌和标签窗口是否明确？ |",
        "",
        "建议路由：总分 ≥70 才进入正式预测；50–69 进入 review；低于 50 作为上下文 Evidence。",
        "无论总分多高，只要触发硬门槛失败，就不得进入正式 ACC。该分数用于可预测性和证据充分度，",
        "不直接决定 up/down。方向仍应由该事件类型对应的 decision checks 和实际证据产生。",
        "",
        "## 各类事件的判断字段",
        "",
    ])
    for category, profile in PROFILES.items():
        report.extend([
            f"### {category}",
            "",
            "- 必需字段：" + "、".join(profile["required_fields"]),
            "- 判断检查：" + "、".join(profile["decision_checks"]),
            "- 实质性参照：" + "、".join(MATERIALITY_ANCHORS[category]),
            "- 建议窗口：" + ("、".join(profile["preferred_horizons"]) or "默认不独立预测"),
            "",
        ])
    report.extend([
        "## 裁决器输出标准",
        "",
        "每个 horizon 应分别输出：`direction`、`confidence`、`up_score`、`down_score`、",
        "`novelty_score`、`materiality_score`、`surprise_score`、`evidence_gaps` 和简短 rationale。",
        "neutral 必须明确属于无方向证据、同强度抵消、影响低于阈值或不可交易中的哪一种，",
        "不能仅因为正文长或信息不完整而默认 neutral。",
        "",
        "## 最常见原生类别",
        "",
        "| 原生类别 | 数量 |",
        "|---|---:|",
    ])
    report.extend(f"| {name or '空'} | {count} |" for name, count in by_native.most_common(30))
    (out_dir / "expanded_taxonomy_report.md").write_text("\n".join(report), encoding="utf-8")
    decision_standard = {
        "version": "all-event-v1",
        "principle": "catalog inclusion is not prediction eligibility; scores measure predictability, not direction",
        "hard_gates": [
            "source_body_identity_match",
            "as_of_and_effective_session_known",
            "main_event_deduplicated",
            "novelty_stage_known",
            "category_required_fields_complete",
            "tradable_and_labelable",
        ],
        "score_weights": {
            "evidence_integrity": 20,
            "novelty": 20,
            "materiality": 20,
            "surprise": 15,
            "direction_causal_clarity": 15,
            "timing_and_tradability": 10,
        },
        "routing": {
            "predict": "all hard gates pass and score >= 70",
            "review": "score 50-69 or a recoverable field gap",
            "evidence_only": "score < 50, routine/procedural, duplicate, or attached document",
        },
        "direction_rules": {
            "up": "direct positive causal chain dominates after surprise, magnitude, timing and conflicts are considered",
            "down": "direct negative causal chain dominates after surprise, magnitude, timing and conflicts are considered",
            "neutral": [
                "no_directional_evidence",
                "balanced_conflicting_evidence",
                "immaterial_below_threshold",
                "not_tradable_in_horizon",
            ],
        },
        "profiles": {
            category: {**profile, "materiality_anchors": MATERIALITY_ANCHORS[category]}
            for category, profile in PROFILES.items()
        },
    }
    (out_dir / "event_decision_standard.json").write_text(
        json.dumps(decision_standard, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "events": len(catalog),
        "native_categories": len(by_native),
        "normalized_categories": dict(by_category),
        "roles": dict(by_role),
        "packet_status": dict(by_packet),
        "catalog": str(path),
        "decision_standard": str(out_dir / "event_decision_standard.json"),
        "database_catalog_rows": db_catalog_rows,
    }
    (out_dir / "expanded_catalog_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
