#!/usr/bin/env python3
"""
migrate_add_time_features.py — Idempotent schema migration.

Adds time-of-day / day-of-week columns to `signals` and `paper_trades`.
Safe to re-run: checks information_schema before issuing each ALTER so
already-existing columns are never touched.

Run once before starting the updated main loop:
    python scripts/migrate_add_time_features.py

No live trading is performed or enabled.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_all

# ── Column definitions ─────────────────────────────────────────────────────────
#
# Format: (column_name, sql_type_and_constraints)
# Columns are added in listed order after any existing columns.

_SIGNALS_COLUMNS: list[tuple[str, str]] = [
    ("entry_date",        "DATE              NULL"),
    ("entry_time",        "TIME              NULL"),   # "HH:MM:SS" in configured tz
    ("entry_hour",        "TINYINT UNSIGNED  NULL"),
    ("entry_minute",      "TINYINT UNSIGNED  NULL"),
    ("entry_day_of_week", "TINYINT UNSIGNED  NULL"),   # 0=Mon … 6=Sun
    ("entry_day_name",    "VARCHAR(10)        NULL"),
    ("entry_is_weekend",  "BOOLEAN            NULL"),
    ("entry_15m_block",   "VARCHAR(5)         NULL"),  # "HH:MM"
    ("entry_30m_block",   "VARCHAR(5)         NULL"),
    ("entry_hour_block",  "VARCHAR(5)         NULL"),  # "HH:00"
    ("market_open_time",  "DATETIME(3)        NULL"),
    ("market_close_time", "DATETIME(3)        NULL"),
    ("timezone_used",     "VARCHAR(50)        NULL DEFAULT 'America/New_York'"),
]

# paper_trades.entry_time already exists as DATETIME(3) — omit it here.
# market_open_time / market_close_time are market-level; available via
# the signals join, so not duplicated in paper_trades.
_PAPER_TRADES_COLUMNS: list[tuple[str, str]] = [
    ("entry_date",        "DATE              NULL"),
    ("entry_hour",        "TINYINT UNSIGNED  NULL"),
    ("entry_minute",      "TINYINT UNSIGNED  NULL"),
    ("entry_day_of_week", "TINYINT UNSIGNED  NULL"),
    ("entry_day_name",    "VARCHAR(10)        NULL"),
    ("entry_is_weekend",  "BOOLEAN            NULL"),
    ("entry_15m_block",   "VARCHAR(5)         NULL"),
    ("entry_30m_block",   "VARCHAR(5)         NULL"),
    ("entry_hour_block",  "VARCHAR(5)         NULL"),
    ("timezone_used",     "VARCHAR(50)        NULL DEFAULT 'America/New_York'"),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _existing_columns(table: str) -> set[str]:
    """Return the set of column names currently in `table`."""
    rows = fetch_all(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = %s
        """,
        (table,),
    )
    return {str(r["COLUMN_NAME"]) for r in rows}


def _migrate_table(table: str, column_defs: list[tuple[str, str]]) -> None:
    existing = _existing_columns(table)
    added    = 0
    skipped  = 0

    for col_name, col_type in column_defs:
        if col_name in existing:
            print(f"  {table}.{col_name:<22}  already exists — skip")
            skipped += 1
            continue

        sql = f"ALTER TABLE `{table}` ADD COLUMN `{col_name}` {col_type}"
        try:
            execute_query(sql)
            print(f"  {table}.{col_name:<22}  ✓ added ({col_type.split()[0]})")
            added += 1
        except Exception as exc:
            print(f"  {table}.{col_name:<22}  ✗ FAILED: {exc}")
            raise

    print(f"  → {table}: {added} added, {skipped} already present\n")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    print("\nTime-feature schema migration")
    print("=" * 48)
    print()

    print("Table: signals")
    print("-" * 48)
    _migrate_table("signals", _SIGNALS_COLUMNS)

    print("Table: paper_trades")
    print("-" * 48)
    _migrate_table("paper_trades", _PAPER_TRADES_COLUMNS)

    print("Migration complete.")
    print(
        "Existing rows will have NULL for all new columns.\n"
        "New signals and paper trades will be populated automatically.\n"
    )


if __name__ == "__main__":
    main()
