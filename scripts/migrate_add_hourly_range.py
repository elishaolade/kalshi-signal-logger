#!/usr/bin/env python3
"""
migrate_add_hourly_range.py — Idempotent schema migration.

Creates two tables for the BTC hourly range market observability pipeline:

  hourly_range_markets
      One row per unique market ticker discovered by HourlyRangeTracker.
      Upserted on every poll.  Outcome fields are filled when the market closes.

  hourly_range_observations
      One row per (market_ticker, observed_at) poll sample.
      Records BTC position relative to the band, contract quotes, BTC features,
      and a simple containment-confidence estimate.

Safe to re-run: uses CREATE TABLE IF NOT EXISTS.

Run once before starting the logger with range-market tracking enabled:
    python scripts/migrate_add_hourly_range.py

No live trading is performed or enabled.  This is observability / research only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLES = ("hourly_range_markets", "hourly_range_observations")

# ── hourly_range_markets ──────────────────────────────────────────────────────
_CREATE_MARKETS = """
CREATE TABLE IF NOT EXISTS hourly_range_markets (
    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
    market_ticker       VARCHAR(100)  NOT NULL,
    event_ticker        VARCHAR(100)  NULL,
    series_ticker       VARCHAR(100)  NULL,
    title               VARCHAR(200)  NULL,

    -- Band geometry (immutable once discovered)
    floor_strike        DECIMAL(14,2) NOT NULL,
    cap_strike          DECIMAL(14,2) NOT NULL,
    band_width          DECIMAL(14,2) NOT NULL,
    band_center         DECIMAL(14,2) NOT NULL,

    open_time           DATETIME(3)   NULL,
    close_time          DATETIME(3)   NULL,

    -- Lifecycle
    status              VARCHAR(20)   NOT NULL DEFAULT 'open',  -- open | closed | settled

    -- Outcome (filled at settlement / close detection)
    final_btc_price     DECIMAL(14,2) NULL,
    contained           BOOLEAN       NULL,  -- TRUE if final BTC in [floor, cap]
    settled_at          DATETIME(3)   NULL,

    -- Summary stats computed at settlement
    n_observations           INT          NOT NULL DEFAULT 0,
    pct_time_inside          DECIMAL(6,4) NULL,  -- fraction of obs where inside_band=TRUE
    max_excursion_above_cap  DECIMAL(14,2) NULL, -- max(btc_price - cap_strike) when above cap
    max_excursion_below_floor DECIMAL(14,2) NULL, -- max(floor_strike - btc_price) when below floor

    first_seen_at       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_hrm_ticker (market_ticker),
    INDEX idx_hrm_event      (event_ticker),
    INDEX idx_hrm_close      (close_time),
    INDEX idx_hrm_status     (status)
)
"""

# ── hourly_range_observations ─────────────────────────────────────────────────
_CREATE_OBSERVATIONS = """
CREATE TABLE IF NOT EXISTS hourly_range_observations (
    id                      BIGINT        AUTO_INCREMENT PRIMARY KEY,
    market_ticker           VARCHAR(100)  NOT NULL,
    observed_at             DATETIME(3)   NOT NULL,

    -- Band geometry snapshot (denormalised for self-contained rows)
    floor_strike            DECIMAL(14,2) NOT NULL,
    cap_strike              DECIMAL(14,2) NOT NULL,
    band_width              DECIMAL(14,2) NOT NULL,
    band_center             DECIMAL(14,2) NOT NULL,

    -- BTC spot position
    btc_price               DECIMAL(14,2) NULL,
    distance_to_floor       DECIMAL(14,2) NULL,  -- btc - floor  (positive = above floor)
    distance_to_cap         DECIMAL(14,2) NULL,  -- cap - btc    (positive = below cap)
    distance_to_center      DECIMAL(14,2) NULL,  -- btc - center (signed)
    inside_band             BOOLEAN       NULL,
    norm_position           DECIMAL(8,4)  NULL,  -- (btc-floor)/band_width  0=floor 1=cap
    range_state             ENUM('below_range','inside_lower_half','inside_upper_half','above_range') NULL,

    -- Timing
    contract_age_seconds    INT           NULL,
    time_to_expiry_seconds  INT           NULL,

    -- Contract quotes (YES = "BTC stays in range")
    yes_bid                 DECIMAL(6,4)  NULL,
    yes_ask                 DECIMAL(6,4)  NULL,
    yes_mid                 DECIMAL(6,4)  NULL,
    yes_spread              DECIMAL(6,4)  NULL,
    last_price              DECIMAL(6,4)  NULL,
    volume                  DECIMAL(14,2) NULL,
    liquidity               DECIMAL(14,2) NULL,

    -- BTC features (derived from the in-memory btc_ticks buffer)
    btc_volatility_30s      DECIMAL(10,6) NULL,  -- rolling std over 30s window
    btc_volatility_60s      DECIMAL(10,6) NULL,  -- rolling std over 60s window
    btc_velocity_10s        DECIMAL(10,6) NULL,  -- mean price-change per sec, 10s window
    btc_velocity_30s        DECIMAL(10,6) NULL,  -- mean price-change per sec, 30s window

    -- Simple containment confidence
    -- P(final BTC in [floor,cap]) estimated from current vol projected forward.
    -- Formula: Phi(d_cap/sigma) + Phi(d_floor/sigma) - 1
    -- where sigma = btc_volatility_60s * sqrt(time_to_expiry / 60).
    -- Documented here so readers know the exact model.
    containment_confidence  DECIMAL(6,4)  NULL,

    created_at              TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_hro_ticker       (market_ticker),
    INDEX idx_hro_observed     (observed_at),
    INDEX idx_hro_ticker_time  (market_ticker, observed_at),
    INDEX idx_hro_state        (range_state),
    INDEX idx_hro_inside       (market_ticker, inside_band)
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
    print("\nhourly_range tables schema migration")
    print("=" * 42)
    print()

    existed = {t: _table_exists(t) for t in _TABLES}

    # Markets must exist before observations (FK not enforced but logical dependency).
    execute_query(_CREATE_MARKETS)
    execute_query(_CREATE_OBSERVATIONS)

    for t in _TABLES:
        if existed[t]:
            print(f"  {t:38s} already existed — no change")
        else:
            print(f"  {t:38s} ✓ created")

    print("\nMigration complete.")
    print(
        "HourlyRangeTracker will populate these tables with sampled\n"
        "observations of BTC hourly range markets.  No paper trades are\n"
        "opened and no live trading is enabled.\n"
    )


if __name__ == "__main__":
    main()
