#!/usr/bin/env python3
"""
contract_value_bounce_backtest.py — Lookahead-safe replay for the
contract_value_bounce_scalp/v1 WATCH-ONLY hypothesis.

Hypothesis
----------
A losing contract that has already sold down and then BOUNCED upward in
contract value can be scalped for a short +3c–+6c gain after realistic
spread and slippage.  The entry trigger is CONTRACT-LED: the losing-side
contract must already be ≥ +2c off its session low and not currently falling.
BTC is recorded as context but is NOT the primary driver.

This is intentionally distinct from:
  - cheap_losing_contract_reversal_trail / late_reversal : fire while contract
    is still falling or at unknown state; no confirmed bounce required.
  - post_move_continuation_scalp : fires on the WINNING side after a BTC move.

Design
------
Drives the REAL `contract_value_bounce_scalp` (v1) strategy over reconstructed
snapshots (BTC ticks + contract quotes), tracking each side's contract-mid
history so bounce_from_low is evaluated exactly as live.  For every watch-only
signal it simulates four exit tests:

    cvbs_test_a : take_profit +0.03, stop_loss -0.03, timeout 30s
    cvbs_test_b : take_profit +0.04, stop_loss -0.03, timeout 45s
    cvbs_test_c : take_profit +0.05, stop_loss -0.04, timeout 60s
    cvbs_test_d : take_profit +0.06, stop_loss -0.04, timeout 60s

No lookahead
------------
At snapshot ts t the strategy sees only ticks/quotes with recorded_at <= t and
contract-mid history <= t; the exit simulator only walks the bid path forward.

Fill model (NOT mid):  entry = ask + slippage,  exit = bid - slippage.

PAPER-ONLY / BACKTEST-ONLY RESEARCH.  Never trades, never places an order, never
enables LIVE_TRADING_ENABLED.  Writes only to contract_value_bounce_backtest_*
tables, kept entirely separate from live paper_trades.

Usage
-----
    python scripts/migrate_add_contract_value_bounce.py   # once, first
    python scripts/contract_value_bounce_backtest.py \\
        [--slippage realistic] \\
        [--start 2026-05-01] [--end 2026-06-01] \\
        [--limit-markets N] [--cooldown-seconds 30] \\
        [--timezone America/New_York] [--notes "..."]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import SIGNAL_TIMEZONE, SLIPPAGE_MODE
from app.db import execute_query, fetch_all, insert_and_get_id
from app.exit_simulator import (
    CVBS_PROFILES,
    CVBS_PROFILE_NAMES,
    PathPoint,
    simulate_exit_fixed_abs,
)
from app.features import Tick, build_time_features, rolling_std, volatility_regime
from app.followthrough import spread_bucket
from app.strategies import Signal, contract_value_bounce_scalp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("contract_value_bounce_backtest")

_SLIPPAGE: dict[str, tuple[float, float]] = {
    "optimistic": (0.00, 0.00),
    "realistic":  (0.01, 0.01),
    "harsh":      (0.02, 0.02),
}

_RULE_NAME    = "contract_value_bounce_scalp"
_RULE_VERSION = "v1"

_DEFAULT_COOLDOWN_S = 30.0
_WINDOW_KEEP_S      = 300.0
_MIN_TICKS          = 2


def _null_reversal_prob(**_kwargs: Any) -> dict[str, Any]:
    return {
        "similar_sample_count":       0,
        "p_plus_2c_before_minus_2c":  None,
        "p_plus_3c_before_minus_2c":  None,
        "p_plus_4c_before_minus_3c":  None,
        "confidence_label":           "insufficient_data",
        "match_level":                "none",
    }


# ── Reconstructed snapshot ──────────────────────────────────────────────────────

@dataclass
class _Snapshot:
    ts:             float
    recorded_at:    datetime
    time_remaining: float
    contract_age:   float
    quotes:         dict[str, dict]
    btc_price:      float = 0.0
    vol_regime:     str   = "unknown"
    win_lo:         int   = 0
    win_hi:         int   = 0


@dataclass
class _RunCounters:
    n_markets:    int = 0
    n_snapshots:  int = 0
    n_signals:    int = 0
    n_test_rows:  int = 0
    data_start: Optional[datetime] = None
    data_end:   Optional[datetime] = None


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


# ── Data loading ────────────────────────────────────────────────────────────────

def _load_markets(
    start: Optional[str], end: Optional[str], limit: Optional[int]
) -> list[dict]:
    sql = """
        SELECT m.market_ticker, m.target_price, m.open_time, m.close_time
        FROM markets m
        WHERE EXISTS (
            SELECT 1 FROM contract_ticks c
            WHERE c.market_ticker = m.market_ticker
              {start_clause}
              {end_clause}
        )
        ORDER BY m.open_time ASC, m.market_ticker ASC
    """
    params: list[Any] = []
    start_clause = end_clause = ""
    if start:
        start_clause = "AND c.recorded_at >= %s"
        params.append(start)
    if end:
        end_clause = "AND c.recorded_at < %s"
        params.append(end)
    sql = sql.format(start_clause=start_clause, end_clause=end_clause)
    if limit:
        sql += "\nLIMIT %s"
        params.append(int(limit))
    return fetch_all(sql, tuple(params))


def _load_btc_ticks(
    ticker: str, start: Optional[str], end: Optional[str]
) -> list[Tick]:
    sql = ("SELECT btc_price, recorded_at FROM btc_ticks "
           "WHERE market_ticker = %s")
    params: list[Any] = [ticker]
    if start:
        sql += " AND recorded_at >= %s"; params.append(start)
    if end:
        sql += " AND recorded_at < %s"; params.append(end)
    sql += " ORDER BY recorded_at ASC"
    rows = fetch_all(sql, tuple(params))
    return [Tick(price=float(r["btc_price"]), ts=r["recorded_at"].timestamp())
            for r in rows]


def _load_snapshots(
    ticker: str, start: Optional[str], end: Optional[str]
) -> list[_Snapshot]:
    sql = """
        SELECT side, bid_price, ask_price, mid_price, spread,
               time_remaining_seconds, contract_age_seconds, recorded_at
        FROM contract_ticks
        WHERE market_ticker = %s
    """
    params: list[Any] = [ticker]
    if start:
        sql += " AND recorded_at >= %s"; params.append(start)
    if end:
        sql += " AND recorded_at < %s"; params.append(end)
    sql += " ORDER BY recorded_at ASC, side ASC"
    rows = fetch_all(sql, tuple(params))

    by_ts: dict[datetime, _Snapshot] = {}
    for r in rows:
        ra = r["recorded_at"]
        snap = by_ts.get(ra)
        if snap is None:
            snap = _Snapshot(
                ts=ra.timestamp(), recorded_at=ra,
                time_remaining=float(r["time_remaining_seconds"] or 0),
                contract_age=float(r["contract_age_seconds"] or 0),
                quotes={},
            )
            by_ts[ra] = snap
        snap.quotes[r["side"]] = {
            "bid_price": _f(r["bid_price"]),
            "ask_price": _f(r["ask_price"]),
            "mid_price": _f(r["mid_price"]),
            "spread":    _f(r["spread"]),
        }
    return [by_ts[k] for k in sorted(by_ts)]


# ── Per-market replay ────────────────────────────────────────────────────────────

def _annotate_snapshots(snaps: list[_Snapshot], btc: list[Tick]) -> None:
    lo = hi = 0
    n = len(btc)
    for s in snaps:
        while hi < n and btc[hi].ts <= s.ts:
            hi += 1
        cutoff = s.ts - _WINDOW_KEEP_S
        while lo < hi and btc[lo].ts < cutoff:
            lo += 1
        s.win_lo, s.win_hi = lo, hi
        if hi > 0:
            s.btc_price = btc[hi - 1].price
        s.vol_regime = volatility_regime(rolling_std(btc[lo:hi], 60.0))


def _replay_market(
    market: dict,
    btc: list[Tick],
    snaps: list[_Snapshot],
    *,
    entry_add: float,
    exit_sub: float,
    slippage_mode: str,
    cooldown_s: float,
    timezone_name: str,
    run_id: int,
    counters: _RunCounters,
) -> None:
    ticker       = market["market_ticker"]
    target_price = _f(market.get("target_price"))
    if target_price is None or len(btc) < _MIN_TICKS or not snaps:
        return

    _annotate_snapshots(snaps, btc)

    # Accumulate per-side contract-mid history (used for bounce_from_low, etc.)
    hist: dict[str, list[Tick]] = {"YES": [], "NO": []}
    # Per-side cooldown to avoid re-entering the same setup repeatedly.
    blocked_until_ts: dict[str, float] = {"YES": float("-inf"), "NO": float("-inf")}

    for i, s in enumerate(snaps):
        for side in ("YES", "NO"):
            mid = s.quotes.get(side, {}).get("mid_price")
            if mid is not None:
                hist[side].append(Tick(price=float(mid), ts=s.ts))

        counters.n_snapshots += 1
        if counters.data_start is None:
            counters.data_start = s.recorded_at
        counters.data_end = s.recorded_at

        ticks_window = btc[s.win_lo:s.win_hi]
        if len(ticks_window) < _MIN_TICKS:
            continue

        sig = contract_value_bounce_scalp(
            ticks                  = ticks_window,
            market_ticker          = ticker,
            btc_price              = s.btc_price,
            target_price           = target_price,
            contract_age_seconds   = s.contract_age,
            time_remaining_seconds = s.time_remaining,
            contract_prices        = s.quotes,
            contract_history       = {k: list(v) for k, v in hist.items()},
            reversal_prob_fn       = _null_reversal_prob,
        )
        if sig is None:
            continue

        if s.ts < blocked_until_ts.get(sig.side, float("-inf")):
            continue

        latest_exit_ts = _record_signal(
            sig, s, i, snaps,
            entry_add=entry_add, exit_sub=exit_sub, slippage_mode=slippage_mode,
            timezone_name=timezone_name, run_id=run_id, counters=counters,
        )
        blocked_until_ts[sig.side] = max(latest_exit_ts, s.ts + cooldown_s)


def _record_signal(
    sig: Signal,
    entry_snap: _Snapshot,
    entry_idx: int,
    snaps: list[_Snapshot],
    *,
    entry_add: float,
    exit_sub: float,
    slippage_mode: str,
    timezone_name: str,
    run_id: int,
    counters: _RunCounters,
) -> float:
    """
    Simulate all four exit tests for one signal and write rows to DB.
    Returns the latest simulated exit timestamp (for cooldown tracking).
    """
    extra = sig.extra or {}

    # ── Entry fill ─────────────────────────────────────────────────────────────
    entry_sim = round(sig.ask_price + entry_add, 4)
    entry_bid = sig.bid_price

    # ── Time-of-day features ──────────────────────────────────────────────────
    tf = build_time_features(entry_snap.recorded_at, tz=timezone_name)
    hour_block = tf.get("hour_block")
    day_name   = tf.get("day_name")

    # ── Build the forward bid path for the losing-side contract ──────────────
    # The forward path is: all snaps AFTER entry_idx, losing-side bid over time.
    losing_side = extra.get("losing_side", sig.side)
    entry_ts    = entry_snap.ts

    path: list[PathPoint] = []
    for s in snaps[entry_idx + 1:]:
        bid = s.quotes.get(losing_side, {}).get("bid_price")
        if bid is None:
            continue
        elapsed = s.ts - entry_ts
        if elapsed <= 0:
            continue
        path.append(PathPoint(
            elapsed        = elapsed,
            bid            = float(bid),
            time_remaining = s.time_remaining,
            vol_regime     = s.vol_regime,
        ))

    latest_exit_ts = entry_snap.ts

    # ── Spread bucket ─────────────────────────────────────────────────────────
    sp_bucket = spread_bucket(sig.spread)

    # ── Simulate each exit test ───────────────────────────────────────────────
    for pname in CVBS_PROFILE_NAMES:
        profile = CVBS_PROFILES[pname]

        if path:
            res = simulate_exit_fixed_abs(
                entry_sim    = entry_sim,
                entry_bid    = float(entry_bid),
                exit_sub     = exit_sub,
                path         = path,
                profile      = profile,
            )
            hit_tp  = res.exit_reason == "take_profit"
            hit_sl  = res.exit_reason in ("stop_loss",)
            timed_out = res.exit_reason in ("timeout", "near_expiry", "end_of_data")
            pnl          = res.pnl
            pnl_pct      = res.pnl_percent
            mfe          = res.max_favorable_excursion
            mae          = res.max_adverse_excursion
            time_to_peak = res.time_to_peak
            exit_reason  = res.exit_reason
            n_updates    = res.n_updates
            # time to profit target = elapsed when exit_reason == take_profit
            time_to_tp   = None
            if hit_tp and path:
                # walk path to find the point elapsed
                for pt in path:
                    sim_at_pt = round(float(pt.bid) - exit_sub, 4)
                    if sim_at_pt >= entry_sim + (profile.tp_abs or 0):
                        time_to_tp = round(pt.elapsed, 3)
                        break

            # Exit time as datetime
            exit_ts: Optional[datetime] = None
            if path:
                for pt in path[:n_updates]:
                    pass  # last point walked
                exit_offset = path[min(n_updates - 1, len(path) - 1)].elapsed if path else 0
                exit_ts = datetime.fromtimestamp(
                    entry_ts + exit_offset, tz=timezone.utc
                ).replace(tzinfo=None)

            # Track the latest exit for cooldown
            latest_exit_ts = max(latest_exit_ts, entry_ts + (exit_offset if path else 0))
        else:
            # No forward data
            hit_tp = hit_sl = timed_out = False
            pnl = pnl_pct = mfe = mae = None
            time_to_peak = time_to_tp = None
            exit_reason = "end_of_data"
            n_updates = 0
            exit_ts = None

        row: dict[str, Any] = {
            "run_id":         run_id,
            "rule_name":      _RULE_NAME,
            "rule_version":   _RULE_VERSION,
            "market_ticker":  sig.market_ticker,

            "side_bought":    sig.side,
            "winning_side":   extra.get("winning_side", "YES"),
            "losing_contract_ask":             extra.get("losing_contract_ask"),
            "losing_contract_bid":             extra.get("losing_contract_bid"),
            "losing_contract_spread":          extra.get("losing_contract_spread"),
            "losing_contract_low_since_open":  extra.get("losing_contract_low_since_open"),
            "losing_contract_bounce_from_low": extra.get("losing_contract_bounce_from_low"),
            "losing_contract_mom_10s":         extra.get("losing_contract_mom_10s"),
            "price_bucket":                    extra.get("price_bucket"),
            "bounce_bucket":                   extra.get("bounce_bucket"),
            "spread_bucket":                   sp_bucket,
            "simulated_entry_price":           entry_sim,
            "slippage_mode":                   slippage_mode,

            "btc_price":           sig.btc_price,
            "target_price":        sig.target_price,
            "adverse_z_score":     extra.get("adverse_z_score"),
            "raw_momentum_score":  extra.get("raw_momentum_score"),

            "volatility_regime":   extra.get("volatility_regime"),
            "volatility_30s":      sig.volatility_30s,
            "volatility_60s":      sig.volatility_60s,
            "whipsaw_score":       extra.get("whipsaw_score"),

            "market_age_seconds":     int(sig.contract_age_seconds),
            "time_remaining_seconds": int(sig.time_remaining_seconds),
            "entry_time":             entry_snap.recorded_at,
            "hour_block":             hour_block,
            "day_name":               day_name,
            "timezone_used":          timezone_name,

            "exit_test":  pname,
            "tp_abs":     profile.tp_abs,
            "sl_abs":     profile.sl_abs,
            "timeout_s":  profile.timeout_s,

            "hit_take_profit_before_stop": hit_tp,
            "hit_stop_before_take_profit": hit_sl,
            "timed_out":                   timed_out,
            "max_favorable_excursion":     mfe,
            "max_adverse_excursion":       mae,
            "time_to_peak":                time_to_peak,
            "time_to_profit_target":       time_to_tp,
            "simulated_pnl":               pnl,
            "simulated_pnl_percent":       pnl_pct,
            "exit_reason_simulated":       exit_reason,
            "exit_time":                   exit_ts,
            "n_updates":                   n_updates,
        }

        cols = ", ".join(row.keys())
        placeholders = ", ".join(["%s"] * len(row))
        insert_and_get_id(
            f"INSERT INTO contract_value_bounce_backtest_signals ({cols}) VALUES ({placeholders})",
            tuple(row.values()),
        )
        counters.n_test_rows += 1

    counters.n_signals += 1
    return latest_exit_ts


# ── Run orchestrator ─────────────────────────────────────────────────────────────

def run(
    slippage_mode: str  = "realistic",
    start:         Optional[str] = None,
    end:           Optional[str] = None,
    limit_markets: Optional[int] = None,
    cooldown_s:    float = _DEFAULT_COOLDOWN_S,
    timezone_name: str  = SIGNAL_TIMEZONE,
    notes:         Optional[str] = None,
) -> int:
    entry_add, exit_sub = _SLIPPAGE.get(slippage_mode, (0.01, 0.01))
    logger.info(
        "contract_value_bounce_backtest starting | slippage=%s (+%.2f/-%.2f) "
        "start=%s end=%s limit=%s",
        slippage_mode, entry_add, exit_sub, start, end, limit_markets,
    )

    markets = _load_markets(start, end, limit_markets)
    logger.info("Markets to replay: %d", len(markets))
    if not markets:
        logger.warning("No markets found — nothing to do.")
        return 0

    run_id = insert_and_get_id(
        """
        INSERT INTO contract_value_bounce_backtest_runs
            (rule_name, rule_version, slippage_mode, exit_tests, params, timezone_used, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            _RULE_NAME, _RULE_VERSION, slippage_mode,
            json.dumps(CVBS_PROFILE_NAMES),
            json.dumps({"cooldown_s": cooldown_s, "min_ticks": _MIN_TICKS}),
            timezone_name,
            notes,
        ),
    )
    logger.info("Created run_id=%d", run_id)

    counters = _RunCounters()

    for m in markets:
        ticker = m["market_ticker"]
        logger.info("Replaying %s …", ticker)
        btc   = _load_btc_ticks(ticker, start, end)
        snaps = _load_snapshots(ticker, start, end)
        if not snaps:
            continue
        counters.n_markets += 1
        _replay_market(
            m, btc, snaps,
            entry_add     = entry_add,
            exit_sub      = exit_sub,
            slippage_mode = slippage_mode,
            cooldown_s    = cooldown_s,
            timezone_name = timezone_name,
            run_id        = run_id,
            counters      = counters,
        )

    # Finalise the run row with aggregate counts.
    execute_query(
        """
        UPDATE contract_value_bounce_backtest_runs
        SET data_start   = %s,
            data_end     = %s,
            n_markets    = %s,
            n_snapshots  = %s,
            n_signals    = %s,
            n_test_rows  = %s
        WHERE id = %s
        """,
        (
            counters.data_start, counters.data_end,
            counters.n_markets, counters.n_snapshots,
            counters.n_signals, counters.n_test_rows,
            run_id,
        ),
    )

    logger.info(
        "Run %d complete — markets=%d snaps=%d signals=%d test_rows=%d",
        run_id, counters.n_markets, counters.n_snapshots,
        counters.n_signals, counters.n_test_rows,
    )
    return run_id


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Replay contract_value_bounce_scalp/v1 over stored snapshots."
    )
    p.add_argument("--slippage",       default=SLIPPAGE_MODE,
                   choices=list(_SLIPPAGE), help="Slippage model (default: %(default)s)")
    p.add_argument("--start",          default=None, metavar="YYYY-MM-DD",
                   help="Filter snapshots to this start date (inclusive)")
    p.add_argument("--end",            default=None, metavar="YYYY-MM-DD",
                   help="Filter snapshots to this end date (exclusive)")
    p.add_argument("--limit-markets",  default=None, type=int, metavar="N",
                   help="Process at most N markets (smoke-test mode)")
    p.add_argument("--cooldown-seconds", default=_DEFAULT_COOLDOWN_S, type=float,
                   help="Minimum seconds between signals on the same side (default: %(default)s)")
    p.add_argument("--timezone",       default=SIGNAL_TIMEZONE, metavar="TZ",
                   help="Timezone for hour_block / day_name (default: %(default)s)")
    p.add_argument("--notes",          default=None,
                   help="Free-text notes stored on the run row")
    args = p.parse_args()

    run(
        slippage_mode = args.slippage,
        start         = args.start,
        end           = args.end,
        limit_markets = args.limit_markets,
        cooldown_s    = args.cooldown_seconds,
        timezone_name = args.timezone,
        notes         = args.notes,
    )


if __name__ == "__main__":
    main()
