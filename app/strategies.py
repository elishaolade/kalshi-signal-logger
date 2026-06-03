"""
strategies.py — Signal evaluation rules.

Each strategy function:
  - Receives raw market state (BTC ticks, contract quotes, timing)
  - Computes features via app.features
  - Returns a Signal dataclass when all entry conditions are met, else None
  - Has no side effects and creates no trades or DB records

Adding a new strategy:
  1. Define the function with the standard signature (see cheap_reversal_scalp).
  2. Append it to _STRATEGIES.  run_all() will pick it up automatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from app.features import (
    Tick,
    btc_velocity,
    build_time_features,
    directional_gap as _directional_gap,
    gap as _gap,
    gap_z_score as _gap_z_score,
    momentum_score,
    reversal_score,
    rolling_std as _rolling_std,
    rolling_stds,
    series_change as _series_change,
    series_min as _series_min,
    volatility_regime as _volatility_regime,
    whipsaw_score as _whipsaw_score,
    z_from_target as _z_from_target,
)
from app.reversal_probability import setup_type as _setup_type

logger = logging.getLogger(__name__)


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class Signal:
    """
    All fields needed to persist one row to the `signals` table.
    Matches the schema column names exactly so callers can do
    `asdict(signal)` and pass it straight to insert_and_get_id.
    """
    # identity
    market_ticker:          str
    rule_name:              str
    rule_version:           str
    side:                   str             # "YES" | "NO"

    # contract state at signal time
    contract_price:         float           # mid_price
    bid_price:              float
    ask_price:              float
    spread:                 float

    # BTC context
    btc_price:              float
    target_price:           float
    gap:                    float
    directional_gap:        float
    gap_z_score:            Optional[float]

    # timing
    contract_age_seconds:   float
    time_remaining_seconds: float

    # computed features
    momentum_score:         float
    reversal_score:         float
    btc_velocity:           Optional[float]
    volatility_30s:         Optional[float]
    volatility_60s:         Optional[float]
    volatility_120s:        Optional[float]

    # optional enrichment (filled by downstream analysis, not strategy logic)
    edge:                   Optional[float] = None
    confidence_score:       Optional[float] = None

    reason:                 str = ""
    signal_status:          str = "paper"
    recorded_at:            datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Strategy-specific extras NOT persisted to the `signals` table.
    # Used to hand richer context (e.g. early_overextension_reversal_scalp metrics)
    # to the ObservationTracker without widening the signals schema.
    extra:                  Optional[dict] = None

    # ── time-of-day features (attached by run_all after strategy fires) ────────
    # All values expressed in the configured SIGNAL_TIMEZONE (default ET).
    # Stored as None until run_all() calls build_time_features().
    entry_date:          Optional[str]      = None   # "YYYY-MM-DD"
    entry_time_local:    Optional[str]      = None   # "HH:MM:SS"
    entry_hour:          Optional[int]      = None   # 0-23
    entry_minute:        Optional[int]      = None   # 0-59
    entry_day_of_week:   Optional[int]      = None   # 0=Mon … 6=Sun
    entry_day_name:      Optional[str]      = None   # "Monday" … "Sunday"
    entry_is_weekend:    Optional[bool]     = None
    entry_15m_block:     Optional[str]      = None   # "HH:MM" e.g. "14:30"
    entry_30m_block:     Optional[str]      = None   # "HH:MM" e.g. "14:30"
    entry_hour_block:    Optional[str]      = None   # "HH:00" e.g. "14:00"
    market_open_time:    Optional[datetime] = None
    market_close_time:   Optional[datetime] = None
    timezone_used:       str                = "America/New_York"


# ── cheap_reversal_scalp ──────────────────────────────────────────────────────

_RULE_NAME    = "cheap_reversal_scalp"
_RULE_VERSION = "v1"

# Entry thresholds — named constants so they show up clearly in test assertions
_MAX_CONTRACT_AGE_S = 300       # only fire early in the window
_MIN_ASK            = 0.05      # cheap contract floor  ($0.05 per $1 contract)
_MAX_ASK            = 0.20      # cheap contract ceiling ($0.20 per $1 contract)
_MAX_SPREAD         = 0.03      # 3-cent maximum bid/ask spread
_MIN_REVERSAL       = 3.0       # minimum reversal_score to qualify
_MOMENTUM_N         = 10        # lookback tick count for momentum / reversal


def _recent_window(ticks: list[Tick], window_seconds: float) -> list[Tick]:
    """
    Return ticks whose ts is within `window_seconds` of the most recent tick.
    Used to scope "fresh low" checks to a short trailing window.
    """
    if not ticks:
        return []
    cutoff = ticks[-1].ts - window_seconds
    return [t for t in ticks if t.ts >= cutoff]


def _liquidity_ok(_side_quotes: dict) -> bool:
    # Placeholder: always passes.
    # TODO: gate on minimum volume and open_interest once those fields
    #       are populated from live or replayed contract_ticks.
    return True


def cheap_reversal_scalp(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Hypothesis
    ----------
    Low-priced contracts early in a 15-minute BTC up/down window can produce
    temporary reversal bounces large enough to scalp before expiry.

    Entry conditions
    ----------------
    1. contract_age_seconds <= 300  (early in window; more time left to run)
    2. Ask price in [0.05, 0.20]    (cheap enough to offer asymmetric payoff)
    3. Spread <= 0.03               (tight enough to enter and exit cleanly)
    4. reversal_score >= 3          (clear directional momentum in last 10 ticks)
    5. Liquidity check passes       (placeholder — always true for now)
    6. Side = YES when BTC ticking upward  (reversal_score for YES >= threshold)
       Side = NO  when BTC ticking downward (reversal_score for NO  >= threshold)

    Returns None when any condition is not met.
    """
    # ── 1. Timing gate ────────────────────────────────────────────────────────
    if contract_age_seconds > _MAX_CONTRACT_AGE_S:
        logger.debug(
            "%s: skip — age %.0fs > %ds",
            _RULE_NAME, contract_age_seconds, _MAX_CONTRACT_AGE_S,
        )
        return None

    # ── 2. Feature computation ────────────────────────────────────────────────
    mom    = momentum_score(ticks, n=_MOMENTUM_N)
    r_yes  = reversal_score(ticks, "YES", n=_MOMENTUM_N)
    r_no   = reversal_score(ticks, "NO",  n=_MOMENTUM_N)
    vel    = btc_velocity(ticks, window_seconds=30.0)
    stds   = rolling_stds(ticks)

    # ── 3. Side selection via reversal score ──────────────────────────────────
    # r_yes and r_no are mirror images; only one can be >= threshold.
    if r_yes >= _MIN_REVERSAL:
        side = "YES"
        rev  = r_yes
    elif r_no >= _MIN_REVERSAL:
        side = "NO"
        rev  = r_no
    else:
        logger.debug(
            "%s: skip — reversal YES=%.0f NO=%.0f both below %.0f",
            _RULE_NAME, r_yes, r_no, _MIN_REVERSAL,
        )
        return None

    # ── 4. Contract price and spread checks ───────────────────────────────────
    quotes = contract_prices.get(side, {})
    ask    = quotes.get("ask_price", 0.0)
    bid    = quotes.get("bid_price", 0.0)
    mid    = quotes.get("mid_price", 0.0)
    spread = quotes.get("spread",    0.0)

    if not (_MIN_ASK <= ask <= _MAX_ASK):
        logger.debug(
            "%s: skip — %s ask=%.4f outside [%.2f, %.2f]",
            _RULE_NAME, side, ask, _MIN_ASK, _MAX_ASK,
        )
        return None

    if spread > _MAX_SPREAD:
        logger.debug(
            "%s: skip — spread=%.4f > %.2f", _RULE_NAME, spread, _MAX_SPREAD,
        )
        return None

    # ── 5. Liquidity placeholder ──────────────────────────────────────────────
    if not _liquidity_ok(quotes):
        logger.debug("%s: skip — liquidity check failed", _RULE_NAME)
        return None

    # ── 6. Compute remaining signal fields ────────────────────────────────────
    g   = _gap(btc_price, target_price)
    dg  = _directional_gap(btc_price, target_price, side)
    gz  = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)

    vel_s = f"{vel:+.2f}$/s" if vel is not None else "n/a"
    gz_s  = f"{gz:+.4f}"    if gz  is not None else "n/a"
    reason = (
        f"{_RULE_NAME} {_RULE_VERSION}: {side} @ ask={ask:.4f} | "
        f"reversal={rev:+.0f} mom={mom:+.0f} vel={vel_s} gz={gz_s} | "
        f"btc={btc_price:,.2f} target={target_price:,.2f} gap={g:+.2f} "
        f"age={contract_age_seconds:.0f}s remaining={time_remaining_seconds:.0f}s"
    )

    logger.info("Signal — %s", reason)

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _RULE_NAME,
        rule_version           = _RULE_VERSION,
        side                   = side,

        contract_price         = mid,
        bid_price              = bid,
        ask_price              = ask,
        spread                 = spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
    )


# ── premium_momentum_continuation ────────────────────────────────────────────

_PMC_RULE_NAME    = "premium_momentum_continuation"
_PMC_RULE_VERSION = "v1"

_PMC_MIN_TIME_REMAINING = 180   # seconds — only fire in the last 5 minutes …
_PMC_MAX_TIME_REMAINING = 300   # … but not closer than 3 minutes to expiry
_PMC_MIN_LEADING_ASK    = 0.76  # leading side ask must be at least $0.76
_PMC_NO_MAX_ENTRY_PRICE = 0.90  # NO entries priced ≥ $0.90 are skipped (watch-only research gate)
_PMC_MIN_GAP_Z          = 1.0   # directional gap must be ≥ 1 std away from target
_PMC_MIN_MOMENTUM_YES   = 3.0   # YES entry: raw momentum_score must be ≥ +3
_PMC_MAX_MOMENTUM_NO    = -3.0  # NO  entry: raw momentum_score must be ≤ −3
_PMC_MAX_SPREAD         = 0.03  # same 3-cent ceiling as Strategy A; tune in config
_PMC_MOMENTUM_N         = 10    # lookback tick count shared with Strategy A


def premium_momentum_continuation(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Hypothesis
    ----------
    In the final 3–5 minutes of a 15-minute BTC up/down window, if BTC has
    built sustained momentum and the leading contract is already priced at
    $0.76+, that momentum may carry through to expiry — or produce a short
    scalp window if the market briefly over-prices it.

    Entry conditions
    ----------------
    1. 180 ≤ time_remaining_seconds ≤ 300  (last 3–5 min; not the final 3 min)
    2. BTC on correct side of target
         YES: btc_price > target_price
         NO:  btc_price < target_price
    3. Leading-side ask price ≥ 0.76       (contract already priced as likely winner)
    4. Directional gap_z_score ≥ 1.0       (BTC ≥ 1 std above/below target)
    5. Momentum confirms side
         YES: momentum_score ≥  +3
         NO:  momentum_score ≤  −3
    6. Spread ≤ 0.03 (_PMC_MAX_SPREAD)

    Returns None when any condition is not met.
    """
    # ── 1. Time-window gate ───────────────────────────────────────────────────
    if not (_PMC_MIN_TIME_REMAINING <= time_remaining_seconds <= _PMC_MAX_TIME_REMAINING):
        logger.debug(
            "%s: skip — remaining=%.0fs outside [%d, %d]",
            _PMC_RULE_NAME, time_remaining_seconds,
            _PMC_MIN_TIME_REMAINING, _PMC_MAX_TIME_REMAINING,
        )
        return None

    # ── 2. Determine side from BTC position relative to target ────────────────
    if btc_price > target_price:
        side = "YES"
    elif btc_price < target_price:
        side = "NO"
    else:
        # Exactly at target — no directional edge.
        logger.debug("%s: skip — btc_price == target_price", _PMC_RULE_NAME)
        return None

    # ── 3. Feature computation ────────────────────────────────────────────────
    mom  = momentum_score(ticks, n=_PMC_MOMENTUM_N)
    rev  = reversal_score(ticks, side, n=_PMC_MOMENTUM_N)
    vel  = btc_velocity(ticks, window_seconds=30.0)
    stds = rolling_stds(ticks)
    g    = _gap(btc_price, target_price)
    dg   = _directional_gap(btc_price, target_price, side)
    gz   = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)

    # ── 4. Leading-side contract price and spread ─────────────────────────────
    quotes = contract_prices.get(side, {})
    ask    = quotes.get("ask_price", 0.0)
    bid    = quotes.get("bid_price", 0.0)
    mid    = quotes.get("mid_price", 0.0)
    spread = quotes.get("spread",    0.0)

    if ask < _PMC_MIN_LEADING_ASK:
        logger.debug(
            "%s: skip — %s ask=%.4f < %.2f",
            _PMC_RULE_NAME, side, ask, _PMC_MIN_LEADING_ASK,
        )
        return None

    # ── 4b. NO-side rich-price rejection gate ─────────────────────────────────
    # Skip NO entries priced at or above 0.90 (near-certain winners: poor
    # risk/reward).  Entry price = the leading-side ask we would pay.
    if side == "NO" and ask >= _PMC_NO_MAX_ENTRY_PRICE:
        logger.info("SKIP: NO entry price %.4f >= 0.90 threshold", ask)
        return None

    if spread > _PMC_MAX_SPREAD:
        logger.debug(
            "%s: skip — spread=%.4f > %.2f", _PMC_RULE_NAME, spread, _PMC_MAX_SPREAD,
        )
        return None

    # ── 5. Directional gap z-score ────────────────────────────────────────────
    # gap_z_score = (btc − target) / std: positive when YES is winning,
    # negative when NO is winning.  We flip the sign for NO so the same
    # threshold (_PMC_MIN_GAP_Z ≥ 1.0) applies to both sides.
    gz_directional = gz if side == "YES" else (-gz if gz is not None else None)
    if gz_directional is None or gz_directional < _PMC_MIN_GAP_Z:
        logger.debug(
            "%s: skip — directional gz=%.4f < %.1f",
            _PMC_RULE_NAME,
            gz_directional if gz_directional is not None else float("nan"),
            _PMC_MIN_GAP_Z,
        )
        return None

    # ── 6. Momentum confirmation ──────────────────────────────────────────────
    if side == "YES" and mom < _PMC_MIN_MOMENTUM_YES:
        logger.debug(
            "%s: skip — YES momentum=%.0f < %.0f",
            _PMC_RULE_NAME, mom, _PMC_MIN_MOMENTUM_YES,
        )
        return None
    if side == "NO" and mom > _PMC_MAX_MOMENTUM_NO:
        logger.debug(
            "%s: skip — NO momentum=%.0f > %.0f",
            _PMC_RULE_NAME, mom, _PMC_MAX_MOMENTUM_NO,
        )
        return None

    # ── Build signal ──────────────────────────────────────────────────────────
    vel_s  = f"{vel:+.2f}$/s" if vel is not None else "n/a"
    gz_s   = f"{gz:+.4f}"    if gz  is not None else "n/a"
    gz_d_s = f"{gz_directional:+.4f}"
    reason = (
        f"{_PMC_RULE_NAME} {_PMC_RULE_VERSION}: {side} @ ask={ask:.4f} | "
        f"mom={mom:+.0f} gz_dir={gz_d_s} gz_raw={gz_s} vel={vel_s} | "
        f"btc={btc_price:,.2f} target={target_price:,.2f} gap={g:+.2f} "
        f"remaining={time_remaining_seconds:.0f}s"
    )

    logger.info("Signal — %s", reason)

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _PMC_RULE_NAME,
        rule_version           = _PMC_RULE_VERSION,
        side                   = side,

        contract_price         = mid,
        bid_price              = bid,
        ask_price              = ask,
        spread                 = spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
    )


# ── Registry / dispatcher ─────────────────────────────────────────────────────

_PMS_V2_RULE_NAME    = "premium_momentum_scalp"
_PMS_V2_RULE_VERSION = "v2"

_PMS_V2_MIN_CONTRACT_PRICE = 0.65
_PMS_V2_MAX_CONTRACT_PRICE = 0.80
_PMS_V2_MIN_TIME_REMAINING = 240
_PMS_V2_MAX_TIME_REMAINING = 300
_PMS_V2_MAX_SPREAD         = 0.03
_PMS_V2_MIN_MOMENTUM_YES   = 3.0
_PMS_V2_MAX_MOMENTUM_NO    = -3.0
_PMS_V2_MIN_DIRECTIONAL_GZ = -1.0
_PMS_V2_MOMENTUM_N         = 10


def premium_momentum_scalp_v2(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Hypothesis
    ----------
    The first promising refinement is a tighter premium momentum scalp:
    enter only in the 4–5 minute-to-expiry window, only when the selected
    contract is priced in the 65c–80c band, and only when momentum confirms
    the side without BTC being extremely stretched against it.
    """
    if not (_PMS_V2_MIN_TIME_REMAINING <= time_remaining_seconds <= _PMS_V2_MAX_TIME_REMAINING):
        logger.debug(
            "%s: skip — remaining=%.0fs outside [%d, %d]",
            _PMS_V2_RULE_NAME, time_remaining_seconds,
            _PMS_V2_MIN_TIME_REMAINING, _PMS_V2_MAX_TIME_REMAINING,
        )
        return None

    mom  = momentum_score(ticks, n=_PMS_V2_MOMENTUM_N)
    vel  = btc_velocity(ticks, window_seconds=30.0)
    stds = rolling_stds(ticks)
    g    = _gap(btc_price, target_price)
    gz   = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)

    candidates: list[tuple[str, float, dict]] = []
    for side in ("YES", "NO"):
        quotes = contract_prices.get(side, {})
        mid    = quotes.get("mid_price", 0.0)
        spread = quotes.get("spread", 0.0)

        if not (_PMS_V2_MIN_CONTRACT_PRICE <= mid <= _PMS_V2_MAX_CONTRACT_PRICE):
            continue
        if spread > _PMS_V2_MAX_SPREAD:
            continue
        if side == "YES" and mom < _PMS_V2_MIN_MOMENTUM_YES:
            continue
        if side == "NO" and mom > _PMS_V2_MAX_MOMENTUM_NO:
            continue

        directional_gz = gz if side == "YES" else (-gz if gz is not None else None)
        if directional_gz is None or directional_gz < _PMS_V2_MIN_DIRECTIONAL_GZ:
            continue

        candidates.append((side, directional_gz, quotes))

    if not candidates:
        logger.debug(
            "%s: skip — no side passed price/spread/momentum/gap filters",
            _PMS_V2_RULE_NAME,
        )
        return None

    side, directional_gz, quotes = max(candidates, key=lambda item: item[1])
    bid    = quotes.get("bid_price", 0.0)
    ask    = quotes.get("ask_price", 0.0)
    mid    = quotes.get("mid_price", 0.0)
    spread = quotes.get("spread", 0.0)
    rev    = reversal_score(ticks, side, n=_PMS_V2_MOMENTUM_N)
    dg     = _directional_gap(btc_price, target_price, side)

    vel_s  = f"{vel:+.2f}$/s" if vel is not None else "n/a"
    gz_s   = f"{gz:+.4f}"    if gz  is not None else "n/a"
    reason = (
        f"{_PMS_V2_RULE_NAME} {_PMS_V2_RULE_VERSION}: {side} @ mid={mid:.4f} "
        f"ask={ask:.4f} | mom={mom:+.0f} gz_dir={directional_gz:+.4f} "
        f"gz_raw={gz_s} vel={vel_s} | btc={btc_price:,.2f} "
        f"target={target_price:,.2f} gap={g:+.2f} "
        f"remaining={time_remaining_seconds:.0f}s"
    )

    logger.info("Signal — %s", reason)

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _PMS_V2_RULE_NAME,
        rule_version           = _PMS_V2_RULE_VERSION,
        side                   = side,

        contract_price         = mid,
        bid_price              = bid,
        ask_price              = ask,
        spread                 = spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
    )


# ── premium_no_midrange_scalp ─────────────────────────────────────────────────

_PNMS_RULE_NAME    = "premium_no_midrange_scalp"
_PNMS_RULE_VERSION = "v1"

_PNMS_MIN_TIME_REMAINING = 180   # seconds
_PNMS_MAX_TIME_REMAINING = 300   # seconds
_PNMS_MIN_NO_ASK         = 0.65  # NO ask floor
_PNMS_MAX_NO_ASK         = 0.80  # NO ask ceiling
_PNMS_MAX_SPREAD         = 0.03
_PNMS_MIN_DIR_MOMENTUM   = 3.0   # directional (NO = -raw_momentum_score)
_PNMS_MIN_DIR_GAP_Z      = 2.0   # directional (NO = -raw_gap_z_score)
_PNMS_MOMENTUM_N         = 10


def premium_no_midrange_scalp(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Hypothesis
    ----------
    NO contracts priced 0.65–0.80 in the final 3–5 minutes of a 15-minute
    BTC window may produce reliable short scalps when BTC momentum and the
    gap z-score both confirm the NO direction (BTC trending below target).
    Rationale: the cross-bucket analysis showed the 0.65–0.80 band has PF 1.80
    overall, but it may be driven by one side; this isolates the NO side and
    tightens the directional filters.

    Entry conditions
    ----------------
    1. 180 ≤ time_remaining_seconds ≤ 300
    2. Side = NO only
    3. NO ask price in [0.65, 0.80]
    4. Spread ≤ 0.03
    5. Directional momentum ≥ 3   (NO → −raw_momentum_score ≥ 3,
                                   i.e. raw_momentum_score ≤ −3)
    6. Directional gap z-score ≥ 2 (NO → −raw_gap_z_score ≥ 2,
                                    i.e. raw_gap_z_score ≤ −2)

    Side-normalised values
    ----------------------
    directional_momentum_score:  YES = raw_mom       NO = −raw_mom
    directional_gap_z_score:     YES = raw_gz        NO = −raw_gz
    """
    side = "NO"

    # ── 1. Time-window gate ───────────────────────────────────────────────────
    if not (_PNMS_MIN_TIME_REMAINING <= time_remaining_seconds <= _PNMS_MAX_TIME_REMAINING):
        logger.debug(
            "%s: skip — remaining=%.0fs outside [%d, %d]",
            _PNMS_RULE_NAME, time_remaining_seconds,
            _PNMS_MIN_TIME_REMAINING, _PNMS_MAX_TIME_REMAINING,
        )
        return None

    # ── 2. Feature computation ────────────────────────────────────────────────
    mom  = momentum_score(ticks, n=_PNMS_MOMENTUM_N)
    rev  = reversal_score(ticks, side, n=_PNMS_MOMENTUM_N)
    vel  = btc_velocity(ticks, window_seconds=30.0)
    stds = rolling_stds(ticks)
    g    = _gap(btc_price, target_price)
    dg   = _directional_gap(btc_price, target_price, side)
    gz   = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)

    # ── 3. Directional momentum (NO side: flip sign) ──────────────────────────
    dir_mom = -mom
    if dir_mom < _PNMS_MIN_DIR_MOMENTUM:
        logger.debug(
            "%s: skip — directional_mom=%.2f < %.1f  (raw_mom=%.2f)",
            _PNMS_RULE_NAME, dir_mom, _PNMS_MIN_DIR_MOMENTUM, mom,
        )
        return None

    # ── 4. Directional gap z-score (NO side: flip sign) ──────────────────────
    dir_gz = (-gz) if gz is not None else None
    if dir_gz is None or dir_gz < _PNMS_MIN_DIR_GAP_Z:
        logger.debug(
            "%s: skip — directional_gz=%s < %.1f",
            _PNMS_RULE_NAME,
            f"{dir_gz:.4f}" if dir_gz is not None else "N/A",
            _PNMS_MIN_DIR_GAP_Z,
        )
        return None

    # ── 5. NO contract price and spread ──────────────────────────────────────
    quotes = contract_prices.get(side, {})
    ask    = quotes.get("ask_price", 0.0)
    bid    = quotes.get("bid_price", 0.0)
    mid    = quotes.get("mid_price", 0.0)
    spread = quotes.get("spread",    0.0)

    if not (_PNMS_MIN_NO_ASK <= ask <= _PNMS_MAX_NO_ASK):
        logger.debug(
            "%s: skip — NO ask=%.4f outside [%.2f, %.2f]",
            _PNMS_RULE_NAME, ask, _PNMS_MIN_NO_ASK, _PNMS_MAX_NO_ASK,
        )
        return None

    if spread > _PNMS_MAX_SPREAD:
        logger.debug(
            "%s: skip — spread=%.4f > %.2f", _PNMS_RULE_NAME, spread, _PNMS_MAX_SPREAD,
        )
        return None

    # ── 6. Build signal ───────────────────────────────────────────────────────
    vel_s   = f"{vel:+.2f}$/s"   if vel  is not None else "n/a"
    gz_s    = f"{gz:+.4f}"       if gz   is not None else "n/a"
    dgz_s   = f"{dir_gz:+.4f}"  if dir_gz is not None else "n/a"
    reason  = (
        f"{_PNMS_RULE_NAME} {_PNMS_RULE_VERSION}: {side} @ ask={ask:.4f} | "
        f"dir_mom={dir_mom:+.0f} dir_gz={dgz_s} gz_raw={gz_s} vel={vel_s} | "
        f"btc={btc_price:,.2f} target={target_price:,.2f} gap={g:+.2f} "
        f"remaining={time_remaining_seconds:.0f}s"
    )
    logger.info("Signal — %s", reason)

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _PNMS_RULE_NAME,
        rule_version           = _PNMS_RULE_VERSION,
        side                   = side,

        contract_price         = mid,
        bid_price              = bid,
        ask_price              = ask,
        spread                 = spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
        signal_status          = "paper_active",
    )


# ── premium_no_midrange_scalp_v2 (NO 0.65–0.80, trailing + hour filter) ────────
#
# v2 is a CLEAN follow-up test of the only paper branch with positive
# expectancy: NO contracts priced 0.65–0.80 in the last 3–5 minutes.  It runs
# in PARALLEL with v1 (both paper_active) so v1 vs v2 can be compared directly.
# Differences from v1: (a) gate on contract_price (mid) in the 0.65–0.80 band to
# match the winning bucket exactly; (b) drop the gap_z gate (momentum-only
# confirmation, per spec); (c) skip weekday 9/10 AM ET (applied in run_all);
# (d) trailing exit that arms only after +4c (configured in paper_trader).
# PAPER-ONLY: no live trading, no order execution.
_PNMS_V2_RULE_NAME    = "premium_no_midrange_scalp_v2"
_PNMS_V2_RULE_VERSION = "v2"

_PNMS_V2_MIN_TIME_REMAINING = 180   # seconds
_PNMS_V2_MAX_TIME_REMAINING = 300   # seconds
_PNMS_V2_MIN_PRICE          = 0.65  # contract_price (mid) floor
_PNMS_V2_MAX_PRICE          = 0.80  # contract_price (mid) ceiling
_PNMS_V2_MAX_SPREAD         = 0.03
_PNMS_V2_MIN_DIR_MOMENTUM   = 3.0   # NO confirmation: −raw_momentum_score ≥ 3
_PNMS_V2_MOMENTUM_N         = 10
_PNMS_V2_SKIP_HOURS: frozenset[int] = frozenset({9, 10})  # weekday ET hours blocked


def premium_no_midrange_scalp_v2(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Premium NO mid-range scalp, v2 (PAPER-ONLY — no live trading).

    Focuses on the single paper branch with positive expectancy in the live
    sample: NO side, contract_price 0.65–0.80, last 3–5 minutes.  Kept as a
    separate rule_name/version so v1 and v2 run in parallel for comparison.

    Entry conditions
    ----------------
    1. Side = NO only (YES never entered)
    2. 180 ≤ time_remaining_seconds ≤ 300
    3. Momentum confirms NO: −raw_momentum_score ≥ 3
    4. contract_price (mid) in [0.65, 0.80]   (avoid <0.65 or >0.80)
    5. Spread ≤ 0.03
    6. (run_all) skip weekday 9/10 AM ET → marked watch_only, not traded

    Exit conditions live in app.paper_trader (_exit_premium_no_midrange_scalp_v2):
    take-profit +5c, stop −4c, trailing arms only after +4c, timeout/near-expiry.
    """
    side = "NO"

    # ── 1. Time-window gate ───────────────────────────────────────────────────
    if not (_PNMS_V2_MIN_TIME_REMAINING <= time_remaining_seconds <= _PNMS_V2_MAX_TIME_REMAINING):
        logger.debug(
            "%s: skip — remaining=%.0fs outside [%d, %d]",
            _PNMS_V2_RULE_NAME, time_remaining_seconds,
            _PNMS_V2_MIN_TIME_REMAINING, _PNMS_V2_MAX_TIME_REMAINING,
        )
        return None

    # ── 2. Feature computation ────────────────────────────────────────────────
    mom  = momentum_score(ticks, n=_PNMS_V2_MOMENTUM_N)
    rev  = reversal_score(ticks, side, n=_PNMS_V2_MOMENTUM_N)
    vel  = btc_velocity(ticks, window_seconds=30.0)
    stds = rolling_stds(ticks)
    g    = _gap(btc_price, target_price)
    dg   = _directional_gap(btc_price, target_price, side)
    gz   = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)

    # ── 3. Momentum confirmation (NO side: flip sign) ─────────────────────────
    dir_mom = -mom
    if dir_mom < _PNMS_V2_MIN_DIR_MOMENTUM:
        logger.debug(
            "%s: skip — directional_mom=%.2f < %.1f  (raw_mom=%.2f)",
            _PNMS_V2_RULE_NAME, dir_mom, _PNMS_V2_MIN_DIR_MOMENTUM, mom,
        )
        return None

    # ── 4. NO contract price (mid) and spread ─────────────────────────────────
    quotes = contract_prices.get(side, {})
    ask    = quotes.get("ask_price", 0.0)
    bid    = quotes.get("bid_price", 0.0)
    mid    = quotes.get("mid_price", 0.0)
    spread = quotes.get("spread",    0.0)

    if not (_PNMS_V2_MIN_PRICE <= mid <= _PNMS_V2_MAX_PRICE):
        logger.debug(
            "%s: skip — NO contract_price=%.4f outside [%.2f, %.2f]",
            _PNMS_V2_RULE_NAME, mid, _PNMS_V2_MIN_PRICE, _PNMS_V2_MAX_PRICE,
        )
        return None

    if spread > _PNMS_V2_MAX_SPREAD:
        logger.debug(
            "%s: skip — spread=%.4f > %.2f", _PNMS_V2_RULE_NAME, spread, _PNMS_V2_MAX_SPREAD,
        )
        return None

    # ── 5. Build signal ───────────────────────────────────────────────────────
    vel_s  = f"{vel:+.2f}$/s" if vel is not None else "n/a"
    gz_s   = f"{gz:+.4f}"     if gz  is not None else "n/a"
    reason = (
        f"{_PNMS_V2_RULE_NAME} {_PNMS_V2_RULE_VERSION}: {side} @ mid={mid:.4f} "
        f"ask={ask:.4f} | dir_mom={dir_mom:+.0f} gz_raw={gz_s} vel={vel_s} | "
        f"btc={btc_price:,.2f} target={target_price:,.2f} gap={g:+.2f} "
        f"remaining={time_remaining_seconds:.0f}s"
    )
    logger.info("Signal — %s", reason)

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _PNMS_V2_RULE_NAME,
        rule_version           = _PNMS_V2_RULE_VERSION,
        side                   = side,

        contract_price         = mid,
        bid_price              = bid,
        ask_price              = ask,
        spread                 = spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
        signal_status          = "paper_active",
    )


# ── early_overextension_reversal_scalp ────────────────────────────────────────

_EOR_RULE_NAME    = "early_overextension_reversal_scalp"
_EOR_RULE_VERSION = "v1"

_EOR_MAX_MARKET_AGE_S      = 120     # only the first 2 minutes of the window
_EOR_MIN_LOSING_ASK        = 0.05    # losing-side ask floor
_EOR_MAX_LOSING_ASK        = 0.25    # losing-side ask ceiling
_EOR_MIN_WIN_CHANGE_60S    = 0.15    # winning side must have jumped ≥ +0.15 in 60s
_EOR_MIN_WIN_DIR_Z         = 2.5     # winning-side directional z-score ≥ 2.5
_EOR_MAX_LOSING_SPREAD     = 0.03    # losing-side spread ≤ 0.03
_EOR_MIN_BOUNCE            = 0.01    # losing side ≥ +0.01 off its low since open …
_EOR_WIN_CHANGE_WINDOW_S   = 60.0
_EOR_CONTRACT_MOM_WINDOW_S = 10.0    # … OR losing-side 10s momentum > 0
_EOR_Z_WINDOW_S            = 60.0


def early_overextension_reversal_scalp(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Hypothesis (WATCH-ONLY RESEARCH — never paper-traded)
    -----------------------------------------------------
    In the first two minutes of a BTC 15-minute up/down market, if the
    winning-side contract rapidly gains value while BTC moves several standard
    deviations away from the target, the losing-side contract may become
    temporarily underpriced and produce a 3¢–5¢ reversal bounce.

    Sides
    -----
    winning side : YES if btc_price > target_price, NO if btc_price < target_price
    losing  side : the opposite.  The SIGNAL is recorded on the *losing* side —
                   that is the contract whose hypothetical reversal we track.

    Entry observation conditions
    ----------------------------
    1. contract_age_seconds <= 120
    2. losing-side ask in [0.05, 0.25]
    3. winning-side contract price change over last 60s >= +0.15
    4. winning-side directional z-score >= 2.5
    5. losing-side spread <= 0.03
    6. losing side has bounced >= +0.01 off its low since market open
       OR losing-side contract momentum over last 10s > 0

    Outcome tracking and exit simulation are performed live by the
    ObservationTracker (no paper trade is opened).
    """
    # ── 1. Market-age gate (only the first 2 minutes) ─────────────────────────
    if contract_age_seconds > _EOR_MAX_MARKET_AGE_S:
        logger.debug(
            "%s: skip — age %.0fs > %ds",
            _EOR_RULE_NAME, contract_age_seconds, _EOR_MAX_MARKET_AGE_S,
        )
        return None

    # ── 2. Determine winning / losing side ────────────────────────────────────
    if btc_price > target_price:
        winning, losing = "YES", "NO"
    elif btc_price < target_price:
        winning, losing = "NO", "YES"
    else:
        logger.debug("%s: skip — btc_price == target_price", _EOR_RULE_NAME)
        return None

    # ── 3. Losing-side quotes: ask band + spread ──────────────────────────────
    lq       = contract_prices.get(losing, {})
    l_ask    = lq.get("ask_price", 0.0)
    l_bid    = lq.get("bid_price", 0.0)
    l_mid    = lq.get("mid_price", 0.0)
    l_spread = lq.get("spread",    0.0)

    if not (_EOR_MIN_LOSING_ASK <= l_ask <= _EOR_MAX_LOSING_ASK):
        logger.debug(
            "%s: skip — losing(%s) ask=%.4f outside [%.2f, %.2f]",
            _EOR_RULE_NAME, losing, l_ask, _EOR_MIN_LOSING_ASK, _EOR_MAX_LOSING_ASK,
        )
        return None

    if l_spread > _EOR_MAX_LOSING_SPREAD:
        logger.debug(
            "%s: skip — losing spread=%.4f > %.2f",
            _EOR_RULE_NAME, l_spread, _EOR_MAX_LOSING_SPREAD,
        )
        return None

    # ── 4. Winning-side rapid gain over last 60s ──────────────────────────────
    history  = contract_history or {}
    win_hist = history.get(winning, [])
    win_change = _series_change(win_hist, _EOR_WIN_CHANGE_WINDOW_S)
    if win_change is None or win_change < _EOR_MIN_WIN_CHANGE_60S:
        logger.debug(
            "%s: skip — winning(%s) 60s change=%s < %.2f",
            _EOR_RULE_NAME, winning,
            f"{win_change:+.4f}" if win_change is not None else "N/A",
            _EOR_MIN_WIN_CHANGE_60S,
        )
        return None

    # ── 5. Winning-side directional z-score (overextension) ───────────────────
    std60     = _rolling_std(ticks, _EOR_Z_WINDOW_S)
    win_dir_z = _z_from_target(btc_price, target_price, winning, std60)
    if win_dir_z is None or win_dir_z < _EOR_MIN_WIN_DIR_Z:
        logger.debug(
            "%s: skip — winning(%s) dir_z=%s < %.1f",
            _EOR_RULE_NAME, winning,
            f"{win_dir_z:+.4f}" if win_dir_z is not None else "N/A",
            _EOR_MIN_WIN_DIR_Z,
        )
        return None

    # ── 6. Losing-side bounce-from-low OR short-term momentum ─────────────────
    los_hist  = history.get(losing, [])
    los_low   = _series_min(los_hist)
    bounce    = (l_mid - los_low) if los_low is not None else None
    los_mom10 = _series_change(los_hist, _EOR_CONTRACT_MOM_WINDOW_S)

    bounced     = bounce is not None and bounce >= _EOR_MIN_BOUNCE
    momentum_up = los_mom10 is not None and los_mom10 > 0.0
    if not (bounced or momentum_up):
        logger.debug(
            "%s: skip — losing(%s) no bounce (%.4f) and 10s mom (%s) not > 0",
            _EOR_RULE_NAME, losing,
            bounce if bounce is not None else float("nan"),
            f"{los_mom10:+.4f}" if los_mom10 is not None else "N/A",
        )
        return None

    # ── Build (watch-only) signal on the LOSING side ──────────────────────────
    mom  = momentum_score(ticks, n=10)
    rev  = reversal_score(ticks, losing, n=10)
    vel  = btc_velocity(ticks, window_seconds=30.0)
    stds = rolling_stds(ticks)
    g    = _gap(btc_price, target_price)
    dg   = _directional_gap(btc_price, target_price, losing)
    gz   = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)

    bounce_s = f"{bounce:+.4f}"   if bounce   is not None else "n/a"
    mom10_s  = f"{los_mom10:+.4f}" if los_mom10 is not None else "n/a"
    reason = (
        f"{_EOR_RULE_NAME} {_EOR_RULE_VERSION}: losing={losing} @ ask={l_ask:.4f} "
        f"(winning={winning}) | win_chg60={win_change:+.4f} win_dir_z={win_dir_z:+.2f} "
        f"| losing bounce={bounce_s} mom10={mom10_s} spread={l_spread:.4f} | "
        f"btc={btc_price:,.2f} target={target_price:,.2f} gap={g:+.2f} "
        f"age={contract_age_seconds:.0f}s remaining={time_remaining_seconds:.0f}s"
    )
    logger.info("Signal — %s", reason)

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _EOR_RULE_NAME,
        rule_version           = _EOR_RULE_VERSION,
        side                   = losing,        # track the losing-side contract

        contract_price         = l_mid,
        bid_price              = l_bid,
        ask_price              = l_ask,
        spread                 = l_spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
        signal_status          = "watch_only",
        # EOR-specific context for the ObservationTracker (not persisted to signals)
        extra = {
            "winning_side":          winning,
            "losing_side":           losing,
            "winning_change_60s":    round(win_change, 6),
            "winning_dir_z":         round(win_dir_z, 6),
            "losing_bounce_from_low": round(bounce, 6) if bounce is not None else None,
            "losing_mom_10s":        round(los_mom10, 6) if los_mom10 is not None else None,
            "losing_ask_at_signal":  round(l_ask, 6),
            "losing_mid_at_signal":  round(l_mid, 6),
            "market_age_seconds":    round(contract_age_seconds, 3),
        },
    )


# ── cheap_losing_contract_reversal_trail ──────────────────────────────────────

_CLC_RULE_NAME    = "cheap_losing_contract_reversal_trail"
_CLC_RULE_VERSION = "v1"

_CLC_STRICT_MAX_AGE_S  = 120.0   # strict early test (first 2 minutes)
_CLC_COMPARE_MAX_AGE_S = 180.0   # also observe 2–3 min as a comparison bucket
_CLC_NEARMISS_MAX_AGE_S = 360.0  # only count "age" near-misses up to here (else ignore)
_CLC_MIN_ASK           = 0.05    # losing-side ask floor
_CLC_MAX_ASK           = 0.30    # losing-side ask ceiling
_CLC_PREFERRED_MIN_ASK = 0.10    # "preferred" price bucket flag (0.10–0.30)
# No hard adverse-z ceiling while watch-only: cheap losing contracts are usually
# cheap *because* BTC is already meaningfully against them.  Record the z bucket
# and let the observations tell us where reversals actually happen.
_CLC_MAX_SPREAD        = 0.03    # losing-side spread ceiling
_CLC_MIN_REVERSAL_PROB = 0.55    # +3c/-2c reversal-prob gate (recorded, see note)
_CLC_MAX_ADVERSE_MOM   = 2.0     # adverse directional momentum tolerance
_CLC_MIN_BOUNCE        = 0.01    # losing side ≥ +0.01 off its low since open …
_CLC_NEW_LOW_WINDOW_S  = 20.0    # … OR no fresh low in the last 20s
_CLC_Z_WINDOW_S        = 60.0
_CLC_MOM_N             = 10


# ── cheap_losing_contract_late_reversal (late-window variant) ──────────────────
#
# Separate watch-only research strategy (NOT a replacement for the early v1).
# Hypothesis: a cheap-but-not-dead losing contract reverses better in the last
# few minutes, after repricing has mostly happened and the losing side shows
# actual bounce/stall confirmation.  Raises the ask floor to 0.10 (never study
# near-dead 5-cent contracts) and requires positive short-term confirmation so
# we do not sit in a contract that is still collapsing.
_CLC_LATE_RULE_NAME      = "cheap_losing_contract_late_reversal"
_CLC_LATE_RULE_VERSION   = "v1"
_CLC_LATE_PHASE          = "late_1_to_5min"

_CLC_LATE_MIN_TR_S       = 60.0    # entry only when 60s <= time_remaining <= 300s
_CLC_LATE_MAX_TR_S       = 300.0
# Only count "time_window" near-misses just outside the entry window; markets
# far from the last-5-minutes window are not plausible candidates and ignored.
_CLC_LATE_NEARMISS_TR_LO = 30.0
_CLC_LATE_NEARMISS_TR_HI = 360.0

_CLC_LATE_MIN_ASK        = 0.10    # ask floor RAISED to 0.10 (no near-dead 5c)
_CLC_LATE_MAX_ASK        = 0.30    # ask ceiling
_CLC_LATE_MAX_SPREAD     = 0.03    # losing-side spread ceiling
# No hard adverse-z cap (record + bucket it in reports); z must only be measurable.
_CLC_LATE_MIN_BOUNCE     = 0.02    # losing side >= +0.02 off its low since open
_CLC_LATE_MIN_MOM10      = 0.00    # losing-contract 10s change >= 0.00 (stall/up)
_CLC_LATE_MOM_WINDOW_S   = 10.0
_CLC_LATE_Z_WINDOW_S     = 60.0
_CLC_LATE_MOM_N          = 10


class _CLCSkipStats:
    """
    Funnel counter for cheap_losing_contract_reversal_trail (watch-only
    diagnostics).  Each candidate evaluation is attributed to the FIRST gate it
    fails (or ``fired`` when a signal is emitted), so the tallies form a strict
    rejection funnel.  ``age`` only counts *near*-misses (market just past the
    180s window, up to _CLC_NEARMISS_MAX_AGE_S); markets far past the window are
    not plausible candidates and are ignored entirely.

    Purely in-memory; flushed to the log periodically by main.py.  Touches no
    DB and opens no trades.
    """

    _GATES = ("age", "no_side", "ask_band", "spread",
              "adverse_z", "violent_vol", "momentum", "fired")

    def __init__(
        self,
        gates: Optional[tuple[str, ...]] = None,
        label: str = "CLC near-miss funnel",
    ) -> None:
        # Per-instance gate list so the early and late variants can keep
        # different funnels (e.g. "age" vs "time_window"/"confirmation").
        self._gates: tuple[str, ...] = tuple(gates) if gates else self._GATES
        self._label = label
        self._counts: dict[str, int] = {g: 0 for g in self._gates}

    def record(self, gate: str) -> None:
        self._counts[gate] = self._counts.get(gate, 0) + 1

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)

    def total(self) -> int:
        return sum(self._counts.values())

    def reset(self) -> None:
        for g in self._counts:
            self._counts[g] = 0

    def format_summary(self) -> str:
        parts = "  ".join(f"{g}={self._counts[g]}" for g in self._gates)
        return f"{self._label} (since last flush):  {parts}"


# Module-level singletons, instrumented inside the strategies and flushed by
# main.py.  The early and late CLC variants keep separate funnels.
clc_skip_stats = _CLCSkipStats(label="CLC near-miss funnel (early)")

# Late-window CLC variant funnel.  Gates mirror the order they are checked in
# cheap_losing_contract_late_reversal so the tallies form a strict rejection
# funnel.  "time_window" only counts *near*-misses (time_remaining close to the
# 60–300s window); markets far outside are not plausible candidates.
clc_late_skip_stats = _CLCSkipStats(
    gates=("time_window", "no_side", "ask_band", "spread",
           "adverse_z_missing", "violent_vol", "confirmation", "fired"),
    label="CLC near-miss funnel (late)",
)


def cheap_losing_contract_reversal_trail(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Hypothesis (WATCH-ONLY RESEARCH — never paper-traded, no live orders)
    --------------------------------------------------------------------
    Early in a BTC 15-minute up/down market, if the *losing* contract is cheap,
    BTC displacement is measured, adverse momentum is tolerable, volatility is
    not violent, and historically similar setups reversed often, the losing
    contract may produce a short reversal move worth riding with a trailing
    exit.  Outcomes for five exit profiles are simulated live by the
    CLCReversalTracker (no paper trade is opened).

    Sides
    -----
    winning side : YES if btc > target, NO if btc < target
    losing  side : the opposite — the cheap contract we BUY and track.

    adverse_z_score : how far BTC sits *against* the losing side, in σ.
                      = z_from_target(btc, target, winning_side, std60)  (≥ 0).

    Structural entry gates (all hard):
      1. market_age_seconds <= 180   (strict early flag set when <= 120)
      2. losing-side ask in [0.05, 0.30]
      3. adverse_z_score is measurable  (recorded/bucketed, not hard-capped)
      4. losing-side spread <= 0.03
      5. volatility_regime != "violent"
      6. adverse momentum tolerable:
            adverse_directional_momentum_score <= 2
         OR losing side made no fresh low in the last 20s
         OR losing side bounced >= +0.01 off its low since open

    Historical reversal probability
    --------------------------------
    Computed from this strategy's own completed observations via the injected
    `reversal_prob_fn` (self-referential).  The spec's 0.55 gate is RECORDED
    (meets_reversal_prob_gate) but is intentionally NOT a hard blocker during
    the watch-only phase: we must observe low-probability setups too, both to
    bootstrap the table from cold and to answer "does reversal probability
    improve expectancy?".  The 0.55 gate is enforced only at promotion time.
    """
    # ── 1. Market-age gate (first 3 minutes; strict flag for first 2) ─────────
    if contract_age_seconds > _CLC_COMPARE_MAX_AGE_S:
        # Only count as a near-miss if the market just missed the window; markets
        # far past 180s are not plausible candidates, so they are ignored.
        if contract_age_seconds <= _CLC_NEARMISS_MAX_AGE_S:
            clc_skip_stats.record("age")
        return None
    strict_early = contract_age_seconds <= _CLC_STRICT_MAX_AGE_S
    market_phase = "first_2min" if strict_early else "min_2_to_3"

    # ── 2. Winning / losing side ──────────────────────────────────────────────
    if btc_price > target_price:
        winning, losing = "YES", "NO"
    elif btc_price < target_price:
        winning, losing = "NO", "YES"
    else:
        clc_skip_stats.record("no_side")
        return None

    # ── 3. Losing-side quotes: cheap-ask band + spread ────────────────────────
    lq       = contract_prices.get(losing, {})
    l_ask    = lq.get("ask_price", 0.0)
    l_bid    = lq.get("bid_price", 0.0)
    l_mid    = lq.get("mid_price", 0.0)
    l_spread = lq.get("spread",    0.0)

    if not (_CLC_MIN_ASK <= l_ask <= _CLC_MAX_ASK):
        clc_skip_stats.record("ask_band")
        return None
    if l_spread > _CLC_MAX_SPREAD:
        clc_skip_stats.record("spread")
        return None
    preferred_bucket = l_ask >= _CLC_PREFERRED_MIN_ASK

    # ── 4. Adverse z-score (BTC against the losing side, in σ) ────────────────
    std60     = _rolling_std(ticks, _CLC_Z_WINDOW_S)
    adverse_z = _z_from_target(btc_price, target_price, winning, std60)
    if adverse_z is None:
        clc_skip_stats.record("adverse_z")
        return None

    # ── 5. Volatility regime must not be violent ──────────────────────────────
    vol_regime = _volatility_regime(std60)
    if vol_regime == "violent":
        clc_skip_stats.record("violent_vol")
        logger.debug("%s: skip — volatility regime violent (std60=%s)",
                     _CLC_RULE_NAME, std60)
        return None
    whip = _whipsaw_score(ticks, n=20)

    # ── 6. Adverse-momentum tolerance ─────────────────────────────────────────
    adverse_mom = reversal_score(ticks, winning, n=_CLC_MOM_N)   # momentum AGAINST losing side

    history   = contract_history or {}
    los_hist  = history.get(losing, [])
    los_low   = _series_min(los_hist)
    bounce    = (l_mid - los_low) if los_low is not None else None
    bounced   = bounce is not None and bounce >= _CLC_MIN_BOUNCE

    # "no fresh low in the last 20s": the window-low sits above the session low.
    recent_low = _series_min(_recent_window(los_hist, _CLC_NEW_LOW_WINDOW_S))
    no_new_low = (
        recent_low is not None and los_low is not None and recent_low > los_low
    )

    momentum_tolerable = (adverse_mom <= _CLC_MAX_ADVERSE_MOM) or no_new_low or bounced
    if not momentum_tolerable:
        clc_skip_stats.record("momentum")
        logger.debug(
            "%s: skip — adverse momentum intolerable (adv_mom=%.1f, no_new_low=%s, bounced=%s)",
            _CLC_RULE_NAME, adverse_mom, no_new_low, bounced,
        )
        return None

    # ── 7. Historical reversal probability (self-referential lookup) ──────────
    # hour_block is unavailable at strategy time (time features are attached by
    # run_all afterwards), so the lookup starts broadening from the no-hour
    # level — acceptable, since hour is the first dimension dropped anyway.
    rp = (
        reversal_prob_fn(
            losing_ask         = l_ask,
            market_age_seconds = contract_age_seconds,
            adverse_z          = adverse_z,
            spread             = l_spread,
            volatility_regime  = vol_regime,
            hour_block         = None,
        )
        if reversal_prob_fn is not None
        else None
    )
    p3 = rp.get("p_plus_3c_before_minus_2c") if rp else None
    similar_n        = rp.get("similar_sample_count", 0) if rp else 0
    confidence_label = rp.get("confidence_label", "insufficient_data") if rp else "insufficient_data"
    meets_prob_gate  = p3 is not None and p3 >= _CLC_MIN_REVERSAL_PROB

    # ── Features for the signals row ──────────────────────────────────────────
    mom  = momentum_score(ticks, n=_CLC_MOM_N)
    rev  = reversal_score(ticks, losing, n=_CLC_MOM_N)
    vel  = btc_velocity(ticks, window_seconds=30.0)
    stds = rolling_stds(ticks)
    g    = _gap(btc_price, target_price)
    dg   = _directional_gap(btc_price, target_price, losing)
    gz   = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)

    s_type = _setup_type(l_ask, contract_age_seconds, adverse_z)
    p3_s   = f"{p3:.3f}" if p3 is not None else "n/a"
    reason = (
        f"{_CLC_RULE_NAME} {_CLC_RULE_VERSION}: BUY losing={losing} @ ask={l_ask:.4f} "
        f"(winning={winning}) | adv_z={adverse_z:+.2f} adv_mom={adverse_mom:+.1f} "
        f"spread={l_spread:.4f} regime={vol_regime} | "
        f"revprob(+3c/-2c)={p3_s} n={similar_n} ({confidence_label}) "
        f"gate={'pass' if meets_prob_gate else 'below'} | "
        f"phase={market_phase} age={contract_age_seconds:.0f}s setup[{s_type}]"
    )
    logger.info("Signal — %s", reason)
    clc_skip_stats.record("fired")

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _CLC_RULE_NAME,
        rule_version           = _CLC_RULE_VERSION,
        side                   = losing,          # BUY and track the cheap losing side

        contract_price         = l_mid,
        bid_price              = l_bid,
        ask_price              = l_ask,
        spread                 = l_spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
        signal_status          = "watch_only",
        # Context for the CLCReversalTracker (not persisted to the signals table).
        extra = {
            "setup_type":                          s_type,
            "market_phase":                        market_phase,
            "strict_early":                        strict_early,
            "winning_side":                        winning,
            "losing_side":                         losing,
            "adverse_z_score":                     round(adverse_z, 6),
            "adverse_directional_momentum_score":  round(adverse_mom, 4),
            "raw_gap_z_score":                     round(gz, 6) if gz is not None else None,
            "raw_momentum_score":                  round(mom, 4),
            "losing_contract_ask":                 round(l_ask, 6),
            "losing_contract_bid":                 round(l_bid, 6),
            "losing_contract_spread":              round(l_spread, 6),
            "losing_contract_low_since_open":      round(los_low, 6) if los_low is not None else None,
            "losing_contract_bounce_from_low":     round(bounce, 6) if bounce is not None else None,
            "historical_reversal_probability":     p3,
            "p_plus_2c_before_minus_2c":           rp.get("p_plus_2c_before_minus_2c") if rp else None,
            "p_plus_3c_before_minus_2c":           p3,
            "p_plus_4c_before_minus_3c":           rp.get("p_plus_4c_before_minus_3c") if rp else None,
            "similar_sample_count":                similar_n,
            "confidence_label":                    confidence_label,
            "meets_reversal_prob_gate":            meets_prob_gate,
            "preferred_price_bucket":              preferred_bucket,
            "volatility_regime":                   vol_regime,
            "whipsaw_score":                       round(whip, 4) if whip is not None else None,
            "market_age_seconds":                  round(contract_age_seconds, 3),
        },
    )


def cheap_losing_contract_late_reversal(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Late-window cheap-losing-contract reversal (WATCH-ONLY RESEARCH).

    NEVER paper-traded, NO live orders.  Tracked by the shared
    CLCReversalTracker, which simulates the same FIVE exit profiles against the
    observed bid path and writes rows to `clc_reversal_observations`.  This is a
    *separate* strategy from cheap_losing_contract_reversal_trail/v1 (kept live
    for early-vs-late comparison), distinguished by rule_name/rule_version.

    Hypothesis
    ----------
    The early variant entered while the losing contract was still falling
    (first live sample: 0% win, 100% stop, 0% trail activation).  This looks
    like an entry-timing problem.  In the last few minutes — after repricing has
    mostly happened — a cheap-but-not-dead losing contract that shows actual
    bounce/stall confirmation may reverse.  If it instead resumes making new
    lows after the signal, the normal stop / hard-stop logic records that as a
    loss; nothing is hidden.

    Entry gates (all hard, checked in order)
    ----------------------------------------
      1. time_remaining_seconds in [60, 300]   (the last 1–5 minutes)
      2. losing side = opposite of current BTC winner
      3. losing-side ask in [0.10, 0.30]        (floor RAISED from 0.05)
      4. losing-side spread <= 0.03
      5. adverse_z_score is measurable          (recorded/bucketed, NOT capped)
      6. volatility_regime != "violent"
      7. confirmation: bounce_from_low >= 0.02  AND  losing 10s change >= 0.00
    """
    # ── 1. Time-window gate (last 1–5 minutes) ────────────────────────────────
    if not (_CLC_LATE_MIN_TR_S <= time_remaining_seconds <= _CLC_LATE_MAX_TR_S):
        # Only count near-misses just outside the window; ignore the rest.
        if _CLC_LATE_NEARMISS_TR_LO <= time_remaining_seconds <= _CLC_LATE_NEARMISS_TR_HI:
            clc_late_skip_stats.record("time_window")
        return None
    market_phase = _CLC_LATE_PHASE

    # ── 2. Winning / losing side ──────────────────────────────────────────────
    if btc_price > target_price:
        winning, losing = "YES", "NO"
    elif btc_price < target_price:
        winning, losing = "NO", "YES"
    else:
        clc_late_skip_stats.record("no_side")
        return None

    # ── 3. Losing-side quotes: cheap-ask band + spread ────────────────────────
    lq       = contract_prices.get(losing, {})
    l_ask    = lq.get("ask_price", 0.0)
    l_bid    = lq.get("bid_price", 0.0)
    l_mid    = lq.get("mid_price", 0.0)
    l_spread = lq.get("spread",    0.0)

    if not (_CLC_LATE_MIN_ASK <= l_ask <= _CLC_LATE_MAX_ASK):
        clc_late_skip_stats.record("ask_band")
        return None
    if l_spread > _CLC_LATE_MAX_SPREAD:
        clc_late_skip_stats.record("spread")
        return None
    preferred_bucket = l_ask >= _CLC_LATE_MIN_ASK   # whole band is the preferred 0.10+ band

    # ── 4. Adverse z-score (BTC against the losing side, in σ) — recorded only ─
    std60     = _rolling_std(ticks, _CLC_LATE_Z_WINDOW_S)
    adverse_z = _z_from_target(btc_price, target_price, winning, std60)
    if adverse_z is None:
        clc_late_skip_stats.record("adverse_z_missing")
        return None

    # ── 5. Volatility regime must not be violent ──────────────────────────────
    vol_regime = _volatility_regime(std60)
    if vol_regime == "violent":
        clc_late_skip_stats.record("violent_vol")
        logger.debug("%s: skip — volatility regime violent (std60=%s)",
                     _CLC_LATE_RULE_NAME, std60)
        return None
    whip = _whipsaw_score(ticks, n=20)

    # ── 6. Confirmation: actual bounce off the low AND a non-falling last 10s ──
    history   = contract_history or {}
    los_hist  = history.get(losing, [])
    los_low   = _series_min(los_hist)
    bounce    = (l_mid - los_low) if los_low is not None else None
    los_mom10 = _series_change(los_hist, _CLC_LATE_MOM_WINDOW_S)

    bounced     = bounce    is not None and bounce    >= _CLC_LATE_MIN_BOUNCE
    not_falling = los_mom10 is not None and los_mom10 >= _CLC_LATE_MIN_MOM10
    if not (bounced and not_falling):
        clc_late_skip_stats.record("confirmation")
        logger.debug(
            "%s: skip — no confirmation (bounce=%s>=%.2f? %s, mom10=%s>=%.2f? %s)",
            _CLC_LATE_RULE_NAME, bounce, _CLC_LATE_MIN_BOUNCE, bounced,
            los_mom10, _CLC_LATE_MIN_MOM10, not_falling,
        )
        return None

    # ── Features for the signals row ──────────────────────────────────────────
    adverse_mom = reversal_score(ticks, winning, n=_CLC_LATE_MOM_N)
    mom  = momentum_score(ticks, n=_CLC_LATE_MOM_N)
    rev  = reversal_score(ticks, losing, n=_CLC_LATE_MOM_N)
    vel  = btc_velocity(ticks, window_seconds=30.0)
    stds = rolling_stds(ticks)
    g    = _gap(btc_price, target_price)
    dg   = _directional_gap(btc_price, target_price, losing)
    gz   = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)

    s_type = _setup_type(l_ask, contract_age_seconds, adverse_z)
    reason = (
        f"{_CLC_LATE_RULE_NAME} {_CLC_LATE_RULE_VERSION}: BUY losing={losing} "
        f"@ ask={l_ask:.4f} (winning={winning}) | adv_z={adverse_z:+.2f} "
        f"bounce={bounce:+.4f} mom10={los_mom10:+.4f} spread={l_spread:.4f} "
        f"regime={vol_regime} | phase={market_phase} "
        f"t_remain={time_remaining_seconds:.0f}s setup[{s_type}]"
    )
    logger.info("Signal — %s", reason)
    clc_late_skip_stats.record("fired")

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _CLC_LATE_RULE_NAME,
        rule_version           = _CLC_LATE_RULE_VERSION,
        side                   = losing,          # BUY and track the cheap losing side

        contract_price         = l_mid,
        bid_price              = l_bid,
        ask_price              = l_ask,
        spread                 = l_spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
        signal_status          = "watch_only",
        # Context for the shared CLCReversalTracker (mirrors the early variant's
        # extra keys so the tracker is rule-agnostic).  No reversal-probability
        # lookup here — left as insufficient_data on purpose.
        extra = {
            "setup_type":                          s_type,
            "market_phase":                        market_phase,
            "strict_early":                        False,
            "winning_side":                        winning,
            "losing_side":                         losing,
            "adverse_z_score":                     round(adverse_z, 6),
            "adverse_directional_momentum_score":  round(adverse_mom, 4),
            "raw_gap_z_score":                     round(gz, 6) if gz is not None else None,
            "raw_momentum_score":                  round(mom, 4),
            "losing_contract_ask":                 round(l_ask, 6),
            "losing_contract_bid":                 round(l_bid, 6),
            "losing_contract_spread":              round(l_spread, 6),
            "losing_contract_low_since_open":      round(los_low, 6) if los_low is not None else None,
            "losing_contract_bounce_from_low":     round(bounce, 6) if bounce is not None else None,
            "historical_reversal_probability":     None,
            "p_plus_2c_before_minus_2c":           None,
            "p_plus_3c_before_minus_2c":           None,
            "p_plus_4c_before_minus_3c":           None,
            "similar_sample_count":                0,
            "confidence_label":                    "insufficient_data",
            "meets_reversal_prob_gate":            False,
            "preferred_price_bucket":              preferred_bucket,
            "volatility_regime":                   vol_regime,
            "whipsaw_score":                       round(whip, 4) if whip is not None else None,
            "market_age_seconds":                  round(contract_age_seconds, 3),
        },
    )


# ── contract_value_bounce_scalp (watch-only research) ────────────────────────
#
# Hypothesis
# ----------
# A losing contract that has already sold down and then BOUNCED in contract
# value can be scalped for a short +3c to +6c gain.  The entry trigger is
# CONTRACT-LED: the losing-side contract must already be bouncing off its
# session low.  BTC context (adverse z-score) is RECORDED but not a hard gate.
# This is intentionally separate from:
#   - cheap_losing_contract_reversal_trail (fires while contract is falling)
#   - post_move_continuation_scalp (fires on the WINNING side, after a BTC move)
#
# WATCH-ONLY: registered below and keyed into _WATCH_ONLY_KEYS, so signals are
# logged to the `signals` table but NEVER open a paper trade.  Exit-test
# simulation lives in scripts/contract_value_bounce_backtest.py.

_CVBS_RULE_NAME    = "contract_value_bounce_scalp"
_CVBS_RULE_VERSION = "v1"

_CVBS_MIN_MARKET_AGE_S   = 60.0    # entry only after 1 minute of market life
_CVBS_MAX_MARKET_AGE_S   = 180.0   # stop looking after 3 minutes
_CVBS_MIN_ASK            = 0.10    # losing-side ask floor (very_cheap bucket)
_CVBS_MAX_ASK            = 0.30    # losing-side ask ceiling
_CVBS_PRIMARY_ASK_FLOOR  = 0.20    # primary hypothesis: 0.20–0.30
_CVBS_MAX_SPREAD         = 0.02    # tighter than CLC: spread ≤ 0.02
_CVBS_MIN_BOUNCE         = 0.02    # losing side must have bounced ≥ +2c off session low
_CVBS_MIN_MOM10          = 0.00    # losing-side 10s change >= 0 (not currently falling)
_CVBS_CALM_REGIMES: frozenset[str] = frozenset({"calm", "normal"})
_CVBS_Z_WINDOW_S         = 60.0
_CVBS_MOM_N              = 10
_CVBS_MOM_WINDOW_S       = 10.0


def _cvbs_price_bucket(ask: float) -> str:
    """Price bucket for the contract_value_bounce_scalp hypothesis."""
    if ask < 0.20:
        return "very_cheap"       # 0.10–0.20 (secondary)
    return "cheap_primary"        # 0.20–0.30 (primary)


def _cvbs_bounce_bucket(bounce: float) -> str:
    """Bounce-from-low size bucket."""
    if bounce < 0.03:
        return "bounce_2c_3c"     # 0.02–0.03
    if bounce < 0.05:
        return "bounce_3c_5c"     # 0.03–0.05
    return "bounce_5c_plus"       # >= 0.05


def contract_value_bounce_scalp(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Contract-value bounce scalp (WATCH-ONLY RESEARCH — never paper-traded).

    Hypothesis
    ----------
    A losing contract that has already sold off and then bounced upward in
    CONTRACT PRICE (not BTC reversing the strike) can be scalped for a short
    +3c–+6c gain after realistic spread and slippage.

    This is NOT a BTC-reversal prediction.  BTC stays against the losing side
    throughout — the setup is purely a contract-value bounce after an overshoot.

    Entry gates (all hard, contract-led)
    ----------------------------------------
      1. 60 ≤ market_age_seconds ≤ 180       (1–3 min into the window)
      2. losing side = opposite of current BTC winner
      3. losing-side ask in [0.10, 0.30]      (cheap-but-not-dead)
      4. losing-side spread ≤ 0.02            (tight enough to scalp)
      5. volatility_regime in {calm, normal}  (not elevated/violent)
      6. bounce_from_low ≥ 0.02              (already bounced ≥ +2c off session low)
      7. losing-side 10s contract change ≥ 0  (not currently falling back)

    BTC context recorded (NOT a hard gate)
    --------------------------------------
      adverse_z_score : how far BTC sits against the losing side (σ)
      raw_momentum_score : stored for reporting

    Primary hypothesis bucket: losing_contract_ask in [0.20, 0.30]
    Secondary bucket:           losing_contract_ask in [0.10, 0.20)
    """
    # ── 1. Market-age gate (1–3 minutes) ─────────────────────────────────────
    if not (_CVBS_MIN_MARKET_AGE_S <= contract_age_seconds <= _CVBS_MAX_MARKET_AGE_S):
        return None

    # ── 2. Determine winning / losing side from BTC position ─────────────────
    if btc_price > target_price:
        winning, losing = "YES", "NO"
    elif btc_price < target_price:
        winning, losing = "NO", "YES"
    else:
        return None

    # ── 3. Losing-side quotes: ask band + spread ──────────────────────────────
    lq       = contract_prices.get(losing, {})
    l_ask    = lq.get("ask_price", 0.0) or 0.0
    l_bid    = lq.get("bid_price", 0.0) or 0.0
    l_mid    = lq.get("mid_price", 0.0) or 0.0
    l_spread = lq.get("spread",    0.0) or 0.0

    if not (_CVBS_MIN_ASK <= l_ask <= _CVBS_MAX_ASK):
        return None
    if l_spread > _CVBS_MAX_SPREAD:
        return None

    # ── 4. Volatility regime must be calm or normal ───────────────────────────
    std60      = _rolling_std(ticks, _CVBS_Z_WINDOW_S)
    vol_regime = _volatility_regime(std60)
    if vol_regime not in _CVBS_CALM_REGIMES:
        return None

    # ── 5. Bounce-from-low gate (contract-led entry trigger) ─────────────────
    history  = contract_history or {}
    los_hist = history.get(losing, [])
    los_low  = _series_min(los_hist)
    bounce   = (l_mid - los_low) if los_low is not None else None

    if bounce is None or bounce < _CVBS_MIN_BOUNCE:
        logger.debug(
            "%s: skip — losing(%s) bounce=%s < %.2f",
            _CVBS_RULE_NAME, losing,
            f"{bounce:+.4f}" if bounce is not None else "N/A",
            _CVBS_MIN_BOUNCE,
        )
        return None

    # ── 6. Not currently falling back (10s contract momentum ≥ 0) ────────────
    los_mom10 = _series_change(los_hist, _CVBS_MOM_WINDOW_S)
    if los_mom10 is None or los_mom10 < _CVBS_MIN_MOM10:
        logger.debug(
            "%s: skip — losing(%s) 10s change=%s < 0",
            _CVBS_RULE_NAME, losing,
            f"{los_mom10:+.4f}" if los_mom10 is not None else "N/A",
        )
        return None

    # ── 7. BTC adverse z-score — CONTEXT ONLY, not a hard gate ───────────────
    adverse_z = _z_from_target(btc_price, target_price, winning, std60)

    # ── Features for the signals row ──────────────────────────────────────────
    mom  = momentum_score(ticks, n=_CVBS_MOM_N)
    rev  = reversal_score(ticks, losing, n=_CVBS_MOM_N)
    vel  = btc_velocity(ticks, window_seconds=30.0)
    stds = rolling_stds(ticks)
    g    = _gap(btc_price, target_price)
    dg   = _directional_gap(btc_price, target_price, losing)
    gz   = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)
    whip = _whipsaw_score(ticks, n=20)

    price_bucket  = _cvbs_price_bucket(l_ask)
    bounce_bucket = _cvbs_bounce_bucket(bounce)
    adverse_z_s   = f"{adverse_z:+.2f}" if adverse_z is not None else "n/a"

    reason = (
        f"{_CVBS_RULE_NAME} {_CVBS_RULE_VERSION}: BUY losing={losing} "
        f"@ ask={l_ask:.4f} [{price_bucket}] | "
        f"bounce={bounce:+.4f} [{bounce_bucket}] mom10={los_mom10:+.4f} "
        f"spread={l_spread:.4f} regime={vol_regime} | "
        f"adv_z={adverse_z_s} btc={btc_price:,.2f} target={target_price:,.2f} "
        f"age={contract_age_seconds:.0f}s remaining={time_remaining_seconds:.0f}s"
    )
    logger.info("Signal — %s", reason)

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _CVBS_RULE_NAME,
        rule_version           = _CVBS_RULE_VERSION,
        side                   = losing,          # BUY the bouncing losing-side contract

        contract_price         = l_mid,
        bid_price              = l_bid,
        ask_price              = l_ask,
        spread                 = l_spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
        signal_status          = "watch_only",
        extra = {
            "winning_side":                        winning,
            "losing_side":                         losing,
            "losing_contract_ask":                 round(l_ask, 6),
            "losing_contract_bid":                 round(l_bid, 6),
            "losing_contract_spread":              round(l_spread, 6),
            "losing_contract_low_since_open":      round(los_low, 6) if los_low is not None else None,
            "losing_contract_bounce_from_low":     round(bounce, 6),
            "losing_contract_mom_10s":             round(los_mom10, 6),
            "price_bucket":                        price_bucket,
            "bounce_bucket":                       bounce_bucket,
            "adverse_z_score":                     round(adverse_z, 6) if adverse_z is not None else None,
            "raw_momentum_score":                  round(mom, 4),
            "volatility_regime":                   vol_regime,
            "whipsaw_score":                       round(whip, 4) if whip is not None else None,
            "market_age_seconds":                  round(contract_age_seconds, 3),
        },
    )


# ── post_move_continuation_scalp (watch-only research) ─────────────────────────
#
# Hypothesis
# ----------
# A contract can be profitable to scalp if it begins trending UPWARD and the move
# continues long enough that the exit bid clears the entry ask after spread and
# slippage.  The goal is NOT to predict expiry — only to capture a short
# continuation move once the contract starts repricing upward.
#
# WATCH-ONLY: registered below and keyed into _WATCH_ONLY_KEYS, so signals are
# logged to the `signals` table but NEVER open a paper trade.  Forward exit-test
# simulation + reporting live in scripts/post_move_continuation_backtest.py and
# scripts/post_move_continuation_report.py (lookahead-safe replay, paper-only).

_PMCS_RULE_NAME    = "post_move_continuation_scalp"
_PMCS_RULE_VERSION = "v1"

_PMCS_MIN_TIME_REMAINING = 120     # seconds
_PMCS_MAX_TIME_REMAINING = 300     # seconds
_PMCS_MIN_ASK            = 0.55    # scalp candidate ask floor
_PMCS_MAX_SCALP_ASK      = 0.85    # ask >= this → 0.85+ context only (NOT scalped)
_PMCS_MAX_SPREAD         = 0.03
_PMCS_MIN_CHANGE_10S     = 0.02    # +2c over 10s …
_PMCS_MIN_CHANGE_30S     = 0.04    # … OR +4c over 30s confirms upward move
_PMCS_MIN_DIR_MOMENTUM   = 3.0     # directional (YES = raw, NO = -raw)
_PMCS_MIN_DIR_GAP_Z      = 1.5     # directional (YES = raw, NO = -raw)
_PMCS_MOMENTUM_N         = 10


def _pmcs_price_bucket(ask: Optional[float]) -> str:
    """Contract-ask price bucket for the post-move continuation thesis."""
    if ask is None:
        return "N/A"
    if ask < 0.55:
        return "below_range"
    if ask < 0.65:
        return "early_momentum"          # 0.55–0.65
    if ask < 0.80:
        return "premium_midrange"        # 0.65–0.80
    if ask < 0.85:
        return "late_premium_caution"    # 0.80–0.85
    return "no_scalp_or_expiry_hold_only"  # 0.85+ — watch-only context, never scalped


def post_move_continuation_scalp(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Watch-only continuation scalp: log entries where a contract has just started
    trending upward, with directional momentum + gap-z confirming the side.

    Entry observation conditions
    ----------------------------
    1. 120 ≤ time_remaining_seconds ≤ 300
    2. Side chosen from BTC position (YES: btc>target, NO: btc<target)
    3. Side's contract ask ≥ 0.55  (upper handling: ask in [0.55,0.85) = scalp
       candidate by bucket; ask ≥ 0.85 logged as watch-only context only)
    4. Spread ≤ 0.03
    5. Upward move: contract_price_change_10s ≥ +0.02 OR _30s ≥ +0.04
    6. directional_momentum_score ≥ 3   (YES = raw_mom,  NO = −raw_mom)
    7. directional_gap_z_score   ≥ 1.5  (YES = raw_gz,   NO = −raw_gz)

    Side-normalised values are stored in `extra` for the research report.  This
    function NEVER trades — it is registered watch-only.
    """
    # ── 1. Time-window gate ───────────────────────────────────────────────────
    if not (_PMCS_MIN_TIME_REMAINING <= time_remaining_seconds <= _PMCS_MAX_TIME_REMAINING):
        return None

    # ── 2. Side from BTC position relative to target ──────────────────────────
    if btc_price > target_price:
        side = "YES"
    elif btc_price < target_price:
        side = "NO"
    else:
        return None

    # ── 3. Side contract quotes ───────────────────────────────────────────────
    quotes = contract_prices.get(side, {})
    ask    = quotes.get("ask_price")
    bid    = quotes.get("bid_price")
    mid    = quotes.get("mid_price", 0.0)
    spread = quotes.get("spread")
    if ask is None or bid is None or ask <= 0:
        return None
    if ask < _PMCS_MIN_ASK:
        return None
    if spread is not None and spread > _PMCS_MAX_SPREAD:
        return None

    # ── 4. Upward-move confirmation from this side's contract-mid history ──────
    side_hist = (contract_history or {}).get(side, []) or []
    ref_ts    = side_hist[-1].ts if side_hist else None
    chg5  = _series_change(side_hist, 5.0,  ref_ts)
    chg10 = _series_change(side_hist, 10.0, ref_ts)
    chg30 = _series_change(side_hist, 30.0, ref_ts)
    chg60 = _series_change(side_hist, 60.0, ref_ts)

    moving_up = (
        (chg10 is not None and chg10 >= _PMCS_MIN_CHANGE_10S)
        or (chg30 is not None and chg30 >= _PMCS_MIN_CHANGE_30S)
    )
    if not moving_up:
        return None

    # ── 5. Directional momentum & gap-z (side-normalised) ─────────────────────
    mom  = momentum_score(ticks, n=_PMCS_MOMENTUM_N)
    rev  = reversal_score(ticks, side, n=_PMCS_MOMENTUM_N)
    vel  = btc_velocity(ticks, window_seconds=30.0)
    stds = rolling_stds(ticks)
    g    = _gap(btc_price, target_price)
    dg   = _directional_gap(btc_price, target_price, side)
    gz   = _gap_z_score(btc_price, target_price, ticks, window_seconds=60.0)

    dir_mom = mom if side == "YES" else -mom
    if dir_mom < _PMCS_MIN_DIR_MOMENTUM:
        return None

    dir_gz = gz if side == "YES" else (-gz if gz is not None else None)
    if dir_gz is None or dir_gz < _PMCS_MIN_DIR_GAP_Z:
        return None

    # ── 6. Build watch-only signal ────────────────────────────────────────────
    price_bucket    = _pmcs_price_bucket(ask)
    scalp_candidate = ask < _PMCS_MAX_SCALP_ASK   # 0.85+ = context only, not scalped

    reason = (
        f"{_PMCS_RULE_NAME} {_PMCS_RULE_VERSION}: {side} @ ask={ask:.4f} "
        f"[{price_bucket}{'' if scalp_candidate else ' / context-only'}] | "
        f"chg10={chg10 if chg10 is None else round(chg10, 4)} "
        f"chg30={chg30 if chg30 is None else round(chg30, 4)} | "
        f"dir_mom={dir_mom:+.0f} dir_gz={dir_gz:+.4f} | "
        f"remaining={time_remaining_seconds:.0f}s"
    )
    logger.info("Signal — %s", reason)

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _PMCS_RULE_NAME,
        rule_version           = _PMCS_RULE_VERSION,
        side                   = side,

        contract_price         = mid,
        bid_price              = bid,
        ask_price              = ask,
        spread                 = spread if spread is not None else 0.0,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
        signal_status          = "watch_only",
        extra                  = {
            "price_bucket":              price_bucket,
            "scalp_candidate":           scalp_candidate,
            "raw_momentum_score":        round(mom, 4),
            "directional_momentum_score": round(dir_mom, 4),
            "raw_gap_z_score":           round(gz, 4) if gz is not None else None,
            "directional_gap_z_score":   round(dir_gz, 4) if dir_gz is not None else None,
            "contract_price_change_5s":  round(chg5,  4) if chg5  is not None else None,
            "contract_price_change_10s": round(chg10, 4) if chg10 is not None else None,
            "contract_price_change_30s": round(chg30, 4) if chg30 is not None else None,
            "contract_price_change_60s": round(chg60, 4) if chg60 is not None else None,
        },
    )


# ── delayed_contract_value_reversal_bounce (watch-only research) ─────────────────
#
# Hypothesis
# ----------
# In BTC 15-minute up/down markets, a losing contract that drops sharply early
# (into the 0.20–0.40 range) may flush a few more cents before producing a short
# +3¢–+6¢ bounce in contract value.  The goal is to scalp the contract-price
# bounce — BTC does NOT need to reverse; only the contract ask/bid needs to lift.
#
# Three entry timing versions are compared in reports:
#   v1  immediate   — entered when contract first entered 0.20–0.40 watch band
#   v2  2c+1c       — entered after extra ≥2¢ flush + ≥1¢ contract bounce (main signal)
#   v3  5c+2c       — deeper subset: flush ≥5¢ AND bounce ≥2¢ at signal time
# Four exit tests (A/B/C/D) are simulated by DCVRBTracker (no paper trade opened).
#
# WATCH-ONLY / status=watch_only: registered below and keyed into _WATCH_ONLY_KEYS.

_DCVRB_RULE_NAME    = "delayed_contract_value_reversal_bounce"
_DCVRB_RULE_VERSION = "v1"

# Stage 1: watch trigger gates
_DCVRB_MIN_AGE_S          = 60.0    # market_age_seconds ∈ [60, 240]
_DCVRB_MAX_AGE_S          = 240.0
_DCVRB_MIN_TR_S           = 300.0   # time_remaining_seconds ≥ 300
_DCVRB_MIN_CONTRACT_ASK   = 0.20    # losing-side ask enters [0.20, 0.40]
_DCVRB_MAX_CONTRACT_ASK   = 0.40
_DCVRB_MIN_DROP_FROM_OPEN = 0.15    # dropped ≥ 0.15 from market-open price …
_DCVRB_MIN_DROP_FROM_HIGH = 0.20    # … OR ≥ 0.20 from recent contract high
_DCVRB_MAX_SPREAD         = 0.03    # spread ≤ 0.03

# Stage 2: extra flush requirement
_DCVRB_MIN_EXTRA_FLUSH    = 0.02    # contract must flush ≥ 2¢ below watch_start_ask

# Stage 3: bounce confirmation
_DCVRB_MIN_BOUNCE         = 0.01    # ask/bid bounced ≥ 1¢ from local_low
_DCVRB_STRONG_BOUNCE      = 0.02    # strong-bounce flag threshold (≥ 2¢)

# V3 comparison thresholds (deep flush + strong bounce subset)
_DCVRB_V3_MIN_FLUSH       = 0.05    # flush ≥ 5¢ required for v3 qualification
_DCVRB_V3_MIN_BOUNCE      = 0.02    # bounce ≥ 2¢ required for v3 qualification

# Contract price change windows (applied to losing-side mid history)
_DCVRB_CHG_5S    =  5.0
_DCVRB_CHG_10S   = 10.0
_DCVRB_CHG_30S   = 30.0
_DCVRB_MOM_N     = 10
_DCVRB_Z_WIN_S   = 60.0


@dataclass
class _DCVRBWatchState:
    """In-memory watch state for a single market/losing-side."""
    market_ticker:            str
    side:                     str    # losing side ("YES" or "NO")
    watch_start_time:         float  # Unix epoch seconds
    watch_start_contract_ask: float
    watch_start_contract_bid: float
    contract_open_price:      float  # earliest mid/ask seen in contract history
    contract_recent_high:     float  # highest mid seen in contract history
    spread_at_watch:          float
    volatility_60s_at_watch:  Optional[float]
    local_low_since_watch:    float  # lowest ask seen since watch started
    signal_fired:             bool = False


# Module-level watch states keyed by "market_ticker/losing_side".
# Cleared automatically on market rollover (old ticker prefix).
_dcvrb_watch_states: dict[str, _DCVRBWatchState] = {}


def _dcvrb_price_bucket(ask: float) -> str:
    if ask < 0.25:
        return "0.20-0.25"
    if ask < 0.30:
        return "0.25-0.30"
    if ask < 0.35:
        return "0.30-0.35"
    return "0.35-0.40"


def _dcvrb_flush_bucket(extra_flush: float) -> str:
    if extra_flush < 0.03:
        return "0.02-0.03"
    if extra_flush < 0.05:
        return "0.03-0.05"
    return "0.05+"


def _dcvrb_bounce_bucket(bounce: float) -> str:
    if bounce < 0.02:
        return "0.01-0.02"
    return "0.02+"


def delayed_contract_value_reversal_bounce(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
) -> Optional[Signal]:
    """
    Hypothesis (WATCH-ONLY RESEARCH — never paper-traded, no live orders)
    ----------------------------------------------------------------------
    In BTC 15-minute up/down markets, a losing contract that drops sharply
    early (Stage 1: ask enters 0.20–0.40 with ≥15¢ drop from open) may flush
    a few more cents (Stage 2: ≥2¢ below watch start) before producing a short
    +3¢–+6¢ bounce in contract value (Stage 3: ≥1¢ lift from local low).

    This strategy fires once per market/side when Stage 3 is first confirmed.
    The DCVRBTracker then simulates four exit tests (A/B/C/D) × three entry
    timing comparison versions (v1 immediate, v2 main, v3 deep flush).

    BTC price, velocity, and z-score are logged as context only — they are NOT
    hard requirements for the v1 watch-only signal.
    """
    global _dcvrb_watch_states

    # ── Clear stale states from previous markets ──────────────────────────────
    stale = [k for k in list(_dcvrb_watch_states.keys())
             if not k.startswith(f"{market_ticker}/")]
    for k in stale:
        del _dcvrb_watch_states[k]

    # ── Timing gates ──────────────────────────────────────────────────────────
    if not (_DCVRB_MIN_AGE_S <= contract_age_seconds <= _DCVRB_MAX_AGE_S):
        return None
    if time_remaining_seconds < _DCVRB_MIN_TR_S:
        return None

    # ── Determine losing side ──────────────────────────────────────────────────
    if btc_price > target_price:
        winning, losing = "YES", "NO"
    elif btc_price < target_price:
        winning, losing = "NO", "YES"
    else:
        return None

    # ── Losing-side quotes ────────────────────────────────────────────────────
    lq       = contract_prices.get(losing, {})
    l_ask    = lq.get("ask_price")
    l_bid    = lq.get("bid_price")
    l_mid    = lq.get("mid_price", 0.0)
    l_spread = lq.get("spread", 0.0)

    if not l_ask or not l_bid or l_ask <= 0:
        return None

    state_key = f"{market_ticker}/{losing}"
    ws = _dcvrb_watch_states.get(state_key)

    # ── If already fired this market/side, suppress ───────────────────────────
    if ws is not None and ws.signal_fired:
        return None

    # ── Losing-side contract history (mid series, same market window) ──────────
    hist = (contract_history or {}).get(losing, [])

    # ── Stage 1: Create watch state when conditions first met ─────────────────
    if ws is None:
        # Ask must be in the 0.20–0.40 band
        if not (_DCVRB_MIN_CONTRACT_ASK <= l_ask <= _DCVRB_MAX_CONTRACT_ASK):
            return None
        if l_spread > _DCVRB_MAX_SPREAD:
            return None

        # Compute drop from market-open price and from recent contract high.
        # contract_history holds mid prices; use them for drop measurement.
        contract_open_price  = hist[0].price  if hist else l_mid
        contract_recent_high = max((t.price for t in hist), default=l_mid)

        drop_from_open = contract_open_price - l_ask
        drop_from_high = contract_recent_high - l_ask

        if drop_from_open < _DCVRB_MIN_DROP_FROM_OPEN and drop_from_high < _DCVRB_MIN_DROP_FROM_HIGH:
            return None

        # Compute volatility context at watch time (logged, not a hard gate)
        std60 = _rolling_std(ticks, _DCVRB_Z_WIN_S)
        vol60 = std60

        now_ts = ticks[-1].ts if ticks else contract_age_seconds
        ws = _DCVRBWatchState(
            market_ticker            = market_ticker,
            side                     = losing,
            watch_start_time         = now_ts,
            watch_start_contract_ask = l_ask,
            watch_start_contract_bid = l_bid,
            contract_open_price      = contract_open_price,
            contract_recent_high     = contract_recent_high,
            spread_at_watch          = l_spread,
            volatility_60s_at_watch  = vol60,
            local_low_since_watch    = l_ask,
        )
        _dcvrb_watch_states[state_key] = ws
        logger.debug(
            "DCVRB: watch_state created — %s losing=%s ask=%.4f "
            "drop_from_open=%.4f drop_from_high=%.4f",
            market_ticker, losing, l_ask, drop_from_open, drop_from_high,
        )
        return None   # Stage 1 only — wait for flush + bounce

    # ── Stage 2 + 3: Update watch state ───────────────────────────────────────
    # Track the lowest ask seen since watch started (flush depth)
    if l_ask < ws.local_low_since_watch:
        ws.local_low_since_watch = l_ask

    extra_flush           = ws.watch_start_contract_ask - ws.local_low_since_watch
    drop_from_watch_start = ws.watch_start_contract_ask - l_ask
    bounce_from_local_low = l_ask - ws.local_low_since_watch  # positive when bounced

    # Stage 2: must have flushed at least 2¢ below watch_start_ask
    if extra_flush < _DCVRB_MIN_EXTRA_FLUSH:
        return None

    # Stage 3: ask/bid must have bounced ≥ 1¢ from local low
    if bounce_from_local_low < _DCVRB_MIN_BOUNCE:
        return None

    # Stage 3: spread still tight
    if l_spread > _DCVRB_MAX_SPREAD:
        return None

    # Stage 3: time gate still valid
    if time_remaining_seconds < _DCVRB_MIN_TR_S:
        return None

    # Stage 3: at least one of the short-term contract-price changes must be positive
    chg5  = _series_change(hist, _DCVRB_CHG_5S)
    chg10 = _series_change(hist, _DCVRB_CHG_10S)
    chg30 = _series_change(hist, _DCVRB_CHG_30S)

    price_rising = (
        (chg5  is not None and chg5  > 0.0)
        or (chg10 is not None and chg10 > 0.0)
    )
    if not price_rising:
        return None

    # ── Build signal (fires exactly once per market/side) ─────────────────────
    ws.signal_fired = True

    mom   = momentum_score(ticks, n=_DCVRB_MOM_N)
    rev   = reversal_score(ticks, losing, n=_DCVRB_MOM_N)
    vel10 = btc_velocity(ticks, window_seconds=10.0)
    vel30 = btc_velocity(ticks, window_seconds=30.0)
    stds  = rolling_stds(ticks)
    g     = _gap(btc_price, target_price)
    dg    = _directional_gap(btc_price, target_price, losing)
    gz    = _gap_z_score(btc_price, target_price, ticks, window_seconds=_DCVRB_Z_WIN_S)

    # Side-normalised directional values
    dir_gz  = gz  if losing == "YES" else (-gz  if gz  is not None else None)
    dir_mom = mom if losing == "YES" else -mom

    vol_regime         = _volatility_regime(stds["std_60s"])
    extra_flush_bucket = _dcvrb_flush_bucket(extra_flush)
    bounce_bucket      = _dcvrb_bounce_bucket(bounce_from_local_low)
    strong_bounce      = bounce_from_local_low >= _DCVRB_STRONG_BOUNCE
    v3_qualified       = (
        extra_flush        >= _DCVRB_V3_MIN_FLUSH
        and bounce_from_local_low >= _DCVRB_V3_MIN_BOUNCE
    )
    price_bucket       = _dcvrb_price_bucket(ws.watch_start_contract_ask)
    drop_from_open     = ws.contract_open_price  - ws.watch_start_contract_ask
    drop_from_high     = ws.contract_recent_high - ws.watch_start_contract_ask

    reason = (
        f"{_DCVRB_RULE_NAME} {_DCVRB_RULE_VERSION}: losing={losing} "
        f"watch_ask={ws.watch_start_contract_ask:.4f} "
        f"local_low={ws.local_low_since_watch:.4f} "
        f"ask_now={l_ask:.4f} "
        f"flush={extra_flush:+.4f}[{extra_flush_bucket}] "
        f"bounce={bounce_from_local_low:+.4f}[{bounce_bucket}] "
        f"strong={strong_bounce} v3={v3_qualified} | "
        f"chg5={chg5 if chg5 is None else round(chg5, 4)} "
        f"chg10={chg10 if chg10 is None else round(chg10, 4)} | "
        f"spread={l_spread:.4f} remaining={time_remaining_seconds:.0f}s "
        f"age={contract_age_seconds:.0f}s"
    )
    logger.info("Signal — %s", reason)

    return Signal(
        market_ticker          = market_ticker,
        rule_name              = _DCVRB_RULE_NAME,
        rule_version           = _DCVRB_RULE_VERSION,
        side                   = losing,

        contract_price         = l_mid,
        bid_price              = l_bid,
        ask_price              = l_ask,
        spread                 = l_spread,

        btc_price              = btc_price,
        target_price           = target_price,
        gap                    = g,
        directional_gap        = dg,
        gap_z_score            = gz,

        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,

        momentum_score         = mom,
        reversal_score         = rev,
        btc_velocity           = vel30,
        volatility_30s         = stds["std_30s"],
        volatility_60s         = stds["std_60s"],
        volatility_120s        = stds["std_120s"],

        reason                 = reason,
        signal_status          = "watch_only",
        extra = {
            # Stage 1 watch-state context
            "contract_open_price":          round(ws.contract_open_price, 6),
            "contract_recent_high":         round(ws.contract_recent_high, 6),
            "watch_start_contract_ask":     round(ws.watch_start_contract_ask, 6),
            "watch_start_contract_bid":     round(ws.watch_start_contract_bid, 6),
            "spread_at_watch":              round(ws.spread_at_watch, 6),
            "volatility_60s_at_watch":      round(ws.volatility_60s_at_watch, 6) if ws.volatility_60s_at_watch is not None else None,
            "drop_from_open":               round(drop_from_open, 6),
            "drop_from_recent_high":        round(drop_from_high, 6),
            # Stage 2/3 flush + bounce metrics
            "local_low_since_watch":        round(ws.local_low_since_watch, 6),
            "drop_from_watch_start":        round(drop_from_watch_start, 6),
            "extra_flush":                  round(extra_flush, 6),
            "bounce_from_local_low":        round(bounce_from_local_low, 6),
            "extra_flush_bucket":           extra_flush_bucket,
            "bounce_bucket":                bounce_bucket,
            "strong_bounce":                strong_bounce,
            "v3_qualified":                 v3_qualified,
            "price_bucket":                 price_bucket,
            # Contract price changes at signal time
            "contract_price_change_5s":     round(chg5,  6) if chg5  is not None else None,
            "contract_price_change_10s":    round(chg10, 6) if chg10 is not None else None,
            "contract_price_change_30s":    round(chg30, 6) if chg30 is not None else None,
            # BTC secondary context (NOT hard requirements for v1)
            "winning_side":                 winning,
            "losing_side":                  losing,
            "raw_gap_z_score":              round(gz,     6) if gz     is not None else None,
            "directional_gap_z_score":      round(dir_gz, 6) if dir_gz is not None else None,
            "raw_momentum_score":           round(mom,    4),
            "directional_momentum_score":   round(dir_mom, 4),
            "btc_velocity_10s":             round(vel10, 6) if vel10 is not None else None,
            "btc_velocity_30s":             round(vel30, 6) if vel30 is not None else None,
            "volatility_regime":            vol_regime,
            "market_age_seconds":           round(contract_age_seconds, 3),
        },
    )


# ── Strategy registry ─────────────────────────────────────────────────────────
#
# Active strategies are evaluated every poll cycle.
# Watch-only keys (see _WATCH_ONLY_KEYS below) are still evaluated and logged
# to the signals table but will NOT open paper trades.

_STRATEGIES = [
    premium_momentum_continuation,              # NO-side active; YES-side watch_only (see below)
    premium_no_midrange_scalp,                  # paper_active — NO only, 0.65–0.80 (v1, ask-gated)
    premium_no_midrange_scalp_v2,               # paper_active — NO only, 0.65–0.80 (v2, mid-gated + trail)
    premium_momentum_scalp_v2,                  # watch_only — conflicts with PNMS in 240-300s/NO band
    early_overextension_reversal_scalp,         # watch_only research — never paper-traded
    post_move_continuation_scalp,               # watch_only research — continuation scalp, never paper-traded
    delayed_contract_value_reversal_bounce,     # watch_only research — contract-price bounce scalp (3-stage)
    contract_value_bounce_scalp,                # watch_only research — losing-side contract-bounce scalp, never paper-traded
    # cheap_losing_contract_reversal_trail      # PAUSED 2026-06-02 — falsified (avg_mfe<0, hit-2c=100%); tracker/data retained
    # cheap_losing_contract_late_reversal       # PAUSED 2026-06-02 — falsified (avg_mfe<0, hit-2c=100%); tracker/data retained
    # cheap_reversal_scalp                      # disabled — removed from registry 2026-05-28
]

# Keys that produce signals for the DB log but must NOT open paper trades.
# Format: "rule_name/rule_version"        → all sides paused
#         "rule_name/rule_version/SIDE"   → one side paused
_WATCH_ONLY_KEYS: frozenset[str] = frozenset({
    "premium_momentum_continuation/v1/YES",           # YES-side PMC: watch-only
    "premium_momentum_scalp/v2",                      # PMS v2: conflicts with PNMS NO band
    "early_overextension_reversal_scalp/v1",          # research only — outcomes tracked, not traded
    "post_move_continuation_scalp/v1",                # research only — continuation scalp hypothesis, not traded
    "cheap_losing_contract_reversal_trail/v1",        # research only — 5 exit profiles simulated, not traded
    "cheap_losing_contract_late_reversal/v1",         # research only — late-window variant, not traded
    "delayed_contract_value_reversal_bounce/v1",      # research only — contract-price bounce, 4 exit tests
    "contract_value_bounce_scalp/v1",                 # research only — losing-side contract-bounce hypothesis, not traded
})

# ── PMC v1 forward-test filter ────────────────────────────────────────────────
# In-sample backtest (31 trades, 77.4% WR, +0.6124 PnL, PF 2.63) identified
# three conditions that improve selectivity.  Applied ONLY to
# premium_momentum_continuation/v1; all other strategies are unaffected.
# Signals that fail any condition are stored as watch_only for offline analysis.
_PMC_FILTER_MIN_PRICE:  float          = 0.65
_PMC_FILTER_MAX_PRICE:  float          = 0.80
_PMC_FILTER_MAX_GAP_Z:  float          = 2.0   # reject when raw gap_z_score >= this
_PMC_FILTER_SKIP_HOURS: frozenset[int] = frozenset({9, 10})  # weekday ET hours blocked


def run_all(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
    contract_history: Optional[dict[str, list[Tick]]] = None,
    reversal_prob_fn: Optional[Callable[..., dict]] = None,
    timezone_name: str = "America/New_York",
    market_open_time: Optional[datetime] = None,
    market_close_time: Optional[datetime] = None,
) -> list[Signal]:
    """
    Evaluate every registered strategy against the current market state.

    Exceptions in individual strategies are caught and logged so one broken
    rule cannot stall the main loop.  Returns a (possibly empty) list of
    Signal objects for all rules that fired.

    After each strategy fires, time-of-day features are computed from
    signal.recorded_at in `timezone_name` and attached to the Signal so
    they are persisted alongside the signal row.
    """
    signals: list[Signal] = []
    kwargs = dict(
        ticks                  = ticks,
        market_ticker          = market_ticker,
        btc_price              = btc_price,
        target_price           = target_price,
        contract_age_seconds   = contract_age_seconds,
        time_remaining_seconds = time_remaining_seconds,
        contract_prices        = contract_prices,
        contract_history       = contract_history,
        reversal_prob_fn       = reversal_prob_fn,
    )
    for fn in _STRATEGIES:
        try:
            result = fn(**kwargs)
        except Exception:
            logger.exception("Unhandled exception in strategy %s", fn.__name__)
            continue

        if result is None:
            continue

        # ── Attach time-of-day features ───────────────────────────────────────
        try:
            tf = build_time_features(result.recorded_at, tz=timezone_name)
            result.entry_date        = tf["date"]
            result.entry_time_local  = tf["time"]
            result.entry_hour        = tf["hour"]
            result.entry_minute      = tf["minute"]
            result.entry_day_of_week = tf["day_of_week"]
            result.entry_day_name    = tf["day_name"]
            result.entry_is_weekend  = tf["is_weekend"]
            result.entry_15m_block   = tf["block_15m"]
            result.entry_30m_block   = tf["block_30m"]
            result.entry_hour_block  = tf["hour_block"]
            result.market_open_time  = market_open_time
            result.market_close_time = market_close_time
            result.timezone_used     = timezone_name
        except Exception:
            logger.warning(
                "build_time_features failed for signal %s — time fields will be NULL",
                result.rule_name, exc_info=True,
            )

        # Apply watch-only status for paused rule/side combinations.
        # Signals are still inserted into the DB for visibility, but the
        # caller must not open a paper trade when signal_status == "watch_only".
        rule_base = f"{result.rule_name}/{result.rule_version}"
        rule_side = f"{rule_base}/{result.side}"
        if rule_base in _WATCH_ONLY_KEYS or rule_side in _WATCH_ONLY_KEYS:
            result.signal_status = "watch_only"
            logger.debug(
                "Watch-only signal: %s %s (not paper-traded)", rule_base, result.side,
            )

        # ── PMC v1 forward-test filter ─────────────────────────────────────────
        # Check only for premium_momentum_continuation/v1 and only when the
        # signal is not already marked watch_only (e.g. YES-side via _WATCH_ONLY_KEYS).
        if (
            result.rule_name    == _PMC_RULE_NAME
            and result.rule_version == _PMC_RULE_VERSION
            and result.signal_status != "watch_only"
        ):
            _pmc_reject: Optional[str] = None

            # 1. Contract price must be in [0.65, 0.80]
            if not (_PMC_FILTER_MIN_PRICE <= result.contract_price <= _PMC_FILTER_MAX_PRICE):
                _pmc_reject = (
                    f"price_filter: contract_price={result.contract_price:.4f} "
                    f"outside [{_PMC_FILTER_MIN_PRICE}, {_PMC_FILTER_MAX_PRICE}]"
                )

            # 2. Skip weekday 9-10 AM ET
            elif (
                result.entry_hour in _PMC_FILTER_SKIP_HOURS
                and result.entry_is_weekend is False
            ):
                _pmc_reject = (
                    f"time_filter: weekday {result.entry_hour:02d}:xx ET blocked"
                )

            # 3. Reject if raw gap_z_score >= 2 (too stretched, regardless of side)
            elif (
                result.gap_z_score is not None
                and result.gap_z_score >= _PMC_FILTER_MAX_GAP_Z
            ):
                _pmc_reject = (
                    f"gap_z_filter: gap_z_score={result.gap_z_score:+.4f} "
                    f">= {_PMC_FILTER_MAX_GAP_Z}"
                )

            if _pmc_reject is not None:
                result.signal_status = "watch_only"
                logger.info(
                    "PMC v1 forward-test filter: %s %s → watch_only (%s)",
                    rule_base, result.side, _pmc_reject,
                )

        # ── PNMS v2 weekday 9/10 AM ET skip ────────────────────────────────────
        # entry_hour / entry_is_weekend are attached above (not available inside
        # the strategy fn), so the hour skip is applied here.  Signal is still
        # logged for offline analysis but marked watch_only → not paper-traded.
        if (
            result.rule_name    == _PNMS_V2_RULE_NAME
            and result.rule_version == _PNMS_V2_RULE_VERSION
            and result.signal_status != "watch_only"
            and result.entry_hour in _PNMS_V2_SKIP_HOURS
            and result.entry_is_weekend is False
        ):
            result.signal_status = "watch_only"
            logger.info(
                "PNMS v2 hour filter: weekday %02d:xx ET blocked → watch_only",
                result.entry_hour,
            )

        signals.append(result)

    return signals
