#!/usr/bin/env python3
"""
migrate_add_research_runs.py — Idempotent schema migration.

Creates the `research_runs` table used by scripts/research_tool.py to persist
frozen-hypothesis research runs. Each row stores the exact SQL snapshot and the
result rows returned at execution time, separate from live paper_trades and the
dedicated backtest tables.

Safe to re-run: uses CREATE TABLE IF NOT EXISTS, so existing tables are left
untouched.

Run once before the first research-tool run:
    python scripts/migrate_add_research_runs.py

PAPER-ONLY RESEARCH. No live trading is performed or enabled; this only stores
research metadata and query results.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one

_TABLE = "research_runs"

_CREATE = """
CREATE TABLE IF NOT EXISTS research_runs (
    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
    hypothesis_key      VARCHAR(120)  NOT NULL,
    hypothesis_name     VARCHAR(200)  NOT NULL,
    hypothesis_status   VARCHAR(40)   NOT NULL,
    runner_kind         VARCHAR(40)   NOT NULL,
    registry_path       VARCHAR(255)  NOT NULL,
    query_text          LONGTEXT      NULL,
    query_sha256        CHAR(64)      NULL,
    sample_thresholds   JSON          NULL,
    result_rows         JSON          NULL,
    notes               TEXT          NULL,
    started_at          DATETIME(3)   NOT NULL,
    finished_at         DATETIME(3)   NULL,
    created_at          TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_rr_hypothesis (hypothesis_key, created_at),
    INDEX idx_rr_created    (created_at)
)
"""


def _table_exists(table: str) -> bool:
    row = fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = %s
        """,
        (table,),
    )
    return bool(row and int(row["n"]) > 0)


def main() -> None:
    print("\nresearch_runs schema migration")
    print("=" * 48)
    print()

    existed = _table_exists(_TABLE)
    execute_query(_CREATE)

    if existed:
        print(f"  {_TABLE:18s} already existed — no change")
    else:
        print(f"  {_TABLE:18s} ✓ created")

    print("\nMigration complete.")
    print(
        "research_tool.py will store frozen-hypothesis query runs here,\n"
        "including the exact SQL text hash and the returned result rows.\n"
        "No paper trades are opened and no live trading is enabled.\n"
    )


if __name__ == "__main__":
    main()
