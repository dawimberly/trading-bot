"""Intraday mean-reversion backtest on crypto 1-hour bars (Alpaca paper data).

Universe v3: WIF, BONK, RENDER, ARB, SOL, AVAX (USD pairs via Alpaca).
Universe v4: same minus ARB/USD, plus correlation filters (SPY gate, hours, RSI band, cooldown).

Run:  python backtest_crypto_vol.py
"""

from __future__ import annotations

import os
import sqlite3
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import find_dotenv, load_dotenv

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parent
OUT_CSV_V4 = ROOT / "crypto_vol_backtest_v4_results.csv"
DB_PATH = ROOT / "market_data.db"

# Display label → Alpaca crypto symbol
UNIVERSE_V3 = {
    "WIF/USD": "WIF/USD",
    "BONK/USD": "BONK/USD",
    "RENDER/USD": "RENDER/USD",
    "ARB/USD": "ARB/USD",
    "SOL/USD": "SOL/USD",
    "AVAX/USD": "AVAX/USD",
}
UNIVERSE_V4 = {k: v for k, v in UNIVERSE_V3.items() if k != "ARB/USD"}

# v4 filter constants
SPY_GATE_PCT = -1.0
RSI_MIN_V4 = 32
RSI_MAX_V4 = 42
LOSS_COOLDOWN_HOURS = 48
HOUR_WINDOWS_UTC = ((13, 16), (18, 22))  # inclusive start/end hour

LOOKBACK_DAYS = 180
MIN_HISTORY_DAYS = 30

# Virtual equity for concurrent-slot / position sizing only (PnL reported per trade %).
DEFAULT_VIRTUAL_EQUITY = 100_000.0
VIRTUAL_EQUITY = DEFAULT_VIRTUAL_EQUITY
POSITION_PCT = 0.05
MAX_POSITIONS = 3

DROP_PCT = -0.03
DROP_PCT_RELAXED = -0.025
RSI_MAX = 42
RSI_MAX_RELAXED = 45
MIN_TRADES_TARGET = 30
RSI_PERIOD = 14
SMA_PERIOD = 10
FOUR_H_BARS = 4

TAKE_PROFIT_PCT = 0.035
STOP_LOSS_PCT = 0.025
TIMEOUT_BARS = 48  # 48 hours on 1h bars

# Alpaca crypto taker fee per leg (0.25%)
FEE_PCT = 0.0025

# Hourly equity returns, annualized for 24/7 crypto (365 * 24 bars/year)
SHARPE_SCALE = np.sqrt(365 * 24)


@dataclass
class Position:
    coin: str
    entry_date: pd.Timestamp
    entry_idx: int
    entry_price: float
    shares: float
    cash_in: float


@dataclass
class Trade:
    date: pd.Timestamp
    coin: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str
    hold_hours: float


@dataclass
class SignalStats:
    signals: int = 0
    taken: int = 0
    skipped: int = 0


@dataclass
class FilterSkipCounts:
    spy_gate: int = 0
    hour_filter: int = 0
    rsi_floor: int = 0
    loss_cooldown: int = 0
    max_positions: int = 0
    insufficient_cash: int = 0


@dataclass
class BacktestConfig:
    label: str
    universe: dict[str, str]
    drop_pct: float = DROP_PCT
    rsi_min: float | None = None
    rsi_max: float = RSI_MAX
    spy_gate: bool = False
    hour_filter: bool = False
    loss_cooldown: bool = False
    allow_relaxed_retry: bool = False


@dataclass
class ConditionCounts:
    drop: int = 0
    rsi: int = 0
    below_sma: int = 0
    combined: int = 0


@dataclass
class EntryParams:
    drop_pct: float = DROP_PCT
    rsi_min: float | None = None
    rsi_max: float = RSI_MAX


_entry_params = EntryParams()


def _load_env() -> None:
    env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if env_override and os.path.isfile(env_override):
        load_dotenv(env_override, override=True)
    else:
        load_dotenv(find_dotenv())


def _paper_alpaca_keys() -> tuple[str, str]:
    key = os.getenv("PAPER_APCA_API_KEY_ID", "").strip()
    secret = os.getenv("PAPER_APCA_API_SECRET_KEY", "").strip()
    if not key or not secret:
        raise ValueError(
            "Missing PAPER_APCA_API_KEY_ID / PAPER_APCA_API_SECRET_KEY in environment."
        )
    return key, secret


def fetch_paper_equity() -> float | None:
    """Fetch live Alpaca paper account equity; return None on failure."""
    try:
        from alpaca.trading.client import TradingClient

        api_key, secret_key = _paper_alpaca_keys()
        client = TradingClient(api_key, secret_key, paper=True)
        account = client.get_account()
        equity = float(account.equity)
        if equity > 0:
            return equity
    except Exception as exc:
        print(f"  Could not fetch live paper equity: {exc}")
    return None


def resolve_virtual_equity() -> float:
    """Use live paper equity when available, else DEFAULT_VIRTUAL_EQUITY."""
    global VIRTUAL_EQUITY
    live = fetch_paper_equity()
    if live is not None:
        VIRTUAL_EQUITY = live
        print(f"Using live Alpaca paper equity: ${VIRTUAL_EQUITY:,.2f}")
    else:
        VIRTUAL_EQUITY = DEFAULT_VIRTUAL_EQUITY
        print(f"Using default virtual equity: ${VIRTUAL_EQUITY:,.0f}")
    per_trade = VIRTUAL_EQUITY * POSITION_PCT
    print(
        f"Note: Sizing aligned with ~$100k paper research book "
        f"({POSITION_PCT:.0%}/trade ~ ${per_trade:,.0f})."
    )
    return VIRTUAL_EQUITY


def set_entry_params(
    drop_pct: float | None = None,
    rsi_max: float | None = None,
    rsi_min: float | None = None,
) -> None:
    global _entry_params
    _entry_params = EntryParams(
        drop_pct=drop_pct if drop_pct is not None else DROP_PCT,
        rsi_min=rsi_min,
        rsi_max=rsi_max if rsi_max is not None else RSI_MAX,
    )


def load_spy_daily_returns() -> pd.Series | None:
    """Load SPY daily close returns (%) from market_data.db."""
    if not DB_PATH.is_file():
        return None
    table = "SPY_daily"
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if cur.fetchone() is None:
            return None
        spy = pd.read_sql(f'SELECT Date, Close FROM "{table}" ORDER BY Date', conn)
    finally:
        conn.close()
    if spy.empty:
        return None
    spy["Date"] = pd.to_datetime(spy["Date"], utc=True).dt.tz_localize(None).dt.normalize()
    spy["Close"] = pd.to_numeric(spy["Close"], errors="coerce")
    spy = spy.dropna().sort_values("Date").drop_duplicates("Date", keep="last")
    spy["spy_return_pct"] = spy["Close"].pct_change() * 100
    return spy.set_index("Date")["spy_return_pct"]


def hour_allowed_utc(dt) -> bool:
    """True when dt falls in v4 UTC entry windows (naive timestamps treated as UTC)."""
    if getattr(dt, "tzinfo", None) is not None:
        hour = dt.astimezone(timezone.utc).hour
    else:
        hour = int(dt.hour)
    return any(start <= hour <= end for start, end in HOUR_WINDOWS_UTC)


def print_v4_filter_header() -> None:
    print("\n=== v4 Active Filters ===")
    print(f"1. SPY gate:        skip entry when SPY daily return < {SPY_GATE_PCT:.0f}%")
    print("2. Hour filter:     entries only 13:00-16:00 UTC or 18:00-22:00 UTC (inclusive)")
    print(f"3. RSI floor:       RSI({RSI_PERIOD}) must be {RSI_MIN_V4}-{RSI_MAX_V4} (inclusive)")
    print(f"4. Loss cooldown:   block coin for {LOSS_COOLDOWN_HOURS}h after stop-loss")
    print("5. Drop ARB:        ARB/USD removed from universe")


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [str(c[0]) for c in out.columns]
    rename = {}
    for col in out.columns:
        low = str(col).lower()
        if low == "open":
            rename[col] = "Open"
        elif low == "high":
            rename[col] = "High"
        elif low == "low":
            rename[col] = "Low"
        elif low == "close":
            rename[col] = "Close"
        elif low in ("date", "datetime", "timestamp"):
            rename[col] = "Date"
    out = out.rename(columns=rename)
    if "Date" not in out.columns:
        out = out.reset_index()
        date_col = next(
            (c for c in out.columns if str(c).lower() in ("date", "datetime", "timestamp", "index")),
            out.columns[0],
        )
        out = out.rename(columns={date_col: "Date"})
    keep = [c for c in ("Date", "Open", "High", "Low", "Close") if c in out.columns]
    out = out[keep].copy()
    out["Date"] = pd.to_datetime(out["Date"], utc=True).dt.tz_localize(None)
    out = out.sort_values("Date").drop_duplicates("Date", keep="last")
    for col in ("Open", "High", "Low", "Close"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Close"]).reset_index(drop=True)
    if "Open" not in out.columns:
        out["Open"] = out["Close"]
    if "High" not in out.columns:
        out["High"] = out["Close"]
    if "Low" not in out.columns:
        out["Low"] = out["Close"]
    return out


def fetch_alpaca_hourly(symbol: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Fetch 1-hour crypto bars from Alpaca paper market data API."""
    api_key, secret_key = _paper_alpaca_keys()
    client = CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days)

    try:
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=start,
            end=end,
        )
        bars = client.get_crypto_bars(request)
    except Exception as exc:
        print(f"  {symbol}: Alpaca API error — {exc}")
        return pd.DataFrame()

    if bars is None or bars.df is None or bars.df.empty:
        print(f"  {symbol}: no bars returned (symbol may be unavailable on Alpaca)")
        return pd.DataFrame()

    df = _normalize_ohlcv(bars.df.reset_index())
    print(f"  {symbol}: fetched {len(df)} hourly bars from Alpaca")
    return df


def _history_span_days(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    span = df["Date"].max() - df["Date"].min()
    return span.total_seconds() / 86400.0


def load_coin_data(symbol: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame | None:
    df = fetch_alpaca_hourly(symbol, days=days)
    if df.empty:
        return None
    span_days = _history_span_days(df)
    if span_days < MIN_HISTORY_DAYS:
        print(
            f"  SKIP {symbol}: only {span_days:.1f} days of hourly history "
            f"(need >= {MIN_HISTORY_DAYS})"
        )
        return None
    cutoff = df["Date"].max() - pd.Timedelta(days=days)
    return df[df["Date"] >= cutoff].reset_index(drop=True)


def compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["sma10"] = out["Close"].rolling(SMA_PERIOD).mean()
    out["rsi14"] = compute_rsi(out["Close"], RSI_PERIOD)
    out["close_4h_ago"] = out["Close"].shift(FOUR_H_BARS)
    out["ret_4h"] = (out["Close"] - out["close_4h_ago"]) / out["close_4h_ago"]
    return out


def _drop_ok(row: pd.Series) -> bool:
    return not pd.isna(row["ret_4h"]) and row["ret_4h"] < _entry_params.drop_pct


def _sma_ok(row: pd.Series) -> bool:
    # Mean-reversion dip buy: price below SMA(10) after a sharp drop
    return not pd.isna(row["sma10"]) and row["Close"] < row["sma10"]


def entry_signal(row: pd.Series) -> bool:
    return _drop_ok(row) and _sma_ok(row) and _rsi_band_ok(row)


def base_entry_signal(row: pd.Series) -> bool:
    """Drop + below SMA before RSI band / v4 filters."""
    return _drop_ok(row) and _sma_ok(row)


def count_condition_signals(data: dict[str, pd.DataFrame]) -> dict[str, ConditionCounts]:
    """Count raw entry-condition hits per coin before portfolio filters."""
    warmup = max(SMA_PERIOD, RSI_PERIOD, FOUR_H_BARS)
    out: dict[str, ConditionCounts] = {}
    for coin, df in data.items():
        enriched = enrich(df)
        counts = ConditionCounts()
        for idx in range(warmup, len(enriched)):
            row = enriched.iloc[idx]
            drop = _drop_ok(row)
            rsi = _rsi_band_ok(row)
            sma = _sma_ok(row)
            if drop:
                counts.drop += 1
            if rsi:
                counts.rsi += 1
            if sma:
                counts.below_sma += 1
            if drop and rsi and sma:
                counts.combined += 1
        out[coin] = counts
    return out


def _rsi_threshold_label() -> str:
    if _entry_params.rsi_min is not None:
        return f"RSI({RSI_PERIOD}) {_entry_params.rsi_min:.0f}-{_entry_params.rsi_max:.0f}"
    return f"RSI({RSI_PERIOD}) <= {_entry_params.rsi_max}"


def _rsi_band_ok(row: pd.Series) -> bool:
    if pd.isna(row["rsi14"]):
        return False
    rsi = float(row["rsi14"])
    if _entry_params.rsi_min is not None:
        return _entry_params.rsi_min <= rsi <= _entry_params.rsi_max
    return rsi <= _entry_params.rsi_max


def print_signal_diagnostics(counts: dict[str, ConditionCounts]) -> None:
    print("\n--- Signal diagnostics (before portfolio filters) ---")
    print(
        f"Thresholds: 4h drop < {_entry_params.drop_pct:.1%}, "
        f"{_rsi_threshold_label()}, "
        f"close < SMA({SMA_PERIOD})"
    )
    header = (
        f"{'Coin':<12} {'Drop':>7} {'RSI':>7} {'<SMA10':>8} {'Combined':>9}"
    )
    print(header)
    print("-" * len(header))
    totals = ConditionCounts()
    for coin in sorted(counts):
        c = counts[coin]
        totals.drop += c.drop
        totals.rsi += c.rsi
        totals.below_sma += c.below_sma
        totals.combined += c.combined
        print(
            f"{coin:<12} {c.drop:>7d} {c.rsi:>7d} {c.below_sma:>8d} {c.combined:>9d}"
        )
    print("-" * len(header))
    print(
        f"{'TOTAL':<12} {totals.drop:>7d} {totals.rsi:>7d} "
        f"{totals.below_sma:>8d} {totals.combined:>9d}"
    )
    if totals.combined == 0:
        print("WARNING: Combined signals = 0 — no entries will fire at these thresholds.")
    else:
        print(f"Combined signals fire: YES ({totals.combined} raw hits across universe).")


def check_exit(
    pos: Position,
    bar_idx: int,
    row: pd.Series,
) -> tuple[float, str] | None:
    stop = pos.entry_price * (1 - STOP_LOSS_PCT)
    target = pos.entry_price * (1 + TAKE_PROFIT_PCT)
    bars_held = bar_idx - pos.entry_idx

    if row["Low"] <= stop:
        return stop, "stop_loss"
    if row["High"] >= target:
        return target, "take_profit"
    if bars_held >= TIMEOUT_BARS:
        return float(row["Close"]), "timeout"
    return None


def run_backtest(
    data: dict[str, pd.DataFrame],
    config: BacktestConfig | None = None,
) -> tuple[list[Trade], pd.Series, dict[str, SignalStats], FilterSkipCounts]:
    cfg = config or BacktestConfig(label="default", universe=data)
    frames = {
        coin: enrich(df)
        for coin, df in data.items()
        if coin in cfg.universe and not df.empty
    }
    if not frames:
        raise RuntimeError("No market data loaded for any coin.")

    spy_returns = load_spy_daily_returns() if cfg.spy_gate else None
    if cfg.spy_gate and spy_returns is None:
        print("  WARNING: SPY gate enabled but SPY_daily missing in market_data.db — gate skipped.")

    warmup = max(SMA_PERIOD, RSI_PERIOD, FOUR_H_BARS)
    date_index = sorted({d for df in frames.values() for d in df["Date"]})
    date_to_idx = {coin: dict(zip(df["Date"], df.index)) for coin, df in frames.items()}

    cash = VIRTUAL_EQUITY
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_rows: list[tuple[pd.Timestamp, float]] = []
    signal_stats: dict[str, SignalStats] = {coin: SignalStats() for coin in frames}
    filter_skips = FilterSkipCounts()
    cooldown_until: dict[str, pd.Timestamp] = {}

    for dt in date_index:
        # --- exits (bars after entry only) ---
        for coin in list(positions.keys()):
            if coin not in frames or dt not in date_to_idx[coin]:
                continue
            df = frames[coin]
            idx = date_to_idx[coin][dt]
            if idx <= positions[coin].entry_idx:
                continue
            row = df.loc[idx]
            hit = check_exit(positions[coin], idx, row)
            if hit is None:
                continue
            exit_price, reason = hit
            pos = positions.pop(coin)
            if cfg.loss_cooldown and reason == "stop_loss":
                cooldown_until[coin] = dt + pd.Timedelta(hours=LOSS_COOLDOWN_HOURS)
            proceeds = pos.shares * exit_price * (1 - FEE_PCT)
            cash += proceeds
            pnl_pct = (proceeds - pos.cash_in) / pos.cash_in * 100
            hold_hours = (dt - pos.entry_date).total_seconds() / 3600
            trades.append(
                Trade(
                    date=dt,
                    coin=coin,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    exit_reason=reason,
                    hold_hours=hold_hours,
                )
            )

        # --- mark-to-market equity ---
        mtm = cash
        for coin, pos in positions.items():
            if coin in frames and dt in date_to_idx[coin]:
                idx = date_to_idx[coin][dt]
                mtm += pos.shares * float(frames[coin].loc[idx, "Close"])
        equity_rows.append((dt, mtm))

        # --- entries at 1h bar close ---
        for coin, df in frames.items():
            if coin in positions:
                continue
            if dt not in date_to_idx[coin]:
                continue
            idx = date_to_idx[coin][dt]
            if idx < warmup:
                continue
            row = df.loc[idx]
            if not base_entry_signal(row):
                continue

            if not _rsi_band_ok(row):
                if _entry_params.rsi_min is not None:
                    signal_stats[coin].signals += 1
                    signal_stats[coin].skipped += 1
                    filter_skips.rsi_floor += 1
                continue

            signal_stats[coin].signals += 1

            if cfg.hour_filter and not hour_allowed_utc(dt):
                signal_stats[coin].skipped += 1
                filter_skips.hour_filter += 1
                continue

            if cfg.spy_gate and spy_returns is not None:
                entry_day = pd.Timestamp(dt).normalize()
                spy_ret = spy_returns.get(entry_day, np.nan)
                if not pd.isna(spy_ret) and spy_ret < SPY_GATE_PCT:
                    signal_stats[coin].skipped += 1
                    filter_skips.spy_gate += 1
                    continue

            if cfg.loss_cooldown and coin in cooldown_until and dt < cooldown_until[coin]:
                signal_stats[coin].skipped += 1
                filter_skips.loss_cooldown += 1
                continue

            if len(positions) >= MAX_POSITIONS:
                signal_stats[coin].skipped += 1
                filter_skips.max_positions += 1
                continue

            notional = mtm * POSITION_PCT
            cost = notional * (1 + FEE_PCT)
            if cost > cash or notional < 1:
                signal_stats[coin].skipped += 1
                filter_skips.insufficient_cash += 1
                continue

            entry_price = float(row["Close"])
            shares = notional / entry_price
            cash -= cost
            positions[coin] = Position(
                coin=coin,
                entry_date=dt,
                entry_idx=idx,
                entry_price=entry_price,
                shares=shares,
                cash_in=cost,
            )
            signal_stats[coin].taken += 1

    # liquidate any open positions at last available close
    for coin, pos in list(positions.items()):
        df = frames[coin]
        last_row = df.iloc[-1]
        dt = last_row["Date"]
        exit_price = float(last_row["Close"])
        proceeds = pos.shares * exit_price * (1 - FEE_PCT)
        cash += proceeds
        pnl_pct = (proceeds - pos.cash_in) / pos.cash_in * 100
        hold_hours = (dt - pos.entry_date).total_seconds() / 3600
        trades.append(
            Trade(
                date=dt,
                coin=coin,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                pnl_pct=pnl_pct,
                exit_reason="eod_liquidation",
                hold_hours=hold_hours,
            )
        )

    equity = pd.Series(
        [e for _, e in equity_rows],
        index=pd.DatetimeIndex([d for d, _ in equity_rows]),
        name="equity",
    )
    return trades, equity, signal_stats, filter_skips


def compute_metrics(trades: list[Trade], equity: pd.Series) -> dict:
    if equity.empty:
        total_ret = sum(t.pnl_pct for t in trades) if trades else 0.0
        sharpe = 0.0
        max_dd = 0.0
    else:
        total_ret = (equity.iloc[-1] / VIRTUAL_EQUITY - 1) * 100
        returns = equity.pct_change().dropna()
        sharpe = (
            (returns.mean() / returns.std()) * SHARPE_SCALE if returns.std() != 0 else 0.0
        )
        max_dd = ((equity / equity.cummax()) - 1).min() * 100

    wins = sum(1 for t in trades if t.pnl_pct > 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0
    return {
        "total_return_pct": round(total_ret, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "win_rate_pct": round(win_rate, 2),
        "total_trades": len(trades),
    }


def per_coin_breakdown(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            columns=["coin", "trades", "win_rate_pct", "avg_pnl_pct", "total_pnl_pct"]
        )
    rows = []
    df = pd.DataFrame([t.__dict__ for t in trades])
    for coin, grp in df.groupby("coin"):
        wins = (grp["pnl_pct"] > 0).sum()
        rows.append(
            {
                "coin": coin,
                "trades": len(grp),
                "win_rate_pct": round(wins / len(grp) * 100, 2),
                "avg_pnl_pct": round(grp["pnl_pct"].mean(), 2),
                "total_pnl_pct": round(grp["pnl_pct"].sum(), 2),
            }
        )
    return pd.DataFrame(rows).sort_values("coin")


def print_filter_skip_counts(filter_skips: FilterSkipCounts) -> None:
    print("\n--- v4 filter skip counts ---")
    print(f"  SPY gate:          {filter_skips.spy_gate:>5d}")
    print(f"  Hour filter:       {filter_skips.hour_filter:>5d}")
    print(f"  RSI floor/ceiling: {filter_skips.rsi_floor:>5d}")
    print(f"  Loss cooldown:     {filter_skips.loss_cooldown:>5d}")
    print(f"  Max positions:     {filter_skips.max_positions:>5d}")
    print(f"  Insufficient cash: {filter_skips.insufficient_cash:>5d}")


def print_summary(
    metrics: dict,
    breakdown: pd.DataFrame,
    signal_stats: dict[str, SignalStats],
    *,
    version_label: str = "",
    filter_skips: FilterSkipCounts | None = None,
) -> None:
    title = "=== Crypto Vol Mean-Reversion Backtest"
    if version_label:
        title += f" ({version_label})"
    title += " ==="
    print(f"\n{title}")
    print(f"Period:           ~{LOOKBACK_DAYS} days, 1-hour bars (Alpaca paper)")
    print(
        f"Entry thresholds: 4h drop < {_entry_params.drop_pct:.1%}, "
        f"{_rsi_threshold_label()}, close < SMA({SMA_PERIOD})"
    )
    print(f"Virtual equity:   ${VIRTUAL_EQUITY:,.0f} (sizing only; PnL tracked per trade %)")
    print(
        f"Position size:    {POSITION_PCT:.0%} ~ ${VIRTUAL_EQUITY * POSITION_PCT:,.0f}/trade, "
        f"max {MAX_POSITIONS} concurrent"
    )
    print(f"Fee per leg:      {FEE_PCT:.2%} ({FEE_PCT * 2:.2%} round trip)")
    print(
        "Sharpe method:    hourly equity pct_change, annualized x sqrt(365*24) "
        "(crypto 24/7)"
    )
    print()
    header = (
        f"{'Total Return':>14} {'Sharpe':>8} {'Max DD':>9} "
        f"{'Win Rate':>10} {'Trades':>8}"
    )
    print(header)
    print("-" * len(header))
    print(
        f"{metrics['total_return_pct']:>13.2f}% "
        f"{metrics['sharpe']:>8.2f} "
        f"{metrics['max_drawdown_pct']:>8.2f}% "
        f"{metrics['win_rate_pct']:>9.2f}% "
        f"{metrics['total_trades']:>8d}"
    )
    if not breakdown.empty:
        print("\n--- Per-coin breakdown ---")
        bheader = (
            f"{'Coin':<12} {'Trades':>7} {'Win%':>8} "
            f"{'Avg PnL%':>10} {'Sum PnL%':>10}"
        )
        print(bheader)
        print("-" * len(bheader))
        for _, row in breakdown.iterrows():
            print(
                f"{row['coin']:<12} "
                f"{int(row['trades']):>7d} "
                f"{row['win_rate_pct']:>7.2f}% "
                f"{row['avg_pnl_pct']:>9.2f}% "
                f"{row['total_pnl_pct']:>9.2f}%"
            )

    print("\n--- Signals per coin ---")
    sheader = f"{'Coin':<12} {'Signals':>8} {'Taken':>8} {'Skipped':>8}"
    print(sheader)
    print("-" * len(sheader))
    for coin in sorted(signal_stats):
        st = signal_stats[coin]
        print(f"{coin:<12} {st.signals:>8d} {st.taken:>8d} {st.skipped:>8d}")

    if filter_skips is not None:
        print_filter_skip_counts(filter_skips)


def print_version_comparison(v3_metrics: dict, v4_metrics: dict) -> None:
    print("\n=== v3 vs v4 Comparison ===")
    header = (
        f"{'Version':<8} {'Return':>10} {'Sharpe':>8} {'Max DD':>9} "
        f"{'Win Rate':>10} {'Trades':>8}"
    )
    print(header)
    print("-" * len(header))
    for label, m in (("v3", v3_metrics), ("v4", v4_metrics)):
        print(
            f"{label:<8} "
            f"{m['total_return_pct']:>9.2f}% "
            f"{m['sharpe']:>8.2f} "
            f"{m['max_drawdown_pct']:>8.2f}% "
            f"{m['win_rate_pct']:>9.2f}% "
            f"{m['total_trades']:>8d}"
        )

    sharpe_ok = v4_metrics["sharpe"] > 1.3
    win_ok = v4_metrics["win_rate_pct"] > 60.0
    print("\n--- v4 Success Criteria (paper sleeve wiring) ---")
    print(f"  Sharpe > 1.3:     {v4_metrics['sharpe']:.2f}  {'PASS' if sharpe_ok else 'FAIL'}")
    print(
        f"  Win rate > 60%:   {v4_metrics['win_rate_pct']:.2f}%  "
        f"{'PASS' if win_ok else 'FAIL'}"
    )
    print(f"  Overall:          {'PASS' if sharpe_ok and win_ok else 'FAIL'}")


def run_version_backtest(
    data: dict[str, pd.DataFrame],
    config: BacktestConfig,
) -> tuple[list[Trade], pd.Series, dict[str, SignalStats], FilterSkipCounts, dict]:
    set_entry_params(config.drop_pct, config.rsi_max, config.rsi_min)
    condition_counts = count_condition_signals(
        {k: v for k, v in data.items() if k in config.universe}
    )
    print_signal_diagnostics(condition_counts)

    trades, equity, signal_stats, filter_skips = run_backtest(data, config)
    metrics = compute_metrics(trades, equity)

    if config.allow_relaxed_retry and metrics["total_trades"] < MIN_TRADES_TARGET:
        print(
            f"\n--- Only {metrics['total_trades']} trades (< {MIN_TRADES_TARGET}); "
            f"retrying with relaxed thresholds (drop > 2.5%, RSI <= 45) ---"
        )
        set_entry_params(DROP_PCT_RELAXED, RSI_MAX_RELAXED, rsi_min=None)
        condition_counts = count_condition_signals(
            {k: v for k, v in data.items() if k in config.universe}
        )
        print_signal_diagnostics(condition_counts)
        trades, equity, signal_stats, filter_skips = run_backtest(data, config)
        metrics = compute_metrics(trades, equity)

    return trades, equity, signal_stats, filter_skips, metrics


def save_trades_csv(trades: list[Trade], path: Path) -> None:
    rows = [
        {
            "date": t.date.strftime("%Y-%m-%d %H:%M"),
            "coin": t.coin,
            "entry_price": round(t.entry_price, 6),
            "exit_price": round(t.exit_price, 6),
            "pnl_pct": round(t.pnl_pct, 4),
            "exit_reason": t.exit_reason,
            "hold_hours": round(t.hold_hours, 1),
        }
        for t in trades
    ]
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"\nSaved trade log: {path}")


def main() -> None:
    _load_env()
    resolve_virtual_equity()

    print("\nLoading crypto 1h OHLCV from Alpaca paper market data...")
    data: dict[str, pd.DataFrame] = {}
    for label, alpaca_symbol in UNIVERSE_V3.items():
        df = load_coin_data(alpaca_symbol)
        if df is None or df.empty:
            print(f"  WARNING: skipping {label}")
            continue
        data[label] = df

    if not data:
        raise SystemExit("No coins with sufficient Alpaca hourly history.")

    v3_config = BacktestConfig(
        label="v3",
        universe=UNIVERSE_V3,
        drop_pct=DROP_PCT,
        rsi_min=None,
        rsi_max=RSI_MAX,
        allow_relaxed_retry=True,
    )
    print("\n" + "=" * 60)
    print("Running v3 baseline (no correlation filters)")
    print("=" * 60)
    v3_trades, _, v3_signal_stats, _, v3_metrics = run_version_backtest(data, v3_config)
    v3_breakdown = per_coin_breakdown(v3_trades)
    print_summary(v3_metrics, v3_breakdown, v3_signal_stats, version_label="v3")

    v4_config = BacktestConfig(
        label="v4",
        universe=UNIVERSE_V4,
        drop_pct=DROP_PCT,
        rsi_min=RSI_MIN_V4,
        rsi_max=RSI_MAX_V4,
        spy_gate=True,
        hour_filter=True,
        loss_cooldown=True,
        allow_relaxed_retry=False,
    )
    print("\n" + "=" * 60)
    print("Running v4 (correlation filters)")
    print("=" * 60)
    print_v4_filter_header()
    v4_trades, _, v4_signal_stats, v4_filter_skips, v4_metrics = run_version_backtest(
        data, v4_config
    )
    v4_breakdown = per_coin_breakdown(v4_trades)
    print_summary(
        v4_metrics,
        v4_breakdown,
        v4_signal_stats,
        version_label="v4",
        filter_skips=v4_filter_skips,
    )
    save_trades_csv(v4_trades, OUT_CSV_V4)

    print_version_comparison(v3_metrics, v4_metrics)

    trade_note = "meets" if v4_metrics["total_trades"] >= MIN_TRADES_TARGET else "below"
    print(
        f"\nv4: {v4_metrics['total_trades']} trades — {trade_note} "
        f"{MIN_TRADES_TARGET}+ threshold."
    )


if __name__ == "__main__":
    main()
