"""
app/momentum_live_trader.py — Real-money execution layer for the frozen
ht120s_tp3c momentum candidate.

THIS PLACES REAL ORDERS when (and only when) every MOMENTUM_LIVE_* gate is set.
It is a SEPARATE component from the shadow tracker:
  - It does NOT modify or replace app/momentum_shadow_tracker.py.
  - Shadow tracking and shadow reporting continue to run unchanged.
  - It reuses the EXACT frozen signal + exit logic so live entries line up
    tick-for-tick with shadow entries (paired projected-vs-actual).

Signal alignment
----------------
Entry detection and exit rules are imported directly from the shadow tracker /
backtest modules (same ExperimentConfig, same detect_signal, same window math,
same 120s hold / +3c target / 10s grace).  No strategy parameter is redefined
here. Optional live-only exit experiments remain disabled unless their explicit
MOMENTUM_LIVE_* flags are enabled.

Safety model (fail closed)
--------------------------
No order is sent unless ALL of these hold (checked in is_live_armed()):
    MOMENTUM_LIVE_ENABLED == true
    MOMENTUM_LIVE_CONFIRM == "I_UNDERSTAND_REAL_MONEY"
    Kalshi RSA auth configured (KALSHI_KEY_ID + KALSHI_KEY_FILE)
    bankroll, kelly fraction, and per-trade dollar cap all > 0
Plus, before EACH entry, per-trade risk gates (kill switch, pause latch, max
active, spread, quote freshness, daily loss, sizing) must all pass.

If the component is constructed but not fully armed, it runs in INERT mode:
on_tick() returns immediately and no order API is ever called.  Projected
outcomes continue to be recorded by the (separate) shadow tracker.
"""
from __future__ import annotations

import logging
import os
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app import config
from app.db import execute_query, fetch_all, fetch_one, insert_and_get_id
from app.features import Tick
from app.kalshi_trading import (
    KalshiTradingClient,
    KalshiTradingError,
    cents_to_dollars,
    is_authenticated,
)
from app.kalshi_ws import KalshiMarketStream, WSOrderState
from backtest.loader import MarketInfo, MarketRow, get_bid
from backtest.signals import detect_signal

# Reuse the frozen profile constants + helpers from the shadow tracker so the
# live trader cannot drift from shadow behaviour.
from app.momentum_shadow_tracker import (
    _SHADOW_CONFIG,
    _PROFILE,
    _HOLD_S,
    _TP,
    _GRACE_S,
    _TRIM_WINDOW_S,
    _entry_filter_reason,
    _build_market_row,
    _classify_pnl,
    EXIT_PROFIT_TARGET,
    EXIT_STOP_LOSS,
    EXIT_FIXED_TIME,
    EXIT_UNEXECUTABLE,
)

logger = logging.getLogger(__name__)

# Round-trip cost used for the PROJECTED (shadow) pnl so it matches the shadow
# tracker exactly.  Actual pnl uses real fill prices + real fees instead.
_PROJ_FEE      = _SHADOW_CONFIG.estimated_fee_per_contract
_PROJ_SLIPPAGE = _SHADOW_CONFIG.estimated_slippage_cents


# ══════════════════════════════════════════════════════════════════════════════
# Pure helpers (no DB / no network) — unit tested in tests/test_momentum_live.py
# ══════════════════════════════════════════════════════════════════════════════

def compute_full_kelly_fraction(
    win_rate: Optional[float],
    profit_loss_ratio: Optional[float],
) -> float:
    """
    Full-Kelly stake fraction for a win/loss bet.

        f* = p - (1 - p) / b

    where p = win_rate (0..1), b = profit_loss_ratio (avg win / |avg loss|).
    Returns 0.0 when inputs are missing/degenerate or the edge is non-positive.
    Result is clamped to [0, 1].
    """
    if win_rate is None or profit_loss_ratio is None:
        return 0.0
    if profit_loss_ratio <= 0:
        return 0.0
    p = max(0.0, min(1.0, win_rate))
    f = p - (1.0 - p) / profit_loss_ratio
    return max(0.0, min(1.0, f))


def compute_position_size(
    *,
    bankroll_dollars: float,
    kelly_fraction: float,
    full_kelly_fraction: float,
    max_dollars_per_trade: float,
    max_contracts_per_trade: int,
    price_per_contract: float,
) -> tuple[int, float]:
    """
    Conservative position sizing.  Returns (contracts, dollars_budgeted).

    Steps:
      1. Kelly dollars = bankroll * full_kelly * kelly_fraction
      2. Budget = min(Kelly dollars, max_dollars_per_trade)
      3. contracts = floor(budget / price_per_contract)   (round DOWN)
      4. cap by max_contracts_per_trade when > 0
    contracts is 0 when any input is non-positive or the budget can't afford one
    contract — the caller must treat 0 as "do not trade".
    """
    if (
        bankroll_dollars <= 0
        or kelly_fraction <= 0
        or full_kelly_fraction <= 0
        or max_dollars_per_trade <= 0
        or price_per_contract <= 0
    ):
        return 0, 0.0

    kelly_dollars = bankroll_dollars * full_kelly_fraction * kelly_fraction
    budget = min(kelly_dollars, max_dollars_per_trade)
    if budget <= 0:
        return 0, 0.0

    contracts = int(budget // price_per_contract)   # floor — never round up
    if max_contracts_per_trade and max_contracts_per_trade > 0:
        contracts = min(contracts, max_contracts_per_trade)
    if contracts < 1:
        return 0, 0.0

    dollars_budgeted = round(contracts * price_per_contract, 4)
    return contracts, dollars_budgeted


def compute_fixed_position_size(
    *,
    fixed_contracts: int,
    max_dollars_per_trade: float,
    max_contracts_per_trade: int,
    price_per_contract: float,
) -> tuple[int, float]:
    """
    Fixed-contract sizing with the same hard caps as Kelly sizing.

    Returns (0, 0.0) when the fixed count is invalid or one capped fixed lot
    would exceed the per-trade dollar cap.
    """
    if fixed_contracts <= 0 or max_dollars_per_trade <= 0 or price_per_contract <= 0:
        return 0, 0.0
    contracts = fixed_contracts
    if max_contracts_per_trade and max_contracts_per_trade > 0:
        contracts = min(contracts, max_contracts_per_trade)
    if contracts < 1:
        return 0, 0.0
    dollars_budgeted = round(contracts * price_per_contract, 4)
    if dollars_budgeted > max_dollars_per_trade:
        return 0, 0.0
    return contracts, dollars_budgeted


def compute_live_entry_limit_price(
    projected_entry_ask: float,
    price_offset_cents: float,
) -> float:
    """
    Live entry limit derived from the shadow entry ask plus a small configurable
    nudge toward marketability.

    ``price_offset_cents`` uses the repo's dollar-fraction convention
    (0.01 = 1 cent). The projected shadow entry is left unchanged; this helper
    only controls the actual live order price.
    """
    adjusted = round(projected_entry_ask + (price_offset_cents or 0.0), 4)
    return max(0.01, min(0.99, adjusted))


def price_to_cents(price: Optional[float]) -> Optional[float]:
    """
    Convert a contract price from repo-standard dollar-fraction units
    (0.074 == 7.4c) into explicit cents.
    """
    if price is None:
        return None
    return round(float(price) * 100.0, 4)


def price_delta_to_cents(
    current_exit_bid: Optional[float],
    actual_entry_price: Optional[float],
) -> Optional[float]:
    """
    Convert a price delta into profit cents.

    Example:
      entry 0.074, bid 0.097 -> (0.097 - 0.074) * 100 = 2.3c
    """
    if current_exit_bid is None or actual_entry_price is None:
        return None
    return round((float(current_exit_bid) - float(actual_entry_price)) * 100.0, 4)


def determine_ws_shadow_exit_reasons(
    *,
    current_profit_cents: float,
    max_profit_cents: float,
    age_seconds: float,
) -> list[str]:
    """
    Return the hypothetical WS-aware exit triggers that are satisfied now.

    These thresholds operate in literal cents, not repo price units:
      +3.0 means +3 cents of per-contract profit.
    """
    reasons: list[str] = []
    if config.MOMENTUM_LIVE_WS_SHADOW_TP2_ENABLED and current_profit_cents >= 2.0:
        reasons.append("ws_shadow_tp2")
    if config.MOMENTUM_LIVE_WS_SHADOW_TP3_ENABLED and current_profit_cents >= 3.0:
        reasons.append("ws_shadow_tp3")
    if config.MOMENTUM_LIVE_WS_SHADOW_TP5_ENABLED and current_profit_cents >= 5.0:
        reasons.append("ws_shadow_tp5")
    if (
        config.MOMENTUM_LIVE_WS_SHADOW_PROFIT_PROTECTION_ENABLED
        and max_profit_cents >= config.MOMENTUM_LIVE_WS_SHADOW_PROFIT_PROTECTION_TRIGGER_CENTS
        and current_profit_cents <= config.MOMENTUM_LIVE_WS_SHADOW_PROFIT_PROTECTION_FLOOR_CENTS
    ):
        reasons.append("ws_shadow_profit_protection")
    if (
        config.MOMENTUM_LIVE_WS_SHADOW_GIVEBACK_ENABLED
        and max_profit_cents >= config.MOMENTUM_LIVE_WS_SHADOW_GIVEBACK_TRIGGER_CENTS
        and current_profit_cents <= config.MOMENTUM_LIVE_WS_SHADOW_GIVEBACK_FLOOR_CENTS
    ):
        reasons.append("ws_shadow_giveback")
    if (
        config.MOMENTUM_LIVE_WS_SHADOW_TIME_PROGRESS_ENABLED
        and age_seconds >= config.MOMENTUM_LIVE_WS_SHADOW_TIME_PROGRESS_SECONDS
        and current_profit_cents <= config.MOMENTUM_LIVE_WS_SHADOW_TIME_PROGRESS_MIN_PROFIT_CENTS
    ):
        reasons.append("ws_shadow_time_progress")
    return reasons


def summarize_pnls(pnls: list[float]) -> dict[str, Optional[float]]:
    """
    Win-rate / expectancy / profit-factor / profit-loss-ratio for a list of
    per-contract net pnls (dollar fractions).  Used for both projected and
    actual aggregates so they are computed identically.
    """
    n = len(pnls)
    if n == 0:
        return {
            "n": 0, "win_rate": None, "expectancy": None,
            "profit_factor": None, "profit_loss_ratio": None,
        }
    wins   = [p for p in pnls if p > 1e-9]
    losses = [p for p in pnls if p < -1e-9]
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win  = statistics.mean(wins)   if wins   else None
    avg_loss = abs(statistics.mean(losses)) if losses else None
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "expectancy": statistics.mean(pnls),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "profit_loss_ratio": (avg_win / avg_loss) if (avg_win and avg_loss) else None,
    }


def compute_actual_pnl(
    *,
    actual_entry_price: Optional[float],
    actual_exit_price: Optional[float],
    entry_fees_total: Optional[float],
    exit_fees_total: Optional[float],
    filled_contracts: int,
) -> dict[str, Optional[float]]:
    """
    Per-contract realized live pnl, charging BOTH entry and exit fees.

    Kalshi fill fees from ``_summarize_fills`` are TOTALS across all contracts in
    that order, so they are normalized to per-contract here to stay comparable
    with the per-contract projected pnl.  Unknown fees (None) are treated as 0
    for the pnl but reported as None in ``fees_per_contract`` (never fabricated;
    if EITHER leg reports a fee, the known total is used).

    Returns a dict: fees_per_contract, actual_profit_cents,
    actual_profit_dollars, actual_trade_won.  All None when the trade did not
    fill on both legs.
    """
    if (
        actual_entry_price is None
        or actual_exit_price is None
        or filled_contracts < 1
    ):
        return {
            "fees_per_contract": None,
            "actual_profit_cents": None,
            "actual_profit_dollars": None,
            "actual_trade_won": None,
        }
    known = [f for f in (entry_fees_total, exit_fees_total) if f is not None]
    total_fees = sum(known) if known else None
    fee_for_pnl = (total_fees or 0.0) / filled_contracts
    fees_per_contract = (
        round(total_fees / filled_contracts, 6) if total_fees is not None else None
    )
    profit = round(actual_exit_price - actual_entry_price - fee_for_pnl, 6)
    return {
        "fees_per_contract": fees_per_contract,
        "actual_profit_cents": profit,
        "actual_profit_dollars": round(profit * filled_contracts, 6),
        "actual_trade_won": 1 if profit > 1e-9 else 0,
    }


# ── True-cents slippage / decay helpers ──────────────────────────────────────
# UNIT NOTE: The existing *_drift_cents columns store price-unit fractions
# (0.05 = 5 cents).  These helpers return TRUE cents (5.0 = 5 cents) and feed
# the new *_slippage_cents / *_pnl_cents columns added by
# scripts/migrate_add_momentum_live_telemetry.py.

def compute_entry_slippage_cents(
    ideal_entry_ask: Optional[float],
    actual_entry_price: Optional[float],
) -> Optional[float]:
    """
    Entry slippage in true cents.
    Positive => live paid more than the shadow/logger entry.
    """
    if ideal_entry_ask is None or actual_entry_price is None:
        return None
    return round((float(actual_entry_price) - float(ideal_entry_ask)) * 100.0, 4)


def compute_exit_slippage_cents(
    ideal_exit_bid: Optional[float],
    actual_exit_price: Optional[float],
) -> Optional[float]:
    """
    Exit slippage in true cents.
    Positive => live received less than the shadow/logger exit.
    """
    if ideal_exit_bid is None or actual_exit_price is None:
        return None
    return round((float(ideal_exit_bid) - float(actual_exit_price)) * 100.0, 4)


def compute_projected_path_pnl_cents(
    ideal_entry_ask: Optional[float],
    ideal_exit_bid: Optional[float],
) -> Optional[float]:
    """
    Shadow path gain in true cents, NO fees.
    """
    if ideal_entry_ask is None or ideal_exit_bid is None:
        return None
    return round((float(ideal_exit_bid) - float(ideal_entry_ask)) * 100.0, 4)


def compute_actual_path_pnl_cents(
    actual_entry_price: Optional[float],
    actual_exit_price: Optional[float],
) -> Optional[float]:
    """
    Live path gain in true cents, NO fees.
    Distinct from actual_profit_cents (which includes Kalshi fees).
    """
    if actual_entry_price is None or actual_exit_price is None:
        return None
    return round((float(actual_exit_price) - float(actual_entry_price)) * 100.0, 4)


def compute_live_decay_cents(
    projected_path_pnl_cents: Optional[float],
    actual_path_pnl_cents: Optional[float],
) -> Optional[float]:
    """
    Live underperformance vs shadow path in true cents.
    Positive => live underperformed the logger/shadow path.
    """
    if projected_path_pnl_cents is None or actual_path_pnl_cents is None:
        return None
    return round(float(projected_path_pnl_cents) - float(actual_path_pnl_cents), 4)


# ── Bucket classification helpers ─────────────────────────────────────────────

def bucket_entry_price(price: Optional[float]) -> str:
    """Classify contract entry price into broad range buckets."""
    if price is None:
        return "unknown"
    p = float(price)
    if p < 0.10:
        return "0.00-0.10"
    if p < 0.25:
        return "0.10-0.25"
    if p < 0.50:
        return "0.25-0.50"
    if p < 0.70:
        return "0.50-0.70"
    if p < 0.85:
        return "0.70-0.85"
    return "0.85-1.00"


def bucket_spread(spread_price_units: Optional[float]) -> str:
    """
    Classify spread (in repo price-unit fractions, 0.01 = 1 cent) into buckets.
    Spread of 0.01 = 1c, 0.02 = 2c, 0.04 = 4c.
    """
    if spread_price_units is None:
        return "unknown"
    c = float(spread_price_units) * 100.0
    if c < 1.0:
        return "0-1c"
    if c < 2.0:
        return "1-2c"
    if c < 4.0:
        return "2-4c"
    return "4c+"


def bucket_quote_age(age_seconds: Optional[float]) -> str:
    """Classify quote age in seconds."""
    if age_seconds is None:
        return "unknown"
    s = float(age_seconds)
    if s < 1.0:
        return "0-1s"
    if s < 2.0:
        return "1-2s"
    if s < 5.0:
        return "2-5s"
    return "5s+"


def bucket_time_remaining(seconds: Optional[float]) -> str:
    """Classify time remaining in the hold window at exit signal."""
    if seconds is None:
        return "unknown"
    s = float(seconds)
    if s < 60.0:
        return "0-60s"
    if s < 180.0:
        return "60-180s"
    if s < 300.0:
        return "180-300s"
    return "300s+"


def bucket_filled_contracts(requested: int, filled: int) -> str:
    """Classify fill completeness."""
    if filled <= 0:
        return "zero-fill"
    if filled < requested:
        return "partial-fill"
    return "full-fill"


def build_pause_windows(
    rows: list[tuple[Optional[float], Optional[float]]],
    window_sizes,
) -> dict[int, dict]:
    """
    Build rolling projected-vs-actual windows from PAIRED completed trades.

    ``rows`` is (projected_profit, actual_profit) ordered most-recent-first.
    Only rows where BOTH values are present are kept, so projected and actual
    samples in every window are strictly paired — same trades, equal length —
    which makes the pause comparison apples-to-apples.
    """
    paired = [(p, a) for (p, a) in rows if p is not None and a is not None]
    out: dict[int, dict] = {}
    for w in window_sizes:
        chunk = paired[:w]
        proj = [p for p, _ in chunk]
        act = [a for _, a in chunk]
        out[w] = {
            "n": len(chunk),
            "projected": summarize_pnls(proj),
            "actual": summarize_pnls(act),
        }
    return out


def compute_drift_fields(
    *,
    projected_entry_ask: Optional[float],
    projected_exit_bid: Optional[float],
    projected_profit: Optional[float],
    projected_expectancy: Optional[float],
    actual_entry_price: Optional[float],
    actual_exit_price: Optional[float],
    actual_profit: Optional[float],
) -> dict[str, Optional[float]]:
    """
    Projected-vs-actual comparison fields (all dollar fractions / contract).

    Conventions (documented in the migration too):
      entry_price_drift     = actual_entry - projected_entry_ask  (+ = paid more)
      exit_price_drift      = actual_exit  - projected_exit_bid   (- = got less)
      profit_delta          = actual_profit - projected_profit
      total_execution_drift = projected_profit - actual_profit    (adverse cost)
      profit_capture_ratio  = actual_profit / projected_profit
      expectancy_delta      = actual_profit - projected_expectancy
      expectancy_capture    = actual_profit / projected_expectancy
    """
    def _sub(a, b):
        return round(a - b, 6) if (a is not None and b is not None) else None

    def _ratio(a, b):
        if a is None or b is None or abs(b) < 1e-9:
            return None
        return round(a / b, 6)

    entry_drift = _sub(actual_entry_price, projected_entry_ask)
    exit_drift  = _sub(actual_exit_price, projected_exit_bid)
    profit_delta = _sub(actual_profit, projected_profit)
    total_exec_drift = _sub(projected_profit, actual_profit)
    return {
        "entry_price_drift_cents":     entry_drift,
        "exit_price_drift_cents":      exit_drift,
        "profit_delta_cents":          profit_delta,
        "total_execution_drift_cents": total_exec_drift,
        "profit_capture_ratio":        _ratio(actual_profit, projected_profit),
        "expectancy_delta_cents":      _sub(actual_profit, projected_expectancy),
        "expectancy_capture_ratio":    _ratio(actual_profit, projected_expectancy),
    }


def evaluate_pause(
    windows: dict[int, dict],
    *,
    min_trades: int,
    win_rate_gap_pct: float,
    expectancy_gap: float,
    profit_factor_gap: float,
) -> Optional[dict]:
    """
    Decide whether automatic pause should trigger.

    ``windows`` maps window_size -> {
        "n": int,
        "projected": {win_rate, expectancy, profit_factor, ...},
        "actual":    {win_rate, expectancy, profit_factor, ...},
    }

    A window is only evaluated once it has at least ``min_trades`` completed live
    trades (do not pause on tiny samples).  Returns the first breaching window's
    detail dict, or None if nothing breaches.

    Gaps are projected MINUS actual (live trailing shadow):
      win-rate gap in percentage POINTS, expectancy in dollar fraction,
      profit-factor as a raw difference.
    """
    for size in sorted(windows):
        w = windows[size]
        if w.get("n", 0) < min_trades:
            continue
        proj = w["projected"]
        act = w["actual"]
        breaches: list[str] = []

        pw, aw = proj.get("win_rate"), act.get("win_rate")
        if pw is not None and aw is not None:
            gap = (pw - aw) * 100.0
            if gap > win_rate_gap_pct:
                breaches.append(
                    f"win_rate gap {gap:.1f}pp > {win_rate_gap_pct:.1f}pp"
                )

        pe, ae = proj.get("expectancy"), act.get("expectancy")
        if pe is not None and ae is not None:
            gap = pe - ae
            if gap > expectancy_gap:
                breaches.append(
                    f"expectancy gap {gap:.4f} > {expectancy_gap:.4f}"
                )

        pf, af = proj.get("profit_factor"), act.get("profit_factor")
        if pf is not None and af is not None:
            gap = pf - af
            if gap > profit_factor_gap:
                breaches.append(
                    f"profit_factor gap {gap:.3f} > {profit_factor_gap:.3f}"
                )

        if breaches:
            return {
                "window": size,
                "n": w["n"],
                "breaches": breaches,
                "projected": proj,
                "actual": act,
            }
    return None


def kill_switch_engaged() -> bool:
    """True when the env flag is set or the kill-switch file exists."""
    if config.MOMENTUM_LIVE_KILL_SWITCH:
        return True
    path = config.MOMENTUM_LIVE_KILL_SWITCH_FILE
    return bool(path) and os.path.exists(path)


def is_live_armed() -> tuple[bool, str]:
    """
    Static gate: is the live trader allowed to place real orders at all?
    Returns (armed, reason).  reason is empty when armed.
    """
    if not config.MOMENTUM_LIVE_ENABLED:
        return False, "MOMENTUM_LIVE_ENABLED is not true"
    if config.MOMENTUM_LIVE_CONFIRM != config.MOMENTUM_LIVE_CONFIRM_TOKEN:
        return False, "MOMENTUM_LIVE_CONFIRM token not set correctly"
    if not is_authenticated():
        return False, "Kalshi RSA auth not configured (KALSHI_KEY_ID/KALSHI_KEY_FILE)"
    if config.MOMENTUM_LIVE_BANKROLL_DOLLARS <= 0:
        return False, "MOMENTUM_LIVE_BANKROLL_DOLLARS must be > 0"
    if config.MOMENTUM_LIVE_SIZE_MODE not in ("kelly", "fixed"):
        return False, "MOMENTUM_LIVE_SIZE_MODE must be 'kelly' or 'fixed'"
    if config.MOMENTUM_LIVE_SIZE_MODE == "kelly" and config.MOMENTUM_LIVE_KELLY_FRACTION <= 0:
        return False, "MOMENTUM_LIVE_KELLY_FRACTION must be > 0"
    if config.MOMENTUM_LIVE_SIZE_MODE == "fixed" and config.MOMENTUM_LIVE_FIXED_CONTRACTS <= 0:
        return False, "MOMENTUM_LIVE_FIXED_CONTRACTS must be > 0 in fixed mode"
    if config.MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE <= 0:
        return False, "MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE must be > 0"
    return True, ""


def _summarize_fills(fills: list[dict], side: str) -> tuple[int, Optional[float], Optional[float]]:
    """
    Aggregate Kalshi fills into (total_count, avg_price_dollars, total_fees_dollars).

    Supports both older integer-cent payloads (``count``, ``yes_price``,
    ``no_price``, ``fee``/``fee_dollars``) and the current V2 fixed-point
    payloads (``count_fp``, ``yes_price_dollars``, ``no_price_dollars``,
    ``fee_cost``). Never fabricates — returns (0, None, None) when there are
    no fills.
    """
    price_key = "yes_price" if side == "YES" else "no_price"
    price_dollars_key = f"{price_key}_dollars"
    total_fp = 0.0
    weighted = 0.0
    fees = 0.0
    saw_fee = False
    for f in fills:
        cnt_raw = f.get("count_fp")
        if cnt_raw is None:
            cnt_raw = f.get("count")
        try:
            cnt = float(cnt_raw or 0.0)
        except (TypeError, ValueError):
            continue
        if cnt <= 0:
            continue

        px_raw = f.get(price_dollars_key)
        if px_raw is not None:
            try:
                px = round(float(px_raw), 4)
            except (TypeError, ValueError):
                px = None
        else:
            px = cents_to_dollars(f.get(price_key))
        if px is None:
            continue
        total_fp += cnt
        weighted += px * cnt
        fee = f.get("fee_cost")
        if fee is None:
            fee = f.get("fee") or f.get("fee_dollars")
        if fee is not None:
            try:
                fees += float(fee)
                saw_fee = True
            except (TypeError, ValueError):
                pass
    if total_fp <= 0:
        return 0, None, None
    avg = round(weighted / total_fp, 4)
    total = int(round(total_fp))
    return total, avg, (round(fees, 4) if saw_fee else None)


def aggregate_fills(
    fill_lists: list[list[dict]], side: str
) -> tuple[int, Optional[float], Optional[float]]:
    """
    Aggregate fills ACROSS multiple order ids into one (count, avg_price, fees).

    Used so a trade exited via several orders (partial fills, cancel/replace)
    is summarized as a single round-trip outcome rather than only the last
    order.  Flattens the per-order fill lists and defers to ``_summarize_fills``.
    """
    flat: list[dict] = []
    for fl in fill_lists:
        if fl:
            flat.extend(fl)
    return _summarize_fills(flat, side)


def order_remaining(requested: int, filled: int) -> int:
    """Non-negative quantity still to fill.  Never resubmit more than this."""
    return max(0, int(requested) - int(filled))


def is_order_open(order: Optional[dict]) -> bool:
    """
    Best-effort: is a Kalshi order still working (resting / partially filled)?

    Terminal when status is canceled/executed/expired/closed OR remaining_count
    is 0.  Unknown/missing state is treated as OPEN (conservative — we would
    rather poll again or cancel explicitly than assume an order is dead and
    leave residual quantity untracked).

    NOTE: exact Kalshi order field names (``status`` values, ``remaining_count``)
    must be confirmed against the live API.
    """
    if not order:
        return True
    status = str(order.get("status") or "").strip().lower()
    if status in ("canceled", "cancelled", "executed", "filled", "closed", "expired"):
        return False
    rem = order.get("remaining_count")
    if rem is None:
        rem = order.get("remaining")
    if rem is None:
        rem = order.get("remaining_count_fp")
    if rem is not None:
        try:
            return float(rem) > 0
        except (TypeError, ValueError):
            pass
    return True


def net_position_for_side(positions: list[dict], ticker: str, side: str) -> int:
    """
    Net long contracts held on ``side`` for ``ticker`` from a positions payload.

    Assumes Kalshi ``market_position.position`` is signed: positive = long YES,
    negative = long NO.  Returns the magnitude on the requested side, else 0.

    NOTE: this sign convention must be confirmed against the live API.
    """
    for p in positions:
        if str(p.get("ticker") or "") != ticker:
            continue
        pos = p.get("position")
        try:
            pos = int(pos)
        except (TypeError, ValueError):
            continue
        return max(0, pos) if side == "YES" else max(0, -pos)
    return 0


def combine_exit_fills(
    baseline_count: int,
    baseline_value: float,
    baseline_fees: Optional[float],
    session_count: int,
    session_avg: Optional[float],
    session_fees: Optional[float],
) -> dict:
    """
    Combine a restart BASELINE of prior exit fills (from closed orders before a
    crash) with the CURRENT session's exit aggregate into one cumulative result.

    ``baseline_value`` is sum(price*count) for prior fills; the session value is
    derived from ``session_avg * session_count``.  Unknown fees (None) are
    summed only over known parts.  For a fresh trade baseline is 0/0/None, so
    the result equals the session aggregate (no behaviour change).
    """
    count = int(baseline_count) + int(session_count)
    session_value = (
        float(session_avg) * int(session_count)
        if (session_avg is not None and session_count) else 0.0
    )
    value = float(baseline_value) + session_value
    fee_parts = [f for f in (baseline_fees, session_fees) if f is not None]
    fees = round(sum(fee_parts), 6) if fee_parts else None
    avg = round(value / count, 6) if count > 0 else None
    return {"count": count, "avg": avg, "fees": fees, "value": round(value, 6)}


def reconstruct_exit_state(
    *, db_entry_filled: int, db_exit_filled: int, position_qty: int
) -> dict:
    """
    Pure quantity reconstruction for a trade being rebuilt after a restart.

    Keeps the ORIGINAL entry size distinct from current remaining position:
      original_entry = max(recorded entry size, position + recorded exits)
                       (never shrinks the original; repairs upward if the DB
                        under-recorded the entry fill before the crash)
      exit_filled    = original_entry - position_qty   (already sold)
      remaining      = position_qty                    (still to flatten)

    This guarantees a partially-exited trade is NOT rebuilt as if ``position_qty``
    were the original trade size.
    """
    pos = max(0, int(position_qty))
    original_entry = max(int(db_entry_filled or 0), pos + int(db_exit_filled or 0))
    exit_filled = max(0, original_entry - pos)
    remaining = max(0, original_entry - exit_filled)
    return {
        "original_entry": original_entry,
        "exit_filled": exit_filled,
        "remaining": remaining,
    }


def should_replace_exit(order: Optional[dict], remaining: int) -> bool:
    """
    Safety gate for cancel/reprice: only submit a replacement sell when the
    prior order is CONFIRMED TERMINAL (no more fills can land on it) and there
    is still quantity left.  If the order is not confirmed terminal we must NOT
    replace (doing so off a stale fill snapshot can oversell).
    """
    return (not is_order_open(order)) and int(remaining) >= 1


def partition_trade_orders(
    orders: list[dict],
    known_order_ids: set[str],
    known_client_order_ids: set[str],
) -> tuple[list[dict], list[dict]]:
    """
    Partition ticker-scoped open orders into orders attributable to this trade
    and orders we cannot safely claim.

    Startup reconciliation must never cancel or adopt unrelated live orders on
    the same ticker.  An order is treated as ours only when its server order id
    or client_order_id matches a durable key we already recorded for the trade.
    Unknown orders force manual review instead of broad cancellation.
    """
    owned: list[dict] = []
    unknown: list[dict] = []
    for order in orders:
        oid = str(order.get("order_id") or order.get("id") or "")
        coid = str(order.get("client_order_id") or "")
        if (oid and oid in known_order_ids) or (coid and coid in known_client_order_ids):
            owned.append(order)
        else:
            unknown.append(order)
    return owned, unknown


def reconcile_decision(position_qty: int, has_open_order: bool) -> str:
    """
    Pure decision for startup reconciliation of a non-terminal live row.

      position_qty > 0           -> "rehydrate_flatten" (real exposure to manage)
      position_qty 0, open order -> "cancel_then_close" (cancel stray, no posn)
      position_qty 0, no order   -> "mark_reconciled"   (already flat)
    """
    if position_qty and position_qty > 0:
        return "rehydrate_flatten"
    if has_open_order:
        return "cancel_then_close"
    return "mark_reconciled"


def resolve_recovery_baseline(
    *,
    reconstructed_exit_count: int,
    persisted_count: int,
    persisted_value: Optional[float],
    persisted_fees: Optional[float],
    fills_count: int,
    fills_avg: Optional[float],
    fills_fees: Optional[float],
) -> dict:
    """
    Pick the restart baseline used for exit accounting.

    We preserve the reconstructed exit COUNT even when value/fee history cannot
    be verified, because exposure management depends on the count.  In that
    mismatch case we intentionally mark accounting unverified and decline to
    synthesize PnL from mismatched count/value histories.
    """
    reconstructed_exit_count = max(0, int(reconstructed_exit_count))
    persisted_count = max(0, int(persisted_count))
    fills_count = max(0, int(fills_count))

    if reconstructed_exit_count == 0:
        return {
            "count": 0,
            "value": 0.0,
            "fees": None,
            "verified": True,
            "source": "none",
        }

    if persisted_count == reconstructed_exit_count and persisted_value is not None:
        return {
            "count": reconstructed_exit_count,
            "value": round(float(persisted_value), 6),
            "fees": persisted_fees,
            "verified": True,
            "source": "persisted",
        }

    if fills_count == reconstructed_exit_count and fills_avg is not None:
        return {
            "count": reconstructed_exit_count,
            "value": round(float(fills_avg) * reconstructed_exit_count, 6),
            "fees": fills_fees,
            "verified": True,
            "source": "fills",
        }

    return {
        "count": reconstructed_exit_count,
        "value": 0.0,
        "fees": None,
        "verified": False,
        "source": "mismatch",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Active live trade state
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class _ActiveLive:
    live_trade_id: int
    market_id:     int
    contract_id:   int
    market_ticker: str
    side:          str
    signal_at:     datetime
    signal_ts:     float
    entry_ask:     float           # projected entry (observed ask at signal)
    horizon_ts:    float

    requested_contracts: int
    status:        str             # PENDING_ENTRY | ACTIVE | PENDING_EXIT

    # Order linkage
    entry_order_id:        Optional[str] = None
    entry_client_order_id: Optional[str] = None
    entry_submit_ts:       Optional[float] = None
    entry_cancel_requested: bool = False
    # Exit may span MULTIPLE orders (cancel/replace, partial fills); track all.
    exit_order_id:   Optional[str] = None          # most-recent exit order
    exit_order_ids:  list[str] = field(default_factory=list)
    exit_submit_ts:  Optional[float] = None

    # Fills
    filled_contracts:      int = 0                  # ORIGINAL entry size (never shrinks)
    exit_filled_contracts: int = 0                  # cumulative EXIT fills (baseline+session)
    actual_entry_price:    Optional[float] = None
    actual_entry_fees:     Optional[float] = None   # total entry-side fees

    # Cumulative exit aggregates (running): value = sum(exit_price*count).
    exit_value: float = 0.0
    exit_fees:  Optional[float] = None
    exit_accounting_verified: bool = True
    # Restart baseline = exit fills from orders NOT in exit_order_ids (prior,
    # already-closed exits recovered on restart). 0/0/None for a fresh trade.
    exit_baseline_count: int = 0
    exit_baseline_value: float = 0.0
    exit_baseline_fees:  Optional[float] = None

    # Projected exit tracking (identical to shadow _advance_active)
    peak_bid:    float = float("-inf")
    trough_bid:  float = float("inf")
    grace_start: Optional[float] = None
    projected_exit_bid: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_signal_ts: Optional[float] = None

    # Last bid observed on THIS market (used to price a flatten after rollover,
    # when the main loop no longer feeds this market's quotes).
    last_bid: Optional[float] = None
    last_ask: Optional[float] = None
    # True once this trade must be flattened off the main quote stream
    # (market rolled over, or rehydrated on restart): managed by _advance_flattening.
    flattening: bool = False

    # WS / execution instrumentation
    ws_enabled: bool = False
    ws_quote_age_at_entry: Optional[float] = None
    ws_spread_at_entry: Optional[float] = None
    ws_entry_best_bid: Optional[float] = None
    ws_entry_best_ask: Optional[float] = None
    ws_exit_best_bid: Optional[float] = None
    ws_exit_spread: Optional[float] = None
    ws_spread_sum: float = 0.0
    ws_spread_samples: int = 0
    ws_max_quote_age_during_trade: float = 0.0
    max_bid_after_entry: Optional[float] = None
    max_profit_cents: Optional[float] = None
    time_to_max_bid_seconds: Optional[float] = None
    seconds_above_1c: float = 0.0
    seconds_above_2c: float = 0.0
    seconds_above_3c: float = 0.0
    seconds_above_4c: float = 0.0
    seconds_above_5c: float = 0.0
    bid_at_30s: Optional[float] = None
    bid_at_60s: Optional[float] = None
    bid_at_90s: Optional[float] = None
    bid_at_120s: Optional[float] = None
    went_green: bool = False
    target_touched: bool = False
    target_touch_count: int = 0
    target_first_touched_at: Optional[datetime] = None
    target_total_visible_seconds: float = 0.0
    # Telemetry — order detail captured at submission time
    entry_order_price:     Optional[float] = None   # limit price POSTed for entry
    exit_order_price:      Optional[float] = None   # bid price POSTed for first exit
    quote_age_at_exit_seconds: Optional[float] = None

    entry_fill_detected_by: Optional[str] = None
    exit_fill_detected_by: Optional[str] = None
    entry_signal_to_order_ms: Optional[int] = None
    entry_order_to_ack_ms: Optional[int] = None
    entry_ack_to_fill_ms: Optional[int] = None
    fill_to_exit_order_ms: Optional[int] = None
    exit_signal_to_order_ms: Optional[int] = None
    entry_ack_ts: Optional[float] = None
    entry_fill_ts: Optional[float] = None
    last_metrics_ts: Optional[float] = None
    ws_shadow_triggered_reasons: set[str] = field(default_factory=set)


# ══════════════════════════════════════════════════════════════════════════════
# MomentumLiveTrader
# ══════════════════════════════════════════════════════════════════════════════

class MomentumLiveTrader:
    """
    Live execution for the frozen ht120s_tp3c profile.  Call on_tick() once per
    poll cycle, AFTER the shadow tracker, with the same arguments.
    """

    def __init__(self, ws_stream: Optional[KalshiMarketStream] = None) -> None:
        import collections
        self._window: collections.deque[MarketRow] = collections.deque()
        self._current_market_id: Optional[str] = None
        self._cooldown_until: dict[int, float] = {}
        self._active: dict[int, _ActiveLive] = {}
        self._ws = (
            ws_stream
            if ws_stream is not None
            else KalshiMarketStream() if config.MOMENTUM_LIVE_USE_WEBSOCKET else None
        )
        self._owns_ws = self._ws is not None and ws_stream is None
        self._ws_stale_guardrail_emitted = False

        # Recovery / safety latches.  While set, _try_open_live refuses new
        # entries until open exposure from a restart or rollover is resolved.
        self._flatten_only = False
        self._unresolved_recovery = False

        self._armed, reason = is_live_armed()
        self._client: Optional[KalshiTradingClient] = None
        if self._armed:
            try:
                self._client = KalshiTradingClient(require_auth=True)
            except KalshiTradingError as exc:
                self._armed = False
                reason = str(exc)

        self._reconcile_on_startup()
        if self._ws and self._owns_ws:
            self._ws.start()

        if self._armed:
            logger.warning(
                "MomentumLiveTrader ARMED — REAL ORDERS ENABLED | profile=%s "
                "size_mode=%s fixed_contracts=%d bankroll=$%.2f kelly=%.3f "
                "max$/trade=%.2f max_contracts=%d "
                "max_active=%d max_spread=%.3f ws=%s",
                _PROFILE,
                config.MOMENTUM_LIVE_SIZE_MODE,
                config.MOMENTUM_LIVE_FIXED_CONTRACTS,
                config.MOMENTUM_LIVE_BANKROLL_DOLLARS,
                config.MOMENTUM_LIVE_KELLY_FRACTION,
                config.MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE,
                config.MOMENTUM_LIVE_MAX_CONTRACTS_PER_TRADE,
                config.MOMENTUM_LIVE_MAX_ACTIVE_TRADES,
                config.MOMENTUM_LIVE_MAX_SPREAD,
                "on" if self._ws else "off",
            )
        else:
            logger.info(
                "MomentumLiveTrader INERT (no real orders) — %s", reason,
            )

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def active_count(self) -> int:
        return len(self._active)

    def _best_bid_for_side(self, market_ticker: str, side: str, row: MarketRow) -> Optional[float]:
        ws = getattr(self, "_ws", None)
        if ws:
            bid = ws.get_best_bid(market_ticker, side)
            age = ws.get_quote_age_seconds(market_ticker)
            if bid is not None and age is not None and age <= config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS:
                return bid
        return get_bid(row, side)

    def _best_ask_for_side(self, market_ticker: str, side: str) -> Optional[float]:
        ws = getattr(self, "_ws", None)
        if ws:
            return ws.get_best_ask(market_ticker, side)
        return None

    def _quote_age_seconds(self, market_ticker: str, captured_at: datetime) -> float:
        ws = getattr(self, "_ws", None)
        if ws:
            age = ws.get_quote_age_seconds(market_ticker)
            if age is not None:
                return age
        return (datetime.now(timezone.utc) - captured_at).total_seconds()

    def _spread_for_side(self, market_ticker: str, side: str, fallback: Optional[float]) -> Optional[float]:
        ws = getattr(self, "_ws", None)
        if ws:
            spread = ws.get_spread(market_ticker, side)
            if spread is not None:
                return spread
        return fallback

    def _ws_order_state(self, live: _ActiveLive) -> Optional[WSOrderState]:
        ws = getattr(self, "_ws", None)
        if not ws:
            return None
        return ws.get_order_state(
            order_id=live.entry_order_id or live.exit_order_id,
            client_order_id=live.entry_client_order_id or None,
        )

    def _subscribe_ws_interest(self, market_ticker: str) -> None:
        if not self._ws:
            return
        self._ws.subscribe_market(market_ticker)
        for live in self._active.values():
            self._ws.subscribe_market(live.market_ticker)

    def _target_cents(self) -> float:
        return config.MOMENTUM_LIVE_TP_CENTS or _TP

    def _experimental_exit_decision(
        self,
        live: _ActiveLive,
        row: MarketRow,
        bid: Optional[float],
    ) -> tuple[Optional[str], Optional[float]]:
        if bid is None:
            return None, None
        entry_price = live.actual_entry_price if live.actual_entry_price is not None else live.entry_ask
        current_profit = round(bid - entry_price, 6)

        if (
            config.MOMENTUM_LIVE_STOP_LOSS_ENABLED
            and current_profit <= -abs(config.MOMENTUM_LIVE_STOP_LOSS_CENTS)
        ):
            return EXIT_STOP_LOSS, bid

        if (
            config.MOMENTUM_LIVE_PROFIT_PROTECTION_ENABLED
            and live.max_profit_cents is not None
            and live.max_profit_cents >= config.MOMENTUM_LIVE_PROFIT_PROTECTION_TRIGGER_CENTS
            and current_profit <= config.MOMENTUM_LIVE_PROFIT_PROTECTION_FLOOR_CENTS
        ):
            return "profit_protection", bid

        if (
            config.MOMENTUM_LIVE_TIME_PROGRESS_EXIT_ENABLED
            and (row.ts - live.signal_ts) >= config.MOMENTUM_LIVE_TIME_PROGRESS_SECONDS
            and current_profit < config.MOMENTUM_LIVE_TIME_PROGRESS_MIN_PROFIT_CENTS
        ):
            return "time_progress", bid
        return None, None

    def _record_ws_shadow_exit(
        self,
        live: _ActiveLive,
        *,
        reason: str,
        captured_at: datetime,
        age_seconds: float,
        exit_price: float,
        current_profit_cents: float,
        max_profit_cents: float,
    ) -> None:
        if reason in live.ws_shadow_triggered_reasons:
            return

        existing = fetch_one(
            "SELECT id FROM momentum_live_shadow_exits "
            "WHERE live_trade_id=%s AND shadow_exit_reason=%s",
            (live.live_trade_id, reason),
        )
        if existing:
            live.ws_shadow_triggered_reasons.add(reason)
            return

        execute_query(
            """
            INSERT INTO momentum_live_shadow_exits (
                live_trade_id, ticker, side, shadow_exit_reason,
                triggered_at, age_seconds, exit_price, profit_cents,
                current_profit_cents, max_profit_cents
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                live.live_trade_id,
                live.market_ticker,
                live.side,
                reason,
                captured_at,
                round(age_seconds, 2),
                exit_price,
                current_profit_cents,
                current_profit_cents,
                max_profit_cents,
            ),
        )
        live.ws_shadow_triggered_reasons.add(reason)
        logger.info(
            "[WS_SHADOW_EXIT] trade_id=%s reason=%s price=%.3f profit_cents=%.2f age=%.2f",
            live.live_trade_id,
            reason,
            exit_price,
            current_profit_cents,
            age_seconds,
        )

    def _maybe_record_ws_shadow_exits(
        self,
        live: _ActiveLive,
        *,
        bid: float,
        captured_at: datetime,
        current_profit_cents: float,
        age_seconds: float,
    ) -> None:
        if not config.MOMENTUM_LIVE_WS_EXIT_SHADOW:
            return
        max_profit_cents = float(live.max_profit_cents or current_profit_cents)
        reasons = determine_ws_shadow_exit_reasons(
            current_profit_cents=current_profit_cents,
            max_profit_cents=max_profit_cents,
            age_seconds=age_seconds,
        )
        for reason in reasons:
            self._record_ws_shadow_exit(
                live,
                reason=reason,
                captured_at=captured_at,
                age_seconds=age_seconds,
                exit_price=bid,
                current_profit_cents=current_profit_cents,
                max_profit_cents=max_profit_cents,
            )

    def _update_trade_metrics(
        self,
        live: _ActiveLive,
        *,
        bid: Optional[float],
        quote_age: Optional[float],
        spread: Optional[float],
        captured_at: datetime,
    ) -> None:
        if live.status not in ("ACTIVE", "PENDING_EXIT") or live.actual_entry_price is None:
            return

        now_ts = captured_at.timestamp()
        if live.entry_fill_ts is None:
            live.entry_fill_ts = now_ts
        elapsed = max(0.0, now_ts - live.entry_fill_ts)
        last_ts = live.last_metrics_ts or live.entry_fill_ts
        delta = max(0.0, now_ts - last_ts)
        prior_bid = live.last_bid

        if quote_age is not None:
            live.ws_max_quote_age_during_trade = max(live.ws_max_quote_age_during_trade, quote_age)
        if spread is not None:
            live.ws_spread_sum += spread
            live.ws_spread_samples += 1
            if live.status == "PENDING_EXIT":
                live.ws_exit_spread = spread
        if bid is not None:
            live.last_bid = bid
            if live.status == "PENDING_EXIT":
                live.ws_exit_best_bid = bid
            # Contract prices are stored as dollar fractions (0.074 == 7.4c).
            # The instrumentation fields below are intended to be literal cents,
            # so convert the bid/entry delta explicitly once here.
            profit_cents = price_delta_to_cents(bid, live.actual_entry_price)
            if profit_cents is None:
                return
            if profit_cents > 0:
                live.went_green = True
            if live.max_bid_after_entry is None or bid > live.max_bid_after_entry:
                live.max_bid_after_entry = bid
                live.max_profit_cents = profit_cents
                live.time_to_max_bid_seconds = round(elapsed, 2)
            thresholds = (
                ("seconds_above_1c", 1.0),
                ("seconds_above_2c", 2.0),
                ("seconds_above_3c", 3.0),
                ("seconds_above_4c", 4.0),
                ("seconds_above_5c", 5.0),
            )
            for attr, threshold in thresholds:
                if profit_cents >= threshold:
                    setattr(live, attr, round(getattr(live, attr) + delta, 2))

            for mark, attr in (
                (30, "bid_at_30s"),
                (60, "bid_at_60s"),
                (90, "bid_at_90s"),
                (120, "bid_at_120s"),
            ):
                if elapsed >= mark and getattr(live, attr) is None:
                    setattr(live, attr, bid)

            target_bid = round(live.entry_ask + self._target_cents(), 4)
            if bid >= target_bid:
                if not live.target_touched:
                    live.target_touched = True
                    live.target_first_touched_at = captured_at
                if live.target_touch_count == 0 or live.last_metrics_ts is None or prior_bid is None or prior_bid < target_bid:
                    live.target_touch_count += 1
                live.target_total_visible_seconds = round(live.target_total_visible_seconds + delta, 2)

            self._maybe_record_ws_shadow_exits(
                live,
                bid=bid,
                captured_at=captured_at,
                current_profit_cents=profit_cents,
                age_seconds=elapsed,
            )

        live.last_metrics_ts = now_ts

    def _maybe_record_ws_guardrail(self) -> None:
        if not self._ws:
            return
        if self._ws.degraded and self._active and not self._ws_stale_guardrail_emitted:
            self._record_guardrail(
                "blocked_ws_stale",
                None,
                None,
                "websocket stale/degraded while position open; new entries blocked until quote stream recovers",
            )
            self._ws_stale_guardrail_emitted = True
        elif not self._ws.degraded:
            self._ws_stale_guardrail_emitted = False

    def _write_phase(self, live_trade_id: int, phase: str) -> None:
        try:
            execute_query(
                "UPDATE momentum_live_trades SET phase=%s WHERE id=%s",
                (phase, live_trade_id),
            )
        except Exception as exc:
            logger.warning(
                "phase column write failed — run migrate_add_momentum_live_telemetry.py: %s",
                exc,
            )

    def on_tick(
        self,
        market_db_id:  int,
        market_id:     str,
        market_ticker: str,
        contract_ids:  dict[str, int],
        target_price:  Optional[float],
        captured_at:   datetime,
        btc_price:     float,
        tte:           Optional[int],
        prices:        dict[str, dict],
        btc_ticks:     list[Tick],
        snapshot_id:   int,
        snapshot_seq:  int,
    ) -> None:
        """
        Process one live tick.  Mirrors the shadow tracker's ordering exactly so
        signals fire on the same ticks:
          1. market rollover
          2. build MarketRow
          3. advance active live trades (poll orders, check exits)
          4. append to window / trim
          5. detect new signals (only opens real trades when armed + gates pass)
        """
        if not self._armed:
            return
        if target_price is None:
            return
        self._subscribe_ws_interest(market_ticker)
        self._maybe_record_ws_guardrail()

        # ── Market rollover ───────────────────────────────────────────────────
        if market_id != self._current_market_id:
            if self._current_market_id is not None and self._active:
                logger.warning(
                    "live rollover: %s -> %s with %d active trade(s) — closing out",
                    self._current_market_id, market_id, len(self._active),
                )
                for live in list(self._active.values()):
                    try:
                        self._close_out_on_rollover(live, captured_at)
                    except Exception as exc:
                        logger.error("live rollover close-out failed: %s", exc)
            self._current_market_id = market_id
            self._window.clear()
            self._cooldown_until.clear()

        row = _build_market_row(
            snapshot_id=snapshot_id,
            snapshot_seq=snapshot_seq,
            captured_at=captured_at,
            btc_price=btc_price,
            tte=tte,
            prices=prices,
            btc_ticks=btc_ticks,
        )

        if self._active:
            try:
                self._advance_active(row, captured_at)
            except Exception as exc:
                logger.error("live advance-active failed: %s", exc, exc_info=True)

        self._window.append(row)
        cutoff = row.ts - _TRIM_WINDOW_S
        while self._window and self._window[0].ts < cutoff:
            self._window.popleft()

        try:
            self._detect(
                market_db_id=market_db_id,
                market_id=market_id,
                market_ticker=market_ticker,
                contract_ids=contract_ids,
                target_price=target_price,
                row=row,
                captured_at=captured_at,
            )
        except Exception as exc:
            logger.error("live detect failed: %s", exc, exc_info=True)

    # ── Signal detection (identical window math to the shadow tracker) ─────────

    def _detect(
        self,
        market_db_id:  int,
        market_id:     str,
        market_ticker: str,
        contract_ids:  dict[str, int],
        target_price:  float,
        row:           MarketRow,
        captured_at:   datetime,
    ) -> None:
        window = list(self._window)
        lookback_cutoff = row.ts - _SHADOW_CONFIG.lookback_seconds
        in_window = [r for r in window if r.ts >= lookback_cutoff]
        if len(in_window) < 3:
            return
        lookback_row = in_window[0]
        actual_lookback = row.ts - lookback_row.ts
        if actual_lookback < 0.8 * _SHADOW_CONFIG.lookback_seconds:
            return

        market_info = MarketInfo(
            id=market_db_id, market_id=market_id, target_price=target_price,
            opens_at=None, closes_at=None, settles_at=None,
            status="open", contract_ids=contract_ids,
        )

        for side in ("YES", "NO"):
            contract_id = contract_ids.get(side)
            if contract_id is None:
                continue
            if row.ts < self._cooldown_until.get(contract_id, float("-inf")):
                continue
            if contract_id in self._active:
                continue

            sig = detect_signal(
                row=row, lookback_row=lookback_row, market=market_info,
                contract_id=contract_id, side=side, config=_SHADOW_CONFIG,
                snapshot_index=len(window) - 1,
            )
            if sig is None:
                continue

            # Frozen signal fired — attempt a live entry behind the risk gates.
            self._try_open_live(sig, market_ticker, captured_at)

    # ── Risk gates ────────────────────────────────────────────────────────────

    def _check_risk_gates(
        self, sig, market_ticker: str, captured_at: datetime
    ) -> tuple[bool, str, str]:
        """
        Pre-order risk gates.  Returns (ok, guardrail_event_type, reason).
        Order matters: hard stops first, then per-trade conditions.
        """
        if kill_switch_engaged():
            return False, "kill_switch", "kill switch engaged"

        if self._flatten_only:
            return (
                False, "blocked_flatten_only",
                "flatten-only recovery mode — resolve open exposure before new entries",
            )

        if self._is_paused():
            return False, "blocked_paused", "live trading is paused (manual unpause required)"

        if len(self._active) >= config.MOMENTUM_LIVE_MAX_ACTIVE_TRADES:
            return (
                False, "blocked_max_active",
                f"max active live trades reached ({config.MOMENTUM_LIVE_MAX_ACTIVE_TRADES})",
            )

        entry_filter_reason = _entry_filter_reason(sig.side, sig.entry_ask)
        if entry_filter_reason:
            return False, "blocked_entry_filter", entry_filter_reason

        if self._ws and self._ws.degraded and self._active:
            return (
                False, "blocked_ws_stale",
                "websocket stream stale/degraded while managing an open live position",
            )

        # Quote freshness — the quote we are acting on must be recent.
        age = self._quote_age_seconds(market_ticker, captured_at)
        if age > config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS:
            return (
                False, "blocked_quote_stale",
                f"quote age {age:.1f}s > {config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS:.1f}s",
            )

        # Spread gate.
        spread = self._spread_for_side(market_ticker, sig.side, sig.entry_spread)
        if spread is None or spread > config.MOMENTUM_LIVE_MAX_SPREAD:
            return (
                False, "blocked_spread",
                f"spread {spread} > {config.MOMENTUM_LIVE_MAX_SPREAD}",
            )

        # Daily-loss gate.
        if config.MOMENTUM_LIVE_MAX_DAILY_LOSS_DOLLARS > 0:
            todays = self._todays_realized_dollars()
            if todays <= -abs(config.MOMENTUM_LIVE_MAX_DAILY_LOSS_DOLLARS):
                return (
                    False, "blocked_daily_loss",
                    f"daily realized {todays:.2f} <= "
                    f"-{config.MOMENTUM_LIVE_MAX_DAILY_LOSS_DOLLARS:.2f}",
                )
        return True, "", ""

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    def _try_open_live(self, sig, market_ticker: str, captured_at: datetime) -> bool:
        """
        Evaluate gates + sizing and, if all pass, place a real entry order and
        persist a momentum_live_trades row. Returns True only when a real entry
        order was submitted/adopted; cooldown now starts on ACTUAL fill, not on
        blocked or canceled attempts.
        """
        try:
            ok, event_type, reason = self._check_risk_gates(sig, market_ticker, captured_at)
        except TypeError:
            ok, event_type, reason = self._check_risk_gates(sig, captured_at)
        if not ok:
            self._record_guardrail(event_type, market_ticker, sig.side, reason)
            logger.warning("live BLOCKED | %s %s | %s", market_ticker, sig.side, reason)
            return False

        # Projected strategy stats (rolling shadow window) → Kelly inputs.
        proj = self._load_projected_stats()
        if proj is None or proj["n"] < config.MOMENTUM_LIVE_MIN_SHADOW_TRADES:
            self._record_guardrail(
                "blocked_sizing", market_ticker, sig.side,
                f"insufficient shadow history for sizing "
                f"(have {None if proj is None else proj['n']}, "
                f"need {config.MOMENTUM_LIVE_MIN_SHADOW_TRADES})",
            )
            return False

        entry_limit_price = compute_live_entry_limit_price(
            sig.entry_ask,
            config.MOMENTUM_LIVE_ENTRY_PRICE_OFFSET_CENTS,
        )

        full_kelly = compute_full_kelly_fraction(
            proj["win_rate"], proj["profit_loss_ratio"]
        )
        if config.MOMENTUM_LIVE_SIZE_MODE == "fixed":
            contracts, dollars_budgeted = compute_fixed_position_size(
                fixed_contracts=config.MOMENTUM_LIVE_FIXED_CONTRACTS,
                max_dollars_per_trade=config.MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE,
                max_contracts_per_trade=config.MOMENTUM_LIVE_MAX_CONTRACTS_PER_TRADE,
                price_per_contract=entry_limit_price,
            )
        else:
            contracts, dollars_budgeted = compute_position_size(
                bankroll_dollars=config.MOMENTUM_LIVE_BANKROLL_DOLLARS,
                kelly_fraction=config.MOMENTUM_LIVE_KELLY_FRACTION,
                full_kelly_fraction=full_kelly,
                max_dollars_per_trade=config.MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE,
                max_contracts_per_trade=config.MOMENTUM_LIVE_MAX_CONTRACTS_PER_TRADE,
                price_per_contract=entry_limit_price,
            )
        if contracts < 1:
            self._record_guardrail(
                "blocked_sizing", market_ticker, sig.side,
                f"sizing rounded down to 0 contracts "
                f"(mode={config.MOMENTUM_LIVE_SIZE_MODE}, full_kelly={full_kelly:.3f}, "
                f"budget=${dollars_budgeted:.2f}, "
                f"price={entry_limit_price:.3f})",
            )
            return False

        target_ask = round(sig.entry_ask + _TP, 4)
        now = datetime.now(timezone.utc)
        ws = getattr(self, "_ws", None)
        ws_best_bid = ws.get_best_bid(market_ticker, sig.side) if ws else None
        ws_best_ask = self._best_ask_for_side(market_ticker, sig.side)
        ws_spread = self._spread_for_side(
            market_ticker, sig.side, getattr(sig, "entry_spread", None)
        )
        ws_quote_age = self._quote_age_seconds(market_ticker, captured_at)

        # Idempotency: generate the client_order_id and PERSIST it (with the row)
        # BEFORE the POST, so an ambiguous submission can be reconciled by id.
        coid = str(uuid.uuid4())

        # Persist the trade row up front (PENDING_ENTRY) with projected stats.
        live_trade_id = insert_and_get_id(
            """
            INSERT INTO momentum_live_trades (
                market_id, contract_id, market_ticker, side,
                signal_at, exit_profile,
                bankroll_at_entry, kelly_fraction, kelly_full_fraction,
                dollars_budgeted, requested_contracts,
                projected_entry_ask, projected_target_ask,
                projected_expectancy_cents, projected_win_rate,
                projected_profit_factor, projected_profit_loss_ratio,
                ws_enabled, ws_quote_age_at_entry, ws_spread_at_entry,
                ws_entry_best_bid, ws_entry_best_ask, entry_signal_to_order_ms,
                entry_client_order_id,
                phase, entry_order_price, entry_order_type,
                status, created_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s,
                'ENTRY_SUBMITTED', %s, 'limit',
                'PENDING_ENTRY', %s
            )
            """,
            (
                sig.market_id, sig.contract_id, market_ticker, sig.side,
                sig.signal_at, _PROFILE,
                config.MOMENTUM_LIVE_BANKROLL_DOLLARS,
                config.MOMENTUM_LIVE_KELLY_FRACTION, round(full_kelly, 4),
                dollars_budgeted, contracts,
                sig.entry_ask, target_ask,
                _safe(proj["expectancy"]), _safe(proj["win_rate"]),
                _safe(proj["profit_factor"]), _safe(proj["profit_loss_ratio"]),
                1 if ws else 0, _safe(ws_quote_age), _safe(ws_spread),
                _safe(ws_best_bid), _safe(ws_best_ask),
                int(max(0.0, (now.timestamp() - sig.signal_ts) * 1000.0)),
                coid,
                # phase=ENTRY_SUBMITTED, entry_order_price, entry_order_type=limit
                _safe(entry_limit_price),
                now,
            ),
        )

        live = _ActiveLive(
            live_trade_id=live_trade_id,
            market_id=sig.market_id,
            contract_id=sig.contract_id,
            market_ticker=market_ticker,
            side=sig.side,
            signal_at=sig.signal_at,
            signal_ts=sig.signal_ts,
            entry_ask=sig.entry_ask,
            horizon_ts=sig.signal_ts + _HOLD_S,
            requested_contracts=contracts,
            status="PENDING_ENTRY",
            entry_client_order_id=coid,
            ws_enabled=bool(ws),
            ws_quote_age_at_entry=ws_quote_age,
            ws_spread_at_entry=ws_spread,
            ws_entry_best_bid=ws_best_bid,
            ws_entry_best_ask=ws_best_ask,
            entry_signal_to_order_ms=int(max(0.0, (now.timestamp() - sig.signal_ts) * 1000.0)),
            entry_order_price=entry_limit_price,
        )

        # Place the real entry order (limit buy at the observed ask).
        live.entry_submit_ts = datetime.now(timezone.utc).timestamp()
        try:
            order = self._client.place_order(
                ticker=market_ticker, side=sig.side, action="buy",
                count=contracts, limit_price=entry_limit_price, order_type="limit",
                client_order_id=coid,
            )
        except KalshiTradingError as exc:
            # Ambiguous: the POST may have succeeded but the response was lost.
            # Reconcile by client_order_id before declaring it failed.
            order = self._reconcile_ambiguous_order(coid, market_ticker)
            if order is None:
                execute_query(
                    "UPDATE momentum_live_trades SET status='REJECTED' WHERE id=%s",
                    (live_trade_id,),
                )
                self._write_phase(live_trade_id, "REJECTED")
                self._record_order_event(
                    live, "entry_rejected", action="buy",
                    requested_count=contracts, limit_price=entry_limit_price,
                    client_order_id=coid, detail=str(exc)[:480],
                )
                self._start_retry_cooldown(
                    sig.contract_id,
                    max(sig.signal_ts, captured_at.timestamp()),
                    "entry rejected",
                )
                logger.error("live ENTRY REJECTED #%d | %s", live_trade_id, exc)
                return False
            logger.warning(
                "live ENTRY adopted after ambiguous submission #%d (coid=%s)",
                live_trade_id, coid,
            )

        live.entry_order_id = str(order.get("order_id") or order.get("id") or "")
        live.entry_ack_ts = datetime.now(timezone.utc).timestamp()
        if live.entry_submit_ts is not None:
            live.entry_order_to_ack_ms = int(max(0.0, (live.entry_ack_ts - live.entry_submit_ts) * 1000.0))
        entry_submitted_at_dt = (
            datetime.fromtimestamp(live.entry_submit_ts, tz=timezone.utc)
            if live.entry_submit_ts is not None else None
        )
        execute_query(
            "UPDATE momentum_live_trades SET entry_order_id=%s, entry_order_to_ack_ms=%s, "
            "entry_order_submitted_at=%s WHERE id=%s",
            (live.entry_order_id, live.entry_order_to_ack_ms, entry_submitted_at_dt, live_trade_id),
        )
        self._record_order_event(
            live, "order_acknowledged", action="buy",
            requested_count=contracts, limit_price=entry_limit_price,
            order_id=live.entry_order_id, client_order_id=coid, raw=order,
        )
        self._record_order_event(
            live, "entry_submitted", action="buy",
            requested_count=contracts, limit_price=entry_limit_price,
            order_id=live.entry_order_id, client_order_id=coid, raw=order,
        )
        self._active[sig.contract_id] = live
        logger.warning(
            "live ENTRY SUBMITTED #%d | %s %s | x%d @ %.3f (shadow %.3f, order=%s)",
            live_trade_id, market_ticker, sig.side, contracts,
            entry_limit_price, sig.entry_ask, live.entry_order_id,
        )
        return True

    def _advance_active(self, row: MarketRow, captured_at: datetime) -> None:
        """Advance every active live trade by one tick."""
        for contract_id, live in list(self._active.items()):
            # Flattening trades belong to a market the main loop no longer feeds
            # (rolled over / recovered on restart); manage them off the quote
            # stream using last_bid, never the current row.
            if live.flattening:
                try:
                    self._advance_flattening(live, captured_at)
                except KalshiTradingError as exc:
                    logger.warning(
                        "live flatten poll failed (#%d, retry next tick): %s",
                        live.live_trade_id, exc,
                    )
                continue

            bid = self._best_bid_for_side(live.market_ticker, live.side, row)
            if bid is not None:
                live.last_bid = bid
            live.last_ask = self._best_ask_for_side(live.market_ticker, live.side)

            # Projected excursion tracking (identical to shadow tracker).
            if row.ts <= live.horizon_ts and bid is not None:
                live.peak_bid = max(live.peak_bid, bid)
                live.trough_bid = min(live.trough_bid, bid)

            try:
                if live.status == "PENDING_ENTRY":
                    self._advance_pending_entry(live, row, captured_at)
                elif live.status == "ACTIVE":
                    self._advance_active_position(live, row, bid, captured_at)
                elif live.status == "PENDING_EXIT":
                    self._advance_pending_exit(live, row, bid, captured_at)
            except KalshiTradingError as exc:
                # Network/API hiccup — log and retry on the next tick.
                logger.warning(
                    "live order poll failed (#%d, retry next tick): %s",
                    live.live_trade_id, exc,
                )

    def _advance_pending_entry(self, live: _ActiveLive, row: MarketRow, captured_at: datetime) -> None:
        """
        Poll the entry order with partial-fill safety.

        We must never promote to ACTIVE while quantity is still resting and
        untracked, and never lose residual quantity.  Strategy:
          - if fully filled -> promote with the final fill count;
          - if partially filled while the order is still open -> cancel the
            remainder once, then promote on the next tick from the (now final)
            cumulative fills;
          - if unfilled past timeout/horizon -> cancel and abandon;
          - if the order is already terminal -> promote (filled>=1) or abandon.
        ``filled_contracts`` is always kept in sync with cumulative fills.
        """
        if not live.entry_order_id:
            return

        ws_state = self._ws_order_state(live)
        fills = self._client.get_fills(order_id=live.entry_order_id)
        count, avg_price, entry_fees = _summarize_fills(fills, live.side)
        if ws_state and ws_state.filled_count and int(round(ws_state.filled_count)) > count:
            count = int(round(ws_state.filled_count))
            avg_price = ws_state.avg_fill_price or avg_price
            entry_fees = ws_state.fee_total if ws_state.fee_total is not None else entry_fees
        # Keep partial state current at all times (residual is never lost).
        live.filled_contracts = count
        live.actual_entry_price = avg_price
        live.actual_entry_fees = entry_fees

        # Fully filled — promote immediately, nothing resting.
        if count >= live.requested_contracts:
            self._promote_entry_to_active(live, captured_at)
            return

        order = None if (ws_state and ws_state.status) else self._safe_get_order(live.entry_order_id)
        if ws_state and ws_state.status:
            order = {
                "status": ws_state.status,
                "remaining_count_fp": ws_state.remaining_count,
            }
        order_open = is_order_open(order)

        if order_open:
            elapsed = datetime.now(timezone.utc).timestamp() - (live.entry_submit_ts or 0)
            timed_out = elapsed > config.MOMENTUM_LIVE_ENTRY_FILL_TIMEOUT_SECONDS
            past_horizon = row.ts >= live.horizon_ts

            # Partial fill while still resting: cancel the remainder once so no
            # quantity stays untracked. Promotion happens once it goes terminal.
            if count >= 1 and not live.entry_cancel_requested:
                self._cancel_entry_remainder(live, "lock in partial entry fill")
                return
            # No fill and out of time: cancel; abandon once terminal.
            if count == 0 and (timed_out or past_horizon) and not live.entry_cancel_requested:
                self._cancel_entry_remainder(live, "unfilled past timeout/horizon")
                return
            # Still working within the fill window — wait.
            return

        # Order is terminal (executed/canceled/expired).
        if count >= 1:
            self._promote_entry_to_active(live, captured_at)
        else:
            self._abandon_entry(live, "unfilled (order terminal)", retry_from_ts=row.ts)

    def _cancel_entry_remainder(self, live: _ActiveLive, reason: str) -> None:
        """Cancel the resting entry order; promotion/abandon resolves next tick."""
        try:
            self._client.cancel_order(live.entry_order_id)
        except KalshiTradingError as exc:
            logger.warning("entry cancel failed #%d: %s", live.live_trade_id, exc)
        live.entry_cancel_requested = True
        self._record_order_event(
            live, "cancel_requested", action="buy",
            requested_count=live.requested_contracts,
            filled_count=live.filled_contracts,
            order_id=live.entry_order_id,
            detail=reason,
        )
        logger.info("live ENTRY cancel requested #%d | %s", live.live_trade_id, reason)

    def _promote_entry_to_active(self, live: _ActiveLive, captured_at: datetime) -> None:
        """Lock in the final entry fill and move to ACTIVE (no residual qty)."""
        count = live.filled_contracts
        live.status = "ACTIVE"
        live.entry_fill_ts = captured_at.timestamp()
        live.entry_fill_detected_by = "websocket" if (self._ws_order_state(live) and self._ws_order_state(live).filled_count) else "rest"
        if live.entry_ack_ts is not None:
            live.entry_ack_to_fill_ms = int(max(0.0, (live.entry_fill_ts - live.entry_ack_ts) * 1000.0))
        self._cooldown_until[live.contract_id] = max(
            self._cooldown_until.get(live.contract_id, float("-inf")),
            live.signal_ts + _SHADOW_CONFIG.cooldown_seconds,
        )
        execute_query(
            "UPDATE momentum_live_trades SET status='ACTIVE', "
            "filled_contracts=%s, actual_entry_price=%s, actual_entry_fees=%s, "
            "entry_at=%s, entry_fill_detected_by=%s, entry_ack_to_fill_ms=%s WHERE id=%s",
            (count, live.actual_entry_price, live.actual_entry_fees,
             captured_at, live.entry_fill_detected_by, live.entry_ack_to_fill_ms, live.live_trade_id),
        )
        self._write_phase(live.live_trade_id, "ENTRY_FILLED")
        self._record_order_event(
            live,
            "entry_filled" if count >= live.requested_contracts else "entry_partial",
            action="buy", requested_count=live.requested_contracts,
            filled_count=count, avg_fill_price=live.actual_entry_price,
            order_id=live.entry_order_id,
        )
        logger.warning(
            "live ENTRY FILLED #%d | %s %s | %d/%d @ %.3f",
            live.live_trade_id, live.market_ticker, live.side,
            count, live.requested_contracts, live.actual_entry_price or 0.0,
        )

    def _start_retry_cooldown(self, contract_id: int, retry_from_ts: float, reason: str) -> None:
        """Short retry throttle after an unfilled/rejected entry attempt."""
        delay = config.MOMENTUM_LIVE_RETRY_AFTER_CANCEL_SECONDS
        if delay <= 0:
            return
        until = retry_from_ts + delay
        self._cooldown_until[contract_id] = max(
            self._cooldown_until.get(contract_id, float("-inf")),
            until,
        )
        logger.info(
            "live ENTRY retry cooldown | contract=%s | %.1fs | %s",
            contract_id, delay, reason,
        )

    def _abandon_entry(self, live: _ActiveLive, reason: str, retry_from_ts: Optional[float] = None) -> None:
        """No position taken — mark CANCELED and drop from active management."""
        execute_query(
            "UPDATE momentum_live_trades SET status='CANCELED' WHERE id=%s",
            (live.live_trade_id,),
        )
        self._write_phase(live.live_trade_id, "CANCELED")
        self._record_order_event(
            live, "entry_canceled", action="buy",
            requested_count=live.requested_contracts, filled_count=0,
            order_id=live.entry_order_id, detail=reason,
        )
        self._start_retry_cooldown(
            live.contract_id,
            retry_from_ts if retry_from_ts is not None else datetime.now(timezone.utc).timestamp(),
            reason,
        )
        logger.info("live ENTRY CANCELED (%s) #%d", reason, live.live_trade_id)
        self._active.pop(live.contract_id, None)

    def _safe_get_order(self, order_id: str) -> Optional[dict]:
        """get_order that returns None on API error (caller treats as open)."""
        try:
            return self._client.get_order(order_id)
        except KalshiTradingError as exc:
            logger.warning("get_order failed (%s): %s", order_id, exc)
            return None

    def _advance_active_position(
        self, live: _ActiveLive, row: MarketRow, bid: Optional[float], captured_at: datetime
    ) -> None:
        """
        Apply the frozen exit rules.  On the FIRST firing we COMMIT to exiting:
        the projected (shadow) exit is locked, the trade flips to PENDING_EXIT,
        and all subsequent flatten attempts/retries run through the exit path
        (so a failed submit or a price that no longer re-qualifies can never
        strand the position before its horizon).
        """
        quote_age = self._ws.get_quote_age_seconds(live.market_ticker) if self._ws else None
        spread = self._ws.get_spread(live.market_ticker, live.side) if self._ws else None
        self._update_trade_metrics(
            live,
            bid=bid,
            quote_age=quote_age,
            spread=spread,
            captured_at=captured_at,
        )
        exit_reason, exit_bid = self._frozen_exit_decision(live, row, bid)
        if exit_reason is None:
            return

        # Lock projected exit (shadow outcome) at first firing; commit to exit.
        live.exit_reason = exit_reason
        live.projected_exit_bid = exit_bid   # may be None for "unexecutable"
        if live.exit_signal_ts is None:
            live.exit_signal_ts = captured_at.timestamp()
        live.status = "PENDING_EXIT"
        exit_signal_at_dt = datetime.fromtimestamp(live.exit_signal_ts, tz=timezone.utc)
        time_remaining = round(max(0.0, live.horizon_ts - live.exit_signal_ts), 2)
        execute_query(
            "UPDATE momentum_live_trades SET status='PENDING_EXIT', "
            "exit_reason=%s, exit_signal_at=%s, time_remaining_in_market_seconds=%s WHERE id=%s",
            (exit_reason, exit_signal_at_dt, time_remaining, live.live_trade_id),
        )
        self._write_phase(live.live_trade_id, "EXIT_SIGNALLED")

        remaining = order_remaining(live.filled_contracts, live.exit_filled_contracts)
        if remaining < 1:
            return

        if exit_bid is None:
            # Frozen "unexecutable": no bid to sell into. Stay PENDING_EXIT and
            # retry to flatten as soon as a bid appears (never fabricate a fill).
            self._record_guardrail(
                "exit_no_bid", live.market_ticker, live.side,
                f"exit fired ({exit_reason}) with no bid; will retry to flatten",
            )
            return

        self._submit_exit(live, exit_bid, captured_at, exit_reason, remaining)

    def _aggregate_exit_fills(self, live: _ActiveLive) -> tuple[int, Optional[float], Optional[float]]:
        """
        Cumulative exit fills = restart BASELINE (prior closed exits) + this
        session's exit orders.  Persists the running totals so a later restart
        can reconstruct the full round trip.
        """
        fill_lists = [
            self._client.get_fills(order_id=oid) for oid in live.exit_order_ids
        ]
        s_count, s_avg, s_fees = aggregate_fills(fill_lists, live.side)
        combined = combine_exit_fills(
            live.exit_baseline_count, live.exit_baseline_value, live.exit_baseline_fees,
            s_count, s_avg, s_fees,
        )
        live.exit_filled_contracts = combined["count"]
        live.exit_value = combined["value"]
        live.exit_fees = combined["fees"]
        self._persist_exit_progress(live)
        return combined["count"], combined["avg"], combined["fees"]

    def _persist_exit_progress(self, live: _ActiveLive) -> None:
        """Persist cumulative exit progress for restart-safe reconstruction."""
        try:
            execute_query(
                "UPDATE momentum_live_trades SET exit_filled_contracts=%s, "
                "exit_value_cents=%s, exit_fees_total=%s WHERE id=%s",
                (
                    live.exit_filled_contracts,
                    round(live.exit_value, 6) if live.exit_accounting_verified else None,
                    live.exit_fees if live.exit_accounting_verified else None,
                    live.live_trade_id,
                ),
            )
        except Exception as exc:
            logger.warning("failed to persist exit progress #%d: %s", live.live_trade_id, exc)

    def _reconstruct_exit_from_fills(
        self, ticker: str, side: str
    ) -> tuple[int, Optional[float], Optional[float]]:
        """
        Authoritative cumulative exit (count, avg_price, fees) for a ticker/side
        from the fills history (SELL fills).  Used on restart when the persisted
        progress does not match the live position.

        NOTE: assumes get_fills(ticker=...) returns historical (closed-order)
        fills and that each fill exposes an ``action`` of buy/sell — confirm
        against the live Kalshi API.
        """
        fills = self._client.get_fills(ticker=ticker)
        sells = [f for f in fills if str(f.get("action") or "").strip().lower() == "sell"]
        return _summarize_fills(sells, side)

    def _advance_pending_exit(
        self, live: _ActiveLive, row: MarketRow, bid: Optional[float], captured_at: datetime
    ) -> None:
        """
        Poll exit order(s) with partial-exit safety.

        Aggregates fills across ALL exit orders for the trade, finalizes only
        when cumulative exit fills cover the full entry position, and only ever
        resubmits the REMAINING quantity (never the original full size).
        """
        quote_age = self._ws.get_quote_age_seconds(live.market_ticker) if self._ws else None
        spread = self._ws.get_spread(live.market_ticker, live.side) if self._ws else None
        self._update_trade_metrics(
            live,
            bid=bid,
            quote_age=quote_age,
            spread=spread,
            captured_at=captured_at,
        )
        if not live.exit_order_ids:
            # We owe an exit but never placed one (no bid earlier) — try now.
            remaining = order_remaining(live.filled_contracts, live.exit_filled_contracts)
            if bid is not None and remaining >= 1:
                self._submit_exit(live, bid, captured_at,
                                  live.exit_reason or EXIT_FIXED_TIME, remaining)
            return

        count, avg_price, exit_fees = self._aggregate_exit_fills(live)
        ws_state = self._ws_order_state(live)
        if ws_state and ws_state.filled_count and int(round(ws_state.filled_count)) >= count:
            live.exit_fill_detected_by = "websocket"
        if count >= live.filled_contracts and count >= 1:
            self._finalize_complete(live, avg_price, exit_fees, count, captured_at)
            return

        remaining = order_remaining(live.filled_contracts, count)
        latest = live.exit_order_id
        order = self._safe_get_order(latest) if latest else None
        order_open = is_order_open(order) if latest else False

        if not order_open:
            # Latest exit order is done but we are not flat — sell the remainder.
            if bid is not None and remaining >= 1:
                self._submit_exit(live, bid, captured_at,
                                  live.exit_reason or EXIT_FIXED_TIME, remaining)
            return

        # Still resting — chase the bid after the timeout: cancel, then ONLY
        # replace once the cancelled order is CONFIRMED TERMINAL.  A still-open
        # order can keep filling, so deriving the replacement quantity from a
        # stale snapshot could oversell.  If we can't confirm terminal this
        # tick, do nothing and retry next tick (fail safe).
        elapsed = datetime.now(timezone.utc).timestamp() - (live.exit_submit_ts or 0)
        if elapsed > config.MOMENTUM_LIVE_ENTRY_FILL_TIMEOUT_SECONDS and bid is not None:
            try:
                self._client.cancel_order(latest)
            except KalshiTradingError as exc:
                logger.warning("exit cancel failed #%d: %s", live.live_trade_id, exc)
            # Re-confirm terminal state of the cancelled order before replacing.
            confirmed = self._safe_get_order(latest)
            count, avg_price, exit_fees = self._aggregate_exit_fills(live)
            if count >= live.filled_contracts and count >= 1:
                self._finalize_complete(live, avg_price, exit_fees, count, captured_at)
                return
            remaining = order_remaining(live.filled_contracts, count)
            if not should_replace_exit(confirmed, remaining):
                # Not confirmed terminal (or nothing left) — do NOT replace now;
                # the cancel will settle and we retry on a later tick.
                logger.info(
                    "exit replace deferred #%d — cancel not confirmed terminal yet",
                    live.live_trade_id,
                )
                return
            self._submit_exit(live, bid, captured_at,
                              live.exit_reason or EXIT_FIXED_TIME, remaining)

    def _advance_flattening(self, live: _ActiveLive, captured_at: datetime) -> None:
        """
        Manage a position that must be flattened off the main quote stream
        (market rolled over, or rehydrated on restart).  Polls exit fills (no
        market data needed) and resubmits the remaining quantity at the last
        observed bid until flat.
        """
        count, avg_price, exit_fees = self._aggregate_exit_fills(live)
        ws_state = self._ws_order_state(live)
        if ws_state and ws_state.filled_count and int(round(ws_state.filled_count)) >= count:
            live.exit_fill_detected_by = "websocket"
        if count >= live.filled_contracts and count >= 1:
            self._finalize_complete(live, avg_price, exit_fees, count, captured_at)
            return

        remaining = order_remaining(live.filled_contracts, count)
        if remaining < 1:
            return
        latest = live.exit_order_id
        order = self._safe_get_order(latest) if latest else None
        order_open = is_order_open(order) if latest else False
        if not order_open and live.last_bid is not None:
            self._submit_exit(live, live.last_bid, captured_at,
                              live.exit_reason or EXIT_FIXED_TIME, remaining)

    def _frozen_exit_decision(
        self, live: _ActiveLive, row: MarketRow, bid: Optional[float]
    ) -> tuple[Optional[str], Optional[float]]:
        """
        Base replica of the shadow tracker's exit rule order for ht120s_tp3c:
          1. profit target: bid >= entry_ask + 0.03
          2. optional live-only exit experiments, if enabled
          3. fixed time:    elapsed >= 120s, first available bid
          4. grace:         no bid > 10s past horizon -> unexecutable
        """
        if bid is not None and bid >= live.entry_ask + _TP:
            return EXIT_PROFIT_TARGET, bid
        experimental_reason, experimental_bid = self._experimental_exit_decision(live, row, bid)
        if experimental_reason is not None:
            return experimental_reason, experimental_bid
        if row.ts >= live.horizon_ts:
            if bid is not None:
                return EXIT_FIXED_TIME, bid
            if live.grace_start is None:
                live.grace_start = row.ts
            if row.ts > live.grace_start + _GRACE_S:
                return EXIT_UNEXECUTABLE, None
        return None, None

    def _submit_exit(
        self, live: _ActiveLive, bid: float, captured_at: datetime,
        exit_reason: str, qty: int,
    ) -> None:
        """
        Place a limit sell for ``qty`` (the REMAINING quantity, never the full
        original size).  Generates and persists a client_order_id before the
        POST; on an ambiguous failure, reconciles by that id so a landed order
        is adopted instead of duplicated.
        """
        if qty < 1:
            return
        coid = str(uuid.uuid4())
        live.exit_submit_ts = datetime.now(timezone.utc).timestamp()
        exit_submitted_at_dt = datetime.fromtimestamp(live.exit_submit_ts, tz=timezone.utc)
        if live.exit_signal_ts is None:
            live.exit_signal_ts = captured_at.timestamp()
            live.exit_signal_to_order_ms = int(max(0.0, (live.exit_submit_ts - live.exit_signal_ts) * 1000.0))
        if live.exit_signal_ts is not None and live.exit_signal_to_order_ms is None:
            live.exit_signal_to_order_ms = int(
                max(0.0, (live.exit_submit_ts - live.exit_signal_ts) * 1000.0)
            )
        if live.entry_fill_ts is not None:
            live.fill_to_exit_order_ms = int(max(0.0, (live.exit_submit_ts - live.entry_fill_ts) * 1000.0))

        # Capture exit-time market conditions on first exit submission only.
        is_first_exit_submit = live.exit_order_price is None
        if is_first_exit_submit:
            live.exit_order_price = bid
            ws_at_exit = getattr(self, "_ws", None)
            live.quote_age_at_exit_seconds = (
                ws_at_exit.get_quote_age_seconds(live.market_ticker)
                if ws_at_exit else None
            )
            exit_signal_at_dt = (
                datetime.fromtimestamp(live.exit_signal_ts, tz=timezone.utc)
                if live.exit_signal_ts else None
            )
        else:
            exit_signal_at_dt = None  # already persisted

        execute_query(
            "UPDATE momentum_live_trades SET status='PENDING_EXIT', "
            "exit_client_order_id=%s, exit_reason=%s, fill_to_exit_order_ms=%s, "
            "exit_signal_to_order_ms=%s, exit_order_submitted_at=%s"
            + (", exit_order_price=%s, exit_order_type='limit', "
               "quote_age_at_exit_seconds=%s, exit_signal_at=%s"
               if is_first_exit_submit else "")
            + " WHERE id=%s",
            (
                coid, exit_reason, live.fill_to_exit_order_ms, live.exit_signal_to_order_ms,
                exit_submitted_at_dt,
            ) + (
                (live.exit_order_price, live.quote_age_at_exit_seconds, exit_signal_at_dt)
                if is_first_exit_submit else ()
            ) + (live.live_trade_id,),
        )
        self._write_phase(live.live_trade_id, "EXIT_SUBMITTED")
        self._record_order_event(
            live, "exit_submit_intent", action="sell",
            requested_count=qty, limit_price=bid, client_order_id=coid,
            detail="persisted before submit for restart reconciliation",
        )
        try:
            order = self._client.place_order(
                ticker=live.market_ticker, side=live.side, action="sell",
                count=qty, limit_price=bid, order_type="limit",
                client_order_id=coid,
            )
        except KalshiTradingError as exc:
            # Ambiguous: the order may have landed. Try to adopt it by coid so we
            # do not double-sell; otherwise leave it for next-tick retry (which
            # only ever resubmits the remaining quantity).
            adopted = self._reconcile_ambiguous_order(coid, live.market_ticker)
            if adopted is None:
                self._record_order_event(
                    live, "exit_rejected", action="sell",
                    requested_count=qty, limit_price=bid,
                    client_order_id=coid, detail=str(exc)[:480],
                )
                logger.error("live EXIT REJECTED #%d | %s", live.live_trade_id, exc)
                return
            order = adopted

        oid = str(order.get("order_id") or order.get("id") or "")
        if oid and oid not in live.exit_order_ids:
            live.exit_order_ids.append(oid)
        live.exit_order_id = oid
        live.ws_exit_best_bid = bid
        ws = getattr(self, "_ws", None)
        live.ws_exit_spread = ws.get_spread(live.market_ticker, live.side) if ws else live.ws_exit_spread
        live.status = "PENDING_EXIT"
        execute_query(
            "UPDATE momentum_live_trades SET status='PENDING_EXIT', "
            "exit_order_id=%s, exit_client_order_id=%s, exit_reason=%s WHERE id=%s",
            (oid, coid, exit_reason, live.live_trade_id),
        )
        self._record_order_event(
            live, "exit_submitted", action="sell",
            requested_count=qty, limit_price=bid,
            order_id=oid, client_order_id=coid, raw=order,
        )
        logger.warning(
            "live EXIT SUBMITTED #%d | %s %s | x%d @ %.3f (%s)",
            live.live_trade_id, live.market_ticker, live.side, qty, bid, exit_reason,
        )

    def _reconcile_ambiguous_order(
        self, client_order_id: str, ticker: str
    ) -> Optional[dict]:
        """Return the order matching client_order_id if it actually landed, else None."""
        try:
            return self._client.find_order_by_client_order_id(client_order_id, ticker=ticker)
        except KalshiTradingError as exc:
            logger.warning("ambiguous-order reconcile failed (%s): %s", client_order_id, exc)
            return None

    def _finalize_complete(
        self, live: _ActiveLive, actual_exit_price: Optional[float],
        exit_fees_total: Optional[float], exit_count: int, captured_at: datetime,
    ) -> None:
        """
        Compute projected/actual/drift, persist COMPLETE, evaluate pause.

        ``actual_exit_price``/``exit_fees_total``/``exit_count`` are the values
        AGGREGATED across all exit orders for the trade (see _aggregate_exit_fills),
        so the recorded outcome reflects the whole round trip, not the last order.
        """
        live.exit_filled_contracts = exit_count
        # Projected (shadow-of-this-trade) pnl on observed quotes.
        projected_profit = None
        if live.projected_exit_bid is not None:
            projected_profit = round(
                live.projected_exit_bid - live.entry_ask - _PROJ_FEE - _PROJ_SLIPPAGE, 6
            )

        # Actual pnl from real fills — charges BOTH entry and exit fees,
        # normalized to per-contract (see compute_actual_pnl).
        if live.exit_accounting_verified:
            actual = compute_actual_pnl(
                actual_entry_price=live.actual_entry_price,
                actual_exit_price=actual_exit_price,
                entry_fees_total=live.actual_entry_fees,
                exit_fees_total=exit_fees_total,
                filled_contracts=live.filled_contracts,
            )
            actual_profit = actual["actual_profit_cents"]
            actual_profit_dollars = actual["actual_profit_dollars"]
            actual_won = actual["actual_trade_won"]
            actual_fees_per_contract = actual["fees_per_contract"]
        else:
            actual_exit_price = None
            exit_fees_total = None
            actual_profit = None
            actual_profit_dollars = None
            actual_won = None
            actual_fees_per_contract = None

        # Projected strategy expectancy stored at entry (re-read for drift).
        row = fetch_one(
            "SELECT projected_expectancy_cents FROM momentum_live_trades WHERE id=%s",
            (live.live_trade_id,),
        )
        projected_expectancy = (
            float(row["projected_expectancy_cents"])
            if row and row.get("projected_expectancy_cents") is not None else None
        )

        drift = compute_drift_fields(
            projected_entry_ask=live.entry_ask,
            projected_exit_bid=live.projected_exit_bid,
            projected_profit=projected_profit,
            projected_expectancy=projected_expectancy,
            actual_entry_price=live.actual_entry_price,
            actual_exit_price=actual_exit_price,
            actual_profit=actual_profit,
        )

        holding_s = round(captured_at.timestamp() - live.signal_ts, 1)

        if actual_exit_price is not None:
            live.ws_exit_best_bid = actual_exit_price
        ws_avg_spread = (
            round(live.ws_spread_sum / live.ws_spread_samples, 6)
            if live.ws_spread_samples > 0 else None
        )
        # ── True-cents slippage / decay (distinct from price-unit drift fields) ──
        # ideal_entry_ask / ideal_exit_bid are the projected shadow prices.
        entry_slippage = compute_entry_slippage_cents(live.entry_ask, live.actual_entry_price)
        exit_slippage = compute_exit_slippage_cents(live.projected_exit_bid, actual_exit_price)
        total_slippage = (
            round(entry_slippage + exit_slippage, 4)
            if entry_slippage is not None and exit_slippage is not None else None
        )
        proj_path_pnl = compute_projected_path_pnl_cents(live.entry_ask, live.projected_exit_bid)
        actual_path_pnl = compute_actual_path_pnl_cents(live.actual_entry_price, actual_exit_price)
        live_decay = compute_live_decay_cents(proj_path_pnl, actual_path_pnl)

        # ── Bucket classification ─────────────────────────────────────────────
        ep_bucket = bucket_entry_price(live.entry_ask)
        sp_entry_bucket = bucket_spread(live.ws_spread_at_entry)
        sp_exit_bucket = bucket_spread(live.ws_exit_spread)
        qa_entry_bucket = bucket_quote_age(live.ws_quote_age_at_entry)
        qa_exit_bucket = bucket_quote_age(live.quote_age_at_exit_seconds)
        # time_remaining_in_market_seconds already persisted at exit signal; re-derive here
        # from the data that's available (horizon - exit_signal_ts)
        tr_seconds = (
            round(max(0.0, live.horizon_ts - live.exit_signal_ts), 2)
            if live.exit_signal_ts is not None else None
        )
        tr_bucket = bucket_time_remaining(tr_seconds)
        fc_bucket = bucket_filled_contracts(live.requested_contracts, live.filled_contracts)
        ot_bucket = "limit"

        execute_query(
            """
            UPDATE momentum_live_trades SET
                status='COMPLETE', exit_at=%s, exit_reason=%s, holding_seconds=%s,
                actual_exit_price=%s, actual_fees_cents=%s,
                actual_profit_cents=%s, actual_profit_dollars=%s, actual_trade_won=%s,
                projected_exit_bid=%s, projected_profit_cents=%s,
                ws_exit_best_bid=%s, ws_exit_spread=%s, ws_avg_spread_during_trade=%s,
                ws_max_quote_age_during_trade=%s, max_bid_after_entry=%s, max_profit_cents=%s,
                time_to_max_bid_seconds=%s, seconds_above_1c=%s, seconds_above_2c=%s,
                seconds_above_3c=%s, seconds_above_4c=%s, seconds_above_5c=%s,
                bid_at_30s=%s, bid_at_60s=%s, bid_at_90s=%s, bid_at_120s=%s, went_green=%s,
                target_touched=%s, target_touch_count=%s, target_first_touched_at=%s,
                target_total_visible_seconds=%s, exit_fill_detected_by=%s,
                profit_delta_cents=%s, expectancy_delta_cents=%s,
                entry_price_drift_cents=%s, exit_price_drift_cents=%s,
                total_execution_drift_cents=%s,
                profit_capture_ratio=%s, expectancy_capture_ratio=%s,
                entry_slippage_cents=%s, exit_slippage_cents=%s,
                total_execution_slippage_cents=%s,
                projected_path_pnl_cents=%s, actual_pnl_cents=%s, live_decay_cents=%s,
                entry_price_bucket=%s, spread_bucket_entry=%s, spread_bucket_exit=%s,
                quote_age_bucket_entry=%s, quote_age_bucket_exit=%s,
                time_remaining_bucket=%s, filled_contract_bucket=%s, order_type_bucket=%s
            WHERE id=%s
            """,
            (
                captured_at, live.exit_reason, holding_s,
                actual_exit_price, actual_fees_per_contract,
                actual_profit, actual_profit_dollars, actual_won,
                live.projected_exit_bid, projected_profit,
                live.ws_exit_best_bid, live.ws_exit_spread, ws_avg_spread,
                live.ws_max_quote_age_during_trade or None, live.max_bid_after_entry,
                live.max_profit_cents, live.time_to_max_bid_seconds,
                live.seconds_above_1c, live.seconds_above_2c, live.seconds_above_3c,
                live.seconds_above_4c, live.seconds_above_5c,
                live.bid_at_30s, live.bid_at_60s, live.bid_at_90s, live.bid_at_120s,
                1 if live.went_green else 0,
                1 if live.target_touched else 0, live.target_touch_count,
                live.target_first_touched_at, live.target_total_visible_seconds,
                live.exit_fill_detected_by or "rest",
                drift["profit_delta_cents"], drift["expectancy_delta_cents"],
                drift["entry_price_drift_cents"], drift["exit_price_drift_cents"],
                drift["total_execution_drift_cents"],
                drift["profit_capture_ratio"], drift["expectancy_capture_ratio"],
                entry_slippage, exit_slippage, total_slippage,
                proj_path_pnl, actual_path_pnl, live_decay,
                ep_bucket, sp_entry_bucket, sp_exit_bucket,
                qa_entry_bucket, qa_exit_bucket,
                tr_bucket, fc_bucket, ot_bucket,
                live.live_trade_id,
            ),
        )
        self._write_phase(live.live_trade_id, "FINALIZED")
        if not live.exit_accounting_verified:
            self._record_guardrail(
                "accounting_unverified", live.market_ticker, live.side,
                f"trade #{live.live_trade_id}: completed flat but restart baseline "
                f"could not be verified; actual PnL/drift left NULL",
            )
        self._record_order_event(
            live, "exit_filled", action="sell",
            requested_count=live.filled_contracts, filled_count=exit_count,
            avg_fill_price=actual_exit_price, order_id=live.exit_order_id,
        )
        logger.warning(
            "live COMPLETE #%d | %s %s | filled=%d exit=%d | proj=%s act=%s delta=%s capture=%s",
            live.live_trade_id, live.market_ticker, live.side,
            live.filled_contracts, exit_count,
            _fmt(projected_profit), _fmt(actual_profit),
            _fmt(drift["profit_delta_cents"]), _fmt(drift["profit_capture_ratio"]),
        )
        self._active.pop(live.contract_id, None)
        self._maybe_clear_flatten_only()

        # Automatic pause check on every completed live trade.
        try:
            self._evaluate_and_maybe_pause()
        except Exception as exc:
            logger.error("pause evaluation failed: %s", exc, exc_info=True)

    def _maybe_clear_flatten_only(self) -> None:
        """Lift flatten-only mode once no rolled-over/recovered exposure remains."""
        if not self._flatten_only:
            return
        if any(l.flattening for l in self._active.values()):
            return
        self._flatten_only = False
        self._unresolved_recovery = False
        logger.warning("live flatten-only mode cleared — exposure resolved")

    def _close_out_on_rollover(self, live: _ActiveLive, captured_at: datetime) -> None:
        """
        Handle a trade whose market is rolling over.

        - No real position yet (PENDING_ENTRY, 0 filled): cancel and abandon.
        - Real filled position: DO NOT drop it.  Switch to flatten-only
          management (it stays in self._active, flagged ``flattening``) so it is
          driven to flat by _advance_flattening on subsequent ticks, and block
          new entries until the exposure is resolved.
        """
        remaining = order_remaining(live.filled_contracts, live.exit_filled_contracts)

        if live.status == "PENDING_ENTRY" and live.filled_contracts == 0:
            if live.entry_order_id and not live.entry_cancel_requested:
                try:
                    self._client.cancel_order(live.entry_order_id)
                except KalshiTradingError:
                    pass
            execute_query(
                "UPDATE momentum_live_trades SET status='CANCELED' WHERE id=%s",
                (live.live_trade_id,),
            )
            self._write_phase(live.live_trade_id, "CANCELED")
            self._record_order_event(
                live, "entry_canceled", action="buy",
                detail="market rollover (unfilled)", order_id=live.entry_order_id,
            )
            self._active.pop(live.contract_id, None)
            return

        # Real (or possibly real) exposure — keep managing until flat.
        live.flattening = True
        live.exit_reason = live.exit_reason or EXIT_FIXED_TIME
        self._flatten_only = True
        self._unresolved_recovery = True
        execute_query(
            "UPDATE momentum_live_trades SET status='PENDING_EXIT', exit_reason=%s WHERE id=%s",
            (live.exit_reason, live.live_trade_id),
        )
        self._record_guardrail(
            "rollover_open_position", live.market_ticker, live.side,
            f"market rolled over with live trade #{live.live_trade_id} "
            f"filled={live.filled_contracts} remaining={remaining} — flattening",
        )
        # Attempt an immediate flatten at the last bid we saw on this market.
        if remaining >= 1 and live.last_bid is not None and not live.exit_order_id:
            try:
                self._submit_exit(live, live.last_bid, captured_at, live.exit_reason, remaining)
            except KalshiTradingError as exc:
                logger.error("rollover flatten submit failed #%d: %s", live.live_trade_id, exc)
        # NOTE: trade intentionally REMAINS in self._active (flattening).

    # ── Projected stats + pause ───────────────────────────────────────────────

    def _load_projected_stats(self) -> Optional[dict]:
        """Rolling shadow stats (most recent COMPLETE shadow trades) for Kelly."""
        rows = fetch_all(
            """
            SELECT net_pnl_cents FROM momentum_shadow_trades
            WHERE status='COMPLETE' AND net_pnl_cents IS NOT NULL
            ORDER BY signal_at DESC
            LIMIT %s
            """,
            (config.MOMENTUM_LIVE_PROJECTED_WINDOW,),
        )
        pnls = [float(r["net_pnl_cents"]) for r in rows]
        return summarize_pnls(pnls)

    def _load_pause_windows(self) -> dict[int, dict]:
        """
        Paired projected-vs-actual stats over rolling windows of completed live
        trades.  Only rows with BOTH projected and actual profit are considered,
        so each window compares the same trades (e.g. shadow-unexecutable or
        live-flattened rows with a NULL leg are excluded).
        """
        max_w = max(config.LIVE_PAUSE_WINDOWS)
        rows = fetch_all(
            """
            SELECT projected_profit_cents, actual_profit_cents
            FROM momentum_live_trades
            WHERE status='COMPLETE'
              AND projected_profit_cents IS NOT NULL
              AND actual_profit_cents IS NOT NULL
            ORDER BY signal_at DESC
            LIMIT %s
            """,
            (max_w,),
        )
        pairs = [
            (float(r["projected_profit_cents"]), float(r["actual_profit_cents"]))
            for r in rows
        ]
        return build_pause_windows(pairs, config.LIVE_PAUSE_WINDOWS)

    def _evaluate_and_maybe_pause(self) -> None:
        if self._is_paused():
            return
        windows = self._load_pause_windows()
        breach = evaluate_pause(
            windows,
            min_trades=config.LIVE_PAUSE_MIN_TRADES,
            win_rate_gap_pct=config.LIVE_PAUSE_WIN_RATE_GAP_PCT,
            expectancy_gap=config.LIVE_PAUSE_EXPECTANCY_GAP_CENTS,
            profit_factor_gap=config.LIVE_PAUSE_PROFIT_FACTOR_GAP,
        )
        if breach is None:
            return
        reason = (
            f"window={breach['window']} n={breach['n']}: "
            + "; ".join(breach["breaches"])
        )
        import json
        now = datetime.now(timezone.utc)
        execute_query(
            "UPDATE momentum_live_pause_state SET is_paused=1, reason=%s, "
            "metrics_json=%s, paused_at=%s WHERE id=1",
            (reason[:480], json.dumps(breach, default=str), now),
        )
        self._record_guardrail(
            "paused", None, None, reason, metrics=breach,
        )
        logger.error(
            "LIVE TRADING AUTO-PAUSED — %s  (manual unpause required: "
            "python scripts/momentum_live_report.py --unpause \"reviewed\")",
            reason,
        )

    def _is_paused(self) -> bool:
        row = fetch_one("SELECT is_paused FROM momentum_live_pause_state WHERE id=1")
        return bool(row and int(row["is_paused"]) == 1)

    def _todays_realized_dollars(self) -> float:
        row = fetch_one(
            """
            SELECT COALESCE(SUM(actual_profit_dollars), 0) AS pnl
            FROM momentum_live_trades
            WHERE status='COMPLETE'
              AND actual_profit_dollars IS NOT NULL
              AND exit_at >= UTC_DATE()
            """
        )
        return float(row["pnl"]) if row and row["pnl"] is not None else 0.0

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _record_order_event(
        self, live: _ActiveLive, event_type: str, *,
        action: Optional[str] = None, requested_count: Optional[int] = None,
        filled_count: Optional[int] = None, limit_price: Optional[float] = None,
        avg_fill_price: Optional[float] = None, order_id: Optional[str] = None,
        client_order_id: Optional[str] = None, detail: Optional[str] = None,
        raw: Optional[dict] = None,
    ) -> None:
        import json
        try:
            execute_query(
                """
                INSERT INTO momentum_live_order_events (
                    live_trade_id, market_ticker, side, event_type,
                    order_id, client_order_id, action,
                    requested_count, filled_count, limit_price, avg_fill_price,
                    detail, raw_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    live.live_trade_id, live.market_ticker, live.side, event_type,
                    order_id, client_order_id, action,
                    requested_count, filled_count, limit_price, avg_fill_price,
                    detail, json.dumps(raw, default=str) if raw else None,
                ),
            )
        except Exception as exc:
            logger.warning("failed to record order event %s: %s", event_type, exc)

    def _record_guardrail(
        self, event_type: str, market_ticker: Optional[str],
        side: Optional[str], reason: str, *, metrics: Optional[dict] = None,
    ) -> None:
        import json
        try:
            execute_query(
                """
                INSERT INTO momentum_live_guardrail_events
                    (event_type, market_ticker, side, reason, metrics_json)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    event_type, market_ticker, side, reason[:480],
                    json.dumps(metrics, default=str) if metrics else None,
                ),
            )
        except Exception as exc:
            logger.warning("failed to record guardrail %s: %s", event_type, exc)

    def _known_order_keys(self, trade_id: int, row: dict) -> tuple[set[str], set[str]]:
        """Durable order keys already attributed to this trade."""
        order_ids: set[str] = set()
        client_order_ids: set[str] = set()
        for key in ("entry_order_id", "exit_order_id"):
            value = str(row.get(key) or "").strip()
            if value:
                order_ids.add(value)
        for key in ("entry_client_order_id", "exit_client_order_id"):
            value = str(row.get(key) or "").strip()
            if value:
                client_order_ids.add(value)
        try:
            events = fetch_all(
                "SELECT order_id, client_order_id FROM momentum_live_order_events "
                "WHERE live_trade_id=%s",
                (trade_id,),
            )
        except Exception:
            events = []
        for event in events:
            order_id = str(event.get("order_id") or "").strip()
            client_order_id = str(event.get("client_order_id") or "").strip()
            if order_id:
                order_ids.add(order_id)
            if client_order_id:
                client_order_ids.add(client_order_id)
        return order_ids, client_order_ids

    def _reconcile_on_startup(self) -> None:
        """
        On restart, reconcile every non-terminal live row against live API state
        instead of merely logging it.  Real positions are rehydrated into
        flatten-only management; stray orders are cancelled; already-flat rows
        are marked reconciled.  Any unresolved exposure latches flatten-only so
        no new entries open until it is cleared.
        """
        try:
            rows = fetch_all(
                "SELECT id, market_id, contract_id, market_ticker, side, "
                "requested_contracts, filled_contracts, "
                "exit_filled_contracts, exit_value_cents, exit_fees_total, "
                "projected_entry_ask, actual_entry_price, actual_entry_fees, "
                "signal_at, entry_order_id, entry_client_order_id, "
                "exit_order_id, exit_client_order_id, status "
                "FROM momentum_live_trades "
                "WHERE status IN ('PENDING_ENTRY','ACTIVE','PENDING_EXIT')"
            )
        except Exception as exc:
            logger.info(
                "MomentumLiveTrader: startup reconcile skipped (table may not "
                "exist — run scripts/migrate_add_momentum_live.py): %s", exc,
            )
            return
        if not rows:
            return
        logger.warning(
            "live startup reconciliation: %d non-terminal row(s) to resolve", len(rows)
        )
        for r in rows:
            try:
                self._reconcile_one(r)
            except Exception as exc:
                # Unknown state — fail safe: block new entries.
                self._flatten_only = True
                self._unresolved_recovery = True
                logger.error("reconcile failed for #%s: %s", r["id"], exc, exc_info=True)
                self._record_guardrail(
                    "reconcile_error", r.get("market_ticker"), r.get("side"),
                    f"reconcile failed for trade #{r['id']}: {exc}",
                )

    def _reconcile_one(self, r: dict) -> None:
        tid = int(r["id"])
        ticker = r["market_ticker"]
        side = r["side"]

        # Without an API client we cannot verify exposure — fail safe.
        if not self._armed or self._client is None:
            self._flatten_only = True
            self._unresolved_recovery = True
            logger.warning(
                "live ORPHAN #%d status=%s — no API to reconcile; blocking new entries",
                tid, r["status"],
            )
            self._record_guardrail(
                "orphan_unreconciled", ticker, side,
                f"trade #{tid} was {r['status']} at restart; no API client to reconcile",
            )
            return

        positions = self._client.get_positions(ticker=ticker)
        pos_qty = net_position_for_side(positions, ticker, side)
        ticker_open_orders = [
            o for o in self._client.get_orders(ticker=ticker) if is_order_open(o)
        ]
        known_order_ids, known_client_order_ids = self._known_order_keys(tid, r)
        open_orders, unknown_orders = partition_trade_orders(
            ticker_open_orders, known_order_ids, known_client_order_ids
        )
        if unknown_orders:
            self._flatten_only = True
            self._unresolved_recovery = True
            self._record_guardrail(
                "reconcile_ambiguous_orders", ticker, side,
                f"trade #{tid}: found {len(unknown_orders)} open order(s) on ticker "
                f"that are not attributable to this strategy trade; manual review required",
            )
            logger.error(
                "live ORPHAN #%d — found %d unowned open order(s) on %s; manual review",
                tid, len(unknown_orders), ticker,
            )
            return

        decision = reconcile_decision(pos_qty, len(open_orders) > 0)

        if decision == "rehydrate_flatten":
            self._rehydrate_flatten(r, pos_qty, open_orders)
        elif decision == "cancel_then_close":
            for o in open_orders:
                try:
                    self._client.cancel_order(str(o.get("order_id") or o.get("id") or ""))
                except KalshiTradingError:
                    pass
            execute_query(
                "UPDATE momentum_live_trades SET status='CANCELED', "
                "exit_reason='reconciled_no_position' WHERE id=%s",
                (tid,),
            )
            self._write_phase(tid, "CANCELED")
            self._record_guardrail(
                "reconciled_canceled", ticker, side,
                f"trade #{tid}: no position; cancelled {len(open_orders)} stray order(s)",
            )
            logger.warning("live RECONCILED (cancelled stray orders) #%d", tid)
        else:  # mark_reconciled — already flat
            execute_query(
                "UPDATE momentum_live_trades SET status='RECONCILED', "
                "exit_at=UTC_TIMESTAMP() WHERE id=%s",
                (tid,),
            )
            self._write_phase(tid, "RECONCILED")
            self._record_guardrail(
                "reconciled_flat", ticker, side,
                f"trade #{tid}: already flat at restart (no position, no open orders)",
            )
            logger.warning("live RECONCILED (already flat) #%d", tid)

    def _rehydrate_flatten(self, r: dict, pos_qty: int, open_orders: list[dict]) -> None:
        """
        Rebuild a real open position into flatten-only management on restart,
        preserving partial-exit history.

        Safety-first sequencing (avoids oversell + preserves accounting):
          1. Cancel ALL pre-crash open orders and CONFIRM them terminal.  If any
             cannot be confirmed terminal, fail safe: latch flatten-only, record
             a guardrail for manual review, and do NOT auto-manage this trade
             (we will not auto-submit while a stale order might still fill).
          2. Re-read the live position AFTER cancels — the authoritative remaining.
          3. Reconstruct quantities with reconstruct_exit_state():
                original_entry (never shrunk to remaining), exit_filled, remaining.
          4. Seed the exit value/fees baseline from the persisted progress when it
             matches the reconstructed exit count, else from the fills history;
             flag a guardrail if neither agrees.
          5. exit_order_ids stays empty (all prior orders cancelled), so the
             baseline holds ALL prior exits and future flattening uses fresh
             orders only — no double-counting, no oversell.
        """
        self._flatten_only = True
        self._unresolved_recovery = True

        tid = int(r["id"])
        ticker = r["market_ticker"]
        side = r["side"]

        # 1. Cancel all pre-crash open orders and confirm terminal.
        not_terminal = []
        for o in open_orders:
            oid = str(o.get("order_id") or o.get("id") or "")
            if not oid:
                continue
            try:
                self._client.cancel_order(oid)
            except KalshiTradingError:
                pass
            if is_order_open(self._safe_get_order(oid)):
                not_terminal.append(oid)
        if not_terminal:
            # Cannot guarantee no further fills — do NOT auto-manage; alert operator.
            self._record_guardrail(
                "orphan_cancel_unconfirmed", ticker, side,
                f"trade #{tid}: {len(not_terminal)} order(s) not confirmed terminal "
                f"after cancel; flatten-only latched, manual review required",
            )
            logger.error(
                "live ORPHAN #%d — cancel not confirmed terminal; manual review", tid
            )
            return

        # 2. Authoritative remaining AFTER cancels.
        positions = self._client.get_positions(ticker=ticker)
        remaining_pos = net_position_for_side(positions, ticker, side)

        # 3. Quantity reconstruction (original entry preserved).
        qty = reconstruct_exit_state(
            db_entry_filled=int(r.get("filled_contracts") or 0),
            db_exit_filled=int(r.get("exit_filled_contracts") or 0),
            position_qty=remaining_pos,
        )
        original_entry = qty["original_entry"]
        exit_filled = qty["exit_filled"]
        remaining = qty["remaining"]

        # 4. Exit value/fees baseline.
        persisted_count = int(r.get("exit_filled_contracts") or 0)
        rc, ravg, rfees = self._reconstruct_exit_from_fills(ticker, side)
        baseline = resolve_recovery_baseline(
            reconstructed_exit_count=exit_filled,
            persisted_count=persisted_count,
            persisted_value=(
                float(r["exit_value_cents"]) if r.get("exit_value_cents") is not None else None
            ),
            persisted_fees=(
                float(r["exit_fees_total"]) if r.get("exit_fees_total") is not None else None
            ),
            fills_count=rc,
            fills_avg=ravg,
            fills_fees=rfees,
        )
        base_value = baseline["value"]
        base_fees = baseline["fees"]
        accounting_verified = bool(baseline["verified"])
        if not accounting_verified:
            self._record_guardrail(
                "exit_reconstruction_mismatch", ticker, side,
                f"trade #{tid}: reconstructed exit count from position={exit_filled} "
                f"did not match persisted ({persisted_count}) or fills ({rc}); "
                f"flatten will continue but actual PnL/drift will stay unverified",
            )

        signal_at = r.get("signal_at") or datetime.now(timezone.utc)
        signal_ts = signal_at.timestamp() if hasattr(signal_at, "timestamp") else 0.0
        entry_ask = (
            float(r["projected_entry_ask"])
            if r.get("projected_entry_ask") is not None else 0.0
        )
        live = _ActiveLive(
            live_trade_id=tid,
            market_id=int(r["market_id"]),
            contract_id=int(r["contract_id"]),
            market_ticker=ticker,
            side=side,
            signal_at=signal_at,
            signal_ts=signal_ts,
            entry_ask=entry_ask,
            horizon_ts=0.0,                          # already past — flatten now
            requested_contracts=int(r.get("requested_contracts") or original_entry),
            status="PENDING_EXIT",
            filled_contracts=original_entry,         # ORIGINAL size preserved
            actual_entry_price=(
                float(r["actual_entry_price"])
                if r.get("actual_entry_price") is not None else None
            ),
            actual_entry_fees=(
                float(r["actual_entry_fees"])
                if r.get("actual_entry_fees") is not None else None
            ),
            exit_filled_contracts=exit_filled,
            exit_value=base_value,
            exit_fees=base_fees,
            exit_accounting_verified=accounting_verified,
            exit_baseline_count=exit_filled,         # prior exits held as baseline
            exit_baseline_value=base_value,
            exit_baseline_fees=base_fees,
            flattening=True,
            exit_reason=EXIT_FIXED_TIME,
            # Best-effort flatten price on restart (no live quote yet) — operator
            # should verify; only seeds an initial limit when needed.
            last_bid=entry_ask or None,
        )

        self._active[live.contract_id] = live
        # 5. Persist reconstructed progress. NOTE: filled_contracts (original
        # entry) is repaired upward only — never shrunk to the remaining size.
        execute_query(
            "UPDATE momentum_live_trades SET status='PENDING_EXIT', "
            "filled_contracts=%s, exit_filled_contracts=%s, "
            "exit_value_cents=%s, exit_fees_total=%s WHERE id=%s",
            (
                original_entry,
                exit_filled,
                round(base_value, 6) if accounting_verified else None,
                base_fees if accounting_verified else None,
                tid,
            ),
        )
        self._record_guardrail(
            "recovered_position", ticker, side,
            f"trade #{tid}: rehydrated original_entry={original_entry} "
            f"exit_filled={exit_filled} remaining={remaining} into flatten-only",
        )
        logger.warning(
            "live RECOVERED position #%d | %s %s | original=%d sold=%d remaining=%d "
            "-> flatten-only", tid, ticker, side, original_entry, exit_filled, remaining,
        )


# ── small format helpers ────────────────────────────────────────────────────

def _safe(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 6)


def _fmt(v: Optional[float]) -> str:
    return f"{v:+.4f}" if v is not None else "n/a"
