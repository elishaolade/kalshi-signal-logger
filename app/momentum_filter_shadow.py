"""
app/momentum_filter_shadow.py — PURE shadow-evaluation logic for candidate
pre-entry and first-30-second exit filters.

This module contains NO database and NO network access.  It is the unit-tested
core behind ``scripts/momentum_filter_diagnostics.py`` and the optional
``MOMENTUM_FILTER_SHADOW_EVAL`` logging in the live trader.

Research question it answers
----------------------------
For the frozen ht120s_tp3c momentum profile, can a candidate filter materially
reduce ``stop_loss`` trades WITHOUT eliminating most ``profit_target`` winners?

It NEVER changes live behaviour — it only classifies already-recorded trades as
"this candidate WOULD have blocked/exited" and scores that against the actual
outcome.

Units
-----
Everything in this module is in TRUE cents (3.0 = 3 cents) and seconds.  Raw
``momentum_live_trades`` rows mix true cents and legacy price-unit fractions, so
callers should pass rows through :func:`normalize_trade` first (the report does
this).  :func:`normalize_trade` centralises every unit conversion.

Confusion-matrix convention (per candidate)
-------------------------------------------
Defined over trades whose ACTUAL outcome is ``profit_target`` or ``stop_loss``
(``fixed_time`` and other outcomes are tracked separately):

    true_positive  (TP): filter acted on a trade that became stop_loss    (good)
    false_positive (FP): filter acted on a trade that became profit_target (bad)
    true_negative  (TN): filter allowed a trade that became profit_target  (good)
    false_negative (FN): filter allowed a trade that became stop_loss      (bad)

    precision (stop-loss avoidance) = TP / (TP + FP)
    recall    (stop-loss avoidance) = TP / (TP + FN)   == stop_loss_reduction
    profit_target_retention         = TN / (TN + FP)   == pt retention
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

# ── Outcome labels (mirror backtest.signals exit reasons) ─────────────────────
OUTCOME_PROFIT_TARGET = "profit_target"
OUTCOME_STOP_LOSS = "stop_loss"
OUTCOME_FIXED_TIME = "fixed_time"

PRE_ENTRY = "pre_entry_filter"
EARLY_EXIT = "early_exit_filter"


# ══════════════════════════════════════════════════════════════════════════════
# Small numeric helpers
# ══════════════════════════════════════════════════════════════════════════════

def _f(value) -> Optional[float]:
    """Coerce to float or None (never raises)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def entry_ask_gap_cents(
    ws_entry_ask_at_signal: Optional[float],
    rest_ideal_entry_ask: Optional[float],
) -> Optional[float]:
    """
    Slippage-confirmation gap in TRUE cents:

        (ws_entry_ask_at_signal - rest_ideal_entry_ask) * 100

    Positive => the live/WS ask at signal was WORSE (higher) than the
    REST/logger ideal entry.  None when either input is missing.
    """
    ws = _f(ws_entry_ask_at_signal)
    rest = _f(rest_ideal_entry_ask)
    if ws is None or rest is None:
        return None
    return round((ws - rest) * 100.0, 4)


def _price_units_to_cents(value: Optional[float]) -> Optional[float]:
    v = _f(value)
    return None if v is None else round(v * 100.0, 4)


# ══════════════════════════════════════════════════════════════════════════════
# Normalisation: raw momentum_live_trades row -> normalized true-cents dict
# ══════════════════════════════════════════════════════════════════════════════

def trade_net_cents(row: Mapping) -> Optional[float]:
    """
    Realized net P/L per contract in TRUE cents.

    Prefers ``actual_profit_cents`` (legacy dollar-fraction column that INCLUDES
    fees; converted *100) so the economic result is used.  Falls back to
    ``actual_pnl_cents`` (already true cents, no fees) for rows where only that
    was populated (e.g. shadow-only simulated trades).
    """
    apc = _f(row.get("actual_profit_cents"))
    if apc is not None:
        return round(apc * 100.0, 4)
    return _f(row.get("actual_pnl_cents"))


def normalize_trade(row: Mapping) -> dict:
    """
    Convert a raw ``momentum_live_trades`` row into the true-cents / seconds
    shape the candidate logic expects.  Every unit conversion lives here.

    Missing values stay None so a candidate can report "undecided" rather than
    guess.  ``ws_spread_at_signal`` falls back to ``ws_spread_at_entry`` for
    rows written before the filter-diagnostics migration.
    """
    spread_units = row.get("ws_spread_at_signal")
    if spread_units is None:
        spread_units = row.get("ws_spread_at_entry")

    gap = _f(row.get("entry_ask_gap_cents"))
    if gap is None:
        gap = entry_ask_gap_cents(
            row.get("ws_entry_ask_at_signal"),
            row.get("rest_ideal_entry_ask") if row.get("rest_ideal_entry_ask") is not None
            else row.get("projected_entry_ask"),
        )

    quote_age_ms = _f(row.get("ws_quote_age_ms_at_signal"))
    if quote_age_ms is None:
        # Legacy rows only stored quote age in seconds at entry.
        secs = _f(row.get("ws_quote_age_at_entry"))
        quote_age_ms = None if secs is None else round(secs * 1000.0, 2)

    tte = _f(row.get("time_to_expiry_seconds_at_signal"))

    return {
        "id": row.get("id"),
        "side": row.get("side"),
        "outcome": row.get("exit_reason"),
        "shadow_only": row.get("shadow_only"),
        "diagnostic_mode": row.get("diagnostic_mode"),
        "net_cents": trade_net_cents(row),
        # ── Pre-entry (signal-time) inputs, true cents / ms / seconds ─────────
        "entry_ask": _f(row.get("projected_entry_ask")),
        "entry_ask_gap_cents": gap,
        "ws_spread_cents": _price_units_to_cents(spread_units),
        "ws_quote_age_ms": quote_age_ms,
        "tte_seconds": tte,
        # ── First-30s inputs (already true cents / seconds) ───────────────────
        "pnl_at_5s_cents": _f(row.get("pnl_at_5s_cents")),
        "pnl_at_10s_cents": _f(row.get("pnl_at_10s_cents")),
        "pnl_at_15s_cents": _f(row.get("pnl_at_15s_cents")),
        "pnl_at_20s_cents": _f(row.get("pnl_at_20s_cents")),
        "pnl_at_30s_cents": _f(row.get("pnl_at_30s_cents")),
        "pnl_at_45s_cents": _f(row.get("pnl_at_45s_cents")),  # not captured yet
        "max_profit_first_30s_cents": _f(row.get("max_profit_first_30s_cents")),
        "min_profit_first_30s_cents": _f(row.get("min_profit_first_30s_cents")),
        "time_to_first_green_seconds": _f(row.get("time_to_first_green_seconds")),
        "time_to_negative_1c_seconds": _f(row.get("time_to_negative_1c_seconds")),
        "time_to_negative_2c_seconds": _f(row.get("time_to_negative_2c_seconds")),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Candidate model
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Decision:
    """
    A candidate's verdict on ONE (normalized) trade.

    ``acts`` semantics:
      * pre-entry:  True  => this filter WOULD BLOCK the entry.
      * early-exit: True  => this filter WOULD EXIT early.
      * False       => filter allowed the trade unchanged.
      * None        => undecided (required telemetry missing) — excluded.
    """
    acts: Optional[bool]
    trigger_time_seconds: Optional[float] = None
    simulated_exit_pnl_cents: Optional[float] = None


@dataclass(frozen=True)
class Candidate:
    name: str
    candidate_type: str          # PRE_ENTRY | EARLY_EXIT
    threshold_label: str
    decide: Callable[[Mapping], Decision]


# ── Pre-entry candidate factories ─────────────────────────────────────────────

def _cand_entry_ask_gap(threshold_cents: float) -> Candidate:
    def decide(t: Mapping) -> Decision:
        gap = _f(t.get("entry_ask_gap_cents"))
        if gap is None:
            return Decision(None)
        return Decision(gap > threshold_cents)
    return Candidate(
        name="entry_ask_gap",
        candidate_type=PRE_ENTRY,
        threshold_label=f">{threshold_cents:g}c",
        decide=decide,
    )


def _cand_spread(threshold_cents: float) -> Candidate:
    def decide(t: Mapping) -> Decision:
        spread = _f(t.get("ws_spread_cents"))
        if spread is None:
            return Decision(None)
        return Decision(spread > threshold_cents)
    return Candidate(
        name="spread",
        candidate_type=PRE_ENTRY,
        threshold_label=f">{threshold_cents:g}c",
        decide=decide,
    )


def _cand_quote_age(threshold_ms: float) -> Candidate:
    def decide(t: Mapping) -> Decision:
        age = _f(t.get("ws_quote_age_ms"))
        if age is None:
            return Decision(None)
        return Decision(age > threshold_ms)
    return Candidate(
        name="quote_age",
        candidate_type=PRE_ENTRY,
        threshold_label=f">{threshold_ms:g}ms",
        decide=decide,
    )


def _cand_no_high_ask(threshold_price: float = 0.50) -> Candidate:
    """Candidate D — the existing live block: NO entries with ask >= 0.50."""
    def decide(t: Mapping) -> Decision:
        side = str(t.get("side") or "").upper()
        # The block only ever applies to NO; other sides pass untouched.
        if side != "NO":
            return Decision(False)
        ask = _f(t.get("entry_ask"))
        if ask is None:
            return Decision(None)          # NO trade but ask unknown — undecided
        return Decision(ask >= threshold_price)
    return Candidate(
        name="no_high_ask",
        candidate_type=PRE_ENTRY,
        threshold_label=f"NO ask>={threshold_price:g}",
        decide=decide,
    )


def _cand_tte_bucket(min_s: float = 450.0, max_s: float = 600.0) -> Candidate:
    """Candidate E — the existing live block: 450s <= TTE < 600s."""
    def decide(t: Mapping) -> Decision:
        tte = _f(t.get("tte_seconds"))
        if tte is None:
            return Decision(None)
        return Decision(min_s <= tte < max_s)
    return Candidate(
        name="tte_bucket",
        candidate_type=PRE_ENTRY,
        threshold_label=f"[{min_s:g},{max_s:g})s",
        decide=decide,
    )


def default_pre_entry_candidates() -> list[Candidate]:
    """All pre-entry candidates + thresholds from the research spec (A–E)."""
    candidates: list[Candidate] = []
    for thr in (0.5, 1.0, 2.0):                 # A. entry-ask slippage gap
        candidates.append(_cand_entry_ask_gap(thr))
    for thr in (1.0, 2.0, 3.0, 4.0):            # B. spread
        candidates.append(_cand_spread(thr))
    for thr in (250.0, 500.0, 1000.0, 2000.0):  # C. quote freshness
        candidates.append(_cand_quote_age(thr))
    candidates.append(_cand_no_high_ask(0.50))  # D. NO >= 0.50 (existing block)
    candidates.append(_cand_tte_bucket(450.0, 600.0))  # E. TTE (existing block)
    return candidates


# ── Early-exit candidate factories ────────────────────────────────────────────

def _never_green_by(t: Mapping, mark_s: float) -> Optional[bool]:
    """
    True if the trade never went positive within ``mark_s`` seconds.

    Uses ``time_to_first_green_seconds``: None means it never went green (so it
    is not green by the mark); a value > mark means it went green only later.
    Returns None (undecided) only when we cannot know green-status by the mark —
    i.e. the pnl sample at the mark is missing AND first-green is missing.
    """
    ttg = _f(t.get("time_to_first_green_seconds"))
    if ttg is not None:
        return ttg > mark_s
    # No recorded green. Trust that only if we actually observed the path to the
    # mark (the pnl sample exists); otherwise undecided.
    pnl_key = f"pnl_at_{int(mark_s)}s_cents"
    if _f(t.get(pnl_key)) is None:
        return None
    return True


def _cand_not_green_by(mark_s: int) -> Candidate:
    def decide(t: Mapping) -> Decision:
        never = _never_green_by(t, float(mark_s))
        if never is None:
            return Decision(None)
        sim = _f(t.get(f"pnl_at_{mark_s}s_cents"))
        return Decision(bool(never), trigger_time_seconds=float(mark_s),
                        simulated_exit_pnl_cents=sim if never else None)
    return Candidate(
        name=f"not_green_by_{mark_s}s",
        candidate_type=EARLY_EXIT,
        threshold_label=f"{mark_s}s",
        decide=decide,
    )


def _cand_red_after(mark_s: int, threshold_cents: float) -> Candidate:
    def decide(t: Mapping) -> Decision:
        pnl = _f(t.get(f"pnl_at_{mark_s}s_cents"))
        if pnl is None:
            return Decision(None)
        acts = pnl <= threshold_cents
        return Decision(acts, trigger_time_seconds=float(mark_s),
                        simulated_exit_pnl_cents=pnl if acts else None)
    return Candidate(
        name=(f"red_after_{mark_s}s" if threshold_cents == -1.0
              else f"down_{abs(int(threshold_cents))}c_by_{mark_s}s"),
        candidate_type=EARLY_EXIT,
        threshold_label=f"{mark_s}s<= {threshold_cents:g}c",
        decide=decide,
    )


def _cand_no_progress_by(mark_s: int, min_cents: float = 1.0) -> Candidate:
    """
    no_progress_by_45s — exit at ``mark_s`` if P/L < +``min_cents``.

    45s telemetry is not captured yet (only first-30s), so this returns
    undecided (None) until a ``pnl_at_45s_cents`` field exists.  The report
    surfaces it in the missing-telemetry audit rather than guessing.
    """
    def decide(t: Mapping) -> Decision:
        pnl = _f(t.get(f"pnl_at_{mark_s}s_cents"))
        if pnl is None:
            return Decision(None)
        acts = pnl < min_cents
        return Decision(acts, trigger_time_seconds=float(mark_s),
                        simulated_exit_pnl_cents=pnl if acts else None)
    return Candidate(
        name=f"no_progress_by_{mark_s}s",
        candidate_type=EARLY_EXIT,
        threshold_label=f"{mark_s}s< +{min_cents:g}c",
        decide=decide,
    )


def default_early_exit_candidates() -> list[Candidate]:
    """The six candidate early exits from the research spec."""
    return [
        _cand_not_green_by(20),                  # 1. not_green_by_20s
        _cand_not_green_by(30),                  # 2. not_green_by_30s
        _cand_red_after(15, -1.0),               # 3. red_after_15s
        _cand_red_after(30, -1.0),               # 4. red_after_30s
        _cand_red_after(30, -2.0),               # 5. down_2c_by_30s
        _cand_no_progress_by(45, 1.0),           # 6. no_progress_by_45s (missing)
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Aggregation / scoring
# ══════════════════════════════════════════════════════════════════════════════

def _profit_factor(nets: Sequence[float]) -> Optional[float]:
    gross_win = sum(n for n in nets if n > 0)
    gross_loss = abs(sum(n for n in nets if n < 0))
    if gross_loss <= 0:
        return None
    return round(gross_win / gross_loss, 4)


def _win_rate(nets: Sequence[float]) -> Optional[float]:
    if not nets:
        return None
    return round(sum(1 for n in nets if n > 0) / len(nets), 4)


def _mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None


def _pct(numer: int, denom: int) -> Optional[float]:
    if denom <= 0:
        return None
    return round(100.0 * numer / denom, 2)


def confusion_matrix(
    trades: Sequence[Mapping], candidate: Candidate
) -> dict:
    """
    Confusion-matrix counts + derived rates for one candidate.

    Only trades whose actual outcome is profit_target or stop_loss enter the
    matrix.  Undecided decisions (missing telemetry) are counted in
    ``undecided`` and excluded from every rate.
    """
    tp = fp = tn = fn = 0
    undecided = 0
    for t in trades:
        outcome = t.get("outcome")
        if outcome not in (OUTCOME_PROFIT_TARGET, OUTCOME_STOP_LOSS):
            continue
        d = candidate.decide(t)
        if d.acts is None:
            undecided += 1
            continue
        if d.acts:
            if outcome == OUTCOME_STOP_LOSS:
                tp += 1
            else:
                fp += 1
        else:
            if outcome == OUTCOME_PROFIT_TARGET:
                tn += 1
            else:
                fn += 1
    return {
        "candidate": candidate.name,
        "candidate_type": candidate.candidate_type,
        "threshold_label": candidate.threshold_label,
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "undecided": undecided,
        "precision_stop_loss_avoidance": _pct(tp, tp + fp),
        "recall_stop_loss_avoidance": _pct(tp, tp + fn),
        "stop_loss_reduction_pct": _pct(tp, tp + fn),
        "profit_target_retention_pct": _pct(tn, tn + fp),
    }


def stop_loss_reduction_pct(tp: int, fn: int) -> Optional[float]:
    """Share of actual stop_loss trades the filter acted on (recall)."""
    return _pct(tp, tp + fn)


def profit_target_retention_pct(tn: int, fp: int) -> Optional[float]:
    """Share of actual profit_target trades the filter left alone."""
    return _pct(tn, tn + fp)


def summarize_pre_entry(trades: Sequence[Mapping], candidate: Candidate) -> dict:
    """
    Full pre-entry shadow impact for one candidate/threshold.

    ``trades`` are normalized dicts (see :func:`normalize_trade`).  A trade is
    "blocked" when ``decide().acts`` is True, "allowed" when False, and excluded
    when None (missing telemetry).
    """
    allowed: list[Mapping] = []
    blocked: list[Mapping] = []
    undecided = 0
    for t in trades:
        d = candidate.decide(t)
        if d.acts is None:
            undecided += 1
        elif d.acts:
            blocked.append(t)
        else:
            allowed.append(t)

    def _counts(rows: Sequence[Mapping]) -> dict:
        return {
            "n": len(rows),
            "profit_target": sum(1 for r in rows if r.get("outcome") == OUTCOME_PROFIT_TARGET),
            "stop_loss": sum(1 for r in rows if r.get("outcome") == OUTCOME_STOP_LOSS),
            "fixed_time": sum(1 for r in rows if r.get("outcome") == OUTCOME_FIXED_TIME),
            "net_cents": _sum_net(rows),
        }

    allowed_nets = [n for n in (_f(r.get("net_cents")) for r in allowed) if n is not None]
    cm = confusion_matrix(trades, candidate)
    return {
        "candidate": candidate.name,
        "candidate_type": candidate.candidate_type,
        "threshold_label": candidate.threshold_label,
        "allowed": _counts(allowed),
        "blocked": _counts(blocked),
        "undecided": undecided,
        "allowed_win_rate": _win_rate(allowed_nets),
        "allowed_profit_factor": _profit_factor(allowed_nets),
        "allowed_avg_net_cents": _mean(allowed_nets),
        "stop_loss_reduction_pct": cm["stop_loss_reduction_pct"],
        "profit_target_retention_pct": cm["profit_target_retention_pct"],
        "confusion": cm,
    }


def summarize_early_exit(trades: Sequence[Mapping], candidate: Candidate) -> dict:
    """
    Full first-30-second early-exit shadow impact for one candidate.

    For each triggered trade we compare the simulated early-exit P/L against the
    actual final net (both true cents) to measure net improvement.
    """
    triggered: list[Mapping] = []
    undecided = 0
    sim_pnls: list[float] = []
    act_pnls: list[float] = []
    improvements: list[float] = []
    stop_avoided = pt_cut = ft_affected = 0

    for t in trades:
        d = candidate.decide(t)
        if d.acts is None:
            undecided += 1
            continue
        if not d.acts:
            continue
        triggered.append(t)
        outcome = t.get("outcome")
        if outcome == OUTCOME_STOP_LOSS:
            stop_avoided += 1
        elif outcome == OUTCOME_PROFIT_TARGET:
            pt_cut += 1
        elif outcome == OUTCOME_FIXED_TIME:
            ft_affected += 1
        sim = _f(d.simulated_exit_pnl_cents)
        act = _f(t.get("net_cents"))
        if sim is not None:
            sim_pnls.append(sim)
        if act is not None:
            act_pnls.append(act)
        if sim is not None and act is not None:
            improvements.append(round(sim - act, 4))

    cm = confusion_matrix(trades, candidate)
    total_pt = cm["true_negative"] + cm["false_positive"]
    return {
        "candidate": candidate.name,
        "candidate_type": candidate.candidate_type,
        "threshold_label": candidate.threshold_label,
        "triggered": len(triggered),
        "undecided": undecided,
        "stop_loss_avoided": stop_avoided,
        "profit_target_cut": pt_cut,
        "fixed_time_affected": ft_affected,
        "avg_simulated_pnl_cents": _mean(sim_pnls),
        "avg_actual_pnl_cents": _mean(act_pnls),
        "net_improvement_cents": round(sum(improvements), 4) if improvements else None,
        "stop_loss_reduction_pct": cm["stop_loss_reduction_pct"],
        "profit_target_damage_pct": _pct(pt_cut, total_pt),
        "profit_target_retention_pct": cm["profit_target_retention_pct"],
        "confusion": cm,
    }


def _sum_net(rows: Sequence[Mapping]) -> Optional[float]:
    nets = [n for n in (_f(r.get("net_cents")) for r in rows) if n is not None]
    return round(sum(nets), 4) if nets else None


# ══════════════════════════════════════════════════════════════════════════════
# Baseline + promising-filter decision
# ══════════════════════════════════════════════════════════════════════════════

def baseline_performance(trades: Sequence[Mapping]) -> dict:
    """Section-1 baseline over normalized trades (true cents)."""
    n = len(trades)
    pt = sum(1 for t in trades if t.get("outcome") == OUTCOME_PROFIT_TARGET)
    sl = sum(1 for t in trades if t.get("outcome") == OUTCOME_STOP_LOSS)
    ft = sum(1 for t in trades if t.get("outcome") == OUTCOME_FIXED_TIME)
    nets = [x for x in (_f(t.get("net_cents")) for t in trades) if x is not None]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    return {
        "total_trades": n,
        "profit_target": pt,
        "stop_loss": sl,
        "fixed_time": ft,
        "profit_target_rate": _pct(pt, n),
        "stop_loss_rate": _pct(sl, n),
        "fixed_time_rate": _pct(ft, n),
        "win_rate": _win_rate(nets),
        "avg_win_cents": _mean(wins),
        "avg_loss_cents": _mean(losses),
        "profit_factor": _profit_factor(nets),
        "total_net_cents": round(sum(nets), 4) if nets else None,
        "avg_net_cents": _mean(nets),
    }


# Suggested promotion criteria (defaults from the research spec).
DEFAULT_MIN_STOP_LOSS_REDUCTION_PCT = 25.0
DEFAULT_MIN_PROFIT_TARGET_RETENTION_PCT = 75.0
DEFAULT_MIN_SAMPLE = 20


def is_promising(
    summary: Mapping,
    *,
    min_stop_loss_reduction_pct: float = DEFAULT_MIN_STOP_LOSS_REDUCTION_PCT,
    min_profit_target_retention_pct: float = DEFAULT_MIN_PROFIT_TARGET_RETENTION_PCT,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> bool:
    """
    A candidate is promising only when it materially cuts stop losses while
    retaining most winners on a non-tiny sample.
    """
    cm = summary.get("confusion", summary)
    sample = (
        cm.get("true_positive", 0) + cm.get("false_positive", 0)
        + cm.get("true_negative", 0) + cm.get("false_negative", 0)
    )
    slr = summary.get("stop_loss_reduction_pct")
    ptr = summary.get("profit_target_retention_pct")
    if slr is None or ptr is None:
        return False
    return (
        sample >= min_sample
        and slr >= min_stop_loss_reduction_pct
        and ptr >= min_profit_target_retention_pct
    )
