from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import mcp_logger_server as mcp


class TestReadonlySqlValidation:
    def test_allows_single_readonly_statements(self):
        assert mcp.validate_readonly_sql("SELECT * FROM momentum_live_trades;").startswith("SELECT")
        assert mcp.validate_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH")
        assert mcp.validate_readonly_sql("DESCRIBE contracts") == "DESCRIBE contracts"
        assert mcp.validate_readonly_sql("SHOW TABLES") == "SHOW TABLES"

    @pytest.mark.parametrize(
        "sql",
        [
            "",
            "DELETE FROM momentum_live_trades",
            "SELECT * FROM x; DELETE FROM x",
            "UPDATE momentum_live_trades SET status='x'",
            "DROP TABLE market_snapshots",
            "SET @x = 1",
        ],
    )
    def test_rejects_write_or_multi_statement_sql(self, sql):
        with pytest.raises(ValueError):
            mcp.validate_readonly_sql(sql)


class TestJsonSerialization:
    def test_json_text_serializes_db_types(self):
        payload = mcp._as_json_text({
            "price": Decimal("0.9300"),
            "captured_at": datetime(2026, 7, 3, 1, 2, 3),
        })
        decoded = json.loads(payload["text"])
        assert decoded["price"] == 0.93
        assert decoded["captured_at"] == "2026-07-03T01:02:03"


class TestToolDispatch:
    def test_tools_list_contains_expected_tools(self):
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        assert "live_summary" in names
        assert "filter_diagnostics" in names
        assert "readonly_sql" in names
        assert "late_contract_sweep" in names

    def test_unknown_tool_returns_error(self):
        response = mcp.handle_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "not_real", "arguments": {}},
        })
        assert response["error"]["code"] == -32603
        assert "unknown tool" in response["error"]["message"]

    def test_readonly_sql_wraps_select_with_limit(self, monkeypatch):
        calls = []

        def fake_fetch(sql, params=()):
            calls.append((sql, params))
            return [{"x": Decimal("1.0")}]

        monkeypatch.setattr(mcp, "_safe_fetch_all", fake_fetch)
        result = mcp.tool_readonly_sql({"sql": "SELECT 1 AS x", "limit": 5})
        body = json.loads(result["content"][0]["text"])
        assert body["row_count"] == 1
        assert body["rows"] == [{"x": 1.0}]
        assert "SELECT * FROM (SELECT 1 AS x)" in calls[0][0]
        assert calls[0][1] == (5,)

    def test_filter_diagnostics_shapes_summary(self, monkeypatch):
        rows = [
            {
                "id": 1,
                "exit_reason": "profit_target",
                "actual_profit_cents": Decimal("0.03"),
                "projected_entry_ask": Decimal("0.20"),
                "side": "YES",
                "ws_spread_at_signal": Decimal("0.005"),
                "ws_quote_age_ms_at_signal": Decimal("100"),
                "time_to_expiry_seconds_at_signal": Decimal("700"),
                "pnl_at_30s_cents": Decimal("3.0"),
            },
            {
                "id": 2,
                "exit_reason": "stop_loss",
                "actual_profit_cents": Decimal("-0.04"),
                "projected_entry_ask": Decimal("0.60"),
                "side": "NO",
                "ws_spread_at_signal": Decimal("0.030"),
                "ws_quote_age_ms_at_signal": Decimal("800"),
                "time_to_expiry_seconds_at_signal": Decimal("500"),
                "pnl_at_30s_cents": Decimal("-2.0"),
            },
        ]

        monkeypatch.setattr(mcp, "_safe_fetch_all", lambda sql, params=(): rows)
        result = mcp.tool_filter_diagnostics({"source": "all", "min_sample": 1})
        body = json.loads(result["content"][0]["text"])
        assert body["raw_rows"] == 2
        assert body["classified_rows"] == 2
        assert body["baseline"]["profit_target"] == 1
        assert body["baseline"]["stop_loss"] == 1
        assert body["missing_telemetry"]["pnl_at_30s_cents"]["missing"] == 0
