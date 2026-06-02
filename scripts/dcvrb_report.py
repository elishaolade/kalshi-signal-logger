"""
dcvrb_report.py — Analysis report for delayed_contract_value_reversal_bounce/v1.

Reads completed dcvrb_observations rows and answers the core research questions:
  1. Do contracts that collapse early into 20¢–40¢ later produce +3¢–+6¢ bounces?
  2. Is immediate entry (v1) worse than waiting for extra flush + reclaim (v2/v3)?
  3. Which flush depth works best?
  4. Which bounce confirmation works best?
  5. Which exit test has the best expectancy?
  6. Does high volatility help or hurt the bounce?
  7. Are spreads killing the setup?
  8. Does this work better for YES or NO?
  9. Does this work better at certain times / days?

Run:
    python -m scripts.dcvrb_report [--min-signals N] [--version v1|v2|v3|all]
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

from app.db import fetch_all

# ── Sample-size thresholds ────────────────────────────────────────────────────
_THRESHOLDS = [
    (250, "stronger_evidence"),
    (100, "first_serious_review"),
    (50,  "early_clue"),
    (25,  "smoke_test_only"),
    (0,   "insufficient"),
]


def _confidence(n: int) -> str:
    for threshold, label in _thresholds_sorted():
        if n >= threshold:
            return label
    return "insufficient"


def _thresholds_sorted():
    return sorted(_THRESHOLDS, reverse=True)


# ── Metric calculation ────────────────────────────────────────────────────────

def _metrics(rows: list[dict]) -> dict:
    """Compute performance metrics for a group of completed rows."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "confidence": "insufficient"}

    valid = [r for r in rows if r.get("simulated_pnl") is not None]
    n_valid = len(valid)
    if n_valid == 0:
        return {"n": n, "n_valid": 0, "confidence": _confidence(n)}

    pnls = [float(r["simulated_pnl"]) for r in valid]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / n_valid if n_valid else 0.0
    avg_pnl  = sum(pnls) / n_valid
    med_pnl  = sorted(pnls)[n_valid // 2]
    avg_win  = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    mfe_vals = [float(r["max_favorable_excursion"]) for r in valid
                if r.get("max_favorable_excursion") is not None]
    mae_vals = [float(r["max_adverse_excursion"])   for r in valid
                if r.get("max_adverse_excursion")   is not None]

    tp_count    = sum(1 for r in valid if r.get("hit_take_profit_before_stop"))
    sl_count    = sum(1 for r in valid if r.get("hit_stop_before_take_profit"))
    timeout_ct  = sum(1 for r in valid if r.get("timed_out"))
    struct_stop = sum(1 for r in valid if r.get("structure_stop_hit"))

    ttp_vals = [float(r["time_to_profit_target"]) for r in valid
                if r.get("time_to_profit_target") is not None]
    peak_vals = [float(r["time_to_peak"]) for r in valid
                 if r.get("time_to_peak") is not None]

    return {
        "n":                n,
        "n_valid":          n_valid,
        "confidence":       _confidence(n_valid),
        "win_rate":         round(win_rate, 4),
        "avg_pnl":          round(avg_pnl, 4),
        "median_pnl":       round(med_pnl, 4),
        "avg_win":          round(avg_win, 4),
        "avg_loss":         round(avg_loss, 4),
        "expectancy":       round(expectancy, 4),
        "profit_factor":    round(profit_factor, 4),
        "tp_count":         tp_count,
        "sl_count":         sl_count,
        "timeout_count":    timeout_ct,
        "structure_stops":  struct_stop,
        "avg_mfe":          round(sum(mfe_vals) / len(mfe_vals), 4) if mfe_vals else None,
        "avg_mae":          round(sum(mae_vals) / len(mae_vals), 4) if mae_vals else None,
        "avg_time_to_peak": round(sum(peak_vals) / len(peak_vals), 1) if peak_vals else None,
        "avg_time_to_tp":   round(sum(ttp_vals)  / len(ttp_vals),  1) if ttp_vals else None,
    }


# ── Printing helpers ──────────────────────────────────────────────────────────

def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:.1f}%" if v is not None else "  N/A"


def _fmt_cents(v: float | None) -> str:
    return f"{v * 100:+.2f}¢" if v is not None else "  N/A"


def _print_metrics(label: str, m: dict) -> None:
    if m.get("n_valid", 0) == 0:
        print(f"  {label:<40}  n={m.get('n', 0)}  [{m.get('confidence', '?')}]  (no valid P&L rows)")
        return
    print(
        f"  {label:<40}  n={m['n_valid']:>4}  [{m['confidence']}]  "
        f"WR={_fmt_pct(m['win_rate'])}  "
        f"avg={_fmt_cents(m['avg_pnl'])}  "
        f"med={_fmt_cents(m['median_pnl'])}  "
        f"E={_fmt_cents(m['expectancy'])}  "
        f"PF={m['profit_factor']:.2f}  "
        f"tp={m['tp_count']}  sl={m['sl_count']}  to={m['timeout_count']}  "
        f"struct_stop={m['structure_stops']}  "
        f"avg_mfe={_fmt_cents(m['avg_mfe'])}  avg_mae={_fmt_cents(m['avg_mae'])}"
    )


def _section(title: str) -> None:
    print()
    print("─" * 80)
    print(f"  {title}")
    print("─" * 80)


# ── Group-by helper ───────────────────────────────────────────────────────────

def _group(rows: list[dict], key: str) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        k = str(r.get(key) or "NULL")
        groups.setdefault(k, []).append(r)
    return dict(sorted(groups.items()))


# ── Main report ───────────────────────────────────────────────────────────────

def run_report(min_signals: int = 0, version_filter: str = "all") -> None:
    # Load all COMPLETE rows
    rows = fetch_all(
        """
        SELECT *
        FROM dcvrb_observations
        WHERE status = 'COMPLETE'
          AND exit_reason_simulated NOT IN ('v3_not_qualified', 'stopped_out_pre_signal',
                                            'recovered_after_restart')
        ORDER BY recorded_at
        """,
        (),
    )

    if not rows:
        print("No completed dcvrb_observations rows found.")
        return

    # Apply version filter
    if version_filter != "all":
        rows = [r for r in rows if r.get("comparison_version") == version_filter]

    total = len(rows)
    print(f"\n{'='*80}")
    print(f"  DCVRB Report — delayed_contract_value_reversal_bounce/v1")
    print(f"  Total completed rows (after filter): {total}")
    print(f"  Version filter: {version_filter}")
    print(f"  Min signals threshold: {min_signals}")
    print(f"{'='*80}")

    # ── Q1: Overall by comparison version × exit test ──────────────────────────
    _section("Q1/Q2: By comparison version × exit test (does delay help?)")
    for ver in ("v1", "v2", "v3"):
        ver_rows = [r for r in rows if r.get("comparison_version") == ver]
        if not ver_rows:
            continue
        print(f"\n  Version {ver}:")
        for test in ("test_a", "test_b", "test_c", "test_d"):
            m = _metrics([r for r in ver_rows if r.get("exit_test") == test])
            _print_metrics(f"  {test}", m)

    # ── Q3: Flush depth ────────────────────────────────────────────────────────
    _section("Q3: Flush depth — which extra_flush_bucket is best? (v2 rows)")
    v2 = [r for r in rows if r.get("comparison_version") == "v2"]
    for bucket in ("0.02-0.03", "0.03-0.05", "0.05+"):
        bucket_rows = [r for r in v2 if r.get("extra_flush_bucket") == bucket]
        for test in ("test_a", "test_b", "test_c", "test_d"):
            m = _metrics([r for r in bucket_rows if r.get("exit_test") == test])
            _print_metrics(f"flush={bucket} {test}", m)

    # ── Q4: Bounce confirmation ────────────────────────────────────────────────
    _section("Q4: Bounce confirmation — which bounce_bucket is best? (v2 rows)")
    for bucket in ("0.01-0.02", "0.02+"):
        bucket_rows = [r for r in v2 if r.get("bounce_bucket") == bucket]
        for test in ("test_a", "test_b", "test_c", "test_d"):
            m = _metrics([r for r in bucket_rows if r.get("exit_test") == test])
            _print_metrics(f"bounce={bucket} {test}", m)

    # ── Q5: Best exit test overall ────────────────────────────────────────────
    _section("Q5: Exit test summary across all versions")
    for test in ("test_a", "test_b", "test_c", "test_d"):
        m = _metrics([r for r in rows if r.get("exit_test") == test])
        _print_metrics(test, m)

    # ── Q6: Volatility regime ─────────────────────────────────────────────────
    _section("Q6: Volatility regime (v2 rows)")
    for regime in ("calm", "normal", "elevated", "violent", "unknown"):
        regime_rows = [r for r in v2 if r.get("volatility_regime") == regime]
        m = _metrics(regime_rows)
        _print_metrics(f"vol_regime={regime}", m)

    # ── Q7: Spread buckets ────────────────────────────────────────────────────
    _section("Q7: Spread at watch (v2 rows)")
    for spread_label, lo, hi in [
        ("spread_tight  ≤0.01", 0.00, 0.011),
        ("spread_normal 0.01-0.02", 0.011, 0.021),
        ("spread_wide   0.02-0.03", 0.021, 0.031),
    ]:
        spread_rows = [
            r for r in v2
            if r.get("spread_at_watch") is not None
            and lo <= float(r["spread_at_watch"]) < hi
        ]
        m = _metrics(spread_rows)
        _print_metrics(spread_label, m)

    # ── Q8: YES vs NO side ────────────────────────────────────────────────────
    _section("Q8: YES vs NO side (v2 rows × all exit tests)")
    for side in ("YES", "NO"):
        side_rows = [r for r in v2 if r.get("side") == side]
        for test in ("test_a", "test_b", "test_c", "test_d"):
            m = _metrics([r for r in side_rows if r.get("exit_test") == test])
            _print_metrics(f"side={side} {test}", m)

    # ── Q9: Time of day / day of week ─────────────────────────────────────────
    _section("Q9a: Hour block (v2 × test_a)")
    test_a_v2 = [r for r in v2 if r.get("exit_test") == "test_a"]
    for grp_key, grp_rows in _group(test_a_v2, "hour_block").items():
        m = _metrics(grp_rows)
        _print_metrics(f"hour={grp_key}", m)

    _section("Q9b: Day name (v2 × test_a)")
    for grp_key, grp_rows in _group(test_a_v2, "day_name").items():
        m = _metrics(grp_rows)
        _print_metrics(f"day={grp_key}", m)

    # ── Contract price bucket ─────────────────────────────────────────────────
    _section("Contract price bucket at watch_start (v2 rows)")
    for bucket in ("0.20-0.25", "0.25-0.30", "0.30-0.35", "0.35-0.40"):
        bucket_rows = [r for r in v2 if r.get("price_bucket") == bucket]
        for test in ("test_a", "test_b", "test_c", "test_d"):
            m = _metrics([r for r in bucket_rows if r.get("exit_test") == test])
            _print_metrics(f"price={bucket} {test}", m)

    # ── Market age bucket ──────────────────────────────────────────────────────
    _section("Market age at signal (v2 rows)")
    for age_label, lo, hi in [
        ("age  60-120s", 60,  120),
        ("age 120-180s", 120, 180),
        ("age 180-240s", 180, 240),
    ]:
        age_rows = [
            r for r in v2
            if r.get("market_age_seconds") is not None
            and lo <= int(r["market_age_seconds"]) < hi
        ]
        m = _metrics(age_rows)
        _print_metrics(age_label, m)

    # ── Time remaining bucket ──────────────────────────────────────────────────
    _section("Time remaining at signal (v2 rows)")
    for tr_label, lo, hi in [
        ("tr 300-420s (5-7 min)", 300, 420),
        ("tr 420-540s (7-9 min)", 420, 540),
        ("tr 540-720s (9-12 min)", 540, 720),
        ("tr 720+s   (12+ min)",  720, 9999),
    ]:
        tr_rows = [
            r for r in v2
            if r.get("time_remaining_seconds") is not None
            and lo <= int(r["time_remaining_seconds"]) < hi
        ]
        m = _metrics(tr_rows)
        _print_metrics(tr_label, m)

    # ── v1 stopped-out analysis (separate query) ───────────────────────────────
    pre_stop_rows = fetch_all(
        """
        SELECT exit_test, side, extra_flush_bucket, price_bucket,
               COUNT(*) as n,
               SUM(v1_stopped_out_pre_signal) as n_stopped_pre
        FROM dcvrb_observations
        WHERE status = 'COMPLETE'
          AND comparison_version = 'v1'
        GROUP BY exit_test, side, extra_flush_bucket, price_bucket
        ORDER BY exit_test, side
        """,
        (),
    )
    if pre_stop_rows:
        _section("v1 immediate entry — pre-signal stop-out rate (how often did SL hit before signal?)")
        for r in pre_stop_rows:
            n = int(r.get("n") or 0)
            n_sp = int(r.get("n_stopped_pre") or 0)
            pct = n_sp / n * 100 if n else 0
            print(
                f"  {r['exit_test']}  side={r['side']}  "
                f"flush={r.get('extra_flush_bucket', '?')}  "
                f"price={r.get('price_bucket', '?')}  "
                f"n={n}  pre_stop={n_sp} ({pct:.0f}%)"
            )

    # ── Summary statistics ─────────────────────────────────────────────────────
    total_signals = fetch_all(
        "SELECT COUNT(DISTINCT signal_id) as n FROM dcvrb_observations WHERE status='COMPLETE'",
        (),
    )
    n_signals = int(total_signals[0]["n"]) if total_signals else 0
    conf = _confidence(n_signals)
    _section("Sample size assessment")
    print(f"  Distinct signals in COMPLETE rows: {n_signals}")
    print(f"  Confidence label:                  {conf}")
    for threshold, label in sorted(_THRESHOLDS, reverse=True):
        marker = "← YOU ARE HERE" if conf == label else ""
        print(f"    {threshold:>4}+ signals → {label}  {marker}")

    print()
    print("Report complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DCVRB research report")
    parser.add_argument(
        "--min-signals", type=int, default=0,
        help="Minimum signal count to display a group (0 = show all)"
    )
    parser.add_argument(
        "--version", choices=["v1", "v2", "v3", "all"], default="all",
        help="Filter to a single comparison version (default: all)"
    )
    args = parser.parse_args()
    run_report(min_signals=args.min_signals, version_filter=args.version)
