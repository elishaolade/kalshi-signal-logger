#!/usr/bin/env python3
"""
Create table for the prospective 08:00-11:00 ET BTC impulse paper test.

Research-only. This table stores at most one paper-trade/skip row per ET date
for a frozen live-style rule:
  - first clean quote in 08:00-11:00 ET where abs(BTC 60s move) >= $50
  - buy side aligned with BTC 60s direction at ask
  - exit at bid after 120s using first clean quote within tolerance
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS btc_impulse_paper_trades (
    id                              BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    profile                         VARCHAR(120) NOT NULL,
    et_date                         DATE NOT NULL,
    status                          VARCHAR(40) NOT NULL,
    skip_reason                     VARCHAR(255) NULL,

    market_db_id                    BIGINT NULL,
    market_ticker                   VARCHAR(100) NULL,
    contract_id                     BIGINT NULL,
    trade_side                      VARCHAR(10) NULL,

    entry_at                        DATETIME(3) NULL,
    entry_at_et                     DATETIME(3) NULL,
    exit_due_at                     DATETIME(3) NULL,
    exit_at                         DATETIME(3) NULL,
    exit_at_et                      DATETIME(3) NULL,

    btc_price_at_entry              DECIMAL(18,4) NULL,
    btc_price_60s_before_entry      DECIMAL(18,4) NULL,
    btc_60s_move                    DECIMAL(18,4) NULL,

    entry_bid                       DECIMAL(10,4) NULL,
    entry_ask                       DECIMAL(10,4) NULL,
    entry_spread                    DECIMAL(10,4) NULL,
    exit_bid                        DECIMAL(10,4) NULL,

    gross_cents                     DECIMAL(10,4) NULL,
    entry_fee_cents                 DECIMAL(10,4) NULL,
    exit_fee_cents                  DECIMAL(10,4) NULL,
    fee_cents                       DECIMAL(10,4) NULL,
    net_cents                       DECIMAL(10,4) NULL,
    running_total_net_cents         DECIMAL(12,4) NULL,
    running_drawdown_cents          DECIMAL(12,4) NULL,

    first_valid_signal_of_day       BOOLEAN NULL,
    clean_entry_quote               BOOLEAN NULL,
    clean_exit_quote                BOOLEAN NULL,
    exit_horizon_seconds            INT NOT NULL DEFAULT 120,
    exit_tolerance_seconds          INT NOT NULL DEFAULT 10,

    summary_text                    TEXT NULL,
    metadata_json                   JSON NULL,

    created_at                      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_bipt_profile_date (profile, et_date),
    INDEX idx_bipt_status (profile, status),
    INDEX idx_bipt_entry_at (entry_at),
    INDEX idx_bipt_market (market_ticker)
)
"""


def main() -> None:
    execute_query(CREATE_SQL)
    print("btc_impulse_paper_trades table ready")


if __name__ == "__main__":
    main()
