#!/usr/bin/env python3
"""Export the cheap minority Real-Money TEST logs and final markdown report."""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import config
from app.db import fetch_all

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "cheap_minority_real_money_test"


@dataclass(frozen=True)
class Paths:
    directory: Path
    trade_log_csv: Path
    skipped_day_csv: Path
    account_curve_csv: Path
    order_fill_audit_csv: Path
    daily_email_summaries: Path
    final_markdown_report: Path


def _f(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return round(sum(vals) / len(vals), 6) if vals else None


def _median(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return round(statistics.median(vals), 6) if vals else None


def _pf(values: list[float]) -> float | str | None:
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if losses == 0:
        return "inf" if gains > 0 else None
    return round(gains / losses, 4)


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    running = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return round(worst, 6)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
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
    stem = f"cheap_minority_real_money_test_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        trade_log_csv=output_dir / f"{stem}_trade_log.csv",
        skipped_day_csv=output_dir / f"{stem}_skipped_days.csv",
        account_curve_csv=output_dir / f"{stem}_account_curve.csv",
        order_fill_audit_csv=output_dir / f"{stem}_order_fill_audit.csv",
        daily_email_summaries=output_dir / f"{stem}_daily_email_summaries.md",
        final_markdown_report=output_dir / f"{stem}_final_report.md",
    )


def _load(profile: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    trades = fetch_all("SELECT * FROM cheap_minority_test_trades WHERE profile=%s ORDER BY et_date, id", (profile,))
    skips = fetch_all("SELECT * FROM cheap_minority_test_skipped_days WHERE profile=%s ORDER BY et_date", (profile,))
    curve = fetch_all("SELECT * FROM cheap_minority_test_account_curve WHERE profile=%s ORDER BY observed_at, id", (profile,))
    audit = fetch_all("SELECT * FROM cheap_minority_test_order_events WHERE profile=%s ORDER BY event_at, id", (profile,))
    daily = fetch_all("SELECT * FROM cheap_minority_test_daily_summaries WHERE profile=%s ORDER BY et_date", (profile,))
    return trades, skips, curve, audit, daily


def _summary(trades: list[dict[str, Any]], skips: list[dict[str, Any]], audit: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [row for row in trades if row.get("status") == "COMPLETE"]
    nets = [_f(row.get("actual_net_dollars")) for row in complete]
    nets = [v for v in nets if v is not None]
    cents = [_f(row.get("actual_net_cents_per_contract")) for row in complete]
    cents = [v for v in cents if v is not None]
    fees = [_f(row.get("actual_fees_dollars")) for row in complete]
    fees = [v for v in fees if v is not None]
    fills = [row for row in audit if row.get("event_type") == "entry_filled"]
    submitted = [row for row in audit if row.get("event_type") == "entry_submitted"]
    missed = [row for row in trades if row.get("status") == "MISSED_FILL"]
    excluding_winner = list(nets)
    if excluding_winner:
        excluding_winner.remove(max(excluding_winner))
    excluding_loser = list(nets)
    if excluding_loser:
        excluding_loser.remove(min(excluding_loser))
    first15 = cents[:15]
    last15 = cents[-15:] if len(cents) >= 15 else []
    start_balance = config.CHEAP_MINORITY_TEST_STARTING_BALANCE_DOLLARS
    end_balance = _f(complete[-1].get("account_balance_after_trade")) if complete else None
    return {
        "calendar_days_observed": len({row.get("et_date") for row in [*trades, *skips]}),
        "completed_live_trades": len(complete),
        "skipped_days": len(skips),
        "attempted_orders": len(submitted),
        "filled_orders": len(fills),
        "missed_fills": len(missed),
        "fill_rate_pct": round(100.0 * len(fills) / len(submitted), 1) if submitted else None,
        "win_rate_pct": round(100.0 * sum(v > 0 for v in nets) / len(nets), 1) if nets else None,
        "average_actual_net_cents_per_contract": _avg(cents),
        "median_actual_net_cents_per_contract": _median(cents),
        "total_actual_net_dollars": round(sum(nets), 6) if nets else None,
        "average_dollars_per_trade": _avg(nets),
        "actual_profit_factor": _pf(nets),
        "maximum_drawdown_dollars": _max_drawdown(nets),
        "largest_winner": max(nets) if nets else None,
        "largest_loser": min(nets) if nets else None,
        "target_hit_count": sum(row.get("target_hit_flag") == 1 for row in complete),
        "target_hit_rate_pct": round(100.0 * sum(row.get("target_hit_flag") == 1 for row in complete) / len(complete), 1) if complete else None,
        "result_excluding_largest_winner": round(sum(excluding_winner), 6) if excluding_winner else None,
        "result_excluding_largest_loser": round(sum(excluding_loser), 6) if excluding_loser else None,
        "first_15_trades_avg_actual_net_cents": _avg(first15),
        "last_15_trades_avg_actual_net_cents": _avg(last15),
        "account_balance_start": start_balance,
        "account_balance_end": end_balance,
        "account_return_pct": round(100.0 * ((end_balance - start_balance) / start_balance), 2) if end_balance is not None and start_balance else None,
        "actual_fees_paid": round(sum(fees), 6) if fees else None,
        "rule_violations": sum(row.get("rule_violation_flag") == 1 for row in trades),
    }


def _pass_fail(summary: dict[str, Any]) -> str:
    pf = summary.get("actual_profit_factor")
    pf_num = pf if isinstance(pf, (int, float)) else 999 if pf == "inf" else None
    fail_reasons = []
    if (summary.get("completed_live_trades") or 0) < 20:
        fail_reasons.append("fewer than 20 completed live trades")
    if (summary.get("average_actual_net_cents_per_contract") or 0) <= 0:
        fail_reasons.append("average actual net cents per contract <= 0")
    if (summary.get("total_actual_net_dollars") or 0) <= 0:
        fail_reasons.append("total actual net dollars <= 0")
    if pf_num is None or pf_num <= 1.25:
        fail_reasons.append("actual profit factor <= 1.25")
    if summary.get("result_excluding_largest_winner") is not None and summary["result_excluding_largest_winner"] < 0:
        fail_reasons.append("excluding largest winner turns result negative")
    if (summary.get("rule_violations") or 0) > 0:
        fail_reasons.append("rule violations occurred")
    if fail_reasons:
        return "FAILED / INCOMPLETE: " + "; ".join(fail_reasons)
    return "PASSED: all configured pass criteria were met"


def _render_report(paths: Paths, summary: dict[str, Any], trades: list[dict[str, Any]], skips: list[dict[str, Any]], audit: list[dict[str, Any]]) -> str:
    return f"""# Cheap Minority Real-Money TEST Report

## Direct Answer

{_pass_fail(summary)}

This report answers whether the cheap minority 20-30c strategy survived a Real-Money TEST with a `$10` starting account and `2` contracts per trade.

## Summary

{_table([summary], list(summary.keys()), 1)}

## Recent Trades

{_table(trades[-20:], ["test_trade_number", "et_date", "market_ticker", "side", "status", "contracts_filled", "actual_avg_entry_price", "actual_avg_exit_price", "exit_reason", "actual_net_dollars", "actual_net_cents_per_contract", "account_balance_after_trade", "rule_violation_flag"], 20)}

## Missed Fills / Skipped Days

{_table(skips, ["et_date", "reason_no_trade", "markets_checked", "eligible_signals_found", "order_attempted_flag", "missed_fill_flag", "insufficient_balance_flag", "account_balance_at_end_of_day"], 30)}

## Rule Violations

{_table([row for row in trades if row.get("rule_violation_flag") == 1], ["id", "et_date", "market_ticker", "rule_violation_reason", "notes"], 20)}

## Order Audit Preview

{_table(audit[-30:], ["event_at", "trade_id", "event_type", "action", "requested_count", "filled_count", "limit_price", "avg_fill_price", "fees_dollars", "detail"], 30)}

## Output Files

- Real-money TEST trade log CSV: `{paths.trade_log_csv}`
- Skipped-day CSV: `{paths.skipped_day_csv}`
- Account curve CSV: `{paths.account_curve_csv}`
- Order/fill audit CSV: `{paths.order_fill_audit_csv}`
- Daily email summaries: `{paths.daily_email_summaries}`
"""


def build_report(output_dir: Path, profile: str) -> Paths:
    paths = _paths(output_dir)
    trades, skips, curve, audit, daily = _load(profile)
    summary = _summary(trades, skips, audit)
    _write_csv(paths.trade_log_csv, trades)
    _write_csv(paths.skipped_day_csv, skips)
    _write_csv(paths.account_curve_csv, curve)
    _write_csv(paths.order_fill_audit_csv, audit)
    paths.daily_email_summaries.write_text("\n\n---\n\n".join(str(row.get("summary_text") or "") for row in daily))
    paths.final_markdown_report.write_text(_render_report(paths, summary, trades, skips, audit))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Export cheap minority Real-Money TEST report.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--profile", default=config.CHEAP_MINORITY_TEST_PROFILE)
    args = parser.parse_args()
    paths = build_report(args.output_dir, args.profile)
    print("Cheap minority Real-Money TEST report complete")
    print(f"trade_log_csv={paths.trade_log_csv}")
    print(f"skipped_day_csv={paths.skipped_day_csv}")
    print(f"account_curve_csv={paths.account_curve_csv}")
    print(f"order_fill_audit_csv={paths.order_fill_audit_csv}")
    print(f"daily_email_summaries={paths.daily_email_summaries}")
    print(f"final_markdown_report={paths.final_markdown_report}")


if __name__ == "__main__":
    main()
