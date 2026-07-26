#!/usr/bin/env python3
"""
Build a descriptive daily time-window scalp opportunity report.

Question:
  Is there a recurring ET time window where one high-quality Kalshi BTC 15-minute
  scalp opportunity appears often enough to justify prospective paper testing?

This is a historical diagnostic. It does not create, update, or delete database
rows. It intentionally reports best observed opportunities by market/day/window;
that is descriptive/falsification work, not a live-entry strategy.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "daily_time_window_scalp"
ET = ZoneInfo("America/New_York")

QUOTE_TIMELINE_SQL = """
SELECT
  ms.id AS snapshot_id,
  m.id AS market_pk,
  m.market_id AS market_ticker,
  m.opens_at,
  m.closes_at,
  m.target_price AS strike,
  m.status AS market_status,
  ms.captured_at,
  ms.btc_price,
  ms.time_remaining_seconds,
  ms.source AS btc_source,
  MAX(CASE WHEN c.side = 'YES' THEN cs.bid_price END) AS yes_bid,
  MAX(CASE WHEN c.side = 'YES' THEN cs.ask_price END) AS yes_ask,
  MAX(CASE WHEN c.side = 'YES' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS yes_spread,
  MAX(CASE WHEN c.side = 'NO' THEN cs.bid_price END) AS no_bid,
  MAX(CASE WHEN c.side = 'NO' THEN cs.ask_price END) AS no_ask,
  MAX(CASE WHEN c.side = 'NO' THEN COALESCE(cs.spread, cs.ask_price - cs.bid_price) END) AS no_spread
FROM market_snapshots ms
JOIN markets m ON m.id = ms.market_id
JOIN contract_snapshots cs ON cs.market_snapshot_id = ms.id
JOIN contracts c ON c.id = cs.contract_id
WHERE m.market_id LIKE %s
  AND m.opens_at IS NOT NULL
  AND m.closes_at IS NOT NULL
  AND m.target_price IS NOT NULL
  AND ms.btc_price IS NOT NULL
  AND ms.captured_at >= COALESCE(%s, ms.captured_at)
  AND ms.captured_at < COALESCE(%s, ms.captured_at + INTERVAL 1 SECOND)
GROUP BY
  ms.id, m.id, m.market_id, m.opens_at, m.closes_at, m.target_price,
  m.status, ms.captured_at, ms.btc_price, ms.time_remaining_seconds, ms.source
ORDER BY m.id, ms.captured_at
"""


@dataclass(frozen=True)
class Paths:
    directory: Path
    market_csv: Path
    window_summary_csv: Path
    top_windows_csv: Path
    data_quality_csv: Path
    markdown_report: Path


def _f(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return float(value)


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    raise TypeError(f"unsupported datetime value: {value!r}")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(sep=" ")


def _avg(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _median(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    return round(statistics.median(vals), 4)


def _pct(num: float, den: float) -> float | None:
    if den == 0:
        return None
    return round(100.0 * num / den, 1)


def _fee_cents(price: float, fee_rate_cents: float) -> float:
    price = max(0.0, min(1.0, price))
    return round(fee_rate_cents * price * (1.0 - price), 6)


def _profit_factor(values: Iterable[float | None]) -> float | str | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    if losses == 0:
        return "inf" if gains > 0 else None
    return round(gains / losses, 4)


def _max_abs_window_move(rows: list[dict[str, Any]], seconds: int) -> float | None:
    if len(rows) < 2:
        return None
    best = None
    j = 0
    for i, row in enumerate(rows):
        target = row["captured_at"].timestamp() - seconds
        while j + 1 < i and rows[j + 1]["captured_at"].timestamp() <= target:
            j += 1
        if j < i:
            diff = abs(row["btc_price"] - rows[j]["btc_price"])
            best = diff if best is None else max(best, diff)
    return round(best, 4) if best is not None else None


def _max_dominant_reprice(rows: list[dict[str, Any]], seconds: int) -> float | None:
    best = None
    j = 0
    for i, row in enumerate(rows):
        target = row["captured_at"].timestamp() - seconds
        while j + 1 < i and rows[j + 1]["captured_at"].timestamp() <= target:
            j += 1
        if j >= i:
            continue
        side = _dominant_side(row)
        if side is None:
            continue
        old = _price(rows[j], side, "ask")
        now = _price(row, side, "ask")
        if old is None or now is None:
            continue
        move = abs(now - old) * 100.0
        best = move if best is None else max(best, move)
    return round(best, 4) if best is not None else None


def _price(row: dict[str, Any], side: str, kind: str) -> float | None:
    return row.get(f"{side.lower()}_{kind}")


def _dominant_side(row: dict[str, Any]) -> str | None:
    yes = row.get("yes_ask")
    no = row.get("no_ask")
    if yes is None or no is None:
        return None
    if yes > no:
        return "YES"
    if no > yes:
        return "NO"
    return None


def _minority_side(side: str) -> str:
    return "NO" if side == "YES" else "YES"


def _is_clean_side(row: dict[str, Any], side: str, max_spread: float) -> bool:
    bid = _price(row, side, "bid")
    ask = _price(row, side, "ask")
    spread = _price(row, side, "spread")
    if bid is None or ask is None or spread is None:
        return False
    if bid < 0 or ask < 0 or bid > 1 or ask > 1:
        return False
    if bid > ask:
        return False
    if spread <= 0 or spread > max_spread:
        return False
    return True


def _clean_row(row: dict[str, Any], max_spread: float) -> bool:
    return _is_clean_side(row, "YES", max_spread) and _is_clean_side(row, "NO", max_spread)


def _realized_volatility(rows: list[dict[str, Any]]) -> float | None:
    if len(rows) < 3:
        return None
    deltas = [cur["btc_price"] - prev["btc_price"] for prev, cur in zip(rows, rows[1:])]
    if len(deltas) < 2:
        return None
    return round(statistics.pstdev(deltas), 4)


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 4)


def _best_scalps(rows: list[dict[str, Any]], max_spread: float, fee_rate_cents: float) -> dict[str, Any]:
    best_gross = None
    best_net = None
    best_net_side = None
    best_net_entry_at = None
    best_net_exit_at = None
    largest_minority_rebound = None

    for side in ("YES", "NO"):
        best_future_bid = None
        best_future_at = None
        for row in reversed(rows):
            if _is_clean_side(row, side, max_spread):
                bid = _price(row, side, "bid")
                if bid is not None and (best_future_bid is None or bid > best_future_bid):
                    best_future_bid = bid
                    best_future_at = row["captured_at"]

            if not _is_clean_side(row, side, max_spread):
                continue
            ask = _price(row, side, "ask")
            if ask is None or best_future_bid is None or best_future_at is None or best_future_at <= row["captured_at"]:
                continue
            gross = round((best_future_bid - ask) * 100.0, 4)
            net = round(gross - _fee_cents(ask, fee_rate_cents) - _fee_cents(best_future_bid, fee_rate_cents), 4)
            if best_gross is None or gross > best_gross:
                best_gross = gross
            if best_net is None or net > best_net:
                best_net = net
                best_net_side = side
                best_net_entry_at = row["captured_at"]
                best_net_exit_at = best_future_at

    best_future_bid_by_side: dict[str, float | None] = {"YES": None, "NO": None}
    for row in reversed(rows):
        dom = _dominant_side(row)
        if dom is None:
            pass
        else:
            side = _minority_side(dom)
            if _is_clean_side(row, side, max_spread):
                entry_bid = _price(row, side, "bid")
                future_bid = best_future_bid_by_side[side]
                if entry_bid is not None and future_bid is not None:
                    rebound = (future_bid - entry_bid) * 100.0
                    largest_minority_rebound = rebound if largest_minority_rebound is None else max(largest_minority_rebound, rebound)

        for side in ("YES", "NO"):
            if _is_clean_side(row, side, max_spread):
                bid = _price(row, side, "bid")
                if bid is not None and (best_future_bid_by_side[side] is None or bid > best_future_bid_by_side[side]):
                    best_future_bid_by_side[side] = bid

    return {
        "largest_minority_contract_rebound_cents": round(largest_minority_rebound, 4) if largest_minority_rebound is not None else None,
        "maximum_clean_executable_gross_scalp_cents": best_gross,
        "maximum_clean_executable_net_scalp_cents": best_net,
        "best_net_scalp_side": best_net_side,
        "best_net_scalp_entry_at_utc": _iso(best_net_entry_at),
        "best_net_scalp_exit_at_utc": _iso(best_net_exit_at),
    }


def _market_row(rows: list[dict[str, Any]], max_spread: float, fee_rate_cents: float) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: row["captured_at"])
    first = rows[0]
    last = rows[-1]
    opens_at = first["opens_at"]
    closes_at = first["closes_at"]
    open_et = opens_at.astimezone(ET)
    close_et = closes_at.astimezone(ET)
    btc_prices = [row["btc_price"] for row in rows]
    spreads = []
    clean_rows = 0
    for row in rows:
        if _clean_row(row, max_spread):
            clean_rows += 1
        for side in ("YES", "NO"):
            spread = _price(row, side, "spread")
            if spread is not None and spread >= 0:
                spreads.append(spread)

    btc_open = first["btc_price"]
    btc_close = last["btc_price"]
    scalp = _best_scalps(rows, max_spread, fee_rate_cents)

    out = {
        "market_ticker": first["market_ticker"],
        "market_open_time_utc": _iso(opens_at),
        "market_close_time_utc": _iso(closes_at),
        "market_open_time_et": _iso(open_et),
        "market_close_time_et": _iso(close_et),
        "hour_of_day_et": f"{open_et.hour:02d}:00 ET",
        "hour_int_et": open_et.hour,
        "date_et": open_et.date().isoformat(),
        "weekday": open_et.strftime("%A"),
        "is_weekend": int(open_et.weekday() >= 5),
        "btc_open": round(btc_open, 4),
        "btc_close": round(btc_close, 4),
        "btc_high": round(max(btc_prices), 4),
        "btc_low": round(min(btc_prices), 4),
        "btc_open_to_close_move": round(btc_close - btc_open, 4),
        "btc_absolute_open_to_close_move": round(abs(btc_close - btc_open), 4),
        "btc_maximum_upward_excursion": round(max(btc_prices) - btc_open, 4),
        "btc_maximum_downward_excursion": round(min(btc_prices) - btc_open, 4),
        "btc_realized_volatility": _realized_volatility(rows),
        "largest_30s_btc_move": _max_abs_window_move(rows, 30),
        "largest_60s_btc_move": _max_abs_window_move(rows, 60),
        "largest_180s_btc_move": _max_abs_window_move(rows, 180),
        "largest_30s_dominant_contract_repricing_cents": _max_dominant_reprice(rows, 30),
        "largest_60s_dominant_contract_repricing_cents": _max_dominant_reprice(rows, 60),
        "clean_quote_coverage_pct": _pct(clean_rows, len(rows)),
        "median_spread": _median(spreads),
        "minimum_spread": min(spreads) if spreads else None,
        "quote_count": len(rows),
    }
    out.update(scalp)
    return out


def _windows_for_market(row: dict[str, Any]) -> list[tuple[str, str]]:
    hour = int(row["hour_int_et"])
    out = [("hourly", f"{hour:02d}:00-{(hour + 1) % 24:02d}:00 ET")]
    for span in (2, 3):
        for start in range(24):
            if _hour_in_window(hour, start, span):
                out.append((f"{span}h_rolling", f"{start:02d}:00-{(start + span) % 24:02d}:00 ET"))
    if 0 <= hour < 17:
        out.append(("custom", "00:00-17:00 ET"))
    return out


def _hour_in_window(hour: int, start: int, span: int) -> bool:
    end = start + span
    if end <= 24:
        return start <= hour < end
    return hour >= start or hour < (end % 24)


def _summarize_window(window_type: str, window_label: str, rows: list[dict[str, Any]], all_dates: list[str]) -> dict[str, Any]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_day[row["date_et"]].append(row)

    days = sorted(by_day)
    best_by_day = []
    for day in days:
        clean = [row for row in by_day[day] if row.get("maximum_clean_executable_net_scalp_cents") is not None]
        if clean:
            best_by_day.append(max(clean, key=lambda row: row["maximum_clean_executable_net_scalp_cents"]))
        else:
            best_by_day.append(max(by_day[day], key=lambda row: row.get("quote_count") or 0))

    ordered_dates = sorted(all_dates)
    split = len(ordered_dates) // 2
    first_dates = set(ordered_dates[:split])
    second_dates = set(ordered_dates[split:])
    first_best = [row for row in best_by_day if row["date_et"] in first_dates]
    second_best = [row for row in best_by_day if row["date_et"] in second_dates]
    weekday_best = [row for row in best_by_day if not row["is_weekend"]]
    weekend_best = [row for row in best_by_day if row["is_weekend"]]

    net = [row.get("maximum_clean_executable_net_scalp_cents") for row in best_by_day]
    gross = [row.get("maximum_clean_executable_gross_scalp_cents") for row in best_by_day]
    valid_net = [v for v in net if v is not None]
    worst = min(best_by_day, key=lambda row: row.get("maximum_clean_executable_net_scalp_cents") if row.get("maximum_clean_executable_net_scalp_cents") is not None else -999999)
    best = max(best_by_day, key=lambda row: row.get("maximum_clean_executable_net_scalp_cents") if row.get("maximum_clean_executable_net_scalp_cents") is not None else -999999)
    first_avg = _avg(row.get("maximum_clean_executable_net_scalp_cents") for row in first_best)
    second_avg = _avg(row.get("maximum_clean_executable_net_scalp_cents") for row in second_best)

    return {
        "window_type": window_type,
        "window_label": window_label,
        "calendar_days_observed": len(days),
        "markets_observed": len(rows),
        "average_markets_per_day": round(len(rows) / len(days), 4) if days else None,
        "average_btc_absolute_move": _avg(row.get("btc_absolute_open_to_close_move") for row in rows),
        "median_btc_absolute_move": _median(row.get("btc_absolute_open_to_close_move") for row in rows),
        "average_realized_volatility": _avg(row.get("btc_realized_volatility") for row in rows),
        "median_realized_volatility": _median(row.get("btc_realized_volatility") for row in rows),
        "days_with_at_least_one_3c_gross_scalp_pct": _pct(sum((v or -999) >= 3 for v in gross), len(best_by_day)),
        "days_with_at_least_one_5c_gross_scalp_pct": _pct(sum((v or -999) >= 5 for v in gross), len(best_by_day)),
        "days_with_at_least_one_net_positive_scalp_pct": _pct(sum((v or -999) > 0 for v in net), len(best_by_day)),
        "days_with_at_least_one_net_scalp_above_2c_pct": _pct(sum((v or -999) > 2 for v in net), len(best_by_day)),
        "average_best_gross_scalp_per_day": _avg(gross),
        "average_best_net_scalp_per_day": _avg(net),
        "median_best_net_scalp_per_day": _median(net),
        "worst_day": worst.get("date_et"),
        "worst_day_best_net_scalp": worst.get("maximum_clean_executable_net_scalp_cents"),
        "best_day": best.get("date_et"),
        "best_day_best_net_scalp": best.get("maximum_clean_executable_net_scalp_cents"),
        "chronological_first_half_avg_best_net": first_avg,
        "chronological_second_half_avg_best_net": second_avg,
        "chronological_half_gap_abs": round(abs((first_avg or 0) - (second_avg or 0)), 4) if first_avg is not None and second_avg is not None else None,
        "weekday_days": len(weekday_best),
        "weekday_avg_best_net": _avg(row.get("maximum_clean_executable_net_scalp_cents") for row in weekday_best),
        "weekend_days": len(weekend_best),
        "weekend_avg_best_net": _avg(row.get("maximum_clean_executable_net_scalp_cents") for row in weekend_best),
        "best_opportunity_profit_factor": _profit_factor(valid_net),
        "best_opportunity_drawdown_cents": _max_drawdown([v for v in valid_net if v is not None]),
        "small_sample_flag": int(len(days) < 5 or len(rows) < 30),
    }


def _window_summaries(market_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in market_rows:
        for key in _windows_for_market(row):
            windows[key].append(row)
    all_dates = sorted({row["date_et"] for row in market_rows})
    summaries = [_summarize_window(wt, label, rows, all_dates) for (wt, label), rows in windows.items()]
    return sorted(summaries, key=lambda row: (row["window_type"], row["window_label"]))


def _top_windows(summaries: list[dict[str, Any]], min_days: int) -> list[dict[str, Any]]:
    candidates = [row for row in summaries if row["calendar_days_observed"] >= min_days and row["markets_observed"] >= 30]
    ranked = sorted(
        candidates,
        key=lambda row: (
            -(row.get("average_best_net_scalp_per_day") or -999999),
            -(row.get("days_with_at_least_one_net_positive_scalp_pct") or -999999),
            row.get("chronological_half_gap_abs") if row.get("chronological_half_gap_abs") is not None else 999999,
            -row["calendar_days_observed"],
        ),
    )
    out = []
    for rank, row in enumerate(ranked[:5], start=1):
        item = dict(row)
        item["rank"] = rank
        item["ranking_basis"] = "avg_best_net_per_day, net_positive_day_pct, half_stability, sample_size"
        out.append(item)
    return out


def _data_quality(raw_rows: list[dict[str, Any]], rows_by_market: dict[int, list[dict[str, Any]]], market_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []

    def add(issue: str, rows_affected: int, latest_seen: str | None = None) -> None:
        if rows_affected:
            out.append({"issue": issue, "rows_affected": rows_affected, "latest_seen": latest_seen})

    add("quote_age_ms_unavailable_in_historical_snapshots", len(raw_rows), _iso(max((row["captured_at"] for row in raw_rows), default=None)))
    add("raw_yes_bid_gt_ask", sum(row.get("yes_bid") is not None and row.get("yes_ask") is not None and row["yes_bid"] > row["yes_ask"] for row in raw_rows))
    add("raw_no_bid_gt_ask", sum(row.get("no_bid") is not None and row.get("no_ask") is not None and row["no_bid"] > row["no_ask"] for row in raw_rows))
    add("raw_yes_spread_eq_zero", sum(row.get("yes_spread") == 0 for row in raw_rows))
    add("raw_no_spread_eq_zero", sum(row.get("no_spread") == 0 for row in raw_rows))
    add("raw_yes_spread_lt_zero", sum(row.get("yes_spread") is not None and row["yes_spread"] < 0 for row in raw_rows))
    add("raw_no_spread_lt_zero", sum(row.get("no_spread") is not None and row["no_spread"] < 0 for row in raw_rows))
    add("markets_with_fewer_than_10_quotes", sum(row["quote_count"] < 10 for row in market_rows))
    add("markets_loaded", len(rows_by_market))
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _table(rows: list[dict[str, Any]], cols: list[str], limit: int = 10) -> str:
    if not rows:
        return "_No rows._"
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in cols) + " |")
    return "\n".join(lines)


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"daily_time_window_scalp_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        market_csv=output_dir / f"{stem}_markets.csv",
        window_summary_csv=output_dir / f"{stem}_window_summary.csv",
        top_windows_csv=output_dir / f"{stem}_top_windows.csv",
        data_quality_csv=output_dir / f"{stem}_data_quality.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def _load_rows(market_like: str, start: str | None, end: str | None) -> list[dict[str, Any]]:
    from app.db import get_pool

    conn = get_pool().get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(QUOTE_TIMELINE_SQL, (market_like, start, end))
        rows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()

    out = []
    for row in rows:
        item = dict(row)
        for key in ("strike", "btc_price", "yes_bid", "yes_ask", "yes_spread", "no_bid", "no_ask", "no_spread"):
            item[key] = _f(item.get(key))
        for key in ("opens_at", "closes_at", "captured_at"):
            item[key] = _dt(item.get(key))
        out.append(item)
    return out


def _render_markdown(paths: Paths, market_rows: list[dict[str, Any]], summaries: list[dict[str, Any]], top: list[dict[str, Any]], dq: list[dict[str, Any]], max_spread: float, fee_rate_cents: float) -> str:
    eligible = [row for row in summaries if row["small_sample_flag"] == 0]
    best = top[0] if top else None
    conclusion = "No adequately sampled recurring time window passed the ranking filters."
    if best:
        conclusion = (
            f"Best descriptive candidate: `{best['window_type']} {best['window_label']}` with "
            f"avg best net `{best.get('average_best_net_scalp_per_day')}`c/day, "
            f"net-positive days `{best.get('days_with_at_least_one_net_positive_scalp_pct')}`%, "
            f"and half gap `{best.get('chronological_half_gap_abs')}`c."
        )

    return f"""# Daily Time-Window Scalp Opportunity Report

## Scope

- Market: Kalshi BTC 15-minute contracts
- Unit of analysis: one canonical summary row per market
- Time windows: hourly, rolling 2-hour, rolling 3-hour, and 00:00-17:00 ET
- Main evaluation: one best observed executable opportunity per day per window
- Clean executable model: entry at ask, exit later at bid, spread <= `{max_spread}`, fee cents per side = `{fee_rate_cents} * price * (1 - price)`
- This is descriptive and uses future path data to score whether an opportunity existed. It is not a live-entry strategy.

## Direct Answer

{conclusion}

Treat this as a paper-test filter only if the top window has enough days, positive net opportunity rate, and reasonably stable first-half/second-half behavior.

## Dataset

- Markets summarized: `{len(market_rows)}`
- Calendar days: `{len({row['date_et'] for row in market_rows})}`
- Adequately sampled windows: `{len(eligible)}`

## Top Windows

{_table(top, ["rank", "window_type", "window_label", "calendar_days_observed", "markets_observed", "average_best_net_scalp_per_day", "days_with_at_least_one_net_positive_scalp_pct", "chronological_first_half_avg_best_net", "chronological_second_half_avg_best_net", "chronological_half_gap_abs"], 5)}

## Window Summary Preview

{_table(sorted(summaries, key=lambda row: -(row.get("average_best_net_scalp_per_day") or -999999)), ["window_type", "window_label", "calendar_days_observed", "markets_observed", "average_btc_absolute_move", "average_realized_volatility", "days_with_at_least_one_3c_gross_scalp_pct", "days_with_at_least_one_net_positive_scalp_pct", "average_best_net_scalp_per_day", "small_sample_flag"], 15)}

## Data Quality

{_table(dq, ["issue", "rows_affected", "latest_seen"], 20)}

## Output Files

- Market CSV: `{paths.market_csv}`
- Window summary CSV: `{paths.window_summary_csv}`
- Top windows CSV: `{paths.top_windows_csv}`
- Data quality CSV: `{paths.data_quality_csv}`

## Research Warning

This report selects the best observed opportunity per day per window after the fact. A strong row here can justify prospective paper testing, but it does not prove a tradable live edge.
"""


def build_report(output_dir: Path, market_like: str, start: str | None, end: str | None, max_spread: float, fee_rate_cents: float, min_days: int) -> Paths:
    paths = _paths(output_dir)
    raw_rows = _load_rows(market_like, start, end)
    rows_by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        rows_by_market[int(row["market_pk"])].append(row)
    for rows in rows_by_market.values():
        rows.sort(key=lambda row: row["captured_at"])

    market_rows = [_market_row(rows, max_spread, fee_rate_cents) for rows in rows_by_market.values() if rows]
    market_rows.sort(key=lambda row: (row["market_open_time_utc"], row["market_ticker"]))
    summaries = _window_summaries(market_rows)
    top = _top_windows(summaries, min_days)
    dq = _data_quality(raw_rows, rows_by_market, market_rows)

    _write_csv(paths.market_csv, market_rows)
    _write_csv(paths.window_summary_csv, summaries)
    _write_csv(paths.top_windows_csv, top)
    _write_csv(paths.data_quality_csv, dq)
    paths.markdown_report.write_text(_render_markdown(paths, market_rows, summaries, top, dq, max_spread, fee_rate_cents))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Build daily time-window scalp opportunity report")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M-%")
    parser.add_argument("--start", default=None, help="UTC start datetime, e.g. 2026-07-01 00:00:00")
    parser.add_argument("--end", default=None, help="UTC end datetime, e.g. 2026-07-26 00:00:00")
    parser.add_argument("--max-spread", type=float, default=0.01)
    parser.add_argument("--fee-rate-cents", type=float, default=7.0)
    parser.add_argument("--min-days", type=int, default=5)
    args = parser.parse_args()

    paths = build_report(
        output_dir=args.output_dir,
        market_like=args.market_like,
        start=args.start,
        end=args.end,
        max_spread=args.max_spread,
        fee_rate_cents=args.fee_rate_cents,
        min_days=args.min_days,
    )
    print("Daily time-window scalp report complete")
    print(f"market_csv={paths.market_csv}")
    print(f"window_summary_csv={paths.window_summary_csv}")
    print(f"top_windows_csv={paths.top_windows_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
