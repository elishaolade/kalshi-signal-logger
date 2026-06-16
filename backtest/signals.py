"""
backtest/signals.py — Pure, lookahead-safe signal detection.

No database access.  No side-effects.  Fully deterministic given the same
inputs.

Side-adjusted movement definitions
-----------------------------------
For a YES contract:
    btc_move_toward_side  = btc_price_now - btc_price_lookback_ago
    (positive = BTC moved up = favorable for YES)

For a NO contract:
    btc_move_toward_side  = btc_price_lookback_ago - btc_price_now
    (positive = BTC moved down = favorable for NO)

This is equivalent to ``directional_gap`` from app/features.py applied to
the BTC move rather than the gap itself.

distance_change_toward_side:
    YES: (btc_now - target) - (btc_lookback - target) = btc_move
         Positive = the YES advantage (gap above target) grew.
    NO:  (target - btc_now) - (target - btc_lookback) = -(btc_move)
         Positive = the NO advantage (gap below target) grew.
    In both cases this equals btc_move_toward_side.

contract_move_during_lookback:
    ask_at_signal - ask_at_lookback_start  (always positive = ask rose)
    Computed for the evaluated side only.
    The signal condition checks this is <= maximum_contract_reprice_during_lookback.

Look-ahead safety guarantee
-----------------------------
``detect_signal`` is called with:
    row          = rows[i]          (current tick)
    lookback_row = rows[lo_ptr]     (tick at t - lookback_seconds, lo_ptr <= i)
All future ticks rows[i+1:] are NEVER passed to this function.
Exit simulation uses only rows[i+1:] (forward path), never rows[:i+1] again.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from backtest.config import ExperimentConfig
from backtest.loader import MarketInfo, MarketRow, get_ask, get_bid, get_spread

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """
    A qualifying candidate signal.

    All prices are dollar fractions.
    BTC values are USD.
    ``btc_move_toward_side`` and ``distance_change_toward_side`` are always
    positive when BTC moved favorably for ``side`` over the lookback window.
    """
    # Identity
    market_id:   int        # markets.id
    contract_id: int        # contracts.id for this side
    side:        str        # "YES" | "NO"

    # Signal timestamp
    signal_at:   datetime
    signal_ts:   float      # Unix epoch seconds

    # Entry prices (signal time)
    entry_ask:   float
    entry_bid:   float
    entry_spread: float

    # Context
    btc_price:          float
    target_price:       float
    time_remaining_s:   Optional[int]

    # Raw and side-adjusted distance at signal time
    distance_from_target:    Optional[float]   # raw: btc_price - target_price
    side_adjusted_distance:  float             # positive = favorable for side

    # Movement over lookback window
    btc_move_toward_side:            float   # positive = BTC moved toward winning side
    distance_change_toward_side:     float   # equals btc_move_toward_side (same quantity)
    contract_move_during_lookback:   float   # ask_now - ask_lookback (positive = repriced up)

    # Volatility at signal time (from market_metrics, may be None)
    volatility_1m: Optional[float]
    volatility_5m: Optional[float]

    # Snapshot index (for forward-path slicing — not persisted)
    snapshot_index: int


def _side_adjusted_distance(btc_price: float, target_price: float, side: str) -> float:
    """Positive = the side is in-the-money (or closer to it)."""
    raw = btc_price - target_price
    return raw if side == "YES" else -raw


def _btc_move_toward(btc_now: float, btc_lookback: float, side: str) -> float:
    """Positive = BTC moved in the favorable direction for ``side``."""
    raw_move = btc_now - btc_lookback    # positive = BTC rose
    return raw_move if side == "YES" else -raw_move


def detect_signal(
    row:          MarketRow,
    lookback_row: MarketRow,
    market:       MarketInfo,
    contract_id:  int,
    side:         str,
    config:       ExperimentConfig,
    snapshot_index: int,
) -> Optional[Signal]:
    """
    Evaluate whether a signal exists at ``row`` (time t) given ``lookback_row``
    (time t - lookback_seconds).

    Parameters
    ----------
    row           : current snapshot (time t)
    lookback_row  : snapshot at or just before (t - lookback_seconds)
    market        : MarketInfo for this market
    contract_id   : contracts.id for this side
    side          : "YES" | "NO"
    config        : ExperimentConfig
    snapshot_index: index of ``row`` in the market's row list (for slicing)

    Returns None if any entry condition is not met.
    The returned Signal is annotated with the reason it was accepted.
    """
    assert side in ("YES", "NO"), f"side must be YES or NO, got {side!r}"
    assert market.target_price is not None, "target_price must not be None"

    target = market.target_price

    # ── 1. Prices present ─────────────────────────────────────────────────────
    ask    = get_ask(row, side)
    bid    = get_bid(row, side)
    spread = get_spread(row, side)

    if ask is None or bid is None:
        return None   # unexecutable — no quote

    # Defensive: bid/ask inversion → skip this snapshot
    if bid > ask + 1e-6:
        return None

    # ── 2. Time remaining in configured range ─────────────────────────────────
    tte = row.time_remaining_s
    if tte is None:
        return None
    if tte < config.minimum_time_remaining_seconds:
        return None
    if tte > config.maximum_time_remaining_seconds:
        return None

    # ── 3. Entry price in configured range ────────────────────────────────────
    if ask < config.minimum_entry_ask or ask > config.maximum_entry_ask:
        return None

    # ── 4. Spread check ───────────────────────────────────────────────────────
    if spread is None or spread > config.maximum_spread:
        return None

    # ── 5. BTC move toward side ───────────────────────────────────────────────
    btc_move = _btc_move_toward(row.btc_price, lookback_row.btc_price, side)
    if btc_move < config.minimum_btc_move_toward_target:
        return None

    # ── 6. Contract reprice check (ask must NOT have risen too much) ──────────
    lookback_ask = get_ask(lookback_row, side)
    if lookback_ask is None:
        return None   # can't measure repricing — skip
    contract_move = ask - lookback_ask   # positive = ask rose during lookback

    if contract_move > config.maximum_contract_reprice_during_lookback:
        return None

    # ── All conditions met — build Signal ────────────────────────────────────
    raw_distance = row.btc_price - target
    side_adj_dist = _side_adjusted_distance(row.btc_price, target, side)

    return Signal(
        market_id    = market.id,
        contract_id  = contract_id,
        side         = side,
        signal_at    = row.captured_at,
        signal_ts    = row.ts,
        entry_ask    = ask,
        entry_bid    = bid,
        entry_spread = spread,
        btc_price    = row.btc_price,
        target_price = target,
        time_remaining_s = tte,
        distance_from_target   = raw_distance,
        side_adjusted_distance = side_adj_dist,
        btc_move_toward_side          = btc_move,
        distance_change_toward_side   = btc_move,   # same quantity (see module docstring)
        contract_move_during_lookback = contract_move,
        volatility_1m = row.btc_return_stddev_1m,
        volatility_5m = row.btc_return_stddev_5m,
        snapshot_index = snapshot_index,
    )


def iter_signals(
    market:  MarketInfo,
    rows:    list[MarketRow],
    config:  ExperimentConfig,
    split:   str = "all",    # "train" | "test" | "all"
) -> tuple[list[tuple[Signal, str]], dict[str, int]]:
    """
    Walk ``rows`` chronologically and yield all qualifying signals for both
    YES and NO sides.

    Implements:
    - Two-pointer lookback window (no fabricated prices across gaps)
    - Per-contract cooldown and overlap suppression
    - Per-side blocked-until tracking

    Parameters
    ----------
    market  : MarketInfo
    rows    : list[MarketRow] (from load_market_rows)
    config  : ExperimentConfig

    Returns
    -------
    (signals, counters)
        signals  : list of (Signal, split_label) where split_label = "train"|"test"
        counters : dict with n_checked, n_signals, n_skipped_cooldown,
                   n_skipped_overlap, n_skipped_no_lookback
    """
    counters = {
        "n_checked":             0,
        "n_signals":             0,
        "n_skipped_cooldown":    0,
        "n_skipped_overlap":     0,
        "n_skipped_no_lookback": 0,
    }
    signals: list[tuple[Signal, str]] = []

    if not rows or market.target_price is None:
        return signals, counters

    # Per-contract state: {contract_id: earliest ts when next signal allowed}
    cooldown_until: dict[int, float] = {}

    # Per-contract: {contract_id: True} when a simulated position is open.
    # We can't close positions here (we don't run exits) so we defer to the
    # executor — overlap suppression is handled there via the active_trades set.
    # Here we only apply cooldown.

    lo_ptr = 0

    for i, row in enumerate(rows):
        counters["n_checked"] += 1

        # Advance left edge of lookback window.
        while lo_ptr < i and rows[lo_ptr].ts < row.ts - config.lookback_seconds:
            lo_ptr += 1

        # lo_ptr now points to the earliest row within the lookback window.
        # The lookback_row is rows[lo_ptr] (oldest in window, still within seconds).
        lookback_row = rows[lo_ptr]

        # Require at least a few ticks in the window.
        window_len = i - lo_ptr + 1
        if window_len < 3:
            counters["n_skipped_no_lookback"] += 1
            continue

        # Require the lookback row to be at least 80% of lookback_seconds ago
        # (avoids trivial windows where lo_ptr == i).
        actual_lookback = row.ts - lookback_row.ts
        if actual_lookback < 0.8 * config.lookback_seconds:
            counters["n_skipped_no_lookback"] += 1
            continue

        for side in ("YES", "NO"):
            contract_id = market.contract_ids.get(side)
            if contract_id is None:
                continue

            # Cooldown check
            if row.ts < cooldown_until.get(contract_id, float("-inf")):
                counters["n_skipped_cooldown"] += 1
                continue

            sig = detect_signal(
                row          = row,
                lookback_row = lookback_row,
                market       = market,
                contract_id  = contract_id,
                side         = side,
                config       = config,
                snapshot_index = i,
            )

            if sig is not None:
                signals.append((sig, split))
                counters["n_signals"] += 1
                # Apply cooldown from this signal's timestamp.
                cooldown_until[contract_id] = row.ts + config.cooldown_seconds

    return signals, counters
