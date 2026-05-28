#!/usr/bin/env python3
"""
pnms_report.py — Performance report focused on premium_no_midrange_scalp/v1.

Proves or disproves the PNMS hypothesis against three comparison baselines:
  1. PNMS v1                    premium_no_midrange_scalp / v1
  2. PMC NO 0.65–0.80           premium_momentum_continuation / NO / ask 0.65–0.80
  3. PMC YES                    premium_momentum_continuation / YES
  4. CRS                        cheap_reversal_scalp

Directional metrics (momentum_score, gap_z_score) are sign-flipped for NO
trades so that positive always means "confirms the traded direction."

Sample maturity thresholds
  < 25 trades        INSUFFICIENT
  25–49 trades       SMOKE TEST ONLY
  50–99 trades       EARLY CLUE
  100–249 trades     FIRST SERIOUS REVIEW
  250+ trades        STRONGER EVIDENCE

⚠  PAPER TRADING ONLY — this report makes no live trading recommendations.

Usage:
    python scripts/pnms_report.py
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

# ── Constants ──────────────────────────────────────────────────────────────────

_PNMS_RULE = "premium_no_midrange_scalp"
_PNMS_VER  = "v1"
_W = 96                 # console line width

_SAMPLE_STEPS = [
    (250, "STRONGER EVIDENCE    (250+ trades)"),
    (100, "FIRST SERIOUS REVIEW (100–249 trades)"),
    ( 50, "EARLY CLUE           (50–99 trades)"),
    ( 25, "SMOKE TEST ONLY      (25–49 trades)"),
    (  0, "INSUFFICIENT         (<25 trades)"),
]

_EXIT_ORDER = [
    "take_profit", "near_expiry", "stop_loss",
    "trailing_stop", "break_even_stop", "timeout_60s",
    "recovered_after_restart", "market_rollover", "N/A",
]
_DAY_ORDER = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday", "N/A",
]
_DIR_MOM_ORDER = ["≤-5", "-5 to -3", "-3 to 0", "0", "0 to 3", "3 to 5", ">5", "N/A"]
_DIR_GZ_ORDER  = ["≤-2", "-2 to -1", "-1 to 0", "0 to 1", "1 to 2", "≥2", "N/A"]
_SPREAD_ORDER  = ["<0.01", "0.01-0.02", "0.02-0.03", "0.03+", "N/A"]


# ── SQL ────────────────────────────────────────────────────────────────────────

_QUERY = """
    SELECT
        pt.rule_name,
        pt.rule_version,
        pt.side,
        pt.exit_reason,
        CAST(pt.pnl           AS DOUBLE)  AS pnl,
        pt.entry_day_name,
        pt.entry_hour_block,
        CAST(s.contract_price AS DOUBLE)  AS contract_price,
        CAST(s.spread         AS DOUBLE)  AS spread,
        CAST(s.momentum_score AS DOUBLE)  AS momentum_score,
        CAST(s.gap_z_score    AS DOUBLE)  AS gap_z_score
    FROM paper_trades pt
    LEFT JOIN signals s ON pt.signal_id = s.id
    WHERE pt.exit_time IS NOT NULL
      AND pt.pnl       IS NOT NULL
      AND pt.followed_rules = TRUE
    ORDER BY pt.entry_time ASC
"""


def _load() -> list[dict]:
    return fetch_all(_QUERY)


# ── Group definitions ──────────────────────────────────────────────────────────

def _fv(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    return None if v is None else float(v)


_GROUPS: list[dict[str, Any]] = [
    {
        "label": "PNMS v1",
        "desc":  "premium_no_midrange_scalp / v1",
        "filter": lambda r: (
            r.get("rule_name") == _PNMS_RULE
            and r.get("rule_version") == _PNMS_VER
        ),
    },
    {
        "label": "PMC NO 0.65–0.80",
        "desc":  "premium_momentum_continuation / NO / ask 0.65–0.80",
        "filter": lambda r: (
            r.get("rule_name") == "premium_momentum_continuation"
            and r.get("side") == "NO"
            and (_fv(r, "contract_price") or 0.0) >= 0.65
            and (_fv(r, "contract_price") or 0.0) <= 0.80
        ),
    },
    {
        "label": "PMC YES",
        "desc":  "premium_momentum_continuation / YES",
        "filter": lambda r: (
            r.get("rule_name") == "premium_momentum_continuation"
            and r.get("side") == "YES"
        ),
    },
    {
        "label": "CRS",
        "desc":  "cheap_reversal_scalp (all sides)",
        "filter": lambda r: r.get("rule_name") == "cheap_reversal_scalp",
    },
]


# ── Metrics ────────────────────────────────────────────────────────────────────

def compute_metrics(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    if not n:
        return {"n": 0}

    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr     = len(wins) / n
    aw     = mean(wins)   if wins   else 0.0
    al     = mean(losses) if losses else 0.0
    gp     = sum(wins)
    gl     = abs(sum(losses))

    # sequential max drawdown (preserves entry_time order from query)
    running = peak = mdd = 0.0
    for p in pnls:
        running += p
        if running > peak:
            peak = running
        dd = running - peak
        if dd < mdd:
            mdd = dd

    # longest consecutive losing streak (loss = pnl <= 0)
    streak = max_streak = 0
    for p in pnls:
        if p <= 0:
            streak += 1
            if streak > max_streak:
                max_streak = streak
        else:
            streak = 0

    return {
        "n":             n,
        "win_rate":      wr,
        "total_pnl":     sum(pnls),
        "avg_pnl":       mean(pnls),
        "med_pnl":       median(pnls),
        "avg_win":       aw,
        "avg_loss":      al,
        "expectancy":    wr * aw + (1 - wr) * al,
        "profit_factor": gp / gl if gl > 0 else float("inf"),
        "max_drawdown":  mdd,
        "losing_streak": max_streak,
    }


def maturity(n: int) -> str:
    for threshold, label in _SAMPLE_STEPS:
        if n >= threshold:
            return label
    return _SAMPLE_STEPS[-1][1]


def _pf(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


# ── Bucket helpers ─────────────────────────────────────────────────────────────

def _b_exit(r: dict) -> str:
    return str(r.get("exit_reason") or "N/A")


def _b_hour(r: dict) -> str:
    return str(r.get("entry_hour_block") or "N/A")


def _b_day(r: dict) -> str:
    return str(r.get("entry_day_name") or "N/A")


def _b_spread(r: dict) -> str:
    s = _fv(r, "spread")
    if s is None: return "N/A"
    if s < 0.01:  return "<0.01"
    if s < 0.02:  return "0.01-0.02"
    if s < 0.03:  return "0.02-0.03"
    return               "0.03+"


def _directional(r: dict, key: str) -> Optional[float]:
    """Return feature value, sign-flipped for NO trades."""
    v    = _fv(r, key)
    side = r.get("side", "")
    if v is None:
        return None
    return v if side == "YES" else -v


def _b_dir_momentum(r: dict) -> str:
    dm = _directional(r, "momentum_score")
    if dm is None: return "N/A"
    if dm <= -5:   return "≤-5"
    if dm <= -3:   return "-5 to -3"
    if dm <   0:   return "-3 to 0"
    if dm ==  0:   return "0"
    if dm <   3:   return "0 to 3"
    if dm <=  5:   return "3 to 5"
    return                ">5"


def _b_dir_gap_z(r: dict) -> str:
    dz = _directional(r, "gap_z_score")
    if dz is None: return "N/A"
    if dz <= -2:   return "≤-2"
    if dz <= -1:   return "-2 to -1"
    if dz <   0:   return "-1 to 0"
    if dz <   1:   return "0 to 1"
    if dz <   2:   return "1 to 2"
    return                "≥2"


# ── Grouping ───────────────────────────────────────────────────────────────────

def breakdown(
    rows:   list[dict],
    key_fn: Callable[[dict], str],
    order:  Optional[list[str]] = None,
) -> list[tuple[str, dict]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        buckets[key_fn(r)].append(float(r["pnl"]))

    if order:
        items: list[tuple[str, list[float]]] = [
            (k, buckets[k]) for k in order if k in buckets
        ]
        seen = set(order)
        for k in sorted(buckets):
            if k not in seen:
                items.append((k, buckets[k]))
    else:
        items = sorted(buckets.items())

    return [(lbl, compute_metrics(v)) for lbl, v in items]


# ── Console + Markdown rendering ───────────────────────────────────────────────

# Compact breakdown table column spec
_BD_W  = 22     # bucket label width
_BD_COLS = [
    ("Count",    6),
    ("Win%",     6),
    ("Avg PnL",  9),
    ("Avg Win",  9),
    ("Avg Loss", 9),
    ("Expect",   9),
    ("PF",       6),
]


def _bd_row(bucket: str, m: dict) -> Optional[str]:
    if not m.get("n"):
        return None
    cells = [
        str(m["n"]),
        f"{m['win_rate'] * 100:.1f}%",
        f"{m['avg_pnl']:+.4f}",
        f"{m['avg_win']:+.4f}",
        f"{m['avg_loss']:+.4f}",
        f"{m['expectancy']:+.4f}",
        _pf(m["profit_factor"]),
    ]
    return (
        f"  {bucket:<{_BD_W}}"
        + "  ".join(f"{v:>{w}}" for v, (_, w) in zip(cells, _BD_COLS))
    )


def _print_bd_table(
    title: str,
    groups: list[tuple[str, dict]],
    note: str = "",
) -> list[str]:
    """Print breakdown to console; return equivalent markdown lines."""
    hdr = (
        f"  {'Bucket':<{_BD_W}}"
        + "  ".join(f"{h:>{w}}" for h, w in _BD_COLS)
    )
    sep = "  " + "-" * _BD_W + "  " + "  ".join("-" * w for _, w in _BD_COLS)
    rule = "─" * max(0, _W - len(title) - 4)
    print(f"\n── {title} {rule}")
    if note:
        print(f"   {note}")
    print(hdr)
    print(sep)

    md: list[str] = [f"### {title}", ""]
    if note:
        md += [f"_{note}_", ""]
    hdrs   = ["Bucket"] + [h for h, _ in _BD_COLS]
    aligns = [":---"]   + ["---:" for _ in _BD_COLS]
    md += ["| " + " | ".join(hdrs) + " |", "| " + " | ".join(aligns) + " |"]

    any_data = False
    for bucket, m in groups:
        row = _bd_row(bucket, m)
        if row:
            print(row)
            cells = [
                str(m["n"]),
                f"{m['win_rate'] * 100:.1f}%",
                f"{m['avg_pnl']:+.4f}",
                f"{m['avg_win']:+.4f}",
                f"{m['avg_loss']:+.4f}",
                f"{m['expectancy']:+.4f}",
                _pf(m["profit_factor"]),
            ]
            md.append("| " + " | ".join([bucket] + cells) + " |")
            any_data = True

    if not any_data:
        print("   (no data)")
        md.append("_(no data)_")

    print()
    md.append("")
    return md


# ── Core metrics block ─────────────────────────────────────────────────────────

def _print_core(m: dict) -> list[str]:
    """Two-column core metrics; return markdown lines."""
    if not m.get("n"):
        print("  (no data)\n")
        return ["_(no data)_", ""]

    rows = [
        ("Win rate",              f"{m['win_rate'] * 100:.1f}%"),
        ("Total PnL",             f"{m['total_pnl']:+.4f}"),
        ("Avg PnL",               f"{m['avg_pnl']:+.4f}"),
        ("Median PnL",            f"{m['med_pnl']:+.4f}"),
        ("Avg win",               f"{m['avg_win']:+.4f}"),
        ("Avg loss",              f"{m['avg_loss']:+.4f}"),
        ("Expectancy",            f"{m['expectancy']:+.4f}"),
        ("Profit factor",         _pf(m["profit_factor"])),
        ("Max drawdown",          f"{m['max_drawdown']:+.4f}"),
        ("Longest losing streak", str(m["losing_streak"])),
    ]

    # print two metrics side by side
    rule = "─" * max(0, _W - len("Core Metrics") - 4)
    print(f"\n── Core Metrics {rule}")
    half = (len(rows) + 1) // 2
    for i in range(half):
        left  = rows[i]
        right = rows[i + half] if i + half < len(rows) else ("", "")
        print(
            f"  {left[0]:<26} {left[1]:<14}"
            f"  {right[0]:<26} {right[1]}"
        )
    print()

    md: list[str] = [
        "### Core Metrics", "",
        "| Metric | Value |",
        "|:---|---:|",
    ]
    for label, val in rows:
        md.append(f"| {label} | {val} |")
    md.append("")
    return md


# ── Full group report ──────────────────────────────────────────────────────────

def print_group_report(
    label: str,
    desc:  str,
    rows:  list[dict],
) -> list[str]:
    """
    Print the full metrics + breakdown report for one group.
    Returns all markdown lines (for final file write).
    """
    pnls = [float(r["pnl"]) for r in rows]
    m    = compute_metrics(pnls)
    n    = m.get("n", 0)

    print(f"\n{'═' * _W}")
    print(f"  {label}  —  {desc}")
    print(f"{'═' * _W}")
    print(f"  Sample: {n} trades   ›  {maturity(n)}")

    md: list[str] = [
        f"## {label}",
        "",
        f"_{desc}_",
        "",
        f"**Sample:** {n} trades — {maturity(n)}",
        "",
    ]

    if n == 0:
        print("  No closed trades found for this group.\n")
        md += ["_No closed trades._", ""]
        return md

    md += _print_core(m)

    md += _print_bd_table(
        "Exit Reason",
        breakdown(rows, _b_exit, _EXIT_ORDER),
    )

    md += _print_bd_table(
        "Hour Block (America/New_York)",
        breakdown(rows, _b_hour),
        note="N/A = trades recorded before time-feature migration.",
    )

    md += _print_bd_table(
        "Day of Week",
        breakdown(rows, _b_day, _DAY_ORDER),
        note="N/A = trades recorded before time-feature migration.",
    )

    md += _print_bd_table(
        "Spread Bucket",
        breakdown(rows, _b_spread, _SPREAD_ORDER),
    )

    md += _print_bd_table(
        "Directional Momentum  (positive = confirms trade direction)",
        breakdown(rows, _b_dir_momentum, _DIR_MOM_ORDER),
        note="Raw momentum_score sign-flipped for NO trades.",
    )

    md += _print_bd_table(
        "Directional Gap Z-Score  (positive = BTC on correct side)",
        breakdown(rows, _b_dir_gap_z, _DIR_GZ_ORDER),
        note="Raw gap_z_score sign-flipped for NO trades.",
    )

    md.append("---")
    md.append("")
    return md


# ── Comparison table ───────────────────────────────────────────────────────────

def print_comparison(groups_data: list[tuple[str, list[dict]]]) -> list[str]:
    """
    Print a transposed comparison table — metrics as rows, groups as columns.
    Returns markdown lines.
    """
    col_w  = 19   # per-group column width
    lbl_w  = 28   # metric label width

    headers     = [g[0] for g in groups_data]
    all_metrics = {g[0]: compute_metrics([float(r["pnl"]) for r in g[1]])
                   for g in groups_data}

    def _row(metric: str, vals: list[str]) -> str:
        return (
            f"  {metric:<{lbl_w}}"
            + "".join(f"  {v:>{col_w}}" for v in vals)
        )

    print(f"\n{'═' * _W}")
    print("  Strategy Comparison")
    print(f"{'═' * _W}")
    print(
        "\n  ⚠  Values are paper-trade simulations only.\n"
        "     No live trading recommendation is made or implied.\n"
    )
    print(_row("", headers))
    print("  " + "─" * (lbl_w + (col_w + 2) * len(headers)))

    def _vals(fn: Callable[[dict], str]) -> list[str]:
        return [fn(all_metrics[g[0]]) for g in groups_data]

    def _na_if_empty(m: dict, fn: Callable[[dict], str]) -> str:
        return fn(m) if m.get("n", 0) > 0 else "—"

    rows_to_print: list[tuple[str, list[str]]] = [
        ("Trades",            [str(all_metrics[g[0]].get("n", 0)) for g in groups_data]),
        ("Sample maturity",   [maturity(all_metrics[g[0]].get("n", 0)).split()[0]
                               for g in groups_data]),
        ("Win rate",          [_na_if_empty(all_metrics[g[0]],
                               lambda m: f"{m['win_rate']*100:.1f}%")
                               for g in groups_data]),
        ("Total PnL",         [_na_if_empty(all_metrics[g[0]],
                               lambda m: f"{m['total_pnl']:+.4f}")
                               for g in groups_data]),
        ("Avg PnL",           [_na_if_empty(all_metrics[g[0]],
                               lambda m: f"{m['avg_pnl']:+.4f}")
                               for g in groups_data]),
        ("Expectancy",        [_na_if_empty(all_metrics[g[0]],
                               lambda m: f"{m['expectancy']:+.4f}")
                               for g in groups_data]),
        ("Profit factor",     [_na_if_empty(all_metrics[g[0]],
                               lambda m: _pf(m["profit_factor"]))
                               for g in groups_data]),
        ("Max drawdown",      [_na_if_empty(all_metrics[g[0]],
                               lambda m: f"{m['max_drawdown']:+.4f}")
                               for g in groups_data]),
        ("Longest loss streak",[_na_if_empty(all_metrics[g[0]],
                               lambda m: str(m["losing_streak"]))
                               for g in groups_data]),
    ]

    for metric, vals in rows_to_print:
        print(_row(metric, vals))

    print()

    # markdown comparison table
    md_hdrs   = ["Metric"] + [g[0] for g in groups_data]
    md_aligns = [":---"]   + ["---:" for _ in groups_data]
    md: list[str] = [
        "## Strategy Comparison",
        "",
        "> ⚠ Paper-trade simulations only — no live trading recommendation.",
        "",
        "| " + " | ".join(md_hdrs)   + " |",
        "| " + " | ".join(md_aligns) + " |",
    ]
    for metric, vals in rows_to_print:
        md.append("| " + " | ".join([metric] + vals) + " |")
    md.append("")
    return md


# ── Markdown file writer ───────────────────────────────────────────────────────

def write_md(lines: list[str]) -> Path:
    today   = date.today()
    out_dir = Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"pnms_report_{today.strftime('%Y_%m_%d')}.md"

    header = [
        f"# PNMS v1 — Performance Report  {today.isoformat()}",
        "",
        "> ⚠ **PAPER TRADING ONLY** — this report makes no live trading recommendations.",
        "",
        "---",
        "",
    ]
    out_path.write_text("\n".join(header + lines) + "\n")
    return out_path


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    all_rows = _load()

    print(f"\n{'═' * _W}")
    print("  PNMS v1 — Performance Report")
    print(f"{'═' * _W}")
    print(
        "  ⚠  PAPER TRADING ONLY — no live trading recommendations are made.\n"
        f"  Generated: {date.today().isoformat()}\n"
    )

    md_lines: list[str] = []

    # ── Per-group detail reports ──────────────────────────────────────────────
    groups_data: list[tuple[str, list[dict]]] = []
    for g in _GROUPS:
        filtered = [r for r in all_rows if g["filter"](r)]
        groups_data.append((g["label"], filtered))
        md_lines += print_group_report(g["label"], g["desc"], filtered)

    # ── Comparison table ──────────────────────────────────────────────────────
    md_lines += print_comparison(groups_data)

    # ── Closing reminder ──────────────────────────────────────────────────────
    print(f"{'═' * _W}")
    print("  ⚠  PAPER TRADING ONLY — no live trading recommendations.")
    print(f"{'═' * _W}\n")

    # ── Write markdown ────────────────────────────────────────────────────────
    path = write_md(md_lines)
    print(f"Report written → {path}\n")


if __name__ == "__main__":
    main()
