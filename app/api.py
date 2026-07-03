from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app import momentum_filter_shadow as filter_shadow
from app.config import (
    RESEARCH_API_MAX_ROWS,
    RESEARCH_API_PUBLIC_BASE_URL,
    RESEARCH_API_TOKEN,
)
from app.db import fetch_all, fetch_one
from scripts import research_tool

app = FastAPI(
    title="Kalshi Signal Logger Research API",
    version="0.1.0",
    description="Paper-only research/query interface for frozen hypotheses and read-only SQL access.",
)

_DANGEROUS_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|GRANT|REVOKE|CALL|OPTIMIZE|RENAME)\b",
    re.IGNORECASE,
)
_ALLOWED_SQL_PREFIXES = ("SELECT", "WITH", "SHOW", "EXPLAIN", "DESCRIBE")
_ROOT = Path(__file__).parent.parent
_REPORTS_DIR = _ROOT / "reports"


class SQLQueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=12000)
    max_rows: int = Field(default=100, ge=1, le=1000)


def _require_token(x_api_key: Optional[str] = Header(default=None)) -> None:
    if not RESEARCH_API_TOKEN:
        raise HTTPException(status_code=503, detail="RESEARCH_API_TOKEN is not configured")
    if x_api_key != RESEARCH_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _validate_sql(sql: str) -> str:
    cleaned = sql.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="SQL cannot be empty")
    upper = cleaned.lstrip().upper()
    if not upper.startswith(_ALLOWED_SQL_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail="Only read-only SELECT/WITH/SHOW/EXPLAIN/DESCRIBE statements are allowed",
        )
    if _DANGEROUS_SQL.search(cleaned):
        raise HTTPException(status_code=400, detail="Dangerous SQL keyword detected")
    return cleaned


def _hours_filter(hours: Optional[int], column: str = "signal_at") -> tuple[str, tuple[Any, ...]]:
    if hours is None:
        return "", ()
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    return f" AND {column} >= %s", (start,)


def _limit(value: int, maximum: int = 200) -> int:
    return max(1, min(int(value), maximum))


def _normalize_filter_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    classified = {
        filter_shadow.OUTCOME_PROFIT_TARGET,
        filter_shadow.OUTCOME_STOP_LOSS,
        filter_shadow.OUTCOME_FIXED_TIME,
    }
    return [
        filter_shadow.normalize_trade(row)
        for row in rows
        if row.get("exit_reason") in classified
    ]


def _missing_count(rows: list[dict[str, Any]], field: str) -> int:
    return sum(1 for row in rows if row.get(field) is None)


def _latest_report_path_for_key(hypothesis_key: str) -> Optional[Path]:
    matches = sorted(_REPORTS_DIR.glob(f"research_{hypothesis_key}_run*.md"))
    return matches[-1] if matches else None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "paper-only"}


@app.get("/research/hypotheses")
def list_hypotheses(_: None = Depends(_require_token)) -> dict[str, Any]:
    hypotheses = list(research_tool._hypotheses_by_key().values())
    return {"count": len(hypotheses), "hypotheses": hypotheses}


@app.post("/research/run/{key}")
def run_hypothesis(
    key: str,
    notes: str = Query(default="", max_length=500),
    _: None = Depends(_require_token),
) -> dict[str, Any]:
    try:
        run_id = research_tool.run_hypothesis(key, notes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    latest = fetch_one("SELECT * FROM research_runs WHERE id = %s", (run_id,))
    report_path = _latest_report_path_for_key(key)
    return {
        "run_id": run_id,
        "hypothesis_key": key,
        "report_path": str(report_path) if report_path else None,
        "run": latest,
    }


@app.get("/research/runs/latest")
def latest_runs(
    key: Optional[str] = Query(default=None),
    _: None = Depends(_require_token),
) -> dict[str, Any]:
    if key:
        row = fetch_one(
            """
            SELECT *
            FROM research_runs
            WHERE hypothesis_key = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (key,),
        )
        return {"run": row}

    rows = fetch_all(
        """
        SELECT rr.*
        FROM research_runs rr
        JOIN (
          SELECT hypothesis_key, MAX(id) AS max_id
          FROM research_runs
          GROUP BY hypothesis_key
        ) latest
          ON latest.hypothesis_key = rr.hypothesis_key
         AND latest.max_id = rr.id
        ORDER BY rr.hypothesis_key
        """
    )
    return {"count": len(rows), "runs": rows}


@app.get("/research/report/{key}")
def latest_report(
    key: str,
    _: None = Depends(_require_token),
) -> dict[str, Any]:
    report = _latest_report_path_for_key(key)
    if report is None or not report.exists():
        raise HTTPException(status_code=404, detail=f"No report found for hypothesis {key}")
    return {
        "hypothesis_key": key,
        "path": str(report),
        "content": report.read_text(),
    }


@app.get("/paper-trades/summary")
def paper_trade_summary(_: None = Depends(_require_token)) -> dict[str, Any]:
    rows = fetch_all(
        """
        SELECT
          rule_name,
          rule_version,
          side,
          COUNT(*) AS trades,
          ROUND(AVG(pnl > 0) * 100, 1) AS win_rate_pct,
          ROUND(SUM(pnl), 4) AS total_pnl,
          ROUND(AVG(pnl), 4) AS avg_pnl,
          ROUND(
            SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) /
            NULLIF(ABS(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)), 0),
            2
          ) AS profit_factor,
          MAX(exit_time) AS latest_exit
        FROM paper_trades
        WHERE status = 'CLOSED'
          AND followed_rules = TRUE
          AND pnl IS NOT NULL
        GROUP BY rule_name, rule_version, side
        ORDER BY avg_pnl DESC
        """
    )
    return {"count": len(rows), "rows": rows}


@app.post("/query/sql")
def query_sql(
    req: SQLQueryRequest,
    _: None = Depends(_require_token),
) -> dict[str, Any]:
    sql = _validate_sql(req.sql)
    rows = fetch_all(sql)
    limited = rows[: min(req.max_rows, RESEARCH_API_MAX_ROWS)]
    return {
        "row_count": len(limited),
        "truncated": len(rows) > len(limited),
        "rows": limited,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ChatGPT Actions surface
# ══════════════════════════════════════════════════════════════════════════════
#
# These endpoints are deliberately narrower than /query/sql. They are safe to
# expose to a Custom GPT behind x-api-key auth because they do not accept generic
# SQL and they do not mutate database or trading state.


@app.get("/chatgpt/logger/live-summary")
def chatgpt_live_summary(
    hours: Optional[int] = Query(default=None, ge=1, le=24 * 30),
    _: None = Depends(_require_token),
) -> dict[str, Any]:
    time_sql, params = _hours_filter(hours)
    status_rows = fetch_all(
        f"""
        SELECT status, COUNT(*) AS n
        FROM momentum_live_trades
        WHERE 1=1 {time_sql}
        GROUP BY status
        ORDER BY status
        """,
        params,
    )
    completed = fetch_one(
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
    exits = fetch_all(
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
    pause_state = None
    try:
        pause_state = fetch_one("SELECT * FROM momentum_live_pause_state WHERE id=1")
    except Exception as exc:
        pause_state = {"error": str(exc)}
    return {
        "hours": hours,
        "status_breakdown": status_rows,
        "completed_summary": completed,
        "exit_reason_breakdown": exits,
        "pause_state": pause_state,
    }


@app.get("/chatgpt/logger/recent-live-trades")
def chatgpt_recent_live_trades(
    limit: int = Query(default=20, ge=1, le=100),
    hours: Optional[int] = Query(default=None, ge=1, le=24 * 30),
    _: None = Depends(_require_token),
) -> dict[str, Any]:
    row_limit = _limit(limit, 100)
    time_sql, params = _hours_filter(hours)
    rows = fetch_all(
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
          max_profit_first_30s_cents, min_profit_first_30s_cents,
          time_to_first_green_seconds, time_to_negative_1c_seconds,
          time_to_negative_2c_seconds, time_to_stop_threshold_seconds
        FROM momentum_live_trades
        WHERE 1=1 {time_sql}
        ORDER BY signal_at DESC
        LIMIT %s
        """,
        params + (row_limit,),
    )
    return {"hours": hours, "row_count": len(rows), "rows": rows}


@app.get("/chatgpt/logger/guardrail-events")
def chatgpt_guardrail_events(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=50, ge=1, le=100),
    _: None = Depends(_require_token),
) -> dict[str, Any]:
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    row_limit = _limit(limit, 100)
    rows = fetch_all(
        """
        SELECT id, created_at, market_ticker, side, event_type, reason
        FROM momentum_live_guardrail_events
        WHERE created_at >= %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (start, row_limit),
    )
    return {"hours": hours, "row_count": len(rows), "rows": rows}


@app.get("/chatgpt/logger/filter-diagnostics")
def chatgpt_filter_diagnostics(
    hours: Optional[int] = Query(default=None, ge=1, le=24 * 30),
    source: str = Query(default="all", pattern="^(all|live|shadow_only)$"),
    min_sample: int = Query(default=20, ge=1, le=10000),
    _: None = Depends(_require_token),
) -> dict[str, Any]:
    clauses = ["status = 'COMPLETE'", "exit_reason IS NOT NULL"]
    params: list[Any] = []
    if hours is not None:
        clauses.append("signal_at >= %s")
        params.append(datetime.now(timezone.utc) - timedelta(hours=hours))
    if source == "shadow_only":
        clauses.append("shadow_only = 1")
    elif source == "live":
        clauses.append("(shadow_only IS NULL OR shadow_only = 0)")

    rows = fetch_all(
        f"SELECT * FROM momentum_live_trades WHERE {' AND '.join(clauses)} ORDER BY signal_at DESC",
        tuple(params),
    )
    trades = _normalize_filter_rows(rows)
    pre_entry = []
    for candidate in filter_shadow.default_pre_entry_candidates():
        summary = filter_shadow.summarize_pre_entry(trades, candidate)
        summary["promising"] = filter_shadow.is_promising(summary, min_sample=min_sample)
        pre_entry.append(summary)

    early_exit = []
    for candidate in filter_shadow.default_early_exit_candidates():
        summary = filter_shadow.summarize_early_exit(trades, candidate)
        summary["promising"] = filter_shadow.is_promising(summary, min_sample=min_sample)
        early_exit.append(summary)

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
    return {
        "hours": hours,
        "source": source,
        "raw_rows": len(rows),
        "classified_rows": len(trades),
        "baseline": filter_shadow.baseline_performance(trades),
        "pre_entry": pre_entry,
        "early_exit": early_exit,
        "missing_telemetry": missing,
        "unit_note": "P/L values are TRUE cents where 3.0 = 3 cents.",
    }


@app.get("/chatgpt/logger/late-contract-sweep")
def chatgpt_late_contract_sweep(
    entry_min: float = Query(default=0.90, ge=0.01, le=0.99),
    entry_max: float = Query(default=0.92, ge=0.01, le=0.99),
    tte_min: int = Query(default=180, ge=0, le=900),
    tte_max: int = Query(default=240, ge=1, le=900),
    spread_max: float = Query(default=0.02, ge=0, le=0.20),
    stop_bid: float = Query(default=0.85, ge=0.01, le=0.99),
    min_candidates: int = Query(default=1, ge=1, le=1000),
    _: None = Depends(_require_token),
) -> dict[str, Any]:
    if entry_min > entry_max:
        raise HTTPException(status_code=400, detail="entry_min must be <= entry_max")
    if tte_min > tte_max:
        raise HTTPException(status_code=400, detail="tte_min must be <= tte_max")

    rows = fetch_all(
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
    return {
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
    }


def _chatgpt_json_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": True,
                }
            }
        },
    }


@app.get("/chatgpt/openapi.json", include_in_schema=False)
def chatgpt_openapi() -> dict[str, Any]:
    server_url = RESEARCH_API_PUBLIC_BASE_URL or "https://YOUR_PUBLIC_LOGGER_API_HOST"
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Kalshi Logger ChatGPT Actions",
            "version": "0.1.0",
            "description": (
                "Read-only logger diagnostics for live trades, filter telemetry, "
                "guardrails, and late-contract research. No trading actions."
            ),
        },
        "servers": [{"url": server_url}],
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "x-api-key",
                }
            }
        },
        "security": [{"ApiKeyAuth": []}],
        "paths": {
            "/chatgpt/logger/live-summary": {
                "get": {
                    "operationId": "getLiveSummary",
                    "summary": "Summarize live momentum trading performance.",
                    "parameters": [
                        {"name": "hours", "in": "query", "required": False,
                         "schema": {"type": "integer", "minimum": 1, "maximum": 720}}
                    ],
                    "responses": {"200": _chatgpt_json_response("Live summary")},
                }
            },
            "/chatgpt/logger/recent-live-trades": {
                "get": {
                    "operationId": "getRecentLiveTrades",
                    "summary": "Return recent live trades and first-30s telemetry.",
                    "parameters": [
                        {"name": "limit", "in": "query", "required": False,
                         "schema": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}},
                        {"name": "hours", "in": "query", "required": False,
                         "schema": {"type": "integer", "minimum": 1, "maximum": 720}},
                    ],
                    "responses": {"200": _chatgpt_json_response("Recent trades")},
                }
            },
            "/chatgpt/logger/guardrail-events": {
                "get": {
                    "operationId": "getGuardrailEvents",
                    "summary": "Return recent live guardrail/blocker events.",
                    "parameters": [
                        {"name": "hours", "in": "query", "required": False,
                         "schema": {"type": "integer", "default": 24, "minimum": 1, "maximum": 720}},
                        {"name": "limit", "in": "query", "required": False,
                         "schema": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100}},
                    ],
                    "responses": {"200": _chatgpt_json_response("Guardrail events")},
                }
            },
            "/chatgpt/logger/filter-diagnostics": {
                "get": {
                    "operationId": "getFilterDiagnostics",
                    "summary": "Return pre-entry and first-30s filter diagnostics.",
                    "parameters": [
                        {"name": "hours", "in": "query", "required": False,
                         "schema": {"type": "integer", "minimum": 1, "maximum": 720}},
                        {"name": "source", "in": "query", "required": False,
                         "schema": {"type": "string", "enum": ["all", "live", "shadow_only"], "default": "all"}},
                        {"name": "min_sample", "in": "query", "required": False,
                         "schema": {"type": "integer", "default": 20, "minimum": 1, "maximum": 10000}},
                    ],
                    "responses": {"200": _chatgpt_json_response("Filter diagnostics")},
                }
            },
            "/chatgpt/logger/late-contract-sweep": {
                "get": {
                    "operationId": "runLateContractSweep",
                    "summary": "Run the late high-price contract continuation research sweep.",
                    "parameters": [
                        {"name": "entry_min", "in": "query", "required": False,
                         "schema": {"type": "number", "default": 0.90}},
                        {"name": "entry_max", "in": "query", "required": False,
                         "schema": {"type": "number", "default": 0.92}},
                        {"name": "tte_min", "in": "query", "required": False,
                         "schema": {"type": "integer", "default": 180}},
                        {"name": "tte_max", "in": "query", "required": False,
                         "schema": {"type": "integer", "default": 240}},
                        {"name": "spread_max", "in": "query", "required": False,
                         "schema": {"type": "number", "default": 0.02}},
                        {"name": "stop_bid", "in": "query", "required": False,
                         "schema": {"type": "number", "default": 0.85}},
                        {"name": "min_candidates", "in": "query", "required": False,
                         "schema": {"type": "integer", "default": 1}},
                    ],
                    "responses": {"200": _chatgpt_json_response("Late-contract sweep")},
                }
            },
        },
    }
