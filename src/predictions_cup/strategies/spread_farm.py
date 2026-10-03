"""Spread farming strategy.

Ported from PolyBot `src/polybot/strategies/spread_farm.py` (`SpreadFarmStrategy`,
`SpreadFarmConfig`, `QuoteState`). The quoting logic is unchanged: quote a BUY at
the best bid and a SELL at the best ask when the spread is within bounds,
respecting per-side cooldowns, pending quotes, and an inventory limit; exit
when the spread collapses or blows out.

Changes for this repo:
- `token_id` -> `exchange_id`; quote state is keyed by exchange (one market can
  hold several exchanges on this API).
- Config comes from SPREAD_FARM_* env vars (same two PolyBot exposes) and can be
  overridden via `configure()`.
- PolyBot's `handle_fill` / `handle_cancel` / `handle_reject` are wired to the
  executor's `on_fill` / `on_cancel` callbacks. In PolyBot nothing calls them,
  so pending flags never reset and each market is quoted only once.
- Logging adds the executable bid/ask, spread, and expected edge per quote, and
  the rejection reason uses the same absolute spread the filter checks
  (PolyBot computed it from spread/mid).

Thresholds are absolute price differences on the 0-1 YES scale (0.02 = 2 cents).
"""

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Set

from dotenv import load_dotenv

from predictions_cup.messages import PriceUpdate, Signal, SignalAction
from predictions_cup.strategies.base import BaseStrategy, StrategyConfig
from predictions_cup.venues.base import Position


@dataclass
class SpreadFarmConfig(StrategyConfig):
    """Spread farming configuration."""

    # Spread thresholds (absolute)
    min_spread: float = 0.01
    max_spread: float = 0.05

    # Order sizing
    order_size: float = 10.0  # Currency units per order
    max_inventory: float = 100.0  # Max inventory per exchange (currency units)

    # Rate limiting
    signal_cooldown_sec: float = 60.0
    quote_refresh_sec: float = 30.0

    # Quality filters
    min_volume_24h: float = 1000.0  # Not enforced (PolyBot skips it; this API has no volume field)
    min_price: float = 0.01
    max_price: float = 0.99

    # Spread stability (for confidence)
    spread_stability_samples: int = 10


@dataclass
class QuoteState:
    """State for an exchange's quotes."""

    market_id: str
    exchange_id: str
    inventory: float = 0.0  # Positive = long, negative = short (currency units)

    bid_pending: bool = False
    ask_pending: bool = False
    last_bid_time: float = 0.0
    last_ask_time: float = 0.0

    spread_history: List[float] = field(default_factory=list)
    last_spread: float = 0.0

    signals_sent: int = 0
    fills_count: int = 0


class SpreadFarmStrategy(BaseStrategy):
    """Spread farming (market making) strategy.

    Workflow:
    1. Identify exchanges with sufficient spread (but not too wide)
    2. Apply cooldown to prevent signal spam
    3. Quote a buy at the bid and a sell at the ask
    4. Manage inventory to avoid excessive directional exposure
    """

    name = "spread_farm"
    description = "Market making to capture bid-ask spread"

    def __init__(self, settings=None, **kwargs) -> None:
        super().__init__(settings, **kwargs)
        self._quotes: Dict[str, QuoteState] = {}  # exchange_id -> QuoteState
        self._active_markets: Set[str] = set()
        self._reject_log: Dict[str, float] = {}

    def _get_config(self) -> StrategyConfig:
        load_dotenv()
        return SpreadFarmConfig(
            min_spread=float(os.environ.get("SPREAD_FARM_MIN_SPREAD", "0.02")),
            order_size=float(os.environ.get("SPREAD_FARM_ORDER_SIZE", "10.0")),
        )

    @property
    def spread_config(self) -> SpreadFarmConfig:
        return self._config  # type: ignore[return-value]

    async def scan(self, update: PriceUpdate) -> List[Signal]:
        signals: List[Signal] = []
        now = time.time()

        if not self._is_valid_price(update):
            self._log_reject(update, now, "no two-sided book or price out of bounds")
            return signals

        spread = update.ask - update.bid

        if not self._is_valid_spread(spread, update):
            reason = "too narrow" if spread < self.spread_config.min_spread else "too wide"
            self._log_reject(
                update, now,
                f"spread={spread:.3f} ({reason}), bounds=[{self.spread_config.min_spread:.3f}, "
                f"{self.spread_config.max_spread:.3f}]",
            )
            return signals

        # Volume check: PolyBot skips it when no volume data exists; this API exposes none.

        if update.exchange_id not in self._quotes:
            self._quotes[update.exchange_id] = QuoteState(
                market_id=update.market_id,
                exchange_id=update.exchange_id,
            )

        quote = self._quotes[update.exchange_id]
        self._update_spread_history(quote, spread)
        confidence = self._calculate_confidence(quote, spread)

        bid_cooldown_ok = (now - quote.last_bid_time) >= self.spread_config.signal_cooldown_sec
        ask_cooldown_ok = (now - quote.last_ask_time) >= self.spread_config.signal_cooldown_sec

        inventory_limit_reached = abs(quote.inventory) >= self.spread_config.max_inventory

        if inventory_limit_reached:
            if quote.inventory > 0:
                if ask_cooldown_ok and not quote.ask_pending:
                    signals.append(self._create_ask_signal(update, quote, confidence))
                    quote.ask_pending = True
                    quote.last_ask_time = now
                    quote.signals_sent += 1
            else:
                if bid_cooldown_ok and not quote.bid_pending:
                    signals.append(self._create_bid_signal(update, quote, confidence))
                    quote.bid_pending = True
                    quote.last_bid_time = now
                    quote.signals_sent += 1
        else:
            if bid_cooldown_ok and not quote.bid_pending:
                signals.append(self._create_bid_signal(update, quote, confidence))
                quote.bid_pending = True
                quote.last_bid_time = now
                quote.signals_sent += 1

            if ask_cooldown_ok and not quote.ask_pending:
                signals.append(self._create_ask_signal(update, quote, confidence))
                quote.ask_pending = True
                quote.last_ask_time = now
                quote.signals_sent += 1

        if signals:
            self._active_markets.add(update.market_id)
            self._logger.info(
                "[CANDIDATE] exchange=%s bid=%.3f ask=%.3f spread=%.3f mid=%.4f "
                "edge/share=%.4f (vs mid) round-trip=%.3f confidence=%.2f inventory=%.2f quotes=%s",
                update.exchange_id, update.bid, update.ask, spread, update.mid,
                spread / 2, spread, confidence, quote.inventory,
                "+".join(s.action.value for s in signals),
            )
        return signals

    def _log_reject(self, update: PriceUpdate, now: float, reason: str) -> None:
        # At most once per minute per exchange, as in PolyBot.
        if now - self._reject_log.get(update.exchange_id, 0) > 60:
            self._reject_log[update.exchange_id] = now
            self._logger.info(
                "[REJECT] exchange=%s bid=%.3f ask=%.3f: %s",
                update.exchange_id, update.bid, update.ask, reason,
            )

    def _is_valid_price(self, update: PriceUpdate) -> bool:
        if not update.bid or not update.ask:
            return False
        if update.bid < self.spread_config.min_price:
            return False
        if update.ask > self.spread_config.max_price:
            return False
        if update.bid >= update.ask:
            return False
        return True

    def _is_valid_spread(self, spread: float, update: PriceUpdate) -> bool:
        if spread < self.spread_config.min_spread:
            return False
        if spread > self.spread_config.max_spread:
            return False
        return True

    def _update_spread_history(self, quote: QuoteState, spread: float) -> None:
        quote.spread_history.append(spread)
        max_samples = self.spread_config.spread_stability_samples
        if len(quote.spread_history) > max_samples:
            quote.spread_history = quote.spread_history[-max_samples:]
        quote.last_spread = spread

    def _calculate_confidence(self, quote: QuoteState, current_spread: float) -> float:
        """Confidence in [0.5, 0.95] from spread stability (unchanged from PolyBot)."""
        if len(quote.spread_history) < 3:
            return 0.7

        avg_spread = sum(quote.spread_history) / len(quote.spread_history)
        if avg_spread <= 0:
            return 0.5

        variance = sum((s - avg_spread) ** 2 for s in quote.spread_history) / len(quote.spread_history)
        cv = variance ** 0.5 / avg_spread
        stability_score = max(0.5, 0.95 - cv * 0.9)

        if len(quote.spread_history) >= 5:
            recent_avg = sum(quote.spread_history[-3:]) / 3
            older_avg = sum(quote.spread_history[:-3]) / max(1, len(quote.spread_history) - 3)
            if recent_avg < older_avg:
                stability_score = min(0.95, stability_score + 0.05)
            elif recent_avg > older_avg * 1.2:
                stability_score = max(0.5, stability_score - 0.1)

        return round(stability_score, 2)

    def _create_bid_signal(self, update: PriceUpdate, quote: QuoteState, confidence: float) -> Signal:
        size = self.spread_config.order_size / update.mid
        return Signal(
            strategy=self.name,
            market_id=update.market_id,
            exchange_id=update.exchange_id,
            action=SignalAction.BUY,
            price=update.bid,
            size=size,
            reason=f"Spread farm bid, spread={update.spread*100:.2f}%, inv={quote.inventory:.1f}",
            confidence=confidence,
            bid=update.bid,
            ask=update.ask,
            metadata={"expected_edge": update.mid - update.bid, "spread": update.spread},
        )

    def _create_ask_signal(self, update: PriceUpdate, quote: QuoteState, confidence: float) -> Signal:
        size = self.spread_config.order_size / update.mid
        return Signal(
            strategy=self.name,
            market_id=update.market_id,
            exchange_id=update.exchange_id,
            action=SignalAction.SELL,
            price=update.ask,
            size=size,
            reason=f"Spread farm ask, spread={update.spread*100:.2f}%, inv={quote.inventory:.1f}",
            confidence=confidence,
            bid=update.bid,
            ask=update.ask,
            metadata={"expected_edge": update.ask - update.mid, "spread": update.spread},
        )

    async def should_exit(self, position: Position, update: PriceUpdate) -> bool:
        if not update.bid or not update.ask or update.bid <= 0 or update.ask <= 0:
            return False

        spread = update.ask - update.bid

        if spread < self.spread_config.min_spread * 0.5:
            quote = self._quotes.get(position.symbol)
            if quote and abs(quote.inventory) > 0:
                self._logger.info("Spread farm exit: spread collapsed to %.3f", spread)
                return True

        if spread > self.spread_config.max_spread * 1.5:
            self._logger.info("Spread farm exit: spread widened to %.3f", spread)
            return True

        return False

    # =========================================================================
    # Executor callbacks -> PolyBot handlers
    # =========================================================================

    def on_fill(self, signal: Signal, side: str, size: float, price: float) -> None:
        if signal.action == SignalAction.CLOSE:
            quote = self._quotes.get(signal.exchange_id)
            if quote:
                quote.inventory = 0.0
            return
        self.handle_fill(signal.exchange_id, side, size, price)

    def on_cancel(self, signal: Signal, reason: str) -> None:
        if signal.action == SignalAction.CLOSE:
            return
        side = signal.action.value
        if reason == "expired" or reason == "replaced" or reason == "position closed":
            self.handle_cancel(signal.exchange_id, side)
        else:
            self.handle_reject(signal.exchange_id, side, reason)

    def handle_fill(self, exchange_id: str, side: str, size: float, price: float) -> None:
        quote = self._quotes.get(exchange_id)
        if not quote:
            return
        if side == "BUY":
            quote.inventory += size * price
            quote.bid_pending = False
        else:
            quote.inventory -= size * price
            quote.ask_pending = False
        quote.fills_count += 1
        self._logger.info(
            "Spread farm %s filled: %.2f @ %.3f, inventory=%.2f",
            "bid" if side == "BUY" else "ask", size * price, price, quote.inventory,
        )

    def handle_cancel(self, exchange_id: str, side: str) -> None:
        quote = self._quotes.get(exchange_id)
        if not quote:
            return
        if side == "BUY":
            quote.bid_pending = False
        else:
            quote.ask_pending = False

    def handle_reject(self, exchange_id: str, side: str, reason: str) -> None:
        self.handle_cancel(exchange_id, side)
        self._logger.warning("Order rejected for %s: %s", exchange_id, reason)

    # =========================================================================
    # Status
    # =========================================================================

    def get_quotes(self) -> Dict[str, Dict]:
        return {
            exchange_id: {
                "market_id": q.market_id,
                "inventory": q.inventory,
                "bid_pending": q.bid_pending,
                "ask_pending": q.ask_pending,
                "last_spread": q.last_spread,
                "signals_sent": q.signals_sent,
                "fills_count": q.fills_count,
                "spread_stability": self._calculate_confidence(q, q.last_spread),
            }
            for exchange_id, q in self._quotes.items()
        }

    def get_stats(self) -> Dict:
        base_stats = super().get_stats()
        total_signals = sum(q.signals_sent for q in self._quotes.values())
        total_fills = sum(q.fills_count for q in self._quotes.values())
        base_stats.update({
            "active_markets": len(self._active_markets),
            "total_inventory": sum(q.inventory for q in self._quotes.values()),
            "total_signals": total_signals,
            "total_fills": total_fills,
            "fill_rate": total_fills / total_signals if total_signals > 0 else 0,
        })
        return base_stats
