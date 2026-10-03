"""Execution and paper trading, adapted from PolyBot `services/executor.py` and `core/shadow_tracker.py`."""

from predictions_cup.execution.executor import ExecutionResult, Executor, PassiveFillRule
from predictions_cup.execution.shadow_tracker import (
    ShadowFillAssumption,
    ShadowPosition,
    ShadowPositionTracker,
    ShadowStrategyStats,
    ShadowTrade,
)

__all__ = [
    "ExecutionResult",
    "Executor",
    "PassiveFillRule",
    "ShadowFillAssumption",
    "ShadowPosition",
    "ShadowPositionTracker",
    "ShadowStrategyStats",
    "ShadowTrade",
]
