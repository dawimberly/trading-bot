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

# --- Live stack: current_dynamic (Sharpe phase winner; override via .env) ---
# WISDOM_MODE=dynamic, game plan yield-gate-only, halt resume 8% + liquidate.
# Opt-in (default off): NYSE overlap, beta scaling, SPY MA exit, adaptive/cofire.
# DERIVED_BEAR_PAUSE stays off.
#
# --- VTI passive core + active satellite (backtest winner: 80/20 Sharpe vs active-only) ---
VTI_CORE_ENABLED = os.getenv("VTI_CORE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
VTI_CORE_PCT = float(os.getenv("VTI_CORE_PCT", "0.80"))
VTI_CORE_SYMBOL = os.getenv("VTI_CORE_SYMBOL", "VTI").strip().upper()
# Rebalance VTI when |current - target| / equity exceeds this (avoids daily churn)
VTI_CORE_REBALANCE_DRIFT_PCT = float(os.getenv("VTI_CORE_REBALANCE_DRIFT_PCT", "0.02"))

# Paper research book (PAPER_APCA_*) — aggressive profit mode; live ~$100 stays conservative
PAPER_AGGRESSIVE_ENABLED = os.getenv("PAPER_AGGRESSIVE", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_VTI_CORE_PCT = float(os.getenv("PAPER_VTI_CORE_PCT", "0.20"))
PAPER_SOCIAL_SLEEVE_CAP_PCT = float(os.getenv("PAPER_SOCIAL_SLEEVE_CAP_PCT", "0.20"))
PAPER_ACTIVE_SLEEVE_BOOST = float(os.getenv("PAPER_ACTIVE_SLEEVE_BOOST", "1.40"))
PAPER_WISDOM_SIZING_FLOOR = float(os.getenv("PAPER_WISDOM_SIZING_FLOOR", "1.0"))
PAPER_CRYPTO_VOL_ONLY = os.getenv("PAPER_CRYPTO_VOL_ONLY", "false").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_VTI_REBALANCE_DRIFT_PCT = float(os.getenv("PAPER_VTI_REBALANCE_DRIFT_PCT", "0.01"))

_paper_aggressive_ctx = False

# --- Fund sleeves (run_all.py) — 85% deployed, 15% cash buffer (see effective_* when game plan on) ---
FUND_CASH_BUFFER_PCT = 0.15
SPY_SLEEVE_CAP_PCT = 0.45
NYSE_SLEEVE_CAP_PCT = 0.20
# NYSE picks vs SPY sleeve: skip high-beta / high-corr names when SPY is active
_nyse_overlap_env = os.getenv("NYSE_OVERLAP_FILTER_ENABLED") or os.getenv(
    "NYSE_ANTI_OVERLAP_ENABLED", "false"
)
NYSE_OVERLAP_FILTER_ENABLED = _nyse_overlap_env.lower() in ("1", "true", "yes")
NYSE_ANTI_OVERLAP_ENABLED = NYSE_OVERLAP_FILTER_ENABLED
NYSE_SPY_CORR_MAX = float(os.getenv("NYSE_SPY_CORR_MAX", "0.80"))
NYSE_SPY_BETA_MAX = float(os.getenv("NYSE_SPY_BETA_MAX", "1.6"))
NYSE_SPY_CORR_LOOKBACK = int(os.getenv("NYSE_SPY_CORR_LOOKBACK", "60"))
NYSE_BETA_SCALING_ENABLED = os.getenv("NYSE_BETA_SCALING_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Max Tech names in top-3 momentum when SPY on (0 = disabled; 1 = sector test variant)
NYSE_SECTOR_TECH_CAP = int(os.getenv("NYSE_SECTOR_TECH_CAP", "0"))
CRYPTO_SLEEVE_CAP_PCT = 0.20
CRYPTO_VOL_ONLY = True  # crypto pairs only when cross-asset volatility is High

# --- SPY sleeve settings ---
SPY_BOT_SYMBOL = "SPY"
SPY_MA_WINDOW = 200
SPY_RISK_PER_TRADE = SPY_SLEEVE_CAP_PCT  # legacy alias for backtests
SPY_EXIT_ON_MA_BREAK = os.getenv("SPY_EXIT_ON_MA_BREAK", "false").lower() in (
    "1",
    "true",
    "yes",
)
SPY_MA_WINDOWS = [20, 50, 100, 200]
SPY_ALLOCATIONS = [0.10, 0.25, 0.50, 1.00]
SPY_LADDER_SIZING_ENABLED = os.getenv("SPY_LADDER_SIZING_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
SPY_BACKTEST_RESULTS = "spy_backtest_results.csv"
SPY_HEARTBEAT_FILE = "spy_bot_heartbeat.json"
SPY_TRADE_HISTORY_LOG = "spy_trade_history.log"
SPY_PAPER_JOURNAL_CSV = "spy_paper_journal.csv"
SPY_LEDGER_PATH = "spy_trading_history.jsonl"
SPY_RISK_EVENTS_LOG = "spy_risk_events.log"
REFRESH_INTERVAL = 900

# --- Scan schedule (run_all.py): crypto overnight; SPY/NYSE around US open ---
SCAN_SCHEDULE_ENABLED = os.getenv("SCAN_SCHEDULE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
# Equity prep starts this many minutes before the regular open (default 9:25 ET).
EQUITY_SCAN_BEFORE_OPEN_MIN = int(os.getenv("EQUITY_SCAN_BEFORE_OPEN_MIN", "5"))
# SPY/NYSE scans begin this many minutes after the open (default 9:35 ET).
EQUITY_SCAN_AFTER_OPEN_MIN = int(os.getenv("EQUITY_SCAN_AFTER_OPEN_MIN", "5"))
# Loop cadence: 60s during open prep + RTH; slower overnight (crypto-only).
CYCLE_INTERVAL_SEC = int(os.getenv("CYCLE_INTERVAL_SEC", "60"))
# 300s (5m) balances crypto stop-loss vs API noise; 900 aligns with REFRESH_INTERVAL.
CRYPTO_ONLY_CYCLE_INTERVAL_SEC = int(os.getenv("CRYPTO_ONLY_CYCLE_INTERVAL_SEC", "300"))
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "0.10"))
# Resume entries when drawdown falls below this (0 = never resume, legacy halt)
HALT_RESUME_DRAWDOWN_PCT = float(os.getenv("HALT_RESUME_DRAWDOWN_PCT", "0.08"))
HALT_LIQUIDATE_ON_BREACH = os.getenv("HALT_LIQUIDATE_ON_BREACH", "true").lower() in (
    "1",
    "true",
    "yes",
)
HALT_TARGET_CASH_PCT = float(os.getenv("HALT_TARGET_CASH_PCT", "0.25"))
BACKTEST_DAYS = 365

# --- Sentiment (regime input) — "price" is free and matches backtests ---
SENTIMENT_SOURCE = os.getenv("SENTIMENT_SOURCE", "price").strip().lower()
# Daily bars rarely hit ±0.5; lower for RHYME_B/E pause (0.5 = legacy, never fires)
REGIME_SENTIMENT_THRESHOLD = float(os.getenv("REGIME_SENTIMENT_THRESHOLD", "0.5"))
DERIVED_BEAR_PAUSE_ENABLED = os.getenv("DERIVED_BEAR_PAUSE_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
DERIVED_BEAR_SENTIMENT_THRESHOLD = float(
    os.getenv("DERIVED_BEAR_SENTIMENT_THRESHOLD", "0.10")
)

# --- Wisdom layer (web mood + price math -> RHYME; see backtester_wisdom.py) ---
# dynamic (default) | baseline — legacy modes map to dynamic with a warning
WISDOM_MODE = os.getenv("WISDOM_MODE", "dynamic").strip().lower()
AUTO_DYNAMIC_ENABLED = os.getenv("AUTO_DYNAMIC_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
SENTIMENT_GAP_THRESHOLD_AGGRESSIVE = float(
    os.getenv("SENTIMENT_GAP_THRESHOLD_AGGRESSIVE", "0.25")
)
SENTIMENT_GAP_THRESHOLD_NORMAL = float(
    os.getenv("SENTIMENT_GAP_THRESHOLD_NORMAL", "0.35")
)
SENTIMENT_GAP_THRESHOLD_DEFENSIVE = float(
    os.getenv("SENTIMENT_GAP_THRESHOLD_DEFENSIVE", "0.40")
)
DYNAMIC_SIZING_MULTIPLIER_MAX = float(os.getenv("DYNAMIC_SIZING_MULTIPLIER_MAX", "1.5"))
DYNAMIC_SIZING_MULTIPLIER_MIN = float(os.getenv("DYNAMIC_SIZING_MULTIPLIER_MIN", "0.5"))
DYNAMIC_HIGH_VOL_WEB_SCALE = float(os.getenv("DYNAMIC_HIGH_VOL_WEB_SCALE", "0.5"))
DYNAMIC_LOW_VOL_TREND_BOOST = float(os.getenv("DYNAMIC_LOW_VOL_TREND_BOOST", "1.1"))
DYNAMIC_SPY_TREND_STRONG_PCT = float(os.getenv("DYNAMIC_SPY_TREND_STRONG_PCT", "0.05"))
DYNAMIC_SPY_TREND_NEAR_PCT = float(os.getenv("DYNAMIC_SPY_TREND_NEAR_PCT", "0.02"))
DYNAMIC_SPY_TREND_BOOST_SCALE = float(os.getenv("DYNAMIC_SPY_TREND_BOOST_SCALE", "5.0"))
# Legacy gap gate for deprecated modes and journal shadows
WISDOM_GAP_THRESHOLD = float(os.getenv("WISDOM_GAP_THRESHOLD", "0.25"))

# --- Sentiment data layout (all mood archives under sentiment/) ---
SENTIMENT_DIR = os.getenv("SENTIMENT_DIR", "sentiment")
WEB_SENTIMENT_CACHE_FILE = os.path.join(SENTIMENT_DIR, "live", "web_sentiment_live.json")
WAYBACK_SENTIMENT_FILE = os.path.join(SENTIMENT_DIR, "archive", "wayback_sentiment.csv")
WEB_SENTIMENT_CACHE_HOURS = int(os.getenv("WEB_SENTIMENT_CACHE_HOURS", "24"))
# Felix & Friends / Goat Academy YouTube transcripts (optional macro overlay)
FELIX_SYNC_ENABLED = os.getenv("FELIX_SYNC_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
FELIX_SYNC_INTERVAL_HOURS = int(os.getenv("FELIX_SYNC_INTERVAL_HOURS", "24"))
FELIX_SYNC_MAX_VIDEOS = int(os.getenv("FELIX_SYNC_MAX_VIDEOS", "15"))
FELIX_SENTIMENT_ENABLED = os.getenv("FELIX_SENTIMENT_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
FELIX_SENTIMENT_BLEND_WEIGHT = float(os.getenv("FELIX_SENTIMENT_BLEND_WEIGHT", "0.25"))
FELIX_SENTIMENT_MAX_AGE_DAYS = int(os.getenv("FELIX_SENTIMENT_MAX_AGE_DAYS", "14"))
FELIX_YOUTUBE_CHANNEL_URL = os.getenv(
    "FELIX_YOUTUBE_CHANNEL_URL",
    "https://www.youtube.com/@FelixFriends/videos",
).strip()
FELIX_TRANSCRIPTS_DIR = os.path.join(
    SENTIMENT_DIR, "sources", "youtube", "felix_and_friends", "transcripts"
)
FELIX_MANIFEST_FILE = os.path.join(
    SENTIMENT_DIR, "sources", "youtube", "felix_and_friends", "manifest.jsonl"
)

# --- Social / creator sleeve (Felix + shared sources): paper book, optional live mirror ---
SOCIAL_SLEEVE_ENABLED = os.getenv("SOCIAL_SLEEVE_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Share of account equity for social macro tilt (paper account; mirror uses cap * MIRROR_PCT on live)
SOCIAL_SLEEVE_CAP_PCT = float(os.getenv("SOCIAL_SLEEVE_CAP_PCT", "0.10"))
SOCIAL_SLEEVE_PAPER = os.getenv("SOCIAL_SLEEVE_PAPER", "true").lower() in (
    "1",
    "true",
    "yes",
)
# Fraction of social sleeve also placed on live (0.15 = 15% of social cap on live; ~1.5% equity at 10% cap)
SOCIAL_MIRROR_TO_LIVE_PCT = float(os.getenv("SOCIAL_MIRROR_TO_LIVE_PCT", "0.0"))
SOCIAL_FELIX_WEIGHT = float(os.getenv("SOCIAL_FELIX_WEIGHT", "0.65"))
SOCIAL_HEADLINE_WEIGHT = float(os.getenv("SOCIAL_HEADLINE_WEIGHT", "0.35"))
# Score thresholds → GLD / XLE / SPY (Felix: gold + energy when macro bearish)
SOCIAL_BEAR_GLD_THRESHOLD = float(os.getenv("SOCIAL_BEAR_GLD_THRESHOLD", "-0.12"))
SOCIAL_BEAR_ENERGY_THRESHOLD = float(os.getenv("SOCIAL_BEAR_ENERGY_THRESHOLD", "-0.04"))
SOCIAL_BULL_SPY_THRESHOLD = float(os.getenv("SOCIAL_BULL_SPY_THRESHOLD", "0.08"))

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
# Optional: one-shot market buy SPCX on Alpaca when it becomes tradable (paper default on)
_auto_buy_env = os.getenv("SPACEX_IPO_AUTO_BUY")
if _auto_buy_env is None:
    SPACEX_IPO_AUTO_BUY = PAPER_TRADING
else:
    SPACEX_IPO_AUTO_BUY = _auto_buy_env.lower() in ("1", "true", "yes")
# Dollar notional cap (also capped at 25% of equity / 95% of cash in spacex_ipo_buy.py)
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
# Yield gate only: block SPY on hostile rates; no metal sleeve, stress cash, or 0.9 long scale
GAME_PLAN_YIELD_GATE_ONLY = os.getenv("GAME_PLAN_YIELD_GATE_ONLY", "true").lower() in (
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

# --- Risk & sizing (tuned at REFERENCE_EQUITY; scales down for small live accounts) ---
REFERENCE_EQUITY = float(os.getenv("REFERENCE_EQUITY", "100000"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.02"))
MAX_NOTIONAL_PER_ORDER = float(os.getenv("MAX_NOTIONAL_PER_ORDER", "10000"))
# Min order at REFERENCE_EQUITY; effective_min_notional() scales with live equity (floor $1 Alpaca).
MIN_NOTIONAL = float(os.getenv("MIN_NOTIONAL", "10"))
ALPACA_MIN_NOTIONAL = float(os.getenv("ALPACA_MIN_NOTIONAL", "1"))
ADAPTIVE_CHUNK_ENABLED = os.getenv("ADAPTIVE_CHUNK_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
COFIRE_BUDGET_ENABLED = os.getenv("COFIRE_BUDGET_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
ADAPTIVE_CHUNK_MAX_PCT = float(os.getenv("ADAPTIVE_CHUNK_MAX_PCT", "0.05"))
COFIRE_BUDGET_PCT = float(os.getenv("COFIRE_BUDGET_PCT", "0.06"))
STOP_LOSS_PCT = 0.05
CRYPTO_MIN_CORRELATION = 0.5
# Alpaca: US stocks/ETFs commission-free; crypto market orders charge taker fee per leg
ALPACA_CRYPTO_FEE_AWARE = os.getenv("ALPACA_CRYPTO_FEE_AWARE", "true").lower() in (
    "1",
    "true",
    "yes",
)
ALPACA_CRYPTO_TAKER_FEE_PCT = float(os.getenv("ALPACA_CRYPTO_TAKER_FEE_PCT", "0.0025"))

# --- Cost basis / buy-price awareness (Alpaca avg_entry_price) ---
COST_BASIS_AWARE_ENABLED = os.getenv("COST_BASIS_AWARE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
UNDERWATER_SIZING_SCALE = float(os.getenv("UNDERWATER_SIZING_SCALE", "0.75"))
# Block discretionary exits (e.g. SPY MA break) while position is below cost; stops still fire
DISCRETIONARY_SELL_BELOW_COST = os.getenv("DISCRETIONARY_SELL_BELOW_COST", "true").lower() in (
    "1",
    "true",
    "yes",
)

# --- Scheduled macro event guard (NFP, CPI, FOMC, PPI, GDP) ---
MACRO_EVENT_GUARD_ENABLED = os.getenv("MACRO_EVENT_GUARD_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
MACRO_EVENT_HOURS_BEFORE = int(os.getenv("MACRO_EVENT_HOURS_BEFORE", "18"))
MACRO_EVENT_SIZING_SCALE = float(os.getenv("MACRO_EVENT_SIZING_SCALE", "0.7"))

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


def effective_min_notional(equity: float | None = None) -> float:
    """Scale min order with account: $10 at REFERENCE_EQUITY, down to ALPACA_MIN_NOTIONAL."""
    if equity is None or equity <= 0:
        return MIN_NOTIONAL
    ref = REFERENCE_EQUITY if REFERENCE_EQUITY > 0 else 100_000.0
    scaled = MIN_NOTIONAL * (float(equity) / ref)
    return max(ALPACA_MIN_NOTIONAL, round(scaled, 2))


def effective_max_notional_per_order(equity: float | None = None) -> float:
    """Scale per-order cap with account; never above 25% of equity."""
    if equity is None or equity <= 0:
        return MAX_NOTIONAL_PER_ORDER
    ref = REFERENCE_EQUITY if REFERENCE_EQUITY > 0 else 100_000.0
    scaled = MAX_NOTIONAL_PER_ORDER * (float(equity) / ref)
    pct_cap = round(float(equity) * 0.25, 2)
    floor = effective_min_notional(equity)
    return max(floor, min(round(scaled, 2), pct_cap))


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


def metal_sleeve_enabled() -> bool:
    """Full game plan metal sleeve (disabled in yield-gate-only mode)."""
    return GAME_PLAN_ENABLED and not GAME_PLAN_YIELD_GATE_ONLY


def game_plan_active() -> bool:
    """Any game-plan mode (full or yield-gate-only)."""
    return GAME_PLAN_ENABLED or GAME_PLAN_YIELD_GATE_ONLY


def print_dynamic_wisdom_config() -> None:
    """Print dynamic WISDOM_MODE thresholds and sizing knobs (preflight / tuning)."""
    print("--- Dynamic wisdom config ---")
    print(f"  WISDOM_MODE:                    {WISDOM_MODE}")
    print(f"  AUTO_DYNAMIC_ENABLED:           {AUTO_DYNAMIC_ENABLED}")
    print(f"  SENTIMENT_GAP_THRESHOLD_AGGRESSIVE: {SENTIMENT_GAP_THRESHOLD_AGGRESSIVE}")
    print(f"  SENTIMENT_GAP_THRESHOLD_NORMAL:     {SENTIMENT_GAP_THRESHOLD_NORMAL}")
    print(f"  SENTIMENT_GAP_THRESHOLD_DEFENSIVE:  {SENTIMENT_GAP_THRESHOLD_DEFENSIVE}")
    print(
        f"  DYNAMIC_SIZING_MULTIPLIER:      {DYNAMIC_SIZING_MULTIPLIER_MIN} .. "
        f"{DYNAMIC_SIZING_MULTIPLIER_MAX}"
    )
    print(f"  DYNAMIC_HIGH_VOL_WEB_SCALE:     {DYNAMIC_HIGH_VOL_WEB_SCALE}")
    print(f"  DYNAMIC_LOW_VOL_TREND_BOOST:      {DYNAMIC_LOW_VOL_TREND_BOOST}")
    print(f"  DYNAMIC_SPY_TREND_STRONG_PCT:     {DYNAMIC_SPY_TREND_STRONG_PCT}")
    print(f"  DYNAMIC_SPY_TREND_NEAR_PCT:       {DYNAMIC_SPY_TREND_NEAR_PCT}")
    print(f"  DYNAMIC_SPY_TREND_BOOST_SCALE:    {DYNAMIC_SPY_TREND_BOOST_SCALE}")
    print(f"  WISDOM_GAP_THRESHOLD (legacy):    {WISDOM_GAP_THRESHOLD}")


def print_recommended_stack_flags() -> None:
    """Log active current_dynamic live stack flags (preflight / backtest startup)."""
    gp = "yield-gate-only" if (
        game_plan_active() and GAME_PLAN_YIELD_GATE_ONLY
    ) else ("full" if game_plan_active() else "off")
    print("--- current_dynamic live stack ---")
    print(f"  game_plan:              {gp}")
    print(f"  yield_gate:             {YIELD_GATE_ENABLED}")
    print(f"  nyse_overlap_filter:    {NYSE_OVERLAP_FILTER_ENABLED} (corr max {NYSE_SPY_CORR_MAX})")
    print(f"  nyse_beta_scaling:      {NYSE_BETA_SCALING_ENABLED}")
    print(f"  spy_exit_on_ma_break:   {SPY_EXIT_ON_MA_BREAK}")
    print(f"  adaptive_chunk:         {ADAPTIVE_CHUNK_ENABLED}")
    print(f"  cofire_budget:          {COFIRE_BUDGET_ENABLED}")
    print(
        f"  halt_resume_dd:         {HALT_RESUME_DRAWDOWN_PCT:.0%} | "
        f"liquidate_on_breach: {HALT_LIQUIDATE_ON_BREACH}"
    )
    print(f"  derived_bear_pause:     {DERIVED_BEAR_PAUSE_ENABLED}")
    alloc = fund_allocation_pct()
    if vti_core_enabled():
        print(
            f"  vti_core:             {alloc['vti_core']:.0%} {VTI_CORE_SYMBOL} passive | "
            f"active {active_fund_fraction():.0%}"
        )
    print(
        f"  sleeves: SPY {alloc['spy']:.0%} | crypto {alloc['crypto']:.0%} | "
        f"NYSE {alloc['nyse']:.0%} | metal {alloc['metal']:.0%} | cash {alloc['cash_buffer']:.0%}"
    )
    if SOCIAL_SLEEVE_ENABLED:
        print(
            f"  social_sleeve:      {SOCIAL_SLEEVE_CAP_PCT:.0%} paper "
            f"| live mirror {SOCIAL_MIRROR_TO_LIVE_PCT:.0%} of social cap"
        )


def set_paper_aggressive_context(active: bool) -> None:
    """Thread-local style flag: paper research runner / social paper book."""
    global _paper_aggressive_ctx
    _paper_aggressive_ctx = bool(active)


def paper_aggressive_context() -> bool:
    return PAPER_AGGRESSIVE_ENABLED and _paper_aggressive_ctx


def effective_crypto_vol_only() -> bool:
    if paper_aggressive_context():
        return PAPER_CRYPTO_VOL_ONLY
    return CRYPTO_VOL_ONLY


def effective_social_sleeve_cap_pct() -> float:
    if paper_aggressive_context():
        return PAPER_SOCIAL_SLEEVE_CAP_PCT
    return SOCIAL_SLEEVE_CAP_PCT


def effective_vti_rebalance_drift_pct() -> float:
    if paper_aggressive_context():
        return PAPER_VTI_REBALANCE_DRIFT_PCT
    return VTI_CORE_REBALANCE_DRIFT_PCT


def vti_core_allocation_pct() -> float:
    if not VTI_CORE_ENABLED:
        return 0.0
    pct = PAPER_VTI_CORE_PCT if paper_aggressive_context() else VTI_CORE_PCT
    return round(min(0.95, pct), 6) if pct > 0 else 0.0


def vti_core_enabled() -> bool:
    return vti_core_allocation_pct() > 0


def active_fund_fraction() -> float:
    """Share of equity for active sleeves (remainder after VTI core)."""
    if not vti_core_enabled():
        return 1.0
    return round(1.0 - vti_core_allocation_pct(), 6)


def social_live_reserve_pct() -> float:
    """Live equity reserved for social mirror (reduces main fund sleeves)."""
    if not SOCIAL_SLEEVE_ENABLED or SOCIAL_MIRROR_TO_LIVE_PCT <= 0:
        return 0.0
    return round(SOCIAL_SLEEVE_CAP_PCT * SOCIAL_MIRROR_TO_LIVE_PCT, 6)


def long_fund_scale() -> float:
    """Reserve headroom for metal sleeve and social live mirror."""
    scale = 1.0
    if metal_sleeve_enabled():
        scale = max(0.5, 1.0 - METAL_SLEEVE_CAP_PCT)
    reserve = social_live_reserve_pct()
    if reserve > 0:
        scale = round(scale * (1.0 - reserve), 6)
    return scale


def active_sleeve_scale() -> float:
    """Scale active SPY/crypto/NYSE caps (VTI core + metal/social reserves)."""
    af = active_fund_fraction()
    lf = long_fund_scale()
    long_sum = (
        SPY_SLEEVE_CAP_PCT + CRYPTO_SLEEVE_CAP_PCT + NYSE_SLEEVE_CAP_PCT
    )
    if long_sum <= 0:
        return 0.0
    base_scale = round(lf * af, 6)
    if not paper_aggressive_context():
        return base_scale
    # Boost deploys more of the active slice; never exceed active fund headroom.
    base_deploy = round(base_scale * long_sum, 6)
    max_active = base_scale
    target_deploy = round(
        min(max_active, base_deploy * PAPER_ACTIVE_SLEEVE_BOOST), 6
    )
    return round(target_deploy / long_sum, 6)


def apply_paper_wisdom_floor(wisdom: dict | None) -> dict | None:
    """On paper aggressive, do not shrink sizing below floor (profit-seeking)."""
    if not wisdom or not paper_aggressive_context():
        return wisdom
    mult = float(wisdom.get("sizing_multiplier", 1.0))
    if mult < PAPER_WISDOM_SIZING_FLOOR:
        wisdom = dict(wisdom)
        wisdom["sizing_multiplier"] = PAPER_WISDOM_SIZING_FLOOR
    return wisdom


def effective_sleeve_cap(base_pct: float) -> float:
    return round(base_pct * active_sleeve_scale(), 6)


def effective_cash_buffer_pct() -> float:
    """Cash headroom so VTI core + active sleeves + metal sum to 100% of equity."""
    metal = METAL_SLEEVE_CAP_PCT if metal_sleeve_enabled() else 0.0
    vti = vti_core_allocation_pct()
    long_caps = (
        SPY_SLEEVE_CAP_PCT + CRYPTO_SLEEVE_CAP_PCT + NYSE_SLEEVE_CAP_PCT
    ) * active_sleeve_scale()
    cash = round(1.0 - metal - vti - long_caps, 6)
    if cash < 0:
        raise ValueError(
            f"Fund over-allocated: vti {vti:.2%} + metal {metal:.2%} + "
            f"long sleeves {long_caps:.2%} > 100%; reduce VTI_CORE_PCT or sleeve caps"
        )
    return cash


def fund_allocation_pct() -> dict[str, float]:
    """Current sleeve + cash cap fractions (sum to 1.0)."""
    return {
        "vti_core": vti_core_allocation_pct(),
        "spy": effective_sleeve_cap(SPY_SLEEVE_CAP_PCT),
        "crypto": effective_sleeve_cap(CRYPTO_SLEEVE_CAP_PCT),
        "nyse": effective_sleeve_cap(NYSE_SLEEVE_CAP_PCT),
        "metal": METAL_SLEEVE_CAP_PCT if metal_sleeve_enabled() else 0.0,
        "cash_buffer": effective_cash_buffer_pct(),
    }


_alloc = fund_allocation_pct()
if abs(sum(_alloc.values()) - 1.0) > 1e-4:
    raise ValueError(f"Fund allocation must sum to 100%, got {_alloc}")
