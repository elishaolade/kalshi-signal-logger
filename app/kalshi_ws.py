"""
app/kalshi_ws.py — Optional Kalshi WebSocket observer for live trading.

This module is deliberately conservative:
  - REST remains the source of truth for order placement/cancel.
  - WebSocket state is treated as an acceleration layer for quotes and
    order-status/fill awareness.
  - Disconnects never crash the trader. They mark the stream stale and let the
    caller fall back to REST / polled quotes.

Because Kalshi can evolve payload shapes, the message parsers are intentionally
shape-tolerant and best-effort. Unknown messages are ignored rather than
raising. The live trader only uses cached values when they are fresh.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from app import config
from app.data_feed import _kalshi_auth_headers

logger = logging.getLogger(__name__)

try:
    import websockets
except Exception:  # pragma: no cover - optional dependency guard
    websockets = None


def infer_yes_ask(best_no_bid: Optional[float]) -> Optional[float]:
    if best_no_bid is None:
        return None
    return round(max(0.01, min(0.99, 1.0 - float(best_no_bid))), 4)


def infer_no_ask(best_yes_bid: Optional[float]) -> Optional[float]:
    if best_yes_bid is None:
        return None
    return round(max(0.01, min(0.99, 1.0 - float(best_yes_bid))), 4)


def compute_spread(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if best_bid is None or best_ask is None:
        return None
    return round(float(best_ask) - float(best_bid), 4)


def compute_depth_at_or_better(levels: dict[float, float], price: float) -> float:
    total = 0.0
    for level_price, size in levels.items():
        if level_price >= price:
            total += float(size)
    return round(total, 4)


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_ticker(message: dict[str, Any]) -> Optional[str]:
    for key in ("ticker", "market_ticker", "marketTicker", "sid", "symbol"):
        value = message.get(key)
        if value:
            return str(value)
    data = message.get("data")
    if isinstance(data, dict):
        for key in ("ticker", "market_ticker", "marketTicker", "sid", "symbol"):
            value = data.get(key)
            if value:
                return str(value)
    return None


def _iter_levels(value: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if isinstance(value, dict):
        for price, size in value.items():
            p = _to_float(price)
            s = _to_float(size)
            if p is not None and s is not None and s > 0:
                out.append((round(p, 4), s))
        return out
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                p = _to_float(item.get("price") or item.get("px") or item.get("rate"))
                s = _to_float(item.get("size") or item.get("count") or item.get("quantity"))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                p = _to_float(item[0])
                s = _to_float(item[1])
            else:
                continue
            if p is not None and s is not None and s > 0:
                out.append((round(p, 4), s))
    return out


def _extract_book_sides(message: dict[str, Any]) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """
    Return (yes_bids, no_bids) from a permissive set of payload shapes.
    """
    yes_src = (
        message.get("yes_bids")
        or message.get("yes")
        or message.get("bids_yes")
    )
    no_src = (
        message.get("no_bids")
        or message.get("no")
        or message.get("bids_no")
    )
    data = message.get("data")
    if isinstance(data, dict):
        yes_src = yes_src or data.get("yes_bids") or data.get("yes") or data.get("bids_yes")
        no_src = no_src or data.get("no_bids") or data.get("no") or data.get("bids_no")
        if yes_src is None and isinstance(data.get("yes"), dict):
            yes_src = data.get("yes")
        if no_src is None and isinstance(data.get("no"), dict):
            no_src = data.get("no")
    return _iter_levels(yes_src), _iter_levels(no_src)


@dataclass
class WSOrderState:
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: Optional[str] = None
    remaining_count: Optional[float] = None
    filled_count: Optional[float] = None
    avg_fill_price: Optional[float] = None
    fee_total: Optional[float] = None
    detected_by: Optional[str] = None
    updated_at_ts: float = 0.0

    @property
    def updated_at(self) -> Optional[datetime]:
        if self.updated_at_ts <= 0:
            return None
        return datetime.fromtimestamp(self.updated_at_ts, tz=timezone.utc)


@dataclass
class MarketQuote:
    market_ticker: str
    yes_bids: dict[float, float] = field(default_factory=dict)
    no_bids: dict[float, float] = field(default_factory=dict)
    best_yes_bid: Optional[float] = None
    best_no_bid: Optional[float] = None
    best_yes_ask: Optional[float] = None
    best_no_ask: Optional[float] = None
    updated_at_ts: float = 0.0

    def refresh_derived(self) -> None:
        self.best_yes_bid = max(self.yes_bids.keys()) if self.yes_bids else None
        self.best_no_bid = max(self.no_bids.keys()) if self.no_bids else None
        self.best_yes_ask = infer_yes_ask(self.best_no_bid)
        self.best_no_ask = infer_no_ask(self.best_yes_bid)

    def get_best_bid(self, side: str) -> Optional[float]:
        return self.best_yes_bid if side == "YES" else self.best_no_bid

    def get_best_ask(self, side: str) -> Optional[float]:
        return self.best_yes_ask if side == "YES" else self.best_no_ask

    def get_spread(self, side: str) -> Optional[float]:
        return compute_spread(self.get_best_bid(side), self.get_best_ask(side))

    def get_depth_at_or_better(self, side: str, price: float) -> float:
        levels = self.yes_bids if side == "YES" else self.no_bids
        return compute_depth_at_or_better(levels, price)


class KalshiMarketStream:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[str, MarketQuote] = {}
        self._order_states: dict[str, WSOrderState] = {}
        self._client_index: dict[str, str] = {}
        self._subscribed_markets: set[str] = set()
        self._ws_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = False
        self._last_message_ts = 0.0
        self._last_error: Optional[str] = None
        self._supports_runtime = websockets is not None
        self._degraded = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not config.MOMENTUM_LIVE_USE_WEBSOCKET:
            return
        if not self._supports_runtime:
            self._degraded = True
            self._last_error = "websockets package not available"
            logger.warning("KalshiMarketStream disabled: %s", self._last_error)
            return
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop.clear()
        self._ws_thread = threading.Thread(
            target=self._thread_main,
            name="kalshi-market-stream",
            daemon=True,
        )
        self._ws_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2.0)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def degraded(self) -> bool:
        return self._degraded or self.is_stale()

    def is_stale(self) -> bool:
        last = self._last_message_ts
        if last <= 0:
            return True
        return (time.time() - last) > config.MOMENTUM_LIVE_WS_STALE_AFTER_SECONDS

    def last_error(self) -> Optional[str]:
        return self._last_error

    # ── Public cache interface ───────────────────────────────────────────────

    def subscribe_market(self, market_ticker: str) -> None:
        if not market_ticker:
            return
        with self._lock:
            self._subscribed_markets.add(market_ticker)

    def unsubscribe_market(self, market_ticker: str) -> None:
        with self._lock:
            self._subscribed_markets.discard(market_ticker)

    def get_quote(self, market_ticker: str) -> Optional[MarketQuote]:
        with self._lock:
            q = self._quotes.get(market_ticker)
            if not q:
                return None
            clone = MarketQuote(
                market_ticker=q.market_ticker,
                yes_bids=dict(q.yes_bids),
                no_bids=dict(q.no_bids),
                best_yes_bid=q.best_yes_bid,
                best_no_bid=q.best_no_bid,
                best_yes_ask=q.best_yes_ask,
                best_no_ask=q.best_no_ask,
                updated_at_ts=q.updated_at_ts,
            )
            return clone

    def get_best_bid(self, market_ticker: str, side: str) -> Optional[float]:
        q = self.get_quote(market_ticker)
        return q.get_best_bid(side) if q else None

    def get_best_ask(self, market_ticker: str, side: str) -> Optional[float]:
        q = self.get_quote(market_ticker)
        return q.get_best_ask(side) if q else None

    def get_spread(self, market_ticker: str, side: str) -> Optional[float]:
        q = self.get_quote(market_ticker)
        return q.get_spread(side) if q else None

    def get_depth_at_or_better(self, market_ticker: str, side: str, price: float) -> float:
        q = self.get_quote(market_ticker)
        return q.get_depth_at_or_better(side, price) if q else 0.0

    def get_quote_age_seconds(self, market_ticker: str) -> Optional[float]:
        q = self.get_quote(market_ticker)
        if not q or q.updated_at_ts <= 0:
            return None
        return max(0.0, time.time() - q.updated_at_ts)

    def get_order_state(
        self, order_id: Optional[str] = None, client_order_id: Optional[str] = None
    ) -> Optional[WSOrderState]:
        with self._lock:
            if order_id:
                state = self._order_states.get(order_id)
                if state:
                    return WSOrderState(**state.__dict__)
            if client_order_id:
                oid = self._client_index.get(client_order_id)
                if oid and oid in self._order_states:
                    return WSOrderState(**self._order_states[oid].__dict__)
        return None

    # ── Test / parser entrypoints ────────────────────────────────────────────

    def apply_orderbook_snapshot(self, market_ticker: str, yes_bids: list[tuple[float, float]], no_bids: list[tuple[float, float]], *, updated_at_ts: Optional[float] = None) -> None:
        with self._lock:
            quote = self._quotes.get(market_ticker) or MarketQuote(market_ticker=market_ticker)
            quote.yes_bids = {round(p, 4): float(s) for p, s in yes_bids if float(s) > 0}
            quote.no_bids = {round(p, 4): float(s) for p, s in no_bids if float(s) > 0}
            quote.updated_at_ts = updated_at_ts or time.time()
            quote.refresh_derived()
            self._quotes[market_ticker] = quote
            self._last_message_ts = quote.updated_at_ts

    def apply_orderbook_delta(self, market_ticker: str, side: str, levels: list[tuple[float, float]], *, updated_at_ts: Optional[float] = None) -> None:
        with self._lock:
            quote = self._quotes.get(market_ticker) or MarketQuote(market_ticker=market_ticker)
            book = quote.yes_bids if side == "YES" else quote.no_bids
            for price, size in levels:
                p = round(float(price), 4)
                s = float(size)
                if s <= 0:
                    book.pop(p, None)
                else:
                    book[p] = s
            quote.updated_at_ts = updated_at_ts or time.time()
            quote.refresh_derived()
            self._quotes[market_ticker] = quote
            self._last_message_ts = quote.updated_at_ts

    def apply_order_update(
        self,
        *,
        order_id: Optional[str],
        client_order_id: Optional[str],
        status: Optional[str],
        remaining_count: Optional[float],
        filled_count: Optional[float],
        avg_fill_price: Optional[float],
        fee_total: Optional[float],
        detected_by: str,
        updated_at_ts: Optional[float] = None,
    ) -> None:
        if not order_id and not client_order_id:
            return
        ts = updated_at_ts or time.time()
        oid = order_id or self._client_index.get(client_order_id or "")
        if not oid:
            oid = f"coid:{client_order_id}"
        with self._lock:
            state = self._order_states.get(oid) or WSOrderState(order_id=order_id, client_order_id=client_order_id)
            state.order_id = order_id or state.order_id
            state.client_order_id = client_order_id or state.client_order_id
            state.status = status or state.status
            state.remaining_count = remaining_count if remaining_count is not None else state.remaining_count
            state.filled_count = filled_count if filled_count is not None else state.filled_count
            state.avg_fill_price = avg_fill_price if avg_fill_price is not None else state.avg_fill_price
            state.fee_total = fee_total if fee_total is not None else state.fee_total
            state.detected_by = detected_by
            state.updated_at_ts = ts
            self._order_states[oid] = state
            if state.client_order_id:
                self._client_index[state.client_order_id] = oid
            self._last_message_ts = ts

    def ingest_message(self, message: dict[str, Any]) -> None:
        """
        Best-effort message parser for orderbook and user-order/fill payloads.
        Unknown shapes are ignored.
        """
        if not isinstance(message, dict):
            return
        ticker = _extract_ticker(message)
        channel = str(message.get("channel") or message.get("type") or "").lower()

        yes_bids, no_bids = _extract_book_sides(message)
        if ticker and (yes_bids or no_bids):
            if "snapshot" in channel or message.get("snapshot") is True:
                self.apply_orderbook_snapshot(ticker, yes_bids, no_bids)
            else:
                if yes_bids:
                    self.apply_orderbook_delta(ticker, "YES", yes_bids)
                if no_bids:
                    self.apply_orderbook_delta(ticker, "NO", no_bids)

        order = message.get("order")
        data = message.get("data")
        payload = order if isinstance(order, dict) else (data if isinstance(data, dict) else message)
        order_id = payload.get("order_id") or payload.get("id")
        client_order_id = payload.get("client_order_id")
        if order_id or client_order_id:
            status = payload.get("status") or payload.get("state") or payload.get("event_type")
            remaining = _to_float(
                payload.get("remaining_count_fp")
                or payload.get("remaining_count")
                or payload.get("remaining")
            )
            filled = _to_float(
                payload.get("fill_count_fp")
                or payload.get("filled_count")
                or payload.get("fill_count")
                or payload.get("count_filled")
            )
            fee_total = _to_float(payload.get("fee_cost") or payload.get("fee") or payload.get("fee_dollars"))
            avg_fill = None
            for key in ("yes_price_dollars", "no_price_dollars", "avg_fill_price"):
                avg_fill = _to_float(payload.get(key))
                if avg_fill is not None:
                    break
            self.apply_order_update(
                order_id=str(order_id) if order_id else None,
                client_order_id=str(client_order_id) if client_order_id else None,
                status=str(status) if status is not None else None,
                remaining_count=remaining,
                filled_count=filled,
                avg_fill_price=avg_fill,
                fee_total=fee_total,
                detected_by="websocket",
            )

    # ── Background runtime ───────────────────────────────────────────────────

    def _thread_main(self) -> None:  # pragma: no cover - runtime wrapper
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self._degraded = True
            self._last_error = str(exc)
            logger.error("KalshiMarketStream fatal error: %s", exc, exc_info=True)

    async def _run_forever(self) -> None:  # pragma: no cover - runtime wrapper
        while not self._stop.is_set():
            try:
                url = self._ws_url()
                headers = self._ws_headers(url)
                async with websockets.connect(
                    url,
                    extra_headers=headers,
                    ping_interval=config.MOMENTUM_LIVE_WS_PING_INTERVAL_SECONDS,
                    ping_timeout=config.MOMENTUM_LIVE_WS_PING_TIMEOUT_SECONDS,
                    open_timeout=config.KALSHI_API_TIMEOUT_SECONDS,
                    close_timeout=2.0,
                ) as ws:
                    self._connected = True
                    self._degraded = False
                    self._last_error = None
                    await self._resubscribe(ws)
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(
                            ws.recv(),
                            timeout=max(config.MOMENTUM_LIVE_WS_PING_INTERVAL_SECONDS, 5.0),
                        )
                        self._last_message_ts = time.time()
                        try:
                            message = json.loads(raw)
                        except Exception:
                            continue
                        self.ingest_message(message)
            except Exception as exc:
                self._connected = False
                self._degraded = True
                self._last_error = str(exc)
                logger.warning("KalshiMarketStream reconnecting after error: %s", exc)
                await asyncio.sleep(config.MOMENTUM_LIVE_WS_RECONNECT_SECONDS)
        self._connected = False

    def _ws_url(self) -> str:
        if config.KALSHI_WS_URL:
            return config.KALSHI_WS_URL
        split = urlsplit(config.KALSHI_API_BASE)
        scheme = "wss" if split.scheme == "https" else "ws"
        path = split.path.rstrip("/")
        if path.endswith("/v2"):
            path = path[:-3] + "/ws/v2"
        else:
            path = path + "/ws"
        return urlunsplit((scheme, split.netloc, path, "", ""))

    def _ws_headers(self, url: str) -> dict[str, str]:
        path = urlsplit(url).path
        headers = {"Content-Type": "application/json"}
        try:
            headers.update(_kalshi_auth_headers("GET", path))
        except Exception as exc:
            logger.warning("KalshiMarketStream auth headers unavailable: %s", exc)
        return headers

    async def _resubscribe(self, ws) -> None:  # pragma: no cover - runtime wrapper
        with self._lock:
            tickers = sorted(self._subscribed_markets)
        for ticker in tickers:
            payloads = [
                {"type": "subscribe", "channel": "orderbook", "ticker": ticker},
                {"type": "subscribe", "channel": "market_status", "ticker": ticker},
                {"type": "subscribe", "channel": "trades", "ticker": ticker},
            ]
            for payload in payloads:
                await ws.send(json.dumps(payload))
        await ws.send(json.dumps({"type": "subscribe", "channel": "user_orders"}))

