#!/usr/bin/env python3
"""
migrate_add_clc_reversal_observations.py — Idempotent schema migration.

Creates the `clc_reversal_observations` table used by the CLCReversalTracker to
shadow-track the watch-only research signal
cheap_losing_contract_reversal_trail/v1.

One row is written per (signal x exit_profile): the strategy buys the cheap
*losing* contract and FIVE exit profiles (2 fixed + 3 ride-then-trail) are
simulated against the same observed price path.  Fills use ask+slippage on
entry and bid-slippage on exit (never mid).

Safe to re-run: uses CREATE TABLE IF NOT EXISTS, so an existing table is left
untouched.

Run once before starting the updated main loop:
    python scripts/migrate_add_clc_reversal_observations.py

No live trading is performed or enabled.  These rows are observation-only;
no paper trade is opened for the tracked signal.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "clc_reversal_observations"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS clc_reversal_observations (
    id                              BIGINT        AUTO_INCREMENT PRIMARY KEY,
    signal_id                       BIGINT        NULL,
    market_ticker                   VARCHAR(100)  NOT NULL,
    rule_name                       VARCHAR(100)  NOT NULL,
    rule_version                    VARCHAR(20)   NOT NULL,

    setup_type                      VARCHAR(80)   NOT NULL,
    market_phase                    VARCHAR(20),
    market_age_seconds              INT,
    time_remaining_seconds          INT,

    side_bought                     ENUM('YES', 'NO') NOT NULL,
    winning_side                    ENUM('YES', 'NO'),
    losing_side                     ENUM('YES', 'NO'),

    btc_price                       DECIMAL(14, 2),
    target_price                    DECIMAL(14, 2),
    raw_gap_z_score                 DECIMAL(10, 6),
    adverse_z_score                 DECIMAL(10, 6),
    raw_momentum_score              DECIMAL(8, 4),
    adverse_directional_momentum_score DECIMAL(8, 4),

    losing_contract_ask             DECIMAL(6, 4),
    losing_contract_bid             DECIMAL(6, 4),
    losing_contract_spread          DECIMAL(6, 4),
    losing_contract_low_since_open  DECIMAL(6, 4),
    losing_contract_bounce_from_low DECIMAL(8, 4),

    historical_reversal_probability DECIMAL(6, 4)  NULL,
    similar_sample_count            INT            DEFAULT 0,
    confidence_label                VARCHAR(20),

    volatility_regime               VARCHAR(12),
    whipsaw_score                   DECIMAL(6, 4)  NULL,
    hour_block                      VARCHAR(8),
    day_name                        VARCHAR(12),

    slippage_mode                   VARCHAR(12),
    entry_price_simulated           DECIMAL(6, 4),

    exit_profile                    VARCHAR(40)   NOT NULL,
    is_primary_path_row             BOOLEAN       DEFAULT 0,
    trail_activated                 BOOLEAN       NULL,
    peak_contract_price             DECIMAL(6, 4),
    max_favorable_excursion         DECIMAL(8, 4),
    max_adverse_excursion           DECIMAL(8, 4),
    time_to_peak                    DECIMAL(8, 3)  NULL,
    exit_price_simulated            DECIMAL(6, 4)  NULL,
    exit_reason                     VARCHAR(40)    NULL,
    pnl                             DECIMAL(8, 4)  NULL,
    pnl_percent                     DECIMAL(8, 4)  NULL,

    hit_plus_2c_before_minus_2c     BOOLEAN        NULL,
    hit_plus_3c_before_minus_2c     BOOLEAN        NULL,
    hit_plus_4c_before_minus_3c     BOOLEAN        NULL,

    n_updates                       INT            DEFAULT 0,

    status                          ENUM('ACTIVE', 'COMPLETE') DEFAULT 'ACTIVE',
    complete_reason                 VARCHAR(50)    NULL,
    recorded_at                     DATETIME(3)    NOT NULL,
    completed_at                    DATETIME(3)    NULL,
    created_at                      TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_clc_signal  (signal_id),
    INDEX idx_clc_rule    (rule_name, rule_version),
    INDEX idx_clc_status  (status),
    INDEX idx_clc_profile (exit_profile),
    INDEX idx_clc_setup   (setup_type),
    INDEX idx_clc_primary (is_primary_path_row, status),

    CONSTRAINT fk_clc_signal
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
    print("\nclc_reversal_observations schema migration")
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
        "CLCReversalTracker will populate this table for watch-only research\n"
        "signals (cheap_losing_contract_reversal_trail/v1).  No paper trades are\n"
        "opened for these signals; entries/exits are simulated only.\n"
    )


if __name__ == "__main__":
    main()
