# Kalshi Signal Logger

> **⚠️ This system places no real orders and never will.**
> It is a research tool that watches BTC binary markets, detects strategy signals,
> simulates paper trades, and stores everything in MySQL for offline analysis.
> See [Before trading real money](#before-trading-real-money) before acting on any results.

---

## What this is

A Python polling loop that runs every 2 seconds and:

1. Fetches the live BTC price from the Kraken public API (falls back to a random-walk mock)
2. Constructs a mock 15-minute BTC up/down binary market (real Kalshi API hookup is a future step)
3. Evaluates two entry strategies against rolling BTC features
4. Deduplicates signals with a 30-second cooldown window
5. Writes signals, simulated paper trades, and per-tick trade snapshots to MySQL
6. Logs a one-line summary per tick to stdout

Nothing writes to any exchange. The guard `LIVE_TRADING_ENABLED=false` is enforced at the DB-pool level — setting it to `true` raises a hard `RuntimeError` at startup.

---

## Project layout

```
kalshi-signal-logger/
├── app/
│   ├── config.py          # env-var loading
│   ├── db.py              # MySQL connection pool + raw SQL helpers
│   ├── data_feed.py       # BTC price fetch + mock market/contract prices
│   ├── features.py        # pure feature functions (momentum, velocity, z-scores…)
│   ├── strategies.py      # entry rules → Signal dataclass
│   ├── paper_trader.py    # simulated trade lifecycle + exit rules
│   └── main.py            # 2-second polling loop
├── scripts/
│   ├── analyze_trades.py  # strategy performance tables by 8 dimensions
│   └── monte_carlo.py     # equity-curve simulation (flat + snowball sizing)
├── sql/
│   └── schema.sql         # 7-table MySQL schema
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Prerequisites

- Docker and Docker Compose **or** Python 3.11+ with a local MySQL 8.0 instance
- No Kalshi API credentials required — the market and contract prices are mocked until you wire up the live feed

---

## Setup

### 1. Clone and configure

```bash
git clone <repo-url> kalshi-signal-logger
cd kalshi-signal-logger
cp .env.example .env
```

Edit `.env` as needed. The defaults work with Docker Compose without changes:

```dotenv
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_DATABASE=signal_logger
MYSQL_USER=logger_user
MYSQL_PASSWORD=logger_password
LIVE_TRADING_ENABLED=false        # must stay false
POLL_INTERVAL_SECONDS=2
MAX_SPREAD_CENTS=3.0
PAPER_POSITION_SIZE=1
SLIPPAGE_MODE=realistic           # optimistic | realistic | harsh
```

`MYSQL_HOST=mysql` is the Docker Compose service name. Change it to `localhost` when running Python directly against a local MySQL.

### 2. (Optional) Python virtual environment

Only needed when running scripts outside Docker:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Running with Docker Compose (recommended)

```bash
# First run — builds the image, starts MySQL, applies schema, starts the logger
docker compose up --build

# Background mode
docker compose up --build -d

# Follow logs
docker compose logs -f logger

# Stop everything (data is preserved in the mysql_data volume)
docker compose down

# Stop and wipe all data
docker compose down -v
```

MySQL data persists in a named Docker volume (`mysql_data`) across restarts.

### Checking it works

Within a few seconds of startup you should see lines like:

```
2026-05-27 12:00:02 INFO  app.main — tick#0001 | BTC $67,423  tgt $67,500  gap -$77 |
  YES 0.41/0.43  NO 0.57/0.59 | t=812s | sigs=0 opened=0 open=0 closed=0 | KXBTC-...
```

---

## Running Python directly (without Docker)

Requires a running MySQL 8.0 instance with the schema applied:

```bash
mysql -u logger_user -p signal_logger < sql/schema.sql

# Set MYSQL_HOST=localhost in .env, then:
python -m app.main
```

---

## Analysis scripts

Both scripts connect to the same MySQL database. Run them while the logger is running or after it has collected trades.

### Trade analysis

Prints strategy performance grouped by rule, exit reason, contract price, spread, momentum, gap z-score, and time-remaining buckets. Writes a Markdown report.

```bash
# Inside Docker (while running)
docker compose exec logger python scripts/analyze_trades.py

# Or directly with Python
python scripts/analyze_trades.py
```

Output is printed to the terminal and saved to:

```
reports/daily_report_YYYY_MM_DD.md
```

### Monte Carlo simulation

Shuffles the historical per-trade PnL sequence 10,000 times and reports equity-curve statistics under two position-sizing models.

```bash
# All strategies combined (default)
python scripts/monte_carlo.py

# Single strategy
python scripts/monte_carlo.py --strategy cheap_reversal_scalp

# Custom starting balance and simulation count
python scripts/monte_carlo.py --balance 5000 --sims 50000

# Full option list
python scripts/monte_carlo.py --help
```

Reports are saved to:

```
reports/montecarlo_<strategy>_YYYY_MM_DD.md
```

**Sizing modes**

| Mode | Description |
|:---|:---|
| Flat | Each trade moves equity by its raw logged PnL (position_size = 1 contract). End balance is deterministic; the simulation shows path variance and drawdown distribution. |
| Snowball | Risk fraction grows with account profit: `min(2% + 25% × profit_fraction, 3%)`. Early wins compound; the path order matters and final balances vary across simulations. |

---

## Database tables

The schema lives in [`sql/schema.sql`](sql/schema.sql). Docker Compose applies it automatically on first boot.

## Exporting table data

Use the read-only exporter when you need raw table data from the current logger database:

```bash
python scripts/export_table_data.py --table signals --format csv
python scripts/export_table_data.py --table paper_trades --where "status = 'CLOSED'" --order-by "entry_time DESC"
python scripts/export_table_data.py --all --format jsonl --out-dir exports/latest
```

The exporter uses the same MySQL environment variables as the logger and supports `csv`, `jsonl`, and `json` outputs.

### `markets`

One row per 15-minute contract window. Keyed by `market_ticker` (e.g. `KXBTC-260527-1200-T67500`). All other tables foreign-key to this.

### `btc_ticks`

Raw BTC price samples recorded every poll cycle. `source` is `kraken_or_mock`. `market_ticker` is nullable so ticks can be stored before a market window opens.

### `contract_ticks`

Order-book snapshot for each side (YES / NO) per poll cycle. Stores bid, ask, mid, last, and spread. Used to replay intra-window price action.

### `signals`

One row every time a strategy rule fires and passes the cooldown check. Captures the full feature snapshot at signal time: BTC price, target, gap, z-score, momentum, velocity, volatility, contract price, spread, and timing.

### `paper_trades`

Simulated entry and exit lifecycle for each signal. Records entry price (ask + slippage), exit price (bid − slippage), peak/lowest price seen while open, exit reason, PnL, and PnL %. `status` is `OPEN` until the trade exits. No real orders are ever placed.

### `trade_snapshots`

Per-tick feature record written while a paper trade is open. Stores the full market state (BTC context, rolling volatility, z-scores, contract price, momentum) at each polling interval. Used to replay how a trade evolved and to build training datasets.

### `strategy_versions`

Registry of every `rule_name / rule_version` combination with its entry and exit rules captured as JSON. A convenience reference for analysis queries — not enforced at runtime.

---

## Strategies

### `cheap_reversal_scalp` v1

**Hypothesis:** Low-priced contracts early in a 15-minute window can produce short reversal bounces that are large enough to scalp before expiry.

**Entry conditions:**

| Condition | Value | Rationale |
|:---|:---|:---|
| Contract age | ≤ 300 s | More time left on the contract to benefit from a bounce |
| Ask price | 0.05 – 0.20 | Cheap enough to offer asymmetric payoff |
| Spread | ≤ $0.03 | Tight enough to enter and exit cleanly |
| `reversal_score` | ≥ 3 (YES) or ≥ 3 (NO) | Clear directional momentum in last 10 BTC ticks |

Side is chosen by whichever reversal score crosses the threshold first. If both cross simultaneously, YES takes priority.

**Exit rules (priority order):**

| Rule | Trigger |
|:---|:---|
| `timeout_60s` | Trade has been open ≥ 60 seconds |
| `near_expiry` | Time remaining ≤ 30 seconds |
| `take_profit` | Mid price ≥ entry × 1.30 (+30%) |
| `stop_loss` | Mid price ≤ entry − $0.03 |
| `trailing_stop` | Mid price ≤ peak − $0.03, once peak ≥ entry + $0.03 |

---

### `premium_momentum_continuation` v1

**Hypothesis:** In the final 3–5 minutes of a window where BTC has built sustained momentum and the leading contract is already priced at $0.76+, that momentum often carries through to expiry.

**Entry conditions:**

| Condition | Value | Rationale |
|:---|:---|:---|
| Time remaining | 180 – 300 s | Last 3–5 min; not the final danger zone |
| BTC vs target | YES if above, NO if below | Confirms direction |
| Leading-side ask | ≥ $0.76 | Market already pricing a likely winner |
| Directional `gap_z_score` | ≥ 1.0 σ | BTC statistically far enough from target |
| `momentum_score` | ≥ +3 (YES) or ≤ −3 (NO) | Sustained directional pressure |
| Spread | ≤ $0.03 | Same liquidity filter as Strategy A |

**Exit rules (priority order):**

| Rule | Trigger |
|:---|:---|
| `near_expiry` | Time remaining ≤ 30 seconds |
| `take_profit` | Mid price ≥ entry + $0.06 |
| `stop_loss` | Mid price ≤ entry − $0.05 |
| `break_even_stop` | Mid price ≤ entry, once peak ≥ entry + $0.04 |
| `trailing_stop` | Mid price ≤ peak − $0.04 (active from first tick) |

---

## Adding a new strategy version

A new strategy is a pure Python function with a fixed signature. No other infrastructure changes are required.

### Step 1 — Write the entry function (`app/strategies.py`)

```python
_MY_RULE_NAME    = "my_strategy"
_MY_RULE_VERSION = "v1"

def my_strategy(
    ticks: list[Tick],
    market_ticker: str,
    btc_price: float,
    target_price: float,
    contract_age_seconds: float,
    time_remaining_seconds: float,
    contract_prices: dict[str, dict],
) -> Optional[Signal]:
    # 1. Return None if entry conditions are not met.
    # 2. Compute features via app.features (all pure functions).
    # 3. Build and return a Signal dataclass.
    ...
```

### Step 2 — Register the strategy

In `app/strategies.py`, append to `_STRATEGIES`:

```python
_STRATEGIES = [
    cheap_reversal_scalp,
    premium_momentum_continuation,
    my_strategy,          # ← add here
]
```

`run_all()` picks it up automatically on the next poll cycle.

### Step 3 — Write the exit function (`app/paper_trader.py`)

```python
_MY_TP   = 0.05
_MY_SL   = 0.04

def _exit_my_strategy(
    trade: OpenTrade,
    mid: float,
    time_remaining_seconds: float,
    elapsed_seconds: float,
) -> Optional[str]:
    if time_remaining_seconds <= 30:
        return "near_expiry"
    if mid >= trade.entry_price + _MY_TP:
        return "take_profit"
    if mid <= trade.entry_price - _MY_SL:
        return "stop_loss"
    return None
```

### Step 4 — Register the exit function

In `app/paper_trader.py`, add to `_EXIT_DISPATCH`:

```python
_EXIT_DISPATCH = {
    "cheap_reversal_scalp/v1":          _exit_cheap_reversal_scalp,
    "premium_momentum_continuation/v1": _exit_premium_momentum_continuation,
    "my_strategy/v1":                   _exit_my_strategy,   # ← add here
}
```

Trades from strategies without an entry in `_EXIT_DISPATCH` are skipped with a warning log.

### Step 5 — Record it in the registry table (optional but recommended)

```sql
INSERT INTO strategy_versions (rule_name, rule_version, description, entry_rules, exit_rules, is_active)
VALUES (
  'my_strategy', 'v1',
  'One-line description.',
  '{"condition_1": "...", "condition_2": "..."}',
  '{"take_profit": "+0.05", "stop_loss": "-0.04", "near_expiry": "30s"}',
  TRUE
);
```

---

## Configuration reference

| Variable | Default | Description |
|:---|:---|:---|
| `MYSQL_HOST` | `localhost` | MySQL host (`mysql` inside Docker Compose) |
| `MYSQL_PORT` | `3306` | MySQL port |
| `MYSQL_DATABASE` | `signal_logger` | Database name |
| `MYSQL_USER` | `logger_user` | DB user |
| `MYSQL_PASSWORD` | `logger_password` | DB password |
| `LIVE_TRADING_ENABLED` | `false` | **Must stay false.** `true` raises `RuntimeError` at startup. |
| `POLL_INTERVAL_SECONDS` | `2` | How often (seconds) to fetch prices and evaluate strategies |
| `MAX_SPREAD_CENTS` | `3.0` | Signals with spread above this are filtered out |
| `PAPER_POSITION_SIZE` | `1` | Simulated contract count per trade |
| `SLIPPAGE_MODE` | `realistic` | `optimistic` (±$0.00), `realistic` (±$0.01), `harsh` (±$0.02) |
| `KALSHI_API_BASE` | demo URL | Base URL for future live Kalshi API integration |

---

## Before trading real money

> **Do not act on these signals with real capital until all of the following are true.**

### Minimum data requirements

- [ ] **250+ closed paper trades** across varied market conditions (not one streak)
- [ ] At least 60 trading days of data with normal BTC volatility
- [ ] Both strategies have been independently exercised and analysed

### Monte Carlo requirements

Run `python scripts/monte_carlo.py` and confirm all of the following under **flat sizing**:

- [ ] Median ending balance is above starting balance (positive expectancy)
- [ ] Worst-5% ending balance is no more than 20% below start
- [ ] Typical max drawdown (p50) is under −25%
- [ ] Probability of losing 50% of account is under 5%
- [ ] Max losing streak p95 is 10 or fewer consecutive losses

And under **snowball sizing** (3% cap):

- [ ] Typical max drawdown (p50) is under −30%
- [ ] Probability of losing 25% of account is under 10%

### Other checks

- [ ] `analyze_trades.py` shows positive expectancy for both strategies individually
- [ ] Exit-reason breakdown shows `take_profit` and `trailing_stop` are the dominant closers (not `stop_loss` or `near_expiry`)
- [ ] `spread` bucket analysis shows performance does not collapse at higher spreads
- [ ] The liquidity placeholder in `cheap_reversal_scalp` (`_liquidity_ok`) has been replaced with a real volume/open-interest gate
- [ ] Live Kalshi market and contract prices have replaced the mock data feed
- [ ] You understand that past paper-trade performance does not guarantee live performance

Even after all checks pass, start with a small allocation (1–2% of your intended capital) and monitor live performance for 30+ trades before scaling.
