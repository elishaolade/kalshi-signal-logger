#!/usr/bin/env python3
"""
migrate_add_momentum_shadow.py — Idempotent schema migration.

Creates:
    momentum_shadow_trades   — one row per live shadow trade (open + completed)

Safe to re-run: uses CREATE TABLE IF NOT EXISTS.

Run once before starting the logger with shadow tracking enabled:
    python scripts/migrate_add_momentum_shadow.py

RESEARCH ONLY.  No live trades are placed.  This table records simulated
outcomes from live Kalshi quote data only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

# ── Table DDL ─────────────────────────────────────────────────────────────────

_CREATE_SHADOW_TRADES = """
CREATE TABLE IF NOT EXISTS momentum_shadow_trades (
    id                              BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,

    -- Market / contract identity (FK to v2 schema)
    market_id                       BIGINT          NOT NULL,   -- markets.id
    contract_id                     BIGINT          NOT NULL,   -- contracts.id
    market_ticker                   VARCHAR(100)    NOT NULL,   -- Kalshi ticker string
    side                            ENUM('YES','NO') NOT NULL,

    -- Signal / entry timing
    signal_at                       DATETIME(3)     NOT NULL,
    entry_at                        DATETIME(3)     NOT NULL,

    -- Entry prices (live Kalshi quotes at signal time)
    entry_ask                       DECIMAL(10,4)   NOT NULL,
    entry_bid                       DECIMAL(10,4),
    entry_spread                    DECIMAL(10,4),

    -- Context at entry
    btc_price_at_entry              DECIMAL(18,2),
    target_price                    DECIMAL(18,2),
    distance_from_target            DECIMAL(18,2),
    side_adjusted_distance          DECIMAL(18,2),
    btc_move_toward_side            DECIMAL(18,2),
    contract_move_during_lookback   DECIMAL(10,4),
    volatility_1m                   DECIMAL(12,8),
    volatility_5m                   DECIMAL(12,8),
    time_remaining_at_entry         INT,

    -- Frozen exit profile
    exit_profile                    VARCHAR(60)     NOT NULL DEFAULT 'ht120s_tp3c',
    target_ask_price                DECIMAL(10,4),              -- entry_ask + 0.03

    -- Lifecycle status
    status                          VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE',

    -- Exit outcome
    exit_at                         DATETIME(3),
    exit_bid                        DECIMAL(10,4),
    exit_reason                     VARCHAR(40),
    holding_seconds                 DECIMAL(10,2),

    -- Excursion (relative to entry_ask; dollar fractions)
    max_favorable_excursion         DECIMAL(10,4),
    max_adverse_excursion           DECIMAL(10,4),

    -- P&L (dollar fractions; 0.05 = 5 cents)
    gross_pnl_cents                 DECIMAL(10,4),
    estimated_fees_cents            DECIMAL(10,4),
    estimated_slippage_cents        DECIMAL(10,4),
    net_pnl_cents                   DECIMAL(10,4),

    -- Classification
    result_status                   VARCHAR(20),

    -- Optional metadata
    metadata_json                   JSON,

    created_at                      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                    ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_mst_contract    (contract_id),
    INDEX idx_mst_market      (market_id),
    INDEX idx_mst_ticker      (market_ticker),
    INDEX idx_mst_signal_at   (signal_at),
    INDEX idx_mst_status      (status),
    INDEX idx_mst_side        (side),
    INDEX idx_mst_result      (result_status)
)
"""

_TABLES = ["momentum_shadow_trades"]


def _table_exists(name: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (name,),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    print("\nmomentum shadow trades schema migration")
    print("=" * 48)
    print()

    existed = {t: _table_exists(t) for t in _TABLES}

    execute_query(_CREATE_SHADOW_TRADES)

    for t in _TABLES:
        status = "already existed -- no change" if existed[t] else "created"
        print(f"  {t:40s} {status}")

    print()
    print("Migration complete.")
    print(
        "Start the logger with shadow tracking:\n"
        "  python -m app.main\n"
        "\n"
        "Inspect open shadow trades:\n"
        "  SELECT * FROM momentum_shadow_trades WHERE status = 'ACTIVE';\n"
        "\n"
        "Inspect completed shadow trades:\n"
        "  SELECT * FROM momentum_shadow_trades WHERE status = 'COMPLETE'"
        " ORDER BY signal_at DESC LIMIT 50;\n"
        "\n"
        "RESEARCH ONLY.  No live trading is performed.\n"
    )


if __name__ == "__main__":
    main()
