"""
app/late_winning_live_trader.py

Separate real-money strategy for the late winning-contract rule:

    - BTC 15m market only, fed by app/main.py
    - buy the side BTC already favors
    - enter when ask is in the configured 75c-90c band, spread is tight, and
      BTC is already far enough past the strike
    - hold unless BTC crosses back through the strike; otherwise record a
      settlement-mode result at expiry

This module can place real orders, but only when LATE_WINNING_* gates are
explicitly armed.  It intentionally does not alter the existing momentum trader.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app import config
from app.db import execute_query, fetch_all, fetch_one, insert_and_get_id
from app.features import Tick
from app.kalshi_trading import KalshiTradingClient, KalshiTradingError, is_authenticated
from app.momentum_live_trader import (
    _summarize_fills,
    aggregate_fills,
    compute_fixed_position_size,
    compute_live_entry_limit_price,
    is_order_open,
    kill_switch_engaged,
    order_remaining,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LateWinningSignal:
    market_db_id: int
    contract_id: int
    market_ticker: str
    side: str
    signal_at: datetime
    signal_ts: float
    expiry_ts: float
    target_price: float
    btc_price: float
    entry_distance: float
    time_remaining_seconds: float
    entry_bid: Optional[float]
    entry_ask: float
    spread: Optional[float]


@dataclass
class _LateWinningActive:
    live_trade_id: int
    market_db_id: int
    contract_id: int
    market_ticker: str
    side: str
    signal_at: datetime
    signal_ts: float
    target_price: float
    entry_distance: float
    requested_contracts: int
    entry_ask: float
    entry_order_price: float
    entry_client_order_id: str
    status: str = "PENDING_ENTRY"
    entry_order_id: Optional[str] = None
    entry_submit_ts: Optional[float] = None
    entry_cancel_requested: bool = False
    filled_contracts: int = 0
    actual_entry_price: Optional[float] = None
    actual_entry_fees: Optional[float] = None
    entry_at: Optional[datetime] = None
    exit_reason: Optional[str] = None
    exit_order_id: Optional[str] = None
    exit_order_ids: list[str] = field(default_factory=list)
    exit_client_order_id: Optional[str] = None
    exit_submit_ts: Optional[float] = None
    exit_filled_contracts: int = 0
    last_bid: Optional[float] = None
    max_bid_after_entry: Optional[float] = None
    min_distance_after_entry: Optional[float] = None


def _safe_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _aligned_distance(side: str, btc_price: float, target_price: float) -> float:
    if side == "YES":
        return btc_price - target_price
    return target_price - btc_price


def _price_for_side(prices: dict, side: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    row = prices.get(side) or {}
    return (
        _safe_float(row.get("bid_price")),
        _safe_float(row.get("ask_price")),
        _safe_float(row.get("spread")),
    )


def _order_payload(payload: Optional[dict]) -> dict:
    """Return the actual order object from either flat or nested API payloads."""
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("order")
    return nested if isinstance(nested, dict) else payload


def _order_id(payload: Optional[dict]) -> str:
    order = _order_payload(payload)
    return str(order.get("order_id") or order.get("id") or "")


def find_late_winning_signal(
    *,
    market_db_id: int,
    market_ticker: str,
    contract_ids: dict[str, int],
    target_price: float,
    captured_at: datetime,
    btc_price: float,
    tte: Optional[int],
    prices: dict,
) -> Optional[LateWinningSignal]:
    """
    Pure rule detector for the late winning-contract strategy.

    ``LATE_WINNING_MAX_ASK`` is treated as exclusive by design, so the default
    0.91 means 75c through 90c.
    """
    if tte is None:
        return None
    tte_f = float(tte)
    if tte_f < config.LATE_WINNING_MIN_TTE_SECONDS:
        return None
    if tte_f > config.LATE_WINNING_MAX_TTE_SECONDS:
        return None
    if btc_price == target_price:
        return None

    side = "YES" if btc_price > target_price else "NO"
    distance = _aligned_distance(side, btc_price, target_price)
    if distance < config.LATE_WINNING_MIN_DISTANCE_DOLLARS:
        return None

    bid, ask, spread = _price_for_side(prices, side)
    if ask is None:
        return None
    if ask < config.LATE_WINNING_MIN_ASK or ask >= config.LATE_WINNING_MAX_ASK:
        return None
    if spread is None or spread > config.LATE_WINNING_MAX_SPREAD:
        return None
    contract_id = contract_ids.get(side)
    if contract_id is None:
        return None

    return LateWinningSignal(
        market_db_id=market_db_id,
        contract_id=contract_id,
        market_ticker=market_ticker,
        side=side,
        signal_at=captured_at,
        signal_ts=captured_at.timestamp(),
        expiry_ts=captured_at.timestamp() + tte_f,
        target_price=target_price,
        btc_price=btc_price,
        entry_distance=distance,
        time_remaining_seconds=tte_f,
        entry_bid=bid,
        entry_ask=ask,
        spread=spread,
    )


def is_late_winning_armed() -> tuple[bool, str]:
    if not config.LATE_WINNING_LIVE_ENABLED:
        return False, "LATE_WINNING_LIVE_ENABLED is not true"
    if config.LATE_WINNING_LIVE_CONFIRM != config.MOMENTUM_LIVE_CONFIRM_TOKEN:
        return False, "LATE_WINNING_LIVE_CONFIRM token not set correctly"
    if not is_authenticated():
        return False, "Kalshi RSA auth not configured (KALSHI_KEY_ID/KALSHI_KEY_FILE)"
    if config.LATE_WINNING_PROFILE == "":
        return False, "LATE_WINNING_PROFILE is empty"
    if config.LATE_WINNING_FIXED_CONTRACTS <= 0:
        return False, "LATE_WINNING_FIXED_CONTRACTS must be > 0"
    if config.LATE_WINNING_MAX_DOLLARS_PER_TRADE <= 0:
        return False, "LATE_WINNING_MAX_DOLLARS_PER_TRADE must be > 0"
    if config.LATE_WINNING_MAX_ACTIVE_TRADES <= 0:
        return False, "LATE_WINNING_MAX_ACTIVE_TRADES must be > 0"
    if config.LATE_WINNING_MIN_ASK >= config.LATE_WINNING_MAX_ASK:
        return False, "LATE_WINNING_MIN_ASK must be less than LATE_WINNING_MAX_ASK"
    if kill_switch_engaged():
        return False, "kill switch engaged"
    return True, ""


class LateWinningLiveTrader:
    """Live executor for the late winning-contract rule."""

    def __init__(self) -> None:
        self._armed, self._inert_reason = is_late_winning_armed()
        self._client = KalshiTradingClient(require_auth=True) if self._armed else None
        self._active: dict[int, _LateWinningActive] = {}
        self._cooldown_until: dict[int, float] = {}
        self._blocked_startup = False

        if self._armed:
            self._rehydrate_open_rows()
            logger.warning(
                "LateWinningLiveTrader ARMED - REAL ORDERS ENABLED | profile=%s "
                "rule=side_favored dist>=%.0f ask=[%.2f,%.2f) tte=%.0f-%.0fs "
                "fixed_contracts=%d max$/trade=%.2f max_active=%d max_spread=%.3f",
                config.LATE_WINNING_PROFILE,
                config.LATE_WINNING_MIN_DISTANCE_DOLLARS,
                config.LATE_WINNING_MIN_ASK,
                config.LATE_WINNING_MAX_ASK,
                config.LATE_WINNING_MIN_TTE_SECONDS,
                config.LATE_WINNING_MAX_TTE_SECONDS,
                config.LATE_WINNING_FIXED_CONTRACTS,
                config.LATE_WINNING_MAX_DOLLARS_PER_TRADE,
                config.LATE_WINNING_MAX_ACTIVE_TRADES,
                config.LATE_WINNING_MAX_SPREAD,
            )
        else:
            logger.info("LateWinningLiveTrader INERT (no real orders) - %s", self._inert_reason)

    def on_tick(
        self,
        *,
        market_db_id: int,
        market_id: str,
        market_ticker: str,
        contract_ids: dict[str, int],
        target_price: float,
        captured_at: datetime,
        btc_price: float,
        tte: Optional[int],
        prices: dict,
        btc_ticks: list[Tick],
        snapshot_id: int,
        snapshot_seq: int,
    ) -> None:
        del market_id, btc_ticks, snapshot_id, snapshot_seq
        if not self._armed or self._blocked_startup:
            return

        self._advance_active_positions(captured_at, btc_price, market_ticker, prices)

        if kill_switch_engaged():
            self._record_guardrail("blocked_kill_switch", None, None, "kill switch engaged")
            return
        if len(self._active) >= config.LATE_WINNING_MAX_ACTIVE_TRADES:
            return
        if self._todays_pnl() <= -abs(config.LATE_WINNING_MAX_DAILY_LOSS_DOLLARS) and config.LATE_WINNING_MAX_DAILY_LOSS_DOLLARS > 0:
            self._record_guardrail(
                "blocked_daily_loss", None, None,
                f"daily loss reached -${config.LATE_WINNING_MAX_DAILY_LOSS_DOLLARS:.2f}",
            )
            return

        sig = find_late_winning_signal(
            market_db_id=market_db_id,
            market_ticker=market_ticker,
            contract_ids=contract_ids,
            target_price=target_price,
            captured_at=captured_at,
            btc_price=btc_price,
            tte=tte,
            prices=prices,
        )
        if sig is None:
            return
        if sig.contract_id in self._active:
            return
        if captured_at.timestamp() < self._cooldown_until.get(sig.contract_id, float("-inf")):
            return
        if self._already_traded_market_side(sig.market_ticker, sig.side):
            return

        self._try_enter(sig)

    def _try_enter(self, sig: LateWinningSignal) -> None:
        assert self._client is not None

        entry_limit = compute_live_entry_limit_price(
            sig.entry_ask,
            config.LATE_WINNING_ENTRY_PRICE_OFFSET_CENTS,
        )
        contracts, dollars_budgeted = compute_fixed_position_size(
            fixed_contracts=config.LATE_WINNING_FIXED_CONTRACTS,
            max_dollars_per_trade=config.LATE_WINNING_MAX_DOLLARS_PER_TRADE,
            max_contracts_per_trade=config.LATE_WINNING_MAX_CONTRACTS_PER_TRADE,
            price_per_contract=entry_limit,
        )
        if contracts < 1:
            self._record_guardrail(
                "blocked_sizing", sig.market_ticker, sig.side,
                f"fixed size rounded down to 0 contracts (budget=${dollars_budgeted:.2f}, price={entry_limit:.3f})",
            )
            return

        coid = str(uuid.uuid4())
        metadata = {
            "strategy": "late_winning_contract",
            "rule": {
                "min_distance_dollars": config.LATE_WINNING_MIN_DISTANCE_DOLLARS,
                "min_ask": config.LATE_WINNING_MIN_ASK,
                "max_ask_exclusive": config.LATE_WINNING_MAX_ASK,
                "max_spread": config.LATE_WINNING_MAX_SPREAD,
                "min_tte_seconds": config.LATE_WINNING_MIN_TTE_SECONDS,
                "max_tte_seconds": config.LATE_WINNING_MAX_TTE_SECONDS,
                "exit": "btc_crossback_or_settlement",
            },
            "signal": {
                "btc_price": sig.btc_price,
                "strike": sig.target_price,
                "entry_distance": sig.entry_distance,
                "time_remaining_seconds": sig.time_remaining_seconds,
                "entry_bid": sig.entry_bid,
                "entry_ask": sig.entry_ask,
                "spread": sig.spread,
            },
        }
        trade_id = insert_and_get_id(
            """
            INSERT INTO momentum_live_trades (
                market_id, contract_id, market_ticker, side,
                signal_at, exit_profile,
                bankroll_at_entry, kelly_fraction, kelly_full_fraction,
                dollars_budgeted, requested_contracts,
                entry_client_order_id,
                projected_entry_ask, projected_target_ask,
                projected_expectancy_cents, projected_win_rate,
                projected_profit_factor, projected_profit_loss_ratio,
                ws_enabled, ws_spread_at_entry,
                status, metadata_json
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s,
                %s, 0, 0,
                %s, %s,
                %s,
                %s, 1.0,
                NULL, NULL,
                NULL, NULL,
                1, %s,
                'PENDING_ENTRY', %s
            )
            """,
            (
                sig.market_db_id, sig.contract_id, sig.market_ticker, sig.side,
                sig.signal_at, config.LATE_WINNING_PROFILE,
                config.LATE_WINNING_MAX_DOLLARS_PER_TRADE,
                dollars_budgeted, contracts,
                coid,
                sig.entry_ask,
                sig.spread,
                json.dumps(metadata, default=str),
            ),
        )

        live = _LateWinningActive(
            live_trade_id=trade_id,
            market_db_id=sig.market_db_id,
            contract_id=sig.contract_id,
            market_ticker=sig.market_ticker,
            side=sig.side,
            signal_at=sig.signal_at,
            signal_ts=sig.signal_ts,
            expiry_ts=sig.expiry_ts,
            target_price=sig.target_price,
            entry_distance=sig.entry_distance,
            requested_contracts=contracts,
            entry_ask=sig.entry_ask,
            entry_order_price=entry_limit,
            entry_client_order_id=coid,
            last_bid=sig.entry_bid,
            max_bid_after_entry=sig.entry_bid,
            min_distance_after_entry=sig.entry_distance,
        )

        try:
            live.entry_submit_ts = datetime.now(timezone.utc).timestamp()
            order = self._client.place_order(
                ticker=sig.market_ticker,
                side=sig.side,
                action="buy",
                count=contracts,
                limit_price=entry_limit,
                order_type="limit",
                client_order_id=coid,
            )
        except KalshiTradingError as exc:
            execute_query("UPDATE momentum_live_trades SET status='REJECTED' WHERE id=%s", (trade_id,))
            self._record_order_event(
                live, "entry_rejected", action="buy",
                requested_count=contracts, limit_price=entry_limit,
                client_order_id=coid, detail=str(exc)[:480],
            )
            self._start_retry_cooldown(sig.contract_id, sig.signal_ts, "entry rejected")
            logger.error("late winning ENTRY REJECTED #%d | %s", trade_id, exc)
            return

        live.entry_order_id = _order_id(order)
        if not live.entry_order_id:
            adopted = self._find_order_by_client_order_id(coid, sig.market_ticker)
            if adopted is not None:
                live.entry_order_id = _order_id(adopted)
        execute_query(
            "UPDATE momentum_live_trades SET entry_order_id=%s WHERE id=%s",
            (live.entry_order_id, trade_id),
        )
        self._record_order_event(
            live, "entry_submitted", action="buy",
            requested_count=contracts, limit_price=entry_limit,
            order_id=live.entry_order_id, client_order_id=coid, raw=order,
        )
        self._active[sig.contract_id] = live
        logger.warning(
            "late winning ENTRY SUBMITTED #%d | %s %s | x%d @ %.3f | dist=$%.0f tte=%.0fs",
            trade_id, sig.market_ticker, sig.side, contracts, entry_limit,
            sig.entry_distance, sig.time_remaining_seconds,
        )

    def _advance_active_positions(
        self,
        captured_at: datetime,
        btc_price: float,
        current_market_ticker: str,
        prices: dict,
    ) -> None:
        for contract_id, live in list(self._active.items()):
            bid = None
            if live.market_ticker == current_market_ticker:
                bid, _, _ = _price_for_side(prices, live.side)
            if bid is not None:
                live.last_bid = bid
                live.max_bid_after_entry = (
                    bid if live.max_bid_after_entry is None else max(live.max_bid_after_entry, bid)
                )
            distance = _aligned_distance(live.side, btc_price, live.target_price)
            live.min_distance_after_entry = (
                distance if live.min_distance_after_entry is None else min(live.min_distance_after_entry, distance)
            )
            if live.status == "PENDING_ENTRY":
                self._advance_pending_entry(live, captured_at)
            elif live.status == "ACTIVE":
                self._advance_open_position(live, captured_at, distance)
            elif live.status == "PENDING_EXIT":
                self._advance_pending_exit(live, captured_at, bid)

    def _advance_pending_entry(self, live: _LateWinningActive, captured_at: datetime) -> None:
        if self._client is None:
            return
        if not live.entry_order_id and not self._adopt_or_cancel_missing_entry_order(live, captured_at):
            return
        fills = self._client.get_fills(order_id=live.entry_order_id)
        count, avg_price, fees = _summarize_fills(fills, live.side)
        live.filled_contracts = count
        live.actual_entry_price = avg_price
        live.actual_entry_fees = fees

        if count >= live.requested_contracts:
            live.status = "ACTIVE"
            live.entry_at = captured_at
            execute_query(
                "UPDATE momentum_live_trades SET status='ACTIVE', filled_contracts=%s, "
                "actual_entry_price=%s, entry_at=%s WHERE id=%s",
                (count, avg_price, captured_at, live.live_trade_id),
            )
            self._record_order_event(
                live, "entry_filled", action="buy",
                requested_count=live.requested_contracts, filled_count=count,
                avg_fill_price=avg_price, order_id=live.entry_order_id,
            )
            logger.warning(
                "late winning ENTRY FILLED #%d | %s %s | %d/%d @ %.3f",
                live.live_trade_id, live.market_ticker, live.side,
                count, live.requested_contracts, avg_price or 0.0,
            )
            return

        order = self._safe_get_order(live.entry_order_id)
        order_open = is_order_open(order)
        elapsed = datetime.now(timezone.utc).timestamp() - (live.entry_submit_ts or live.signal_ts)
        if order_open and elapsed <= config.LATE_WINNING_ENTRY_FILL_TIMEOUT_SECONDS:
            return

        if order_open and not live.entry_cancel_requested:
            try:
                self._client.cancel_order(live.entry_order_id)
            except KalshiTradingError as exc:
                logger.warning("late winning entry cancel failed #%d: %s", live.live_trade_id, exc)
            live.entry_cancel_requested = True
            self._record_order_event(live, "entry_cancel_requested", action="buy", order_id=live.entry_order_id)
            return

        if count >= 1:
            live.status = "ACTIVE"
            live.entry_at = captured_at
            execute_query(
                "UPDATE momentum_live_trades SET status='ACTIVE', filled_contracts=%s, "
                "actual_entry_price=%s, entry_at=%s WHERE id=%s",
                (count, avg_price, captured_at, live.live_trade_id),
            )
            return

        execute_query("UPDATE momentum_live_trades SET status='CANCELED' WHERE id=%s", (live.live_trade_id,))
        self._record_order_event(
            live, "entry_canceled", action="buy",
            requested_count=live.requested_contracts, filled_count=0,
            order_id=live.entry_order_id, detail="unfilled past timeout",
        )
        self._start_retry_cooldown(live.contract_id, captured_at.timestamp(), "unfilled past timeout")
        self._active.pop(live.contract_id, None)

    def _advance_open_position(
        self,
        live: _LateWinningActive,
        captured_at: datetime,
        aligned_distance: float,
    ) -> None:
        if aligned_distance <= 0:
            if live.last_bid is None:
                self._record_guardrail(
                    "exit_no_bid", live.market_ticker, live.side,
                    "BTC crossed back through strike but no bid was available",
                )
                return
            self._submit_exit(live, live.last_bid, captured_at, "btc_crossback")
            return

        if captured_at.timestamp() >= live.expiry_ts:
            self._finalize_settlement(
                live,
                captured_at,
                settlement_price=1.0 if aligned_distance > 0 else 0.0,
            )

    def _submit_exit(
        self, live: _LateWinningActive, bid: float, captured_at: datetime, reason: str
    ) -> None:
        if self._client is None:
            return
        remaining = order_remaining(live.filled_contracts, live.exit_filled_contracts)
        if remaining < 1:
            return
        coid = str(uuid.uuid4())
        live.status = "PENDING_EXIT"
        live.exit_reason = reason
        live.exit_client_order_id = coid
        live.exit_submit_ts = datetime.now(timezone.utc).timestamp()
        execute_query(
            "UPDATE momentum_live_trades SET status='PENDING_EXIT', exit_reason=%s, "
            "exit_client_order_id=%s, projected_exit_bid=%s WHERE id=%s",
            (reason, coid, bid, live.live_trade_id),
        )
        try:
            order = self._client.place_order(
                ticker=live.market_ticker,
                side=live.side,
                action="sell",
                count=remaining,
                limit_price=bid,
                order_type="limit",
                client_order_id=coid,
            )
        except KalshiTradingError as exc:
            self._record_order_event(
                live, "exit_rejected", action="sell",
                requested_count=remaining, limit_price=bid,
                client_order_id=coid, detail=str(exc)[:480],
            )
            logger.error("late winning EXIT REJECTED #%d | %s", live.live_trade_id, exc)
            return

        oid = _order_id(order)
        if not oid:
            adopted = self._find_order_by_client_order_id(coid, live.market_ticker)
            if adopted is not None:
                oid = _order_id(adopted)
        live.exit_order_id = oid
        if oid and oid not in live.exit_order_ids:
            live.exit_order_ids.append(oid)
        execute_query(
            "UPDATE momentum_live_trades SET exit_order_id=%s WHERE id=%s",
            (oid, live.live_trade_id),
        )
        self._record_order_event(
            live, "exit_submitted", action="sell",
            requested_count=remaining, limit_price=bid,
            order_id=oid, client_order_id=coid, raw=order,
        )
        logger.warning(
            "late winning EXIT SUBMITTED #%d | %s %s | x%d @ %.3f (%s)",
            live.live_trade_id, live.market_ticker, live.side, remaining, bid, reason,
        )

    def _advance_pending_exit(
        self, live: _LateWinningActive, captured_at: datetime, bid: Optional[float]
    ) -> None:
        if self._client is None:
            return
        count, avg_price, fees = aggregate_fills(
            [self._client.get_fills(order_id=oid) for oid in live.exit_order_ids],
            live.side,
        )
        live.exit_filled_contracts = count
        if count >= live.filled_contracts and count >= 1:
            self._finalize_exit_fill(live, captured_at, avg_price, fees)
            return

        latest = live.exit_order_id
        order = self._safe_get_order(latest) if latest else None
        order_open = is_order_open(order) if latest else False
        elapsed = datetime.now(timezone.utc).timestamp() - (live.exit_submit_ts or 0.0)
        if order_open and elapsed <= config.LATE_WINNING_EXIT_REPRICE_SECONDS:
            return
        if order_open and latest:
            try:
                self._client.cancel_order(latest)
            except KalshiTradingError as exc:
                logger.warning("late winning exit cancel failed #%d: %s", live.live_trade_id, exc)
            return
        if bid is not None:
            self._submit_exit(live, bid, captured_at, live.exit_reason or "btc_crossback")

    def _finalize_exit_fill(
        self,
        live: _LateWinningActive,
        captured_at: datetime,
        actual_exit_price: Optional[float],
        exit_fees_total: Optional[float],
    ) -> None:
        fees_total = (live.actual_entry_fees or 0.0) + (exit_fees_total or 0.0)
        fee_per_contract = fees_total / live.filled_contracts if live.filled_contracts else 0.0
        actual_profit = None
        actual_profit_dollars = None
        won = None
        if live.actual_entry_price is not None and actual_exit_price is not None and live.filled_contracts >= 1:
            actual_profit = round(actual_exit_price - live.actual_entry_price - fee_per_contract, 6)
            actual_profit_dollars = round(actual_profit * live.filled_contracts, 6)
            won = 1 if actual_profit > 0 else 0
        holding_s = round(captured_at.timestamp() - live.signal_ts, 2)
        projected_profit = (
            round(actual_exit_price - live.entry_ask, 6)
            if actual_exit_price is not None else None
        )
        execute_query(
            """
            UPDATE momentum_live_trades SET
                status='COMPLETE', exit_at=%s, exit_reason=%s, holding_seconds=%s,
                actual_exit_price=%s, actual_fees_cents=%s,
                actual_profit_cents=%s, actual_profit_dollars=%s, actual_trade_won=%s,
                projected_exit_bid=%s, projected_profit_cents=%s,
                max_bid_after_entry=%s
            WHERE id=%s
            """,
            (
                captured_at, live.exit_reason or "btc_crossback", holding_s,
                actual_exit_price, round(fee_per_contract, 6),
                actual_profit, actual_profit_dollars, won,
                actual_exit_price, projected_profit,
                live.max_bid_after_entry,
                live.live_trade_id,
            ),
        )
        self._record_order_event(
            live, "exit_filled", action="sell",
            requested_count=live.filled_contracts, filled_count=live.exit_filled_contracts,
            avg_fill_price=actual_exit_price, order_id=live.exit_order_id,
        )
        logger.warning(
            "late winning COMPLETE #%d | %s %s | exit=%s pnl=%s",
            live.live_trade_id, live.market_ticker, live.side,
            actual_exit_price, actual_profit_dollars,
        )
        self._active.pop(live.contract_id, None)

    def _finalize_settlement(
        self,
        live: _LateWinningActive,
        captured_at: datetime,
        *,
        settlement_price: float,
    ) -> None:
        fees_total = live.actual_entry_fees or 0.0
        fee_per_contract = fees_total / live.filled_contracts if live.filled_contracts else 0.0
        actual_profit = None
        actual_profit_dollars = None
        won = None
        if live.actual_entry_price is not None and live.filled_contracts >= 1:
            actual_profit = round(settlement_price - live.actual_entry_price - fee_per_contract, 6)
            actual_profit_dollars = round(actual_profit * live.filled_contracts, 6)
            won = 1 if actual_profit > 0 else 0
        holding_s = round(captured_at.timestamp() - live.signal_ts, 2)
        execute_query(
            """
            UPDATE momentum_live_trades SET
                status='COMPLETE', exit_at=%s, exit_reason='held_to_settlement',
                holding_seconds=%s, actual_exit_price=%s, actual_fees_cents=%s,
                actual_profit_cents=%s, actual_profit_dollars=%s, actual_trade_won=%s,
                projected_exit_bid=%s, projected_profit_cents=%s,
                max_bid_after_entry=%s
            WHERE id=%s
            """,
            (
                captured_at, holding_s, settlement_price, round(fee_per_contract, 6),
                actual_profit, actual_profit_dollars, won,
                settlement_price,
                round(settlement_price - live.entry_ask, 6),
                live.max_bid_after_entry,
                live.live_trade_id,
            ),
        )
        self._record_order_event(
            live, "held_to_settlement", action=None,
            requested_count=live.filled_contracts, filled_count=live.filled_contracts,
            avg_fill_price=settlement_price,
            detail="modeled settlement outcome; no exit sell order submitted",
        )
        logger.warning(
            "late winning HELD TO SETTLEMENT #%d | %s %s | settle=%.2f pnl=%s",
            live.live_trade_id, live.market_ticker, live.side,
            settlement_price, actual_profit_dollars,
        )
        self._active.pop(live.contract_id, None)

    def _safe_get_order(self, order_id: Optional[str]) -> Optional[dict]:
        if not order_id or self._client is None:
            return None
        try:
            return self._client.get_order(order_id)
        except KalshiTradingError as exc:
            logger.warning("late winning get_order failed (%s): %s", order_id, exc)
            return None

    def _find_order_by_client_order_id(self, client_order_id: str, ticker: str) -> Optional[dict]:
        if not client_order_id or self._client is None:
            return None
        try:
            return self._client.find_order_by_client_order_id(client_order_id, ticker=ticker)
        except KalshiTradingError as exc:
            logger.warning(
                "late winning client-order reconcile failed (%s): %s",
                client_order_id, exc,
            )
            return None

    def _adopt_or_cancel_missing_entry_order(
        self, live: _LateWinningActive, captured_at: datetime
    ) -> bool:
        """
        Recover PENDING_ENTRY rows whose submit response did not persist an
        order id. Without this, stale rows can sit forever and block entries.

        Returns True only when ``live.entry_order_id`` is now available.
        """
        adopted = self._find_order_by_client_order_id(
            live.entry_client_order_id, live.market_ticker
        )
        if adopted is not None:
            live.entry_order_id = _order_id(adopted)
            if live.entry_order_id:
                execute_query(
                    "UPDATE momentum_live_trades SET entry_order_id=%s WHERE id=%s",
                    (live.entry_order_id, live.live_trade_id),
                )
                self._record_order_event(
                    live,
                    "entry_order_recovered",
                    action="buy",
                    order_id=live.entry_order_id,
                    client_order_id=live.entry_client_order_id,
                    raw=adopted,
                )
                logger.warning(
                    "late winning ENTRY ORDER RECOVERED #%d | order=%s",
                    live.live_trade_id, live.entry_order_id,
                )
                return True

        elapsed = datetime.now(timezone.utc).timestamp() - (live.entry_submit_ts or live.signal_ts)
        if elapsed <= config.LATE_WINNING_ENTRY_FILL_TIMEOUT_SECONDS:
            return False

        execute_query(
            "UPDATE momentum_live_trades SET status='CANCELED', exit_reason='missing_entry_order_id' WHERE id=%s",
            (live.live_trade_id,),
        )
        self._record_order_event(
            live,
            "entry_canceled",
            action="buy",
            requested_count=live.requested_contracts,
            filled_count=0,
            client_order_id=live.entry_client_order_id,
            detail="missing entry_order_id and client-order reconcile found no order",
        )
        self._start_retry_cooldown(
            live.contract_id, captured_at.timestamp(), "missing entry order id"
        )
        logger.warning(
            "late winning ENTRY CANCELED #%d | missing entry_order_id",
            live.live_trade_id,
        )
        self._active.pop(live.contract_id, None)
        return False

    def _start_retry_cooldown(self, contract_id: int, retry_from_ts: float, reason: str) -> None:
        delay = config.LATE_WINNING_RETRY_AFTER_CANCEL_SECONDS
        if delay <= 0:
            return
        self._cooldown_until[contract_id] = max(
            self._cooldown_until.get(contract_id, float("-inf")),
            retry_from_ts + delay,
        )
        logger.info("late winning retry cooldown | contract=%s | %.1fs | %s", contract_id, delay, reason)

    def _todays_pnl(self) -> float:
        row = fetch_one(
            """
            SELECT COALESCE(SUM(actual_profit_dollars), 0) AS pnl
            FROM momentum_live_trades
            WHERE exit_profile=%s
              AND status='COMPLETE'
              AND actual_profit_dollars IS NOT NULL
              AND exit_at >= UTC_DATE()
            """,
            (config.LATE_WINNING_PROFILE,),
        )
        return float(row["pnl"]) if row and row.get("pnl") is not None else 0.0

    def _already_traded_market_side(self, market_ticker: str, side: str) -> bool:
        row = fetch_one(
            """
            SELECT COUNT(*) AS n
            FROM momentum_live_trades
            WHERE exit_profile=%s
              AND market_ticker=%s
              AND side=%s
              AND status IN ('PENDING_ENTRY','ACTIVE','PENDING_EXIT','COMPLETE')
            """,
            (config.LATE_WINNING_PROFILE, market_ticker, side),
        )
        return bool(row and int(row["n"]) > 0)

    def _rehydrate_open_rows(self) -> None:
        rows = fetch_all(
            """
            SELECT id, market_id, contract_id, market_ticker, side, signal_at,
                   projected_entry_ask, requested_contracts, filled_contracts,
                   actual_entry_price, entry_order_id, entry_client_order_id,
                   status, exit_order_id, exit_client_order_id, exit_reason,
                   metadata_json
            FROM momentum_live_trades
            WHERE exit_profile=%s
              AND status IN ('PENDING_ENTRY','ACTIVE','PENDING_EXIT')
            ORDER BY id DESC
            """,
            (config.LATE_WINNING_PROFILE,),
        )
        for r in rows:
            metadata = {}
            raw_meta = r.get("metadata_json")
            if raw_meta:
                try:
                    metadata = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
                except Exception:
                    metadata = {}
            signal = metadata.get("signal") if isinstance(metadata, dict) else {}
            target_price = _safe_float(signal.get("strike")) if isinstance(signal, dict) else None
            if target_price is None:
                self._blocked_startup = True
                logger.error(
                    "LateWinningLiveTrader startup blocked: open row #%s lacks strategy metadata; resolve manually",
                    r.get("id"),
                )
                continue
            signal_at = r["signal_at"]
            if signal_at.tzinfo is None:
                signal_at = signal_at.replace(tzinfo=timezone.utc)
            live = _LateWinningActive(
                live_trade_id=int(r["id"]),
                market_db_id=int(r["market_id"]),
                contract_id=int(r["contract_id"]),
                market_ticker=str(r["market_ticker"]),
                side=str(r["side"]),
                signal_at=signal_at,
                signal_ts=signal_at.timestamp(),
                expiry_ts=signal_at.timestamp() + float(signal.get("time_remaining_seconds") or 0.0),
                target_price=target_price,
                entry_distance=float(signal.get("entry_distance") or 0.0),
                requested_contracts=int(r.get("requested_contracts") or 0),
                entry_ask=float(r.get("projected_entry_ask") or 0.0),
                entry_order_price=float(r.get("projected_entry_ask") or 0.0),
                entry_client_order_id=str(r.get("entry_client_order_id") or ""),
                status=str(r.get("status") or "PENDING_ENTRY"),
                entry_order_id=r.get("entry_order_id"),
                filled_contracts=int(r.get("filled_contracts") or 0),
                actual_entry_price=_safe_float(r.get("actual_entry_price")),
                exit_order_id=r.get("exit_order_id"),
                exit_client_order_id=r.get("exit_client_order_id"),
                exit_reason=r.get("exit_reason"),
            )
            if live.exit_order_id:
                live.exit_order_ids.append(live.exit_order_id)
            self._active[live.contract_id] = live
        if rows:
            logger.warning(
                "LateWinningLiveTrader rehydrated %d open late-winning row(s)",
                len(rows),
            )

    def _record_order_event(
        self,
        live: _LateWinningActive,
        event_type: str,
        *,
        action: Optional[str] = None,
        requested_count: Optional[int] = None,
        filled_count: Optional[int] = None,
        limit_price: Optional[float] = None,
        avg_fill_price: Optional[float] = None,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        detail: Optional[str] = None,
        raw: Optional[dict] = None,
    ) -> None:
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
            logger.warning("late winning failed to record order event %s: %s", event_type, exc)

    def _record_guardrail(
        self,
        event_type: str,
        market_ticker: Optional[str],
        side: Optional[str],
        reason: str,
    ) -> None:
        try:
            execute_query(
                """
                INSERT INTO momentum_live_guardrail_events
                    (event_type, market_ticker, side, reason, metrics_json)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    event_type, market_ticker, side, reason,
                    json.dumps({"strategy": config.LATE_WINNING_PROFILE}),
                ),
            )
        except Exception as exc:
            logger.warning("late winning failed to record guardrail %s: %s", event_type, exc)
