#!/usr/bin/env python3
"""
btc_lead_lag_report.py - Read-only report for the BTC lead-lag live experiment.

The report intentionally separates:
  1. real-money live rows,
  2. shadow-only rows produced by the live trader,
  3. diagnostic-only rows.

Only real-money live rows should be used to decide whether the hypothesis is
tradable.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import config
from app.db import fetch_all, fetch_one


def _h(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _row(label: str, value) -> None:
    print(f"  {label:<36} {value}")


def _fmt(v: Optional[float], nd: int = 4, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{nd}f}" if signed else f"{v:.{nd}f}"


def _pf(gross_win: Optional[float], gross_loss: Optional[float]) -> str:
    if gross_win is None or gross_loss is None:
        return "n/a"
    if gross_loss == 0:
        return "inf" if gross_win > 0 else "n/a"
    return f"{gross_win / abs(gross_loss):.4f}"


def _where(profile: str, hours: Optional[int], extra: str = "") -> tuple[str, tuple]:
    clauses = ["COALESCE(strategy_name, exit_profile) = %s"]
    params: list = [profile]
    if hours is not None:
        clauses.append("signal_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR")
        params.append(hours)
    if extra:
        clauses.append(extra)
    return " AND ".join(clauses), tuple(params)


def _print_population(profile: str, hours: Optional[int], title: str, extra: str) -> None:
    where, params = _where(profile, hours, extra)
    row = fetch_one(
        f"""
        SELECT
          COUNT(*) AS trades,
          SUM(actual_profit_dollars > 0) AS wins,
          SUM(actual_profit_dollars <= 0) AS losses,
          ROUND(100 * SUM(actual_profit_dollars > 0) / NULLIF(COUNT(*), 0), 1) AS win_rate_pct,
          ROUND(AVG(actual_profit_dollars), 4) AS avg_pnl_dollars,
          ROUND(SUM(actual_profit_dollars), 4) AS total_pnl_dollars,
          ROUND(SUM(CASE WHEN actual_profit_dollars > 0 THEN actual_profit_dollars ELSE 0 END), 4) AS gross_win,
          ROUND(SUM(CASE WHEN actual_profit_dollars < 0 THEN actual_profit_dollars ELSE 0 END), 4) AS gross_loss,
          SUM(max_profit_cents >= 3.0) AS touched_plus_3c,
          ROUND(100 * SUM(max_profit_cents >= 3.0) / NULLIF(COUNT(*), 0), 1) AS touched_plus_3c_pct,
          ROUND(AVG(time_to_positive_1c_seconds), 2) AS avg_time_to_1c_s,
          ROUND(AVG(time_to_positive_2c_seconds), 2) AS avg_time_to_2c_s,
          ROUND(AVG(time_to_positive_3c_seconds), 2) AS avg_time_to_3c_s,
          ROUND(AVG(btc_lead_expected_edge_cents), 4) AS avg_expected_edge_cents,
          ROUND(AVG(btc_lead_btc_move_30s), 4) AS avg_btc_move_30s,
          ROUND(AVG(btc_lead_contract_move_30s_cents), 4) AS avg_contract_move_30s_cents
        FROM momentum_live_trades
        WHERE {where}
          AND status = 'COMPLETE'
          AND filled_contracts > 0
          AND actual_profit_dollars IS NOT NULL
        """,
        params,
    ) or {}

    _h(title)
    _row("Trades", row.get("trades") or 0)
    _row("Wins / losses", f"{row.get('wins') or 0} / {row.get('losses') or 0}")
    _row("Win rate", f"{row.get('win_rate_pct') or 0}%")
    _row("Total after-fee P/L", f"${_fmt(row.get('total_pnl_dollars'), 4, True)}")
    _row("Avg after-fee P/L / trade", f"${_fmt(row.get('avg_pnl_dollars'), 4, True)}")
    _row("Profit factor", _pf(row.get("gross_win"), row.get("gross_loss")))
    _row("+3c MFE touch rate", f"{row.get('touched_plus_3c_pct') or 0}%")
    _row("Touched +3c trades", row.get("touched_plus_3c") or 0)
    _row("Avg time to +1c / +2c / +3c", f"{_fmt(row.get('avg_time_to_1c_s'), 2)}s / {_fmt(row.get('avg_time_to_2c_s'), 2)}s / {_fmt(row.get('avg_time_to_3c_s'), 2)}s")
    _row("Avg expected edge", f"{_fmt(row.get('avg_expected_edge_cents'), 4)}c")
    _row("Avg BTC move 30s", f"${_fmt(row.get('avg_btc_move_30s'), 2)}")
    _row("Avg Kalshi ask move 30s", f"{_fmt(row.get('avg_contract_move_30s_cents'), 4)}c")


def _print_breakdown(profile: str, hours: Optional[int]) -> None:
    where, params = _where(
        profile,
        hours,
        "COALESCE(shadow_only, 0) = 0 AND COALESCE(diagnostic_mode, 0) = 0",
    )
    rows = fetch_all(
        f"""
        SELECT
          exit_reason,
          COUNT(*) AS trades,
          SUM(actual_profit_dollars > 0) AS wins,
          SUM(actual_profit_dollars <= 0) AS losses,
          ROUND(AVG(actual_profit_dollars), 4) AS avg_pnl_dollars,
          ROUND(SUM(actual_profit_dollars), 4) AS total_pnl_dollars,
          ROUND(AVG(btc_lead_expected_edge_cents), 4) AS avg_expected_edge_cents
        FROM momentum_live_trades
        WHERE {where}
          AND status = 'COMPLETE'
          AND filled_contracts > 0
          AND actual_profit_dollars IS NOT NULL
        GROUP BY exit_reason
        ORDER BY total_pnl_dollars ASC
        """,
        params,
    )
    _h("Real-Money Exit Breakdown")
    if not rows:
        print("  No completed real-money rows found.")
        return
    print("  exit_reason        trades  wins  losses   avg_pnl   total_pnl  avg_edge")
    for r in rows:
        print(
            f"  {str(r.get('exit_reason') or 'NULL'):<17}"
            f"{int(r.get('trades') or 0):>7}"
            f"{int(r.get('wins') or 0):>6}"
            f"{int(r.get('losses') or 0):>8}"
            f"{_fmt(r.get('avg_pnl_dollars'), 4, True):>10}"
            f"{_fmt(r.get('total_pnl_dollars'), 4, True):>12}"
            f"{_fmt(r.get('avg_expected_edge_cents'), 4):>10}"
        )


def _print_recent(profile: str, hours: Optional[int], limit: int) -> None:
    where, params = _where(profile, hours)
    rows = fetch_all(
        f"""
        SELECT
          id, signal_at, market_ticker, side, status, exit_reason,
          filled_contracts, actual_entry_price, actual_exit_price,
          actual_profit_dollars, max_profit_cents,
          time_to_positive_1c_seconds, time_to_positive_2c_seconds,
          time_to_positive_3c_seconds,
          btc_lead_expected_edge_cents, btc_lead_btc_move_30s,
          btc_lead_contract_move_30s_cents,
          COALESCE(shadow_only, 0) AS shadow_only,
          COALESCE(diagnostic_mode, 0) AS diagnostic_mode
        FROM momentum_live_trades
        WHERE {where}
        ORDER BY signal_at DESC
        LIMIT %s
        """,
        params + (limit,),
    )
    _h("Recent BTC Lead-Lag Rows")
    if not rows:
        print("  No rows found.")
        return
    print("  id   signal_at              mode       side status     exit_reason     pnl     mfe   edge  btc30  k30")
    for r in rows:
        mode = "shadow" if int(r.get("shadow_only") or 0) else "diag" if int(r.get("diagnostic_mode") or 0) else "live"
        print(
            f"  {int(r['id']):<4} {r['signal_at']}  {mode:<9}"
            f"{str(r.get('side') or ''):<5}{str(r.get('status') or ''):<11}"
            f"{str(r.get('exit_reason') or ''):<14}"
            f"{_fmt(r.get('actual_profit_dollars'), 4, True):>8}"
            f"{_fmt(r.get('max_profit_cents'), 2):>7}"
            f"{_fmt(r.get('btc_lead_expected_edge_cents'), 2):>7}"
            f"{_fmt(r.get('btc_lead_btc_move_30s'), 1):>7}"
            f"{_fmt(r.get('btc_lead_contract_move_30s_cents'), 2):>6}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=config.MOMENTUM_BTC_LEAD_PROFILE or "btc_lead_lag_v1")
    parser.add_argument("--hours", type=int, default=None)
    parser.add_argument("--recent", type=int, default=20)
    args = parser.parse_args()

    print("BTC Lead-Lag Experiment Report")
    print(f"profile={args.profile} hours={args.hours if args.hours is not None else 'all'}")
    print("Only real-money live rows determine whether the hypothesis is supported.")

    live_extra = "COALESCE(shadow_only, 0) = 0 AND COALESCE(diagnostic_mode, 0) = 0"
    shadow_extra = "COALESCE(shadow_only, 0) = 1"
    diagnostic_extra = "COALESCE(diagnostic_mode, 0) = 1"
    _print_population(args.profile, args.hours, "Live Real-Money Trades", live_extra)
    _print_population(args.profile, args.hours, "Shadow-Only Rows", shadow_extra)
    _print_population(args.profile, args.hours, "Diagnostic-Only Rows", diagnostic_extra)
    _print_breakdown(args.profile, args.hours)
    _print_recent(args.profile, args.hours, args.recent)


if __name__ == "__main__":
    main()
