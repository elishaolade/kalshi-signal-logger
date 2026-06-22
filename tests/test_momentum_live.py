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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app import config
from app.momentum_live_trader import (
    compute_full_kelly_fraction,
    compute_position_size,
    compute_drift_fields,
    summarize_pnls,
    evaluate_pause,
    kill_switch_engaged,
    is_live_armed,
    _summarize_fills,
)
from app.kalshi_trading import dollars_to_cents, cents_to_dollars


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
