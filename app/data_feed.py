"""
data_feed.py

Real data:
  - BTC/USD spot price from Kraken REST by default, Kraken WebSocket when
    BTC_PRICE_SOURCE=kraken_ws, or a configured CF Benchmarks websocket
    endpoint when BTC_PRICE_SOURCE=cf_benchmark_ws
  - Kalshi production REST API for market and contract data
    Authenticated via RSA key pair when KALSHI_KEY_ID + KALSHI_KEY_FILE are set.
    Falls back to unauthenticated (public endpoints only) when keys are absent.

Development-only mock support:
  - BTC price random walk, seeded from Kraken when available

The logger itself now fails closed on market/contract data. Unused mock
market/contract generators are intentionally removed so the ingestion path stays
easy to reason about and clearly separated from synthetic data.
"""

import asyncio
import base64
import inspect
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
try:
    import websockets
except Exception:  # pragma: no cover - optional runtime dependency
    websockets = None

from app.config import (
    BTC_PRICE_SOURCE,
    CF_BENCHMARK_WS_BOOT_TIMEOUT_SECONDS,
    CF_BENCHMARK_WS_FALLBACK_TO_KRAKEN,
    CF_BENCHMARK_WS_HEADERS_JSON,
    CF_BENCHMARK_WS_PING_INTERVAL_SECONDS,
    CF_BENCHMARK_WS_PING_TIMEOUT_SECONDS,
    CF_BENCHMARK_WS_PRICE_JSON_PATH,
    CF_BENCHMARK_WS_RECONNECT_SECONDS,
    CF_BENCHMARK_WS_STALE_AFTER_SECONDS,
    CF_BENCHMARK_WS_SUBSCRIBE_MESSAGE,
    CF_BENCHMARK_WS_SYMBOL,
    CF_BENCHMARK_WS_SYMBOL_JSON_PATH,
    CF_BENCHMARK_WS_URL,
    KRAKEN_WS_BOOT_TIMEOUT_SECONDS,
    KRAKEN_WS_EVENT_TRIGGER,
    KRAKEN_WS_FALLBACK_TO_REST,
    KRAKEN_WS_PING_INTERVAL_SECONDS,
    KRAKEN_WS_PING_TIMEOUT_SECONDS,
    KRAKEN_WS_PRICE_MODE,
    KRAKEN_WS_RECONNECT_SECONDS,
    KRAKEN_WS_STALE_AFTER_SECONDS,
    KRAKEN_WS_SYMBOL,
    KRAKEN_WS_URL,
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
_KRAKEN_WS_SOURCE_NAME = "kraken_ws"
_kraken_stream: Optional["_KrakenTickerStream"] = None
_kraken_stream_lock = threading.Lock()

# ── CF Benchmarks websocket config ───────────────────────────────────────────

_CF_BENCHMARK_SOURCE_NAME = "cf_benchmark_ws"
_last_btc_price_source = "unknown"
_cf_stream: Optional["_CFBenchmarkPriceStream"] = None
_cf_stream_lock = threading.Lock()

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


def _set_btc_source(name: str) -> None:
    global _last_btc_price_source
    _last_btc_price_source = name


def get_btc_price_source() -> str:
    """Return the source used by the most recent successful ``get_btc_price``."""
    return _last_btc_price_source


def _parse_ws_headers() -> dict[str, str]:
    if not CF_BENCHMARK_WS_HEADERS_JSON:
        return {}
    try:
        parsed = json.loads(CF_BENCHMARK_WS_HEADERS_JSON)
    except Exception as exc:
        raise RuntimeError(f"CF_BENCHMARK_WS_HEADERS_JSON is invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("CF_BENCHMARK_WS_HEADERS_JSON must be a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}


def _ws_connect_kwargs(headers: dict[str, str]) -> dict[str, Any]:
    if not headers or websockets is None:
        return {}
    params = inspect.signature(websockets.connect).parameters
    if "additional_headers" in params:
        return {"additional_headers": headers}
    if "extra_headers" in params:
        return {"extra_headers": headers}
    return {}


def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _parse_numeric(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_price_from_message(message: Any) -> Optional[float]:
    if CF_BENCHMARK_WS_SYMBOL and CF_BENCHMARK_WS_SYMBOL_JSON_PATH:
        symbol = _get_path(message, CF_BENCHMARK_WS_SYMBOL_JSON_PATH)
        if symbol is not None and str(symbol) != CF_BENCHMARK_WS_SYMBOL:
            return None

    if CF_BENCHMARK_WS_PRICE_JSON_PATH:
        return _parse_numeric(_get_path(message, CF_BENCHMARK_WS_PRICE_JSON_PATH))

    if isinstance(message, (int, float, str)):
        return _parse_numeric(message)
    if isinstance(message, list):
        for item in message:
            price = _extract_price_from_message(item)
            if price is not None:
                return price
        return None

    for path in (
        "price",
        "value",
        "index",
        "rate",
        "last",
        "last_price",
        "data.price",
        "data.value",
        "data.index",
        "payload.price",
        "payload.value",
        "result.price",
        "result.value",
    ):
        price = _parse_numeric(_get_path(message, path))
        if price is not None:
            return price
    return None


class _CFBenchmarkPriceStream:
    """Small websocket cache for the configured CF Benchmarks BTC price feed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._price: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._thread_main,
            name="cf-benchmark-btc-ws",
            daemon=True,
        )
        self._thread.start()

    def latest(self) -> tuple[Optional[float], Optional[float], Optional[str]]:
        with self._lock:
            return self._price, self._last_ts, self._last_error

    def _set_price(self, price: float) -> None:
        with self._lock:
            self._price = round(price, 2)
            self._last_ts = time.time()
            self._last_error = None

    def _set_error(self, exc: Exception | str) -> None:
        with self._lock:
            self._last_error = str(exc)

    def _thread_main(self) -> None:  # pragma: no cover - runtime wrapper
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self._set_error(exc)
            logger.error("CF Benchmarks BTC websocket fatal error: %s", exc, exc_info=True)

    async def _run_forever(self) -> None:  # pragma: no cover - runtime wrapper
        if websockets is None:
            raise RuntimeError("websockets package not available")
        if not CF_BENCHMARK_WS_URL:
            raise RuntimeError("CF_BENCHMARK_WS_URL is required when BTC_PRICE_SOURCE=cf_benchmark_ws")

        headers = _parse_ws_headers()
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    CF_BENCHMARK_WS_URL,
                    **_ws_connect_kwargs(headers),
                    ping_interval=CF_BENCHMARK_WS_PING_INTERVAL_SECONDS,
                    ping_timeout=CF_BENCHMARK_WS_PING_TIMEOUT_SECONDS,
                    open_timeout=5.0,
                    close_timeout=2.0,
                ) as ws:
                    logger.info("CF Benchmarks BTC websocket connected")
                    if CF_BENCHMARK_WS_SUBSCRIBE_MESSAGE:
                        try:
                            subscribe = json.loads(CF_BENCHMARK_WS_SUBSCRIBE_MESSAGE)
                        except Exception:
                            subscribe = CF_BENCHMARK_WS_SUBSCRIBE_MESSAGE
                        await ws.send(json.dumps(subscribe) if not isinstance(subscribe, str) else subscribe)
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(
                            ws.recv(),
                            timeout=max(CF_BENCHMARK_WS_PING_INTERVAL_SECONDS, 5.0),
                        )
                        try:
                            message = json.loads(raw)
                        except Exception:
                            message = raw
                        price = _extract_price_from_message(message)
                        if price is not None:
                            self._set_price(price)
            except Exception as exc:
                self._set_error(exc)
                logger.warning("CF Benchmarks BTC websocket reconnecting after error: %s", exc)
                await asyncio.sleep(CF_BENCHMARK_WS_RECONNECT_SECONDS)


def _get_cf_stream() -> _CFBenchmarkPriceStream:
    global _cf_stream
    with _cf_stream_lock:
        if _cf_stream is None:
            _cf_stream = _CFBenchmarkPriceStream()
            _cf_stream.start()
        return _cf_stream


def _get_cf_benchmark_ws_price() -> float:
    stream = _get_cf_stream()
    deadline = time.time() + max(0.0, CF_BENCHMARK_WS_BOOT_TIMEOUT_SECONDS)
    while True:
        price, ts, last_error = stream.latest()
        if price is not None and ts is not None:
            age = time.time() - ts
            if age <= CF_BENCHMARK_WS_STALE_AFTER_SECONDS:
                logger.debug("CF Benchmarks BTC/USD: %.2f age=%.3fs", price, age)
                return price
            raise RuntimeError(
                f"CF Benchmarks BTC websocket price stale "
                f"(age={age:.2f}s > {CF_BENCHMARK_WS_STALE_AFTER_SECONDS:.2f}s)"
            )
        if time.time() >= deadline:
            raise RuntimeError(
                "CF Benchmarks BTC websocket has no price yet"
                + (f" (last_error={last_error})" if last_error else "")
            )
        time.sleep(0.05)


class _KrakenTickerStream:
    """Small websocket cache for Kraken public BTC/USD ticker updates."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._price: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._last_error: Optional[str] = None
        self._bid: Optional[float] = None
        self._ask: Optional[float] = None
        self._last: Optional[float] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._thread_main,
            name="kraken-btc-ws",
            daemon=True,
        )
        self._thread.start()

    def latest(self) -> tuple[Optional[float], Optional[float], Optional[str]]:
        with self._lock:
            return self._price, self._last_ts, self._last_error

    def _set_ticker(
        self,
        *,
        bid: Optional[float],
        ask: Optional[float],
        last: Optional[float],
    ) -> None:
        with self._lock:
            if bid is not None:
                self._bid = bid
            if ask is not None:
                self._ask = ask
            if last is not None:
                self._last = last

            price = self._select_price()
            if price is not None:
                self._price = round(price, 2)
                self._last_ts = time.time()
                self._last_error = None

    def _select_price(self) -> Optional[float]:
        mode = KRAKEN_WS_PRICE_MODE
        if mode == "bid":
            return self._bid
        if mode == "ask":
            return self._ask
        if mode == "last":
            return self._last
        if self._bid is not None and self._ask is not None:
            return (self._bid + self._ask) / 2.0
        return self._last or self._bid or self._ask

    def _set_error(self, exc: Exception | str) -> None:
        with self._lock:
            self._last_error = str(exc)

    def _thread_main(self) -> None:  # pragma: no cover - runtime wrapper
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self._set_error(exc)
            logger.error("Kraken BTC websocket fatal error: %s", exc, exc_info=True)

    async def _run_forever(self) -> None:  # pragma: no cover - runtime wrapper
        if websockets is None:
            raise RuntimeError("websockets package not available")

        subscribe = {
            "method": "subscribe",
            "params": {
                "channel": "ticker",
                "symbol": [KRAKEN_WS_SYMBOL],
                "event_trigger": KRAKEN_WS_EVENT_TRIGGER,
                "snapshot": True,
            },
        }

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    KRAKEN_WS_URL,
                    ping_interval=KRAKEN_WS_PING_INTERVAL_SECONDS,
                    ping_timeout=KRAKEN_WS_PING_TIMEOUT_SECONDS,
                    open_timeout=5.0,
                    close_timeout=2.0,
                ) as ws:
                    logger.info(
                        "Kraken BTC websocket connected | symbol=%s mode=%s trigger=%s",
                        KRAKEN_WS_SYMBOL,
                        KRAKEN_WS_PRICE_MODE,
                        KRAKEN_WS_EVENT_TRIGGER,
                    )
                    await ws.send(json.dumps(subscribe))
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(
                            ws.recv(),
                            timeout=max(KRAKEN_WS_PING_INTERVAL_SECONDS, 5.0),
                        )
                        try:
                            message = json.loads(raw)
                        except Exception:
                            continue
                        self._ingest_message(message)
            except Exception as exc:
                self._set_error(exc)
                logger.warning("Kraken BTC websocket reconnecting after error: %s", exc)
                await asyncio.sleep(KRAKEN_WS_RECONNECT_SECONDS)

    def _ingest_message(self, message: Any) -> None:
        rows = None
        if isinstance(message, dict):
            channel = message.get("channel")
            if channel and channel != "ticker":
                return
            rows = message.get("data")
        elif isinstance(message, list):
            rows = message
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return

        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if symbol and str(symbol) != KRAKEN_WS_SYMBOL:
                continue
            self._set_ticker(
                bid=_parse_numeric(row.get("bid")),
                ask=_parse_numeric(row.get("ask")),
                last=_parse_numeric(row.get("last")),
            )


def _get_kraken_stream() -> _KrakenTickerStream:
    global _kraken_stream
    with _kraken_stream_lock:
        if _kraken_stream is None:
            _kraken_stream = _KrakenTickerStream()
            _kraken_stream.start()
        return _kraken_stream


def _get_kraken_ws_price() -> float:
    stream = _get_kraken_stream()
    deadline = time.time() + max(0.0, KRAKEN_WS_BOOT_TIMEOUT_SECONDS)
    while True:
        price, ts, last_error = stream.latest()
        if price is not None and ts is not None:
            age = time.time() - ts
            if age <= KRAKEN_WS_STALE_AFTER_SECONDS:
                logger.debug("Kraken WS BTC/USD: %.2f age=%.3fs", price, age)
                return price
            raise RuntimeError(
                f"Kraken BTC websocket price stale "
                f"(age={age:.2f}s > {KRAKEN_WS_STALE_AFTER_SECONDS:.2f}s)"
            )
        if time.time() >= deadline:
            raise RuntimeError(
                "Kraken BTC websocket has no price yet"
                + (f" (last_error={last_error})" if last_error else "")
            )
        time.sleep(0.05)


def _get_kraken_btc_price() -> float:
    with httpx.Client(timeout=5.0) as client:
        response = client.get(_KRAKEN_URL, params={"pair": _KRAKEN_PAIR})
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise ValueError(f"Kraken API error: {data['error']}")
        # 'c' field is [last_trade_price, lot_volume]
        price_str = data["result"][_KRAKEN_RESULT_KEY]["c"][0]
        return float(price_str)


# ── Public API ────────────────────────────────────────────────────────────────

def get_btc_price(*, allow_mock: bool = True) -> float:
    """
    Return current BTC/USD price.

    Uses the configured BTC source.  Kraken public REST remains the default.
    When BTC_PRICE_SOURCE=kraken_ws, a local websocket ticker cache is used and
    no REST BTC price is read unless KRAKEN_WS_FALLBACK_TO_REST=true.
    When BTC_PRICE_SOURCE=cf_benchmark_ws, a local websocket cache is used and
    no REST BTC price is read unless CF_BENCHMARK_WS_FALLBACK_TO_KRAKEN=true.

    When ``allow_mock`` is true, any failure falls back to the module-level
    mock random walk so development tooling can keep running.  When false,
    the exception is raised so callers can fail closed instead of inventing
    data.
    """
    try:
        if BTC_PRICE_SOURCE in ("kraken_ws", "kraken_websocket"):
            try:
                price = _get_kraken_ws_price()
                _set_btc_source(_KRAKEN_WS_SOURCE_NAME)
                return _advance_mock_price(anchor=price)  # keep mock in sync
            except Exception:
                if not KRAKEN_WS_FALLBACK_TO_REST:
                    raise
                logger.warning("Kraken BTC websocket unavailable; falling back to Kraken REST")

        if BTC_PRICE_SOURCE in ("cf_benchmark_ws", "cfbenchmarks_ws", "cf_ws"):
            try:
                price = _get_cf_benchmark_ws_price()
                _set_btc_source(_CF_BENCHMARK_SOURCE_NAME)
                return _advance_mock_price(anchor=price)  # keep mock in sync
            except Exception:
                if not CF_BENCHMARK_WS_FALLBACK_TO_KRAKEN:
                    raise
                logger.warning("CF Benchmarks BTC websocket unavailable; falling back to Kraken")

        price = _get_kraken_btc_price()
        logger.debug("Kraken BTC/USD: %.2f", price)
        _set_btc_source("kraken")
        return _advance_mock_price(anchor=price)   # keep mock in sync

    except Exception as exc:
        if not allow_mock:
            raise RuntimeError(f"BTC/USD fetch failed from {BTC_PRICE_SOURCE}: {exc}") from exc
        fallback = _advance_mock_price()
        _set_btc_source("mock")
        logger.warning(
            "BTC fetch failed from %s (%s) — using mock BTC price %.2f",
            BTC_PRICE_SOURCE, exc, fallback,
        )
        return fallback


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
