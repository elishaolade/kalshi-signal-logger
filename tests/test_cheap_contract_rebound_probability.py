from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.research_cheap_contract_rebound_probability import (  # noqa: E402
    _bucket_summary_for,
    _build_candidates,
    _dedupe_market_side,
    _entry_bucket,
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


def test_entry_bucket_uses_requested_ranges():
    assert _entry_bucket(0.05) == "0.05-0.10"
    assert _entry_bucket(0.0999) == "0.05-0.10"
    assert _entry_bucket(0.10) == "0.10-0.20"
    assert _entry_bucket(0.30) == "0.30-0.40"
    assert _entry_bucket(0.40) is None


def test_candidate_scores_future_bid_rebounds_and_btc_alignment():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)  # 08:00 ET
    rows = [
        _row(start, 0.20, 0.21, 0.78, 0.79, 60000.0),
        _row(start + timedelta(seconds=60), 0.30, 0.31, 0.68, 0.69, 60060.0),
        _row(start + timedelta(seconds=90), 0.42, 0.43, 0.56, 0.57, 60080.0),
        _row(start + timedelta(seconds=150), 0.46, 0.47, 0.52, 0.53, 60090.0),
    ]

    candidates = _build_candidates({1: rows}, max_spread=0.01)
    entry = next(row for row in candidates if row["entry_timestamp_utc"] == "2026-07-20 12:00:00" and row["contract_side"] == "YES")

    assert entry["entry_bucket"] == "0.20-0.30"
    assert entry["btc_move_prior_60s"] is None
    assert entry["btc_60s_alignment"] == "unknown"
    assert entry["minority_status"] == "minority"
    assert entry["max_bid_increase_60s_cents"] == pytest.approx(9.0)
    assert entry["max_bid_increase_120s_cents"] == pytest.approx(21.0)
    assert entry["hit_plus_10c_within_60s"] == 0
    assert entry["hit_plus_10c_within_120s"] == 1

    later = next(row for row in candidates if row["entry_timestamp_utc"] == "2026-07-20 12:01:00" and row["contract_side"] == "YES")
    assert later["btc_move_prior_60s"] == pytest.approx(60.0)
    assert later["btc_60s_alignment"] == "aligned"


def test_market_side_dedupe_keeps_first_candidate_and_summary_counts_it_once():
    start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(start, 0.20, 0.21, 0.78, 0.79, 60000.0),
        _row(start + timedelta(seconds=30), 0.22, 0.23, 0.76, 0.77, 60020.0),
        _row(start + timedelta(seconds=90), 0.35, 0.36, 0.63, 0.64, 60040.0),
    ]
    candidates = _build_candidates({1: rows}, max_spread=0.01)
    deduped = _dedupe_market_side(candidates)

    yes_rows = [row for row in deduped if row["contract_side"] == "YES"]
    assert len(yes_rows) == 1
    assert yes_rows[0]["entry_timestamp_utc"] == "2026-07-20 12:00:00"

    summaries = _bucket_summary_for("B_market_side_first", deduped)
    combined = next(
        row
        for row in summaries
        if row["entry_bucket"] == "0.05-0.40" and row["breakdown_type"] == "all"
    )
    assert combined["candidate_count"] == 1
    assert combined["unique_markets"] == 1
