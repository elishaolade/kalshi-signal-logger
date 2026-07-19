#!/usr/bin/env python3
"""
Export read-only table data from the current Kalshi logger MySQL database.

Examples:
    python scripts/export_table_data.py --table signals --format csv
    python scripts/export_table_data.py --table paper_trades --where "status = 'CLOSED'"
    python scripts/export_table_data.py --all --format jsonl --out-dir exports/latest
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ORDER_BY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\s+(ASC|DESC))?$", re.IGNORECASE)
_UNSAFE_WHERE_RE = re.compile(
    r"\b(alter|call|create|delete|drop|execute|grant|insert|load|lock|"
    r"optimize|replace|revoke|set|truncate|update)\b|;",
    re.IGNORECASE,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _validate_identifier(value: str, label: str) -> str:
    if not _IDENT_RE.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _validate_order_by(value: str | None) -> str | None:
    if value is None:
        return None
    order_by = value.strip()
    if not _ORDER_BY_RE.fullmatch(order_by):
        raise ValueError(f"invalid order-by expression: {value!r}")
    return order_by


def _validate_where(value: str | None) -> str | None:
    if value is None:
        return None
    where = value.strip()
    if not where:
        return None
    if _UNSAFE_WHERE_RE.search(where):
        raise ValueError("where clause contains unsafe SQL")
    return where


def list_tables() -> list[str]:
    rows = fetch_all("SHOW TABLES")
    tables: list[str] = []
    for row in rows:
        if row:
            tables.append(str(next(iter(row.values()))))
    return sorted(tables)


def export_rows(
    table: str,
    *,
    limit: int | None = None,
    where: str | None = None,
    order_by: str | None = None,
) -> list[dict[str, Any]]:
    table = _validate_identifier(table, "table")
    where = _validate_where(where)
    order_by = _validate_order_by(order_by)

    sql = f"SELECT * FROM `{table}`"
    params: tuple[Any, ...] = ()
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += " LIMIT %s"
        params = (max(1, int(limit)),)

    return fetch_all(sql, params)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows({k: _json_default(v) if v is not None else "" for k, v in row.items()} for row in rows)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=_json_default, sort_keys=True) + "\n")


def _write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, default=_json_default, indent=2, sort_keys=True)
        fh.write("\n")


def write_export(path: Path, rows: list[dict[str, Any]], fmt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        _write_csv(path, rows)
    elif fmt == "jsonl":
        _write_jsonl(path, rows)
    elif fmt == "json":
        _write_json(path, rows)
    else:
        raise ValueError(f"unsupported format: {fmt}")


def _output_path(out_dir: Path, table: str, fmt: str) -> Path:
    suffix = "jsonl" if fmt == "jsonl" else fmt
    return out_dir / f"{table}.{suffix}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export logger DB table data.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--table", help="Single table to export")
    target.add_argument("--all", action="store_true", help="Export every table")
    parser.add_argument("--format", choices=("csv", "jsonl", "json"), default="csv")
    parser.add_argument("--out-dir", default="exports", help="Directory for exported files")
    parser.add_argument("--limit", type=int, help="Maximum rows per table")
    parser.add_argument("--where", help="Optional WHERE clause for single-table exports")
    parser.add_argument("--order-by", help="Optional '<column> [ASC|DESC]' ordering")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.all and args.where:
        raise SystemExit("--where is only supported with --table")

    out_dir = Path(args.out_dir)
    tables = list_tables() if args.all else [_validate_identifier(args.table, "table")]

    for table in tables:
        rows = export_rows(
            table,
            limit=args.limit,
            where=args.where if not args.all else None,
            order_by=args.order_by,
        )
        path = _output_path(out_dir, table, args.format)
        write_export(path, rows, args.format)
        print(f"{table}: exported {len(rows)} row(s) -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
