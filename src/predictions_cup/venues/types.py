"""Venue type definitions and capabilities.

Adapted from PolyBot `src/polybot/venues/types.py`. Only the Predictions Cup
venue is defined; the enums are otherwise unchanged so venue code stays
interchangeable with PolyBot's interface.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class VenueType(str, Enum):
    """Supported trading venues."""

    PREDICTIONS_CUP = "predictions_cup"  # Susquehanna Predictions Cup (Super Market API)


class MarketType(str, Enum):
    """Types of markets within a venue."""

    PREDICTION = "prediction"  # Binary outcome markets
    SPOT = "spot"
    PERPETUAL = "perpetual"
    FUTURES = "futures"
    OPTIONS = "options"


class OrderSide(str, Enum):
    """Order side."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """Order type."""

    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


class OrderStatus(str, Enum):
    """Order status."""

    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class VenueCapabilities:
    """Capabilities of a trading venue."""

    supports_spot: bool = False
    supports_futures: bool = False
    supports_options: bool = False
    supports_prediction_markets: bool = False
    supports_websocket: bool = False
    supports_shadow_mode: bool = True
    requires_compliance_review: bool = False
    is_dex: bool = False
    settlement_currency: str = "USD"
    supported_market_types: List[MarketType] = field(default_factory=list)


VENUE_CAPABILITIES = {
    VenueType.PREDICTIONS_CUP: VenueCapabilities(
        supports_prediction_markets=True,
        # The API offers Supabase Realtime feeds, but this venue does not implement them yet.
        supports_websocket=False,
        supports_shadow_mode=True,
        # Replaced at runtime with the tournament's `currencyName` (e.g. "SUSQies").
        settlement_currency="UNKNOWN",
        supported_market_types=[MarketType.PREDICTION],
    ),
}
