"""Read-only saved model catalogue and genuine inputs for model-first Arena.

Catalogue entries are model configurations, not successful run records. Model
execution is deliberately owned by the Arena job runner, never this module.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..model_endpoint_security import redact_sensitive_text
from ..model_lab import repository as profiles
from ..model_lab.platform_default import environment_profile
from ..model_lab.providers import profile_snapshot
from ..quant_backtest.data import rows_to_bars
from ..quant_backtest.models import MarketDataset
from . import model_comparison_adapters as adapters
from .datasets import load_market_dataset_snapshot, normalize_frequency, verify_materialized_snapshot
from .evaluation_protocol import (
    MARKET_TIMEZONES,
    SUPPORTED_EVALUATION_HORIZONS,
    chronological_event_ids,
    select_events_for_execution_window,
)
from .models import ALL_HORIZONS, EventRecord
from .protocol import events_snapshot_sha256, file_sha256


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()[:24]


def _name(value: Any, default: str) -> str:
    # This DTO intentionally never contains connection URLs or secret refs.
    value = redact_sensitive_text(str(value or default))
    return re.sub(r"https?://[^\s）)]+", "模型服务", value)[:160]


def _profile_entry(identity: str, kind: str, profile: dict) -> tuple[dict, dict]:
    model_id = _name(profile.get("model_id"), "未配置")
    label = _name(profile.get("name"), model_id)
    name = f"Pronoia（{model_id}）· 平台默认" if identity == "pronoia:platform" else (
        f"Pronoia（{label}）" if kind == "pronoia" else label)
    reason = ("模型连接已停用" if not profile.get("is_active") else
              "请先在模型连接中配置 API Key" if not profile.get("secret_configured") else
              "请补全模型 ID 和接口地址" if not profile.get("model_id") or not profile.get("base_url") else None)
    public = {"id": identity, "name": name, "kind": kind, "model_id": model_id,
        "track": "event",
        "available": reason is None, "reason": reason,
        "description": "固定多 Agent 流程，仅本次对比使用该基模" if kind == "pronoia" else "独立大模型直接预测",
        "supported_target_kinds": ["event", "event_set"], "supported_frequencies": ["1d"]}
    return public, {**public, "profile_snapshot": profile_snapshot(profile)}


def _private(run: dict) -> bool:
    return str(run.get("visibility") or _dict(run.get("config")).get("visibility") or "private").replace("-", "_") == "private"


def _runs() -> list[dict]:
    result, known, offset = [], set(), 0
    while True:
        page = db.list_bt_runs(limit=1000, offset=offset)
        fresh = [run for run in page if str(run["id"]) not in known]
        result.extend(fresh)
        known.update(str(run["id"]) for run in fresh)
        if len(page) < 1000 or not fresh:
            return result
        offset += len(page)


def _strategy_identity(spec: dict) -> dict:
    from ..model_registry import strategy_identity
    return strategy_identity(spec)


def _saved_models(runs: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    from ..model_registry import merge_saved_models, run_model_id
    default = profiles.get_default_profile(public=False) or environment_profile()
    entry, frozen = _profile_entry("pronoia:platform", "pronoia", default)
    public, internal = [entry], {entry["id"]: frozen}
    for profile in profiles.list_profiles(active_only=False, public=False):
        for prefix, kind in (("raw", "external_model"), ("pronoia", "pronoia")):
            entry, frozen = _profile_entry(prefix + ":" + str(profile["id"]), kind, profile)
            public.append(entry)
            internal[entry["id"]] = frozen
    for run in runs:
        spec = deepcopy(_dict(run.get("strategy_spec")))
        runner = str(run.get("runner") or spec.get("runner") or "")
        is_quant = run.get("engine_mode") == "portfolio" or spec.get("type") in {"quant", "api"}
        is_import = (runner == "provided_analysis" or spec.get("adapter") in {"imported_decisions", "signal_file"}
                     or spec.get("kind") == "signal_file" or spec.get("source") == "signal_file")
        is_service = spec.get("adapter") == "external_http" or runner in {"external_http", "event_external_http"}
        if not (is_quant or is_import or is_service) or (is_quant and not spec.get("kind")):
            continue
        kind = "imported_predictions" if is_import else "quant" if is_quant else "external_service"
        semantics = _strategy_identity(spec)
        identity = run_model_id(run) or kind + ":" + _digest(semantics)
        if identity in internal and internal[identity]["available"]:
            continue
        label = _name(spec.get("name") or run.get("name"), "已保存量化模型" if is_quant else "已保存预测模型")
        reason = None
        description = "使用已保存的量化规则，在所选行情与时间段重新计算"
        if is_service and is_quant and spec.get("kind") != "return_forecast":
            description = "逐时点提交截至当时的行情，只接受当前时点返回的持仓预测"
            if not spec.get("endpoint"):
                reason = "该预测服务缺少接口地址"
        if is_service and not is_quant:
            description = "调用已保存的第三方事件预测服务"
            if not spec.get("endpoint"):
                reason = "该预测服务缺少接口地址"
        if is_import:
            description = "仅重放已导入预测对应的原事件或标的；不会生成其他对象的预测"
            if run.get("status") != "done" or not _private(run):
                reason = "导入预测尚无可读取的完整私有结果，请先完成原测试"
            elif not Path(str(run.get("result_path") if is_quant else run.get("out_path") or "")).is_file():
                reason = "原预测结果文件缺失，请重新导入"
        entry = {"id": identity, "name": label, "kind": kind,
            "available": reason is None, "reason": reason, "description": description,
            "supported_target_kinds": ["asset"] if is_quant else ["event", "event_set"]}
        if identity in internal:
            public = [old for old in public if old["id"] != identity]
        public.append(entry)
        internal[identity] = {**entry, "strategy_spec": spec, "source_run_id": str(run["id"]),
                              "execution_mode": "saved_predictions" if is_import else "quant" if is_quant else "external_service"}
    for model in internal.values():
        if model["kind"] == "imported_predictions":
            model["source_run_ids"] = [run["id"] for run in runs if run_model_id(run) == model["id"]]
    public, internal = merge_saved_models(public, internal, runs)
    public_by_id = {model["id"]: model for model in public}
    # The registry predates tracks and may rehydrate models from saved rows.
    # Derive the track from the executable definition, then overwrite legacy
    # broad target declarations so a model can never appear in both Arenas.
    for identity, model in internal.items():
        spec = _dict(model.get("strategy_spec"))
        track = "quant" if (
            model.get("kind") == "quant"
            or spec.get("type") in {"quant", "api"}
            or model.get("execution_mode") == "quant"
            or spec.get("kind") == "signal_file"
            or spec.get("source") == "signal_file"
        ) else "event"
        target_kinds = ["asset"] if track == "quant" else ["event", "event_set"]
        frequencies = None if track == "quant" else ["1d"]
        model.update(track=track, supported_target_kinds=target_kinds)
        if frequencies is not None:
            model["supported_frequencies"] = frequencies
        else:
            model.pop("supported_frequencies", None)
        if identity in public_by_id:
            public_by_id[identity].update(track=track, supported_target_kinds=target_kinds)
            if frequencies is not None:
                public_by_id[identity]["supported_frequencies"] = frequencies
            else:
                public_by_id[identity].pop("supported_frequencies", None)
    return public, internal


def _market_identity(item: dict) -> tuple[str, str] | None:
    symbols, markets = item.get("symbols") or [], item.get("markets") or []
    if len(symbols) != 1 or len(markets) != 1:
        return None
    try:
        return adapters.asset_identity(markets[0], symbols[0])
    except ValueError:
        return None


def _read_events(path: str) -> list[dict]:
    raw = Path(path).read_text(encoding="utf-8-sig")
    rows = json.loads(raw) if raw.lstrip().startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("事件文件必须包含事件对象")
    return rows


def _local_day(value: Any, market: str) -> str:
    instant = adapters._instant(value, market)
    return instant.astimezone(ZoneInfo(MARKET_TIMEZONES[market])).date().isoformat()


def _dataset_contract(item: dict) -> dict:
    """Return the current immutable version while retaining display metadata."""
    contract = deepcopy(item)
    dataset_id = str(item.get("id") or "")
    version_id = str(item.get("dataset_version") or item.get("current_version") or "")
    version = db.get_bt_dataset_version(dataset_id, version_id) if dataset_id and version_id else None
    if version:
        for key, value in version.items():
            if key not in {"id", "dataset_id"}:
                contract[key] = deepcopy(value)
        contract["dataset_version"] = version_id
        # Older rows attached the Oracle to the current dataset row before the
        # versions table gained labels_path.
        if not contract.get("labels_path") and version_id == str(item.get("current_version") or item.get("dataset_version") or ""):
            contract["labels_path"] = item.get("labels_path")
    return contract


def _frequency(value: Any) -> str | None:
    normalized = normalize_frequency(value)
    return normalized if normalized in {"1d", "1m", "5m", "15m", "30m", "60m"} else None


def _frequency_order(value: str) -> tuple[int, str]:
    order = {"1d": 0, "1m": 1, "5m": 2, "15m": 3, "30m": 4, "60m": 5}
    return order.get(value, 99), value


def _asset_target_id(market: str, symbol: str) -> str:
    return f"asset:{market}:{symbol}"


def _asset_name(value: Any, symbol: str) -> str:
    name = _name(value, symbol)
    # A grouped target must not keep the label of whichever frequency happened
    # to be selected as the default snapshot.
    stripped = re.sub(
        r"(?:\s*[·|/_-]\s*)?(?:日\s*[Kk]|(?:1|5|15|30|60)\s*(?:m|min|分钟)\s*[Kk]?|分钟\s*[Kk])"
        r"(?:\s*[（(]\s*\d+\s*根?\s*[）)])?\s*$",
        "", name, flags=re.IGNORECASE,
    ).strip()
    return stripped or symbol


def _event_set_target_id(dataset_id: str, dataset_version: str | None,
                         snapshot_hash: str, labels_sha256: str | None) -> str:
    revision = _digest({"version": dataset_version, "snapshot": snapshot_hash,
                        "labels": labels_sha256})
    return f"event-set:{dataset_id}:{revision}"


def _oracle_summary(labels_path: Any) -> tuple[dict, list[dict]]:
    path = Path(str(labels_path or ""))
    if not path.is_file():
        return {"status": "unavailable", "labels_sha256": None,
                "available_horizons": [], "return_unit": "decimal"}, []
    before = file_sha256(path)
    try:
        rows = _read_events(str(path))
    except (OSError, ValueError, UnicodeError):
        return {"status": "invalid", "labels_sha256": before,
                "available_horizons": [], "return_unit": "decimal"}, []
    if before != file_sha256(path):
        return {"status": "changed", "labels_sha256": before,
                "available_horizons": [], "return_unit": "decimal"}, []
    horizons = [horizon for horizon in SUPPORTED_EVALUATION_HORIZONS if any(
        row.get("label_" + horizon) in {"up", "down", "neutral"} for row in rows
    )]
    if not horizons:
        return {"status": "invalid", "labels_sha256": before,
                "available_horizons": [], "return_unit": "decimal",
                "reason": "Oracle 标签没有 Arena 支持的方向窗口"}, rows
    epsilons = {_finite(row.get("epsilon")) for row in rows if row.get("epsilon") is not None}
    if None in epsilons or len(epsilons) > 1:
        return {"status": "invalid", "labels_sha256": before,
                "available_horizons": horizons, "return_unit": "decimal",
                "reason": "Oracle 标签包含无效或不一致的 epsilon"}, rows
    return {"status": "available", "labels_sha256": before,
            "available_horizons": horizons, "return_unit": "decimal",
            "truth_basis": "market_excess", "label_count": len(rows),
            "oracle_epsilon": next(iter(epsilons), 0.005)}, rows


def _targets(datasets: list[dict], runs: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    public: dict[str, dict] = {}
    internal: dict[str, dict] = {}
    assets: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    for item in datasets:
        if item.get("dataset_kind") != "market":
            continue
        contract = _dataset_contract(item)
        identity = _market_identity(contract)
        frequency = _frequency(contract.get("frequency"))
        if (not identity or not frequency or not Path(str(contract.get("path") or "")).is_file()
                or str(contract.get("status") or "available") != "available"):
            continue
        assets.setdefault(identity, []).append((contract, frequency))
    for (market, symbol), choices in assets.items():
        # list_bt_datasets is newest-first.  Keep one unambiguous frozen source
        # for each frequency and expose the mapping without leaking file paths.
        by_frequency: dict[str, dict] = {}
        for contract, frequency in choices:
            by_frequency.setdefault(frequency, contract)
        options = []
        for frequency in sorted(by_frequency, key=_frequency_order):
            contract = by_frequency[frequency]
            coverage = _dict(contract.get("coverage"))
            start, end = coverage.get("start_at"), coverage.get("end_at")
            options.append({
                "frequency": frequency,
                "dataset_id": str(contract.get("id") or ""),
                "dataset_version": contract.get("dataset_version") or contract.get("current_version"),
                "snapshot_hash": contract.get("snapshot_hash"),
                "name": _name(contract.get("name"), symbol),
                "start_date": str(start)[:10] if start else None,
                "end_date": str(end)[:10] if end else None,
                "adjustment": contract.get("adjustment"),
                "calendar": contract.get("calendar"),
            })
        default_frequency = "1d" if "1d" in by_frequency else options[0]["frequency"]
        default = next(option for option in options if option["frequency"] == default_frequency)
        target_id = _asset_target_id(market, symbol)
        legacy_ids = list(dict.fromkeys(
            "dataset:" + str(contract.get("id") or "") for contract, _frequency_value in choices
        ))
        entry = {
            "id": target_id, "kind": "asset", "target_scope": "asset", "track": "quant",
            "name": _asset_name(default["name"], symbol), "symbol": symbol, "market": market,
            "frequency": options[0]["frequency"] if len(options) == 1 else None,
            "default_frequency": default_frequency, "available_frequencies": options,
            "start_date": default["start_date"], "end_date": default["end_date"],
            "legacy_target_ids": legacy_ids,
        }
        public[target_id] = entry
        internal[target_id] = {"public": entry, "market_contracts": by_frequency}
        for contract, frequency in choices:
            alias = "dataset:" + str(contract.get("id") or "")
            internal[alias] = {"public": {**entry, "id": alias, "frequency": frequency},
                               "market_contracts": {frequency: contract}, "forced_frequency": frequency,
                               "canonical_target_id": target_id}

    event_datasets: list[tuple[str, Any, list[str], dict | None]] = []
    for item in datasets:
        if item.get("dataset_kind", "event") != "event":
            continue
        contract = _dataset_contract(item)
        event_datasets.append((str(contract.get("path") or ""), item.get("name"), [], contract))
    dataset_paths = {path for path, _name_value, _run_ids, _contract in event_datasets if path}
    run_sources: dict[str, list[str]] = {}
    # Unsaved manual inputs can still belong to a run. No success prerequisite,
    # while Arena-safe rows do not reveal their underlying private event facts.
    for run in runs:
        path = str(run.get("events_path") or "")
        if not path or path in dataset_paths or not _private(run) or run.get("engine_mode") == "portfolio":
            continue
        run_sources.setdefault(path, []).append(str(run["id"]))
    sources = [*event_datasets, *((path, None, ids, None) for path, ids in run_sources.items())]
    for path, source_name, source_run_ids, contract in sources:
        if not path or not Path(path).is_file():
            continue
        try:
            rows = _read_events(path)
        except (OSError, ValueError, UnicodeError):
            continue
        parsed: list[tuple[dict, EventRecord, str, str, str, dt.date]] = []
        for row in rows:
            try:
                event = adapters._event_record(row)
                key = adapters._event_key(row)
                market, symbol = adapters.asset_identity(event.market, event.symbol)
                available_date = dt.date.fromisoformat(_local_day(event.available_time or event.event_time, market))
            except (ValueError, KeyError, TypeError, OverflowError):
                continue
            parsed.append((row, event, key, market, symbol, available_date))
        if contract is not None and parsed and len(parsed) == len(rows):
            event_ids = [event.event_id for _, event, *_rest in parsed]
            if all(event_ids) and len(event_ids) == len(set(event_ids)):
                oracle, _ = _oracle_summary(contract.get("labels_path"))
                try:
                    actual_snapshot_hash = events_snapshot_sha256(path)
                    snapshot_hash = str(contract.get("snapshot_hash") or actual_snapshot_hash)
                except (OSError, ValueError, UnicodeError):
                    actual_snapshot_hash = ""
                    snapshot_hash = ""
                if snapshot_hash:
                    dataset_id = str(contract.get("id") or "")
                    version = str(contract.get("dataset_version") or contract.get("current_version") or "") or None
                    dates = [item[-1] for item in parsed]
                    markets = sorted({item[3] for item in parsed})
                    symbols = sorted({item[4] for item in parsed})
                    target_id = _event_set_target_id(dataset_id, version, snapshot_hash, oracle.get("labels_sha256"))
                    entry = {
                        "id": target_id, "kind": "event_set", "target_scope": "event_set", "track": "event",
                        "name": _name(source_name, "未命名事件集"),
                        "symbol": symbols[0] if len(symbols) == 1 else f"{len(symbols)} 个标的",
                        "market": markets[0] if len(markets) == 1 else "MULTI",
                        "symbols": symbols, "markets": markets, "event_count": len(parsed),
                        "event_start_date": min(dates).isoformat(), "event_end_date": max(dates).isoformat(),
                        "start_date": min(dates).isoformat(), "end_date": max(dates).isoformat(),
                        "frequency": "1d", "supported_frequencies": ["1d"],
                        "dataset_id": dataset_id, "dataset_version": version,
                        "snapshot_hash": snapshot_hash, "oracle": oracle,
                        "available": oracle.get("status") == "available" and actual_snapshot_hash == snapshot_hash,
                        "reason": (
                            "事件集冻结快照内容已变化" if actual_snapshot_hash != snapshot_hash
                            else None if oracle.get("status") == "available"
                            else "事件集尚未关联可用的冻结 Oracle 标签"
                        ),
                    }
                    public[target_id] = entry
                    internal[target_id] = {
                        "public": entry, "event_set_contract": contract,
                        "events_path": path, "events": [item[1] for item in parsed],
                        "event_keys": [item[2] for item in parsed],
                        "labels_sha256": oracle.get("labels_sha256"), "source_run_ids": source_run_ids,
                    }
        for row, event, key, market, symbol, available_date in parsed:
            if key in public:
                existing = internal[key]
                existing["source_run_ids"] = list(dict.fromkeys([
                    *existing.get("source_run_ids", []), *source_run_ids,
                ]))
                continue
            entry = {"id": key, "key": key, "kind": "event", "target_scope": "single_event", "track": "event",
                     "name": _name(event.title, "未命名事件"),
                     "symbol": symbol, "market": market, "event_time": event.event_time, "frequency": "1d",
                     "supported_frequencies": ["1d"],
                     "start_date": (available_date - dt.timedelta(days=7)).isoformat(),
                     "end_date": (available_date + dt.timedelta(days=30)).isoformat()}
            public[key] = entry
            internal[key] = {"public": entry, "event": event, "events_path": path,
                             "source_run_ids": source_run_ids}
    order = {"asset": 0, "event": 1, "event_set": 2}
    return sorted(public.values(), key=lambda item: (order.get(item.get("kind"), 99), item.get("name", ""))), internal


def catalog() -> dict:
    """Return only user-facing metadata; never URLs, paths, or credentials."""
    runs, datasets = _runs(), db.list_bt_datasets()
    models, frozen_models = _saved_models(runs)
    targets, frozen_targets = _targets(datasets, runs)
    runs_by_id = {str(run["id"]): run for run in runs}
    for model in models:
        if model["kind"] != "imported_predictions" or not model["available"]:
            continue
        frozen = frozen_models[model["id"]]
        compatible = set()
        for run_id in frozen.get("source_run_ids") or [frozen.get("source_run_id")]:
            run = runs_by_id.get(run_id)
            if not run or run.get("status") != "done" or not _private(run):
                continue
            try:
                description = adapters.describe_run(run)
                keys = {item["key"] for item in description["subjects"] if item["kind"] == "event"}
                assets = {(item["market"], item["symbol"]) for item in description["subjects"] if item["kind"] == "asset"}
                for target in targets:
                    if target["kind"] == "event":
                        matched = target["id"] in keys and frozen.get("track") == "event"
                    elif target["kind"] == "event_set":
                        event_keys = set(frozen_targets[target["id"]].get("event_keys") or [])
                        matched = bool(event_keys) and event_keys.issubset(keys) and frozen.get("track") == "event"
                    else:
                        matched = (target["market"], target["symbol"]) in assets and frozen.get("track") == "quant"
                    if matched:
                        compatible.add(target["id"])
            except (ValueError, KeyError, TypeError, OSError):
                continue
        model["compatible_target_ids"] = sorted(compatible)
        if not compatible:
            model.update(available=False, reason="未找到原预测对应的事件或标的，请先恢复其输入数据")
    return {
        "tracks": [
            {"id": "quant", "target_scopes": ["asset"],
             "frequencies": ["1d", "1m", "5m", "15m", "30m", "60m"]},
            {"id": "event", "target_scopes": ["single_event", "event_set"],
             "frequencies": ["1d"]},
        ],
        "models": models,
        "targets": targets,
    }


def resolve_model(model_id: str) -> dict:
    """Resolve the selected current configuration without changing defaults."""
    _, models = _saved_models(_runs())
    model = models.get(model_id)
    if model is None:
        raise ValueError("所选模型已删除或配置发生变化，请刷新后重新选择")
    if not model["available"]:
        raise ValueError(model["name"] + "：" + str(model["reason"]))
    return deepcopy(model)


def target_source_run_ids(target_id: str) -> list[str]:
    """Return private Run lineage needed to keep an event target resolvable."""
    runs, datasets = _runs(), db.list_bt_datasets()
    _, targets = _targets(datasets, runs)
    selected = targets.get(target_id)
    if not selected:
        raise ValueError("所选事件或指数已不存在，请刷新后重新选择")
    return list(dict.fromkeys(str(item) for item in selected.get("source_run_ids") or [] if str(item)))


def _date(value: Any, fallback: Any = None) -> str | None:
    value = value or fallback
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ValueError("日期请使用 YYYY-MM-DD 格式") from exc


def _filter(dataset: MarketDataset, start: str | None, end: str | None) -> tuple[MarketDataset, MarketDataset]:
    dates = [_local_day(bar.timestamp, dataset.market) for bar in dataset.bars]
    bars = tuple(bar for bar, day in zip(dataset.bars, dates) if (not start or day >= start) and (not end or day <= end))
    if len(bars) < 2:
        raise ValueError("所选日期内至少需要 2 根真实 K 线，请调整日期或导入该时段行情")
    if len(bars) > 100_000:
        raise ValueError("所选时段超过 100000 根 K 线，请缩短时间或选择日 K")
    history = tuple(bar for bar, day in zip(dataset.bars, dates) if not end or day <= end)
    return replace(dataset, bars=bars, fingerprint=""), replace(dataset, bars=history, fingerprint="")


def _load_local(item: dict) -> MarketDataset:
    path = str(item.get("path") or "")
    if item.get("snapshot_hash"):
        verified, reason = verify_materialized_snapshot({**item, "dataset_kind": "market"})
        if not verified:
            raise ValueError("冻结行情快照校验失败：" + str(reason or "内容已变化"))
    before = file_sha256(path)
    if not before:
        raise ValueError("本地行情文件已不存在，请重新导入")
    dataset = load_market_dataset_snapshot(item)
    if before != file_sha256(path):
        raise ValueError("读取期间行情文件发生变化，请重新创建 Arena")
    market, symbol = adapters.asset_identity(dataset.market, dataset.symbol)
    return replace(dataset, market=market, symbol=symbol, fingerprint="")


def _fetch_event_market(event: EventRecord, target: dict, start: str, end: str) -> MarketDataset:
    from .performance import fetch_event_market_series
    event_date = dt.date.fromisoformat(_local_day(event.event_time, target["market"]))
    start_day, end_day = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    before, after = max(120, (event_date - start_day).days + 7), max(30, (end_day - event_date).days + 7)
    if before > 3650 or after > 3650:
        raise ValueError("自动事件行情最多覆盖事件前后各 10 年；更长时间请导入本地行情")
    try:
        payload = fetch_event_market_series(event, days_before=before, days_after=after)
    except Exception as exc:
        raise ValueError("暂时无法获取真实事件行情，请在数据管理导入同标的 OHLC 行情") from exc
    if adapters.asset_identity(payload.get("market") or target["market"], payload.get("symbol")) != (target["market"], target["symbol"]):
        raise ValueError("行情服务返回了其他标的数据，请改用同标的本地行情")
    dates, values = payload.get("dates") or [], payload.get("ohlc") or []
    if payload.get("series_type") != "ohlc" or not dates or len(dates) != len(values):
        raise ValueError("行情服务缺少真实开盘价；请导入 open/high/low/close 行情，不能用收盘价伪造 K 线")
    if any(not isinstance(row, (list, tuple)) or len(row) != 4 for row in values):
        raise ValueError("行情服务返回的 OHLC 不完整，请导入本地行情")
    bars, audit = rows_to_bars([{"timestamp": day, "open": row[0], "close": row[1], "low": row[2], "high": row[3]}
                               for day, row in zip(dates, values)], repair_ohlc=False)
    return MarketDataset(name="事件标的真实日 K", market=target["market"], symbol=target["symbol"], frequency="1d",
        bars=bars, source_type="event_market_snapshot", metadata={**audit, "source": payload.get("source"),
        "adjustment": payload.get("adjustment"), "fetched_at": payload.get("fetched_at")})


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _truth_candidate(row: dict) -> dict | None:
    horizons = {}
    for horizon in ALL_HORIZONS:
        direction = row.get("label_" + horizon)
        if direction not in {"up", "down", "neutral"}:
            continue
        horizons[horizon] = {
            "horizon": horizon,
            "direction": direction,
            "label": direction,
            # Oracle files use decimal returns (0.02 = +2%). Keep raw asset,
            # benchmark and Oracle/CAR series distinct; execution chooses its
            # declared PnL basis rather than inferring one from the label.
            "asset_return": _finite(row.get("ret_" + horizon)),
            "actual_return": _finite(row.get("ret_" + horizon)),  # compatibility alias
            "benchmark_return": _finite(row.get("bm_ret_" + horizon)),
            "excess_return": _finite(row.get("car_" + horizon)),
            "oracle_return": _finite(row.get("car_" + horizon)),
            "return_unit": "decimal",
        }
    if not horizons:
        return None
    return {
        "truth_basis": "market_excess", "return_unit": "decimal", "horizons": horizons,
        "benchmark_ticker": _name(row.get("benchmark_ticker"), "") or None,
        "car_method": _name(row.get("car_method"), "") or None,
    }


def _merge_truth(candidates: list[dict]) -> tuple[dict | None, list[str]]:
    if not candidates:
        return None, ["该事件暂无可核对的已保存方向真值；不会用标的涨跌替代相对基准的事件方向标签。"]
    merged = deepcopy(candidates[0])
    for candidate in candidates[1:]:
        if any(candidate.get(key) != merged.get(key) for key in ("benchmark_ticker", "car_method")):
            return None, ["该事件存在不同基准或计算方法的方向标签，本次暂不计算事件方向准确率。"]
        for horizon, value in candidate["horizons"].items():
            if horizon in merged["horizons"] and merged["horizons"][horizon] != value:
                return None, ["该事件的已保存方向标签或收益数据存在冲突，本次暂不计算事件方向准确率。"]
            merged["horizons"][horizon] = value
    return merged, []


def _event_truth(target: dict, datasets: list[dict], runs: list[dict]) -> tuple[dict | None, list[str]]:
    """Read matched existing labels separately from model-visible event facts."""
    sources = [(item.get("path"), item.get("labels_path")) for item in datasets if item.get("dataset_kind", "event") == "event"]
    sources.extend((run.get("events_path"), run.get("labels_path")) for run in runs if _private(run))
    seen, candidates = set(), []
    for event_path, label_path in sources:
        if not event_path or not label_path or (event_path, label_path) in seen:
            continue
        seen.add((event_path, label_path))
        try:
            rows = _read_events(str(event_path))
            event_ids = {str(row.get("event_id") or row.get("id") or "") for row in rows
                         if adapters._event_key(row) == target["id"]}
            if not event_ids:
                continue
            labels = [row for row in _read_events(str(label_path))
                      if str(row.get("event_id") or row.get("id") or "") in event_ids]
            for row in labels:
                if row.get("symbol") and adapters.asset_identity(row.get("market") or target["market"], row["symbol"]) != (target["market"], target["symbol"]):
                    continue
                if row.get("event_time") and _local_day(row["event_time"], target["market"]) != _local_day(target["event_time"], target["market"]):
                    continue
                candidate = _truth_candidate(row)
                if candidate:
                    candidates.append(candidate)
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            continue
    return _merge_truth(candidates)


def _event_market_case(event: EventRecord, target: dict, start: str, end: str,
                       datasets: list[dict]) -> tuple[MarketDataset, MarketDataset]:
    """Resolve one shared daily market convention for an event case."""
    available = _local_day(event.available_time or event.event_time, target["market"])
    candidates = []
    for item in datasets:
        if item.get("dataset_kind") != "market":
            continue
        contract = _dataset_contract(item)
        if (_market_identity(contract) == (target["market"], target["symbol"])
                and _frequency(contract.get("frequency")) == "1d"):
            candidates.append(contract)
    for contract in candidates:
        try:
            dataset = _load_local(contract)
            window, history = _filter(dataset, start, end)
            closes = adapters.bar_close_times(window)
            instant = adapters._instant(event.available_time or event.event_time,
                                        target["market"], date_end=True)
            if closes[-1] <= instant or _local_day(window.bars[0].timestamp, window.market) > available:
                continue
            return window, history
        except (ValueError, OSError):
            continue
    dataset = _fetch_event_market(event, target, start, end)
    return _filter(dataset, start, end)


def _event_truth_from_rows(event: EventRecord, target: dict,
                           label_rows: list[dict]) -> tuple[dict | None, list[str]]:
    candidates = []
    for row in label_rows:
        if str(row.get("event_id") or row.get("id") or "") != event.event_id:
            continue
        try:
            if row.get("symbol") and adapters.asset_identity(
                row.get("market") or target["market"], row["symbol"],
            ) != (target["market"], target["symbol"]):
                continue
            if row.get("event_time") and _local_day(
                row["event_time"], target["market"],
            ) != _local_day(target["event_time"], target["market"]):
                continue
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
        candidate = _truth_candidate(row)
        if candidate:
            candidates.append(candidate)
    return _merge_truth(candidates)


def _resolve_event_set(selected: dict, target: dict, start: str, end: str) -> dict:
    contract = deepcopy(selected["event_set_contract"])
    path = str(contract.get("path") or selected.get("events_path") or "")
    expected_snapshot = str(target.get("snapshot_hash") or "")
    if not path or not Path(path).is_file():
        raise ValueError("所选事件集的冻结快照已不存在，请重新导入")
    try:
        before = events_snapshot_sha256(path)
        if expected_snapshot and before != expected_snapshot:
            raise ValueError("所选事件集的冻结快照内容已变化，请刷新后重新选择")
        rows = _read_events(path)
        if before != events_snapshot_sha256(path):
            raise ValueError("读取期间事件集快照发生变化，请重新创建 Arena")
        pairs = [(row, adapters._event_record(row), adapters._event_key(row)) for row in rows]
    except (OSError, UnicodeError, KeyError, TypeError) as exc:
        raise ValueError("所选事件集的冻结快照无法读取，请重新导入") from exc
    event_ids = [event.event_id for _row, event, _key in pairs]
    if not event_ids or any(not item for item in event_ids) or len(event_ids) != len(set(event_ids)):
        raise ValueError("事件集必须包含不重复且非空的 event_id")

    selection = select_events_for_execution_window(
        [event for _row, event, _key in pairs],
        {"start_date": start, "end_date": end},
    )
    if not selection.events:
        raise ValueError("所选时间段内没有可评测事件，请调整日期")
    selected_by_id = {event.event_id: event for event in selection.events}
    pair_by_id = {event.event_id: (row, event, key) for row, event, key in pairs}
    ordered_ids = chronological_event_ids(selection.events)

    oracle, label_rows = _oracle_summary(contract.get("labels_path"))
    expected_labels = selected.get("labels_sha256")
    if expected_labels and oracle.get("labels_sha256") != expected_labels:
        raise ValueError("所选事件集的 Oracle 标签快照已变化，请刷新后重新选择")
    if oracle["status"] in {"invalid", "changed"}:
        raise ValueError("所选事件集的 Oracle 标签无法读取或在读取期间发生变化")
    if oracle["status"] != "available":
        raise ValueError("Event Arena 的事件集必须关联已冻结的 Oracle 标签")

    cases = []
    notes: list[str] = []
    for event_id in ordered_ids:
        _row, event, key = pair_by_id[event_id]
        market, symbol = adapters.asset_identity(event.market, event.symbol)
        case_target = {
            "id": key, "key": key, "kind": "event", "target_scope": "single_event",
            "track": "event", "name": _name(event.title, "未命名事件"),
            "symbol": symbol, "market": market, "event_time": event.event_time,
            "frequency": "1d", "start_date": start, "end_date": end,
            "event_set_id": target["id"], "dataset_id": target.get("dataset_id"),
            "dataset_version": target.get("dataset_version"),
        }
        truth, case_notes = _event_truth_from_rows(event, case_target, label_rows)
        notes.extend(f"{case_target['name']}：{message}" for message in case_notes)
        cases.append({
            "target": case_target, "event": event, "dataset": None,
            "history_dataset": None, "event_truth": truth, "notes": case_notes,
        })
    target.update(
        start_date=start, end_date=end, frequency="1d", oracle=oracle,
        selected_event_count=len(cases), selection=selection.to_dict(),
    )
    return {
        "target": target, "event": None,
        "events": [selected_by_id[event_id] for event_id in ordered_ids],
        "event_cases": cases, "event_truth": None,
        "notes": list(dict.fromkeys(notes)),
        "event_set_snapshot": {
            "dataset_id": target.get("dataset_id"),
            "dataset_version": target.get("dataset_version"),
            "snapshot_hash": expected_snapshot or before,
            "labels_sha256": oracle.get("labels_sha256"),
            "oracle": oracle,
        },
    }


def resolve_target(target_id: str, start_date: str | None = None,
                   end_date: str | None = None, frequency: str | None = None) -> dict:
    """Freeze one Quant asset snapshot or one Event target/event-set snapshot.

    ``dataset`` is the evaluation window. ``history_dataset`` includes earlier
    data for indicator warm-up, never bars after the requested end date. Event
    sets return immutable event facts and Oracle rows inside ``event_cases``;
    they deliberately do not fetch one market series per event.
    """
    runs, datasets = _runs(), db.list_bt_datasets()
    _, targets = _targets(datasets, runs)
    selected = targets.get(target_id)
    if not selected:
        raise ValueError("所选事件、事件集或行情已不可用，请刷新或重新导入数据")
    target = deepcopy(selected["public"])
    requested_frequency = None
    if frequency is not None:
        requested_frequency = _frequency(frequency)
        if not requested_frequency:
            raise ValueError("K 线周期仅支持 1d、1m、5m、15m、30m、60m")

    if target["kind"] == "asset":
        forced = selected.get("forced_frequency")
        if requested_frequency and forced and requested_frequency != forced:
            raise ValueError("旧版行情目标已绑定其他 K 线周期，请重新选择标的")
        chosen_frequency = requested_frequency or forced or target.get("frequency") or target.get("default_frequency")
        contracts = selected.get("market_contracts") or {}
        contract = contracts.get(chosen_frequency)
        if contract is None:
            available = "、".join(sorted(contracts, key=_frequency_order))
            raise ValueError(f"所选标的没有 {chosen_frequency} 行情；可用周期：{available}")
        coverage = _dict(contract.get("coverage"))
        start = _date(start_date, coverage.get("start_at") or target.get("start_date"))
        end = _date(end_date, coverage.get("end_at") or target.get("end_date"))
        if start and end and start > end:
            raise ValueError("开始日期不能晚于结束日期")
        dataset = _load_local(contract)
        if _frequency(dataset.frequency) != chosen_frequency:
            raise ValueError("冻结行情声明的 K 线周期与所选周期不一致")
        window, history = _filter(dataset, start, end)
        target.update(
            id=selected.get("canonical_target_id") or target["id"],
            start_date=start or _local_day(window.bars[0].timestamp, window.market),
            end_date=end or _local_day(window.bars[-1].timestamp, window.market),
            frequency=chosen_frequency,
            dataset_id=str(contract.get("id") or ""),
            dataset_version=contract.get("dataset_version") or contract.get("current_version"),
            snapshot_hash=contract.get("snapshot_hash"),
            adjustment=contract.get("adjustment"), calendar=contract.get("calendar"),
        )
        return {"target": target, "dataset": window, "history_dataset": history,
                "event": None, "event_truth": None, "notes": []}

    if requested_frequency not in {None, "1d"}:
        raise ValueError("Event Arena 固定使用事件 Oracle 的日线评价窗口，不能改为分钟 K")
    start = _date(start_date, target.get("start_date"))
    end = _date(end_date, target.get("end_date"))
    if not start or not end:
        raise ValueError("事件比较必须提供完整的开始和结束日期")
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if target["kind"] == "event_set":
        return _resolve_event_set(selected, target, start, end)

    event = selected.get("event")
    if event is None:
        raise ValueError("所选单事件已不存在，请刷新后重新选择")
    available = _local_day(event.available_time or event.event_time, target["market"])
    if available < start or available > end:
        raise ValueError("所选时间段必须包含该事件的信息可得日期")
    window, history = _event_market_case(event, target, start, end, datasets)
    target.update(start_date=start, end_date=end, frequency="1d")
    truth, notes = _event_truth(target, datasets, runs)
    return {"target": target, "dataset": window, "history_dataset": history,
            "event": event, "event_truth": truth, "notes": notes}
