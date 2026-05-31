-- =============================================================
-- Kalshi BTC Signal Logger — schema
-- MySQL 8.0+
-- =============================================================

-- ---------------------------------------------------------------
-- 1. markets
--    One row per 15-minute up/down contract window.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS markets (
    id             BIGINT       AUTO_INCREMENT PRIMARY KEY,
    market_ticker  VARCHAR(100) NOT NULL,
    title          VARCHAR(500),
    target_price   DECIMAL(12, 2),          -- BTC target price for this window
    open_time      DATETIME,
    close_time     DATETIME,
    created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uq_market_ticker (market_ticker)
);

-- ---------------------------------------------------------------
-- 2. btc_ticks
--    Raw BTC price feed; market_ticker is nullable so ticks can
--    be stored before a market is matched/opened.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_ticks (
    id             BIGINT        AUTO_INCREMENT PRIMARY KEY,
    market_ticker  VARCHAR(100)  NULL,
    btc_price      DECIMAL(12, 2) NOT NULL,
    source         VARCHAR(100),            -- e.g. "coinbase", "binance"
    recorded_at    DATETIME(3)   NOT NULL,

    INDEX idx_btc_ticker_time (market_ticker, recorded_at),
    INDEX idx_btc_recorded_at (recorded_at),

    CONSTRAINT fk_btc_ticks_market
        FOREIGN KEY (market_ticker) REFERENCES markets (market_ticker)
        ON DELETE SET NULL ON UPDATE CASCADE
);

-- ---------------------------------------------------------------
-- 3. contract_ticks
--    Order-book snapshots for a specific market + side.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contract_ticks (
    id                     BIGINT       AUTO_INCREMENT PRIMARY KEY,
    market_ticker          VARCHAR(100) NOT NULL,
    side                   ENUM('YES', 'NO') NOT NULL,
    bid_price              DECIMAL(6, 4),
    ask_price              DECIMAL(6, 4),
    mid_price              DECIMAL(6, 4),
    last_price             DECIMAL(6, 4),
    spread                 DECIMAL(6, 4),
    volume                 INT,
    open_interest          INT,
    time_remaining_seconds INT,
    contract_age_seconds   INT,
    recorded_at            DATETIME(3)  NOT NULL,
    created_at             TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_ct_ticker_side_time (market_ticker, side, recorded_at),

    CONSTRAINT fk_contract_ticks_market
        FOREIGN KEY (market_ticker) REFERENCES markets (market_ticker)
        ON DELETE CASCADE ON UPDATE CASCADE
);

-- ---------------------------------------------------------------
-- 4. signals
--    One row per strategy rule firing. Stores the full feature
--    snapshot at signal time for later analysis.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id                     BIGINT        AUTO_INCREMENT PRIMARY KEY,
    market_ticker          VARCHAR(100)  NOT NULL,
    rule_name              VARCHAR(100)  NOT NULL,
    rule_version           VARCHAR(20)   NOT NULL,
    side                   ENUM('YES', 'NO') NOT NULL,

    -- contract state at signal time
    contract_price         DECIMAL(6, 4),
    bid_price              DECIMAL(6, 4),
    ask_price              DECIMAL(6, 4),
    spread                 DECIMAL(6, 4),

    -- BTC context
    btc_price              DECIMAL(12, 2),
    target_price           DECIMAL(12, 2),
    gap                    DECIMAL(10, 4),      -- btc_price - target_price
    directional_gap        DECIMAL(10, 4),      -- gap signed toward side
    gap_z_score            DECIMAL(10, 6),

    -- timing
    contract_age_seconds   INT,
    time_remaining_seconds INT,

    -- derived features
    momentum_score         DECIMAL(10, 6),
    reversal_score         DECIMAL(10, 6),
    btc_velocity           DECIMAL(12, 6),
    volatility_30s         DECIMAL(10, 6),
    volatility_60s         DECIMAL(10, 6),
    volatility_120s        DECIMAL(10, 6),

    -- optional enrichment (may be filled after the fact)
    edge                   DECIMAL(8, 4)  NULL,
    confidence_score       DECIMAL(5, 4)  NULL,
    reason                 TEXT,
    signal_status          VARCHAR(50)    DEFAULT 'paper',

    recorded_at            DATETIME(3)   NOT NULL,
    created_at             TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_sig_ticker_time  (market_ticker, recorded_at),
    INDEX idx_sig_rule         (rule_name, rule_version),
    INDEX idx_sig_status       (signal_status),

    CONSTRAINT fk_signals_market
        FOREIGN KEY (market_ticker) REFERENCES markets (market_ticker)
        ON DELETE CASCADE ON UPDATE CASCADE
);

-- ---------------------------------------------------------------
-- 5. paper_trades
--    Simulated entry/exit lifecycle for each signal.
--    No real orders are ever placed.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_trades (
    id                    BIGINT        AUTO_INCREMENT PRIMARY KEY,
    signal_id             BIGINT        NULL,
    market_ticker         VARCHAR(100)  NOT NULL,
    rule_name             VARCHAR(100)  NOT NULL,
    rule_version          VARCHAR(20)   NOT NULL,
    side                  ENUM('YES', 'NO') NOT NULL,

    -- prices
    entry_price           DECIMAL(6, 4),
    simulated_entry_price DECIMAL(6, 4),   -- mid at entry time (no fill assumption)
    exit_price            DECIMAL(6, 4),
    simulated_exit_price  DECIMAL(6, 4),
    peak_price            DECIMAL(6, 4),   -- best price seen while open
    lowest_price          DECIMAL(6, 4),   -- worst price seen while open

    -- timing
    entry_time            DATETIME(3),
    exit_time             DATETIME(3),
    exit_reason           VARCHAR(200),    -- e.g. "stop_loss", "take_profit", "expiry"

    -- performance
    pnl                   DECIMAL(10, 4),
    pnl_percent           DECIMAL(8, 4),
    final_outcome         ENUM('YES', 'NO', 'UNKNOWN') DEFAULT 'UNKNOWN',
    would_have_won        BOOLEAN,         -- did the market resolve in favor of side?
    followed_rules        BOOLEAN,

    -- sizing / friction
    position_size         DECIMAL(10, 2),  -- simulated contract count or notional
    total_slippage        DECIMAL(8, 4),

    status                ENUM('OPEN', 'CLOSED') DEFAULT 'OPEN',

    created_at            TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_pt_signal      (signal_id),
    INDEX idx_pt_ticker_rule (market_ticker, rule_name),
    INDEX idx_pt_entry_time  (entry_time),

    CONSTRAINT fk_paper_trades_signal
        FOREIGN KEY (signal_id) REFERENCES signals (id)
        ON DELETE SET NULL ON UPDATE CASCADE
);

-- ---------------------------------------------------------------
-- 6. trade_snapshots
--    Per-tick feature record captured while a paper trade is open.
--    Used to replay trade evolution and build training datasets.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_snapshots (
    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
    paper_trade_id      BIGINT        NOT NULL,
    signal_id           BIGINT        NULL,
    market_ticker       VARCHAR(100)  NOT NULL,
    side                ENUM('YES', 'NO') NOT NULL,

    -- BTC context
    btc_price           DECIMAL(12, 2),
    target_price        DECIMAL(12, 2),
    raw_gap             DECIMAL(10, 4),
    directional_gap     DECIMAL(10, 4),

    -- rolling volatility
    rolling_std_30s     DECIMAL(10, 6),
    rolling_std_60s     DECIMAL(10, 6),
    rolling_std_120s    DECIMAL(10, 6),

    -- z-scores vs target
    z_from_target_30s   DECIMAL(10, 6),
    z_from_target_60s   DECIMAL(10, 6),
    z_from_target_120s  DECIMAL(10, 6),

    -- contract state
    contract_price      DECIMAL(6, 4),
    bid_price           DECIMAL(6, 4),
    ask_price           DECIMAL(6, 4),
    spread              DECIMAL(6, 4),

    -- derived features
    momentum_score      DECIMAL(10, 6),
    reversal_score      DECIMAL(10, 6),
    btc_velocity        DECIMAL(12, 6),
    time_remaining_seconds INT,

    recorded_at         DATETIME(3)  NOT NULL,
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_ts_trade_time (paper_trade_id, recorded_at),
    INDEX idx_ts_signal     (signal_id),

    CONSTRAINT fk_ts_paper_trade
        FOREIGN KEY (paper_trade_id) REFERENCES paper_trades (id)
        ON DELETE CASCADE ON UPDATE CASCADE,

    CONSTRAINT fk_ts_signal
        FOREIGN KEY (signal_id) REFERENCES signals (id)
        ON DELETE SET NULL ON UPDATE CASCADE
);

-- ---------------------------------------------------------------
-- 7. strategy_versions
--    Registry of every rule/version combination with its logic
--    snapshot, so analysis scripts can join on exact parameters.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_versions (
    id           INT          AUTO_INCREMENT PRIMARY KEY,
    rule_name    VARCHAR(100) NOT NULL,
    rule_version VARCHAR(20)  NOT NULL,
    description  TEXT,
    entry_rules  JSON,
    exit_rules   JSON,
    is_active    BOOLEAN      DEFAULT FALSE,
    created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uq_rule_version (rule_name, rule_version)
);

-- ---------------------------------------------------------------
-- 8. signal_observations
--    Live shadow-tracking of watch-only research signals
--    (e.g. early_overextension_reversal_scalp/v1).  NO paper trade
--    is opened; the losing-side contract is followed for ~60s and
--    hypothetical outcomes + exit simulations are recorded here.
--    Reference price is the losing-side MID at signal time; all
--    excursions/exits are measured against it (no fill/slippage
--    assumption — this is observation, not trading).
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_observations (
    id                          BIGINT        AUTO_INCREMENT PRIMARY KEY,
    signal_id                   BIGINT        NULL,
    market_ticker               VARCHAR(100)  NOT NULL,
    rule_name                   VARCHAR(100)  NOT NULL,
    rule_version                VARCHAR(20)   NOT NULL,
    side                        ENUM('YES', 'NO') NOT NULL,   -- tracked (losing) side

    -- entry context (from the firing signal)
    winning_side                ENUM('YES', 'NO'),
    losing_side                 ENUM('YES', 'NO'),
    winning_change_60s          DECIMAL(8, 4),
    winning_dir_z               DECIMAL(10, 6),
    losing_bounce_from_low      DECIMAL(8, 4)  NULL,
    losing_mom_10s              DECIMAL(8, 4)  NULL,
    losing_ask_at_signal        DECIMAL(6, 4),
    market_age_seconds          INT,

    -- tracking reference + watermarks (measured on losing-side MID)
    entry_ref_price             DECIMAL(6, 4),
    entry_time                  DATETIME(3),
    peak_price                  DECIMAL(6, 4),
    low_price                   DECIMAL(6, 4),
    last_price                  DECIMAL(6, 4),
    n_updates                   INT            DEFAULT 0,

    -- hypothetical outcomes
    max_favorable_excursion     DECIMAL(8, 4),
    max_adverse_excursion       DECIMAL(8, 4),
    hit_plus_3c_before_minus_2c BOOLEAN,
    hit_plus_4c_before_minus_2c BOOLEAN,
    hit_plus_5c_before_minus_3c BOOLEAN,
    time_to_peak_s              DECIMAL(8, 3)  NULL,
    time_to_plus_3c_s           DECIMAL(8, 3)  NULL,
    time_to_stop_s              DECIMAL(8, 3)  NULL,   -- first time hit -2c
    did_make_new_low_after_signal BOOLEAN,

    -- exit simulations
    sim_tp3_sl2_outcome         VARCHAR(12)    NULL,   -- take_profit | stop_loss | timeout
    sim_tp3_sl2_pnl             DECIMAL(8, 4)  NULL,
    sim_tp5_sl3_outcome         VARCHAR(12)    NULL,
    sim_tp5_sl3_pnl             DECIMAL(8, 4)  NULL,
    sim_timeout60_pnl           DECIMAL(8, 4)  NULL,

    -- lifecycle
    status                      ENUM('ACTIVE', 'COMPLETE') DEFAULT 'ACTIVE',
    complete_reason             VARCHAR(50)    NULL,   -- timeout_60s | near_expiry | market_rollover | recovered_after_restart
    recorded_at                 DATETIME(3)    NOT NULL,  -- signal time
    completed_at                DATETIME(3)    NULL,
    created_at                  TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_so_signal (signal_id),
    INDEX idx_so_rule   (rule_name, rule_version),
    INDEX idx_so_status (status),

    CONSTRAINT fk_so_signal
        FOREIGN KEY (signal_id) REFERENCES signals (id)
        ON DELETE SET NULL ON UPDATE CASCADE
);
