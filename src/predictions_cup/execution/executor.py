"""Signal executor, adapted from PolyBot `src/polybot/services/executor.py`.

Keeps the routing of `ExecutorService._execute_signal`:
shadow -> `ShadowPositionTracker.simulate_signal`; live -> risk check -> place
order via the venue. Removed: the BaseService lifecycle, NNG queues, state DB,
log/event publishing, websocket order handling, and per-strategy shadow flags.

Differences from PolyBot:
- Risk checks also run in shadow mode (PolyBot skips them).
- CLOSE signals are never blocked by risk checks.
- Shadow signals are simulated as limit orders. A marketable order (BUY at or
  above the best ask, SELL at or below the best bid) fills immediately at the
  opposite best price. A non-marketable order rests and fills at its own price
  only when a later book snapshot trades through it; it is cancelled after
  `resting_ttl_sec` or when the strategy re-quotes the same side. (PolyBot
  fills every shadow signal immediately at the signal price, which credits a
  passive quote with the full spread.)
- Fills, rejections, and cancellations are reported back to the strategy
  through `on_fill` / `on_cancel` (PolyBot defines fill handlers on strategies
  but never calls them).
- Orders go through the venue (`BaseVenue.place_order`) rather than a raw client.
  PredictionsCupVenue is read-only, so the live path currently always fails.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Dict, List, Optional, Tuple

from predictions_cup.execution.shadow_tracker import ShadowPositionTracker, ShadowTrade
from predictions_cup.messages import PriceUpdate, Signal, SignalAction
from predictions_cup.risk import RiskCheckResult, RiskManager, worst_case_loss
from predictions_cup.strategies.base import BaseStrategy
from predictions_cup.venues.base import BaseVenue, Order, Position
from predictions_cup.venues.types import OrderSide, OrderType

logger = logging.getLogger(__name__)


class PassiveFillRule(str, Enum):
    """When a resting shadow order counts as filled."""

    THROUGH = "through"  # Opposite best price moves strictly past our price
    TOUCH = "touch"  # Opposite best price reaches our price (ignores queue position)


@dataclass
class ExecutionResult:
    success: bool
    shadow: bool
    signal: Signal
    message: str = ""
    trade: Optional[ShadowTrade] = None
    risk: Optional[RiskCheckResult] = None
    resting: bool = False


@dataclass
class RestingOrder:
    signal: Signal
    placed_at: float


def _real_quotes(bid: Optional[float], ask: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Drop the venue's empty-book placeholders (bid 0.0 / ask 1.0)."""
    return (bid if bid is not None and bid > 0 else None, ask if ask is not None and ask < 1 else None)


def _fmt(value: Optional[float]) -> str:
    return f"{value:.3f}" if value is not None else "none"


class Executor:
    def __init__(
        self,
        venue: BaseVenue,
        risk_manager: RiskManager,
        shadow_tracker: Optional[ShadowPositionTracker] = None,
        *,
        shadow: bool = True,
        passive_fill: PassiveFillRule = PassiveFillRule.THROUGH,
        resting_ttl_sec: float = 30.0,
    ) -> None:
        self._venue = venue
        self._risk = risk_manager
        self._tracker = shadow_tracker or ShadowPositionTracker()
        self._shadow = shadow
        self._passive_fill = passive_fill
        self._resting_ttl = resting_ttl_sec
        self._resting: Dict[Tuple[str, str, str], RestingOrder] = {}  # (strategy, exchange, BUY/SELL)
        self._strategies: Dict[str, BaseStrategy] = {}

    @property
    def shadow_tracker(self) -> ShadowPositionTracker:
        return self._tracker

    @property
    def risk_manager(self) -> RiskManager:
        return self._risk

    def resting_orders(self) -> List[Signal]:
        return [order.signal for order in self._resting.values()]

    # =========================================================================
    # Strategy wiring
    # =========================================================================

    def attach(self, strategy: BaseStrategy) -> None:
        """Route `strategy`'s signals here and keep its positions in sync with its fills."""
        self._strategies[strategy.name] = strategy

        async def handle(signal: Signal) -> None:
            await self.execute_signal(signal)

        strategy.set_signal_handler(handle)
        self._sync_strategy(strategy.name)

    def shadow_positions(self, strategy: str) -> List[Position]:
        """Open shadow positions for `strategy`, as venue Positions (BUY -> long, SELL -> short)."""
        return [
            Position(
                symbol=p["exchange_id"],
                venue=self._venue.venue_type,
                side="long" if p["side"] == "BUY" else "short",
                size=p["size"],
                entry_price=p["entry_price"],
                current_price=p["current_price"] or None,
                unrealized_pnl=p["unrealized_pnl"],
            )
            for p in self._tracker.get_positions(strategy)
        ]

    def _sync_strategy(self, name: str) -> None:
        strategy = self._strategies.get(name)
        if strategy is not None and self._shadow:
            strategy.set_positions(self.shadow_positions(name))

    # =========================================================================
    # Price updates: marking, resting-order fills, expiry
    # =========================================================================

    def process_price_update(self, update: PriceUpdate) -> None:
        """Mark positions and match resting shadow orders against a new book snapshot."""
        bid, ask = _real_quotes(update.bid, update.ask)
        if bid is not None and ask is not None:
            self._tracker.update_price(update.exchange_id, (bid + ask) / 2)

        now = time.time()
        for key, order in list(self._resting.items()):
            sig = order.signal
            if sig.exchange_id != update.exchange_id:
                continue
            if self._crosses(sig, bid, ask):
                del self._resting[key]
                self._risk.decrement_open_orders()
                self._fill(replace(sig, bid=bid, ask=ask), None, fill_price=sig.price, how="passive")
            elif now - order.placed_at >= self._resting_ttl:
                self._cancel(key, "expired")

    def _crosses(self, sig: Signal, bid: Optional[float], ask: Optional[float]) -> bool:
        touch = self._passive_fill == PassiveFillRule.TOUCH
        if sig.action == SignalAction.BUY:
            return ask is not None and (ask < sig.price or (touch and ask <= sig.price))
        return bid is not None and (bid > sig.price or (touch and bid >= sig.price))

    # =========================================================================
    # Signal execution
    # =========================================================================

    async def execute_signal(self, signal: Signal) -> ExecutionResult:
        bid, ask = _real_quotes(signal.bid, signal.ask)
        signal = replace(signal, bid=bid, ask=ask)
        risk: Optional[RiskCheckResult] = None

        if signal.action != SignalAction.CLOSE:
            risk = self._risk.check_pre_trade(
                symbol=signal.exchange_id,
                side=signal.action.value.lower(),
                size=signal.size,
                price=signal.price,
                strategy=signal.strategy,
            )
            if not risk.approved:
                return self._reject(signal, f"risk: {risk.reason}", risk)
            for warning in risk.warnings:
                logger.warning("Risk warning: %s", warning)
            if risk.adjusted_size is not None:
                signal = replace(signal, size=risk.adjusted_size)

        if not self._shadow:
            return await self._execute_live(signal, risk)

        if signal.action == SignalAction.CLOSE:
            return self._execute_close(signal)

        marketable = (
            (signal.action == SignalAction.BUY and signal.ask is not None and signal.price >= signal.ask)
            or (signal.action == SignalAction.SELL and signal.bid is not None and signal.price <= signal.bid)
        )
        if marketable:
            return self._fill(signal, risk, how="taker")
        return self._rest(signal, risk)

    def _execute_close(self, signal: Signal) -> ExecutionResult:
        position = self._tracker.get_position(signal.strategy, signal.market_id, signal.exchange_id)
        if position is None:
            return self._reject(signal, "no shadow position to close", None)
        if position.side == "BUY" and signal.bid is None:
            return self._reject(signal, "cannot close long: no bids on the book", None)
        if position.side == "SELL" and signal.ask is None:
            return self._reject(signal, "cannot close short: no asks on the book", None)
        for side in ("BUY", "SELL"):
            key = (signal.strategy, signal.exchange_id, side)
            if key in self._resting:
                self._cancel(key, "position closed")
        return self._fill(signal, None, how="taker")

    def _rest(self, signal: Signal, risk: Optional[RiskCheckResult]) -> ExecutionResult:
        key = (signal.strategy, signal.exchange_id, signal.action.value)
        if key in self._resting:
            self._cancel(key, "replaced")
        self._resting[key] = RestingOrder(signal=signal, placed_at=time.time())
        self._risk.increment_open_orders()
        logger.info(
            "[REST] %s %s %s %.2f@%.3f (book bid=%s ask=%s), ttl=%gs",
            signal.strategy, signal.action.value, signal.exchange_id, signal.size, signal.price,
            _fmt(signal.bid), _fmt(signal.ask), self._resting_ttl,
        )
        return ExecutionResult(True, True, signal, "Resting limit order", risk=risk, resting=True)

    def _cancel(self, key: Tuple[str, str, str], reason: str) -> None:
        order = self._resting.pop(key)
        self._risk.decrement_open_orders()
        sig = order.signal
        logger.info("[CANCEL] %s %s %s @%.3f: %s", sig.strategy, sig.action.value, sig.exchange_id, sig.price, reason)
        strategy = self._strategies.get(sig.strategy)
        if strategy is not None:
            strategy.on_cancel(sig, reason)

    def _reject(self, signal: Signal, reason: str, risk: Optional[RiskCheckResult]) -> ExecutionResult:
        logger.info(
            "[REJECT] %s %s %s %.2f@%.3f: %s",
            signal.strategy, signal.action.value, signal.exchange_id, signal.size, signal.price, reason,
        )
        strategy = self._strategies.get(signal.strategy)
        if strategy is not None:
            strategy.on_cancel(signal, reason)
        return ExecutionResult(False, self._shadow, signal, reason, risk=risk)

    def _fill(
        self,
        signal: Signal,
        risk: Optional[RiskCheckResult],
        *,
        fill_price: Optional[float] = None,
        how: str,
    ) -> ExecutionResult:
        realized_before = self._tracker.get_stats(signal.strategy).get("total_realized_pnl", 0.0)
        trade = self._tracker.simulate_signal(
            strategy=signal.strategy,
            market_id=signal.market_id,
            exchange_id=signal.exchange_id,
            action=signal.action.value,
            signal_price=signal.price,
            size=signal.size,
            reason=signal.reason,
            confidence=signal.confidence,
            bid=signal.bid,
            ask=signal.ask,
            fill_price=fill_price,
        )
        if trade is None:
            return self._reject(signal, "no shadow position to close", risk)

        position = self._tracker.get_position(signal.strategy, signal.market_id, signal.exchange_id)
        if position is None:
            self._risk.close_position(signal.exchange_id)
        else:
            side = "buy" if position.side == "BUY" else "sell"
            self._risk.update_position(signal.exchange_id, worst_case_loss(side, position.size, position.entry_price))
        stats = self._tracker.get_stats(signal.strategy)
        realized_delta = stats["total_realized_pnl"] - realized_before
        if realized_delta:
            self._risk.record_pnl(realized_delta)

        logger.info(
            "[FILL] %s %s %s %.2f @ %.3f (%s; signal %.3f, book bid=%s ask=%s) realized=%+.2f | "
            "paper PnL realized=%.2f unrealized=%.2f total=%.2f",
            signal.strategy, trade.side, signal.exchange_id, trade.size, trade.simulated_fill_price, how,
            signal.price, _fmt(signal.bid), _fmt(signal.ask), realized_delta,
            stats["total_realized_pnl"], stats["total_unrealized_pnl"], stats["total_pnl"],
        )

        strategy = self._strategies.get(signal.strategy)
        if strategy is not None:
            strategy.on_fill(signal, trade.side, trade.size, trade.simulated_fill_price)
        self._sync_strategy(signal.strategy)
        return ExecutionResult(True, True, signal, f"Shadow fill ({how})", trade=trade, risk=risk)

    async def _execute_live(self, signal: Signal, risk: Optional[RiskCheckResult]) -> ExecutionResult:
        if signal.action == SignalAction.CLOSE:
            return ExecutionResult(False, False, signal, "Live position closing is not implemented", risk=risk)
        order = Order(
            symbol=signal.exchange_id,
            side=OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            size=signal.size,
            price=signal.price,
            metadata={"strategy": signal.strategy, "market_id": signal.market_id},
        )
        try:
            result = await self._venue.place_order(order)
        except NotImplementedError as exc:
            return ExecutionResult(False, False, signal, str(exc), risk=risk)
        if result.success:
            self._risk.increment_open_orders()
        return ExecutionResult(result.success, False, signal, result.error or "Order submitted", risk=risk)
