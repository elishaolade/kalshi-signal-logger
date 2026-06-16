"""
backtest — Momentum-repricing hypothesis backtest module.

Reads from v2 schema tables only (market_snapshots, contract_snapshots,
market_metrics, markets, contracts).  Never writes to raw data tables.

Entry point:  python scripts/momentum_backtest.py --config <file.json>
"""
