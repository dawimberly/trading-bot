"""Central configuration: credentials, universe, paths, and strategy constants."""

import importlib.util
import json
import logging
import os
import shutil
import sys
import warnings
from pathlib import Path

from dotenv import load_dotenv, find_dotenv

_CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _CONFIG_DIR


def _parse_env_bool(key: str, *, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


def _env_bool_first(*keys: str, default: str = "false") -> bool:
    """First set env var wins; otherwise use default string."""
    for key in keys:
        raw = os.getenv(key)
        if raw is not None:
            return raw.strip().lower() in ("1", "true", "yes", "on")
    return default.strip().lower() in ("1", "true", "yes", "on")


def _append_loaded_env(loaded: list[str], path: Path) -> None:
    try:
        resolved = str(path.resolve())
    except OSError:
        return
    if resolved not in loaded:
        loaded.append(resolved)


def _load_project_dotenv() -> None:
    """Load .env: stock-bot/.env is authoritative; dist/.env fills missing keys only."""
    loaded: list[str] = []
    env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if env_override and os.path.isfile(env_override):
        load_dotenv(env_override, override=True)
        _append_loaded_env(loaded, Path(env_override))
        stock_env = _CONFIG_DIR / ".env"
        if stock_env.is_file():
            load_dotenv(stock_env, override=False)
            _append_loaded_env(loaded, stock_env)
        _normalize_alpaca_env_keys()
        if loaded:
            os.environ["PYTHONTRADING_LOADED_ENV"] = ";".join(loaded)
        return

    try:
        from modules.runtime_paths import resolve_data_root, resolve_runtime_root

        root = resolve_runtime_root()
        data_root = resolve_data_root(root)
    except ImportError:
        root = _CONFIG_DIR
        data_root = _CONFIG_DIR

    stock_env = _CONFIG_DIR / ".env"
    dist_env = data_root / ".env"
    repo_env = _CONFIG_DIR.parent / ".env"

    if getattr(sys, "frozen", False):
        if stock_env.is_file():
            load_dotenv(stock_env, override=True)
            _append_loaded_env(loaded, stock_env)
            if dist_env.is_file() and dist_env.resolve() != stock_env.resolve():
                load_dotenv(dist_env, override=False)
                _append_loaded_env(loaded, dist_env)
        elif dist_env.is_file():
            load_dotenv(dist_env, override=True)
            _append_loaded_env(loaded, dist_env)
    else:
        if repo_env.is_file() and repo_env.resolve() != stock_env.resolve():
            load_dotenv(repo_env, override=True)
            _append_loaded_env(loaded, repo_env)
        if stock_env.is_file():
            load_dotenv(stock_env, override=True)
            _append_loaded_env(loaded, stock_env)
        if dist_env.is_file():
            same_as_stock = stock_env.is_file() and dist_env.resolve() == stock_env.resolve()
            if not same_as_stock:
                load_dotenv(dist_env, override=not stock_env.is_file())
                _append_loaded_env(loaded, dist_env)

    found = find_dotenv(usecwd=True)
    if found:
        found_path = Path(found)
        try:
            found_resolved = found_path.resolve()
        except OSError:
            found_resolved = None
        already = {Path(p).resolve() for p in loaded}
        if found_resolved and found_resolved not in already and found_path.is_file():
            load_dotenv(found_path, override=True)
            _append_loaded_env(loaded, found_path)

    _normalize_alpaca_env_keys()
    if loaded:
        os.environ["PYTHONTRADING_LOADED_ENV"] = ";".join(loaded)


def _dotenv_file_value(path: Path, key: str) -> str | None:
    """Read a single KEY=value from a .env file without applying it to os.environ."""
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            if k.strip() == key:
                return _strip_env(v)
    except OSError:
        return None
    return None


_ALPACA_ENV_KEYS = (
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "PAPER_APCA_API_KEY_ID",
    "PAPER_APCA_API_SECRET_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "SPY_APCA_API_KEY_ID",
    "SPY_APCA_API_SECRET_KEY",
    "PAPER_TRADING",
    "ALLOW_LIVE_TRADING",
)


def _strip_env(val: str | None) -> str:
    """Strip whitespace, optional quotes, and UTF-8 BOM artifacts from env values."""
    if val is None:
        return ""
    s = str(val).strip().lstrip("\ufeff")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1].strip()
    return s


def _normalize_alpaca_env_keys() -> None:
    """Normalize known Alpaca keys after dotenv load (quotes, trailing spaces)."""
    for key in _ALPACA_ENV_KEYS:
        raw = os.getenv(key)
        if raw is None:
            continue
        cleaned = _strip_env(raw)
        if cleaned != raw:
            os.environ[key] = cleaned


def _sync_trading_mode_flags(*, skip_root_override: bool = False) -> None:
    """Apply PAPER_TRADING / ALLOW_LIVE_TRADING from os.environ after dotenv load."""
    global PAPER_TRADING, ALLOW_LIVE_TRADING
    ALLOW_LIVE_TRADING = _parse_env_bool("ALLOW_LIVE_TRADING", default="false")
    paper_raw = (os.getenv("PAPER_TRADING", "true") or "true").strip().lower()
    PAPER_TRADING = paper_raw in ("1", "true", "yes", "on")
    if ALLOW_LIVE_TRADING and paper_raw in ("0", "false", "no", "off"):
        PAPER_TRADING = False
    if skip_root_override:
        return
    # Root .env live intent wins over stock-bot/.env paper override (dual-file setups).
    root_env = _CONFIG_DIR.parent / ".env"
    root_allow = (_dotenv_file_value(root_env, "ALLOW_LIVE_TRADING") or "").lower()
    root_paper = (_dotenv_file_value(root_env, "PAPER_TRADING") or "").lower()
    if root_allow in ("1", "true", "yes", "on") and root_paper in ("0", "false", "no", "off"):
        PAPER_TRADING = False
        ALLOW_LIVE_TRADING = True
        os.environ["PAPER_TRADING"] = "false"
        os.environ["ALLOW_LIVE_TRADING"] = "yes"


_load_project_dotenv()

try:
    from modules.ssl_certs import configure_ssl_certificates

    configure_ssl_certificates()
except ImportError:
    pass

# --- Alpaca (canonical: APCA_*; legacy ALPACA_* supported via get_alpaca_credentials) ---
# Paper-only by default. Set ALLOW_LIVE_TRADING=yes with PAPER_TRADING=false for live.
PAPER_TRADING = True
ALLOW_LIVE_TRADING = False
_sync_trading_mode_flags()
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

# Sleeve ETFs / metals kept out of NYSE momentum stock picks (still in UNIVERSE for data).
_SLEEVE_ETFS = frozenset(
    {
        "SPY",
        "QQQ",
        "IWM",
        "VTI",
        "GLD",
        "SLV",
        "CPER",
        "URA",
        "PPLT",
        "DBB",
        "GDX",
    }
)


def _screener_tickers_from_payload(payload) -> list[str]:
    """Parse screener JSON: ``tickers`` list, score_table rows, or bare list."""
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        raw = payload.get("tickers")
        if not raw:
            table = payload.get("score_table") or []
            raw = [
                row.get("ticker") or row.get("symbol")
                for row in table
                if isinstance(row, dict)
            ]
        raw = raw or []
    else:
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("ticker") or item.get("symbol") or ""
        sym = str(item).strip().upper()
        if sym:
            out.append(sym)
    return out


def get_nyse_universe_fixed() -> list[str]:
    """Fixed NYSE momentum candidates only (no screener). Safe for live Profile A."""
    return [t for t in UNIVERSE if "-USD" not in t and t not in _SLEEVE_ETFS]


def get_nyse_universe() -> list[str]:
    """Fixed equity candidates, or fixed ∪ screener when USE_DYNAMIC_UNIVERSE."""
    base = get_nyse_universe_fixed()
    if not USE_DYNAMIC_UNIVERSE:
        return base
    try:
        with open(SCREENER_UNIVERSE_PATH, encoding="utf-8") as f:
            screener = _screener_tickers_from_payload(json.load(f))
        seen = set(base)
        combined = list(base)
        for t in screener:
            if t not in seen:
                combined.append(t)
                seen.add(t)
        return combined
    except Exception:
        return base  # safe fallback to fixed list


# --- Paths ---
DB_PATH = "market_data.db"
LEDGER_PATH = "trading_history.jsonl"
TRADE_HISTORY_LOG = "trade_history.log"
RISK_EVENTS_LOG = "risk_events.log"
PAPER_JOURNAL_CSV = os.getenv("PAPER_JOURNAL_CSV", "paper_journal.csv")
HEARTBEAT_FILE = os.getenv("HEARTBEAT_FILE", "bot_heartbeat.json")
AUTO_LAUNCH_DASHBOARD = _parse_env_bool("AUTO_LAUNCH_DASHBOARD", default="false")


def resolve_db_path() -> Path:
    """Return the best market_data.db path with these priorities:
    1. Project root (stock-bot/market_data.db)
    2. Largest non-empty .db file in cwd or dist/
    3. Copy from project root to dist/ if running from packaged dir
    """
    env_raw = (os.getenv("MARKET_DATA_DB") or "").strip()
    if env_raw:
        p = Path(env_raw)
        return p.resolve() if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    from modules.runtime_paths import resolve_data_root, resolve_runtime_root

    project_root = resolve_runtime_root()
    data_root = resolve_data_root(project_root)
    name = DB_PATH
    min_size = 1_000_000  # < ~1MB = empty stub

    candidates = [
        project_root / name,
        Path.cwd() / name,
        data_root / name,
        project_root / "dist" / name,
    ]
    best: Path | None = None
    best_size = 0
    seen: set[Path] = set()
    for p in candidates:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        if not rp.is_file():
            continue
        size = rp.stat().st_size
        if size > best_size:
            best = rp
            best_size = size

    real_db = (project_root / name).resolve()
    if best is None or best_size < min_size:
        if real_db.is_file() and real_db.stat().st_size >= min_size:
            if data_root != project_root:
                target = (data_root / name).resolve()
            else:
                target = (Path.cwd() / name).resolve()
            if target != real_db:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(real_db, target)
                return target
            return real_db
    return best or real_db


def ensure_market_db() -> Path:
    """Ensure a usable market_data.db exists (delegates to resolve_db_path)."""
    return resolve_db_path()


def resolve_heartbeat_file(paper: bool | None = None) -> Path:
    """Absolute heartbeat path under the runtime data root."""
    from modules.runtime_paths import resolve_data_root

    data_root = resolve_data_root()
    env_raw = (os.getenv("HEARTBEAT_FILE") or "").strip()
    if env_raw:
        env_path = Path(env_raw)
        if env_path.is_absolute():
            return env_path.resolve()

    hb_env = (env_raw or HEARTBEAT_FILE or "bot_heartbeat.json").lower()
    if paper_chase_mode_enabled() or "paper_chase" in hb_env:
        return (data_root / "paper_chase_heartbeat.json").resolve()

    is_paper = bool(PAPER_TRADING) if paper is None else bool(paper)
    if is_paper:
        return (data_root / "bot_heartbeat.json").resolve()
    return (data_root / "live_bot_heartbeat.json").resolve()


def configure_heartbeat_path() -> str:
    """Pin HEARTBEAT_FILE to an absolute path for this runtime mode."""
    global HEARTBEAT_FILE
    path = resolve_heartbeat_file()
    HEARTBEAT_FILE = str(path)
    os.environ["HEARTBEAT_FILE"] = HEARTBEAT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return HEARTBEAT_FILE


def ensure_heartbeat_path_writable() -> str:
    """Resolve relative heartbeat paths and ensure parent dir exists."""
    hb = HEARTBEAT_FILE
    if not Path(hb).is_absolute():
        hb = configure_heartbeat_path()
    else:
        Path(hb).parent.mkdir(parents=True, exist_ok=True)
    return hb


# --- Strategy ---
TICKER = "VTI"
ASSET_TYPE = "STOCK"
MA_WINDOW = 45

# --- Live stack: current_dynamic (Sharpe phase winner; override via .env) ---
# WISDOM_MODE=dynamic, game plan yield-gate-only, halt resume 8% + liquidate.
# Opt-in (default off): NYSE overlap, beta scaling, SPY MA exit, adaptive/cofire.
# DERIVED_BEAR_PAUSE stays off.
#
# === MOST REALISTIC PAPER / RESEARCH DEFAULT (locked 2026-06) ===
# v1.0: Minimal + Deep History Indicators Only (walk-forward + Monte Carlo robust).
# v1.1: v1.0 + Tail Risk Controls (vol ceiling, DD scaling, RHYME_B buffers, sector safety).
# Core: locked SPY @ 40% passive (90d Sharpe VTI 0.56 vs SPY 0.60) — 60% active sleeve budget.
#
# Tail-risk additions in v1.1 (modules/paper_risk_controls.py, modules/sector_screener.py):
#   - TAIL_RISK_CONTROLS_ENABLED — master switch (paper/research default ON)
#   - Vol ceiling — scale risk when ann. vol > PAPER_VOL_CEILING_PCT (default 17%)
#   - Portfolio vol cap — rolling equity vol vs PORTFOLIO_VOL_CEILING_PCT (18%)
#   - Drawdown tiers — 5% DD → 0.6× risk; 8% DD → 0.3× risk
#   - RHYME_B — sleeve cap trim + PAPER_REGIME_B_RISK_MULT (0.50×) + cash buffer boost
#   - Per-name cap — PAPER_MAX_POSITION_PCT 8%
#   - Weak-regime sleeve cap — PAPER_REGIME_WEAK_SLEEVE_MAX_PCT 25% in B/D/E
#   - Sector screener — limit expansion when SECTOR_HIGH_VOL_CEILING_PCT exceeded
#
# --- Realistic Research v1.5 (paper bot default) ---
# New in v1.5 (paper-only scanners + sizing):
#   1. RVOL Scanner — relative volume filter/boost on NYSE momentum (min 2.0x, boost @ 2.5x)
#   2. ORB Scanner — 30m opening-range breakout boost with RVOL confirmation (≥2.0x)
#   3. Catalyst Scoring — news/insider/RVOL/ORB/Kimi composite score (min 65, boost @ 70)
#   4. ATR Sizing — volatility-based notional (14d ATR, 2.0× stop, 4% per-trade cap)
# Carries forward v1.4: tuned shorts 8–18%, sector shorts, dynamic core 63d, insider boosts,
# stat arb 10–14 pairs (RR 1.6:1), tail-risk vol ceiling, sector screener expansion.
# OFFICIALLY LOCKED — scripts/lock_v15.py (idempotent)
REALISTIC_RESEARCH_VERSION = "1.5.4"
REALISTIC_RESEARCH_PROFILE_VERSION = REALISTIC_RESEARCH_VERSION
REALISTIC_RESEARCH_TAGLINE = "v1.5.4 - Sector-Aware Portfolio Constructor"
REALISTIC_RESEARCH_FEATURE_DETAIL = (
    "Smart Dynamic VTI (35-75%) + Sector Rotation (top 2-3 SPDRs) + "
    "ATR Vol Breakout (RVOL+MTF, <=1% risk) + "
    "Sector-Aware Portfolio Constructor + "
    "RVOL/ORB/Catalyst/ATR + Conviction + MTF + Exits + Corr Guard + Shorts + "
    "Stat Arb v1.5.2 + Enriched Thinking"
)
# Locked when enforce_realistic_research_profile() runs (paper chase / Profile B):
REALISTIC_RESEARCH_LOCKED_FEATURES: tuple[str, ...] = (
    "RVOL scanner (min 2.0x)",
    "ORB scanner (30m + RVOL confirm)",
    "Catalyst scoring (min 65)",
    "ATR sizing (14d, 2.0x stop, 4% cap)",
    "Conviction sizing (0.4x-2.0x by signal strength)",
    "Multi-timeframe confirmation (5m/daily/weekly)",
    "Exit optimization (partial + dynamic trail)",
    "Portfolio correlation guard (max 0.65)",
    "Insider monitor + boosts",
    "Protective shorts (8-18%) + sector shorts",
    "Stat arb (12-16 pairs, corr 0.69, RR 1.6, v1.5.2 quality filters)",
    "Smart Dynamic VTI core (35-75%: NYSE/metals, insider, bubble, regime)",
    "Sector rotation (top 2-3 SPDRs, max 25%/sector, monthly/regime)",
    "ATR vol breakout (expand>=1.5x + RVOL/MTF, <=1% risk, paper-only)",
    "Enriched thinking engine (Ollama context + heuristic backtest tilts)",
    "Tail risk controls",
    "Bot Health + strategy performance tracking",
    "Heartbeat watchdog + auto-recovery",
)
#
# --- VTI passive core + active satellite ---
VTI_CORE_ENABLED = os.getenv("VTI_CORE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
VTI_CORE_PCT = float(os.getenv("VTI_CORE_PCT", "0.80"))
VTI_CORE_SYMBOL = os.getenv("VTI_CORE_SYMBOL", "VTI").strip().upper()
# Rebalance VTI when |current - target| / equity exceeds this (avoids daily churn)
VTI_CORE_REBALANCE_DRIFT_PCT = float(os.getenv("VTI_CORE_REBALANCE_DRIFT_PCT", "0.02"))

# Paper research book (PAPER_APCA_*) — uses REALISTIC_RESEARCH_PROFILE defaults below.
PAPER_AGGRESSIVE_ENABLED = os.getenv("PAPER_AGGRESSIVE", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_VTI_CORE_PCT = float(os.getenv("PAPER_VTI_CORE_PCT", "0.80"))
# Fixed passive core for paper research (80% VTI / 20% active sleeves when PAPER_DYNAMIC_VTI=false).
PAPER_SOCIAL_SLEEVE_CAP_PCT = float(os.getenv("PAPER_SOCIAL_SLEEVE_CAP_PCT", "0.20"))
PAPER_ACTIVE_SLEEVE_BOOST = float(os.getenv("PAPER_ACTIVE_SLEEVE_BOOST", "1.40"))
# Paper deployment loosening — reduce idle cash from min-notional / dust / sleeve headroom blocks.
PAPER_MIN_NOTIONAL_MULT = float(os.getenv("PAPER_MIN_NOTIONAL_MULT", "0.50"))
PAPER_MIN_NOTIONAL = float(os.getenv("PAPER_MIN_NOTIONAL", "2.0"))
PAPER_DUST_MAX_NOTIONAL = float(os.getenv("PAPER_DUST_MAX_NOTIONAL", "0.50"))
PAPER_DUST_SKIP_CHUNK_FRAC = float(os.getenv("PAPER_DUST_SKIP_CHUNK_FRAC", "0.02"))
PAPER_EXCESS_CASH_THRESHOLD_PCT = float(os.getenv("PAPER_EXCESS_CASH_THRESHOLD_PCT", "0.15"))
PAPER_EXCESS_CASH_SLEEVE_BOOST = float(os.getenv("PAPER_EXCESS_CASH_SLEEVE_BOOST", "1.12"))
PAPER_EXCESS_CASH_HIGH_THRESHOLD_PCT = float(os.getenv("PAPER_EXCESS_CASH_HIGH_THRESHOLD_PCT", "0.30"))
PAPER_EXCESS_CASH_HIGH_BOOST = float(os.getenv("PAPER_EXCESS_CASH_HIGH_BOOST", "1.35"))
PAPER_EXCESS_CASH_DEPLOY_THRESHOLD_PCT = float(os.getenv("PAPER_EXCESS_CASH_DEPLOY_THRESHOLD_PCT", "0.20"))
PAPER_AGGRESSIVE_CASH_USE_PCT = float(os.getenv("PAPER_AGGRESSIVE_CASH_USE_PCT", "0.99"))
# Live deployment floors — conservative defaults for Profile A (~$300) and larger live books.
LIVE_MIN_NOTIONAL = float(os.getenv("LIVE_MIN_NOTIONAL", "10.0"))
LIVE_DUST_MAX_NOTIONAL = float(os.getenv("LIVE_DUST_MAX_NOTIONAL", "1.0"))
LIVE_DUST_SKIP_CHUNK_FRAC = float(os.getenv("LIVE_DUST_SKIP_CHUNK_FRAC", "0.05"))
LIVE_EXCESS_CASH_SLEEVE_BOOST = float(os.getenv("LIVE_EXCESS_CASH_SLEEVE_BOOST", "1.0"))
LIVE_EXCESS_CASH_HIGH_BOOST = float(os.getenv("LIVE_EXCESS_CASH_HIGH_BOOST", "1.0"))
LIVE_EXCESS_CASH_DEPLOY_THRESHOLD_PCT = float(
    os.getenv("LIVE_EXCESS_CASH_DEPLOY_THRESHOLD_PCT", "1.0")
)
PAPER_NO_ROOM_MIN_MULT = float(os.getenv("PAPER_NO_ROOM_MIN_MULT", "0.25"))
PAPER_STAT_ARB_LEG_MIN_MULT = float(os.getenv("PAPER_STAT_ARB_LEG_MIN_MULT", "1.0"))
PAPER_DEPLOY_DEBUG = _parse_env_bool("PAPER_DEPLOY_DEBUG", default="true")
# Paper-only: soften yield gate so mild rate/bond stress does not block deployment.
# Live stays fully gated. When True, only strong bear/panic regimes keep the gate.
PAPER_YIELD_GATE_OVERRIDE = _parse_env_bool(
    "PAPER_YIELD_GATE_OVERRIDE", default="false"
)
PAPER_EXCESS_CASH_WARN_PCT = float(os.getenv("PAPER_EXCESS_CASH_WARN_PCT", "0.20"))
PAPER_EXCESS_CASH_WARN_DAYS = int(os.getenv("PAPER_EXCESS_CASH_WARN_DAYS", "3"))
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
# Winners treatment (profit-protect) — paper research opt-in; off on live Profile A
PAPER_PROFIT_PROTECT_ENABLED = _parse_env_bool(
    "PAPER_PROFIT_PROTECT_ENABLED", default="false"
)
# Top1 vol sizing / loss cutting — paper research opt-in; off on live Profile A
PAPER_VOL_POSITION_SIZING_ENABLED = _parse_env_bool(
    "PAPER_VOL_POSITION_SIZING_ENABLED", default="false"
)
PAPER_LOSS_CUTTING_ENABLED = _parse_env_bool(
    "PAPER_LOSS_CUTTING_ENABLED", default="false"
)
# Research opt-in sleeves (status.py / paper aggressive only)
PAPER_INTERNATIONAL_SLEEVE_ENABLED = _parse_env_bool(
    "PAPER_INTERNATIONAL_SLEEVE_ENABLED", default="false"
)
PAPER_BOND_SLEEVE_ENABLED = _parse_env_bool("PAPER_BOND_SLEEVE_ENABLED", default="false")
PAPER_SECTOR_ROTATION_ENABLED = _parse_env_bool(
    "PAPER_SECTOR_ROTATION_ENABLED", default="true"
)
# Sector rotation sleeve (SPDR ETFs) — paper-first; live opt-in
SECTOR_ROTATION_ENABLED = _env_bool_first(
    "SECTOR_ROTATION_ENABLED", "PAPER_SECTOR_ROTATION_ENABLED", default="true"
)
SECTOR_ROTATION_LIVE_SLEEVE = _parse_env_bool("SECTOR_ROTATION_LIVE_SLEEVE", default="false")
SECTOR_ROTATION_CAP_PCT = float(os.getenv("SECTOR_ROTATION_CAP_PCT", "0.20"))
SECTOR_ROTATION_LIVE_CAP_PCT = float(os.getenv("SECTOR_ROTATION_LIVE_CAP_PCT", "0.05"))
SECTOR_ROTATION_MAX_SECTOR_PCT = float(os.getenv("SECTOR_ROTATION_MAX_SECTOR_PCT", "0.25"))
# Top 2–3 sector SPDRs by momentum + RS vs SPY (Realistic Research default: 3).
SECTOR_ROTATION_TOP_N = int(os.getenv("SECTOR_ROTATION_TOP_N", "3"))
SECTOR_ROTATION_MIN_SCORE = float(os.getenv("SECTOR_ROTATION_MIN_SCORE", "0.0"))
SECTOR_ROTATION_DRIFT_PCT = float(os.getenv("SECTOR_ROTATION_DRIFT_PCT", "0.04"))
SECTOR_ROTATION_STATE_FILE = os.getenv(
    "SECTOR_ROTATION_STATE_FILE", "data/sector_rotation_state.json"
)
SECTOR_ROTATION_PAPER_DEFAULT = _parse_env_bool(
    "SECTOR_ROTATION_PAPER_DEFAULT", default="true"
)
SECTOR_ROTATION_BACKTEST_ENABLED = _parse_env_bool(
    "SECTOR_ROTATION_BACKTEST_ENABLED", default="true"
)
PAPER_TECH_GUARD_ENABLED = _parse_env_bool("PAPER_TECH_GUARD_ENABLED", default="true")
PAPER_SCALING_STRATEGY_ENABLED = _parse_env_bool(
    "PAPER_SCALING_STRATEGY_ENABLED", default="false"
)
PAPER_PATTERN_AWARENESS_ENABLED = _parse_env_bool(
    "PAPER_PATTERN_AWARENESS_ENABLED", default="false"
)
INTERNATIONAL_SLEEVE_CAP_PCT = float(os.getenv("INTERNATIONAL_SLEEVE_CAP_PCT", "0.10"))
BOND_SLEEVE_CAP_PCT = float(os.getenv("BOND_SLEEVE_CAP_PCT", "0.15"))
BOND_SLEEVE_SYMBOL = os.getenv("BOND_SLEEVE_SYMBOL", "TLT").strip().upper() or "TLT"
STRICT_PIT_BACKTEST = _parse_env_bool("STRICT_PIT_BACKTEST", default="false")
# Classic crypto pairs sleeve — off by default on live and paper bots
PAPER_CRYPTO_ENABLED = _parse_env_bool("PAPER_CRYPTO_ENABLED", default="false")
PAPER_CRYPTO_MAX_PAIRS = int(os.getenv("PAPER_CRYPTO_MAX_PAIRS", "4"))
PAPER_CRYPTO_MAX_TRADES = int(os.getenv("PAPER_CRYPTO_MAX_TRADES", "2"))
PAPER_CRYPTO_Z_EXIT = float(os.getenv("PAPER_CRYPTO_Z_EXIT", "0.5"))
PAPER_CRYPTO_Z_ENTRY_BUMP = float(os.getenv("PAPER_CRYPTO_Z_ENTRY_BUMP", "0.4"))
PAPER_CRYPTO_MAX_HOLD_BARS = int(os.getenv("PAPER_CRYPTO_MAX_HOLD_BARS", "8"))
PAPER_CRYPTO_MIN_NOTIONAL_MULT = float(os.getenv("PAPER_CRYPTO_MIN_NOTIONAL_MULT", "1.25"))
PAPER_CRYPTO_RISK_MULT = float(os.getenv("PAPER_CRYPTO_RISK_MULT", "1.25"))
PAPER_CRYPTO_REGIME_FILTER = _parse_env_bool("PAPER_CRYPTO_REGIME_FILTER", default="true")
CRYPTO_SLEEVE_ENABLED = _parse_env_bool("CRYPTO_SLEEVE_ENABLED", default="false")
PAPER_CRYPTO_V2_SYMBOLS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "ADA-USD",
    "DOT-USD", "MATIC-USD", "ATOM-USD", "UNI-USD", "LTC-USD", "BCH-USD",
    "APT-USD", "ARB-USD", "OP-USD", "NEAR-USD", "FIL-USD", "AAVE-USD",
    "INJ-USD", "DOGE-USD", "SHIB-USD", "RENDER-USD", "SUI-USD", "PEPE-USD",
]
# Path B: expanded Alpaca crypto universe (paper default on; live Profile A off)
_paper_crypto_expanded_default = "true" if PAPER_TRADING else "false"
PAPER_CRYPTO_UNIVERSE_EXPANDED = os.getenv(
    "PAPER_CRYPTO_UNIVERSE_EXPANDED", _paper_crypto_expanded_default
).lower() in ("1", "true", "yes")
CRYPTO_EXPANDED_MAX_SYMBOLS = int(os.getenv("CRYPTO_EXPANDED_MAX_SYMBOLS", "48"))
CRYPTO_EXPANDED_MIN_BARS = int(os.getenv("CRYPTO_EXPANDED_MIN_BARS", "30"))
_backtest_crypto_expanded_prefetch = False
PAPER_VTI_REBALANCE_DRIFT_PCT = float(os.getenv("PAPER_VTI_REBALANCE_DRIFT_PCT", "0.01"))
PAPER_DYNAMIC_VTI_ENABLED = os.getenv("PAPER_DYNAMIC_VTI", "true").lower() in (
    "1",
    "true",
    "yes",
)
# Dynamic core allocator — off by default (experiment: fixed 80% core beat dynamic allocator).
_paper_dynamic_core_default = "false"
DYNAMIC_CORE_ENABLED = os.getenv("DYNAMIC_CORE_ENABLED", _paper_dynamic_core_default).lower() in (
    "1",
    "true",
    "yes",
)
DYNAMIC_CORE_LIVE_ENABLED = os.getenv("DYNAMIC_CORE_LIVE_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
DYNAMIC_CORE_MIN_PCT = float(os.getenv("DYNAMIC_CORE_MIN_PCT", "0.30"))
DYNAMIC_CORE_MAX_PCT = float(os.getenv("DYNAMIC_CORE_MAX_PCT", "0.50"))
DYNAMIC_CORE_REVIEW_DAYS = int(os.getenv("DYNAMIC_CORE_REVIEW_DAYS", "30"))
DYNAMIC_CORE_LOOKBACK_DAYS = int(os.getenv("DYNAMIC_CORE_LOOKBACK_DAYS", "63"))
# Locked core allocator (paper research final 2026-06): SPY @ 40% passive slice.
# Sharpe 90d lookback: VTI 0.56 vs SPY 0.60 — frees 60% for active sleeves vs 15% @ 85% VTI.
CORE_ALLOCATOR_LOCKED = _env_bool_first("CORE_ALLOCATOR_LOCKED", default="false")
CORE_ALLOCATOR_LOCKED_CHOICE = (
    os.getenv("CORE_ALLOCATOR_LOCKED_CHOICE", "spy").strip().lower() or "spy"
)
# Deep indicator context for regime/MAs; sim window stays on --days (see backtester --deep-history).
DEEP_HISTORY_ENABLED = _env_bool_first("DEEP_HISTORY_ENABLED", default="true")
DEEP_HISTORY_INDICATORS_ONLY = _env_bool_first(
    "DEEP_HISTORY_INDICATORS_ONLY", default="true"
)
# When True, VTI core floats 35–75% via Smart Dynamic VTI (vol/stress + sleeve/insider/bubble/regime).
PAPER_SOFT_PAUSE_ENABLED = os.getenv("PAPER_SOFT_PAUSE", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Paper-only: in PAUSED_REGIMES (wisdom/rhyme bear+vol), size down instead of blocking.
# Pair with PAPER_DYNAMIC_VTI=false + PAPER_VTI_CORE_PCT=0.80 for fixed 80/20 core/active split.
PAPER_SOFT_PAUSE_SIZING_MULT = float(os.getenv("PAPER_SOFT_PAUSE_SIZING_MULT", "0.50"))
PAPER_DYNAMIC_RISK_ENABLED = os.getenv("PAPER_DYNAMIC_RISK_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
_paper_dyn_univ = os.getenv("PAPER_DYNAMIC_UNIVERSE_ENABLED") or os.getenv(
    "PAPER_DYNAMIC_UNIVERSE", "true"
)
PAPER_DYNAMIC_UNIVERSE_ENABLED = _paper_dyn_univ.lower() in ("1", "true", "yes")
PAPER_DYNAMIC_UNIVERSE_STRICT = os.getenv(
    "PAPER_DYNAMIC_UNIVERSE_STRICT", "false"
).lower() in ("1", "true", "yes")
# Dynamic sector screener — expand momentum/stat-arb pools in strong sectors (paper only).
DYNAMIC_SECTOR_SCREENER_ENABLED = _env_bool_first(
    "DYNAMIC_SECTOR_SCREENER_ENABLED", default="true"
)
BASE_UNIVERSE_SIZE = int(os.getenv("BASE_UNIVERSE_SIZE", "110"))
SECTOR_EXPANSION_SIZE = int(os.getenv("SECTOR_EXPANSION_SIZE", "45"))
SECTOR_STRENGTH_MA_WINDOW = int(os.getenv("SECTOR_STRENGTH_MA_WINDOW", "200"))
MAX_ACTIVE_SECTORS = int(os.getenv("MAX_ACTIVE_SECTORS", "3"))
MAX_ACTIVE_SECTORS_STRONG = int(os.getenv("MAX_ACTIVE_SECTORS_STRONG", "4"))
SECTOR_STRONG_SCORE_MIN = float(os.getenv("SECTOR_STRONG_SCORE_MIN", "0.06"))
SECTOR_MAX_TOTAL_TICKERS = int(os.getenv("SECTOR_MAX_TOTAL_TICKERS", "180"))
SECTOR_STRENGTH_THRESHOLD = float(os.getenv("SECTOR_STRENGTH_THRESHOLD", "0.0"))
SECTOR_RS_MIN = float(os.getenv("SECTOR_RS_MIN", "0.0"))
SECTOR_FALLBACK_MOMENTUM_COUNT = int(os.getenv("SECTOR_FALLBACK_MOMENTUM_COUNT", "18"))
# Portfolio constructor (v1.5.4, paper research only) — sector_regime_score-driven tilts
# on top of Smart Dynamic VTI. See modules/portfolio_constructor.py.
PORTFOLIO_CONSTRUCTOR_ENABLED = _env_bool_first(
    "PORTFOLIO_CONSTRUCTOR_ENABLED", default="false"
)
PORTFOLIO_ACTIVE_SLEEVE_MULT_FLOOR = float(
    os.getenv("PORTFOLIO_ACTIVE_SLEEVE_MULT_FLOOR", "0.85")
)
PORTFOLIO_ACTIVE_SLEEVE_MULT_CEILING = float(
    os.getenv("PORTFOLIO_ACTIVE_SLEEVE_MULT_CEILING", "1.15")
)
SECTOR_SCREENER_LOG_FILE = os.getenv(
    "SECTOR_SCREENER_LOG_FILE", "logs/sector_screener.jsonl"
)
SECTOR_SCREENER_STATE_FILE = os.getenv(
    "SECTOR_SCREENER_STATE_FILE", "data/sector_screener_state.json"
)
# Tuned defaults for paper-aggressive (minimal research profile, 2026-06).
PAPER_RISK_PER_TRADE = float(os.getenv("PAPER_RISK_PER_TRADE", "0.018"))
PAPER_RISK_CALM_BULL_PCT = float(
    os.getenv("PAPER_RISK_CALM_BULL_PCT", str(PAPER_RISK_PER_TRADE))
)
PAPER_RISK_MODERATE_PCT = float(os.getenv("PAPER_RISK_MODERATE_PCT", "0.0105"))
PAPER_RISK_STRESS_PCT = float(os.getenv("PAPER_RISK_STRESS_PCT", "0.007"))
# Regime + drawdown risk multiplier (paper aggressive); see modules/paper_risk_controls.py
PAPER_REGIME_DD_RISK_ENABLED = os.getenv("PAPER_REGIME_DD_RISK_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_REGIME_B_RISK_MULT = float(os.getenv("PAPER_REGIME_B_RISK_MULT", "0.50"))
PAPER_REGIME_D_RISK_MULT = float(os.getenv("PAPER_REGIME_D_RISK_MULT", "0.75"))
PAPER_DD_RISK_WARN_PCT = float(os.getenv("PAPER_DD_RISK_WARN_PCT", "0.05"))
PAPER_DD_RISK_MULT_5 = float(os.getenv("PAPER_DD_RISK_MULT_5", "0.6"))
PAPER_DD_RISK_SEVERE_PCT = float(os.getenv("PAPER_DD_RISK_SEVERE_PCT", "0.08"))
PAPER_DD_RISK_MULT_8 = float(os.getenv("PAPER_DD_RISK_MULT_8", "0.3"))
# Legacy alias (pre-8% severe tier)
PAPER_DD_RISK_MULT_7 = float(os.getenv("PAPER_DD_RISK_MULT_7", str(PAPER_DD_RISK_MULT_8)))
PAPER_POSITION_MAX_HOLD_BARS = int(os.getenv("PAPER_POSITION_MAX_HOLD_BARS", "30"))
PER_NAME_MAX_PCT = float(os.getenv("PER_NAME_MAX_PCT", "0.08"))
PAPER_MAX_POSITION_PCT = float(os.getenv("PAPER_MAX_POSITION_PCT", str(PER_NAME_MAX_PCT)))
# Tail-risk overlay (Realistic Research v1.1) — portfolio vol cap, panic buffers, sector safety.
# Default ON for paper/research; set TAIL_RISK_CONTROLS_ENABLED=false to disable.
TAIL_RISK_CONTROLS_ENABLED = _env_bool_first("TAIL_RISK_CONTROLS_ENABLED", default="true")
PORTFOLIO_VOL_WINDOW = int(os.getenv("PORTFOLIO_VOL_WINDOW", "20"))
PORTFOLIO_VOL_CEILING_PCT = float(os.getenv("PORTFOLIO_VOL_CEILING_PCT", "0.18"))
PORTFOLIO_VOL_MIN_RISK_MULT = float(os.getenv("PORTFOLIO_VOL_MIN_RISK_MULT", "0.35"))
PAPER_REGIME_B_CASH_BUFFER_BOOST = float(os.getenv("PAPER_REGIME_B_CASH_BUFFER_BOOST", "0.12"))
SECTOR_HIGH_VOL_CEILING_PCT = float(os.getenv("SECTOR_HIGH_VOL_CEILING_PCT", "0.18"))
SECTOR_HIGH_VOL_EXPANSION_CAP = int(os.getenv("SECTOR_HIGH_VOL_EXPANSION_CAP", "10"))
SECTOR_HIGH_VOL_MAX_ACTIVE_SECTORS = int(os.getenv("SECTOR_HIGH_VOL_MAX_ACTIVE_SECTORS", "1"))
# Portfolio vol ceiling (annualized) — paper default 17%; live uses VOL_CEILING_PCT.
PAPER_VOL_CEILING_PCT = float(os.getenv("PAPER_VOL_CEILING_PCT", "0.17"))
VOL_CEILING_ENABLED = os.getenv("VOL_CEILING_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
VOL_CEILING_PCT = float(os.getenv("VOL_CEILING_PCT", "0.18"))
PAPER_TRAILING_STOP_ARM_PCT = float(os.getenv("PAPER_TRAILING_STOP_ARM_PCT", "0.10"))
PAPER_TRAILING_STOP_TRAIL_PCT = float(os.getenv("PAPER_TRAILING_STOP_TRAIL_PCT", "0.05"))
PAPER_SPY_MAX_EXPOSURE_PCT = float(os.getenv("PAPER_SPY_MAX_EXPOSURE_PCT", "0.46"))
# Paper/research NYSE momentum sleeve: target 18–22% (scaled fund math often lands ~9–16%).
PAPER_NYSE_SLEEVE_CAP_PCT = float(os.getenv("PAPER_NYSE_SLEEVE_CAP_PCT", "0.20"))
PAPER_NYSE_HIGH_CASH_CAP_PCT = float(os.getenv("PAPER_NYSE_HIGH_CASH_CAP_PCT", "0.22"))
PAPER_NYSE_MAX_EXPOSURE_PCT = float(os.getenv("PAPER_NYSE_MAX_EXPOSURE_PCT", "0.22"))
# Paper NYSE momentum: allow multiple names per cycle when cash is idle.
PAPER_MAX_EQUITY_TRADES = int(os.getenv("PAPER_MAX_EQUITY_TRADES", "3"))
PAPER_CRYPTO_MAX_EXPOSURE_PCT = float(os.getenv("PAPER_CRYPTO_MAX_EXPOSURE_PCT", "0.12"))
PAPER_HALT_RESUME_DRAWDOWN_PCT = float(os.getenv("PAPER_HALT_RESUME_DRAWDOWN_PCT", "0.06"))
PAPER_HALT_RECOVERY_RISK_MULT = float(os.getenv("PAPER_HALT_RECOVERY_RISK_MULT", "0.65"))
PAPER_HALT_RECOVERY_CLEAR_PCT = float(os.getenv("PAPER_HALT_RECOVERY_CLEAR_PCT", "0.03"))
DYNAMIC_VTI_PAPER_FLOOR = float(os.getenv("DYNAMIC_VTI_PAPER_FLOOR", "0.35"))
DYNAMIC_VTI_PAPER_CEILING = float(os.getenv("DYNAMIC_VTI_PAPER_CEILING", "0.75"))
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
PAPER_STAT_ARB_ENABLED = _env_bool_first(
    "PAPER_STAT_ARB_ENABLED", "STAT_ARB_ENABLED", default="true"
)
PAPER_PAIR_MIN_CORRELATION = float(os.getenv("PAPER_PAIR_MIN_CORRELATION", "0.65"))
PAPER_PAIR_Z_THRESHOLD = float(os.getenv("PAPER_PAIR_Z_THRESHOLD", "2.0"))
PAPER_PAIR_Z_EXIT = float(os.getenv("PAPER_PAIR_Z_EXIT", "0.5"))
PAPER_PAIR_Z_DYNAMIC = os.getenv("PAPER_PAIR_Z_DYNAMIC", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAPER_PAIR_Z_CALM = float(os.getenv("PAPER_PAIR_Z_CALM", "1.8"))
PAPER_PAIR_Z_STRESS = float(os.getenv("PAPER_PAIR_Z_STRESS", "2.4"))
PAPER_PAIR_COINT_SLOPE = float(os.getenv("PAPER_PAIR_COINT_SLOPE", "-0.01"))
PAPER_STAT_ARB_MAX_TRADES = int(os.getenv("PAPER_STAT_ARB_MAX_TRADES", "2"))
# Stat arb v1.1 (Realistic Research) — cointegration, tighter corr, overlap guard, RR exits.
# v1.5.1 tune: corr 0.72->0.68, pairs 10/12/14 -> 12/14/16 to lift activity when room exists.
# v1.5.2 quality tune: corr 0.68->0.70, liquidity 25M->40M, hold 35->25 (equity), stronger
# reversion (0.55->0.60), tighter trail, vol filter + conviction sizing to cut drag.
PAPER_STAT_ARB_MIN_CORR = float(os.getenv("PAPER_STAT_ARB_MIN_CORR", "0.69"))
PAPER_STAT_ARB_MAX_PAIRS = int(os.getenv("PAPER_STAT_ARB_MAX_PAIRS", "12"))
PAPER_STAT_ARB_MAX_PAIRS_EXPANDED = int(os.getenv("PAPER_STAT_ARB_MAX_PAIRS_EXPANDED", "14"))
PAPER_STAT_ARB_MAX_PAIRS_CEILING = int(os.getenv("PAPER_STAT_ARB_MAX_PAIRS_CEILING", "16"))
# Overlap guard: block top N*mult NYSE momentum names from stat-arb legs (avoid double exposure).
STAT_ARB_NYSE_OVERLAP_BLOCK_MULT = int(os.getenv("STAT_ARB_NYSE_OVERLAP_BLOCK_MULT", "2"))
PAPER_STAT_ARB_MAX_HOLD_BARS = int(os.getenv("PAPER_STAT_ARB_MAX_HOLD_BARS", "35"))
PAPER_STAT_ARB_Z_ENTRY_BASE = float(os.getenv("PAPER_STAT_ARB_Z_ENTRY_BASE", "2.0"))
PAPER_STAT_ARB_Z_ENTRY_MAX = float(os.getenv("PAPER_STAT_ARB_Z_ENTRY_MAX", "2.6"))
PAPER_STAT_ARB_RISK_REWARD = float(os.getenv("PAPER_STAT_ARB_RISK_REWARD", "1.6"))
PAPER_STAT_ARB_COINT_PVALUE = float(os.getenv("PAPER_STAT_ARB_COINT_PVALUE", "0.12"))
PAPER_STAT_ARB_Z_EXIT = float(os.getenv("PAPER_STAT_ARB_Z_EXIT", "0.5"))
# v1.5.2: require reversion >=55% of profit target before mean-revert exit (unchanged from v1.5.1).
PAPER_STAT_ARB_MIN_REVERT_FRAC = float(os.getenv("PAPER_STAT_ARB_MIN_REVERT_FRAC", "0.55"))
# v1.5.2: raise liquidity floor 25M->40M to favor tighter, cleaner pairs.
PAPER_STAT_ARB_MIN_DOLLAR_VOLUME = float(
    os.getenv("PAPER_STAT_ARB_MIN_DOLLAR_VOLUME", "35000000")
)
# v1.5.2: tighter trail on profitable pairs — arm sooner (0.40) and lock on smaller pullback (0.25).
PAPER_STAT_ARB_TRAILING_ARM_FRAC = float(
    os.getenv("PAPER_STAT_ARB_TRAILING_ARM_FRAC", "0.40")
)
PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC = float(
    os.getenv("PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC", "0.25")
)
# v1.5.2: only arm the tighter trailing stop after this fraction of profit target
# is achieved (prevents noise exits on pairs that haven't meaningfully reverted).
PAPER_STAT_ARB_TRAIL_MIN_PROFIT_FRAC = float(
    os.getenv("PAPER_STAT_ARB_TRAIL_MIN_PROFIT_FRAC", "0.50")
)
# v1.5.2: disable partial exits on stat-arb pairs (leg asymmetry bleeds PnL).
PAPER_STAT_ARB_PARTIAL_EXIT = _env_bool_first(
    "PAPER_STAT_ARB_PARTIAL_EXIT", default="false"
)
# v1.5.2: dedicated equity-pair max hold (bars); shorter than the shared 35-bar cap.
PAPER_STAT_ARB_EQUITY_MAX_HOLD_BARS = int(
    os.getenv("PAPER_STAT_ARB_EQUITY_MAX_HOLD_BARS", "25")
)
# v1.5.2: skip pairs where either leg's recent daily-return std exceeds this (high-vol filter).
PAPER_STAT_ARB_MAX_LEG_VOL = float(os.getenv("PAPER_STAT_ARB_MAX_LEG_VOL", "0.065"))
# v1.5.2: stat-arb-specific conviction sizing band (tighter than global 0.4x-2.0x).
PAPER_STAT_ARB_CONVICTION_MIN_SCALE = float(
    os.getenv("PAPER_STAT_ARB_CONVICTION_MIN_SCALE", "0.65")
)
PAPER_STAT_ARB_CONVICTION_MAX_SCALE = float(
    os.getenv("PAPER_STAT_ARB_CONVICTION_MAX_SCALE", "1.50")
)
PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF = _env_bool_first(
    "PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF", default="true"
)
PAPER_STAT_ARB_SECTOR_NEUTRAL_BOOST = float(
    os.getenv("PAPER_STAT_ARB_SECTOR_NEUTRAL_BOOST", "1.12")
)
PAPER_STAT_ARB_USE_COINT = _env_bool_first("PAPER_STAT_ARB_USE_COINT", default="true")
STAT_ARB_LOOKBACK = int(os.getenv("STAT_ARB_LOOKBACK", "60"))
# Verbose stat-arb scan/funnel diagnostics (paper/backtest only; default off).
STAT_ARB_SCAN_DEBUG = os.getenv("STAT_ARB_SCAN_DEBUG", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Optimized stat arb — paper/backtest only (default off; live unchanged)
PAPER_STAT_ARB_OPTIMIZED = os.getenv("PAPER_STAT_ARB_OPTIMIZED", "false").lower() in (
    "1",
    "true",
    "yes",
)
STAT_ARB_MAX_HOLD_BARS = int(os.getenv("STAT_ARB_MAX_HOLD_BARS", "35"))
STAT_ARB_PROFIT_Z_DELTA = float(os.getenv("STAT_ARB_PROFIT_Z_DELTA", "1.2"))
STAT_ARB_CORR_HALF_LIFE = float(os.getenv("STAT_ARB_CORR_HALF_LIFE", "25"))
# --- Protective / opportunistic shorts (Realistic Research v1.5 tuned, paper only) ---
# Entry:
#   RHYME_B — VIX≥22 rising + momentum exhaustion + depth≥2% (cap 18% gross)
#   RHYME_E — 3-bar SPY bear streak + depth≥3% + bubble≥60 (cap 12%); VIX≥28 waives
#             full streak if ≥2 down days; waiver entries sized at 75%
#   Sector ETFs — weak momentum + bubble≥55 + full 3-bar streak; ≤8%/name
# Sizing — bubble power curve (1.35); gross 8–18% by regime + bubble score
# Exits — 50% partial @ 1:1 RR; trail arm 50% / pull 35%; tighter stop VIX 25–28 only
#         (panic VIX≥28 keeps full 2% stop); long hedge floor 78% when shorts on
PROTECTIVE_SHORT_ENABLED = _env_bool_first(
    "PROTECTIVE_SHORT_ENABLED", "PAPER_PROTECTIVE_SHORT_ENABLED", default="true"
)
SHORT_OPPORTUNISTIC_ENABLED = _env_bool_first(
    "SHORT_OPPORTUNISTIC_ENABLED", "PAPER_SHORT_OPPORTUNISTIC_ENABLED", default="false"
)
PROTECTIVE_SHORT_MAX_PCT = float(os.getenv("PROTECTIVE_SHORT_MAX_PCT", "0.18"))
PROTECTIVE_SHORT_MIN_PCT = float(os.getenv("PROTECTIVE_SHORT_MIN_PCT", "0.08"))
# Portfolio constructor short-willingness tilt bounds — scales gross short target
# *within* PROTECTIVE_SHORT_MIN/MAX_PCT above; never past them (see PORTFOLIO_CONSTRUCTOR_ENABLED).
PORTFOLIO_SHORT_WILLINGNESS_FLOOR = float(
    os.getenv("PORTFOLIO_SHORT_WILLINGNESS_FLOOR", "0.60")
)
PORTFOLIO_SHORT_WILLINGNESS_CEILING = float(
    os.getenv("PORTFOLIO_SHORT_WILLINGNESS_CEILING", "1.40")
)
SHORT_RHYME_E_MAX_PCT = float(os.getenv("SHORT_RHYME_E_MAX_PCT", "0.12"))
SHORT_RHYME_B_MAX_PCT = float(os.getenv("SHORT_RHYME_B_MAX_PCT", "0.18"))
SECTOR_SHORT_ENABLED = _env_bool_first(
    "SECTOR_SHORT_ENABLED", "PAPER_SECTOR_SHORT_ENABLED", default="true"
)
SECTOR_SHORT_MAX_PCT = float(os.getenv("SECTOR_SHORT_MAX_PCT", "0.08"))
SECTOR_SHORT_MAX_SCORE = float(os.getenv("SECTOR_SHORT_MAX_SCORE", "-0.04"))
SECTOR_SHORT_MIN_RS_VS_SPY = float(os.getenv("SECTOR_SHORT_MIN_RS_VS_SPY", "-0.03"))
SECTOR_SHORT_MAX_POSITIONS = int(os.getenv("SECTOR_SHORT_MAX_POSITIONS", "2"))
SECTOR_SHORT_MIN_BUBBLE_SCORE = float(os.getenv("SECTOR_SHORT_MIN_BUBBLE_SCORE", "0.55"))
SHORT_DEEP_BEAR_MIN_DEPTH = float(os.getenv("SHORT_DEEP_BEAR_MIN_DEPTH", "0.030"))
SHORT_RHYME_B_MIN_DEPTH = float(os.getenv("SHORT_RHYME_B_MIN_DEPTH", "0.020"))
SHORT_RHYME_E_ENABLED = _env_bool_first(
    "SHORT_RHYME_E_ENABLED", "PAPER_SHORT_RHYME_E_ENABLED", default="true"
)
SHORT_RHYME_E_EXHAUSTION_REQUIRED = _env_bool_first(
    "SHORT_RHYME_E_EXHAUSTION_REQUIRED",
    "PAPER_SHORT_RHYME_E_EXHAUSTION_REQUIRED",
    default="false",
)
SHORT_RHYME_E_STRONG_BUBBLE = float(os.getenv("SHORT_RHYME_E_STRONG_BUBBLE", "0.65"))
SHORT_BUBBLE_MIN_FOR_RHYME_E = float(os.getenv("SHORT_BUBBLE_MIN_FOR_RHYME_E", "60"))
SHORT_WEAK_MOMENTUM_MAX = float(os.getenv("SHORT_WEAK_MOMENTUM_MAX", "-0.05"))
SHORT_SINGLE_NAME_MAX_TRADES = int(os.getenv("SHORT_SINGLE_NAME_MAX_TRADES", "1"))
SHORT_BROAD_SYMBOLS_RAW = os.getenv("SHORT_BROAD_SYMBOLS", "SPY,QQQ")
SHORT_VIX_SPIKE_CONFIRM = _env_bool_first(
    "SHORT_VIX_SPIKE_CONFIRM", "PAPER_SHORT_VIX_SPIKE_CONFIRM", default="true"
)
SHORT_VIX_MIN_LEVEL = float(
    os.getenv("SHORT_VIX_MIN") or os.getenv("SHORT_VIX_MIN_LEVEL") or "22"
)
SHORT_VIX_MIN = SHORT_VIX_MIN_LEVEL
SHORT_VIX_REQUIRE_RISING = _env_bool_first(
    "SHORT_VIX_REQUIRE_RISING", "PAPER_SHORT_VIX_REQUIRE_RISING", default="true"
)
SHORT_RSI_PERIOD = int(os.getenv("SHORT_RSI_PERIOD", "14"))
SHORT_RSI_EXHAUSTION_MIN = float(os.getenv("SHORT_RSI_EXHAUSTION_MIN", "70"))
SHORT_MA200_WINDOW = int(os.getenv("SHORT_MA200_WINDOW", "200"))
SHORT_MA200_EXTENSION_PCT = float(os.getenv("SHORT_MA200_EXTENSION_PCT", "0.08"))
SHORT_MA200_EXTENSION_LOOKBACK = int(os.getenv("SHORT_MA200_EXTENSION_LOOKBACK", "25"))
SHORT_MOMENTUM_EXHAUSTION_LOOKBACK = int(os.getenv("SHORT_MOMENTUM_EXHAUSTION_LOOKBACK", "10"))
SHORT_MOMENTUM_EXHAUSTION_MIN = float(os.getenv("SHORT_MOMENTUM_EXHAUSTION_MIN", "0.02"))
SHORT_BUBBLE_SCORE_MIN = float(os.getenv("SHORT_BUBBLE_SCORE_MIN", "0.45"))
BUFFETT_INDICATOR_ENABLED = _env_bool_first(
    "BUFFETT_INDICATOR_ENABLED", "PAPER_BUFFETT_INDICATOR_ENABLED", default="true"
)
BUFFETT_OVERVALUED_THRESHOLD = float(os.getenv("BUFFETT_OVERVALUED_THRESHOLD", "200"))
# SEC EDGAR insider & filings monitor — paper only
INSIDER_MONITOR_ENABLED = _env_bool_first(
    "INSIDER_MONITOR_ENABLED", "PAPER_INSIDER_MONITOR_ENABLED", default="true"
)
INSIDER_CLUSTER_MIN_BUYERS = int(os.getenv("INSIDER_CLUSTER_MIN_BUYERS", "2"))
INSIDER_MONITOR_POLL_HOURS = int(os.getenv("INSIDER_MONITOR_POLL_HOURS", "5"))
INSIDER_MONITOR_LOOKBACK_DAYS = int(os.getenv("INSIDER_MONITOR_LOOKBACK_DAYS", "7"))
INSIDER_MONITOR_STATE_FILE = os.getenv(
    "INSIDER_MONITOR_STATE_FILE", "data/insider_monitor_state.json"
)
INSIDER_SIGNAL_BOOST_ENABLED = _env_bool_first(
    "INSIDER_SIGNAL_BOOST_ENABLED", "PAPER_INSIDER_SIGNAL_BOOST_ENABLED", default="true"
)
# Paper-only master toggle (alias for signal boost; v1.5 tiered boosts).
INSIDER_BOOST_ENABLED = _env_bool_first(
    "INSIDER_BOOST_ENABLED", "PAPER_INSIDER_BOOST_ENABLED", default="true"
)
# Insider Signal Boost v1.5 — tiered multipliers (paper research only).
INSIDER_TIER1_CLUSTER_MIN = int(os.getenv("INSIDER_TIER1_CLUSTER_MIN", "5"))
INSIDER_TIER2_CLUSTER_MIN = int(os.getenv("INSIDER_TIER2_CLUSTER_MIN", "3"))
INSIDER_TIER1_CLUSTER_MIN_SCORE = int(os.getenv("INSIDER_TIER1_CLUSTER_MIN_SCORE", "80"))
INSIDER_TIER1_MOMENTUM_BOOST = float(os.getenv("INSIDER_TIER1_MOMENTUM_BOOST", "0.28"))
INSIDER_TIER2_MOMENTUM_BOOST = float(os.getenv("INSIDER_TIER2_MOMENTUM_BOOST", "0.18"))
INSIDER_TIER3_MOMENTUM_BOOST = float(os.getenv("INSIDER_TIER3_MOMENTUM_BOOST", "0.08"))
INSIDER_TIER1_STAT_ARB_MULT = float(os.getenv("INSIDER_TIER1_STAT_ARB_MULT", "1.22"))
INSIDER_TIER2_STAT_ARB_MULT = float(os.getenv("INSIDER_TIER2_STAT_ARB_MULT", "1.15"))
INSIDER_TIER1_SHORT_BOOST = float(os.getenv("INSIDER_TIER1_SHORT_BOOST", "0.42"))
INSIDER_TIER2_SHORT_BOOST = float(os.getenv("INSIDER_TIER2_SHORT_BOOST", "0.42"))
INSIDER_SHORT_AMPLIFIED_BOOST = float(os.getenv("INSIDER_SHORT_AMPLIFIED_BOOST", "0.55"))
INSIDER_LARGE_EXEC_VALUE_USD = float(os.getenv("INSIDER_LARGE_EXEC_VALUE_USD", "500000"))
INSIDER_BUBBLE_BULLISH_SUPPRESS = float(os.getenv("INSIDER_BUBBLE_BULLISH_SUPPRESS", "80"))
INSIDER_BUBBLE_SHORT_AMPLIFY_SCORE = float(os.getenv("INSIDER_BUBBLE_SHORT_AMPLIFY_SCORE", "65"))
INSIDER_RHYME_B_BULLISH_MULT = float(os.getenv("INSIDER_RHYME_B_BULLISH_MULT", "0.50"))
INSIDER_MAX_BOOSTED_POSITIONS = int(os.getenv("INSIDER_MAX_BOOSTED_POSITIONS", "3"))
INSIDER_SINGLE_NAME_CAP_PCT = float(os.getenv("INSIDER_SINGLE_NAME_CAP_PCT", "0.05"))
INSIDER_CLUSTER_BOOST_MAX = float(os.getenv("INSIDER_CLUSTER_BOOST_MAX", "0.30"))
INSIDER_SELL_SHORT_BOOST_MAX = float(os.getenv("INSIDER_SELL_SHORT_BOOST_MAX", "0.58"))
INSIDER_RISK_GUARD_ENABLED = _env_bool_first(
    "INSIDER_RISK_GUARD_ENABLED", "PAPER_INSIDER_RISK_GUARD_ENABLED", default="true"
)
INSIDER_RISK_BUBBLE_SUPPRESS = float(os.getenv("INSIDER_RISK_BUBBLE_SUPPRESS", "85"))
# Opening Range Breakout scanner — paper only (yfinance intraday on demand)
ORB_ENABLED = _env_bool_first("ORB_ENABLED", "PAPER_ORB_ENABLED", default="true")
ORB_BREAKOUT_MINUTES = int(os.getenv("ORB_BREAKOUT_MINUTES", "30"))
ORB_RVOL_MIN = float(os.getenv("ORB_RVOL_MIN", "2.0"))
ORB_BOOST_FACTOR = float(os.getenv("ORB_BOOST_FACTOR", "0.18"))
# RVOL + ORB momentum sleeve — paper-first; live opt-in for small (~$300) book
ORB_MOMENTUM_ENABLED = _env_bool_first(
    "ORB_MOMENTUM_ENABLED", "PAPER_ORB_MOMENTUM_ENABLED", default="true"
)
ORB_MOMENTUM_LIVE_SLEEVE = _parse_env_bool("ORB_MOMENTUM_LIVE_SLEEVE", default="false")
ORB_MOMENTUM_RISK_PCT = float(os.getenv("ORB_MOMENTUM_RISK_PCT", "0.01"))
ORB_MOMENTUM_MIN_SIZE_PCT = float(os.getenv("ORB_MOMENTUM_MIN_SIZE_PCT", "0.05"))
ORB_MOMENTUM_MAX_SIZE_PCT = float(os.getenv("ORB_MOMENTUM_MAX_SIZE_PCT", "0.10"))
ORB_MOMENTUM_RR = float(os.getenv("ORB_MOMENTUM_RR", "1.5"))
ORB_MOMENTUM_ATR_MULT = float(
    os.getenv("ORB_MOMENTUM_ATR_MULT", os.getenv("ATR_RISK_MULTIPLE", "2.0"))
)
ORB_MOMENTUM_CAP_PCT = float(os.getenv("ORB_MOMENTUM_CAP_PCT", "0.15"))
ORB_MOMENTUM_LIVE_CAP_PCT = float(os.getenv("ORB_MOMENTUM_LIVE_CAP_PCT", "0.05"))
ORB_MOMENTUM_MAX_POSITIONS = int(os.getenv("ORB_MOMENTUM_MAX_POSITIONS", "3"))
ORB_MOMENTUM_LIVE_MAX_POSITIONS = int(os.getenv("ORB_MOMENTUM_LIVE_MAX_POSITIONS", "1"))
ORB_MOMENTUM_MIN_CONVICTION = float(os.getenv("ORB_MOMENTUM_MIN_CONVICTION", "0.45"))
ORB_MOMENTUM_BACKTEST_ENABLED = _parse_env_bool(
    "ORB_MOMENTUM_BACKTEST_ENABLED", default="true"
)
ORB_MOMENTUM_STATE_FILE = os.getenv(
    "ORB_MOMENTUM_STATE_FILE", "data/orb_momentum_state.json"
)
# ATR volatility breakout — paper only (ATR expansion + RVOL + MTF)
VOL_BREAKOUT_ENABLED = _env_bool_first(
    "VOL_BREAKOUT_ENABLED", "PAPER_VOL_BREAKOUT_ENABLED", default="true"
)
PAPER_VOL_BREAKOUT_ENABLED = _parse_env_bool(
    "PAPER_VOL_BREAKOUT_ENABLED", default="true"
)
VOL_BREAKOUT_RISK_PCT = float(os.getenv("VOL_BREAKOUT_RISK_PCT", "0.01"))  # max 1%
VOL_BREAKOUT_MIN_SIZE_PCT = float(os.getenv("VOL_BREAKOUT_MIN_SIZE_PCT", "0.03"))
VOL_BREAKOUT_MAX_SIZE_PCT = float(os.getenv("VOL_BREAKOUT_MAX_SIZE_PCT", "0.08"))
VOL_BREAKOUT_RR = float(os.getenv("VOL_BREAKOUT_RR", "1.5"))
VOL_BREAKOUT_ATR_MULT = float(
    os.getenv("VOL_BREAKOUT_ATR_MULT", os.getenv("ATR_RISK_MULTIPLE", "2.0"))
)
VOL_BREAKOUT_ATR_EXPAND_MULT = float(os.getenv("VOL_BREAKOUT_ATR_EXPAND_MULT", "1.5"))
VOL_BREAKOUT_ATR_BASELINE_BARS = int(os.getenv("VOL_BREAKOUT_ATR_BASELINE_BARS", "20"))
VOL_BREAKOUT_BREAKOUT_LOOKBACK = int(os.getenv("VOL_BREAKOUT_BREAKOUT_LOOKBACK", "20"))
VOL_BREAKOUT_RVOL_MIN = float(
    os.getenv("VOL_BREAKOUT_RVOL_MIN", os.getenv("RVOL_MIN_THRESHOLD", "2.0"))
)
VOL_BREAKOUT_CAP_PCT = float(os.getenv("VOL_BREAKOUT_CAP_PCT", "0.12"))
VOL_BREAKOUT_MAX_POSITIONS = int(os.getenv("VOL_BREAKOUT_MAX_POSITIONS", "3"))
VOL_BREAKOUT_MIN_CONVICTION = float(os.getenv("VOL_BREAKOUT_MIN_CONVICTION", "0.45"))
VOL_BREAKOUT_BACKTEST_ENABLED = _parse_env_bool(
    "VOL_BREAKOUT_BACKTEST_ENABLED", default="true"
)
VOL_BREAKOUT_STATE_FILE = os.getenv(
    "VOL_BREAKOUT_STATE_FILE", "data/vol_breakout_state.json"
)
# Catalyst scoring — paper only (news + insider + RVOL + ORB + thinking)
CATALYST_SCORING_ENABLED = _env_bool_first(
    "CATALYST_SCORING_ENABLED", "PAPER_CATALYST_SCORING_ENABLED", default="true"
)
CATALYST_MIN_SCORE = float(os.getenv("CATALYST_MIN_SCORE", "65"))
CATALYST_BOOST_FACTOR = float(os.getenv("CATALYST_BOOST_FACTOR", "0.20"))
# Historical news simulation — backtest only (catalyst + thinking proxies)
HISTORICAL_NEWS_ENABLED = os.getenv("HISTORICAL_NEWS_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
HISTORICAL_NEWS_CACHE_DIR = os.getenv(
    "HISTORICAL_NEWS_CACHE_DIR", "data/historical_news_cache"
)
# Aggressively drop low-signal simulated headlines below this relevance score (0-100).
HISTORICAL_NEWS_MIN_RELEVANCE = int(os.getenv("HISTORICAL_NEWS_MIN_RELEVANCE", "50"))
STRATEGY_METRICS_DB = os.getenv("STRATEGY_METRICS_DB", "data/strategy_metrics.db")
# ATR position sizing — paper only
ATR_SIZING_ENABLED = _env_bool_first(
    "ATR_SIZING_ENABLED", "PAPER_ATR_SIZING_ENABLED", default="true"
)
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_RISK_MULTIPLE = float(os.getenv("ATR_RISK_MULTIPLE", "2.0"))
ATR_MAX_SIZE_PCT = float(os.getenv("ATR_MAX_SIZE_PCT", "0.04"))
# Conviction-based position sizing — paper only (scales risk by signal strength)
CONVICTION_SIZING_ENABLED = _env_bool_first(
    "CONVICTION_SIZING_ENABLED", "PAPER_CONVICTION_SIZING_ENABLED", default="true"
)
CONVICTION_MIN_SCALE = float(os.getenv("CONVICTION_MIN_SCALE", "0.4"))
# v1.5.1 tune: raise top-signal ceiling 1.8x -> 2.0x (scanner/insider weights also up).
CONVICTION_MAX_SCALE = float(os.getenv("CONVICTION_MAX_SCALE", "2.0"))
CONVICTION_METRICS_FILE = os.getenv(
    "CONVICTION_METRICS_FILE", "data/conviction_metrics.json"
)
# Multi-timeframe trend confirmation — paper only
MULTI_TIMEFRAME_ENABLED = _env_bool_first(
    "MULTI_TIMEFRAME_ENABLED", "PAPER_MULTI_TIMEFRAME_ENABLED", default="true"
)
MULTI_TIMEFRAME_MIN_ALIGNMENT = float(os.getenv("MULTI_TIMEFRAME_MIN_ALIGNMENT", "0.65"))
MULTI_TIMEFRAME_BOOST_FACTOR = float(os.getenv("MULTI_TIMEFRAME_BOOST_FACTOR", "0.22"))
# Exit optimization — paper only (partial @1R, dynamic trail, time exit)
EXIT_OPTIMIZATION_ENABLED = _env_bool_first(
    "EXIT_OPTIMIZATION_ENABLED", "PAPER_EXIT_OPTIMIZATION_ENABLED", default="true"
)
PARTIAL_EXIT_RR = float(os.getenv("PARTIAL_EXIT_RR", "1.0"))
TRAIL_ARM_PCT = float(os.getenv("TRAIL_ARM_PCT", "0.50"))
TRAIL_PULLBACK_PCT = float(os.getenv("TRAIL_PULLBACK_PCT", "0.35"))
EXIT_OPTIMIZATION_MAX_HOLD_BARS = int(os.getenv("EXIT_OPTIMIZATION_MAX_HOLD_BARS", "35"))
EXIT_EVENTS_FILE = os.getenv("EXIT_EVENTS_FILE", "data/exit_events.json")
# Portfolio correlation guard — paper only (reduces sizing when holdings too correlated)
CORRELATION_GUARD_ENABLED = _env_bool_first(
    "CORRELATION_GUARD_ENABLED", "PAPER_CORRELATION_GUARD_ENABLED", default="true"
)
MAX_PORTFOLIO_CORR = float(os.getenv("MAX_PORTFOLIO_CORR", "0.65"))
CORR_GUARD_MIN_SCALE = float(os.getenv("CORR_GUARD_MIN_SCALE", "0.60"))
CORR_GUARD_CEILING = float(os.getenv("CORR_GUARD_CEILING", "0.85"))
CORRELATION_METRICS_FILE = os.getenv(
    "CORRELATION_METRICS_FILE", "data/correlation_guard.json"
)
# Relative volume scanner — paper only (yfinance volume on demand)
RVOL_SCANNER_ENABLED = _env_bool_first(
    "RVOL_SCANNER_ENABLED", "PAPER_RVOL_SCANNER_ENABLED", default="true"
)
RVOL_MIN_THRESHOLD = float(os.getenv("RVOL_MIN_THRESHOLD", "2.0"))
RVOL_STRONG_THRESHOLD = float(os.getenv("RVOL_STRONG_THRESHOLD", "3.0"))
RVOL_BOOST_FACTOR = float(os.getenv("RVOL_BOOST_FACTOR", "0.15"))
RVOL_MOMENTUM_BOOST_THRESHOLD = float(os.getenv("RVOL_MOMENTUM_BOOST_THRESHOLD", "2.5"))
RVOL_LOOKBACK_DAYS = int(os.getenv("RVOL_LOOKBACK_DAYS", "10"))
SHORT_PROFIT_TARGET_PCT = float(os.getenv("SHORT_PROFIT_TARGET_PCT", "0.032"))
SHORT_STOP_LOSS_PCT = float(os.getenv("SHORT_STOP_LOSS_PCT", "0.02"))
SHORT_MIN_HOLD_BARS = int(os.getenv("SHORT_MIN_HOLD_BARS", "4"))
SHORT_MA_EXIT_BUFFER = float(os.getenv("SHORT_MA_EXIT_BUFFER", "0.008"))
SHORT_TIME_EXIT_BARS = int(os.getenv("SHORT_TIME_EXIT_BARS", "30"))
SHORT_MAX_HOLD_BARS = int(os.getenv("SHORT_MAX_HOLD_BARS", "30"))
SHORT_EXIT_MIN_Z = float(os.getenv("SHORT_EXIT_MIN_Z", "0.8"))
SHORT_TRAILING_ARM_FRAC = float(os.getenv("SHORT_TRAILING_ARM_FRAC", "0.50"))
SHORT_TRAILING_PULLBACK_FRAC = float(os.getenv("SHORT_TRAILING_PULLBACK_FRAC", "0.35"))
SHORT_PARTIAL_PROFIT_ENABLED = _env_bool_first(
    "SHORT_PARTIAL_PROFIT_ENABLED", "PAPER_SHORT_PARTIAL_PROFIT_ENABLED", default="true"
)
SHORT_PARTIAL_PROFIT_FRAC = float(os.getenv("SHORT_PARTIAL_PROFIT_FRAC", "0.50"))
SHORT_PARTIAL_PROFIT_RR = float(os.getenv("SHORT_PARTIAL_PROFIT_RR", "1.0"))
SHORT_HIGH_VOL_VIX_THRESHOLD = float(os.getenv("SHORT_HIGH_VOL_VIX_THRESHOLD", "25"))
SHORT_HIGH_VOL_STOP_MULT = float(os.getenv("SHORT_HIGH_VOL_STOP_MULT", "0.75"))
SHORT_RHYME_E_BEAR_STREAK_BARS = int(os.getenv("SHORT_RHYME_E_BEAR_STREAK_BARS", "3"))
SHORT_RHYME_E_BEAR_STREAK_VIX_WAIVER = float(
    os.getenv("SHORT_RHYME_E_BEAR_STREAK_VIX_WAIVER", "28")
)
SHORT_RHYME_E_WAIVER_MIN_STREAK = int(os.getenv("SHORT_RHYME_E_WAIVER_MIN_STREAK", "2"))
SHORT_WAIVER_SIZE_MULT = float(os.getenv("SHORT_WAIVER_SIZE_MULT", "0.75"))
SHORT_BUBBLE_SIZE_POWER = float(os.getenv("SHORT_BUBBLE_SIZE_POWER", "1.35"))
SHORT_REGIME_B_SIZE_MULT = float(os.getenv("SHORT_REGIME_B_SIZE_MULT", "1.10"))
SHORT_REGIME_E_SIZE_MULT = float(os.getenv("SHORT_REGIME_E_SIZE_MULT", "0.85"))
SHORT_VOL_SIZE_FLOOR = float(os.getenv("SHORT_VOL_SIZE_FLOOR", "0.55"))
SHORT_LONG_HEDGE_ENABLED = _env_bool_first(
    "SHORT_LONG_HEDGE_ENABLED", "PAPER_SHORT_LONG_HEDGE_ENABLED", default="true"
)
SHORT_LONG_HEDGE_FLOOR = float(os.getenv("SHORT_LONG_HEDGE_FLOOR", "0.78"))
# Local LLM thinking engine (Ollama) — system-wide, configurable per book.
# Defaults: ON for paper/research, OFF for live (opt-in via LIVE_THINKING_ENGINE_ENABLED).
# Run scripts/setup_ollama.py first. Live tilts still require approval when THINKING_MANUAL_APPROVAL_LIVE=true.
THINKING_ENGINE_ENABLED = _parse_env_bool("THINKING_ENGINE_ENABLED", default="true")
PAPER_THINKING_ENGINE_ENABLED = _env_bool_first(
    "PAPER_THINKING_ENGINE_ENABLED", "THINKING_ENGINE_ENABLED", default="true"
)
LIVE_THINKING_ENGINE_ENABLED = _parse_env_bool(
    "LIVE_THINKING_ENGINE_ENABLED", default="false"
)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:32b")
OLLAMA_FALLBACK_MODELS = os.getenv(
    "OLLAMA_FALLBACK_MODELS", "qwen2.5-coder:14b,deepseek-r1:8b,llama3.1:8b"
)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_TIMEOUT_SEC = int(os.getenv("OLLAMA_TIMEOUT_SEC", "1200"))
# Structured thinking / analyzer calls: fail-fast then heuristic (max 3 attempts).
OLLAMA_THINKING_TIMEOUT_SEC = int(os.getenv("OLLAMA_THINKING_TIMEOUT_SEC", "90"))
OLLAMA_RETRY_COUNT = int(os.getenv("OLLAMA_RETRY_COUNT", "3"))
OLLAMA_USE_CHAT_API = os.getenv("OLLAMA_USE_CHAT_API", "true").lower() in (
    "1",
    "true",
    "yes",
)
OLLAMA_JSON_FORMAT = os.getenv("OLLAMA_JSON_FORMAT", "true").lower() in (
    "1",
    "true",
    "yes",
)
THINKING_CACHE_HOURS = int(os.getenv("THINKING_CACHE_HOURS", "24"))
THINKING_ENGINE_STATE_FILE = os.getenv("THINKING_ENGINE_STATE_FILE", "thinking_engine_state.json")
THINKING_ENGINE_OUTPUT_FILE = os.getenv("THINKING_ENGINE_OUTPUT_FILE", "thinking_engine_last.json")
THINKING_APPROVAL_FILE = os.getenv("THINKING_APPROVAL_FILE", "thinking_engine_approval.json")
# Optional Moonshot/Kimi cloud deep thinker — daily only; off by default (cost + latency).
KIMI_API_ENABLED = _env_bool_first("KIMI_API_ENABLED", default="false")
# Moonshot direct or NVIDIA NIM (nvapi-...); NVIDIA_API_KEY is an alias for KIMI_API_KEY
KIMI_API_KEY = os.getenv("KIMI_API_KEY", "") or os.getenv("NVIDIA_API_KEY", "")
KIMI_MODEL = os.getenv("KIMI_MODEL", "kimi-latest")
KIMI_TEMPERATURE = float(os.getenv("KIMI_TEMPERATURE", "0.3"))
KIMI_MAX_TOKENS = int(os.getenv("KIMI_MAX_TOKENS", "4096"))
KIMI_TOP_P = os.getenv("KIMI_TOP_P")  # optional; e.g. 1.0 for NVIDIA NIM
KIMI_DAILY_THINK = _env_bool_first("KIMI_DAILY_THINK", default="true")
# Moonshot: https://api.moonshot.ai/v1 | NVIDIA NIM: https://integrate.api.nvidia.com/v1
KIMI_API_BASE_URL = os.getenv("KIMI_API_BASE_URL", "https://api.moonshot.ai/v1")
KIMI_TIMEOUT_SEC = int(os.getenv("KIMI_TIMEOUT_SEC", "120"))
KIMI_MAX_RETRIES = int(os.getenv("KIMI_MAX_RETRIES", "3"))
KIMI_STATE_FILE = os.getenv("KIMI_STATE_FILE", "data/kimi_daily_state.json")
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
# Base NYSE momentum sleeve; paper/research uses effective_nyse_sleeve_cap_pct() (0.18–0.22).
NYSE_SLEEVE_CAP_PCT = float(os.getenv("NYSE_SLEEVE_CAP_PCT", "0.20"))
# Dedicated stat-arb sleeve (paper/research) — independent of NYSE momentum cap.
STAT_ARB_SLEEVE_CAP_PCT = float(os.getenv("STAT_ARB_SLEEVE_CAP_PCT", "0.07"))
STAT_ARB_SLEEVE_CAP_ENABLED = _env_bool_first(
    "STAT_ARB_SLEEVE_CAP_ENABLED", default="true"
)
STAT_ARB_VOL_SCALING_ENABLED = _env_bool_first(
    "STAT_ARB_VOL_SCALING_ENABLED", default="true"
)
STAT_ARB_VOL_MIN_NOTIONAL_SCALE = float(
    os.getenv("STAT_ARB_VOL_MIN_NOTIONAL_SCALE", "0.30")
)
# Portfolio constructor stat-arb cap tilt bounds (see PORTFOLIO_CONSTRUCTOR_ENABLED above).
PORTFOLIO_STAT_ARB_MULT_FLOOR = float(os.getenv("PORTFOLIO_STAT_ARB_MULT_FLOOR", "0.75"))
PORTFOLIO_STAT_ARB_MULT_CEILING = float(os.getenv("PORTFOLIO_STAT_ARB_MULT_CEILING", "1.25"))
# NYSE picks vs SPY sleeve: skip high-beta / high-corr names when SPY is active
_nyse_overlap_env = os.getenv("NYSE_OVERLAP_FILTER_ENABLED") or os.getenv(
    "NYSE_ANTI_OVERLAP_ENABLED", "false"
)
NYSE_OVERLAP_FILTER_ENABLED = _nyse_overlap_env.lower() in ("1", "true", "yes")
NYSE_ANTI_OVERLAP_ENABLED = NYSE_OVERLAP_FILTER_ENABLED
NYSE_SPY_CORR_MAX = float(os.getenv("NYSE_SPY_CORR_MAX", "0.80"))
NYSE_SPY_BETA_MAX = float(os.getenv("NYSE_SPY_BETA_MAX", "1.6"))
NYSE_SPY_CORR_LOOKBACK = int(os.getenv("NYSE_SPY_CORR_LOOKBACK", "60"))
# Paper aggressive: stricter NYSE filter when SPY sleeve is full or bullish (live: always off)
NYSE_CONDITIONAL_ON_SPY = os.getenv("NYSE_CONDITIONAL_ON_SPY", "true").lower() in (
    "1",
    "true",
    "yes",
)
NYSE_CONDITIONAL_SPY_CORR_MAX = float(os.getenv("NYSE_CONDITIONAL_SPY_CORR_MAX", "0.78"))
NYSE_CONDITIONAL_SPY_CAP_FILL_PCT = float(
    os.getenv("NYSE_CONDITIONAL_SPY_CAP_FILL_PCT", "0.50")
)
PAPER_NYSE_CONDITIONAL_ON_SPY = os.getenv("PAPER_NYSE_CONDITIONAL_ON_SPY", "true").lower() in (
    "1",
    "true",
    "yes",
)
NYSE_BETA_SCALING_ENABLED = os.getenv("NYSE_BETA_SCALING_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Max Tech names in top-3 momentum when SPY on (0 = disabled; 1 = sector test variant)
NYSE_SECTOR_TECH_CAP = int(os.getenv("NYSE_SECTOR_TECH_CAP", "0"))
CRYPTO_SLEEVE_CAP_PCT = 0.20
CRYPTO_VOL_ONLY = True  # crypto pairs only when cross-asset volatility is High

# --- SPY / NYSE sleeve settings (live Profile A defaults) ---
SPY_BOT_SYMBOL = "SPY"
SPY_MA_WINDOW = int(os.getenv("SPY_MA_WINDOW", "200"))
NYSE_MA_WINDOW = int(os.getenv("NYSE_MA_WINDOW", "50"))
# Paper-aggressive tuned defaults (tail-risk tuning Option A, 2026-06).
PAPER_SPY_MA_WINDOW = int(os.getenv("PAPER_SPY_MA_WINDOW", "160"))
PAPER_NYSE_MA_WINDOW = int(os.getenv("PAPER_NYSE_MA_WINDOW", "70"))
# v1.5.1 NYSE momentum tune (paper only): reduce drag, lift meaningful activity.
#   - Entry tolerance: allow entries within this % below MA (0.01 = 1% band) for flexibility.
#   - Rank scanner weight: amplify RVOL/ORB/Catalyst rank boosts in momentum ranking.
#   - Entry RVOL floor: slightly relaxed vs scanner floor for NYSE entries.
NYSE_MA_ENTRY_TOLERANCE_PCT = float(os.getenv("NYSE_MA_ENTRY_TOLERANCE_PCT", "0.01"))
NYSE_RANK_SCANNER_WEIGHT = float(os.getenv("NYSE_RANK_SCANNER_WEIGHT", "1.35"))
NYSE_ENTRY_RVOL_MIN = float(os.getenv("NYSE_ENTRY_RVOL_MIN", "1.8"))
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

# --- Real-time Alpaca WebSocket (modules/real_time_data.py) ---
# Default on for paper, off for live — set REAL_TIME_WEBSOCKET_ENABLED explicitly to override.
REAL_TIME_FLUSH_SEC = int(os.getenv("REAL_TIME_FLUSH_SEC", "5"))

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
# Backtest / daily bars: min bars halted before auto-resume (wall-clock ignored when > 0).
HALT_MIN_BARS = int(os.getenv("HALT_MIN_BARS", "3"))
HALT_LIQUIDATE_ON_BREACH = os.getenv("HALT_LIQUIDATE_ON_BREACH", "true").lower() in (
    "1",
    "true",
    "yes",
)
HALT_TARGET_CASH_PCT = float(os.getenv("HALT_TARGET_CASH_PCT", "0.25"))
BACKTEST_DAYS = 365

# --- Sentiment (regime input) — "price" is free and matches backtests ---
SENTIMENT_SOURCE = os.getenv("SENTIMENT_SOURCE", "price").strip().lower()
# Applied to normalized sentiment in [-1, 1] (see normalize_regime_sentiment).
# 0.12 balances responsiveness vs excessive RHYME_E; legacy 0.5 never fired on daily.
REGIME_SENTIMENT_THRESHOLD = float(os.getenv("REGIME_SENTIMENT_THRESHOLD", "0.12"))
# |sentiment| at or below this is treated as raw price momentum (not web blend).
REGIME_RAW_SENTIMENT_MAX = float(os.getenv("REGIME_RAW_SENTIMENT_MAX", "0.25"))
# Vol High/Low cutoffs — 5m cross-asset stdev is much smaller than daily.
REGIME_VOL_THRESHOLD_5M = float(os.getenv("REGIME_VOL_THRESHOLD_5M", "0.008"))
REGIME_VOL_THRESHOLD_DAILY = float(os.getenv("REGIME_VOL_THRESHOLD_DAILY", "0.02"))
REGIME_DAILY_LOOKBACK_DAYS = int(os.getenv("REGIME_DAILY_LOOKBACK_DAYS", "120"))
REGIME_HYSTERESIS_ENABLED = os.getenv("REGIME_HYSTERESIS_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
REGIME_MIN_DWELL_SEC = int(os.getenv("REGIME_MIN_DWELL_SEC", "3600"))
# Stronger sentiment required to flip RHYME quadrant when already in a regime.
REGIME_HYSTERESIS_SENTIMENT_BUMP = float(
    os.getenv("REGIME_HYSTERESIS_SENTIMENT_BUMP", "0.05")
)
# Daily backtest / bar clock: min bars before regime label can change (smoother flips).
REGIME_MIN_DWELL_BARS = int(os.getenv("REGIME_MIN_DWELL_BARS", "8"))
# Dynamic per-regime sizing (paper aggressive) — replaces hard PAUSED_REGIMES blocks.
PAPER_REGIME_DYNAMIC_SIZING_ENABLED = os.getenv(
    "PAPER_REGIME_DYNAMIC_SIZING_ENABLED", "true"
).lower() in ("1", "true", "yes")
PAPER_REGIME_A_SIZING_MULT = float(os.getenv("PAPER_REGIME_A_SIZING_MULT", "1.2"))
PAPER_REGIME_B_SIZING_MULT = float(os.getenv("PAPER_REGIME_B_SIZING_MULT", "0.30"))
PAPER_REGIME_C_SIZING_MULT = float(os.getenv("PAPER_REGIME_C_SIZING_MULT", "1.0"))
PAPER_REGIME_D_SIZING_MULT = float(os.getenv("PAPER_REGIME_D_SIZING_MULT", "0.7"))
PAPER_REGIME_E_SIZING_MULT = float(os.getenv("PAPER_REGIME_E_SIZING_MULT", "1.60"))
# Weak RHYME (B/D/E): per-sleeve exposure ceiling as fraction of equity.
PAPER_REGIME_WEAK_SLEEVE_MAX_PCT = float(os.getenv("PAPER_REGIME_WEAK_SLEEVE_MAX_PCT", "0.25"))
# COT / positioning overlay — off by default (experiment: neutral-negative); opt-in via COT_OVERLAY_ENABLED
POSITIONING_OVERLAY_ENABLED = _env_bool_first(
    "POSITIONING_OVERLAY_ENABLED", "COT_OVERLAY_ENABLED", default="false"
)
POSITIONING_OVERLAY_LIVE_ENABLED = os.getenv(
    "POSITIONING_OVERLAY_LIVE_ENABLED", "false"
).lower() in ("1", "true", "yes")
COT_DATA_FILE = os.getenv("COT_DATA_FILE", "reference/cot_es.json")
COT_API_ENABLED = os.getenv("COT_API_ENABLED", "false").lower() in ("1", "true", "yes")
COT_CONTRACT_LABEL = os.getenv("COT_CONTRACT_LABEL", "E-mini S&P 500")
# Large-spec net as fraction of open interest (negative = net short).
COT_NET_SHORT_THRESH = float(os.getenv("COT_NET_SHORT_THRESH", "-0.08"))
COT_NET_LONG_THRESH = float(os.getenv("COT_NET_LONG_THRESH", "0.08"))
COT_EXTREME_SHORT_THRESH = float(os.getenv("COT_EXTREME_SHORT_THRESH", "-0.15"))
COT_EXTREME_LONG_THRESH = float(os.getenv("COT_EXTREME_LONG_THRESH", "0.15"))
COT_BULLISH_MULT = float(os.getenv("COT_BULLISH_MULT", "1.2"))
COT_BULLISH_EXTREME_MULT = float(os.getenv("COT_BULLISH_EXTREME_MULT", "1.4"))
COT_BEARISH_MULT = float(os.getenv("COT_BEARISH_MULT", "0.8"))
COT_BEARISH_EXTREME_MULT = float(os.getenv("COT_BEARISH_EXTREME_MULT", "0.6"))
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
    os.getenv("SENTIMENT_GAP_THRESHOLD_DEFENSIVE", "0.50")
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

# --- Operating Layer + Wisdom Layer (strategic rebalance) ---
REBALANCE_ENABLED = _env_bool_first(
    "REBALANCE_ENABLED", "WISDOM_LAYER_ENABLED", default="false"
)
REBALANCE_CORE_TARGET = float(os.getenv("REBALANCE_CORE_TARGET", "0.80"))
REBALANCE_BAND_WIDTH = float(os.getenv("REBALANCE_BAND_WIDTH", "0.08"))
REBALANCE_CORE_MIN = float(os.getenv("REBALANCE_CORE_MIN", "0.70"))
REBALANCE_CORE_MAX = float(os.getenv("REBALANCE_CORE_MAX", "0.90"))
REBALANCE_TACTICAL_MIN = float(os.getenv("REBALANCE_TACTICAL_MIN", "0.10"))
REBALANCE_TACTICAL_MAX = float(os.getenv("REBALANCE_TACTICAL_MAX", "0.30"))
REBALANCE_CASH_MIN = float(os.getenv("REBALANCE_CASH_MIN", "0.05"))
REBALANCE_CASH_MAX = float(os.getenv("REBALANCE_CASH_MAX", "0.15"))
WISDOM_CONVICTION_THRESHOLD = float(os.getenv("WISDOM_CONVICTION_THRESHOLD", "0.75"))
WISDOM_MAX_CORE_SHIFT_PCT = float(os.getenv("WISDOM_MAX_CORE_SHIFT_PCT", "0.10"))
WISDOM_LOG_FILE = os.getenv("WISDOM_LOG_FILE", "logs/wisdom_log.jsonl")

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
# Base risk when dynamic paper risk is off; live Profile A default 2%.
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.018"))
MAX_NOTIONAL_PER_ORDER = float(os.getenv("MAX_NOTIONAL_PER_ORDER", "10000"))
# Small live account safety (< threshold equity): conservative sizing + higher VTI core
SMALL_ACCOUNT_EQUITY_THRESHOLD = float(
    os.getenv("SMALL_ACCOUNT_EQUITY_THRESHOLD", "500")
)
SMALL_ACCOUNT_RISK_PER_TRADE = float(os.getenv("SMALL_ACCOUNT_RISK_PER_TRADE", "0.01"))
SMALL_ACCOUNT_MAX_NOTIONAL = float(os.getenv("SMALL_ACCOUNT_MAX_NOTIONAL", "10"))
# Live conservative profile (alpaca_live small account): 85% VTI + 5% active sleeve winner.
LIVE_VTI_CORE_PCT = float(os.getenv("LIVE_VTI_CORE_PCT", "0.85"))
LIVE_SMALL_ACTIVE_SLEEVE_PCT = float(os.getenv("LIVE_SMALL_ACTIVE_SLEEVE_PCT", "0.05"))
# v1.1c 365d evidence: SPY MA200 best risk-adjusted live sleeve (PF 114, 98% win vs NYSE unrealized).
LIVE_ACTIVE_SLEEVE_CHOICE = os.getenv("LIVE_ACTIVE_SLEEVE_CHOICE", "spy").strip().lower()
LIVE_CONSERVATIVE_ENABLED = _env_bool_first("LIVE_CONSERVATIVE_ENABLED", default="true")
LIVE_CONSERVATIVE_PROFILE: dict[str, float | str] = {
    "vti_core_pct": LIVE_VTI_CORE_PCT,
    "active_sleeve_pct": LIVE_SMALL_ACTIVE_SLEEVE_PCT,
    "active_sleeve": LIVE_ACTIVE_SLEEVE_CHOICE,
    "risk_per_trade": SMALL_ACCOUNT_RISK_PER_TRADE,
    "max_notional_per_order": SMALL_ACCOUNT_MAX_NOTIONAL,
}
SMALL_ACCOUNT_VTI_CORE_PCT = float(
    os.getenv("SMALL_ACCOUNT_VTI_CORE_PCT", str(LIVE_VTI_CORE_PCT))
)
SMALL_ACCOUNT_BACKTEST_EQUITY = float(
    os.getenv("SMALL_ACCOUNT_BACKTEST_EQUITY", "100")
)

_account_equity: float | None = None
_account_cash: float | None = None
_small_account_mode = False
_backtest_small_account_ctx = False
_backtest_live_conservative_ctx = False
_backtest_paper_sleeves_ctx = False
_backtest_vti_ceiling: float | None = None
_backtest_strict_pit_ctx = False
_live_thinking_sim_ctx = False
_dynamic_risk_ctx: dict = {
    "vol_score": 0.02,
    "regime": "",
    "macro_stress": False,
    "drawdown": 0.0,
    "recovery_mode": False,
    "equity_history": [],
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


def reload_from_env(env_file: str | None = None, *, book_scoped: bool = False) -> None:
    """Reload credentials flags after portal switches per-user .env."""
    if env_file and os.path.isfile(env_file):
        load_dotenv(env_file, override=True)
    else:
        _load_project_dotenv()
    _normalize_alpaca_env_keys()
    _sync_trading_mode_flags(skip_root_override=book_scoped)
    try:
        from modules.alpaca_client import reset_trading_client_cache

        reset_trading_client_cache()
    except ImportError:
        pass


def get_paper_alpaca_credentials() -> tuple[str, str]:
    """Paper book keys — prefer PAPER_APCA_*, fallback to APCA_* for single-key setups."""
    key = _strip_env(os.getenv("PAPER_APCA_API_KEY_ID"))
    secret = _strip_env(os.getenv("PAPER_APCA_API_SECRET_KEY"))
    if key and secret:
        return key, secret
    key = _strip_env(os.getenv("APCA_API_KEY_ID")) or _strip_env(os.getenv("ALPACA_API_KEY"))
    secret = _strip_env(os.getenv("APCA_API_SECRET_KEY")) or _strip_env(
        os.getenv("ALPACA_SECRET_KEY")
    )
    if not key or not secret:
        raise ValueError(
            "Paper Alpaca credentials missing. Add to .env (never commit):\n"
            "  PAPER_APCA_API_KEY_ID=your_paper_key_id\n"
            "  PAPER_APCA_API_SECRET_KEY=your_paper_secret\n"
            "  PAPER_TRADING=true"
        )
    return key, secret


def get_live_alpaca_credentials() -> tuple[str, str]:
    """Live book keys — APCA_* only (never PAPER_APCA_*)."""
    key = _strip_env(os.getenv("APCA_API_KEY_ID")) or _strip_env(os.getenv("ALPACA_API_KEY"))
    secret = _strip_env(os.getenv("APCA_API_SECRET_KEY")) or _strip_env(
        os.getenv("ALPACA_SECRET_KEY")
    )
    if not key or not secret:
        raise ValueError(
            "Live Alpaca credentials missing. Add to .env (never commit):\n"
            "  APCA_API_KEY_ID=your_live_key_id\n"
            "  APCA_API_SECRET_KEY=your_live_secret\n"
            "  PAPER_TRADING=false\n"
            "  ALLOW_LIVE_TRADING=yes"
        )
    return key, secret


def alpaca_credentials_status(*, paper: bool | None = None) -> dict[str, str | bool]:
    """Safe credential diagnostics (suffix only — never full keys)."""
    use_paper = PAPER_TRADING if paper is None else bool(paper)
    if use_paper:
        pk = _strip_env(os.getenv("PAPER_APCA_API_KEY_ID"))
        ps = _strip_env(os.getenv("PAPER_APCA_API_SECRET_KEY"))
        if pk and ps:
            key, secret, source = pk, ps, "PAPER_APCA_*"
        else:
            key = _strip_env(os.getenv("APCA_API_KEY_ID")) or _strip_env(
                os.getenv("ALPACA_API_KEY")
            )
            secret = _strip_env(os.getenv("APCA_API_SECRET_KEY")) or _strip_env(
                os.getenv("ALPACA_SECRET_KEY")
            )
            source = "APCA_* (paper fallback)"
    else:
        key = _strip_env(os.getenv("APCA_API_KEY_ID")) or _strip_env(
            os.getenv("ALPACA_API_KEY")
        )
        secret = _strip_env(os.getenv("APCA_API_SECRET_KEY")) or _strip_env(
            os.getenv("ALPACA_SECRET_KEY")
        )
        source = "APCA_* (live)"
    return {
        "paper": use_paper,
        "mode": "PAPER" if use_paper else "LIVE",
        "base_url": get_alpaca_base_url(paper=use_paper),
        "key_source": source,
        "key_suffix": key[-4:] if len(key) >= 4 else "????",
        "has_key": bool(key),
        "has_secret": bool(secret),
        "loaded_env": os.getenv("PYTHONTRADING_LOADED_ENV", "(unknown)"),
        "env_override": os.getenv("PYTHONTRADING_ENV_FILE", ""),
    }


def get_alpaca_credentials(*, paper: bool | None = None) -> tuple[str, str]:
    """Return (api_key, secret_key) for paper or live based on PAPER_TRADING."""
    use_paper = PAPER_TRADING if paper is None else bool(paper)
    if use_paper:
        return get_paper_alpaca_credentials()
    return get_live_alpaca_credentials()


def get_alpaca_base_url(*, paper: bool | None = None) -> str:
    """Return Alpaca REST base URL for paper or live trading."""
    use_paper = PAPER_TRADING if paper is None else bool(paper)
    if use_paper:
        return ALPACA_PAPER_BASE_URL
    override = _strip_env(os.getenv("APCA_API_BASE_URL"))
    if override:
        return override.rstrip("/")
    return ALPACA_LIVE_BASE_URL


def trading_mode_summary() -> dict[str, str | bool]:
    """Resolved trading mode for startup banners and diagnostics."""
    paper = bool(PAPER_TRADING)
    creds = alpaca_credentials_status(paper=paper)
    return {
        "paper": paper,
        "mode": "PAPER" if paper else "LIVE",
        "base_url": get_alpaca_base_url(),
        "allow_live": bool(ALLOW_LIVE_TRADING),
        "paper_env": os.getenv("PAPER_TRADING", "(unset)"),
        "allow_live_env": os.getenv("ALLOW_LIVE_TRADING", "(unset)"),
        "key_source": creds["key_source"],
        "key_suffix": creds["key_suffix"],
        "loaded_env": creds["loaded_env"],
    }


def print_trading_mode_banner(*, stream=None) -> None:
    """Loud startup line showing effective Alpaca mode (after dotenv)."""
    import sys

    from modules.safe_io import ensure_stdio_streams, safe_print

    ensure_stdio_streams()
    out = stream or sys.stdout
    info = trading_mode_summary()
    bar = "=" * 60
    lines = [
        bar,
        f"  ALPACA MODE: {info['mode']}",
        f"  PAPER_TRADING={info['paper_env']}  ALLOW_LIVE_TRADING={info['allow_live_env']}",
        f"  API endpoint: {info['base_url']}",
        f"  Keys: {info['key_source']} (…{info['key_suffix']})",
    ]
    if not info["paper"] and not info["allow_live"]:
        lines.append("  WARNING: live mode blocked — set ALLOW_LIVE_TRADING=yes")
    lines.append(bar)
    for line in lines:
        safe_print(line, file=out)
    if getattr(sys, "frozen", False):
        log = logging.getLogger("config")
        for line in lines:
            log.info(line)


def validate_alpaca_config(*, require_credentials: bool = True) -> None:
    """Validate Alpaca env at startup; raise ValueError with setup instructions."""
    _sync_trading_mode_flags()
    use_paper = PAPER_TRADING
    if require_credentials:
        get_alpaca_credentials(paper=use_paper)
    if not use_paper and not ALLOW_LIVE_TRADING:
        raise ValueError(
            "Live trading blocked. Set PAPER_TRADING=true for paper keys, "
            "or set ALLOW_LIVE_TRADING=yes to acknowledge live risk."
        )
    base_url = get_alpaca_base_url(paper=use_paper)
    creds = alpaca_credentials_status(paper=use_paper)
    print_trading_mode_banner()
    logging.getLogger(__name__).info(
        "Alpaca config OK: paper=%s base_url=%s key_source=%s key_suffix=…%s loaded_env=%s",
        use_paper,
        base_url,
        creds["key_source"],
        creds["key_suffix"],
        creds["loaded_env"],
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


# --- Alert policy (high-signal Telegram/email; noisy topics off by default) ---
TELEGRAM_ALERT_HALT = _parse_env_bool("TELEGRAM_ALERT_HALT", default="true")
TELEGRAM_ALERT_DRAWDOWN_MAJOR = _parse_env_bool("TELEGRAM_ALERT_DRAWDOWN_MAJOR", default="true")
TELEGRAM_MAJOR_DRAWDOWN_PCT = float(os.getenv("TELEGRAM_MAJOR_DRAWDOWN_PCT", "0.05"))
TELEGRAM_ALERT_DAILY_SUMMARY = _parse_env_bool("TELEGRAM_ALERT_DAILY_SUMMARY", default="true")
TELEGRAM_DAILY_SUMMARY_TIME = os.getenv("TELEGRAM_DAILY_SUMMARY_TIME", "16:30").strip()
TELEGRAM_ALERT_YIELD_GATE = _parse_env_bool("TELEGRAM_ALERT_YIELD_GATE", default="true")
TELEGRAM_ALERT_PERIODIC_SUMMARY = _parse_env_bool(
    "TELEGRAM_ALERT_PERIODIC_SUMMARY", default="true"
)
TELEGRAM_PERIODIC_SUMMARY_HOURS = float(
    os.getenv("TELEGRAM_PERIODIC_SUMMARY_HOURS", "3")
)
# Live bot: one post-close Telegram summary per weekday after this ET time.
TELEGRAM_ALERT_LIVE_DAILY_SUMMARY = _parse_env_bool(
    "TELEGRAM_ALERT_LIVE_DAILY_SUMMARY", default="true"
)
TELEGRAM_LIVE_DAILY_SUMMARY_TIME = os.getenv(
    "TELEGRAM_LIVE_DAILY_SUMMARY_TIME", "16:00"
).strip()
TELEGRAM_ALERT_LIVE_FILLS = _parse_env_bool("TELEGRAM_ALERT_LIVE_FILLS", default="true")
TELEGRAM_LIVE_FILL_MIN_USD = float(os.getenv("TELEGRAM_LIVE_FILL_MIN_USD", "5"))
TELEGRAM_ALERT_SPACEX = _parse_env_bool("TELEGRAM_ALERT_SPACEX", default="false")
TELEGRAM_ALERT_BTC = _parse_env_bool("TELEGRAM_ALERT_BTC", default="false")
TELEGRAM_ALERT_SOCIAL = _parse_env_bool("TELEGRAM_ALERT_SOCIAL", default="false")
TELEGRAM_WEEKLY_SUMMARY_TIME = os.getenv("TELEGRAM_WEEKLY_SUMMARY_TIME", "16:30").strip()
TELEGRAM_WEEKLY_LIVE_ENABLED = _parse_env_bool("TELEGRAM_WEEKLY_LIVE_ENABLED", default="false")


def telegram_weekly_summary_enabled() -> bool:
    """Friday weekly Telegram: on by default for paper; live needs TELEGRAM_WEEKLY_LIVE_ENABLED."""
    raw = os.getenv("TELEGRAM_WEEKLY_SUMMARY_ENABLED", "").strip().lower()
    if raw in ("false", "0", "no", "off"):
        return False
    if raw in ("true", "1", "yes", "on"):
        return True
    if PAPER_TRADING:
        return True
    return bool(TELEGRAM_WEEKLY_LIVE_ENABLED)


def email_weekly_summary_enabled() -> bool:
    """Legacy weekly email — off unless EMAIL_WEEKLY_SUMMARY_ENABLED=true."""
    raw = os.getenv("EMAIL_WEEKLY_SUMMARY_ENABLED", "").strip().lower()
    if raw not in ("true", "1", "yes", "on"):
        return False
    if PAPER_TRADING:
        return True
    return _parse_env_bool("EMAIL_WEEKLY_LIVE_ENABLED", default="false")


def telegram_alert_policy_summary() -> str:
    """One-line summary for startup / preflight logs."""
    bits = []
    if TELEGRAM_ALERT_HALT:
        bits.append("halt/resume")
    if TELEGRAM_ALERT_DRAWDOWN_MAJOR:
        bits.append(f"drawdown>{TELEGRAM_MAJOR_DRAWDOWN_PCT:.0%}")
    if TELEGRAM_ALERT_YIELD_GATE:
        bits.append("yield gate")
    if TELEGRAM_ALERT_DAILY_SUMMARY:
        bits.append(f"daily@{TELEGRAM_DAILY_SUMMARY_TIME} ET")
    if TELEGRAM_ALERT_PERIODIC_SUMMARY:
        bits.append(f"every {TELEGRAM_PERIODIC_SUMMARY_HOURS:g}h")
    if TELEGRAM_ALERT_LIVE_DAILY_SUMMARY and not PAPER_TRADING:
        bits.append(f"live daily@{TELEGRAM_LIVE_DAILY_SUMMARY_TIME} ET")
    if telegram_weekly_summary_enabled():
        bits.append(f"weekly Fri@{TELEGRAM_WEEKLY_SUMMARY_TIME} ET")
    if TELEGRAM_ALERT_LIVE_FILLS and not PAPER_TRADING:
        bits.append(f"live fills>${TELEGRAM_LIVE_FILL_MIN_USD:.0f}")
    return ", ".join(bits) if bits else "all high-signal alerts off"


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


def set_backtest_vti_ceiling(pct: float | None) -> None:
    """Paper backtest: max VTI core when PAPER_DYNAMIC_VTI is off (incl. --vti-core)."""
    global _backtest_vti_ceiling
    _backtest_vti_ceiling = float(pct) if pct is not None and pct > 0 else None


def backtest_vti_ceiling() -> float | None:
    return _backtest_vti_ceiling


def set_backtest_strict_pit_context(active: bool) -> None:
    global _backtest_strict_pit_ctx
    _backtest_strict_pit_ctx = bool(active)


def backtest_strict_pit_context() -> bool:
    return _backtest_strict_pit_ctx


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


def trading_profile() -> str:
    """Live Profile A (default) vs paper/research profiles (set TRADING_PROFILE in .env)."""
    return os.getenv("TRADING_PROFILE", "A").strip().upper()


CRYPTO_SLEEVE_DISABLED_MSG = (
    "Crypto sleeve disabled for Profile A (Alpaca crypto not enabled)"
)


def crypto_sleeve_enabled() -> bool:
    """Alpaca crypto orders — delegates to effective_crypto_enabled()."""
    return effective_crypto_enabled()


def effective_crypto_enabled() -> bool:
    """Crypto pairs sleeve — off on live Profile A; paper opt-in or crypto v2."""
    if not PAPER_TRADING:
        return False
    if is_small_account() and not paper_only_sleeves_active():
        return False
    if effective_crypto_v2_enabled():
        return True
    if paper_only_sleeves_active() or paper_aggressive_context():
        return PAPER_CRYPTO_ENABLED
    if paper_chase_mode_enabled():
        return PAPER_CRYPTO_ENABLED
    return CRYPTO_SLEEVE_ENABLED


def set_dynamic_risk_context(
    *,
    vol_score: float | None = None,
    regime: str | None = None,
    macro_stress: bool | None = None,
    drawdown: float | None = None,
    recovery_mode: bool | None = None,
    equity_history: list[float] | None = None,
) -> None:
    """Update per-cycle inputs for paper dynamic risk (run_all / backtester)."""
    global _dynamic_risk_ctx
    if vol_score is not None:
        _dynamic_risk_ctx["vol_score"] = float(vol_score)
    if regime is not None:
        _dynamic_risk_ctx["regime"] = str(regime)
    if macro_stress is not None:
        _dynamic_risk_ctx["macro_stress"] = bool(macro_stress)
    if drawdown is not None:
        _dynamic_risk_ctx["drawdown"] = max(0.0, float(drawdown))
    if recovery_mode is not None:
        _dynamic_risk_ctx["recovery_mode"] = bool(recovery_mode)
    if equity_history is not None:
        _dynamic_risk_ctx["equity_history"] = [float(x) for x in equity_history if x is not None]


def effective_halt_resume_drawdown_pct() -> float:
    if paper_aggressive_context():
        return PAPER_HALT_RESUME_DRAWDOWN_PCT
    return HALT_RESUME_DRAWDOWN_PCT


def paper_sleeve_hard_cap_pct(sleeve_key: str) -> float | None:
    if not paper_aggressive_context():
        return None
    caps = {
        "spy": PAPER_SPY_MAX_EXPOSURE_PCT,
        "nyse": PAPER_NYSE_MAX_EXPOSURE_PCT,
        "crypto": PAPER_CRYPTO_MAX_EXPOSURE_PCT,
    }
    return caps.get(sleeve_key)


def set_backtest_live_conservative_context(enabled: bool) -> None:
    global _backtest_live_conservative_ctx
    _backtest_live_conservative_ctx = bool(enabled)


def backtest_live_conservative_context() -> bool:
    return bool(_backtest_live_conservative_ctx)


def live_conservative_profile_active() -> bool:
    """85/15 live small-account split: 85% VTI + 5% SPY trend (v1.1c winner) + legacy active."""
    if not LIVE_CONSERVATIVE_ENABLED:
        return False
    if paper_aggressive_context() or paper_only_sleeves_active():
        return False
    if backtest_small_account_context():
        return backtest_live_conservative_context()
    if PAPER_TRADING:
        return False
    return is_small_account()


def enforce_live_small_account_profile() -> None:
    """Apply live conservative defaults for alpaca_live small accounts (.env overrides win)."""
    global SMALL_ACCOUNT_VTI_CORE_PCT
    if not _env_explicit("SMALL_ACCOUNT_VTI_CORE_PCT"):
        SMALL_ACCOUNT_VTI_CORE_PCT = LIVE_VTI_CORE_PCT


def format_live_conservative_banner() -> str:
    sleeve_labels = {
        "spy": "SPY trend",
        "stat_arb": "Stat Arb",
        "nyse": "NYSE momentum",
        "cash": "cash buffer",
    }
    sleeve = sleeve_labels.get(LIVE_ACTIVE_SLEEVE_CHOICE, LIVE_ACTIVE_SLEEVE_CHOICE)
    legacy_active = max(
        0.0, 1.0 - LIVE_VTI_CORE_PCT - LIVE_SMALL_ACTIVE_SLEEVE_PCT
    )
    return (
        f"Live Conservative {LIVE_VTI_CORE_PCT:.0%}/{legacy_active:.0%}: "
        f"{LIVE_VTI_CORE_PCT:.0%} VTI | {LIVE_SMALL_ACTIVE_SLEEVE_PCT:.0%} {sleeve} | "
        f"{legacy_active:.0%} NYSE/active | "
        f"{SMALL_ACCOUNT_RISK_PER_TRADE:.0%} risk | ${SMALL_ACCOUNT_MAX_NOTIONAL:.0f} max/order"
    )


def get_live_profile_summary() -> str:
    return (
        f"Live Conservative: {LIVE_VTI_CORE_PCT:.0%} VTI | "
        f"{LIVE_SMALL_ACTIVE_SLEEVE_PCT:.0%} SPY trend | crypto OFF | thinking OFF | static universe"
    )


def configure_account_profile(equity: float, cash: float | None = None) -> dict:
    """Apply runtime sizing/VTI profile from live Alpaca equity (call each cycle)."""
    global _account_equity, _small_account_mode, _account_cash
    _account_equity = float(equity)
    _account_cash = float(cash) if cash is not None else _account_cash
    _small_account_mode = _account_equity < SMALL_ACCOUNT_EQUITY_THRESHOLD
    if _small_account_mode and not PAPER_TRADING:
        enforce_live_small_account_profile()
    core_pct = effective_vti_core_pct(equity=_account_equity)
    return {
        "equity": _account_equity,
        "cash": _account_cash,
        "cash_pct": account_cash_pct(),
        "small_account": _small_account_mode,
        "live_conservative": live_conservative_profile_active(),
        "risk_per_trade": effective_risk_per_trade(_account_equity),
        "max_notional_per_order": effective_max_notional_per_order(),
        "vti_core_pct": core_pct,
        "active_sleeve_pct": LIVE_SMALL_ACTIVE_SLEEVE_PCT
        if live_conservative_profile_active()
        else 0.0,
        "active_sleeve": LIVE_ACTIVE_SLEEVE_CHOICE
        if live_conservative_profile_active()
        else "",
    }


def effective_risk_per_trade(
    equity: float | None = None,
    *,
    vol_score: float | None = None,
    regime: str | None = None,
    macro_stress: bool | None = None,
    drawdown: float | None = None,
    recovery_mode: bool | None = None,
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

        base = get_dynamic_risk_per_trade(eq, float(vs), reg, bool(stress))
    else:
        base = RISK_PER_TRADE
    if PAPER_REGIME_DD_RISK_ENABLED and paper_aggressive_context():
        from modules.paper_risk_controls import regime_dd_risk_multiplier

        dd = (
            drawdown
            if drawdown is not None
            else float(_dynamic_risk_ctx.get("drawdown", 0.0))
        )
        rec = (
            recovery_mode
            if recovery_mode is not None
            else bool(_dynamic_risk_ctx.get("recovery_mode", False))
        )
        base = round(base * regime_dd_risk_multiplier(reg, dd, recovery_mode=rec), 6)
    if effective_positioning_overlay_enabled():
        from modules.positioning_overlay import positioning_risk_multiplier

        base = round(base * positioning_risk_multiplier(), 6)
    if VOL_CEILING_ENABLED and paper_aggressive_context():
        from modules.paper_risk_controls import vol_ceiling_risk_multiplier

        base = round(base * vol_ceiling_risk_multiplier(vs), 6)
    if effective_tail_risk_controls():
        from modules.risk_management import portfolio_vol_risk_multiplier

        hist = _dynamic_risk_ctx.get("equity_history") or []
        base = round(
            base
            * portfolio_vol_risk_multiplier(
                hist,
                ceiling=PORTFOLIO_VOL_CEILING_PCT,
                window=PORTFOLIO_VOL_WINDOW,
                min_mult=PORTFOLIO_VOL_MIN_RISK_MULT,
            ),
            6,
        )
    return base


def effective_per_name_max_pct() -> float:
    """Strict per-name ceiling (default 8%)."""
    return min(float(PER_NAME_MAX_PCT), float(PAPER_MAX_POSITION_PCT))


def effective_tail_risk_controls() -> bool:
    """Conservative tail-risk overlay — paper/research only."""
    if not TAIL_RISK_CONTROLS_ENABLED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    return bool(
        is_realistic_research_active()
        or paper_chase_mode_enabled()
        or paper_aggressive_context()
        or backtest_paper_sleeves_context()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_positioning_overlay_enabled() -> bool:
    """COT positioning overlay — paper aggressive default; live opt-in."""
    if not POSITIONING_OVERLAY_ENABLED:
        return False
    if paper_aggressive_context() or backtest_paper_sleeves_context():
        return True
    if POSITIONING_OVERLAY_LIVE_ENABLED and not PAPER_TRADING:
        return True
    return False


# ---------------------------------------------------------------------------
# Deployment threshold routing (paper research vs live)
#
# Paper aggressive (~$100k research book) uses low floors ($2 min order, $0.50
# dust) and excess-cash sleeve boosts so model targets actually fill — idle
# cash was the main blocker for SPY/NYSE/stat-arb sleeves.
#
# Live Profile A (~$300) keeps $10 min orders and no excess-cash boost to limit
# partial fills, fee drag, and churn on a small real-money book. Override via
# LIVE_MIN_NOTIONAL / LIVE_DUST_* env vars if needed.
#
# Call sites must use effective_* helpers — never PAPER_* / LIVE_* / MIN_NOTIONAL
# directly in Alpaca execution (alpaca_executor, deployment_sizing, sleeves).
# ---------------------------------------------------------------------------


def effective_min_notional(equity: float | None = None) -> float:
    """Minimum order notional for the active book (paper research vs live).

    Paper (``paper_aggressive_context``): ``PAPER_MIN_NOTIONAL`` ($2 default) so
    ~$100k research accounts can deploy idle cash without sub-cap clips blocking
    every sleeve top-up.

    Live: ``LIVE_MIN_NOTIONAL`` ($10 default) — higher floor avoids fee churn,
    partial-fill noise, and over-trading on Profile A's ~$300 book. Live never
    scales min-notional down with equity; the floor is absolute.

    Always use this helper (never raw ``MIN_NOTIONAL``) in Alpaca execution paths.
    """
    del equity  # reserved for future equity-tier live tiers
    if paper_aggressive_context():
        return max(ALPACA_MIN_NOTIONAL, PAPER_MIN_NOTIONAL)
    return max(ALPACA_MIN_NOTIONAL, LIVE_MIN_NOTIONAL)


def effective_dust_max_notional() -> float:
    """Dust cleanup / scrape-room threshold — lower on paper, higher on live."""
    if paper_aggressive_context():
        return max(ALPACA_MIN_NOTIONAL, PAPER_DUST_MAX_NOTIONAL)
    return max(ALPACA_MIN_NOTIONAL, LIVE_DUST_MAX_NOTIONAL)


def effective_dust_skip_chunk_frac() -> float:
    """Fraction of per-trade chunk below which paper skips sleeve top-ups (live higher)."""
    if paper_aggressive_context():
        return PAPER_DUST_SKIP_CHUNK_FRAC
    return LIVE_DUST_SKIP_CHUNK_FRAC


def effective_excess_cash_threshold_pct() -> float:
    """Cash % above which sleeve-cap boost begins (paper only by default)."""
    if paper_aggressive_context():
        return PAPER_EXCESS_CASH_THRESHOLD_PCT
    return 1.0


def effective_excess_cash_high_threshold_pct() -> float:
    if paper_aggressive_context():
        return PAPER_EXCESS_CASH_HIGH_THRESHOLD_PCT
    return 1.0


def effective_excess_cash_deploy_threshold_pct() -> float:
    """Cash % above which aggressive deploy sizing kicks in (paper only by default)."""
    if paper_aggressive_context():
        return PAPER_EXCESS_CASH_DEPLOY_THRESHOLD_PCT
    return LIVE_EXCESS_CASH_DEPLOY_THRESHOLD_PCT


def _resolve_cash_pct(
    cash_pct: float | None = None,
    *,
    equity: float | None = None,
    cash: float | None = None,
) -> float | None:
    """Best available broker cash % — explicit arg, then equity/cash, then profile cache."""
    if cash_pct is not None:
        return float(cash_pct)
    if equity is not None and cash is not None and float(equity) > 0:
        return round(float(cash) / float(equity), 6)
    return account_cash_pct()


def effective_excess_cash_sleeve_mult(
    cash_pct: float | None = None,
    *,
    equity: float | None = None,
    cash: float | None = None,
) -> float:
    """Boost active sleeve caps when broker cash is high (paper research only).

    Live returns ``LIVE_EXCESS_CASH_SLEEVE_BOOST`` (1.0 = no boost) so Profile A
    stays at model caps without research-style cash-burn deployment.
    """
    if not paper_aggressive_context():
        mult = LIVE_EXCESS_CASH_SLEEVE_BOOST
        if PAPER_DEPLOY_DEBUG:
            print(
                f"[deploy] excess_cash_sleeve_mult ctx=False pct={cash_pct} -> {mult}"
            )
        return mult
    pct = _resolve_cash_pct(cash_pct, equity=equity, cash=cash)
    if pct is None or pct < PAPER_EXCESS_CASH_DEPLOY_THRESHOLD_PCT:
        mult = 1.0
    elif pct >= PAPER_EXCESS_CASH_HIGH_THRESHOLD_PCT:
        mult = round(PAPER_EXCESS_CASH_HIGH_BOOST, 4)
    else:
        span = max(
            1e-6,
            PAPER_EXCESS_CASH_HIGH_THRESHOLD_PCT - PAPER_EXCESS_CASH_DEPLOY_THRESHOLD_PCT,
        )
        t = max(
            0.0,
            min(1.0, (float(pct) - PAPER_EXCESS_CASH_DEPLOY_THRESHOLD_PCT) / span),
        )
        mult = round(
            PAPER_EXCESS_CASH_SLEEVE_BOOST
            + (PAPER_EXCESS_CASH_HIGH_BOOST - PAPER_EXCESS_CASH_SLEEVE_BOOST) * t,
            4,
        )
        mult = max(PAPER_EXCESS_CASH_SLEEVE_BOOST, mult)
    if PAPER_DEPLOY_DEBUG:
        print(
            f"[deploy] excess_cash_sleeve_mult ctx=True pct={pct} "
            f"high>={PAPER_EXCESS_CASH_HIGH_THRESHOLD_PCT} -> {mult}"
        )
    return mult


def paper_excess_cash_sleeve_mult(cash_pct: float | None = None) -> float:
    """Alias for ``effective_excess_cash_sleeve_mult`` (legacy call sites)."""
    return effective_excess_cash_sleeve_mult(cash_pct)


def paper_deploy_aggressive(
    cash_pct: float | None = None,
    *,
    equity: float | None = None,
    cash: float | None = None,
) -> bool:
    """True when broker cash is high enough to force more sleeve filling."""
    if not paper_aggressive_context():
        if PAPER_DEPLOY_DEBUG:
            print("[deploy] paper_deploy_aggressive ctx=False -> False")
        return False
    pct = _resolve_cash_pct(cash_pct, equity=equity, cash=cash)
    threshold = PAPER_EXCESS_CASH_DEPLOY_THRESHOLD_PCT
    active = pct is not None and pct >= threshold
    if PAPER_DEPLOY_DEBUG:
        print(
            f"[deploy] paper_deploy_aggressive pct={pct} threshold={threshold} -> {active}"
        )
    return active


def _yield_gate_hard_regime(regime: str | None) -> bool:
    """True for strong bear / panic regimes where paper override still blocks."""
    reg = str(regime or "")
    return any(tag in reg for tag in ("RHYME_B", "RHYME_E", "Panic_Volatility", "Bearish_Decline"))


def effective_yield_gate(
    raw_gated: bool,
    *,
    regime: str | None = None,
) -> bool:
    """Resolve yield-gate block for this cycle.

    Live / non-paper: unchanged (fully gated when raw_gated).
    Paper with ``PAPER_YIELD_GATE_OVERRIDE``: soften mild rate/bond stress so
    deployment can continue; still block in strong bear/panic (RHYME_B/E).
    """
    if not raw_gated:
        return False
    if not PAPER_YIELD_GATE_OVERRIDE:
        return True
    if not (paper_aggressive_context() or is_realistic_research_active()):
        # Live / non-paper books stay fully gated.
        return True
    if _yield_gate_hard_regime(regime):
        return True
    return False


def effective_no_room_min_notional(
    equity: float | None = None,
    *,
    cash_pct: float | None = None,
    cash: float | None = None,
) -> float:
    """Lower effective min for sleeve room checks when paper has excess cash."""
    base = effective_min_notional(equity)
    if paper_deploy_aggressive(cash_pct, equity=equity, cash=cash):
        return ALPACA_MIN_NOTIONAL
    return base


def format_high_cash_deploy_banner(
    equity: float | None = None,
    cash: float | None = None,
) -> str | None:
    """Startup/cycle banner when paper research is in high-cash deploy mode."""
    if not paper_aggressive_context():
        return None
    pct = _resolve_cash_pct(equity=equity, cash=cash)
    if not paper_deploy_aggressive(pct, equity=equity, cash=cash):
        return None
    mult = effective_excess_cash_sleeve_mult(pct, equity=equity, cash=cash)
    pct_disp = float(pct) * 100.0 if pct is not None else 0.0
    nyse_cap = effective_nyse_sleeve_cap_pct(pct, equity=equity, cash=cash)
    return (
        f"High cash deploy mode: ON ({pct_disp:.0f}% cash -> {mult:.2f}x boost; "
        f"NYSE cap {nyse_cap:.0%})"
    )


def effective_nyse_sleeve_cap_pct(
    cash_pct: float | None = None,
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str | None = None,
    base_pct: float | None = None,
) -> float:
    """Paper/research NYSE momentum sleeve cap (0.18–0.22) with high-cash expansion.

    Live books keep the scaled fund-allocation NYSE cap. Paper/research floors the
    sleeve at PAPER_NYSE_SLEEVE_CAP_PCT and temporarily expands toward
    PAPER_NYSE_HIGH_CASH_CAP_PCT when deploy-aggressive (high cash).
    """
    fund_scaled = effective_sleeve_cap(NYSE_SLEEVE_CAP_PCT, sleeve="nyse")
    if base_pct is not None:
        # Callers may pass raw NYSE_SLEEVE_CAP_PCT or an already-scaled fraction.
        scaled = max(fund_scaled, float(base_pct))
    else:
        scaled = fund_scaled
    paperish = paper_aggressive_context() or is_realistic_research_active()
    if not paperish:
        return round(fund_scaled, 6)

    target = max(scaled, float(PAPER_NYSE_SLEEVE_CAP_PCT))
    hard = float(PAPER_NYSE_MAX_EXPOSURE_PCT)
    expanded = False
    if paper_deploy_aggressive(cash_pct, equity=equity, cash=cash):
        high = float(PAPER_NYSE_HIGH_CASH_CAP_PCT)
        if high > target + 1e-9:
            expanded = True
        target = max(target, high)

    if regime and effective_tail_risk_controls() and "RHYME_B" in str(regime):
        target = round(target * (1.0 - float(PAPER_REGIME_B_CASH_BUFFER_BOOST)), 6)
    if regime:
        try:
            from modules.regime_sizing import regime_sleeve_exposure_ceiling

            weak_ceil = regime_sleeve_exposure_ceiling(regime)
            if weak_ceil is not None:
                target = min(target, float(weak_ceil))
        except Exception:
            pass

    out = round(min(max(target, 0.0), hard), 6)
    if expanded and PAPER_DEPLOY_DEBUG:
        print(
            f"[deploy] NYSE cap expansion due to high cash "
            f"scaled={scaled:.2%} -> {out:.2%} (floor={PAPER_NYSE_SLEEVE_CAP_PCT:.0%} "
            f"high={PAPER_NYSE_HIGH_CASH_CAP_PCT:.0%})"
        )
    return out


def effective_max_equity_trades() -> int:
    """NYSE momentum entries per cycle. Paper defaults to 3 when cash is idle."""
    if not (paper_aggressive_context() or is_realistic_research_active()):
        return 1
    try:
        n = int(PAPER_MAX_EQUITY_TRADES)
    except (TypeError, ValueError):
        n = 3
    return max(1, min(5, n))


def account_cash_pct() -> float | None:
    """Live broker cash / equity from the last configure_account_profile() call."""
    if _account_equity is None or _account_equity <= 0 or _account_cash is None:
        return None
    return round(float(_account_cash) / float(_account_equity), 6)


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
    sym = normalize_symbol(symbol)
    if sym.endswith("-USD"):
        return True
    if "/" in str(symbol).upper() and str(symbol).upper().endswith("/USD"):
        return True
    if sym in CRYPTO_TICKERS:
        return True
    if effective_crypto_universe_expanded():
        try:
            from modules.crypto_universe import expanded_crypto_symbols

            return sym in expanded_crypto_symbols()
        except ImportError:
            pass
    return False


def normalize_symbol(symbol: str) -> str:
    """Alpaca (BTCUSD, BTC/USD) -> universe form (BTC-USD)."""
    s = symbol.replace("/", "-")
    if s.endswith("USD") and "-" not in s:
        return f"{s[:-3]}-USD"
    return s


def base_crypto_universe() -> list[str]:
    """Static crypto pairs from UNIVERSE (24 majors)."""
    return [t for t in UNIVERSE if t in CRYPTO_TICKERS]


def crypto_universe() -> list[str]:
    """Crypto symbols for data refresh and sleeve scans."""
    if effective_crypto_universe_expanded():
        try:
            from modules.crypto_universe import expanded_crypto_symbols

            return expanded_crypto_symbols()
        except ImportError:
            pass
    return base_crypto_universe()


def effective_crypto_universe_expanded() -> bool:
    """Paper expanded Alpaca crypto universe; off on live Profile A by default."""
    if not PAPER_CRYPTO_UNIVERSE_EXPANDED:
        return False
    if paper_only_sleeves_active() or PAPER_TRADING:
        return True
    return bool(backtest_paper_sleeves_context())


def set_backtest_crypto_expanded_prefetch(enabled: bool) -> None:
    """Include expanded crypto tickers in backtest_fetch_tickers() prefetch."""
    global _backtest_crypto_expanded_prefetch
    _backtest_crypto_expanded_prefetch = bool(enabled)


def backtest_crypto_expanded_prefetch() -> bool:
    return bool(_backtest_crypto_expanded_prefetch)


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
    """Tickers to load for daily backtests (static UNIVERSE + screener + expanded crypto)."""
    tickers = list(UNIVERSE)
    if USE_DYNAMIC_UNIVERSE or effective_paper_dynamic_universe():
        extra = load_screener_universe_tickers() or []
        tickers = sorted(set(tickers) | set(extra))
    if effective_crypto_universe_expanded() or backtest_crypto_expanded_prefetch():
        try:
            from modules.crypto_universe import expanded_crypto_symbols

            tickers = sorted(set(tickers) | set(expanded_crypto_symbols()))
        except ImportError:
            pass
    if effective_dynamic_sector_screener() or DYNAMIC_SECTOR_SCREENER_ENABLED:
        try:
            from modules.sector_screener import (
                sector_etf_symbols,
                sector_expansion_prefetch_tickers,
            )

            tickers = sorted(
                set(tickers)
                | set(sector_etf_symbols())
                | set(sector_expansion_prefetch_tickers())
            )
        except ImportError:
            pass
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
    if not crypto_sleeve_enabled():
        print(f"  crypto_sleeve:          {CRYPTO_SLEEVE_DISABLED_MSG}")
    if vti_core_enabled():
        print(
            f"  vti_core:             {alloc['vti_core']:.0%} {VTI_CORE_SYMBOL} passive | "
            f"active {active_fund_fraction():.0%}"
        )
    stat_part = (
        f" | stat_arb {alloc['stat_arb']:.0%}"
        if alloc.get("stat_arb", 0) > 0
        else ""
    )
    print(
        f"  sleeves: SPY {alloc['spy']:.0%} | crypto {alloc['crypto']:.0%} | "
        f"NYSE {alloc['nyse']:.0%}{stat_part} | metal {alloc['metal']:.0%} | "
        f"cash {alloc['cash_buffer']:.0%}"
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
        stack["nyse_conditional"] = PAPER_NYSE_CONDITIONAL_ON_SPY
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
            "nyse_conditional": PAPER_NYSE_CONDITIONAL_ON_SPY,
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


_best_paper_config_mod = None


def refresh_paper_new_markets_flags_from_env() -> None:
    """Re-read opt-in ADR/bond sleeve flags after dotenv or .env edits."""
    global PAPER_INTERNATIONAL_SLEEVE_ENABLED, PAPER_BOND_SLEEVE_ENABLED
    PAPER_INTERNATIONAL_SLEEVE_ENABLED = _parse_env_bool(
        "PAPER_INTERNATIONAL_SLEEVE_ENABLED", default="false"
    )
    PAPER_BOND_SLEEVE_ENABLED = _parse_env_bool("PAPER_BOND_SLEEVE_ENABLED", default="false")


def _load_best_paper_config():
    """Load config/best_paper_config.py (config.py shadows the config/ package name)."""
    global _best_paper_config_mod
    if _best_paper_config_mod is not None:
        return _best_paper_config_mod
    path = Path(__file__).resolve().parent / "config" / "best_paper_config.py"
    if not path.is_file():
        raise ImportError(f"best_paper_config not found at {path}")
    spec = importlib.util.spec_from_file_location("_best_paper_config", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load best_paper_config from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _best_paper_config_mod = mod
    return mod


def get_best_paper_validated_defaults_line() -> str:
    try:
        return _load_best_paper_config().get_validated_defaults_line()
    except (ImportError, AttributeError):
        return (
            "v2.2 LOCK: strict PIT | conservative blend | thinking ON | "
            "dyn_univ ON | crypto/rotation/ADR/bond OFF"
        )


def get_best_paper_display_name() -> str:
    try:
        return _load_best_paper_config().BEST_PAPER_DISPLAY_NAME
    except (ImportError, AttributeError):
        return "Best Paper Bot v2.2 (conservative blend + thinking ON)"


def get_best_paper_final_lock_banner() -> str:
    try:
        return _load_best_paper_config().get_final_lock_banner()
    except (ImportError, AttributeError):
        return "FINAL CONFIG: Best Paper Bot v2.2 locked"


def get_best_paper_v22_summary_lines() -> list[str]:
    try:
        return _load_best_paper_config().get_v22_config_summary_lines()
    except (ImportError, AttributeError):
        return [
            "=== Best Paper v2.2 (locked defaults) ===",
            "  ON: strict PIT | conservative Top1 | thinking | dyn_univ",
            "  OFF: crypto | rotation | ADR | bond | scaling | patterns",
        ]


def get_best_paper_locked_header() -> str:
    try:
        return _load_best_paper_config().get_locked_stack_header()
    except (ImportError, AttributeError):
        return "LOCKED Best Paper Bot v2.2 (conservative blend + thinking ON)"


def get_best_paper_restart_lines() -> list[str]:
    try:
        return _load_best_paper_config().get_restart_commands_block()
    except (ImportError, AttributeError):
        return [
            "=== Restart bots ===",
            "Live: python run_all.py",
            "Paper: python run_paper_bot.py",
        ]


def get_live_profile_defaults_line() -> str:
    try:
        summary = _load_best_paper_config().get_live_profile_summary()
        if summary:
            return summary
    except (ImportError, AttributeError):
        pass
    return get_live_profile_summary()


def get_top1_conservative_blend_line() -> str:
    if not PAPER_VOL_POSITION_SIZING_ENABLED and not PAPER_LOSS_CUTTING_ENABLED:
        return "Top1 conservative blend: OFF"
    parts: list[str] = []
    if effective_vol_position_sizing_enabled():
        parts.append("vol sizing on")
    if effective_loss_cutting_enabled():
        parts.append("loss cutting on")
    return "Top1 conservative blend: " + (", ".join(parts) if parts else "OFF")


def print_paper_research_stack_flags() -> None:
    """Log Best Paper Bot / Profile B stack (paper_aggressive)."""
    gp = _game_plan_label()
    was_ctx = paper_aggressive_context()
    set_paper_aggressive_context(True)
    try:
        alloc = fund_allocation_pct()
        stack = get_best_paper_bot_stack()
        print("--- Best Paper Bot (realistic research / Profile B) ---")
        if is_realistic_research_active():
            for line in format_realistic_research_startup_lines():
                print(f"  {line}")
        research_line = format_research_mode_banner()
        if research_line:
            print(f"--- {research_line} ---")
        for line in paper_frequency_mode_lines():
            print(f"  {line}")
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
            f"(Smart {DYNAMIC_VTI_PAPER_FLOOR:.0%}-{DYNAMIC_VTI_PAPER_CEILING:.0%})"
        )
        print(
            f"  dynamic_risk:         "
            f"{'on' if stack['dynamic_risk'] else 'off'} "
            f"({PAPER_RISK_CALM_BULL_PCT:.1%} calm bull / "
            f"{PAPER_RISK_MODERATE_PCT:.1%} moderate / {PAPER_RISK_STRESS_PCT:.1%} stress)"
        )
        z_label = (
            f"Z>={PAPER_PAIR_Z_THRESHOLD} (dynamic {PAPER_PAIR_Z_CALM}-{PAPER_PAIR_Z_STRESS})"
            if PAPER_PAIR_Z_DYNAMIC
            else f"Z>={PAPER_PAIR_Z_THRESHOLD}"
        )
        print(
            f"  stat_arb:             "
            f"{'on' if stack['stat_arb'] and effective_stat_arb_enabled() else 'off'} "
            f"(corr>{PAPER_PAIR_MIN_CORRELATION}, {z_label}, "
            f"max_trades={PAPER_STAT_ARB_MAX_TRADES})"
        )
        print(f"  nyse_stat_arb_path:   {nyse_stat_arb_mode_label()}")
        print(
            f"  vol_overlay:          "
            f"{'on' if stack['vol_overlay'] and effective_vol_trading_enabled() else 'off'} "
            f"(cap {VOL_SLEEVE_CAP_PCT:.0%})"
        )
        print(
            f"  thinking_engine:      "
            f"{'on' if effective_thinking_engine_enabled() else 'off'} "
            f"(paper={'on' if PAPER_THINKING_ENGINE_ENABLED else 'off'}, "
            f"live={'on' if LIVE_THINKING_ENGINE_ENABLED else 'off'})"
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
        print(
            f"  crypto_sleeve:        "
            f"{'on' if effective_crypto_enabled() else 'off (PAPER_CRYPTO_ENABLED=false)'}"
        )
        print(f"  crypto_vol_only:      {effective_crypto_vol_only()}")
        print(f"  wisdom_sizing_floor:  {PAPER_WISDOM_SIZING_FLOOR}")
        stat_part = (
            f" | stat_arb {alloc['stat_arb']:.0%}"
            if alloc.get("stat_arb", 0) > 0
            else ""
        )
        print(
            f"  sleeves: SPY {alloc['spy']:.0%} | crypto {alloc['crypto']:.0%} | "
            f"NYSE {alloc['nyse']:.0%}{stat_part} | metal {alloc['metal']:.0%} | "
            f"cash {alloc['cash_buffer']:.0%}"
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
    """Disable weak/redundant paper sleeves (Profile B locked stack v3)."""
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
    enforce_realistic_research_profile()


def _env_explicit(*keys: str) -> bool:
    return any(os.getenv(key) is not None for key in keys)


def enforce_realistic_research_profile() -> None:
    """Re-apply Realistic Research v1.5 locks (.env overrides win).

  Final paper default (Profile B / alpaca_paper). Locked stack:
    - Scanners: RVOL, ORB, Catalyst, ATR sizing
    - Sizing: conviction (0.4x-1.8x), multi-timeframe confirmation, correlation guard
    - Exits: partial @1R, dynamic trail, time-based max hold
    - Insider monitor + signal boosts + risk guard
    - Protective + sector shorts (8-18% gross, RR 1.6)
    - Stat arb 10-14 pairs, Smart Dynamic VTI (default), tail-risk vol ceiling
    - Bot health + per-strategy performance tracking
    - Heartbeat watchdog + auto-recovery (supervisor)
    See REALISTIC_RESEARCH_LOCKED_FEATURES for the banner summary.
    """
    global DYNAMIC_CORE_ENABLED
    global DEEP_HISTORY_ENABLED
    global DEEP_HISTORY_INDICATORS_ONLY
    global REBALANCE_ENABLED
    global POSITIONING_OVERLAY_ENABLED
    global PAPER_DYNAMIC_VTI_ENABLED
    global PAPER_VTI_CORE_PCT
    global VTI_CORE_PCT
    global PAPER_RISK_PER_TRADE
    global PAPER_RISK_CALM_BULL_PCT
    global RISK_PER_TRADE
    global PAPER_POSITION_MAX_HOLD_BARS
    global PAPER_REGIME_B_SIZING_MULT
    global PAPER_REGIME_B_RISK_MULT
    global TAIL_RISK_CONTROLS_ENABLED
    global VOL_CEILING_ENABLED
    global PAPER_VOL_CEILING_PCT
    global PORTFOLIO_VOL_CEILING_PCT
    global PORTFOLIO_VOL_MIN_RISK_MULT
    global PORTFOLIO_VOL_WINDOW
    global PAPER_REGIME_DD_RISK_ENABLED
    global PAPER_REGIME_D_RISK_MULT
    global PAPER_DD_RISK_WARN_PCT
    global PAPER_DD_RISK_MULT_5
    global PAPER_DD_RISK_SEVERE_PCT
    global PAPER_DD_RISK_MULT_8
    global PAPER_REGIME_B_CASH_BUFFER_BOOST
    global PAPER_MAX_POSITION_PCT
    global PER_NAME_MAX_PCT
    global SECTOR_HIGH_VOL_CEILING_PCT
    global SECTOR_HIGH_VOL_EXPANSION_CAP
    global SECTOR_HIGH_VOL_MAX_ACTIVE_SECTORS
    global PAPER_REGIME_WEAK_SLEEVE_MAX_PCT
    global CORE_ALLOCATOR_LOCKED
    global CORE_ALLOCATOR_LOCKED_CHOICE
    global DYNAMIC_SECTOR_SCREENER_ENABLED
    global BASE_UNIVERSE_SIZE
    global SECTOR_EXPANSION_SIZE
    global SECTOR_MAX_TOTAL_TICKERS
    global SECTOR_FALLBACK_MOMENTUM_COUNT
    global SECTOR_RS_MIN
    global PAPER_MIN_NOTIONAL_MULT
    global PAPER_MIN_NOTIONAL
    global PAPER_DUST_MAX_NOTIONAL
    global PAPER_DUST_SKIP_CHUNK_FRAC
    global PAPER_EXCESS_CASH_SLEEVE_BOOST
    global PAPER_EXCESS_CASH_HIGH_BOOST
    global PAPER_YIELD_GATE_OVERRIDE
    global PAPER_NYSE_SLEEVE_CAP_PCT
    global PAPER_NYSE_HIGH_CASH_CAP_PCT
    global PAPER_NYSE_MAX_EXPOSURE_PCT
    global NYSE_SLEEVE_CAP_PCT
    global PORTFOLIO_CONSTRUCTOR_ENABLED
    global STAT_ARB_SLEEVE_CAP_ENABLED
    global STAT_ARB_SLEEVE_CAP_PCT
    global STAT_ARB_VOL_SCALING_ENABLED
    global STAT_ARB_VOL_MIN_NOTIONAL_SCALE
    global PAPER_STAT_ARB_MIN_DOLLAR_VOLUME
    global PAPER_STAT_ARB_TRAILING_ARM_FRAC
    global PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC
    global PAPER_STAT_ARB_MIN_REVERT_FRAC
    global PAPER_STAT_ARB_EQUITY_MAX_HOLD_BARS
    global PAPER_STAT_ARB_MAX_LEG_VOL
    global PAPER_STAT_ARB_CONVICTION_MIN_SCALE
    global PAPER_STAT_ARB_CONVICTION_MAX_SCALE
    global PAPER_STAT_ARB_TRAIL_MIN_PROFIT_FRAC
    global PAPER_STAT_ARB_MAX_PAIRS
    global PAPER_STAT_ARB_MAX_PAIRS_EXPANDED
    global PAPER_STAT_ARB_MAX_PAIRS_CEILING
    global PAPER_STAT_ARB_Z_ENTRY_BASE
    global PAPER_STAT_ARB_Z_ENTRY_MAX
    global PAPER_STAT_ARB_RISK_REWARD
    global PAPER_STAT_ARB_MIN_CORR
    global PAPER_STAT_ARB_COINT_PVALUE
    global PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF
    global PROTECTIVE_SHORT_MAX_PCT
    global PROTECTIVE_SHORT_MIN_PCT
    global SHORT_RHYME_E_MAX_PCT
    global SHORT_RHYME_B_MAX_PCT
    global SECTOR_SHORT_MIN_BUBBLE_SCORE
    global SHORT_PARTIAL_PROFIT_ENABLED
    global SHORT_RHYME_E_BEAR_STREAK_BARS
    global SHORT_RHYME_E_WAIVER_MIN_STREAK
    global SHORT_WAIVER_SIZE_MULT
    global SHORT_TRAILING_ARM_FRAC
    global SHORT_TRAILING_PULLBACK_FRAC
    global SHORT_HIGH_VOL_VIX_THRESHOLD
    global SHORT_HIGH_VOL_STOP_MULT
    global SHORT_BUBBLE_SIZE_POWER
    global DYNAMIC_CORE_LOOKBACK_DAYS
    global OLLAMA_MODEL
    global OLLAMA_FALLBACK_MODELS
    global OLLAMA_USE_CHAT_API
    global OLLAMA_JSON_FORMAT
    global PAPER_THINKING_ENGINE_ENABLED
    global LIVE_THINKING_ENGINE_ENABLED
    global THINKING_ENGINE_ENABLED
    global SHORT_RHYME_E_ENABLED
    global SECTOR_SHORT_ENABLED
    global SECTOR_SHORT_MAX_PCT
    global SHORT_PROFIT_TARGET_PCT
    global SHORT_STOP_LOSS_PCT
    global SHORT_RHYME_E_EXHAUSTION_REQUIRED
    global SHORT_BUBBLE_MIN_FOR_RHYME_E
    global BUFFETT_INDICATOR_ENABLED
    global BUFFETT_OVERVALUED_THRESHOLD
    global INSIDER_MONITOR_ENABLED
    global INSIDER_CLUSTER_MIN_BUYERS
    global INSIDER_SIGNAL_BOOST_ENABLED
    global INSIDER_BOOST_ENABLED
    global INSIDER_RISK_GUARD_ENABLED
    global RVOL_SCANNER_ENABLED
    global RVOL_MIN_THRESHOLD
    global RVOL_STRONG_THRESHOLD
    global RVOL_BOOST_FACTOR
    global ORB_ENABLED
    global ORB_BREAKOUT_MINUTES
    global ORB_RVOL_MIN
    global ORB_BOOST_FACTOR
    global ORB_MOMENTUM_ENABLED
    global ORB_MOMENTUM_RISK_PCT
    global ORB_MOMENTUM_MAX_SIZE_PCT
    global ORB_MOMENTUM_MIN_SIZE_PCT
    global ORB_MOMENTUM_RR
    global ORB_MOMENTUM_BACKTEST_ENABLED
    global VOL_BREAKOUT_ENABLED
    global PAPER_VOL_BREAKOUT_ENABLED
    global VOL_BREAKOUT_RISK_PCT
    global VOL_BREAKOUT_MAX_SIZE_PCT
    global VOL_BREAKOUT_ATR_EXPAND_MULT
    global VOL_BREAKOUT_BACKTEST_ENABLED
    global SECTOR_ROTATION_ENABLED
    global PAPER_SECTOR_ROTATION_ENABLED
    global SECTOR_ROTATION_CAP_PCT
    global SECTOR_ROTATION_MAX_SECTOR_PCT
    global SECTOR_ROTATION_TOP_N
    global SECTOR_ROTATION_BACKTEST_ENABLED
    global CATALYST_SCORING_ENABLED
    global CATALYST_MIN_SCORE
    global CATALYST_BOOST_FACTOR
    global ATR_SIZING_ENABLED
    global CONVICTION_SIZING_ENABLED
    global MULTI_TIMEFRAME_ENABLED
    global EXIT_OPTIMIZATION_ENABLED
    global CORRELATION_GUARD_ENABLED
    global STAT_ARB_NYSE_OVERLAP_BLOCK_MULT
    global NYSE_MA_ENTRY_TOLERANCE_PCT
    global NYSE_RANK_SCANNER_WEIGHT
    global NYSE_ENTRY_RVOL_MIN
    global ATR_PERIOD
    global ATR_RISK_MULTIPLE
    global ATR_MAX_SIZE_PCT

    if not _env_explicit("DYNAMIC_CORE_ENABLED"):
        DYNAMIC_CORE_ENABLED = True
    if not _env_explicit("DEEP_HISTORY_ENABLED"):
        DEEP_HISTORY_ENABLED = True
    if not _env_explicit("DEEP_HISTORY_INDICATORS_ONLY"):
        DEEP_HISTORY_INDICATORS_ONLY = True
    if not _env_explicit("REBALANCE_ENABLED", "WISDOM_LAYER_ENABLED"):
        REBALANCE_ENABLED = False
    if not _env_explicit("POSITIONING_OVERLAY_ENABLED", "COT_OVERLAY_ENABLED"):
        POSITIONING_OVERLAY_ENABLED = False
    if not _env_explicit("PAPER_DYNAMIC_VTI", "PAPER_DYNAMIC_VTI_ENABLED"):
        PAPER_DYNAMIC_VTI_ENABLED = True
    if not _env_explicit("PORTFOLIO_CONSTRUCTOR_ENABLED"):
        # Sector-aware sleeve/short tilts on top of Smart Dynamic VTI (v1.5.4, paper research).
        PORTFOLIO_CONSTRUCTOR_ENABLED = True
    if not _env_explicit("CORE_ALLOCATOR_LOCKED"):
        # Smart Dynamic VTI (35-75%) is the research default; lock is opt-in.
        CORE_ALLOCATOR_LOCKED = False
    if not _env_explicit("CORE_ALLOCATOR_LOCKED_CHOICE"):
        CORE_ALLOCATOR_LOCKED_CHOICE = "spy"
    if not _env_explicit("PAPER_VTI_CORE_PCT", "VTI_CORE_PCT"):
        PAPER_VTI_CORE_PCT = 0.40
    if not _env_explicit("VTI_CORE_PCT"):
        VTI_CORE_PCT = 0.80
    if not _env_explicit("PAPER_RISK_PER_TRADE", "RISK_PER_TRADE"):
        PAPER_RISK_PER_TRADE = 0.018
    if not _env_explicit("PAPER_RISK_CALM_BULL_PCT"):
        PAPER_RISK_CALM_BULL_PCT = PAPER_RISK_PER_TRADE
    if not _env_explicit("RISK_PER_TRADE"):
        RISK_PER_TRADE = 0.018
    if not _env_explicit("PAPER_POSITION_MAX_HOLD_BARS"):
        PAPER_POSITION_MAX_HOLD_BARS = 30
    if not _env_explicit("PAPER_REGIME_B_SIZING_MULT"):
        PAPER_REGIME_B_SIZING_MULT = 0.30
    if not _env_explicit("PAPER_REGIME_B_RISK_MULT"):
        PAPER_REGIME_B_RISK_MULT = 0.50

    # --- Tail Risk Controls v1.1 (paper/research locks) ---
    if not _env_explicit("TAIL_RISK_CONTROLS_ENABLED"):
        TAIL_RISK_CONTROLS_ENABLED = True
    if not _env_explicit("VOL_CEILING_ENABLED"):
        VOL_CEILING_ENABLED = True
    if not _env_explicit("PAPER_VOL_CEILING_PCT"):
        PAPER_VOL_CEILING_PCT = 0.17
    if not _env_explicit("PORTFOLIO_VOL_CEILING_PCT"):
        PORTFOLIO_VOL_CEILING_PCT = 0.18
    if not _env_explicit("PORTFOLIO_VOL_MIN_RISK_MULT"):
        PORTFOLIO_VOL_MIN_RISK_MULT = 0.35
    if not _env_explicit("PORTFOLIO_VOL_WINDOW"):
        PORTFOLIO_VOL_WINDOW = 20
    if not _env_explicit("PAPER_REGIME_DD_RISK_ENABLED"):
        PAPER_REGIME_DD_RISK_ENABLED = True
    if not _env_explicit("PAPER_REGIME_D_RISK_MULT"):
        PAPER_REGIME_D_RISK_MULT = 0.75
    if not _env_explicit("PAPER_DD_RISK_WARN_PCT"):
        PAPER_DD_RISK_WARN_PCT = 0.05
    if not _env_explicit("PAPER_DD_RISK_MULT_5"):
        PAPER_DD_RISK_MULT_5 = 0.6
    if not _env_explicit("PAPER_DD_RISK_SEVERE_PCT"):
        PAPER_DD_RISK_SEVERE_PCT = 0.08
    if not _env_explicit("PAPER_DD_RISK_MULT_8"):
        PAPER_DD_RISK_MULT_8 = 0.3
    if not _env_explicit("PAPER_REGIME_B_CASH_BUFFER_BOOST"):
        PAPER_REGIME_B_CASH_BUFFER_BOOST = 0.12
    if not _env_explicit("PAPER_MAX_POSITION_PCT", "PER_NAME_MAX_PCT"):
        PAPER_MAX_POSITION_PCT = 0.08
    if not _env_explicit("PER_NAME_MAX_PCT"):
        PER_NAME_MAX_PCT = 0.08
    if not _env_explicit("SECTOR_HIGH_VOL_CEILING_PCT"):
        SECTOR_HIGH_VOL_CEILING_PCT = 0.18
    if not _env_explicit("SECTOR_HIGH_VOL_EXPANSION_CAP"):
        SECTOR_HIGH_VOL_EXPANSION_CAP = 10
    if not _env_explicit("SECTOR_HIGH_VOL_MAX_ACTIVE_SECTORS"):
        SECTOR_HIGH_VOL_MAX_ACTIVE_SECTORS = 1
    if not _env_explicit("PAPER_REGIME_WEAK_SLEEVE_MAX_PCT"):
        PAPER_REGIME_WEAK_SLEEVE_MAX_PCT = 0.25
    if not _env_explicit("DYNAMIC_SECTOR_SCREENER_ENABLED"):
        DYNAMIC_SECTOR_SCREENER_ENABLED = True
    if not _env_explicit("BASE_UNIVERSE_SIZE"):
        BASE_UNIVERSE_SIZE = 125
    if not _env_explicit("SECTOR_EXPANSION_SIZE"):
        SECTOR_EXPANSION_SIZE = 55
    if not _env_explicit("SECTOR_MAX_TOTAL_TICKERS"):
        SECTOR_MAX_TOTAL_TICKERS = 200
    if not _env_explicit("MAX_ACTIVE_SECTORS_STRONG"):
        MAX_ACTIVE_SECTORS_STRONG = 4
    if not _env_explicit("SECTOR_FALLBACK_MOMENTUM_COUNT"):
        SECTOR_FALLBACK_MOMENTUM_COUNT = 24
    if not _env_explicit("SECTOR_RS_MIN"):
        SECTOR_RS_MIN = -0.01
    if not _env_explicit("PAPER_MIN_NOTIONAL_MULT"):
        PAPER_MIN_NOTIONAL_MULT = 0.50
    if not _env_explicit("PAPER_MIN_NOTIONAL"):
        PAPER_MIN_NOTIONAL = 2.0
    if not _env_explicit("PAPER_DUST_MAX_NOTIONAL"):
        PAPER_DUST_MAX_NOTIONAL = 0.50
    if not _env_explicit("PAPER_DUST_SKIP_CHUNK_FRAC"):
        PAPER_DUST_SKIP_CHUNK_FRAC = 0.02
    if not _env_explicit("PAPER_EXCESS_CASH_SLEEVE_BOOST"):
        PAPER_EXCESS_CASH_SLEEVE_BOOST = 1.12
    if not _env_explicit("PAPER_EXCESS_CASH_HIGH_BOOST"):
        PAPER_EXCESS_CASH_HIGH_BOOST = 1.35
    if not _env_explicit("PAPER_YIELD_GATE_OVERRIDE"):
        PAPER_YIELD_GATE_OVERRIDE = True
    if not _env_explicit("PAPER_NYSE_SLEEVE_CAP_PCT"):
        PAPER_NYSE_SLEEVE_CAP_PCT = 0.20
    if not _env_explicit("PAPER_NYSE_HIGH_CASH_CAP_PCT"):
        PAPER_NYSE_HIGH_CASH_CAP_PCT = 0.22
    if not _env_explicit("PAPER_NYSE_MAX_EXPOSURE_PCT"):
        PAPER_NYSE_MAX_EXPOSURE_PCT = 0.22
    if not _env_explicit("NYSE_SLEEVE_CAP_PCT"):
        NYSE_SLEEVE_CAP_PCT = 0.20
    if not _env_explicit("PAPER_NO_ROOM_MIN_MULT"):
        PAPER_NO_ROOM_MIN_MULT = 0.25
    if not _env_explicit("PAPER_STAT_ARB_LEG_MIN_MULT"):
        PAPER_STAT_ARB_LEG_MIN_MULT = 1.0
    if not _env_explicit("PAPER_STAT_ARB_MIN_CORR"):
        PAPER_STAT_ARB_MIN_CORR = 0.69
    if not _env_explicit("PAPER_STAT_ARB_MAX_PAIRS"):
        PAPER_STAT_ARB_MAX_PAIRS = 12
    if not _env_explicit("PAPER_STAT_ARB_MAX_PAIRS_EXPANDED"):
        PAPER_STAT_ARB_MAX_PAIRS_EXPANDED = 14
    if not _env_explicit("PAPER_STAT_ARB_MAX_PAIRS_CEILING"):
        PAPER_STAT_ARB_MAX_PAIRS_CEILING = 16
    if not _env_explicit("STAT_ARB_NYSE_OVERLAP_BLOCK_MULT"):
        STAT_ARB_NYSE_OVERLAP_BLOCK_MULT = 2
    if not _env_explicit("PAPER_STAT_ARB_MAX_HOLD_BARS"):
        PAPER_STAT_ARB_MAX_HOLD_BARS = 35
    if not _env_explicit("PAPER_STAT_ARB_Z_ENTRY_BASE"):
        PAPER_STAT_ARB_Z_ENTRY_BASE = 2.0
    if not _env_explicit("PAPER_STAT_ARB_Z_ENTRY_MAX"):
        PAPER_STAT_ARB_Z_ENTRY_MAX = 2.6
    if not _env_explicit("PAPER_STAT_ARB_RISK_REWARD"):
        PAPER_STAT_ARB_RISK_REWARD = 1.6
    if not _env_explicit("PAPER_STAT_ARB_MIN_DOLLAR_VOLUME"):
        PAPER_STAT_ARB_MIN_DOLLAR_VOLUME = 35_000_000
    if not _env_explicit("PAPER_STAT_ARB_TRAILING_ARM_FRAC"):
        PAPER_STAT_ARB_TRAILING_ARM_FRAC = 0.40
    if not _env_explicit("PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC"):
        PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC = 0.25
    if not _env_explicit("PAPER_STAT_ARB_MIN_REVERT_FRAC"):
        PAPER_STAT_ARB_MIN_REVERT_FRAC = 0.55
    if not _env_explicit("PAPER_STAT_ARB_EQUITY_MAX_HOLD_BARS"):
        PAPER_STAT_ARB_EQUITY_MAX_HOLD_BARS = 25
    if not _env_explicit("PAPER_STAT_ARB_MAX_LEG_VOL"):
        PAPER_STAT_ARB_MAX_LEG_VOL = 0.065
    if not _env_explicit("PAPER_STAT_ARB_CONVICTION_MIN_SCALE"):
        PAPER_STAT_ARB_CONVICTION_MIN_SCALE = 0.65
    if not _env_explicit("PAPER_STAT_ARB_CONVICTION_MAX_SCALE"):
        PAPER_STAT_ARB_CONVICTION_MAX_SCALE = 1.50
    if not _env_explicit("PAPER_STAT_ARB_TRAIL_MIN_PROFIT_FRAC"):
        PAPER_STAT_ARB_TRAIL_MIN_PROFIT_FRAC = 0.50
    if not _env_explicit("PAPER_STAT_ARB_COINT_PVALUE"):
        PAPER_STAT_ARB_COINT_PVALUE = 0.12
    if not _env_explicit("PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF"):
        PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF = True
    if not _env_explicit("STAT_ARB_SLEEVE_CAP_ENABLED"):
        STAT_ARB_SLEEVE_CAP_ENABLED = True
    if not _env_explicit("STAT_ARB_SLEEVE_CAP_PCT"):
        STAT_ARB_SLEEVE_CAP_PCT = 0.07
    if not _env_explicit("STAT_ARB_VOL_SCALING_ENABLED"):
        STAT_ARB_VOL_SCALING_ENABLED = True
    if not _env_explicit("STAT_ARB_VOL_MIN_NOTIONAL_SCALE"):
        STAT_ARB_VOL_MIN_NOTIONAL_SCALE = 0.30
    if not _env_explicit("PROTECTIVE_SHORT_ENABLED", "PAPER_PROTECTIVE_SHORT_ENABLED"):
        PROTECTIVE_SHORT_ENABLED = True
    if not _env_explicit("SHORT_OPPORTUNISTIC_ENABLED", "PAPER_SHORT_OPPORTUNISTIC_ENABLED"):
        SHORT_OPPORTUNISTIC_ENABLED = False
    if not _env_explicit("PROTECTIVE_SHORT_MAX_PCT"):
        PROTECTIVE_SHORT_MAX_PCT = 0.18
    if not _env_explicit("PROTECTIVE_SHORT_MIN_PCT"):
        PROTECTIVE_SHORT_MIN_PCT = 0.08
    if not _env_explicit("SHORT_RHYME_E_MAX_PCT"):
        SHORT_RHYME_E_MAX_PCT = 0.12
    if not _env_explicit("SHORT_RHYME_B_MAX_PCT"):
        SHORT_RHYME_B_MAX_PCT = 0.18
    if not _env_explicit("SECTOR_SHORT_ENABLED", "PAPER_SECTOR_SHORT_ENABLED"):
        SECTOR_SHORT_ENABLED = True
    if not _env_explicit("SECTOR_SHORT_MAX_PCT"):
        SECTOR_SHORT_MAX_PCT = 0.08
    if not _env_explicit("SECTOR_SHORT_MIN_BUBBLE_SCORE"):
        SECTOR_SHORT_MIN_BUBBLE_SCORE = 0.55
    if not _env_explicit("SECTOR_SHORT_MAX_SCORE"):
        SECTOR_SHORT_MAX_SCORE = -0.04
    if not _env_explicit("SECTOR_SHORT_MIN_RS_VS_SPY"):
        SECTOR_SHORT_MIN_RS_VS_SPY = -0.03
    if not _env_explicit("SECTOR_SHORT_MAX_POSITIONS"):
        SECTOR_SHORT_MAX_POSITIONS = 2
    if not _env_explicit("SHORT_PROFIT_TARGET_PCT"):
        SHORT_PROFIT_TARGET_PCT = 0.032
    if not _env_explicit("SHORT_STOP_LOSS_PCT"):
        SHORT_STOP_LOSS_PCT = 0.02
    if not _env_explicit("SHORT_RHYME_E_EXHAUSTION_REQUIRED", "PAPER_SHORT_RHYME_E_EXHAUSTION_REQUIRED"):
        SHORT_RHYME_E_EXHAUSTION_REQUIRED = False
    if not _env_explicit("SHORT_BUBBLE_MIN_FOR_RHYME_E"):
        SHORT_BUBBLE_MIN_FOR_RHYME_E = 60.0
    if not _env_explicit("SHORT_RHYME_E_STRONG_BUBBLE"):
        SHORT_RHYME_E_STRONG_BUBBLE = 0.65
    if not _env_explicit("SHORT_MAX_HOLD_BARS"):
        SHORT_MAX_HOLD_BARS = 30
    if not _env_explicit("SHORT_LONG_HEDGE_ENABLED", "PAPER_SHORT_LONG_HEDGE_ENABLED"):
        SHORT_LONG_HEDGE_ENABLED = True
    if not _env_explicit("SHORT_LONG_HEDGE_FLOOR"):
        SHORT_LONG_HEDGE_FLOOR = 0.78
    if not _env_explicit("SHORT_VIX_SPIKE_CONFIRM", "PAPER_SHORT_VIX_SPIKE_CONFIRM"):
        SHORT_VIX_SPIKE_CONFIRM = True
    if not _env_explicit("SHORT_VIX_REQUIRE_RISING", "PAPER_SHORT_VIX_REQUIRE_RISING"):
        SHORT_VIX_REQUIRE_RISING = True
    if not _env_explicit("SHORT_RHYME_B_MIN_DEPTH"):
        SHORT_RHYME_B_MIN_DEPTH = 0.020
    if not _env_explicit("SHORT_RHYME_E_ENABLED", "PAPER_SHORT_RHYME_E_ENABLED"):
        SHORT_RHYME_E_ENABLED = True
    if not _env_explicit("SHORT_BUBBLE_SCORE_MIN"):
        SHORT_BUBBLE_SCORE_MIN = 0.45
    if not _env_explicit("BUFFETT_INDICATOR_ENABLED", "PAPER_BUFFETT_INDICATOR_ENABLED"):
        BUFFETT_INDICATOR_ENABLED = True
    if not _env_explicit("BUFFETT_OVERVALUED_THRESHOLD"):
        BUFFETT_OVERVALUED_THRESHOLD = 200.0
    if not _env_explicit("INSIDER_MONITOR_ENABLED", "PAPER_INSIDER_MONITOR_ENABLED"):
        INSIDER_MONITOR_ENABLED = True
    if not _env_explicit("INSIDER_CLUSTER_MIN_BUYERS"):
        INSIDER_CLUSTER_MIN_BUYERS = 2
    if not _env_explicit("INSIDER_SIGNAL_BOOST_ENABLED", "PAPER_INSIDER_SIGNAL_BOOST_ENABLED"):
        INSIDER_SIGNAL_BOOST_ENABLED = True
    if not _env_explicit("INSIDER_BOOST_ENABLED", "PAPER_INSIDER_BOOST_ENABLED"):
        INSIDER_BOOST_ENABLED = True
    if not _env_explicit("INSIDER_RISK_GUARD_ENABLED", "PAPER_INSIDER_RISK_GUARD_ENABLED"):
        INSIDER_RISK_GUARD_ENABLED = True
    if not _env_explicit("RVOL_SCANNER_ENABLED", "PAPER_RVOL_SCANNER_ENABLED"):
        RVOL_SCANNER_ENABLED = True
    if not _env_explicit("RVOL_MIN_THRESHOLD"):
        RVOL_MIN_THRESHOLD = 2.0
    if not _env_explicit("RVOL_STRONG_THRESHOLD"):
        RVOL_STRONG_THRESHOLD = 3.0
    if not _env_explicit("RVOL_BOOST_FACTOR"):
        RVOL_BOOST_FACTOR = 0.15
    if not _env_explicit("RVOL_MOMENTUM_BOOST_THRESHOLD"):
        RVOL_MOMENTUM_BOOST_THRESHOLD = 2.5
    if not _env_explicit("RVOL_LOOKBACK_DAYS"):
        RVOL_LOOKBACK_DAYS = 10
    if not _env_explicit("ORB_ENABLED", "PAPER_ORB_ENABLED"):
        ORB_ENABLED = True
    if not _env_explicit("ORB_BREAKOUT_MINUTES"):
        ORB_BREAKOUT_MINUTES = 30
    if not _env_explicit("ORB_RVOL_MIN"):
        ORB_RVOL_MIN = 2.0
    if not _env_explicit("ORB_BOOST_FACTOR"):
        ORB_BOOST_FACTOR = 0.18
    if not _env_explicit("ORB_MOMENTUM_ENABLED", "PAPER_ORB_MOMENTUM_ENABLED"):
        ORB_MOMENTUM_ENABLED = True
    if not _env_explicit("ORB_MOMENTUM_RISK_PCT"):
        ORB_MOMENTUM_RISK_PCT = 0.01
    if not _env_explicit("ORB_MOMENTUM_MAX_SIZE_PCT"):
        ORB_MOMENTUM_MAX_SIZE_PCT = 0.10
    if not _env_explicit("ORB_MOMENTUM_MIN_SIZE_PCT"):
        ORB_MOMENTUM_MIN_SIZE_PCT = 0.05
    if not _env_explicit("ORB_MOMENTUM_RR"):
        ORB_MOMENTUM_RR = 1.5
    if not _env_explicit("ORB_MOMENTUM_BACKTEST_ENABLED"):
        ORB_MOMENTUM_BACKTEST_ENABLED = True
    if not _env_explicit("VOL_BREAKOUT_ENABLED", "PAPER_VOL_BREAKOUT_ENABLED"):
        VOL_BREAKOUT_ENABLED = True
        PAPER_VOL_BREAKOUT_ENABLED = True
    if not _env_explicit("VOL_BREAKOUT_RISK_PCT"):
        VOL_BREAKOUT_RISK_PCT = 0.01
    if not _env_explicit("VOL_BREAKOUT_MAX_SIZE_PCT"):
        VOL_BREAKOUT_MAX_SIZE_PCT = 0.08
    if not _env_explicit("VOL_BREAKOUT_ATR_EXPAND_MULT"):
        VOL_BREAKOUT_ATR_EXPAND_MULT = 1.5
    if not _env_explicit("VOL_BREAKOUT_BACKTEST_ENABLED"):
        VOL_BREAKOUT_BACKTEST_ENABLED = True
    if not _env_explicit("SECTOR_ROTATION_ENABLED", "PAPER_SECTOR_ROTATION_ENABLED"):
        SECTOR_ROTATION_ENABLED = True
        PAPER_SECTOR_ROTATION_ENABLED = True
    if not _env_explicit("SECTOR_ROTATION_CAP_PCT"):
        SECTOR_ROTATION_CAP_PCT = 0.20
    if not _env_explicit("SECTOR_ROTATION_MAX_SECTOR_PCT"):
        SECTOR_ROTATION_MAX_SECTOR_PCT = 0.25
    if not _env_explicit("SECTOR_ROTATION_TOP_N"):
        SECTOR_ROTATION_TOP_N = 3
    if not _env_explicit("SECTOR_ROTATION_BACKTEST_ENABLED"):
        SECTOR_ROTATION_BACKTEST_ENABLED = True
    if not _env_explicit("CATALYST_SCORING_ENABLED", "PAPER_CATALYST_SCORING_ENABLED"):
        CATALYST_SCORING_ENABLED = True
    if not _env_explicit("CATALYST_MIN_SCORE"):
        CATALYST_MIN_SCORE = 65.0
    if not _env_explicit("CATALYST_BOOST_FACTOR"):
        CATALYST_BOOST_FACTOR = 0.20
    if not _env_explicit("ATR_SIZING_ENABLED", "PAPER_ATR_SIZING_ENABLED"):
        ATR_SIZING_ENABLED = True
    if not _env_explicit("ATR_PERIOD"):
        ATR_PERIOD = 14
    if not _env_explicit("ATR_RISK_MULTIPLE"):
        ATR_RISK_MULTIPLE = 2.0
    if not _env_explicit("ATR_MAX_SIZE_PCT"):
        ATR_MAX_SIZE_PCT = 0.04
    if not _env_explicit("CONVICTION_SIZING_ENABLED", "PAPER_CONVICTION_SIZING_ENABLED"):
        CONVICTION_SIZING_ENABLED = True
    if not _env_explicit("CONVICTION_MIN_SCALE"):
        CONVICTION_MIN_SCALE = 0.4
    if not _env_explicit("CONVICTION_MAX_SCALE"):
        CONVICTION_MAX_SCALE = 2.0
    if not _env_explicit("NYSE_MA_ENTRY_TOLERANCE_PCT"):
        NYSE_MA_ENTRY_TOLERANCE_PCT = 0.01
    if not _env_explicit("NYSE_RANK_SCANNER_WEIGHT"):
        NYSE_RANK_SCANNER_WEIGHT = 1.35
    if not _env_explicit("NYSE_ENTRY_RVOL_MIN"):
        NYSE_ENTRY_RVOL_MIN = 1.8
    if not _env_explicit("MULTI_TIMEFRAME_ENABLED", "PAPER_MULTI_TIMEFRAME_ENABLED"):
        MULTI_TIMEFRAME_ENABLED = True
    if not _env_explicit("MULTI_TIMEFRAME_MIN_ALIGNMENT"):
        MULTI_TIMEFRAME_MIN_ALIGNMENT = 0.65
    if not _env_explicit("MULTI_TIMEFRAME_BOOST_FACTOR"):
        MULTI_TIMEFRAME_BOOST_FACTOR = 0.22
    if not _env_explicit("EXIT_OPTIMIZATION_ENABLED", "PAPER_EXIT_OPTIMIZATION_ENABLED"):
        EXIT_OPTIMIZATION_ENABLED = True
    if not _env_explicit("PARTIAL_EXIT_RR"):
        PARTIAL_EXIT_RR = 1.0
    if not _env_explicit("TRAIL_ARM_PCT"):
        TRAIL_ARM_PCT = 0.50
    if not _env_explicit("TRAIL_PULLBACK_PCT"):
        TRAIL_PULLBACK_PCT = 0.35
    if not _env_explicit("EXIT_OPTIMIZATION_MAX_HOLD_BARS"):
        EXIT_OPTIMIZATION_MAX_HOLD_BARS = 35
    if not _env_explicit("CORRELATION_GUARD_ENABLED", "PAPER_CORRELATION_GUARD_ENABLED"):
        CORRELATION_GUARD_ENABLED = True
    if not _env_explicit("MAX_PORTFOLIO_CORR"):
        MAX_PORTFOLIO_CORR = 0.65
    if not _env_explicit("CORR_GUARD_MIN_SCALE"):
        CORR_GUARD_MIN_SCALE = 0.60
    if not _env_explicit("SHORT_VIX_MIN_LEVEL"):
        SHORT_VIX_MIN_LEVEL = 22.0
    if not _env_explicit("SHORT_PARTIAL_PROFIT_ENABLED", "PAPER_SHORT_PARTIAL_PROFIT_ENABLED"):
        SHORT_PARTIAL_PROFIT_ENABLED = True
    if not _env_explicit("SHORT_RHYME_E_BEAR_STREAK_BARS"):
        SHORT_RHYME_E_BEAR_STREAK_BARS = 3
    if not _env_explicit("SHORT_RHYME_E_WAIVER_MIN_STREAK"):
        SHORT_RHYME_E_WAIVER_MIN_STREAK = 2
    if not _env_explicit("SHORT_WAIVER_SIZE_MULT"):
        SHORT_WAIVER_SIZE_MULT = 0.75
    if not _env_explicit("SHORT_TRAILING_ARM_FRAC"):
        SHORT_TRAILING_ARM_FRAC = 0.50
    if not _env_explicit("SHORT_TRAILING_PULLBACK_FRAC"):
        SHORT_TRAILING_PULLBACK_FRAC = 0.35
    if not _env_explicit("SHORT_HIGH_VOL_VIX_THRESHOLD"):
        SHORT_HIGH_VOL_VIX_THRESHOLD = 25.0
    if not _env_explicit("SHORT_HIGH_VOL_STOP_MULT"):
        SHORT_HIGH_VOL_STOP_MULT = 0.75
    if not _env_explicit("SHORT_BUBBLE_SIZE_POWER"):
        SHORT_BUBBLE_SIZE_POWER = 1.35
    if not _env_explicit("DYNAMIC_CORE_LOOKBACK_DAYS"):
        DYNAMIC_CORE_LOOKBACK_DAYS = 63
    if not _env_explicit("OLLAMA_MODEL"):
        OLLAMA_MODEL = "qwen2.5:32b"
    if not _env_explicit("OLLAMA_FALLBACK_MODELS"):
        OLLAMA_FALLBACK_MODELS = "qwen2.5-coder:14b,deepseek-r1:8b,llama3.1:8b"
    if not _env_explicit("OLLAMA_USE_CHAT_API"):
        OLLAMA_USE_CHAT_API = True
    if not _env_explicit("OLLAMA_JSON_FORMAT"):
        OLLAMA_JSON_FORMAT = True
    if not _env_explicit("PAPER_THINKING_ENGINE_ENABLED", "THINKING_ENGINE_ENABLED"):
        # Off until Ollama is stable — avoids 90s-cycle watchdog stalls.
        PAPER_THINKING_ENGINE_ENABLED = False
        THINKING_ENGINE_ENABLED = False
    if not _env_explicit("LIVE_THINKING_ENGINE_ENABLED"):
        LIVE_THINKING_ENGINE_ENABLED = False
    if effective_core_allocator_locked():
        try:
            from modules.core_allocator import lock_core_allocator

            lock_core_allocator()
        except ImportError:
            pass


# Subprocess env defaults for portal paper book / run_paper_bot (live book must not inherit).
REALISTIC_RESEARCH_ENV: dict[str, str] = {
    "PAPER_AGGRESSIVE": "true",
    "VTI_CORE_PCT": "0.80",
    "PAPER_VTI_CORE_PCT": "0.40",
    "PAPER_DYNAMIC_VTI": "true",
    "PAPER_DYNAMIC_VTI_ENABLED": "true",
    "DYNAMIC_VTI_PAPER_FLOOR": "0.35",
    "DYNAMIC_VTI_PAPER_CEILING": "0.75",
    "CORE_ALLOCATOR_LOCKED": "false",
    "CORE_ALLOCATOR_LOCKED_CHOICE": "spy",
    "HEARTBEAT_WATCHDOG_TIMEOUT_SEC": "300",
    "PAPER_MAX_EQUITY_TRADES": "3",
    "DEEP_HISTORY_ENABLED": "true",
    "DEEP_HISTORY_INDICATORS_ONLY": "true",
    "RISK_PER_TRADE": "0.018",
    "PAPER_RISK_PER_TRADE": "0.018",
    "PAPER_POSITION_MAX_HOLD_BARS": "30",
    "PAPER_REGIME_B_SIZING_MULT": "0.30",
    "PAPER_REGIME_B_RISK_MULT": "0.50",
    "TAIL_RISK_CONTROLS_ENABLED": "true",
    "VOL_CEILING_ENABLED": "true",
    "PAPER_VOL_CEILING_PCT": "0.17",
    "PORTFOLIO_VOL_CEILING_PCT": "0.18",
    "PORTFOLIO_VOL_MIN_RISK_MULT": "0.35",
    "PORTFOLIO_VOL_WINDOW": "20",
    "PAPER_REGIME_DD_RISK_ENABLED": "true",
    "PAPER_REGIME_D_RISK_MULT": "0.75",
    "PAPER_DD_RISK_WARN_PCT": "0.05",
    "PAPER_DD_RISK_MULT_5": "0.6",
    "PAPER_DD_RISK_SEVERE_PCT": "0.08",
    "PAPER_DD_RISK_MULT_8": "0.3",
    "PAPER_REGIME_B_CASH_BUFFER_BOOST": "0.12",
    "PAPER_MAX_POSITION_PCT": "0.08",
    "PER_NAME_MAX_PCT": "0.08",
    "SECTOR_HIGH_VOL_CEILING_PCT": "0.18",
    "SECTOR_HIGH_VOL_EXPANSION_CAP": "10",
    "SECTOR_HIGH_VOL_MAX_ACTIVE_SECTORS": "1",
    "PAPER_REGIME_WEAK_SLEEVE_MAX_PCT": "0.25",
    "REALISTIC_RESEARCH_VERSION": REALISTIC_RESEARCH_VERSION,
    "WISDOM_LAYER_ENABLED": "false",
    "REBALANCE_ENABLED": "false",
    "COT_OVERLAY_ENABLED": "false",
    "POSITIONING_OVERLAY_ENABLED": "false",
    "STAT_ARB_ENABLED": "true",
    "PAPER_STAT_ARB_ENABLED": "true",
    "PAPER_STAT_ARB_MIN_CORR": "0.69",
    "PAPER_STAT_ARB_MAX_PAIRS": "12",
    "PAPER_STAT_ARB_MAX_PAIRS_EXPANDED": "14",
    "PAPER_STAT_ARB_MAX_PAIRS_CEILING": "16",
    "STAT_ARB_NYSE_OVERLAP_BLOCK_MULT": "2",
    "PAPER_STAT_ARB_MAX_HOLD_BARS": "35",
    "PAPER_STAT_ARB_EQUITY_MAX_HOLD_BARS": "25",
    "PAPER_STAT_ARB_Z_ENTRY_BASE": "2.0",
    "PAPER_STAT_ARB_Z_ENTRY_MAX": "2.6",
    "PAPER_STAT_ARB_RISK_REWARD": "1.6",
    "PAPER_STAT_ARB_MIN_DOLLAR_VOLUME": "35000000",
    "PAPER_STAT_ARB_MIN_REVERT_FRAC": "0.55",
    "PAPER_STAT_ARB_MAX_LEG_VOL": "0.065",
    "PAPER_STAT_ARB_CONVICTION_MIN_SCALE": "0.65",
    "PAPER_STAT_ARB_CONVICTION_MAX_SCALE": "1.50",
    "PAPER_STAT_ARB_TRAIL_MIN_PROFIT_FRAC": "0.50",
    "PAPER_STAT_ARB_TRAILING_ARM_FRAC": "0.40",
    "PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC": "0.25",
    "PAPER_STAT_ARB_COINT_PVALUE": "0.12",
    "PAPER_STAT_ARB_USE_COINT": "true",
    "PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF": "true",
    "PAPER_STAT_ARB_SECTOR_NEUTRAL_BOOST": "1.12",
    "STAT_ARB_SLEEVE_CAP_ENABLED": "true",
    "STAT_ARB_SLEEVE_CAP_PCT": "0.07",
    "STAT_ARB_VOL_SCALING_ENABLED": "true",
    "STAT_ARB_VOL_MIN_NOTIONAL_SCALE": "0.30",
    "PROTECTIVE_SHORT_ENABLED": "true",
    "PROTECTIVE_SHORT_MAX_PCT": "0.18",
    "PROTECTIVE_SHORT_MIN_PCT": "0.08",
    "SHORT_RHYME_E_MAX_PCT": "0.12",
    "SHORT_RHYME_B_MAX_PCT": "0.18",
    "SECTOR_SHORT_ENABLED": "true",
    "SECTOR_SHORT_MAX_PCT": "0.08",
    "SECTOR_SHORT_MIN_BUBBLE_SCORE": "0.55",
    "SECTOR_SHORT_MAX_SCORE": "-0.04",
    "SECTOR_SHORT_MIN_RS_VS_SPY": "-0.03",
    "SECTOR_SHORT_MAX_POSITIONS": "2",
    "SHORT_PROFIT_TARGET_PCT": "0.032",
    "SHORT_STOP_LOSS_PCT": "0.02",
    "SHORT_RHYME_E_EXHAUSTION_REQUIRED": "false",
    "SHORT_BUBBLE_MIN_FOR_RHYME_E": "60",
    "SHORT_VIX_MIN": "22",
    "SHORT_RHYME_E_STRONG_BUBBLE": "0.65",
    "SHORT_MAX_HOLD_BARS": "30",
    "SHORT_LONG_HEDGE_ENABLED": "true",
    "SHORT_LONG_HEDGE_FLOOR": "0.78",
    "INSIDER_MONITOR_ENABLED": "true",
    "INSIDER_SIGNAL_BOOST_ENABLED": "true",
    "INSIDER_BOOST_ENABLED": "true",
    "INSIDER_RISK_GUARD_ENABLED": "true",
    "INSIDER_CLUSTER_BOOST_MAX": "0.30",
    "INSIDER_SELL_SHORT_BOOST_MAX": "0.58",
    "INSIDER_TIER1_CLUSTER_MIN_SCORE": "80",
    "INSIDER_TIER1_MOMENTUM_BOOST": "0.28",
    "INSIDER_TIER2_MOMENTUM_BOOST": "0.18",
    "INSIDER_TIER1_STAT_ARB_MULT": "1.22",
    "INSIDER_TIER2_STAT_ARB_MULT": "1.15",
    "INSIDER_TIER1_SHORT_BOOST": "0.42",
    "INSIDER_TIER2_SHORT_BOOST": "0.42",
    "INSIDER_SHORT_AMPLIFIED_BOOST": "0.55",
    "INSIDER_BUBBLE_BULLISH_SUPPRESS": "80",
    "INSIDER_BUBBLE_SHORT_AMPLIFY_SCORE": "65",
    "INSIDER_RHYME_B_BULLISH_MULT": "0.50",
    "INSIDER_MAX_BOOSTED_POSITIONS": "3",
    "INSIDER_SINGLE_NAME_CAP_PCT": "0.05",
    "INSIDER_CLUSTER_MIN_BUYERS": "2",
    "RVOL_SCANNER_ENABLED": "true",
    "RVOL_MIN_THRESHOLD": "2.0",
    "RVOL_STRONG_THRESHOLD": "3.0",
    "RVOL_BOOST_FACTOR": "0.15",
    "RVOL_MOMENTUM_BOOST_THRESHOLD": "2.5",
    "RVOL_LOOKBACK_DAYS": "10",
    "ORB_ENABLED": "true",
    "ORB_BREAKOUT_MINUTES": "30",
    "ORB_RVOL_MIN": "2.0",
    "ORB_BOOST_FACTOR": "0.18",
    "ORB_MOMENTUM_ENABLED": "true",
    "ORB_MOMENTUM_RISK_PCT": "0.01",
    "ORB_MOMENTUM_MAX_SIZE_PCT": "0.10",
    "ORB_MOMENTUM_RR": "1.5",
    "ORB_MOMENTUM_BACKTEST_ENABLED": "true",
    "VOL_BREAKOUT_ENABLED": "true",
    "PAPER_VOL_BREAKOUT_ENABLED": "true",
    "VOL_BREAKOUT_RISK_PCT": "0.01",
    "VOL_BREAKOUT_MAX_SIZE_PCT": "0.08",
    "VOL_BREAKOUT_ATR_EXPAND_MULT": "1.5",
    "VOL_BREAKOUT_BACKTEST_ENABLED": "true",
    "SECTOR_ROTATION_ENABLED": "true",
    "PAPER_SECTOR_ROTATION_ENABLED": "true",
    "SECTOR_ROTATION_CAP_PCT": "0.20",
    "SECTOR_ROTATION_MAX_SECTOR_PCT": "0.25",
    "SECTOR_ROTATION_TOP_N": "3",
    "SECTOR_ROTATION_LIVE_SLEEVE": "false",
    "SECTOR_ROTATION_BACKTEST_ENABLED": "true",
    "CATALYST_SCORING_ENABLED": "true",
    "CATALYST_MIN_SCORE": "65",
    "CATALYST_BOOST_FACTOR": "0.20",
    "ATR_SIZING_ENABLED": "true",
    "ATR_PERIOD": "14",
    "ATR_RISK_MULTIPLE": "2.0",
    "ATR_MAX_SIZE_PCT": "0.04",
    "CONVICTION_SIZING_ENABLED": "true",
    "CONVICTION_MIN_SCALE": "0.4",
    "CONVICTION_MAX_SCALE": "2.0",
    "NYSE_MA_ENTRY_TOLERANCE_PCT": "0.01",
    "NYSE_RANK_SCANNER_WEIGHT": "1.35",
    "NYSE_ENTRY_RVOL_MIN": "1.8",
    "MULTI_TIMEFRAME_ENABLED": "true",
    "MULTI_TIMEFRAME_MIN_ALIGNMENT": "0.65",
    "MULTI_TIMEFRAME_BOOST_FACTOR": "0.22",
    "EXIT_OPTIMIZATION_ENABLED": "true",
    "PARTIAL_EXIT_RR": "1.0",
    "TRAIL_ARM_PCT": "0.50",
    "TRAIL_PULLBACK_PCT": "0.35",
    "EXIT_OPTIMIZATION_MAX_HOLD_BARS": "35",
    "CORRELATION_GUARD_ENABLED": "true",
    "MAX_PORTFOLIO_CORR": "0.65",
    "CORR_GUARD_MIN_SCALE": "0.60",
    "CORR_GUARD_CEILING": "0.85",
    "SHORT_OPPORTUNISTIC_ENABLED": "false",
    "SHORT_VIX_SPIKE_CONFIRM": "true",
    "SHORT_VIX_MIN_LEVEL": "22",
    "SHORT_VIX_REQUIRE_RISING": "true",
    "SHORT_RHYME_E_BEAR_STREAK_BARS": "3",
    "SHORT_RHYME_E_WAIVER_MIN_STREAK": "2",
    "SHORT_PARTIAL_PROFIT_ENABLED": "true",
    "SHORT_PARTIAL_PROFIT_FRAC": "0.50",
    "SHORT_PARTIAL_PROFIT_RR": "1.0",
    "DYNAMIC_CORE_LOOKBACK_DAYS": "63",
    "BUFFETT_INDICATOR_ENABLED": "true",
    "SHORT_RHYME_E_ENABLED": "true",
    "SHORT_BUBBLE_SCORE_MIN": "0.45",
    "PAPER_THINKING_ENGINE_ENABLED": "false",
    "LIVE_THINKING_ENGINE_ENABLED": "false",
    "THINKING_ENGINE_ENABLED": "false",
    "OLLAMA_MODEL": "qwen2.5:32b",
    "OLLAMA_FALLBACK_MODELS": "qwen2.5-coder:14b,deepseek-r1:8b,llama3.1:8b",
    "OLLAMA_USE_CHAT_API": "true",
    "OLLAMA_JSON_FORMAT": "true",
    "DYNAMIC_SECTOR_SCREENER_ENABLED": "true",
    "BASE_UNIVERSE_SIZE": "125",
    "SECTOR_EXPANSION_SIZE": "55",
    "SECTOR_MAX_TOTAL_TICKERS": "200",
    "SECTOR_FALLBACK_MOMENTUM_COUNT": "24",
    "SECTOR_RS_MIN": "-0.01",
    "PAPER_MIN_NOTIONAL_MULT": "0.50",
    "PAPER_MIN_NOTIONAL": "2.0",
    "PAPER_EXCESS_CASH_HIGH_BOOST": "1.35",
    "PAPER_EXCESS_CASH_DEPLOY_THRESHOLD_PCT": "0.20",
    "PAPER_EXCESS_CASH_THRESHOLD_PCT": "0.15",
    "PAPER_EXCESS_CASH_HIGH_THRESHOLD_PCT": "0.30",
    "PAPER_NO_ROOM_MIN_MULT": "0.25",
    "PAPER_DUST_MAX_NOTIONAL": "0.50",
    "PAPER_DUST_SKIP_CHUNK_FRAC": "0.02",
    "PAPER_EXCESS_CASH_SLEEVE_BOOST": "1.12",
    "PAPER_YIELD_GATE_OVERRIDE": "true",
    "PAPER_NYSE_SLEEVE_CAP_PCT": "0.20",
    "PAPER_NYSE_HIGH_CASH_CAP_PCT": "0.22",
    "PAPER_NYSE_MAX_EXPOSURE_PCT": "0.22",
    "NYSE_SLEEVE_CAP_PCT": "0.20",
    "SECTOR_EXPANSION_SIZE": "45",
    "SECTOR_MAX_TOTAL_TICKERS": "180",
    "MAX_ACTIVE_SECTORS_STRONG": "4",
    "SECTOR_FALLBACK_MOMENTUM_COUNT": "18",
    "EMAIL_WEEKLY_SUMMARY_ENABLED": "false",
    "TELEGRAM_WEEKLY_SUMMARY_ENABLED": "true",
}


def apply_realistic_research_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Merge locked realistic research defaults (setdefault — explicit env wins)."""
    merged = dict(env or os.environ)
    for key, value in REALISTIC_RESEARCH_ENV.items():
        merged.setdefault(key, value)
    return merged


def clear_paper_research_env(env: dict[str, str]) -> dict[str, str]:
    """Strip paper-research keys from subprocess env (live alpaca_live book)."""
    out = dict(env)
    out.pop("PAPER_CHASE_MODE", None)
    for key in REALISTIC_RESEARCH_ENV:
        out.pop(key, None)
    return out


def is_realistic_research_active() -> bool:
    """True when paper chase / Profile B book is running."""
    return bool(PAPER_TRADING and paper_chase_mode_enabled())


def format_paper_live_profile_line() -> str:
    """Cross-book startup line: paper v1.5 aggressive vs live conservative."""
    return (
        f">>> PAPER BOT: Realistic Research v{REALISTIC_RESEARCH_VERSION} (Aggressive) | "
        f"{REALISTIC_RESEARCH_TAGLINE} | Live Bot: Conservative {LIVE_VTI_CORE_PCT:.0%} VTI"
    )


def format_realistic_research_tagline() -> str:
    return REALISTIC_RESEARCH_TAGLINE


def format_universe_pool_label() -> str:
    """Compact universe cap summary for banners."""
    return (
        f"universe base {BASE_UNIVERSE_SIZE} + sector x{SECTOR_EXPANSION_SIZE} "
        f"(cap {SECTOR_MAX_TOTAL_TICKERS})"
    )


def format_realistic_research_headline() -> str:
    """Prominent version line for paper bot startup."""
    features = (
        "RVOL + ORB + Catalyst + ATR | Tuned Shorts 8-18% | Sector Shorts | "
        "Dynamic Core 63d | Insider Boosts | Stat Arb 12-16p v1.5.2 | Thinking ON"
    )
    return (
        f">>> REALISTIC RESEARCH v{REALISTIC_RESEARCH_VERSION} (LOCKED) - "
        f"{REALISTIC_RESEARCH_TAGLINE} | {features} | Paper Bot Default <<<"
    )


def format_realistic_research_banner() -> str:
    """Detail line for the locked realistic research profile."""
    sector_on = effective_dynamic_sector_screener() or (
        DYNAMIC_SECTOR_SCREENER_ENABLED and PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED
    )
    sector_flag = "ON" if sector_on else "OFF"
    tail_flag = "ON" if effective_tail_risk_controls() else "OFF"
    core_pct = vti_core_allocation_pct()
    if effective_dynamic_core_enabled():
        from modules.core_allocator import core_allocator_snapshot

        snap = core_allocator_snapshot()
        vti_sh = (snap.get("metrics") or {}).get("vti", {}).get("sharpe", 0.0)
        spy_sh = (snap.get("metrics") or {}).get("spy", {}).get("sharpe", 0.0)
        core_label = (
            f"Dynamic {VTI_CORE_SYMBOL}/{SPY_BOT_SYMBOL} "
            f"{DYNAMIC_CORE_MIN_PCT:.0%}-{DYNAMIC_CORE_MAX_PCT:.0%} "
            f"@ {core_pct:.0%} (Sharpe VTI {vti_sh:.2f} vs SPY {spy_sh:.2f})"
        )
    elif effective_core_allocator_locked():
        from modules.core_allocator import current_core_choice

        core_label = f"{current_core_choice().upper()} @ {core_pct:.0%} core (locked)"
    else:
        core_label = f"{VTI_CORE_PCT:.0%} {VTI_CORE_SYMBOL} core"
    short_note = ""
    if effective_opportunistic_short_enabled():
        short_note = f" | {format_opportunistic_short_banner()}"
    return (
        f"Tail Risk: {tail_flag} | {core_label} | "
        f"risk {RISK_PER_TRADE:.1%} | hold {PAPER_POSITION_MAX_HOLD_BARS}d | "
        f"vol cap {PAPER_VOL_CEILING_PCT:.0%} | RHYME_B x{PAPER_REGIME_B_SIZING_MULT:.2f} | "
        f"sector {sector_flag} | {format_universe_pool_label()}"
        f"{short_note}"
    )


def format_realistic_research_startup_lines() -> list[str]:
    """Multi-line startup block: headline, stat arb, profile details."""
    lines = [
        f">>> {REALISTIC_RESEARCH_TAGLINE} <<<",
        format_realistic_research_headline(),
        f">>> {REALISTIC_RESEARCH_FEATURE_DETAIL} <<<",
    ]
    stat_line = format_stat_arb_research_line()
    if stat_line:
        lines.append(stat_line)
    try:
        from modules.volume_analysis import format_rvol_scanner_banner

        rvol_line = format_rvol_scanner_banner()
        if rvol_line:
            lines.append(rvol_line)
    except Exception:
        pass
    try:
        from modules.orb_strategy import format_orb_scanner_banner

        orb_line = format_orb_scanner_banner()
        if orb_line:
            lines.append(orb_line)
    except Exception:
        pass
    try:
        from modules.orb_momentum_sleeve import format_orb_momentum_banner

        mom_line = format_orb_momentum_banner()
        if mom_line:
            lines.append(mom_line)
    except Exception:
        pass
    try:
        from modules.vol_breakout_sleeve import format_vol_breakout_banner

        vol_bo_line = format_vol_breakout_banner()
        if vol_bo_line:
            lines.append(vol_bo_line)
    except Exception:
        pass
    try:
        from modules.sector_rotation import format_sector_rotation_banner

        rot_line = format_sector_rotation_banner()
        if rot_line:
            lines.append(rot_line)
    except Exception:
        pass
    try:
        from modules.catalyst_scoring import format_catalyst_scanner_banner

        catalyst_line = format_catalyst_scanner_banner()
        if catalyst_line:
            lines.append(catalyst_line)
    except Exception:
        pass
    try:
        from modules.risk_management import format_atr_sizing_banner

        atr_line = format_atr_sizing_banner()
        if atr_line:
            lines.append(atr_line)
    except Exception:
        pass
    try:
        from modules.risk_management import format_conviction_sizing_banner

        conv_line = format_conviction_sizing_banner()
        if conv_line:
            lines.append(conv_line)
    except Exception:
        pass
    try:
        from modules.multi_timeframe import format_multi_timeframe_banner

        mtf_line = format_multi_timeframe_banner()
        if mtf_line:
            lines.append(mtf_line)
    except Exception:
        pass
    try:
        from modules.exit_management import format_exit_optimization_banner

        exit_line = format_exit_optimization_banner()
        if exit_line:
            lines.append(exit_line)
    except Exception:
        pass
    try:
        from modules.risk_management import format_correlation_guard_banner

        corr_line = format_correlation_guard_banner()
        if corr_line:
            lines.append(corr_line)
    except Exception:
        pass
    try:
        from modules.ollama_client import format_ollama_status_line

        ollama_line = format_ollama_status_line()
        if ollama_line:
            lines.append(ollama_line)
    except Exception:
        pass
    lines.append(format_realistic_research_banner())
    if paper_chase_mode_enabled() or paper_aggressive_context():
        try:
            from modules.strategy_performance import format_strategy_health_banner

            strat_line = format_strategy_health_banner()
            if strat_line:
                lines.append(strat_line)
        except Exception:
            pass
        try:
            from modules.bot_health import format_health_line, gather_health_context, calculate_health_score

            ctx = gather_health_context()
            health = calculate_health_score(**ctx)
            lines.append(format_health_line(health))
        except Exception:
            pass
    return lines


def realistic_research_profile_snapshot() -> dict[str, bool | float | str]:
    """Serializable snapshot for reports / experiment exports."""
    return {
        "version": REALISTIC_RESEARCH_VERSION,
        "tail_risk_controls": effective_tail_risk_controls(),
        "vol_ceiling_pct": PAPER_VOL_CEILING_PCT,
        "portfolio_vol_ceiling_pct": PORTFOLIO_VOL_CEILING_PCT,
        "dd_risk_warn_pct": PAPER_DD_RISK_WARN_PCT,
        "dd_risk_severe_pct": PAPER_DD_RISK_SEVERE_PCT,
        "per_name_max_pct": PAPER_MAX_POSITION_PCT,
        "vti_core_pct": VTI_CORE_PCT,
        "deep_history_enabled": DEEP_HISTORY_ENABLED,
        "deep_history_indicators_only": DEEP_HISTORY_INDICATORS_ONLY,
        "risk_per_trade": RISK_PER_TRADE,
        "paper_position_max_hold_bars": PAPER_POSITION_MAX_HOLD_BARS,
        "paper_regime_b_sizing_mult": PAPER_REGIME_B_SIZING_MULT,
        "wisdom_layer_enabled": REBALANCE_ENABLED,
        "cot_overlay_enabled": POSITIONING_OVERLAY_ENABLED,
        "dynamic_core_enabled": DYNAMIC_CORE_ENABLED,
        "paper_dynamic_vti": PAPER_DYNAMIC_VTI_ENABLED,
    }


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
        extras.insert(0, f"realistic_research_v{REALISTIC_RESEARCH_VERSION}")
        extras.insert(1, "best_paper_stack_v3_locked")
    return extras


def set_paper_aggressive_context(active: bool) -> None:
    """Thread-local style flag: paper research runner / social paper book."""
    global _paper_aggressive_ctx
    _paper_aggressive_ctx = bool(active)
    if _paper_aggressive_ctx:
        enforce_realistic_research_profile()


def paper_aggressive_context() -> bool:
    return PAPER_AGGRESSIVE_ENABLED and _paper_aggressive_ctx


def research_mode_ready() -> bool:
    """Paper aggressive with stat arb sleeve (research / attribution stack)."""
    if not paper_aggressive_context():
        return False
    return effective_stat_arb_enabled()


def format_research_mode_banner() -> str | None:
    if not research_mode_ready():
        return None
    if effective_crypto_enabled():
        return (
            "Research Mode Ready: stat arb + crypto ON — "
            "see PAPER_RESEARCH_PROFILE.md"
        )
    return (
        "Research Mode Ready: stat arb ON | crypto sleeve OFF "
        "(PAPER_CRYPTO_ENABLED=false) — see PAPER_RESEARCH_PROFILE.md"
    )


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
        "positioning_overlay": effective_positioning_overlay_enabled(),
        "dynamic_core": effective_dynamic_core_enabled(),
        "options": effective_options_sleeve_enabled(),
        "macro_regime": effective_macro_regime_adaptor_enabled(),
        "thinking_engine": effective_thinking_engine_enabled(),
        "risk_parity": effective_risk_parity_enabled(),
        "stat_arb_optimized": False,
        "social": effective_social_sleeve_enabled(),
        "nyse_overlap": PAPER_NYSE_OVERLAP_FILTER_ENABLED,
        "nyse_conditional": PAPER_NYSE_CONDITIONAL_ON_SPY,
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
        "nyse_conditional": PAPER_NYSE_CONDITIONAL_ON_SPY,
        "adaptive_chunk": PAPER_ADAPTIVE_CHUNK_ENABLED,
        "cofire_budget": PAPER_COFIRE_BUDGET_ENABLED,
        "spy_exit_on_ma_break": PAPER_SPY_EXIT_ON_MA_BREAK,
    }


def apply_paper_sleeve_flags(flags: dict[str, bool]) -> None:
    """Set PAPER_* sleeve flags (used by backtester A/B)."""
    global PAPER_NYSE_OVERLAP_FILTER_ENABLED
    global PAPER_NYSE_CONDITIONAL_ON_SPY
    global PAPER_ADAPTIVE_CHUNK_ENABLED
    global PAPER_COFIRE_BUDGET_ENABLED
    global PAPER_SPY_EXIT_ON_MA_BREAK
    if "nyse_overlap" in flags:
        PAPER_NYSE_OVERLAP_FILTER_ENABLED = bool(flags["nyse_overlap"])
    if "nyse_conditional" in flags:
        PAPER_NYSE_CONDITIONAL_ON_SPY = bool(flags["nyse_conditional"])
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


def effective_nyse_conditional_on_spy() -> bool:
    """Stricter NYSE vs SPY filter — paper aggressive / chase only."""
    if not (paper_only_sleeves_active() or paper_aggressive_context()):
        return False
    if not NYSE_CONDITIONAL_ON_SPY or not PAPER_NYSE_CONDITIONAL_ON_SPY:
        return False
    return True


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


def short_broad_symbols() -> tuple[str, ...]:
    out: list[str] = []
    for raw in SHORT_BROAD_SYMBOLS_RAW.split(","):
        sym = raw.strip().upper()
        if sym:
            out.append(sym)
    return tuple(out or (SPY_BOT_SYMBOL, "QQQ"))


def effective_protective_short_enabled() -> bool:
    return bool(PROTECTIVE_SHORT_ENABLED)


def effective_short_opportunistic_single_names() -> bool:
    return bool(SHORT_OPPORTUNISTIC_ENABLED)


def effective_protective_short_max_pct(regime: str | None = None) -> float:
    reg = str(regime or "")
    if "RHYME_B" in reg:
        return max(0.0, min(0.50, float(SHORT_RHYME_B_MAX_PCT)))
    if "RHYME_E" in reg:
        return max(0.0, min(0.50, float(SHORT_RHYME_E_MAX_PCT)))
    return max(0.0, min(0.50, float(PROTECTIVE_SHORT_MAX_PCT)))


def effective_sector_short_min_bubble() -> float:
    """Normalized bubble floor for sector ETF shorts (accepts 55 or 0.55)."""
    v = float(SECTOR_SHORT_MIN_BUBBLE_SCORE)
    return v / 100.0 if v > 1.0 else v


def effective_protective_short_min_pct() -> float:
    lo = max(0.0, float(PROTECTIVE_SHORT_MIN_PCT))
    return min(lo, effective_protective_short_max_pct())


def effective_opportunistic_short_enabled() -> bool:
    """Directional shorts — paper/research only; never live Profile A."""
    if not effective_protective_short_enabled():
        return False
    return bool(paper_only_sleeves_active() or backtest_paper_sleeves_context())


def effective_short_bubble_min_for_rhyme_e() -> float:
    """Bubble Risk Score floor for RHYME_E shorts (accepts 60 or 0.60)."""
    v = float(SHORT_BUBBLE_MIN_FOR_RHYME_E)
    return v / 100.0 if v > 1.0 else v


def effective_buffett_indicator_enabled() -> bool:
    return bool(BUFFETT_INDICATOR_ENABLED)


def effective_short_rhyme_e_exhaustion_required() -> bool:
    return bool(SHORT_RHYME_E_EXHAUSTION_REQUIRED)


def effective_sector_short_enabled() -> bool:
    """Sector ETF shorts — paper/research only."""
    if not effective_opportunistic_short_enabled():
        return False
    return bool(SECTOR_SHORT_ENABLED)


def format_opportunistic_short_banner() -> str:
    if not effective_opportunistic_short_enabled():
        return "Protective Shorts: OFF"
    lo = effective_protective_short_min_pct()
    hi = effective_protective_short_max_pct()
    line = (
        f"Protective Shorts: ON ({lo:.0%}-{hi:.0%} gross, "
        f"RHYME_E<={SHORT_RHYME_E_MAX_PCT:.0%} RHYME_B<={SHORT_RHYME_B_MAX_PCT:.0%}, "
        f"partial@{SHORT_PARTIAL_PROFIT_RR:.0f}:1, trail {SHORT_TRAILING_ARM_FRAC:.0%}/{SHORT_TRAILING_PULLBACK_FRAC:.0%})"
    )
    if not effective_short_rhyme_e_exhaustion_required():
        line += " | RHYME_E bear-streak+depth"
    if effective_sector_short_enabled():
        line += f" | Sector shorts <={SECTOR_SHORT_MAX_PCT:.0%}/name"
    return line


def effective_crypto_v2_enabled() -> bool:
    """Dual-entry crypto sleeve (MR + breakout) — paper aggressive only, default off."""
    if not paper_only_sleeves_active() or not PAPER_CRYPTO_V2_ENABLED:
        return False
    return True


def effective_paper_soft_pause() -> bool:
    """Paper-only: in PAUSED_REGIMES, size down instead of blocking new entries."""
    if effective_regime_dynamic_sizing():
        return False
    if not PAPER_SOFT_PAUSE_ENABLED:
        return False
    return bool(
        paper_only_sleeves_active()
        or paper_aggressive_context()
        or backtest_paper_sleeves_context()
    )


def effective_regime_dynamic_sizing() -> bool:
    """Paper aggressive: per-RHYME sizing multipliers instead of hard entry pauses."""
    if not PAPER_REGIME_DYNAMIC_SIZING_ENABLED:
        return False
    return bool(
        paper_only_sleeves_active()
        or paper_aggressive_context()
        or backtest_paper_sleeves_context()
    )


def format_smart_dynamic_vti_lock_banner() -> str:
    """Static lock banner for Realistic Research paper-aggressive startup."""
    return (
        f">>> SMART DYNAMIC VTI DEFAULT — {DYNAMIC_VTI_PAPER_FLOOR:.0%}-{DYNAMIC_VTI_PAPER_CEILING:.0%} {VTI_CORE_SYMBOL} | "
        "drivers: NYSE/metals momentum, insider clusters, bubble/Buffett, regime, VTI vs SPY"
    )


def paper_fixed_vti_ceiling() -> float | None:
    """When paper aggressive with fixed VTI, cap core pct for sleeve room."""
    if effective_dynamic_core_enabled():
        return None
    if not (paper_aggressive_context() or backtest_paper_sleeves_context()):
        return None
    if PAPER_DYNAMIC_VTI_ENABLED:
        return None
    bt_ceiling = backtest_vti_ceiling()
    if bt_ceiling is not None:
        return bt_ceiling
    return float(PAPER_VTI_CORE_PCT)


def clamp_paper_vti_core(pct: float) -> float:
    """Clamp paper VTI core into the Smart Dynamic band when enabled, else fixed ceiling."""
    if PAPER_DYNAMIC_VTI_ENABLED and (
        paper_aggressive_context()
        or backtest_paper_sleeves_context()
        or paper_only_sleeves_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    ):
        lo = float(DYNAMIC_VTI_PAPER_FLOOR)
        hi = float(DYNAMIC_VTI_PAPER_CEILING)
        if hi < lo:
            lo, hi = hi, lo
        return round(min(hi, max(lo, float(pct))), 6)
    ceiling = paper_fixed_vti_ceiling()
    if ceiling is None:
        return float(pct)
    return round(min(float(pct), ceiling), 6)


def effective_vol_ceiling_pct() -> float:
    """Annualized vol cap for risk scaling — paper-aggressive uses PAPER_VOL_CEILING_PCT."""
    if paper_aggressive_context() or backtest_paper_sleeves_context():
        return float(PAPER_VOL_CEILING_PCT)
    return float(VOL_CEILING_PCT)


def effective_paper_risk_calm_pct() -> float:
    """Calm-bull risk cap for paper-aggressive dynamic risk tiers."""
    if paper_aggressive_context() or backtest_paper_sleeves_context():
        return float(PAPER_RISK_PER_TRADE)
    return float(PAPER_RISK_CALM_BULL_PCT)


def effective_spy_ma_window() -> int:
    """SPY trend MA — paper-aggressive uses tuned PAPER_SPY_MA_WINDOW (default 160)."""
    if paper_aggressive_context() or backtest_paper_sleeves_context():
        return int(PAPER_SPY_MA_WINDOW)
    return int(SPY_MA_WINDOW)


def effective_nyse_ma_window() -> int:
    """NYSE momentum MA — paper-aggressive uses tuned PAPER_NYSE_MA_WINDOW (default 70)."""
    if paper_aggressive_context() or backtest_paper_sleeves_context():
        return int(PAPER_NYSE_MA_WINDOW)
    return int(NYSE_MA_WINDOW)


def paper_tuned_defaults_line() -> str:
    """One-line summary of paper-aggressive defaults (tail-risk tuning Option A)."""
    return (
        f">>> PAPER TUNED DEFAULTS (tail-risk Option A) — "
        f"SPY MA{PAPER_SPY_MA_WINDOW} | NYSE MA{PAPER_NYSE_MA_WINDOW} | "
        f"risk {PAPER_RISK_PER_TRADE:.1%} calm / {PAPER_RISK_MODERATE_PCT:.1%} mod / "
        f"{PAPER_RISK_STRESS_PCT:.1%} stress | RHYME_B x{PAPER_REGIME_B_SIZING_MULT:.2f} | "
        f"RHYME_E x{PAPER_REGIME_E_SIZING_MULT:.2f} | "
        f"max hold {PAPER_POSITION_MAX_HOLD_BARS} bars | "
        f"vol cap {PAPER_VOL_CEILING_PCT:.0%} | "
        f"per-name cap {PAPER_MAX_POSITION_PCT:.0%} | "
        f"DD risk x{PAPER_DD_RISK_MULT_5:.1f} @ {PAPER_DD_RISK_WARN_PCT:.0%}+ / "
        f"x{PAPER_DD_RISK_MULT_8:.1f} @ {PAPER_DD_RISK_SEVERE_PCT:.0%}+"
    )


def paper_frequency_mode_lines() -> list[str]:
    """Startup lines for paper-aggressive trade-frequency tuning (soft pause + fixed VTI)."""
    if not (
        paper_only_sleeves_active()
        or paper_aggressive_context()
        or backtest_paper_sleeves_context()
    ):
        return []
    lines: list[str] = [paper_tuned_defaults_line()]
    if effective_regime_dynamic_sizing():
        lines.append(
            ">>> PAPER REGIME DYNAMIC SIZING — entries always on; "
            f"A x{PAPER_REGIME_A_SIZING_MULT:.1f} | C x{PAPER_REGIME_C_SIZING_MULT:.1f} | "
            f"D x{PAPER_REGIME_D_SIZING_MULT:.1f} | E x{PAPER_REGIME_E_SIZING_MULT:.1f} | "
            f"B x{PAPER_REGIME_B_SIZING_MULT:.1f} "
            f"| weak B/D/E sleeve cap {PAPER_REGIME_WEAK_SLEEVE_MAX_PCT:.0%} "
            f"(hysteresis bump {REGIME_HYSTERESIS_SENTIMENT_BUMP:.2f}, "
            f"dwell {REGIME_MIN_DWELL_BARS} bars)"
        )
    elif effective_paper_soft_pause():
        lines.append(
            ">>> PAPER SOFT PAUSE ENABLED - higher frequency, "
            f"{PAPER_SOFT_PAUSE_SIZING_MULT:.0%} sizing on pause regimes "
            "(entries allowed; daily loss circuit still blocks)"
        )
    if not PAPER_DYNAMIC_VTI_ENABLED:
        ceiling = paper_fixed_vti_ceiling() or PAPER_VTI_CORE_PCT
        lines.append(
            f">>> PAPER FIXED VTI - {ceiling:.0%} {VTI_CORE_SYMBOL} core | "
            f"{1.0 - ceiling:.0%} active sleeve budget (thinking/RP capped)"
        )
    elif paper_aggressive_context() or backtest_paper_sleeves_context():
        lines.append(format_smart_dynamic_vti_lock_banner())
    elif effective_paper_soft_pause():
        lines.append(
            ">>> TIP: PAPER_DYNAMIC_VTI=false + PAPER_VTI_CORE_PCT=0.80 "
            "frees sleeve room (fewer no_room skips)"
        )
    if effective_positioning_overlay_enabled():
        from modules.positioning_overlay import format_positioning_banner

        cot_line = format_positioning_banner()
        if cot_line:
            lines.append(cot_line)
    if effective_dynamic_core_enabled() or effective_core_allocator_locked():
        from modules.core_allocator import format_core_allocator_banner

        core_line = format_core_allocator_banner()
        if core_line:
            lines.append(core_line)
    stat_line = format_stat_arb_research_line()
    if stat_line:
        lines.append(stat_line)
    if effective_dynamic_sector_screener() or (
        DYNAMIC_SECTOR_SCREENER_ENABLED and PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED
    ):
        from modules.sector_screener import format_sector_screener_banner

        sector_line = format_sector_screener_banner()
        if sector_line:
            lines.append(sector_line)
    if KIMI_API_ENABLED or effective_kimi_deep_thinker_enabled():
        from modules.kimi_client import format_kimi_deep_thinker_banner

        kimi_line = format_kimi_deep_thinker_banner()
        if kimi_line:
            lines.append(kimi_line)
    if effective_insider_monitor_enabled():
        from modules.insider_monitor import format_insider_monitor_banner

        insider_line = format_insider_monitor_banner()
        if insider_line:
            lines.append(insider_line)
    return lines


def effective_rvol_scanner_enabled() -> bool:
    """Relative volume filter/boost — paper/research; live when ORB momentum sleeve opted in."""
    if not RVOL_SCANNER_ENABLED:
        return False
    if orb_momentum_live_sleeve_enabled() and not PAPER_TRADING:
        return True
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_orb_enabled() -> bool:
    """Opening range breakout scanner — paper/research; live when ORB momentum sleeve opted in."""
    if not ORB_ENABLED:
        return False
    if orb_momentum_live_sleeve_enabled() and not PAPER_TRADING:
        return True
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def orb_momentum_live_sleeve_enabled() -> bool:
    """Small live ORB+RVOL sleeve opt-in (default off)."""
    return bool(ORB_MOMENTUM_ENABLED and ORB_MOMENTUM_LIVE_SLEEVE)


def effective_orb_momentum_enabled() -> bool:
    """Paper ORB momentum sleeve, or live when ORB_MOMENTUM_LIVE_SLEEVE=true."""
    if not ORB_MOMENTUM_ENABLED:
        return False
    if PAPER_TRADING or paper_aggressive_context() or backtest_paper_sleeves_context():
        return True
    if is_realistic_research_active():
        return True
    return orb_momentum_live_sleeve_enabled()


def effective_vol_breakout_enabled() -> bool:
    """ATR volatility-breakout sleeve — paper / research only (no live)."""
    if not VOL_BREAKOUT_ENABLED:
        return False
    if not (
        PAPER_TRADING
        or paper_aggressive_context()
        or backtest_paper_sleeves_context()
        or is_realistic_research_active()
    ):
        return False
    return bool(PAPER_VOL_BREAKOUT_ENABLED or VOL_BREAKOUT_ENABLED)


def effective_catalyst_scoring_enabled() -> bool:
    """Catalyst scoring — paper/research only."""
    if not CATALYST_SCORING_ENABLED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_historical_news_enabled() -> bool:
    """Date-shifted headline proxies for backtests (paper-aggressive only)."""
    if not HISTORICAL_NEWS_ENABLED:
        return False
    return bool(backtest_paper_sleeves_context())


def effective_atr_sizing_enabled() -> bool:
    """ATR-based position sizing — paper/research only."""
    if not ATR_SIZING_ENABLED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_conviction_sizing_enabled() -> bool:
    """Conviction-based position sizing — paper/research only."""
    if not CONVICTION_SIZING_ENABLED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_multi_timeframe_enabled() -> bool:
    """Multi-timeframe trend confirmation — paper/research only."""
    if not MULTI_TIMEFRAME_ENABLED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_exit_optimization_enabled() -> bool:
    """Dynamic exits (partial, trail, time) — paper/research only."""
    if not EXIT_OPTIMIZATION_ENABLED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_partial_exit_enabled() -> bool:
    return effective_exit_optimization_enabled()


def effective_correlation_guard_enabled() -> bool:
    """Portfolio correlation sizing guard — paper/research only."""
    if not CORRELATION_GUARD_ENABLED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_dynamic_sector_screener() -> bool:
    """Sector ETF strength expansion — paper/research only; never live Profile A."""
    if not DYNAMIC_SECTOR_SCREENER_ENABLED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    return bool(
        backtest_paper_sleeves_context()
        or paper_aggressive_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_paper_dynamic_universe() -> bool:
    """Weekly NYSE/screener refresh — paper aggressive only."""
    return bool(paper_only_sleeves_active() and PAPER_DYNAMIC_UNIVERSE_ENABLED)


def effective_paper_dynamic_universe_strict() -> bool:
    """Strict quality screener (8–12 names, 30d momentum) — paper aggressive only."""
    return bool(
        effective_paper_dynamic_universe() and PAPER_DYNAMIC_UNIVERSE_STRICT
    )


def paper_crypto_v2_symbols() -> list[str]:
    return list(PAPER_CRYPTO_V2_SYMBOLS)


def effective_stat_arb_optimized() -> bool:
    """Enhanced stat arb (Kalman/decay/dynamic Z) — disabled; see stat_arb_optimized.py."""
    return False


def effective_international_sleeve_enabled() -> bool:
    """ADR sleeve — research opt-in; off on live Profile A."""
    if not paper_only_sleeves_active() and not backtest_paper_sleeves_context():
        return False
    if not (paper_aggressive_context() or backtest_paper_sleeves_context()):
        return False
    return PAPER_INTERNATIONAL_SLEEVE_ENABLED


def sector_rotation_live_sleeve_enabled() -> bool:
    """Small live sector-rotation sleeve opt-in (default off)."""
    return bool(SECTOR_ROTATION_ENABLED and SECTOR_ROTATION_LIVE_SLEEVE)


def effective_sector_rotation_enabled() -> bool:
    """Sector SPDR rotation — paper/research default; live via SECTOR_ROTATION_LIVE_SLEEVE."""
    try:
        from modules.sector_rotation import effective_sector_rotation_enabled as _on

        return bool(_on())
    except Exception:
        if not SECTOR_ROTATION_ENABLED:
            return False
        if PAPER_TRADING or paper_aggressive_context() or backtest_paper_sleeves_context():
            return bool(PAPER_SECTOR_ROTATION_ENABLED or SECTOR_ROTATION_PAPER_DEFAULT)
        return bool(SECTOR_ROTATION_LIVE_SLEEVE)


def effective_bond_sleeve_enabled() -> bool:
    """Bond sleeve — research opt-in; off on live Profile A."""
    if not paper_only_sleeves_active() and not backtest_paper_sleeves_context():
        return False
    if not (paper_aggressive_context() or backtest_paper_sleeves_context()):
        return False
    return PAPER_BOND_SLEEVE_ENABLED


def effective_paper_profit_protect_enabled() -> bool:
    """Profit-protect risk sizing — paper opt-in; always off on live (incl. small account)."""
    if not PAPER_TRADING:
        return False
    if is_small_account() and not paper_only_sleeves_active():
        return False
    if not (paper_only_sleeves_active() or paper_aggressive_context()):
        return False
    return PAPER_PROFIT_PROTECT_ENABLED


def effective_vol_position_sizing_enabled() -> bool:
    """Top1 vol + conviction position sizing — paper opt-in; off on live Profile A."""
    if not PAPER_TRADING:
        return False
    if is_small_account() and not paper_only_sleeves_active():
        return False
    if not (paper_only_sleeves_active() or paper_aggressive_context()):
        return False
    return PAPER_VOL_POSITION_SIZING_ENABLED


def effective_loss_cutting_enabled() -> bool:
    """Top1 asymmetric loss cutting — paper opt-in; off on live Profile A."""
    if not PAPER_TRADING:
        return False
    if is_small_account() and not paper_only_sleeves_active():
        return False
    if not (paper_only_sleeves_active() or paper_aggressive_context()):
        return False
    return PAPER_LOSS_CUTTING_ENABLED


def effective_thinking_engine_enabled() -> bool:
    """Ollama PM reasoning — paper/research default ON; live default OFF (opt-in)."""
    if not THINKING_ENGINE_ENABLED:
        return False
    if live_thinking_sim_context() and PAPER_THINKING_ENGINE_ENABLED:
        return backtest_small_account_context()

    paperish = (
        PAPER_TRADING
        or paper_only_sleeves_active()
        or paper_aggressive_context()
        or backtest_paper_sleeves_context()
        or is_realistic_research_active()
    )
    if paperish:
        return bool(PAPER_THINKING_ENGINE_ENABLED)

    # Live money book — explicit opt-in only.
    return bool(LIVE_THINKING_ENGINE_ENABLED)


def effective_kimi_deep_thinker_enabled() -> bool:
    """Moonshot/Kimi daily deep reasoning — optional; never on live money by default."""
    if not KIMI_API_ENABLED or not (KIMI_API_KEY or "").strip():
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or (PAPER_TRADING and paper_chase_mode_enabled())
    )


def effective_insider_monitor_enabled() -> bool:
    """SEC EDGAR insider/filings RSS monitor — paper/research only."""
    if not INSIDER_MONITOR_ENABLED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    if ALLOW_LIVE_TRADING and not PAPER_TRADING:
        return False
    return bool(
        paper_only_sleeves_active()
        or backtest_paper_sleeves_context()
        or paper_chase_mode_enabled()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )


def effective_insider_signal_boost_enabled() -> bool:
    """Apply insider cluster/sell boosts to momentum, stat arb, shorts — paper only."""
    if not INSIDER_SIGNAL_BOOST_ENABLED or not INSIDER_BOOST_ENABLED:
        return False
    return effective_insider_monitor_enabled()


def effective_insider_risk_guard_enabled() -> bool:
    """Bubble / regime guards on insider bullish boosts — paper only."""
    if not INSIDER_RISK_GUARD_ENABLED:
        return False
    return effective_insider_signal_boost_enabled()


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
        "master_enabled": THINKING_ENGINE_ENABLED,
        "paper_thinking_enabled": PAPER_THINKING_ENGINE_ENABLED,
        "live_thinking_enabled": LIVE_THINKING_ENGINE_ENABLED,
        "effective_enabled": effective_thinking_engine_enabled(),
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


def nyse_stat_arb_mode_label() -> str:
    """Human-readable NYSE path when stat arb is enabled on paper."""
    if not paper_only_sleeves_active():
        return "live_profile"
    if not effective_stat_arb_enabled():
        return "momentum_only"
    if effective_equity_pairs_enabled():
        return "pairs_only (PAPER_EQUITY_PAIRS=true)"
    return "momentum + stat_arb (default; set PAPER_EQUITY_PAIRS=true for pairs-only)"


def effective_stat_arb_min_correlation() -> float:
    if effective_stat_arb_enabled():
        return float(PAPER_STAT_ARB_MIN_CORR)
    return effective_pair_min_correlation()


def effective_stat_arb_max_pairs() -> int:
    return max(1, int(PAPER_STAT_ARB_MAX_PAIRS))


def effective_stat_arb_max_pairs_expanded() -> int:
    return max(effective_stat_arb_max_pairs(), int(PAPER_STAT_ARB_MAX_PAIRS_EXPANDED))


def effective_stat_arb_max_pairs_ceiling() -> int:
    return max(
        effective_stat_arb_max_pairs_expanded(),
        int(PAPER_STAT_ARB_MAX_PAIRS_CEILING),
    )


def effective_stat_arb_max_hold_bars() -> int:
    return max(1, int(PAPER_STAT_ARB_MAX_HOLD_BARS))


def effective_stat_arb_z_entry(
    *,
    volatility: str | None = None,
    regime: str | None = None,
) -> float:
    """Dynamic Z entry between PAPER_STAT_ARB_Z_ENTRY_BASE and _MAX by vol/regime."""
    if not effective_stat_arb_enabled():
        return effective_pair_z_threshold(2.0, volatility=volatility, regime=regime)
    base = float(PAPER_STAT_ARB_Z_ENTRY_BASE)
    ceiling = float(PAPER_STAT_ARB_Z_ENTRY_MAX)
    vol = (volatility or "").strip().lower()
    reg = (regime or "").strip().lower()
    if vol in ("high", "stress", "elevated") or reg in ("stress", "crisis", "bear"):
        return ceiling
    if vol in ("low", "calm", "quiet") or reg in ("bull", "calm", "recovery"):
        return base
    mid = vol in ("moderate", "normal", "medium") or reg in ("neutral", "range", "sideways")
    if mid:
        return round(base + (ceiling - base) * 0.5, 3)
    z = effective_pair_z_threshold(base, volatility=volatility, regime=regime)
    return round(min(max(z, base), ceiling), 3)


def effective_stat_arb_risk_reward() -> float:
    return max(0.5, float(PAPER_STAT_ARB_RISK_REWARD))


def effective_stat_arb_min_dollar_volume() -> float:
    return max(0.0, float(PAPER_STAT_ARB_MIN_DOLLAR_VOLUME))


def effective_stat_arb_trailing_arm_frac() -> float:
    return max(0.1, min(1.0, float(PAPER_STAT_ARB_TRAILING_ARM_FRAC)))


def effective_stat_arb_trailing_pullback_frac() -> float:
    return max(0.1, min(0.9, float(PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC)))


def effective_stat_arb_trail_min_profit_frac() -> float:
    """Minimum fraction of profit-z delta before tighter trailing can arm."""
    return max(0.1, min(1.0, float(PAPER_STAT_ARB_TRAIL_MIN_PROFIT_FRAC)))


def effective_stat_arb_partial_exit_enabled() -> bool:
    return bool(PAPER_STAT_ARB_PARTIAL_EXIT) and effective_exit_optimization_enabled()


def effective_stat_arb_equity_max_hold_bars() -> int:
    """Equity-pair max hold; falls back to shared max hold when unset/larger."""
    dedicated = int(PAPER_STAT_ARB_EQUITY_MAX_HOLD_BARS)
    if dedicated <= 0:
        return effective_stat_arb_max_hold_bars()
    return max(1, min(dedicated, effective_stat_arb_max_hold_bars()))


def effective_stat_arb_max_leg_vol() -> float:
    """Max per-leg daily-return std for equity pairs (0 disables the filter)."""
    return max(0.0, float(PAPER_STAT_ARB_MAX_LEG_VOL))


def effective_stat_arb_conviction_scale_band() -> tuple[float, float]:
    """(min, max) conviction position scale for stat-arb pair legs."""
    lo = max(0.1, float(PAPER_STAT_ARB_CONVICTION_MIN_SCALE))
    hi = max(lo, float(PAPER_STAT_ARB_CONVICTION_MAX_SCALE))
    return lo, hi


def format_stat_arb_pairs_label() -> str:
    """Compact max-pairs range for banners."""
    return (
        f"max {effective_stat_arb_max_pairs()}-{effective_stat_arb_max_pairs_ceiling()} pairs "
        f"Z {PAPER_STAT_ARB_Z_ENTRY_BASE:.1f}-{PAPER_STAT_ARB_Z_ENTRY_MAX:.1f}"
    )


def effective_stat_arb_z_exit() -> float:
    if effective_stat_arb_enabled():
        return float(PAPER_STAT_ARB_Z_EXIT)
    return effective_pair_z_exit()


def effective_stat_arb_profit_z_delta() -> float:
    if effective_stat_arb_enabled():
        return float(STAT_ARB_PROFIT_Z_DELTA)
    return float(STAT_ARB_PROFIT_Z_DELTA)


def format_stat_arb_research_line() -> str | None:
    """One-line stat arb config for realistic research / paper startup."""
    if not effective_stat_arb_enabled():
        return None
    cap_line = ""
    if effective_stat_arb_sleeve_cap_enabled():
        cap_line = f" | {format_stat_arb_dedicated_cap_label()}"
    sector_note = ""
    if PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF:
        sector_note = f" | sector-neutral x{PAPER_STAT_ARB_SECTOR_NEUTRAL_BOOST:.2f}"
    conv_note = ""
    if effective_conviction_sizing_enabled():
        clo, chi = effective_stat_arb_conviction_scale_band()
        conv_note = f" | conviction {clo:.1f}-{chi:.1f}x"
    vol_note = ""
    mv = effective_stat_arb_max_leg_vol()
    if mv > 0:
        vol_note = f" | vol<{mv * 100:.1f}%"
    hold_b = effective_stat_arb_equity_max_hold_bars()
    return (
        f">>> STAT ARB v{REALISTIC_RESEARCH_VERSION}: cointegration p<{PAPER_STAT_ARB_COINT_PVALUE:.2f} | "
        f"corr>={effective_stat_arb_min_correlation():.2f} | "
        f"liquidity>${effective_stat_arb_min_dollar_volume()/1e6:.0f}M{vol_note} | "
        f"{format_stat_arb_pairs_label()} | "
        f"hold={hold_b}b | revert>={PAPER_STAT_ARB_MIN_REVERT_FRAC:.0%} | "
        f"RR {effective_stat_arb_risk_reward():.1f}:1 + profit-gated trail"
        f"{conv_note}{sector_note} | partial OFF | NYSE overlap blocked{cap_line}"
    )


def effective_pair_min_correlation() -> float:
    if effective_stat_arb_enabled() or effective_market_neutral_pairs_enabled():
        return PAPER_PAIR_MIN_CORRELATION
    return CRYPTO_MIN_CORRELATION


def effective_pair_coint_slope() -> float:
    if effective_stat_arb_enabled() or effective_market_neutral_pairs_enabled():
        return PAPER_PAIR_COINT_SLOPE
    return -0.02


def effective_pair_z_threshold(
    default: float = 2.0,
    *,
    volatility: str | None = None,
    regime: str | None = None,
) -> float:
    if not (
        effective_stat_arb_enabled() or effective_market_neutral_pairs_enabled()
    ):
        return default
    base = PAPER_PAIR_Z_THRESHOLD
    if not PAPER_PAIR_Z_DYNAMIC:
        return base
    vol = (volatility or "").strip().lower()
    if vol in ("high", "stress", "elevated"):
        return max(base, PAPER_PAIR_Z_STRESS)
    if vol in ("low", "calm", "quiet"):
        return min(base, PAPER_PAIR_Z_CALM)
    reg = (regime or "").strip().lower()
    if reg in ("stress", "crisis", "bear"):
        return max(base, PAPER_PAIR_Z_STRESS)
    if reg in ("bull", "calm", "recovery"):
        return min(base, PAPER_PAIR_Z_CALM)
    return base


def effective_stat_arb_max_trades() -> int:
    if effective_stat_arb_enabled():
        return max(1, PAPER_STAT_ARB_MAX_TRADES)
    return 1


def effective_pair_z_exit() -> float:
    return PAPER_PAIR_Z_EXIT


def effective_crypto_max_pairs() -> int:
    if paper_aggressive_context() and effective_crypto_enabled():
        return max(1, PAPER_CRYPTO_MAX_PAIRS)
    return 2


def effective_crypto_max_trades_per_cycle() -> int:
    if paper_aggressive_context() and effective_crypto_enabled():
        return max(1, PAPER_CRYPTO_MAX_TRADES)
    return 2


def effective_crypto_z_exit() -> float:
    if paper_aggressive_context() and effective_crypto_enabled():
        return PAPER_CRYPTO_Z_EXIT
    return PAPER_PAIR_Z_EXIT


def effective_crypto_z_entry(
    default: float = 2.0,
    *,
    volatility: str | None = None,
    regime: str | None = None,
) -> float:
    """Stricter Z entry for crypto pairs vs NYSE stat arb."""
    base = effective_pair_z_threshold(
        default, volatility=volatility, regime=regime
    )
    if paper_aggressive_context() and effective_crypto_enabled():
        return base + max(0.0, PAPER_CRYPTO_Z_ENTRY_BUMP)
    return base


def effective_crypto_max_hold_bars() -> int:
    if paper_aggressive_context() and effective_crypto_enabled():
        return max(1, PAPER_CRYPTO_MAX_HOLD_BARS)
    return STAT_ARB_MAX_HOLD_BARS


def effective_crypto_min_notional(equity: float | None = None) -> float:
    return effective_min_notional(equity) * max(1.0, PAPER_CRYPTO_MIN_NOTIONAL_MULT)


def effective_crypto_risk_mult() -> float:
    if paper_aggressive_context() and effective_crypto_enabled():
        return max(1.0, PAPER_CRYPTO_RISK_MULT)
    return 1.0


def effective_crypto_regime_filter() -> bool:
    if paper_aggressive_context() and effective_crypto_enabled():
        return PAPER_CRYPTO_REGIME_FILTER
    return False


def effective_crypto_regime_sizing_mult(regime: str | None) -> float:
    """Scale crypto pair size by regime; 0 = blocked."""
    if not effective_crypto_regime_filter():
        return 1.0
    reg = str(regime or "")
    if reg == "RHYME_B: Panic_Volatility":
        return 0.0
    if reg == "RHYME_E: Steady_Bearish_Decline":
        return 0.0
    return 1.0


def scale_crypto_pair_notional(notional: float | None, equity: float | None = None) -> float | None:
    """Apply crypto risk mult and min-notional floor for pair entries."""
    if notional is None:
        return None
    scaled = round(float(notional) * effective_crypto_risk_mult(), 2)
    floor = effective_crypto_min_notional(equity)
    if scaled < floor:
        return None
    return scaled


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
    is_paper_aggressive: bool | None = None,
    *,
    regime: str | None = None,
    data=None,
    bubble_score_100: float | None = None,
    insider_state: dict | None = None,
) -> float:
    """Dynamic VTI target (paper aggressive) or static live/small-account pct."""
    from modules.fund_config import get_vti_core_pct as _get_vti_core_pct

    if is_paper_aggressive is None:
        is_paper_aggressive = bool(
            paper_aggressive_context() or backtest_paper_sleeves_context()
        )
    return _get_vti_core_pct(
        equity,
        vol_score=vol_score,
        macro_stress=macro_stress,
        volatility=volatility,
        is_paper_aggressive=is_paper_aggressive,
        regime=regime,
        data=data,
        bubble_score_100=bubble_score_100,
        insider_state=insider_state,
    )


def effective_core_allocator_locked() -> bool:
    """Paper research: fixed passive core from allocator winner (default SPY @ 40%)."""
    if not CORE_ALLOCATOR_LOCKED:
        return False
    if not PAPER_TRADING and not backtest_paper_sleeves_context():
        return False
    return bool(
        is_realistic_research_active()
        or paper_chase_mode_enabled()
        or paper_aggressive_context()
        or backtest_paper_sleeves_context()
        or (PAPER_AGGRESSIVE_ENABLED and PAPER_TRADING)
    )


def effective_dynamic_core_enabled() -> bool:
    from modules.core_allocator import effective_dynamic_core_enabled as _on

    return _on()


def effective_real_time_websocket_enabled() -> bool:
    """Paper default on; live default off. Explicit REAL_TIME_WEBSOCKET_ENABLED overrides."""
    raw = (os.getenv("REAL_TIME_WEBSOCKET_ENABLED") or "").strip()
    if raw:
        return raw.lower() in ("1", "true", "yes", "on")
    return bool(PAPER_TRADING)


def effective_real_time_crypto_ws() -> bool:
    """Optional crypto WebSocket leg (default: follow crypto sleeve when WS enabled)."""
    if not effective_real_time_websocket_enabled():
        return False
    raw = (os.getenv("REAL_TIME_CRYPTO_WS") or "").strip()
    if raw:
        return raw.lower() in ("1", "true", "yes", "on")
    return bool(crypto_sleeve_enabled())


def vti_core_allocation_pct(
    equity: float | None = None,
    vol_score: float | None = None,
    macro_stress: bool = False,
    volatility: str | None = None,
    *,
    regime: str | None = None,
    data=None,
    bubble_score_100: float | None = None,
    insider_state: dict | None = None,
) -> float:
    if not VTI_CORE_ENABLED:
        return 0.0

    # Locked SPY/VTI passive core wins over Smart Dynamic VTI on paper research.
    if effective_core_allocator_locked():
        from modules.core_allocator import CORE_VTI_PCT

        choice = CORE_ALLOCATOR_LOCKED_CHOICE
        pct = float(CORE_VTI_PCT.get(choice, PAPER_VTI_CORE_PCT))
        return round(min(0.95, max(0.0, pct)), 6)

    paper_agg = (
        paper_aggressive_context()
        or backtest_paper_sleeves_context()
        or is_realistic_research_active()
        or (PAPER_TRADING and PAPER_AGGRESSIVE_ENABLED)
    )
    if paper_agg and PAPER_DYNAMIC_VTI_ENABLED:
        eq = equity if equity is not None and equity > 0 else (_account_equity or 0.0)
        if eq > 0:
            pct = get_vti_core_pct(
                eq,
                vol_score=vol_score,
                macro_stress=macro_stress,
                volatility=volatility,
                is_paper_aggressive=True,
                regime=regime,
                data=data,
                bubble_score_100=bubble_score_100,
                insider_state=insider_state,
            )
        else:
            pct = float(os.getenv("DYNAMIC_VTI_DEFAULT_PCT", "0.65"))
        pct = clamp_paper_vti_core(pct)
        return round(min(0.95, pct), 6)

    from modules.core_allocator import effective_vti_core_pct as _dynamic_core

    dynamic = _dynamic_core(
        equity,
        vol_score=vol_score,
        macro_stress=macro_stress,
        volatility=volatility,
    )
    if dynamic is not None:
        return dynamic
    if paper_only_sleeves_active():
        eq = equity if equity is not None and equity > 0 else (_account_equity or 0.0)
        if eq > 0:
            pct = get_vti_core_pct(
                eq,
                vol_score=vol_score,
                macro_stress=macro_stress,
                volatility=volatility,
                regime=regime,
                data=data,
                bubble_score_100=bubble_score_100,
                insider_state=insider_state,
            )
        elif PAPER_DYNAMIC_VTI_ENABLED:
            pct = float(os.getenv("DYNAMIC_VTI_DEFAULT_PCT", "0.65"))
        else:
            pct = PAPER_VTI_CORE_PCT
    elif is_small_account(equity):
        pct = SMALL_ACCOUNT_VTI_CORE_PCT
        if live_conservative_profile_active() and not _env_explicit("SMALL_ACCOUNT_VTI_CORE_PCT"):
            pct = LIVE_VTI_CORE_PCT
    else:
        pct = VTI_CORE_PCT
    if pct > 0:
        pct = clamp_paper_vti_core(pct)
        return round(min(0.95, pct), 6)
    return 0.0


def effective_vti_core_pct(
    equity: float | None = None,
    vol_score: float | None = None,
    macro_stress: bool = False,
    volatility: str | None = None,
    *,
    regime: str | None = None,
    data=None,
    bubble_score_100: float | None = None,
    insider_state: dict | None = None,
) -> float:
    """Canonical passive core % — locked SPY@40% on paper bypasses all dynamic paths."""
    if CORE_ALLOCATOR_LOCKED and (
        PAPER_TRADING
        or paper_aggressive_context()
        or backtest_paper_sleeves_context()
        or is_realistic_research_active()
    ):
        from modules.core_allocator import CORE_VTI_PCT

        choice = CORE_ALLOCATOR_LOCKED_CHOICE
        pct = float(CORE_VTI_PCT.get(choice, PAPER_VTI_CORE_PCT))
        return round(min(0.95, max(0.0, pct)), 6)
    return vti_core_allocation_pct(
        equity=equity,
        vol_score=vol_score,
        macro_stress=macro_stress,
        volatility=volatility,
        regime=regime,
        data=data,
        bubble_score_100=bubble_score_100,
        insider_state=insider_state,
    )


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


def _long_sleeve_base_cap_sum() -> float:
    """Sum of base long-sleeve caps; crypto omitted when sleeve disabled."""
    total = SPY_SLEEVE_CAP_PCT + NYSE_SLEEVE_CAP_PCT
    if effective_stat_arb_sleeve_cap_enabled():
        total += STAT_ARB_SLEEVE_CAP_PCT
    if effective_crypto_enabled():
        total += CRYPTO_SLEEVE_CAP_PCT
    return total


def active_sleeve_scale() -> float:
    """Scale active SPY/crypto/NYSE caps (VTI core + metal/social reserves)."""
    af = active_fund_fraction()
    lf = long_fund_scale()
    long_sum = _long_sleeve_base_cap_sum()
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


def _live_small_active_baseline_scale() -> float:
    """Frozen 10% active fraction (pre-85% VTI) so NYSE cap stays unchanged."""
    af = 0.10
    lf = long_fund_scale()
    long_sum = _long_sleeve_base_cap_sum()
    if long_sum <= 0:
        return 0.0
    return round(lf * af / long_sum, 6)


def _live_conservative_sleeve_boost(sleeve: str | None) -> float:
    if not live_conservative_profile_active():
        return 0.0
    choice = LIVE_ACTIVE_SLEEVE_CHOICE
    if choice == "spy" and sleeve == "spy":
        return LIVE_SMALL_ACTIVE_SLEEVE_PCT
    if choice == "cash" and sleeve == "cash":
        return LIVE_SMALL_ACTIVE_SLEEVE_PCT
    return 0.0


def effective_sleeve_cap(base_pct: float, *, sleeve: str | None = None) -> float:
    if sleeve == "stat_arb" and effective_stat_arb_sleeve_cap_enabled():
        return round(STAT_ARB_SLEEVE_CAP_PCT * active_sleeve_scale(), 6)
    if live_conservative_profile_active():
        cap = round(base_pct * _live_small_active_baseline_scale(), 6)
        cap = round(cap + _live_conservative_sleeve_boost(sleeve), 6)
        return cap
    return round(base_pct * active_sleeve_scale(), 6)


def effective_stat_arb_sleeve_cap_enabled() -> bool:
    """Dedicated stat-arb cap — paper/research with stat arb on only."""
    if not STAT_ARB_SLEEVE_CAP_ENABLED:
        return False
    if not effective_stat_arb_enabled():
        return False
    return bool(
        paper_aggressive_context()
        or backtest_paper_sleeves_context()
        or is_realistic_research_active()
    )


def effective_portfolio_constructor_enabled() -> bool:
    """Sector-aware sleeve/short tilts (modules/portfolio_constructor.py) — paper research only."""
    return bool(PORTFOLIO_CONSTRUCTOR_ENABLED) and paper_aggressive_context()


def _portfolio_constructor_stat_arb_mult() -> float:
    """Stat-arb cap tilt from the last portfolio_constructor decision (1.0 = no-op)."""
    if not effective_portfolio_constructor_enabled():
        return 1.0
    try:
        from modules.portfolio_constructor import get_last_portfolio_decision

        decision = get_last_portfolio_decision()
        return float(decision.get("stat_arb_mult", 1.0)) if decision else 1.0
    except Exception:
        return 1.0


def effective_stat_arb_cap() -> float:
    """Effective equity fraction for the dedicated stat-arb sleeve."""
    if not effective_stat_arb_sleeve_cap_enabled():
        return 0.0
    base = effective_sleeve_cap(STAT_ARB_SLEEVE_CAP_PCT, sleeve="stat_arb")
    return round(base * _portfolio_constructor_stat_arb_mult(), 6)


def effective_stat_arb_vol_scaling_enabled() -> bool:
    if not STAT_ARB_VOL_SCALING_ENABLED:
        return False
    if not effective_stat_arb_sleeve_cap_enabled():
        return False
    return effective_tail_risk_controls()


def effective_stat_arb_vol_ceiling_pct() -> float:
    return float(PORTFOLIO_VOL_CEILING_PCT)


def format_stat_arb_dedicated_cap_label() -> str:
    """Human label for startup banner (base cap % + vol scaling note)."""
    base_pct = int(round(float(STAT_ARB_SLEEVE_CAP_PCT) * 100))
    vol_note = " (vol scaled)" if effective_stat_arb_vol_scaling_enabled() else ""
    return f"Stat Arb sleeve: {base_pct}% dedicated{vol_note}"


def effective_cash_buffer_pct() -> float:
    """Cash headroom so VTI core + active sleeves + metal sum to 100% of equity."""
    metal = METAL_SLEEVE_CAP_PCT if metal_sleeve_enabled() else 0.0
    vti = vti_core_allocation_pct()
    if live_conservative_profile_active():
        long_caps = (
            effective_sleeve_cap(SPY_SLEEVE_CAP_PCT, sleeve="spy")
            + effective_sleeve_cap(NYSE_SLEEVE_CAP_PCT, sleeve="nyse")
        )
        if effective_crypto_enabled():
            long_caps += effective_sleeve_cap(CRYPTO_SLEEVE_CAP_PCT, sleeve="crypto")
    else:
        long_caps = _long_sleeve_base_cap_sum() * active_sleeve_scale()
    cash = round(1.0 - metal - vti - long_caps, 6)
    if cash < 0:
        raise ValueError(
            f"Fund over-allocated: vti {vti:.2%} + metal {metal:.2%} + "
            f"long sleeves {long_caps:.2%} > 100%; reduce VTI_CORE_PCT or sleeve caps"
        )
    return cash


def fund_allocation_pct() -> dict[str, float]:
    """Current sleeve + cash cap fractions (sum to 1.0)."""
    crypto_cap = (
        effective_sleeve_cap(CRYPTO_SLEEVE_CAP_PCT)
        if effective_crypto_enabled()
        else 0.0
    )
    stat_arb_cap = (
        effective_stat_arb_cap()
        if effective_stat_arb_sleeve_cap_enabled()
        else 0.0
    )
    return {
        "vti_core": vti_core_allocation_pct(),
        "spy": effective_sleeve_cap(SPY_SLEEVE_CAP_PCT),
        "crypto": crypto_cap,
        "nyse": effective_sleeve_cap(NYSE_SLEEVE_CAP_PCT),
        "stat_arb": stat_arb_cap,
        "metal": METAL_SLEEVE_CAP_PCT if metal_sleeve_enabled() else 0.0,
        "cash_buffer": effective_cash_buffer_pct(),
    }


_alloc = fund_allocation_pct()
if abs(sum(_alloc.values()) - 1.0) > 1e-4:
    raise ValueError(f"Fund allocation must sum to 100%, got {_alloc}")
