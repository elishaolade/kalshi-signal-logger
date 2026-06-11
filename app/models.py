"""
models.py — Python dataclasses for schema v2 tables.

These are not ORM models. They are typed containers that mirror the
database schema. Use them to pass data through the ingestion pipeline
without relying on raw dicts.

All fields match the column names in schema_v2.sql exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Market:
    """
    Represents one row in the `markets` table.
    Kalshi markets are upserted on first discovery.
    """
    market_id:      str
    title:          Optional[str]       = None
    market_type:    Optional[str]       = None   # "binary" | "range"
    target_price:   Optional[float]     = None   # NULL for range markets
    opens_at:       Optional[datetime]  = None
    closes_at:      Optional[datetime]  = None
    settles_at:     Optional[datetime]  = None
    status:         Optional[str]       = None   # "open" | "closed" | "settled"
    raw_payload:    Optional[dict]      = None

    # Set by DB after upsert.
    id: Optional[int] = None


@dataclass
class Contract:
    """
    Represents one row in the `contracts` table.
    One YES and one NO contract are created per market on discovery.
    """
    market_id:      int
    contract_id:    str                 # "{market_id}_YES" or "{market_id}_NO"
    side:           str                 # "YES" | "NO"
    title:          Optional[str]       = None
    status:         Optional[str]       = None
    raw_payload:    Optional[dict]      = None

    # Set by DB after upsert.
    id: Optional[int] = None


@dataclass
class MarketSnapshot:
    """
    Represents one row in the `market_snapshots` table.

    Captured once per polling interval per market.
    Insert-only — never updated.
    """
    market_id:              int
    snapshot_sequence:      int
    captured_at:            datetime
    btc_price:              float
    time_remaining_seconds: Optional[int]   = None
    source:                 Optional[str]   = None   # "kraken" | "mock"
    raw_payload:            Optional[dict]  = None

    # Set by DB after insert.
    id: Optional[int] = None


@dataclass
class ContractSnapshot:
    """
    Represents one row in the `contract_snapshots` table.

    Captured once per polling interval per contract (YES + NO).
    Insert-only — never updated.
    """
    market_snapshot_id: int
    contract_id:        int
    captured_at:        datetime
    bid_price:          float
    ask_price:          float
    last_price:         Optional[float] = None
    spread:             Optional[float] = None
    volume:             Optional[int]   = None
    raw_payload:        Optional[dict]  = None

    # Set by DB after insert.
    id: Optional[int] = None


@dataclass
class MarketMetrics:
    """
    Represents one row in the `market_metrics` table.

    Derived values computed from market_snapshots + rolling BTC history.
    Not raw data — calculated values only.

    Definitions
    -----------
    distance_from_target      = btc_price - target_price
    distance_from_target_pct  = (btc_price - target_price) / target_price * 100
    btc_return_X              = (btc_now - btc_X_ago) / btc_X_ago * 100
    btc_return_stddev_X       = std dev of per-tick % returns over rolling window X
    """
    market_snapshot_id:         int
    captured_at:                datetime

    # Distance from strike
    distance_from_target:       Optional[float] = None
    distance_from_target_pct:   Optional[float] = None
    is_above_target:            Optional[bool]  = None

    # BTC returns (expressed as fractions, e.g. 0.00123 = 0.123%)
    btc_return_30s:             Optional[float] = None
    btc_return_1m:              Optional[float] = None
    btc_return_5m:              Optional[float] = None

    # Volatility: std dev of per-tick returns over rolling window
    btc_return_stddev_1m:       Optional[float] = None
    btc_return_stddev_3m:       Optional[float] = None
    btc_return_stddev_5m:       Optional[float] = None
    btc_return_stddev_15m:      Optional[float] = None
