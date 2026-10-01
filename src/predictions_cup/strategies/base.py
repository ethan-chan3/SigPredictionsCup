"""Base strategy interface.

Adapted from PolyBot `src/polybot/strategies/base.py`. Kept: `StrategyConfig`,
`StrategyState`, the `BaseStrategy` contract (`_get_config`, `scan`, `should_exit`,
`_on_start`/`_on_stop`, `get_stats`) and the per-update flow of
`_handle_price_update`. Removed: NNG sockets, state/log services, SQLite, and the
self-driven main loop. Instead, a caller feeds updates via `handle_price_update()`
and receives signals through the injected `signal_handler`.
"""

from __future__ import annotations

import inspect
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Union

from predictions_cup.messages import PriceUpdate, Signal, SignalAction
from predictions_cup.venues.base import Position

SignalHandler = Callable[[Signal], Union[None, Awaitable[None]]]


@dataclass
class StrategyConfig:
    """Base configuration for strategies."""

    max_position_size: float = 100.0
    max_positions: int = 10


@dataclass
class StrategyState:
    """Runtime state for a strategy."""

    positions: Dict[str, Position] = field(default_factory=dict)  # keyed by exchange_id
    signals_sent: int = 0
    last_scan_time: float = 0
    scans_performed: int = 0
    opportunities_found: int = 0
    entries: int = 0
    exits: int = 0
    errors: int = 0


class BaseStrategy(ABC):
    """Abstract base class for trading strategies.

    Subclasses implement:
    - `_get_config()`: return the strategy's StrategyConfig (or subclass)
    - `scan(update)`: analyze a price update and return signals
    - `should_exit(position, update)`: decide whether an open position should close

    Lifecycle:
    1. `await start()`
    2. The caller (e.g. a polling loop over the venue) calls `handle_price_update()`
    3. Signals from `scan()` and exit checks go to `signal_handler`
    4. `await stop()`
    """

    name: str = "base"
    description: str = "Base strategy"

    def __init__(self, settings: Any = None, *, signal_handler: Optional[SignalHandler] = None) -> None:
        self._settings = settings
        self._config = self._get_config()
        self._state = StrategyState()
        self._running = False
        self._signal_handler = signal_handler
        self._logger = logging.getLogger(f"predictions_cup.strategies.{self.name}")
        self._prices: Dict[str, PriceUpdate] = {}  # keyed by exchange_id

    @abstractmethod
    def _get_config(self) -> StrategyConfig:
        ...

    @property
    def config(self) -> StrategyConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._logger.info("Starting %s strategy", self.name)
        await self._on_start()
        self._running = True

    async def stop(self) -> None:
        self._logger.info("Stopping %s strategy", self.name)
        self._running = False
        await self._on_stop()

    def set_signal_handler(self, handler: Optional[SignalHandler]) -> None:
        self._signal_handler = handler

    def set_positions(self, positions: Iterable[Position]) -> None:
        """Replace the strategy's tracked positions (e.g. from `venue.get_positions()`).

        The API does not attribute positions to strategies, so the caller decides
        which venue positions belong to this strategy.
        """
        self._state.positions = {p.symbol: p for p in positions}

    async def handle_price_update(self, update: PriceUpdate) -> List[Signal]:
        """Process one price update; returns every signal emitted for it."""
        if not self._running:
            return []

        self._prices[update.exchange_id] = update
        scan_start = time.time()
        emitted: List[Signal] = []

        try:
            signals = await self.scan(update)
        except Exception:
            self._state.errors += 1
            self._logger.exception("scan() failed for exchange %s", update.exchange_id)
            signals = []

        self._state.scans_performed += 1
        self._state.last_scan_time = scan_start

        if signals:
            self._state.opportunities_found += len(signals)
            for signal in signals:
                await self._send_signal(signal)
                emitted.append(signal)

        position = self._state.positions.get(update.exchange_id)
        if position is not None:
            # Only the mark is refreshed; unrealized PnL stays as reported by the API,
            # because the docs do not define avgCost's denomination for NO positions.
            position.current_price = update.mid
            try:
                exit_now = await self.should_exit(position, update)
            except Exception:
                self._state.errors += 1
                self._logger.exception("should_exit() failed for exchange %s", update.exchange_id)
                exit_now = False
            if exit_now:
                exit_signal = Signal(
                    strategy=self.name,
                    market_id=update.market_id,
                    exchange_id=position.symbol,
                    action=SignalAction.CLOSE,
                    price=update.mid,
                    size=position.size,
                    reason="Exit condition met",
                    bid=update.bid,
                    ask=update.ask,
                )
                await self._send_signal(exit_signal)
                emitted.append(exit_signal)

        return emitted

    async def _send_signal(self, signal: Signal) -> None:
        self._logger.info(
            "Signal: %s %s@%s for exchange %s (%s)",
            signal.action.value, signal.size, signal.price, signal.exchange_id, signal.reason,
        )
        self._state.signals_sent += 1
        if signal.action == SignalAction.CLOSE:
            self._state.exits += 1
        else:
            self._state.entries += 1
        if self._signal_handler is not None:
            result = self._signal_handler(signal)
            if inspect.isawaitable(result):
                await result

    # =========================================================================
    # Abstract methods
    # =========================================================================

    @abstractmethod
    async def scan(self, update: PriceUpdate) -> List[Signal]:
        """Return trading signals for this update (empty list if none)."""
        ...

    @abstractmethod
    async def should_exit(self, position: Position, update: PriceUpdate) -> bool:
        """Return True if `position` should be closed given `update`."""
        ...

    # =========================================================================
    # Optional hooks
    # =========================================================================

    async def _on_start(self) -> None:
        pass

    async def _on_stop(self) -> None:
        pass

    # =========================================================================
    # Utilities
    # =========================================================================

    def get_price(self, exchange_id: str) -> Optional[PriceUpdate]:
        return self._prices.get(exchange_id)

    def get_position(self, exchange_id: str) -> Optional[Position]:
        return self._state.positions.get(exchange_id)

    def has_position(self, exchange_id: str) -> bool:
        return exchange_id in self._state.positions

    def get_stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "running": self._running,
            "positions": len(self._state.positions),
            **{f.name: getattr(self._state, f.name) for f in fields(self._state) if f.name != "positions"},
        }
