#!/usr/bin/env python3
"""
repricing_discrepancy_report.py — Research report for the BTC-Kalshi
repricing discrepancy hypothesis.

Reads from repricing_discrepancy_runs and repricing_discrepancy_events.

Report sections
---------------
  0. Run summary
  1. Headline: all burst events, forward repricing by Z
  2. Underreaction vs. normal reaction comparison (key test of hypothesis)
  3. N threshold breakdown (N=40/50/60)
  4. X threshold comparison (X=0.01/0.02/0.03)
  5. Side breakdown (YES vs NO)
  6. Spread bucket analysis
  7. Volatility regime analysis
  8. Time remaining analysis
  9. Best-case parameter grid summary
  10. Tradability assessment (realistic cost model)

Usage:
    python scripts/repricing_discrepancy_report.py --run-id 1
    python scripts/repricing_discrepancy_report.py  # uses latest run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all, fetch_one

# ── Formatting ────────────────────────────────────────────────────────────────

def _pct(v: Optional[float], denom: Optional[float] = None) -> str:
    if v is None:
        return "    n/a"
    if denom is not None:
        v = v / denom if denom else 0.0
    return f"{v*100:+6.1f}%" if isinstance(v, float) else f"{v:+6.1f}%"

def _c(v: Optional[float]) -> str:
    """Format as contract cents (e.g. +0.0312)."""
    return f"{v:+.4f}" if v is not None else "    n/a"

def _n(v: Optional[float]) -> str:
    return f"{int(v):,}" if v is not None else "n/a"

def _sep(char: str = "─", w: int = 72) -> None:
    print(char * w)

def _h1(title: str) -> None:
    print()
    _sep("═")
    print(f"  {title}")
    _sep("═")

def _h2(title: str) -> None:
    print()
    _sep()
    print(f"  {title}")
    _sep()

def _row(label: str, value: str, w: int = 40) -> None:
    print(f"  {label:<{w}} {value}")


# ── Metric helpers ────────────────────────────────────────────────────────────

_FWD_COLS = {10: "fwd_mid_10s", 20: "fwd_mid_20s", 30: "fwd_mid_30s",
             45: "fwd_mid_45s", 60: "fwd_mid_60s"}
_PNL_COLS = {10: "pnl_10s",     20: "pnl_20s",     30: "pnl_30s",
             45: "pnl_45s",     60: "pnl_60s"}
_MFE_COLS = {10: "fwd_mfe_10s", 20: "fwd_mfe_20s", 30: "fwd_mfe_30s",
             45: "fwd_mfe_45s", 60: "fwd_mfe_60s"}
_MAE_COLS = {10: "fwd_mae_10s", 20: "fwd_mae_20s", 30: "fwd_mae_30s",
             45: "fwd_mae_45s", 60: "fwd_mae_60s"}
_Z = [10, 20, 30, 45, 60]


def _metrics(run_id: int, where: str = "1=1", params: tuple = ()) -> dict:
    """Pull aggregate stats for a subset of events."""
    extra = f" AND run_id={run_id} AND ({where})" if where != "1=1" else f" AND run_id={run_id}"
    avgs = ", ".join(
        f"AVG({_FWD_COLS[z]}) AS avg_fwd_{z}s, "
        f"AVG({_PNL_COLS[z]}) AS avg_pnl_{z}s, "
        f"AVG({_MFE_COLS[z]}) AS avg_mfe_{z}s, "
        f"AVG({_MAE_COLS[z]}) AS avg_mae_{z}s, "
        f"SUM({_FWD_COLS[z]} > 0) AS n_pos_{z}s, "
        f"SUM({_PNL_COLS[z]} > 0) AS n_pnl_pos_{z}s"
        for z in _Z
    )
    sql = f"""
        SELECT COUNT(*) AS n,
               {avgs},
               AVG(btc_move_abs) AS avg_burst,
               AVG(contract_change) AS avg_contract_change,
               AVG(entry_sim) AS avg_entry_sim
        FROM repricing_discrepancy_events
        WHERE 1=1{extra}
    """
    row = fetch_one(sql, params)
    return row if row else {}


def _fmt_z_row(m: dict, label: str, z: int, col: str = "fwd") -> None:
    """Print one Z-window row (avg_fwd or avg_pnl)."""
    prefix = f"avg_{col}_{z}s"
    n_pos  = m.get(f"n_pos_{z}s")
    n      = m.get("n") or 0
    avg    = m.get(prefix)
    pnl    = m.get(f"avg_pnl_{z}s")
    mfe    = m.get(f"avg_mfe_{z}s")
    mae    = m.get(f"avg_mae_{z}s")
    pct_pos = f"{float(n_pos)/n*100:.0f}%" if n_pos and n else "n/a"
    print(
        f"  {label:<18} Z={z:>2}s  "
        f"avg_fwd={_c(avg)}  pnl={_c(pnl)}  "
        f"mfe={_c(mfe)}  mae={_c(mae)}  "
        f"pos={pct_pos:>5}  n={_n(n)}"
    )


# ── Sections ──────────────────────────────────────────────────────────────────

def _section_run_summary(run_id: int) -> None:
    run = fetch_one(
        "SELECT * FROM repricing_discrepancy_runs WHERE id = %s", (run_id,)
    )
    if not run:
        print(f"  No run found with id={run_id}")
        sys.exit(1)

    _h1(f"Repricing Discrepancy Backtest — Run #{run_id}")
    _row("Burst window:",        f"{run['burst_window_s']}s  "
                                  f"N thresholds: {run['n_thresholds']}  "
                                  f"X thresholds: {run['x_thresholds']}")
    _row("Z forward windows:",   str(run["z_windows"]))
    _row("Slippage model:",      str(run["slippage_mode"]))
    _row("Cooldown (per side):", f"{run['cooldown_s']}s")
    _row("Data range:",          f"{run['data_start']} → {run['data_end']}")
    _row("Markets:",             str(run["n_markets"]))
    _row("Burst events (N≥$40):", _n(run["n_burst_events"]))
    _row("Underreaction X<0.01:", _n(run["n_underreaction_x01"]))
    _row("Underreaction X<0.02:", _n(run["n_underreaction_x02"]))
    _row("Underreaction X<0.03:", _n(run["n_underreaction_x03"]))
    if run.get("notes"):
        _row("Notes:", str(run["notes"]))


def _section_headline(run_id: int) -> None:
    _h2("1. Headline — All burst events (N≥$40)")

    m = _metrics(run_id)
    if not m or not m.get("n"):
        print("  No events found.")
        return

    n = int(m["n"])
    print(f"\n  Total events: {n:,}")
    print(f"  Avg BTC burst size:       ${float(m.get('avg_burst') or 0):,.1f}")
    print(f"  Avg contract change (30s): {_c(m.get('avg_contract_change'))}")
    print()
    print(f"  {'':18}  {'avg_fwd_mid':>12}  {'avg_pnl':>10}  {'avg_mfe':>10}  {'avg_mae':>10}  {'pct_pos':>7}  n")
    _sep("-", 88)
    for z in _Z:
        _fmt_z_row(m, "all events", z)


def _section_underreaction_comparison(run_id: int) -> None:
    _h2("2. Underreaction vs Normal reaction (core hypothesis test)")

    for x_label, x_col in [("X<0.01", "underreaction_x01"),
                            ("X<0.02", "underreaction_x02"),
                            ("X<0.03", "underreaction_x03")]:
        m_under  = _metrics(run_id, f"{x_col} = 1")
        m_normal = _metrics(run_id, f"{x_col} = 0")
        m_all    = _metrics(run_id)

        n_under  = int(m_under.get("n") or 0)
        n_normal = int(m_normal.get("n") or 0)
        n_all    = int(m_all.get("n") or 0)

        print(f"\n  Threshold {x_label} — "
              f"under-react={n_under}  normal={n_normal}  total={n_all}")
        _sep("-", 88)
        print(f"  {'group':18}  {'avg_fwd_mid':>12}  {'avg_pnl':>10}  "
              f"{'avg_mfe':>10}  {'avg_mae':>10}  {'pct_pos':>7}  n")
        for z in _Z:
            _fmt_z_row(m_under,  f"under-react {x_label}", z)
        _sep("-", 88)
        for z in _Z:
            _fmt_z_row(m_normal, f"normal-react {x_label}", z)

        # Key comparison at Z=30 and Z=60
        for z in [30, 60]:
            a = m_under.get(f"avg_fwd_{z}s")
            b = m_normal.get(f"avg_fwd_{z}s")
            if a is not None and b is not None:
                delta = float(a) - float(b)
                print(f"\n  {x_label}@Z={z}s: under-react avg_fwd={_c(a)}  "
                      f"normal avg_fwd={_c(b)}  delta={_c(delta)}")


def _section_n_breakdown(run_id: int) -> None:
    _h2("3. N (BTC burst size) breakdown")

    specs = [
        ("N≥$40 (all)", "qualified_n40 = 1"),
        ("N≥$50",       "qualified_n50 = 1"),
        ("N≥$60",       "qualified_n60 = 1"),
    ]
    for label, where in specs:
        m = _metrics(run_id, where)
        n = int(m.get("n") or 0)
        if not n:
            continue
        print(f"\n  {label} — n={n:,}  avg_burst=${float(m.get('avg_burst') or 0):.1f}  "
              f"avg_change={_c(m.get('avg_contract_change'))}")
        for z in [20, 30, 60]:
            _fmt_z_row(m, label, z)


def _section_x_breakdown(run_id: int) -> None:
    _h2("4. X threshold breakdown — underreaction subsets (Z=30s, Z=60s)")

    rows = fetch_all(
        """
        SELECT
            underreaction_x01, underreaction_x02, underreaction_x03,
            COUNT(*) AS n,
            AVG(contract_change) AS avg_change,
            AVG(fwd_mid_30s)  AS avg_fwd_30,
            AVG(fwd_mid_60s)  AS avg_fwd_60,
            AVG(pnl_30s)      AS avg_pnl_30,
            AVG(pnl_60s)      AS avg_pnl_60,
            AVG(fwd_mfe_30s)  AS avg_mfe_30,
            AVG(fwd_mae_30s)  AS avg_mae_30
        FROM repricing_discrepancy_events
        WHERE run_id = %s
        GROUP BY underreaction_x01, underreaction_x02, underreaction_x03
        ORDER BY underreaction_x01, underreaction_x02, underreaction_x03
        """,
        (run_id,),
    )

    print(f"\n  {'x01':>4} {'x02':>4} {'x03':>4} {'n':>6}  "
          f"{'avg_chg':>9}  {'fwd30':>9}  {'fwd60':>9}  "
          f"{'pnl30':>9}  {'pnl60':>9}  {'mfe30':>9}  {'mae30':>9}")
    _sep("-", 90)
    for r in rows:
        def _b(v) -> str: return " Y" if v else " N"
        print(
            f"  {_b(r['underreaction_x01'])} {_b(r['underreaction_x02'])} {_b(r['underreaction_x03'])}"
            f"  {int(r['n']):>6}"
            f"  {_c(r['avg_change']):>9}"
            f"  {_c(r['avg_fwd_30']):>9}  {_c(r['avg_fwd_60']):>9}"
            f"  {_c(r['avg_pnl_30']):>9}  {_c(r['avg_pnl_60']):>9}"
            f"  {_c(r['avg_mfe_30']):>9}  {_c(r['avg_mae_30']):>9}"
        )


def _section_side(run_id: int) -> None:
    _h2("5. Side breakdown (YES vs NO)")

    for side in ("YES", "NO"):
        for x_label, x_col in [("all", "1=1"),
                                ("under x02", "underreaction_x02=1")]:
            m = _metrics(run_id, f"implied_side='{side}' AND {x_col}")
            n = int(m.get("n") or 0)
            if not n:
                continue
            print(f"\n  Side={side} ({x_label}) — n={n:,}")
            for z in [20, 30, 60]:
                _fmt_z_row(m, f"{side} {x_label}", z)


def _section_spread(run_id: int) -> None:
    _h2("6. Spread bucket analysis (underreaction X<0.02, Z=30s)")

    rows = fetch_all(
        """
        SELECT spread_bucket,
               COUNT(*) AS n,
               SUM(underreaction_x02) AS n_under,
               AVG(CASE WHEN underreaction_x02=1 THEN fwd_mid_30s END) AS under_fwd30,
               AVG(CASE WHEN underreaction_x02=1 THEN pnl_30s END)     AS under_pnl30,
               AVG(CASE WHEN underreaction_x02=0 THEN fwd_mid_30s END) AS normal_fwd30
        FROM repricing_discrepancy_events
        WHERE run_id = %s
        GROUP BY spread_bucket
        ORDER BY spread_bucket
        """,
        (run_id,),
    )

    print(f"\n  {'spread_bucket':>12}  {'n':>6}  {'n_under':>8}  "
          f"{'under_fwd30':>12}  {'under_pnl30':>12}  {'normal_fwd30':>12}")
    _sep("-", 70)
    for r in rows:
        print(
            f"  {str(r['spread_bucket'] or 'n/a'):>12}"
            f"  {int(r['n']):>6}"
            f"  {int(r['n_under'] or 0):>8}"
            f"  {_c(r['under_fwd30']):>12}"
            f"  {_c(r['under_pnl30']):>12}"
            f"  {_c(r['normal_fwd30']):>12}"
        )


def _section_volatility(run_id: int) -> None:
    _h2("7. Volatility regime analysis (underreaction X<0.02, Z=30s)")

    rows = fetch_all(
        """
        SELECT volatility_regime,
               COUNT(*) AS n,
               SUM(underreaction_x02) AS n_under,
               AVG(CASE WHEN underreaction_x02=1 THEN fwd_mid_30s END) AS under_fwd30,
               AVG(CASE WHEN underreaction_x02=1 THEN pnl_30s END)     AS under_pnl30,
               AVG(CASE WHEN underreaction_x02=0 THEN fwd_mid_30s END) AS normal_fwd30
        FROM repricing_discrepancy_events
        WHERE run_id = %s
        GROUP BY volatility_regime
        ORDER BY FIELD(volatility_regime,'calm','normal','elevated','violent','unknown')
        """,
        (run_id,),
    )

    print(f"\n  {'vol_regime':>10}  {'n':>6}  {'n_under':>8}  "
          f"{'under_fwd30':>12}  {'under_pnl30':>12}  {'normal_fwd30':>12}")
    _sep("-", 65)
    for r in rows:
        print(
            f"  {str(r['volatility_regime'] or 'n/a'):>10}"
            f"  {int(r['n']):>6}"
            f"  {int(r['n_under'] or 0):>8}"
            f"  {_c(r['under_fwd30']):>12}"
            f"  {_c(r['under_pnl30']):>12}"
            f"  {_c(r['normal_fwd30']):>12}"
        )


def _section_time_remaining(run_id: int) -> None:
    _h2("8. Time remaining buckets (underreaction X<0.02, Z=30s)")

    rows = fetch_all(
        """
        SELECT
            CASE
                WHEN time_remaining_s >= 600 THEN '>10min'
                WHEN time_remaining_s >= 300 THEN '5-10min'
                WHEN time_remaining_s >= 120 THEN '2-5min'
                WHEN time_remaining_s >= 60  THEN '1-2min'
                ELSE '<1min'
            END                                            AS tte_bucket,
            COUNT(*)                                       AS n,
            SUM(underreaction_x02)                         AS n_under,
            AVG(CASE WHEN underreaction_x02=1 THEN fwd_mid_30s END) AS under_fwd30,
            AVG(CASE WHEN underreaction_x02=1 THEN pnl_30s END)     AS under_pnl30
        FROM repricing_discrepancy_events
        WHERE run_id = %s
        GROUP BY tte_bucket
        ORDER BY MIN(time_remaining_s) DESC
        """,
        (run_id,),
    )

    print(f"\n  {'tte_bucket':>10}  {'n':>6}  {'n_under':>8}  "
          f"{'under_fwd30':>12}  {'under_pnl30':>12}")
    _sep("-", 55)
    for r in rows:
        print(
            f"  {str(r['tte_bucket'] or 'n/a'):>10}"
            f"  {int(r['n']):>6}"
            f"  {int(r['n_under'] or 0):>8}"
            f"  {_c(r['under_fwd30']):>12}"
            f"  {_c(r['under_pnl30']):>12}"
        )


def _section_grid_summary(run_id: int) -> None:
    _h2("9. Parameter grid summary — best avg_fwd by (N, X, Z) combination")

    combos = []
    for n_thresh in [40, 50, 60]:
        for x_col, x_label in [("underreaction_x01", "X01"),
                                ("underreaction_x02", "X02"),
                                ("underreaction_x03", "X03")]:
            n_flag = "qualified_n50=1" if n_thresh == 50 else (
                     "qualified_n60=1" if n_thresh == 60 else "qualified_n40=1")
            m = _metrics(run_id, f"{n_flag} AND {x_col}=1")
            n_evt = int(m.get("n") or 0)
            if n_evt == 0:
                continue
            for z in _Z:
                fwd = m.get(f"avg_fwd_{z}s")
                pnl = m.get(f"avg_pnl_{z}s")
                mfe = m.get(f"avg_mfe_{z}s")
                combos.append((
                    n_thresh, x_label, z, n_evt,
                    float(fwd) if fwd else None,
                    float(pnl) if pnl else None,
                    float(mfe) if mfe else None,
                ))

    if not combos:
        print("  No data.")
        return

    combos.sort(key=lambda x: x[4] or float("-inf"), reverse=True)
    print(f"\n  {'N':>5}  {'X':>4}  {'Z':>5}  {'n':>6}  "
          f"{'avg_fwd':>10}  {'avg_pnl':>10}  {'avg_mfe':>10}")
    _sep("-", 58)
    for n_thresh, x_label, z, n_evt, fwd, pnl, mfe in combos[:20]:
        print(
            f"  N≥${n_thresh:<3}  {x_label}  Z={z:>2}s"
            f"  {n_evt:>6}"
            f"  {_c(fwd):>10}"
            f"  {_c(pnl):>10}"
            f"  {_c(mfe):>10}"
        )


def _section_tradability(run_id: int) -> None:
    _h2("10. Tradability assessment (realistic slippage ~2c round-trip)")

    print("""
  Break-even assumptions (realistic):
    entry = ask + 0.01 slippage
    exit  = bid - 0.01 slippage
    round-trip cost ≈ spread + 0.02

  To be tradable, avg_pnl must be POSITIVE after slippage and spread.
  avg_mfe > avg_spread means the move reaches the entry price.
  Positive avg_pnl at Z=30s is the minimum bar; Z=60s shows durability.
""")

    run = fetch_one("SELECT * FROM repricing_discrepancy_runs WHERE id=%s", (run_id,))
    slip_mode = run["slippage_mode"] if run else "unknown"
    print(f"  Slippage mode used: {slip_mode}")

    # Evaluate best combo at X=0.02 (middle threshold), various N and Z
    results: list[tuple] = []
    for n_thresh in [40, 50, 60]:
        n_flag = ("qualified_n50=1" if n_thresh == 50 else
                  "qualified_n60=1" if n_thresh == 60 else "qualified_n40=1")
        m_under  = _metrics(run_id, f"{n_flag} AND underreaction_x02=1")
        m_all    = _metrics(run_id, n_flag)
        n_u = int(m_under.get("n") or 0)
        n_a = int(m_all.get("n") or 0)
        for z in [20, 30, 60]:
            pnl   = m_under.get(f"avg_pnl_{z}s")
            fwd   = m_under.get(f"avg_fwd_{z}s")
            mfe   = m_under.get(f"avg_mfe_{z}s")
            mae   = m_under.get(f"avg_mae_{z}s")
            results.append((n_thresh, z, n_u, n_a,
                            float(pnl) if pnl else None,
                            float(fwd) if fwd else None,
                            float(mfe) if mfe else None,
                            float(mae) if mae else None))

    print(f"\n  Under-react X<0.02 subset:")
    print(f"  {'N':>5}  {'Z':>4}  {'n_under':>8}  "
          f"{'avg_pnl':>9}  {'avg_fwd':>9}  {'avg_mfe':>9}  {'avg_mae':>9}  verdict")
    _sep("-", 85)
    for n_thresh, z, n_u, n_a, pnl, fwd, mfe, mae in results:
        verdict = "?? (no data)"
        if pnl is not None:
            if pnl > 0.01:
                verdict = "PROMISING — review carefully"
            elif pnl > 0:
                verdict = "marginal — barely above cost"
            elif pnl > -0.01:
                verdict = "borderline — within noise"
            else:
                verdict = "NOT TRADABLE — negative after costs"
        print(
            f"  N≥${n_thresh:<3}  Z={z:>2}s"
            f"  {n_u:>8}"
            f"  {_c(pnl):>9}"
            f"  {_c(fwd):>9}"
            f"  {_c(mfe):>9}"
            f"  {_c(mae):>9}"
            f"  {verdict}"
        )

    print("""
  Verdict key:
    PROMISING             — avg_pnl > +0.01 (>1c net after slip); worth paper-trading
    marginal              — avg_pnl > 0 but < 0.01; investigate subset conditions
    borderline            — avg_pnl in [-0.01, 0]; likely noise
    NOT TRADABLE          — negative after realistic costs; hypothesis not supported

  Note: This analysis uses mid-price for detection and bid/ask for P&L.
  Real tradability requires at least 50 events with consistent positive pnl
  across MULTIPLE volatility regimes and time-of-day buckets.
""")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Repricing discrepancy research report."
    )
    ap.add_argument("--run-id", type=int, help="Run ID (default: latest)")
    args = ap.parse_args()

    if args.run_id:
        run_id = args.run_id
    else:
        row = fetch_one(
            "SELECT id FROM repricing_discrepancy_runs ORDER BY id DESC LIMIT 1"
        )
        if not row:
            print("No runs found. Run the backtest first.", file=sys.stderr)
            sys.exit(1)
        run_id = int(row["id"])
        print(f"Using latest run: #{run_id}")

    _section_run_summary(run_id)
    _section_headline(run_id)
    _section_underreaction_comparison(run_id)
    _section_n_breakdown(run_id)
    _section_x_breakdown(run_id)
    _section_side(run_id)
    _section_spread(run_id)
    _section_volatility(run_id)
    _section_time_remaining(run_id)
    _section_grid_summary(run_id)
    _section_tradability(run_id)

    print()


if __name__ == "__main__":
    main()
