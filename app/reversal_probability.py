"""
reversal_probability.py — Self-referential reversal-probability estimator for
cheap_losing_contract_reversal_trail/v1.

Given a candidate setup (losing-contract price, market age, adverse z-score,
spread, volatility regime, hour block), this estimates how often *similar past
setups* produced a favorable reversal, using this strategy's own completed
observations in `clc_reversal_observations`.

It is read-only: it never writes, and it is the ONLY place that touches the DB
for this feature, so the strategy functions stay side-effect free.  A provider
instance is created in main.py and its `lookup` method is injected into
`run_all`.

Progressive broadening (stops at the first level with >= MIN_SAMPLE rows):
  1. exact      price + age + adverse_z + spread + vol_regime + hour_block
  2. drop hour  price + age + adverse_z + spread + vol_regime
  3. drop vol   price + age + adverse_z + spread
  4. widen      coarse price + coarse adverse_z + age   (spread dropped)
  5. insufficient_data — use the widest stats but label by final sample size

Confidence labels (by final sample size):
  < 30   insufficient_data
  30-49  weak_sample
  50-99  usable_sample
  >= 100 stronger_sample

historical_reversal_probability returned to the strategy is
p_plus_3c_before_minus_2c (the "+3c/-2c style reversal test" in the spec).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.db import fetch_one

logger = logging.getLogger(__name__)

MIN_SAMPLE = 30   # broaden until at least this many similar setups are found

_RULE_NAME    = "cheap_losing_contract_reversal_trail"
_RULE_VERSION = "v1"


# ── Bucket bounds (shared with the strategy for setup_type labelling) ──────────

def _bounds(value: float, edges: list[float]) -> tuple[float, float]:
    """
    Return the [lo, hi) edge pair that `value` falls into.  `edges` is an
    ascending list; values below the first edge clamp to (-inf, edges[0]) and
    values at/above the last clamp to (edges[-1], +inf).
    """
    for i in range(len(edges) - 1):
        if edges[i] <= value < edges[i + 1]:
            return edges[i], edges[i + 1]
    if value < edges[0]:
        return float("-inf"), edges[0]
    return edges[-1], float("inf")


_PRICE_EDGES        = [0.05, 0.10, 0.20, 0.30]
_PRICE_EDGES_COARSE = [0.05, 0.15, 0.30]
_AGE_EDGES          = [0.0, 60.0, 120.0, 180.0]
# Adverse-z gate relaxed 1.5 → 3.0 (2026-05-31), so the bucket grid is extended
# to keep the self-referential lookup meaningful across the wider range.
_Z_EDGES            = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
_Z_EDGES_COARSE     = [0.0, 1.0, 2.0, 3.0]
_SPREAD_EDGES       = [0.0, 0.01, 0.02, 0.03]


def price_bucket_label(ask: float) -> str:
    lo, hi = _bounds(ask, _PRICE_EDGES)
    return f"{lo:.2f}-{hi:.2f}" if hi != float("inf") else f"{lo:.2f}+"


def age_bucket_label(age_s: float) -> str:
    if age_s < 60:   return "0-60s"
    if age_s < 120:  return "60-120s"
    if age_s < 180:  return "120-180s"
    return                  "180s+"


def adverse_z_bucket_label(z: float) -> str:
    if z < 0.0:      return "<0.0"
    if z < 1.5:      return "0.0-1.5"
    if z < 2.5:      return "1.5-2.5"
    if z < 3.5:      return "2.5-3.5"
    return                  "3.5+"


def setup_type(ask: float, age_s: float, adverse_z: float) -> str:
    """Compact, human-readable signature of the core setup buckets."""
    return (
        f"price={price_bucket_label(ask)}"
        f"|age={age_bucket_label(age_s)}"
        f"|advz={adverse_z_bucket_label(adverse_z)}"
    )


# ── Provider ───────────────────────────────────────────────────────────────────

def _empty_result() -> dict[str, Any]:
    return {
        "similar_sample_count":            0,
        "p_plus_2c_before_minus_2c":       None,
        "p_plus_3c_before_minus_2c":       None,
        "p_plus_4c_before_minus_3c":       None,
        "avg_max_favorable_excursion":     None,
        "median_max_favorable_excursion":  None,
        "avg_max_adverse_excursion":       None,
        "confidence_label":                "insufficient_data",
        "match_level":                     "none",
    }


def _confidence_label(n: int) -> str:
    if n < 30:   return "insufficient_data"
    if n < 50:   return "weak_sample"
    if n < 100:  return "usable_sample"
    return            "stronger_sample"


class ReversalProbabilityProvider:
    """
    Read-only estimator over this strategy's own completed primary-path rows.

    Only rows with is_primary_path_row = 1 AND status = 'COMPLETE' are counted,
    so each observed signal contributes exactly once (not once per exit profile).
    """

    # Stats aggregated per matching level.
    _STATS_SELECT = """
        SELECT
            COUNT(*)                                  AS n,
            AVG(hit_plus_2c_before_minus_2c)          AS p2,
            AVG(hit_plus_3c_before_minus_2c)          AS p3,
            AVG(hit_plus_4c_before_minus_3c)          AS p4,
            AVG(max_favorable_excursion)              AS avg_mfe,
            AVG(max_adverse_excursion)                AS avg_mae
        FROM clc_reversal_observations
        WHERE is_primary_path_row = 1
          AND status = 'COMPLETE'
          AND rule_name = %s
          AND rule_version = %s
    """

    def __init__(self, min_sample: int = MIN_SAMPLE) -> None:
        self._min_sample = min_sample

    # ── Public ────────────────────────────────────────────────────────────────

    def lookup(
        self,
        *,
        losing_ask: float,
        market_age_seconds: float,
        adverse_z: float,
        spread: float,
        volatility_regime: str,
        hour_block: Optional[str],
    ) -> dict[str, Any]:
        """
        Estimate reversal probability for a candidate setup.  Never raises —
        on any DB error it logs and returns insufficient_data so the strategy
        can still record the observation.
        """
        try:
            return self._lookup(
                losing_ask, market_age_seconds, adverse_z,
                spread, volatility_regime, hour_block,
            )
        except Exception:
            logger.exception("ReversalProbabilityProvider.lookup failed — returning insufficient_data")
            return _empty_result()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _lookup(
        self,
        losing_ask: float,
        market_age_seconds: float,
        adverse_z: float,
        spread: float,
        volatility_regime: str,
        hour_block: Optional[str],
    ) -> dict[str, Any]:
        p_lo, p_hi   = _bounds(losing_ask,         _PRICE_EDGES)
        pc_lo, pc_hi = _bounds(losing_ask,         _PRICE_EDGES_COARSE)
        a_lo, a_hi   = _bounds(market_age_seconds, _AGE_EDGES)
        z_lo, z_hi   = _bounds(adverse_z,          _Z_EDGES)
        zc_lo, zc_hi = _bounds(adverse_z,          _Z_EDGES_COARSE)
        s_lo, s_hi   = _bounds(spread,             _SPREAD_EDGES)

        price_cond = ("losing_contract_ask >= %s AND losing_contract_ask < %s", [p_lo, p_hi])
        age_cond   = ("market_age_seconds  >= %s AND market_age_seconds  < %s", [a_lo, a_hi])
        z_cond     = ("adverse_z_score     >= %s AND adverse_z_score     < %s", [z_lo, z_hi])
        spread_cond= ("losing_contract_spread >= %s AND losing_contract_spread < %s", [s_lo, s_hi])
        vol_cond   = ("volatility_regime = %s", [volatility_regime])
        hour_cond  = ("hour_block = %s", [hour_block]) if hour_block else None

        price_coarse = ("losing_contract_ask >= %s AND losing_contract_ask < %s", [pc_lo, pc_hi])
        z_coarse     = ("adverse_z_score     >= %s AND adverse_z_score     < %s", [zc_lo, zc_hi])

        # Ordered broadening levels: each is a list of (sql_fragment, params).
        levels: list[tuple[str, list]] = [
            ("exact",     [c for c in (price_cond, age_cond, z_cond, spread_cond, vol_cond, hour_cond) if c]),
            ("drop_hour", [price_cond, age_cond, z_cond, spread_cond, vol_cond]),
            ("drop_vol",  [price_cond, age_cond, z_cond, spread_cond]),
            ("widened",   [price_coarse, age_cond, z_coarse]),
        ]

        widest_result: Optional[dict[str, Any]] = None
        for level_name, conds in levels:
            row = self._run_level(conds)
            n = int(row["n"] or 0)
            result = self._row_to_result(row, level_name)
            widest_result = result            # remember the broadest attempt
            if n >= self._min_sample:
                return result

        # Nothing reached MIN_SAMPLE — return the widest attempt (labelled by its
        # actual count, which will be insufficient_data when < 30).
        return widest_result or _empty_result()

    def _run_level(self, conds: list[tuple[str, list]]) -> dict:
        sql = self._STATS_SELECT
        params: list[Any] = [_RULE_NAME, _RULE_VERSION]
        for frag, frag_params in conds:
            sql += f"\n          AND ({frag})"
            params.extend(frag_params)
        row = fetch_one(sql, tuple(params))
        return row or {"n": 0}

    @staticmethod
    def _row_to_result(row: dict, level_name: str) -> dict[str, Any]:
        n = int(row.get("n") or 0)

        def _f(key: str) -> Optional[float]:
            v = row.get(key)
            return None if v is None else round(float(v), 6)

        return {
            "similar_sample_count":            n,
            "p_plus_2c_before_minus_2c":       _f("p2"),
            "p_plus_3c_before_minus_2c":       _f("p3"),
            "p_plus_4c_before_minus_3c":       _f("p4"),
            "avg_max_favorable_excursion":     _f("avg_mfe"),
            # Median is approximated by avg here (a separate percentile query is
            # avoided for cost); reports compute true medians from raw rows.
            "median_max_favorable_excursion":  _f("avg_mfe"),
            "avg_max_adverse_excursion":       _f("avg_mae"),
            "confidence_label":                _confidence_label(n),
            "match_level":                     level_name,
        }
