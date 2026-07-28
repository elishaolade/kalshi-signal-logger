from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.cheap_minority_test_live_tracker import (  # noqa: E402
    _QuoteRow,
    find_cheap_minority_signal,
    is_cheap_minority_test_armed,
)


def _prices(yes_bid, yes_ask, no_bid, no_ask):
    return {
        "YES": {"bid_price": yes_bid, "ask_price": yes_ask, "spread": round(yes_ask - yes_bid, 4)},
        "NO": {"bid_price": no_bid, "ask_price": no_ask, "spread": round(no_ask - no_bid, 4)},
    }


def test_find_signal_requires_first_120s_08_11_minority_20_30c():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    prices = _prices(0.70, 0.71, 0.24, 0.25)
    rows = [
        _QuoteRow(start, 60000.0, prices),
        _QuoteRow(start + timedelta(seconds=60), 60040.0, prices),
    ]

    sig = find_cheap_minority_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        opens_at=start,
        closes_at=start + timedelta(minutes=15),
        captured_at=start + timedelta(seconds=60),
        btc_price=60040.0,
        prices=prices,
        market_rows=rows,
    )

    assert sig is not None
    assert sig.side == "NO"
    assert sig.dominant_side == "YES"
    assert sig.entry_ask == 0.25
    assert sig.entry_spread == 0.01
    assert sig.btc_60s_move == 40.0


def test_find_signal_rejects_non_minority_price_bucket():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    prices = _prices(0.61, 0.62, 0.37, 0.38)
    rows = [_QuoteRow(start, 60000.0, prices)]

    sig = find_cheap_minority_signal(
        market_db_id=1,
        market_ticker="KXBTC15M-TEST",
        contract_ids={"YES": 10, "NO": 11},
        opens_at=start,
        closes_at=start + timedelta(minutes=15),
        captured_at=start,
        btc_price=60000.0,
        prices=prices,
        market_rows=rows,
    )

    assert sig is None


def test_arming_gate_is_inert_by_default():
    armed, reason = is_cheap_minority_test_armed()
    assert armed is False
    assert reason
