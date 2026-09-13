"""Cancellation primitives shared by event backtest runners.

Keeping user cancellation distinct from an ordinary runner failure prevents
generic retry/fallback code from spending more model calls after a Run has
already been cancelled.
"""


class BacktestCancelled(RuntimeError):
    """Raised when a user-requested backtest cancellation must propagate."""
