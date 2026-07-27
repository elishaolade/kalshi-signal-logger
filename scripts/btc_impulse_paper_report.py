#!/usr/bin/env python3
"""
Summarize the prospective 08:00-11:00 ET BTC impulse paper test.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all


def _avg(values: Iterable[float]) -> float | None:
    vals = list(values)
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _median(values: Iterable[float]) -> float | None:
    vals = list(values)
    if not vals:
        return None
    return round(statistics.median(vals), 4)


def _profit_factor(values: Iterable[float]) -> float | str | None:
    vals = list(values)
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    if losses == 0:
        return "inf" if gains > 0 else None
    return round(gains / losses, 4)


def _max_drawdown(values: list[float]) -> float | None:
    running = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return round(worst, 4)


def _pct(num: int, den: int) -> float | None:
    if den == 0:
        return None
    return round(100 * num / den, 1)


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0].keys())
    print("\t".join(cols))
    for row in rows:
        print("\t".join("" if row.get(c) is None else str(row.get(c)) for c in cols))


def _summary(rows: list[dict]) -> dict:
    completed = [row for row in rows if row["status"] == "COMPLETE" and row["net_cents"] is not None]
    nets = [float(row["net_cents"]) for row in completed]
    first15 = nets[:15]
    last15 = nets[-15:] if len(nets) >= 15 else nets
    excluding_largest_winner = list(nets)
    excluding_largest_loser = list(nets)
    if excluding_largest_winner:
        excluding_largest_winner.remove(max(excluding_largest_winner))
        excluding_largest_loser.remove(min(excluding_largest_loser))
    return {
        "days_observed": len({row["et_date"] for row in rows}),
        "days_with_trade": len({row["et_date"] for row in rows if row["status"] in ("ACTIVE", "COMPLETE", "NO_VALID_EXIT")}),
        "days_skipped": len({row["et_date"] for row in rows if row["status"] == "NO_TRADE"}),
        "completed_trades": len(completed),
        "active_trades": sum(row["status"] == "ACTIVE" for row in rows),
        "average_net_cents": _avg(nets),
        "median_net_cents": _median(nets),
        "total_net_cents": round(sum(nets), 4) if nets else None,
        "win_rate_pct": _pct(sum(v > 0 for v in nets), len(nets)),
        "profit_factor": _profit_factor(nets),
        "maximum_drawdown": _max_drawdown(nets),
        "first_15_trades_average_net": _avg(first15),
        "last_15_trades_average_net": _avg(last15),
        "largest_winner": max(nets) if nets else None,
        "largest_loser": min(nets) if nets else None,
        "average_net_excluding_largest_winner": _avg(excluding_largest_winner),
        "total_net_excluding_largest_winner": round(sum(excluding_largest_winner), 4) if excluding_largest_winner else None,
        "average_net_excluding_largest_loser": _avg(excluding_largest_loser),
        "total_net_excluding_largest_loser": round(sum(excluding_largest_loser), 4) if excluding_largest_loser else None,
        "test_complete_by_30_days_or_30_opportunities": int(len({row["et_date"] for row in rows}) >= 30 or len(completed) >= 30),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize BTC impulse paper test")
    parser.add_argument("--profile", default="frozen_08_11_btc_60s_abs50_aligned_120s_v1")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    rows = fetch_all(
        """
        SELECT *
        FROM btc_impulse_paper_trades
        WHERE profile=%s
        ORDER BY et_date, entry_at, id
        """,
        (args.profile,),
    )

    print("SUMMARY")
    _print_table([_summary(rows)])

    print("\nRECENT")
    recent = list(reversed(rows[-args.limit:]))
    _print_table(
        [
            {
                "id": row["id"],
                "et_date": row["et_date"],
                "status": row["status"],
                "market_ticker": row["market_ticker"],
                "trade_side": row["trade_side"],
                "entry_at_et": row["entry_at_et"],
                "exit_at_et": row["exit_at_et"],
                "btc_60s_move": row["btc_60s_move"],
                "entry_ask": row["entry_ask"],
                "exit_bid": row["exit_bid"],
                "gross_cents": row["gross_cents"],
                "fee_cents": row["fee_cents"],
                "net_cents": row["net_cents"],
                "running_total_net_cents": row["running_total_net_cents"],
                "running_drawdown_cents": row["running_drawdown_cents"],
                "skip_reason": row["skip_reason"],
            }
            for row in recent
        ]
    )


if __name__ == "__main__":
    main()
