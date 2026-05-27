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

# ── External API ─────────────────────────────────────────────────────────────
KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://demo-api.kalshi.co/trade-api/v2")
