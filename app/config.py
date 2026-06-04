import os

from dotenv import load_dotenv

load_dotenv()

# ── MySQL ────────────────────────────────────────────────────────────────────
MYSQL_HOST     = os.getenv("MYSQL_HOST",     "localhost")
MYSQL_PORT     = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "signal_logger")
MYSQL_USER     = os.getenv("MYSQL_USER",     "logger_user")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "logger_password")

# ── Runtime behaviour ────────────────────────────────────────────────────────
# Must remain false — this project logs and paper-trades only.
LIVE_TRADING_ENABLED  = os.getenv("LIVE_TRADING_ENABLED",  "false").lower() == "true"
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "2"))

# ── Signal / paper-trade filters ─────────────────────────────────────────────
# Reject signals whose contract spread exceeds this many cents.
MAX_SPREAD_CENTS  = float(os.getenv("MAX_SPREAD_CENTS",  "3.0"))
# Simulated contract count per paper trade.
PAPER_POSITION_SIZE = int(os.getenv("PAPER_POSITION_SIZE", "1"))
# Slippage model applied to simulated entries and exits.
# optimistic: entry=ask+0.00  exit=bid-0.00
# realistic:  entry=ask+0.01  exit=bid-0.01
# harsh:      entry=ask+0.02  exit=bid-0.02
SLIPPAGE_MODE = os.getenv("SLIPPAGE_MODE", "realistic")

# ── Timezone (for time-of-day feature logging) ────────────────────────────────
# All entry_date / entry_hour / block fields stored in this timezone.
SIGNAL_TIMEZONE = os.getenv("SIGNAL_TIMEZONE", "America/New_York")

# ── External API ─────────────────────────────────────────────────────────────
KALSHI_API_BASE            = os.getenv("KALSHI_API_BASE", "https://demo-api.kalshi.co/trade-api/v2")
KALSHI_API_TIMEOUT_SECONDS = float(os.getenv("KALSHI_API_TIMEOUT_SECONDS", "8"))

# ── Hourly BTC range market discovery ────────────────────────────────────────
# Set ONE of these in .env to enable range-market observation.
# KALSHI_BTC_RANGE_EVENT_TICKER  — a specific single event  (e.g. KXBTC-25060313)
# KALSHI_BTC_RANGE_SERIES_TICKER — a whole series            (e.g. KXBTCR)
# If both are empty the HourlyRangeTracker skips silently.
KALSHI_BTC_RANGE_EVENT_TICKER  = os.getenv("KALSHI_BTC_RANGE_EVENT_TICKER",  "")
KALSHI_BTC_RANGE_SERIES_TICKER = os.getenv("KALSHI_BTC_RANGE_SERIES_TICKER", "")
# How often (seconds) the range tracker polls Kalshi.  30s is fine for hourly markets.
RANGE_MARKET_POLL_INTERVAL_SECONDS = float(os.getenv("RANGE_MARKET_POLL_INTERVAL_SECONDS", "30"))

# ── Research API ─────────────────────────────────────────────────────────────
RESEARCH_API_TOKEN = os.getenv("RESEARCH_API_TOKEN", "")
RESEARCH_API_MAX_ROWS = int(os.getenv("RESEARCH_API_MAX_ROWS", "200"))
