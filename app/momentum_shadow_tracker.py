"""
app/momentum_shadow_tracker.py — Live shadow tracking for the frozen ht120s_tp3c
momentum-repricing candidate.

RESEARCH ONLY.  No orders are placed.  No trading endpoints are called.

Alignment with backtest
-----------------------
Signal detection delegates directly to backtest.signals.detect_signal() with
the same ExperimentConfig loaded from experiments/momentum-lag.json.  The
lookback-window logic (two-pointer walk, min 3 rows, 80% actual lookback) is
replicated here exactly as in backtest/signals.iter_signals().

Exit rules for the frozen ht120s_tp3c profile mirror backtest/executor.py:
  1. Profit target: live bid >= entry_ask + 0.03 -> EXIT_PROFIT_TARGET
  2. Optional stop loss when MOMENTUM_LIVE_STOP_LOSS_ENABLED=true
  3. Fixed-time horizon: elapsed >= 120s -> exit at first available bid
  4. Grace period: up to 10s past horizon with no bid -> EXIT_UNEXECUTABLE

PnL formula:
  gross = exit_bid - entry_ask
  net   = gross - estimated_fee_per_contract - estimated_slippage_cents

All thresholds come from ExperimentConfig; no magic constants in this module.
"""
from __future__ import annotations

import collections
import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app import config
from app.db import execute_query, insert_and_get_id
from app.features import Tick
from backtest.config import load_config, ExperimentConfig
from backtest.loader import MarketInfo, MarketRow, get_bid
from backtest.signals import detect_signal

logger = logging.getLogger(__name__)

# ── Frozen profile config ──────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent.parent / "experiments" / "momentum-lag.json"

# Exit profile label for the frozen candidate.
_PROFILE = "ht120s_tp3c"
_HOLD_S  = 120
_TP      = 0.03    # profit target (dollar fraction)

# Grace window at horizon with no bid -> unexecutable (mirrors executor.py).
_GRACE_S = 10.0

# Keep at most this many seconds of recent rows in the detection window.
# Must be > lookback_seconds (30s).  60s gives a comfortable margin.
_TRIM_WINDOW_S = 60.0


def _load_shadow_config() -> ExperimentConfig:
    """
    Load base config from experiments/momentum-lag.json and override to the
    single frozen candidate profile (ht120s_tp3c).
    Falls back to constructed defaults if the file is missing so the process
    can start even in stripped environments.
    """
    if _CONFIG_PATH.exists():
        cfg = load_config(_CONFIG_PATH)
    else:
        logger.warning(
            "MomentumShadowTracker: experiments/momentum-lag.json not found — "
            "using built-in defaults (lookback=30s, btc_min=20, reprice_max=0.03)"
        )
        cfg = ExperimentConfig(name="momentum-lag-shadow-fallback")

    # Freeze to the single candidate profile; all other parameters remain as-is.
    cfg.holding_period_seconds = [_HOLD_S]
    cfg.profit_target_cents    = [_TP]
    return cfg


_SHADOW_CONFIG: ExperimentConfig = _load_shadow_config()

# ── Exit reason labels (mirror executor.py) ───────────────────────────────────

EXIT_PROFIT_TARGET = "profit_target"
EXIT_STOP_LOSS     = "stop_loss"
EXIT_FIXED_TIME    = "fixed_time"
EXIT_UNEXECUTABLE  = "unexecutable"

RESULT_WIN          = "win"
RESULT_LOSS         = "loss"
RESULT_BREAKEVEN    = "breakeven"
RESULT_UNEXECUTABLE = "unexecutable"


# ── Active shadow trade state ─────────────────────────────────────────────────

@dataclass
class _ActiveShadow:
    trade_id:      int
    market_id:     int
    contract_id:   int
    market_ticker: str
    side:          str
    signal_at:     datetime
    signal_ts:     float      # Unix epoch, from signal.signal_ts
    entry_ask:     float
    horizon_ts:    float      # signal_ts + _HOLD_S
    peak_bid:      float      # MFE watermark (float("-inf") until first update)
    trough_bid:    float      # MAE watermark (float("inf")  until first update)
    grace_start:   Optional[float]  # ts when we first hit the horizon without a bid


# ── MomentumShadowTracker ─────────────────────────────────────────────────────

class MomentumShadowTracker:
    """
    Manages live shadow trades for the frozen ht120s_tp3c momentum profile.

    Call on_tick() once per poll cycle after DB writes complete.
    """

    def __init__(self) -> None:
        # Rolling window of recent MarketRows for the current market.
        self._window: collections.deque[MarketRow] = collections.deque()
        self._current_market_id: Optional[str] = None

        # Per-contract cooldown: {contract_id -> earliest ts for next signal}
        self._cooldown_until: dict[int, float] = {}

        # Active shadow trades: {contract_id -> _ActiveShadow}
        # At most one active trade per contract at a time.
        self._active: dict[int, _ActiveShadow] = {}

        self._recover_orphans()
        logger.info(
            "MomentumShadowTracker ready | profile=%s  lookback=%ds  ht=%ds  "
            "tp=%.2f  btc_min=%.0f  reprice_max=%.3f  cooldown=%.0fs",
            _PROFILE,
            _SHADOW_CONFIG.lookback_seconds,
            _HOLD_S,
            _TP,
            _SHADOW_CONFIG.minimum_btc_move_toward_target,
            _SHADOW_CONFIG.maximum_contract_reprice_during_lookback,
            _SHADOW_CONFIG.cooldown_seconds,
        )

    # ── Public interface ──────────────────────────────────────────────────────

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
        Process one live tick.  Safe to call even if the migration has not been
        run yet (errors are caught and logged, not re-raised).

        Ordering per tick:
          1. Market rollover detection — resets window / cooldowns.
          2. Build a MarketRow from live data (no DB read).
          3. Advance all active shadow trades (check exits using new row).
          4. Append new row to rolling window; trim old rows.
          5. Signal detection (reuses backtest.signals.detect_signal).
        """
        if target_price is None:
            return

        # ── Market rollover ───────────────────────────────────────────────────
        if market_id != self._current_market_id:
            if self._current_market_id is not None:
                logger.info(
                    "shadow rollover: %s -> %s (active=%d, clearing window+cooldowns)",
                    self._current_market_id, market_id, len(self._active),
                )
                # Any trade open on the old market gets marked unexecutable.
                for shadow in list(self._active.values()):
                    try:
                        self._finalize(shadow, EXIT_UNEXECUTABLE, None, captured_at)
                    except Exception as exc:
                        logger.warning("shadow finalize-on-rollover failed: %s", exc)
                self._active.clear()
            self._current_market_id = market_id
            self._window.clear()
            self._cooldown_until.clear()

        # ── Build MarketRow from live data ────────────────────────────────────
        row = _build_market_row(
            snapshot_id  = snapshot_id,
            snapshot_seq = snapshot_seq,
            captured_at  = captured_at,
            btc_price    = btc_price,
            tte          = tte,
            prices       = prices,
            btc_ticks    = btc_ticks,
        )

        # ── Advance active trades (exit checks at this tick) ──────────────────
        # This mirrors executor.py walking forward_rows AFTER the signal index.
        # A trade opened in the current detect() call won't be checked until the
        # next on_tick(), preserving strict lookahead safety.
        if self._active:
            try:
                self._advance_active(row, captured_at)
            except Exception as exc:
                logger.warning("shadow advance-active failed: %s", exc)

        # ── Append to rolling window; trim old rows ───────────────────────────
        self._window.append(row)
        cutoff = row.ts - _TRIM_WINDOW_S
        while self._window and self._window[0].ts < cutoff:
            self._window.popleft()

        # ── Signal detection ──────────────────────────────────────────────────
        try:
            self._detect(
                market_db_id  = market_db_id,
                market_id     = market_id,
                market_ticker = market_ticker,
                contract_ids  = contract_ids,
                target_price  = target_price,
                row           = row,
            )
        except Exception as exc:
            logger.warning("shadow detect failed: %s", exc)

    @property
    def active_count(self) -> int:
        return len(self._active)

    # ── Signal detection ──────────────────────────────────────────────────────

    def _detect(
        self,
        market_db_id:  int,
        market_id:     str,
        market_ticker: str,
        contract_ids:  dict[str, int],
        target_price:  float,
        row:           MarketRow,
    ) -> None:
        """
        Attempt YES/NO signal detection at the current row.

        Replicates the lookback-window logic from backtest/signals.iter_signals():
          - lookback_row = oldest row in the 30s window
          - window_len >= 3
          - actual_lookback >= 0.8 * lookback_seconds
        Then calls detect_signal() from backtest/signals directly.
        """
        window = list(self._window)

        # Find rows within the lookback window (mirrors iter_signals lo_ptr logic).
        lookback_cutoff = row.ts - _SHADOW_CONFIG.lookback_seconds
        in_window = [r for r in window if r.ts >= lookback_cutoff]

        if len(in_window) < 3:
            return

        lookback_row    = in_window[0]
        actual_lookback = row.ts - lookback_row.ts
        if actual_lookback < 0.8 * _SHADOW_CONFIG.lookback_seconds:
            return

        market_info = MarketInfo(
            id           = market_db_id,
            market_id    = market_id,
            target_price = target_price,
            opens_at     = None,
            closes_at    = None,
            settles_at   = None,
            status       = "open",
            contract_ids = contract_ids,
        )

        for side in ("YES", "NO"):
            contract_id = contract_ids.get(side)
            if contract_id is None:
                continue

            # Cooldown check.
            if row.ts < self._cooldown_until.get(contract_id, float("-inf")):
                remaining = self._cooldown_until[contract_id] - row.ts
                logger.debug(
                    "shadow SKIP cooldown | %s %s | remaining=%.1fs",
                    market_ticker, side, remaining,
                )
                continue

            # Overlap check: one active trade per contract.
            if contract_id in self._active:
                logger.debug(
                    "shadow SKIP active-overlap | %s %s", market_ticker, side,
                )
                continue

            sig = detect_signal(
                row            = row,
                lookback_row   = lookback_row,
                market         = market_info,
                contract_id    = contract_id,
                side           = side,
                config         = _SHADOW_CONFIG,
                snapshot_index = len(window) - 1,
            )

            if sig is not None:
                self._open_shadow(sig, market_ticker)
                self._cooldown_until[contract_id] = (
                    row.ts + _SHADOW_CONFIG.cooldown_seconds
                )

    # ── Shadow trade lifecycle ────────────────────────────────────────────────

    def _open_shadow(self, sig, market_ticker: str) -> None:
        """Insert a new ACTIVE shadow trade record and register it in memory."""
        target_ask = round(sig.entry_ask + _TP, 4)
        now = datetime.now(timezone.utc)

        trade_id = insert_and_get_id(
            """
            INSERT INTO momentum_shadow_trades (
                market_id, contract_id, market_ticker, side,
                signal_at, entry_at,
                entry_ask, entry_bid, entry_spread,
                btc_price_at_entry, target_price,
                distance_from_target, side_adjusted_distance,
                btc_move_toward_side, contract_move_during_lookback,
                volatility_1m, volatility_5m,
                time_remaining_at_entry,
                exit_profile, target_ask_price,
                estimated_fees_cents, estimated_slippage_cents,
                status, created_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s,
                %s, %s,
                %s, %s,
                'ACTIVE', %s
            )
            """,
            (
                sig.market_id, sig.contract_id, market_ticker, sig.side,
                sig.signal_at, sig.signal_at,
                sig.entry_ask, sig.entry_bid, sig.entry_spread,
                sig.btc_price, sig.target_price,
                sig.distance_from_target, sig.side_adjusted_distance,
                sig.btc_move_toward_side, sig.contract_move_during_lookback,
                sig.volatility_1m, sig.volatility_5m,
                sig.time_remaining_s,
                _PROFILE, target_ask,
                _SHADOW_CONFIG.estimated_fee_per_contract,
                _SHADOW_CONFIG.estimated_slippage_cents,
                now,
            ),
        )

        shadow = _ActiveShadow(
            trade_id      = trade_id,
            market_id     = sig.market_id,
            contract_id   = sig.contract_id,
            market_ticker = market_ticker,
            side          = sig.side,
            signal_at     = sig.signal_at,
            signal_ts     = sig.signal_ts,
            entry_ask     = sig.entry_ask,
            horizon_ts    = sig.signal_ts + _HOLD_S,
            peak_bid      = float("-inf"),
            trough_bid    = float("inf"),
            grace_start   = None,
        )
        self._active[sig.contract_id] = shadow

        logger.info(
            "shadow OPENED #%d | %s %s | ask=%.3f target=%.3f "
            "btc_move=%+.1f contract_move=%+.3f tte=%ss",
            trade_id, market_ticker, sig.side,
            sig.entry_ask, target_ask,
            sig.btc_move_toward_side, sig.contract_move_during_lookback,
            sig.time_remaining_s if sig.time_remaining_s is not None else "--",
        )

    def _advance_active(self, row: MarketRow, captured_at: datetime) -> None:
        """
        Advance all active shadow trades by one tick.

        Exit rule order:
          1. Profit target: bid >= entry_ask + 0.03
          2. Optional stop loss: bid <= entry_ask - configured stop
          3. Fixed-time horizon: elapsed >= 120s + first available bid
          4. Grace period: no bid for >10s past horizon -> unexecutable
        """
        for contract_id, shadow in list(self._active.items()):
            bid = get_bid(row, shadow.side)

            # Track excursion within the 120s holding window.
            if row.ts <= shadow.horizon_ts and bid is not None:
                if bid > shadow.peak_bid:
                    shadow.peak_bid   = bid
                if bid < shadow.trough_bid:
                    shadow.trough_bid = bid

            exit_reason: Optional[str] = None
            exit_bid:    Optional[float] = None

            # 1. Profit target.
            if bid is not None and bid >= shadow.entry_ask + _TP:
                exit_reason = EXIT_PROFIT_TARGET
                exit_bid    = bid

            # 2. Optional stop loss.
            elif (
                bid is not None
                and config.MOMENTUM_LIVE_STOP_LOSS_ENABLED
                and bid <= shadow.entry_ask - abs(config.MOMENTUM_LIVE_STOP_LOSS_CENTS)
            ):
                exit_reason = EXIT_STOP_LOSS
                exit_bid    = bid

            # 3. Fixed-time horizon.
            elif row.ts >= shadow.horizon_ts:
                if bid is not None:
                    exit_reason = EXIT_FIXED_TIME
                    exit_bid    = bid
                else:
                    # No bid at horizon: start grace period clock.
                    if shadow.grace_start is None:
                        shadow.grace_start = row.ts
                    # Grace period exceeded: mark unexecutable.
                    if row.ts > shadow.grace_start + _GRACE_S:
                        exit_reason = EXIT_UNEXECUTABLE
                        exit_bid    = None

            if exit_reason is not None:
                self._finalize(shadow, exit_reason, exit_bid, captured_at)
                del self._active[contract_id]

    def _finalize(
        self,
        shadow:      _ActiveShadow,
        exit_reason: str,
        exit_bid:    Optional[float],
        exit_at:     datetime,
    ) -> None:
        """Compute PnL, persist result, log."""
        gross: Optional[float] = None
        net:   Optional[float] = None
        if exit_bid is not None:
            gross = round(exit_bid - shadow.entry_ask, 6)
            net   = round(
                gross
                - _SHADOW_CONFIG.estimated_fee_per_contract
                - _SHADOW_CONFIG.estimated_slippage_cents,
                6,
            )

        result_status = _classify_pnl(net)

        mfe = (
            round(shadow.peak_bid   - shadow.entry_ask, 6)
            if shadow.peak_bid   > float("-inf") else None
        )
        mae = (
            round(shadow.trough_bid - shadow.entry_ask, 6)
            if shadow.trough_bid < float("inf")  else None
        )

        holding_s: Optional[float] = (
            round(exit_at.timestamp() - shadow.signal_ts, 1)
            if exit_at is not None else None
        )

        execute_query(
            """
            UPDATE momentum_shadow_trades
            SET
                status                   = 'COMPLETE',
                exit_at                  = %s,
                exit_bid                 = %s,
                exit_reason              = %s,
                holding_seconds          = %s,
                max_favorable_excursion  = %s,
                max_adverse_excursion    = %s,
                gross_pnl_cents          = %s,
                net_pnl_cents            = %s,
                result_status            = %s
            WHERE id = %s
            """,
            (
                exit_at, exit_bid, exit_reason, holding_s,
                mfe, mae,
                gross, net,
                result_status,
                shadow.trade_id,
            ),
        )

        if exit_reason == EXIT_PROFIT_TARGET:
            logger.info(
                "shadow PROFIT TARGET #%d | %s %s | "
                "ask=%.3f bid=%.3f net=%+.4f hold=%.1fs",
                shadow.trade_id, shadow.market_ticker, shadow.side,
                shadow.entry_ask, exit_bid or 0.0, net or 0.0, holding_s or 0.0,
            )
        elif exit_reason == EXIT_FIXED_TIME:
            logger.info(
                "shadow TIMEOUT #%d | %s %s | "
                "ask=%.3f bid=%.3f net=%+.4f hold=%.1fs",
                shadow.trade_id, shadow.market_ticker, shadow.side,
                shadow.entry_ask, exit_bid or 0.0, net or 0.0, holding_s or 0.0,
            )
        elif exit_reason == EXIT_UNEXECUTABLE:
            logger.warning(
                "shadow UNEXECUTABLE #%d | %s %s | no bid at exit horizon",
                shadow.trade_id, shadow.market_ticker, shadow.side,
            )
        else:
            logger.info(
                "shadow EXIT #%d | %s %s | reason=%s bid=%s net=%s hold=%ss",
                shadow.trade_id, shadow.market_ticker, shadow.side,
                exit_reason, exit_bid, net, holding_s,
            )

    def _recover_orphans(self) -> None:
        """Close any ACTIVE rows left open by a previous process on restart."""
        now = datetime.now(timezone.utc)
        try:
            execute_query(
                """
                UPDATE momentum_shadow_trades
                SET
                    status        = 'COMPLETE',
                    exit_reason   = 'recovered_after_restart',
                    result_status = 'unexecutable',
                    exit_at       = %s
                WHERE status = 'ACTIVE'
                """,
                (now,),
            )
        except Exception as exc:
            logger.warning(
                "MomentumShadowTracker: recover_orphans skipped "
                "(table may not exist yet — run the migration): %s",
                exc,
            )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_market_row(
    snapshot_id:  int,
    snapshot_seq: int,
    captured_at:  datetime,
    btc_price:    float,
    tte:          Optional[int],
    prices:       dict[str, dict],
    btc_ticks:    list[Tick],
) -> MarketRow:
    """
    Build a MarketRow from live data without a DB round-trip.

    Volatility fields (btc_return_stddev_1m / _5m) are computed from the
    in-memory BTC tick buffer using the same formula as _insert_market_metrics()
    in app/main.py, so the values are consistent with what will be stored in
    market_metrics once the DB write completes.
    """
    yes = prices.get("YES", {})
    no  = prices.get("NO",  {})

    return MarketRow(
        snapshot_id  = snapshot_id,
        snapshot_seq = snapshot_seq,
        captured_at  = captured_at,
        ts           = captured_at.timestamp(),
        btc_price    = btc_price,
        time_remaining_s = tte,
        yes_bid    = _f(yes.get("bid_price")),
        yes_ask    = _f(yes.get("ask_price")),
        yes_spread = _f(yes.get("spread")),
        no_bid     = _f(no.get("bid_price")),
        no_ask     = _f(no.get("ask_price")),
        no_spread  = _f(no.get("spread")),
        # Distance and return metrics are not needed for entry detection.
        distance_from_target     = None,
        distance_from_target_pct = None,
        is_above_target          = None,
        btc_return_30s           = None,
        btc_return_1m            = None,
        btc_return_5m            = None,
        btc_return_stddev_1m     = _return_stddev(btc_ticks, 60.0),
        btc_return_stddev_5m     = _return_stddev(btc_ticks, 300.0),
    )


def _f(v) -> Optional[float]:
    return None if v is None else float(v)


def _return_stddev(ticks: list[Tick], window_s: float) -> Optional[float]:
    """
    Std dev of per-tick returns over a rolling window.
    Identical to the _return_stddev closure in app/main.py:_insert_market_metrics().
    """
    if len(ticks) < 2:
        return None
    cutoff = ticks[-1].ts - window_s
    window = [t for t in ticks if t.ts >= cutoff]
    if len(window) < 2:
        return None
    rets = [
        window[i].price / window[i - 1].price - 1
        for i in range(1, len(window))
        if window[i - 1].price > 0
    ]
    if len(rets) < 2:
        return None
    try:
        return round(statistics.stdev(rets), 8)
    except statistics.StatisticsError:
        return None


def _classify_pnl(net: Optional[float]) -> str:
    if net is None:
        return RESULT_UNEXECUTABLE
    if net > 1e-5:
        return RESULT_WIN
    if net < -1e-5:
        return RESULT_LOSS
    return RESULT_BREAKEVEN
