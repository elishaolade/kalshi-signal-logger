"""
app/ws_first_observer.py — WebSocket-first quote observer for clean data runs.

The logger may still use REST for market discovery, but this observer owns the
quote source used by shadow/live diagnostics when enabled. If a fresh WebSocket
book is not available, the tick is skipped instead of silently degrading to a
poll snapshot.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app import config
from app.kalshi_ws import KalshiMarketStream, MarketQuote

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WsObservedPrices:
    prices: dict[str, dict]
    quote_age_seconds: float
    yes_depth_at_bid: float
    no_depth_at_bid: float


class WebSocketFirstObserver:
    def __init__(self, stream: Optional[KalshiMarketStream] = None) -> None:
        self.stream = stream or KalshiMarketStream(enabled=True)
        self._last_missing_log_ts = 0.0
        self._last_invalid_log_ts = 0.0
        self._last_resync_ts: dict[str, float] = {}
        self._crossed_since_ts: dict[str, float] = {}
        self._active_market: Optional[str] = None
        self._last_ready_market: Optional[str] = None

    def start(self) -> None:
        self.stream.start()
        deadline = time.time() + config.MOMENTUM_WS_OBSERVER_BOOT_TIMEOUT_SECONDS
        while time.time() < deadline:
            if self.stream.connected:
                break
            time.sleep(0.1)
        logger.info(
            "WebSocketFirstObserver ready | url=%s require_fresh=%s max_age=%.1fs",
            self.stream._ws_url(),
            config.MOMENTUM_WS_OBSERVER_REQUIRE_FRESH_QUOTES,
            config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS,
        )
        if not self.stream.connected and config.MOMENTUM_WS_OBSERVER_REQUIRE_FRESH_QUOTES:
            raise RuntimeError(
                "WebSocket observer could not connect "
                f"within {config.MOMENTUM_WS_OBSERVER_BOOT_TIMEOUT_SECONDS:.1f}s "
                f"(url={self.stream._ws_url()} last_error={self.stream.last_error()})"
            )

    def stop(self) -> None:
        self.stream.stop()

    def observe_market(self, market_ticker: str) -> Optional[WsObservedPrices]:
        self._set_active_market(market_ticker)
        self.stream.subscribe_market(market_ticker)
        quote = self.stream.get_quote(market_ticker)
        age = self.stream.get_quote_age_seconds(market_ticker)

        usable, reason = self._quote_status(quote, age)
        if not usable:
            if reason == "crossed_book":
                self._log_invalid(market_ticker, quote, age)
                self._resync_crossed_market_if_persistent(market_ticker)
            else:
                self._log_missing(market_ticker, age)
            if config.MOMENTUM_WS_OBSERVER_REQUIRE_FRESH_QUOTES:
                return None
            return None

        assert quote is not None
        assert age is not None
        self._crossed_since_ts.pop(market_ticker, None)
        if self._last_ready_market != market_ticker:
            logger.info(
                "ws observer quotes ready | %s | age=%.3fs YES %.3f/%.3f NO %.3f/%.3f",
                market_ticker,
                age,
                quote.best_yes_bid or 0.0,
                quote.best_yes_ask or 0.0,
                quote.best_no_bid or 0.0,
                quote.best_no_ask or 0.0,
            )
            self._last_ready_market = market_ticker

        yes_depth = (
            quote.get_depth_at_or_better("YES", quote.best_yes_bid)
            if quote.best_yes_bid is not None else 0.0
        )
        no_depth = (
            quote.get_depth_at_or_better("NO", quote.best_no_bid)
            if quote.best_no_bid is not None else 0.0
        )
        return WsObservedPrices(
            prices={
                "YES": self._side_prices(quote, "YES"),
                "NO": self._side_prices(quote, "NO"),
            },
            quote_age_seconds=age,
            yes_depth_at_bid=yes_depth,
            no_depth_at_bid=no_depth,
        )

    def _set_active_market(self, market_ticker: str) -> None:
        if self._active_market == market_ticker:
            return
        previous_market = self._active_market
        self._active_market = market_ticker
        self._last_ready_market = None
        self._crossed_since_ts.clear()
        if previous_market:
            self._last_resync_ts.pop(previous_market, None)
            self.stream.unsubscribe_market(previous_market)
            logger.info(
                "ws observer active market rollover | %s -> %s",
                previous_market,
                market_ticker,
            )

    def _quote_status(self, quote: Optional[MarketQuote], age: Optional[float]) -> tuple[bool, str]:
        if quote is None or age is None:
            return False, "missing"
        if age > config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS:
            return False, "stale"
        values = (
            quote.best_yes_bid,
            quote.best_yes_ask,
            quote.best_no_bid,
            quote.best_no_ask,
        )
        if not all(v is not None for v in values):
            return False, "incomplete"
        assert quote.best_yes_bid is not None
        assert quote.best_yes_ask is not None
        assert quote.best_no_bid is not None
        assert quote.best_no_ask is not None
        if quote.best_yes_ask < quote.best_yes_bid or quote.best_no_ask < quote.best_no_bid:
            return False, "crossed_book"
        return True, "ok"

    def _log_missing(self, market_ticker: str, age: Optional[float]) -> None:
        now = time.time()
        if now - self._last_missing_log_ts < 10.0:
            return
        self._last_missing_log_ts = now
        logger.warning(
            "ws observer waiting for fresh order book | market=%s connected=%s "
            "degraded=%s age=%s last_error=%s",
            market_ticker,
            self.stream.connected,
            self.stream.degraded,
            f"{age:.3f}s" if age is not None else "n/a",
            self.stream.last_error(),
        )

    def _log_invalid(self, market_ticker: str, quote: Optional[MarketQuote], age: Optional[float]) -> None:
        now = time.time()
        if now - self._last_invalid_log_ts < 10.0:
            return
        self._last_invalid_log_ts = now
        logger.warning(
            "ws observer rejected crossed order book | market=%s age=%s "
            "persisted=%.3fs threshold=%.3fs YES %.3f/%.3f NO %.3f/%.3f",
            market_ticker,
            f"{age:.3f}s" if age is not None else "n/a",
            self._crossed_duration(market_ticker, now),
            config.MOMENTUM_WS_OBSERVER_CROSSED_RESYNC_SECONDS,
            (quote.best_yes_bid if quote and quote.best_yes_bid is not None else 0.0),
            (quote.best_yes_ask if quote and quote.best_yes_ask is not None else 0.0),
            (quote.best_no_bid if quote and quote.best_no_bid is not None else 0.0),
            (quote.best_no_ask if quote and quote.best_no_ask is not None else 0.0),
        )

    def _resync_crossed_market_if_persistent(self, market_ticker: str) -> None:
        now = time.time()
        first_crossed_at = self._crossed_since_ts.setdefault(market_ticker, now)
        persisted = now - first_crossed_at
        if persisted < config.MOMENTUM_WS_OBSERVER_CROSSED_RESYNC_SECONDS:
            return
        logger.info(
            "ws observer resyncing persistent crossed order book | market=%s "
            "persisted=%.3fs threshold=%.3fs",
            market_ticker,
            persisted,
            config.MOMENTUM_WS_OBSERVER_CROSSED_RESYNC_SECONDS,
        )
        self._resync_market(market_ticker)
        self._crossed_since_ts[market_ticker] = now

    def _crossed_duration(self, market_ticker: str, now: float) -> float:
        first_crossed_at = self._crossed_since_ts.get(market_ticker, now)
        return max(0.0, now - first_crossed_at)

    def _resync_market(self, market_ticker: str) -> None:
        now = time.time()
        last = self._last_resync_ts.get(market_ticker, 0.0)
        if now - last < 5.0:
            return
        self._last_resync_ts[market_ticker] = now
        self.stream.reset_market(market_ticker)

    @staticmethod
    def _side_prices(quote: MarketQuote, side: str) -> dict:
        bid = quote.get_best_bid(side)
        ask = quote.get_best_ask(side)
        spread = quote.get_spread(side)
        return {
            "bid_price": bid,
            "ask_price": ask,
            "spread": spread,
            "source": "websocket",
        }
