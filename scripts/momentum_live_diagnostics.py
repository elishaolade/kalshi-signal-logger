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


def _print_notes() -> None:
    print("  Note: projected_profit_cents is the shadow outcome for the completed")
    print("  trade's projected exit path, not the target profit assumed at entry.")
    print("  Values are stored in repo price units where 0.05 = 5 cents, even")
    print("  when some legacy column names still end in '_cents'.")


def _fmt_num(v: Optional[float], nd: int = 4, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{nd}f}" if signed else f"{v:.{nd}f}"


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "n/a"


def _safe_float(v) -> Optional[float]:
    return float(v) if v is not None else None


def _repo_units_to_cents(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return round(float(v) * 100.0, 4)


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
               went_green,
               target_touched, target_touch_count, target_first_touched_at,
               target_total_visible_seconds,
               entry_fill_detected_by, exit_fill_detected_by,
               entry_signal_to_order_ms, entry_order_to_ack_ms,
               entry_ack_to_fill_ms, fill_to_exit_order_ms, exit_signal_to_order_ms,
               entry_slippage_cents, exit_slippage_cents, total_execution_slippage_cents,
               projected_path_pnl_cents, actual_pnl_cents, live_decay_cents,
               entry_price_bucket, spread_bucket_entry, spread_bucket_exit,
               quote_age_bucket_entry, quote_age_bucket_exit, time_remaining_bucket,
               filled_contract_bucket, order_type_bucket,
               phase, exit_signal_at, entry_order_price, entry_order_type,
               entry_order_submitted_at, exit_order_price, exit_order_type,
               exit_order_submitted_at, spread_at_exit, quote_age_at_exit_seconds,
               time_remaining_in_market_seconds,
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


def _table_exists(table: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return bool(row and int(row["n"]) > 0)


def _load_shadow_exits(rows: list[dict]) -> list[dict]:
    if not rows or not _table_exists("momentum_live_shadow_exits"):
        return []
    trade_ids = [int(r["id"]) for r in rows if r.get("id") is not None]
    if not trade_ids:
        return []
    placeholders = ",".join(["%s"] * len(trade_ids))
    return fetch_all(
        f"""
        SELECT id, live_trade_id, ticker, side, shadow_exit_reason,
               triggered_at, age_seconds, exit_price, profit_cents,
               current_profit_cents, max_profit_cents, created_at
        FROM momentum_live_shadow_exits
        WHERE live_trade_id IN ({placeholders})
        ORDER BY triggered_at DESC, id DESC
        """,
        tuple(trade_ids),
    )


def _completed_filled_rows(rows: Iterable[dict]) -> list[dict]:
    return [
        r for r in rows
        if r.get("status") == "COMPLETE"
        and int(r.get("filled_contracts") or 0) > 0
    ]


def _summarize_completed(rows: Iterable[dict]) -> dict[str, Optional[float]]:
    completed = _completed_filled_rows(rows)
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
        "avg_live_decay": statistics.mean(
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


def _entry_price_bucket_cents(price: Optional[float]) -> str:
    if price is None:
        return "unknown"
    cents = float(price) * 100.0
    if cents < 5:
        return "0-5c"
    if cents < 10:
        return "5-10c"
    if cents < 15:
        return "10-15c"
    if cents < 25:
        return "15-25c"
    if cents < 40:
        return "25-40c"
    return "40c+"


def _went_green(row: dict) -> bool:
    return bool(
        int(row.get("went_green") or 0) == 1
        or (_safe_float(row.get("max_profit_cents")) or 0.0) > 0
    )


def _fixed_time_failure_bucket(row: dict) -> str:
    max_profit_cents = _safe_float(row.get("max_profit_cents")) or 0.0
    if not _went_green(row):
        return "never green"
    for threshold in (5, 4, 3, 2, 1):
        if max_profit_cents >= threshold:
            return f"reached +{threshold}c then failed"
    return "went green (<1c) then failed"


def _shadow_exits_by_trade(shadow_rows: Iterable[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in shadow_rows:
        grouped[int(row["live_trade_id"])].append(row)
    for events in grouped.values():
        events.sort(
            key=lambda r: (
                _safe_float(r.get("profit_cents")) if r.get("profit_cents") is not None else float("-inf"),
                str(r.get("triggered_at") or ""),
            ),
            reverse=True,
        )
    return grouped


def _print_completed_summary(rows: list[dict]) -> None:
    summary = _summarize_completed(rows)
    _h2("Completed Live Trades")
    _row("Completed trades:", str(summary["completed"]))
    _row("Wins:", str(summary["wins"]))
    _row("Win rate:", _fmt_pct(summary["win_rate"]))
    _row("Avg actual pnl / contract:", _fmt_num(summary["avg_actual_pnl"], signed=True))
    _row(
        "Avg projected exit-path pnl / contract:",
        _fmt_num(summary["avg_projected_pnl"], signed=True),
    )
    _row("Avg entry drift:", _fmt_num(summary["avg_entry_drift"], signed=True))
    _row("Avg exit drift:", _fmt_num(summary["avg_exit_drift"], signed=True))
    _row("Avg live decay:", _fmt_num(summary["avg_live_decay"], signed=True))
    _row("Avg execution drift:", _fmt_num(summary["avg_exec_drift"], signed=True))
    _row("Avg MFE:", _fmt_num(summary["avg_mfe"], signed=True))


def _print_group_breakdown(rows: list[dict], title: str, key_fn) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in _completed_filled_rows(rows):
        groups[str(key_fn(row))].append(row)

    _h2(title)
    if not groups:
        print("  No completed live trades yet.")
        return

    print("  group".ljust(18) + "n".rjust(4) + "  " +
          "win".rjust(8) + "  " +
          "act pnl".rjust(10) + "  " +
          "proj path".rjust(10) + "  " +
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
            f"{_fmt_num(stats['avg_live_decay'], signed=True):>10}  "
            f"{_fmt_num(stats['avg_exec_drift'], signed=True):>10}  "
            f"{_fmt_num(stats['avg_mfe'], signed=True):>8}"
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
        return

    spreads = [_parse_spread_value(str(g.get("reason") or "")) for g in blocked]
    spreads = [s for s in spreads if s is not None]
    if spreads:
        _row("Avg blocked spread:", _fmt_num(statistics.mean(spreads)))
        _row("Min blocked spread:", _fmt_num(min(spreads)))
        _row("Max blocked spread:", _fmt_num(max(spreads)))
    print("  This section reflects trades blocked by the spread gate.")


def _print_recent_completed(rows: list[dict], limit: int = 10) -> None:
    completed = _completed_filled_rows(rows)[:limit]
    _h2(f"Recent Completed Trades (last {limit})")
    if not completed:
        print("  No completed live trades yet.")
        return
    hdr = (
        "  signal_at            ticker                 sd  "
        "exit_reason     p_entry a_entry p_exit a_exit p_path a_pnl  e_drift"
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
    completed = _completed_filled_rows(rows)
    touched = [r for r in completed if int(r.get("target_touched") or 0) == 1]
    fixed_time_green = [
        r for r in completed
        if str(r.get("exit_reason") or "").lower() == "fixed_time"
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


def _print_fixed_time_audit(rows: list[dict]) -> None:
    completed = _completed_filled_rows(rows)
    fixed = [r for r in completed if str(r.get("exit_reason") or "").lower() == "fixed_time"]
    losers = [r for r in fixed if (_safe_float(r.get("actual_profit_cents")) or 0.0) < 0]

    _h2("Fixed-Time Exit Audit")
    _row("Fixed-time exits:", str(len(fixed)))
    if not fixed:
        print("  No fixed-time exits in this window.")
        return

    avg_pnl = statistics.mean(
        [_safe_float(r.get("actual_profit_cents")) for r in fixed if r.get("actual_profit_cents") is not None]
    ) if any(r.get("actual_profit_cents") is not None for r in fixed) else None
    avg_mfe = statistics.mean(
        [_safe_float(r.get("max_profit_cents")) for r in fixed if r.get("max_profit_cents") is not None]
    ) if any(r.get("max_profit_cents") is not None for r in fixed) else None
    avg_ttmfe = statistics.mean(
        [_safe_float(r.get("time_to_max_bid_seconds")) for r in fixed if r.get("time_to_max_bid_seconds") is not None]
    ) if any(r.get("time_to_max_bid_seconds") is not None for r in fixed) else None
    went_green_count = sum(1 for r in fixed if _went_green(r))

    _row("Average fixed-time pnl:", _fmt_num(avg_pnl, signed=True))
    _row("Went green first:", f"{went_green_count} ({(went_green_count / len(fixed)) * 100:.1f}%)")
    for threshold in (1, 2, 3, 4, 5):
        count = sum(1 for r in fixed if (_safe_float(r.get("max_profit_cents")) or 0.0) >= threshold)
        _row(f"Reached +{threshold}c:", str(count))
    _row("Average max favorable excursion:", _fmt_num(avg_mfe, signed=True))
    _row("Average time to MFE (s):", _fmt_num(avg_ttmfe, nd=2))

    _h2("Fixed-Time Loser Classification")
    if not losers:
        print("  No fixed-time losers in this window.")
        return
    buckets: dict[str, int] = defaultdict(int)
    for row in losers:
        buckets[_fixed_time_failure_bucket(row)] += 1
    for label in (
        "never green",
        "went green (<1c) then failed",
        "reached +1c then failed",
        "reached +2c then failed",
        "reached +3c then failed",
        "reached +4c then failed",
        "reached +5c then failed",
    ):
        _row(label + ":", str(buckets.get(label, 0)))


def _print_ws_stats(rows: list[dict]) -> None:
    completed = _completed_filled_rows(rows)
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


def _print_ws_shadow_exit_audit(rows: list[dict], shadow_rows: list[dict]) -> None:
    _h2("WebSocket Shadow Exit Audit")
    if not shadow_rows:
        print("  No WebSocket shadow-exit data yet.")
        if not _table_exists("momentum_live_shadow_exits"):
            print("  Run scripts/migrate_add_momentum_live_shadow_exits.py, then enable")
            print("  MOMENTUM_LIVE_WS_EXIT_SHADOW=true to start collecting it.")
        return

    completed = {int(r["id"]): r for r in _completed_filled_rows(rows) if r.get("id") is not None}
    grouped = _shadow_exits_by_trade(shadow_rows)
    completed_with_shadow = {
        trade_id: completed[trade_id]
        for trade_id in grouped
        if trade_id in completed
    }

    _row("Completed filled trades with shadow data:", str(len(completed_with_shadow)))
    counts = defaultdict(int)
    for event in shadow_rows:
        counts[str(event.get("shadow_exit_reason") or "unknown")] += 1
    for reason, label in (
        ("ws_shadow_tp2", "TP2 triggered:"),
        ("ws_shadow_tp3", "TP3 triggered:"),
        ("ws_shadow_tp5", "TP5 triggered:"),
        ("ws_shadow_profit_protection", "Profit protection triggered:"),
        ("ws_shadow_giveback", "Giveback triggered:"),
        ("ws_shadow_time_progress", "Time-progress triggered:"),
    ):
        _row(label, str(counts.get(reason, 0)))

    fixed_time_losers = [
        row for row in completed_with_shadow.values()
        if str(row.get("exit_reason") or "").lower() == "fixed_time"
        and (_safe_float(row.get("actual_profit_cents")) or 0.0) < 0
    ]
    _row("Fixed-time losers with shadow data:", str(len(fixed_time_losers)))

    candidate_green_counts = defaultdict(int)
    best_shadow_pnls_cents: list[float] = []
    actual_fixed_time_pnls_cents: list[float] = []
    improvements_cents: list[float] = []
    recent_examples: list[tuple[dict, dict]] = []

    for row in fixed_time_losers:
        trade_id = int(row["id"])
        events = grouped.get(trade_id, [])
        if not events:
            continue
        actual_pnl_cents = _repo_units_to_cents(_safe_float(row.get("actual_profit_cents")))
        if actual_pnl_cents is None:
            continue
        actual_fixed_time_pnls_cents.append(actual_pnl_cents)
        for reason in (
            "ws_shadow_tp2",
            "ws_shadow_tp3",
            "ws_shadow_tp5",
            "ws_shadow_profit_protection",
            "ws_shadow_giveback",
            "ws_shadow_time_progress",
        ):
            event = next((e for e in events if e.get("shadow_exit_reason") == reason), None)
            if not event:
                continue
            profit_cents = _safe_float(event.get("profit_cents"))
            if profit_cents is not None and profit_cents >= 0:
                candidate_green_counts[reason] += 1

        best_event = max(
            events,
            key=lambda e: (
                _safe_float(e.get("profit_cents")) if e.get("profit_cents") is not None else float("-inf"),
                str(e.get("triggered_at") or ""),
            ),
        )
        best_profit_cents = _safe_float(best_event.get("profit_cents"))
        if best_profit_cents is None:
            continue
        best_shadow_pnls_cents.append(best_profit_cents)
        improvements_cents.append(best_profit_cents - actual_pnl_cents)
        recent_examples.append((row, best_event))

    for reason, label in (
        ("ws_shadow_tp2", "Fixed-time losers TP2 green:"),
        ("ws_shadow_tp3", "Fixed-time losers TP3 green:"),
        ("ws_shadow_tp5", "Fixed-time losers TP5 green:"),
        ("ws_shadow_profit_protection", "Fixed-time losers profit-protection green:"),
        ("ws_shadow_giveback", "Fixed-time losers giveback green:"),
        ("ws_shadow_time_progress", "Fixed-time losers time-progress green:"),
    ):
        _row(label, str(candidate_green_counts.get(reason, 0)))

    _row(
        "Avg actual fixed-time loser pnl (c):",
        _fmt_num(statistics.mean(actual_fixed_time_pnls_cents) if actual_fixed_time_pnls_cents else None, signed=True),
    )
    _row(
        "Avg best shadow exit pnl (c):",
        _fmt_num(statistics.mean(best_shadow_pnls_cents) if best_shadow_pnls_cents else None, signed=True),
    )
    _row(
        "Avg improvement vs actual (c):",
        _fmt_num(statistics.mean(improvements_cents) if improvements_cents else None, signed=True),
    )

    print()
    print("  Note: WebSocket shadow exits are simulated decision triggers only.")
    print("  They do not prove fillability unless depth/order-fill simulation is added.")

    _h2("WebSocket Shadow Exit Examples")
    if not recent_examples:
        print("  No fixed-time loser examples with shadow data yet.")
        return
    recent_examples.sort(
        key=lambda pair: str(pair[0].get("signal_at") or ""),
        reverse=True,
    )
    hdr = (
        "  signal_at            ticker                 sd  actual_reason    "
        "actual(c)  best_shadow_reason               best(c) improve(c)"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for row, best_event in recent_examples[:10]:
        actual_pnl_cents = _repo_units_to_cents(_safe_float(row.get("actual_profit_cents")))
        best_profit_cents = _safe_float(best_event.get("profit_cents"))
        improvement = (
            None if actual_pnl_cents is None or best_profit_cents is None
            else best_profit_cents - actual_pnl_cents
        )
        print(
            f"  {str(row['signal_at'])[:19]:<19}  "
            f"{str(row['market_ticker'])[:21]:<21}  "
            f"{str(row['side']):<2}  "
            f"{str(row.get('exit_reason') or '')[:14]:<14}  "
            f"{_fmt_num(actual_pnl_cents, 2, signed=True):>9}  "
            f"{str(best_event.get('shadow_exit_reason') or '')[:31]:<31}  "
            f"{_fmt_num(best_profit_cents, 2, signed=True):>7}  "
            f"{_fmt_num(improvement, 2, signed=True):>10}"
        )


def _column_present(rows: list[dict], col: str) -> bool:
    """True if at least one row has the column (even if all values are None)."""
    return any(col in r for r in rows)


def _print_live_vs_shadow_summary(rows: list[dict]) -> None:
    """Section 1: Live vs Shadow Execution Summary (true-cents slippage fields)."""
    _h2("Live vs Shadow Execution Summary (true-cents slippage)")
    completed = _completed_filled_rows(rows)
    if not completed:
        print("  No completed filled trades yet.")
        return

    if not _column_present(completed, "entry_slippage_cents"):
        print("  entry_slippage_cents column not found — run")
        print("  scripts/migrate_add_momentum_live_telemetry.py to add telemetry fields.")
        return

    def _vals(key: str) -> list[float]:
        return [float(r[key]) for r in completed if r.get(key) is not None]

    def _avg(lst: list[float]) -> Optional[float]:
        return round(statistics.mean(lst), 4) if lst else None

    def _med(lst: list[float]) -> Optional[float]:
        return round(statistics.median(lst), 4) if lst else None

    entry_slip = _vals("entry_slippage_cents")
    exit_slip  = _vals("exit_slippage_cents")
    total_slip = _vals("total_execution_slippage_cents")
    proj_path  = _vals("projected_path_pnl_cents")
    act_pnl    = _vals("actual_pnl_cents")
    decay      = _vals("live_decay_cents")

    _row("Completed filled trades:", str(len(completed)))
    _row("Avg entry slippage (cents):", _fmt_num(_avg(entry_slip), signed=True))
    _row("Median entry slippage (cents):", _fmt_num(_med(entry_slip), signed=True))
    _row("Avg exit slippage (cents):", _fmt_num(_avg(exit_slip), signed=True))
    _row("Median exit slippage (cents):", _fmt_num(_med(exit_slip), signed=True))
    _row("Avg total execution slippage (cents):", _fmt_num(_avg(total_slip), signed=True))
    _row("Avg projected path P/L (cents):", _fmt_num(_avg(proj_path), signed=True))
    _row("Avg actual P/L no-fee (cents):", _fmt_num(_avg(act_pnl), signed=True))
    _row("Avg live decay (cents):", _fmt_num(_avg(decay), signed=True))
    print()
    print("  Note: slippage fields are in TRUE cents (1.0 = 1¢).")
    print("  Positive entry slippage = live paid more than shadow.")
    print("  Positive exit slippage  = live received less than shadow.")
    print("  Positive live decay     = live underperformed the shadow path.")


def _print_bucket_performance(rows: list[dict]) -> None:
    """Section 2: Bucket Performance grouped by key classification dimensions."""
    _h2("Bucket Performance")
    completed = _completed_filled_rows(rows)
    if not completed:
        print("  No completed filled trades yet.")
        return

    if not _column_present(completed, "entry_price_bucket"):
        print("  Bucket columns not found — run scripts/migrate_add_momentum_live_telemetry.py.")
        return

    bucket_dims = [
        ("side",                "Side"),
        ("entry_price_bucket",  "Entry Price"),
        ("spread_bucket_entry", "Spread @ Entry"),
        ("quote_age_bucket_entry", "Quote Age @ Entry"),
        ("exit_reason",         "Exit Reason"),
        ("time_remaining_bucket", "Time Remaining"),
        ("filled_contract_bucket", "Fill Status"),
    ]

    for col, label in bucket_dims:
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in completed:
            key = str(r.get(col) or "unknown")
            groups[key].append(r)
        if not groups:
            continue

        print(f"\n  Grouped by {label}:")
        hdr = ("    group".ljust(20) + "n".rjust(4) + "  " +
               "proj_path".rjust(9) + "  " +
               "act_pnl".rjust(8) + "  " +
               "decay".rjust(8) + "  " +
               "e_slip".rjust(7) + "  " +
               "x_slip".rjust(7) + "  " +
               "win%".rjust(6) + "  " +
               "ft%".rjust(5))
        print(hdr)
        print("    " + "-" * 76)
        for grp in sorted(groups):
            chunk = groups[grp]
            n = len(chunk)
            proj_vals = [float(r["projected_path_pnl_cents"]) for r in chunk if r.get("projected_path_pnl_cents") is not None]
            act_vals  = [float(r["actual_pnl_cents"]) for r in chunk if r.get("actual_pnl_cents") is not None]
            dec_vals  = [float(r["live_decay_cents"]) for r in chunk if r.get("live_decay_cents") is not None]
            es_vals   = [float(r["entry_slippage_cents"]) for r in chunk if r.get("entry_slippage_cents") is not None]
            xs_vals   = [float(r["exit_slippage_cents"]) for r in chunk if r.get("exit_slippage_cents") is not None]
            wins = sum(1 for r in chunk if (_safe_float(r.get("actual_pnl_cents")) or 0.0) > 0)
            ft   = sum(1 for r in chunk if str(r.get("exit_reason") or "").lower() == "fixed_time")
            avg = lambda lst: round(statistics.mean(lst), 2) if lst else None
            print(
                f"    {grp:<20}{n:>4}  "
                f"{_fmt_num(avg(proj_vals), 2, signed=True):>9}  "
                f"{_fmt_num(avg(act_vals), 2, signed=True):>8}  "
                f"{_fmt_num(avg(dec_vals), 2, signed=True):>8}  "
                f"{_fmt_num(avg(es_vals), 2, signed=True):>7}  "
                f"{_fmt_num(avg(xs_vals), 2, signed=True):>7}  "
                f"{_fmt_pct(wins/n if n else None):>6}  "
                f"{_fmt_pct(ft/n if n else None):>5}"
            )


def _print_trade_lifecycle_samples(rows: list[dict], limit: int = 5) -> None:
    """Section 3: Recent completed trade samples with full slippage/decay breakdown."""
    _h2(f"Trade Lifecycle Samples (most recent {limit} completed)")
    completed = _completed_filled_rows(rows)[:limit]
    if not completed:
        print("  No completed filled trades yet.")
        return

    for r in completed:
        tid = r.get("id", "?")
        sig = str(r.get("signal_at", ""))[:19]
        side = r.get("side", "?")
        ideal_e = _safe_float(r.get("projected_entry_ask"))
        actual_e = _safe_float(r.get("actual_entry_price"))
        ideal_x = _safe_float(r.get("projected_exit_bid"))
        actual_x = _safe_float(r.get("actual_exit_price"))
        e_slip = _safe_float(r.get("entry_slippage_cents"))
        x_slip = _safe_float(r.get("exit_slippage_cents"))
        xreason = r.get("exit_reason", "?")
        act_pnl = _safe_float(r.get("actual_pnl_cents"))
        decay = _safe_float(r.get("live_decay_cents"))
        phase = r.get("phase", "n/a")
        status = r.get("status", "?")
        print(
            f"  trade_id={tid}  signal={sig}  side={side}  "
            f"phase={phase}  status={status}"
        )
        print(
            f"    ideal_entry={_fmt_num(ideal_e, 4)}  actual_entry={_fmt_num(actual_e, 4)}  "
            f"entry_slip={_fmt_num(e_slip, 2, signed=True)}c"
        )
        print(
            f"    ideal_exit={_fmt_num(ideal_x, 4)}   actual_exit={_fmt_num(actual_x, 4)}   "
            f"exit_slip={_fmt_num(x_slip, 2, signed=True)}c"
        )
        print(
            f"    exit_reason={xreason}  actual_pnl={_fmt_num(act_pnl, 2, signed=True)}c  "
            f"live_decay={_fmt_num(decay, 2, signed=True)}c"
        )
        print()


def _print_missing_telemetry_audit(rows: list[dict]) -> None:
    """Section 4: Missing Telemetry Audit — fields that are null/missing."""
    _h2("Missing Telemetry Audit")
    completed = _completed_filled_rows(rows)
    n = len(completed)
    if n == 0:
        print("  No completed filled trades yet.")
        return

    audit_fields = [
        ("projected_entry_ask",         "ideal_entry_ask (projected_entry_ask)"),
        ("actual_entry_price",          "actual_entry_price"),
        ("projected_exit_bid",          "ideal_exit_bid (projected_exit_bid)"),
        ("actual_exit_price",           "actual_exit_price"),
        ("ws_spread_at_entry",          "spread_at_entry"),
        ("spread_at_exit",              "spread_at_exit"),
        ("ws_quote_age_at_entry",       "quote_age_at_entry_seconds"),
        ("quote_age_at_exit_seconds",   "quote_age_at_exit_seconds"),
        ("entry_slippage_cents",        "entry_slippage_cents"),
        ("exit_slippage_cents",         "exit_slippage_cents"),
        ("max_profit_cents",            "max_profit_cents"),
        ("target_touched",              "target_touched"),
        ("projected_path_pnl_cents",    "projected_path_pnl_cents"),
        ("actual_pnl_cents",            "actual_pnl_cents (no-fee path)"),
        ("live_decay_cents",            "live_decay_cents"),
        ("entry_price_bucket",          "entry_price_bucket"),
        ("phase",                       "phase"),
    ]

    _row("Completed filled trades:", str(n))
    print()
    print(f"  {'field':<42} {'null_count':>10}  {'null_pct':>8}")
    print("  " + "-" * 64)
    for col, label in audit_fields:
        null_count = sum(1 for r in completed if r.get(col) is None)
        null_pct = null_count / n if n else 0.0
        marker = "  ← needs migration?" if null_count == n and col not in ("spread_at_exit", "quote_age_at_exit_seconds") else ""
        print(f"  {label:<42} {null_count:>10}  {_fmt_pct(null_pct):>8}{marker}")

    print()
    print("  Fields with 100% nulls on old rows are expected before the telemetry")
    print("  migration ran.  New trades should populate these fields automatically.")


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
    shadow_rows = _load_shadow_exits(rows)

    label = (
        f"Momentum LIVE Diagnostics — Last {args.hours}h"
        if args.hours is not None else
        "Momentum LIVE Diagnostics — All History"
    )
    _h1(label)
    _row("Rows loaded:", str(len(rows)))
    _row("Guardrail rows loaded:", str(len(guardrails)))
    print()
    _print_notes()

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
        lambda r: _entry_price_bucket_cents(_safe_float(r.get("projected_entry_ask"))),
    )
    _print_canceled_entries(rows, cutoffs)
    _print_fixed_time_audit(rows)
    _print_target_touch(rows)
    _print_ws_shadow_exit_audit(rows, shadow_rows)
    _print_ws_stats(rows)
    _print_spread_guardrails(guardrails)
    _print_recent_completed(rows)

    # ── New live-vs-shadow execution telemetry sections ───────────────────────
    _print_live_vs_shadow_summary(rows)
    _print_bucket_performance(rows)
    _print_trade_lifecycle_samples(rows)
    _print_missing_telemetry_audit(rows)
    print()


if __name__ == "__main__":
    main()
