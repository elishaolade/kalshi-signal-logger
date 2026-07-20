#!/usr/bin/env python3
"""
Summarize fast_rebound_test_trades TEST rows.

Read-only. Produces quick performance by exit model for the frozen target/stop
TEST tracker.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0].keys())
    print("\t".join(cols))
    for row in rows:
        print("\t".join("" if row.get(c) is None else str(row.get(c)) for c in cols))


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize fast rebound TEST rows")
    parser.add_argument("--profile", default="frozen_65_70_fast_rebound_reprice_9_15_target_stop_v1")
    parser.add_argument("--hours", type=int, default=None)
    args = parser.parse_args()

    time_filter = ""
    params: list[object] = [args.profile]
    if args.hours is not None:
        time_filter = "AND signal_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR"
        params.append(args.hours)

    summary = fetch_all(
        f"""
        SELECT
          exit_model,
          COUNT(*) AS trades,
          SUM(status='ACTIVE') AS active,
          SUM(status='COMPLETE') AS complete,
          SUM(exit_reason='target_hit') AS target_hits,
          SUM(exit_reason='stop_hit') AS stop_hits,
          SUM(exit_reason='timeout') AS timeouts,
          ROUND(100 * SUM(exit_reason='target_hit') / NULLIF(SUM(status='COMPLETE'), 0), 1) AS target_hit_pct,
          ROUND(AVG(gross_pnl_cents), 4) AS avg_gross_pnl_cents,
          ROUND(AVG(estimated_net_pnl_cents), 4) AS avg_est_net_pnl_cents,
          ROUND(SUM(estimated_net_pnl_cents), 4) AS total_est_net_pnl_cents,
          ROUND(
            SUM(CASE WHEN estimated_net_pnl_cents > 0 THEN estimated_net_pnl_cents ELSE 0 END) /
            NULLIF(-SUM(CASE WHEN estimated_net_pnl_cents < 0 THEN estimated_net_pnl_cents ELSE 0 END), 0),
            4
          ) AS est_net_profit_factor,
          ROUND(AVG(holding_seconds), 2) AS avg_holding_seconds
        FROM fast_rebound_test_trades
        WHERE profile = %s
          {time_filter}
        GROUP BY exit_model
        ORDER BY total_est_net_pnl_cents DESC
        """,
        tuple(params),
    )
    print("\nSUMMARY")
    _print_table(summary)

    recent = fetch_all(
        f"""
        SELECT
          id, signal_at, market_ticker, dominant_side, minority_side, exit_model,
          status, entry_ask, exit_bid, exit_reason, gross_pnl_cents,
          estimated_net_pnl_cents, holding_seconds,
          dominant_change_prev_30s_cents
        FROM fast_rebound_test_trades
        WHERE profile = %s
          {time_filter}
        ORDER BY id DESC
        LIMIT 30
        """,
        tuple(params),
    )
    print("\nRECENT")
    _print_table(recent)


if __name__ == "__main__":
    main()
