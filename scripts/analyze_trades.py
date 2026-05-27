#!/usr/bin/env python3
"""
Analyze closed paper trades stored in MySQL.

Groups by rule_name, rule_version, exit_reason, and five feature buckets
(time_remaining, contract_price, spread, momentum_score, gap_z_score).

Metrics per group: trade count, win rate, avg/median PnL, avg win, avg loss,
expectancy, max loss, sequential max drawdown, profit factor.

Also writes a Markdown report to reports/daily_report_YYYY_MM_DD.md.

Usage:
    python scripts/analyze_trades.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all

# ── SQL ────────────────────────────────────────────────────────────────────────

_QUERY = """
    SELECT
        pt.rule_name,
        pt.rule_version,
        pt.exit_reason,
        CAST(pt.pnl AS DOUBLE) AS pnl,
        s.time_remaining_seconds,
        CAST(s.contract_price  AS DOUBLE) AS contract_price,
        CAST(s.spread          AS DOUBLE) AS spread,
        CAST(s.momentum_score  AS DOUBLE) AS momentum_score,
        CAST(s.gap_z_score     AS DOUBLE) AS gap_z_score
    FROM paper_trades pt
    LEFT JOIN signals s ON pt.signal_id = s.id
    WHERE pt.exit_time IS NOT NULL
    ORDER BY pt.entry_time
"""


def load_trades() -> list[dict]:
    return fetch_all(_QUERY)


# ── Bucket helpers ─────────────────────────────────────────────────────────────

def _fv(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    return None if v is None else float(v)


def _b_time_remaining(r: dict) -> str:
    t = _fv(r, "time_remaining_seconds")
    if t is None: return "N/A"
    if t < 60:    return "<60s"
    if t < 120:   return "60-120s"
    if t < 180:   return "120-180s"
    if t < 240:   return "180-240s"
    if t < 300:   return "240-300s"
    return "300s+"


def _b_contract_price(r: dict) -> str:
    p = _fv(r, "contract_price")
    if p is None: return "N/A"
    if p < 0.15:  return "<0.15"
    if p < 0.25:  return "0.15-0.25"
    if p < 0.35:  return "0.25-0.35"
    if p < 0.50:  return "0.35-0.50"
    if p < 0.65:  return "0.50-0.65"
    if p < 0.80:  return "0.65-0.80"
    return "0.80+"


def _b_spread(r: dict) -> str:
    s = _fv(r, "spread")
    if s is None: return "N/A"
    if s < 0.01:  return "<0.01"
    if s < 0.02:  return "0.01-0.02"
    if s < 0.03:  return "0.02-0.03"
    return "0.03+"


def _b_momentum(r: dict) -> str:
    m = _fv(r, "momentum_score")
    if m is None:  return "N/A"
    if m <= -5:    return "≤-5"
    if m <= -3:    return "-5 to -3"
    if m < 0:      return "-3 to 0"
    if m == 0:     return "0"
    if m < 3:      return "0 to 3"
    if m <= 5:     return "3 to 5"
    return ">5"


def _b_gap_z(r: dict) -> str:
    z = _fv(r, "gap_z_score")
    if z is None:  return "N/A"
    if z <= -2:    return "≤-2"
    if z <= -1:    return "-2 to -1"
    if z < 0:      return "-1 to 0"
    if z < 1:      return "0 to 1"
    if z < 2:      return "1 to 2"
    return "≥2"


# ── Metrics ────────────────────────────────────────────────────────────────────

def compute_metrics(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    if not n:
        return {}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr     = len(wins) / n
    aw     = mean(wins)   if wins   else 0.0
    al     = mean(losses) if losses else 0.0
    gp     = sum(wins)
    gl     = abs(sum(losses))

    # Sequential max drawdown — entry_time order preserved from query.
    # Measures the worst peak-to-trough equity decline within this group.
    running = peak = mdd = 0.0
    for p in pnls:
        running += p
        if running > peak:
            peak = running
        dd = running - peak
        if dd < mdd:
            mdd = dd

    return {
        "count":         n,
        "win_rate":      wr,
        "avg_pnl":       mean(pnls),
        "med_pnl":       median(pnls),
        "avg_win":       aw,
        "avg_loss":      al,
        "expectancy":    wr * aw + (1 - wr) * al,
        "max_loss":      min(pnls),
        "max_drawdown":  mdd,
        "profit_factor": gp / gl if gl > 0 else float("inf"),
    }


# ── Grouping ───────────────────────────────────────────────────────────────────

def group_by(
    rows: list[dict],
    key_fn: Callable[[dict], str],
    order: Optional[list[str]] = None,
) -> list[tuple[str, dict]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        buckets[key_fn(r)].append(float(r["pnl"]))

    if order:
        items = [(k, buckets[k]) for k in order if k in buckets]
        seen  = set(order)
        for k in sorted(buckets):
            if k not in seen:
                items.append((k, buckets[k]))
    else:
        items = sorted(buckets.items())

    return [(lbl, compute_metrics(pnls)) for lbl, pnls in items]


# ── Column definitions ─────────────────────────────────────────────────────────

_W_BUCKET = 32

# (header, width)  — all right-aligned except Bucket which is left-aligned
_COLS = [
    ("Count",    6),
    ("Win%",     6),
    ("Avg PnL",  9),
    ("Med PnL",  9),
    ("Avg Win",  9),
    ("Avg Loss", 9),
    ("Expect",   9),
    ("Max Loss", 9),
    ("Max DD",   9),
    ("PF",       6),
]


def _pf_str(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _metric_cells(m: dict) -> list[str]:
    return [
        str(m["count"]),
        f"{m['win_rate'] * 100:.1f}%",
        f"{m['avg_pnl']:+.4f}",
        f"{m['med_pnl']:+.4f}",
        f"{m['avg_win']:+.4f}",
        f"{m['avg_loss']:+.4f}",
        f"{m['expectancy']:+.4f}",
        f"{m['max_loss']:+.4f}",
        f"{m['max_drawdown']:+.4f}",
        _pf_str(m["profit_factor"]),
    ]


# ── Console printing ───────────────────────────────────────────────────────────

def _console_row(bucket: str, m: dict) -> str:
    left  = f"{bucket:<{_W_BUCKET}}"
    right = "  ".join(f"{v:>{w}}" for v, (_, w) in zip(_metric_cells(m), _COLS))
    return f"{left}  {right}"


def print_table(title: str, groups: list[tuple[str, dict]]) -> None:
    hdr  = f"{'Bucket':<{_W_BUCKET}}  " + "  ".join(f"{h:>{w}}" for h, w in _COLS)
    sep  = "-" * _W_BUCKET + "  " + "  ".join("-" * w for _, w in _COLS)
    rule = "─" * max(0, 76 - len(title))

    print(f"\n── {title} {rule}")
    print(hdr)
    print(sep)
    for bucket, m in groups:
        if m:
            print(_console_row(bucket, m))
    print()


# ── Markdown output ────────────────────────────────────────────────────────────

def _md_table(title: str, groups: list[tuple[str, dict]]) -> list[str]:
    hdrs   = ["Bucket"] + [h for h, _ in _COLS]
    aligns = [":---"]   + ["---:" for _ in _COLS]
    lines  = [
        f"### {title}",
        "",
        "| " + " | ".join(hdrs)   + " |",
        "| " + " | ".join(aligns) + " |",
    ]
    for bucket, m in groups:
        if m:
            cells = [bucket] + _metric_cells(m)
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def write_report(
    sections: list[tuple[str, list[tuple[str, dict]]]],
    overall:  dict,
) -> Path:
    today   = date.today()
    out_dir = Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"daily_report_{today.strftime('%Y_%m_%d')}.md"

    lines: list[str] = [
        f"# Kalshi Signal Logger — Trade Report {today.isoformat()}",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "|:---|---:|",
        f"| Trades | {overall['count']} |",
        f"| Win rate | {overall['win_rate'] * 100:.1f}% |",
        f"| Avg PnL | {overall['avg_pnl']:+.4f} |",
        f"| Median PnL | {overall['med_pnl']:+.4f} |",
        f"| Avg win | {overall['avg_win']:+.4f} |",
        f"| Avg loss | {overall['avg_loss']:+.4f} |",
        f"| Expectancy | {overall['expectancy']:+.4f} |",
        f"| Max single loss | {overall['max_loss']:+.4f} |",
        f"| Max drawdown | {overall['max_drawdown']:+.4f} |",
        f"| Profit factor | {_pf_str(overall['profit_factor'])} |",
        "",
        "---",
        "",
        "## Breakdowns",
        "",
    ]
    for title, groups in sections:
        lines.extend(_md_table(title, groups))

    out_path.write_text("\n".join(lines) + "\n")
    return out_path


# ── Dimension registry ─────────────────────────────────────────────────────────

_DIMENSIONS: list[tuple[str, Callable, Optional[list[str]]]] = [
    (
        "rule_name",
        lambda r: str(r["rule_name"] or "N/A"),
        None,
    ),
    (
        "rule_version",
        lambda r: str(r["rule_version"] or "N/A"),
        None,
    ),
    (
        "exit_reason",
        lambda r: str(r["exit_reason"] or "N/A"),
        ["near_expiry", "take_profit", "stop_loss",
         "trailing_stop", "break_even_stop", "timeout_60s", "N/A"],
    ),
    (
        "time_remaining",
        _b_time_remaining,
        ["<60s", "60-120s", "120-180s", "180-240s", "240-300s", "300s+", "N/A"],
    ),
    (
        "contract_price",
        _b_contract_price,
        ["<0.15", "0.15-0.25", "0.25-0.35", "0.35-0.50",
         "0.50-0.65", "0.65-0.80", "0.80+", "N/A"],
    ),
    (
        "spread",
        _b_spread,
        ["<0.01", "0.01-0.02", "0.02-0.03", "0.03+", "N/A"],
    ),
    (
        "momentum_score",
        _b_momentum,
        ["≤-5", "-5 to -3", "-3 to 0", "0", "0 to 3", "3 to 5", ">5", "N/A"],
    ),
    (
        "gap_z_score",
        _b_gap_z,
        ["≤-2", "-2 to -1", "-1 to 0", "0 to 1", "1 to 2", "≥2", "N/A"],
    ),
]


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    rows = load_trades()
    if not rows:
        print("No closed trades found.")
        return

    pnls    = [float(r["pnl"]) for r in rows]
    overall = compute_metrics(pnls)
    pf      = _pf_str(overall["profit_factor"])

    print(f"\n{'═' * 96}")
    print(f"  Kalshi Signal Logger — Trade Analysis   ({overall['count']} closed trades)")
    print(f"{'═' * 96}")
    print(
        f"  Total PnL: {sum(pnls):+.4f}"
        f"  |  Win rate: {overall['win_rate'] * 100:.1f}%"
        f"  |  Expectancy: {overall['expectancy']:+.4f}"
        f"  |  PF: {pf}"
        f"  |  Max DD: {overall['max_drawdown']:+.4f}"
    )

    sections: list[tuple[str, list[tuple[str, dict]]]] = []
    for title, key_fn, order in _DIMENSIONS:
        groups = group_by(rows, key_fn, order)
        print_table(title, groups)
        sections.append((title, groups))

    path = write_report(sections, overall)
    print(f"Report written → {path}\n")


if __name__ == "__main__":
    main()
