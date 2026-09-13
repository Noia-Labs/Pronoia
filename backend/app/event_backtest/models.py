from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Optional


Market = Literal["CN", "US", "HK", "FUTURES", "CRYPTO", "FX"]
Direction = Literal["up", "down", "neutral"]
Label = Literal["up", "down", "neutral"]
# Oracle horizons used for scoring. Numeric horizons (t*) map 1-to-1 to label_t* / car_t*.
# Composite horizons (avg_* / consensus66) map to label_avg_* / label_consensus66.
Horizon = Literal[
    "t1", "t3", "t5", "t7", "t15", "t30", "t60",
    "avg_short", "avg_mid", "avg_long", "avg_all", "consensus66",
]

ALL_HORIZONS: tuple[Horizon, ...] = (
    "t1", "t3", "t5", "t7", "t15", "t30", "t60",
    "avg_short", "avg_mid", "avg_long", "avg_all", "consensus66",
)

_EVENT_FACT_KEYS = (
    "actual_value", "expected_value", "previous_value", "value_unit",
)
_PRE_EVENT_FEATURE_ALIASES: dict[str, tuple[str, ...]] = {
    "asset_return_5d_pct": (
        "asset_return_5d_pct", "pre5", "pre5_pct", "pre5_return",
        "pre5_return_pct", "pre5_cum_return_pct", "pre_5d_return",
    ),
    "asset_return_20d_pct": (
        "asset_return_20d_pct", "pre20", "pre20_pct", "pre20_return",
        "pre20_return_pct", "pre20_cum_return_pct", "pre_20d_return",
    ),
    "benchmark_return_5d_pct": ("benchmark_return_5d_pct",),
    "benchmark_return_20d_pct": ("benchmark_return_20d_pct",),
    "excess_return_5d_pct": ("excess_return_5d_pct", "pre5_cum_ar_pct"),
    "excess_return_20d_pct": ("excess_return_20d_pct", "pre20_cum_ar_pct"),
}
_LEGACY_RETURN_UNIT_UNKNOWN_KEYS = {
    "pre5", "pre20", "pre5_return", "pre20_return",
    "pre_5d_return", "pre_20d_return",
}


def finite_expected_return_pct(value: Any) -> Optional[float]:
    """Only an explicit finite numeric forecast, in percentage points."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def return_forecast_contract(horizon: str) -> dict[str, str]:
    return {
        "unit": "percent", "basis": "asset_return", "horizon": str(horizon),
        "anchor": "event_close_post_close_next_close",
    }


def _bounded_scalar(value: Any, *, numeric: bool = False) -> Any:
    """Return a JSON-safe scalar or None; nested payloads are never exposed."""
    if isinstance(value, bool) or value is None:
        return None
    if numeric:
        try:
            if isinstance(value, str):
                value = value.strip().removesuffix("%").strip()
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, str):
        stripped = value.strip()
        return stripped[:500] if stripped else None
    return None


def sanitize_event_facts(value: Any) -> dict[str, Any]:
    """Allow only non-nested, explicitly as-of event fact fields."""
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in _EVENT_FACT_KEYS:
        # The three measurement fields are deliberately numeric.  Allowing
        # arbitrary prose in (for example) ``actual_value`` would create a
        # second, weakly validated event-text channel and make the strict
        # as-of contract much harder to audit.  The unit is the only text
        # field and is bounded separately below.
        safe = _bounded_scalar(source.get(key), numeric=key != "value_unit")
        if safe is not None:
            result[key] = safe[:80] if key == "value_unit" else safe
    return result


def sanitize_pre_event_features(value: Any) -> dict[str, Any]:
    """Normalize the six prior-close return fields; reject all future keys."""
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    legacy_units_unknown = False
    for canonical, aliases in _PRE_EVENT_FEATURE_ALIASES.items():
        for alias in aliases:
            safe = _bounded_scalar(source.get(alias), numeric=True)
            if safe is not None:
                result[canonical] = safe
                legacy_units_unknown = legacy_units_unknown or alias in _LEGACY_RETURN_UNIT_UNKNOWN_KEYS
                break
    if legacy_units_unknown:
        # The value remains unchanged.  Old ``pre5/pre20`` files did not say
        # whether 0.012 meant a decimal return or 0.012 percent, so silently
        # multiplying/dividing here would fabricate precision.
        result["as_of_note"] = "legacy_units_unknown"
    elif source.get("as_of_note") in {"prior_close_only", "legacy_units_unknown"}:
        result["as_of_note"] = source["as_of_note"]
    return result


def event_template() -> dict[str, Any]:
    return {
        "event_id": "evt_cn_rate_0001",
        "market": "CN",
        "symbol": "600000",
        "event_time": "2025-01-10T09:00:00+08:00",
        "event_type_l2": "政策利率调整",
        "title": "LPR 下调",
        "event_text": "央行宣布 LPR 下调 10bp，市场预期银行与地产链风险偏好改善。",
        "source_url": "https://example.com/official-announcement",
        "sector_etf": "银行ETF",
        "benchmark": "sh000300",
        "direction_prior": "up",
        "event_strength": 2,
    }


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    market: Market
    symbol: str
    event_time: str
    event_type_l2: str
    title: str
    event_text: str
    source_url: str
    sector_etf: Optional[str] = None
    benchmark: Optional[str] = None
    direction_prior: Optional[str] = None
    event_strength: Optional[int] = None
    # Optional user/third-party analysis. Kept alongside the event so the
    # provided_analysis runner can evaluate it without exposing strategy code.
    analysis_direction: Optional[str] = None
    analysis_confidence: Optional[float] = None
    analysis_rationale: Optional[str] = None
    analysis_horizon: Optional[str] = None
    available_time: Optional[str] = None
    occurred_at: Optional[str] = None
    # Optional, explicitly supplied as-of facts.  These fields are never
    # populated from Oracle labels or post-event prices by EventRecord.
    event_facts: Optional[dict[str, Any]] = None
    pre_event_features: Optional[dict[str, Any]] = None
    analysis_expected_return_pct: Optional[float] = None

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "EventRecord":
        # ``event_time`` is the historical compatibility name for the decision
        # timestamp.  Newer datasets can preserve the economic occurrence time
        # separately and state when the information actually became available.
        # Every strategy-facing path must use the latter, otherwise a delayed
        # filing/news feed can be evaluated before it was observable.
        raw_event_time = str(d.get("event_time") or "").strip()
        raw_available_time = str(d.get("available_time") or d.get("available_at") or "").strip()
        decision_time = raw_available_time or raw_event_time
        occurred_at = str(d.get("occurred_at") or raw_event_time or "").strip()

        raw_facts = d.get("event_facts")
        facts_source: dict[str, Any] = dict(raw_facts) if isinstance(raw_facts, dict) else {}
        # Normalize legacy top-level macro/fundamental values into one
        # strategy-facing namespace.  Values are copied verbatim; no label or
        # return-derived enrichment is performed here.
        for key in ("actual_value", "expected_value", "previous_value", "value_unit"):
            if key in d and d.get(key) is not None and key not in facts_source:
                facts_source[key] = d.get(key)
        event_facts = sanitize_event_facts(facts_source)

        raw_features = d.get("pre_event_features")
        features_source: dict[str, Any] = (
            dict(raw_features) if isinstance(raw_features, dict) else {}
        )
        for canonical, aliases in _PRE_EVENT_FEATURE_ALIASES.items():
            if any(alias in features_source for alias in aliases):
                continue
            for alias in aliases:
                if alias in d and d.get(alias) is not None:
                    features_source[alias] = d.get(alias)
                    break
        if "as_of_note" in d and "as_of_note" not in features_source:
            features_source["as_of_note"] = d.get("as_of_note")
        pre_event_features = sanitize_pre_event_features(features_source)
        return EventRecord(
            event_id=str(d.get("event_id") or d.get("id") or ""),
            market=str(d.get("market") or "").upper(),  # type: ignore[arg-type]
            symbol=str(d.get("symbol") or ""),
            event_time=decision_time,
            event_type_l2=str(d.get("event_type_l2") or d.get("event_type") or ""),
            title=str(d.get("title") or ""),
            event_text=str(d.get("event_text") or d.get("text") or ""),
            source_url=str(d.get("source_url") or d.get("url") or ""),
            sector_etf=(str(d.get("sector_etf")).strip() or None) if d.get("sector_etf") is not None else None,
            benchmark=(str(d.get("benchmark")).strip() or None) if d.get("benchmark") is not None else None,
            direction_prior=(str(d.get("direction_prior")).strip() or None) if d.get("direction_prior") is not None else None,
            event_strength=int(d["event_strength"]) if d.get("event_strength") is not None else None,
            analysis_direction=(str(
                d.get("analysis_direction") or d.get("pred_direction") or d.get("direction")
            ).strip().lower() or None)
            if any(d.get(key) is not None for key in ("analysis_direction", "pred_direction", "direction")) else None,
            analysis_confidence=float(d["analysis_confidence"])
            if d.get("analysis_confidence") is not None else (
                float(d["confidence"]) if d.get("confidence") is not None else None
            ),
            analysis_rationale=(str(d.get("analysis_rationale") or d.get("rationale")).strip() or None)
            if (d.get("analysis_rationale") is not None or d.get("rationale") is not None) else None,
            analysis_horizon=(str(d.get("analysis_horizon") or d.get("horizon")).strip() or None)
            if (d.get("analysis_horizon") is not None or d.get("horizon") is not None) else None,
            available_time=(raw_available_time or decision_time or None),
            occurred_at=(occurred_at or None),
            event_facts=event_facts or None,
            pre_event_features=pre_event_features or None,
            analysis_expected_return_pct=finite_expected_return_pct(d.get("analysis_expected_return_pct", d.get("expected_return_pct"))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "market": self.market,
            "symbol": self.symbol,
            "event_time": self.event_time,
            "event_type_l2": self.event_type_l2,
            "title": self.title,
            "event_text": self.event_text,
            "source_url": self.source_url,
            "sector_etf": self.sector_etf,
            "benchmark": self.benchmark,
            "direction_prior": self.direction_prior,
            "event_strength": self.event_strength,
            "analysis_direction": self.analysis_direction,
            "analysis_confidence": self.analysis_confidence,
            "analysis_rationale": self.analysis_rationale,
            "analysis_horizon": self.analysis_horizon,
            "available_time": self.available_time,
            "occurred_at": self.occurred_at,
            "event_facts": sanitize_event_facts(self.event_facts) or None,
            "pre_event_features": sanitize_pre_event_features(self.pre_event_features) or None,
            "analysis_expected_return_pct": self.analysis_expected_return_pct,
        }


@dataclass(frozen=True)
class TeamPrediction:
    event_id: str
    pred_direction: Direction
    run_id: str
    model_version: str = ""
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    abstain: bool = False
    horizon: Optional[str] = None
    strategy_metadata: Optional[dict[str, Any]] = None
    tokens_in: int = 0
    tokens_out: int = 0
    step_ms: int = 0
    cost_usd: float = 0.0
    expected_return_pct: Optional[float] = None

    def __post_init__(self) -> None:
        forecast = finite_expected_return_pct(self.expected_return_pct)
        object.__setattr__(self, "expected_return_pct", forecast)
        if forecast is not None:
            metadata = dict(self.strategy_metadata or {})
            metadata["expected_return_pct"] = forecast
            metadata.setdefault("return_forecast_contract", return_forecast_contract(self.horizon or "t3"))
            object.__setattr__(self, "strategy_metadata", metadata)
    # Legacy multi-window predictions remain readable while new runs use the
    # explicit single ``horizon`` evaluation contract above.
    horizons: Optional[dict[str, Any]] = None

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TeamPrediction":
        return TeamPrediction(
            event_id=str(d.get("event_id") or d.get("id") or ""),
            pred_direction=str(d.get("pred_direction") or d.get("direction") or ""),
            run_id=str(d.get("run_id") or ""),
            model_version=str(d.get("model_version") or ""),
            confidence=float(d["confidence"]) if d.get("confidence") is not None else None,
            rationale=(str(d.get("rationale")).strip() or None) if d.get("rationale") is not None else None,
            abstain=bool(d.get("abstain") is True),
            horizon=(str(d.get("horizon")).strip() or None) if d.get("horizon") is not None else None,
            strategy_metadata=dict(d.get("strategy_metadata")) if isinstance(d.get("strategy_metadata"), dict) else None,
            tokens_in=max(0, int(d.get("tokens_in") or 0)),
            tokens_out=max(0, int(d.get("tokens_out") or 0)),
            step_ms=max(0, int(d.get("step_ms") or 0)),
            cost_usd=max(0.0, float(d.get("cost_usd") or 0.0)),
            expected_return_pct=finite_expected_return_pct(
                d.get("expected_return_pct", (d.get("strategy_metadata") if isinstance(d.get("strategy_metadata"), dict) else {}).get("expected_return_pct"))
            ),
            horizons=dict(d.get("horizons")) if isinstance(d.get("horizons"), dict) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "pred_direction": self.pred_direction,
            "run_id": self.run_id,
            "model_version": self.model_version,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "abstain": self.abstain,
            "horizon": self.horizon,
            "strategy_metadata": self.strategy_metadata,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "step_ms": self.step_ms,
            "cost_usd": self.cost_usd,
            "expected_return_pct": self.expected_return_pct,
            "horizons": self.horizons,
        }


@dataclass(frozen=True)
class EventLabel:
    event_id: str
    label_t1: Label
    label_t3: Label
    label_t5: Label
    label_t7: Label = ""
    label_t15: Label = ""
    label_t30: Label = ""
    label_t60: Label = ""
    label_avg_short: Label = ""
    label_avg_mid: Label = ""
    label_avg_long: Label = ""
    label_avg_all: Label = ""
    label_consensus66: Label = ""
    # Missing Oracle returns must remain missing. Treating them as 0.0 creates a
    # fictitious neutral/flat outcome and contaminates both accuracy and PnL.
    car_t1: Optional[float] = None
    car_t3: Optional[float] = None
    car_t5: Optional[float] = None
    car_t7: Optional[float] = None
    car_t15: Optional[float] = None
    car_t30: Optional[float] = None
    car_t60: Optional[float] = None
    car_avg_short: Optional[float] = None
    car_avg_mid: Optional[float] = None
    car_avg_long: Optional[float] = None
    car_avg_all: Optional[float] = None
    market: Optional[Market] = None
    symbol: Optional[str] = None
    event_time: Optional[str] = None
    event_type_l2: Optional[str] = None
    asset_ticker: Optional[str] = None
    benchmark_ticker: Optional[str] = None
    car_method: Optional[str] = None
    ret_t1: Optional[float] = None
    ret_t3: Optional[float] = None
    ret_t5: Optional[float] = None
    ret_t7: Optional[float] = None
    ret_t15: Optional[float] = None
    ret_t30: Optional[float] = None
    ret_t60: Optional[float] = None
    bm_ret_t1: Optional[float] = None
    bm_ret_t3: Optional[float] = None
    bm_ret_t5: Optional[float] = None
    bm_ret_t7: Optional[float] = None
    bm_ret_t15: Optional[float] = None
    bm_ret_t30: Optional[float] = None
    bm_ret_t60: Optional[float] = None
    car_t1_pvalue: Optional[float] = None
    car_t3_pvalue: Optional[float] = None
    car_t5_pvalue: Optional[float] = None
    car_t7_pvalue: Optional[float] = None
    car_t15_pvalue: Optional[float] = None
    car_t30_pvalue: Optional[float] = None
    car_t60_pvalue: Optional[float] = None
    # Consistency / horizon-metadata signals
    n_horizons_valid: Optional[int] = None
    n_horizons_signed: Optional[int] = None
    consensus_net: Optional[float] = None
    consensus_maj_frac: Optional[float] = None
    # Meta: which oracle horizon was used as the primary direction label (avg_all / t3 / etc.)
    primary_oracle_horizon: Optional[str] = None

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "EventLabel":
        market = d.get("market")
        def _safe_float(key):
            v = d.get(key)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        def _label(key):
            return str(d.get(key) or "")
        def _car(key):
            v = d.get(key)
            if v is None or v == "":
                return None
            try:
                parsed = float(v)
                return parsed if math.isfinite(parsed) else None
            except (TypeError, ValueError):
                return None
        def _opt_int(key):
            v = d.get(key)
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        return EventLabel(
            event_id=str(d.get("event_id") or d.get("id") or ""),
            # numeric horizon labels (backward-compat: t1/t3/t5 always present)
            label_t1=_label("label_t1"),
            label_t3=_label("label_t3"),
            label_t5=_label("label_t5"),
            # extended numeric horizons
            label_t7=_label("label_t7"),
            label_t15=_label("label_t15"),
            label_t30=_label("label_t30"),
            label_t60=_label("label_t60"),
            # avgCAR horizons
            label_avg_short=_label("label_avg_short"),
            label_avg_mid=_label("label_avg_mid"),
            label_avg_long=_label("label_avg_long"),
            label_avg_all=_label("label_avg_all"),
            # strict consensus horizon
            label_consensus66=_label("label_consensus66"),
            # Base horizons are nullable too. A literal 0 remains 0; absent or
            # malformed values remain None and are excluded from realized PnL.
            car_t1=_car("car_t1"),
            car_t3=_car("car_t3"),
            car_t5=_car("car_t5"),
            # extended cars (nullable: None = no data)
            car_t7=_car("car_t7"),
            car_t15=_car("car_t15"),
            car_t30=_car("car_t30"),
            car_t60=_car("car_t60"),
            car_avg_short=_car("car_avg_short"),
            car_avg_mid=_car("car_avg_mid"),
            car_avg_long=_car("car_avg_long"),
            car_avg_all=_car("car_avg_all"),
            market=(str(market).upper() if market is not None else None),  # type: ignore[arg-type]
            symbol=(str(d.get("symbol")).strip() or None) if d.get("symbol") is not None else None,
            event_time=(str(d.get("event_time")).strip() or None) if d.get("event_time") is not None else None,
            event_type_l2=(str(d.get("event_type_l2")).strip() or None) if d.get("event_type_l2") is not None else None,
            asset_ticker=(str(d.get("asset_ticker")).strip() or None) if d.get("asset_ticker") is not None else None,
            benchmark_ticker=(str(d.get("benchmark_ticker")).strip() or None) if d.get("benchmark_ticker") is not None else None,
            car_method=(str(d.get("car_method")).strip() or None) if d.get("car_method") is not None else None,
            ret_t1=_car("ret_t1"),
            ret_t3=_car("ret_t3"),
            ret_t5=_car("ret_t5"),
            ret_t7=_car("ret_t7"),
            ret_t15=_car("ret_t15"),
            ret_t30=_car("ret_t30"),
            ret_t60=_car("ret_t60"),
            bm_ret_t1=_car("bm_ret_t1"),
            bm_ret_t3=_car("bm_ret_t3"),
            bm_ret_t5=_car("bm_ret_t5"),
            bm_ret_t7=_car("bm_ret_t7"),
            bm_ret_t15=_car("bm_ret_t15"),
            bm_ret_t30=_car("bm_ret_t30"),
            bm_ret_t60=_car("bm_ret_t60"),
            car_t1_pvalue=_safe_float("car_t1_pvalue"),
            car_t3_pvalue=_safe_float("car_t3_pvalue"),
            car_t5_pvalue=_safe_float("car_t5_pvalue"),
            car_t7_pvalue=_safe_float("car_t7_pvalue"),
            car_t15_pvalue=_safe_float("car_t15_pvalue"),
            car_t30_pvalue=_safe_float("car_t30_pvalue"),
            car_t60_pvalue=_safe_float("car_t60_pvalue"),
            n_horizons_valid=_opt_int("n_horizons_valid"),
            n_horizons_signed=_opt_int("n_horizons_signed"),
            consensus_net=_safe_float("consensus_net"),
            consensus_maj_frac=_safe_float("consensus_maj_frac"),
            primary_oracle_horizon=str(d.get("primary_oracle_horizon") or "") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "label_t1": self.label_t1,
            "label_t3": self.label_t3,
            "label_t5": self.label_t5,
            "label_t7": self.label_t7,
            "label_t15": self.label_t15,
            "label_t30": self.label_t30,
            "label_t60": self.label_t60,
            "label_avg_short": self.label_avg_short,
            "label_avg_mid": self.label_avg_mid,
            "label_avg_long": self.label_avg_long,
            "label_avg_all": self.label_avg_all,
            "label_consensus66": self.label_consensus66,
            "car_t1": self.car_t1,
            "car_t3": self.car_t3,
            "car_t5": self.car_t5,
            "car_t7": self.car_t7,
            "car_t15": self.car_t15,
            "car_t30": self.car_t30,
            "car_t60": self.car_t60,
            "car_avg_short": self.car_avg_short,
            "car_avg_mid": self.car_avg_mid,
            "car_avg_long": self.car_avg_long,
            "car_avg_all": self.car_avg_all,
            "market": self.market,
            "symbol": self.symbol,
            "event_time": self.event_time,
            "event_type_l2": self.event_type_l2,
            "asset_ticker": self.asset_ticker,
            "benchmark_ticker": self.benchmark_ticker,
            "car_method": self.car_method,
            "ret_t1": self.ret_t1,
            "ret_t3": self.ret_t3,
            "ret_t5": self.ret_t5,
            "ret_t7": self.ret_t7,
            "ret_t15": self.ret_t15,
            "ret_t30": self.ret_t30,
            "ret_t60": self.ret_t60,
            "bm_ret_t1": self.bm_ret_t1,
            "bm_ret_t3": self.bm_ret_t3,
            "bm_ret_t5": self.bm_ret_t5,
            "bm_ret_t7": self.bm_ret_t7,
            "bm_ret_t15": self.bm_ret_t15,
            "bm_ret_t30": self.bm_ret_t30,
            "bm_ret_t60": self.bm_ret_t60,
            "car_t1_pvalue": self.car_t1_pvalue,
            "car_t3_pvalue": self.car_t3_pvalue,
            "car_t5_pvalue": self.car_t5_pvalue,
            "car_t7_pvalue": self.car_t7_pvalue,
            "car_t15_pvalue": self.car_t15_pvalue,
            "car_t30_pvalue": self.car_t30_pvalue,
            "car_t60_pvalue": self.car_t60_pvalue,
            "n_horizons_valid": self.n_horizons_valid,
            "n_horizons_signed": self.n_horizons_signed,
            "consensus_net": self.consensus_net,
            "consensus_maj_frac": self.consensus_maj_frac,
            "primary_oracle_horizon": self.primary_oracle_horizon,
        }
