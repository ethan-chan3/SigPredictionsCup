"""Polling price scanner, adapted from the poll loop in PolyBot `services/scanner.py`.

Fetches a ticker per tracked contract from the venue, builds a PriceUpdate,
marks shadow positions, and feeds every strategy. PolyBot's market refresh,
database snapshots, websocket handling, and NNG publishing are omitted.

Each tracked contract costs one API request per poll; the docs enforce
per-key rate limits (429 RATE_LIMITED) without publishing the numbers, so keep
the tracked list small and the interval generous.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable, List, Optional

from predictions_cup.execution.executor import Executor
from predictions_cup.messages import PriceUpdate
from predictions_cup.strategies.base import BaseStrategy
from predictions_cup.venues.base import BaseVenue, Market

logger = logging.getLogger(__name__)


class PriceScanner:
    def __init__(
        self,
        venue: BaseVenue,
        markets: Iterable[Market],
        strategies: Iterable[BaseStrategy],
        *,
        executor: Optional[Executor] = None,
        interval: float = 10.0,
    ) -> None:
        self._venue = venue
        self._markets = list(markets)
        self._strategies = list(strategies)
        self._executor = executor
        self._interval = interval

    async def poll_once(self) -> List[PriceUpdate]:
        updates: List[PriceUpdate] = []
        for market in self._markets:
            try:
                ticker = await self._venue.get_ticker(market.symbol)
            except Exception as exc:
                logger.warning("Failed to fetch ticker for %s: %s", market.symbol, exc)
                continue
            update = PriceUpdate.from_ticker(ticker, market.metadata.get("market_id", ""))
            if self._executor is not None:
                self._executor.process_price_update(update)
            for strategy in self._strategies:
                await strategy.handle_price_update(update)
            updates.append(update)
        return updates

    async def run(self, iterations: Optional[int] = None) -> None:
        """Poll every `interval` seconds; forever if `iterations` is None."""
        count = 0
        while iterations is None or count < iterations:
            await self.poll_once()
            count += 1
            if iterations is None or count < iterations:
                await asyncio.sleep(self._interval)
