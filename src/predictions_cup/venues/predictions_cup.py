"""Predictions Cup venue implementation.

Wraps the existing PredictionsCupClient to implement the BaseVenue interface,
following PolyBot `venues/polymarket.py` (which wraps `core/client.py`).
Trading methods raise NotImplementedError, as in PolyBot `venues/kalshi.py`:
this venue is read-only.

Symbol convention: a venue `symbol` is an API *exchange* ID, because the API's
order books, positions, and orders all reference an `exchangeId`
(docs, Data Model: "Exchange is the API name for a tradable contract").
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv

from predictions_cup import models
from predictions_cup.client import PredictionsCupClient
from predictions_cup.venues.base import (
    Balance,
    BaseVenue,
    Market,
    Order,
    OrderResult,
    Position,
    Ticker,
)
from predictions_cup.venues.types import (
    VENUE_CAPABILITIES,
    MarketType,
    VenueCapabilities,
    VenueType,
)

logger = logging.getLogger(__name__)

ENV_TOURNAMENT_SLUG = "PREDICTIONS_CUP_TOURNAMENT_SLUG"

# Docs: "Fresh limit-order prices must be on the `0.005` tick between `0.005` and `0.995`."
TICK_SIZE = 0.005

# GET /markets page size cap (docs: "Number of results per page (max 100)").
_MARKETS_PAGE_LIMIT = 100

_READ_ONLY_MESSAGE = "PredictionsCupVenue is read-only; trading is not implemented."


class PredictionsCupVenue(BaseVenue):
    """Read-only Predictions Cup venue.

    Tournament context: if a tournament slug is configured (argument or
    PREDICTIONS_CUP_TOURNAMENT_SLUG), `connect()` resolves it via
    GET /tournaments/{slug} and every read is scoped to that tournament, as the
    docs' "Tournament trading" workflow requires. Without a slug, reads use the
    API key's default scope.
    """

    venue_type = VenueType.PREDICTIONS_CUP

    def __init__(
        self,
        settings: Any = None,
        *,
        client: Optional[PredictionsCupClient] = None,
        tournament_slug: Optional[str] = None,
    ) -> None:
        super().__init__(settings)
        load_dotenv()
        self._client = client
        self._owns_client = client is None
        self._tournament_slug = tournament_slug or os.environ.get(ENV_TOURNAMENT_SLUG, "").strip() or None
        self._tournament: Optional[models.Tournament] = None
        self._price_callbacks: Dict[str, List[Callable[[Ticker], None]]] = {}

    @property
    def tournament(self) -> Optional[models.Tournament]:
        return self._tournament

    def get_capabilities(self) -> VenueCapabilities:
        caps = VENUE_CAPABILITIES[VenueType.PREDICTIONS_CUP]
        if self._tournament:
            caps = replace(caps, settlement_currency=self._tournament.currency_name)
        return caps

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def connect(self) -> None:
        if self._connected:
            return
        if self._client is None:
            self._client = PredictionsCupClient()
        if self._tournament_slug:
            self._tournament = await asyncio.to_thread(self._client.get_tournament, self._tournament_slug)
            logger.info("Using tournament %s (%s)", self._tournament.slug, self._tournament.id)
        self._connected = True
        logger.info("Connected to Predictions Cup")

    async def disconnect(self) -> None:
        if self._client and self._owns_client:
            self._client.close()
            self._client = None
        self._connected = False
        self._price_callbacks.clear()
        logger.info("Disconnected from Predictions Cup")

    def _require_client(self) -> PredictionsCupClient:
        if not self._client or not self._connected:
            raise RuntimeError("Not connected")
        return self._client

    @property
    def _tournament_id(self) -> Optional[str]:
        return self._tournament.id if self._tournament else None

    # =========================================================================
    # Market Data
    # =========================================================================

    async def get_markets(self, market_type: Optional[MarketType] = None) -> List[Market]:
        """All markets in the tournament context, one venue Market per exchange."""
        client = self._require_client()
        if market_type not in (None, MarketType.PREDICTION):
            return []

        markets: List[Market] = []
        cursor: Optional[str] = None
        while True:
            page = await asyncio.to_thread(
                client.list_markets,
                limit=_MARKETS_PAGE_LIMIT,
                cursor=cursor,
                tournament_id=self._tournament_id,
            )
            for api_market in page.markets:
                markets.extend(self._to_venue_markets(api_market))
            if not page.has_more or not page.next_cursor:
                break
            cursor = page.next_cursor
        return markets

    def _to_venue_markets(self, api_market: models.Market) -> List[Market]:
        result = []
        for exchange in api_market.exchanges:
            description = api_market.title
            if api_market.is_multi_outcome and exchange.option:
                description = f"{api_market.title} [{exchange.option}]"
            result.append(
                Market(
                    symbol=exchange.id,
                    venue=self.venue_type,
                    market_type=MarketType.PREDICTION,
                    description=description,
                    is_active=api_market.status == "open",
                    tick_size=TICK_SIZE,
                    expiry=api_market.settlement_date,
                    metadata={
                        "market_id": api_market.id,
                        "option": exchange.option,
                        "status": api_market.status,
                        # Latest YES trade price in the resolved context; None until a trade
                        # establishes a tournament price (docs, GET /markets `latestPrice`).
                        "latest_price": exchange.latest_price,
                        "categories": api_market.categories,
                        "tournament_slugs": [c.tournament.slug for c in api_market.contexts if c.tournament],
                    },
                )
            )
        return result

    async def get_ticker(self, symbol: str) -> Ticker:
        """Best bid/ask from GET /exchanges/{id}/orderbook (YES-normalized, 0-1).

        As in PolyBot's PolymarketVenue, an empty side defaults to bid=0.0 / ask=1.0.
        """
        client = self._require_client()
        book = await asyncio.to_thread(
            client.get_orderbook, symbol, depth=1, tournament_id=self._tournament_id
        )
        return Ticker(
            symbol=symbol,
            venue=self.venue_type,
            bid=book.best_bid if book.best_bid is not None else 0.0,
            ask=book.best_ask if book.best_ask is not None else 1.0,
        )

    async def subscribe_prices(
        self,
        symbols: List[str],
        callback: Callable[[Ticker], None],
    ) -> None:
        """Register callbacks; prices are delivered by `poll_prices()` (no websocket yet)."""
        for symbol in symbols:
            self._price_callbacks.setdefault(symbol, []).append(callback)

    async def unsubscribe_prices(self, symbols: List[str]) -> None:
        for symbol in symbols:
            self._price_callbacks.pop(symbol, None)

    async def poll_prices(self) -> None:
        """Fetch a ticker for every subscribed symbol and invoke its callbacks."""
        for symbol, callbacks in list(self._price_callbacks.items()):
            try:
                ticker = await self.get_ticker(symbol)
            except Exception as exc:
                logger.warning("Failed to poll %s: %s", symbol, exc)
                continue
            for callback in callbacks:
                callback(ticker)

    # =========================================================================
    # Trading (read-only)
    # =========================================================================

    async def place_order(self, order: Order) -> OrderResult:
        raise NotImplementedError(_READ_ONLY_MESSAGE)

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> bool:
        raise NotImplementedError(_READ_ONLY_MESSAGE)

    async def get_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        raise NotImplementedError(_READ_ONLY_MESSAGE)

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError(_READ_ONLY_MESSAGE)

    # =========================================================================
    # Positions & Account
    # =========================================================================

    async def get_positions(self) -> List[Position]:
        client = self._require_client()
        api_positions = await asyncio.to_thread(client.get_positions, tournament_slug=self._tournament_slug)
        return [self._to_venue_position(p) for p in api_positions if p.quantity != 0]

    def _to_venue_position(self, p: models.Position) -> Position:
        # Docs: `quantity` is "Positive = YES shares, negative = NO shares."
        return Position(
            symbol=p.exchange_id,
            venue=self.venue_type,
            side="long" if p.quantity > 0 else "short",
            size=abs(p.quantity),
            entry_price=p.avg_cost,
            current_price=p.current_price,
            unrealized_pnl=p.unrealized_pnl,
            # Docs: `moneyEarned` is "Realized cash from prior closes on this position."
            realized_pnl=p.money_earned,
        )

    async def get_balance(self, currency: Optional[str] = None) -> Balance:
        """Tournament `myBalance` when a tournament is configured, else GET /account `balance`.

        The docs do not describe locked or reserved funds, so `available` equals
        `total` and `locked` is 0.
        """
        client = self._require_client()
        if self._tournament_slug:
            tournament = await asyncio.to_thread(client.get_tournament, self._tournament_slug)
            self._tournament = tournament
            total, currency_name = tournament.my_balance, tournament.currency_name
        else:
            account = await asyncio.to_thread(client.get_balance)
            if account.balance is None:
                raise RuntimeError("GET /account returned a null balance")
            total, currency_name = account.balance, self.get_capabilities().settlement_currency
        return Balance(venue=self.venue_type, currency=currency_name, total=total, available=total)
