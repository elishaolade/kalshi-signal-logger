from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.research_daily_time_window_scalp_report import (  # noqa: E402
    _market_row,
    _window_summaries,
)


def _row(ts, yes_bid, yes_ask, no_bid, no_ask, btc=60000.0):
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


def test_market_row_scores_clean_executable_scalp_at_ask_to_future_bid():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(start, 0.30, 0.31, 0.68, 0.69, btc=60000.0),
        _row(start + timedelta(seconds=30), 0.34, 0.35, 0.64, 0.65, btc=60020.0),
        _row(start + timedelta(seconds=60), 0.40, 0.41, 0.58, 0.59, btc=60040.0),
    ]

    out = _market_row(rows, max_spread=0.02, fee_rate_cents=7.0)

    assert out["maximum_clean_executable_gross_scalp_cents"] == pytest.approx(9.0)
    assert out["maximum_clean_executable_net_scalp_cents"] == pytest.approx(5.8227)
    assert out["best_net_scalp_side"] == "YES"
    assert out["largest_minority_contract_rebound_cents"] == pytest.approx(10.0)
    assert out["clean_quote_coverage_pct"] == pytest.approx(100.0)


def test_window_summaries_use_one_best_market_per_day_per_window():
    rows = []
    for day, first_net, second_net in (
        ("2026-07-20", 1.0, 5.0),
        ("2026-07-21", -2.0, 3.0),
    ):
        rows.append(
            {
                "market_ticker": f"{day}-A",
                "date_et": day,
                "hour_int_et": 8,
                "hour_of_day_et": "08:00 ET",
                "is_weekend": 0,
                "btc_absolute_open_to_close_move": 10.0,
                "btc_realized_volatility": 1.0,
                "maximum_clean_executable_gross_scalp_cents": first_net + 2,
                "maximum_clean_executable_net_scalp_cents": first_net,
                "quote_count": 10,
            }
        )
        rows.append(
            {
                "market_ticker": f"{day}-B",
                "date_et": day,
                "hour_int_et": 8,
                "hour_of_day_et": "08:00 ET",
                "is_weekend": 0,
                "btc_absolute_open_to_close_move": 20.0,
                "btc_realized_volatility": 2.0,
                "maximum_clean_executable_gross_scalp_cents": second_net + 2,
                "maximum_clean_executable_net_scalp_cents": second_net,
                "quote_count": 10,
            }
        )

    summaries = _window_summaries(rows)
    hourly = next(row for row in summaries if row["window_type"] == "hourly" and row["window_label"] == "08:00-09:00 ET")

    assert hourly["calendar_days_observed"] == 2
    assert hourly["markets_observed"] == 4
    assert hourly["average_markets_per_day"] == pytest.approx(2.0)
    assert hourly["average_best_net_scalp_per_day"] == pytest.approx(4.0)
    assert hourly["days_with_at_least_one_net_positive_scalp_pct"] == pytest.approx(100.0)
