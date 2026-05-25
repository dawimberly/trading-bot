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

# --- Fund sleeves (run_all.py) — 85% deployed, 15% cash buffer ---
FUND_CASH_BUFFER_PCT = 0.15
SPY_SLEEVE_CAP_PCT = 0.45
NYSE_SLEEVE_CAP_PCT = 0.20
CRYPTO_SLEEVE_CAP_PCT = 0.20
CRYPTO_VOL_ONLY = True  # crypto pairs only when cross-asset volatility is High

# --- SPY sleeve settings ---
SPY_BOT_SYMBOL = "SPY"
SPY_MA_WINDOW = 200
SPY_RISK_PER_TRADE = SPY_SLEEVE_CAP_PCT  # legacy alias for backtests
SPY_EXIT_ON_MA_BREAK = False
SPY_MA_WINDOWS = [20, 50, 100, 200]
SPY_ALLOCATIONS = [0.10, 0.25, 0.50, 1.00]
SPY_BACKTEST_RESULTS = "spy_backtest_results.csv"
SPY_HEARTBEAT_FILE = "spy_bot_heartbeat.json"
SPY_TRADE_HISTORY_LOG = "spy_trade_history.log"
SPY_PAPER_JOURNAL_CSV = "spy_paper_journal.csv"
SPY_LEDGER_PATH = "spy_trading_history.jsonl"
SPY_RISK_EVENTS_LOG = "spy_risk_events.log"
REFRESH_INTERVAL = 900
MAX_DRAWDOWN_PCT = 0.10
BACKTEST_DAYS = 365

# --- Sentiment (regime input) — "price" is free and matches backtests ---
SENTIMENT_SOURCE = os.getenv("SENTIMENT_SOURCE", "price").strip().lower()

# --- Wisdom layer (web mood + price math -> RHYME; see backtester_wisdom.py) ---
# baseline | web_regime | arbitrage | wisdom_pause
WISDOM_MODE = os.getenv("WISDOM_MODE", "arbitrage").strip().lower()
WISDOM_GAP_THRESHOLD = float(os.getenv("WISDOM_GAP_THRESHOLD", "0.25"))
WEB_SENTIMENT_CACHE_FILE = "web_sentiment_live.json"
WEB_SENTIMENT_CACHE_HOURS = int(os.getenv("WEB_SENTIMENT_CACHE_HOURS", "24"))

# --- Wisdom self-evaluation (journal + rolling scorecard + monthly rollup) ---
WISDOM_EVAL_ENABLED = os.getenv("WISDOM_EVAL_ENABLED", "true").lower() in ("1", "true", "yes")
WISDOM_EVAL_DAYS = int(os.getenv("WISDOM_EVAL_DAYS", "30"))
WISDOM_MONTHLY_ENABLED = os.getenv("WISDOM_MONTHLY_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
WISDOM_JOURNAL_FILE = "wisdom_journal.csv"
WISDOM_SCORECARD_FILE = "wisdom_scorecard.json"
WISDOM_EVAL_HISTORY_FILE = "wisdom_evaluations.jsonl"
WISDOM_EVAL_STATE_FILE = "wisdom_eval_state.json"
WISDOM_MONTHLY_HISTORY_FILE = "wisdom_monthly_history.jsonl"

# --- Holdings reconcile (Alpaca vs ledger vs sleeve caps) ---
RECONCILE_ON_STARTUP = os.getenv("RECONCILE_ON_STARTUP", "true").lower() in (
    "1",
    "true",
    "yes",
)
TRIM_OVER_CAP_ON_STARTUP = os.getenv("TRIM_OVER_CAP_ON_STARTUP", "true").lower() in (
    "1",
    "true",
    "yes",
)
REBALANCE_ON_STARTUP = os.getenv("REBALANCE_ON_STARTUP", "false").lower() in (
    "1",
    "true",
    "yes",
)

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


def get_spy_alpaca_credentials():
    """SPY bot keys: SPY_APCA_* if set, else main APCA_* (same paper account)."""
    key = os.getenv("SPY_APCA_API_KEY_ID") or os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")
    secret = (
        os.getenv("SPY_APCA_API_SECRET_KEY")
        or os.getenv("APCA_API_SECRET_KEY")
        or os.getenv("ALPACA_SECRET_KEY")
    )
    if not key or not secret:
        raise ValueError(
            "Alpaca credentials missing. Set SPY_APCA_* or APCA_* in .env"
        )
    return key, secret


def spy_uses_separate_alpaca_account():
    return bool(os.getenv("SPY_APCA_API_KEY_ID") and os.getenv("SPY_APCA_API_SECRET_KEY"))


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
    """True for universe crypto pairs (BTC-USD) or Alpaca format (BTC/USD, BTCUSD)."""
    return normalize_symbol(symbol) in CRYPTO_TICKERS


def normalize_symbol(symbol: str) -> str:
    """Alpaca (BTCUSD, BTC/USD) -> universe form (BTC-USD)."""
    s = symbol.replace("/", "-")
    if s.endswith("USD") and "-" not in s:
        return f"{s[:-3]}-USD"
    return s


def crypto_universe():
    return [t for t in UNIVERSE if is_crypto(t)]


def equity_universe():
    return [t for t in UNIVERSE if not is_crypto(t)]
