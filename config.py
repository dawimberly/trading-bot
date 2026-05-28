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
    "GLD", "SLV", "CPER", "URA", "PPLT", "DBB", "GDX",
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

# --- Fund sleeves (run_all.py) — 85% deployed, 15% cash buffer (see effective_* when game plan on) ---
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
# baseline | web_regime | arbitrage | wisdom_pause | governor
WISDOM_MODE = os.getenv("WISDOM_MODE", "arbitrage").strip().lower()
WISDOM_GAP_THRESHOLD = float(os.getenv("WISDOM_GAP_THRESHOLD", "0.25"))
WEB_SENTIMENT_CACHE_FILE = "web_sentiment_live.json"
WEB_SENTIMENT_CACHE_HOURS = int(os.getenv("WEB_SENTIMENT_CACHE_HOURS", "24"))

# --- SpaceX IPO ↔ crypto monitor (headline watch; S-1 BTC treasury narrative) ---
SPACEX_IPO_MONITOR_ENABLED = os.getenv("SPACEX_IPO_MONITOR_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
SPACEX_IPO_CACHE_FILE = "spacex_ipo_monitor.json"
SPACEX_IPO_HISTORY_FILE = "spacex_ipo_monitor_history.jsonl"
SPACEX_IPO_CACHE_HOURS = int(os.getenv("SPACEX_IPO_CACHE_HOURS", "6"))
SPACEX_IPO_ALERT_HEADLINES = int(os.getenv("SPACEX_IPO_ALERT_HEADLINES", "3"))
# Open crypto sleeve when SpaceX/BTC narrative hot despite Low 5m vol
SPACEX_IPO_CRYPTO_OVERRIDE = os.getenv("SPACEX_IPO_CRYPTO_OVERRIDE", "true").lower() in (
    "1",
    "true",
    "yes",
)
SPACEX_CRYPTO_OVERRIDE_MIN_BTC_HEADLINES = int(
    os.getenv("SPACEX_CRYPTO_OVERRIDE_MIN_BTC_HEADLINES", "3")
)
SPACEX_CRYPTO_OVERRIDE_MIN_SPCX_PERP = int(
    os.getenv("SPACEX_CRYPTO_OVERRIDE_MIN_SPCX_PERP", "1")
)
SPACEX_CRYPTO_OVERRIDE_MIN_SENTIMENT = float(
    os.getenv("SPACEX_CRYPTO_OVERRIDE_MIN_SENTIMENT", "-0.35")
)

# --- Real SpaceX IPO listing (Nasdaq SPCX): SEC + Alpaca tradability ---
SPACEX_IPO_LISTING_MONITOR_ENABLED = os.getenv(
    "SPACEX_IPO_LISTING_MONITOR_ENABLED", "true"
).lower() in ("1", "true", "yes")
SPACEX_IPO_TICKER = os.getenv("SPACEX_IPO_TICKER", "SPCX").strip().upper()
SPACEX_IPO_CIK = int(os.getenv("SPACEX_IPO_CIK", "1181412"))
SPACEX_IPO_EXPECTED_DATE = os.getenv("SPACEX_IPO_EXPECTED_DATE", "2026-06-12")
SPACEX_IPO_LISTING_CACHE_FILE = "spacex_ipo_listing.json"
SPACEX_IPO_LISTING_HISTORY_FILE = "spacex_ipo_listing_history.jsonl"
SPACEX_IPO_LISTING_CACHE_HOURS = int(os.getenv("SPACEX_IPO_LISTING_CACHE_HOURS", "1"))
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "PythonTradingBot/1.0 (personal research)")
# Optional: market buy SPCX on Alpaca the first cycle it becomes tradable (paper only by default)
_auto_buy_env = os.getenv("SPACEX_IPO_AUTO_BUY")
if _auto_buy_env is None:
    SPACEX_IPO_AUTO_BUY = PAPER_TRADING
else:
    SPACEX_IPO_AUTO_BUY = _auto_buy_env.lower() in ("1", "true", "yes")
SPACEX_IPO_BUY_NOTIONAL = float(os.getenv("SPACEX_IPO_BUY_NOTIONAL", "2500"))

# Kraken Pro SPCX (xStock or equity pair) when IPO lists on Kraken API
ALLOW_KRAKEN_TRADING = os.getenv("ALLOW_KRAKEN_TRADING", "").lower() in (
    "1",
    "true",
    "yes",
)
KRAKEN_SPCX_BUY_ENABLED = os.getenv("KRAKEN_SPCX_BUY_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Legacy alias
if os.getenv("KRAKEN_IPO_BUY_ENABLED", "").lower() in ("1", "true", "yes"):
    KRAKEN_SPCX_BUY_ENABLED = True
KRAKEN_SPCX_BUY_USD = float(
    os.getenv("KRAKEN_SPCX_BUY_USD", os.getenv("KRAKEN_IPO_BUY_USD", "500"))
)
KRAKEN_SPCX_PAIR = os.getenv("KRAKEN_SPCX_PAIR", "").strip().upper()

# Kraken autopilot: cleanup + crypto mirror + paper-bot mirror (wisdom + game plan gates)
KRAKEN_AUTOPILOT_ENABLED = os.getenv("KRAKEN_AUTOPILOT_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Default dry-run: validate orders only until you set KRAKEN_DRY_RUN=false
KRAKEN_DRY_RUN = os.getenv("KRAKEN_DRY_RUN", "true").lower() in ("1", "true", "yes")
KRAKEN_AUTOPILOT_CLEANUP = os.getenv("KRAKEN_AUTOPILOT_CLEANUP", "true").lower() in (
    "1",
    "true",
    "yes",
)
KRAKEN_AUTOPILOT_CRYPTO_MIRROR = os.getenv(
    "KRAKEN_AUTOPILOT_CRYPTO_MIRROR", "true"
).lower() in ("1", "true", "yes")
KRAKEN_AUTOPILOT_MIRROR = os.getenv("KRAKEN_AUTOPILOT_MIRROR", "true").lower() in (
    "1",
    "true",
    "yes",
)
KRAKEN_MAX_ORDER_USD = float(os.getenv("KRAKEN_MAX_ORDER_USD", "25"))
# Max USD on buys per autopilot cycle (0 = no cycle cap, only KRAKEN_MAX_ORDER_USD)
KRAKEN_CYCLE_BUDGET_USD = float(os.getenv("KRAKEN_CYCLE_BUDGET_USD", "0"))
KRAKEN_CRYPTO_NOTIONAL = float(os.getenv("KRAKEN_CRYPTO_NOTIONAL", "15"))
KRAKEN_CLEANUP_MAX_ACTIONS = int(os.getenv("KRAKEN_CLEANUP_MAX_ACTIONS", "3"))
# Kraken stocks tab: simplify toward this many names (cleanup mode); not applied to Alpaca
KRAKEN_MAX_POSITIONS = int(os.getenv("KRAKEN_MAX_POSITIONS", "5"))
# Min base volume to treat as a real position (skip dust after partial sells)
KRAKEN_DUST_VOLUME = float(os.getenv("KRAKEN_DUST_VOLUME", "0.1"))
KRAKEN_REBALANCE_ENABLED = os.getenv("KRAKEN_REBALANCE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
KRAKEN_REBALANCE_MAX_TRADES = int(os.getenv("KRAKEN_REBALANCE_MAX_TRADES", "6"))
KRAKEN_REBALANCE_FORCE = os.getenv("KRAKEN_REBALANCE_FORCE", "false").lower() in (
    "1",
    "true",
    "yes",
)
# When true, skip Telegram manual stock alerts; run_all logs only
KRAKEN_NO_MANUAL_ALERTS = os.getenv("KRAKEN_NO_MANUAL_ALERTS", "true").lower() in (
    "1",
    "true",
    "yes",
)

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

# --- Game plan (yield gate + metal sleeve + stress cash) — backtest: game_plan_gld / gld_slv_cper ---
GAME_PLAN_ENABLED = os.getenv("GAME_PLAN_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
YIELD_GATE_ENABLED = os.getenv("YIELD_GATE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
METAL_SLEEVE_CAP_PCT = float(os.getenv("METAL_SLEEVE_CAP_PCT", "0.10"))
METAL_SLEEVE_DEPLOY_PCT = float(os.getenv("METAL_SLEEVE_DEPLOY_PCT", "0.90"))
METAL_BLEND_GLD = float(os.getenv("METAL_BLEND_GLD", "0.50"))
METAL_BLEND_SLV = float(os.getenv("METAL_BLEND_SLV", "0.30"))
METAL_BLEND_CPER = float(os.getenv("METAL_BLEND_CPER", "0.20"))
STRESS_CASH_PCT = float(os.getenv("STRESS_CASH_PCT", "0.25"))
# Live bot trades GLD/SLV/CPER only; backtester may use the full set below.
LIVE_METAL_SYMBOLS = frozenset({"GLD", "SLV", "CPER"})
METAL_SYMBOLS = frozenset({"GLD", "SLV", "CPER", "URA", "PPLT", "DBB", "GDX"})
MACRO_DAILY_TICKERS = ("TLT", "SPY", *sorted(METAL_SYMBOLS))

_universe_set = frozenset(UNIVERSE)
_metal_not_in_universe = METAL_SYMBOLS - _universe_set
if _metal_not_in_universe:
    raise ValueError(
        f"METAL_SYMBOLS not in UNIVERSE: {sorted(_metal_not_in_universe)}"
    )
if not LIVE_METAL_SYMBOLS <= METAL_SYMBOLS:
    raise ValueError("LIVE_METAL_SYMBOLS must be a subset of METAL_SYMBOLS")

# --- Risk & sizing (paper month defaults) ---
RISK_PER_TRADE = 0.02
MAX_NOTIONAL_PER_ORDER = 10000.0
MIN_NOTIONAL = 10.0
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
    return [t for t in UNIVERSE if not is_crypto(t) and t not in METAL_SYMBOLS]


def is_metal_symbol(symbol: str) -> bool:
    """True for live-traded metal ETFs (GLD/SLV/CPER), not research-only metals."""
    return normalize_symbol(symbol) in LIVE_METAL_SYMBOLS


def live_metal_universe() -> list[str]:
    return sorted(LIVE_METAL_SYMBOLS)


def metal_blend_weights() -> dict[str, float]:
    weights = {
        "GLD": METAL_BLEND_GLD,
        "SLV": METAL_BLEND_SLV,
        "CPER": METAL_BLEND_CPER,
    }
    return validate_metal_weights(weights, allowed=LIVE_METAL_SYMBOLS, strategy="live_blend")


def validate_metal_weights(
    weights: dict[str, float],
    *,
    allowed: frozenset | None = None,
    available: frozenset | set | None = None,
    strategy: str = "",
) -> dict[str, float]:
    """Ensure metal weights use allowed symbols and sum to 1.0 — never re-normalize."""
    if not weights:
        raise ValueError("Metal weights cannot be empty")
    label = f" ({strategy})" if strategy else ""
    allowed_set = allowed or METAL_SYMBOLS
    unsupported = sorted(s for s in weights if s not in allowed_set)
    if unsupported:
        raise ValueError(
            f"Metal strategy{label} uses unsupported symbols {unsupported}. "
            f"Allowed: {sorted(allowed_set)}"
        )
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            f"Metal strategy{label} weights must sum to 1.0, got {total:.6f}: {weights}"
        )
    if available is not None:
        missing = sorted(s for s in weights if s not in available)
        if missing:
            raise ValueError(
                f"Metal strategy{label} missing price data for {missing}. "
                f"Available: {sorted(available)}"
            )
    return dict(weights)


def long_fund_scale() -> float:
    """Reserve headroom for metal sleeve when game plan is on."""
    if GAME_PLAN_ENABLED:
        return max(0.5, 1.0 - METAL_SLEEVE_CAP_PCT)
    return 1.0


def effective_sleeve_cap(base_pct: float) -> float:
    return round(base_pct * long_fund_scale(), 6)


def effective_cash_buffer_pct() -> float:
    """Cash headroom so long + metal sleeve caps sum to 100% of equity."""
    metal = METAL_SLEEVE_CAP_PCT if GAME_PLAN_ENABLED else 0.0
    long_caps = (
        SPY_SLEEVE_CAP_PCT + CRYPTO_SLEEVE_CAP_PCT + NYSE_SLEEVE_CAP_PCT
    ) * long_fund_scale()
    cash = round(1.0 - metal - long_caps, 6)
    if cash < 0:
        raise ValueError(
            f"Fund over-allocated: metal {metal:.2%} + long sleeves {long_caps:.2%} "
            f"> 100%; reduce METAL_SLEEVE_CAP_PCT or base sleeve caps"
        )
    return cash


def fund_allocation_pct() -> dict[str, float]:
    """Current sleeve + cash cap fractions (sum to 1.0)."""
    return {
        "spy": effective_sleeve_cap(SPY_SLEEVE_CAP_PCT),
        "crypto": effective_sleeve_cap(CRYPTO_SLEEVE_CAP_PCT),
        "nyse": effective_sleeve_cap(NYSE_SLEEVE_CAP_PCT),
        "metal": METAL_SLEEVE_CAP_PCT if GAME_PLAN_ENABLED else 0.0,
        "cash_buffer": effective_cash_buffer_pct(),
    }


_alloc = fund_allocation_pct()
if abs(sum(_alloc.values()) - 1.0) > 1e-4:
    raise ValueError(f"Fund allocation must sum to 100%, got {_alloc}")
