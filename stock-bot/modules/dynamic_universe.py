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
IPO_SAFETY_MAX_POSITION_PCT = float(
    __import__("os").getenv("PAPER_IPO_MAX_POSITION_PCT", "0.02")
)
IPO_SAFETY_TRIM_TARGET_PCT = float(
    __import__("os").getenv("PAPER_IPO_TRIM_TARGET_PCT", "0.01")
)
IPO_SAFETY_TRIM_GAIN_PCT = float(
    __import__("os").getenv("PAPER_IPO_TRIM_GAIN_PCT", "0.20")
)
HIGH_VOL_ATR_PCT = float(__import__("os").getenv("PAPER_UNIVERSE_HIGH_VOL_ATR", "0.08"))
HIGH_VOL_POSITION_SCALE = float(
    __import__("os").getenv("PAPER_UNIVERSE_HIGH_VOL_SCALE", "0.75")
)
# Strict mode: quality-over-quantity (8–12 names, 30d momentum, sector balance)
STRICT_MIN_UNIVERSE_SIZE = int(__import__("os").getenv("PAPER_UNIVERSE_STRICT_MIN", "8"))
STRICT_MAX_UNIVERSE_SIZE = int(__import__("os").getenv("PAPER_UNIVERSE_STRICT_MAX", "12"))
STRICT_MIN_AVG_DOLLAR_VOLUME = float(
    __import__("os").getenv("PAPER_UNIVERSE_STRICT_MIN_DOLLAR_VOL", "100000000")
)
STRICT_MIN_SHARE_VOLUME = int(
    __import__("os").getenv("PAPER_UNIVERSE_STRICT_MIN_SHARE_VOL", "500000")
)
STRICT_MOMENTUM_LOOKBACK = int(
    __import__("os").getenv("PAPER_UNIVERSE_STRICT_MOMENTUM_DAYS", "30")
)
STRICT_MAX_PER_SECTOR = int(
    __import__("os").getenv("PAPER_UNIVERSE_STRICT_MAX_PER_SECTOR", "2")
)
STRICT_MIN_MOMENTUM_RANK = float(
    __import__("os").getenv("PAPER_UNIVERSE_STRICT_MIN_MOM_RANK", "0.65")
)
STRICT_MAX_IPO_SLOTS = int(__import__("os").getenv("PAPER_UNIVERSE_STRICT_MAX_IPO", "1"))
MOMENTUM_LOOKBACK = int(__import__("os").getenv("PAPER_UNIVERSE_MOMENTUM_DAYS", "20"))

# GICS-lite tags for sector balance (subset of liquid US names)
EQUITY_SECTOR_MAP: dict[str, str] = {
    "AAPL": "Tech",
    "MSFT": "Tech",
    "NVDA": "Tech",
    "AMD": "Tech",
    "GOOGL": "Tech",
    "GOOG": "Tech",
    "AMZN": "Tech",
    "TSLA": "Tech",
    "META": "Tech",
    "NFLX": "Tech",
    "INTC": "Tech",
    "MU": "Tech",
    "SMCI": "Tech",
    "CRM": "Tech",
    "SHOP": "Tech",
    "SNOW": "Tech",
    "OKTA": "Tech",
    "HPE": "Tech",
    "BB": "Tech",
    "ARM": "Tech",
    "PLTR": "Tech",
    "COIN": "Tech",
    "SPCX": "Tech",
    "XOM": "Energy",
    "CVX": "Energy",
    "LNG": "Energy",
    "COP": "Energy",
    "RTX": "Defense",
    "LMT": "Defense",
    "KTOS": "Defense",
    "NOC": "Defense",
    "GD": "Defense",
    "JPM": "Financials",
    "BAC": "Financials",
    "GS": "Financials",
    "MS": "Financials",
    "JNJ": "Healthcare",
    "UNH": "Healthcare",
    "PFE": "Healthcare",
    "LLY": "Healthcare",
    "WMT": "Consumer",
    "COST": "Consumer",
    "HD": "Consumer",
    "DIS": "Consumer",
}

_meta_cache: dict[str, dict] | None = None
_meta_cache_mtime: float | None = None


def strict_mode_active() -> bool:
    try:
        return bool(config.effective_paper_dynamic_universe_strict())
    except AttributeError:
        return False


def effective_max_universe_size() -> int:
    return STRICT_MAX_UNIVERSE_SIZE if strict_mode_active() else MAX_UNIVERSE_SIZE


def effective_min_dollar_volume() -> float:
    return STRICT_MIN_AVG_DOLLAR_VOLUME if strict_mode_active() else MIN_AVG_DOLLAR_VOLUME


def effective_min_share_volume() -> int:
    return STRICT_MIN_SHARE_VOLUME if strict_mode_active() else MIN_SHARE_VOLUME


def effective_momentum_lookback() -> int:
    return STRICT_MOMENTUM_LOOKBACK if strict_mode_active() else MOMENTUM_LOOKBACK


def effective_max_ipo_slots() -> int:
    return STRICT_MAX_IPO_SLOTS if strict_mode_active() else MAX_IPO_SLOTS


def sector_for_symbol(symbol: str) -> str:
    return EQUITY_SECTOR_MAP.get(config.normalize_symbol(symbol), "Other")


def apply_sector_balance(
    scored: list[dict],
    *,
    max_size: int | None = None,
    max_per_sector: int | None = None,
) -> list[dict]:
    """Pick top names with at most max_per_sector per sector tag."""
    max_size = max_size if max_size is not None else effective_max_universe_size()
    max_per_sector = (
        max_per_sector if max_per_sector is not None else STRICT_MAX_PER_SECTOR
    )
    selected: list[dict] = []
    sector_counts: dict[str, int] = {}
    deferred: list[dict] = []
    for row in scored:
        sym = str(row.get("ticker", "")).upper()
        sector = sector_for_symbol(sym)
        if sector_counts.get(sector, 0) < max_per_sector:
            selected.append(row)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        else:
            deferred.append(row)
        if len(selected) >= max_size:
            break
    if len(selected) < max_size:
        for row in deferred:
            if len(selected) >= max_size:
                break
            selected.append(row)
    return selected


def screener_momentum_order(symbols: list[str]) -> list[str] | None:
    """Order symbols by screener 30d momentum rank (strict mode helper)."""
    if not strict_mode_active():
        return None
    meta = load_screener_ticker_meta()
    ranked: list[tuple[float, float, str]] = []
    for sym in symbols:
        row = meta.get(config.normalize_symbol(sym), {})
        mom_rank = row.get("momentum_30d_rank")
        if mom_rank is None:
            mom_rank = row.get("momentum_rank")
        score = row.get("score")
        mom = row.get("momentum_30d")
        if mom is None:
            mom = row.get("momentum")
        if mom_rank is not None:
            ranked.append((float(mom_rank), float(mom or 0), sym))
        elif score is not None:
            ranked.append((float(score), float(mom or 0), sym))
    if not ranked:
        return None
    ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [sym for _, _, sym in ranked]


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


def ipo_safety_enabled() -> bool:
    try:
        return bool(config.effective_paper_ipo_safety_enabled())
    except AttributeError:
        return False


def trading_days_from_series(prices) -> int:
    return int(len(prices.dropna()))


def trading_days_at_bar(data, symbol: str, bar_idx: int) -> int:
    """Trading days since first valid daily bar (listing proxy)."""
    sym = config.normalize_symbol(symbol)
    if sym not in getattr(data, "columns", []):
        return 0
    series = data[sym]
    first = series.first_valid_index()
    if first is None:
        return 0
    end = data.index[int(bar_idx)]
    if end < first:
        return 0
    return trading_days_from_series(series.loc[first:end])


def is_ipo_trading_days(trading_days: int) -> bool:
    """IPO window: 5–30 trading days of history (exclusive upper bound at 30)."""
    return IPO_MIN_TRADING_DAYS <= trading_days < IPO_MAX_TRADING_DAYS


def is_ipo_symbol(
    symbol: str, *, data=None, bar_idx: int | None = None
) -> bool:
    """True when symbol is in the IPO age window (5–29 trading days)."""
    sym = config.normalize_symbol(symbol)
    if data is not None and sym in getattr(data, "columns", []):
        if bar_idx is not None:
            td = trading_days_at_bar(data, sym, int(bar_idx))
        else:
            series = data[sym]
            first = series.first_valid_index()
            td = trading_days_from_series(series.loc[first:]) if first is not None else 0
        return is_ipo_trading_days(td)
    meta = load_screener_ticker_meta().get(sym, {})
    td = meta.get("trading_days")
    if td is not None:
        return is_ipo_trading_days(int(td))
    return bool(meta.get("is_ipo"))


def ipo_max_position_notional(equity: float) -> float:
    return round(float(equity) * IPO_SAFETY_MAX_POSITION_PCT, 2)


def cap_ipo_buy_notional(
    symbol: str,
    notional: float,
    equity: float,
    *,
    data=None,
    bar_idx: int | None = None,
) -> float:
    if not ipo_safety_enabled() or not is_ipo_symbol(symbol, data=data, bar_idx=bar_idx):
        return float(notional)
    return round(min(float(notional), ipo_max_position_notional(equity)), 2)


def ipo_trim_reduce_notional(
    equity: float, cost_basis: float, market_value: float
) -> float | None:
    """Sell notional to reach 1% equity when unrealized gain >= 20%."""
    if not ipo_safety_enabled() or cost_basis <= 0 or market_value <= 0:
        return None
    gain = (market_value - cost_basis) / cost_basis
    if gain < IPO_SAFETY_TRIM_GAIN_PCT:
        return None
    target = float(equity) * IPO_SAFETY_TRIM_TARGET_PCT
    if market_value <= target:
        return None
    return round(market_value - target, 2)


def ipo_momentum_scale(
    symbol: str, *, data=None, bar_idx: int | None = None
) -> float:
    if not ipo_safety_enabled():
        return 1.0
    if is_ipo_symbol(symbol, data=data, bar_idx=bar_idx):
        return IPO_POSITION_SCALE
    return 1.0


def position_scale_for_symbol(symbol: str) -> float:
    """Safe sizing multiplier for volatile names (paper only; IPO via ipo_momentum_scale)."""
    if not config.effective_paper_dynamic_universe():
        return 1.0
    meta = load_screener_ticker_meta().get(config.normalize_symbol(symbol), {})
    if meta.get("position_scale") is not None:
        return float(meta["position_scale"])
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
    # Screener tickers often lack price history after a refresh — fall back / merge
    # with the full static NYSE universe so the equity sleeves are never stuck on
    # a 0-1 name pool (scan_signals=0, 0 pairs).
    min_cover = int(getattr(config, "PAPER_DYNAMIC_UNIVERSE_MIN_COVER", 10) or 10)
    if len(dynamic) < min_cover:
        merged = list(dict.fromkeys([*dynamic, *static]))
        logger.debug(
            "[UNIVERSE] dynamic overlap %d < %d — merged with static NYSE "
            "universe: %d names (top: %s)",
            len(dynamic),
            min_cover,
            len(merged),
            ", ".join(merged[:10]),
        )
        return merged
    logger.debug(
        "[UNIVERSE] dynamic screener universe: %d names (top: %s)",
        len(dynamic),
        ", ".join(dynamic[:10]),
    )
    return dynamic or static


def backtest_extra_tickers() -> list[str]:
    """Screener tickers to fetch for dynamic-universe backtests."""
    if not config.effective_paper_dynamic_universe():
        return []
    tickers = config.load_screener_universe_tickers() or []
    base = set(config.UNIVERSE)
    return sorted(t for t in tickers if t not in base)


def live_equity_refresh_symbols() -> list[str]:
    """Static equity universe + screener names for live 5m refresh."""
    symbols = list(config.equity_universe())
    if not (config.USE_DYNAMIC_UNIVERSE or config.effective_paper_dynamic_universe()):
        return symbols
    extra = config.load_screener_universe_tickers() or []
    for sym in extra:
        if sym and sym not in symbols:
            symbols.append(sym)
    return symbols


def prefetch_screener_price_data(
    tickers: list[str] | None = None,
    *,
    days: int = 120,
    include_5m: bool = True,
    include_daily: bool = True,
) -> dict:
    """
    Pull yfinance history for screener symbols into SQLite so the live close
    matrix / dynamic universe intersection is not empty after a refresh.
    """
    tickers = list(tickers or config.load_screener_universe_tickers() or [])
    tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]
    static = set(config.equity_universe())
    extra = [t for t in dict.fromkeys(tickers) if t not in static]
    out = {
        "requested": len(tickers),
        "extra": len(extra),
        "skipped_static": len(tickers) - len(extra),
        "fetched_5m": 0,
        "fetched_daily": 0,
    }
    if not extra:
        return out

    if include_5m:
        try:
            from fetch_data import fetch_and_store

            fetch_and_store(extra)
            out["fetched_5m"] = len(extra)
        except Exception as exc:
            logger.warning("screener 5m prefetch failed: %s", exc)
            out["error_5m"] = str(exc)

    if include_daily:
        try:
            from fetch_data import fetch_daily_history_for_tickers

            fetch_daily_history_for_tickers(extra, days=days)
            out["fetched_daily"] = len(extra)
        except Exception as exc:
            logger.warning("screener daily prefetch failed: %s", exc)
            out["error_daily"] = str(exc)

    try:
        from modules.data_loader import clear_close_matrix_cache

        clear_close_matrix_cache()
    except Exception as exc:
        logger.debug("dyn universe soft-fail: %s", exc)
    return out


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
    max_seed = effective_max_universe_size()
    for sym in priority + from_universe:
        sym = sym.upper()
        if sym not in seeds:
            seeds.append(sym)
        if len(seeds) >= max_seed:
            break
    if strict_mode_active() and len(seeds) > STRICT_MIN_UNIVERSE_SIZE:
        rows_pre = [{"ticker": sym, "score": 1.0 - i * 0.01} for i, sym in enumerate(seeds)]
        seeds = [r["ticker"] for r in apply_sector_balance(rows_pre, max_size=max_seed)]
    rows = [
        {
            "ticker": sym,
            "score": round(1.0 - i * 0.02, 4),
            "momentum": 0.0,
            "momentum_30d": 0.0,
            "momentum_rank": round(1.0 - i * 0.02, 4),
            "momentum_30d_rank": round(1.0 - i * 0.02, 4),
            "atr_pct": 0.05,
            "trend": 0.0,
            "price": 0.0,
            "avg_volume": 0,
            "avg_dollar_volume": int(
                STRICT_MIN_AVG_DOLLAR_VOLUME
                if strict_mode_active()
                else MIN_AVG_DOLLAR_VOLUME
            ),
            "trading_days": 120,
            "is_ipo": False,
            "exchange": "NASDAQ" if sym in {"NVDA", "TSLA", "AMD", "AAPL", "SPCX"} else "NYSE",
            "sector": sector_for_symbol(sym),
            "easy_to_borrow": True,
            "position_scale": 1.0,
        }
        for i, sym in enumerate(seeds)
    ]
    payload = {
        "tickers": seeds,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score_table": rows,
        "ipo_count": 0,
        "filters": {"source": "offline_seed", "strict_mode": strict_mode_active()},
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
        from scripts.analysis.universe_screener import screen_universe

        load_screener_ticker_meta(force=True)
        result = screen_universe()
        logger.info(
            "dynamic_universe refreshed: %s tickers",
            len(result.get("tickers") or []),
        )
        # Prefetch runs inside screen_universe; report ticker count only.
        return {
            "action": "refreshed",
            "tickers": result.get("tickers") or [],
            "generated_at": result.get("generated_at"),
            **screener_universe_meta(),
        }
    except Exception as exc:
        logger.warning("dynamic_universe refresh failed: %s", exc)
        if not Path(config.SCREENER_UNIVERSE_PATH).is_file():
            try:
                seed = build_offline_screener_seed()
                prefetch_screener_price_data(seed.get("tickers") or [])
                return {"action": "offline_seed", **seed, **screener_universe_meta()}
            except Exception as seed_exc:
                logger.warning("offline screener seed failed: %s", seed_exc)
        # Fresh file may exist but lacks price coverage — still prefetch.
        try:
            pref = prefetch_screener_price_data()
            if pref.get("extra"):
                return {
                    "action": "prefetch_only",
                    "error": str(exc),
                    "prefetch": pref,
                    **screener_universe_meta(),
                }
        except Exception as exc:
            logger.debug("dyn universe soft-fail: %s", exc)
        return {
            "action": "failed",
            "error": str(exc),
            **screener_universe_meta(),
        }
