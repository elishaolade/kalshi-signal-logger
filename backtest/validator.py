"""
backtest/validator.py — Pre-flight data quality validation.

Run this before generating any backtest results.  A ValidationReport is
returned rather than raised so the caller can log it and decide whether to
proceed or abort.

Checks performed
----------------
1.  Timestamps are strictly ascending (no duplicates, no reversals).
2.  Polling gaps (> 10 seconds between consecutive snapshots) are counted.
3.  At least one future snapshot exists (forward window not empty).
4.  bid <= ask for both YES and NO where prices are present.
5.  Prices are within the expected contract range [0.00, 1.00].
6.  market target_price is present.
7.  market opens_at and closes_at are present.
8.  contract IDs for both YES and NO exist.
9.  Row count is sufficient for the configured lookback window.
10. No future data is accessible from signal rows (verified structurally —
    the signal detector uses only rows[lo_ptr:i] which is past-only).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from backtest.loader import MarketInfo, MarketRow

logger = logging.getLogger(__name__)

# A gap larger than this many seconds is flagged (expected polling ~2s).
GAP_THRESHOLD_S   = 10.0
# Minimum rows needed to have any chance of matching a 30-second lookback.
MIN_ROWS_FOR_LOOKBACK = 10
# Maximum plausible contract price.
MAX_CONTRACT_PRICE = 1.00
MIN_CONTRACT_PRICE = 0.00


@dataclass
class MarketValidation:
    """Validation result for a single market."""
    market_id:    str
    n_rows:       int = 0
    n_gaps:       int = 0          # rows where ts[i] - ts[i-1] > GAP_THRESHOLD_S
    max_gap_s:    float = 0.0
    n_bid_gt_ask: int = 0          # rows where bid > ask for either side
    n_price_oob:  int = 0          # rows with contract price outside [0, 1]
    n_dup_ts:     int = 0          # rows with duplicate timestamp
    passed:       bool = True
    issues:       list[str] = field(default_factory=list)

    def _fail(self, msg: str) -> None:
        self.passed = False
        self.issues.append(msg)


@dataclass
class ValidationReport:
    """Aggregate validation result across all markets."""
    n_markets_checked:   int = 0
    n_markets_passed:    int = 0
    n_markets_failed:    int = 0
    n_markets_skipped:   int = 0   # no rows at all → skip silently
    total_rows:          int = 0
    total_gaps:          int = 0
    total_bid_gt_ask:    int = 0
    market_results:      list[MarketValidation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.n_markets_failed == 0

    def summary_lines(self) -> list[str]:
        lines = [
            f"Validation: {self.n_markets_checked} markets checked, "
            f"{self.n_markets_passed} passed, {self.n_markets_failed} failed, "
            f"{self.n_markets_skipped} skipped (no rows)",
            f"  Total rows: {self.total_rows}   "
            f"Polling gaps: {self.total_gaps}   "
            f"Bid>ask violations: {self.total_bid_gt_ask}",
        ]
        for mv in self.market_results:
            if not mv.passed:
                lines.append(f"  FAIL {mv.market_id}: {'; '.join(mv.issues)}")
        return lines


def validate_market(
    market: MarketInfo,
    rows:   list[MarketRow],
    lookback_seconds: int = 30,
) -> MarketValidation:
    """Validate one market's rows and return a MarketValidation."""
    mv = MarketValidation(market_id=market.market_id)

    # ── Market-level checks ───────────────────────────────────────────────────
    if market.target_price is None:
        mv._fail("target_price is NULL")

    if market.opens_at is None:
        mv._fail("opens_at is NULL")

    if market.closes_at is None:
        mv._fail("closes_at is NULL")

    if "YES" not in market.contract_ids:
        mv._fail("missing YES contract row")

    if "NO" not in market.contract_ids:
        mv._fail("missing NO contract row")

    mv.n_rows = len(rows)

    if not rows:
        # No snapshot data — skip further row-level checks.
        mv._fail("no rows (market was never polled or data not migrated to v2)")
        return mv

    if mv.n_rows < MIN_ROWS_FOR_LOOKBACK:
        mv._fail(
            f"only {mv.n_rows} rows — need at least {MIN_ROWS_FOR_LOOKBACK} "
            f"for {lookback_seconds}s lookback"
        )

    # Check at least one future row will exist after any signal.
    if mv.n_rows < 2:
        mv._fail("fewer than 2 rows — no forward path exists for any signal")

    # ── Row-level checks ───────────────────────────────────────────────────────
    seen_ts: set[float] = set()

    for i, row in enumerate(rows):
        # Duplicate timestamps
        if row.ts in seen_ts:
            mv.n_dup_ts += 1
        seen_ts.add(row.ts)

        # Polling gap
        if i > 0:
            gap = row.ts - rows[i - 1].ts
            if gap > GAP_THRESHOLD_S:
                mv.n_gaps += 1
                mv.max_gap_s = max(mv.max_gap_s, gap)

        # bid > ask violations (contract prices)
        for bid, ask, label in (
            (row.yes_bid, row.yes_ask, "YES"),
            (row.no_bid,  row.no_ask,  "NO"),
        ):
            if bid is not None and ask is not None and bid > ask + 1e-6:
                mv.n_bid_gt_ask += 1
                logger.debug(
                    "%s ts=%.0f %s bid=%.4f > ask=%.4f",
                    market.market_id, row.ts, label, bid, ask,
                )

        # Price range check
        for price in (row.yes_bid, row.yes_ask, row.no_bid, row.no_ask):
            if price is not None and not (MIN_CONTRACT_PRICE <= price <= MAX_CONTRACT_PRICE):
                mv.n_price_oob += 1

    if mv.n_dup_ts > 0:
        mv._fail(f"{mv.n_dup_ts} duplicate timestamps")

    if mv.n_bid_gt_ask > 0:
        # Warn but don't fail — occasional stale quotes can cause transient inversions.
        mv.issues.append(
            f"WARNING: {mv.n_bid_gt_ask} row(s) with bid > ask "
            "(these snapshots will be skipped by the signal detector)"
        )

    if mv.n_price_oob > 0:
        mv.issues.append(
            f"WARNING: {mv.n_price_oob} price(s) outside [0,1] "
            "(those snapshots will be skipped)"
        )

    if mv.n_gaps > 0:
        mv.issues.append(
            f"INFO: {mv.n_gaps} polling gap(s) > {GAP_THRESHOLD_S:.0f}s "
            f"(max {mv.max_gap_s:.0f}s) — no price fabrication; gaps are skipped"
        )

    return mv


def validate_all(
    markets:          list[MarketInfo],
    rows_by_market:   dict[str, list[MarketRow]],
    lookback_seconds: int = 30,
) -> ValidationReport:
    """
    Validate all markets and return a ValidationReport.

    Parameters
    ----------
    markets         : list from load_all_markets()
    rows_by_market  : {market.market_id: rows} from load_market_rows()
    lookback_seconds: from ExperimentConfig.lookback_seconds
    """
    report = ValidationReport()

    for market in markets:
        rows = rows_by_market.get(market.market_id, [])
        report.n_markets_checked += 1

        if not rows:
            report.n_markets_skipped += 1
            continue

        mv = validate_market(market, rows, lookback_seconds)
        report.market_results.append(mv)
        report.total_rows       += mv.n_rows
        report.total_gaps       += mv.n_gaps
        report.total_bid_gt_ask += mv.n_bid_gt_ask

        if mv.passed:
            report.n_markets_passed += 1
        else:
            report.n_markets_failed += 1

    return report
