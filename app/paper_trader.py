"""
paper_trader.py — Simulate trade entries, exits, and intra-trade snapshots.

No real orders are placed.  All prices are derived from contract bid/ask quotes.

Slippage modes
--------------
  optimistic   entry = ask + 0.00   exit = bid - 0.00
  realistic    entry = ask + 0.01   exit = bid - 0.01   (default)
  harsh        entry = ask + 0.02   exit = bid - 0.02

Exit rules
----------
  cheap_reversal_scalp/v1
    take_profit     mid >= entry * 1.30   (+30% of entry price)
    stop_loss       mid <= entry - 0.03
    trailing_stop   mid <= peak - 0.03    (once peak >= entry + 0.03)
    timeout_60s     elapsed >= 60 seconds
    near_expiry     time_remaining <= 30 seconds

  premium_momentum_continuation/v1
    take_profit     mid >= entry + 0.06
    stop_loss       mid <= entry - 0.05
    trailing_stop   mid <= peak - 0.04   (always active from first tick)
    break_even_stop mid <= entry          (active after peak >= entry + 0.04)
    near_expiry     time_remaining <= 30 seconds
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from app.config import PAPER_POSITION_SIZE, SLIPPAGE_MODE
from app.db import execute_query, insert_and_get_id
from app.features import (
    Tick,
    btc_velocity     as _btc_velocity,
    directional_gap  as _directional_gap,
    gap              as _gap,
    momentum_score   as _momentum_score,
    reversal_score   as _reversal_score,
    rolling_stds     as _rolling_stds,
    z_from_target    as _z_from_target,
)
from app.strategies import Signal

logger = logging.getLogger(__name__)


# ── Slippage ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Slip:
    entry_add: float
    exit_sub:  float

_SLIPPAGE_MODES: dict[str, _Slip] = {
    "optimistic": _Slip(0.00, 0.00),
    "realistic":  _Slip(0.01, 0.01),
    "harsh":      _Slip(0.02, 0.02),
}


# ── In-memory open-trade state ────────────────────────────────────────────────

@dataclass
class OpenTrade:
    trade_id:       int
    signal_id:      int
    market_ticker:  str
    rule_key:       str       # "rule_name/rule_version"
    side:           str       # "YES" | "NO"
    entry_price:    float     # simulated entry (ask + slippage)
    ask_at_entry:   float     # raw ask captured at entry moment
    peak_price:     float     # highest mid seen while open
    lowest_price:   float     # lowest mid seen while open
    entry_time:     datetime
    break_even_activated: bool = False   # PMC: set once peak >= entry + 0.04


# ── Exit-condition functions ──────────────────────────────────────────────────
#
# Signature for every exit function:
#   (trade, mid, time_remaining_seconds, elapsed_seconds) -> exit_reason | None
#
# Conditions are evaluated in priority order; the first match wins.
# Using mid_price for condition checks so a single bad bid doesn't whipsaw out.

_CRS_TIMEOUT_S     = 60
_CRS_NEAR_EXPIRY_S = 30
_CRS_TP_FACTOR     = 1.30    # take profit at +30% of entry
_CRS_SL_ABS        = 0.03    # absolute stop loss
_CRS_TRAIL_DIST    = 0.03    # trailing distance from peak
_CRS_TRAIL_ARM     = 0.03    # peak must reach entry+this before trailing arms

_PMC_NEAR_EXPIRY_S = 30
_PMC_TP_ABS        = 0.06    # absolute take profit
_PMC_SL_ABS        = 0.05    # absolute stop loss
_PMC_TRAIL_DIST    = 0.04    # trailing distance from peak (always active)
_PMC_BE_THRESH     = 0.04    # gain required to arm break-even stop

ExitFn = Callable[["OpenTrade", float, float, float], Optional[str]]


def _exit_cheap_reversal_scalp(
    trade: OpenTrade,
    mid: float,
    time_remaining_seconds: float,
    elapsed_seconds: float,
) -> Optional[str]:
    entry = trade.entry_price
    peak  = trade.peak_price

    if elapsed_seconds >= _CRS_TIMEOUT_S:
        return "timeout_60s"
    if time_remaining_seconds <= _CRS_NEAR_EXPIRY_S:
        return "near_expiry"
    if mid >= entry * _CRS_TP_FACTOR:
        return "take_profit"
    if mid <= entry - _CRS_SL_ABS:
        return "stop_loss"
    if peak >= entry + _CRS_TRAIL_ARM and mid <= peak - _CRS_TRAIL_DIST:
        return "trailing_stop"
    return None


def _exit_premium_momentum_continuation(
    trade: OpenTrade,
    mid: float,
    time_remaining_seconds: float,
    elapsed_seconds: float,   # signature parity; not used by this rule
) -> Optional[str]:
    entry = trade.entry_price
    peak  = trade.peak_price

    if time_remaining_seconds <= _PMC_NEAR_EXPIRY_S:
        return "near_expiry"
    if mid >= entry + _PMC_TP_ABS:
        return "take_profit"
    if mid <= entry - _PMC_SL_ABS:
        return "stop_loss"
    if trade.break_even_activated and mid <= entry:
        return "break_even_stop"
    if mid <= peak - _PMC_TRAIL_DIST:
        return "trailing_stop"
    return None


_EXIT_DISPATCH: dict[str, ExitFn] = {
    "cheap_reversal_scalp/v1":          _exit_cheap_reversal_scalp,
    "premium_momentum_continuation/v1": _exit_premium_momentum_continuation,
}


# ── PaperTrader ───────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Manages the full lifecycle of simulated trades:
      1. open_trade()  — enter on a signal, insert paper_trades row
      2. update()      — called every polling cycle; checks exits, writes snapshots
      3. _apply_exit() — persist the closed state to the DB
    """

    def __init__(
        self,
        slippage_mode: str = SLIPPAGE_MODE,
        position_size: int = PAPER_POSITION_SIZE,
    ) -> None:
        if slippage_mode not in _SLIPPAGE_MODES:
            raise ValueError(
                f"Unknown slippage_mode '{slippage_mode}'. "
                f"Valid options: {sorted(_SLIPPAGE_MODES)}"
            )
        self._slip          = _SLIPPAGE_MODES[slippage_mode]
        self._position_size = position_size
        self._open_trades:  dict[int, OpenTrade] = {}   # trade_id → OpenTrade

        logger.info(
            "PaperTrader ready — mode=%s entry_slip=+%.2f exit_slip=-%.2f positions=%d",
            slippage_mode, self._slip.entry_add, self._slip.exit_sub, position_size,
        )
        self._close_recovered_open_trades()

    # ── Public interface ──────────────────────────────────────────────────────

    def open_trade(
        self,
        signal: Signal,
        signal_id: int,
        contract_prices: dict[str, dict],
    ) -> Optional[int]:
        """
        Simulate entering a trade from a fired signal.

        Args:
            signal:           Signal object produced by a strategy.
            signal_id:        The DB row id returned after inserting the signal.
            contract_prices:  Current {"YES": {bid,ask,mid,...}, "NO": {...}}.

        Returns the new paper_trades.id, or None when no exit rule is registered
        for this signal's rule_name/version.
        """
        rule_key = f"{signal.rule_name}/{signal.rule_version}"
        if rule_key not in _EXIT_DISPATCH:
            logger.warning(
                "open_trade: no exit rule for '%s' — trade skipped. "
                "Register it in _EXIT_DISPATCH to enable trading.",
                rule_key,
            )
            return None

        quotes   = contract_prices.get(signal.side, {})
        ask      = quotes.get("ask_price", 0.0)
        sim_entry = _clamp(ask + self._slip.entry_add)
        now      = datetime.now(timezone.utc)

        trade_id = insert_and_get_id(
            """
            INSERT INTO paper_trades (
                signal_id, market_ticker, rule_name, rule_version, side,
                entry_price, simulated_entry_price,
                peak_price, lowest_price,
                entry_time, position_size, followed_rules, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
            """,
            (
                signal_id,
                signal.market_ticker,
                signal.rule_name,
                signal.rule_version,
                signal.side,
                sim_entry,
                sim_entry,
                sim_entry,   # peak_price initialised to entry
                sim_entry,   # lowest_price initialised to entry
                now,
                self._position_size,
                True,
            ),
        )

        trade = OpenTrade(
            trade_id      = trade_id,
            signal_id     = signal_id,
            market_ticker = signal.market_ticker,
            rule_key      = rule_key,
            side          = signal.side,
            entry_price   = sim_entry,
            ask_at_entry  = ask,
            peak_price    = sim_entry,
            lowest_price  = sim_entry,
            entry_time    = now,
        )
        self._open_trades[trade_id] = trade

        logger.info(
            "Opened trade #%d  %s  %s  entry=%.4f  (ask=%.4f slip=+%.2f)",
            trade_id, rule_key, signal.side,
            sim_entry, ask, self._slip.entry_add,
        )
        return trade_id

    def update(
        self,
        market_ticker: str,
        contract_prices: dict[str, dict],
        btc_ticks: list[Tick],
        btc_price: float,
        target_price: float,
        time_remaining_seconds: float,
    ) -> list[int]:
        """
        Process one polling cycle for all open trades on `market_ticker`.

        Order of operations per trade:
          1. Update peak_price and lowest_price from current mid.
          2. Arm break-even stop if gain threshold reached (PMC).
          3. Evaluate exit conditions → get exit_reason or None.
          4. Write trade_snapshot (always, regardless of exit).
          5. If exit_reason: close trade in DB and remove from memory.

        Returns a list of trade_ids closed this cycle.
        """
        closed: list[int] = []
        now = datetime.now(timezone.utc)

        for trade_id, trade in list(self._open_trades.items()):
            if trade.market_ticker != market_ticker:
                self._apply_forced_exit(trade, "market_rollover", now)
                del self._open_trades[trade_id]
                closed.append(trade_id)
                continue

            quotes = contract_prices.get(trade.side, {})
            mid    = quotes.get("mid_price",  trade.entry_price)
            bid    = quotes.get("bid_price",  trade.entry_price)

            # ── 1. Update high / low watermarks ──────────────────────────────
            if mid > trade.peak_price:
                trade.peak_price = round(mid, 4)
            if mid < trade.lowest_price:
                trade.lowest_price = round(mid, 4)

            # ── 2. Arm break-even stop (PMC only) ────────────────────────────
            if (
                not trade.break_even_activated
                and (trade.peak_price - trade.entry_price) >= _PMC_BE_THRESH
            ):
                trade.break_even_activated = True
                logger.info(
                    "Trade #%d break-even stop armed  peak=%.4f  entry=%.4f",
                    trade_id, trade.peak_price, trade.entry_price,
                )

            # ── 3. Evaluate exit conditions ───────────────────────────────────
            elapsed     = (now - trade.entry_time).total_seconds()
            exit_fn     = _EXIT_DISPATCH[trade.rule_key]
            exit_reason = exit_fn(trade, mid, time_remaining_seconds, elapsed)

            # ── 4. Write snapshot (always) ────────────────────────────────────
            self._write_snapshot(
                trade, quotes, btc_ticks,
                btc_price, target_price, time_remaining_seconds, now,
            )

            # ── 5. Execute exit ───────────────────────────────────────────────
            if exit_reason:
                self._apply_exit(trade, bid, exit_reason, now)
                del self._open_trades[trade_id]
                closed.append(trade_id)

        return closed

    @property
    def open_count(self) -> int:
        """Number of trades currently held open in memory."""
        return len(self._open_trades)

    def open_trade_ids(self, market_ticker: Optional[str] = None) -> list[int]:
        """Return trade_ids of all currently open trades, optionally filtered."""
        if market_ticker is None:
            return list(self._open_trades)
        return [tid for tid, t in self._open_trades.items()
                if t.market_ticker == market_ticker]

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _apply_exit(
        self,
        trade: OpenTrade,
        bid: float,
        exit_reason: str,
        now: datetime,
    ) -> None:
        sim_exit  = _clamp(bid - self._slip.exit_sub)
        pnl       = round((sim_exit - trade.entry_price) * self._position_size, 6)
        pnl_pct   = round(
            (sim_exit - trade.entry_price) / trade.entry_price
            if trade.entry_price > 0 else 0.0,
            6,
        )
        total_slip = round(self._slip.entry_add + self._slip.exit_sub, 4)

        execute_query(
            """
            UPDATE paper_trades
            SET
                exit_price           = %s,
                simulated_exit_price = %s,
                peak_price           = %s,
                lowest_price         = %s,
                exit_time            = %s,
                exit_reason          = %s,
                pnl                  = %s,
                pnl_percent          = %s,
                total_slippage       = %s,
                status               = 'CLOSED'
            WHERE id = %s
            """,
            (
                sim_exit, sim_exit,
                trade.peak_price, trade.lowest_price,
                now, exit_reason,
                pnl, pnl_pct,
                total_slip,
                trade.trade_id,
            ),
        )

        logger.info(
            "Closed trade #%d  %s  %s  reason=%-18s  exit=%.4f  pnl=%+.4f  (%.1f%%)",
            trade.trade_id, trade.rule_key, trade.side,
            exit_reason, sim_exit, pnl, pnl_pct * 100,
        )

    def _apply_forced_exit(
        self,
        trade: OpenTrade,
        exit_reason: str,
        now: datetime,
    ) -> None:
        """
        Close a trade when its market is no longer active.

        This should be rare because normal near-expiry exits usually fire first.
        When a trade gets stranded across a market rollover, we no longer have a
        live quote for that old ticker, so close it at entry and mark the rule
        as not followed to keep the row out of strategy-quality conclusions.
        """
        execute_query(
            """
            UPDATE paper_trades
            SET
                exit_price           = entry_price,
                simulated_exit_price = simulated_entry_price,
                peak_price           = %s,
                lowest_price         = %s,
                exit_time            = %s,
                exit_reason          = %s,
                pnl                  = 0,
                pnl_percent          = 0,
                total_slippage       = %s,
                followed_rules       = FALSE,
                status               = 'CLOSED'
            WHERE id = %s
            """,
            (
                trade.peak_price,
                trade.lowest_price,
                now,
                exit_reason,
                round(self._slip.entry_add + self._slip.exit_sub, 4),
                trade.trade_id,
            ),
        )

        logger.warning(
            "Forced close trade #%d  %s  %s  reason=%s",
            trade.trade_id, trade.rule_key, trade.side, exit_reason,
        )

    def _close_recovered_open_trades(self) -> None:
        """
        Close DB rows left OPEN by a previous process.

        Open trade state is in memory. After a restart, those rows cannot be
        managed safely, so mark them closed at breakeven and exclude them from
        followed-rules analysis.
        """
        now = datetime.now(timezone.utc)
        execute_query(
            """
            UPDATE paper_trades
            SET
                exit_price           = entry_price,
                simulated_exit_price = simulated_entry_price,
                exit_time            = %s,
                exit_reason          = 'recovered_after_restart',
                pnl                  = 0,
                pnl_percent          = 0,
                total_slippage       = %s,
                followed_rules       = FALSE,
                status               = 'CLOSED'
            WHERE status = 'OPEN'
            """,
            (now, round(self._slip.entry_add + self._slip.exit_sub, 4)),
        )

    def _write_snapshot(
        self,
        trade: OpenTrade,
        quotes: dict,
        btc_ticks: list[Tick],
        btc_price: float,
        target_price: float,
        time_remaining_seconds: float,
        now: datetime,
    ) -> None:
        stds   = _rolling_stds(btc_ticks)
        std_30  = stds["std_30s"]
        std_60  = stds["std_60s"]
        std_120 = stds["std_120s"]

        execute_query(
            """
            INSERT INTO trade_snapshots (
                paper_trade_id, signal_id, market_ticker, side,
                btc_price, target_price, raw_gap, directional_gap,
                rolling_std_30s, rolling_std_60s, rolling_std_120s,
                z_from_target_30s, z_from_target_60s, z_from_target_120s,
                contract_price, bid_price, ask_price, spread,
                momentum_score, reversal_score, btc_velocity,
                time_remaining_seconds, recorded_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s
            )
            """,
            (
                trade.trade_id,
                trade.signal_id,
                trade.market_ticker,
                trade.side,
                btc_price,
                target_price,
                _gap(btc_price, target_price),
                _directional_gap(btc_price, target_price, trade.side),
                std_30, std_60, std_120,
                _z_from_target(btc_price, target_price, trade.side, std_30),
                _z_from_target(btc_price, target_price, trade.side, std_60),
                _z_from_target(btc_price, target_price, trade.side, std_120),
                quotes.get("mid_price"),
                quotes.get("bid_price"),
                quotes.get("ask_price"),
                quotes.get("spread"),
                _momentum_score(btc_ticks, 10),
                _reversal_score(btc_ticks, trade.side, 10),
                _btc_velocity(btc_ticks, 30.0),
                time_remaining_seconds,
                now,
            ),
        )

        logger.debug(
            "Snapshot trade #%d  mid=%.4f  remaining=%.0fs",
            trade.trade_id, quotes.get("mid_price", 0.0), time_remaining_seconds,
        )


# ── Module helper ─────────────────────────────────────────────────────────────

def _clamp(price: float) -> float:
    """Keep contract prices within the valid $0.01–$0.99 range."""
    return round(min(0.99, max(0.01, price)), 4)
