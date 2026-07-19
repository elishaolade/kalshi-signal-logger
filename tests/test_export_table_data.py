from __future__ import annotations

import csv
import json
from datetime import datetime
from decimal import Decimal

import pytest

from scripts import export_table_data as exporter


class TestValidation:
    def test_rejects_invalid_table_names(self):
        with pytest.raises(ValueError):
            exporter.export_rows("signals; DROP TABLE signals")

    @pytest.mark.parametrize(
        "where",
        [
            "status = 'CLOSED'; DELETE FROM paper_trades",
            "id IN (SELECT id FROM signals);",
            "UPDATE signals SET signal_status = 'x'",
        ],
    )
    def test_rejects_unsafe_where_clauses(self, where):
        with pytest.raises(ValueError):
            exporter._validate_where(where)

    def test_allows_simple_where_clause(self):
        assert exporter._validate_where("status = 'CLOSED'") == "status = 'CLOSED'"

    @pytest.mark.parametrize("order_by", ["recorded_at DESC", "id", "entry_time ASC"])
    def test_allows_simple_order_by(self, order_by):
        assert exporter._validate_order_by(order_by) == order_by

    @pytest.mark.parametrize("order_by", ["id; DELETE FROM x", "id, recorded_at", "LOWER(name)"])
    def test_rejects_complex_order_by(self, order_by):
        with pytest.raises(ValueError):
            exporter._validate_order_by(order_by)


class TestExportRows:
    def test_builds_read_query_with_limit_where_and_order(self, monkeypatch):
        calls = []

        def fake_fetch_all(sql, params=()):
            calls.append((sql, params))
            return [{"id": 1}]

        monkeypatch.setattr(exporter, "fetch_all", fake_fetch_all)
        rows = exporter.export_rows(
            "paper_trades",
            where="status = 'CLOSED'",
            order_by="entry_time DESC",
            limit=10,
        )

        assert rows == [{"id": 1}]
        assert calls == [(
            "SELECT * FROM `paper_trades` WHERE status = 'CLOSED' ORDER BY entry_time DESC LIMIT %s",
            (10,),
        )]


class TestWriters:
    def test_writes_jsonl_with_db_types(self, tmp_path):
        path = tmp_path / "signals.jsonl"
        exporter.write_export(
            path,
            [{"id": 1, "price": Decimal("0.9300"), "recorded_at": datetime(2026, 7, 19, 1, 2, 3)}],
            "jsonl",
        )

        assert json.loads(path.read_text()) == {
            "id": 1,
            "price": 0.93,
            "recorded_at": "2026-07-19T01:02:03",
        }

    def test_writes_csv_with_headers(self, tmp_path):
        path = tmp_path / "signals.csv"
        exporter.write_export(path, [{"id": 1, "price": Decimal("0.5000")}], "csv")

        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        assert rows == [{"id": "1", "price": "0.5"}]
