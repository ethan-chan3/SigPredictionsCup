"""Typed models for Predictions Cup (Super Market) API responses.

Field names follow the response schemas in the official API reference:
https://sig.thesuper.market/api/v1/docs

Only documented fields are mapped; the full original payload is kept in `raw`.
Per the docs, "All responses are JSON. Timestamps are ISO-8601 UTC."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _opt_float(value: Any) -> float | None:
    return float(value) if value is not None else None


@dataclass(frozen=True)
class PriceLevel:
    """`OrderbookLevel` schema: `{ price, quantity }`. Prices are YES-normalized (0-1)."""

    price: float
    quantity: float

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PriceLevel:
        return cls(price=float(data["price"]), quantity=float(data["quantity"]))


@dataclass
class Exchange:
    """A tradable contract inside a market (`exchanges[]` on Markets > GET /markets).

    Docs, Data Model: "Exchange is the API name for a tradable contract. All orders,
    trades, and positions reference an `exchangeId`."
    """

    id: str
    option: str | None
    latest_price: float | None
    initial_price: float | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Exchange:
        return cls(
            id=str(data["id"]),
            option=data.get("option"),
            latest_price=_opt_float(data.get("latestPrice")),
            initial_price=_opt_float(data.get("initialPrice")),
        )


@dataclass
class TournamentContext:
    """`MarketTournamentContext` schema (null for the public global context)."""

    id: str
    slug: str
    name: str
    currency_name: str
    is_ongoing_play: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TournamentContext:
        return cls(
            id=str(data["id"]),
            slug=data["slug"],
            name=data["name"],
            currency_name=data["currencyName"],
            is_ongoing_play=bool(data["isOngoingPlay"]),
        )


@dataclass
class MarketContext:
    """One entry of a market's `contexts[]`.

    Docs, Quickstart step 2: choose the intended `contexts[]` entry and carry its
    `tournament.id` through every context-sensitive read and write.
    """

    type: str  # "public" | "tournament"
    tournament: TournamentContext | None
    exchange_prices: dict[str, float | None] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> MarketContext:
        tournament = data.get("tournament")
        return cls(
            type=data["type"],
            tournament=TournamentContext.from_api(tournament) if tournament else None,
            exchange_prices={
                str(ex["id"]): _opt_float(ex.get("latestPrice")) for ex in data.get("exchanges", [])
            },
        )


@dataclass
class Market:
    """Response item of Markets > GET /markets and body of GET /markets/{id}."""

    id: str
    title: str
    status: str  # "open" | "closed" | "settled"
    created_at: datetime | None
    settlement_date: datetime | None
    settled_with: str | None
    settled_on: datetime | None
    categories: list[str]
    is_composite: bool
    is_multi_outcome: bool
    exchanges: list[Exchange]
    contexts: list[MarketContext]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Market:
        return cls(
            id=str(data["id"]),
            title=data["title"],
            status=data["status"],
            created_at=_parse_datetime(data.get("createdAt")),
            settlement_date=_parse_datetime(data.get("settlementDate")),
            settled_with=data.get("settledWith"),
            settled_on=_parse_datetime(data.get("settledOn")),
            categories=list(data.get("categories", [])),
            is_composite=bool(data.get("isComposite")),
            is_multi_outcome=bool(data.get("isMultiOutcome")),
            exchanges=[Exchange.from_api(ex) for ex in data.get("exchanges", [])],
            contexts=[MarketContext.from_api(ctx) for ctx in data.get("contexts", [])],
            raw=data,
        )


@dataclass
class MarketPage:
    """Paginated result of Markets > GET /markets (`data` + `pagination`).

    Docs, Quickstart: list endpoints return `pagination.hasMore` and an opaque
    `pagination.nextCursor` to pass back as `?cursor=`.
    """

    markets: list[Market]
    total: int
    limit: int
    has_more: bool
    next_cursor: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> MarketPage:
        pagination = data["pagination"]
        return cls(
            markets=[Market.from_api(item) for item in data["data"]],
            total=int(pagination["total"]),
            limit=int(pagination["limit"]),
            has_more=bool(pagination["hasMore"]),
            next_cursor=pagination.get("nextCursor"),
        )


@dataclass
class OrderBook:
    """Order book for one exchange.

    Built from Exchanges > GET /exchanges/{id}/orderbook (all fields populated) or
    from one `exchanges[]` item of Markets > GET /markets/{id}/orderbook, which
    documents only `exchangeId`, `option`, `impliedProbability`, `bids`, `asks`.
    Docs: "Prices are YES-normalized (0-1). Bids are sorted descending, asks ascending."
    """

    exchange_id: str
    bids: list[PriceLevel]
    asks: list[PriceLevel]
    market_id: str | None = None
    option: str | None = None
    depth: int | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    implied_probability: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any], market_id: str | None = None) -> OrderBook:
        return cls(
            exchange_id=str(data["exchangeId"]),
            bids=[PriceLevel.from_api(level) for level in data.get("bids", [])],
            asks=[PriceLevel.from_api(level) for level in data.get("asks", [])],
            market_id=str(data["marketId"]) if data.get("marketId") is not None else market_id,
            option=data.get("option"),
            depth=data.get("depth"),
            best_bid=_opt_float(data.get("bestBid")),
            best_ask=_opt_float(data.get("bestAsk")),
            spread=_opt_float(data.get("spread")),
            implied_probability=_opt_float(data.get("impliedProbability")),
            raw=data,
        )


@dataclass
class Position:
    """Item of `positions[]` from Portfolio > GET /portfolio/positions
    (same shape as Tournaments > GET /tournaments/{slug}/portfolio/positions).
    """

    exchange_id: str
    market_id: str
    market_title: str
    option: str | None
    settled: bool
    quantity: float  # Docs: "Positive = YES shares, negative = NO shares."
    avg_cost: float
    current_price: float | None
    market_value: float
    cost_basis: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    money_earned: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Position:
        return cls(
            exchange_id=str(data["exchangeId"]),
            market_id=str(data["marketId"]),
            market_title=data["marketTitle"],
            option=data.get("option"),
            settled=bool(data["settled"]),
            quantity=float(data["quantity"]),
            avg_cost=float(data["avgCost"]),
            current_price=_opt_float(data.get("currentPrice")),
            market_value=float(data["marketValue"]),
            cost_basis=float(data["costBasis"]),
            unrealized_pnl=float(data["unrealizedPnl"]),
            unrealized_pnl_pct=float(data["unrealizedPnlPct"]),
            money_earned=float(data["moneyEarned"]),
            raw=data,
        )


@dataclass
class Tournament:
    """Body of Tournaments > GET /tournaments/{slug}.

    Docs: the response `id` is the tournament UUID to pass as `tournamentId`;
    `myBalance` is the caller's current or seed virtual cash balance.
    """

    id: str
    slug: str
    name: str
    status: str
    currency_name: str
    initial_balance: float
    my_balance: float
    is_pending_enrolment: bool
    start_date: datetime | None
    end_date: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Tournament:
        return cls(
            id=str(data["id"]),
            slug=data["slug"],
            name=data["name"],
            status=data["status"],
            currency_name=data["currencyName"],
            initial_balance=float(data["initialBalance"]),
            my_balance=float(data["myBalance"]),
            is_pending_enrolment=bool(data["isPendingEnrolment"]),
            start_date=_parse_datetime(data.get("startDate")),
            end_date=_parse_datetime(data.get("endDate")),
            raw=data,
        )


@dataclass
class Balance:
    """Balance as documented on Account > GET /account.

    Docs: for an organization-bound key, `balance` comes from the organization's
    default tournament; for an unbound key it is the public-global cash balance.
    For an explicit tournament, the docs point to GET /tournaments/{slug} (`myBalance`).
    """

    profile_id: str
    username: str | None
    balance: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Balance:
        return cls(
            profile_id=str(data["id"]),
            username=data.get("username"),
            balance=_opt_float(data.get("balance")),
            raw=data,
        )
