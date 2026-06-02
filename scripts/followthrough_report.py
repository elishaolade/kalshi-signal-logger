#!/usr/bin/env python3
"""
followthrough_report.py — A-vs-B report for a follow-through filter backtest.

Reads `followthrough_backtest_trades` for one `followthrough_backtest_runs` row
(latest by default) and, per exit profile, compares:

    Variant A  base_no_followthrough   — ALL base entries
    Variant B  base_plus_followthrough — entries with followthrough_confirmed=1

Metrics per variant: total trades, win rate, expectancy, profit factor, avg win,
avg loss, median PnL, stop-loss/timeout/take-profit frequency, max drawdown
approximation, average spread.  Plus breakdowns by hour_block, market_phase,
spread_bucket, and scalping_valid_window (true/false).

Then it answers the hypothesis's research questions and grades the result
against the promising / stronger-paper / falsification standards.

Backtest results live in their own tables, entirely separate from live
paper_trades.  PAPER-ONLY RESEARCH — no live trading, no order execution.  This
is a hypothesis test, not a trade recommendation.

Usage:
    python scripts/followthrough_report.py [--run-id N]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all, fetch_one

_W = 104

# Proof standards (from the hypothesis spec).
_PROMISING_MIN_TRADES = 50
_STRONG_MIN_TRADES    = 100
_STRONG_MIN_PF        = 1.20
_STRONG_MIN_WIN_RATE  = 0.58


# ── Loading ──────────────────────────────────────────────────────────────────

def _load_run(run_id: Optional[int]) -> Optional[dict]:
    if run_id is not None:
        return fetch_one("SELECT * FROM followthrough_backtest_runs WHERE id = %s", (run_id,))
    return fetch_one("SELECT * FROM followthrough_backtest_runs ORDER BY id DESC LIMIT 1")


def _load_trades(run_id: int) -> list[dict]:
    return fetch_all(
        "SELECT * FROM followthrough_backtest_trades WHERE run_id = %s "
        "ORDER BY entry_time ASC, id ASC",
        (run_id,),
    )


def _fv(r: dict, key: str) -> Optional[float]:
    v = r.get(key)
    return None if v is None else float(v)


def _truthy(v: Any) -> bool:
    return bool(int(v)) if v is not None else False


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0}

    pnls   = [float(r["pnl"]) for r in rows if r.get("pnl") is not None]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_win  = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)

    cum = peak = max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    reasons = Counter(r.get("exit_reason") or "unknown" for r in rows)
    spreads = [s for s in (_fv(r, "spread") for r in rows) if s is not None]

    return {
        "n":             n,
        "win_rate":      (len(wins) / len(pnls)) if pnls else 0.0,
        "total_pnl":     sum(pnls),
        "expectancy":    mean(pnls) if pnls else 0.0,
        "med_pnl":       median(pnls) if pnls else 0.0,
        "avg_win":       mean(wins) if wins else 0.0,
        "avg_loss":      mean(losses) if losses else 0.0,
        "profit_factor": profit_factor,
        "max_drawdown":  max_dd,
        "sl_freq":       (reasons.get("stop_loss", 0) / n),
        "tp_freq":       (reasons.get("take_profit", 0) / n),
        "timeout_freq":  (reasons.get("timeout", 0) / n),
        "avg_spread":    mean(spreads) if spreads else 0.0,
        "reasons":       reasons,
    }


def maturity(n: int) -> str:
    if n == 0:    return "no data"
    if n < 30:    return "VERY SMALL sample — directional only"
    if n < _PROMISING_MIN_TRADES: return "small sample — treat as a clue"
    if n < _STRONG_MIN_TRADES:    return "moderate sample"
    return "usable sample"


# ── Bucketers ──────────────────────────────────────────────────────────────────

def _b_hour(r: dict) -> str:
    return r.get("entry_hour_block") or "N/A"


def _b_phase(r: dict) -> str:
    return r.get("market_phase") or "unknown"


def _b_spread_bucket(r: dict) -> str:
    return r.get("spread_bucket") or "N/A"


def _b_scalp_window(r: dict) -> str:
    v = r.get("scalping_valid_window")
    if v is None:
        return "unknown"
    return "valid_window" if _truthy(v) else "off_window"


_PHASE_ORDER   = ["early", "mid", "late", "near_expiry", "unknown"]
_SPREAD_ORDER  = ["<0.01", "0.01-0.02", "0.02-0.03", "0.03+", "N/A"]
_SCALP_ORDER   = ["valid_window", "off_window", "unknown"]


def breakdown(
    rows: list[dict], key_fn: Callable[[dict], str],
    order: Optional[list[str]] = None,
) -> list[tuple[str, dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    keys = (
        [k for k in order if k in groups] + [k for k in groups if order and k not in order]
        if order else sorted(groups)
    )
    return [(k, compute_metrics(groups[k])) for k in keys]


# ── Rendering ────────────────────────────────────────────────────────────────

_HEADER = (f"{'group':<20}{'n':>5}{'win%':>7}{'expect':>9}{'PF':>7}"
           f"{'avgWin':>9}{'avgLoss':>9}{'medPnL':>9}{'SL%':>7}{'TP%':>7}"
           f"{'TO%':>7}{'maxDD':>8}{'avgSpr':>8}")


def _row_line(label: str, m: dict) -> str:
    if m.get("n", 0) == 0:
        return (f"{label:<20}{0:>5}" + "".join(f"{'—':>{w}}" for w in
                (7, 9, 7, 9, 9, 9, 7, 7, 7, 8, 8)))
    pf = m["profit_factor"]
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (
        f"{label:<20}{m['n']:>5}{m['win_rate']*100:>6.1f}%"
        f"{m['expectancy']:>+9.4f}{pf_s:>7}"
        f"{m['avg_win']:>+9.4f}{m['avg_loss']:>+9.4f}{m['med_pnl']:>+9.4f}"
        f"{m['sl_freq']*100:>6.1f}%{m['tp_freq']*100:>6.1f}%{m['timeout_freq']*100:>6.1f}%"
        f"{m['max_drawdown']:>8.4f}{m['avg_spread']:>8.4f}"
    )


def _md_row(label: str, m: dict) -> str:
    if m.get("n", 0) == 0:
        return f"| {label} | 0 | — | — | — | — | — | — | — | — | — | — | — |"
    pf = m["profit_factor"]
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (
        f"| {label} | {m['n']} | {m['win_rate']*100:.1f}% | {m['expectancy']:+.4f} "
        f"| {pf_s} | {m['avg_win']:+.4f} | {m['avg_loss']:+.4f} | {m['med_pnl']:+.4f} "
        f"| {m['sl_freq']*100:.1f}% | {m['tp_freq']*100:.1f}% | {m['timeout_freq']*100:.1f}% "
        f"| {m['max_drawdown']:.4f} | {m['avg_spread']:.4f} |"
    )


_MD_HEADER = [
    "| group | n | win% | expect | PF | avgWin | avgLoss | medPnL | SL% | TP% | TO% | maxDD | avgSpr |",
    "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
]


def _print_table(title: str, bd: list[tuple[str, dict]], note: str = "") -> list[str]:
    print(f"\n{'─' * _W}")
    print(f"  {title}")
    if note:
        print(f"  {note}")
    print(f"{'─' * _W}")
    print(_HEADER)
    print("-" * _W)
    md = [f"### {title}", ""]
    if note:
        md += [f"_{note}_", ""]
    md += list(_MD_HEADER)
    for label, m in bd:
        print(_row_line(label, m))
        md.append(_md_row(label, m))
    md.append("")
    return md


# ── Verdicts ───────────────────────────────────────────────────────────────────

def _pf_s(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _verdict_block(profile: str, a: dict, b: dict) -> list[str]:
    """Answer the hypothesis research questions + grade for one exit profile."""
    out: list[str] = []

    def emit(line: str) -> None:
        print(f"    {line}")
        out.append(line)

    print(f"\n{'═' * _W}\n  VERDICT — profile {profile}  (A=base, B=base+followthrough)\n{'═' * _W}")
    out += [f"## Verdict — profile `{profile}`", "",
            "A = base_no_followthrough · B = base_plus_followthrough", ""]

    if a.get("n", 0) == 0 or b.get("n", 0) == 0:
        emit("Insufficient data in one variant — cannot compare.")
        out.append("")
        return out

    d_exp = b["expectancy"] - a["expectancy"]
    d_pf  = (b["profit_factor"] - a["profit_factor"]
             if a["profit_factor"] != float("inf") and b["profit_factor"] != float("inf")
             else float("nan"))
    d_sl  = b["sl_freq"] - a["sl_freq"]
    retention = (b["n"] / a["n"]) if a["n"] else 0.0

    emit(f"Q1 expectancy: A={a['expectancy']:+.4f}  B={b['expectancy']:+.4f}  "
         f"Δ={d_exp:+.4f}  → {'IMPROVES' if d_exp > 0 else 'no improvement'}")
    emit(f"Q2 profit factor: A={_pf_s(a['profit_factor'])}  B={_pf_s(b['profit_factor'])}  "
         f"→ {'IMPROVES' if b['profit_factor'] > a['profit_factor'] else 'no improvement'}")
    emit(f"Q3 stop-loss freq: A={a['sl_freq']*100:.1f}%  B={b['sl_freq']*100:.1f}%  "
         f"Δ={d_sl*100:+.1f}pp  → {'REDUCED' if d_sl < 0 else 'not reduced'}")
    emit(f"Q4 false entries (trades): A n={a['n']}  B n={b['n']}  "
         f"retention={retention*100:.1f}%")
    avg_pnl_improves = d_exp > 0
    emit(f"Q5 trade-count vs avg PnL: B keeps {retention*100:.1f}% of trades; "
         f"avg PnL {'improved' if avg_pnl_improves else 'did NOT improve'} "
         f"→ {'acceptable filter' if avg_pnl_improves else 'over-filtering without payoff'}")

    # Q6 — does it only work in valid scalping windows?  Caller passes nothing
    # here; the scalping_valid_window breakdown table above answers it visually.
    emit("Q6 valid-window dependence: see the [scalping_valid_window] breakdown table above.")

    # ── Grading ───────────────────────────────────────────────────────────────
    promising = (
        b["n"] >= _PROMISING_MIN_TRADES
        and d_exp > 0
        and b["profit_factor"] > a["profit_factor"]
        and d_sl < 0
        and b["expectancy"] > a["expectancy"]   # avg PnL improves after slippage
    )
    strong = (
        b["n"] >= _STRONG_MIN_TRADES
        and b["expectancy"] > 0
        and b["profit_factor"] > _STRONG_MIN_PF
        and b["win_rate"] >= _STRONG_MIN_WIN_RATE
    )
    falsified = (
        d_exp <= 0
        or b["profit_factor"] <= 1.00
        or (retention < 0.5 and not avg_pnl_improves)
        or d_sl >= 0
    )

    emit("")
    if strong:
        grade = "STRONG (paper) — meets the stronger paper standard"
    elif promising:
        grade = "PROMISING — meets the promising standard"
    elif falsified:
        grade = "FALSIFIED / NOT SUPPORTED — fails the hypothesis criteria"
    else:
        grade = "INCONCLUSIVE — neither promising nor clearly falsified"
    emit(f"GRADE: {grade}")
    emit("(PAPER-ONLY hypothesis test — not a trade recommendation; no live trading.)")
    out += ["", f"**GRADE: {grade}**", "",
            "_PAPER-ONLY hypothesis test — not a trade recommendation; no live trading._", ""]
    return out


# ── Output ───────────────────────────────────────────────────────────────────

def write_md(lines: list[str], run_id: int) -> Path:
    out_dir = Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"followthrough_report_run{run_id}_{date.today().isoformat()}.md"
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="A-vs-B report for a follow-through backtest run")
    ap.add_argument("--run-id", type=int, default=None,
                    help="followthrough_backtest_runs.id (default: latest run)")
    args = ap.parse_args()

    run = _load_run(args.run_id)
    if run is None:
        print("\nNo follow-through backtest runs found. "
              "Run scripts/followthrough_backtest.py first.\n")
        return

    run_id = int(run["id"])
    trades = _load_trades(run_id)

    print(f"\n{'═' * _W}")
    print(f"  Follow-Through Filter Report — run #{run_id}  "
          f"({run['rule_name']}/{run['rule_version']})")
    print(f"{'═' * _W}")
    print("  ⚠  PAPER-ONLY RESEARCH — replayed from stored snapshots, no live trading.")
    print("     Hypothesis: NO-side scalps perform better when contract-price follow-through confirms the entry.")
    print(f"     Fill model: entry = ask+slip, exit = bid-slip   (slippage={run['slippage_mode']})")
    print(f"     Coverage: {run.get('data_start')} → {run.get('data_end')}  "
          f"markets={run.get('n_markets')} snapshots={run.get('n_snapshots')}")
    print(f"     Base entries: {run.get('n_base_entries')}   "
          f"Confirmed (B): {run.get('n_confirmed_entries')}   Profile-trades: {len(trades)}")
    print(f"  Generated: {date.today().isoformat()}")

    md: list[str] = [
        f"# Follow-Through Filter Report — run #{run_id} "
        f"({run['rule_name']}/{run['rule_version']})",
        "",
        "⚠ **PAPER-ONLY RESEARCH** — replayed from stored snapshots, no live trading. "
        "This is a hypothesis test, not a trade recommendation.",
        "",
        "Hypothesis: a NO-side scalp entry is more valid when the contract price keeps "
        "moving in the bought direction (or holds most of its gain) rather than spiking once "
        "and fading.",
        "",
        f"- Fill model: entry = ask+slip, exit = bid-slip (slippage = `{run['slippage_mode']}`)",
        f"- Coverage: {run.get('data_start')} → {run.get('data_end')}",
        f"- Markets: {run.get('n_markets')}  ·  Snapshots: {run.get('n_snapshots')}",
        f"- Base entries (A): {run.get('n_base_entries')}  ·  "
        f"Confirmed (B): {run.get('n_confirmed_entries')}  ·  Profile-trades: {len(trades)}",
        f"- Generated: {date.today().isoformat()}",
        "",
        "Variants: **A = base_no_followthrough** (all base entries) · "
        "**B = base_plus_followthrough** (followthrough_confirmed = 1).",
        "",
    ]

    if not trades:
        print("\n  No simulated trades for this run.\n")
        md += ["_No simulated trades for this run._", ""]
        path = write_md(md, run_id)
        print(f"Report written → {path}\n")
        return

    profiles = sorted({r["exit_profile"] for r in trades})

    # 1. Headline: A vs B per profile.
    headline: list[tuple[str, dict]] = []
    for p in profiles:
        prows = [r for r in trades if r["exit_profile"] == p]
        a = prows
        b = [r for r in prows if _truthy(r.get("followthrough_confirmed"))]
        headline.append((f"{p} · A", compute_metrics(a)))
        headline.append((f"{p} · B", compute_metrics(b)))
    md += _print_table("Headline — A (base) vs B (base+followthrough) by exit profile",
                       headline,
                       note="A = all base entries · B = followthrough_confirmed subset")

    # 2. Per-profile detail + breakdowns (A and B side by side) + verdict.
    for p in profiles:
        prows = [r for r in trades if r["exit_profile"] == p]
        a_rows = prows
        b_rows = [r for r in prows if _truthy(r.get("followthrough_confirmed"))]
        a = compute_metrics(a_rows)
        b = compute_metrics(b_rows)

        print(f"\n{'═' * _W}\n  PROFILE: {p}   "
              f"(A n={len(a_rows)}  ·  B n={len(b_rows)})\n{'═' * _W}")
        md += [f"## Profile: `{p}`  (A n={len(a_rows)} · B n={len(b_rows)}) — "
               f"{maturity(len(b_rows))} (B)", ""]

        for variant, vrows in (("A base", a_rows), ("B base+ft", b_rows)):
            md += _print_table(f"[{p}] {variant} — by hour block",
                               breakdown(vrows, _b_hour))
            md += _print_table(f"[{p}] {variant} — by market phase",
                               breakdown(vrows, _b_phase, _PHASE_ORDER))
            md += _print_table(f"[{p}] {variant} — by spread bucket",
                               breakdown(vrows, _b_spread_bucket, _SPREAD_ORDER))
            md += _print_table(f"[{p}] {variant} — by scalping_valid_window",
                               breakdown(vrows, _b_scalp_window, _SCALP_ORDER))

        md += _verdict_block(p, a, b)

    print(f"\n{'═' * _W}\n")
    path = write_md(md, run_id)
    print(f"Report written → {path}\n")


if __name__ == "__main__":
    main()
