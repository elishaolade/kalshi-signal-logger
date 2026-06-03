# Frozen Test Definition

Date frozen: 2026-06-03

## Hypothesis

Late premium NO continuation performs best when the NO contract is already
strong but not fully saturated, momentum still confirms downside continuation,
spread remains manageable, and the target sits near the rolling 5-hour BTC
high.

## Frozen Slice

- `rule_name = premium_momentum_continuation`
- `rule_version = v1`
- `side = NO`
- `contract_price >= 0.75 AND contract_price < 0.80`
- `spread <= 0.03`
- `time_remaining_seconds >= 240 AND time_remaining_seconds < 300`
- `momentum_score <= -3`
- `target_price within 200 dollars of rolling 5-hour BTC high`

## Tracked Query

```sql
WITH signal_ctx AS (
  SELECT
    s.id AS signal_id,
    s.contract_price,
    s.spread,
    s.time_remaining_seconds,
    s.momentum_score,
    s.target_price,
    (
      SELECT MAX(bt.btc_price)
      FROM btc_ticks bt
      WHERE bt.recorded_at <= s.recorded_at
        AND bt.recorded_at > s.recorded_at - INTERVAL 5 HOUR
    ) AS rolling_5h_high
  FROM signals s
  WHERE s.rule_name = 'premium_momentum_continuation'
    AND s.rule_version = 'v1'
    AND s.side = 'NO'
)
SELECT
  CASE
    WHEN target_price >= rolling_5h_high - 100 THEN 'within_100'
    WHEN target_price >= rolling_5h_high - 200 THEN 'within_200'
    WHEN target_price >= rolling_5h_high - 300 THEN 'within_300'
    ELSE 'more_than_300_below'
  END AS target_vs_5h_high,
  COUNT(*) AS trades,
  ROUND(AVG(pt.pnl > 0) * 100, 1) AS win_rate_pct,
  ROUND(SUM(pt.pnl), 4) AS total_pnl,
  ROUND(AVG(pt.pnl), 4) AS avg_pnl,
  ROUND(AVG(CASE WHEN pt.pnl > 0 THEN pt.pnl END), 4) AS avg_win,
  ROUND(AVG(CASE WHEN pt.pnl < 0 THEN pt.pnl END), 4) AS avg_loss,
  ROUND(
    SUM(CASE WHEN pt.pnl > 0 THEN pt.pnl ELSE 0 END) /
    NULLIF(ABS(SUM(CASE WHEN pt.pnl < 0 THEN pt.pnl ELSE 0 END)), 0),
    2
  ) AS profit_factor,
  MAX(pt.exit_time) AS latest_exit
FROM paper_trades pt
JOIN signal_ctx sc ON sc.signal_id = pt.signal_id
WHERE pt.status = 'CLOSED'
  AND pt.followed_rules = TRUE
  AND pt.pnl IS NOT NULL
  AND sc.contract_price >= 0.75
  AND sc.contract_price < 0.80
  AND sc.spread <= 0.03
  AND sc.time_remaining_seconds >= 240
  AND sc.time_remaining_seconds < 300
  AND sc.momentum_score <= -3
GROUP BY target_vs_5h_high
ORDER BY target_vs_5h_high;
```

## Rules

- Do not change the filters until there are at least 20 qualifying trades in
  the `within_100` + `within_200` buckets combined.
- Reassess at 20, 30, and 50 qualifying trades.
- Keep this paper-only. No live trading changes.
- Treat this as a regime-sensitive hypothesis until proven otherwise.
