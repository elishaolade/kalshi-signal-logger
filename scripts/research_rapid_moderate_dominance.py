#!/usr/bin/env python3
"""
Focused report: rapid moderate dominance and minority-contract mean reversion.

Primary question:
  On clean executable quotes, with one qualifying entry per market, do fast or
  extreme-rapid moves into 60-80c dominance create positive short-term
  expectancy when buying the opposing minority contract?

Execution model:
  - Entry = minority ask at the first qualifying event.
  - Exit = first valid minority bid at or after the intended fixed hold time.
  - Fees = best-estimate Kalshi taker fee per side:
      fee_cents = fee_rate_cents * price * (1 - price)
    Default fee_rate_cents=7.0, matching observed live fill fee behavior.
  - Optional extra slippage can be layered on top.

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
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "rapid_moderate_dominance"
ET = ZoneInfo("America/New_York")

HOLD_SECONDS = (15, 30, 45, 60, 90, 120)
TARGET_CENTS = (1, 2, 3, 5, 10)
MODERATE_BUCKETS = (
    ("60-65c", 0.60, 0.65),
    ("65-70c", 0.65, 0.70),
    ("70-75c", 0.70, 0.75),
    ("75-80c", 0.75, 0.80),
)
RULES = (
    ("first_rapid_60c_plus", 0.60, ("fast", "extreme_rapid")),
    ("first_rapid_65c_plus", 0.65, ("fast", "extreme_rapid")),
    ("first_rapid_70c_plus", 0.70, ("fast", "extreme_rapid")),
    ("first_rapid_75c_plus", 0.75, ("fast", "extreme_rapid")),
    ("first_fast_60_80c", 0.60, ("fast",)),
    ("first_extreme_rapid_60_80c", 0.60, ("extreme_rapid",)),
    ("first_fast_or_extreme_60_80c", 0.60, ("fast", "extreme_rapid")),
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
    trades_csv: Path
    rule_summary_csv: Path
    matrix_csv: Path
    rebound_path_csv: Path
    feature_compare_csv: Path
    chronological_csv: Path
    outlier_sensitivity_csv: Path
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


def _first_at_or_after(rows: list[dict[str, Any]], ts: datetime, max_delay_seconds: int) -> dict[str, Any] | None:
    max_ts = ts + timedelta(seconds=max_delay_seconds)
    for row in rows:
        if ts <= row["captured_at"] <= max_ts:
            return row
    return None


def _bucket_dominance(value: float | None) -> str:
    if value is None:
        return "unknown"
    for label, lo, hi in MODERATE_BUCKETS:
        if lo <= value < hi:
            return label
    return "out_of_scope"


def _speed_bucket(cents_per_second: float | None) -> str:
    if cents_per_second is None:
        return "unknown"
    if cents_per_second >= 0.30:
        return "extreme_rapid"
    if cents_per_second >= 0.15:
        return "fast"
    if cents_per_second >= 0.07:
        return "medium"
    return "slow"


def _fee_cents(price: float, fee_rate_cents: float) -> float:
    price = max(0.0, min(1.0, price))
    return round(fee_rate_cents * price * (1.0 - price), 6)


def _clean_quote(row: dict[str, Any], max_spread: float) -> bool:
    for side in ("YES", "NO"):
        bid = _price(row, side, "bid")
        ask = _price(row, side, "ask")
        spread = _price(row, side, "spread")
        if bid is None or ask is None or spread is None:
            return False
        if bid < 0 or ask < 0 or bid > 1 or ask > 1:
            return False
        if bid > ask:
            return False
        if spread <= 0 or spread > max_spread:
            return False
    return True


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
    for prev, cur in zip(window, window[1:]):
        deltas.append(cur["btc_price"] - prev["btc_price"])
    if len(deltas) < 2:
        return None
    return round(statistics.pstdev(deltas), 4)


def _future_path(rows: list[dict[str, Any]], entry_ts: datetime, side: str, close_ts: datetime) -> list[dict[str, Any]]:
    path = []
    for row in rows:
        if entry_ts <= row["captured_at"] <= close_ts:
            bid = _price(row, side, "bid")
            ask = _price(row, side, "ask")
            if bid is not None and ask is not None:
                path.append({"captured_at": row["captured_at"], "bid": bid, "ask": ask, "btc_price": row["btc_price"]})
    return path


def _first_target(path: list[dict[str, Any]], entry_ask: float, cents: int) -> datetime | None:
    target = entry_ask + cents / 100.0
    for row in path:
        if row["bid"] >= target:
            return row["captured_at"]
    return None


def _mae_before(path: list[dict[str, Any]], entry_ask: float, target_at: datetime | None) -> float | None:
    if target_at is None:
        return None
    sub = [row for row in path if row["captured_at"] <= target_at]
    if not sub:
        return None
    return round((min(row["bid"] for row in sub) - entry_ask) * 100.0, 4)


def _seconds(start: datetime, end: datetime | None) -> float | None:
    if end is None:
        return None
    return round((end - start).total_seconds(), 3)


def _find_first_qualifying_event(
    rows: list[dict[str, Any]],
    rule_min_dominance: float,
    speed_classes: tuple[str, ...],
    max_spread: float,
) -> dict[str, Any] | None:
    for row in rows:
        elapsed = (row["captured_at"] - row["opens_at"]).total_seconds()
        if elapsed < 0 or elapsed > 300:
            continue
        if not _clean_quote(row, max_spread):
            continue
        dom_side = _dominant_side(row)
        if dom_side is None:
            continue
        dom_ask = _price(row, dom_side, "ask")
        if dom_ask is None or dom_ask < rule_min_dominance or dom_ask >= 0.80:
            continue
        open_row = _last_at_or_before(rows, row["opens_at"] + timedelta(seconds=20)) or rows[0]
        open_dom_ask = _price(open_row, dom_side, "ask")
        if open_dom_ask is None:
            continue
        speed = ((dom_ask - open_dom_ask) * 100.0) / max(1.0, elapsed)
        if _speed_bucket(speed) in speed_classes:
            return row
    return None


def _score_trade(
    market_rows: list[dict[str, Any]],
    entry_row: dict[str, Any],
    rule_name: str,
    hold_seconds: int,
    fee_rate_cents: float,
    extra_slippage_cents: float,
    max_exit_delay_seconds: int,
) -> dict[str, Any] | None:
    dom_side = _dominant_side(entry_row)
    if dom_side is None:
        return None
    min_side = _minority_side(dom_side)
    dom_ask = _price(entry_row, dom_side, "ask")
    min_ask = _price(entry_row, min_side, "ask")
    min_bid = _price(entry_row, min_side, "bid")
    min_spread = _price(entry_row, min_side, "spread")
    if dom_ask is None or min_ask is None or min_bid is None:
        return None

    entry_ts = entry_row["captured_at"]
    exit_row = _first_at_or_after(
        [row for row in market_rows if row["captured_at"] >= entry_ts],
        entry_ts + timedelta(seconds=hold_seconds),
        max_exit_delay_seconds,
    )
    if exit_row is None:
        return None
    exit_bid = _price(exit_row, min_side, "bid")
    if exit_bid is None:
        return None

    path = _future_path(market_rows, entry_ts, min_side, entry_row["closes_at"])
    open_row = _last_at_or_before(market_rows, entry_row["opens_at"] + timedelta(seconds=20)) or market_rows[0]
    prev = {
        10: _last_at_or_before(market_rows, entry_ts - timedelta(seconds=10)),
        30: _last_at_or_before(market_rows, entry_ts - timedelta(seconds=30)),
        60: _last_at_or_before(market_rows, entry_ts - timedelta(seconds=60)),
    }
    open_dom_ask = _price(open_row, dom_side, "ask")
    elapsed = max(1.0, (entry_ts - entry_row["opens_at"]).total_seconds())
    dom_increase_cents = ((dom_ask - open_dom_ask) * 100.0) if open_dom_ask is not None else None
    speed = (dom_increase_cents / elapsed) if dom_increase_cents is not None else None

    targets = {c: _first_target(path, min_ask, c) for c in TARGET_CENTS}
    winner = _winner(entry_row.get("settlement_result"))
    et = entry_ts.astimezone(ET)
    gross = round((exit_bid - min_ask) * 100.0, 4)
    entry_fee = _fee_cents(min_ask, fee_rate_cents)
    exit_fee = _fee_cents(exit_bid, fee_rate_cents)
    total_fee = round(entry_fee + exit_fee, 6)
    net = round(gross - total_fee - extra_slippage_cents, 4)

    if path:
        future_bids = [row["bid"] for row in path]
        mfe = round((max(future_bids) - min_ask) * 100.0, 4)
        mae = round((min(future_bids) - min_ask) * 100.0, 4)
    else:
        mfe = None
        mae = None

    return {
        "rule_name": rule_name,
        "hold_seconds": hold_seconds,
        "market_ticker": entry_row["market_ticker"],
        "entry_at": _iso(entry_ts),
        "entry_at_et": _iso(et),
        "entry_date_et": et.date().isoformat(),
        "hour_et": f"{et.hour:02d}:00 ET",
        "dataset_half": None,
        "dominant_side": dom_side,
        "minority_side": min_side,
        "settlement_winner": winner,
        "minority_settled_winner": int(winner == min_side) if winner is not None else None,
        "dominant_ask": dom_ask,
        "dominance_bucket": _bucket_dominance(dom_ask),
        "dominance_speed_bucket": _speed_bucket(speed),
        "dominant_cents_per_second": round(speed, 6) if speed is not None else None,
        "minority_ask": min_ask,
        "minority_bid": min_bid,
        "minority_spread": min_spread,
        "exit_at": _iso(exit_row["captured_at"]),
        "exit_delay_seconds": round((exit_row["captured_at"] - (entry_ts + timedelta(seconds=hold_seconds))).total_seconds(), 3),
        "exit_bid": exit_bid,
        "gross_pnl_cents": gross,
        "entry_fee_cents": entry_fee,
        "exit_fee_cents": exit_fee,
        "total_fee_cents": total_fee,
        "extra_slippage_cents": extra_slippage_cents,
        "net_pnl_cents": net,
        "time_since_open_seconds": round(elapsed, 3),
        "time_remaining_seconds": entry_row.get("time_remaining_seconds"),
        "btc_price": entry_row["btc_price"],
        "strike": entry_row["strike"],
        "btc_distance_dominant_side": round(_side_distance(entry_row["btc_price"], entry_row["strike"], dom_side), 2),
        "abs_btc_distance": round(abs(entry_row["btc_price"] - entry_row["strike"]), 2),
        "btc_move_since_open_dominant_side": _btc_move(entry_row, open_row, dom_side),
        "btc_move_prev_10s_dominant_side": _btc_move(entry_row, prev[10], dom_side),
        "btc_move_prev_30s_dominant_side": _btc_move(entry_row, prev[30], dom_side),
        "btc_move_prev_60s_dominant_side": _btc_move(entry_row, prev[60], dom_side),
        "dominant_change_prev_10s_cents": _contract_move(entry_row, prev[10], dom_side),
        "dominant_change_prev_30s_cents": _contract_move(entry_row, prev[30], dom_side),
        "dominant_change_prev_60s_cents": _contract_move(entry_row, prev[60], dom_side),
        "minority_change_prev_10s_cents": _contract_move(entry_row, prev[10], min_side),
        "minority_change_prev_30s_cents": _contract_move(entry_row, prev[30], min_side),
        "minority_change_prev_60s_cents": _contract_move(entry_row, prev[60], min_side),
        "btc_volatility_60s": _volatility(market_rows, entry_ts, 60),
        "mfe_cents": mfe,
        "mae_cents": mae,
        "target_1c_hit": int(targets[1] is not None),
        "target_2c_hit": int(targets[2] is not None),
        "target_3c_hit": int(targets[3] is not None),
        "target_5c_hit": int(targets[5] is not None),
        "target_10c_hit": int(targets[10] is not None),
        "seconds_to_3c": _seconds(entry_ts, targets[3]),
        "seconds_to_5c": _seconds(entry_ts, targets[5]),
        "mae_before_3c_cents": _mae_before(path, min_ask, targets[3]),
        "mae_before_5c_cents": _mae_before(path, min_ask, targets[5]),
    }


def _group(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    out: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[tuple(row.get(k) for k in keys)].append(row)
    return out


def _summary(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for key_values, group in _group(rows, keys).items():
        gross = [row["gross_pnl_cents"] for row in group]
        net = [row["net_pnl_cents"] for row in group]
        wins = sum(value > 0 for value in gross)
        net_wins = sum(value > 0 for value in net)
        row = {key: value for key, value in zip(keys, key_values)}
        row.update(
            {
                "trades": len(group),
                "unique_markets": len({r["market_ticker"] for r in group}),
                "active_days": len({r["entry_date_et"] for r in group}),
                "wins_gross": wins,
                "losses_gross": len(gross) - wins,
                "win_rate_gross_pct": _pct(wins, len(gross)),
                "wins_net": net_wins,
                "losses_net": len(net) - net_wins,
                "win_rate_net_pct": _pct(net_wins, len(net)),
                "avg_win_gross_cents": _avg(v for v in gross if v > 0),
                "avg_loss_gross_cents": _avg(v for v in gross if v <= 0),
                "median_gross_pnl_cents": _median(gross),
                "avg_gross_pnl_cents": _avg(gross),
                "total_gross_pnl_cents": round(sum(gross), 4),
                "gross_profit_factor": _profit_factor(gross),
                "max_gross_drawdown_cents": _max_drawdown(gross),
                "avg_fee_cents": _avg(row["total_fee_cents"] for row in group),
                "avg_net_pnl_cents": _avg(net),
                "total_net_pnl_cents": round(sum(net), 4),
                "net_profit_factor": _profit_factor(net),
                "max_net_drawdown_cents": _max_drawdown(net),
                "avg_dominant_ask": _avg(row["dominant_ask"] for row in group),
                "avg_minority_ask": _avg(row["minority_ask"] for row in group),
                "avg_minority_spread": _avg(row["minority_spread"] for row in group),
                "avg_time_since_open_seconds": _avg(row["time_since_open_seconds"] for row in group),
                "avg_time_remaining_seconds": _avg(row["time_remaining_seconds"] for row in group),
                "avg_abs_btc_distance": _avg(row["abs_btc_distance"] for row in group),
                "target_3c_rate": _pct(sum(row["target_3c_hit"] for row in group), len(group)),
                "target_5c_rate": _pct(sum(row["target_5c_hit"] for row in group), len(group)),
                "median_time_to_3c": _median(row["seconds_to_3c"] for row in group),
                "median_time_to_5c": _median(row["seconds_to_5c"] for row in group),
                "median_mae_before_3c": _median(row["mae_before_3c_cents"] for row in group if row["target_3c_hit"]),
                "median_mae_before_5c": _median(row["mae_before_5c_cents"] for row in group if row["target_5c_hit"]),
                "p75_mae_before_3c": _percentile((row["mae_before_3c_cents"] for row in group if row["target_3c_hit"]), 0.75),
                "p90_mae_before_3c": _percentile((row["mae_before_3c_cents"] for row in group if row["target_3c_hit"]), 0.90),
                "small_sample_flag": int(len(group) < 30 or len({r["entry_date_et"] for r in group}) < 5),
            }
        )
        out.append(row)
    return sorted(out, key=lambda row: (-int(row["trades"]), tuple(str(row.get(k)) for k in keys)))


def _feature_compare(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base = [row for row in rows if row["rule_name"] == "first_fast_or_extreme_60_80c" and row["hold_seconds"] == 120]
    cohorts = {
        "profitable_120s_net": [row for row in base if row["net_pnl_cents"] > 0],
        "losing_120s_net": [row for row in base if row["net_pnl_cents"] <= 0],
        "fast_successful_rebound_3c": [row for row in base if row["target_3c_hit"]],
        "continued_decline_no_3c": [row for row in base if not row["target_3c_hit"]],
    }
    fields = (
        "dominant_ask",
        "minority_ask",
        "minority_spread",
        "dominant_cents_per_second",
        "btc_move_since_open_dominant_side",
        "btc_move_prev_10s_dominant_side",
        "btc_move_prev_30s_dominant_side",
        "btc_move_prev_60s_dominant_side",
        "btc_distance_dominant_side",
        "abs_btc_distance",
        "btc_volatility_60s",
        "dominant_change_prev_30s_cents",
        "minority_change_prev_30s_cents",
    )
    out = []
    for name, group in cohorts.items():
        row = {"cohort": name, "trades": len(group), "unique_markets": len({r["market_ticker"] for r in group})}
        for field in fields:
            row[f"avg_{field}"] = _avg(r.get(field) for r in group)
            row[f"median_{field}"] = _median(r.get(field) for r in group)
        out.append(row)
    return out


def _chronological(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base = [row for row in rows if row["rule_name"] == "first_fast_or_extreme_60_80c" and row["hold_seconds"] == 120]
    ordered_dates = sorted({row["entry_date_et"] for row in base})
    midpoint = len(ordered_dates) // 2
    first_dates = set(ordered_dates[:midpoint])
    second_dates = set(ordered_dates[midpoint:])
    cohorts = {
        "first_half": [row for row in base if row["entry_date_et"] in first_dates],
        "second_half": [row for row in base if row["entry_date_et"] in second_dates],
    }
    out = []
    for row in _summary(base, ("entry_date_et",)):
        row["breakdown"] = "day"
        out.append(row)
    for row in _summary(base, ("hour_et",)):
        row["breakdown"] = "hour_et"
        out.append(row)
    for name, group in cohorts.items():
        for row in _summary(group, tuple()):
            row["breakdown"] = name
            out.append(row)
    return out


def _outlier_sensitivity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base = [row for row in rows if row["rule_name"] == "first_fast_or_extreme_60_80c" and row["hold_seconds"] == 120]
    variants = {
        "raw": base,
        "remove_largest_winner": _remove_largest_winner(base),
        "remove_top_1pct_winners": _remove_top_pct_winners(base, 0.01),
        "winsorize_99pct": _winsorize(base, 0.99),
    }
    out = []
    for name, group in variants.items():
        for row in _summary(group, tuple()):
            row["variant"] = name
            out.append(row)
    return out


def _remove_largest_winner(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    winners = [row for row in rows if row["net_pnl_cents"] > 0]
    if not winners:
        return list(rows)
    largest = max(winners, key=lambda row: row["net_pnl_cents"])
    removed = False
    out = []
    for row in rows:
        if row is largest and not removed:
            removed = True
            continue
        out.append(row)
    return out


def _remove_top_pct_winners(rows: list[dict[str, Any]], pct: float) -> list[dict[str, Any]]:
    winners = sorted([row for row in rows if row["net_pnl_cents"] > 0], key=lambda row: row["net_pnl_cents"], reverse=True)
    remove_n = max(1, math.ceil(len(winners) * pct)) if winners else 0
    remove_ids = {id(row) for row in winners[:remove_n]}
    return [row for row in rows if id(row) not in remove_ids]


def _winsorize(rows: list[dict[str, Any]], pct: float) -> list[dict[str, Any]]:
    if not rows:
        return []
    cap = _percentile((row["net_pnl_cents"] for row in rows), pct)
    if cap is None:
        return list(rows)
    out = []
    for row in rows:
        item = dict(row)
        if item["net_pnl_cents"] > cap:
            delta = item["net_pnl_cents"] - cap
            item["net_pnl_cents"] = cap
            item["gross_pnl_cents"] = round(item["gross_pnl_cents"] - delta, 4)
        out.append(item)
    return out


def _data_quality(raw_rows: list[dict[str, Any]], rows_by_market: dict[int, list[dict[str, Any]]], max_spread: float) -> list[dict[str, Any]]:
    out = []

    def add(issue: str, count: int) -> None:
        if count:
            out.append({"issue": issue, "rows_affected": count})

    add("quote_age_unavailable", len(raw_rows))
    add("raw_yes_bid_gt_ask", sum(r.get("yes_bid") is not None and r.get("yes_ask") is not None and r["yes_bid"] > r["yes_ask"] for r in raw_rows))
    add("raw_no_bid_gt_ask", sum(r.get("no_bid") is not None and r.get("no_ask") is not None and r["no_bid"] > r["no_ask"] for r in raw_rows))
    add("raw_zero_spread_yes", sum(r.get("yes_spread") == 0 for r in raw_rows))
    add("raw_zero_spread_no", sum(r.get("no_spread") == 0 for r in raw_rows))
    add("raw_spread_gt_max_yes", sum((r.get("yes_spread") or 0) > max_spread for r in raw_rows))
    add("raw_spread_gt_max_no", sum((r.get("no_spread") or 0) > max_spread for r in raw_rows))
    add("markets_loaded", len(rows_by_market))
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


def _table(rows: list[dict[str, Any]], cols: list[str], limit: int = 16) -> str:
    if not rows:
        return "_No rows._"
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in cols) + " |")
    return "\n".join(lines)


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"rapid_moderate_dominance_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        trades_csv=output_dir / f"{stem}_trades.csv",
        rule_summary_csv=output_dir / f"{stem}_rule_summary.csv",
        matrix_csv=output_dir / f"{stem}_matrix.csv",
        rebound_path_csv=output_dir / f"{stem}_rebound_path.csv",
        feature_compare_csv=output_dir / f"{stem}_feature_compare.csv",
        chronological_csv=output_dir / f"{stem}_chronological.csv",
        outlier_sensitivity_csv=output_dir / f"{stem}_outlier_sensitivity.csv",
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
    max_spread: float,
    fee_rate_cents: float,
    extra_slippage_cents: float,
    max_exit_delay_seconds: int,
) -> Paths:
    paths = _paths(output_dir)
    raw_rows = _load_rows(market_like, start, end)
    rows_by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        rows_by_market[int(row["market_pk"])].append(row)
    for rows in rows_by_market.values():
        rows.sort(key=lambda row: row["captured_at"])

    trades = []
    for market_rows in rows_by_market.values():
        for rule_name, min_dominance, speed_classes in RULES:
            event = _find_first_qualifying_event(market_rows, min_dominance, speed_classes, max_spread)
            if event is None:
                continue
            for hold in HOLD_SECONDS:
                scored = _score_trade(
                    market_rows,
                    event,
                    rule_name,
                    hold,
                    fee_rate_cents,
                    extra_slippage_cents,
                    max_exit_delay_seconds,
                )
                if scored is not None:
                    trades.append(scored)

    by_date = sorted({row["entry_date_et"] for row in trades})
    first_half_dates = set(by_date[: len(by_date) // 2])
    for row in trades:
        row["dataset_half"] = "first_half" if row["entry_date_et"] in first_half_dates else "second_half"

    rule_summary = _summary(trades, ("rule_name", "hold_seconds"))
    matrix = _summary(
        [row for row in trades if row["rule_name"] == "first_fast_or_extreme_60_80c" and row["hold_seconds"] in (30, 60, 90, 120)],
        ("dominance_bucket", "dominance_speed_bucket", "hold_seconds"),
    )
    rebound_path = _summary(
        [row for row in trades if row["rule_name"] == "first_fast_or_extreme_60_80c" and row["hold_seconds"] == 120],
        ("dominance_bucket", "dominance_speed_bucket"),
    )
    feature_compare = _feature_compare(trades)
    chronological = _chronological(trades)
    outlier_sensitivity = _outlier_sensitivity(trades)
    data_quality = _data_quality(raw_rows, rows_by_market, max_spread)

    outputs = {
        "rule_summary": rule_summary,
        "matrix": matrix,
        "rebound_path": rebound_path,
        "feature_compare": feature_compare,
        "chronological": chronological,
        "outlier_sensitivity": outlier_sensitivity,
        "data_quality": data_quality,
    }

    _write_csv(paths.trades_csv, trades)
    _write_csv(paths.rule_summary_csv, rule_summary)
    _write_csv(paths.matrix_csv, matrix)
    _write_csv(paths.rebound_path_csv, rebound_path)
    _write_csv(paths.feature_compare_csv, feature_compare)
    _write_csv(paths.chronological_csv, chronological)
    _write_csv(paths.outlier_sensitivity_csv, outlier_sensitivity)
    _write_csv(paths.data_quality_csv, data_quality)
    paths.markdown_report.write_text(_render_markdown(paths, outputs, max_spread, fee_rate_cents, extra_slippage_cents, max_exit_delay_seconds))
    return paths


def _render_markdown(
    paths: Paths,
    outputs: dict[str, list[dict[str, Any]]],
    max_spread: float,
    fee_rate_cents: float,
    extra_slippage_cents: float,
    max_exit_delay_seconds: int,
) -> str:
    rule_summary = outputs["rule_summary"]
    candidates = [
        row for row in rule_summary
        if row.get("trades", 0) >= 30
        and row.get("active_days", 0) >= 5
        and row.get("avg_net_pnl_cents") is not None
        and row.get("net_profit_factor") not in (None, "")
    ]
    positive = [
        row for row in candidates
        if float(row.get("avg_net_pnl_cents") or 0) > 0
        and (row.get("net_profit_factor") == "inf" or float(row.get("net_profit_factor") or 0) > 1)
    ]
    positive_sorted = sorted(positive, key=lambda row: -float(row["avg_net_pnl_cents"]))

    return f"""# Rapid Moderate Dominance Report

## Scope

- Primary zone: dominant ask 60-80c in the first 5 minutes
- Primary speed classes: fast and extreme_rapid
- Primary evidence: clean quotes only, one qualifying entry per market per deterministic rule
- Entry: minority ask
- Exit: first valid minority bid at or after the fixed hold time

## Clean Quote Criteria

- YES and NO bid/ask present
- Prices between 0 and 1
- bid <= ask for both sides
- spread > 0 for both sides
- spread <= `{max_spread}` for both sides
- Historical quote age is unavailable, so stale quote filtering is limited to timestamp path availability

## Speed Definitions

- extreme_rapid: dominant contract moved `>= 0.30` cents/second from open
- fast: `>= 0.15` and `< 0.30` cents/second
- medium: `>= 0.07` and `< 0.15` cents/second
- slow: `< 0.07` cents/second

## Fee Model

- fee cents per side = `{fee_rate_cents} * price * (1 - price)`
- net P/L = gross bid/ask P/L - entry fee - exit fee - extra slippage
- extra slippage cents = `{extra_slippage_cents}`
- exit quote rule: first valid quote at or after intended exit, max delay `{max_exit_delay_seconds}` seconds

## Candidate Rules With Positive Net Expectancy

{_table(positive_sorted, ["rule_name", "hold_seconds", "trades", "unique_markets", "active_days", "win_rate_net_pct", "avg_gross_pnl_cents", "avg_net_pnl_cents", "net_profit_factor", "max_net_drawdown_cents", "small_sample_flag"], 20)}

## Rule Summary

{_table(rule_summary, ["rule_name", "hold_seconds", "trades", "unique_markets", "active_days", "win_rate_gross_pct", "avg_gross_pnl_cents", "avg_net_pnl_cents", "gross_profit_factor", "net_profit_factor", "max_net_drawdown_cents", "small_sample_flag"], 42)}

## Dominance Level x Speed x Hold Matrix

{_table(outputs["matrix"], ["dominance_bucket", "dominance_speed_bucket", "hold_seconds", "trades", "active_days", "win_rate_net_pct", "avg_gross_pnl_cents", "avg_net_pnl_cents", "net_profit_factor", "max_net_drawdown_cents", "small_sample_flag"], 32)}

## Outlier Sensitivity

{_table(outputs["outlier_sensitivity"], ["variant", "trades", "unique_markets", "avg_gross_pnl_cents", "avg_net_pnl_cents", "net_profit_factor", "max_net_drawdown_cents"], 12)}

## Data Quality

{_table(outputs["data_quality"], ["issue", "rows_affected"], 20)}

## Direct Classification

If the positive-net table is empty, classify the evidence as either unsupported or behavioral-but-not-tradable.
If a simple rule remains positive after clean quotes, one-entry-per-market, fees, outlier sensitivity,
and chronological checks, it is only a candidate for prospective simulated testing, not live sizing.

## Output Files

- Trades CSV: `{paths.trades_csv}`
- Rule summary CSV: `{paths.rule_summary_csv}`
- Matrix CSV: `{paths.matrix_csv}`
- Rebound path CSV: `{paths.rebound_path_csv}`
- Feature comparison CSV: `{paths.feature_compare_csv}`
- Chronological CSV: `{paths.chronological_csv}`
- Outlier sensitivity CSV: `{paths.outlier_sensitivity_csv}`
- Data quality CSV: `{paths.data_quality_csv}`
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Rapid moderate dominance research report")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M-%")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-spread", type=float, default=0.02)
    parser.add_argument("--fee-rate-cents", type=float, default=7.0)
    parser.add_argument("--extra-slippage-cents", type=float, default=0.0)
    parser.add_argument("--max-exit-delay-seconds", type=int, default=5)
    args = parser.parse_args()

    paths = build_report(
        output_dir=args.output_dir,
        market_like=args.market_like,
        start=args.start,
        end=args.end,
        max_spread=args.max_spread,
        fee_rate_cents=args.fee_rate_cents,
        extra_slippage_cents=args.extra_slippage_cents,
        max_exit_delay_seconds=args.max_exit_delay_seconds,
    )
    print("Rapid moderate dominance report complete")
    print(f"trades_csv={paths.trades_csv}")
    print(f"rule_summary_csv={paths.rule_summary_csv}")
    print(f"matrix_csv={paths.matrix_csv}")
    print(f"rebound_path_csv={paths.rebound_path_csv}")
    print(f"feature_compare_csv={paths.feature_compare_csv}")
    print(f"chronological_csv={paths.chronological_csv}")
    print(f"outlier_sensitivity_csv={paths.outlier_sensitivity_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
