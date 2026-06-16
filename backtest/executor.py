"""
backtest/executor.py — Pure, lookahead-safe exit simulation.

SIMULATED FILL MODEL — important caveats
-----------------------------------------
    entry_price = ask at signal time
    exit_price  = first available bid at or after the exit timestamp

This model may OVERESTIMATE actual execution quality because:
- It assumes fills at quoted prices with no market impact.
- It does not model queue position or partial fills.
- Quoted bid/ask may not be fillable in full at the displayed size.

P&L accounting (dollar fractions, not percentages)
----------------------------------------------------
    gross_pnl = exit_bid - entry_ask
    net_pnl   = gross_pnl - estimated_fee_per_contract - estimated_slippage_cents

Slippage is deducted as a flat round-trip cost, not as a fill-price
adjustment.  This is a simplification; mark it explicitly in results.

Exit priority (first-fired wins)
----------------------------------
At each forward tick the checks run in this order:
    1. Loss-limit     (if config.loss_limit_cents is set)
    2. Profit-target  (if config.profit_target_cents is set, per profile)
    3. Momentum-reversal (if config.exit_on_momentum_reversal)
    4. Distance-invalidation (if config.exit_on_distance_invalidation)
    5. Fixed-time horizon (holding_period_seconds)
    6. End-of-market   (last valid bid before market closes)

For fixed-time and profit-target exits, one ``TradeResult`` row is produced
per (holding_period × profit_target) combination.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backtest.config import ExperimentConfig
from backtest.loader import MarketInfo, MarketRow, get_bid
from backtest.signals import Signal, _btc_move_toward

logger = logging.getLogger(__name__)

# Exit reason labels
EXIT_PROFIT_TARGET      = "profit_target"
EXIT_LOSS_LIMIT         = "loss_limit"
EXIT_FIXED_TIME         = "fixed_time"
EXIT_MOMENTUM_REVERSAL  = "momentum_reversal"
EXIT_DISTANCE_INVALID   = "distance_invalidation"
EXIT_END_OF_MARKET      = "end_of_market"
EXIT_UNEXECUTABLE       = "unexecutable"    # no forward bid exists

RESULT_WIN          = "win"
RESULT_LOSS         = "loss"
RESULT_BREAKEVEN    = "breakeven"
RESULT_SKIPPED      = "skipped"
RESULT_UNEXECUTABLE = "unexecutable"

# BTC momentum reversal: evaluate over a short window (ticks, not seconds)
# of forward observations.  When the signed BTC move over the last
# REVERSAL_WINDOW forward ticks flips negative for the traded side, exit.
REVERSAL_WINDOW_TICKS = 5


@dataclass
class TradeResult:
    """
    One simulated trade outcome.

    ``exit_profile`` encodes the (holding_period, profit_target) combination:
    e.g. "ht30s" (time-only), "ht30s_tp5c" (time + 5¢ target).
    """
    # Identity (copied from Signal)
    market_id:   int
    contract_id: int
    side:        str

    # Profile key
    exit_profile:     str
    holding_seconds_config: int         # configured holding period (seconds)
    profit_target_cfg: Optional[float]  # configured profit target (dollar frac)

    # Signal context
    signal_at:           datetime
    entry_ask:           float
    entry_bid:           float
    entry_spread:        float
    btc_price_at_entry:  float
    target_price:        float
    distance_from_target: Optional[float]
    side_adjusted_distance: float
    btc_move_toward_side:   float
    contract_move_during_lookback: float
    volatility_1m: Optional[float]
    volatility_5m: Optional[float]
    time_remaining_at_entry: Optional[int]

    # Exit outcome
    entry_at:     datetime
    exit_at:      Optional[datetime]
    exit_bid:     Optional[float]
    exit_reason:  str
    holding_seconds: Optional[float]

    # Excursion (computed over the holding window)
    max_favorable_excursion: Optional[float]   # max(bid - entry_ask) during hold
    max_adverse_excursion:   Optional[float]   # min(bid - entry_ask) during hold

    # P&L (dollar fractions; gross = raw bid-ask, net = after fees/slippage)
    gross_pnl_cents:         Optional[float]
    estimated_fees_cents:    float
    estimated_slippage_cents: float
    net_pnl_cents:           Optional[float]

    # Classification
    result_status: str

    # Extra metadata (e.g. exit tick index)
    metadata: dict = field(default_factory=dict)


def _profile_label(holding_s: int, profit_target: Optional[float]) -> str:
    label = f"ht{holding_s}s"
    if profit_target is not None:
        label += f"_tp{round(profit_target * 100):.0f}c"
    return label


def _classify_pnl(net_pnl: Optional[float]) -> str:
    if net_pnl is None:
        return RESULT_UNEXECUTABLE
    if net_pnl > 1e-5:
        return RESULT_WIN
    if net_pnl < -1e-5:
        return RESULT_LOSS
    return RESULT_BREAKEVEN


def simulate_exits(
    signal:       Signal,
    forward_rows: list[MarketRow],   # rows AFTER signal index (strictly forward)
    market:       MarketInfo,
    config:       ExperimentConfig,
) -> list[TradeResult]:
    """
    Simulate all (holding_period × profit_target) exit combinations for one
    signal.

    Parameters
    ----------
    signal       : qualifying signal (from signals.detect_signal)
    forward_rows : market rows with ts > signal.signal_ts
                   (caller slices rows[signal.snapshot_index + 1:])
    market       : MarketInfo (for end-of-market detection)
    config       : ExperimentConfig

    Returns
    -------
    List of TradeResult — one per (holding_period × profit_target) combination.
    Empty list if forward_rows is empty (no data after signal).
    """
    if not forward_rows:
        # No forward data — mark all profiles as unexecutable.
        return _all_unexecutable(signal, config)

    side = signal.side
    entry_ask = signal.entry_ask

    # Build the Cartesian product of (holding_period, profit_target).
    profit_targets: list[Optional[float]] = (
        list(config.profit_target_cents) if config.profit_target_cents else [None]
    )
    combos: list[tuple[int, Optional[float]]] = [
        (ht, pt)
        for ht in config.holding_period_seconds
        for pt in profit_targets
    ]

    # Per-profile state: track whether this profile has already exited.
    profile_done: dict[str, bool] = {
        _profile_label(ht, pt): False for ht, pt in combos
    }
    profile_exit: dict[str, dict] = {}   # label → exit info

    # Rolling MFE/MAE trackers (shared across profiles, reset per profile
    # since holding windows differ).
    peak_bid:   dict[str, float] = {_profile_label(ht, pt): float("-inf") for ht, pt in combos}
    trough_bid: dict[str, float] = {_profile_label(ht, pt): float("inf")  for ht, pt in combos}

    # For momentum-reversal exit: track recent BTC prices (forward path).
    recent_btc: list[float] = []

    # Walk forward.
    for fwd in forward_rows:
        bid = get_bid(fwd, side)

        # Momentum reversal check (computed once per tick, shared across profiles).
        momentum_reversed = False
        if config.exit_on_momentum_reversal and len(recent_btc) >= REVERSAL_WINDOW_TICKS:
            window_btc = recent_btc[-REVERSAL_WINDOW_TICKS:]
            fwd_move = _btc_move_toward(fwd.btc_price, window_btc[0], side)
            momentum_reversed = fwd_move < 0

        # Distance invalidation check.
        distance_invalid = False
        if config.exit_on_distance_invalidation is not None and fwd.distance_from_target is not None:
            # Compute how much the side-adjusted distance has deteriorated.
            fwd_side_adj = (
                fwd.btc_price - signal.target_price if side == "YES"
                else signal.target_price - fwd.btc_price
            )
            entry_side_adj = signal.side_adjusted_distance
            deterioration = entry_side_adj - fwd_side_adj   # positive = gotten worse
            distance_invalid = deterioration > config.exit_on_distance_invalidation

        for ht, pt in combos:
            label = _profile_label(ht, pt)
            if profile_done[label]:
                continue

            horizon_ts = signal.signal_ts + ht

            # Update excursion trackers only when within the holding window.
            if fwd.ts <= horizon_ts and bid is not None:
                peak_bid[label]   = max(peak_bid[label],   bid)
                trough_bid[label] = min(trough_bid[label], bid)

            # ── Exit rule evaluation (first-fired wins) ────────────────────────

            exit_reason: Optional[str] = None
            exit_bid:    Optional[float] = bid

            # 1. Loss-limit
            if (config.loss_limit_cents is not None
                    and bid is not None
                    and bid <= entry_ask - config.loss_limit_cents):
                exit_reason = EXIT_LOSS_LIMIT

            # 2. Profit-target
            elif (pt is not None
                    and bid is not None
                    and bid >= entry_ask + pt):
                exit_reason = EXIT_PROFIT_TARGET

            # 3. Momentum reversal
            elif config.exit_on_momentum_reversal and momentum_reversed:
                exit_reason = EXIT_MOMENTUM_REVERSAL

            # 4. Distance invalidation
            elif config.exit_on_distance_invalidation is not None and distance_invalid:
                exit_reason = EXIT_DISTANCE_INVALID

            # 5. Fixed-time horizon: use first available bid at or after horizon.
            elif fwd.ts >= horizon_ts:
                if bid is not None:
                    exit_reason = EXIT_FIXED_TIME
                else:
                    # No bid at the horizon; keep walking (up to 10s grace).
                    if fwd.ts > horizon_ts + 10:
                        exit_reason = EXIT_UNEXECUTABLE
                        exit_bid = None

            if exit_reason is not None:
                profile_exit[label] = {
                    "exit_at":     fwd.captured_at,
                    "exit_ts":     fwd.ts,
                    "exit_bid":    exit_bid,
                    "exit_reason": exit_reason,
                    "tick_index":  None,  # not tracked
                }
                profile_done[label] = True

        recent_btc.append(fwd.btc_price)

        # Early-out if all profiles have exited.
        if all(profile_done.values()):
            break

    # End-of-market: any profiles still open at this point use the last valid bid.
    last_valid_bid: Optional[float] = None
    last_valid_at:  Optional[datetime] = None
    last_valid_ts:  float = signal.signal_ts

    for fwd in reversed(forward_rows):
        b = get_bid(fwd, side)
        if b is not None:
            last_valid_bid = b
            last_valid_at  = fwd.captured_at
            last_valid_ts  = fwd.ts
            break

    for ht, pt in combos:
        label = _profile_label(ht, pt)
        if not profile_done[label]:
            if last_valid_bid is not None:
                profile_exit[label] = {
                    "exit_at":     last_valid_at,
                    "exit_ts":     last_valid_ts,
                    "exit_bid":    last_valid_bid,
                    "exit_reason": EXIT_END_OF_MARKET,
                }
            else:
                profile_exit[label] = {
                    "exit_at":     None,
                    "exit_ts":     None,
                    "exit_bid":    None,
                    "exit_reason": EXIT_UNEXECUTABLE,
                }

    # ── Build TradeResult list ─────────────────────────────────────────────────
    total_cost = config.estimated_fee_per_contract + config.estimated_slippage_cents
    results: list[TradeResult] = []

    for ht, pt in combos:
        label = _profile_label(ht, pt)
        ex    = profile_exit.get(label, {})

        exit_bid    = ex.get("exit_bid")
        exit_reason = ex.get("exit_reason", EXIT_UNEXECUTABLE)
        exit_at     = ex.get("exit_at")
        exit_ts     = ex.get("exit_ts")

        # Gross P&L
        gross: Optional[float] = None
        net:   Optional[float] = None
        if exit_bid is not None:
            gross = round(exit_bid - entry_ask, 6)
            net   = round(gross - total_cost, 6)

        # Excursion (in dollars relative to entry ask)
        pk = peak_bid[label]
        tr = trough_bid[label]
        mfe = round(pk - entry_ask, 6)   if pk   > float("-inf") else None
        mae = round(tr - entry_ask, 6)   if tr   < float("inf")  else None

        holding_s: Optional[float] = None
        if exit_ts is not None:
            holding_s = round(exit_ts - signal.signal_ts, 1)

        results.append(TradeResult(
            market_id    = signal.market_id,
            contract_id  = signal.contract_id,
            side         = signal.side,
            exit_profile = label,
            holding_seconds_config = ht,
            profit_target_cfg      = pt,
            signal_at    = signal.signal_at,
            entry_ask    = entry_ask,
            entry_bid    = signal.entry_bid,
            entry_spread = signal.entry_spread,
            btc_price_at_entry           = signal.btc_price,
            target_price                 = signal.target_price,
            distance_from_target         = signal.distance_from_target,
            side_adjusted_distance       = signal.side_adjusted_distance,
            btc_move_toward_side         = signal.btc_move_toward_side,
            contract_move_during_lookback= signal.contract_move_during_lookback,
            volatility_1m              = signal.volatility_1m,
            volatility_5m              = signal.volatility_5m,
            time_remaining_at_entry    = signal.time_remaining_s,
            entry_at    = signal.signal_at,   # entry = signal time (ask-based)
            exit_at     = exit_at,
            exit_bid    = exit_bid,
            exit_reason = exit_reason,
            holding_seconds = holding_s,
            max_favorable_excursion = mfe,
            max_adverse_excursion   = mae,
            gross_pnl_cents          = gross,
            estimated_fees_cents     = config.estimated_fee_per_contract,
            estimated_slippage_cents = config.estimated_slippage_cents,
            net_pnl_cents            = net,
            result_status            = _classify_pnl(net),
            metadata = {
                "exit_reason": exit_reason,
                "holding_seconds_config": ht,
                "profit_target_cfg": pt,
            },
        ))

    return results


def _all_unexecutable(signal: Signal, config: ExperimentConfig) -> list[TradeResult]:
    """Return unexecutable TradeResult for all profiles when forward_rows is empty."""
    profit_targets: list[Optional[float]] = (
        list(config.profit_target_cents) if config.profit_target_cents else [None]
    )
    results = []
    total_cost = config.estimated_fee_per_contract + config.estimated_slippage_cents
    for ht in config.holding_period_seconds:
        for pt in profit_targets:
            label = _profile_label(ht, pt)
            results.append(TradeResult(
                market_id   = signal.market_id,
                contract_id = signal.contract_id,
                side        = signal.side,
                exit_profile           = label,
                holding_seconds_config = ht,
                profit_target_cfg      = pt,
                signal_at    = signal.signal_at,
                entry_ask    = signal.entry_ask,
                entry_bid    = signal.entry_bid,
                entry_spread = signal.entry_spread,
                btc_price_at_entry           = signal.btc_price,
                target_price                 = signal.target_price,
                distance_from_target         = signal.distance_from_target,
                side_adjusted_distance       = signal.side_adjusted_distance,
                btc_move_toward_side         = signal.btc_move_toward_side,
                contract_move_during_lookback= signal.contract_move_during_lookback,
                volatility_1m              = signal.volatility_1m,
                volatility_5m              = signal.volatility_5m,
                time_remaining_at_entry    = signal.time_remaining_s,
                entry_at    = signal.signal_at,
                exit_at     = None,
                exit_bid    = None,
                exit_reason = EXIT_UNEXECUTABLE,
                holding_seconds          = None,
                max_favorable_excursion  = None,
                max_adverse_excursion    = None,
                gross_pnl_cents          = None,
                estimated_fees_cents     = config.estimated_fee_per_contract,
                estimated_slippage_cents = config.estimated_slippage_cents,
                net_pnl_cents            = None,
                result_status            = RESULT_UNEXECUTABLE,
            ))
    return results
