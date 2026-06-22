"""
test_momentum_live.py — Unit tests for the live-trading pure logic.

These tests cover the parts of app/momentum_live_trader.py that are pure
functions (no DB, no network): Kelly sizing, conservative position sizing,
projected-vs-actual drift math, rolling-window pause decisions, the kill switch,
and the static arming gate.  They do NOT place orders or touch MySQL.

Run:
    pytest tests/test_momentum_live.py -v
"""
from __future__ import annotations

import sys
import os
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app import config
from app.momentum_live_trader import (
    MomentumLiveTrader,
    _ActiveLive,
    compute_full_kelly_fraction,
    compute_position_size,
    compute_drift_fields,
    compute_actual_pnl,
    build_pause_windows,
    summarize_pnls,
    evaluate_pause,
    kill_switch_engaged,
    is_live_armed,
    _summarize_fills,
    aggregate_fills,
    order_remaining,
    is_order_open,
    net_position_for_side,
    reconcile_decision,
    combine_exit_fills,
    reconstruct_exit_state,
    should_replace_exit,
    partition_trade_orders,
    resolve_recovery_baseline,
)
from app.kalshi_trading import KalshiTradingClient, dollars_to_cents, cents_to_dollars


# ══════════════════════════════════════════════════════════════════════════════
# Kelly fraction
# ══════════════════════════════════════════════════════════════════════════════

class TestKelly:
    def test_positive_edge(self):
        # p=0.6, b=1.0 -> f* = 0.6 - 0.4/1.0 = 0.2
        assert compute_full_kelly_fraction(0.6, 1.0) == pytest.approx(0.2)

    def test_no_edge_returns_zero(self):
        # p=0.5, b=1.0 -> f* = 0.5 - 0.5 = 0.0
        assert compute_full_kelly_fraction(0.5, 1.0) == pytest.approx(0.0)

    def test_negative_edge_clamped_to_zero(self):
        assert compute_full_kelly_fraction(0.3, 1.0) == 0.0

    def test_missing_inputs_zero(self):
        assert compute_full_kelly_fraction(None, 1.0) == 0.0
        assert compute_full_kelly_fraction(0.6, None) == 0.0
        assert compute_full_kelly_fraction(0.6, 0.0) == 0.0

    def test_clamped_to_one(self):
        assert compute_full_kelly_fraction(1.0, 5.0) == 1.0


# ══════════════════════════════════════════════════════════════════════════════
# Position sizing — conservative, round DOWN
# ══════════════════════════════════════════════════════════════════════════════

class TestPositionSize:
    def test_basic_floor(self):
        # budget = 1000 * 0.2 * 1.0 = 200; min(200, 50)=50; 50/0.42 = 119.04 -> 119
        contracts, dollars = compute_position_size(
            bankroll_dollars=1000, kelly_fraction=1.0, full_kelly_fraction=0.2,
            max_dollars_per_trade=50, max_contracts_per_trade=0,
            price_per_contract=0.42,
        )
        assert contracts == 119
        assert dollars == pytest.approx(round(119 * 0.42, 4))

    def test_dollar_cap_binds(self):
        contracts, _ = compute_position_size(
            bankroll_dollars=1_000_000, kelly_fraction=1.0, full_kelly_fraction=1.0,
            max_dollars_per_trade=10, max_contracts_per_trade=0,
            price_per_contract=0.50,
        )
        assert contracts == 20   # 10 / 0.50

    def test_contract_cap_binds(self):
        contracts, _ = compute_position_size(
            bankroll_dollars=1_000_000, kelly_fraction=1.0, full_kelly_fraction=1.0,
            max_dollars_per_trade=1000, max_contracts_per_trade=5,
            price_per_contract=0.10,
        )
        assert contracts == 5

    def test_rounds_down_never_up(self):
        # budget 0.99 / price 0.50 = 1.98 -> floor 1
        contracts, _ = compute_position_size(
            bankroll_dollars=100, kelly_fraction=1.0, full_kelly_fraction=0.0099,
            max_dollars_per_trade=100, max_contracts_per_trade=0,
            price_per_contract=0.50,
        )
        assert contracts == 1

    def test_too_small_budget_is_zero(self):
        contracts, dollars = compute_position_size(
            bankroll_dollars=100, kelly_fraction=1.0, full_kelly_fraction=0.001,
            max_dollars_per_trade=100, max_contracts_per_trade=0,
            price_per_contract=0.50,
        )
        assert contracts == 0 and dollars == 0.0

    def test_zero_inputs_block(self):
        assert compute_position_size(
            bankroll_dollars=0, kelly_fraction=1.0, full_kelly_fraction=0.2,
            max_dollars_per_trade=50, max_contracts_per_trade=0,
            price_per_contract=0.42)[0] == 0
        assert compute_position_size(
            bankroll_dollars=1000, kelly_fraction=0, full_kelly_fraction=0.2,
            max_dollars_per_trade=50, max_contracts_per_trade=0,
            price_per_contract=0.42)[0] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Drift fields
# ══════════════════════════════════════════════════════════════════════════════

class TestDrift:
    def test_drift_math(self):
        d = compute_drift_fields(
            projected_entry_ask=0.40, projected_exit_bid=0.46,
            projected_profit=0.05, projected_expectancy=0.03,
            actual_entry_price=0.41, actual_exit_price=0.45,
            actual_profit=0.03,
        )
        assert d["entry_price_drift_cents"] == pytest.approx(0.01)   # paid 1c more
        assert d["exit_price_drift_cents"] == pytest.approx(-0.01)   # got 1c less
        assert d["profit_delta_cents"] == pytest.approx(-0.02)
        assert d["total_execution_drift_cents"] == pytest.approx(0.02)  # adverse
        assert d["profit_capture_ratio"] == pytest.approx(0.6)
        assert d["expectancy_delta_cents"] == pytest.approx(0.0)
        assert d["expectancy_capture_ratio"] == pytest.approx(1.0)

    def test_none_safe(self):
        d = compute_drift_fields(
            projected_entry_ask=0.40, projected_exit_bid=None,
            projected_profit=None, projected_expectancy=None,
            actual_entry_price=None, actual_exit_price=None, actual_profit=None,
        )
        assert d["profit_delta_cents"] is None
        assert d["profit_capture_ratio"] is None

    def test_capture_ratio_zero_projected_is_none(self):
        d = compute_drift_fields(
            projected_entry_ask=0.40, projected_exit_bid=0.40,
            projected_profit=0.0, projected_expectancy=0.0,
            actual_entry_price=0.40, actual_exit_price=0.42,
            actual_profit=0.02,
        )
        assert d["profit_capture_ratio"] is None
        assert d["expectancy_capture_ratio"] is None


# ══════════════════════════════════════════════════════════════════════════════
# summarize_pnls
# ══════════════════════════════════════════════════════════════════════════════

class TestSummarize:
    def test_basic(self):
        s = summarize_pnls([0.05, 0.05, -0.03])
        assert s["n"] == 3
        assert s["win_rate"] == pytest.approx(2 / 3)
        assert s["expectancy"] == pytest.approx((0.05 + 0.05 - 0.03) / 3)
        assert s["profit_factor"] == pytest.approx(0.10 / 0.03)
        assert s["profit_loss_ratio"] == pytest.approx(0.05 / 0.03)

    def test_empty(self):
        s = summarize_pnls([])
        assert s["n"] == 0 and s["win_rate"] is None

    def test_no_losses_profit_factor_none(self):
        s = summarize_pnls([0.05, 0.02])
        assert s["profit_factor"] is None


# ══════════════════════════════════════════════════════════════════════════════
# Pause logic
# ══════════════════════════════════════════════════════════════════════════════

class TestPause:
    def _windows(self, n, proj, act):
        return {
            25: {"n": n, "projected": proj, "actual": act},
            50: {"n": 0, "projected": summarize_pnls([]), "actual": summarize_pnls([])},
            100: {"n": 0, "projected": summarize_pnls([]), "actual": summarize_pnls([])},
        }

    def test_no_breach(self):
        proj = {"win_rate": 0.6, "expectancy": 0.03, "profit_factor": 1.5}
        act = {"win_rate": 0.58, "expectancy": 0.028, "profit_factor": 1.4}
        res = evaluate_pause(
            self._windows(30, proj, act),
            min_trades=25, win_rate_gap_pct=15,
            expectancy_gap=0.02, profit_factor_gap=0.5,
        )
        assert res is None

    def test_win_rate_breach(self):
        proj = {"win_rate": 0.60, "expectancy": 0.03, "profit_factor": 1.5}
        act = {"win_rate": 0.40, "expectancy": 0.03, "profit_factor": 1.5}  # 20pp gap
        res = evaluate_pause(
            self._windows(30, proj, act),
            min_trades=25, win_rate_gap_pct=15,
            expectancy_gap=0.02, profit_factor_gap=0.5,
        )
        assert res is not None
        assert res["window"] == 25
        assert any("win_rate" in b for b in res["breaches"])

    def test_small_sample_never_pauses(self):
        proj = {"win_rate": 0.60, "expectancy": 0.03, "profit_factor": 1.5}
        act = {"win_rate": 0.10, "expectancy": -0.05, "profit_factor": 0.2}
        res = evaluate_pause(
            self._windows(10, proj, act),   # n=10 < min_trades=25
            min_trades=25, win_rate_gap_pct=15,
            expectancy_gap=0.02, profit_factor_gap=0.5,
        )
        assert res is None

    def test_expectancy_breach(self):
        proj = {"win_rate": 0.6, "expectancy": 0.05, "profit_factor": 1.5}
        act = {"win_rate": 0.6, "expectancy": 0.01, "profit_factor": 1.5}  # 0.04 gap
        res = evaluate_pause(
            self._windows(40, proj, act),
            min_trades=25, win_rate_gap_pct=15,
            expectancy_gap=0.02, profit_factor_gap=0.5,
        )
        assert res is not None
        assert any("expectancy" in b for b in res["breaches"])


# ══════════════════════════════════════════════════════════════════════════════
# Kill switch + arming gate (manipulate config module attributes)
# ══════════════════════════════════════════════════════════════════════════════

class TestKillSwitch:
    def test_env_flag(self, monkeypatch):
        monkeypatch.setattr(config, "MOMENTUM_LIVE_KILL_SWITCH", True)
        monkeypatch.setattr(config, "MOMENTUM_LIVE_KILL_SWITCH_FILE", "")
        assert kill_switch_engaged() is True

    def test_file_presence(self, monkeypatch, tmp_path):
        f = tmp_path / "STOP"
        f.write_text("halt")
        monkeypatch.setattr(config, "MOMENTUM_LIVE_KILL_SWITCH", False)
        monkeypatch.setattr(config, "MOMENTUM_LIVE_KILL_SWITCH_FILE", str(f))
        assert kill_switch_engaged() is True

    def test_disengaged(self, monkeypatch):
        monkeypatch.setattr(config, "MOMENTUM_LIVE_KILL_SWITCH", False)
        monkeypatch.setattr(config, "MOMENTUM_LIVE_KILL_SWITCH_FILE", "")
        assert kill_switch_engaged() is False


class TestArmingGate:
    def test_default_not_armed(self, monkeypatch):
        # With defaults, live is not enabled -> not armed.
        monkeypatch.setattr(config, "MOMENTUM_LIVE_ENABLED", False)
        armed, reason = is_live_armed()
        assert armed is False
        assert "ENABLED" in reason

    def test_missing_confirm_blocks(self, monkeypatch):
        monkeypatch.setattr(config, "MOMENTUM_LIVE_ENABLED", True)
        monkeypatch.setattr(config, "MOMENTUM_LIVE_CONFIRM", "wrong")
        armed, reason = is_live_armed()
        assert armed is False
        assert "CONFIRM" in reason


# ══════════════════════════════════════════════════════════════════════════════
# Fill summarization + price conversion
# ══════════════════════════════════════════════════════════════════════════════

class TestFills:
    def test_weighted_avg(self):
        fills = [
            {"count": 2, "yes_price": 40},
            {"count": 3, "yes_price": 45},
        ]
        count, avg, fees = _summarize_fills(fills, "YES")
        assert count == 5
        assert avg == pytest.approx((2 * 0.40 + 3 * 0.45) / 5)
        assert fees is None   # no fee field present

    def test_no_side_price(self):
        fills = [{"count": 1, "no_price": 55}]
        count, avg, _ = _summarize_fills(fills, "NO")
        assert count == 1 and avg == pytest.approx(0.55)

    def test_empty(self):
        assert _summarize_fills([], "YES") == (0, None, None)

    def test_fees_summed_when_present(self):
        fills = [{"count": 1, "yes_price": 50, "fee": 0.01}]
        _, _, fees = _summarize_fills(fills, "YES")
        assert fees == pytest.approx(0.01)


class TestPriceConversion:
    def test_dollars_to_cents(self):
        assert dollars_to_cents(0.42) == 42
        assert dollars_to_cents(0.005) == 1   # clamped up to 1
        assert dollars_to_cents(1.5) == 99     # clamped down to 99

    def test_cents_to_dollars(self):
        assert cents_to_dollars(42) == pytest.approx(0.42)
        assert cents_to_dollars(None) is None


class TestKalshiOrderV2Mapping:
    def test_place_yes_buy_maps_to_bid_at_same_price(self, monkeypatch):
        seen = {}

        def fake_request(self, method, path, *, body=None, params=None):
            seen.update({"method": method, "path": path, "body": body, "params": params})
            return {"order_id": "o1", "client_order_id": body["client_order_id"]}

        monkeypatch.setattr(KalshiTradingClient, "_request", fake_request)

        client = KalshiTradingClient(require_auth=False)
        client.place_order(
            ticker="KXBTC15M-TEST",
            side="YES",
            action="buy",
            count=10,
            limit_price=0.42,
            client_order_id="coid-yes-buy",
        )

        assert seen["method"] == "POST"
        assert seen["path"] == "/portfolio/events/orders"
        assert seen["body"]["side"] == "bid"
        assert seen["body"]["price"] == "0.4200"
        assert seen["body"]["count"] == "10.00"

    def test_place_no_buy_maps_to_yes_ask_at_one_minus_price(self, monkeypatch):
        seen = {}

        def fake_request(self, method, path, *, body=None, params=None):
            seen.update({"method": method, "path": path, "body": body, "params": params})
            return {"order_id": "o2", "client_order_id": body["client_order_id"]}

        monkeypatch.setattr(KalshiTradingClient, "_request", fake_request)

        client = KalshiTradingClient(require_auth=False)
        client.place_order(
            ticker="KXBTC15M-TEST",
            side="NO",
            action="buy",
            count=7,
            limit_price=0.37,
            client_order_id="coid-no-buy",
        )

        assert seen["path"] == "/portfolio/events/orders"
        assert seen["body"]["side"] == "ask"
        assert seen["body"]["price"] == "0.6300"
        assert seen["body"]["count"] == "7.00"

    def test_cancel_uses_v2_event_orders_path(self, monkeypatch):
        seen = {}

        def fake_request(self, method, path, *, body=None, params=None):
            seen.update({"method": method, "path": path})
            return {"order_id": "o3"}

        monkeypatch.setattr(KalshiTradingClient, "_request", fake_request)

        client = KalshiTradingClient(require_auth=False)
        client.cancel_order("o3")

        assert seen == {"method": "DELETE", "path": "/portfolio/events/orders/o3"}


# ══════════════════════════════════════════════════════════════════════════════
# Partial-fill safety helpers (Priority 1)
# ══════════════════════════════════════════════════════════════════════════════

class TestOrderRemaining:
    def test_basic(self):
        assert order_remaining(10, 4) == 6

    def test_never_negative(self):
        # Cumulative fills can momentarily appear >= requested; never go negative.
        assert order_remaining(10, 12) == 0

    def test_zero_when_complete(self):
        assert order_remaining(5, 5) == 0


class TestIsOrderOpen:
    def test_terminal_status(self):
        assert is_order_open({"status": "executed"}) is False
        assert is_order_open({"status": "canceled"}) is False
        assert is_order_open({"status": "expired"}) is False

    def test_remaining_count_zero_is_terminal(self):
        assert is_order_open({"status": "resting", "remaining_count": 0}) is False

    def test_resting_with_remaining_is_open(self):
        assert is_order_open({"status": "resting", "remaining_count": 3}) is True

    def test_partial_still_open(self):
        assert is_order_open({"status": "partially_filled", "remaining_count": 2}) is True

    def test_unknown_treated_as_open(self):
        # Conservative: never assume an order is dead without evidence.
        assert is_order_open({}) is True
        assert is_order_open(None) is True


class TestAggregateFills:
    def test_aggregates_across_orders(self):
        # Two exit orders: 3 @ 0.50 then 2 @ 0.45 -> cumulative 5, vwap weighted.
        order1 = [{"count": 3, "yes_price": 50}]
        order2 = [{"count": 2, "yes_price": 45}]
        count, avg, fees = aggregate_fills([order1, order2], "YES")
        assert count == 5
        assert avg == pytest.approx((3 * 0.50 + 2 * 0.45) / 5)
        assert fees is None

    def test_partial_exit_then_remainder_covers_position(self):
        # filled 5; first exit fills 2 (remaining 3), second exit fills 3 -> flat.
        filled = 5
        first = [{"count": 2, "no_price": 40}]
        count1, _, _ = aggregate_fills([first], "NO")
        assert order_remaining(filled, count1) == 3       # only resubmit 3
        second = [{"count": 3, "no_price": 38}]
        count2, _, _ = aggregate_fills([first, second], "NO")
        assert count2 == 5
        assert order_remaining(filled, count2) == 0       # now flat -> finalize

    def test_fees_summed_across_orders(self):
        o1 = [{"count": 1, "yes_price": 50, "fee": 0.01}]
        o2 = [{"count": 1, "yes_price": 50, "fee": 0.01}]
        _, _, fees = aggregate_fills([o1, o2], "YES")
        assert fees == pytest.approx(0.02)

    def test_empty(self):
        assert aggregate_fills([[], []], "YES") == (0, None, None)


# ══════════════════════════════════════════════════════════════════════════════
# Restart reconciliation helpers (Priority 2)
# ══════════════════════════════════════════════════════════════════════════════

class TestNetPosition:
    def test_yes_long(self):
        pos = [{"ticker": "KXBTC-T1", "position": 7}]
        assert net_position_for_side(pos, "KXBTC-T1", "YES") == 7
        assert net_position_for_side(pos, "KXBTC-T1", "NO") == 0

    def test_no_long(self):
        pos = [{"ticker": "KXBTC-T1", "position": -4}]
        assert net_position_for_side(pos, "KXBTC-T1", "NO") == 4
        assert net_position_for_side(pos, "KXBTC-T1", "YES") == 0

    def test_ticker_mismatch(self):
        pos = [{"ticker": "OTHER", "position": 5}]
        assert net_position_for_side(pos, "KXBTC-T1", "YES") == 0

    def test_empty(self):
        assert net_position_for_side([], "KXBTC-T1", "YES") == 0


class TestReconcileDecision:
    def test_position_triggers_flatten(self):
        assert reconcile_decision(5, True) == "rehydrate_flatten"
        assert reconcile_decision(5, False) == "rehydrate_flatten"

    def test_no_position_open_order_cancels(self):
        assert reconcile_decision(0, True) == "cancel_then_close"

    def test_no_position_no_order_reconciled(self):
        assert reconcile_decision(0, False) == "mark_reconciled"


# ══════════════════════════════════════════════════════════════════════════════
# Gap 1 — restart reconstruction after partial exit
# ══════════════════════════════════════════════════════════════════════════════

class TestReconstructExitState:
    def test_partial_exit_preserved(self):
        # Bought 10, sold 4 before crash, 6 remain. Original must NOT become 6.
        s = reconstruct_exit_state(db_entry_filled=10, db_exit_filled=4, position_qty=6)
        assert s["original_entry"] == 10     # original entry preserved
        assert s["exit_filled"] == 4         # prior exits preserved
        assert s["remaining"] == 6           # remaining reconstructed

    def test_exit_unpersisted_recovered_from_position(self):
        # Exit filled before crash but exit_filled not yet persisted (=0):
        # derive it from (original - position).
        s = reconstruct_exit_state(db_entry_filled=10, db_exit_filled=0, position_qty=6)
        assert s["original_entry"] == 10
        assert s["exit_filled"] == 4
        assert s["remaining"] == 6

    def test_entry_underrecorded_repaired_upward(self):
        # Crash right after fill before DB entry update: db_entry=0 but position=5.
        s = reconstruct_exit_state(db_entry_filled=0, db_exit_filled=0, position_qty=5)
        assert s["original_entry"] == 5
        assert s["exit_filled"] == 0
        assert s["remaining"] == 5

    def test_remaining_never_turns_into_original(self):
        # The core bug guard: with a prior partial exit, original != remaining.
        s = reconstruct_exit_state(db_entry_filled=8, db_exit_filled=3, position_qty=5)
        assert s["original_entry"] == 8
        assert s["original_entry"] != s["remaining"]

    def test_idempotent_across_repeated_restarts(self):
        # First restart derives exit_filled=4 and persists it; a second restart
        # with that persisted value and the same position yields the same state.
        first = reconstruct_exit_state(db_entry_filled=10, db_exit_filled=0, position_qty=6)
        second = reconstruct_exit_state(
            db_entry_filled=first["original_entry"],
            db_exit_filled=first["exit_filled"],
            position_qty=6,
        )
        assert first == second
        third = reconstruct_exit_state(
            db_entry_filled=second["original_entry"],
            db_exit_filled=second["exit_filled"],
            position_qty=6,
        )
        assert third == second


class TestCombineExitFills:
    def test_baseline_plus_session_full_round_trip(self):
        # Pre-restart baseline: 4 sold @ 0.46 (value 1.84). Post-restart
        # session: 6 sold @ 0.44. Cumulative must reflect ALL 10.
        combined = combine_exit_fills(
            baseline_count=4, baseline_value=4 * 0.46, baseline_fees=None,
            session_count=6, session_avg=0.44, session_fees=None,
        )
        assert combined["count"] == 10
        assert combined["value"] == pytest.approx(4 * 0.46 + 6 * 0.44)
        assert combined["avg"] == pytest.approx((4 * 0.46 + 6 * 0.44) / 10)

    def test_fresh_trade_equals_session(self):
        combined = combine_exit_fills(0, 0.0, None, 5, 0.50, 0.01)
        assert combined["count"] == 5
        assert combined["avg"] == pytest.approx(0.50)
        assert combined["fees"] == pytest.approx(0.01)

    def test_fees_summed_over_known_parts(self):
        combined = combine_exit_fills(4, 1.84, 0.02, 6, 0.44, 0.03)
        assert combined["fees"] == pytest.approx(0.05)

    def test_restart_pnl_reflects_full_round_trip(self):
        # End-to-end via the same helpers the trader uses: original 10 contracts,
        # entry 0.40 (entry fees 0.10 total), exits 4@0.46 (pre) + 6@0.44 (post).
        combined = combine_exit_fills(
            baseline_count=4, baseline_value=4 * 0.46, baseline_fees=0.04,
            session_count=6, session_avg=0.44, session_fees=0.06,
        )
        pnl = compute_actual_pnl(
            actual_entry_price=0.40,
            actual_exit_price=combined["avg"],          # blended over all 10
            entry_fees_total=0.10,
            exit_fees_total=combined["fees"],           # 0.04 + 0.06
            filled_contracts=10,                        # ORIGINAL size, not 6
        )
        # gross/contract = blended_exit - 0.40; fees/contract = (0.10+0.10)/10 = 0.02
        expected = round(combined["avg"] - 0.40 - 0.02, 6)
        assert pnl["actual_profit_cents"] == pytest.approx(expected)
        assert pnl["actual_profit_dollars"] == pytest.approx(expected * 10)


# ══════════════════════════════════════════════════════════════════════════════
# Gap 2 — no oversell during cancel/reprice (replacement gate)
# ══════════════════════════════════════════════════════════════════════════════

class TestShouldReplaceExit:
    def test_open_order_must_not_replace(self):
        # Order still working -> fills can still land -> do NOT replace.
        assert should_replace_exit({"status": "resting", "remaining_count": 2}, 2) is False

    def test_unknown_state_must_not_replace(self):
        # Ambiguous state is treated as open -> fail safe, no replace.
        assert should_replace_exit(None, 3) is False
        assert should_replace_exit({}, 3) is False

    def test_terminal_with_remaining_replaces(self):
        assert should_replace_exit({"status": "canceled", "remaining_count": 0}, 3) is True

    def test_terminal_but_flat_does_not_replace(self):
        # Nothing left to sell -> no replacement (would otherwise oversell).
        assert should_replace_exit({"status": "executed", "remaining_count": 0}, 0) is False

    def test_replacement_qty_bounded_by_remaining(self):
        # The gate pairs with order_remaining: even when replacing, qty is the
        # remaining only — never the original size.
        filled, cumulative_exit = 10, 7
        remaining = order_remaining(filled, cumulative_exit)
        assert remaining == 3
        assert should_replace_exit({"status": "canceled"}, remaining) is True
        # If a late fill pushed cumulative to full, remaining is 0 -> no replace.
        assert should_replace_exit({"status": "canceled"}, order_remaining(10, 10)) is False


class TestPartitionTradeOrders:
    def test_owned_and_unknown_orders_split_by_durable_keys(self):
        orders = [
            {"order_id": "srv-1", "client_order_id": "cli-a"},
            {"order_id": "srv-2", "client_order_id": "cli-b"},
            {"order_id": "srv-3", "client_order_id": "cli-c"},
        ]
        owned, unknown = partition_trade_orders(
            orders,
            known_order_ids={"srv-2"},
            known_client_order_ids={"cli-a"},
        )
        assert [o["order_id"] for o in owned] == ["srv-1", "srv-2"]
        assert [o["order_id"] for o in unknown] == ["srv-3"]


class TestResolveRecoveryBaseline:
    def test_prefers_persisted_when_count_and_value_match(self):
        baseline = resolve_recovery_baseline(
            reconstructed_exit_count=4,
            persisted_count=4,
            persisted_value=1.84,
            persisted_fees=0.04,
            fills_count=4,
            fills_avg=0.45,
            fills_fees=0.05,
        )
        assert baseline["verified"] is True
        assert baseline["source"] == "persisted"
        assert baseline["count"] == 4
        assert baseline["value"] == pytest.approx(1.84)
        assert baseline["fees"] == pytest.approx(0.04)

    def test_falls_back_to_fill_history_when_persisted_progress_missing(self):
        baseline = resolve_recovery_baseline(
            reconstructed_exit_count=4,
            persisted_count=0,
            persisted_value=None,
            persisted_fees=None,
            fills_count=4,
            fills_avg=0.46,
            fills_fees=0.04,
        )
        assert baseline["verified"] is True
        assert baseline["source"] == "fills"
        assert baseline["value"] == pytest.approx(4 * 0.46)

    def test_mismatch_keeps_count_but_marks_accounting_unverified(self):
        baseline = resolve_recovery_baseline(
            reconstructed_exit_count=4,
            persisted_count=1,
            persisted_value=0.44,
            persisted_fees=0.01,
            fills_count=3,
            fills_avg=0.47,
            fills_fees=0.03,
        )
        assert baseline["verified"] is False
        assert baseline["source"] == "mismatch"
        assert baseline["count"] == 4
        assert baseline["value"] == pytest.approx(0.0)
        assert baseline["fees"] is None


class TestExitSubmitSequencing:
    def test_exit_client_order_id_persisted_before_post(self, monkeypatch):
        calls = []

        class FakeClient:
            def __init__(self):
                self.seen = []

            def place_order(self, **kwargs):
                self.seen.append(kwargs["client_order_id"])
                update_calls = [c for c in calls if c[0] == "update"]
                assert update_calls, "trade row should be updated before POST"
                assert update_calls[-1][1][0] == kwargs["client_order_id"]
                return {"order_id": "exit-1", "client_order_id": kwargs["client_order_id"]}

        def fake_execute_query(sql, params):
            if "UPDATE momentum_live_trades SET status='PENDING_EXIT'" in sql:
                calls.append(("update", params))

        trader = object.__new__(MomentumLiveTrader)
        trader._client = FakeClient()
        trader._active = {}
        trader._reconcile_ambiguous_order = lambda *args, **kwargs: None
        trader._record_order_event = lambda live, event_type, **kwargs: calls.append(
            ("event", event_type, kwargs)
        )

        monkeypatch.setattr("app.momentum_live_trader.execute_query", fake_execute_query)

        live = _ActiveLive(
            live_trade_id=7,
            market_id=1,
            contract_id=2,
            market_ticker="TICK",
            side="YES",
            signal_at=datetime.now(timezone.utc),
            signal_ts=0.0,
            entry_ask=0.40,
            horizon_ts=0.0,
            requested_contracts=5,
            status="PENDING_EXIT",
            filled_contracts=5,
        )

        trader._submit_exit(live, 0.45, live.signal_at, "fixed_time", 3)

        assert live.exit_order_id == "exit-1"
        assert any(c[0] == "event" and c[1] == "exit_submit_intent" for c in calls)
        assert any(c[0] == "event" and c[1] == "exit_submitted" for c in calls)


# ══════════════════════════════════════════════════════════════════════════════
# Actual PnL — BOTH entry and exit fees must be charged (Issue 1)
# ══════════════════════════════════════════════════════════════════════════════

class TestActualPnL:
    def test_both_fees_charged_single_contract(self):
        # entry 0.40, exit 0.46 -> gross +0.06; fees 0.01 (entry) + 0.01 (exit)
        r = compute_actual_pnl(
            actual_entry_price=0.40, actual_exit_price=0.46,
            entry_fees_total=0.01, exit_fees_total=0.01, filled_contracts=1,
        )
        assert r["actual_profit_cents"] == pytest.approx(0.04)   # 0.06 - 0.02
        assert r["fees_per_contract"] == pytest.approx(0.02)
        assert r["actual_profit_dollars"] == pytest.approx(0.04)
        assert r["actual_trade_won"] == 1

    def test_entry_fee_not_ignored_vs_exit_only(self):
        # Regression for the bug: ignoring entry fee overstates profit.
        both = compute_actual_pnl(
            actual_entry_price=0.40, actual_exit_price=0.46,
            entry_fees_total=0.02, exit_fees_total=0.02, filled_contracts=1,
        )
        exit_only = compute_actual_pnl(
            actual_entry_price=0.40, actual_exit_price=0.46,
            entry_fees_total=None, exit_fees_total=0.02, filled_contracts=1,
        )
        assert both["actual_profit_cents"] < exit_only["actual_profit_cents"]
        assert both["actual_profit_cents"] == pytest.approx(0.02)  # 0.06 - 0.04

    def test_fees_normalized_per_contract(self):
        # Totals from fills are across all contracts -> divide by count.
        r = compute_actual_pnl(
            actual_entry_price=0.40, actual_exit_price=0.50,
            entry_fees_total=0.10, exit_fees_total=0.10, filled_contracts=10,
        )
        # per-contract fee = (0.10+0.10)/10 = 0.02 ; profit = 0.10 - 0.02 = 0.08
        assert r["fees_per_contract"] == pytest.approx(0.02)
        assert r["actual_profit_cents"] == pytest.approx(0.08)
        assert r["actual_profit_dollars"] == pytest.approx(0.80)

    def test_unknown_fees_treated_as_zero_but_reported_none(self):
        r = compute_actual_pnl(
            actual_entry_price=0.40, actual_exit_price=0.46,
            entry_fees_total=None, exit_fees_total=None, filled_contracts=1,
        )
        assert r["fees_per_contract"] is None
        assert r["actual_profit_cents"] == pytest.approx(0.06)

    def test_unfilled_leg_is_none(self):
        r = compute_actual_pnl(
            actual_entry_price=None, actual_exit_price=0.46,
            entry_fees_total=0.01, exit_fees_total=0.01, filled_contracts=1,
        )
        assert r["actual_profit_cents"] is None
        assert r["actual_trade_won"] is None

    def test_drift_reflects_corrected_actual_profit(self):
        # Projected (shadow) profit 0.05; actual with both fees should yield a
        # smaller actual profit, so capture < 1 and total_execution_drift > 0.
        actual = compute_actual_pnl(
            actual_entry_price=0.41, actual_exit_price=0.45,
            entry_fees_total=0.01, exit_fees_total=0.01, filled_contracts=1,
        )
        # actual: 0.04 gross - 0.02 fees = 0.02
        assert actual["actual_profit_cents"] == pytest.approx(0.02)
        d = compute_drift_fields(
            projected_entry_ask=0.40, projected_exit_bid=0.46,
            projected_profit=0.05, projected_expectancy=0.03,
            actual_entry_price=0.41, actual_exit_price=0.45,
            actual_profit=actual["actual_profit_cents"],
        )
        assert d["profit_delta_cents"] == pytest.approx(-0.03)        # 0.02 - 0.05
        assert d["total_execution_drift_cents"] == pytest.approx(0.03)
        assert d["profit_capture_ratio"] == pytest.approx(0.02 / 0.05)


# ══════════════════════════════════════════════════════════════════════════════
# Paired rolling windows for pause (Issue 2)
# ══════════════════════════════════════════════════════════════════════════════

class TestPairedPauseWindows:
    def test_unpaired_rows_excluded(self):
        # 3 fully-paired + 2 with a NULL leg must not unbalance the samples.
        rows = [
            (0.05, 0.04),
            (0.03, None),     # live unexecutable -> excluded
            (None, 0.02),     # shadow-only       -> excluded
            (0.06, 0.05),
            (-0.02, -0.03),
        ]
        windows = build_pause_windows(rows, (25, 50, 100))
        w = windows[25]
        assert w["n"] == 3                      # only the 3 paired rows
        assert w["projected"]["n"] == 3
        assert w["actual"]["n"] == 3            # equal length == apples-to-apples

    def test_window_slices_most_recent(self):
        rows = [(0.01 * i, 0.01 * i) for i in range(1, 11)]   # 10 paired, newest first
        windows = build_pause_windows(rows, (3, 5, 100))
        assert windows[3]["n"] == 3
        assert windows[5]["n"] == 5
        assert windows[100]["n"] == 10

    def test_paired_then_pause_decision(self):
        # Build paired windows where actual badly trails projected -> pause.
        rows = [(0.05, -0.05) for _ in range(30)]   # 30 paired, big gap
        windows = build_pause_windows(rows, (25, 50, 100))
        res = evaluate_pause(
            windows, min_trades=25, win_rate_gap_pct=15,
            expectancy_gap=0.02, profit_factor_gap=0.5,
        )
        assert res is not None
        assert res["window"] == 25
        assert res["n"] == 25

    def test_small_paired_sample_does_not_pause(self):
        # Only 10 paired rows (rest unpaired) -> below min_trades -> no pause.
        rows = [(0.05, -0.05) for _ in range(10)] + [(0.05, None) for _ in range(40)]
        windows = build_pause_windows(rows, (25, 50, 100))
        assert windows[25]["n"] == 10
        res = evaluate_pause(
            windows, min_trades=25, win_rate_gap_pct=15,
            expectancy_gap=0.02, profit_factor_gap=0.5,
        )
        assert res is None
