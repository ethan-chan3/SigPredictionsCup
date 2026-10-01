"""Signal executor, adapted from PolyBot `src/polybot/services/executor.py`.

Keeps the routing of `ExecutorService._execute_signal`:
shadow -> `ShadowPositionTracker.simulate_signal`; live -> risk check -> place
order via the venue. Removed: the BaseService lifecycle, NNG queues, state DB,
log/event publishing, websocket order handling, and per-strategy shadow flags.

Differences from PolyBot:
- Risk checks also run in shadow mode (PolyBot skips them), so paper results
  respect the same limits a live run would.
- CLOSE signals are never blocked by risk checks.
- Orders go through the venue (`BaseVenue.place_order`) rather than a raw client.
  PredictionsCupVenue is read-only, so the live path currently always fails.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import List, Optional

from predictions_cup.execution.shadow_tracker import ShadowPositionTracker, ShadowTrade
from predictions_cup.messages import PriceUpdate, Signal, SignalAction
from predictions_cup.risk import RiskCheckResult, RiskManager, worst_case_loss
from predictions_cup.strategies.base import BaseStrategy
from predictions_cup.venues.base import BaseVenue, Order, Position
from predictions_cup.venues.types import OrderSide, OrderType

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    success: bool
    shadow: bool
    signal: Signal
    message: str = ""
    trade: Optional[ShadowTrade] = None
    risk: Optional[RiskCheckResult] = None


def _real_quotes(bid: Optional[float], ask: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Drop the venue's empty-book placeholders (bid 0.0 / ask 1.0)."""
    return (bid if bid is not None and bid > 0 else None, ask if ask is not None and ask < 1 else None)


class Executor:
    def __init__(
        self,
        venue: BaseVenue,
        risk_manager: RiskManager,
        shadow_tracker: Optional[ShadowPositionTracker] = None,
        *,
        shadow: bool = True,
    ) -> None:
        self._venue = venue
        self._risk = risk_manager
        self._tracker = shadow_tracker or ShadowPositionTracker()
        self._shadow = shadow

    @property
    def shadow_tracker(self) -> ShadowPositionTracker:
        return self._tracker

    @property
    def risk_manager(self) -> RiskManager:
        return self._risk

    def attach(self, strategy: BaseStrategy) -> None:
        """Route `strategy`'s signals here and keep its positions in sync with its fills.

        In shadow mode the strategy's positions mirror its shadow positions, so
        `has_position()` and `should_exit()` see paper trades.
        """

        async def handle(signal: Signal) -> None:
            await self.execute_signal(signal)
            if self._shadow:
                strategy.set_positions(self.shadow_positions(strategy.name))

        strategy.set_signal_handler(handle)
        if self._shadow:
            strategy.set_positions(self.shadow_positions(strategy.name))

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

    def mark_price(self, update: PriceUpdate) -> None:
        """Mark shadow positions to the mid, only when both sides of the book are real."""
        bid, ask = _real_quotes(update.bid, update.ask)
        if bid is not None and ask is not None:
            self._tracker.update_price(update.exchange_id, (bid + ask) / 2)

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
                logger.warning("Risk check failed for %s: %s", signal.strategy, risk.reason)
                return ExecutionResult(False, self._shadow, signal, f"Risk check failed: {risk.reason}", risk=risk)
            for warning in risk.warnings:
                logger.warning("Risk warning: %s", warning)
            if risk.adjusted_size is not None:
                signal = replace(signal, size=risk.adjusted_size)

        if self._shadow:
            return self._execute_shadow(signal, risk)
        return await self._execute_live(signal, risk)

    def _execute_shadow(self, signal: Signal, risk: Optional[RiskCheckResult]) -> ExecutionResult:
        # An immediate simulated fill needs a resting order on the opposite side.
        if signal.action == SignalAction.BUY and signal.ask is None:
            return ExecutionResult(False, True, signal, "No asks on the book; BUY cannot fill", risk=risk)
        if signal.action == SignalAction.SELL and signal.bid is None:
            return ExecutionResult(False, True, signal, "No bids on the book; SELL cannot fill", risk=risk)

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
        )
        if trade is None:
            return ExecutionResult(False, True, signal, "No shadow position to close", risk=risk)

        position = self._tracker.get_position(signal.strategy, signal.market_id, signal.exchange_id)
        if position is None:
            self._risk.close_position(signal.exchange_id)
        else:
            side = "buy" if position.side == "BUY" else "sell"
            self._risk.update_position(signal.exchange_id, worst_case_loss(side, position.size, position.entry_price))
        realized_after = self._tracker.get_stats(signal.strategy).get("total_realized_pnl", 0.0)
        if realized_after != realized_before:
            self._risk.record_pnl(realized_after - realized_before)

        logger.info(
            "[SHADOW] %s %s %.4f@%.4f filled at %.4f (%s)",
            signal.strategy, trade.side, trade.size, signal.price, trade.simulated_fill_price, signal.reason,
        )
        return ExecutionResult(True, True, signal, "Shadow mode - trade simulated", trade=trade, risk=risk)

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
