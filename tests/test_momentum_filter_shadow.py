"""
tests/test_momentum_filter_shadow.py — unit tests for the filter-diagnostics
layer (app/momentum_filter_shadow.py) and the shadow-only no-order guarantee in
app/momentum_live_trader.py.

All tests are DB-free and network-free.  They exercise the PURE candidate logic
and the order-suppression choke point directly (no synthetic market data is
passed off as real signal behaviour — the trades here are explicit fixtures for
arithmetic checks only).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import config
from app import momentum_filter_shadow as fs
from app import momentum_live_trader as mlt
from app.momentum_live_trader import MomentumLiveTrader


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures — explicit trade dictionaries (already normalized: TRUE cents)
# ══════════════════════════════════════════════════════════════════════════════

def _trade(outcome, net, **kw):
    base = {"outcome": outcome, "net_cents": net, "side": kw.pop("side", "YES")}
    base.update(kw)
    return base


# A small deterministic population:
#   SL1: stop_loss, wide spread 3c, gap 1.5c, NO ask 0.6, never green, red early
#   SL2: stop_loss, tight spread 0.5c, gap 0.1c, went green late
#   PT1: profit_target, tight spread 1c, gap 0.2c, green fast
#   FT1: fixed_time, mid spread 2c
def _population():
    return [
        _trade("stop_loss", -3.0, side="NO", ws_spread_cents=3.0, entry_ask=0.60,
               entry_ask_gap_cents=1.5, ws_quote_age_ms=300.0, tte_seconds=500.0,
               time_to_first_green_seconds=None,
               pnl_at_15s_cents=-1.5, pnl_at_20s_cents=-2.0, pnl_at_30s_cents=-2.5),
        _trade("stop_loss", -3.0, side="YES", ws_spread_cents=0.5, entry_ask=0.30,
               entry_ask_gap_cents=0.1, ws_quote_age_ms=100.0, tte_seconds=800.0,
               time_to_first_green_seconds=25.0,
               pnl_at_15s_cents=-1.0, pnl_at_20s_cents=-0.5, pnl_at_30s_cents=0.5),
        _trade("profit_target", 3.0, side="YES", ws_spread_cents=1.0, entry_ask=0.20,
               entry_ask_gap_cents=0.2, ws_quote_age_ms=120.0, tte_seconds=700.0,
               time_to_first_green_seconds=3.0,
               pnl_at_15s_cents=1.0, pnl_at_20s_cents=2.0, pnl_at_30s_cents=3.0),
        _trade("fixed_time", -1.0, side="NO", ws_spread_cents=2.0, entry_ask=0.45,
               entry_ask_gap_cents=0.3, ws_quote_age_ms=900.0, tte_seconds=460.0,
               time_to_first_green_seconds=8.0,
               pnl_at_15s_cents=0.2, pnl_at_20s_cents=-0.5, pnl_at_30s_cents=-0.5),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Filter threshold calculations
# ══════════════════════════════════════════════════════════════════════════════

class TestEntryAskGapCents:
    def test_positive_gap(self):
        # ws ask 0.08 vs ideal 0.074 -> 0.6c worse
        assert fs.entry_ask_gap_cents(0.08, 0.074) == pytest.approx(0.6)

    def test_negative_gap(self):
        assert fs.entry_ask_gap_cents(0.07, 0.074) == pytest.approx(-0.4)

    def test_missing_returns_none(self):
        assert fs.entry_ask_gap_cents(None, 0.074) is None
        assert fs.entry_ask_gap_cents(0.08, None) is None


class TestPreEntryThresholds:
    def test_spread_threshold_boundary(self):
        cand = fs._cand_spread(2.0)
        assert cand.decide({"ws_spread_cents": 2.5}).acts is True   # > 2c blocks
        assert cand.decide({"ws_spread_cents": 2.0}).acts is False  # == 2c allowed
        assert cand.decide({"ws_spread_cents": None}).acts is None   # undecided

    def test_quote_age_threshold(self):
        cand = fs._cand_quote_age(500.0)
        assert cand.decide({"ws_quote_age_ms": 750.0}).acts is True
        assert cand.decide({"ws_quote_age_ms": 250.0}).acts is False

    def test_entry_ask_gap_threshold(self):
        cand = fs._cand_entry_ask_gap(1.0)
        assert cand.decide({"entry_ask_gap_cents": 1.5}).acts is True
        assert cand.decide({"entry_ask_gap_cents": 0.5}).acts is False

    def test_no_high_ask_only_applies_to_no(self):
        cand = fs._cand_no_high_ask(0.50)
        assert cand.decide({"side": "NO", "entry_ask": 0.55}).acts is True
        assert cand.decide({"side": "NO", "entry_ask": 0.45}).acts is False
        assert cand.decide({"side": "YES", "entry_ask": 0.90}).acts is False  # YES untouched
        assert cand.decide({"side": "NO", "entry_ask": None}).acts is None

    def test_tte_bucket_window(self):
        cand = fs._cand_tte_bucket(450.0, 600.0)
        assert cand.decide({"tte_seconds": 500.0}).acts is True
        assert cand.decide({"tte_seconds": 600.0}).acts is False  # upper exclusive
        assert cand.decide({"tte_seconds": 449.0}).acts is False


# ══════════════════════════════════════════════════════════════════════════════
# Confusion matrix + reduction / retention
# ══════════════════════════════════════════════════════════════════════════════

class TestConfusionMatrix:
    def test_counts_and_rates(self):
        trades = _population()
        cand = fs._cand_spread(1.0)  # blocks spread > 1c: SL1(3c) and FT1(2c)
        cm = fs.confusion_matrix(trades, cand)
        # Only PT/SL enter the matrix (FT excluded).
        # SL1 blocked -> TP; SL2 allowed -> FN; PT1 allowed -> TN; no PT blocked.
        assert cm["true_positive"] == 1
        assert cm["false_positive"] == 0
        assert cm["true_negative"] == 1
        assert cm["false_negative"] == 1
        assert cm["precision_stop_loss_avoidance"] == pytest.approx(100.0)
        assert cm["recall_stop_loss_avoidance"] == pytest.approx(50.0)

    def test_undecided_excluded(self):
        trades = [
            _trade("stop_loss", -3.0, ws_spread_cents=None),
            _trade("profit_target", 3.0, ws_spread_cents=0.5),
        ]
        cm = fs.confusion_matrix(trades, fs._cand_spread(1.0))
        assert cm["undecided"] == 1
        assert cm["true_positive"] == 0 and cm["false_negative"] == 0

    def test_stop_loss_reduction_pct(self):
        # 3 of 4 stop losses acted on -> 75%
        assert fs.stop_loss_reduction_pct(tp=3, fn=1) == pytest.approx(75.0)
        assert fs.stop_loss_reduction_pct(tp=0, fn=0) is None

    def test_profit_target_retention_pct(self):
        # 8 of 10 profit targets allowed -> 80%
        assert fs.profit_target_retention_pct(tn=8, fp=2) == pytest.approx(80.0)
        assert fs.profit_target_retention_pct(tn=0, fp=0) is None


# ══════════════════════════════════════════════════════════════════════════════
# Pre-entry summary + promising decision
# ══════════════════════════════════════════════════════════════════════════════

class TestPreEntrySummary:
    def test_blocked_and_allowed_counts(self):
        trades = _population()
        s = fs.summarize_pre_entry(trades, fs._cand_spread(1.0))
        assert s["blocked"]["n"] == 2          # SL1 + FT1
        assert s["blocked"]["stop_loss"] == 1
        assert s["blocked"]["fixed_time"] == 1
        assert s["allowed"]["n"] == 2          # SL2 + PT1
        assert s["stop_loss_reduction_pct"] == pytest.approx(50.0)
        assert s["profit_target_retention_pct"] == pytest.approx(100.0)

    def test_is_promising_requires_all_criteria(self):
        strong = {
            "stop_loss_reduction_pct": 40.0,
            "profit_target_retention_pct": 90.0,
            "confusion": {"true_positive": 8, "false_positive": 1,
                          "true_negative": 9, "false_negative": 12},
        }
        assert fs.is_promising(strong, min_sample=10) is True
        # Fails retention floor.
        weak = dict(strong, profit_target_retention_pct=60.0)
        assert fs.is_promising(weak, min_sample=10) is False
        # Fails sample floor.
        assert fs.is_promising(strong, min_sample=100) is False


# ══════════════════════════════════════════════════════════════════════════════
# Early-exit simulated P/L
# ══════════════════════════════════════════════════════════════════════════════

class TestEarlyExitSimulation:
    def test_not_green_by_20s_uses_20s_pnl(self):
        cand = fs._cand_not_green_by(20)
        # never green -> exits at 20s, simulated pnl = pnl_at_20s
        t = {"time_to_first_green_seconds": None, "pnl_at_20s_cents": -2.0}
        d = cand.decide(t)
        assert d.acts is True
        assert d.trigger_time_seconds == 20.0
        assert d.simulated_exit_pnl_cents == pytest.approx(-2.0)

    def test_not_green_by_20s_allows_green_trade(self):
        cand = fs._cand_not_green_by(20)
        t = {"time_to_first_green_seconds": 5.0, "pnl_at_20s_cents": 1.0}
        assert cand.decide(t).acts is False

    def test_red_after_15s_threshold(self):
        cand = fs._cand_red_after(15, -1.0)
        assert cand.decide({"pnl_at_15s_cents": -1.5}).acts is True
        assert cand.decide({"pnl_at_15s_cents": -0.5}).acts is False
        assert cand.decide({"pnl_at_15s_cents": None}).acts is None

    def test_down_2c_by_30s(self):
        cand = fs._cand_red_after(30, -2.0)
        assert cand.decide({"pnl_at_30s_cents": -2.0}).acts is True
        assert cand.decide({"pnl_at_30s_cents": -1.0}).acts is False

    def test_no_progress_by_45s_is_missing_telemetry(self):
        # pnl_at_45s is not captured yet -> undecided (never guesses).
        cand = fs._cand_no_progress_by(45, 1.0)
        assert cand.decide({"pnl_at_20s_cents": -1.0}).acts is None

    def test_summary_net_improvement(self):
        trades = _population()
        cand = fs._cand_not_green_by(20)
        s = fs.summarize_early_exit(trades, cand)
        # SL1 (never green) triggers; SL2 green@25s -> still not green by 20s -> triggers;
        # FT1 green@8s -> allowed; PT1 green@3s -> allowed.
        assert s["triggered"] == 2
        assert s["stop_loss_avoided"] == 2
        assert s["profit_target_cut"] == 0
        # net improvement = sum(sim - actual) over triggered
        # SL1: -2.0 - (-3.0) = +1.0 ; SL2: -0.5 - (-3.0) = +2.5 ; total +3.5
        assert s["net_improvement_cents"] == pytest.approx(3.5)


# ══════════════════════════════════════════════════════════════════════════════
# Baseline
# ══════════════════════════════════════════════════════════════════════════════

class TestBaseline:
    def test_baseline_counts(self):
        b = fs.baseline_performance(_population())
        assert b["total_trades"] == 4
        assert b["stop_loss"] == 2
        assert b["profit_target"] == 1
        assert b["fixed_time"] == 1
        assert b["stop_loss_rate"] == pytest.approx(50.0)
        # nets: -3,-3,+3,-1 -> total -4
        assert b["total_net_cents"] == pytest.approx(-4.0)
        assert b["profit_factor"] == pytest.approx(3.0 / 7.0, rel=1e-3)


# ══════════════════════════════════════════════════════════════════════════════
# Normalisation (unit conversions)
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeTrade:
    def test_units_and_fallbacks(self):
        raw = {
            "id": 7,
            "side": "NO",
            "exit_reason": "stop_loss",
            "shadow_only": 1,
            "projected_entry_ask": 0.60,
            "actual_profit_cents": -0.03,      # dollar fraction incl fees -> -3c
            "ws_spread_at_signal": 0.02,       # 2c
            "ws_entry_ask_at_signal": 0.615,
            "rest_ideal_entry_ask": 0.60,
            "ws_quote_age_ms_at_signal": 320.0,
            "time_to_expiry_seconds_at_signal": 512.0,
            "pnl_at_20s_cents": -2.0,
        }
        n = fs.normalize_trade(raw)
        assert n["id"] == 7
        assert n["net_cents"] == pytest.approx(-3.0)
        assert n["ws_spread_cents"] == pytest.approx(2.0)
        assert n["entry_ask_gap_cents"] == pytest.approx(1.5)  # (0.615-0.60)*100
        assert n["ws_quote_age_ms"] == pytest.approx(320.0)
        assert n["tte_seconds"] == pytest.approx(512.0)

    def test_legacy_row_fallbacks(self):
        # Old row: only ws_spread_at_entry + ws_quote_age_at_entry (seconds).
        raw = {
            "exit_reason": "profit_target",
            "projected_entry_ask": 0.20,
            "actual_pnl_cents": 3.0,           # already true cents, no fee column
            "ws_spread_at_entry": 0.01,
            "ws_quote_age_at_entry": 1.5,      # seconds
        }
        n = fs.normalize_trade(raw)
        assert n["net_cents"] == pytest.approx(3.0)
        assert n["ws_spread_cents"] == pytest.approx(1.0)
        assert n["ws_quote_age_ms"] == pytest.approx(1500.0)


# ══════════════════════════════════════════════════════════════════════════════
# Shadow-only mode NEVER places orders
# ══════════════════════════════════════════════════════════════════════════════

class _SpyClient:
    """Records any order-API call; every method here is forbidden in shadow mode."""
    def __init__(self):
        self.calls: list[str] = []

    def place_order(self, *a, **k):
        self.calls.append("place_order")
        raise AssertionError("place_order must never be called in shadow-only mode")

    def cancel_order(self, *a, **k):
        self.calls.append("cancel_order")
        raise AssertionError("cancel_order must never be called in shadow-only mode")


@pytest.fixture
def shadow_trader(monkeypatch):
    monkeypatch.setattr(config, "MOMENTUM_WS_SHADOW_ONLY", True)
    monkeypatch.setattr(config, "MOMENTUM_LIVE_USE_WEBSOCKET", False)
    monkeypatch.setattr(config, "MOMENTUM_FILTER_SHADOW_EVAL", False)
    trader = MomentumLiveTrader(ws_stream=None)
    return trader


class TestShadowOnlyNoOrders:
    def test_shadow_only_is_inert_for_orders(self, shadow_trader):
        assert shadow_trader.shadow_only is True
        assert shadow_trader.armed is False
        assert shadow_trader._orders_enabled is False
        # No real trading client is ever constructed in shadow-only mode.
        assert shadow_trader._client is None

    def test_guard_blocks_order_calls(self, shadow_trader):
        with pytest.raises(RuntimeError, match="order placement blocked"):
            shadow_trader._guard_orders_enabled("entry")
        with pytest.raises(RuntimeError, match="order placement blocked"):
            shadow_trader._guard_orders_enabled("exit")

    def test_shadow_entry_places_no_order(self, shadow_trader, monkeypatch):
        # Stub the DB writes so the entry path runs without a database, and
        # attach a spy client that fails loudly if any order method is touched.
        writes: list[str] = []
        monkeypatch.setattr(mlt, "insert_and_get_id", lambda *a, **k: 4242)
        monkeypatch.setattr(mlt, "execute_query", lambda *a, **k: writes.append("w"))
        shadow_trader._client = _SpyClient()

        sig = SimpleNamespace(
            market_id=1, contract_id=99, side="NO",
            signal_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
            signal_ts=1_000_000.0, entry_ask=0.55, entry_spread=0.02,
            time_remaining_s=500,
        )
        shadow_trader._open_shadow_entry(sig, "KXBTC-TEST", datetime(2026, 7, 2, tzinfo=timezone.utc))

        # A hypothetical trade was recorded and is being tracked, with NO order.
        assert 99 in shadow_trader._active
        active = shadow_trader._active[99]
        assert active.shadow_only is True
        assert active.actual_entry_price == pytest.approx(0.55)
        assert active.live_trade_id == 4242
        assert shadow_trader._client.calls == []   # place_order never called
        assert writes, "signal-time telemetry should have been persisted"
