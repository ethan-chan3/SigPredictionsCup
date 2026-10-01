"""Shadow position tracker for paper trading.

Adapted from PolyBot `src/polybot/core/shadow_tracker.py`. Same classes,
fill assumptions, position netting, and stats. Changes:
- `token_id` renamed `exchange_id`; timestamps are timezone-aware UTC.
- Default fill assumption is AGGRESSIVE (buy at ask, sell at bid), so paper
  results are not flattering.
- CLOSE trades are recorded and filled on the side opposite the open position
  (PolyBot always treats CLOSE as SELL, which misprices closing a short).
- No global singleton.

Prices are YES-normalized (0-1): BUY = long YES, SELL = short YES (i.e. long NO).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MIN_REMAINDER = 0.01


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ShadowFillAssumption(str, Enum):
    """How to simulate order fills in shadow mode."""

    IMMEDIATE = "immediate"  # Fill at signal price
    MIDPOINT = "midpoint"  # Fill at midpoint of bid/ask
    AGGRESSIVE = "aggressive"  # Worse price: ask for buy, bid for sell
    CONSERVATIVE = "conservative"  # Better price: bid for buy, ask for sell


@dataclass
class ShadowPosition:
    """A simulated position."""

    id: str
    strategy: str
    market_id: str
    exchange_id: str
    side: str  # "BUY" or "SELL"
    size: float
    entry_price: float
    entry_time: datetime
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    status: str = "open"
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    realized_pnl: float = 0.0

    def _pnl_at(self, price: float, size: float) -> float:
        if self.side == "BUY":
            return (price - self.entry_price) * size
        return (self.entry_price - price) * size

    def update_price(self, price: float) -> None:
        self.current_price = price
        self.unrealized_pnl = self._pnl_at(price, self.size)

    def close(self, exit_price: float) -> float:
        self.exit_price = exit_price
        self.exit_time = _utcnow()
        self.status = "closed"
        self.realized_pnl = self._pnl_at(exit_price, self.size)
        self.unrealized_pnl = 0.0
        return self.realized_pnl

    def to_dict(self) -> Dict[str, Any]:
        unrealized = (
            self._pnl_at(self.current_price, self.size)
            if self.status == "open" and self.current_price > 0
            else self.unrealized_pnl
        )
        return {
            "id": self.id,
            "strategy": self.strategy,
            "market_id": self.market_id,
            "exchange_id": self.exchange_id,
            "side": self.side,
            "size": self.size,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "current_price": self.current_price,
            "unrealized_pnl": round(unrealized, 4),
            "status": self.status,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "realized_pnl": self.realized_pnl,
        }


@dataclass
class ShadowTrade:
    """A simulated trade."""

    id: str
    strategy: str
    market_id: str
    exchange_id: str
    side: str
    price: float
    size: float
    notional: float
    timestamp: datetime
    signal_reason: str
    signal_confidence: float
    simulated_fill_price: float
    slippage: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "strategy": self.strategy,
            "market_id": self.market_id,
            "exchange_id": self.exchange_id,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "notional": self.notional,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "signal_reason": self.signal_reason,
            "signal_confidence": self.signal_confidence,
            "simulated_fill_price": self.simulated_fill_price,
            "slippage": self.slippage,
        }


@dataclass
class ShadowStrategyStats:
    """Aggregated shadow stats for one strategy."""

    strategy: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_realized_pnl: float = 0.0
    total_unrealized_pnl: float = 0.0
    total_volume: float = 0.0
    avg_trade_pnl: float = 0.0
    win_rate: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    open_positions: int = 0
    start_time: Optional[datetime] = None
    last_trade_time: Optional[datetime] = None

    def record_close(self, pnl: float) -> None:
        self.total_realized_pnl += pnl
        if pnl > 0:
            self.winning_trades += 1
            self.largest_win = max(self.largest_win, pnl)
        else:
            self.losing_trades += 1
            self.largest_loss = min(self.largest_loss, pnl)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "total_realized_pnl": round(self.total_realized_pnl, 4),
            "total_unrealized_pnl": round(self.total_unrealized_pnl, 4),
            "total_pnl": round(self.total_realized_pnl + self.total_unrealized_pnl, 4),
            "total_volume": round(self.total_volume, 2),
            "avg_trade_pnl": round(self.avg_trade_pnl, 4),
            "win_rate": round(self.win_rate, 4),
            "largest_win": round(self.largest_win, 4),
            "largest_loss": round(self.largest_loss, 4),
            "open_positions": self.open_positions,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "last_trade_time": self.last_trade_time.isoformat() if self.last_trade_time else None,
        }


class ShadowPositionTracker:
    """Tracks simulated positions, trades, and PnL per strategy."""

    def __init__(
        self,
        fill_assumption: ShadowFillAssumption = ShadowFillAssumption.AGGRESSIVE,
        slippage_bps: float = 0.0,
    ) -> None:
        self._fill_assumption = fill_assumption
        self._slippage_bps = slippage_bps
        self._positions: Dict[str, Dict[str, ShadowPosition]] = {}  # strategy -> key -> position
        self._trades: Dict[str, List[ShadowTrade]] = {}
        self._stats: Dict[str, ShadowStrategyStats] = {}
        self._current_prices: Dict[str, float] = {}  # exchange_id -> price
        self._trade_counter = 0

    @staticmethod
    def _key(market_id: str, exchange_id: str) -> str:
        return f"{market_id}:{exchange_id}"

    def get_position(self, strategy: str, market_id: str, exchange_id: str) -> Optional[ShadowPosition]:
        return self._positions.get(strategy, {}).get(self._key(market_id, exchange_id))

    def simulate_signal(
        self,
        strategy: str,
        market_id: str,
        exchange_id: str,
        action: str,  # "BUY", "SELL", "CLOSE"
        signal_price: float,
        size: float,
        reason: str = "",
        confidence: float = 0.0,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> Optional[ShadowTrade]:
        """Simulate a signal. Returns the trade, or None for a CLOSE with no position."""
        if strategy not in self._positions:
            self._positions[strategy] = {}
            self._trades[strategy] = []
            self._stats[strategy] = ShadowStrategyStats(strategy=strategy, start_time=_utcnow())

        pos_key = self._key(market_id, exchange_id)
        positions = self._positions[strategy]
        existing = positions.get(pos_key)

        if action == "CLOSE":
            if existing is None:
                logger.info("[SHADOW] %s CLOSE ignored: no position in %s", strategy, pos_key)
                return None
            trade_side = "SELL" if existing.side == "BUY" else "BUY"
            size = existing.size
        else:
            trade_side = action

        if bid is not None and ask is not None:
            market_price = (bid + ask) / 2
        else:
            market_price = bid if bid is not None else ask if ask is not None else signal_price
        self.update_price(exchange_id, market_price)

        fill_price = self._calculate_fill_price(signal_price, trade_side, bid, ask)

        self._trade_counter += 1
        trade = ShadowTrade(
            id=f"shadow_{strategy}_{self._trade_counter}",
            strategy=strategy,
            market_id=market_id,
            exchange_id=exchange_id,
            side=trade_side,
            price=signal_price,
            size=size,
            notional=fill_price * size,
            timestamp=_utcnow(),
            signal_reason=reason,
            signal_confidence=confidence,
            simulated_fill_price=fill_price,
            slippage=abs(fill_price - signal_price),
        )
        self._trades[strategy].append(trade)

        stats = self._stats[strategy]
        stats.total_trades += 1
        stats.total_volume += trade.notional
        stats.last_trade_time = trade.timestamp

        if existing and existing.side != trade_side:
            if size >= existing.size:
                pnl = existing.close(fill_price)
                del positions[pos_key]
                stats.record_close(pnl)
                logger.info("[SHADOW] %s closed %s %s: PnL=%.4f", strategy, existing.side, pos_key, pnl)
                remainder = size - existing.size
                if remainder > _MIN_REMAINDER:
                    positions[pos_key] = self._open(strategy, market_id, exchange_id, trade_side, remainder, fill_price, market_price)
            else:
                pnl = existing._pnl_at(fill_price, size)
                existing.size -= size
                stats.record_close(pnl)
                logger.info("[SHADOW] %s reduced %s by %.4f: PnL=%.4f", strategy, existing.side, size, pnl)
        elif existing:
            total_cost = existing.entry_price * existing.size + fill_price * size
            existing.size += size
            existing.entry_price = total_cost / existing.size
            existing.update_price(market_price)
            logger.info("[SHADOW] %s added %s %.4f@%.4f", strategy, trade_side, size, fill_price)
        else:
            positions[pos_key] = self._open(strategy, market_id, exchange_id, trade_side, size, fill_price, market_price)
            logger.info("[SHADOW] %s %s %.4f@%.4f (%s)", strategy, trade_side, size, fill_price, reason)

        self._update_stats(strategy)
        return trade

    def _open(
        self, strategy: str, market_id: str, exchange_id: str, side: str, size: float, fill_price: float, market_price: float
    ) -> ShadowPosition:
        position = ShadowPosition(
            id=f"shadow_pos_{strategy}_{self._trade_counter}",
            strategy=strategy,
            market_id=market_id,
            exchange_id=exchange_id,
            side=side,
            size=size,
            entry_price=fill_price,
            entry_time=_utcnow(),
        )
        position.update_price(market_price)
        return position

    def _calculate_fill_price(
        self, signal_price: float, side: str, bid: Optional[float], ask: Optional[float]
    ) -> float:
        fill_price = signal_price
        if self._fill_assumption == ShadowFillAssumption.MIDPOINT:
            if bid is not None and ask is not None:
                fill_price = (bid + ask) / 2
        elif self._fill_assumption == ShadowFillAssumption.AGGRESSIVE:
            if side == "BUY":
                fill_price = ask if ask is not None else signal_price
            else:
                fill_price = bid if bid is not None else signal_price
        elif self._fill_assumption == ShadowFillAssumption.CONSERVATIVE:
            if side == "BUY":
                fill_price = bid if bid is not None else signal_price
            else:
                fill_price = ask if ask is not None else signal_price

        if self._slippage_bps > 0:
            slippage_pct = self._slippage_bps / 10000
            fill_price *= (1 + slippage_pct) if side == "BUY" else (1 - slippage_pct)
        return fill_price

    def update_price(self, exchange_id: str, price: float) -> None:
        """Mark all open positions in `exchange_id` to `price`."""
        self._current_prices[exchange_id] = price
        for strategy, positions in self._positions.items():
            for position in positions.values():
                if position.exchange_id == exchange_id and position.status == "open":
                    position.update_price(price)
            self._update_stats(strategy)

    def _update_stats(self, strategy: str) -> None:
        stats = self._stats.get(strategy)
        if stats is None:
            return
        open_positions = [p for p in self._positions.get(strategy, {}).values() if p.status == "open"]
        stats.total_unrealized_pnl = sum(
            p._pnl_at(p.current_price, p.size) for p in open_positions if p.current_price > 0
        )
        stats.open_positions = len(open_positions)
        closed = stats.winning_trades + stats.losing_trades
        if closed > 0:
            stats.win_rate = stats.winning_trades / closed
            stats.avg_trade_pnl = stats.total_realized_pnl / closed

    def get_positions(self, strategy: Optional[str] = None) -> List[Dict[str, Any]]:
        strategies = [strategy] if strategy else list(self._positions)
        return [
            p.to_dict()
            for s in strategies
            for p in self._positions.get(s, {}).values()
            if p.status == "open"
        ]

    def get_trades(self, strategy: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        strategies = [strategy] if strategy else list(self._trades)
        trades = [t.to_dict() for s in strategies for t in self._trades.get(s, [])]
        trades.sort(key=lambda t: t["timestamp"] or "", reverse=True)
        return trades[:limit]

    def get_stats(self, strategy: Optional[str] = None) -> Dict[str, Any]:
        if strategy:
            return self._stats[strategy].to_dict() if strategy in self._stats else {}
        stats = self._stats.values()
        return {
            "strategies": {name: s.to_dict() for name, s in self._stats.items()},
            "totals": {
                "total_realized_pnl": sum(s.total_realized_pnl for s in stats),
                "total_unrealized_pnl": sum(s.total_unrealized_pnl for s in stats),
                "total_trades": sum(s.total_trades for s in stats),
                "total_volume": sum(s.total_volume for s in stats),
                "open_positions": sum(s.open_positions for s in stats),
            },
        }

    def get_summary(self) -> Dict[str, Any]:
        stats = list(self._stats.values())
        realized = sum(s.total_realized_pnl for s in stats)
        unrealized = sum(s.total_unrealized_pnl for s in stats)
        wins = sum(s.winning_trades for s in stats)
        losses = sum(s.losing_trades for s in stats)
        return {
            "total_pnl": round(realized + unrealized, 4),
            "realized_pnl": round(realized, 4),
            "unrealized_pnl": round(unrealized, 4),
            "total_trades": sum(s.total_trades for s in stats),
            "win_rate": round(wins / (wins + losses), 4) if wins + losses else 0,
            "strategies_count": len(stats),
            "open_positions": sum(s.open_positions for s in stats),
        }

    def reset(self, strategy: Optional[str] = None) -> None:
        if strategy:
            self._positions.pop(strategy, None)
            self._trades.pop(strategy, None)
            self._stats.pop(strategy, None)
        else:
            self._positions.clear()
            self._trades.clear()
            self._stats.clear()
