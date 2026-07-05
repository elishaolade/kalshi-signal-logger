-- research_side_strength_distance.sql
-- MySQL 8 diagnostics for Kalshi BTC 15m side-strength distance support.
--
-- This file creates session TEMPORARY TABLES only. It does not create, update,
-- delete, or alter production tables.
--
-- Discovered schema used:
--   markets(id, market_id, target_price, closes_at, status, raw_payload)
--   contracts(id, market_id, contract_id, side)
--   market_snapshots(id, market_id, captured_at, btc_price, time_remaining_seconds, source)
--   contract_snapshots(market_snapshot_id, contract_id, captured_at, bid_price, ask_price, spread)
--   momentum_shadow_trades(...)
--   momentum_live_trades(...)
--
-- Assumptions:
--   - Entry buys dominant contract at ask.
--   - Exit sells the same contract at bid.
--   - P/L is true cents: 3.0 = 3 cents.
--   - Historical quote_age_ms is not stored in market_snapshots/contract_snapshots.
--   - Time buckets use fixed EDT offset (-04:00). This matches the current July
--     research data. If analyzing winter EST, adjust the offset.
--   - Full-sample distance medians are labeled biased. Prior-only average support
--     is preferred when prior_sample_count >= 5.

SET @entry_min := 0.85;
SET @entry_max := 0.98;
SET @tte_min := 120;
SET @tte_max := 300;
SET @spread_max := 0.01;
SET @quote_age_max_ms := 500;
SET @replay_window_seconds := 120;
SET @stop_bid_absolute := 0.83;
SET @estimated_fee_cents := 0.50;
SET @estimated_slippage_cents := 0.50;
SET @min_prior_baseline_count := 5;

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
       'signal_at, market_ticker, side, actual_entry_price, actual_exit_price, actual_profit_dollars';

DROP TEMPORARY TABLE IF EXISTS vw_side_strength_candidates;
CREATE TEMPORARY TABLE vw_side_strength_candidates AS
WITH quote_pivot AS (
  SELECT
    ms.id AS snapshot_id,
    ms.captured_at AS observed_at,
    CONVERT_TZ(ms.captured_at, '+00:00', '-04:00') AS observed_at_et,
    HOUR(CONVERT_TZ(ms.captured_at, '+00:00', '-04:00')) AS hour_of_day_et,
    CASE
      WHEN TIME(CONVERT_TZ(ms.captured_at, '+00:00', '-04:00')) < '06:00:00' THEN '00:00-06:00'
      WHEN TIME(CONVERT_TZ(ms.captured_at, '+00:00', '-04:00')) < '09:30:00' THEN '06:00-09:30'
      WHEN TIME(CONVERT_TZ(ms.captured_at, '+00:00', '-04:00')) < '11:00:00' THEN '09:30-11:00'
      WHEN TIME(CONVERT_TZ(ms.captured_at, '+00:00', '-04:00')) < '14:00:00' THEN '11:00-14:00'
      WHEN TIME(CONVERT_TZ(ms.captured_at, '+00:00', '-04:00')) < '16:00:00' THEN '14:00-16:00'
      WHEN TIME(CONVERT_TZ(ms.captured_at, '+00:00', '-04:00')) < '20:00:00' THEN '16:00-20:00'
      ELSE '20:00-24:00'
    END AS time_of_day_bucket,
    m.id AS market_pk,
    m.market_id AS market_ticker,
    m.target_price AS strike,
    m.closes_at AS expiry_ts,
    ms.btc_price,
    ms.time_remaining_seconds AS time_to_expiry_seconds,
    ms.source AS btc_source,
    MAX(CASE WHEN c.side = 'YES' THEN c.id END) AS yes_contract_pk,
    MAX(CASE WHEN c.side = 'NO' THEN c.id END) AS no_contract_pk,
    MAX(CASE WHEN c.side = 'YES' THEN c.contract_id END) AS yes_contract_ticker,
    MAX(CASE WHEN c.side = 'NO' THEN c.contract_id END) AS no_contract_ticker,
    MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
    MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
    MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
    MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
    MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
    MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
  FROM market_snapshots ms
  JOIN markets m ON m.id = ms.market_id
  JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
  JOIN contracts c ON c.id = cs.contract_id
  GROUP BY
    ms.id, ms.captured_at, m.id, m.market_id, m.target_price, m.closes_at,
    ms.btc_price, ms.time_remaining_seconds, ms.source
),
dominance AS (
  SELECT
    q.*,
    ROUND((q.yes_bid + q.yes_ask) / 2, 4) AS yes_mid,
    ROUND((q.no_bid + q.no_ask) / 2, 4) AS no_mid,
    CASE
      WHEN q.yes_bid > q.no_bid AND q.yes_ask > q.no_ask
       AND ((q.yes_bid + q.yes_ask) / 2) > ((q.no_bid + q.no_ask) / 2) THEN 'YES'
      WHEN q.no_bid > q.yes_bid AND q.no_ask > q.yes_ask
       AND ((q.no_bid + q.no_ask) / 2) > ((q.yes_bid + q.yes_ask) / 2) THEN 'NO'
      ELSE NULL
    END AS dominant_side
  FROM quote_pivot q
),
with_prices AS (
  SELECT
    d.*,
    CASE WHEN d.dominant_side = 'YES' THEN d.yes_contract_pk ELSE d.no_contract_pk END AS contract_pk,
    CASE WHEN d.dominant_side = 'YES' THEN d.yes_contract_ticker ELSE d.no_contract_ticker END AS contract_ticker,
    CASE WHEN d.dominant_side = 'YES' THEN d.yes_bid ELSE d.no_bid END AS dominant_bid,
    CASE WHEN d.dominant_side = 'YES' THEN d.yes_ask ELSE d.no_ask END AS dominant_ask,
    CASE WHEN d.dominant_side = 'YES' THEN d.yes_mid ELSE d.no_mid END AS dominant_mid,
    CASE WHEN d.dominant_side = 'YES' THEN d.no_bid ELSE d.yes_bid END AS opposite_bid,
    CASE WHEN d.dominant_side = 'YES' THEN d.no_ask ELSE d.yes_ask END AS opposite_ask,
    CASE WHEN d.dominant_side = 'YES' THEN d.yes_spread ELSE d.no_spread END AS spread,
    CAST(NULL AS SIGNED) AS quote_age_ms,
    d.btc_price - d.strike AS signed_distance_for_yes,
    d.strike - d.btc_price AS signed_distance_for_no,
    CASE WHEN d.dominant_side = 'YES' THEN d.btc_price - d.strike
         WHEN d.dominant_side = 'NO' THEN d.strike - d.btc_price END AS aligned_distance,
    ABS(d.btc_price - d.strike) AS absolute_distance
  FROM dominance d
),
with_trends AS (
  SELECT
    w.*,
    CASE WHEN w.dominant_side = 'YES' THEN w.btc_price - p10.btc_price
         WHEN w.dominant_side = 'NO' THEN p10.btc_price - w.btc_price END AS btc_change_10s,
    CASE WHEN w.dominant_side = 'YES' THEN w.btc_price - p30.btc_price
         WHEN w.dominant_side = 'NO' THEN p30.btc_price - w.btc_price END AS btc_change_30s,
    CASE WHEN w.dominant_side = 'YES' THEN w.btc_price - p60.btc_price
         WHEN w.dominant_side = 'NO' THEN p60.btc_price - w.btc_price END AS btc_change_60s,
    CASE WHEN w.dominant_side = 'YES' THEN (w.btc_price - w.strike) - (p10.btc_price - w.strike)
         WHEN w.dominant_side = 'NO' THEN (w.strike - w.btc_price) - (w.strike - p10.btc_price) END AS aligned_distance_change_10s,
    CASE WHEN w.dominant_side = 'YES' THEN (w.btc_price - w.strike) - (p30.btc_price - w.strike)
         WHEN w.dominant_side = 'NO' THEN (w.strike - w.btc_price) - (w.strike - p30.btc_price) END AS aligned_distance_change_30s,
    CASE WHEN w.dominant_side = 'YES' THEN (w.btc_price - w.strike) - (p60.btc_price - w.strike)
         WHEN w.dominant_side = 'NO' THEN (w.strike - w.btc_price) - (w.strike - p60.btc_price) END AS aligned_distance_change_60s
  FROM with_prices w
  LEFT JOIN market_snapshots p10 ON p10.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = w.market_pk AND p.captured_at <= w.observed_at - INTERVAL 10 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p30 ON p30.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = w.market_pk AND p.captured_at <= w.observed_at - INTERVAL 30 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
  LEFT JOIN market_snapshots p60 ON p60.id = (
    SELECT p.id FROM market_snapshots p
    WHERE p.market_id = w.market_pk AND p.captured_at <= w.observed_at - INTERVAL 60 SECOND
    ORDER BY p.captured_at DESC LIMIT 1
  )
)
SELECT
  observed_at,
  observed_at_et,
  hour_of_day_et,
  time_of_day_bucket,
  market_ticker,
  contract_ticker,
  market_pk,
  contract_pk,
  snapshot_id,
  strike,
  expiry_ts,
  time_to_expiry_seconds,
  CASE
    WHEN time_to_expiry_seconds >= 120 AND time_to_expiry_seconds < 180 THEN '120-180'
    WHEN time_to_expiry_seconds >= 180 AND time_to_expiry_seconds < 240 THEN '180-240'
    WHEN time_to_expiry_seconds >= 240 AND time_to_expiry_seconds <= 300 THEN '240-300'
    ELSE 'other'
  END AS time_to_expiry_bucket,
  btc_price,
  yes_bid,
  yes_ask,
  yes_mid,
  no_bid,
  no_ask,
  no_mid,
  dominant_side,
  dominant_bid,
  dominant_ask,
  dominant_mid,
  opposite_bid,
  opposite_ask,
  spread,
  quote_age_ms,
  signed_distance_for_yes,
  signed_distance_for_no,
  aligned_distance,
  absolute_distance,
  btc_change_10s,
  btc_change_30s,
  btc_change_60s,
  aligned_distance_change_10s,
  aligned_distance_change_30s,
  aligned_distance_change_60s,
  COALESCE(aligned_distance_change_10s, 0) >= 0 AS trend_confirming_10s,
  COALESCE(aligned_distance_change_30s, 0) >= 0 AS trend_confirming_30s,
  COALESCE(aligned_distance_change_60s, 0) >= 0 AS trend_confirming_60s,
  CASE
    WHEN dominant_ask >= 0.85 AND dominant_ask < 0.90 THEN '0.85-0.90'
    WHEN dominant_ask >= 0.90 AND dominant_ask < 0.96 THEN '0.90-0.96'
    WHEN dominant_ask >= 0.96 AND dominant_ask < 0.98 THEN '0.96-0.98'
    ELSE 'other'
  END AS entry_price_bucket,
  CASE
    WHEN aligned_distance >= 200 THEN '>=200'
    WHEN aligned_distance >= 150 THEN '>=150'
    WHEN aligned_distance >= 120 THEN '>=120'
    WHEN aligned_distance >= 100 THEN '>=100'
    WHEN aligned_distance >= 80 THEN '>=80'
    WHEN aligned_distance >= 60 THEN '>=60'
    WHEN aligned_distance >= 40 THEN '>=40'
    ELSE '<40'
  END AS distance_bucket,
  CONCAT('side_strength_distance_v1|entry=0.85-0.98|tte=120-300|spread<=0.01') AS strategy_version,
  btc_source
FROM with_trends
WHERE dominant_side IN ('YES', 'NO')
  AND dominant_ask >= @entry_min
  AND dominant_ask < @entry_max
  AND time_to_expiry_seconds BETWEEN @tte_min AND @tte_max
  AND spread <= @spread_max
  AND aligned_distance > 0;

DROP TEMPORARY TABLE IF EXISTS vw_side_strength_baseline_full;
CREATE TEMPORARY TABLE vw_side_strength_baseline_full AS
WITH ordered AS (
  SELECT
    dominant_side,
    entry_price_bucket,
    time_of_day_bucket,
    time_to_expiry_bucket,
    aligned_distance,
    ROW_NUMBER() OVER (
      PARTITION BY dominant_side, entry_price_bucket, time_of_day_bucket, time_to_expiry_bucket
      ORDER BY aligned_distance
    ) AS rn,
    COUNT(*) OVER (
      PARTITION BY dominant_side, entry_price_bucket, time_of_day_bucket, time_to_expiry_bucket
    ) AS n
  FROM vw_side_strength_candidates
)
SELECT
  dominant_side,
  entry_price_bucket,
  time_of_day_bucket,
  time_to_expiry_bucket,
  COUNT(*) AS sample_count,
  ROUND(AVG(CASE WHEN rn IN (GREATEST(1, FLOOR((n + 1) * 0.50)), GREATEST(1, CEIL((n + 1) * 0.50))) THEN aligned_distance END), 4) AS median_aligned_distance_biased,
  ROUND(AVG(aligned_distance), 4) AS avg_aligned_distance_biased,
  ROUND(AVG(CASE WHEN rn IN (GREATEST(1, FLOOR((n + 1) * 0.25)), GREATEST(1, CEIL((n + 1) * 0.25))) THEN aligned_distance END), 4) AS p25_aligned_distance,
  ROUND(AVG(CASE WHEN rn IN (GREATEST(1, FLOOR((n + 1) * 0.75)), GREATEST(1, CEIL((n + 1) * 0.75))) THEN aligned_distance END), 4) AS p75_aligned_distance,
  MIN(aligned_distance) AS min_aligned_distance,
  MAX(aligned_distance) AS max_aligned_distance
FROM ordered
GROUP BY dominant_side, entry_price_bucket, time_of_day_bucket, time_to_expiry_bucket;

DROP TEMPORARY TABLE IF EXISTS vw_side_strength_signals;
CREATE TEMPORARY TABLE vw_side_strength_signals AS
WITH ranked AS (
  SELECT
    c.*,
    ROW_NUMBER() OVER (
      PARTITION BY c.market_ticker, c.strategy_version
      ORDER BY c.observed_at ASC
    ) AS rn,
    COUNT(*) OVER (
      PARTITION BY c.market_ticker, c.strategy_version
    ) AS raw_candidate_count_for_market_strategy
  FROM vw_side_strength_candidates c
),
deduped AS (
  SELECT
    ROW_NUMBER() OVER (ORDER BY observed_at, market_ticker, dominant_side) AS signal_id,
    r.*
  FROM ranked r
  WHERE r.rn = 1
),
with_baseline AS (
  SELECT
    d.*,
    b.sample_count AS full_sample_baseline_count,
    b.median_aligned_distance_biased AS full_sample_baseline_median_distance_biased,
    b.avg_aligned_distance_biased AS full_sample_baseline_avg_distance_biased,
    b.p25_aligned_distance,
    b.p75_aligned_distance,
    b.min_aligned_distance,
    b.max_aligned_distance,
    (
      SELECT COUNT(*)
      FROM vw_side_strength_candidates p
      WHERE p.observed_at < d.observed_at
        AND p.dominant_side = d.dominant_side
        AND p.entry_price_bucket = d.entry_price_bucket
        AND p.time_of_day_bucket = d.time_of_day_bucket
        AND p.time_to_expiry_bucket = d.time_to_expiry_bucket
    ) AS prior_baseline_count,
    (
      SELECT ROUND(AVG(p.aligned_distance), 4)
      FROM vw_side_strength_candidates p
      WHERE p.observed_at < d.observed_at
        AND p.dominant_side = d.dominant_side
        AND p.entry_price_bucket = d.entry_price_bucket
        AND p.time_of_day_bucket = d.time_of_day_bucket
        AND p.time_to_expiry_bucket = d.time_to_expiry_bucket
    ) AS prior_baseline_avg_distance
  FROM deduped d
  LEFT JOIN vw_side_strength_baseline_full b
    ON b.dominant_side = d.dominant_side
   AND b.entry_price_bucket = d.entry_price_bucket
   AND b.time_of_day_bucket = d.time_of_day_bucket
   AND b.time_to_expiry_bucket = d.time_to_expiry_bucket
),
scored AS (
  SELECT
    wb.*,
    wb.full_sample_baseline_median_distance_biased AS recent_baseline_median_distance,
    CASE
      WHEN wb.prior_baseline_count >= @min_prior_baseline_count THEN wb.prior_baseline_avg_distance
      ELSE wb.full_sample_baseline_avg_distance_biased
    END AS recent_baseline_avg_distance,
    ROUND(wb.aligned_distance / NULLIF(wb.full_sample_baseline_median_distance_biased, 0), 4) AS distance_support_ratio_median,
    ROUND(wb.aligned_distance / NULLIF(
      CASE
        WHEN wb.prior_baseline_count >= @min_prior_baseline_count THEN wb.prior_baseline_avg_distance
        ELSE wb.full_sample_baseline_avg_distance_biased
      END, 0), 4) AS distance_support_ratio_avg
  FROM with_baseline wb
)
SELECT
  *,
  CASE
    WHEN distance_support_ratio_avg < 0.75 THEN 'weak_support'
    WHEN distance_support_ratio_avg < 1.00 THEN 'below_normal_support'
    WHEN distance_support_ratio_avg < 1.25 THEN 'normal_support'
    WHEN distance_support_ratio_avg < 1.50 THEN 'strong_support'
    ELSE 'very_strong_support'
  END AS support_bucket
FROM scored;

DROP TEMPORARY TABLE IF EXISTS vw_side_strength_outcomes;
CREATE TEMPORARY TABLE vw_side_strength_outcomes AS
WITH future_quotes AS (
  SELECT
    s.signal_id,
    fms.captured_at,
    CASE WHEN s.dominant_side = 'YES' THEN fms.btc_price - s.strike
         WHEN s.dominant_side = 'NO' THEN s.strike - fms.btc_price END AS future_aligned_distance,
    fcs.bid_price,
    fcs.ask_price
  FROM vw_side_strength_signals s
  JOIN market_snapshots fms
    ON fms.market_id = s.market_pk
   AND fms.captured_at > s.observed_at
   AND fms.captured_at <= s.observed_at + INTERVAL 120 SECOND
  JOIN contract_snapshots fcs
    ON fcs.market_snapshot_id = fms.id
   AND fcs.contract_id = s.contract_pk
),
replay AS (
  SELECT
    s.*,
    s.dominant_ask + 0.01 AS target_1c_price,
    s.dominant_ask + 0.02 AS target_2c_price,
    s.dominant_ask + 0.03 AS target_3c_price,
    s.dominant_ask - 0.02 AS stop_2c_price,
    s.dominant_ask - 0.03 AS stop_3c_price,
    @stop_bid_absolute AS absolute_stop_bid_083,
    MIN(CASE WHEN fq.bid_price >= s.dominant_ask + 0.01 THEN fq.captured_at END) AS target_1c_hit_at,
    MIN(CASE WHEN fq.bid_price >= s.dominant_ask + 0.02 THEN fq.captured_at END) AS target_2c_hit_at,
    MIN(CASE WHEN fq.bid_price >= s.dominant_ask + 0.03 THEN fq.captured_at END) AS target_3c_hit_at,
    MIN(CASE WHEN fq.bid_price <= s.dominant_ask - 0.02 THEN fq.captured_at END) AS stop_2c_hit_at,
    MIN(CASE WHEN fq.bid_price <= s.dominant_ask - 0.03 THEN fq.captured_at END) AS stop_3c_hit_at,
    MIN(CASE WHEN fq.bid_price <= @stop_bid_absolute THEN fq.captured_at END) AS stop_083_hit_at,
    ROUND((MAX(fq.bid_price) - s.dominant_ask) * 100, 4) AS max_favorable_excursion_cents,
    ROUND((MIN(fq.bid_price) - s.dominant_ask) * 100, 4) AS max_adverse_excursion_cents,
    MAX(fq.bid_price) AS max_bid_after_entry,
    MIN(fq.bid_price) AS min_bid_after_entry,
    SUBSTRING_INDEX(GROUP_CONCAT(fq.bid_price ORDER BY fq.captured_at DESC), ',', 1) AS final_bid_120s,
    MIN(fq.future_aligned_distance) AS btc_distance_min_after_entry,
    MAX(fq.future_aligned_distance) AS btc_distance_max_after_entry,
    SUBSTRING_INDEX(GROUP_CONCAT(fq.future_aligned_distance ORDER BY fq.captured_at DESC), ',', 1) AS btc_distance_end_120s,
    COUNT(fq.captured_at) AS forward_quote_count
  FROM vw_side_strength_signals s
  LEFT JOIN future_quotes fq ON fq.signal_id = s.signal_id
  GROUP BY s.signal_id
),
scored AS (
  SELECT
    r.*,
    CASE
      WHEN forward_quote_count = 0 THEN 'no_forward_quotes'
      WHEN target_1c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_1c_hit_at < stop_2c_hit_at) THEN 'target_first'
      WHEN stop_2c_hit_at IS NOT NULL AND (target_1c_hit_at IS NULL OR stop_2c_hit_at < target_1c_hit_at) THEN 'stop_first'
      ELSE 'neither'
    END AS outcome_1c_vs_stop_2c,
    CASE
      WHEN forward_quote_count = 0 THEN 'no_forward_quotes'
      WHEN target_2c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_2c_hit_at < stop_2c_hit_at) THEN 'target_first'
      WHEN stop_2c_hit_at IS NOT NULL AND (target_2c_hit_at IS NULL OR stop_2c_hit_at < target_2c_hit_at) THEN 'stop_first'
      ELSE 'neither'
    END AS outcome_2c_vs_stop_2c,
    CASE
      WHEN forward_quote_count = 0 THEN 'no_forward_quotes'
      WHEN target_3c_hit_at IS NOT NULL AND (stop_3c_hit_at IS NULL OR target_3c_hit_at < stop_3c_hit_at) THEN 'target_first'
      WHEN stop_3c_hit_at IS NOT NULL AND (target_3c_hit_at IS NULL OR stop_3c_hit_at < target_3c_hit_at) THEN 'stop_first'
      ELSE 'neither'
    END AS outcome_3c_vs_stop_3c,
    CASE
      WHEN forward_quote_count = 0 THEN 'no_forward_quotes'
      WHEN target_1c_hit_at IS NOT NULL AND (stop_083_hit_at IS NULL OR target_1c_hit_at < stop_083_hit_at) THEN 'target_first'
      WHEN stop_083_hit_at IS NOT NULL AND (target_1c_hit_at IS NULL OR stop_083_hit_at < target_1c_hit_at) THEN 'stop_first'
      ELSE 'neither'
    END AS outcome_1c_vs_stop_083,
    CASE
      WHEN forward_quote_count = 0 THEN 'no_forward_quotes'
      WHEN target_2c_hit_at IS NOT NULL AND (stop_083_hit_at IS NULL OR target_2c_hit_at < stop_083_hit_at) THEN 'target_first'
      WHEN stop_083_hit_at IS NOT NULL AND (target_2c_hit_at IS NULL OR stop_083_hit_at < target_2c_hit_at) THEN 'stop_first'
      ELSE 'neither'
    END AS outcome_2c_vs_stop_083,
    CASE
      WHEN forward_quote_count = 0 THEN 'no_forward_quotes'
      WHEN target_3c_hit_at IS NOT NULL AND (stop_083_hit_at IS NULL OR target_3c_hit_at < stop_083_hit_at) THEN 'target_first'
      WHEN stop_083_hit_at IS NOT NULL AND (target_3c_hit_at IS NULL OR stop_083_hit_at < target_3c_hit_at) THEN 'stop_first'
      ELSE 'neither'
    END AS outcome_3c_vs_stop_083
  FROM replay r
),
pnl AS (
  SELECT
    s.*,
    CASE
      WHEN outcome_1c_vs_stop_2c = 'no_forward_quotes' THEN NULL
      WHEN outcome_1c_vs_stop_2c = 'target_first' THEN 1.0
      WHEN outcome_1c_vs_stop_2c = 'stop_first' THEN -2.0
      ELSE ROUND((CAST(final_bid_120s AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS gross_pnl_1c_vs_stop_2c,
    CASE
      WHEN outcome_2c_vs_stop_2c = 'no_forward_quotes' THEN NULL
      WHEN outcome_2c_vs_stop_2c = 'target_first' THEN 2.0
      WHEN outcome_2c_vs_stop_2c = 'stop_first' THEN -2.0
      ELSE ROUND((CAST(final_bid_120s AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS gross_pnl_2c_vs_stop_2c,
    CASE
      WHEN outcome_3c_vs_stop_3c = 'no_forward_quotes' THEN NULL
      WHEN outcome_3c_vs_stop_3c = 'target_first' THEN 3.0
      WHEN outcome_3c_vs_stop_3c = 'stop_first' THEN -3.0
      ELSE ROUND((CAST(final_bid_120s AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS gross_pnl_3c_vs_stop_3c,
    CASE
      WHEN outcome_1c_vs_stop_083 = 'no_forward_quotes' THEN NULL
      WHEN outcome_1c_vs_stop_083 = 'target_first' THEN 1.0
      WHEN outcome_1c_vs_stop_083 = 'stop_first' THEN ROUND((@stop_bid_absolute - dominant_ask) * 100, 4)
      ELSE ROUND((CAST(final_bid_120s AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS gross_pnl_1c_vs_stop_083,
    CASE
      WHEN outcome_2c_vs_stop_083 = 'no_forward_quotes' THEN NULL
      WHEN outcome_2c_vs_stop_083 = 'target_first' THEN 2.0
      WHEN outcome_2c_vs_stop_083 = 'stop_first' THEN ROUND((@stop_bid_absolute - dominant_ask) * 100, 4)
      ELSE ROUND((CAST(final_bid_120s AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS gross_pnl_2c_vs_stop_083,
    CASE
      WHEN outcome_3c_vs_stop_083 = 'no_forward_quotes' THEN NULL
      WHEN outcome_3c_vs_stop_083 = 'target_first' THEN 3.0
      WHEN outcome_3c_vs_stop_083 = 'stop_first' THEN ROUND((@stop_bid_absolute - dominant_ask) * 100, 4)
      ELSE ROUND((CAST(final_bid_120s AS DECIMAL(10,4)) - dominant_ask) * 100, 4)
    END AS gross_pnl_3c_vs_stop_083
  FROM scored s
)
SELECT
  *,
  CASE WHEN target_1c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_1c_hit_at < stop_2c_hit_at) THEN 'target_first'
       WHEN stop_2c_hit_at IS NOT NULL AND (target_1c_hit_at IS NULL OR stop_2c_hit_at < target_1c_hit_at) THEN 'stop_first'
       ELSE outcome_1c_vs_stop_2c END AS first_hit_1c_vs_2c_stop,
  CASE WHEN target_2c_hit_at IS NOT NULL AND (stop_2c_hit_at IS NULL OR target_2c_hit_at < stop_2c_hit_at) THEN 'target_first'
       WHEN stop_2c_hit_at IS NOT NULL AND (target_2c_hit_at IS NULL OR stop_2c_hit_at < target_2c_hit_at) THEN 'stop_first'
       ELSE outcome_2c_vs_stop_2c END AS first_hit_2c_vs_2c_stop,
  CASE WHEN target_3c_hit_at IS NOT NULL AND (stop_3c_hit_at IS NULL OR target_3c_hit_at < stop_3c_hit_at) THEN 'target_first'
       WHEN stop_3c_hit_at IS NOT NULL AND (target_3c_hit_at IS NULL OR stop_3c_hit_at < target_3c_hit_at) THEN 'stop_first'
       ELSE outcome_3c_vs_stop_3c END AS first_hit_3c_vs_3c_stop,
  outcome_1c_vs_stop_083 AS first_hit_1c_vs_083_stop,
  outcome_2c_vs_stop_083 AS first_hit_2c_vs_083_stop,
  outcome_3c_vs_stop_083 AS first_hit_3c_vs_083_stop,
  @estimated_fee_cents AS estimated_fee_cents,
  @estimated_slippage_cents AS estimated_slippage_cents,
  gross_pnl_1c_vs_stop_2c - @estimated_fee_cents - @estimated_slippage_cents AS estimated_net_pnl_1c_vs_stop_2c,
  gross_pnl_2c_vs_stop_2c - @estimated_fee_cents - @estimated_slippage_cents AS estimated_net_pnl_2c_vs_stop_2c,
  gross_pnl_3c_vs_stop_3c - @estimated_fee_cents - @estimated_slippage_cents AS estimated_net_pnl_3c_vs_stop_3c,
  gross_pnl_1c_vs_stop_083 - @estimated_fee_cents - @estimated_slippage_cents AS estimated_net_pnl_1c_vs_stop_083,
  gross_pnl_2c_vs_stop_083 - @estimated_fee_cents - @estimated_slippage_cents AS estimated_net_pnl_2c_vs_stop_083,
  gross_pnl_3c_vs_stop_083 - @estimated_fee_cents - @estimated_slippage_cents AS estimated_net_pnl_3c_vs_stop_083,
  btc_distance_min_after_entry <= 0 AS btc_crossed_back_over_strike,
  aligned_distance - btc_distance_min_after_entry >= 20 AS distance_compressed_by_20,
  aligned_distance - btc_distance_min_after_entry >= 40 AS distance_compressed_by_40,
  btc_distance_max_after_entry - aligned_distance >= 20 AS distance_expanded_by_20,
  btc_distance_max_after_entry - aligned_distance >= 40 AS distance_expanded_by_40
FROM pnl;

DROP TEMPORARY TABLE IF EXISTS vw_side_strength_daily_pnl;
CREATE TEMPORARY TABLE vw_side_strength_daily_pnl AS
SELECT
  dominant_side,
  entry_price_bucket,
  distance_bucket,
  support_bucket,
  time_of_day_bucket,
  hour_of_day_et,
  time_to_expiry_bucket,
  trend_confirming_10s,
  trend_confirming_30s,
  trend_confirming_60s,
  DATE(observed_at_et) AS active_day,
  SUM(gross_pnl_1c_vs_stop_2c) AS day_pnl_1c_vs_stop_2c
FROM vw_side_strength_outcomes
GROUP BY
  dominant_side, entry_price_bucket, distance_bucket, support_bucket,
  time_of_day_bucket, hour_of_day_et, time_to_expiry_bucket,
  trend_confirming_10s, trend_confirming_30s, trend_confirming_60s,
  DATE(observed_at_et);

DROP TEMPORARY TABLE IF EXISTS vw_side_strength_best_day;
CREATE TEMPORARY TABLE vw_side_strength_best_day AS
SELECT
  dominant_side,
  entry_price_bucket,
  distance_bucket,
  support_bucket,
  time_of_day_bucket,
  hour_of_day_et,
  time_to_expiry_bucket,
  trend_confirming_10s,
  trend_confirming_30s,
  trend_confirming_60s,
  MAX(day_pnl_1c_vs_stop_2c) AS best_day_pnl_1c_vs_stop_2c
FROM vw_side_strength_daily_pnl
GROUP BY
  dominant_side, entry_price_bucket, distance_bucket, support_bucket,
  time_of_day_bucket, hour_of_day_et, time_to_expiry_bucket,
  trend_confirming_10s, trend_confirming_30s, trend_confirming_60s;

DROP TEMPORARY TABLE IF EXISTS vw_side_strength_summary;
CREATE TEMPORARY TABLE vw_side_strength_summary AS
SELECT
  o.dominant_side,
  o.entry_price_bucket,
  o.distance_bucket,
  o.support_bucket,
  o.time_of_day_bucket,
  o.hour_of_day_et,
  o.time_to_expiry_bucket,
  o.trend_confirming_10s,
  o.trend_confirming_30s,
  o.trend_confirming_60s,
  SUM(o.raw_candidate_count_for_market_strategy) AS raw_candidates,
  COUNT(*) AS deduped_signals,
  COUNT(DISTINCT DATE(o.observed_at_et)) AS active_days,
  ROUND(100 * SUM(o.outcome_1c_vs_stop_2c = 'target_first') / COUNT(*), 1) AS target_1c_first_rate,
  ROUND(100 * SUM(o.outcome_2c_vs_stop_2c = 'target_first') / COUNT(*), 1) AS target_2c_first_rate,
  ROUND(100 * SUM(o.outcome_3c_vs_stop_3c = 'target_first') / COUNT(*), 1) AS target_3c_first_rate,
  ROUND(100 * SUM(o.outcome_1c_vs_stop_2c = 'stop_first') / COUNT(*), 1) AS stop_2c_first_rate,
  ROUND(100 * SUM(o.outcome_3c_vs_stop_3c = 'stop_first') / COUNT(*), 1) AS stop_3c_first_rate,
  ROUND(100 * SUM(o.stop_083_hit_at IS NOT NULL) / COUNT(*), 1) AS stop_083_first_rate,
  ROUND(AVG(o.gross_pnl_1c_vs_stop_2c), 4) AS avg_gross_pnl_1c_vs_stop_2c,
  ROUND(AVG(o.gross_pnl_2c_vs_stop_2c), 4) AS avg_gross_pnl_2c_vs_stop_2c,
  ROUND(AVG(o.gross_pnl_3c_vs_stop_3c), 4) AS avg_gross_pnl_3c_vs_stop_3c,
  ROUND(AVG(o.gross_pnl_1c_vs_stop_083), 4) AS avg_gross_pnl_1c_vs_stop_083,
  ROUND(AVG(o.gross_pnl_2c_vs_stop_083), 4) AS avg_gross_pnl_2c_vs_stop_083,
  ROUND(AVG(o.gross_pnl_3c_vs_stop_083), 4) AS avg_gross_pnl_3c_vs_stop_083,
  ROUND(AVG(o.estimated_net_pnl_1c_vs_stop_2c), 4) AS avg_estimated_net_pnl_1c_vs_stop_2c,
  ROUND(AVG(o.estimated_net_pnl_2c_vs_stop_2c), 4) AS avg_estimated_net_pnl_2c_vs_stop_2c,
  ROUND(AVG(o.estimated_net_pnl_3c_vs_stop_3c), 4) AS avg_estimated_net_pnl_3c_vs_stop_3c,
  ROUND(AVG(o.estimated_net_pnl_1c_vs_stop_083), 4) AS avg_estimated_net_pnl_1c_vs_stop_083,
  ROUND(AVG(o.estimated_net_pnl_2c_vs_stop_083), 4) AS avg_estimated_net_pnl_2c_vs_stop_083,
  ROUND(AVG(o.estimated_net_pnl_3c_vs_stop_083), 4) AS avg_estimated_net_pnl_3c_vs_stop_083,
  ROUND(SUM(CASE WHEN o.gross_pnl_1c_vs_stop_2c > 0 THEN o.gross_pnl_1c_vs_stop_2c ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN o.gross_pnl_1c_vs_stop_2c < 0 THEN o.gross_pnl_1c_vs_stop_2c ELSE 0 END)), 0), 4) AS profit_factor_1c_vs_stop_2c,
  ROUND(SUM(CASE WHEN o.gross_pnl_2c_vs_stop_2c > 0 THEN o.gross_pnl_2c_vs_stop_2c ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN o.gross_pnl_2c_vs_stop_2c < 0 THEN o.gross_pnl_2c_vs_stop_2c ELSE 0 END)), 0), 4) AS profit_factor_2c_vs_stop_2c,
  ROUND(SUM(CASE WHEN o.gross_pnl_3c_vs_stop_3c > 0 THEN o.gross_pnl_3c_vs_stop_3c ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN o.gross_pnl_3c_vs_stop_3c < 0 THEN o.gross_pnl_3c_vs_stop_3c ELSE 0 END)), 0), 4) AS profit_factor_3c_vs_stop_3c,
  ROUND(SUM(CASE WHEN o.gross_pnl_1c_vs_stop_083 > 0 THEN o.gross_pnl_1c_vs_stop_083 ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN o.gross_pnl_1c_vs_stop_083 < 0 THEN o.gross_pnl_1c_vs_stop_083 ELSE 0 END)), 0), 4) AS profit_factor_1c_vs_stop_083,
  ROUND(SUM(CASE WHEN o.gross_pnl_2c_vs_stop_083 > 0 THEN o.gross_pnl_2c_vs_stop_083 ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN o.gross_pnl_2c_vs_stop_083 < 0 THEN o.gross_pnl_2c_vs_stop_083 ELSE 0 END)), 0), 4) AS profit_factor_2c_vs_stop_083,
  ROUND(SUM(CASE WHEN o.gross_pnl_3c_vs_stop_083 > 0 THEN o.gross_pnl_3c_vs_stop_083 ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN o.gross_pnl_3c_vs_stop_083 < 0 THEN o.gross_pnl_3c_vs_stop_083 ELSE 0 END)), 0), 4) AS profit_factor_3c_vs_stop_083,
  ROUND(AVG(o.dominant_ask), 4) AS avg_entry_price,
  ROUND(AVG(o.aligned_distance), 2) AS avg_aligned_distance,
  ROUND(AVG(o.distance_support_ratio_avg), 4) AS avg_distance_support_ratio,
  ROUND(AVG(o.time_to_expiry_seconds), 1) AS avg_time_to_expiry,
  ROUND(AVG(o.spread), 4) AS avg_spread,
  COALESCE(SUM(o.quote_age_ms > @quote_age_max_ms), 0) AS stale_quote_count,
  SUM(o.spread = 0) AS locked_quote_count,
  SUM(o.dominant_bid > o.dominant_ask) AS crossed_quote_count,
  SUM(o.forward_quote_count = 0) AS no_forward_quote_count,
  ROUND(100 * SUM(o.btc_crossed_back_over_strike) / COUNT(*), 1) AS btc_crossback_rate,
  ROUND(100 * SUM(o.distance_compressed_by_40) / COUNT(*), 1) AS distance_compression_40_rate,
  ROUND(100 * SUM(o.distance_expanded_by_40) / COUNT(*), 1) AS distance_expansion_40_rate,
  ROUND(100 * MAX(dp.best_day_pnl_1c_vs_stop_2c) / NULLIF(SUM(o.gross_pnl_1c_vs_stop_2c), 0), 1) AS percent_profit_from_best_day
FROM vw_side_strength_outcomes o
LEFT JOIN vw_side_strength_best_day dp
  ON dp.dominant_side = o.dominant_side
 AND dp.entry_price_bucket = o.entry_price_bucket
 AND dp.distance_bucket = o.distance_bucket
 AND dp.support_bucket = o.support_bucket
 AND dp.time_of_day_bucket = o.time_of_day_bucket
 AND dp.hour_of_day_et = o.hour_of_day_et
 AND dp.time_to_expiry_bucket = o.time_to_expiry_bucket
 AND dp.trend_confirming_10s = o.trend_confirming_10s
 AND dp.trend_confirming_30s = o.trend_confirming_30s
 AND dp.trend_confirming_60s = o.trend_confirming_60s
GROUP BY
  o.dominant_side, o.entry_price_bucket, o.distance_bucket, o.support_bucket,
  o.time_of_day_bucket, o.hour_of_day_et, o.time_to_expiry_bucket,
  o.trend_confirming_10s, o.trend_confirming_30s, o.trend_confirming_60s;

DROP TEMPORARY TABLE IF EXISTS vw_side_strength_7pm_summary;
CREATE TEMPORARY TABLE vw_side_strength_7pm_summary AS
SELECT
  dominant_side,
  entry_price_bucket,
  distance_bucket,
  support_bucket,
  hour_of_day_et,
  COUNT(*) AS signal_count,
  COUNT(DISTINCT DATE(observed_at_et)) AS active_days,
  ROUND(100 * SUM(outcome_1c_vs_stop_2c = 'target_first') / COUNT(*), 1) AS target_1c_first_rate,
  ROUND(100 * SUM(outcome_2c_vs_stop_2c = 'target_first') / COUNT(*), 1) AS target_2c_first_rate,
  ROUND(100 * SUM(outcome_3c_vs_stop_3c = 'target_first') / COUNT(*), 1) AS target_3c_first_rate,
  ROUND(AVG(gross_pnl_1c_vs_stop_2c), 4) AS avg_gross_pnl_1c_vs_stop_2c,
  ROUND(AVG(estimated_net_pnl_1c_vs_stop_2c), 4) AS avg_estimated_net_pnl_1c_vs_stop_2c,
  ROUND(SUM(CASE WHEN gross_pnl_1c_vs_stop_2c > 0 THEN gross_pnl_1c_vs_stop_2c ELSE 0 END) / NULLIF(ABS(SUM(CASE WHEN gross_pnl_1c_vs_stop_2c < 0 THEN gross_pnl_1c_vs_stop_2c ELSE 0 END)), 0), 4) AS profit_factor_1c_vs_stop_2c,
  ROUND(AVG(aligned_distance), 2) AS avg_aligned_distance,
  ROUND(AVG(distance_support_ratio_avg), 4) AS avg_distance_support_ratio
FROM vw_side_strength_outcomes
WHERE hour_of_day_et BETWEEN 18 AND 20
GROUP BY dominant_side, entry_price_bucket, distance_bucket, support_bucket, hour_of_day_et;

DROP TEMPORARY TABLE IF EXISTS vw_side_strength_data_quality;
CREATE TEMPORARY TABLE vw_side_strength_data_quality AS
WITH quote_pivot AS (
  SELECT
    ms.id AS snapshot_id,
    ms.captured_at AS observed_at,
    DATE(CONVERT_TZ(ms.captured_at, '+00:00', '-04:00')) AS observed_day_et,
    m.market_id AS market_ticker,
    m.target_price AS strike,
    m.closes_at AS expiry_ts,
    ms.btc_price,
    MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
    MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
    MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
    MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
    MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
    MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
  FROM market_snapshots ms
  JOIN markets m ON m.id = ms.market_id
  JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
  JOIN contracts c ON c.id = cs.contract_id
  GROUP BY ms.id, ms.captured_at, DATE(CONVERT_TZ(ms.captured_at, '+00:00', '-04:00')),
           m.market_id, m.target_price, m.closes_at, ms.btc_price
),
issues AS (
  SELECT 'spread_eq_zero' AS issue, observed_at, NULL AS side FROM quote_pivot WHERE yes_spread = 0 OR no_spread = 0
  UNION ALL SELECT 'spread_lt_zero', observed_at, NULL FROM quote_pivot WHERE yes_spread < 0 OR no_spread < 0
  UNION ALL SELECT 'bid_gt_ask', observed_at, NULL FROM quote_pivot WHERE yes_bid > yes_ask OR no_bid > no_ask
  UNION ALL SELECT 'ask_out_of_range', observed_at, NULL FROM quote_pivot WHERE yes_ask < 0 OR yes_ask > 1 OR no_ask < 0 OR no_ask > 1
  UNION ALL SELECT 'bid_out_of_range', observed_at, NULL FROM quote_pivot WHERE yes_bid < 0 OR yes_bid > 1 OR no_bid < 0 OR no_bid > 1
  UNION ALL SELECT 'quote_age_ms_unavailable', observed_at, NULL FROM vw_side_strength_candidates
  UNION ALL SELECT 'missing_btc_price', observed_at, NULL FROM quote_pivot WHERE btc_price IS NULL
  UNION ALL SELECT 'missing_strike', observed_at, NULL FROM quote_pivot WHERE strike IS NULL
  UNION ALL SELECT 'missing_expiry', observed_at, NULL FROM quote_pivot WHERE expiry_ts IS NULL
  UNION ALL SELECT 'missing_yes_or_no_quote', observed_at, NULL FROM quote_pivot
    WHERE yes_bid IS NULL OR yes_ask IS NULL OR no_bid IS NULL OR no_ask IS NULL
  UNION ALL SELECT 'no_forward_quotes_next_120s', observed_at, dominant_side FROM vw_side_strength_outcomes WHERE forward_quote_count = 0
  UNION ALL SELECT 'duplicate_candidate_snapshots_per_market', observed_at, dominant_side FROM vw_side_strength_candidates c
    WHERE (SELECT COUNT(*) FROM vw_side_strength_candidates d WHERE d.market_ticker = c.market_ticker AND d.strategy_version = c.strategy_version) > 1
  UNION ALL SELECT 'aligned_distance_lte_zero', observed_at, dominant_side FROM vw_side_strength_candidates WHERE aligned_distance <= 0
  UNION ALL SELECT 'dominant_side_disagrees_with_btc_side', observed_at, dominant_side
    FROM vw_side_strength_candidates
    WHERE (dominant_side = 'YES' AND signed_distance_for_yes <= 0)
       OR (dominant_side = 'NO' AND signed_distance_for_no <= 0)
)
SELECT
  issue,
  side,
  COUNT(*) AS rows_affected,
  MAX(observed_at) AS latest_seen
FROM issues
GROUP BY issue, side
UNION ALL
SELECT
  'locked_quote_count_by_day_and_side' AS issue,
  dominant_side AS side,
  COUNT(*) AS rows_affected,
  MAX(observed_at) AS latest_seen
FROM vw_side_strength_candidates
WHERE spread = 0
GROUP BY DATE(observed_at_et), dominant_side;

SELECT * FROM vw_side_strength_candidates ORDER BY observed_at DESC LIMIT 200;
SELECT * FROM vw_side_strength_signals ORDER BY observed_at DESC LIMIT 200;
SELECT * FROM vw_side_strength_outcomes ORDER BY observed_at DESC LIMIT 200;
SELECT * FROM vw_side_strength_summary
ORDER BY avg_estimated_net_pnl_1c_vs_stop_2c DESC, deduped_signals DESC
LIMIT 200;
SELECT * FROM vw_side_strength_7pm_summary
ORDER BY avg_estimated_net_pnl_1c_vs_stop_2c DESC, signal_count DESC;
SELECT * FROM vw_side_strength_data_quality
ORDER BY rows_affected DESC, issue;

SELECT
  'research_warning' AS warning_type,
  'Do not trust top groups with fewer than 30 deduped signals or fewer than 5 active days. This query is diagnostics only and does not justify live trading by itself.' AS warning_text;
