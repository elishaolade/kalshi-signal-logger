#!/usr/bin/env python3
"""Create tables for the cheap minority Real-Money TEST tracker."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one


_TRADES = """
CREATE TABLE IF NOT EXISTS cheap_minority_test_trades (
    id                                  BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    test_id                             VARCHAR(100) NOT NULL,
    profile                             VARCHAR(100) NOT NULL,
    test_label                          VARCHAR(160),
    test_trade_number                   INT,

    et_date                             DATE NOT NULL,
    market_id                           BIGINT NOT NULL,
    contract_id                         BIGINT NOT NULL,
    market_ticker                       VARCHAR(100) NOT NULL,
    market_open_et                      DATETIME(3),
    market_close_et                     DATETIME(3),
    entry_signal_at                     DATETIME(3) NOT NULL,
    entry_at                            DATETIME(3),
    seconds_since_market_open           DECIMAL(10,3),

    side                                ENUM('YES','NO') NOT NULL,
    dominant_side                       ENUM('YES','NO'),
    minority_side                       ENUM('YES','NO'),
    entry_bid                           DECIMAL(10,4),
    entry_ask                           DECIMAL(10,4) NOT NULL,
    entry_spread                        DECIMAL(10,4),
    entry_quote_clean                   TINYINT NOT NULL DEFAULT 0,

    contracts_attempted                 INT NOT NULL,
    contracts_filled                    INT NOT NULL DEFAULT 0,
    entry_limit_price                   DECIMAL(10,4),
    entry_order_id                      VARCHAR(120),
    entry_client_order_id               VARCHAR(120),
    actual_avg_entry_price              DECIMAL(10,4),
    actual_entry_fees_dollars           DECIMAL(12,6),
    notional_cost_dollars               DECIMAL(12,6),
    account_balance_before_trade        DECIMAL(12,4),

    btc_price_at_market_open            DECIMAL(18,2),
    btc_price_60s_before_entry          DECIMAL(18,2),
    btc_price_at_entry                  DECIMAL(18,2),
    btc_60s_move                        DECIMAL(18,2),

    target_level                        DECIMAL(10,4) NOT NULL,
    target_hit_flag                     TINYINT NOT NULL DEFAULT 0,
    target_hit_at                       DATETIME(3),
    exit_at                             DATETIME(3),
    exit_bid_observed                   DECIMAL(10,4),
    exit_order_limit_price              DECIMAL(10,4),
    exit_order_id                       VARCHAR(120),
    exit_client_order_id                VARCHAR(120),
    actual_avg_exit_price               DECIMAL(10,4),
    exit_reason                         VARCHAR(40),

    modeled_gross_cents_per_contract    DECIMAL(10,4),
    modeled_fee_cents_per_contract      DECIMAL(10,4),
    modeled_net_cents_per_contract      DECIMAL(10,4),
    actual_fees_dollars                 DECIMAL(12,6),
    actual_net_dollars                  DECIMAL(12,6),
    actual_net_cents_per_contract       DECIMAL(10,4),
    account_balance_after_trade         DECIMAL(12,4),
    running_total_actual_net_dollars    DECIMAL(12,6),
    running_drawdown_dollars            DECIMAL(12,6),
    win_loss_flag                       VARCHAR(10),

    status                              VARCHAR(30) NOT NULL DEFAULT 'PENDING_ENTRY',
    rule_violation_flag                 TINYINT NOT NULL DEFAULT 0,
    rule_violation_reason               VARCHAR(500),
    notes                               VARCHAR(1000),
    metadata_json                       JSON,
    created_at                          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                    ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uniq_cmt_profile_date (profile, et_date),
    UNIQUE KEY uniq_cmt_entry_client_order_id (entry_client_order_id),
    INDEX idx_cmt_status (status),
    INDEX idx_cmt_signal_at (entry_signal_at),
    INDEX idx_cmt_market (market_id),
    INDEX idx_cmt_contract (contract_id),
    INDEX idx_cmt_profile (profile)
)
"""

_SKIPS = """
CREATE TABLE IF NOT EXISTS cheap_minority_test_skipped_days (
    id                                  BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    test_id                             VARCHAR(100) NOT NULL,
    profile                             VARCHAR(100) NOT NULL,
    et_date                             DATE NOT NULL,
    reason_no_trade                     VARCHAR(500) NOT NULL,
    markets_checked                     INT NOT NULL DEFAULT 0,
    eligible_signals_found              INT NOT NULL DEFAULT 0,
    order_attempted_flag                TINYINT NOT NULL DEFAULT 0,
    missed_fill_flag                    TINYINT NOT NULL DEFAULT 0,
    spread_violation_flag               TINYINT NOT NULL DEFAULT 0,
    insufficient_balance_flag           TINYINT NOT NULL DEFAULT 0,
    platform_feed_issue_flag            TINYINT NOT NULL DEFAULT 0,
    account_balance_at_end_of_day       DECIMAL(12,4),
    created_at                          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                    ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_cms_profile_date (profile, et_date),
    INDEX idx_cms_date (et_date)
)
"""

_ACCOUNT = """
CREATE TABLE IF NOT EXISTS cheap_minority_test_account_curve (
    id                                  BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    test_id                             VARCHAR(100) NOT NULL,
    profile                             VARCHAR(100) NOT NULL,
    et_date                             DATE,
    observed_at                         DATETIME(3) NOT NULL,
    trade_id                            BIGINT,
    account_balance_dollars             DECIMAL(12,4),
    running_total_actual_net_dollars    DECIMAL(12,6),
    running_drawdown_dollars            DECIMAL(12,6),
    event_type                          VARCHAR(40),
    created_at                          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_cma_profile_time (profile, observed_at),
    INDEX idx_cma_trade (trade_id)
)
"""

_AUDIT = """
CREATE TABLE IF NOT EXISTS cheap_minority_test_order_events (
    id                                  BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    test_id                             VARCHAR(100) NOT NULL,
    profile                             VARCHAR(100) NOT NULL,
    trade_id                            BIGINT,
    et_date                             DATE,
    event_at                            DATETIME(3) NOT NULL,
    event_type                          VARCHAR(60) NOT NULL,
    action                              VARCHAR(10),
    requested_count                     INT,
    filled_count                        INT,
    limit_price                         DECIMAL(10,4),
    avg_fill_price                      DECIMAL(10,4),
    fees_dollars                        DECIMAL(12,6),
    order_id                            VARCHAR(120),
    client_order_id                     VARCHAR(120),
    detail                              VARCHAR(500),
    raw_json                            JSON,
    created_at                          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_cmao_profile_time (profile, event_at),
    INDEX idx_cmao_trade (trade_id),
    INDEX idx_cmao_event (event_type)
)
"""

_DAILY = """
CREATE TABLE IF NOT EXISTS cheap_minority_test_daily_summaries (
    id                                  BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    test_id                             VARCHAR(100) NOT NULL,
    profile                             VARCHAR(100) NOT NULL,
    et_date                             DATE NOT NULL,
    summary_text                        TEXT NOT NULL,
    sent_flag                           TINYINT NOT NULL DEFAULT 0,
    created_at                          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_cmtds_profile_date (profile, et_date)
)
"""


def _exists(name: str) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (name,),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    tables = {
        "cheap_minority_test_trades": _TRADES,
        "cheap_minority_test_skipped_days": _SKIPS,
        "cheap_minority_test_account_curve": _ACCOUNT,
        "cheap_minority_test_order_events": _AUDIT,
        "cheap_minority_test_daily_summaries": _DAILY,
    }
    for name, sql in tables.items():
        existed = _exists(name)
        execute_query(sql)
        print(name, "already existed -- no change" if existed else "created")
    print("Migration complete. Cheap minority Real-Money TEST tables ready.")


if __name__ == "__main__":
    main()
