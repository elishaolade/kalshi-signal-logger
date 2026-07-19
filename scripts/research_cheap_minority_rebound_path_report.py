#!/usr/bin/env python3
"""
Path-focused cheap/minority rebound research report.

Question:
  Cheap BTC 15m minority contracts often rebound later, but how much adverse
  path do they take before the rebound, and is a fixed -3c stop incompatible
  with their natural movement?

This script uses executable-style prices:
  - Entry = ask at the checkpoint observation
  - Exit = future bid

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
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "cheap_minority_rebound_path"
ET = ZoneInfo("America/New_York")

CHECKPOINT_SECONDS = (180, 240, 300)
TARGET_CENTS = (1, 2, 3, 5, 10)
STOP_CENTS = (1, 2, 3, 4, 5, 7, 10, 15)
MODEL_SPECS = (
    ("tp3_sl3", 3, 3),
    ("tp3_sl5", 3, 5),
    ("tp3_sl10", 3, 10),
    ("tp5_sl5", 5, 5),
    ("tp5_sl10", 5, 10),
)
TIME_EXIT_SECONDS = (30, 60, 120)
ENTRY_BUCKETS = (
    ("0-5c", 0.00, 0.05),
    ("5-10c", 0.05, 0.10),
    ("10-15c", 0.10, 0.15),
    ("15-20c", 0.15, 0.20),
    ("20-25c", 0.20, 0.25),
    ("25-30c", 0.25, 0.30),
    ("30-40c", 0.30, 0.40),
    ("40-50c", 0.40, 0.50),
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
    candidates_csv: Path
    mae_success_csv: Path
    stop_width_csv: Path
    entry_bucket_csv: Path
    time_to_rebound_csv: Path
    exit_model_csv: Path
    feature_compare_csv: Path
    dedupe_csv: Path
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


def _et(value: datetime) -> datetime:
    return value.astimezone(ET)


def _bucket(value: float | None, buckets: Iterable[tuple[str, float, float]], default: str = "out_of_range") -> str:
    if value is None:
        return "unknown"
    for label, lo, hi in buckets:
        if lo <= value < hi:
            return label
    return default


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
    pos = (len(vals) - 1) * pct
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


def _cheap_side(row: dict[str, Any]) -> str | None:
    yes_ask = row.get("yes_ask")
    no_ask = row.get("no_ask")
    if yes_ask is None or no_ask is None:
        return None
    if yes_ask < no_ask:
        return "YES"
    if no_ask < yes_ask:
        return "NO"
    return None


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


def _last_at_or_before(rows: list[dict[str, Any]], target_ts: datetime) -> dict[str, Any] | None:
    best = None
    for row in rows:
        if row["captured_at"] <= target_ts:
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
        diff = abs(elapsed - checkpoint_seconds)
        if diff <= tolerance_seconds:
            choices.append((diff, row["captured_at"], row))
    if not choices:
        return None
    choices.sort(key=lambda item: (item[0], item[1]))
    return choices[0][2]


def _move_toward_side(row: dict[str, Any], prev: dict[str, Any] | None, side: str) -> float | None:
    if prev is None:
        return None
    return round(
        _side_distance(row["btc_price"], row["strike"], side)
        - _side_distance(prev["btc_price"], row["strike"], side),
        2,
    )


def _contract_change(row: dict[str, Any], prev: dict[str, Any] | None, side: str) -> float | None:
    if prev is None:
        return None
    now = _price(row, side, "ask")
    old = _price(prev, side, "ask")
    if now is None or old is None:
        return None
    return round(100.0 * (now - old), 4)


def _future_bid_path(entry_row: dict[str, Any], future_rows: list[dict[str, Any]], side: str) -> list[dict[str, Any]]:
    entry_bid = _price(entry_row, side, "bid")
    path = [{"captured_at": entry_row["captured_at"], "bid": entry_bid, "btc_price": entry_row["btc_price"]}]
    for row in future_rows:
        bid = _price(row, side, "bid")
        if bid is not None:
            path.append({"captured_at": row["captured_at"], "bid": bid, "btc_price": row["btc_price"]})
    return [row for row in path if row["bid"] is not None]


def _first_bid_event(path: list[dict[str, Any]], threshold: float, direction: str) -> datetime | None:
    if direction == "up":
        return _first_time(path, lambda row: row["bid"] >= threshold)
    return _first_time(path, lambda row: row["bid"] <= threshold)


def _path_until(path: list[dict[str, Any]], end_ts: datetime | None) -> list[dict[str, Any]]:
    if end_ts is None:
        return []
    return [row for row in path if row["captured_at"] <= end_ts]


def _seconds_between(start: datetime, end: datetime | None) -> float | None:
    if end is None:
        return None
    return round((end - start).total_seconds(), 3)


def _model_target_stop(
    entry_ask: float,
    path: list[dict[str, Any]],
    target_cents: int,
    stop_cents: int,
    fee_slippage_cents: float,
) -> tuple[str, float, float, bool]:
    target_at = _first_bid_event(path, entry_ask + target_cents / 100.0, "up")
    stop_at = _first_bid_event(path, entry_ask - stop_cents / 100.0, "down")
    if target_at is not None and (stop_at is None or target_at < stop_at):
        gross = float(target_cents)
        return "target_first", gross, round(gross - fee_slippage_cents, 4), False
    if stop_at is not None and (target_at is None or stop_at < target_at):
        gross = -float(stop_cents)
        later_rebound = target_at is not None and target_at > stop_at
        return "stop_first", gross, round(gross - fee_slippage_cents, 4), later_rebound
    if not path:
        return "no_path", 0.0, round(-fee_slippage_cents, 4), False
    gross = round(100.0 * (path[-1]["bid"] - entry_ask), 4)
    return "neither", gross, round(gross - fee_slippage_cents, 4), False


def _model_time_exit(entry_ask: float, path: list[dict[str, Any]], entry_ts: datetime, seconds: int, fee_slippage_cents: float) -> tuple[float, float]:
    cutoff = entry_ts + timedelta(seconds=seconds)
    exit_row = None
    for row in path:
        if row["captured_at"] <= cutoff:
            exit_row = row
        else:
            break
    if exit_row is None:
        exit_row = path[-1] if path else None
    if exit_row is None:
        return 0.0, round(-fee_slippage_cents, 4)
    gross = round(100.0 * (exit_row["bid"] - entry_ask), 4)
    return gross, round(gross - fee_slippage_cents, 4)


def _score_candidate(
    market_rows: list[dict[str, Any]],
    entry_row: dict[str, Any],
    checkpoint_seconds: int,
    fee_slippage_cents: float,
) -> dict[str, Any] | None:
    side = _cheap_side(entry_row)
    if side is None:
        return None
    entry_ask = _price(entry_row, side, "ask")
    entry_bid = _price(entry_row, side, "bid")
    entry_spread = _price(entry_row, side, "spread")
    if entry_ask is None or entry_bid is None or entry_ask < 0 or entry_ask >= 0.50:
        return None

    entry_ts = entry_row["captured_at"]
    future_rows = [
        row for row in market_rows
        if entry_ts < row["captured_at"] <= entry_row["closes_at"]
    ]
    path = _future_bid_path(entry_row, future_rows, side)
    if len(path) < 2:
        return None

    prev = {
        10: _last_at_or_before(market_rows, entry_ts - timedelta(seconds=10)),
        30: _last_at_or_before(market_rows, entry_ts - timedelta(seconds=30)),
        60: _last_at_or_before(market_rows, entry_ts - timedelta(seconds=60)),
    }
    prev_120 = _last_at_or_before(market_rows, entry_ts - timedelta(seconds=120))
    prior_rows = [row for row in market_rows if row["captured_at"] <= entry_ts]
    prior_side_asks = [_price(row, side, "ask") for row in prior_rows if _price(row, side, "ask") is not None]

    target_at = {
        cents: _first_bid_event(path, entry_ask + cents / 100.0, "up")
        for cents in TARGET_CENTS
    }
    stop_at = {
        cents: _first_bid_event(path, entry_ask - cents / 100.0, "down")
        for cents in STOP_CENTS
    }

    mae_before = {}
    drawdown_before = {}
    for target in (3, 5):
        sub_path = _path_until(path, target_at[target])
        if sub_path:
            min_bid = min(row["bid"] for row in sub_path)
            mae = round(100.0 * (min_bid - entry_ask), 4)
            mae_before[target] = mae
            drawdown_before[target] = round(max(0.0, -mae), 4)
        else:
            mae_before[target] = None
            drawdown_before[target] = None

    model_results: dict[str, Any] = {}
    for name, target, stop in MODEL_SPECS:
        result, gross, net, later_rebound = _model_target_stop(
            entry_ask,
            path,
            target,
            stop,
            fee_slippage_cents,
        )
        model_results[f"{name}_result"] = result
        model_results[f"{name}_gross_pnl_cents"] = gross
        model_results[f"{name}_estimated_net_pnl_cents"] = net
        model_results[f"{name}_stopped_then_later_target"] = int(later_rebound)

    time_exit_results: dict[str, Any] = {}
    for seconds in TIME_EXIT_SECONDS:
        gross, net = _model_time_exit(entry_ask, path, entry_ts, seconds, fee_slippage_cents)
        time_exit_results[f"time_exit_{seconds}s_gross_pnl_cents"] = gross
        time_exit_results[f"time_exit_{seconds}s_estimated_net_pnl_cents"] = net

    btc_entry_distance = _side_distance(entry_row["btc_price"], entry_row["strike"], side)
    btc_against_40_at = _first_time(
        future_rows,
        lambda row: _side_distance(row["btc_price"], entry_row["strike"], side) <= btc_entry_distance - 40,
    )
    btc_cross_against_at = _first_time(
        future_rows,
        lambda row: _side_distance(row["btc_price"], entry_row["strike"], side) <= 0,
    )

    all_bids = [row["bid"] for row in path]
    et = _et(entry_ts)
    settlement_winner = _winner(entry_row.get("settlement_result"))
    settlement_group = (
        "unknown_settlement"
        if settlement_winner is None
        else "ultimate_winner"
        if settlement_winner == side
        else "ultimate_loser"
    )
    contract_decline_from_high = None
    if prior_side_asks:
        contract_decline_from_high = round(100.0 * (max(prior_side_asks) - entry_ask), 4)

    row = {
        "market_ticker": entry_row["market_ticker"],
        "checkpoint_seconds": checkpoint_seconds,
        "observed_at": _iso(entry_ts),
        "observed_at_et": _iso(et),
        "entry_date_et": et.date().isoformat(),
        "hour_et": f"{et.hour:02d}:00 ET",
        "day_of_week_et": et.strftime("%a"),
        "side": side,
        "settlement_winner": settlement_winner,
        "settlement_group": settlement_group,
        "entry_bucket": _bucket(entry_ask, ENTRY_BUCKETS),
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "entry_spread": entry_spread,
        "strike": entry_row["strike"],
        "btc_price": entry_row["btc_price"],
        "btc_entry_distance_to_minority_side": round(btc_entry_distance, 2),
        "time_remaining_seconds": entry_row.get("time_remaining_seconds"),
        "btc_move_prev_10s_toward_minority": _move_toward_side(entry_row, prev[10], side),
        "btc_move_prev_30s_toward_minority": _move_toward_side(entry_row, prev[30], side),
        "btc_move_prev_60s_toward_minority": _move_toward_side(entry_row, prev[60], side),
        "btc_move_prev_120s_toward_minority": _move_toward_side(entry_row, prev_120, side),
        "btc_accel_10_vs_30": None,
        "contract_change_prev_10s_cents": _contract_change(entry_row, prev[10], side),
        "contract_change_prev_30s_cents": _contract_change(entry_row, prev[30], side),
        "contract_change_prev_60s_cents": _contract_change(entry_row, prev[60], side),
        "contract_decline_from_recent_high_cents": contract_decline_from_high,
        "minority_prior_high_ask": max(prior_side_asks) if prior_side_asks else None,
        "minority_prior_low_ask": min(prior_side_asks) if prior_side_asks else None,
        "dominant_bid": _price(entry_row, "NO" if side == "YES" else "YES", "bid"),
        "dominant_ask": _price(entry_row, "NO" if side == "YES" else "YES", "ask"),
        "future_min_bid": min(all_bids),
        "future_max_bid": max(all_bids),
        "future_final_bid": all_bids[-1],
        "mfe_cents": round(100.0 * (max(all_bids) - entry_ask), 4),
        "mae_cents": round(100.0 * (min(all_bids) - entry_ask), 4),
        "target_1c_at": _iso(target_at[1]),
        "target_2c_at": _iso(target_at[2]),
        "target_3c_at": _iso(target_at[3]),
        "target_5c_at": _iso(target_at[5]),
        "target_10c_at": _iso(target_at[10]),
        "seconds_to_1c": _seconds_between(entry_ts, target_at[1]),
        "seconds_to_2c": _seconds_between(entry_ts, target_at[2]),
        "seconds_to_3c": _seconds_between(entry_ts, target_at[3]),
        "seconds_to_5c": _seconds_between(entry_ts, target_at[5]),
        "mae_before_3c_target_cents": mae_before[3],
        "drawdown_before_3c_target_cents": drawdown_before[3],
        "mae_before_5c_target_cents": mae_before[5],
        "drawdown_before_5c_target_cents": drawdown_before[5],
        "btc_against_40_at": _iso(btc_against_40_at),
        "btc_cross_against_at": _iso(btc_cross_against_at),
        "clean_quote": int(
            entry_spread is not None
            and entry_spread > 0
            and entry_spread <= 0.02
            and entry_bid <= entry_ask
        ),
    }

    if row["btc_move_prev_10s_toward_minority"] is not None and row["btc_move_prev_30s_toward_minority"] is not None:
        row["btc_accel_10_vs_30"] = round(row["btc_move_prev_10s_toward_minority"] - row["btc_move_prev_30s_toward_minority"] / 3.0, 4)

    prev60_move = row["btc_move_prev_60s_toward_minority"]
    contract60 = row["contract_change_prev_60s_cents"]
    row["contract_reaction_cents_per_10_btc"] = None
    if prev60_move is not None and abs(prev60_move) >= 1 and contract60 is not None:
        row["contract_reaction_cents_per_10_btc"] = round(contract60 / (abs(prev60_move) / 10.0), 4)

    for cents, ts in stop_at.items():
        row[f"stop_{cents}c_at"] = _iso(ts)
    for target in (3, 5):
        for stop in STOP_CENTS:
            row[f"target_{target}c_before_stop_{stop}c"] = int(
                target_at[target] is not None
                and (stop_at[stop] is None or target_at[target] < stop_at[stop])
            )

    row.update(model_results)
    row.update(time_exit_results)
    return row


def _group(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row.get(k) for k in keys)].append(row)
    return grouped


def _base_summary(group: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(group)
    active_days = len({row.get("entry_date_et") for row in group if row.get("entry_date_et")})
    return {
        "signals": n,
        "unique_markets": len({row["market_ticker"] for row in group}),
        "active_days": active_days,
        "clean_quotes": sum(row.get("clean_quote") == 1 for row in group),
        "ultimate_winners": sum(row.get("settlement_group") == "ultimate_winner" for row in group),
        "ultimate_losers": sum(row.get("settlement_group") == "ultimate_loser" for row in group),
        "unknown_settlement": sum(row.get("settlement_group") == "unknown_settlement" for row in group),
        "ultimate_win_rate_pct": _pct(sum(row.get("settlement_group") == "ultimate_winner" for row in group), n),
        "avg_entry_ask": _avg(row.get("entry_ask") for row in group),
        "avg_entry_spread": _avg(row.get("entry_spread") for row in group),
        "avg_mfe_cents": _avg(row.get("mfe_cents") for row in group),
        "avg_mae_cents": _avg(row.get("mae_cents") for row in group),
        "target_3c_hit_rate": _pct(sum(bool(row.get("target_3c_at")) for row in group), n),
        "target_5c_hit_rate": _pct(sum(bool(row.get("target_5c_at")) for row in group), n),
        "target_3c_before_stop_3c_rate": _pct(sum(row.get("target_3c_before_stop_3c") == 1 for row in group), n),
        "target_3c_before_stop_5c_rate": _pct(sum(row.get("target_3c_before_stop_5c") == 1 for row in group), n),
        "target_3c_before_stop_10c_rate": _pct(sum(row.get("target_3c_before_stop_10c") == 1 for row in group), n),
        "target_5c_before_stop_5c_rate": _pct(sum(row.get("target_5c_before_stop_5c") == 1 for row in group), n),
        "median_drawdown_before_3c_success": _median(row.get("drawdown_before_3c_target_cents") for row in group if row.get("target_3c_at")),
        "median_drawdown_before_5c_success": _median(row.get("drawdown_before_5c_target_cents") for row in group if row.get("target_5c_at")),
        "small_sample_flag": int(n < 30 or active_days < 5),
    }


def _summarize_by(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for key_values, group in _group(rows, keys).items():
        row = {key: value for key, value in zip(keys, key_values)}
        row.update(_base_summary(group))
        out.append(row)
    return sorted(out, key=lambda row: (-int(row["signals"]), tuple(str(row.get(key)) for key in keys)))


def _mae_success_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for target in (3, 5):
        successful = [row for row in rows if row.get(f"target_{target}c_at")]
        drawdowns = [row.get(f"drawdown_before_{target}c_target_cents") for row in successful]
        out.append(
            {
                "target": f"+{target}c",
                "successful_rebounds": len(successful),
                "avg_drawdown_before_target_cents": _avg(drawdowns),
                "median_drawdown_before_target_cents": _median(drawdowns),
                "p25_drawdown_before_target_cents": _percentile(drawdowns, 0.25),
                "p75_drawdown_before_target_cents": _percentile(drawdowns, 0.75),
                "p90_drawdown_before_target_cents": _percentile(drawdowns, 0.90),
                "max_drawdown_before_target_cents": max([v for v in drawdowns if v is not None], default=None),
            }
        )
    return out


def _stop_width_summary(rows: list[dict[str, Any]], fee_slippage_cents: float) -> list[dict[str, Any]]:
    out = []
    for target in (3, 5):
        for stop in STOP_CENTS:
            model = []
            for row in rows:
                if row.get(f"target_{target}c_before_stop_{stop}c") == 1:
                    model.append(float(target))
                elif row.get(f"stop_{stop}c_at"):
                    model.append(-float(stop))
                elif row.get("future_final_bid") is not None and row.get("entry_ask") is not None:
                    model.append(round(100.0 * (row["future_final_bid"] - row["entry_ask"]), 4))
            stopped_then_later = sum(
                bool(row.get(f"stop_{stop}c_at"))
                and bool(row.get(f"target_{target}c_at"))
                and row[f"stop_{stop}c_at"] < row[f"target_{target}c_at"]
                for row in rows
            )
            wins = sum(value > 0 for value in model)
            losses = sum(value <= 0 for value in model)
            out.append(
                {
                    "target": f"+{target}c",
                    "stop": f"-{stop}c",
                    "trades": len(model),
                    "target_first": sum(value == float(target) for value in model),
                    "stop_first": sum(value == -float(stop) for value in model),
                    "neither": len(model) - sum(value == float(target) for value in model) - sum(value == -float(stop) for value in model),
                    "survival_to_target_pct": _pct(sum(value == float(target) for value in model), len(model)),
                    "wins": wins,
                    "losses": losses,
                    "win_rate_pct": _pct(wins, len(model)),
                    "avg_gross_pnl_cents": _avg(model),
                    "avg_estimated_net_pnl_cents": _avg([value - fee_slippage_cents for value in model]),
                    "profit_factor_gross": _profit_factor(model),
                    "max_model_drawdown_cents": _max_drawdown(model),
                    "stopped_then_later_rebounded": stopped_then_later,
                    "stopped_then_later_rebounded_pct": _pct(stopped_then_later, len(model)),
                }
            )
    for target in (3, 5):
        successful = sum(bool(row.get(f"target_{target}c_at")) for row in rows)
        out.append(
            {
                "target": f"+{target}c",
                "stop": "none",
                "trades": len(rows),
                "target_first": successful,
                "stop_first": 0,
                "neither": len(rows) - successful,
                "survival_to_target_pct": _pct(successful, len(rows)),
                "wins": successful,
                "losses": len(rows) - successful,
                "win_rate_pct": _pct(successful, len(rows)),
                "avg_gross_pnl_cents": None,
                "avg_estimated_net_pnl_cents": None,
                "profit_factor_gross": None,
                "max_model_drawdown_cents": None,
                "stopped_then_later_rebounded": 0,
                "stopped_then_later_rebounded_pct": 0.0,
            }
        )
    return out


def _time_to_rebound_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for key_values, group in _group(rows, ("checkpoint_seconds", "entry_bucket")).items():
        for target in (1, 2, 3, 5):
            values = [row.get(f"seconds_to_{target}c") for row in group if row.get(f"seconds_to_{target}c") is not None]
            out.append(
                {
                    "checkpoint_seconds": key_values[0],
                    "entry_bucket": key_values[1],
                    "target": f"+{target}c",
                    "successful_rebounds": len(values),
                    "avg_seconds_to_target": _avg(values),
                    "median_seconds_to_target": _median(values),
                    "p75_seconds_to_target": _percentile(values, 0.75),
                    "p90_seconds_to_target": _percentile(values, 0.90),
                }
            )
    return sorted(out, key=lambda row: (str(row["checkpoint_seconds"]), str(row["entry_bucket"]), str(row["target"])))


def _exit_model_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for name, _, _ in MODEL_SPECS:
        gross_col = f"{name}_gross_pnl_cents"
        net_col = f"{name}_estimated_net_pnl_cents"
        values = [row.get(gross_col) for row in rows if row.get(gross_col) is not None]
        wins = sum(value > 0 for value in values)
        losses = sum(value <= 0 for value in values)
        out.append(
            {
                "exit_model": name,
                "trades": len(values),
                "wins": wins,
                "losses": losses,
                "win_rate_pct": _pct(wins, len(values)),
                "avg_win_cents": _avg([value for value in values if value > 0]),
                "avg_loss_cents": _avg([value for value in values if value <= 0]),
                "avg_gross_pnl_cents": _avg(values),
                "avg_estimated_net_pnl_cents": _avg(row.get(net_col) for row in rows),
                "profit_factor": _profit_factor(values),
                "max_model_drawdown_cents": _max_drawdown(values),
                "stopped_then_later_rebounded_pct": _pct(sum(row.get(f"{name}_stopped_then_later_target") == 1 for row in rows), len(rows)),
            }
        )
    for seconds in TIME_EXIT_SECONDS:
        gross_col = f"time_exit_{seconds}s_gross_pnl_cents"
        net_col = f"time_exit_{seconds}s_estimated_net_pnl_cents"
        values = [row.get(gross_col) for row in rows if row.get(gross_col) is not None]
        wins = sum(value > 0 for value in values)
        losses = sum(value <= 0 for value in values)
        out.append(
            {
                "exit_model": f"time_exit_{seconds}s",
                "trades": len(values),
                "wins": wins,
                "losses": losses,
                "win_rate_pct": _pct(wins, len(values)),
                "avg_win_cents": _avg([value for value in values if value > 0]),
                "avg_loss_cents": _avg([value for value in values if value <= 0]),
                "avg_gross_pnl_cents": _avg(values),
                "avg_estimated_net_pnl_cents": _avg(row.get(net_col) for row in rows),
                "profit_factor": _profit_factor(values),
                "max_model_drawdown_cents": _max_drawdown(values),
                "stopped_then_later_rebounded_pct": None,
            }
        )
    return out


def _feature_compare_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    classes = {
        "clean_3c_before_3c_adverse": [
            row for row in rows
            if row.get("target_3c_before_stop_3c") == 1
        ],
        "hit_3c_after_3c_adverse": [
            row for row in rows
            if row.get("target_3c_at") and row.get("target_3c_before_stop_3c") != 1
        ],
        "never_hit_3c": [
            row for row in rows
            if not row.get("target_3c_at")
        ],
        "eventual_loser_only_hit_3c": [
            row for row in rows
            if row.get("settlement_group") == "ultimate_loser" and row.get("target_3c_at")
        ],
        "eventual_loser_only_no_3c": [
            row for row in rows
            if row.get("settlement_group") == "ultimate_loser" and not row.get("target_3c_at")
        ],
    }
    fields = (
        "entry_ask",
        "entry_spread",
        "btc_entry_distance_to_minority_side",
        "btc_move_prev_10s_toward_minority",
        "btc_move_prev_30s_toward_minority",
        "btc_move_prev_60s_toward_minority",
        "btc_accel_10_vs_30",
        "contract_change_prev_10s_cents",
        "contract_change_prev_30s_cents",
        "contract_change_prev_60s_cents",
        "contract_decline_from_recent_high_cents",
        "contract_reaction_cents_per_10_btc",
        "dominant_ask",
        "time_remaining_seconds",
    )
    out = []
    for name, group in classes.items():
        row = {"cohort": name, "signals": len(group), "unique_markets": len({r["market_ticker"] for r in group})}
        for field in fields:
            row[f"avg_{field}"] = _avg(r.get(field) for r in group)
            row[f"median_{field}"] = _median(r.get(field) for r in group)
        out.append(row)
    return out


def _dedupe_summary(rows: list[dict[str, Any]], fee_slippage_cents: float) -> list[dict[str, Any]]:
    one_per_market: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: (r["observed_at"], r["market_ticker"])):
        one_per_market.setdefault(row["market_ticker"], row)
    clean_rows = [row for row in rows if row.get("clean_quote") == 1]
    clean_one_per_market: dict[str, dict[str, Any]] = {}
    for row in sorted(clean_rows, key=lambda r: (r["observed_at"], r["market_ticker"])):
        clean_one_per_market.setdefault(row["market_ticker"], row)

    cohorts = {
        "all_checkpoint_observations": rows,
        "one_observation_per_checkpoint_per_market": rows,
        "one_entry_per_market_first_observation": list(one_per_market.values()),
        "clean_all_checkpoint_observations": clean_rows,
        "clean_one_entry_per_market_first_observation": list(clean_one_per_market.values()),
    }
    out = []
    for name, group in cohorts.items():
        base = _base_summary(group)
        base["cohort"] = name
        vals = [row.get("tp3_sl3_gross_pnl_cents") for row in group if row.get("tp3_sl3_gross_pnl_cents") is not None]
        base["tp3_sl3_avg_gross_pnl_cents"] = _avg(vals)
        base["tp3_sl3_avg_estimated_net_pnl_cents"] = _avg([v - fee_slippage_cents for v in vals])
        base["tp3_sl3_profit_factor"] = _profit_factor(vals)
        out.append(base)
    return out


def _data_quality(raw_rows: list[dict[str, Any]], candidates: list[dict[str, Any]], missing_checkpoints: int) -> list[dict[str, Any]]:
    rows = []

    def add(issue: str, affected: int) -> None:
        if affected:
            rows.append({"issue": issue, "rows_affected": affected})

    add("missing_checkpoint_observations", missing_checkpoints)
    add("candidate_spread_eq_zero", sum(row.get("entry_spread") == 0 for row in candidates))
    add("candidate_spread_lt_zero", sum((row.get("entry_spread") or 0) < 0 for row in candidates))
    add("candidate_spread_gt_2c", sum((row.get("entry_spread") or 0) > 0.02 for row in candidates))
    add("candidate_unknown_settlement", sum(row.get("settlement_group") == "unknown_settlement" for row in candidates))
    add("candidate_quote_age_unavailable", len(candidates))
    add("raw_yes_bid_gt_ask", sum(row.get("yes_bid") is not None and row.get("yes_ask") is not None and row["yes_bid"] > row["yes_ask"] for row in raw_rows))
    add("raw_no_bid_gt_ask", sum(row.get("no_bid") is not None and row.get("no_ask") is not None and row["no_bid"] > row["no_ask"] for row in raw_rows))
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields: list[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _table(rows: list[dict[str, Any]], columns: list[str], limit: int = 12) -> str:
    if not rows:
        return "_No rows._"
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return "\n".join(lines)


def _render_report(paths: Paths, rows: list[dict[str, Any]], outputs: dict[str, list[dict[str, Any]]]) -> str:
    overall = _base_summary(rows) if rows else {}
    mae = outputs["mae_success"]
    stops = outputs["stop_width"]
    dedupe = outputs["dedupe"]
    exits = outputs["exit_model"]
    eventual_losers = [row for row in rows if row.get("settlement_group") == "ultimate_loser"]
    loser_summary = _base_summary(eventual_losers) if eventual_losers else {}

    best_non_none_stops = sorted(
        [row for row in stops if row["stop"] != "none" and row.get("target") == "+3c"],
        key=lambda row: (
            -999999 if row.get("avg_estimated_net_pnl_cents") is None else -float(row["avg_estimated_net_pnl_cents"]),
            str(row["stop"]),
        ),
    )

    return f"""# Cheap Minority Rebound Path Report

## Scope

- Market: Kalshi BTC 15-minute contracts
- Checkpoints: 3m, 4m, 5m after open
- Entry: cheaper/minority contract ask
- Exit modeling: future executable bid
- P/L: gross cents plus estimated net using configured fee/slippage placeholder

## Direct Answers

1. Frequent +3c rebounds supported: `{overall.get("target_3c_hit_rate")}%` eventual +3c hit rate across `{overall.get("signals", 0)}` candidates.
2. Median drawdown before successful +3c: `{mae[0].get("median_drawdown_before_target_cents") if mae else None}c`; 90th percentile: `{mae[0].get("p90_drawdown_before_target_cents") if mae else None}c`.
3. -3c stop too tight: check +3c survival under -3c below. If many later rebounds are stopped first, yes mechanically too tight for the path.
4. Wider stops: only useful if estimated net expectancy and PF improve; see stop-width table.
5. Best historical stop width for +3c by estimated net: `{best_non_none_stops[0].get("stop") if best_non_none_stops else None}` with avg net `{best_non_none_stops[0].get("avg_estimated_net_pnl_cents") if best_non_none_stops else None}c`.
6. Rebound speed: see time-to-rebound CSV; median times are grouped by checkpoint and entry bucket.
7. Entry ranges: see entry bucket summary.
8. BTC rapid move / deceleration: see feature comparison CSV; do not optimize thresholds from this report alone.
9. Eventual losing contracts: `{len(eventual_losers)}` candidates, +3c hit rate `{loser_summary.get("target_3c_hit_rate")}%`, +5c hit rate `{loser_summary.get("target_5c_hit_rate")}%`.
10. Dedup/clean survival: see dedupe table below.
11. Positive expectancy after costs: compare `avg_estimated_net_pnl_cents` and PF in exit model / stop-width tables.

## Overall

{_table([overall], ["signals", "unique_markets", "active_days", "clean_quotes", "ultimate_win_rate_pct", "avg_entry_ask", "target_3c_hit_rate", "target_5c_hit_rate", "target_3c_before_stop_3c_rate", "target_3c_before_stop_5c_rate", "target_3c_before_stop_10c_rate"])}

## MAE Before Successful Rebound

{_table(mae, ["target", "successful_rebounds", "avg_drawdown_before_target_cents", "median_drawdown_before_target_cents", "p25_drawdown_before_target_cents", "p75_drawdown_before_target_cents", "p90_drawdown_before_target_cents", "max_drawdown_before_target_cents"])}

## Stop Width Survival

{_table(stops, ["target", "stop", "trades", "target_first", "stop_first", "survival_to_target_pct", "avg_gross_pnl_cents", "avg_estimated_net_pnl_cents", "profit_factor_gross", "stopped_then_later_rebounded_pct"], 24)}

## Exit Models

{_table(exits, ["exit_model", "trades", "wins", "losses", "win_rate_pct", "avg_gross_pnl_cents", "avg_estimated_net_pnl_cents", "profit_factor", "max_model_drawdown_cents", "stopped_then_later_rebounded_pct"])}

## Deduplication And Clean Quotes

{_table(dedupe, ["cohort", "signals", "unique_markets", "clean_quotes", "target_3c_hit_rate", "target_3c_before_stop_3c_rate", "tp3_sl3_avg_estimated_net_pnl_cents", "tp3_sl3_profit_factor", "small_sample_flag"])}

## Output Files

- Candidate CSV: `{paths.candidates_csv}`
- MAE success CSV: `{paths.mae_success_csv}`
- Stop width CSV: `{paths.stop_width_csv}`
- Entry bucket CSV: `{paths.entry_bucket_csv}`
- Time-to-rebound CSV: `{paths.time_to_rebound_csv}`
- Exit model CSV: `{paths.exit_model_csv}`
- Feature comparison CSV: `{paths.feature_compare_csv}`
- Deduplication CSV: `{paths.dedupe_csv}`
- Data quality CSV: `{paths.data_quality_csv}`

## Final Interpretation

This report separates two claims:

- Cheap contracts frequently bounce.
- There is a profitable executable strategy for capturing those bounces.

The first claim is supported if target hit rates remain high after dedup and clean-quote filters.
The second claim requires positive estimated net expectancy, PF above 1.0, tolerable drawdown,
and survival after one-entry-per-market deduplication.
"""


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"cheap_minority_rebound_path_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        candidates_csv=output_dir / f"{stem}_candidates.csv",
        mae_success_csv=output_dir / f"{stem}_mae_success.csv",
        stop_width_csv=output_dir / f"{stem}_stop_width.csv",
        entry_bucket_csv=output_dir / f"{stem}_entry_bucket.csv",
        time_to_rebound_csv=output_dir / f"{stem}_time_to_rebound.csv",
        exit_model_csv=output_dir / f"{stem}_exit_model.csv",
        feature_compare_csv=output_dir / f"{stem}_feature_compare.csv",
        dedupe_csv=output_dir / f"{stem}_dedupe.csv",
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

    normalized = []
    for row in rows:
        item = dict(row)
        for key in (
            "strike",
            "btc_price",
            "yes_bid",
            "yes_ask",
            "yes_spread",
            "no_bid",
            "no_ask",
            "no_spread",
        ):
            item[key] = _f(item.get(key))
        for key in ("opens_at", "closes_at", "captured_at"):
            item[key] = _dt(item.get(key))
        normalized.append(item)
    return normalized


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

    candidates = []
    missing_checkpoints = 0
    for market_rows in rows_by_market.values():
        market_rows.sort(key=lambda row: row["captured_at"])
        for checkpoint in CHECKPOINT_SECONDS:
            checkpoint_row = _select_checkpoint_row(market_rows, checkpoint, checkpoint_tolerance_seconds)
            if checkpoint_row is None:
                missing_checkpoints += 1
                continue
            scored = _score_candidate(market_rows, checkpoint_row, checkpoint, fee_slippage_cents)
            if scored is not None:
                candidates.append(scored)

    outputs = {
        "mae_success": _mae_success_summary(candidates),
        "stop_width": _stop_width_summary(candidates, fee_slippage_cents),
        "entry_bucket": _summarize_by(candidates, ("entry_bucket",)),
        "time_to_rebound": _time_to_rebound_summary(candidates),
        "exit_model": _exit_model_summary(candidates),
        "feature_compare": _feature_compare_summary(candidates),
        "dedupe": _dedupe_summary(candidates, fee_slippage_cents),
        "data_quality": _data_quality(raw_rows, candidates, missing_checkpoints),
    }

    _write_csv(paths.candidates_csv, candidates)
    _write_csv(paths.mae_success_csv, outputs["mae_success"])
    _write_csv(paths.stop_width_csv, outputs["stop_width"])
    _write_csv(paths.entry_bucket_csv, outputs["entry_bucket"])
    _write_csv(paths.time_to_rebound_csv, outputs["time_to_rebound"])
    _write_csv(paths.exit_model_csv, outputs["exit_model"])
    _write_csv(paths.feature_compare_csv, outputs["feature_compare"])
    _write_csv(paths.dedupe_csv, outputs["dedupe"])
    _write_csv(paths.data_quality_csv, outputs["data_quality"])
    paths.markdown_report.write_text(_render_report(paths, candidates, outputs))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Cheap/minority rebound path report")
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
    print("Cheap minority rebound path report complete")
    print(f"candidate_csv={paths.candidates_csv}")
    print(f"mae_success_csv={paths.mae_success_csv}")
    print(f"stop_width_csv={paths.stop_width_csv}")
    print(f"entry_bucket_csv={paths.entry_bucket_csv}")
    print(f"time_to_rebound_csv={paths.time_to_rebound_csv}")
    print(f"exit_model_csv={paths.exit_model_csv}")
    print(f"feature_compare_csv={paths.feature_compare_csv}")
    print(f"dedupe_csv={paths.dedupe_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
