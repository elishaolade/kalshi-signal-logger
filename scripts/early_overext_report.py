#!/usr/bin/env python3
"""
early_overext_report.py — Research report for early_overextension_reversal_scalp/v1.

This strategy is **watch-only**.  No paper trade and no live order is ever
opened for it.  The ObservationTracker shadow-follows the *losing-side*
contract for ~60s after each signal and records hypothetical outcomes into the
`signal_observations` table.  This report summarises those observations so the
hypothesis ("an over-extended losing side bounces enough to scalp") can be
judged before it is ever promoted to paper trading.

What it shows, per slice
-------------------------
  • Outcome hit-rates     +3c-before-2c / +4c-before-2c / +5c-before-3c
  • Excursions            avg MFE, avg MAE (mid − entry_ref, in dollars)
  • Exit-sim expectancies sim_tp3_sl2, sim_tp5_sl3, sim_timeout60 (avg pnl)
  • Timing                avg time-to-peak, time-to-+3c, time-to-stop (s)

Slices
------
  side · losing-price bucket · winning-jump bucket · directional-z bucket ·
  market-age bucket · hour block · day of week · volatility regime

Only COMPLETE observations are scored.  Rows still ACTIVE, or finalised as
'recovered_after_restart' / 'market_rollover', are excluded from outcome
scoring (they had no fair 60s window).

⚠  WATCH-ONLY RESEARCH — NO PAPER TRADES, NO LIVE TRADING.

Usage:
    python scripts/early_overext_report.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all

# ── Constants ──────────────────────────────────────────────────────────────────

_RULE = "early_overextension_reversal_scalp"
_VER  = "v1"
_W    = 104                # console line width

# Only these completion reasons had a fair observation window.
_SCORED_REASONS = ("timeout_60s", "near_expiry")

_SAMPLE_STEPS = [
    (250, "STRONGER EVIDENCE    (250+ obs)"),
    (100, "FIRST SERIOUS REVIEW (100–249 obs)"),
    ( 50, "EARLY CLUE           (50–99 obs)"),
    ( 25, "SMOKE TEST ONLY      (25–49 obs)"),
    (  0, "INSUFFICIENT         (<25 obs)"),
]

_DAY_ORDER = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday", "N/A",
]
_PRICE_ORDER = ["<0.05", "0.05-0.10", "0.10-0.15", "0.15-0.20", "0.20-0.25", "0.25+", "N/A"]
_JUMP_ORDER  = ["<0.15", "0.15-0.20", "0.20-0.30", "0.30+", "N/A"]
_Z_ORDER     = ["<2.5", "2.5-3.0", "3.0-4.0", "4.0+", "N/A"]
_AGE_ORDER   = ["<30s", "30-60s", "60-90s", "90-120s", "120s+", "N/A"]
_VOL_ORDER   = ["low", "medium", "high", "N/A"]


# ── SQL ────────────────────────────────────────────────────────────────────────

_QUERY = """
    SELECT
        so.side,
        so.complete_reason,
        CAST(so.entry_ref_price            AS DOUBLE) AS entry_ref_price,
        CAST(so.losing_ask_at_signal       AS DOUBLE) AS losing_ask_at_signal,
        CAST(so.winning_change_60s         AS DOUBLE) AS winning_change_60s,
        CAST(so.winning_dir_z              AS DOUBLE) AS winning_dir_z,
        CAST(so.market_age_seconds         AS DOUBLE) AS market_age_seconds,
        CAST(so.max_favorable_excursion    AS DOUBLE) AS mfe,
        CAST(so.max_adverse_excursion      AS DOUBLE) AS mae,
        so.hit_plus_3c_before_minus_2c     AS hit32,
        so.hit_plus_4c_before_minus_2c     AS hit42,
        so.hit_plus_5c_before_minus_3c     AS hit53,
        CAST(so.time_to_peak_s             AS DOUBLE) AS t_peak,
        CAST(so.time_to_plus_3c_s          AS DOUBLE) AS t_3c,
        CAST(so.time_to_stop_s             AS DOUBLE) AS t_stop,
        so.did_make_new_low_after_signal   AS new_low,
        so.sim_tp3_sl2_outcome             AS s32_out,
        CAST(so.sim_tp3_sl2_pnl            AS DOUBLE) AS s32_pnl,
        so.sim_tp5_sl3_outcome             AS s53_out,
        CAST(so.sim_tp5_sl3_pnl            AS DOUBLE) AS s53_pnl,
        CAST(so.sim_timeout60_pnl          AS DOUBLE) AS s60_pnl,
        s.entry_hour_block                 AS entry_hour_block,
        s.entry_day_name                   AS entry_day_name,
        CAST(s.volatility_60s              AS DOUBLE) AS volatility_60s
    FROM signal_observations so
    LEFT JOIN signals s ON so.signal_id = s.id
    WHERE so.rule_name    = %s
      AND so.rule_version = %s
      AND so.status       = 'COMPLETE'
      AND so.complete_reason IN ('timeout_60s', 'near_expiry')
    ORDER BY so.recorded_at ASC
"""


def _load() -> list[dict]:
    return fetch_all(_QUERY, (_RULE, _VER))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fv(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    return None if v is None else float(v)


def _rate(rows: list[dict], key: str) -> Optional[float]:
    vals = [bool(r.get(key)) for r in rows if r.get(key) is not None]
    return (sum(vals) / len(vals)) if vals else None


def _avg(rows: list[dict], key: str) -> Optional[float]:
    vals = [_fv(r, key) for r in rows]
    vals = [v for v in vals if v is not None]
    return mean(vals) if vals else None


def compute_metrics(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    return {
        "n":          n,
        "hit32":      _rate(rows, "hit32"),
        "hit42":      _rate(rows, "hit42"),
        "hit53":      _rate(rows, "hit53"),
        "mfe":        _avg(rows, "mfe"),
        "mae":        _avg(rows, "mae"),
        "new_low":    _rate(rows, "new_low"),
        "s32_pnl":    _avg(rows, "s32_pnl"),
        "s53_pnl":    _avg(rows, "s53_pnl"),
        "s60_pnl":    _avg(rows, "s60_pnl"),
        "t_peak":     _avg(rows, "t_peak"),
        "t_3c":       _avg(rows, "t_3c"),
        "t_stop":     _avg(rows, "t_stop"),
    }


def maturity(n: int) -> str:
    for threshold, label in _SAMPLE_STEPS:
        if n >= threshold:
            return label
    return _SAMPLE_STEPS[-1][1]


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _dol(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:+.4f}"


def _sec(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.1f}s"


# ── Bucket helpers ─────────────────────────────────────────────────────────────

def _b_side(r: dict) -> str:
    return str(r.get("side") or "N/A")


def _b_price(r: dict) -> str:
    p = _fv(r, "entry_ref_price")
    if p is None:  return "N/A"
    if p < 0.05:   return "<0.05"
    if p < 0.10:   return "0.05-0.10"
    if p < 0.15:   return "0.10-0.15"
    if p < 0.20:   return "0.15-0.20"
    if p < 0.25:   return "0.20-0.25"
    return                "0.25+"


def _b_jump(r: dict) -> str:
    j = _fv(r, "winning_change_60s")
    if j is None:  return "N/A"
    if j < 0.15:   return "<0.15"
    if j < 0.20:   return "0.15-0.20"
    if j < 0.30:   return "0.20-0.30"
    return                "0.30+"


def _b_dir_z(r: dict) -> str:
    z = _fv(r, "winning_dir_z")
    if z is None:  return "N/A"
    if z < 2.5:    return "<2.5"
    if z < 3.0:    return "2.5-3.0"
    if z < 4.0:    return "3.0-4.0"
    return                "4.0+"


def _b_age(r: dict) -> str:
    a = _fv(r, "market_age_seconds")
    if a is None:  return "N/A"
    if a < 30:     return "<30s"
    if a < 60:     return "30-60s"
    if a < 90:     return "60-90s"
    if a < 120:    return "90-120s"
    return                "120s+"


def _b_hour(r: dict) -> str:
    return str(r.get("entry_hour_block") or "N/A")


def _b_day(r: dict) -> str:
    return str(r.get("entry_day_name") or "N/A")


def _b_vol(r: dict) -> str:
    v = _fv(r, "volatility_60s")
    if v is None:   return "N/A"
    if v < 15.0:    return "low"
    if v < 40.0:    return "medium"
    return                 "high"


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

_BD_W = 16
_BD_COLS = [
    ("N",        4),
    ("+3/-2",    7),
    ("+4/-2",    7),
    ("+5/-3",    7),
    ("AvgMFE",   8),
    ("AvgMAE",   8),
    ("tp3/sl2",  8),
    ("tp5/sl3",  8),
    ("hold60",   8),
    ("t→peak",   7),
    ("t→+3c",    7),
]


def _bd_cells(m: dict) -> list[str]:
    return [
        str(m["n"]),
        _pct(m["hit32"]),
        _pct(m["hit42"]),
        _pct(m["hit53"]),
        _dol(m["mfe"]),
        _dol(m["mae"]),
        _dol(m["s32_pnl"]),
        _dol(m["s53_pnl"]),
        _dol(m["s60_pnl"]),
        _sec(m["t_peak"]),
        _sec(m["t_3c"]),
    ]


def _print_bd_table(
    title:  str,
    groups: list[tuple[str, dict]],
    note:   str = "",
) -> list[str]:
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
        if not m.get("n"):
            continue
        cells = _bd_cells(m)
        print(
            f"  {bucket:<{_BD_W}}"
            + "  ".join(f"{v:>{w}}" for v, (_, w) in zip(cells, _BD_COLS))
        )
        md.append("| " + " | ".join([bucket] + cells) + " |")
        any_data = True

    if not any_data:
        print("   (no data)")
        md.append("_(no data)_")

    print()
    md.append("")
    return md


def _print_overall(m: dict) -> list[str]:
    if not m.get("n"):
        print("  (no scored observations)\n")
        return ["_(no scored observations)_", ""]

    rows = [
        ("Scored observations",   str(m["n"])),
        ("+3c before -2c",        _pct(m["hit32"])),
        ("+4c before -2c",        _pct(m["hit42"])),
        ("+5c before -3c",        _pct(m["hit53"])),
        ("Made new low after sig",_pct(m["new_low"])),
        ("Avg MFE",               _dol(m["mfe"])),
        ("Avg MAE",               _dol(m["mae"])),
        ("Sim tp3/sl2 avg pnl",   _dol(m["s32_pnl"])),
        ("Sim tp5/sl3 avg pnl",   _dol(m["s53_pnl"])),
        ("Sim hold-60 avg pnl",   _dol(m["s60_pnl"])),
        ("Avg time → peak",       _sec(m["t_peak"])),
        ("Avg time → +3c",        _sec(m["t_3c"])),
        ("Avg time → stop",       _sec(m["t_stop"])),
    ]

    rule = "─" * max(0, _W - len("Overall") - 4)
    print(f"\n── Overall {rule}")
    half = (len(rows) + 1) // 2
    for i in range(half):
        left  = rows[i]
        right = rows[i + half] if i + half < len(rows) else ("", "")
        print(f"  {left[0]:<28} {left[1]:<12}  {right[0]:<28} {right[1]}")
    print()

    md: list[str] = ["### Overall", "", "| Metric | Value |", "|:---|---:|"]
    for label, val in rows:
        md.append(f"| {label} | {val} |")
    md.append("")
    return md


# ── Markdown file writer ───────────────────────────────────────────────────────

def write_md(lines: list[str]) -> Path:
    today   = date.today()
    out_dir = Path(__file__).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"early_overext_report_{today.strftime('%Y_%m_%d')}.md"

    header = [
        f"# early_overextension_reversal_scalp/v1 — Research Report  {today.isoformat()}",
        "",
        "> ⚠ **WATCH-ONLY RESEARCH** — no paper trades and no live trading.",
        "> Outcomes are shadow-observations of the losing-side contract mid,",
        "> measured against the losing-side mid at signal time (no fill/slippage).",
        "",
        "---",
        "",
    ]
    out_path.write_text("\n".join(header + lines) + "\n")
    return out_path


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    rows = _load()

    print(f"\n{'═' * _W}")
    print("  early_overextension_reversal_scalp/v1 — Research Report")
    print(f"{'═' * _W}")
    print(
        "  ⚠  WATCH-ONLY RESEARCH — no paper trades, no live trading.\n"
        "     Shadow-observation of the losing side vs its mid at signal time.\n"
        f"  Generated: {date.today().isoformat()}\n"
    )

    overall = compute_metrics(rows)
    n = overall.get("n", 0)
    print(f"  Sample: {n} scored observations   ›  {maturity(n)}")
    print("  (only timeout_60s / near_expiry completions are scored)")

    md: list[str] = [
        f"**Sample:** {n} scored observations — {maturity(n)}",
        "",
        "_Only `timeout_60s` / `near_expiry` completions are scored; "
        "`recovered_after_restart` and `market_rollover` are excluded._",
        "",
    ]

    if n == 0:
        print("\n  No scored observations yet.\n")
        md += ["_No scored observations yet._", ""]
        path = write_md(md)
        print(f"Report written → {path}\n")
        return

    md += _print_overall(overall)

    md += _print_bd_table("Tracked (losing) Side", breakdown(rows, _b_side))
    md += _print_bd_table(
        "Losing-side Price at Signal (entry_ref)",
        breakdown(rows, _b_price, _PRICE_ORDER),
    )
    md += _print_bd_table(
        "Winning-side 60s Jump",
        breakdown(rows, _b_jump, _JUMP_ORDER),
        note="winning_change_60s — how far the winning side ran in 60s.",
    )
    md += _print_bd_table(
        "Winning-side Directional Z",
        breakdown(rows, _b_dir_z, _Z_ORDER),
        note="winning_dir_z — BTC displacement toward the winning side, in σ.",
    )
    md += _print_bd_table(
        "Market Age at Signal",
        breakdown(rows, _b_age, _AGE_ORDER),
    )
    md += _print_bd_table(
        "Hour Block (America/New_York)",
        breakdown(rows, _b_hour),
        note="N/A = signal joined before time-feature population.",
    )
    md += _print_bd_table(
        "Day of Week",
        breakdown(rows, _b_day, _DAY_ORDER),
    )
    md += _print_bd_table(
        "Volatility Regime (BTC σ over 60s)",
        breakdown(rows, _b_vol, _VOL_ORDER),
        note="low <15 · medium 15–40 · high ≥40 (USD).",
    )

    print(f"{'═' * _W}")
    print("  ⚠  WATCH-ONLY RESEARCH — no paper trades, no live trading recommendation.")
    print(f"{'═' * _W}\n")

    path = write_md(md)
    print(f"Report written → {path}\n")


if __name__ == "__main__":
    main()
