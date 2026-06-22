#!/usr/bin/env python3
"""
migrate_add_momentum_live_exit_progress.py — Idempotent column migration.

Adds cumulative EXIT-progress columns to momentum_live_trades so a partially
exited trade can be reconstructed correctly after a crash/restart without
losing already-realized exit history or corrupting round-trip PnL:

    exit_filled_contracts  INT            cumulative contracts sold so far
    exit_value_cents       DECIMAL(18,6)  cumulative sum(exit_price * count)
    exit_fees_total        DECIMAL(10,4)  cumulative exit-side fees (dollars)

``filled_contracts`` remains the ORIGINAL entry size; remaining-to-flatten is
``filled_contracts - exit_filled_contracts``.

Safe to re-run: each column is added only if it does not already exist.

    python scripts/migrate_add_momentum_live_exit_progress.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "momentum_live_trades"

# column_name -> column DDL (added after the existing filled_contracts column)
_COLUMNS = [
    ("exit_filled_contracts", "INT NOT NULL DEFAULT 0"),
    ("exit_value_cents",      "DECIMAL(18,6) DEFAULT NULL"),
    ("exit_fees_total",       "DECIMAL(10,4) DEFAULT NULL"),
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
    print("\nmomentum LIVE exit-progress column migration")
    print("=" * 48)
    print()

    if not _table_exists(_TABLE):
        print(
            f"  {_TABLE} does not exist — run scripts/migrate_add_momentum_live.py first."
        )
        return

    for col, ddl in _COLUMNS:
        if _column_exists(_TABLE, col):
            print(f"  {col:24s} already existed -- no change")
            continue
        execute_query(f"ALTER TABLE {_TABLE} ADD COLUMN {col} {ddl}")
        print(f"  {col:24s} added")

    print()
    print("Migration complete.")
    print(
        "These columns let the live trader reconstruct partial-exit state after a "
        "restart without losing realized exit history."
    )


if __name__ == "__main__":
    main()
