"""
dcvrb_tracker.py — Live shadow-tracking for delayed_contract_value_reversal_bounce/v1.

WATCH-ONLY RESEARCH.  No paper trade and no real order is ever placed.  For each
watch-only signal of this rule the tracker simulates FOUR exit tests (A/B/C/D) ×
THREE entry-timing comparison versions (v1/v2/v3) = 12 rows per signal, all written
to `dcvrb_observations`.

Entry timing versions
---------------------
  v1  immediate    — hypothetical entry at watch_start_contract_ask + slippage
                     (when the contract first touched 0.20–0.40)
  v2  2c+1c        — entry at signal-time ask + slippage (always valid; this is the
                     main signal trigger: flush ≥ 2¢ + bounce ≥ 1¢)
  v3  5c+2c        — same entry price as v2, but only for signals where flush ≥ 5¢
                     AND bounce ≥ 2¢ at signal time; rows marked v3_qualified=FALSE
                     are inserted with NULL simulated P&L fields for set-coverage

Fill model (NOT mid)
--------------------
  entry_price_simulated = contract ask at entry time + slippage
  exit_price_simulated  = contract bid at exit time  - slippage
  pnl                   = exit_sim - entry_sim

For v1: because the entry happened before the signal fires, the pre-signal minimum
ask (local_low_since_watch from the signal's extra dict) is used to compute a
pre_signal_mae.  If the stop-loss level was already breached in the pre-signal
window the row is marked v1_stopped_out_pre_signal=TRUE and the simulated outcome
is 'stopped_out_pre_signal'.

Exit tests
----------
  test_a  tp +0.03  sl -0.02  timeout  45s
  test_b  tp +0.04  sl -0.03  timeout  60s
  test_c  tp +0.05  sl -0.03  timeout  75s
  test_d  tp +0.06  sl -0.04  timeout  90s

Structure stop
--------------
After entry, if bid drops below local_low_since_watch the row's
structure_stop_hit flag is set to TRUE (the contract broke through its flush
nadir).  The structure stop is informational — it does not override the active
exit test outcome.

Completion: max(timeout_s) elapsed = 90s, near-expiry (≤ 30s remaining), or
market rollover.  On restart, any ACTIVE rows are closed 'recovered_after_restart'.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.db import execute_query, insert_and_get_id
from app.strategies import Signal

logger = logging.getLogger(__name__)


# ── Tracking parameters ──────────────────────────────────────────────────────────

_TRACKED_RULE = "delayed_contract_value_reversal_bounce/v1"

_NEAR_EXPIRY_S = 30.0

# Slippage mirrors app.paper_trader.
_SLIPPAGE: dict[str, tuple[float, float]] = {
    "optimistic": (0.00, 0.00),
    "realistic":  (0.01, 0.01),
    "harsh":      (0.02, 0.02),
}


# ── Exit test configuration ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class _ExitTestCfg:
    name:      str    # test_a | test_b | test_c | test_d
    tp:        float  # take-profit offset (dollars, positive)
    sl:        float  # stop-loss offset   (dollars, positive)
    timeout_s: float  # max hold time in seconds


_EXIT_TESTS: list[_ExitTestCfg] = [
    _ExitTestCfg("test_a", tp=0.03, sl=0.02, timeout_s=45.0),
    _ExitTestCfg("test_b", tp=0.04, sl=0.03, timeout_s=60.0),
    _ExitTestCfg("test_c", tp=0.05, sl=0.03, timeout_s=75.0),
    _ExitTestCfg("test_d", tp=0.06, sl=0.04, timeout_s=90.0),
]

# Maximum timeout across all tests — used for final completion gate.
_MAX_TIMEOUT_S = max(t.timeout_s for t in _EXIT_TESTS)

# Comparison version names.
_VERSIONS = ("v1", "v2", "v3")


# ── In-memory state ───────────────────────────────────────────────────────────────

@dataclass
class _Row:
    """One simulated row: a (signal × version × exit_test) combination."""
    row_id:              int
    version:             str       # v1 | v2 | v3
    cfg:                 _ExitTestCfg
    entry_sim:           float     # ask_at_entry + entry_slip
    closed:              bool      = False
    structure_stop_hit:  bool      = False
    peak_bid:            float     = 0.0
    low_bid:             float     = 0.0
    time_to_peak:        Optional[float] = None
    time_to_tp:          Optional[float] = None
    exit_reason:         Optional[str]   = None
    exit_bid:            Optional[float] = None

    # Pre-signal v1 fields (populated on registration, never updated after)
    v1_pre_signal_mae:            Optional[float] = None
    v1_stopped_out_pre_signal:    bool             = False

    # v3 qualification flag
    v3_qualified:        bool = True


@dataclass
class _Obs:
    signal_id:              int
    market_ticker:          str
    side:                   str
    local_low_since_watch:  float    # flush nadir — structure-stop reference
    entry_bid:              float    # bid at signal time (all versions share same path)
    exit_sub:               float    # exit slippage to subtract
    entry_time:             datetime
    rows:                   list[_Row]
    last_bid:               float = 0.0
    n_updates:              int   = 0


# ── Tracker ───────────────────────────────────────────────────────────────────────

class DCVRBTracker:
    """Manages shadow-tracking for delayed_contract_value_reversal_bounce/v1."""

    def __init__(self, slippage_mode: str = "realistic") -> None:
        if slippage_mode not in _SLIPPAGE:
            raise ValueError(
                f"Unknown slippage_mode '{slippage_mode}'. "
                f"Valid: {sorted(_SLIPPAGE)}"
            )
        self._slippage_mode   = slippage_mode
        self._entry_add, self._exit_sub = _SLIPPAGE[slippage_mode]
        self._active: dict[int, _Obs] = {}   # signal_id → _Obs
        self._recover_orphans()
        logger.info(
            "DCVRBTracker ready — slippage=%s rule=%s",
            slippage_mode, _TRACKED_RULE,
        )

    @property
    def active_count(self) -> int:
        return len(self._active)

    # ── Register ───────────────────────────────────────────────────────────────

    def register(
        self,
        signal: Signal,
        signal_id: int,
        contract_prices: dict[str, dict],
    ) -> Optional[list[int]]:
        """
        Begin tracking a watch-only signal.  Inserts 12 rows (4 exit tests ×
        3 comparison versions) and returns their ids, or None if not tracked.
        """
        rule_key = f"{signal.rule_name}/{signal.rule_version}"
        if rule_key != _TRACKED_RULE:
            return None
        if signal_id in self._active:
            logger.debug("DCVRBTracker: signal #%d already tracked", signal_id)
            return None

        extra = signal.extra or {}
        lq    = contract_prices.get(signal.side, {})
        ask   = lq.get("ask_price", signal.ask_price)
        bid   = lq.get("bid_price", signal.bid_price)
        if not ask or ask <= 0 or bid is None:
            logger.warning("DCVRBTracker: skip — no usable ask/bid for signal #%d", signal_id)
            return None

        ask   = float(ask)
        bid   = float(bid)
        now   = datetime.now(timezone.utc)

        # Shared path reference
        local_low = float(extra.get("local_low_since_watch") or bid)
        v3_qual   = bool(extra.get("v3_qualified", False))

        # Pre-compute v1 entry and MAE (same for all tests within v1)
        watch_ask = float(extra.get("watch_start_contract_ask") or ask)
        v1_entry  = round(watch_ask + self._entry_add, 4)
        v1_pre_mae = round((local_low - self._exit_sub) - v1_entry, 4)

        rows: list[_Row] = []
        for version in _VERSIONS:
            for cfg in _EXIT_TESTS:
                if version == "v1":
                    entry_sim = v1_entry
                    pre_mae   = v1_pre_mae
                    stopped_pre = pre_mae <= -cfg.sl
                else:
                    entry_sim   = round(ask + self._entry_add, 4)
                    pre_mae     = None
                    stopped_pre = False

                row_id = self._insert_row(
                    signal, signal_id, extra, version, cfg, entry_sim,
                    v3_qual, pre_mae, stopped_pre, now,
                )

                row = _Row(
                    row_id                     = row_id,
                    version                    = version,
                    cfg                        = cfg,
                    entry_sim                  = entry_sim,
                    peak_bid                   = bid,
                    low_bid                    = bid,
                    v3_qualified               = v3_qual,
                    v1_pre_signal_mae          = pre_mae,
                    v1_stopped_out_pre_signal  = stopped_pre,
                )

                # v3 rows that don't qualify are closed immediately (no tracking)
                if version == "v3" and not v3_qual:
                    row.closed = True
                    row.exit_reason = "v3_not_qualified"
                    row.exit_bid    = bid

                # v1 already stopped out before signal — close immediately
                if version == "v1" and stopped_pre:
                    row.closed = True
                    row.exit_reason = "stopped_out_pre_signal"
                    row.exit_bid    = local_low

                rows.append(row)

        self._active[signal_id] = _Obs(
            signal_id             = signal_id,
            market_ticker         = signal.market_ticker,
            side                  = signal.side,
            local_low_since_watch = local_low,
            entry_bid             = bid,
            exit_sub              = self._exit_sub,
            entry_time            = now,
            rows                  = rows,
            last_bid              = bid,
        )
        logger.info(
            "DCVRB observation started — signal #%d losing=%s ask=%.4f bid=%.4f "
            "local_low=%.4f v3_qualified=%s (12 rows)",
            signal_id, signal.side, ask, bid, local_low, v3_qual,
        )
        return [r.row_id for r in rows]

    # ── Update ─────────────────────────────────────────────────────────────────

    def update(
        self,
        market_ticker: str,
        contract_prices: dict[str, dict],
        time_remaining_seconds: float,
    ) -> list[int]:
        """
        Advance all active observations by one cycle.
        Returns signal_ids finalized this cycle.
        """
        finalized: list[int] = []
        now = datetime.now(timezone.utc)

        for signal_id, obs in list(self._active.items()):
            if obs.market_ticker != market_ticker:
                self._finalize(obs, "market_rollover", now)
                del self._active[signal_id]
                finalized.append(signal_id)
                continue

            quotes = contract_prices.get(obs.side, {})
            bid    = quotes.get("bid_price")
            if bid is None:
                continue
            bid     = float(bid)
            elapsed = (now - obs.entry_time).total_seconds()

            self._apply_update(obs, bid, elapsed)

            # Check overall completion: max timeout OR near expiry
            all_closed = all(r.closed for r in obs.rows)
            if all_closed or elapsed >= _MAX_TIMEOUT_S:
                reason = "all_tests_closed" if all_closed else "max_timeout"
                self._finalize(obs, reason, now)
                del self._active[signal_id]
                finalized.append(signal_id)
            elif time_remaining_seconds <= _NEAR_EXPIRY_S:
                self._finalize(obs, "near_expiry", now)
                del self._active[signal_id]
                finalized.append(signal_id)

        return finalized

    # ── Per-cycle bookkeeping ───────────────────────────────────────────────────

    def _apply_update(self, obs: _Obs, bid: float, elapsed: float) -> None:
        obs.n_updates += 1
        obs.last_bid   = round(bid, 4)

        for row in obs.rows:
            if row.closed:
                continue

            # Update path watermarks
            if bid > row.peak_bid:
                row.peak_bid     = round(bid, 4)
                row.time_to_peak = round(elapsed, 3)
            if bid < row.low_bid:
                row.low_bid = round(bid, 4)

            # Structure stop: bid dropped below the flush nadir
            if bid < obs.local_low_since_watch:
                row.structure_stop_hit = True

            cfg = row.cfg
            # Skip if timeout elapsed for this specific test
            if elapsed >= cfg.timeout_s:
                self._close_row(row, "timeout", bid)
                continue

            tp_level = row.entry_sim + cfg.tp
            sl_level = row.entry_sim - cfg.sl

            if bid >= tp_level:
                row.time_to_tp = round(elapsed, 3)
                self._close_row(row, "take_profit", bid)
            elif bid <= sl_level:
                self._close_row(row, "stop_loss", bid)

    @staticmethod
    def _close_row(row: _Row, reason: str, bid: float) -> None:
        row.closed      = True
        row.exit_reason = reason
        row.exit_bid    = round(bid, 4)

    # ── Finalize + persist ──────────────────────────────────────────────────────

    def _finalize(self, obs: _Obs, terminal_reason: str, now: datetime) -> None:
        for row in obs.rows:
            if not row.closed:
                self._close_row(row, terminal_reason, obs.last_bid)

            exit_bid   = row.exit_bid if row.exit_bid is not None else obs.entry_bid
            exit_sim   = round(exit_bid - obs.exit_sub, 4)

            if row.v1_stopped_out_pre_signal:
                # Pre-signal stop: P&L computed from v1 entry to local_low
                pnl = row.v1_pre_signal_mae
            elif row.version == "v3" and not row.v3_qualified:
                pnl = None
            else:
                pnl = round(exit_sim - row.entry_sim, 4)

            pnl_pct = round(pnl / row.entry_sim, 4) if (pnl is not None and row.entry_sim) else None
            mfe     = round((row.peak_bid - obs.exit_sub) - row.entry_sim, 4)
            mae     = round((row.low_bid  - obs.exit_sub) - row.entry_sim, 4)

            # For v1 stopped out pre-signal, the "forward" MFE/MAE is not meaningful
            if row.v1_stopped_out_pre_signal:
                hit_tp   = False
                hit_sl   = True
                timed_out = False
                sim_exit_reason = "stopped_out_pre_signal"
            elif row.version == "v3" and not row.v3_qualified:
                hit_tp = hit_sl = timed_out = None
                sim_exit_reason = "v3_not_qualified"
                mfe = mae = pnl = pnl_pct = None
            else:
                hit_tp        = row.exit_reason == "take_profit"
                hit_sl        = row.exit_reason == "stop_loss"
                timed_out     = row.exit_reason in ("timeout", "max_timeout", "near_expiry",
                                                     "market_rollover", "all_tests_closed")
                sim_exit_reason = row.exit_reason

            execute_query(
                """
                UPDATE dcvrb_observations
                SET
                    hit_take_profit_before_stop  = %s,
                    hit_stop_before_take_profit  = %s,
                    timed_out                    = %s,
                    structure_stop_hit           = %s,
                    max_favorable_excursion      = %s,
                    max_adverse_excursion        = %s,
                    time_to_peak                 = %s,
                    time_to_profit_target        = %s,
                    simulated_pnl                = %s,
                    simulated_pnl_percent        = %s,
                    exit_price_simulated         = %s,
                    exit_reason_simulated        = %s,
                    n_updates                    = %s,
                    status                       = 'COMPLETE',
                    complete_reason              = %s,
                    completed_at                 = %s
                WHERE id = %s
                """,
                (
                    hit_tp, hit_sl, timed_out,
                    row.structure_stop_hit,
                    mfe, mae,
                    row.time_to_peak, row.time_to_tp,
                    pnl, pnl_pct,
                    (exit_sim if pnl is not None else None),
                    sim_exit_reason,
                    obs.n_updates,
                    terminal_reason,
                    now,
                    row.row_id,
                ),
            )

        logger.info(
            "DCVRB observation #%d complete (%s) — %d rows closed",
            obs.signal_id, terminal_reason, len(obs.rows),
        )

    # ── INSERT one row ──────────────────────────────────────────────────────────

    def _insert_row(
        self,
        signal: Signal,
        signal_id: int,
        extra: dict,
        version: str,
        cfg: _ExitTestCfg,
        entry_sim: float,
        v3_qualified: bool,
        v1_pre_signal_mae: Optional[float],
        v1_stopped_out_pre_signal: bool,
        now: datetime,
    ) -> int:
        return insert_and_get_id(
            """
            INSERT INTO dcvrb_observations (
                signal_id, market_ticker, rule_name, rule_version, side,
                contract_open_price, contract_recent_high,
                watch_start_contract_ask, watch_start_contract_bid,
                drop_from_open, drop_from_recent_high,
                spread_at_watch, volatility_60s_at_watch,
                local_low_since_watch, drop_from_watch_start,
                extra_flush, bounce_from_local_low,
                extra_flush_bucket, bounce_bucket,
                strong_bounce, price_bucket,
                contract_ask, contract_bid, spread,
                contract_price_change_5s, contract_price_change_10s,
                contract_price_change_30s,
                market_age_seconds, time_remaining_seconds,
                btc_price, target_price,
                raw_gap_z_score, directional_gap_z_score,
                raw_momentum_score, directional_momentum_score,
                btc_velocity_10s, btc_velocity_30s,
                volatility_30s, volatility_60s,
                volatility_regime, hour_block, day_name, timezone_used,
                comparison_version, v3_qualified,
                slippage_mode, entry_price_simulated,
                exit_test, tp_abs, sl_abs, timeout_s,
                v1_pre_signal_mae, v1_stopped_out_pre_signal,
                status, recorded_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                'ACTIVE', %s
            )
            """,
            (
                signal_id, signal.market_ticker, signal.rule_name, signal.rule_version,
                signal.side,
                extra.get("contract_open_price"), extra.get("contract_recent_high"),
                extra.get("watch_start_contract_ask"), extra.get("watch_start_contract_bid"),
                extra.get("drop_from_open"), extra.get("drop_from_recent_high"),
                extra.get("spread_at_watch"), extra.get("volatility_60s_at_watch"),
                extra.get("local_low_since_watch"), extra.get("drop_from_watch_start"),
                extra.get("extra_flush"), extra.get("bounce_from_local_low"),
                extra.get("extra_flush_bucket"), extra.get("bounce_bucket"),
                extra.get("strong_bounce"), extra.get("price_bucket"),
                signal.ask_price, signal.bid_price, signal.spread,
                extra.get("contract_price_change_5s"), extra.get("contract_price_change_10s"),
                extra.get("contract_price_change_30s"),
                extra.get("market_age_seconds"), signal.time_remaining_seconds,
                signal.btc_price, signal.target_price,
                extra.get("raw_gap_z_score"), extra.get("directional_gap_z_score"),
                extra.get("raw_momentum_score"), extra.get("directional_momentum_score"),
                extra.get("btc_velocity_10s"), extra.get("btc_velocity_30s"),
                signal.volatility_30s, signal.volatility_60s,
                extra.get("volatility_regime"), signal.entry_hour_block,
                signal.entry_day_name, signal.timezone_used,
                version, v3_qualified,
                self._slippage_mode, entry_sim,
                cfg.name, cfg.tp, cfg.sl, cfg.timeout_s,
                v1_pre_signal_mae, v1_stopped_out_pre_signal,
                signal.recorded_at,
            ),
        )

    # ── Recovery ──────────────────────────────────────────────────────────────

    def _recover_orphans(self) -> None:
        """Close any rows left ACTIVE by a previous process."""
        now = datetime.now(timezone.utc)
        execute_query(
            """
            UPDATE dcvrb_observations
            SET status          = 'COMPLETE',
                complete_reason = 'recovered_after_restart',
                completed_at    = %s
            WHERE status = 'ACTIVE'
            """,
            (now,),
        )
