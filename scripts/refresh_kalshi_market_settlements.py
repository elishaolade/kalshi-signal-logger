#!/usr/bin/env python3
"""
Refresh Kalshi BTC market settlement payloads in the local DB.

The main logger follows open markets, so rows in ``markets`` can remain
``status='open'`` with stale raw_payload after they close.  This script fetches
recent closed/settled Kalshi markets from the REST API and updates local market
rows by ticker.  Research queries can then use real API outcome fields from
``markets.raw_payload`` instead of inferring winners from near-close quotes.

Usage:
    python scripts/refresh_kalshi_market_settlements.py
    python scripts/refresh_kalshi_market_settlements.py --days 21
    python scripts/refresh_kalshi_market_settlements.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import KALSHI_BTC_BINARY_SERIES_TICKER
from app.data_feed import _parse_dt, get_kalshi_markets
from app.db import execute_query, fetch_all


_OUTCOME_KEYS = ("result", "settlement_value", "winning_outcome")


def _market_status(raw: dict[str, Any]) -> str:
    status = str(raw.get("status") or "").strip().lower()
    return status or "unknown"


def _settles_at(raw: dict[str, Any]) -> Optional[datetime]:
    for key in ("settle_time", "settles_at", "settlement_time", "settled_at"):
        dt = _parse_dt(raw.get(key))
        if dt is not None:
            return dt
    return None


def _winner(raw: dict[str, Any]) -> Optional[str]:
    for key in _OUTCOME_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip().upper()
        if text in ("YES", "NO"):
            return text
        if text in ("1", "TRUE"):
            return "YES"
        if text in ("0", "FALSE"):
            return "NO"
    return None


def _recent_local_tickers(days: int) -> set[str]:
    rows = fetch_all(
        """
        SELECT market_id
        FROM markets
        WHERE market_id LIKE 'KXBTC15M%%'
          AND closes_at >= %s
          AND closes_at <= %s
        """,
        (
            datetime.now(timezone.utc) - timedelta(days=days),
            datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    return {str(r["market_id"]) for r in rows}


def _fetch_remote_markets(series_ticker: str, statuses: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for status in statuses:
        rows = get_kalshi_markets(series_ticker=series_ticker, status=status)
        for raw in rows:
            ticker = str(raw.get("ticker") or "").strip()
            if ticker:
                out[ticker] = raw
    return out


def refresh_settlements(
    *,
    days: int,
    series_ticker: str,
    statuses: list[str],
    dry_run: bool,
) -> dict[str, int]:
    local = _recent_local_tickers(days)
    remote = _fetch_remote_markets(series_ticker, statuses)

    checked = 0
    matched = 0
    updated = 0
    with_winner = 0

    for ticker, raw in sorted(remote.items()):
        checked += 1
        if ticker not in local:
            continue
        matched += 1
        if _winner(raw):
            with_winner += 1
        if dry_run:
            continue

        close_time = _parse_dt(raw.get("close_time"))
        execute_query(
            """
            UPDATE markets
            SET status=%s,
                raw_payload=%s,
                closes_at=COALESCE(%s, closes_at),
                settles_at=COALESCE(%s, settles_at),
                updated_at=CURRENT_TIMESTAMP
            WHERE market_id=%s
            """,
            (
                _market_status(raw),
                json.dumps(raw, default=str),
                close_time,
                _settles_at(raw),
                ticker,
            ),
        )
        updated += 1

    return {
        "local_recent": len(local),
        "remote_checked": checked,
        "matched": matched,
        "updated": updated,
        "matched_with_winner_field": with_winner,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh local Kalshi BTC 15m markets with closed/settled API payloads."
    )
    parser.add_argument("--days", type=int, default=7, help="Recent local close window to update.")
    parser.add_argument(
        "--series-ticker",
        default=KALSHI_BTC_BINARY_SERIES_TICKER or "KXBTC",
        help="Kalshi series ticker to fetch.",
    )
    parser.add_argument(
        "--status",
        action="append",
        dest="statuses",
        help="Remote status to fetch. May be repeated. Defaults to closed + settled.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and match without updating DB.")
    args = parser.parse_args()

    statuses = args.statuses or ["closed", "settled"]
    result = refresh_settlements(
        days=args.days,
        series_ticker=args.series_ticker,
        statuses=statuses,
        dry_run=args.dry_run,
    )
    print(json.dumps({
        "series_ticker": args.series_ticker,
        "statuses": statuses,
        "days": args.days,
        "dry_run": args.dry_run,
        **result,
    }, indent=2))


if __name__ == "__main__":
    main()
