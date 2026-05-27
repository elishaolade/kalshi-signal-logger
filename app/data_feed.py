"""
data_feed.py

Real data:
  - Kraken public REST API for BTC/USD spot price (no auth needed)

Mock data (active until live Kalshi endpoints are wired up):
  - BTC price random walk, seeded from Kraken when available
  - 15-minute BTC up/down market aligned to the current clock window
  - Binary contract bid/ask priced from a probability model (not Kalshi)
"""

import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Kraken config ─────────────────────────────────────────────────────────────

_KRAKEN_URL = "https://api.kraken.com/0/public/Ticker"
_KRAKEN_PAIR = "XBTUSD"
_KRAKEN_RESULT_KEY = "XXBTZUSD"     # Kraken's canonical pair name in the response

# ── Module-level mock state ───────────────────────────────────────────────────

# Random-walk price; seeded from Kraken on first successful fetch, then drifts.
# Step size (~$7) is calibrated to BTC's roughly $500/hour volatility at 2s polling.
_mock_btc_price: float = 67_000.0
_WALK_STEP_STDDEV = 7.0
_WALK_MIN = 20_000.0
_WALK_MAX = 200_000.0


# ── Internal helpers ──────────────────────────────────────────────────────────

def _advance_mock_price(anchor: Optional[float] = None) -> float:
    """Move the mock BTC price one step along a random walk."""
    global _mock_btc_price
    if anchor is not None:
        _mock_btc_price = anchor
    else:
        _mock_btc_price += random.gauss(0, _WALK_STEP_STDDEV)
        _mock_btc_price = max(_WALK_MIN, min(_WALK_MAX, _mock_btc_price))
    return round(_mock_btc_price, 2)


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via the complementary error function."""
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _yes_probability(
    btc_price: float,
    target_price: float,
    time_remaining_seconds: float,
) -> float:
    """
    Estimate P(BTC closes above target) using a simplified log-normal model.

    Approach: treat the remaining window as a diffusion process with constant
    hourly vol.  The z-score of the gap relative to expected movement gives the
    probability via the normal CDF.

    Hourly vol ≈ $500 (rough BTC average; tune via config if needed).
    """
    gap = btc_price - target_price
    hourly_vol = 500.0
    t_hours = max(time_remaining_seconds, 1.0) / 3600.0
    sigma = hourly_vol * math.sqrt(t_hours)
    sigma = max(sigma, 1.0)          # floor avoids division issues near expiry
    z = gap / sigma
    raw_prob = _normal_cdf(z)

    # Small Gaussian noise keeps prices from being perfectly deterministic
    noise = random.gauss(0, 0.005)
    return max(0.01, min(0.99, raw_prob + noise))


def _make_side(mid: float, spread: float) -> dict[str, float]:
    """Build a bid/ask quote around a mid-price with the given spread."""
    half = spread / 2.0
    bid  = round(max(0.01, mid - half), 4)
    ask  = round(min(0.99, mid + half), 4)
    # last price: random fill somewhere inside the spread
    last = round(bid + random.uniform(0, ask - bid), 4)
    return {
        "bid_price":  bid,
        "ask_price":  ask,
        "mid_price":  round((bid + ask) / 2.0, 4),
        "last_price": last,
        "spread":     round(ask - bid, 4),
    }


def _current_15min_window() -> tuple[datetime, datetime]:
    """Return (open_time, close_time) for the active 15-minute clock window."""
    now = datetime.now(timezone.utc)
    boundary_minute = (now.minute // 15) * 15
    open_time  = now.replace(minute=boundary_minute, second=0, microsecond=0)
    close_time = open_time + timedelta(minutes=15)
    return open_time, close_time


# ── Public API ────────────────────────────────────────────────────────────────

def get_btc_price() -> float:
    """
    Return current BTC/USD price.

    Tries the Kraken public ticker API first (no credentials required).
    On any failure, falls back to the module-level mock random walk so the
    rest of the system keeps running during development.
    """
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(_KRAKEN_URL, params={"pair": _KRAKEN_PAIR})
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                raise ValueError(f"Kraken API error: {data['error']}")
            # 'c' field is [last_trade_price, lot_volume]
            price_str = data["result"][_KRAKEN_RESULT_KEY]["c"][0]
            price = float(price_str)
            logger.debug("Kraken BTC/USD: %.2f", price)
            return _advance_mock_price(anchor=price)   # keep mock in sync

    except Exception as exc:
        fallback = _advance_mock_price()
        logger.warning(
            "Kraken fetch failed (%s) — using mock BTC price %.2f",
            exc, fallback,
        )
        return fallback


def get_active_mock_market() -> dict:
    """
    Return a fake 15-minute BTC up/down market aligned to the current clock.

    The target price is the opening BTC price rounded to the nearest $500.
    The ticker encodes the window date, start time, and target so it is
    unique per window.

    Returns:
        {
            "market_ticker":          str,
            "target_price":           float,
            "open_time":              datetime (UTC, tz-aware),
            "close_time":             datetime (UTC, tz-aware),
            "time_remaining_seconds": float,
            "contract_age_seconds":   float,
        }
    """
    open_time, close_time = _current_15min_window()
    now = datetime.now(timezone.utc)

    # Snap target to the nearest $500 — typical Kalshi strike granularity
    btc_now = get_btc_price()
    target_price = round(btc_now / 500) * 500

    time_remaining  = max(0.0, (close_time - now).total_seconds())
    contract_age    = (now - open_time).total_seconds()

    # Ticker format: KXBTC-YYMMDD-HHMM-T<target>
    # e.g.  KXBTC-250527-1415-T97500
    date_part   = open_time.strftime("%y%m%d")
    time_part   = open_time.strftime("%H%M")
    market_ticker = f"KXBTC-{date_part}-{time_part}-T{int(target_price)}"

    logger.debug(
        "Mock market: %s  BTC=%.2f  target=%.2f  remaining=%.0fs",
        market_ticker, btc_now, target_price, time_remaining,
    )

    return {
        "market_ticker":          market_ticker,
        "target_price":           float(target_price),
        "open_time":              open_time,
        "close_time":             close_time,
        "time_remaining_seconds": time_remaining,
        "contract_age_seconds":   contract_age,
    }


def get_mock_contract_prices(
    btc_price: float,
    target_price: float,
    time_remaining_seconds: float,
) -> dict[str, dict[str, float]]:
    """
    Return simulated YES and NO bid/ask quotes for a BTC up/down contract.

    Pricing logic:
      - P(YES) is estimated from the gap between BTC and target using the
        normal CDF, scaled by time-weighted volatility (fewer seconds left →
        less uncertainty → price converges toward 0 or 1).
      - P(NO) = 1 - P(YES).
      - Each side gets an independent random spread of 1–3 cents.
      - Small Gaussian noise is added so prices are never perfectly static.

    Args:
        btc_price:              Current BTC/USD spot price.
        target_price:           Contract strike (the "will BTC close above X?" level).
        time_remaining_seconds: Seconds until contract expiry.

    Returns:
        {
            "YES": {"bid_price", "ask_price", "mid_price", "last_price", "spread"},
            "NO":  {"bid_price", "ask_price", "mid_price", "last_price", "spread"},
        }
        All prices are in [0.01, 0.99] as dollar fractions (multiply by 100 for cents).
    """
    yes_prob = _yes_probability(btc_price, target_price, time_remaining_seconds)
    no_prob  = 1.0 - yes_prob

    # Independent spreads per side: 1–3 cents (0.01–0.03 in dollar fraction)
    yes_spread = random.uniform(0.01, 0.03)
    no_spread  = random.uniform(0.01, 0.03)

    yes_side = _make_side(yes_prob, yes_spread)
    no_side  = _make_side(no_prob,  no_spread)

    logger.debug(
        "Contract prices — BTC=%.2f target=%.2f t=%.0fs | "
        "YES mid=%.4f  NO mid=%.4f",
        btc_price, target_price, time_remaining_seconds,
        yes_side["mid_price"], no_side["mid_price"],
    )

    return {"YES": yes_side, "NO": no_side}
