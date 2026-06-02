#!/usr/bin/env python3
"""
migrate_add_followthrough_backtest.py — Idempotent schema migration.

Creates `followthrough_backtest_runs` and `followthrough_backtest_trades`, the
dedicated tables for the follow-through filter hypothesis test
(scripts/followthrough_backtest.py).  Results are stored ENTIRELY SEPARATELY
from live `paper_trades` and from the generic `backtest_*` tables.

Safe to re-run: uses CREATE TABLE IF NOT EXISTS, so existing tables are left
untouched.

Run once before the first follow-through backtest:
    python scripts/migrate_add_followthrough_backtest.py

PAPER-ONLY / BACKTEST-ONLY RESEARCH.  Replay re-derives decisions from stored
snapshots and simulates fills (entry = ask + slippage, exit = bid - slippage).
No live trading is performed or enabled; no real order is ever placed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLES = ("followthrough_backtest_runs", "followthrough_backtest_trades")

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS followthrough_backtest_runs (
    id                  BIGINT       AUTO_INCREMENT PRIMARY KEY,
    rule_name           VARCHAR(100) NOT NULL,
    rule_version        VARCHAR(20)  NOT NULL,
    slippage_mode       VARCHAR(12)  NOT NULL,
    exit_profiles       JSON,
    params              JSON,
    timezone_used       VARCHAR(40),
    baseline_volume_60s DECIMAL(14, 4) NULL,

    data_start          DATETIME(3),
    data_end            DATETIME(3),
    n_markets           INT          DEFAULT 0,
    n_snapshots         INT          DEFAULT 0,
    n_base_entries      INT          DEFAULT 0,
    n_confirmed_entries INT          DEFAULT 0,
    n_trades            INT          DEFAULT 0,

    notes               TEXT,
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_ftr_rule    (rule_name, rule_version),
    INDEX idx_ftr_created (created_at)
)
"""

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS followthrough_backtest_trades (
    id                          BIGINT        AUTO_INCREMENT PRIMARY KEY,
    run_id                      BIGINT        NOT NULL,
    rule_name                   VARCHAR(100)  NOT NULL,
    rule_version                VARCHAR(20)   NOT NULL,
    market_ticker               VARCHAR(100)  NOT NULL,
    signal_seq                  INT,

    side_bought                 ENUM('YES', 'NO') NOT NULL,
    market_phase                VARCHAR(20),
    market_age_seconds          INT,
    time_remaining_seconds      INT,
    btc_price                   DECIMAL(14, 2),
    target_price                DECIMAL(14, 2),
    raw_gap_z_score             DECIMAL(10, 6),
    directional_momentum        DECIMAL(10, 4),

    no_ask                      DECIMAL(6, 4),
    no_bid                      DECIMAL(6, 4),
    no_mid                      DECIMAL(6, 4),
    spread                      DECIMAL(6, 4),
    spread_bucket               VARCHAR(12),
    volatility_regime           VARCHAR(12),

    no_price_change_10s          DECIMAL(8, 4)  NULL,
    no_price_change_30s          DECIMAL(8, 4)  NULL,
    no_recent_high_30s           DECIMAL(6, 4)  NULL,
    no_pullback_from_recent_high DECIMAL(8, 4)  NULL,
    quote_update_count_60s       INT            NULL,
    volume_60s                   DECIMAL(14, 4) NULL,
    baseline_volume_60s          DECIMAL(14, 4) NULL,
    participation_basis          VARCHAR(12)    NULL,
    followthrough_confirmed      BOOLEAN        NOT NULL,
    followthrough_failed         BOOLEAN        NOT NULL,
    scalping_valid_window        BOOLEAN        NULL,

    entry_time                  DATETIME(3),
    entry_date                  VARCHAR(10),
    entry_hour                  INT,
    entry_day_of_week           INT,
    entry_day_name              VARCHAR(12),
    entry_hour_block            VARCHAR(8),
    entry_is_weekend            BOOLEAN        NULL,

    slippage_mode               VARCHAR(12),
    entry_price_simulated       DECIMAL(6, 4),
    entry_bid                   DECIMAL(6, 4),

    exit_profile                VARCHAR(40)   NOT NULL,
    peak_contract_price         DECIMAL(6, 4),
    max_favorable_excursion     DECIMAL(8, 4),
    max_adverse_excursion       DECIMAL(8, 4),
    time_to_peak                DECIMAL(8, 3)  NULL,
    exit_time                   DATETIME(3)    NULL,
    exit_price_simulated        DECIMAL(6, 4)  NULL,
    exit_reason                 VARCHAR(40)    NULL,
    pnl                         DECIMAL(8, 4)  NULL,
    pnl_percent                 DECIMAL(8, 4)  NULL,

    n_updates                   INT            DEFAULT 0,
    created_at                  TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_ftt_run        (run_id),
    INDEX idx_ftt_profile    (run_id, exit_profile),
    INDEX idx_ftt_confirmed  (run_id, followthrough_confirmed),
    INDEX idx_ftt_market     (market_ticker),

    CONSTRAINT fk_ftt_run
        FOREIGN KEY (run_id) REFERENCES followthrough_backtest_runs (id)
        ON DELETE CASCADE ON UPDATE CASCADE
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
    print("\nfollow-through backtest tables schema migration")
    print("=" * 48)
    print()

    existed = {t: _table_exists(t) for t in _TABLES}

    # runs must exist before trades (FK target).
    execute_query(_CREATE_RUNS)
    execute_query(_CREATE_TRADES)

    for t in _TABLES:
        if existed[t]:
            print(f"  {t:34s} already existed — no change")
        else:
            print(f"  {t:34s} ✓ created")

    print("\nMigration complete.")
    print(
        "followthrough_backtest.py will populate these tables with simulated\n"
        "results, kept entirely separate from live paper_trades.\n"
        "No paper trades are opened and no live trading is enabled.\n"
    )


if __name__ == "__main__":
    main()
