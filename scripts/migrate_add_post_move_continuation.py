#!/usr/bin/env python3
"""
migrate_add_post_move_continuation.py — Idempotent schema migration.

Creates `post_move_continuation_runs` and `post_move_continuation_signals`, the
dedicated tables for the post_move_continuation_scalp/v1 WATCH-ONLY research
hypothesis (scripts/post_move_continuation_backtest.py).  Results are stored
ENTIRELY SEPARATELY from live `paper_trades` and from the generic backtest_* /
followthrough_* tables.

One row is written per (watch-only signal × exit test).  Scalp-candidate signals
(ask < 0.85) get one row per exit test (test_a..test_d) with simulated outcomes;
0.85+ signals are logged as watch-only context (exit_test = 'context', outcome
columns NULL) and are NEVER actively scalped.

Safe to re-run: uses CREATE TABLE IF NOT EXISTS.

Run once before the first post-move backtest:
    python scripts/migrate_add_post_move_continuation.py

PAPER-ONLY / BACKTEST-ONLY RESEARCH.  Replay re-derives decisions from stored
snapshots and simulates fills (entry = ask + slippage, exit = bid - slippage).
No live trading is performed or enabled; no real order is ever placed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLES = ("post_move_continuation_runs", "post_move_continuation_signals")

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS post_move_continuation_runs (
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
    n_scalp_candidates  INT          DEFAULT 0,
    n_context_signals   INT          DEFAULT 0,
    n_test_rows         INT          DEFAULT 0,

    notes               TEXT,
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_pmcs_rule    (rule_name, rule_version),
    INDEX idx_pmcs_created (created_at)
)
"""

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS post_move_continuation_signals (
    id                          BIGINT        AUTO_INCREMENT PRIMARY KEY,
    run_id                      BIGINT        NOT NULL,
    rule_name                   VARCHAR(100)  NOT NULL,
    rule_version                VARCHAR(20)   NOT NULL,
    market_ticker               VARCHAR(100)  NOT NULL,
    signal_seq                  INT,

    side                        ENUM('YES', 'NO') NOT NULL,
    contract_ask                DECIMAL(6, 4),
    contract_bid                DECIMAL(6, 4),
    spread                      DECIMAL(6, 4),
    spread_bucket               VARCHAR(12),
    simulated_entry_price       DECIMAL(6, 4),
    price_bucket                VARCHAR(32),
    scalp_candidate             BOOLEAN       NOT NULL,

    btc_price                   DECIMAL(14, 2),
    target_price                DECIMAL(14, 2),
    raw_momentum_score          DECIMAL(10, 4),
    directional_momentum_score  DECIMAL(10, 4),
    raw_gap_z_score             DECIMAL(10, 6),
    directional_gap_z_score     DECIMAL(10, 6),

    contract_price_change_5s    DECIMAL(8, 4) NULL,
    contract_price_change_10s   DECIMAL(8, 4) NULL,
    contract_price_change_30s   DECIMAL(8, 4) NULL,
    contract_price_change_60s   DECIMAL(8, 4) NULL,

    time_remaining_seconds      INT,
    contract_age_seconds        INT,
    volatility_30s              DECIMAL(14, 4) NULL,
    volatility_60s              DECIMAL(14, 4) NULL,
    volatility_regime           VARCHAR(12),

    entry_time                  DATETIME(3),
    hour_block                  VARCHAR(8),
    day_name                    VARCHAR(12),
    timezone_used               VARCHAR(40),

    -- per exit-test simulation (NULL for context-only 0.85+ rows) ---------------
    exit_test                   VARCHAR(20)   NOT NULL,
    tp_abs                      DECIMAL(6, 4) NULL,
    sl_abs                      DECIMAL(6, 4) NULL,
    timeout_s                   DECIMAL(8, 3) NULL,
    slippage_mode               VARCHAR(12),

    hit_take_profit_before_stop BOOLEAN       NULL,
    hit_stop_before_take_profit BOOLEAN       NULL,
    timed_out                   BOOLEAN       NULL,
    max_favorable_excursion     DECIMAL(8, 4) NULL,
    max_adverse_excursion       DECIMAL(8, 4) NULL,
    time_to_peak                DECIMAL(8, 3) NULL,
    time_to_profit_target       DECIMAL(8, 3) NULL,
    simulated_pnl               DECIMAL(8, 4) NULL,
    simulated_pnl_percent       DECIMAL(8, 4) NULL,
    exit_reason_simulated       VARCHAR(40)   NULL,
    exit_time                   DATETIME(3)   NULL,
    n_updates                   INT           DEFAULT 0,

    created_at                  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_pmcss_run     (run_id),
    INDEX idx_pmcss_test    (run_id, exit_test),
    INDEX idx_pmcss_side    (run_id, side),
    INDEX idx_pmcss_bucket  (run_id, price_bucket),
    INDEX idx_pmcss_market  (market_ticker),

    CONSTRAINT fk_pmcss_run
        FOREIGN KEY (run_id) REFERENCES post_move_continuation_runs (id)
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
    print("\npost_move_continuation tables schema migration")
    print("=" * 48)
    print()

    existed = {t: _table_exists(t) for t in _TABLES}

    # runs must exist before signals (FK target).
    execute_query(_CREATE_RUNS)
    execute_query(_CREATE_SIGNALS)

    for t in _TABLES:
        if existed[t]:
            print(f"  {t:34s} already existed — no change")
        else:
            print(f"  {t:34s} ✓ created")

    print("\nMigration complete.")
    print(
        "post_move_continuation_backtest.py will populate these tables with\n"
        "simulated watch-only results, kept entirely separate from live\n"
        "paper_trades.  No paper trades are opened and no live trading is enabled.\n"
    )


if __name__ == "__main__":
    main()
