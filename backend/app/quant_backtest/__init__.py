"""Independent quantitative backtesting engine for Pronoia.

This package deliberately does not depend on the event-backtest orchestrator.
The REST layer can call :class:`QuantBacktestService` without changing the
existing event prediction workflow.
"""

from .engine import run_backtest
from .models import (
    BacktestResult,
    Bar,
    ContextRecord,
    ExecutionConfig,
    MarketDataset,
    QuantBacktestError,
    Signal,
)
from .service import QuantBacktestService
from .store import QuantStore

__all__ = [
    "BacktestResult",
    "Bar",
    "ContextRecord",
    "ExecutionConfig",
    "MarketDataset",
    "QuantBacktestError",
    "QuantBacktestService",
    "QuantStore",
    "Signal",
    "run_backtest",
]
