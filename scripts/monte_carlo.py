#!/usr/bin/env python3
"""
Monte Carlo simulation of paper-trade equity curves.

Loads closed paper_trades from MySQL, shuffles trade order N times, and
reports equity-curve statistics under two position-sizing models.

Sizing modes
------------
flat
    Every trade uses the raw dollar PnL stored in the database.
    Equity shifts by that fixed dollar amount each trade.

snowball
    Position size compounds with account profits:
        profit_frac = max(0, (equity − start) / start)
        risk_pct    = min(BASE_RISK + ADDON_RATE × profit_frac, MAX_RISK)
        n_contracts = risk_pct × equity / entry_price
        trade_$     = n_contracts × raw_pnl
    BASE_RISK = 2 %, ADDON_RATE = 25 %, MAX_RISK = 3 %

Usage
-----
    python scripts/monte_carlo.py
    python scripts/monte_carlo.py --strategy cheap_reversal_scalp
    python scripts/monte_carlo.py --balance 5000 --sims 20000 --seed 7
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all

# ── Snowball parameters ────────────────────────────────────────────────────────

_BASE_RISK  = 0.02   # 2 % of current equity per trade
_ADDON_RATE = 0.25   # 25 % of profit-fraction added on top of base
_MAX_RISK   = 0.03   # 3 % cap

# ── CLI defaults ───────────────────────────────────────────────────────────────

N_SIMS_DEFAULT  = 10_000
BALANCE_DEFAULT = 1_000.0
SEED_DEFAULT    = 42


# ── Data loading ───────────────────────────────────────────────────────────────

def load_trades(strategy: Optional[str]) -> tuple[np.ndarray, np.ndarray, str]:
    """
    Returns (pnls, entry_prices, label).  Both arrays share the same length.
    """
    if strategy:
        rows = fetch_all(
            """
            SELECT CAST(pnl          AS DOUBLE) AS pnl,
                   CAST(entry_price  AS DOUBLE) AS ep
            FROM   paper_trades
            WHERE  exit_time IS NOT NULL
              AND  pnl IS NOT NULL
              AND  followed_rules = TRUE
              AND  rule_name = %s
            ORDER BY entry_time
            """,
            (strategy,),
        )
        label = strategy
    else:
        rows = fetch_all(
            """
            SELECT CAST(pnl          AS DOUBLE) AS pnl,
                   CAST(entry_price  AS DOUBLE) AS ep
            FROM   paper_trades
            WHERE  exit_time IS NOT NULL
              AND  pnl IS NOT NULL
              AND  followed_rules = TRUE
            ORDER BY entry_time
            """,
        )
        label = "ALL"

    pnls = np.array([float(r["pnl"]) for r in rows], dtype=np.float64)
    eps  = np.array([float(r["ep"])  for r in rows], dtype=np.float64)

    bad = eps <= 0
    if bad.any():
        print(f"  [warn] dropped {int(bad.sum())} trade(s) with zero entry price")
        pnls = pnls[~bad]
        eps  = eps[~bad]

    return pnls, eps, label


# ── Low-level helpers ──────────────────────────────────────────────────────────

def _shuffle_idx(n_trades: int, n_sims: int, rng: np.random.Generator) -> np.ndarray:
    """Return (n_sims, n_trades) int array; each row is an independent permutation."""
    base = np.tile(np.arange(n_trades), (n_sims, 1))
    return rng.permuted(base, axis=1)


def _max_streaks(is_loss: np.ndarray) -> np.ndarray:
    """
    Vectorised max consecutive-loss streak per simulation.

    Loops over the trade axis (typically ~200 steps), broadcasting over the
    simulation axis in each step.  Fast without needing numba or scipy.

    Args:
        is_loss: (n_sims, n_trades) boolean array.

    Returns:
        (n_sims,) int32 array of per-simulation max streak lengths.
    """
    cur  = np.zeros(is_loss.shape[0], dtype=np.int32)
    best = np.zeros(is_loss.shape[0], dtype=np.int32)
    il   = is_loss.astype(np.int32)
    for j in range(is_loss.shape[1]):
        cur = (cur + 1) * il[:, j]
        np.maximum(best, cur, out=best)
    return best


def _hist_streak(pnls: np.ndarray) -> int:
    """Max consecutive losing trades in the original (unshuffled) sequence."""
    cur = best = 0
    for p in pnls:
        if p < 0:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


# ── Simulation engines ─────────────────────────────────────────────────────────

def run_flat(
    pnls:    np.ndarray,
    sb:      float,
    n_sims:  int,
    rng:     np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Flat sizing: each trade moves equity by its raw PnL dollar amount.

    Returns:
        equity:   (n_sims, n_trades+1)  — column 0 is the starting balance.
        shuffled: (n_sims, n_trades)    — shuffled raw PnLs (for streak stats).
    """
    idx      = _shuffle_idx(len(pnls), n_sims, rng)
    shuffled = pnls[idx]
    equity   = np.empty((n_sims, len(pnls) + 1))
    equity[:, 0]  = sb
    equity[:, 1:] = sb + np.cumsum(shuffled, axis=1)
    return equity, shuffled


def run_snowball(
    pnls:   np.ndarray,
    eps:    np.ndarray,
    sb:     float,
    n_sims: int,
    rng:    np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Snowball sizing: risk fraction grows with profit.

    For each trade j:
        profit_frac  = max(0, (equity - sb) / sb)
        risk_pct     = min(BASE_RISK + ADDON_RATE * profit_frac, MAX_RISK)
        n_contracts  = risk_pct * equity / entry_price
        dollar_pnl   = n_contracts * raw_pnl

    Returns:
        equity:   (n_sims, n_trades+1)
        shuffled: (n_sims, n_trades)  — raw PnL signs (for streak stats).
    """
    n = len(pnls)
    idx = _shuffle_idx(n, n_sims, rng)
    sp  = pnls[idx]   # shuffled raw pnls   (n_sims, n)
    se  = eps[idx]    # shuffled entry px   (n_sims, n)

    equity = np.empty((n_sims, n + 1))
    equity[:, 0] = sb

    for j in range(n):
        bal = equity[:, j]
        pf  = np.maximum(0.0, (bal - sb) / sb)
        rf  = np.minimum(_BASE_RISK + _ADDON_RATE * pf, _MAX_RISK)
        nc  = rf * bal / se[:, j]
        equity[:, j + 1] = np.maximum(0.0, bal + nc * sp[:, j])

    return equity, sp


# ── Statistics ─────────────────────────────────────────────────────────────────

_MILESTONES_PCT = [20, 40, 60, 80, 100]


def compute_stats(
    equity:   np.ndarray,   # (n_sims, n_trades+1)
    shuffled: np.ndarray,   # (n_sims, n_trades) raw pnls
    sb:       float,
) -> dict:
    """Compute all output metrics from a completed simulation run."""
    n_trades = shuffled.shape[1]
    final    = equity[:, -1]
    min_eq   = equity.min(axis=1)

    # Max drawdown expressed as a fraction of starting balance (negative values)
    peak    = np.maximum.accumulate(equity, axis=1)
    dd_frac = (equity - peak) / sb
    mdd     = dd_frac.min(axis=1)

    # Losing streak distribution
    streaks = _max_streaks(shuffled < 0)

    # Equity path at five milestones (20 / 40 / 60 / 80 / 100 % through trades)
    path = []
    for pct in _MILESTONES_PCT:
        col = max(1, int(n_trades * pct / 100))
        path.append((
            pct,
            float(np.percentile(equity[:, col],  5)),
            float(np.percentile(equity[:, col], 50)),
            float(np.percentile(equity[:, col], 95)),
        ))

    return {
        "final_p5":    float(np.percentile(final,  5)),
        "final_p50":   float(np.percentile(final, 50)),
        "final_p95":   float(np.percentile(final, 95)),
        "prob_profit": float((final > sb).mean()),
        "mdd_p50":     float(np.percentile(mdd, 50)),
        "mdd_p5":      float(np.percentile(mdd,  5)),
        "mdd_p95":     float(np.percentile(mdd, 95)),
        "ruin_25":     float((min_eq < sb * 0.75).mean()),
        "ruin_50":     float((min_eq < sb * 0.50).mean()),
        "ruin_75":     float((min_eq < sb * 0.25).mean()),
        "streak_p50":  int(np.percentile(streaks, 50)),
        "streak_p95":  int(np.percentile(streaks, 95)),
        "streak_p99":  int(np.percentile(streaks, 99)),
        "path":        path,
    }


# ── Console output ─────────────────────────────────────────────────────────────

_W = 70


def _pct_chg(v: float, sb: float) -> str:
    return f"{(v - sb) / sb * 100:+.1f}%"


def _print_mode(name: str, s: dict, sb: float) -> None:
    print(f"\n{'─' * _W}")
    print(f"  {name}")
    print(f"{'─' * _W}")

    print("\n  Ending balance")
    print(f"    Median   (p50):   ${s['final_p50']:>10,.2f}  ({_pct_chg(s['final_p50'], sb)})")
    print(f"    Worst 5% (p5):    ${s['final_p5']:>10,.2f}  ({_pct_chg(s['final_p5'],  sb)})")
    print(f"    Best 5%  (p95):   ${s['final_p95']:>10,.2f}  ({_pct_chg(s['final_p95'], sb)})")
    print(f"    Probability of profit:  {s['prob_profit'] * 100:.1f}%")

    print("\n  Max drawdown  (% of starting balance)")
    print(f"    Typical   (p50):  {s['mdd_p50'] * 100:+.1f}%")
    print(f"    Bad case   (p5):  {s['mdd_p5']  * 100:+.1f}%")
    print(f"    Best case (p95):  {s['mdd_p95'] * 100:+.1f}%")

    print("\n  Ruin risk  (equity ever falls to or below threshold)")
    print(f"    Lose 25%  (< ${sb * 0.75:>8,.0f}):  {s['ruin_25'] * 100:.2f}%")
    print(f"    Lose 50%  (< ${sb * 0.50:>8,.0f}):  {s['ruin_50'] * 100:.2f}%")
    print(f"    Lose 75%  (< ${sb * 0.25:>8,.0f}):  {s['ruin_75'] * 100:.2f}%")

    print("\n  Max consecutive losing streak")
    print(f"    Typical   (p50):  {s['streak_p50']:>3} trades")
    print(f"    Bad case  (p95):  {s['streak_p95']:>3} trades")
    print(f"    Worst     (p99):  {s['streak_p99']:>3} trades")

    print("\n  Equity path  (p5 / p50 / p95)")
    print(f"    {'At trade %':>10}  {'p5':>12}  {'p50 (med)':>12}  {'p95':>12}")
    print(f"    {'----------':>10}  {'------------':>12}  {'------------':>12}  {'------------':>12}")
    for pct, p5, p50, p95 in s["path"]:
        print(f"    {pct:>9}%  ${p5:>10,.2f}  ${p50:>10,.2f}  ${p95:>10,.2f}")


def print_results(
    label:       str,
    n_trades:    int,
    n_sims:      int,
    sb:          float,
    pnls:        np.ndarray,
    hist_streak: int,
    flat_stats:  dict,
    snow_stats:  dict,
) -> None:
    wr = float((pnls > 0).mean())
    print(f"\n{'═' * _W}")
    print(f"  Monte Carlo — {label}")
    print(f"  {n_trades} trades  |  {n_sims:,} simulations  |  starting ${sb:,.2f}")
    print(f"  Win rate: {wr * 100:.1f}%  |  Avg PnL: {pnls.mean():+.4f}"
          f"  |  Historical max streak: {hist_streak}")
    print(f"{'═' * _W}")
    _print_mode("Flat sizing", flat_stats, sb)
    _print_mode(
        f"Snowball sizing  "
        f"(base {_BASE_RISK*100:.0f}%  addon {_ADDON_RATE*100:.0f}%  cap {_MAX_RISK*100:.0f}%)",
        snow_stats,
        sb,
    )
    print()


# ── Markdown report ────────────────────────────────────────────────────────────

def _md_mode_section(name: str, s: dict, sb: float) -> list[str]:
    lines = [
        f"## {name}",
        "",
        "### Ending Balance",
        "",
        "| Metric | Value | vs Start |",
        "|:---|---:|---:|",
        f"| Median (p50) | ${s['final_p50']:,.2f} | {_pct_chg(s['final_p50'], sb)} |",
        f"| Worst 5% (p5) | ${s['final_p5']:,.2f} | {_pct_chg(s['final_p5'], sb)} |",
        f"| Best 5% (p95) | ${s['final_p95']:,.2f} | {_pct_chg(s['final_p95'], sb)} |",
        f"| Probability of profit | {s['prob_profit'] * 100:.1f}% | |",
        "",
        "### Max Drawdown (% of starting balance)",
        "",
        "| Percentile | Drawdown |",
        "|:---|---:|",
        f"| Typical (p50) | {s['mdd_p50'] * 100:.1f}% |",
        f"| Bad case (p5) | {s['mdd_p5']  * 100:.1f}% |",
        f"| Best case (p95) | {s['mdd_p95'] * 100:.1f}% |",
        "",
        "### Ruin Risk (ever touching threshold)",
        "",
        "| Threshold | Probability |",
        "|:---|---:|",
        f"| Lose 25% (< ${sb * 0.75:,.0f}) | {s['ruin_25'] * 100:.2f}% |",
        f"| Lose 50% (< ${sb * 0.50:,.0f}) | {s['ruin_50'] * 100:.2f}% |",
        f"| Lose 75% (< ${sb * 0.25:,.0f}) | {s['ruin_75'] * 100:.2f}% |",
        "",
        "### Max Consecutive Losing Streak",
        "",
        "| Percentile | Streak |",
        "|:---|---:|",
        f"| Typical (p50) | {s['streak_p50']} trades |",
        f"| Bad case (p95) | {s['streak_p95']} trades |",
        f"| Worst case (p99) | {s['streak_p99']} trades |",
        "",
        "### Equity Path",
        "",
        "| At trade % | p5 | p50 (median) | p95 |",
        "|---:|---:|---:|---:|",
    ]
    for pct, p5, p50, p95 in s["path"]:
        lines.append(f"| {pct}% | ${p5:,.2f} | ${p50:,.2f} | ${p95:,.2f} |")
    lines.append("")
    return lines


def write_report(
    label:       str,
    n_trades:    int,
    n_sims:      int,
    sb:          float,
    pnls:        np.ndarray,
    hist_streak: int,
    flat_stats:  dict,
    snow_stats:  dict,
) -> Path:
    today    = date.today()
    slug     = label.replace(" ", "_").replace("/", "-")
    out_dir  = Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"montecarlo_{slug}_{today.strftime('%Y_%m_%d')}.md"

    wr = float((pnls > 0).mean())
    lines: list[str] = [
        f"# Monte Carlo — {label} — {today.isoformat()}",
        "",
        "## Simulation Parameters",
        "",
        "| Parameter | Value |",
        "|:---|---:|",
        f"| Trades in sample | {n_trades} |",
        f"| Simulations | {n_sims:,} |",
        f"| Starting balance | ${sb:,.2f} |",
        f"| Snowball base risk | {_BASE_RISK * 100:.0f}% of equity |",
        f"| Snowball profit add-on | {_ADDON_RATE * 100:.0f}% × profit fraction |",
        f"| Snowball max cap | {_MAX_RISK * 100:.0f}% of equity |",
        "",
        "## Historical Sample",
        "",
        "| Metric | Value |",
        "|:---|---:|",
        f"| Win rate | {wr * 100:.1f}% |",
        f"| Avg PnL / trade | {float(pnls.mean()):+.4f} |",
        f"| Median PnL / trade | {float(np.median(pnls)):+.4f} |",
        f"| Worst trade | {float(pnls.min()):+.4f} |",
        f"| Best trade | {float(pnls.max()):+.4f} |",
        f"| Historical max losing streak | {hist_streak} trades |",
        "",
        "---",
        "",
    ]
    lines.extend(_md_mode_section("Flat Sizing", flat_stats, sb))
    lines.append("---")
    lines.append("")
    lines.extend(_md_mode_section(
        f"Snowball Sizing "
        f"(base {_BASE_RISK*100:.0f}% + {_ADDON_RATE*100:.0f}% addon, {_MAX_RISK*100:.0f}% cap)",
        snow_stats,
        sb,
    ))

    out_path.write_text("\n".join(lines) + "\n")
    return out_path


# ── Entry point ────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Monte Carlo simulation of paper-trade equity curves.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python scripts/monte_carlo.py\n"
            "  python scripts/monte_carlo.py --strategy cheap_reversal_scalp\n"
            "  python scripts/monte_carlo.py --balance 5000 --sims 50000\n"
        ),
    )
    p.add_argument(
        "--strategy", default=None, metavar="RULE",
        help="Filter by rule_name (default: all strategies combined)",
    )
    p.add_argument(
        "--balance", type=float, default=BALANCE_DEFAULT, metavar="USD",
        help=f"Starting account balance in dollars (default: {BALANCE_DEFAULT:,.0f})",
    )
    p.add_argument(
        "--sims", type=int, default=N_SIMS_DEFAULT,
        help=f"Number of Monte Carlo simulations (default: {N_SIMS_DEFAULT:,})",
    )
    p.add_argument(
        "--seed", type=int, default=SEED_DEFAULT,
        help=f"Random seed for reproducibility (default: {SEED_DEFAULT})",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if args.balance <= 0:
        sys.exit("Error: --balance must be a positive dollar amount")
    if args.sims < 100:
        sys.exit("Error: --sims must be at least 100")

    pnls, eps, label = load_trades(args.strategy)

    if len(pnls) == 0:
        suffix = f" for strategy '{args.strategy}'" if args.strategy else ""
        print(f"No closed trades found{suffix}.")
        return

    if len(pnls) < 30:
        print(f"  [warn] Only {len(pnls)} historical trades — results have wide uncertainty")

    sb         = args.balance
    n_sims     = args.sims
    n_trades   = len(pnls)
    hist_s     = _hist_streak(pnls)

    # Independent RNG streams for each mode so they don't share state.
    root_rng  = np.random.default_rng(args.seed)
    flat_rng  = np.random.default_rng(root_rng.integers(0, 2**63))
    snow_rng  = np.random.default_rng(root_rng.integers(0, 2**63))

    flat_eq, flat_sp = run_flat(pnls, sb, n_sims, flat_rng)
    snow_eq, snow_sp = run_snowball(pnls, eps, sb, n_sims, snow_rng)

    flat_stats = compute_stats(flat_eq, flat_sp, sb)
    snow_stats = compute_stats(snow_eq, snow_sp, sb)

    print_results(label, n_trades, n_sims, sb, pnls, hist_s, flat_stats, snow_stats)

    path = write_report(label, n_trades, n_sims, sb, pnls, hist_s, flat_stats, snow_stats)
    print(f"Report written → {path}\n")


if __name__ == "__main__":
    main()
