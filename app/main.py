"""
main.py — Main polling loop for the Kalshi BTC signal logger.

Poll cycle (every POLL_INTERVAL_SECONDS):
  1.  Fetch BTC price             (Kraken → mock fallback)
  2.  Get active 15-min market    (mock)
  3.  Upsert market row           → markets
  4.  Store BTC tick              → btc_ticks
  5.  Get YES/NO contract prices  (mock pricing model)
  6.  Store contract ticks        → contract_ticks
  7.  Maintain BTC history        (in-memory deque, warm-started from DB)
  8.  Evaluate strategies         cheap_reversal_scalp_v1 + premium_momentum_continuation_v1
  9.  Deduplicate signals          cooldown window per (rule/version/side/ticker)
  10. Insert fired signals         → signals
  11. Open paper trades            → paper_trades  (via PaperTrader)
  12. Update open trades           exit checks + → trade_snapshots
  13. Log console summary

No live trades are placed at any point.
"""

from __future__ import annotations

import collections
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import (
    LIVE_TRADING_ENABLED,
    PAPER_POSITION_SIZE,
    POLL_INTERVAL_SECONDS,
    SLIPPAGE_MODE,
)
from app.data_feed import (
    get_active_mock_market,
    get_btc_price,
    get_mock_contract_prices,
)
from app.db import execute_query, fetch_all, insert_and_get_id, get_pool
from app.features import Tick
from app.paper_trader import PaperTrader
from app.strategies import Signal, run_all

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# How long to suppress duplicate signals for the same rule/side/market.
SIGNAL_COOLDOWN_SECONDS = int(30)

# In-memory BTC tick buffer depth.  120s features + overhead at 2s polling ≈ 90
# ticks minimum; 600 gives ~20 minutes of history for restarts and analysis.
BTC_TICK_BUFFER = 600

# How far back to pre-load BTC ticks from the DB on startup.
WARMUP_LOOKBACK_SECONDS = 180

# ── Graceful shutdown ─────────────────────────────────────────────────────────

_stop_event = False   # set to True by signal handler; checked in loop


def _on_shutdown(signum: int, _frame) -> None:
    global _stop_event
    name = signal.Signals(signum).name
    logger.info("Shutdown requested (%s) — finishing current tick then stopping", name)
    _stop_event = True


# ── Signal cooldown tracker ───────────────────────────────────────────────────

class _Cooldown:
    """
    Prevents the same strategy/side/market from firing more than once per
    cooldown window.  Keyed by "rule_name/rule_version/side/market_ticker".
    """

    def __init__(self, window_seconds: int) -> None:
        self._window = timedelta(seconds=window_seconds)
        self._last: dict[str, datetime] = {}

    def is_blocked(self, key: str) -> bool:
        last = self._last.get(key)
        return last is not None and (datetime.now(timezone.utc) - last) < self._window

    def mark(self, key: str) -> None:
        self._last[key] = datetime.now(timezone.utc)

    def evict_expired(self) -> None:
        """Prune stale entries to prevent unbounded growth."""
        now = datetime.now(timezone.utc)
        expired = [k for k, v in self._last.items() if now - v >= self._window]
        for k in expired:
            del self._last[k]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _upsert_market(market: dict) -> None:
    execute_query(
        """
        INSERT INTO markets (market_ticker, target_price, open_time, close_time)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            target_price = VALUES(target_price),
            open_time    = VALUES(open_time),
            close_time   = VALUES(close_time)
        """,
        (
            market["market_ticker"],
            market["target_price"],
            market["open_time"],
            market["close_time"],
        ),
    )


def _insert_btc_tick(
    market_ticker: str,
    btc_price: float,
    recorded_at: datetime,
) -> None:
    execute_query(
        "INSERT INTO btc_ticks (market_ticker, btc_price, source, recorded_at) "
        "VALUES (%s, %s, 'kraken_or_mock', %s)",
        (market_ticker, btc_price, recorded_at),
    )


def _insert_contract_tick(
    market_ticker: str,
    side: str,
    quotes: dict,
    time_remaining: float,
    contract_age: float,
    recorded_at: datetime,
) -> None:
    execute_query(
        """
        INSERT INTO contract_ticks (
            market_ticker, side,
            bid_price, ask_price, mid_price, last_price, spread,
            time_remaining_seconds, contract_age_seconds,
            recorded_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            market_ticker, side,
            quotes.get("bid_price"),
            quotes.get("ask_price"),
            quotes.get("mid_price"),
            quotes.get("last_price"),
            quotes.get("spread"),
            int(time_remaining),
            int(contract_age),
            recorded_at,
        ),
    )


def _insert_signal(sig: Signal) -> int:
    return insert_and_get_id(
        """
        INSERT INTO signals (
            market_ticker, rule_name, rule_version, side,
            contract_price, bid_price, ask_price, spread,
            btc_price, target_price, gap, directional_gap, gap_z_score,
            contract_age_seconds, time_remaining_seconds,
            momentum_score, reversal_score, btc_velocity,
            volatility_30s, volatility_60s, volatility_120s,
            edge, confidence_score, reason, signal_status, recorded_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        """,
        (
            sig.market_ticker, sig.rule_name, sig.rule_version, sig.side,
            sig.contract_price, sig.bid_price, sig.ask_price, sig.spread,
            sig.btc_price, sig.target_price,
            sig.gap, sig.directional_gap, sig.gap_z_score,
            sig.contract_age_seconds, sig.time_remaining_seconds,
            sig.momentum_score, sig.reversal_score, sig.btc_velocity,
            sig.volatility_30s, sig.volatility_60s, sig.volatility_120s,
            sig.edge, sig.confidence_score,
            sig.reason, sig.signal_status, sig.recorded_at,
        ),
    )


def _warm_up(btc_ticks: collections.deque) -> None:
    """Pre-load recent BTC ticks from DB so feature windows are populated at start."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=WARMUP_LOOKBACK_SECONDS)
        rows = fetch_all(
            "SELECT btc_price, recorded_at FROM btc_ticks "
            "WHERE recorded_at >= %s ORDER BY recorded_at ASC",
            (cutoff,),
        )
        for row in rows:
            btc_ticks.append(Tick(
                price=float(row["btc_price"]),
                ts=row["recorded_at"].timestamp(),
            ))
        if btc_ticks:
            logger.info("Warm-up: loaded %d BTC ticks from the last %ds",
                        len(btc_ticks), WARMUP_LOOKBACK_SECONDS)
        else:
            logger.info("Warm-up: no recent ticks in DB — starting cold")
    except Exception as exc:
        logger.warning("Warm-up from DB failed (continuing cold): %s", exc)


# ── Console summary ───────────────────────────────────────────────────────────

def _log_summary(
    n: int,
    btc_price: float,
    target_price: float,
    market_ticker: str,
    time_remaining: float,
    contract_prices: dict,
    signals_fired: int,
    trades_opened: int,
    trades_open: int,
    trades_closed: int,
) -> None:
    yes = contract_prices.get("YES", {})
    no  = contract_prices.get("NO",  {})
    gap = btc_price - target_price
    logger.info(
        "tick#%04d | BTC $%s  tgt $%s  gap %s | "
        "YES %.2f/%.2f  NO %.2f/%.2f | "
        "t=%ds | sigs=%d opened=%d open=%d closed=%d | %s",
        n,
        f"{btc_price:,.0f}", f"{target_price:,.0f}", f"{gap:+,.0f}",
        yes.get("bid_price", 0), yes.get("ask_price", 0),
        no.get("bid_price",  0), no.get("ask_price",  0),
        int(time_remaining),
        signals_fired, trades_opened, trades_open, trades_closed,
        market_ticker,
    )


# ── One poll cycle ────────────────────────────────────────────────────────────

def _tick(
    n: int,
    btc_ticks: collections.deque,
    cooldown: _Cooldown,
    trader: PaperTrader,
) -> None:
    now = datetime.now(timezone.utc)

    # ── 1. BTC price ──────────────────────────────────────────────────────────
    btc_price = get_btc_price()

    # ── 2. Active market ──────────────────────────────────────────────────────
    market          = get_active_mock_market()
    market_ticker   = market["market_ticker"]
    target_price    = market["target_price"]
    time_remaining  = market["time_remaining_seconds"]
    contract_age    = market["contract_age_seconds"]

    # ── 3 + 4. Persist raw market and BTC data ────────────────────────────────
    try:
        _upsert_market(market)
        _insert_btc_tick(market_ticker, btc_price, now)
    except Exception as exc:
        logger.warning("Failed to persist market/BTC row: %s", exc)

    # ── 5. Contract prices ────────────────────────────────────────────────────
    contract_prices = get_mock_contract_prices(btc_price, target_price, time_remaining)

    # ── 6. Persist contract ticks ─────────────────────────────────────────────
    try:
        for side in ("YES", "NO"):
            _insert_contract_tick(
                market_ticker, side, contract_prices[side],
                time_remaining, contract_age, now,
            )
    except Exception as exc:
        logger.warning("Failed to persist contract tick(s): %s", exc)

    # ── 7. Update in-memory BTC history ──────────────────────────────────────
    btc_ticks.append(Tick(price=btc_price, ts=now.timestamp()))
    ticks = list(btc_ticks)

    # ── 8–11. Strategy evaluation, dedup, insert, trade open ─────────────────
    signals_fired  = 0
    trades_opened  = 0

    if len(ticks) >= 2:                                  # need at least 2 ticks for features
        fired = run_all(
            ticks                  = ticks,
            market_ticker          = market_ticker,
            btc_price              = btc_price,
            target_price           = target_price,
            contract_age_seconds   = contract_age,
            time_remaining_seconds = time_remaining,
            contract_prices        = contract_prices,
        )

        for sig in fired:
            cd_key = f"{sig.rule_name}/{sig.rule_version}/{sig.side}/{market_ticker}"

            if cooldown.is_blocked(cd_key):
                logger.debug("Cooldown — suppressing duplicate signal: %s", cd_key)
                continue

            try:
                signal_id = _insert_signal(sig)
            except Exception as exc:
                logger.error("Failed to insert signal (%s): %s", cd_key, exc)
                continue                         # don't open a trade without a DB signal row

            cooldown.mark(cd_key)
            signals_fired += 1
            logger.info(
                "Signal #%d fired: %s  side=%s  ask=%.4f",
                signal_id, sig.rule_name, sig.side, sig.ask_price,
            )

            try:
                trade_id = trader.open_trade(sig, signal_id, contract_prices)
                if trade_id:
                    trades_opened += 1
            except Exception as exc:
                logger.error("Failed to open paper trade for signal #%d: %s",
                             signal_id, exc)

    # ── 12 + 13. Update open trades; snapshots written inside update() ────────
    trades_closed: list[int] = []
    try:
        trades_closed = trader.update(
            market_ticker          = market_ticker,
            contract_prices        = contract_prices,
            btc_ticks              = ticks,
            btc_price              = btc_price,
            target_price           = target_price,
            time_remaining_seconds = time_remaining,
        )
    except Exception as exc:
        logger.error("Trade update error: %s", exc, exc_info=True)

    # ── Periodic cooldown eviction ────────────────────────────────────────────
    if n % 60 == 0:
        cooldown.evict_expired()

    # ── 14. Console summary ───────────────────────────────────────────────────
    _log_summary(
        n, btc_price, target_price, market_ticker, time_remaining,
        contract_prices, signals_fired, trades_opened,
        trader.open_count, len(trades_closed),
    )


# ── Startup + main loop ───────────────────────────────────────────────────────

def run() -> None:
    global _stop_event

    # Hard guard — belt and suspenders alongside the db.py check.
    if LIVE_TRADING_ENABLED:
        raise RuntimeError(
            "LIVE_TRADING_ENABLED=true is not permitted — "
            "this project is paper-only. Set it to false and restart."
        )

    # Register OS signal handlers.
    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT,  _on_shutdown)

    logger.info("Kalshi signal logger starting (paper-only mode)")

    # ── Connect to MySQL (db.py retries internally) ───────────────────────────
    logger.info("Connecting to MySQL at %s …", "%(host)s:%(port)s/%(db)s" % {
        "host": __import__("app.config", fromlist=["MYSQL_HOST"]).MYSQL_HOST,
        "port": __import__("app.config", fromlist=["MYSQL_PORT"]).MYSQL_PORT,
        "db":   __import__("app.config", fromlist=["MYSQL_DATABASE"]).MYSQL_DATABASE,
    })
    try:
        get_pool()
    except RuntimeError as exc:
        logger.critical("Cannot reach MySQL: %s", exc)
        raise SystemExit(1) from exc

    # ── Initialise stateful components ────────────────────────────────────────
    trader    = PaperTrader(slippage_mode=SLIPPAGE_MODE, position_size=PAPER_POSITION_SIZE)
    btc_ticks: collections.deque[Tick] = collections.deque(maxlen=BTC_TICK_BUFFER)
    cooldown  = _Cooldown(SIGNAL_COOLDOWN_SECONDS)

    _warm_up(btc_ticks)

    logger.info(
        "Running — interval=%.1fs  slippage=%s  position_size=%d  "
        "cooldown=%ds  tick_buffer=%d",
        POLL_INTERVAL_SECONDS, SLIPPAGE_MODE, PAPER_POSITION_SIZE,
        SIGNAL_COOLDOWN_SECONDS, BTC_TICK_BUFFER,
    )

    # ── Main loop ─────────────────────────────────────────────────────────────
    n = 0
    while not _stop_event:
        n += 1
        tick_start = time.monotonic()

        try:
            _tick(n, btc_ticks, cooldown, trader)
        except Exception as exc:
            logger.error("Unhandled error on tick #%d: %s", n, exc, exc_info=True)

        elapsed = time.monotonic() - tick_start
        sleep_for = max(0.0, POLL_INTERVAL_SECONDS - elapsed)

        if elapsed > POLL_INTERVAL_SECONDS:
            logger.warning("Tick #%d took %.2fs (> poll interval %.1fs)",
                           n, elapsed, POLL_INTERVAL_SECONDS)

        # Interruptible sleep — wakes immediately on shutdown signal.
        deadline = time.monotonic() + sleep_for
        while not _stop_event and time.monotonic() < deadline:
            time.sleep(min(0.1, deadline - time.monotonic()))

    logger.info("Signal logger stopped cleanly after %d tick(s)", n)


if __name__ == "__main__":
    run()
