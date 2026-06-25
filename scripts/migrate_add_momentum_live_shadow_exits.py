#!/usr/bin/env python3
"""
migrate_add_momentum_live_shadow_exits.py — Add WebSocket shadow-exit event table.

This table records hypothetical WS-aware exit triggers for live trades. These
rows are diagnostics only; they never place or alter real orders.

Run:
    python scripts/migrate_add_momentum_live_shadow_exits.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "momentum_live_shadow_exits"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    live_trade_id BIGINT UNSIGNED NOT NULL,
    ticker VARCHAR(64) NOT NULL,
    side ENUM('YES','NO') NOT NULL,
    shadow_exit_reason VARCHAR(64) NOT NULL,
    triggered_at DATETIME(3) NOT NULL,
    age_seconds DECIMAL(10,2) DEFAULT NULL,
    exit_price DECIMAL(10,4) DEFAULT NULL,
    profit_cents DECIMAL(10,4) DEFAULT NULL,
    current_profit_cents DECIMAL(10,4) DEFAULT NULL,
    max_profit_cents DECIMAL(10,4) DEFAULT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_live_trade_shadow_reason (live_trade_id, shadow_exit_reason),
    KEY idx_live_trade_id (live_trade_id),
    KEY idx_shadow_exit_reason (shadow_exit_reason),
    KEY idx_triggered_at (triggered_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _table_exists(table: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    print("\nMomentum LIVE WS shadow-exit migration")
    print("=" * 44)
    print()

    already_exists = _table_exists(_TABLE)
    execute_query(_CREATE_TABLE_SQL)

    if already_exists:
        print(f"  {_TABLE} already existed -- no change")
    else:
        print(f"  {_TABLE} created")

    print()
    print("Migration complete.")
    print("This table stores shadow-only WS exit triggers for diagnostics.")


if __name__ == "__main__":
    main()
