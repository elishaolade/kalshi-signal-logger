#!/usr/bin/env python3
"""
research_tool.py — Minimal hypothesis registry runner for paper-only research.

V1 scope:
  - load frozen hypotheses from research/hypotheses.json
  - list available hypotheses
  - run SQL-backed hypotheses against MySQL
  - persist each run in research_runs
  - write a markdown report to reports/

This is intentionally small and boring. It does not change trading behavior,
place orders, or enable live trading.

Usage:
    python scripts/research_tool.py list
    python scripts/research_tool.py run pmc_no_075_080_5h_high
    python scripts/research_tool.py run-all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all, fetch_one, insert_and_get_id

_ROOT = Path(__file__).parent.parent
_REGISTRY_PATH = _ROOT / "research" / "hypotheses.json"
_REPORTS_DIR = _ROOT / "reports"
_RUNNER_KIND = "sql"
_W = 96


def _load_registry() -> dict[str, Any]:
    if not _REGISTRY_PATH.exists():
        raise FileNotFoundError(f"Registry not found: {_REGISTRY_PATH}")
    return json.loads(_REGISTRY_PATH.read_text())


def _hypotheses_by_key() -> dict[str, dict[str, Any]]:
    data = _load_registry()
    out: dict[str, dict[str, Any]] = {}
    for hyp in data.get("hypotheses", []):
        key = str(hyp["key"])
        if key in out:
            raise ValueError(f"Duplicate hypothesis key: {key}")
        out[key] = hyp
    return out


def _fmt(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _load_latest_run(hypothesis_key: str) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT *
        FROM research_runs
        WHERE hypothesis_key = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (hypothesis_key,),
    )


def _render_table(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not rows:
        return ["(no rows)"], ["_No rows returned._", ""]

    columns = list(rows[0].keys())
    widths = {
        col: max(len(col), max(len(_fmt(r.get(col))) for r in rows))
        for col in columns
    }

    header = "  " + "  ".join(f"{col:<{widths[col]}}" for col in columns)
    rule = "  " + "  ".join("-" * widths[col] for col in columns)
    body = [
        "  " + "  ".join(f"{_fmt(row.get(col)):<{widths[col]}}" for col in columns)
        for row in rows
    ]

    md = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---:" if all(isinstance(r.get(c), (int, float)) and r.get(c) is not None for r in rows) else ":---" for c in columns]) + " |",
    ]
    for row in rows:
        md.append("| " + " | ".join(_fmt(row.get(c)) for c in columns) + " |")
    md.append("")
    return [header, rule, *body], md


def _write_report(run_id: int, hypothesis: dict[str, Any], rows: list[dict[str, Any]]) -> Path:
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _REPORTS_DIR / f"research_{hypothesis['key']}_run{run_id}_{date.today().isoformat()}.md"

    latest = _load_latest_run(str(hypothesis["key"]))
    md: list[str] = [
        f"# Research Run — {hypothesis['name']}",
        "",
        "⚠ **PAPER-ONLY RESEARCH** — SQL-based frozen-hypothesis evaluation. No live trading.",
        "",
        f"- Hypothesis key: `{hypothesis['key']}`",
        f"- Status: `{hypothesis.get('status', 'unknown')}`",
        f"- Runner: `{_RUNNER_KIND}`",
        f"- Run id: `{run_id}`",
        f"- Generated: `{date.today().isoformat()}`",
        "",
        "## Hypothesis",
        "",
        hypothesis.get("description", "_No description provided._"),
        "",
        "## Reassess Thresholds",
        "",
        ", ".join(str(x) for x in hypothesis.get("reassess_at_trades", [])) or "_None recorded._",
        "",
        "## Query Result",
        "",
    ]
    _, md_table = _render_table(rows)
    md += md_table
    md += [
        "## Query",
        "",
        "```sql",
        hypothesis["sql"].rstrip(),
        "```",
        "",
    ]
    if latest is not None and int(latest["id"]) == run_id:
        md += [
            "## Notes",
            "",
            "This report is the latest stored run for this hypothesis.",
            "",
        ]

    out_path.write_text("\n".join(md))
    return out_path


def list_hypotheses() -> None:
    hypotheses = _hypotheses_by_key()
    print(f"\n{'═' * _W}")
    print("  Research Tool — Frozen Hypotheses")
    print(f"{'═' * _W}")
    for hyp in hypotheses.values():
        thresholds = hyp.get("reassess_at_trades", [])
        threshold_txt = ", ".join(str(x) for x in thresholds) if thresholds else "—"
        print(f"\n- {hyp['key']}")
        print(f"  name:   {hyp['name']}")
        print(f"  status: {hyp.get('status', 'unknown')}")
        print(f"  kind:   {hyp.get('kind', 'unknown')}")
        print(f"  review: {threshold_txt}")
        print(f"  desc:   {hyp.get('description', '')}")
    print()


def run_hypothesis(key: str, notes: str) -> int:
    hypotheses = _hypotheses_by_key()
    hyp = hypotheses.get(key)
    if hyp is None:
        raise KeyError(f"Unknown hypothesis key: {key}")
    if hyp.get("kind") != "sql":
        raise ValueError(f"Unsupported hypothesis kind for V1: {hyp.get('kind')}")

    sql = str(hyp["sql"]).strip()
    started_at = datetime.now().replace(microsecond=0)
    rows = fetch_all(sql)
    finished_at = datetime.now().replace(microsecond=0)
    query_sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()

    run_id = insert_and_get_id(
        """
        INSERT INTO research_runs (
            hypothesis_key, hypothesis_name, hypothesis_status, runner_kind,
            registry_path, query_text, query_sha256, sample_thresholds,
            result_rows, notes, started_at, finished_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            hyp["key"],
            hyp["name"],
            hyp.get("status", "unknown"),
            _RUNNER_KIND,
            str(_REGISTRY_PATH.relative_to(_ROOT)),
            sql,
            query_sha,
            json.dumps(hyp.get("reassess_at_trades", [])),
            json.dumps(rows, default=str),
            notes,
            started_at.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )

    console_rows, _ = _render_table(rows)
    print(f"\n{'═' * _W}")
    print(f"  Research Run #{run_id} — {hyp['name']}")
    print(f"{'═' * _W}")
    print(f"  key:     {hyp['key']}")
    print(f"  status:  {hyp.get('status', 'unknown')}")
    print(f"  kind:    {_RUNNER_KIND}")
    print(f"  query:   {query_sha[:12]}…")
    if notes:
        print(f"  notes:   {notes}")
    print()
    for line in console_rows:
        print(line)
    report_path = _write_report(run_id, hyp, rows)
    print(f"\nReport written → {report_path}\n")
    return run_id


def run_all(notes: str) -> None:
    hypotheses = _hypotheses_by_key()
    for key in hypotheses:
        run_hypothesis(key, notes)


def main() -> None:
    ap = argparse.ArgumentParser(description="Frozen hypothesis research runner")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List registry hypotheses")

    run_ap = sub.add_parser("run", help="Run one hypothesis")
    run_ap.add_argument("key", help="hypothesis key")
    run_ap.add_argument("--notes", default="", help="optional note stored with the run")

    run_all_ap = sub.add_parser("run-all", help="Run every registry hypothesis")
    run_all_ap.add_argument("--notes", default="", help="optional note stored with every run")

    args = ap.parse_args()

    if args.command == "list":
        list_hypotheses()
        return
    if args.command == "run":
        run_hypothesis(args.key, args.notes)
        return
    if args.command == "run-all":
        run_all(args.notes)
        return


if __name__ == "__main__":
    main()
