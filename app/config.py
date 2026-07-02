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
KALSHI_API_BASE            = os.getenv("KALSHI_API_BASE", "https://external-api.kalshi.com/trade-api/v2")
KALSHI_API_TIMEOUT_SECONDS = float(os.getenv("KALSHI_API_TIMEOUT_SECONDS", "8"))

# ── Active 15-minute BTC binary market selection ─────────────────────────────
# Series used by the core BTC up/down logger. Keep this separate from the
# hourly range tracker configuration so the logger never piggybacks on range
# market settings.
KALSHI_BTC_BINARY_SERIES_TICKER = os.getenv("KALSHI_BTC_BINARY_SERIES_TICKER", "KXBTC").strip()
# Reject binary markets whose strike is implausibly far from spot.
KALSHI_BTC_BINARY_MAX_STRIKE_GAP = float(os.getenv("KALSHI_BTC_BINARY_MAX_STRIKE_GAP", "1500"))
# The active 15-minute market should be closing soon; allow a little slack.
KALSHI_BTC_BINARY_MAX_TIME_TO_CLOSE_SECONDS = int(
    os.getenv("KALSHI_BTC_BINARY_MAX_TIME_TO_CLOSE_SECONDS", "1200")
)

# ── Kalshi API authentication (RSA key pair) ──────────────────────────────────
# Both must be set together to enable authenticated requests.
# KALSHI_KEY_ID   — the key ID shown in your Kalshi API settings
# KALSHI_KEY_FILE — path to the RSA private key PEM file
# When either is absent, requests fall back to unauthenticated (public endpoints only).
KALSHI_KEY_ID   = os.getenv("KALSHI_KEY_ID",   "").strip()
KALSHI_KEY_FILE = os.getenv("KALSHI_KEY_FILE", "").strip()

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


# ══════════════════════════════════════════════════════════════════════════════
# Momentum LIVE trading (real money) — SEPARATE, EXPLICITLY GATED subsystem
# ══════════════════════════════════════════════════════════════════════════════
#
# These flags control app/momentum_live_trader.py ONLY.  They are intentionally
# namespaced (MOMENTUM_LIVE_*) and kept distinct from the legacy
# LIVE_TRADING_ENABLED flag, which continues to guard the research logger /
# paper pipeline (app/db.py refuses to build a pool when LIVE_TRADING_ENABLED is
# true).  The live trader runs WITH LIVE_TRADING_ENABLED=false and its own
# explicit MOMENTUM_LIVE_* gates set.
#
# DEFAULTS ARE NON-LIVE.  No real order is ever sent unless ALL required gates
# below are explicitly set:
#   1. MOMENTUM_LIVE_ENABLED=true
#   2. MOMENTUM_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY   (exact string)
#   3. KALSHI_KEY_ID and KALSHI_KEY_FILE configured (authenticated client)
#   4. Kill switch not engaged
#   5. Sane bankroll / per-trade dollar caps configured (> 0)
# ──────────────────────────────────────────────────────────────────────────────

def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() == "true"


# ── Master gates ──────────────────────────────────────────────────────────────
MOMENTUM_LIVE_ENABLED = _env_bool("MOMENTUM_LIVE_ENABLED", "false")
# Must equal this exact token for live execution to be permitted.
MOMENTUM_LIVE_CONFIRM_TOKEN = "I_UNDERSTAND_REAL_MONEY"
MOMENTUM_LIVE_CONFIRM = os.getenv("MOMENTUM_LIVE_CONFIRM", "").strip()

# ── Kill switch ───────────────────────────────────────────────────────────────
# Engaged when either the env flag is true OR the kill-switch file exists.
MOMENTUM_LIVE_KILL_SWITCH = _env_bool("MOMENTUM_LIVE_KILL_SWITCH", "false")
MOMENTUM_LIVE_KILL_SWITCH_FILE = os.getenv(
    "MOMENTUM_LIVE_KILL_SWITCH_FILE", ""
).strip()

# ── Bankroll + Kelly sizing ───────────────────────────────────────────────────
# Bankroll in whole dollars.  Kelly fraction is a multiplier on the computed
# full-Kelly stake (e.g. 0.25 = quarter Kelly).  Per-trade dollar and contract
# caps bound the stake regardless of what Kelly suggests.
MOMENTUM_LIVE_BANKROLL_DOLLARS       = float(os.getenv("MOMENTUM_LIVE_BANKROLL_DOLLARS", "0"))
MOMENTUM_LIVE_KELLY_FRACTION         = float(os.getenv("MOMENTUM_LIVE_KELLY_FRACTION", "0"))
MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE  = float(os.getenv("MOMENTUM_LIVE_MAX_DOLLARS_PER_TRADE", "0"))
# 0 = no explicit contract cap (still bounded by the dollar caps above).
MOMENTUM_LIVE_MAX_CONTRACTS_PER_TRADE = int(os.getenv("MOMENTUM_LIVE_MAX_CONTRACTS_PER_TRADE", "0"))

# ── Risk gates ────────────────────────────────────────────────────────────────
MOMENTUM_LIVE_MAX_ACTIVE_TRADES      = int(os.getenv("MOMENTUM_LIVE_MAX_ACTIVE_TRADES", "1"))
# Max cumulative realized loss (whole dollars, positive number) per UTC day
# before new live entries are blocked.  0 disables the daily-loss gate.
MOMENTUM_LIVE_MAX_DAILY_LOSS_DOLLARS = float(os.getenv("MOMENTUM_LIVE_MAX_DAILY_LOSS_DOLLARS", "0"))
# Max bid-ask spread (dollar fraction) tolerated at entry. 0.04 = 4 cents.
MOMENTUM_LIVE_MAX_SPREAD             = float(os.getenv("MOMENTUM_LIVE_MAX_SPREAD", "0.04"))
# Reject entry if the quote we are acting on is older than this many seconds.
MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS  = float(os.getenv("MOMENTUM_LIVE_QUOTE_MAX_AGE_SECONDS", "5"))
# Minimum number of COMPLETE shadow trades required before any live sizing /
# entry is permitted (projected stats must be statistically meaningful).
MOMENTUM_LIVE_MIN_SHADOW_TRADES      = int(os.getenv("MOMENTUM_LIVE_MIN_SHADOW_TRADES", "50"))
# Rolling window (most-recent COMPLETE shadow trades) used to derive projected
# win-rate / expectancy / profit-factor for Kelly sizing.
MOMENTUM_LIVE_PROJECTED_WINDOW       = int(os.getenv("MOMENTUM_LIVE_PROJECTED_WINDOW", "100"))
# Small positive offset (dollar fraction) added to the shadow entry ask when
# placing a LIVE entry order. 0.01 = pay up 1 cent to improve fill odds while
# keeping the projected shadow entry unchanged for comparison.
MOMENTUM_LIVE_ENTRY_PRICE_OFFSET_CENTS = float(
    os.getenv("MOMENTUM_LIVE_ENTRY_PRICE_OFFSET_CENTS", "0.01")
)
# After a REJECTED or unfilled CANCELED entry, wait this many seconds before
# retrying the same contract/side. This is intentionally much shorter than the
# full trade cooldown used after an actual fill.
MOMENTUM_LIVE_RETRY_AFTER_CANCEL_SECONDS = float(
    os.getenv("MOMENTUM_LIVE_RETRY_AFTER_CANCEL_SECONDS", "20")
)
# How long (seconds) a resting entry order may sit unfilled before it is
# cancelled (do not chase; do not fabricate fills).
MOMENTUM_LIVE_ENTRY_FILL_TIMEOUT_SECONDS = float(
    os.getenv("MOMENTUM_LIVE_ENTRY_FILL_TIMEOUT_SECONDS", "10")
)

# ── Automatic pause (live actual drifting below shadow projected) ──────────────
# Pause is evaluated over rolling windows of the last 25 / 50 / 100 live trades.
# A window only counts once it has at least LIVE_PAUSE_MIN_TRADES completed live
# trades (do not pause on tiny samples).  If, in any qualifying window, live
# actual performance trails shadow-projected by more than any threshold below,
# new live entries are paused until a human manually unpauses.
LIVE_PAUSE_MIN_TRADES            = int(os.getenv("LIVE_PAUSE_MIN_TRADES", "25"))
# Win-rate gap in percentage POINTS (projected_pct - actual_pct).
LIVE_PAUSE_WIN_RATE_GAP_PCT      = float(os.getenv("LIVE_PAUSE_WIN_RATE_GAP_PCT", "15"))
# Expectancy gap in dollar fraction (projected - actual). 0.02 = 2 cents/contract.
LIVE_PAUSE_EXPECTANCY_GAP_CENTS  = float(os.getenv("LIVE_PAUSE_EXPECTANCY_GAP_CENTS", "0.02"))
# Profit-factor gap (projected_pf - actual_pf).
LIVE_PAUSE_PROFIT_FACTOR_GAP     = float(os.getenv("LIVE_PAUSE_PROFIT_FACTOR_GAP", "0.5"))
# Rolling windows evaluated for pause.
LIVE_PAUSE_WINDOWS = (25, 50, 100)

# ── Optional WebSocket state layer for live trading ───────────────────────────
# REST order placement remains the source of writes; the WebSocket provides
# fresher quote + order-state observation when available.
MOMENTUM_LIVE_USE_WEBSOCKET = _env_bool("MOMENTUM_LIVE_USE_WEBSOCKET", "false")
KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "").strip()
MOMENTUM_LIVE_WS_RECONNECT_SECONDS = float(
    os.getenv("MOMENTUM_LIVE_WS_RECONNECT_SECONDS", "3")
)
MOMENTUM_LIVE_WS_STALE_AFTER_SECONDS = float(
    os.getenv("MOMENTUM_LIVE_WS_STALE_AFTER_SECONDS", "5")
)
MOMENTUM_LIVE_WS_PING_INTERVAL_SECONDS = float(
    os.getenv("MOMENTUM_LIVE_WS_PING_INTERVAL_SECONDS", "20")
)
MOMENTUM_LIVE_WS_PING_TIMEOUT_SECONDS = float(
    os.getenv("MOMENTUM_LIVE_WS_PING_TIMEOUT_SECONDS", "20")
)

# ── WebSocket-first observer layer ───────────────────────────────────────────
# This is the clean-data mode: Kalshi REST may still discover markets, but the
# YES/NO quote inputs handed to shadow/live diagnostics must come from a fresh
# WebSocket order book. When enabled, stale/missing WS quotes skip the tick.
MOMENTUM_WS_OBSERVER_ENABLED = _env_bool("MOMENTUM_WS_OBSERVER_ENABLED", "false")
MOMENTUM_WS_OBSERVER_REQUIRE_FRESH_QUOTES = _env_bool(
    "MOMENTUM_WS_OBSERVER_REQUIRE_FRESH_QUOTES", "true"
)
MOMENTUM_WS_OBSERVER_BOOT_TIMEOUT_SECONDS = float(
    os.getenv("MOMENTUM_WS_OBSERVER_BOOT_TIMEOUT_SECONDS", "10")
)
# Crossed WS books can appear briefly while fast deltas are applying. Reject the
# bad tick immediately, but only force a stream resync if the state persists.
MOMENTUM_WS_OBSERVER_CROSSED_RESYNC_SECONDS = float(
    os.getenv("MOMENTUM_WS_OBSERVER_CROSSED_RESYNC_SECONDS", "8")
)
# Only drop/reconnect immediately when the reconstructed WS book is severely
# crossed. Small/medium crosses are common during fast deltas and are handled by
# the observer debounce above, which rejects those ticks without recording them.
MOMENTUM_WS_ORDERBOOK_MAX_CROSSED_AMOUNT = float(
    os.getenv("MOMENTUM_WS_ORDERBOOK_MAX_CROSSED_AMOUNT", "0.10")
)

# ── WebSocket shadow-only exit audit (diagnostics only) ───────────────────────
# These rules never place/cancel/modify real orders. They only record the first
# time a hypothetical WS-aware exit would have triggered for a live trade.
MOMENTUM_LIVE_WS_EXIT_SHADOW = _env_bool("MOMENTUM_LIVE_WS_EXIT_SHADOW", "false")
MOMENTUM_LIVE_WS_SHADOW_TP2_ENABLED = _env_bool(
    "MOMENTUM_LIVE_WS_SHADOW_TP2_ENABLED", "true"
)
MOMENTUM_LIVE_WS_SHADOW_TP3_ENABLED = _env_bool(
    "MOMENTUM_LIVE_WS_SHADOW_TP3_ENABLED", "true"
)
MOMENTUM_LIVE_WS_SHADOW_TP5_ENABLED = _env_bool(
    "MOMENTUM_LIVE_WS_SHADOW_TP5_ENABLED", "true"
)
MOMENTUM_LIVE_WS_SHADOW_PROFIT_PROTECTION_ENABLED = _env_bool(
    "MOMENTUM_LIVE_WS_SHADOW_PROFIT_PROTECTION_ENABLED", "true"
)
# Shadow-exit thresholds below are literal cents, not repo price units:
# 3 means +3c, 1 means +1c, 0 means breakeven.
MOMENTUM_LIVE_WS_SHADOW_PROFIT_PROTECTION_TRIGGER_CENTS = float(
    os.getenv("MOMENTUM_LIVE_WS_SHADOW_PROFIT_PROTECTION_TRIGGER_CENTS", "3")
)
MOMENTUM_LIVE_WS_SHADOW_PROFIT_PROTECTION_FLOOR_CENTS = float(
    os.getenv("MOMENTUM_LIVE_WS_SHADOW_PROFIT_PROTECTION_FLOOR_CENTS", "1")
)
MOMENTUM_LIVE_WS_SHADOW_GIVEBACK_ENABLED = _env_bool(
    "MOMENTUM_LIVE_WS_SHADOW_GIVEBACK_ENABLED", "true"
)
MOMENTUM_LIVE_WS_SHADOW_GIVEBACK_TRIGGER_CENTS = float(
    os.getenv("MOMENTUM_LIVE_WS_SHADOW_GIVEBACK_TRIGGER_CENTS", "2")
)
MOMENTUM_LIVE_WS_SHADOW_GIVEBACK_FLOOR_CENTS = float(
    os.getenv("MOMENTUM_LIVE_WS_SHADOW_GIVEBACK_FLOOR_CENTS", "0")
)
MOMENTUM_LIVE_WS_SHADOW_TIME_PROGRESS_ENABLED = _env_bool(
    "MOMENTUM_LIVE_WS_SHADOW_TIME_PROGRESS_ENABLED", "true"
)
MOMENTUM_LIVE_WS_SHADOW_TIME_PROGRESS_SECONDS = float(
    os.getenv("MOMENTUM_LIVE_WS_SHADOW_TIME_PROGRESS_SECONDS", "60")
)
MOMENTUM_LIVE_WS_SHADOW_TIME_PROGRESS_MIN_PROFIT_CENTS = float(
    os.getenv("MOMENTUM_LIVE_WS_SHADOW_TIME_PROGRESS_MIN_PROFIT_CENTS", "0")
)

# ── Momentum entry filters ───────────────────────────────────────────────────
# Block the bucket that has been the largest TP3C/stop-loss leak so far:
# high-priced NO entries. 0.50 = 50 cents.
MOMENTUM_BLOCK_HIGH_NO_ENABLED = _env_bool("MOMENTUM_BLOCK_HIGH_NO_ENABLED", "true")
MOMENTUM_BLOCK_NO_ENTRY_ASK_MIN = float(
    os.getenv("MOMENTUM_BLOCK_NO_ENTRY_ASK_MIN", "0.50")
)

# ── Optional exit experiments (disabled by default) ───────────────────────────
MOMENTUM_LIVE_PLACE_RESTING_TP = _env_bool("MOMENTUM_LIVE_PLACE_RESTING_TP", "false")
MOMENTUM_LIVE_TP_CENTS = float(os.getenv("MOMENTUM_LIVE_TP_CENTS", "0.03"))
MOMENTUM_LIVE_STOP_LOSS_ENABLED = _env_bool("MOMENTUM_LIVE_STOP_LOSS_ENABLED", "false")
# Dollar fraction below entry. 0.04 = 4 cents.
MOMENTUM_LIVE_STOP_LOSS_CENTS = float(
    os.getenv("MOMENTUM_LIVE_STOP_LOSS_CENTS", "0.04")
)
MOMENTUM_LIVE_PROFIT_PROTECTION_ENABLED = _env_bool(
    "MOMENTUM_LIVE_PROFIT_PROTECTION_ENABLED", "false"
)
MOMENTUM_LIVE_PROFIT_PROTECTION_TRIGGER_CENTS = float(
    os.getenv("MOMENTUM_LIVE_PROFIT_PROTECTION_TRIGGER_CENTS", "0.03")
)
MOMENTUM_LIVE_PROFIT_PROTECTION_FLOOR_CENTS = float(
    os.getenv("MOMENTUM_LIVE_PROFIT_PROTECTION_FLOOR_CENTS", "0.01")
)
MOMENTUM_LIVE_TIME_PROGRESS_EXIT_ENABLED = _env_bool(
    "MOMENTUM_LIVE_TIME_PROGRESS_EXIT_ENABLED", "false"
)
MOMENTUM_LIVE_TIME_PROGRESS_SECONDS = float(
    os.getenv("MOMENTUM_LIVE_TIME_PROGRESS_SECONDS", "60")
)
MOMENTUM_LIVE_TIME_PROGRESS_MIN_PROFIT_CENTS = float(
    os.getenv("MOMENTUM_LIVE_TIME_PROGRESS_MIN_PROFIT_CENTS", "0")
)
