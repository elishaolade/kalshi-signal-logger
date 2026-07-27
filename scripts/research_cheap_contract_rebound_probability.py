#!/usr/bin/env python3
"""
Build an 08:00-11:00 ET cheap-contract rebound probability report.

This is a descriptive historical report. It evaluates clean executable quotes in
the first 120 seconds of Kalshi BTC 15-minute markets and measures whether a
cheap contract later had a bid rebound of +3c, +5c, or +10c.
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.research_daily_time_window_scalp_report import (  # noqa: E402
    ET,
    _avg,
    _clean_row,
    _dominant_side,
    _dt,
    _f,
    _is_clean_side,
    _load_rows,
    _median,
    _minority_side,
    _pct,
    _price,
    _table,
    _write_csv,
)

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "cheap_contract_rebound_probability"

BASE_BUCKETS = (
    ("0.05-0.10", 0.05, 0.10),
    ("0.10-0.20", 0.10, 0.20),
    ("0.20-0.30", 0.20, 0.30),
    ("0.30-0.40", 0.30, 0.40),
)
COMBINED_BUCKETS = (
    ("0.10-0.40", 0.10, 0.40),
    ("0.05-0.40", 0.05, 0.40),
)
HORIZONS = (60, 120, 180)
TARGETS = (3, 5, 10)


@dataclass(frozen=True)
class Paths:
    directory: Path
    candidate_csv: Path
    bucket_summary_csv: Path
    market_level_csv: Path
    day_level_csv: Path
    version_summary_csv: Path
    data_quality_csv: Path
    markdown_report: Path


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _iso_et(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(ET).replace(tzinfo=None).isoformat(sep=" ")


def _bucket(value: float | None, buckets: Iterable[tuple[str, float, float]]) -> str | None:
    if value is None:
        return None
    for label, low, high in buckets:
        if value >= low and value < high:
            return label
    return None


def _entry_bucket(value: float | None) -> str | None:
    return _bucket(value, BASE_BUCKETS)


def _entry_elapsed_bucket(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds <= 30:
        return "0-30s"
    if seconds <= 60:
        return "30-60s"
    if seconds <= 120:
        return "60-120s"
    return "out_of_window"


def _alignment_bucket(side: str, btc_move_60s: float | None) -> str:
    if btc_move_60s is None or btc_move_60s == 0:
        return "unknown"
    aligned_side = "YES" if btc_move_60s > 0 else "NO"
    return "aligned" if side == aligned_side else "against"


def _minority_bucket(side: str, dominant_side: str | None) -> str:
    if dominant_side is None:
        return "unknown"
    return "minority" if side == _minority_side(dominant_side) else "not_minority"


def _last_at_or_before(rows: list[dict[str, Any]], ts: datetime) -> dict[str, Any] | None:
    out = None
    for row in rows:
        if row["captured_at"] <= ts:
            out = row
        else:
            break
    return out


def _max_future_bid(rows: list[dict[str, Any]], side: str, entry_at: datetime, end_at: datetime, max_spread: float) -> float | None:
    bids = [
        _price(row, side, "bid")
        for row in rows
        if row["captured_at"] > entry_at and row["captured_at"] <= end_at and _is_clean_side(row, side, max_spread)
    ]
    vals = [bid for bid in bids if bid is not None]
    if not vals:
        return None
    return max(vals)


def _candidate_from_row(
    market_rows: list[dict[str, Any]],
    row: dict[str, Any],
    side: str,
    max_spread: float,
) -> dict[str, Any] | None:
    ask = _price(row, side, "ask")
    bid = _price(row, side, "bid")
    spread = _price(row, side, "spread")
    if ask is None or bid is None or spread is None:
        return None
    if not (0.05 <= ask < 0.40):
        return None
    if not _is_clean_side(row, side, max_spread):
        return None

    opens_at = row["opens_at"]
    closes_at = row["closes_at"]
    entry_at = row["captured_at"]
    seconds_since_open = (entry_at - opens_at).total_seconds()
    if seconds_since_open < 0 or seconds_since_open > 120:
        return None
    entry_et = entry_at.astimezone(ET)
    if not (8 <= entry_et.hour < 11):
        return None

    prev_30 = _last_at_or_before(market_rows, entry_at - timedelta(seconds=30))
    prev_60 = _last_at_or_before(market_rows, entry_at - timedelta(seconds=60))
    btc_move_30s = row["btc_price"] - prev_30["btc_price"] if prev_30 else None
    btc_move_60s = row["btc_price"] - prev_60["btc_price"] if prev_60 else None
    dominant = _dominant_side(row)

    out: dict[str, Any] = {
        "market_ticker": row["market_ticker"],
        "market_pk": row["market_pk"],
        "market_open_timestamp_et": _iso_et(opens_at),
        "market_close_timestamp_et": _iso_et(closes_at),
        "entry_timestamp_et": _iso_et(entry_at),
        "entry_timestamp_utc": entry_at.replace(tzinfo=None).isoformat(sep=" "),
        "date_et": entry_et.date().isoformat(),
        "weekday": entry_et.strftime("%A"),
        "is_weekend": 1 if entry_et.weekday() >= 5 else 0,
        "seconds_since_market_open": _round(seconds_since_open, 3),
        "contract_side": side,
        "entry_ask": _round(ask),
        "entry_bid": _round(bid),
        "entry_spread": _round(spread),
        "entry_bucket": _entry_bucket(ask),
        "btc_price_at_entry": _round(row["btc_price"], 2),
        "btc_move_prior_30s": _round(btc_move_30s, 2),
        "btc_move_prior_60s": _round(btc_move_60s, 2),
        "dominant_side_at_entry": dominant,
        "btc_60s_alignment": _alignment_bucket(side, btc_move_60s),
        "is_minority_side": 1 if _minority_bucket(side, dominant) == "minority" else 0,
        "minority_status": _minority_bucket(side, dominant),
        "entry_elapsed_bucket": _entry_elapsed_bucket(seconds_since_open),
        "version_b_market_side_first": 0,
    }

    for seconds in HORIZONS:
        max_bid = _max_future_bid(market_rows, side, entry_at, entry_at + timedelta(seconds=seconds), max_spread)
        increase = (max_bid - ask) * 100.0 if max_bid is not None else None
        out[f"max_future_bid_{seconds}s"] = _round(max_bid)
        out[f"max_bid_increase_{seconds}s_cents"] = _round(increase, 4)
        for target in TARGETS:
            out[f"hit_plus_{target}c_within_{seconds}s"] = 1 if increase is not None and increase >= target else 0

    max_bid_close = _max_future_bid(market_rows, side, entry_at, closes_at, max_spread)
    increase_close = (max_bid_close - ask) * 100.0 if max_bid_close is not None else None
    out["max_future_bid_before_close"] = _round(max_bid_close)
    out["max_bid_increase_before_close_cents"] = _round(increase_close, 4)
    for target in TARGETS:
        out[f"hit_plus_{target}c_before_close"] = 1 if increase_close is not None and increase_close >= target else 0

    return out


def _build_candidates(rows_by_market: dict[int, list[dict[str, Any]]], max_spread: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for market_rows in rows_by_market.values():
        market_rows.sort(key=lambda row: row["captured_at"])
        for row in market_rows:
            if not _clean_row(row, max_spread):
                continue
            for side in ("YES", "NO"):
                candidate = _candidate_from_row(market_rows, row, side, max_spread)
                if candidate:
                    candidates.append(candidate)
    return sorted(candidates, key=lambda row: (row["entry_timestamp_utc"], row["market_ticker"], row["contract_side"]))


def _dedupe_market_side(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in sorted(candidates, key=lambda item: (item["entry_timestamp_utc"], item["market_ticker"], item["contract_side"])):
        key = (str(row["market_ticker"]), str(row["contract_side"]))
        if key in seen:
            continue
        seen.add(key)
        copy = dict(row)
        copy["version_b_market_side_first"] = 1
        out.append(copy)
    return out


def _rows_for_bucket(rows: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    bucket_defs = dict((name, (low, high)) for name, low, high in (*BASE_BUCKETS, *COMBINED_BUCKETS))
    low, high = bucket_defs[label]
    return [row for row in rows if row["entry_ask"] is not None and row["entry_ask"] >= low and row["entry_ask"] < high]


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "candidate_count": len(rows),
        "unique_markets": len({row["market_ticker"] for row in rows}),
        "unique_days": len({row["date_et"] for row in rows}),
        "average_entry_ask": _avg(row.get("entry_ask") for row in rows),
        "median_entry_ask": _median(row.get("entry_ask") for row in rows),
        "average_spread": _avg(row.get("entry_spread") for row in rows),
        "median_spread": _median(row.get("entry_spread") for row in rows),
    }
    for seconds in HORIZONS:
        increases = [row.get(f"max_bid_increase_{seconds}s_cents") for row in rows]
        out[f"average_max_bid_increase_{seconds}s_cents"] = _avg(increases)
        out[f"median_max_bid_increase_{seconds}s_cents"] = _median(increases)
        for target in TARGETS:
            out[f"plus_{target}c_hit_rate_{seconds}s"] = _pct(
                sum(row.get(f"hit_plus_{target}c_within_{seconds}s") == 1 for row in rows),
                len(rows),
            )
    close_increases = [row.get("max_bid_increase_before_close_cents") for row in rows]
    out["average_max_bid_increase_before_close_cents"] = _avg(close_increases)
    out["median_max_bid_increase_before_close_cents"] = _median(close_increases)
    for target in TARGETS:
        out[f"plus_{target}c_hit_rate_before_close"] = _pct(
            sum(row.get(f"hit_plus_{target}c_before_close") == 1 for row in rows),
            len(rows),
        )
    return out


def _bucket_summary_for(version: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    breakdowns = [
        ("all", "all", rows),
        ("btc_60s_alignment", "aligned", [row for row in rows if row["btc_60s_alignment"] == "aligned"]),
        ("btc_60s_alignment", "against", [row for row in rows if row["btc_60s_alignment"] == "against"]),
        ("btc_60s_alignment", "unknown", [row for row in rows if row["btc_60s_alignment"] == "unknown"]),
        ("minority_status", "minority", [row for row in rows if row["minority_status"] == "minority"]),
        ("minority_status", "not_minority", [row for row in rows if row["minority_status"] == "not_minority"]),
        ("minority_status", "unknown", [row for row in rows if row["minority_status"] == "unknown"]),
        ("entry_elapsed_bucket", "0-30s", [row for row in rows if row["entry_elapsed_bucket"] == "0-30s"]),
        ("entry_elapsed_bucket", "30-60s", [row for row in rows if row["entry_elapsed_bucket"] == "30-60s"]),
        ("entry_elapsed_bucket", "60-120s", [row for row in rows if row["entry_elapsed_bucket"] == "60-120s"]),
    ]
    for bucket_label, _, _ in (*BASE_BUCKETS, *COMBINED_BUCKETS):
        for breakdown_type, breakdown_value, breakdown_rows in breakdowns:
            group = _rows_for_bucket(breakdown_rows, bucket_label)
            row = {
                "version": version,
                "entry_bucket": bucket_label,
                "breakdown_type": breakdown_type,
                "breakdown_value": breakdown_value,
            }
            row.update(_summarize_rows(group))
            out.append(row)
    return out


def _split_rates(rows: list[dict[str, Any]], hit_field: str) -> tuple[float | None, float | None]:
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


def _version_summary(version: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    first, second = _split_rates(rows, "hit_plus_10c_before_close")
    weekdays = [row for row in rows if row["is_weekend"] == 0]
    weekends = [row for row in rows if row["is_weekend"] == 1]
    out: dict[str, Any] = {
        "version": version,
        "sample_size": len(rows),
        "unique_markets": len({row["market_ticker"] for row in rows}),
        "unique_days": len({row["date_et"] for row in rows}),
        "first_half_plus_10c_before_close_rate": first,
        "second_half_plus_10c_before_close_rate": second,
        "weekday_plus_10c_before_close_rate": _pct(sum(row["hit_plus_10c_before_close"] == 1 for row in weekdays), len(weekdays)),
        "weekend_plus_10c_before_close_rate": _pct(sum(row["hit_plus_10c_before_close"] == 1 for row in weekends), len(weekends)),
    }
    out.update(_summarize_rows(rows))
    return out


def _day_level_rows(rows_by_market: dict[int, list[dict[str, Any]]], market_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observed_days: set[str] = set()
    markets_seen_by_day: dict[str, set[str]] = defaultdict(set)
    for rows in rows_by_market.values():
        if not rows:
            continue
        ticker = str(rows[0]["market_ticker"])
        for row in rows:
            captured_et = row["captured_at"].astimezone(ET)
            seconds_since_open = (row["captured_at"] - row["opens_at"]).total_seconds()
            if 8 <= captured_et.hour < 11 and 0 <= seconds_since_open <= 120:
                observed_days.add(captured_et.date().isoformat())
                markets_seen_by_day[captured_et.date().isoformat()].add(ticker)

    candidates_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in market_rows:
        candidates_by_day[row["date_et"]].append(row)

    out: list[dict[str, Any]] = []
    for day in sorted(observed_days):
        rows = candidates_by_day.get(day, [])
        item: dict[str, Any] = {
            "date_et": day,
            "markets_observed_in_window": len(markets_seen_by_day.get(day, set())),
            "eligible_market_side_candidates": len(rows),
            "had_eligible_cheap_contract": 1 if rows else 0,
            "weekday": datetime.fromisoformat(day).strftime("%A"),
            "is_weekend": 1 if datetime.fromisoformat(day).weekday() >= 5 else 0,
        }
        for seconds in HORIZONS:
            item[f"day_had_plus_10c_within_{seconds}s"] = 1 if any(row[f"hit_plus_10c_within_{seconds}s"] == 1 for row in rows) else 0
        item["day_had_plus_10c_before_close"] = 1 if any(row["hit_plus_10c_before_close"] == 1 for row in rows) else 0
        item["best_max_bid_increase_120s_cents"] = max(
            [row["max_bid_increase_120s_cents"] for row in rows if row["max_bid_increase_120s_cents"] is not None],
            default=None,
        )
        item["best_max_bid_increase_before_close_cents"] = max(
            [row["max_bid_increase_before_close_cents"] for row in rows if row["max_bid_increase_before_close_cents"] is not None],
            default=None,
        )
        out.append(item)
    return out


def _day_version_summary(day_rows: list[dict[str, Any]]) -> dict[str, Any]:
    first, second = _split_day_rates(day_rows, "day_had_plus_10c_before_close")
    weekdays = [row for row in day_rows if row["is_weekend"] == 0]
    weekends = [row for row in day_rows if row["is_weekend"] == 1]
    out: dict[str, Any] = {
        "version": "C_day_level",
        "sample_size": len(day_rows),
        "days_with_eligible_contract": sum(row["had_eligible_cheap_contract"] == 1 for row in day_rows),
        "first_half_plus_10c_before_close_rate": first,
        "second_half_plus_10c_before_close_rate": second,
        "weekday_plus_10c_before_close_rate": _pct(sum(row["day_had_plus_10c_before_close"] == 1 for row in weekdays), len(weekdays)),
        "weekend_plus_10c_before_close_rate": _pct(sum(row["day_had_plus_10c_before_close"] == 1 for row in weekends), len(weekends)),
        "average_max_bid_increase_120s_cents": _avg(row.get("best_max_bid_increase_120s_cents") for row in day_rows),
        "median_max_bid_increase_120s_cents": _median(row.get("best_max_bid_increase_120s_cents") for row in day_rows),
        "average_max_bid_increase_before_close_cents": _avg(row.get("best_max_bid_increase_before_close_cents") for row in day_rows),
        "median_max_bid_increase_before_close_cents": _median(row.get("best_max_bid_increase_before_close_cents") for row in day_rows),
    }
    for seconds in HORIZONS:
        out[f"plus_10c_hit_rate_{seconds}s"] = _pct(sum(row[f"day_had_plus_10c_within_{seconds}s"] == 1 for row in day_rows), len(day_rows))
    out["plus_10c_hit_rate_before_close"] = _pct(sum(row["day_had_plus_10c_before_close"] == 1 for row in day_rows), len(day_rows))
    return out


def _split_day_rates(rows: list[dict[str, Any]], hit_field: str) -> tuple[float | None, float | None]:
    if not rows:
        return None, None
    ordered = sorted(rows, key=lambda row: row["date_et"])
    midpoint = len(ordered) // 2
    first = ordered[:midpoint]
    second = ordered[midpoint:]
    return (
        _pct(sum(row.get(hit_field) == 1 for row in first), len(first)),
        _pct(sum(row.get(hit_field) == 1 for row in second), len(second)),
    )


def _data_quality(raw_rows: list[dict[str, Any]], candidates: list[dict[str, Any]], market_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def item(issue: str, rows_affected: int) -> dict[str, Any]:
        return {"issue": issue, "rows_affected": rows_affected}

    return [
        item("raw_yes_bid_gt_ask", sum(row.get("yes_bid") is not None and row.get("yes_ask") is not None and row["yes_bid"] > row["yes_ask"] for row in raw_rows)),
        item("raw_no_bid_gt_ask", sum(row.get("no_bid") is not None and row.get("no_ask") is not None and row["no_bid"] > row["no_ask"] for row in raw_rows)),
        item("raw_yes_spread_eq_zero", sum(row.get("yes_spread") == 0 for row in raw_rows)),
        item("raw_no_spread_eq_zero", sum(row.get("no_spread") == 0 for row in raw_rows)),
        item("raw_yes_spread_lt_zero", sum(row.get("yes_spread") is not None and row["yes_spread"] < 0 for row in raw_rows)),
        item("raw_no_spread_lt_zero", sum(row.get("no_spread") is not None and row["no_spread"] < 0 for row in raw_rows)),
        item("eligible_quote_candidates", len(candidates)),
        item("deduped_market_side_candidates", len(market_rows)),
    ]


def _render_markdown(
    paths: Paths,
    version_summaries: list[dict[str, Any]],
    bucket_summaries: list[dict[str, Any]],
    day_rows: list[dict[str, Any]],
    dq: list[dict[str, Any]],
    max_spread: float,
) -> str:
    version_b = next((row for row in version_summaries if row["version"] == "B_market_side_first"), {})
    version_c = next((row for row in version_summaries if row["version"] == "C_day_level"), {})
    all_b = next(
        (
            row
            for row in bucket_summaries
            if row["version"] == "B_market_side_first"
            and row["entry_bucket"] == "0.05-0.40"
            and row["breakdown_type"] == "all"
        ),
        {},
    )
    aligned = next(
        (
            row
            for row in bucket_summaries
            if row["version"] == "B_market_side_first"
            and row["entry_bucket"] == "0.05-0.40"
            and row["breakdown_type"] == "btc_60s_alignment"
            and row["breakdown_value"] == "aligned"
        ),
        {},
    )
    against = next(
        (
            row
            for row in bucket_summaries
            if row["version"] == "B_market_side_first"
            and row["entry_bucket"] == "0.05-0.40"
            and row["breakdown_type"] == "btc_60s_alignment"
            and row["breakdown_value"] == "against"
        ),
        {},
    )

    scalp_note = "Insufficient deduped market-side data to classify the effect."
    hit_120 = all_b.get("plus_10c_hit_rate_120s")
    hit_close = all_b.get("plus_10c_hit_rate_before_close")
    if hit_120 is not None and hit_close is not None:
        if hit_120 >= hit_close * 0.7:
            scalp_note = "Most of the +10c effect appears quickly enough to be scalp-relevant."
        else:
            scalp_note = "The +10c effect looks more like eventual rebound than a short scalp; before-close hits are materially higher than 120s hits."

    top_bucket_rows = [
        row
        for row in bucket_summaries
        if row["version"] == "B_market_side_first" and row["breakdown_type"] == "all"
    ]
    top_bucket_rows.sort(key=lambda row: (-(row.get("plus_10c_hit_rate_120s") or -1), -row.get("candidate_count", 0)))

    return f"""# 08:00-11:00 ET Cheap-Contract Rebound Probability Report

## Scope

- Market: Kalshi BTC 15-minute contracts
- Time window: 08:00-11:00 ET
- Entry lifecycle window: first 120 seconds after market open
- Entry model: buy at ask
- Exit measurement: future bid path
- Clean quote requirement: both sides clean, entry spread <= `{max_spread}`, no crossed books, no zero/negative spreads
- Main non-duplicate view: Version B, first eligible cheap quote per side per market
- This is descriptive probability work, not a live strategy.

## Direct Answer

Using the deduped market-side view over all cheap entries `0.05-0.40`:

- Chance of +10c within 60 seconds: `{all_b.get("plus_10c_hit_rate_60s")}`%
- Chance of +10c within 120 seconds: `{all_b.get("plus_10c_hit_rate_120s")}`%
- Chance of +10c within 180 seconds: `{all_b.get("plus_10c_hit_rate_180s")}`%
- Chance of +10c before market close: `{all_b.get("plus_10c_hit_rate_before_close")}`%

BTC-aligned cheap contracts: `{aligned.get("plus_10c_hit_rate_120s")}`% within 120s, `{aligned.get("plus_10c_hit_rate_before_close")}`% before close.
Countertrend cheap contracts: `{against.get("plus_10c_hit_rate_120s")}`% within 120s, `{against.get("plus_10c_hit_rate_before_close")}`% before close.

{scalp_note}

## Version Summary

{_table(version_summaries, ["version", "sample_size", "unique_markets", "unique_days", "plus_10c_hit_rate_60s", "plus_10c_hit_rate_120s", "plus_10c_hit_rate_180s", "plus_10c_hit_rate_before_close", "average_max_bid_increase_120s_cents", "median_max_bid_increase_120s_cents", "first_half_plus_10c_before_close_rate", "second_half_plus_10c_before_close_rate"], 10)}

## Bucket Summary

{_table(top_bucket_rows, ["entry_bucket", "candidate_count", "unique_markets", "unique_days", "plus_3c_hit_rate_120s", "plus_5c_hit_rate_120s", "plus_10c_hit_rate_120s", "plus_10c_hit_rate_before_close", "average_max_bid_increase_120s_cents", "median_max_bid_increase_120s_cents", "average_entry_ask", "median_spread"], 20)}

## Breakdown Preview

{_table([row for row in bucket_summaries if row["version"] == "B_market_side_first" and row["entry_bucket"] == "0.05-0.40" and row["breakdown_type"] != "all"], ["breakdown_type", "breakdown_value", "candidate_count", "plus_10c_hit_rate_60s", "plus_10c_hit_rate_120s", "plus_10c_hit_rate_180s", "plus_10c_hit_rate_before_close", "average_max_bid_increase_120s_cents"], 20)}

## Day-Level View

{_table(day_rows, ["date_et", "markets_observed_in_window", "eligible_market_side_candidates", "had_eligible_cheap_contract", "day_had_plus_10c_within_120s", "day_had_plus_10c_before_close", "best_max_bid_increase_120s_cents"], 20)}

## Data Quality

{_table(dq, ["issue", "rows_affected"], 20)}

## Output Files

- Candidate-level CSV: `{paths.candidate_csv}`
- Bucket summary CSV: `{paths.bucket_summary_csv}`
- Market-level summary CSV: `{paths.market_level_csv}`
- Day-level summary CSV: `{paths.day_level_csv}`
- Version summary CSV: `{paths.version_summary_csv}`
- Data quality CSV: `{paths.data_quality_csv}`

## Research Warning

Version A is quote-level and can overweight markets with many repeated snapshots. Use Version B for the cleanest probability estimate and Version C for the daily opportunity question.
"""


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"cheap_contract_rebound_probability_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        candidate_csv=output_dir / f"{stem}_candidates.csv",
        bucket_summary_csv=output_dir / f"{stem}_bucket_summary.csv",
        market_level_csv=output_dir / f"{stem}_market_level.csv",
        day_level_csv=output_dir / f"{stem}_day_level.csv",
        version_summary_csv=output_dir / f"{stem}_version_summary.csv",
        data_quality_csv=output_dir / f"{stem}_data_quality.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def build_report(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    market_like: str = "KXBTC15M%",
    start: str | None = None,
    end: str | None = None,
    max_spread: float = 0.01,
) -> Paths:
    paths = _paths(output_dir)
    raw_rows = _load_rows(market_like, start, end)
    rows_by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        rows_by_market[int(row["market_pk"])].append(row)
    for rows in rows_by_market.values():
        rows.sort(key=lambda row: row["captured_at"])

    candidates = _build_candidates(rows_by_market, max_spread)
    market_level = _dedupe_market_side(candidates)
    candidate_keys = {(row["market_ticker"], row["contract_side"], row["entry_timestamp_utc"]) for row in market_level}
    for row in candidates:
        row["version_b_market_side_first"] = 1 if (row["market_ticker"], row["contract_side"], row["entry_timestamp_utc"]) in candidate_keys else 0

    bucket_summaries = [
        *_bucket_summary_for("A_quote_level", candidates),
        *_bucket_summary_for("B_market_side_first", market_level),
    ]
    day_rows = _day_level_rows(rows_by_market, market_level)
    version_summaries = [
        _version_summary("A_quote_level", candidates),
        _version_summary("B_market_side_first", market_level),
        _day_version_summary(day_rows),
    ]
    dq = _data_quality(raw_rows, candidates, market_level)

    _write_csv(paths.candidate_csv, candidates)
    _write_csv(paths.bucket_summary_csv, bucket_summaries)
    _write_csv(paths.market_level_csv, market_level)
    _write_csv(paths.day_level_csv, day_rows)
    _write_csv(paths.version_summary_csv, version_summaries)
    _write_csv(paths.data_quality_csv, dq)
    paths.markdown_report.write_text(_render_markdown(paths, version_summaries, bucket_summaries, day_rows, dq, max_spread))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Build 08:00-11:00 ET cheap-contract rebound probability report.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M%")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-spread", type=float, default=0.01)
    args = parser.parse_args()

    paths = build_report(args.output_dir, args.market_like, args.start, args.end, args.max_spread)
    print("Cheap-contract rebound probability report complete")
    print(f"candidate_csv={paths.candidate_csv}")
    print(f"bucket_summary_csv={paths.bucket_summary_csv}")
    print(f"market_level_csv={paths.market_level_csv}")
    print(f"day_level_csv={paths.day_level_csv}")
    print(f"version_summary_csv={paths.version_summary_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
