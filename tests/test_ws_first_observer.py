from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.kalshi_ws import KalshiMarketStream
from app.ws_first_observer import WebSocketFirstObserver


def test_observer_returns_ws_prices_when_quote_is_fresh(monkeypatch):
    monkeypatch.setattr("app.config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS", 5)

    stream = KalshiMarketStream()
    stream.apply_orderbook_snapshot(
        "KXBTC15M-TEST",
        yes_bids=[(0.31, 10)],
        no_bids=[(0.66, 7)],
        updated_at_ts=100.0,
    )
    monkeypatch.setattr(stream, "get_quote_age_seconds", lambda ticker: 1.0)

    observed = WebSocketFirstObserver(stream).observe_market("KXBTC15M-TEST")

    assert observed is not None
    assert observed.quote_age_seconds == pytest.approx(1.0)
    assert observed.prices["YES"]["source"] == "websocket"
    assert observed.prices["YES"]["bid_price"] == pytest.approx(0.31)
    assert observed.prices["YES"]["ask_price"] == pytest.approx(0.34)
    assert observed.prices["YES"]["spread"] == pytest.approx(0.03)
    assert observed.yes_depth_at_bid == pytest.approx(10)


def test_observer_rejects_stale_quote(monkeypatch):
    monkeypatch.setattr("app.config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS", 2)

    stream = KalshiMarketStream()
    stream.apply_orderbook_snapshot(
        "KXBTC15M-TEST",
        yes_bids=[(0.31, 10)],
        no_bids=[(0.66, 7)],
        updated_at_ts=100.0,
    )
    monkeypatch.setattr(stream, "get_quote_age_seconds", lambda ticker: 3.0)

    assert WebSocketFirstObserver(stream).observe_market("KXBTC15M-TEST") is None


def test_observer_rejects_crossed_book(monkeypatch):
    monkeypatch.setattr("app.config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS", 5)

    stream = KalshiMarketStream()
    stream.apply_orderbook_snapshot(
        "KXBTC15M-TEST",
        yes_bids=[(0.76, 10)],
        no_bids=[(0.30, 7)],
        updated_at_ts=100.0,
    )
    monkeypatch.setattr(stream, "get_quote_age_seconds", lambda ticker: 1.0)

    assert WebSocketFirstObserver(stream).observe_market("KXBTC15M-TEST") is None
