"""
exit_simulator.py — Pure, lookahead-safe exit simulation for backtesting.

Given a single entry and the *forward* bid path that followed it, this simulates
how a given exit profile would have resolved.  It is a side-effect-free mirror
of the live CLCReversalTracker exit logic (fixed TP/SL, ride-then-trail with a
hard stop + trailing pullback, violent-volatility safety exit, timeout and
near-expiry terminals), extracted so the backtester and the live tracker share
identical exit semantics.

Fill model (NOT mid)
--------------------
    entry_price_simulated = ask + entry_slippage      (computed by the caller)
    exit_price_simulated  = bid - exit_slippage        (computed here)
    pnl                   = exit_sim - entry_sim

No lookahead
------------
`simulate_exit` only ever walks the path *forward* from the entry.  Each
PathPoint must have ts strictly after the entry; the caller is responsible for
slicing the path so it contains only post-entry observations.  Exit decisions at
point i use solely points 0..i (peak/low watermarks are running maxima/minima),
so the simulator can never "see" a future price when deciding to exit.

PAPER-ONLY RESEARCH.  Nothing here trades or touches the DB.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Terminal / intrinsic exit reasons (kept identical to CLCReversalTracker).
TIMEOUT_S      = 60.0
NEAR_EXPIRY_S  = 30.0
VIOLENT_BE_EPS = 0.01   # realized pnl at or below this is "not safe" under violent vol


@dataclass(frozen=True)
class ExitProfile:
    """One exit policy.  `kind` is 'fixed' (percent), 'trail', or 'fixed_abs'."""
    name:                 str
    kind:                 str
    tp_pct:               Optional[float] = None   # fixed take-profit (frac of entry_sim)
    sl_pct:               Optional[float] = None   # fixed stop-loss   (frac of entry_sim)
    hard_stop_pct:        Optional[float] = None   # trail hard stop   (frac of entry_sim)
    trail_activation_pct: Optional[float] = None   # trail arm level   (frac of entry_sim)
    trail_cents:          Optional[float] = None   # trail distance    (dollars of bid)
    # fixed_abs profiles use absolute dollar take-profit / stop-loss thresholds
    # (cents of contract price), e.g. tp_abs=0.04 = exit on a +4¢ move.
    tp_abs:               Optional[float] = None   # absolute take-profit (dollars)
    sl_abs:               Optional[float] = None   # absolute stop-loss   (dollars)
    timeout_s:            Optional[float] = None   # per-profile timeout override (seconds)


# ── The three profiles for the first backtest target ───────────────────────────
#
#   1. fixed +20% / -15% stop
#   2. ride-then-trail: arm at +20%, exit on a 2¢ pullback from peak
#   3. ride-then-trail: arm at +15%, exit on a 1¢ pullback from peak
#
# Profiles 1 and 2 match CLCReversalTracker's fixed_20pct_stop_15pct and
# ride_then_trail_20pct_2c exactly.  Profile 3 (arm +15% / 1¢) is new here; it
# uses a -12% hard stop to match the tracker's +15% trail family
# (ride_then_trail_15pct_2c), so only the pullback distance differs from a
# tracked profile.
FIXED_20_15      = ExitProfile("fixed_20pct_stop_15pct",   "fixed",
                               tp_pct=0.20, sl_pct=0.15)
TRAIL_20PCT_2C   = ExitProfile("ride_then_trail_20pct_2c", "trail",
                               hard_stop_pct=0.15, trail_activation_pct=0.20, trail_cents=0.02)
TRAIL_15PCT_1C   = ExitProfile("ride_then_trail_15pct_1c", "trail",
                               hard_stop_pct=0.12, trail_activation_pct=0.15, trail_cents=0.01)

BACKTEST_PROFILES: dict[str, ExitProfile] = {
    p.name: p for p in (FIXED_20_15, TRAIL_20PCT_2C, TRAIL_15PCT_1C)
}

# Default comparison set requested for the first backtest.
DEFAULT_PROFILE_NAMES: list[str] = [FIXED_20_15.name, TRAIL_20PCT_2C.name, TRAIL_15PCT_1C.name]


# ── Absolute-cent profiles for the follow-through hypothesis test ──────────────
#
#   fixed_4c_stop_6c : take_profit +0.04, stop_loss -0.06, timeout 60s
#   fixed_5c_stop_6c : take_profit +0.05, stop_loss -0.06, timeout 60s
#
# These use ABSOLUTE dollar thresholds (cents of contract price), unlike the
# percent-of-entry profiles above.  Simulated via simulate_exit_fixed_abs.
FIXED_4C_STOP_6C = ExitProfile("fixed_4c_stop_6c", "fixed_abs",
                               tp_abs=0.04, sl_abs=0.06, timeout_s=60.0)
FIXED_5C_STOP_6C = ExitProfile("fixed_5c_stop_6c", "fixed_abs",
                               tp_abs=0.05, sl_abs=0.06, timeout_s=60.0)

FOLLOWTHROUGH_PROFILES: dict[str, ExitProfile] = {
    p.name: p for p in (FIXED_4C_STOP_6C, FIXED_5C_STOP_6C)
}
FOLLOWTHROUGH_PROFILE_NAMES: list[str] = [FIXED_4C_STOP_6C.name, FIXED_5C_STOP_6C.name]


@dataclass
class PathPoint:
    """One post-entry observation of the bought (losing) side."""
    elapsed:        float   # seconds since entry
    bid:            float   # losing-side bid at this point
    time_remaining: float   # seconds until market expiry at this point
    vol_regime:     str     # calm | normal | elevated | violent | unknown


@dataclass
class ExitResult:
    exit_profile:    str
    exit_reason:     str
    exit_bid:        float
    exit_price_simulated: float
    entry_price_simulated: float
    pnl:             float
    pnl_percent:     Optional[float]
    peak_bid:        float
    max_favorable_excursion: float
    max_adverse_excursion:   float
    time_to_peak:    Optional[float]
    trail_activated: Optional[bool]   # None for fixed profiles
    n_updates:       int


def simulate_exit(
    *,
    entry_sim: float,
    entry_bid: float,
    exit_sub: float,
    path: list[PathPoint],
    profile: ExitProfile,
    timeout_s: float = TIMEOUT_S,
    near_expiry_s: float = NEAR_EXPIRY_S,
    violent_be_eps: float = VIOLENT_BE_EPS,
) -> ExitResult:
    """
    Simulate one exit profile over the forward bid `path`.

    entry_sim : entry fill (ask + entry slippage), already computed.
    entry_bid : losing-side bid at entry; the watermark baseline.
    exit_sub  : exit slippage subtracted from the bid on exit.
    path      : forward observations (ts strictly after entry), in time order.

    Returns an ExitResult.  The exit reason is one of:
        take_profit | stop_loss | hard_stop | trailing_stop |
        violent_vol_exit | timeout | near_expiry | end_of_data
    """
    peak_bid     = entry_bid
    low_bid      = entry_bid
    time_to_peak: Optional[float] = None
    trail_active = False
    n_updates    = 0

    closed       = False
    exit_reason: Optional[str] = None
    exit_bid:    Optional[float] = None

    for pt in path:
        n_updates += 1
        bid = pt.bid

        # Running watermarks (use only points seen so far — no lookahead).
        if bid > peak_bid:
            peak_bid     = round(bid, 4)
            time_to_peak = round(pt.elapsed, 3)
        if bid < low_bid:
            low_bid = round(bid, 4)

        exit_pnl = (bid - exit_sub) - entry_sim

        # 0. Violent-volatility safety exit (negative / near break-even).
        if pt.vol_regime == "violent" and exit_pnl <= violent_be_eps:
            closed, exit_reason, exit_bid = True, "violent_vol_exit", round(bid, 4)
        elif profile.kind == "fixed":
            if bid >= entry_sim * (1.0 + profile.tp_pct):
                closed, exit_reason, exit_bid = True, "take_profit", round(bid, 4)
            elif bid <= entry_sim * (1.0 - profile.sl_pct):
                closed, exit_reason, exit_bid = True, "stop_loss", round(bid, 4)
        else:  # trail
            if not trail_active:
                if bid <= entry_sim * (1.0 - profile.hard_stop_pct):
                    closed, exit_reason, exit_bid = True, "hard_stop", round(bid, 4)
                elif bid >= entry_sim * (1.0 + profile.trail_activation_pct):
                    trail_active = True   # arm; exit decided on a later pullback
            else:
                if bid <= peak_bid - profile.trail_cents:
                    closed, exit_reason, exit_bid = True, "trailing_stop", round(bid, 4)

        if closed:
            break

        # Terminal conditions, checked after intrinsic exits (mirrors the live
        # tracker: _apply_update first, then timeout / near-expiry finalize).
        if pt.elapsed >= timeout_s:
            closed, exit_reason, exit_bid = True, "timeout", round(bid, 4)
            break
        if pt.time_remaining <= near_expiry_s:
            closed, exit_reason, exit_bid = True, "near_expiry", round(bid, 4)
            break

    # Path exhausted without an exit (e.g. data ended before 60s / expiry).
    if not closed:
        last_bid = round(path[-1].bid, 4) if path else round(entry_bid, 4)
        exit_reason, exit_bid = "end_of_data", last_bid

    exit_sim = round(exit_bid - exit_sub, 4)
    pnl      = round(exit_sim - entry_sim, 4)
    pnl_pct  = round(pnl / entry_sim, 4) if entry_sim else None
    mfe      = round((peak_bid - exit_sub) - entry_sim, 4)
    mae      = round((low_bid  - exit_sub) - entry_sim, 4)

    return ExitResult(
        exit_profile          = profile.name,
        exit_reason           = exit_reason,
        exit_bid              = exit_bid,
        exit_price_simulated  = exit_sim,
        entry_price_simulated = entry_sim,
        pnl                   = pnl,
        pnl_percent           = pnl_pct,
        peak_bid              = peak_bid,
        max_favorable_excursion = mfe,
        max_adverse_excursion   = mae,
        time_to_peak          = time_to_peak,
        trail_activated       = (trail_active if profile.kind == "trail" else None),
        n_updates             = n_updates,
    )


def simulate_exit_fixed_abs(
    *,
    entry_sim: float,
    entry_bid: float,
    exit_sub: float,
    path: list[PathPoint],
    profile: ExitProfile,
    timeout_s: float = TIMEOUT_S,
    near_expiry_s: float = NEAR_EXPIRY_S,
) -> ExitResult:
    """
    Simulate an absolute-cent fixed exit (take-profit / stop-loss / timeout).

    Unlike `simulate_exit` (percent-of-entry, with violent-vol + trail logic),
    this is a clean fixed-dollar policy used by the follow-through hypothesis:
        take_profit  when bid >= entry_sim + profile.tp_abs
        stop_loss    when bid <= entry_sim - profile.sl_abs
        timeout      when elapsed >= profile.timeout_s (or timeout_s default)
        near_expiry  defensive terminal at near_expiry_s remaining
        end_of_data  path exhausted before any terminal

    No lookahead: the decision at point i uses only points 0..i; the exit fill
    is bid - exit_sub (never mid).  Volatility is an ENTRY filter for this
    hypothesis, so no mid-trade violent exit is applied here.
    """
    if profile.kind != "fixed_abs" or profile.tp_abs is None or profile.sl_abs is None:
        raise ValueError(f"simulate_exit_fixed_abs requires a fixed_abs profile with "
                         f"tp_abs/sl_abs; got {profile!r}")

    eff_timeout = profile.timeout_s if profile.timeout_s is not None else timeout_s
    tp_level = entry_sim + profile.tp_abs
    sl_level = entry_sim - profile.sl_abs

    peak_bid     = entry_bid
    low_bid      = entry_bid
    time_to_peak: Optional[float] = None
    n_updates    = 0

    closed       = False
    exit_reason: Optional[str] = None
    exit_bid:    Optional[float] = None

    for pt in path:
        n_updates += 1
        bid = pt.bid

        if bid > peak_bid:
            peak_bid     = round(bid, 4)
            time_to_peak = round(pt.elapsed, 3)
        if bid < low_bid:
            low_bid = round(bid, 4)

        # Intrinsic exits (decision on raw bid; fill applies slippage on exit).
        if bid >= tp_level:
            closed, exit_reason, exit_bid = True, "take_profit", round(bid, 4)
        elif bid <= sl_level:
            closed, exit_reason, exit_bid = True, "stop_loss", round(bid, 4)

        if closed:
            break

        # Terminal conditions, checked after intrinsic exits.
        if pt.elapsed >= eff_timeout:
            closed, exit_reason, exit_bid = True, "timeout", round(bid, 4)
            break
        if pt.time_remaining <= near_expiry_s:
            closed, exit_reason, exit_bid = True, "near_expiry", round(bid, 4)
            break

    if not closed:
        last_bid = round(path[-1].bid, 4) if path else round(entry_bid, 4)
        exit_reason, exit_bid = "end_of_data", last_bid

    exit_sim = round(exit_bid - exit_sub, 4)
    pnl      = round(exit_sim - entry_sim, 4)
    pnl_pct  = round(pnl / entry_sim, 4) if entry_sim else None
    mfe      = round((peak_bid - exit_sub) - entry_sim, 4)
    mae      = round((low_bid  - exit_sub) - entry_sim, 4)

    return ExitResult(
        exit_profile          = profile.name,
        exit_reason           = exit_reason,
        exit_bid              = exit_bid,
        exit_price_simulated  = exit_sim,
        entry_price_simulated = entry_sim,
        pnl                   = pnl,
        pnl_percent           = pnl_pct,
        peak_bid              = peak_bid,
        max_favorable_excursion = mfe,
        max_adverse_excursion   = mae,
        time_to_peak          = time_to_peak,
        trail_activated       = None,
        n_updates             = n_updates,
    )
