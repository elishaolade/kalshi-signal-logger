"""
observation_tracker.py — Live shadow-tracking for watch-only research signals.

NO paper trade and NO real order are ever placed.  For each watch-only signal
that opts in (currently early_overextension_reversal_scalp/v1), this tracker
follows the *losing-side* contract for ~60 seconds and records hypothetical
outcomes and exit simulations into the `signal_observations` table.

Reference price
---------------
The reference (`entry_ref`) is the losing-side MID at signal time.  Every
excursion and exit simulation is measured against it.  There is no fill or
slippage assumption — this is observation, not trading.

Tracked outcomes
----------------
  max_favorable_excursion / max_adverse_excursion   (mid − entry_ref)
  hit_plus_3c_before_minus_2c                        (+0.03 reached before -0.02)
  hit_plus_4c_before_minus_2c                        (+0.04 reached before -0.02)
  hit_plus_5c_before_minus_3c                        (+0.05 reached before -0.03)
  time_to_peak_s / time_to_plus_3c_s / time_to_stop_s
  did_make_new_low_after_signal                      (mid dropped below entry_ref)

Exit simulations (independent, first threshold wins; else timeout at 60s)
  sim_tp3_sl2   take_profit +0.03 / stop_loss -0.02
  sim_tp5_sl3   take_profit +0.05 / stop_loss -0.03
  sim_timeout60 pure 60s hold — pnl = last mid − entry_ref

Completion triggers: 60s elapsed, near-expiry (≤30s remaining), or market
rollover.  On restart, any rows left ACTIVE are closed as
'recovered_after_restart'.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.db import execute_query, insert_and_get_id
from app.strategies import Signal

logger = logging.getLogger(__name__)


# ── Tracking parameters ─────────────────────────────────────────────────────────

_OBS_TIMEOUT_S     = 60.0
_OBS_NEAR_EXPIRY_S = 30.0

# Race thresholds (favorable take-profit vs adverse stop, in dollars).
_R32_TP, _R32_SL = 0.03, 0.02   # +3c before -2c
_R42_TP, _R42_SL = 0.04, 0.02   # +4c before -2c
_R53_TP, _R53_SL = 0.05, 0.03   # +5c before -3c

# Exit-simulation thresholds.
_SIM1_TP, _SIM1_SL = 0.03, 0.02
_SIM2_TP, _SIM2_SL = 0.05, 0.03

# Canonical "stop" used for time_to_stop_s (the tighter -2c stop).
_STOP_FOR_TIMING = 0.02

# Only these rules are shadow-tracked.
_TRACKED_RULES: frozenset[str] = frozenset({
    "early_overextension_reversal_scalp/v1",
})


# ── In-memory observation state ──────────────────────────────────────────────────

@dataclass
class _Obs:
    obs_id:        int
    signal_id:     int
    market_ticker: str
    side:          str          # tracked (losing) side
    rule_key:      str
    entry_ref:     float        # losing-side mid at signal time
    entry_time:    datetime

    peak:          float
    low:           float
    last:          float
    n_updates:     int = 0

    mfe:           float = 0.0
    mae:           float = 0.0
    time_to_peak_s:    Optional[float] = None
    time_to_plus_3c_s: Optional[float] = None
    time_to_stop_s:    Optional[float] = None

    # Race resolutions: None (unresolved) | "plus" | "minus".
    race32: Optional[str] = None
    race42: Optional[str] = None
    race53: Optional[str] = None

    # Exit sims: outcome None until take_profit/stop_loss latches.
    sim1_outcome: Optional[str] = None
    sim1_pnl:     Optional[float] = None
    sim2_outcome: Optional[str] = None
    sim2_pnl:     Optional[float] = None


# ── ObservationTracker ───────────────────────────────────────────────────────────

class ObservationTracker:
    """Manages the lifecycle of watch-only signal observations."""

    def __init__(self) -> None:
        self._active: dict[int, _Obs] = {}   # obs_id → _Obs
        self._recover_orphans()
        logger.info("ObservationTracker ready — tracked rules: %s",
                    ", ".join(sorted(_TRACKED_RULES)))

    # ── Public interface ──────────────────────────────────────────────────────

    def register(
        self,
        signal: Signal,
        signal_id: int,
        contract_prices: dict[str, dict],
    ) -> Optional[int]:
        """
        Begin tracking a watch-only signal.  Returns the new
        signal_observations.id, or None if the rule is not tracked or the
        signal is missing the required context.
        """
        rule_key = f"{signal.rule_name}/{signal.rule_version}"
        if rule_key not in _TRACKED_RULES:
            return None

        extra = signal.extra or {}
        quotes    = contract_prices.get(signal.side, {})
        entry_ref = quotes.get("mid_price", extra.get("losing_mid_at_signal"))
        if not entry_ref or entry_ref <= 0:
            logger.warning(
                "ObservationTracker: skip — no usable mid for %s %s",
                rule_key, signal.side,
            )
            return None

        # Avoid duplicate concurrent observations for the same market/side/rule.
        for existing in self._active.values():
            if (
                existing.market_ticker == signal.market_ticker
                and existing.side      == signal.side
                and existing.rule_key  == rule_key
            ):
                logger.debug(
                    "ObservationTracker: skip — obs #%d already active for %s %s %s",
                    existing.obs_id, rule_key, signal.side, signal.market_ticker,
                )
                return None

        entry_ref = round(float(entry_ref), 4)
        now       = datetime.now(timezone.utc)

        obs_id = insert_and_get_id(
            """
            INSERT INTO signal_observations (
                signal_id, market_ticker, rule_name, rule_version, side,
                winning_side, losing_side,
                winning_change_60s, winning_dir_z,
                losing_bounce_from_low, losing_mom_10s,
                losing_ask_at_signal, market_age_seconds,
                entry_ref_price, entry_time,
                peak_price, low_price, last_price, n_updates,
                max_favorable_excursion, max_adverse_excursion,
                did_make_new_low_after_signal,
                status, recorded_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s,
                'ACTIVE', %s
            )
            """,
            (
                signal_id, signal.market_ticker, signal.rule_name,
                signal.rule_version, signal.side,
                extra.get("winning_side"), extra.get("losing_side"),
                extra.get("winning_change_60s"), extra.get("winning_dir_z"),
                extra.get("losing_bounce_from_low"), extra.get("losing_mom_10s"),
                extra.get("losing_ask_at_signal"), extra.get("market_age_seconds"),
                entry_ref, now,
                entry_ref, entry_ref, entry_ref, 0,
                0.0, 0.0,
                False,
                signal.recorded_at,
            ),
        )

        self._active[obs_id] = _Obs(
            obs_id        = obs_id,
            signal_id     = signal_id,
            market_ticker = signal.market_ticker,
            side          = signal.side,
            rule_key      = rule_key,
            entry_ref     = entry_ref,
            entry_time    = now,
            peak          = entry_ref,
            low           = entry_ref,
            last          = entry_ref,
        )
        logger.info(
            "Observation #%d started — %s losing=%s entry_ref=%.4f",
            obs_id, rule_key, signal.side, entry_ref,
        )
        return obs_id

    def update(
        self,
        market_ticker: str,
        contract_prices: dict[str, dict],
        time_remaining_seconds: float,
    ) -> list[int]:
        """
        Advance all active observations by one polling cycle.  Returns the list
        of obs_ids completed this cycle.
        """
        completed: list[int] = []
        now = datetime.now(timezone.utc)

        for obs_id, obs in list(self._active.items()):
            # Market rolled over while still tracking — finalize on last seen data.
            if obs.market_ticker != market_ticker:
                self._finalize(obs, "market_rollover", now)
                del self._active[obs_id]
                completed.append(obs_id)
                continue

            quotes = contract_prices.get(obs.side, {})
            mid    = quotes.get("mid_price", obs.last)
            elapsed = (now - obs.entry_time).total_seconds()

            self._apply_update(obs, float(mid), elapsed)

            if elapsed >= _OBS_TIMEOUT_S:
                self._finalize(obs, "timeout_60s", now)
                del self._active[obs_id]
                completed.append(obs_id)
            elif time_remaining_seconds <= _OBS_NEAR_EXPIRY_S:
                self._finalize(obs, "near_expiry", now)
                del self._active[obs_id]
                completed.append(obs_id)

        return completed

    @property
    def active_count(self) -> int:
        return len(self._active)

    # ── Internal: per-tick update ───────────────────────────────────────────────

    def _apply_update(self, obs: _Obs, mid: float, elapsed: float) -> None:
        obs.last       = round(mid, 4)
        obs.n_updates += 1
        fav = mid - obs.entry_ref      # favorable (>0) / adverse (<0)

        # Watermarks + excursions
        if mid > obs.peak:
            obs.peak = round(mid, 4)
            obs.time_to_peak_s = round(elapsed, 3)
        if mid < obs.low:
            obs.low = round(mid, 4)
        if fav > obs.mfe:
            obs.mfe = round(fav, 4)
        if fav < obs.mae:
            obs.mae = round(fav, 4)

        # First-hit timings
        if obs.time_to_plus_3c_s is None and fav >= 0.03:
            obs.time_to_plus_3c_s = round(elapsed, 3)
        if obs.time_to_stop_s is None and fav <= -_STOP_FOR_TIMING:
            obs.time_to_stop_s = round(elapsed, 3)

        # Race resolutions (+Nc before -Mc)
        obs.race32 = self._resolve_race(obs.race32, fav, _R32_TP, _R32_SL)
        obs.race42 = self._resolve_race(obs.race42, fav, _R42_TP, _R42_SL)
        obs.race53 = self._resolve_race(obs.race53, fav, _R53_TP, _R53_SL)

        # Exit simulations (latch on first threshold)
        if obs.sim1_outcome is None:
            if fav >= _SIM1_TP:
                obs.sim1_outcome, obs.sim1_pnl = "take_profit", _SIM1_TP
            elif fav <= -_SIM1_SL:
                obs.sim1_outcome, obs.sim1_pnl = "stop_loss", -_SIM1_SL
        if obs.sim2_outcome is None:
            if fav >= _SIM2_TP:
                obs.sim2_outcome, obs.sim2_pnl = "take_profit", _SIM2_TP
            elif fav <= -_SIM2_SL:
                obs.sim2_outcome, obs.sim2_pnl = "stop_loss", -_SIM2_SL

    @staticmethod
    def _resolve_race(
        current: Optional[str], fav: float, tp: float, sl: float
    ) -> Optional[str]:
        if current is not None:
            return current
        if fav >= tp:
            return "plus"
        if fav <= -sl:
            return "minus"
        return None

    # ── Internal: finalize + persist ─────────────────────────────────────────────

    def _finalize(self, obs: _Obs, reason: str, now: datetime) -> None:
        last_fav = round(obs.last - obs.entry_ref, 4)

        # Unresolved exit sims time out at the last observed favorable.
        sim1_outcome = obs.sim1_outcome or "timeout"
        sim1_pnl     = obs.sim1_pnl if obs.sim1_pnl is not None else last_fav
        sim2_outcome = obs.sim2_outcome or "timeout"
        sim2_pnl     = obs.sim2_pnl if obs.sim2_pnl is not None else last_fav

        execute_query(
            """
            UPDATE signal_observations
            SET
                peak_price                    = %s,
                low_price                     = %s,
                last_price                    = %s,
                n_updates                     = %s,
                max_favorable_excursion       = %s,
                max_adverse_excursion         = %s,
                hit_plus_3c_before_minus_2c   = %s,
                hit_plus_4c_before_minus_2c   = %s,
                hit_plus_5c_before_minus_3c   = %s,
                time_to_peak_s                = %s,
                time_to_plus_3c_s             = %s,
                time_to_stop_s                = %s,
                did_make_new_low_after_signal = %s,
                sim_tp3_sl2_outcome           = %s,
                sim_tp3_sl2_pnl               = %s,
                sim_tp5_sl3_outcome           = %s,
                sim_tp5_sl3_pnl               = %s,
                sim_timeout60_pnl             = %s,
                status                        = 'COMPLETE',
                complete_reason               = %s,
                completed_at                  = %s
            WHERE id = %s
            """,
            (
                obs.peak, obs.low, obs.last, obs.n_updates,
                obs.mfe, obs.mae,
                obs.race32 == "plus",
                obs.race42 == "plus",
                obs.race53 == "plus",
                obs.time_to_peak_s, obs.time_to_plus_3c_s, obs.time_to_stop_s,
                obs.low < obs.entry_ref,
                sim1_outcome, sim1_pnl,
                sim2_outcome, sim2_pnl,
                last_fav,
                reason, now,
                obs.obs_id,
            ),
        )
        logger.info(
            "Observation #%d complete (%s) — MFE=%+.4f MAE=%+.4f "
            "tp3sl2=%s(%+.4f) tp5sl3=%s(%+.4f) hold60=%+.4f",
            obs.obs_id, reason, obs.mfe, obs.mae,
            sim1_outcome, sim1_pnl, sim2_outcome, sim2_pnl, last_fav,
        )

    def _recover_orphans(self) -> None:
        """Close any observations left ACTIVE by a previous process."""
        now = datetime.now(timezone.utc)
        execute_query(
            """
            UPDATE signal_observations
            SET status          = 'COMPLETE',
                complete_reason = 'recovered_after_restart',
                completed_at    = %s
            WHERE status = 'ACTIVE'
            """,
            (now,),
        )
