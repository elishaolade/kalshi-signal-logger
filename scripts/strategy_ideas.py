#!/usr/bin/env python3
"""
Strategy idea generator from paper trade database.

For each variable bucket (contract_price, time_remaining, spread,
momentum_score, gap_z_score, volatility regime, side, btc_velocity)
the script identifies positive/negative expectancy regions, scores each
finding by effect size and sample quality, then surfaces:

  • Top 5 promising strategy ideas  (where to concentrate)
  • Top 5 watchlist buckets         (better than baseline, not profitable yet)
  • Top 5 no-trade filters           (where to stop trading)
  • Top 5 exit experiments           (how to tune exits)

Writes a Markdown report to reports/strategy_ideas_YYYY_MM_DD.md.

Do NOT use these findings to place real trades.

Usage:
    python scripts/strategy_ideas.py
    python scripts/strategy_ideas.py --min-sample 20
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all

# ── Constants ──────────────────────────────────────────────────────────────────

MIN_SAMPLE_DEFAULT = 15   # suppress findings below this (too noisy)
WEAK_SAMPLE        = 30   # warn but include findings below this

# Minimum |effect size| to surface a finding at all (avoids noise)
_MIN_EFFECT = 0.4
_MIN_STD_ERR = 0.005

# ── Data ───────────────────────────────────────────────────────────────────────

_QUERY = """
    SELECT
        pt.rule_name,
        pt.rule_version,
        pt.side,
        pt.exit_reason,
        CAST(pt.pnl           AS DOUBLE) AS pnl,
        CAST(pt.entry_price   AS DOUBLE) AS entry_price,
        s.time_remaining_seconds,
        CAST(s.contract_price AS DOUBLE) AS contract_price,
        CAST(s.spread         AS DOUBLE) AS spread,
        CAST(s.momentum_score AS DOUBLE) AS momentum_score,
        CAST(s.gap_z_score    AS DOUBLE) AS gap_z_score,
        CAST(s.volatility_30s AS DOUBLE) AS volatility_30s,
        CAST(s.btc_velocity   AS DOUBLE) AS btc_velocity
    FROM paper_trades pt
    LEFT JOIN signals s ON pt.signal_id = s.id
    WHERE pt.exit_time IS NOT NULL
      AND pt.pnl IS NOT NULL
      AND pt.followed_rules = TRUE
    ORDER BY pt.entry_time
"""


def load_trades() -> list[dict]:
    return fetch_all(_QUERY)


# ── Bucket functions ───────────────────────────────────────────────────────────

def _fv(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    return None if v is None else float(v)


def _b_rule(r: dict) -> str:
    rule = r.get("rule_name") or "N/A"
    version = r.get("rule_version") or "N/A"
    return f"{rule}/{version}"

def _b_side(r: dict) -> str:
    return str(r.get("side") or "N/A")

def _b_exit(r: dict) -> str:
    return str(r.get("exit_reason") or "N/A")

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
    if s < 0.01:  return "<$0.01"
    if s < 0.02:  return "$0.01–0.02"
    if s < 0.03:  return "$0.02–0.03"
    return "≥$0.03"

def _b_momentum(r: dict) -> str:
    m = _fv(r, "momentum_score")
    if m is None:  return "N/A"
    if m <= -5:    return "≤ −5"
    if m <= -3:    return "−5 to −3"
    if m < 0:      return "−3 to 0"
    if m == 0:     return "0"
    if m < 3:      return "0 to 3"
    if m <= 5:     return "3 to 5"
    return "> 5"

def _b_gap_z(r: dict) -> str:
    z = _fv(r, "gap_z_score")
    if z is None:  return "N/A"
    if z <= -2:    return "≤ −2"
    if z <= -1:    return "−2 to −1"
    if z < 0:      return "−1 to 0"
    if z < 1:      return "0 to 1"
    if z < 2:      return "1 to 2"
    return "≥ 2"

def _b_volatility(r: dict) -> str:
    v = _fv(r, "volatility_30s")
    if v is None: return "N/A"
    if v < 5:     return "flat (<$5)"
    if v < 15:    return "low ($5–15)"
    if v < 30:    return "normal ($15–30)"
    if v < 60:    return "elevated ($30–60)"
    return "high (>$60)"

def _b_velocity(r: dict) -> str:
    v = _fv(r, "btc_velocity")
    if v is None:  return "N/A"
    if v < -2.0:   return "falling fast (<−$2/s)"
    if v < -0.5:   return "falling (−$2 to −$0.50/s)"
    if v <= 0.5:   return "flat (±$0.50/s)"
    if v <= 2.0:   return "rising ($0.50–2/s)"
    return "rising fast (>$2/s)"

def _b_rule_side(r: dict) -> str:
    rule = r.get("rule_name") or "?"
    version = r.get("rule_version") or "?"
    side = r.get("side") or "?"
    return f"{rule}/{version} / {side}"


# ── Entry-dimension registry ───────────────────────────────────────────────────
# Each tuple: (key, bucket_fn, human label)
# exit_reason is excluded here — handled separately in generate_exit_findings.

_ENTRY_DIMS: list[tuple[str, Callable, str]] = [
    ("rule",           _b_rule,           "Strategy rule"),
    ("side",           _b_side,           "Side (YES / NO)"),
    ("rule_side",      _b_rule_side,      "Rule × side"),
    ("time_remaining", _b_time_remaining, "Time remaining at entry"),
    ("contract_price", _b_contract_price, "Contract price at entry"),
    ("spread",         _b_spread,         "Bid/ask spread at entry"),
    ("momentum_score", _b_momentum,       "Momentum score at entry"),
    ("gap_z_score",    _b_gap_z,          "Gap z-score at entry"),
    ("volatility",     _b_volatility,     "BTC 30s volatility at entry"),
    ("btc_velocity",   _b_velocity,       "BTC price velocity at entry"),
]


# ── Metrics ────────────────────────────────────────────────────────────────────

def _metrics(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    if n == 0:
        return {"n": 0, "expectancy": 0.0, "std_err": float("inf"),
                "win_rate": 0.0, "avg_pnl": 0.0, "med_pnl": 0.0,
                "pf": 0.0, "std_pnl": 0.0}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    mu     = mean(pnls)
    sd     = stdev(pnls) if n > 1 else 0.0
    se     = max(sd / math.sqrt(n), _MIN_STD_ERR) if sd > 0 else _MIN_STD_ERR
    gp     = sum(wins)
    gl     = abs(sum(losses))
    return {
        "n":          n,
        "win_rate":   len(wins) / n,
        "avg_pnl":    mu,
        "med_pnl":    median(pnls),
        "std_pnl":    sd,
        "std_err":    se,
        "avg_win":    mean(wins)   if wins   else 0.0,
        "avg_loss":   mean(losses) if losses else 0.0,
        "expectancy": mu,
        "pf":         gp / gl if gl > 0 else float("inf"),
    }


def _effect(bucket_exp: float, overall_exp: float, se: float) -> float:
    """t-like effect: how many standard errors bucket mean differs from overall."""
    return (bucket_exp - overall_exp) / se if se > 0 else 0.0


def _score(effect: float, n: int) -> float:
    """Combine effect size with a sample-size reliability weight."""
    return abs(effect) * min(1.0, n / 50.0)


def _pf_str(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _sample_tag(n: int, min_sample: int) -> str:
    if n < min_sample:   return f"⚠ Very small ({n} trades) — unreliable"
    if n < WEAK_SAMPLE:  return f"⚠ Small ({n} trades) — directional only"
    if n < 100:          return f"{n} trades — adequate for directional guidance"
    return f"{n} trades — solid sample"


# ── Finding dataclass ──────────────────────────────────────────────────────────

@dataclass
class Finding:
    ftype:        str    # "strategy_idea" | "watchlist" | "no_trade_filter" | "exit_experiment"
    title:        str
    hypothesis:   str    # one actionable sentence
    evidence:     str    # multi-line narrative
    dimension:    str    # dimension key
    dim_label:    str    # human-readable dimension name
    bucket:       str    # bucket label that triggered this finding
    n:            int
    expectancy:   float
    overall_exp:  float
    effect:       float
    score:        float
    small_sample: bool


# ── Finding generators ─────────────────────────────────────────────────────────

def _pct_delta(a: float, b: float) -> str:
    if b == 0:
        return "N/A"
    return f"{(a - b) / abs(b) * 100:+.0f}%"


def generate_entry_findings(
    rows: list[dict],
    overall: dict,
    total_n: int,
    min_sample: int,
) -> list[Finding]:
    findings: list[Finding] = []
    oe = overall["expectancy"]
    owr = overall["win_rate"]

    for dim_key, key_fn, dim_label in _ENTRY_DIMS:
        groups: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            lbl = key_fn(r)
            if lbl != "N/A":
                groups[lbl].append(float(r["pnl"]))

        for bucket, pnls in groups.items():
            m  = _metrics(pnls)
            n  = m["n"]
            if n < min_sample:
                continue

            exp    = m["expectancy"]
            effect = _effect(exp, oe, m["std_err"])
            score  = _score(effect, n)

            if abs(effect) < _MIN_EFFECT:
                continue

            pct_of_trades = n / total_n * 100
            wr_delta      = (m["win_rate"] - owr) * 100

            is_profitable = exp > 0 and m["pf"] > 1.0

            if effect > 0 and is_profitable:
                ftype = "strategy_idea"
                title = f"Concentrate on {dim_label} = {bucket}"
                hypothesis = (
                    f"Add entry filter: only take trades where {dim_label} "
                    f"is in '{bucket}'. This regime covers {pct_of_trades:.0f}% "
                    f"of trades and shows {_pct_delta(exp, oe)} better expectancy."
                )
            elif effect > 0:
                ftype = "watchlist"
                title = f"Watch {dim_label} = {bucket}"
                hypothesis = (
                    f"This bucket beats the current baseline but is not profitable yet "
                    f"(expectancy {exp:+.4f}, PF {_pf_str(m['pf'])}). Keep collecting "
                    f"data before promoting it into an entry filter."
                )
            else:
                ftype = "no_trade_filter"
                title = f"Avoid {dim_label} = {bucket}"
                hypothesis = (
                    f"Add entry rejection: skip signals where {dim_label} "
                    f"is in '{bucket}'. This would exclude {pct_of_trades:.0f}% "
                    f"of trades with {_pct_delta(exp, oe)} worse expectancy."
                )

            evidence = (
                f"  Expectancy:   {exp:+.4f}  vs  {oe:+.4f} overall  "
                f"({_pct_delta(exp, oe)})\n"
                f"  Win rate:     {m['win_rate']*100:.1f}%  vs  {owr*100:.1f}% overall  "
                f"({wr_delta:+.1f}pp)\n"
                f"  Profit factor: {_pf_str(m['pf'])}  |  "
                f"Effect size: {effect:+.2f}σ  |  "
                f"Sample: {_sample_tag(n, min_sample)}"
            )

            findings.append(Finding(
                ftype=ftype, title=title, hypothesis=hypothesis,
                evidence=evidence, dimension=dim_key, dim_label=dim_label,
                bucket=bucket, n=n, expectancy=exp, overall_exp=oe,
                effect=effect, score=score,
                small_sample=(n < WEAK_SAMPLE),
            ))

    return findings


# Exit reason → actionable suggestion
_EXIT_ADVICE: dict[str, dict[str, str]] = {
    "near_expiry": {
        "bad":  "Add a max-time-remaining entry filter to avoid entering late in the "
                "window, or add an explicit 'exit N seconds before expiry' rule.",
        "good": "Holding into near-expiry is adding value here. Confirm this is "
                "intentional and not a data artifact.",
    },
    "timeout_60s": {
        "bad":  "The 60s timeout is expiring stale trades at a loss. Options: "
                "(a) shorten timeout to 30–45s, (b) add a momentum-decay exit, "
                "(c) tighten the entry filter.",
        "good": "Timeout exits are profitable — this is unusual. Verify trade "
                "direction and that timeout isn't capturing wins by coincidence.",
    },
    "stop_loss": {
        "bad":  "High-frequency stop-loss exits are normal but may indicate the "
                "entry filter is too permissive. Consider tightening one entry "
                "condition, or widen the stop slightly.",
        "good": "Stop-loss exits showing positive PnL is unexpected. The stop "
                "may be set too far — consider tightening it.",
    },
    "trailing_stop": {
        "bad":  "Trailing stop may be exiting winners prematurely. Options: "
                "(a) widen trail distance, (b) raise the arming threshold.",
        "good": "Trailing stop is the top profit driver — it is locking in gains "
                "correctly. Preserve this exit rule.",
    },
    "take_profit": {
        "bad":  "Take-profit exits showing poor PnL is a data anomaly — verify "
                "the exit price logic and that entry vs exit sides are correct.",
        "good": "Take-profit is the primary profit generator. Consider widening "
                "the target to capture larger moves.",
    },
    "break_even_stop": {
        "bad":  "Break-even stop is activating but price is recovering afterward. "
                "Options: (a) raise the profit threshold that arms the stop, "
                "(b) add a small buffer above entry price.",
        "good": "Break-even stop is preserving capital effectively.",
    },
}


def generate_exit_findings(
    rows: list[dict],
    overall: dict,
    min_sample: int,
) -> list[Finding]:
    findings: list[Finding] = []
    oe = overall["expectancy"]

    # Group by rule × exit_reason
    by_rule_exit: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_exit:      dict[str, list[float]]             = defaultdict(list)

    for r in rows:
        rule   = str(r.get("rule_name") or "N/A")
        reason = str(r.get("exit_reason") or "N/A")
        pnl    = float(r["pnl"])
        by_rule_exit[(rule, reason)].append(pnl)
        by_exit[reason].append(pnl)

    # Overall metrics per rule (for reference)
    by_rule: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_rule[str(r.get("rule_name") or "N/A")].append(float(r["pnl"]))

    total_n = len(rows)

    # Exit-wide findings (all rules combined)
    for reason, pnls in by_exit.items():
        if reason == "N/A" or len(pnls) < min_sample:
            continue
        m      = _metrics(pnls)
        effect = _effect(m["expectancy"], oe, m["std_err"])
        if abs(effect) < _MIN_EFFECT:
            continue

        polarity = "bad" if effect < 0 else "good"
        advice = _EXIT_ADVICE.get(reason, {}).get(polarity, "")
        pct_exits = m["n"] / total_n * 100

        evidence = (
            f"  Exit accounts for:  {pct_exits:.0f}% of all exits ({m['n']} trades)\n"
            f"  Avg PnL:            {m['avg_pnl']:+.4f}  vs  {oe:+.4f} overall\n"
            f"  Win rate:           {m['win_rate']*100:.1f}%  |  "
            f"Profit factor: {_pf_str(m['pf'])}  |  "
            f"Effect size: {effect:+.2f}σ\n"
            f"  Sample:             {_sample_tag(m['n'], min_sample)}"
        )

        findings.append(Finding(
            ftype="exit_experiment",
            title=f"Exit experiment: review '{reason}' exits",
            hypothesis=advice,
            evidence=evidence,
            dimension="exit_reason",
            dim_label="Exit reason",
            bucket=reason,
            n=m["n"],
            expectancy=m["expectancy"],
            overall_exp=oe,
            effect=effect,
            score=_score(effect, m["n"]),
            small_sample=(m["n"] < WEAK_SAMPLE),
        ))

    # Rule-specific exit findings (more targeted)
    for (rule, reason), pnls in by_rule_exit.items():
        if reason == "N/A" or len(pnls) < min_sample:
            continue
        rule_overall_exp = _metrics(by_rule.get(rule, [])).get("expectancy", oe)
        m      = _metrics(pnls)
        effect = _effect(m["expectancy"], rule_overall_exp, m["std_err"])
        if abs(effect) < _MIN_EFFECT:
            continue

        polarity = "bad" if effect < 0 else "good"
        advice = _EXIT_ADVICE.get(reason, {}).get(polarity, "")
        pct_rule = m["n"] / max(len(by_rule.get(rule, [])), 1) * 100

        evidence = (
            f"  For {rule}: '{reason}' accounts for {pct_rule:.0f}% of its exits "
            f"({m['n']} trades)\n"
            f"  Avg PnL:     {m['avg_pnl']:+.4f}  vs  {rule_overall_exp:+.4f} "
            f"for {rule} overall\n"
            f"  Win rate:    {m['win_rate']*100:.1f}%  |  "
            f"Effect size: {effect:+.2f}σ\n"
            f"  Sample:      {_sample_tag(m['n'], min_sample)}"
        )

        findings.append(Finding(
            ftype="exit_experiment",
            title=f"Exit experiment: '{reason}' for {rule}",
            hypothesis=advice,
            evidence=evidence,
            dimension="exit_reason_rule",
            dim_label=f"Exit reason ({rule})",
            bucket=f"{rule} / {reason}",
            n=m["n"],
            expectancy=m["expectancy"],
            overall_exp=rule_overall_exp,
            effect=effect,
            score=_score(effect, m["n"]) * 1.1,   # slight boost for targeted findings
            small_sample=(m["n"] < WEAK_SAMPLE),
        ))

    return findings


def _top(findings: list[Finding], ftype: str, n: int) -> list[Finding]:
    subset = [f for f in findings if f.ftype == ftype]
    subset.sort(key=lambda f: f.score, reverse=True)
    return subset[:n]


# ── Console output ─────────────────────────────────────────────────────────────

_FTYPE_LABELS = {
    "strategy_idea":   "Promising Strategy Ideas",
    "watchlist":       "Watchlist",
    "no_trade_filter": "No-Trade Filters",
    "exit_experiment": "Exit Experiments",
}


def _print_finding(rank: int, f: Finding) -> None:
    print(f"\n  {rank}. {f.title}")
    print(f"     Score: {f.score:.2f}  |  Effect: {f.effect:+.2f}σ")
    print(f"\n     Evidence:")
    for line in f.evidence.splitlines():
        print(f"     {line}")
    print(f"\n     Hypothesis / Action:")
    # word-wrap at ~72 chars
    words = f.hypothesis.split()
    line, lines = [], []
    for w in words:
        if sum(len(x) + 1 for x in line) + len(w) > 70:
            lines.append(" ".join(line))
            line = [w]
        else:
            line.append(w)
    if line:
        lines.append(" ".join(line))
    for ln in lines:
        print(f"     {ln}")


def print_section(title: str, findings: list[Finding]) -> None:
    W = 70
    print(f"\n{'─' * W}")
    print(f"  {title}")
    print(f"{'─' * W}")
    if not findings:
        print("  No findings met the minimum sample and effect-size thresholds.")
        return
    for i, f in enumerate(findings, 1):
        _print_finding(i, f)


# ── Markdown report ────────────────────────────────────────────────────────────

def _md_finding(rank: int, f: Finding) -> list[str]:
    sign = "▲" if f.effect > 0 else "▼"
    sample_note = (
        f"> ⚠ **Small sample ({f.n} trades)** — treat as directional only. "
        f"Validate with more data."
    ) if f.small_sample else (
        f"> Sample: {f.n} trades"
    )

    lines = [
        f"### {rank}. {f.title}",
        "",
        f"**Score:** {f.score:.2f} &nbsp;|&nbsp; "
        f"**Effect size:** {f.effect:+.2f}σ &nbsp;|&nbsp; "
        f"**Direction:** {sign}",
        "",
        "**Evidence from data:**",
        "",
        "```",
    ]
    for line in f.evidence.splitlines():
        lines.append(line)
    lines += [
        "```",
        "",
        "**Hypothesis / Action:**",
        "",
        f"{f.hypothesis}",
        "",
        sample_note,
        "",
    ]
    return lines


def write_report(
    all_findings:  list[Finding],
    overall:       dict,
    total_n:       int,
    min_sample:    int,
) -> Path:
    today   = date.today()
    out_dir = Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"strategy_ideas_{today.strftime('%Y_%m_%d')}.md"

    ideas   = _top(all_findings, "strategy_idea",   5)
    watch   = _top(all_findings, "watchlist",        5)
    filters = _top(all_findings, "no_trade_filter",  5)
    exits   = _top(all_findings, "exit_experiment",  5)

    lines: list[str] = [
        f"# Strategy Idea Generator",
        f"",
        f"Generated: {today.isoformat()} &nbsp;|&nbsp; "
        f"{total_n} closed trades analysed &nbsp;|&nbsp; "
        f"min sample: {min_sample}",
        "",
        "> ⚠ **Paper trading research only.** "
        "Do not act on these findings with real capital.",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Category | Findings surfaced |",
        "|:---|---:|",
        f"| Promising strategy ideas | {len(ideas)} |",
        f"| Watchlist buckets | {len(watch)} |",
        f"| No-trade filters | {len(filters)} |",
        f"| Exit experiments | {len(exits)} |",
        "",
        "**Overall sample stats:**",
        "",
        "| Metric | Value |",
        "|:---|---:|",
        f"| Closed trades | {total_n} |",
        f"| Win rate | {overall['win_rate']*100:.1f}% |",
        f"| Avg PnL | {overall['avg_pnl']:+.4f} |",
        f"| Median PnL | {overall['med_pnl']:+.4f} |",
        f"| Profit factor | {_pf_str(overall['pf'])} |",
        "",
        "---",
        "",
        "## Top 5 Promising Strategy Ideas",
        "",
        "Buckets where historical expectancy is positive, profit factor exceeds 1, "
        "and performance significantly beats baseline.",
        "",
    ]

    if ideas:
        for i, f in enumerate(ideas, 1):
            lines.extend(_md_finding(i, f))
    else:
        lines.append("*No promising ideas met the minimum thresholds.*\n")

    lines += [
        "---",
        "",
        "## Top 5 Watchlist Buckets",
        "",
        "Buckets that beat the current baseline but are not yet profitable. "
        "Treat these as hypotheses to monitor, not filters to promote.",
        "",
    ]

    if watch:
        for i, f in enumerate(watch, 1):
            lines.extend(_md_finding(i, f))
    else:
        lines.append("*No watchlist buckets met the minimum thresholds.*\n")

    lines += [
        "---",
        "",
        "## Top 5 No-Trade Filters",
        "",
        "Buckets where historical expectancy significantly *lags* baseline. "
        "Each filter suggests conditions to avoid at entry.",
        "",
    ]

    if filters:
        for i, f in enumerate(filters, 1):
            lines.extend(_md_finding(i, f))
    else:
        lines.append("*No filters met the minimum thresholds.*\n")

    lines += [
        "---",
        "",
        "## Top 5 Exit Experiments",
        "",
        "Outcome-conditioned exit diagnostics with the largest deviation from "
        "baseline. Treat these as prompts for controlled exit tests.",
        "",
    ]

    if exits:
        for i, f in enumerate(exits, 1):
            lines.extend(_md_finding(i, f))
    else:
        lines.append("*No exit experiments met the minimum thresholds.*\n")

    lines += [
        "---",
        "",
        "## All Findings",
        "",
        "Complete list, ranked by score within each category.",
        "",
        "| # | Type | Title | Dim | Bucket | N | Exp | Effect |",
        "|---:|:---|:---|:---|:---|---:|---:|---:|",
    ]

    ranked = sorted(all_findings, key=lambda f: (f.ftype, -f.score))
    for i, f in enumerate(ranked, 1):
        warn = " ⚠" if f.small_sample else ""
        lines.append(
            f"| {i} | {f.ftype} | {f.title} | {f.dim_label} "
            f"| {f.bucket} | {f.n}{warn} | {f.expectancy:+.4f} | {f.effect:+.2f}σ |"
        )

    out_path.write_text("\n".join(lines) + "\n")
    return out_path


# ── Entry point ────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate strategy ideas from paper trade data.")
    p.add_argument(
        "--min-sample", type=int, default=MIN_SAMPLE_DEFAULT, metavar="N",
        help=f"Minimum trades per bucket to surface a finding (default: {MIN_SAMPLE_DEFAULT})",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    rows = load_trades()
    if not rows:
        print("No closed trades found. Run the logger first.")
        return

    total_n = len(rows)
    pnls    = [float(r["pnl"]) for r in rows]
    overall = _metrics(pnls)

    if total_n < 30:
        print(f"\n  ⚠ Only {total_n} trades — findings will be highly unreliable.")
        print("  Collect at least 100 trades before treating any idea as actionable.\n")

    # ── Generate all findings ──────────────────────────────────────────────────
    entry_findings = generate_entry_findings(rows, overall, total_n, args.min_sample)
    exit_findings  = generate_exit_findings(rows, overall, args.min_sample)
    all_findings   = entry_findings + exit_findings

    ideas   = _top(all_findings, "strategy_idea",   5)
    watch   = _top(all_findings, "watchlist",        5)
    filters = _top(all_findings, "no_trade_filter",  5)
    exits   = _top(all_findings, "exit_experiment",  5)

    # ── Console output ─────────────────────────────────────────────────────────
    W = 70
    print(f"\n{'═' * W}")
    print(f"  Strategy Idea Generator   ({total_n} closed trades)")
    print(f"  Win rate: {overall['win_rate']*100:.1f}%  |  "
          f"Avg PnL: {overall['avg_pnl']:+.4f}  |  "
          f"PF: {_pf_str(overall['pf'])}")
    print(f"  {len(all_findings)} findings generated  |  "
          f"min sample threshold: {args.min_sample}")
    print(f"{'═' * W}")

    print_section("Top 5 Promising Strategy Ideas", ideas)
    print_section("Top 5 Watchlist Buckets", watch)
    print_section("Top 5 No-Trade Filters", filters)
    print_section("Top 5 Exit Experiments", exits)

    # ── Markdown report ────────────────────────────────────────────────────────
    path = write_report(all_findings, overall, total_n, args.min_sample)
    print(f"\n{'─' * W}")
    print(f"  Report written → {path}\n")


if __name__ == "__main__":
    main()
