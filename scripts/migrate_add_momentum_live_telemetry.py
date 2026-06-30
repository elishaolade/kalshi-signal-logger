#!/usr/bin/env python3
"""
migrate_add_momentum_live_telemetry.py — Additive telemetry columns for the
live-vs-shadow execution data layer.

This migration extends momentum_live_trades with fields needed to make each
live trade fully explainable and consumable by a future read-only MCP/watcher:

  Signal/phase fields:
    phase                         — trade lifecycle state (enum-like VARCHAR)
    exit_signal_at                — when the exit rule first fired

  Entry order detail:
    entry_order_price             — the actual limit price submitted for entry
    entry_order_type              — always "limit" for now; explicit for watcher
    entry_order_submitted_at      — wall-clock time the POST was issued

  Exit order detail:
    exit_order_price              — the bid price at first exit submission
    exit_order_type               — always "limit"
    exit_order_submitted_at       — wall-clock time the exit POST was issued
    spread_at_exit                — spread captured at exit submission moment
    quote_age_at_exit_seconds     — quote age at exit submission moment
    time_remaining_in_market_seconds — seconds left in hold window at exit signal

  TRUE-CENTS slippage / decay fields
  -----------------------------------
  IMPORTANT UNIT DISTINCTION:
    Existing *_drift_cents columns (entry_price_drift_cents, etc.) store values
    in repo price-unit fractions where 0.05 = 5 cents.  Despite their "_cents"
    suffix they are NOT literal cents — this is a legacy naming issue.

    The NEW fields below store TRUE CENTS where 5.0 = 5 cents:
      entry_slippage_cents     = (actual_entry_price - projected_entry_ask) * 100
                                 Positive  => live paid MORE than shadow/logger
      exit_slippage_cents      = (projected_exit_bid - actual_exit_price) * 100
                                 Positive  => live received LESS than shadow/logger
      total_execution_slippage_cents = entry_slippage_cents + exit_slippage_cents
      projected_path_pnl_cents = (projected_exit_bid - projected_entry_ask) * 100
                                 Raw shadow path gain, NO fees
      actual_pnl_cents         = (actual_exit_price - actual_entry_price) * 100
                                 Raw live path gain, NO fees
                                 DISTINCT from actual_profit_cents (which includes fees)
      live_decay_cents         = projected_path_pnl_cents - actual_pnl_cents
                                 Positive  => live underperformed the shadow path

  Bucket / classification fields (VARCHAR, populated at finalization):
    entry_price_bucket           spread_bucket_entry   spread_bucket_exit
    quote_age_bucket_entry       quote_age_bucket_exit time_remaining_bucket
    filled_contract_bucket       order_type_bucket

Safe to re-run (each column is only added if absent).
Run after existing live-trade migrations:
    python scripts/migrate_add_momentum_live_telemetry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "momentum_live_trades"

# (column_name, DDL)
_COLUMNS = [
    # ── Phase / signal timing ────────────────────────────────────────────────
    ("phase",                           "VARCHAR(30) DEFAULT NULL"),
    ("exit_signal_at",                  "DATETIME(3) DEFAULT NULL"),

    # ── Entry order detail ────────────────────────────────────────────────────
    ("entry_order_price",               "DECIMAL(10,4) DEFAULT NULL"),
    ("entry_order_type",                "VARCHAR(20) DEFAULT NULL"),
    ("entry_order_submitted_at",        "DATETIME(3) DEFAULT NULL"),

    # ── Exit order detail ────────────────────────────────────────────────────
    ("exit_order_price",                "DECIMAL(10,4) DEFAULT NULL"),
    ("exit_order_type",                 "VARCHAR(20) DEFAULT NULL"),
    ("exit_order_submitted_at",         "DATETIME(3) DEFAULT NULL"),
    ("spread_at_exit",                  "DECIMAL(10,4) DEFAULT NULL"),
    ("quote_age_at_exit_seconds",       "DECIMAL(10,4) DEFAULT NULL"),
    ("time_remaining_in_market_seconds","DECIMAL(10,2) DEFAULT NULL"),

    # ── TRUE-CENTS slippage / decay (see module docstring for unit note) ─────
    ("entry_slippage_cents",            "DECIMAL(10,4) DEFAULT NULL"),
    ("exit_slippage_cents",             "DECIMAL(10,4) DEFAULT NULL"),
    ("total_execution_slippage_cents",  "DECIMAL(10,4) DEFAULT NULL"),
    ("projected_path_pnl_cents",        "DECIMAL(10,4) DEFAULT NULL"),
    ("actual_pnl_cents",                "DECIMAL(10,4) DEFAULT NULL"),
    ("live_decay_cents",                "DECIMAL(10,4) DEFAULT NULL"),

    # ── Bucket / classification fields ───────────────────────────────────────
    ("entry_price_bucket",              "VARCHAR(20) DEFAULT NULL"),
    ("spread_bucket_entry",             "VARCHAR(20) DEFAULT NULL"),
    ("spread_bucket_exit",              "VARCHAR(20) DEFAULT NULL"),
    ("quote_age_bucket_entry",          "VARCHAR(20) DEFAULT NULL"),
    ("quote_age_bucket_exit",           "VARCHAR(20) DEFAULT NULL"),
    ("time_remaining_bucket",           "VARCHAR(20) DEFAULT NULL"),
    ("filled_contract_bucket",          "VARCHAR(20) DEFAULT NULL"),
    ("order_type_bucket",               "VARCHAR(20) DEFAULT NULL"),
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
    print("\nmomentum LIVE telemetry / live-vs-shadow column migration")
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
    print("Unit note: the new *_slippage_cents and *_pnl_cents fields store TRUE cents")
    print("(5.0 = 5 cents).  Existing *_drift_cents fields store price-unit fractions")
    print("(0.05 = 5 cents) — a legacy naming quirk.  Do not mix them.")


if __name__ == "__main__":
    main()
