"""
hourly_range_tracker.py — Observability layer for BTC hourly range markets.

RESEARCH / OBSERVABILITY ONLY.  No paper trades, no orders, no live execution.

Purpose
-------
Sample the state of Kalshi BTC hourly range contracts throughout the hour so we
can study contract and BTC behaviour before designing a trading strategy.

What gets recorded per poll
---------------------------
  hourly_range_markets  — one row per unique market_ticker (upserted)
  hourly_range_observations — one row per (market_ticker, observed_at)

  Observation fields:
    • BTC spot price vs. band (floor, cap, center)
    • distance_to_floor / distance_to_cap / distance_to_center
    • inside_band, norm_position (0=floor, 1=cap), range_state
    • contract_age_seconds, time_to_expiry_seconds
    • YES bid / ask / mid / spread / last_price / volume / liquidity
    • BTC features: volatility_30s, volatility_60s, velocity_10s, velocity_30s
    • containment_confidence (simple z-score model, see below)

Containment confidence model
-----------------------------
    sigma_remaining = btc_volatility_60s * sqrt(time_to_expiry_seconds / 60)
    z_floor = (btc_price - floor_strike)  / sigma_remaining
    z_cap   = (cap_strike  - btc_price)   / sigma_remaining
    confidence = Phi(z_cap) + Phi(z_floor) - 1

  This is the probability that the final BTC price falls in [floor, cap]
  IF the price process is a driftless random walk with the current 60s
  volatility.  The assumption is rough but clearly documented.  Treat
  confidence as a relative indicator, not a calibrated probability.

Outcome recording
-----------------
When a market disappears from the Kalshi active list (close_time passed or
it is no longer returned), the tracker calls _settle_market():
  • Records final_btc_price (BTC price at settlement moment)
  • Sets contained = floor_strike <= final_btc_price <= cap_strike
  • Computes n_observations, pct_time_inside, max_excursion_above_cap,
    max_excursion_below_floor from hourly_range_observations
  • Sets status='settled'

Startup recovery
----------------
On __init__ the tracker closes any hourly_range_markets rows with
close_time < now and status='open' (these are orphaned from a previous run).

Rate limiting
-------------
observe() is called from the main loop every ~2 seconds but only runs a full
poll cycle when RANGE_MARKET_POLL_INTERVAL_SECONDS have elapsed (default 30s).
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Optional

from app.config import (
    KALSHI_BTC_RANGE_EVENT_TICKER,
    KALSHI_BTC_RANGE_SERIES_TICKER,
    RANGE_MARKET_POLL_INTERVAL_SECONDS,
)
from app.data_feed import get_kalshi_btc_hourly_range_markets
from app.db import execute_query, fetch_all, fetch_one
from app.features import Tick, btc_velocity, rolling_std

logger = logging.getLogger(__name__)

# ── Simple normal CDF (duplicated from data_feed to avoid circular import) ────

def _normal_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


# ── Derived feature helpers ───────────────────────────────────────────────────

def _range_state(btc_price: float, floor_strike: float, cap_strike: float) -> str:
    if btc_price < floor_strike:
        return "below_range"
    if btc_price > cap_strike:
        return "above_range"
    band_center = (floor_strike + cap_strike) / 2
    return "inside_lower_half" if btc_price <= band_center else "inside_upper_half"


def _norm_position(btc_price: float, floor_strike: float, band_width: float) -> Optional[float]:
    if band_width <= 0:
        return None
    return round((btc_price - floor_strike) / band_width, 4)


def _containment_confidence(
    btc_price: float,
    floor_strike: float,
    cap_strike: float,
    btc_ticks: list[Tick],
    time_to_expiry_seconds: int,
) -> Optional[float]:
    """
    Rough P(final BTC in [floor, cap]) using current 60s vol projected forward.

    Returns None when there is insufficient tick history or no time remaining.
    """
    if time_to_expiry_seconds <= 0 or len(btc_ticks) < 5:
        return None
    vol60 = rolling_std(btc_ticks, 60.0)
    if vol60 <= 0:
        return None
    # Project: if the last 60s had std=vol60, remaining T/60 periods give sigma_rem
    sigma = vol60 * math.sqrt(max(time_to_expiry_seconds, 1) / 60.0)
    z_floor = (btc_price - floor_strike) / sigma   # z-score to floor (positive = above)
    z_cap   = (cap_strike - btc_price)  / sigma   # z-score to cap   (positive = below)
    conf = _normal_cdf(z_floor) + _normal_cdf(z_cap) - 1.0
    return round(max(0.0, min(1.0, conf)), 4)


def _select_nearby_markets(
    markets: list[dict],
    btc_price: float,
    *,
    nearest_above: int = 5,
    nearest_below: int = 5,
) -> list[dict]:
    """
    Keep the spot-containing band plus the nearest bands above and below.

    This trims the full hourly surface down to the contracts that are plausible
    outcomes from the current BTC price while still preserving enough context
    around spot for research.
    """
    containing: list[dict] = []
    below: list[tuple[float, dict]] = []
    above: list[tuple[float, dict]] = []

    for market in markets:
        try:
            floor_s = float(market["floor_strike"])
            cap_s = float(market["cap_strike"])
            center = float(market["band_center"])
        except (KeyError, TypeError, ValueError):
            continue

        if floor_s <= btc_price <= cap_s:
            containing.append(market)
        elif center < btc_price:
            below.append((btc_price - center, market))
        else:
            above.append((center - btc_price, market))

    below.sort(key=lambda item: item[0])
    above.sort(key=lambda item: item[0])

    selected = containing + [market for _, market in below[:nearest_below]]
    selected.extend(market for _, market in above[:nearest_above])

    # Preserve first-seen order for duplicates and API quirks.
    deduped: list[dict] = []
    seen: set[str] = set()
    for market in selected:
        ticker = market.get("ticker")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        deduped.append(market)
    return deduped


# ── Tracker ───────────────────────────────────────────────────────────────────

class HourlyRangeTracker:
    """
    Observes Kalshi BTC hourly range markets and persists sampled state.

    Thread-safety: designed for single-threaded use inside the main poll loop.
    """

    def __init__(self, poll_interval_seconds: float = RANGE_MARKET_POLL_INTERVAL_SECONDS) -> None:
        self._poll_interval = poll_interval_seconds
        self._last_poll_ts  = 0.0
        # Tickers we last saw as open; used to detect markets that have closed.
        self._known_open: set[str] = set()
        # Check whether range tracking is configured.
        self._enabled = bool(
            KALSHI_BTC_RANGE_EVENT_TICKER or KALSHI_BTC_RANGE_SERIES_TICKER
        )
        if not self._enabled:
            logger.info(
                "HourlyRangeTracker: no KALSHI_BTC_RANGE_EVENT_TICKER or "
                "KALSHI_BTC_RANGE_SERIES_TICKER configured — skipping range observations"
            )
        else:
            self._recover_orphans()
            logger.info(
                "HourlyRangeTracker ready — poll_interval=%.0fs  "
                "event=%r  series=%r",
                self._poll_interval,
                KALSHI_BTC_RANGE_EVENT_TICKER or "(not set)",
                KALSHI_BTC_RANGE_SERIES_TICKER or "(not set)",
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    def observe(self, btc_price: float, btc_ticks: list[Tick]) -> None:
        """
        Called from the main loop on every tick.  Internally rate-limited to
        RANGE_MARKET_POLL_INTERVAL_SECONDS so Kalshi API calls are infrequent.
        """
        if not self._enabled:
            return
        now = time.monotonic()
        if now - self._last_poll_ts < self._poll_interval:
            return
        self._last_poll_ts = now
        try:
            self._do_observe(btc_price, btc_ticks)
        except Exception as exc:
            logger.error("HourlyRangeTracker.observe failed: %s", exc, exc_info=True)

    # ── Internal poll cycle ────────────────────────────────────────────────────

    def _do_observe(self, btc_price: float, btc_ticks: list[Tick]) -> None:
        now_dt = datetime.now(timezone.utc)

        # 1. Fetch current open range markets from Kalshi.
        try:
            markets = get_kalshi_btc_hourly_range_markets(status="open")
        except ValueError as exc:
            # Misconfigured env — disable self to avoid log spam.
            logger.warning("HourlyRangeTracker: config error — %s (disabling)", exc)
            self._enabled = False
            return
        except Exception as exc:
            logger.warning("HourlyRangeTracker: Kalshi fetch failed — %s", exc)
            return

        selected_markets = _select_nearby_markets(markets, btc_price)

        # 2. Compute BTC features once for all markets this cycle.
        vol30  = rolling_std(btc_ticks, 30.0) or None
        vol60  = rolling_std(btc_ticks, 60.0) or None
        vel10  = btc_velocity(btc_ticks, window_seconds=10.0)
        vel30  = btc_velocity(btc_ticks, window_seconds=30.0)

        current_open_tickers = {
            mkt.get("ticker") for mkt in markets if mkt.get("ticker")
        }
        observed_tickers: set[str] = set()

        for mkt in selected_markets:
            ticker = mkt.get("ticker")
            if not ticker:
                continue
            observed_tickers.add(ticker)

            # Compute timing fields.
            close_time       = mkt.get("close_time")
            open_time        = mkt.get("open_time")
            tte_s: Optional[int] = None
            age_s: Optional[int] = None
            if close_time:
                tte_s = max(0, int((close_time - now_dt).total_seconds()))
            if open_time:
                age_s = max(0, int((now_dt - open_time).total_seconds()))

            floor_s = float(mkt["floor_strike"])
            cap_s   = float(mkt["cap_strike"])
            width   = float(mkt["band_width"])
            center  = float(mkt["band_center"])

            # Derived BTC position fields.
            d_floor   = round(btc_price - floor_s, 2)
            d_cap     = round(cap_s - btc_price, 2)
            d_center  = round(btc_price - center, 2)
            inside    = floor_s <= btc_price <= cap_s
            norm_pos  = _norm_position(btc_price, floor_s, width)
            rstate    = _range_state(btc_price, floor_s, cap_s)
            conf      = _containment_confidence(
                btc_price, floor_s, cap_s, btc_ticks,
                tte_s if tte_s is not None else 0,
            )

            # 3a. Upsert the market row.
            try:
                self._upsert_market(ticker, mkt, now_dt)
            except Exception as exc:
                logger.warning("Failed to upsert market %s: %s", ticker, exc)
                continue

            # 3b. Write the observation row.
            try:
                self._insert_observation(
                    ticker     = ticker,
                    observed_at = now_dt,
                    floor_s    = floor_s,
                    cap_s      = cap_s,
                    width      = width,
                    center     = center,
                    btc_price  = btc_price,
                    d_floor    = d_floor,
                    d_cap      = d_cap,
                    d_center   = d_center,
                    inside     = inside,
                    norm_pos   = norm_pos,
                    rstate     = rstate,
                    age_s      = age_s,
                    tte_s      = tte_s,
                    mkt        = mkt,
                    vol30      = vol30,
                    vol60      = vol60,
                    vel10      = vel10,
                    vel30      = vel30,
                    conf       = conf,
                )
            except Exception as exc:
                logger.warning("Failed to insert observation for %s: %s", ticker, exc)

            logger.debug(
                "HRM obs %s | BTC=%.2f floor=%.2f cap=%.2f | %s | "
                "tte=%ss conf=%s",
                ticker, btc_price, floor_s, cap_s, rstate,
                tte_s, f"{conf:.2f}" if conf is not None else "n/a",
            )

        # 4. Detect markets that have closed (were open last cycle, absent now).
        closed_tickers = self._known_open - current_open_tickers
        for ticker in closed_tickers:
            try:
                self._settle_market(ticker, btc_price, now_dt)
            except Exception as exc:
                logger.warning("Failed to settle market %s: %s", ticker, exc)

        self._known_open = current_open_tickers

        if markets:
            logger.info(
                "HourlyRangeTracker: %d open range market(s) fetched, "
                "%d nearby market(s) observed  "
                "%d settled this cycle",
                len(markets), len(observed_tickers), len(closed_tickers),
            )

    # ── DB helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _upsert_market(ticker: str, mkt: dict, now_dt: datetime) -> None:
        execute_query(
            """
            INSERT INTO hourly_range_markets
                (market_ticker, event_ticker, series_ticker, title,
                 floor_strike, cap_strike, band_width, band_center,
                 open_time, close_time, status)
            VALUES (%s, %s, %s, %s,  %s, %s, %s, %s,  %s, %s, 'open')
            ON DUPLICATE KEY UPDATE
                event_ticker  = VALUES(event_ticker),
                series_ticker = VALUES(series_ticker),
                title         = VALUES(title),
                open_time     = VALUES(open_time),
                close_time    = VALUES(close_time),
                updated_at    = CURRENT_TIMESTAMP
            """,
            (
                ticker,
                mkt.get("event_ticker"),
                mkt.get("series_ticker") or None,
                mkt.get("title") or None,
                float(mkt["floor_strike"]),
                float(mkt["cap_strike"]),
                float(mkt["band_width"]),
                float(mkt["band_center"]),
                mkt.get("open_time"),
                mkt.get("close_time"),
            ),
        )

    @staticmethod
    def _insert_observation(
        *,
        ticker: str,
        observed_at: datetime,
        floor_s: float,
        cap_s: float,
        width: float,
        center: float,
        btc_price: float,
        d_floor: float,
        d_cap: float,
        d_center: float,
        inside: bool,
        norm_pos: Optional[float],
        rstate: str,
        age_s: Optional[int],
        tte_s: Optional[int],
        mkt: dict,
        vol30: Optional[float],
        vol60: Optional[float],
        vel10: Optional[float],
        vel30: Optional[float],
        conf: Optional[float],
    ) -> None:
        execute_query(
            """
            INSERT INTO hourly_range_observations (
                market_ticker, observed_at,
                floor_strike, cap_strike, band_width, band_center,
                btc_price,
                distance_to_floor, distance_to_cap, distance_to_center,
                inside_band, norm_position, range_state,
                contract_age_seconds, time_to_expiry_seconds,
                yes_bid, yes_ask, yes_mid, yes_spread,
                last_price, volume, liquidity,
                btc_volatility_30s, btc_volatility_60s,
                btc_velocity_10s, btc_velocity_30s,
                containment_confidence
            ) VALUES (
                %s, %s,
                %s, %s, %s, %s,
                %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s
            )
            """,
            (
                ticker, observed_at,
                floor_s, cap_s, width, center,
                btc_price,
                d_floor, d_cap, d_center,
                inside, norm_pos, rstate,
                age_s, tte_s,
                mkt.get("yes_bid"),  mkt.get("yes_ask"),
                mkt.get("yes_mid"),  mkt.get("yes_spread"),
                mkt.get("last_price"), mkt.get("volume"), mkt.get("liquidity"),
                vol30, vol60, vel10, vel30,
                conf,
            ),
        )

    @staticmethod
    def _settle_market(ticker: str, final_btc_price: float, now_dt: datetime) -> None:
        """
        Mark a market as settled, record outcome, and compute summary stats
        from accumulated observations.
        """
        # Fetch band geometry.
        row = fetch_one(
            "SELECT floor_strike, cap_strike FROM hourly_range_markets "
            "WHERE market_ticker = %s",
            (ticker,),
        )
        if not row:
            return

        floor_s    = float(row["floor_strike"])
        cap_s      = float(row["cap_strike"])
        contained  = floor_s <= final_btc_price <= cap_s

        # Summary stats from observations.
        stats = fetch_one(
            """
            SELECT
                COUNT(*)                                          AS n_obs,
                AVG(inside_band)                                  AS pct_inside,
                MAX(GREATEST(0, btc_price - %s))                  AS max_above_cap,
                MAX(GREATEST(0, %s - btc_price))                  AS max_below_floor
            FROM hourly_range_observations
            WHERE market_ticker = %s
            """,
            (cap_s, floor_s, ticker),
        )
        n_obs         = int(stats["n_obs"]) if stats else 0
        pct_inside    = float(stats["pct_inside"]) if stats and stats["pct_inside"] is not None else None
        max_above     = float(stats["max_above_cap"]) if stats and stats["max_above_cap"] else None
        max_below     = float(stats["max_below_floor"]) if stats and stats["max_below_floor"] else None

        execute_query(
            """
            UPDATE hourly_range_markets
            SET status                      = 'settled',
                final_btc_price             = %s,
                contained                   = %s,
                settled_at                  = %s,
                n_observations              = %s,
                pct_time_inside             = %s,
                max_excursion_above_cap     = %s,
                max_excursion_below_floor   = %s,
                updated_at                  = CURRENT_TIMESTAMP
            WHERE market_ticker = %s
            """,
            (
                final_btc_price, contained, now_dt,
                n_obs, pct_inside, max_above, max_below,
                ticker,
            ),
        )
        logger.info(
            "HRM settled %s | BTC=%.2f floor=%.2f cap=%.2f | "
            "contained=%s  n_obs=%d  pct_inside=%.1f%%",
            ticker, final_btc_price, floor_s, cap_s,
            contained, n_obs,
            (pct_inside * 100 if pct_inside is not None else 0.0),
        )

    # ── Startup recovery ───────────────────────────────────────────────────────

    def _recover_orphans(self) -> None:
        """
        Close any markets with close_time < now that are still status='open'.
        These were left open by a previous logger run that shut down mid-hour.
        We mark them 'closed' (not 'settled') because we don't have the true
        final BTC price for the settlement moment.
        """
        now_dt = datetime.now(timezone.utc)
        result = fetch_all(
            """
            SELECT market_ticker FROM hourly_range_markets
            WHERE status = 'open' AND close_time < %s
            """,
            (now_dt,),
        )
        if not result:
            return
        for row in result:
            ticker = row["market_ticker"]
            execute_query(
                """
                UPDATE hourly_range_markets
                SET status = 'closed', updated_at = CURRENT_TIMESTAMP
                WHERE market_ticker = %s
                """,
                (ticker,),
            )
            logger.info(
                "HourlyRangeTracker: orphan market %s marked 'closed' on startup",
                ticker,
            )
