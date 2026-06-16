"""
backtest/exporter.py — CSV and JSON export of backtest results.

CSV:  one row per TradeResult (flattened)
JSON: experiment summary dict (from metrics.compute_summary)
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from backtest.executor import TradeResult


_TRADE_FIELDS = [
    "exit_profile",
    "holding_seconds_config",
    "profit_target_cfg",
    "side",
    "signal_at",
    "entry_at",
    "entry_ask",
    "entry_bid",
    "entry_spread",
    "exit_at",
    "exit_bid",
    "exit_reason",
    "holding_seconds",
    "btc_price_at_entry",
    "target_price",
    "distance_from_target",
    "side_adjusted_distance",
    "btc_move_toward_side",
    "contract_move_during_lookback",
    "volatility_1m",
    "volatility_5m",
    "time_remaining_at_entry",
    "max_favorable_excursion",
    "max_adverse_excursion",
    "gross_pnl_cents",
    "estimated_fees_cents",
    "estimated_slippage_cents",
    "net_pnl_cents",
    "result_status",
]


def export_trades_csv(trades: list[TradeResult], path: str | Path) -> Path:
    """
    Write one row per TradeResult to a CSV file.

    Returns the Path of the written file.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_TRADE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            row: dict[str, Any] = {}
            for field_name in _TRADE_FIELDS:
                v = getattr(t, field_name, None)
                # Format datetimes as ISO strings.
                if hasattr(v, "isoformat"):
                    v = v.isoformat()
                row[field_name] = v
            writer.writerow(row)

    return out


def export_summary_json(summary: dict[str, Any], path: str | Path) -> Path:
    """
    Write the compute_summary() dict to a JSON file.

    Returns the Path of the written file.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    return out
