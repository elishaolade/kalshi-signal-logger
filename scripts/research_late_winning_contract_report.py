#!/usr/bin/env python3
"""
Build a read-only research report for the late winning-contract strategy.

Hypothesis:
  With 7-8 minutes left in a Kalshi BTC 15m market, if BTC is already at least
  $100 past the strike and the contract on that side is 75-91c with a tight
  spread, contract-price drawdown may be noise while BTC crossback/distance is
  the better stop signal.

Outputs:
  - candidate-level CSV
  - grouped summary CSV
  - data-quality CSV
  - markdown report with direct answers

No production tables are created, updated, deleted, or altered. The script uses
session temporary tables on a single MySQL connection.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "late_winning_contract"


PARAM_SQL = """
SET SESSION group_concat_max_len = 1048576
"""


DROP_SQL = [
    "DROP TEMPORARY TABLE IF EXISTS lws_quote_pivot",
    "DROP TEMPORARY TABLE IF EXISTS lws_final_btc",
    "DROP TEMPORARY TABLE IF EXISTS lws_raw_candidates",
    "DROP TEMPORARY TABLE IF EXISTS lws_candidates",
    "DROP TEMPORARY TABLE IF EXISTS lws_future_quotes",
    "DROP TEMPORARY TABLE IF EXISTS lws_aggregated",
    "DROP TEMPORARY TABLE IF EXISTS lws_settlement",
    "DROP TEMPORARY TABLE IF EXISTS lws_outcomes",
    "DROP TEMPORARY TABLE IF EXISTS lws_data_quality",
]


CREATE_QUOTE_PIVOT_SQL = """
CREATE TEMPORARY TABLE lws_quote_pivot AS
SELECT
  ms.id AS snapshot_id,
  ms.captured_at AS observation_ts,
  CONVERT_TZ(ms.captured_at, '+00:00', '-04:00') AS observation_ts_et,
  m.id AS market_pk,
  m.market_id AS market_ticker,
  m.target_price AS strike,
  m.closes_at AS expiry_ts,
  m.status AS market_status,
  m.raw_payload AS market_raw_payload,
  ms.btc_price,
  ms.time_remaining_seconds,
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
WHERE m.market_id LIKE 'KXBTC15M-%'
  AND ms.time_remaining_seconds BETWEEN 420 AND 480
GROUP BY
  ms.id, ms.captured_at, CONVERT_TZ(ms.captured_at, '+00:00', '-04:00'),
  m.id, m.market_id, m.target_price, m.closes_at, m.status, m.raw_payload,
  ms.btc_price, ms.time_remaining_seconds, ms.source
"""


CREATE_FINAL_BTC_SQL = """
CREATE TEMPORARY TABLE lws_final_btc AS
SELECT
  c.market_pk,
  CAST(SUBSTRING_INDEX(GROUP_CONCAT(ms.btc_price ORDER BY ms.captured_at DESC), ',', 1) AS DECIMAL(18,4)) AS final_btc_price,
  MAX(ms.captured_at) AS final_btc_snapshot_at
FROM (
  SELECT DISTINCT market_pk, expiry_ts
  FROM lws_candidates
) c
JOIN market_snapshots ms
  ON ms.market_id = c.market_pk
 AND ms.captured_at <= c.expiry_ts + INTERVAL 30 SECOND
GROUP BY c.market_pk
"""


CREATE_RAW_CANDIDATES_SQL = """
CREATE TEMPORARY TABLE lws_raw_candidates AS
WITH enriched AS (
  SELECT
    q.*,
    ROUND((q.yes_bid + q.yes_ask) / 2, 4) AS yes_mid,
    ROUND((q.no_bid + q.no_ask) / 2, 4) AS no_mid,
    CASE
      WHEN q.btc_price > q.strike THEN 'YES'
      WHEN q.btc_price < q.strike THEN 'NO'
      ELSE NULL
    END AS side,
    CASE
      WHEN q.btc_price > q.strike THEN q.btc_price - q.strike
      WHEN q.btc_price < q.strike THEN q.strike - q.btc_price
      ELSE 0
    END AS entry_distance,
    CAST(NULL AS SIGNED) AS quote_age_ms
  FROM lws_quote_pivot q
),
priced AS (
  SELECT
    e.*,
    CASE WHEN e.side = 'YES' THEN e.yes_contract_pk ELSE e.no_contract_pk END AS contract_pk,
    CASE WHEN e.side = 'YES' THEN e.yes_contract_ticker ELSE e.no_contract_ticker END AS contract_ticker,
    CASE WHEN e.side = 'YES' THEN e.yes_bid ELSE e.no_bid END AS entry_bid,
    CASE WHEN e.side = 'YES' THEN e.yes_ask ELSE e.no_ask END AS entry_ask,
    CASE WHEN e.side = 'YES' THEN e.yes_mid ELSE e.no_mid END AS entry_mid,
    CASE WHEN e.side = 'YES' THEN e.yes_spread ELSE e.no_spread END AS spread
  FROM enriched e
)
SELECT
  *,
  entry_ask AS entry_price_used,
  CASE
    WHEN entry_ask >= 0.75 AND entry_ask < 0.80 THEN '75-79c'
    WHEN entry_ask >= 0.80 AND entry_ask < 0.85 THEN '80-84c'
    WHEN entry_ask >= 0.85 AND entry_ask < 0.90 THEN '85-89c'
    WHEN entry_ask >= 0.90 AND entry_ask <= 0.91 THEN '90-91c'
    ELSE 'other'
  END AS entry_price_bucket,
  CASE
    WHEN entry_distance >= 100 AND entry_distance < 125 THEN '$100-$124'
    WHEN entry_distance >= 125 AND entry_distance < 150 THEN '$125-$149'
    WHEN entry_distance >= 150 AND entry_distance < 200 THEN '$150-$199'
    WHEN entry_distance >= 200 THEN '$200+'
    ELSE '<$100'
  END AS entry_distance_bucket,
  CASE
    WHEN time_remaining_seconds BETWEEN 420 AND 450 THEN '420-450s'
    WHEN time_remaining_seconds BETWEEN 451 AND 480 THEN '451-480s'
    ELSE 'other'
  END AS tte_bucket,
  ROW_NUMBER() OVER (
    PARTITION BY market_ticker, side
    ORDER BY observation_ts ASC
  ) AS rn_for_market_side,
  COUNT(*) OVER (
    PARTITION BY market_ticker, side
  ) AS raw_candidate_count_for_market_side
FROM priced
WHERE side IN ('YES', 'NO')
  AND entry_distance >= 100
  AND time_remaining_seconds BETWEEN 420 AND 480
  AND entry_ask >= 0.75
  AND entry_ask <= 0.91
  AND spread <= 0.01
"""


CREATE_CANDIDATES_SQL = """
CREATE TEMPORARY TABLE lws_candidates AS
SELECT
  ROW_NUMBER() OVER (ORDER BY observation_ts, market_ticker, side) AS signal_id,
  market_ticker,
  contract_ticker,
  market_pk,
  contract_pk,
  expiry_ts,
  observation_ts,
  observation_ts_et,
  side,
  strike,
  btc_price AS btc_price_at_entry,
  entry_distance,
  time_remaining_seconds AS time_to_expiry_seconds,
  entry_bid,
  entry_ask,
  entry_mid,
  entry_price_used,
  spread,
  quote_age_ms,
  entry_price_bucket,
  entry_distance_bucket,
  tte_bucket,
  market_status,
  market_raw_payload,
  raw_candidate_count_for_market_side
FROM lws_raw_candidates
WHERE rn_for_market_side = 1
"""


CREATE_FUTURE_QUOTES_SQL = """
CREATE TEMPORARY TABLE lws_future_quotes AS
SELECT
  c.signal_id,
  fms.captured_at,
  CASE
    WHEN c.side = 'YES' THEN fms.btc_price - c.strike
    WHEN c.side = 'NO' THEN c.strike - fms.btc_price
  END AS future_entry_side_distance,
  fms.btc_price AS future_btc_price,
  fcs.bid_price AS future_bid
FROM lws_candidates c
JOIN market_snapshots fms
  ON fms.market_id = c.market_pk
 AND fms.captured_at > c.observation_ts
 AND fms.captured_at <= c.expiry_ts
JOIN contract_snapshots fcs
  ON fcs.market_snapshot_id = fms.id
 AND fcs.contract_id = c.contract_pk
"""


CREATE_AGGREGATED_SQL = """
CREATE TEMPORARY TABLE lws_aggregated AS
SELECT
  signal_id,
  MIN(future_entry_side_distance) AS min_btc_distance_after_entry,
  MIN(CASE WHEN future_entry_side_distance <= 0 THEN captured_at END) AS btc_cross_target_at,
  MIN(CASE WHEN future_entry_side_distance <= 50 THEN captured_at END) AS btc_distance_below_50_at,
  MIN(CASE WHEN future_entry_side_distance <= 25 THEN captured_at END) AS btc_distance_below_25_at,
  CAST(SUBSTRING_INDEX(GROUP_CONCAT(CASE WHEN future_entry_side_distance <= 0 THEN future_bid END ORDER BY captured_at ASC), ',', 1) AS DECIMAL(10,4)) AS bid_at_btc_cross_target,
  CAST(SUBSTRING_INDEX(GROUP_CONCAT(CASE WHEN future_entry_side_distance <= 50 THEN future_bid END ORDER BY captured_at ASC), ',', 1) AS DECIMAL(10,4)) AS bid_at_btc_distance_below_50,
  CAST(SUBSTRING_INDEX(GROUP_CONCAT(CASE WHEN future_entry_side_distance <= 25 THEN future_bid END ORDER BY captured_at ASC), ',', 1) AS DECIMAL(10,4)) AS bid_at_btc_distance_below_25,
  MIN(future_bid) AS lowest_contract_bid_after_entry,
  MAX(future_bid) AS max_contract_bid_after_entry,
  COUNT(future_bid) AS forward_quote_count
FROM lws_future_quotes
GROUP BY signal_id
"""


CREATE_SETTLEMENT_SQL = """
CREATE TEMPORARY TABLE lws_settlement AS
SELECT
  c.signal_id,
  fb.final_btc_price,
  fb.final_btc_snapshot_at,
  UPPER(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(c.market_raw_payload, '$.result')), 'null')) AS raw_result,
  UPPER(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(c.market_raw_payload, '$.settlement_value')), 'null')) AS raw_settlement_value,
  UPPER(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(c.market_raw_payload, '$.winning_outcome')), 'null')) AS raw_winning_outcome
FROM lws_candidates c
LEFT JOIN lws_final_btc fb ON fb.market_pk = c.market_pk
"""


CREATE_OUTCOMES_SQL = """
CREATE TEMPORARY TABLE lws_outcomes AS
WITH scored AS (
  SELECT
    c.*,
    COALESCE(a.forward_quote_count, 0) AS forward_quote_count,
    s.final_btc_price,
    s.final_btc_snapshot_at,
    CASE
      WHEN s.raw_result IN ('YES', 'NO') THEN s.raw_result
      WHEN s.raw_winning_outcome IN ('YES', 'NO') THEN s.raw_winning_outcome
      WHEN s.raw_settlement_value IN ('YES', 'NO') THEN s.raw_settlement_value
      WHEN s.raw_result IN ('1', 'TRUE') THEN 'YES'
      WHEN s.raw_result IN ('0', 'FALSE') THEN 'NO'
      WHEN s.raw_settlement_value IN ('1', 'TRUE') THEN 'YES'
      WHEN s.raw_settlement_value IN ('0', 'FALSE') THEN 'NO'
      WHEN s.final_btc_price > c.strike THEN 'YES'
      WHEN s.final_btc_price < c.strike THEN 'NO'
      ELSE NULL
    END AS final_settlement_winner,
    CASE
      WHEN s.raw_result IN ('YES', 'NO')
        OR s.raw_winning_outcome IN ('YES', 'NO')
        OR s.raw_settlement_value IN ('YES', 'NO', '1', '0', 'TRUE', 'FALSE')
        OR s.raw_result IN ('1', '0', 'TRUE', 'FALSE')
      THEN 'market_raw_payload'
      WHEN s.final_btc_price IS NOT NULL THEN 'final_btc_snapshot'
      ELSE 'unknown'
    END AS settlement_winner_source,
    a.min_btc_distance_after_entry,
    a.btc_cross_target_at,
    TIMESTAMPDIFF(SECOND, c.observation_ts, a.btc_cross_target_at) AS seconds_until_btc_crossed_target,
    a.btc_distance_below_50_at,
    TIMESTAMPDIFF(SECOND, c.observation_ts, a.btc_distance_below_50_at) AS seconds_until_btc_distance_below_50,
    a.btc_distance_below_25_at,
    TIMESTAMPDIFF(SECOND, c.observation_ts, a.btc_distance_below_25_at) AS seconds_until_btc_distance_below_25,
    a.bid_at_btc_cross_target,
    a.bid_at_btc_distance_below_50,
    a.bid_at_btc_distance_below_25,
    a.lowest_contract_bid_after_entry,
    ROUND((c.entry_price_used - a.lowest_contract_bid_after_entry) * 100, 4) AS max_contract_drawdown_cents,
    a.max_contract_bid_after_entry
  FROM lws_candidates c
  LEFT JOIN lws_aggregated a ON a.signal_id = c.signal_id
  LEFT JOIN lws_settlement s ON s.signal_id = c.signal_id
)
SELECT
  *,
  final_settlement_winner = side AS did_entry_side_win_settlement,
  btc_cross_target_at IS NOT NULL AS did_btc_cross_target_after_entry,
  lowest_contract_bid_after_entry <= entry_price_used - 0.05 AS did_contract_drop_5c,
  lowest_contract_bid_after_entry <= entry_price_used - 0.10 AS did_contract_drop_10c,
  lowest_contract_bid_after_entry <= entry_price_used - 0.15 AS did_contract_drop_15c,
  lowest_contract_bid_after_entry <= entry_price_used - 0.20 AS did_contract_drop_20c,
  lowest_contract_bid_after_entry < entry_price_used AND max_contract_bid_after_entry >= entry_price_used AS did_contract_recover_to_entry,
  CASE
    WHEN final_settlement_winner = side THEN ROUND((1.0 - entry_price_used) * 100, 4)
    WHEN final_settlement_winner IS NOT NULL THEN ROUND(-entry_price_used * 100, 4)
    ELSE NULL
  END AS contract_pnl_if_held_to_settlement,
  CASE
    WHEN lowest_contract_bid_after_entry <= entry_price_used - 0.05 THEN -5.0
    WHEN final_settlement_winner = side THEN ROUND((1.0 - entry_price_used) * 100, 4)
    WHEN final_settlement_winner IS NOT NULL THEN ROUND(-entry_price_used * 100, 4)
    ELSE NULL
  END AS contract_pnl_if_stopped_at_5c,
  CASE
    WHEN lowest_contract_bid_after_entry <= entry_price_used - 0.10 THEN -10.0
    WHEN final_settlement_winner = side THEN ROUND((1.0 - entry_price_used) * 100, 4)
    WHEN final_settlement_winner IS NOT NULL THEN ROUND(-entry_price_used * 100, 4)
    ELSE NULL
  END AS contract_pnl_if_stopped_at_10c,
  CASE
    WHEN lowest_contract_bid_after_entry <= entry_price_used - 0.15 THEN -15.0
    WHEN final_settlement_winner = side THEN ROUND((1.0 - entry_price_used) * 100, 4)
    WHEN final_settlement_winner IS NOT NULL THEN ROUND(-entry_price_used * 100, 4)
    ELSE NULL
  END AS contract_pnl_if_stopped_at_15c,
  CASE
    WHEN btc_cross_target_at IS NOT NULL THEN ROUND((bid_at_btc_cross_target - entry_price_used) * 100, 4)
    WHEN final_settlement_winner = side THEN ROUND((1.0 - entry_price_used) * 100, 4)
    WHEN final_settlement_winner IS NOT NULL THEN ROUND(-entry_price_used * 100, 4)
    ELSE NULL
  END AS contract_pnl_if_exited_only_on_btc_cross,
  CASE
    WHEN btc_distance_below_50_at IS NOT NULL THEN ROUND((bid_at_btc_distance_below_50 - entry_price_used) * 100, 4)
    WHEN final_settlement_winner = side THEN ROUND((1.0 - entry_price_used) * 100, 4)
    WHEN final_settlement_winner IS NOT NULL THEN ROUND(-entry_price_used * 100, 4)
    ELSE NULL
  END AS contract_pnl_if_exited_when_btc_distance_below_50,
  CASE
    WHEN btc_distance_below_25_at IS NOT NULL THEN ROUND((bid_at_btc_distance_below_25 - entry_price_used) * 100, 4)
    WHEN final_settlement_winner = side THEN ROUND((1.0 - entry_price_used) * 100, 4)
    WHEN final_settlement_winner IS NOT NULL THEN ROUND(-entry_price_used * 100, 4)
    ELSE NULL
  END AS contract_pnl_if_exited_when_btc_distance_below_25,
  FLOOR(100 / entry_price_used) AS contracts_for_100_notional,
  ROUND((CASE
    WHEN final_settlement_winner = side THEN (1.0 - entry_price_used)
    WHEN final_settlement_winner IS NOT NULL THEN -entry_price_used
    ELSE NULL
  END) * FLOOR(100 / entry_price_used), 4) AS modeled_pnl_dollars_per_100_notional
FROM scored
"""


CREATE_DATA_QUALITY_SQL = """
CREATE TEMPORARY TABLE lws_data_quality AS
WITH issues AS (
  SELECT 'missing_yes_quote' AS issue, observation_ts, NULL AS side
  FROM lws_quote_pivot WHERE yes_bid IS NULL OR yes_ask IS NULL
  UNION ALL SELECT 'missing_no_quote', observation_ts, NULL
  FROM lws_quote_pivot WHERE no_bid IS NULL OR no_ask IS NULL
  UNION ALL SELECT 'missing_btc_price', observation_ts, NULL
  FROM lws_quote_pivot WHERE btc_price IS NULL
  UNION ALL SELECT 'missing_strike', observation_ts, NULL
  FROM lws_quote_pivot WHERE strike IS NULL
  UNION ALL SELECT 'missing_expiry', observation_ts, NULL
  FROM lws_quote_pivot WHERE expiry_ts IS NULL
  UNION ALL SELECT 'spread_eq_zero', observation_ts, NULL
  FROM lws_quote_pivot WHERE yes_spread = 0 OR no_spread = 0
  UNION ALL SELECT 'spread_lt_zero', observation_ts, NULL
  FROM lws_quote_pivot WHERE yes_spread < 0 OR no_spread < 0
  UNION ALL SELECT 'bid_gt_ask', observation_ts, NULL
  FROM lws_quote_pivot WHERE yes_bid > yes_ask OR no_bid > no_ask
  UNION ALL SELECT 'ask_out_of_range', observation_ts, NULL
  FROM lws_quote_pivot WHERE yes_ask < 0 OR yes_ask > 1 OR no_ask < 0 OR no_ask > 1
  UNION ALL SELECT 'bid_out_of_range', observation_ts, NULL
  FROM lws_quote_pivot WHERE yes_bid < 0 OR yes_bid > 1 OR no_bid < 0 OR no_bid > 1
  UNION ALL SELECT 'quote_age_ms_unavailable_in_historical_snapshots', observation_ts, side
  FROM lws_candidates WHERE quote_age_ms IS NULL
  UNION ALL SELECT 'duplicate_qualifying_observations_per_market_side', observation_ts, side
  FROM lws_raw_candidates WHERE raw_candidate_count_for_market_side > 1
  UNION ALL SELECT 'no_forward_quotes_after_entry', observation_ts, side
  FROM lws_outcomes WHERE forward_quote_count = 0
  UNION ALL SELECT 'missing_settlement_winner', observation_ts, side
  FROM lws_outcomes WHERE final_settlement_winner IS NULL
)
SELECT
  issue,
  side,
  COUNT(*) AS rows_affected,
  MAX(observation_ts) AS latest_seen
FROM issues
GROUP BY issue, side
ORDER BY rows_affected DESC, issue, side
"""


OUTCOME_SQL = """
SELECT
  market_ticker,
  contract_ticker,
  expiry_ts,
  observation_ts,
  observation_ts_et,
  side,
  strike,
  btc_price_at_entry,
  entry_distance,
  time_to_expiry_seconds,
  entry_bid,
  entry_ask,
  entry_mid,
  entry_price_used,
  spread,
  entry_price_bucket,
  entry_distance_bucket,
  tte_bucket,
  final_settlement_winner,
  settlement_winner_source,
  did_entry_side_win_settlement,
  min_btc_distance_after_entry,
  did_btc_cross_target_after_entry,
  seconds_until_btc_crossed_target,
  lowest_contract_bid_after_entry,
  max_contract_drawdown_cents,
  did_contract_drop_5c,
  did_contract_drop_10c,
  did_contract_drop_15c,
  did_contract_drop_20c,
  did_contract_recover_to_entry,
  max_contract_bid_after_entry,
  contract_pnl_if_held_to_settlement,
  contract_pnl_if_stopped_at_5c,
  contract_pnl_if_stopped_at_10c,
  contract_pnl_if_stopped_at_15c,
  contract_pnl_if_exited_only_on_btc_cross,
  contract_pnl_if_exited_when_btc_distance_below_50,
  contract_pnl_if_exited_when_btc_distance_below_25,
  contracts_for_100_notional,
  modeled_pnl_dollars_per_100_notional,
  forward_quote_count,
  raw_candidate_count_for_market_side
FROM lws_outcomes
ORDER BY observation_ts, market_ticker, side
"""


DATA_QUALITY_SQL = "SELECT * FROM lws_data_quality"


SCHEMA_DISCOVERY_ROWS = [
    {
        "section": "schema_discovery",
        "table_name": "markets",
        "relevant_columns": "id, market_id, target_price, closes_at, status, raw_payload",
    },
    {
        "section": "schema_discovery",
        "table_name": "contracts",
        "relevant_columns": "id, market_id, contract_id, side",
    },
    {
        "section": "schema_discovery",
        "table_name": "market_snapshots",
        "relevant_columns": "id, market_id, captured_at, btc_price, time_remaining_seconds, source",
    },
    {
        "section": "schema_discovery",
        "table_name": "contract_snapshots",
        "relevant_columns": "market_snapshot_id, contract_id, captured_at, bid_price, ask_price, spread",
    },
]


@dataclass(frozen=True)
class OutputPaths:
    directory: Path
    candidates_csv: Path
    summary_csv: Path
    data_quality_csv: Path
    markdown_report: Path


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, Decimal)):
        return int(value) == 1
    return str(value).strip().lower() in {"1", "true", "yes"}


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        if math.isnan(value):
            return "NULL"
        return f"{value:.{digits}f}"
    if isinstance(value, Decimal):
        return f"{float(value):.{digits}f}"
    return str(value)


def _avg(values: Iterable[Any]) -> float | None:
    vals = [_to_float(v) for v in values]
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _median(values: Iterable[Any]) -> float | None:
    vals = [_to_float(v) for v in values]
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    if not vals:
        return None
    return float(statistics.median(vals))


def _pct(count: int, total: int) -> float | None:
    if total <= 0:
        return None
    return 100.0 * count / total


def _profit_factor(values: Iterable[Any]) -> float | None:
    vals = [_to_float(v) for v in values]
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    gains = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    if losses == 0:
        return None if gains == 0 else math.inf
    return gains / losses


def _max_losing_streak(rows: list[dict[str, Any]], pnl_key: str) -> int:
    longest = 0
    current = 0
    for row in sorted(rows, key=lambda r: str(r.get("observation_ts") or "")):
        pnl = _to_float(row.get(pnl_key))
        if pnl is not None and pnl < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _worst_drawdown_path(rows: list[dict[str, Any]], pnl_key: str) -> float | None:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    seen = False
    for row in sorted(rows, key=lambda r: str(r.get("observation_ts") or "")):
        pnl = _to_float(row.get(pnl_key))
        if pnl is None:
            continue
        seen = True
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst if seen else None


def _summary_row(key: tuple[Any, ...], rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for r in rows if _is_true(r.get("did_entry_side_win_settlement")))
    losses = sum(1 for r in rows if r.get("final_settlement_winner") is not None and not _is_true(r.get("did_entry_side_win_settlement")))
    held_pnls = [r.get("contract_pnl_if_held_to_settlement") for r in rows]
    modeled_100 = [r.get("modeled_pnl_dollars_per_100_notional") for r in rows]
    return {
        "entry_price_bucket": key[0],
        "entry_distance_bucket": key[1],
        "side": key[2],
        "tte_bucket": key[3],
        "deduped_qualifying_trades": n,
        "settlement_wins": wins,
        "settlement_losses": losses,
        "win_rate_to_settlement_pct": _pct(wins, wins + losses),
        "btc_crossback_rate_pct": _pct(sum(1 for r in rows if _is_true(r.get("did_btc_cross_target_after_entry"))), n),
        "avg_entry_price": _avg(r.get("entry_price_used") for r in rows),
        "avg_max_contract_drawdown_cents": _avg(r.get("max_contract_drawdown_cents") for r in rows),
        "median_max_contract_drawdown_cents": _median(r.get("max_contract_drawdown_cents") for r in rows),
        "drop_5c_pct": _pct(sum(1 for r in rows if _is_true(r.get("did_contract_drop_5c"))), n),
        "drop_10c_pct": _pct(sum(1 for r in rows if _is_true(r.get("did_contract_drop_10c"))), n),
        "drop_15c_pct": _pct(sum(1 for r in rows if _is_true(r.get("did_contract_drop_15c"))), n),
        "drop_20c_pct": _pct(sum(1 for r in rows if _is_true(r.get("did_contract_drop_20c"))), n),
        "avg_min_btc_distance_after_entry": _avg(r.get("min_btc_distance_after_entry") for r in rows),
        "avg_pnl_cents_if_held_to_settlement": _avg(held_pnls),
        "avg_pnl_dollars_per_100_notional": _avg(modeled_100),
        "total_modeled_pnl_dollars_100_per_trade": sum(v for v in (_to_float(x) for x in modeled_100) if v is not None),
        "avg_pnl_stop_5c_cents": _avg(r.get("contract_pnl_if_stopped_at_5c") for r in rows),
        "avg_pnl_stop_10c_cents": _avg(r.get("contract_pnl_if_stopped_at_10c") for r in rows),
        "avg_pnl_stop_15c_cents": _avg(r.get("contract_pnl_if_stopped_at_15c") for r in rows),
        "avg_pnl_btc_cross_stop_cents": _avg(r.get("contract_pnl_if_exited_only_on_btc_cross") for r in rows),
        "avg_pnl_btc_distance_below_50_stop_cents": _avg(r.get("contract_pnl_if_exited_when_btc_distance_below_50") for r in rows),
        "avg_pnl_btc_distance_below_25_stop_cents": _avg(r.get("contract_pnl_if_exited_when_btc_distance_below_25") for r in rows),
        "profit_factor_held_to_settlement": _profit_factor(held_pnls),
        "max_losing_streak_held_to_settlement": _max_losing_streak(rows, "contract_pnl_if_held_to_settlement"),
        "worst_drawdown_path_dollars_100_per_trade": _worst_drawdown_path(rows, "modeled_pnl_dollars_per_100_notional"),
    }


def _build_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            row.get("entry_price_bucket"),
            row.get("entry_distance_bucket"),
            row.get("side"),
            row.get("tte_bucket"),
        )
        groups[key].append(row)

    summary = [_summary_row(key, group_rows) for key, group_rows in groups.items()]
    summary.sort(
        key=lambda r: (
            -int(r["deduped_qualifying_trades"]),
            str(r["entry_price_bucket"]),
            str(r["entry_distance_bucket"]),
            str(r["side"]),
            str(r["tte_bucket"]),
        )
    )
    return summary


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    columns = list(rows[0].keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _top_summary(summary: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    def score(row: dict[str, Any]) -> tuple[float, int]:
        avg = _to_float(row.get("avg_pnl_dollars_per_100_notional"))
        return (avg if avg is not None else -999999.0, int(row.get("deduped_qualifying_trades") or 0))

    return sorted(summary, key=score, reverse=True)[:limit]


def _money_model(rows: list[dict[str, Any]], pnl_key: str) -> dict[str, Any]:
    pnls = [r.get(pnl_key) for r in rows]
    return {
        "avg_cents": _avg(pnls),
        "profit_factor": _profit_factor(pnls),
    }


def _count_winner_drops(rows: list[dict[str, Any]], key: str) -> int:
    return sum(
        1
        for r in rows
        if _is_true(r.get("did_entry_side_win_settlement")) and _is_true(r.get(key))
    )


def _render_markdown(rows: list[dict[str, Any]], summary: list[dict[str, Any]], quality: list[dict[str, Any]], paths: OutputPaths) -> str:
    n = len(rows)
    winners = [r for r in rows if _is_true(r.get("did_entry_side_win_settlement"))]
    losers = [
        r
        for r in rows
        if r.get("final_settlement_winner") is not None and not _is_true(r.get("did_entry_side_win_settlement"))
    ]
    unknown = n - len(winners) - len(losers)
    loser_crosses = sum(1 for r in losers if _is_true(r.get("did_btc_cross_target_after_entry")))

    distance_stop_rows = [
        {
            "stop": "BTC cross target",
            "losers_stopped": sum(1 for r in losers if _is_true(r.get("did_btc_cross_target_after_entry"))),
            "winners_stopped": sum(1 for r in winners if _is_true(r.get("did_btc_cross_target_after_entry"))),
            **_money_model(rows, "contract_pnl_if_exited_only_on_btc_cross"),
        },
        {
            "stop": "BTC distance below $50",
            "losers_stopped": sum(1 for r in losers if r.get("btc_distance_below_50_at") is not None),
            "winners_stopped": sum(1 for r in winners if r.get("btc_distance_below_50_at") is not None),
            **_money_model(rows, "contract_pnl_if_exited_when_btc_distance_below_50"),
        },
        {
            "stop": "BTC distance below $25",
            "losers_stopped": sum(1 for r in losers if r.get("btc_distance_below_25_at") is not None),
            "winners_stopped": sum(1 for r in winners if r.get("btc_distance_below_25_at") is not None),
            **_money_model(rows, "contract_pnl_if_exited_when_btc_distance_below_25"),
        },
    ]
    best_distance_stop = sorted(
        distance_stop_rows,
        key=lambda r: (
            _pct(int(r["losers_stopped"]), len(losers)) or 0,
            -(_pct(int(r["winners_stopped"]), len(winners)) or 0),
            _to_float(r.get("avg_cents")) or -999999.0,
        ),
        reverse=True,
    )[0] if distance_stop_rows else None

    contract_stop_models = [
        ("5c contract stop", "contract_pnl_if_stopped_at_5c"),
        ("10c contract stop", "contract_pnl_if_stopped_at_10c"),
        ("15c contract stop", "contract_pnl_if_stopped_at_15c"),
        ("BTC cross stop", "contract_pnl_if_exited_only_on_btc_cross"),
        ("BTC distance < $50 stop", "contract_pnl_if_exited_when_btc_distance_below_50"),
        ("BTC distance < $25 stop", "contract_pnl_if_exited_when_btc_distance_below_25"),
        ("Hold to settlement", "contract_pnl_if_held_to_settlement"),
    ]

    lines = [
        "# Late Winning-Contract Research Report",
        "",
        f"Generated: `{date.today().isoformat()}`",
        "",
        "## Scope",
        "",
        "- Market: Kalshi BTC 15-minute contracts",
        "- Observation window: 420-480 seconds to expiry",
        "- Entry side: YES when BTC > strike, NO when BTC < strike",
        "- Required distance: entry side at least $100 past target",
        "- Entry ask: 0.75-0.91",
        "- Spread max: 0.01",
        "- Entry execution: buy at ask",
        "- Deduplication: first qualifying observation per market/side",
        "",
        "## Direct Answers",
        "",
        f"1. Entries analyzed: `{n}`. Settlement losses: `{len(losers)}`. Settlement wins: `{len(winners)}`. Unknown settlement: `{unknown}`.",
        f"2. Winning entries that still dropped 5c/10c/15c/20c: `{_count_winner_drops(rows, 'did_contract_drop_5c')}` / `{_count_winner_drops(rows, 'did_contract_drop_10c')}` / `{_count_winner_drops(rows, 'did_contract_drop_15c')}` / `{_count_winner_drops(rows, 'did_contract_drop_20c')}`.",
        f"3. Losing entries where BTC crossed back through target: `{loser_crosses}` of `{len(losers)}`.",
    ]

    if best_distance_stop:
        lines.append(
            "4. Best BTC-distance stop by loser capture versus winner preservation: "
            f"`{best_distance_stop['stop']}` "
            f"(stopped `{best_distance_stop['losers_stopped']}` losers and `{best_distance_stop['winners_stopped']}` winners)."
        )
    else:
        lines.append("4. Best BTC-distance stop: not available because no rows were returned.")

    held = _money_model(rows, "contract_pnl_if_held_to_settlement")
    btc_cross = _money_model(rows, "contract_pnl_if_exited_only_on_btc_cross")
    stop_5 = _money_model(rows, "contract_pnl_if_stopped_at_5c")
    lines += [
        "5. Contract-price stop versus BTC stop:",
        f"   - Hold-to-settlement avg: `{_fmt(held['avg_cents'], 4)}c`, PF `{_fmt(held['profit_factor'], 4)}`",
        f"   - 5c contract stop avg: `{_fmt(stop_5['avg_cents'], 4)}c`, PF `{_fmt(stop_5['profit_factor'], 4)}`",
        f"   - BTC-cross stop avg: `{_fmt(btc_cross['avg_cents'], 4)}c`, PF `{_fmt(btc_cross['profit_factor'], 4)}`",
        "   - Use the grouped CSV to decide superiority. If many settlement winners suffer 5c-20c contract drawdowns without BTC crossback, contract-price stops are likely inferior.",
        "",
        "6. Best `$100 per trade` versions by modeled P/L:",
        "",
        "| entry_price_bucket | entry_distance_bucket | side | tte_bucket | trades | win_rate | avg_$100_pnl | total_$100_pnl | PF | max_losing_streak |",
        "|:---|:---|:---|:---|---:|---:|---:|---:|---:|---:|",
    ]

    for row in _top_summary(summary):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["entry_price_bucket"]),
                    str(row["entry_distance_bucket"]),
                    str(row["side"]),
                    str(row["tte_bucket"]),
                    str(row["deduped_qualifying_trades"]),
                    _fmt(row["win_rate_to_settlement_pct"], 1),
                    _fmt(row["avg_pnl_dollars_per_100_notional"], 4),
                    _fmt(row["total_modeled_pnl_dollars_100_per_trade"], 4),
                    _fmt(row["profit_factor_held_to_settlement"], 4),
                    str(row["max_losing_streak_held_to_settlement"]),
                ]
            )
            + " |"
        )

    lines += [
        "",
        "## Stop Comparison",
        "",
        "| stop | losers stopped | winners stopped | avg pnl cents | profit factor |",
        "|:---|---:|---:|---:|---:|",
    ]
    for row in distance_stop_rows:
        lines.append(
            f"| {row['stop']} | {row['losers_stopped']} | {row['winners_stopped']} | {_fmt(row['avg_cents'], 4)} | {_fmt(row['profit_factor'], 4)} |"
        )

    lines += [
        "",
        "## Contract Stop Models",
        "",
        "| model | avg pnl cents | profit factor |",
        "|:---|---:|---:|",
    ]
    for label, key in contract_stop_models:
        model = _money_model(rows, key)
        lines.append(f"| {label} | {_fmt(model['avg_cents'], 4)} | {_fmt(model['profit_factor'], 4)} |")

    lines += [
        "",
        "## Data Quality Warnings",
        "",
        "| issue | side | rows affected | latest seen |",
        "|:---|:---|---:|:---|",
    ]
    for row in quality:
        lines.append(
            f"| {row.get('issue')} | {row.get('side') or ''} | {row.get('rows_affected')} | {row.get('latest_seen')} |"
        )

    lines += [
        "",
        "## Output Files",
        "",
        f"- Candidate-level CSV: `{paths.candidates_csv}`",
        f"- Grouped summary CSV: `{paths.summary_csv}`",
        f"- Data-quality CSV: `{paths.data_quality_csv}`",
        "",
        "## Research Warning",
        "",
        "This is a historical diagnostic. Do not treat any group with a small sample, concentrated active days, missing settlement data, or duplicated candidate snapshots as live-trading proof.",
        "",
    ]
    return "\n".join(lines)


def _fetch_all(cur, sql: str) -> list[dict[str, Any]]:
    cur.execute(sql)
    return list(cur.fetchall())


def _output_paths(output_dir: Path) -> OutputPaths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"late_winning_contract_{date.today().isoformat()}"
    return OutputPaths(
        directory=output_dir,
        candidates_csv=output_dir / f"{stem}_candidates.csv",
        summary_csv=output_dir / f"{stem}_summary.csv",
        data_quality_csv=output_dir / f"{stem}_data_quality.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def build_report(output_dir: Path) -> OutputPaths:
    from app.db import get_pool

    paths = _output_paths(output_dir)
    conn = get_pool().get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(PARAM_SQL)
        for sql in DROP_SQL:
            cur.execute(sql)
        for sql in (
            CREATE_QUOTE_PIVOT_SQL,
            CREATE_RAW_CANDIDATES_SQL,
            CREATE_CANDIDATES_SQL,
            CREATE_FINAL_BTC_SQL,
            CREATE_FUTURE_QUOTES_SQL,
            CREATE_AGGREGATED_SQL,
            CREATE_SETTLEMENT_SQL,
            CREATE_OUTCOMES_SQL,
            CREATE_DATA_QUALITY_SQL,
        ):
            cur.execute(sql)

        rows = _fetch_all(cur, OUTCOME_SQL)
        quality = _fetch_all(cur, DATA_QUALITY_SQL)
        summary = _build_summary(rows)

        _write_csv(paths.candidates_csv, rows)
        _write_csv(paths.summary_csv, summary)
        _write_csv(paths.data_quality_csv, quality)
        paths.markdown_report.write_text(_render_markdown(rows, summary, quality, paths))
        conn.rollback()
    finally:
        conn.close()
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Late winning-contract research report")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="print relevant discovered schema mapping and exit",
    )
    args = parser.parse_args()

    if args.schema:
        for row in SCHEMA_DISCOVERY_ROWS:
            print(f"{row['section']}\t{row['table_name']}\t{row['relevant_columns']}")
        return

    paths = build_report(args.output_dir)
    print("Late winning-contract report complete")
    print(f"candidate_csv={paths.candidates_csv}")
    print(f"summary_csv={paths.summary_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
