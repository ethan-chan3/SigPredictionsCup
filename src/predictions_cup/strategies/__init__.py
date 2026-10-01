"""Strategy interface, adapted from PolyBot `src/polybot/strategies/`."""

from predictions_cup.strategies.base import (
    BaseStrategy,
    SignalHandler,
    StrategyConfig,
    StrategyState,
)

__all__ = ["BaseStrategy", "SignalHandler", "StrategyConfig", "StrategyState"]
