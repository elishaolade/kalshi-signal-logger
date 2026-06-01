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

    def __init__(self) -> None:
        self._counts: dict[str, int] = {g: 0 for g in self._GATES}

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
        parts = "  ".join(f"{g}={self._counts[g]}" for g in self._GATES)
        return f"CLC near-miss funnel (since last flush):  {parts}"


# Module-level singleton, instrumented inside the strategy and flushed by main.py.
clc_skip_stats = _CLCSkipStats()


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


# ── Strategy registry ─────────────────────────────────────────────────────────
#
# Active strategies are evaluated every poll cycle.
# Watch-only keys (see _WATCH_ONLY_KEYS below) are still evaluated and logged
# to the signals table but will NOT open paper trades.

_STRATEGIES = [
    premium_momentum_continuation,        # NO-side active; YES-side watch_only (see below)
    premium_no_midrange_scalp,            # paper_active — NO only, 0.65–0.80
    premium_momentum_scalp_v2,            # watch_only — conflicts with PNMS in 240-300s/NO band
    early_overextension_reversal_scalp,   # watch_only research — never paper-traded
    cheap_losing_contract_reversal_trail, # watch_only research — never paper-traded
    # cheap_reversal_scalp                # disabled — removed from registry 2026-05-28
]

# Keys that produce signals for the DB log but must NOT open paper trades.
# Format: "rule_name/rule_version"        → all sides paused
#         "rule_name/rule_version/SIDE"   → one side paused
_WATCH_ONLY_KEYS: frozenset[str] = frozenset({
    "premium_momentum_continuation/v1/YES",  # YES-side PMC: watch-only
    "premium_momentum_scalp/v2",             # PMS v2: conflicts with PNMS NO band
    "early_overextension_reversal_scalp/v1", # research only — outcomes tracked, not traded
    "cheap_losing_contract_reversal_trail/v1", # research only — 5 exit profiles simulated, not traded
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

        signals.append(result)

    return signals
