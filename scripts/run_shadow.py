"""Run a strategy in shadow (paper) mode against live Predictions Cup order books.

Nothing is ever submitted to the exchange: the executor runs in shadow mode and
the venue is read-only.

Examples:
  python scripts/run_shadow.py --strategy spread_farm --watchlist 1075,1077 --iterations 30
  python scripts/run_shadow.py --strategy spread_farm --search Senate --max 8 --set max_spread=0.08
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Dict, List

from predictions_cup import ConfigError, PredictionsCupAPIError
from predictions_cup.execution import Executor, PassiveFillRule
from predictions_cup.risk import RiskManager
from predictions_cup.scanner import PriceScanner
from predictions_cup.strategies import STRATEGIES
from predictions_cup.venues import Market, PredictionsCupVenue


def parse_overrides(pairs: List[str]) -> Dict[str, str]:
    overrides = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        overrides[key.strip()] = value.strip()
    return overrides


def select_markets(markets: List[Market], args: argparse.Namespace) -> List[Market]:
    active = [m for m in markets if m.is_active]
    if args.watchlist:
        wanted = [s.strip() for s in args.watchlist.split(",") if s.strip()]
        by_symbol = {m.symbol: m for m in active}
        missing = [s for s in wanted if s not in by_symbol]
        if missing:
            logging.warning("Not found or not active in this tournament: %s", ", ".join(missing))
        selected = [by_symbol[s] for s in wanted if s in by_symbol]
    elif args.search:
        needle = args.search.lower()
        selected = [m for m in active if needle in (m.description or "").lower()]
    elif args.all:
        selected = active
    else:
        raise SystemExit("Choose markets with --watchlist, --search, or --all")
    return selected[: args.max]


async def run(args: argparse.Namespace) -> None:
    venue = PredictionsCupVenue(tournament_slug=args.tournament)
    await venue.connect()

    strategy = STRATEGIES[args.strategy]()
    if args.set:
        strategy.configure(**parse_overrides(args.set))

    executor = Executor(
        venue,
        RiskManager(),
        shadow=True,
        passive_fill=PassiveFillRule(args.fill),
        resting_ttl_sec=args.resting_ttl,
    )
    executor.attach(strategy)

    try:
        markets = select_markets(await venue.get_markets(), args)
        if not markets:
            raise SystemExit("No markets selected")

        scope = venue.tournament.slug if venue.tournament else "API key default scope"
        logging.info("Strategy %s, scope %s, config %s", strategy.name, scope, strategy.config)
        logging.info(
            "Watching %d contracts every %.0fs (fill rule: %s, resting TTL %.0fs):",
            len(markets), args.interval, args.fill, args.resting_ttl,
        )
        for m in markets:
            logging.info("  %s  %s", m.symbol, m.description)

        await strategy.start()
        scanner = PriceScanner(venue, markets, [strategy], executor=executor, interval=args.interval)
        await scanner.run(iterations=args.iterations)
    finally:
        await strategy.stop()
        await venue.disconnect()
        print_summary(executor, strategy)


def print_summary(executor: Executor, strategy) -> None:
    tracker = executor.shadow_tracker
    print("\n=== Shadow summary ===")
    stats = tracker.get_stats(strategy.name) or {}
    for key in ("total_trades", "total_realized_pnl", "total_unrealized_pnl", "total_pnl", "win_rate", "open_positions"):
        print(f"  {key:<22} {stats.get(key, 0)}")
    trades = list(reversed(tracker.get_trades(strategy.name, limit=1000)))
    if trades:
        print("  Trades:")
        for t in trades:
            print(f"    {t['timestamp'][11:19]}  {t['side']:<4} {t['exchange_id']:<6} {t['size']:.2f} @ {t['simulated_fill_price']:.3f}")
    positions = tracker.get_positions(strategy.name)
    if positions:
        print("  Open positions:")
        for p in positions:
            print(f"    {p['side']:<4} {p['exchange_id']:<6} {p['size']:.2f} @ {p['entry_price']:.3f}  uPnL={p['unrealized_pnl']:.2f}")
    resting = executor.resting_orders()
    if resting:
        print(f"  Resting orders at exit: {len(resting)}")
    print(f"  Risk: {executor.risk_manager.get_risk_status()}")
    print(f"  Strategy: {strategy.get_stats()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    group = parser.add_argument_group("market selection")
    group.add_argument("--watchlist", help="comma-separated exchange IDs (see scripts/inspect_venue.py)")
    group.add_argument("--search", help="case-insensitive substring of the market title")
    group.add_argument("--all", action="store_true", help="every active contract (mind the rate limits)")
    group.add_argument("--max", type=int, default=10, help="cap on contracts watched (default 10)")
    parser.add_argument("--tournament", default=None, help="tournament slug (default: PREDICTIONS_CUP_TOURNAMENT_SLUG)")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between polls (default 10)")
    parser.add_argument("--iterations", type=int, default=None, help="number of polls (default: until Ctrl+C)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a strategy config field")
    parser.add_argument("--fill", choices=[r.value for r in PassiveFillRule], default="through", help="resting-order fill rule")
    parser.add_argument("--resting-ttl", type=float, default=30.0, help="seconds before a resting order is cancelled")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except PredictionsCupAPIError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
