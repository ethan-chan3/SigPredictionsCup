"""Read-only HTTP client for the Predictions Cup (Super Market) API.

Official reference: https://sig.thesuper.market/api/v1/docs

This client intentionally exposes only GET requests. It does not place, modify,
or cancel orders. Every method here needs only the `read` scope
(docs, #description/scopes: "`read` | `GET` - market data, prices, orderbooks,
positions, fills, transaction history").
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import load_dotenv

from predictions_cup.models import Balance, Market, MarketPage, OrderBook, Position, Tournament

DOCS_URL = "https://sig.thesuper.market/api/v1/docs"

ENV_API_KEY = "PREDICTIONS_CUP_API_KEY"
ENV_BASE_URL = "PREDICTIONS_CUP_BASE_URL"
ENV_TIMEOUT = "PREDICTIONS_CUP_TIMEOUT"

# The spec's `servers` list "https://www.thesuper.market/api/v1" (Production) and
# "/api/v1" (Current environment). The docs are served from sig.thesuper.market, so
# its current-environment base is used by default; override via PREDICTIONS_CUP_BASE_URL.
DEFAULT_BASE_URL = "https://sig.thesuper.market/api/v1"

# Documented GET endpoints (docs, tags Markets / Exchanges / Portfolio / Account / Tournaments).
_ENDPOINTS: dict[str, str] = {
    "list_markets": "/markets",
    "get_market": "/markets/{market_id}",
    "get_market_orderbook": "/markets/{market_id}/orderbook",
    "get_exchange_orderbook": "/exchanges/{exchange_id}/orderbook",
    "get_account": "/account",
    "get_positions": "/portfolio/positions",
    "get_tournament_positions": "/tournaments/{slug}/portfolio/positions",
    "get_tournament": "/tournaments/{slug}",
}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


class PredictionsCupAPIError(RuntimeError):
    """Raised when the API returns a non-2xx response or the request fails.

    Docs, #description/error-responses: every error uses
    `{ "error": { "code", "message", "details"? } }`; branch on `code`, not `message`.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        code: str | None = None,
        details: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details


@dataclass(frozen=True)
class ClientConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 10.0

    def __repr__(self) -> str:
        return f"ClientConfig(base_url={self.base_url!r}, api_key=***, timeout={self.timeout})"

    @classmethod
    def from_env(cls) -> ClientConfig:
        load_dotenv()
        api_key = os.environ.get(ENV_API_KEY, "").strip()
        base_url = os.environ.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL
        if not api_key:
            raise ConfigError(
                f"Missing required environment variable: {ENV_API_KEY}. "
                "Set it in your shell or in a local .env file (see .env.example)."
            )
        timeout = os.environ.get(ENV_TIMEOUT, "").strip()
        try:
            timeout_sec = float(timeout) if timeout else 10.0
        except ValueError:
            raise ConfigError(f"{ENV_TIMEOUT} must be a number of seconds, got {timeout!r}") from None
        return cls(api_key=api_key, base_url=base_url.rstrip("/"), timeout=timeout_sec)


class PredictionsCupClient:
    def __init__(self, config: ClientConfig | None = None):
        self.config = config or ClientConfig.from_env()
        self._http = httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            headers=self._auth_headers(),
        )

    def __enter__(self) -> PredictionsCupClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _auth_headers(self) -> dict[str, str]:
        # Docs, #description/authentication: "All endpoints require an API key. Pass it
        # in one of two ways: `Authorization: Bearer <key>` or `X-API-Key: <key>`."
        # The spec's global security scheme is `bearerAuth`, so Bearer is used.
        return {"Authorization": f"Bearer {self.config.api_key}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        try:
            response = self._http.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise PredictionsCupAPIError(f"Request to {path} failed: {exc}") from exc
        if not response.is_success:
            code = details = None
            message = f"GET {path} returned HTTP {response.status_code}"
            try:
                error = response.json().get("error", {})
                code, details = error.get("code"), error.get("details")
                if error.get("message"):
                    message = f"{message} [{code}]: {error['message']}"
            except (ValueError, AttributeError):
                pass
            raise PredictionsCupAPIError(
                message, status_code=response.status_code, code=code, details=details
            )
        return response.json()

    def list_markets(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        tournament_id: str | None = None,
        search: str | None = None,
        sort: str | None = None,
        status: str | None = None,
        category: str | None = None,
        ids: list[str] | None = None,
    ) -> MarketPage:
        """Markets > GET /markets. `limit` max 100 (default 20); `sort` is one of
        recent|trending|closing|oldest; `status` is one of open|closed|settled|any.
        """
        params = {
            "limit": limit,
            "cursor": cursor,
            "tournamentId": tournament_id,
            "search": search,
            "sort": sort,
            "status": status,
            "category": category,
            "ids": ",".join(ids) if ids else None,
        }
        return MarketPage.from_api(self._get(_ENDPOINTS["list_markets"], params))

    def get_market(self, market_id: str, *, tournament_id: str | None = None) -> Market:
        """Markets > GET /markets/{id}."""
        path = _ENDPOINTS["get_market"].format(market_id=market_id)
        return Market.from_api(self._get(path, {"tournamentId": tournament_id}))

    def get_orderbook(
        self, exchange_id: str, *, depth: int | None = None, tournament_id: str | None = None
    ) -> OrderBook:
        """Exchanges > GET /exchanges/{id}/orderbook (`depth` max 200, default 20).

        Order books are per exchange (tradable contract), not per market; see the
        docs' Data Model section. Use `Market.exchanges[i].id` as `exchange_id`.
        """
        path = _ENDPOINTS["get_exchange_orderbook"].format(exchange_id=exchange_id)
        return OrderBook.from_api(self._get(path, {"depth": depth, "tournamentId": tournament_id}))

    def get_market_orderbooks(
        self, market_id: str, *, depth: int | None = None, tournament_id: str | None = None
    ) -> list[OrderBook]:
        """Markets > GET /markets/{id}/orderbook: one book per exchange in the market.

        Uses the top-level `exchanges[]`, which the docs say mirror the organization's
        default tournament (or the first accessible context) unless `tournament_id`
        is passed.
        """
        path = _ENDPOINTS["get_market_orderbook"].format(market_id=market_id)
        data = self._get(path, {"depth": depth, "tournamentId": tournament_id})
        return [OrderBook.from_api(ex, market_id=market_id) for ex in data["exchanges"]]

    def get_balance(self) -> Balance:
        """Account > GET /account (`balance` field)."""
        return Balance.from_api(self._get(_ENDPOINTS["get_account"]))

    def get_tournament(self, slug: str) -> Tournament:
        """Tournaments > GET /tournaments/{slug}.

        Docs, Common bot workflows: resolve a slug here, then pass its UUID `id` as
        `tournamentId` on market discovery and exchange price/orderbook reads.
        """
        return Tournament.from_api(self._get(_ENDPOINTS["get_tournament"].format(slug=slug)))

    def get_positions(self, *, tournament_slug: str | None = None) -> list[Position]:
        """Portfolio > GET /portfolio/positions, or
        Tournaments > GET /tournaments/{slug}/portfolio/positions when a slug is given.

        Docs, Common bot workflows: on an organization-bound key the no-argument call
        reports the organization's default tournament; use the slug form for an
        explicit non-default tournament.
        """
        if tournament_slug:
            path = _ENDPOINTS["get_tournament_positions"].format(slug=tournament_slug)
        else:
            path = _ENDPOINTS["get_positions"]
        data = self._get(path)
        return [Position.from_api(item) for item in data["positions"]]
