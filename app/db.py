import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Optional

import mysql.connector
from mysql.connector import Error as MySQLError
from mysql.connector.pooling import MySQLConnectionPool

from app.config import (
    LIVE_TRADING_ENABLED,
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
)

logger = logging.getLogger(__name__)

_POOL_SIZE      = 5
_RETRY_ATTEMPTS = 10
_RETRY_DELAY    = 3.0   # seconds between connection attempts

_pool: Optional[MySQLConnectionPool] = None
_pool_lock = threading.Lock()

# ── Connection pool ───────────────────────────────────────────────────────────

def _build_pool() -> MySQLConnectionPool:
    if LIVE_TRADING_ENABLED:
        raise RuntimeError(
            "LIVE_TRADING_ENABLED=true — this project is paper-only. "
            "Set LIVE_TRADING_ENABLED=false and restart."
        )

    config = dict(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        database=MYSQL_DATABASE,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        charset="utf8mb4",
        use_pure=True,
        autocommit=False,
    )

    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            pool = MySQLConnectionPool(
                pool_name="signal_logger",
                pool_size=_POOL_SIZE,
                **config,
            )
            logger.info(
                "MySQL pool ready — %s:%s/%s (size=%d)",
                MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE, _POOL_SIZE,
            )
            return pool
        except MySQLError as exc:
            logger.warning(
                "MySQL connection attempt %d/%d failed: %s — retrying in %.0fs",
                attempt, _RETRY_ATTEMPTS, exc, _RETRY_DELAY,
            )
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_DELAY)

    raise RuntimeError(
        f"Cannot connect to MySQL at {MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE} "
        f"after {_RETRY_ATTEMPTS} attempts"
    )


def get_pool() -> MySQLConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:           # re-check after acquiring lock
                _pool = _build_pool()
    return _pool


@contextmanager
def _connection():
    """Yield a connection from the pool; commit on success, rollback on error."""
    conn = get_pool().get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()   # returns connection to pool


# ── Internal helpers ──────────────────────────────────────────────────────────

def _as_dicts(cursor) -> list[dict[str, Any]]:
    cols = [col[0] for col in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# ── Public query helpers ──────────────────────────────────────────────────────

def execute_query(sql: str, params: tuple = ()) -> None:
    """Run an INSERT / UPDATE / DELETE with no return value."""
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            logger.debug("execute_query: %d row(s) affected", cur.rowcount)


def fetch_one(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    """Return the first matching row as a dict, or None."""
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [col[0] for col in cur.description]
            return dict(zip(cols, row))


def fetch_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Return all matching rows as a list of dicts."""
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return _as_dicts(cur)


def insert_and_get_id(sql: str, params: tuple = ()) -> int:
    """Execute an INSERT and return the auto-increment id of the new row."""
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row_id: int = cur.lastrowid
            logger.debug("insert_and_get_id → %d", row_id)
            return row_id
