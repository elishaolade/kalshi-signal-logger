#!/usr/bin/env python3
"""
Below-35c cheap minority rebound probability and live-style rule report.

This is historical research only. It evaluates Kalshi BTC 15-minute quote paths
with clean executable bid/ask quotes. It does not place orders or mutate DB rows.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.research_daily_time_window_scalp_report import (  # noqa: E402
    ET,
    _avg,
    _clean_row,
    _dominant_side,
    _fee_cents,
    _is_clean_side,
    _load_rows,
    _max_drawdown,
    _median,
    _minority_side,
    _pct,
    _price,
    _profit_factor,
    _table,
    _write_csv,
)

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "below_35c_cheap_minority_rebound"

HORIZONS = (60, 120, 180, 300)
TARGETS = (3, 5, 10, 15, 20)
TP_RELATIVE_CENTS = (10, 15, 20)
TP_ABSOLUTE_LEVELS = (0.45, 0.55, 0.65, 0.75, 0.85, 0.95)

ENTRY_BUCKETS = (
    ("0.05-0.10", 0.05, 0.10),
    ("0.10-0.15", 0.10, 0.15),
    ("0.15-0.20", 0.15, 0.20),
    ("0.20-0.25", 0.20, 0.25),
    ("0.25-0.30", 0.25, 0.30),
    ("0.30-0.35", 0.30, 0.35),
)
COMBINED_BUCKETS = (
    ("0.05-0.35", 0.05, 0.35),
    ("0.10-0.35", 0.10, 0.35),
    ("0.20-0.35", 0.20, 0.35),
)
ALL_BUCKETS = ENTRY_BUCKETS + COMBINED_BUCKETS


@dataclass(frozen=True)
class RuleConfig:
    rule_name: str
    selection_unit: str
    min_ask: float
    max_ask: float
    description: str


@dataclass(frozen=True)
class Paths:
    directory: Path
    candidate_probability_csv: Path
    market_side_first_probability_csv: Path
    market_level_probability_csv: Path
    bucket_summary_csv: Path
    breakdown_summary_csv: Path
    live_style_trade_csv: Path
    live_style_summary_csv: Path
    daily_pnl_csv: Path
    data_quality_csv: Path
    markdown_report: Path


RULES = (
    RuleConfig(
        "rule_a_first_below_35c_per_market",
        "market",
        0.0,
        0.35,
        "First clean minority ask < 0.35 in first 5 minutes of each market.",
    ),
    RuleConfig(
        "rule_b_first_below_35c_per_day",
        "day",
        0.0,
        0.35,
        "First clean minority ask < 0.35 in first 5 minutes of each ET day.",
    ),
    RuleConfig(
        "rule_c_first_20_35c_per_market",
        "market",
        0.20,
        0.35,
        "First clean 0.20 <= minority ask < 0.35 in first 5 minutes of each market.",
    ),
    RuleConfig(
        "rule_d_first_20_35c_per_day",
        "day",
        0.20,
        0.35,
        "First clean 0.20 <= minority ask < 0.35 in first 5 minutes of each ET day.",
    ),
    RuleConfig(
        "rule_e_first_20_30c_per_day",
        "day",
        0.20,
        0.30,
        "Previous narrower rule: first clean 0.20 <= minority ask < 0.30 in first 5 minutes of each ET day.",
    ),
)


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(sep=" ")


def _iso_et(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(ET).replace(tzinfo=None).isoformat(sep=" ")


def _last_at_or_before(rows: list[dict[str, Any]], ts: datetime) -> Optional[dict[str, Any]]:
    out = None
    for row in rows:
        if row["captured_at"] <= ts:
            out = row
        else:
            break
    return out


def _first_at_or_after(
    rows: list[dict[str, Any]],
    ts: datetime,
    tolerance_seconds: int,
) -> Optional[dict[str, Any]]:
    max_ts = ts + timedelta(seconds=tolerance_seconds)
    for row in rows:
        if ts <= row["captured_at"] <= max_ts:
            return row
    return None


def _last_clean_before_close(
    rows: list[dict[str, Any]],
    side: str,
    entry_at: datetime,
    close_at: datetime,
    max_spread: float,
) -> Optional[dict[str, Any]]:
    out = None
    for row in rows:
        if row["captured_at"] <= entry_at:
            continue
        if row["captured_at"] > close_at:
            break
        if _is_clean_side(row, side, max_spread):
            out = row
    return out


def _max_future_bid(
    rows: list[dict[str, Any]],
    side: str,
    entry_at: datetime,
    end_at: datetime,
    max_spread: float,
) -> Optional[float]:
    vals = [
        _price(row, side, "bid")
        for row in rows
        if row["captured_at"] > entry_at
        and row["captured_at"] <= end_at
        and _is_clean_side(row, side, max_spread)
    ]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def _first_target_hit(
    rows: list[dict[str, Any]],
    side: str,
    entry_at: datetime,
    end_at: datetime,
    target_bid: float,
    max_spread: float,
) -> Optional[dict[str, Any]]:
    for row in rows:
        if row["captured_at"] <= entry_at:
            continue
        if row["captured_at"] > end_at:
            break
        if not _is_clean_side(row, side, max_spread):
            continue
        bid = _price(row, side, "bid")
        if bid is not None and bid >= target_bid:
            return row
    return None


def _entry_bucket(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    for label, low, high in ENTRY_BUCKETS:
        if low <= value < high:
            return label
    return None


def _lifecycle_bucket(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    if seconds < 60:
        return "0-60s"
    if seconds < 120:
        return "60-120s"
    if seconds < 180:
        return "120-180s"
    if seconds <= 300:
        return "180-300s"
    return "out_of_window"


def _abs_move_bucket(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    value = abs(value)
    if value < 10:
        return "<$10"
    if value < 25:
        return "$10-$25"
    if value < 50:
        return "$25-$50"
    if value < 100:
        return "$50-$100"
    return "$100+"


def _signed_move_bucket(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value <= -100:
        return "<=-$100"
    if value <= -50:
        return "-$100--$50"
    if value <= -25:
        return "-$50--$25"
    if value < 25:
        return "-$25-$25"
    if value < 50:
        return "$25-$50"
    if value < 100:
        return "$50-$100"
    return "$100+"


def _dominant_price_bucket(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < 0.65:
        return "<65c"
    if value < 0.75:
        return "65-75c"
    if value < 0.85:
        return "75-85c"
    if value < 0.95:
        return "85-95c"
    return "95c+"


def _spread_bucket(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value <= 0.0025:
        return "<=0.25c"
    if value <= 0.005:
        return "0.25-0.5c"
    if value <= 0.01:
        return "0.5-1c"
    return ">1c"


def _alignment_bucket(side: str, btc_move_60s: Optional[float]) -> str:
    if btc_move_60s is None or btc_move_60s == 0:
        return "unknown"
    aligned_side = "YES" if btc_move_60s > 0 else "NO"
    return "aligned" if side == aligned_side else "against"


def _time_to_hit(
    rows: list[dict[str, Any]],
    side: str,
    entry_at: datetime,
    close_at: datetime,
    target_bid: float,
    max_spread: float,
) -> Optional[float]:
    hit = _first_target_hit(rows, side, entry_at, close_at, target_bid, max_spread)
    if hit is None:
        return None
    return round((hit["captured_at"] - entry_at).total_seconds(), 3)


def _candidate_from_row(
    market_rows: list[dict[str, Any]],
    row: dict[str, Any],
    max_spread: float,
) -> Optional[dict[str, Any]]:
    if not _clean_row(row, max_spread):
        return None
    entry_at = row["captured_at"]
    opens_at = row["opens_at"]
    closes_at = row["closes_at"]
    seconds_since_open = (entry_at - opens_at).total_seconds()
    if seconds_since_open < 0 or seconds_since_open > 300:
        return None

    dominant = _dominant_side(row)
    if dominant is None:
        return None
    minority = _minority_side(dominant)
    if not _is_clean_side(row, minority, max_spread):
        return None

    entry_ask = _price(row, minority, "ask")
    entry_bid = _price(row, minority, "bid")
    entry_spread = _price(row, minority, "spread")
    dominant_ask = _price(row, dominant, "ask")
    if entry_ask is None or entry_bid is None or entry_spread is None:
        return None
    if not (entry_ask < 0.35):
        return None

    open_row = _last_at_or_before(market_rows, opens_at + timedelta(seconds=1)) or market_rows[0]
    prev30 = _last_at_or_before(market_rows, entry_at - timedelta(seconds=30))
    prev60 = _last_at_or_before(market_rows, entry_at - timedelta(seconds=60))
    btc_open = open_row["btc_price"] if open_row else None
    btc_entry = row["btc_price"]
    btc_from_open = btc_entry - btc_open if btc_open is not None else None
    btc_30 = btc_entry - prev30["btc_price"] if prev30 else None
    btc_60 = btc_entry - prev60["btc_price"] if prev60 else None
    entry_et = entry_at.astimezone(ET)

    out: dict[str, Any] = {
        "market_ticker": row["market_ticker"],
        "market_pk": row["market_pk"],
        "market_open_timestamp_utc": _iso(opens_at),
        "market_open_timestamp_et": _iso_et(opens_at),
        "entry_timestamp_utc": _iso(entry_at),
        "entry_timestamp_et": _iso_et(entry_at),
        "date_et": entry_et.date().isoformat(),
        "hour_et": f"{entry_et.hour:02d}:00",
        "weekday": entry_et.strftime("%A"),
        "is_weekend": int(entry_et.weekday() >= 5),
        "seconds_since_market_open": _round(seconds_since_open, 3),
        "lifecycle_bucket": _lifecycle_bucket(seconds_since_open),
        "contract_side": minority,
        "dominant_side": dominant,
        "minority_side": minority,
        "entry_bid": _round(entry_bid),
        "entry_ask": _round(entry_ask),
        "entry_spread": _round(entry_spread),
        "entry_bucket": _entry_bucket(entry_ask),
        "dominant_ask": _round(dominant_ask),
        "dominant_price_bucket": _dominant_price_bucket(dominant_ask),
        "spread_bucket": _spread_bucket(entry_spread),
        "btc_price_at_market_open": _round(btc_open, 2),
        "btc_price_at_entry": _round(btc_entry, 2),
        "btc_move_open_to_entry": _round(btc_from_open, 2),
        "btc_move_open_to_entry_bucket": _signed_move_bucket(btc_from_open),
        "btc_30s_move_before_entry": _round(btc_30, 2),
        "btc_60s_move_before_entry": _round(btc_60, 2),
        "btc_60s_direction": "YES" if btc_60 is not None and btc_60 > 0 else "NO" if btc_60 is not None and btc_60 < 0 else None,
        "btc_60s_alignment": _alignment_bucket(minority, btc_60),
        "btc_60s_abs_move_bucket": _abs_move_bucket(btc_60),
        "version_b_market_side_first": 0,
    }

    for seconds in HORIZONS:
        max_bid = _max_future_bid(market_rows, minority, entry_at, entry_at + timedelta(seconds=seconds), max_spread)
        increase = (max_bid - entry_ask) * 100.0 if max_bid is not None else None
        out[f"max_future_bid_{seconds}s"] = _round(max_bid)
        out[f"max_bid_increase_{seconds}s_cents"] = _round(increase, 4)
        for target in TARGETS:
            out[f"hit_plus_{target}c_within_{seconds}s"] = int(increase is not None and increase >= target)

    max_bid_close = _max_future_bid(market_rows, minority, entry_at, closes_at, max_spread)
    increase_close = (max_bid_close - entry_ask) * 100.0 if max_bid_close is not None else None
    out["max_future_bid_before_close"] = _round(max_bid_close)
    out["max_bid_increase_before_close_cents"] = _round(increase_close, 4)
    for target in TARGETS:
        out[f"hit_plus_{target}c_before_close"] = int(increase_close is not None and increase_close >= target)
    out["time_to_plus_10c_seconds"] = _time_to_hit(
        market_rows, minority, entry_at, closes_at, entry_ask + 0.10, max_spread
    )
    out["time_to_plus_20c_seconds"] = _time_to_hit(
        market_rows, minority, entry_at, closes_at, entry_ask + 0.20, max_spread
    )
    return out


def _build_candidates(rows_by_market: dict[int, list[dict[str, Any]]], max_spread: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rows in rows_by_market.values():
        rows.sort(key=lambda row: row["captured_at"])
        for row in rows:
            candidate = _candidate_from_row(rows, row, max_spread)
            if candidate:
                out.append(candidate)
    return sorted(out, key=lambda row: (row["entry_timestamp_utc"], row["market_ticker"], row["contract_side"]))


def _dedupe_market_side(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in candidates:
        key = (str(row["market_ticker"]), str(row["contract_side"]))
        if key in seen:
            continue
        seen.add(key)
        copy = dict(row)
        copy["version_b_market_side_first"] = 1
        out.append(copy)
    return out


def _market_level_probability(market_side_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in market_side_rows:
        grouped[str(row["market_ticker"])].append(row)
    out: list[dict[str, Any]] = []
    for ticker, rows in sorted(grouped.items(), key=lambda item: min(row["entry_timestamp_utc"] for row in item[1])):
        first = min(rows, key=lambda row: row["entry_timestamp_utc"])
        item = {
            "market_ticker": ticker,
            "market_open_timestamp_utc": first["market_open_timestamp_utc"],
            "market_open_timestamp_et": first["market_open_timestamp_et"],
            "date_et": first["date_et"],
            "hour_et": first["hour_et"],
            "weekday": first["weekday"],
            "is_weekend": first["is_weekend"],
            "eligible_sides": len(rows),
            "first_entry_timestamp_et": first["entry_timestamp_et"],
            "best_max_bid_increase_before_close_cents": max(
                (row["max_bid_increase_before_close_cents"] for row in rows if row["max_bid_increase_before_close_cents"] is not None),
                default=None,
            ),
        }
        for seconds in HORIZONS:
            for target in TARGETS:
                item[f"market_had_plus_{target}c_within_{seconds}s"] = int(
                    any(row[f"hit_plus_{target}c_within_{seconds}s"] == 1 for row in rows)
                )
        for target in TARGETS:
            item[f"market_had_plus_{target}c_before_close"] = int(
                any(row[f"hit_plus_{target}c_before_close"] == 1 for row in rows)
            )
        out.append(item)
    return out


def _bucket_rows(rows: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    bucket_map = {name: (low, high) for name, low, high in ALL_BUCKETS}
    low, high = bucket_map[label]
    return [row for row in rows if row.get("entry_ask") is not None and low <= row["entry_ask"] < high]


def _split_rate(rows: list[dict[str, Any]], hit_field: str) -> tuple[Optional[float], Optional[float]]:
    dates = sorted({row["date_et"] for row in rows})
    if not dates:
        return None, None
    midpoint = len(dates) // 2
    first_dates = set(dates[:midpoint])
    second_dates = set(dates[midpoint:])
    first = [row for row in rows if row["date_et"] in first_dates]
    second = [row for row in rows if row["date_et"] in second_dates]
    return (
        _pct(sum(row.get(hit_field) == 1 for row in first), len(first)),
        _pct(sum(row.get(hit_field) == 1 for row in second), len(second)),
    )


def _probability_summary(version: str, rows: list[dict[str, Any]], bucket: Optional[str] = None) -> dict[str, Any]:
    if bucket:
        rows = _bucket_rows(rows, bucket)
    first, second = _split_rate(rows, "hit_plus_10c_before_close")
    weekdays = [row for row in rows if row["is_weekend"] == 0]
    weekends = [row for row in rows if row["is_weekend"] == 1]
    out: dict[str, Any] = {
        "version": version,
        "entry_bucket": bucket or "all_below_35c",
        "sample_size": len(rows),
        "unique_markets": len({row["market_ticker"] for row in rows}),
        "unique_days": len({row["date_et"] for row in rows}),
        "average_entry_ask": _avg(row.get("entry_ask") for row in rows),
        "median_entry_ask": _median(row.get("entry_ask") for row in rows),
        "average_spread": _avg(row.get("entry_spread") for row in rows),
        "median_spread": _median(row.get("entry_spread") for row in rows),
        "average_max_bid_increase_before_close_cents": _avg(row.get("max_bid_increase_before_close_cents") for row in rows),
        "median_max_bid_increase_before_close_cents": _median(row.get("max_bid_increase_before_close_cents") for row in rows),
        "average_time_to_plus_10c_seconds": _avg(row.get("time_to_plus_10c_seconds") for row in rows),
        "median_time_to_plus_10c_seconds": _median(row.get("time_to_plus_10c_seconds") for row in rows),
        "first_half_plus_10c_before_close_rate": first,
        "second_half_plus_10c_before_close_rate": second,
        "weekday_plus_10c_before_close_rate": _pct(sum(row.get("hit_plus_10c_before_close") == 1 for row in weekdays), len(weekdays)),
        "weekend_plus_10c_before_close_rate": _pct(sum(row.get("hit_plus_10c_before_close") == 1 for row in weekends), len(weekends)),
    }
    for seconds in HORIZONS:
        increases = [row.get(f"max_bid_increase_{seconds}s_cents") for row in rows]
        out[f"average_max_bid_increase_{seconds}s_cents"] = _avg(increases)
        out[f"median_max_bid_increase_{seconds}s_cents"] = _median(increases)
        for target in TARGETS:
            out[f"plus_{target}c_hit_rate_{seconds}s"] = _pct(
                sum(row.get(f"hit_plus_{target}c_within_{seconds}s") == 1 for row in rows), len(rows)
            )
    for target in TARGETS:
        out[f"plus_{target}c_hit_rate_before_close"] = _pct(
            sum(row.get(f"hit_plus_{target}c_before_close") == 1 for row in rows), len(rows)
        )
    return out


def _market_probability_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dates = sorted({row["date_et"] for row in rows})
    midpoint = len(dates) // 2
    first_dates = set(dates[:midpoint])
    second_dates = set(dates[midpoint:])
    first = [row for row in rows if row["date_et"] in first_dates]
    second = [row for row in rows if row["date_et"] in second_dates]
    weekdays = [row for row in rows if row["is_weekend"] == 0]
    weekends = [row for row in rows if row["is_weekend"] == 1]
    out: dict[str, Any] = {
        "version": "C_market_level",
        "entry_bucket": "market_had_any_below_35c",
        "sample_size": len(rows),
        "unique_markets": len(rows),
        "unique_days": len(dates),
        "average_max_bid_increase_before_close_cents": _avg(row.get("best_max_bid_increase_before_close_cents") for row in rows),
        "median_max_bid_increase_before_close_cents": _median(row.get("best_max_bid_increase_before_close_cents") for row in rows),
        "first_half_plus_10c_before_close_rate": _pct(sum(row.get("market_had_plus_10c_before_close") == 1 for row in first), len(first)),
        "second_half_plus_10c_before_close_rate": _pct(sum(row.get("market_had_plus_10c_before_close") == 1 for row in second), len(second)),
        "weekday_plus_10c_before_close_rate": _pct(sum(row.get("market_had_plus_10c_before_close") == 1 for row in weekdays), len(weekdays)),
        "weekend_plus_10c_before_close_rate": _pct(sum(row.get("market_had_plus_10c_before_close") == 1 for row in weekends), len(weekends)),
    }
    for seconds in HORIZONS:
        for target in TARGETS:
            out[f"plus_{target}c_hit_rate_{seconds}s"] = _pct(
                sum(row.get(f"market_had_plus_{target}c_within_{seconds}s") == 1 for row in rows), len(rows)
            )
    for target in TARGETS:
        out[f"plus_{target}c_hit_rate_before_close"] = _pct(
            sum(row.get(f"market_had_plus_{target}c_before_close") == 1 for row in rows), len(rows)
        )
    return out


def _bucket_summary(rows: list[dict[str, Any]], version: str) -> list[dict[str, Any]]:
    return [_probability_summary(version, rows, bucket=label) for label, _, _ in ALL_BUCKETS]


def _breakdown_summary(rows: list[dict[str, Any]], version: str) -> list[dict[str, Any]]:
    specs = [
        ("market_lifecycle", "lifecycle_bucket"),
        ("btc_60s_direction", "btc_60s_alignment"),
        ("btc_60s_abs_move_bucket", "btc_60s_abs_move_bucket"),
        ("btc_open_to_entry_bucket", "btc_move_open_to_entry_bucket"),
        ("dominant_contract_price_bucket", "dominant_price_bucket"),
        ("spread_bucket", "spread_bucket"),
        ("hour_et", "hour_et"),
        ("weekday_weekend", "is_weekend"),
    ]
    out: list[dict[str, Any]] = []
    for breakdown_name, field in specs:
        values = sorted({str(row.get(field)) for row in rows})
        for value in values:
            group = [row for row in rows if str(row.get(field)) == value]
            item = _probability_summary(version, group)
            item["breakdown_type"] = breakdown_name
            item["breakdown_value"] = "weekend" if field == "is_weekend" and value == "1" else "weekday" if field == "is_weekend" and value == "0" else value
            out.append(item)
    return out


def _observed_days(raw_rows: list[dict[str, Any]]) -> list[str]:
    days = set()
    for row in raw_rows:
        seconds_since_open = (row["captured_at"] - row["opens_at"]).total_seconds()
        if 0 <= seconds_since_open <= 300:
            days.add(row["captured_at"].astimezone(ET).date().isoformat())
    return sorted(days)


def _base_signal(row: dict[str, Any], min_ask: float, max_ask: float) -> bool:
    ask = row.get("entry_ask")
    return ask is not None and min_ask <= ask < max_ask


def _select_rule_signals(
    market_side_rows: list[dict[str, Any]],
    rule: RuleConfig,
) -> list[dict[str, Any]]:
    eligible = [row for row in market_side_rows if _base_signal(row, rule.min_ask, rule.max_ask)]
    if rule.selection_unit == "market":
        seen: set[str] = set()
        out = []
        for row in eligible:
            ticker = str(row["market_ticker"])
            if ticker in seen:
                continue
            seen.add(ticker)
            copy = dict(row)
            copy["rule_name"] = rule.rule_name
            out.append(copy)
        return out
    if rule.selection_unit == "day":
        seen_days: set[str] = set()
        out = []
        for row in eligible:
            day = str(row["date_et"])
            if day in seen_days:
                continue
            seen_days.add(day)
            copy = dict(row)
            copy["rule_name"] = rule.rule_name
            out.append(copy)
        return out
    raise ValueError(f"unknown selection unit {rule.selection_unit}")


def _exit_rule_names() -> list[str]:
    names = [f"fixed_{h}s" for h in (60, 120, 180, 300)]
    names.extend(f"tp_plus_{c}c_else_close" for c in TP_RELATIVE_CENTS)
    names.extend(f"tp_bid_{int(level * 100)}c_else_close" for level in TP_ABSOLUTE_LEVELS)
    names.append("close")
    return names


def _score_exit(
    signal: dict[str, Any],
    market_rows: list[dict[str, Any]],
    exit_rule: str,
    max_spread: float,
    fee_rate_cents: float,
    exit_tolerance_seconds: int,
) -> dict[str, Any]:
    side = str(signal["contract_side"])
    entry_at = datetime.fromisoformat(str(signal["entry_timestamp_utc"])).replace(tzinfo=timezone.utc)
    entry_ask = float(signal["entry_ask"])
    close_at = market_rows[0]["closes_at"]
    exit_row = None
    exit_reason = "no_valid_exit"
    target_hit = 0
    target_level = None

    clean_future_rows = [
        row for row in market_rows if row["captured_at"] > entry_at and _is_clean_side(row, side, max_spread)
    ]
    if exit_rule.startswith("fixed_"):
        seconds = int(exit_rule.split("_")[1][:-1])
        exit_row = _first_at_or_after(clean_future_rows, entry_at + timedelta(seconds=seconds), exit_tolerance_seconds)
        exit_reason = f"fixed_{seconds}s"
    elif exit_rule.startswith("tp_plus_"):
        cents = int(exit_rule.split("_")[2][:-1])
        target_level = round(entry_ask + cents / 100.0, 4)
        exit_row = _first_target_hit(market_rows, side, entry_at, close_at, target_level, max_spread)
        if exit_row is not None:
            exit_reason = "target_hit"
            target_hit = 1
        else:
            exit_row = _last_clean_before_close(market_rows, side, entry_at, close_at, max_spread)
            exit_reason = "close_exit"
    elif exit_rule.startswith("tp_bid_"):
        cents = int(exit_rule.split("_")[2][:-1])
        target_level = cents / 100.0
        exit_row = _first_target_hit(market_rows, side, entry_at, close_at, target_level, max_spread)
        if exit_row is not None:
            exit_reason = "target_hit"
            target_hit = 1
        else:
            exit_row = _last_clean_before_close(market_rows, side, entry_at, close_at, max_spread)
            exit_reason = "close_exit"
    elif exit_rule == "close":
        exit_row = _last_clean_before_close(market_rows, side, entry_at, close_at, max_spread)
        exit_reason = "close_exit"
    else:
        raise ValueError(f"unknown exit rule {exit_rule}")

    row = {
        "rule_name": signal["rule_name"],
        "date_et": signal["date_et"],
        "market_ticker": signal["market_ticker"],
        "market_open_timestamp_et": signal["market_open_timestamp_et"],
        "entry_timestamp_et": signal["entry_timestamp_et"],
        "seconds_since_market_open": signal["seconds_since_market_open"],
        "contract_side_bought": side,
        "dominant_side_at_entry": signal["dominant_side"],
        "minority_side_at_entry": signal["minority_side"],
        "entry_bid": signal["entry_bid"],
        "entry_ask": signal["entry_ask"],
        "entry_spread": signal["entry_spread"],
        "btc_price_at_market_open": signal["btc_price_at_market_open"],
        "btc_price_at_entry": signal["btc_price_at_entry"],
        "btc_30s_move_before_entry": signal["btc_30s_move_before_entry"],
        "btc_60s_move_before_entry": signal["btc_60s_move_before_entry"],
        "is_weekend": signal["is_weekend"],
        "exit_rule": exit_rule,
        "target_level": target_level,
        "target_hit_flag": target_hit,
    }
    if exit_row is None:
        row.update(
            {
                "status": "NO_VALID_EXIT",
                "exit_timestamp_et": None,
                "exit_bid": None,
                "exit_reason": "no_valid_exit",
                "gross_cents": None,
                "fee_cents": None,
                "net_cents": None,
                "win_loss_flag": None,
                "time_to_target_seconds": None,
                "running_total_net_cents": None,
                "running_drawdown_cents": None,
            }
        )
        return row

    exit_bid = _price(exit_row, side, "bid")
    gross = round((exit_bid - entry_ask) * 100.0, 4)
    fee = round(_fee_cents(entry_ask, fee_rate_cents) + _fee_cents(exit_bid, fee_rate_cents), 6)
    net = round(gross - fee, 4)
    row.update(
        {
            "status": "COMPLETE",
            "exit_timestamp_et": _iso_et(exit_row["captured_at"]),
            "exit_bid": _round(exit_bid),
            "exit_reason": exit_reason,
            "gross_cents": gross,
            "fee_cents": fee,
            "net_cents": net,
            "win_loss_flag": "win" if net > 0 else "loss",
            "time_to_target_seconds": (
                _round((exit_row["captured_at"] - entry_at).total_seconds(), 3) if target_hit else None
            ),
            "running_total_net_cents": None,
            "running_drawdown_cents": None,
        }
    )
    return row


def _build_live_trades(
    market_side_rows: list[dict[str, Any]],
    rows_by_market: dict[int, list[dict[str, Any]]],
    max_spread: float,
    fee_rate_cents: float,
    exit_tolerance_seconds: int,
) -> list[dict[str, Any]]:
    trades = []
    by_ticker = {str(rows[0]["market_ticker"]): rows for rows in rows_by_market.values() if rows}
    for rule in RULES:
        for signal in _select_rule_signals(market_side_rows, rule):
            market_rows = by_ticker.get(str(signal["market_ticker"]))
            if not market_rows:
                continue
            for exit_rule in _exit_rule_names():
                trades.append(_score_exit(signal, market_rows, exit_rule, max_spread, fee_rate_cents, exit_tolerance_seconds))
    _add_running_pnl(trades)
    return trades


def _add_running_pnl(trades: list[dict[str, Any]]) -> None:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        by_key[(str(row["rule_name"]), str(row["exit_rule"]))].append(row)
    for rows in by_key.values():
        total = 0.0
        peak = 0.0
        for row in sorted(rows, key=lambda item: item["entry_timestamp_et"]):
            if row["status"] != "COMPLETE" or row["net_cents"] is None:
                continue
            total = round(total + row["net_cents"], 4)
            peak = max(peak, total)
            row["running_total_net_cents"] = total
            row["running_drawdown_cents"] = round(total - peak, 4)


def _summarize_live(trades: list[dict[str, Any]], observed_days: list[str], total_markets: int) -> list[dict[str, Any]]:
    out = []
    first_half_dates = set(observed_days[: len(observed_days) // 2])
    second_half_dates = set(observed_days[len(observed_days) // 2 :])
    for rule in RULES:
        for exit_rule in _exit_rule_names():
            group = [row for row in trades if row["rule_name"] == rule.rule_name and row["exit_rule"] == exit_rule]
            complete = [row for row in group if row["status"] == "COMPLETE" and row["net_cents"] is not None]
            nets = [row["net_cents"] for row in complete]
            gross = [row["gross_cents"] for row in complete]
            fees = [row["fee_cents"] for row in complete]
            days_with_trade = len({row["date_et"] for row in group})
            first = [row["net_cents"] for row in complete if row["date_et"] in first_half_dates]
            second = [row["net_cents"] for row in complete if row["date_et"] in second_half_dates]
            weekday = [row["net_cents"] for row in complete if row["is_weekend"] == 0]
            weekend = [row["net_cents"] for row in complete if row["is_weekend"] == 1]
            excluding_winner = list(nets)
            if excluding_winner:
                excluding_winner.remove(max(excluding_winner))
            excluding_loser = list(nets)
            if excluding_loser:
                excluding_loser.remove(min(excluding_loser))
            target_rows = [row for row in complete if row["target_level"] is not None]
            out.append(
                {
                    "rule_name": rule.rule_name,
                    "exit_rule": exit_rule,
                    "calendar_days_observed": len(observed_days),
                    "unique_markets_observed": total_markets,
                    "days_with_trade": days_with_trade,
                    "days_skipped": len(observed_days) - days_with_trade,
                    "trade_count": len(complete),
                    "average_trades_per_day": _round(len(complete) / len(observed_days), 4) if observed_days else None,
                    "win_rate_pct": _pct(sum(v > 0 for v in nets), len(nets)),
                    "target_hit_rate_pct": _pct(sum(row["target_hit_flag"] == 1 for row in target_rows), len(target_rows)) if target_rows else None,
                    "average_time_to_target_seconds": _avg(row.get("time_to_target_seconds") for row in target_rows),
                    "average_gross_cents": _avg(gross),
                    "average_fee_cents": _avg(fees),
                    "average_net_cents": _avg(nets),
                    "median_net_cents": _median(nets),
                    "total_net_cents": round(sum(nets), 4) if nets else None,
                    "profit_factor": _profit_factor(nets),
                    "best_trade": max(nets) if nets else None,
                    "worst_trade": min(nets) if nets else None,
                    "maximum_drawdown": _max_drawdown(nets),
                    "first_half_average_net": _avg(first),
                    "second_half_average_net": _avg(second),
                    "weekday_average_net": _avg(weekday),
                    "weekend_average_net": _avg(weekend),
                    "result_excluding_largest_winner": round(sum(excluding_winner), 4) if excluding_winner else None,
                    "result_excluding_largest_loser": round(sum(excluding_loser), 4) if excluding_loser else None,
                }
            )
    return out


def _daily_pnl(trades: list[dict[str, Any]], observed_days: list[str]) -> list[dict[str, Any]]:
    out = []
    by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        by_key[(str(row["rule_name"]), str(row["exit_rule"]), str(row["date_et"]))].append(row)
    for rule in RULES:
        for exit_rule in _exit_rule_names():
            total = 0.0
            peak = 0.0
            for day in observed_days:
                rows = by_key.get((rule.rule_name, exit_rule, day), [])
                complete = [row for row in rows if row["status"] == "COMPLETE" and row["net_cents"] is not None]
                day_net = round(sum(row["net_cents"] for row in complete), 4) if complete else None
                if day_net is not None:
                    total = round(total + day_net, 4)
                    peak = max(peak, total)
                out.append(
                    {
                        "date_et": day,
                        "rule_name": rule.rule_name,
                        "exit_rule": exit_rule,
                        "status": "TRADE" if complete else "NO_TRADE",
                        "trades": len(complete),
                        "day_net_cents": day_net,
                        "running_total_net_cents": total,
                        "running_drawdown_cents": round(total - peak, 4),
                    }
                )
    return out


def _data_quality(raw_rows: list[dict[str, Any]], candidates: list[dict[str, Any]], market_side: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"issue": "raw_rows_loaded", "rows_affected": len(raw_rows)},
        {"issue": "markets_loaded", "rows_affected": len({row["market_ticker"] for row in raw_rows})},
        {"issue": "quote_age_ms_unavailable_in_historical_snapshots", "rows_affected": len(raw_rows)},
        {"issue": "raw_yes_bid_gt_ask", "rows_affected": sum(row.get("yes_bid") is not None and row.get("yes_ask") is not None and row["yes_bid"] > row["yes_ask"] for row in raw_rows)},
        {"issue": "raw_no_bid_gt_ask", "rows_affected": sum(row.get("no_bid") is not None and row.get("no_ask") is not None and row["no_bid"] > row["no_ask"] for row in raw_rows)},
        {"issue": "raw_yes_spread_eq_zero", "rows_affected": sum(row.get("yes_spread") == 0 for row in raw_rows)},
        {"issue": "raw_no_spread_eq_zero", "rows_affected": sum(row.get("no_spread") == 0 for row in raw_rows)},
        {"issue": "raw_yes_spread_lt_zero", "rows_affected": sum(row.get("yes_spread") is not None and row["yes_spread"] < 0 for row in raw_rows)},
        {"issue": "raw_no_spread_lt_zero", "rows_affected": sum(row.get("no_spread") is not None and row["no_spread"] < 0 for row in raw_rows)},
        {"issue": "eligible_quote_level_candidates", "rows_affected": len(candidates)},
        {"issue": "deduped_market_side_candidates", "rows_affected": len(market_side)},
    ]


def _interpret_live(summary: list[dict[str, Any]]) -> str:
    complete = [row for row in summary if row.get("trade_count") and row.get("average_net_cents") is not None]
    positive = [row for row in complete if row["average_net_cents"] > 0]
    if not positive:
        return "No live-style rule/exit combination produced positive average net after fees."
    best = max(positive, key=lambda row: (row["average_net_cents"], row["trade_count"]))
    close_best = [row for row in positive if row["exit_rule"] == "close"]
    short_best = [row for row in positive if row["exit_rule"] in {"fixed_60s", "fixed_120s", "tp_plus_10c_else_close"}]
    behavior = "quick scalp or modest rebound" if short_best else "later option-style rebound"
    if close_best and best["exit_rule"] == "close":
        behavior = "hold-to-close option-style trade"
    return (
        f"Best positive live-style row: {best['rule_name']} / {best['exit_rule']} "
        f"with avg net {best['average_net_cents']}c over {best['trade_count']} trades. "
        f"Classification: {behavior}."
    )


def _render_markdown(
    paths: Paths,
    version_summaries: list[dict[str, Any]],
    bucket_summary: list[dict[str, Any]],
    breakdown_summary: list[dict[str, Any]],
    live_summary: list[dict[str, Any]],
    dq: list[dict[str, Any]],
) -> str:
    version_b = next((row for row in version_summaries if row["version"] == "B_market_side_first"), {})
    bucket_b = [row for row in bucket_summary if row["version"] == "B_market_side_first"]
    top_bucket = max(bucket_b, key=lambda row: (row.get("plus_10c_hit_rate_before_close") or -1, row.get("sample_size") or 0), default={})
    live_ranked = sorted(
        live_summary,
        key=lambda row: row.get("average_net_cents") if row.get("average_net_cents") is not None else -999999,
        reverse=True,
    )
    hit_120 = version_b.get("plus_10c_hit_rate_120s")
    hit_close = version_b.get("plus_10c_hit_rate_before_close")
    speed_answer = "Insufficient data."
    if hit_120 is not None and hit_close is not None:
        speed_answer = (
            "Fast enough to be scalp-relevant."
            if hit_close == 0 or hit_120 >= hit_close * 0.65
            else "Mostly later-before-close; not mainly a fast scalp."
        )

    return f"""# Below-35c Cheap Minority Rebound Report

## Scope

- Market: Kalshi BTC 15-minute contracts
- Entry lifecycle: first 5 minutes after market open
- Contract: minority side only
- Entry ask: `< 0.35`
- Entry: ask
- Exit measurement: future bid path
- Clean quote requirement: both sides clean, entry spread <= 0.01
- Hours: all logged hours, no hour-of-day filter

## Direct Answers

1. Deduped market-side +10c before-close rate: `{version_b.get("plus_10c_hit_rate_before_close")}`%.
2. Speed: `{speed_answer}` 120s rate `{version_b.get("plus_10c_hit_rate_120s")}`%, 300s rate `{version_b.get("plus_10c_hit_rate_300s")}`%, before-close rate `{version_b.get("plus_10c_hit_rate_before_close")}`%.
3. Strongest +10c bucket by before-close rate: `{top_bucket.get("entry_bucket")}` with `{top_bucket.get("plus_10c_hit_rate_before_close")}`% over `{top_bucket.get("sample_size")}` deduped signals.
4. Expanding from 20-30c to below 35c is answered in the bucket table; compare `0.20-0.25`, `0.25-0.30`, `0.30-0.35`, and combined `0.20-0.35`.
5. Live-style result: `{_interpret_live(live_summary)}`
6. Classification depends on which exits dominate; see live-style summary. If close exits dominate, this is not a scalp.
7. Prospective TEST candidate should only come from rows with positive net, stable halves, and no single-winner dependence.

## Probability Version Summary

{_table(version_summaries, ["version", "entry_bucket", "sample_size", "unique_markets", "unique_days", "plus_10c_hit_rate_60s", "plus_10c_hit_rate_120s", "plus_10c_hit_rate_180s", "plus_10c_hit_rate_300s", "plus_10c_hit_rate_before_close", "average_time_to_plus_10c_seconds", "first_half_plus_10c_before_close_rate", "second_half_plus_10c_before_close_rate"], 10)}

## Entry Bucket Summary

{_table(bucket_b, ["entry_bucket", "sample_size", "unique_markets", "unique_days", "plus_3c_hit_rate_120s", "plus_5c_hit_rate_120s", "plus_10c_hit_rate_120s", "plus_10c_hit_rate_300s", "plus_10c_hit_rate_before_close", "average_max_bid_increase_before_close_cents", "median_max_bid_increase_before_close_cents", "average_time_to_plus_10c_seconds"], 20)}

## Breakdown Preview

{_table([row for row in breakdown_summary if row["version"] == "B_market_side_first"], ["breakdown_type", "breakdown_value", "sample_size", "unique_markets", "plus_10c_hit_rate_120s", "plus_10c_hit_rate_300s", "plus_10c_hit_rate_before_close", "average_max_bid_increase_before_close_cents"], 30)}

## Live-Style Summary

{_table(live_ranked, ["rule_name", "exit_rule", "calendar_days_observed", "unique_markets_observed", "days_with_trade", "days_skipped", "trade_count", "average_trades_per_day", "win_rate_pct", "target_hit_rate_pct", "average_time_to_target_seconds", "average_net_cents", "median_net_cents", "total_net_cents", "profit_factor", "maximum_drawdown", "result_excluding_largest_winner", "result_excluding_largest_loser"], 40)}

## Data Quality

{_table(dq, ["issue", "rows_affected"], 20)}

## Output Files

- Candidate-level probability CSV: `{paths.candidate_probability_csv}`
- Market-side first probability CSV: `{paths.market_side_first_probability_csv}`
- Market-level probability CSV: `{paths.market_level_probability_csv}`
- Bucket summary CSV: `{paths.bucket_summary_csv}`
- Breakdown summary CSV: `{paths.breakdown_summary_csv}`
- Live-style trade CSV: `{paths.live_style_trade_csv}`
- Live-style summary CSV: `{paths.live_style_summary_csv}`
- Daily P/L CSV: `{paths.daily_pnl_csv}`
- Data quality CSV: `{paths.data_quality_csv}`

## Research Warning

Version A is quote-level and duplicate-heavy. Version B is the main non-duplicate probability view. Live-style rules are historical simulations, not live-trading recommendations.
"""


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"below_35c_cheap_minority_rebound_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        candidate_probability_csv=output_dir / f"{stem}_candidates.csv",
        market_side_first_probability_csv=output_dir / f"{stem}_market_side_first.csv",
        market_level_probability_csv=output_dir / f"{stem}_market_level.csv",
        bucket_summary_csv=output_dir / f"{stem}_bucket_summary.csv",
        breakdown_summary_csv=output_dir / f"{stem}_breakdown_summary.csv",
        live_style_trade_csv=output_dir / f"{stem}_live_style_trades.csv",
        live_style_summary_csv=output_dir / f"{stem}_live_style_summary.csv",
        daily_pnl_csv=output_dir / f"{stem}_daily_pnl.csv",
        data_quality_csv=output_dir / f"{stem}_data_quality.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def build_report(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    market_like: str = "KXBTC15M%",
    start: Optional[str] = None,
    end: Optional[str] = None,
    max_spread: float = 0.01,
    fee_rate_cents: float = 7.0,
    exit_tolerance_seconds: int = 20,
) -> Paths:
    paths = _paths(output_dir)
    raw_rows = _load_rows(market_like, start, end)
    rows_by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        rows_by_market[int(row["market_pk"])].append(row)
    for rows in rows_by_market.values():
        rows.sort(key=lambda row: row["captured_at"])

    candidates = _build_candidates(rows_by_market, max_spread)
    market_side = _dedupe_market_side(candidates)
    market_side_keys = {(row["market_ticker"], row["contract_side"], row["entry_timestamp_utc"]) for row in market_side}
    for row in candidates:
        row["version_b_market_side_first"] = int((row["market_ticker"], row["contract_side"], row["entry_timestamp_utc"]) in market_side_keys)
    market_level = _market_level_probability(market_side)

    version_summaries = [
        _probability_summary("A_quote_level", candidates),
        _probability_summary("B_market_side_first", market_side),
        _market_probability_summary(market_level),
    ]
    bucket_summary = [
        *_bucket_summary(candidates, "A_quote_level"),
        *_bucket_summary(market_side, "B_market_side_first"),
    ]
    breakdown_summary = [
        *_breakdown_summary(candidates, "A_quote_level"),
        *_breakdown_summary(market_side, "B_market_side_first"),
    ]
    observed_days = _observed_days(raw_rows)
    live_trades = _build_live_trades(market_side, rows_by_market, max_spread, fee_rate_cents, exit_tolerance_seconds)
    live_summary = _summarize_live(live_trades, observed_days, len(rows_by_market))
    daily_pnl = _daily_pnl(live_trades, observed_days)
    dq = _data_quality(raw_rows, candidates, market_side)

    _write_csv(paths.candidate_probability_csv, candidates)
    _write_csv(paths.market_side_first_probability_csv, market_side)
    _write_csv(paths.market_level_probability_csv, market_level)
    _write_csv(paths.bucket_summary_csv, bucket_summary)
    _write_csv(paths.breakdown_summary_csv, breakdown_summary)
    _write_csv(paths.live_style_trade_csv, live_trades)
    _write_csv(paths.live_style_summary_csv, live_summary)
    _write_csv(paths.daily_pnl_csv, daily_pnl)
    _write_csv(paths.data_quality_csv, dq)
    paths.markdown_report.write_text(
        _render_markdown(paths, version_summaries, bucket_summary, breakdown_summary, live_summary, dq)
    )
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Build below-35c cheap minority rebound probability and live-style report.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M%")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-spread", type=float, default=0.01)
    parser.add_argument("--fee-rate-cents", type=float, default=7.0)
    parser.add_argument("--exit-tolerance-seconds", type=int, default=20)
    args = parser.parse_args()

    paths = build_report(
        output_dir=args.output_dir,
        market_like=args.market_like,
        start=args.start,
        end=args.end,
        max_spread=args.max_spread,
        fee_rate_cents=args.fee_rate_cents,
        exit_tolerance_seconds=args.exit_tolerance_seconds,
    )
    print("Below-35c cheap minority rebound report complete")
    print(f"candidate_probability_csv={paths.candidate_probability_csv}")
    print(f"market_side_first_probability_csv={paths.market_side_first_probability_csv}")
    print(f"market_level_probability_csv={paths.market_level_probability_csv}")
    print(f"bucket_summary_csv={paths.bucket_summary_csv}")
    print(f"breakdown_summary_csv={paths.breakdown_summary_csv}")
    print(f"live_style_trade_csv={paths.live_style_trade_csv}")
    print(f"live_style_summary_csv={paths.live_style_summary_csv}")
    print(f"daily_pnl_csv={paths.daily_pnl_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
