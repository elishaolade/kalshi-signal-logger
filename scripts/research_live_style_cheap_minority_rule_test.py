#!/usr/bin/env python3
"""
Live-style one-cheap-minority-contract-per-day rule test for 08:00-11:00 ET.

This is a falsification report. It scans chronologically and takes only the
first valid pre-entry signal per ET calendar day per frozen rule. It does not
select the best opportunity after seeing future prices.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

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
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "live_style_cheap_minority_rule_test"
EXIT_HORIZONS: tuple[int | str, ...] = (60, 120, 180, "close")


@dataclass(frozen=True)
class RuleConfig:
    rule_name: str
    description: str
    threshold_summary: str


@dataclass(frozen=True)
class Paths:
    directory: Path
    trade_csv: Path
    daily_pnl_csv: Path
    summary_csv: Path
    skipped_days_csv: Path
    rule_config_csv: Path
    data_quality_csv: Path
    descriptive_comparison_csv: Path
    markdown_report: Path


RULE_CONFIGS = (
    RuleConfig(
        "rule_a_first_eligible",
        "First clean 0.20-0.30 minority ask in 08:00-11:00 ET within first 120s after market open.",
        "minority_ask >= 0.20 AND minority_ask < 0.30",
    ),
    RuleConfig(
        "rule_b_countertrend",
        "First Rule A setup where minority side is against BTC 60-second direction.",
        "Rule A + btc_60s_direction exists + minority_side != btc_60s_direction",
    ),
    RuleConfig(
        "rule_c_btc_impulse",
        "First Rule A setup after absolute BTC 60-second move >= $50.",
        "Rule A + ABS(btc_move_60s) >= 50",
    ),
    RuleConfig(
        "rule_d_countertrend_btc_impulse",
        "First Rule A setup after absolute BTC 60-second move >= $50 and minority side is countertrend.",
        "Rule A + ABS(btc_move_60s) >= 50 + minority_side != btc_60s_direction",
    ),
    RuleConfig(
        "rule_e_first_30s",
        "First Rule A setup within first 30 seconds after market open.",
        "Rule A + seconds_since_market_open <= 30",
    ),
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(sep=" ")


def _iso_et(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(ET).replace(tzinfo=None).isoformat(sep=" ")


def _last_at_or_before(rows: list[dict[str, Any]], ts: datetime) -> dict[str, Any] | None:
    out = None
    for row in rows:
        if row["captured_at"] <= ts:
            out = row
        else:
            break
    return out


def _first_at_or_after(rows: list[dict[str, Any]], ts: datetime, tolerance_seconds: int) -> dict[str, Any] | None:
    max_ts = ts + timedelta(seconds=tolerance_seconds)
    for row in rows:
        if ts <= row["captured_at"] <= max_ts:
            return row
    return None


def _last_clean_before_close(rows: list[dict[str, Any]], side: str, entry_at: datetime, close_at: datetime, max_spread: float) -> dict[str, Any] | None:
    out = None
    for row in rows:
        if row["captured_at"] <= entry_at:
            continue
        if row["captured_at"] > close_at:
            break
        if _is_clean_side(row, side, max_spread):
            out = row
    return out


def _btc_direction(delta: float | None) -> str | None:
    if delta is None or delta == 0:
        return None
    return "YES" if delta > 0 else "NO"


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _base_candidate(row: dict[str, Any], market_rows: list[dict[str, Any]], max_spread: float) -> dict[str, Any] | None:
    if not _clean_row(row, max_spread):
        return None
    entry_at = row["captured_at"]
    seconds_since_open = (entry_at - row["opens_at"]).total_seconds()
    if seconds_since_open < 0 or seconds_since_open > 120:
        return None
    entry_et = entry_at.astimezone(ET)
    if not (8 <= entry_et.hour < 11):
        return None

    dominant = _dominant_side(row)
    if dominant is None:
        return None
    minority = _minority_side(dominant)
    entry_ask = _price(row, minority, "ask")
    entry_bid = _price(row, minority, "bid")
    entry_spread = _price(row, minority, "spread")
    if entry_ask is None or entry_bid is None or entry_spread is None:
        return None
    if not (0.20 <= entry_ask < 0.30):
        return None
    if not _is_clean_side(row, minority, max_spread):
        return None

    prev60 = _last_at_or_before(market_rows, entry_at - timedelta(seconds=60))
    btc_price_60s_before = prev60["btc_price"] if prev60 else None
    btc_move_60s = row["btc_price"] - btc_price_60s_before if btc_price_60s_before is not None else None
    btc_dir = _btc_direction(btc_move_60s)
    aligned = None if btc_dir is None else int(minority == btc_dir)

    return {
        "market_pk": row["market_pk"],
        "market_ticker": row["market_ticker"],
        "date_et": entry_et.date().isoformat(),
        "weekday": entry_et.strftime("%A"),
        "is_weekend": int(entry_et.weekday() >= 5),
        "market_open_timestamp_et": _iso_et(row["opens_at"]),
        "market_close_timestamp_et": _iso_et(row["closes_at"]),
        "entry_timestamp_et": _iso_et(entry_at),
        "entry_at": entry_at,
        "seconds_since_market_open": _round(seconds_since_open, 3),
        "contract_side": minority,
        "minority_side": minority,
        "dominant_side": dominant,
        "btc_price_60s_before_entry": _round(btc_price_60s_before, 2),
        "btc_price_at_entry": _round(row["btc_price"], 2),
        "btc_60s_move": _round(btc_move_60s, 2),
        "btc_abs_60s_move": _round(abs(btc_move_60s), 2) if btc_move_60s is not None else None,
        "btc_60s_direction": btc_dir,
        "minority_aligned_with_btc_60s_direction": aligned,
        "entry_bid": _round(entry_bid),
        "entry_ask": _round(entry_ask),
        "entry_spread": _round(entry_spread),
    }


def _candidate_matches_rule(candidate: dict[str, Any], rule_name: str, btc_impulse_threshold: float) -> bool:
    if rule_name == "rule_a_first_eligible":
        return True
    if rule_name == "rule_b_countertrend":
        return candidate["minority_aligned_with_btc_60s_direction"] == 0
    if rule_name == "rule_c_btc_impulse":
        return candidate["btc_abs_60s_move"] is not None and candidate["btc_abs_60s_move"] >= btc_impulse_threshold
    if rule_name == "rule_d_countertrend_btc_impulse":
        return (
            candidate["btc_abs_60s_move"] is not None
            and candidate["btc_abs_60s_move"] >= btc_impulse_threshold
            and candidate["minority_aligned_with_btc_60s_direction"] == 0
        )
    if rule_name == "rule_e_first_30s":
        return candidate["seconds_since_market_open"] is not None and candidate["seconds_since_market_open"] <= 30
    raise ValueError(f"unknown rule {rule_name}")


def _find_daily_signal(
    day_rows: list[dict[str, Any]],
    rows_by_market: dict[int, list[dict[str, Any]]],
    rule_name: str,
    max_spread: float,
    btc_impulse_threshold: float,
) -> dict[str, Any] | None:
    for row in sorted(day_rows, key=lambda item: (item["captured_at"], item["market_ticker"])):
        market_rows = rows_by_market[int(row["market_pk"])]
        candidate = _base_candidate(row, market_rows, max_spread)
        if candidate and _candidate_matches_rule(candidate, rule_name, btc_impulse_threshold):
            candidate["rule_name"] = rule_name
            return candidate
    return None


def _score_signal(
    signal: dict[str, Any],
    market_rows: list[dict[str, Any]],
    exit_horizon: int | str,
    max_spread: float,
    fee_rate_cents: float,
    exit_tolerance_seconds: int,
) -> dict[str, Any]:
    side = signal["contract_side"]
    entry_at = signal["entry_at"]
    entry_ask = signal["entry_ask"]
    if isinstance(exit_horizon, int):
        exit_row = _first_at_or_after(
            [row for row in market_rows if row["captured_at"] >= entry_at and _is_clean_side(row, side, max_spread)],
            entry_at + timedelta(seconds=exit_horizon),
            exit_tolerance_seconds,
        )
    else:
        exit_row = _last_clean_before_close(market_rows, side, entry_at, market_rows[0]["closes_at"], max_spread)

    row = {
        key: value
        for key, value in signal.items()
        if key not in {"entry_at", "market_pk"}
    }
    row.update({"exit_horizon": str(exit_horizon), "first_valid_signal_of_day": 1})

    if exit_row is None:
        row.update(
            {
                "status": "NO_VALID_EXIT",
                "exit_timestamp_et": None,
                "exit_bid": None,
                "gross_cents": None,
                "fee_cents": None,
                "net_cents": None,
                "win_loss_flag": None,
                "holding_seconds": None,
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
            "gross_cents": gross,
            "fee_cents": fee,
            "net_cents": net,
            "win_loss_flag": "win" if net > 0 else "loss",
            "holding_seconds": _round((exit_row["captured_at"] - entry_at).total_seconds(), 3),
            "running_total_net_cents": None,
            "running_drawdown_cents": None,
        }
    )
    return row


def _add_running_pnl(trades: list[dict[str, Any]]) -> None:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        by_key[(row["rule_name"], row["exit_horizon"])].append(row)
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


def _observed_entry_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in raw_rows:
        et = row["captured_at"].astimezone(ET)
        seconds_since_open = (row["captured_at"] - row["opens_at"]).total_seconds()
        if 8 <= et.hour < 11 and 0 <= seconds_since_open <= 120:
            out.append(row)
    return out


def _summarize(trades: list[dict[str, Any]], observed_days: list[str]) -> list[dict[str, Any]]:
    out = []
    first_half_dates = set(observed_days[: len(observed_days) // 2])
    second_half_dates = set(observed_days[len(observed_days) // 2 :])
    for config in RULE_CONFIGS:
        for horizon in EXIT_HORIZONS:
            horizon_s = str(horizon)
            group = [row for row in trades if row["rule_name"] == config.rule_name and row["exit_horizon"] == horizon_s]
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
            out.append(
                {
                    "rule_name": config.rule_name,
                    "exit_horizon": horizon_s,
                    "calendar_days_observed": len(observed_days),
                    "days_with_trade": days_with_trade,
                    "days_skipped": len(observed_days) - days_with_trade,
                    "trade_count": len(complete),
                    "no_valid_exit_trades": sum(row["status"] == "NO_VALID_EXIT" for row in group),
                    "win_rate_pct": _pct(sum(v > 0 for v in nets), len(nets)),
                    "average_gross_cents_per_trade": _avg(gross),
                    "average_fee_cents_per_trade": _avg(fees),
                    "average_net_cents_per_trade": _avg(nets),
                    "median_net_cents_per_trade": _median(nets),
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
    by_key = {(row["rule_name"], row["exit_horizon"], row["date_et"]): row for row in trades}
    out = []
    for config in RULE_CONFIGS:
        for horizon in EXIT_HORIZONS:
            total = 0.0
            peak = 0.0
            for day in observed_days:
                row = by_key.get((config.rule_name, str(horizon), day))
                if row is None:
                    out.append(
                        {
                            "date_et": day,
                            "rule_name": config.rule_name,
                            "exit_horizon": str(horizon),
                            "status": "NO_TRADE",
                            "net_cents": None,
                            "running_total_net_cents": total,
                            "running_drawdown_cents": round(total - peak, 4),
                        }
                    )
                    continue
                if row["status"] == "COMPLETE" and row["net_cents"] is not None:
                    total = round(total + row["net_cents"], 4)
                    peak = max(peak, total)
                out.append(
                    {
                        "date_et": day,
                        "rule_name": config.rule_name,
                        "exit_horizon": str(horizon),
                        "status": row["status"],
                        "market_ticker": row["market_ticker"],
                        "net_cents": row["net_cents"],
                        "gross_cents": row["gross_cents"],
                        "running_total_net_cents": total,
                        "running_drawdown_cents": round(total - peak, 4),
                    }
                )
    return out


def _skipped_days(trades: list[dict[str, Any]], observed_days: list[str]) -> list[dict[str, Any]]:
    traded = {(row["rule_name"], row["date_et"]) for row in trades}
    out = []
    for config in RULE_CONFIGS:
        for day in observed_days:
            if (config.rule_name, day) not in traded:
                out.append(
                    {
                        "date_et": day,
                        "rule_name": config.rule_name,
                        "reason_skipped": "no_first_clean_0.20_0.30_minority_signal_matching_rule",
                    }
                )
    return out


def _rule_config_rows(btc_impulse_threshold: float, max_spread: float, fee_rate_cents: float) -> list[dict[str, Any]]:
    return [
        {
            "rule_name": config.rule_name,
            "description": config.description,
            "threshold_summary": config.threshold_summary,
            "time_window_et": "08:00-11:00",
            "market_lifecycle_entry_window_seconds": "0-120",
            "entry_ask_bucket": "0.20 <= ask < 0.30",
            "contract_type": "minority_side_only",
            "max_trades_per_et_day": 1,
            "entry_spread_max": max_spread,
            "fee_model_cents_per_side": f"{fee_rate_cents} * price * (1 - price)",
            "btc_impulse_threshold": btc_impulse_threshold if "impulse" in config.rule_name else None,
        }
        for config in RULE_CONFIGS
    ]


def _data_quality(raw_rows: list[dict[str, Any]], entry_rows: list[dict[str, Any]], max_spread: float) -> list[dict[str, Any]]:
    return [
        {"issue": "raw_rows_loaded", "rows_affected": len(raw_rows)},
        {"issue": "entry_window_rows_loaded", "rows_affected": len(entry_rows)},
        {"issue": "entry_window_clean_rows", "rows_affected": sum(_clean_row(row, max_spread) for row in entry_rows)},
        {"issue": "quote_age_ms_unavailable_in_historical_snapshots", "rows_affected": len(raw_rows)},
        {"issue": "raw_yes_bid_gt_ask", "rows_affected": sum(row.get("yes_bid") is not None and row.get("yes_ask") is not None and row["yes_bid"] > row["yes_ask"] for row in raw_rows)},
        {"issue": "raw_no_bid_gt_ask", "rows_affected": sum(row.get("no_bid") is not None and row.get("no_ask") is not None and row["no_bid"] > row["no_ask"] for row in raw_rows)},
        {"issue": "raw_yes_spread_eq_zero", "rows_affected": sum(row.get("yes_spread") == 0 for row in raw_rows)},
        {"issue": "raw_no_spread_eq_zero", "rows_affected": sum(row.get("no_spread") == 0 for row in raw_rows)},
    ]


def _descriptive_comparison(output_dir: Path) -> list[dict[str, Any]]:
    report_dir = output_dir.parent / "cheap_contract_rebound_probability"
    matches = sorted(report_dir.glob("*_bucket_summary.csv"))
    if not matches:
        return [{"comparison": "descriptive_probability_report", "status": "not_found"}]
    latest = matches[-1]
    with latest.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    selected = [
        row
        for row in rows
        if row.get("version") == "B_market_side_first"
        and row.get("entry_bucket") == "0.20-0.30"
        and row.get("breakdown_type") in {"all", "btc_60s_alignment", "minority_status"}
    ]
    out = []
    for row in selected:
        out.append(
            {
                "comparison": "descriptive_probability_report",
                "source_csv": str(latest),
                "entry_bucket": row.get("entry_bucket"),
                "breakdown_type": row.get("breakdown_type"),
                "breakdown_value": row.get("breakdown_value"),
                "candidate_count": row.get("candidate_count"),
                "plus_10c_hit_rate_60s": row.get("plus_10c_hit_rate_60s"),
                "plus_10c_hit_rate_120s": row.get("plus_10c_hit_rate_120s"),
                "plus_10c_hit_rate_180s": row.get("plus_10c_hit_rate_180s"),
                "plus_10c_hit_rate_before_close": row.get("plus_10c_hit_rate_before_close"),
                "note": "descriptive rebound probability; not one-trade-per-day live-style selection",
            }
        )
    return out or [{"comparison": "descriptive_probability_report", "status": "no_matching_0.20_0.30_rows", "source_csv": str(latest)}]


def _interpret(summary: list[dict[str, Any]]) -> str:
    complete = [row for row in summary if row.get("trade_count", 0) and row.get("average_net_cents_per_trade") is not None]
    positive = [row for row in complete if row["average_net_cents_per_trade"] > 0]
    if not positive:
        return "No frozen pre-entry rule produced positive average net cents after fees. The descriptive rebound behavior did not translate into a live-style one-trade-per-day edge."
    best = max(positive, key=lambda row: (row["average_net_cents_per_trade"], row["trade_count"]))
    close_best = [row for row in positive if row["exit_horizon"] == "close"]
    short_positive = [row for row in positive if row["exit_horizon"] in {"60", "120"}]
    behavior = "scalpable"
    if close_best and not short_positive:
        behavior = "eventual rebound, not a scalp"
    counter = [
        row
        for row in positive
        if row["rule_name"] in {"rule_b_countertrend", "rule_d_countertrend_btc_impulse"}
    ]
    impulse = [
        row
        for row in positive
        if row["rule_name"] in {"rule_c_btc_impulse", "rule_d_countertrend_btc_impulse"}
    ]
    tags = []
    if counter:
        tags.append("cheap minority mean-reversion")
    if impulse:
        tags.append("impulse-driven rebound")
    if not tags:
        tags.append("simple first-eligible minority rebound")
    return (
        f"Best positive row: {best['rule_name']} at {best['exit_horizon']} exit, "
        f"avg net {best['average_net_cents_per_trade']}c over {best['trade_count']} trades. "
        f"Classification: {behavior}; {', '.join(tags)}."
    )


def _render_markdown(
    paths: Paths,
    summary: list[dict[str, Any]],
    configs: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    comparison: list[dict[str, Any]],
    dq: list[dict[str, Any]],
) -> str:
    ranked = sorted(
        summary,
        key=lambda row: row.get("average_net_cents_per_trade") if row.get("average_net_cents_per_trade") is not None else -999999,
        reverse=True,
    )
    skip_counts = defaultdict(int)
    for row in skipped:
        skip_counts[row["rule_name"]] += 1
    skip_rows = [{"rule_name": key, "days_skipped": value} for key, value in sorted(skip_counts.items())]
    return f"""# Live-Style Cheap Minority Rule Test

## Direct Answer

{_interpret(summary)}

This report takes only the first valid signal per ET day per rule. It does not choose the best opportunity after the fact.

## Rule Configurations

{_table(configs, ["rule_name", "threshold_summary", "time_window_et", "entry_ask_bucket", "contract_type", "max_trades_per_et_day"], 10)}

## Summary By Rule And Exit

{_table(ranked, ["rule_name", "exit_horizon", "calendar_days_observed", "days_with_trade", "days_skipped", "trade_count", "win_rate_pct", "average_gross_cents_per_trade", "average_fee_cents_per_trade", "average_net_cents_per_trade", "median_net_cents_per_trade", "total_net_cents", "profit_factor", "maximum_drawdown", "result_excluding_largest_winner"], 30)}

## Skipped Days

{_table(skip_rows, ["rule_name", "days_skipped"], 10)}

## Descriptive Probability Comparison

{_table(comparison, ["comparison", "entry_bucket", "breakdown_type", "breakdown_value", "candidate_count", "plus_10c_hit_rate_60s", "plus_10c_hit_rate_120s", "plus_10c_hit_rate_180s", "plus_10c_hit_rate_before_close", "note"], 20)}

## Data Quality

{_table(dq, ["issue", "rows_affected"], 20)}

## Output Files

- Trade-level CSV: `{paths.trade_csv}`
- Daily P/L CSV: `{paths.daily_pnl_csv}`
- Summary CSV: `{paths.summary_csv}`
- Skipped-days CSV: `{paths.skipped_days_csv}`
- Rule config CSV: `{paths.rule_config_csv}`
- Descriptive comparison CSV: `{paths.descriptive_comparison_csv}`
- Data quality CSV: `{paths.data_quality_csv}`

## Research Warning

This is a historical live-style simulation with frozen rules. A positive result can justify prospective paper testing, not live trading.
"""


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"live_style_cheap_minority_rule_test_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        trade_csv=output_dir / f"{stem}_trades.csv",
        daily_pnl_csv=output_dir / f"{stem}_daily_pnl.csv",
        summary_csv=output_dir / f"{stem}_summary.csv",
        skipped_days_csv=output_dir / f"{stem}_skipped_days.csv",
        rule_config_csv=output_dir / f"{stem}_rule_config.csv",
        data_quality_csv=output_dir / f"{stem}_data_quality.csv",
        descriptive_comparison_csv=output_dir / f"{stem}_descriptive_comparison.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def build_report(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    market_like: str = "KXBTC15M%",
    start: str | None = None,
    end: str | None = None,
    max_spread: float = 0.01,
    fee_rate_cents: float = 7.0,
    btc_impulse_threshold: float = 50.0,
    exit_tolerance_seconds: int = 10,
) -> Paths:
    paths = _paths(output_dir)
    raw_rows = _load_rows(market_like, start, end)
    rows_by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        rows_by_market[int(row["market_pk"])].append(row)
    for rows in rows_by_market.values():
        rows.sort(key=lambda item: item["captured_at"])

    entry_rows = _observed_entry_rows(raw_rows)
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in entry_rows:
        by_day[row["captured_at"].astimezone(ET).date().isoformat()].append(row)
    observed_days = sorted(by_day)

    trades: list[dict[str, Any]] = []
    for day in observed_days:
        day_rows = sorted(by_day[day], key=lambda item: (item["captured_at"], item["market_ticker"]))
        for config in RULE_CONFIGS:
            signal = _find_daily_signal(day_rows, rows_by_market, config.rule_name, max_spread, btc_impulse_threshold)
            if signal is None:
                continue
            market_rows = rows_by_market[int(signal["market_pk"])]
            for horizon in EXIT_HORIZONS:
                trades.append(_score_signal(signal, market_rows, horizon, max_spread, fee_rate_cents, exit_tolerance_seconds))
    _add_running_pnl(trades)

    daily = _daily_pnl(trades, observed_days)
    summary = _summarize(trades, observed_days)
    skipped = _skipped_days(trades, observed_days)
    configs = _rule_config_rows(btc_impulse_threshold, max_spread, fee_rate_cents)
    dq = _data_quality(raw_rows, entry_rows, max_spread)
    comparison = _descriptive_comparison(output_dir)

    _write_csv(paths.trade_csv, trades)
    _write_csv(paths.daily_pnl_csv, daily)
    _write_csv(paths.summary_csv, summary)
    _write_csv(paths.skipped_days_csv, skipped)
    _write_csv(paths.rule_config_csv, configs)
    _write_csv(paths.data_quality_csv, dq)
    _write_csv(paths.descriptive_comparison_csv, comparison)
    paths.markdown_report.write_text(_render_markdown(paths, summary, configs, skipped, comparison, dq))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Build live-style 08:00-11:00 ET cheap minority rule test.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M%")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-spread", type=float, default=0.01)
    parser.add_argument("--fee-rate-cents", type=float, default=7.0)
    parser.add_argument("--btc-impulse-threshold", type=float, default=50.0)
    parser.add_argument("--exit-tolerance-seconds", type=int, default=10)
    args = parser.parse_args()

    paths = build_report(
        output_dir=args.output_dir,
        market_like=args.market_like,
        start=args.start,
        end=args.end,
        max_spread=args.max_spread,
        fee_rate_cents=args.fee_rate_cents,
        btc_impulse_threshold=args.btc_impulse_threshold,
        exit_tolerance_seconds=args.exit_tolerance_seconds,
    )
    print("Live-style cheap minority rule test complete")
    print(f"trade_csv={paths.trade_csv}")
    print(f"daily_pnl_csv={paths.daily_pnl_csv}")
    print(f"summary_csv={paths.summary_csv}")
    print(f"skipped_days_csv={paths.skipped_days_csv}")
    print(f"rule_config_csv={paths.rule_config_csv}")
    print(f"descriptive_comparison_csv={paths.descriptive_comparison_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
