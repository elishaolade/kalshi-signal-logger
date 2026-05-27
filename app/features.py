"""
features.py — Pure feature computation for BTC up/down signal detection.

All functions are side-effect-free and take only plain Python values so they
are straightforward to unit-test without mocking anything.

Tick namedtuple
---------------
    Tick(price: float, ts: float)   # ts = Unix epoch seconds

reference_ts
------------
Functions that compute rolling windows accept an optional `reference_ts`.
When omitted, the last tick's timestamp is used as "now".  Pass a fixed
value in tests to make results fully deterministic.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import NamedTuple, Optional


# ── Data type ─────────────────────────────────────────────────────────────────

class Tick(NamedTuple):
    price: float
    ts: float       # Unix epoch seconds


# ── Internal helpers ──────────────────────────────────────────────────────────

def _window(
    ticks: list[Tick],
    seconds: float,
    reference_ts: Optional[float] = None,
) -> list[Tick]:
    """Return ticks whose ts >= (reference_ts - seconds)."""
    if not ticks:
        return []
    ref = reference_ts if reference_ts is not None else ticks[-1].ts
    cutoff = ref - seconds
    return [t for t in ticks if t.ts >= cutoff]


def _ols_slope(xs: list[float], ys: list[float]) -> float:
    """Ordinary-least-squares slope (dy/dx) for parallel lists."""
    n = len(xs)
    xm = sum(xs) / n
    ym = sum(ys) / n
    num = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    den = sum((x - xm) ** 2 for x in xs)
    return num / den if den != 0.0 else 0.0


# ── Rolling standard deviation ────────────────────────────────────────────────

def rolling_std(
    ticks: list[Tick],
    window_seconds: float,
    reference_ts: Optional[float] = None,
) -> Optional[float]:
    """
    Sample standard deviation of BTC prices over the last `window_seconds`.
    Returns None when fewer than 2 ticks fall within the window (not enough
    data to compute a meaningful spread).
    """
    w = _window(ticks, window_seconds, reference_ts)
    if len(w) < 2:
        return None
    return statistics.stdev(t.price for t in w)


def rolling_stds(
    ticks: list[Tick],
    reference_ts: Optional[float] = None,
) -> dict[str, Optional[float]]:
    """
    Convenience wrapper: return all three standard windows at once.

    Returns:
        {"std_30s": float|None, "std_60s": float|None, "std_120s": float|None}
    """
    return {
        "std_30s":  rolling_std(ticks,  30.0, reference_ts),
        "std_60s":  rolling_std(ticks,  60.0, reference_ts),
        "std_120s": rolling_std(ticks, 120.0, reference_ts),
    }


# ── BTC velocity ──────────────────────────────────────────────────────────────

def btc_velocity(
    ticks: list[Tick],
    window_seconds: float = 30.0,
    reference_ts: Optional[float] = None,
) -> Optional[float]:
    """
    BTC price velocity in dollars per second over the last `window_seconds`.

    Uses OLS slope (price regressed on timestamp) rather than a simple
    endpoint difference so that a single outlier tick does not dominate.
    Returns None when fewer than 2 ticks are available.

    Positive → price rising.  Negative → price falling.
    """
    w = _window(ticks, window_seconds, reference_ts)
    if len(w) < 2:
        return None
    return _ols_slope([t.ts for t in w], [t.price for t in w])


# ── Momentum score ────────────────────────────────────────────────────────────

def momentum_score(ticks: list[Tick], n: int = 10) -> float:
    """
    Directional tick count over the last `n` ticks.

      +1 for each up-tick   (price[i] > price[i-1])
      -1 for each down-tick (price[i] < price[i-1])
       0 for unchanged

    Range: [-(n-1), +(n-1)].  Returns 0.0 when fewer than 2 ticks exist.
    """
    sample = ticks[-n:] if len(ticks) >= n else ticks
    if len(sample) < 2:
        return 0.0
    score = 0.0
    for i in range(1, len(sample)):
        delta = sample[i].price - sample[i - 1].price
        if delta > 0:
            score += 1.0
        elif delta < 0:
            score -= 1.0
    return score


# ── Reversal score ────────────────────────────────────────────────────────────

def reversal_score(ticks: list[Tick], side: str, n: int = 10) -> float:
    """
    Side-aware momentum: positive means price is moving toward this side's
    resolution.

    YES → positive when BTC is ticking upward (toward / past target).
    NO  → positive when BTC is ticking downward (away from / below target).

    Computed as: momentum_score * direction(side).
    Same magnitude as momentum_score; only the sign is flipped for NO.
    """
    raw = momentum_score(ticks, n)
    return raw if side.upper() == "YES" else -raw


# ── Gap and directional gap ───────────────────────────────────────────────────

def gap(btc_price: float, target_price: float) -> float:
    """
    Signed distance from target.

        gap = btc_price - target_price

    Positive → BTC above target.  Negative → BTC below target.
    """
    return btc_price - target_price


def directional_gap(btc_price: float, target_price: float, side: str) -> float:
    """
    Gap oriented so that positive always means "favorable for this side."

    YES: btc_price - target_price  (positive → BTC above target → YES winning)
    NO:  target_price - btc_price  (positive → BTC below target → NO winning)
    """
    raw = btc_price - target_price
    return raw if side.upper() == "YES" else -raw


# ── Z-scores ──────────────────────────────────────────────────────────────────

def z_from_target(
    btc_price: float,
    target_price: float,
    side: str,
    std: Optional[float],
) -> Optional[float]:
    """
    How many standard deviations the directional gap is from zero.

        z = directional_gap(side) / rolling_std

    Returns None when std is None or zero (flat price or insufficient data).
    A large positive z means the current side is comfortably in the money.
    """
    if not std:
        return None
    return directional_gap(btc_price, target_price, side) / std


def gap_z_score(
    btc_price: float,
    target_price: float,
    ticks: list[Tick],
    window_seconds: float = 60.0,
    reference_ts: Optional[float] = None,
) -> Optional[float]:
    """
    Z-score of the raw (undirected) gap normalised by rolling volatility.

        gap_z_score = (btc_price - target_price) / rolling_std(window_seconds)

    Positive → BTC above target regardless of side.
    Returns None when rolling_std is unavailable or zero.
    """
    std = rolling_std(ticks, window_seconds, reference_ts)
    if not std:
        return None
    return gap(btc_price, target_price) / std


# ── Demo / smoke-test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time as _time

    random.seed(42)
    TARGET = 67_100.0
    now = _time.time()

    def _make_ticks(
        n: int,
        start: float,
        drift_per_tick: float,
        noise_std: float,
        start_ts: Optional[float] = None,
    ) -> list[Tick]:
        base_ts = start_ts if start_ts is not None else now - n
        return [
            Tick(
                price=round(start + i * drift_per_tick + random.gauss(0, noise_std), 2),
                ts=base_ts + i,
            )
            for i in range(n)
        ]

    def _print_features(label: str, ticks: list[Tick]) -> None:
        btc   = ticks[-1].price
        stds  = rolling_stds(ticks)
        vel   = btc_velocity(ticks, 30.0)
        mom   = momentum_score(ticks, n=10)

        print(f"\n{'=' * 56}")
        print(f"  {label}")
        print(f"{'=' * 56}")
        print(f"  BTC now       : ${btc:>11,.2f}")
        print(f"  Target        : ${TARGET:>11,.2f}")
        print(f"  Gap           : {gap(btc, TARGET):>+11.2f}")
        print(f"  Dir gap YES   : {directional_gap(btc, TARGET, 'YES'):>+11.2f}")
        print(f"  Dir gap NO    : {directional_gap(btc, TARGET, 'NO'):>+11.2f}")

        print()
        for label_w, key in [("30s", "std_30s"), ("60s", "std_60s"), ("120s", "std_120s")]:
            val = stds[key]
            print(
                f"  Rolling std {label_w} : {val:>8.4f}"
                if val is not None else
                f"  Rolling std {label_w} : N/A (need ≥2 ticks in window)"
            )

        print()
        print(
            f"  Velocity 30s  : {vel:>+10.4f} $/s"
            if vel is not None else
            "  Velocity 30s  : N/A"
        )

        print()
        print(f"  Momentum (n=10)  : {mom:>+6.1f}   (range ±9)")
        print(f"  Reversal YES     : {reversal_score(ticks, 'YES'):>+6.1f}")
        print(f"  Reversal NO      : {reversal_score(ticks, 'NO'):>+6.1f}")

        print()
        for label_w, w in [("30s", 30.0), ("60s", 60.0), ("120s", 120.0)]:
            std  = rolling_std(ticks, w)
            z_y  = z_from_target(btc, TARGET, "YES", std)
            z_n  = z_from_target(btc, TARGET, "NO",  std)
            if z_y is not None:
                print(f"  z_from_target YES ({label_w}): {z_y:>+8.4f}")
                print(f"  z_from_target NO  ({label_w}): {z_n:>+8.4f}")
            else:
                print(f"  z_from_target     ({label_w}): N/A")

        print()
        gz = gap_z_score(btc, TARGET, ticks, 60.0)
        print(
            f"  Gap z-score (60s): {gz:>+8.4f}"
            if gz is not None else
            "  Gap z-score (60s): N/A"
        )

    # ── Scenario A: BTC drifting UP through target ────────────────────────────
    # 130 ticks, 1/second, gentle +3 $/tick drift starting from $66,900
    ticks_up = _make_ticks(130, start=66_900.0, drift_per_tick=3.0, noise_std=20.0)
    _print_features("Scenario A — BTC trending UP through target", ticks_up)

    # ── Scenario B: BTC drifting DOWN away from target ────────────────────────
    ticks_down = _make_ticks(130, start=67_300.0, drift_per_tick=-3.0, noise_std=20.0)
    _print_features("Scenario B — BTC trending DOWN away from target", ticks_down)

    # ── Scenario C: flat/choppy BTC right at target ───────────────────────────
    ticks_flat = _make_ticks(130, start=67_100.0, drift_per_tick=0.0, noise_std=8.0)
    _print_features("Scenario C — BTC choppy at target", ticks_flat)

    # ── Scenario D: edge case — only 1 tick ───────────────────────────────────
    one_tick = [Tick(price=67_050.0, ts=now)]
    print(f"\n{'=' * 56}")
    print("  Scenario D — only 1 tick (all rolling values should be None/0)")
    print(f"{'=' * 56}")
    print(f"  rolling_std(30s) : {rolling_std(one_tick, 30.0)}")
    print(f"  btc_velocity     : {btc_velocity(one_tick, 30.0)}")
    print(f"  momentum_score   : {momentum_score(one_tick)}")
    print(f"  gap_z_score      : {gap_z_score(67_050.0, TARGET, one_tick, 60.0)}")
