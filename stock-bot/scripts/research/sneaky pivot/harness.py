"""
Phase 0: Shared research harness.
Research-only. Does not wire into live/paper execution. Freeze-safe.

Provides:
  - Dual-resolution bar cache (15m for structure, 1m for fills)
  - Sleeve universe definitions (equity + crypto)
  - RHYME A-E regime labeling per day (wraps market_context classifiers)
  - Time-of-day (TOD) bucket labeling
  - Simple fee/slippage model

This is meant to be reused by every future strategy test, not just
Sneaky Pivot -- keep strategy-specific logic out of this file.
"""

from __future__ import annotations

import datetime as dt
import os
import pickle
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import find_dotenv, load_dotenv

# stock-bot root (…/scripts/research/sneaky pivot → parents[2])
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402

warnings.filterwarnings("ignore", category=FutureWarning)

ET = ZoneInfo("America/New_York")
CACHE_15M = _ROOT / "data" / "intraday_cache_15m"
CACHE_1M = _ROOT / "data" / "intraday_cache_1m"
# Versioned range caches live alongside unversioned legacy files; they do not
# expire on CACHE_MAX_AGE so 90d/180d/365d archives don't collide or vanish.
CHUNK_DAYS = 30
# Free/paper SIP plans often block the most recent ~5 trading days of minute bars.
SIP_LAG_DAYS = 5
CACHE_MAX_AGE = dt.timedelta(days=2)

RhymeLabel = Literal["A", "B", "C", "D", "E"]

_FULL_TO_LETTER = {
    "RHYME_A: Euphoric_Volatility": "A",
    "RHYME_B: Panic_Volatility": "B",
    "RHYME_C: Steady_Bullish_Growth": "C",
    "RHYME_D: Range_Bound_Neutral": "D",
    "RHYME_E: Steady_Bearish_Decline": "E",
}


def _load_env() -> None:
    env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if env_override and os.path.isfile(env_override):
        load_dotenv(env_override, override=True)
    else:
        load_dotenv(_ROOT / ".env")
        load_dotenv(find_dotenv(), override=False)


_load_env()


# --------------------------------------------------------------------------
# Sleeve universes
# --------------------------------------------------------------------------

def _nyse_momentum_tickers() -> list[str]:
    """Static NYSE momentum sleeve candidates (same source as live Profile A)."""
    return list(config.get_nyse_universe_fixed())


def _crypto_vol_tickers() -> list[str]:
    """Match live crypto_vol sleeve symbols (Alpaca slash form)."""
    try:
        from modules.crypto_vol_sleeve import UNIVERSE

        return list(UNIVERSE.values())
    except Exception:
        return ["RENDER/USD", "SOL/USD"]


SLEEVE_UNIVERSES: dict[str, list[str]] = {
    "nyse_momentum": _nyse_momentum_tickers(),
    # Live crypto_vol book (RENDER + SOL). Majors kept separate for research breadth.
    "crypto_vol": _crypto_vol_tickers(),
    "crypto_majors": ["BTC/USD", "ETH/USD", "SOL/USD"],
    # Small fixed test list (video-style names + liquid indices).
    "sanity_check": ["SPY", "QQQ", "AAPL", "MSFT"],
}


# --------------------------------------------------------------------------
# Symbol / asset helpers
# --------------------------------------------------------------------------

def _is_crypto_symbol(symbol: str) -> bool:
    s = symbol.upper().replace("/", "-")
    try:
        return bool(config.is_crypto(config.normalize_symbol(s)))
    except Exception:
        return "-USD" in s or s.endswith("USD")


def _alpaca_crypto_symbol(symbol: str) -> str:
    """BTC-USD / BTC/USD → BTC/USD for CryptoBarsRequest."""
    n = config.normalize_symbol(symbol.replace("/", "-"))
    return n.replace("-", "/")


def _cache_key(symbol: str) -> str:
    return symbol.upper().replace("/", "-")


# --------------------------------------------------------------------------
# Bar normalization + disk cache
# --------------------------------------------------------------------------

def _normalize_ohlcv(df: pd.DataFrame, *, rth_only: bool) -> pd.DataFrame:
    """Return DatetimeIndex (ET) + lowercase ohlcv columns."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        out = out.reset_index()
        rename = {}
        for col in out.columns:
            low = str(col).lower()
            if low in ("timestamp", "time", "datetime", "date", "index"):
                rename[col] = "_ts"
            elif low == "open":
                rename[col] = "open"
            elif low == "high":
                rename[col] = "high"
            elif low == "low":
                rename[col] = "low"
            elif low == "close":
                rename[col] = "close"
            elif low == "volume":
                rename[col] = "volume"
        out = out.rename(columns=rename)
        if "_ts" not in out.columns:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        out["_ts"] = pd.to_datetime(out["_ts"], utc=True).dt.tz_convert(ET)
        out = out.set_index("_ts")
    else:
        idx = pd.to_datetime(out.index, utc=True)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        out.index = idx.tz_convert(ET)
        out = out.rename(columns={c: str(c).lower() for c in out.columns})

    keep = [c for c in ("open", "high", "low", "close", "volume") if c in out.columns]
    out = out[keep].copy()
    for col in keep:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    if "volume" not in out.columns:
        out["volume"] = 0.0

    if rth_only:
        def _rth(ts: pd.Timestamp) -> bool:
            if ts.weekday() >= 5:
                return False
            t = ts.timetz().replace(tzinfo=None)
            return dt.time(9, 30) <= t < dt.time(16, 0)

        out = out.loc[[_rth(ts) for ts in out.index]]

    return out


def _disk_path(
    cache_dir: Path,
    symbol: str,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> Path:
    """Unversioned `{sym}.pkl` or range-versioned `{sym}_{start}_{end}.pkl`."""
    key = _cache_key(symbol)
    if start is not None and end is not None:
        return cache_dir / f"{key}_{start.isoformat()}_{end.isoformat()}.pkl"
    return cache_dir / f"{key}.pkl"


def _load_disk(
    cache_dir: Path,
    symbol: str,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> pd.DataFrame | None:
    path = _disk_path(cache_dir, symbol, start=start, end=end)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    versioned = start is not None and end is not None
    if not versioned:
        age = dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)
        if age > CACHE_MAX_AGE:
            return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return _normalize_ohlcv(df, rth_only=False)
    except Exception:
        return None
    return None


def _save_disk(
    cache_dir: Path,
    symbol: str,
    df: pd.DataFrame,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(_disk_path(cache_dir, symbol, start=start, end=end), "wb") as f:
        pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)


def _paper_keys() -> tuple[str, str]:
    return config.get_paper_alpaca_credentials()


def _fetch_stock_bars(symbol: str, *, timeframe_minutes: int, start: dt.date, end: dt.date) -> pd.DataFrame:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    api_key, secret_key = _paper_keys()
    client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    # SIP lag: don't request the freshest minute bars.
    end_dt = min(
        dt.datetime.combine(end, dt.time(23, 59), tzinfo=dt.timezone.utc),
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=SIP_LAG_DAYS),
    )
    start_dt = dt.datetime.combine(start, dt.time(0, 0), tzinfo=dt.timezone.utc) - dt.timedelta(
        days=10
    )

    frames: list[pd.DataFrame] = []
    chunk_start = start_dt
    while chunk_start < end_dt:
        chunk_end = min(chunk_start + dt.timedelta(days=CHUNK_DAYS), end_dt)
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
                start=chunk_start,
                end=chunk_end,
            )
            bars = client.get_stock_bars(req)
            raw = getattr(bars, "df", None)
            if raw is not None and not raw.empty:
                frames.append(_normalize_ohlcv(raw, rth_only=True))
        except Exception as exc:
            print(
                f"  {symbol} {timeframe_minutes}m: chunk "
                f"{chunk_start.date()}–{chunk_end.date()} — {exc}"
            )
        chunk_start = chunk_end

    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _fetch_crypto_bars(symbol: str, *, timeframe_minutes: int, start: dt.date, end: dt.date) -> pd.DataFrame:
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    api_key, secret_key = _paper_keys()
    client = CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    alpaca_sym = _alpaca_crypto_symbol(symbol)
    end_dt = dt.datetime.combine(end, dt.time(23, 59), tzinfo=dt.timezone.utc)
    start_dt = dt.datetime.combine(start, dt.time(0, 0), tzinfo=dt.timezone.utc) - dt.timedelta(
        days=5
    )

    frames: list[pd.DataFrame] = []
    chunk_start = start_dt
    while chunk_start < end_dt:
        chunk_end = min(chunk_start + dt.timedelta(days=CHUNK_DAYS), end_dt)
        try:
            req = CryptoBarsRequest(
                symbol_or_symbols=alpaca_sym,
                timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
                start=chunk_start.replace(tzinfo=None),
                end=chunk_end.replace(tzinfo=None),
            )
            bars = client.get_crypto_bars(req)
            raw = getattr(bars, "df", None)
            if raw is not None and not raw.empty:
                # Crypto: keep 24/7 (no RTH filter).
                frames.append(_normalize_ohlcv(raw, rth_only=False))
        except Exception as exc:
            print(
                f"  {alpaca_sym} {timeframe_minutes}m: chunk "
                f"{chunk_start.date()}–{chunk_end.date()} — {exc}"
            )
        chunk_start = chunk_end

    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _bars_to_daily(bars_15m: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 15m → daily OHLC (session date in ET)."""
    if bars_15m is None or bars_15m.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    tmp = bars_15m.copy()
    sessions = pd.Series(tmp.index.tz_convert(ET).date, index=tmp.index)
    g = tmp.groupby(sessions, sort=True)
    daily = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    )
    daily.index = pd.to_datetime(daily.index)
    return daily


def load_bars(
    symbol: str,
    timeframe: Literal["1Min", "15Min", "1Day"],
    start: dt.date,
    end: dt.date,
    *,
    refresh: bool = False,
    versioned: bool = True,
) -> pd.DataFrame:
    """
    Load OHLCV for one symbol/timeframe.

    Equities: Alpaca stock bars, RTH-filtered (same pattern as
    scripts/research/sneaky_pivot_backtest.py).
    Crypto: Alpaca crypto bars, 24/7 (same client as improve_crypto_sleeve_v2).
    Daily: aggregated from 15m (keeps level geometry consistent with sessions).

    When versioned=True (default), disk cache keys include start/end so
    90d / 180d / 365d pulls do not overwrite each other.
    """
    crypto = _is_crypto_symbol(symbol)
    if timeframe == "1Day":
        m15 = load_bars(
            symbol, "15Min", start, end, refresh=refresh, versioned=versioned
        )
        return _bars_to_daily(m15)

    cache_dir = CACHE_1M if timeframe == "1Min" else CACHE_15M
    minutes = 1 if timeframe == "1Min" else 15
    v_start, v_end = (start, end) if versioned else (None, None)

    if not refresh:
        cached = _load_disk(cache_dir, symbol, start=v_start, end=v_end)
        if cached is not None and not cached.empty:
            mask = (cached.index.date >= start) & (cached.index.date <= end)
            trimmed = cached.loc[mask]
            if not trimmed.empty:
                return trimmed

    print(f"  fetching {timeframe} {symbol} ({start}->{end})...")
    if crypto:
        df = _fetch_crypto_bars(symbol, timeframe_minutes=minutes, start=start, end=end)
    else:
        df = _fetch_stock_bars(symbol, timeframe_minutes=minutes, start=start, end=end)

    if not df.empty:
        _save_disk(cache_dir, symbol, df, start=v_start, end=v_end)
    return df


# --------------------------------------------------------------------------
# Bar cache
# --------------------------------------------------------------------------

@dataclass
class BarCache:
    """
    Holds 15m and 1m OHLCV bars per symbol, loaded once and reused across
    phases so repeated backtests don't refetch.
    """
    bars_15m: dict[str, pd.DataFrame] = field(default_factory=dict)
    bars_1m: dict[str, pd.DataFrame] = field(default_factory=dict)
    bars_daily: dict[str, pd.DataFrame] = field(default_factory=dict)
    refresh: bool = False
    versioned: bool = True

    def load(self, symbols: list[str], start: dt.date, end: dt.date) -> None:
        for sym in symbols:
            if sym in self.bars_15m and not self.refresh:
                continue
            m15 = load_bars(
                sym,
                "15Min",
                start,
                end,
                refresh=self.refresh,
                versioned=self.versioned,
            )
            m1 = load_bars(
                sym,
                "1Min",
                start,
                end,
                refresh=self.refresh,
                versioned=self.versioned,
            )
            daily = _bars_to_daily(m15)
            self.bars_15m[sym] = m15
            self.bars_1m[sym] = m1
            self.bars_daily[sym] = daily
            n15 = 0 if m15.empty else m15.index.normalize().nunique()
            n1 = len(m1)
            print(f"  cached {sym}: 15m_sessions={n15}, 1m_bars={n1}, daily={len(daily)}")

    def sessions(self, symbol: str) -> list[dt.date]:
        """Return sorted list of trading days available for a symbol."""
        df = self.bars_15m.get(symbol)
        if df is None or df.empty:
            return []
        return sorted({ts.date() for ts in df.index})


# --------------------------------------------------------------------------
# RHYME regime labeling (per day)
# --------------------------------------------------------------------------

# Market-wide calendar (RHYME is not per-symbol in stock-bot).
_rhyme_by_day: dict[dt.date, RhymeLabel] = {}
_rhyme_built_for: tuple[dt.date, dt.date] | None = None


def _letter_from_full(label: str) -> RhymeLabel | None:
    if label in _FULL_TO_LETTER:
        return _FULL_TO_LETTER[label]  # type: ignore[return-value]
    for full, letter in _FULL_TO_LETTER.items():
        if label.startswith(full.split(":")[0]):
            return letter  # type: ignore[return-value]
    if label in ("A", "B", "C", "D", "E"):
        return label  # type: ignore[return-value]
    return None


def _ensure_rhyme_calendar(start: dt.date, end: dt.date) -> None:
    """
    Build day→A–E using the same classifiers as backtester.py:

      sentiment = get_price_sentiment(window)
      vol = get_volatility(window)
      regime = get_market_regime(sentiment, vol, apply_hysteresis=True)

    Window is the multi-asset daily close matrix from load_close_matrix
    (preferred regime input in market_context.regime_dataframe).

    Note: compute_regime_breakdown() only *attributes* PnL to labels — it
    does not classify. Classification lives in market_context.
    """
    global _rhyme_by_day, _rhyme_built_for

    if _rhyme_by_day and _rhyme_built_for is not None:
        built_start, built_end = _rhyme_built_for
        if built_start <= start and built_end >= end:
            return

    from modules.data_loader import load_close_matrix
    from modules.market_context import (
        get_market_regime,
        get_price_sentiment,
        get_volatility,
        reset_regime_hysteresis,
        set_regime_bar_index,
    )

    # Extra history so sentiment/vol windows are valid near `start`.
    lookback = max(120, (end - start).days + 80)
    try:
        matrix = load_close_matrix(interval="1d", days=lookback)
    except Exception as exc:
        print(f"  rhyme calendar: load_close_matrix failed — {exc}")
        matrix = None

    if matrix is None or matrix.empty or len(matrix) < 25:
        print("  rhyme calendar: insufficient daily matrix; labels will be None")
        _rhyme_by_day = {}
        _rhyme_built_for = (start, end)
        return

    idx = pd.to_datetime(matrix.index)
    matrix = matrix.copy()
    matrix.index = idx

    reset_regime_hysteresis()
    out: dict[dt.date, RhymeLabel] = {}
    warmup = 20

    # Suppress REGIME CHANGE banners during research walk.
    import modules.market_context as mc

    _announce = mc.announce_regime_change
    mc.announce_regime_change = lambda regime: regime
    try:
        for i in range(warmup, len(matrix)):
            set_regime_bar_index(i)
            window = matrix.iloc[: i + 1]
            day = pd.Timestamp(matrix.index[i]).date()
            try:
                sentiment = get_price_sentiment(window)
                vol = get_volatility(window, interval="1d")
                full = get_market_regime(sentiment, vol, apply_hysteresis=True)
                letter = _letter_from_full(full)
                if letter is not None:
                    out[day] = letter
            except Exception:
                continue
    finally:
        mc.announce_regime_change = _announce

    reset_regime_hysteresis()
    # Keep any previously labeled days outside this rebuild window.
    merged = dict(_rhyme_by_day)
    merged.update(out)
    _rhyme_by_day = merged
    if _rhyme_built_for is None:
        _rhyme_built_for = (start, end)
    else:
        _rhyme_built_for = (
            min(_rhyme_built_for[0], start),
            max(_rhyme_built_for[1], end),
        )
    print(f"  rhyme calendar: {len(out)} labeled days ({start}->{end})")


def label_rhyme_for_day(day: dt.date, symbol: str | None = None) -> RhymeLabel | None:
    """
    Market-wide RHYME letter for `day`.

    `symbol` is optional and ignored — stock-bot classifies from the
    cross-asset daily matrix, not per ticker. Kept as kw-only-friendly
    second arg for older call sites: label_rhyme_for_day(sym, day) still
    works if callers pass two positionals (see wrapper below).
    """
    # Support legacy call signature label_rhyme_for_day(symbol, day).
    if not isinstance(day, dt.date) and isinstance(symbol, dt.date):
        day = symbol
    if day not in _rhyme_by_day:
        _ensure_rhyme_calendar(day - dt.timedelta(days=5), day + dt.timedelta(days=5))
    return _rhyme_by_day.get(day)


def label_rhyme_for_universe(days: list[dt.date]) -> pd.DataFrame:
    """
    Market-wide day→rhyme map (columns: day, rhyme).

    RHYME is not per-symbol — join trades on session_date == day only.
    """
    if days:
        _ensure_rhyme_calendar(min(days), max(days))
    rows = [{"day": day, "rhyme": label_rhyme_for_day(day)} for day in days]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Time-of-day buckets
# --------------------------------------------------------------------------

# Bucket boundaries in minutes since session open (9:30 ET).
# Tuned to the structure Doug describes: first 15m is the "opening range",
# 15-45m is where the sneaky/entry candle usually lands, everything after
# is "trade management" territory.
TOD_BUCKETS: list[tuple[str, int, int]] = [
    ("open_range_0_15", 0, 15),
    ("sneaky_window_15_45", 15, 45),
    ("mid_morning_45_120", 45, 120),
    ("midday_120_240", 120, 240),
    ("power_hour_240_390", 240, 390),
]


def tod_bucket(minutes_since_open: int) -> str:
    for name, lo, hi in TOD_BUCKETS:
        if lo <= minutes_since_open < hi:
            return name
    return "after_hours_or_unassigned"


# --------------------------------------------------------------------------
# Fee / slippage model
# --------------------------------------------------------------------------

@dataclass
class FeeModel:
    equity_bps: float = 1.0  # per-side, in basis points of notional
    crypto_bps: float = 5.0  # spot-style, wider for vol sleeve
    slippage_bps: float = 2.0  # applied on top, both sides

    def round_trip_cost_bps(self, asset_class: Literal["equity", "crypto"]) -> float:
        per_side = self.equity_bps if asset_class == "equity" else self.crypto_bps
        return 2 * (per_side + self.slippage_bps)


DEFAULT_FEE_MODEL = FeeModel()
