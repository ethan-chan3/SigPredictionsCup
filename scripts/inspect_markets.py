"""Fetch and print a few Predictions Cup markets (read-only)."""

from __future__ import annotations

import argparse
import sys

from predictions_cup import ConfigError, PredictionsCupAPIError, PredictionsCupClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5, help="number of markets to print (max 100)")
    parser.add_argument("--status", default=None, help="open | closed | settled | any")
    args = parser.parse_args()

    try:
        with PredictionsCupClient() as client:
            page = client.list_markets(limit=args.limit, status=args.status)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except PredictionsCupAPIError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 2

    if not page.markets:
        print("No markets returned.")
        return 0

    for m in page.markets:
        settles = m.settlement_date.strftime("%Y-%m-%d %H:%M UTC") if m.settlement_date else "-"
        tournaments = ", ".join(c.tournament.slug for c in m.contexts if c.tournament) or "public"
        print(f"[{m.id}] {m.title}")
        print(f"    status={m.status}  settles={settles}  contexts={tournaments}")
        for ex in m.exchanges:
            price = f"{ex.latest_price:.3f}" if ex.latest_price is not None else "-"
            print(f"    exchange {ex.id:<8} option={ex.option or '-':<12} latest={price}")
    print(f"\nShowing {len(page.markets)} of {page.total} markets (has_more={page.has_more}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
