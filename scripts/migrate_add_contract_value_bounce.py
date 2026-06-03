#!/usr/bin/env python3
"""
migrate_add_contract_value_bounce.py — Idempotent schema migration.

Creates `contract_value_bounce_backtest_runs` and
`contract_value_bounce_backtest_signals`, the dedicated tables for the
contract_value_bounce_scalp/v1 WATCH-ONLY research hypothesis
(scripts/contract_value_bounce_backtest.py).

Results are stored ENTIRELY SEPARATELY from live `paper_trades` and from all
other backtest / followthrough / post-move tables.

Safe to re-run: uses CREATE TABLE IF NOT EXISTS.

Run once before the first contract-value-bounce backtest:
    python scripts/migrate_add_contract_value_bounce.py

PAPER-ONLY / BACKTEST-ONLY RESEARCH.  Replay re-derives decisions from stored
snapshots and simulates fills (entry = ask + slippage, exit = bid - slippage).
No live trading is performed or enabled; no real order is ever placed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLES = (
    "contract_value_bounce_backtest_runs",
    "contract_value_bounce_backtest_signals",
)

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS contract_value_bounce_backtest_runs (
    id                  BIGINT       AUTO_INCREMENT PRIMARY KEY,
    rule_name           VARCHAR(100) NOT NULL,
    rule_version        VARCHAR(20)  NOT NULL,
    slippage_mode       VARCHAR(12)  NOT NULL,
    exit_tests          JSON,
    params              JSON,
    timezone_used       VARCHAR(40),

    data_start          DATETIME(3),
    data_end            DATETIME(3),
    n_markets           INT          DEFAULT 0,
    n_snapshots         INT          DEFAULT 0,
    n_signals           INT          DEFAULT 0,
    n_test_rows         INT          DEFAULT 0,

    notes               TEXT,
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_cvbbr_rule    (rule_name, rule_version),
    INDEX idx_cvbbr_created (created_at)
)
"""

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS contract_value_bounce_backtest_signals (
    id                              BIGINT        AUTO_INCREMENT PRIMARY KEY,
    run_id                          BIGINT        NOT NULL,
    rule_name                       VARCHAR(100)  NOT NULL,
    rule_version                    VARCHAR(20)   NOT NULL,
    market_ticker                   VARCHAR(100)  NOT NULL,

    -- contract state at signal time ------------------------------------------
    side_bought                     ENUM('YES','NO') NOT NULL,
    winning_side                    ENUM('YES','NO') NOT NULL,
    losing_contract_ask             DECIMAL(6,4),
    losing_contract_bid             DECIMAL(6,4),
    losing_contract_spread          DECIMAL(6,4),
    losing_contract_low_since_open  DECIMAL(6,4)  NULL,
    losing_contract_bounce_from_low DECIMAL(8,4),
    losing_contract_mom_10s         DECIMAL(8,4)  NULL,
    price_bucket                    VARCHAR(20),
    bounce_bucket                   VARCHAR(20),
    spread_bucket                   VARCHAR(12),
    simulated_entry_price           DECIMAL(6,4),
    slippage_mode                   VARCHAR(12),

    -- BTC / market context ----------------------------------------------------
    btc_price                       DECIMAL(14,2) NULL,
    target_price                    DECIMAL(14,2) NULL,
    adverse_z_score                 DECIMAL(10,6) NULL,
    raw_momentum_score              DECIMAL(10,4) NULL,

    -- volatility --------------------------------------------------------------
    volatility_regime               VARCHAR(12),
    volatility_30s                  DECIMAL(14,4) NULL,
    volatility_60s                  DECIMAL(14,4) NULL,
    whipsaw_score                   DECIMAL(6,4)  NULL,

    -- timing ------------------------------------------------------------------
    market_age_seconds              INT,
    time_remaining_seconds          INT,
    entry_time                      DATETIME(3),
    hour_block                      VARCHAR(8),
    day_name                        VARCHAR(12),
    timezone_used                   VARCHAR(40),

    -- per exit-test simulation ------------------------------------------------
    exit_test                       VARCHAR(20)   NOT NULL,
    tp_abs                          DECIMAL(6,4)  NULL,
    sl_abs                          DECIMAL(6,4)  NULL,
    timeout_s                       DECIMAL(8,3)  NULL,

    hit_take_profit_before_stop     BOOLEAN       NULL,
    hit_stop_before_take_profit     BOOLEAN       NULL,
    timed_out                       BOOLEAN       NULL,
    max_favorable_excursion         DECIMAL(8,4)  NULL,
    max_adverse_excursion           DECIMAL(8,4)  NULL,
    time_to_peak                    DECIMAL(8,3)  NULL,
    time_to_profit_target           DECIMAL(8,3)  NULL,
    simulated_pnl                   DECIMAL(8,4)  NULL,
    simulated_pnl_percent           DECIMAL(8,4)  NULL,
    exit_reason_simulated           VARCHAR(40)   NULL,
    exit_time                       DATETIME(3)   NULL,
    n_updates                       INT           DEFAULT 0,

    created_at                      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_cvbbs_run      (run_id),
    INDEX idx_cvbbs_test     (run_id, exit_test),
    INDEX idx_cvbbs_side     (run_id, side_bought),
    INDEX idx_cvbbs_bucket   (run_id, price_bucket),
    INDEX idx_cvbbs_bounce   (run_id, bounce_bucket),
    INDEX idx_cvbbs_market   (market_ticker),

    CONSTRAINT fk_cvbbs_run
        FOREIGN KEY (run_id) REFERENCES contract_value_bounce_backtest_runs (id)
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
    print("\ncontract_value_bounce tables schema migration")
    print("=" * 48)
    print()

    existed = {t: _table_exists(t) for t in _TABLES}

    # runs must exist before signals (FK target).
    execute_query(_CREATE_RUNS)
    execute_query(_CREATE_SIGNALS)

    for t in _TABLES:
        if existed[t]:
            print(f"  {t:40s} already existed — no change")
        else:
            print(f"  {t:40s} ✓ created")

    print("\nMigration complete.")
    print(
        "contract_value_bounce_backtest.py will populate these tables with\n"
        "simulated watch-only results, kept entirely separate from live\n"
        "paper_trades.  No paper trades are opened and no live trading is enabled.\n"
    )


if __name__ == "__main__":
    main()
