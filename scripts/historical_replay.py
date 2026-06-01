#!/usr/bin/env python3
"""
historical_replay.py — Lookahead-safe backtester over stored market snapshots.

Replays recorded markets from `btc_ticks` + `contract_ticks` + `markets`, runs a
strategy's REAL entry function against each reconstructed snapshot, and simulates
several exit profiles forward against the observed bid path.  Results are written
to `backtest_runs` + `backtest_trades`, kept entirely separate from the live
`paper_trades` lifecycle.

No lookahead
------------
At each snapshot timestamp t, the strategy is given only data with
recorded_at <= t:
  • BTC tick series sliced to ts <= t (features recomputed exactly as live),
  • per-side contract-mid history accumulated up to t,
  • the contract quotes / timing of snapshot t.
Exit simulation only ever walks the path *forward* from the entry (ts > entry).
The self-referential reversal-probability lookup is deliberately replaced by a
null provider during replay: querying the live observations table would leak
information recorded at other (incl. future) times.  The 0.55 reversal-prob gate
is non-blocking anyway, so entries are unaffected.

Fill model (NOT mid):  entry = ask + slippage,  exit = bid - slippage.

PAPER-ONLY RESEARCH.  This script never trades and never enables order
execution; it only reads snapshots and writes simulated results.

Usage
-----
    python scripts/historical_replay.py \
        [--rule cheap_losing_contract_reversal_trail] [--version v1] \
        [--slippage realistic] [--profiles a,b,c] \
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
    BACKTEST_PROFILES,
    DEFAULT_PROFILE_NAMES,
    PathPoint,
    simulate_exit,
)
from app.features import Tick, build_time_features, rolling_std, volatility_regime
from app.strategies import Signal, cheap_losing_contract_reversal_trail

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("historical_replay")

# Slippage table (mirrors app.paper_trader / CLCReversalTracker).
_SLIPPAGE: dict[str, tuple[float, float]] = {
    "optimistic": (0.00, 0.00),
    "realistic":  (0.01, 0.01),
    "harsh":      (0.02, 0.02),
}

# Strategies the replayer knows how to drive (entry fn + the side it buys).
# Only cheap_losing_contract_reversal_trail is supported for now — its Signal
# already carries the bought (losing) side and a rich `extra` context dict.
_STRATEGY_FNS = {
    "cheap_losing_contract_reversal_trail": cheap_losing_contract_reversal_trail,
}

_DEFAULT_COOLDOWN_S = 30.0
_WINDOW_KEEP_S      = 300.0   # trailing BTC ticks kept per snapshot (>= 120s max feature window)
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


# ── Reconstructed snapshot ──────────────────────────────────────────────────────

@dataclass
class _Snapshot:
    ts:             float
    recorded_at:    datetime
    time_remaining: float
    contract_age:   float
    quotes:         dict[str, dict]          # {"YES": {...}, "NO": {...}}
    btc_price:      float = 0.0              # filled in pass A
    vol_regime:     str   = "unknown"        # filled in pass A
    win_lo:         int   = 0                # BTC-tick window [lo:hi) for this ts
    win_hi:         int   = 0


@dataclass
class _RunCounters:
    n_markets:   int = 0
    n_snapshots: int = 0
    n_signals:   int = 0
    n_trades:    int = 0
    data_start:  Optional[datetime] = None
    data_end:    Optional[datetime] = None


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


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


# ── Per-market replay ────────────────────────────────────────────────────────────

def _annotate_snapshots(snaps: list[_Snapshot], btc: list[Tick]) -> None:
    """Pass A: attach btc_price, vol_regime and BTC-window indices to each snapshot."""
    lo = hi = 0
    n = len(btc)
    for s in snaps:
        # hi = first index with ts > snapshot ts  → window is [lo:hi)
        while hi < n and btc[hi].ts <= s.ts:
            hi += 1
        # lo advances to keep only the trailing _WINDOW_KEEP_S seconds.
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
    strategy_fn,
    rule_name: str,
    rule_version: str,
    entry_add: float,
    exit_sub: float,
    slippage_mode: str,
    profile_names: list[str],
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
    blocked_until_ts = float("-inf")

    for i, s in enumerate(snaps):
        # ── 7b. Accumulate per-side contract-mid history up to this ts ─────────
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
        if s.ts < blocked_until_ts:          # cooldown / no-overlap with prior entry
            continue

        sig = strategy_fn(
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

        latest_exit_ts = _take_signal(
            sig, s, i, snaps,
            entry_add=entry_add, exit_sub=exit_sub, slippage_mode=slippage_mode,
            profile_names=profile_names, timezone_name=timezone_name,
            rule_name=rule_name, rule_version=rule_version,
            run_id=run_id, counters=counters,
        )
        # Block re-entry until the trade is done AND the cooldown has elapsed.
        blocked_until_ts = max(latest_exit_ts, s.ts + cooldown_s)


def _take_signal(
    sig: Signal,
    entry_snap: _Snapshot,
    entry_idx: int,
    snaps: list[_Snapshot],
    *,
    entry_add: float,
    exit_sub: float,
    slippage_mode: str,
    profile_names: list[str],
    timezone_name: str,
    rule_name: str,
    rule_version: str,
    run_id: int,
    counters: _RunCounters,
) -> float:
    """Simulate every exit profile for one entry; persist a row per profile.
    Returns the latest exit timestamp across profiles (for overlap blocking)."""
    side  = sig.side                       # bought (losing) side
    quote = entry_snap.quotes.get(side, {})
    ask   = quote.get("ask_price")
    bid   = quote.get("bid_price")
    if not ask or ask <= 0 or bid is None:
        return entry_snap.ts

    entry_sim = round(float(ask) + entry_add, 4)
    entry_bid = round(float(bid), 4)

    # Forward path: every later snapshot's bid for the bought side.
    path: list[PathPoint] = []
    for j in range(entry_idx + 1, len(snaps)):
        sj   = snaps[j]
        b    = sj.quotes.get(side, {}).get("bid_price")
        if b is None:
            continue
        path.append(PathPoint(
            elapsed=sj.ts - entry_snap.ts, bid=float(b),
            time_remaining=sj.time_remaining, vol_regime=sj.vol_regime,
        ))

    extra = sig.extra or {}
    tf    = build_time_features(entry_snap.recorded_at, tz=timezone_name)
    counters.n_signals += 1
    seq = counters.n_signals

    latest_exit_ts = entry_snap.ts
    for name in profile_names:
        profile = BACKTEST_PROFILES[name]
        res = simulate_exit(
            entry_sim=entry_sim, entry_bid=entry_bid, exit_sub=exit_sub,
            path=path, profile=profile,
        )
        # Exit timestamp = entry + elapsed of the point that triggered the exit.
        exit_ts, exit_dt = _exit_timestamp(res, path, entry_snap)
        latest_exit_ts = max(latest_exit_ts, exit_ts)

        _insert_trade(
            run_id=run_id, seq=seq, sig=sig, extra=extra, snap=entry_snap, tf=tf,
            slippage_mode=slippage_mode, entry_sim=entry_sim, entry_bid=entry_bid,
            res=res, exit_dt=exit_dt, rule_name=rule_name, rule_version=rule_version,
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
    *, rule_name: str, rule_version: str, slippage_mode: str,
    profile_names: list[str], params: dict, timezone_name: str,
) -> int:
    profiles_json = json.dumps([
        {
            "name": p.name, "kind": p.kind, "tp_pct": p.tp_pct, "sl_pct": p.sl_pct,
            "hard_stop_pct": p.hard_stop_pct,
            "trail_activation_pct": p.trail_activation_pct, "trail_cents": p.trail_cents,
        }
        for p in (BACKTEST_PROFILES[n] for n in profile_names)
    ])
    return insert_and_get_id(
        """
        INSERT INTO backtest_runs (
            rule_name, rule_version, slippage_mode, exit_profiles, params,
            timezone_used, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW(3))
        """,
        (rule_name, rule_version, slippage_mode, profiles_json,
         json.dumps(params), timezone_name),
    )


def _finalize_run(run_id: int, c: _RunCounters, notes: str) -> None:
    execute_query(
        """
        UPDATE backtest_runs
        SET data_start = %s, data_end = %s, n_markets = %s, n_snapshots = %s,
            n_signals = %s, n_trades = %s, notes = %s
        WHERE id = %s
        """,
        (c.data_start, c.data_end, c.n_markets, c.n_snapshots,
         c.n_signals, c.n_trades, notes, run_id),
    )


def _insert_trade(
    *, run_id, seq, sig: Signal, extra: dict, snap: _Snapshot, tf: dict,
    slippage_mode, entry_sim, entry_bid, res, exit_dt, rule_name, rule_version,
) -> None:
    insert_and_get_id(
        """
        INSERT INTO backtest_trades (
            run_id, rule_name, rule_version, market_ticker, signal_seq,
            setup_type, market_phase, market_age_seconds, time_remaining_seconds,
            side_bought, winning_side, losing_side,
            btc_price, target_price, raw_gap_z_score, adverse_z_score,
            losing_contract_ask, losing_contract_bid, losing_contract_spread,
            volatility_regime, whipsaw_score,
            entry_time, entry_date, entry_hour, entry_day_of_week,
            entry_day_name, entry_hour_block,
            slippage_mode, entry_price_simulated, entry_bid,
            exit_profile, trail_activated, peak_contract_price,
            max_favorable_excursion, max_adverse_excursion, time_to_peak,
            exit_time, exit_price_simulated, exit_reason, pnl, pnl_percent,
            n_updates
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s
        )
        """,
        (
            run_id, rule_name, rule_version, sig.market_ticker, seq,
            extra.get("setup_type"), extra.get("market_phase"),
            extra.get("market_age_seconds"), sig.time_remaining_seconds,
            sig.side, extra.get("winning_side"), extra.get("losing_side"),
            sig.btc_price, sig.target_price,
            extra.get("raw_gap_z_score"), extra.get("adverse_z_score"),
            extra.get("losing_contract_ask"), extra.get("losing_contract_bid"),
            extra.get("losing_contract_spread"),
            extra.get("volatility_regime"), extra.get("whipsaw_score"),
            snap.recorded_at, tf["date"], tf["hour"], tf["day_of_week"],
            tf["day_name"], tf["hour_block"],
            slippage_mode, entry_sim, entry_bid,
            res.exit_profile, res.trail_activated, res.peak_bid,
            res.max_favorable_excursion, res.max_adverse_excursion, res.time_to_peak,
            exit_dt, res.exit_price_simulated, res.exit_reason, res.pnl, res.pnl_percent,
            res.n_updates,
        ),
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Lookahead-safe historical replay backtester")
    ap.add_argument("--rule", default="cheap_losing_contract_reversal_trail")
    ap.add_argument("--version", default="v1")
    ap.add_argument("--slippage", default=SLIPPAGE_MODE, choices=sorted(_SLIPPAGE))
    ap.add_argument("--profiles", default=",".join(DEFAULT_PROFILE_NAMES),
                    help="comma-separated exit-profile names")
    ap.add_argument("--start", default=None, help="ISO date/time lower bound (inclusive)")
    ap.add_argument("--end", default=None, help="ISO date/time upper bound (exclusive)")
    ap.add_argument("--limit-markets", type=int, default=None)
    ap.add_argument("--cooldown-seconds", type=float, default=_DEFAULT_COOLDOWN_S)
    ap.add_argument("--timezone", default=SIGNAL_TIMEZONE)
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    strategy_fn = _STRATEGY_FNS.get(args.rule)
    if strategy_fn is None:
        ap.error(f"Unsupported rule '{args.rule}'. Supported: {sorted(_STRATEGY_FNS)}")

    profile_names = [p.strip() for p in args.profiles.split(",") if p.strip()]
    unknown = [p for p in profile_names if p not in BACKTEST_PROFILES]
    if unknown:
        ap.error(f"Unknown profile(s): {unknown}. Known: {sorted(BACKTEST_PROFILES)}")

    entry_add, exit_sub = _SLIPPAGE[args.slippage]

    markets = _load_markets(args.start, args.end, args.limit_markets)
    logger.info("Replaying %d market(s)  rule=%s/%s  slippage=%s  profiles=%s",
                len(markets), args.rule, args.version, args.slippage, profile_names)

    params = {
        "cooldown_seconds": args.cooldown_seconds,
        "start": args.start, "end": args.end,
        "limit_markets": args.limit_markets,
        "min_ticks": _MIN_TICKS, "window_keep_s": _WINDOW_KEEP_S,
        "reversal_prob": "null_provider (lookahead-safe)",
        "fill_model": "entry=ask+slip, exit=bid-slip",
    }
    run_id = _insert_run(
        rule_name=args.rule, rule_version=args.version, slippage_mode=args.slippage,
        profile_names=profile_names, params=params, timezone_name=args.timezone,
    )
    counters = _RunCounters()

    for m in markets:
        ticker = m["market_ticker"]
        try:
            btc   = _load_btc_ticks(ticker, args.start, args.end)
            snaps = _load_snapshots(ticker, args.start, args.end)
            _replay_market(
                m, btc, snaps,
                strategy_fn=strategy_fn, rule_name=args.rule, rule_version=args.version,
                entry_add=entry_add, exit_sub=exit_sub, slippage_mode=args.slippage,
                profile_names=profile_names, cooldown_s=args.cooldown_seconds,
                timezone_name=args.timezone, run_id=run_id, counters=counters,
            )
            counters.n_markets += 1
        except Exception:
            logger.exception("Replay failed for market %s — skipping", ticker)

    _finalize_run(run_id, counters, args.notes)

    logger.info(
        "Backtest run #%d complete — markets=%d snapshots=%d signals=%d trades=%d",
        run_id, counters.n_markets, counters.n_snapshots,
        counters.n_signals, counters.n_trades,
    )
    print(f"\nBacktest run #{run_id} written.  "
          f"Generate the report with:\n"
          f"    python scripts/backtest_report.py --run-id {run_id}\n")


if __name__ == "__main__":
    main()
