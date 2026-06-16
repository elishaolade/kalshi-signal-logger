#!/usr/bin/env python3
"""
migrate_add_momentum_backtest.py — Idempotent schema migration.

Creates:
    momentum_bt_experiments   — one row per backtest run (config + metadata)
    momentum_bt_trades        — one row per simulated trade

Safe to re-run: uses CREATE TABLE IF NOT EXISTS.  Existing tables are left
untouched.

Run once before the first backtest:
    python scripts/migrate_add_momentum_backtest.py

PAPER-ONLY RESEARCH.  These tables store simulated results only.
No live trading is performed or enabled.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

# ── Table DDL ─────────────────────────────────────────────────────────────────

_CREATE_EXPERIMENTS = """
CREATE TABLE IF NOT EXISTS momentum_bt_experiments (
    id                  BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    name                VARCHAR(200)    NOT NULL,
    description         TEXT,
    hypothesis          TEXT,
    configuration_json  JSON            NOT NULL,
    data_start_at       DATETIME(3),
    data_end_at         DATETIME(3),
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at        DATETIME,
    status              VARCHAR(20)     NOT NULL DEFAULT 'pending',
    git_commit          VARCHAR(40),
    notes               TEXT,

    INDEX idx_mbe_status  (status),
    INDEX idx_mbe_created (created_at)
)
"""

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS momentum_bt_trades (
    id                              BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    experiment_id                   BIGINT          NOT NULL,

    -- Market / contract identity (FK to v2 schema)
    market_id                       BIGINT          NOT NULL,   -- markets.id
    contract_id                     BIGINT          NOT NULL,   -- contracts.id
    contract_side                   ENUM('YES','NO') NOT NULL,

    -- Exit profile label: e.g. "ht30s_tp5c"
    exit_profile                    VARCHAR(60)     NOT NULL,
    holding_seconds_config          INT             NOT NULL,   -- configured holding period
    profit_target_cfg               DECIMAL(10,4),              -- configured profit target (NULL if none)

    -- Signal / entry timing
    signal_at                       DATETIME(3)     NOT NULL,
    entry_at                        DATETIME(3),
    entry_ask                       DECIMAL(10,4),
    entry_bid                       DECIMAL(10,4),
    entry_spread                    DECIMAL(10,4),

    -- Exit outcome
    exit_at                         DATETIME(3),
    exit_bid                        DECIMAL(10,4),
    exit_reason                     VARCHAR(40),
    holding_seconds                 DECIMAL(10,2),

    -- Context at entry
    btc_price_at_entry              DECIMAL(18,2),
    target_price                    DECIMAL(18,2),
    distance_from_target            DECIMAL(18,2),
    side_adjusted_distance          DECIMAL(18,2),

    -- Side-adjusted movement over lookback window
    btc_move_toward_side            DECIMAL(18,2),
    contract_move_during_lookback   DECIMAL(10,4),

    -- Volatility at entry (from market_metrics)
    volatility_1m                   DECIMAL(12,8),
    volatility_5m                   DECIMAL(12,8),
    time_remaining_at_entry         INT,

    -- Excursion (relative to entry_ask; dollar fractions)
    max_favorable_excursion         DECIMAL(10,4),
    max_adverse_excursion           DECIMAL(10,4),

    -- P&L (dollar fractions; 0.05 = 5 cents)
    gross_pnl_cents                 DECIMAL(10,4),
    estimated_fees_cents            DECIMAL(10,4),
    estimated_slippage_cents        DECIMAL(10,4),
    net_pnl_cents                   DECIMAL(10,4),

    -- Classification
    result_status                   VARCHAR(20)     NOT NULL DEFAULT 'unexecutable',
    split_label                     VARCHAR(10),                -- 'train' | 'test'
    metadata_json                   JSON,

    created_at                      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_mbt_experiment  (experiment_id),
    INDEX idx_mbt_market      (market_id),
    INDEX idx_mbt_contract    (contract_id),
    INDEX idx_mbt_signal_at   (signal_at),
    INDEX idx_mbt_side        (contract_side),
    INDEX idx_mbt_profile     (exit_profile),
    INDEX idx_mbt_split       (split_label),

    CONSTRAINT fk_mbt_experiment
        FOREIGN KEY (experiment_id) REFERENCES momentum_bt_experiments (id)
        ON DELETE CASCADE ON UPDATE CASCADE
)
"""

_TABLES = ["momentum_bt_experiments", "momentum_bt_trades"]


def _table_exists(name: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (name,),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    print("\nmomentum backtest schema migration")
    print("=" * 48)
    print()

    existed = {t: _table_exists(t) for t in _TABLES}

    # momentum_bt_experiments must exist before momentum_bt_trades (FK target).
    execute_query(_CREATE_EXPERIMENTS)
    execute_query(_CREATE_TRADES)

    for t in _TABLES:
        status = "already existed — no change" if existed[t] else "✓ created"
        print(f"  {t:35s} {status}")

    print()
    print("Migration complete.")
    print(
        "Run a backtest with:\n"
        "  python scripts/momentum_backtest.py --config experiments/momentum-lag.json\n"
        "\n"
        "These tables store SIMULATED results only.  No live trading is performed.\n"
    )


if __name__ == "__main__":
    main()
