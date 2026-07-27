#!/usr/bin/env python3
"""
Live-style one-trade-per-day pre-entry rule test for 08:00-11:00 ET.

This is a falsification report. Each rule is evaluated chronologically and may
take at most the first valid signal per ET calendar day. Exits are modeled at
fixed horizons using ask entry, bid exit, and estimated Kalshi fees.

The default thresholds are frozen before evaluation by this script, but they are
marked exploratory because they were not proven by an independent prospective
test set.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.research_daily_time_window_scalp_report import (  # noqa: E402
    ET,
    _avg,
    _clean_row,
    _dominant_side,
    _fee_cents,
    _is_clean_side,
    _load_rows,
    _market_row,
    _max_drawdown,
    _median,
    _minority_side,
    _pct,
    _price,
    _profit_factor,
)

ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "live_style_time_window_rule_test"
HORIZONS = (60, 90, 120)


@dataclass(frozen=True)
class RuleConfig:
    rule_name: str
    entry_side_mode: str
    description: str
    threshold_summary: str
    exploratory_status: str = "exploratory_fixed"


@dataclass(frozen=True)
class Paths:
    directory: Path
    trade_csv: Path
    daily_pnl_csv: Path
    summary_csv: Path
    rule_config_csv: Path
    skipped_days_csv: Path
    threshold_grid_csv: Path
    hindsight_comparison_csv: Path
    data_quality_csv: Path
    markdown_report: Path


def _last_at_or_before(rows: list[dict[str, Any]], ts: datetime) -> dict[str, Any] | None:
    best = None
    for row in rows:
        if row["captured_at"] <= ts:
            best = row
        else:
            break
    return best


def _first_at_or_after(rows: list[dict[str, Any]], ts: datetime, tolerance_seconds: int) -> dict[str, Any] | None:
    max_ts = ts + timedelta(seconds=tolerance_seconds)
    for row in rows:
        if ts <= row["captured_at"] <= max_ts:
            return row
    return None


def _side_for_btc_move(delta: float | None) -> str | None:
    if delta is None:
        return None
    if delta > 0:
        return "YES"
    if delta < 0:
        return "NO"
    return None


def _side_distance(row: dict[str, Any], side: str) -> float:
    strike = row["strike"]
    return row["btc_price"] - strike if side == "YES" else strike - row["btc_price"]


def _contract_reprice(row: dict[str, Any], prev: dict[str, Any] | None, side: str) -> float | None:
    if prev is None:
        return None
    now = _price(row, side, "ask")
    old = _price(prev, side, "ask")
    if now is None or old is None:
        return None
    return round((now - old) * 100.0, 4)


def _btc_delta(row: dict[str, Any], prev: dict[str, Any] | None) -> float | None:
    if prev is None:
        return None
    return round(row["btc_price"] - prev["btc_price"], 4)


def _clean_entry(row: dict[str, Any], side: str, max_spread: float) -> bool:
    return _clean_row(row, max_spread) and _is_clean_side(row, side, max_spread)


def _entry_payload(rule: RuleConfig, row: dict[str, Any], side: str, prev30: dict[str, Any] | None, prev60: dict[str, Any] | None, open_row: dict[str, Any] | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    dom = _dominant_side(row)
    min_side = _minority_side(dom) if dom else None
    payload = {
        "rule_name": rule.rule_name,
        "entry_side": side,
        "entry_side_mode": rule.entry_side_mode,
        "dominant_side": dom,
        "minority_side": min_side,
        "btc_delta_60s": _btc_delta(row, prev60),
        "btc_abs_delta_60s": abs(_btc_delta(row, prev60)) if _btc_delta(row, prev60) is not None else None,
        "dominant_change_prev_30s_cents": _contract_reprice(row, prev30, dom) if dom else None,
        "entry_side_change_prev_30s_cents": _contract_reprice(row, prev30, side),
        "dominant_ask": _price(row, dom, "ask") if dom else None,
        "minority_ask": _price(row, min_side, "ask") if min_side else None,
        "entry_side_distance": round(_side_distance(row, side), 4),
        "abs_btc_distance": round(abs(row["btc_price"] - row["strike"]), 4),
        "time_since_open_seconds": round((row["captured_at"] - row["opens_at"]).total_seconds(), 3),
    }
    if open_row and dom:
        open_dom = _price(open_row, dom, "ask")
        now_dom = _price(row, dom, "ask")
        if open_dom is not None and now_dom is not None:
            elapsed = max(1.0, (row["captured_at"] - open_row["captured_at"]).total_seconds())
            payload["dominant_cents_per_second_since_open"] = round(((now_dom - open_dom) * 100.0) / elapsed, 6)
    if extra:
        payload.update(extra)
    return payload


def _rule_a(row: dict[str, Any], market_rows: list[dict[str, Any]], max_spread: float, btc_60s_abs_threshold: float) -> dict[str, Any] | None:
    prev60 = _last_at_or_before(market_rows, row["captured_at"] - timedelta(seconds=60))
    delta = _btc_delta(row, prev60)
    side = _side_for_btc_move(delta)
    if side is None or abs(delta or 0) < btc_60s_abs_threshold or not _clean_entry(row, side, max_spread):
        return None
    prev30 = _last_at_or_before(market_rows, row["captured_at"] - timedelta(seconds=30))
    open_row = _last_at_or_before(market_rows, row["opens_at"] + timedelta(seconds=20)) or market_rows[0]
    return _entry_payload(RULE_CONFIGS["rule_a_btc_60s_abs_move"], row, side, prev30, prev60, open_row)


def _rule_b(row: dict[str, Any], market_rows: list[dict[str, Any]], max_spread: float, dominant_30s_reprice_threshold_cents: float) -> dict[str, Any] | None:
    dom = _dominant_side(row)
    if dom is None:
        return None
    prev30 = _last_at_or_before(market_rows, row["captured_at"] - timedelta(seconds=30))
    reprice = _contract_reprice(row, prev30, dom)
    if reprice is None or reprice < dominant_30s_reprice_threshold_cents or not _clean_entry(row, dom, max_spread):
        return None
    prev60 = _last_at_or_before(market_rows, row["captured_at"] - timedelta(seconds=60))
    open_row = _last_at_or_before(market_rows, row["opens_at"] + timedelta(seconds=20)) or market_rows[0]
    return _entry_payload(RULE_CONFIGS["rule_b_dominant_30s_reprice"], row, dom, prev30, prev60, open_row)


def _rule_c(row: dict[str, Any], market_rows: list[dict[str, Any]], max_spread: float) -> dict[str, Any] | None:
    dom = _dominant_side(row)
    if dom is None:
        return None
    min_side = _minority_side(dom)
    dom_ask = _price(row, dom, "ask")
    if dom_ask is None or dom_ask < 0.65 or dom_ask >= 0.70:
        return None
    prev30 = _last_at_or_before(market_rows, row["captured_at"] - timedelta(seconds=30))
    reprice = _contract_reprice(row, prev30, dom)
    if reprice is None or reprice <= 9.0 or reprice > 15.0:
        return None
    open_row = _last_at_or_before(market_rows, row["opens_at"] + timedelta(seconds=20)) or market_rows[0]
    open_dom = _price(open_row, dom, "ask")
    if open_dom is None:
        return None
    elapsed = max(1.0, (row["captured_at"] - open_row["captured_at"]).total_seconds())
    speed = ((dom_ask - open_dom) * 100.0) / elapsed
    if speed < 0.15 or speed >= 0.30 or not _clean_entry(row, min_side, max_spread):
        return None
    prev60 = _last_at_or_before(market_rows, row["captured_at"] - timedelta(seconds=60))
    return _entry_payload(RULE_CONFIGS["rule_c_fast_dominance_moderate_reprice"], row, min_side, prev30, prev60, open_row)


def _rule_d(row: dict[str, Any], market_rows: list[dict[str, Any]], max_spread: float, window_start: datetime, bounce_confirm_cents: float, min_seconds_after_low: float) -> dict[str, Any] | None:
    dom = _dominant_side(row)
    if dom is None:
        return None
    min_side = _minority_side(dom)
    dom_ask = _price(row, dom, "ask")
    current_bid = _price(row, min_side, "bid")
    if dom_ask is None or current_bid is None or dom_ask < 0.65 or dom_ask >= 0.70:
        return None
    if not _clean_entry(row, min_side, max_spread):
        return None
    prior = [
        r for r in market_rows
        if window_start <= r["captured_at"] <= row["captured_at"] and _is_clean_side(r, min_side, max_spread)
    ]
    if len(prior) < 2:
        return None
    low_row = min(prior, key=lambda r: _price(r, min_side, "bid") if _price(r, min_side, "bid") is not None else 999)
    low_bid = _price(low_row, min_side, "bid")
    if low_bid is None:
        return None
    seconds_after_low = (row["captured_at"] - low_row["captured_at"]).total_seconds()
    if seconds_after_low < min_seconds_after_low:
        return None
    if (current_bid - low_bid) * 100.0 < bounce_confirm_cents:
        return None
    prev30 = _last_at_or_before(market_rows, row["captured_at"] - timedelta(seconds=30))
    prev60 = _last_at_or_before(market_rows, row["captured_at"] - timedelta(seconds=60))
    open_row = _last_at_or_before(market_rows, row["opens_at"] + timedelta(seconds=20)) or market_rows[0]
    return _entry_payload(
        RULE_CONFIGS["rule_d_minority_local_low_bounce"],
        row,
        min_side,
        prev30,
        prev60,
        open_row,
        {
            "minority_local_low_bid": low_bid,
            "minority_bid_bounce_from_low_cents": round((current_bid - low_bid) * 100.0, 4),
            "seconds_after_local_low": round(seconds_after_low, 3),
        },
    )


RULE_CONFIGS = {
    "rule_a_btc_60s_abs_move": RuleConfig(
        "rule_a_btc_60s_abs_move",
        "btc_60s_direction",
        "First clean 08:00-11:00 ET signal where absolute BTC 60-second move is at least $50; buy side BTC moved toward.",
        "btc_abs_delta_60s >= 50",
    ),
    "rule_b_dominant_30s_reprice": RuleConfig(
        "rule_b_dominant_30s_reprice",
        "dominant_continuation",
        "First clean signal where dominant ask repriced at least +10c over prior 30 seconds; buy dominant side.",
        "dominant_change_prev_30s_cents >= 10",
    ),
    "rule_c_fast_dominance_moderate_reprice": RuleConfig(
        "rule_c_fast_dominance_moderate_reprice",
        "minority_rebound_immediate",
        "First clean minority entry where dominant ask is 65-70c, speed is 0.15-0.30c/s, and dominant 30s reprice is >9c to <=15c.",
        "dominant_ask [0.65,0.70), speed [0.15,0.30), dominant_30s_reprice (9,15]",
    ),
    "rule_d_minority_local_low_bounce": RuleConfig(
        "rule_d_minority_local_low_bounce",
        "minority_local_low_bounce",
        "First clean minority entry where dominant ask is 65-70c and minority bid has bounced +2c from its 08:00-11:00 ET local low after at least 4 seconds.",
        "dominant_ask [0.65,0.70), minority_bid >= local_low_bid + 2c, seconds_after_low >= 4",
    ),
}


def _in_entry_window(ts: datetime, start_hour: int, end_hour: int) -> bool:
    et = ts.astimezone(ET)
    return start_hour <= et.hour < end_hour


def _find_daily_signal(
    day_rows: list[dict[str, Any]],
    rows_by_market: dict[int, list[dict[str, Any]]],
    rule_name: str,
    max_spread: float,
    start_hour: int,
    btc_60s_abs_threshold: float,
    dominant_30s_reprice_threshold_cents: float,
    bounce_confirm_cents: float,
    min_seconds_after_low: float,
) -> dict[str, Any] | None:
    for row in sorted(day_rows, key=lambda item: item["captured_at"]):
        market_rows = rows_by_market[int(row["market_pk"])]
        window_start_et = row["captured_at"].astimezone(ET).replace(hour=start_hour, minute=0, second=0, microsecond=0)
        window_start = window_start_et.astimezone(row["captured_at"].tzinfo)
        if rule_name == "rule_a_btc_60s_abs_move":
            sig = _rule_a(row, market_rows, max_spread, btc_60s_abs_threshold)
        elif rule_name == "rule_b_dominant_30s_reprice":
            sig = _rule_b(row, market_rows, max_spread, dominant_30s_reprice_threshold_cents)
        elif rule_name == "rule_c_fast_dominance_moderate_reprice":
            sig = _rule_c(row, market_rows, max_spread)
        elif rule_name == "rule_d_minority_local_low_bounce":
            sig = _rule_d(row, market_rows, max_spread, window_start, bounce_confirm_cents, min_seconds_after_low)
        else:
            raise ValueError(f"unknown rule {rule_name}")
        if sig is not None:
            sig["market_pk"] = row["market_pk"]
            sig["market_ticker"] = row["market_ticker"]
            sig["entry_at"] = row["captured_at"]
            sig["entry_at_et"] = row["captured_at"].astimezone(ET)
            sig["market_open_time_utc"] = row["opens_at"]
            sig["market_close_time_utc"] = row["closes_at"]
            sig["btc_price"] = row["btc_price"]
            sig["strike"] = row["strike"]
            sig["entry_bid"] = _price(row, sig["entry_side"], "bid")
            sig["entry_ask"] = _price(row, sig["entry_side"], "ask")
            sig["entry_spread"] = _price(row, sig["entry_side"], "spread")
            return sig
    return None


def _score_signal(sig: dict[str, Any], market_rows: list[dict[str, Any]], horizon: int, fee_rate_cents: float, exit_tolerance_seconds: int, max_spread: float) -> dict[str, Any]:
    side = sig["entry_side"]
    entry_at = sig["entry_at"]
    entry_ask = sig["entry_ask"]
    exit_target = entry_at + timedelta(seconds=horizon)
    future_rows = [row for row in market_rows if row["captured_at"] >= entry_at and _is_clean_side(row, side, max_spread)]
    exit_row = _first_at_or_after(future_rows, exit_target, exit_tolerance_seconds)
    et = entry_at.astimezone(ET)
    base = {
        "rule_name": sig["rule_name"],
        "exit_horizon_seconds": horizon,
        "date_et": et.date().isoformat(),
        "weekday": et.strftime("%A"),
        "is_weekend": int(et.weekday() >= 5),
        "market_ticker": sig["market_ticker"],
        "market_open_time_utc": _iso_dt(sig["market_open_time_utc"]),
        "market_close_time_utc": _iso_dt(sig["market_close_time_utc"]),
        "entry_at_utc": _iso_dt(entry_at),
        "entry_at_et": _iso_dt(sig["entry_at_et"]),
        "entry_side": side,
        "entry_side_mode": sig["entry_side_mode"],
        "dominant_side": sig.get("dominant_side"),
        "minority_side": sig.get("minority_side"),
        "entry_bid": sig["entry_bid"],
        "entry_ask": entry_ask,
        "entry_spread": sig.get("entry_spread"),
        "btc_price": sig["btc_price"],
        "strike": sig["strike"],
        "btc_delta_60s": sig.get("btc_delta_60s"),
        "btc_abs_delta_60s": sig.get("btc_abs_delta_60s"),
        "dominant_change_prev_30s_cents": sig.get("dominant_change_prev_30s_cents"),
        "dominant_ask": sig.get("dominant_ask"),
        "minority_ask": sig.get("minority_ask"),
        "entry_side_distance": sig.get("entry_side_distance"),
        "abs_btc_distance": sig.get("abs_btc_distance"),
        "time_since_open_seconds": sig.get("time_since_open_seconds"),
        "dominant_cents_per_second_since_open": sig.get("dominant_cents_per_second_since_open"),
        "minority_local_low_bid": sig.get("minority_local_low_bid"),
        "minority_bid_bounce_from_low_cents": sig.get("minority_bid_bounce_from_low_cents"),
        "seconds_after_local_low": sig.get("seconds_after_local_low"),
    }
    if exit_row is None:
        base.update(
            {
                "status": "NO_VALID_EXIT",
                "exit_at_utc": None,
                "exit_bid": None,
                "gross_pnl_cents": None,
                "entry_fee_cents": _fee_cents(entry_ask, fee_rate_cents),
                "exit_fee_cents": None,
                "total_fee_cents": None,
                "net_pnl_cents": None,
                "holding_seconds": None,
            }
        )
        return base

    exit_bid = _price(exit_row, side, "bid")
    gross = round((exit_bid - entry_ask) * 100.0, 4)
    entry_fee = _fee_cents(entry_ask, fee_rate_cents)
    exit_fee = _fee_cents(exit_bid, fee_rate_cents)
    total_fee = round(entry_fee + exit_fee, 6)
    base.update(
        {
            "status": "COMPLETE",
            "exit_at_utc": _iso_dt(exit_row["captured_at"]),
            "exit_bid": exit_bid,
            "gross_pnl_cents": gross,
            "entry_fee_cents": entry_fee,
            "exit_fee_cents": exit_fee,
            "total_fee_cents": total_fee,
            "net_pnl_cents": round(gross - total_fee, 4),
            "holding_seconds": round((exit_row["captured_at"] - entry_at).total_seconds(), 3),
        }
    )
    return base


def _iso_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(sep=" ")


def _summarize(rows: list[dict[str, Any]], observed_days: list[str]) -> list[dict[str, Any]]:
    out = []
    observed_n = len(observed_days)
    observed_dates = sorted(observed_days)
    first_half_dates = set(observed_dates[: len(observed_dates) // 2])
    second_half_dates = set(observed_dates[len(observed_dates) // 2 :])
    for rule_name in RULE_CONFIGS:
        for horizon in HORIZONS:
            group = [row for row in rows if row["rule_name"] == rule_name and row["exit_horizon_seconds"] == horizon]
            complete = [row for row in group if row["status"] == "COMPLETE" and row["net_pnl_cents"] is not None]
            nets = [row["net_pnl_cents"] for row in complete]
            gross = [row["gross_pnl_cents"] for row in complete]
            days_with_trade = len({row["date_et"] for row in group})
            wins = sum(v > 0 for v in nets)
            first = [row["net_pnl_cents"] for row in complete if row["date_et"] in first_half_dates]
            second = [row["net_pnl_cents"] for row in complete if row["date_et"] in second_half_dates]
            weekday = [row["net_pnl_cents"] for row in complete if not row["is_weekend"]]
            weekend = [row["net_pnl_cents"] for row in complete if row["is_weekend"]]
            out.append(
                {
                    "rule_name": rule_name,
                    "exit_horizon_seconds": horizon,
                    "calendar_days_observed": observed_n,
                    "days_with_trade": days_with_trade,
                    "days_with_no_trade": observed_n - days_with_trade,
                    "average_trades_per_day": round(days_with_trade / observed_n, 4) if observed_n else None,
                    "completed_trades": len(complete),
                    "no_valid_exit_trades": sum(row["status"] == "NO_VALID_EXIT" for row in group),
                    "win_rate_pct": _pct(wins, len(complete)),
                    "average_gross_cents_per_trade": _avg(gross),
                    "average_net_cents_per_trade": _avg(nets),
                    "median_net_cents_per_trade": _median(nets),
                    "total_net_cents": round(sum(nets), 4) if nets else None,
                    "profit_factor": _profit_factor(nets),
                    "worst_trade": min(nets) if nets else None,
                    "best_trade": max(nets) if nets else None,
                    "maximum_drawdown_cents": _max_drawdown(nets),
                    "first_half_average_net": _avg(first),
                    "second_half_average_net": _avg(second),
                    "weekday_average_net": _avg(weekday),
                    "weekend_average_net": _avg(weekend),
                    "exploratory_status": RULE_CONFIGS[rule_name].exploratory_status,
                }
            )
    return out


def _daily_pnl(rows: list[dict[str, Any]], observed_days: list[str]) -> list[dict[str, Any]]:
    by_key = {(row["rule_name"], row["exit_horizon_seconds"], row["date_et"]): row for row in rows}
    out = []
    for rule_name in RULE_CONFIGS:
        for horizon in HORIZONS:
            for day in observed_days:
                row = by_key.get((rule_name, horizon, day))
                if row is None:
                    out.append(
                        {
                            "date_et": day,
                            "rule_name": rule_name,
                            "exit_horizon_seconds": horizon,
                            "status": "NO_TRADE",
                            "market_ticker": None,
                            "net_pnl_cents": None,
                            "gross_pnl_cents": None,
                        }
                    )
                else:
                    out.append(
                        {
                            "date_et": day,
                            "rule_name": rule_name,
                            "exit_horizon_seconds": horizon,
                            "status": row["status"],
                            "market_ticker": row["market_ticker"],
                            "net_pnl_cents": row["net_pnl_cents"],
                            "gross_pnl_cents": row["gross_pnl_cents"],
                        }
                    )
    return out


def _skipped_days(trades: list[dict[str, Any]], observed_days: list[str]) -> list[dict[str, Any]]:
    traded = {(row["rule_name"], row["date_et"]) for row in trades}
    out = []
    for rule_name in RULE_CONFIGS:
        for day in observed_days:
            if (rule_name, day) not in traded:
                out.append(
                    {
                        "date_et": day,
                        "rule_name": rule_name,
                        "skip_reason": "no_clean_pre_entry_signal_matching_frozen_rule",
                    }
                )
    return out


def _rule_config_rows(
    btc_60s_abs_threshold: float,
    dominant_30s_reprice_threshold_cents: float,
    bounce_confirm_cents: float,
    min_seconds_after_low: float,
) -> list[dict[str, Any]]:
    rows = []
    for rule in RULE_CONFIGS.values():
        row = {
            "rule_name": rule.rule_name,
            "entry_side_mode": rule.entry_side_mode,
            "description": rule.description,
            "threshold_summary": rule.threshold_summary,
            "exploratory_status": rule.exploratory_status,
            "btc_60s_abs_threshold": btc_60s_abs_threshold if rule.rule_name == "rule_a_btc_60s_abs_move" else None,
            "dominant_30s_reprice_threshold_cents": dominant_30s_reprice_threshold_cents if rule.rule_name == "rule_b_dominant_30s_reprice" else None,
            "bounce_confirm_cents": bounce_confirm_cents if rule.rule_name == "rule_d_minority_local_low_bounce" else None,
            "min_seconds_after_low": min_seconds_after_low if rule.rule_name == "rule_d_minority_local_low_bounce" else None,
        }
        rows.append(row)
    return rows


def _threshold_grid_rows() -> list[dict[str, Any]]:
    return [
        {"rule_name": "rule_a_btc_60s_abs_move", "candidate_threshold": "btc_abs_delta_60s >= 30"},
        {"rule_name": "rule_a_btc_60s_abs_move", "candidate_threshold": "btc_abs_delta_60s >= 50", "used_default": 1},
        {"rule_name": "rule_a_btc_60s_abs_move", "candidate_threshold": "btc_abs_delta_60s >= 75"},
        {"rule_name": "rule_b_dominant_30s_reprice", "candidate_threshold": "dominant_30s_reprice >= 5c"},
        {"rule_name": "rule_b_dominant_30s_reprice", "candidate_threshold": "dominant_30s_reprice >= 10c", "used_default": 1},
        {"rule_name": "rule_b_dominant_30s_reprice", "candidate_threshold": "dominant_30s_reprice >= 15c"},
        {"rule_name": "rule_d_minority_local_low_bounce", "candidate_threshold": "local_low_bounce >= 1c"},
        {"rule_name": "rule_d_minority_local_low_bounce", "candidate_threshold": "local_low_bounce >= 2c", "used_default": 1},
        {"rule_name": "rule_d_minority_local_low_bounce", "candidate_threshold": "local_low_bounce >= 3c"},
    ]


def _hindsight_comparison(rows_by_market: dict[int, list[dict[str, Any]]], max_spread: float, fee_rate_cents: float) -> list[dict[str, Any]]:
    market_rows = [_market_row(rows, max_spread, fee_rate_cents) for rows in rows_by_market.values() if rows]
    selected = [row for row in market_rows if 8 <= int(row["hour_int_et"]) < 11]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        by_day[row["date_et"]].append(row)
    best = []
    for day, group in by_day.items():
        clean = [row for row in group if row.get("maximum_clean_executable_net_scalp_cents") is not None]
        if clean:
            best.append(max(clean, key=lambda row: row["maximum_clean_executable_net_scalp_cents"]))
    nets = [row["maximum_clean_executable_net_scalp_cents"] for row in best]
    return [
        {
            "comparison_name": "hindsight_best_opportunity_08_11_et",
            "calendar_days_observed": len(by_day),
            "days_with_hindsight_opportunity": len(best),
            "average_best_net_scalp_per_day": _avg(nets),
            "median_best_net_scalp_per_day": _median(nets),
            "net_positive_day_pct": _pct(sum(v > 0 for v in nets), len(nets)),
            "profit_factor": _profit_factor(nets),
            "warning": "uses future path to pick best opportunity; not a live-entry rule",
        }
    ]


def _data_quality(raw_rows: list[dict[str, Any]], rows_by_market: dict[int, list[dict[str, Any]]], entry_rows: list[dict[str, Any]], max_spread: float) -> list[dict[str, Any]]:
    return [
        {"issue": "raw_rows_loaded", "rows_affected": len(raw_rows)},
        {"issue": "markets_loaded", "rows_affected": len(rows_by_market)},
        {"issue": "entry_window_rows_loaded", "rows_affected": len(entry_rows)},
        {"issue": "quote_age_ms_unavailable_in_historical_snapshots", "rows_affected": len(raw_rows)},
        {"issue": "raw_yes_bid_gt_ask", "rows_affected": sum(row.get("yes_bid") is not None and row.get("yes_ask") is not None and row["yes_bid"] > row["yes_ask"] for row in raw_rows)},
        {"issue": "raw_no_bid_gt_ask", "rows_affected": sum(row.get("no_bid") is not None and row.get("no_ask") is not None and row["no_bid"] > row["no_ask"] for row in raw_rows)},
        {"issue": "raw_yes_spread_eq_zero", "rows_affected": sum(row.get("yes_spread") == 0 for row in raw_rows)},
        {"issue": "raw_no_spread_eq_zero", "rows_affected": sum(row.get("no_spread") == 0 for row in raw_rows)},
        {"issue": "entry_window_clean_quote_rows", "rows_affected": sum(_clean_row(row, max_spread) for row in entry_rows)},
    ]


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


def _table(rows: list[dict[str, Any]], cols: list[str], limit: int = 16) -> str:
    if not rows:
        return "_No rows._"
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in cols) + " |")
    return "\n".join(lines)


def _paths(output_dir: Path) -> Paths:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"live_style_time_window_rule_test_{date.today().isoformat()}"
    return Paths(
        directory=output_dir,
        trade_csv=output_dir / f"{stem}_trades.csv",
        daily_pnl_csv=output_dir / f"{stem}_daily_pnl.csv",
        summary_csv=output_dir / f"{stem}_summary.csv",
        rule_config_csv=output_dir / f"{stem}_rule_config.csv",
        skipped_days_csv=output_dir / f"{stem}_skipped_days.csv",
        threshold_grid_csv=output_dir / f"{stem}_threshold_grid.csv",
        hindsight_comparison_csv=output_dir / f"{stem}_hindsight_comparison.csv",
        data_quality_csv=output_dir / f"{stem}_data_quality.csv",
        markdown_report=output_dir / f"{stem}_report.md",
    )


def _render_markdown(paths: Paths, summary: list[dict[str, Any]], configs: list[dict[str, Any]], skipped: list[dict[str, Any]], hindsight: list[dict[str, Any]], dq: list[dict[str, Any]]) -> str:
    ranked = sorted(summary, key=lambda row: row.get("average_net_cents_per_trade") if row.get("average_net_cents_per_trade") is not None else -999999, reverse=True)
    positive = [row for row in ranked if row.get("average_net_cents_per_trade") is not None and row["average_net_cents_per_trade"] > 0 and row["days_with_trade"] >= 10]
    conclusion = "No pre-entry rule produced positive net expectancy over a meaningful sample after fees."
    if positive:
        best = positive[0]
        conclusion = (
            f"Best positive pre-entry result: `{best['rule_name']}` at `{best['exit_horizon_seconds']}s`, "
            f"avg net `{best['average_net_cents_per_trade']}`c over `{best['days_with_trade']}` trade days. "
            "Because thresholds are exploratory-fixed, this should only justify prospective paper testing."
        )

    skip_counts = defaultdict(int)
    for row in skipped:
        skip_counts[row["rule_name"]] += 1
    skip_rows = [{"rule_name": k, "days_skipped": v} for k, v in sorted(skip_counts.items())]

    return f"""# Live-Style 08:00-11:00 ET Rule Test

## Direct Answer

{conclusion}

This report uses one first-valid signal per rule per ET day. It does not select the best trade after the fact.

## Rule Configurations

{_table(configs, ["rule_name", "entry_side_mode", "threshold_summary", "exploratory_status"], 10)}

## Summary By Rule And Exit

{_table(ranked, ["rule_name", "exit_horizon_seconds", "calendar_days_observed", "days_with_trade", "days_with_no_trade", "win_rate_pct", "average_gross_cents_per_trade", "average_net_cents_per_trade", "median_net_cents_per_trade", "total_net_cents", "profit_factor", "maximum_drawdown_cents", "first_half_average_net", "second_half_average_net"], 20)}

## Skipped Days

{_table(skip_rows, ["rule_name", "days_skipped"], 10)}

## Hindsight Comparison

{_table(hindsight, ["comparison_name", "calendar_days_observed", "days_with_hindsight_opportunity", "average_best_net_scalp_per_day", "median_best_net_scalp_per_day", "net_positive_day_pct", "warning"], 5)}

## Data Quality

{_table(dq, ["issue", "rows_affected"], 20)}

## Output Files

- Trade CSV: `{paths.trade_csv}`
- Daily P/L CSV: `{paths.daily_pnl_csv}`
- Summary CSV: `{paths.summary_csv}`
- Rule config CSV: `{paths.rule_config_csv}`
- Skipped days CSV: `{paths.skipped_days_csv}`
- Threshold grid CSV: `{paths.threshold_grid_csv}`
- Hindsight comparison CSV: `{paths.hindsight_comparison_csv}`
- Data quality CSV: `{paths.data_quality_csv}`

## Research Warning

These are fixed exploratory thresholds. Do not treat a positive row as a trading edge unless it survives a prospective paper test without threshold changes.
"""


def build_report(
    output_dir: Path,
    market_like: str,
    start: str | None,
    end: str | None,
    max_spread: float,
    fee_rate_cents: float,
    start_hour_et: int,
    end_hour_et: int,
    exit_tolerance_seconds: int,
    btc_60s_abs_threshold: float,
    dominant_30s_reprice_threshold_cents: float,
    bounce_confirm_cents: float,
    min_seconds_after_low: float,
) -> Paths:
    paths = _paths(output_dir)
    raw_rows = _load_rows(market_like, start, end)
    rows_by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        rows_by_market[int(row["market_pk"])].append(row)
    for rows in rows_by_market.values():
        rows.sort(key=lambda row: row["captured_at"])

    entry_rows = [row for row in raw_rows if _in_entry_window(row["captured_at"], start_hour_et, end_hour_et)]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in entry_rows:
        by_day[row["captured_at"].astimezone(ET).date().isoformat()].append(row)
    observed_days = sorted(by_day)

    trades = []
    for day in observed_days:
        day_rows = sorted(by_day[day], key=lambda row: row["captured_at"])
        for rule_name in RULE_CONFIGS:
            sig = _find_daily_signal(
                day_rows,
                rows_by_market,
                rule_name,
                max_spread,
                start_hour_et,
                btc_60s_abs_threshold,
                dominant_30s_reprice_threshold_cents,
                bounce_confirm_cents,
                min_seconds_after_low,
            )
            if sig is None:
                continue
            market_rows = rows_by_market[int(sig["market_pk"])]
            for horizon in HORIZONS:
                trades.append(_score_signal(sig, market_rows, horizon, fee_rate_cents, exit_tolerance_seconds, max_spread))

    summary = _summarize(trades, observed_days)
    daily = _daily_pnl(trades, observed_days)
    skipped = _skipped_days(trades, observed_days)
    configs = _rule_config_rows(
        btc_60s_abs_threshold,
        dominant_30s_reprice_threshold_cents,
        bounce_confirm_cents,
        min_seconds_after_low,
    )
    grid = _threshold_grid_rows()
    hindsight = _hindsight_comparison(rows_by_market, max_spread, fee_rate_cents)
    dq = _data_quality(raw_rows, rows_by_market, entry_rows, max_spread)

    _write_csv(paths.trade_csv, trades)
    _write_csv(paths.daily_pnl_csv, daily)
    _write_csv(paths.summary_csv, summary)
    _write_csv(paths.rule_config_csv, configs)
    _write_csv(paths.skipped_days_csv, skipped)
    _write_csv(paths.threshold_grid_csv, grid)
    _write_csv(paths.hindsight_comparison_csv, hindsight)
    _write_csv(paths.data_quality_csv, dq)
    paths.markdown_report.write_text(_render_markdown(paths, summary, configs, skipped, hindsight, dq))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Live-style one-trade-per-day 08:00-11:00 ET rule test")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-like", default="KXBTC15M-%")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-spread", type=float, default=0.01)
    parser.add_argument("--fee-rate-cents", type=float, default=7.0)
    parser.add_argument("--start-hour-et", type=int, default=8)
    parser.add_argument("--end-hour-et", type=int, default=11)
    parser.add_argument("--exit-tolerance-seconds", type=int, default=10)
    parser.add_argument("--btc-60s-abs-threshold", type=float, default=50.0)
    parser.add_argument("--dominant-30s-reprice-threshold-cents", type=float, default=10.0)
    parser.add_argument("--bounce-confirm-cents", type=float, default=2.0)
    parser.add_argument("--min-seconds-after-low", type=float, default=4.0)
    args = parser.parse_args()

    paths = build_report(
        output_dir=args.output_dir,
        market_like=args.market_like,
        start=args.start,
        end=args.end,
        max_spread=args.max_spread,
        fee_rate_cents=args.fee_rate_cents,
        start_hour_et=args.start_hour_et,
        end_hour_et=args.end_hour_et,
        exit_tolerance_seconds=args.exit_tolerance_seconds,
        btc_60s_abs_threshold=args.btc_60s_abs_threshold,
        dominant_30s_reprice_threshold_cents=args.dominant_30s_reprice_threshold_cents,
        bounce_confirm_cents=args.bounce_confirm_cents,
        min_seconds_after_low=args.min_seconds_after_low,
    )
    print("Live-style time-window rule test complete")
    print(f"trade_csv={paths.trade_csv}")
    print(f"daily_pnl_csv={paths.daily_pnl_csv}")
    print(f"summary_csv={paths.summary_csv}")
    print(f"rule_config_csv={paths.rule_config_csv}")
    print(f"skipped_days_csv={paths.skipped_days_csv}")
    print(f"threshold_grid_csv={paths.threshold_grid_csv}")
    print(f"hindsight_comparison_csv={paths.hindsight_comparison_csv}")
    print(f"data_quality_csv={paths.data_quality_csv}")
    print(f"markdown_report={paths.markdown_report}")


if __name__ == "__main__":
    main()
