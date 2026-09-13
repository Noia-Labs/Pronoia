"""Persistent model identity, independent of test datasets, dates and costs."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any

from . import db
from .model_endpoint_security import redact_sensitive_text

_TEST_FIELDS = {
    "dataset_id", "dataset_version", "dataset_name", "dataset", "events_path", "labels_path", "out_path", "result_path",
    "symbol", "symbols", "market", "markets", "frequency", "start", "end", "start_date", "end_date", "start_at", "end_at",
    "benchmark", "benchmark_ticker", "execution_spec", "execution", "initial_capital", "fee_bps", "slippage_bps",
    "commission_bps", "stamp_duty_bps", "other_cost_bps", "minimum_commission", "holding_bars", "holding_horizon",
    "horizon_bars", "horizon", "evaluation_horizon", "primary_oracle_horizon", "evaluation_protocol", "protocol",
    "name", "description", "rules_summary", "engine_mode", "result_nature", "input_contract",
    "prediction_only", "point_in_time_enforced", "forecast_contract", "contract_version",
}
_SECRET_KEYS = {"headers", "secret_env_ref", "api_key", "authorization", "api_token", "password", "secret", "token", "secret_ref"}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()[:24]


def _omit_default_confirmations(spec: dict) -> None:
    """An explicit one-close confirmation is the existing rule behavior."""
    if spec.get("type") != "quant" or spec.get("kind") != "declarative_rules":
        return
    for side in ("entry", "exit"):
        group = _dict(_dict(spec.get("parameters")).get(side))
        rules = group.get("conditions") if isinstance(group.get("conditions"), list) else group.get("rules")
        for rule in rules if isinstance(rules, list) else []:
            if not isinstance(rule, dict) or isinstance(rule.get("consecutive_count"), bool):
                continue
            try:
                if float(rule.get("consecutive_count", 0)) == 1:
                    rule.pop("consecutive_count", None)
            except (ValueError, TypeError, OverflowError):
                pass  # Full rule validation rejects invalid values on save.


def strategy_identity(spec: dict) -> dict:
    """Keep algorithm parameters; drop every dataset/window/execution field."""
    spec = deepcopy(spec)
    spec.setdefault("version", "1")
    if spec.get("type") == "quant":
        spec.setdefault("kind", spec.get("model_id"))
        spec.pop("model_id", None)
        spec.setdefault("adapter", "signal_file" if spec.get("kind") == "signal_file" else "builtin")
        if spec.get("kind") == "return_forecast":
            spec.setdefault("source", "builtin")
    _omit_default_confirmations(spec)
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): clean(item) for key, item in value.items()
                    if str(key) not in _TEST_FIELDS | {"path"} and item not in (None, {}, [])}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    return clean(spec)


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()
                if str(key).lower() not in _SECRET_KEYS and not any(word in str(key).lower() for word in ("secret", "credential"))}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        value = redact_sensitive_text(value)
        value = re.sub(r"\bPRONOIA_(?:MODEL|STRATEGY)_SECRET_[A-Z0-9_]+\b", "[密钥引用已隐藏]", value)
        return re.sub(r"\b(?:local-model|local-strategy):[a-f0-9]+\b", "[密钥引用已隐藏]", value)
    return value


def _name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 120:
        raise ValueError("模型名称请填写 1–120 个字符")
    return _safe(name)


def _rows() -> list[dict]:
    try:
        with db._lock:
            rows = db._get_conn().execute("SELECT * FROM bt_saved_models ORDER BY created_at,id").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    result = []
    for row in rows:
        item = dict(row)
        item["definition"] = json.loads(item.pop("definition_json"))
        result.append(item)
    return result


def _deleted_model_ids() -> set[str]:
    with db._lock:
        try:
            rows = db._get_conn().execute("SELECT model_id FROM bt_model_tombstones").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return set()
            raise
    return {row["model_id"] for row in rows}


def _semantic_id(kind: str, spec: dict) -> str:
    return kind + ":" + _digest(strategy_identity(spec))


def _saved_identity(semantic_id: str) -> str:
    """An edited configuration keeps its catalog ID as its semantics change."""
    with db._lock:
        try:
            row = db._get_conn().execute("SELECT id FROM bt_saved_models WHERE identity_key=?", (semantic_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return semantic_id
            raise
    return row["id"] if row else semantic_id


def _run_kind(run: dict) -> str | None:
    spec = _dict(run.get("strategy_spec")) or _dict(_dict(run.get("config")).get("strategy_spec"))
    runner = str(run.get("runner") or spec.get("runner") or "")
    if runner == "provided_analysis" or spec.get("adapter") in {"signal_file", "imported_decisions"} or spec.get("source") == "signal_file":
        return "imported_predictions"
    if run.get("engine_mode") == "portfolio" or spec.get("type") in {"quant", "api"}:
        return "quant"
    if spec.get("adapter") == "external_http" or runner == "event_external_http":
        return "external_service"
    return None


def run_model_id(run: dict) -> str:
    cfg = _dict(run.get("config"))
    if cfg.get("saved_model_id"):
        return str(cfg["saved_model_id"])
    kind = _run_kind(run)
    if kind:
        if kind == "imported_predictions":
            spec = _dict(run.get("strategy_spec")) or _dict(cfg.get("strategy_spec"))
            # Historical uploads have no reliable reusable model identity.
            # Preserve explicitly declared provenance instead of treating every
            # empty imported_decisions adapter as the same financial model.
            source = cfg.get("source") or spec.get("path") or run.get("events_path") or run.get("result_path")
            return kind + ":" + _digest({"strategy": strategy_identity(spec), "source": source,
                                          "model_version": run.get("model_version")})
        return _saved_identity(_semantic_id(kind, _dict(run.get("strategy_spec")) or _dict(cfg.get("strategy_spec"))))
    runner = str(run.get("runner") or "")
    profile_id = str(cfg.get("model_profile_id") or _dict(cfg.get("model_profile_snapshot")).get("id") or "")
    if runner == "raw_model":
        return "raw:" + profile_id if profile_id else ""
    if runner in {"team_full", "team_prompt", "baseline"}:
        if not profile_id or profile_id == "__platform_default__" or profile_id.startswith("__platform_env__:"):
            return "pronoia:platform" if runner != "baseline" else ""
        return "pronoia:" + profile_id
    return ""


def merge_saved_models(public: list[dict], internal: dict[str, dict], runs: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """Overlay persistent names/configurations onto the Arena's shared list."""
    by_id = {entry["id"]: entry for entry in public}
    for row in _rows():
        definition, identity = row["definition"], row["id"]
        if identity in internal:
            internal[identity]["name"] = row["name"]
            by_id[identity]["name"] = row["name"]
            if definition.get("strategy_spec"):
                internal[identity]["strategy_spec"] = deepcopy(definition["strategy_spec"])
            internal[identity]["created_at"] = row["created_at"]
            if row["kind"] == "imported_predictions":
                internal[identity]["source_run_ids"] = [run["id"] for run in runs if run_model_id(run) == identity]
            continue
        kind = row["kind"]
        if kind in {"pronoia", "external_model"}:
            # A deleted connection stays listed but cannot be impersonated by
            # silently falling back to a different platform connection.
            entry = {"id": identity, "name": row["name"], "kind": kind, "available": False,
                     "reason": "模型连接已删除，请重新添加连接", "description": "已保存模型", "supported_target_kinds": ["event", "asset"]}
            frozen = {**entry, **definition}
        else:
            spec = deepcopy(definition["strategy_spec"])
            entry = {"id": identity, "name": row["name"], "kind": kind, "available": kind != "imported_predictions",
                     "reason": "请先发起测试并导入该模型的预测文件" if kind == "imported_predictions" else None,
                     "description": "保存的模型配置，可在不同数据与时间段重复测试",
                     "supported_target_kinds": ["event"] if kind == "external_service" else ["event", "asset"]}
            frozen = {**entry, "strategy_spec": spec, "execution_mode": "saved_predictions" if kind == "imported_predictions" else "quant" if row["category"] == "quant" else "external_service"}
        matching = [run for run in runs if run_model_id(run) == identity]
        if matching:
            frozen["source_run_id"] = matching[0]["id"]
            frozen["source_run_ids"] = [run["id"] for run in matching]
        frozen["created_at"] = row["created_at"]
        public.append(entry)
        internal[identity] = frozen
        by_id[identity] = entry
    # Connections and historical runs remain intact, so their automatically
    # derived entries must respect removal as well as explicitly saved models.
    deleted = _deleted_model_ids()
    return ([entry for entry in public if entry["id"] not in deleted],
            {identity: model for identity, model in internal.items() if identity not in deleted})


def _models() -> tuple[list[dict], dict[str, dict], list[dict]]:
    from .event_backtest.arena_workspace_catalog import _runs, _saved_models
    runs = _runs()
    public, internal = _saved_models(runs)
    return public, internal, runs


def _setup(model: dict) -> dict:
    kind, identity = model["kind"], model["id"]
    category = "quant" if kind == "quant" or _dict(model.get("strategy_spec")).get("type") in {"quant", "api"} else "event"
    result = {"saved_model_id": identity, "category": category, "kind": kind}
    if kind in {"pronoia", "external_model"}:
        profile_id = "__platform_default__" if identity == "pronoia:platform" else identity.split(":", 1)[1]
        runner = "team_full" if kind == "pronoia" else "raw_model"
        result.update(profile_id=profile_id, runner=runner,
            strategy_spec={"type": "event", "adapter": "existing_platform" if kind == "pronoia" else "raw_model", "runner": runner, "parameters": {}, "version": "1"})
    else:
        spec = deepcopy(model["strategy_spec"])
        runner = str(spec.get("runner") or ("event_external_http" if kind == "external_service" else spec.get("kind") or "provided_analysis"))
        result.update(runner=runner, strategy_spec=spec)
    return result


def list_models(category: str | None = None) -> dict:
    if category not in {None, "event", "quant"}:
        raise ValueError("模型类别只能是 event 或 quant")
    public, internal, runs = _models()
    registry = {row["id"]: row for row in _rows()}
    batch_ids = _model_batches()
    items = []
    for model in public:
        frozen = internal[model["id"]]
        setup = _setup(frozen)
        if category and setup["category"] != category:
            continue
        matched = [run for run in runs if run_model_id(run) == model["id"]]
        latest = matched[0] if matched else None
        row = registry.get(model["id"])
        entry = {**model, "category": setup["category"], "can_test": bool(model["available"] or model["kind"] == "imported_predictions"),
            "run_count": len(matched), "run_ids": [run["id"] for run in matched],
            "batch_ids": list(dict.fromkeys([*batch_ids.get(model["id"], []), *(str(_dict(run.get("config")).get("model_lab_batch_id")) for run in matched if _dict(run.get("config")).get("model_lab_batch_id"))])),
            "latest_run": {key: latest.get(key) for key in ("id", "name", "status", "created_at")} if latest else None,
            "created_at": row["created_at"] if row else min((str(run.get("created_at") or "") for run in matched), default=None),
            "setup": _safe(setup)}
        items.append(_safe(entry))
    return {"items": items}


def _model_batches() -> dict[str, list[str]]:
    try:
        with db._lock:
            tasks = db._get_conn().execute("SELECT batch_id,profile_id,kind,config_json FROM ml_tasks ORDER BY created_at DESC").fetchall()
    except sqlite3.OperationalError:
        return {}
    result: dict[str, list[str]] = {}
    for row in tasks:
        try:
            cfg = _dict(json.loads(row["config_json"] or "{}"))
        except (TypeError, ValueError):
            cfg = {}
        profile_id = str(row["profile_id"] or "")
        if not profile_id:
            continue
        variants = cfg.get("variants") or ["pronoia"]
        if row["kind"] != "qa":
            variants = ["pronoia"]
        for variant in variants:
            identity = ("raw:" if variant == "raw" else "pronoia:") + profile_id
            if variant != "raw" and (profile_id == "__platform_default__" or profile_id.startswith("__platform_env__:")):
                identity = "pronoia:platform"
            result.setdefault(identity, []).append(row["batch_id"])
    return result


def get_model(model_id: str) -> dict:
    item = next((item for item in list_models()["items"] if item["id"] == model_id), None)
    if item is None:
        raise KeyError("模型不存在")
    return item


def execution_setup(model_id: str) -> dict:
    """Trusted setup for creating a test; contains refs only on the server."""
    _, models, _ = _models()
    model = models.get(model_id)
    if model is None:
        raise KeyError("模型不存在，请刷新模型列表")
    if not model["available"] and model["kind"] != "imported_predictions":
        raise ValueError(str(model.get("reason") or "模型尚未配置完成"))
    return _setup(model)


def public_setup(model_id: str) -> dict:
    return _safe(execution_setup(model_id))


def delete_models(model_ids: list[str]) -> dict:
    """Remove catalog entries without changing models used by existing runs."""
    if not isinstance(model_ids, list) or not 1 <= len(model_ids) <= 100:
        raise ValueError("每次请选择 1–100 个模型")
    if any(not isinstance(identity, str) or not identity.strip() or len(identity) > 200 for identity in model_ids):
        raise ValueError("模型标识无效，请刷新模型列表")
    identities = list(dict.fromkeys(model_ids))
    with db._lock:
        _, current, _ = _models()
        known = set(current) | _deleted_model_ids()
        if any(identity not in known for identity in identities):
            raise KeyError("模型不存在，请刷新模型列表")
        connection = db._get_conn()
        with connection:
            connection.executemany(
                "INSERT INTO bt_model_tombstones(model_id,deleted_at) VALUES(?,?) ON CONFLICT(model_id) DO NOTHING",
                [(identity, db.now_iso()) for identity in identities],
            )
    return {"deleted_ids": identities}


def _prepare_model(payload: dict) -> tuple[str, str, str, str, dict]:
    from .event_backtest.strategy_registry import normalize_strategy_spec
    from .quant_backtest.strategies import strategy_from_spec
    name, category = _name(payload.get("name")), payload.get("category")
    if category not in {"event", "quant"}:
        raise ValueError("请选择事件或量化模型")
    kind, profile_id = payload.get("kind"), payload.get("profile_id")
    if profile_id or kind in {"pronoia", "external_model"}:
        if category != "event":
            raise ValueError("聊天模型和 Pronoia 请保存到事件模型列表")
        kind = kind or ("pronoia" if payload.get("runner") == "team_full" else "external_model")
        if kind not in {"pronoia", "external_model"}:
            raise ValueError("模型连接仅用于 Pronoia 或独立大模型")
        if kind == "pronoia" and profile_id in {None, "", "__platform_default__"}:
            identity = "pronoia:platform"
        else:
            if not profile_id or profile_id == "__platform_default__":
                raise ValueError("独立大模型需要选择已保存的连接")
            identity = ("pronoia:" if kind == "pronoia" else "raw:") + str(profile_id)
        # A removed catalog entry can be added again. Validate the underlying
        # connection itself rather than the filtered catalog or a stale saved
        # definition whose connection may have been deleted independently.
        if identity != "pronoia:platform":
            from .model_lab import repository as profiles
            from .model_lab.platform_default import PLATFORM_PROFILE_PREFIX
            if str(profile_id).startswith(PLATFORM_PROFILE_PREFIX) or profiles.get_profile(str(profile_id), public=False) is None:
                raise ValueError("所选模型连接不存在")
        definition = {"profile_id": profile_id, "kind": kind}
    else:
        spec = normalize_strategy_spec(payload.get("strategy_spec"), legacy_runner=payload.get("runner"), legacy_strategy_type=category)
        from pydantic import TypeAdapter
        from .schemas import StrategySpec
        TypeAdapter(StrategySpec).validate_python(spec)
        if (spec["type"] == "event") != (category == "event"):
            raise ValueError("模型配置与所选类别不一致")
        if spec.get("type") == "event" and spec.get("adapter") not in {"external_http", "imported_decisions"}:
            raise ValueError("请选择 Pronoia、已保存 API 连接、第三方服务或导入预测")
        if spec.get("type") == "event" and spec.get("adapter") == "external_http":
            timeout = spec.get("timeout_seconds", 30)
            try:
                valid_timeout = not isinstance(timeout, bool) and 0 < float(timeout) <= 300
            except (ValueError, TypeError, OverflowError):
                valid_timeout = False
            if not valid_timeout:
                raise ValueError("预测服务超时时间必须大于 0 且不超过 300 秒")
        if category == "quant":
            try:
                strategy_from_spec(spec)  # validation only, never generate/network
            except (TypeError, OverflowError) as exc:
                raise ValueError("量化模型参数无效，请填写完整且有效的数值") from exc
            _omit_default_confirmations(spec)
        kind = "imported_predictions" if spec.get("adapter") in {"signal_file", "imported_decisions"} or spec.get("source") == "signal_file" else "quant" if category == "quant" else "external_service"
        if kind == "imported_predictions":
            explicit = str(payload.get("model_identity") or uuid.uuid4().hex)
            if not re.fullmatch(r"[a-f0-9]{32}", explicit):
                raise ValueError("导入模型身份必须是 32 位十六进制标识")
            identity = kind + ":" + explicit
        else:
            identity = _semantic_id(kind, spec)
        # Persist model settings alone. A forecast window is supplied by the
        # test; strategy_from_spec supplies its ordinary default when omitted.
        spec = {key: value for key, value in spec.items() if key not in _TEST_FIELDS or key in {"engine_mode", "result_nature", "input_contract", "prediction_only", "point_in_time_enforced", "forecast_contract", "contract_version"}}
        spec["parameters"] = {key: value for key, value in _dict(spec.get("parameters")).items() if key not in _TEST_FIELDS}
        definition = {"strategy_spec": spec, "kind": kind}
    return identity, name, category, kind, definition


def match_model(payload: dict) -> dict:
    """Read-only identity check for the add form; never saves or invokes a model."""
    identity, _, _, _, _ = _prepare_model(payload)
    with db._lock:
        row = db._get_conn().execute("SELECT id FROM bt_saved_models WHERE identity_key=?", (identity,)).fetchone()
        model_id = row["id"] if row else identity
        # An edited model may retain the original semantic hash as its ID.
        if row is None and db._get_conn().execute("SELECT id FROM bt_saved_models WHERE id=?", (model_id,)).fetchone():
            return {"model": None}
        try:
            return {"model": get_model(model_id)}
        except KeyError:
            return {"model": None}


def save_model(payload: dict) -> dict:
    identity, name, category, kind, definition = _prepare_model(payload)
    with db._lock:
        connection = db._get_conn()
        previous = connection.execute("SELECT id,name FROM bt_saved_models WHERE identity_key=?", (identity,)).fetchone()
        model_id = previous["id"] if previous else identity
        if previous is None and connection.execute("SELECT id FROM bt_saved_models WHERE id=?", (model_id,)).fetchone():
            # The original semantic hash may now be the stable ID of an edited
            # model. Saving the old algorithm again creates a distinct model.
            model_id = kind + ":" + uuid.uuid4().hex
        restoring = model_id in _deleted_model_ids()
        prior_name = previous["name"] if previous else None
        if previous is None and not restoring:
            try:
                prior_name = get_model(model_id)["name"]
            except KeyError:
                pass  # A genuinely new configuration has no catalog entry yet.
        disposition = "restored" if restoring else "created" if prior_name is None else "existing" if prior_name == name else "renamed"
        with connection:
            timestamp = db.now_iso()
            if previous is None:
                connection.execute("INSERT INTO bt_saved_models(id,identity_key,name,category,kind,definition_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (model_id, identity, name, category, kind, json.dumps(definition, ensure_ascii=False, allow_nan=False), timestamp, timestamp))
            elif restoring:
                connection.execute("UPDATE bt_saved_models SET name=?,category=?,kind=?,definition_json=?,updated_at=? WHERE id=?",
                    (name, category, kind, json.dumps(definition, ensure_ascii=False, allow_nan=False), timestamp, model_id))
            elif prior_name != name:
                # The add form reuses an identical model. Only its display name
                # changes; stored definitions and historical test snapshots stay intact.
                connection.execute("UPDATE bt_saved_models SET name=?,updated_at=? WHERE id=?", (name, timestamp, model_id))
            connection.execute("DELETE FROM bt_model_tombstones WHERE model_id=?", (model_id,))
    return {**get_model(model_id), "save_disposition": disposition}


def update_configuration(model_id: str, payload: dict) -> dict:
    """Edit future executions while preserving the identity of existing tests."""
    name = _name(payload.get("name"))
    with db._lock:
        _, models, runs = _models()
        model = models.get(model_id)
        if model is None:
            raise KeyError("模型不存在，请刷新模型列表")
        setup = _setup(model)
        supplied_spec = payload.get("strategy_spec")
        if model["kind"] in {"pronoia", "external_model"}:
            if supplied_spec is not None:
                raise ValueError("API 模型请在模型连接中修改配置；平台默认模型请使用统一基模设置")
            # Renaming a retained unavailable connection is still meaningful.
            identity = model_id
            definition = {"kind": model["kind"], "profile_id": setup["profile_id"]}
        else:
            old_spec = deepcopy(model["strategy_spec"])
            from .event_backtest.strategy_registry import normalize_strategy_spec
            old_kind = normalize_strategy_spec(old_spec, legacy_runner=setup["runner"], legacy_strategy_type=setup["category"])
            proposed = deepcopy(supplied_spec if supplied_spec is not None else old_spec)
            if not isinstance(proposed, dict):
                raise ValueError("模型配置必须是对象")
            if "headers" not in proposed:
                if old_spec.get("headers") and proposed.get("endpoint") != old_spec.get("endpoint"):
                    raise ValueError("修改预测服务地址后，请重新设置 API 认证，或明确选择无需认证")
                if "headers" in old_spec:
                    proposed["headers"] = deepcopy(old_spec["headers"])
            identity, _, category, kind, definition = _prepare_model({
                "name": name, "category": setup["category"], "runner": setup["runner"], "strategy_spec": proposed,
            })
            if category != setup["category"] or kind != model["kind"] or any(
                definition["strategy_spec"].get(key) != old_kind.get(key) for key in ("type", "adapter", "kind", "source")
            ):
                raise ValueError("请保持当前模型类型；更换模型类型请添加新模型")
            if kind == "imported_predictions":
                row = next((row for row in _rows() if row["id"] == model_id), None)
                identity = row["identity_key"] if row else model_id
        connection = db._get_conn()
        duplicate = connection.execute("SELECT id FROM bt_saved_models WHERE identity_key=? AND id!=?", (identity, model_id)).fetchone()
        if duplicate:
            raise ValueError("已有相同配置的模型，请使用该模型，或调整参数后保存")
        # A run-derived model that has not been persisted yet also owns its
        # current configuration. Do not merge it into a different card by edit.
        if model["kind"] not in {"pronoia", "external_model", "imported_predictions"}:
            for other_id, other in models.items():
                if other_id != model_id and other.get("kind") == model["kind"] and _semantic_id(other["kind"], _dict(other.get("strategy_spec"))) == identity:
                    raise ValueError("已有相同配置的模型，请使用该模型，或调整参数后保存")
        matching = [run for run in runs if run_model_id(run) == model_id and not _dict(run.get("config")).get("saved_model_id")]
        with connection:
            timestamp = db.now_iso()
            connection.execute(
                "INSERT INTO bt_saved_models(id,identity_key,name,category,kind,definition_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET identity_key=excluded.identity_key,name=excluded.name,category=excluded.category,kind=excluded.kind,definition_json=excluded.definition_json,updated_at=excluded.updated_at",
                (model_id, identity, name, setup["category"], model["kind"], json.dumps(definition, ensure_ascii=False, allow_nan=False), timestamp, timestamp),
            )
            for run in matching:
                # Only add the identity link. Existing strategy/profile
                # snapshots, execution configuration and results stay frozen.
                row = connection.execute("SELECT config_json FROM bt_runs WHERE id=?", (run["id"],)).fetchone()
                if row is None:
                    continue
                config = _dict(json.loads(row["config_json"] or "{}"))
                config.setdefault("saved_model_id", model_id)
                connection.execute("UPDATE bt_runs SET config_json=? WHERE id=?", (json.dumps(config, ensure_ascii=False, allow_nan=False), run["id"]))
    return get_model(model_id)


def rename_model(model_id: str, name: str) -> dict:
    name = _name(name)
    entry = get_model(model_id)
    if not any(row["id"] == model_id for row in _rows()):
        setup = execution_setup(model_id)
        if setup["kind"] == "imported_predictions":
            now = db.now_iso()
            with db._lock:
                db._get_conn().execute("INSERT INTO bt_saved_models(id,identity_key,name,category,kind,definition_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (model_id, model_id, entry["name"], setup["category"], setup["kind"], json.dumps({"kind": setup["kind"], "strategy_spec": setup["strategy_spec"]}), now, now))
                db._get_conn().commit()
        else:
            save_model({**setup, "name": entry["name"]})
    with db._lock:
        db._get_conn().execute("UPDATE bt_saved_models SET name=?,updated_at=? WHERE id=?", (name, db.now_iso(), model_id))
        db._get_conn().commit()
    return get_model(model_id)


def bind_test_request(request):
    """Rebuild the model side from storage; caller retains test data/window."""
    identity = _dict(request.config).get("saved_model_id")
    if not identity:
        return request
    from .schemas import CreateBacktestRunRequest
    from .event_backtest.arena_workspace_catalog import resolve_model
    setup = execution_setup(str(identity))
    payload = request.model_dump()
    spec = deepcopy(setup["strategy_spec"])
    if setup["kind"] == "imported_predictions" and setup["category"] == "quant":
        proposed_path = _dict(payload.get("strategy_spec")).get("path")
        if proposed_path is not None:
            spec["path"] = proposed_path
    if spec.get("kind") == "return_forecast":
        proposed = _dict(_dict(payload.get("strategy_spec")).get("parameters")).get("horizon_bars")
        horizon = proposed or _dict(payload.get("execution_spec")).get("holding_bars")
        if horizon is not None:
            spec.setdefault("parameters", {})["horizon_bars"] = horizon
    payload.update(strategy_spec=spec, runner=setup["runner"], strategy_type=setup["category"])
    payload["config"]["saved_model_id"] = str(identity)
    if setup["kind"] in {"pronoia", "external_model"}:
        frozen = resolve_model(str(identity))
        snapshot = frozen["profile_snapshot"]
        payload["config"].update(model_profile_snapshot=snapshot, model_profile_id=snapshot["id"])
        payload["model_version"] = snapshot["model_id"]
    return CreateBacktestRunRequest.model_validate(payload)
