from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.inspect_live_style_rule_contract_prices import build_report  # noqa: E402


def test_contract_price_inspection_filters_rule_a_120s_and_buckets(tmp_path):
    input_csv = tmp_path / "trades.csv"
    rows = [
        {
            "rule_name": "rule_a_btc_60s_abs_move",
            "exit_horizon_seconds": "120",
            "status": "COMPLETE",
            "date_et": "2026-07-20",
            "market_ticker": "KXBTC15M-A",
            "entry_at_et": "2026-07-20 08:30:00",
            "exit_at_utc": "2026-07-20 12:32:00",
            "entry_side": "YES",
            "btc_price": "60060",
            "btc_delta_60s": "60",
            "entry_ask": "0.35",
            "exit_bid": "0.47",
            "gross_pnl_cents": "12",
            "total_fee_cents": "3.2",
            "net_pnl_cents": "8.8",
            "entry_spread": "0.01",
        },
        {
            "rule_name": "rule_a_btc_60s_abs_move",
            "exit_horizon_seconds": "120",
            "status": "COMPLETE",
            "date_et": "2026-07-21",
            "market_ticker": "KXBTC15M-B",
            "entry_at_et": "2026-07-21 09:30:00",
            "exit_at_utc": "2026-07-21 13:32:00",
            "entry_side": "NO",
            "btc_price": "60000",
            "btc_delta_60s": "-70",
            "entry_ask": "0.62",
            "exit_bid": "0.55",
            "gross_pnl_cents": "-7",
            "total_fee_cents": "3.4",
            "net_pnl_cents": "-10.4",
            "entry_spread": "0.01",
        },
        {
            "rule_name": "rule_b_dominant_30s_reprice",
            "exit_horizon_seconds": "120",
            "status": "COMPLETE",
            "date_et": "2026-07-22",
            "market_ticker": "IGNORE",
        },
    ]
    with input_csv.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=sorted({key for row in rows for key in row}))
        writer.writeheader()
        writer.writerows(rows)

    paths = build_report(input_csv, tmp_path / "out")

    with paths.trade_csv.open(newline="") as fh:
        trades = list(csv.DictReader(fh))
    assert len(trades) == 2
    assert trades[0]["btc_price_60s_before_entry"] == "60000.0"
    assert trades[0]["contract_price_bucket"] == "0.30-0.40"
    assert trades[1]["contract_price_bucket"] == "0.60-0.70"

    with paths.bucket_summary_csv.open(newline="") as fh:
        buckets = list(csv.DictReader(fh))
    assert [row["contract_price_bucket"] for row in buckets] == ["0.30-0.40", "0.60-0.70"]
    assert buckets[0]["total_net_cents"] == "8.8"
    assert buckets[1]["total_net_cents"] == "-10.4"
