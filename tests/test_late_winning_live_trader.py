"""
Unit tests for the late winning-contract live strategy rule detector.

These tests cover pure entry qualification only. They do not place orders and
do not touch MySQL.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config
from app.late_winning_live_trader import _order_id, find_late_winning_signal


def _prices(*, yes_bid=0.76, yes_ask=0.77, no_bid=0.22, no_ask=0.23):
    return {
        "YES": {
            "bid_price": yes_bid,
            "ask_price": yes_ask,
            "spread": round(yes_ask - yes_bid, 4),
        },
        "NO": {
            "bid_price": no_bid,
            "ask_price": no_ask,
            "spread": round(no_ask - no_bid, 4),
        },
    }


@pytest.fixture(autouse=True)
def late_winning_defaults(monkeypatch):
    monkeypatch.setattr(config, "LATE_WINNING_MIN_TTE_SECONDS", 0.0)
    monkeypatch.setattr(config, "LATE_WINNING_MAX_TTE_SECONDS", 480.0)
    monkeypatch.setattr(config, "LATE_WINNING_MIN_DISTANCE_DOLLARS", 150.0)
    monkeypatch.setattr(config, "LATE_WINNING_MIN_ASK", 0.75)
    monkeypatch.setattr(config, "LATE_WINNING_MAX_ASK", 0.91)
    monkeypatch.setattr(config, "LATE_WINNING_MAX_SPREAD", 0.01)


def test_qualifies_yes_when_btc_is_150_above_strike_and_ask_is_75_90c():
    sig = find_late_winning_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        captured_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        btc_price=60160.0,
        tte=450,
        prices=_prices(yes_bid=0.76, yes_ask=0.77),
    )

    assert sig is not None
    assert sig.side == "YES"
    assert sig.contract_id == 10
    assert sig.entry_distance == pytest.approx(160.0)
    assert sig.entry_ask == pytest.approx(0.77)


def test_qualifies_no_when_btc_is_150_below_strike_and_no_ask_is_75_90c():
    sig = find_late_winning_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        captured_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        btc_price=59825.0,
        tte=420,
        prices=_prices(yes_bid=0.22, yes_ask=0.23, no_bid=0.77, no_ask=0.78),
    )

    assert sig is not None
    assert sig.side == "NO"
    assert sig.contract_id == 11
    assert sig.entry_distance == pytest.approx(175.0)


def test_rejects_if_distance_is_too_small():
    sig = find_late_winning_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        captured_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        btc_price=60149.99,
        tte=450,
        prices=_prices(),
    )

    assert sig is None


def test_qualifies_if_ask_is_90c():
    sig = find_late_winning_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        captured_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        btc_price=60160.0,
        tte=450,
        prices=_prices(yes_bid=0.89, yes_ask=0.90),
    )

    assert sig is not None
    assert sig.entry_ask == pytest.approx(0.90)


def test_rejects_if_ask_reaches_91c_exclusive_bound():
    sig = find_late_winning_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        captured_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        btc_price=60160.0,
        tte=450,
        prices=_prices(yes_bid=0.90, yes_ask=0.91),
    )

    assert sig is None


def test_rejects_if_spread_is_more_than_one_cent():
    sig = find_late_winning_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        captured_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        btc_price=60160.0,
        tte=450,
        prices=_prices(yes_bid=0.75, yes_ask=0.77),
    )

    assert sig is None


def test_rejects_if_tte_is_more_than_8_minutes():
    sig = find_late_winning_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        captured_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        btc_price=60160.0,
        tte=481,
        prices=_prices(),
    )

    assert sig is None


def test_order_id_handles_flat_and_nested_order_payloads():
    assert _order_id({"order_id": "flat-123"}) == "flat-123"
    assert _order_id({"id": "flat-id"}) == "flat-id"
    assert _order_id({"order": {"order_id": "nested-123"}}) == "nested-123"
    assert _order_id({"order": {"id": "nested-id"}}) == "nested-id"
    assert _order_id({"order": {}}) == ""
