"""Exercise the read-only PredictionsCupVenue: markets, tickers, positions, balance."""

from __future__ import annotations

import argparse
import asyncio
import sys

from predictions_cup import ConfigError, PredictionsCupAPIError
from predictions_cup.venues import PredictionsCupVenue


async def run(limit: int, tournament_slug: str | None) -> None:
    venue = PredictionsCupVenue(tournament_slug=tournament_slug)
    await venue.connect()
    try:
        caps = venue.get_capabilities()
        scope = venue.tournament.slug if venue.tournament else "API key default scope"
        print(f"{venue!r}  scope={scope}  currency={caps.settlement_currency}\n")

        markets = await venue.get_markets()
        active = [m for m in markets if m.is_active]
        print(f"{len(markets)} tradable contracts ({len(active)} active). First {limit} active:")
        for m in active[:limit]:
            ticker = await venue.get_ticker(m.symbol)
            print(
                f"  {m.symbol:<6} bid={ticker.bid:.3f} ask={ticker.ask:.3f} "
                f"spread={ticker.spread:.3f}  {m.description}"
            )

        positions = await venue.get_positions()
        print(f"\nOpen positions: {len(positions)}")
        for p in positions:
            print(f"  {p.symbol:<6} {p.side:<5} size={p.size:g} entry={p.entry_price:.3f} uPnL={p.unrealized_pnl:.2f}")

        balance = await venue.get_balance()
        print(f"\nBalance: {balance.total:,.2f} {balance.currency}")
    finally:
        await venue.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5, help="number of active contracts to show")
    parser.add_argument("--tournament", default=None, help="tournament slug (overrides PREDICTIONS_CUP_TOURNAMENT_SLUG)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.limit, args.tournament))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except PredictionsCupAPIError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
