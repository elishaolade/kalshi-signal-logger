#!/usr/bin/env python3
"""
momentum_live_report.py — Live-vs-shadow performance report for the frozen
ht120s_tp5c momentum strategy.

Reads:
    momentum_live_trades            — real-money trades (projected + actual)
    momentum_live_guardrail_events  — risk-gate / pause / kill events
    momentum_live_pause_state       — current pause latch

Shows:
    - total live trades + status breakdown
    - projected vs actual: win rate, expectancy, profit factor, avg profit
      (overall and over rolling windows of the last 25 / 50 / 100 trades)
    - recent row-level projected-vs-actual comparison rows
    - recent guardrail / pause events
    - current pause state

Admin:
    --unpause "review note"   clear the automatic pause latch (manual review)

Usage:
    python scripts/momentum_live_report.py
    python scripts/momentum_live_report.py --recent 30
    python scripts/momentum_live_report.py --unpause "reviewed drift, resuming"
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_all, fetch_one

_WINDOWS = (25, 50, 100)


# ── formatting ────────────────────────────────────────────────────────────────

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


def _pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "n/a"


def _money(v: Optional[float]) -> str:
    return f"{v:+.4f}" if v is not None else "n/a"


def _num(v: Optional[float], nd: int = 3) -> str:
    return f"{v:.{nd}f}" if v is not None else "n/a"


# ── stats helpers (mirror momentum_live_trader.summarize_pnls) ────────────────

def _summarize(pnls: list[float]) -> dict[str, Optional[float]]:
    n = len(pnls)
    if n == 0:
        return {"n": 0, "win_rate": None, "expectancy": None,
                "profit_factor": None, "avg": None}
    wins = [p for p in pnls if p > 1e-9]
    losses = [p for p in pnls if p < -1e-9]
    gl = abs(sum(losses))
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "expectancy": statistics.mean(pnls),
        "avg": statistics.mean(pnls),
        "profit_factor": (sum(wins) / gl) if gl > 0 else None,
    }


def _pair_line(label: str, proj: Optional[float], act: Optional[float], fmt) -> None:
    delta = (act - proj) if (proj is not None and act is not None) else None
    print(f"  {label:<24} projected={fmt(proj):>10}   actual={fmt(act):>10}   "
          f"delta={fmt(delta) if delta is not None else 'n/a':>10}")


# ── sections ──────────────────────────────────────────────────────────────────

def _overview() -> None:
    counts = fetch_all(
        "SELECT status, COUNT(*) AS n FROM momentum_live_trades GROUP BY status"
    )
    total = sum(int(c["n"]) for c in counts)
    _h1("Momentum LIVE Trading Report")
    _row("Total live trades:", str(total))
    for c in sorted(counts, key=lambda r: r["status"]):
        _row(f"  status {c['status']}:", str(int(c["n"])))


def _pause_state() -> None:
    row = fetch_one("SELECT * FROM momentum_live_pause_state WHERE id=1")
    _h2("Pause State")
    if not row:
        print("  No pause-state row (run the live migration).")
        return
    if int(row["is_paused"]) == 1:
        _row("Status:", "PAUSED ⛔")
        _row("Reason:", str(row.get("reason") or ""))
        _row("Paused at:", str(row.get("paused_at") or ""))
        print("\n  To resume after manual review:")
        print('    python scripts/momentum_live_report.py --unpause "your note"')
    else:
        _row("Status:", "active (not paused)")
        if row.get("unpaused_at"):
            _row("Last unpaused at:", str(row["unpaused_at"]))


def _projected_vs_actual_overall() -> None:
    rows = fetch_all(
        """
        SELECT projected_profit_cents, actual_profit_cents
        FROM momentum_live_trades
        WHERE status='COMPLETE'
        """
    )
    proj = [float(r["projected_profit_cents"]) for r in rows
            if r["projected_profit_cents"] is not None]
    act = [float(r["actual_profit_cents"]) for r in rows
           if r["actual_profit_cents"] is not None]
    ps, as_ = _summarize(proj), _summarize(act)

    _h2("Projected vs Actual — All Completed Live Trades")
    _row("Paired completed trades:", str(as_["n"]))
    _pair_line("Win rate", ps["win_rate"], as_["win_rate"], _pct)
    _pair_line("Expectancy / contract", ps["expectancy"], as_["expectancy"], _money)
    _pair_line("Avg profit / contract", ps["avg"], as_["avg"], _money)
    _pair_line("Profit factor", ps["profit_factor"], as_["profit_factor"],
               lambda v: _num(v, 3))


def _projected_vs_actual_windows() -> None:
    max_w = max(_WINDOWS)
    rows = fetch_all(
        """
        SELECT projected_profit_cents, actual_profit_cents
        FROM momentum_live_trades
        WHERE status='COMPLETE'
        ORDER BY signal_at DESC
        LIMIT %s
        """,
        (max_w,),
    )
    _h2("Projected vs Actual — Rolling Windows (most recent N)")
    print(f"  {'window':>7}  {'n':>4}  {'win proj/act':>18}  "
          f"{'exp proj/act':>20}  {'pf proj/act':>18}")
    print("  " + "-" * 74)
    for w in _WINDOWS:
        chunk = rows[:w]
        proj = [float(r["projected_profit_cents"]) for r in chunk
                if r["projected_profit_cents"] is not None]
        act = [float(r["actual_profit_cents"]) for r in chunk
               if r["actual_profit_cents"] is not None]
        ps, as_ = _summarize(proj), _summarize(act)
        win = f"{_pct(ps['win_rate'])}/{_pct(as_['win_rate'])}"
        exp = f"{_money(ps['expectancy'])}/{_money(as_['expectancy'])}"
        pf = f"{_num(ps['profit_factor'])}/{_num(as_['profit_factor'])}"
        print(f"  {w:>7}  {as_['n']:>4}  {win:>18}  {exp:>20}  {pf:>18}")


def _recent_rows(limit: int) -> None:
    rows = fetch_all(
        """
        SELECT signal_at, market_ticker, side, filled_contracts,
               projected_entry_ask, actual_entry_price,
               projected_exit_bid, actual_exit_price,
               projected_profit_cents, actual_profit_cents,
               profit_delta_cents, total_execution_drift_cents,
               profit_capture_ratio, exit_reason, status
        FROM momentum_live_trades
        ORDER BY signal_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    _h2(f"Recent Trades — Row-Level Projected vs Actual (last {limit})")
    if not rows:
        print("  No live trades yet.")
        return
    hdr = ("  signal_at            ticker                 sd  qty  "
           "p_entry a_entry  p_exit a_exit   p_pnl    a_pnl   delta  cap   status")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        print(
            f"  {str(r['signal_at'])[:19]:<19}  "
            f"{str(r['market_ticker'])[:21]:<21}  "
            f"{r['side']:<2}  "
            f"{_i(r['filled_contracts']):>3}  "
            f"{_c(r['projected_entry_ask'])}  {_c(r['actual_entry_price'])}  "
            f"{_c(r['projected_exit_bid'])}  {_c(r['actual_exit_price'])}  "
            f"{_c(r['projected_profit_cents'])} {_c(r['actual_profit_cents'])} "
            f"{_c(r['profit_delta_cents'])} {_cap(r['profit_capture_ratio'])} "
            f"{str(r['status'])[:11]}"
        )


def _guardrails(limit: int) -> None:
    rows = fetch_all(
        """
        SELECT created_at, event_type, market_ticker, side, reason
        FROM momentum_live_guardrail_events
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    _h2(f"Recent Guardrail / Pause Events (last {limit})")
    if not rows:
        print("  No guardrail events.")
        return
    for r in rows:
        tkr = f" {r['market_ticker']}" if r.get("market_ticker") else ""
        print(f"  {str(r['created_at'])[:19]}  {r['event_type']:<22}{tkr}  "
              f"{str(r.get('reason') or '')[:60]}")


def _i(v) -> int:
    return int(v or 0)


def _c(v) -> str:
    """Compact dollar-fraction cell."""
    return f"{float(v):+.3f}" if v is not None else "  n/a "


def _cap(v) -> str:
    return f"{float(v):.2f}" if v is not None else " n/a"


# ── unpause admin ─────────────────────────────────────────────────────────────

def _unpause(note: str) -> None:
    row = fetch_one("SELECT is_paused, reason FROM momentum_live_pause_state WHERE id=1")
    if not row:
        print("No pause-state row found. Run scripts/migrate_add_momentum_live.py first.")
        return
    if int(row["is_paused"]) == 0:
        print("Live trading is not currently paused — nothing to do.")
        return
    now = datetime.now(timezone.utc)
    execute_query(
        "UPDATE momentum_live_pause_state SET is_paused=0, "
        "reason=%s, unpaused_at=%s WHERE id=1",
        (f"unpaused: {note}", now),
    )
    execute_query(
        "INSERT INTO momentum_live_guardrail_events (event_type, reason) "
        "VALUES ('unpaused', %s)",
        (f"manual unpause: {note}",),
    )
    print(f"Live trading UNPAUSED at {now.isoformat()} — note: {note}")
    print(f"(was paused for: {row.get('reason')})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live-vs-shadow momentum report")
    ap.add_argument("--recent", type=int, default=20,
                    help="how many recent trade rows to show (default 20)")
    ap.add_argument("--events", type=int, default=15,
                    help="how many recent guardrail events to show (default 15)")
    ap.add_argument("--unpause", type=str, default=None, metavar="NOTE",
                    help="clear the automatic pause latch with a review note")
    args = ap.parse_args()

    if args.unpause is not None:
        _unpause(args.unpause)
        return

    _overview()
    _pause_state()
    _projected_vs_actual_overall()
    _projected_vs_actual_windows()
    _recent_rows(args.recent)
    _guardrails(args.events)
    print()


if __name__ == "__main__":
    main()
