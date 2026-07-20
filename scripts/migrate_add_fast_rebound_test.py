#!/usr/bin/env python3
"""
Create research-only TEST rows for the frozen 65-70c fast rebound hypothesis.

Safe to re-run. This does not touch trading endpoints or production order state.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_CREATE_FAST_REBOUND_TEST_TRADES = """
CREATE TABLE IF NOT EXISTS fast_rebound_test_trades (
    id                                  BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,

    profile                             VARCHAR(100) NOT NULL,
    exit_model                          VARCHAR(40) NOT NULL,

    market_id                           BIGINT NOT NULL,
    contract_id                         BIGINT NOT NULL,
    market_ticker                       VARCHAR(100) NOT NULL,
    dominant_side                       ENUM('YES','NO') NOT NULL,
    minority_side                       ENUM('YES','NO') NOT NULL,

    signal_at                           DATETIME(3) NOT NULL,
    entry_at                            DATETIME(3) NOT NULL,
    entry_bid                           DECIMAL(10,4),
    entry_ask                           DECIMAL(10,4) NOT NULL,
    entry_spread                        DECIMAL(10,4),

    btc_price_at_entry                  DECIMAL(18,2),
    strike                              DECIMAL(18,2),
    btc_distance_dominant_side          DECIMAL(18,2),
    time_since_open_seconds             DECIMAL(10,3),
    time_remaining_seconds              INT,

    dominant_ask                        DECIMAL(10,4),
    dominant_bid                        DECIMAL(10,4),
    dominant_spread                     DECIMAL(10,4),
    dominant_cents_per_second           DECIMAL(12,6),
    dominant_change_prev_30s_cents      DECIMAL(10,4),

    target_cents                        DECIMAL(10,4) NOT NULL,
    stop_cents                          DECIMAL(10,4) NOT NULL,
    target_bid_price                    DECIMAL(10,4) NOT NULL,
    stop_bid_price                      DECIMAL(10,4) NOT NULL,
    timeout_seconds                     DECIMAL(10,3) NOT NULL,

    status                              VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    exit_at                             DATETIME(3),
    exit_bid                            DECIMAL(10,4),
    exit_reason                         VARCHAR(40),
    holding_seconds                     DECIMAL(10,3),

    max_favorable_excursion_cents       DECIMAL(10,4),
    max_adverse_excursion_cents         DECIMAL(10,4),
    gross_pnl_cents                     DECIMAL(10,4),
    estimated_entry_fee_cents           DECIMAL(10,4),
    estimated_exit_fee_cents            DECIMAL(10,4),
    estimated_total_fee_cents           DECIMAL(10,4),
    estimated_extra_slippage_cents      DECIMAL(10,4),
    estimated_net_pnl_cents             DECIMAL(10,4),

    metadata_json                       JSON,
    created_at                          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uniq_frt_profile_market_model (profile, market_ticker, exit_model),
    INDEX idx_frt_status (status),
    INDEX idx_frt_signal_at (signal_at),
    INDEX idx_frt_market (market_id),
    INDEX idx_frt_contract (contract_id),
    INDEX idx_frt_profile (profile),
    INDEX idx_frt_exit_model (exit_model)
)
"""


def _table_exists(name: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (name,),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    existed = _table_exists("fast_rebound_test_trades")
    execute_query(_CREATE_FAST_REBOUND_TEST_TRADES)
    print("fast_rebound_test_trades", "already existed -- no change" if existed else "created")
    print("Migration complete. Research-only TEST table; no live orders.")


if __name__ == "__main__":
    main()
