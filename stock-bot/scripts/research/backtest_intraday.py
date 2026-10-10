"""Standalone 5-minute NYSE momentum intraday backtest (research only).

Fetches Alpaca paper market data, caches per-ticker bars, and simulates
NYSE MA50 momentum entries on 5-minute bars during regular hours.

Run:
  python scripts/research/backtest_intraday.py
  python scripts/research/backtest_intraday.py --quality-fixes
  python scripts/research/backtest_intraday.py --refresh --quality-fixes
  python scripts/research/backtest_intraday.py --days 90 --quality-fixes
  python scripts/research/backtest_intraday.py --garch-test --quality-fixes
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

OUT_CSV = Path(__file__).resolve().parent / "intraday_backtest_results.csv"
CACHE_DIR = ROOT / "data" / "intraday_cache"
ET = ZoneInfo("America/New_York")

MAX_HISTORY_DAYS = 730
CACHE_MAX_AGE = timedelta(days=1)
CHUNK_DAYS = 30

INITIAL_EQUITY = 100_000.0
POSITION_PCT = 0.05
MAX_POSITIONS = 3
MA_WINDOW = 50
RSI_PERIOD = 14
RSI_MAX = 70
STOP_LOSS_PCT = 0.025
TAKE_PROFIT_PCT = 0.04
MAX_HOLD_BARS = 390
GAP_THRESHOLD = 0.02
COOLDOWN_START = time(9, 30)
COOLDOWN_END = time(10, 0)
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

HOUR_BUCKETS = [
    time(9, 30),
    time(10, 0),
    time(11, 0),
    time(12, 0),
    time(13, 0),
    time(14, 0),
    time(15, 0),
]


@dataclass
class Position:
    ticker: str
    entry_ts: pd.Timestamp
    entry_price: float
    shares: float
    cash_in: float
    entry_bar_idx: int
    was_gap: bool = False
    was_cooldown: bool = False


@dataclass
class TradeRow:
    date: str
    ticker: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str
    hold_minutes: float
    was_gap: bool
    was_cooldown: bool


@dataclass
class SimResult:
    trades: list[TradeRow] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)
    cooldown_blocked: int = 0
    gap_blocked: int = 0
    one_per_day_blocked: int = 0
    position_sizes: list[float] = field(default_factory=list)  # fraction of equity at entry


def _load_env() -> None:
    env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if env_override and os.path.isfile(env_override):
        load_dotenv(env_override, override=True)
    else:
        load_dotenv(find_dotenv())


def _paper_keys() -> tuple[str, str]:
    return config.get_paper_alpaca_credentials()


def _normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if isinstance(out.index, pd.MultiIndex):
        out = out.reset_index()
    else:
        out = out.reset_index()
    rename = {}
    for col in out.columns:
        low = str(col).lower()
        if low in ("timestamp", "time", "datetime"):
            rename[col] = "timestamp"
        elif low == "open":
            rename[col] = "Open"
        elif low == "high":
            rename[col] = "High"
        elif low == "low":
            rename[col] = "Low"
        elif low == "close":
            rename[col] = "Close"
        elif low == "volume":
            rename[col] = "Volume"
        elif low == "symbol":
            rename[col] = "symbol"
    out = out.rename(columns=rename)
    if "timestamp" not in out.columns:
        ts_col = next(
            (c for c in out.columns if str(c).lower() in ("date", "index")),
            out.columns[0],
        )
        out = out.rename(columns={ts_col: "timestamp"})
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).dt.tz_convert(ET)
    keep = [c for c in ("timestamp", "Open", "High", "Low", "Close", "Volume") if c in out.columns]
    out = out[keep].copy()
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Close"]).sort_values("timestamp")
    out = out.drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    return out


def _is_rth(ts: pd.Timestamp) -> bool:
    if ts.weekday() >= 5:
        return False
    t = ts.time()
    return RTH_OPEN <= t < RTH_CLOSE


def _filter_rth(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df["timestamp"].map(_is_rth)
    return df.loc[mask].reset_index(drop=True)


def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}.pkl"


def _cache_fresh(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < CACHE_MAX_AGE


def _load_cache(ticker: str) -> pd.DataFrame | None:
    path = _cache_path(ticker)
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        if isinstance(df, pd.DataFrame) and not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
            return df
    except Exception:
        return None
    return None


def _save_cache(ticker: str, df: pd.DataFrame) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_path(ticker), "wb") as f:
        pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)


def fetch_alpaca_5m(ticker: str, *, days: int) -> pd.DataFrame:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    api_key, secret_key = _paper_keys()
    client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    # Free/paper SIP plans block the most recent ~5 trading days of minute bars.
    end = datetime.now(timezone.utc) - timedelta(days=5)
    start = end - timedelta(days=days + 5)

    frames: list[pd.DataFrame] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end)
        try:
            request = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=chunk_start,
                end=chunk_end,
            )
            bars = client.get_stock_bars(request)
            df = getattr(bars, "df", None)
            if df is not None and not df.empty:
                frames.append(_normalize_bars(df))
        except Exception as exc:
            print(f"  {ticker}: chunk error {chunk_start.date()}–{chunk_end.date()} — {exc}")
        chunk_start = chunk_end

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    return _filter_rth(out.reset_index(drop=True))


def load_ticker_bars(
    ticker: str,
    *,
    days: int,
    refresh: bool,
) -> pd.DataFrame | None:
    path = _cache_path(ticker)
    if not refresh and _cache_fresh(path):
        cached = _load_cache(ticker)
        if cached is not None and not cached.empty:
            return cached
    if not refresh:
        cached = _load_cache(ticker)
        if cached is not None and not cached.empty:
            return cached

    df = fetch_alpaca_5m(ticker, days=days)
    if df.empty:
        return None
    _save_cache(ticker, df)
    return df


def _compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def enrich_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized indicators and session metadata on 5-minute RTH bars."""
    out = df.copy()
    out["ma50"] = out["Close"].rolling(MA_WINDOW, min_periods=MA_WINDOW).mean()
    out["rsi"] = _compute_rsi(out["Close"], RSI_PERIOD)

    cal_date = out["timestamp"].dt.date
    out["cal_date"] = cal_date

    daily_close = out.groupby("cal_date", sort=False)["Close"].last()
    daily_close_prev = daily_close.shift(1)
    out["prior_close"] = cal_date.map(daily_close_prev.to_dict())

    session_open = out.groupby("cal_date", sort=False)["Open"].first()
    out["session_open"] = cal_date.map(session_open.to_dict())
    out["gap_pct"] = (out["session_open"] / out["prior_close"]) - 1.0

    bar_time = out["timestamp"].dt.time
    out["in_cooldown"] = (bar_time >= COOLDOWN_START) & (bar_time <= COOLDOWN_END)
    out["gap_up"] = out["gap_pct"] > GAP_THRESHOLD

    out["above_ma50"] = out["Close"] > out["ma50"]
    out["rsi_ok"] = out["rsi"] < RSI_MAX
    out["mom_ok"] = out["Close"] > out["prior_close"]
    out["entry_signal"] = (
        out["above_ma50"]
        & out["rsi_ok"]
        & out["mom_ok"]
        & out["ma50"].notna()
        & out["prior_close"].notna()
    )
    return out


def _slice_window(df: pd.DataFrame, *, days: int) -> pd.DataFrame:
    if df.empty or days <= 0:
        return df
    end = df["timestamp"].max()
    start = end - pd.Timedelta(days=days)
    return df.loc[df["timestamp"] >= start].reset_index(drop=True)


def _prepare_ticker_frames(
    universe: list[str],
    *,
    days: int,
    refresh: bool,
) -> dict[str, pd.DataFrame]:
    fetch_days = min(MAX_HISTORY_DAYS, max(days + 30, 120))
    frames: dict[str, pd.DataFrame] = {}
    for i, ticker in enumerate(universe, 1):
        sym = config.normalize_symbol(ticker)
        if i % 10 == 0 or i == 1 or i == len(universe):
            print(f"[{i}/{len(universe)}] loading {sym}...")
        raw = load_ticker_bars(sym, days=fetch_days, refresh=refresh)
        if raw is None or raw.empty:
            print(f"  skip {sym}: no bars")
            continue
        enriched = enrich_bars(raw)
        enriched = _slice_window(enriched, days=days)
        if len(enriched) < MA_WINDOW + RSI_PERIOD:
            print(f"  skip {sym}: insufficient bars after window slice")
            continue
        enriched = enriched.reset_index(drop=True)
        enriched["bar_idx"] = np.arange(len(enriched), dtype=np.int64)
        frames[sym] = enriched
    return frames


def _build_signal_index(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Sparse entry candidates across tickers (vectorized concat)."""
    parts: list[pd.DataFrame] = []
    for ticker, df in frames.items():
        hits = df.loc[df["entry_signal"], [
            "timestamp", "bar_idx", "Close", "in_cooldown", "gap_up", "cal_date"
        ]].copy()
        if hits.empty:
            continue
        hits["ticker"] = ticker
        parts.append(hits)
    if not parts:
        return pd.DataFrame(
            columns=["timestamp", "bar_idx", "Close", "in_cooldown", "gap_up", "cal_date", "ticker"]
        )
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values(["timestamp", "ticker"]).reset_index(drop=True)


def simulate(
    frames: dict[str, pd.DataFrame],
    *,
    quality_fixes: bool,
    initial_equity: float = INITIAL_EQUITY,
    position_pct: float = POSITION_PCT,
    size_mult_fn=None,
) -> SimResult:
    """Event-driven portfolio sim on precomputed entry signals.

    size_mult_fn: optional callable(ts) -> float multiplier applied to position_pct.
    """
    signals = _build_signal_index(frames)
    if signals.empty and not frames:
        return SimResult()

    bar_at_ts: dict[str, dict[pd.Timestamp, tuple[int, float]]] = {}
    for ticker, df in frames.items():
        bar_at_ts[ticker] = {
            ts: (int(idx), float(px))
            for ts, idx, px in zip(
                df["timestamp"].to_numpy(),
                df["bar_idx"].to_numpy(),
                df["Close"].to_numpy(),
            )
        }

    all_ts = sorted({ts for df in frames.values() for ts in df["timestamp"].unique()})
    sig_by_ts: dict[pd.Timestamp, pd.DataFrame] = {
        ts: grp for ts, grp in signals.groupby("timestamp", sort=True)
    }

    positions: dict[str, Position] = {}
    trades: list[TradeRow] = []
    position_sizes: list[float] = []
    cash = float(initial_equity)
    equity_points: list[tuple[pd.Timestamp, float]] = []
    entered_today: set[tuple[object, str]] = set()

    cooldown_blocked = 0
    gap_blocked = 0
    one_per_day_blocked = 0

    for ts in all_ts:
        mtm = cash
        for pos in positions.values():
            hit = bar_at_ts.get(pos.ticker, {}).get(ts)
            if hit is not None:
                _, price = hit
                mtm += pos.shares * price
        equity_points.append((ts, mtm))

        closed: list[str] = []
        for ticker, pos in positions.items():
            hit = bar_at_ts.get(ticker, {}).get(ts)
            if hit is None:
                continue
            bar_idx, price = hit
            pnl_pct = (price - pos.entry_price) / pos.entry_price
            hold_bars = bar_idx - pos.entry_bar_idx
            hold_min = hold_bars * 5.0

            reason = ""
            if pnl_pct <= -STOP_LOSS_PCT:
                reason = "stop_loss"
            elif pnl_pct >= TAKE_PROFIT_PCT:
                reason = "take_profit"
            elif hold_bars >= MAX_HOLD_BARS:
                reason = "max_hold"

            if reason:
                cash += pos.shares * price
                trades.append(
                    TradeRow(
                        date=str(ts.date()),
                        ticker=ticker,
                        entry_time=pos.entry_ts,
                        exit_time=ts,
                        entry_price=pos.entry_price,
                        exit_price=price,
                        pnl_pct=pnl_pct,
                        exit_reason=reason,
                        hold_minutes=hold_min,
                        was_gap=pos.was_gap,
                        was_cooldown=pos.was_cooldown,
                    )
                )
                closed.append(ticker)

        for ticker in closed:
            positions.pop(ticker, None)

        if len(positions) >= MAX_POSITIONS:
            continue

        candidates = sig_by_ts.get(ts)
        if candidates is None or candidates.empty:
            continue

        for _, row in candidates.iterrows():
            if len(positions) >= MAX_POSITIONS:
                break
            ticker = str(row["ticker"])
            if ticker in positions:
                continue

            cal_day = row["cal_date"]
            in_cooldown = bool(row["in_cooldown"])
            gap_up = bool(row["gap_up"])

            if quality_fixes:
                if in_cooldown:
                    cooldown_blocked += 1
                    continue
                if gap_up:
                    gap_blocked += 1
                    continue
                key = (cal_day, ticker)
                if key in entered_today:
                    one_per_day_blocked += 1
                    continue

            price = float(row["Close"])
            mult = 1.0
            if size_mult_fn is not None:
                try:
                    mult = float(size_mult_fn(ts) or 1.0)
                except Exception:
                    mult = 1.0
            size_pct = float(position_pct) * max(0.0, mult)
            notional = mtm * size_pct
            if notional <= 0 or price <= 0 or notional > cash:
                continue
            shares = notional / price
            cash -= notional
            position_sizes.append(size_pct)

            positions[ticker] = Position(
                ticker=ticker,
                entry_ts=ts,
                entry_price=price,
                shares=shares,
                cash_in=notional,
                entry_bar_idx=int(row["bar_idx"]),
                was_gap=gap_up,
                was_cooldown=in_cooldown,
            )
            if quality_fixes:
                entered_today.add((cal_day, ticker))

    if all_ts:
        last_ts = all_ts[-1]
        for ticker, pos in list(positions.items()):
            df = frames[ticker]
            price = float(df["Close"].iloc[-1])
            pnl_pct = (price - pos.entry_price) / pos.entry_price
            hold_bars = int(df["bar_idx"].iloc[-1]) - pos.entry_bar_idx
            cash += pos.shares * price
            trades.append(
                TradeRow(
                    date=str(last_ts.date()),
                    ticker=ticker,
                    entry_time=pos.entry_ts,
                    exit_time=last_ts,
                    entry_price=pos.entry_price,
                    exit_price=price,
                    pnl_pct=pnl_pct,
                    exit_reason="eod_liquidation",
                    hold_minutes=hold_bars * 5.0,
                    was_gap=pos.was_gap,
                    was_cooldown=pos.was_cooldown,
                )
            )
        positions.clear()
        equity_points.append((last_ts, cash))

    curve = (
        pd.Series(
            [e for _, e in equity_points],
            index=pd.DatetimeIndex([t for t, _ in equity_points]),
            dtype=float,
        )
        if equity_points
        else pd.Series(dtype=float)
    )

    return SimResult(
        trades=trades,
        equity_curve=curve,
        cooldown_blocked=cooldown_blocked,
        gap_blocked=gap_blocked,
        one_per_day_blocked=one_per_day_blocked,
        position_sizes=position_sizes,
    )


def _max_drawdown_pct(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    roll_max = curve.cummax()
    dd = (curve - roll_max) / roll_max.replace(0, np.nan)
    return float(dd.min() * 100.0)


def _sharpe_from_curve(curve: pd.Series) -> float:
    if curve.empty or len(curve) < 3:
        return 0.0
    daily = curve.resample("1D").last().dropna().pct_change().dropna()
    if daily.empty or daily.std() == 0:
        return 0.0
    return float(daily.mean() / daily.std() * np.sqrt(252))


def _metrics(result: SimResult, *, initial_equity: float = INITIAL_EQUITY) -> dict[str, float]:
    trades = result.trades
    curve = result.equity_curve
    final_eq = float(curve.iloc[-1]) if not curve.empty else initial_equity
    total_ret = (final_eq / initial_equity - 1.0) * 100.0
    pnls = [t.pnl_pct for t in trades if t.exit_reason != "eod_liquidation"]
    if not pnls:
        pnls = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    win_rate = (len(wins) / len(pnls) * 100.0) if pnls else 0.0
    holds = [t.hold_minutes for t in trades]
    avg_hold = float(np.mean(holds)) if holds else 0.0
    avg_pnl = float(np.mean(pnls) * 100.0) if pnls else 0.0
    sizes = list(getattr(result, "position_sizes", None) or [])
    avg_pos = float(np.mean(sizes) * 100.0) if sizes else float(POSITION_PCT * 100.0)
    return {
        "total_return_pct": total_ret,
        "sharpe": _sharpe_from_curve(curve),
        "max_dd_pct": _max_drawdown_pct(curve),
        "total_trades": float(len(trades)),
        "win_rate_pct": win_rate,
        "avg_hold_min": avg_hold,
        "avg_pnl_pct": avg_pnl,
        "avg_position_pct": avg_pos,
        "cooldown_blocked": float(result.cooldown_blocked),
        "gap_blocked": float(result.gap_blocked),
    }


def _hour_bucket(ts: pd.Timestamp) -> time:
    t = ts.time()
    bucket = time(9, 30)
    for h in HOUR_BUCKETS:
        if t >= h:
            bucket = h
    return bucket


def _ticker_pnl(trades: list[TradeRow]) -> pd.Series:
    if not trades:
        return pd.Series(dtype=float)
    df = pd.DataFrame([{"ticker": t.ticker, "pnl_pct": t.pnl_pct} for t in trades])
    return df.groupby("ticker")["pnl_pct"].sum().sort_values(ascending=False)


def _hour_stats(trades: list[TradeRow]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not trades:
        empty = pd.DataFrame(columns=["hour", "win_rate_pct", "avg_pnl_pct", "trades"])
        return empty, empty
    rows = []
    for t in trades:
        rows.append(
            {
                "hour": _hour_bucket(t.entry_time).strftime("%H:%M"),
                "pnl_pct": t.pnl_pct,
                "win": t.pnl_pct > 0,
            }
        )
    df = pd.DataFrame(rows)
    win = df.groupby("hour").agg(
        win_rate_pct=("win", lambda s: s.mean() * 100.0),
        trades=("win", "count"),
    )
    avg = df.groupby("hour").agg(avg_pnl_pct=("pnl_pct", lambda s: s.mean() * 100.0))
    win = win.reset_index()
    avg = avg.reset_index()
    return win, avg


def _print_comparison(base: dict[str, float], fixed: dict[str, float]) -> None:
    rows = [
        ("Total return %", "total_return_pct", ".2f"),
        ("Sharpe", "sharpe", ".2f"),
        ("Max DD %", "max_dd_pct", ".2f"),
        ("Total trades", "total_trades", ".0f"),
        ("Win rate %", "win_rate_pct", ".1f"),
        ("Avg hold (minutes)", "avg_hold_min", ".1f"),
        ("Avg PnL per trade", "avg_pnl_pct", ".3f"),
        ("Trades in open cooldown (blocked)", "cooldown_blocked", ".0f"),
        ("Gap opens blocked", "gap_blocked", ".0f"),
    ]
    print("\n=== Quality fixes comparison ===")
    print(f"{'Metric':<36} {'Without fixes':>14} {'With fixes':>14}")
    print("-" * 66)
    for label, key, fmt in rows:
        a = base.get(key, 0.0)
        b = fixed.get(key, 0.0)
        if fmt == ".0f":
            print(f"{label:<36} {a:14.0f} {b:14.0f}")
        elif fmt == ".1f":
            print(f"{label:<36} {a:14.1f} {b:14.1f}")
        elif fmt == ".3f":
            print(f"{label:<36} {a:14.3f} {b:14.3f}")
        else:
            print(f"{label:<36} {a:14.2f} {b:14.2f}")
    print()


def _print_single(label: str, m: dict[str, float]) -> None:
    print(f"\n=== {label} ===")
    print(f"Total return:     {m['total_return_pct']:.2f}%")
    print(f"Sharpe:           {m['sharpe']:.2f}")
    print(f"Max drawdown:     {m['max_dd_pct']:.2f}%")
    print(f"Total trades:     {int(m['total_trades'])}")
    print(f"Win rate:         {m['win_rate_pct']:.1f}%")
    print(f"Avg hold (min):   {m['avg_hold_min']:.1f}")
    print(f"Avg PnL/trade:    {m['avg_pnl_pct']:.3f}%")
    if "avg_position_pct" in m:
        print(f"Avg position %:   {m['avg_position_pct']:.2f}%")


def _load_spy_daily_closes(min_days: int = 400) -> pd.Series | None:
    """SPY daily closes for GARCH sizing test (yfinance → DB fallback)."""
    try:
        import yfinance as yf

        hist = yf.download("SPY", period="2y", progress=False, auto_adjust=True)
        if hist is not None and not hist.empty:
            close = hist["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            close = pd.to_numeric(close, errors="coerce").dropna()
            if len(close) >= 60:
                return close
    except Exception as exc:
        print(f"  yfinance SPY load failed: {exc}")

    db = ROOT / "market_data.db"
    if db.is_file():
        try:
            import sqlite3

            conn = sqlite3.connect(db)
            try:
                spy = pd.read_sql(
                    'SELECT Date, Close FROM "SPY_daily" ORDER BY Date', conn
                )
            finally:
                conn.close()
            if not spy.empty:
                spy["Date"] = pd.to_datetime(spy["Date"], utc=True).dt.tz_localize(None)
                s = pd.Series(
                    pd.to_numeric(spy["Close"], errors="coerce").to_numpy(),
                    index=spy["Date"],
                ).dropna()
                if len(s) >= 60:
                    return s
        except Exception as exc:
            print(f"  market_data.db SPY load failed: {exc}")
    return None


def _make_garch_size_mult_fn(spy_daily: pd.Series):
    """Cache GARCH multipliers by calendar date (paper sizing gate forced on)."""
    cache: dict[object, float] = {}
    # Force-enable paper GARCH path for the research comparison only
    os.environ["PAPER_GARCH_SIZING"] = "true"
    os.environ.setdefault("PAPER_TRADING", "true")
    from modules.garch_sizer import get_multiplier

    def _fn(ts) -> float:
        t = pd.Timestamp(ts)
        if getattr(t, "tzinfo", None) is not None:
            t = t.tz_convert("UTC").tz_localize(None)
        day = t.normalize().date()
        if day in cache:
            return cache[day]
        hist = spy_daily.loc[: pd.Timestamp(day)]
        if len(hist) < 60:
            cache[day] = 1.0
            return 1.0
        # Refit at most weekly for speed
        week_key = (day.isocalendar().year, day.isocalendar().week)
        if week_key in cache:
            cache[day] = cache[week_key]
            return cache[day]
        mult = float(get_multiplier(hist))
        cache[week_key] = mult
        cache[day] = mult
        return mult

    return _fn


def _run_garch_test(frames: dict[str, pd.DataFrame], *, quality_fixes: bool) -> None:
    print("\n=== GARCH sizing validation (90-day window comparison) ===")
    spy = _load_spy_daily_closes()
    if spy is None or spy.empty:
        raise SystemExit("Cannot load SPY daily closes for --garch-test")

    print(f"SPY daily bars: {len(spy)} | target_vol gate via modules.garch_sizer")

    print("\nRunning WITHOUT GARCH sizing...")
    res_base = simulate(frames, quality_fixes=quality_fixes, size_mult_fn=None)
    m_base = _metrics(res_base)

    print("Running WITH GARCH sizing...")
    mult_fn = _make_garch_size_mult_fn(spy)
    res_garch = simulate(frames, quality_fixes=quality_fixes, size_mult_fn=mult_fn)
    m_garch = _metrics(res_garch)

    print("\n=== GARCH vs baseline ===")
    header = f"{'Metric':<22} {'Baseline':>12} {'GARCH':>12} {'Delta':>12}"
    print(header)
    print("-" * len(header))
    rows = [
        ("Total return %", "total_return_pct", ".2f"),
        ("Sharpe", "sharpe", ".2f"),
        ("Max DD %", "max_dd_pct", ".2f"),
        ("Avg position %", "avg_position_pct", ".2f"),
        ("Total trades", "total_trades", ".0f"),
        ("Win rate %", "win_rate_pct", ".1f"),
    ]
    for label, key, fmt in rows:
        a = float(m_base[key])
        b = float(m_garch[key])
        d = b - a
        if fmt == ".0f":
            print(f"{label:<22} {a:12.0f} {b:12.0f} {d:12.0f}")
        elif fmt == ".1f":
            print(f"{label:<22} {a:12.1f} {b:12.1f} {d:12.1f}")
        else:
            print(f"{label:<22} {a:12.2f} {b:12.2f} {d:12.2f}")

    sharpe_lift = float(m_garch["sharpe"]) - float(m_base["sharpe"])
    # max_dd_pct is negative; "reduces max DD" means less severe → higher (closer to 0)
    dd_improved = float(m_garch["max_dd_pct"]) > float(m_base["max_dd_pct"])
    print()
    if sharpe_lift > 0.1 and dd_improved:
        print(
            "RECOMMENDATION: ENABLE PAPER_GARCH_SIZING=true "
            f"(Sharpe +{sharpe_lift:.2f}, max DD improved)."
        )
    else:
        print(
            "RECOMMENDATION: keep PAPER_GARCH_SIZING=false "
            f"(Sharpe delta={sharpe_lift:+.2f}, DD improved={dd_improved})."
        )
    _save_trades(res_garch.trades, OUT_CSV)


def _print_analytics(trades: list[TradeRow]) -> None:
    pnl_by_ticker = _ticker_pnl(trades)
    if not pnl_by_ticker.empty:
        print("\nBest tickers (sum PnL %):")
        for t, v in pnl_by_ticker.head(5).items():
            print(f"  {t}: {v*100:.2f}%")
        print("Worst tickers (sum PnL %):")
        for t, v in pnl_by_ticker.tail(5).items():
            print(f"  {t}: {v*100:.2f}%")

    win_by_hour, avg_by_hour = _hour_stats(trades)
    if not win_by_hour.empty:
        print("\nWin rate by entry hour (ET):")
        for _, row in win_by_hour.iterrows():
            print(f"  {row['hour']}: {row['win_rate_pct']:.1f}% ({int(row['trades'])} trades)")
        print("\nAvg PnL % by entry hour (ET):")
        for _, row in avg_by_hour.iterrows():
            print(f"  {row['hour']}: {row['avg_pnl_pct']:.3f}%")


def _save_trades(trades: list[TradeRow], path: Path) -> None:
    rows = [
        {
            "date": t.date,
            "ticker": t.ticker,
            "entry_time": t.entry_time.isoformat(),
            "exit_time": t.exit_time.isoformat(),
            "entry_price": round(t.entry_price, 4),
            "exit_price": round(t.exit_price, 4),
            "pnl_pct": round(t.pnl_pct * 100.0, 4),
            "exit_reason": t.exit_reason,
            "hold_minutes": round(t.hold_minutes, 1),
            "was_gap": t.was_gap,
            "was_cooldown": t.was_cooldown,
        }
        for t in trades
    ]
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"\nSaved trade log: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="5-minute NYSE momentum intraday research backtest")
    parser.add_argument(
        "--days",
        type=int,
        default=MAX_HISTORY_DAYS,
        help=f"History/simulation window in calendar days (default {MAX_HISTORY_DAYS})",
    )
    parser.add_argument(
        "--quality-fixes",
        action="store_true",
        help="Run with and without quality fixes and print comparison table",
    )
    parser.add_argument(
        "--garch-test",
        action="store_true",
        help="Compare 90-day backtest with vs without GARCH vol-target sizing",
    )
    parser.add_argument("--refresh", action="store_true", help="Force re-download of cached bars")
    args = parser.parse_args()

    _load_env()
    if args.garch_test:
        days = 90
    else:
        days = max(30, min(args.days, MAX_HISTORY_DAYS))
    universe = [config.normalize_symbol(t) for t in config.get_nyse_universe()]
    universe = sorted({t for t in universe if t})
    dyn = bool(getattr(config, "USE_DYNAMIC_UNIVERSE", False))
    print(f"NYSE intraday backtest | tickers={len(universe)} | days={days}")
    if dyn:
        print("Universe: config.get_nyse_universe() (fixed + screener)")
    else:
        print("Universe: config.get_nyse_universe() (fixed only — set USE_DYNAMIC_UNIVERSE=true for 103)")
    print(f"Cache: {CACHE_DIR}")

    frames = _prepare_ticker_frames(universe, days=days, refresh=args.refresh)
    if not frames:
        raise SystemExit("No ticker data loaded — check Alpaca credentials and universe.")

    print(f"Loaded {len(frames)} tickers with 5-minute RTH bars.")

    if args.garch_test:
        _run_garch_test(frames, quality_fixes=bool(args.quality_fixes))
        return

    if args.quality_fixes:
        print("\nRunning WITHOUT quality fixes...")
        res_base = simulate(frames, quality_fixes=False)
        m_base = _metrics(res_base)

        print("Running WITH quality fixes...")
        res_fixed = simulate(frames, quality_fixes=True)
        m_fixed = _metrics(res_fixed)

        _print_comparison(m_base, m_fixed)
        _print_analytics(res_fixed.trades)
        _save_trades(res_fixed.trades, OUT_CSV)
    else:
        res = simulate(frames, quality_fixes=False)
        m = _metrics(res)
        _print_single("Intraday NYSE momentum (no quality fixes)", m)
        _print_analytics(res.trades)
        _save_trades(res.trades, OUT_CSV)


if __name__ == "__main__":
    main()
