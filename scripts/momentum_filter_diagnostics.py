#!/usr/bin/env python3
"""
momentum_filter_diagnostics.py — Does a candidate PRE-ENTRY or FIRST-30-SECOND
filter materially reduce stop_loss trades WITHOUT eliminating most profit_target
winners?

This report is read-only.  It scores candidate filters against ALREADY-recorded
``momentum_live_trades`` rows (live 1-contract diagnostic trades and/or
MOMENTUM_WS_SHADOW_ONLY hypothetical trades).  It never changes strategy
behaviour and never places an order.  All candidate logic lives in the pure,
unit-tested module ``app/momentum_filter_shadow.py``.

Sections
--------
  1. Baseline performance
  2. Pre-entry filter shadow impact (+ stop_loss_reduction% / pt_retention%)
  3. First-30-second early-exit shadow impact
  4. Confusion-matrix-style output (precision / recall / retention)
  5. Missing-telemetry audit

Units
-----
All P/L values are TRUE cents (3.0 = 3 cents).  See app/momentum_filter_shadow.

Usage
-----
    python scripts/momentum_filter_diagnostics.py
    python scripts/momentum_filter_diagnostics.py --hours 48
    python scripts/momentum_filter_diagnostics.py --source shadow_only
    python scripts/momentum_filter_diagnostics.py --source live --min-sample 30
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all
from app import momentum_filter_shadow as fs

# ── Outcomes we can classify against ──────────────────────────────────────────
_CLASSIFIED = (fs.OUTCOME_PROFIT_TARGET, fs.OUTCOME_STOP_LOSS, fs.OUTCOME_FIXED_TIME)


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


def _row(label: str, value: str, width: int = 36) -> None:
    print(f"  {label:<{width}} {value}")


def _fmt(v: Optional[float], nd: int = 2, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{nd}f}" if signed else f"{v:.{nd}f}"


def _fmt_pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.1f}%"


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_rows(hours: Optional[int], source: str) -> list[dict]:
    clauses = ["status = 'COMPLETE'", "exit_reason IS NOT NULL"]
    params: list = []
    if hours is not None:
        clauses.append("signal_at >= %s")
        params.append(datetime.now(timezone.utc) - timedelta(hours=hours))
    if source == "shadow_only":
        clauses.append("shadow_only = 1")
    elif source == "live":
        clauses.append("(shadow_only IS NULL OR shadow_only = 0)")
    where = " AND ".join(clauses)
    try:
        return fetch_all(
            f"SELECT * FROM momentum_live_trades WHERE {where} ORDER BY signal_at DESC",
            tuple(params),
        )
    except Exception as exc:
        # shadow_only column may not exist yet (migration not run): retry without it.
        if source != "all" and "shadow_only" in str(exc):
            print("  (shadow_only column missing — run "
                  "migrate_add_momentum_filter_diagnostics.py; showing all rows)")
            return _load_rows(hours, "all")
        raise


# ── Section 1: baseline ───────────────────────────────────────────────────────

def _print_baseline(trades: list[dict]) -> None:
    _h2("1. Baseline Performance")
    b = fs.baseline_performance(trades)
    _row("Total trades:", str(b["total_trades"]))
    _row("profit_target:", f"{b['profit_target']}  ({_fmt_pct(b['profit_target_rate'])})")
    _row("stop_loss:", f"{b['stop_loss']}  ({_fmt_pct(b['stop_loss_rate'])})")
    _row("fixed_time:", f"{b['fixed_time']}  ({_fmt_pct(b['fixed_time_rate'])})")
    _row("Win rate:", _fmt_pct((b["win_rate"] or 0) * 100 if b["win_rate"] is not None else None))
    _row("Avg win (c):", _fmt(b["avg_win_cents"], signed=True))
    _row("Avg loss (c):", _fmt(b["avg_loss_cents"], signed=True))
    _row("Profit factor:", _fmt(b["profit_factor"]))
    _row("Total net (c):", _fmt(b["total_net_cents"], signed=True))
    _row("Avg net / trade (c):", _fmt(b["avg_net_cents"], signed=True))


# ── Section 2: pre-entry filter impact ────────────────────────────────────────

def _print_pre_entry(trades: list[dict], min_sample: int) -> None:
    _h2("2. Pre-Entry Filter Shadow Impact")
    print("  A filter is PROMISING when it cuts stop_loss >= 25% AND retains")
    print("  profit_target >= 75% on a non-tiny sample.  '*' marks promising.")
    print()
    hdr = ("  candidate         thresh        alw  blk   blkSL blkPT blkFT   "
           "alwNet  alwPF  alwWR    SLred%  PTret%")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for cand in fs.default_pre_entry_candidates():
        s = fs.summarize_pre_entry(trades, cand)
        promising = fs.is_promising(s, min_sample=min_sample)
        alw, blk = s["allowed"], s["blocked"]
        print(
            f"  {cand.name:<17} {cand.threshold_label:<12} "
            f"{alw['n']:>3}  {blk['n']:>3}   "
            f"{blk['stop_loss']:>5} {blk['profit_target']:>5} {blk['fixed_time']:>5}   "
            f"{_fmt(alw['net_cents'], 1, signed=True):>6}  "
            f"{_fmt(s['allowed_profit_factor'], 2):>5}  "
            f"{_fmt_pct((s['allowed_win_rate'] or 0)*100 if s['allowed_win_rate'] is not None else None):>6}   "
            f"{_fmt_pct(s['stop_loss_reduction_pct']):>6}  "
            f"{_fmt_pct(s['profit_target_retention_pct']):>6}"
            f"{'  *' if promising else ''}"
        )
    print()
    print("  alw=allowed blk=blocked blkSL/PT/FT=blocked stop_loss/profit_target/fixed_time")
    print("  alwNet=allowed net (c)  alwPF=allowed profit factor  alwWR=allowed win rate")
    print("  SLred%=stop_loss_reduction_pct  PTret%=profit_target_retention_pct")


# ── Section 3: first-30s early-exit impact ────────────────────────────────────

def _print_early_exit(trades: list[dict], min_sample: int) -> None:
    _h2("3. First-30-Second Early-Exit Shadow Impact")
    hdr = ("  candidate            thresh          trig  SLavd PTcut FTaff   "
           "simPnl  actPnl  netImp   SLred%  PTdmg%  PTret%")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for cand in fs.default_early_exit_candidates():
        s = fs.summarize_early_exit(trades, cand)
        note = "  (missing telemetry)" if s["triggered"] == 0 and s["undecided"] > 0 else ""
        print(
            f"  {cand.name:<20} {cand.threshold_label:<14}  "
            f"{s['triggered']:>4}  {s['stop_loss_avoided']:>5} {s['profit_target_cut']:>5} "
            f"{s['fixed_time_affected']:>5}   "
            f"{_fmt(s['avg_simulated_pnl_cents'], 2, signed=True):>6}  "
            f"{_fmt(s['avg_actual_pnl_cents'], 2, signed=True):>6}  "
            f"{_fmt(s['net_improvement_cents'], 1, signed=True):>6}   "
            f"{_fmt_pct(s['stop_loss_reduction_pct']):>6}  "
            f"{_fmt_pct(s['profit_target_damage_pct']):>6}  "
            f"{_fmt_pct(s['profit_target_retention_pct']):>6}{note}"
        )
    print()
    print("  trig=triggered SLavd=stop_loss avoided PTcut=profit_target cut early")
    print("  FTaff=fixed_time affected simPnl/actPnl=avg simulated/actual P/L on triggered (c)")
    print("  netImp=sum(simulated-actual) on triggered (c)  PTdmg%=profit_target damage")
    print("  no_progress_by_45s needs pnl_at_45s (not captured yet) — shown as undecided.")


# ── Section 4: confusion matrix ───────────────────────────────────────────────

def _print_confusion(trades: list[dict]) -> None:
    _h2("4. Confusion-Matrix-Style Output")
    print("  Over profit_target / stop_loss trades only.  'acted' = filter blocked")
    print("  (pre-entry) or exited early (first-30s).")
    print("    TP=acted on stop_loss  FP=acted on profit_target")
    print("    TN=allowed profit_target  FN=allowed stop_loss")
    print()
    hdr = ("  candidate            thresh           TP   FP   TN   FN  undec   "
           "prec   recall  PTret%")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    all_cands = fs.default_pre_entry_candidates() + fs.default_early_exit_candidates()
    for cand in all_cands:
        cm = fs.confusion_matrix(trades, cand)
        print(
            f"  {cand.name:<20} {cand.threshold_label:<14}  "
            f"{cm['true_positive']:>3}  {cm['false_positive']:>3}  "
            f"{cm['true_negative']:>3}  {cm['false_negative']:>3}  "
            f"{cm['undecided']:>5}   "
            f"{_fmt_pct(cm['precision_stop_loss_avoidance']):>6} "
            f"{_fmt_pct(cm['recall_stop_loss_avoidance']):>6}  "
            f"{_fmt_pct(cm['profit_target_retention_pct']):>6}"
        )
    print()
    print("  prec=precision (stop-loss avoidance) recall=recall (== stop_loss_reduction)")


# ── Section 5: missing telemetry audit ────────────────────────────────────────

def _print_missing(trades: list[dict], raw_rows: list[dict]) -> None:
    _h2("5. Missing-Telemetry Audit")
    n = len(trades)
    _row("Classified trades (PT/SL/FT):", str(n))
    if n == 0:
        print("  No classified trades yet — run in shadow-only or 1-contract diagnostic")
        print("  mode to accumulate data, then re-run this report.")
        return

    # (normalized-key, label) — counted as missing when None on the normalized row.
    fields = [
        ("ws_entry_ask_at_signal_raw", "WS entry ask at signal"),
        ("ws_spread_cents",            "WS spread at signal"),
        ("ws_quote_age_ms",            "WS quote age at signal"),
        ("tte_seconds",                "time-to-expiry at signal"),
        ("entry_ask_gap_cents",        "entry_ask_gap_cents"),
        ("pnl_at_5s_cents",            "pnl_at_5s"),
        ("pnl_at_10s_cents",           "pnl_at_10s"),
        ("pnl_at_15s_cents",           "pnl_at_15s"),
        ("pnl_at_20s_cents",           "pnl_at_20s"),
        ("pnl_at_30s_cents",           "pnl_at_30s"),
        ("pnl_at_45s_cents",           "pnl_at_45s (NOT captured yet)"),
        ("max_profit_first_30s_cents", "max profit first 30s"),
        ("min_profit_first_30s_cents", "min profit first 30s"),
        ("net_cents",                  "actual final P/L"),
        ("outcome",                    "actual exit reason"),
    ]
    print()
    print(f"  {'field':<34} {'missing':>8}  {'missing%':>9}")
    print("  " + "-" * 56)
    # ws_entry_ask_at_signal_raw isn't a normalized key; count from raw rows.
    raw_by_id = {r.get("id"): r for r in raw_rows}
    for key, label in fields:
        if key == "ws_entry_ask_at_signal_raw":
            miss = sum(
                1 for t in trades
                if raw_by_id.get(t.get("id"), {}).get("ws_entry_ask_at_signal") is None
            )
        else:
            miss = sum(1 for t in trades if t.get(key) is None)
        print(f"  {label:<34} {miss:>8}  {100.0*miss/n:>8.1f}%")

    print()
    print("  BTC continuation (price 1s/3s/5s after signal) is NOT recorded anywhere")
    print("  in the current pipeline — candidate F cannot be evaluated without new")
    print("  telemetry, and is intentionally NOT fabricated here.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=None,
                    help="restrict to trades whose signal_at is within the last N hours")
    ap.add_argument("--source", choices=("all", "live", "shadow_only"), default="all",
                    help="which recorded trades to score (default: all)")
    ap.add_argument("--min-sample", type=int, default=fs.DEFAULT_MIN_SAMPLE,
                    help="min PT+SL sample before a candidate can be flagged promising")
    args = ap.parse_args()

    raw_rows = _load_rows(args.hours, args.source)
    # normalize_trade carries the row id through for the missing-telemetry audit.
    trades = [
        fs.normalize_trade(r) for r in raw_rows
        if r.get("exit_reason") in _CLASSIFIED
    ]

    label = "Momentum FILTER Diagnostics"
    if args.hours is not None:
        label += f" — last {args.hours}h"
    label += f" — source={args.source}"
    _h1(label)
    _row("Rows loaded (COMPLETE):", str(len(raw_rows)))
    _row("Classified (PT/SL/FT):", str(len(trades)))

    _print_baseline(trades)
    _print_pre_entry(trades, args.min_sample)
    _print_early_exit(trades, args.min_sample)
    _print_confusion(trades)
    _print_missing(trades, raw_rows)
    print()


if __name__ == "__main__":
    main()
