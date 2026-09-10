#!/usr/bin/env python3
"""Build a semantic, event-level evaluation shortlist from daily cohorts."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta
import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRICT_CN = re.compile(
    r"重大资产重组报告书|发行股份购买资产.*报告|"
    r"收购报告书(?:摘要|$)|要约收购报告书|重组.*(?:草案|预案)"
)
BORDERLINE_CN = re.compile(r"发行结果公告|申请非公开发行公司债券")
PROCEDURAL = re.compile(
    r"持续督导|督导总结|转股价格调整|暂停转股|持有比例变动|"
    r"持股比例.*变动|现金管理|到期赎回|债券持有人会议|"
    r"兑付及摘牌|回售相关|股份担保|信托登记"
)
AUXILIARY = re.compile(r"财务顾问|法律意见|审计报告|评估报告|提示性公告")


def iter_dates(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def classify(row: dict[str, Any]) -> tuple[str, str]:
    title = str(row.get("title") or "")
    text = str(row.get("event_text") or "")
    market = str(row.get("market") or "")
    if market == "US":
        if "8-K" in title and re.search(r"financial results|net revenue|guidance|outlook", text, re.I):
            return "strict", "SEC 8-K 2.02 及 Exhibit 99.1 含财务结果/指引"
        if "10-Q" in title:
            return "strict", "SEC 10-Q 含完整季度财务数据"
        return "exclude", "未验证为目标财报或并购披露"
    if PROCEDURAL.search(title):
        return "exclude", "流程性或持续督导公告，不代表新的实质事件"
    if STRICT_CN.search(title):
        return "strict", "新增或更新的收购/重组实质文件"
    if BORDERLINE_CN.search(title):
        return "review", "融资行为真实，但方向含义及对上市公司的直接性需复核"
    return "exclude", "不属于新增实质事件或仅为辅助文件"


def family(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "")
    for name, pattern in (
        ("tender", r"要约收购"),
        ("acquisition", r"收购报告书"),
        ("restructuring", r"重大资产重组|发行股份购买资产|吸收合并"),
        ("financing", r"公司债券|再融资|定增|配股|可转债"),
    ):
        if re.search(pattern, title):
            return name
    return str(row.get("event_id") or title)


def primary_score(row: dict[str, Any]) -> tuple[int, int, int]:
    title = str(row.get("title") or "")
    return (
        0 if AUXILIARY.search(title) else 1,
        int(row.get("quality_score") or 0),
        len(str(row.get("event_text") or "")),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--input-root",
        default=str(PROJECT_ROOT / "pronoia_run" / "online_test"),
    )
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    root = Path(args.input_root)
    output_dir = root / f"week_{start.isoformat()}_{end.isoformat()}"

    daily: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for day in iter_dates(start, end):
        cohort_dir = root / day.isoformat()
        manifest_path = cohort_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        daily.append({"date": day.isoformat(), **manifest["counts"]})
        for row in load_jsonl(cohort_dir / "validated.jsonl"):
            row["cohort_date"] = day.isoformat()
            status, reason = classify(row)
            row["evaluation_status"] = status
            row["evaluation_selection_reason"] = reason
            rows.append(row)

    # Collapse multiple documents for the same CN event. For US earnings, the
    # earliest full 8-K in the week wins over a later 10-Q for the same release.
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        if row["evaluation_status"] == "exclude":
            continue
        if row.get("market") == "US" and row.get("event_type_l2") == "财报超预期/不及预期":
            key = ("US-earnings", str(row.get("symbol") or ""))
        else:
            key = (
                str(row.get("market") or ""),
                str(row.get("cohort_date") or ""),
                str(row.get("symbol") or ""),
                family(row),
            )
        grouped.setdefault(key, []).append(row)

    selected: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for members in grouped.values():
        if members[0].get("market") == "US":
            members.sort(key=lambda r: ("8-K" not in str(r.get("title") or ""), r["cohort_date"]))
            primary = members[0]
        else:
            primary = max(members, key=primary_score)
        selected.append(primary)
        for duplicate in members:
            if duplicate is not primary:
                duplicate["evaluation_status"] = "duplicate"
                duplicate["evaluation_selection_reason"] = f"与 {primary['event_id']} 为同一事件"
                duplicates.append(duplicate)

    strict = sorted(
        (r for r in selected if r["evaluation_status"] == "strict"),
        key=lambda r: (r["cohort_date"], r["market"], r["symbol"]),
    )
    review = sorted(
        (r for r in selected if r["evaluation_status"] == "review"),
        key=lambda r: (r["cohort_date"], r["market"], r["symbol"]),
    )
    excluded = [r for r in rows if r["evaluation_status"] == "exclude"] + duplicates
    write_jsonl(output_dir / "strict_candidates.jsonl", strict)
    write_jsonl(output_dir / "review_candidates.jsonl", review)
    write_jsonl(output_dir / "excluded_or_duplicate.jsonl", excluded)

    total = Counter()
    for item in daily:
        total.update({k: item[k] for k in ("raw", "candidates", "validated", "quarantine")})
    report = [
        f"# Online Test 一周事件回溯：{start.isoformat()} 至 {end.isoformat()}",
        "",
        "## 漏斗",
        "",
        f"- 原始记录：**{total['raw']}**",
        f"- 规则候选：**{total['candidates']}**",
        f"- 正文与身份质量通过：**{total['validated']}**",
        f"- 语义复核后核心可评测：**{len(strict)}**",
        f"- 边界样本（建议人工确认）：**{len(review)}**",
        f"- 排除或事件级重复：**{len(excluded)}**",
        "",
        "## 每日漏斗",
        "",
        "| 日期 | Raw | 规则候选 | 正文通过 | Quarantine |",
        "|---|---:|---:|---:|---:|",
    ]
    report.extend(
        f"| {r['date']} | {r['raw']} | {r['candidates']} | {r['validated']} | {r['quarantine']} |"
        for r in daily
    )
    report.extend([
        "",
        "## 核心可评测事件",
        "",
        "| 日期 | 市场 | 标的 | 类型 | 事件 | 选择理由 |",
        "|---|---|---|---|---|---|",
    ])
    report.extend(
        f"| {r['cohort_date']} | {r['market']} | {r['symbol']} {r.get('issuer_name') or ''} | "
        f"{r['event_type_l2']} | {r['title']} | {r['evaluation_selection_reason']} |"
        for r in strict
    )
    report.extend([
        "",
        "## 边界样本",
        "",
        "| 日期 | 标的 | 事件 | 原因 |",
        "|---|---|---|---|",
    ])
    report.extend(
        f"| {r['cohort_date']} | {r['symbol']} {r.get('issuer_name') or ''} | "
        f"{r['title']} | {r['evaluation_selection_reason']} |"
        for r in review
    )
    report.extend([
        "",
        "## 口径说明",
        "",
        "正文通过只表示 packet 可读且来源身份一致；进入评测还要求它是新增实质事件，",
        "并排除持续督导、转债流程公告、资金管理、辅助意见和同一事件重复文件。",
        "本次为事后回填，所有记录均不得计入正式无泄漏 Online ACC。",
        "",
    ])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({
        "raw": total["raw"],
        "rule_candidates": total["candidates"],
        "packet_quality_pass": total["validated"],
        "strict": len(strict),
        "review": len(review),
        "excluded_or_duplicate": len(excluded),
        "output_dir": str(output_dir),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
