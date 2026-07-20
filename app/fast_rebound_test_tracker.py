"""
Research-only TEST tracker for the frozen 65-70c fast rebound hypothesis.

No orders are placed. The tracker logs modeled entries and exits using live
quotes only, with bot-managed target/stop models.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app import config
from app.db import execute_query, insert_and_get_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FastReboundSignal:
    market_db_id: int
    market_ticker: str
    contract_id: int
    dominant_side: str
    minority_side: str
    signal_at: datetime
    signal_ts: float
    entry_bid: float
    entry_ask: float
    entry_spread: float
    dominant_bid: float
    dominant_ask: float
    dominant_spread: float
    btc_price: float
    strike: float
    btc_distance_dominant_side: float
    time_since_open_seconds: float
    time_remaining_seconds: Optional[int]
    dominant_cents_per_second: float
    dominant_change_prev_30s_cents: float


@dataclass
class _QuoteRow:
    captured_at: datetime
    ts: float
    btc_price: float
    tte: Optional[int]
    prices: dict[str, dict]


@dataclass
class _ActiveTest:
    trade_id: int
    market_ticker: str
    contract_id: int
    side: str
    signal_ts: float
    entry_ask: float
    target_cents: float
    stop_cents: float
    target_bid_price: float
    stop_bid_price: float
    timeout_ts: float
    peak_bid: float
    trough_bid: float


def _price(prices: dict[str, dict], side: str, key: str) -> Optional[float]:
    value = (prices.get(side) or {}).get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _side_distance(side: str, btc_price: float, strike: float) -> float:
    return btc_price - strike if side == "YES" else strike - btc_price


def _clean_quote(prices: dict[str, dict], max_spread: float) -> bool:
    for side in ("YES", "NO"):
        bid = _price(prices, side, "bid_price")
        ask = _price(prices, side, "ask_price")
        spread = _price(prices, side, "spread")
        if bid is None or ask is None:
            return False
        if spread is None:
            spread = ask - bid
        if bid < 0 or ask < 0 or bid > 1 or ask > 1:
            return False
        if bid > ask:
            return False
        if spread <= 0 or spread > max_spread:
            return False
    return True


def _last_at_or_before(rows: list[_QuoteRow], ts: float) -> Optional[_QuoteRow]:
    best = None
    for row in rows:
        if row.ts <= ts:
            best = row
        else:
            break
    return best


def _fee_cents(price: float) -> float:
    price = max(0.0, min(1.0, price))
    return round(config.FAST_REBOUND_TEST_FEE_RATE_CENTS * price * (1.0 - price), 6)


def parse_exit_models(raw: str) -> list[tuple[float, float]]:
    models: list[tuple[float, float]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        target, stop = part.split(":", 1)
        models.append((float(target), float(stop)))
    if not models:
        raise ValueError("FAST_REBOUND_TEST_EXIT_MODELS produced no exit models")
    return models


def find_fast_rebound_signal(
    *,
    market_db_id: int,
    market_ticker: str,
    contract_ids: dict[str, int],
    target_price: float,
    opens_at: datetime,
    captured_at: datetime,
    btc_price: float,
    tte: Optional[int],
    prices: dict[str, dict],
    market_rows: list[_QuoteRow],
) -> Optional[FastReboundSignal]:
    if not config.FAST_REBOUND_TEST_ENABLED:
        return None
    if target_price is None or opens_at is None:
        return None
    if not _clean_quote(prices, config.FAST_REBOUND_TEST_MAX_SPREAD):
        return None

    elapsed = (captured_at - opens_at).total_seconds()
    if elapsed < 0 or elapsed > config.FAST_REBOUND_TEST_MAX_ENTRY_SECONDS_AFTER_OPEN:
        return None

    dominant = _dominant_side(prices)
    if dominant is None:
        return None
    minority = _minority_side(dominant)
    contract_id = contract_ids.get(minority)
    if contract_id is None:
        return None

    dom_ask = _price(prices, dominant, "ask_price")
    dom_bid = _price(prices, dominant, "bid_price")
    min_ask = _price(prices, minority, "ask_price")
    min_bid = _price(prices, minority, "bid_price")
    if dom_ask is None or dom_bid is None or min_ask is None or min_bid is None:
        return None
    dom_spread = _price(prices, dominant, "spread")
    min_spread = _price(prices, minority, "spread")
    if dom_spread is None:
        dom_spread = dom_ask - dom_bid
    if min_spread is None:
        min_spread = min_ask - min_bid

    if not (config.FAST_REBOUND_TEST_DOMINANT_MIN_ASK <= dom_ask < config.FAST_REBOUND_TEST_DOMINANT_MAX_ASK):
        return None

    baseline = market_rows[0] if market_rows else None
    if baseline is None:
        return None
    baseline_elapsed = (baseline.captured_at - opens_at).total_seconds()
    if baseline_elapsed > config.FAST_REBOUND_TEST_BASELINE_MAX_SECONDS_AFTER_OPEN:
        return None
    baseline_dom_ask = _price(baseline.prices, dominant, "ask_price")
    if baseline_dom_ask is None:
        return None

    speed = ((dom_ask - baseline_dom_ask) * 100.0) / max(1.0, elapsed)
    if speed < config.FAST_REBOUND_TEST_SPEED_MIN_CENTS_PER_SECOND:
        return None
    if speed >= config.FAST_REBOUND_TEST_SPEED_MAX_CENTS_PER_SECOND:
        return None

    prev30 = _last_at_or_before(market_rows, captured_at.timestamp() - 30.0)
    if prev30 is None:
        return None
    prev_dom_ask = _price(prev30.prices, dominant, "ask_price")
    if prev_dom_ask is None:
        return None
    reprice_30 = round((dom_ask - prev_dom_ask) * 100.0, 4)
    if not (
        reprice_30 > config.FAST_REBOUND_TEST_REPRICE_30S_MIN_CENTS
        and reprice_30 <= config.FAST_REBOUND_TEST_REPRICE_30S_MAX_CENTS
    ):
        return None

    return FastReboundSignal(
        market_db_id=market_db_id,
        market_ticker=market_ticker,
        contract_id=contract_id,
        dominant_side=dominant,
        minority_side=minority,
        signal_at=captured_at,
        signal_ts=captured_at.timestamp(),
        entry_bid=min_bid,
        entry_ask=min_ask,
        entry_spread=min_spread,
        dominant_bid=dom_bid,
        dominant_ask=dom_ask,
        dominant_spread=dom_spread,
        btc_price=btc_price,
        strike=target_price,
        btc_distance_dominant_side=round(_side_distance(dominant, btc_price, target_price), 2),
        time_since_open_seconds=round(elapsed, 3),
        time_remaining_seconds=tte,
        dominant_cents_per_second=round(speed, 6),
        dominant_change_prev_30s_cents=reprice_30,
    )


class FastReboundTestTracker:
    def __init__(self) -> None:
        self._enabled = config.FAST_REBOUND_TEST_ENABLED
        self._profile = config.FAST_REBOUND_TEST_PROFILE
        self._exit_models = parse_exit_models(config.FAST_REBOUND_TEST_EXIT_MODELS)
        self._current_market: Optional[str] = None
        self._rows: list[_QuoteRow] = []
        self._active: dict[int, _ActiveTest] = {}
        self._seen_markets: set[str] = set()
        if self._enabled:
            self._abandon_open_rows_from_prior_process()
            logger.info(
                "FastReboundTestTracker enabled | profile=%s models=%s timeout=%.0fs",
                self._profile,
                ",".join(f"+{int(t)}/-{int(s)}" for t, s in self._exit_models),
                config.FAST_REBOUND_TEST_TIMEOUT_SECONDS,
            )
        else:
            logger.info("FastReboundTestTracker disabled")

    def _abandon_open_rows_from_prior_process(self) -> None:
        execute_query(
            """
            UPDATE fast_rebound_test_trades
            SET status='ABANDONED',
                exit_reason='logger_restart',
                exit_at=UTC_TIMESTAMP(3)
            WHERE profile=%s
              AND status='ACTIVE'
            """,
            (self._profile,),
        )

    def on_tick(
        self,
        *,
        market_db_id: int,
        market_id: str,
        market_ticker: str,
        contract_ids: dict[str, int],
        target_price: Optional[float],
        opens_at: Optional[datetime],
        captured_at: datetime,
        btc_price: float,
        tte: Optional[int],
        prices: dict[str, dict],
    ) -> None:
        if not self._enabled or target_price is None or opens_at is None:
            return

        if market_id != self._current_market:
            self._finalize_rollover(captured_at)
            self._current_market = market_id
            self._rows = []
            self._active = {}

        row = _QuoteRow(
            captured_at=captured_at,
            ts=captured_at.timestamp(),
            btc_price=btc_price,
            tte=tte,
            prices=prices,
        )
        if self._active:
            self._advance_active(row, captured_at)

        self._rows.append(row)
        cutoff = row.ts - 360.0
        while self._rows and self._rows[0].ts < cutoff:
            self._rows.pop(0)

        if market_ticker in self._seen_markets:
            return
        sig = find_fast_rebound_signal(
            market_db_id=market_db_id,
            market_ticker=market_ticker,
            contract_ids=contract_ids,
            target_price=target_price,
            opens_at=opens_at,
            captured_at=captured_at,
            btc_price=btc_price,
            tte=tte,
            prices=prices,
            market_rows=self._rows,
        )
        if sig is not None:
            self._open_signal(sig)
            self._seen_markets.add(market_ticker)

    def _open_signal(self, sig: FastReboundSignal) -> None:
        for target_cents, stop_cents in self._exit_models:
            exit_model = f"tp{int(target_cents)}c_sl{int(stop_cents)}c"
            target_bid = round(sig.entry_ask + target_cents / 100.0, 4)
            stop_bid = round(sig.entry_ask - stop_cents / 100.0, 4)
            metadata = {
                "test_mode": True,
                "no_real_orders": True,
                "rule": "dominant 65-70c fast; prev30 dominant reprice >9c and <=15c; buy minority",
            }
            trade_id = insert_and_get_id(
                """
                INSERT IGNORE INTO fast_rebound_test_trades (
                    profile, exit_model,
                    market_id, contract_id, market_ticker,
                    dominant_side, minority_side,
                    signal_at, entry_at,
                    entry_bid, entry_ask, entry_spread,
                    btc_price_at_entry, strike, btc_distance_dominant_side,
                    time_since_open_seconds, time_remaining_seconds,
                    dominant_ask, dominant_bid, dominant_spread,
                    dominant_cents_per_second, dominant_change_prev_30s_cents,
                    target_cents, stop_cents, target_bid_price, stop_bid_price,
                    timeout_seconds,
                    estimated_entry_fee_cents, estimated_extra_slippage_cents,
                    metadata_json
                ) VALUES (
                    %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s,
                    %s, %s,
                    %s
                )
                """,
                (
                    self._profile, exit_model,
                    sig.market_db_id, sig.contract_id, sig.market_ticker,
                    sig.dominant_side, sig.minority_side,
                    sig.signal_at, sig.signal_at,
                    sig.entry_bid, sig.entry_ask, sig.entry_spread,
                    sig.btc_price, sig.strike, sig.btc_distance_dominant_side,
                    sig.time_since_open_seconds, sig.time_remaining_seconds,
                    sig.dominant_ask, sig.dominant_bid, sig.dominant_spread,
                    sig.dominant_cents_per_second, sig.dominant_change_prev_30s_cents,
                    target_cents, stop_cents, target_bid, stop_bid,
                    config.FAST_REBOUND_TEST_TIMEOUT_SECONDS,
                    _fee_cents(sig.entry_ask), config.FAST_REBOUND_TEST_EXTRA_SLIPPAGE_CENTS,
                    json.dumps(metadata),
                ),
            )
            if not trade_id:
                continue
            self._active[trade_id] = _ActiveTest(
                trade_id=trade_id,
                market_ticker=sig.market_ticker,
                contract_id=sig.contract_id,
                side=sig.minority_side,
                signal_ts=sig.signal_ts,
                entry_ask=sig.entry_ask,
                target_cents=target_cents,
                stop_cents=stop_cents,
                target_bid_price=target_bid,
                stop_bid_price=stop_bid,
                timeout_ts=sig.signal_ts + config.FAST_REBOUND_TEST_TIMEOUT_SECONDS,
                peak_bid=float("-inf"),
                trough_bid=float("inf"),
            )
        logger.info(
            "fast rebound TEST OPEN | %s dominant=%s minority=%s ask=%.3f dom30=%+.1fc models=%d",
            sig.market_ticker,
            sig.dominant_side,
            sig.minority_side,
            sig.entry_ask,
            sig.dominant_change_prev_30s_cents,
            len(self._exit_models),
        )

    def _advance_active(self, row: _QuoteRow, captured_at: datetime) -> None:
        for trade_id, active in list(self._active.items()):
            bid = _price(row.prices, active.side, "bid_price")
            if bid is None:
                continue
            active.peak_bid = max(active.peak_bid, bid)
            active.trough_bid = min(active.trough_bid, bid)

            reason = None
            if bid >= active.target_bid_price:
                reason = "target_hit"
            elif bid <= active.stop_bid_price:
                reason = "stop_hit"
            elif row.ts >= active.timeout_ts:
                reason = "timeout"

            if reason is not None:
                self._finalize(active, captured_at, bid, reason)
                del self._active[trade_id]

    def _finalize_rollover(self, captured_at: datetime) -> None:
        for trade_id, active in list(self._active.items()):
            self._finalize(active, captured_at, None, "market_rollover")
            del self._active[trade_id]

    def _finalize(
        self,
        active: _ActiveTest,
        exit_at: datetime,
        exit_bid: Optional[float],
        reason: str,
    ) -> None:
        gross = None
        entry_fee = _fee_cents(active.entry_ask)
        exit_fee = None
        total_fee = None
        net = None
        if exit_bid is not None:
            gross = round((exit_bid - active.entry_ask) * 100.0, 4)
            exit_fee = _fee_cents(exit_bid)
            total_fee = round(entry_fee + exit_fee, 6)
            net = round(gross - total_fee - config.FAST_REBOUND_TEST_EXTRA_SLIPPAGE_CENTS, 4)
        mfe = (
            round((active.peak_bid - active.entry_ask) * 100.0, 4)
            if active.peak_bid > float("-inf") else None
        )
        mae = (
            round((active.trough_bid - active.entry_ask) * 100.0, 4)
            if active.trough_bid < float("inf") else None
        )
        holding = round(exit_at.timestamp() - active.signal_ts, 3)
        execute_query(
            """
            UPDATE fast_rebound_test_trades
            SET status='COMPLETE',
                exit_at=%s,
                exit_bid=%s,
                exit_reason=%s,
                holding_seconds=%s,
                max_favorable_excursion_cents=%s,
                max_adverse_excursion_cents=%s,
                gross_pnl_cents=%s,
                estimated_exit_fee_cents=%s,
                estimated_total_fee_cents=%s,
                estimated_net_pnl_cents=%s
            WHERE id=%s
            """,
            (
                exit_at, exit_bid, reason, holding,
                mfe, mae, gross, exit_fee, total_fee, net,
                active.trade_id,
            ),
        )
        logger.info(
            "fast rebound TEST COMPLETE #%d | %s %s exit=%s bid=%s gross=%s net=%s hold=%.1fs",
            active.trade_id,
            active.market_ticker,
            active.side,
            reason,
            f"{exit_bid:.3f}" if exit_bid is not None else "none",
            gross,
            net,
            holding,
        )
