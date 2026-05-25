"""Central configuration: credentials, universe, paths, and strategy constants."""

import os
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# --- Alpaca (canonical: APCA_*; legacy ALPACA_* supported via get_alpaca_credentials) ---
# Paper-only by default. Set ALLOW_LIVE_TRADING=yes in .env to override PAPER_TRADING=False.
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes")
ALLOW_LIVE_TRADING = os.getenv("ALLOW_LIVE_TRADING", "").lower() in ("1", "true", "yes")

# --- Universe (single source of truth) ---
UNIVERSE = [
    "BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "AVAX-USD", "LINK-USD",
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "AMZN", "TSLA", "META",
    "VTI", "QQQ", "SPY", "IWM",
    "XOM", "CVX", "LNG",
    "RTX", "LMT", "KTOS",
    "JPM", "BAC", "GS",
    "JNJ", "UNH", "PFE",
]

# --- Paths ---
DB_PATH = "market_data.db"
LEDGER_PATH = "trading_history.jsonl"
TRADE_HISTORY_LOG = "trade_history.log"
RISK_EVENTS_LOG = "risk_events.log"
PAPER_JOURNAL_CSV = "paper_journal.csv"
HEARTBEAT_FILE = "bot_heartbeat.json"

# --- Strategy ---
TICKER = "VTI"
ASSET_TYPE = "STOCK"
MA_WINDOW = 45

# --- SPY bot (run_spy.py) — tuned from 500-day backtest grid ---
SPY_BOT_SYMBOL = "SPY"
SPY_MA_WINDOW = 200
SPY_RISK_PER_TRADE = 1.00
SPY_EXIT_ON_MA_BREAK = False
SPY_MA_WINDOWS = [20, 50, 100, 200]
SPY_ALLOCATIONS = [0.10, 0.25, 0.50, 1.00]
SPY_BACKTEST_RESULTS = "spy_backtest_results.csv"
SPY_HEARTBEAT_FILE = "spy_bot_heartbeat.json"
REFRESH_INTERVAL = 900
MAX_DRAWDOWN_PCT = 0.10
BACKTEST_DAYS = 365

# --- Risk & sizing (paper month defaults) ---
RISK_PER_TRADE = 0.02
MAX_NOTIONAL_PER_ORDER = 10000.0
MIN_NOTIONAL = 10.0
MAX_OPEN_POSITIONS = 5
STOP_LOSS_PCT = 0.05
CRYPTO_MIN_CORRELATION = 0.5

# Crypto tickers in UNIVERSE use NAME-USD (e.g. BTC-USD), not bare "USD" substring
CRYPTO_TICKERS = frozenset(
    t for t in UNIVERSE if t.endswith("-USD")
)


def get_alpaca_credentials():
    """Return (api_key, secret_key). Prefers APCA_*; falls back to legacy ALPACA_*."""
    key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")
    secret = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise ValueError(
            "Alpaca credentials missing. Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env"
        )
    return key, secret


def get_tavily_api_key():
    return os.getenv("TAVILY_API_KEY")


def get_kraken_credentials():
    """Return (api_key, secret). Accepts KRAKEN_SECRET_KEY or KRAKEN_API_SECRET."""
    key = os.getenv("KRAKEN_API_KEY")
    secret = os.getenv("KRAKEN_SECRET_KEY") or os.getenv("KRAKEN_API_SECRET")
    return key, secret


def get_telegram_config():
    """Return (bot_token, chat_id) or None if not configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return token, chat_id
    return None


def get_smtp_config():
    """SMTP settings for email alerts."""
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", "").strip(),
        "to": os.getenv("ALERT_EMAIL_TO", "").strip(),
        "from": os.getenv("ALERT_EMAIL_FROM", "").strip(),
    }


def is_crypto(symbol: str) -> bool:
    """True for universe crypto pairs (BTC-USD) or Alpaca format (BTC/USD)."""
    normalized = symbol.replace("/", "-")
    return normalized in CRYPTO_TICKERS


def crypto_universe():
    return [t for t in UNIVERSE if is_crypto(t)]


def equity_universe():
    return [t for t in UNIVERSE if not is_crypto(t)]
