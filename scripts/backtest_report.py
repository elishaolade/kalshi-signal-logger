#!/usr/bin/env python3
"""
backtest_report.py — Report a historical_replay backtest run.

Reads `backtest_trades` for one `backtest_runs` row (the latest by default) and
reports, per exit profile:
    trades, win rate, expectancy, profit factor, avg win / avg loss,
    max drawdown, exit-reason breakdown,
    and performance by market phase, price bucket, hour, and day.

Backtest results live in their own tables, entirely separate from live
paper_trades.  PAPER-ONLY RESEARCH — no live trading, no order execution.

Usage:
    python scripts/backtest_report.py [--run-id N]
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

_W = 96


# ── Loading ──────────────────────────────────────────────────────────────────

def _load_run(run_id: Optional[int]) -> Optional[dict]:
    if run_id is not None:
        return fetch_one("SELECT * FROM backtest_runs WHERE id = %s", (run_id,))
    return fetch_one("SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 1")


def _load_trades(run_id: int) -> list[dict]:
    return fetch_all(
        "SELECT * FROM backtest_trades WHERE run_id = %s ORDER BY entry_time ASC, id ASC",
        (run_id,),
    )


def _fv(r: dict, key: str) -> Optional[float]:
    v = r.get(key)
    return None if v is None else float(v)


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(rows: list[dict]) -> dict[str, Any]:
    """Performance stats for a set of simulated trades (already one profile, or mixed)."""
    n = len(rows)
    if n == 0:
        return {"n": 0}

    pnls = [float(r["pnl"]) for r in rows if r.get("pnl") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_win  = sum(wins)
    gross_loss = -sum(losses)                    # positive magnitude
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)

    # Max drawdown on the cumulative-PnL curve (rows already time-ordered).
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        "n":            n,
        "win_rate":     (len(wins) / len(pnls)) if pnls else 0.0,
        "total_pnl":    sum(pnls),
        "expectancy":   mean(pnls) if pnls else 0.0,
        "med_pnl":      median(pnls) if pnls else 0.0,
        "avg_win":      mean(wins) if wins else 0.0,
        "avg_loss":     mean(losses) if losses else 0.0,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
    }


def maturity(n: int) -> str:
    if n == 0:    return "no data"
    if n < 30:    return "VERY SMALL sample — directional only"
    if n < 50:    return "small sample — treat as a clue"
    if n < 100:   return "moderate sample"
    return "usable sample"


# ── Bucketers ──────────────────────────────────────────────────────────────────

def _b_phase(r: dict) -> str:
    return r.get("market_phase") or "unknown"


def _b_price(r: dict) -> str:
    a = _fv(r, "losing_contract_ask")
    if a is None:  return "N/A"
    if a < 0.10:   return "0.05-0.10"
    if a < 0.20:   return "0.10-0.20"
    if a < 0.30:   return "0.20-0.30"
    return                "0.30+"


def _b_hour(r: dict) -> str:
    return r.get("entry_hour_block") or "N/A"


def _b_day(r: dict) -> str:
    return r.get("entry_day_name") or "N/A"


_PHASE_ORDER = ["first_2min", "min_2_to_3", "unknown"]
_PRICE_ORDER = ["0.05-0.10", "0.10-0.20", "0.20-0.30", "0.30+", "N/A"]
_DAY_ORDER   = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday", "N/A"]


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

_HEADER = (f"{'group':<22}{'n':>5}{'win%':>7}{'expect':>9}{'PF':>7}"
           f"{'avgWin':>9}{'avgLoss':>9}{'maxDD':>9}{'totPnL':>9}")


def _row_line(label: str, m: dict) -> str:
    if m.get("n", 0) == 0:
        return f"{label:<22}{0:>5}{'—':>7}{'—':>9}{'—':>7}{'—':>9}{'—':>9}{'—':>9}{'—':>9}"
    pf = m["profit_factor"]
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (
        f"{label:<22}{m['n']:>5}{m['win_rate']*100:>6.1f}%"
        f"{m['expectancy']:>+9.4f}{pf_s:>7}"
        f"{m['avg_win']:>+9.4f}{m['avg_loss']:>+9.4f}"
        f"{m['max_drawdown']:>9.4f}{m['total_pnl']:>+9.4f}"
    )


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
    md += ["| group | n | win% | expect | PF | avgWin | avgLoss | maxDD | totPnL |",
           "|---|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for label, m in bd:
        print(_row_line(label, m))
        if m.get("n", 0) == 0:
            md.append(f"| {label} | 0 | — | — | — | — | — | — | — |")
        else:
            pf = m["profit_factor"]
            pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
            md.append(
                f"| {label} | {m['n']} | {m['win_rate']*100:.1f}% "
                f"| {m['expectancy']:+.4f} | {pf_s} | {m['avg_win']:+.4f} "
                f"| {m['avg_loss']:+.4f} | {m['max_drawdown']:.4f} | {m['total_pnl']:+.4f} |"
            )
    md.append("")
    return md


def _exit_reason_block(rows: list[dict]) -> list[str]:
    c = Counter(r.get("exit_reason") or "unknown" for r in rows)
    n = sum(c.values())
    print(f"\n  Exit reasons:")
    md = ["**Exit reasons:**", ""]
    for reason, cnt in c.most_common():
        pct = (cnt / n * 100) if n else 0.0
        print(f"    {reason:<20} {cnt:>5}  ({pct:4.1f}%)")
        md.append(f"- `{reason}`: {cnt} ({pct:.1f}%)")
    md.append("")
    return md


def write_md(lines: list[str], run_id: int) -> Path:
    out_dir = Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"backtest_report_run{run_id}_{date.today().isoformat()}.md"
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Report a historical_replay backtest run")
    ap.add_argument("--run-id", type=int, default=None,
                    help="backtest_runs.id (default: latest run)")
    args = ap.parse_args()

    run = _load_run(args.run_id)
    if run is None:
        print("\nNo backtest runs found. Run scripts/historical_replay.py first.\n")
        return

    run_id = int(run["id"])
    trades = _load_trades(run_id)

    print(f"\n{'═' * _W}")
    print(f"  Backtest Report — run #{run_id}  "
          f"({run['rule_name']}/{run['rule_version']})")
    print(f"{'═' * _W}")
    print("  ⚠  PAPER-ONLY RESEARCH — replayed from stored snapshots, no live trading.")
    print(f"     Fill model: entry = ask+slip, exit = bid-slip   (slippage={run['slippage_mode']})")
    print(f"     Coverage: {run.get('data_start')} → {run.get('data_end')}  "
          f"markets={run.get('n_markets')} snapshots={run.get('n_snapshots')}")
    print(f"     Signals: {run.get('n_signals')}   Profile-trades: {len(trades)}  "
          f"›  {maturity(int(run.get('n_signals') or 0))}")
    print(f"  Generated: {date.today().isoformat()}")

    md: list[str] = [
        f"# Backtest Report — run #{run_id} ({run['rule_name']}/{run['rule_version']})",
        "",
        "⚠ **PAPER-ONLY RESEARCH** — replayed from stored snapshots, no live trading.",
        "",
        f"- Fill model: entry = ask+slip, exit = bid-slip (slippage = `{run['slippage_mode']}`)",
        f"- Coverage: {run.get('data_start')} → {run.get('data_end')}",
        f"- Markets: {run.get('n_markets')}  ·  Snapshots: {run.get('n_snapshots')}",
        f"- Signals: {run.get('n_signals')}  ·  Profile-trades: {len(trades)} — "
        f"{maturity(int(run.get('n_signals') or 0))}",
        f"- Generated: {date.today().isoformat()}",
        "",
    ]

    if not trades:
        print("\n  No simulated trades for this run.\n")
        md += ["_No simulated trades for this run._", ""]
        path = write_md(md, run_id)
        print(f"Report written → {path}\n")
        return

    # Profiles present, preserving run order if available.
    profiles = sorted({r["exit_profile"] for r in trades})

    # 1. Headline: one row per exit profile (the key comparison).
    md += _print_table(
        "Headline — by exit profile",
        [(p, compute_metrics([r for r in trades if r["exit_profile"] == p])) for p in profiles],
    )

    # 2. Per-profile detail: exit reasons + dimension breakdowns.
    for p in profiles:
        prows = [r for r in trades if r["exit_profile"] == p]
        print(f"\n{'═' * _W}\n  PROFILE: {p}   (n={len(prows)})\n{'═' * _W}")
        md += [f"## Profile: `{p}`  (n={len(prows)})", ""]
        md += _exit_reason_block(prows)
        md += _print_table(f"[{p}] by market phase", breakdown(prows, _b_phase, _PHASE_ORDER))
        md += _print_table(f"[{p}] by price bucket", breakdown(prows, _b_price, _PRICE_ORDER))
        md += _print_table(f"[{p}] by hour block",   breakdown(prows, _b_hour))
        md += _print_table(f"[{p}] by day",          breakdown(prows, _b_day, _DAY_ORDER))

    print(f"\n{'═' * _W}\n")
    path = write_md(md, run_id)
    print(f"Report written → {path}\n")


if __name__ == "__main__":
    main()
