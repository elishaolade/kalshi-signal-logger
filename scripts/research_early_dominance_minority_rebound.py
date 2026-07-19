#!/usr/bin/env python3
"""
Research report: early dominant-contract extremes and minority-contract rebounds.

Hypothesis:
  In the first five minutes of a Kalshi BTC 15m market, one side can become
  dominant unusually quickly. The opposing cheap/minority contract may then
  show an executable short-term rebound even when it ultimately settles at 0.

This report uses market/contract snapshots, not strategy trade rows.
Execution assumptions:
  - Entry = minority ask
  - Exit = future minority bid
  - No midpoint execution

No production tables are created, updated, deleted, or altered.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "early_dominance_minority_rebound"
ET = ZoneInfo("America/New_York")

CHECKPOINT_SECONDS = (60, 120, 180, 240, 300)
DOMINANCE_THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
SPEED_BUCKETS = (
    ("extreme_rapid", 0.30),
    ("fast", 0.15),
    ("medium", 0.07),
    ("slow", -999.0),
)
TARGET_CENTS = (1, 2, 3, 5, 10)
ENTRY_MODELS = (
    ("immediate_dom80_tp3_120s", 0.80, 3, 120, None),
    ("immediate_dom85_tp3_120s", 0.85, 3, 120, None),
    ("immediate_dom90_tp3_120s", 0.90, 3, 120, None),
    ("dom85_pullback1c_tp3_120s", 0.85, 3, 120, 1),
    ("dom90_pullback1c_tp3_120s", 0.90, 3, 120, 1),
)


QUOTE_TIMELINE_SQL = """
SELECT
  ms.id AS snapshot_id,
  m.id AS market_pk,
  m.market_id AS market_ticker,
  m.opens_at,
  m.closes_at,
  m.target_price AS strike,
  m.status AS market_status,
  JSON_UNQUOTE(JSON_EXTRACT(m.raw_payload, '$.result')) AS settlement_result,
  ms.captured_at,
  ms.btc_price,
  ms.time_remaining_seconds,
  ms.source AS btc_source,
  MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
  MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
  MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
  MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
  MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
  MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
FROM market_snapshots ms
JOIN markets m ON m.id = ms.market_id
JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
JOIN contracts c ON c.id = cs.contract_id
WHERE m.market_id LIKE %s
  AND m.opens_at IS NOT NULL
  AND m.closes_at IS NOT NULL
  AND m.target_price IS NOT NULL
  AND ms.btc_price IS NOT NULL
  AND ms.captured_at >= COALESCE(%s, ms.captured_at)
  AND ms.captured_at < COALESCE(%s, ms.captured_at + INTERVAL 1 SECOND)
GROUP BY
  ms.id, m.id, m.market_id, m.opens_at, m.closes_at, m.target_price,
  m.status, JSON_UNQUOTE(JSON_EXTRACT(m.raw_payload, '$.result')),
  ms.captured_at, ms.btc_price, ms.time_remaining_seconds, ms.source
ORDER BY m.id, ms.captured_at
"""


@dataclass(frozen=True)
class Paths:
    directory: Path
    events_csv: Path
    dominance_matrix_csv: Path
    threshold_summary_csv: Path
    speed_summary_csv: Path
    deceleration_summary_csv: Path
    eventual_loser_csv: Path
    strategy_model_csv: Path
    dedupe_clean_csv: Path
    data_quality_csv: Path
    markdown_report: Path


def _f(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return float(value)


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    raise TypeError(f"unsupported datetime value: {value!r}")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(sep=" ")


def _pct(num: float, den: float) -> float | None:
    if den == 0:
        return None
    return round(100.0 * num / den, 1)


def _avg(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _median(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    return round(statistics.median(vals), 4)


def _percentile(values: Iterable[float | None], pct: float) -> float | None:
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 4)
    pos = pct * (len(vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return round(vals[lo], 4)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo), 4)


def _profit_factor(values: Iterable[float | None]) -> float | str | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    if losses == 0:
        return "inf" if gains > 0 else None
    return round(gains / losses, 4)


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 4)


def _price(row: dict[str, Any], side: str, kind: str) -> float | None:
    return row.get(f"{side.lower()}_{kind}")


def _dominant_side(row: dict[str, Any]) -> str | None:
    yes = row.get("yes_ask")
    no = row.get("no_ask")
    if yes is None or no is None:
        return None
    if yes > no:
        return "YES"
    if no > yes:
        return "NO"
    return None


def _minority_side(side: str) -> str:
    return "NO" if side == "YES" else "YES"


def _winner(result: str | None) -> str | None:
    if result is None:
        return None
    result = str(result).strip().lower()
    if result == "yes":
        return "YES"
    if result == "no":
        return "NO"
    return None


def _side_distance(btc_price: float, strike: float, side: str) -> float:
    return btc_price - strike if side == "YES" else strike - btc_price


def _last_at_or_before(rows: list[dict[str, Any]], ts: datetime) -> dict[str, Any] | None:
    best = None
    for row in rows:
        if row["captured_at"] <= ts:
            best = row
        else:
            break
    return best


def _first_time(rows: list[dict[str, Any]], predicate) -> datetime | None:
    for row in rows:
        if predicate(row):
            return row["captured_at"]
    return None


def _select_checkpoint_row(rows: list[dict[str, Any]], checkpoint_seconds: int, tolerance_seconds: int) -> dict[str, Any] | None:
    choices = []
    for row in rows:
        elapsed = (row["captured_at"] - row["opens_at"]).total_seconds()
        if elapsed < 0:
            continue
        diff = abs(elapsed - checkpoint_seconds)
        if diff <= tolerance_seconds:
            choices.append((diff, row["captured_at"], row))
    if not choices:
        return None
    choices.sort(key=lambda item: (item[0], item[1]))
    return choices[0][2]


def _time_to_threshold(rows: list[dict[str, Any]], side: str, threshold: float, start_ts: datetime, end_ts: datetime) -> datetime | None:
    return _first_time(
        [row for row in rows if start_ts <= row["captured_at"] <= end_ts],
        lambda row: (_price(row, side, "ask") or -1) >= threshold,
    )


def _future_path(rows: list[dict[str, Any]], entry_ts: datetime, side: str, close_ts: datetime) -> list[dict[str, Any]]:
    path = []
    for row in rows:
        if entry_ts <= row["captured_at"] <= close_ts:
            bid = _price(row, side, "bid")
            ask = _price(row, side, "ask")
            if bid is not None and ask is not None:
                path.append({"captured_at": row["captured_at"], "bid": bid, "ask": ask, "btc_price": row["btc_price"]})
    return path


def _first_bid_at(path: list[dict[str, Any]], threshold: float, direction: str = "up") -> datetime | None:
    if direction == "up":
        return _first_time(path, lambda row: row["bid"] >= threshold)
    return _first_time(path, lambda row: row["bid"] <= threshold)


def _seconds(start: datetime, end: datetime | None) -> float | None:
    if end is None:
        return None
    return round((end - start).total_seconds(), 3)


def _speed_bucket(cents_per_second: float | None) -> str:
    if cents_per_second is None:
        return "unknown"
    for label, lo in SPEED_BUCKETS:
        if cents_per_second >= lo:
            return label
    return "unknown"


def _dominance_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 0.95:
        return "95c+"
    lo = math.floor(value * 20) / 20.0
    hi = lo + 0.05
    if lo < 0.55:
        return "<55c"
    return f"{int(round(lo * 100))}-{int(round(hi * 100))}c"


def _btc_move(row: dict[str, Any], prev: dict[str, Any] | None, side: str) -> float | None:
    if prev is None:
        return None
    return round(
        _side_distance(row["btc_price"], row["strike"], side)
        - _side_distance(prev["btc_price"], row["strike"], side),
        2,
    )


def _contract_move(row: dict[str, Any], prev: dict[str, Any] | None, side: str) -> float | None:
    if prev is None:
        return None
    now = _price(row, side, "ask")
    old = _price(prev, side, "ask")
    if now is None or old is None:
        return None
    return round((now - old) * 100.0, 4)


def _volatility(rows: list[dict[str, Any]], end_ts: datetime, window_seconds: int) -> float | None:
    window = [row for row in rows if end_ts - timedelta(seconds=window_seconds) <= row["captured_at"] <= end_ts]
    if len(window) < 3:
        return None
    deltas = []
    prev = window[0]
    for row in window[1:]:
        deltas.append(row["btc_price"] - prev["btc_price"])
        prev = row
    if len(deltas) < 2:
        return None
    return round(statistics.pstdev(deltas), 4)


def _mae_before_target(path: list[dict[str, Any]], entry_ask: float, target_at: datetime | None) -> float | None:
    if target_at is None:
        return None
    sub = [row for row in path if row["captured_at"] <= target_at]
    if not sub:
        return None
    return round((min(row["bid"] for row in sub) - entry_ask) * 100.0, 4)


def _time_exit_pnl(path: list[dict[str, Any]], entry_ask: float, entry_ts: datetime, seconds: int) -> float | None:
    if not path:
        return None
    cutoff = entry_ts + timedelta(seconds=seconds)
    exit_row = None
    for row in path:
        if row["captured_at"] <= cutoff:
            exit_row = row
        else:
            break
    if exit_row is None:
        exit_row = path[-1]
    return round((exit_row["bid"] - entry_ask) * 100.0, 4)


def _score_event(
    rows: list[dict[str, Any]],
    row: dict[str, Any],
    event_type: str,
    threshold: float | None,
    checkpoint_seconds: int | None,
    threshold_times: dict[tuple[str, float], datetime | None],
    fee_slippage_cents: float,
) -> dict[str, Any] | None:
    dom_side = _dominant_side(row)
    if dom_side is None:
        return None
    min_side = _minority_side(dom_side)
    dom_ask = _price(row, dom_side, "ask")
    dom_bid = _price(row, dom_side, "bid")
    min_ask = _price(row, min_side, "ask")
    min_bid = _price(row, min_side, "bid")
    min_spread = _price(row, min_side, "spread")
    if dom_ask is None or min_ask is None or min_bid is None:
        return None
    if min_ask < 0 or min_ask > 1 or min_bid < 0 or min_bid > 1:
        return None

    entry_ts = row["captured_at"]
    elapsed = (entry_ts - row["opens_at"]).total_seconds()
    if elapsed < 0 or elapsed > 300:
        return None

    open_row = _last_at_or_before(rows, row["opens_at"] + timedelta(seconds=20)) or rows[0]
    prev = {
        10: _last_at_or_before(rows, entry_ts - timedelta(seconds=10)),
        30: _last_at_or_before(rows, entry_ts - timedelta(seconds=30)),
        60: _last_at_or_before(rows, entry_ts - timedelta(seconds=60)),
    }
    prior_rows = [r for r in rows if r["captured_at"] <= entry_ts]
    min_prior_asks = [_price(r, min_side, "ask") for r in prior_rows if _price(r, min_side, "ask") is not None]
    dom_prior_asks = [_price(r, dom_side, "ask") for r in prior_rows if _price(r, dom_side, "ask") is not None]

    path = _future_path(rows, entry_ts, min_side, row["closes_at"])
    if not path:
        return None

    target_at = {
        cents: _first_bid_at(path, min_ask + cents / 100.0)
        for cents in TARGET_CENTS
    }
    future_bids = [p["bid"] for p in path]
    max_by_window = {}
    for seconds in (10, 30, 60, 120):
        sub = [p["bid"] for p in path if p["captured_at"] <= entry_ts + timedelta(seconds=seconds)]
        max_by_window[seconds] = max(sub) if sub else None

    seconds_to_open = max(1.0, elapsed)
    dom_open_ask = _price(open_row, dom_side, "ask")
    min_open_ask = _price(open_row, min_side, "ask")
    dom_increase = round((dom_ask - dom_open_ask) * 100.0, 4) if dom_open_ask is not None else None
    dom_cents_per_second = round(dom_increase / seconds_to_open, 6) if dom_increase is not None else None
    min_decline = round((min_open_ask - min_ask) * 100.0, 4) if min_open_ask is not None else None

    et = entry_ts.astimezone(ET)
    winner = _winner(row.get("settlement_result"))
    settlement_group = (
        "unknown_settlement"
        if winner is None
        else "minority_winner"
        if winner == min_side
        else "minority_loser"
    )

    row_out = {
        "event_type": event_type,
        "threshold": threshold,
        "checkpoint_seconds": checkpoint_seconds,
        "market_ticker": row["market_ticker"],
        "observed_at": _iso(entry_ts),
        "observed_at_et": _iso(et),
        "entry_date_et": et.date().isoformat(),
        "hour_et": f"{et.hour:02d}:00 ET",
        "day_of_week_et": et.strftime("%a"),
        "elapsed_seconds": round(elapsed, 3),
        "time_remaining_seconds": row.get("time_remaining_seconds"),
        "strike": row["strike"],
        "btc_price": row["btc_price"],
        "dominant_side": dom_side,
        "minority_side": min_side,
        "settlement_winner": winner,
        "settlement_group": settlement_group,
        "dominant_bid": dom_bid,
        "dominant_ask": dom_ask,
        "dominant_bucket": _dominance_bucket(dom_ask),
        "minority_bid": min_bid,
        "minority_ask": min_ask,
        "minority_spread": min_spread,
        "minority_recent_high_ask": max(min_prior_asks) if min_prior_asks else None,
        "minority_recent_low_ask": min(min_prior_asks) if min_prior_asks else None,
        "minority_decline_from_open_cents": min_decline,
        "minority_decline_from_recent_high_cents": round((max(min_prior_asks) - min_ask) * 100.0, 4) if min_prior_asks else None,
        "dominant_open_ask": dom_open_ask,
        "dominant_price_increase_from_open_cents": dom_increase,
        "dominant_cents_per_second": dom_cents_per_second,
        "dominance_speed_bucket": _speed_bucket(dom_cents_per_second),
        "seconds_60_to_70": _threshold_delta(threshold_times, dom_side, 0.60, 0.70),
        "seconds_70_to_80": _threshold_delta(threshold_times, dom_side, 0.70, 0.80),
        "seconds_80_to_85": _threshold_delta(threshold_times, dom_side, 0.80, 0.85),
        "seconds_80_to_90": _threshold_delta(threshold_times, dom_side, 0.80, 0.90),
        "dominant_change_prev_10s_cents": _contract_move(row, prev[10], dom_side),
        "dominant_change_prev_30s_cents": _contract_move(row, prev[30], dom_side),
        "dominant_change_prev_60s_cents": _contract_move(row, prev[60], dom_side),
        "minority_change_prev_10s_cents": _contract_move(row, prev[10], min_side),
        "minority_change_prev_30s_cents": _contract_move(row, prev[30], min_side),
        "minority_change_prev_60s_cents": _contract_move(row, prev[60], min_side),
        "btc_move_since_open_dominant_side": _btc_move(row, open_row, dom_side),
        "btc_move_prev_10s_dominant_side": _btc_move(row, prev[10], dom_side),
        "btc_move_prev_30s_dominant_side": _btc_move(row, prev[30], dom_side),
        "btc_move_prev_60s_dominant_side": _btc_move(row, prev[60], dom_side),
        "btc_distance_dominant_side": round(_side_distance(row["btc_price"], row["strike"], dom_side), 2),
        "abs_btc_distance": round(abs(row["btc_price"] - row["strike"]), 2),
        "btc_volatility_60s": _volatility(rows, entry_ts, 60),
        "future_max_bid_10s": max_by_window[10],
        "future_max_bid_30s": max_by_window[30],
        "future_max_bid_60s": max_by_window[60],
        "future_max_bid_120s": max_by_window[120],
        "future_max_bid_to_expiry": max(future_bids),
        "future_min_bid_to_expiry": min(future_bids),
        "future_final_bid": future_bids[-1],
        "mfe_cents": round((max(future_bids) - min_ask) * 100.0, 4),
        "mae_cents": round((min(future_bids) - min_ask) * 100.0, 4),
        "gross_time_exit_30s_cents": _time_exit_pnl(path, min_ask, entry_ts, 30),
        "gross_time_exit_60s_cents": _time_exit_pnl(path, min_ask, entry_ts, 60),
        "gross_time_exit_120s_cents": _time_exit_pnl(path, min_ask, entry_ts, 120),
        "clean_quote": int(min_spread is not None and 0 < min_spread <= 0.02 and min_bid <= min_ask and dom_bid is not None and dom_bid <= dom_ask),
    }

    for cents in TARGET_CENTS:
        ts = target_at[cents]
        row_out[f"target_{cents}c_at"] = _iso(ts)
        row_out[f"seconds_to_{cents}c"] = _seconds(entry_ts, ts)
        row_out[f"mae_before_{cents}c_cents"] = _mae_before_target(path, min_ask, ts)
        row_out[f"drawdown_before_{cents}c_cents"] = (
            round(max(0.0, -row_out[f"mae_before_{cents}c_cents"]), 4)
            if row_out[f"mae_before_{cents}c_cents"] is not None
            else None
        )

    btc_move = row_out["btc_move_since_open_dominant_side"]
    row_out["dominant_reaction_cents_per_10_btc"] = None
    row_out["minority_reaction_cents_per_10_btc"] = None
    if btc_move is not None and abs(btc_move) >= 1:
        if dom_increase is not None:
            row_out["dominant_reaction_cents_per_10_btc"] = round(dom_increase / (abs(btc_move) / 10.0), 4)
        if min_decline is not None:
            row_out["minority_reaction_cents_per_10_btc"] = round(min_decline / (abs(btc_move) / 10.0), 4)

    if row_out["btc_move_prev_10s_dominant_side"] is not None and row_out["btc_move_prev_30s_dominant_side"] is not None:
        row_out["btc_accel_10_vs_30"] = round(
            row_out["btc_move_prev_10s_dominant_side"] - row_out["btc_move_prev_30s_dominant_side"] / 3.0,
            4,
        )
    else:
        row_out["btc_accel_10_vs_30"] = None
    row_out["estimated_fee_slippage_cents"] = fee_slippage_cents
    return row_out


def _threshold_delta(threshold_times: dict[tuple[str, float], datetime | None], side: str, start: float, end: float) -> float | None:
    start_ts = threshold_times.get((side, start))
    end_ts = threshold_times.get((side, end))
    if start_ts is None or end_ts is None or end_ts < start_ts:
        return None
    return round((end_ts - start_ts).total_seconds(), 3)


def _group(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    out: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[tuple(row.get(k) for k in keys)].append(row)
    return out


def _base_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    return {
        "events": n,
        "unique_markets": len({row["market_ticker"] for row in rows}),
        "active_days": len({row["entry_date_et"] for row in rows}),
        "clean_quotes": sum(row.get("clean_quote") == 1 for row in rows),
        "minority_settlement_wins": sum(row.get("settlement_group") == "minority_winner" for row in rows),
        "minority_settlement_loses": sum(row.get("settlement_group") == "minority_loser" for row in rows),
        "minority_settlement_win_rate_pct": _pct(sum(row.get("settlement_group") == "minority_winner" for row in rows), n),
        "avg_dominant_ask": _avg(row.get("dominant_ask") for row in rows),
        "avg_minority_ask": _avg(row.get("minority_ask") for row in rows),
        "avg_minority_spread": _avg(row.get("minority_spread") for row in rows),
        "avg_dominant_cents_per_second": _avg(row.get("dominant_cents_per_second") for row in rows),
        "avg_btc_move_since_open": _avg(row.get("btc_move_since_open_dominant_side") for row in rows),
        "avg_reaction_cents_per_10_btc": _avg(row.get("dominant_reaction_cents_per_10_btc") for row in rows),
        "target_1c_rate": _pct(sum(bool(row.get("target_1c_at")) for row in rows), n),
        "target_2c_rate": _pct(sum(bool(row.get("target_2c_at")) for row in rows), n),
        "target_3c_rate": _pct(sum(bool(row.get("target_3c_at")) for row in rows), n),
        "target_5c_rate": _pct(sum(bool(row.get("target_5c_at")) for row in rows), n),
        "target_10c_rate": _pct(sum(bool(row.get("target_10c_at")) for row in rows), n),
        "median_time_to_3c": _median(row.get("seconds_to_3c") for row in rows),
        "median_time_to_5c": _median(row.get("seconds_to_5c") for row in rows),
        "median_drawdown_before_3c": _median(row.get("drawdown_before_3c_cents") for row in rows if row.get("target_3c_at")),
        "median_drawdown_before_5c": _median(row.get("drawdown_before_5c_cents") for row in rows if row.get("target_5c_at")),
        "p90_drawdown_before_3c": _percentile((row.get("drawdown_before_3c_cents") for row in rows if row.get("target_3c_at")), 0.90),
        "p90_drawdown_before_5c": _percentile((row.get("drawdown_before_5c_cents") for row in rows if row.get("target_5c_at")), 0.90),
        "avg_time_exit_120s_gross_cents": _avg(row.get("gross_time_exit_120s_cents") for row in rows),
        "avg_time_exit_120s_est_net_cents": _avg((row.get("gross_time_exit_120s_cents") or 0) - (row.get("estimated_fee_slippage_cents") or 0) for row in rows),
        "small_sample_flag": int(n < 30 or len({row["entry_date_et"] for row in rows}) < 5),
    }


def _summarize(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for key_values, group in _group(rows, keys).items():
        row = {key: value for key, value in zip(keys, key_values)}
        row.update(_base_summary(group))
        out.append(row)
    return sorted(out, key=lambda row: (-int(row["events"]), tuple(str(row.get(k)) for k in keys)))


def _deceleration_label(row: dict[str, Any]) -> str:
    accel = row.get("btc_accel_10_vs_30")
    move10 = row.get("btc_move_prev_10s_dominant_side")
    if accel is None or move10 is None:
        return "unknown"
    if move10 < -5:
        return "partial_reversal"
    if abs(move10) <= 2:
        return "flat"
    if accel < -5:
        return "moving_but_decelerating"
    if accel > 5:
        return "accelerating"
    return "steady"


def _model_entries(
    rows_by_market: dict[int, list[dict[str, Any]]],
    fee_slippage_cents: float,
) -> list[dict[str, Any]]:
    trades = []
    for market_rows in rows_by_market.values():
        if not market_rows:
            continue
        threshold_times = _threshold_times(market_rows)
        for model_name, threshold, target_cents, max_hold_seconds, pullback_cents in ENTRY_MODELS:
            start_row = _first_model_row(market_rows, threshold, pullback_cents)
            if start_row is None:
                continue
            event = _score_event(
                market_rows,
                start_row,
                f"strategy_{model_name}",
                threshold,
                None,
                threshold_times,
                fee_slippage_cents,
            )
            if event is None:
                continue
            entry_ask = event["minority_ask"]
            path = _future_path(market_rows, start_row["captured_at"], event["minority_side"], start_row["closes_at"])
            target_at = _first_bid_at(path, entry_ask + target_cents / 100.0)
            cutoff = start_row["captured_at"] + timedelta(seconds=max_hold_seconds)
            if target_at is not None and target_at <= cutoff:
                gross = float(target_cents)
                outcome = "target"
            else:
                gross = _time_exit_pnl(path, entry_ask, start_row["captured_at"], max_hold_seconds) or 0.0
                outcome = "time_exit"
            trades.append(
                {
                    "model_name": model_name,
                    "market_ticker": event["market_ticker"],
                    "entry_at": event["observed_at"],
                    "dominant_side": event["dominant_side"],
                    "minority_side": event["minority_side"],
                    "dominant_ask": event["dominant_ask"],
                    "minority_ask": entry_ask,
                    "target_cents": target_cents,
                    "max_hold_seconds": max_hold_seconds,
                    "outcome": outcome,
                    "gross_pnl_cents": gross,
                    "estimated_net_pnl_cents": round(gross - fee_slippage_cents, 4),
                }
            )
    return trades


def _first_model_row(rows: list[dict[str, Any]], threshold: float, pullback_cents: int | None) -> dict[str, Any] | None:
    first = None
    dom_side = None
    high = None
    for row in rows:
        elapsed = (row["captured_at"] - row["opens_at"]).total_seconds()
        if elapsed < 0 or elapsed > 300:
            continue
        side = _dominant_side(row)
        if side is None:
            continue
        ask = _price(row, side, "ask")
        if ask is None:
            continue
        if first is None and ask >= threshold:
            first = row
            dom_side = side
            high = ask
            if pullback_cents is None:
                return row
            continue
        if first is not None and side == dom_side:
            high = max(high or ask, ask)
            if ask <= (high or ask) - pullback_cents / 100.0:
                return row
    return None


def _threshold_times(rows: list[dict[str, Any]]) -> dict[tuple[str, float], datetime | None]:
    out: dict[tuple[str, float], datetime | None] = {}
    if not rows:
        return out
    end_ts = rows[0]["opens_at"] + timedelta(seconds=300)
    start_ts = rows[0]["opens_at"]
    for side in ("YES", "NO"):
        for threshold in DOMINANCE_THRESHOLDS:
            out[(side, threshold)] = _time_to_threshold(rows, side, threshold, start_ts, end_ts)
    return out


def _strategy_summary(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for key_values, group in _group(trades, ("model_name",)).items():
        vals = [row["gross_pnl_cents"] for row in group]
        net = [row["estimated_net_pnl_cents"] for row in group]
        wins = sum(v > 0 for v in vals)
        out.append(
            {
                "model_name": key_values[0],
                "trades": len(group),
                "unique_markets": len({row["market_ticker"] for row in group}),
                "wins": wins,
                "losses": len(vals) - wins,
                "win_rate_pct": _pct(wins, len(vals)),
                "avg_win_cents": _avg(v for v in vals if v > 0),
                "avg_loss_cents": _avg(v for v in vals if v <= 0),
                "avg_gross_pnl_cents": _avg(vals),
                "avg_estimated_net_pnl_cents": _avg(net),
                "profit_factor": _profit_factor(vals),
                "max_model_drawdown_cents": _max_drawdown(vals),
            }
        )
    return out


def _dedupe_clean_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    first_event_per_market = {}
    for row in sorted(events, key=lambda item: (item["observed_at"], item["market_ticker"])):
        if row["event_type"] == "first_touch":
            first_event_per_market.setdefault(row["market_ticker"], row)
    cohorts = {
        "all_first_touch_events": [row for row in events if row["event_type"] == "first_touch"],
        "first_extreme_event_per_market": [row for row in events if row["event_type"] == "first_extreme_per_market"],
        "first_touch_clean_quotes": [row for row in events if row["event_type"] == "first_touch" and row.get("clean_quote") == 1],
        "first_event_per_market": list(first_event_per_market.values()),
        "checkpoint_diagnostic": [row for row in events if row["event_type"] == "checkpoint"],
    }
    out = []
    for name, rows in cohorts.items():
        item = {"cohort": name}
        item.update(_base_summary(rows))
        out.append(item)
    return out


def _data_quality(raw_rows: list[dict[str, Any]], events: list[dict[str, Any]], missing_checkpoints: int) -> list[dict[str, Any]]:
    out = []

    def add(issue: str, count: int) -> None:
        if count:
            out.append({"issue": issue, "rows_affected": count})

    add("missing_checkpoint_observations", missing_checkpoints)
    add("event_quote_age_unavailable", len(events))
    add("event_minority_spread_eq_zero", sum(row.get("minority_spread") == 0 for row in events))
    add("event_minority_spread_lt_zero", sum((row.get("minority_spread") or 0) < 0 for row in events))
    add("event_unclean_quotes", sum(row.get("clean_quote") != 1 for row in events))
    add("event_unknown_settlement", sum(row.get("settlement_group") == "unknown_settlement" for row in events))
    add("raw_yes_bid_gt_ask", sum(row.get("yes_bid") is not None and row.get("yes_ask") is not None and row["yes_bid"] > row["yes_ask"] for row in raw_rows))
    add("raw_no_bid_gt_ask", sum(row.get("no_bid") is not None and row.get("no_ask") is not None and row["no_bid"] > row["no_ask"] for row in raw_rows))
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _table(rows: list[dict[str, Any]], cols: list[str], limit: int = 12) -> str:
    if not rows:
        return "_No rows._"
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in cols) + " |")
    return "\n".join(lines)


def _render_report(paths: Paths, outputs: dict[str, list[dict[str, Any]]]) -> str:
    dedupe = outputs["dedupe_clean"]
    first_touch = next((row for row in dedupe if row["cohort"] == "all_first_touch_events"), {})
    matrix = outputs["dominance_matrix"]
    strategies = outputs["strategy_model"]
    best_strategy = sorted(
        strategies,
        key=lambda row: -999999 if row.get("avg_estimated_net_pnl_cents") is None else -float(row["avg_estimated_net_pnl_cents"]),
    )[:1]
    return f"""# Early Dominance Minority Rebound Report

## Scope

- Market: Kalshi BTC 15-minute contracts
- Window: first 5 minutes after market open
- Primary event: first touch of each dominant-price threshold per market
- Entry: minority ask
- Exit: future minority bid

## Direct Answers

1. Minority rebound frequency rises with dominance only if the threshold summary shows higher +3c/+5c rates at higher `dominant_bucket` values.
2. 85c vs 70c and 90c+ vs lower levels are in the threshold summary and dominance matrix.
3. Dominance speed is in the dominance matrix by `dominance_speed_bucket`.
4. Contract repricing relative to BTC is approximated by `dominant_reaction_cents_per_10_btc` and `minority_reaction_cents_per_10_btc`.
5. Pullback confirmation is compared in the strategy model table.
6. BTC deceleration is summarized separately; any rule using it must enter after the deceleration is observable.
7. Eventual losing minority contracts have their own summary.
8. No retrospective result here is a live-trading recommendation.

## Primary First-Touch Summary

{_table([first_touch], ["cohort", "events", "unique_markets", "clean_quotes", "minority_settlement_win_rate_pct", "avg_dominant_ask", "avg_minority_ask", "target_3c_rate", "target_5c_rate", "median_time_to_3c", "median_drawdown_before_3c", "avg_time_exit_120s_est_net_cents"])}

## Dominance Level x Speed Matrix

{_table(matrix, ["dominant_bucket", "dominance_speed_bucket", "events", "unique_markets", "avg_minority_ask", "target_3c_rate", "target_5c_rate", "target_10c_rate", "median_drawdown_before_3c", "median_time_to_3c", "small_sample_flag"], 20)}

## Simple Strategy Models

{_table(strategies, ["model_name", "trades", "unique_markets", "win_rate_pct", "avg_gross_pnl_cents", "avg_estimated_net_pnl_cents", "profit_factor", "max_model_drawdown_cents"])}

## Best Strategy By Estimated Net

{_table(best_strategy, ["model_name", "trades", "unique_markets", "avg_estimated_net_pnl_cents", "profit_factor", "max_model_drawdown_cents"])}

## Dedupe / Clean Quote Impact

{_table(dedupe, ["cohort", "events", "unique_markets", "clean_quotes", "target_3c_rate", "target_5c_rate", "avg_time_exit_120s_est_net_cents", "small_sample_flag"])}

## Output Files

- Events CSV: `{paths.events_csv}`
- Dominance matrix CSV: `{paths.dominance_matrix_csv}`
- Threshold summary CSV: `{paths.threshold_summary_csv}`
- Speed summary CSV: `{paths.speed_summary_csv}`
- Deceleration summary CSV: `{paths.deceleration_summary_csv}`
- Eventual loser CSV: `{paths.eventual_loser_csv}`
- Strategy model CSV: `{paths.strategy_model_csv}`
- Dedupe/clean CSV: `{paths.dedupe_clean_csv}`
- Data quality CSV: `{paths.data_quality_csv}`

## Final Interpretation

This report tests two separate claims:

- Extreme early dominance causes the minority contract to rebound frequently.
- Buying the minority contract after extreme early dominance is profitable.

The first can be true while the second remains false after bid/ask execution, time exits, costs,
deduplication, and clean-quote filtering.
"""


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"early_dominance_minority_rebound_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        events_csv=output_dir / f"{stem}_events.csv",
        dominance_matrix_csv=output_dir / f"{stem}_dominance_matrix.csv",
        threshold_summary_csv=output_dir / f"{stem}_threshold_summary.csv",
        speed_summary_csv=output_dir / f"{stem}_speed_summary.csv",
        deceleration_summary_csv=output_dir / f"{stem}_deceleration_summary.csv",
        eventual_loser_csv=output_dir / f"{stem}_eventual_loser.csv",
        strategy_model_csv=output_dir / f"{stem}_strategy_model.csv",
        dedupe_clean_csv=output_dir / f"{stem}_dedupe_clean.csv",
        data_quality_csv=output_dir / f"{stem}_data_quality.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def _load_rows(market_like: str, start: str | None, end: str | None) -> list[dict[str, Any]]:
    from app.db import get_pool

    conn = get_pool().get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(QUOTE_TIMELINE_SQL, (market_like, start, end))
        rows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()

    out = []
    for row in rows:
        item = dict(row)
        for key in ("strike", "btc_price", "yes_bid", "yes_ask", "yes_spread", "no_bid", "no_ask", "no_spread"):
            item[key] = _f(item.get(key))
        for key in ("opens_at", "closes_at", "captured_at"):
            item[key] = _dt(item.get(key))
        out.append(item)
    return out


def build_report(
    output_dir: Path,
    market_like: str,
    start: str | None,
    end: str | None,
    checkpoint_tolerance_seconds: int,
    fee_slippage_cents: float,
) -> Paths:
    paths = _paths(output_dir)
    raw_rows = _load_rows(market_like, start, end)
    rows_by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        rows_by_market[int(row["market_pk"])].append(row)

    events = []
    missing_checkpoints = 0
    for market_rows in rows_by_market.values():
        market_rows.sort(key=lambda row: row["captured_at"])
        if not market_rows:
            continue
        threshold_times = _threshold_times(market_rows)

        seen_market_extreme = False
        for side in ("YES", "NO"):
            for threshold in DOMINANCE_THRESHOLDS:
                ts = threshold_times.get((side, threshold))
                if ts is None:
                    continue
                row = _last_at_or_before(market_rows, ts)
                if row is None:
                    continue
                scored = _score_event(market_rows, row, "first_touch", threshold, None, threshold_times, fee_slippage_cents)
                if scored is not None:
                    events.append(scored)
                    if threshold >= 0.80 and not seen_market_extreme:
                        extreme = dict(scored)
                        extreme["event_type"] = "first_extreme_per_market"
                        events.append(extreme)
                        seen_market_extreme = True

        for checkpoint in CHECKPOINT_SECONDS:
            row = _select_checkpoint_row(market_rows, checkpoint, checkpoint_tolerance_seconds)
            if row is None:
                missing_checkpoints += 1
                continue
            scored = _score_event(market_rows, row, "checkpoint", None, checkpoint, threshold_times, fee_slippage_cents)
            if scored is not None:
                events.append(scored)

    for event in events:
        event["btc_deceleration_class"] = _deceleration_label(event)

    first_touch = [event for event in events if event["event_type"] == "first_touch"]
    outputs = {
        "dominance_matrix": _summarize(first_touch, ("dominant_bucket", "dominance_speed_bucket")),
        "threshold_summary": _summarize(first_touch, ("threshold", "dominant_bucket")),
        "speed_summary": _summarize(first_touch, ("dominance_speed_bucket",)),
        "deceleration_summary": _summarize(first_touch, ("btc_deceleration_class", "dominant_bucket")),
        "eventual_loser": _summarize([event for event in first_touch if event["settlement_group"] == "minority_loser"], ("dominant_bucket", "dominance_speed_bucket")),
        "strategy_model": _strategy_summary(_model_entries(rows_by_market, fee_slippage_cents)),
        "dedupe_clean": _dedupe_clean_summary(events),
        "data_quality": _data_quality(raw_rows, events, missing_checkpoints),
    }

    _write_csv(paths.events_csv, events)
    _write_csv(paths.dominance_matrix_csv, outputs["dominance_matrix"])
    _write_csv(paths.threshold_summary_csv, outputs["threshold_summary"])
    _write_csv(paths.speed_summary_csv, outputs["speed_summary"])
    _write_csv(paths.deceleration_summary_csv, outputs["deceleration_summary"])
    _write_csv(paths.eventual_loser_csv, outputs["eventual_loser"])
    _write_csv(paths.strategy_model_csv, outputs["strategy_model"])
    _write_csv(paths.dedupe_clean_csv, outputs["dedupe_clean"])
    _write_csv(paths.data_quality_csv, outputs["data_quality"])
    paths.markdown_report.write_text(_render_report(paths, outputs))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Early dominance minority rebound research report")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M-%")
    parser.add_argument("--start", default=None, help="optional UTC start timestamp")
    parser.add_argument("--end", default=None, help="optional UTC end timestamp")
    parser.add_argument("--checkpoint-tolerance-seconds", type=int, default=12)
    parser.add_argument("--fee-slippage-cents", type=float, default=1.0)
    args = parser.parse_args()

    paths = build_report(
        output_dir=args.output_dir,
        market_like=args.market_like,
        start=args.start,
        end=args.end,
        checkpoint_tolerance_seconds=args.checkpoint_tolerance_seconds,
        fee_slippage_cents=args.fee_slippage_cents,
    )
    print("Early dominance minority rebound report complete")
    print(f"events_csv={paths.events_csv}")
    print(f"dominance_matrix_csv={paths.dominance_matrix_csv}")
    print(f"threshold_summary_csv={paths.threshold_summary_csv}")
    print(f"speed_summary_csv={paths.speed_summary_csv}")
    print(f"deceleration_summary_csv={paths.deceleration_summary_csv}")
    print(f"eventual_loser_csv={paths.eventual_loser_csv}")
    print(f"strategy_model_csv={paths.strategy_model_csv}")
    print(f"dedupe_clean_csv={paths.dedupe_clean_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
