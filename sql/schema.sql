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


-- ---------------------------------------------------------------
-- 9. clc_reversal_observations
--    Live shadow-tracking for cheap_losing_contract_reversal_trail/v1
--    (WATCH-ONLY research — NO paper trade, NO live order is opened).
--
--    One row per (signal x exit_profile): the strategy buys the cheap
--    *losing* contract and we simulate FIVE exit profiles (2 fixed +
--    3 ride-then-trail) against the same observed price path.
--
--    Fill model (NOT mid):  entry = ask + slippage,  exit = bid - slippage.
--    Path-level fields (race outcomes, MFE/MAE, peak) are identical across
--    the 5 profile rows of one signal; the row with is_primary_path_row = 1
--    is the canonical one used by the self-referential reversal-probability
--    lookup.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clc_reversal_observations (
    id                              BIGINT        AUTO_INCREMENT PRIMARY KEY,
    signal_id                       BIGINT        NULL,
    market_ticker                   VARCHAR(100)  NOT NULL,
    rule_name                       VARCHAR(100)  NOT NULL,
    rule_version                    VARCHAR(20)   NOT NULL,

    -- setup classification
    setup_type                      VARCHAR(80)   NOT NULL,   -- bucketed setup signature
    market_phase                    VARCHAR(20),              -- early | mid | late
    market_age_seconds              INT,
    time_remaining_seconds          INT,

    -- sides
    side_bought                     ENUM('YES', 'NO') NOT NULL,  -- the losing (cheap) side
    winning_side                    ENUM('YES', 'NO'),
    losing_side                     ENUM('YES', 'NO'),

    -- BTC context at signal
    btc_price                       DECIMAL(14, 2),
    target_price                    DECIMAL(14, 2),
    raw_gap_z_score                 DECIMAL(10, 6),
    adverse_z_score                 DECIMAL(10, 6),
    raw_momentum_score              DECIMAL(8, 4),
    adverse_directional_momentum_score DECIMAL(8, 4),

    -- losing-contract quotes at signal
    losing_contract_ask             DECIMAL(6, 4),
    losing_contract_bid             DECIMAL(6, 4),
    losing_contract_spread          DECIMAL(6, 4),
    losing_contract_low_since_open  DECIMAL(6, 4),
    losing_contract_bounce_from_low DECIMAL(8, 4),

    -- historical reversal probability (self-referential lookup)
    historical_reversal_probability DECIMAL(6, 4)  NULL,
    similar_sample_count            INT            DEFAULT 0,
    confidence_label                VARCHAR(20),              -- insufficient_data | weak_sample | usable_sample | stronger_sample

    -- regime
    volatility_regime               VARCHAR(12),              -- calm | normal | elevated | violent | unknown
    whipsaw_score                   DECIMAL(6, 4)  NULL,
    hour_block                      VARCHAR(8),               -- "HH:00"
    day_name                        VARCHAR(12),

    -- fill model
    slippage_mode                   VARCHAR(12),
    entry_price_simulated           DECIMAL(6, 4),            -- ask + slippage

    -- per-profile exit
    exit_profile                    VARCHAR(40)   NOT NULL,   -- fixed_20pct_stop_15pct | ... | ride_then_trail_20pct_1c
    is_primary_path_row             BOOLEAN       DEFAULT 0,  -- 1 on exactly one profile row per signal
    trail_activated                 BOOLEAN       NULL,
    peak_contract_price             DECIMAL(6, 4),            -- highest bid seen (path-level)
    max_favorable_excursion         DECIMAL(8, 4),            -- max(bid - entry)
    max_adverse_excursion           DECIMAL(8, 4),            -- min(bid - entry)
    time_to_peak                    DECIMAL(8, 3)  NULL,
    exit_price_simulated            DECIMAL(6, 4)  NULL,      -- bid - slippage at exit
    exit_reason                     VARCHAR(40)    NULL,      -- take_profit | stop_loss | hard_stop | trailing_stop | timeout | violent_vol_exit | near_expiry | market_rollover | recovered_after_restart
    pnl                             DECIMAL(8, 4)  NULL,      -- exit_sim - entry_sim (dollars)
    pnl_percent                     DECIMAL(8, 4)  NULL,      -- pnl / entry_sim

    -- path-level race outcomes (duplicated across profiles; read from primary row)
    hit_plus_2c_before_minus_2c     BOOLEAN        NULL,
    hit_plus_3c_before_minus_2c     BOOLEAN        NULL,
    hit_plus_4c_before_minus_3c     BOOLEAN        NULL,

    n_updates                       INT            DEFAULT 0,

    -- lifecycle
    status                          ENUM('ACTIVE', 'COMPLETE') DEFAULT 'ACTIVE',
    complete_reason                 VARCHAR(50)    NULL,
    recorded_at                     DATETIME(3)    NOT NULL,
    completed_at                    DATETIME(3)    NULL,
    created_at                      TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_clc_signal  (signal_id),
    INDEX idx_clc_rule    (rule_name, rule_version),
    INDEX idx_clc_status  (status),
    INDEX idx_clc_profile (exit_profile),
    INDEX idx_clc_setup   (setup_type),
    INDEX idx_clc_primary (is_primary_path_row, status),

    CONSTRAINT fk_clc_signal
        FOREIGN KEY (signal_id) REFERENCES signals (id)
        ON DELETE SET NULL ON UPDATE CASCADE
);


-- ---------------------------------------------------------------
-- 10. backtest_runs
--     One row per historical-replay invocation.  Backtest results are
--     stored ENTIRELY SEPARATELY from live paper_trades — these tables
--     never mix with the live trading lifecycle.
--
--     PAPER-ONLY RESEARCH.  Replay never places or enables real orders;
--     it re-derives strategy decisions from stored market snapshots and
--     simulates fills (entry = ask + slippage, exit = bid - slippage).
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_runs (
    id              BIGINT       AUTO_INCREMENT PRIMARY KEY,
    rule_name       VARCHAR(100) NOT NULL,
    rule_version    VARCHAR(20)  NOT NULL,
    slippage_mode   VARCHAR(12)  NOT NULL,          -- optimistic | realistic | harsh
    exit_profiles   JSON,                           -- profile names + params simulated
    params          JSON,                           -- cooldown, timezone, gates, limits …
    timezone_used   VARCHAR(40),

    -- coverage of the replayed snapshot window
    data_start      DATETIME(3),
    data_end        DATETIME(3),
    n_markets       INT          DEFAULT 0,
    n_snapshots     INT          DEFAULT 0,
    n_signals       INT          DEFAULT 0,         -- distinct entries taken
    n_trades        INT          DEFAULT 0,         -- rows written to backtest_trades

    notes           TEXT,
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_btr_rule    (rule_name, rule_version),
    INDEX idx_btr_created (created_at)
);

-- ---------------------------------------------------------------
-- 11. backtest_trades
--     One row per (replayed signal x exit_profile).  Each entry is
--     simulated independently under every exit profile against the same
--     forward bid path, so profiles can be compared on identical entries.
--
--     Fill model (NOT mid):  entry = ask + slippage,  exit = bid - slippage.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_trades (
    id                          BIGINT        AUTO_INCREMENT PRIMARY KEY,
    run_id                      BIGINT        NOT NULL,
    rule_name                   VARCHAR(100)  NOT NULL,
    rule_version                VARCHAR(20)   NOT NULL,
    market_ticker               VARCHAR(100)  NOT NULL,
    signal_seq                  INT,                       -- nth entry within this run

    -- setup classification (mirrors clc_reversal_observations where applicable)
    setup_type                  VARCHAR(80),
    market_phase                VARCHAR(20),
    market_age_seconds          INT,
    time_remaining_seconds      INT,

    -- sides
    side_bought                 ENUM('YES', 'NO') NOT NULL,  -- the losing (cheap) side
    winning_side                ENUM('YES', 'NO'),
    losing_side                 ENUM('YES', 'NO'),

    -- BTC context at entry
    btc_price                   DECIMAL(14, 2),
    target_price                DECIMAL(14, 2),
    raw_gap_z_score             DECIMAL(10, 6),
    adverse_z_score             DECIMAL(10, 6),

    -- losing-contract quotes at entry
    losing_contract_ask         DECIMAL(6, 4),
    losing_contract_bid         DECIMAL(6, 4),
    losing_contract_spread      DECIMAL(6, 4),

    -- regime
    volatility_regime           VARCHAR(12),
    whipsaw_score               DECIMAL(6, 4)  NULL,

    -- time-of-day (computed from entry timestamp in timezone_used)
    entry_time                  DATETIME(3),
    entry_date                  VARCHAR(10),
    entry_hour                  INT,
    entry_day_of_week           INT,
    entry_day_name              VARCHAR(12),
    entry_hour_block            VARCHAR(8),

    -- fill model
    slippage_mode               VARCHAR(12),
    entry_price_simulated       DECIMAL(6, 4),            -- ask + slippage
    entry_bid                   DECIMAL(6, 4),            -- bid at entry (path reference)

    -- per-profile exit
    exit_profile                VARCHAR(40)   NOT NULL,
    trail_activated             BOOLEAN       NULL,
    peak_contract_price         DECIMAL(6, 4),            -- highest bid seen
    max_favorable_excursion     DECIMAL(8, 4),
    max_adverse_excursion       DECIMAL(8, 4),
    time_to_peak                DECIMAL(8, 3)  NULL,
    exit_time                   DATETIME(3)    NULL,
    exit_price_simulated        DECIMAL(6, 4)  NULL,      -- bid - slippage at exit
    exit_reason                 VARCHAR(40)    NULL,
    pnl                         DECIMAL(8, 4)  NULL,      -- exit_sim - entry_sim
    pnl_percent                 DECIMAL(8, 4)  NULL,      -- pnl / entry_sim

    n_updates                   INT            DEFAULT 0,
    created_at                  TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_btt_run     (run_id),
    INDEX idx_btt_profile (run_id, exit_profile),
    INDEX idx_btt_market  (market_ticker),

    CONSTRAINT fk_btt_run
        FOREIGN KEY (run_id) REFERENCES backtest_runs (id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

-- ---------------------------------------------------------------
-- 12. followthrough_backtest_runs / followthrough_backtest_trades
--     Dedicated tables for the follow-through filter hypothesis test
--     (scripts/followthrough_backtest.py).  Stored ENTIRELY SEPARATELY
--     from live paper_trades and from the generic backtest_* tables.
--
--     One backtest produces ALL base entries (premium NO continuation,
--     0.65–0.80) with a followthrough_confirmed flag; the report derives
--     Variant A (all base entries) vs Variant B (followthrough_confirmed only).
--     One trade row per (base entry x exit_profile).
--
--     Fill model (NOT mid):  entry = ask + slippage,  exit = bid - slippage.
--     PAPER-ONLY / BACKTEST-ONLY RESEARCH — no live trading, no order execution.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS followthrough_backtest_runs (
    id                  BIGINT       AUTO_INCREMENT PRIMARY KEY,
    rule_name           VARCHAR(100) NOT NULL,          -- premium_no_continuation_065_080_v2
    rule_version        VARCHAR(20)  NOT NULL,
    slippage_mode       VARCHAR(12)  NOT NULL,          -- optimistic | realistic | harsh
    exit_profiles       JSON,                           -- profile names + params simulated
    params              JSON,                           -- gates, baseline, limits, windows …
    timezone_used       VARCHAR(40),
    baseline_volume_60s DECIMAL(14, 4) NULL,            -- run-level volume baseline (if used)

    data_start          DATETIME(3),
    data_end            DATETIME(3),
    n_markets           INT          DEFAULT 0,
    n_snapshots         INT          DEFAULT 0,
    n_base_entries      INT          DEFAULT 0,         -- Variant A entry count
    n_confirmed_entries INT          DEFAULT 0,         -- Variant B entry count
    n_trades            INT          DEFAULT 0,         -- rows written (entries x profiles)

    notes               TEXT,
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_ftr_rule    (rule_name, rule_version),
    INDEX idx_ftr_created (created_at)
);

CREATE TABLE IF NOT EXISTS followthrough_backtest_trades (
    id                          BIGINT        AUTO_INCREMENT PRIMARY KEY,
    run_id                      BIGINT        NOT NULL,
    rule_name                   VARCHAR(100)  NOT NULL,
    rule_version                VARCHAR(20)   NOT NULL,
    market_ticker               VARCHAR(100)  NOT NULL,
    signal_seq                  INT,                       -- nth base entry within this run

    -- base entry context
    side_bought                 ENUM('YES', 'NO') NOT NULL,
    market_phase                VARCHAR(20),
    market_age_seconds          INT,
    time_remaining_seconds      INT,
    btc_price                   DECIMAL(14, 2),
    target_price                DECIMAL(14, 2),
    raw_gap_z_score             DECIMAL(10, 6),
    directional_momentum        DECIMAL(10, 4),

    -- NO contract quotes at entry
    no_ask                      DECIMAL(6, 4),
    no_bid                      DECIMAL(6, 4),
    no_mid                      DECIMAL(6, 4),
    spread                      DECIMAL(6, 4),
    spread_bucket               VARCHAR(12),
    volatility_regime           VARCHAR(12),

    -- follow-through features (lookahead-safe, computed up to entry ts)
    no_price_change_10s          DECIMAL(8, 4)  NULL,
    no_price_change_30s          DECIMAL(8, 4)  NULL,
    no_recent_high_30s           DECIMAL(6, 4)  NULL,
    no_pullback_from_recent_high DECIMAL(8, 4)  NULL,
    quote_update_count_60s       INT            NULL,
    volume_60s                   DECIMAL(14, 4) NULL,
    baseline_volume_60s          DECIMAL(14, 4) NULL,
    participation_basis          VARCHAR(12)    NULL,    -- volume | quote_count
    followthrough_confirmed      BOOLEAN        NOT NULL,
    followthrough_failed         BOOLEAN        NOT NULL,
    scalping_valid_window        BOOLEAN        NULL,

    -- time-of-day (computed from entry timestamp in timezone_used)
    entry_time                  DATETIME(3),
    entry_date                  VARCHAR(10),
    entry_hour                  INT,
    entry_day_of_week           INT,
    entry_day_name              VARCHAR(12),
    entry_hour_block            VARCHAR(8),
    entry_is_weekend            BOOLEAN        NULL,

    -- fill model
    slippage_mode               VARCHAR(12),
    entry_price_simulated       DECIMAL(6, 4),            -- ask + slippage
    entry_bid                   DECIMAL(6, 4),            -- bid at entry (path reference)

    -- per-profile exit
    exit_profile                VARCHAR(40)   NOT NULL,
    peak_contract_price         DECIMAL(6, 4),
    max_favorable_excursion     DECIMAL(8, 4),
    max_adverse_excursion       DECIMAL(8, 4),
    time_to_peak                DECIMAL(8, 3)  NULL,
    exit_time                   DATETIME(3)    NULL,
    exit_price_simulated        DECIMAL(6, 4)  NULL,      -- bid - slippage at exit
    exit_reason                 VARCHAR(40)    NULL,
    pnl                         DECIMAL(8, 4)  NULL,      -- exit_sim - entry_sim
    pnl_percent                 DECIMAL(8, 4)  NULL,

    n_updates                   INT            DEFAULT 0,
    created_at                  TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_ftt_run        (run_id),
    INDEX idx_ftt_profile    (run_id, exit_profile),
    INDEX idx_ftt_confirmed  (run_id, followthrough_confirmed),
    INDEX idx_ftt_market     (market_ticker),

    CONSTRAINT fk_ftt_run
        FOREIGN KEY (run_id) REFERENCES followthrough_backtest_runs (id)
        ON DELETE CASCADE ON UPDATE CASCADE
);


-- ============================================================================
-- 13. post_move_continuation_runs / post_move_continuation_signals
--     WATCH-ONLY research for post_move_continuation_scalp/v1.  Tests whether a
--     contract is profitable to scalp AFTER it begins trending upward.  Stored
--     entirely separately from live paper_trades.  One row per
--     (watch-only signal × exit test); scalp candidates (ask < 0.85) get
--     test_a..test_d outcomes, 0.85+ signals are logged as context only.
--     PAPER-ONLY / BACKTEST-ONLY — no live trading, no order execution.
-- ============================================================================

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
);

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
    simulated_entry_price       DECIMAL(6, 4),           -- ask + slippage
    price_bucket                VARCHAR(32),
    scalp_candidate             BOOLEAN       NOT NULL,   -- ask < 0.85

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
    exit_test                   VARCHAR(20)   NOT NULL,   -- test_a..test_d | context
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
    simulated_pnl               DECIMAL(8, 4) NULL,       -- exit_sim - entry_sim
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
);


-- ============================================================================
-- 14. dcvrb_observations
--     WATCH-ONLY research for delayed_contract_value_reversal_bounce/v1.
--     Tracks whether a losing contract that collapsed early into the 0.20–0.40
--     range produces a short +3¢–+6¢ bounce in contract value after a flush.
--
--     One row per (signal × comparison_version × exit_test) = 12 rows per signal.
--     All 12 rows share the same forward bid path from signal time.
--
--     Comparison versions:
--       v1  immediate   — hypothetical entry at watch_start_contract_ask + slippage
--       v2  2c+1c       — entry at signal-time ask + slippage (main signal)
--       v3  5c+2c       — same entry as v2 but only when flush ≥ 5¢ AND bounce ≥ 2¢
--
--     Exit tests:
--       test_a  tp +0.03  sl -0.02  timeout  45s
--       test_b  tp +0.04  sl -0.03  timeout  60s
--       test_c  tp +0.05  sl -0.03  timeout  75s
--       test_d  tp +0.06  sl -0.04  timeout  90s
--
--     Fill model: entry = ask + slippage,  exit = bid - slippage.
--     PAPER-ONLY — no live trading, no order execution.
-- ============================================================================

CREATE TABLE IF NOT EXISTS dcvrb_observations (
    id                              BIGINT        AUTO_INCREMENT PRIMARY KEY,
    signal_id                       BIGINT        NULL,
    market_ticker                   VARCHAR(100)  NOT NULL,
    rule_name                       VARCHAR(100)  NOT NULL,
    rule_version                    VARCHAR(20)   NOT NULL,
    side                            ENUM('YES', 'NO') NOT NULL,  -- losing (tracked) side

    -- Stage 1 watch-state context (from signal.extra)
    contract_open_price             DECIMAL(6, 4)  NULL,
    contract_recent_high            DECIMAL(6, 4)  NULL,
    watch_start_contract_ask        DECIMAL(6, 4)  NULL,
    watch_start_contract_bid        DECIMAL(6, 4)  NULL,
    drop_from_open                  DECIMAL(8, 4)  NULL,
    drop_from_recent_high           DECIMAL(8, 4)  NULL,
    spread_at_watch                 DECIMAL(6, 4)  NULL,
    volatility_60s_at_watch         DECIMAL(10, 6) NULL,

    -- Stage 2/3 flush + bounce metrics
    local_low_since_watch           DECIMAL(6, 4)  NULL,
    drop_from_watch_start           DECIMAL(8, 4)  NULL,
    extra_flush                     DECIMAL(8, 4)  NULL,
    bounce_from_local_low           DECIMAL(8, 4)  NULL,
    extra_flush_bucket              VARCHAR(12)    NULL,   -- 0.02-0.03 | 0.03-0.05 | 0.05+
    bounce_bucket                   VARCHAR(12)    NULL,   -- 0.01-0.02 | 0.02+
    strong_bounce                   BOOLEAN        NULL,
    price_bucket                    VARCHAR(12)    NULL,   -- 0.20-0.25 | 0.25-0.30 | 0.30-0.35 | 0.35-0.40

    -- Signal-time contract quotes
    contract_ask                    DECIMAL(6, 4)  NULL,
    contract_bid                    DECIMAL(6, 4)  NULL,
    spread                          DECIMAL(6, 4)  NULL,

    -- Contract price changes at signal time (losing-side mid series)
    contract_price_change_5s        DECIMAL(8, 4)  NULL,
    contract_price_change_10s       DECIMAL(8, 4)  NULL,
    contract_price_change_30s       DECIMAL(8, 4)  NULL,

    -- Timing
    market_age_seconds              INT            NULL,
    time_remaining_seconds          INT            NULL,

    -- BTC context (secondary / informational)
    btc_price                       DECIMAL(14, 2) NULL,
    target_price                    DECIMAL(14, 2) NULL,
    raw_gap_z_score                 DECIMAL(10, 6) NULL,
    directional_gap_z_score         DECIMAL(10, 6) NULL,   -- YES=raw, NO=-raw
    raw_momentum_score              DECIMAL(10, 4) NULL,
    directional_momentum_score      DECIMAL(10, 4) NULL,   -- YES=raw, NO=-raw
    btc_velocity_10s                DECIMAL(12, 6) NULL,
    btc_velocity_30s                DECIMAL(12, 6) NULL,
    volatility_30s                  DECIMAL(10, 6) NULL,
    volatility_60s                  DECIMAL(10, 6) NULL,
    volatility_regime               VARCHAR(12)    NULL,   -- calm | normal | elevated | violent | unknown
    hour_block                      VARCHAR(8)     NULL,   -- "HH:00"
    day_name                        VARCHAR(12)    NULL,
    timezone_used                   VARCHAR(40)    NULL,

    -- Entry timing comparison version
    comparison_version              VARCHAR(4)     NOT NULL,  -- v1 | v2 | v3
    v3_qualified                    BOOLEAN        NOT NULL DEFAULT 0,  -- TRUE only when flush≥5c AND bounce≥2c

    -- Fill model
    slippage_mode                   VARCHAR(12)    NULL,
    entry_price_simulated           DECIMAL(6, 4)  NULL,   -- ask at entry + slippage

    -- Exit test definition
    exit_test                       VARCHAR(8)     NOT NULL,  -- test_a | test_b | test_c | test_d
    tp_abs                          DECIMAL(6, 4)  NULL,      -- take-profit offset (dollars)
    sl_abs                          DECIMAL(6, 4)  NULL,      -- stop-loss offset   (dollars)
    timeout_s                       DECIMAL(8, 3)  NULL,      -- max hold time (seconds)

    -- v1 pre-signal fields
    v1_pre_signal_mae               DECIMAL(8, 4)  NULL,   -- (local_low - exit_slip) - v1_entry
    v1_stopped_out_pre_signal       BOOLEAN        NULL,   -- TRUE if SL was hit before signal

    -- Exit simulation results (filled by tracker)
    hit_take_profit_before_stop     BOOLEAN        NULL,
    hit_stop_before_take_profit     BOOLEAN        NULL,
    timed_out                       BOOLEAN        NULL,
    structure_stop_hit              BOOLEAN        NULL,   -- bid broke below local_low after entry
    max_favorable_excursion         DECIMAL(8, 4)  NULL,   -- max(bid - entry_sim)
    max_adverse_excursion           DECIMAL(8, 4)  NULL,   -- min(bid - entry_sim)
    time_to_peak                    DECIMAL(8, 3)  NULL,
    time_to_profit_target           DECIMAL(8, 3)  NULL,
    simulated_pnl                   DECIMAL(8, 4)  NULL,   -- exit_sim - entry_sim
    simulated_pnl_percent           DECIMAL(8, 4)  NULL,   -- pnl / entry_sim
    exit_price_simulated            DECIMAL(6, 4)  NULL,   -- bid at exit - slippage
    exit_reason_simulated           VARCHAR(40)    NULL,   -- take_profit | stop_loss | timeout | near_expiry | ... | stopped_out_pre_signal | v3_not_qualified

    n_updates                       INT            DEFAULT 0,

    -- Lifecycle
    status                          ENUM('ACTIVE', 'COMPLETE') DEFAULT 'ACTIVE',
    complete_reason                 VARCHAR(50)    NULL,
    recorded_at                     DATETIME(3)    NOT NULL,
    completed_at                    DATETIME(3)    NULL,
    created_at                      TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_dcvrb_signal   (signal_id),
    INDEX idx_dcvrb_rule     (rule_name, rule_version),
    INDEX idx_dcvrb_status   (status),
    INDEX idx_dcvrb_version  (comparison_version),
    INDEX idx_dcvrb_test     (exit_test),
    INDEX idx_dcvrb_bucket   (price_bucket, extra_flush_bucket),
    INDEX idx_dcvrb_side     (side, comparison_version, exit_test),

    CONSTRAINT fk_dcvrb_signal
        FOREIGN KEY (signal_id) REFERENCES signals (id)
        ON DELETE SET NULL ON UPDATE CASCADE
);


-- ---------------------------------------------------------------
-- 15. contract_value_bounce_backtest_runs
--     One row per backtest run of the contract_value_bounce_scalp/v1
--     WATCH-ONLY hypothesis.  Kept entirely separate from paper_trades
--     and from all prior backtest/followthrough/post-move tables.
-- ---------------------------------------------------------------
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
);

-- ---------------------------------------------------------------
-- 16. contract_value_bounce_backtest_signals
--     One row per (signal × exit_test).  All four exit tests are
--     simulated for every signal; outcomes stored here.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contract_value_bounce_backtest_signals (
    id                              BIGINT        AUTO_INCREMENT PRIMARY KEY,
    run_id                          BIGINT        NOT NULL,
    rule_name                       VARCHAR(100)  NOT NULL,
    rule_version                    VARCHAR(20)   NOT NULL,
    market_ticker                   VARCHAR(100)  NOT NULL,

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

    btc_price                       DECIMAL(14,2) NULL,
    target_price                    DECIMAL(14,2) NULL,
    adverse_z_score                 DECIMAL(10,6) NULL,
    raw_momentum_score              DECIMAL(10,4) NULL,

    volatility_regime               VARCHAR(12),
    volatility_30s                  DECIMAL(14,4) NULL,
    volatility_60s                  DECIMAL(14,4) NULL,
    whipsaw_score                   DECIMAL(6,4)  NULL,

    market_age_seconds              INT,
    time_remaining_seconds          INT,
    entry_time                      DATETIME(3),
    hour_block                      VARCHAR(8),
    day_name                        VARCHAR(12),
    timezone_used                   VARCHAR(40),

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
);
