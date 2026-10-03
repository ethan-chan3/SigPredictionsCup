"""Strategies, adapted from PolyBot `src/polybot/strategies/`."""

from typing import Dict, Type

from predictions_cup.strategies.base import (
    BaseStrategy,
    SignalHandler,
    StrategyConfig,
    StrategyState,
)
from predictions_cup.strategies.spread_farm import SpreadFarmConfig, SpreadFarmStrategy

STRATEGIES: Dict[str, Type[BaseStrategy]] = {
    SpreadFarmStrategy.name: SpreadFarmStrategy,
}

__all__ = [
    "STRATEGIES",
    "BaseStrategy",
    "SignalHandler",
    "SpreadFarmConfig",
    "SpreadFarmStrategy",
    "StrategyConfig",
    "StrategyState",
]
