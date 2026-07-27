"""
Prospective paper tracker for the frozen 08:00-11:00 ET BTC impulse rule.

No orders are placed. The tracker logs one paper trade or skip row per ET
calendar day.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app import config
from app.db import execute_query, fetch_one, insert_and_get_id
from app.features import Tick

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class BtcImpulseSignal:
    et_date: date
    market_db_id: int
    market_ticker: str
    contract_id: int
    side: str
    captured_at: datetime
    captured_at_et: datetime
    btc_price: float
    btc_price_60s_ago: float
    btc_60s_move: float
    entry_bid: float
    entry_ask: float
    entry_spread: float


@dataclass
class _ActivePaperTrade:
    trade_id: int
    et_date: date
    market_ticker: str
    side: str
    entry_at: datetime
    exit_due_at: datetime
    entry_ask: float


def _price(prices: dict[str, dict], side: str, key: str) -> Optional[float]:
    value = (prices.get(side) or {}).get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spread(prices: dict[str, dict], side: str) -> Optional[float]:
    spread = _price(prices, side, "spread")
    if spread is not None:
        return spread
    bid = _price(prices, side, "bid_price")
    ask = _price(prices, side, "ask_price")
    if bid is None or ask is None:
        return None
    return ask - bid


def _side_for_btc_move(move: float) -> Optional[str]:
    if move > 0:
        return "YES"
    if move < 0:
        return "NO"
    return None


def _clean_side(prices: dict[str, dict], side: str, max_spread: float) -> bool:
    bid = _price(prices, side, "bid_price")
    ask = _price(prices, side, "ask_price")
    spread = _spread(prices, side)
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


def _fee_cents(price: float) -> float:
    price = max(0.0, min(1.0, price))
    return round(config.BTC_IMPULSE_PAPER_FEE_RATE_CENTS * price * (1.0 - price), 6)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo("UTC"))
    return value


def _price_ago(btc_ticks: list[Tick], captured_at: datetime, seconds: float) -> Optional[float]:
    cutoff = captured_at.timestamp() - seconds
    best = None
    for tick in btc_ticks:
        if tick.ts <= cutoff:
            best = tick
        else:
            break
    return best.price if best is not None else None


def _in_entry_window(captured_at: datetime) -> bool:
    et = captured_at.astimezone(ET)
    start = time(config.BTC_IMPULSE_PAPER_START_HOUR_ET, 0)
    end = time(config.BTC_IMPULSE_PAPER_END_HOUR_ET, 0)
    return start <= et.time() < end


def _window_has_passed(captured_at: datetime) -> bool:
    et = captured_at.astimezone(ET)
    return et.time() >= time(config.BTC_IMPULSE_PAPER_END_HOUR_ET, 0)


def find_btc_impulse_signal(
    *,
    market_db_id: int,
    market_ticker: str,
    contract_ids: dict[str, int],
    captured_at: datetime,
    btc_price: float,
    prices: dict[str, dict],
    btc_ticks: list[Tick],
) -> Optional[BtcImpulseSignal]:
    if not config.BTC_IMPULSE_PAPER_ENABLED:
        return None
    if not _in_entry_window(captured_at):
        return None
    if not _clean_quote(prices, config.BTC_IMPULSE_PAPER_MAX_SPREAD):
        return None

    prior = _price_ago(btc_ticks, captured_at, 60.0)
    if prior is None:
        return None
    move = round(btc_price - prior, 4)
    if abs(move) < config.BTC_IMPULSE_PAPER_BTC_60S_ABS_THRESHOLD:
        return None
    side = _side_for_btc_move(move)
    if side is None:
        return None
    contract_id = contract_ids.get(side)
    if contract_id is None:
        return None
    if not _clean_side(prices, side, config.BTC_IMPULSE_PAPER_MAX_SPREAD):
        return None

    bid = _price(prices, side, "bid_price")
    ask = _price(prices, side, "ask_price")
    spread = _spread(prices, side)
    if bid is None or ask is None or spread is None:
        return None

    return BtcImpulseSignal(
        et_date=captured_at.astimezone(ET).date(),
        market_db_id=market_db_id,
        market_ticker=market_ticker,
        contract_id=contract_id,
        side=side,
        captured_at=captured_at,
        captured_at_et=captured_at.astimezone(ET),
        btc_price=btc_price,
        btc_price_60s_ago=prior,
        btc_60s_move=move,
        entry_bid=bid,
        entry_ask=ask,
        entry_spread=spread,
    )


class BtcImpulsePaperTracker:
    def __init__(self) -> None:
        self._enabled = config.BTC_IMPULSE_PAPER_ENABLED
        self._profile = config.BTC_IMPULSE_PAPER_PROFILE
        self._active: dict[int, _ActivePaperTrade] = {}
        if self._enabled:
            self._load_active_rows()
            logger.info(
                "BtcImpulsePaperTracker enabled | profile=%s window=%02d:00-%02d:00 ET threshold=$%.2f exit=%ss spread<=%.3f",
                self._profile,
                config.BTC_IMPULSE_PAPER_START_HOUR_ET,
                config.BTC_IMPULSE_PAPER_END_HOUR_ET,
                config.BTC_IMPULSE_PAPER_BTC_60S_ABS_THRESHOLD,
                config.BTC_IMPULSE_PAPER_EXIT_SECONDS,
                config.BTC_IMPULSE_PAPER_MAX_SPREAD,
            )

    def _load_active_rows(self) -> None:
        row = fetch_one(
            """
            SELECT id, et_date, market_ticker, trade_side, entry_at, exit_due_at, entry_ask
            FROM btc_impulse_paper_trades
            WHERE profile=%s AND status='ACTIVE'
            ORDER BY entry_at DESC
            LIMIT 1
            """,
            (self._profile,),
        )
        if not row:
            return
        self._active[int(row["id"])] = _ActivePaperTrade(
            trade_id=int(row["id"]),
            et_date=row["et_date"],
            market_ticker=row["market_ticker"],
            side=row["trade_side"],
            entry_at=_as_utc(row["entry_at"]),
            exit_due_at=_as_utc(row["exit_due_at"]),
            entry_ask=float(row["entry_ask"]),
        )

    def on_tick(
        self,
        *,
        market_db_id: int,
        market_ticker: str,
        contract_ids: dict[str, int],
        captured_at: datetime,
        btc_price: float,
        prices: dict[str, dict],
        btc_ticks: list[Tick],
    ) -> None:
        if not self._enabled:
            return
        self._update_active(captured_at, prices)
        self._maybe_record_skip(captured_at)
        if self._has_row_for_day(captured_at.astimezone(ET).date()):
            return

        sig = find_btc_impulse_signal(
            market_db_id=market_db_id,
            market_ticker=market_ticker,
            contract_ids=contract_ids,
            captured_at=captured_at,
            btc_price=btc_price,
            prices=prices,
            btc_ticks=btc_ticks,
        )
        if sig is not None:
            self._insert_entry(sig)

    def _has_row_for_day(self, et_date: date) -> bool:
        row = fetch_one(
            "SELECT id FROM btc_impulse_paper_trades WHERE profile=%s AND et_date=%s LIMIT 1",
            (self._profile, et_date),
        )
        return row is not None

    def _maybe_record_skip(self, captured_at: datetime) -> None:
        if not _window_has_passed(captured_at):
            return
        et_date = captured_at.astimezone(ET).date()
        if self._has_row_for_day(et_date):
            return
        summary = self._summary_text(
            status="NO_TRADE",
            et_date=et_date,
            net=None,
            reason="no clean BTC 60s impulse signal before 11:00 ET",
        )
        execute_query(
            """
            INSERT IGNORE INTO btc_impulse_paper_trades (
                profile, et_date, status, skip_reason, first_valid_signal_of_day,
                exit_horizon_seconds, exit_tolerance_seconds, summary_text
            ) VALUES (%s, %s, 'NO_TRADE', %s, TRUE, %s, %s, %s)
            """,
            (
                self._profile,
                et_date,
                "no_clean_btc_60s_impulse_signal_before_window_close",
                int(config.BTC_IMPULSE_PAPER_EXIT_SECONDS),
                int(config.BTC_IMPULSE_PAPER_EXIT_TOLERANCE_SECONDS),
                summary,
            ),
        )
        logger.info("BTC impulse PAPER daily summary\n%s", summary)

    def _insert_entry(self, sig: BtcImpulseSignal) -> None:
        entry_fee = _fee_cents(sig.entry_ask)
        metadata = {
            "frozen_rule": {
                "window_et": f"{config.BTC_IMPULSE_PAPER_START_HOUR_ET:02d}:00-{config.BTC_IMPULSE_PAPER_END_HOUR_ET:02d}:00",
                "btc_60s_abs_threshold": config.BTC_IMPULSE_PAPER_BTC_60S_ABS_THRESHOLD,
                "max_spread": config.BTC_IMPULSE_PAPER_MAX_SPREAD,
                "exit_seconds": config.BTC_IMPULSE_PAPER_EXIT_SECONDS,
                "fee_rate_cents": config.BTC_IMPULSE_PAPER_FEE_RATE_CENTS,
            }
        }
        trade_id = insert_and_get_id(
            """
            INSERT INTO btc_impulse_paper_trades (
                profile, et_date, status,
                market_db_id, market_ticker, contract_id, trade_side,
                entry_at, entry_at_et, exit_due_at,
                btc_price_at_entry, btc_price_60s_before_entry, btc_60s_move,
                entry_bid, entry_ask, entry_spread,
                entry_fee_cents, fee_cents,
                first_valid_signal_of_day, clean_entry_quote,
                exit_horizon_seconds, exit_tolerance_seconds,
                metadata_json
            ) VALUES (
                %s, %s, 'ACTIVE',
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                TRUE, TRUE,
                %s, %s,
                %s
            )
            ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
            """,
            (
                self._profile,
                sig.et_date,
                sig.market_db_id,
                sig.market_ticker,
                sig.contract_id,
                sig.side,
                sig.captured_at,
                sig.captured_at_et.replace(tzinfo=None),
                sig.captured_at + timedelta(seconds=config.BTC_IMPULSE_PAPER_EXIT_SECONDS),
                sig.btc_price,
                sig.btc_price_60s_ago,
                sig.btc_60s_move,
                sig.entry_bid,
                sig.entry_ask,
                sig.entry_spread,
                entry_fee,
                entry_fee,
                int(config.BTC_IMPULSE_PAPER_EXIT_SECONDS),
                int(config.BTC_IMPULSE_PAPER_EXIT_TOLERANCE_SECONDS),
                json.dumps(metadata),
            ),
        )
        self._active[trade_id] = _ActivePaperTrade(
            trade_id=trade_id,
            et_date=sig.et_date,
            market_ticker=sig.market_ticker,
            side=sig.side,
            entry_at=sig.captured_at,
            exit_due_at=sig.captured_at + timedelta(seconds=config.BTC_IMPULSE_PAPER_EXIT_SECONDS),
            entry_ask=sig.entry_ask,
        )
        logger.info(
            "BTC impulse PAPER entry | date=%s market=%s side=%s btc60=%+.2f entry=%.4f",
            sig.et_date,
            sig.market_ticker,
            sig.side,
            sig.btc_60s_move,
            sig.entry_ask,
        )

    def _update_active(self, captured_at: datetime, prices: dict[str, dict]) -> None:
        if not self._active:
            return
        for trade_id, active in list(self._active.items()):
            if captured_at < active.exit_due_at:
                continue
            if captured_at > active.exit_due_at + timedelta(seconds=config.BTC_IMPULSE_PAPER_EXIT_TOLERANCE_SECONDS):
                self._mark_no_valid_exit(active)
                self._active.pop(trade_id, None)
                continue
            if not _clean_side(prices, active.side, config.BTC_IMPULSE_PAPER_MAX_SPREAD):
                continue
            exit_bid = _price(prices, active.side, "bid_price")
            if exit_bid is None:
                continue
            self._complete_trade(active, captured_at, exit_bid)
            self._active.pop(trade_id, None)

    def _complete_trade(self, active: _ActivePaperTrade, exit_at: datetime, exit_bid: float) -> None:
        entry_fee = _fee_cents(active.entry_ask)
        exit_fee = _fee_cents(exit_bid)
        fee = round(entry_fee + exit_fee, 6)
        gross = round((exit_bid - active.entry_ask) * 100.0, 4)
        net = round(gross - fee, 4)
        stats = self._running_stats(extra_net=net)
        summary = self._summary_text("COMPLETE", active.et_date, net, reason=None, gross=gross, fee=fee)
        execute_query(
            """
            UPDATE btc_impulse_paper_trades
            SET status='COMPLETE',
                exit_at=%s,
                exit_at_et=%s,
                exit_bid=%s,
                gross_cents=%s,
                entry_fee_cents=%s,
                exit_fee_cents=%s,
                fee_cents=%s,
                net_cents=%s,
                running_total_net_cents=%s,
                running_drawdown_cents=%s,
                clean_exit_quote=TRUE,
                summary_text=%s
            WHERE id=%s
            """,
            (
                exit_at,
                exit_at.astimezone(ET).replace(tzinfo=None),
                exit_bid,
                gross,
                entry_fee,
                exit_fee,
                fee,
                net,
                stats["running_total"],
                stats["drawdown"],
                summary,
                active.trade_id,
            ),
        )
        logger.info("BTC impulse PAPER daily summary\n%s", summary)

    def _mark_no_valid_exit(self, active: _ActivePaperTrade) -> None:
        summary = self._summary_text(
            status="NO_VALID_EXIT",
            et_date=active.et_date,
            net=None,
            reason="no clean exit quote within tolerance",
        )
        execute_query(
            """
            UPDATE btc_impulse_paper_trades
            SET status='NO_VALID_EXIT',
                skip_reason=%s,
                summary_text=%s
            WHERE id=%s
            """,
            ("no_clean_exit_quote_within_tolerance", summary, active.trade_id),
        )
        logger.info("BTC impulse PAPER daily summary\n%s", summary)

    def _running_stats(self, *, extra_net: Optional[float] = None) -> dict[str, float]:
        rows = self._completed_nets()
        if extra_net is not None:
            rows.append(extra_net)
        running = 0.0
        peak = 0.0
        drawdown = 0.0
        for net in rows:
            running += net
            peak = max(peak, running)
            drawdown = min(drawdown, running - peak)
        return {"running_total": round(running, 4), "drawdown": round(drawdown, 4)}

    def _completed_nets(self) -> list[float]:
        from app.db import fetch_all

        rows = fetch_all(
            """
            SELECT net_cents
            FROM btc_impulse_paper_trades
            WHERE profile=%s AND status='COMPLETE' AND net_cents IS NOT NULL
            ORDER BY et_date, entry_at
            """,
            (self._profile,),
        )
        return [float(row["net_cents"]) for row in rows]

    def _summary_text(
        self,
        status: str,
        et_date: date,
        net: Optional[float],
        reason: Optional[str],
        gross: Optional[float] = None,
        fee: Optional[float] = None,
    ) -> str:
        stats = self._running_stats(extra_net=net)
        lines = [
            f"Subject: BTC impulse paper test - {et_date} - {status}",
            "",
            f"Profile: {self._profile}",
            f"Date ET: {et_date}",
            f"Status: {status}",
        ]
        if reason:
            lines.append(f"Reason: {reason}")
        if gross is not None:
            lines.append(f"Gross cents: {gross:.4f}")
        if fee is not None:
            lines.append(f"Fee cents: {fee:.4f}")
        if net is not None:
            lines.append(f"Net cents: {net:.4f}")
        lines.extend(
            [
                f"Running total net cents: {stats['running_total']:.4f}",
                f"Running drawdown cents: {stats['drawdown']:.4f}",
                "Rule frozen: 08:00-11:00 ET, first clean abs BTC 60s move >= $50, aligned side, ask entry, 120s bid exit.",
            ]
        )
        return "\n".join(lines)
