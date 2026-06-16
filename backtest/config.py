"""
backtest/config.py — Typed experiment configuration.

All parameters come from a JSON file passed on the CLI.  No values here
represent validated strategy rules — any numeric defaults shown below are
example placeholders only.

Usage
-----
    from backtest.config import ExperimentConfig, load_config
    cfg = load_config("experiments/momentum-lag.json")
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ExperimentConfig:
    """
    Fully typed configuration for one momentum-repricing backtest experiment.

    Pricing units
    -------------
    Contract prices are dollar fractions in [0.01, 0.99].
    "5 cents" = 0.05.  All ``*_cents`` parameters use this convention.
    BTC distances and moves are in USD.

    Field definitions
    -----------------
    name / description / hypothesis
        Human-readable labels stored in the experiment row.

    lookback_seconds
        Window (seconds) over which BTC movement and contract repricing are
        measured.  Lookback uses only snapshots at or before the signal time
        (no lookahead).

    minimum_btc_move_toward_target
        Minimum USD move by BTC toward the evaluated contract's winning side
        over the lookback window.  Positive always means "toward YES winning"
        or "toward NO winning" depending on the side being evaluated.

    maximum_contract_reprice_during_lookback
        Maximum increase in the evaluated contract's ask over the same
        lookback window (dollar fraction).  Signals where the ask already
        moved by more than this are classified as "already priced in" and
        skipped.  A value of 0.03 = 3¢ cap.

    minimum/maximum_time_remaining_seconds
        Time-in-market filter.  Signals outside this range are skipped.

    minimum/maximum_entry_ask
        Entry price filter on the contract ask at signal time.

    maximum_spread
        Maximum allowable bid-ask spread (dollar fraction) at signal time.

    holding_period_seconds
        List of holding periods to compare.  One trade row is produced per
        (holding_period × profit_target) combination per qualifying signal.

    profit_target_cents
        Optional list of absolute profit targets (dollar fractions).
        Exit fires when: bid >= entry_ask + profit_target.
        None = no profit-target exit; use only time-based exits.

    loss_limit_cents
        Optional absolute loss limit (dollar fraction).  Exit fires when:
        bid <= entry_ask - loss_limit_cents.

    exit_on_momentum_reversal
        When True, exit when the 30-second side-adjusted BTC momentum sign
        flips negative (BTC starts moving away from the winning side).
        The reversal is evaluated at each forward tick.

    exit_on_distance_invalidation
        Optional USD threshold.  Exit when BTC has moved away from the
        contract's winning side by more than this amount from the entry
        distance (i.e., the trade thesis is invalidated).

    allow_multiple_entries_per_contract
        When False (default), only one simulated position per contract_id
        is active at a time.  Additional signals are suppressed until the
        active simulated trade exits.

    cooldown_seconds
        After any signal fires on a contract (trade opened or not), ignore
        further signals on that contract for this many seconds.

    estimated_fee_per_contract
        Flat cost per simulated contract per trade side (dollar fraction).
        Deducted once (not round-trip); the caller controls whether to apply
        at entry, exit, or both.  Document your choice in ``description``.

    estimated_slippage_cents
        Total round-trip slippage (dollar fraction) deducted from net P&L.
        Represents expected execution quality degradation vs. quoted prices.

    train_fraction
        Fraction of markets (earliest by open time) designated as the
        training/exploration set.  The remainder is the validation set.
        Split is always by COMPLETE MARKET — no snapshot ever crosses the
        boundary.  Default 0.7 = 70% training.
    """

    # ── Metadata ─────────────────────────────────────────────────────────────
    name:        str = "unnamed"
    description: str = ""
    hypothesis:  str = ""

    # ── Signal detection ─────────────────────────────────────────────────────
    lookback_seconds:                         int   = 30
    minimum_btc_move_toward_target:           float = 20.0
    maximum_contract_reprice_during_lookback: float = 0.03

    # ── Time-remaining filter ─────────────────────────────────────────────────
    minimum_time_remaining_seconds: int = 300   # 5 min
    maximum_time_remaining_seconds: int = 720   # 12 min

    # ── Entry price filter ────────────────────────────────────────────────────
    minimum_entry_ask: float = 0.05
    maximum_entry_ask: float = 0.95
    maximum_spread:    float = 0.04

    # ── Exit configuration ────────────────────────────────────────────────────
    # Multiple values → one trade row per (holding_period × profit_target) combo.
    holding_period_seconds: list[int]            = field(default_factory=lambda: [30, 60, 120])
    profit_target_cents:    Optional[list[float]] = None   # e.g. [0.03, 0.05, 0.08, 0.10]
    loss_limit_cents:       Optional[float]       = None

    exit_on_momentum_reversal:     bool           = False
    exit_on_distance_invalidation: Optional[float] = None  # USD threshold

    # ── Position management ───────────────────────────────────────────────────
    allow_multiple_entries_per_contract: bool  = False
    cooldown_seconds:                    float = 30.0

    # ── Cost model (dollar fractions, round-trip) ─────────────────────────────
    estimated_fee_per_contract: float = 0.003
    estimated_slippage_cents:   float = 0.01

    # ── Train / test split ────────────────────────────────────────────────────
    train_fraction: float = 0.7


_VALID_KEYS = frozenset(f.name for f in dataclasses.fields(ExperimentConfig))


def load_config(path: str | Path) -> ExperimentConfig:
    """Load an ExperimentConfig from a JSON file.

    Raises ValueError for any unrecognised key so typos fail loudly.
    """
    raw: dict[str, Any] = json.loads(Path(path).read_text())
    extra = set(raw) - _VALID_KEYS
    if extra:
        raise ValueError(
            f"Unknown config keys in {path}: {sorted(extra)}\n"
            f"Valid keys: {sorted(_VALID_KEYS)}"
        )
    return ExperimentConfig(**raw)


def config_to_dict(cfg: ExperimentConfig) -> dict[str, Any]:
    """Serialize an ExperimentConfig to a JSON-safe dict."""
    return dataclasses.asdict(cfg)
