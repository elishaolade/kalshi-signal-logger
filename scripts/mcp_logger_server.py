#!/usr/bin/env python3
"""
mcp_logger_server.py - read-only MCP server for Kalshi logger data.

This is a dependency-light Model Context Protocol server over stdio.  It gives
Codex/Claude-style MCP clients safe access to the logger database without
exposing write operations or trading actions.

Usage:
    python scripts/mcp_logger_server.py

Example MCP client config:
    {
      "mcpServers": {
        "kalshi-logger": {
          "command": "python3",
          "args": ["/opt/kalshi-signal-logger/scripts/mcp_logger_server.py"],
          "env": {
            "MYSQL_HOST": "127.0.0.1",
            "MYSQL_PORT": "3306",
            "MYSQL_DATABASE": "signal_logger",
            "MYSQL_USER": "123_XJSV",
            "MYSQL_PASSWORD": "..."
          }
        }
      }
    }
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import momentum_filter_shadow as fs

SERVER_NAME = "kalshi-logger-mcp"
SERVER_VERSION = "0.1.0"
MAX_SQL_ROWS = 500

_WRITE_RE = re.compile(
    r"\b("
    r"alter|analyze|call|create|delete|drop|execute|grant|insert|load|lock|"
    r"optimize|replace|revoke|set|truncate|update"
    r")\b",
    re.IGNORECASE,
)
_READONLY_PREFIX_RE = re.compile(r"^\s*(select|with|show|describe|desc)\b", re.IGNORECASE)


# ==============================================================================
# JSON / MCP helpers
# ==============================================================================

def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _as_json_text(value: Any) -> dict[str, str]:
    return {
        "type": "text",
        "text": json.dumps(value, default=_json_default, indent=2, sort_keys=True),
    }


def _tool_result(value: Any) -> dict[str, Any]:
    return {"content": [_as_json_text(value)]}


def _error_response(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _ok_response(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _print_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, default=_json_default, separators=(",", ":")) + "\n")
    sys.stdout.flush()


# ==============================================================================
# Safety / data helpers
# ==============================================================================

def validate_readonly_sql(sql: str) -> str:
    """Return stripped SQL when it is a single read-only statement."""
    stripped = (sql or "").strip()
    if not stripped:
        raise ValueError("sql is required")
    if stripped.count(";") > 1 or (";" in stripped and not stripped.endswith(";")):
        raise ValueError("only one SQL statement is allowed")
    stripped = stripped.rstrip(";").strip()
    if not _READONLY_PREFIX_RE.match(stripped):
        raise ValueError("only SELECT/WITH/SHOW/DESCRIBE statements are allowed")
    if _WRITE_RE.search(stripped):
        raise ValueError("write/admin SQL keywords are not allowed")
    return stripped


def _limit(n: Any, *, default: int = 50, maximum: int = MAX_SQL_ROWS) -> int:
    try:
        value = int(n)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _hours_clause(hours: Optional[int], column: str = "signal_at") -> tuple[str, tuple[Any, ...]]:
    if hours is None:
        return "", ()
    start = datetime.now(timezone.utc) - timedelta(hours=int(hours))
    return f" AND {column} >= %s", (start,)


def _safe_fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    from app.db import fetch_all

    return fetch_all(sql, params)


def _safe_fetch_one(sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
    from app.db import fetch_one

    return fetch_one(sql, params)


def _normalize_filter_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    classified = {
        fs.OUTCOME_PROFIT_TARGET,
        fs.OUTCOME_STOP_LOSS,
        fs.OUTCOME_FIXED_TIME,
    }
    return [
        fs.normalize_trade(r)
        for r in rows
        if r.get("exit_reason") in classified
    ]


def _missing_count(rows: list[dict[str, Any]], field: str) -> int:
    return sum(1 for row in rows if row.get(field) is None)


# ==============================================================================
# MCP tool implementations
# ==============================================================================

def tool_readonly_sql(args: dict[str, Any]) -> dict[str, Any]:
    sql = validate_readonly_sql(str(args.get("sql", "")))
    limit = _limit(args.get("limit"), default=100)
    wrapped = f"SELECT * FROM ({sql}) AS mcp_readonly_q LIMIT %s"
    if re.match(r"^\s*(show|describe|desc)\b", sql, re.IGNORECASE):
        rows = _safe_fetch_all(sql)
        rows = rows[:limit]
    else:
        rows = _safe_fetch_all(wrapped, (limit,))
    return _tool_result({"row_count": len(rows), "rows": rows})


def tool_schema_overview(args: dict[str, Any]) -> dict[str, Any]:
    table_arg = args.get("tables")
    if isinstance(table_arg, str):
        tables = [t.strip() for t in table_arg.split(",") if t.strip()]
    elif isinstance(table_arg, list):
        tables = [str(t).strip() for t in table_arg if str(t).strip()]
    else:
        tables = [
            "markets",
            "contracts",
            "market_snapshots",
            "contract_snapshots",
            "momentum_shadow_trades",
            "momentum_live_trades",
            "momentum_live_guardrail_events",
        ]

    out: dict[str, Any] = {}
    for table in tables:
        if not re.match(r"^[A-Za-z0-9_]+$", table):
            out[table] = {"error": "invalid table name"}
            continue
        try:
            out[table] = _safe_fetch_all(f"DESCRIBE {table}")
        except Exception as exc:
            out[table] = {"error": str(exc)}
    return _tool_result(out)


def tool_live_summary(args: dict[str, Any]) -> dict[str, Any]:
    hours = args.get("hours")
    hours_int = int(hours) if hours is not None else None
    time_sql, params = _hours_clause(hours_int)

    status_rows = _safe_fetch_all(
        f"""
        SELECT status, COUNT(*) AS n
        FROM momentum_live_trades
        WHERE 1=1 {time_sql}
        GROUP BY status
        ORDER BY status
        """,
        params,
    )
    exit_rows = _safe_fetch_all(
        f"""
        SELECT
          exit_reason,
          COUNT(*) AS trades,
          SUM(actual_profit_dollars > 0) AS wins,
          SUM(actual_profit_dollars <= 0) AS losses,
          ROUND(AVG(actual_profit_dollars), 4) AS avg_pnl_dollars,
          ROUND(SUM(actual_profit_dollars), 4) AS total_pnl_dollars
        FROM momentum_live_trades
        WHERE status='COMPLETE'
          AND filled_contracts > 0
          AND actual_profit_dollars IS NOT NULL
          {time_sql}
        GROUP BY exit_reason
        ORDER BY total_pnl_dollars ASC
        """,
        params,
    )
    overall = _safe_fetch_one(
        f"""
        SELECT
          COUNT(*) AS completed_trades,
          SUM(actual_profit_dollars > 0) AS wins,
          SUM(actual_profit_dollars <= 0) AS losses,
          ROUND(100 * SUM(actual_profit_dollars > 0) / NULLIF(COUNT(*), 0), 1) AS win_rate_pct,
          ROUND(AVG(actual_profit_dollars), 4) AS avg_pnl_dollars,
          ROUND(SUM(actual_profit_dollars), 4) AS total_pnl_dollars
        FROM momentum_live_trades
        WHERE status='COMPLETE'
          AND filled_contracts > 0
          AND actual_profit_dollars IS NOT NULL
          {time_sql}
        """,
        params,
    )
    pause = None
    try:
        pause = _safe_fetch_one("SELECT * FROM momentum_live_pause_state WHERE id=1")
    except Exception as exc:
        pause = {"error": str(exc)}
    return _tool_result({
        "hours": hours_int,
        "status_breakdown": status_rows,
        "completed_summary": overall,
        "exit_reason_breakdown": exit_rows,
        "pause_state": pause,
    })


def tool_recent_live_trades(args: dict[str, Any]) -> dict[str, Any]:
    limit = _limit(args.get("limit"), default=20, maximum=200)
    hours = args.get("hours")
    hours_int = int(hours) if hours is not None else None
    time_sql, params = _hours_clause(hours_int)
    rows = _safe_fetch_all(
        f"""
        SELECT
          id, signal_at, entry_at, exit_at, market_ticker, side, status, exit_profile,
          requested_contracts, filled_contracts,
          projected_entry_ask, projected_target_ask, projected_exit_bid,
          actual_entry_price, actual_exit_price,
          actual_profit_cents, actual_profit_dollars, exit_reason,
          ws_enabled, ws_quote_age_at_entry, ws_spread_at_entry,
          shadow_only, diagnostic_mode,
          pnl_at_5s_cents, pnl_at_10s_cents, pnl_at_15s_cents,
          pnl_at_20s_cents, pnl_at_30s_cents,
          max_profit_first_30s_cents, min_profit_first_30s_cents
        FROM momentum_live_trades
        WHERE 1=1 {time_sql}
        ORDER BY signal_at DESC
        LIMIT %s
        """,
        params + (limit,),
    )
    return _tool_result({"row_count": len(rows), "rows": rows})


def tool_guardrail_events(args: dict[str, Any]) -> dict[str, Any]:
    limit = _limit(args.get("limit"), default=50, maximum=200)
    hours = int(args.get("hours", 24))
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = _safe_fetch_all(
        """
        SELECT id, created_at, market_ticker, side, event_type, reason
        FROM momentum_live_guardrail_events
        WHERE created_at >= %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (start, limit),
    )
    return _tool_result({"hours": hours, "row_count": len(rows), "rows": rows})


def tool_filter_diagnostics(args: dict[str, Any]) -> dict[str, Any]:
    hours = args.get("hours")
    source = str(args.get("source", "all"))
    min_sample = _limit(args.get("min_sample"), default=20, maximum=10_000)

    clauses = ["status = 'COMPLETE'", "exit_reason IS NOT NULL"]
    params: list[Any] = []
    if hours is not None:
        clauses.append("signal_at >= %s")
        params.append(datetime.now(timezone.utc) - timedelta(hours=int(hours)))
    if source == "shadow_only":
        clauses.append("shadow_only = 1")
    elif source == "live":
        clauses.append("(shadow_only IS NULL OR shadow_only = 0)")
    elif source != "all":
        raise ValueError("source must be one of: all, live, shadow_only")

    rows = _safe_fetch_all(
        f"SELECT * FROM momentum_live_trades WHERE {' AND '.join(clauses)} ORDER BY signal_at DESC",
        tuple(params),
    )
    trades = _normalize_filter_rows(rows)
    pre = []
    for cand in fs.default_pre_entry_candidates():
        summary = fs.summarize_pre_entry(trades, cand)
        summary["promising"] = fs.is_promising(summary, min_sample=min_sample)
        pre.append(summary)
    early = []
    for cand in fs.default_early_exit_candidates():
        summary = fs.summarize_early_exit(trades, cand)
        summary["promising"] = fs.is_promising(summary, min_sample=min_sample)
        early.append(summary)

    telemetry_fields = [
        "ws_entry_ask_at_signal",
        "ws_spread_at_signal",
        "ws_quote_age_ms_at_signal",
        "time_to_expiry_seconds_at_signal",
        "entry_ask_gap_cents",
        "pnl_at_5s_cents",
        "pnl_at_10s_cents",
        "pnl_at_15s_cents",
        "pnl_at_20s_cents",
        "pnl_at_30s_cents",
        "max_profit_first_30s_cents",
        "min_profit_first_30s_cents",
    ]
    missing = {
        field: {
            "missing": _missing_count(rows, field),
            "missing_pct": round(100.0 * _missing_count(rows, field) / len(rows), 2) if rows else None,
        }
        for field in telemetry_fields
    }
    return _tool_result({
        "hours": hours,
        "source": source,
        "raw_rows": len(rows),
        "classified_rows": len(trades),
        "baseline": fs.baseline_performance(trades),
        "pre_entry": pre,
        "early_exit": early,
        "missing_telemetry": missing,
        "unit_note": "P/L values are TRUE cents where 3.0 = 3 cents.",
    })


def tool_shadow_summary(args: dict[str, Any]) -> dict[str, Any]:
    limit = _limit(args.get("limit"), default=20, maximum=200)
    rows = _safe_fetch_all(
        """
        SELECT
          exit_profile,
          COUNT(*) AS trades,
          SUM(net_pnl_cents > 0) AS wins,
          SUM(net_pnl_cents <= 0) AS losses,
          ROUND(100 * SUM(net_pnl_cents > 0) / NULLIF(COUNT(*), 0), 1) AS win_rate_pct,
          ROUND(AVG(net_pnl_cents), 4) AS avg_net_cents,
          ROUND(SUM(net_pnl_cents), 4) AS total_net_cents
        FROM momentum_shadow_trades
        WHERE status='COMPLETE'
          AND net_pnl_cents IS NOT NULL
        GROUP BY exit_profile
        ORDER BY total_net_cents DESC
        LIMIT %s
        """,
        (limit,),
    )
    return _tool_result({"row_count": len(rows), "rows": rows})


def tool_late_contract_sweep(args: dict[str, Any]) -> dict[str, Any]:
    """Research helper for high-price late-contract continuation pockets."""
    entry_min = float(args.get("entry_min", 0.90))
    entry_max = float(args.get("entry_max", 0.92))
    tte_min = int(args.get("tte_min", 180))
    tte_max = int(args.get("tte_max", 240))
    spread_max = float(args.get("spread_max", 0.02))
    stop_bid = float(args.get("stop_bid", 0.85))
    min_candidates = int(args.get("min_candidates", 1))

    rows = _safe_fetch_all(
        """
        WITH raw AS (
          SELECT
            ms.id AS snapshot_id,
            ms.captured_at AS entry_at,
            DATE(ms.captured_at) AS entry_date,
            ms.market_id,
            c.id AS contract_pk,
            c.side,
            cs.ask_price AS entry_ask,
            cs.spread,
            ms.time_remaining_seconds AS tte_seconds,
            ms.btc_price,
            m.target_price
          FROM market_snapshots ms
          JOIN markets m ON m.id = ms.market_id
          JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
          JOIN contracts c ON c.id = cs.contract_id
          WHERE ms.time_remaining_seconds BETWEEN %s AND %s
            AND cs.ask_price BETWEEN %s AND %s
            AND cs.spread <= %s
        ),
        filtered AS (
          SELECT
            raw.*,
            CASE
              WHEN side = 'YES' THEN btc_price - target_price
              WHEN side = 'NO' THEN target_price - btc_price
              ELSE NULL
            END AS side_distance
          FROM raw
        ),
        grid AS (
          SELECT 40 AS min_dist
          UNION ALL SELECT 60
          UNION ALL SELECT 80
          UNION ALL SELECT 100
          UNION ALL SELECT 120
        ),
        candidates AS (
          SELECT *
          FROM (
            SELECT
              g.min_dist,
              filtered.*,
              ROW_NUMBER() OVER (
                PARTITION BY g.min_dist, market_id, contract_pk
                ORDER BY entry_at ASC
              ) AS rn
            FROM grid g
            JOIN filtered
              ON filtered.side_distance >= g.min_dist
          ) x
          WHERE rn = 1
        ),
        future AS (
          SELECT
            c.min_dist,
            c.snapshot_id,
            MIN(fcs.bid_price) AS min_future_bid,
            MAX(fcs.bid_price) AS max_future_bid,
            MIN(CASE WHEN fcs.bid_price <= %s THEN fms.captured_at END) AS touched_stop_at,
            MAX(CASE WHEN fms.time_remaining_seconds <= 5 THEN fcs.bid_price END) AS final_near_close_bid
          FROM candidates c
          JOIN market_snapshots fms
            ON fms.market_id = c.market_id
           AND fms.captured_at >= c.entry_at
          JOIN contract_snapshots fcs
            ON fcs.market_snapshot_id = fms.id
           AND fcs.contract_id = c.contract_pk
          GROUP BY c.min_dist, c.snapshot_id
        )
        SELECT
          c.min_dist,
          c.entry_date,
          c.side,
          COUNT(*) AS candidates,
          SUM(f.touched_stop_at IS NULL) AS survived_stop,
          SUM(f.touched_stop_at IS NOT NULL) AS stopped,
          ROUND(100 * SUM(f.touched_stop_at IS NULL) / COUNT(*), 1) AS survived_stop_pct,
          ROUND(AVG(c.entry_ask), 4) AS avg_entry,
          ROUND(AVG(c.side_distance), 2) AS avg_dist,
          ROUND(AVG(f.min_future_bid), 4) AS avg_min_bid,
          ROUND(AVG(
            CASE
              WHEN f.touched_stop_at IS NOT NULL THEN %s - c.entry_ask
              ELSE COALESCE(f.final_near_close_bid, f.max_future_bid) - c.entry_ask
            END
          ), 4) AS avg_pnl_stop
        FROM candidates c
        JOIN future f
          ON f.min_dist = c.min_dist
         AND f.snapshot_id = c.snapshot_id
        GROUP BY c.min_dist, c.entry_date, c.side
        HAVING candidates >= %s
        ORDER BY c.min_dist, c.entry_date DESC, c.side
        """,
        (tte_min, tte_max, entry_min, entry_max, spread_max, stop_bid, stop_bid, min_candidates),
    )
    return _tool_result({
        "params": {
            "entry_min": entry_min,
            "entry_max": entry_max,
            "tte_min": tte_min,
            "tte_max": tte_max,
            "spread_max": spread_max,
            "stop_bid": stop_bid,
            "min_candidates": min_candidates,
        },
        "row_count": len(rows),
        "rows": rows,
    })


TOOLS: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "readonly_sql": (
        "Run a single read-only SQL statement against the logger DB. SELECT/WITH/SHOW/DESCRIBE only.",
        {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": MAX_SQL_ROWS},
            },
            "required": ["sql"],
        },
        tool_readonly_sql,
    ),
    "schema_overview": (
        "Describe common logger tables or a supplied list of table names.",
        {
            "type": "object",
            "properties": {
                "tables": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                }
            },
        },
        tool_schema_overview,
    ),
    "live_summary": (
        "Summarize live momentum trades, P/L, exit reasons, and pause state.",
        {
            "type": "object",
            "properties": {"hours": {"type": "integer", "minimum": 1}},
        },
        tool_live_summary,
    ),
    "recent_live_trades": (
        "Return recent momentum_live_trades rows with execution and first-30s telemetry fields.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                "hours": {"type": "integer", "minimum": 1},
            },
        },
        tool_recent_live_trades,
    ),
    "guardrail_events": (
        "Return recent momentum live guardrail events.",
        {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "default": 24, "minimum": 1},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
        tool_guardrail_events,
    ),
    "filter_diagnostics": (
        "Return JSON filter diagnostics: baseline, pre-entry filters, early exits, and missing telemetry.",
        {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "minimum": 1},
                "source": {"type": "string", "enum": ["all", "live", "shadow_only"], "default": "all"},
                "min_sample": {"type": "integer", "default": 20, "minimum": 1},
            },
        },
        tool_filter_diagnostics,
    ),
    "shadow_summary": (
        "Summarize momentum_shadow_trades performance by exit profile.",
        {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200}},
        },
        tool_shadow_summary,
    ),
    "late_contract_sweep": (
        "Run the late high-price contract continuation sweep by day/side/distance.",
        {
            "type": "object",
            "properties": {
                "entry_min": {"type": "number", "default": 0.90},
                "entry_max": {"type": "number", "default": 0.92},
                "tte_min": {"type": "integer", "default": 180},
                "tte_max": {"type": "integer", "default": 240},
                "spread_max": {"type": "number", "default": 0.02},
                "stop_bid": {"type": "number", "default": 0.85},
                "min_candidates": {"type": "integer", "default": 1},
            },
        },
        tool_late_contract_sweep,
    ),
}


# ==============================================================================
# MCP protocol loop
# ==============================================================================

def _tools_list() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": name,
                "description": desc,
                "inputSchema": schema,
            }
            for name, (desc, schema, _handler) in TOOLS.items()
        ]
    }


def _resources_list() -> dict[str, Any]:
    return {
        "resources": [
            {
                "uri": "kalshi-logger://schema",
                "name": "Logger DB schema overview",
                "mimeType": "application/json",
                "description": "DESCRIBE output for common logger tables.",
            }
        ]
    }


def _resource_read(uri: str) -> dict[str, Any]:
    if uri != "kalshi-logger://schema":
        raise ValueError(f"unknown resource URI: {uri}")
    result = tool_schema_overview({})
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": result["content"][0]["text"],
            }
        ]
    }


def handle_request(request: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}

    try:
        if method == "initialize":
            return _ok_response(req_id, {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {}, "resources": {}},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _ok_response(req_id, _tools_list())
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in TOOLS:
                raise ValueError(f"unknown tool: {name}")
            return _ok_response(req_id, TOOLS[name][2](args))
        if method == "resources/list":
            return _ok_response(req_id, _resources_list())
        if method == "resources/read":
            return _ok_response(req_id, _resource_read(str(params.get("uri", ""))))
        if method == "ping":
            return _ok_response(req_id, {})
        raise ValueError(f"unsupported MCP method: {method}")
    except Exception as exc:
        return _error_response(req_id, -32603, str(exc))


def serve_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
        except Exception as exc:
            response = _error_response(None, -32700, f"invalid JSON-RPC request: {exc}")
        if response is not None:
            _print_response(response)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only MCP server for Kalshi logger data.")
    parser.add_argument("--check", action="store_true", help="validate startup and print available tools")
    args = parser.parse_args()
    if args.check:
        print(json.dumps({
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "tools": sorted(TOOLS),
        }, indent=2))
        return
    serve_stdio()


if __name__ == "__main__":
    main()
