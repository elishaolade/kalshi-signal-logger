#!/usr/bin/env python3
"""
Take-profit ladder exit report for the live-style cheap minority entry rule.

Entry is frozen as the first eligible 20-30c minority contract per ET day in
08:00-11:00 ET. Exit targets are compared after entry selection, so this report
is an exploratory exit comparison, not a live optimized strategy.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.research_daily_time_window_scalp_report import (  # noqa: E402
    ET,
    _avg,
    _clean_row,
    _fee_cents,
    _is_clean_side,
    _load_rows,
    _max_drawdown,
    _median,
    _pct,
    _price,
    _profit_factor,
    _table,
    _write_csv,
)
from scripts.research_live_style_cheap_minority_rule_test import (  # noqa: E402
    _base_candidate,
    _last_clean_before_close,
    _observed_entry_rows,
    _round,
)

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "cheap_minority_take_profit_ladder"
TARGET_LEVELS: tuple[float | None, ...] = (0.45, 0.55, 0.65, 0.75, 0.85, 0.95, None)


@dataclass(frozen=True)
class Paths:
    directory: Path
    trade_csv: Path
    summary_csv: Path
    daily_pnl_csv: Path
    target_timing_csv: Path
    data_quality_csv: Path
    markdown_report: Path


def _iso_et(value) -> str | None:
    if value is None:
        return None
    return value.astimezone(ET).replace(tzinfo=None).isoformat(sep=" ")


def _exit_rule_name(target: float | None) -> str:
    if target is None:
        return "hold_to_close"
    return f"tp_{int(round(target * 100)):02d}c_else_close"


def _find_first_daily_signal(day_rows: list[dict[str, Any]], rows_by_market: dict[int, list[dict[str, Any]]], max_spread: float) -> dict[str, Any] | None:
    for row in sorted(day_rows, key=lambda item: (item["captured_at"], item["market_ticker"])):
        candidate = _base_candidate(row, rows_by_market[int(row["market_pk"])], max_spread)
        if candidate is not None:
            candidate["rule_name"] = "first_eligible_20_30c_minority"
            return candidate
    return None


def _first_target_hit(rows: list[dict[str, Any]], side: str, entry_at, target: float, max_spread: float) -> dict[str, Any] | None:
    for row in rows:
        if row["captured_at"] <= entry_at:
            continue
        if not _is_clean_side(row, side, max_spread):
            continue
        bid = _price(row, side, "bid")
        if bid is not None and bid >= target:
            return row
    return None


def _score_signal(signal: dict[str, Any], market_rows: list[dict[str, Any]], target: float | None, max_spread: float, fee_rate_cents: float) -> dict[str, Any]:
    side = signal["contract_side"]
    entry_at = signal["entry_at"]
    entry_ask = signal["entry_ask"]
    close_row = _last_clean_before_close(market_rows, side, entry_at, market_rows[0]["closes_at"], max_spread)
    target_row = None if target is None else _first_target_hit(market_rows, side, entry_at, target, max_spread)
    if target_row is not None:
        exit_row = target_row
        exit_reason = "target_hit"
    else:
        exit_row = close_row
        exit_reason = "close_exit"

    base = {
        "date_et": signal["date_et"],
        "weekday": signal["weekday"],
        "is_weekend": signal["is_weekend"],
        "market_ticker": signal["market_ticker"],
        "market_open_timestamp_et": signal["market_open_timestamp_et"],
        "entry_timestamp_et": signal["entry_timestamp_et"],
        "seconds_since_market_open": signal["seconds_since_market_open"],
        "contract_side": side,
        "minority_side": signal["minority_side"],
        "dominant_side": signal["dominant_side"],
        "entry_bid": signal["entry_bid"],
        "entry_ask": entry_ask,
        "entry_spread": signal["entry_spread"],
        "exit_rule": _exit_rule_name(target),
        "target_level": target,
        "first_valid_signal_of_day": 1,
        "btc_price_60s_before_entry": signal.get("btc_price_60s_before_entry"),
        "btc_price_at_entry": signal.get("btc_price_at_entry"),
        "btc_60s_move": signal.get("btc_60s_move"),
        "minority_aligned_with_btc_60s_direction": signal.get("minority_aligned_with_btc_60s_direction"),
    }
    if exit_row is None:
        base.update(
            {
                "status": "NO_VALID_EXIT",
                "exit_timestamp_et": None,
                "exit_bid": None,
                "exit_reason": None,
                "time_to_target_seconds": None,
                "gross_cents": None,
                "fee_cents": None,
                "net_cents": None,
                "win_loss_flag": None,
                "running_total_net_cents": None,
                "running_drawdown": None,
            }
        )
        return base

    exit_bid = _price(exit_row, side, "bid")
    gross = round((exit_bid - entry_ask) * 100.0, 4)
    fee = round(_fee_cents(entry_ask, fee_rate_cents) + _fee_cents(exit_bid, fee_rate_cents), 6)
    net = round(gross - fee, 4)
    time_to_target = None
    if target_row is not None:
        time_to_target = round((target_row["captured_at"] - entry_at).total_seconds(), 3)
    base.update(
        {
            "status": "COMPLETE",
            "exit_timestamp_et": _iso_et(exit_row["captured_at"]),
            "exit_bid": _round(exit_bid),
            "exit_reason": exit_reason,
            "time_to_target_seconds": time_to_target,
            "gross_cents": gross,
            "fee_cents": fee,
            "net_cents": net,
            "win_loss_flag": "win" if net > 0 else "loss",
            "running_total_net_cents": None,
            "running_drawdown": None,
        }
    )
    return base


def _add_running_pnl(trades: list[dict[str, Any]]) -> None:
    by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        by_rule[row["exit_rule"]].append(row)
    for rows in by_rule.values():
        total = 0.0
        peak = 0.0
        for row in sorted(rows, key=lambda item: item["entry_timestamp_et"]):
            if row["status"] != "COMPLETE" or row["net_cents"] is None:
                continue
            total = round(total + row["net_cents"], 4)
            peak = max(peak, total)
            row["running_total_net_cents"] = total
            row["running_drawdown"] = round(total - peak, 4)


def _summarize(trades: list[dict[str, Any]], observed_days: list[str]) -> list[dict[str, Any]]:
    out = []
    first_half_dates = set(observed_days[: len(observed_days) // 2])
    second_half_dates = set(observed_days[len(observed_days) // 2 :])
    for target in TARGET_LEVELS:
        rule = _exit_rule_name(target)
        group = [row for row in trades if row["exit_rule"] == rule]
        complete = [row for row in group if row["status"] == "COMPLETE" and row["net_cents"] is not None]
        nets = [row["net_cents"] for row in complete]
        gross = [row["gross_cents"] for row in complete]
        fees = [row["fee_cents"] for row in complete]
        first = [row["net_cents"] for row in complete if row["date_et"] in first_half_dates]
        second = [row["net_cents"] for row in complete if row["date_et"] in second_half_dates]
        weekday = [row["net_cents"] for row in complete if row["is_weekend"] == 0]
        weekend = [row["net_cents"] for row in complete if row["is_weekend"] == 1]
        time_to_target = [row["time_to_target_seconds"] for row in complete if row["time_to_target_seconds"] is not None]
        excluding_winner = list(nets)
        if excluding_winner:
            excluding_winner.remove(max(excluding_winner))
        excluding_loser = list(nets)
        if excluding_loser:
            excluding_loser.remove(min(excluding_loser))
        out.append(
            {
                "exit_rule": rule,
                "target_level": target,
                "calendar_days_observed": len(observed_days),
                "days_with_trade": len({row["date_et"] for row in group}),
                "days_skipped": len(observed_days) - len({row["date_et"] for row in group}),
                "trade_count": len(complete),
                "target_hit_count": sum(row["exit_reason"] == "target_hit" for row in complete),
                "target_hit_rate_pct": _pct(sum(row["exit_reason"] == "target_hit" for row in complete), len(complete)),
                "average_time_to_target_seconds": _avg(time_to_target),
                "median_time_to_target_seconds": _median(time_to_target),
                "win_rate_pct": _pct(sum(v > 0 for v in nets), len(nets)),
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
    by_key = {(row["exit_rule"], row["date_et"]): row for row in trades}
    out = []
    for target in TARGET_LEVELS:
        rule = _exit_rule_name(target)
        total = 0.0
        peak = 0.0
        for day in observed_days:
            row = by_key.get((rule, day))
            if row is None:
                out.append(
                    {
                        "date_et": day,
                        "exit_rule": rule,
                        "status": "NO_TRADE",
                        "net_cents": None,
                        "running_total_net_cents": total,
                        "running_drawdown": round(total - peak, 4),
                    }
                )
                continue
            if row["status"] == "COMPLETE" and row["net_cents"] is not None:
                total = round(total + row["net_cents"], 4)
                peak = max(peak, total)
            out.append(
                {
                    "date_et": day,
                    "exit_rule": rule,
                    "status": row["status"],
                    "market_ticker": row["market_ticker"],
                    "exit_reason": row["exit_reason"],
                    "net_cents": row["net_cents"],
                    "gross_cents": row["gross_cents"],
                    "running_total_net_cents": total,
                    "running_drawdown": round(total - peak, 4),
                }
            )
    return out


def _target_timing(summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "exit_rule": row["exit_rule"],
            "target_level": row["target_level"],
            "target_hit_count": row["target_hit_count"],
            "target_hit_rate_pct": row["target_hit_rate_pct"],
            "average_time_to_target_seconds": row["average_time_to_target_seconds"],
            "median_time_to_target_seconds": row["median_time_to_target_seconds"],
        }
        for row in summary
        if row["target_level"] is not None
    ]


def _data_quality(raw_rows: list[dict[str, Any]], entry_rows: list[dict[str, Any]], signals: list[dict[str, Any]], max_spread: float) -> list[dict[str, Any]]:
    return [
        {"issue": "raw_rows_loaded", "rows_affected": len(raw_rows)},
        {"issue": "entry_window_rows_loaded", "rows_affected": len(entry_rows)},
        {"issue": "entry_window_clean_rows", "rows_affected": sum(_clean_row(row, max_spread) for row in entry_rows)},
        {"issue": "first_daily_entry_signals", "rows_affected": len(signals)},
        {"issue": "quote_age_ms_unavailable_in_historical_snapshots", "rows_affected": len(raw_rows)},
        {"issue": "raw_yes_bid_gt_ask", "rows_affected": sum(row.get("yes_bid") is not None and row.get("yes_ask") is not None and row["yes_bid"] > row["yes_ask"] for row in raw_rows)},
        {"issue": "raw_no_bid_gt_ask", "rows_affected": sum(row.get("no_bid") is not None and row.get("no_ask") is not None and row["no_bid"] > row["no_ask"] for row in raw_rows)},
        {"issue": "raw_yes_spread_eq_zero", "rows_affected": sum(row.get("yes_spread") == 0 for row in raw_rows)},
        {"issue": "raw_no_spread_eq_zero", "rows_affected": sum(row.get("no_spread") == 0 for row in raw_rows)},
    ]


def _best_by(summary: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    rows = [row for row in summary if row.get(field) is not None]
    if not rows:
        return None
    return max(rows, key=lambda row: row[field])


def _interpret(summary: list[dict[str, Any]]) -> str:
    best_avg = _best_by(summary, "average_net_cents")
    best_median = _best_by(summary, "median_net_cents")
    best_pf = _best_by([row for row in summary if isinstance(row.get("profit_factor"), (int, float))], "profit_factor")
    best_dd = min(
        [row for row in summary if row.get("maximum_drawdown") is not None],
        key=lambda row: abs(row["maximum_drawdown"]),
        default=None,
    )
    close = next((row for row in summary if row["exit_rule"] == "hold_to_close"), None)
    positive_targets = [row for row in summary if row["target_level"] is not None and (row.get("average_net_cents") or -999) > 0]
    if not best_avg:
        return "No complete trades were available to compare the exit ladder."
    dependency_note = "Largest-winner dependence is unresolved."
    if best_avg.get("total_net_cents") is not None and best_avg.get("result_excluding_largest_winner") is not None:
        dependency_note = (
            "The best row remains positive without its largest winner."
            if best_avg["result_excluding_largest_winner"] > 0
            else "The best row turns negative or flat without its largest winner."
        )
    if close and positive_targets:
        style = "modest/large rebound trade"
    elif close and (close.get("average_net_cents") or -999) > 0:
        style = "hold-to-close option-style trade"
    else:
        style = "not favorable after fees"
    return (
        f"Best average net: `{best_avg['exit_rule']}` at `{best_avg['average_net_cents']}`c. "
        f"Best median net: `{best_median['exit_rule'] if best_median else None}`. "
        f"Best drawdown row: `{best_dd['exit_rule'] if best_dd else None}`. "
        f"Best profit factor: `{best_pf['exit_rule'] if best_pf else None}`. "
        f"{dependency_note} Overall style classification: {style}."
    )


def _render_markdown(paths: Paths, summary: list[dict[str, Any]], timing: list[dict[str, Any]], dq: list[dict[str, Any]]) -> str:
    ranked = sorted(
        summary,
        key=lambda row: row.get("average_net_cents") if row.get("average_net_cents") is not None else -999999,
        reverse=True,
    )
    return f"""# Cheap Minority Take-Profit Ladder Report

## Direct Answer

{_interpret(summary)}

Entry is fixed as the first eligible 20-30c minority contract per ET day in 08:00-11:00 ET. Exit targets are an exploratory comparison.

## Exit Summary

{_table(ranked, ["exit_rule", "target_level", "calendar_days_observed", "days_with_trade", "trade_count", "target_hit_count", "target_hit_rate_pct", "average_time_to_target_seconds", "win_rate_pct", "average_net_cents", "median_net_cents", "total_net_cents", "profit_factor", "maximum_drawdown", "result_excluding_largest_winner"], 20)}

## Target Timing

{_table(timing, ["exit_rule", "target_level", "target_hit_count", "target_hit_rate_pct", "average_time_to_target_seconds", "median_time_to_target_seconds"], 20)}

## Data Quality

{_table(dq, ["issue", "rows_affected"], 20)}

## Output Files

- Trade-level CSV: `{paths.trade_csv}`
- Exit-rule summary CSV: `{paths.summary_csv}`
- Daily P/L CSV: `{paths.daily_pnl_csv}`
- Target-hit timing CSV: `{paths.target_timing_csv}`
- Data quality CSV: `{paths.data_quality_csv}`

## Research Warning

Do not choose a take-profit level from this report and treat it as validated. This compares exits after the entry cohort is known and should be followed by prospective paper testing.
"""


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"cheap_minority_take_profit_ladder_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        trade_csv=output_dir / f"{stem}_trades.csv",
        summary_csv=output_dir / f"{stem}_summary.csv",
        daily_pnl_csv=output_dir / f"{stem}_daily_pnl.csv",
        target_timing_csv=output_dir / f"{stem}_target_timing.csv",
        data_quality_csv=output_dir / f"{stem}_data_quality.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def build_report(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    market_like: str = "KXBTC15M%",
    start: str | None = None,
    end: str | None = None,
    max_spread: float = 0.01,
    fee_rate_cents: float = 7.0,
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

    signals = []
    for day in observed_days:
        signal = _find_first_daily_signal(by_day[day], rows_by_market, max_spread)
        if signal is not None:
            signals.append(signal)

    trades = []
    for signal in signals:
        market_rows = rows_by_market[int(signal["market_pk"])]
        for target in TARGET_LEVELS:
            trades.append(_score_signal(signal, market_rows, target, max_spread, fee_rate_cents))
    _add_running_pnl(trades)

    summary = _summarize(trades, observed_days)
    daily = _daily_pnl(trades, observed_days)
    timing = _target_timing(summary)
    dq = _data_quality(raw_rows, entry_rows, signals, max_spread)

    _write_csv(paths.trade_csv, trades)
    _write_csv(paths.summary_csv, summary)
    _write_csv(paths.daily_pnl_csv, daily)
    _write_csv(paths.target_timing_csv, timing)
    _write_csv(paths.data_quality_csv, dq)
    paths.markdown_report.write_text(_render_markdown(paths, summary, timing, dq))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Build cheap minority take-profit ladder exit report.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M%")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-spread", type=float, default=0.01)
    parser.add_argument("--fee-rate-cents", type=float, default=7.0)
    args = parser.parse_args()
    paths = build_report(args.output_dir, args.market_like, args.start, args.end, args.max_spread, args.fee_rate_cents)
    print("Cheap minority take-profit ladder report complete")
    print(f"trade_csv={paths.trade_csv}")
    print(f"summary_csv={paths.summary_csv}")
    print(f"daily_pnl_csv={paths.daily_pnl_csv}")
    print(f"target_timing_csv={paths.target_timing_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
