"""
Unit tests for the research-only fast rebound TEST signal detector.

These tests do not touch MySQL, Kalshi, or trading endpoints.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config
from app.fast_rebound_test_tracker import _QuoteRow, find_fast_rebound_signal, parse_exit_models


def _prices(*, yes_bid=0.65, yes_ask=0.66, no_bid=0.33, no_ask=0.34):
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
def fast_rebound_defaults(monkeypatch):
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_ENABLED", True)
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_MAX_ENTRY_SECONDS_AFTER_OPEN", 300.0)
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_BASELINE_MAX_SECONDS_AFTER_OPEN", 30.0)
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_DOMINANT_MIN_ASK", 0.65)
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_DOMINANT_MAX_ASK", 0.70)
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_SPEED_MIN_CENTS_PER_SECOND", 0.15)
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_SPEED_MAX_CENTS_PER_SECOND", 0.30)
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_REPRICE_30S_MIN_CENTS", 9.0)
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_REPRICE_30S_MAX_CENTS", 15.0)
    monkeypatch.setattr(config, "FAST_REBOUND_TEST_MAX_SPREAD", 0.02)


def _row(captured_at, prices):
    return _QuoteRow(
        captured_at=captured_at,
        ts=captured_at.timestamp(),
        btc_price=60020.0,
        tte=800,
        prices=prices,
    )


def test_parse_exit_models():
    assert parse_exit_models("10:10,15:10,10:15") == [(10.0, 10.0), (15.0, 10.0), (10.0, 15.0)]


def test_qualifies_on_frozen_reprice_9_to_15_bucket():
    opens_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    captured_at = opens_at + timedelta(seconds=120)
    rows = [
        _row(opens_at + timedelta(seconds=10), _prices(yes_bid=0.41, yes_ask=0.42, no_bid=0.57, no_ask=0.58)),
        _row(captured_at - timedelta(seconds=30), _prices(yes_bid=0.52, yes_ask=0.53, no_bid=0.46, no_ask=0.47)),
    ]

    sig = find_fast_rebound_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        opens_at=opens_at,
        captured_at=captured_at,
        btc_price=60020.0,
        tte=780,
        prices=_prices(yes_bid=0.64, yes_ask=0.66, no_bid=0.32, no_ask=0.34),
        market_rows=rows,
    )

    assert sig is not None
    assert sig.dominant_side == "YES"
    assert sig.minority_side == "NO"
    assert sig.contract_id == 11
    assert sig.entry_ask == pytest.approx(0.34)
    assert sig.dominant_change_prev_30s_cents == pytest.approx(13.0)


def test_rejects_when_dominant_30s_reprice_is_too_high():
    opens_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    captured_at = opens_at + timedelta(seconds=120)
    rows = [
        _row(opens_at + timedelta(seconds=10), _prices(yes_bid=0.41, yes_ask=0.42, no_bid=0.57, no_ask=0.58)),
        _row(captured_at - timedelta(seconds=30), _prices(yes_bid=0.48, yes_ask=0.49, no_bid=0.50, no_ask=0.51)),
    ]

    sig = find_fast_rebound_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        opens_at=opens_at,
        captured_at=captured_at,
        btc_price=60020.0,
        tte=780,
        prices=_prices(yes_bid=0.64, yes_ask=0.66, no_bid=0.32, no_ask=0.34),
        market_rows=rows,
    )

    assert sig is None


def test_rejects_extreme_rapid_speed():
    opens_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    captured_at = opens_at + timedelta(seconds=80)
    rows = [
        _row(opens_at + timedelta(seconds=5), _prices(yes_bid=0.30, yes_ask=0.31, no_bid=0.68, no_ask=0.69)),
        _row(captured_at - timedelta(seconds=30), _prices(yes_bid=0.52, yes_ask=0.53, no_bid=0.46, no_ask=0.47)),
    ]

    sig = find_fast_rebound_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        target_price=60000.0,
        opens_at=opens_at,
        captured_at=captured_at,
        btc_price=60020.0,
        tte=820,
        prices=_prices(yes_bid=0.64, yes_ask=0.66, no_bid=0.32, no_ask=0.34),
        market_rows=rows,
    )

    assert sig is None
