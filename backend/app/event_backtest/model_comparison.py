"""Object-first Arena orchestration; original runs and their metrics are read-only.

Comparisons replay saved model signals on one newly frozen OHLC snapshot. The
legacy event CAR proxy is never merged with a portfolio's old PnL series.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid
from typing import Any

from .. import config, db
from ..quant_backtest.data import load_csv_dataset, rows_to_bars
from ..quant_backtest.models import MarketDataset
from . import model_comparison_adapters as adapters
from .model_comparison_replay import compare_on_market
from .protocol import file_sha256, recompute_protocol_hash_for_run

VERSION = "arena-model-comparison-v1"
_SAVE_LOCK = threading.RLock()


def _record(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _private(run: dict) -> bool:
    visibility = str(run.get("visibility") or _record(run.get("config")).get("visibility") or "private")
    return visibility.lower().replace("-", "_") == "private"


def _eligibility(run: dict, *, verify: bool = True) -> str | None:
    if not _private(run):
        return "该记录仅提供安全摘要，无法读取逐条信号进行共同交易回放"
    if run.get("status") != "done":
        return "请先完成这条模型测试记录"
    cfg = _record(run.get("config"))
    lab = _record(cfg.get("model_lab"))
    if any(value is True or str(value).lower() in {"demo", "dry_run", "demo_only"}
           for source in (cfg, lab) for key in ("demo", "dry_run", "result_mode")
           for value in [source.get(key)]):
        return "演示或 dry-run 记录没有可比较的真实模型结果"
    if str(run.get("engine_mode") or "event_proxy") not in {"event_proxy", "portfolio"}:
        return "该运行类型暂不提供可重放的模型信号"
    try:
        if verify and run.get("protocol_hash") and run["protocol_hash"] != recompute_protocol_hash_for_run(run):
            return "原始数据或运行设置已改变，请基于当前数据重新完成模型测试"
    except (ValueError, OSError):
        return "无法核验原始运行数据，请检查输入和结果文件"
    return None


def _identity_from_dataset(item: dict) -> tuple[str, str] | None:
    symbols = item.get("symbols") or []
    markets = item.get("markets") or []
    if len(symbols) != 1 or len(markets) != 1:
        return None
    # Use the same canonical identity as signal adapters, including exchange.
    try:
        return adapters.asset_identity(str(markets[0]), str(symbols[0]))
    except ValueError:
        return None


def catalog() -> dict:
    subjects: dict[str, dict] = {}
    runs: list[dict] = []
    run_descriptions: dict[str, dict] = {}
    for run in db.list_bt_runs(limit=1000):
        reason = _eligibility(run, verify=False)
        row = {"id": run["id"], "name": run.get("name") or run["id"],
               "model_kind": "quant" if run.get("engine_mode") == "portfolio" else "event",
               "eligible": reason is None, "reason": reason, "subject_keys": []}
        # Do not expose titles/timestamps from an Arena-safe record.
        if _private(run) and run.get("status") == "done":
            try:
                description = adapters.describe_run(run)
                row["model_kind"] = description["model_kind"]
                row["model_name"] = description.get("model_name") or description["name"]
                row["subject_keys"] = [item["key"] for item in description["subjects"]]
                run_descriptions[run["id"]] = description
                for item in description["subjects"]:
                    subject = subjects.setdefault(item["key"], {**item, "run_ids": [], "sources": []})
                    subject["run_ids"].append(run["id"])
            except (ValueError, OSError, KeyError) as exc:
                row.update(eligible=False, reason="无法读取该记录的事件或模型信号，请检查原始测试结果")
        runs.append(row)

    # A quant model can compete on an event when its asset matches. Event
    # candidates still need an identical event fact key, not just its title.
    datasets = db.list_bt_datasets()
    for subject in subjects.values():
        if subject["kind"] == "event":
            for run_id, description in run_descriptions.items():
                if description.get("market_source") and any(
                    item["kind"] == "asset" and item["market"] == subject["market"]
                    and item["symbol"] == subject["symbol"] for item in description["subjects"]
                ):
                    subject["run_ids"].append(run_id)
                    next(row for row in runs if row["id"] == run_id)["subject_keys"].append(subject["key"])
            subject["sources"].append({"id": "auto:" + subject["key"],
                "label": "获取事件附近的真实日 K（预览时获取并保存快照）", "frequency": "1d"})
        for run_id, description in run_descriptions.items():
            source = description.get("market_source")
            if source and source.get("symbol") == subject["symbol"] and source.get("market") == subject["market"]:
                subject["sources"].append({"id": "run:" + run_id,
                    "label": "已冻结行情 · " + str(description["name"]),
                    **{key: source.get(key) for key in ("frequency", "start_at", "end_at")}})
        for dataset in datasets:
            if dataset.get("dataset_kind") != "market":
                continue
            identity = _identity_from_dataset(dataset)
            if identity == (subject["market"], subject["symbol"]):
                subject["sources"].append({"id": "dataset:" + dataset["id"],
                    "label": "本地行情 · " + dataset["name"], "frequency": dataset.get("frequency") or "1d"})
        subject["run_ids"] = list(dict.fromkeys(subject["run_ids"]))
        # Prefer an already available immutable source over a network fetch.
        subject["sources"].sort(key=lambda source: source["id"].startswith("auto:"))
    return {"subjects": list(subjects.values()), "runs": runs}


def _fingerprints(run: dict) -> dict[str, str]:
    result = {}
    for field in ("events_path", "labels_path", "out_path", "result_path"):
        if run.get(field) and Path(str(run[field])).is_file():
            result[str(Path(str(run[field])).resolve())] = file_sha256(run[field]) or ""
    return result


def _load_market(source_id: str, subject: dict, selected_runs: list[dict], holding_bars: int) -> tuple[MarketDataset, dict]:
    if source_id.startswith("run:"):
        source_run = db.get_bt_run(source_id[4:])
        if not source_run or _eligibility(source_run):
            raise ValueError("行情来源的运行不可用，请重新选择已完成的私有量化记录")
        fingerprints = _fingerprints(source_run)
        dataset = adapters.get_run_market(source_run)
        if fingerprints != _fingerprints(source_run):
            raise ValueError("行情来源在读取期间发生变化，请重新预览")
        return dataset, fingerprints
    if source_id.startswith("dataset:"):
        item = db.get_bt_dataset(source_id[8:])
        if not item or item.get("dataset_kind") != "market" or _identity_from_dataset(item) != (subject["market"], subject["symbol"]):
            raise ValueError("请为本次对比选择同一标的的本地行情")
        path = str(item.get("path") or "")
        before = file_sha256(path)
        dataset = load_csv_dataset(path, name=item["name"], symbol=subject["symbol"],
            market=subject["market"], frequency=item.get("frequency") or "1d", repair_ohlc=False)
        if not before or before != file_sha256(path):
            raise ValueError("行情文件在读取期间发生变化，请重新预览")
        return dataset, {str(Path(path).resolve()): before}
    if source_id != "auto:" + subject["key"] or subject["kind"] != "event":
        raise ValueError("请选择当前比较对象对应的行情来源")
    event = None
    for run in selected_runs:
        if run.get("engine_mode") != "portfolio":
            try:
                event = adapters.get_event_record(run, subject["key"])
                break
            except ValueError:
                continue
    if event is None:
        raise ValueError("自动获取事件行情需要选择至少一条该事件的模型记录；也可选择本地行情")
    from .performance import fetch_event_market_series
    try:
        payload = fetch_event_market_series(event, days_before=15, days_after=max(30, min(365, holding_bars * 3 + 10)))
    except Exception as exc:
        raise ValueError("暂时无法获取该事件的真实日 K，请在数据管理导入同标的 OHLC 文件，再选择本地行情预览") from exc
    dates, ohlc = payload.get("dates") or [], payload.get("ohlc") or []
    if adapters.asset_identity(str(payload.get("market") or subject["market"]), str(payload.get("symbol") or "")) != (subject["market"], subject["symbol"]):
        raise ValueError("行情源返回了其他标的数据，请改用同标的本地行情")
    if not dates or len(dates) != len(ohlc) or payload.get("series_type") != "ohlc":
        raise ValueError("行情源只有收盘价，缺少真实开盘价；请导入含 open/high/low/close 的行情后进行下一根开盘回放")
    rows = [{"timestamp": stamp, "open": values[0], "close": values[1], "low": values[2], "high": values[3]}
            for stamp, values in zip(dates, ohlc) if isinstance(values, (list, tuple)) and len(values) == 4]
    if len(rows) != len(dates):
        raise ValueError("行情源返回的 OHLC 不完整，请选择本地行情")
    bars, audit = rows_to_bars(rows, repair_ohlc=False)
    return MarketDataset(name="事件附近日 K", symbol=subject["symbol"], market=subject["market"],
        frequency="1d", bars=bars, source_type="event_market_snapshot",
        metadata={**audit, "source": payload.get("source"), "fetched_at": payload.get("fetched_at"),
                  "adjustment": payload.get("adjustment")}), {}


def _directory() -> Path:
    folder = Path(config.DATA_DIR) / "arena_comparisons"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def _preview_path(preview_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", preview_id):
        raise ValueError("预览标识无效，请重新生成对比")
    return _directory() / (preview_id + ".json")


def _public_subject(subject: dict) -> dict:
    return {key: value for key, value in subject.items() if key not in {"run_ids", "sources"}}


def preview(settings: dict) -> dict:
    available = catalog()
    subject = next((item for item in available["subjects"] if item["key"] == settings["subject_key"]), None)
    if subject is None:
        raise ValueError("比较对象已不可用，请刷新后重新选择事件或标的")
    source_id = settings["market_source_id"]
    if source_id not in {source["id"] for source in subject["sources"]}:
        raise ValueError("该行情来源不属于所选事件或标的")
    run_ids = list(dict.fromkeys(settings["run_ids"]))
    if not 2 <= len(run_ids) <= 8:
        raise ValueError("请选择 2–8 条不同的模型测试记录")
    selected, fingerprints = [], {}
    for run_id in run_ids:
        if run_id not in subject["run_ids"]:
            raise ValueError("所选记录不属于同一事件或标的，请重新选择模型")
        run = db.get_bt_run(run_id)
        if not run:
            raise ValueError("模型记录已被删除，请刷新")
        reason = _eligibility(run)
        if reason:
            raise ValueError(str(run.get("name") or run_id) + "：" + reason)
        fingerprints.update(_fingerprints(run))
        selected.append(run)
    dataset, market_fingerprints = _load_market(source_id, subject, selected, settings["holding_bars"])
    fingerprints.update(market_fingerprints)
    identity = adapters.asset_identity(dataset.market, dataset.symbol)
    if identity != (subject["market"], subject["symbol"]):
        raise ValueError("行情与模型预测的标的不一致，不能生成比较")
    bars = dataset.bars
    start, end = settings.get("start_date"), settings.get("end_date")
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期")
    bars = tuple(bar for bar in bars if (not start or bar.timestamp[:10] >= start) and (not end or bar.timestamp[:10] <= end))
    if len(bars) < 2:
        raise ValueError("所选日期内没有足够的真实 K 线，请调整时间范围")
    if len(bars) > 100_000:
        raise ValueError("单次比较最多使用 100000 根 K 线，请缩短日期范围或改用日 K")
    dataset = replace(dataset, bars=bars, fingerprint="")
    contestants = []
    for run in selected:
        try:
            contestants.append(adapters.load_contestant(run, subject, dataset))
        except ValueError as exc:
            raise ValueError(str(run.get("name") or run["id"]) + "：" + str(exc)) from exc
    rules = {key: settings[key] for key in ("holding_bars", "fee_bps", "slippage_bps", "initial_capital", "position_rule")}
    result = compare_on_market(dataset, contestants, rules)
    result["subject"] = _public_subject(subject)
    result["market"]["snapshot_hash"] = dataset.fingerprint
    result["market"]["source_label"] = next(source["label"] for source in subject["sources"] if source["id"] == source_id)
    result.setdefault("notes", []).extend([
        "本次收益由已保存的模型判断按共同交易规则重放，不重新调用模型。不同模型的原始研究流程和信息来源可能不同。",
        "基准为同一标的的收盘价买入持有收益，单独展示。模型收益包含每次买卖的手续费和滑点。",
        "同一根 K 线收盘前可得的判断在下一根开盘成交；期末持仓按末根收盘计价，未虚构强制卖出。",
    ])
    for source_path, digest in fingerprints.items():
        if file_sha256(source_path) != digest:
            raise ValueError("源数据在计算期间发生变化，请重新预览")
    preview_id = uuid.uuid4().hex
    frozen = {"version": VERSION, "created_at": db.now_iso(), "settings": settings,
              "run_ids": run_ids, "fingerprints": fingerprints, "result": result,
              "market_snapshot": {**dataset.summary(), "bars": [bar.to_dict() for bar in dataset.bars]}}
    # Keys and arbitrary local paths are never included in the public DTO.
    with _preview_path(preview_id).open("x", encoding="utf-8") as stream:
        import os
        os.chmod(stream.name, 0o600)
        json.dump(frozen, stream, ensure_ascii=False, allow_nan=False)
    return {"preview_id": preview_id, "result": result}


def save(preview_id: str, name: str) -> dict:
    with db.HISTORY_RELATION_LOCK, _SAVE_LOCK:
        return _save(preview_id, name)


def _save(preview_id: str, name: str) -> dict:
    _preview_path(preview_id)  # Validate even on idempotent requests.
    arena_id = "mc_" + preview_id
    existing = db.get_bt_arena(arena_id)
    if existing and existing.get("status") == "done":
        assert_readable(existing)
        return existing
    try:
        frozen = json.loads(_preview_path(preview_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("预览已不可用，请重新生成对比") from exc
    market_source_id = str(frozen["settings"].get("market_source_id") or "")
    source_run_id = market_source_id[4:] if market_source_id.startswith("run:") else None
    for run_id in list(frozen["run_ids"]) + ([source_run_id] if source_run_id else []):
        run = db.get_bt_run(run_id)
        if not run or _eligibility(run):
            raise ValueError("模型记录的状态或可见范围已变化，请重新预览")
    for source_path, digest in frozen["fingerprints"].items():
        if file_sha256(source_path) != digest:
            raise ValueError("预览后源数据发生变化，请重新预览再保存")
    result = frozen["result"]
    protocol_hash = hashlib.sha256(json.dumps({"market": result["market"], "rules": result["rules"],
        "subject": result["subject"], "version": VERSION}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        db.create_bt_arena(name=name, run_ids=frozen["run_ids"], dataset_name=result["subject"]["title"],
            arena_type="model_comparison", protocol_hash=protocol_hash, arena_id=arena_id,
            config={"schema_version": VERSION, "visibility": "private", "preview_id": preview_id,
                    "market_source_run_id": source_run_id,
                    "subject": result["subject"], "rules": result["rules"], "market": result["market"]})
    except sqlite3.IntegrityError:
        existing = db.get_bt_arena(arena_id)
        if not existing:
            raise
    db.update_bt_arena_status(arena_id, "done", result=result)
    return db.get_bt_arena(arena_id)


def assert_readable(arena: dict) -> None:
    """Do not turn an aggregate-only model record into a signal disclosure."""
    if arena.get("arena_type") != "model_comparison":
        return
    source_run_id = _record(arena.get("config")).get("market_source_run_id")
    for run_id in list(arena.get("run_ids") or []) + ([source_run_id] if source_run_id else []):
        run = db.get_bt_run(run_id)
        if run and not _private(run):
            raise ValueError("模型记录现仅提供安全摘要，不能展示含逐条信号的历史比较")
