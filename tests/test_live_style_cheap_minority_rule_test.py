from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.research_live_style_cheap_minority_rule_test import (  # noqa: E402
    _base_candidate,
    _candidate_matches_rule,
    _find_daily_signal,
    _score_signal,
)


def _row(ts, yes_bid, yes_ask, no_bid, no_ask, btc):
    return {
        "market_pk": 1,
        "market_ticker": "KXBTC15M-TEST",
        "opens_at": datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        "closes_at": datetime(2026, 7, 20, 12, 15, tzinfo=timezone.utc),
        "captured_at": ts,
        "btc_price": btc,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_spread": round(yes_ask - yes_bid, 4),
        "no_bid": no_bid,
        "no_ask": no_ask,
        "no_spread": round(no_ask - no_bid, 4),
    }


def test_base_candidate_requires_020_030_minority_contract():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(start, 0.70, 0.71, 0.24, 0.25, 60000.0),
        _row(start + timedelta(seconds=60), 0.72, 0.73, 0.22, 0.23, 60060.0),
    ]

    candidate = _base_candidate(rows[1], rows, max_spread=0.01)

    assert candidate is not None
    assert candidate["dominant_side"] == "YES"
    assert candidate["minority_side"] == "NO"
    assert candidate["entry_ask"] == pytest.approx(0.23)
    assert candidate["btc_60s_move"] == pytest.approx(60.0)
    assert candidate["minority_aligned_with_btc_60s_direction"] == 0
    assert _candidate_matches_rule(candidate, "rule_a_first_eligible", 50.0)
    assert _candidate_matches_rule(candidate, "rule_b_countertrend", 50.0)
    assert _candidate_matches_rule(candidate, "rule_c_btc_impulse", 50.0)
    assert _candidate_matches_rule(candidate, "rule_d_countertrend_btc_impulse", 50.0)


def test_find_daily_signal_takes_first_valid_signal_only():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(start, 0.68, 0.69, 0.30, 0.31, 60000.0),  # minority ask too high
        _row(start + timedelta(seconds=30), 0.70, 0.71, 0.24, 0.25, 60020.0),
        _row(start + timedelta(seconds=60), 0.72, 0.73, 0.22, 0.23, 60060.0),
    ]

    signal = _find_daily_signal(rows, {1: rows}, "rule_a_first_eligible", max_spread=0.01, btc_impulse_threshold=50.0)

    assert signal is not None
    assert signal["entry_timestamp_et"] == "2026-07-20 08:00:30"
    assert signal["entry_ask"] == pytest.approx(0.25)


def test_score_signal_uses_entry_ask_exit_bid_and_fees():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(start, 0.70, 0.71, 0.24, 0.25, 60000.0),
        _row(start + timedelta(seconds=60), 0.72, 0.73, 0.22, 0.23, 60060.0),
        _row(start + timedelta(seconds=120), 0.62, 0.63, 0.34, 0.35, 60040.0),
    ]
    signal = _base_candidate(rows[0], rows, max_spread=0.01)
    assert signal is not None
    signal["rule_name"] = "rule_a_first_eligible"

    scored = _score_signal(signal, rows, 120, max_spread=0.01, fee_rate_cents=7.0, exit_tolerance_seconds=10)

    assert scored["status"] == "COMPLETE"
    assert scored["exit_bid"] == pytest.approx(0.34)
    assert scored["gross_cents"] == pytest.approx(9.0)
    assert scored["fee_cents"] > 0
    assert scored["net_cents"] < 9.0
