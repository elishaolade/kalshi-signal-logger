#!/usr/bin/env python3
"""
Analyze closed paper trades stored in MySQL.

Part 1 — Single-dimension breakdowns
  Groups by rule_name, rule_version, exit_reason, and five feature buckets
  (time_remaining, contract_price, spread, momentum_score, gap_z_score).

Part 2 — Cross-bucket analysis (n ≥ 10 per cell)
  Answers five specific questions:
    Q1. Does premium_momentum_continuation work better for YES or NO?
    Q2. Does negative momentum outperform positive momentum?
    Q3. Is gap_z_score sign mapped correctly for each side?
    Q4. Does the 0.65–0.80 contract-price edge belong to one side only?
    Q5. Are stop_loss and trailing_stop hurting more than helping?

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
        pt.side,
        pt.exit_reason,
        CAST(pt.pnl AS DOUBLE) AS pnl,
        s.time_remaining_seconds,
        CAST(s.contract_price  AS DOUBLE) AS contract_price,
        CAST(s.spread          AS DOUBLE) AS spread,
        CAST(s.momentum_score  AS DOUBLE) AS momentum_score,
        CAST(s.gap_z_score     AS DOUBLE) AS gap_z_score,
        -- time-of-day features stored on the trade row (NULL for pre-migration rows)
        pt.entry_day_name,
        pt.entry_hour_block,
        pt.entry_30m_block,
        pt.entry_15m_block
    FROM paper_trades pt
    LEFT JOIN signals s ON pt.signal_id = s.id
    WHERE pt.exit_time IS NOT NULL
      AND pt.pnl IS NOT NULL
      AND pt.followed_rules = TRUE
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


def print_table(
    title: str,
    groups: list[tuple[str, dict]],
    min_flag: int = 0,
) -> None:
    hdr  = f"{'Bucket':<{_W_BUCKET}}  " + "  ".join(f"{h:>{w}}" for h, w in _COLS)
    sep  = "-" * _W_BUCKET + "  " + "  ".join("-" * w for _, w in _COLS)
    rule = "─" * max(0, 76 - len(title))

    print(f"\n── {title} {rule}")
    if min_flag:
        print(f"   ⚑ = n < {min_flag}  (insufficient sample size)")
    print(hdr)
    print(sep)
    for bucket, m in groups:
        if m:
            label = bucket if (not min_flag or m["count"] >= min_flag) else f"{bucket}  ⚑"
            print(_console_row(label, m))
    print()


# ── Markdown output ────────────────────────────────────────────────────────────

def _md_table(
    title: str,
    groups: list[tuple[str, dict]],
    min_flag: int = 0,
) -> list[str]:
    hdrs   = ["Bucket"] + [h for h, _ in _COLS]
    aligns = [":---"]   + ["---:" for _ in _COLS]
    lines  = [f"### {title}", ""]
    if min_flag:
        lines += [f"_⚑ = n < {min_flag}  (insufficient sample size)_", ""]
    lines += [
        "| " + " | ".join(hdrs)   + " |",
        "| " + " | ".join(aligns) + " |",
    ]
    for bucket, m in groups:
        if m:
            label = bucket if (not min_flag or m["count"] >= min_flag) else f"{bucket} ⚑"
            cells = [label] + _metric_cells(m)
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def write_report(
    sections:       list[tuple[str, list[tuple[str, dict]], int]],
    overall:        dict,
    cross_sections: Optional[list[tuple[str, list[tuple[str, dict]], str]]] = None,
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
    for title, groups, min_flag in sections:
        lines.extend(_md_table(title, groups, min_flag))

    if cross_sections:
        lines += [
            "---",
            "",
            "## Cross-Bucket Analysis",
            "",
            f"_Only cells with n ≥ {_MIN_CROSS_COUNT} trades are shown. "
            "Sorted by expectancy (best first)._",
            "",
        ]
        for title, groups, note in cross_sections:
            lines.extend(_md_cross_table(title, groups, note))

    out_path.write_text("\n".join(lines) + "\n")
    return out_path


# ── Cross-bucket helpers ───────────────────────────────────────────────────────

_MIN_CROSS_COUNT = 10

# Compact column set for cross tables (drops Med PnL and Max Loss to save width)
_CROSS_W_BUCKET = 52
_CROSS_COLS = [
    ("Count",   6),
    ("Win%",    6),
    ("Avg PnL", 9),
    ("Avg Win", 9),
    ("Avg Loss",9),
    ("Expect",  9),
    ("PF",      6),
    ("Max DD",  9),
]


def _b_side(r: dict) -> str:
    return str(r.get("side") or "N/A")


def _b_rule_short(r: dict) -> str:
    """Abbreviated rule names so composite keys stay readable."""
    name = str(r.get("rule_name") or "N/A")
    return {
        "cheap_reversal_scalp":          "CRS",
        "premium_momentum_continuation": "PMC",
        "premium_momentum_scalp":        "PMS",
    }.get(name, name[:8])


def _b_exit(r: dict) -> str:
    return str(r.get("exit_reason") or "N/A")


def _b_directional_gap_z(r: dict) -> str:
    """
    Gap z-score signed relative to the traded side.
    Positive = BTC is on the winning side of the target.
    YES → use raw gz;  NO → flip sign.
    This tests whether the signal's gz filter is directionally correct.
    """
    z    = _fv(r, "gap_z_score")
    side = r.get("side", "")
    if z is None:
        return "N/A"
    dz = z if side == "YES" else -z
    if dz <= -2: return "≤-2 (vs trade)"
    if dz <= -1: return "-2 to -1 (vs)"
    if dz <   0: return "-1 to 0  (vs)"
    if dz <   1: return "0 to 1   (with)"
    if dz <   2: return "1 to 2   (with)"
    return              "≥2      (with, far)"


def _b_momentum_sign(r: dict) -> str:
    """Signed momentum relative to the traded side."""
    m    = _fv(r, "momentum_score")
    side = r.get("side", "")
    if m is None:
        return "N/A"
    dm = m if side == "YES" else -m
    if dm <= -3: return "≤-3 (vs trade)"
    if dm <   0: return "-3 to 0 (vs)"
    if dm <   3: return "0 to 3  (with)"
    return              "≥3     (with)"


def cross_group_by(
    rows:      list[dict],
    key_fns:   list[tuple[str, Callable[[dict], str]]],
    min_count: int = _MIN_CROSS_COUNT,
    sort_by:   str = "expectancy",
) -> list[tuple[str, dict]]:
    """
    Group rows by the compound key formed by applying each key_fn in order.
    Cells with fewer than min_count trades are dropped.
    Sorted by sort_by descending (best first).
    """
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        key = "  |  ".join(fn(r) for _, fn in key_fns)
        buckets[key].append(float(r["pnl"]))

    results = []
    for label, pnls in buckets.items():
        if len(pnls) < min_count:
            continue
        results.append((label, compute_metrics(pnls)))

    results.sort(
        key=lambda x: x[1].get(sort_by, float("-inf")),
        reverse=True,
    )
    return results


def _cross_metric_cells(m: dict) -> list[str]:
    return [
        str(m["count"]),
        f"{m['win_rate'] * 100:.1f}%",
        f"{m['avg_pnl']:+.4f}",
        f"{m['avg_win']:+.4f}",
        f"{m['avg_loss']:+.4f}",
        f"{m['expectancy']:+.4f}",
        _pf_str(m["profit_factor"]),
        f"{m['max_drawdown']:+.4f}",
    ]


def print_cross_table(
    title:   str,
    groups:  list[tuple[str, dict]],
    note:    str = "",
    col_headers: Optional[list[str]] = None,
) -> None:
    """Print a cross-bucket table with optional column headers override."""
    cols = list(zip(col_headers, [c[1] for c in _CROSS_COLS])) if col_headers else _CROSS_COLS

    hdr = f"{'Bucket':<{_CROSS_W_BUCKET}}  " + "  ".join(f"{h:>{w}}" for h, w in cols)
    sep = "-" * _CROSS_W_BUCKET + "  " + "  ".join("-" * w for _, w in cols)
    rule = "─" * max(0, 76 - len(title))

    print(f"\n── {title} {rule}")
    if note:
        print(f"   {note}")
    print(hdr)
    print(sep)
    for bucket, m in groups:
        if not m:
            continue
        left  = f"{bucket:<{_CROSS_W_BUCKET}}"
        right = "  ".join(f"{v:>{w}}" for v, (_, w) in zip(_cross_metric_cells(m), cols))
        print(f"{left}  {right}")
    if not groups:
        print(f"  (no cells with n ≥ {_MIN_CROSS_COUNT})")
    print()


def _md_cross_table(title: str, groups: list[tuple[str, dict]], note: str = "") -> list[str]:
    hdrs   = ["Bucket"] + [h for h, _ in _CROSS_COLS]
    aligns = [":---"]   + ["---:" for _ in _CROSS_COLS]
    lines  = [f"### {title}", ""]
    if note:
        lines += [f"_{note}_", ""]
    lines += [
        "| " + " | ".join(hdrs)   + " |",
        "| " + " | ".join(aligns) + " |",
    ]
    for bucket, m in groups:
        if m:
            cells = [bucket.replace("|", "\\|")] + _cross_metric_cells(m)
            lines.append("| " + " | ".join(cells) + " |")
    if not groups:
        lines.append(f"_(no cells with n ≥ {_MIN_CROSS_COUNT})_")
    lines.append("")
    return lines


# ── Five-question cross analysis ───────────────────────────────────────────────

def run_cross_analysis(
    rows: list[dict],
) -> list[tuple[str, list[tuple[str, dict]], str]]:
    """
    Build cross-bucket tables for the five research questions.
    Returns list of (title, groups, note) tuples for both console and markdown.
    """
    pmc = [r for r in rows if r.get("rule_name") == "premium_momentum_continuation"]

    sections: list[tuple[str, list[tuple[str, dict]], str]] = []

    # ── Q1: PMC — YES vs NO ───────────────────────────────────────────────────
    q1 = cross_group_by(pmc, [("side", _b_side)])
    sections.append((
        "Q1 · PMC: YES vs NO  (sorted by expectancy)",
        q1,
        "Does premium_momentum_continuation work better on one side?",
    ))

    # ── Q2: Momentum direction × side × rule ──────────────────────────────────
    q2 = cross_group_by(
        rows,
        [("rule", _b_rule_short), ("side", _b_side), ("mom_dir", _b_momentum_sign)],
    )
    sections.append((
        "Q2 · Momentum direction × rule × side  (n ≥ 10, sorted by expectancy)",
        q2,
        "Momentum sign is relative to the traded side: "
        "'with' = confirms direction, 'vs' = counter-direction.",
    ))

    # ── Q3: Directional gap z-score × rule × side ─────────────────────────────
    q3 = cross_group_by(
        rows,
        [("rule", _b_rule_short), ("side", _b_side), ("dgz", _b_directional_gap_z)],
    )
    sections.append((
        "Q3 · Directional gap-z × rule × side  (n ≥ 10, sorted by expectancy)",
        q3,
        "gap_z flipped for NO trades so positive always means "
        "BTC is on the correct side of the target. "
        "If mapping is correct, 'with' buckets should outperform 'vs' buckets.",
    ))

    # ── Q4: Contract price × side ─────────────────────────────────────────────
    q4 = cross_group_by(
        rows,
        [("side", _b_side), ("cp", _b_contract_price)],
    )
    sections.append((
        "Q4 · Contract price × side  (n ≥ 10, sorted by expectancy)",
        q4,
        "Does the 0.65–0.80 edge hold for both YES and NO, or just one side?",
    ))

    # ── Q5: Exit reason × rule × side ─────────────────────────────────────────
    q5 = cross_group_by(
        rows,
        [("rule", _b_rule_short), ("side", _b_side), ("exit", _b_exit)],
        min_count=5,      # relax to 5 here — exit combos are naturally sparse
    )
    sections.append((
        "Q5 · Exit reason × rule × side  (n ≥ 5, sorted by expectancy)",
        q5,
        "Are stop_loss and trailing_stop worse on a particular rule or side?",
    ))

    # ── Time-of-day cross-bucket sections ──────────────────────────────────────
    # Cells with n < 10 are excluded; flagged as insufficient sample size.

    # Q6: strategy × hour_block
    q6 = cross_group_by(
        rows,
        [("rule", _b_rule_short), ("hour", _b_hour_block)],
    )
    sections.append((
        "Q6 · Strategy × hour_block  (n ≥ 10, sorted by expectancy)",
        q6,
        "Does performance vary by hour of day?  Hour is in America/New_York "
        "(or configured SIGNAL_TIMEZONE).  N/A = pre-migration rows.",
    ))

    # Q7: strategy × day_name
    q7 = cross_group_by(
        rows,
        [("rule", _b_rule_short), ("day", _b_day_name)],
    )
    sections.append((
        "Q7 · Strategy × day_name  (n ≥ 10, sorted by expectancy)",
        q7,
        "Does performance differ by day of week?",
    ))

    # Q8: strategy × side × hour_block
    q8 = cross_group_by(
        rows,
        [("rule", _b_rule_short), ("side", _b_side), ("hour", _b_hour_block)],
    )
    sections.append((
        "Q8 · Strategy × side × hour_block  (n ≥ 10, sorted by expectancy)",
        q8,
        "Combines direction and time of day to surface side-specific time edges.",
    ))

    # Q9: PNMS v1 by hour_block
    pnms = [r for r in rows if r.get("rule_name") == "premium_no_midrange_scalp"]
    q9   = cross_group_by(pnms, [("hour", _b_hour_block)])
    sections.append((
        "Q9 · PNMS v1 by hour_block  (n ≥ 10, sorted by expectancy)",
        q9,
        "Which hour of day is most profitable for premium_no_midrange_scalp?",
    ))

    # Q10: PNMS v1 by day_name
    q10 = cross_group_by(pnms, [("day", _b_day_name)])
    sections.append((
        "Q10 · PNMS v1 by day_name  (n ≥ 10, sorted by expectancy)",
        q10,
        "Which day of week is most profitable for premium_no_midrange_scalp?",
    ))

    return sections


# ── Time-of-day bucket helpers ────────────────────────────────────────────────

# Canonical orderings so tables read chronologically, not alphabetically.
_DAY_ORDER: list[str] = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday", "N/A",
]
_HOUR_ORDER:    list[str] = [f"{h:02d}:00" for h in range(24)] + ["N/A"]
_BLOCK_30M_ORDER: list[str] = [
    f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)
] + ["N/A"]
_BLOCK_15M_ORDER: list[str] = [
    f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)
] + ["N/A"]

_MIN_TIME_SAMPLE = 10   # flag rows below this count in time-based tables


def _b_day_name(r: dict) -> str:
    return str(r.get("entry_day_name") or "N/A")


def _b_hour_block(r: dict) -> str:
    return str(r.get("entry_hour_block") or "N/A")


def _b_30m_block(r: dict) -> str:
    return str(r.get("entry_30m_block") or "N/A")


def _b_15m_block(r: dict) -> str:
    return str(r.get("entry_15m_block") or "N/A")


# ── Dimension registry ─────────────────────────────────────────────────────────
#
# Each entry: (title, key_fn, order, min_flag)
# min_flag: rows with count < min_flag get a "  ⚑" suffix in console output.
#           0 = no flagging.

_DIMENSIONS: list[tuple[str, Callable, Optional[list[str]], int]] = [
    (
        "rule_name",
        lambda r: str(r["rule_name"] or "N/A"),
        None,
        0,
    ),
    (
        "rule_version",
        lambda r: str(r["rule_version"] or "N/A"),
        None,
        0,
    ),
    (
        "exit_reason",
        lambda r: str(r["exit_reason"] or "N/A"),
        ["near_expiry", "take_profit", "stop_loss",
         "trailing_stop", "break_even_stop", "timeout_60s", "N/A"],
        0,
    ),
    (
        "time_remaining",
        _b_time_remaining,
        ["<60s", "60-120s", "120-180s", "180-240s", "240-300s", "300s+", "N/A"],
        0,
    ),
    (
        "contract_price",
        _b_contract_price,
        ["<0.15", "0.15-0.25", "0.25-0.35", "0.35-0.50",
         "0.50-0.65", "0.65-0.80", "0.80+", "N/A"],
        0,
    ),
    (
        "spread",
        _b_spread,
        ["<0.01", "0.01-0.02", "0.02-0.03", "0.03+", "N/A"],
        0,
    ),
    (
        "momentum_score",
        _b_momentum,
        ["≤-5", "-5 to -3", "-3 to 0", "0", "0 to 3", "3 to 5", ">5", "N/A"],
        0,
    ),
    (
        "gap_z_score",
        _b_gap_z,
        ["≤-2", "-2 to -1", "-1 to 0", "0 to 1", "1 to 2", "≥2", "N/A"],
        0,
    ),
    # ── Time-of-day dimensions (flag rows with n < 10) ────────────────────────
    (
        "day_name",
        _b_day_name,
        _DAY_ORDER,
        _MIN_TIME_SAMPLE,
    ),
    (
        "hour_block",
        _b_hour_block,
        _HOUR_ORDER,
        _MIN_TIME_SAMPLE,
    ),
    (
        "block_30m",
        _b_30m_block,
        _BLOCK_30M_ORDER,
        _MIN_TIME_SAMPLE,
    ),
    (
        "block_15m",
        _b_15m_block,
        _BLOCK_15M_ORDER,
        _MIN_TIME_SAMPLE,
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

    sections: list[tuple[str, list[tuple[str, dict]], int]] = []
    for title, key_fn, order, min_flag in _DIMENSIONS:
        groups = group_by(rows, key_fn, order)
        print_table(title, groups, min_flag)
        sections.append((title, groups, min_flag))

    # ── Cross-bucket analysis ─────────────────────────────────────────────────
    print(f"\n{'═' * 96}")
    print("  Cross-Bucket Analysis")
    print(f"{'═' * 96}")
    print(f"  (Only cells with n ≥ {_MIN_CROSS_COUNT} shown, except Q5 where n ≥ 5.)")
    print("  Sorted by expectancy — best cells first.\n")

    cross_sections = run_cross_analysis(rows)
    for title, groups, note in cross_sections:
        print_cross_table(title, groups, note)

    path = write_report(sections, overall, cross_sections)
    print(f"Report written → {path}\n")


if __name__ == "__main__":
    main()
