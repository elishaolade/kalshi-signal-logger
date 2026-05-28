#!/usr/bin/env python3
"""
sanity_check.py — Verify premium_no_midrange_scalp/v1 paper trades
against their entry/exit rules.

Checks
------
  01  All trades are NO side
  02  Signal ask price is 0.65–0.80
  03  Signal time_remaining_seconds is 180–300
  04  Signal spread <= 0.03
  05  Directional momentum >= 3  (NO → raw_momentum_score <= −3)
  06  Directional gap z-score >= 2  (NO → raw_gap_z_score <= −2)
  07  Entry price = clamp(ask + entry_slip)  — not bid or mid
  08  Exit price  = clamp(bid − exit_slip)   — not ask or mid
  09  PnL = (exit_price − entry_price) × position_size
  10  No overlapping trades on the same market/side/rule
  11  Every CLOSED trade has a non-null exit_reason
  12  All exit_reasons are from the allowed set

Exit code 0 when all checks pass, 1 when any fail.
Handles zero PNMS trades gracefully (reports 0 checked, all pass).

Usage:
    python scripts/sanity_check.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all

# ── Strategy constants (must match strategies.py and paper_trader.py) ──────────

RULE_NAME    = "premium_no_midrange_scalp"
RULE_VERSION = "v1"
SIDE         = "NO"

MIN_ASK      = 0.65
MAX_ASK      = 0.80
MIN_TIME_REM = 180
MAX_TIME_REM = 300
MAX_SPREAD   = 0.03
MIN_DIR_MOM  = 3.0   # directional — for NO: raw_mom <= -3
MIN_DIR_GZ   = 2.0   # directional — for NO: raw_gz  <= -2

TP_ABS          = 0.05
SL_ABS          = 0.03
TIMEOUT_S       = 60
NEAR_EXPIRY_S   = 30

VALID_EXIT_REASONS: frozenset[str] = frozenset({
    "take_profit",
    "stop_loss",
    "timeout_60s",
    "near_expiry",
    "recovered_after_restart",   # process restart while trade open
    "market_rollover",           # market changed while trade open
})

# Slippage tolerance for float comparisons (rounding + 1 poll-cycle of drift)
_SLIP_TOL = 0.007

# Maximum violation rows shown per failing check
_MAX_SHOW = 5

# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    number:     int
    name:       str
    passed:     bool
    n_checked:  int
    n_failed:   int
    violations: list[dict[str, Any]] = field(default_factory=list)
    note:       str = ""


# ── Shared query fragments ─────────────────────────────────────────────────────

_RULE_FILTER = f"""
    pt.rule_name    = '{RULE_NAME}'
    AND pt.rule_version = '{RULE_VERSION}'
"""

# ── Individual checks ──────────────────────────────────────────────────────────

def _c01_side_is_no(trades: list[dict]) -> CheckResult:
    """All PNMS trades must be on the NO side."""
    bad = [r for r in trades if r.get("side") != SIDE]
    return CheckResult(
        number    = 1,
        name      = f"Side = {SIDE} only",
        passed    = len(bad) == 0,
        n_checked = len(trades),
        n_failed  = len(bad),
        violations= bad[:_MAX_SHOW],
    )


def _c02_ask_range() -> CheckResult:
    """Signal ask price must be 0.65–0.80 at entry time."""
    rows = fetch_all(f"""
        SELECT
            pt.id          AS trade_id,
            CAST(s.ask_price AS DOUBLE) AS ask_price
        FROM paper_trades pt
        JOIN signals s ON pt.signal_id = s.id
        WHERE {_RULE_FILTER}
          AND NOT (CAST(s.ask_price AS DOUBLE) BETWEEN {MIN_ASK} AND {MAX_ASK})
    """)
    total = fetch_all(f"""
        SELECT COUNT(*) AS n FROM paper_trades pt
        WHERE {_RULE_FILTER}
    """)[0]["n"]
    return CheckResult(
        number    = 2,
        name      = f"Ask price {MIN_ASK}–{MAX_ASK} at entry",
        passed    = len(rows) == 0,
        n_checked = int(total),
        n_failed  = len(rows),
        violations= rows[:_MAX_SHOW],
    )


def _c03_time_remaining() -> CheckResult:
    """Signal time_remaining_seconds must be 180–300."""
    rows = fetch_all(f"""
        SELECT
            pt.id                   AS trade_id,
            s.time_remaining_seconds
        FROM paper_trades pt
        JOIN signals s ON pt.signal_id = s.id
        WHERE {_RULE_FILTER}
          AND NOT (s.time_remaining_seconds BETWEEN {MIN_TIME_REM} AND {MAX_TIME_REM})
    """)
    total = fetch_all(f"""
        SELECT COUNT(*) AS n FROM paper_trades pt
        WHERE {_RULE_FILTER}
    """)[0]["n"]
    return CheckResult(
        number    = 3,
        name      = f"time_remaining {MIN_TIME_REM}–{MAX_TIME_REM}s",
        passed    = len(rows) == 0,
        n_checked = int(total),
        n_failed  = len(rows),
        violations= rows[:_MAX_SHOW],
    )


def _c04_spread() -> CheckResult:
    """Signal spread must be <= 0.03."""
    rows = fetch_all(f"""
        SELECT
            pt.id                  AS trade_id,
            CAST(s.spread AS DOUBLE) AS spread
        FROM paper_trades pt
        JOIN signals s ON pt.signal_id = s.id
        WHERE {_RULE_FILTER}
          AND CAST(s.spread AS DOUBLE) > {MAX_SPREAD}
    """)
    total = fetch_all(f"""
        SELECT COUNT(*) AS n FROM paper_trades pt
        WHERE {_RULE_FILTER}
    """)[0]["n"]
    return CheckResult(
        number    = 4,
        name      = f"Spread <= {MAX_SPREAD}",
        passed    = len(rows) == 0,
        n_checked = int(total),
        n_failed  = len(rows),
        violations= rows[:_MAX_SHOW],
    )


def _c05_directional_momentum() -> CheckResult:
    """
    Directional momentum must be >= 3.
    For NO: directional = -raw_momentum_score, so raw_momentum_score must be <= -3.
    """
    rows = fetch_all(f"""
        SELECT
            pt.id                         AS trade_id,
            CAST(s.momentum_score AS DOUBLE) AS raw_momentum_score,
            CAST(-s.momentum_score AS DOUBLE) AS dir_momentum_score
        FROM paper_trades pt
        JOIN signals s ON pt.signal_id = s.id
        WHERE {_RULE_FILTER}
          AND NOT (CAST(s.momentum_score AS DOUBLE) <= -{MIN_DIR_MOM})
    """)
    total = fetch_all(f"""
        SELECT COUNT(*) AS n FROM paper_trades pt
        WHERE {_RULE_FILTER}
    """)[0]["n"]
    return CheckResult(
        number    = 5,
        name      = f"Directional momentum >= {MIN_DIR_MOM:.0f}  (raw_mom <= -{MIN_DIR_MOM:.0f})",
        passed    = len(rows) == 0,
        n_checked = int(total),
        n_failed  = len(rows),
        violations= rows[:_MAX_SHOW],
    )


def _c06_directional_gap_z() -> CheckResult:
    """
    Directional gap z-score must be >= 2.
    For NO: directional = -raw_gap_z_score, so raw_gap_z_score must be <= -2.
    """
    rows = fetch_all(f"""
        SELECT
            pt.id                          AS trade_id,
            CAST(s.gap_z_score AS DOUBLE)  AS raw_gap_z_score,
            CAST(-s.gap_z_score AS DOUBLE) AS dir_gap_z_score
        FROM paper_trades pt
        JOIN signals s ON pt.signal_id = s.id
        WHERE {_RULE_FILTER}
          AND (
              s.gap_z_score IS NULL
              OR NOT (CAST(s.gap_z_score AS DOUBLE) <= -{MIN_DIR_GZ})
          )
    """)
    total = fetch_all(f"""
        SELECT COUNT(*) AS n FROM paper_trades pt
        WHERE {_RULE_FILTER}
    """)[0]["n"]
    return CheckResult(
        number    = 6,
        name      = f"Directional gap-z >= {MIN_DIR_GZ:.0f}  (raw_gz <= -{MIN_DIR_GZ:.0f})",
        passed    = len(rows) == 0,
        n_checked = int(total),
        n_failed  = len(rows),
        violations= rows[:_MAX_SHOW],
    )


def _c07_entry_uses_ask() -> CheckResult:
    """
    Entry price must be ask + entry_slip (clamped).
    Verified two ways:
      a) entry_price >= ask_price - tolerance  (not below ask)
      b) entry_price != bid_price and != mid_price (contract_price)
    Tolerate rounding and clamping to [0.01, 0.99].
    """
    rows = fetch_all(f"""
        SELECT
            pt.id                                AS trade_id,
            CAST(pt.entry_price  AS DOUBLE)      AS entry_price,
            CAST(s.ask_price     AS DOUBLE)      AS ask_price,
            CAST(s.bid_price     AS DOUBLE)      AS bid_price,
            CAST(s.contract_price AS DOUBLE)     AS mid_price,
            CAST(pt.total_slippage AS DOUBLE)    AS total_slippage,
            -- entry_price should be >= ask (it's ask + slip, possibly clamped)
            CASE
                WHEN CAST(pt.entry_price AS DOUBLE)
                   < CAST(s.ask_price AS DOUBLE) - {_SLIP_TOL}
                THEN 'entry_below_ask'
                WHEN ABS(CAST(pt.entry_price AS DOUBLE)
                   - CAST(s.bid_price AS DOUBLE)) < 0.001
                THEN 'entry_equals_bid'
                WHEN ABS(CAST(pt.entry_price AS DOUBLE)
                   - CAST(s.contract_price AS DOUBLE)) < 0.001
                THEN 'entry_equals_mid'
                ELSE NULL
            END AS violation_reason
        FROM paper_trades pt
        JOIN signals s ON pt.signal_id = s.id
        WHERE {_RULE_FILTER}
    """)
    bad = [r for r in rows if r.get("violation_reason") is not None]
    return CheckResult(
        number    = 7,
        name      = "Entry fill = ask + slip  (not bid or mid)",
        passed    = len(bad) == 0,
        n_checked = len(rows),
        n_failed  = len(bad),
        violations= bad[:_MAX_SHOW],
        note      = "Checks entry >= ask and entry != bid/mid",
    )


def _c08_exit_uses_bid() -> CheckResult:
    """
    Exit price must be bid - exit_slip (clamped) at the moment of exit.
    The last trade_snapshot before exit contains the bid used for the fill.
    Excludes recovered_after_restart and market_rollover (those use entry_price).
    Skips OPEN trades (no exit yet).
    """
    rows = fetch_all(f"""
        SELECT
            pt.id                              AS trade_id,
            pt.exit_reason,
            CAST(pt.exit_price    AS DOUBLE)   AS exit_price,
            CAST(pt.total_slippage AS DOUBLE)  AS total_slippage,
            CAST(ts_last.bid_price AS DOUBLE)  AS last_snapshot_bid,
            CAST(ts_last.ask_price AS DOUBLE)  AS last_snapshot_ask,
            CAST(ts_last.contract_price AS DOUBLE) AS last_snapshot_mid,
            -- expected exit = bid - exit_slip; for symmetric modes exit_slip = total_slip/2
            ROUND(ABS(
                CAST(pt.exit_price AS DOUBLE) -
                LEAST(0.99, GREATEST(0.01,
                    CAST(ts_last.bid_price AS DOUBLE)
                    - CAST(pt.total_slippage AS DOUBLE) / 2
                ))
            ), 5) AS exit_discrepancy
        FROM paper_trades pt
        JOIN (
            SELECT
                paper_trade_id,
                bid_price,
                ask_price,
                contract_price,
                ROW_NUMBER() OVER (
                    PARTITION BY paper_trade_id ORDER BY recorded_at DESC
                ) AS rn
            FROM trade_snapshots
        ) ts_last ON ts_last.paper_trade_id = pt.id AND ts_last.rn = 1
        WHERE {_RULE_FILTER}
          AND pt.exit_price IS NOT NULL
          AND pt.exit_reason NOT IN ('recovered_after_restart', 'market_rollover')
          AND ts_last.bid_price IS NOT NULL
    """)
    bad = [r for r in rows
           if r.get("exit_discrepancy") is not None
           and float(r["exit_discrepancy"]) > _SLIP_TOL]
    return CheckResult(
        number    = 8,
        name      = "Exit fill = bid − slip  (not ask or mid)",
        passed    = len(bad) == 0,
        n_checked = len(rows),
        n_failed  = len(bad),
        violations= bad[:_MAX_SHOW],
        note      = (
            f"Tolerance ±{_SLIP_TOL}. "
            "Excludes recovered_after_restart / market_rollover exits."
        ),
    )


def _c09_pnl_math() -> CheckResult:
    """
    PnL must equal (exit_price − entry_price) × position_size.
    Skips OPEN trades and trades with null PnL.
    """
    rows = fetch_all(f"""
        SELECT
            pt.id                             AS trade_id,
            CAST(pt.pnl          AS DOUBLE)   AS pnl,
            CAST(pt.exit_price   AS DOUBLE)   AS exit_price,
            CAST(pt.entry_price  AS DOUBLE)   AS entry_price,
            CAST(pt.position_size AS DOUBLE)  AS position_size,
            ROUND(ABS(
                CAST(pt.pnl AS DOUBLE)
                - (CAST(pt.exit_price AS DOUBLE) - CAST(pt.entry_price AS DOUBLE))
                  * CAST(pt.position_size AS DOUBLE)
            ), 6) AS pnl_discrepancy
        FROM paper_trades pt
        WHERE {_RULE_FILTER}
          AND pt.pnl IS NOT NULL
          AND pt.exit_price IS NOT NULL
          AND pt.entry_price IS NOT NULL
          AND pt.position_size IS NOT NULL
    """)
    bad = [r for r in rows
           if r.get("pnl_discrepancy") is not None
           and float(r["pnl_discrepancy"]) > 0.0001]
    return CheckResult(
        number    = 9,
        name      = "PnL = (exit − entry) × position_size",
        passed    = len(bad) == 0,
        n_checked = len(rows),
        n_failed  = len(bad),
        violations= bad[:_MAX_SHOW],
    )


def _c10_no_overlapping_trades() -> CheckResult:
    """
    No two PNMS trades should overlap in time for the same market/side.
    Overlap = trade B opens before trade A closes (or A is still open).
    """
    rows = fetch_all(f"""
        SELECT
            a.id                          AS trade_a,
            b.id                          AS trade_b,
            a.market_ticker,
            a.side,
            a.entry_time                  AS a_entry,
            COALESCE(a.exit_time, NOW())  AS a_exit,
            b.entry_time                  AS b_entry,
            COALESCE(b.exit_time, NOW())  AS b_exit
        FROM paper_trades a
        JOIN paper_trades b
          ON  a.market_ticker  = b.market_ticker
          AND a.rule_name      = b.rule_name
          AND a.rule_version   = b.rule_version
          AND a.side           = b.side
          AND a.id             < b.id
          AND a.entry_time     < COALESCE(b.exit_time, NOW())
          AND b.entry_time     < COALESCE(a.exit_time, NOW())
        WHERE a.rule_name    = '{RULE_NAME}'
          AND a.rule_version = '{RULE_VERSION}'
    """)
    total = fetch_all(f"""
        SELECT COUNT(*) AS n FROM paper_trades pt
        WHERE {_RULE_FILTER}
    """)[0]["n"]
    return CheckResult(
        number    = 10,
        name      = "No overlapping trades (same market/side/rule)",
        passed    = len(rows) == 0,
        n_checked = int(total),
        n_failed  = len(rows),
        violations= rows[:_MAX_SHOW],
        note      = "Each row is an overlapping pair (trade_a, trade_b)",
    )


def _c11_closed_has_exit_reason() -> CheckResult:
    """Every CLOSED trade must have a non-null exit_reason."""
    rows = fetch_all(f"""
        SELECT id AS trade_id, status, exit_time
        FROM paper_trades
        WHERE {_RULE_FILTER}
          AND status = 'CLOSED'
          AND exit_reason IS NULL
    """)
    total = fetch_all(f"""
        SELECT COUNT(*) AS n FROM paper_trades pt
        WHERE {_RULE_FILTER} AND status = 'CLOSED'
    """)[0]["n"]
    return CheckResult(
        number    = 11,
        name      = "Every CLOSED trade has exit_reason",
        passed    = len(rows) == 0,
        n_checked = int(total),
        n_failed  = len(rows),
        violations= rows[:_MAX_SHOW],
    )


def _c12_valid_exit_reasons() -> CheckResult:
    """All exit_reasons must be from the allowed set."""
    allowed_list = ", ".join(f"'{r}'" for r in sorted(VALID_EXIT_REASONS))
    rows = fetch_all(f"""
        SELECT id AS trade_id, exit_reason
        FROM paper_trades
        WHERE {_RULE_FILTER}
          AND exit_reason IS NOT NULL
          AND exit_reason NOT IN ({allowed_list})
    """)
    total = fetch_all(f"""
        SELECT COUNT(*) AS n
        FROM paper_trades pt
        WHERE {_RULE_FILTER} AND exit_reason IS NOT NULL
    """)[0]["n"]
    return CheckResult(
        number    = 12,
        name      = "Exit reasons are from allowed set",
        passed    = len(rows) == 0,
        n_checked = int(total),
        n_failed  = len(rows),
        violations= rows[:_MAX_SHOW],
        note      = f"Allowed: {', '.join(sorted(VALID_EXIT_REASONS))}",
    )


# ── Console output ─────────────────────────────────────────────────────────────

_PASS = "✓"
_FAIL = "✗"
_W    = 96


def _fmt_val(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def _print_result(r: CheckResult) -> None:
    icon   = _PASS if r.passed else _FAIL
    status = "PASS" if r.passed else f"FAIL  ({r.n_failed} violation{'s' if r.n_failed != 1 else ''})"
    label  = f"[{r.number:02d}] {r.name}"
    check_info = f"({r.n_checked} checked)"
    print(f"  {icon}  {label:<54}  {check_info:<15}  {status}")

    if r.note:
        print(f"       note: {r.note}")

    for row in r.violations:
        cells = "  ".join(f"{k}={_fmt_val(v)}" for k, v in row.items())
        print(f"       → {cells}")

    if r.n_failed > _MAX_SHOW:
        print(f"       … and {r.n_failed - _MAX_SHOW} more")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    # Pre-flight: how many PNMS trades exist?
    summary = fetch_all(f"""
        SELECT
            COUNT(*)                           AS total,
            SUM(status = 'OPEN')               AS open_count,
            SUM(status = 'CLOSED')             AS closed_count,
            MIN(entry_time)                    AS first_entry,
            MAX(COALESCE(exit_time, entry_time)) AS last_activity
        FROM paper_trades
        WHERE {_RULE_FILTER}
    """)[0]

    total  = int(summary["total"]  or 0)
    n_open = int(summary["open_count"]   or 0)
    n_cls  = int(summary["closed_count"] or 0)

    print(f"\n{'═' * _W}")
    print(f"  Sanity Check — {RULE_NAME}/{RULE_VERSION}")
    print(f"{'═' * _W}")
    print(
        f"  Trades: {total} total  ({n_open} open, {n_cls} closed)"
        + (f"  |  first={summary['first_entry']}  last={summary['last_activity']}"
           if total else "")
    )

    if total == 0:
        print(
            "\n  No trades found — nothing to verify.\n"
            "  Run the main loop and return once trades have been recorded.\n"
        )
        sys.exit(0)

    print()

    # Fetch base trade rows once (used by checks that just need side/id)
    all_trades = fetch_all(f"""
        SELECT id, side, status, entry_time, exit_time, exit_reason
        FROM paper_trades
        WHERE {_RULE_FILTER}
        ORDER BY entry_time
    """)

    checks: list[CheckResult] = [
        _c01_side_is_no(all_trades),
        _c02_ask_range(),
        _c03_time_remaining(),
        _c04_spread(),
        _c05_directional_momentum(),
        _c06_directional_gap_z(),
        _c07_entry_uses_ask(),
        _c08_exit_uses_bid(),
        _c09_pnl_math(),
        _c10_no_overlapping_trades(),
        _c11_closed_has_exit_reason(),
        _c12_valid_exit_reasons(),
    ]

    for r in checks:
        _print_result(r)

    n_passed = sum(1 for r in checks if r.passed)
    n_total  = len(checks)
    overall  = "ALL PASS" if n_passed == n_total else f"{n_passed}/{n_total} passed"

    print(f"\n{'─' * _W}")
    print(f"  Result: {overall}")
    print(f"{'═' * _W}\n")

    if n_passed < n_total:
        sys.exit(1)


if __name__ == "__main__":
    main()
