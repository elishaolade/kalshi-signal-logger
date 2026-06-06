#!/usr/bin/env python3
"""
repricing_discrepancy_backtest.py — Detect BTC-Kalshi repricing discrepancy events.

Hypothesis
----------
When BTC moves sharply in a short window, Kalshi contract prices sometimes
lag behind.  If the contract price changed LESS than X cents in the same
direction during that window ("underreaction"), does subsequent repricing
in the expected direction follow?

Definitions
-----------
BTC "tick":
    Each row in the btc_ticks table is one polled price sample at ~2-second
    intervals.  A "burst" is a NET directional price move measured over a
    rolling 30-second window using the actual dollar amounts, NOT the count
    of consecutive steps.

BTC burst threshold (N):
    The minimum net dollar move over the 30-second window.  Parameter grid:
    N × $10 where N ∈ {4, 5, 6}  →  $40, $50, $60.
    The event fires at the lowest threshold ($40) and is tagged for all higher
    thresholds it also qualifies for.

Direction → implied side:
    BTC net UP  (btc_end > btc_start)  →  implied side = YES
        Rationale: YES = "BTC closes above target"; YES mid should rise when
        BTC rises and the gap to target narrows.
    BTC net DOWN (btc_end < btc_start) →  implied side = NO
        Rationale: NO mid should rise when BTC falls away from target.

Contract price field:
    mid_price is used for burst-window reaction measurement and forward
    repricing.  Mid is the unbiased, noise-reduced estimate of fair value.
    ask_price is used only for simulating the entry cost (entry_sim = ask + slip),
    which is stored separately for P&L analysis.

Underreaction (X threshold):
    implied-side mid change over same 30-second window < X cents.
    A negative contract_change (price moved AGAINST the expected direction)
    always counts as underreaction for all X thresholds.
    Values tested: X ∈ {0.01, 0.02, 0.03}.

Forward repricing window (Z):
    After the detection point, how far to measure subsequent contract movement.
    Values tested: Z ∈ {10, 20, 30, 45, 60} seconds.

Cooldown:
    30 seconds per side after each detected event.  Prevents the same BTC move
    from generating overlapping events at every subsequent tick.

No lookahead:
    All burst-window measurements use only data with recorded_at <= event_time.
    Forward measurements walk strictly forward from event_time.

RESEARCH ONLY — no live trading, no order execution.  Writes only to
repricing_discrepancy_* tables; never touches paper_trades.

Usage
-----
    python scripts/migrate_add_repricing_discrepancy.py   # once, first

    # Full run (all available data):
    python scripts/repricing_discrepancy_backtest.py

    # Limited smoke test:
    python scripts/repricing_discrepancy_backtest.py --limit-markets 10

    # Date-filtered:
    python scripts/repricing_discrepancy_backtest.py --start 2026-05-27 --end 2026-06-01

    # Slippage model:
    python scripts/repricing_discrepancy_backtest.py --slippage realistic
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

from app.config import SLIPPAGE_MODE
from app.db import execute_query, fetch_all, insert_and_get_id
from app.features import Tick, rolling_std, volatility_regime
from app.followthrough import spread_bucket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("repricing_discrepancy_backtest")

# ── Fixed parameters ─────────────────────────────────────────────────────────

BURST_WINDOW_S   = 30.0          # detection window (seconds)
MIN_BURST_DOLLAR = 40.0          # minimum threshold to fire ($40 = N=4 × $10)
N_THRESHOLDS     = [40, 50, 60]  # dollar amounts corresponding to N=4,5,6
X_THRESHOLDS     = [0.01, 0.02, 0.03]
Z_WINDOWS_S      = [10, 20, 30, 45, 60]
DEFAULT_COOLDOWN = 30.0          # per-side cooldown (seconds)

_SLIPPAGE: dict[str, tuple[float, float]] = {
    "optimistic": (0.00, 0.00),
    "realistic":  (0.01, 0.01),
    "harsh":      (0.02, 0.02),
}

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class _Row:
    """One aligned row: btc_price + YES + NO contract quotes at the same timestamp."""
    ts:         float     # Unix epoch seconds
    recorded_at: datetime
    btc:        float
    yes_mid:    Optional[float]
    yes_ask:    Optional[float]
    yes_bid:    Optional[float]
    yes_spread: Optional[float]
    no_mid:     Optional[float]
    no_ask:     Optional[float]
    no_bid:     Optional[float]
    no_spread:  Optional[float]
    time_remaining_s: int
    contract_age_s:   int


@dataclass
class _RunCounters:
    n_markets:          int = 0
    n_snapshots:        int = 0
    n_burst_events:     int = 0
    n_under_x01:        int = 0
    n_under_x02:        int = 0
    n_under_x03:        int = 0
    data_start: Optional[datetime] = None
    data_end:   Optional[datetime] = None


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_markets(start: Optional[str], end: Optional[str], limit: Optional[int]) -> list[dict]:
    sql = """
        SELECT m.market_ticker, m.target_price, m.open_time, m.close_time
        FROM markets m
        WHERE EXISTS (
            SELECT 1 FROM btc_ticks b
            WHERE b.market_ticker = m.market_ticker
              {start_clause}
              {end_clause}
        )
        ORDER BY m.open_time ASC, m.market_ticker ASC
    """
    params: list[Any] = []
    start_clause = end_clause = ""
    if start:
        start_clause = "AND b.recorded_at >= %s"
        params.append(start)
    if end:
        end_clause = "AND b.recorded_at < %s"
        params.append(end)
    sql = sql.format(start_clause=start_clause, end_clause=end_clause)
    if limit:
        sql += "\nLIMIT %s"
        params.append(int(limit))
    return fetch_all(sql, tuple(params))


def _load_rows(ticker: str, start: Optional[str], end: Optional[str]) -> list[_Row]:
    """
    Load aligned (btc, yes, no) rows for a single market ordered by time.
    Joins btc_ticks with contract_ticks on (market_ticker, recorded_at).
    """
    sql = """
        SELECT
            b.recorded_at,
            UNIX_TIMESTAMP(b.recorded_at)   AS ts,
            b.btc_price,
            cy.mid_price   AS yes_mid,   cy.ask_price AS yes_ask,
            cy.bid_price   AS yes_bid,   cy.spread    AS yes_spread,
            cn.mid_price   AS no_mid,    cn.ask_price AS no_ask,
            cn.bid_price   AS no_bid,    cn.spread    AS no_spread,
            COALESCE(cy.time_remaining_seconds, cn.time_remaining_seconds, 0) AS tte,
            COALESCE(cy.contract_age_seconds,   cn.contract_age_seconds,   0) AS age
        FROM btc_ticks b
        LEFT JOIN contract_ticks cy
            ON cy.market_ticker = b.market_ticker
           AND cy.recorded_at   = b.recorded_at
           AND cy.side          = 'YES'
        LEFT JOIN contract_ticks cn
            ON cn.market_ticker = b.market_ticker
           AND cn.recorded_at   = b.recorded_at
           AND cn.side          = 'NO'
        WHERE b.market_ticker = %s
    """
    params: list[Any] = [ticker]
    if start:
        sql += " AND b.recorded_at >= %s"; params.append(start)
    if end:
        sql += " AND b.recorded_at < %s"; params.append(end)
    sql += " ORDER BY b.recorded_at ASC"

    raw = fetch_all(sql, tuple(params))
    out: list[_Row] = []
    for r in raw:
        out.append(_Row(
            ts          = float(r["ts"]),
            recorded_at = r["recorded_at"],
            btc         = float(r["btc_price"]),
            yes_mid     = _f(r["yes_mid"]),    yes_ask = _f(r["yes_ask"]),
            yes_bid     = _f(r["yes_bid"]),    yes_spread = _f(r["yes_spread"]),
            no_mid      = _f(r["no_mid"]),     no_ask  = _f(r["no_ask"]),
            no_bid      = _f(r["no_bid"]),     no_spread  = _f(r["no_spread"]),
            time_remaining_s = int(r["tte"] or 0),
            contract_age_s   = int(r["age"] or 0),
        ))
    return out


# ── Per-market analysis ───────────────────────────────────────────────────────

def _mid(row: _Row, side: str) -> Optional[float]:
    return row.yes_mid if side == "YES" else row.no_mid

def _ask(row: _Row, side: str) -> Optional[float]:
    return row.yes_ask if side == "YES" else row.no_ask

def _bid(row: _Row, side: str) -> Optional[float]:
    return row.yes_bid if side == "YES" else row.no_bid

def _spread(row: _Row, side: str) -> Optional[float]:
    return row.yes_spread if side == "YES" else row.no_spread


def _process_market(
    market: dict,
    rows: list[_Row],
    *,
    entry_add: float,
    exit_sub: float,
    slippage_mode: str,
    cooldown_s: float,
    run_id: int,
    counters: _RunCounters,
) -> None:
    if len(rows) < 20:   # need at least 30s of history → ~15 rows at 2s polling
        return

    n = len(rows)
    ticker = market["market_ticker"]
    counters.n_markets += 1

    # Per-side cooldown: tracks the earliest ts at which the next event fires.
    blocked_until: dict[str, float] = {"YES": float("-inf"), "NO": float("-inf")}

    # Two-pointer window: lo_ptr is the left edge of the 30s burst window.
    lo_ptr = 0

    for i, row in enumerate(rows):
        counters.n_snapshots += 1
        if counters.data_start is None:
            counters.data_start = row.recorded_at
        counters.data_end = row.recorded_at

        # Advance left edge to maintain 30-second window.
        while rows[lo_ptr].ts < row.ts - BURST_WINDOW_S:
            lo_ptr += 1

        # Need at least a few points in the window.
        window_len = i - lo_ptr + 1
        if window_len < 5:
            continue

        start_row = rows[lo_ptr]
        btc_move  = row.btc - start_row.btc   # signed: positive=up, negative=down

        if abs(btc_move) < MIN_BURST_DOLLAR:
            continue

        # Determine implied side.
        side    = "YES" if btc_move > 0 else "NO"
        mid_end = _mid(row, side)
        mid_start = _mid(start_row, side)
        if mid_end is None or mid_start is None:
            continue

        # Cooldown check.
        if row.ts < blocked_until[side]:
            continue

        # ── Burst confirmed ───────────────────────────────────────────────────
        btc_abs = abs(btc_move)

        # Contract change in the direction we expect (positive = reacted correctly).
        contract_change = mid_end - mid_start

        # Underreaction flags.
        under_x01 = contract_change < 0.01
        under_x02 = contract_change < 0.02
        under_x03 = contract_change < 0.03

        # Simulated entry.
        ask_now    = _ask(row, side)
        bid_now    = _bid(row, side)
        entry_sim  = round(float(ask_now) + entry_add, 4) if ask_now else None
        sprd_val   = _spread(row, side)
        vol_regime = volatility_regime(rolling_std(
            [Tick(price=r.btc, ts=r.ts) for r in rows[lo_ptr:i+1]], 60.0
        ))

        # ── Forward repricing measurements ────────────────────────────────────
        fwd_mids: dict[int, Optional[float]] = {}
        fwd_bids: dict[int, Optional[float]] = {}
        mfe:      dict[int, float]           = {}
        mae:      dict[int, float]           = {}
        pnl:      dict[int, Optional[float]] = {}

        for z in Z_WINDOWS_S:
            target_ts = row.ts + z
            # Walk forward to find the row closest to (but not after) target_ts.
            best_fwd: Optional[_Row] = None
            best_peak_bid = float("-inf")
            best_trough_bid = float("inf")

            for fwd in rows[i:]:
                if fwd.ts > target_ts + 2:   # 2s tolerance
                    break
                if fwd.ts >= row.ts:
                    b = _bid(fwd, side)
                    m = _mid(fwd, side)
                    if b is not None:
                        best_peak_bid   = max(best_peak_bid, b)
                        best_trough_bid = min(best_trough_bid, b)
                    if fwd.ts <= target_ts:
                        best_fwd = fwd

            fwd_mid = _mid(best_fwd, side) if best_fwd else None
            fwd_bid = _bid(best_fwd, side) if best_fwd else None

            fwd_mids[z] = round(float(fwd_mid) - float(mid_end), 4) if fwd_mid is not None else None
            fwd_bids[z] = fwd_bid

            if entry_sim is not None and best_peak_bid > float("-inf"):
                mfe[z] = round(best_peak_bid - exit_sub - entry_sim, 4)
                mae[z] = round(best_trough_bid - exit_sub - entry_sim, 4)
                pnl[z] = round(float(fwd_bid) - exit_sub - entry_sim, 4) if fwd_bid is not None else None
            else:
                mfe[z] = mae[z] = None
                pnl[z] = None

        # ── Insert event row ──────────────────────────────────────────────────
        execute_query(
            """
            INSERT INTO repricing_discrepancy_events (
                run_id, market_ticker, event_time, implied_side,
                btc_price_start, btc_price_end, btc_move, btc_move_abs,
                qualified_n40, qualified_n50, qualified_n60,
                contract_mid_start, contract_mid_end, contract_change,
                underreaction_x01, underreaction_x02, underreaction_x03,
                contract_ask, contract_bid, contract_spread, spread_bucket,
                time_remaining_s, contract_age_s, volatility_regime,
                entry_sim,
                fwd_mid_10s, fwd_mid_20s, fwd_mid_30s, fwd_mid_45s, fwd_mid_60s,
                fwd_mfe_10s, fwd_mfe_20s, fwd_mfe_30s, fwd_mfe_45s, fwd_mfe_60s,
                fwd_mae_10s, fwd_mae_20s, fwd_mae_30s, fwd_mae_45s, fwd_mae_60s,
                pnl_10s,     pnl_20s,     pnl_30s,     pnl_45s,     pnl_60s
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            """,
            (
                run_id, ticker, row.recorded_at, side,
                round(start_row.btc, 2), round(row.btc, 2),
                round(btc_move, 2), round(btc_abs, 2),
                True, btc_abs >= 50, btc_abs >= 60,
                round(mid_start, 6), round(mid_end, 6), round(contract_change, 6),
                under_x01, under_x02, under_x03,
                ask_now, bid_now, sprd_val, spread_bucket(sprd_val),
                row.time_remaining_s, row.contract_age_s, vol_regime,
                entry_sim,
                fwd_mids.get(10), fwd_mids.get(20), fwd_mids.get(30),
                fwd_mids.get(45), fwd_mids.get(60),
                mfe.get(10), mfe.get(20), mfe.get(30), mfe.get(45), mfe.get(60),
                mae.get(10), mae.get(20), mae.get(30), mae.get(45), mae.get(60),
                pnl.get(10), pnl.get(20), pnl.get(30), pnl.get(45), pnl.get(60),
            ),
        )

        counters.n_burst_events += 1
        if under_x01: counters.n_under_x01 += 1
        if under_x02: counters.n_under_x02 += 1
        if under_x03: counters.n_under_x03 += 1

        # Apply cooldown for this side.
        blocked_until[side] = row.ts + cooldown_s

        logger.debug(
            "Burst %s %s | btc_move=%+.1f | contract_change=%+.4f | "
            "under_x01=%s under_x02=%s",
            ticker, side, btc_move, contract_change, under_x01, under_x02,
        )

    logger.info(
        "Market %-35s | rows=%4d bursts=%3d under_x01=%3d under_x02=%3d",
        ticker, len(rows), counters.n_burst_events, counters.n_under_x01, counters.n_under_x02,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="BTC-Kalshi repricing discrepancy backtest.")
    ap.add_argument("--start",          help="Start date YYYY-MM-DD")
    ap.add_argument("--end",            help="End date YYYY-MM-DD")
    ap.add_argument("--limit-markets",  type=int, help="Limit to N markets (smoke test)")
    ap.add_argument("--slippage",       default=SLIPPAGE_MODE,
                    choices=list(_SLIPPAGE), help="Slippage model (default: %(default)s)")
    ap.add_argument("--cooldown",       type=float, default=DEFAULT_COOLDOWN,
                    help="Per-side cooldown seconds (default: %(default)s)")
    ap.add_argument("--notes",          default="", help="Optional run notes")
    args = ap.parse_args()

    entry_add, exit_sub = _SLIPPAGE[args.slippage]

    markets = _load_markets(args.start, args.end, args.limit_markets)
    if not markets:
        logger.error("No markets found.  Run the logger first to collect data.")
        sys.exit(1)
    logger.info("Loaded %d market(s)", len(markets))

    # ── Create run row ────────────────────────────────────────────────────────
    run_id = insert_and_get_id(
        """
        INSERT INTO repricing_discrepancy_runs
            (burst_window_s, n_thresholds, x_thresholds, z_windows,
             slippage_mode, contract_price_field, cooldown_s, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            int(BURST_WINDOW_S),
            json.dumps(N_THRESHOLDS),
            json.dumps(X_THRESHOLDS),
            json.dumps(Z_WINDOWS_S),
            args.slippage,
            "mid",
            int(args.cooldown),
            args.notes or None,
        ),
    )
    logger.info("Run #%d created", run_id)

    counters = _RunCounters()

    for mkt in markets:
        ticker = mkt["market_ticker"]
        rows   = _load_rows(ticker, args.start, args.end)
        if len(rows) < 20:
            logger.debug("Skipping %s — only %d rows", ticker, len(rows))
            continue

        _process_market(
            mkt, rows,
            entry_add=entry_add, exit_sub=exit_sub,
            slippage_mode=args.slippage,
            cooldown_s=args.cooldown,
            run_id=run_id,
            counters=counters,
        )

    # ── Update run summary ────────────────────────────────────────────────────
    execute_query(
        """
        UPDATE repricing_discrepancy_runs SET
            data_start          = %s,
            data_end            = %s,
            n_markets           = %s,
            n_burst_events      = %s,
            n_underreaction_x01 = %s,
            n_underreaction_x02 = %s,
            n_underreaction_x03 = %s
        WHERE id = %s
        """,
        (
            counters.data_start, counters.data_end,
            counters.n_markets,
            counters.n_burst_events,
            counters.n_under_x01, counters.n_under_x02, counters.n_under_x03,
            run_id,
        ),
    )

    logger.info(
        "Run #%d complete — markets=%d snapshots=%d bursts=%d "
        "under_x01=%d under_x02=%d under_x03=%d",
        run_id,
        counters.n_markets, counters.n_snapshots, counters.n_burst_events,
        counters.n_under_x01, counters.n_under_x02, counters.n_under_x03,
    )
    print(f"\nRun ID: {run_id}")
    print(f"Pass to report: python scripts/repricing_discrepancy_report.py --run-id {run_id}")


if __name__ == "__main__":
    main()
