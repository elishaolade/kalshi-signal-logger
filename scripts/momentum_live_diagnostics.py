#!/usr/bin/env python3
"""
momentum_live_diagnostics.py — Focused diagnostics for live momentum trades.

This report is meant for the "refine before giving up" stage. It emphasizes:
  - completed live trade quality vs the projected (shadow/backtest-style) path
  - side and exit-reason breakdowns
  - entry-price buckets (cheap vs mid/high-priced contracts)
  - canceled-entry behavior
  - spread-related blockers

Usage:
    python scripts/momentum_live_diagnostics.py
    python scripts/momentum_live_diagnostics.py --hours 24
    python scripts/momentum_live_diagnostics.py --bucket-cutoffs 0.10,0.25,0.50
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all, fetch_one


def _sep(char: str = "─", width: int = 100) -> None:
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


def _fmt_num(v: Optional[float], nd: int = 4, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{nd}f}" if signed else f"{v:.{nd}f}"


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "n/a"


def _safe_float(v) -> Optional[float]:
    return float(v) if v is not None else None


def _build_time_filter(hours: Optional[int]) -> tuple[str, tuple]:
    if hours is None:
        return "", ()
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    return " WHERE signal_at >= %s", (start,)


def _load_live_rows(hours: Optional[int]) -> list[dict]:
    where_sql, params = _build_time_filter(hours)
    return fetch_all(
        f"""
        SELECT id, signal_at, market_ticker, side, status, exit_reason,
               requested_contracts, filled_contracts, exit_filled_contracts,
               projected_entry_ask, actual_entry_price,
               projected_exit_bid, actual_exit_price,
               projected_profit_cents, actual_profit_cents,
               projected_expectancy_cents,
               actual_profit_dollars, actual_trade_won,
               ws_enabled, ws_quote_age_at_entry, ws_spread_at_entry,
               ws_entry_best_bid, ws_entry_best_ask, ws_exit_best_bid,
               ws_exit_spread, ws_avg_spread_during_trade,
               ws_max_quote_age_during_trade,
               max_bid_after_entry, max_profit_cents, time_to_max_bid_seconds,
               seconds_above_1c, seconds_above_2c, seconds_above_3c,
               seconds_above_4c, seconds_above_5c,
               bid_at_30s, bid_at_60s, bid_at_90s, bid_at_120s,
               target_touched, target_touch_count, target_first_touched_at,
               target_total_visible_seconds,
               entry_fill_detected_by, exit_fill_detected_by,
               entry_signal_to_order_ms, entry_order_to_ack_ms,
               entry_ack_to_fill_ms, fill_to_exit_order_ms, exit_signal_to_order_ms,
               entry_price_drift_cents, exit_price_drift_cents,
               profit_delta_cents, total_execution_drift_cents,
               profit_capture_ratio, expectancy_capture_ratio
        FROM momentum_live_trades
        {where_sql}
        ORDER BY signal_at DESC
        """,
        params,
    )


def _load_guardrails(hours: Optional[int]) -> list[dict]:
    if hours is None:
        return fetch_all(
            """
            SELECT created_at, event_type, market_ticker, side, reason
            FROM momentum_live_guardrail_events
            ORDER BY created_at DESC
            """
        )
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    return fetch_all(
        """
        SELECT created_at, event_type, market_ticker, side, reason
        FROM momentum_live_guardrail_events
        WHERE created_at >= %s
        ORDER BY created_at DESC
        """,
        (start,),
    )


def _summarize_completed(rows: Iterable[dict]) -> dict[str, Optional[float]]:
    completed = [r for r in rows if r.get("status") == "COMPLETE"]
    pnls = [_safe_float(r.get("actual_profit_cents")) for r in completed]
    pnls = [p for p in pnls if p is not None]
    projected = [_safe_float(r.get("projected_profit_cents")) for r in completed]
    projected = [p for p in projected if p is not None]
    wins = [
        r for r in completed
        if _safe_float(r.get("actual_profit_cents")) is not None
        and _safe_float(r.get("actual_profit_cents")) > 0
    ]
    return {
        "completed": len(completed),
        "wins": len(wins),
        "win_rate": (len(wins) / len(completed)) if completed else None,
        "avg_actual_pnl": statistics.mean(pnls) if pnls else None,
        "avg_projected_pnl": statistics.mean(projected) if projected else None,
        "avg_entry_drift": statistics.mean(
            [_safe_float(r.get("entry_price_drift_cents")) for r in completed if r.get("entry_price_drift_cents") is not None]
        ) if any(r.get("entry_price_drift_cents") is not None for r in completed) else None,
        "avg_exit_drift": statistics.mean(
            [_safe_float(r.get("exit_price_drift_cents")) for r in completed if r.get("exit_price_drift_cents") is not None]
        ) if any(r.get("exit_price_drift_cents") is not None for r in completed) else None,
        "avg_profit_delta": statistics.mean(
            [_safe_float(r.get("profit_delta_cents")) for r in completed if r.get("profit_delta_cents") is not None]
        ) if any(r.get("profit_delta_cents") is not None for r in completed) else None,
        "avg_exec_drift": statistics.mean(
            [_safe_float(r.get("total_execution_drift_cents")) for r in completed if r.get("total_execution_drift_cents") is not None]
        ) if any(r.get("total_execution_drift_cents") is not None for r in completed) else None,
        "avg_mfe": statistics.mean(
            [_safe_float(r.get("max_profit_cents")) for r in completed if r.get("max_profit_cents") is not None]
        ) if any(r.get("max_profit_cents") is not None for r in completed) else None,
    }


def _bucket_label(price: Optional[float], cutoffs: list[float]) -> str:
    if price is None:
        return "unknown"
    prev = 0.0
    for cutoff in cutoffs:
        if price < cutoff:
            return f"[{prev:.2f},{cutoff:.2f})"
        prev = cutoff
    return f"[{cutoffs[-1]:.2f},1.00]"


def _print_completed_summary(rows: list[dict]) -> None:
    summary = _summarize_completed(rows)
    _h2("Completed Live Trades")
    _row("Completed trades:", str(summary["completed"]))
    _row("Wins:", str(summary["wins"]))
    _row("Win rate:", _fmt_pct(summary["win_rate"]))
    _row("Avg actual pnl / contract:", _fmt_num(summary["avg_actual_pnl"], signed=True))
    _row("Avg projected pnl / contract:", _fmt_num(summary["avg_projected_pnl"], signed=True))
    _row("Avg entry drift:", _fmt_num(summary["avg_entry_drift"], signed=True))
    _row("Avg exit drift:", _fmt_num(summary["avg_exit_drift"], signed=True))
    _row("Avg profit delta:", _fmt_num(summary["avg_profit_delta"], signed=True))
    _row("Avg execution drift:", _fmt_num(summary["avg_exec_drift"], signed=True))
    _row("Avg MFE:", _fmt_num(summary["avg_mfe"], signed=True))


def _print_group_breakdown(rows: list[dict], title: str, key_fn) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("status") == "COMPLETE":
            groups[str(key_fn(row))].append(row)

    _h2(title)
    if not groups:
        print("  No completed live trades yet.")
        return

    print("  group".ljust(18) + "n".rjust(4) + "  " +
          "win".rjust(8) + "  " +
          "act pnl".rjust(10) + "  " +
          "proj pnl".rjust(10) + "  " +
          "delta".rjust(10) + "  " +
          "exec drift".rjust(10) + "  " +
          "mfe".rjust(8))
    print("  " + "-" * 74)
    for group, chunk in sorted(groups.items()):
        stats = _summarize_completed(chunk)
        print(
            f"  {group:<18}{stats['completed']:>4}  "
            f"{_fmt_pct(stats['win_rate']):>8}  "
            f"{_fmt_num(stats['avg_actual_pnl'], signed=True):>10}  "
            f"{_fmt_num(stats['avg_projected_pnl'], signed=True):>10}  "
            f"{_fmt_num(stats['avg_profit_delta'], signed=True):>10}  "
            f"{_fmt_num(stats['avg_exec_drift'], signed=True):>10}"
        )


def _print_canceled_entries(rows: list[dict], cutoffs: list[float]) -> None:
    canceled = [
        r for r in rows
        if r.get("status") == "CANCELED" and int(r.get("filled_contracts") or 0) == 0
    ]
    rejected = [r for r in rows if r.get("status") == "REJECTED"]
    _h2("Canceled Entry Diagnostics")
    _row("Canceled zero-fill rows:", str(len(canceled)))
    _row("Rejected rows:", str(len(rejected)))
    if not canceled:
        return

    by_side: dict[str, int] = defaultdict(int)
    by_bucket: dict[str, int] = defaultdict(int)
    for row in canceled:
        by_side[str(row.get("side"))] += 1
        by_bucket[_bucket_label(_safe_float(row.get("projected_entry_ask")), cutoffs)] += 1

    print("\n  By side")
    for side, n in sorted(by_side.items()):
        print(f"    {side:<4} {n}")

    print("\n  By projected entry bucket")
    for bucket, n in sorted(by_bucket.items()):
        print(f"    {bucket:<14} {n}")


def _parse_spread_value(reason: str) -> Optional[float]:
    m = re.search(r"spread\s+([0-9.]+)\s+>", reason or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _print_spread_guardrails(guardrails: list[dict]) -> None:
    blocked = [g for g in guardrails if g.get("event_type") == "blocked_spread"]
    _h2("Spread Guardrails")
    _row("blocked_spread events:", str(len(blocked)))
    if not blocked:
        print("  No blocked spread events in this window.")
        print("  Note: completed live trades do not currently persist entry spread.")
        return

    spreads = [_parse_spread_value(str(g.get("reason") or "")) for g in blocked]
    spreads = [s for s in spreads if s is not None]
    if spreads:
        _row("Avg blocked spread:", _fmt_num(statistics.mean(spreads)))
        _row("Min blocked spread:", _fmt_num(min(spreads)))
        _row("Max blocked spread:", _fmt_num(max(spreads)))
    print("  Note: completed live trades do not currently persist entry spread,")
    print("  so this section only reflects trades blocked by the spread gate.")


def _print_recent_completed(rows: list[dict], limit: int = 10) -> None:
    completed = [r for r in rows if r.get("status") == "COMPLETE"][:limit]
    _h2(f"Recent Completed Trades (last {limit})")
    if not completed:
        print("  No completed live trades yet.")
        return
    hdr = (
        "  signal_at            ticker                 sd  "
        "exit_reason     p_entry a_entry p_exit a_exit p_pnl  a_pnl  e_drift"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in completed:
        print(
            f"  {str(r['signal_at'])[:19]:<19}  "
            f"{str(r['market_ticker'])[:21]:<21}  "
            f"{str(r['side']):<2}  "
            f"{str(r.get('exit_reason') or '')[:13]:<13}  "
            f"{_fmt_num(_safe_float(r.get('projected_entry_ask')), 3, signed=True):>7} "
            f"{_fmt_num(_safe_float(r.get('actual_entry_price')), 3, signed=True):>7} "
            f"{_fmt_num(_safe_float(r.get('projected_exit_bid')), 3, signed=True):>6} "
            f"{_fmt_num(_safe_float(r.get('actual_exit_price')), 3, signed=True):>6} "
            f"{_fmt_num(_safe_float(r.get('projected_profit_cents')), 3, signed=True):>6} "
            f"{_fmt_num(_safe_float(r.get('actual_profit_cents')), 3, signed=True):>6} "
            f"{_fmt_num(_safe_float(r.get('total_execution_drift_cents')), 3, signed=True):>8}  "
            f"{_fmt_num(_safe_float(r.get('max_profit_cents')), 3, signed=True):>8}"
        )


def _print_target_touch(rows: list[dict]) -> None:
    completed = [r for r in rows if r.get("status") == "COMPLETE"]
    touched = [r for r in completed if int(r.get("target_touched") or 0) == 1]
    fixed_time_green = [
        r for r in completed
        if (r.get("exit_reason") == "fixed_time" or r.get("exit_reason") == "fixed_time".upper())
        and _safe_float(r.get("actual_profit_cents")) is not None
        and _safe_float(r.get("actual_profit_cents")) < 0
    ]
    _h2("Target Touch / MFE")
    _row("Completed trades touching target:", str(len(touched)))
    _row("Avg target visible seconds:", _fmt_num(statistics.mean([
        _safe_float(r.get("target_total_visible_seconds")) for r in touched if r.get("target_total_visible_seconds") is not None
    ]) if touched and any(r.get("target_total_visible_seconds") is not None for r in touched) else None, nd=2))
    for cents, field in ((1, "seconds_above_1c"), (2, "seconds_above_2c"), (3, "seconds_above_3c"), (4, "seconds_above_4c"), (5, "seconds_above_5c")):
        count = sum(
            1 for r in fixed_time_green
            if _safe_float(r.get(field)) is not None and _safe_float(r.get(field)) > 0
        )
        _row(f"Fixed-time losers that saw +{cents}c:", str(count))


def _print_ws_stats(rows: list[dict]) -> None:
    completed = [r for r in rows if r.get("status") == "COMPLETE"]
    _h2("WebSocket Quote Stats")
    if not completed:
        print("  No completed live trades yet.")
        return
    for label, key in (
        ("Avg quote age at entry:", "ws_quote_age_at_entry"),
        ("Avg spread at entry:", "ws_spread_at_entry"),
        ("Avg spread during trade:", "ws_avg_spread_during_trade"),
        ("Max quote age during trade:", "ws_max_quote_age_during_trade"),
    ):
        values = [_safe_float(r.get(key)) for r in completed if r.get(key) is not None]
        _row(label, _fmt_num(statistics.mean(values) if values else None, nd=4))


def main() -> None:
    ap = argparse.ArgumentParser(description="Focused diagnostics for live momentum trades")
    ap.add_argument("--hours", type=int, default=None,
                    help="restrict diagnostics to the last N hours")
    ap.add_argument("--bucket-cutoffs", type=str, default="0.10,0.25,0.50",
                    help="comma-separated projected-entry price cutoffs")
    args = ap.parse_args()

    cutoffs = sorted(
        [float(x.strip()) for x in args.bucket_cutoffs.split(",") if x.strip()]
    )
    rows = _load_live_rows(args.hours)
    guardrails = _load_guardrails(args.hours)

    label = (
        f"Momentum LIVE Diagnostics — Last {args.hours}h"
        if args.hours is not None else
        "Momentum LIVE Diagnostics — All History"
    )
    _h1(label)
    _row("Rows loaded:", str(len(rows)))
    _row("Guardrail rows loaded:", str(len(guardrails)))

    _print_completed_summary(rows)
    _print_group_breakdown(rows, "Completed Trades by Side", lambda r: r.get("side") or "unknown")
    _print_group_breakdown(
        rows,
        "Completed Trades by Exit Reason",
        lambda r: r.get("exit_reason") or "unknown",
    )
    _print_group_breakdown(
        rows,
        "Completed Trades by Projected Entry Bucket",
        lambda r: _bucket_label(_safe_float(r.get("projected_entry_ask")), cutoffs),
    )
    _print_canceled_entries(rows, cutoffs)
    _print_target_touch(rows)
    _print_ws_stats(rows)
    _print_spread_guardrails(guardrails)
    _print_recent_completed(rows)
    print()


if __name__ == "__main__":
    main()
