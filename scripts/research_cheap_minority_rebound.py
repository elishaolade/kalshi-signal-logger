#!/usr/bin/env python3
"""
Research report: cheap/minority contract rebound in Kalshi BTC 15m markets.

This script intentionally does the path analysis in Python instead of one huge
SQL statement. That keeps it Docker/terminal friendly and avoids MySQL temp
table/CTE reopen limits.

Outputs:
  - candidate-level CSV
  - grouped summary CSVs
  - data-quality CSV
  - markdown report

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
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "cheap_minority_rebound"
ET = ZoneInfo("America/New_York")

CHECKPOINT_SECONDS = (180, 240, 300)
TARGET_CENTS = (1, 2, 3, 5, 10)
ENTRY_BUCKETS = (
    ("0-5c", 0.00, 0.05),
    ("5-10c", 0.05, 0.10),
    ("10-15c", 0.10, 0.15),
    ("15-20c", 0.15, 0.20),
    ("20-30c", 0.20, 0.30),
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
    grouped_summary_csv: Path
    checkpoint_summary_csv: Path
    context_summary_csv: Path
    overshoot_summary_csv: Path
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


def _to_et(value: datetime) -> datetime:
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


def _profit_factor(values: Iterable[float | None]) -> float | str | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    if losses == 0:
        return "inf" if gains > 0 else None
    return round(gains / losses, 4)


def _first_time(rows: list[dict[str, Any]], predicate) -> datetime | None:
    for row in rows:
        if predicate(row):
            return row["captured_at"]
    return None


def _last_at_or_before(rows: list[dict[str, Any]], target_ts: datetime) -> dict[str, Any] | None:
    best = None
    for row in rows:
        if row["captured_at"] <= target_ts:
            best = row
        else:
            break
    return best


def _price_for_side(row: dict[str, Any], side: str, kind: str) -> float | None:
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


def _settlement_winner(result: str | None) -> str | None:
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


def _contract_move(row_now: dict[str, Any], row_then: dict[str, Any] | None, side: str) -> float | None:
    if row_then is None:
        return None
    now = _price_for_side(row_now, side, "ask")
    then = _price_for_side(row_then, side, "ask")
    if now is None or then is None:
        return None
    return round(100.0 * (now - then), 4)


def _btc_move(row_now: dict[str, Any], row_then: dict[str, Any] | None, side: str, strike: float) -> float | None:
    if row_then is None:
        return None
    return round(
        _side_distance(row_now["btc_price"], strike, side)
        - _side_distance(row_then["btc_price"], strike, side),
        2,
    )


def _select_checkpoint_row(rows: list[dict[str, Any]], checkpoint_seconds: int, tolerance_seconds: int) -> dict[str, Any] | None:
    candidates = []
    for row in rows:
        elapsed = (row["captured_at"] - row["opens_at"]).total_seconds()
        diff = abs(elapsed - checkpoint_seconds)
        if diff <= tolerance_seconds:
            candidates.append((diff, row["captured_at"], row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


def _score_candidate(
    market_rows: list[dict[str, Any]],
    row: dict[str, Any],
    checkpoint_seconds: int,
    fee_slippage_cents: float,
) -> dict[str, Any] | None:
    side = _cheap_side(row)
    if side is None:
        return None

    entry_ask = _price_for_side(row, side, "ask")
    entry_bid = _price_for_side(row, side, "bid")
    spread = _price_for_side(row, side, "spread")
    if entry_ask is None or entry_bid is None:
        return None
    if entry_ask < 0 or entry_bid < 0 or entry_ask > 1 or entry_bid > 1:
        return None

    strike = row["strike"]
    entry_distance = _side_distance(row["btc_price"], strike, side)
    entry_ts = row["captured_at"]
    open_row = _last_at_or_before(market_rows, row["opens_at"] + (entry_ts - row["opens_at"]) * 0) or market_rows[0]
    prev_10 = _last_at_or_before(market_rows, entry_ts.replace() - timedelta_seconds(10))
    prev_30 = _last_at_or_before(market_rows, entry_ts.replace() - timedelta_seconds(30))
    prev_60 = _last_at_or_before(market_rows, entry_ts.replace() - timedelta_seconds(60))

    prior_rows = [r for r in market_rows if r["captured_at"] <= entry_ts]
    future_rows = [r for r in market_rows if r["captured_at"] > entry_ts and r["captured_at"] <= row["closes_at"]]
    future_30 = [r for r in future_rows if r["captured_at"] <= entry_ts + timedelta_seconds(30)]
    future_60 = [r for r in future_rows if r["captured_at"] <= entry_ts + timedelta_seconds(60)]
    future_120 = [r for r in future_rows if r["captured_at"] <= entry_ts + timedelta_seconds(120)]

    prior_asks = [_price_for_side(r, side, "ask") for r in prior_rows]
    prior_asks = [v for v in prior_asks if v is not None]
    future_bids = [_price_for_side(r, side, "bid") for r in future_rows]
    future_bids = [v for v in future_bids if v is not None]

    if not future_bids:
        return None

    def max_bid(rows: list[dict[str, Any]]) -> float | None:
        vals = [_price_for_side(r, side, "bid") for r in rows]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    def first_bid_at_or_above(delta_cents: int) -> datetime | None:
        target = entry_ask + delta_cents / 100.0
        return _first_time(future_rows, lambda r: (_price_for_side(r, side, "bid") or -1) >= target)

    def first_bid_at_or_below(delta_cents: int) -> datetime | None:
        stop = entry_ask - delta_cents / 100.0
        return _first_time(future_rows, lambda r: (_price_for_side(r, side, "bid") or 2) <= stop)

    target_times = {c: first_bid_at_or_above(c) for c in TARGET_CENTS}
    stop_2_at = first_bid_at_or_below(2)
    stop_3_at = first_bid_at_or_below(3)

    max_future_bid = max(future_bids)
    min_future_bid = min(future_bids)
    max_bid_30 = max_bid(future_30)
    max_bid_60 = max_bid(future_60)
    max_bid_120 = max_bid(future_120)
    mfe_cents = round(100.0 * (max_future_bid - entry_ask), 4)
    mae_cents = round(100.0 * (min_future_bid - entry_ask), 4)

    settlement_winner = _settlement_winner(row.get("settlement_result"))
    settlement_group = (
        "unknown_settlement"
        if settlement_winner is None
        else "ultimate_winner"
        if settlement_winner == side
        else "ultimate_loser"
    )
    settlement_pnl_cents = None
    if settlement_winner is not None:
        settlement_pnl_cents = round((1.0 - entry_ask) * 100.0, 4) if settlement_winner == side else round(-entry_ask * 100.0, 4)

    def model_pnl(target_cents: int, stop_cents: int) -> float:
        target_at = target_times[target_cents]
        stop_at = stop_2_at if stop_cents == 2 else stop_3_at
        if target_at is not None and (stop_at is None or target_at < stop_at):
            return float(target_cents)
        if stop_at is not None and (target_at is None or stop_at < target_at):
            return -float(stop_cents)
        return round(100.0 * (future_bids[-1] - entry_ask), 4)

    et = _to_et(entry_ts)
    return {
        "market_ticker": row["market_ticker"],
        "checkpoint_seconds": checkpoint_seconds,
        "observed_at": _iso(entry_ts),
        "observed_at_et": _iso(et),
        "entry_date_et": et.date().isoformat(),
        "hour_et": f"{et.hour:02d}:00 ET",
        "day_of_week_et": et.strftime("%a"),
        "side": side,
        "settlement_winner": settlement_winner,
        "settlement_group": settlement_group,
        "strike": strike,
        "btc_price": row["btc_price"],
        "entry_distance": round(entry_distance, 2),
        "time_remaining_seconds": row.get("time_remaining_seconds"),
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "entry_spread": spread,
        "entry_bucket": _bucket(entry_ask, ENTRY_BUCKETS),
        "btc_move_since_open": _btc_move(row, open_row, side, strike),
        "btc_move_prev_10s": _btc_move(row, prev_10, side, strike),
        "btc_move_prev_30s": _btc_move(row, prev_30, side, strike),
        "btc_move_prev_60s": _btc_move(row, prev_60, side, strike),
        "contract_move_prev_10s_cents": _contract_move(row, prev_10, side),
        "contract_move_prev_30s_cents": _contract_move(row, prev_30, side),
        "contract_move_prev_60s_cents": _contract_move(row, prev_60, side),
        "prior_high_ask": max(prior_asks) if prior_asks else None,
        "prior_low_ask": min(prior_asks) if prior_asks else None,
        "decline_from_prior_high_cents": round(100.0 * ((max(prior_asks) if prior_asks else entry_ask) - entry_ask), 4),
        "future_max_bid_30s": max_bid_30,
        "future_max_bid_60s": max_bid_60,
        "future_max_bid_120s": max_bid_120,
        "future_max_bid_to_expiry": max_future_bid,
        "future_min_bid_to_expiry": min_future_bid,
        "mfe_cents": mfe_cents,
        "mae_cents": mae_cents,
        "target_1c_hit_at": _iso(target_times[1]),
        "target_2c_hit_at": _iso(target_times[2]),
        "target_3c_hit_at": _iso(target_times[3]),
        "target_5c_hit_at": _iso(target_times[5]),
        "target_10c_hit_at": _iso(target_times[10]),
        "stop_2c_hit_at": _iso(stop_2_at),
        "stop_3c_hit_at": _iso(stop_3_at),
        "target_2c_before_stop_2c": int(target_times[2] is not None and (stop_2_at is None or target_times[2] < stop_2_at)),
        "target_3c_before_stop_3c": int(target_times[3] is not None and (stop_3_at is None or target_times[3] < stop_3_at)),
        "target_5c_before_stop_3c": int(target_times[5] is not None and (stop_3_at is None or target_times[5] < stop_3_at)),
        "pnl_2c_target_2c_stop": model_pnl(2, 2),
        "pnl_3c_target_3c_stop": model_pnl(3, 3),
        "pnl_5c_target_3c_stop": model_pnl(5, 3),
        "gross_best_pnl_cents": mfe_cents,
        "estimated_net_best_pnl_cents": round(mfe_cents - fee_slippage_cents, 4),
        "settlement_pnl_cents": settlement_pnl_cents,
        "btc_source": row.get("btc_source"),
        "quote_quality_flags": "",
    }


def timedelta_seconds(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def _summary(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(k) for k in keys)].append(row)

    out = []
    for key_values, group in groups.items():
        n = len(group)
        active_days = len({r.get("entry_date_et") for r in group if r.get("entry_date_et")})
        row = {k: v for k, v in zip(keys, key_values)}
        row.update(
            {
                "signals": n,
                "unique_markets": len({r["market_ticker"] for r in group}),
                "active_days": active_days,
                "ultimate_winners": sum(r.get("settlement_group") == "ultimate_winner" for r in group),
                "ultimate_losers": sum(r.get("settlement_group") == "ultimate_loser" for r in group),
                "ultimate_win_rate_pct": _pct(sum(r.get("settlement_group") == "ultimate_winner" for r in group), n),
                "avg_entry_ask": _avg(r.get("entry_ask") for r in group),
                "avg_entry_spread": _avg(r.get("entry_spread") for r in group),
                "avg_entry_distance": _avg(r.get("entry_distance") for r in group),
                "avg_mfe_cents": _avg(r.get("mfe_cents") for r in group),
                "median_mfe_cents": _median(r.get("mfe_cents") for r in group),
                "avg_mae_cents": _avg(r.get("mae_cents") for r in group),
                "median_mae_cents": _median(r.get("mae_cents") for r in group),
                "target_1c_hit_rate": _pct(sum(bool(r.get("target_1c_hit_at")) for r in group), n),
                "target_2c_hit_rate": _pct(sum(bool(r.get("target_2c_hit_at")) for r in group), n),
                "target_3c_hit_rate": _pct(sum(bool(r.get("target_3c_hit_at")) for r in group), n),
                "target_5c_hit_rate": _pct(sum(bool(r.get("target_5c_hit_at")) for r in group), n),
                "target_10c_hit_rate": _pct(sum(bool(r.get("target_10c_hit_at")) for r in group), n),
                "target_2c_before_stop_2c_rate": _pct(sum(r.get("target_2c_before_stop_2c") == 1 for r in group), n),
                "target_3c_before_stop_3c_rate": _pct(sum(r.get("target_3c_before_stop_3c") == 1 for r in group), n),
                "target_5c_before_stop_3c_rate": _pct(sum(r.get("target_5c_before_stop_3c") == 1 for r in group), n),
                "avg_pnl_2c_target_2c_stop": _avg(r.get("pnl_2c_target_2c_stop") for r in group),
                "profit_factor_2c_target_2c_stop": _profit_factor(r.get("pnl_2c_target_2c_stop") for r in group),
                "avg_pnl_3c_target_3c_stop": _avg(r.get("pnl_3c_target_3c_stop") for r in group),
                "profit_factor_3c_target_3c_stop": _profit_factor(r.get("pnl_3c_target_3c_stop") for r in group),
                "avg_pnl_5c_target_3c_stop": _avg(r.get("pnl_5c_target_3c_stop") for r in group),
                "profit_factor_5c_target_3c_stop": _profit_factor(r.get("pnl_5c_target_3c_stop") for r in group),
                "avg_settlement_pnl_cents": _avg(r.get("settlement_pnl_cents") for r in group),
                "small_sample_flag": int(n < 30 or active_days < 5),
            }
        )
        out.append(row)
    return sorted(out, key=lambda r: (-int(r["signals"]), tuple(str(r.get(k)) for k in keys)))


def _overshoot_label(row: dict[str, Any]) -> str:
    btc = row.get("btc_move_since_open")
    decline = row.get("decline_from_prior_high_cents")
    if btc is not None and btc >= 150 and decline is not None and decline >= 20:
        return "btc_overshoot_150_contract_down_20c"
    if btc is not None and btc >= 100 and decline is not None and decline >= 10:
        return "btc_overshoot_100_contract_down_10c"
    if decline is not None and decline >= 10:
        return "contract_declined_10c_plus"
    if btc is not None and btc >= 100:
        return "btc_overshoot_100_plus"
    return "other"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _quality_rows(raw_rows: list[dict[str, Any]], candidates: list[dict[str, Any]], missing_checkpoints: int) -> list[dict[str, Any]]:
    quality = []

    def add(issue: str, rows_affected: int) -> None:
        if rows_affected:
            quality.append({"issue": issue, "rows_affected": rows_affected})

    add("missing_checkpoint_observations", missing_checkpoints)
    add("candidate_spread_eq_zero", sum((r.get("entry_spread") == 0) for r in candidates))
    add("candidate_spread_lt_zero", sum(((r.get("entry_spread") or 0) < 0) for r in candidates))
    add("raw_quote_bid_gt_ask_yes", sum((r.get("yes_bid") is not None and r.get("yes_ask") is not None and r["yes_bid"] > r["yes_ask"]) for r in raw_rows))
    add("raw_quote_bid_gt_ask_no", sum((r.get("no_bid") is not None and r.get("no_ask") is not None and r["no_bid"] > r["no_ask"]) for r in raw_rows))
    add("unknown_settlement_candidates", sum(r.get("settlement_group") == "unknown_settlement" for r in candidates))
    add("quote_age_ms_unavailable_in_historical_snapshots", len(candidates))
    return quality


def _render_report(paths: Paths, rows: list[dict[str, Any]], summaries: dict[str, list[dict[str, Any]]], quality: list[dict[str, Any]]) -> str:
    overall = _summary(rows, tuple())[0] if rows else {}
    top_bucket = summaries["grouped"][:10]

    def table(items: list[dict[str, Any]], columns: list[str], limit: int = 12) -> str:
        if not items:
            return "_No rows._"
        lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
        for item in items[:limit]:
            lines.append("| " + " | ".join(str(item.get(c, "")) for c in columns) + " |")
        return "\n".join(lines)

    return f"""# Cheap Minority Rebound Report

## Scope

- Market: Kalshi BTC 15-minute contracts
- Checkpoints after market open: 3m, 4m, 5m
- Entry side: lower-priced YES/NO contract by executable ask
- Entry buckets: 0-50c
- Exit modeling: future bid path, gross cents before real fees/fill slippage

## Executive Summary

- Candidate signals: `{overall.get("signals", 0)}`
- Unique markets: `{overall.get("unique_markets", 0)}`
- Ultimate win rate: `{overall.get("ultimate_win_rate_pct")}%`
- Avg entry ask: `{overall.get("avg_entry_ask")}`
- Avg MFE: `{overall.get("avg_mfe_cents")}c`
- Avg MAE: `{overall.get("avg_mae_cents")}c`
- +3c hit rate: `{overall.get("target_3c_hit_rate")}%`
- +5c hit rate: `{overall.get("target_5c_hit_rate")}%`
- +3c before -3c rate: `{overall.get("target_3c_before_stop_3c_rate")}%`
- +5c before -3c rate: `{overall.get("target_5c_before_stop_3c_rate")}%`

## Best Buckets By Signal Count

{table(top_bucket, ["entry_bucket", "checkpoint_seconds", "settlement_group", "signals", "unique_markets", "ultimate_win_rate_pct", "target_3c_hit_rate", "target_5c_hit_rate", "avg_pnl_3c_target_3c_stop", "profit_factor_3c_target_3c_stop", "small_sample_flag"])}

## Data Quality

{table(quality, ["issue", "rows_affected"])}

## Output Files

- Candidate CSV: `{paths.candidates_csv}`
- Grouped summary CSV: `{paths.grouped_summary_csv}`
- Checkpoint summary CSV: `{paths.checkpoint_summary_csv}`
- Context summary CSV: `{paths.context_summary_csv}`
- Overshoot summary CSV: `{paths.overshoot_summary_csv}`
- Data quality CSV: `{paths.data_quality_csv}`

## Warning

This is a historical diagnostic. Treat groups with `small_sample_flag=1` as idea generation only, not proof of a live edge.
"""


def _output_paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"cheap_minority_rebound_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        candidates_csv=output_dir / f"{stem}_candidates.csv",
        grouped_summary_csv=output_dir / f"{stem}_grouped_summary.csv",
        checkpoint_summary_csv=output_dir / f"{stem}_checkpoint_summary.csv",
        context_summary_csv=output_dir / f"{stem}_context_summary.csv",
        overshoot_summary_csv=output_dir / f"{stem}_overshoot_summary.csv",
        data_quality_csv=output_dir / f"{stem}_data_quality.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def build_report(
    output_dir: Path,
    market_like: str,
    start: str | None,
    end: str | None,
    checkpoint_tolerance_seconds: int,
    fee_slippage_cents: float,
) -> Paths:
    from app.db import get_pool

    paths = _output_paths(output_dir)
    conn = get_pool().get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(QUOTE_TIMELINE_SQL, (market_like, start, end))
        raw_rows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()

    rows_by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        normalized = dict(row)
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
            normalized[key] = _f(normalized.get(key))
        for key in ("opens_at", "closes_at", "captured_at"):
            normalized[key] = _dt(normalized.get(key))
        rows_by_market[int(normalized["market_pk"])].append(normalized)

    candidate_rows: list[dict[str, Any]] = []
    missing_checkpoints = 0
    for market_rows in rows_by_market.values():
        market_rows.sort(key=lambda r: r["captured_at"])
        for checkpoint in CHECKPOINT_SECONDS:
            checkpoint_row = _select_checkpoint_row(
                market_rows,
                checkpoint,
                checkpoint_tolerance_seconds,
            )
            if checkpoint_row is None:
                missing_checkpoints += 1
                continue
            scored = _score_candidate(market_rows, checkpoint_row, checkpoint, fee_slippage_cents)
            if scored is None:
                continue
            if scored["entry_bucket"] == "out_of_range":
                continue
            candidate_rows.append(scored)

    for row in candidate_rows:
        row["overshoot_shape"] = _overshoot_label(row)

    grouped_summary = _summary(candidate_rows, ("entry_bucket", "checkpoint_seconds", "settlement_group"))
    checkpoint_summary = _summary(candidate_rows, ("checkpoint_seconds", "side", "settlement_group"))
    context_summary = _summary(candidate_rows, ("hour_et", "day_of_week_et", "entry_bucket"))
    overshoot_summary = _summary(candidate_rows, ("overshoot_shape", "entry_bucket", "checkpoint_seconds"))
    quality = _quality_rows(raw_rows, candidate_rows, missing_checkpoints)

    _write_csv(paths.candidates_csv, candidate_rows)
    _write_csv(paths.grouped_summary_csv, grouped_summary)
    _write_csv(paths.checkpoint_summary_csv, checkpoint_summary)
    _write_csv(paths.context_summary_csv, context_summary)
    _write_csv(paths.overshoot_summary_csv, overshoot_summary)
    _write_csv(paths.data_quality_csv, quality)
    paths.markdown_report.write_text(
        _render_report(
            paths,
            candidate_rows,
            {
                "grouped": grouped_summary,
                "checkpoint": checkpoint_summary,
                "context": context_summary,
                "overshoot": overshoot_summary,
            },
            quality,
        )
    )
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Cheap/minority contract rebound research report")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M-%")
    parser.add_argument("--start", default=None, help="optional UTC start timestamp, e.g. 2026-07-01 00:00:00")
    parser.add_argument("--end", default=None, help="optional UTC end timestamp, e.g. 2026-07-10 00:00:00")
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
    print("Cheap minority rebound report complete")
    print(f"candidate_csv={paths.candidates_csv}")
    print(f"grouped_summary_csv={paths.grouped_summary_csv}")
    print(f"checkpoint_summary_csv={paths.checkpoint_summary_csv}")
    print(f"context_summary_csv={paths.context_summary_csv}")
    print(f"overshoot_summary_csv={paths.overshoot_summary_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
