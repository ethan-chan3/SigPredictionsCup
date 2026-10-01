"""Price and signal messages passed between the venue, strategies, and (later) execution.

Adapted from PolyBot `src/polybot/models/messages.py`: `PriceUpdate`, `SignalAction`
and `Signal` keep PolyBot's fields and `to_dict`/`from_dict` helpers. Differences:
dataclasses instead of pydantic, and `token_id` is renamed `exchange_id` (the API's
tradable-contract ID). PolyBot's inter-process message types (MessageType,
OrderRequest/Response, SystemEvent, Heartbeat) are omitted.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from predictions_cup.venues.base import Ticker


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class PriceUpdate:
    """Price snapshot for one exchange, delivered to strategies.

    Prices are YES-normalized (0-1). Inherited from the venue Ticker convention,
    an empty bid side is reported as bid=0.0 and an empty ask side as ask=1.0.
    """

    market_id: str
    exchange_id: str
    bid: float
    ask: float
    mid: float
    spread: float
    timestamp: int = field(default_factory=_now_ms)

    @classmethod
    def from_ticker(cls, ticker: Ticker, market_id: str) -> PriceUpdate:
        return cls(
            market_id=market_id,
            exchange_id=ticker.symbol,
            bid=ticker.bid,
            ask=ticker.ask,
            mid=ticker.mid,
            spread=ticker.spread,
            timestamp=int(ticker.timestamp.timestamp() * 1000),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PriceUpdate:
        return cls(**data)


class SignalAction(str, Enum):
    """Signal action types."""

    BUY = "BUY"
    SELL = "SELL"
    CLOSE = "CLOSE"


@dataclass
class Signal:
    """Trading signal from a strategy to the executor.

    A signal is an intent, not an order: nothing in this package submits it.
    """

    strategy: str
    market_id: str
    exchange_id: str
    action: SignalAction
    price: float
    size: float
    reason: str = ""
    confidence: float = 1.0
    bid: Optional[float] = None  # Current bid, for shadow fill simulation and PnL tracking
    ask: Optional[float] = None  # Current ask, for shadow fill simulation and PnL tracking
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: int = field(default_factory=_now_ms)

    def __post_init__(self) -> None:
        if isinstance(self.action, str) and not isinstance(self.action, SignalAction):
            self.action = SignalAction(self.action)
        if not 0 <= self.confidence <= 1:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Signal:
        return cls(**data)
