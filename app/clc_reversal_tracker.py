"""
clc_reversal_tracker.py — Live shadow-tracking for the cheap-losing-contract
reversal research family: cheap_losing_contract_reversal_trail/v1 (early window)
and cheap_losing_contract_late_reversal/v1 (last 1–5 minutes).  Both rules share
the identical five-profile exit simulation; see _TRACKED_RULES.

WATCH-ONLY RESEARCH.  NO paper trade and NO real order is ever placed.  For each
watch-only signal of this rule, the tracker buys (hypothetically) the cheap
*losing* contract and simulates FIVE independent exit profiles against the same
observed bid path, writing one row per (signal x profile) into
`clc_reversal_observations`.

Fill model (NOT mid)
--------------------
  entry_price_simulated = losing-side ask at signal + slippage
  exit_price_simulated  = losing-side bid at exit   - slippage
  pnl                   = exit_price_simulated - entry_price_simulated

Two reference frames, each used where appropriate:
  • Reversal-probability races (+2c/-2c, +3c/-2c, +4c/-3c) are measured on the
    *contract's own bid move* from its signal-time bid — i.e. how far the
    contract reversed in cents, independent of entry cost.  These feed the
    self-referential ReversalProbabilityProvider.
  • Exit-profile P&L is realized: entry = ask+slip, exit = bid-slip, so
    expectancy already accounts for spread + slippage.

Exit profiles
-------------
  A fixed_20pct_stop_15pct   tp +20% / sl -15%                 / timeout 60s
  B fixed_15pct_stop_10pct   tp +15% / sl -10%                 / timeout 60s
  C ride_then_trail_20pct_2c hard -15% / arm +20% / trail 0.02 / timeout 60s
  D ride_then_trail_15pct_2c hard -12% / arm +15% / trail 0.02 / timeout 60s
  E ride_then_trail_20pct_1c hard -15% / arm +20% / trail 0.01 / timeout 60s
(all percentages are of entry_price_simulated; trail distances are in dollars
of bid.)

Completion: 60s elapsed, near-expiry (<=30s remaining), or market rollover.  An
open profile at completion exits at the last bid with the terminal reason.  A
profile also exits immediately if volatility turns "violent" while its P&L is
negative or near break-even.  On restart, any ACTIVE rows are closed
'recovered_after_restart'.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.db import execute_query, insert_and_get_id
from app.strategies import Signal

logger = logging.getLogger(__name__)


# ── Tracking parameters ─────────────────────────────────────────────────────────

_TIMEOUT_S       = 60.0
_NEAR_EXPIRY_S   = 30.0
_VIOLENT_BE_EPS  = 0.01   # "near break-even": realized pnl at or below this is not safe

# Race thresholds (favorable cents before adverse cents) on raw bid move.
_R22_TP, _R22_SL = 0.02, 0.02
_R32_TP, _R32_SL = 0.03, 0.02
_R43_TP, _R43_SL = 0.04, 0.03

# Canonical profile whose row carries the full-path race outcomes / acts as the
# single per-signal row for the reversal-probability lookup.
_PRIMARY_PROFILE = "fixed_20pct_stop_15pct"

_TRACKED_RULES: frozenset[str] = frozenset({
    "cheap_losing_contract_reversal_trail/v1",  # early-window variant
    "cheap_losing_contract_late_reversal/v1",   # late-window variant (last 1–5 min)
})

# Slippage (mirrors app.paper_trader; kept local to avoid importing the trader).
_SLIPPAGE: dict[str, tuple[float, float]] = {
    "optimistic": (0.00, 0.00),
    "realistic":  (0.01, 0.01),
    "harsh":      (0.02, 0.02),
}


# ── Exit-profile configuration ───────────────────────────────────────────────────

@dataclass(frozen=True)
class _ProfileCfg:
    name:                 str
    kind:                 str                # "fixed" | "trail"
    tp_pct:               Optional[float] = None   # fixed take-profit (frac of entry)
    sl_pct:               Optional[float] = None   # fixed stop-loss   (frac of entry)
    hard_stop_pct:        Optional[float] = None   # trail hard stop   (frac of entry)
    trail_activation_pct: Optional[float] = None   # trail arm level   (frac of entry)
    trail_cents:          Optional[float] = None   # trail distance    (dollars of bid)


_PROFILE_CFGS: list[_ProfileCfg] = [
    _ProfileCfg("fixed_20pct_stop_15pct",   "fixed", tp_pct=0.20, sl_pct=0.15),
    _ProfileCfg("fixed_15pct_stop_10pct",   "fixed", tp_pct=0.15, sl_pct=0.10),
    _ProfileCfg("ride_then_trail_20pct_2c", "trail", hard_stop_pct=0.15,
                trail_activation_pct=0.20, trail_cents=0.02),
    _ProfileCfg("ride_then_trail_15pct_2c", "trail", hard_stop_pct=0.12,
                trail_activation_pct=0.15, trail_cents=0.02),
    _ProfileCfg("ride_then_trail_20pct_1c", "trail", hard_stop_pct=0.15,
                trail_activation_pct=0.20, trail_cents=0.01),
]


# ── In-memory state ──────────────────────────────────────────────────────────────

@dataclass
class _Profile:
    cfg:          _ProfileCfg
    row_id:       int
    closed:       bool            = False
    trail_active: bool            = False
    peak_bid:     float           = 0.0      # max bid seen up to exit
    low_bid:      float           = 0.0      # min bid seen up to exit
    time_to_peak: Optional[float] = None
    exit_reason:  Optional[str]   = None
    exit_bid:     Optional[float] = None


@dataclass
class _Obs:
    signal_id:     int
    market_ticker: str
    side:          str            # losing (bought) side
    entry_sim:     float          # ask + entry slippage
    entry_bid:     float          # losing-side bid at signal time (race reference)
    exit_sub:      float          # exit slippage
    entry_time:    datetime
    profiles:      list[_Profile]
    last_bid:      float = 0.0      # most recent observed bid (for terminal exits)
    n_updates:     int = 0

    # Path-level race latches (full window): None | "plus" | "minus".
    race22: Optional[str] = None
    race32: Optional[str] = None
    race43: Optional[str] = None


# ── Tracker ──────────────────────────────────────────────────────────────────────

class CLCReversalTracker:
    """Manages the lifecycle of cheap_losing_contract_reversal_trail/v1 observations."""

    def __init__(self, slippage_mode: str = "realistic") -> None:
        if slippage_mode not in _SLIPPAGE:
            raise ValueError(
                f"Unknown slippage_mode '{slippage_mode}'. "
                f"Valid: {sorted(_SLIPPAGE)}"
            )
        self._slippage_mode = slippage_mode
        self._entry_add, self._exit_sub = _SLIPPAGE[slippage_mode]
        self._active: dict[int, _Obs] = {}   # signal_id → _Obs
        self._recover_orphans()
        logger.info(
            "CLCReversalTracker ready — slippage=%s tracked rules: %s",
            slippage_mode, ", ".join(sorted(_TRACKED_RULES)),
        )

    @property
    def active_count(self) -> int:
        return len(self._active)

    # ── Register ─────────────────────────────────────────────────────────────────

    def register(
        self,
        signal: Signal,
        signal_id: int,
        contract_prices: dict[str, dict],
    ) -> Optional[list[int]]:
        """
        Begin tracking a watch-only signal.  Inserts five ACTIVE rows (one per
        exit profile) and returns their ids, or None if the rule is not tracked
        or context is missing.
        """
        rule_key = f"{signal.rule_name}/{signal.rule_version}"
        if rule_key not in _TRACKED_RULES:
            return None
        if signal_id in self._active:
            logger.debug("CLCReversalTracker: signal #%d already tracked", signal_id)
            return None

        extra = signal.extra or {}
        lq    = contract_prices.get(signal.side, {})
        ask   = lq.get("ask_price", extra.get("losing_contract_ask"))
        bid   = lq.get("bid_price", extra.get("losing_contract_bid"))
        if not ask or ask <= 0 or bid is None:
            logger.warning("CLCReversalTracker: skip — no usable ask/bid for %s %s",
                           rule_key, signal.side)
            return None

        entry_sim = round(float(ask) + self._entry_add, 4)
        entry_bid = round(float(bid), 4)
        now       = datetime.now(timezone.utc)

        profiles: list[_Profile] = []
        for cfg in _PROFILE_CFGS:
            row_id = self._insert_row(signal, signal_id, extra, cfg, entry_sim, now)
            profiles.append(_Profile(
                cfg=cfg, row_id=row_id, peak_bid=entry_bid, low_bid=entry_bid,
            ))

        self._active[signal_id] = _Obs(
            signal_id     = signal_id,
            market_ticker = signal.market_ticker,
            side          = signal.side,
            entry_sim     = entry_sim,
            entry_bid     = entry_bid,
            exit_sub      = self._exit_sub,
            entry_time    = now,
            profiles      = profiles,
            last_bid      = entry_bid,
        )
        logger.info(
            "CLC observation started — signal #%d losing=%s entry_sim=%.4f "
            "entry_bid=%.4f (5 profiles)",
            signal_id, signal.side, entry_sim, entry_bid,
        )
        return [p.row_id for p in profiles]

    # ── Update ───────────────────────────────────────────────────────────────────

    def update(
        self,
        market_ticker: str,
        contract_prices: dict[str, dict],
        time_remaining_seconds: float,
        volatility_regime: str,
    ) -> list[int]:
        """
        Advance all active observations by one cycle.  Returns the list of
        signal_ids finalized this cycle.
        """
        finalized: list[int] = []
        now = datetime.now(timezone.utc)

        for signal_id, obs in list(self._active.items()):
            # Market rolled over while tracking — finalize on last data.
            if obs.market_ticker != market_ticker:
                self._finalize(obs, "market_rollover", now)
                del self._active[signal_id]
                finalized.append(signal_id)
                continue

            quotes  = contract_prices.get(obs.side, {})
            bid     = quotes.get("bid_price")
            if bid is None:
                continue
            bid     = float(bid)
            elapsed = (now - obs.entry_time).total_seconds()

            self._apply_update(obs, bid, elapsed, volatility_regime)

            if elapsed >= _TIMEOUT_S:
                self._finalize(obs, "timeout", now)
                del self._active[signal_id]
                finalized.append(signal_id)
            elif time_remaining_seconds <= _NEAR_EXPIRY_S:
                self._finalize(obs, "near_expiry", now)
                del self._active[signal_id]
                finalized.append(signal_id)

        return finalized

    # ── Per-cycle bookkeeping ──────────────────────────────────────────────────────

    def _apply_update(
        self, obs: _Obs, bid: float, elapsed: float, vol_regime: str
    ) -> None:
        obs.n_updates += 1
        obs.last_bid   = round(bid, 4)

        # Path-level races on the contract's bid move from signal time.
        fav_raw = bid - obs.entry_bid
        obs.race22 = self._resolve_race(obs.race22, fav_raw, _R22_TP, _R22_SL)
        obs.race32 = self._resolve_race(obs.race32, fav_raw, _R32_TP, _R32_SL)
        obs.race43 = self._resolve_race(obs.race43, fav_raw, _R43_TP, _R43_SL)

        for p in obs.profiles:
            if p.closed:
                continue

            # Per-profile watermarks (up to exit).
            if bid > p.peak_bid:
                p.peak_bid      = round(bid, 4)
                p.time_to_peak  = round(elapsed, 3)
            if bid < p.low_bid:
                p.low_bid = round(bid, 4)

            # Realized P&L if we exited now.
            exit_pnl = (bid - obs.exit_sub) - obs.entry_sim

            # 0. Violent-volatility safety exit (negative or near break-even).
            if vol_regime == "violent" and exit_pnl <= _VIOLENT_BE_EPS:
                self._close(p, "violent_vol_exit", bid)
                continue

            cfg = p.cfg
            if cfg.kind == "fixed":
                if bid >= obs.entry_sim * (1.0 + cfg.tp_pct):
                    self._close(p, "take_profit", bid)
                elif bid <= obs.entry_sim * (1.0 - cfg.sl_pct):
                    self._close(p, "stop_loss", bid)
            else:  # trail
                if not p.trail_active:
                    if bid <= obs.entry_sim * (1.0 - cfg.hard_stop_pct):
                        self._close(p, "hard_stop", bid)
                    elif bid >= obs.entry_sim * (1.0 + cfg.trail_activation_pct):
                        p.trail_active = True   # arm; exit decided on later pullback
                else:
                    if bid <= p.peak_bid - cfg.trail_cents:
                        self._close(p, "trailing_stop", bid)

    @staticmethod
    def _close(p: _Profile, reason: str, bid: float) -> None:
        p.closed      = True
        p.exit_reason = reason
        p.exit_bid    = round(bid, 4)

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

    # ── Finalize + persist ─────────────────────────────────────────────────────────

    def _finalize(self, obs: _Obs, terminal_reason: str, now: datetime) -> None:
        hit22 = obs.race22 == "plus"
        hit32 = obs.race32 == "plus"
        hit43 = obs.race43 == "plus"

        for p in obs.profiles:
            # Any still-open profile exits at the last observed bid with the
            # terminal reason (timeout / near_expiry / market_rollover).
            if not p.closed:
                self._close(p, terminal_reason, obs.last_bid)

            exit_bid   = p.exit_bid if p.exit_bid is not None else obs.entry_bid
            exit_sim   = round(exit_bid - obs.exit_sub, 4)
            pnl        = round(exit_sim - obs.entry_sim, 4)
            pnl_pct    = round(pnl / obs.entry_sim, 4) if obs.entry_sim else None
            mfe        = round((p.peak_bid - obs.exit_sub) - obs.entry_sim, 4)
            mae        = round((p.low_bid  - obs.exit_sub) - obs.entry_sim, 4)

            execute_query(
                """
                UPDATE clc_reversal_observations
                SET
                    trail_activated             = %s,
                    peak_contract_price         = %s,
                    max_favorable_excursion     = %s,
                    max_adverse_excursion       = %s,
                    time_to_peak                = %s,
                    exit_price_simulated        = %s,
                    exit_reason                 = %s,
                    pnl                         = %s,
                    pnl_percent                 = %s,
                    hit_plus_2c_before_minus_2c = %s,
                    hit_plus_3c_before_minus_2c = %s,
                    hit_plus_4c_before_minus_3c = %s,
                    n_updates                   = %s,
                    status                      = 'COMPLETE',
                    complete_reason             = %s,
                    completed_at                = %s
                WHERE id = %s
                """,
                (
                    (p.trail_active if p.cfg.kind == "trail" else None),
                    p.peak_bid, mfe, mae, p.time_to_peak,
                    exit_sim, p.exit_reason, pnl, pnl_pct,
                    hit22, hit32, hit43,
                    obs.n_updates,
                    terminal_reason,
                    now,
                    p.row_id,
                ),
            )

        primary = next(
            (p for p in obs.profiles if p.cfg.name == _PRIMARY_PROFILE),
            obs.profiles[0],
        )
        logger.info(
            "CLC observation #%d complete (%s) — races +2c=%s +3c=%s +4c=%s | "
            "primary[%s] exit=%s pnl=%+.4f",
            obs.signal_id, terminal_reason, hit22, hit32, hit43,
            primary.cfg.name, primary.exit_reason,
            round((((primary.exit_bid if primary.exit_bid is not None else obs.entry_bid)
                    - obs.exit_sub) - obs.entry_sim), 4),
        )

    # ── INSERT one profile row ─────────────────────────────────────────────────────

    def _insert_row(
        self,
        signal: Signal,
        signal_id: int,
        extra: dict,
        cfg: _ProfileCfg,
        entry_sim: float,
        now: datetime,
    ) -> int:
        return insert_and_get_id(
            """
            INSERT INTO clc_reversal_observations (
                signal_id, market_ticker, rule_name, rule_version,
                setup_type, market_phase, market_age_seconds, time_remaining_seconds,
                side_bought, winning_side, losing_side,
                btc_price, target_price,
                raw_gap_z_score, adverse_z_score,
                raw_momentum_score, adverse_directional_momentum_score,
                losing_contract_ask, losing_contract_bid, losing_contract_spread,
                losing_contract_low_since_open, losing_contract_bounce_from_low,
                historical_reversal_probability, similar_sample_count, confidence_label,
                volatility_regime, whipsaw_score, hour_block, day_name,
                slippage_mode, entry_price_simulated,
                exit_profile, is_primary_path_row,
                status, recorded_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                'ACTIVE', %s
            )
            """,
            (
                signal_id, signal.market_ticker, signal.rule_name, signal.rule_version,
                extra.get("setup_type"), extra.get("market_phase"),
                extra.get("market_age_seconds"), signal.time_remaining_seconds,
                signal.side, extra.get("winning_side"), extra.get("losing_side"),
                signal.btc_price, signal.target_price,
                extra.get("raw_gap_z_score"), extra.get("adverse_z_score"),
                extra.get("raw_momentum_score"),
                extra.get("adverse_directional_momentum_score"),
                extra.get("losing_contract_ask"), extra.get("losing_contract_bid"),
                extra.get("losing_contract_spread"),
                extra.get("losing_contract_low_since_open"),
                extra.get("losing_contract_bounce_from_low"),
                extra.get("historical_reversal_probability"),
                extra.get("similar_sample_count"), extra.get("confidence_label"),
                extra.get("volatility_regime"), extra.get("whipsaw_score"),
                signal.entry_hour_block, signal.entry_day_name,
                self._slippage_mode, entry_sim,
                cfg.name, (cfg.name == _PRIMARY_PROFILE),
                signal.recorded_at,
            ),
        )

    # ── Recovery ─────────────────────────────────────────────────────────────────

    def _recover_orphans(self) -> None:
        """Close any rows left ACTIVE by a previous process."""
        now = datetime.now(timezone.utc)
        execute_query(
            """
            UPDATE clc_reversal_observations
            SET status          = 'COMPLETE',
                complete_reason = 'recovered_after_restart',
                completed_at    = %s
            WHERE status = 'ACTIVE'
            """,
            (now,),
        )
