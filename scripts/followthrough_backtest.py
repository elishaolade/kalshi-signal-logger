#!/usr/bin/env python3
"""
followthrough_backtest.py — Lookahead-safe follow-through filter hypothesis test.

Tests whether NO-side scalp entries perform better when contract-price
*follow-through* confirms the side being bought (the NO price keeps rising or
holds most of its gain after the initial move) rather than spiking once and
fading.

Design
------
The base entry is the REAL `premium_no_midrange_scalp` (v1) strategy function
(side=NO, NO ask 0.65–0.80, spread<=0.03, time_remaining 180–300s, directional
momentum & gap-z confirm NO).  On top of that we add a `volatility_regime !=
violent` gate and, for EVERY base entry, compute follow-through context via
`app.followthrough.evaluate_followthrough` using only data up to the entry
timestamp (NO contract-mid history + cumulative-volume history).

Each base entry is recorded with a `followthrough_confirmed` flag, so a single
run yields BOTH variants the report compares:
    • Variant A (base_no_followthrough)  = ALL base entries
    • Variant B (base_plus_followthrough) = entries with followthrough_confirmed

Both exit profiles are simulated per entry (fixed_4c_stop_6c, fixed_5c_stop_6c)
via `simulate_exit_fixed_abs`, so one row is written per (entry × profile).

No lookahead
------------
At snapshot ts t the strategy sees only ticks/quotes with recorded_at <= t; the
follow-through helpers only read history <= t; the exit simulator only walks the
bid path forward (ts > entry).

Fill model (NOT mid):  entry = ask + slippage,  exit = bid - slippage.

PAPER-ONLY / BACKTEST-ONLY RESEARCH.  This script never trades, never places an
order, and never enables LIVE_TRADING_ENABLED.  It only reads snapshots and
writes simulated results into the dedicated `followthrough_backtest_*` tables,
kept entirely separate from live `paper_trades`.  This is a hypothesis test, not
a trade recommendation.

Usage
-----
    python scripts/migrate_add_followthrough_backtest.py   # once, first
    python scripts/followthrough_backtest.py \
        [--slippage realistic] \
        [--start 2026-05-01] [--end 2026-06-01] \
        [--limit-markets N] [--cooldown-seconds 30] \
        [--timezone America/New_York] \
        [--baseline-volume-60s 0.0] [--notes "..."]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import SIGNAL_TIMEZONE, SLIPPAGE_MODE
from app.db import execute_query, fetch_all, insert_and_get_id
from app.exit_simulator import (
    FOLLOWTHROUGH_PROFILES,
    FOLLOWTHROUGH_PROFILE_NAMES,
    PathPoint,
    simulate_exit_fixed_abs,
)
from app.features import Tick, build_time_features, rolling_std, volatility_regime
from app.followthrough import (
    FT_MIN_QUOTE_UPDATES_60S,
    FollowThrough,
    evaluate_followthrough,
    scalping_valid_window,
)
from app.strategies import Signal, premium_no_midrange_scalp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("followthrough_backtest")

# Slippage table (mirrors app.paper_trader / historical_replay).
_SLIPPAGE: dict[str, tuple[float, float]] = {
    "optimistic": (0.00, 0.00),
    "realistic":  (0.01, 0.01),
    "harsh":      (0.02, 0.02),
}

# The base-entry rule we drive (NOT registered in _STRATEGIES — backtest only).
_BASE_RULE_NAME    = "premium_no_midrange_scalp"
_BASE_RULE_VERSION = "v1"
# The hypothesis label the rows are tagged with (kept separate from the live
# rule above so the report and tables read as a distinct experiment).
_HYP_RULE_NAME    = "premium_no_continuation_065_080_v2"
_HYP_RULE_VERSION = "v2"

_DEFAULT_COOLDOWN_S = 30.0
_WINDOW_KEEP_S      = 300.0   # trailing BTC ticks kept per snapshot
_MIN_TICKS          = 2       # live requires >= 2 BTC ticks before evaluating


# ── Null reversal-probability provider (lookahead-safe) ────────────────────────

def _null_reversal_prob(**_kwargs: Any) -> dict[str, Any]:
    return {
        "similar_sample_count":       0,
        "p_plus_2c_before_minus_2c":  None,
        "p_plus_3c_before_minus_2c":  None,
        "p_plus_4c_before_minus_3c":  None,
        "confidence_label":           "insufficient_data",
        "match_level":                "none",
    }


def _market_phase(time_remaining: float) -> str:
    """Coarse market phase for breakdown reporting (research dimension only)."""
    if time_remaining >= 600:
        return "early"
    if time_remaining >= 300:
        return "mid"
    if time_remaining >= 120:
        return "late"
    return "near_expiry"


# ── Reconstructed snapshot (volume-aware) ──────────────────────────────────────

@dataclass
class _Snapshot:
    ts:             float
    recorded_at:    datetime
    time_remaining: float
    contract_age:   float
    quotes:         dict[str, dict]          # {"YES": {...}, "NO": {...}}
    volumes:        dict[str, Optional[float]]   # cumulative volume per side
    btc_price:      float = 0.0
    vol_regime:     str   = "unknown"
    win_lo:         int   = 0
    win_hi:         int   = 0


@dataclass
class _RunCounters:
    n_markets:           int = 0
    n_snapshots:         int = 0
    n_base_entries:      int = 0
    n_confirmed_entries: int = 0
    n_trades:            int = 0
    data_start:  Optional[datetime] = None
    data_end:    Optional[datetime] = None


# ── Data loading ────────────────────────────────────────────────────────────────

def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


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
    """Load per-ts snapshots WITH cumulative volume per side (follow-through needs it)."""
    sql = """
        SELECT side, bid_price, ask_price, mid_price, spread, volume,
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
                quotes={}, volumes={},
            )
            by_ts[ra] = snap
        snap.quotes[r["side"]] = {
            "bid_price": _f(r["bid_price"]),
            "ask_price": _f(r["ask_price"]),
            "mid_price": _f(r["mid_price"]),
            "spread":    _f(r["spread"]),
        }
        snap.volumes[r["side"]] = _f(r["volume"])
    return [by_ts[k] for k in sorted(by_ts)]


# ── Per-market replay ────────────────────────────────────────────────────────────

def _annotate_snapshots(snaps: list[_Snapshot], btc: list[Tick]) -> None:
    """Pass A: attach btc_price, vol_regime and BTC-window indices to each snapshot."""
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
        window = btc[lo:hi]
        s.vol_regime = volatility_regime(rolling_std(window, 60.0))


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
    baseline_volume_60s: Optional[float],
    run_id: int,
    counters: _RunCounters,
) -> None:
    ticker       = market["market_ticker"]
    target_price = float(market["target_price"]) if market["target_price"] is not None else None
    if target_price is None or len(btc) < _MIN_TICKS or not snaps:
        return

    _annotate_snapshots(snaps, btc)

    hist: dict[str, list[Tick]] = {"YES": [], "NO": []}
    vol_hist: list[tuple[float, Optional[float]]] = []   # NO cumulative volume history
    blocked_until_ts = float("-inf")

    for i, s in enumerate(snaps):
        # ── Accumulate per-side contract-mid history + NO volume up to this ts ──
        for side in ("YES", "NO"):
            mid = s.quotes.get(side, {}).get("mid_price")
            if mid is not None:
                hist[side].append(Tick(price=float(mid), ts=s.ts))
        vol_hist.append((s.ts, s.volumes.get("NO")))

        counters.n_snapshots += 1
        if counters.data_start is None:
            counters.data_start = s.recorded_at
        counters.data_end = s.recorded_at

        ticks_window = btc[s.win_lo:s.win_hi]
        if len(ticks_window) < _MIN_TICKS:
            continue
        if s.ts < blocked_until_ts:
            continue

        sig = premium_no_midrange_scalp(
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

        # Base-entry volatility gate (spec: volatility_regime != violent).
        if s.vol_regime == "violent":
            continue

        latest_exit_ts = _take_entry(
            sig, s, i, snaps,
            no_history=list(hist["NO"]), vol_history=list(vol_hist),
            entry_add=entry_add, exit_sub=exit_sub, slippage_mode=slippage_mode,
            timezone_name=timezone_name, baseline_volume_60s=baseline_volume_60s,
            run_id=run_id, counters=counters,
        )
        blocked_until_ts = max(latest_exit_ts, s.ts + cooldown_s)


def _take_entry(
    sig: Signal,
    entry_snap: _Snapshot,
    entry_idx: int,
    snaps: list[_Snapshot],
    *,
    no_history: list[Tick],
    vol_history: list[tuple[float, Optional[float]]],
    entry_add: float,
    exit_sub: float,
    slippage_mode: str,
    timezone_name: str,
    baseline_volume_60s: Optional[float],
    run_id: int,
    counters: _RunCounters,
) -> float:
    """Evaluate follow-through for one NO base entry and simulate both exit
    profiles.  Persists one row per profile.  Returns latest exit ts."""
    side  = "NO"
    quote = entry_snap.quotes.get(side, {})
    ask   = quote.get("ask_price")
    bid   = quote.get("bid_price")
    if not ask or ask <= 0 or bid is None:
        return entry_snap.ts

    # ── Follow-through evaluation (lookahead-safe; history is <= entry ts) ─────
    ft: FollowThrough = evaluate_followthrough(
        no_history          = no_history,
        now_ts              = entry_snap.ts,
        spread              = sig.spread,
        volatility_regime   = entry_snap.vol_regime,
        vol_history         = vol_history,
        baseline_volume_60s = baseline_volume_60s,
        min_quote_updates_60s = FT_MIN_QUOTE_UPDATES_60S,
    )

    tf = build_time_features(entry_snap.recorded_at, tz=timezone_name)
    svw = scalping_valid_window(
        entry_hour             = tf.get("hour"),
        is_weekend             = tf.get("is_weekend"),
        volatility_regime      = entry_snap.vol_regime,
        quote_update_count_60s = ft.quote_update_count_60s,
    )

    # Directional momentum (NO side = -raw_momentum_score) carried for breakdowns.
    directional_momentum = -sig.momentum_score if sig.momentum_score is not None else None

    entry_sim = round(float(ask) + entry_add, 4)
    entry_bid = round(float(bid), 4)

    # Forward path: every later snapshot's NO bid.
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

    counters.n_base_entries += 1
    if ft.followthrough_confirmed:
        counters.n_confirmed_entries += 1
    seq = counters.n_base_entries

    latest_exit_ts = entry_snap.ts
    for name in FOLLOWTHROUGH_PROFILE_NAMES:
        profile = FOLLOWTHROUGH_PROFILES[name]
        res = simulate_exit_fixed_abs(
            entry_sim=entry_sim, entry_bid=entry_bid, exit_sub=exit_sub,
            path=path, profile=profile,
        )
        exit_ts, exit_dt = _exit_timestamp(res, path, entry_snap)
        latest_exit_ts = max(latest_exit_ts, exit_ts)

        _insert_trade(
            run_id=run_id, seq=seq, sig=sig, snap=entry_snap, tf=tf, ft=ft,
            scalping_valid=svw, directional_momentum=directional_momentum,
            slippage_mode=slippage_mode, entry_sim=entry_sim, entry_bid=entry_bid,
            res=res, exit_dt=exit_dt,
        )
        counters.n_trades += 1

    return latest_exit_ts


def _exit_timestamp(res, path: list[PathPoint], entry_snap: _Snapshot):
    """Map an ExitResult back to a wall-clock exit time using its n_updates."""
    if not path:
        return entry_snap.ts, entry_snap.recorded_at
    idx = min(max(res.n_updates - 1, 0), len(path) - 1)
    elapsed = path[idx].elapsed
    ts = entry_snap.ts + elapsed
    dt = datetime.fromtimestamp(ts, tz=entry_snap.recorded_at.tzinfo)
    return ts, dt


# ── Persistence ──────────────────────────────────────────────────────────────────

def _insert_run(
    *, slippage_mode: str, params: dict, timezone_name: str,
    baseline_volume_60s: Optional[float],
) -> int:
    profiles_json = json.dumps([
        {"name": p.name, "kind": p.kind, "tp_abs": p.tp_abs,
         "sl_abs": p.sl_abs, "timeout_s": p.timeout_s}
        for p in (FOLLOWTHROUGH_PROFILES[n] for n in FOLLOWTHROUGH_PROFILE_NAMES)
    ])
    return insert_and_get_id(
        """
        INSERT INTO followthrough_backtest_runs (
            rule_name, rule_version, slippage_mode, exit_profiles, params,
            timezone_used, baseline_volume_60s, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(3))
        """,
        (_HYP_RULE_NAME, _HYP_RULE_VERSION, slippage_mode, profiles_json,
         json.dumps(params), timezone_name, baseline_volume_60s),
    )


def _finalize_run(run_id: int, c: _RunCounters, notes: str) -> None:
    execute_query(
        """
        UPDATE followthrough_backtest_runs
        SET data_start = %s, data_end = %s, n_markets = %s, n_snapshots = %s,
            n_base_entries = %s, n_confirmed_entries = %s, n_trades = %s, notes = %s
        WHERE id = %s
        """,
        (c.data_start, c.data_end, c.n_markets, c.n_snapshots,
         c.n_base_entries, c.n_confirmed_entries, c.n_trades, notes, run_id),
    )


def _insert_trade(
    *, run_id, seq, sig: Signal, snap: _Snapshot, tf: dict, ft: FollowThrough,
    scalping_valid: bool, directional_momentum: Optional[float],
    slippage_mode, entry_sim, entry_bid, res, exit_dt,
) -> None:
    insert_and_get_id(
        """
        INSERT INTO followthrough_backtest_trades (
            run_id, rule_name, rule_version, market_ticker, signal_seq,
            side_bought, market_phase, market_age_seconds, time_remaining_seconds,
            btc_price, target_price, raw_gap_z_score, directional_momentum,
            no_ask, no_bid, no_mid, spread, spread_bucket, volatility_regime,
            no_price_change_10s, no_price_change_30s, no_recent_high_30s,
            no_pullback_from_recent_high, quote_update_count_60s, volume_60s,
            baseline_volume_60s, participation_basis,
            followthrough_confirmed, followthrough_failed, scalping_valid_window,
            entry_time, entry_date, entry_hour, entry_day_of_week,
            entry_day_name, entry_hour_block, entry_is_weekend,
            slippage_mode, entry_price_simulated, entry_bid,
            exit_profile, peak_contract_price,
            max_favorable_excursion, max_adverse_excursion, time_to_peak,
            exit_time, exit_price_simulated, exit_reason, pnl, pnl_percent,
            n_updates
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s
        )
        """,
        (
            run_id, _HYP_RULE_NAME, _HYP_RULE_VERSION, sig.market_ticker, seq,
            "NO", _market_phase(sig.time_remaining_seconds),
            sig.contract_age_seconds, sig.time_remaining_seconds,
            sig.btc_price, sig.target_price, sig.gap_z_score, directional_momentum,
            sig.ask_price, sig.bid_price, sig.contract_price, sig.spread,
            ft.spread_bucket, snap.vol_regime,
            ft.no_price_change_10s, ft.no_price_change_30s, ft.no_recent_high_30s,
            ft.no_pullback_from_recent_high, ft.quote_update_count_60s, ft.volume_60s,
            ft.baseline_volume_60s, ft.participation_basis,
            ft.followthrough_confirmed, ft.followthrough_failed, scalping_valid,
            snap.recorded_at, tf["date"], tf["hour"], tf["day_of_week"],
            tf["day_name"], tf["hour_block"], tf["is_weekend"],
            slippage_mode, entry_sim, entry_bid,
            res.exit_profile, res.peak_bid,
            res.max_favorable_excursion, res.max_adverse_excursion, res.time_to_peak,
            exit_dt, res.exit_price_simulated, res.exit_reason, res.pnl, res.pnl_percent,
            res.n_updates,
        ),
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Lookahead-safe follow-through filter hypothesis backtester "
                    "(paper-only / backtest-only — never trades)")
    ap.add_argument("--slippage", default=SLIPPAGE_MODE, choices=sorted(_SLIPPAGE))
    ap.add_argument("--start", default=None, help="ISO date/time lower bound (inclusive)")
    ap.add_argument("--end", default=None, help="ISO date/time upper bound (exclusive)")
    ap.add_argument("--limit-markets", type=int, default=None)
    ap.add_argument("--cooldown-seconds", type=float, default=_DEFAULT_COOLDOWN_S)
    ap.add_argument("--timezone", default=SIGNAL_TIMEZONE)
    ap.add_argument("--baseline-volume-60s", type=float, default=None,
                    help="optional 60s baseline volume; when given AND volume is "
                         "available, participation = volume_60s > baseline. "
                         "Otherwise falls back to a quote-update-count proxy.")
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    entry_add, exit_sub = _SLIPPAGE[args.slippage]

    markets = _load_markets(args.start, args.end, args.limit_markets)
    logger.info(
        "Follow-through backtest over %d market(s)  base=%s/%s  slippage=%s  "
        "profiles=%s  baseline_vol=%s",
        len(markets), _BASE_RULE_NAME, _BASE_RULE_VERSION, args.slippage,
        FOLLOWTHROUGH_PROFILE_NAMES, args.baseline_volume_60s,
    )

    params = {
        "base_rule": f"{_BASE_RULE_NAME}/{_BASE_RULE_VERSION}",
        "cooldown_seconds": args.cooldown_seconds,
        "start": args.start, "end": args.end,
        "limit_markets": args.limit_markets,
        "min_ticks": _MIN_TICKS, "window_keep_s": _WINDOW_KEEP_S,
        "reversal_prob": "null_provider (lookahead-safe)",
        "fill_model": "entry=ask+slip, exit=bid-slip (never mid)",
        "vol_gate": "volatility_regime != violent (entry filter)",
        "exit_profiles": FOLLOWTHROUGH_PROFILE_NAMES,
        "followthrough": "evaluate_followthrough (lookahead-safe NO history)",
        "live_trading": "DISABLED — paper-only/backtest-only research",
    }
    run_id = _insert_run(
        slippage_mode=args.slippage, params=params, timezone_name=args.timezone,
        baseline_volume_60s=args.baseline_volume_60s,
    )
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
                baseline_volume_60s=args.baseline_volume_60s,
                run_id=run_id, counters=counters,
            )
            counters.n_markets += 1
        except Exception:
            logger.exception("Replay failed for market %s — skipping", ticker)

    _finalize_run(run_id, counters, args.notes)

    logger.info(
        "Follow-through run #%d complete — markets=%d snapshots=%d "
        "base_entries=%d confirmed=%d trades=%d",
        run_id, counters.n_markets, counters.n_snapshots,
        counters.n_base_entries, counters.n_confirmed_entries, counters.n_trades,
    )
    print(f"\nFollow-through backtest run #{run_id} written.  "
          f"Generate the A-vs-B report with:\n"
          f"    python scripts/followthrough_report.py --run-id {run_id}\n")


if __name__ == "__main__":
    main()
