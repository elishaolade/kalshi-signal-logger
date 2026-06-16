"""
backtest/loader.py — v2-schema data loader for the momentum backtest.

Reads from:
    markets            (one row per market)
    contracts          (YES + NO rows per market)
    market_snapshots   (btc_price + timing per poll cycle)
    contract_snapshots (bid/ask per side per poll cycle)
    market_metrics     (derived distance / return metrics)

Never writes to any table.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from app.db import fetch_all, fetch_one

logger = logging.getLogger(__name__)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class MarketInfo:
    """One row from the ``markets`` table."""
    id:           int
    market_id:    str                    # e.g. KXBTC-260612-0500-T63500
    target_price: Optional[float]        # contract strike (floor_strike)
    opens_at:     Optional[datetime]
    closes_at:    Optional[datetime]
    settles_at:   Optional[datetime]
    status:       Optional[str]
    # contracts.id keyed by side
    contract_ids: dict[str, int]         # {"YES": <id>, "NO": <id>}


@dataclass
class MarketRow:
    """
    One aligned snapshot row: BTC price + YES/NO quotes + derived metrics.

    All prices are dollar fractions [0.01, 0.99].
    BTC prices / distances are in USD.
    ``ts`` is Unix epoch seconds (float) derived from ``captured_at``.
    """
    snapshot_id:        int
    snapshot_seq:       int
    captured_at:        datetime
    ts:                 float           # UNIX_TIMESTAMP(captured_at)
    btc_price:          float
    time_remaining_s:   Optional[int]

    # YES side
    yes_bid:    Optional[float]
    yes_ask:    Optional[float]
    yes_spread: Optional[float]

    # NO side
    no_bid:    Optional[float]
    no_ask:    Optional[float]
    no_spread: Optional[float]

    # Derived metrics (may be NULL if market_metrics has no row for this snapshot)
    distance_from_target:     Optional[float]
    distance_from_target_pct: Optional[float]
    is_above_target:          Optional[bool]
    btc_return_30s:           Optional[float]
    btc_return_1m:            Optional[float]
    btc_return_5m:            Optional[float]
    btc_return_stddev_1m:     Optional[float]
    btc_return_stddev_5m:     Optional[float]


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _i(v: Any) -> Optional[int]:
    return None if v is None else int(v)


def _b(v: Any) -> Optional[bool]:
    if v is None:
        return None
    return bool(int(v))


# ── Public loaders ────────────────────────────────────────────────────────────

def load_all_markets(
    start: Optional[str] = None,
    end:   Optional[str] = None,
    limit: Optional[int] = None,
) -> list[MarketInfo]:
    """
    Load all binary markets from the v2 ``markets`` table, ordered by
    opens_at ascending (chronological for train/test split).

    Parameters
    ----------
    start : "YYYY-MM-DD" optional — only markets with opens_at >= this date.
    end   : "YYYY-MM-DD" optional — only markets with opens_at <  this date.
    limit : cap the number of markets returned (for smoke-test runs).
    """
    conditions = ["m.market_type = 'binary'", "m.target_price IS NOT NULL"]
    params: list[Any] = []

    if start:
        conditions.append("m.opens_at >= %s")
        params.append(start)
    if end:
        conditions.append("m.opens_at < %s")
        params.append(end)

    where = "WHERE " + " AND ".join(conditions)
    sql = f"""
        SELECT m.id, m.market_id, m.target_price,
               m.opens_at, m.closes_at, m.settles_at, m.status
        FROM markets m
        {where}
        ORDER BY m.opens_at ASC, m.market_id ASC
    """
    if limit:
        sql += f"\nLIMIT {int(limit)}"

    rows = fetch_all(sql, tuple(params))

    markets: list[MarketInfo] = []
    for r in rows:
        contract_ids = _load_contract_ids(int(r["id"]))
        if "YES" not in contract_ids or "NO" not in contract_ids:
            logger.debug(
                "Skipping market %s — missing YES or NO contract row",
                r["market_id"],
            )
            continue
        markets.append(MarketInfo(
            id           = int(r["id"]),
            market_id    = str(r["market_id"]),
            target_price = _f(r["target_price"]),
            opens_at     = r["opens_at"],
            closes_at    = r["closes_at"],
            settles_at   = r["settles_at"],
            status       = r["status"],
            contract_ids = contract_ids,
        ))

    logger.debug("load_all_markets → %d markets", len(markets))
    return markets


def _load_contract_ids(market_db_id: int) -> dict[str, int]:
    """Return {side: contracts.id} for a given markets.id."""
    rows = fetch_all(
        "SELECT side, id FROM contracts WHERE market_id = %s AND side IN ('YES', 'NO')",
        (market_db_id,),
    )
    return {r["side"]: int(r["id"]) for r in rows}


# ── Snapshot loader ───────────────────────────────────────────────────────────

_SNAPSHOT_SQL = """
    SELECT
        ms.id                           AS snapshot_id,
        ms.snapshot_sequence            AS snapshot_seq,
        ms.captured_at,
        UNIX_TIMESTAMP(ms.captured_at)  AS ts,
        ms.btc_price,
        ms.time_remaining_seconds,

        MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
        MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
        MAX(CASE WHEN c.side = 'YES' THEN cs.spread    END) AS yes_spread,
        MAX(CASE WHEN c.side = 'NO'  THEN cs.bid_price END) AS no_bid,
        MAX(CASE WHEN c.side = 'NO'  THEN cs.ask_price END) AS no_ask,
        MAX(CASE WHEN c.side = 'NO'  THEN cs.spread    END) AS no_spread,

        mm.distance_from_target,
        mm.distance_from_target_pct,
        mm.is_above_target,
        mm.btc_return_30s,
        mm.btc_return_1m,
        mm.btc_return_5m,
        mm.btc_return_stddev_1m,
        mm.btc_return_stddev_5m

    FROM market_snapshots ms

    -- Contracts pivot: join contracts to know which contract_id is YES vs NO,
    -- then join contract_snapshots for the bid/ask at this snapshot.
    JOIN contracts c
        ON  c.market_id = ms.market_id
        AND c.side IN ('YES', 'NO')
    JOIN contract_snapshots cs
        ON  cs.market_snapshot_id = ms.id
        AND cs.contract_id        = c.id

    -- Metrics are optional (may not exist for every snapshot).
    LEFT JOIN market_metrics mm
        ON mm.market_snapshot_id = ms.id

    WHERE ms.market_id = %s

    GROUP BY
        ms.id, ms.snapshot_sequence, ms.captured_at, ms.btc_price,
        ms.time_remaining_seconds,
        mm.distance_from_target, mm.distance_from_target_pct, mm.is_above_target,
        mm.btc_return_30s, mm.btc_return_1m, mm.btc_return_5m,
        mm.btc_return_stddev_1m, mm.btc_return_stddev_5m

    ORDER BY ms.captured_at ASC
"""


def load_market_rows(market: MarketInfo) -> list[MarketRow]:
    """
    Load all aligned snapshot rows for one market, ordered chronologically.

    Returns an empty list if there are no snapshots or no contract_snapshots
    for this market (e.g. data collection started after market opened).
    """
    raw = fetch_all(_SNAPSHOT_SQL, (market.id,))
    if not raw:
        return []

    out: list[MarketRow] = []
    for r in raw:
        out.append(MarketRow(
            snapshot_id   = int(r["snapshot_id"]),
            snapshot_seq  = int(r["snapshot_seq"]),
            captured_at   = r["captured_at"],
            ts            = float(r["ts"]),
            btc_price     = float(r["btc_price"]),
            time_remaining_s = _i(r["time_remaining_seconds"]),
            yes_bid    = _f(r["yes_bid"]),
            yes_ask    = _f(r["yes_ask"]),
            yes_spread = _f(r["yes_spread"]),
            no_bid     = _f(r["no_bid"]),
            no_ask     = _f(r["no_ask"]),
            no_spread  = _f(r["no_spread"]),
            distance_from_target     = _f(r["distance_from_target"]),
            distance_from_target_pct = _f(r["distance_from_target_pct"]),
            is_above_target          = _b(r["is_above_target"]),
            btc_return_30s           = _f(r["btc_return_30s"]),
            btc_return_1m            = _f(r["btc_return_1m"]),
            btc_return_5m            = _f(r["btc_return_5m"]),
            btc_return_stddev_1m     = _f(r["btc_return_stddev_1m"]),
            btc_return_stddev_5m     = _f(r["btc_return_stddev_5m"]),
        ))

    return out


# ── Side accessors (helpers shared across modules) ────────────────────────────

def get_bid(row: MarketRow, side: str) -> Optional[float]:
    return row.yes_bid if side == "YES" else row.no_bid

def get_ask(row: MarketRow, side: str) -> Optional[float]:
    return row.yes_ask if side == "YES" else row.no_ask

def get_spread(row: MarketRow, side: str) -> Optional[float]:
    return row.yes_spread if side == "YES" else row.no_spread
