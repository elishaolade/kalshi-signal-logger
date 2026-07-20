#!/usr/bin/env python3
"""
Feature discovery for the frozen 65-70c fast-dominance rebound cohort.

Input:
  The trades CSV produced by research_falsify_65_70_fast_rebound.py.

Question:
  Can pre-entry information identify which minority contracts have stronger
  90-second executable gross P/L and +3c/+5c/+10c rebounds?

This script is descriptive only. It does not optimize a strategy or propose live
trading. Buckets are broad/sign-based or quantile-based.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).parent.parent
DEFAULT_INPUT_GLOB = ROOT / "reports" / "falsify_65_70_fast_rebound" / "*_trades.csv"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "falsify_65_70_fast_rebound_features"


@dataclass(frozen=True)
class Paths:
    directory: Path
    feature_bucket_csv: Path
    stability_csv: Path
    feature_compare_csv: Path
    candidates_csv: Path
    cutoffs_csv: Path
    markdown_report: Path


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _avg(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 6)


def _median(values: Iterable[float | None]) -> float | None:
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return round(vals[mid], 6)
    return round((vals[mid - 1] + vals[mid]) / 2.0, 6)


def _pct(num: float, den: float) -> float | None:
    if den == 0:
        return None
    return round(100.0 * num / den, 1)


def _profit_factor(values: Iterable[float | None]) -> float | str | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    if losses == 0:
        return "inf" if gains > 0 else None
    return round(gains / losses, 6)


def _max_drawdown(values: list[float]) -> float | None:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 6)


def _quantiles(rows: list[dict[str, Any]], field: str) -> tuple[float | None, float | None]:
    vals = sorted(row[field] for row in rows if row.get(field) is not None)
    if len(vals) < 4:
        return None, None
    return vals[len(vals) // 3], vals[(2 * len(vals)) // 3]


def _sign_bucket(value: float | None, eps: float = 0.0) -> str:
    if value is None:
        return "unknown"
    if value > eps:
        return "positive"
    if value < -eps:
        return "negative"
    return "flat"


def _quantile_bucket(value: float | None, q1: float | None, q2: float | None) -> str:
    if value is None or q1 is None or q2 is None:
        return "unknown"
    if value <= q1:
        return "low"
    if value <= q2:
        return "mid"
    return "high"


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            row = dict(raw)
            for key in (
                "row_num",
                "gross_pnl_cents",
                "target_3c_hit",
                "target_5c_hit",
                "target_10c_hit",
                "btc_move_since_open_dominant_side",
                "btc_move_prev_10s_dominant_side",
                "btc_move_prev_30s_dominant_side",
                "btc_move_prev_60s_dominant_side",
                "btc_distance_dominant_side",
                "abs_btc_distance",
                "btc_volatility_60s",
                "dominant_cents_per_second",
                "dominant_change_prev_10s_cents",
                "dominant_change_prev_30s_cents",
                "dominant_change_prev_60s_cents",
                "minority_change_prev_10s_cents",
                "minority_change_prev_30s_cents",
                "minority_change_prev_60s_cents",
                "minority_spread",
                "minority_ask",
                "dominant_ask",
            ):
                row[key] = _to_float(row.get(key))
            rows.append(row)
    rows.sort(key=lambda r: (r.get("entry_at") or "", r.get("market_ticker") or ""))
    n = len(rows)
    for i, row in enumerate(rows):
        row["dataset_half"] = "first_half" if i < n / 2 else "second_half"
        row["gross_win"] = int((row.get("gross_pnl_cents") or 0) > 0)
    return rows


def _cutoff_row(
    feature_name: str,
    source_field: str,
    q1: float | None,
    q2: float | None,
) -> dict[str, Any]:
    return {
        "feature_name": feature_name,
        "source_field": source_field,
        "low_bucket_rule": f"{source_field} <= {q1}" if q1 is not None else "unknown",
        "mid_bucket_rule": f"{source_field} > {q1} AND {source_field} <= {q2}" if q1 is not None and q2 is not None else "unknown",
        "high_bucket_rule": f"{source_field} > {q2}" if q2 is not None else "unknown",
        "low_max_inclusive": q1,
        "mid_min_exclusive": q1,
        "mid_max_inclusive": q2,
        "high_min_exclusive": q2,
    }


def _derive_features(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    q_abs_dist = _quantiles(rows, "abs_btc_distance")
    q_spread = _quantiles(rows, "minority_spread")
    q_dom_speed = _quantiles(rows, "dominant_cents_per_second")
    q_dom_reprice_30 = _quantiles(rows, "dominant_change_prev_30s_cents")
    q_minority_ask = _quantiles(rows, "minority_ask")
    cutoffs = [
        _cutoff_row("abs_btc_distance_bucket", "abs_btc_distance", *q_abs_dist),
        _cutoff_row("spread_bucket", "minority_spread", *q_spread),
        _cutoff_row("dominant_speed_quantile", "dominant_cents_per_second", *q_dom_speed),
        _cutoff_row("dominant_reprice_30s_quantile", "dominant_change_prev_30s_cents", *q_dom_reprice_30),
        _cutoff_row("minority_ask_quantile", "minority_ask", *q_minority_ask),
    ]

    for row in rows:
        btc10 = row.get("btc_move_prev_10s_dominant_side")
        btc30 = row.get("btc_move_prev_30s_dominant_side")
        btc60 = row.get("btc_move_prev_60s_dominant_side")
        dom10 = row.get("dominant_change_prev_10s_cents")
        dom30 = row.get("dominant_change_prev_30s_cents")
        min10 = row.get("minority_change_prev_10s_cents")
        min30 = row.get("minority_change_prev_30s_cents")

        row["btc_deceleration"] = _deceleration_bucket(btc10, btc30)
        row["btc_accel_weakening"] = _accel_weakening_bucket(btc10, btc30, btc60)
        row["dominant_repricing_exhaustion"] = _exhaustion_bucket(dom10, dom30)
        row["minority_stabilization"] = _minority_stabilization_bucket(min10, min30)
        row["btc_prev_10s_bucket"] = _sign_bucket(btc10, eps=2.0)
        row["btc_prev_30s_bucket"] = _sign_bucket(btc30, eps=5.0)
        row["abs_btc_distance_bucket"] = _quantile_bucket(row.get("abs_btc_distance"), *q_abs_dist)
        row["spread_bucket"] = _quantile_bucket(row.get("minority_spread"), *q_spread)
        row["dominant_speed_quantile"] = _quantile_bucket(row.get("dominant_cents_per_second"), *q_dom_speed)
        row["dominant_reprice_30s_quantile"] = _quantile_bucket(row.get("dominant_change_prev_30s_cents"), *q_dom_reprice_30)
        row["minority_ask_quantile"] = _quantile_bucket(row.get("minority_ask"), *q_minority_ask)
    return rows, cutoffs


def _deceleration_bucket(btc10: float | None, btc30: float | None) -> str:
    if btc10 is None or btc30 is None:
        return "unknown"
    recent_rate = btc10 / 10.0
    prior_rate = btc30 / 30.0
    if btc10 < -2:
        return "reversing_against_dominant"
    if abs(btc10) <= 2:
        return "flat_recent"
    if recent_rate < prior_rate * 0.5:
        return "strong_deceleration"
    if recent_rate < prior_rate:
        return "mild_deceleration"
    return "not_decelerating"


def _accel_weakening_bucket(btc10: float | None, btc30: float | None, btc60: float | None) -> str:
    if btc10 is None or btc30 is None or btc60 is None:
        return "unknown"
    accel_short = btc10 / 10.0 - btc30 / 30.0
    accel_long = btc30 / 30.0 - btc60 / 60.0
    if accel_short < accel_long - 0.25:
        return "acceleration_weakening"
    if accel_short > accel_long + 0.25:
        return "acceleration_strengthening"
    return "acceleration_stable"


def _exhaustion_bucket(dom10: float | None, dom30: float | None) -> str:
    if dom10 is None or dom30 is None:
        return "unknown"
    recent_rate = dom10 / 10.0
    prior_rate = dom30 / 30.0
    if dom10 < 0:
        return "dominant_reversing"
    if abs(dom10) <= 0.25:
        return "dominant_flat"
    if recent_rate < prior_rate * 0.5:
        return "dominant_strong_slowdown"
    if recent_rate < prior_rate:
        return "dominant_mild_slowdown"
    return "dominant_not_slowing"


def _minority_stabilization_bucket(min10: float | None, min30: float | None) -> str:
    if min10 is None or min30 is None:
        return "unknown"
    recent_rate = min10 / 10.0
    prior_rate = min30 / 30.0
    if min10 > 0:
        return "minority_already_bouncing"
    if abs(min10) <= 0.25:
        return "minority_flat"
    if recent_rate > prior_rate * 0.5:
        return "minority_decline_slowing"
    return "minority_still_falling"


def _summarize(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(k) for k in keys)].append(row)
    out = []
    for key_values, group in groups.items():
        gross = [row["gross_pnl_cents"] for row in group if row.get("gross_pnl_cents") is not None]
        item = {key: value for key, value in zip(keys, key_values)}
        if len(keys) == 1:
            item["feature_name"] = keys[0]
            item["feature_value"] = key_values[0]
        elif len(keys) == 2 and keys[1] == "dataset_half":
            item["feature_name"] = keys[0]
            item["feature_value"] = key_values[0]
            item["dataset_half"] = key_values[1]
        item.update(
            {
                "trades": len(group),
                "unique_markets": len({row.get("market_ticker") for row in group}),
                "active_days": len({row.get("entry_date_et") for row in group}),
                "avg_gross_pnl_cents": _avg(gross),
                "median_gross_pnl_cents": _median(gross),
                "total_gross_pnl_cents": round(sum(gross), 6),
                "gross_profit_factor": _profit_factor(gross),
                "max_gross_drawdown_cents": _max_drawdown(gross),
                "gross_win_rate_pct": _pct(sum(v > 0 for v in gross), len(gross)),
                "target_3c_rate": _pct(sum((row.get("target_3c_hit") or 0) == 1 for row in group), len(group)),
                "target_5c_rate": _pct(sum((row.get("target_5c_hit") or 0) == 1 for row in group), len(group)),
                "target_10c_rate": _pct(sum((row.get("target_10c_hit") or 0) == 1 for row in group), len(group)),
                "avg_btc_move_prev_10s": _avg(row.get("btc_move_prev_10s_dominant_side") for row in group),
                "avg_btc_move_prev_30s": _avg(row.get("btc_move_prev_30s_dominant_side") for row in group),
                "avg_dominant_change_prev_10s": _avg(row.get("dominant_change_prev_10s_cents") for row in group),
                "avg_minority_change_prev_10s": _avg(row.get("minority_change_prev_10s_cents") for row in group),
                "small_sample_flag": int(len(group) < 30 or len({row.get("entry_date_et") for row in group}) < 5),
            }
        )
        out.append(item)
    return sorted(out, key=lambda r: (-(r.get("avg_gross_pnl_cents") or -999), -int(r["trades"])))


def _feature_compare(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cohorts = {
        "strong_gross_gt_5c": [row for row in rows if (row.get("gross_pnl_cents") or 0) > 5],
        "weak_gross_le_0c": [row for row in rows if (row.get("gross_pnl_cents") or 0) <= 0],
        "target_3c_hit": [row for row in rows if row.get("target_3c_hit") == 1],
        "target_3c_miss": [row for row in rows if row.get("target_3c_hit") != 1],
    }
    fields = (
        "btc_move_prev_10s_dominant_side",
        "btc_move_prev_30s_dominant_side",
        "btc_move_prev_60s_dominant_side",
        "abs_btc_distance",
        "btc_volatility_60s",
        "dominant_cents_per_second",
        "dominant_change_prev_10s_cents",
        "dominant_change_prev_30s_cents",
        "minority_change_prev_10s_cents",
        "minority_change_prev_30s_cents",
        "minority_spread",
        "minority_ask",
    )
    out = []
    for cohort, group in cohorts.items():
        item = {"cohort": cohort, "trades": len(group)}
        for field in fields:
            item[f"avg_{field}"] = _avg(row.get(field) for row in group)
            item[f"median_{field}"] = _median(row.get(field) for row in group)
        out.append(item)
    return out


def _candidate_features(feature_summary: list[dict[str, Any]], min_trades: int, min_avg_gross: float) -> list[dict[str, Any]]:
    candidates = []
    for row in feature_summary:
        if row.get("trades", 0) < min_trades:
            continue
        if (row.get("avg_gross_pnl_cents") or -999) < min_avg_gross:
            continue
        candidates.append(row)
    return sorted(candidates, key=lambda r: (-(r.get("avg_gross_pnl_cents") or -999), -int(r["trades"])))


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


def _latest_input(pattern: str) -> Path:
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"no trades CSV matched {pattern}")
    return Path(matches[-1])


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"65_70_fast_preentry_features_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        feature_bucket_csv=output_dir / f"{stem}_feature_buckets.csv",
        stability_csv=output_dir / f"{stem}_stability.csv",
        feature_compare_csv=output_dir / f"{stem}_feature_compare.csv",
        candidates_csv=output_dir / f"{stem}_candidate_features.csv",
        cutoffs_csv=output_dir / f"{stem}_cutoffs.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def build_report(input_csv: Path, output_dir: Path, min_trades: int, min_avg_gross: float) -> Paths:
    paths = _paths(output_dir)
    rows, cutoffs = _derive_features(_load_rows(input_csv))

    feature_keys = (
        "btc_deceleration",
        "btc_accel_weakening",
        "dominant_repricing_exhaustion",
        "minority_stabilization",
        "btc_prev_10s_bucket",
        "btc_prev_30s_bucket",
        "abs_btc_distance_bucket",
        "spread_bucket",
        "dominant_speed_quantile",
        "dominant_reprice_30s_quantile",
        "minority_ask_quantile",
    )
    feature_rows = []
    for key in feature_keys:
        feature_rows.extend(_summarize(rows, (key,)))

    stability_rows = []
    for key in feature_keys:
        stability_rows.extend(_summarize(rows, (key, "dataset_half")))

    compare_rows = _feature_compare(rows)
    candidate_rows = _candidate_features(feature_rows, min_trades, min_avg_gross)

    _write_csv(paths.feature_bucket_csv, feature_rows)
    _write_csv(paths.stability_csv, stability_rows)
    _write_csv(paths.feature_compare_csv, compare_rows)
    _write_csv(paths.candidates_csv, candidate_rows)
    _write_csv(paths.cutoffs_csv, cutoffs)
    paths.markdown_report.write_text(_render_report(input_csv, rows, feature_rows, stability_rows, compare_rows, candidate_rows, cutoffs, min_trades, min_avg_gross))
    return paths


def _stable_candidates(candidates: list[dict[str, Any]], stability_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stable = []
    by_feature = defaultdict(list)
    for row in stability_rows:
        feature_name = row.get("feature_name")
        feature_value = row.get("feature_value")
        if feature_name is None:
            continue
        by_feature[(feature_name, feature_value)].append(row)
    for candidate in candidates:
        feature_name = candidate.get("feature_name")
        feature_value = candidate.get("feature_value")
        if feature_name is None:
            continue
        halves = by_feature.get((feature_name, feature_value), [])
        if len(halves) == 2 and all((h.get("avg_gross_pnl_cents") or -999) > 0 for h in halves):
            row = dict(candidate)
            row["first_half_avg_gross"] = next((h["avg_gross_pnl_cents"] for h in halves if h["dataset_half"] == "first_half"), None)
            row["second_half_avg_gross"] = next((h["avg_gross_pnl_cents"] for h in halves if h["dataset_half"] == "second_half"), None)
            stable.append(row)
    return stable


def _render_report(
    input_csv: Path,
    rows: list[dict[str, Any]],
    feature_rows: list[dict[str, Any]],
    stability_rows: list[dict[str, Any]],
    compare_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    cutoffs: list[dict[str, Any]],
    min_trades: int,
    min_avg_gross: float,
) -> str:
    baseline = _summarize(rows, tuple())[0]
    stable = _stable_candidates(candidate_rows, stability_rows)
    return f"""# 65-70c Fast Pre-Entry Feature Discovery

## Scope

- Input CSV: `{input_csv}`
- Trades analyzed: `{len(rows)}`
- Target outcome: 90-second executable gross P/L and +3c/+5c/+10c rebound flags
- Features: pre-entry only
- Bucketing: sign-based or broad quantiles
- Candidate screen: at least `{min_trades}` trades and avg gross P/L >= `{min_avg_gross}` cents

## Baseline

{_table([baseline], ["trades", "unique_markets", "active_days", "avg_gross_pnl_cents", "median_gross_pnl_cents", "gross_profit_factor", "target_3c_rate", "target_5c_rate", "target_10c_rate"])}

## Candidate Feature Buckets

{_table(candidate_rows, ["feature_name", "feature_value", "trades", "avg_gross_pnl_cents", "median_gross_pnl_cents", "gross_profit_factor", "target_3c_rate", "target_5c_rate", "target_10c_rate"], 30)}

## Candidate Buckets Stable In Both Halves

{_table(stable, ["feature_name", "feature_value", "trades", "avg_gross_pnl_cents", "first_half_avg_gross", "second_half_avg_gross", "gross_profit_factor", "target_3c_rate", "target_5c_rate"], 20)}

## Quantile Cutoffs

{_table(cutoffs, ["feature_name", "source_field", "low_bucket_rule", "mid_bucket_rule", "high_bucket_rule"], 20)}

For example, `dominant_reprice_30s_quantile=mid` means:

`dominant_change_prev_30s_cents > low_max_inclusive AND dominant_change_prev_30s_cents <= mid_max_inclusive`

## Winner / Loser Feature Comparison

{_table(compare_rows, ["cohort", "trades", "avg_btc_move_prev_10s_dominant_side", "avg_btc_move_prev_30s_dominant_side", "avg_dominant_change_prev_10s_cents", "avg_minority_change_prev_10s_cents", "avg_minority_spread", "avg_abs_btc_distance"], 10)}

## Direct Answers

1. BTC deceleration predicts stronger rebounds only if `btc_deceleration` buckets show materially higher average gross P/L and survive both halves.
2. Weakening acceleration predicts stronger rebounds only if `btc_accel_weakening` buckets pass the same check.
3. Dominant repricing exhaustion predicts stronger rebounds only if `dominant_repricing_exhaustion` buckets pass.
4. Minority stabilization predicts stronger rebounds only if `minority_stabilization` buckets pass.
5. The most predictive feature is the highest avg-gross candidate that also appears in the stable table.
6. If the stable table is empty, the relationship is not stable enough for a frozen hypothesis.
7. A reasonably sized >5c subgroup exists only if the candidate table has rows with `trades >= {min_trades}`.
8. A new falsification hypothesis is justified only if one candidate is broad, stable in both halves, and not a tiny subgroup.

This is feature discovery only. It does not change the frozen trading rule.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze pre-entry features for the 65-70c fast cohort")
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--input-glob", default=str(DEFAULT_INPUT_GLOB))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-trades", type=int, default=30)
    parser.add_argument("--min-avg-gross", type=float, default=5.0)
    args = parser.parse_args()

    input_csv = args.input_csv or _latest_input(args.input_glob)
    paths = build_report(input_csv, args.output_dir, args.min_trades, args.min_avg_gross)
    print("65-70c fast pre-entry feature report complete")
    print(f"input_csv={input_csv}")
    print(f"feature_bucket_csv={paths.feature_bucket_csv}")
    print(f"stability_csv={paths.stability_csv}")
    print(f"feature_compare_csv={paths.feature_compare_csv}")
    print(f"candidates_csv={paths.candidates_csv}")
    print(f"cutoffs_csv={paths.cutoffs_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
