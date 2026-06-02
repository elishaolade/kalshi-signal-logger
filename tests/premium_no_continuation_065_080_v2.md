# premium_no_continuation_065_080_v2

## Status
paper_active

## Hypothesis
NO-side premium continuation between 0.65 and 0.80 has positive expectancy when momentum confirms direction.

## Entry
- NO only
- NO ask 0.65-0.80
- spread <= 0.03
- directional momentum >= 3
- directional gap/z-score supports NO
- volatility_regime != violent
- avoid time_remaining > 300s

## Exit
- take profit: +0.04 or +0.05
- stop loss: -0.06
- timeout: 60s

## Prove
- 100+ trades
- expectancy > +0.003
- profit factor > 1.20
- win rate >= 58%

## Falsify
- 50+ trades
- expectancy <= 0
- profit factor < 1.00
- avg loss overwhelms avg win
