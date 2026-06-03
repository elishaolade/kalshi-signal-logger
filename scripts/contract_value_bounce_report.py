#!/usr/bin/env python3
"""
contract_value_bounce_report.py — Research report for the
contract_value_bounce_scalp/v1 hypothesis.

Reads from `contract_value_bounce_backtest_signals` (produced by
contract_value_bounce_backtest.py) and answers 7 research questions across
10 breakdowns.

Hypothesis recap
----------------
Buy a losing contract that has already BOUNCED off its session low (contract-led
entry, not BTC-led).  Ask ≤ 0.30, bounce_from_low ≥ 0.02, spread ≤ 0.02,
market age 60–180s, vol in {calm, normal}.  Simulate 4 fixed absolute-cent
exit tests.

Report dimensions
-----------------
1. Headline per exit test
2. By price bucket        (very_cheap 0.10–0.20 vs cheap_primary 0.20–0.30)
3. By spread bucket
4. By market age          (60–90s / 91–120s / 121–150s / 151–180s)
5. By volatility regime   (calm / normal)
6. By hour block          (hour of day ET)
7. By day of week
8. By bounce-from-low bucket
9. By side bought         (YES / NO)
10. MFE / MAE style summary per exit test

Research questions
------------------
Q1. Does contract-value bounce scalp show positive expectancy after costs?
Q2. Is 0.20–0.30 materially better than 0.10–0.20?
Q3. Does spread ≤ 0.01 outperform spread 0.01–0.02?
Q4. Is 60–120s or 120–180s the better market-age window?
Q5. Does larger bounce size (≥ 0.05) correlate with better outcomes?
Q6. Which exit test is best (or least bad)?
Q7. Are there sub-buckets with n ≥ 50 that justify a second-pass hypothesis?

Usage
-----
    python scripts/contract_value_bounce_report.py [--run-id N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all, fetch_one

# ── Proof standards ────────────────────────────────────────────────────────────
_PROMISING_N         = 25
_INTERESTING_N       = 50
_STRONG_N            = 100


def _latest_run_id() -> Optional[int]:
    row = fetch_one(
        "SELECT MAX(id) AS id FROM contract_value_bounce_backtest_runs"
    )
    return int(row["id"]) if row and row["id"] is not None else None


def _load_run(run_id: int) -> Optional[dict]:
    return fetch_one(
        "SELECT * FROM contract_value_bounce_backtest_runs WHERE id = %s",
        (run_id,),
    )


def _load_signals(run_id: int) -> list[dict]:
    return fetch_all(
        "SELECT * FROM contract_value_bounce_backtest_signals WHERE run_id = %s",
        (run_id,),
    )


# ── Metric helpers ─────────────────────────────────────────────────────────────

def _metrics(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    n       = len(rows)
    wins    = sum(1 for r in rows if r.get("hit_take_profit_before_stop"))
    losses  = sum(1 for r in rows if r.get("hit_stop_before_take_profit"))
    timeouts = sum(1 for r in rows if r.get("timed_out"))
    pnls    = [float(r["simulated_pnl"]) for r in rows
               if r.get("simulated_pnl") is not None]
    mfes    = [float(r["max_favorable_excursion"]) for r in rows
               if r.get("max_favorable_excursion") is not None]
    maes    = [float(r["max_adverse_excursion"]) for r in rows
               if r.get("max_adverse_excursion") is not None]

    win_pnls  = [float(r["simulated_pnl"]) for r in rows
                 if r.get("hit_take_profit_before_stop") and r.get("simulated_pnl") is not None]
    loss_pnls = [float(r["simulated_pnl"]) for r in rows
                 if r.get("hit_stop_before_take_profit") and r.get("simulated_pnl") is not None]

    total_pnl    = sum(pnls)
    avg_pnl      = total_pnl / n if n else 0
    win_rate     = wins / n if n else 0
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss   = abs(sum(p for p in pnls if p < 0))
    pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_win      = sum(win_pnls)  / len(win_pnls)  if win_pnls  else 0
    avg_loss     = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
    avg_mfe      = sum(mfes) / len(mfes) if mfes else 0
    avg_mae      = sum(maes) / len(maes) if maes else 0

    return {
        "n":           n,
        "wins":        wins,
        "losses":      losses,
        "timeouts":    timeouts,
        "win_rate":    win_rate,
        "total_pnl":   total_pnl,
        "avg_pnl":     avg_pnl,
        "profit_factor": pf,
        "avg_win":     avg_win,
        "avg_loss":    avg_loss,
        "avg_mfe":     avg_mfe,
        "avg_mae":     avg_mae,
    }


def _fmt(m: dict[str, Any]) -> str:
    if m["n"] == 0:
        return "  n=0 (no data)"
    pf_s = f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "∞"
    return (
        f"  n={m['n']:4d}  WR={m['win_rate']:5.1%}  "
        f"avg_pnl={m['avg_pnl']:+.4f}  PF={pf_s}  "
        f"avg_MFE={m['avg_mfe']:+.4f}  avg_MAE={m['avg_mae']:+.4f}  "
        f"tot_pnl={m['total_pnl']:+.4f}  "
        f"[TP={m['wins']} SL={m['losses']} TO={m['timeouts']}]"
    )


def _age_bucket(age: Optional[Any]) -> str:
    if age is None:
        return "unknown"
    a = int(age)
    if a <= 90:
        return " 60- 90s"
    if a <= 120:
        return " 91-120s"
    if a <= 150:
        return "121-150s"
    return "151-180s"


def _header(title: str) -> None:
    print(f"\n{'─' * 72}")
    print(f"  {title}")
    print(f"{'─' * 72}")


def _section(label: str, rows: list[dict], indent: int = 0) -> None:
    m = _metrics(rows)
    prefix = "  " * indent
    print(f"{prefix}{label:<36s}{_fmt(m)}")


# ── Report ─────────────────────────────────────────────────────────────────────

def report(run_id: int) -> None:
    run = _load_run(run_id)
    if run is None:
        print(f"ERROR: run_id={run_id} not found.")
        sys.exit(1)

    rows = _load_signals(run_id)
    if not rows:
        print(f"Run {run_id} has no signals yet.")
        return

    print(f"\n{'═' * 72}")
    print(f"  CONTRACT VALUE BOUNCE SCALP / v1  —  Research Report")
    print(f"  run_id={run_id}  slippage={run['slippage_mode']}  "
          f"tz={run['timezone_used']}")
    print(f"  data: {run['data_start']} → {run['data_end']}")
    print(f"  markets={run['n_markets']}  snapshots={run['n_snapshots']}  "
          f"signals={run['n_signals']}  rows={run['n_test_rows']}")
    if run.get("notes"):
        print(f"  notes: {run['notes']}")
    print(f"{'═' * 72}")

    # Group by exit test
    by_test: dict[str, list[dict]] = {}
    for r in rows:
        by_test.setdefault(r["exit_test"], []).append(r)

    # ── 1. Headline per exit test ─────────────────────────────────────────────
    _header("1. HEADLINE — per exit test")
    for tname, trows in sorted(by_test.items()):
        m = _metrics(trows)
        tp  = trows[0].get("tp_abs") if trows else None
        sl  = trows[0].get("sl_abs") if trows else None
        tmo = trows[0].get("timeout_s") if trows else None
        print(f"\n  {tname}  (tp={tp} sl={sl} timeout={tmo}s)")
        print(_fmt(m))

    all_rows = rows  # convenience alias for non-test breakdowns

    # ── 2. By price bucket ────────────────────────────────────────────────────
    _header("2. BY PRICE BUCKET (primary = 0.20–0.30)")
    buckets_seen: set[str] = {r.get("price_bucket", "unknown") for r in all_rows}
    for tname, trows in sorted(by_test.items()):
        print(f"\n  {tname}:")
        for bucket in sorted(buckets_seen):
            subset = [r for r in trows if r.get("price_bucket") == bucket]
            _section(f"    {bucket}", subset)

    # ── 3. By spread bucket ───────────────────────────────────────────────────
    _header("3. BY SPREAD BUCKET (gate ≤ 0.02)")
    sp_buckets_seen: set[str] = {r.get("spread_bucket", "unknown") for r in all_rows}
    for tname, trows in sorted(by_test.items()):
        print(f"\n  {tname}:")
        for bucket in sorted(sp_buckets_seen):
            subset = [r for r in trows if r.get("spread_bucket") == bucket]
            _section(f"    spread {bucket}", subset)

    # ── 4. By market age ──────────────────────────────────────────────────────
    _header("4. BY MARKET AGE (60–180s window)")
    for tname, trows in sorted(by_test.items()):
        print(f"\n  {tname}:")
        age_groups: dict[str, list[dict]] = {}
        for r in trows:
            ab = _age_bucket(r.get("market_age_seconds"))
            age_groups.setdefault(ab, []).append(r)
        for ab in sorted(age_groups):
            _section(f"    age {ab}", age_groups[ab])

    # ── 5. By volatility regime ───────────────────────────────────────────────
    _header("5. BY VOLATILITY REGIME (only calm / normal enter)")
    vol_buckets: set[str] = {r.get("volatility_regime", "unknown") for r in all_rows}
    for tname, trows in sorted(by_test.items()):
        print(f"\n  {tname}:")
        for vb in sorted(vol_buckets):
            subset = [r for r in trows if r.get("volatility_regime") == vb]
            _section(f"    {vb}", subset)

    # ── 6. By hour block ──────────────────────────────────────────────────────
    _header("6. BY HOUR BLOCK (ET)")
    for tname, trows in sorted(by_test.items()):
        print(f"\n  {tname}:")
        hour_groups: dict[str, list[dict]] = {}
        for r in trows:
            hb = r.get("hour_block") or "unknown"
            hour_groups.setdefault(hb, []).append(r)
        for hb in sorted(hour_groups):
            _section(f"    {hb}", hour_groups[hb])

    # ── 7. By day of week ─────────────────────────────────────────────────────
    _header("7. BY DAY OF WEEK")
    for tname, trows in sorted(by_test.items()):
        print(f"\n  {tname}:")
        day_groups: dict[str, list[dict]] = {}
        for r in trows:
            dn = r.get("day_name") or "unknown"
            day_groups.setdefault(dn, []).append(r)
        for dn in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                   "Saturday", "Sunday", "unknown"]:
            if dn in day_groups:
                _section(f"    {dn}", day_groups[dn])

    # ── 8. By bounce-from-low bucket ──────────────────────────────────────────
    _header("8. BY BOUNCE-FROM-LOW BUCKET")
    bounce_buckets: set[str] = {r.get("bounce_bucket", "unknown") for r in all_rows}
    for tname, trows in sorted(by_test.items()):
        print(f"\n  {tname}:")
        for bb in sorted(bounce_buckets):
            subset = [r for r in trows if r.get("bounce_bucket") == bb]
            _section(f"    {bb}", subset)

    # ── 9. By side bought ─────────────────────────────────────────────────────
    _header("9. BY SIDE BOUGHT (YES = losing YES contract; NO = losing NO contract)")
    for tname, trows in sorted(by_test.items()):
        print(f"\n  {tname}:")
        for side in ("YES", "NO"):
            subset = [r for r in trows if r.get("side_bought") == side]
            _section(f"    {side}", subset)

    # ── 10. MFE / MAE summary ─────────────────────────────────────────────────
    _header("10. MFE / MAE PERCENTILE SUMMARY")
    for tname, trows in sorted(by_test.items()):
        mfes = sorted(
            float(r["max_favorable_excursion"])
            for r in trows if r.get("max_favorable_excursion") is not None
        )
        maes = sorted(
            float(r["max_adverse_excursion"])
            for r in trows if r.get("max_adverse_excursion") is not None
        )
        if not mfes:
            continue

        def pct(lst: list[float], q: float) -> float:
            if not lst:
                return 0.0
            idx = int(q * (len(lst) - 1))
            return lst[idx]

        print(f"\n  {tname}  (n={len(mfes)})")
        print(f"    MFE  p25={pct(mfes,0.25):+.4f}  p50={pct(mfes,0.50):+.4f}  "
              f"p75={pct(mfes,0.75):+.4f}  p90={pct(mfes,0.90):+.4f}")
        print(f"    MAE  p25={pct(maes,0.25):+.4f}  p50={pct(maes,0.50):+.4f}  "
              f"p75={pct(maes,0.75):+.4f}  p90={pct(maes,0.90):+.4f}")
        # Fraction that touched TP level in MFE (proxy for "could have hit TP")
        tp = float(trows[0]["tp_abs"] or 0) if trows else 0
        touched_tp = sum(1 for m in mfes if m >= tp)
        print(f"    MFE ≥ tp_abs({tp:.2f}): {touched_tp}/{len(mfes)} "
              f"({touched_tp/len(mfes):.1%}) — 'could have hit TP'")

    # ── Research questions ─────────────────────────────────────────────────────
    _header("RESEARCH QUESTIONS")

    def _best_test_by_pnl(
        subset_fn,
        label: str,
    ) -> None:
        best_pnl = float("-inf")
        best_test = None
        for tname, trows in sorted(by_test.items()):
            subset = subset_fn(trows)
            m = _metrics(subset)
            if m["n"] >= _PROMISING_N and m["avg_pnl"] > best_pnl:
                best_pnl  = m["avg_pnl"]
                best_test = (tname, m)
        if best_test:
            t, m = best_test
            print(f"  Best for {label}: {t}  avg_pnl={m['avg_pnl']:+.4f}  "
                  f"WR={m['win_rate']:.1%}  n={m['n']}")
        else:
            print(f"  {label}: no test with n≥{_PROMISING_N}")

    print("\nQ1. Overall positive expectancy after realistic costs?")
    for tname, trows in sorted(by_test.items()):
        m = _metrics(trows)
        verdict = "POSITIVE" if m["n"] >= _PROMISING_N and m["avg_pnl"] > 0 else (
                  "NEGATIVE" if m["n"] >= _PROMISING_N else "INSUFFICIENT DATA")
        print(f"  {tname:14s}: avg_pnl={m['avg_pnl']:+.4f}  n={m['n']}  → {verdict}")

    print("\nQ2. Is 0.20–0.30 materially better than 0.10–0.20?")
    for tname, trows in sorted(by_test.items()):
        pri = _metrics([r for r in trows if r.get("price_bucket") == "cheap_primary"])
        sec = _metrics([r for r in trows if r.get("price_bucket") == "very_cheap"])
        print(f"  {tname:14s}  cheap_primary(n={pri['n']}) avg_pnl={pri['avg_pnl']:+.4f}  "
              f"very_cheap(n={sec['n']}) avg_pnl={sec['avg_pnl']:+.4f}")

    print("\nQ3. Does tighter spread (0-1c) outperform 1c-2c?")
    for tname, trows in sorted(by_test.items()):
        tight = _metrics([r for r in trows if r.get("spread_bucket") == "0-1c"])
        wider = _metrics([r for r in trows if r.get("spread_bucket") == "1c-2c"])
        tight_pnl = tight.get("avg_pnl", 0.0)
        wider_pnl = wider.get("avg_pnl", 0.0)
        print(f"  {tname:14s}  0-1c(n={tight['n']}) avg_pnl={tight_pnl:+.4f}  "
              f"1c-2c(n={wider['n']}) avg_pnl={wider_pnl:+.4f}")

    print("\nQ4. Is 60–120s or 120–180s the better market-age window?")
    for tname, trows in sorted(by_test.items()):
        early = _metrics([r for r in trows if _age_bucket(r.get("market_age_seconds")) in
                          (" 60- 90s", " 91-120s")])
        late  = _metrics([r for r in trows if _age_bucket(r.get("market_age_seconds")) in
                          ("121-150s", "151-180s")])
        print(f"  {tname:14s}  60-120s(n={early['n']}) avg={early.get('avg_pnl', 0.0):+.4f}  "
              f"120-180s(n={late['n']}) avg={late.get('avg_pnl', 0.0):+.4f}")

    print("\nQ5. Does bounce_5c_plus outperform smaller bounces?")
    for tname, trows in sorted(by_test.items()):
        big   = _metrics([r for r in trows if r.get("bounce_bucket") == "bounce_5c_plus"])
        med   = _metrics([r for r in trows if r.get("bounce_bucket") == "bounce_3c_5c"])
        small = _metrics([r for r in trows if r.get("bounce_bucket") == "bounce_2c_3c"])
        print(f"  {tname:14s}  5c+(n={big['n']}) avg={big.get('avg_pnl', 0.0):+.4f}  "
              f"3c-5c(n={med['n']}) avg={med.get('avg_pnl', 0.0):+.4f}  "
              f"2c-3c(n={small['n']}) avg={small.get('avg_pnl', 0.0):+.4f}")

    print("\nQ6. Which exit test shows best expectancy?")
    best_n = _PROMISING_N
    best = None
    for tname, trows in sorted(by_test.items()):
        m = _metrics(trows)
        if m["n"] >= best_n:
            if best is None or m["avg_pnl"] > best[1]["avg_pnl"]:
                best = (tname, m)
    if best:
        t, m = best
        print(f"  Best: {t}  avg_pnl={m['avg_pnl']:+.4f}  "
              f"WR={m['win_rate']:.1%}  PF={m['profit_factor']:.2f}  n={m['n']}")
    else:
        print(f"  No test with n ≥ {_PROMISING_N}")

    print("\nQ7. Any sub-bucket with n ≥ 50 justifying a second pass?")
    candidates: list[tuple[str, str, str, dict]] = []
    for tname, trows in sorted(by_test.items()):
        # price × age cross
        for pb in ("cheap_primary", "very_cheap"):
            for ab in (" 60- 90s", " 91-120s", "121-150s", "151-180s"):
                subset = [r for r in trows
                          if r.get("price_bucket") == pb
                          and _age_bucket(r.get("market_age_seconds")) == ab]
                m = _metrics(subset)
                if m["n"] >= _INTERESTING_N and m["avg_pnl"] > 0:
                    candidates.append((tname, pb, ab, m))
    if candidates:
        for tname, pb, ab, m in sorted(candidates, key=lambda x: -x[3]["avg_pnl"]):
            print(f"  ✓ {tname}  {pb} × {ab}  "
                  f"n={m['n']}  avg_pnl={m['avg_pnl']:+.4f}  WR={m['win_rate']:.1%}")
    else:
        print(f"  None found (threshold: n≥{_INTERESTING_N} AND avg_pnl>0).")
        # Show best anyway
        best_any: list[tuple[str, dict]] = []
        for tname, trows in sorted(by_test.items()):
            m = _metrics(trows)
            best_any.append((tname, m))
        best_any.sort(key=lambda x: -x[1]["avg_pnl"])
        if best_any:
            t, m = best_any[0]
            print(f"  Best overall: {t}  avg_pnl={m['avg_pnl']:+.4f}  n={m['n']}")

    print(f"\n{'═' * 72}")
    print("  Sample thresholds: promising=n≥25  interesting=n≥50  strong=n≥100")
    print(f"{'═' * 72}\n")


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Report for contract_value_bounce_scalp/v1 backtest runs."
    )
    p.add_argument("--run-id", default=None, type=int,
                   help="Run ID to report on (default: latest)")
    args = p.parse_args()

    run_id = args.run_id or _latest_run_id()
    if run_id is None:
        print("No backtest runs found.  Run contract_value_bounce_backtest.py first.")
        sys.exit(1)

    report(run_id)


if __name__ == "__main__":
    main()
