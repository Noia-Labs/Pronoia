from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .collector import collect_cn_announcement_seeds, collect_macro_calendar_seeds, collect_us_sec_seeds
from .engine import run_baseline, run_team_prompt, validate_events
from .metrics import MetricsSummary, compute_metrics
from .models import EventLabel, EventRecord, TeamPrediction, event_template


PHASE1_EVENT_TYPES = [
    "政策利率调整",
    "通胀数据意外",
    "增长/就业数据意外",
    "并购/分拆/再融资",
    "财报超预期/不及预期",
    "公司指引上调/下调",
]


def read_jsonl(path: str | Path) -> Iterator[dict]:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            yield json.loads(s)


def write_jsonl(path: str | Path, rows: Iterable[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def load_predictions(path: str | Path) -> list[TeamPrediction]:
    return [TeamPrediction.from_dict(x) for x in read_jsonl(path)]


def load_labels(path: str | Path) -> list[EventLabel]:
    return [EventLabel.from_dict(x) for x in read_jsonl(path)]


def load_events(path: str | Path) -> list[EventRecord]:
    return [EventRecord.from_dict(x) for x in read_jsonl(path)]


def discover_backtesting_datasets() -> dict[str, int]:
    """扫描 <PROJECT_ROOT>/backtesting 目录，把 events/labels JSONL 对注册进 bt_datasets。

    命名约定（二者皆支持，前者为当前默认规范）：
      - 前缀式：events_<name>.jsonl + labels_<name>.jsonl
      - 后缀式：<name>.events.jsonl + <name>.labels.jsonl

    path/labels_path 一律写为绝对路径；扫描后清理 path 已失效的旧数据集记录。
    返回 {dataset_id: 事件数}（仅成功注册的项）。供后端启动时调用，保证「创建回测」的数据源
    始终来自 backtesting 目录，而不是数据库里残留的硬编码旧路径。
    """
    from .. import config, db
    from .datasets import materialize_contract

    btdir = Path(config.PROJECT_ROOT) / "backtesting"
    if not btdir.is_dir():
        return {}

    pairs: dict[str, dict[str, str]] = {}
    for p in btdir.glob("*.jsonl"):
        stem = p.name[: -len(".jsonl")]
        if stem.startswith("events_"):
            pairs.setdefault(stem[len("events_"):], {})["events"] = p.name
        elif stem.startswith("labels_"):
            pairs.setdefault(stem[len("labels_"):], {})["labels"] = p.name
        elif stem.endswith(".events"):
            pairs.setdefault(stem[: -len(".events")], {})["events"] = p.name
        elif stem.endswith(".labels"):
            pairs.setdefault(stem[: -len(".labels")], {})["labels"] = p.name

    registered: dict[str, int] = {}
    for key, parts in pairs.items():
        ev_fn = parts.get("events")
        if not ev_fn:
            continue
        ev_path = btdir / ev_fn
        try:
            rows = list(read_jsonl(ev_path))
        except Exception:
            continue
        if not rows:
            continue

        total = len(rows)
        by_market = Counter(str(r.get("market") or "—") for r in rows)
        by_type = Counter(str(r.get("event_type_l2") or "—") for r in rows)
        by_symbol = Counter(str(r.get("symbol") or "—") for r in rows)
        times = [str(r.get("event_time") or "") for r in rows if r.get("event_time")]
        date_range = None
        if times:
            date_range = {"min": min(times)[:10], "max": max(times)[:10]}

        lab_fn = parts.get("labels")
        contract = materialize_contract({
            "dataset_kind": "event",
            "path": str(ev_path),
            "source": {
                "type": "local_file",
                "provider": "project_backtesting_catalog",
                "ref": str(ev_path),
                "metadata": {"discovery": "startup_filename_pair"},
            },
            "markets": sorted(by_market),
            "symbols": sorted(by_symbol),
            "coverage": {
                "start_at": date_range.get("min") if date_range else None,
                "end_at": date_range.get("max") if date_range else None,
                "row_count": total,
            },
        })
        db.upsert_bt_dataset(
            dataset_id=key,
            path=str(ev_path),
            name=key.replace("_", " ").strip() or key,
            total_events=total,
            by_market=dict(by_market) if by_market else None,
            by_type=dict(by_type) if by_type else None,
            by_symbol=dict(by_symbol) if by_symbol else None,
            date_range=date_range,
            labels_path=str(btdir / lab_fn) if lab_fn else None,
            dataset_kind="event",
            dataset_version=contract["dataset_version"],
            snapshot_hash=contract.get("snapshot_hash"),
            status=contract["status"],
            source=contract.get("source"),
            markets=contract.get("markets"),
            symbols=contract.get("symbols"),
            schema_mapping=contract.get("schema_mapping"),
            capabilities=contract.get("capabilities"),
            coverage=contract.get("coverage"),
            quality_status=contract.get("quality_status") or "unverified",
            quality_report=contract.get("quality_report"),
        )
        registered[key] = total

    db.prune_missing_bt_datasets()
    return registered


def validate_events_file(path: str | Path) -> list[str]:
    return validate_events(load_events(path))


def run_baseline_file(
    *,
    events_path: str | Path,
    out_path: str | Path,
    run_id: str,
    model_version: str = "event-baseline-v0",
) -> int:
    events = load_events(events_path)
    issues = validate_events(events)
    if issues:
        raise ValueError("事件文件校验失败:\n" + "\n".join(issues[:20]))
    preds = run_baseline(events, run_id=run_id, model_version=model_version)
    write_jsonl(out_path, [p.to_dict() for p in preds])
    return len(preds)


def run_predictions_file(
    *,
    events_path: str | Path,
    out_path: str | Path,
    run_id: str,
    runner: str = "baseline",
    model_version: str | None = None,
    concurrency: int = 4,
    resume: bool = False,
    system_prompt_variant: str = "v0",
    trajectory_ckpt_dir: str | Path | None = None,
    model_profile_snapshot: dict | None = None,
    target_horizon: str = "t3",
) -> int:
    events = load_events(events_path)
    issues = validate_events(events)
    if issues:
        raise ValueError("事件文件校验失败:\n" + "\n".join(issues[:20]))

    existing: list[TeamPrediction] = []
    skip_event_ids: set[str] = set()
    out_p = Path(out_path)
    if resume and out_p.exists():
        try:
            existing = load_predictions(out_p)
            skip_event_ids = {p.event_id for p in existing if p.event_id}
        except Exception:
            existing = []
            skip_event_ids = set()

    if runner == "raw_model":
        from .raw_model import run_raw_model
        if not model_profile_snapshot:
            raise ValueError("独立事件模型必须提供 model_profile_snapshot")
        preds_buffer = list(existing)

        def on_raw_prediction(prediction: TeamPrediction) -> None:
            preds_buffer.append(prediction)
            write_jsonl(out_path, [item.to_dict() for item in preds_buffer])

        preds = asyncio.run(run_raw_model(
            events, run_id=run_id, profile=model_profile_snapshot, concurrency=concurrency,
            target_horizon=target_horizon, skip_event_ids=skip_event_ids,
            on_pred_callback=on_raw_prediction,
        ))
        merged = existing + preds
    elif runner == "baseline":
        preds = run_baseline(events, run_id=run_id, model_version=model_version or "event-baseline-v0")
        merged = existing + preds
        write_jsonl(out_path, [p.to_dict() for p in merged])
    elif runner == "team_prompt":
        # runner=team_prompt 时支持 --system-prompt-variant v2_cn_specialized = 路线A merged CN-v2 + US-v1
        variant = system_prompt_variant or "v0"
        effective_model = model_version
        if variant in {"v2_cn_specialized", "cn_v2", "cnv2", "merged_cnv2_usv1", "v3_cn_calib", "cnv3", "v4_cn_calib", "cnv4", "v5_cn_calib", "cnv5", "v6_cn_calib", "cnv6"}:
            effective_model = effective_model or "team-prompt-v2-CN-specialized"
        else:
            effective_model = effective_model or "team-prompt-v0"

        # 增量写盘逻辑
        preds_buffer = list(existing)
        def on_pred(p: TeamPrediction):
            preds_buffer.append(p)
            write_jsonl(out_path, [x.to_dict() for x in preds_buffer])

        preds = asyncio.run(
            run_team_prompt(
                events,
                run_id=run_id,
                model_version=effective_model,
                concurrency=concurrency,
                skip_event_ids=skip_event_ids,
                system_prompt_variant=variant,
                on_pred_callback=on_pred,
            )
        )
        merged = existing + preds
    elif runner == "team_full":
        # runner=team_full = 真 Team Agent 6 步 trajectory（plan→fan-out→synthesize→verify→extract hypotheses），
        # 每个 event 产出一个 TeamPrediction + 写盘 trajectory_ckpt_dir/{event_id}.json 可被 `bt trajectory` 回放。
        from .engine import run_team_full_trajectory
        variant = system_prompt_variant or "v0"
        effective_model = model_version or "team-full-trajectory-v1"
        ckpt_dir = Path(trajectory_ckpt_dir) if trajectory_ckpt_dir else Path("data/_trajectory_ckpt")

        # F2: trajectory ckpt 级 resume：从 ckpt_dir 扫描已落盘的 event_id（即便 pred JSONL 还没 flush 也不重复跑）
        skip_by_ckpt: set[str] = set()
        if resume and ckpt_dir.exists() and ckpt_dir.is_dir():
            for fn in ckpt_dir.iterdir():
                if fn.is_file() and fn.suffix == ".json" and not fn.name.startswith("."):
                    stem = fn.stem
                    # 文件名可能形如 {event_id}.json 或 {event_id}__{run_id}.json，取第一段 __ 之前
                    ev_id = stem.split("__")[0]
                    if ev_id:
                        skip_by_ckpt.add(ev_id)
        if skip_by_ckpt:
            # 合并到 skip_event_ids（上游 resume 已从 preds JSONL 加载一份，ckpt 再补一份）
            skip_event_ids = (skip_event_ids or set()) | skip_by_ckpt
            print(
                f"[resume] team_full: from trajectory_ckpt_dir={ckpt_dir} picked {len(skip_by_ckpt)} existing ckpt events, "
                f"total skip={len(skip_event_ids)}",
                flush=True,
            )

        preds = asyncio.run(
            run_team_full_trajectory(
                events,
                run_id=run_id,
                model_version=effective_model,
                concurrency=concurrency,
                skip_event_ids=skip_event_ids,
                system_prompt_variant=variant,
                trajectory_ckpt_dir=ckpt_dir,
            )
        )
        merged = existing + preds
        write_jsonl(out_path, [p.to_dict() for p in merged])
    else:
        raise ValueError(f"未知 runner: {runner}")

    return len(merged)


def render_trajectory_md_report(
    *,
    trajectory_obj: dict,
    labels_path: str | Path | None = None,
) -> str:
    """把 team_full 单 event trajectory 渲染为标准 Markdown 报告。

    报告含 11 节：元信息 / 执行流程时间线 / PLAN / Expert Fan-out /
    Evidence Graph / 可证伪假设 / Verifier 复核 / 最终结构化输出 /
    team_prompt vs team_full 对比 / Oracle 对照与误判根因 / 完整最终回答原文。

    labels_path 可选：若提供 labels.jsonl，自动匹配 Oracle T+1/T+3/T+5 标签 + CAR。
    """
    import datetime as dt
    import json as _json
    from pathlib import Path

    t = trajectory_obj
    eid = t.get("event_id", "?")
    meta = t.get("event_meta") or {}
    stats = t.get("llm_trajectory_stats") or {}
    ex = t.get("structured_extract") or {}
    final_state = t.get("team_final_state") or {}
    content_full = final_state.get("content_full") or ""
    tool_trace = final_state.get("tool_trace") or []
    sse_events = t.get("trajectory_sse_events") or []

    # Oracle 标签（可选）
    oracle = None
    if labels_path:
        try:
            for line in open(labels_path, encoding="utf-8"):
                if not line.strip():
                    continue
                lb = _json.loads(line)
                if lb.get("event_id") == eid:
                    oracle = lb
                    break
        except Exception:
            oracle = None

    # 从 SSE events 提取 plan / agent_start / agent_done / verify / logic_items
    plans, agent_phases, verify_info, logic_items = [], [], None, []
    for ev in sse_events:
        et = ev.get("type")
        if et == "agent_step" and ev.get("phase") == "plan":
            plans = list(ev.get("plan") or [])
        elif et == "agent_step" and ev.get("phase") in ("agent_start", "agent_done"):
            agent_phases.append({
                "phase": ev.get("phase"),
                "agent": ev.get("agent", ""),
                "note": str(ev.get("note", "")),
                "ts": ev.get("t"),
            })
        elif et == "agent_step" and ev.get("phase") == "verified":
            verify_info = {
                "verdict": "issues" if "issues" in str(ev.get("note", "")) else "pass",
                "note": str(ev.get("note", "")),
            }
        elif et == "logic_items":
            logic_items = ev.get("items") or ev.get("hypotheses") or []
    # 从 tool_trace 提取 verify / logic_items（兼容老版本）
    for tt in tool_trace:
        if tt.get("type") == "verify" and not verify_info:
            verify_info = tt
        elif tt.get("type") == "logic_items" and not logic_items:
            logic_items = tt.get("items") or []

    md = []
    A = md.append

    # ===== Header =====
    A(f"# Pronoia team_full 单条事件试跑报告 — {meta.get('symbol','?')}({meta.get('market','?')})")
    A("")
    A(f"> **生成时间**：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    A(f"> **runner**：`team_full`（真 Team Agent 5 阶段流水线：plan → fan-out → synthesize → verify → extract）")
    A(f"> **event_id**：`{eid}`")
    if oracle:
        car_v = oracle.get("car_t3")
        car_bps = f"{car_v*10000:.1f}" if isinstance(car_v, (int, float)) else "?"
        A(f"> **Oracle T+3**：**{oracle.get('label_t3','?')}**（CAR={car_bps} bps）")
    A("")

    # ===== 1. 事件元信息 =====
    A("## 1. 事件元信息")
    A("")
    A("| 字段 | 值 |")
    A("|---|---|")
    A(f"| event_id | `{eid}` |")
    A(f"| market / symbol | {meta.get('market','?')} / {meta.get('symbol','?')} |")
    A(f"| event_time | {meta.get('event_time','?')} |")
    A(f"| event_type_l2 | {meta.get('event_type_l2','?')} |")
    A(f"| benchmark | {meta.get('benchmark','?')} |")
    if oracle:
        for hz, lab, car in [("T+1", "label_t1", "car_t1"), ("T+3", "label_t3", "car_t3"), ("T+5", "label_t5", "car_t5")]:
            car_v = oracle.get(car)
            car_bps = f"{car_v*10000:.1f}" if isinstance(car_v, (int, float)) else "?"
            A(f"| Oracle {hz} | {oracle.get(lab,'?')}（CAR={car_bps} bps） |")
    A(f"| **team_full 预测** | **{ex.get('direction','?')}**（conf={ex.get('confidence','?')}） |")
    A("")

    # 运行参数
    A("### 运行参数")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| wall_seconds | {t.get('wall_seconds',0):.1f}s（{t.get('wall_seconds',0)/60:.1f} min） |")
    A(f"| model_version | {t.get('model_version','?')} |")
    A(f"| system_prompt_variant | {t.get('system_prompt_variant','?')} |")
    A(f"| n_sse_events | {stats.get('n_sse_events','?')} |")
    A(f"| n_tool_calls | {stats.get('n_tool_calls','?')} |")
    A(f"| n_final_chars | {stats.get('n_final_chars','?')} |")
    A(f"| agents_seen | {', '.join(stats.get('agents_seen') or [])} |")
    A("")

    # ===== 2. 执行流程时间线 =====
    A("## 2. 执行流程时间线（5 阶段）")
    A("")
    A("| 阶段 | 产出 |")
    A("|---|---|")
    if plans:
        A(f"| ① PLAN | router 拆 {len(plans)} 子任务 |")
    agent_starts = [p for p in agent_phases if p["phase"] == "agent_start"]
    agent_dones = [p for p in agent_phases if p["phase"] == "agent_done"]
    for i, (s, d) in enumerate(zip(agent_starts, agent_dones) if len(agent_starts) == len(agent_dones) else []):
        A(f"| ② Fan-out #{i+1} | {s['agent']}: {d['note'][:80]} |")
    A("| ③ SYNTHESIZE | router 综合专家发现 |")
    if verify_info:
        A(f"| ④ VERIFY | verdict={verify_info.get('verdict','?')} |")
    A(f"| ⑤ EXTRACT | {len(logic_items)} 条 hypotheses |")
    A("")

    # ===== 3. PLAN =====
    A("## 3. PLAN — router LLM 拆解的子任务")
    A("")
    if plans:
        A("| # | Agent | 任务 |")
        A("|---|---|---|")
        for i, task in enumerate(plans, 1):
            agent = task.get("agent") or task.get("agent_id") or ""
            name = task.get("agent_name") or agent
            task_text = str(task.get("task") or task.get("description") or "")[:200]
            A(f"| {i} | **{name}**({agent}) | {task_text} |")
    else:
        A("（未找到 plan）")
    A("")

    # ===== 4. Expert Fan-out =====
    A("## 4. Expert Fan-out 综合")
    A("")
    if agent_dones:
        A("| # | Agent | 产出摘要 |")
        A("|---|---|---|")
        for i, d in enumerate(agent_dones, 1):
            A(f"| {i} | {d['agent']} | {d['note'][:150]} |")
    else:
        A("（未找到 agent_done 事件）")
    A("")

    # ===== 5. Evidence Graph =====
    A("## 5. Evidence Graph（证据图）")
    A("")
    A("（从 team_final_state.content_full 提取，完整内容见第 11 节）")
    A("")
    A("```")
    # 从 content_full 里截取 Evidence Graph 段落
    import re
    eg_match = re.search(r"(Evidence Graph.*?)(?=###|\Z)", content_full, flags=re.S)
    if eg_match:
        A(eg_match.group(1).strip())
    else:
        A("（content_full 中未找到 Evidence Graph 段落）")
    A("```")
    A("")

    # ===== 6. 可证伪假设 =====
    A("## 6. 可证伪假设（Hypotheses / Claims）")
    A("")
    if logic_items:
        A(f"共 {len(logic_items)} 条，由 `extract` 阶段从最终回答中抽取：")
        A("")
        A("| # | ID | hypothesis | category | probability | horizon | 可证伪条件 |")
        A("|---|---|---|---|---|---|---|")
        for i, h in enumerate(logic_items, 1):
            A(f"| {i} | {h.get('id','')} | {h.get('hypothesis','')[:150]} | {h.get('category','')} | {h.get('probability','')} | {h.get('horizon','')[:40]} | {h.get('check','')[:100]} |")
        A("")
        if oracle:
            A("**Oracle 事后证伪**：")
            A("")
            oracle_t3 = oracle.get("label_t3", "")
            oracle_car3 = oracle.get("car_t3", 0)
            oracle_car3_bps = f"{oracle_car3*10000:.1f}" if isinstance(oracle_car3, (int, float)) else "?"
            for i, h in enumerate(logic_items, 1):
                hyp = h.get("hypothesis", "")
                # 简单判断：若 oracle 是 up 且 hypothesis 说 down，则证伪
                if "down" in hyp.lower() and oracle_t3 == "up":
                    A(f"- Hypothesis {i} → **证伪**（Oracle T+3={oracle_t3}, CAR={oracle_car3_bps} bps，方向相反）")
                elif "up" in hyp.lower() and oracle_t3 == "down":
                    A(f"- Hypothesis {i} → **证伪**（Oracle T+3={oracle_t3}, CAR={oracle_car3_bps} bps，方向相反）")
                else:
                    A(f"- Hypothesis {i} → 需人工核对（Oracle T+3={oracle_t3}）")
            A("")
    else:
        A("（未提取到 hypotheses）")
        A("")

    # ===== 7. Verifier 复核 =====
    A("## 7. Verifier 复核结果")
    A("")
    if verify_info:
        verdict = verify_info.get("verdict", "?")
        A(f"**verdict**：`{verdict}`")
        A("")
        issues = verify_info.get("issues") or []
        if issues:
            A(f"发现 {len(issues)} 个问题：")
            A("")
            for j, iss in enumerate(issues, 1):
                A(f"### Issue {j}")
                A("")
                A(f"> {iss}")
                A("")
        corrected = verify_info.get("corrected", "")
        if corrected:
            A("### Router 自动修正后的关键段落")
            A("")
            A("```")
            A(corrected[:1500])
            A("```")
            A("")
    else:
        A("（未找到 verify 阶段）")
        A("")

    # ===== 8. 最终结构化输出 =====
    A("## 8. 最终结构化输出")
    A("")
    A("| 字段 | 值 |")
    A("|---|---|")
    A(f"| **direction** | `{ex.get('direction','?')}` |")
    A(f"| **confidence** | `{ex.get('confidence','?')}` |")
    rationale = ex.get("rationale", "")
    A(f"| **rationale** | {rationale[:500]} |")
    A("")

    # ===== 9. Oracle 对照与误判根因 =====
    if oracle:
        A("## 9. Oracle 对照与误判根因分析")
        A("")
        A("### Oracle 真实结果")
        A("")
        A("| Horizon | Label | CAR |")
        A("|---|---|---|")
        for hz, lab, car in [("T+1", "label_t1", "car_t1"), ("T+3", "label_t3", "car_t3"), ("T+5", "label_t5", "car_t5")]:
            car_v = oracle.get(car)
            car_bps = f"{car_v*10000:.1f}" if isinstance(car_v, (int, float)) else "?"
            A(f"| {hz} | {oracle.get(lab,'?')} | {car_bps} bps |")
        A("")
        pred_dir = ex.get("direction", "")
        oracle_t3 = oracle.get("label_t3", "")
        correct = (pred_dir == oracle_t3)
        A(f"### 预测正确性：{'✅ 正确' if correct else '❌ 误判'}")
        A("")
        A(f"- team_full 预测：{pred_dir}")
        A(f"- Oracle T+3：{oracle_t3}")
        if not correct:
            A(f"- 误判方向：预测 {pred_dir}，实际 {oracle_t3}")
            A("")
            A("### 误判根因")
            A("")
            A("（见完整最终回答中的证据图与 synthesize 段落，分析 as-of 证据是否一致指向错误方向）")
        A("")

    # ===== 10/11. 完整最终回答 =====
    section_num = 10 if not oracle else 10
    A(f"## {section_num}. 完整最终回答原文（team_final_state.content_full）")
    A("")
    A("```markdown")
    A(content_full)
    A("```")
    A("")
    A("---")
    A("")
    A(f"*报告由 FEVER team_full runner 自动生成，trajectory 完整数据见 JSON 文件*")

    return "\n".join(md)


def score_files(
    *,
    predictions_path: str | Path,
    labels_path: str | Path,
    epsilon: float = 0.005,
    primary_oracle_horizon: str = "t3",
) -> MetricsSummary:
    preds = load_predictions(predictions_path)
    labels = load_labels(labels_path)
    return compute_metrics(
        predictions=preds,
        labels=labels,
        epsilon=epsilon,
        primary_oracle_horizon=primary_oracle_horizon,
    )


def write_metrics(path: str | Path, metrics: MetricsSummary) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def read_metrics(path: str | Path) -> dict:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def write_event_template(path: str | Path, *, count: int = 1) -> None:
    rows = []
    for i in range(max(1, int(count))):
        row = dict(event_template())
        row["event_id"] = f"evt_template_{i + 1:04d}"
        rows.append(row)
    write_jsonl(path, rows)


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {value!r}")


def _format_date_ymd(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _date_range(start: date, end: date, *, step_days: int = 7) -> list[str]:
    if end < start:
        return []
    out: list[str] = []
    cur = start
    step = max(1, int(step_days or 1))
    while cur <= end:
        out.append(_format_date_ymd(cur))
        cur += timedelta(days=step)
    if out and out[-1] != _format_date_ymd(end):
        out.append(_format_date_ymd(end))
    return out


def _recent_dates(days: int = 10) -> list[str]:
    out: list[str] = []
    d = date.today()
    for i in range(max(1, int(days or 10))):
        out.append((d - timedelta(days=i)).strftime("%Y%m%d"))
    return out


def _parse_event_event_time(value: str) -> date | None:
    s = (value or "").strip()
    if not s:
        return None
    s = s.split(" ")[0].split("T")[0]
    s = s.replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _filter_event_by_history_window(
    event: EventRecord,
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    min_history_days: Optional[int] = None,
    today: Optional[date] = None,
) -> bool:
    d = _parse_event_event_time(event.event_time)
    if d is None:
        return False
    if start_date is not None and d < start_date:
        return False
    if end_date is not None and d > end_date:
        return False
    if min_history_days is not None:
        ref = today or date.today()
        if (ref - d).days < max(0, int(min_history_days)):
            return False
    return True


def collect_seed_events_file(
    *,
    out_path: str | Path,
    cn_dates: list[str] | None = None,
    cn_keywords: list[str] | None = None,
    us_symbols: list[str] | None = None,
    us_count_per_symbol: int = 20,
    include_macro: bool = False,
    macro_limit: int = 120,
    default_recent_days: int = 10,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    min_history_days: int | None = None,
    cn_backtest_step_days: int = 5,
) -> int:
    start_dt = _parse_date(start_date)
    end_dt = _parse_date(end_date)
    use_backtest_window = start_dt is not None or end_dt is not None or min_history_days is not None
    rows: list[EventRecord] = []

    if cn_dates is None and use_backtest_window:
        today = date.today()
        safe_end = end_dt or today
        if min_history_days is not None:
            safe_end = min(safe_end, today - timedelta(days=max(0, int(min_history_days))))
        safe_start = start_dt or (safe_end - timedelta(days=max(1, int(default_recent_days))))
        cn_dates = _date_range(safe_start, safe_end, step_days=max(1, int(cn_backtest_step_days)))
    elif cn_dates is None:
        cn_dates = _recent_dates(default_recent_days)

    if cn_dates:
        rows.extend(
            collect_cn_announcement_seeds(
                dates=cn_dates,
                keywords=cn_keywords,
            )
        )
    if us_symbols is None:
        us_symbols = ["NVDA", "AAPL", "MSFT", "AMZN", "META", "TSLA", "GOOGL"]
    if us_symbols:
        rows.extend(
            collect_us_sec_seeds(
                symbols=us_symbols,
                count_per_symbol=us_count_per_symbol,
            )
        )
    if include_macro:
        rows.extend(collect_macro_calendar_seeds(limit=macro_limit))

    if use_backtest_window:
        today = date.today()
        rows = [
            r
            for r in rows
            if _filter_event_by_history_window(
                r,
                start_date=start_dt,
                end_date=end_dt,
                min_history_days=min_history_days,
                today=today,
            )
        ]
    write_jsonl(out_path, [r.to_dict() for r in rows])
    return len(rows)


def _usable_seed_event(event: EventRecord) -> bool:
    return all(
        [
            event.event_id,
            event.market,
            event.symbol,
            event.symbol != "UNKNOWN",
            event.event_time,
            event.event_type_l2,
            event.title,
            event.source_url,
        ]
    )


def _dedupe_seed_events(events: Iterable[EventRecord]) -> list[EventRecord]:
    seen: set[tuple[str, str, str, str, str, str]] = set()
    out: list[EventRecord] = []
    for event in events:
        key = (
            event.market,
            event.symbol,
            event.event_time,
            event.event_type_l2,
            event.title,
            event.source_url,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def curate_phase1_events(
    events: Iterable[EventRecord],
    *,
    per_type_target: int = 30,
    max_per_symbol_per_type: int = 2,
    type_order: list[str] | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    min_history_days: int | None = None,
) -> list[EventRecord]:
    ordered_types = list(type_order or PHASE1_EVENT_TYPES)
    today = date.today()
    start_dt = _parse_date(start_date)
    end_dt = _parse_date(end_date)
    usable = [
        e
        for e in events
        if _usable_seed_event(e)
        and _filter_event_by_history_window(
            e,
            start_date=start_dt,
            end_date=end_dt,
            min_history_days=min_history_days,
            today=today,
        )
    ]
    usable = _dedupe_seed_events(usable)
    usable.sort(key=lambda e: (e.event_time, e.event_id), reverse=True)

    grouped: dict[str, list[EventRecord]] = defaultdict(list)
    for event in usable:
        grouped[event.event_type_l2].append(event)

    selected: list[EventRecord] = []
    selected_ids: set[str] = set()
    selected_type_counts: Counter[str] = Counter()

    for event_type in ordered_types:
        symbol_counts: Counter[str] = Counter()
        for event in grouped.get(event_type, []):
            if selected_type_counts[event_type] >= per_type_target:
                break
            symbol_key = event.symbol.strip().upper() or event.event_id
            if symbol_counts[symbol_key] >= max(1, int(max_per_symbol_per_type or 1)):
                continue
            if event.event_id in selected_ids:
                continue
            selected.append(event)
            selected_ids.add(event.event_id)
            selected_type_counts[event_type] += 1
            symbol_counts[symbol_key] += 1

        if selected_type_counts[event_type] >= per_type_target:
            continue

        for event in grouped.get(event_type, []):
            if selected_type_counts[event_type] >= per_type_target:
                break
            if event.event_id in selected_ids:
                continue
            selected.append(event)
            selected_ids.add(event.event_id)
            selected_type_counts[event_type] += 1

    target_total = max(1, int(per_type_target or 30)) * len(ordered_types)
    if len(selected) >= target_total:
        return selected[:target_total]

    for event in usable:
        if len(selected) >= target_total:
            break
        if event.event_id in selected_ids:
            continue
        selected.append(event)
        selected_ids.add(event.event_id)
    return selected


def _largest_remainder_allocation(total: int, weights: dict[str, float]) -> dict[str, int]:
    total = max(0, int(total))
    if total <= 0 or not weights:
        return {k: 0 for k in weights.keys()}
    positive = {k: float(v) for k, v in weights.items() if float(v) > 0.0}
    if not positive:
        return {k: 0 for k in weights.keys()}
    s = sum(positive.values())
    base: dict[str, int] = {}
    rema: list[tuple[float, str]] = []
    for k, w in positive.items():
        x = total * (w / s)
        b = int(x)
        base[k] = b
        rema.append((x - b, k))
    used = sum(base.values())
    remain = max(0, total - used)
    rema.sort(reverse=True)
    for i in range(remain):
        _, k = rema[i % len(rema)]
        base[k] = base.get(k, 0) + 1
    out = {k: 0 for k in weights.keys()}
    out.update(base)
    return out


def curate_phase1_events_natural(
    events: Iterable[EventRecord],
    *,
    total_target: int = 180,
    cn_ratio: float = 0.7,
    max_per_symbol_per_type: int = 2,
    type_order: list[str] | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    min_history_days: int | None = None,
) -> list[EventRecord]:
    ordered_types = list(type_order or PHASE1_EVENT_TYPES)
    today = date.today()
    start_dt = _parse_date(start_date)
    end_dt = _parse_date(end_date)
    usable = [
        e
        for e in events
        if _usable_seed_event(e)
        and _filter_event_by_history_window(
            e,
            start_date=start_dt,
            end_date=end_dt,
            min_history_days=min_history_days,
            today=today,
        )
    ]
    usable = _dedupe_seed_events(usable)
    usable.sort(key=lambda e: (e.event_time, e.event_id), reverse=True)

    total_target = max(1, int(total_target or 180))
    cn_target = int(round(total_target * float(cn_ratio)))
    cn_target = max(0, min(total_target, cn_target))
    us_target = total_target - cn_target

    def market_slice(market: str) -> list[EventRecord]:
        return [e for e in usable if e.market == market]

    def by_type_counts(rows: list[EventRecord]) -> Counter[str]:
        c: Counter[str] = Counter()
        for e in rows:
            c[e.event_type_l2] += 1
        return c

    def pick_for_market(market: str, target: int) -> list[EventRecord]:
        rows = market_slice(market)
        if not rows or target <= 0:
            return []
        counts = by_type_counts(rows)
        weights = {t: float(counts.get(t, 0)) for t in ordered_types}
        alloc = _largest_remainder_allocation(target, weights)

        grouped: dict[str, list[EventRecord]] = defaultdict(list)
        for e in rows:
            grouped[e.event_type_l2].append(e)

        selected: list[EventRecord] = []
        selected_ids: set[str] = set()
        for t in ordered_types:
            want = int(alloc.get(t, 0))
            if want <= 0:
                continue
            symbol_counts: Counter[str] = Counter()
            for e in grouped.get(t, []):
                if want <= 0:
                    break
                if e.event_id in selected_ids:
                    continue
                sym = e.symbol.strip().upper() or e.event_id
                if symbol_counts[sym] >= max(1, int(max_per_symbol_per_type or 1)):
                    continue
                selected.append(e)
                selected_ids.add(e.event_id)
                symbol_counts[sym] += 1
                want -= 1
        if len(selected) >= target:
            return selected[:target]
        for e in rows:
            if len(selected) >= target:
                break
            if e.event_id in selected_ids:
                continue
            selected.append(e)
            selected_ids.add(e.event_id)
        return selected

    cn_selected = pick_for_market("CN", cn_target)
    us_selected = pick_for_market("US", us_target)
    selected = cn_selected + us_selected
    if len(selected) >= total_target:
        return selected[:total_target]

    selected_ids = {e.event_id for e in selected}
    for e in usable:
        if len(selected) >= total_target:
            break
        if e.event_id in selected_ids:
            continue
        selected.append(e)
        selected_ids.add(e.event_id)
    return selected


def curate_phase1_events_file(
    *,
    seeds_path: str | Path,
    out_path: str | Path,
    per_type_target: int = 30,
    max_per_symbol_per_type: int = 2,
    type_order: list[str] | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    min_history_days: int | None = None,
    mode: str = "balanced",
    total_target: int = 180,
    cn_ratio: float = 0.7,
) -> dict[str, object]:
    ordered_types = list(type_order or PHASE1_EVENT_TYPES)
    seeds = load_events(seeds_path)
    if str(mode or "balanced").strip().lower() == "natural":
        curated = curate_phase1_events_natural(
            seeds,
            total_target=total_target,
            cn_ratio=cn_ratio,
            max_per_symbol_per_type=max_per_symbol_per_type,
            type_order=ordered_types,
            start_date=start_date,
            end_date=end_date,
            min_history_days=min_history_days,
        )
        requested_total = int(total_target)
    else:
        curated = curate_phase1_events(
            seeds,
            per_type_target=per_type_target,
            max_per_symbol_per_type=max_per_symbol_per_type,
            type_order=ordered_types,
            start_date=start_date,
            end_date=end_date,
            min_history_days=min_history_days,
        )
        requested_total = int(per_type_target) * len(ordered_types)
    write_jsonl(out_path, [row.to_dict() for row in curated])

    by_type = Counter(row.event_type_l2 for row in curated)
    by_market = Counter(row.market for row in curated)
    if curated:
        dates = [d for d in (_parse_event_event_time(r.event_time) for r in curated) if d]
        date_range = {
            "min": min(dates).isoformat(),
            "max": max(dates).isoformat(),
        } if dates else {}
    else:
        date_range = {}
    if str(mode or "balanced").strip().lower() == "natural":
        shortfalls = {event_type: 0 for event_type in ordered_types}
    else:
        shortfalls = {event_type: max(0, int(per_type_target) - int(by_type.get(event_type, 0))) for event_type in ordered_types}
    return {
        "selected": len(curated),
        "requested_total": requested_total,
        "mode": str(mode or "balanced"),
        "by_type": dict(by_type),
        "by_market": dict(by_market),
        "date_range": date_range,
        "type_shortfalls": shortfalls,
    }
