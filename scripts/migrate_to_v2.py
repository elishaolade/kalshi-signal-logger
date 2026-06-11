#!/usr/bin/env python3
"""
migrate_to_v2.py — Migrate from schema v1 (signal-logger) to schema v2 (research-only).

What this script does
---------------------
Phase 1  Create v2 tables (markets, contracts, market_snapshots,
         contract_snapshots, market_metrics).
Phase 2  Migrate data:
           markets       → markets        (rename fields, add defaults)
           markets       → contracts      (generate YES + NO rows per market)
           btc_ticks     → market_snapshots
           contract_ticks→ contract_snapshots  (joined through market_snapshots)
Phase 3  Rename v1 tables to _archive_v1 (data preserved, not deleted).
Phase 4  Print verification counts.

What data is lost
-----------------
The following tables are NOT migrated. They are renamed to _archive_v1.
All rows in them remain on disk but are no longer part of the active schema.

  signals                            934 rows  — strategy signal firings
  paper_trades                       176 rows  — simulated trade P&L
  trade_snapshots                  2,470 rows  — per-tick trade state
  signal_observations                 10 rows  — watch-only signal outcomes
  dcvrb_observations                 624 rows  — DCVRB strategy observations
  repricing_discrepancy_events        30 rows  — burst repricing events
  repricing_discrepancy_runs           1 row   — run metadata
  contract_value_bounce_backtest_*    32 rows  — backtest results
  strategy_versions                    0 rows
  clc_reversal_observations            0 rows
  backtest_runs / backtest_trades      0 rows
  followthrough_backtest_*             0 rows
  post_move_continuation_*             0 rows
  hourly_range_markets                 0 rows
  hourly_range_observations            0 rows

Migrated data
-------------
  markets        746 rows  → markets (new)  +  contracts (2 × 746 = 1,492 rows)
  btc_ticks   130,215 rows → market_snapshots
  contract_ticks 260,430 rows → contract_snapshots

Safety
------
• Stop the logger container before running this script.
• All v1 tables are only renamed, never dropped. To reclaim disk space
  after verification, run the cleanup section at the bottom manually.
• Run with --dry-run to inspect counts without making any changes.

Usage
-----
  python scripts/migrate_to_v2.py
  python scripts/migrate_to_v2.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import mysql.connector

from app.config import (
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
)

# ── helpers ────────────────────────────────────────────────────────────────────

def _conn():
    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        database=MYSQL_DATABASE,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        charset="utf8mb4",
        autocommit=False,
    )


def _exec(cur, sql: str, params: tuple = (), *, echo: bool = False) -> int:
    if echo:
        preview = sql.strip().splitlines()[0][:120]
        print(f"    SQL: {preview}")
    cur.execute(sql, params)
    return cur.rowcount


def _count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM `{table}`")
    return cur.fetchone()[0]


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (MYSQL_DATABASE, table),
    )
    return cur.fetchone()[0] > 0


def _section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── phase 1: create v2 tables ──────────────────────────────────────────────────

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS markets (
    id              BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_id       VARCHAR(100)    NOT NULL,
    title           VARCHAR(255),
    market_type     VARCHAR(100),
    target_price    DECIMAL(18, 2),
    opens_at        DATETIME,
    closes_at       DATETIME,
    settles_at      DATETIME,
    status          VARCHAR(50),
    raw_payload     JSON,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_market_id (market_id)
)
;;;
CREATE TABLE IF NOT EXISTS contracts (
    id              BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_id       BIGINT          NOT NULL,
    contract_id     VARCHAR(100)    NOT NULL,
    side            VARCHAR(50)     NOT NULL,
    title           VARCHAR(255),
    status          VARCHAR(50),
    raw_payload     JSON,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_contract_id (contract_id),
    INDEX idx_contracts_market_id (market_id),
    CONSTRAINT fk_contracts_market
        FOREIGN KEY (market_id) REFERENCES markets (id)
        ON DELETE CASCADE ON UPDATE CASCADE
)
;;;
CREATE TABLE IF NOT EXISTS market_snapshots (
    id                      BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_id               BIGINT          NOT NULL,
    snapshot_sequence       BIGINT          NOT NULL,
    captured_at             DATETIME(3)     NOT NULL,
    btc_price               DECIMAL(18, 2)  NOT NULL,
    time_remaining_seconds  INT,
    source                  VARCHAR(100),
    raw_payload             JSON,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_ms_market_captured (market_id, captured_at),
    CONSTRAINT fk_ms_market
        FOREIGN KEY (market_id) REFERENCES markets (id)
        ON DELETE CASCADE ON UPDATE CASCADE
)
;;;
CREATE TABLE IF NOT EXISTS contract_snapshots (
    id                  BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_snapshot_id  BIGINT          NOT NULL,
    contract_id         BIGINT          NOT NULL,
    captured_at         DATETIME(3)     NOT NULL,
    last_price          DECIMAL(10, 4),
    bid_price           DECIMAL(10, 4)  NOT NULL,
    ask_price           DECIMAL(10, 4)  NOT NULL,
    spread              DECIMAL(10, 4),
    volume              BIGINT,
    raw_payload         JSON,
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_cs_contract_captured  (contract_id, captured_at),
    INDEX idx_cs_snapshot           (market_snapshot_id),
    CONSTRAINT fk_cs_snapshot
        FOREIGN KEY (market_snapshot_id) REFERENCES market_snapshots (id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_cs_contract
        FOREIGN KEY (contract_id) REFERENCES contracts (id)
        ON DELETE CASCADE ON UPDATE CASCADE
)
;;;
CREATE TABLE IF NOT EXISTS market_metrics (
    id                          BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    market_snapshot_id          BIGINT          NOT NULL,
    captured_at                 DATETIME(3)     NOT NULL,
    distance_from_target        DECIMAL(18, 2),
    distance_from_target_pct    DECIMAL(10, 6),
    is_above_target             BOOLEAN,
    btc_return_30s              DECIMAL(12, 8),
    btc_return_1m               DECIMAL(12, 8),
    btc_return_5m               DECIMAL(12, 8),
    btc_return_stddev_1m        DECIMAL(12, 8),
    btc_return_stddev_3m        DECIMAL(12, 8),
    btc_return_stddev_5m        DECIMAL(12, 8),
    btc_return_stddev_15m       DECIMAL(12, 8),
    created_at                  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_mm_snapshot   (market_snapshot_id),
    INDEX idx_mm_captured   (captured_at),
    CONSTRAINT fk_mm_snapshot
        FOREIGN KEY (market_snapshot_id) REFERENCES market_snapshots (id)
        ON DELETE CASCADE ON UPDATE CASCADE
)
"""


def phase1_create_tables(cur, dry_run: bool) -> None:
    _section("Phase 1 — Create v2 tables")
    for stmt in SCHEMA_V2.strip().split(";;;"):
        stmt = stmt.strip()
        if not stmt:
            continue
        table = stmt.split("(")[0].split()[-1]
        if _table_exists(cur, table):
            print(f"  ✓ {table} already exists — skipped")
            continue
        if dry_run:
            print(f"  [dry-run] would create: {table}")
        else:
            _exec(cur, stmt, echo=False)
            print(f"  ✓ created: {table}")


# ── phase 2: data migration ────────────────────────────────────────────────────

def phase2_migrate_markets(cur, dry_run: bool) -> int:
    """Migrate v1 markets → v2 markets."""
    _section("Phase 2a — Migrate markets")

    src_count = _count(cur, "markets")
    print(f"  source rows: {src_count}")

    sql = """
        INSERT INTO markets (market_id, title, market_type, target_price,
                             opens_at, closes_at, status)
        SELECT
            market_ticker,
            title,
            'binary',
            target_price,
            open_time,
            close_time,
            'settled'
        FROM markets
        ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP
    """
    # Note: this SELECT FROM markets is the v1 table since we haven't renamed yet.
    # This works because the INSERT is into the same table only if MySQL aliases
    # are used. Instead we'll do it differently — use a temp approach.
    # We INSERT from the OLD markets rows using the old column names.
    # Since we're altering the same table, we need a different strategy.
    # We'll RENAME first (done in phase3) and then migrate from _archive.
    # See phase2b instead.
    print("  (deferred — runs after archive rename in phase 3)")
    return 0


def phase2_migrate_markets_from_archive(cur, dry_run: bool) -> int:
    """Migrate from markets_archive_v1 → markets (new schema)."""
    _section("Phase 2a — Migrate markets from archive")

    src_count = _count(cur, "markets_archive_v1")
    print(f"  source rows (markets_archive_v1): {src_count}")

    sql = """
        INSERT IGNORE INTO markets
            (market_id, title, market_type, target_price,
             opens_at, closes_at, status, created_at)
        SELECT
            market_ticker,
            title,
            'binary',
            target_price,
            open_time,
            close_time,
            'settled',
            created_at
        FROM markets_archive_v1
    """
    if dry_run:
        print("  [dry-run] would insert markets rows")
        return src_count

    _exec(cur, sql, echo=True)
    inserted = _count(cur, "markets")
    print(f"  ✓ inserted: {inserted} markets")
    return inserted


def phase2_create_contracts(cur, dry_run: bool) -> int:
    """Generate YES + NO contract rows for every market."""
    _section("Phase 2b — Create contracts (YES + NO per market)")

    market_count = _count(cur, "markets")
    expected = market_count * 2
    print(f"  markets: {market_count}  →  expected contracts: {expected}")

    sql_yes = """
        INSERT IGNORE INTO contracts (market_id, contract_id, side, status)
        SELECT id, CONCAT(market_id, '_YES'), 'YES', 'settled'
        FROM markets
    """
    sql_no = """
        INSERT IGNORE INTO contracts (market_id, contract_id, side, status)
        SELECT id, CONCAT(market_id, '_NO'), 'NO', 'settled'
        FROM markets
    """
    if dry_run:
        print(f"  [dry-run] would insert {expected} contract rows")
        return expected

    _exec(cur, sql_yes, echo=True)
    _exec(cur, sql_no, echo=True)
    inserted = _count(cur, "contracts")
    print(f"  ✓ inserted: {inserted} contracts")
    return inserted


def phase2_migrate_snapshots(cur, dry_run: bool) -> int:
    """
    Migrate btc_ticks → market_snapshots.

    Join: btc_ticks.market_ticker → markets.market_id
    snapshot_sequence: ROW_NUMBER() per market ordered by recorded_at
    time_remaining_seconds: GREATEST(0, TIMESTAMPDIFF(SECOND, recorded_at, closes_at))
    """
    _section("Phase 2c — Migrate btc_ticks → market_snapshots")

    src_count = _count(cur, "btc_ticks_archive_v1")
    print(f"  source rows (btc_ticks_archive_v1): {src_count:,}")

    sql = """
        INSERT INTO market_snapshots
            (market_id, snapshot_sequence, captured_at, btc_price,
             time_remaining_seconds, source)
        SELECT
            m.id,
            ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY bt.recorded_at),
            bt.recorded_at,
            bt.btc_price,
            GREATEST(0, TIMESTAMPDIFF(SECOND, bt.recorded_at, m.closes_at)),
            bt.source
        FROM btc_ticks_archive_v1 bt
        JOIN markets m ON m.market_id = bt.market_ticker
        ORDER BY bt.recorded_at
    """
    if dry_run:
        print("  [dry-run] would insert market_snapshot rows")
        return src_count

    print("  inserting market_snapshots (this may take a moment)…")
    t0 = time.monotonic()
    _exec(cur, sql, echo=True)
    elapsed = time.monotonic() - t0
    inserted = _count(cur, "market_snapshots")
    print(f"  ✓ inserted: {inserted:,} market_snapshots  ({elapsed:.1f}s)")
    return inserted


def phase2_migrate_contract_snapshots(cur, dry_run: bool) -> int:
    """
    Migrate contract_ticks → contract_snapshots.

    Join path:
      contract_ticks  →  btc_ticks (same market_ticker + recorded_at)
                       →  market_snapshots (same market + captured_at)
                       →  contracts (market_id + side)
    """
    _section("Phase 2d — Migrate contract_ticks → contract_snapshots")

    src_count = _count(cur, "contract_ticks_archive_v1")
    print(f"  source rows (contract_ticks_archive_v1): {src_count:,}")

    sql = """
        INSERT INTO contract_snapshots
            (market_snapshot_id, contract_id, captured_at,
             last_price, bid_price, ask_price, spread, volume)
        SELECT
            ms.id,
            c.id,
            ct.recorded_at,
            ct.last_price,
            COALESCE(ct.bid_price, 0),
            COALESCE(ct.ask_price, 0),
            ct.spread,
            ct.volume
        FROM contract_ticks_archive_v1 ct
        JOIN markets m
            ON  m.market_id = ct.market_ticker
        JOIN market_snapshots ms
            ON  ms.market_id   = m.id
            AND ms.captured_at = ct.recorded_at
        JOIN contracts c
            ON  c.market_id = m.id
            AND c.side      = ct.side
        ORDER BY ct.recorded_at
    """
    if dry_run:
        print("  [dry-run] would insert contract_snapshot rows")
        return src_count

    print("  inserting contract_snapshots (this may take a moment)…")
    t0 = time.monotonic()
    _exec(cur, sql, echo=True)
    elapsed = time.monotonic() - t0
    inserted = _count(cur, "contract_snapshots")
    print(f"  ✓ inserted: {inserted:,} contract_snapshots  ({elapsed:.1f}s)")
    return inserted


# ── phase 3: archive v1 tables ─────────────────────────────────────────────────

# Tables that have data worth keeping as archive.
ARCHIVE_TABLES = [
    "markets",
    "btc_ticks",
    "contract_ticks",
    "signals",
    "paper_trades",
    "trade_snapshots",
    "signal_observations",
    "dcvrb_observations",
    "repricing_discrepancy_events",
    "repricing_discrepancy_runs",
    "contract_value_bounce_backtest_signals",
    "contract_value_bounce_backtest_runs",
    "strategy_versions",
    "clc_reversal_observations",
    "backtest_runs",
    "backtest_trades",
    "followthrough_backtest_runs",
    "followthrough_backtest_trades",
    "post_move_continuation_runs",
    "post_move_continuation_signals",
    "hourly_range_markets",
    "hourly_range_observations",
]


def phase3_archive_v1_tables(cur, dry_run: bool) -> None:
    _section("Phase 3 — Rename v1 tables to _archive_v1")
    for table in ARCHIVE_TABLES:
        archive_name = f"{table}_archive_v1"
        if not _table_exists(cur, table):
            print(f"  skip: {table} (does not exist)")
            continue
        if _table_exists(cur, archive_name):
            print(f"  skip: {table} (archive already exists)")
            continue
        if dry_run:
            print(f"  [dry-run] would rename: {table} → {archive_name}")
        else:
            _exec(cur, f"RENAME TABLE `{table}` TO `{archive_name}`")
            print(f"  ✓ renamed: {table} → {archive_name}")


# ── phase 4: verification ──────────────────────────────────────────────────────

def phase4_verify(cur) -> None:
    _section("Phase 4 — Verification")

    checks = [
        ("markets",            "markets_archive_v1",          "market_id", "market_ticker"),
        ("market_snapshots",   "btc_ticks_archive_v1",        None,        None),
        ("contract_snapshots", "contract_ticks_archive_v1",   None,        None),
    ]

    for new_table, old_table, new_col, old_col in checks:
        new_count = _count(cur, new_table) if _table_exists(cur, new_table) else "—"
        old_count = _count(cur, old_table) if _table_exists(cur, old_table) else "—"
        print(f"  {new_table:<25} {new_count:>8}   ←   {old_table:<35} {old_count:>8}")

    # Spot-check: every market_snapshot must have exactly 2 contract_snapshots.
    if _table_exists(cur, "contract_snapshots") and _table_exists(cur, "market_snapshots"):
        cur.execute("""
            SELECT COUNT(*) FROM (
                SELECT market_snapshot_id, COUNT(*) as n
                FROM contract_snapshots
                GROUP BY market_snapshot_id
                HAVING n <> 2
            ) bad
        """)
        bad = cur.fetchone()[0]
        status = "✓ OK" if bad == 0 else f"✗ {bad} snapshots have unexpected contract count"
        print(f"\n  Integrity check (each snapshot = 2 contracts): {status}")

    # Contracts check.
    if _table_exists(cur, "contracts") and _table_exists(cur, "markets"):
        markets_n  = _count(cur, "markets")
        contracts_n = _count(cur, "contracts")
        expected   = markets_n * 2
        status = "✓ OK" if contracts_n == expected else f"✗ expected {expected}, got {contracts_n}"
        print(f"  Contracts count ({markets_n} markets × 2): {status}")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate schema v1 → v2")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would happen without making any changes")
    args = ap.parse_args()

    if args.dry_run:
        print("\n⚠  DRY RUN — no changes will be made\n")

    conn = _conn()
    cur  = conn.cursor()

    try:
        print(f"\nConnected to {MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}")
        print("STOP the logger container before running this migration.\n")

        # ── Phase 3: archive v1 tables FIRST ──────────────────────────────────
        # Must run before Phase 1 so there are no name conflicts when creating
        # v2 tables (e.g. both v1 and v2 have a table called "markets").
        phase3_archive_v1_tables(cur, args.dry_run)
        if not args.dry_run:
            conn.commit()

        # ── Phase 1: create v2 tables (no conflicts now) ───────────────────────
        phase1_create_tables(cur, args.dry_run)
        if not args.dry_run:
            conn.commit()

        # ── Phase 2: migrate data ──────────────────────────────────────────────
        # In a dry-run the archive tables don't actually exist yet, so we
        # only attempt SQL that touches them when running for real.
        if args.dry_run:
            _section("Phase 2 — Data migration (dry-run summary)")
            print("  [dry-run] would migrate markets_archive_v1 → markets")
            print("  [dry-run] would create contracts (YES + NO per market)")
            print("  [dry-run] would migrate btc_ticks_archive_v1 → market_snapshots")
            print("  [dry-run] would migrate contract_ticks_archive_v1 → contract_snapshots")
        else:
            if _table_exists(cur, "markets_archive_v1"):
                phase2_migrate_markets_from_archive(cur, dry_run=False)
                conn.commit()
                phase2_create_contracts(cur, dry_run=False)
                conn.commit()

            if _table_exists(cur, "btc_ticks_archive_v1"):
                phase2_migrate_snapshots(cur, dry_run=False)
                conn.commit()

            if _table_exists(cur, "contract_ticks_archive_v1"):
                phase2_migrate_contract_snapshots(cur, dry_run=False)
                conn.commit()

        # ── Phase 4: verify ────────────────────────────────────────────────────
        phase4_verify(cur)

        _section("Done")
        if args.dry_run:
            print("  Dry run complete — no changes made.")
        else:
            print("  Migration complete.")
            print()
            print("  Archive tables (_archive_v1) are preserved on disk.")
            print("  After verifying the new schema, drop them with:")
            print()
            for t in ARCHIVE_TABLES:
                archive = f"{t}_archive_v1"
                print(f"    DROP TABLE IF EXISTS `{archive}`;")

    except Exception as exc:
        conn.rollback()
        print(f"\n✗ Migration failed: {exc}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
