#!/usr/bin/env python3
"""
fetch_hourly_range_markets.py — Inspect Kalshi BTC hourly range contracts.

Fetches and prints (or dumps JSON of) currently-open BTC hourly range markets
using the same repo-native data-feed layer that will feed HourlyRangeTracker.

This does NOT write anything to the DB and does NOT open trades.
Use it to verify your KALSHI_BTC_RANGE_EVENT_TICKER / SERIES_TICKER settings
and to see what contracts are currently available.

Usage:
    python scripts/fetch_hourly_range_markets.py
    python scripts/fetch_hourly_range_markets.py --series-ticker KXBTCR
    python scripts/fetch_hourly_range_markets.py --event-ticker KXBTC-25060313 --json
    python scripts/fetch_hourly_range_markets.py --status settled
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.data_feed import get_kalshi_btc_hourly_range_markets


def _fmt(v, fmt: str = ".4f") -> str:
    if v is None:
        return "   -  "
    return format(float(v), fmt)


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("No range markets found.")
        return

    header = (
        f"{'close_time':>22}  {'ticker':<28}  "
        f"{'floor':>10}  {'cap':>10}  {'width':>7}  "
        f"{'yes_bid':>8}  {'yes_ask':>8}  {'last':>8}  title"
    )
    print(header)
    print("─" * 140)
    for m in rows:
        ct = m.get("close_time")
        ct_s = ct.isoformat() if ct else "                    -"
        print(
            f"  {ct_s:>20}  {str(m.get('ticker') or '-'):<28}  "
            f"${m.get('floor_strike', 0):>9,.2f}  "
            f"${m.get('cap_strike', 0):>9,.2f}  "
            f"${m.get('band_width', 0):>6,.0f}  "
            f"  {_fmt(m.get('yes_bid')):>6}  "
            f"  {_fmt(m.get('yes_ask')):>6}  "
            f"  {_fmt(m.get('last_price')):>6}  "
            f"{str(m.get('title') or '')[:50]}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch Kalshi BTC hourly range markets for inspection."
    )
    ap.add_argument("--event-ticker",  help="Kalshi event ticker")
    ap.add_argument("--series-ticker", help="Kalshi series ticker")
    ap.add_argument("--status", default="open",
                    help="Market status filter: open | closed | settled (default: open)")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="Dump JSON instead of table")
    args = ap.parse_args()

    try:
        rows = get_kalshi_btc_hourly_range_markets(
            event_ticker=args.event_ticker,
            series_ticker=args.series_ticker,
            status=args.status,
        )
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        print(
            "\nSet one of these in your .env (or pass as a flag):\n"
            "  KALSHI_BTC_RANGE_EVENT_TICKER=<event>\n"
            "  KALSHI_BTC_RANGE_SERIES_TICKER=<series>",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.as_json:
        serializable = []
        for row in rows:
            item = dict(row)
            for key in ("open_time", "close_time"):
                if item.get(key) is not None:
                    item[key] = item[key].isoformat()
            serializable.append(item)
        print(json.dumps(serializable, indent=2, sort_keys=True))
    else:
        print(f"\nFound {len(rows)} BTC hourly range market(s):\n")
        _print_table(rows)
        print()


if __name__ == "__main__":
    main()
