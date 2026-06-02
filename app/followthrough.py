"""
followthrough.py — Pure, lookahead-safe follow-through feature helpers.

Hypothesis support module for the follow-through filter test
(scripts/followthrough_backtest.py).  These functions answer one question:

    Is a NO-side scalp entry confirmed by *price follow-through* — i.e. the NO
    contract price keeps moving in the bought direction (up) or holds most of
    its recent gain — rather than spiking once and fading?

All helpers operate on a per-side contract-price history `list[Tick]` (price,
ts) accumulated up to the entry timestamp.  They NEVER look past `now_ts`, so
they are safe to use inside a lookahead-free backtest.

PAPER-ONLY / BACKTEST-ONLY RESEARCH.  Nothing here trades, places orders, or
touches the DB.  This is a hypothesis test, not a trade recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from app.features import Tick

# ── Follow-through confirmation thresholds (NO side) ───────────────────────────
FT_MIN_CHANGE_10S        = 0.01   # NO price must be up >= +1c over the last 10s
FT_MIN_CHANGE_30S        = 0.02   # NO price must be up >= +2c over the last 30s
FT_MAX_PULLBACK          = 0.01   # NO price within 1c of its 30s recent high
FT_MAX_SPREAD            = 0.03   # spread cap (mirrors base entry)
FT_CHANGE_SHORT_WINDOW_S = 10.0
FT_CHANGE_LONG_WINDOW_S  = 30.0
FT_RECENT_HIGH_WINDOW_S  = 30.0
FT_PARTICIPATION_WINDOW_S = 60.0

# Participation proxy (used when volume is unavailable / no baseline supplied):
# require at least this many NO quote updates in the trailing 60s.
FT_MIN_QUOTE_UPDATES_60S = 6

# scalping_valid_window heuristic: weekday hours known to be thin/erratic in
# prior research (matches the live PMC / PNMS v2 9–10 AM ET skip).
SCALP_SKIP_HOURS: frozenset[int] = frozenset({9, 10})


# ── Low-level history accessors (lookahead-safe) ───────────────────────────────

def _value_at_or_before(history: Sequence[Tick], ts: float) -> Optional[float]:
    """Most recent price with point.ts <= ts (history assumed ascending)."""
    val: Optional[float] = None
    for t in history:
        if t.ts <= ts:
            val = t.price
        else:
            break
    return val


def _window(history: Sequence[Tick], now_ts: float, window_s: float) -> list[Tick]:
    """Points with (now_ts - window_s) <= ts <= now_ts."""
    lo = now_ts - window_s
    return [t for t in history if lo <= t.ts <= now_ts]


def price_change(history: Sequence[Tick], now_ts: float, window_s: float) -> Optional[float]:
    """
    Current price minus the price ~`window_s` ago.  Positive = price rose.
    Returns None if there is no point at/before the lookback instant or no
    current point.
    """
    now_val = _value_at_or_before(history, now_ts)
    past_val = _value_at_or_before(history, now_ts - window_s)
    if now_val is None or past_val is None:
        return None
    return round(now_val - past_val, 4)


def recent_high(history: Sequence[Tick], now_ts: float, window_s: float) -> Optional[float]:
    """Highest price observed in the trailing `window_s` (inclusive of now)."""
    w = _window(history, now_ts, window_s)
    if not w:
        return None
    return round(max(t.price for t in w), 4)


def pullback_from_recent_high(
    history: Sequence[Tick], now_ts: float, window_s: float
) -> Optional[float]:
    """recent_high - current price.  0 = at the high; larger = faded more."""
    hi = recent_high(history, now_ts, window_s)
    now_val = _value_at_or_before(history, now_ts)
    if hi is None or now_val is None:
        return None
    return round(max(0.0, hi - now_val), 4)


def quote_update_count(history: Sequence[Tick], now_ts: float, window_s: float) -> int:
    """Number of quote updates in the trailing `window_s` (participation proxy)."""
    return len(_window(history, now_ts, window_s))


def volume_in_window(
    vol_history: Sequence[tuple[float, Optional[float]]],
    now_ts: float,
    window_s: float,
) -> Optional[float]:
    """
    Traded volume over the trailing `window_s`, derived as the change in the
    (assumed non-decreasing) cumulative `volume` field across NO snapshots.

    vol_history is a list of (ts, cumulative_volume) ascending.  Returns the
    max-minus-min within the window when at least two non-null points exist,
    else None (caller falls back to the quote-update participation proxy).
    """
    pts = [(ts, v) for ts, v in vol_history
           if v is not None and (now_ts - window_s) <= ts <= now_ts]
    if len(pts) < 2:
        return None
    vals = [v for _, v in pts]
    delta = max(vals) - min(vals)
    return round(float(delta), 4) if delta >= 0 else None


def spread_bucket(spread: Optional[float]) -> str:
    """Coarse spread bucket for breakdown reporting."""
    if spread is None:
        return "N/A"
    if spread < 0.01:
        return "<0.01"
    if spread < 0.02:
        return "0.01-0.02"
    if spread < 0.03:
        return "0.02-0.03"
    return "0.03+"


# ── Aggregate follow-through evaluation ────────────────────────────────────────

@dataclass
class FollowThrough:
    """Computed follow-through context for one NO-side base entry."""
    no_price_change_10s:          Optional[float]
    no_price_change_30s:          Optional[float]
    no_recent_high_30s:           Optional[float]
    no_pullback_from_recent_high: Optional[float]
    quote_update_count_60s:       int
    volume_60s:                   Optional[float]
    baseline_volume_60s:          Optional[float]
    spread:                       Optional[float]
    spread_bucket:                str
    volatility_regime:            str
    participation_ok:             bool
    participation_basis:          str            # "volume" | "quote_count"
    followthrough_confirmed:      bool
    followthrough_failed:         bool


def evaluate_followthrough(
    *,
    no_history: Sequence[Tick],
    now_ts: float,
    spread: Optional[float],
    volatility_regime: str,
    vol_history: Optional[Sequence[tuple[float, Optional[float]]]] = None,
    baseline_volume_60s: Optional[float] = None,
    min_quote_updates_60s: int = FT_MIN_QUOTE_UPDATES_60S,
) -> FollowThrough:
    """
    Evaluate NO-side follow-through confirmation for a base entry.

    followthrough_confirmed is True when ALL hold:
      • no_price_change_10s >= FT_MIN_CHANGE_10S
      • no_price_change_30s >= FT_MIN_CHANGE_30S
      • no_pullback_from_recent_high <= FT_MAX_PULLBACK
      • spread <= FT_MAX_SPREAD
      • volatility_regime != "violent"
      • participation_ok

    participation_ok:
      • if a baseline is supplied AND volume_60s is available → volume_60s > baseline
      • otherwise (fallback) → quote_update_count_60s >= min_quote_updates_60s
    """
    chg10 = price_change(no_history, now_ts, FT_CHANGE_SHORT_WINDOW_S)
    chg30 = price_change(no_history, now_ts, FT_CHANGE_LONG_WINDOW_S)
    hi30  = recent_high(no_history, now_ts, FT_RECENT_HIGH_WINDOW_S)
    pull  = pullback_from_recent_high(no_history, now_ts, FT_RECENT_HIGH_WINDOW_S)
    qc60  = quote_update_count(no_history, now_ts, FT_PARTICIPATION_WINDOW_S)
    vol60 = (volume_in_window(vol_history, now_ts, FT_PARTICIPATION_WINDOW_S)
             if vol_history is not None else None)

    if baseline_volume_60s is not None and vol60 is not None:
        participation_ok    = vol60 > baseline_volume_60s
        participation_basis = "volume"
    else:
        participation_ok    = qc60 >= min_quote_updates_60s
        participation_basis = "quote_count"

    confirmed = (
        chg10 is not None and chg10 >= FT_MIN_CHANGE_10S
        and chg30 is not None and chg30 >= FT_MIN_CHANGE_30S
        and pull is not None and pull <= FT_MAX_PULLBACK
        and spread is not None and spread <= FT_MAX_SPREAD
        and volatility_regime != "violent"
        and participation_ok
    )

    return FollowThrough(
        no_price_change_10s          = chg10,
        no_price_change_30s          = chg30,
        no_recent_high_30s           = hi30,
        no_pullback_from_recent_high = pull,
        quote_update_count_60s       = qc60,
        volume_60s                   = vol60,
        baseline_volume_60s          = baseline_volume_60s,
        spread                       = spread,
        spread_bucket                = spread_bucket(spread),
        volatility_regime            = volatility_regime,
        participation_ok             = participation_ok,
        participation_basis          = participation_basis,
        followthrough_confirmed      = confirmed,
        followthrough_failed         = not confirmed,
    )


def scalping_valid_window(
    *,
    entry_hour: Optional[int],
    is_weekend: Optional[bool],
    volatility_regime: str,
    quote_update_count_60s: int,
    min_quote_updates_60s: int = FT_MIN_QUOTE_UPDATES_60S,
) -> bool:
    """
    Heuristic 'is this a scalpable window?' flag (research dimension only).

    True when the market is actively quoted, not violently volatile, and not in
    a known thin weekday block (9–10 AM ET).  Used to answer 'does follow-through
    only work during valid scalping windows?' — never a trade gate.
    """
    if volatility_regime == "violent":
        return False
    if quote_update_count_60s < min_quote_updates_60s:
        return False
    if is_weekend is False and entry_hour in SCALP_SKIP_HOURS:
        return False
    return True
