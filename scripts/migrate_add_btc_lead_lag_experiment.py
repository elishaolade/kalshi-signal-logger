#!/usr/bin/env python3
"""
migrate_add_btc_lead_lag_experiment.py - Add telemetry columns for the BTC
lead-lag live experiment.

This migration is additive and safe to re-run. It does not enable live trading.

The experiment asks whether external BTC spot movement leads Kalshi BTC 15m
quote repricing enough to create a short-term live edge.  The wide path
telemetry is stored as JSON so we do not add dozens of sparse columns:

  btc_lead_signal_json:
    BTC moves over 5/10/30/60s, Kalshi ask moves over same windows, signal-time
    YES/NO bid/ask, strike distance, TTE, spread, quote age, expected edge.

  btc_lead_path_json:
    BTC price and YES/NO bid/ask snapshots at signal/entry and +5/+10/+15/+20/
    +30/+60 seconds after entry fill.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "momentum_live_trades"

_COLUMNS = [
    ("strategy_name", "VARCHAR(80) DEFAULT NULL"),
    ("btc_lead_triggered", "TINYINT(1) DEFAULT NULL"),
    ("btc_lead_expected_edge_cents", "DECIMAL(10,4) DEFAULT NULL"),
    ("btc_lead_btc_move_5s", "DECIMAL(12,4) DEFAULT NULL"),
    ("btc_lead_btc_move_10s", "DECIMAL(12,4) DEFAULT NULL"),
    ("btc_lead_btc_move_30s", "DECIMAL(12,4) DEFAULT NULL"),
    ("btc_lead_btc_move_60s", "DECIMAL(12,4) DEFAULT NULL"),
    ("btc_lead_contract_move_5s_cents", "DECIMAL(10,4) DEFAULT NULL"),
    ("btc_lead_contract_move_10s_cents", "DECIMAL(10,4) DEFAULT NULL"),
    ("btc_lead_contract_move_30s_cents", "DECIMAL(10,4) DEFAULT NULL"),
    ("btc_lead_contract_move_60s_cents", "DECIMAL(10,4) DEFAULT NULL"),
    ("btc_lead_signal_json", "JSON DEFAULT NULL"),
    ("btc_lead_path_json", "JSON DEFAULT NULL"),
]


def _table_exists(table: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return bool(row and int(row["n"]) > 0)


def _column_exists(table: str, column: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    print("\nBTC lead-lag experiment telemetry migration")
    print("=" * 52)
    print()

    if not _table_exists(_TABLE):
        print(f"  {_TABLE} does not exist - run scripts/migrate_add_momentum_live.py first.")
        return

    added = 0
    for column, ddl in _COLUMNS:
        if _column_exists(_TABLE, column):
            print(f"  {column:36s} already existed -- no change")
            continue
        execute_query(f"ALTER TABLE {_TABLE} ADD COLUMN {column} {ddl}")
        print(f"  {column:36s} added")
        added += 1

    print()
    print(f"Migration complete ({added} column(s) added).")
    print("All new columns are nullable and do not change live behavior.")


if __name__ == "__main__":
    main()
