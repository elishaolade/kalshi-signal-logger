#!/usr/bin/env python3
"""
post_move_continuation_backtest.py — Lookahead-safe replay for the
post_move_continuation_scalp/v1 WATCH-ONLY hypothesis.

Hypothesis
----------
A contract is profitable to scalp if it begins trending UPWARD and the move
continues long enough that the exit bid clears the entry ask after spread and
slippage.  The goal is NOT to predict expiry — only to capture a short
continuation move once the contract starts repricing upward.

Design
------
Drives the REAL `post_move_continuation_scalp` (v1) strategy over reconstructed
snapshots (BTC ticks + contract quotes), accumulating each side's contract-mid
history so the strategy's contract_price_change_* gates are evaluated exactly as
live.  For every watch-only signal it records the full feature context, and for
scalp candidates (ask < 0.85) it simulates four exit tests:

    test_a : take_profit +0.03, stop_loss -0.02, timeout 30s
    test_b : take_profit +0.04, stop_loss -0.03, timeout 45s
    test_c : take_profit +0.05, stop_loss -0.03, timeout 60s
    test_d : take_profit +0.06, stop_loss -0.04, timeout 60s

0.85+ signals (price_bucket = no_scalp_or_expiry_hold_only) are logged as
watch-only CONTEXT only (exit_test = 'context', outcomes NULL) — never scalped.

No lookahead
------------
At snapshot ts t the strategy sees only ticks/quotes with recorded_at <= t and
contract-mid history <= t; the exit simulator only walks the bid path forward.

Fill model (NOT mid):  entry = ask + slippage,  exit = bid - slippage.

PAPER-ONLY / BACKTEST-ONLY RESEARCH.  Never trades, never places an order, never
enables LIVE_TRADING_ENABLED.  Writes only to post_move_continuation_* tables,
kept entirely separate from live paper_trades.

Usage
-----
    python scripts/migrate_add_post_move_continuation.py   # once, first
    python scripts/post_move_continuation_backtest.py \
        [--slippage realistic] \
        [--start 2026-05-01] [--end 2026-06-01] \
        [--limit-markets N] [--cooldown-seconds 30] \
        [--timezone America/New_York] [--notes "..."]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import SIGNAL_TIMEZONE, SLIPPAGE_MODE
from app.db import execute_query, fetch_all, insert_and_get_id
from app.exit_simulator import (
    POST_MOVE_PROFILES,
    POST_MOVE_PROFILE_NAMES,
    PathPoint,
    simulate_exit_fixed_abs,
)
from app.features import Tick, build_time_features, rolling_std, volatility_regime
from app.followthrough import spread_bucket
from app.strategies import Signal, post_move_continuation_scalp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("post_move_continuation_backtest")

_SLIPPAGE: dict[str, tuple[float, float]] = {
    "optimistic": (0.00, 0.00),
    "realistic":  (0.01, 0.01),
    "harsh":      (0.02, 0.02),
}

_RULE_NAME    = "post_move_continuation_scalp"
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
    n_markets:          int = 0
    n_snapshots:        int = 0
    n_signals:          int = 0
    n_scalp_candidates: int = 0
    n_context_signals:  int = 0
    n_test_rows:        int = 0
    data_start: Optional[datetime] = None
    data_end:   Optional[datetime] = None


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


# ── Data loading ────────────────────────────────────────────────────────────────

def _load_markets(start: Optional[str], end: Optional[str], limit: Optional[int]) -> list[dict]:
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


def _load_btc_ticks(ticker: str, start: Optional[str], end: Optional[str]) -> list[Tick]:
    sql = ("SELECT btc_price, recorded_at FROM btc_ticks "
           "WHERE market_ticker = %s")
    params: list[Any] = [ticker]
    if start:
        sql += " AND recorded_at >= %s"; params.append(start)
    if end:
        sql += " AND recorded_at < %s"; params.append(end)
    sql += " ORDER BY recorded_at ASC"
    rows = fetch_all(sql, tuple(params))
    return [Tick(price=float(r["btc_price"]), ts=r["recorded_at"].timestamp()) for r in rows]


def _load_snapshots(ticker: str, start: Optional[str], end: Optional[str]) -> list[_Snapshot]:
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
    target_price = float(market["target_price"]) if market["target_price"] is not None else None
    if target_price is None or len(btc) < _MIN_TICKS or not snaps:
        return

    _annotate_snapshots(snaps, btc)

    hist: dict[str, list[Tick]] = {"YES": [], "NO": []}
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

        sig = post_move_continuation_scalp(
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

        # Cooldown / no-duplicate per market+side+rule.
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
    side  = sig.side
    quote = entry_snap.quotes.get(side, {})
    ask   = quote.get("ask_price")
    bid   = quote.get("bid_price")
    if not ask or ask <= 0 or bid is None:
        return entry_snap.ts

    extra = sig.extra or {}
    scalp_candidate = bool(extra.get("scalp_candidate", True))

    entry_sim = round(float(ask) + entry_add, 4)
    entry_bid = round(float(bid), 4)

    tf = build_time_features(entry_snap.recorded_at, tz=timezone_name)

    counters.n_signals += 1
    seq = counters.n_signals

    common = dict(
        run_id=run_id, seq=seq, sig=sig, extra=extra, snap=entry_snap, tf=tf,
        slippage_mode=slippage_mode, entry_sim=entry_sim, entry_bid=entry_bid,
        scalp_candidate=scalp_candidate, timezone_used=timezone_name,
    )

    # 0.85+ context only — log a single row, no exit simulation.
    if not scalp_candidate:
        counters.n_context_signals += 1
        _insert_row(**common, exit_test="context", profile=None, res=None,
                    exit_dt=None, time_to_profit_target=None)
        return entry_snap.ts

    counters.n_scalp_candidates += 1

    # Forward path: every later snapshot's bid for the bought side.
    path: list[PathPoint] = []
    for j in range(entry_idx + 1, len(snaps)):
        sj = snaps[j]
        b  = sj.quotes.get(side, {}).get("bid_price")
        if b is None:
            continue
        path.append(PathPoint(
            elapsed=sj.ts - entry_snap.ts, bid=float(b),
            time_remaining=sj.time_remaining, vol_regime=sj.vol_regime,
        ))

    latest_exit_ts = entry_snap.ts
    for name in POST_MOVE_PROFILE_NAMES:
        profile = POST_MOVE_PROFILES[name]
        res = simulate_exit_fixed_abs(
            entry_sim=entry_sim, entry_bid=entry_bid, exit_sub=exit_sub,
            path=path, profile=profile,
        )
        exit_ts, exit_dt = _exit_timestamp(res, path, entry_snap)
        latest_exit_ts = max(latest_exit_ts, exit_ts)
        # time_to_profit_target: when the +tp move was first reached (= exit
        # elapsed iff this exit was a take_profit; else target was never hit).
        ttp = round(exit_ts - entry_snap.ts, 3) if res.exit_reason == "take_profit" else None

        _insert_row(**common, exit_test=name, profile=profile, res=res,
                    exit_dt=exit_dt, time_to_profit_target=ttp)
        counters.n_test_rows += 1

    return latest_exit_ts


def _exit_timestamp(res, path: list[PathPoint], entry_snap: _Snapshot):
    if not path:
        return entry_snap.ts, entry_snap.recorded_at
    idx = min(max(res.n_updates - 1, 0), len(path) - 1)
    elapsed = path[idx].elapsed
    ts = entry_snap.ts + elapsed
    dt = datetime.fromtimestamp(ts, tz=entry_snap.recorded_at.tzinfo)
    return ts, dt


# ── Persistence ──────────────────────────────────────────────────────────────────

def _insert_run(*, slippage_mode: str, params: dict, timezone_name: str) -> int:
    tests_json = json.dumps([
        {"name": p.name, "tp_abs": p.tp_abs, "sl_abs": p.sl_abs, "timeout_s": p.timeout_s}
        for p in (POST_MOVE_PROFILES[n] for n in POST_MOVE_PROFILE_NAMES)
    ])
    return insert_and_get_id(
        """
        INSERT INTO post_move_continuation_runs (
            rule_name, rule_version, slippage_mode, exit_tests, params,
            timezone_used, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW(3))
        """,
        (_RULE_NAME, _RULE_VERSION, slippage_mode, tests_json,
         json.dumps(params), timezone_name),
    )


def _finalize_run(run_id: int, c: _RunCounters, notes: str) -> None:
    execute_query(
        """
        UPDATE post_move_continuation_runs
        SET data_start = %s, data_end = %s, n_markets = %s, n_snapshots = %s,
            n_signals = %s, n_scalp_candidates = %s, n_context_signals = %s,
            n_test_rows = %s, notes = %s
        WHERE id = %s
        """,
        (c.data_start, c.data_end, c.n_markets, c.n_snapshots, c.n_signals,
         c.n_scalp_candidates, c.n_context_signals, c.n_test_rows, notes, run_id),
    )


def _insert_row(
    *, run_id, seq, sig: Signal, extra: dict, snap: _Snapshot, tf: dict,
    slippage_mode, entry_sim, entry_bid, scalp_candidate, timezone_used,
    exit_test: str, profile, res, exit_dt, time_to_profit_target,
) -> None:
    if res is not None:
        hit_tp   = res.exit_reason == "take_profit"
        hit_sl   = res.exit_reason == "stop_loss"
        timedout = res.exit_reason in ("timeout", "near_expiry")
        mfe, mae = res.max_favorable_excursion, res.max_adverse_excursion
        ttpk     = res.time_to_peak
        pnl, pnlpct = res.pnl, res.pnl_percent
        exit_reason = res.exit_reason
        n_updates   = res.n_updates
    else:
        hit_tp = hit_sl = timedout = None
        mfe = mae = ttpk = pnl = pnlpct = None
        exit_reason = None
        n_updates   = 0

    tp_abs    = profile.tp_abs if profile is not None else None
    sl_abs    = profile.sl_abs if profile is not None else None
    timeout_s = profile.timeout_s if profile is not None else None

    insert_and_get_id(
        """
        INSERT INTO post_move_continuation_signals (
            run_id, rule_name, rule_version, market_ticker, signal_seq,
            side, contract_ask, contract_bid, spread, spread_bucket,
            simulated_entry_price, price_bucket, scalp_candidate,
            btc_price, target_price, raw_momentum_score, directional_momentum_score,
            raw_gap_z_score, directional_gap_z_score,
            contract_price_change_5s, contract_price_change_10s,
            contract_price_change_30s, contract_price_change_60s,
            time_remaining_seconds, contract_age_seconds,
            volatility_30s, volatility_60s, volatility_regime,
            entry_time, hour_block, day_name, timezone_used,
            exit_test, tp_abs, sl_abs, timeout_s, slippage_mode,
            hit_take_profit_before_stop, hit_stop_before_take_profit, timed_out,
            max_favorable_excursion, max_adverse_excursion, time_to_peak,
            time_to_profit_target, simulated_pnl, simulated_pnl_percent,
            exit_reason_simulated, exit_time, n_updates
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s
        )
        """,
        (
            run_id, _RULE_NAME, _RULE_VERSION, sig.market_ticker, seq,
            sig.side, sig.ask_price, sig.bid_price, sig.spread, spread_bucket(sig.spread),
            entry_sim, extra.get("price_bucket"), scalp_candidate,
            sig.btc_price, sig.target_price,
            extra.get("raw_momentum_score"), extra.get("directional_momentum_score"),
            extra.get("raw_gap_z_score"), extra.get("directional_gap_z_score"),
            extra.get("contract_price_change_5s"), extra.get("contract_price_change_10s"),
            extra.get("contract_price_change_30s"), extra.get("contract_price_change_60s"),
            sig.time_remaining_seconds, sig.contract_age_seconds,
            sig.volatility_30s, sig.volatility_60s, snap.vol_regime,
            snap.recorded_at, tf["hour_block"], tf["day_name"], timezone_used,
            exit_test, tp_abs, sl_abs, timeout_s, slippage_mode,
            hit_tp, hit_sl, timedout,
            mfe, mae, ttpk,
            time_to_profit_target, pnl, pnlpct,
            exit_reason, exit_dt, n_updates,
        ),
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Lookahead-safe post-move continuation watch-only backtester "
                    "(paper-only / backtest-only — never trades)")
    ap.add_argument("--slippage", default=SLIPPAGE_MODE, choices=sorted(_SLIPPAGE))
    ap.add_argument("--start", default=None, help="ISO date/time lower bound (inclusive)")
    ap.add_argument("--end", default=None, help="ISO date/time upper bound (exclusive)")
    ap.add_argument("--limit-markets", type=int, default=None)
    ap.add_argument("--cooldown-seconds", type=float, default=_DEFAULT_COOLDOWN_S)
    ap.add_argument("--timezone", default=SIGNAL_TIMEZONE)
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    entry_add, exit_sub = _SLIPPAGE[args.slippage]

    markets = _load_markets(args.start, args.end, args.limit_markets)
    logger.info(
        "Post-move backtest over %d market(s)  rule=%s/%s  slippage=%s  tests=%s",
        len(markets), _RULE_NAME, _RULE_VERSION, args.slippage, POST_MOVE_PROFILE_NAMES,
    )

    params = {
        "cooldown_seconds": args.cooldown_seconds,
        "start": args.start, "end": args.end,
        "limit_markets": args.limit_markets,
        "min_ticks": _MIN_TICKS, "window_keep_s": _WINDOW_KEEP_S,
        "reversal_prob": "null_provider (lookahead-safe)",
        "fill_model": "entry=ask+slip, exit=bid-slip (never mid)",
        "exit_tests": POST_MOVE_PROFILE_NAMES,
        "scalp_rule": "ask < 0.85 scalped; 0.85+ logged as context only",
        "live_trading": "DISABLED — watch-only / paper-only research",
    }
    run_id = _insert_run(slippage_mode=args.slippage, params=params, timezone_name=args.timezone)
    counters = _RunCounters()

    for m in markets:
        ticker = m["market_ticker"]
        try:
            btc   = _load_btc_ticks(ticker, args.start, args.end)
            snaps = _load_snapshots(ticker, args.start, args.end)
            _replay_market(
                m, btc, snaps,
                entry_add=entry_add, exit_sub=exit_sub, slippage_mode=args.slippage,
                cooldown_s=args.cooldown_seconds, timezone_name=args.timezone,
                run_id=run_id, counters=counters,
            )
            counters.n_markets += 1
        except Exception:
            logger.exception("Replay failed for market %s — skipping", ticker)

    _finalize_run(run_id, counters, args.notes)

    logger.info(
        "Post-move run #%d complete — markets=%d snapshots=%d signals=%d "
        "scalp_candidates=%d context=%d test_rows=%d",
        run_id, counters.n_markets, counters.n_snapshots, counters.n_signals,
        counters.n_scalp_candidates, counters.n_context_signals, counters.n_test_rows,
    )
    print(f"\nPost-move continuation backtest run #{run_id} written.  "
          f"Generate the report with:\n"
          f"    python scripts/post_move_continuation_report.py --run-id {run_id}\n")


if __name__ == "__main__":
    main()
