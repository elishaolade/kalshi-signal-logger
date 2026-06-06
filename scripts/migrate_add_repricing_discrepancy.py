#!/usr/bin/env python3
"""
migrate_add_repricing_discrepancy.py — Idempotent schema migration.

Creates two tables for the BTC-Kalshi repricing discrepancy research hypothesis:

  repricing_discrepancy_runs
      One row per backtest execution.  Stores the full parameter grid and
      summary counts so results from multiple runs are comparable.

  repricing_discrepancy_events
      One row per detected burst event.  Contains the BTC burst size, the
      same-window contract move, underreaction flags for each X threshold,
      and forward repricing / MFE / MAE measurements for each Z window.

Hypothesis
----------
When BTC moves at least $N in one direction over 30 seconds, Kalshi contract
prices may not immediately adjust.  If they move less than X cents in the same
direction during that window ("underreaction"), does subsequent repricing in
the expected direction follow?

Run once before the first backtest:
    python scripts/migrate_add_repricing_discrepancy.py

RESEARCH ONLY — no live trading, no order execution.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLES = ("repricing_discrepancy_runs", "repricing_discrepancy_events")

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS repricing_discrepancy_runs (
    id                  BIGINT       AUTO_INCREMENT PRIMARY KEY,
    burst_window_s      INT          NOT NULL DEFAULT 30,
    n_thresholds        JSON,         -- dollar amounts e.g. [40, 50, 60]
    x_thresholds        JSON,         -- contract cents e.g. [0.01, 0.02, 0.03]
    z_windows           JSON,         -- forward seconds e.g. [10, 20, 30, 45, 60]
    slippage_mode       VARCHAR(12)  NOT NULL,
    contract_price_field VARCHAR(10) NOT NULL DEFAULT 'mid',
    cooldown_s          INT          NOT NULL DEFAULT 30,

    data_start          DATETIME(3),
    data_end            DATETIME(3),
    n_markets           INT          DEFAULT 0,
    n_burst_events      INT          DEFAULT 0,
    n_underreaction_x01 INT          DEFAULT 0,
    n_underreaction_x02 INT          DEFAULT 0,
    n_underreaction_x03 INT          DEFAULT 0,

    notes               TEXT,
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_rpdr_created (created_at)
)
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS repricing_discrepancy_events (
    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
    run_id              BIGINT        NOT NULL,
    market_ticker       VARCHAR(100)  NOT NULL,
    event_time          DATETIME(3)   NOT NULL,

    -- ── BTC burst during 30-second detection window ───────────────────────────
    implied_side        ENUM('YES','NO') NOT NULL,   -- YES=BTC up, NO=BTC down
    btc_price_start     DECIMAL(14,2) NULL,          -- BTC at window start
    btc_price_end       DECIMAL(14,2) NULL,          -- BTC at detection point
    btc_move            DECIMAL(10,2) NULL,          -- signed net move (+ = up)
    btc_move_abs        DECIMAL(10,2) NULL,          -- |btc_move|
    -- Threshold qualifications (the event fires at N=40; higher N is subset)
    qualified_n40       BOOLEAN       NOT NULL DEFAULT 1, -- always TRUE (min detection threshold)
    qualified_n50       BOOLEAN       NULL,
    qualified_n60       BOOLEAN       NULL,

    -- ── Contract reaction during same 30-second window ────────────────────────
    -- Uses mid_price of implied side.  Positive = moved in correct direction.
    contract_mid_start  DECIMAL(6,4)  NULL,
    contract_mid_end    DECIMAL(6,4)  NULL,
    contract_change     DECIMAL(8,4)  NULL,          -- mid_end - mid_start (directional)
    -- Underreaction flags: TRUE when contract_change < threshold
    underreaction_x01   BOOLEAN       NULL,          -- change < 0.01
    underreaction_x02   BOOLEAN       NULL,          -- change < 0.02
    underreaction_x03   BOOLEAN       NULL,          -- change < 0.03

    -- ── Market context at event detection ─────────────────────────────────────
    contract_ask        DECIMAL(6,4)  NULL,
    contract_bid        DECIMAL(6,4)  NULL,
    contract_spread     DECIMAL(6,4)  NULL,
    spread_bucket       VARCHAR(12)   NULL,          -- tight | normal | wide | very_wide
    time_remaining_s    INT           NULL,
    contract_age_s      INT           NULL,
    volatility_regime   VARCHAR(12)   NULL,          -- calm|normal|elevated|violent|unknown

    -- ── Simulated entry (for P&L measurement) ────────────────────────────────
    -- entry_sim = ask + slippage at event time; NULL if ask unavailable
    entry_sim           DECIMAL(6,4)  NULL,

    -- ── Forward mid repricing (implied side mid_price change) ─────────────────
    -- Positive = continued to move in expected direction
    fwd_mid_10s         DECIMAL(8,4)  NULL,
    fwd_mid_20s         DECIMAL(8,4)  NULL,
    fwd_mid_30s         DECIMAL(8,4)  NULL,
    fwd_mid_45s         DECIMAL(8,4)  NULL,
    fwd_mid_60s         DECIMAL(8,4)  NULL,

    -- ── Forward MFE (max favourable excursion, bid-based, entry_sim subtracted) ─
    -- max(bid_price - entry_sim) over each forward window; NULL if no entry_sim
    fwd_mfe_10s         DECIMAL(8,4)  NULL,
    fwd_mfe_20s         DECIMAL(8,4)  NULL,
    fwd_mfe_30s         DECIMAL(8,4)  NULL,
    fwd_mfe_45s         DECIMAL(8,4)  NULL,
    fwd_mfe_60s         DECIMAL(8,4)  NULL,

    -- ── Forward MAE (max adverse excursion, bid-based) ────────────────────────
    -- min(bid_price - entry_sim) over each forward window
    fwd_mae_10s         DECIMAL(8,4)  NULL,
    fwd_mae_20s         DECIMAL(8,4)  NULL,
    fwd_mae_30s         DECIMAL(8,4)  NULL,
    fwd_mae_45s         DECIMAL(8,4)  NULL,
    fwd_mae_60s         DECIMAL(8,4)  NULL,

    -- ── Simulated P&L at each Z-window horizon ───────────────────────────────
    -- bid(event + Z) - slippage - entry_sim; NULL if entry_sim NULL
    pnl_10s             DECIMAL(8,4)  NULL,
    pnl_20s             DECIMAL(8,4)  NULL,
    pnl_30s             DECIMAL(8,4)  NULL,
    pnl_45s             DECIMAL(8,4)  NULL,
    pnl_60s             DECIMAL(8,4)  NULL,

    created_at          TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_rpde_run         (run_id),
    INDEX idx_rpde_market      (market_ticker),
    INDEX idx_rpde_time        (event_time),
    INDEX idx_rpde_side        (run_id, implied_side),
    INDEX idx_rpde_n50         (run_id, qualified_n50),
    INDEX idx_rpde_n60         (run_id, qualified_n60),
    INDEX idx_rpde_under_x01   (run_id, underreaction_x01),
    INDEX idx_rpde_under_x02   (run_id, underreaction_x02),
    INDEX idx_rpde_under_x03   (run_id, underreaction_x03),

    CONSTRAINT fk_rpde_run
        FOREIGN KEY (run_id) REFERENCES repricing_discrepancy_runs (id)
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
    print("\nrepricing_discrepancy tables schema migration")
    print("=" * 48)
    print()

    existed = {t: _table_exists(t) for t in _TABLES}

    execute_query(_CREATE_RUNS)
    execute_query(_CREATE_EVENTS)

    for t in _TABLES:
        if existed[t]:
            print(f"  {t:42s} already existed — no change")
        else:
            print(f"  {t:42s} ✓ created")

    print("\nMigration complete.")
    print(
        "repricing_discrepancy_backtest.py will populate these tables.\n"
        "RESEARCH ONLY — no live trading, no orders.\n"
    )


if __name__ == "__main__":
    main()
