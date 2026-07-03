#!/usr/bin/env python3
"""
migrate_add_momentum_filter_diagnostics.py — Additive telemetry columns that
back the pre-entry / first-30-second FILTER DIAGNOSTICS layer.

Purpose (research milestone)
----------------------------
Prove whether a pre-entry filter or a first-30-second early-exit filter could
materially reduce ``stop_loss`` trades WITHOUT eliminating most ``profit_target``
winners.  This migration only adds nullable columns to ``momentum_live_trades``
so that:

  * live 1-contract diagnostic trades, and
  * MOMENTUM_WS_SHADOW_ONLY hypothetical trades

both record enough signal-time and early-path telemetry for
``scripts/momentum_filter_diagnostics.py`` to evaluate candidate filters in
shadow.  NOTHING here changes strategy behaviour, and creating the columns does
NOT enable live trading.

Additive + safe to re-run: each column is only added when absent, and every
column is nullable so old rows are untouched.

UNITS (read carefully)
----------------------
Fields ending in ``_cents`` on THIS migration store TRUE cents (3.0 = 3 cents),
matching the true-cents convention introduced by
``scripts/migrate_add_momentum_live_telemetry.py``.  They are NOT repo
price-unit fractions.  Example: a +0.03 contract-price move is stored as 3.0.

  Mode / provenance
    diagnostic_mode                    1 when the row came from a forced
                                       1-contract live diagnostic run.
    shadow_only                        1 when the row is a hypothetical
                                       MOMENTUM_WS_SHADOW_ONLY trade (NO real
                                       order was ever placed).

  Pre-entry (signal-time) filter inputs
    ws_entry_ask_at_signal             WS best ask for the traded side at signal
                                       (dollar fraction, 0.074 = 7.4c).
    rest_ideal_entry_ask               REST/logger ideal entry ask at signal
                                       (== projected_entry_ask; dollar fraction).
    entry_ask_gap_cents                (ws_entry_ask_at_signal
                                        - rest_ideal_entry_ask) * 100  [TRUE cents]
    ws_spread_at_signal                WS spread at signal (dollar fraction).
    ws_quote_age_ms_at_signal          WS quote age at signal in MILLISECONDS.
    time_to_expiry_seconds_at_signal   Seconds to market expiry at signal.

  First-30-second early-exit filter inputs (all TRUE cents / seconds)
    pnl_at_5s_cents .. pnl_at_30s_cents  per-contract P/L at 5/10/15/20/30s
                                         after entry fill (bid - entry) * 100.
    max_profit_first_30s_cents           max P/L observed in first 30s.
    min_profit_first_30s_cents           min P/L observed in first 30s.
    time_to_first_green_seconds          first time P/L went > 0.
    time_to_negative_1c_seconds          first time P/L <= -1c.
    time_to_negative_2c_seconds          first time P/L <= -2c.
    time_to_stop_threshold_seconds       first time P/L <= -stop_loss threshold
                                         (MOMENTUM_LIVE_STOP_LOSS_CENTS).

Run after the base + telemetry migrations:
    python scripts/migrate_add_momentum_filter_diagnostics.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "momentum_live_trades"

# (column_name, DDL) — every column nullable, additive only.
_COLUMNS = [
    # ── Mode / provenance ────────────────────────────────────────────────────
    ("diagnostic_mode",                  "TINYINT(1) DEFAULT NULL"),
    ("shadow_only",                      "TINYINT(1) DEFAULT NULL"),

    # ── Pre-entry (signal-time) filter inputs ────────────────────────────────
    ("ws_entry_ask_at_signal",           "DECIMAL(10,4) DEFAULT NULL"),
    ("rest_ideal_entry_ask",             "DECIMAL(10,4) DEFAULT NULL"),
    ("entry_ask_gap_cents",              "DECIMAL(10,4) DEFAULT NULL"),
    ("ws_spread_at_signal",              "DECIMAL(10,4) DEFAULT NULL"),
    ("ws_quote_age_ms_at_signal",        "DECIMAL(12,2) DEFAULT NULL"),
    ("time_to_expiry_seconds_at_signal", "DECIMAL(10,2) DEFAULT NULL"),

    # ── First-30-second early-exit filter inputs (TRUE cents / seconds) ──────
    ("pnl_at_5s_cents",                  "DECIMAL(10,4) DEFAULT NULL"),
    ("pnl_at_10s_cents",                 "DECIMAL(10,4) DEFAULT NULL"),
    ("pnl_at_15s_cents",                 "DECIMAL(10,4) DEFAULT NULL"),
    ("pnl_at_20s_cents",                 "DECIMAL(10,4) DEFAULT NULL"),
    ("pnl_at_30s_cents",                 "DECIMAL(10,4) DEFAULT NULL"),
    ("max_profit_first_30s_cents",       "DECIMAL(10,4) DEFAULT NULL"),
    ("min_profit_first_30s_cents",       "DECIMAL(10,4) DEFAULT NULL"),
    ("time_to_first_green_seconds",      "DECIMAL(10,2) DEFAULT NULL"),
    ("time_to_negative_1c_seconds",      "DECIMAL(10,2) DEFAULT NULL"),
    ("time_to_negative_2c_seconds",      "DECIMAL(10,2) DEFAULT NULL"),
    ("time_to_stop_threshold_seconds",   "DECIMAL(10,2) DEFAULT NULL"),
]


def _column_exists(table: str, column: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )
    return bool(row and int(row["n"]) > 0)


def _table_exists(table: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    print("\nmomentum FILTER DIAGNOSTICS column migration")
    print("=" * 58)
    print()

    if not _table_exists(_TABLE):
        print(f"  {_TABLE} does not exist — run scripts/migrate_add_momentum_live.py first.")
        return

    added = 0
    for col, ddl in _COLUMNS:
        if _column_exists(_TABLE, col):
            print(f"  {col:38s} already existed -- no change")
            continue
        execute_query(f"ALTER TABLE {_TABLE} ADD COLUMN {col} {ddl}")
        print(f"  {col:38s} added")
        added += 1

    print()
    print(f"Migration complete ({added} column(s) added).")
    print()
    print("Unit note: the new *_cents fields store TRUE cents (3.0 = 3 cents),")
    print("matching migrate_add_momentum_live_telemetry.py.  Do NOT mix them with")
    print("the legacy *_drift_cents price-unit fields (0.03 = 3 cents).")
    print()
    print("The normalized momentum_filter_shadow_evaluations table is intentionally")
    print("NOT created here — filter outcomes are computed at report time from the")
    print("columns above (see scripts/momentum_filter_diagnostics.py).  TODO: persist")
    print("them to a table if forward audit history is needed.")


if __name__ == "__main__":
    main()
