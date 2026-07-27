#!/usr/bin/env python3
"""
Inspect trade-level contract prices for the live-style BTC impulse rule.

Input is the CSV produced by research_live_style_time_window_rule_test.py.
This script does not query MySQL and does not modify strategy thresholds.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_INPUT_CSV = Path(
    "/app/reports/live_style_time_window_rule_test/"
    "live_style_time_window_rule_test_2026-07-27_trades.csv"
)
DEFAULT_OUTPUT_DIR = Path("/app/reports/live_style_time_window_rule_contract_price_inspection")
TARGET_RULE = "rule_a_btc_60s_abs_move"
TARGET_HORIZON = 120


@dataclass(frozen=True)
class Paths:
    directory: Path
    trade_csv: Path
    bucket_summary_csv: Path
    overall_summary_csv: Path
    markdown_report: Path


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        value_float = float(text)
    except ValueError:
        return None
    if not math.isfinite(value_float):
        return None
    return value_float


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


def _pct(num: int, den: int) -> float | None:
    if den == 0:
        return None
    return round(100.0 * num / den, 1)


def _profit_factor(values: Iterable[float | None]) -> float | str | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    if losses == 0:
        return "inf" if gains > 0 else None
    return round(gains / losses, 4)


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_et_from_utc(value: str | None) -> str | None:
    parsed = _parse_utc(value)
    if parsed is None:
        return None
    return parsed.astimezone(ET).replace(tzinfo=None).isoformat(sep=" ")


def _entry_bucket(entry_ask: float | None) -> str:
    if entry_ask is None:
        return "unknown"
    if entry_ask < 0 or entry_ask > 1:
        return "out_of_range"
    lo = min(9, int(entry_ask * 10))
    return f"{lo / 10:.2f}-{(lo + 1) / 10:.2f}"


def _bucket_sort_key(label: str) -> tuple[int, str]:
    if label in ("unknown", "out_of_range"):
        return (99, label)
    try:
        return (int(float(label.split("-", 1)[0]) * 100), label)
    except ValueError:
        return (98, label)


def _read_rows(input_csv: Path) -> list[dict[str, str]]:
    with input_csv.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _filtered_trade_rows(raw_rows: list[dict[str, str]]) -> list[dict]:
    out = []
    for row in raw_rows:
        if row.get("rule_name") != TARGET_RULE:
            continue
        if int(_to_float(row.get("exit_horizon_seconds")) or -1) != TARGET_HORIZON:
            continue
        if row.get("status") != "COMPLETE":
            continue
        entry_ask = _to_float(row.get("entry_ask"))
        exit_bid = _to_float(row.get("exit_bid"))
        gross = _to_float(row.get("gross_pnl_cents"))
        fee = _to_float(row.get("total_fee_cents"))
        net = _to_float(row.get("net_pnl_cents"))
        btc_price = _to_float(row.get("btc_price"))
        btc_move = _to_float(row.get("btc_delta_60s"))
        btc_prior = btc_price - btc_move if btc_price is not None and btc_move is not None else None
        trade = {
            "et_date": row.get("date_et"),
            "market_ticker": row.get("market_ticker"),
            "entry_timestamp_et": row.get("entry_at_et"),
            "exit_timestamp_et": _format_et_from_utc(row.get("exit_at_utc")),
            "trade_side": row.get("entry_side"),
            "btc_price_60s_before_entry": round(btc_prior, 4) if btc_prior is not None else None,
            "btc_price_at_entry": btc_price,
            "btc_60_second_move": btc_move,
            "entry_ask": entry_ask,
            "exit_bid": exit_bid,
            "gross_cents": gross,
            "fee_cents": fee,
            "net_cents": net,
            "win_loss_flag": "win" if net is not None and net > 0 else "loss",
            "entry_spread": _to_float(row.get("entry_spread")),
            "contract_price_bucket": _entry_bucket(entry_ask),
        }
        out.append(trade)
    return sorted(out, key=lambda item: (item.get("et_date") or "", item.get("entry_timestamp_et") or ""))


def _summarize_group(label: str, rows: list[dict]) -> dict:
    nets = [row.get("net_cents") for row in rows]
    return {
        "contract_price_bucket": label,
        "trade_count": len(rows),
        "win_rate": _pct(sum((row.get("net_cents") or 0) > 0 for row in rows), len(rows)),
        "average_entry_ask": _avg(row.get("entry_ask") for row in rows),
        "average_exit_bid": _avg(row.get("exit_bid") for row in rows),
        "average_gross_cents": _avg(row.get("gross_cents") for row in rows),
        "average_fee_cents": _avg(row.get("fee_cents") for row in rows),
        "average_net_cents": _avg(nets),
        "median_net_cents": _median(nets),
        "total_net_cents": round(sum(v for v in nets if v is not None), 4) if rows else None,
        "best_trade": max((v for v in nets if v is not None), default=None),
        "worst_trade": min((v for v in nets if v is not None), default=None),
        "profit_factor": _profit_factor(nets),
    }


def _bucket_summary(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in trades:
        groups[row["contract_price_bucket"]].append(row)
    return [_summarize_group(label, groups[label]) for label in sorted(groups, key=_bucket_sort_key)]


def _overall_summary(trades: list[dict]) -> list[dict]:
    if not trades:
        return [
            {
                "trade_count": 0,
                "average_entry_ask": None,
                "median_entry_ask": None,
                "minimum_entry_ask": None,
                "maximum_entry_ask": None,
                "average_exit_bid": None,
                "median_exit_bid": None,
                "average_gross_cents": None,
                "average_fee_cents": None,
                "average_net_cents": None,
                "median_net_cents": None,
            }
        ]
    entry = [row.get("entry_ask") for row in trades]
    exit_bid = [row.get("exit_bid") for row in trades]
    return [
        {
            "trade_count": len(trades),
            "average_entry_ask": _avg(entry),
            "median_entry_ask": _median(entry),
            "minimum_entry_ask": min(v for v in entry if v is not None),
            "maximum_entry_ask": max(v for v in entry if v is not None),
            "average_exit_bid": _avg(exit_bid),
            "median_exit_bid": _median(exit_bid),
            "average_gross_cents": _avg(row.get("gross_cents") for row in trades),
            "average_fee_cents": _avg(row.get("fee_cents") for row in trades),
            "average_net_cents": _avg(row.get("net_cents") for row in trades),
            "median_net_cents": _median(row.get("net_cents") for row in trades),
        }
    ]


def _classify_price_level(avg_entry: float | None) -> str:
    if avg_entry is None:
        return "unknown"
    if avg_entry < 0.30:
        return "cheap"
    if avg_entry < 0.70:
        return "mid-priced"
    return "expensive"


def _top_bucket(summary: list[dict]) -> dict | None:
    if not summary:
        return None
    return max(summary, key=lambda row: row.get("total_net_cents") if row.get("total_net_cents") is not None else -999999)


def _largest_trade(trades: list[dict]) -> dict | None:
    if not trades:
        return None
    return max(trades, key=lambda row: abs(row.get("net_cents") or 0))


def _render_markdown(paths: Paths, trades: list[dict], buckets: list[dict], overall: list[dict]) -> str:
    overall_row = overall[0]
    winning = [row for row in trades if (row.get("net_cents") or 0) > 0]
    winning_avg_entry = _avg(row.get("entry_ask") for row in winning)
    winning_level = _classify_price_level(winning_avg_entry)
    top = _top_bucket(buckets)
    largest = _largest_trade(trades)
    total_net = sum(row.get("net_cents") or 0 for row in trades)
    largest_share = None
    if largest is not None and total_net:
        largest_share = round(100.0 * (largest.get("net_cents") or 0) / total_net, 1)

    direction_answer = "insufficient data"
    if overall_row.get("average_entry_ask") is not None:
        direction_answer = _classify_price_level(overall_row["average_entry_ask"])

    outlier_answer = "No trade data."
    if largest is not None:
        outlier_answer = (
            f"Largest absolute trade: `{largest['et_date']} {largest['market_ticker']}` "
            f"bucket `{largest['contract_price_bucket']}`, net `{largest['net_cents']}`c"
        )
        if largest_share is not None:
            outlier_answer += f", equal to `{largest_share}`% of total net."

    return f"""# Contract Price Inspection — Rule A 120s

## Scope

- Input CSV: `{paths.trade_csv.name}` derived from the live-style rule-test trades CSV
- Rule: `rule_a_btc_60s_abs_move`
- Exit horizon: `120s`
- Entry: ask
- Exit: bid
- Fee model: `7.0 * price * (1 - price)` cents per side
- This report only inspects prices. It does not change thresholds or strategy logic.

## Direct Answers

1. Profitable trades were mostly `{winning_level}` contracts by average winning entry ask `{winning_avg_entry}`.
2. Overall entry pricing looks `{direction_answer}` by average entry ask `{overall_row.get("average_entry_ask")}`. Use the bucket table below to distinguish cheap explosion from directional continuation.
3. Bucket contributing most total net: `{top.get("contract_price_bucket") if top else None}` with total net `{top.get("total_net_cents") if top else None}`c.
4. Outlier check: {outlier_answer}
5. Future research classification: `{"cheap-contract strategy" if direction_answer == "cheap" else "directional momentum strategy" if direction_answer in ("mid-priced", "expensive") else "undetermined"}`.

## Overall

{_table(overall, ["trade_count", "average_entry_ask", "median_entry_ask", "minimum_entry_ask", "maximum_entry_ask", "average_exit_bid", "median_exit_bid", "average_gross_cents", "average_fee_cents", "average_net_cents", "median_net_cents"])}

## Bucket Summary

{_table(buckets, ["contract_price_bucket", "trade_count", "win_rate", "average_entry_ask", "average_exit_bid", "average_gross_cents", "average_fee_cents", "average_net_cents", "median_net_cents", "total_net_cents", "best_trade", "worst_trade", "profit_factor"], 20)}

## Trade Preview

{_table(trades, ["et_date", "market_ticker", "entry_timestamp_et", "exit_timestamp_et", "trade_side", "btc_60_second_move", "entry_ask", "exit_bid", "gross_cents", "fee_cents", "net_cents", "win_loss_flag", "entry_spread", "contract_price_bucket"], 30)}

## Output Files

- Trade-level CSV: `{paths.trade_csv}`
- Bucket summary CSV: `{paths.bucket_summary_csv}`
- Overall summary CSV: `{paths.overall_summary_csv}`
"""


def _table(rows: list[dict], cols: list[str], limit: int = 12) -> str:
    if not rows:
        return "_No rows._"
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in cols) + " |")
    return "\n".join(lines)


def _write_csv(path: Path, rows: list[dict]) -> None:
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


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"live_style_rule_a_120s_contract_price_inspection_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        trade_csv=output_dir / f"{stem}_trades.csv",
        bucket_summary_csv=output_dir / f"{stem}_bucket_summary.csv",
        overall_summary_csv=output_dir / f"{stem}_overall_summary.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def build_report(input_csv: Path, output_dir: Path) -> Paths:
    paths = _paths(output_dir)
    raw = _read_rows(input_csv)
    trades = _filtered_trade_rows(raw)
    buckets = _bucket_summary(trades)
    overall = _overall_summary(trades)
    _write_csv(paths.trade_csv, trades)
    _write_csv(paths.bucket_summary_csv, buckets)
    _write_csv(paths.overall_summary_csv, overall)
    paths.markdown_report.write_text(_render_markdown(paths, trades, buckets, overall))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect contract prices for Rule A 120s trades")
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT_CSV)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    paths = build_report(args.input_csv, args.output_dir)
    print("Contract price inspection report complete")
    print(f"trade_csv={paths.trade_csv}")
    print(f"bucket_summary_csv={paths.bucket_summary_csv}")
    print(f"overall_summary_csv={paths.overall_summary_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
