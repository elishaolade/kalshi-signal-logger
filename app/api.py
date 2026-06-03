from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import RESEARCH_API_MAX_ROWS, RESEARCH_API_TOKEN
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
