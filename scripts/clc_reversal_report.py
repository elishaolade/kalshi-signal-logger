#!/usr/bin/env python3
"""
clc_reversal_report.py — Research report for the cheap-losing-contract reversal
family: cheap_losing_contract_reversal_trail/v1 (early window) and
cheap_losing_contract_late_reversal/v1 (last 1–5 minutes).

Both strategies are **watch-only**.  No paper trade and no live order is ever
opened.  The CLCReversalTracker buys (hypothetically) the cheap *losing*
contract and simulates FIVE exit profiles against the same observed bid path,
writing one row per (signal x exit_profile) into `clc_reversal_observations`.

By default the report covers ALL CLC rules and leads with an early-vs-late
breakdown so the two entry-timing hypotheses can be compared directly.  Pass
`--rule <rule_name>` to scope the report to a single variant.

Row semantics
-------------
Each row is one (signal x exit_profile) outcome.  P&L uses the realized fill
model (entry = ask + slippage, exit = bid - slippage), so expectancy already
accounts for spread + slippage.  The headline table groups by exit_profile;
the remaining dimension tables aggregate ACROSS all five profiles (so their
counts are ~5x the number of signals) and "Trail%" is computed only over
trailing-profile rows in each group.

Research questions this answers
-------------------------------
  1. Does the early cheap-losing-contract reversal setup reverse often enough?
  2. Does trail-after-activation beat fixed take-profit?
  3. Is +20% activation better than +15%?
  4. Is a 1c or 2c trailing stop better?
  5. Which price bucket performs best (5–10c / 10–20c / 20–30c)?
  6. Does it work better in the first 2 minutes than later?
  7. Does historical reversal probability improve expectancy?
  8. Does violent volatility destroy the setup?

⚠  WATCH-ONLY RESEARCH — NO PAPER TRADES, NO LIVE TRADING.

Usage:
    python scripts/clc_reversal_report.py
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all

# ── Constants ──────────────────────────────────────────────────────────────────

_W    = 116

_SAMPLE_STEPS = [
    (100, "FIRST SERIOUS REVIEW (100+ outcomes)"),
    ( 50, "EARLY CLUE           (50–99 outcomes)"),
    ( 25, "SMOKE TEST ONLY      (25–49 outcomes)"),
    (  0, "INSUFFICIENT         (<25 outcomes)"),
]

_PROFILE_ORDER = [
    "fixed_20pct_stop_15pct",
    "fixed_15pct_stop_10pct",
    "ride_then_trail_20pct_2c",
    "ride_then_trail_15pct_2c",
    "ride_then_trail_20pct_1c",
]
_PHASE_ORDER  = ["first_2min", "min_2_to_3", "late_1_to_5min", "N/A"]
_RULE_ORDER   = ["early (trail/v1)", "late (late_rev/v1)"]
_AGE_ORDER    = ["0-60s", "60-120s", "120-180s", "180s+", "N/A"]
_PRICE_ORDER  = ["0.05-0.10", "0.10-0.20", "0.20-0.30", "N/A"]
_Z_ORDER      = ["<0.0", "0.0-1.5", "1.5-2.5", "2.5-3.5", "3.5+", "N/A"]
_PROB_ORDER   = ["none", "<0.45", "0.45-0.55", "0.55-0.65", "0.65+", "N/A"]
_SPREAD_ORDER = ["<0.01", "0.01-0.02", "0.02-0.03", "N/A"]
_VOL_ORDER    = ["calm", "normal", "elevated", "violent", "unknown", "N/A"]
_DAY_ORDER    = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday", "N/A"]


# ── SQL ────────────────────────────────────────────────────────────────────────

_QUERY = """
    SELECT
        signal_id,
        rule_name,
        rule_version,
        exit_profile,
        market_phase,
        CAST(market_age_seconds         AS DOUBLE) AS market_age_seconds,
        CAST(losing_contract_ask        AS DOUBLE) AS losing_contract_ask,
        CAST(adverse_z_score            AS DOUBLE) AS adverse_z_score,
        CAST(historical_reversal_probability AS DOUBLE) AS hrp,
        CAST(losing_contract_spread     AS DOUBLE) AS spread,
        volatility_regime,
        hour_block,
        day_name,
        side_bought,
        trail_activated,
        CAST(max_favorable_excursion    AS DOUBLE) AS mfe,
        CAST(pnl                        AS DOUBLE) AS pnl,
        exit_reason
    FROM clc_reversal_observations
    WHERE status = 'COMPLETE'
      AND complete_reason IN ('timeout', 'near_expiry', 'market_rollover')
      AND pnl IS NOT NULL
      {rule_filter}
    ORDER BY recorded_at ASC
"""


def _load(rule: Optional[str], version: Optional[str]) -> list[dict]:
    """Load completed observations.  When rule is None, load ALL rules so the
    report can compare early vs late side by side."""
    if rule is None:
        return fetch_all(_QUERY.format(rule_filter=""), ())
    sql = _QUERY.format(rule_filter="AND rule_name = %s AND rule_version = %s")
    return fetch_all(sql, (rule, version))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fv(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    return None if v is None else float(v)


def _is_trail(profile: str) -> bool:
    return profile.startswith("ride_then_trail")


def maturity(n: int) -> str:
    for threshold, label in _SAMPLE_STEPS:
        if n >= threshold:
            return label
    return _SAMPLE_STEPS[-1][1]


def _pf(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def compute_metrics(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}

    pnls   = [float(r["pnl"]) for r in rows]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr     = len(wins) / n
    aw     = mean(wins)   if wins   else 0.0
    al     = mean(losses) if losses else 0.0
    gp     = sum(wins)
    gl     = abs(sum(losses))

    mfes      = [_fv(r, "mfe") for r in rows if _fv(r, "mfe") is not None]
    givebacks = [
        _fv(r, "mfe") - float(r["pnl"])
        for r in rows if _fv(r, "mfe") is not None
    ]

    trail_rows = [r for r in rows if _is_trail(str(r.get("exit_profile")))]
    trail_act  = [bool(r.get("trail_activated")) for r in trail_rows
                  if r.get("trail_activated") is not None]

    def _reason_freq(*reasons: str) -> float:
        return sum(1 for r in rows if r.get("exit_reason") in reasons) / n

    return {
        "n":              n,
        "win_rate":       wr,
        "total_pnl":      sum(pnls),
        "avg_pnl":        mean(pnls),
        "med_pnl":        median(pnls),
        "avg_win":        aw,
        "avg_loss":       al,
        "expectancy":     wr * aw + (1 - wr) * al,
        "profit_factor":  gp / gl if gl > 0 else float("inf"),
        "avg_peak":       mean(mfes)      if mfes      else None,
        "avg_realized":   mean(pnls),
        "avg_giveback":   mean(givebacks) if givebacks else None,
        "trail_act_rate": (sum(trail_act) / len(trail_act)) if trail_act else None,
        "timeout_freq":   _reason_freq("timeout", "near_expiry", "market_rollover"),
        "stop_freq":      _reason_freq("stop_loss", "hard_stop"),
        "violent_freq":   _reason_freq("violent_vol_exit"),
    }


# ── Bucket helpers ─────────────────────────────────────────────────────────────

def _b_profile(r: dict) -> str:
    return str(r.get("exit_profile") or "N/A")


_RULE_LABELS = {
    "cheap_losing_contract_reversal_trail/v1": "early (trail/v1)",
    "cheap_losing_contract_late_reversal/v1":  "late (late_rev/v1)",
}


def _b_rule(r: dict) -> str:
    rn = r.get("rule_name")
    rv = r.get("rule_version")
    if not rn:
        return "N/A"
    key = f"{rn}/{rv}"
    return _RULE_LABELS.get(key, key)


def _b_phase(r: dict) -> str:
    return str(r.get("market_phase") or "N/A")


def _b_age(r: dict) -> str:
    a = _fv(r, "market_age_seconds")
    if a is None:  return "N/A"
    if a < 60:     return "0-60s"
    if a < 120:    return "60-120s"
    if a < 180:    return "120-180s"
    return                "180s+"


def _b_price(r: dict) -> str:
    p = _fv(r, "losing_contract_ask")
    if p is None:  return "N/A"
    if p < 0.10:   return "0.05-0.10"
    if p < 0.20:   return "0.10-0.20"
    return                "0.20-0.30"


def _b_z(r: dict) -> str:
    z = _fv(r, "adverse_z_score")
    if z is None:  return "N/A"
    if z < 0.0:    return "<0.0"
    if z < 1.5:    return "0.0-1.5"
    if z < 2.5:    return "1.5-2.5"
    if z < 3.5:    return "2.5-3.5"
    return                "3.5+"


def _b_prob(r: dict) -> str:
    p = _fv(r, "hrp")
    if p is None:  return "none"
    if p < 0.45:   return "<0.45"
    if p < 0.55:   return "0.45-0.55"
    if p < 0.65:   return "0.55-0.65"
    return                "0.65+"


def _b_spread(r: dict) -> str:
    s = _fv(r, "spread")
    if s is None:  return "N/A"
    if s < 0.01:   return "<0.01"
    if s < 0.02:   return "0.01-0.02"
    return                "0.02-0.03"


def _b_vol(r: dict) -> str:
    return str(r.get("volatility_regime") or "N/A")


def _b_hour(r: dict) -> str:
    return str(r.get("hour_block") or "N/A")


def _b_day(r: dict) -> str:
    return str(r.get("day_name") or "N/A")


def _b_side(r: dict) -> str:
    return str(r.get("side_bought") or "N/A")


# ── Grouping ───────────────────────────────────────────────────────────────────

def breakdown(
    rows:   list[dict],
    key_fn: Callable[[dict], str],
    order:  Optional[list[str]] = None,
) -> list[tuple[str, dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[key_fn(r)].append(r)

    if order:
        items = [(k, buckets[k]) for k in order if k in buckets]
        seen  = set(order)
        for k in sorted(buckets):
            if k not in seen:
                items.append((k, buckets[k]))
    else:
        items = sorted(buckets.items())

    return [(lbl, compute_metrics(v)) for lbl, v in items]


# ── Rendering ──────────────────────────────────────────────────────────────────

_BD_W = 24
_BD_COLS = [
    ("N",        5),
    ("Win%",     6),
    ("AvgPnL",   8),
    ("Expect",   8),
    ("PF",       5),
    ("Trail%",   7),
    ("Peak",     8),
    ("Giveback", 9),
    ("Stop%",    6),
    ("Viol%",    6),
]


def _bd_cells(m: dict) -> list[str]:
    return [
        str(m["n"]),
        f"{m['win_rate'] * 100:.1f}%",
        f"{m['avg_pnl']:+.4f}",
        f"{m['expectancy']:+.4f}",
        _pf(m["profit_factor"]),
        _pct(m["trail_act_rate"]),
        ("—" if m["avg_peak"] is None else f"{m['avg_peak']:+.4f}"),
        ("—" if m["avg_giveback"] is None else f"{m['avg_giveback']:+.4f}"),
        _pct(m["stop_freq"]),
        _pct(m["violent_freq"]),
    ]


def _print_bd_table(
    title:  str,
    groups: list[tuple[str, dict]],
    note:   str = "",
) -> list[str]:
    hdr = f"  {'Bucket':<{_BD_W}}" + "  ".join(f"{h:>{w}}" for h, w in _BD_COLS)
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
        if not m.get("n"):
            continue
        cells = _bd_cells(m)
        print(f"  {bucket:<{_BD_W}}" + "  ".join(f"{v:>{w}}" for v, (_, w) in zip(cells, _BD_COLS)))
        md.append("| " + " | ".join([bucket] + cells) + " |")
        any_data = True

    if not any_data:
        print("   (no data)")
        md.append("_(no data)_")

    print()
    md.append("")
    return md


# ── Markdown writer ─────────────────────────────────────────────────────────────

def write_md(lines: list[str], title: str, scope_tag: str) -> Path:
    today   = date.today()
    out_dir = Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"clc_reversal_report_{scope_tag}_{today.strftime('%Y_%m_%d')}.md"

    header = [
        f"# {title} — Research Report  {today.isoformat()}",
        "",
        "> ⚠ **WATCH-ONLY RESEARCH** — no paper trades and no live trading.",
        "> Each row is one (signal × exit_profile) outcome; P&L uses",
        "> entry = ask + slippage and exit = bid − slippage (spread/slippage included).",
        "> Non-profile tables aggregate across all five profiles (~5 rows per signal);",
        "> Trail% is computed only over trailing-profile rows in each group.",
        "",
        "---",
        "",
    ]
    out_path.write_text("\n".join(header + lines) + "\n")
    return out_path


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Research report for the cheap-losing-contract reversal family "
                    "(watch-only; early and late variants)."
    )
    ap.add_argument(
        "--rule", default=None,
        help="rule_name to scope to (e.g. cheap_losing_contract_late_reversal). "
             "Default: ALL CLC rules, with an early-vs-late breakdown.",
    )
    ap.add_argument("--version", default="v1", help="rule_version (default: v1)")
    ap.add_argument("--all", action="store_true",
                    help="Force all-rules report (default when --rule omitted).")
    args = ap.parse_args()

    rule    = None if args.all else args.rule
    version = args.version if rule else None
    rows    = _load(rule, version)

    if rule:
        scope_label = f"{rule}/{version}"
        scope_tag   = rule.replace("cheap_losing_contract_", "")
    else:
        scope_label = "all CLC rules (early vs late)"
        scope_tag   = "all"
    title = f"cheap-losing-contract reversal — {scope_label}"

    n_rows    = len(rows)
    n_signals = len({r.get("signal_id") for r in rows if r.get("signal_id") is not None})

    print(f"\n{'═' * _W}")
    print(f"  {title} — Research Report")
    print(f"{'═' * _W}")
    print(
        "  ⚠  WATCH-ONLY RESEARCH — no paper trades, no live trading.\n"
        "     Realized fill model: entry = ask+slip, exit = bid-slip.\n"
        f"  Generated: {date.today().isoformat()}\n"
    )
    print(f"  Profile-outcome rows: {n_rows}   ›  {maturity(n_rows)}")
    print(f"  Distinct signals:     {n_signals}   ›  {maturity(n_signals)}")

    md: list[str] = [
        f"**Scope:** {scope_label}",
        f"**Profile-outcome rows:** {n_rows} — {maturity(n_rows)}",
        f"**Distinct signals:** {n_signals} — {maturity(n_signals)}",
        "",
    ]

    if n_rows == 0:
        print("\n  No completed observations yet.\n")
        md += ["_No completed observations yet._", ""]
        path = write_md(md, title, scope_tag)
        print(f"Report written → {path}\n")
        return

    # When reporting all rules, lead with the early-vs-late comparison.
    if rule is None:
        md += _print_bd_table(
            "By Rule Name  (early-window vs late-window entry timing)",
            breakdown(rows, _b_rule, _RULE_ORDER),
            "Aggregated across all 5 exit profiles (≈5 rows per signal).",
        )

    # Headline: compare the five exit profiles (Q2–Q4).
    md += _print_bd_table(
        "By Exit Profile  (headline — compares fixed vs ride-then-trail)",
        breakdown(rows, _b_profile, _PROFILE_ORDER),
    )

    blended_note = "Aggregated across all 5 exit profiles (≈5 rows per signal)."
    md += _print_bd_table("By Market Phase  (Q6: first 2 min / 2–3 min / late 1–5 min)",
                          breakdown(rows, _b_phase, _PHASE_ORDER), blended_note)
    md += _print_bd_table("By Market Age", breakdown(rows, _b_age, _AGE_ORDER), blended_note)
    md += _print_bd_table("By Losing-Contract Price  (Q5)",
                          breakdown(rows, _b_price, _PRICE_ORDER), blended_note)
    md += _print_bd_table("By Adverse Z-Score", breakdown(rows, _b_z, _Z_ORDER), blended_note)
    md += _print_bd_table("By Historical Reversal Probability  (Q7)",
                          breakdown(rows, _b_prob, _PROB_ORDER),
                          "'none' = insufficient_data at signal time. " + blended_note)
    md += _print_bd_table("By Spread", breakdown(rows, _b_spread, _SPREAD_ORDER), blended_note)
    md += _print_bd_table("By Volatility Regime  (Q8)",
                          breakdown(rows, _b_vol, _VOL_ORDER), blended_note)
    md += _print_bd_table("By Hour Block (America/New_York)",
                          breakdown(rows, _b_hour), blended_note)
    md += _print_bd_table("By Day of Week", breakdown(rows, _b_day, _DAY_ORDER), blended_note)
    md += _print_bd_table("By Side Bought (losing side)",
                          breakdown(rows, _b_side), blended_note)

    print(f"{'═' * _W}")
    print("  ⚠  WATCH-ONLY RESEARCH — no paper trades, no live trading recommendation.")
    print("  Promotion gates: ≥50 signals (early), ≥100 (serious), positive")
    print("  post-cost expectancy, profit factor > 1.20, acceptable drawdown.")
    print(f"{'═' * _W}\n")

    path = write_md(md, title, scope_tag)
    print(f"Report written → {path}\n")


if __name__ == "__main__":
    main()
