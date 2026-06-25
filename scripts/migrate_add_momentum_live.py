#!/usr/bin/env python3
"""
migrate_add_momentum_live.py — Idempotent schema migration for LIVE trading.

Creates:
    momentum_live_trades            — one row per real-money trade (entry+exit)
    momentum_live_order_events      — append-only order lifecycle log
    momentum_live_guardrail_events  — append-only risk-gate / pause / kill log
    momentum_live_pause_state       — single-row latch for automatic pause

Safe to re-run: CREATE TABLE IF NOT EXISTS.

Run once before starting the logger with the live trader enabled:
    python scripts/migrate_add_momentum_live.py

REAL-MONEY SUBSYSTEM.  These tables back app/momentum_live_trader.py, which is
gated behind explicit MOMENTUM_LIVE_* flags.  Creating the tables does NOT
enable live trading.

Projected-vs-actual convention (per trade)
------------------------------------------
All price / pnl fields are dollar fractions per contract (0.05 = 5 cents),
matching momentum_shadow_trades and the backtest.

    projected_*  = what the frozen shadow logic computed on the SAME observed
                   quotes (the "shadow of this live trade").
    actual_*     = what really happened at real fills.

Drift fields (each documented inline in the DDL):
    entry_price_drift_cents     = actual_entry_price - projected_entry_ask
    exit_price_drift_cents      = actual_exit_price  - projected_exit_bid
    profit_delta_cents          = actual_profit_cents - projected_profit_cents
    total_execution_drift_cents = projected_profit_cents - actual_profit_cents
                                  (adverse execution cost; +ve = execution hurt)
    profit_capture_ratio        = actual_profit_cents / projected_profit_cents
    expectancy_capture_ratio    = actual_profit_cents / projected_expectancy_cents
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

# ── Table DDL ─────────────────────────────────────────────────────────────────

_CREATE_LIVE_TRADES = """
CREATE TABLE IF NOT EXISTS momentum_live_trades (
    id                              BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,

    -- Market / contract identity (FK to v2 schema)
    market_id                       BIGINT          NOT NULL,   -- markets.id
    contract_id                     BIGINT          NOT NULL,   -- contracts.id
    market_ticker                   VARCHAR(100)    NOT NULL,
    side                            ENUM('YES','NO') NOT NULL,

    -- Optional link to the paired shadow trade (same signal/tick), if recorded.
    shadow_trade_id                 BIGINT,

    -- Signal / entry timing
    signal_at                       DATETIME(3)     NOT NULL,
    entry_at                        DATETIME(3),
    exit_at                         DATETIME(3),

    -- Frozen exit profile
    exit_profile                    VARCHAR(60)     NOT NULL DEFAULT 'ht120s_tp5c',
    exit_reason                     VARCHAR(40),
    holding_seconds                 DECIMAL(10,2),

    -- Sizing / bankroll context
    bankroll_at_entry               DECIMAL(14,2),
    kelly_fraction                  DECIMAL(8,4),
    kelly_full_fraction             DECIMAL(8,4),               -- computed full-Kelly stake fraction
    dollars_budgeted                DECIMAL(14,4),
    requested_contracts             INT             NOT NULL DEFAULT 0,
    filled_contracts                INT             NOT NULL DEFAULT 0,

    -- Order linkage (Kalshi ids / client ids)
    entry_order_id                  VARCHAR(80),
    entry_client_order_id           VARCHAR(80),
    exit_order_id                   VARCHAR(80),
    exit_client_order_id            VARCHAR(80),

    -- ── Projected (shadow-of-this-trade) values ──────────────────────────────
    projected_entry_ask             DECIMAL(10,4),
    projected_target_ask            DECIMAL(10,4),              -- entry_ask + 0.05
    projected_exit_bid              DECIMAL(10,4),
    projected_profit_cents          DECIMAL(10,4),              -- shadow net pnl / contract
    -- Strategy-level projected stats captured at entry (rolling shadow window):
    projected_expectancy_cents      DECIMAL(10,4),
    projected_win_rate              DECIMAL(8,4),               -- fraction 0..1
    projected_profit_factor         DECIMAL(10,4),
    projected_profit_loss_ratio     DECIMAL(10,4),              -- avg win / |avg loss|

    -- ── Actual (real fills) values ───────────────────────────────────────────
    actual_entry_price              DECIMAL(10,4),              -- avg fill (ask side)
    actual_exit_price               DECIMAL(10,4),              -- avg fill (bid side)
    actual_fees_cents               DECIMAL(10,4),
    actual_profit_cents             DECIMAL(10,4),              -- realized net pnl / contract
    actual_profit_dollars           DECIMAL(14,4),              -- realized net pnl total
    actual_trade_won                TINYINT(1),                 -- 1 win, 0 not, NULL n/a

    -- ── Live execution / WS instrumentation ────────────────────────────────
    ws_enabled                      TINYINT(1)      NOT NULL DEFAULT 0,
    ws_quote_age_at_entry           DECIMAL(10,4),
    ws_spread_at_entry              DECIMAL(10,4),
    ws_entry_best_bid               DECIMAL(10,4),
    ws_entry_best_ask               DECIMAL(10,4),
    ws_exit_best_bid                DECIMAL(10,4),
    ws_exit_spread                  DECIMAL(10,4),
    ws_avg_spread_during_trade      DECIMAL(10,4),
    ws_max_quote_age_during_trade   DECIMAL(10,4),
    max_bid_after_entry             DECIMAL(10,4),
    max_profit_cents                DECIMAL(10,4),
    time_to_max_bid_seconds         DECIMAL(10,2),
    seconds_above_1c                DECIMAL(10,2),
    seconds_above_2c                DECIMAL(10,2),
    seconds_above_3c                DECIMAL(10,2),
    seconds_above_4c                DECIMAL(10,2),
    seconds_above_5c                DECIMAL(10,2),
    bid_at_30s                      DECIMAL(10,4),
    bid_at_60s                      DECIMAL(10,4),
    bid_at_90s                      DECIMAL(10,4),
    bid_at_120s                     DECIMAL(10,4),
    went_green                      TINYINT(1),
    target_touched                  TINYINT(1),
    target_touch_count              INT             NOT NULL DEFAULT 0,
    target_first_touched_at         DATETIME(3),
    target_total_visible_seconds    DECIMAL(10,2),
    entry_fill_detected_by          VARCHAR(32),
    exit_fill_detected_by           VARCHAR(32),
    entry_signal_to_order_ms        INT,
    entry_order_to_ack_ms           INT,
    entry_ack_to_fill_ms            INT,
    fill_to_exit_order_ms           INT,
    exit_signal_to_order_ms         INT,

    -- ── Comparison / drift fields ────────────────────────────────────────────
    profit_delta_cents              DECIMAL(10,4),
    expectancy_delta_cents          DECIMAL(10,4),
    entry_price_drift_cents         DECIMAL(10,4),
    exit_price_drift_cents          DECIMAL(10,4),
    total_execution_drift_cents     DECIMAL(10,4),
    profit_capture_ratio            DECIMAL(10,4),
    expectancy_capture_ratio        DECIMAL(10,4),

    -- Lifecycle status:
    --   PENDING_ENTRY -> ACTIVE -> PENDING_EXIT -> COMPLETE
    --   terminal alternatives: REJECTED, CANCELED, UNEXECUTABLE
    status                          VARCHAR(20)     NOT NULL DEFAULT 'PENDING_ENTRY',

    metadata_json                   JSON,

    created_at                      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                    ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_mlt_contract  (contract_id),
    INDEX idx_mlt_market    (market_id),
    INDEX idx_mlt_ticker    (market_ticker),
    INDEX idx_mlt_signal_at (signal_at),
    INDEX idx_mlt_status    (status),
    INDEX idx_mlt_side      (side)
)
"""

_CREATE_ORDER_EVENTS = """
CREATE TABLE IF NOT EXISTS momentum_live_order_events (
    id                  BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    live_trade_id       BIGINT,                                 -- momentum_live_trades.id
    market_ticker       VARCHAR(100),
    side                ENUM('YES','NO'),
    -- entry_submitted, entry_filled, entry_partial, entry_canceled,
    -- entry_rejected, exit_submitted, exit_filled, exit_canceled, exit_rejected
    event_type          VARCHAR(40)     NOT NULL,
    order_id            VARCHAR(80),
    client_order_id     VARCHAR(80),
    action              VARCHAR(8),                             -- buy | sell
    requested_count     INT,
    filled_count        INT,
    limit_price         DECIMAL(10,4),
    avg_fill_price      DECIMAL(10,4),
    detail              VARCHAR(500),
    raw_json            JSON,
    created_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    INDEX idx_mloe_trade  (live_trade_id),
    INDEX idx_mloe_type   (event_type),
    INDEX idx_mloe_order  (order_id),
    INDEX idx_mloe_time   (created_at)
)
"""

_CREATE_GUARDRAIL_EVENTS = """
CREATE TABLE IF NOT EXISTS momentum_live_guardrail_events (
    id                  BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    -- kill_switch, paused, unpaused, blocked_max_active, blocked_spread,
    -- blocked_quote_stale, blocked_daily_loss, blocked_sizing,
    -- blocked_not_confirmed, blocked_paused, blocked_no_auth
    event_type          VARCHAR(50)     NOT NULL,
    market_ticker       VARCHAR(100),
    side                ENUM('YES','NO'),
    reason              VARCHAR(500),
    -- Snapshot of the metrics that triggered the event (window, gaps, etc.)
    metrics_json        JSON,
    created_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    INDEX idx_mlge_type (event_type),
    INDEX idx_mlge_time (created_at)
)
"""

_CREATE_PAUSE_STATE = """
CREATE TABLE IF NOT EXISTS momentum_live_pause_state (
    id                  TINYINT         NOT NULL PRIMARY KEY DEFAULT 1,
    is_paused           TINYINT(1)      NOT NULL DEFAULT 0,
    reason              VARCHAR(500),
    metrics_json        JSON,
    paused_at           DATETIME(3),
    unpaused_at         DATETIME(3),
    updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                        ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT chk_mlps_singleton CHECK (id = 1)
)
"""

# Seed the singleton pause row (unpaused).
_SEED_PAUSE_STATE = """
INSERT INTO momentum_live_pause_state (id, is_paused)
VALUES (1, 0)
ON DUPLICATE KEY UPDATE id = id
"""

_TABLES = [
    "momentum_live_trades",
    "momentum_live_order_events",
    "momentum_live_guardrail_events",
    "momentum_live_pause_state",
]

_DDL = {
    "momentum_live_trades":           _CREATE_LIVE_TRADES,
    "momentum_live_order_events":     _CREATE_ORDER_EVENTS,
    "momentum_live_guardrail_events": _CREATE_GUARDRAIL_EVENTS,
    "momentum_live_pause_state":      _CREATE_PAUSE_STATE,
}


def _table_exists(name: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (name,),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    print("\nmomentum LIVE trading schema migration")
    print("=" * 48)
    print()

    existed = {t: _table_exists(t) for t in _TABLES}

    for t in _TABLES:
        execute_query(_DDL[t])
    execute_query(_SEED_PAUSE_STATE)

    for t in _TABLES:
        status = "already existed -- no change" if existed[t] else "created"
        print(f"  {t:34s} {status}")

    print()
    print("Migration complete.")
    print(
        "\nThis does NOT enable live trading. To run the live trader you must set,"
        "\nin addition to the usual logger env:\n"
        "  MOMENTUM_LIVE_ENABLED=true\n"
        "  MOMENTUM_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY\n"
        "  KALSHI_KEY_ID=... and KALSHI_KEY_FILE=...\n"
        "  MOMENTUM_LIVE_BANKROLL_DOLLARS, MOMENTUM_LIVE_KELLY_FRACTION,\n"
        "  MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE  (all > 0)\n"
        "\nInspect live trades:\n"
        "  SELECT * FROM momentum_live_trades ORDER BY signal_at DESC LIMIT 50;\n"
        "Inspect live-vs-shadow comparison:\n"
        "  python scripts/momentum_live_report.py\n"
    )


if __name__ == "__main__":
    main()
