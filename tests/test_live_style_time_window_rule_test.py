from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.research_live_style_time_window_rule_test import (  # noqa: E402
    _find_daily_signal,
    _score_signal,
    _summarize,
)


def _row(ts, *, market_pk=1, yes_bid=0.40, yes_ask=0.41, no_bid=0.58, no_ask=0.59, btc=60000.0):
    return {
        "market_pk": market_pk,
        "market_ticker": f"KXBTC15M-TEST-{market_pk}",
        "opens_at": datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        "closes_at": datetime(2026, 7, 20, 12, 15, tzinfo=timezone.utc),
        "captured_at": ts,
        "btc_price": btc,
        "strike": 60000.0,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_spread": round(yes_ask - yes_bid, 4),
        "no_bid": no_bid,
        "no_ask": no_ask,
        "no_spread": round(no_ask - no_bid, 4),
    }


def test_rule_a_finds_first_valid_daily_signal_and_scores_exit():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)  # 08:00 ET
    rows = [
        _row(start, btc=60000.0),
        _row(start + timedelta(seconds=60), yes_bid=0.45, yes_ask=0.46, no_bid=0.53, no_ask=0.54, btc=60040.0),
        _row(start + timedelta(seconds=90), yes_bid=0.50, yes_ask=0.51, no_bid=0.48, no_ask=0.49, btc=60060.0),
        _row(start + timedelta(seconds=120), yes_bid=0.57, yes_ask=0.58, no_bid=0.41, no_ask=0.42, btc=60065.0),
    ]
    rows_by_market = {1: rows}

    sig = _find_daily_signal(
        rows,
        rows_by_market,
        "rule_a_btc_60s_abs_move",
        max_spread=0.02,
        start_hour=8,
        btc_60s_abs_threshold=50.0,
        dominant_30s_reprice_threshold_cents=10.0,
        bounce_confirm_cents=2.0,
        min_seconds_after_low=4.0,
    )

    assert sig is not None
    assert sig["entry_at"] == start + timedelta(seconds=90)
    assert sig["entry_side"] == "YES"
    assert sig["entry_ask"] == pytest.approx(0.51)

    scored = _score_signal(sig, rows, horizon=30, fee_rate_cents=7.0, exit_tolerance_seconds=10, max_spread=0.02)

    assert scored["status"] == "COMPLETE"
    assert scored["exit_bid"] == pytest.approx(0.57)
    assert scored["gross_pnl_cents"] == pytest.approx(6.0)
    assert scored["net_pnl_cents"] < scored["gross_pnl_cents"]


def test_summary_counts_no_trade_days_separately():
    rows = [
        {
            "rule_name": "rule_a_btc_60s_abs_move",
            "exit_horizon_seconds": 60,
            "date_et": "2026-07-20",
            "status": "COMPLETE",
            "net_pnl_cents": 3.0,
            "gross_pnl_cents": 5.0,
            "is_weekend": 0,
        }
    ]

    summary = _summarize(rows, ["2026-07-20", "2026-07-21"])
    rule_a_60 = next(row for row in summary if row["rule_name"] == "rule_a_btc_60s_abs_move" and row["exit_horizon_seconds"] == 60)

    assert rule_a_60["calendar_days_observed"] == 2
    assert rule_a_60["days_with_trade"] == 1
    assert rule_a_60["days_with_no_trade"] == 1
    assert rule_a_60["average_trades_per_day"] == pytest.approx(0.5)
