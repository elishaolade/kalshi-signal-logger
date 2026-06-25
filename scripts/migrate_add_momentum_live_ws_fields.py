#!/usr/bin/env python3
"""
migrate_add_momentum_live_ws_fields.py — Idempotent live-trade instrumentation migration.

Adds optional WebSocket / execution-observability columns to momentum_live_trades.
These fields are used for quote freshness, spread-at-entry, max favorable
excursion, target-touch tracking, and latency-style diagnostics.

Run after scripts/migrate_add_momentum_live.py:
    python scripts/migrate_add_momentum_live_ws_fields.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "momentum_live_trades"

_COLUMNS = [
    ("ws_enabled", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("ws_quote_age_at_entry", "DECIMAL(10,4) DEFAULT NULL"),
    ("ws_spread_at_entry", "DECIMAL(10,4) DEFAULT NULL"),
    ("ws_entry_best_bid", "DECIMAL(10,4) DEFAULT NULL"),
    ("ws_entry_best_ask", "DECIMAL(10,4) DEFAULT NULL"),
    ("ws_exit_best_bid", "DECIMAL(10,4) DEFAULT NULL"),
    ("ws_exit_spread", "DECIMAL(10,4) DEFAULT NULL"),
    ("ws_avg_spread_during_trade", "DECIMAL(10,4) DEFAULT NULL"),
    ("ws_max_quote_age_during_trade", "DECIMAL(10,4) DEFAULT NULL"),
    ("max_bid_after_entry", "DECIMAL(10,4) DEFAULT NULL"),
    ("max_profit_cents", "DECIMAL(10,4) DEFAULT NULL"),
    ("time_to_max_bid_seconds", "DECIMAL(10,2) DEFAULT NULL"),
    ("seconds_above_1c", "DECIMAL(10,2) DEFAULT NULL"),
    ("seconds_above_2c", "DECIMAL(10,2) DEFAULT NULL"),
    ("seconds_above_3c", "DECIMAL(10,2) DEFAULT NULL"),
    ("seconds_above_4c", "DECIMAL(10,2) DEFAULT NULL"),
    ("seconds_above_5c", "DECIMAL(10,2) DEFAULT NULL"),
    ("bid_at_30s", "DECIMAL(10,4) DEFAULT NULL"),
    ("bid_at_60s", "DECIMAL(10,4) DEFAULT NULL"),
    ("bid_at_90s", "DECIMAL(10,4) DEFAULT NULL"),
    ("bid_at_120s", "DECIMAL(10,4) DEFAULT NULL"),
    ("went_green", "TINYINT(1) DEFAULT NULL"),
    ("target_touched", "TINYINT(1) DEFAULT NULL"),
    ("target_touch_count", "INT NOT NULL DEFAULT 0"),
    ("target_first_touched_at", "DATETIME(3) DEFAULT NULL"),
    ("target_total_visible_seconds", "DECIMAL(10,2) DEFAULT NULL"),
    ("entry_fill_detected_by", "VARCHAR(32) DEFAULT NULL"),
    ("exit_fill_detected_by", "VARCHAR(32) DEFAULT NULL"),
    ("entry_signal_to_order_ms", "INT DEFAULT NULL"),
    ("entry_order_to_ack_ms", "INT DEFAULT NULL"),
    ("entry_ack_to_fill_ms", "INT DEFAULT NULL"),
    ("fill_to_exit_order_ms", "INT DEFAULT NULL"),
    ("exit_signal_to_order_ms", "INT DEFAULT NULL"),
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
    print("\nmomentum LIVE websocket/instrumentation field migration")
    print("=" * 56)
    print()

    if not _table_exists(_TABLE):
        print(f"  {_TABLE} does not exist — run scripts/migrate_add_momentum_live.py first.")
        return

    for col, ddl in _COLUMNS:
        if _column_exists(_TABLE, col):
            print(f"  {col:30s} already existed -- no change")
            continue
        execute_query(f"ALTER TABLE {_TABLE} ADD COLUMN {col} {ddl}")
        print(f"  {col:30s} added")

    print()
    print("Migration complete.")
    print(
        "These fields support WebSocket-aware live diagnostics, target-touch "
        "tracking, and execution bottleneck analysis."
    )


if __name__ == "__main__":
    main()
