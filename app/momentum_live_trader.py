"""
app/momentum_live_trader.py — Real-money execution layer for the frozen
ht120s_tp5c momentum candidate.

THIS PLACES REAL ORDERS when (and only when) every MOMENTUM_LIVE_* gate is set.
It is a SEPARATE component from the shadow tracker:
  - It does NOT modify or replace app/momentum_shadow_tracker.py.
  - Shadow tracking and shadow reporting continue to run unchanged.
  - It reuses the EXACT frozen signal + exit logic so live entries line up
    tick-for-tick with shadow entries (paired projected-vs-actual).

Signal alignment
----------------
Entry detection and exit rules are imported directly from the shadow tracker /
backtest modules (same ExperimentConfig, same detect_signal, same window math,
same 120s hold / +5c target / 10s grace).  No strategy parameter is redefined
here.

Safety model (fail closed)
--------------------------
No order is sent unless ALL of these hold (checked in is_live_armed()):
    MOMENTUM_LIVE_ENABLED == true
    MOMENTUM_LIVE_CONFIRM == "I_UNDERSTAND_REAL_MONEY"
    Kalshi RSA auth configured (KALSHI_KEY_ID + KALSHI_KEY_FILE)
    bankroll, kelly fraction, and per-trade dollar cap all > 0
Plus, before EACH entry, per-trade risk gates (kill switch, pause latch, max
active, spread, quote freshness, daily loss, sizing) must all pass.

If the component is constructed but not fully armed, it runs in INERT mode:
on_tick() returns immediately and no order API is ever called.  Projected
outcomes continue to be recorded by the (separate) shadow tracker.
"""
from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app import config
from app.db import execute_query, fetch_all, fetch_one, insert_and_get_id
from app.features import Tick
from app.kalshi_trading import (
    KalshiTradingClient,
    KalshiTradingError,
    cents_to_dollars,
    is_authenticated,
)
from backtest.loader import MarketInfo, MarketRow, get_bid
from backtest.signals import detect_signal

# Reuse the frozen profile constants + helpers from the shadow tracker so the
# live trader cannot drift from shadow behaviour.
from app.momentum_shadow_tracker import (
    _SHADOW_CONFIG,
    _PROFILE,
    _HOLD_S,
    _TP,
    _GRACE_S,
    _TRIM_WINDOW_S,
    _build_market_row,
    _classify_pnl,
    EXIT_PROFIT_TARGET,
    EXIT_FIXED_TIME,
    EXIT_UNEXECUTABLE,
)

logger = logging.getLogger(__name__)

# Round-trip cost used for the PROJECTED (shadow) pnl so it matches the shadow
# tracker exactly.  Actual pnl uses real fill prices + real fees instead.
_PROJ_FEE      = _SHADOW_CONFIG.estimated_fee_per_contract
_PROJ_SLIPPAGE = _SHADOW_CONFIG.estimated_slippage_cents


# ══════════════════════════════════════════════════════════════════════════════
# Pure helpers (no DB / no network) — unit tested in tests/test_momentum_live.py
# ══════════════════════════════════════════════════════════════════════════════

def compute_full_kelly_fraction(
    win_rate: Optional[float],
    profit_loss_ratio: Optional[float],
) -> float:
    """
    Full-Kelly stake fraction for a win/loss bet.

        f* = p - (1 - p) / b

    where p = win_rate (0..1), b = profit_loss_ratio (avg win / |avg loss|).
    Returns 0.0 when inputs are missing/degenerate or the edge is non-positive.
    Result is clamped to [0, 1].
    """
    if win_rate is None or profit_loss_ratio is None:
        return 0.0
    if profit_loss_ratio <= 0:
        return 0.0
    p = max(0.0, min(1.0, win_rate))
    f = p - (1.0 - p) / profit_loss_ratio
    return max(0.0, min(1.0, f))


def compute_position_size(
    *,
    bankroll_dollars: float,
    kelly_fraction: float,
    full_kelly_fraction: float,
    max_dollars_per_trade: float,
    max_contracts_per_trade: int,
    price_per_contract: float,
) -> tuple[int, float]:
    """
    Conservative position sizing.  Returns (contracts, dollars_budgeted).

    Steps:
      1. Kelly dollars = bankroll * full_kelly * kelly_fraction
      2. Budget = min(Kelly dollars, max_dollars_per_trade)
      3. contracts = floor(budget / price_per_contract)   (round DOWN)
      4. cap by max_contracts_per_trade when > 0
    contracts is 0 when any input is non-positive or the budget can't afford one
    contract — the caller must treat 0 as "do not trade".
    """
    if (
        bankroll_dollars <= 0
        or kelly_fraction <= 0
        or full_kelly_fraction <= 0
        or max_dollars_per_trade <= 0
        or price_per_contract <= 0
    ):
        return 0, 0.0

    kelly_dollars = bankroll_dollars * full_kelly_fraction * kelly_fraction
    budget = min(kelly_dollars, max_dollars_per_trade)
    if budget <= 0:
        return 0, 0.0

    contracts = int(budget // price_per_contract)   # floor — never round up
    if max_contracts_per_trade and max_contracts_per_trade > 0:
        contracts = min(contracts, max_contracts_per_trade)
    if contracts < 1:
        return 0, 0.0

    dollars_budgeted = round(contracts * price_per_contract, 4)
    return contracts, dollars_budgeted


def summarize_pnls(pnls: list[float]) -> dict[str, Optional[float]]:
    """
    Win-rate / expectancy / profit-factor / profit-loss-ratio for a list of
    per-contract net pnls (dollar fractions).  Used for both projected and
    actual aggregates so they are computed identically.
    """
    n = len(pnls)
    if n == 0:
        return {
            "n": 0, "win_rate": None, "expectancy": None,
            "profit_factor": None, "profit_loss_ratio": None,
        }
    wins   = [p for p in pnls if p > 1e-9]
    losses = [p for p in pnls if p < -1e-9]
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win  = statistics.mean(wins)   if wins   else None
    avg_loss = abs(statistics.mean(losses)) if losses else None
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "expectancy": statistics.mean(pnls),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "profit_loss_ratio": (avg_win / avg_loss) if (avg_win and avg_loss) else None,
    }


def compute_drift_fields(
    *,
    projected_entry_ask: Optional[float],
    projected_exit_bid: Optional[float],
    projected_profit: Optional[float],
    projected_expectancy: Optional[float],
    actual_entry_price: Optional[float],
    actual_exit_price: Optional[float],
    actual_profit: Optional[float],
) -> dict[str, Optional[float]]:
    """
    Projected-vs-actual comparison fields (all dollar fractions / contract).

    Conventions (documented in the migration too):
      entry_price_drift     = actual_entry - projected_entry_ask  (+ = paid more)
      exit_price_drift      = actual_exit  - projected_exit_bid   (- = got less)
      profit_delta          = actual_profit - projected_profit
      total_execution_drift = projected_profit - actual_profit    (adverse cost)
      profit_capture_ratio  = actual_profit / projected_profit
      expectancy_delta      = actual_profit - projected_expectancy
      expectancy_capture    = actual_profit / projected_expectancy
    """
    def _sub(a, b):
        return round(a - b, 6) if (a is not None and b is not None) else None

    def _ratio(a, b):
        if a is None or b is None or abs(b) < 1e-9:
            return None
        return round(a / b, 6)

    entry_drift = _sub(actual_entry_price, projected_entry_ask)
    exit_drift  = _sub(actual_exit_price, projected_exit_bid)
    profit_delta = _sub(actual_profit, projected_profit)
    total_exec_drift = _sub(projected_profit, actual_profit)
    return {
        "entry_price_drift_cents":     entry_drift,
        "exit_price_drift_cents":      exit_drift,
        "profit_delta_cents":          profit_delta,
        "total_execution_drift_cents": total_exec_drift,
        "profit_capture_ratio":        _ratio(actual_profit, projected_profit),
        "expectancy_delta_cents":      _sub(actual_profit, projected_expectancy),
        "expectancy_capture_ratio":    _ratio(actual_profit, projected_expectancy),
    }


def evaluate_pause(
    windows: dict[int, dict],
    *,
    min_trades: int,
    win_rate_gap_pct: float,
    expectancy_gap: float,
    profit_factor_gap: float,
) -> Optional[dict]:
    """
    Decide whether automatic pause should trigger.

    ``windows`` maps window_size -> {
        "n": int,
        "projected": {win_rate, expectancy, profit_factor, ...},
        "actual":    {win_rate, expectancy, profit_factor, ...},
    }

    A window is only evaluated once it has at least ``min_trades`` completed live
    trades (do not pause on tiny samples).  Returns the first breaching window's
    detail dict, or None if nothing breaches.

    Gaps are projected MINUS actual (live trailing shadow):
      win-rate gap in percentage POINTS, expectancy in dollar fraction,
      profit-factor as a raw difference.
    """
    for size in sorted(windows):
        w = windows[size]
        if w.get("n", 0) < min_trades:
            continue
        proj = w["projected"]
        act = w["actual"]
        breaches: list[str] = []

        pw, aw = proj.get("win_rate"), act.get("win_rate")
        if pw is not None and aw is not None:
            gap = (pw - aw) * 100.0
            if gap > win_rate_gap_pct:
                breaches.append(
                    f"win_rate gap {gap:.1f}pp > {win_rate_gap_pct:.1f}pp"
                )

        pe, ae = proj.get("expectancy"), act.get("expectancy")
        if pe is not None and ae is not None:
            gap = pe - ae
            if gap > expectancy_gap:
                breaches.append(
                    f"expectancy gap {gap:.4f} > {expectancy_gap:.4f}"
                )

        pf, af = proj.get("profit_factor"), act.get("profit_factor")
        if pf is not None and af is not None:
            gap = pf - af
            if gap > profit_factor_gap:
                breaches.append(
                    f"profit_factor gap {gap:.3f} > {profit_factor_gap:.3f}"
                )

        if breaches:
            return {
                "window": size,
                "n": w["n"],
                "breaches": breaches,
                "projected": proj,
                "actual": act,
            }
    return None


def kill_switch_engaged() -> bool:
    """True when the env flag is set or the kill-switch file exists."""
    if config.MOMENTUM_LIVE_KILL_SWITCH:
        return True
    path = config.MOMENTUM_LIVE_KILL_SWITCH_FILE
    return bool(path) and os.path.exists(path)


def is_live_armed() -> tuple[bool, str]:
    """
    Static gate: is the live trader allowed to place real orders at all?
    Returns (armed, reason).  reason is empty when armed.
    """
    if not config.MOMENTUM_LIVE_ENABLED:
        return False, "MOMENTUM_LIVE_ENABLED is not true"
    if config.MOMENTUM_LIVE_CONFIRM != config.MOMENTUM_LIVE_CONFIRM_TOKEN:
        return False, "MOMENTUM_LIVE_CONFIRM token not set correctly"
    if not is_authenticated():
        return False, "Kalshi RSA auth not configured (KALSHI_KEY_ID/KALSHI_KEY_FILE)"
    if config.MOMENTUM_LIVE_BANKROLL_DOLLARS <= 0:
        return False, "MOMENTUM_LIVE_BANKROLL_DOLLARS must be > 0"
    if config.MOMENTUM_LIVE_KELLY_FRACTION <= 0:
        return False, "MOMENTUM_LIVE_KELLY_FRACTION must be > 0"
    if config.MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE <= 0:
        return False, "MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE must be > 0"
    return True, ""


def _summarize_fills(fills: list[dict], side: str) -> tuple[int, Optional[float], Optional[float]]:
    """
    Aggregate Kalshi fills into (total_count, avg_price_dollars, total_fees_dollars).

    Reads the price field matching ``side`` (yes_price / no_price, integer cents)
    and an optional fee field if present.  Never fabricates — returns (0, None,
    None) when there are no fills.
    """
    price_key = "yes_price" if side == "YES" else "no_price"
    total = 0
    weighted = 0.0
    fees = 0.0
    saw_fee = False
    for f in fills:
        cnt = int(f.get("count") or 0)
        if cnt <= 0:
            continue
        px = cents_to_dollars(f.get(price_key))
        if px is None:
            continue
        total += cnt
        weighted += px * cnt
        fee = f.get("fee") or f.get("fee_dollars")
        if fee is not None:
            try:
                fees += float(fee)
                saw_fee = True
            except (TypeError, ValueError):
                pass
    if total == 0:
        return 0, None, None
    avg = round(weighted / total, 4)
    return total, avg, (round(fees, 4) if saw_fee else None)


# ══════════════════════════════════════════════════════════════════════════════
# Active live trade state
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class _ActiveLive:
    live_trade_id: int
    market_id:     int
    contract_id:   int
    market_ticker: str
    side:          str
    signal_at:     datetime
    signal_ts:     float
    entry_ask:     float           # projected entry (observed ask at signal)
    horizon_ts:    float

    requested_contracts: int
    status:        str             # PENDING_ENTRY | ACTIVE | PENDING_EXIT

    # Order linkage
    entry_order_id:  Optional[str] = None
    entry_submit_ts: Optional[float] = None
    exit_order_id:   Optional[str] = None
    exit_submit_ts:  Optional[float] = None

    # Fills
    filled_contracts:   int = 0
    actual_entry_price: Optional[float] = None

    # Projected exit tracking (identical to shadow _advance_active)
    peak_bid:    float = float("-inf")
    trough_bid:  float = float("inf")
    grace_start: Optional[float] = None
    projected_exit_bid: Optional[float] = None
    exit_reason: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# MomentumLiveTrader
# ══════════════════════════════════════════════════════════════════════════════

class MomentumLiveTrader:
    """
    Live execution for the frozen ht120s_tp5c profile.  Call on_tick() once per
    poll cycle, AFTER the shadow tracker, with the same arguments.
    """

    def __init__(self) -> None:
        import collections
        self._window: collections.deque[MarketRow] = collections.deque()
        self._current_market_id: Optional[str] = None
        self._cooldown_until: dict[int, float] = {}
        self._active: dict[int, _ActiveLive] = {}

        self._armed, reason = is_live_armed()
        self._client: Optional[KalshiTradingClient] = None
        if self._armed:
            try:
                self._client = KalshiTradingClient(require_auth=True)
            except KalshiTradingError as exc:
                self._armed = False
                reason = str(exc)

        self._recover_orphans()

        if self._armed:
            logger.warning(
                "MomentumLiveTrader ARMED — REAL ORDERS ENABLED | profile=%s "
                "bankroll=$%.2f kelly=%.3f max$/trade=%.2f max_contracts=%d "
                "max_active=%d max_spread=%.3f",
                _PROFILE,
                config.MOMENTUM_LIVE_BANKROLL_DOLLARS,
                config.MOMENTUM_LIVE_KELLY_FRACTION,
                config.MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE,
                config.MOMENTUM_LIVE_MAX_CONTRACTS_PER_TRADE,
                config.MOMENTUM_LIVE_MAX_ACTIVE_TRADES,
                config.MOMENTUM_LIVE_MAX_SPREAD,
            )
        else:
            logger.info(
                "MomentumLiveTrader INERT (no real orders) — %s", reason,
            )

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def active_count(self) -> int:
        return len(self._active)

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
        Process one live tick.  Mirrors the shadow tracker's ordering exactly so
        signals fire on the same ticks:
          1. market rollover
          2. build MarketRow
          3. advance active live trades (poll orders, check exits)
          4. append to window / trim
          5. detect new signals (only opens real trades when armed + gates pass)
        """
        if not self._armed:
            return
        if target_price is None:
            return

        # ── Market rollover ───────────────────────────────────────────────────
        if market_id != self._current_market_id:
            if self._current_market_id is not None and self._active:
                logger.warning(
                    "live rollover: %s -> %s with %d active trade(s) — closing out",
                    self._current_market_id, market_id, len(self._active),
                )
                for live in list(self._active.values()):
                    try:
                        self._close_out_on_rollover(live, captured_at)
                    except Exception as exc:
                        logger.error("live rollover close-out failed: %s", exc)
            self._current_market_id = market_id
            self._window.clear()
            self._cooldown_until.clear()

        row = _build_market_row(
            snapshot_id=snapshot_id,
            snapshot_seq=snapshot_seq,
            captured_at=captured_at,
            btc_price=btc_price,
            tte=tte,
            prices=prices,
            btc_ticks=btc_ticks,
        )

        if self._active:
            try:
                self._advance_active(row, captured_at)
            except Exception as exc:
                logger.error("live advance-active failed: %s", exc, exc_info=True)

        self._window.append(row)
        cutoff = row.ts - _TRIM_WINDOW_S
        while self._window and self._window[0].ts < cutoff:
            self._window.popleft()

        try:
            self._detect(
                market_db_id=market_db_id,
                market_id=market_id,
                market_ticker=market_ticker,
                contract_ids=contract_ids,
                target_price=target_price,
                row=row,
                captured_at=captured_at,
            )
        except Exception as exc:
            logger.error("live detect failed: %s", exc, exc_info=True)

    # ── Signal detection (identical window math to the shadow tracker) ─────────

    def _detect(
        self,
        market_db_id:  int,
        market_id:     str,
        market_ticker: str,
        contract_ids:  dict[str, int],
        target_price:  float,
        row:           MarketRow,
        captured_at:   datetime,
    ) -> None:
        window = list(self._window)
        lookback_cutoff = row.ts - _SHADOW_CONFIG.lookback_seconds
        in_window = [r for r in window if r.ts >= lookback_cutoff]
        if len(in_window) < 3:
            return
        lookback_row = in_window[0]
        actual_lookback = row.ts - lookback_row.ts
        if actual_lookback < 0.8 * _SHADOW_CONFIG.lookback_seconds:
            return

        market_info = MarketInfo(
            id=market_db_id, market_id=market_id, target_price=target_price,
            opens_at=None, closes_at=None, settles_at=None,
            status="open", contract_ids=contract_ids,
        )

        for side in ("YES", "NO"):
            contract_id = contract_ids.get(side)
            if contract_id is None:
                continue
            if row.ts < self._cooldown_until.get(contract_id, float("-inf")):
                continue
            if contract_id in self._active:
                continue

            sig = detect_signal(
                row=row, lookback_row=lookback_row, market=market_info,
                contract_id=contract_id, side=side, config=_SHADOW_CONFIG,
                snapshot_index=len(window) - 1,
            )
            if sig is None:
                continue

            # Frozen signal fired — attempt a live entry behind the risk gates.
            opened = self._try_open_live(sig, market_ticker, captured_at)
            if opened:
                # Match shadow cooldown semantics regardless of fill outcome.
                self._cooldown_until[contract_id] = (
                    row.ts + _SHADOW_CONFIG.cooldown_seconds
                )

    # ── Risk gates ────────────────────────────────────────────────────────────

    def _check_risk_gates(self, sig, captured_at: datetime) -> tuple[bool, str, str]:
        """
        Pre-order risk gates.  Returns (ok, guardrail_event_type, reason).
        Order matters: hard stops first, then per-trade conditions.
        """
        if kill_switch_engaged():
            return False, "kill_switch", "kill switch engaged"

        if self._is_paused():
            return False, "blocked_paused", "live trading is paused (manual unpause required)"

        if len(self._active) >= config.MOMENTUM_LIVE_MAX_ACTIVE_TRADES:
            return (
                False, "blocked_max_active",
                f"max active live trades reached ({config.MOMENTUM_LIVE_MAX_ACTIVE_TRADES})",
            )

        # Quote freshness — the quote we are acting on must be recent.
        age = (datetime.now(timezone.utc) - captured_at).total_seconds()
        if age > config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS:
            return (
                False, "blocked_quote_stale",
                f"quote age {age:.1f}s > {config.MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS:.1f}s",
            )

        # Spread gate.
        if sig.entry_spread is None or sig.entry_spread > config.MOMENTUM_LIVE_MAX_SPREAD:
            return (
                False, "blocked_spread",
                f"spread {sig.entry_spread} > {config.MOMENTUM_LIVE_MAX_SPREAD}",
            )

        # Daily-loss gate.
        if config.MOMENTUM_LIVE_MAX_DAILY_LOSS_DOLLARS > 0:
            todays = self._todays_realized_dollars()
            if todays <= -abs(config.MOMENTUM_LIVE_MAX_DAILY_LOSS_DOLLARS):
                return (
                    False, "blocked_daily_loss",
                    f"daily realized {todays:.2f} <= "
                    f"-{config.MOMENTUM_LIVE_MAX_DAILY_LOSS_DOLLARS:.2f}",
                )
        return True, "", ""

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    def _try_open_live(self, sig, market_ticker: str, captured_at: datetime) -> bool:
        """
        Evaluate gates + sizing and, if all pass, place a real entry order and
        persist a momentum_live_trades row.  Returns True if a signal was acted
        on (whether or not an order was placed) so cooldown applies.
        """
        ok, event_type, reason = self._check_risk_gates(sig, captured_at)
        if not ok:
            self._record_guardrail(event_type, market_ticker, sig.side, reason)
            logger.warning("live BLOCKED | %s %s | %s", market_ticker, sig.side, reason)
            return True   # signal handled (blocked) — apply cooldown

        # Projected strategy stats (rolling shadow window) → Kelly inputs.
        proj = self._load_projected_stats()
        if proj is None or proj["n"] < config.MOMENTUM_LIVE_MIN_SHADOW_TRADES:
            self._record_guardrail(
                "blocked_sizing", market_ticker, sig.side,
                f"insufficient shadow history for sizing "
                f"(have {None if proj is None else proj['n']}, "
                f"need {config.MOMENTUM_LIVE_MIN_SHADOW_TRADES})",
            )
            return True

        full_kelly = compute_full_kelly_fraction(
            proj["win_rate"], proj["profit_loss_ratio"]
        )
        contracts, dollars_budgeted = compute_position_size(
            bankroll_dollars=config.MOMENTUM_LIVE_BANKROLL_DOLLARS,
            kelly_fraction=config.MOMENTUM_LIVE_KELLY_FRACTION,
            full_kelly_fraction=full_kelly,
            max_dollars_per_trade=config.MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE,
            max_contracts_per_trade=config.MOMENTUM_LIVE_MAX_CONTRACTS_PER_TRADE,
            price_per_contract=sig.entry_ask,
        )
        if contracts < 1:
            self._record_guardrail(
                "blocked_sizing", market_ticker, sig.side,
                f"sizing rounded down to 0 contracts "
                f"(full_kelly={full_kelly:.3f}, budget=${dollars_budgeted:.2f}, "
                f"price={sig.entry_ask:.3f})",
            )
            return True

        target_ask = round(sig.entry_ask + _TP, 4)
        now = datetime.now(timezone.utc)

        # Persist the trade row up front (PENDING_ENTRY) with projected stats.
        live_trade_id = insert_and_get_id(
            """
            INSERT INTO momentum_live_trades (
                market_id, contract_id, market_ticker, side,
                signal_at, exit_profile,
                bankroll_at_entry, kelly_fraction, kelly_full_fraction,
                dollars_budgeted, requested_contracts,
                projected_entry_ask, projected_target_ask,
                projected_expectancy_cents, projected_win_rate,
                projected_profit_factor, projected_profit_loss_ratio,
                status, created_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                'PENDING_ENTRY', %s
            )
            """,
            (
                sig.market_id, sig.contract_id, market_ticker, sig.side,
                sig.signal_at, _PROFILE,
                config.MOMENTUM_LIVE_BANKROLL_DOLLARS,
                config.MOMENTUM_LIVE_KELLY_FRACTION, round(full_kelly, 4),
                dollars_budgeted, contracts,
                sig.entry_ask, target_ask,
                _safe(proj["expectancy"]), _safe(proj["win_rate"]),
                _safe(proj["profit_factor"]), _safe(proj["profit_loss_ratio"]),
                now,
            ),
        )

        live = _ActiveLive(
            live_trade_id=live_trade_id,
            market_id=sig.market_id,
            contract_id=sig.contract_id,
            market_ticker=market_ticker,
            side=sig.side,
            signal_at=sig.signal_at,
            signal_ts=sig.signal_ts,
            entry_ask=sig.entry_ask,
            horizon_ts=sig.signal_ts + _HOLD_S,
            requested_contracts=contracts,
            status="PENDING_ENTRY",
        )

        # Place the real entry order (limit buy at the observed ask).
        try:
            order = self._client.place_order(
                ticker=market_ticker, side=sig.side, action="buy",
                count=contracts, limit_price=sig.entry_ask, order_type="limit",
            )
            live.entry_order_id = str(order.get("order_id") or order.get("id") or "")
            live.entry_submit_ts = datetime.now(timezone.utc).timestamp()
            execute_query(
                "UPDATE momentum_live_trades SET entry_order_id=%s, "
                "entry_client_order_id=%s WHERE id=%s",
                (live.entry_order_id, str(order.get("client_order_id") or ""), live_trade_id),
            )
            self._record_order_event(
                live, "entry_submitted", action="buy",
                requested_count=contracts, limit_price=sig.entry_ask,
                order_id=live.entry_order_id,
                client_order_id=str(order.get("client_order_id") or ""),
                raw=order,
            )
            self._active[sig.contract_id] = live
            logger.warning(
                "live ENTRY SUBMITTED #%d | %s %s | x%d @ %.3f (order=%s)",
                live_trade_id, market_ticker, sig.side, contracts,
                sig.entry_ask, live.entry_order_id,
            )
        except KalshiTradingError as exc:
            execute_query(
                "UPDATE momentum_live_trades SET status='REJECTED' WHERE id=%s",
                (live_trade_id,),
            )
            self._record_order_event(
                live, "entry_rejected", action="buy",
                requested_count=contracts, limit_price=sig.entry_ask,
                detail=str(exc)[:480],
            )
            logger.error("live ENTRY REJECTED #%d | %s", live_trade_id, exc)
        return True

    def _advance_active(self, row: MarketRow, captured_at: datetime) -> None:
        """Advance every active live trade by one tick."""
        for contract_id, live in list(self._active.items()):
            bid = get_bid(row, live.side)

            # Projected excursion tracking (identical to shadow tracker).
            if row.ts <= live.horizon_ts and bid is not None:
                live.peak_bid = max(live.peak_bid, bid)
                live.trough_bid = min(live.trough_bid, bid)

            try:
                if live.status == "PENDING_ENTRY":
                    self._advance_pending_entry(live, row, captured_at)
                elif live.status == "ACTIVE":
                    self._advance_active_position(live, row, bid, captured_at)
                elif live.status == "PENDING_EXIT":
                    self._advance_pending_exit(live, row, bid, captured_at)
            except KalshiTradingError as exc:
                # Network/API hiccup — log and retry on the next tick.
                logger.warning(
                    "live order poll failed (#%d, retry next tick): %s",
                    live.live_trade_id, exc,
                )

    def _advance_pending_entry(self, live: _ActiveLive, row: MarketRow, captured_at: datetime) -> None:
        """Poll the resting entry order; promote to ACTIVE on fill, cancel on timeout."""
        if not live.entry_order_id:
            return
        fills = self._client.get_fills(order_id=live.entry_order_id)
        count, avg_price, _fees = _summarize_fills(fills, live.side)

        elapsed = datetime.now(timezone.utc).timestamp() - (live.entry_submit_ts or 0)
        timed_out = elapsed > config.MOMENTUM_LIVE_ENTRY_FILL_TIMEOUT_SECONDS
        past_horizon = row.ts >= live.horizon_ts

        if count >= 1:
            # (Partial or full) entry achieved.
            live.filled_contracts = count
            live.actual_entry_price = avg_price
            live.status = "ACTIVE"
            execute_query(
                "UPDATE momentum_live_trades SET status='ACTIVE', "
                "filled_contracts=%s, actual_entry_price=%s, entry_at=%s WHERE id=%s",
                (count, avg_price, captured_at, live.live_trade_id),
            )
            self._record_order_event(
                live,
                "entry_filled" if count >= live.requested_contracts else "entry_partial",
                action="buy", requested_count=live.requested_contracts,
                filled_count=count, avg_fill_price=avg_price,
                order_id=live.entry_order_id,
            )
            logger.warning(
                "live ENTRY FILLED #%d | %s %s | %d/%d @ %.3f",
                live.live_trade_id, live.market_ticker, live.side,
                count, live.requested_contracts, avg_price or 0.0,
            )
            return

        if timed_out or past_horizon:
            # No fill — do not chase. Cancel and abandon (no fabricated fill).
            try:
                self._client.cancel_order(live.entry_order_id)
            except KalshiTradingError as exc:
                logger.warning("entry cancel failed #%d: %s", live.live_trade_id, exc)
            execute_query(
                "UPDATE momentum_live_trades SET status='CANCELED' WHERE id=%s",
                (live.live_trade_id,),
            )
            self._record_order_event(
                live, "entry_canceled", action="buy",
                requested_count=live.requested_contracts, filled_count=0,
                order_id=live.entry_order_id,
                detail="unfilled past timeout/horizon",
            )
            logger.info("live ENTRY CANCELED (unfilled) #%d", live.live_trade_id)
            del self._active[live.contract_id]

    def _advance_active_position(
        self, live: _ActiveLive, row: MarketRow, bid: Optional[float], captured_at: datetime
    ) -> None:
        """Apply the frozen exit rules; submit a real sell when an exit fires."""
        exit_reason, exit_bid = self._frozen_exit_decision(live, row, bid)
        if exit_reason is None:
            return

        live.exit_reason = exit_reason
        live.projected_exit_bid = exit_bid   # observed bid drives projected pnl

        if exit_bid is None:
            # Frozen "unexecutable": no bid to sell into.  Keep the position and
            # retry the exit as soon as a bid appears (do not fabricate a fill).
            live.status = "PENDING_EXIT"
            execute_query(
                "UPDATE momentum_live_trades SET status='PENDING_EXIT', "
                "exit_reason=%s WHERE id=%s",
                (exit_reason, live.live_trade_id),
            )
            self._record_guardrail(
                "exit_no_bid", live.market_ticker, live.side,
                f"exit fired ({exit_reason}) with no bid; will retry to flatten",
            )
            return

        self._submit_exit(live, exit_bid, captured_at, exit_reason)

    def _advance_pending_exit(
        self, live: _ActiveLive, row: MarketRow, bid: Optional[float], captured_at: datetime
    ) -> None:
        """Poll the resting exit order; finalize on fill; re-price/retry otherwise."""
        if not live.exit_order_id:
            # We owed an exit but had no bid before — try again now.
            if bid is not None:
                self._submit_exit(live, bid, captured_at, live.exit_reason or EXIT_FIXED_TIME)
            return

        fills = self._client.get_fills(order_id=live.exit_order_id)
        count, avg_price, fees = _summarize_fills(fills, live.side)
        if count >= live.filled_contracts and count >= 1:
            self._finalize_complete(live, avg_price, fees, captured_at)
            return

        # Not (fully) filled — chase the bid to get flat: cancel + re-submit.
        elapsed = datetime.now(timezone.utc).timestamp() - (live.exit_submit_ts or 0)
        if elapsed > config.MOMENTUM_LIVE_ENTRY_FILL_TIMEOUT_SECONDS and bid is not None:
            try:
                self._client.cancel_order(live.exit_order_id)
            except KalshiTradingError as exc:
                logger.warning("exit cancel failed #%d: %s", live.live_trade_id, exc)
            self._submit_exit(live, bid, captured_at, live.exit_reason or EXIT_FIXED_TIME)

    def _frozen_exit_decision(
        self, live: _ActiveLive, row: MarketRow, bid: Optional[float]
    ) -> tuple[Optional[str], Optional[float]]:
        """
        EXACT replica of the shadow tracker's exit rule order for ht120s_tp5c:
          1. profit target: bid >= entry_ask + 0.05
          2. fixed time:    elapsed >= 120s, first available bid
          3. grace:         no bid > 10s past horizon -> unexecutable
        """
        if bid is not None and bid >= live.entry_ask + _TP:
            return EXIT_PROFIT_TARGET, bid
        if row.ts >= live.horizon_ts:
            if bid is not None:
                return EXIT_FIXED_TIME, bid
            if live.grace_start is None:
                live.grace_start = row.ts
            if row.ts > live.grace_start + _GRACE_S:
                return EXIT_UNEXECUTABLE, None
        return None, None

    def _submit_exit(
        self, live: _ActiveLive, bid: float, captured_at: datetime, exit_reason: str
    ) -> None:
        """Place a real limit sell to flatten the position at the observed bid."""
        try:
            order = self._client.place_order(
                ticker=live.market_ticker, side=live.side, action="sell",
                count=live.filled_contracts, limit_price=bid, order_type="limit",
            )
            live.exit_order_id = str(order.get("order_id") or order.get("id") or "")
            live.exit_submit_ts = datetime.now(timezone.utc).timestamp()
            live.status = "PENDING_EXIT"
            execute_query(
                "UPDATE momentum_live_trades SET status='PENDING_EXIT', "
                "exit_order_id=%s, exit_client_order_id=%s, exit_reason=%s WHERE id=%s",
                (live.exit_order_id, str(order.get("client_order_id") or ""),
                 exit_reason, live.live_trade_id),
            )
            self._record_order_event(
                live, "exit_submitted", action="sell",
                requested_count=live.filled_contracts, limit_price=bid,
                order_id=live.exit_order_id,
                client_order_id=str(order.get("client_order_id") or ""),
                raw=order,
            )
            logger.warning(
                "live EXIT SUBMITTED #%d | %s %s | x%d @ %.3f (%s)",
                live.live_trade_id, live.market_ticker, live.side,
                live.filled_contracts, bid, exit_reason,
            )
        except KalshiTradingError as exc:
            self._record_order_event(
                live, "exit_rejected", action="sell",
                requested_count=live.filled_contracts, limit_price=bid,
                detail=str(exc)[:480],
            )
            logger.error("live EXIT REJECTED #%d | %s", live.live_trade_id, exc)

    def _finalize_complete(
        self, live: _ActiveLive, actual_exit_price: Optional[float],
        actual_fees: Optional[float], captured_at: datetime,
    ) -> None:
        """Compute projected/actual/drift, persist COMPLETE, evaluate pause."""
        # Projected (shadow-of-this-trade) pnl on observed quotes.
        projected_profit = None
        if live.projected_exit_bid is not None:
            projected_profit = round(
                live.projected_exit_bid - live.entry_ask - _PROJ_FEE - _PROJ_SLIPPAGE, 6
            )

        # Actual pnl from real fills.
        actual_profit = None
        actual_profit_dollars = None
        actual_won = None
        if actual_exit_price is not None and live.actual_entry_price is not None:
            fee = actual_fees or 0.0
            actual_profit = round(
                actual_exit_price - live.actual_entry_price - fee, 6
            )
            actual_profit_dollars = round(actual_profit * live.filled_contracts, 6)
            actual_won = 1 if actual_profit > 1e-9 else 0

        # Projected strategy expectancy stored at entry (re-read for drift).
        row = fetch_one(
            "SELECT projected_expectancy_cents FROM momentum_live_trades WHERE id=%s",
            (live.live_trade_id,),
        )
        projected_expectancy = (
            float(row["projected_expectancy_cents"])
            if row and row.get("projected_expectancy_cents") is not None else None
        )

        drift = compute_drift_fields(
            projected_entry_ask=live.entry_ask,
            projected_exit_bid=live.projected_exit_bid,
            projected_profit=projected_profit,
            projected_expectancy=projected_expectancy,
            actual_entry_price=live.actual_entry_price,
            actual_exit_price=actual_exit_price,
            actual_profit=actual_profit,
        )

        holding_s = round(captured_at.timestamp() - live.signal_ts, 1)

        execute_query(
            """
            UPDATE momentum_live_trades SET
                status='COMPLETE', exit_at=%s, exit_reason=%s, holding_seconds=%s,
                actual_exit_price=%s, actual_fees_cents=%s,
                actual_profit_cents=%s, actual_profit_dollars=%s, actual_trade_won=%s,
                projected_exit_bid=%s, projected_profit_cents=%s,
                profit_delta_cents=%s, expectancy_delta_cents=%s,
                entry_price_drift_cents=%s, exit_price_drift_cents=%s,
                total_execution_drift_cents=%s,
                profit_capture_ratio=%s, expectancy_capture_ratio=%s
            WHERE id=%s
            """,
            (
                captured_at, live.exit_reason, holding_s,
                actual_exit_price, actual_fees,
                actual_profit, actual_profit_dollars, actual_won,
                live.projected_exit_bid, projected_profit,
                drift["profit_delta_cents"], drift["expectancy_delta_cents"],
                drift["entry_price_drift_cents"], drift["exit_price_drift_cents"],
                drift["total_execution_drift_cents"],
                drift["profit_capture_ratio"], drift["expectancy_capture_ratio"],
                live.live_trade_id,
            ),
        )
        self._record_order_event(
            live, "exit_filled", action="sell",
            requested_count=live.filled_contracts, filled_count=live.filled_contracts,
            avg_fill_price=actual_exit_price, order_id=live.exit_order_id,
        )
        logger.warning(
            "live COMPLETE #%d | %s %s | proj=%s act=%s delta=%s capture=%s",
            live.live_trade_id, live.market_ticker, live.side,
            _fmt(projected_profit), _fmt(actual_profit),
            _fmt(drift["profit_delta_cents"]), _fmt(drift["profit_capture_ratio"]),
        )
        del self._active[live.contract_id]

        # Automatic pause check on every completed live trade.
        try:
            self._evaluate_and_maybe_pause()
        except Exception as exc:
            logger.error("pause evaluation failed: %s", exc, exc_info=True)

    def _close_out_on_rollover(self, live: _ActiveLive, captured_at: datetime) -> None:
        """Best-effort flatten on market rollover; never leave a silent position."""
        if live.status == "PENDING_ENTRY" and live.entry_order_id:
            try:
                self._client.cancel_order(live.entry_order_id)
            except KalshiTradingError:
                pass
            execute_query(
                "UPDATE momentum_live_trades SET status='CANCELED' WHERE id=%s",
                (live.live_trade_id,),
            )
            self._record_order_event(
                live, "entry_canceled", action="buy",
                detail="market rollover", order_id=live.entry_order_id,
            )
        else:
            # We may be holding contracts; record a guardrail so it can't be
            # missed. A standing exit order (if any) is left to fill/cancel.
            execute_query(
                "UPDATE momentum_live_trades SET status='UNEXECUTABLE', exit_at=%s, "
                "exit_reason='rollover_open_position' WHERE id=%s",
                (captured_at, live.live_trade_id),
            )
            self._record_guardrail(
                "rollover_open_position", live.market_ticker, live.side,
                f"market rolled over with live trade #{live.live_trade_id} "
                f"status={live.status} filled={live.filled_contracts} — verify positions",
            )
        self._active.pop(live.contract_id, None)

    # ── Projected stats + pause ───────────────────────────────────────────────

    def _load_projected_stats(self) -> Optional[dict]:
        """Rolling shadow stats (most recent COMPLETE shadow trades) for Kelly."""
        rows = fetch_all(
            """
            SELECT net_pnl_cents FROM momentum_shadow_trades
            WHERE status='COMPLETE' AND net_pnl_cents IS NOT NULL
            ORDER BY signal_at DESC
            LIMIT %s
            """,
            (config.MOMENTUM_LIVE_PROJECTED_WINDOW,),
        )
        pnls = [float(r["net_pnl_cents"]) for r in rows]
        return summarize_pnls(pnls)

    def _load_pause_windows(self) -> dict[int, dict]:
        """Projected vs actual stats over rolling windows of completed live trades."""
        out: dict[int, dict] = {}
        max_w = max(config.LIVE_PAUSE_WINDOWS)
        rows = fetch_all(
            """
            SELECT projected_profit_cents, actual_profit_cents
            FROM momentum_live_trades
            WHERE status='COMPLETE'
            ORDER BY signal_at DESC
            LIMIT %s
            """,
            (max_w,),
        )
        for w in config.LIVE_PAUSE_WINDOWS:
            chunk = rows[:w]
            proj = [float(r["projected_profit_cents"]) for r in chunk
                    if r["projected_profit_cents"] is not None]
            act = [float(r["actual_profit_cents"]) for r in chunk
                   if r["actual_profit_cents"] is not None]
            # Use the count of paired actual trades as the window n.
            out[w] = {
                "n": len(act),
                "projected": summarize_pnls(proj),
                "actual": summarize_pnls(act),
            }
        return out

    def _evaluate_and_maybe_pause(self) -> None:
        if self._is_paused():
            return
        windows = self._load_pause_windows()
        breach = evaluate_pause(
            windows,
            min_trades=config.LIVE_PAUSE_MIN_TRADES,
            win_rate_gap_pct=config.LIVE_PAUSE_WIN_RATE_GAP_PCT,
            expectancy_gap=config.LIVE_PAUSE_EXPECTANCY_GAP_CENTS,
            profit_factor_gap=config.LIVE_PAUSE_PROFIT_FACTOR_GAP,
        )
        if breach is None:
            return
        reason = (
            f"window={breach['window']} n={breach['n']}: "
            + "; ".join(breach["breaches"])
        )
        import json
        now = datetime.now(timezone.utc)
        execute_query(
            "UPDATE momentum_live_pause_state SET is_paused=1, reason=%s, "
            "metrics_json=%s, paused_at=%s WHERE id=1",
            (reason[:480], json.dumps(breach, default=str), now),
        )
        self._record_guardrail(
            "paused", None, None, reason, metrics=breach,
        )
        logger.error(
            "LIVE TRADING AUTO-PAUSED — %s  (manual unpause required: "
            "python scripts/momentum_live_report.py --unpause \"reviewed\")",
            reason,
        )

    def _is_paused(self) -> bool:
        row = fetch_one("SELECT is_paused FROM momentum_live_pause_state WHERE id=1")
        return bool(row and int(row["is_paused"]) == 1)

    def _todays_realized_dollars(self) -> float:
        row = fetch_one(
            """
            SELECT COALESCE(SUM(actual_profit_dollars), 0) AS pnl
            FROM momentum_live_trades
            WHERE status='COMPLETE'
              AND actual_profit_dollars IS NOT NULL
              AND exit_at >= UTC_DATE()
            """
        )
        return float(row["pnl"]) if row and row["pnl"] is not None else 0.0

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _record_order_event(
        self, live: _ActiveLive, event_type: str, *,
        action: Optional[str] = None, requested_count: Optional[int] = None,
        filled_count: Optional[int] = None, limit_price: Optional[float] = None,
        avg_fill_price: Optional[float] = None, order_id: Optional[str] = None,
        client_order_id: Optional[str] = None, detail: Optional[str] = None,
        raw: Optional[dict] = None,
    ) -> None:
        import json
        try:
            execute_query(
                """
                INSERT INTO momentum_live_order_events (
                    live_trade_id, market_ticker, side, event_type,
                    order_id, client_order_id, action,
                    requested_count, filled_count, limit_price, avg_fill_price,
                    detail, raw_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    live.live_trade_id, live.market_ticker, live.side, event_type,
                    order_id, client_order_id, action,
                    requested_count, filled_count, limit_price, avg_fill_price,
                    detail, json.dumps(raw, default=str) if raw else None,
                ),
            )
        except Exception as exc:
            logger.warning("failed to record order event %s: %s", event_type, exc)

    def _record_guardrail(
        self, event_type: str, market_ticker: Optional[str],
        side: Optional[str], reason: str, *, metrics: Optional[dict] = None,
    ) -> None:
        import json
        try:
            execute_query(
                """
                INSERT INTO momentum_live_guardrail_events
                    (event_type, market_ticker, side, reason, metrics_json)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    event_type, market_ticker, side, reason[:480],
                    json.dumps(metrics, default=str) if metrics else None,
                ),
            )
        except Exception as exc:
            logger.warning("failed to record guardrail %s: %s", event_type, exc)

    def _recover_orphans(self) -> None:
        """
        On restart, flag any non-terminal live rows for human review.  We do NOT
        auto-close real positions here (we cannot know fill state without the
        API); we mark them and emit a guardrail so they are noticed.
        """
        try:
            rows = fetch_all(
                "SELECT id, status FROM momentum_live_trades "
                "WHERE status IN ('PENDING_ENTRY','ACTIVE','PENDING_EXIT')"
            )
        except Exception as exc:
            logger.info(
                "MomentumLiveTrader: orphan check skipped (table may not exist "
                "— run scripts/migrate_add_momentum_live.py): %s", exc,
            )
            return
        for r in rows:
            logger.warning(
                "live ORPHAN on restart: trade #%s status=%s — needs manual review",
                r["id"], r["status"],
            )
            try:
                execute_query(
                    "INSERT INTO momentum_live_guardrail_events "
                    "(event_type, reason) VALUES ('orphan_on_restart', %s)",
                    (f"live trade #{r['id']} was {r['status']} at restart",),
                )
            except Exception:
                pass


# ── small format helpers ────────────────────────────────────────────────────

def _safe(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 6)


def _fmt(v: Optional[float]) -> str:
    return f"{v:+.4f}" if v is not None else "n/a"
