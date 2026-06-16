#!/usr/bin/env python3
"""
momentum_backtest.py — CLI entry point for the momentum-repricing backtest.

Hypothesis
----------
When Bitcoin moves materially toward a contract's winning side over a short
interval (lookback_seconds), but the Kalshi contract has not repriced
proportionally, the contract may continue repricing in that direction over
the following seconds.

This is a hypothesis to test, not an established fact.  Do not report a
profitable edge unless results demonstrate positive out-of-sample expectancy
after fees and slippage.

Usage
-----
    # Full run with config file:
    python scripts/momentum_backtest.py --config experiments/momentum-lag.json

    # Dry run (no DB writes, still exports CSV/JSON):
    python scripts/momentum_backtest.py --config experiments/momentum-lag.json --dry-run

    # Limit to N markets (smoke test):
    python scripts/momentum_backtest.py --config experiments/momentum-lag.json --limit-markets 5

    # Date-filtered:
    python scripts/momentum_backtest.py \\
        --config experiments/momentum-lag.json \\
        --start 2026-05-01 --end 2026-06-01

    # Export to custom paths:
    python scripts/momentum_backtest.py \\
        --config experiments/momentum-lag.json \\
        --csv-out results/trades.csv \\
        --json-out results/summary.json

Requirements
------------
- Run the migration first:
    python scripts/migrate_add_momentum_backtest.py
- Backtest requires data in the v2 schema tables (market_snapshots,
  contract_snapshots, market_metrics, markets, contracts).
  If your data is still in the v1 tables, run:
    python scripts/migrate_to_v2.py

PAPER-ONLY RESEARCH.  No live trades are placed.  No order API is called.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import execute_query, fetch_one, insert_and_get_id
from backtest.config import ExperimentConfig, config_to_dict, load_config
from backtest.exporter import export_summary_json, export_trades_csv
from backtest.executor import TradeResult, simulate_exits
from backtest.loader import MarketInfo, load_all_markets, load_market_rows
from backtest.metrics import compute_summary
from backtest.signals import Signal, iter_signals
from backtest.validator import validate_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("momentum_backtest")


# ── Git commit helper ─────────────────────────────────────────────────────────

def _git_commit() -> Optional[str]:
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


# ── DB write helpers ──────────────────────────────────────────────────────────

def _create_experiment(cfg: ExperimentConfig, notes: str) -> int:
    """Insert a momentum_bt_experiments row and return its id."""
    return insert_and_get_id(
        """
        INSERT INTO momentum_bt_experiments
            (name, description, hypothesis, configuration_json,
             status, git_commit, notes)
        VALUES (%s, %s, %s, %s, 'running', %s, %s)
        """,
        (
            cfg.name,
            cfg.description or None,
            cfg.hypothesis  or None,
            json.dumps(config_to_dict(cfg)),
            _git_commit(),
            notes or None,
        ),
    )


def _finish_experiment(
    experiment_id: int,
    data_start:    Optional[datetime],
    data_end:      Optional[datetime],
    status:        str = "complete",
) -> None:
    execute_query(
        """
        UPDATE momentum_bt_experiments
        SET status = %s, completed_at = %s,
            data_start_at = %s, data_end_at = %s
        WHERE id = %s
        """,
        (status, datetime.now(timezone.utc), data_start, data_end, experiment_id),
    )


def _insert_trade(experiment_id: int, t: TradeResult, split_label: str) -> None:
    meta = dict(t.metadata)
    meta["split"] = split_label
    execute_query(
        """
        INSERT INTO momentum_bt_trades (
            experiment_id, market_id, contract_id, contract_side,
            exit_profile, holding_seconds_config, profit_target_cfg,
            signal_at, entry_at, entry_ask, entry_bid, entry_spread,
            exit_at, exit_bid, exit_reason, holding_seconds,
            btc_price_at_entry, target_price, distance_from_target,
            side_adjusted_distance, btc_move_toward_side,
            contract_move_during_lookback, volatility_1m, volatility_5m,
            time_remaining_at_entry, max_favorable_excursion, max_adverse_excursion,
            gross_pnl_cents, estimated_fees_cents, estimated_slippage_cents,
            net_pnl_cents, result_status, split_label, metadata_json
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s
        )
        """,
        (
            experiment_id, t.market_id, t.contract_id, t.side,
            t.exit_profile, t.holding_seconds_config, t.profit_target_cfg,
            t.signal_at, t.entry_at, t.entry_ask, t.entry_bid, t.entry_spread,
            t.exit_at, t.exit_bid, t.exit_reason, t.holding_seconds,
            t.btc_price_at_entry, t.target_price, t.distance_from_target,
            t.side_adjusted_distance, t.btc_move_toward_side,
            t.contract_move_during_lookback, t.volatility_1m, t.volatility_5m,
            t.time_remaining_at_entry, t.max_favorable_excursion, t.max_adverse_excursion,
            t.gross_pnl_cents, t.estimated_fees_cents, t.estimated_slippage_cents,
            t.net_pnl_cents, t.result_status, split_label, json.dumps(meta),
        ),
    )


# ── Core run logic ────────────────────────────────────────────────────────────

def run_backtest(
    cfg:           ExperimentConfig,
    markets:       list[MarketInfo],
    dry_run:       bool = False,
    experiment_id: Optional[int] = None,
) -> tuple[list[TradeResult], dict[str, Any]]:
    """
    Core backtest loop.  Returns (all_trades, summary_dict).
    Writes to DB only when dry_run=False and experiment_id is not None.
    """
    # ── Train / test split by complete market (chronological) ─────────────────
    n_train = max(1, int(len(markets) * cfg.train_fraction))
    train_markets = set(m.market_id for m in markets[:n_train])
    logger.info(
        "Split: %d train markets, %d test markets (train_fraction=%.2f)",
        n_train, len(markets) - n_train, cfg.train_fraction,
    )

    all_trades:   list[TradeResult] = []
    data_start:   Optional[datetime] = None
    data_end:     Optional[datetime] = None

    total_signals  = 0
    total_skipped  = 0
    n_markets_run  = 0

    for market in markets:
        split_label = "train" if market.market_id in train_markets else "test"
        rows = load_market_rows(market)

        if not rows:
            logger.debug("Skipping %s — no v2 snapshot data", market.market_id)
            continue

        if len(rows) < 10:
            logger.debug("Skipping %s — only %d rows", market.market_id, len(rows))
            continue

        n_markets_run += 1
        if data_start is None or rows[0].captured_at < data_start:
            data_start = rows[0].captured_at
        if data_end is None or rows[-1].captured_at > data_end:
            data_end = rows[-1].captured_at

        # ── Signal detection ───────────────────────────────────────────────────
        signals_with_split, counters = iter_signals(
            market = market,
            rows   = rows,
            config = cfg,
            split  = split_label,
        )
        total_signals += counters["n_signals"]
        total_skipped += counters["n_skipped_cooldown"] + counters["n_skipped_overlap"]

        # ── Overlap tracking (one active position per contract) ────────────────
        # active_exit_ts: {contract_id: ts when last simulated trade will exit}
        active_exit_ts: dict[int, float] = {}

        for signal, split in signals_with_split:
            cid = signal.contract_id

            # Overlap suppression (unless allow_multiple_entries_per_contract).
            if not cfg.allow_multiple_entries_per_contract:
                if signal.signal_ts < active_exit_ts.get(cid, 0.0):
                    total_skipped += 1
                    continue

            # Forward path = rows strictly after the signal index.
            forward_rows = rows[signal.snapshot_index + 1:]

            trade_results = simulate_exits(
                signal       = signal,
                forward_rows = forward_rows,
                market       = market,
                config       = cfg,
            )

            # Stamp the split label into metadata for later filtering.
            for t in trade_results:
                t.metadata["split"] = split

            all_trades.extend(trade_results)

            # Update active exit timestamp to the maximum exit across profiles.
            if not cfg.allow_multiple_entries_per_contract and trade_results:
                max_ht = max(t.holding_seconds_config for t in trade_results)
                active_exit_ts[cid] = signal.signal_ts + max_ht

            # Write to DB if not dry-run.
            if not dry_run and experiment_id is not None:
                for t in trade_results:
                    _insert_trade(experiment_id, t, split)

        logger.info(
            "Market %-40s [%s] | rows=%4d  signals=%3d  skip=%3d",
            market.market_id, split_label,
            len(rows), counters["n_signals"], total_skipped,
        )

    logger.info(
        "Total: %d markets  %d signals  %d skipped  %d trade rows",
        n_markets_run, total_signals, total_skipped, len(all_trades),
    )

    # ── Summary metrics ────────────────────────────────────────────────────────
    summary = compute_summary(all_trades)
    summary["experiment_id"]   = experiment_id
    summary["dry_run"]         = dry_run
    summary["n_markets"]       = n_markets_run
    summary["data_start_at"]   = data_start.isoformat() if data_start else None
    summary["data_end_at"]     = data_end.isoformat()   if data_end   else None
    summary["train_summary"]   = compute_summary(all_trades, split_filter="train")
    summary["test_summary"]    = compute_summary(all_trades, split_filter="test")

    return all_trades, summary, data_start, data_end


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Momentum-repricing hypothesis backtest over Kalshi BTC market data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--config", required=True, metavar="PATH",
        help="Path to experiment JSON config (e.g. experiments/momentum-lag.json)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Run the backtest without writing results to the database",
    )
    ap.add_argument(
        "--start", metavar="YYYY-MM-DD",
        help="Only include markets with opens_at >= this date",
    )
    ap.add_argument(
        "--end", metavar="YYYY-MM-DD",
        help="Only include markets with opens_at < this date",
    )
    ap.add_argument(
        "--limit-markets", type=int, metavar="N",
        help="Limit to N markets (chronologically oldest first — for smoke tests)",
    )
    ap.add_argument(
        "--csv-out", metavar="PATH", default=None,
        help="Export trade results to this CSV path",
    )
    ap.add_argument(
        "--json-out", metavar="PATH", default=None,
        help="Export summary to this JSON path",
    )
    ap.add_argument(
        "--notes", default="",
        help="Optional free-text notes stored in the experiment row",
    )
    ap.add_argument(
        "--skip-validation", action="store_true",
        help="Skip pre-flight data quality validation (faster, less safe)",
    )
    return ap.parse_args()


def main() -> None:
    args = _parse_args()

    # ── Load config ───────────────────────────────────────────────────────────
    cfg = load_config(args.config)
    logger.info("Experiment: %s", cfg.name)
    logger.info("Config: lookback=%ds  btc_min_move=$%.0f  max_reprice=%.2fc",
                cfg.lookback_seconds,
                cfg.minimum_btc_move_toward_target,
                cfg.maximum_contract_reprice_during_lookback * 100)

    if args.dry_run:
        logger.info("DRY RUN — no DB writes")

    # ── Load markets ──────────────────────────────────────────────────────────
    markets = load_all_markets(
        start = args.start,
        end   = args.end,
        limit = args.limit_markets,
    )
    if not markets:
        logger.error("No markets found in the v2 schema.  "
                     "Run the logger first, then migrate_to_v2.py.")
        sys.exit(1)
    logger.info("Loaded %d market(s)", len(markets))

    # ── Pre-flight validation ─────────────────────────────────────────────────
    if not args.skip_validation:
        logger.info("Running pre-flight validation …")
        rows_by_market = {m.market_id: load_market_rows(m) for m in markets}
        report = validate_all(markets, rows_by_market, cfg.lookback_seconds)
        for line in report.summary_lines():
            logger.info("%s", line)
        if not report.passed:
            logger.error("Validation found %d failed market(s). "
                         "Review issues above before proceeding.",
                         report.n_markets_failed)
            sys.exit(1)
    else:
        logger.info("Validation skipped (--skip-validation)")

    # ── Create experiment row ─────────────────────────────────────────────────
    experiment_id: Optional[int] = None
    if not args.dry_run:
        experiment_id = _create_experiment(cfg, args.notes)
        logger.info("Experiment row created: id=%d", experiment_id)

    # ── Run backtest ──────────────────────────────────────────────────────────
    all_trades, summary, data_start, data_end = run_backtest(
        cfg           = cfg,
        markets       = markets,
        dry_run       = args.dry_run,
        experiment_id = experiment_id,
    )

    # ── Finalize experiment ───────────────────────────────────────────────────
    if not args.dry_run and experiment_id is not None:
        _finish_experiment(experiment_id, data_start, data_end)
        logger.info("Experiment #%d marked complete", experiment_id)

    # ── Console summary ───────────────────────────────────────────────────────
    _print_summary(summary, experiment_id)

    # ── CSV export ────────────────────────────────────────────────────────────
    if args.csv_out:
        path = export_trades_csv(all_trades, args.csv_out)
        logger.info("Trades CSV written to %s", path)
    else:
        default_csv = Path("results") / f"momentum_bt_{cfg.name.replace(' ', '_')}.csv"
        path = export_trades_csv(all_trades, default_csv)
        logger.info("Trades CSV written to %s", path)

    # ── JSON summary export ───────────────────────────────────────────────────
    if args.json_out:
        path = export_summary_json(summary, args.json_out)
        logger.info("Summary JSON written to %s", path)
    else:
        default_json = Path("results") / f"momentum_bt_{cfg.name.replace(' ', '_')}_summary.json"
        path = export_summary_json(summary, default_json)
        logger.info("Summary JSON written to %s", path)

    if not args.dry_run and experiment_id:
        print(f"\nExperiment ID: {experiment_id}")
        print(
            f"Query results:\n"
            f"  SELECT * FROM momentum_bt_trades WHERE experiment_id = {experiment_id} LIMIT 20;"
        )


def _print_summary(summary: dict, experiment_id: Optional[int]) -> None:
    """Print a concise summary table to stdout."""
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  MOMENTUM BACKTEST RESULTS")
    if experiment_id:
        print(f"  Experiment ID: {experiment_id}")
    print(sep)

    def _fmt(v) -> str:
        return "—" if v is None else str(v)

    print(f"\n  Total signals:       {_fmt(summary.get('total_signals'))}")
    print(f"  Executable trades:   {_fmt(summary.get('executable_trades'))}")
    print(f"  Skipped signals:     {_fmt(summary.get('skipped_signals'))}")
    print(f"  Unexecutable:        {_fmt(summary.get('unexecutable'))}")

    print(f"\n  Wins:                {_fmt(summary.get('winning_trades'))}")
    print(f"  Losses:              {_fmt(summary.get('losing_trades'))}")
    wr = summary.get("win_rate")
    print(f"  Win rate:            {f'{wr:.1%}' if wr is not None else '—'}")

    ep = summary.get("expectancy_primary")
    print(f"\n  Expectancy (mean net P&L):  {f'{ep*100:+.2f}¢' if ep is not None else '—'}")
    ed = summary.get("expectancy_decomposed")
    print(f"  Expectancy (decomposed):    {f'{ed*100:+.2f}¢' if ed is not None else '—'}")
    pf = summary.get("profit_factor")
    print(f"  Profit factor:              {_fmt(pf)}")
    cn = summary.get("cumulative_net")
    print(f"  Cumulative net:             {f'{cn*100:+.2f}¢' if cn is not None else '—'}")
    md = summary.get("max_drawdown")
    print(f"  Max drawdown:               {f'{md*100:.2f}¢' if md else '0¢'}")

    print(f"\n  --- BY HOLDING PERIOD ---")
    for label, bkt in summary.get("by_holding_period", {}).items():
        wr_b = bkt.get("win_rate")
        avg  = bkt.get("avg_net")
        print(
            f"    {label:8s}  n={bkt['n']:4d}  "
            f"wr={f'{wr_b:.1%}' if wr_b is not None else '—':7s}  "
            f"avg_net={f'{avg*100:+.2f}¢' if avg is not None else '—'}"
        )

    print(f"\n  --- BY PROFIT TARGET ---")
    for label, bkt in summary.get("by_profit_target", {}).items():
        wr_b = bkt.get("win_rate")
        avg  = bkt.get("avg_net")
        print(
            f"    {label:10s}  n={bkt['n']:4d}  "
            f"wr={f'{wr_b:.1%}' if wr_b is not None else '—':7s}  "
            f"avg_net={f'{avg*100:+.2f}¢' if avg is not None else '—'}"
        )

    print(f"\n  --- BY SIDE ---")
    for label, bkt in summary.get("by_side", {}).items():
        wr_b = bkt.get("win_rate")
        avg  = bkt.get("avg_net")
        print(
            f"    {label:6s}  n={bkt['n']:4d}  "
            f"wr={f'{wr_b:.1%}' if wr_b is not None else '—':7s}  "
            f"avg_net={f'{avg*100:+.2f}¢' if avg is not None else '—'}"
        )

    ts = summary.get("test_summary", {})
    ep_test = ts.get("expectancy_primary")
    n_test  = ts.get("executable_trades", 0)
    print(f"\n  --- OUT-OF-SAMPLE (test set) ---")
    print(f"    Executable trades: {n_test}")
    print(f"    Expectancy:        {f'{ep_test*100:+.2f}¢' if ep_test is not None else '—'}")
    print()
    print("  NOTE: This is a simulated fill model.  Results may overestimate")
    print("  actual execution quality.  Do not report a profitable edge unless")
    print("  out-of-sample expectancy is positive after fees and slippage.")
    print(sep)


if __name__ == "__main__":
    main()
