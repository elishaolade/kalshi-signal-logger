#!/usr/bin/env python3
"""
migrate_add_dcvrb_observations.py — Idempotent schema migration.

Creates the `dcvrb_observations` table used by DCVRBTracker to shadow-track
the watch-only research signal delayed_contract_value_reversal_bounce/v1.

One row is written per (signal × comparison_version × exit_test) = 12 rows
per signal.  All 12 rows share the same forward bid path from signal time.

Comparison versions:
  v1  immediate   — entry at watch_start_contract_ask + slippage
  v2  2c+1c       — entry at signal-time ask + slippage (main signal)
  v3  5c+2c       — same entry as v2 but only when flush ≥ 5¢ AND bounce ≥ 2¢

Exit tests:
  test_a  tp +0.03  sl -0.02  timeout  45s
  test_b  tp +0.04  sl -0.03  timeout  60s
  test_c  tp +0.05  sl -0.03  timeout  75s
  test_d  tp +0.06  sl -0.04  timeout  90s

Safe to re-run: uses CREATE TABLE IF NOT EXISTS, so an existing table is left
untouched.

Run once before starting the updated main loop:
    python scripts/migrate_add_dcvrb_observations.py

No live trading is performed or enabled.  These rows are observation-only;
no paper trade is opened for the tracked signal.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "dcvrb_observations"

_CREATE = """
CREATE TABLE IF NOT EXISTS dcvrb_observations (
    id                              BIGINT        AUTO_INCREMENT PRIMARY KEY,
    signal_id                       BIGINT        NULL,
    market_ticker                   VARCHAR(100)  NOT NULL,
    rule_name                       VARCHAR(100)  NOT NULL,
    rule_version                    VARCHAR(20)   NOT NULL,
    side                            ENUM('YES', 'NO') NOT NULL,

    -- Stage 1 watch-state context (from signal.extra)
    contract_open_price             DECIMAL(6, 4)  NULL,
    contract_recent_high            DECIMAL(6, 4)  NULL,
    watch_start_contract_ask        DECIMAL(6, 4)  NULL,
    watch_start_contract_bid        DECIMAL(6, 4)  NULL,
    drop_from_open                  DECIMAL(8, 4)  NULL,
    drop_from_recent_high           DECIMAL(8, 4)  NULL,
    spread_at_watch                 DECIMAL(6, 4)  NULL,
    volatility_60s_at_watch         DECIMAL(10, 6) NULL,

    -- Stage 2/3 flush + bounce metrics
    local_low_since_watch           DECIMAL(6, 4)  NULL,
    drop_from_watch_start           DECIMAL(8, 4)  NULL,
    extra_flush                     DECIMAL(8, 4)  NULL,
    bounce_from_local_low           DECIMAL(8, 4)  NULL,
    extra_flush_bucket              VARCHAR(12)    NULL,
    bounce_bucket                   VARCHAR(12)    NULL,
    strong_bounce                   BOOLEAN        NULL,
    price_bucket                    VARCHAR(12)    NULL,

    -- Signal-time contract quotes
    contract_ask                    DECIMAL(6, 4)  NULL,
    contract_bid                    DECIMAL(6, 4)  NULL,
    spread                          DECIMAL(6, 4)  NULL,

    -- Contract price changes at signal time
    contract_price_change_5s        DECIMAL(8, 4)  NULL,
    contract_price_change_10s       DECIMAL(8, 4)  NULL,
    contract_price_change_30s       DECIMAL(8, 4)  NULL,

    -- Timing
    market_age_seconds              INT            NULL,
    time_remaining_seconds          INT            NULL,

    -- BTC context (secondary / informational)
    btc_price                       DECIMAL(14, 2) NULL,
    target_price                    DECIMAL(14, 2) NULL,
    raw_gap_z_score                 DECIMAL(10, 6) NULL,
    directional_gap_z_score         DECIMAL(10, 6) NULL,
    raw_momentum_score              DECIMAL(10, 4) NULL,
    directional_momentum_score      DECIMAL(10, 4) NULL,
    btc_velocity_10s                DECIMAL(12, 6) NULL,
    btc_velocity_30s                DECIMAL(12, 6) NULL,
    volatility_30s                  DECIMAL(10, 6) NULL,
    volatility_60s                  DECIMAL(10, 6) NULL,
    volatility_regime               VARCHAR(12)    NULL,
    hour_block                      VARCHAR(8)     NULL,
    day_name                        VARCHAR(12)    NULL,
    timezone_used                   VARCHAR(40)    NULL,

    -- Entry timing comparison version
    comparison_version              VARCHAR(4)     NOT NULL,
    v3_qualified                    BOOLEAN        NOT NULL DEFAULT 0,

    -- Fill model
    slippage_mode                   VARCHAR(12)    NULL,
    entry_price_simulated           DECIMAL(6, 4)  NULL,

    -- Exit test definition
    exit_test                       VARCHAR(8)     NOT NULL,
    tp_abs                          DECIMAL(6, 4)  NULL,
    sl_abs                          DECIMAL(6, 4)  NULL,
    timeout_s                       DECIMAL(8, 3)  NULL,

    -- v1 pre-signal fields
    v1_pre_signal_mae               DECIMAL(8, 4)  NULL,
    v1_stopped_out_pre_signal       BOOLEAN        NULL,

    -- Exit simulation results (filled by tracker)
    hit_take_profit_before_stop     BOOLEAN        NULL,
    hit_stop_before_take_profit     BOOLEAN        NULL,
    timed_out                       BOOLEAN        NULL,
    structure_stop_hit              BOOLEAN        NULL,
    max_favorable_excursion         DECIMAL(8, 4)  NULL,
    max_adverse_excursion           DECIMAL(8, 4)  NULL,
    time_to_peak                    DECIMAL(8, 3)  NULL,
    time_to_profit_target           DECIMAL(8, 3)  NULL,
    simulated_pnl                   DECIMAL(8, 4)  NULL,
    simulated_pnl_percent           DECIMAL(8, 4)  NULL,
    exit_price_simulated            DECIMAL(6, 4)  NULL,
    exit_reason_simulated           VARCHAR(40)    NULL,

    n_updates                       INT            DEFAULT 0,

    -- Lifecycle
    status                          ENUM('ACTIVE', 'COMPLETE') DEFAULT 'ACTIVE',
    complete_reason                 VARCHAR(50)    NULL,
    recorded_at                     DATETIME(3)    NOT NULL,
    completed_at                    DATETIME(3)    NULL,
    created_at                      TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_dcvrb_signal   (signal_id),
    INDEX idx_dcvrb_rule     (rule_name, rule_version),
    INDEX idx_dcvrb_status   (status),
    INDEX idx_dcvrb_version  (comparison_version),
    INDEX idx_dcvrb_test     (exit_test),
    INDEX idx_dcvrb_bucket   (price_bucket, extra_flush_bucket),
    INDEX idx_dcvrb_side     (side, comparison_version, exit_test),

    CONSTRAINT fk_dcvrb_signal
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
    print("\ndcvrb_observations schema migration")
    print("=" * 40)
    print()

    already = _table_exists(_TABLE)
    execute_query(_CREATE)

    if already:
        print(f"  {_TABLE:40s} already existed — no change")
    else:
        print(f"  {_TABLE:40s} ✓ created")

    print("\nMigration complete.")
    print(
        "DCVRBTracker will populate dcvrb_observations with shadow-tracked\n"
        "watch-only exit simulations.  No paper trades are opened and no\n"
        "live trading is enabled.\n"
    )


if __name__ == "__main__":
    main()
