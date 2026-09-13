"""Route-friendly facade that joins data, strategy, engine, and persistence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import PROJECT_ROOT
from .data import AksharePublicDataSource, load_context_csv, load_csv_dataset
from .engine import run_backtest
from .models import ContextRecord, ExecutionConfig, MarketDataset, QuantBacktestError
from .store import QuantStore
from .strategies import QuantStrategy, strategy_from_spec


def _default_futures_daily_dir() -> Path:
    """Resolve the optional futures seed directory without a workstation path.

    Deployments that keep a larger futures archive outside the repository can
    opt in with ``PRONOIA_FUTURES_DAILY_DIR``.  A checkout remains portable and
    otherwise looks only under its own versionable seed-data directory.
    """
    configured = str(os.getenv("PRONOIA_FUTURES_DAILY_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return PROJECT_ROOT / "seed_data" / "futures" / "daily"


DEFAULT_FUTURES_DAILY_DIR = _default_futures_daily_dir()


class QuantBacktestService:
    """Synchronous MVP facade suitable for a FastAPI worker/background task.

    Public methods return JSON-compatible dictionaries. A future route only
    needs to pass validated request dictionaries; it does not need to know any
    engine or database details.
    """

    def __init__(
        self,
        *,
        store: QuantStore | None = None,
        public_source: AksharePublicDataSource | None = None,
        futures_daily_dir: str | Path | None = None,
    ):
        self.store = store or QuantStore()
        self.public_source = public_source or AksharePublicDataSource()
        self.futures_daily_dir = Path(futures_daily_dir or DEFAULT_FUTURES_DAILY_DIR).expanduser()

    def resolve_dataset(self, spec: Mapping[str, Any]) -> MarketDataset:
        """Resolve ``local_csv``, ``akshare``, or local-first ``auto`` data."""

        kind = str(spec.get("kind") or "auto").strip().lower()
        symbol = str(spec.get("symbol") or "").strip().upper()
        if not symbol:
            raise QuantBacktestError("行情 symbol 不能为空")
        frequency = str(spec.get("frequency") or "1d").strip()
        name = str(spec.get("name") or f"{symbol}-{frequency}")
        market = str(spec.get("market") or ("FUTURES" if kind in {"auto", "auto_futures"} else "CN"))
        common = {
            "name": name,
            "symbol": symbol,
            "market": market,
            "frequency": frequency,
            "start": str(spec["start"]) if spec.get("start") else None,
            "end": str(spec["end"]) if spec.get("end") else None,
            "repair_ohlc": bool(spec.get("repair_ohlc", True)),
        }
        if kind == "local_csv":
            path = str(spec.get("path") or "").strip()
            if not path:
                raise QuantBacktestError("local_csv 行情必须提供 path")
            return load_csv_dataset(path, **common)
        if kind in {"auto", "auto_futures"}:
            if frequency.lower() not in {"1d", "d", "day", "daily"}:
                raise QuantBacktestError("本地优先的期货 MVP 当前只自动解析日 K；分钟线请使用 local_csv")
            local_path = self.futures_daily_dir / f"{symbol}.csv"
            if local_path.is_file():
                return load_csv_dataset(local_path, **common)
            return self.public_source.fetch(
                asset_class="futures",
                symbol=symbol,
                frequency=frequency,
                start=str(spec.get("start") or "19900101"),
                end=str(spec.get("end") or "22220101"),
                name=name,
                repair_ohlc=common["repair_ohlc"],
            )
        if kind in {"akshare", "public"}:
            return self.public_source.fetch(
                asset_class=str(spec.get("asset_class") or "equity"),
                symbol=symbol,
                frequency=frequency,
                start=str(spec.get("start") or "19900101"),
                end=str(spec.get("end") or "22220101"),
                adjust=str(spec.get("adjust") or ""),
                name=name,
                repair_ohlc=common["repair_ohlc"],
            )
        raise QuantBacktestError(f"不支持的行情来源: {kind}")

    @staticmethod
    def resolve_context(
        *,
        context_path: str | Path | None = None,
        context_records: Sequence[ContextRecord] = (),
    ) -> tuple[ContextRecord, ...]:
        if context_path and context_records:
            raise QuantBacktestError("context_path 和 context_records 只能选择一种")
        if context_path:
            return load_context_csv(context_path)
        return tuple(context_records)

    def execute(
        self,
        *,
        name: str,
        data_spec: Mapping[str, Any],
        strategy_spec: Mapping[str, Any],
        execution_spec: Mapping[str, Any] | None = None,
        context_path: str | Path | None = None,
        context_records: Sequence[ContextRecord] = (),
    ) -> dict[str, Any]:
        """Resolve, execute, and atomically persist one quantitative run."""

        dataset = self.resolve_dataset(data_spec)
        strategy = strategy_from_spec(strategy_spec)
        context = self.resolve_context(context_path=context_path, context_records=context_records)
        execution = ExecutionConfig(**dict(execution_spec or {}))
        saved_dataset = self.store.save_dataset(dataset)
        run = self.store.create_run(
            name=name,
            dataset_id=str(saved_dataset["id"]),
            dataset_fingerprint=dataset.fingerprint,
            strategy=strategy.describe(),
            execution=execution.to_dict(),
        )
        run_id = str(run["id"])
        self.store.mark_running(run_id)
        try:
            signals = strategy.generate(dataset, context=context)
            result = run_backtest(
                dataset,
                signals,
                execution=execution,
                strategy=strategy.describe(),
            )
            return self.store.save_result(run_id, result)
        except Exception as exc:
            self.store.mark_failed(run_id, str(exc))
            raise

    def execute_loaded(
        self,
        *,
        name: str,
        dataset: MarketDataset,
        strategy: QuantStrategy,
        execution: ExecutionConfig | None = None,
        context: Sequence[ContextRecord] = (),
    ) -> dict[str, Any]:
        """Dependency-injection entry point for tests and custom in-process models."""

        execution = execution or ExecutionConfig()
        saved_dataset = self.store.save_dataset(dataset)
        run = self.store.create_run(
            name=name,
            dataset_id=str(saved_dataset["id"]),
            dataset_fingerprint=dataset.fingerprint,
            strategy=strategy.describe(),
            execution=execution.to_dict(),
        )
        run_id = str(run["id"])
        self.store.mark_running(run_id)
        try:
            result = run_backtest(
                dataset,
                strategy.generate(dataset, context=context),
                execution=execution,
                strategy=strategy.describe(),
            )
            return self.store.save_result(run_id, result)
        except Exception as exc:
            self.store.mark_failed(run_id, str(exc))
            raise

    def get_run(self, run_id: str, *, include_result: bool = True) -> dict[str, Any] | None:
        return self.store.get_run(run_id, include_result=include_result)

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_runs(limit=limit)

    def compare(self, run_ids: Sequence[str]) -> dict[str, Any]:
        return self.store.compare(run_ids)
