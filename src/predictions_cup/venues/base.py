"""Base venue interface.

Adapted from PolyBot `src/polybot/venues/base.py` with the same classes and
method signatures. Differences: timestamps are timezone-aware UTC, and the
docstrings note that trading methods may be unimplemented on read-only venues.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from predictions_cup.venues.types import (
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    VenueCapabilities,
    VenueType,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Ticker:
    """Price ticker from a venue."""

    symbol: str
    venue: VenueType
    bid: float
    ask: float
    last: Optional[float] = None
    volume_24h: Optional[float] = None
    timestamp: datetime = field(default_factory=_utcnow)

    @property
    def mid(self) -> float:
        """Mid price."""
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        """Bid-ask spread."""
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        """Spread as percentage of mid."""
        mid = self.mid
        return (self.spread / mid * 100) if mid > 0 else 0


@dataclass
class Market:
    """Market/instrument on a venue."""

    symbol: str
    venue: VenueType
    market_type: MarketType
    base_asset: Optional[str] = None
    quote_asset: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    min_order_size: Optional[float] = None
    tick_size: Optional[float] = None
    expiry: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Order:
    """Order to place on a venue."""

    symbol: str
    side: OrderSide
    order_type: OrderType
    size: float
    price: Optional[float] = None
    client_order_id: Optional[str] = None
    time_in_force: str = "GTC"
    reduce_only: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    """Result of an order placement."""

    success: bool
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0
    filled_price: Optional[float] = None
    error: Optional[str] = None
    venue: Optional[VenueType] = None
    timestamp: datetime = field(default_factory=_utcnow)
    raw_response: Optional[Dict[str, Any]] = None


@dataclass
class Position:
    """Position on a venue."""

    symbol: str
    venue: VenueType
    side: str  # "long" or "short"
    size: float
    entry_price: float
    current_price: Optional[float] = None
    unrealized_pnl: float = 0
    realized_pnl: float = 0
    liquidation_price: Optional[float] = None
    leverage: float = 1.0
    margin_type: Optional[str] = None
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def notional_usd(self) -> float:
        """Position notional value (in the venue's settlement currency)."""
        price = self.current_price or self.entry_price
        return abs(self.size * price)


@dataclass
class Balance:
    """Account balance on a venue."""

    venue: VenueType
    currency: str
    total: float
    available: float
    locked: float = 0
    updated_at: datetime = field(default_factory=_utcnow)


class BaseVenue(ABC):
    """Abstract base class for trading venues.

    All venue implementations inherit from this class and implement the
    abstract methods, giving strategies, risk, and execution one interface
    regardless of the underlying API.
    """

    venue_type: VenueType

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings
        self._connected = False
        self._shadow_mode = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    def set_shadow_mode(self, enabled: bool) -> None:
        """Enable or disable shadow mode for paper trading."""
        self._shadow_mode = enabled

    @abstractmethod
    def get_capabilities(self) -> VenueCapabilities:
        ...

    # =========================================================================
    # Lifecycle
    # =========================================================================

    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    # =========================================================================
    # Market Data
    # =========================================================================

    @abstractmethod
    async def get_markets(self, market_type: Optional[MarketType] = None) -> List[Market]:
        ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker:
        ...

    @abstractmethod
    async def subscribe_prices(
        self,
        symbols: List[str],
        callback: Callable[[Ticker], None],
    ) -> None:
        ...

    async def unsubscribe_prices(self, symbols: List[str]) -> None:
        pass

    # =========================================================================
    # Trading (read-only venues raise NotImplementedError)
    # =========================================================================

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult:
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> bool:
        ...

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        orders = await self.get_open_orders(symbol)
        cancelled = 0
        for order in orders:
            if await self.cancel_order(order.get("order_id", ""), symbol):
                cancelled += 1
        return cancelled

    @abstractmethod
    async def get_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        ...

    # =========================================================================
    # Positions & Account
    # =========================================================================

    @abstractmethod
    async def get_positions(self) -> List[Position]:
        ...

    @abstractmethod
    async def get_balance(self, currency: Optional[str] = None) -> Balance:
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} venue={self.venue_type.value} connected={self._connected}>"
