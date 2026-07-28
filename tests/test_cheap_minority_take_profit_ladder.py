from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.research_cheap_minority_take_profit_ladder import (  # noqa: E402
    _find_first_daily_signal,
    _score_signal,
    _summarize,
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


def test_ladder_uses_first_daily_signal_and_first_target_hit():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(start, 0.70, 0.71, 0.24, 0.25, 60000.0),
        _row(start + timedelta(seconds=30), 0.66, 0.67, 0.33, 0.34, 60010.0),
        _row(start + timedelta(seconds=60), 0.55, 0.56, 0.45, 0.46, 60020.0),
        _row(start + timedelta(seconds=120), 0.40, 0.41, 0.58, 0.59, 60030.0),
    ]

    signal = _find_first_daily_signal(rows, {1: rows}, max_spread=0.01)
    assert signal is not None
    assert signal["entry_timestamp_et"] == "2026-07-20 08:00:00"
    assert signal["entry_ask"] == pytest.approx(0.25)

    scored = _score_signal(signal, rows, 0.45, max_spread=0.01, fee_rate_cents=7.0)
    assert scored["exit_reason"] == "target_hit"
    assert scored["exit_bid"] == pytest.approx(0.45)
    assert scored["time_to_target_seconds"] == pytest.approx(60.0)
    assert scored["gross_cents"] == pytest.approx(20.0)


def test_ladder_falls_back_to_final_clean_bid_before_close():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(start, 0.70, 0.71, 0.24, 0.25, 60000.0),
        _row(start + timedelta(seconds=60), 0.62, 0.63, 0.36, 0.37, 60020.0),
        _row(start + timedelta(seconds=120), 0.60, 0.61, 0.38, 0.39, 60030.0),
    ]
    signal = _find_first_daily_signal(rows, {1: rows}, max_spread=0.01)
    assert signal is not None

    scored = _score_signal(signal, rows, 0.75, max_spread=0.01, fee_rate_cents=7.0)
    assert scored["exit_reason"] == "close_exit"
    assert scored["exit_bid"] == pytest.approx(0.38)
    assert scored["time_to_target_seconds"] is None
    assert scored["gross_cents"] == pytest.approx(13.0)


def test_summary_reports_target_hit_rate_and_excluding_largest_winner():
    rows = [
        {"exit_rule": "tp_45c_else_close", "target_level": 0.45, "date_et": "2026-07-20", "is_weekend": 0, "status": "COMPLETE", "exit_reason": "target_hit", "time_to_target_seconds": 10.0, "net_cents": 10.0, "gross_cents": 13.0, "fee_cents": 3.0},
        {"exit_rule": "tp_45c_else_close", "target_level": 0.45, "date_et": "2026-07-21", "is_weekend": 0, "status": "COMPLETE", "exit_reason": "close_exit", "time_to_target_seconds": None, "net_cents": -2.0, "gross_cents": 1.0, "fee_cents": 3.0},
    ]

    summary = _summarize(rows, ["2026-07-20", "2026-07-21"])
    tp45 = next(row for row in summary if row["exit_rule"] == "tp_45c_else_close")
    assert tp45["target_hit_count"] == 1
    assert tp45["target_hit_rate_pct"] == pytest.approx(50.0)
    assert tp45["result_excluding_largest_winner"] == pytest.approx(-2.0)
