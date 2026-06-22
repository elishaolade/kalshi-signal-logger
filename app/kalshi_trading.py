"""
app/kalshi_trading.py — Small, conservative authenticated Kalshi trading client.

REAL-MONEY CAPABILITY.  This module can place and cancel live orders.  It is
used ONLY by app/momentum_live_trader.py, which gates every call behind explicit
MOMENTUM_LIVE_* flags and risk checks.  Nothing here is invoked by the research
logger or the shadow tracker.

Design goals
------------
- Explicit over clever.  One method per Kalshi endpoint we need.
- Conservative.  Reuses the exact RSA-PSS request signing already used for
  read-only Kalshi calls in app/data_feed (single private-key load path).
- Honest.  Returns the raw Kalshi payloads.  Never fabricates fills, prices, or
  order state.  Network/HTTP errors propagate to the caller (the live trader
  catches, logs, and retries on the next tick).

Price units
-----------
The rest of the system uses *dollar fractions* in [0.01, 0.99] (e.g. 0.42).
Kalshi's V2 event-order API expects *fixed-point dollar strings* from the YES
side of the book (e.g. "0.4200"). When our strategy wants to trade the NO leg,
we translate it into the economically-equivalent YES-book order:

    buy  YES @ p   -> side=bid, price=p
    sell YES @ p   -> side=ask, price=p
    buy  NO  @ p   -> side=ask, price=1-p
    sell NO  @ p   -> side=bid, price=1-p
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from app.config import (
    KALSHI_API_BASE,
    KALSHI_API_TIMEOUT_SECONDS,
    KALSHI_KEY_ID,
)
# Reuse the single private-key load + signing path from data_feed so there is
# exactly one place that touches the RSA key material.
from app.data_feed import _kalshi_auth_headers, _KALSHI_PRIVATE_KEY

logger = logging.getLogger(__name__)

# API path prefix (e.g. "/trade-api/v2") parsed from KALSHI_API_BASE.  Kalshi
# signs the FULL request path including this prefix, so we must include it when
# building auth headers for trading endpoints.
_API_PATH_PREFIX = urlsplit(KALSHI_API_BASE).path.rstrip("/")


def dollars_to_cents(price: float) -> int:
    """Compatibility helper: convert a dollar fraction to integer cents."""
    cents = int(round(price * 100))
    return max(1, min(99, cents))


def cents_to_dollars(cents: Any) -> Optional[float]:
    """Convert Kalshi integer cents back to a dollar fraction. None-safe."""
    if cents is None:
        return None
    try:
        return round(float(cents) / 100.0, 4)
    except (TypeError, ValueError):
        return None


def is_authenticated() -> bool:
    """True only when an RSA private key and key id are loaded for signing."""
    return _KALSHI_PRIVATE_KEY is not None and bool(KALSHI_KEY_ID)


class KalshiTradingError(RuntimeError):
    """Raised for non-2xx responses from the Kalshi trading API."""


class KalshiTradingClient:
    """
    Minimal authenticated wrapper over the Kalshi portfolio/order endpoints.

    Every request is RSA-PSS signed.  Construction does not place any order; it
    only verifies that authentication is available so the live trader can fail
    closed early if keys are missing.
    """

    def __init__(self, *, require_auth: bool = True) -> None:
        self._base = KALSHI_API_BASE.rstrip("/")
        if require_auth and not is_authenticated():
            raise KalshiTradingError(
                "Kalshi trading client requires KALSHI_KEY_ID + KALSHI_KEY_FILE "
                "to be configured (RSA signing). Refusing to operate unauthenticated."
            )

    # ── Low-level request ──────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Issue one signed request.  ``path`` is the API path only (no host, no
        query string), e.g. ``/portfolio/orders`` — Kalshi signs method+path.

        Raises KalshiTradingError on any non-2xx response.
        """
        url_path = "/" + path.lstrip("/")
        # Kalshi signs the full request path including the /trade-api/v2 prefix.
        sign_path = _API_PATH_PREFIX + url_path
        headers = {
            "Content-Type": "application/json",
            **_kalshi_auth_headers(method, sign_path),
        }
        url = self._base + url_path

        with httpx.Client(timeout=KALSHI_API_TIMEOUT_SECONDS) as client:
            resp = client.request(method, url, json=body, params=params, headers=headers)
            if resp.status_code // 100 != 2:
                raise KalshiTradingError(
                    f"{method} {url_path} -> HTTP {resp.status_code}: {resp.text[:500]}"
                )
            if not resp.content:
                return {}
            return resp.json()

    # ── Orders ──────────────────────────────────────────────────────────────────

    def place_order(
        self,
        *,
        ticker: str,
        side: str,            # "YES" | "NO"  (our convention)
        action: str,          # "buy" | "sell"
        count: int,
        limit_price: float,   # dollar fraction from our YES/NO side view
        order_type: str = "limit",
        client_order_id: Optional[str] = None,
        time_in_force: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Place a single order.  Returns the Kalshi ``order`` payload.

        Conservative defaults: limit orders only (no market orders) so we never
        cross an arbitrarily wide book.  ``count`` must be a positive integer
        (the live trader rounds size DOWN before calling this).
        """
        if count < 1:
            raise KalshiTradingError(f"refusing to place order with count={count}")
        if not client_order_id:
            raise KalshiTradingError("client_order_id is required for V2 order placement")
        side_l = side.strip().lower()
        if side_l not in ("yes", "no"):
            raise KalshiTradingError(f"side must be YES or NO, got {side!r}")
        action_l = action.strip().lower()
        if action_l not in ("buy", "sell"):
            raise KalshiTradingError(f"action must be buy or sell, got {action!r}")

        if order_type != "limit":
            raise KalshiTradingError(f"only limit orders are supported, got {order_type!r}")

        yes_side: str
        yes_price = float(limit_price)
        if side_l == "yes":
            yes_side = "bid" if action_l == "buy" else "ask"
        else:
            yes_side = "ask" if action_l == "buy" else "bid"
            yes_price = 1.0 - yes_price
        yes_price = max(0.01, min(0.99, round(yes_price, 4)))

        tif = (time_in_force or "good_till_canceled").strip().lower()
        if tif not in ("good_till_canceled", "immediate_or_cancel", "fill_or_kill"):
            raise KalshiTradingError(f"unsupported time_in_force {time_in_force!r}")

        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": yes_side,
            "count": f"{int(count):.2f}",
            "price": f"{yes_price:.4f}",
            "time_in_force": tif,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": False,
            "reduce_only": False,
            "subaccount": 0,
            "exchange_index": 0,
        }

        logger.info(
            "kalshi place_order_v2 | %s %s %s -> %s x%d @ %.4f (tif=%s, coid=%s)",
            ticker, action_l, side_l, yes_side, count, yes_price, tif, body["client_order_id"],
        )
        return self._request("POST", "/portfolio/events/orders", body=body)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a resting order by Kalshi order id. Returns the response payload."""
        logger.info("kalshi cancel_order | %s", order_id)
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}")

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch a single order's current status. Returns the ``order`` payload."""
        payload = self._request("GET", f"/portfolio/orders/{order_id}")
        return payload.get("order", payload)

    def get_orders(
        self,
        *,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List recent orders, optionally filtered by ticker/status."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        payload = self._request("GET", "/portfolio/orders", params=params)
        orders = payload.get("orders")
        return orders if isinstance(orders, list) else []

    def find_order_by_client_order_id(
        self, client_order_id: str, *, ticker: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """
        Reconciliation helper: locate an order we may have submitted by its
        client_order_id.  Used after an ambiguous POST (request sent, response
        lost) to decide whether the order actually landed before retrying.
        Lists recent orders and matches client-side so it works regardless of
        whether the API supports a client_order_id query filter.
        """
        if not client_order_id:
            return None
        for o in self.get_orders(ticker=ticker):
            if str(o.get("client_order_id") or "") == client_order_id:
                return o
        return None

    def get_fills(
        self,
        *,
        order_id: Optional[str] = None,
        ticker: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch fills, optionally filtered by order id or ticker."""
        params: dict[str, Any] = {"limit": limit}
        if order_id:
            params["order_id"] = order_id
        if ticker:
            params["ticker"] = ticker
        payload = self._request("GET", "/portfolio/fills", params=params)
        fills = payload.get("fills")
        return fills if isinstance(fills, list) else []

    def get_positions(self, *, ticker: Optional[str] = None) -> list[dict[str, Any]]:
        """Fetch current market positions, optionally filtered by ticker."""
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        payload = self._request("GET", "/portfolio/positions", params=params)
        positions = payload.get("market_positions")
        return positions if isinstance(positions, list) else []

    def get_balance(self) -> dict[str, Any]:
        """Fetch the account balance payload (cents)."""
        return self._request("GET", "/portfolio/balance")
