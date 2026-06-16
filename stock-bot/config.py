"""Central configuration: credentials, universe, paths, and strategy constants."""

import json
import logging
import os
import warnings
from dotenv import load_dotenv, find_dotenv

_env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
if _env_override and os.path.isfile(_env_override):
    load_dotenv(_env_override, override=True)
else:
    load_dotenv(find_dotenv())

try:
    from modules.ssl_certs import configure_ssl_certificates

    configure_ssl_certificates()
except ImportError:
    pass

# --- Alpaca (canonical: APCA_*; legacy ALPACA_* supported via get_alpaca_credentials) ---
# Paper-only by default. Set ALLOW_LIVE_TRADING=yes in .env to override PAPER_TRADING=False.
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes")
ALLOW_LIVE_TRADING = os.getenv("ALLOW_LIVE_TRADING", "").lower() in ("1", "true", "yes")
ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_BASE_URL = "https://api.alpaca.markets"

# --- Universe (single source of truth) ---
UNIVERSE = [
    "BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "AVAX-USD", "LINK-USD",
    "DOT-USD", "MATIC-USD", "ATOM-USD", "UNI-USD", "LTC-USD", "BCH-USD",
    "APT-USD", "ARB-USD", "OP-USD", "NEAR-USD", "FIL-USD", "AAVE-USD",
    "INJ-USD", "DOGE-USD", "SHIB-USD", "RENDER-USD", "SUI-USD", "PEPE-USD",
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "AMZN", "TSLA", "META",
    "SPCX", "PLTR", "NFLX", "INTC", "MU", "SMCI", "COIN", "CRM", "SHOP",
    "VTI", "QQQ", "SPY", "IWM",
    "GLD", "SLV", "CPER", "URA", "PPLT", "DBB", "GDX",
    "XOM", "CVX", "LNG",
    "RTX", "LMT", "KTOS",
    "JPM", "BAC", "GS",
    "JNJ", "UNH", "PFE",
]

# --- Dynamic NYSE screener (scripts/analysis/universe_screener.py) ---
USE_DYNAMIC_UNIVERSE = os.getenv("USE_DYNAMIC_UNIVERSE", "false").lower() in (
    "1",
    "true",
    "yes",
)
SCREENER_UNIVERSE_PATH = os.getenv("SCREENER_UNIVERSE_PATH", "data/screener_universe.json")

# --- Paths ---
DB_PATH = "market_data.db"
LEDGER_PATH = "trading_history.jsonl"
TRADE_HISTORY_LOG = "trade_history.log"
RISK_EVENTS_LOG = "risk_events.log"
PAPER_JOURNAL_CSV = os.getenv("PAPER_JOURNAL_CSV", "paper_journal.csv")
HEARTBEAT_FILE = os.getenv("HEARTBEAT_FILE", "bot_heartbeat.json")

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
# --- Best Paper Bot (Profile B) — locked defaults for beat-mutual-funds goal ---
# Core ON: dynamic VTI | dynamic risk | stat arb | vol overlay | options | overlap | chunk | co-fire
# Core OFF: macro regime | social | risk parity | stat arb optimized | equity pairs | spy MA exit
# Opt-in: thinking engine (Ollama) via PAPER_THINKING_ENGINE_ENABLED=true
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
# Paper aggressive crypto v2: dual-entry sleeve (mean reversion + breakout); live stays on stat arb
PAPER_CRYPTO_V2_ENABLED = os.getenv("PAPER_CRYPTO_V2_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_CRYPTO_V2_SYMBOLS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "ADA-USD",
    "DOT-USD", "MATIC-USD", "ATOM-USD", "UNI-USD", "LTC-USD", "BCH-USD",
    "APT-USD", "ARB-USD", "OP-USD", "NEAR-USD", "FIL-USD", "AAVE-USD",
    "INJ-USD", "DOGE-USD", "SHIB-USD", "RENDER-USD", "SUI-USD", "PEPE-USD",
]
PAPER_VTI_REBALANCE_DRIFT_PCT = float(os.getenv("PAPER_VTI_REBALANCE_DRIFT_PCT", "0.01"))
PAPER_DYNAMIC_VTI_ENABLED = os.getenv("PAPER_DYNAMIC_VTI", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_DYNAMIC_RISK_ENABLED = os.getenv("PAPER_DYNAMIC_RISK_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
_paper_dyn_univ = os.getenv("PAPER_DYNAMIC_UNIVERSE_ENABLED") or os.getenv(
    "PAPER_DYNAMIC_UNIVERSE", "true"
)
PAPER_DYNAMIC_UNIVERSE_ENABLED = _paper_dyn_univ.lower() in ("1", "true", "yes")
PAPER_RISK_CALM_BULL_PCT = float(os.getenv("PAPER_RISK_CALM_BULL_PCT", "0.03"))
PAPER_RISK_MODERATE_PCT = float(os.getenv("PAPER_RISK_MODERATE_PCT", "0.022"))
PAPER_RISK_STRESS_PCT = float(os.getenv("PAPER_RISK_STRESS_PCT", "0.01"))
DYNAMIC_VTI_PAPER_FLOOR = float(os.getenv("DYNAMIC_VTI_PAPER_FLOOR", "0.40"))
# Advanced sleeve features — paper aggressive only (live Profile A stays off)
PAPER_NYSE_OVERLAP_FILTER_ENABLED = os.getenv(
    "PAPER_NYSE_OVERLAP_FILTER_ENABLED", "true"
).lower() in ("1", "true", "yes")
PAPER_ADAPTIVE_CHUNK_ENABLED = os.getenv("PAPER_ADAPTIVE_CHUNK_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_COFIRE_BUDGET_ENABLED = os.getenv("PAPER_COFIRE_BUDGET_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_SPY_EXIT_ON_MA_BREAK = os.getenv("PAPER_SPY_EXIT_ON_MA_BREAK", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Market-neutral pair trades — paper aggressive only; live conservative stays off
PAPER_MARKET_NEUTRAL_PAIRS = os.getenv("PAPER_MARKET_NEUTRAL_PAIRS", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_EQUITY_PAIRS = os.getenv("PAPER_EQUITY_PAIRS", "false").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_STAT_ARB_ENABLED = os.getenv("PAPER_STAT_ARB_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_PAIR_MIN_CORRELATION = float(os.getenv("PAPER_PAIR_MIN_CORRELATION", "0.75"))
PAPER_PAIR_Z_THRESHOLD = float(os.getenv("PAPER_PAIR_Z_THRESHOLD", "2.5"))
PAPER_PAIR_Z_EXIT = float(os.getenv("PAPER_PAIR_Z_EXIT", "0.5"))
STAT_ARB_LOOKBACK = int(os.getenv("STAT_ARB_LOOKBACK", "60"))
# Optimized stat arb — paper/backtest only (default off; live unchanged)
PAPER_STAT_ARB_OPTIMIZED = os.getenv("PAPER_STAT_ARB_OPTIMIZED", "false").lower() in (
    "1",
    "true",
    "yes",
)
STAT_ARB_MAX_HOLD_BARS = int(os.getenv("STAT_ARB_MAX_HOLD_BARS", "35"))
STAT_ARB_PROFIT_Z_DELTA = float(os.getenv("STAT_ARB_PROFIT_Z_DELTA", "1.5"))
STAT_ARB_CORR_HALF_LIFE = float(os.getenv("STAT_ARB_CORR_HALF_LIFE", "25"))
# Local LLM thinking engine (Ollama) — paper only; run scripts/setup_ollama.py first
PAPER_THINKING_ENGINE_ENABLED = os.getenv("PAPER_THINKING_ENGINE_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:8b")
OLLAMA_FALLBACK_MODELS = os.getenv(
    "OLLAMA_FALLBACK_MODELS", "llama3.2:3b,deepseek-r1:1.5b"
)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_TIMEOUT_SEC = int(os.getenv("OLLAMA_TIMEOUT_SEC", "1200"))
THINKING_CACHE_HOURS = int(os.getenv("THINKING_CACHE_HOURS", "24"))
THINKING_ENGINE_STATE_FILE = os.getenv("THINKING_ENGINE_STATE_FILE", "thinking_engine_state.json")
THINKING_ENGINE_OUTPUT_FILE = os.getenv("THINKING_ENGINE_OUTPUT_FILE", "thinking_engine_last.json")
THINKING_APPROVAL_FILE = os.getenv("THINKING_APPROVAL_FILE", "thinking_engine_approval.json")
# Production hard cap — applies to paper and live (never exceed ±6% per sleeve)
THINKING_PRODUCTION_MAX_SLEEVE_DELTA = float(
    os.getenv("THINKING_PRODUCTION_MAX_SLEEVE_DELTA", "0.06")
)
THINKING_MAX_SLEEVE_DELTA = min(
    float(os.getenv("THINKING_MAX_SLEEVE_DELTA", "0.06")),
    THINKING_PRODUCTION_MAX_SLEEVE_DELTA,
)
# Tighter cap when simulating thinking on live small-account profile
LIVE_THINKING_MAX_SLEEVE_DELTA = min(
    float(os.getenv("LIVE_THINKING_MAX_SLEEVE_DELTA", "0.06")),
    THINKING_PRODUCTION_MAX_SLEEVE_DELTA,
)
# Daily loss circuit breaker — blocks new entries + thinking tilts after intraday drawdown
DAILY_LOSS_CIRCUIT_BREAKER_ENABLED = os.getenv(
    "DAILY_LOSS_CIRCUIT_BREAKER_ENABLED", "true"
).lower() in ("1", "true", "yes")
TRADING_SAFETY_STATE_FILE = os.getenv("TRADING_SAFETY_STATE_FILE", "trading_safety_state.json")
THINKING_DAILY_LOSS_LIMIT_LIVE = float(os.getenv("THINKING_DAILY_LOSS_LIMIT_LIVE", "0.02"))
THINKING_DAILY_LOSS_LIMIT_PAPER = float(os.getenv("THINKING_DAILY_LOSS_LIMIT_PAPER", "0.04"))
# Live: require explicit approval file before applying tilts (see scripts/approve_thinking_tilt.py)
THINKING_MANUAL_APPROVAL_LIVE = os.getenv("THINKING_MANUAL_APPROVAL_LIVE", "true").lower() in (
    "1",
    "true",
    "yes",
)
# Disabled until live confidence calibration improves (see thinking engine accuracy analysis)
THINKING_CONFIDENCE_AMPLIFY_ENABLED = os.getenv(
    "THINKING_CONFIDENCE_AMPLIFY_ENABLED", "true"
).lower() in ("1", "true", "yes")
# Risk parity / All Weather + pod drawdown limits — paper aggressive only
PAPER_RISK_PARITY_ENABLED = os.getenv("PAPER_RISK_PARITY_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
RISK_PARITY_MAX_CAP_SHIFT = float(os.getenv("RISK_PARITY_MAX_CAP_SHIFT", "0.12"))
POD_RISK_STATE_FILE = os.getenv("POD_RISK_STATE_FILE", "pod_risk_state.json")
POD_REDUCE_SCALE = float(os.getenv("POD_REDUCE_SCALE", "0.50"))
POD_PAUSE_SCALE = float(os.getenv("POD_PAUSE_SCALE", "0.0"))
POD_MAX_DRAWDOWN_PCT = {
    "spy": float(os.getenv("POD_MAX_DD_SPY", "0.08")),
    "crypto": float(os.getenv("POD_MAX_DD_CRYPTO", "0.12")),
    "nyse": float(os.getenv("POD_MAX_DD_NYSE", "0.10")),
    "stat_arb": float(os.getenv("POD_MAX_DD_STAT_ARB", "0.06")),
    "vol": float(os.getenv("POD_MAX_DD_VOL", "0.10")),
    "options": float(os.getenv("POD_MAX_DD_OPTIONS", "0.08")),
}
# Options income sleeve (covered calls) — paper aggressive only; live stays off
OPTIONS_SLEEVE_ENABLED = os.getenv("OPTIONS_SLEEVE_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_OPTIONS_SLEEVE_ENABLED = os.getenv("PAPER_OPTIONS_SLEEVE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
OPTIONS_SLEEVE_CAP_PCT = float(os.getenv("OPTIONS_SLEEVE_CAP_PCT", "0.12"))
OPTIONS_OTM_PCT = float(os.getenv("OPTIONS_OTM_PCT", "0.075"))
OPTIONS_OTM_PCT_MIN = 0.05
OPTIONS_OTM_PCT_MAX = 0.10
OPTIONS_VIX_CALM_MAX = float(os.getenv("OPTIONS_VIX_CALM_MAX", "22"))
OPTIONS_MONTHLY_BARS = int(os.getenv("OPTIONS_MONTHLY_BARS", "21"))
OPTIONS_VTI_ALLOC_PCT = float(os.getenv("OPTIONS_VTI_ALLOC_PCT", "0.70"))
# Volatility trading overlay — paper aggressive only; live stays off
PAPER_VOL_TRADING_ENABLED = os.getenv("PAPER_VOL_TRADING_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
VOL_SLEEVE_CAP_PCT = float(os.getenv("VOL_SLEEVE_CAP_PCT", "0.08"))
VOL_SLEEVE_CAP_MIN_PCT = float(os.getenv("VOL_SLEEVE_CAP_MIN_PCT", "0.05"))
VOL_SLEEVE_CAP_MAX_PCT = float(os.getenv("VOL_SLEEVE_CAP_MAX_PCT", "0.10"))
VOL_VIX_HIGH_THRESHOLD = float(os.getenv("VOL_VIX_HIGH_THRESHOLD", "20"))
VOL_VIX_CALM_THRESHOLD = float(os.getenv("VOL_VIX_CALM_THRESHOLD", "15"))
VOL_VIX_SPIKE_PCT = float(os.getenv("VOL_VIX_SPIKE_PCT", "0.10"))
VOL_CONTANGO_VIX_MAX = float(os.getenv("VOL_CONTANGO_VIX_MAX", "22"))
VOL_MONTHLY_BARS = int(os.getenv("VOL_MONTHLY_BARS", "21"))
VOL_VXX_BETA = float(os.getenv("VOL_VXX_BETA", "0.85"))
VOL_CONTANGO_DECAY_DAILY = float(os.getenv("VOL_CONTANGO_DECAY_DAILY", "0.00025"))

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
# Minimum seconds halted before auto-resume (stops rapid halt/resume flicker).
HALT_MIN_SECONDS = int(os.getenv("HALT_MIN_SECONDS", "300"))
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


def ensure_sentiment_dirs() -> None:
    """Create sentiment/live and sentiment/archive (safe after crash or fresh install)."""
    from pathlib import Path

    base = Path(SENTIMENT_DIR)
    (base / "live").mkdir(parents=True, exist_ok=True)
    (base / "archive").mkdir(parents=True, exist_ok=True)


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
# Andrei Jikh — personal finance / macro (blended into creator sentiment with Felix)
ANDREI_JIKH_YOUTUBE_ENABLED = os.getenv("ANDREI_JIKH_YOUTUBE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
ANDREI_JIKH_YOUTUBE_CHANNEL_URL = os.getenv(
    "ANDREI_JIKH_YOUTUBE_CHANNEL_URL",
    "https://www.youtube.com/channel/UCGy7SkBjcIAgTiwkXEtPnYg/videos",
).strip()
SOCIAL_FELIX_CHANNEL_WEIGHT = float(os.getenv("SOCIAL_FELIX_CHANNEL_WEIGHT", "0.50"))
SOCIAL_ANDREI_JIKH_WEIGHT = float(os.getenv("SOCIAL_ANDREI_JIKH_WEIGHT", "0.50"))


def youtube_channel_specs() -> list[dict]:
    """Registered YouTube channels for creator/social sentiment sync."""
    channels: list[dict] = []
    if FELIX_SYNC_ENABLED or FELIX_SENTIMENT_ENABLED:
        channels.append(
            {
                "id": "felix_and_friends",
                "name": "Felix & Friends",
                "url": FELIX_YOUTUBE_CHANNEL_URL,
                "weight": SOCIAL_FELIX_CHANNEL_WEIGHT,
            }
        )
    if ANDREI_JIKH_YOUTUBE_ENABLED:
        channels.append(
            {
                "id": "andrei_jikh",
                "name": "Andrei Jikh",
                "url": ANDREI_JIKH_YOUTUBE_CHANNEL_URL,
                "weight": SOCIAL_ANDREI_JIKH_WEIGHT,
            }
        )
    return channels


def youtube_channel_dir(channel_id: str) -> str:
    return os.path.join(SENTIMENT_DIR, "sources", "youtube", channel_id)


def youtube_manifest_file(channel_id: str) -> str:
    return os.path.join(youtube_channel_dir(channel_id), "manifest.jsonl")


def youtube_transcripts_dir(channel_id: str) -> str:
    return os.path.join(youtube_channel_dir(channel_id), "transcripts")


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
SOCIAL_MACRO_BEAR_OVERRIDE_SCORE = float(
    os.getenv("SOCIAL_MACRO_BEAR_OVERRIDE_SCORE", "-0.4")
)
SOCIAL_MACRO_BULL_OVERRIDE_SCORE = float(
    os.getenv("SOCIAL_MACRO_BULL_OVERRIDE_SCORE", "0.5")
)
PAPER_SOCIAL_SLEEVE_ENABLED = os.getenv("PAPER_SOCIAL_SLEEVE_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
# --- Macro Regime Adaptor (replaces Felix/Social on paper aggressive) ---
MACRO_REGIME_ADAPTOR_ENABLED = os.getenv("MACRO_REGIME_ADAPTOR_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_MACRO_REGIME_ADAPTOR_ENABLED = os.getenv(
    "PAPER_MACRO_REGIME_ADAPTOR_ENABLED", "false"
).lower() in ("1", "true", "yes")
MACRO_OIL_SURGE_PCT = float(os.getenv("MACRO_OIL_SURGE_PCT", "0.08"))
MACRO_GLD_SURGE_PCT = float(os.getenv("MACRO_GLD_SURGE_PCT", "0.04"))
MACRO_VIX_SAFE_HAVEN_MIN = float(os.getenv("MACRO_VIX_SAFE_HAVEN_MIN", "20"))
MACRO_VIX_SPIKE_PCT = float(os.getenv("MACRO_VIX_SPIKE_PCT", "0.10"))
MACRO_ENERGY_CAP_PCT = float(os.getenv("MACRO_ENERGY_CAP_PCT", "0.10"))
MACRO_SAFE_HAVEN_CAP_PCT = float(os.getenv("MACRO_SAFE_HAVEN_CAP_PCT", "0.10"))
MACRO_ENERGY_SLEEVE_BOOST = float(os.getenv("MACRO_ENERGY_SLEEVE_BOOST", "0.08"))
MACRO_SLEEVE_ADJUST_MAX_PCT = float(os.getenv("MACRO_SLEEVE_ADJUST_MAX_PCT", "0.15"))
SOCIAL_MACRO_OVERRIDES_ENABLED = os.getenv("SOCIAL_MACRO_OVERRIDES_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_SOCIAL_MACRO_BOOST_ENABLED = os.getenv(
    "PAPER_SOCIAL_MACRO_BOOST_ENABLED", "false"
).lower() in ("1", "true", "yes")

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
WISDOM_JOURNAL_FILE = os.getenv("WISDOM_JOURNAL_FILE", "wisdom_journal.csv")
WISDOM_SCORECARD_FILE = os.getenv("WISDOM_SCORECARD_FILE", "wisdom_scorecard.json")
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
# Small live account safety (< threshold equity): conservative sizing + higher VTI core
SMALL_ACCOUNT_EQUITY_THRESHOLD = float(
    os.getenv("SMALL_ACCOUNT_EQUITY_THRESHOLD", "500")
)
SMALL_ACCOUNT_RISK_PER_TRADE = float(os.getenv("SMALL_ACCOUNT_RISK_PER_TRADE", "0.01"))
SMALL_ACCOUNT_MAX_NOTIONAL = float(os.getenv("SMALL_ACCOUNT_MAX_NOTIONAL", "10"))
SMALL_ACCOUNT_VTI_CORE_PCT = float(os.getenv("SMALL_ACCOUNT_VTI_CORE_PCT", "0.90"))
SMALL_ACCOUNT_BACKTEST_EQUITY = float(
    os.getenv("SMALL_ACCOUNT_BACKTEST_EQUITY", "100")
)

_account_equity: float | None = None
_small_account_mode = False
_backtest_small_account_ctx = False
_backtest_paper_sleeves_ctx = False
_live_thinking_sim_ctx = False
_dynamic_risk_ctx: dict = {
    "vol_score": 0.02,
    "regime": "",
    "macro_stress": False,
}
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
DYNAMIC_SLEEVE_CAPS_ENABLED = os.getenv("DYNAMIC_SLEEVE_CAPS_ENABLED", "false").lower() in (
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


def reload_from_env(env_file: str | None = None) -> None:
    """Reload credentials flags after portal switches per-user .env."""
    global PAPER_TRADING, ALLOW_LIVE_TRADING
    if env_file and os.path.isfile(env_file):
        load_dotenv(env_file, override=True)
    else:
        load_dotenv(find_dotenv(), override=True)
    PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes")
    ALLOW_LIVE_TRADING = os.getenv("ALLOW_LIVE_TRADING", "").lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        from modules.alpaca_client import reset_trading_client_cache

        reset_trading_client_cache()
    except ImportError:
        pass


def _strip_env(val: str | None) -> str:
    return (val or "").strip()


def get_alpaca_base_url(*, paper: bool | None = None) -> str:
    """Return Alpaca REST base URL. Paper mode always uses the paper endpoint."""
    use_paper = PAPER_TRADING if paper is None else bool(paper)
    if use_paper:
        return ALPACA_PAPER_BASE_URL
    override = _strip_env(os.getenv("APCA_API_BASE_URL"))
    if override:
        return override.rstrip("/")
    return ALPACA_LIVE_BASE_URL


def get_alpaca_credentials():
    """Return (api_key, secret_key). Prefers APCA_*; falls back to legacy ALPACA_*."""
    key = _strip_env(os.getenv("APCA_API_KEY_ID")) or _strip_env(os.getenv("ALPACA_API_KEY"))
    secret = _strip_env(os.getenv("APCA_API_SECRET_KEY")) or _strip_env(
        os.getenv("ALPACA_SECRET_KEY")
    )
    if not key or not secret:
        raise ValueError(
            "Alpaca credentials missing. Add to your .env file (never commit .env):\n"
            "  APCA_API_KEY_ID=your_key_id\n"
            "  APCA_API_SECRET_KEY=your_secret_key\n"
            "For paper trading also set PAPER_TRADING=true"
        )
    return key, secret


def validate_alpaca_config(*, require_credentials: bool = True) -> None:
    """Validate Alpaca env at startup; raise ValueError with setup instructions."""
    if require_credentials:
        get_alpaca_credentials()
    use_paper = PAPER_TRADING
    if not use_paper and not ALLOW_LIVE_TRADING:
        raise ValueError(
            "Live trading blocked. Set PAPER_TRADING=true for paper keys, "
            "or set ALLOW_LIVE_TRADING=yes to acknowledge live risk."
        )
    base_url = get_alpaca_base_url(paper=use_paper)
    logging.getLogger(__name__).info(
        "Alpaca config OK: paper=%s base_url=%s",
        use_paper,
        base_url,
    )


def get_spy_alpaca_credentials():
    """SPY bot keys: SPY_APCA_* if set, else main APCA_* (same paper account)."""
    key = (
        _strip_env(os.getenv("SPY_APCA_API_KEY_ID"))
        or _strip_env(os.getenv("APCA_API_KEY_ID"))
        or _strip_env(os.getenv("ALPACA_API_KEY"))
    )
    secret = (
        _strip_env(os.getenv("SPY_APCA_API_SECRET_KEY"))
        or _strip_env(os.getenv("APCA_API_SECRET_KEY"))
        or _strip_env(os.getenv("ALPACA_SECRET_KEY"))
    )
    if not key or not secret:
        raise ValueError(
            "Alpaca credentials missing. Set SPY_APCA_* or APCA_* in .env "
            "(see README; never commit .env)."
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


def set_backtest_small_account_context(active: bool) -> None:
    """Lock live small-account sizing rules for backtester.py --small-account."""
    global _backtest_small_account_ctx, _small_account_mode, _account_equity
    _backtest_small_account_ctx = bool(active)
    if active:
        _small_account_mode = True
        _account_equity = SMALL_ACCOUNT_BACKTEST_EQUITY
    elif _account_equity is not None:
        _small_account_mode = (
            float(_account_equity) < SMALL_ACCOUNT_EQUITY_THRESHOLD
        )
    else:
        _small_account_mode = False


def backtest_small_account_context() -> bool:
    return _backtest_small_account_ctx


def set_backtest_paper_sleeves_context(active: bool) -> None:
    """True while run_backtest() runs paper aggressive sleeves (not live)."""
    global _backtest_paper_sleeves_ctx
    _backtest_paper_sleeves_ctx = bool(active)


def backtest_paper_sleeves_context() -> bool:
    return _backtest_paper_sleeves_ctx


def set_live_thinking_sim_context(active: bool) -> None:
    """True while backtest simulates thinking tilts on live small-account profile."""
    global _live_thinking_sim_ctx
    _live_thinking_sim_ctx = bool(active)


def live_thinking_sim_context() -> bool:
    return _live_thinking_sim_ctx


def is_small_account(equity: float | None = None) -> bool:
    """True when equity is below SMALL_ACCOUNT_EQUITY_THRESHOLD ($500 default)."""
    if _backtest_small_account_ctx:
        return True
    if equity is not None:
        return float(equity) < SMALL_ACCOUNT_EQUITY_THRESHOLD
    return _small_account_mode


def set_dynamic_risk_context(
    *,
    vol_score: float | None = None,
    regime: str | None = None,
    macro_stress: bool | None = None,
) -> None:
    """Update per-cycle inputs for paper dynamic risk (run_all / backtester)."""
    global _dynamic_risk_ctx
    if vol_score is not None:
        _dynamic_risk_ctx["vol_score"] = float(vol_score)
    if regime is not None:
        _dynamic_risk_ctx["regime"] = str(regime)
    if macro_stress is not None:
        _dynamic_risk_ctx["macro_stress"] = bool(macro_stress)


def configure_account_profile(equity: float) -> dict:
    """Apply runtime sizing/VTI profile from live Alpaca equity (call each cycle)."""
    global _account_equity, _small_account_mode
    _account_equity = float(equity)
    _small_account_mode = _account_equity < SMALL_ACCOUNT_EQUITY_THRESHOLD
    return {
        "equity": _account_equity,
        "small_account": _small_account_mode,
        "risk_per_trade": effective_risk_per_trade(_account_equity),
        "max_notional_per_order": effective_max_notional_per_order(),
        "vti_core_pct": vti_core_allocation_pct(equity=_account_equity),
    }


def effective_risk_per_trade(
    equity: float | None = None,
    *,
    vol_score: float | None = None,
    regime: str | None = None,
    macro_stress: bool | None = None,
) -> float:
    eq = float(equity) if equity is not None else (_account_equity or REFERENCE_EQUITY)
    if is_small_account(eq):
        return SMALL_ACCOUNT_RISK_PER_TRADE
    vs = vol_score if vol_score is not None else _dynamic_risk_ctx.get("vol_score", 0.02)
    reg = regime if regime is not None else _dynamic_risk_ctx.get("regime", "")
    stress = (
        macro_stress
        if macro_stress is not None
        else _dynamic_risk_ctx.get("macro_stress", False)
    )
    if (
        PAPER_TRADING
        and PAPER_DYNAMIC_RISK_ENABLED
        and (paper_aggressive_context() or paper_chase_mode_enabled())
    ):
        from modules.fund_config import get_dynamic_risk_per_trade

        return get_dynamic_risk_per_trade(eq, float(vs), reg, bool(stress))
    return RISK_PER_TRADE


def effective_min_notional(equity: float | None = None) -> float:
    """Scale min order with account: $10 at REFERENCE_EQUITY, down to ALPACA_MIN_NOTIONAL."""
    if equity is None or equity <= 0:
        return MIN_NOTIONAL
    ref = REFERENCE_EQUITY if REFERENCE_EQUITY > 0 else 100_000.0
    scaled = MIN_NOTIONAL * (float(equity) / ref)
    return max(ALPACA_MIN_NOTIONAL, round(scaled, 2))


def effective_max_notional_per_order(equity: float | None = None) -> float:
    """Scale per-order cap with account; never above 25% of equity."""
    eq = float(equity) if equity is not None else _account_equity
    if is_small_account(eq):
        floor = effective_min_notional(eq)
        pct_cap = round(eq * 0.25, 2) if eq and eq > 0 else SMALL_ACCOUNT_MAX_NOTIONAL
        return max(floor, min(SMALL_ACCOUNT_MAX_NOTIONAL, pct_cap))
    if eq is None or eq <= 0:
        return MAX_NOTIONAL_PER_ORDER
    ref = REFERENCE_EQUITY if REFERENCE_EQUITY > 0 else 100_000.0
    scaled = MAX_NOTIONAL_PER_ORDER * (eq / ref)
    pct_cap = round(eq * 0.25, 2)
    floor = effective_min_notional(eq)
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


_screener_fallback_warned = False


def load_screener_universe_tickers() -> list[str] | None:
    """Load ranked tickers from screener JSON; None if missing or invalid."""
    path = SCREENER_UNIVERSE_PATH
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        tickers = payload.get("tickers") or []
        return [str(t).strip().upper() for t in tickers if str(t).strip()]
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        return None


def _nyse_eligible_symbol(symbol: str) -> bool:
    return (
        not is_crypto(symbol)
        and symbol != SPY_BOT_SYMBOL
        and symbol != VTI_CORE_SYMBOL
        and not is_metal_symbol(symbol)
    )


def nyse_momentum_universe(data_columns) -> list[str]:
    """Equity sleeve candidates: dynamic screener (NYSE+NASDAQ) or static columns."""
    global _screener_fallback_warned
    from modules.dynamic_universe import equity_sleeve_universe

    use_dynamic = USE_DYNAMIC_UNIVERSE or effective_paper_dynamic_universe()
    if not use_dynamic:
        return [c for c in data_columns if _nyse_eligible_symbol(c)]

    dynamic = equity_sleeve_universe(data_columns)
    static = [c for c in data_columns if _nyse_eligible_symbol(c)]
    if dynamic == static and use_dynamic and load_screener_universe_tickers():
        if not _screener_fallback_warned:
            warnings.warn(
                f"Dynamic universe enabled but no screener tickers in price data — "
                f"fetch daily history for {SCREENER_UNIVERSE_PATH} tickers",
                stacklevel=2,
            )
            _screener_fallback_warned = True
    return dynamic


def backtest_fetch_tickers() -> list[str]:
    """Tickers to load for daily backtests (static UNIVERSE + screener when dynamic)."""
    tickers = list(UNIVERSE)
    if USE_DYNAMIC_UNIVERSE or effective_paper_dynamic_universe():
        extra = load_screener_universe_tickers() or []
        tickers = sorted(set(tickers) | set(extra))
    return tickers


def dynamic_equity_position_scale(symbol: str) -> float:
    """Paper-only position scale for IPO / high-vol names from screener metadata."""
    try:
        from modules.dynamic_universe import position_scale_for_symbol

        return position_scale_for_symbol(symbol)
    except ImportError:
        return 1.0


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


def _game_plan_label() -> str:
    if game_plan_active() and GAME_PLAN_YIELD_GATE_ONLY:
        return "yield-gate-only"
    if game_plan_active():
        return "full"
    return "off"


# === Best Paper Config Support ===
_best_paper_applied = False


def use_best_paper_config() -> bool:
    """True if BEST_PAPER_CONFIG env or best_paper_config mode should be applied."""
    return os.getenv("BEST_PAPER_CONFIG", "").lower() in ("1", "true", "yes")


def apply_best_paper_config_if_enabled() -> None:
    """Apply best paper config if BEST_PAPER_CONFIG env is set.
    
    Call early in run_all.py main() to ensure best paper flags override config.py defaults.
    """
    global _best_paper_applied
    if _best_paper_applied or not use_best_paper_config():
        return
    
    logger = logging.getLogger(__name__)
    try:
        from config.best_paper_config import apply_best_paper_config, validate_best_paper_config
        
        # Check for deprecated features
        _, warnings = validate_best_paper_config()
        if warnings:
            for w in warnings:
                logger.warning("best_paper_config: %s", w)
        
        # Apply best paper defaults (disables deprecated features)
        apply_best_paper_config()
        _best_paper_applied = True
        
        logger.info("best_paper_config applied: simplified paper bot stack enabled")
    except ImportError as e:
        logger.warning("Failed to import best_paper_config: %s", e, exc_info=True)


def print_live_stack_flags() -> None:
    """Log Profile A: current_dynamic live stack (preflight / live run_all / backtest default)."""
    gp = _game_plan_label()
    print("--- current_dynamic live stack (Profile A) ---")
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
    print(f"  wisdom_mode:            {WISDOM_MODE}")
    alloc = fund_allocation_pct()
    if is_small_account():
        print(
            f"  small_account:        ON (<${SMALL_ACCOUNT_EQUITY_THRESHOLD:,.0f}) | "
            f"risk {effective_risk_per_trade():.0%} | "
            f"max order ${effective_max_notional_per_order():,.0f}"
        )
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


def get_best_paper_bot_stack() -> dict[str, bool]:
    """Locked Best Paper Bot v2 flags (paper aggressive only)."""
    try:
        from config.best_paper_config import get_full_stack

        stack = get_full_stack()
        stack["stat_arb"] = effective_stat_arb_enabled() or PAPER_STAT_ARB_ENABLED
        stack["thinking_engine"] = PAPER_THINKING_ENGINE_ENABLED
        stack["vol_overlay"] = PAPER_VOL_TRADING_ENABLED
        stack["options_income"] = PAPER_OPTIONS_SLEEVE_ENABLED
        stack["dynamic_vti"] = PAPER_DYNAMIC_VTI_ENABLED
        stack["dynamic_risk"] = PAPER_DYNAMIC_RISK_ENABLED
        stack["nyse_overlap"] = PAPER_NYSE_OVERLAP_FILTER_ENABLED
        stack["adaptive_chunk"] = PAPER_ADAPTIVE_CHUNK_ENABLED
        stack["cofire_budget"] = PAPER_COFIRE_BUDGET_ENABLED
        stack["dynamic_universe"] = PAPER_DYNAMIC_UNIVERSE_ENABLED
        return stack
    except ImportError:
        return {
            "dynamic_vti": PAPER_DYNAMIC_VTI_ENABLED,
            "dynamic_risk": PAPER_DYNAMIC_RISK_ENABLED,
            "stat_arb": PAPER_STAT_ARB_ENABLED,
            "vol_overlay": PAPER_VOL_TRADING_ENABLED,
            "options_income": PAPER_OPTIONS_SLEEVE_ENABLED,
            "thinking_engine": PAPER_THINKING_ENGINE_ENABLED,
            "nyse_overlap": PAPER_NYSE_OVERLAP_FILTER_ENABLED,
            "adaptive_chunk": PAPER_ADAPTIVE_CHUNK_ENABLED,
            "cofire_budget": PAPER_COFIRE_BUDGET_ENABLED,
            "dynamic_universe": PAPER_DYNAMIC_UNIVERSE_ENABLED,
            "macro_regime": False,
            "risk_parity": False,
            "stat_arb_optimized": False,
            "social_sleeve": False,
            "equity_pairs": False,
            "spy_exit": False,
        }


BEST_PAPER_LOCKED_OFF = (
    "macro_regime",
    "risk_parity",
    "stat_arb_optimized",
    "social_sleeve",
    "equity_pairs",
    "spy_exit",
)


def format_best_paper_status_lines() -> tuple[str, str]:
    """Compact ON / locked-OFF lines for status.py and docs."""
    was_ctx = paper_aggressive_context()
    was_bt = backtest_paper_sleeves_context()
    set_paper_aggressive_context(True)
    set_backtest_paper_sleeves_context(True)
    enforce_best_paper_stack()
    try:
        stack = get_best_paper_bot_stack()
        chase = paper_chase_mode_enabled()
        on_parts = [
            f"chase={'on' if chase else 'off'}",
            f"dyn_vti={'on' if stack['dynamic_vti'] else 'off'}",
            f"dyn_risk={'on' if stack['dynamic_risk'] else 'off'}",
            f"stat_arb={'on' if stack['stat_arb'] else 'off'}",
            f"vol={'on' if stack['vol_overlay'] else 'off'}",
            f"options={'on' if stack['options_income'] else 'off'}",
            f"thinking={'on' if stack['thinking_engine'] else 'off'}",
            f"overlap={'on' if stack['nyse_overlap'] else 'off'}",
            f"chunk={'on' if stack['adaptive_chunk'] else 'off'}",
            f"cofire={'on' if stack['cofire_budget'] else 'off'}",
            f"dyn_univ={'on' if stack.get('dynamic_universe') else 'off'}",
        ]
        off_parts = [
            "macro",
            "risk_parity",
            "stat_arb_opt",
            "social",
            "equity_pairs",
            "spy_exit",
        ]
        return " | ".join(on_parts), " | ".join(off_parts)
    finally:
        set_paper_aggressive_context(was_ctx)
        set_backtest_paper_sleeves_context(was_bt)


def print_paper_research_stack_flags() -> None:
    """Log Best Paper Bot / Profile B stack (paper_aggressive)."""
    gp = _game_plan_label()
    was_ctx = paper_aggressive_context()
    set_paper_aggressive_context(True)
    try:
        alloc = fund_allocation_pct()
        stack = get_best_paper_bot_stack()
        print("--- Best Paper Bot (paper_aggressive / Profile B) ---")
        if paper_chase_mode_enabled():
            print("  paper_chase_mode:       ON (PAPER_CHASE_MODE)")
        print(f"  game_plan:              {gp}")
        print(f"  yield_gate:             {YIELD_GATE_ENABLED}")
        on_line, off_line = format_best_paper_status_lines()
        print(f"  stack ON:               {on_line}")
        print(f"  locked OFF:             {off_line}")
        print(
            f"  nyse_beta_scaling:      {NYSE_BETA_SCALING_ENABLED} "
            f"(paper chase tuning)"
        )
        print(
            f"  options_sleeve:       "
            f"{'on' if effective_options_sleeve_enabled() else 'off'} "
            f"(cap {OPTIONS_SLEEVE_CAP_PCT:.0%}, calm VIX<{OPTIONS_VIX_CALM_MAX:.0f})"
        )
        print(
            f"  dynamic_vti:          "
            f"{'on' if stack['dynamic_vti'] else 'off'} "
            f"({DYNAMIC_VTI_PAPER_FLOOR:.0%}-75% by vol/stress)"
        )
        print(
            f"  dynamic_risk:         "
            f"{'on' if stack['dynamic_risk'] else 'off'} "
            f"({PAPER_RISK_CALM_BULL_PCT:.1%} calm bull / "
            f"{PAPER_RISK_MODERATE_PCT:.1%} moderate / {PAPER_RISK_STRESS_PCT:.1%} stress)"
        )
        print(
            f"  stat_arb:             "
            f"{'on' if stack['stat_arb'] and effective_stat_arb_enabled() else 'off'} "
            f"(corr>{PAPER_PAIR_MIN_CORRELATION}, Z>={PAPER_PAIR_Z_THRESHOLD})"
        )
        print(
            f"  vol_overlay:          "
            f"{'on' if stack['vol_overlay'] and effective_vol_trading_enabled() else 'off'} "
            f"(cap {VOL_SLEEVE_CAP_PCT:.0%})"
        )
        print(
            f"  thinking_engine:      "
            f"{'on' if effective_thinking_engine_enabled() else 'off (opt-in: PAPER_THINKING_ENGINE_ENABLED=true)'}"
        )
        safety = get_thinking_safety_summary()
        print(
            f"  thinking_safety:        ±{safety['max_sleeve_delta_pp']:.0f}% cap | "
            f"daily_loss paper {safety['daily_loss_limit_paper_pct']:.0f}% / "
            f"live {safety['daily_loss_limit_live_pct']:.0f}% | "
            f"live_approval={'on' if safety['manual_approval_live'] else 'off'}"
        )
        print(
            f"  halt_resume_dd:         {HALT_RESUME_DRAWDOWN_PCT:.0%} | "
            f"liquidate_on_breach: {HALT_LIQUIDATE_ON_BREACH}"
        )
        print(f"  derived_bear_pause:     {DERIVED_BEAR_PAUSE_ENABLED}")
        print(f"  wisdom_mode:            {WISDOM_MODE}")
        print(
            f"  vti_core:             {alloc['vti_core']:.0%} {VTI_CORE_SYMBOL} | "
            f"active boost {PAPER_ACTIVE_SLEEVE_BOOST:.2f}x"
        )
        print(f"  crypto_vol_only:      {effective_crypto_vol_only()}")
        print(f"  wisdom_sizing_floor:  {PAPER_WISDOM_SIZING_FLOOR}")
        print(
            f"  sleeves: SPY {alloc['spy']:.0%} | crypto {alloc['crypto']:.0%} | "
            f"NYSE {alloc['nyse']:.0%} | metal {alloc['metal']:.0%} | cash {alloc['cash_buffer']:.0%}"
        )
        social_cap = effective_social_sleeve_cap_pct()
        if SOCIAL_SLEEVE_ENABLED:
            print(
                f"  social_sleeve:      {social_cap:.0%} paper "
                f"| live mirror {SOCIAL_MIRROR_TO_LIVE_PCT:.0%} of social cap"
            )
        elif paper_chase_mode_enabled():
            print("  social_sleeve:      off (locked by Best Paper Bot stack)")
    finally:
        set_paper_aggressive_context(was_ctx)


def print_recommended_stack_flags(*, profile: str | None = None) -> None:
    """Print active deployment profile flags (preflight / run_all / backtest startup).

    profile: ``live`` | ``paper`` | None (auto: paper when PAPER_CHASE_MODE or paper ctx).
    """
    if profile == "live":
        print_live_stack_flags()
    elif profile == "paper" or (
        profile is None
        and (paper_chase_mode_enabled() or paper_aggressive_context())
    ):
        print_paper_research_stack_flags()
    else:
        print_live_stack_flags()


def paper_chase_mode_enabled() -> bool:
    """Paper Sharpe-chase loop (run_paper_bot / portal paper users)."""
    return os.getenv("PAPER_CHASE_MODE", "").lower() in ("1", "true", "yes")


def apply_paper_chase_runtime_tuning() -> list[str]:
    """
    Turn on under-used stack layers for paper Sharpe chase only.
    Live ~$100 profile is unchanged. Bot CPU/WiFi stay light (mostly sleeping).
    Set PAPER_CHASE_EXTRA=false to skip.
    """
    global SOCIAL_SLEEVE_ENABLED
    global FELIX_SYNC_ENABLED, FELIX_SENTIMENT_ENABLED, NYSE_BETA_SCALING_ENABLED
    global REFRESH_INTERVAL, CRYPTO_ONLY_CYCLE_INTERVAL_SEC, CYCLE_INTERVAL_SEC

    if not paper_chase_mode_enabled():
        return []
    if os.getenv("PAPER_CHASE_EXTRA", "true").lower() not in ("1", "true", "yes"):
        return []

    turned_on: list[str] = []

    def _enable(name: str, flag: str, value: bool = True) -> None:
        nonlocal turned_on
        globals()[flag] = value
        turned_on.append(name)

    # adaptive_chunk / cofire / overlap / spy_exit: paper via effective_*() helpers
    # Felix sync on for sentiment; social + macro adaptor off by default (opt-in via .env)
    _enable("felix_sync", "FELIX_SYNC_ENABLED")
    _enable("felix_sentiment", "FELIX_SENTIMENT_ENABLED")
    _enable("nyse_beta_scaling", "NYSE_BETA_SCALING_ENABLED")

    # Slightly faster cycles/data — still far below PC or WiFi limits.
    if int(os.getenv("PAPER_CHASE_CYCLE_SEC", "45")) < CYCLE_INTERVAL_SEC:
        CYCLE_INTERVAL_SEC = int(os.getenv("PAPER_CHASE_CYCLE_SEC", "45"))
        turned_on.append(f"cycle_{CYCLE_INTERVAL_SEC}s")
    if int(os.getenv("PAPER_CHASE_CRYPTO_CYCLE_SEC", "180")) < CRYPTO_ONLY_CYCLE_INTERVAL_SEC:
        CRYPTO_ONLY_CYCLE_INTERVAL_SEC = int(os.getenv("PAPER_CHASE_CRYPTO_CYCLE_SEC", "180"))
        turned_on.append(f"crypto_cycle_{CRYPTO_ONLY_CYCLE_INTERVAL_SEC}s")
    refresh = int(os.getenv("PAPER_CHASE_REFRESH_SEC", "600"))
    if refresh < REFRESH_INTERVAL:
        REFRESH_INTERVAL = refresh
        turned_on.append(f"refresh_{REFRESH_INTERVAL}s")

    return turned_on


def paper_only_sleeves_active() -> bool:
    """Paper aggressive / chase sleeves — never on live money accounts."""
    active_stack = bool(
        paper_aggressive_context()
        or (PAPER_AGGRESSIVE_ENABLED and paper_chase_mode_enabled())
    )
    if not active_stack:
        return False
    if PAPER_TRADING:
        return True
    return _backtest_paper_sleeves_ctx


def enforce_best_paper_stack() -> None:
    """Disable weak/redundant paper sleeves (Profile B locked stack v2)."""
    global PAPER_RISK_PARITY_ENABLED
    global PAPER_MACRO_REGIME_ADAPTOR_ENABLED
    global PAPER_SOCIAL_SLEEVE_ENABLED
    global PAPER_EQUITY_PAIRS
    global PAPER_SPY_EXIT_ON_MA_BREAK
    global PAPER_STAT_ARB_OPTIMIZED
    global PAPER_SOCIAL_MACRO_BOOST_ENABLED
    global PAPER_CRYPTO_V2_ENABLED
    PAPER_RISK_PARITY_ENABLED = False
    PAPER_MACRO_REGIME_ADAPTOR_ENABLED = False
    PAPER_SOCIAL_SLEEVE_ENABLED = False
    PAPER_EQUITY_PAIRS = False
    PAPER_SPY_EXIT_ON_MA_BREAK = False
    PAPER_STAT_ARB_OPTIMIZED = False
    PAPER_SOCIAL_MACRO_BOOST_ENABLED = False
    PAPER_CRYPTO_V2_ENABLED = False


def init_paper_chase_if_enabled() -> list[str]:
    """Enable aggressive paper profile when PAPER_CHASE_MODE is set (paper only)."""
    extras: list[str] = []
    if (
        PAPER_TRADING
        and paper_chase_mode_enabled()
        and PAPER_AGGRESSIVE_ENABLED
    ):
        try:
            from config.best_paper_config import apply_best_paper_config

            apply_best_paper_config()
        except ImportError:
            enforce_best_paper_stack()
        set_paper_aggressive_context(True)
        extras = apply_paper_chase_runtime_tuning()
        try:
            from modules.dynamic_universe import maybe_refresh_screener_universe

            uni = maybe_refresh_screener_universe()
            if uni.get("action") == "refreshed":
                extras.append(f"universe_refresh_{uni.get('count', 0)}")
        except Exception:
            pass
        extras.insert(0, "best_paper_stack_v2_locked")
    return extras


def set_paper_aggressive_context(active: bool) -> None:
    """Thread-local style flag: paper research runner / social paper book."""
    global _paper_aggressive_ctx
    _paper_aggressive_ctx = bool(active)


def paper_aggressive_context() -> bool:
    return PAPER_AGGRESSIVE_ENABLED and _paper_aggressive_ctx


def get_paper_feature_flags() -> dict[str, bool]:
    """Paper aggressive sleeve toggles; live returns {}."""
    if not paper_only_sleeves_active():
        return {}
    return {
        "dynamic_vti": PAPER_DYNAMIC_VTI_ENABLED,
        "dynamic_universe": PAPER_DYNAMIC_UNIVERSE_ENABLED,
        "dynamic_risk": PAPER_DYNAMIC_RISK_ENABLED,
        "stat_arb": effective_stat_arb_enabled(),
        "vol_overlay": effective_vol_trading_enabled(),
        "options": effective_options_sleeve_enabled(),
        "macro_regime": effective_macro_regime_adaptor_enabled(),
        "thinking_engine": effective_thinking_engine_enabled(),
        "risk_parity": effective_risk_parity_enabled(),
        "stat_arb_optimized": False,
        "social": effective_social_sleeve_enabled(),
        "nyse_overlap": PAPER_NYSE_OVERLAP_FILTER_ENABLED,
        "adaptive_chunk": PAPER_ADAPTIVE_CHUNK_ENABLED,
        "cofire_budget": PAPER_COFIRE_BUDGET_ENABLED,
        "spy_exit_on_ma_break": PAPER_SPY_EXIT_ON_MA_BREAK,
        "market_neutral_pairs": effective_market_neutral_pairs_enabled(),
        "equity_pairs": effective_equity_pairs_enabled(),
    }


def snapshot_paper_sleeve_flags() -> dict[str, bool]:
    """Save PAPER_* sleeve env flags for backtest restore."""
    return {
        "nyse_overlap": PAPER_NYSE_OVERLAP_FILTER_ENABLED,
        "adaptive_chunk": PAPER_ADAPTIVE_CHUNK_ENABLED,
        "cofire_budget": PAPER_COFIRE_BUDGET_ENABLED,
        "spy_exit_on_ma_break": PAPER_SPY_EXIT_ON_MA_BREAK,
    }


def apply_paper_sleeve_flags(flags: dict[str, bool]) -> None:
    """Set PAPER_* sleeve flags (used by backtester A/B)."""
    global PAPER_NYSE_OVERLAP_FILTER_ENABLED
    global PAPER_ADAPTIVE_CHUNK_ENABLED
    global PAPER_COFIRE_BUDGET_ENABLED
    global PAPER_SPY_EXIT_ON_MA_BREAK
    if "nyse_overlap" in flags:
        PAPER_NYSE_OVERLAP_FILTER_ENABLED = bool(flags["nyse_overlap"])
    if "adaptive_chunk" in flags:
        PAPER_ADAPTIVE_CHUNK_ENABLED = bool(flags["adaptive_chunk"])
    if "cofire_budget" in flags:
        PAPER_COFIRE_BUDGET_ENABLED = bool(flags["cofire_budget"])
    if "spy_exit_on_ma_break" in flags:
        PAPER_SPY_EXIT_ON_MA_BREAK = bool(flags["spy_exit_on_ma_break"])


def effective_nyse_overlap_filter_enabled() -> bool:
    flags = get_paper_feature_flags()
    if flags:
        return flags["nyse_overlap"]
    return NYSE_OVERLAP_FILTER_ENABLED


def effective_adaptive_chunk_enabled() -> bool:
    flags = get_paper_feature_flags()
    if flags:
        return flags["adaptive_chunk"]
    return ADAPTIVE_CHUNK_ENABLED


def effective_cofire_budget_enabled() -> bool:
    flags = get_paper_feature_flags()
    if flags:
        return flags["cofire_budget"]
    return COFIRE_BUDGET_ENABLED


def effective_stat_arb_enabled() -> bool:
    """Cointegration + z-score stat arb — paper aggressive only."""
    if effective_crypto_v2_enabled():
        return False
    return bool(paper_only_sleeves_active() and PAPER_STAT_ARB_ENABLED)


def effective_crypto_v2_enabled() -> bool:
    """Dual-entry crypto sleeve (MR + breakout) — paper aggressive only, default off."""
    if not paper_only_sleeves_active() or not PAPER_CRYPTO_V2_ENABLED:
        return False
    return True


def effective_paper_dynamic_universe() -> bool:
    """Weekly NYSE/screener refresh — paper aggressive only."""
    return bool(paper_only_sleeves_active() and PAPER_DYNAMIC_UNIVERSE_ENABLED)


def paper_crypto_v2_symbols() -> list[str]:
    return list(PAPER_CRYPTO_V2_SYMBOLS)


def effective_stat_arb_optimized() -> bool:
    """Enhanced stat arb (Kalman/decay/dynamic Z) — disabled; see stat_arb_optimized.py."""
    return False


def effective_thinking_engine_enabled() -> bool:
    """Local Ollama PM reasoning — paper aggressive or live-thinking sim only."""
    if live_thinking_sim_context() and PAPER_THINKING_ENGINE_ENABLED:
        return backtest_small_account_context()
    if not paper_only_sleeves_active() or not PAPER_THINKING_ENGINE_ENABLED:
        return False
    if PAPER_TRADING:
        return True
    return bool(backtest_paper_sleeves_context())


def effective_thinking_max_sleeve_delta(*, live_sim: bool = False) -> float:
    """Hard production cap ±6% per sleeve (ignores confidence amplification)."""
    if live_sim:
        return min(LIVE_THINKING_MAX_SLEEVE_DELTA, THINKING_PRODUCTION_MAX_SLEEVE_DELTA)
    return min(THINKING_MAX_SLEEVE_DELTA, THINKING_PRODUCTION_MAX_SLEEVE_DELTA)


def thinking_daily_loss_limit_pct() -> float:
    """Max allowed intraday loss before thinking tilts are blocked."""
    if PAPER_TRADING or paper_only_sleeves_active():
        return THINKING_DAILY_LOSS_LIMIT_PAPER
    return THINKING_DAILY_LOSS_LIMIT_LIVE


def thinking_manual_approval_required() -> bool:
    """Live money requires approval file before tilt apply."""
    if PAPER_TRADING or paper_only_sleeves_active():
        return False
    return THINKING_MANUAL_APPROVAL_LIVE


def get_thinking_safety_summary() -> dict[str, str | float | bool]:
    """Compact safety flags for status.py / docs."""
    return {
        "max_sleeve_delta_pp": round(effective_thinking_max_sleeve_delta() * 100, 1),
        "daily_loss_limit_live_pct": round(THINKING_DAILY_LOSS_LIMIT_LIVE * 100, 1),
        "daily_loss_limit_paper_pct": round(THINKING_DAILY_LOSS_LIMIT_PAPER * 100, 1),
        "daily_loss_breaker_enabled": DAILY_LOSS_CIRCUIT_BREAKER_ENABLED,
        "manual_approval_live": thinking_manual_approval_required(),
        "confidence_amplify": THINKING_CONFIDENCE_AMPLIFY_ENABLED,
        "paper_thinking_enabled": PAPER_THINKING_ENGINE_ENABLED,
    }


def get_production_safety_summary() -> dict[str, str | float | bool]:
    """Live vs paper production safety (entries + thinking)."""
    s = get_thinking_safety_summary()
    s["live_tilt_cap_pp"] = round(
        min(LIVE_THINKING_MAX_SLEEVE_DELTA, THINKING_PRODUCTION_MAX_SLEEVE_DELTA) * 100, 1
    )
    s["production_tilt_cap_pp"] = round(THINKING_PRODUCTION_MAX_SLEEVE_DELTA * 100, 1)
    return s


def effective_risk_parity_enabled() -> bool:
    """All Weather risk parity + pod drawdown limits — paper aggressive only."""
    if not paper_only_sleeves_active() or not PAPER_RISK_PARITY_ENABLED:
        return False
    if PAPER_TRADING:
        return True
    return bool(backtest_paper_sleeves_context())


def effective_market_neutral_pairs_enabled() -> bool:
    """Legacy correlation pairs — superseded by stat arb when enabled."""
    if effective_stat_arb_enabled():
        return False
    return bool(paper_only_sleeves_active() and PAPER_MARKET_NEUTRAL_PAIRS)


def effective_equity_pairs_enabled() -> bool:
    """Long strong / short weak NYSE pairs — opt-in on paper aggressive."""
    if not paper_only_sleeves_active():
        return False
    if effective_stat_arb_enabled():
        return PAPER_EQUITY_PAIRS
    return effective_market_neutral_pairs_enabled() and PAPER_EQUITY_PAIRS


def effective_pair_min_correlation() -> float:
    if effective_stat_arb_enabled() or effective_market_neutral_pairs_enabled():
        return PAPER_PAIR_MIN_CORRELATION
    return CRYPTO_MIN_CORRELATION


def effective_pair_z_threshold(default: float = 2.0) -> float:
    if effective_stat_arb_enabled() or effective_market_neutral_pairs_enabled():
        return PAPER_PAIR_Z_THRESHOLD
    return default


def effective_pair_z_exit() -> float:
    return PAPER_PAIR_Z_EXIT


def effective_spy_exit_on_ma_break() -> bool:
    flags = get_paper_feature_flags()
    if flags:
        return flags["spy_exit_on_ma_break"]
    return SPY_EXIT_ON_MA_BREAK


def effective_crypto_vol_only() -> bool:
    if paper_only_sleeves_active():
        return PAPER_CRYPTO_VOL_ONLY
    return CRYPTO_VOL_ONLY


def effective_social_sleeve_enabled() -> bool:
    """Felix/Social sleeve — off by default; opt-in via env (legacy)."""
    if paper_only_sleeves_active():
        return PAPER_SOCIAL_SLEEVE_ENABLED
    return SOCIAL_SLEEVE_ENABLED


def effective_macro_regime_adaptor_enabled() -> bool:
    """Macro Regime Adaptor — off by default; opt-in via PAPER_MACRO_REGIME_ADAPTOR_ENABLED."""
    if paper_only_sleeves_active():
        return PAPER_MACRO_REGIME_ADAPTOR_ENABLED
    return MACRO_REGIME_ADAPTOR_ENABLED


def effective_options_sleeve_enabled() -> bool:
    """Covered-call income sleeve — paper aggressive only."""
    if not paper_only_sleeves_active():
        return False
    return PAPER_OPTIONS_SLEEVE_ENABLED


def effective_vol_trading_enabled() -> bool:
    """VIX/VXX vol overlay — paper aggressive only."""
    if not paper_only_sleeves_active():
        return False
    return PAPER_VOL_TRADING_ENABLED


def effective_social_sleeve_cap_pct() -> float:
    if paper_only_sleeves_active():
        return PAPER_SOCIAL_SLEEVE_CAP_PCT
    return SOCIAL_SLEEVE_CAP_PCT


def effective_vti_rebalance_drift_pct() -> float:
    if paper_only_sleeves_active():
        return PAPER_VTI_REBALANCE_DRIFT_PCT
    return VTI_CORE_REBALANCE_DRIFT_PCT


def get_vti_core_pct(
    equity: float,
    vol_score: float | None = None,
    macro_stress: bool = False,
    volatility: str | None = None,
) -> float:
    """Dynamic VTI target (paper aggressive) or static live/small-account pct."""
    from modules.fund_config import get_vti_core_pct as _get_vti_core_pct

    return _get_vti_core_pct(
        equity,
        vol_score=vol_score,
        macro_stress=macro_stress,
        volatility=volatility,
    )


def vti_core_allocation_pct(
    equity: float | None = None,
    vol_score: float | None = None,
    macro_stress: bool = False,
    volatility: str | None = None,
) -> float:
    if not VTI_CORE_ENABLED:
        return 0.0
    if paper_only_sleeves_active():
        eq = equity if equity is not None and equity > 0 else (_account_equity or 0.0)
        if eq > 0:
            pct = get_vti_core_pct(
                eq,
                vol_score=vol_score,
                macro_stress=macro_stress,
                volatility=volatility,
            )
        elif PAPER_DYNAMIC_VTI_ENABLED:
            pct = float(os.getenv("DYNAMIC_VTI_DEFAULT_PCT", "0.65"))
        else:
            pct = PAPER_VTI_CORE_PCT
    elif is_small_account(equity):
        pct = SMALL_ACCOUNT_VTI_CORE_PCT
    else:
        pct = VTI_CORE_PCT
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
