#!/usr/bin/env python3
"""
momentum_shadow_report.py — Recent performance report for live momentum shadow trades.

Reads from:
  momentum_shadow_trades — one row per live shadow trade (ACTIVE + COMPLETE)

Purpose:
  Provide a quick operating view of the frozen ht120s_tp5c shadow strategy:
    - recent summary (last N hours)
    - ACTIVE vs COMPLETE counts
    - side split
    - exit-reason split
    - hourly buckets
    - breadth (markets / signal minutes)
    - worst fixed-time losers

Usage:
    python scripts/momentum_shadow_report.py
    python scripts/momentum_shadow_report.py --hours 8
    python scripts/momentum_shadow_report.py --hours 24 --worst-timeouts 20
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all, fetch_one


def _sep(char: str = "─", width: int = 86) -> None:
    print(char * width)


def _h1(title: str) -> None:
    print()
    _sep("═")
    print(f"  {title}")
    _sep("═")


def _h2(title: str) -> None:
    print()
    _sep()
    print(f"  {title}")
    _sep()


def _row(label: str, value: str, width: int = 34) -> None:
    print(f"  {label:<{width}} {value}")


def _f4(v: Optional[float]) -> str:
    return f"{float(v):.4f}" if v is not None else "n/a"


def _pct(v: Optional[float]) -> str:
    return f"{float(v) * 100:.1f}%" if v is not None else "n/a"


def _money(v: Optional[float]) -> str:
    return f"{float(v):+.4f}" if v is not None else "n/a"


def _int(v: Optional[int]) -> str:
    return str(int(v or 0))


def _summary(hours: int) -> None:
    row = fetch_one(
        """
        SELECT
          COUNT(*) AS completed_trades,
          AVG(net_pnl_cents) AS avg_net_cents,
          AVG(net_pnl_cents > 0) AS win_rate_pct,
          SUM(net_pnl_cents) AS total_net_cents,
          SUM(CASE WHEN net_pnl_cents > 0 THEN net_pnl_cents ELSE 0 END) /
            NULLIF(ABS(SUM(CASE WHEN net_pnl_cents < 0 THEN net_pnl_cents ELSE 0 END)), 0)
            AS profit_factor
        FROM momentum_shadow_trades
        WHERE status = 'COMPLETE'
          AND signal_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR
        """,
        (hours,),
    )

    active = fetch_one(
        """
        SELECT
          COUNT(*) AS total_shadow_rows,
          SUM(status = 'ACTIVE') AS active_rows,
          SUM(status = 'COMPLETE') AS complete_rows
        FROM momentum_shadow_trades
        """,
        (),
    )

    _h1(f"Momentum Shadow Report — Last {hours} Hours")
    _row("Completed trades:", _int(row["completed_trades"]))
    _row("Average net / trade:", _money(row["avg_net_cents"]))
    _row("Win rate:", _pct(row["win_rate_pct"]))
    _row("Total net:", _money(row["total_net_cents"]))
    _row("Profit factor:", _f4(row["profit_factor"]))
    print()
    _row("Total shadow rows:", _int(active["total_shadow_rows"]))
    _row("Currently ACTIVE:", _int(active["active_rows"]))
    _row("Currently COMPLETE:", _int(active["complete_rows"]))


def _print_table(title: str, rows: list[dict], columns: list[tuple[str, str]]) -> None:
    _h2(title)
    if not rows:
        print("  No rows.")
        return
    widths = []
    for key, label in columns:
        max_data = max(len(str(r.get(key, ""))) for r in rows)
        widths.append(max(max_data, len(label)))
    header = "  " + "  ".join(label.ljust(width) for width, (_, label) in zip(widths, columns))
    print(header)
    print("  " + "  ".join("-" * width for width in widths))
    for r in rows:
        print("  " + "  ".join(str(r.get(key, "")).ljust(width) for width, (key, _) in zip(widths, columns)))


def _side_split(hours: int) -> None:
    rows = fetch_all(
        """
        SELECT
          side,
          COUNT(*) AS trades,
          ROUND(AVG(net_pnl_cents), 4) AS avg_net_cents,
          ROUND(AVG(net_pnl_cents > 0) * 100, 1) AS win_rate_pct,
          ROUND(SUM(net_pnl_cents), 4) AS total_net_cents,
          ROUND(
            SUM(CASE WHEN net_pnl_cents > 0 THEN net_pnl_cents ELSE 0 END) /
            NULLIF(ABS(SUM(CASE WHEN net_pnl_cents < 0 THEN net_pnl_cents ELSE 0 END)), 0),
            2
          ) AS profit_factor
        FROM momentum_shadow_trades
        WHERE status = 'COMPLETE'
          AND signal_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR
        GROUP BY side
        ORDER BY side
        """,
        (hours,),
    )
    _print_table(
        "By Side",
        rows,
        [
            ("side", "side"),
            ("trades", "trades"),
            ("avg_net_cents", "avg_net"),
            ("win_rate_pct", "win_rate_pct"),
            ("total_net_cents", "total_net"),
            ("profit_factor", "pf"),
        ],
    )


def _exit_split(hours: int) -> None:
    rows = fetch_all(
        """
        SELECT
          exit_reason,
          COUNT(*) AS trades,
          ROUND(AVG(net_pnl_cents), 4) AS avg_net_cents,
          ROUND(AVG(net_pnl_cents > 0) * 100, 1) AS win_rate_pct,
          ROUND(SUM(net_pnl_cents), 4) AS total_net_cents
        FROM momentum_shadow_trades
        WHERE status = 'COMPLETE'
          AND signal_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR
        GROUP BY exit_reason
        ORDER BY trades DESC, exit_reason
        """,
        (hours,),
    )
    _print_table(
        "By Exit Reason",
        rows,
        [
            ("exit_reason", "exit_reason"),
            ("trades", "trades"),
            ("avg_net_cents", "avg_net"),
            ("win_rate_pct", "win_rate_pct"),
            ("total_net_cents", "total_net"),
        ],
    )


def _breadth(hours: int) -> None:
    row = fetch_one(
        """
        SELECT
          COUNT(*) AS trades,
          COUNT(DISTINCT market_ticker) AS distinct_markets,
          COUNT(DISTINCT DATE_FORMAT(signal_at, '%%Y-%%m-%%d %%H:%%i')) AS distinct_signal_minutes
        FROM momentum_shadow_trades
        WHERE status = 'COMPLETE'
          AND signal_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR
        """,
        (hours,),
    )
    _h2("Breadth")
    _row("Trades:", _int(row["trades"]))
    _row("Distinct markets:", _int(row["distinct_markets"]))
    _row("Distinct signal minutes:", _int(row["distinct_signal_minutes"]))


def _hourly(hours: int) -> None:
    rows = fetch_all(
        """
        SELECT
          DATE_FORMAT(signal_at, '%%Y-%%m-%%d %%H:00:00') AS hour_bucket,
          COUNT(*) AS trades,
          ROUND(AVG(net_pnl_cents), 4) AS avg_net_cents,
          ROUND(AVG(net_pnl_cents > 0) * 100, 1) AS win_rate_pct,
          ROUND(SUM(net_pnl_cents), 4) AS total_net_cents
        FROM momentum_shadow_trades
        WHERE status = 'COMPLETE'
          AND signal_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR
        GROUP BY hour_bucket
        ORDER BY hour_bucket
        """,
        (hours,),
    )
    _print_table(
        "By Hour",
        rows,
        [
            ("hour_bucket", "hour_bucket"),
            ("trades", "trades"),
            ("avg_net_cents", "avg_net"),
            ("win_rate_pct", "win_rate_pct"),
            ("total_net_cents", "total_net"),
        ],
    )


def _worst_timeouts(hours: int, limit: int) -> None:
    rows = fetch_all(
        """
        SELECT
          signal_at,
          market_ticker,
          side,
          entry_ask,
          exit_bid,
          holding_seconds,
          net_pnl_cents,
          btc_move_toward_side,
          contract_move_during_lookback,
          entry_spread
        FROM momentum_shadow_trades
        WHERE status = 'COMPLETE'
          AND signal_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR
          AND exit_reason = 'fixed_time'
          AND net_pnl_cents < 0
        ORDER BY net_pnl_cents ASC, signal_at DESC
        LIMIT %s
        """,
        (hours, limit),
    )
    _print_table(
        f"Worst Fixed-Time Losers (top {limit})",
        rows,
        [
            ("signal_at", "signal_at"),
            ("market_ticker", "market_ticker"),
            ("side", "side"),
            ("entry_ask", "entry_ask"),
            ("exit_bid", "exit_bid"),
            ("holding_seconds", "hold_s"),
            ("net_pnl_cents", "net"),
            ("btc_move_toward_side", "btc_move"),
            ("contract_move_during_lookback", "contract_move"),
            ("entry_spread", "spread"),
        ],
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Recent report for momentum_shadow_trades")
    ap.add_argument("--hours", type=int, default=24, help="Recent window in hours (default: 24)")
    ap.add_argument(
        "--worst-timeouts",
        type=int,
        default=15,
        help="How many worst fixed-time losers to show (default: 15)",
    )
    args = ap.parse_args()

    _summary(args.hours)
    _side_split(args.hours)
    _exit_split(args.hours)
    _breadth(args.hours)
    _hourly(args.hours)
    _worst_timeouts(args.hours, args.worst_timeouts)


if __name__ == "__main__":
    main()
