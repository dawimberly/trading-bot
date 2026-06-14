"""Exchange-agnostic equity universe for paper NYSE momentum + stat arb (weekly refresh)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = int(
    __import__("os").getenv("PAPER_UNIVERSE_REFRESH_DAYS", "7")
)

# --- Screener filters (paper only; lightweight for laptop) ---
ALLOWED_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA"})
MIN_PRICE = float(__import__("os").getenv("PAPER_UNIVERSE_MIN_PRICE", "5"))
MIN_AVG_DOLLAR_VOLUME = float(
    __import__("os").getenv("PAPER_UNIVERSE_MIN_DOLLAR_VOL", "50000000")
)
MIN_SHARE_VOLUME = int(__import__("os").getenv("PAPER_UNIVERSE_MIN_SHARE_VOL", "200000"))
MAX_ATR_PCT = float(__import__("os").getenv("PAPER_UNIVERSE_MAX_ATR_PCT", "0.15"))
IPO_MAX_TRADING_DAYS = int(__import__("os").getenv("PAPER_UNIVERSE_IPO_MAX_DAYS", "30"))
IPO_MIN_TRADING_DAYS = int(__import__("os").getenv("PAPER_UNIVERSE_IPO_MIN_DAYS", "5"))
MAX_UNIVERSE_SIZE = int(__import__("os").getenv("PAPER_UNIVERSE_MAX_TICKERS", "28"))
MAX_IPO_SLOTS = int(__import__("os").getenv("PAPER_UNIVERSE_MAX_IPO", "5"))
IPO_POSITION_SCALE = float(__import__("os").getenv("PAPER_UNIVERSE_IPO_SCALE", "0.50"))
HIGH_VOL_ATR_PCT = float(__import__("os").getenv("PAPER_UNIVERSE_HIGH_VOL_ATR", "0.08"))
HIGH_VOL_POSITION_SCALE = float(
    __import__("os").getenv("PAPER_UNIVERSE_HIGH_VOL_SCALE", "0.75")
)

_meta_cache: dict[str, dict] | None = None
_meta_cache_mtime: float | None = None


def screener_universe_age_days() -> float | None:
    path = Path(config.SCREENER_UNIVERSE_PATH)
    if not path.is_file():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 86400.0


def screener_universe_meta() -> dict:
    path = Path(config.SCREENER_UNIVERSE_PATH)
    if not path.is_file():
        return {"exists": False, "path": str(path), "age_days": None, "count": 0}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        tickers = payload.get("tickers") or []
        ipo_count = sum(
            1 for row in (payload.get("score_table") or []) if row.get("is_ipo")
        )
    except (OSError, json.JSONDecodeError):
        tickers = []
        ipo_count = 0
        payload = {}
    age = screener_universe_age_days()
    return {
        "exists": True,
        "path": str(path),
        "age_days": round(age, 1) if age is not None else None,
        "count": len(tickers),
        "ipo_count": ipo_count,
        "generated_at": payload.get("generated_at") if tickers else None,
        "filters": payload.get("filters"),
    }


def _load_payload() -> dict:
    path = Path(config.SCREENER_UNIVERSE_PATH)
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def load_screener_ticker_meta(*, force: bool = False) -> dict[str, dict]:
    """Ticker -> screener row (atr, is_ipo, easy_to_borrow, position_scale, ...)."""
    global _meta_cache, _meta_cache_mtime
    path = Path(config.SCREENER_UNIVERSE_PATH)
    mtime = path.stat().st_mtime if path.is_file() else None
    if (
        not force
        and _meta_cache is not None
        and _meta_cache_mtime == mtime
    ):
        return _meta_cache

    rows = _load_payload().get("score_table") or []
    _meta_cache = {
        str(row.get("ticker", "")).strip().upper(): row
        for row in rows
        if str(row.get("ticker", "")).strip()
    }
    _meta_cache_mtime = mtime
    return _meta_cache


def position_scale_for_symbol(symbol: str) -> float:
    """Safe sizing multiplier for volatile / IPO names (paper only)."""
    if not config.effective_paper_dynamic_universe():
        return 1.0
    meta = load_screener_ticker_meta().get(config.normalize_symbol(symbol), {})
    if meta.get("position_scale") is not None:
        return float(meta["position_scale"])
    if meta.get("is_ipo"):
        return IPO_POSITION_SCALE
    atr = float(meta.get("atr_pct") or 0)
    if atr >= HIGH_VOL_ATR_PCT:
        return HIGH_VOL_POSITION_SCALE
    return 1.0


def short_borrow_allowed(symbol: str) -> bool:
    """Stat-arb short leg: skip when Alpaca marks not easy-to-borrow (if known)."""
    meta = load_screener_ticker_meta().get(config.normalize_symbol(symbol), {})
    etb = meta.get("easy_to_borrow")
    if etb is None:
        return True
    return bool(etb)


def equity_sleeve_universe(data_columns) -> list[str]:
    """
    NYSE momentum + stat-arb equity pool: dynamic screener (paper) or static columns.
    Exchange-agnostic (NYSE + NASDAQ + ARCA).
    """
    cols = list(data_columns)
    static = [c for c in cols if config._nyse_eligible_symbol(c)]
    if not (config.USE_DYNAMIC_UNIVERSE or config.effective_paper_dynamic_universe()):
        return static

    screener = config.load_screener_universe_tickers()
    if not screener:
        return static

    screener_set = frozenset(screener)
    dynamic = [c for c in cols if c in screener_set and config._nyse_eligible_symbol(c)]
    return dynamic or static


def backtest_extra_tickers() -> list[str]:
    """Screener tickers to fetch for dynamic-universe backtests."""
    if not config.effective_paper_dynamic_universe():
        return []
    tickers = config.load_screener_universe_tickers() or []
    base = set(config.UNIVERSE)
    return sorted(t for t in tickers if t not in base)


def build_offline_screener_seed() -> dict:
    """
    Lightweight fallback when Alpaca refresh fails: seed from static UNIVERSE
    equities (NYSE + NASDAQ names in config), no merge with stale screener junk.
    """
    priority = [
        "NVDA", "TSLA", "AMD", "AAPL", "MSFT", "GOOGL", "AMZN", "META",
        "SPCX", "PLTR", "NFLX", "INTC", "MU", "SMCI", "COIN", "CRM", "SHOP",
        "SNOW", "OKTA", "HPE", "BB", "ARM",
    ]
    from_universe = [
        t
        for t in config.equity_universe()
        if t not in (config.SPY_BOT_SYMBOL, config.VTI_CORE_SYMBOL, "QQQ", "IWM")
    ]
    seeds: list[str] = []
    for sym in priority + from_universe:
        sym = sym.upper()
        if sym not in seeds:
            seeds.append(sym)
        if len(seeds) >= MAX_UNIVERSE_SIZE:
            break
    rows = [
        {
            "ticker": sym,
            "score": 1.0,
            "momentum": 0.0,
            "atr_pct": 0.05,
            "trend": 0.0,
            "price": 0.0,
            "avg_volume": 0,
            "avg_dollar_volume": int(MIN_AVG_DOLLAR_VOLUME),
            "trading_days": 120,
            "is_ipo": False,
            "exchange": "NASDAQ" if sym in {"NVDA", "TSLA", "AMD", "AAPL", "SPCX"} else "NYSE",
            "easy_to_borrow": True,
            "position_scale": 1.0,
        }
        for sym in seeds
    ]
    payload = {
        "tickers": seeds,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score_table": rows,
        "ipo_count": 0,
        "filters": {"source": "offline_seed"},
    }
    path = Path(config.SCREENER_UNIVERSE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload


def maybe_refresh_screener_universe(
    *,
    force: bool = False,
    max_age_days: int | None = None,
) -> dict:
    """
    Refresh data/screener_universe.json when stale (default weekly).
    Paper aggressive only unless USE_DYNAMIC_UNIVERSE is set globally.
    """
    if not config.effective_paper_dynamic_universe() and not config.USE_DYNAMIC_UNIVERSE:
        return {"action": "disabled", "reason": "paper_dynamic_universe_off"}

    max_age = max_age_days if max_age_days is not None else DEFAULT_MAX_AGE_DAYS
    age = screener_universe_age_days()
    if not force and age is not None and age < max_age:
        return {
            "action": "fresh",
            "age_days": round(age, 1),
            "max_age_days": max_age,
            **screener_universe_meta(),
        }

    try:
        from scripts.analysis.universe_screener import run_screener

        load_screener_ticker_meta(force=True)
        result = run_screener()
        logger.info(
            "dynamic_universe refreshed: %s tickers (%s IPO)",
            len(result.get("tickers") or []),
            result.get("ipo_count", 0),
        )
        return {"action": "refreshed", **result, **screener_universe_meta()}
    except Exception as exc:
        logger.warning("dynamic_universe refresh failed: %s", exc)
        if not Path(config.SCREENER_UNIVERSE_PATH).is_file():
            try:
                seed = build_offline_screener_seed()
                return {"action": "offline_seed", **seed, **screener_universe_meta()}
            except Exception as seed_exc:
                logger.warning("offline screener seed failed: %s", seed_exc)
        return {
            "action": "failed",
            "error": str(exc),
            **screener_universe_meta(),
        }
