"""
data_feed.py

Real data:
  - Kraken public REST API for BTC/USD spot price (no auth needed)
  - Kalshi production REST API for market and contract data
    Authenticated via RSA key pair when KALSHI_KEY_ID + KALSHI_KEY_FILE are set.
    Falls back to unauthenticated (public endpoints only) when keys are absent.

Mock data (fallback when Kalshi API is unavailable):
  - BTC price random walk, seeded from Kraken when available
  - 15-minute BTC up/down market aligned to the current clock window
  - Binary contract bid/ask priced from a probability model (not Kalshi)
"""

import base64
import logging
import math
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.config import (
    KALSHI_API_BASE,
    KALSHI_API_TIMEOUT_SECONDS,
    KALSHI_BTC_RANGE_EVENT_TICKER,
    KALSHI_BTC_RANGE_SERIES_TICKER,
    KALSHI_KEY_FILE,
    KALSHI_KEY_ID,
)

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


# ── Kalshi public REST helpers ────────────────────────────────────────────────

def _parse_float(value: Any) -> Optional[float]:
    """Parse an API field that may be numeric, string, empty string, or absent."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse ISO-8601 timestamps returned by the Kalshi REST API."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ── Kalshi RSA authentication ─────────────────────────────────────────────────

# Private key is loaded once at module import and cached.
# If the key file is missing or keys aren't configured, _KALSHI_PRIVATE_KEY
# stays None and all requests are made without auth headers.

_KALSHI_PRIVATE_KEY = None

def _load_private_key():
    """Load the RSA private key from KALSHI_KEY_FILE. Called once at import."""
    global _KALSHI_PRIVATE_KEY
    if not KALSHI_KEY_ID or not KALSHI_KEY_FILE:
        return
    if not os.path.exists(KALSHI_KEY_FILE):
        logger.warning(
            "KALSHI_KEY_FILE=%r not found — requests will be unauthenticated",
            KALSHI_KEY_FILE,
        )
        return
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        with open(KALSHI_KEY_FILE, "rb") as f:
            _KALSHI_PRIVATE_KEY = load_pem_private_key(f.read(), password=None)
        logger.info("Kalshi private key loaded from %s (key_id=%s)", KALSHI_KEY_FILE, KALSHI_KEY_ID)
    except Exception as exc:
        logger.warning("Failed to load Kalshi private key: %s — requests will be unauthenticated", exc)


_load_private_key()


def _kalshi_auth_headers(method: str, path: str) -> dict[str, str]:
    """
    Build Kalshi RSA authentication headers for a single request.

    Kalshi signs: {timestamp_ms}{METHOD}{path}
      timestamp_ms — current Unix time in milliseconds as a string
      METHOD       — uppercase HTTP verb (GET, POST, …)
      path         — URL path only, no host, no query string
                     e.g.  /trade-api/v2/markets

    Signature algorithm: RSA-PSS with SHA-256, DIGEST_LENGTH salt.

    Returns an empty dict when auth is not configured, so unauthenticated
    requests continue to work for public endpoints.
    """
    if _KALSHI_PRIVATE_KEY is None or not KALSHI_KEY_ID:
        return {}

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    message      = (timestamp_ms + method.upper() + path).encode("utf-8")

    signature = _KALSHI_PRIVATE_KEY.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY":       KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


def _kalshi_get(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """
    Single GET against the Kalshi REST API.

    Adds RSA auth headers automatically when KALSHI_KEY_ID and KALSHI_KEY_FILE
    are configured. Falls back to unauthenticated for public endpoints.
    Raises on HTTP errors — callers should catch and log.
    """
    base    = KALSHI_API_BASE.rstrip("/")
    url_path = "/" + path.lstrip("/")
    url     = base + url_path
    headers = _kalshi_auth_headers("GET", url_path)
    with httpx.Client(timeout=KALSHI_API_TIMEOUT_SECONDS) as client:
        resp = client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _collect_markets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Kalshi /markets responses can return a single market or a list."""
    if isinstance(payload.get("market"), dict):
        return [payload["market"]]
    markets = payload.get("markets")
    return markets if isinstance(markets, list) else []


def _normalize_range_market(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    Normalise a Kalshi market dict into the fields used by HourlyRangeTracker.

    Returns None for markets that lack numeric floor_strike / cap_strike (i.e.
    binary up/down markets — not range markets).
    """
    floor_strike = _parse_float(raw.get("floor_strike"))
    cap_strike   = _parse_float(raw.get("cap_strike"))
    if floor_strike is None or cap_strike is None:
        return None

    yes_bid  = _parse_float(raw.get("yes_bid") or raw.get("yes_bid_dollars"))
    yes_ask  = _parse_float(raw.get("yes_ask") or raw.get("yes_ask_dollars"))
    yes_mid: Optional[float] = None
    yes_spread: Optional[float] = None
    if yes_bid is not None and yes_ask is not None:
        yes_mid    = round((yes_bid + yes_ask) / 2, 4)
        yes_spread = round(yes_ask - yes_bid, 4)

    return {
        "ticker":        raw.get("ticker"),
        "event_ticker":  raw.get("event_ticker"),
        "series_ticker": raw.get("series_ticker", ""),
        "title":         raw.get("title") or raw.get("yes_sub_title") or "",
        "floor_strike":  floor_strike,
        "cap_strike":    cap_strike,
        "band_width":    round(cap_strike - floor_strike, 2),
        "band_center":   round((cap_strike + floor_strike) / 2, 2),
        "open_time":     _parse_dt(raw.get("open_time")),
        "close_time":    _parse_dt(raw.get("close_time")),
        "yes_bid":       yes_bid,
        "yes_ask":       yes_ask,
        "yes_mid":       yes_mid,
        "yes_spread":    yes_spread,
        "last_price":    _parse_float(
            raw.get("last_price") or raw.get("last_price_dollars")
            or raw.get("previous_price_dollars")
        ),
        "volume":    _parse_float(raw.get("volume")    or raw.get("volume_dollars")    or raw.get("volume_fp")),
        "liquidity": _parse_float(raw.get("liquidity") or raw.get("liquidity_dollars") or raw.get("liquidity_fp")),
    }


def get_kalshi_markets(
    *,
    event_ticker:  Optional[str] = None,
    series_ticker: Optional[str] = None,
    status: str = "open",
    limit:  int = 1000,
) -> list[dict[str, Any]]:
    """
    Fetch raw Kalshi market dicts, handling cursor-based pagination.

    Raises on network or HTTP errors — callers should catch and log.
    """
    params: dict[str, Any] = {"limit": limit, "status": status}
    if event_ticker:
        params["event_ticker"] = event_ticker
    if series_ticker:
        params["series_ticker"] = series_ticker

    cursor: Optional[str] = None
    out: list[dict[str, Any]] = []
    while True:
        page = dict(params)
        if cursor:
            page["cursor"] = cursor
        payload = _kalshi_get("/markets", params=page)
        out.extend(_collect_markets(payload))
        cursor = payload.get("cursor")
        if not cursor:
            break
    return out


def get_kalshi_btc_hourly_range_markets(
    *,
    event_ticker:  Optional[str] = None,
    series_ticker: Optional[str] = None,
    status: str = "open",
) -> list[dict[str, Any]]:
    """
    Fetch and normalise BTC hourly range markets from Kalshi.

    Resolves configuration in order:
      1. Explicit keyword arguments
      2. KALSHI_BTC_RANGE_EVENT_TICKER  / KALSHI_BTC_RANGE_SERIES_TICKER env vars

    Returns a list of normalised market dicts (floor/cap guaranteed non-None),
    sorted by close_time then floor_strike.

    Raises ValueError when neither event nor series ticker is available.
    Raises httpx.HTTPError on network failures (let callers decide how to handle).
    """
    chosen_event  = event_ticker  or KALSHI_BTC_RANGE_EVENT_TICKER  or None
    chosen_series = series_ticker or KALSHI_BTC_RANGE_SERIES_TICKER or None
    if not chosen_event and not chosen_series:
        raise ValueError(
            "Hourly BTC range lookup requires event_ticker or series_ticker "
            "(set KALSHI_BTC_RANGE_EVENT_TICKER or KALSHI_BTC_RANGE_SERIES_TICKER in .env)."
        )

    raw = get_kalshi_markets(
        event_ticker=chosen_event,
        series_ticker=chosen_series,
        status=status,
    )

    normalized = [nm for nm in (_normalize_range_market(m) for m in raw) if nm is not None]
    normalized.sort(key=lambda m: (
        m["close_time"] or datetime.max.replace(tzinfo=timezone.utc),
        m["floor_strike"],
    ))
    return normalized


# ── Mock / 15-min helpers ─────────────────────────────────────────────────────

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
