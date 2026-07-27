from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
from app.btc_impulse_paper_tracker import find_btc_impulse_signal  # noqa: E402
from app.features import Tick  # noqa: E402


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(config, "BTC_IMPULSE_PAPER_ENABLED", True)
    monkeypatch.setattr(config, "BTC_IMPULSE_PAPER_START_HOUR_ET", 8)
    monkeypatch.setattr(config, "BTC_IMPULSE_PAPER_END_HOUR_ET", 11)
    monkeypatch.setattr(config, "BTC_IMPULSE_PAPER_BTC_60S_ABS_THRESHOLD", 50.0)
    monkeypatch.setattr(config, "BTC_IMPULSE_PAPER_MAX_SPREAD", 0.01)


def _prices(yes_bid=0.51, yes_ask=0.52, no_bid=0.47, no_ask=0.48):
    return {
        "YES": {"bid_price": yes_bid, "ask_price": yes_ask, "spread": round(yes_ask - yes_bid, 4)},
        "NO": {"bid_price": no_bid, "ask_price": no_ask, "spread": round(no_ask - no_bid, 4)},
    }


def test_signal_buys_yes_when_btc_60s_move_is_up_enough():
    captured = datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc)  # 08:30 ET
    ticks = [
        Tick(price=60000.0, ts=captured.timestamp() - 61),
        Tick(price=60060.0, ts=captured.timestamp()),
    ]

    sig = find_btc_impulse_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        captured_at=captured,
        btc_price=60060.0,
        prices=_prices(),
        btc_ticks=ticks,
    )

    assert sig is not None
    assert sig.side == "YES"
    assert sig.btc_60s_move == pytest.approx(60.0)
    assert sig.entry_ask == pytest.approx(0.52)


def test_signal_buys_no_when_btc_60s_move_is_down_enough():
    captured = datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc)
    ticks = [
        Tick(price=60080.0, ts=captured.timestamp() - 61),
        Tick(price=60020.0, ts=captured.timestamp()),
    ]

    sig = find_btc_impulse_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        captured_at=captured,
        btc_price=60020.0,
        prices=_prices(),
        btc_ticks=ticks,
    )

    assert sig is not None
    assert sig.side == "NO"
    assert sig.btc_60s_move == pytest.approx(-60.0)
    assert sig.entry_ask == pytest.approx(0.48)


def test_signal_rejects_small_move_and_wide_spread():
    captured = datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc)
    ticks = [
        Tick(price=60000.0, ts=captured.timestamp() - 61),
        Tick(price=60040.0, ts=captured.timestamp()),
    ]

    assert (
        find_btc_impulse_signal(
            market_db_id=1,
            market_ticker="KXBTC15M-TEST",
            contract_ids={"YES": 10, "NO": 11},
            captured_at=captured,
            btc_price=60040.0,
            prices=_prices(),
            btc_ticks=ticks,
        )
        is None
    )

    ticks[1] = Tick(price=60060.0, ts=captured.timestamp())
    assert (
        find_btc_impulse_signal(
            market_db_id=1,
            market_ticker="KXBTC15M-TEST",
            contract_ids={"YES": 10, "NO": 11},
            captured_at=captured,
            btc_price=60060.0,
            prices=_prices(yes_bid=0.49, yes_ask=0.52),
            btc_ticks=ticks,
        )
        is None
    )
