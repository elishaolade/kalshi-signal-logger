from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.kalshi_ws import (
    KalshiMarketStream,
    build_connect_kwargs,
    compute_depth_at_or_better,
    compute_spread,
    derive_ws_url,
    infer_no_ask,
    infer_yes_ask,
)


class TestQuoteMath:
    def test_infers_opposite_ask(self):
        assert infer_yes_ask(0.62) == pytest.approx(0.38)
        assert infer_no_ask(0.27) == pytest.approx(0.73)

    def test_spread(self):
        assert compute_spread(0.22, 0.25) == pytest.approx(0.03)
        assert compute_spread(None, 0.25) is None

    def test_depth_at_or_better(self):
        levels = {0.25: 10, 0.24: 7, 0.20: 5}
        assert compute_depth_at_or_better(levels, 0.24) == pytest.approx(17.0)


class TestConnectKwargs:
    def test_derives_production_ws_host_from_rest_base(self):
        assert derive_ws_url("https://external-api.kalshi.com/trade-api/v2") == (
            "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
        )

    def test_derives_demo_ws_host_from_rest_base(self):
        assert derive_ws_url("https://demo-api.kalshi.co/trade-api/v2") == (
            "wss://demo-api-ws.kalshi.co/trade-api/ws/v2"
        )

    def test_uses_additional_headers_when_supported(self, monkeypatch):
        class FakeWebsockets:
            def connect(self, uri, *, additional_headers=None, ping_interval=None):
                return None

        monkeypatch.setattr("app.kalshi_ws.websockets", FakeWebsockets())
        kwargs = build_connect_kwargs({"Authorization": "x"})
        assert kwargs == {"additional_headers": {"Authorization": "x"}}

    def test_uses_extra_headers_when_supported(self, monkeypatch):
        class FakeWebsockets:
            def connect(self, uri, *, extra_headers=None, ping_interval=None):
                return None

        monkeypatch.setattr("app.kalshi_ws.websockets", FakeWebsockets())
        kwargs = build_connect_kwargs({"Authorization": "x"})
        assert kwargs == {"extra_headers": {"Authorization": "x"}}


class TestStreamCache:
    def test_snapshot_and_delta_update_best_prices(self):
        stream = KalshiMarketStream()
        stream.apply_orderbook_snapshot(
            "KXBTC15M-TEST",
            yes_bids=[(0.31, 4), (0.30, 8)],
            no_bids=[(0.66, 3), (0.65, 5)],
            updated_at_ts=100.0,
        )
        assert stream.get_best_bid("KXBTC15M-TEST", "YES") == pytest.approx(0.31)
        assert stream.get_best_ask("KXBTC15M-TEST", "YES") == pytest.approx(0.34)
        assert stream.get_spread("KXBTC15M-TEST", "YES") == pytest.approx(0.03)

        stream.apply_orderbook_delta(
            "KXBTC15M-TEST",
            "YES",
            [(0.32, 5), (0.31, 0)],
            updated_at_ts=101.0,
        )
        assert stream.get_best_bid("KXBTC15M-TEST", "YES") == pytest.approx(0.32)

    def test_quote_age(self):
        stream = KalshiMarketStream()
        now = time.time()
        stream.apply_orderbook_snapshot(
            "KXBTC15M-TEST",
            yes_bids=[(0.31, 4)],
            no_bids=[(0.66, 3)],
            updated_at_ts=now - 2.0,
        )
        age = stream.get_quote_age_seconds("KXBTC15M-TEST")
        assert age is not None
        assert 1.0 <= age <= 3.5

    def test_unsubscribe_market_clears_local_book_and_subscription(self):
        stream = KalshiMarketStream()
        stream.subscribe_market("KXBTC15M-TEST")
        stream.apply_orderbook_snapshot(
            "KXBTC15M-TEST",
            yes_bids=[(0.31, 4)],
            no_bids=[(0.66, 3)],
            updated_at_ts=100.0,
        )

        stream.unsubscribe_market("KXBTC15M-TEST")

        assert stream.get_quote("KXBTC15M-TEST") is None
        assert "KXBTC15M-TEST" not in stream._subscribed_markets

    def test_order_state_tracks_by_client_order_id(self):
        stream = KalshiMarketStream()
        stream.apply_order_update(
            order_id="srv-1",
            client_order_id="cli-1",
            status="partially_filled",
            remaining_count=3,
            filled_count=7,
            avg_fill_price=0.41,
            fee_total=0.02,
            detected_by="websocket",
            updated_at_ts=100.0,
        )
        state = stream.get_order_state(client_order_id="cli-1")
        assert state is not None
        assert state.order_id == "srv-1"
        assert state.filled_count == pytest.approx(7)
        assert state.avg_fill_price == pytest.approx(0.41)

    def test_ingest_message_parses_orderbook_and_order_update(self):
        stream = KalshiMarketStream()
        stream.ingest_message(
            {
                "type": "orderbook_snapshot",
                "ticker": "KXBTC15M-TEST",
                "yes_bids": [{"price": "0.40", "size": "5"}],
                "no_bids": [{"price": "0.55", "size": "7"}],
            }
        )
        assert stream.get_best_bid("KXBTC15M-TEST", "YES") == pytest.approx(0.40)
        assert stream.get_best_ask("KXBTC15M-TEST", "NO") == pytest.approx(0.60)

        stream.ingest_message(
            {
                "channel": "user_orders",
                "data": {
                    "order_id": "srv-2",
                    "client_order_id": "cli-2",
                    "status": "filled",
                    "fill_count_fp": "20.00",
                    "yes_price_dollars": "0.2900",
                    "fee_cost": "0.2883",
                },
            }
        )
        state = stream.get_order_state(order_id="srv-2")
        assert state is not None
        assert state.status == "filled"
        assert state.filled_count == pytest.approx(20.0)
        assert state.avg_fill_price == pytest.approx(0.29)

    def test_ingest_message_parses_kalshi_v2_snapshot_and_delta(self):
        stream = KalshiMarketStream()
        stream.ingest_message(
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": 1,
                "msg": {
                    "market_ticker": "KXBTC15M-TEST",
                    "yes": [[31, 10], [30, 5]],
                    "no": [[66, 7]],
                },
            }
        )
        assert stream.get_best_bid("KXBTC15M-TEST", "YES") == pytest.approx(0.31)
        assert stream.get_best_ask("KXBTC15M-TEST", "YES") == pytest.approx(0.34)

        stream.ingest_message(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 2,
                "msg": {
                    "market_ticker": "KXBTC15M-TEST",
                    "side": "yes",
                    "price": 32,
                    "delta": 4,
                },
            }
        )
        assert stream.get_best_bid("KXBTC15M-TEST", "YES") == pytest.approx(0.32)
        assert stream.get_depth_at_or_better("KXBTC15M-TEST", "YES", 0.32) == pytest.approx(4)

    def test_ingest_message_parses_kalshi_dollars_fp_fields(self):
        stream = KalshiMarketStream()
        stream.ingest_message(
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": 1,
                "msg": {
                    "market_ticker": "KXBTC15M-TEST",
                    "yes_dollars_fp": [["0.31", "10"]],
                    "no_dollars_fp": [["0.66", "7"]],
                },
            }
        )
        assert stream.get_best_bid("KXBTC15M-TEST", "YES") == pytest.approx(0.31)
        assert stream.get_best_ask("KXBTC15M-TEST", "YES") == pytest.approx(0.34)

        stream.ingest_message(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 2,
                "msg": {
                    "market_ticker": "KXBTC15M-TEST",
                    "side": "yes",
                    "price_dollars": "0.32",
                    "delta_fp": "4",
                },
            }
        )
        assert stream.get_best_bid("KXBTC15M-TEST", "YES") == pytest.approx(0.32)

    def test_small_crossed_delta_keeps_book_for_observer_debounce(self, monkeypatch):
        monkeypatch.setattr("app.config.MOMENTUM_WS_ORDERBOOK_MAX_CROSSED_AMOUNT", 0.03)

        stream = KalshiMarketStream()
        stream.apply_orderbook_snapshot(
            "KXBTC15M-TEST",
            yes_bids=[(0.41, 4)],
            no_bids=[(0.58, 3)],
            updated_at_ts=100.0,
        )
        calls = []
        monkeypatch.setattr(stream, "reset_market", lambda ticker: calls.append(ticker))

        stream.apply_orderbook_delta(
            "KXBTC15M-TEST",
            "YES",
            [(0.43, 5)],
            updated_at_ts=101.0,
        )

        quote = stream.get_quote("KXBTC15M-TEST")
        assert quote is not None
        assert quote.crossed_amount() == pytest.approx(0.01)
        assert calls == []

    def test_severe_crossed_delta_drops_book_and_resets_market(self, monkeypatch):
        monkeypatch.setattr("app.config.MOMENTUM_WS_ORDERBOOK_MAX_CROSSED_AMOUNT", 0.03)

        stream = KalshiMarketStream()
        stream.apply_orderbook_snapshot(
            "KXBTC15M-TEST",
            yes_bids=[(0.41, 4)],
            no_bids=[(0.58, 3)],
            updated_at_ts=100.0,
        )
        calls = []
        monkeypatch.setattr(stream, "reset_market", lambda ticker: calls.append(ticker))

        stream.apply_orderbook_delta(
            "KXBTC15M-TEST",
            "YES",
            [(0.63, 5)],
            updated_at_ts=101.0,
        )

        assert stream.get_quote("KXBTC15M-TEST") is None
        assert calls == ["KXBTC15M-TEST"]

    def test_sequence_gap_resets_market_and_ignores_delta(self, monkeypatch):
        stream = KalshiMarketStream()
        stream.ingest_message(
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": 10,
                "msg": {
                    "market_ticker": "KXBTC15M-TEST",
                    "yes_dollars_fp": [["0.41", "10"]],
                    "no_dollars_fp": [["0.58", "7"]],
                },
            }
        )
        calls = []
        monkeypatch.setattr(stream, "reset_market", lambda ticker: calls.append(ticker))

        stream.ingest_message(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 12,
                "msg": {
                    "market_ticker": "KXBTC15M-TEST",
                    "side": "yes",
                    "price_dollars": "0.42",
                    "delta_fp": "4",
                },
            }
        )

        assert calls == ["KXBTC15M-TEST"]
        assert stream.get_best_bid("KXBTC15M-TEST", "YES") == pytest.approx(0.41)

    def test_duplicate_sequence_ignores_delta_without_reset(self, monkeypatch):
        stream = KalshiMarketStream()
        stream.ingest_message(
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": 10,
                "msg": {
                    "market_ticker": "KXBTC15M-TEST",
                    "yes_dollars_fp": [["0.41", "10"]],
                    "no_dollars_fp": [["0.58", "7"]],
                },
            }
        )
        calls = []
        monkeypatch.setattr(stream, "reset_market", lambda ticker: calls.append(ticker))

        stream.ingest_message(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 10,
                "msg": {
                    "market_ticker": "KXBTC15M-TEST",
                    "side": "yes",
                    "price_dollars": "0.42",
                    "delta_fp": "4",
                },
            }
        )

        assert calls == []
        assert stream.get_best_bid("KXBTC15M-TEST", "YES") == pytest.approx(0.41)
