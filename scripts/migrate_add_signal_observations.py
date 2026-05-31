#!/usr/bin/env python3
"""
migrate_add_signal_observations.py — Idempotent schema migration.

Creates the `signal_observations` table used by the ObservationTracker to
record live shadow-tracking of watch-only research signals
(early_overextension_reversal_scalp/v1).

Safe to re-run: uses CREATE TABLE IF NOT EXISTS, so an existing table is left
untouched.

Run once before starting the updated main loop:
    python scripts/migrate_add_signal_observations.py

No live trading is performed or enabled.  These rows are observation-only;
no paper trade is opened for the tracked signals.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "signal_observations"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS signal_observations (
    id                          BIGINT        AUTO_INCREMENT PRIMARY KEY,
    signal_id                   BIGINT        NULL,
    market_ticker               VARCHAR(100)  NOT NULL,
    rule_name                   VARCHAR(100)  NOT NULL,
    rule_version                VARCHAR(20)   NOT NULL,
    side                        ENUM('YES', 'NO') NOT NULL,

    winning_side                ENUM('YES', 'NO'),
    losing_side                 ENUM('YES', 'NO'),
    winning_change_60s          DECIMAL(8, 4),
    winning_dir_z               DECIMAL(10, 6),
    losing_bounce_from_low      DECIMAL(8, 4)  NULL,
    losing_mom_10s              DECIMAL(8, 4)  NULL,
    losing_ask_at_signal        DECIMAL(6, 4),
    market_age_seconds          INT,

    entry_ref_price             DECIMAL(6, 4),
    entry_time                  DATETIME(3),
    peak_price                  DECIMAL(6, 4),
    low_price                   DECIMAL(6, 4),
    last_price                  DECIMAL(6, 4),
    n_updates                   INT            DEFAULT 0,

    max_favorable_excursion     DECIMAL(8, 4),
    max_adverse_excursion       DECIMAL(8, 4),
    hit_plus_3c_before_minus_2c BOOLEAN,
    hit_plus_4c_before_minus_2c BOOLEAN,
    hit_plus_5c_before_minus_3c BOOLEAN,
    time_to_peak_s              DECIMAL(8, 3)  NULL,
    time_to_plus_3c_s           DECIMAL(8, 3)  NULL,
    time_to_stop_s              DECIMAL(8, 3)  NULL,
    did_make_new_low_after_signal BOOLEAN,

    sim_tp3_sl2_outcome         VARCHAR(12)    NULL,
    sim_tp3_sl2_pnl             DECIMAL(8, 4)  NULL,
    sim_tp5_sl3_outcome         VARCHAR(12)    NULL,
    sim_tp5_sl3_pnl             DECIMAL(8, 4)  NULL,
    sim_timeout60_pnl           DECIMAL(8, 4)  NULL,

    status                      ENUM('ACTIVE', 'COMPLETE') DEFAULT 'ACTIVE',
    complete_reason             VARCHAR(50)    NULL,
    recorded_at                 DATETIME(3)    NOT NULL,
    completed_at                DATETIME(3)    NULL,
    created_at                  TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_so_signal (signal_id),
    INDEX idx_so_rule   (rule_name, rule_version),
    INDEX idx_so_status (status),

    CONSTRAINT fk_so_signal
        FOREIGN KEY (signal_id) REFERENCES signals (id)
        ON DELETE SET NULL ON UPDATE CASCADE
)
"""


def _table_exists(table: str) -> bool:
    row = fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = %s
        """,
        (table,),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    print("\nsignal_observations schema migration")
    print("=" * 48)
    print()

    existed_before = _table_exists(_TABLE)
    execute_query(_CREATE_SQL)

    if existed_before:
        print(f"  {_TABLE}  already existed — no change")
    else:
        print(f"  {_TABLE}  ✓ created")

    print("\nMigration complete.")
    print(
        "ObservationTracker will populate this table for watch-only research\n"
        "signals (early_overextension_reversal_scalp/v1).  No paper trades are\n"
        "opened for these signals.\n"
    )


if __name__ == "__main__":
    main()
