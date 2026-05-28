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
from typing import Optional

from app.features import (
    Tick,
    btc_velocity,
    directional_gap as _directional_gap,
    gap as _gap,
    gap_z_score as _gap_z_score,
    momentum_score,
    reversal_score,
    rolling_stds,
)

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


# ── Strategy registry ─────────────────────────────────────────────────────────
#
# Active strategies are evaluated every poll cycle.
# Watch-only keys (see _WATCH_ONLY_KEYS below) are still evaluated and logged
# to the signals table but will NOT open paper trades.

_STRATEGIES = [
    premium_momentum_continuation,   # NO-side active; YES-side watch_only (see below)
    premium_no_midrange_scalp,       # paper_active — NO only, 0.65–0.80
    premium_momentum_scalp_v2,       # watch_only — conflicts with PNMS in 240-300s/NO band
    # cheap_reversal_scalp           # disabled — removed from registry 2026-05-28
]

# Keys that produce signals for the DB log but must NOT open paper trades.
# Format: "rule_name/rule_version"        → all sides paused
#         "rule_name/rule_version/SIDE"   → one side paused
_WATCH_ONLY_KEYS: frozenset[str] = frozenset({
    "premium_momentum_continuation/v1/YES",  # YES-side PMC: watch-only
    "premium_momentum_scalp/v2",             # PMS v2: conflicts with PNMS NO band
})


def run_all(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
) -> list[Signal]:
    """
    Evaluate every registered strategy against the current market state.

    Exceptions in individual strategies are caught and logged so one broken
    rule cannot stall the main loop.  Returns a (possibly empty) list of
    Signal objects for all rules that fired.
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
    )
    for fn in _STRATEGIES:
        try:
            result = fn(**kwargs)
        except Exception:
            logger.exception("Unhandled exception in strategy %s", fn.__name__)
            continue

        if result is None:
            continue

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

        signals.append(result)

    return signals
