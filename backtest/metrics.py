"""
backtest/metrics.py — Pure summary statistics for backtest results.

All functions are side-effect-free.  They operate on a list of TradeResult
objects and return plain dicts.

Expectancy definition
---------------------
Primary form (actual trade P&L average):
    expectancy = mean(net_pnl_cents) across all executable trades

Decomposed form (probability-weighted):
    expectancy = (win_rate × avg_net_win) - (loss_rate × abs(avg_net_loss))

Both are computed and returned.

IMPORTANT: Unexecutable trades are excluded from all P&L metrics but counted
separately.  "Executable" = result_status in (win, loss, breakeven).
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Optional

from backtest.executor import (
    RESULT_BREAKEVEN,
    RESULT_LOSS,
    RESULT_SKIPPED,
    RESULT_UNEXECUTABLE,
    RESULT_WIN,
    TradeResult,
)


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return statistics.mean(values)


def _pf(wins: list[float], losses: list[float]) -> Optional[float]:
    """Profit factor = sum(wins) / abs(sum(losses))."""
    w = sum(wins)
    l = abs(sum(losses))
    if l == 0:
        return None
    return round(w / l, 4)


def _max_drawdown(pnl_series: list[float]) -> float:
    """Maximum drawdown of a cumulative P&L series (dollar fractions)."""
    if not pnl_series:
        return 0.0
    peak = 0.0
    cumul = 0.0
    max_dd = 0.0
    for pnl in pnl_series:
        cumul += pnl
        peak = max(peak, cumul)
        dd = peak - cumul
        max_dd = max(max_dd, dd)
    return round(max_dd, 6)


def _safe_round(v: Optional[float], n: int = 6) -> Optional[float]:
    return None if v is None else round(v, n)


def compute_summary(
    trades:       list[TradeResult],
    split_filter: Optional[str] = None,  # "train" | "test" | None = all
) -> dict[str, Any]:
    """
    Compute all summary metrics for a list of TradeResult objects.

    Parameters
    ----------
    trades       : all trade results (may include skipped/unexecutable)
    split_filter : when not None, only trades matching this split label
                   are included (requires TradeResult.metadata["split"])
    """
    if split_filter is not None:
        trades = [t for t in trades if t.metadata.get("split") == split_filter]

    executable = [t for t in trades if t.result_status in (RESULT_WIN, RESULT_LOSS, RESULT_BREAKEVEN)]
    wins       = [t for t in executable if t.result_status == RESULT_WIN]
    losses     = [t for t in executable if t.result_status == RESULT_LOSS]
    breakeven  = [t for t in executable if t.result_status == RESULT_BREAKEVEN]
    skipped    = [t for t in trades     if t.result_status == RESULT_SKIPPED]
    unexec     = [t for t in trades     if t.result_status == RESULT_UNEXECUTABLE]

    n_exec  = len(executable)
    n_total = len(trades)

    # ── P&L lists ─────────────────────────────────────────────────────────────
    net_pnls  = [t.net_pnl_cents  for t in executable if t.net_pnl_cents  is not None]
    gross_pnls= [t.gross_pnl_cents for t in executable if t.gross_pnl_cents is not None]
    win_pnls  = [t.net_pnl_cents  for t in wins   if t.net_pnl_cents is not None]
    loss_pnls = [t.net_pnl_cents  for t in losses if t.net_pnl_cents is not None]

    win_rate  = len(wins)  / n_exec if n_exec > 0 else None
    loss_rate = len(losses)/ n_exec if n_exec > 0 else None

    avg_net_win  = _mean(win_pnls)
    avg_net_loss = _mean(loss_pnls)

    # Primary expectancy: mean net P&L per executable trade.
    expectancy_primary = _mean(net_pnls)

    # Decomposed expectancy.
    expectancy_decomposed: Optional[float] = None
    if win_rate is not None and avg_net_win is not None and loss_rate is not None and avg_net_loss is not None:
        expectancy_decomposed = round(
            (win_rate * avg_net_win) - (loss_rate * abs(avg_net_loss)), 6
        )

    # Profit factor
    pf = _pf(
        [p for p in win_pnls if p > 0],
        [p for p in loss_pnls if p < 0],
    )

    # Cumulative net P&L
    cumul_net = round(sum(net_pnls), 6) if net_pnls else None

    # Maximum drawdown
    max_dd = _max_drawdown(net_pnls)

    # Holding times
    hold_times = [t.holding_seconds for t in executable if t.holding_seconds is not None]

    # MFE / MAE
    mfes = [t.max_favorable_excursion for t in executable if t.max_favorable_excursion is not None]
    maes = [t.max_adverse_excursion   for t in executable if t.max_adverse_excursion   is not None]

    # ── Breakdown tables ───────────────────────────────────────────────────────
    by_holding     = _breakdown(executable, key=lambda t: str(t.holding_seconds_config) + "s")
    by_profit_tgt  = _breakdown(executable, key=lambda t:
                        f"tp{round(t.profit_target_cfg*100):.0f}c" if t.profit_target_cfg else "no_tp")
    by_side        = _breakdown(executable, key=lambda t: t.side)
    by_tte_bucket  = _breakdown(executable, key=lambda t: _tte_bucket(t.time_remaining_at_entry))
    by_ask_bucket  = _breakdown(executable, key=lambda t: _ask_bucket(t.entry_ask))
    by_vol_regime  = _breakdown(executable, key=lambda t: _vol_regime(t.volatility_1m))
    by_dist_bucket = _breakdown(executable, key=lambda t: _dist_bucket(t.side_adjusted_distance))

    return {
        # Counts
        "total_signals":     n_total,
        "executable_trades": n_exec,
        "skipped_signals":   len(skipped),
        "unexecutable":      len(unexec),
        "winning_trades":    len(wins),
        "losing_trades":     len(losses),
        "breakeven_trades":  len(breakeven),

        # Rates
        "win_rate":  _safe_round(win_rate,  4),
        "loss_rate": _safe_round(loss_rate, 4),

        # Averages
        "avg_gross_win":  _safe_round(_mean([t.gross_pnl_cents for t in wins  if t.gross_pnl_cents is not None])),
        "avg_gross_loss": _safe_round(_mean([t.gross_pnl_cents for t in losses if t.gross_pnl_cents is not None])),
        "avg_net_win":    _safe_round(avg_net_win),
        "avg_net_loss":   _safe_round(avg_net_loss),
        "avg_net_pnl":    _safe_round(_mean(net_pnls)),

        # Expectancy
        "expectancy_primary":    _safe_round(expectancy_primary),
        "expectancy_decomposed": _safe_round(expectancy_decomposed),

        # Aggregate
        "profit_factor":   pf,
        "cumulative_net":  cumul_net,
        "max_drawdown":    round(max_dd, 6),

        # Holding time
        "median_holding_seconds": _safe_round(statistics.median(hold_times), 1) if hold_times else None,
        "avg_holding_seconds":    _safe_round(_mean(hold_times), 1),

        # Excursion
        "avg_mfe": _safe_round(_mean(mfes)),
        "avg_mae": _safe_round(_mean(maes)),

        # Breakdowns
        "by_holding_period":    by_holding,
        "by_profit_target":     by_profit_tgt,
        "by_side":              by_side,
        "by_tte_bucket":        by_tte_bucket,
        "by_ask_bucket":        by_ask_bucket,
        "by_vol_regime":        by_vol_regime,
        "by_dist_bucket":       by_dist_bucket,
    }


def _breakdown(trades: list[TradeResult], *, key) -> dict[str, dict]:
    """Group trades by a key function and compute P&L stats for each bucket."""
    buckets: dict[str, list[TradeResult]] = defaultdict(list)
    for t in trades:
        buckets[key(t)].append(t)

    out: dict[str, dict] = {}
    for label, group in sorted(buckets.items()):
        wins   = [t for t in group if t.result_status == RESULT_WIN]
        losses = [t for t in group if t.result_status == RESULT_LOSS]
        pnls   = [t.net_pnl_cents for t in group if t.net_pnl_cents is not None]
        out[label] = {
            "n":          len(group),
            "wins":       len(wins),
            "losses":     len(losses),
            "win_rate":   _safe_round(len(wins) / len(group), 4) if group else None,
            "avg_net":    _safe_round(_mean(pnls)),
            "total_net":  _safe_round(sum(pnls), 6) if pnls else None,
        }
    return out


# ── Bucket helpers ────────────────────────────────────────────────────────────

def _tte_bucket(tte: Optional[int]) -> str:
    if tte is None:
        return "unknown"
    if tte < 120:
        return "0-2m"
    if tte < 300:
        return "2-5m"
    if tte < 480:
        return "5-8m"
    if tte < 720:
        return "8-12m"
    return "12m+"


def _ask_bucket(ask: float) -> str:
    if ask < 0.20:
        return "0-20c"
    if ask < 0.40:
        return "20-40c"
    if ask < 0.60:
        return "40-60c"
    if ask < 0.80:
        return "60-80c"
    return "80-100c"


def _vol_regime(vol_1m: Optional[float]) -> str:
    """Classify volatility using the same thresholds as app/features.py."""
    if vol_1m is None:
        return "unknown"
    # market_metrics stores btc_return_stddev (fraction, not USD std).
    # Treat very small values as calm, larger as elevated.
    if vol_1m < 0.0003:
        return "calm"
    if vol_1m < 0.0008:
        return "normal"
    if vol_1m < 0.0015:
        return "elevated"
    return "violent"


def _dist_bucket(dist: float) -> str:
    """Side-adjusted distance from target in USD."""
    if dist < -200:
        return "<-200"
    if dist < -100:
        return "-200 to -100"
    if dist < 0:
        return "-100 to 0"
    if dist < 100:
        return "0 to 100"
    if dist < 200:
        return "100 to 200"
    return ">200"
