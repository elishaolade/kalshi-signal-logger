#!/usr/bin/env python3
"""
Falsification report for one frozen candidate rule:

  First clean fast 65-70c dominance event per market
  -> buy opposing minority contract at ask
  -> sell at executable bid 90 seconds later

This is intentionally not an optimization script. It does not search thresholds,
holding periods, stops, or profit targets.

No production tables are created, updated, deleted, or altered.
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from research_rapid_moderate_dominance import (  # noqa: E402
    _bucket_dominance,
    _clean_quote,
    _dominant_side,
    _fee_cents,
    _first_target,
    _future_path,
    _last_at_or_before,
    _load_rows,
    _mae_before,
    _max_drawdown,
    _median,
    _pct,
    _price,
    _profit_factor,
    _score_trade,
    _summary,
    _speed_bucket,
)

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "falsify_65_70_fast_rebound"

FROZEN_RULE = "frozen_65_70c_fast_90s"


@dataclass(frozen=True)
class Paths:
    directory: Path
    trades_csv: Path
    summary_csv: Path
    violations_csv: Path
    fee_sensitivity_csv: Path
    chronological_split_csv: Path
    day_summary_csv: Path
    distribution_csv: Path
    outlier_sensitivity_csv: Path
    drawdown_curve_csv: Path
    btc_context_csv: Path
    price_distribution_csv: Path
    settlement_csv: Path
    rebound_path_csv: Path
    uncertainty_csv: Path
    markdown_report: Path


def _avg(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 6)


def _percentile(values: Iterable[float | None], pct: float) -> float | None:
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 6)
    pos = pct * (len(vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return round(vals[lo], 6)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo), 6)


def _std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return round(statistics.stdev(values), 6)


def _streaks(values: list[float]) -> tuple[int, int]:
    max_loss = 0
    max_win = 0
    cur_loss = 0
    cur_win = 0
    for value in values:
        if value <= 0:
            cur_loss += 1
            cur_win = 0
        else:
            cur_win += 1
            cur_loss = 0
        max_loss = max(max_loss, cur_loss)
        max_win = max(max_win, cur_win)
    return max_loss, max_win


def _drawdown_curve(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    curve = []
    equity = 0.0
    peak = 0.0
    below_peak_start = None
    longest_below = 0
    for i, row in enumerate(rows, start=1):
        equity = round(equity + row["net_pnl_cents"], 6)
        if equity >= peak:
            peak = equity
            below_peak_start = None
        elif below_peak_start is None:
            below_peak_start = i
        if below_peak_start is not None:
            longest_below = max(longest_below, i - below_peak_start + 1)
        curve.append(
            {
                "trade_num": i,
                "entry_at": row["entry_at"],
                "market_ticker": row["market_ticker"],
                "net_pnl_cents": row["net_pnl_cents"],
                "cumulative_net_pnl_cents": equity,
                "equity_peak_cents": peak,
                "drawdown_cents": round(equity - peak, 6),
                "longest_trades_below_peak_so_far": longest_below,
            }
        )
    return curve


def _longest_below_peak(curve: list[dict[str, Any]]) -> int:
    return max((int(row["longest_trades_below_peak_so_far"]) for row in curve), default=0)


def _one_row_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [row["net_pnl_cents"] for row in rows]
    gross = [row["gross_pnl_cents"] for row in rows]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    loss_streak, win_streak = _streaks(values)
    curve = _drawdown_curve(rows)
    return {
        "trades": len(rows),
        "unique_markets": len({row["market_ticker"] for row in rows}),
        "active_days": len({row["entry_date_et"] for row in rows}),
        "avg_entry_ask": _avg(row["minority_ask"] for row in rows),
        "avg_exit_bid": _avg(row["exit_bid"] for row in rows),
        "avg_gross_pnl_cents": _avg(gross),
        "avg_entry_fee_cents": _avg(row["entry_fee_cents"] for row in rows),
        "avg_exit_fee_cents": _avg(row["exit_fee_cents"] for row in rows),
        "avg_total_fee_cents": _avg(row["total_fee_cents"] for row in rows),
        "min_total_fee_cents": min((row["total_fee_cents"] for row in rows), default=None),
        "max_total_fee_cents": max((row["total_fee_cents"] for row in rows), default=None),
        "avg_gross_to_net_reduction_cents": _avg(row["gross_pnl_cents"] - row["net_pnl_cents"] for row in rows),
        "avg_net_pnl_cents": _avg(values),
        "total_net_pnl_cents": round(sum(values), 6),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": _pct(len(wins), len(rows)),
        "avg_winner_cents": _avg(wins),
        "avg_loser_cents": _avg(losses),
        "median_net_pnl_cents": _median(values),
        "gross_profit_factor": _profit_factor(gross),
        "net_profit_factor": _profit_factor(values),
        "max_net_drawdown_cents": _max_drawdown(values),
        "median_drawdown_cents": _median(row["drawdown_cents"] for row in curve),
        "max_losing_streak": loss_streak,
        "max_winning_streak": win_streak,
        "longest_trades_below_peak": _longest_below_peak(curve),
    }


def _frozen_event(rows: list[dict[str, Any]], max_spread: float) -> dict[str, Any] | None:
    for row in rows:
        elapsed = (row["captured_at"] - row["opens_at"]).total_seconds()
        if elapsed < 0 or elapsed > 300:
            continue
        if not _clean_quote(row, max_spread):
            continue
        dom_side = _dominant_side(row)
        dom_ask = _price(row, dom_side, "ask") if dom_side else None
        if dom_ask is None or not (0.65 <= dom_ask < 0.70):
            continue
        open_row = _last_at_or_before(rows, row["opens_at"] + timedelta(seconds=20)) or rows[0]
        open_dom_ask = _price(open_row, dom_side, "ask")
        if open_dom_ask is None:
            continue
        speed = ((dom_ask - open_dom_ask) * 100.0) / max(1.0, elapsed)
        if _speed_bucket(speed) == "fast":
            return row
    return None


def _build_trades(
    rows_by_market: dict[int, list[dict[str, Any]]],
    max_spread: float,
    fee_rate_cents: float,
    max_exit_delay_seconds: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades = []
    violations = []
    seen = set()
    for market_rows in rows_by_market.values():
        event = _frozen_event(market_rows, max_spread)
        if event is None:
            continue
        scored = _score_trade(
            market_rows,
            event,
            FROZEN_RULE,
            90,
            fee_rate_cents,
            0.0,
            max_exit_delay_seconds,
        )
        if scored is None:
            continue
        scored["row_num"] = len(trades) + 1
        trades.append(scored)
        seen.add(scored["market_ticker"])
        violations.extend(_verify_trade(scored, seen_count=1))
    trades.sort(key=lambda row: row["entry_at"])
    for i, row in enumerate(trades, start=1):
        row["row_num"] = i
    return trades, violations


def _verify_trade(row: dict[str, Any], seen_count: int) -> list[dict[str, Any]]:
    issues = []

    def add(issue: str, detail: str) -> None:
        issues.append({"market_ticker": row["market_ticker"], "entry_at": row["entry_at"], "issue": issue, "detail": detail})

    if not (0.65 <= row["dominant_ask"] < 0.70):
        add("dominant_ask_out_of_range", str(row["dominant_ask"]))
    if row["dominance_speed_bucket"] != "fast":
        add("speed_not_fast", str(row["dominance_speed_bucket"]))
    if row["time_since_open_seconds"] > 300:
        add("entry_after_first_5m", str(row["time_since_open_seconds"]))
    if _bucket_dominance(row["dominant_ask"]) != "65-70c":
        add("dominance_bucket_mismatch", str(row["dominance_bucket"]))
    if row["exit_delay_seconds"] < 0 or row["exit_delay_seconds"] > 5:
        add("exit_delay_violation", str(row["exit_delay_seconds"]))
    if seen_count != 1:
        add("duplicate_market", str(seen_count))
    return issues


def _fee_sensitivity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for label, mult in (("75pct_fee", 0.75), ("100pct_fee", 1.0), ("125pct_fee", 1.25)):
        adjusted = []
        for row in rows:
            net = row["gross_pnl_cents"] - row["total_fee_cents"] * mult
            adjusted.append(round(net, 6))
        summary = {
            "fee_scenario": label,
            "fee_multiplier": mult,
            "trades": len(rows),
            "avg_net_pnl_cents": _avg(adjusted),
            "total_net_pnl_cents": round(sum(adjusted), 6),
            "net_profit_factor": _profit_factor(adjusted),
            "max_net_drawdown_cents": _max_drawdown(adjusted),
        }
        out.append(summary)
    return out


def _chronological_splits(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    n = len(rows)
    cohorts = {
        "first_25pct": rows[: n // 4],
        "second_25pct": rows[n // 4: n // 2],
        "third_25pct": rows[n // 2: (3 * n) // 4],
        "final_25pct": rows[(3 * n) // 4:],
        "first_half": rows[: n // 2],
        "second_half": rows[n // 2:],
    }
    out = []
    for label, group in cohorts.items():
        item = {"period": label}
        item.update(_one_row_summary(group))
        out.append(item)
    return out


def _day_summary(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    daily = _summary(rows, ("entry_date_et",))
    for row in daily:
        row["date"] = row.pop("entry_date_et")
    profitable = [row for row in daily if row["total_net_pnl_cents"] > 0]
    losing = [row for row in daily if row["total_net_pnl_cents"] < 0]
    flat = [row for row in daily if row["total_net_pnl_cents"] == 0]
    sorted_days = sorted(daily, key=lambda row: row["total_net_pnl_cents"], reverse=True)
    total = sum(row["total_net_pnl_cents"] for row in daily)
    meta = {
        "active_days": len(daily),
        "profitable_days": len(profitable),
        "losing_days": len(losing),
        "flat_days": len(flat),
        "profitable_days_pct": _pct(len(profitable), len(daily)),
        "best_day": sorted_days[0]["date"] if sorted_days else None,
        "best_day_pnl_cents": sorted_days[0]["total_net_pnl_cents"] if sorted_days else None,
        "worst_day": sorted_days[-1]["date"] if sorted_days else None,
        "worst_day_pnl_cents": sorted_days[-1]["total_net_pnl_cents"] if sorted_days else None,
        "total_net_pnl_cents": round(total, 6),
        "total_excluding_best_day_cents": round(total - (sorted_days[0]["total_net_pnl_cents"] if sorted_days else 0), 6),
        "total_excluding_best_two_days_cents": round(total - sum(row["total_net_pnl_cents"] for row in sorted_days[:2]), 6),
    }
    daily.insert(0, {"date": "__SUMMARY__", **meta})
    return daily, meta


def _distribution(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    vals = [row["net_pnl_cents"] for row in rows]
    return [
        {
            "metric": "net_pnl_distribution",
            "trades": len(vals),
            "min": min(vals, default=None),
            "p10": _percentile(vals, 0.10),
            "p25": _percentile(vals, 0.25),
            "median": _median(vals),
            "p75": _percentile(vals, 0.75),
            "p90": _percentile(vals, 0.90),
            "p95": _percentile(vals, 0.95),
            "max": max(vals, default=None),
            "pct_gt_0": _pct(sum(v > 0 for v in vals), len(vals)),
            "pct_gt_1c": _pct(sum(v > 1 for v in vals), len(vals)),
            "pct_gt_3c": _pct(sum(v > 3 for v in vals), len(vals)),
            "pct_gt_5c": _pct(sum(v > 5 for v in vals), len(vals)),
            "pct_lt_minus_1c": _pct(sum(v < -1 for v in vals), len(vals)),
            "pct_lt_minus_3c": _pct(sum(v < -3 for v in vals), len(vals)),
            "pct_lt_minus_5c": _pct(sum(v < -5 for v in vals), len(vals)),
            "pct_lt_minus_10c": _pct(sum(v < -10 for v in vals), len(vals)),
        }
    ]


def _outlier_sensitivity(rows: list[dict[str, Any]], daily_meta: dict[str, Any]) -> list[dict[str, Any]]:
    variants = {
        "raw": rows,
        "remove_largest_winner": _remove_top_winners(rows, 1),
        "remove_top_1pct_winners": _remove_top_winners(rows, max(1, math.ceil(len(rows) * 0.01))),
        "remove_top_5pct_winners": _remove_top_winners(rows, max(1, math.ceil(len(rows) * 0.05))),
        "winsorize_99pct": _winsorize(rows, 0.99),
        "remove_best_day": [row for row in rows if row["entry_date_et"] != daily_meta.get("best_day")],
    }
    sorted_days = sorted(
        defaultdict(float, _day_totals(rows)).items(),
        key=lambda item: item[1],
        reverse=True,
    )
    best_two = {day for day, _ in sorted_days[:2]}
    variants["remove_best_two_days"] = [row for row in rows if row["entry_date_et"] not in best_two]

    out = []
    for name, group in variants.items():
        item = {"variant": name}
        item.update(_one_row_summary(group))
        out.append(item)
    return out


def _day_totals(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[row["entry_date_et"]] += row["net_pnl_cents"]
    return totals


def _remove_top_winners(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    winners = sorted([row for row in rows if row["net_pnl_cents"] > 0], key=lambda row: row["net_pnl_cents"], reverse=True)
    remove = {id(row) for row in winners[:count]}
    return [row for row in rows if id(row) not in remove]


def _winsorize(rows: list[dict[str, Any]], pct: float) -> list[dict[str, Any]]:
    cap = _percentile((row["net_pnl_cents"] for row in rows), pct)
    if cap is None:
        return list(rows)
    out = []
    for row in rows:
        item = dict(row)
        if item["net_pnl_cents"] > cap:
            delta = item["net_pnl_cents"] - cap
            item["net_pnl_cents"] = cap
            item["gross_pnl_cents"] = round(item["gross_pnl_cents"] - delta, 6)
        out.append(item)
    return out


def _btc_context(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cohorts = {
        "net_winners": [row for row in rows if row["net_pnl_cents"] > 0],
        "net_losers": [row for row in rows if row["net_pnl_cents"] <= 0],
    }
    fields = (
        "btc_move_since_open_dominant_side",
        "btc_move_prev_10s_dominant_side",
        "btc_move_prev_30s_dominant_side",
        "btc_move_prev_60s_dominant_side",
        "btc_distance_dominant_side",
        "abs_btc_distance",
        "btc_volatility_60s",
        "dominant_cents_per_second",
    )
    out = []
    for label, group in cohorts.items():
        row = {"cohort": label, "trades": len(group)}
        for field in fields:
            row[f"avg_{field}"] = _avg(item.get(field) for item in group)
            row[f"median_{field}"] = _median(item.get(field) for item in group)
        out.append(row)
    return out


def _price_distribution(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "dominant_ask",
        "minority_ask",
        "minority_spread",
        "exit_bid",
        "gross_pnl_cents",
        "net_pnl_cents",
    )
    out = []
    for field in fields:
        vals = [row.get(field) for row in rows]
        out.append(
            {
                "field": field,
                "min": min((v for v in vals if v is not None), default=None),
                "p10": _percentile(vals, 0.10),
                "p25": _percentile(vals, 0.25),
                "median": _median(vals),
                "p75": _percentile(vals, 0.75),
                "p90": _percentile(vals, 0.90),
                "max": max((v for v in vals if v is not None), default=None),
            }
        )
    return out


def _settlement_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def label(row: dict[str, Any]) -> str:
        if row["minority_settled_winner"] is None:
            return "settlement_unknown"
        return "minority_settled_1" if row["minority_settled_winner"] else "minority_settled_0"

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[label(row)].append(row)
    out = []
    for group_label, group in grouped.items():
        item = {"settlement_group": group_label}
        item.update(_one_row_summary(group))
        out.append(item)
    return out


def _rebound_path(rows: list[dict[str, Any]], rows_by_market: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    enriched = []
    by_ticker = {
        market_rows[0]["market_ticker"]: market_rows
        for market_rows in rows_by_market.values()
        if market_rows
    }
    profitable_30 = profitable_60 = profitable_90 = 0
    for row in rows:
        if row["net_pnl_cents"] > 0:
            profitable_90 += 1
        market_rows = by_ticker.get(row["market_ticker"])
        if not market_rows:
            continue
        entry_ts = next((r["captured_at"] for r in market_rows if _iso(r["captured_at"]) == row["entry_at"]), None)
        if entry_ts is None:
            continue
        side = row["minority_side"]
        path = _future_path(market_rows, entry_ts, side, market_rows[0]["closes_at"])
        t30 = _first_exit_gross(path, row["minority_ask"], entry_ts, 30)
        t60 = _first_exit_gross(path, row["minority_ask"], entry_ts, 60)
        profitable_30 += int(t30 is not None and t30 > 0)
        profitable_60 += int(t60 is not None and t60 > 0)
    return [
        {
            "trades": len(rows),
            "target_1c_rate": _pct(sum(row["target_1c_hit"] for row in rows), len(rows)),
            "target_2c_rate": _pct(sum(row["target_2c_hit"] for row in rows), len(rows)),
            "target_3c_rate": _pct(sum(row["target_3c_hit"] for row in rows), len(rows)),
            "target_5c_rate": _pct(sum(row["target_5c_hit"] for row in rows), len(rows)),
            "target_10c_rate": _pct(sum(row["target_10c_hit"] for row in rows), len(rows)),
            "median_time_to_3c": _median(row["seconds_to_3c"] for row in rows),
            "median_time_to_5c": _median(row["seconds_to_5c"] for row in rows),
            "median_mae_before_3c": _median(row["mae_before_3c_cents"] for row in rows if row["target_3c_hit"]),
            "median_mae_before_5c": _median(row["mae_before_5c_cents"] for row in rows if row["target_5c_hit"]),
            "profitable_at_30s": profitable_30,
            "profitable_at_60s": profitable_60,
            "profitable_at_90s": profitable_90,
        }
    ]


def _first_exit_gross(path: list[dict[str, Any]], entry_ask: float, entry_ts, seconds: int) -> float | None:
    target = entry_ts + timedelta(seconds=seconds)
    row = next((p for p in path if p["captured_at"] >= target), None)
    if row is None:
        return None
    return round((row["bid"] - entry_ask) * 100.0, 6)


def _uncertainty(rows: list[dict[str, Any]], bootstrap_samples: int) -> list[dict[str, Any]]:
    vals = [row["net_pnl_cents"] for row in rows]
    n = len(vals)
    mean = statistics.mean(vals) if vals else 0.0
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    se = sd / math.sqrt(n) if n else None
    ci_low = mean - 1.96 * se if se is not None else None
    ci_high = mean + 1.96 * se if se is not None else None
    rng = random.Random(1776)
    boot = []
    if vals and bootstrap_samples > 0:
        for _ in range(bootstrap_samples):
            sample = [rng.choice(vals) for _ in vals]
            boot.append(statistics.mean(sample))
    return [
        {
            "metric": "net_pnl_mean",
            "trades": n,
            "mean_net_pnl_cents": round(mean, 6),
            "stddev_net_pnl_cents": round(sd, 6),
            "standard_error": round(se, 6) if se is not None else None,
            "normal_ci95_low": round(ci_low, 6) if ci_low is not None else None,
            "normal_ci95_high": round(ci_high, 6) if ci_high is not None else None,
            "bootstrap_samples": bootstrap_samples,
            "bootstrap_ci95_low": _percentile(boot, 0.025),
            "bootstrap_ci95_high": _percentile(boot, 0.975),
            "normal_ci_includes_zero": int(ci_low <= 0 <= ci_high) if ci_low is not None and ci_high is not None else None,
            "bootstrap_ci_includes_zero": int((_percentile(boot, 0.025) or 0) <= 0 <= (_percentile(boot, 0.975) or 0)) if boot else None,
        }
    ]


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


def _table(rows: list[dict[str, Any]], cols: list[str], limit: int = 20) -> str:
    if not rows:
        return "_No rows._"
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in cols) + " |")
    return "\n".join(lines)


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"falsify_65_70_fast_rebound_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        trades_csv=output_dir / f"{stem}_trades.csv",
        summary_csv=output_dir / f"{stem}_summary.csv",
        violations_csv=output_dir / f"{stem}_violations.csv",
        fee_sensitivity_csv=output_dir / f"{stem}_fee_sensitivity.csv",
        chronological_split_csv=output_dir / f"{stem}_chronological_split.csv",
        day_summary_csv=output_dir / f"{stem}_day_summary.csv",
        distribution_csv=output_dir / f"{stem}_distribution.csv",
        outlier_sensitivity_csv=output_dir / f"{stem}_outlier_sensitivity.csv",
        drawdown_curve_csv=output_dir / f"{stem}_drawdown_curve.csv",
        btc_context_csv=output_dir / f"{stem}_btc_context.csv",
        price_distribution_csv=output_dir / f"{stem}_price_distribution.csv",
        settlement_csv=output_dir / f"{stem}_settlement.csv",
        rebound_path_csv=output_dir / f"{stem}_rebound_path.csv",
        uncertainty_csv=output_dir / f"{stem}_uncertainty.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def build_report(
    output_dir: Path,
    market_like: str,
    start: str | None,
    end: str | None,
    max_spread: float,
    fee_rate_cents: float,
    max_exit_delay_seconds: int,
    bootstrap_samples: int,
) -> Paths:
    paths = _paths(output_dir)
    raw_rows = _load_rows(market_like, start, end)
    rows_by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        rows_by_market[int(row["market_pk"])].append(row)
    for rows in rows_by_market.values():
        rows.sort(key=lambda row: row["captured_at"])

    trades, violations = _build_trades(rows_by_market, max_spread, fee_rate_cents, max_exit_delay_seconds)
    summary = [{"rule_name": FROZEN_RULE, **_one_row_summary(trades)}]
    fee_sensitivity = _fee_sensitivity(trades)
    chronological = _chronological_splits(trades)
    day_rows, day_meta = _day_summary(trades)
    distribution = _distribution(trades)
    outlier = _outlier_sensitivity(trades, day_meta)
    curve = _drawdown_curve(trades)
    btc_context = _btc_context(trades)
    price_distribution = _price_distribution(trades)
    settlement = _settlement_summary(trades)
    rebound = _rebound_path(trades, rows_by_market)
    uncertainty = _uncertainty(trades, bootstrap_samples)

    _write_csv(paths.trades_csv, trades)
    _write_csv(paths.summary_csv, summary)
    _write_csv(paths.violations_csv, violations)
    _write_csv(paths.fee_sensitivity_csv, fee_sensitivity)
    _write_csv(paths.chronological_split_csv, chronological)
    _write_csv(paths.day_summary_csv, day_rows)
    _write_csv(paths.distribution_csv, distribution)
    _write_csv(paths.outlier_sensitivity_csv, outlier)
    _write_csv(paths.drawdown_curve_csv, curve)
    _write_csv(paths.btc_context_csv, btc_context)
    _write_csv(paths.price_distribution_csv, price_distribution)
    _write_csv(paths.settlement_csv, settlement)
    _write_csv(paths.rebound_path_csv, rebound)
    _write_csv(paths.uncertainty_csv, uncertainty)
    paths.markdown_report.write_text(_render_markdown(paths, summary, violations, fee_sensitivity, chronological, day_rows, outlier, uncertainty))
    return paths


def _render_markdown(
    paths: Paths,
    summary: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    fee_sensitivity: list[dict[str, Any]],
    chronological: list[dict[str, Any]],
    day_rows: list[dict[str, Any]],
    outlier: list[dict[str, Any]],
    uncertainty: list[dict[str, Any]],
) -> str:
    s = summary[0] if summary else {}
    u = uncertainty[0] if uncertainty else {}
    expected_reproduced = s.get("trades") in (120, 121, 122)
    survives = (
        expected_reproduced
        and not violations
        and (s.get("avg_net_pnl_cents") or 0) > 0
        and (s.get("net_profit_factor") == "inf" or float(s.get("net_profit_factor") or 0) > 1)
        and not (u.get("normal_ci_includes_zero") == 1 and u.get("bootstrap_ci_includes_zero") == 1)
    )
    classification = (
        "C. Survives historical falsification enough for prospective simulation"
        if survives
        else "A/B. Fails or remains historically unstable; inspect stability tables before prospective TEST"
    )
    return f"""# Falsification Report: 65-70c Fast Dominance Minority Rebound

## Frozen Rule

- Market: Kalshi BTC 15-minute contracts
- Window: first 5 minutes after open
- Dominant ask: 65c to <70c
- Speed: fast, defined as >=0.15 and <0.30 cents/second from market open
- Entry: buy minority at executable ask
- Exit: sell minority at first valid executable bid at/after 90s, max 5s delay
- Clean quotes only
- One entry per market
- No stop, no target, no optimization

## Reproduction

{_table(summary, ["rule_name", "trades", "unique_markets", "active_days", "avg_entry_ask", "avg_exit_bid", "avg_gross_pnl_cents", "avg_total_fee_cents", "avg_net_pnl_cents", "total_net_pnl_cents", "win_rate_pct", "median_net_pnl_cents", "net_profit_factor", "max_net_drawdown_cents", "max_losing_streak"])}

Expected previous cohort: 121 trades, avg gross +3.824c, avg net +0.7526c, net PF 1.1359.
Near reproduction: `{expected_reproduced}`.

## Entry Logic Violations

Violations found: `{len(violations)}`.

{_table(violations, ["market_ticker", "entry_at", "issue", "detail"], 20)}

## Fee Sensitivity

{_table(fee_sensitivity, ["fee_scenario", "fee_multiplier", "trades", "avg_net_pnl_cents", "total_net_pnl_cents", "net_profit_factor", "max_net_drawdown_cents"])}

## Chronological Stability

{_table(chronological, ["period", "trades", "active_days", "win_rate_pct", "avg_gross_pnl_cents", "avg_net_pnl_cents", "median_net_pnl_cents", "net_profit_factor", "total_net_pnl_cents", "max_net_drawdown_cents"])}

## Day Stability Summary

{_table(day_rows[:1], ["date", "active_days", "profitable_days", "losing_days", "flat_days", "profitable_days_pct", "best_day", "best_day_pnl_cents", "worst_day", "worst_day_pnl_cents", "total_excluding_best_day_cents", "total_excluding_best_two_days_cents"])}

## Outlier Sensitivity

{_table(outlier, ["variant", "trades", "avg_net_pnl_cents", "total_net_pnl_cents", "net_profit_factor", "max_net_drawdown_cents"])}

## Statistical Uncertainty

{_table(uncertainty, ["metric", "trades", "mean_net_pnl_cents", "stddev_net_pnl_cents", "standard_error", "normal_ci95_low", "normal_ci95_high", "bootstrap_ci95_low", "bootstrap_ci95_high", "normal_ci_includes_zero", "bootstrap_ci_includes_zero"])}

## Classification

`{classification}`

If this is class C, the next step is exactly 30 consecutive prospective simulated trades using the frozen rule. Do not combine prospective results with this historical set.

## Output Files

- Trades CSV: `{paths.trades_csv}`
- Summary CSV: `{paths.summary_csv}`
- Violations CSV: `{paths.violations_csv}`
- Fee sensitivity CSV: `{paths.fee_sensitivity_csv}`
- Chronological split CSV: `{paths.chronological_split_csv}`
- Day summary CSV: `{paths.day_summary_csv}`
- Distribution CSV: `{paths.distribution_csv}`
- Outlier sensitivity CSV: `{paths.outlier_sensitivity_csv}`
- Drawdown curve CSV: `{paths.drawdown_curve_csv}`
- BTC context CSV: `{paths.btc_context_csv}`
- Price distribution CSV: `{paths.price_distribution_csv}`
- Settlement CSV: `{paths.settlement_csv}`
- Rebound path CSV: `{paths.rebound_path_csv}`
- Uncertainty CSV: `{paths.uncertainty_csv}`
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Falsify frozen 65-70c fast dominance rebound rule")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M-%")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-spread", type=float, default=0.02)
    parser.add_argument("--fee-rate-cents", type=float, default=7.0)
    parser.add_argument("--max-exit-delay-seconds", type=int, default=5)
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    args = parser.parse_args()

    paths = build_report(
        output_dir=args.output_dir,
        market_like=args.market_like,
        start=args.start,
        end=args.end,
        max_spread=args.max_spread,
        fee_rate_cents=args.fee_rate_cents,
        max_exit_delay_seconds=args.max_exit_delay_seconds,
        bootstrap_samples=args.bootstrap_samples,
    )
    print("Falsification report complete")
    print(f"trades_csv={paths.trades_csv}")
    print(f"summary_csv={paths.summary_csv}")
    print(f"violations_csv={paths.violations_csv}")
    print(f"fee_sensitivity_csv={paths.fee_sensitivity_csv}")
    print(f"chronological_split_csv={paths.chronological_split_csv}")
    print(f"day_summary_csv={paths.day_summary_csv}")
    print(f"distribution_csv={paths.distribution_csv}")
    print(f"outlier_sensitivity_csv={paths.outlier_sensitivity_csv}")
    print(f"drawdown_curve_csv={paths.drawdown_curve_csv}")
    print(f"btc_context_csv={paths.btc_context_csv}")
    print(f"price_distribution_csv={paths.price_distribution_csv}")
    print(f"settlement_csv={paths.settlement_csv}")
    print(f"rebound_path_csv={paths.rebound_path_csv}")
    print(f"uncertainty_csv={paths.uncertainty_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
