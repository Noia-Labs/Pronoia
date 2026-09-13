"""Versioned data-benchmark contracts for the unified backtest API.

This module only inspects already supplied files.  It deliberately has no
network fetch path: a configured API source without a materialized snapshot is
``pending``/``unavailable`` and cannot be used by a Run.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .protocol import events_snapshot_sha256, file_sha256


DATASET_CONTRACT_VERSION = "pronoia-market-dataset-v1"
_DAILY = {"1d", "d", "day", "daily", "1w", "w", "weekly", "1mo", "monthly"}
_MINUTE = {"1m", "5m", "15m", "30m", "60m", "1min", "5min", "15min", "30min", "60min"}


_SYNTHETIC_MARKERS = (
    "synth-v7",
    "synthetic://",
    "generated-demo",
)
_PLACEHOLDER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bconsensus\s*[:：]\s*(?:survey|poll|market|bloomberg|sell[- ]?side|street|prior|tbd|n/?a)\b",
        r"\bprev(?:ious)?\s*[:：]\s*(?:prior|last\s*q|recent|previous|tbd|n/?a)\b",
        r"预期\s*[:：]\s*(?:一致|机构|市场|调查|预期|待定|未知)(?:\s|$)",
        r"前值\s*[:：]\s*(?:上期|上月|历史|前值|待定|未知)(?:\s|$)",
        r"\b(?:placeholder|sample[-_ ]?only|demo[-_ ]?only)\b",
        r"(?:占位|示例数据|演示数据)",
    )
)
_SEMANTIC_SCAN_VERSION = "event-semantic-quality-v2"


def _event_text_is_metadata_only(title: str, event_text: str) -> bool:
    """Recognize known seed formats, not text length or language quality.

    Collectors historically stored either the title itself, title plus a CN
    security/type snippet, or SEC filing type plus title. Short factual
    summaries with actual content must remain usable.
    """
    title = re.sub(r"\s+", " ", title).strip()
    text = re.sub(r"\s+", " ", event_text).strip()
    if not text:
        return False
    if title and text == title:
        return True
    parts = [part.strip() for part in re.split(r"\s*[|｜]\s*", text) if part.strip()]
    filing_type = r"(?:8-K|10-K|10-Q|20-F|6-K|40-F|S-1|S-3|S-4|F-1|F-4|425|DEFA14A|DEF14A)(?:/A)?"
    if title and len(parts) == 2 and parts[1] == title and re.fullmatch(filing_type, parts[0], re.I):
        return True
    # This is the exact metadata-only shape of get_announcements().snippet:
    # code + security name + separator + filing category, with no statement.
    security_metadata = (
        r"(?:SH|SZ|BJ)?\d{6}(?:\.(?:SH|SZ|BJ))?\s+"
        r"[^|｜·;；。\n]{1,40}\s*·\s*"
        r"(?:公告|定期报告|临时公告|重大事项|业绩(?:报告|预告|快报)|"
        r"(?:发行|融资|发行融资|再融资|担保|董事会|监事会|股东大会|权益分派|"
        r"股权变动|股本变动|股权激励|资产重组|并购重组|收购兼并)(?:公告)?|"
        r"年度报告|半年度报告|季度报告|其他(?:公告)?)"
    )
    if re.fullmatch(security_metadata, text, re.I):
        return True
    return bool(
        title and len(parts) == 2 and parts[0] == title
        and re.fullmatch(security_metadata, parts[1], re.I)
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def normalize_frequency(value: Any) -> str | None:
    raw = str(value or "").strip().lower().replace("minute", "min")
    if not raw:
        return None
    aliases = {"d": "1d", "day": "1d", "daily": "1d", "1min": "1m", "5min": "5m",
               "15min": "15m", "30min": "30m", "60min": "60m"}
    return aliases.get(raw, raw)


def _event_rows(source: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            if not isinstance(row, dict):
                raise ValueError(f"事件文件第 {line_number} 行不是 JSON object")
            rows.append(row)
    return rows


def inspect_event_semantic_quality(
    path: str | Path,
    *,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect whether an event snapshot contains usable historical facts.

    ``quality_status`` elsewhere in the dataset contract answers a different
    question: whether the file is structurally readable.  This report never
    promotes a structurally valid file to research quality merely because it
    parsed successfully.
    """
    event_path = Path(path).expanduser().resolve()
    rows = _event_rows(event_path)
    total = len(rows)
    if total <= 0:
        raise ValueError("事件数据集为空")

    source_contract = dict(source or {})
    source_metadata = source_contract.get("metadata")
    source_text = " ".join(
        str(value or "")
        for value in (
            source_contract.get("provider"),
            source_contract.get("ref"),
            json.dumps(source_metadata, ensure_ascii=False, default=str)
            if isinstance(source_metadata, Mapping) else source_metadata,
        )
    ).lower()
    contract_is_synthetic = any(marker in source_text for marker in _SYNTHETIC_MARKERS)

    synthetic_count = 0
    placeholder_count = 0
    metadata_only_count = 0
    fact_complete_count = 0
    weekend_count = 0
    untraceable_source_count = 0
    for row in rows:
        joined = "\n".join(
            str(row.get(key) or "")
            for key in ("event_id", "title", "event_text", "text", "source_url", "url")
        )
        lowered = joined.lower()
        is_synthetic = contract_is_synthetic or any(marker in lowered for marker in _SYNTHETIC_MARKERS)
        is_placeholder = any(pattern.search(joined) for pattern in _PLACEHOLDER_PATTERNS)
        synthetic_count += int(is_synthetic)
        placeholder_count += int(is_placeholder)

        decision_time = str(
            row.get("available_time")
            or row.get("available_at")
            or row.get("event_time")
            or row.get("occurred_at")
            or ""
        ).strip()
        event_type = str(row.get("event_type_l2") or row.get("event_type") or "").strip()
        event_text = str(row.get("event_text") or row.get("text") or "").strip()
        metadata_only = _event_text_is_metadata_only(str(row.get("title") or ""), event_text)
        metadata_only_count += int(metadata_only)
        source_url = str(row.get("source_url") or row.get("url") or "").strip()
        traceable_source = bool(
            source_url
            and not source_url.lower().startswith(("manual://", "generated://"))
        )
        if not traceable_source:
            untraceable_source_count += 1
        required_facts_present = all((
            str(row.get("event_id") or row.get("id") or "").strip(),
            str(row.get("market") or "").strip(),
            str(row.get("symbol") or "").strip(),
            decision_time,
            event_type,
            str(row.get("title") or "").strip(),
            event_text,
            traceable_source,
        ))
        # Placeholder and generated records can be schema-complete while still
        # lacking the actual facts a model would need.  Keep that distinction
        # visible in ``fact_complete_ratio``.
        if required_facts_present and not is_synthetic and not is_placeholder and not metadata_only:
            fact_complete_count += 1

        if decision_time:
            try:
                if date.fromisoformat(decision_time[:10]).weekday() >= 5:
                    weekend_count += 1
            except ValueError:
                # Structural validation owns invalid timestamps.  Semantic
                # quality simply does not count an unparseable value as weekend.
                pass

    synthetic_ratio = synthetic_count / total
    placeholder_ratio = placeholder_count / total
    fact_complete_ratio = fact_complete_count / total
    weekend_ratio = weekend_count / total
    reason_codes: list[str] = []
    reasons: list[str] = []
    if synthetic_count:
        reason_codes.append("contains_synthetic_events")
        reasons.append(f"检测到 {synthetic_count}/{total} 条 synth-v7 或其他合成事件")
    if placeholder_count:
        reason_codes.append("contains_placeholder_events")
        reasons.append(f"检测到 {placeholder_count}/{total} 条占位事实（如泛化的预期/前值）")
    if metadata_only_count:
        reason_codes.append("metadata_only_event_text")
        reasons.append(f"有 {metadata_only_count}/{total} 条正文仅复制标题或包含证券/文件类型元信息，需补充原始公告事实")
    if fact_complete_count < total:
        reason_codes.append("incomplete_event_facts")
        reasons.append(f"仅 {fact_complete_count}/{total} 条具备可追溯且非占位的完整事件事实")
    if untraceable_source_count:
        reason_codes.append("missing_traceable_source")
        reasons.append(
            f"有 {untraceable_source_count}/{total} 条仅有手工占位来源或缺少可追溯出处"
        )
    if weekend_count:
        reason_codes.append("weekend_event_dates_present")
        reasons.append(f"有 {weekend_count}/{total} 条信息可得日期落在周末，建议复核时点映射")

    if synthetic_count or placeholder_count:
        status = "demo_only"
    elif fact_complete_ratio >= 0.95:
        status = "research_ready"
    else:
        status = "partial"
    if not reasons:
        reasons.append("事件事实、来源与信息时点满足当前研究质量基线")

    return {
        "scan_version": _SEMANTIC_SCAN_VERSION,
        "row_count": total,
        "synthetic_ratio": round(synthetic_ratio, 6),
        "placeholder_ratio": round(placeholder_ratio, 6),
        "metadata_only_ratio": round(metadata_only_count / total, 6),
        "fact_complete_ratio": round(fact_complete_ratio, 6),
        "weekend_ratio": round(weekend_ratio, 6),
        "traceable_source_ratio": round((total - untraceable_source_count) / total, 6),
        "status": status,
        "reason_codes": reason_codes,
        "reasons": reasons,
        # Partial data is still allowed in a formal comparison when it is real
        # and both Runs share the exact frozen snapshot.  Known demo data is not.
        "formal_evaluation_eligible": status != "demo_only",
    }


def with_semantic_quality(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return a read-only enriched event contract without mutating source data."""
    enriched = dict(contract)
    if str(enriched.get("dataset_kind") or "event").strip().lower() != "event":
        return enriched
    report = dict(enriched.get("quality_report") or {})
    semantic = enriched.get("semantic_quality")
    if not isinstance(semantic, Mapping):
        semantic = report.get("semantic_quality")
    path = Path(str(enriched.get("path") or "")).expanduser()
    scan_is_current = (
        isinstance(semantic, Mapping)
        and semantic.get("scan_version") == _SEMANTIC_SCAN_VERSION
    )
    if not scan_is_current and path.is_file():
        semantic = inspect_event_semantic_quality(path, source=enriched.get("source"))
    if isinstance(semantic, Mapping):
        semantic = dict(semantic)
        enriched["semantic_quality"] = semantic
        report["semantic_quality"] = semantic
        enriched["quality_report"] = report
    return enriched


def default_capabilities(*, dataset_kind: str, frequency: str | None) -> dict[str, Any]:
    normalized = normalize_frequency(frequency)
    is_daily = normalized in _DAILY
    is_minute = normalized in _MINUTE or bool(normalized and normalized.endswith("m") and normalized[:-1].isdigit())
    if dataset_kind == "event":
        return {
            "daily": False,
            "minute": False,
            "ohlc": False,
            "volume": False,
            "point_in_time": True,
            "event_catalog": True,
            "portfolio_execution": False,
            "supported_frequencies": [],
        }
    return {
        "daily": is_daily,
        "minute": is_minute,
        "ohlc": True,
        "volume": True,
        "point_in_time": True,
        "event_catalog": False,
        "portfolio_execution": True,
        "supported_frequencies": [normalized] if normalized else [],
    }


def _mapped_market_rows(
    source: Path,
    schema_mapping: Mapping[str, str] | None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("行情文件为空")
    requested = {str(k).strip(): str(v).strip() for k, v in dict(schema_mapping or {}).items() if str(v).strip()}
    # Public contract uses canonical -> source-column.  Accept the reverse form
    # as a convenience when every key is a real source header.
    canonical = {"timestamp", "open", "high", "low", "close", "volume", "symbol"}
    headers = {str(key) for key in rows[0]}
    if requested and set(requested).issubset(headers) and set(requested.values()) & canonical:
        requested = {target: source_name for source_name, target in requested.items()}
    if requested:
        missing_columns = [column for column in requested.values() if column not in headers]
        if missing_columns:
            raise ValueError("schema_mapping 引用了不存在的列: " + ", ".join(sorted(missing_columns)))
        required = [key for key in ("timestamp", "open", "high", "low", "close") if key not in requested]
        if required:
            raise ValueError("schema_mapping 缺少必要字段: " + ", ".join(required))
        normalized = [
            {**row, **{key: row.get(column) for key, column in requested.items()}}
            for row in rows
        ]
        effective = requested
    else:
        normalized = rows
        effective = {}
    return normalized, effective


def inspect_dataset_file(
    *,
    path: str | Path,
    dataset_kind: str,
    frequency: str | None = None,
    schema_mapping: Mapping[str, str] | None = None,
    declared_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Validate a local immutable snapshot and return auditable coverage."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"数据文件不存在: {source}")
    kind = str(dataset_kind or "event").strip().lower()
    if kind == "event":
        from .application import load_events

        events = load_events(source)
        if not events:
            raise ValueError("事件数据集为空")
        semantic_quality = inspect_event_semantic_quality(source)
        symbols = sorted({str(event.symbol) for event in events if str(event.symbol or "").strip()})
        markets = sorted({str(event.market).upper() for event in events if str(event.market or "").strip()})
        times = sorted(str(event.event_time) for event in events if str(event.event_time or "").strip())
        available = sum(1 for event in events if getattr(event, "available_time", None))
        occurred = sum(1 for event in events if getattr(event, "occurred_at", None))
        return {
            "path": str(source),
            "snapshot_hash": events_snapshot_sha256(source),
            "row_count": len(events),
            "markets": markets,
            "symbols": symbols,
            "coverage": {
                "start_at": times[0] if times else None,
                "end_at": times[-1] if times else None,
                "symbols": symbols,
                "row_count": len(events),
                "available_time_count": available,
                "occurred_at_count": occurred,
            },
            "quality_status": "passed",
            "quality_report": {
                "validated_records": len(events),
                "point_in_time_audit_complete": available == len(events) and occurred == len(events),
                "semantic_quality": semantic_quality,
            },
            "semantic_quality": semantic_quality,
            "schema_mapping": dict(schema_mapping or {}),
        }

    if source.suffix.lower() not in {".csv", ".txt"}:
        raise ValueError("market 数据 MVP 仅导入本地 CSV；API 数据必须先冻结为文件")
    from ..quant_backtest.data import rows_to_bars

    rows, effective_mapping = _mapped_market_rows(source, schema_mapping)
    symbol_column = effective_mapping.get("symbol") if effective_mapping else None
    if not symbol_column:
        headers = {str(key).strip().lower(): str(key) for key in rows[0]}
        symbol_column = next(
            (headers[key] for key in ("symbol", "ticker", "code", "标的", "代码") if key in headers),
            None,
        )
    symbols = sorted({str(row.get(symbol_column) or "").strip() for row in rows}) if symbol_column else []
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
        symbols = sorted({str(symbol).strip() for symbol in (declared_symbols or []) if str(symbol).strip()})
    groups: dict[str, list[dict[str, Any]]] = {}
    if symbol_column and symbols:
        for row in rows:
            symbol = str(row.get(symbol_column) or "").strip()
            if symbol:
                groups.setdefault(symbol, []).append(row)
    else:
        groups["__single_series__"] = rows
    per_symbol: dict[str, dict[str, Any]] = {}
    total_bars = total_duplicates = 0
    all_starts: list[str] = []
    all_ends: list[str] = []
    for symbol, group_rows in sorted(groups.items()):
        bars, audit = rows_to_bars(group_rows, repair_ohlc=False)
        total_bars += len(bars)
        total_duplicates += int(audit.get("duplicate_timestamp_count") or 0)
        all_starts.append(bars[0].timestamp)
        all_ends.append(bars[-1].timestamp)
        per_symbol[symbol] = {
            "row_count": len(bars),
            "start_at": bars[0].timestamp,
            "end_at": bars[-1].timestamp,
            "duplicate_timestamp_count": int(audit.get("duplicate_timestamp_count") or 0),
        }
    return {
        "path": str(source),
        "snapshot_hash": file_sha256(source),
        "row_count": total_bars,
        "markets": [],
        "symbols": symbols,
        "coverage": {
            "start_at": min(all_starts),
            "end_at": max(all_ends),
            "symbols": symbols,
            "row_count": total_bars,
            "bar_count_total": total_bars,
            "per_symbol": per_symbol,
        },
        "quality_status": "passed",
        "quality_report": {
            "source_row_count": len(rows),
            "duplicate_timestamp_count": total_duplicates,
            "runtime_ohlc_repair_count": 0,
            "validated_bars": total_bars,
            "strict_ohlc": True,
            "single_asset_execution_ready": len(symbols) <= 1,
            "multi_asset_execution_ready": False,
        },
        "schema_mapping": effective_mapping or dict(schema_mapping or {}),
    }


def build_dataset_version(
    *,
    dataset_kind: str,
    snapshot_hash: str | None,
    source: Mapping[str, Any] | None,
    markets: list[str] | None,
    asset_type: str | None,
    frequency: str | None,
    adjustment: str | None,
    calendar: str | None,
    symbols: list[str] | None,
    schema_mapping: Mapping[str, Any] | None,
) -> str:
    """Content-address a benchmark without including its mutable display ID."""
    source_contract = dict(source or {})
    # A local filename is provenance, not semantic data identity.  Once bytes
    # are frozen, cloned aliases with identical content must share a version so
    # they can be compared.  For an unmaterialized source, ``ref`` remains the
    # only available identity and is therefore retained.
    if snapshot_hash:
        source_contract.pop("ref", None)
    payload = {
        "contract_version": DATASET_CONTRACT_VERSION,
        "dataset_kind": dataset_kind,
        "snapshot_hash": snapshot_hash,
        "source": source_contract,
        "markets": sorted(str(item).upper() for item in (markets or [])),
        "asset_type": asset_type,
        "frequency": normalize_frequency(frequency),
        "adjustment": adjustment,
        "calendar": calendar,
        "symbols": sorted(str(item) for item in (symbols or [])),
        "schema_mapping": dict(schema_mapping or {}),
    }
    return "dsv_" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:32]


def materialize_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize metadata and inspect a supplied file, without fetching data."""
    result = dict(payload)
    kind = str(result.get("dataset_kind") or "event").strip().lower()
    result["dataset_kind"] = kind
    result["frequency"] = normalize_frequency(result.get("frequency"))
    result["markets"] = sorted({str(item).strip().upper() for item in (result.get("markets") or []) if str(item).strip()})
    result["symbols"] = sorted({str(item).strip() for item in (result.get("symbols") or []) if str(item).strip()})
    source = result.get("source") if isinstance(result.get("source"), Mapping) else {}
    result["source"] = dict(source)
    raw_path = result.get("path") or source.get("ref")
    inspection: dict[str, Any] | None = None
    if raw_path and Path(str(raw_path)).expanduser().is_file():
        inspection = inspect_dataset_file(
            path=str(raw_path),
            dataset_kind=kind,
            frequency=result.get("frequency"),
            schema_mapping=result.get("schema_mapping"),
            declared_symbols=result.get("symbols"),
        )
        result["path"] = inspection["path"]
        result["snapshot_hash"] = inspection["snapshot_hash"]
        result["total_events"] = inspection["row_count"]
        result["markets"] = result["markets"] or inspection["markets"]
        result["symbols"] = result["symbols"] or inspection["symbols"]
        result["coverage"] = {**inspection["coverage"], **dict(result.get("coverage") or {})}
        result["schema_mapping"] = inspection["schema_mapping"]
        result["quality_status"] = result.get("quality_status") or inspection["quality_status"]
        result["quality_report"] = {**inspection["quality_report"], **dict(result.get("quality_report") or {})}
        if kind == "event":
            # Re-run with source provenance included.  The resulting semantic
            # report is scanner-owned and cannot be overridden by declared
            # ``quality_status`` or arbitrary caller metadata.
            semantic_quality = inspect_event_semantic_quality(
                inspection["path"], source=result.get("source"),
            )
            result["semantic_quality"] = semantic_quality
            result["quality_report"]["semantic_quality"] = semantic_quality
        result["status"] = "available"
    else:
        result["path"] = str(raw_path or "")
        result["snapshot_hash"] = None
        requested_status = str(result.get("status") or "pending")
        if requested_status == "available":
            raise ValueError("status=available 必须提供可读取且校验通过的本地快照")
        result["status"] = requested_status if requested_status in {"pending", "unavailable", "invalid"} else "pending"
        result["quality_status"] = result.get("quality_status") or (
            "failed" if result["status"] == "invalid" else "pending"
        )
        result["quality_report"] = {
            "materialized": False,
            "reason": "尚未提供本地冻结快照；注册动作不会在线抓取或生成行情",
            **dict(result.get("quality_report") or {}),
        }
    capabilities = default_capabilities(dataset_kind=kind, frequency=result.get("frequency"))
    capabilities.update(dict(result.get("capabilities") or {}))
    if kind == "market":
        single_asset_ready = bool(
            result.get("quality_report", {}).get("single_asset_execution_ready", len(result["symbols"]) <= 1)
        )
        capabilities["multi_symbol_data"] = len(result["symbols"]) > 1
        capabilities["single_asset_execution_ready"] = single_asset_ready
        capabilities["multi_asset_execution_ready"] = False
        capabilities["portfolio_execution"] = single_asset_ready
    if result["status"] != "available":
        capabilities["portfolio_execution"] = False
    result["capabilities"] = capabilities
    result["dataset_version"] = build_dataset_version(
        dataset_kind=kind,
        snapshot_hash=result.get("snapshot_hash"),
        source=result.get("source"),
        markets=result.get("markets"),
        asset_type=result.get("asset_type"),
        frequency=result.get("frequency"),
        adjustment=result.get("adjustment"),
        calendar=result.get("calendar"),
        symbols=result.get("symbols"),
        schema_mapping=result.get("schema_mapping"),
    )
    return result


def load_market_dataset_snapshot(
    contract: Mapping[str, Any],
    *,
    start: str | None = None,
    end: str | None = None,
):
    """Load one registered single-asset CSV version for the portfolio engine."""
    from ..quant_backtest.data import rows_to_bars, select_bars_for_execution_window
    from ..quant_backtest.models import MarketDataset, QuantBacktestError

    path = Path(str(contract.get("path") or "")).expanduser().resolve()
    if not path.is_file():
        raise QuantBacktestError(f"冻结行情快照不存在: {path}")
    rows, effective_mapping = _mapped_market_rows(path, contract.get("schema_mapping"))
    declared_symbols = [str(item) for item in (contract.get("symbols") or []) if str(item)]
    if len(declared_symbols) > 1:
        raise QuantBacktestError("当前组合执行器只支持单标的；多标数据已保留但不可执行")
    symbol = declared_symbols[0] if declared_symbols else ""
    symbol_column = effective_mapping.get("symbol") if effective_mapping else None
    if not symbol_column:
        headers = {str(key).strip().lower(): str(key) for key in rows[0]}
        symbol_column = next(
            (headers[key] for key in ("symbol", "ticker", "code", "标的", "代码") if key in headers),
            None,
        )
    if symbol_column:
        file_symbols = sorted({str(row.get(symbol_column) or "").strip() for row in rows if str(row.get(symbol_column) or "").strip()})
        if len(file_symbols) > 1:
            raise QuantBacktestError("冻结行情包含多个标的，当前单标执行器拒绝运行")
        if file_symbols:
            if symbol and file_symbols[0] != symbol:
                raise QuantBacktestError("声明 symbols 与行情文件 symbol 不一致")
            symbol = file_symbols[0]
    if not symbol:
        raise QuantBacktestError("单标行情必须在数据契约 symbols 中声明标的")
    markets = [str(item) for item in (contract.get("markets") or []) if str(item)]
    market = markets[0] if len(markets) == 1 else str(contract.get("market") or "")
    bars, audit = rows_to_bars(rows, repair_ohlc=False)
    source = contract.get("source") if isinstance(contract.get("source"), Mapping) else {}
    source_metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
    coverage = contract.get("coverage") if isinstance(contract.get("coverage"), Mapping) else {}
    timezone_name = next(
        (
            str(value).strip()
            for value in (
                contract.get("timezone"),
                contract.get("time_zone"),
                source.get("timezone"),
                source.get("time_zone"),
                source_metadata.get("timezone"),
                source_metadata.get("time_zone"),
                coverage.get("timezone"),
                coverage.get("time_zone"),
            )
            if str(value or "").strip()
        ),
        None,
    )
    bars, window_audit = select_bars_for_execution_window(
        bars,
        start=start,
        end=end,
        market=market,
        timezone_name=timezone_name,
    )
    return MarketDataset(
        name=str(contract.get("name") or path.stem),
        symbol=symbol,
        market=market,
        frequency=str(contract.get("frequency") or "1d"),
        bars=bars,
        source_type=str((contract.get("source") or {}).get("type") or "local_file"),
        source_ref=str(path),
        metadata={
            **audit,
            **window_audit,
            "dataset_version": contract.get("dataset_version") or contract.get("id"),
            "snapshot_hash": contract.get("snapshot_hash"),
            "adjustment": contract.get("adjustment"),
            "calendar": contract.get("calendar"),
            "schema_mapping": dict(contract.get("schema_mapping") or {}),
        },
    )


def verify_materialized_snapshot(contract: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Verify immutable version bytes/facts immediately before a Run executes."""
    path = Path(str(contract.get("path") or "")).expanduser()
    expected = str(contract.get("snapshot_hash") or "")
    if not path.is_file():
        return False, f"冻结快照文件不存在: {path}"
    if not expected:
        return False, "冻结数据版本缺少 snapshot_hash"
    actual = (
        events_snapshot_sha256(path)
        if str(contract.get("dataset_kind") or "event") == "event"
        else file_sha256(path)
    )
    if actual != expected:
        return False, f"冻结快照内容已变化: expected={expected}, actual={actual}"
    return True, None


def verify_run_frozen_inputs(run: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Validate both dataset bytes and the full persisted comparison fingerprint."""
    from .. import db
    from .protocol import recompute_protocol_hash_for_run

    dataset_id = str(run.get("dataset_id") or "")
    dataset_version = str(run.get("dataset_version") or "")
    if dataset_id and dataset_version:
        dataset = db.get_bt_dataset(dataset_id)
        version = db.get_bt_dataset_version(dataset_id, dataset_version)
        if not dataset or not version:
            return False, "Run 引用的数据版本记录不存在"
        contract = dict(dataset)
        for key, value in version.items():
            if key not in {"id", "dataset_id"}:
                contract[key] = value
    else:
        config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
        frozen = config.get("dataset_snapshot") if isinstance(config.get("dataset_snapshot"), Mapping) else {}
        if not frozen:
            # Legacy Runs predate content versions; their protocol hash still
            # protects events/labels and remains executable for compatibility.
            contract = None
        else:
            contract = {
                **dict(frozen),
                "path": run.get("events_path"),
                "dataset_kind": frozen.get("dataset_kind") or "event",
            }
    if contract is not None:
        valid, error = verify_materialized_snapshot(contract)
        if not valid:
            return False, error
    persisted = str(run.get("protocol_hash") or "")
    if persisted:
        recomputed = recompute_protocol_hash_for_run(dict(run))
        if recomputed != persisted:
            return False, f"执行协议或底层快照已变化: expected={persisted}, actual={recomputed}"
    return True, None
