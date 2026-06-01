#!/usr/bin/env python3
"""
migrate_add_backtest_tables.py — Idempotent schema migration.

Creates the `backtest_runs` and `backtest_trades` tables used by the historical
replay / backtesting engine (scripts/historical_replay.py).  Backtest results
are stored ENTIRELY SEPARATELY from live `paper_trades` — the two never mix.

Safe to re-run: uses CREATE TABLE IF NOT EXISTS, so existing tables are left
untouched.

Run once before the first backtest:
    python scripts/migrate_add_backtest_tables.py

PAPER-ONLY RESEARCH.  Replay re-derives decisions from stored snapshots and
simulates fills (entry = ask + slippage, exit = bid - slippage).  No live
trading is performed or enabled; no real order is ever placed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLES = ("backtest_runs", "backtest_trades")

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    id              BIGINT       AUTO_INCREMENT PRIMARY KEY,
    rule_name       VARCHAR(100) NOT NULL,
    rule_version    VARCHAR(20)  NOT NULL,
    slippage_mode   VARCHAR(12)  NOT NULL,
    exit_profiles   JSON,
    params          JSON,
    timezone_used   VARCHAR(40),
    data_start      DATETIME(3),
    data_end        DATETIME(3),
    n_markets       INT          DEFAULT 0,
    n_snapshots     INT          DEFAULT 0,
    n_signals       INT          DEFAULT 0,
    n_trades        INT          DEFAULT 0,
    notes           TEXT,
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_btr_rule    (rule_name, rule_version),
    INDEX idx_btr_created (created_at)
)
"""

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS backtest_trades (
    id                          BIGINT        AUTO_INCREMENT PRIMARY KEY,
    run_id                      BIGINT        NOT NULL,
    rule_name                   VARCHAR(100)  NOT NULL,
    rule_version                VARCHAR(20)   NOT NULL,
    market_ticker               VARCHAR(100)  NOT NULL,
    signal_seq                  INT,

    setup_type                  VARCHAR(80),
    market_phase                VARCHAR(20),
    market_age_seconds          INT,
    time_remaining_seconds      INT,

    side_bought                 ENUM('YES', 'NO') NOT NULL,
    winning_side                ENUM('YES', 'NO'),
    losing_side                 ENUM('YES', 'NO'),

    btc_price                   DECIMAL(14, 2),
    target_price                DECIMAL(14, 2),
    raw_gap_z_score             DECIMAL(10, 6),
    adverse_z_score             DECIMAL(10, 6),

    losing_contract_ask         DECIMAL(6, 4),
    losing_contract_bid         DECIMAL(6, 4),
    losing_contract_spread      DECIMAL(6, 4),

    volatility_regime           VARCHAR(12),
    whipsaw_score               DECIMAL(6, 4)  NULL,

    entry_time                  DATETIME(3),
    entry_date                  VARCHAR(10),
    entry_hour                  INT,
    entry_day_of_week           INT,
    entry_day_name              VARCHAR(12),
    entry_hour_block            VARCHAR(8),

    slippage_mode               VARCHAR(12),
    entry_price_simulated       DECIMAL(6, 4),
    entry_bid                   DECIMAL(6, 4),

    exit_profile                VARCHAR(40)   NOT NULL,
    trail_activated             BOOLEAN       NULL,
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

    INDEX idx_btt_run     (run_id),
    INDEX idx_btt_profile (run_id, exit_profile),
    INDEX idx_btt_market  (market_ticker),

    CONSTRAINT fk_btt_run
        FOREIGN KEY (run_id) REFERENCES backtest_runs (id)
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
    print("\nbacktest tables schema migration")
    print("=" * 48)
    print()

    existed = {t: _table_exists(t) for t in _TABLES}

    # backtest_runs must exist before backtest_trades (FK target).
    execute_query(_CREATE_RUNS)
    execute_query(_CREATE_TRADES)

    for t in _TABLES:
        if existed[t]:
            print(f"  {t:18s} already existed — no change")
        else:
            print(f"  {t:18s} ✓ created")

    print("\nMigration complete.")
    print(
        "historical_replay.py will populate these tables with simulated\n"
        "backtest results, kept entirely separate from live paper_trades.\n"
        "No paper trades are opened and no live trading is enabled.\n"
    )


if __name__ == "__main__":
    main()
