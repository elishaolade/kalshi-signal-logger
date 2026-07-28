"""
main.py — Research ingestion loop for the Kalshi BTC market logger.

Purpose
-------
Capture what happened, when it happened, and what the market knew
at that moment. No signals. No strategies. No predictions.

Poll cycle (every POLL_INTERVAL_SECONDS):
  1.  Fetch BTC price             (configured source; Kraken REST by default)
  2.  Fetch active Kalshi market  (production Kalshi API only)
  3.  Upsert market row           → markets
  4.  Upsert contract rows        → contracts  (YES + NO, once per market)
  5.  Insert market snapshot      → market_snapshots
  6.  Insert contract snapshots   → contract_snapshots  (YES + NO)
  7.  Compute + insert metrics    → market_metrics

All writes are insert-only for snapshots. Markets and contracts are
upserted (one row per unique market — not one per poll).

No live trades are placed at any point.
"""
from __future__ import annotations

import collections
import json
import logging
import signal
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from app.config import (
    KALSHI_BTC_BINARY_MAX_STRIKE_GAP,
    KALSHI_BTC_BINARY_MAX_TIME_TO_CLOSE_SECONDS,
    KALSHI_BTC_BINARY_SERIES_TICKER,
    LIVE_TRADING_ENABLED,
    MOMENTUM_LIVE_ACTIVE_PULSE_SECONDS,
    MOMENTUM_WS_OBSERVER_ENABLED,
    POLL_INTERVAL_SECONDS,
    FAST_REBOUND_TEST_ENABLED,
    CHEAP_MINORITY_TEST_ENABLED,
    BTC_IMPULSE_PAPER_ENABLED,
)
from app.data_feed import (
    get_btc_price,
    get_btc_price_source,
    get_kalshi_markets,
    _parse_float,
    _parse_dt,
)
from app.db import execute_query, fetch_all, fetch_one, insert_and_get_id, get_pool
from app.features import Tick
from app.models import MarketMetrics, MarketSnapshot
from app.momentum_shadow_tracker import MomentumShadowTracker
from app.momentum_live_trader import MomentumLiveTrader
from app.late_winning_live_trader import LateWinningLiveTrader
from app.fast_rebound_test_tracker import FastReboundTestTracker
from app.cheap_minority_test_live_tracker import CheapMinorityRealMoneyTestTracker
from app.btc_impulse_paper_tracker import BtcImpulsePaperTracker
from app.ws_first_observer import WebSocketFirstObserver

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# In-memory BTC tick buffer.
# 15m window at 2s polling = 450 ticks; 600 gives 20 min of rolling history.
BTC_TICK_BUFFER = 600

# ── Graceful shutdown ─────────────────────────────────────────────────────────

_stop_event = False


def _on_shutdown(signum: int, _frame) -> None:
    global _stop_event
    logger.info("Shutdown requested (%s) — finishing current tick", signal.Signals(signum).name)
    _stop_event = True


# ── In-memory state ───────────────────────────────────────────────────────────

# market_id (str) → markets.id (int)
_market_id_cache: dict[str, int] = {}

# market_id (str) → {side: contracts.id (int)}
_contract_id_cache: dict[str, dict[str, int]] = {}

# market_id (str) → current snapshot_sequence counter
_snapshot_sequence: dict[str, int] = collections.defaultdict(int)

# Momentum shadow tracker (initialized in run(), after DB pool is ready).
_shadow_tracker: Optional[MomentumShadowTracker] = None

# Momentum LIVE trader (real money). Default non-live; stays inert unless every
# MOMENTUM_LIVE_* gate is set. Runs alongside — never replaces — the shadow
# tracker. Initialized in run() after the DB pool is ready.
_live_trader: Optional[MomentumLiveTrader] = None

# Late winning-contract LIVE trader (real money). Separate from momentum;
# disabled unless LATE_WINNING_* gates are explicitly armed.
_late_winning_trader: Optional[LateWinningLiveTrader] = None

# Fast rebound TEST tracker. Research-only; no real orders.
_fast_rebound_test_tracker: Optional[FastReboundTestTracker] = None

# Cheap minority Real-Money TEST tracker. Explicitly gated; places real orders
# only when CHEAP_MINORITY_TEST_* gates are armed.
_cheap_minority_test_tracker: Optional[CheapMinorityRealMoneyTestTracker] = None

# BTC impulse prospective PAPER tracker. Research-only; no real orders.
_btc_impulse_paper_tracker: Optional[BtcImpulsePaperTracker] = None

# WebSocket-first observer. When enabled, quote snapshots handed to shadow/live
# diagnostics come from fresh WS order books; stale/missing WS books skip ticks.
_ws_observer: Optional[WebSocketFirstObserver] = None


# ── Kalshi market fetcher ──────────────────────────────────────────────────────

_kalshi_market_cache: Optional[dict] = None
_kalshi_market_cache_ts: float = 0.0
_KALSHI_CACHE_TTL = 30.0  # seconds


def _get_active_kalshi_market(btc_price: float) -> dict:
    """
    Return the nearest-expiry Kalshi BTC binary market closest to current BTC price.

    Returns a dict with keys:
        market_id, title, market_type, target_price,
        opens_at, closes_at, time_remaining_seconds, status,
        _raw  (full Kalshi API payload for raw_payload storage)
    """
    global _kalshi_market_cache, _kalshi_market_cache_ts

    now_ts = time.monotonic()
    if _kalshi_market_cache and now_ts - _kalshi_market_cache_ts < _KALSHI_CACHE_TTL:
        return _update_timing(_kalshi_market_cache)

    series = KALSHI_BTC_BINARY_SERIES_TICKER or "KXBTC"
    try:
        raw_markets = get_kalshi_markets(series_ticker=series, status="open")
    except Exception as exc:
        raise RuntimeError(f"Kalshi fetch failed: {exc}") from exc

    # Binary (T) markets: have floor_strike, no cap_strike.
    binary = []
    now_utc = datetime.now(timezone.utc)
    for m in raw_markets:
        fs = _parse_float(m.get("floor_strike"))
        cs = _parse_float(m.get("cap_strike"))
        ct = _parse_dt(m.get("close_time"))
        ot = _parse_dt(m.get("open_time"))
        if fs is not None and cs is None and ct is not None:
            tte_s = int((ct - now_utc).total_seconds())
            binary.append(
                {
                    "raw": m,
                    "floor_strike": fs,
                    "close_time": ct,
                    "open_time": ot,
                    "tte_s": tte_s,
                    "gap": abs(fs - btc_price),
                }
            )

    if not binary:
        raise RuntimeError("No Kalshi BTC binary markets found")

    candidates = [
        b for b in binary
        if 0 < b["tte_s"] <= KALSHI_BTC_BINARY_MAX_TIME_TO_CLOSE_SECONDS
    ]
    if not candidates:
        nearest = min(binary, key=lambda b: abs(b["tte_s"]))
        raise RuntimeError(
            "No BTC binary markets close soon enough "
            f"(series={series!r}, nearest_tte={nearest['tte_s']}s, "
            f"nearest_strike={nearest['floor_strike']:.2f})"
        )

    candidates.sort(key=lambda b: (b["close_time"], b["gap"]))
    nearest_close = candidates[0]["close_time"]
    same_expiry = [b for b in candidates if b["close_time"] == nearest_close]
    best = min(same_expiry, key=lambda b: b["gap"])
    if best["gap"] > KALSHI_BTC_BINARY_MAX_STRIKE_GAP:
        raise RuntimeError(
            "Nearest soon-closing BTC binary strike is implausibly far from spot "
            f"(series={series!r}, strike={best['floor_strike']:.2f}, "
            f"btc={btc_price:.2f}, gap={best['gap']:.2f}, tte={best['tte_s']}s)"
        )
    raw           = best["raw"]

    result = {
        "market_id":    raw.get("ticker"),
        "title":        raw.get("title") or raw.get("yes_sub_title") or "",
        "market_type":  "binary",
        # target_price = contract strike/threshold (Kalshi field: floor_strike).
        # This is the BTC level at which YES/NO settles.  It is NOT the live
        # spot price.  e.g. KXBTC-260612-0500-T63500 → target_price = 63500.00
        "target_price": best["floor_strike"],
        "opens_at":     _parse_dt(raw.get("open_time")),
        "closes_at":    best["close_time"],
        "settles_at":   None,
        "status":       "open",
        "_raw":         raw,
    }
    _kalshi_market_cache    = result
    _kalshi_market_cache_ts = now_ts
    return _update_timing(result)


def _update_timing(market: dict) -> dict:
    """Recompute time_remaining_seconds without hitting the API."""
    now       = datetime.now(timezone.utc)
    closes_at = market.get("closes_at")
    tte       = max(0, int((closes_at - now).total_seconds())) if closes_at else None
    return {**market, "time_remaining_seconds": tte}


def _get_contract_prices(market: dict, btc_price: float) -> dict[str, dict]:
    """
    Extract YES/NO bid/ask from a Kalshi market dict.
    Returns {"YES": {bid_price, ask_price, last_price, spread, volume, source}, "NO": ...}
    """
    raw     = market.get("_raw") or {}
    yes_bid = _parse_float(raw.get("yes_bid_dollars") or raw.get("yes_bid"))
    yes_ask = _parse_float(raw.get("yes_ask_dollars") or raw.get("yes_ask"))
    no_bid  = _parse_float(raw.get("no_bid_dollars")  or raw.get("no_bid"))
    no_ask  = _parse_float(raw.get("no_ask_dollars")  or raw.get("no_ask"))
    yes_last = _parse_float(
        raw.get("last_price_dollars")
        or raw.get("previous_price_dollars")
        or raw.get("last_price")
    )

    if yes_ask is None or yes_ask <= 0:
        raise RuntimeError(
            f"Kalshi quote data unavailable for market {market.get('market_id')}"
        )

    yes_bid  = yes_bid or 0.0
    no_bid   = no_bid  or round(1.0 - yes_ask, 4)
    no_ask   = no_ask  or round(1.0 - yes_bid, 4)
    no_last  = round(1.0 - yes_last, 4) if yes_last is not None else None
    volume   = int(_parse_float(
        raw.get("volume_fp") or raw.get("volume_dollars") or raw.get("volume")
    ) or 0) or None

    return {
        "YES": {"bid_price": yes_bid, "ask_price": yes_ask,
                "last_price": yes_last, "spread": round(yes_ask - yes_bid, 4),
                "volume": volume, "source": "kalshi"},
        "NO":  {"bid_price": no_bid,  "ask_price": no_ask,
                "last_price": no_last, "spread": round(no_ask - no_bid, 4),
                "volume": volume, "source": "kalshi"},
    }


# ── DB write helpers ──────────────────────────────────────────────────────────

def _upsert_market(market: dict) -> int:
    """Upsert market row. Returns markets.id."""
    market_id = market["market_id"]
    if market_id in _market_id_cache:
        return _market_id_cache[market_id]

    raw_json = json.dumps(market.get("_raw")) if market.get("_raw") else None
    execute_query(
        """
        INSERT INTO markets
            (market_id, title, market_type, target_price,
             opens_at, closes_at, status, raw_payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            title      = VALUES(title),
            status     = VALUES(status),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            market_id,
            market.get("title"),
            market.get("market_type", "binary"),
            market.get("target_price"),
            market.get("opens_at"),
            market.get("closes_at"),
            market.get("status", "open"),
            raw_json,
        ),
    )
    row = fetch_one("SELECT id FROM markets WHERE market_id = %s", (market_id,))
    db_id = row["id"]
    _market_id_cache[market_id] = db_id
    return db_id


def _upsert_contracts(market_db_id: int, market_id: str) -> dict[str, int]:
    """
    Upsert YES + NO contract rows for a market.
    Returns {"YES": contracts.id, "NO": contracts.id}.
    """
    if market_id in _contract_id_cache:
        return _contract_id_cache[market_id]

    ids: dict[str, int] = {}
    for side in ("YES", "NO"):
        contract_id = f"{market_id}_{side}"
        execute_query(
            """
            INSERT INTO contracts (market_id, contract_id, side, status)
            VALUES (%s, %s, %s, 'open')
            ON DUPLICATE KEY UPDATE status = 'open', updated_at = CURRENT_TIMESTAMP
            """,
            (market_db_id, contract_id, side),
        )
        row = fetch_one("SELECT id FROM contracts WHERE contract_id = %s", (contract_id,))
        ids[side] = row["id"]

    _contract_id_cache[market_id] = ids
    return ids


def _insert_market_snapshot(
    market_db_id: int,
    market_id:    str,
    captured_at:  datetime,
    btc_price:    float,
    tte:          Optional[int],
    source:       str,
    raw_payload:  Optional[dict],
) -> int:
    """Insert one market_snapshot row. Returns market_snapshots.id."""
    _snapshot_sequence[market_id] += 1
    seq      = _snapshot_sequence[market_id]
    raw_json = json.dumps(raw_payload) if raw_payload else None

    return insert_and_get_id(
        """
        INSERT INTO market_snapshots
            (market_id, snapshot_sequence, captured_at,
             btc_price, time_remaining_seconds, source, raw_payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (market_db_id, seq, captured_at, btc_price, tte, source, raw_json),
    )


def _insert_contract_snapshots(
    snapshot_id:  int,
    contract_ids: dict[str, int],
    captured_at:  datetime,
    prices:       dict[str, dict],
) -> None:
    """Insert YES + NO contract_snapshots for one poll cycle."""
    for side, price_data in prices.items():
        cid = contract_ids.get(side)
        if cid is None:
            continue
        bid = price_data.get("bid_price")
        ask = price_data.get("ask_price")
        if bid is None or ask is None:
            continue
        execute_query(
            """
            INSERT INTO contract_snapshots
                (market_snapshot_id, contract_id, captured_at,
                 last_price, bid_price, ask_price, spread, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                snapshot_id, cid, captured_at,
                price_data.get("last_price"),
                bid, ask,
                price_data.get("spread"),
                price_data.get("volume"),
            ),
        )


def _insert_market_metrics(
    snapshot_id:  int,
    captured_at:  datetime,
    btc_price:    float,
    target_price: Optional[float],
    btc_ticks:    list[Tick],
) -> None:
    """Compute and insert one market_metrics row."""

    # Distance from strike
    distance     : Optional[float] = None
    distance_pct : Optional[float] = None
    above        : Optional[bool]  = None
    if target_price and target_price > 0:
        distance     = round(btc_price - target_price, 2)
        distance_pct = round((btc_price - target_price) / target_price * 100, 6)
        above        = btc_price > target_price

    def _price_ago(seconds: float) -> Optional[float]:
        if not btc_ticks:
            return None
        cutoff = btc_ticks[-1].ts - seconds
        for tick in reversed(btc_ticks[:-1]):
            if tick.ts <= cutoff:
                return tick.price
        return None

    def _pct_return(lag_s: float) -> Optional[float]:
        past = _price_ago(lag_s)
        if past is None or past == 0:
            return None
        return round((btc_price - past) / past, 8)

    def _return_stddev(window_s: float) -> Optional[float]:
        if len(btc_ticks) < 2:
            return None
        cutoff = btc_ticks[-1].ts - window_s
        window = [t for t in btc_ticks if t.ts >= cutoff]
        if len(window) < 2:
            return None
        rets = [
            window[i].price / window[i - 1].price - 1
            for i in range(1, len(window))
            if window[i - 1].price > 0
        ]
        if len(rets) < 2:
            return None
        try:
            return round(statistics.stdev(rets), 8)
        except statistics.StatisticsError:
            return None

    execute_query(
        """
        INSERT INTO market_metrics (
            market_snapshot_id, captured_at,
            distance_from_target, distance_from_target_pct, is_above_target,
            btc_return_30s, btc_return_1m, btc_return_5m,
            btc_return_stddev_1m, btc_return_stddev_3m,
            btc_return_stddev_5m, btc_return_stddev_15m
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            snapshot_id, captured_at,
            distance, distance_pct, above,
            _pct_return(30), _pct_return(60), _pct_return(300),
            _return_stddev(60),  _return_stddev(180),
            _return_stddev(300), _return_stddev(900),
        ),
    )


# ── Warm-up ───────────────────────────────────────────────────────────────────

def _warm_up(btc_ticks: collections.deque) -> None:
    """Pre-load recent BTC prices from market_snapshots to seed the rolling buffer."""
    try:
        rows = fetch_all(
            "SELECT btc_price, captured_at FROM market_snapshots "
            "ORDER BY captured_at DESC LIMIT %s",
            (BTC_TICK_BUFFER,),
        )
        for row in reversed(rows):
            btc_ticks.append(Tick(
                price=float(row["btc_price"]),
                ts=row["captured_at"].timestamp(),
            ))
        if btc_ticks:
            logger.info("Warm-up: loaded %d BTC prices from market_snapshots", len(btc_ticks))
        else:
            logger.info("Warm-up: no recent data in DB — starting cold")
    except Exception as exc:
        logger.warning("Warm-up failed (continuing cold): %s", exc)


# ── Console summary ───────────────────────────────────────────────────────────

def _log_summary(
    n:           int,
    btc_price:   float,
    market_id:   str,
    target:      Optional[float],
    tte:         Optional[int],
    prices:      dict,
) -> None:
    yes = prices.get("YES", {})
    no  = prices.get("NO",  {})
    gap = f"{btc_price - target:+,.0f}" if target else "—"
    logger.info(
        "tick#%04d | BTC $%s  strike $%s  gap %s | "
        "YES %.3f/%.3f  NO %.3f/%.3f | tte=%ss  src=%s | %s",
        n,
        f"{btc_price:,.0f}",
        f"{target:,.0f}" if target else "—",
        gap,
        yes.get("bid_price", 0), yes.get("ask_price", 0),
        no.get("bid_price",  0), no.get("ask_price",  0),
        tte if tte is not None else "—",
        yes.get("source", "?"),
        market_id,
    )


# ── One poll cycle ────────────────────────────────────────────────────────────

def _tick(n: int, btc_ticks: collections.deque) -> None:
    now = datetime.now(timezone.utc)

    # 1. BTC price
    try:
        btc_price = get_btc_price(allow_mock=False)
        btc_source = get_btc_price_source()
    except Exception as exc:
        logger.warning("Skipping tick #%d: BTC price unavailable — %s", n, exc)
        return
    btc_ticks.append(Tick(price=btc_price, ts=now.timestamp()))
    ticks = list(btc_ticks)

    # 2. Active market
    try:
        market = _get_active_kalshi_market(btc_price)
    except Exception as exc:
        logger.warning("Skipping tick #%d: active market unavailable — %s", n, exc)
        return
    market_id    = market["market_id"]
    target_price = market.get("target_price")
    tte          = market.get("time_remaining_seconds")

    # 3. Upsert market
    try:
        market_db_id = _upsert_market(market)
    except Exception as exc:
        logger.warning("Failed to upsert market %s: %s", market_id, exc)
        return

    # 4. Upsert contracts
    try:
        contract_ids = _upsert_contracts(market_db_id, market_id)
    except Exception as exc:
        logger.warning("Failed to upsert contracts for %s: %s", market_id, exc)
        return

    # 5. Contract prices
    try:
        prices = _get_contract_prices(market, btc_price)
    except Exception as exc:
        logger.warning("Skipping tick #%d: contract prices unavailable — %s", n, exc)
        return

    if _ws_observer is not None:
        observed = _ws_observer.observe_market(market_id)
        if observed is None:
            logger.debug("Skipping tick #%d: waiting for WebSocket prices", n)
            return
        prices = observed.prices

    # 6. Insert market snapshot
    try:
        snapshot_id = _insert_market_snapshot(
            market_db_id=market_db_id,
            market_id=market_id,
            captured_at=now,
            btc_price=btc_price,
            tte=tte,
            source=btc_source,
            raw_payload={
                "btc_price": btc_price,
                "btc_price_source": btc_source,
                "market_raw": market.get("_raw"),
            },
        )
    except Exception as exc:
        logger.warning("Failed to insert market snapshot: %s", exc)
        return

    # 7. Insert contract snapshots
    try:
        _insert_contract_snapshots(snapshot_id, contract_ids, now, prices)
    except Exception as exc:
        logger.warning("Failed to insert contract snapshots: %s", exc)

    # 8. Insert market metrics
    try:
        _insert_market_metrics(snapshot_id, now, btc_price, target_price, ticks)
    except Exception as exc:
        logger.warning("Failed to insert market metrics: %s", exc)

    # 9. Console summary
    _log_summary(n, btc_price, market_id, target_price, tte, prices)

    # 10. Shadow tracking (research-only; no orders placed)
    if _shadow_tracker is not None:
        try:
            _shadow_tracker.on_tick(
                market_db_id  = market_db_id,
                market_id     = market_id,
                market_ticker = market_id,
                contract_ids  = contract_ids,
                target_price  = target_price,
                captured_at   = now,
                btc_price     = btc_price,
                tte           = tte,
                prices        = prices,
                btc_ticks     = ticks,
                snapshot_id   = snapshot_id,
                snapshot_seq  = _snapshot_sequence[market_id],
            )
        except Exception as exc:
            logger.warning("Shadow tracker error on tick #%d: %s", n, exc)

    # 11. Live trading (real money; runs only when fully armed via MOMENTUM_LIVE_*).
    #     Called AFTER the shadow tracker so live entries pair tick-for-tick with
    #     shadow entries. Inert by default — never places orders unless armed.
    if _live_trader is not None:
        try:
            _live_trader.on_tick(
                market_db_id  = market_db_id,
                market_id     = market_id,
                market_ticker = market_id,
                contract_ids  = contract_ids,
                target_price  = target_price,
                captured_at   = now,
                btc_price     = btc_price,
                tte           = tte,
                prices        = prices,
                btc_ticks     = ticks,
                snapshot_id   = snapshot_id,
                snapshot_seq  = _snapshot_sequence[market_id],
            )
        except Exception as exc:
            logger.warning("Live trader error on tick #%d: %s", n, exc)

    # 12. Late winning-contract strategy (real money; separate LATE_WINNING_* gates).
    if _late_winning_trader is not None:
        try:
            _late_winning_trader.on_tick(
                market_db_id  = market_db_id,
                market_id     = market_id,
                market_ticker = market_id,
                contract_ids  = contract_ids,
                target_price  = target_price,
                captured_at   = now,
                btc_price     = btc_price,
                tte           = tte,
                prices        = prices,
                btc_ticks     = ticks,
                snapshot_id   = snapshot_id,
                snapshot_seq  = _snapshot_sequence[market_id],
            )
        except Exception as exc:
            logger.warning("Late winning trader error on tick #%d: %s", n, exc)

    # 13. Fast rebound TEST tracker (research-only; never places orders).
    if _fast_rebound_test_tracker is not None:
        try:
            _fast_rebound_test_tracker.on_tick(
                market_db_id=market_db_id,
                market_id=market_id,
                market_ticker=market_id,
                contract_ids=contract_ids,
                target_price=target_price,
                opens_at=market.get("opens_at"),
                captured_at=now,
                btc_price=btc_price,
                tte=tte,
                prices=prices,
            )
        except Exception as exc:
            logger.warning("Fast rebound TEST tracker error on tick #%d: %s", n, exc)

    # 14. Cheap minority Real-Money TEST tracker (real orders; separate gates).
    if _cheap_minority_test_tracker is not None:
        try:
            _cheap_minority_test_tracker.on_tick(
                market_db_id=market_db_id,
                market_ticker=market_id,
                contract_ids=contract_ids,
                opens_at=market.get("opens_at"),
                closes_at=market.get("closes_at"),
                captured_at=now,
                btc_price=btc_price,
                tte=tte,
                prices=prices,
            )
        except Exception as exc:
            logger.warning("Cheap minority Real-Money TEST tracker error on tick #%d: %s", n, exc)

    # 15. BTC impulse PAPER tracker (research-only; never places orders).
    if _btc_impulse_paper_tracker is not None:
        try:
            _btc_impulse_paper_tracker.on_tick(
                market_db_id=market_db_id,
                market_ticker=market_id,
                contract_ids=contract_ids,
                captured_at=now,
                btc_price=btc_price,
                prices=prices,
                btc_ticks=ticks,
            )
        except Exception as exc:
            logger.warning("BTC impulse PAPER tracker error on tick #%d: %s", n, exc)


# ── Startup + main loop ───────────────────────────────────────────────────────

def run() -> None:
    global _stop_event, _shadow_tracker, _live_trader, _late_winning_trader
    global _fast_rebound_test_tracker, _cheap_minority_test_tracker
    global _btc_impulse_paper_tracker, _ws_observer

    if LIVE_TRADING_ENABLED:
        raise RuntimeError(
            "LIVE_TRADING_ENABLED=true is not permitted — "
            "this project is research-only. Set it to false and restart."
        )

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT,  _on_shutdown)

    logger.info("Kalshi market logger starting (research-only mode)")

    try:
        get_pool()
    except RuntimeError as exc:
        logger.critical("Cannot reach MySQL: %s", exc)
        raise SystemExit(1) from exc

    btc_ticks: collections.deque[Tick] = collections.deque(maxlen=BTC_TICK_BUFFER)
    _warm_up(btc_ticks)

    if MOMENTUM_WS_OBSERVER_ENABLED:
        try:
            _ws_observer = WebSocketFirstObserver()
            _ws_observer.start()
        except Exception as exc:
            logger.critical("WebSocketFirstObserver init failed: %s", exc)
            raise SystemExit(1) from exc

    try:
        _shadow_tracker = MomentumShadowTracker()
    except Exception as exc:
        logger.warning(
            "MomentumShadowTracker init failed (shadow tracking disabled "
            "— run the migration first): %s", exc
        )

    try:
        _live_trader = MomentumLiveTrader(
            ws_stream=_ws_observer.stream if _ws_observer is not None else None
        )
    except Exception as exc:
        logger.warning(
            "MomentumLiveTrader init failed (live trading disabled "
            "— run scripts/migrate_add_momentum_live.py first): %s", exc
        )

    try:
        _late_winning_trader = LateWinningLiveTrader()
    except Exception as exc:
        logger.warning(
            "LateWinningLiveTrader init failed (late winning live disabled "
            "— run scripts/migrate_add_momentum_live.py first): %s", exc
        )

    if FAST_REBOUND_TEST_ENABLED:
        try:
            _fast_rebound_test_tracker = FastReboundTestTracker()
        except Exception as exc:
            logger.warning(
                "FastReboundTestTracker init failed "
                "(run scripts/migrate_add_fast_rebound_test.py first): %s", exc
            )

    if CHEAP_MINORITY_TEST_ENABLED:
        try:
            _cheap_minority_test_tracker = CheapMinorityRealMoneyTestTracker()
        except Exception as exc:
            logger.warning(
                "CheapMinorityRealMoneyTestTracker init failed "
                "(run scripts/migrate_add_cheap_minority_real_money_test.py first): %s",
                exc,
            )

    if BTC_IMPULSE_PAPER_ENABLED:
        try:
            _btc_impulse_paper_tracker = BtcImpulsePaperTracker()
        except Exception as exc:
            logger.warning(
                "BtcImpulsePaperTracker init failed "
                "(run scripts/migrate_add_btc_impulse_paper_test.py first): %s", exc
            )

    logger.info(
        "Running — interval=%.1fs  active_pulse=%.3fs  tick_buffer=%d",
        POLL_INTERVAL_SECONDS, MOMENTUM_LIVE_ACTIVE_PULSE_SECONDS, BTC_TICK_BUFFER,
    )

    n = 0
    next_active_pulse_at = time.monotonic()
    while not _stop_event:
        n += 1
        tick_start = time.monotonic()

        try:
            _tick(n, btc_ticks)
        except Exception as exc:
            logger.error("Unhandled error on tick #%d: %s", n, exc, exc_info=True)

        elapsed   = time.monotonic() - tick_start
        sleep_for = max(0.0, POLL_INTERVAL_SECONDS - elapsed)

        if elapsed > POLL_INTERVAL_SECONDS:
            logger.warning(
                "Tick #%d took %.2fs (> %.1fs poll interval)", n, elapsed, POLL_INTERVAL_SECONDS
            )

        deadline = time.monotonic() + sleep_for
        while not _stop_event and time.monotonic() < deadline:
            now_mono = time.monotonic()
            if (
                _live_trader is not None
                and MOMENTUM_LIVE_ACTIVE_PULSE_SECONDS > 0
                and now_mono >= next_active_pulse_at
            ):
                try:
                    _live_trader.pulse_active_positions()
                except Exception as exc:
                    logger.error("Live active pulse failed: %s", exc, exc_info=True)
                next_active_pulse_at = now_mono + MOMENTUM_LIVE_ACTIVE_PULSE_SECONDS
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    logger.info("Market logger stopped cleanly after %d tick(s)", n)


if __name__ == "__main__":
    run()
