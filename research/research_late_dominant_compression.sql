-- research_late_dominant_compression.sql
-- MySQL 8 read-only diagnostics for the BTC 15m late dominant compression idea.
--
-- Purpose:
--   Test whether buying the dominant Kalshi BTC 15m contract late in the market
--   at 0.90-0.96, with BTC clearly on the correct side of the strike, reaches
--   +1c/+2c/+3c before realistic bid-side stops.
--
-- Schema discovered in this repo:
--   markets(id, market_id, target_price, closes_at, status, raw_payload)
--   contracts(id, market_id, contract_id, side)
--   market_snapshots(id, market_id, captured_at, btc_price, time_remaining_seconds, source)
--   contract_snapshots(market_snapshot_id, contract_id, captured_at, bid_price, ask_price, spread)
--   momentum_shadow_trades(...)
--   momentum_live_trades(...)
--
-- Assumptions:
--   - Entry buys at dominant contract ask.
--   - Exit/replay uses dominant contract bid.
--   - P/L output is true cents: 3.0 = 3 cents.
--   - Historical quote age is not stored in market_snapshots/contract_snapshots.
--     quote_age_ms is therefore NULL and quote-age diagnostics report unavailable.
--   - Time-of-day uses fixed EDT offset (-04:00). This is correct for the July
--     data currently under inspection; use timezone tables if analyzing EST dates.
--   - No production tables are created, updated, deleted, or altered.

-- Parameters. Change these values before running the file.
SET @entry_min := 0.90;
SET @entry_max := 0.96;
SET @tte_min := 120;
SET @tte_max := 300;
SET @spread_max := 0.01;
SET @quote_age_max_ms := 500;
SET @min_distance := 100;
SET @replay_window_seconds := 120;
SET @stop_bid_absolute := 0.83;
SET @fee_slippage_cents := 1.0;

SELECT 'schema_discovery' AS section, 'markets' AS table_name,
       'id, market_id, target_price, closes_at, status, raw_payload' AS relevant_columns
UNION ALL SELECT 'schema_discovery', 'contracts',
       'id, market_id, contract_id, side'
UNION ALL SELECT 'schema_discovery', 'market_snapshots',
       'id, market_id, captured_at, btc_price, time_remaining_seconds, source'
UNION ALL SELECT 'schema_discovery', 'contract_snapshots',
       'market_snapshot_id, contract_id, captured_at, bid_price, ask_price, spread'
UNION ALL SELECT 'schema_discovery', 'momentum_shadow_trades',
       'signal_at, market_ticker, side, exit_profile, entry_ask, exit_bid, net_pnl_cents'
UNION ALL SELECT 'schema_discovery', 'momentum_live_trades',
       'signal_at, market_ticker, side, exit_profile, actual_entry_price, actual_exit_price, actual_profit_dollars';

-- ============================================================
-- 1. Candidate Rows
-- ============================================================
WITH quote_pivot AS (
  SELECT
    ms.id AS snapshot_id,
    ms.captured_at AS observed_at,
    m.id AS market_pk,
    m.market_id AS market_ticker,
    m.target_price AS strike,
    m.closes_at,
    m.status AS market_status,
    ms.btc_price,
    ms.time_remaining_seconds AS time_to_expiry_seconds,
    ms.source AS btc_source,
    MAX(CASE WHEN c.side = 'YES' THEN c.id END) AS yes_contract_pk,
    MAX(CASE WHEN c.side = 'NO' THEN c.id END) AS no_contract_pk,
    MAX(CASE WHEN c.side = 'YES' THEN c.contract_id END) AS yes_contract_ticker,
    MAX(CASE WHEN c.side = 'NO' THEN c.contract_id END) AS no_contract_ticker,
    MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
    MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
    MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
    MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
    MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
    MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
  FROM market_snapshots ms
  JOIN markets m ON m.id = ms.market_id
  JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
  JOIN contracts c ON c.id = cs.contract_id
  GROUP BY
    ms.id, ms.captured_at, m.id, m.market_id, m.target_price, m.closes_at,
    m.status, ms.btc_price, ms.time_remaining_seconds, ms.source
),
enriched AS (
  SELECT
    q.*,
    ROUND((q.yes_bid + q.yes_ask) / 2, 4) AS yes_mid,
    ROUND((q.no_bid + q.no_ask) / 2, 4) AS no_mid,
    q.btc_price - q.strike AS signed_distance_to_strike,
    ABS(q.btc_price - q.strike) AS absolute_distance_to_strike,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2)
      THEN 'YES'
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2)
      THEN 'NO'
      ELSE NULL
    END AS dominant_side,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2)
      THEN q.yes_contract_pk
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2)
      THEN q.no_contract_pk
      ELSE NULL
    END AS contract_pk,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2)
      THEN q.yes_contract_ticker
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2)
      THEN q.no_contract_ticker
      ELSE NULL
    END AS contract_ticker,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2)
      THEN q.yes_bid
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2)
      THEN q.no_bid
      ELSE NULL
    END AS dominant_bid,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2)
      THEN q.yes_ask
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2)
      THEN q.no_ask
      ELSE NULL
    END AS dominant_ask,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2)
      THEN ROUND((q.yes_bid + q.yes_ask) / 2, 4)
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2)
      THEN ROUND((q.no_bid + q.no_ask) / 2, 4)
      ELSE NULL
    END AS dominant_mid,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2)
      THEN q.no_bid
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2)
      THEN q.yes_bid
      ELSE NULL
    END AS opposite_bid,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2)
      THEN q.no_ask
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2)
      THEN q.yes_ask
      ELSE NULL
    END AS opposite_ask,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2)
      THEN q.yes_spread
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2)
      THEN q.no_spread
      ELSE NULL
    END AS spread,
    CAST(NULL AS SIGNED) AS quote_age_ms
  FROM quote_pivot q
),
with_trends AS (
  SELECT
    e.*,
    CASE WHEN e.dominant_side = 'YES' THEN e.btc_price - e.strike
         WHEN e.dominant_side = 'NO' THEN e.strike - e.btc_price
    END AS side_distance,
    CASE WHEN e.dominant_side = 'YES' THEN e.btc_price - p10.btc_price
         WHEN e.dominant_side = 'NO' THEN p10.btc_price - e.btc_price
    END AS btc_trend_10s,
    CASE WHEN e.dominant_side = 'YES' THEN e.btc_price - p30.btc_price
         WHEN e.dominant_side = 'NO' THEN p30.btc_price - e.btc_price
    END AS btc_trend_30s,
    CASE WHEN e.dominant_side = 'YES' THEN e.btc_price - p60.btc_price
         WHEN e.dominant_side = 'NO' THEN p60.btc_price - e.btc_price
    END AS btc_trend_60s,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p10.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p10.btc_price)
    END AS distance_change_10s,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p30.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p30.btc_price)
    END AS distance_change_30s,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p60.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p60.btc_price)
    END AS distance_change_60s,
    TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) AS time_of_day_et,
    CASE
      WHEN e.dominant_ask >= 0.85 AND e.dominant_ask < 0.90 THEN '0.85-0.90'
      WHEN e.dominant_ask >= 0.90 AND e.dominant_ask < 0.96 THEN '0.90-0.96'
      WHEN e.dominant_ask >= 0.96 AND e.dominant_ask < 0.98 THEN '0.96-0.98'
      ELSE 'other'
    END AS entry_bucket,
    CASE
      WHEN ABS(e.btc_price - e.strike) >= 120 THEN '>=120'
      WHEN ABS(e.btc_price - e.strike) >= 100 THEN '>=100'
      WHEN ABS(e.btc_price - e.strike) >= 80 THEN '>=80'
      WHEN ABS(e.btc_price - e.strike) >= 60 THEN '>=60'
      ELSE '<60'
    END AS distance_bucket
  FROM enriched e
  LEFT JOIN market_snapshots p10
    ON p10.id = (
      SELECT p.id
      FROM market_snapshots p
      WHERE p.market_id = e.market_pk
        AND p.captured_at <= e.observed_at - INTERVAL 10 SECOND
      ORDER BY p.captured_at DESC
      LIMIT 1
    )
  LEFT JOIN market_snapshots p30
    ON p30.id = (
      SELECT p.id
      FROM market_snapshots p
      WHERE p.market_id = e.market_pk
        AND p.captured_at <= e.observed_at - INTERVAL 30 SECOND
      ORDER BY p.captured_at DESC
      LIMIT 1
    )
  LEFT JOIN market_snapshots p60
    ON p60.id = (
      SELECT p.id
      FROM market_snapshots p
      WHERE p.market_id = e.market_pk
        AND p.captured_at <= e.observed_at - INTERVAL 60 SECOND
      ORDER BY p.captured_at DESC
      LIMIT 1
    )
),
candidates AS (
  SELECT
    CONCAT(
      'late_dominant_compression_v1',
      '|entry=', @entry_min, '-', @entry_max,
      '|tte=', @tte_min, '-', @tte_max,
      '|spread<=', @spread_max,
      '|dist>=', @min_distance
    ) AS strategy_version,
    observed_at,
    market_ticker,
    contract_ticker,
    dominant_side,
    dominant_bid,
    dominant_ask,
    dominant_mid,
    opposite_bid,
    opposite_ask,
    spread,
    btc_price,
    strike,
    signed_distance_to_strike,
    absolute_distance_to_strike,
    time_to_expiry_seconds,
    quote_age_ms,
    btc_trend_10s,
    btc_trend_30s,
    btc_trend_60s,
    distance_change_10s,
    distance_change_30s,
    distance_change_60s,
    time_of_day_et,
    entry_bucket,
    distance_bucket,
    snapshot_id,
    market_pk,
    contract_pk,
    side_distance,
    btc_source
  FROM with_trends
  WHERE dominant_side IN ('YES', 'NO')
    AND dominant_ask >= @entry_min
    AND dominant_ask < @entry_max
    AND time_to_expiry_seconds BETWEEN @tte_min AND @tte_max
    AND spread <= @spread_max
    AND absolute_distance_to_strike >= @min_distance
    AND side_distance > 0
    AND COALESCE(distance_change_10s, 0) >= 0
    AND COALESCE(distance_change_30s, 0) >= 0
    AND COALESCE(distance_change_60s, 0) >= 0
)
SELECT
  observed_at,
  market_ticker,
  contract_ticker,
  dominant_side,
  dominant_bid,
  dominant_ask,
  dominant_mid,
  opposite_bid,
  opposite_ask,
  spread,
  btc_price,
  strike,
  signed_distance_to_strike,
  absolute_distance_to_strike,
  time_to_expiry_seconds,
  quote_age_ms,
  btc_trend_10s,
  btc_trend_30s,
  btc_trend_60s,
  distance_change_10s,
  distance_change_30s,
  distance_change_60s,
  time_of_day_et,
  entry_bucket,
  distance_bucket
FROM candidates
ORDER BY observed_at DESC
LIMIT 200;

-- ============================================================
-- 2. Deduped Signal Table
-- First qualifying row per market_ticker + strategy_version.
-- ============================================================
WITH quote_pivot AS (
  SELECT
    ms.id AS snapshot_id, ms.captured_at AS observed_at, m.id AS market_pk,
    m.market_id AS market_ticker, m.target_price AS strike, m.closes_at,
    ms.btc_price, ms.time_remaining_seconds AS time_to_expiry_seconds,
    MAX(CASE WHEN c.side = 'YES' THEN c.id END) AS yes_contract_pk,
    MAX(CASE WHEN c.side = 'NO' THEN c.id END) AS no_contract_pk,
    MAX(CASE WHEN c.side = 'YES' THEN c.contract_id END) AS yes_contract_ticker,
    MAX(CASE WHEN c.side = 'NO' THEN c.contract_id END) AS no_contract_ticker,
    MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
    MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
    MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
    MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
    MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
    MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
  FROM market_snapshots ms
  JOIN markets m ON m.id = ms.market_id
  JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
  JOIN contracts c ON c.id = cs.contract_id
  GROUP BY ms.id, ms.captured_at, m.id, m.market_id, m.target_price, m.closes_at,
           ms.btc_price, ms.time_remaining_seconds
),
enriched AS (
  SELECT
    q.*,
    q.btc_price - q.strike AS signed_distance_to_strike,
    ABS(q.btc_price - q.strike) AS absolute_distance_to_strike,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2) THEN 'YES'
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2) THEN 'NO'
    END AS dominant_side
  FROM quote_pivot q
),
with_trends AS (
  SELECT
    e.*,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_contract_pk ELSE e.no_contract_pk END AS contract_pk,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_contract_ticker ELSE e.no_contract_ticker END AS contract_ticker,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_bid ELSE e.no_bid END AS dominant_bid,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END AS dominant_ask,
    CASE WHEN e.dominant_side = 'YES' THEN ROUND((e.yes_bid + e.yes_ask) / 2, 4)
         WHEN e.dominant_side = 'NO' THEN ROUND((e.no_bid + e.no_ask) / 2, 4) END AS dominant_mid,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_spread ELSE e.no_spread END AS spread,
    CASE WHEN e.dominant_side = 'YES' THEN e.btc_price - e.strike
         WHEN e.dominant_side = 'NO' THEN e.strike - e.btc_price END AS side_distance,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p10.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p10.btc_price) END AS distance_change_10s,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p30.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p30.btc_price) END AS distance_change_30s,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p60.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p60.btc_price) END AS distance_change_60s,
    TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) AS time_of_day_et,
    CASE
      WHEN CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END >= 0.85
       AND CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END < 0.90 THEN '0.85-0.90'
      WHEN CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END >= 0.90
       AND CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END < 0.96 THEN '0.90-0.96'
      WHEN CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END >= 0.96
       AND CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END < 0.98 THEN '0.96-0.98'
      ELSE 'other'
    END AS entry_bucket,
    CASE
      WHEN ABS(e.btc_price - e.strike) >= 120 THEN '>=120'
      WHEN ABS(e.btc_price - e.strike) >= 100 THEN '>=100'
      WHEN ABS(e.btc_price - e.strike) >= 80 THEN '>=80'
      WHEN ABS(e.btc_price - e.strike) >= 60 THEN '>=60'
      ELSE '<60'
    END AS distance_bucket
  FROM enriched e
  LEFT JOIN market_snapshots p10 ON p10.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = e.market_pk AND p.captured_at <= e.observed_at - INTERVAL 10 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p30 ON p30.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = e.market_pk AND p.captured_at <= e.observed_at - INTERVAL 30 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p60 ON p60.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = e.market_pk AND p.captured_at <= e.observed_at - INTERVAL 60 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
),
candidates AS (
  SELECT
    CONCAT('late_dominant_compression_v1|entry=', @entry_min, '-', @entry_max,
           '|tte=', @tte_min, '-', @tte_max, '|spread<=', @spread_max,
           '|dist>=', @min_distance) AS strategy_version,
    w.*,
    ROW_NUMBER() OVER (
      PARTITION BY w.market_ticker,
                   CONCAT('late_dominant_compression_v1|entry=', @entry_min, '-', @entry_max,
                          '|tte=', @tte_min, '-', @tte_max, '|spread<=', @spread_max,
                          '|dist>=', @min_distance)
      ORDER BY w.observed_at ASC
    ) AS rn,
    COUNT(*) OVER (
      PARTITION BY w.market_ticker,
                   CONCAT('late_dominant_compression_v1|entry=', @entry_min, '-', @entry_max,
                          '|tte=', @tte_min, '-', @tte_max, '|spread<=', @spread_max,
                          '|dist>=', @min_distance)
    ) AS duplicate_candidate_snapshots
  FROM with_trends w
  WHERE w.dominant_side IN ('YES', 'NO')
    AND w.dominant_ask >= @entry_min
    AND w.dominant_ask < @entry_max
    AND w.time_to_expiry_seconds BETWEEN @tte_min AND @tte_max
    AND w.spread <= @spread_max
    AND w.absolute_distance_to_strike >= @min_distance
    AND w.side_distance > 0
    AND COALESCE(w.distance_change_10s, 0) >= 0
    AND COALESCE(w.distance_change_30s, 0) >= 0
    AND COALESCE(w.distance_change_60s, 0) >= 0
)
SELECT
  strategy_version,
  observed_at,
  market_ticker,
  contract_ticker,
  dominant_side,
  dominant_bid,
  dominant_ask,
  dominant_mid,
  spread,
  btc_price,
  strike,
  signed_distance_to_strike,
  absolute_distance_to_strike,
  side_distance,
  time_to_expiry_seconds,
  time_of_day_et,
  entry_bucket,
  distance_bucket,
  duplicate_candidate_snapshots
FROM candidates
WHERE rn = 1
ORDER BY observed_at DESC
LIMIT 200;

-- ============================================================
-- 3. Forward Replay / Outcome Table
-- ============================================================
WITH quote_pivot AS (
  SELECT
    ms.id AS snapshot_id, ms.captured_at AS observed_at, m.id AS market_pk,
    m.market_id AS market_ticker, m.target_price AS strike, m.closes_at,
    m.status AS market_status, m.raw_payload AS market_raw_payload,
    ms.btc_price, ms.time_remaining_seconds AS time_to_expiry_seconds,
    MAX(CASE WHEN c.side = 'YES' THEN c.id END) AS yes_contract_pk,
    MAX(CASE WHEN c.side = 'NO' THEN c.id END) AS no_contract_pk,
    MAX(CASE WHEN c.side = 'YES' THEN c.contract_id END) AS yes_contract_ticker,
    MAX(CASE WHEN c.side = 'NO' THEN c.contract_id END) AS no_contract_ticker,
    MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
    MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
    MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
    MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
    MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
    MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
  FROM market_snapshots ms
  JOIN markets m ON m.id = ms.market_id
  JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
  JOIN contracts c ON c.id = cs.contract_id
  GROUP BY ms.id, ms.captured_at, m.id, m.market_id, m.target_price, m.closes_at,
           m.status, m.raw_payload, ms.btc_price, ms.time_remaining_seconds
),
enriched AS (
  SELECT
    q.*,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2) THEN 'YES'
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2) THEN 'NO'
    END AS dominant_side,
    q.btc_price - q.strike AS signed_distance_to_strike,
    ABS(q.btc_price - q.strike) AS absolute_distance_to_strike
  FROM quote_pivot q
),
with_trends AS (
  SELECT
    e.*,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_contract_pk ELSE e.no_contract_pk END AS contract_pk,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_contract_ticker ELSE e.no_contract_ticker END AS contract_ticker,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_bid ELSE e.no_bid END AS dominant_bid,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END AS dominant_ask,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_spread ELSE e.no_spread END AS spread,
    CASE WHEN e.dominant_side = 'YES' THEN e.btc_price - e.strike
         WHEN e.dominant_side = 'NO' THEN e.strike - e.btc_price END AS side_distance,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p10.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p10.btc_price) END AS distance_change_10s,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p30.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p30.btc_price) END AS distance_change_30s,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p60.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p60.btc_price) END AS distance_change_60s,
    CASE
      WHEN CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END >= 0.85
       AND CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END < 0.90 THEN '0.85-0.90'
      WHEN CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END >= 0.90
       AND CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END < 0.96 THEN '0.90-0.96'
      WHEN CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END >= 0.96
       AND CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END < 0.98 THEN '0.96-0.98'
      ELSE 'other'
    END AS entry_bucket,
    CASE
      WHEN ABS(e.btc_price - e.strike) >= 120 THEN '>=120'
      WHEN ABS(e.btc_price - e.strike) >= 100 THEN '>=100'
      WHEN ABS(e.btc_price - e.strike) >= 80 THEN '>=80'
      WHEN ABS(e.btc_price - e.strike) >= 60 THEN '>=60'
      ELSE '<60'
    END AS distance_bucket,
    CASE
      WHEN e.time_to_expiry_seconds >= 120 AND e.time_to_expiry_seconds < 180 THEN '120-180'
      WHEN e.time_to_expiry_seconds >= 180 AND e.time_to_expiry_seconds < 240 THEN '180-240'
      WHEN e.time_to_expiry_seconds >= 240 AND e.time_to_expiry_seconds <= 300 THEN '240-300'
      ELSE 'other'
    END AS tte_bucket,
    CASE
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '06:00:00' THEN '00:00-06:00'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '09:30:00' THEN '06:00-09:30'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '11:00:00' THEN '09:30-11:00'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '14:00:00' THEN '11:00-14:00'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '16:00:00' THEN '14:00-16:00'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '20:00:00' THEN '16:00-20:00'
      ELSE '20:00-24:00'
    END AS time_of_day_bucket
  FROM enriched e
  LEFT JOIN market_snapshots p10 ON p10.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = e.market_pk AND p.captured_at <= e.observed_at - INTERVAL 10 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p30 ON p30.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = e.market_pk AND p.captured_at <= e.observed_at - INTERVAL 30 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p60 ON p60.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = e.market_pk AND p.captured_at <= e.observed_at - INTERVAL 60 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
),
deduped AS (
  SELECT *
  FROM (
    SELECT
      CONCAT('late_dominant_compression_v1|entry=', @entry_min, '-', @entry_max,
             '|tte=', @tte_min, '-', @tte_max, '|spread<=', @spread_max,
             '|dist>=', @min_distance) AS strategy_version,
      w.*,
      ROW_NUMBER() OVER (
        PARTITION BY w.market_ticker,
                     CONCAT('late_dominant_compression_v1|entry=', @entry_min, '-', @entry_max,
                            '|tte=', @tte_min, '-', @tte_max, '|spread<=', @spread_max,
                            '|dist>=', @min_distance)
        ORDER BY w.observed_at ASC
      ) AS rn
    FROM with_trends w
    WHERE w.dominant_side IN ('YES', 'NO')
      AND w.dominant_ask >= @entry_min
      AND w.dominant_ask < @entry_max
      AND w.time_to_expiry_seconds BETWEEN @tte_min AND @tte_max
      AND w.spread <= @spread_max
      AND w.absolute_distance_to_strike >= @min_distance
      AND w.side_distance > 0
      AND COALESCE(w.distance_change_10s, 0) >= 0
      AND COALESCE(w.distance_change_30s, 0) >= 0
      AND COALESCE(w.distance_change_60s, 0) >= 0
  ) x
  WHERE rn = 1
),
future_quotes AS (
  SELECT
    d.snapshot_id,
    fms.captured_at,
    fms.btc_price,
    CASE WHEN d.dominant_side = 'YES' THEN fms.btc_price - d.strike
         WHEN d.dominant_side = 'NO' THEN d.strike - fms.btc_price END AS future_side_distance,
    fcs.bid_price,
    fcs.ask_price
  FROM deduped d
  JOIN market_snapshots fms
    ON fms.market_id = d.market_pk
   AND fms.captured_at > d.observed_at
   AND fms.captured_at <= d.observed_at + INTERVAL 120 SECOND
  JOIN contract_snapshots fcs
    ON fcs.market_snapshot_id = fms.id
   AND fcs.contract_id = d.contract_pk
),
replay AS (
  SELECT
    d.*,
    MIN(CASE WHEN fq.bid_price >= d.dominant_ask + 0.01 THEN fq.captured_at END) AS target_1c_hit_at,
    MIN(CASE WHEN fq.bid_price >= d.dominant_ask + 0.02 THEN fq.captured_at END) AS target_2c_hit_at,
    MIN(CASE WHEN fq.bid_price >= d.dominant_ask + 0.03 THEN fq.captured_at END) AS target_3c_hit_at,
    MIN(CASE WHEN fq.bid_price <= d.dominant_ask - 0.02 THEN fq.captured_at END) AS stop_2c_hit_at,
    MIN(CASE WHEN fq.bid_price <= d.dominant_ask - 0.03 THEN fq.captured_at END) AS stop_3c_hit_at,
    MIN(CASE WHEN fq.bid_price <= @stop_bid_absolute THEN fq.captured_at END) AS stop_bid_083_hit_at,
    ROUND((MAX(fq.bid_price) - d.dominant_ask) * 100, 4) AS max_favorable_excursion_cents,
    ROUND((MIN(fq.bid_price) - d.dominant_ask) * 100, 4) AS max_adverse_excursion_cents,
    MIN(fq.bid_price) AS min_bid_after_entry,
    MAX(fq.bid_price) AS max_bid_after_entry,
    MIN(fq.future_side_distance) AS btc_min_distance_after_entry,
    MAX(fq.future_side_distance) AS btc_max_distance_after_entry,
    MAX(fq.captured_at) AS last_forward_quote_at,
    SUBSTRING_INDEX(GROUP_CONCAT(fq.bid_price ORDER BY fq.captured_at DESC), ',', 1) AS final_bid
  FROM deduped d
  LEFT JOIN future_quotes fq ON fq.snapshot_id = d.snapshot_id
  GROUP BY
    d.snapshot_id, d.observed_at, d.market_pk, d.market_ticker, d.strike, d.closes_at,
    d.market_status, d.market_raw_payload, d.btc_price, d.time_to_expiry_seconds,
    d.yes_contract_pk, d.no_contract_pk, d.yes_contract_ticker, d.no_contract_ticker,
    d.yes_bid, d.yes_ask, d.yes_spread, d.no_bid, d.no_ask, d.no_spread,
    d.dominant_side, d.signed_distance_to_strike, d.absolute_distance_to_strike,
    d.contract_pk, d.contract_ticker, d.dominant_bid, d.dominant_ask, d.spread,
    d.side_distance, d.distance_change_10s, d.distance_change_30s, d.distance_change_60s,
    d.entry_bucket, d.distance_bucket, d.tte_bucket, d.time_of_day_bucket,
    d.strategy_version, d.rn
)
SELECT
  observed_at,
  market_ticker,
  contract_ticker,
  dominant_side,
  dominant_ask AS entry_ask,
  dominant_bid AS entry_bid,
  spread,
  btc_price,
  strike,
  side_distance AS entry_side_distance,
  time_to_expiry_seconds,
  target_1c_hit_at,
  target_2c_hit_at,
  target_3c_hit_at,
  stop_2c_hit_at,
  stop_3c_hit_at,
  stop_bid_083_hit_at,
  CASE
    WHEN target_1c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_1c_hit_at < stop_2c_hit_at) THEN 'target_1c_first'
    WHEN stop_2c_hit_at IS NOT NULL AND (target_1c_hit_at IS NULL OR stop_2c_hit_at < target_1c_hit_at) THEN 'stop_2c_first'
    ELSE 'no_first_hit'
  END AS first_hit_1c_vs_2c_stop,
  CASE
    WHEN target_2c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_2c_hit_at < stop_2c_hit_at) THEN 'target_2c_first'
    WHEN stop_2c_hit_at IS NOT NULL AND (target_2c_hit_at IS NULL OR stop_2c_hit_at < target_2c_hit_at) THEN 'stop_2c_first'
    ELSE 'no_first_hit'
  END AS first_hit_2c_vs_2c_stop,
  CASE
    WHEN target_3c_hit_at IS NOT NULL AND (stop_3c_hit_at IS NULL OR target_3c_hit_at < stop_3c_hit_at) THEN 'target_3c_first'
    WHEN stop_3c_hit_at IS NOT NULL AND (target_3c_hit_at IS NULL OR stop_3c_hit_at < target_3c_hit_at) THEN 'stop_3c_first'
    ELSE 'no_first_hit'
  END AS first_hit_3c_vs_3c_stop,
  max_favorable_excursion_cents,
  max_adverse_excursion_cents,
  min_bid_after_entry,
  max_bid_after_entry,
  btc_min_distance_after_entry,
  btc_max_distance_after_entry,
  (side_distance - btc_min_distance_after_entry >= 20) AS btc_distance_compressed_by_20,
  (side_distance - btc_min_distance_after_entry >= 40) AS btc_distance_compressed_by_40,
  (btc_min_distance_after_entry <= 0) AS btc_crossed_back_over_strike,
  market_status AS settlement_status,
  COALESCE(
    JSON_UNQUOTE(JSON_EXTRACT(market_raw_payload, '$.result')),
    JSON_UNQUOTE(JSON_EXTRACT(market_raw_payload, '$.settlement_value')),
    JSON_UNQUOTE(JSON_EXTRACT(market_raw_payload, '$.winning_outcome'))
  ) AS settlement_outcome,
  CASE
    WHEN target_1c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_1c_hit_at < stop_2c_hit_at) THEN 1.0
    WHEN stop_2c_hit_at IS NOT NULL AND (target_1c_hit_at IS NULL OR stop_2c_hit_at < target_1c_hit_at) THEN -2.0
    ELSE ROUND((CAST(final_bid AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
  END AS gross_pnl_1c_vs_2c_stop,
  CASE
    WHEN target_2c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_2c_hit_at < stop_2c_hit_at) THEN 2.0
    WHEN stop_2c_hit_at IS NOT NULL AND (target_2c_hit_at IS NULL OR stop_2c_hit_at < target_2c_hit_at) THEN -2.0
    ELSE ROUND((CAST(final_bid AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
  END AS gross_pnl_2c_vs_2c_stop,
  CASE
    WHEN target_3c_hit_at IS NOT NULL AND (stop_3c_hit_at IS NULL OR target_3c_hit_at < stop_3c_hit_at) THEN 3.0
    WHEN stop_3c_hit_at IS NOT NULL AND (target_3c_hit_at IS NULL OR stop_3c_hit_at < target_3c_hit_at) THEN -3.0
    ELSE ROUND((CAST(final_bid AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
  END AS gross_pnl_3c_vs_3c_stop,
  @fee_slippage_cents AS fee_slippage_placeholder_cents
FROM replay
ORDER BY observed_at DESC
LIMIT 200;

-- ============================================================
-- 4. Summary Diagnostics
-- Same CTE as section 3, grouped by bucket dimensions.
-- ============================================================
WITH quote_pivot AS (
  SELECT
    ms.id AS snapshot_id, ms.captured_at AS observed_at, m.id AS market_pk,
    m.market_id AS market_ticker, m.target_price AS strike, m.closes_at,
    ms.btc_price, ms.time_remaining_seconds AS time_to_expiry_seconds,
    MAX(CASE WHEN c.side = 'YES' THEN c.id END) AS yes_contract_pk,
    MAX(CASE WHEN c.side = 'NO' THEN c.id END) AS no_contract_pk,
    MAX(CASE WHEN c.side = 'YES' THEN c.contract_id END) AS yes_contract_ticker,
    MAX(CASE WHEN c.side = 'NO' THEN c.contract_id END) AS no_contract_ticker,
    MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
    MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
    MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
    MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
    MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
    MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
  FROM market_snapshots ms
  JOIN markets m ON m.id = ms.market_id
  JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
  JOIN contracts c ON c.id = cs.contract_id
  GROUP BY ms.id, ms.captured_at, m.id, m.market_id, m.target_price, m.closes_at,
           ms.btc_price, ms.time_remaining_seconds
),
enriched AS (
  SELECT
    q.*,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2) THEN 'YES'
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2) THEN 'NO'
    END AS dominant_side,
    ABS(q.btc_price - q.strike) AS absolute_distance_to_strike
  FROM quote_pivot q
),
with_trends AS (
  SELECT
    e.*,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_contract_pk ELSE e.no_contract_pk END AS contract_pk,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END AS dominant_ask,
    CASE WHEN e.dominant_side = 'YES' THEN e.yes_spread ELSE e.no_spread END AS spread,
    CASE WHEN e.dominant_side = 'YES' THEN e.btc_price - e.strike
         WHEN e.dominant_side = 'NO' THEN e.strike - e.btc_price END AS side_distance,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p10.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p10.btc_price) END AS distance_change_10s,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p30.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p30.btc_price) END AS distance_change_30s,
    CASE WHEN e.dominant_side = 'YES' THEN (e.btc_price - e.strike) - (p60.btc_price - e.strike)
         WHEN e.dominant_side = 'NO' THEN (e.strike - e.btc_price) - (e.strike - p60.btc_price) END AS distance_change_60s,
    CASE
      WHEN CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END >= 0.85
       AND CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END < 0.90 THEN '0.85-0.90'
      WHEN CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END >= 0.90
       AND CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END < 0.96 THEN '0.90-0.96'
      WHEN CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END >= 0.96
       AND CASE WHEN e.dominant_side = 'YES' THEN e.yes_ask ELSE e.no_ask END < 0.98 THEN '0.96-0.98'
      ELSE 'other'
    END AS entry_bucket,
    CASE
      WHEN ABS(e.btc_price - e.strike) >= 120 THEN '>=120'
      WHEN ABS(e.btc_price - e.strike) >= 100 THEN '>=100'
      WHEN ABS(e.btc_price - e.strike) >= 80 THEN '>=80'
      WHEN ABS(e.btc_price - e.strike) >= 60 THEN '>=60'
      ELSE '<60'
    END AS distance_bucket,
    CASE
      WHEN e.time_to_expiry_seconds >= 120 AND e.time_to_expiry_seconds < 180 THEN '120-180'
      WHEN e.time_to_expiry_seconds >= 180 AND e.time_to_expiry_seconds < 240 THEN '180-240'
      WHEN e.time_to_expiry_seconds >= 240 AND e.time_to_expiry_seconds <= 300 THEN '240-300'
      ELSE 'other'
    END AS tte_bucket,
    CASE
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '06:00:00' THEN '00:00-06:00'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '09:30:00' THEN '06:00-09:30'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '11:00:00' THEN '09:30-11:00'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '14:00:00' THEN '11:00-14:00'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '16:00:00' THEN '14:00-16:00'
      WHEN TIME(CONVERT_TZ(e.observed_at, '+00:00', '-04:00')) < '20:00:00' THEN '16:00-20:00'
      ELSE '20:00-24:00'
    END AS time_of_day_bucket
  FROM enriched e
  LEFT JOIN market_snapshots p10 ON p10.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = e.market_pk AND p.captured_at <= e.observed_at - INTERVAL 10 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p30 ON p30.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = e.market_pk AND p.captured_at <= e.observed_at - INTERVAL 30 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p60 ON p60.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = e.market_pk AND p.captured_at <= e.observed_at - INTERVAL 60 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
),
candidates AS (
  SELECT
    w.*,
    ROW_NUMBER() OVER (
      PARTITION BY w.market_ticker,
                   CONCAT('late_dominant_compression_v1|entry=', @entry_min, '-', @entry_max,
                          '|tte=', @tte_min, '-', @tte_max, '|spread<=', @spread_max,
                          '|dist>=', @min_distance)
      ORDER BY w.observed_at ASC
    ) AS rn,
    COUNT(*) OVER (
      PARTITION BY w.market_ticker,
                   CONCAT('late_dominant_compression_v1|entry=', @entry_min, '-', @entry_max,
                          '|tte=', @tte_min, '-', @tte_max, '|spread<=', @spread_max,
                          '|dist>=', @min_distance)
    ) AS raw_candidate_count
  FROM with_trends w
  WHERE w.dominant_side IN ('YES', 'NO')
    AND w.dominant_ask >= @entry_min
    AND w.dominant_ask < @entry_max
    AND w.time_to_expiry_seconds BETWEEN @tte_min AND @tte_max
    AND w.spread <= @spread_max
    AND w.absolute_distance_to_strike >= @min_distance
    AND w.side_distance > 0
    AND COALESCE(w.distance_change_10s, 0) >= 0
    AND COALESCE(w.distance_change_30s, 0) >= 0
    AND COALESCE(w.distance_change_60s, 0) >= 0
),
deduped AS (
  SELECT * FROM candidates WHERE rn = 1
),
future_quotes AS (
  SELECT
    d.snapshot_id,
    fms.captured_at,
    CASE WHEN d.dominant_side = 'YES' THEN fms.btc_price - d.strike
         WHEN d.dominant_side = 'NO' THEN d.strike - fms.btc_price END AS future_side_distance,
    fcs.bid_price
  FROM deduped d
  JOIN market_snapshots fms
    ON fms.market_id = d.market_pk
   AND fms.captured_at > d.observed_at
   AND fms.captured_at <= d.observed_at + INTERVAL 120 SECOND
  JOIN contract_snapshots fcs
    ON fcs.market_snapshot_id = fms.id
   AND fcs.contract_id = d.contract_pk
),
replay AS (
  SELECT
    d.snapshot_id,
    d.market_ticker,
    d.dominant_side,
    d.dominant_ask,
    d.spread,
    d.side_distance,
    d.time_to_expiry_seconds,
    d.entry_bucket,
    d.distance_bucket,
    d.tte_bucket,
    d.time_of_day_bucket,
    d.raw_candidate_count,
    MIN(CASE WHEN fq.bid_price >= d.dominant_ask + 0.01 THEN fq.captured_at END) AS target_1c_hit_at,
    MIN(CASE WHEN fq.bid_price >= d.dominant_ask + 0.02 THEN fq.captured_at END) AS target_2c_hit_at,
    MIN(CASE WHEN fq.bid_price >= d.dominant_ask + 0.03 THEN fq.captured_at END) AS target_3c_hit_at,
    MIN(CASE WHEN fq.bid_price <= d.dominant_ask - 0.02 THEN fq.captured_at END) AS stop_2c_hit_at,
    MIN(CASE WHEN fq.bid_price <= d.dominant_ask - 0.03 THEN fq.captured_at END) AS stop_3c_hit_at,
    MIN(fq.future_side_distance) AS btc_min_distance_after_entry,
    MAX(fq.bid_price) AS max_bid_after_entry,
    SUBSTRING_INDEX(GROUP_CONCAT(fq.bid_price ORDER BY fq.captured_at DESC), ',', 1) AS final_bid
  FROM deduped d
  LEFT JOIN future_quotes fq ON fq.snapshot_id = d.snapshot_id
  GROUP BY
    d.snapshot_id, d.market_ticker, d.dominant_side, d.dominant_ask, d.spread,
    d.side_distance, d.time_to_expiry_seconds, d.entry_bucket, d.distance_bucket,
    d.tte_bucket, d.time_of_day_bucket, d.raw_candidate_count
),
scored AS (
  SELECT
    r.*,
    CASE
      WHEN target_1c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_1c_hit_at < stop_2c_hit_at) THEN 1.0
      WHEN stop_2c_hit_at IS NOT NULL AND (target_1c_hit_at IS NULL OR stop_2c_hit_at < target_1c_hit_at) THEN -2.0
      ELSE ROUND((CAST(final_bid AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS pnl_1c_2c,
    CASE
      WHEN target_2c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_2c_hit_at < stop_2c_hit_at) THEN 2.0
      WHEN stop_2c_hit_at IS NOT NULL AND (target_2c_hit_at IS NULL OR stop_2c_hit_at < target_2c_hit_at) THEN -2.0
      ELSE ROUND((CAST(final_bid AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS pnl_2c_2c,
    CASE
      WHEN target_3c_hit_at IS NOT NULL AND (stop_3c_hit_at IS NULL OR target_3c_hit_at < stop_3c_hit_at) THEN 3.0
      WHEN stop_3c_hit_at IS NOT NULL AND (target_3c_hit_at IS NULL OR stop_3c_hit_at < target_3c_hit_at) THEN -3.0
      ELSE ROUND((CAST(final_bid AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS pnl_3c_3c
  FROM replay r
)
SELECT
  entry_bucket,
  distance_bucket,
  tte_bucket,
  dominant_side AS side,
  time_of_day_bucket,
  SUM(raw_candidate_count) AS candidates_raw,
  COUNT(*) AS unique_markets,
  ROUND(100 * SUM(target_1c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_1c_hit_at < stop_2c_hit_at)) / COUNT(*), 1) AS target_1c_first_rate,
  ROUND(100 * SUM(target_2c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_2c_hit_at < stop_2c_hit_at)) / COUNT(*), 1) AS target_2c_first_rate,
  ROUND(100 * SUM(target_3c_hit_at IS NOT NULL AND (stop_3c_hit_at IS NULL OR target_3c_hit_at < stop_3c_hit_at)) / COUNT(*), 1) AS target_3c_first_rate,
  ROUND(100 * SUM(stop_2c_hit_at IS NOT NULL AND (target_1c_hit_at IS NULL OR stop_2c_hit_at < target_1c_hit_at)) / COUNT(*), 1) AS stop_2c_first_rate,
  ROUND(100 * SUM(stop_3c_hit_at IS NOT NULL AND (target_3c_hit_at IS NULL OR stop_3c_hit_at < target_3c_hit_at)) / COUNT(*), 1) AS stop_3c_first_rate,
  ROUND(AVG(pnl_1c_2c), 4) AS avg_gross_pnl_1c_vs_2c_stop,
  ROUND(AVG(pnl_2c_2c), 4) AS avg_gross_pnl_2c_vs_2c_stop,
  ROUND(AVG(pnl_3c_3c), 4) AS avg_gross_pnl_3c_vs_3c_stop,
  ROUND(SUM(CASE WHEN pnl_1c_2c > 0 THEN pnl_1c_2c ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN pnl_1c_2c < 0 THEN pnl_1c_2c ELSE 0 END)), 0), 4) AS profit_factor_1c_vs_2c_stop,
  ROUND(SUM(CASE WHEN pnl_2c_2c > 0 THEN pnl_2c_2c ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN pnl_2c_2c < 0 THEN pnl_2c_2c ELSE 0 END)), 0), 4) AS profit_factor_2c_vs_2c_stop,
  ROUND(SUM(CASE WHEN pnl_3c_3c > 0 THEN pnl_3c_3c ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN pnl_3c_3c < 0 THEN pnl_3c_3c ELSE 0 END)), 0), 4) AS profit_factor_3c_vs_3c_stop,
  ROUND(AVG(dominant_ask), 4) AS avg_entry_price,
  ROUND(AVG(side_distance), 2) AS avg_distance,
  ROUND(AVG(time_to_expiry_seconds), 1) AS avg_time_to_expiry,
  ROUND(AVG(spread), 4) AS avg_spread,
  SUM(spread = 0) AS locked_quote_count,
  0 AS stale_quote_count,
  ROUND(100 * SUM(btc_min_distance_after_entry <= 0) / COUNT(*), 1) AS btc_crossback_rate,
  ROUND(100 * SUM(side_distance - btc_min_distance_after_entry >= 40) / COUNT(*), 1) AS distance_compression_40_rate
FROM scored
GROUP BY entry_bucket, distance_bucket, tte_bucket, dominant_side, time_of_day_bucket
ORDER BY unique_markets DESC, avg_gross_pnl_1c_vs_2c_stop DESC;

-- ============================================================
-- 5. Data-Quality Diagnostics
-- ============================================================
WITH quote_pivot AS (
  SELECT
    ms.id AS snapshot_id,
    ms.captured_at AS observed_at,
    m.id AS market_pk,
    m.market_id AS market_ticker,
    m.target_price AS strike,
    m.closes_at,
    ms.btc_price,
    ms.time_remaining_seconds AS time_to_expiry_seconds,
    MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
    MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
    MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
    MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
    MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
    MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
  FROM market_snapshots ms
  JOIN markets m ON m.id = ms.market_id
  JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
  JOIN contracts c ON c.id = cs.contract_id
  GROUP BY ms.id, ms.captured_at, m.id, m.market_id, m.target_price, m.closes_at,
           ms.btc_price, ms.time_remaining_seconds
),
dominance AS (
  SELECT
    q.*,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2) THEN 'YES'
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2) THEN 'NO'
    END AS dominant_side,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2) THEN q.yes_ask
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2) THEN q.no_ask
    END AS dominant_ask,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2) THEN q.yes_spread
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2) THEN q.no_spread
    END AS spread,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2) THEN q.btc_price - q.strike
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2) THEN q.strike - q.btc_price
    END AS side_distance
  FROM quote_pivot q
),
candidates AS (
  SELECT
    d.*,
    COUNT(*) OVER (PARTITION BY d.market_ticker) AS candidate_snapshots_per_market
  FROM dominance d
  WHERE d.dominant_side IN ('YES', 'NO')
    AND d.dominant_ask >= @entry_min
    AND d.dominant_ask < @entry_max
    AND d.time_to_expiry_seconds BETWEEN @tte_min AND @tte_max
    AND d.spread <= @spread_max
    AND ABS(d.btc_price - d.strike) >= @min_distance
),
forward_coverage AS (
  SELECT
    c.snapshot_id,
    COUNT(fms.id) AS forward_quotes
  FROM candidates c
  LEFT JOIN market_snapshots fms
    ON fms.market_id = c.market_pk
   AND fms.captured_at > c.observed_at
   AND fms.captured_at <= c.observed_at + INTERVAL 120 SECOND
  GROUP BY c.snapshot_id
),
issues AS (
  SELECT 'spread_eq_zero' AS issue, observed_at FROM quote_pivot WHERE yes_spread = 0 OR no_spread = 0
  UNION ALL SELECT 'spread_lt_zero', observed_at FROM quote_pivot WHERE yes_spread < 0 OR no_spread < 0
  UNION ALL SELECT 'bid_gt_ask', observed_at FROM quote_pivot WHERE yes_bid > yes_ask OR no_bid > no_ask
  UNION ALL SELECT 'quote_age_ms_unavailable_in_historical_snapshots', observed_at FROM candidates
  UNION ALL SELECT 'missing_btc_price', observed_at FROM quote_pivot WHERE btc_price IS NULL
  UNION ALL SELECT 'missing_strike', observed_at FROM quote_pivot WHERE strike IS NULL
  UNION ALL SELECT 'missing_expiry', observed_at FROM quote_pivot WHERE closes_at IS NULL AND time_to_expiry_seconds IS NULL
  UNION ALL SELECT 'missing_bid_or_ask', observed_at FROM quote_pivot
    WHERE yes_bid IS NULL OR yes_ask IS NULL OR no_bid IS NULL OR no_ask IS NULL
  UNION ALL SELECT 'no_forward_quotes_next_120s', c.observed_at
    FROM candidates c JOIN forward_coverage f ON f.snapshot_id = c.snapshot_id
    WHERE f.forward_quotes = 0
  UNION ALL SELECT 'duplicate_candidate_snapshots_per_market', observed_at
    FROM candidates WHERE candidate_snapshots_per_market > 1
  UNION ALL SELECT 'candidate_btc_side_mismatch', observed_at
    FROM candidates WHERE side_distance <= 0
)
SELECT
  issue,
  COUNT(*) AS rows_affected,
  MAX(observed_at) AS latest_seen
FROM issues
GROUP BY issue
ORDER BY rows_affected DESC, issue;

-- ============================================================
-- 6. Comparison Query
-- Late dominant compression vs existing momentum shadow/live rows.
--
-- The late-dominant row uses gross_pnl_1c_vs_2c_stop from this file.
-- The older cheap-loser reversal was explored with ad-hoc CTEs in this repo;
-- no dedicated result table is present in the discovered schema, so it is not
-- included here unless you materialize that strategy separately.
-- ============================================================
WITH ldc_quote_pivot AS (
  SELECT
    ms.id AS snapshot_id,
    ms.captured_at AS observed_at,
    m.id AS market_pk,
    m.market_id AS market_ticker,
    m.target_price AS strike,
    ms.btc_price,
    ms.time_remaining_seconds AS time_to_expiry_seconds,
    MAX(CASE WHEN c.side = 'YES' THEN c.id END) AS yes_contract_pk,
    MAX(CASE WHEN c.side = 'NO' THEN c.id END) AS no_contract_pk,
    MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
    MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
    MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
    MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
    MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
    MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
  FROM market_snapshots ms
  JOIN markets m ON m.id = ms.market_id
  JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
  JOIN contracts c ON c.id = cs.contract_id
  GROUP BY ms.id, ms.captured_at, m.id, m.market_id, m.target_price,
           ms.btc_price, ms.time_remaining_seconds
),
ldc_dominance AS (
  SELECT
    q.*,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2) THEN 'YES'
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2) THEN 'NO'
    END AS dominant_side
  FROM ldc_quote_pivot q
),
ldc_with_trends AS (
  SELECT
    d.*,
    CASE WHEN d.dominant_side = 'YES' THEN d.yes_contract_pk ELSE d.no_contract_pk END AS contract_pk,
    CASE WHEN d.dominant_side = 'YES' THEN d.yes_ask ELSE d.no_ask END AS dominant_ask,
    CASE WHEN d.dominant_side = 'YES' THEN d.yes_spread ELSE d.no_spread END AS spread,
    CASE WHEN d.dominant_side = 'YES' THEN d.btc_price - d.strike
         WHEN d.dominant_side = 'NO' THEN d.strike - d.btc_price END AS side_distance,
    ABS(d.btc_price - d.strike) AS absolute_distance_to_strike,
    CASE WHEN d.dominant_side = 'YES' THEN (d.btc_price - d.strike) - (p10.btc_price - d.strike)
         WHEN d.dominant_side = 'NO' THEN (d.strike - d.btc_price) - (d.strike - p10.btc_price) END AS distance_change_10s,
    CASE WHEN d.dominant_side = 'YES' THEN (d.btc_price - d.strike) - (p30.btc_price - d.strike)
         WHEN d.dominant_side = 'NO' THEN (d.strike - d.btc_price) - (d.strike - p30.btc_price) END AS distance_change_30s,
    CASE WHEN d.dominant_side = 'YES' THEN (d.btc_price - d.strike) - (p60.btc_price - d.strike)
         WHEN d.dominant_side = 'NO' THEN (d.strike - d.btc_price) - (d.strike - p60.btc_price) END AS distance_change_60s
  FROM ldc_dominance d
  LEFT JOIN market_snapshots p10 ON p10.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = d.market_pk AND p.captured_at <= d.observed_at - INTERVAL 10 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p30 ON p30.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = d.market_pk AND p.captured_at <= d.observed_at - INTERVAL 30 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p60 ON p60.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = d.market_pk AND p.captured_at <= d.observed_at - INTERVAL 60 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
),
ldc_candidates AS (
  SELECT *
  FROM (
    SELECT
      w.*,
      ROW_NUMBER() OVER (
        PARTITION BY w.market_ticker
        ORDER BY w.observed_at ASC
      ) AS rn
    FROM ldc_with_trends w
    WHERE w.dominant_side IN ('YES', 'NO')
      AND w.dominant_ask >= @entry_min
      AND w.dominant_ask < @entry_max
      AND w.time_to_expiry_seconds BETWEEN @tte_min AND @tte_max
      AND w.spread <= @spread_max
      AND w.absolute_distance_to_strike >= @min_distance
      AND w.side_distance > 0
      AND COALESCE(w.distance_change_10s, 0) >= 0
      AND COALESCE(w.distance_change_30s, 0) >= 0
      AND COALESCE(w.distance_change_60s, 0) >= 0
  ) x
  WHERE rn = 1
),
ldc_replay AS (
  SELECT
    c.snapshot_id,
    c.market_ticker,
    DATE(c.observed_at) AS active_day,
    c.dominant_ask,
    MIN(CASE WHEN fcs.bid_price >= c.dominant_ask + 0.01 THEN fms.captured_at END) AS target_1c_hit_at,
    MIN(CASE WHEN fcs.bid_price <= c.dominant_ask - 0.02 THEN fms.captured_at END) AS stop_2c_hit_at,
    SUBSTRING_INDEX(GROUP_CONCAT(fcs.bid_price ORDER BY fms.captured_at DESC), ',', 1) AS final_bid
  FROM ldc_candidates c
  LEFT JOIN market_snapshots fms
    ON fms.market_id = c.market_pk
   AND fms.captured_at > c.observed_at
   AND fms.captured_at <= c.observed_at + INTERVAL 120 SECOND
  LEFT JOIN contract_snapshots fcs
    ON fcs.market_snapshot_id = fms.id
   AND fcs.contract_id = c.contract_pk
  GROUP BY c.snapshot_id, c.market_ticker, DATE(c.observed_at), c.dominant_ask
),
late_dominant_compression AS (
  SELECT
    'late_dominant_compression' AS strategy_name,
    active_day,
    market_ticker,
    CASE
      WHEN target_1c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_1c_hit_at < stop_2c_hit_at) THEN 1.0
      WHEN stop_2c_hit_at IS NOT NULL AND (target_1c_hit_at IS NULL OR stop_2c_hit_at < target_1c_hit_at) THEN -2.0
      ELSE ROUND((CAST(final_bid AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS gross_like_pnl_cents
  FROM ldc_replay
),
momentum_shadow AS (
  SELECT
    'momentum_shadow' AS strategy_name,
    DATE(signal_at) AS active_day,
    market_ticker,
    net_pnl_cents * 100 AS gross_like_pnl_cents
  FROM momentum_shadow_trades
  WHERE status = 'COMPLETE'
    AND net_pnl_cents IS NOT NULL
),
momentum_live AS (
  SELECT
    'momentum_live' AS strategy_name,
    DATE(signal_at) AS active_day,
    market_ticker,
    actual_profit_cents * 100 AS gross_like_pnl_cents
  FROM momentum_live_trades
  WHERE status = 'COMPLETE'
    AND filled_contracts > 0
    AND actual_profit_cents IS NOT NULL
),
combined AS (
  SELECT * FROM late_dominant_compression
  UNION ALL
  SELECT * FROM momentum_shadow
  UNION ALL
  SELECT * FROM momentum_live
),
daily AS (
  SELECT
    strategy_name,
    active_day,
    SUM(gross_like_pnl_cents) AS day_pnl_cents
  FROM combined
  GROUP BY strategy_name, active_day
)
SELECT
  c.strategy_name,
  COUNT(DISTINCT c.market_ticker) AS unique_markets,
  ROUND(100 * SUM(c.gross_like_pnl_cents > 0) / COUNT(*), 1) AS win_rate,
  ROUND(AVG(c.gross_like_pnl_cents), 4) AS avg_gross_pnl_cents,
  ROUND(AVG(c.gross_like_pnl_cents - @fee_slippage_cents), 4) AS estimated_avg_net_pnl_cents,
  ROUND(SUM(CASE WHEN c.gross_like_pnl_cents > 0 THEN c.gross_like_pnl_cents ELSE 0 END)
        / NULLIF(ABS(SUM(CASE WHEN c.gross_like_pnl_cents < 0 THEN c.gross_like_pnl_cents ELSE 0 END)), 0), 4) AS profit_factor,
  COUNT(DISTINCT c.active_day) AS active_days,
  ROUND(100 * MAX(d.day_pnl_cents) / NULLIF(SUM(c.gross_like_pnl_cents), 0), 1) AS percent_profit_from_best_day
FROM combined c
JOIN daily d
  ON d.strategy_name = c.strategy_name
 AND d.active_day = c.active_day
GROUP BY c.strategy_name
ORDER BY estimated_avg_net_pnl_cents DESC;
