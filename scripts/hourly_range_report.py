#!/usr/bin/env python3
"""
hourly_range_report.py — Research report for BTC hourly range market observations.

Reads from:
  hourly_range_markets      — one row per settled/closed/open market
  hourly_range_observations — sampled state throughout each market's life

Answers six research questions:
  Q1. How often does BTC stay inside the range for the full hour?
  Q2. How often does it start inside and finish outside?
  Q3. How often does it start outside and move inside by expiry?
  Q4. What is the typical BTC path relative to floor/cap/center?
  Q5. Which band widths and center-offsets seem most stable (containment)?
  Q6. How does recent BTC volatility relate to containment outcome?

Usage:
    python scripts/hourly_range_report.py
    python scripts/hourly_range_report.py --min-markets 5   # only show if ≥5 markets
    python scripts/hourly_range_report.py --band-width 1000  # filter to $1000-wide bands
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import fetch_all, fetch_one

# ── Formatting helpers ────────────────────────────────────────────────────────

def _pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "  n/a"

def _f2(v: Optional[float]) -> str:
    return f"{v:.2f}" if v is not None else "n/a"

def _f4(v: Optional[float]) -> str:
    return f"{v:.4f}" if v is not None else "n/a"

def _row(label: str, value: str, width: int = 38) -> None:
    print(f"  {label:<{width}} {value}")

def _sep(char: str = "─", width: int = 70) -> None:
    print(char * width)

def _h1(title: str) -> None:
    print()
    _sep("═")
    print(f"  {title}")
    _sep("═")

def _h2(title: str) -> None:
    print()
    _sep()
    print(f"  {title}")
    _sep()


# ── DB helpers ────────────────────────────────────────────────────────────────

def _count(table: str, where: str = "1=1", params: tuple = ()) -> int:
    row = fetch_one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)
    return int(row["n"]) if row else 0


# ── Section builders ──────────────────────────────────────────────────────────

def _section_overview(min_markets: int) -> None:
    _h1("Hourly Range Market Observations — Overview")

    total   = _count("hourly_range_markets")
    settled = _count("hourly_range_markets", "status = 'settled'")
    closed  = _count("hourly_range_markets", "status = 'closed'")
    open_   = _count("hourly_range_markets", "status = 'open'")
    total_obs = _count("hourly_range_observations")

    _row("Total markets discovered:", str(total))
    _row("  settled (outcome known):", str(settled))
    _row("  closed (no final price):", str(closed))
    _row("  open (currently active):", str(open_))
    _row("Total observations logged:", str(total_obs))

    if settled == 0:
        print(f"\n  ⚠  No settled markets yet (need at least {min_markets} for Q1–Q6).")
        return

    # Overall containment rate on settled markets.
    cont = fetch_one(
        """
        SELECT
            COUNT(*)                                AS n,
            SUM(contained = 1)                      AS n_contained,
            AVG(contained)                          AS contain_rate,
            AVG(n_observations)                     AS avg_obs,
            AVG(pct_time_inside)                    AS avg_pct_inside,
            AVG(band_width)                         AS avg_band_width,
            MIN(band_width)                         AS min_band_width,
            MAX(band_width)                         AS max_band_width
        FROM hourly_range_markets
        WHERE status = 'settled'
        """
    )
    if cont:
        print()
        _row("Containment rate (settled):", _pct(cont["contain_rate"]))
        _row("  N settled:", str(int(cont["n"])))
        _row("  N contained:", str(int(cont["n_contained"] or 0)))
        _row("Avg pct time inside band:", _pct(cont["avg_pct_inside"]))
        _row("Avg observations / market:", f"{float(cont['avg_obs'] or 0):.1f}")
        _row("Band width — avg / min / max:",
             f"${_f2(cont['avg_band_width'])} / "
             f"${_f2(cont['min_band_width'])} / "
             f"${_f2(cont['max_band_width'])}")


def _section_q1_q3(min_markets: int) -> None:
    _h2("Q1–Q3: Entry state vs. outcome")

    # First observation per market to get entry state.
    summary = fetch_all(
        """
        SELECT
            hrm.market_ticker,
            hrm.contained,
            hrm.floor_strike,
            hrm.cap_strike,
            first_obs.inside_band   AS started_inside
        FROM hourly_range_markets hrm
        JOIN (
            SELECT market_ticker, inside_band
            FROM hourly_range_observations
            WHERE (market_ticker, observed_at) IN (
                SELECT market_ticker, MIN(observed_at)
                FROM hourly_range_observations
                GROUP BY market_ticker
            )
        ) first_obs ON first_obs.market_ticker = hrm.market_ticker
        WHERE hrm.status = 'settled'
        """
    )

    if len(summary) < min_markets:
        print(f"  Not enough settled markets (have {len(summary)}, need {min_markets}).")
        return

    n_total    = len(summary)
    n_si_so    = sum(1 for r in summary if r["started_inside"] and     r["contained"])  # started inside, stayed inside
    n_si_leave = sum(1 for r in summary if r["started_inside"] and not r["contained"])  # started inside, left
    n_so_in    = sum(1 for r in summary if not r["started_inside"] and     r["contained"])  # started outside, moved in
    n_so_out   = sum(1 for r in summary if not r["started_inside"] and not r["contained"])  # started outside, stayed out

    print()
    print("  Started inside, finished inside (full containment):"
          f"  {n_si_so:3d} / {n_total}  ({n_si_so/n_total*100:.1f}%)")
    print("  Started inside, finished outside (breakout)      :"
          f"  {n_si_leave:3d} / {n_total}  ({n_si_leave/n_total*100:.1f}%)")
    print("  Started outside, finished inside (late recovery) :"
          f"  {n_so_in:3d} / {n_total}  ({n_so_in/n_total*100:.1f}%)")
    print("  Started outside, stayed outside                  :"
          f"  {n_so_out:3d} / {n_total}  ({n_so_out/n_total*100:.1f}%)")

    # Q1 answer
    print()
    q1_rate = n_si_so / n_total if n_total else 0
    print(f"  Q1  Full-hour containment (started+ended inside): {_pct(q1_rate)}")
    q2_rate = n_si_leave / sum(1 for r in summary if r["started_inside"]) if any(r["started_inside"] for r in summary) else None
    if q2_rate is not None:
        print(f"  Q2  Start-inside → broke out:                     {_pct(q2_rate)}")
    q3_den = sum(1 for r in summary if not r["started_inside"])
    q3_rate = n_so_in / q3_den if q3_den else None
    if q3_rate is not None:
        print(f"  Q3  Start-outside → moved inside by expiry:       {_pct(q3_rate)}")


def _section_q4_path(min_markets: int) -> None:
    _h2("Q4: Typical BTC path relative to center (by time bucket)")

    # Split contract life into deciles (0–10%, 10–20%, …, 90–100%)
    rows = fetch_all(
        """
        SELECT
            FLOOR(10 * contract_age_seconds /
                  NULLIF(contract_age_seconds + time_to_expiry_seconds, 0)) AS decile,
            AVG(distance_to_center)   AS avg_d_center,
            AVG(norm_position)        AS avg_norm_pos,
            AVG(inside_band)          AS pct_inside,
            COUNT(*)                  AS n
        FROM hourly_range_observations
        WHERE time_to_expiry_seconds IS NOT NULL
          AND contract_age_seconds   IS NOT NULL
        GROUP BY decile
        ORDER BY decile
        """
    )

    if not rows:
        print("  No observation data yet.")
        return

    print()
    print(f"  {'Decile':>8}  {'Pct inside':>10}  {'Avg dist-center':>16}  "
          f"{'Avg norm pos':>13}  {'N obs':>7}")
    _sep("-", 68)
    for r in rows:
        dec = int(r["decile"]) if r["decile"] is not None else -1
        label = f"{dec*10}–{dec*10+10}%" if dec >= 0 else "unknown"
        print(f"  {label:>8}  {_pct(r['pct_inside']):>10}  "
              f"${_f2(r['avg_d_center']):>15}  "
              f"{_f4(r['avg_norm_pos']):>13}  "
              f"{int(r['n']):>7,}")


def _section_q5_band_width(min_markets: int) -> None:
    _h2("Q5: Containment by band width bucket")

    rows = fetch_all(
        """
        SELECT
            FLOOR(band_width / 500) * 500   AS width_bucket,
            COUNT(*)                        AS n,
            AVG(contained)                  AS contain_rate,
            AVG(pct_time_inside)            AS avg_pct_inside,
            AVG(ABS(final_btc_price - band_center)) AS avg_dist_center_at_settle
        FROM hourly_range_markets
        WHERE status = 'settled'
        GROUP BY width_bucket
        ORDER BY width_bucket
        """
    )

    if not rows:
        print("  No settled markets yet.")
        return

    print()
    print(f"  {'Band $width':>12}  {'N':>5}  {'Containment':>12}  "
          f"{'Pct inside':>11}  {'Avg|dist center|':>17}")
    _sep("-", 70)
    for r in rows:
        bucket = float(r["width_bucket"] or 0)
        print(f"  ${bucket:>5,.0f}–${bucket+500:>5,.0f}"
              f"  {int(r['n']):>5}"
              f"  {_pct(r['contain_rate']):>12}"
              f"  {_pct(r['avg_pct_inside']):>11}"
              f"  ${_f2(r['avg_dist_center_at_settle']):>16}")

    # Also show by distance-to-center at first observation.
    print()
    print("  — Band center offset at market open (|BTC − center|) vs containment —")
    rows2 = fetch_all(
        """
        SELECT
            FLOOR(ABS(first_obs.distance_to_center) / 250) * 250  AS offset_bucket,
            COUNT(*)                                               AS n,
            AVG(hrm.contained)                                     AS contain_rate
        FROM hourly_range_markets hrm
        JOIN (
            SELECT market_ticker, distance_to_center
            FROM hourly_range_observations
            WHERE (market_ticker, observed_at) IN (
                SELECT market_ticker, MIN(observed_at)
                FROM hourly_range_observations
                GROUP BY market_ticker
            )
        ) first_obs ON first_obs.market_ticker = hrm.market_ticker
        WHERE hrm.status = 'settled'
        GROUP BY offset_bucket
        ORDER BY offset_bucket
        """
    )
    if rows2:
        print()
        print(f"  {'|Dist center| open':>20}  {'N':>5}  {'Containment':>12}")
        _sep("-", 44)
        for r in rows2:
            lo = float(r["offset_bucket"] or 0)
            print(f"  ${lo:>7,.0f}–${lo+250:>7,.0f}  {int(r['n']):>5}  "
                  f"{_pct(r['contain_rate']):>12}")


def _section_q6_volatility(min_markets: int) -> None:
    _h2("Q6: BTC volatility at market open vs containment outcome")

    # Join first observation's vol60 to outcome.
    rows = fetch_all(
        """
        SELECT
            FLOOR(COALESCE(first_obs.btc_volatility_60s, 0) / 50) * 50   AS vol_bucket,
            COUNT(*)                                                       AS n,
            AVG(hrm.contained)                                            AS contain_rate,
            AVG(first_obs.containment_confidence)                         AS avg_confidence
        FROM hourly_range_markets hrm
        JOIN (
            SELECT market_ticker, btc_volatility_60s, containment_confidence
            FROM hourly_range_observations
            WHERE (market_ticker, observed_at) IN (
                SELECT market_ticker, MIN(observed_at)
                FROM hourly_range_observations
                GROUP BY market_ticker
            )
        ) first_obs ON first_obs.market_ticker = hrm.market_ticker
        WHERE hrm.status = 'settled'
          AND first_obs.btc_volatility_60s IS NOT NULL
        GROUP BY vol_bucket
        ORDER BY vol_bucket
        """
    )

    if not rows:
        print("  No vol data yet (need more observations with btc_volatility_60s).")
        return

    print()
    print(f"  {'Vol60 bucket ($)':>18}  {'N':>5}  {'Actual contain':>15}  {'Avg confidence':>15}")
    _sep("-", 60)
    for r in rows:
        lo = float(r["vol_bucket"] or 0)
        print(f"  ${lo:>6,.0f}–${lo+50:>6,.0f}"
              f"  {int(r['n']):>5}"
              f"  {_pct(r['contain_rate']):>15}"
              f"  {_pct(r['avg_confidence']):>15}")

    print()
    print("  Note: confidence = Phi(z_cap)+Phi(z_floor)−1, vol projected forward.")
    print("  Low confidence at open should predict low containment.  Check alignment.")


def _section_excursions() -> None:
    _h2("Excursion stats (settled markets)")

    rows = fetch_all(
        """
        SELECT
            AVG(max_excursion_above_cap)   AS avg_above,
            MAX(max_excursion_above_cap)   AS max_above,
            AVG(max_excursion_below_floor) AS avg_below,
            MAX(max_excursion_below_floor) AS max_below,
            SUM(max_excursion_above_cap > 0)   AS n_ever_above,
            SUM(max_excursion_below_floor > 0) AS n_ever_below,
            COUNT(*) AS n
        FROM hourly_range_markets
        WHERE status = 'settled'
        """
    )
    r = rows[0] if rows else None
    if not r or not r["n"]:
        print("  No settled markets yet.")
        return

    n = int(r["n"])
    print()
    _row("Markets ever above cap:",
         f"{int(r['n_ever_above'] or 0)} / {n}  "
         f"({int(r['n_ever_above'] or 0)/n*100:.1f}%)")
    _row("  avg excursion above cap:", f"${_f2(r['avg_above'])}")
    _row("  max excursion above cap:", f"${_f2(r['max_above'])}")
    _row("Markets ever below floor:",
         f"{int(r['n_ever_below'] or 0)} / {n}  "
         f"({int(r['n_ever_below'] or 0)/n*100:.1f}%)")
    _row("  avg excursion below floor:", f"${_f2(r['avg_below'])}")
    _row("  max excursion below floor:", f"${_f2(r['max_below'])}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hourly range market research report."
    )
    parser.add_argument(
        "--min-markets", type=int, default=3,
        help="Minimum settled markets needed to show section (default: 3)",
    )
    parser.add_argument(
        "--band-width", type=float, default=None,
        help="Filter to a specific band width (e.g. 1000 for $1000-wide bands)",
    )
    args = parser.parse_args()

    total_settled = _count("hourly_range_markets", "status = 'settled'")
    total_obs     = _count("hourly_range_observations")

    print("\nBTC Hourly Range Market Research Report")
    print(f"  Settled markets: {total_settled}   Observations: {total_obs:,}")

    _section_overview(args.min_markets)
    _section_q1_q3(args.min_markets)
    _section_q4_path(args.min_markets)
    _section_q5_band_width(args.min_markets)
    _section_q6_volatility(args.min_markets)
    _section_excursions()

    print()


if __name__ == "__main__":
    main()
