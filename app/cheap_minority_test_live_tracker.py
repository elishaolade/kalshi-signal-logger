"""
Real-money TEST tracker for the cheap minority 20-30c strategy.

TEST = Thirty Emailed Small Live Trades. This module can place real orders only
when CHEAP_MINORITY_TEST_* gates are explicitly armed. It is intentionally
separate from the momentum and late-winning live traders.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app import config
from app.db import execute_query, fetch_all, fetch_one, insert_and_get_id
from app.kalshi_trading import KalshiTradingClient, KalshiTradingError, is_authenticated
from app.momentum_live_trader import _summarize_fills, is_order_open, kill_switch_engaged

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CheapMinoritySignal:
    market_db_id: int
    contract_id: int
    market_ticker: str
    side: str
    dominant_side: str
    minority_side: str
    signal_at: datetime
    market_open_et: datetime
    market_close_et: datetime
    seconds_since_open: float
    entry_bid: float
    entry_ask: float
    entry_spread: float
    btc_price_at_market_open: Optional[float]
    btc_price_60s_before_entry: Optional[float]
    btc_price_at_entry: float
    btc_60s_move: Optional[float]


@dataclass
class _QuoteRow:
    captured_at: datetime
    btc_price: float
    prices: dict[str, dict]


@dataclass
class _ActiveTrade:
    trade_id: int
    et_date: date
    market_ticker: str
    side: str
    contract_id: int
    requested_contracts: int
    entry_ask: float
    entry_client_order_id: str
    entry_order_id: Optional[str]
    entry_submit_ts: float
    status: str
    filled_contracts: int = 0
    actual_entry_price: Optional[float] = None
    actual_entry_fees: Optional[float] = None
    entry_at: Optional[datetime] = None
    exit_reason: Optional[str] = None
    exit_order_id: Optional[str] = None
    exit_client_order_id: Optional[str] = None
    exit_submit_ts: Optional[float] = None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(prices: dict[str, dict], side: str, key: str) -> Optional[float]:
    row = prices.get(side) or {}
    value = row.get(key)
    if value is None and key == "spread":
        bid = _safe_float(row.get("bid_price"))
        ask = _safe_float(row.get("ask_price"))
        return None if bid is None or ask is None else round(ask - bid, 4)
    return _safe_float(value)


def _minority_side(side: str) -> str:
    return "NO" if side == "YES" else "YES"


def _dominant_side(prices: dict[str, dict]) -> Optional[str]:
    yes = _price(prices, "YES", "ask_price")
    no = _price(prices, "NO", "ask_price")
    if yes is None or no is None:
        return None
    if yes > no:
        return "YES"
    if no > yes:
        return "NO"
    return None


def _clean_side(prices: dict[str, dict], side: str, max_spread: float) -> bool:
    bid = _price(prices, side, "bid_price")
    ask = _price(prices, side, "ask_price")
    spread = _price(prices, side, "spread")
    if bid is None or ask is None or spread is None:
        return False
    if bid < 0 or ask < 0 or bid > 1 or ask > 1:
        return False
    if bid > ask:
        return False
    if spread <= 0 or spread > max_spread:
        return False
    return True


def _clean_quote(prices: dict[str, dict], max_spread: float) -> bool:
    return _clean_side(prices, "YES", max_spread) and _clean_side(prices, "NO", max_spread)


def _last_at_or_before(rows: list[_QuoteRow], ts: float) -> Optional[_QuoteRow]:
    out = None
    for row in rows:
        if row.captured_at.timestamp() <= ts:
            out = row
        else:
            break
    return out


def _fee_cents(price: float) -> float:
    price = max(0.0, min(1.0, price))
    return round(config.CHEAP_MINORITY_TEST_FEE_RATE_CENTS * price * (1.0 - price), 6)


def _order_id(payload: Optional[dict]) -> str:
    if not isinstance(payload, dict):
        return ""
    order = payload.get("order") if isinstance(payload.get("order"), dict) else payload
    return str(order.get("order_id") or order.get("id") or "")


def _balance_dollars(payload: Optional[dict]) -> Optional[float]:
    if not isinstance(payload, dict):
        return None
    for key in ("balance", "available_balance", "cash_balance", "portfolio_value"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return round(float(value) / 100.0, 4)
        except (TypeError, ValueError):
            continue
    return None


def find_cheap_minority_signal(
    *,
    market_db_id: int,
    market_ticker: str,
    contract_ids: dict[str, int],
    opens_at: datetime,
    closes_at: datetime,
    captured_at: datetime,
    btc_price: float,
    prices: dict[str, dict],
    market_rows: list[_QuoteRow],
) -> Optional[CheapMinoritySignal]:
    captured_et = captured_at.astimezone(ET)
    if not (config.CHEAP_MINORITY_TEST_START_HOUR_ET <= captured_et.hour < config.CHEAP_MINORITY_TEST_END_HOUR_ET):
        return None
    elapsed = (captured_at - opens_at).total_seconds()
    if elapsed < 0 or elapsed > config.CHEAP_MINORITY_TEST_MAX_ENTRY_SECONDS_AFTER_OPEN:
        return None
    if not _clean_quote(prices, config.CHEAP_MINORITY_TEST_MAX_SPREAD):
        return None

    dominant = _dominant_side(prices)
    if dominant is None:
        return None
    minority = _minority_side(dominant)
    contract_id = contract_ids.get(minority)
    if contract_id is None:
        return None
    bid = _price(prices, minority, "bid_price")
    ask = _price(prices, minority, "ask_price")
    spread = _price(prices, minority, "spread")
    if bid is None or ask is None or spread is None:
        return None
    if ask < config.CHEAP_MINORITY_TEST_MIN_ASK or ask >= config.CHEAP_MINORITY_TEST_MAX_ASK:
        return None
    if spread > config.CHEAP_MINORITY_TEST_MAX_SPREAD:
        return None

    open_row = market_rows[0] if market_rows else None
    prev60 = _last_at_or_before(market_rows, captured_at.timestamp() - 60.0)
    btc_60_before = prev60.btc_price if prev60 else None
    return CheapMinoritySignal(
        market_db_id=market_db_id,
        contract_id=contract_id,
        market_ticker=market_ticker,
        side=minority,
        dominant_side=dominant,
        minority_side=minority,
        signal_at=captured_at,
        market_open_et=opens_at.astimezone(ET),
        market_close_et=closes_at.astimezone(ET),
        seconds_since_open=round(elapsed, 3),
        entry_bid=bid,
        entry_ask=ask,
        entry_spread=spread,
        btc_price_at_market_open=open_row.btc_price if open_row else None,
        btc_price_60s_before_entry=btc_60_before,
        btc_price_at_entry=btc_price,
        btc_60s_move=round(btc_price - btc_60_before, 2) if btc_60_before is not None else None,
    )


def is_cheap_minority_test_armed() -> tuple[bool, str]:
    if not config.CHEAP_MINORITY_TEST_ENABLED:
        return False, "CHEAP_MINORITY_TEST_ENABLED is not true"
    if config.CHEAP_MINORITY_TEST_CONFIRM != config.MOMENTUM_LIVE_CONFIRM_TOKEN:
        return False, "CHEAP_MINORITY_TEST_CONFIRM token not set correctly"
    if not is_authenticated():
        return False, "Kalshi RSA auth not configured"
    if kill_switch_engaged():
        return False, "kill switch engaged"
    if config.CHEAP_MINORITY_TEST_CONTRACTS != 2:
        return False, "CHEAP_MINORITY_TEST_CONTRACTS must equal 2 for this frozen TEST"
    if config.CHEAP_MINORITY_TEST_MIN_ASK != 0.20 or config.CHEAP_MINORITY_TEST_MAX_ASK != 0.30:
        return False, "cheap minority ask bucket must remain frozen at [0.20,0.30)"
    if config.CHEAP_MINORITY_TEST_TARGET_BID != 0.95:
        return False, "target bid must remain frozen at 0.95"
    return True, ""


class CheapMinorityRealMoneyTestTracker:
    def __init__(self) -> None:
        self._armed, self._inert_reason = is_cheap_minority_test_armed()
        self._client = KalshiTradingClient(require_auth=True) if self._armed else None
        self._current_market: Optional[str] = None
        self._rows: list[_QuoteRow] = []
        self._active: dict[int, _ActiveTrade] = {}
        self._daily_markets: dict[date, set[str]] = defaultdict(set)
        self._daily_eligible: dict[date, int] = defaultdict(int)

        if self._armed:
            self._rehydrate_active()
            logger.warning(
                "CheapMinorityRealMoneyTEST ARMED - REAL ORDERS ENABLED | profile=%s "
                "contracts=%d ask=[%.2f,%.2f) target_bid=%.2f one/day window=%02d-%02d ET",
                config.CHEAP_MINORITY_TEST_PROFILE,
                config.CHEAP_MINORITY_TEST_CONTRACTS,
                config.CHEAP_MINORITY_TEST_MIN_ASK,
                config.CHEAP_MINORITY_TEST_MAX_ASK,
                config.CHEAP_MINORITY_TEST_TARGET_BID,
                config.CHEAP_MINORITY_TEST_START_HOUR_ET,
                config.CHEAP_MINORITY_TEST_END_HOUR_ET,
            )
        else:
            logger.info("CheapMinorityRealMoneyTEST INERT - %s", self._inert_reason)

    def on_tick(
        self,
        *,
        market_db_id: int,
        market_ticker: str,
        contract_ids: dict[str, int],
        opens_at: Optional[datetime],
        closes_at: Optional[datetime],
        captured_at: datetime,
        btc_price: float,
        tte: Optional[int],
        prices: dict[str, dict],
    ) -> None:
        if not self._armed or opens_at is None or closes_at is None:
            return
        if self._current_market != market_ticker:
            self._current_market = market_ticker
            self._rows = []
        self._rows.append(_QuoteRow(captured_at=captured_at, btc_price=btc_price, prices=prices))
        self._rows = self._rows[-600:]

        et_date = captured_at.astimezone(ET).date()
        captured_et = captured_at.astimezone(ET)
        if config.CHEAP_MINORITY_TEST_START_HOUR_ET <= captured_et.hour < config.CHEAP_MINORITY_TEST_END_HOUR_ET:
            self._daily_markets[et_date].add(market_ticker)

        self._advance_active(prices, captured_at, tte)
        self._maybe_write_no_trade_summary(captured_at)

        if self._stop_reached() or self._day_consumed(et_date):
            return
        sig = find_cheap_minority_signal(
            market_db_id=market_db_id,
            market_ticker=market_ticker,
            contract_ids=contract_ids,
            opens_at=opens_at,
            closes_at=closes_at,
            captured_at=captured_at,
            btc_price=btc_price,
            prices=prices,
            market_rows=self._rows,
        )
        if sig is None:
            return
        self._daily_eligible[et_date] += 1
        self._attempt_entry(sig)

    def _stop_reached(self) -> bool:
        row = fetch_one(
            "SELECT COUNT(*) AS n FROM cheap_minority_test_trades "
            "WHERE profile=%s AND status='COMPLETE'",
            (config.CHEAP_MINORITY_TEST_PROFILE,),
        )
        if row and int(row["n"]) >= config.CHEAP_MINORITY_TEST_MAX_COMPLETED_TRADES:
            return True
        row = fetch_one(
            "SELECT COUNT(DISTINCT et_date) AS n FROM ("
            "SELECT et_date FROM cheap_minority_test_trades WHERE profile=%s "
            "UNION SELECT et_date FROM cheap_minority_test_skipped_days WHERE profile=%s"
            ") x",
            (config.CHEAP_MINORITY_TEST_PROFILE, config.CHEAP_MINORITY_TEST_PROFILE),
        )
        return bool(row and int(row["n"]) >= config.CHEAP_MINORITY_TEST_MAX_CALENDAR_DAYS)

    def _day_consumed(self, et_date: date) -> bool:
        row = fetch_one(
            "SELECT 1 FROM cheap_minority_test_trades WHERE profile=%s AND et_date=%s "
            "UNION SELECT 1 FROM cheap_minority_test_skipped_days WHERE profile=%s AND et_date=%s LIMIT 1",
            (config.CHEAP_MINORITY_TEST_PROFILE, et_date, config.CHEAP_MINORITY_TEST_PROFILE, et_date),
        )
        return bool(row)

    def _attempt_entry(self, sig: CheapMinoritySignal) -> None:
        et_date = sig.signal_at.astimezone(ET).date()
        balance_before = self._get_balance()
        modeled_entry_fee_dollars = (_fee_cents(sig.entry_ask) / 100.0) * config.CHEAP_MINORITY_TEST_CONTRACTS
        required = sig.entry_ask * config.CHEAP_MINORITY_TEST_CONTRACTS + modeled_entry_fee_dollars
        if balance_before is not None and balance_before < required:
            self._upsert_skip(et_date, "insufficient_balance_for_2_contracts", insufficient_balance=True, balance=balance_before)
            self._write_daily_summary(et_date)
            return

        # Re-check current spread immediately before order submission.
        if sig.entry_spread > config.CHEAP_MINORITY_TEST_MAX_SPREAD:
            self._upsert_skip(et_date, "spread_violation_before_order", spread_violation=True, balance=balance_before)
            self._write_daily_summary(et_date)
            return

        trade_no = self._next_trade_number()
        coid = f"cmt-{et_date.isoformat()}-{uuid.uuid4().hex[:12]}"
        trade_id = insert_and_get_id(
            "INSERT INTO cheap_minority_test_trades ("
            "test_id, profile, test_label, test_trade_number, et_date, market_id, contract_id, "
            "market_ticker, market_open_et, market_close_et, entry_signal_at, seconds_since_market_open, "
            "side, dominant_side, minority_side, entry_bid, entry_ask, entry_spread, entry_quote_clean, "
            "contracts_attempted, entry_limit_price, entry_client_order_id, account_balance_before_trade, "
            "btc_price_at_market_open, btc_price_60s_before_entry, btc_price_at_entry, btc_60s_move, "
            "target_level, status, metadata_json"
            ") VALUES ("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING_ENTRY',%s"
            ")",
            (
                config.CHEAP_MINORITY_TEST_PROFILE,
                config.CHEAP_MINORITY_TEST_PROFILE,
                config.CHEAP_MINORITY_TEST_LABEL,
                trade_no,
                et_date,
                sig.market_db_id,
                sig.contract_id,
                sig.market_ticker,
                sig.market_open_et.replace(tzinfo=None),
                sig.market_close_et.replace(tzinfo=None),
                sig.signal_at,
                sig.seconds_since_open,
                sig.side,
                sig.dominant_side,
                sig.minority_side,
                sig.entry_bid,
                sig.entry_ask,
                sig.entry_spread,
                1,
                config.CHEAP_MINORITY_TEST_CONTRACTS,
                sig.entry_ask,
                coid,
                balance_before,
                sig.btc_price_at_market_open,
                sig.btc_price_60s_before_entry,
                sig.btc_price_at_entry,
                sig.btc_60s_move,
                config.CHEAP_MINORITY_TEST_TARGET_BID,
                json.dumps({"unfilled_consumes_day": config.CHEAP_MINORITY_TEST_UNFILLED_CONSUMES_DAY}),
            ),
        )
        active = _ActiveTrade(
            trade_id=trade_id,
            et_date=et_date,
            market_ticker=sig.market_ticker,
            side=sig.side,
            contract_id=sig.contract_id,
            requested_contracts=config.CHEAP_MINORITY_TEST_CONTRACTS,
            entry_ask=sig.entry_ask,
            entry_client_order_id=coid,
            entry_order_id=None,
            entry_submit_ts=datetime.now(timezone.utc).timestamp(),
            status="PENDING_ENTRY",
        )
        try:
            order = self._client.place_order(
                ticker=sig.market_ticker,
                side=sig.side,
                action="buy",
                count=config.CHEAP_MINORITY_TEST_CONTRACTS,
                limit_price=sig.entry_ask,
                order_type="limit",
                client_order_id=coid,
            )
        except KalshiTradingError as exc:
            execute_query(
                "UPDATE cheap_minority_test_trades SET status='REJECTED', notes=%s WHERE id=%s",
                (str(exc)[:500], trade_id),
            )
            self._record_event(active, "entry_rejected", action="buy", detail=str(exc)[:500])
            self._write_daily_summary(et_date)
            logger.error("cheap minority TEST entry rejected #%d: %s", trade_id, exc)
            return

        active.entry_order_id = _order_id(order)
        execute_query(
            "UPDATE cheap_minority_test_trades SET entry_order_id=%s WHERE id=%s",
            (active.entry_order_id, trade_id),
        )
        self._record_event(
            active,
            "entry_submitted",
            action="buy",
            requested_count=active.requested_contracts,
            limit_price=sig.entry_ask,
            order_id=active.entry_order_id,
            client_order_id=coid,
            raw=order,
        )
        self._active[trade_id] = active
        logger.warning(
            "cheap minority TEST ENTRY SUBMITTED #%d | %s %s x%d @ %.3f",
            trade_id, sig.market_ticker, sig.side, active.requested_contracts, sig.entry_ask,
        )

    def _advance_active(self, prices: dict[str, dict], captured_at: datetime, tte: Optional[int]) -> None:
        for trade_id, active in list(self._active.items()):
            if active.status == "PENDING_ENTRY":
                self._advance_pending_entry(active, captured_at)
            elif active.status == "ACTIVE":
                self._advance_position(active, prices, captured_at, tte)
            elif active.status == "PENDING_EXIT":
                self._advance_pending_exit(active, prices, captured_at)

    def _advance_pending_entry(self, active: _ActiveTrade, captured_at: datetime) -> None:
        fills = self._client.get_fills(order_id=active.entry_order_id)
        count, avg, fees = _summarize_fills(fills, active.side)
        active.filled_contracts = count
        active.actual_entry_price = avg
        active.actual_entry_fees = fees
        if count >= active.requested_contracts:
            self._promote_entry(active, captured_at)
            return
        order = self._client.get_order(active.entry_order_id) if active.entry_order_id else None
        if is_order_open(order) and datetime.now(timezone.utc).timestamp() - active.entry_submit_ts <= config.CHEAP_MINORITY_TEST_ENTRY_FILL_TIMEOUT_SECONDS:
            return
        if active.entry_order_id and is_order_open(order):
            try:
                self._client.cancel_order(active.entry_order_id)
            except KalshiTradingError as exc:
                logger.warning("cheap minority TEST entry cancel failed #%d: %s", active.trade_id, exc)
        if count > 0:
            self._promote_entry(active, captured_at)
            return
        execute_query(
            "UPDATE cheap_minority_test_trades SET status='MISSED_FILL', contracts_filled=0 WHERE id=%s",
            (active.trade_id,),
        )
        self._record_event(active, "missed_fill", action="buy", requested_count=active.requested_contracts, filled_count=0)
        self._upsert_skip(active.et_date, "missed_fill_entry_order_unfilled", order_attempted=True, missed_fill=True, balance=self._get_balance())
        self._write_daily_summary(active.et_date)
        self._active.pop(active.trade_id, None)

    def _promote_entry(self, active: _ActiveTrade, captured_at: datetime) -> None:
        active.status = "ACTIVE"
        active.entry_at = captured_at
        notional = (active.actual_entry_price or active.entry_ask) * active.filled_contracts
        execute_query(
            "UPDATE cheap_minority_test_trades SET status='ACTIVE', contracts_filled=%s, "
            "actual_avg_entry_price=%s, actual_entry_fees_dollars=%s, notional_cost_dollars=%s, entry_at=%s "
            "WHERE id=%s",
            (active.filled_contracts, active.actual_entry_price, active.actual_entry_fees, notional, captured_at, active.trade_id),
        )
        self._record_event(
            active,
            "entry_filled",
            action="buy",
            requested_count=active.requested_contracts,
            filled_count=active.filled_contracts,
            avg_fill_price=active.actual_entry_price,
            fees=active.actual_entry_fees,
            order_id=active.entry_order_id,
        )
        logger.warning(
            "cheap minority TEST ENTRY FILLED #%d | %s %s %d/%d @ %.3f",
            active.trade_id, active.market_ticker, active.side, active.filled_contracts,
            active.requested_contracts, active.actual_entry_price or 0.0,
        )

    def _advance_position(self, active: _ActiveTrade, prices: dict[str, dict], captured_at: datetime, tte: Optional[int]) -> None:
        bid = _price(prices, active.side, "bid_price")
        if bid is None or not _clean_side(prices, active.side, config.CHEAP_MINORITY_TEST_MAX_SPREAD):
            return
        reason = None
        if bid >= config.CHEAP_MINORITY_TEST_TARGET_BID:
            reason = "target_hit"
            execute_query(
                "UPDATE cheap_minority_test_trades SET target_hit_flag=1, target_hit_at=%s WHERE id=%s",
                (captured_at, active.trade_id),
            )
        elif tte is not None and tte <= config.CHEAP_MINORITY_TEST_CLOSE_EXIT_TTE_SECONDS:
            reason = "close_exit"
        if reason is None:
            return
        self._submit_exit(active, bid, reason, captured_at)

    def _submit_exit(self, active: _ActiveTrade, bid: float, reason: str, captured_at: datetime) -> None:
        coid = f"cmx-{active.trade_id}-{uuid.uuid4().hex[:10]}"
        active.exit_client_order_id = coid
        active.exit_reason = reason
        active.exit_submit_ts = datetime.now(timezone.utc).timestamp()
        active.status = "PENDING_EXIT"
        execute_query(
            "UPDATE cheap_minority_test_trades SET status='PENDING_EXIT', exit_reason=%s, "
            "exit_bid_observed=%s, exit_order_limit_price=%s, exit_client_order_id=%s WHERE id=%s",
            (reason, bid, bid, coid, active.trade_id),
        )
        try:
            order = self._client.place_order(
                ticker=active.market_ticker,
                side=active.side,
                action="sell",
                count=active.filled_contracts,
                limit_price=bid,
                order_type="limit",
                client_order_id=coid,
            )
        except KalshiTradingError as exc:
            execute_query(
                "UPDATE cheap_minority_test_trades SET status='MISSED_EXIT', notes=%s WHERE id=%s",
                (str(exc)[:500], active.trade_id),
            )
            self._record_event(active, "exit_rejected", action="sell", limit_price=bid, detail=str(exc)[:500])
            logger.error("cheap minority TEST exit rejected #%d: %s", active.trade_id, exc)
            self._active.pop(active.trade_id, None)
            return
        active.exit_order_id = _order_id(order)
        execute_query(
            "UPDATE cheap_minority_test_trades SET exit_order_id=%s WHERE id=%s",
            (active.exit_order_id, active.trade_id),
        )
        self._record_event(
            active,
            "exit_submitted",
            action="sell",
            requested_count=active.filled_contracts,
            limit_price=bid,
            order_id=active.exit_order_id,
            client_order_id=coid,
            raw=order,
        )
        logger.warning(
            "cheap minority TEST EXIT SUBMITTED #%d | %s x%d @ %.3f reason=%s",
            active.trade_id, active.side, active.filled_contracts, bid, reason,
        )

    def _advance_pending_exit(self, active: _ActiveTrade, prices: dict[str, dict], captured_at: datetime) -> None:
        if not active.exit_order_id:
            execute_query(
                "UPDATE cheap_minority_test_trades SET status='MISSED_EXIT', notes='missing exit order id' WHERE id=%s",
                (active.trade_id,),
            )
            self._record_event(active, "missed_exit", action="sell", detail="missing exit order id")
            self._active.pop(active.trade_id, None)
            return
        fills = self._client.get_fills(order_id=active.exit_order_id)
        count, avg, fees = _summarize_fills(fills, active.side)
        if count >= active.filled_contracts:
            self._complete_trade(active, avg, fees, captured_at)
            return
        if active.exit_submit_ts and datetime.now(timezone.utc).timestamp() - active.exit_submit_ts <= config.CHEAP_MINORITY_TEST_EXIT_FILL_TIMEOUT_SECONDS:
            return
        if active.exit_order_id:
            try:
                order = self._client.get_order(active.exit_order_id)
                if is_order_open(order):
                    self._client.cancel_order(active.exit_order_id)
            except KalshiTradingError as exc:
                logger.warning("cheap minority TEST exit cancel failed #%d: %s", active.trade_id, exc)
        bid = _price(prices, active.side, "bid_price")
        if bid is not None and _clean_side(prices, active.side, config.CHEAP_MINORITY_TEST_MAX_SPREAD):
            self._submit_exit(active, bid, active.exit_reason or "close_exit", captured_at)
        else:
            execute_query(
                "UPDATE cheap_minority_test_trades SET status='MISSED_EXIT' WHERE id=%s",
                (active.trade_id,),
            )
            self._record_event(active, "missed_exit", action="sell", filled_count=count)
            self._active.pop(active.trade_id, None)

    def _complete_trade(self, active: _ActiveTrade, exit_avg: Optional[float], exit_fees: Optional[float], captured_at: datetime) -> None:
        entry = active.actual_entry_price or active.entry_ask
        exit_price = exit_avg or 0.0
        actual_fees = (active.actual_entry_fees or 0.0) + (exit_fees or 0.0)
        actual_net = round((exit_price - entry) * active.filled_contracts - actual_fees, 6)
        modeled_gross_cents = round((exit_price - active.entry_ask) * 100.0, 4)
        modeled_fee_cents = round(_fee_cents(active.entry_ask) + _fee_cents(exit_price), 4)
        modeled_net_cents = round(modeled_gross_cents - modeled_fee_cents, 4)
        net_cents_per_contract = round((actual_net / active.filled_contracts) * 100.0, 4) if active.filled_contracts else None
        running, drawdown = self._running_after(actual_net)
        balance_after = self._get_balance()
        execute_query(
            "UPDATE cheap_minority_test_trades SET status='COMPLETE', exit_at=%s, actual_avg_exit_price=%s, "
            "actual_fees_dollars=%s, actual_net_dollars=%s, actual_net_cents_per_contract=%s, "
            "modeled_gross_cents_per_contract=%s, modeled_fee_cents_per_contract=%s, modeled_net_cents_per_contract=%s, "
            "account_balance_after_trade=%s, running_total_actual_net_dollars=%s, running_drawdown_dollars=%s, "
            "win_loss_flag=%s WHERE id=%s",
            (
                captured_at,
                exit_avg,
                actual_fees,
                actual_net,
                net_cents_per_contract,
                modeled_gross_cents,
                modeled_fee_cents,
                modeled_net_cents,
                balance_after,
                running,
                drawdown,
                "win" if actual_net > 0 else "loss",
                active.trade_id,
            ),
        )
        self._record_event(
            active,
            "exit_filled",
            action="sell",
            requested_count=active.filled_contracts,
            filled_count=active.filled_contracts,
            avg_fill_price=exit_avg,
            fees=exit_fees,
            order_id=active.exit_order_id,
        )
        self._record_account(active.et_date, captured_at, active.trade_id, balance_after, running, drawdown, "trade_complete")
        self._write_daily_summary(active.et_date)
        self._active.pop(active.trade_id, None)
        logger.warning(
            "cheap minority TEST COMPLETE #%d | net=$%.4f balance=%s",
            active.trade_id, actual_net, balance_after,
        )

    def _running_after(self, new_net: float) -> tuple[float, float]:
        rows = fetch_all(
            "SELECT actual_net_dollars FROM cheap_minority_test_trades "
            "WHERE profile=%s AND status='COMPLETE' AND actual_net_dollars IS NOT NULL",
            (config.CHEAP_MINORITY_TEST_PROFILE,),
        )
        vals = [float(r["actual_net_dollars"]) for r in rows]
        vals.append(new_net)
        total = round(sum(vals), 6)
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for value in vals:
            running += value
            peak = max(peak, running)
            max_dd = min(max_dd, running - peak)
        return total, round(max_dd, 6)

    def _next_trade_number(self) -> int:
        row = fetch_one(
            "SELECT COUNT(*) AS n FROM cheap_minority_test_trades WHERE profile=%s AND status='COMPLETE'",
            (config.CHEAP_MINORITY_TEST_PROFILE,),
        )
        return int(row["n"] if row else 0) + 1

    def _get_balance(self) -> Optional[float]:
        try:
            return _balance_dollars(self._client.get_balance())
        except Exception as exc:
            logger.warning("cheap minority TEST balance fetch failed: %s", exc)
            return None

    def _record_event(
        self,
        active: _ActiveTrade,
        event_type: str,
        *,
        action: Optional[str] = None,
        requested_count: Optional[int] = None,
        filled_count: Optional[int] = None,
        limit_price: Optional[float] = None,
        avg_fill_price: Optional[float] = None,
        fees: Optional[float] = None,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        detail: Optional[str] = None,
        raw: Optional[dict] = None,
    ) -> None:
        execute_query(
            "INSERT INTO cheap_minority_test_order_events ("
            "test_id, profile, trade_id, et_date, event_at, event_type, action, requested_count, filled_count, "
            "limit_price, avg_fill_price, fees_dollars, order_id, client_order_id, detail, raw_json"
            ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                config.CHEAP_MINORITY_TEST_PROFILE,
                config.CHEAP_MINORITY_TEST_PROFILE,
                active.trade_id,
                active.et_date,
                datetime.now(timezone.utc),
                event_type,
                action,
                requested_count,
                filled_count,
                limit_price,
                avg_fill_price,
                fees,
                order_id,
                client_order_id,
                detail,
                json.dumps(raw) if raw is not None else None,
            ),
        )

    def _record_account(self, et_date: date, observed_at: datetime, trade_id: Optional[int], balance: Optional[float], running: Optional[float], drawdown: Optional[float], event_type: str) -> None:
        execute_query(
            "INSERT INTO cheap_minority_test_account_curve (test_id, profile, et_date, observed_at, trade_id, "
            "account_balance_dollars, running_total_actual_net_dollars, running_drawdown_dollars, event_type) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                config.CHEAP_MINORITY_TEST_PROFILE,
                config.CHEAP_MINORITY_TEST_PROFILE,
                et_date,
                observed_at,
                trade_id,
                balance,
                running,
                drawdown,
                event_type,
            ),
        )

    def _upsert_skip(
        self,
        et_date: date,
        reason: str,
        *,
        order_attempted: bool = False,
        missed_fill: bool = False,
        spread_violation: bool = False,
        insufficient_balance: bool = False,
        platform_feed_issue: bool = False,
        balance: Optional[float] = None,
    ) -> None:
        execute_query(
            "INSERT INTO cheap_minority_test_skipped_days ("
            "test_id, profile, et_date, reason_no_trade, markets_checked, eligible_signals_found, "
            "order_attempted_flag, missed_fill_flag, spread_violation_flag, insufficient_balance_flag, "
            "platform_feed_issue_flag, account_balance_at_end_of_day"
            ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE reason_no_trade=VALUES(reason_no_trade), "
            "markets_checked=GREATEST(markets_checked, VALUES(markets_checked)), "
            "eligible_signals_found=GREATEST(eligible_signals_found, VALUES(eligible_signals_found)), "
            "order_attempted_flag=GREATEST(order_attempted_flag, VALUES(order_attempted_flag)), "
            "missed_fill_flag=GREATEST(missed_fill_flag, VALUES(missed_fill_flag)), "
            "spread_violation_flag=GREATEST(spread_violation_flag, VALUES(spread_violation_flag)), "
            "insufficient_balance_flag=GREATEST(insufficient_balance_flag, VALUES(insufficient_balance_flag)), "
            "platform_feed_issue_flag=GREATEST(platform_feed_issue_flag, VALUES(platform_feed_issue_flag)), "
            "account_balance_at_end_of_day=VALUES(account_balance_at_end_of_day)",
            (
                config.CHEAP_MINORITY_TEST_PROFILE,
                config.CHEAP_MINORITY_TEST_PROFILE,
                et_date,
                reason,
                len(self._daily_markets.get(et_date, set())),
                self._daily_eligible.get(et_date, 0),
                int(order_attempted),
                int(missed_fill),
                int(spread_violation),
                int(insufficient_balance),
                int(platform_feed_issue),
                balance,
            ),
        )

    def _maybe_write_no_trade_summary(self, captured_at: datetime) -> None:
        et_now = captured_at.astimezone(ET)
        for et_date in list(self._daily_markets):
            if et_date > et_now.date():
                continue
            window_done = et_date < et_now.date() or et_now.hour >= config.CHEAP_MINORITY_TEST_END_HOUR_ET
            if not window_done or self._day_consumed(et_date):
                continue
            self._upsert_skip(et_date, "no_eligible_signal_in_window", balance=self._get_balance())
            self._write_daily_summary(et_date)

    def _write_daily_summary(self, et_date: date) -> None:
        existing = fetch_one(
            "SELECT id FROM cheap_minority_test_daily_summaries WHERE profile=%s AND et_date=%s",
            (config.CHEAP_MINORITY_TEST_PROFILE, et_date),
        )
        if existing:
            return
        trade = fetch_one(
            "SELECT * FROM cheap_minority_test_trades WHERE profile=%s AND et_date=%s ORDER BY id DESC LIMIT 1",
            (config.CHEAP_MINORITY_TEST_PROFILE, et_date),
        )
        completed = fetch_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(actual_net_dollars),0) AS pnl FROM cheap_minority_test_trades "
            "WHERE profile=%s AND status='COMPLETE'",
            (config.CHEAP_MINORITY_TEST_PROFILE,),
        )
        if trade:
            text = (
                f"Real-Money TEST daily summary\n"
                f"Date: {et_date}\n"
                f"Trade number: {trade.get('test_trade_number')}\n"
                f"Market: {trade.get('market_ticker')}\n"
                f"Side: {trade.get('side')}\n"
                f"Entry ask: {trade.get('entry_ask')}\n"
                f"Contracts filled: {trade.get('contracts_filled')}\n"
                f"Exit reason: {trade.get('exit_reason')}\n"
                f"Actual net dollars: {trade.get('actual_net_dollars')}\n"
                f"Account balance after: {trade.get('account_balance_after_trade')}\n"
                f"Running TEST P/L: {completed.get('pnl') if completed else 0}\n"
                f"Completed TEST trades: {completed.get('n') if completed else 0}\n"
                f"Rule violation: {trade.get('rule_violation_flag')} {trade.get('rule_violation_reason') or ''}\n"
            )
        else:
            skip = fetch_one(
                "SELECT * FROM cheap_minority_test_skipped_days WHERE profile=%s AND et_date=%s",
                (config.CHEAP_MINORITY_TEST_PROFILE, et_date),
            )
            text = (
                f"Real-Money TEST daily summary\n"
                f"Date: {et_date}\n"
                f"No trade: {skip.get('reason_no_trade') if skip else 'unknown'}\n"
                f"Account balance: {skip.get('account_balance_at_end_of_day') if skip else None}\n"
                f"Completed TEST trades: {completed.get('n') if completed else 0}\n"
            )
        execute_query(
            "INSERT IGNORE INTO cheap_minority_test_daily_summaries (test_id, profile, et_date, summary_text) "
            "VALUES (%s,%s,%s,%s)",
            (config.CHEAP_MINORITY_TEST_PROFILE, config.CHEAP_MINORITY_TEST_PROFILE, et_date, text),
        )
        logger.info("cheap minority TEST daily summary generated | date=%s", et_date)

    def _rehydrate_active(self) -> None:
        rows = fetch_all(
            "SELECT * FROM cheap_minority_test_trades WHERE profile=%s AND status IN ('PENDING_ENTRY','ACTIVE','PENDING_EXIT')",
            (config.CHEAP_MINORITY_TEST_PROFILE,),
        )
        for row in rows:
            active = _ActiveTrade(
                trade_id=int(row["id"]),
                et_date=row["et_date"],
                market_ticker=str(row["market_ticker"]),
                side=str(row["side"]),
                contract_id=int(row["contract_id"]),
                requested_contracts=int(row["contracts_attempted"]),
                entry_ask=float(row["entry_ask"]),
                entry_client_order_id=str(row["entry_client_order_id"] or ""),
                entry_order_id=str(row["entry_order_id"] or "") or None,
                entry_submit_ts=datetime.now(timezone.utc).timestamp(),
                status=str(row["status"]),
                filled_contracts=int(row["contracts_filled"] or 0),
                actual_entry_price=_safe_float(row.get("actual_avg_entry_price")),
                actual_entry_fees=_safe_float(row.get("actual_entry_fees_dollars")),
                entry_at=row.get("entry_at"),
                exit_reason=row.get("exit_reason"),
                exit_order_id=str(row.get("exit_order_id") or "") or None,
                exit_client_order_id=str(row.get("exit_client_order_id") or "") or None,
                exit_submit_ts=datetime.now(timezone.utc).timestamp(),
            )
            self._active[active.trade_id] = active
