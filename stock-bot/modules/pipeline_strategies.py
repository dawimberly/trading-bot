"""Crypto pair and equity MA50 strategies shared by run_all.py and backtester.py."""

from __future__ import annotations

import logging
from collections import deque
from datetime import date, datetime, time
from pathlib import Path

import numpy as np

import config
from modules import deployment_sizing
from modules.crypto_universe import crypto_trading_columns

logger = logging.getLogger(__name__)

PAUSED_REGIMES = ("RHYME_B: Panic_Volatility", "RHYME_E: Steady_Bearish_Decline")

# In-session NYSE momentum entries for one-per-day (backtest + live gate).
_nyse_mom_entries_by_day: dict[str, set[str]] = {}
# Buys counted while a long is open (reset when flat).
_nyse_open_add_counts: dict[str, int] = {}
# Same-day sells (backtest + live session); journal also checked live.
_nyse_sold_today_by_day: dict[str, set[str]] = {}
# Hygiene skip tallies for backtest A/B reporting.
_nyse_hygiene_skip_counts: dict[str, int] = {
    "hygiene_max_adds": 0,
    "hygiene_same_day": 0,
    "hygiene_min_notional": 0,
}
# Rolling per-symbol pick history keyed by backtest bar index.
_nyse_pick_history: dict[str, deque[int]] = {}


def reset_nyse_pick_rotation() -> None:
    """Clear pick-rotation state (backtests / clean restarts)."""
    _nyse_pick_history.clear()


def reset_nyse_entry_hygiene_state() -> None:
    """Clear add / same-day / skip counters (backtests / clean restarts)."""
    _nyse_open_add_counts.clear()
    _nyse_mom_entries_by_day.clear()
    _nyse_sold_today_by_day.clear()
    for key in _nyse_hygiene_skip_counts:
        _nyse_hygiene_skip_counts[key] = 0


def get_nyse_hygiene_skip_counts() -> dict[str, int]:
    """Copy of hygiene skip counters (max adds / same-day / min notional)."""
    return {k: int(v) for k, v in _nyse_hygiene_skip_counts.items()}


def _record_nyse_hygiene_skip(kind: str) -> None:
    if kind in _nyse_hygiene_skip_counts:
        _nyse_hygiene_skip_counts[kind] = int(_nyse_hygiene_skip_counts[kind]) + 1


def mark_nyse_sold_today(symbol: str, now=None, data=None) -> None:
    """Record a NYSE sell for same-day reentry block (live session + backtest)."""
    sym = config.normalize_symbol(symbol)
    day = _calendar_day_key(now, data=data)
    bucket = _nyse_sold_today_by_day.setdefault(day, set())
    bucket.add(sym)


def _nyse_pick_bar_index(now, cooldown_bars=None) -> int | None:
    if cooldown_bars is not None and isinstance(now, (int, np.integer)):
        return int(now)
    return None


def _nyse_pick_rotation_blocked(symbol: str, bar_index: int) -> bool:
    if not config.effective_nyse_pick_rotation():
        return False
    window = max(1, int(getattr(config, "NYSE_PICK_WINDOW_BARS", 20)))
    max_picks = max(1, int(getattr(config, "NYSE_MAX_PICKS_PER_SYMBOL_WINDOW", 5)))
    hist = _nyse_pick_history.setdefault(config.normalize_symbol(symbol), deque())
    while hist and (bar_index - hist[0]) >= window:
        hist.popleft()
    return len(hist) >= max_picks


def _record_nyse_pick(symbol: str, bar_index: int) -> None:
    hist = _nyse_pick_history.setdefault(config.normalize_symbol(symbol), deque())
    hist.append(bar_index)


def _et_now_time(now=None) -> time | None:
    """Return America/New_York clock time for *now* (or wall clock)."""
    try:
        try:
            from zoneinfo import ZoneInfo

            et = ZoneInfo("America/New_York")
        except Exception:
            import pytz

            et = pytz.timezone("America/New_York")
        if now is None:
            return datetime.now(et).timetz().replace(tzinfo=None)
        if isinstance(now, datetime):
            if now.tzinfo is None:
                # Assume already ET/local wall time from live loop.
                return now.time()
            return now.astimezone(et).time()
        return None
    except Exception:
        return None


def _calendar_day_key(now=None, data=None) -> str:
    """Calendar day for one-entry/day; supports live datetime and backtest bar windows."""
    if isinstance(now, datetime):
        return now.date().isoformat()
    if isinstance(now, date):
        return now.isoformat()
    # Backtest passes bar index + truncated window — use last bar date, not wall clock.
    if data is not None and hasattr(data, "index") and len(data.index) > 0:
        try:
            ts = data.index[-1]
            if hasattr(ts, "date"):
                return ts.date().isoformat()
            from pandas import Timestamp

            return Timestamp(ts).date().isoformat()
        except Exception as exc:
            logger.debug("pipeline soft-fail: %s", exc)
    if isinstance(now, (int, float)) and data is not None and hasattr(data, "index"):
        try:
            idx = int(now)
            if 0 <= idx < len(data.index):
                ts = data.index[idx]
                if hasattr(ts, "date"):
                    return ts.date().isoformat()
                from pandas import Timestamp

                return Timestamp(ts).date().isoformat()
        except Exception as exc:
            logger.debug("pipeline soft-fail: %s", exc)
    return datetime.now().date().isoformat()


NYSE_PREF_ENTRY_START = time(12, 0)
NYSE_PREF_ENTRY_END = time(14, 0)
NYSE_RSI_MAX_OFF_PEAK = 70
NYSE_RSI_MAX_PREF_WINDOW = 72
NYSE_PREF_WINDOW_RANK_BOOST = 0.02
NYSE_RSI_PERIOD = 14


def _rsi(series, period: int) -> float | None:
    if len(series) < period + 2:
        return None
    s = series.astype(float)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss <= 1e-12:
        return 100.0
    rs = float(gain.iloc[-1]) / last_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def _nyse_symbol_rsi(symbol: str, data) -> float | None:
    if data is None or not hasattr(data, "columns") or symbol not in data.columns:
        return None
    prices = data[symbol].dropna()
    if len(prices) < NYSE_RSI_PERIOD + 2:
        return None
    return _rsi(prices, NYSE_RSI_PERIOD)


def _nyse_preferred_entry_window_active(now=None) -> bool:
    """True during 12:00–14:00 ET preferred entry window (live clock only)."""
    if isinstance(now, (int, float)):
        return False
    t = _et_now_time(now)
    if t is None:
        return False
    return NYSE_PREF_ENTRY_START <= t <= NYSE_PREF_ENTRY_END


def _nyse_momentum_rsi_max(now=None) -> int | None:
    """RSI ceiling under quality fixes; looser in the 12:00–14:00 ET window."""
    if not config.effective_paper_momentum_quality_fixes():
        return None
    if _nyse_preferred_entry_window_active(now):
        return NYSE_RSI_MAX_PREF_WINDOW
    return NYSE_RSI_MAX_OFF_PEAK


def _nyse_time_of_day_rank_boost(now=None) -> float:
    """Afternoon scoring boost — prefer entries 12:00–14:00 ET without blocking others."""
    if not config.effective_paper_momentum_quality_fixes():
        return 0.0
    if _nyse_preferred_entry_window_active(now):
        return NYSE_PREF_WINDOW_RANK_BOOST
    return 0.0


def _nyse_open_cooldown_active(now=None) -> bool:
    """True during 9:30–10:00 ET (first 30 minutes). Daily backtests (bar index / midnight) skip."""
    if isinstance(now, (int, float)):
        return False
    t = _et_now_time(now)
    if t is None:
        return False
    return time(9, 30) <= t <= time(10, 0)


def _overnight_gap_pct(symbol: str, data) -> float | None:
    """(today_open / prior_close) - 1. Prefers yfinance OHLC; falls back to close/close."""
    sym = config.normalize_symbol(symbol)
    # Backtests must stay PIT — no live yfinance gaps.
    try:
        use_yf = not config.backtest_paper_sleeves_context()
    except Exception:
        use_yf = True
    if use_yf:
        try:
            import yfinance as yf

            hist = yf.Ticker(sym).history(period="10d", auto_adjust=True)
            if hist is not None and len(hist) >= 2 and "Open" in hist.columns:
                prior_close = float(hist["Close"].iloc[-2])
                today_open = float(hist["Open"].iloc[-1])
                if prior_close > 0 and today_open > 0:
                    return today_open / prior_close - 1.0
        except Exception as exc:
            logger.debug("gap yfinance fallback for %s: %s", sym, exc)

    if data is None or not hasattr(data, "columns") or sym not in data.columns:
        return None
    prices = data[sym].dropna()
    if len(prices) < 2:
        return None
    prior = float(prices.iloc[-2])
    last = float(prices.iloc[-1])
    if prior <= 0:
        return None
    return last / prior - 1.0


def _nyse_journal_entered_today(symbol: str, now=None, data=None) -> bool:
    """True if journal already has a NYSE momentum buy/signal for symbol today."""
    sym = config.normalize_symbol(symbol)
    day = _calendar_day_key(now, data=data)
    cached = _nyse_mom_entries_by_day.get(day) or set()
    if sym in cached:
        return True
    # Backtests only use in-memory marks — avoid live journal dates polluting PIT.
    try:
        if config.backtest_paper_sleeves_context():
            return False
    except Exception as exc:
        logger.debug("pipeline soft-fail: %s", exc)
    try:
        import pandas as pd
        from modules.paper_journal import ENTRY_EVENTS, journal_paths, normalize_journal_df, read_journal

        day_dt = datetime.strptime(day, "%Y-%m-%d").date()
        for path in journal_paths():
            try:
                df = read_journal(path=path)
            except Exception:
                continue
            df = normalize_journal_df(df)
            if df is None or df.empty:
                continue
            ticker = df.get("ticker")
            if ticker is None:
                continue
            mask = ticker.astype(str).str.upper() == sym
            if "event" in df.columns:
                mask &= df["event"].isin(ENTRY_EVENTS)
            if "side" in df.columns:
                mask &= df["side"].astype(str).str.lower().isin(["buy", ""])
            if "timestamp" not in df.columns:
                continue
            ts = pd.to_datetime(df["timestamp"], errors="coerce")
            mask &= ts.dt.date == day_dt
            if mask.any():
                return True
    except Exception as exc:
        logger.debug("journal same-day check skipped for %s: %s", sym, exc)
    return False


def _mark_nyse_entered_today(symbol: str, now=None, data=None) -> None:
    day = _calendar_day_key(now, data=data)
    _nyse_mom_entries_by_day.setdefault(day, set()).add(config.normalize_symbol(symbol))


def _nyse_held_qty(executor, symbol: str) -> float:
    """Signed long qty for symbol on executor (0 if flat / unknown)."""
    sym = config.normalize_symbol(symbol)
    if executor is None:
        return 0.0
    try:
        if hasattr(executor, "_find_position"):
            pos = executor._find_position(sym)
            if pos is not None and hasattr(executor, "_position_signed_qty"):
                return float(executor._position_signed_qty(pos) or 0.0)
        portfolio = getattr(executor, "portfolio", None)
        if portfolio is not None and hasattr(portfolio, "positions"):
            return float(portfolio.positions.get(sym, 0) or portfolio.positions.get(symbol, 0) or 0.0)
    except Exception as exc:
        logger.debug("nyse held qty check skipped for %s: %s", sym, exc)
    return 0.0


def _nyse_journal_buys_since_flat(symbol: str) -> int | None:
    """Count buy events after the latest exit in the paper journal (None if unavailable)."""
    sym = config.normalize_symbol(symbol)
    try:
        if config.backtest_paper_sleeves_context():
            return None
    except Exception:
        pass
    try:
        import pandas as pd
        from modules.paper_journal import (
            ENTRY_EVENTS,
            EXIT_EVENTS,
            journal_paths,
            normalize_journal_df,
            read_journal,
        )

        best = 0
        for path in journal_paths():
            try:
                df = read_journal(path=path)
            except Exception:
                continue
            df = normalize_journal_df(df)
            if df is None or df.empty or "ticker" not in df.columns:
                continue
            sub = df[df["ticker"].astype(str).str.upper() == sym].copy()
            if sub.empty or "timestamp" not in sub.columns:
                continue
            sub["_ts"] = pd.to_datetime(sub["timestamp"], errors="coerce")
            sub = sub.dropna(subset=["_ts"]).sort_values("_ts")
            if sub.empty:
                continue
            last_exit_ts = None
            if "event" in sub.columns:
                exits = sub[sub["event"].isin(EXIT_EVENTS)]
                if not exits.empty:
                    last_exit_ts = exits["_ts"].iloc[-1]
            elif "side" in sub.columns:
                exits = sub[sub["side"].astype(str).str.lower().isin(["sell", "exit"])]
                if not exits.empty:
                    last_exit_ts = exits["_ts"].iloc[-1]
            after = sub if last_exit_ts is None else sub[sub["_ts"] > last_exit_ts]
            if after.empty:
                continue
            if "event" in after.columns:
                buys = after[after["event"].isin(ENTRY_EVENTS)]
            elif "side" in after.columns:
                buys = after[after["side"].astype(str).str.lower().isin(["buy", ""])]
            else:
                buys = after
            best = max(best, int(len(buys)))
        return best if best > 0 else 0
    except Exception as exc:
        logger.debug("journal open-add count skipped for %s: %s", sym, exc)
        return None


def _nyse_open_add_count(executor, symbol: str) -> int:
    """Number of buys while the current long is open (0 if flat)."""
    sym = config.normalize_symbol(symbol)
    qty = _nyse_held_qty(executor, sym)
    if qty <= 1e-12:
        _nyse_open_add_counts.pop(sym, None)
        return 0
    if sym in _nyse_open_add_counts:
        return int(_nyse_open_add_counts[sym])
    journal_n = _nyse_journal_buys_since_flat(sym)
    if journal_n is not None and journal_n > 0:
        _nyse_open_add_counts[sym] = int(journal_n)
        return int(journal_n)
    # Holding but no journal trail — treat as one open unit.
    _nyse_open_add_counts[sym] = 1
    return 1


def _mark_nyse_open_add(symbol: str) -> None:
    sym = config.normalize_symbol(symbol)
    _nyse_open_add_counts[sym] = int(_nyse_open_add_counts.get(sym, 0)) + 1


def _nyse_session_sold_today(symbol: str, now=None, data=None) -> bool:
    """True if in-session/backtest state recorded a sell for symbol today."""
    sym = config.normalize_symbol(symbol)
    day = _calendar_day_key(now, data=data)
    return sym in _nyse_sold_today_by_day.get(day, set())


def _nyse_journal_sold_today(symbol: str, now=None, data=None) -> bool:
    """True if session state or journal has a sell/exit for symbol this calendar day."""
    sym = config.normalize_symbol(symbol)
    if _nyse_session_sold_today(sym, now=now, data=data):
        return True
    day = _calendar_day_key(now, data=data)
    try:
        if config.backtest_paper_sleeves_context():
            return False
    except Exception:
        pass
    try:
        import pandas as pd
        from modules.paper_journal import (
            EXIT_EVENTS,
            journal_paths,
            normalize_journal_df,
            read_journal,
        )

        day_dt = datetime.strptime(day, "%Y-%m-%d").date()
        for path in journal_paths():
            try:
                df = read_journal(path=path)
            except Exception:
                continue
            df = normalize_journal_df(df)
            if df is None or df.empty or "ticker" not in df.columns:
                continue
            mask = df["ticker"].astype(str).str.upper() == sym
            if "event" in df.columns:
                mask &= df["event"].isin(EXIT_EVENTS)
            if "side" in df.columns:
                mask &= df["side"].astype(str).str.lower().isin(["sell", "exit", ""])
            if "timestamp" not in df.columns:
                continue
            ts = pd.to_datetime(df["timestamp"], errors="coerce")
            mask &= ts.dt.date == day_dt
            if mask.any():
                return True
    except Exception as exc:
        logger.debug("journal same-day sell check skipped for %s: %s", sym, exc)
    return False


def _nyse_entry_hygiene_skip(executor, symbol: str, *, now=None, data=None) -> str | None:
    """Return skip reason for paper NYSE entry hygiene (max adds / same-day reentry)."""
    if not config.effective_paper_nyse_entry_hygiene():
        return None
    sym = config.normalize_symbol(symbol)
    if config.effective_paper_nyse_same_day_reentry_block():
        if _nyse_journal_sold_today(sym, now=now, data=data):
            _record_nyse_hygiene_skip("hygiene_same_day")
            return "same-day reentry block (sold earlier today)"
    max_adds = config.effective_paper_nyse_max_adds_per_symbol()
    open_adds = _nyse_open_add_count(executor, sym)
    # Flat → this buy would be add #1; held with N buys → next would be N+1.
    next_add = open_adds + 1 if open_adds >= 0 else 1
    if open_adds >= max_adds or next_add > max_adds:
        _record_nyse_hygiene_skip("hygiene_max_adds")
        return f"max adds/symbol ({open_adds}/{max_adds})"
    return None


def _record_nyse_min_notional_hygiene_skip() -> None:
    if config.effective_paper_nyse_entry_hygiene():
        _record_nyse_hygiene_skip("hygiene_min_notional")


def _nyse_momentum_quality_skip(symbol: str, data, now=None) -> str | None:
    """Return skip reason string when PAPER_MOMENTUM_QUALITY_FIXES blocks entry."""
    if not config.effective_paper_momentum_quality_fixes():
        return None
    if _nyse_open_cooldown_active(now):
        return "open cooldown (9:30-10:00 ET)"
    gap = _overnight_gap_pct(symbol, data)
    if gap is not None and gap > 0.02:
        return f"overnight gap {gap:.1%} too large"
    if _nyse_journal_entered_today(symbol, now, data=data):
        return "already entered this symbol today"
    rsi_max = _nyse_momentum_rsi_max(now)
    if rsi_max is not None:
        rsi = _nyse_symbol_rsi(symbol, data)
        if rsi is not None and rsi >= rsi_max:
            return f"RSI {rsi:.0f} overbought (max {rsi_max})"
    return None


def regime_soft_pause_sizing_multiplier(regime, *, wisdom_paused=False) -> float:
    """Paper soft-pause: scale entries in PAUSED_REGIMES instead of blocking."""
    if wisdom_paused and not config.effective_paper_soft_pause():
        return config.PAPER_SOFT_PAUSE_SIZING_MULT if config.PAPER_SOFT_PAUSE_ENABLED else 0.0
    if config.effective_paper_soft_pause() and regime in PAUSED_REGIMES:
        return float(config.PAPER_SOFT_PAUSE_SIZING_MULT)
    return 1.0


def regime_entries_paused(regime, data=None, sentiment=None):
    """True when new entries should be blocked (rhyme pause, bear, or daily loss circuit)."""
    try:
        from modules.trading_safety import entry_block_active

        if entry_block_active():
            return True
    except ImportError:
        pass
    if regime in PAUSED_REGIMES:
        return True
    if not config.DERIVED_BEAR_PAUSE_ENABLED or data is None:
        return False
    if sentiment is None:
        from modules.market_context import get_price_sentiment

        sentiment = get_price_sentiment(data)
    bullish, _ = _spy_market_up_signal(data, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    if not bullish and sentiment < config.DERIVED_BEAR_SENTIMENT_THRESHOLD:
        return True
    return False
# Sector tags for NYSE anti-overlap tests (subset of equity universe)
NYSE_SECTOR_MAP = {
    "AAPL": "Tech",
    "MSFT": "Tech",
    "NVDA": "Tech",
    "AMD": "Tech",
    "GOOGL": "Tech",
    "AMZN": "Tech",
    "TSLA": "Tech",
    "META": "Tech",
    "XOM": "Energy",
    "CVX": "Energy",
    "LNG": "Energy",
    "RTX": "Defense",
    "LMT": "Defense",
    "KTOS": "Defense",
    "ONDS": "Tech",
    "JPM": "Financials",
    "BAC": "Financials",
    "GS": "Financials",
    "JNJ": "Healthcare",
    "UNH": "Healthcare",
    "PFE": "Healthcare",
}
CRYPTO_Z_THRESHOLD = 2.0
MAX_CRYPTO_TRADES = 2
MAX_EQUITY_TRADES = 1
COOLDOWN_SECONDS = 3600


def load_pipeline_data(*, interval: str = "1d", days=None, force_refresh: bool = False):
    """Close-price matrix for pipeline sleeves, RVOL scans, and startup banners."""
    try:
        from modules.real_time_data import load_live_close_matrix

        return load_live_close_matrix(interval=interval, days=days, force_refresh=force_refresh)
    except ImportError:
        from modules.data_loader import load_close_matrix

        return load_close_matrix(interval=interval, days=days, force_refresh=force_refresh)


PAIR_FILL_WAIT = 5.0


def _count_if_filled(executor, order, *, max_wait=PAIR_FILL_WAIT):
    """Return 1 only when Alpaca confirms a fill (not a queued accept)."""
    if order is None:
        return 0
    if hasattr(executor, "order_filled"):
        return 1 if executor.order_filled(order, max_wait=max_wait) else 0
    return 1


def _order_fill_notional(executor, order, *, max_wait=PAIR_FILL_WAIT) -> float | None:
    if order is None:
        return None
    if hasattr(executor, "order_fill_details"):
        details = executor.order_fill_details(order, max_wait=max_wait)
        if details and details.get("filled"):
            notional = float(details.get("notional") or 0)
            return notional if notional > 0 else None
    return None


def _leg_has_exposure(executor, symbol) -> bool:
    if not hasattr(executor, "_find_position"):
        return False
    pos = executor._find_position(symbol)
    if pos is None:
        return False
    return abs(float(pos.qty)) > 1e-9


def _unwind_pair_leg(executor, symbol, *, max_wait=PAIR_FILL_WAIT) -> None:
    if not _leg_has_exposure(executor, symbol):
        return
    order = executor.execute_full_exit(symbol)
    if order is not None and hasattr(executor, "order_filled"):
        executor.order_filled(order, max_wait=max_wait)


def execute_atomic_pair_entry(
    executor,
    long_sym: str,
    short_sym: str,
    leg_n: float,
    *,
    pair_key: str = "",
    strategy: str = "",
    max_wait: float = PAIR_FILL_WAIT,
) -> tuple[bool, float | None, float | None]:
    """Both legs must fill; unwind any single-leg fill immediately."""
    long_order = executor.execute_order(
        long_sym, "buy", notional=leg_n, reason=pair_key or None, strategy=strategy or None
    )
    long_ok = bool(_count_if_filled(executor, long_order, max_wait=max_wait))
    short_order = executor.execute_order(
        short_sym, "sell", notional=leg_n, reason=pair_key or None, strategy=strategy or None
    )
    short_ok = bool(_count_if_filled(executor, short_order, max_wait=max_wait))
    if long_ok and short_ok:
        long_n = _order_fill_notional(executor, long_order, max_wait=0) or leg_n
        short_n = _order_fill_notional(executor, short_order, max_wait=0) or leg_n
        return True, long_n, short_n
    if long_ok:
        _unwind_pair_leg(executor, long_sym, max_wait=max_wait)
    if short_ok:
        _unwind_pair_leg(executor, short_sym, max_wait=max_wait)
    return False, None, None


def execute_atomic_pair_exit(
    executor,
    long_sym: str,
    short_sym: str,
    *,
    pair_key: str = "",
    max_wait: float = PAIR_FILL_WAIT,
) -> bool:
    """Close both legs; return True only when neither has exposure."""
    if _leg_has_exposure(executor, long_sym):
        order = executor.execute_full_exit(long_sym)
        if order is None or not _count_if_filled(executor, order, max_wait=max_wait):
            return False
    if _leg_has_exposure(executor, short_sym):
        order = executor.execute_full_exit(short_sym)
        if order is None or not _count_if_filled(executor, order, max_wait=max_wait):
            return False
    return not _leg_has_exposure(executor, long_sym) and not _leg_has_exposure(
        executor, short_sym
    )


def _on_cooldown(pair_cooldown, key, now, cooldown_seconds=COOLDOWN_SECONDS, cooldown_bars=None):
    last = pair_cooldown.get(key)
    if last is None:
        return False
    if cooldown_bars is not None:
        return (now - last) < cooldown_bars
    return (now - last).total_seconds() < cooldown_seconds


def _crypto_pair_z(data, t1, t2):
    spread = data[t1] - data[t2]
    return (spread.iloc[-1] - spread.mean()) / (spread.std() + 1e-9)


def _pair_leg_notional(total_notional, executor, *, sleeve_attempted: bool = False):
    """Split sleeve notional across two market-neutral legs; scale by dynamic risk."""
    if total_notional is None:
        if sleeve_attempted:
            return None, None
        if hasattr(executor, "compute_notional"):
            equity_fn = getattr(executor, "_get_account", None)
            if equity_fn:
                equity = float(equity_fn().equity)
                total_notional = round(
                    equity * config.effective_risk_per_trade(equity), 2
                )
        if total_notional is None:
            return None, None
    leg = round(float(total_notional) / 2, 2)
    min_n = config.effective_min_notional()
    if hasattr(executor, "_get_account"):
        try:
            min_n = config.effective_min_notional(float(executor._get_account().equity))
        except Exception as exc:
            logger.debug("equity-scaled min notional unavailable, using default: %s", exc)
    if leg < min_n:
        return None, None
    return leg, leg


def _momentum_score(data, symbol):
    prices = data[symbol].dropna()
    if len(prices) < 20:
        return None
    ma50 = prices.rolling(window=min(50, len(prices))).mean().iloc[-1]
    current = prices.iloc[-1]
    if ma50 <= 0:
        return None
    return current / ma50 - 1


def crypto_trade_intents(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=MAX_CRYPTO_TRADES,
    z_threshold=CRYPTO_Z_THRESHOLD,
    volatility=None,
    spacex_snapshot=None,
    notional=None,
):
    """Same logic as crypto strategy but returns intents for Kraken mirror (no Alpaca orders)."""
    from modules.crypto_vol_gate import crypto_trading_allowed

    crypto_cols = crypto_trading_columns(data)
    if len(crypto_cols) < 2:
        return []
    gate = crypto_trading_allowed(
        volatility or "Low",
        regime,
        spacex_snapshot=spacex_snapshot,
        data=data,
    )
    if not gate["allowed"]:
        return []

    min_corr = config.effective_pair_min_correlation()
    z_threshold = config.effective_pair_z_threshold(z_threshold)
    market_neutral = config.effective_market_neutral_pairs_enabled()

    candidates = []
    for i in range(len(crypto_cols)):
        for j in range(i + 1, len(crypto_cols)):
            t1, t2 = crypto_cols[i], crypto_cols[j]
            if data[t1].corr(data[t2]) < min_corr:
                continue
            z = _crypto_pair_z(data, t1, t2)
            if abs(z) > z_threshold:
                candidates.append((abs(z), z, t1, t2))

    candidates.sort(reverse=True)
    fired = set()
    intents = []
    for _abs_z, z, t1, t2 in candidates:
        if len(intents) >= max_trades:
            break
        if t1 in fired or t2 in fired:
            continue
        pair_key = t1 + "/" + t2
        if _on_cooldown(
            pair_cooldown,
            pair_key,
            now,
            cooldown_seconds=cooldown_seconds,
            cooldown_bars=cooldown_bars,
        ):
            continue
        if market_neutral:
            long_sym = t2 if z > 0 else t1
            short_sym = t1 if z > 0 else t2
            intents.append(
                {
                    "market_neutral": True,
                    "long_symbol": long_sym,
                    "short_symbol": short_sym,
                    "pair_key": pair_key,
                    "z_score": z,
                    "notional": notional,
                    "phase": "crypto_pair",
                }
            )
        else:
            side = "sell" if z > 0 else "buy"
            symbol = t1
            intents.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "pair_key": pair_key,
                    "z_score": z,
                    "notional": notional,
                    "phase": "crypto_mirror",
                }
            )
        fired.add(t1)
        fired.add(t2)
    return intents


def _execute_market_neutral_legs(executor, intent, *, log_fn=None, regime="", portfolio_manager=None):
    """Buy long leg and sell short leg; both must fill or neither is kept."""
    long_sym = intent["long_symbol"]
    short_sym = intent["short_symbol"]
    z = intent["z_score"]
    pair_key = intent["pair_key"]
    leg_n, _ = _pair_leg_notional(
        intent.get("notional"),
        executor,
        sleeve_attempted="notional" in intent,
    )
    if leg_n is None:
        return 0, False

    ok, _, _ = execute_atomic_pair_entry(executor, long_sym, short_sym, leg_n)
    if not ok:
        return 0, False

    msg = f"Market-neutral pair: LONG {long_sym} / SHORT {short_sym}, Z={round(z, 1)}"
    if log_fn:
        log_fn(long_sym, "buy", regime, pair_key, z, leg_n, pair_msg=msg)
        log_fn(short_sym, "sell", regime, pair_key, z, leg_n, pair_msg=msg)
    if hasattr(executor, "register_pair_symbols"):
        executor.register_pair_symbols(long_sym, short_sym)
    if portfolio_manager:
        portfolio_manager.add_position(pair_key, z, 0)
    return 1, True


def equity_pair_trade_intents(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=1,
    yield_gated=False,
    notional=None,
):
    """Long strongest / short weakest NYSE name when spread z-score fires (paper only)."""
    if not config.effective_equity_pairs_enabled():
        return []
    if regime_entries_paused(regime, data) or config.effective_yield_gate(
        yield_gated, regime=regime
    ):
        return []

    equity_cols = _nyse_equity_columns(data)
    if len(equity_cols) < 2:
        return []

    min_corr = config.effective_pair_min_correlation()
    z_threshold = config.effective_pair_z_threshold()

    candidates = []
    for i in range(len(equity_cols)):
        for j in range(i + 1, len(equity_cols)):
            t1, t2 = equity_cols[i], equity_cols[j]
            if data[t1].corr(data[t2]) < min_corr:
                continue
            z = _crypto_pair_z(data, t1, t2)
            if abs(z) <= z_threshold:
                continue
            mom1 = _momentum_score(data, t1)
            mom2 = _momentum_score(data, t2)
            if mom1 is None or mom2 is None:
                continue
            if mom1 >= mom2:
                long_sym, short_sym = t1, t2
            else:
                long_sym, short_sym = t2, t1
            candidates.append((abs(z), z, long_sym, short_sym))

    candidates.sort(reverse=True)
    intents = []
    for _abs_z, z, long_sym, short_sym in candidates:
        if len(intents) >= max_trades:
            break
        pair_key = f"{long_sym}/{short_sym}"
        if _on_cooldown(
            pair_cooldown,
            pair_key,
            now,
            cooldown_seconds=cooldown_seconds,
            cooldown_bars=cooldown_bars,
        ):
            continue
        intents.append(
            {
                "market_neutral": True,
                "long_symbol": long_sym,
                "short_symbol": short_sym,
                "pair_key": pair_key,
                "z_score": z,
                "notional": notional,
                "phase": "equity_pair",
            }
        )
    return intents


def run_equity_pairs_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    log_fn=None,
    portfolio_manager=None,
    yield_gated=False,
):
    """Market-neutral NYSE pair sleeve — paper aggressive + PAPER_EQUITY_PAIRS only."""
    if config.effective_stat_arb_enabled():
        from modules.stat_arb_sleeve import run_equity_stat_arb

        return run_equity_stat_arb(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            log_fn=log_fn,
            portfolio_manager=portfolio_manager,
            yield_gated=yield_gated,
        )

    notional = None
    if hasattr(executor, "compute_nyse_notional"):
        notional = executor.compute_nyse_notional()

    intents = equity_pair_trade_intents(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        yield_gated=yield_gated,
        notional=notional,
    )
    trades = 0
    for intent in intents:
        n, ok = _execute_market_neutral_legs(
            executor,
            intent,
            log_fn=log_fn,
            regime=regime,
            portfolio_manager=portfolio_manager,
        )
        if ok:
            pair_cooldown[intent["pair_key"]] = now
            trades += n
    return trades


def run_crypto_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=MAX_CRYPTO_TRADES,
    z_threshold=CRYPTO_Z_THRESHOLD,
    log_fn=None,
    portfolio_manager=None,
    volatility=None,
    spacex_snapshot=None,
):
    """Z-score pairs; paper aggressive uses cointegration stat arb when enabled."""
    if not config.effective_crypto_enabled():
        return 0

    if config.effective_crypto_v2_enabled():
        from modules.crypto_dual_sleeve import run_crypto_dual_sleeve

        return run_crypto_dual_sleeve(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            max_trades=max_trades,
            log_fn=log_fn,
            portfolio_manager=portfolio_manager,
            volatility=volatility,
        full_data=full_data,
        bar_idx=bar_idx,
            spacex_snapshot=spacex_snapshot,
        )

    if config.effective_stat_arb_enabled():
        from modules.stat_arb_sleeve import run_crypto_stat_arb

        return run_crypto_stat_arb(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            max_trades=max_trades,
            log_fn=log_fn,
            portfolio_manager=portfolio_manager,
            volatility=volatility,
            spacex_snapshot=spacex_snapshot,
        )

    notional = None
    if hasattr(executor, "compute_crypto_notional"):
        notional = executor.compute_crypto_notional()

    intents = crypto_trade_intents(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        max_trades=max_trades,
        z_threshold=z_threshold,
        volatility=volatility,
        spacex_snapshot=spacex_snapshot,
        notional=notional,
    )
    trades = 0
    for intent in intents:
        if intent.get("market_neutral"):
            n, ok = _execute_market_neutral_legs(
                executor,
                intent,
                log_fn=log_fn,
                regime=regime,
                portfolio_manager=portfolio_manager,
            )
            if ok:
                pair_cooldown[intent["pair_key"]] = now
                trades += n
            continue

        t1 = intent["symbol"]
        side = intent["side"]
        pair_key = intent["pair_key"]
        z = intent["z_score"]
        trade_notional = intent.get("notional")
        if side == "buy" and trade_notional is None:
            continue
        order = executor.execute_order(
            t1, side, notional=trade_notional, reason=pair_key, sleeve="Crypto"
        )
        if not _count_if_filled(executor, order, max_wait=3.0):
            continue
        pair_cooldown[pair_key] = now
        trades += 1
        if portfolio_manager:
            portfolio_manager.add_position(pair_key, z, 0)
        if log_fn:
            if trade_notional is None:
                trade_notional = getattr(executor, "compute_notional", lambda: "")()
            log_fn(t1, side, regime, pair_key, z, trade_notional)
    return trades


def _apply_strict_candidate_intersection(cols: list[str], data) -> list[str]:
    """Intersect MA50 pool with strict screener tickers; widen slightly if too thin."""
    try:
        from modules.dynamic_universe import screener_momentum_order, strict_screener_symbol_set
    except ImportError:
        return cols
    strict_set = strict_screener_symbol_set()
    if not strict_set:
        return cols
    intersected = [c for c in cols if config.normalize_symbol(c) in strict_set]
    min_names = max(1, int(getattr(config, "NYSE_STRICT_INTERSECT_MIN", 3)))
    if len(intersected) >= min_names:
        logger.debug(
            "[UNIVERSE] strict-at-candidate intersect: %d -> %d names",
            len(cols),
            len(intersected),
        )
        return intersected
    order = screener_momentum_order(cols) or []
    widened = list(
        dict.fromkeys(
            [
                *intersected,
                *[s for s in order if config.normalize_symbol(s) in strict_set],
            ]
        )
    )
    if len(widened) >= min_names:
        logger.debug(
            "[UNIVERSE] strict-at-candidate widened: %d -> %d names (min=%d)",
            len(cols),
            len(widened),
            min_names,
        )
        return widened
    logger.debug(
        "[UNIVERSE] strict-at-candidate fallback — overlap %d < min %d",
        len(intersected),
        min_names,
    )
    return cols


def _nyse_equity_columns(data):
    """NYSE momentum sleeve symbols (static columns or dynamic screener)."""
    # Live Profile A: never use screener / USE_DYNAMIC_UNIVERSE (fixed pool only).
    live_book = not config.PAPER_TRADING and not config.paper_only_sleeves_active()
    if live_book:
        allowed = set(config.get_nyse_universe_fixed())
        cols = [c for c in data.columns if c in allowed]
    elif config.USE_DYNAMIC_UNIVERSE:
        # Paper: fixed ∪ screener via get_nyse_universe(); intersect with loaded bars.
        allowed = set(config.get_nyse_universe())
        cols = [
            c
            for c in data.columns
            if c in allowed and config._nyse_eligible_symbol(c)
        ]
        # Guard against a collapsed pool (screener names without price columns):
        # merge with the full static/dynamic universe so downstream sleeves
        # always see a workable pool (~20+ names) instead of 0-1.
        min_cols = int(getattr(config, "STAT_ARB_MIN_UNIVERSE", 20) or 20)
        if len(cols) < min_cols:
            fallback = config.nyse_momentum_universe(data.columns)
            cols = list(dict.fromkeys([*cols, *fallback]))
            logger.debug(
                "[UNIVERSE] _nyse_equity_columns below floor — merged to %d "
                "names (top: %s)",
                len(cols),
                ", ".join(cols[:10]),
            )
    elif config.effective_dynamic_sector_screener():
        try:
            from modules.sector_screener import get_expanded_universe

            expanded = get_expanded_universe(data.columns, data)
            cols = [
                c
                for c in expanded
                if c in data.columns and config._nyse_eligible_symbol(c)
            ]
        except Exception as exc:
            logger.debug("sector expanded universe skipped: %s", exc)
            cols = config.nyse_momentum_universe(data.columns)
        min_cols = int(getattr(config, "STAT_ARB_MIN_UNIVERSE", 20) or 20)
        if len(cols) < min_cols:
            fallback = config.nyse_momentum_universe(data.columns)
            cols = list(dict.fromkeys([*cols, *fallback]))
    else:
        cols = config.nyse_momentum_universe(data.columns)
    if config.effective_nyse_strict_intersect_candidates():
        cols = _apply_strict_candidate_intersection(cols, data)
    if config.effective_rvol_scanner_enabled():
        try:
            from modules.volume_analysis import apply_rvol_universe_boost

            cols = apply_rvol_universe_boost(cols, data)
        except Exception as exc:
            logger.debug("RVOL universe boost skipped: %s", exc)
    return cols


def _equity_momentum_candidates(
    data, equity_cols, executor=None, now=None, *, bar_idx: int | None = None, full_data=None
):
    from modules.dynamic_universe import IPO_MIN_TRADING_DAYS, is_ipo_symbol, is_ipo_trading_days

    src = full_data if full_data is not None else data
    rows = []
    # v1.5.1: allow entries within a small band below MA (flexibility) to reduce drag.
    tol = max(0.0, float(getattr(config, "NYSE_MA_ENTRY_TOLERANCE_PCT", 0.0)))
    entry_floor = 1.0 - tol
    for symbol in equity_cols:
        if bar_idx is not None and full_data is not None and symbol in full_data.columns:
            prices = full_data[symbol].iloc[: bar_idx + 1].dropna()
            ipo = is_ipo_symbol(symbol, data=full_data, bar_idx=bar_idx)
        elif full_data is not None and symbol in full_data.columns:
            first = full_data[symbol].first_valid_index()
            end = data.index[-1] if len(data.index) else None
            if first is not None and end is not None:
                prices = full_data[symbol].loc[first:end].dropna()
                ipo = is_ipo_trading_days(len(prices))
            else:
                prices = data[symbol].dropna()
                ipo = is_ipo_symbol(symbol, data=src)
        else:
            prices = data[symbol].dropna()
            ipo = is_ipo_symbol(symbol, data=src)
        min_bars = IPO_MIN_TRADING_DAYS if ipo else 20
        ma_window = 20 if ipo else 50
        if len(prices) < min_bars:
            continue
        window = min(ma_window, len(prices))
        ma = prices.rolling(window=window).mean().iloc[-1]
        current = prices.iloc[-1]
        if ma > 0 and current > ma * entry_floor:
            rows.append((current / ma - 1, symbol))
    rows.sort(reverse=True)
    # v1.5.1: amplify scanner-driven rank boosts (RVOL/ORB/Catalyst) so high-signal
    # names rise in the momentum ranking without changing base momentum economics.
    #
    # EXTENSION POINT — adding a new scanner/signal rank boost:
    #   Follow the guarded block pattern below: gate on a config.effective_*()
    #   flag, import the boost fn lazily inside the try (avoids import cycles and
    #   keeps the scanner optional), add `score + boost(sym, data) * rank_w`, then
    #   re-sort. Always keep the `except -> logger.debug` so a broken scanner
    #   degrades to base momentum instead of killing the whole ranking pass.
    rank_w = float(getattr(config, "NYSE_RANK_SCANNER_WEIGHT", 1.0))
    if config.effective_insider_monitor_enabled():
        try:
            from modules.insider_monitor import momentum_rank_boost

            rows = [
                (score + momentum_rank_boost(sym, executor), sym) for score, sym in rows
            ]
            rows.sort(reverse=True)
        except Exception as exc:
            logger.debug("insider momentum rank boost skipped: %s", exc)
    if config.effective_rvol_scanner_enabled():
        try:
            from modules.volume_analysis import rvol_momentum_rank_boost

            rows = [
                (score + rvol_momentum_rank_boost(sym, data) * rank_w, sym)
                for score, sym in rows
            ]
            rows.sort(reverse=True)
        except Exception as exc:
            logger.debug("RVOL momentum rank boost skipped: %s", exc)
    if config.effective_orb_enabled():
        try:
            from modules.orb_strategy import orb_momentum_rank_boost

            rows = [
                (score + orb_momentum_rank_boost(sym, data) * rank_w, sym)
                for score, sym in rows
            ]
            rows.sort(reverse=True)
        except Exception as exc:
            logger.debug("ORB momentum rank boost skipped: %s", exc)
    if config.effective_catalyst_scoring_enabled():
        try:
            from modules.catalyst_scoring import catalyst_momentum_rank_boost

            rows = [
                (score + catalyst_momentum_rank_boost(sym, data) * rank_w, sym)
                for score, sym in rows
            ]
            rows.sort(reverse=True)
        except Exception as exc:
            logger.debug("catalyst momentum rank boost skipped: %s", exc)
    if config.effective_multi_timeframe_enabled():
        try:
            from modules.multi_timeframe import multi_timeframe_momentum_rank_boost

            rows = [
                (score + multi_timeframe_momentum_rank_boost(sym, data), sym)
                for score, sym in rows
            ]
            rows.sort(reverse=True)
        except Exception as exc:
            logger.debug("multi-timeframe momentum rank boost skipped: %s", exc)
    tod_boost = _nyse_time_of_day_rank_boost(now)
    if tod_boost > 0 and rows:
        rows = [(score + tod_boost, sym) for score, sym in rows]
        rows.sort(reverse=True)
    return [s for _, s in rows]


def _is_nyse_tech(symbol):
    return NYSE_SECTOR_MAP.get(symbol) == "Tech"


def _spy_vs_equity_metrics(data, symbol, lookback=None):
    """60d return correlation and beta vs SPY (0, 0 if insufficient data)."""
    lookback = lookback or config.NYSE_SPY_CORR_LOOKBACK
    spy = config.SPY_BOT_SYMBOL
    if symbol not in data.columns or spy not in data.columns:
        return 0.0, 0.0
    rets = data[[symbol, spy]].pct_change().dropna().tail(lookback)
    if len(rets) < 20:
        return 0.0, 0.0
    corr = float(rets[symbol].corr(rets[spy]))
    if not np.isfinite(corr):
        corr = 0.0
    spy_var = float(rets[spy].var())
    if spy_var < 1e-12:
        return corr, 0.0
    beta = float(rets[symbol].cov(rets[spy]) / spy_var)
    if not np.isfinite(beta):
        beta = 0.0
    return corr, beta


def _spy_sleeve_active(data, *, yield_gated=False, regime=None):
    if regime_entries_paused(regime, data) or config.effective_yield_gate(
        yield_gated, regime=regime
    ):
        return False
    bullish, _ = _spy_market_up_signal(data, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    return bullish


def _filter_nyse_anti_overlap(data, ranked):
    """Drop names too correlated / high-beta vs SPY; keep momentum order."""
    if not ranked:
        return ranked
    out = []
    for symbol in ranked:
        corr, beta = _spy_vs_equity_metrics(data, symbol)
        if corr > config.NYSE_SPY_CORR_MAX or beta > config.NYSE_SPY_BETA_MAX:
            continue
        out.append(symbol)
    return out


def _apply_sector_tech_cap(ranked, *, top_n=3, max_tech=None):
    """At most max_tech Tech names in the first top_n momentum slots."""
    max_tech = config.NYSE_SECTOR_TECH_CAP if max_tech is None else max_tech
    if max_tech <= 0 or not ranked:
        return ranked
    primary = []
    deferred = []
    tech_count = 0
    for sym in ranked:
        if len(primary) < top_n:
            if _is_nyse_tech(sym) and tech_count >= max_tech:
                deferred.append(sym)
                continue
            if _is_nyse_tech(sym):
                tech_count += 1
            primary.append(sym)
        else:
            deferred.append(sym)
    fill = []
    remaining = []
    for sym in deferred:
        if len(primary) + len(fill) < top_n:
            if _is_nyse_tech(sym) and tech_count >= max_tech:
                remaining.append(sym)
                continue
            if _is_nyse_tech(sym):
                tech_count += 1
            fill.append(sym)
        else:
            remaining.append(sym)
    return primary + fill + remaining


def _apply_screener_momentum_order(ranked):
    """Strict dynamic universe: prefer screener 30d momentum rank among MA50 picks."""
    try:
        from modules.dynamic_universe import screener_momentum_order

        screener_order = screener_momentum_order(ranked)
    except ImportError:
        return ranked
    if not screener_order:
        return ranked
    order_index = {sym: i for i, sym in enumerate(screener_order)}
    return sorted(ranked, key=lambda s: order_index.get(s, len(order_index)))


def _executor_equity(executor) -> float:
    if hasattr(executor, "portfolio"):
        return float(executor.portfolio.equity(executor.prices))
    return float(executor._get_account().equity)


def _apply_ipo_buy_notional(
    symbol: str,
    notional: float,
    equity: float,
    *,
    data=None,
    bar_idx: int | None = None,
) -> float:
    from modules.dynamic_universe import cap_ipo_buy_notional

    return cap_ipo_buy_notional(symbol, notional, equity, data=data, bar_idx=bar_idx)


def _record_ipo_buy(executor, symbol, *, data=None, bar_idx: int | None = None) -> None:
    from modules.dynamic_universe import is_ipo_symbol

    if not is_ipo_symbol(symbol, data=data, bar_idx=bar_idx):
        return
    stats = getattr(executor, "ipo_stats", None)
    if stats is None:
        executor.ipo_stats = {"buys": 0, "trims": 0, "trim_notional": 0.0}
        stats = executor.ipo_stats
    stats["buys"] += 1


def _is_nyse_momentum_position(symbol: str) -> bool:
    sym = config.normalize_symbol(symbol)
    if config.is_crypto(sym):
        return False
    if sym == config.SPY_BOT_SYMBOL:
        return False
    if config.is_metal_symbol(sym):
        return False
    if sym == config.VTI_CORE_SYMBOL:
        return False
    return True


def run_ipo_safety_trims(
    data,
    executor,
    *,
    log_fn=None,
    bar_idx: int | None = None,
) -> int:
    """Trim IPO positions at +20% unrealized gain down to 1% of equity."""
    from modules.cost_basis import _position_cost
    from modules.dynamic_universe import ipo_safety_enabled, ipo_trim_reduce_notional, is_ipo_symbol

    if not ipo_safety_enabled():
        return 0

    equity = _executor_equity(executor)
    min_n = config.effective_min_notional(equity)
    trims = 0

    if hasattr(executor, "portfolio"):
        symbols = [
            sym
            for sym, qty in executor.portfolio.positions.items()
            if float(qty) > 0 and _is_nyse_momentum_position(sym)
        ]
        positions = [executor._find_position(sym) for sym in symbols]
    else:
        positions = [
            pos
            for pos in executor._get_positions()
            if float(pos.qty) > 0 and _is_nyse_momentum_position(pos.symbol)
        ]

    for pos in positions:
        if pos is None:
            continue
        sym = config.normalize_symbol(pos.symbol)
        if not is_ipo_symbol(sym, data=data, bar_idx=bar_idx):
            continue
        cost, value, _ = _position_cost(pos)
        reduce_n = ipo_trim_reduce_notional(equity, cost, value)
        if reduce_n is None:
            continue
        reduce_n = round(float(reduce_n), 2)
        if reduce_n <= 0 or reduce_n < min_n:
            continue
        if hasattr(executor, "execute_reduce_notional"):
            order = executor.execute_reduce_notional(
                sym, reduce_n, reason="ipo_trim", sleeve="NYSE"
            )
        else:
            order = executor.execute_order(
                sym, "sell", notional=reduce_n, reason="ipo_trim", sleeve="NYSE"
            )
        if not _count_if_filled(executor, order):
            continue
        trims += 1
        stats = getattr(executor, "ipo_stats", None)
        if stats is None:
            executor.ipo_stats = {"buys": 0, "trims": 0, "trim_notional": 0.0}
            stats = executor.ipo_stats
        stats["trims"] += 1
        stats["trim_notional"] = round(float(stats["trim_notional"]) + reduce_n, 2)
        if log_fn:
            log_fn(sym, "ipo_trim", "", "ipo_trim", 0.0, reduce_n)

    return trims


def _equity_momentum_ranked(
    data,
    equity_cols,
    *,
    yield_gated=False,
    regime=None,
    executor=None,
    now=None,
    bar_idx: int | None = None,
    full_data=None,
):
    ranked = _equity_momentum_candidates(
        data, equity_cols, executor=executor, now=now, bar_idx=bar_idx, full_data=full_data
    )
    if not ranked:
        return ranked
    if config.effective_paper_dynamic_universe_strict():
        ranked = _apply_screener_momentum_order(ranked)
    if _spy_sleeve_active(data, yield_gated=yield_gated, regime=regime):
        if config.NYSE_SECTOR_TECH_CAP > 0:
            ranked = _apply_sector_tech_cap(ranked)
        if config.effective_nyse_overlap_filter_enabled():
            ranked = _filter_nyse_anti_overlap(data, ranked)
    return ranked


def _spy_market_up_signal(data, symbol, ma_window):
    """True when price is above the moving average (market-up bet)."""
    if symbol not in data.columns:
        return False, 0.0
    prices = data[symbol].dropna()
    if len(prices) < ma_window:
        return False, 0.0
    window = min(ma_window, len(prices))
    ma = prices.rolling(window=window).mean().iloc[-1]
    current = prices.iloc[-1]
    if ma <= 0 or current <= ma:
        return False, 0.0
    return True, current / ma - 1


def _holds_symbol(executor, symbol):
    target = config.normalize_symbol(symbol)
    if hasattr(executor, "portfolio"):
        for sym, qty in executor.portfolio.positions.items():
            if config.normalize_symbol(sym) == target and float(qty) > 0:
                return True
        return False
    try:
        return any(
            config.normalize_symbol(p.symbol) == target
            for p in executor.client.get_all_positions()
        )
    except Exception as exc:
        logger.debug("position lookup via broker failed for %s: %s", target, exc)
        return False


def _sleeve_room(executor, cap_pct, value_fn):
    if hasattr(executor, "portfolio"):
        equity = executor.portfolio.equity(executor.prices)
    else:
        account = executor._get_account()
        equity = float(account.equity)
    cap = round(equity * cap_pct, 2)
    return round(cap - value_fn(), 2)


def _spy_buy_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    yield_gated=False,
    symbol=None,
    ma_window=None,
):
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if regime_entries_paused(regime, data) or config.effective_yield_gate(
        yield_gated, regime=regime
    ):
        return False
    bullish, _ = _spy_market_up_signal(data, symbol, ma_window)
    if not bullish:
        return False
    pair_key = f"{symbol}/MA{ma_window}"
    return not _on_cooldown(
        pair_cooldown,
        pair_key,
        now,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
    )


def _nyse_buy_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    yield_gated=False,
):
    if regime_entries_paused(regime, data):
        return False
    equity_cols = _nyse_equity_columns(data)
    ranked = _equity_momentum_ranked(
        data, equity_cols, yield_gated=yield_gated, regime=regime, now=now
    )
    if not ranked:
        return False
    pair_key = ranked[0] + "/MA50"
    return not _on_cooldown(
        pair_cooldown,
        pair_key,
        now,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
    )


def _crypto_buy_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    volatility=None,
    spacex_snapshot=None,
):
    intents = crypto_trade_intents(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        volatility=volatility,
        spacex_snapshot=spacex_snapshot,
    )
    return any(
        i.get("side") == "buy" or i.get("market_neutral")
        for i in intents
    )


def resolve_cycle_deploy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    volatility=None,
    spacex_snapshot=None,
    yield_gated=False,
    market_open=True,
):
    """Pre-compute co-fire sleeve notionals when 2+ sleeves want to buy."""
    if hasattr(executor, "begin_deployment_cycle"):
        executor.begin_deployment_cycle()
    else:
        executor.set_cofire_allocations({})
        return

    if hasattr(executor, "set_sizing_context"):
        executor.set_sizing_context(data)

    if not config.effective_cofire_budget_enabled():
        return

    rooms = {}
    spy_cap = config.effective_sleeve_cap(config.SPY_SLEEVE_CAP_PCT)
    crypto_cap = config.effective_sleeve_cap(config.CRYPTO_SLEEVE_CAP_PCT)
    nyse_cap = config.effective_nyse_sleeve_cap_pct()
    if hasattr(executor, "_get_account"):
        try:
            acct = executor._get_account()
            eq = float(acct.equity)
            ca = float(acct.cash)
            nyse_cap = config.effective_nyse_sleeve_cap_pct(
                equity=eq, cash=ca
            )
        except Exception as exc:
            logger.debug("NYSE cap account probe failed; using default cap: %s", exc)

    if hasattr(executor, "portfolio"):
        equity = executor.portfolio.equity(executor.prices)
    else:
        equity = float(executor._get_account().equity)
    room_min = config.effective_no_room_min_notional(equity)

    if market_open and _spy_buy_intent(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        yield_gated=yield_gated,
    ):
        room = _sleeve_room(executor, spy_cap, executor.spy_sleeve_value)
        if room >= room_min:
            rooms["spy"] = room

    if (
        config.effective_crypto_enabled()
        and _crypto_buy_intent(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        volatility=volatility,
        spacex_snapshot=spacex_snapshot,
    )
    ):
        room = _sleeve_room(executor, crypto_cap, executor.crypto_sleeve_value)
        if room >= room_min:
            rooms["crypto"] = room

    if market_open and _nyse_buy_intent(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        yield_gated=yield_gated,
    ):
        room = _sleeve_room(executor, nyse_cap, executor.nyse_sleeve_value)
        if room >= room_min:
            rooms["nyse"] = room

    if len(rooms) < 2:
        return

    if hasattr(executor, "portfolio"):
        cash = executor.portfolio.cash
    else:
        account = executor._get_account()
        cash = float(account.cash)

    allocations = deployment_sizing.compute_cofire_allocations(equity, cash, rooms)
    executor.set_cofire_allocations(allocations)


def run_spy_exits(
    data,
    executor,
    regime="",
    *,
    symbol=None,
    ma_window=None,
    log_fn=None,
):
    """Sell full SPY position when price closes below the moving average."""
    if not config.effective_spy_exit_on_ma_break():
        return 0
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if not _holds_symbol(executor, symbol):
        return 0
    bullish, momentum = _spy_market_up_signal(data, symbol, ma_window)
    if bullish:
        return 0

    if (
        config.COST_BASIS_AWARE_ENABLED
        and config.DISCRETIONARY_SELL_BELOW_COST
        and hasattr(executor, "_find_position")
    ):
        from modules.cost_basis import position_below_cost

        if position_below_cost(executor, symbol):
            return 0

    pair_key = f"{symbol}/MA{ma_window}"
    if hasattr(executor, "execute_full_exit"):
        order = executor.execute_full_exit(symbol, reason=pair_key, sleeve="SPY")
    else:
        order = executor.execute_order(
            symbol, "sell", reduce_only=True, reason=pair_key, sleeve="SPY"
        )
    if not _count_if_filled(executor, order):
        return 0
    if log_fn:
        notional = ""
        if isinstance(order, dict):
            notional = order.get("notional", "")
        log_fn(symbol, "sell", regime, pair_key, momentum, notional)
    return 1


def run_spy_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    symbol=None,
    ma_window=None,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    log_fn=None,
    portfolio_manager=None,
    yield_gated=False,
):
    """Buy SPY when above MA — a simple bet that the broad market keeps rising."""
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if regime_entries_paused(regime, data):
        return 0
    if config.effective_yield_gate(yield_gated, regime=regime):
        return 0
    bullish, momentum = _spy_market_up_signal(data, symbol, ma_window)
    if not bullish:
        return 0

    pair_key = f"{symbol}/MA{ma_window}"
    if _on_cooldown(
        pair_cooldown,
        pair_key,
        now,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
    ):
        return 0

    notional = None
    if hasattr(executor, "compute_spy_notional"):
        notional = executor.compute_spy_notional()
        if notional is None:
            return 0
    order = executor.execute_order(
        symbol, "buy", notional=notional, reason=pair_key, sleeve="SPY"
    )
    if not _count_if_filled(executor, order):
        return 0
    pair_cooldown[pair_key] = now
    if portfolio_manager:
        portfolio_manager.add_position(pair_key, momentum, 0)
    if log_fn:
        if notional is None:
            notional = getattr(executor, "compute_notional", lambda: "")()
        log_fn(symbol, "buy", regime, pair_key, momentum, notional)
    return 1


def run_equity_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=MAX_EQUITY_TRADES,
    log_fn=None,
    portfolio_manager=None,
    yield_gated=False,
    pick_log=None,
    volatility=None,
    full_data=None,
    bar_idx: int | None = None,
):
    """Buy the equity with the strongest momentum above MA50 (not arbitrary column order)."""
    if regime_entries_paused(regime, data):
        return 0
    ipo_data = full_data if full_data is not None else data
    equity_cols = _nyse_equity_columns(data)
    ranked = _equity_momentum_ranked(
        data,
        equity_cols,
        yield_gated=yield_gated,
        regime=regime,
        executor=executor,
        now=now,
        bar_idx=bar_idx,
        full_data=ipo_data,
    )
    if not ranked:
        return 0

    bar_index = _nyse_pick_bar_index(now, cooldown_bars)
    trades = 0
    equity = _executor_equity(executor)
    min_n = config.effective_paper_nyse_min_notional(equity)
    for symbol in ranked:
        if trades >= max_trades:
            break
        if bar_index is not None and _nyse_pick_rotation_blocked(symbol, bar_index):
            logger.debug(
                "[NYSE] Skipping %s — pick rotation cap (%d/%d bars)",
                symbol,
                config.NYSE_MAX_PICKS_PER_SYMBOL_WINDOW,
                config.NYSE_PICK_WINDOW_BARS,
            )
            continue
        if pick_log is not None:
            pick_log.append(symbol)
        if bar_index is not None:
            _record_nyse_pick(symbol, bar_index)
        skip = _nyse_momentum_quality_skip(symbol, data, now=now)
        if skip:
            logger.info("[NYSE] Skipping %s — %s", symbol, skip)
            continue
        hygiene = _nyse_entry_hygiene_skip(executor, symbol, now=now, data=data)
        if hygiene:
            logger.info("[NYSE] Skipping %s — hygiene: %s", symbol, hygiene)
            continue
        pair_key = symbol + "/MA50"
        if _on_cooldown(
            pair_cooldown,
            pair_key,
            now,
            cooldown_seconds=cooldown_seconds,
            cooldown_bars=cooldown_bars,
        ):
            continue
        try:
            from modules.strategy_performance import classify_nyse_entry_tags, format_reason_with_tags

            boost_tags = classify_nyse_entry_tags(symbol, data)
            pair_key = format_reason_with_tags(pair_key, boost_tags)
        except Exception as exc:
            logger.debug("NYSE entry tag classification skipped for %s: %s", symbol, exc)
        notional = None
        garch_multiplier = 1.0
        if hasattr(executor, "compute_nyse_notional"):
            notional = executor.compute_nyse_notional()
            if notional is None:
                continue
            min_n = config.effective_paper_nyse_min_notional(
                float(executor._get_account().equity)
            )
            # Paper-only GARCH vol-target sizing (PAPER_GARCH_SIZING=true)
            try:
                from modules.garch_sizer import (
                    get_multiplier,
                    paper_garch_sizing_enabled,
                    spy_series_from_data,
                )

                if paper_garch_sizing_enabled():
                    spy_px = spy_series_from_data(data)
                    garch_multiplier = float(get_multiplier(spy_px) if spy_px is not None else 1.0)
                    notional = round(float(notional) * garch_multiplier, 2)
                    if notional < min_n:
                        _record_nyse_min_notional_hygiene_skip()
                        continue
                    logger.info(
                        "[NYSE] garch_multiplier=%.3f notional=%.2f",
                        garch_multiplier,
                        notional,
                    )
            except Exception:
                garch_multiplier = 1.0
            if config.NYSE_BETA_SCALING_ENABLED:
                _, beta = _spy_vs_equity_metrics(data, symbol)
                scaled = round(notional * deployment_sizing.nyse_beta_scale(beta), 2)
                if scaled < min_n:
                    _record_nyse_min_notional_hygiene_skip()
                    continue
                notional = scaled
            vol_scale = config.dynamic_equity_position_scale(
                symbol, data=ipo_data, bar_idx=bar_idx
            )
            if vol_scale < 1.0:
                notional = round(float(notional) * vol_scale, 2)
                if notional < min_n:
                    _record_nyse_min_notional_hygiene_skip()
                    continue
            if config.effective_conviction_sizing_enabled():
                from modules.risk_management import (
                    compute_conviction_score,
                    scale_notional_by_conviction,
                )

                equity = float(executor._get_account().equity)
                conviction = compute_conviction_score(
                    symbol, data, regime, sleeve="nyse"
                )
                notional = scale_notional_by_conviction(
                    notional,
                    equity,
                    conviction,
                    symbol=symbol,
                    data=data,
                    sleeve="NYSE",
                    strategy_id="nyse_momentum_base",
                )
                if notional is None or notional < min_n:
                    _record_nyse_min_notional_hygiene_skip()
                    continue
            if config.effective_correlation_guard_enabled():
                from modules.risk_management import apply_correlation_guard_notional

                notional = apply_correlation_guard_notional(
                    notional,
                    float(executor._get_account().equity),
                    executor,
                    data,
                    symbol=symbol,
                )
                if notional is None or notional < min_n:
                    _record_nyse_min_notional_hygiene_skip()
                    continue
            if config.effective_insider_signal_boost_enabled():
                try:
                    from modules.insider_signal_handler import cap_insider_boost_notional

                    notional = cap_insider_boost_notional(
                        symbol,
                        notional,
                        float(executor._get_account().equity),
                        executor,
                    )
                    if notional is None or notional < min_n:
                        _record_nyse_min_notional_hygiene_skip()
                        continue
                except Exception as exc:
                    logger.debug("insider notional cap skipped for %s: %s", symbol, exc)
            notional = _apply_ipo_buy_notional(
                symbol, notional, equity, data=ipo_data, bar_idx=bar_idx
            )
            if notional < min_n:
                if config.effective_paper_nyse_entry_hygiene():
                    _record_nyse_min_notional_hygiene_skip()
                    logger.info(
                        "[NYSE] Skipping %s — hygiene: below min notional "
                        "($%.2f < $%.2f)",
                        symbol,
                        float(notional),
                        float(min_n),
                    )
                continue
        order = executor.execute_order(
            symbol, "buy", notional=notional, reason=pair_key, sleeve="NYSE"
        )
        if not _count_if_filled(executor, order):
            continue
        _mark_nyse_entered_today(symbol, now=now, data=data)
        _mark_nyse_open_add(symbol)
        if config.effective_insider_signal_boost_enabled():
            try:
                from modules.insider_signal_handler import get_boost_snapshot, record_insider_boost_trade

                snap = get_boost_snapshot()
                if float((snap.get("momentum_boosts") or {}).get(symbol, 0)) > 0:
                    record_insider_boost_trade("momentum")
            except Exception as exc:
                logger.debug("insider boost trade record skipped: %s", exc)
        _record_ipo_buy(executor, symbol, data=ipo_data, bar_idx=bar_idx)
        pair_cooldown[pair_key] = now
        trades += 1
        if portfolio_manager:
            portfolio_manager.add_position(pair_key, 0, 0)
        if log_fn:
            if notional is None:
                notional = getattr(executor, "compute_notional", lambda: "")()
            # Embed garch_multiplier in pair_key so it lands in trade journal notes
            log_key = pair_key
            if garch_multiplier != 1.0:
                log_key = f"{pair_key}|garch_multiplier={garch_multiplier:.3f}"
            log_fn(symbol, "buy", regime, log_key, 0.0, notional)
    return trades


def run_nyse_momentum_and_stat_arb(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=None,
    log_fn=None,
    portfolio_manager=None,
    yield_gated=False,
    pick_log=None,
    volatility=None,
    full_data=None,
    bar_idx=None,
) -> int:
    """NYSE MA50 momentum plus stat-arb pairs (default paper path when PAPER_EQUITY_PAIRS=false)."""
    if max_trades is None:
        try:
            max_trades = config.effective_max_equity_trades()
        except Exception:
            max_trades = MAX_EQUITY_TRADES
    trades = run_equity_strategy(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        max_trades=max_trades,
        log_fn=log_fn,
        portfolio_manager=portfolio_manager,
        yield_gated=yield_gated,
        pick_log=pick_log,
        volatility=volatility,
        full_data=full_data,
        bar_idx=bar_idx,
    )
    if config.effective_stat_arb_enabled() and not config.effective_equity_pairs_enabled():
        from modules.stat_arb_sleeve import run_equity_stat_arb

        trades += run_equity_stat_arb(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            log_fn=log_fn,
            portfolio_manager=portfolio_manager,
            yield_gated=yield_gated,
            volatility=volatility,
        )
    return trades


def spy_mirror_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    yield_gated=False,
    cooldown_seconds=COOLDOWN_SECONDS,
    symbol=None,
    ma_window=None,
) -> dict | None:
    """Intent to mirror SPY sleeve buy on Kraken (QQQ/SPY .EQ), or None."""
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if regime_entries_paused(regime, data) or config.effective_yield_gate(
        yield_gated, regime=regime
    ):
        return None
    bullish, momentum = _spy_market_up_signal(data, symbol, ma_window)
    if not bullish:
        return None
    pair_key = f"{symbol}/MA{ma_window}"
    if _on_cooldown(pair_cooldown, pair_key, now, cooldown_seconds=cooldown_seconds):
        return None
    return {
        "symbol": symbol,
        "side": "buy",
        "pair_key": pair_key,
        "phase": "spy_mirror",
        "momentum": momentum,
    }


def nyse_mirror_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    yield_gated=False,
    executor=None,
) -> dict | None:
    """Top MA50 momentum equity intent for Kraken mirror."""
    if regime_entries_paused(regime, data):
        return None
    equity_cols = _nyse_equity_columns(data)
    ranked = _equity_momentum_ranked(
        data,
        equity_cols,
        yield_gated=yield_gated,
        regime=regime,
        executor=executor,
        now=now,
    )
    if not ranked:
        return None
    for symbol in ranked:
        skip = _nyse_momentum_quality_skip(symbol, data, now=now)
        if skip:
            logger.info("[NYSE] Skipping %s — %s", symbol, skip)
            continue
        hygiene = _nyse_entry_hygiene_skip(executor, symbol, now=now, data=data)
        if hygiene:
            logger.info("[NYSE] Skipping %s — hygiene: %s", symbol, hygiene)
            continue
        pair_key = symbol + "/MA50"
        if _on_cooldown(pair_cooldown, pair_key, now, cooldown_seconds=cooldown_seconds):
            continue
        return {
            "symbol": symbol,
            "side": "buy",
            "pair_key": pair_key,
            "phase": "nyse_mirror",
        }
    return None


def run_international_strategy(*_args, **_kwargs) -> int:
    """ADR sleeve placeholder — disabled on live; full impl in research branch."""
    return 0


def run_bond_strategy(*_args, **_kwargs) -> int:
    """Bond sleeve placeholder — disabled on live; full impl in research branch."""
    return 0


def _short_spy_extension_above_ma200(data, symbol: str) -> tuple[bool, float]:
    """True if price was >8% above 200d MA within recent lookback."""
    if symbol not in data.columns:
        return False, 0.0
    series = data[symbol].dropna()
    ma_w = int(config.SHORT_MA200_WINDOW)
    lb = int(config.SHORT_MA200_EXTENSION_LOOKBACK)
    if len(series) < ma_w + 5:
        return False, 0.0
    peak_ext = 0.0
    tail = series.iloc[-(lb + 1) :]
    for i in range(len(tail)):
        window = series.iloc[: len(series) - len(tail) + i + 1]
        if len(window) < ma_w:
            continue
        ma = window.rolling(ma_w).mean().iloc[-1]
        px = float(tail.iloc[i])
        if ma > 0 and px > ma:
            peak_ext = max(peak_ext, (px / ma) - 1.0)
    threshold = float(config.SHORT_MA200_EXTENSION_PCT)
    return peak_ext >= threshold, peak_ext


def short_momentum_exhaustion_signal(data, symbol: str | None = None) -> tuple[bool, str, float]:
    """RSI > threshold OR recent >8% extension above 200d MA."""
    sym = symbol or config.SPY_BOT_SYMBOL
    if sym not in data.columns:
        return False, "no_data", 0.0
    series = data[sym].dropna()
    rsi = _rsi(series, int(config.SHORT_RSI_PERIOD))
    ext_ok, ext = _short_spy_extension_above_ma200(data, sym)
    if rsi is not None and rsi >= config.SHORT_RSI_EXHAUSTION_MIN:
        return True, f"rsi_{rsi:.0f}", float(rsi)
    if ext_ok:
        return True, f"ma200_ext_{ext:.1%}", ext
    lb = max(5, int(config.SHORT_MOMENTUM_EXHAUSTION_LOOKBACK))
    if len(series) >= lb + 5:
        prior = float(series.iloc[-lb] / series.iloc[-lb - 5] - 1.0)
        recent = float(series.iloc[-1] / series.iloc[-5] - 1.0)
        if prior >= config.SHORT_MOMENTUM_EXHAUSTION_MIN and recent < 0:
            return True, "rollover", prior
    return False, "no_exhaustion", float(rsi or 0.0)


def short_vix_spike_confirmed(
    data,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
) -> tuple[bool, str, float | None]:
    from modules.opportunistic_short_sleeve import _resolve_vix_level, _vix_change_pct

    if not config.SHORT_VIX_SPIKE_CONFIRM:
        return True, "vix_confirm_off", None
    vix = _resolve_vix_level(data, volatility=volatility, vol_score=vol_score)
    chg = _vix_change_pct(data)
    rising = chg is not None and chg > 0
    if config.SHORT_VIX_REQUIRE_RISING:
        if vix is not None and vix >= config.SHORT_VIX_MIN_LEVEL and rising:
            return True, f"vix_{vix:.1f}_rising", vix
        if vix is None or vix < config.SHORT_VIX_MIN_LEVEL:
            return False, "vix_low", vix
        return False, "vix_not_rising", vix
    if vix is not None and vix >= config.SHORT_VIX_MIN_LEVEL:
        return True, f"vix_{vix:.1f}", vix
    return False, "vix_low", vix


def _spy_bear_streak(data, symbol: str, bars: int | None = None) -> tuple[bool, int]:
    """Consecutive down daily bars on symbol (most recent first)."""
    need = bars or int(config.SHORT_RHYME_E_BEAR_STREAK_BARS)
    if symbol not in data.columns:
        return False, 0
    series = data[symbol].dropna()
    if len(series) < need + 1:
        return False, 0
    rets = series.pct_change().dropna()
    streak = 0
    for val in reversed(rets.iloc[-need:].tolist()):
        if float(val) < 0:
            streak += 1
        else:
            break
    return streak >= need, streak


def evaluate_short_entry_triggers(
    data,
    regime: str,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
) -> dict:
    """Protective shorts — RHYME_B: VIX + exhaustion + depth; RHYME_E: VIX + bubble + depth (exhaustion optional)."""
    from modules.bubble_risk import compute_bubble_risk

    reg = str(regime or "")
    spy = config.SPY_BOT_SYMBOL
    ma_window = config.effective_spy_ma_window()
    bubble_min_e = config.effective_short_bubble_min_for_rhyme_e()
    result = {
        "allowed": False,
        "regime": reg,
        "reject": "unknown",
        "trigger_reason": "",
        "bubble_score": 0.0,
        "bubble_score_100": 0.0,
        "buffett_ratio_pct": None,
        "buffett_signal": "",
        "vix_reason": "",
        "exhaustion_reason": "",
        "regime_path": "",
        "bear_streak": 0,
    }
    if not config.effective_opportunistic_short_enabled():
        result["reject"] = "shorts_disabled"
        return result

    bear_b = "RHYME_B" in reg
    bear_e = config.SHORT_RHYME_E_ENABLED and "RHYME_E" in reg
    if not bear_b and not bear_e:
        result["reject"] = "regime_not_bear"
        return result

    result["regime_path"] = "RHYME_B" if bear_b else "RHYME_E"
    bubble_ctx = compute_bubble_risk(data, regime, volatility=volatility, vol_score=vol_score)
    bubble = float(bubble_ctx["score_normalized"])
    result["bubble_score"] = bubble
    result["bubble_score_100"] = float(bubble_ctx["score_100"])
    buff = bubble_ctx.get("buffett") or {}
    result["buffett_ratio_pct"] = buff.get("ratio_pct")
    result["buffett_signal"] = buff.get("signal") or ""

    from modules.opportunistic_short_sleeve import _spy_market_down_signal

    vix_ok, vix_reason, _vix = short_vix_spike_confirmed(
        data, volatility=volatility, vol_score=vol_score
    )
    result["vix_reason"] = vix_reason
    if not vix_ok:
        result["reject"] = vix_reason
        return result

    if bear_e and not config.effective_short_rhyme_e_exhaustion_required():
        if bubble < bubble_min_e:
            result["reject"] = "bubble_low"
            return result
        streak_ok, streak = _spy_bear_streak(data, spy)
        result["bear_streak"] = streak
        vix_waiver = (
            _vix is not None
            and float(_vix) >= float(config.SHORT_RHYME_E_BEAR_STREAK_VIX_WAIVER)
        )
        min_waiver = int(config.SHORT_RHYME_E_WAIVER_MIN_STREAK)
        if not streak_ok:
            if not vix_waiver or streak < min_waiver:
                result["reject"] = "bear_streak"
                return result
        result["vix_waiver_active"] = bool(vix_waiver and not streak_ok)
        bearish, depth = _spy_market_down_signal(data, spy, ma_window)
        if not bearish or depth < config.SHORT_DEEP_BEAR_MIN_DEPTH:
            result["reject"] = "depth_low"
            return result
        result["allowed"] = True
        result["exhaustion_reason"] = "waived"
        result["trigger_reason"] = (
            f"RHYME_E|waived|{vix_reason}|bubble={bubble:.2f}|depth={depth:.3f}|streak={streak}"
        )
        return result

    exhausted, exh_reason, _ = short_momentum_exhaustion_signal(data, spy)
    result["exhaustion_reason"] = exh_reason
    if not exhausted:
        result["reject"] = "no_exhaustion"
        return result

    if bear_b:
        bearish, depth = _spy_market_down_signal(data, spy, ma_window)
        if bearish and depth >= config.SHORT_RHYME_B_MIN_DEPTH:
            result["allowed"] = True
            result["trigger_reason"] = (
                f"RHYME_B|{exh_reason}|{vix_reason}|bubble={bubble:.2f}|depth={depth:.3f}"
            )
            return result
        result["reject"] = "depth_low"
        return result

    if bear_e:
        if bubble < bubble_min_e:
            result["reject"] = "bubble_low"
            return result
        streak_ok, streak = _spy_bear_streak(data, spy)
        result["bear_streak"] = streak
        vix_waiver = (
            _vix is not None
            and float(_vix) >= float(config.SHORT_RHYME_E_BEAR_STREAK_VIX_WAIVER)
        )
        min_waiver = int(config.SHORT_RHYME_E_WAIVER_MIN_STREAK)
        if not streak_ok:
            if not vix_waiver or streak < min_waiver:
                result["reject"] = "bear_streak"
                return result
        result["vix_waiver_active"] = bool(vix_waiver and not streak_ok)
        bearish, depth = _spy_market_down_signal(data, spy, ma_window)
        if bearish and depth >= config.SHORT_DEEP_BEAR_MIN_DEPTH:
            result["allowed"] = True
            result["trigger_reason"] = (
                f"RHYME_E|{exh_reason}|{vix_reason}|bubble={bubble:.2f}|depth={depth:.3f}|streak={streak}"
            )
            return result
        result["reject"] = "depth_low"
        return result

    result["reject"] = "regime_not_bear"
    return result


def run_opportunistic_short_strategy(*args, **kwargs) -> int:
    """Opportunistic directional shorts — Realistic Research paper only."""
    from modules.opportunistic_short_sleeve import run_opportunistic_short_strategy as _run

    return _run(*args, **kwargs)


def summarize_entry_skip_reason(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    yield_gated=False,
    market_open=True,
    volatility=None,
    wisdom_paused=False,
) -> str:
    """Return a token describing why no entries fired this cycle (for skip funnel)."""
    yield_gated = config.effective_yield_gate(yield_gated, regime=regime)
    if wisdom_paused and not config.effective_paper_soft_pause():
        return "wisdom_paused"
    if regime_entries_paused(regime, data):
        if config.effective_paper_soft_pause():
            pass
        else:
            return "regime_paused"
    if not market_open:
        return "equity_session_closed"
    if yield_gated:
        return "yield_gated"
    if config.effective_cofire_budget_enabled() and hasattr(executor, "begin_deployment_cycle"):
        resolve_cycle_deploy(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            cooldown_seconds=cooldown_seconds,
            cooldown_bars=cooldown_bars,
            volatility=volatility,
            yield_gated=yield_gated,
            market_open=market_open,
        )
        rooms = getattr(executor, "_cofire_notionals", None) or {}
        if rooms:
            return "signals_ok"
    equity_cols = _nyse_equity_columns(data)
    ranked = _equity_momentum_ranked(data, equity_cols, yield_gated=yield_gated, regime=regime)
    if ranked:
        sym = ranked[0]
        pair_key = sym + "/MA50"
        if _on_cooldown(
            pair_cooldown,
            pair_key,
            now,
            cooldown_seconds=cooldown_seconds,
            cooldown_bars=cooldown_bars,
        ):
            return "nyse_cooldown"
        if hasattr(executor, "portfolio"):
            equity = executor.portfolio.equity(executor.prices)
            cash = None
        else:
            acct = executor._get_account()
            equity = float(acct.equity)
            cash = float(acct.cash)
        if cash is None and hasattr(executor, "_get_account"):
            try:
                cash = float(executor._get_account().cash)
            except Exception:
                cash = None
        nyse_cap = config.effective_nyse_sleeve_cap_pct(equity=equity, cash=cash)
        room = _sleeve_room(executor, nyse_cap, executor.nyse_sleeve_value)
        room_min = config.effective_no_room_min_notional(equity, cash=cash)
        if room < room_min:
            return "nyse_no_room"
        return "signals_ok"
    if _spy_buy_intent(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        yield_gated=yield_gated,
    ):
        return "signals_ok"
    return "no_ma50_candidates"
