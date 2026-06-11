-- ============================================================
-- Kalshi BTC Market Research Logger — Schema v2
-- MySQL 8.0+
--
-- Design principles:
--   • Raw data is immutable. Snapshots are insert-only.
--   • Every row represents a specific moment in time.
--   • Raw API payloads stored in JSON columns where available.
--   • No trading logic, no signals, no strategies, no predictions.
-- ============================================================


-- ------------------------------------------------------------
-- 1. markets
--    One row per Kalshi market. Upserted on discovery.
--    raw_payload stores the full Kalshi API market object.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS markets (
    id              BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_id       VARCHAR(100)    NOT NULL,           -- Kalshi ticker, e.g. KXBTC-26JUN0805-T71799.99
    title           VARCHAR(255),
    market_type     VARCHAR(100),                       -- binary | range
    target_price    DECIMAL(18, 2),                     -- strike price (NULL for range markets)
    opens_at        DATETIME,
    closes_at       DATETIME,
    settles_at      DATETIME,
    status          VARCHAR(50),                        -- open | closed | settled
    raw_payload     JSON,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_market_id (market_id)
);


-- ------------------------------------------------------------
-- 2. contracts
--    One row per outcome side within a market.
--    A binary Kalshi market produces two rows: YES and NO.
--    contract_id = "{market_id}_YES" / "{market_id}_NO"
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contracts (
    id              BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_id       BIGINT          NOT NULL,
    contract_id     VARCHAR(100)    NOT NULL,           -- e.g. KXBTC-26JUN0805-T71799.99_YES
    side            VARCHAR(50)     NOT NULL,           -- YES | NO
    title           VARCHAR(255),
    status          VARCHAR(50),
    raw_payload     JSON,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_contract_id (contract_id),
    INDEX idx_contracts_market_id (market_id),

    CONSTRAINT fk_contracts_market
        FOREIGN KEY (market_id) REFERENCES markets (id)
        ON DELETE CASCADE ON UPDATE CASCADE
);


-- ------------------------------------------------------------
-- 3. market_snapshots
--    One row per polling interval per market.
--    Captures Bitcoin price and market timing at a specific moment.
--
--    Insert-only. Never update.
--    snapshot_sequence is a per-market counter starting at 1.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_snapshots (
    id                      BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_id               BIGINT          NOT NULL,
    snapshot_sequence       BIGINT          NOT NULL,
    captured_at             DATETIME(3)     NOT NULL,
    btc_price               DECIMAL(18, 2)  NOT NULL,
    time_remaining_seconds  INT,
    source                  VARCHAR(100),               -- kraken | mock
    raw_payload             JSON,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_ms_market_captured (market_id, captured_at),

    CONSTRAINT fk_ms_market
        FOREIGN KEY (market_id) REFERENCES markets (id)
        ON DELETE CASCADE ON UPDATE CASCADE
);


-- ------------------------------------------------------------
-- 4. contract_snapshots
--    One row per contract per polling interval.
--    Links to market_snapshots via market_snapshot_id.
--
--    Insert-only. Never update.
--    At 2s polling: ~43,200 market_snapshots/day,
--    ~86,400 contract_snapshots/day (YES + NO per snapshot).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contract_snapshots (
    id                  BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_snapshot_id  BIGINT          NOT NULL,
    contract_id         BIGINT          NOT NULL,
    captured_at         DATETIME(3)     NOT NULL,
    last_price          DECIMAL(10, 4),
    bid_price           DECIMAL(10, 4)  NOT NULL,
    ask_price           DECIMAL(10, 4)  NOT NULL,
    spread              DECIMAL(10, 4),
    volume              BIGINT,
    raw_payload         JSON,
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_cs_contract_captured  (contract_id, captured_at),
    INDEX idx_cs_snapshot           (market_snapshot_id),

    CONSTRAINT fk_cs_snapshot
        FOREIGN KEY (market_snapshot_id) REFERENCES market_snapshots (id)
        ON DELETE CASCADE ON UPDATE CASCADE,

    CONSTRAINT fk_cs_contract
        FOREIGN KEY (contract_id) REFERENCES contracts (id)
        ON DELETE CASCADE ON UPDATE CASCADE
);


-- ------------------------------------------------------------
-- 5. market_metrics
--    Derived research metrics computed from market_snapshots.
--    Not raw data — calculated values only.
--
--    Definitions:
--      distance_from_target     = btc_price - target_price
--      distance_from_target_pct = (btc_price - target_price) / target_price * 100
--      btc_return_X             = (btc_price_now - btc_price_Xs_ago) / btc_price_Xs_ago * 100
--      btc_return_stddev_X      = std dev of per-tick returns over rolling X window
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_metrics (
    id                          BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_snapshot_id          BIGINT          NOT NULL,
    captured_at                 DATETIME(3)     NOT NULL,

    -- distance from strike
    distance_from_target        DECIMAL(18, 2),
    distance_from_target_pct    DECIMAL(10, 6),
    is_above_target             BOOLEAN,

    -- BTC returns (percentage, e.g. 0.00123456 = 0.012%)
    btc_return_30s              DECIMAL(12, 8),
    btc_return_1m               DECIMAL(12, 8),
    btc_return_5m               DECIMAL(12, 8),

    -- volatility: std dev of per-tick returns over rolling window
    btc_return_stddev_1m        DECIMAL(12, 8),
    btc_return_stddev_3m        DECIMAL(12, 8),
    btc_return_stddev_5m        DECIMAL(12, 8),
    btc_return_stddev_15m       DECIMAL(12, 8),

    created_at                  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_mm_snapshot   (market_snapshot_id),
    INDEX idx_mm_captured   (captured_at),

    CONSTRAINT fk_mm_snapshot
        FOREIGN KEY (market_snapshot_id) REFERENCES market_snapshots (id)
        ON DELETE CASCADE ON UPDATE CASCADE
);
