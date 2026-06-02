#!/usr/bin/env python3
"""
post_move_continuation_report.py — Report for the post_move_continuation_scalp/v1
WATCH-ONLY continuation-scalp hypothesis.

Reads `post_move_continuation_signals` for one `post_move_continuation_runs` row
(latest by default) and reports, per exit test (test_a..test_d):
    signal count, win rate, avg / median simulated PnL, avg win, avg loss,
    expectancy, profit factor, max-drawdown approximation, avg time to peak,
broken down by:
    side · price bucket · time_remaining bucket · spread bucket ·
    directional_momentum bucket · directional_gap_z bucket ·
    contract_price_change_30s bucket · hour_block · day_name · volatility regime.

It then answers the hypothesis's research questions and tags each cell with a
sample-maturity tier.

0.85+ signals are summarised separately as watch-only context (never scalped).

Backtest results live in their own tables, entirely separate from live
paper_trades.  PAPER-ONLY RESEARCH — no live trading, no order execution.  This
is a hypothesis test, not a trade recommendation.

Usage:
    python scripts/post_move_continuation_report.py [--run-id N]
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
_EXIT_TESTS = ["test_a", "test_b", "test_c", "test_d"]


# ── Loading ──────────────────────────────────────────────────────────────────

def _load_run(run_id: Optional[int]) -> Optional[dict]:
    if run_id is not None:
        return fetch_one("SELECT * FROM post_move_continuation_runs WHERE id = %s", (run_id,))
    return fetch_one("SELECT * FROM post_move_continuation_runs ORDER BY id DESC LIMIT 1")


def _load_rows(run_id: int) -> list[dict]:
    return fetch_all(
        "SELECT * FROM post_move_continuation_signals WHERE run_id = %s "
        "ORDER BY entry_time ASC, id ASC",
        (run_id,),
    )


def _fv(r: dict, key: str) -> Optional[float]:
    v = r.get(key)
    return None if v is None else float(v)


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(rows: list[dict]) -> dict[str, Any]:
    """Stats for a set of (signal × one exit test) rows."""
    n = len(rows)
    if n == 0:
        return {"n": 0}

    pnls   = [float(r["simulated_pnl"]) for r in rows if r.get("simulated_pnl") is not None]
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

    ttp = [t for t in (_fv(r, "time_to_peak") for r in rows) if t is not None]

    return {
        "n":             n,
        "win_rate":      (len(wins) / len(pnls)) if pnls else 0.0,
        "total_pnl":     sum(pnls),
        "avg_pnl":       mean(pnls) if pnls else 0.0,
        "expectancy":    mean(pnls) if pnls else 0.0,   # per-trade mean == expectancy
        "med_pnl":       median(pnls) if pnls else 0.0,
        "avg_win":       mean(wins) if wins else 0.0,
        "avg_loss":      mean(losses) if losses else 0.0,
        "profit_factor": profit_factor,
        "max_drawdown":  max_dd,
        "avg_time_to_peak": mean(ttp) if ttp else 0.0,
    }


def maturity(n: int) -> str:
    if n < 25:   return "insufficient (<25)"
    if n < 50:   return "smoke test only (25–49)"
    if n < 100:  return "early clue (50–99)"
    if n < 250:  return "first serious review (100–249)"
    return "stronger evidence (250+)"


# ── Bucketers ──────────────────────────────────────────────────────────────────

def _b_side(r: dict) -> str:
    return r.get("side") or "N/A"


def _b_price_bucket(r: dict) -> str:
    return r.get("price_bucket") or "N/A"


def _b_time_remaining(r: dict) -> str:
    t = _fv(r, "time_remaining_seconds")
    if t is None:  return "N/A"
    if t < 120:    return "<120"
    if t < 180:    return "120-180"
    if t < 240:    return "180-240"
    if t < 300:    return "240-300"
    return                "300+"


def _b_spread(r: dict) -> str:
    return r.get("spread_bucket") or "N/A"


def _b_dir_mom(r: dict) -> str:
    m = _fv(r, "directional_momentum_score")
    if m is None:  return "N/A"
    if m < 3:      return "<3"
    if m < 5:      return "3-5"
    if m < 8:      return "5-8"
    return                "8+"


def _b_dir_gz(r: dict) -> str:
    z = _fv(r, "directional_gap_z_score")
    if z is None:  return "N/A"
    if z < 1.5:    return "<1.5"
    if z < 2.5:    return "1.5-2.5"
    if z < 4.0:    return "2.5-4.0"
    return                "4.0+"


def _b_chg30(r: dict) -> str:
    c = _fv(r, "contract_price_change_30s")
    if c is None:    return "N/A"
    if c < 0.04:     return "<0.04"
    if c < 0.06:     return "0.04-0.06"
    if c < 0.10:     return "0.06-0.10"
    return                  "0.10+"


def _b_hour(r: dict) -> str:
    return r.get("hour_block") or "N/A"


def _b_day(r: dict) -> str:
    return r.get("day_name") or "N/A"


def _b_vol_regime(r: dict) -> str:
    return r.get("volatility_regime") or "unknown"


_PRICE_ORDER = ["early_momentum", "premium_midrange", "late_premium_caution",
                "no_scalp_or_expiry_hold_only", "below_range", "N/A"]
_SIDE_ORDER  = ["YES", "NO", "N/A"]
_TR_ORDER    = ["<120", "120-180", "180-240", "240-300", "300+", "N/A"]
_SPREAD_ORDER = ["<0.01", "0.01-0.02", "0.02-0.03", "0.03+", "N/A"]
_MOM_ORDER   = ["<3", "3-5", "5-8", "8+", "N/A"]
_GZ_ORDER    = ["<1.5", "1.5-2.5", "2.5-4.0", "4.0+", "N/A"]
_CHG_ORDER   = ["<0.04", "0.04-0.06", "0.06-0.10", "0.10+", "N/A"]
_DAY_ORDER   = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday", "N/A"]
_VOL_ORDER   = ["calm", "normal", "elevated", "violent", "unknown"]

_DIMENSIONS: list[tuple[str, Callable[[dict], str], Optional[list[str]]]] = [
    ("side",                    _b_side,          _SIDE_ORDER),
    ("price bucket",            _b_price_bucket,  _PRICE_ORDER),
    ("time remaining",          _b_time_remaining, _TR_ORDER),
    ("spread bucket",           _b_spread,        _SPREAD_ORDER),
    ("directional momentum",    _b_dir_mom,       _MOM_ORDER),
    ("directional gap_z",       _b_dir_gz,        _GZ_ORDER),
    ("contract_price_change_30s", _b_chg30,       _CHG_ORDER),
    ("hour block",              _b_hour,          None),
    ("day",                     _b_day,           _DAY_ORDER),
    ("volatility regime",       _b_vol_regime,    _VOL_ORDER),
]


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

_HEADER = (f"{'group':<22}{'n':>5}{'win%':>7}{'avgPnL':>9}{'expect':>9}{'medPnL':>9}"
           f"{'avgWin':>9}{'avgLoss':>9}{'PF':>7}{'maxDD':>8}{'t2peak':>8}")


def _pf_s(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _row_line(label: str, m: dict) -> str:
    if m.get("n", 0) == 0:
        return (f"{label:<22}{0:>5}" + "".join(f"{'—':>{w}}" for w in
                (7, 9, 9, 9, 9, 9, 7, 8, 8)))
    return (
        f"{label:<22}{m['n']:>5}{m['win_rate']*100:>6.1f}%"
        f"{m['avg_pnl']:>+9.4f}{m['expectancy']:>+9.4f}{m['med_pnl']:>+9.4f}"
        f"{m['avg_win']:>+9.4f}{m['avg_loss']:>+9.4f}{_pf_s(m['profit_factor']):>7}"
        f"{m['max_drawdown']:>8.4f}{m['avg_time_to_peak']:>8.2f}"
    )


_MD_HEADER = [
    "| group | n | win% | avgPnL | expect | medPnL | avgWin | avgLoss | PF | maxDD | t2peak |",
    "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
]


def _md_row(label: str, m: dict) -> str:
    if m.get("n", 0) == 0:
        return f"| {label} | 0 | — | — | — | — | — | — | — | — | — |"
    return (
        f"| {label} | {m['n']} | {m['win_rate']*100:.1f}% | {m['avg_pnl']:+.4f} "
        f"| {m['expectancy']:+.4f} | {m['med_pnl']:+.4f} | {m['avg_win']:+.4f} "
        f"| {m['avg_loss']:+.4f} | {_pf_s(m['profit_factor'])} | {m['max_drawdown']:.4f} "
        f"| {m['avg_time_to_peak']:.2f} |"
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
    md += list(_MD_HEADER)
    for label, m in bd:
        print(_row_line(label, m))
        md.append(_md_row(label, m))
    md.append("")
    return md


# ── Verdicts ───────────────────────────────────────────────────────────────────

def _overall_by_test(scalp_rows: list[dict]) -> dict[str, dict]:
    return {t: compute_metrics([r for r in scalp_rows if r["exit_test"] == t])
            for t in _EXIT_TESTS}


def _bucket_metric(rows: list[dict], key_fn, bucket: str) -> dict:
    return compute_metrics([r for r in rows if key_fn(r) == bucket])


def _verdict(scalp_rows: list[dict], by_test: dict[str, dict]) -> list[str]:
    out: list[str] = []

    def emit(line: str) -> None:
        print(f"    {line}")
        out.append(line)

    print(f"\n{'═' * _W}\n  RESEARCH VERDICT  (scalp candidates only; 0.85+ excluded)\n{'═' * _W}")
    out += ["## Research verdict", "", "_Scalp candidates only (0.85+ logged as context, excluded)._", ""]

    # Best exit test by expectancy (require some sample).
    ranked = sorted(
        ((t, m) for t, m in by_test.items() if m.get("n", 0) > 0),
        key=lambda kv: kv[1]["expectancy"], reverse=True,
    )

    # Q1 — room left to scalp profitably after the move?
    if ranked:
        bt, bm = ranked[0]
        room = bm["expectancy"] > 0 and bm["profit_factor"] > 1.0
        emit(f"Q1 room to scalp after upward move: best test {bt} "
             f"expect={bm['expectancy']:+.4f} PF={_pf_s(bm['profit_factor'])} "
             f"→ {'YES — positive edge survives spread+slippage' if room else 'NO clear edge after costs'}")
    else:
        emit("Q1 room to scalp: insufficient data.")

    # Q2 — which side works better?
    for t in _EXIT_TESTS:
        trows = [r for r in scalp_rows if r["exit_test"] == t]
        if not trows:
            continue
        y = _bucket_metric(trows, _b_side, "YES")
        n = _bucket_metric(trows, _b_side, "NO")
        better = "YES" if y.get("expectancy", 0) > n.get("expectancy", 0) else "NO"
        emit(f"Q2 side [{t}]: YES expect={y.get('expectancy',0):+.4f} (n={y.get('n',0)}) "
             f"vs NO expect={n.get('expectancy',0):+.4f} (n={n.get('n',0)}) → {better} better")
        break  # representative test is enough for the headline line

    # Q3 — early_momentum vs premium_midrange (use best test).
    if ranked:
        bt = ranked[0][0]
        trows = [r for r in scalp_rows if r["exit_test"] == bt]
        em = _bucket_metric(trows, _b_price_bucket, "early_momentum")
        pm = _bucket_metric(trows, _b_price_bucket, "premium_midrange")
        emit(f"Q3 bucket [{bt}]: early_momentum expect={em.get('expectancy',0):+.4f} "
             f"(n={em.get('n',0)}) vs premium_midrange expect={pm.get('expectancy',0):+.4f} "
             f"(n={pm.get('n',0)})")

        # Q4 — late_premium_caution upside.
        lp = _bucket_metric(trows, _b_price_bucket, "late_premium_caution")
        emit(f"Q4 late_premium_caution [{bt}]: expect={lp.get('expectancy',0):+.4f} "
             f"PF={_pf_s(lp.get('profit_factor',0))} (n={lp.get('n',0)}) → "
             f"{'has upside' if lp.get('expectancy',0) > 0 and lp.get('n',0) >= 25 else 'no reliable upside / thin sample'}")

    # Q5 — best exit test by expectancy.
    if ranked:
        order = "  ".join(f"{t}={m['expectancy']:+.4f}" for t, m in ranked)
        emit(f"Q5 best exit test by expectancy: {ranked[0][0]}  ({order})")

    # Q6 — are spreads killing it?  Compare tight vs wide spreads (best test).
    if ranked:
        bt = ranked[0][0]
        trows = [r for r in scalp_rows if r["exit_test"] == bt]
        tight = _bucket_metric(trows, _b_spread, "<0.01")
        wide  = _bucket_metric(trows, _b_spread, "0.02-0.03")
        emit(f"Q6 spread impact [{bt}]: <0.01 expect={tight.get('expectancy',0):+.4f} "
             f"(n={tight.get('n',0)}) vs 0.02-0.03 expect={wide.get('expectancy',0):+.4f} "
             f"(n={wide.get('n',0)}) → "
             f"{'spreads hurt' if tight.get('expectancy',0) > wide.get('expectancy',0) else 'spread not the main driver'}")

    # Q7 — hour/day dependence (best test): flag best & worst hour blocks.
    if ranked:
        bt = ranked[0][0]
        trows = [r for r in scalp_rows if r["exit_test"] == bt]
        hb = [(k, m) for k, m in breakdown(trows, _b_hour) if m.get("n", 0) >= 25]
        if hb:
            best = max(hb, key=lambda kv: kv[1]["expectancy"])
            worst = min(hb, key=lambda kv: kv[1]["expectancy"])
            emit(f"Q7 time-of-day [{bt}]: best {best[0]} expect={best[1]['expectancy']:+.4f}, "
                 f"worst {worst[0]} expect={worst[1]['expectancy']:+.4f} "
                 f"(buckets with n>=25 only)")
        else:
            emit("Q7 time-of-day: no hour block has n>=25 yet — inconclusive.")

    # Sample maturity headline (max test sample).
    max_n = max((m.get("n", 0) for m in by_test.values()), default=0)
    emit("")
    emit(f"SAMPLE: max per-test n={max_n} → {maturity(max_n)}")
    emit("(WATCH-ONLY hypothesis test — not a trade recommendation; no live trading.)")
    out += ["", f"**Sample: max per-test n={max_n} → {maturity(max_n)}**", "",
            "_WATCH-ONLY hypothesis test — not a trade recommendation; no live trading._", ""]
    return out


# ── Output ───────────────────────────────────────────────────────────────────

def write_md(lines: list[str], run_id: int) -> Path:
    out_dir = Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"post_move_continuation_report_run{run_id}_{date.today().isoformat()}.md"
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Report a post_move_continuation_scalp watch-only backtest run")
    ap.add_argument("--run-id", type=int, default=None,
                    help="post_move_continuation_runs.id (default: latest run)")
    args = ap.parse_args()

    run = _load_run(args.run_id)
    if run is None:
        print("\nNo post-move continuation runs found. "
              "Run scripts/post_move_continuation_backtest.py first.\n")
        return

    run_id = int(run["id"])
    rows = _load_rows(run_id)
    scalp_rows   = [r for r in rows if r["exit_test"] in _EXIT_TESTS]
    context_rows = [r for r in rows if r["exit_test"] == "context"]

    print(f"\n{'═' * _W}")
    print(f"  Post-Move Continuation Scalp — run #{run_id}  "
          f"({run['rule_name']}/{run['rule_version']})  WATCH-ONLY")
    print(f"{'═' * _W}")
    print("  ⚠  PAPER-ONLY RESEARCH — replayed from stored snapshots, no live trading.")
    print("     Hypothesis: a contract is scalpable after it BEGINS trending upward and the move continues.")
    print(f"     Fill model: entry = ask+slip, exit = bid-slip   (slippage={run['slippage_mode']})")
    print(f"     Coverage: {run.get('data_start')} → {run.get('data_end')}  "
          f"markets={run.get('n_markets')} snapshots={run.get('n_snapshots')}")
    print(f"     Signals: {run.get('n_signals')}  scalp_candidates={run.get('n_scalp_candidates')}  "
          f"context(0.85+)={run.get('n_context_signals')}  test_rows={run.get('n_test_rows')}")
    print(f"  Generated: {date.today().isoformat()}")

    md: list[str] = [
        f"# Post-Move Continuation Scalp — run #{run_id} "
        f"({run['rule_name']}/{run['rule_version']}) · WATCH-ONLY",
        "",
        "⚠ **PAPER-ONLY RESEARCH** — replayed from stored snapshots, no live trading. "
        "This is a hypothesis test, not a trade recommendation.",
        "",
        "Hypothesis: a contract is profitable to scalp if it begins trending upward and the "
        "move continues long enough that the exit bid clears the entry ask after spread and "
        "slippage. The goal is a short continuation move, not predicting expiry.",
        "",
        f"- Fill model: entry = ask+slip, exit = bid-slip (slippage = `{run['slippage_mode']}`)",
        f"- Coverage: {run.get('data_start')} → {run.get('data_end')}",
        f"- Markets: {run.get('n_markets')}  ·  Snapshots: {run.get('n_snapshots')}",
        f"- Signals: {run.get('n_signals')}  ·  Scalp candidates: {run.get('n_scalp_candidates')}  "
        f"·  Context (0.85+): {run.get('n_context_signals')}  ·  Test rows: {run.get('n_test_rows')}",
        f"- Generated: {date.today().isoformat()}",
        "",
        "Exit tests: **test_a** (+0.03/-0.02/30s) · **test_b** (+0.04/-0.03/45s) · "
        "**test_c** (+0.05/-0.03/60s) · **test_d** (+0.06/-0.04/60s).",
        "",
    ]

    if not scalp_rows:
        print("\n  No scalp-candidate trades for this run.\n")
        md += ["_No scalp-candidate trades for this run._", ""]
        if context_rows:
            md += [f"_Context-only (0.85+) signals: {len(context_rows)}._", ""]
        path = write_md(md, run_id)
        print(f"Report written → {path}\n")
        return

    # 1. Headline — overall metrics per exit test.
    by_test = _overall_by_test(scalp_rows)
    md += _print_table(
        "Headline — overall by exit test (scalp candidates)",
        [(t, by_test[t]) for t in _EXIT_TESTS],
        note="test_a +3/-2/30s · test_b +4/-3/45s · test_c +5/-3/60s · test_d +6/-4/60s",
    )

    # 2. Per-exit-test breakdowns across every requested dimension.
    for t in _EXIT_TESTS:
        trows = [r for r in scalp_rows if r["exit_test"] == t]
        print(f"\n{'═' * _W}\n  EXIT TEST: {t}   (n={len(trows)}  ›  {maturity(len(trows))})\n{'═' * _W}")
        md += [f"## Exit test: `{t}`  (n={len(trows)} — {maturity(len(trows))})", ""]
        for title, key_fn, order in _DIMENSIONS:
            md += _print_table(f"[{t}] by {title}", breakdown(trows, key_fn, order))

    # 3. Context (0.85+) summary — counts only, never scalped.
    if context_rows:
        print(f"\n{'═' * _W}\n  CONTEXT (0.85+, watch-only — NOT scalped)   n={len(context_rows)}\n{'═' * _W}")
        md += [f"## Context (0.85+) — watch-only, not scalped  (n={len(context_rows)})", ""]
        c_side = Counter(r.get("side") or "N/A" for r in context_rows)
        c_hour = Counter(r.get("hour_block") or "N/A" for r in context_rows)
        print("  by side:", dict(c_side))
        md += ["**By side:** " + ", ".join(f"{k}={v}" for k, v in c_side.items()), "",
               "**By hour block:** " + ", ".join(f"{k}={v}" for k, v in sorted(c_hour.items())), ""]

    # 4. Verdict — answer the 7 research questions.
    md += _verdict(scalp_rows, by_test)

    print(f"\n{'═' * _W}\n")
    path = write_md(md, run_id)
    print(f"Report written → {path}\n")


if __name__ == "__main__":
    main()
