"""Crypto Vol Backtest v5 — 15-minute bars + mean-reversion + breakout entries.

Standalone research script (does not modify production modules).

Compares:
  - v4: hourly mean-reversion (v4 filters, v4 coin universe)
  - v5 MR-only: 15m bars, config crypto universe, mean-reversion only
  - v5 MR+Breakout: 15m bars, both signal types tagged

Run:
  python scripts/research/backtest_crypto_vol_v5.py
  python scripts/research/backtest_crypto_vol_v5.py --days 90
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import find_dotenv, load_dotenv

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from backtest_crypto_vol import (  # noqa: E402
    DROP_PCT,
    FEE_PCT,
    BacktestConfig,
    FilterSkipCounts,
    LOOKBACK_DAYS,
    LOSS_COOLDOWN_HOURS,
    MAX_POSITIONS,
    MIN_HISTORY_DAYS,
    POSITION_PCT,
    RSI_MAX_V4,
    RSI_MIN_V4,
    RSI_PERIOD,
    SMA_PERIOD,
    SPY_GATE_PCT,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    UNIVERSE_V3,
    UNIVERSE_V4,
    VIRTUAL_EQUITY,
    compute_rsi,
    hour_allowed_utc,
    load_coin_data,
    load_spy_daily_returns,
    resolve_virtual_equity,
    run_version_backtest,
    set_entry_params,
)

OUT_CSV = Path(__file__).resolve().parent / "crypto_vol_backtest_v5_results.csv"

# --- v5 15m constants ---
BAR_MINUTES = 15
FOUR_H_BARS_15M = 16  # 4h / 15m
TIMEOUT_BARS_15M = 192  # 48h on 15m bars
SHARPE_SCALE_15M = float(np.sqrt(365 * 24 * (60 // BAR_MINUTES)))
BREAKOUT_PCT = 0.03
BREAKOUT_RSI_MIN = 58.0
VOL_MA_PERIOD = 20
BREAKOUT_VOL_MULT = 1.5


@dataclass
class PositionV5:
    coin: str
    entry_date: pd.Timestamp
    entry_idx: int
    entry_price: float
    shares: float
    cash_in: float
    signal_type: str


@dataclass
class TradeV5:
    date: pd.Timestamp
    coin: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str
    hold_hours: float
    signal_type: str


@dataclass
class SignalStatsV5:
    signals: int = 0
    taken: int = 0
    skipped: int = 0
    mr_signals: int = 0
    breakout_signals: int = 0


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


def _to_alpaca_symbol(sym: str) -> str:
    norm = config.normalize_symbol(sym)
    return norm.replace("-USD", "/USD")


def build_v5_universe(*, expanded: bool | None = None) -> dict[str, str]:
    """Display label -> Alpaca symbol (config base + optional expanded list)."""
    use_expanded = (
        config.effective_crypto_universe_expanded()
        if expanded is None
        else bool(expanded)
    )
    if use_expanded:
        from modules.crypto_universe import expanded_crypto_symbols

        symbols = expanded_crypto_symbols()
    else:
        symbols = config.base_crypto_universe()
    out: dict[str, str] = {}
    for sym in symbols:
        alpaca = _to_alpaca_symbol(sym)
        out[alpaca] = alpaca
    return out


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [str(c[0]) for c in out.columns]
    rename: dict[str, str] = {}
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
        elif low == "volume":
            rename[col] = "Volume"
        elif low in ("date", "datetime", "timestamp"):
            rename[col] = "Date"
    out = out.rename(columns=rename)
    if "Date" not in out.columns:
        out = out.reset_index()
        date_col = next(
            (
                c
                for c in out.columns
                if str(c).lower() in ("date", "datetime", "timestamp", "index")
            ),
            out.columns[0],
        )
        out = out.rename(columns={date_col: "Date"})
    keep = [c for c in ("Date", "Open", "High", "Low", "Close", "Volume") if c in out.columns]
    out = out[keep].copy()
    out["Date"] = pd.to_datetime(out["Date"], utc=True).dt.tz_localize(None)
    out = out.sort_values("Date").drop_duplicates("Date", keep="last")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Close"]).reset_index(drop=True)
    if "Open" not in out.columns:
        out["Open"] = out["Close"]
    if "High" not in out.columns:
        out["High"] = out["Close"]
    if "Low" not in out.columns:
        out["Low"] = out["Close"]
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    return out


def fetch_alpaca_15m(symbol: str, days: int) -> pd.DataFrame:
    api_key, secret_key = _paper_alpaca_keys()
    client = CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days)
    try:
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start,
            end=end,
        )
        bars = client.get_crypto_bars(request)
    except Exception as exc:
        print(f"  {symbol}: Alpaca API error — {exc}")
        return pd.DataFrame()
    if bars is None or bars.df is None or bars.df.empty:
        print(f"  {symbol}: no 15m bars returned")
        return pd.DataFrame()
    df = _normalize_ohlcv(bars.df.reset_index())
    print(f"  {symbol}: fetched {len(df)} x 15m bars")
    return df


def _history_span_days(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    span = df["Date"].max() - df["Date"].min()
    return span.total_seconds() / 86400.0


def load_coin_data_15m(symbol: str, days: int) -> pd.DataFrame | None:
    df = fetch_alpaca_15m(symbol, days=days)
    if df.empty:
        return None
    span_days = _history_span_days(df)
    if span_days < MIN_HISTORY_DAYS:
        print(
            f"  SKIP {symbol}: only {span_days:.1f} days of 15m history "
            f"(need >= {MIN_HISTORY_DAYS})"
        )
        return None
    cutoff = df["Date"].max() - pd.Timedelta(days=days)
    return df[df["Date"] >= cutoff].reset_index(drop=True)


def enrich_15m(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["sma10"] = out["Close"].rolling(SMA_PERIOD).mean()
    out["rsi14"] = compute_rsi(out["Close"], RSI_PERIOD)
    out["close_4h_ago"] = out["Close"].shift(FOUR_H_BARS_15M)
    out["ret_4h"] = (out["Close"] - out["close_4h_ago"]) / out["close_4h_ago"]
    out["vol_ma20"] = out["Volume"].rolling(VOL_MA_PERIOD).mean()
    return out


def mr_signal(row: pd.Series) -> bool:
    if pd.isna(row["ret_4h"]) or pd.isna(row["rsi14"]) or pd.isna(row["sma10"]):
        return False
    rsi = float(row["rsi14"])
    return (
        float(row["ret_4h"]) < DROP_PCT
        and RSI_MIN_V4 <= rsi <= RSI_MAX_V4
        and float(row["Close"]) < float(row["sma10"])
    )


def breakout_signal(row: pd.Series) -> bool:
    if pd.isna(row["ret_4h"]) or pd.isna(row["rsi14"]) or pd.isna(row["sma10"]):
        return False
    vol_ok = (
        not pd.isna(row.get("vol_ma20"))
        and float(row.get("Volume") or 0) > BREAKOUT_VOL_MULT * float(row["vol_ma20"])
    )
    return (
        float(row["ret_4h"]) > BREAKOUT_PCT
        and float(row["rsi14"]) > BREAKOUT_RSI_MIN
        and float(row["Close"]) > float(row["sma10"])
        and vol_ok
    )


def pick_entry_signal(row: pd.Series, *, allow_breakout: bool) -> str | None:
    if mr_signal(row):
        return "mean_reversion"
    if allow_breakout and breakout_signal(row):
        return "breakout"
    return None


def check_exit_v5(pos: PositionV5, bar_idx: int, row: pd.Series) -> tuple[float, str] | None:
    stop = pos.entry_price * (1 - STOP_LOSS_PCT)
    target = pos.entry_price * (1 + TAKE_PROFIT_PCT)
    bars_held = bar_idx - pos.entry_idx
    if row["Low"] <= stop:
        return stop, "stop_loss"
    if row["High"] >= target:
        return target, "take_profit"
    if bars_held >= TIMEOUT_BARS_15M:
        return float(row["Close"]), "timeout"
    return None


def run_backtest_v5(
    data: dict[str, pd.DataFrame],
    universe: dict[str, str],
    *,
    allow_breakout: bool,
    label: str,
) -> tuple[list[TradeV5], pd.Series, dict[str, SignalStatsV5], FilterSkipCounts]:
    frames = {
        coin: enrich_15m(df)
        for coin, df in data.items()
        if coin in universe and not df.empty
    }
    if not frames:
        raise RuntimeError(f"No 15m data for {label}")

    spy_returns = load_spy_daily_returns()
    warmup = max(SMA_PERIOD, RSI_PERIOD, FOUR_H_BARS_15M, VOL_MA_PERIOD)
    date_index = sorted({d for df in frames.values() for d in df["Date"]})
    date_to_idx = {coin: dict(zip(df["Date"], df.index)) for coin, df in frames.items()}

    cash = VIRTUAL_EQUITY
    positions: dict[str, PositionV5] = {}
    trades: list[TradeV5] = []
    equity_rows: list[tuple[pd.Timestamp, float]] = []
    signal_stats: dict[str, SignalStatsV5] = {coin: SignalStatsV5() for coin in frames}
    filter_skips = FilterSkipCounts()
    cooldown_until: dict[str, pd.Timestamp] = {}

    for dt in date_index:
        for coin in list(positions.keys()):
            if coin not in frames or dt not in date_to_idx[coin]:
                continue
            idx = date_to_idx[coin][dt]
            if idx <= positions[coin].entry_idx:
                continue
            row = frames[coin].loc[idx]
            hit = check_exit_v5(positions[coin], idx, row)
            if hit is None:
                continue
            exit_price, reason = hit
            pos = positions.pop(coin)
            if reason == "stop_loss":
                cooldown_until[coin] = dt + pd.Timedelta(hours=LOSS_COOLDOWN_HOURS)
            proceeds = pos.shares * exit_price * (1 - FEE_PCT)
            cash += proceeds
            pnl_pct = (proceeds - pos.cash_in) / pos.cash_in * 100
            hold_hours = (dt - pos.entry_date).total_seconds() / 3600
            trades.append(
                TradeV5(
                    date=dt,
                    coin=coin,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    exit_reason=reason,
                    hold_hours=hold_hours,
                    signal_type=pos.signal_type,
                )
            )

        mtm = cash
        for coin, pos in positions.items():
            if coin in frames and dt in date_to_idx[coin]:
                idx = date_to_idx[coin][dt]
                mtm += pos.shares * float(frames[coin].loc[idx, "Close"])
        equity_rows.append((dt, mtm))

        for coin, df in frames.items():
            if coin in positions:
                continue
            if dt not in date_to_idx[coin]:
                continue
            idx = date_to_idx[coin][dt]
            if idx < warmup:
                continue
            row = df.loc[idx]
            signal_type = pick_entry_signal(row, allow_breakout=allow_breakout)
            if signal_type is None:
                continue

            signal_stats[coin].signals += 1
            if signal_type == "mean_reversion":
                signal_stats[coin].mr_signals += 1
            else:
                signal_stats[coin].breakout_signals += 1

            if not hour_allowed_utc(dt):
                signal_stats[coin].skipped += 1
                filter_skips.hour_filter += 1
                continue

            if spy_returns is not None:
                entry_day = pd.Timestamp(dt).normalize()
                spy_ret = spy_returns.get(entry_day, np.nan)
                if not pd.isna(spy_ret) and spy_ret < SPY_GATE_PCT:
                    signal_stats[coin].skipped += 1
                    filter_skips.spy_gate += 1
                    continue

            if coin in cooldown_until and dt < cooldown_until[coin]:
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
            positions[coin] = PositionV5(
                coin=coin,
                entry_date=dt,
                entry_idx=idx,
                entry_price=entry_price,
                shares=shares,
                cash_in=cost,
                signal_type=signal_type,
            )
            signal_stats[coin].taken += 1

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
            TradeV5(
                date=dt,
                coin=coin,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                pnl_pct=pnl_pct,
                exit_reason="eod_liquidation",
                hold_hours=hold_hours,
                signal_type=pos.signal_type,
            )
        )

    equity = pd.Series(
        [e for _, e in equity_rows],
        index=pd.DatetimeIndex([d for d, _ in equity_rows]),
        name="equity",
    )
    return trades, equity, signal_stats, filter_skips


def compute_metrics_v5(trades: list[TradeV5], equity: pd.Series) -> dict:
    if equity.empty:
        total_ret = sum(t.pnl_pct for t in trades) if trades else 0.0
        sharpe = 0.0
        max_dd = 0.0
    else:
        total_ret = (equity.iloc[-1] / VIRTUAL_EQUITY - 1) * 100
        returns = equity.pct_change().dropna()
        sharpe = (
            (returns.mean() / returns.std()) * SHARPE_SCALE_15M if returns.std() != 0 else 0.0
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


def signal_type_breakdown(trades: list[TradeV5]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            columns=["signal_type", "trades", "win_rate_pct", "avg_pnl_pct", "total_pnl_pct"]
        )
    rows = []
    df = pd.DataFrame([t.__dict__ for t in trades])
    for sig, grp in df.groupby("signal_type"):
        wins = (grp["pnl_pct"] > 0).sum()
        rows.append(
            {
                "signal_type": sig,
                "trades": len(grp),
                "win_rate_pct": round(wins / len(grp) * 100, 2),
                "avg_pnl_pct": round(grp["pnl_pct"].mean(), 2),
                "total_pnl_pct": round(grp["pnl_pct"].sum(), 2),
            }
        )
    return pd.DataFrame(rows).sort_values("signal_type")


def save_trades_csv_v5(trades: list[TradeV5], path: Path) -> None:
    rows = [
        {
            "date": t.date.strftime("%Y-%m-%d %H:%M"),
            "coin": t.coin,
            "signal_type": t.signal_type,
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


def print_comparison_table(rows: list[tuple[str, dict]]) -> None:
    print("\n=== Comparison: v4 (hourly MR) vs v5 MR-only vs v5 MR+Breakout ===")
    header = (
        f"{'Variant':<22} {'Return':>10} {'Sharpe':>8} {'Max DD':>9} "
        f"{'Win Rate':>10} {'Trades':>8}"
    )
    print(header)
    print("-" * len(header))
    for label, m in rows:
        print(
            f"{label:<22} "
            f"{m['total_return_pct']:>9.2f}% "
            f"{m['sharpe']:>8.2f} "
            f"{m['max_drawdown_pct']:>8.2f}% "
            f"{m['win_rate_pct']:>9.2f}% "
            f"{m['total_trades']:>8d}"
        )


def print_signal_breakdown(df: pd.DataFrame, *, title: str) -> None:
    print(f"\n--- Per-signal breakdown ({title}) ---")
    if df.empty:
        print("  (no trades)")
        return
    header = f"{'Signal':<18} {'Trades':>7} {'Win%':>8} {'Avg PnL%':>10} {'Sum PnL%':>10}"
    print(header)
    print("-" * len(header))
    for _, row in df.iterrows():
        print(
            f"{row['signal_type']:<18} "
            f"{int(row['trades']):>7d} "
            f"{row['win_rate_pct']:>7.2f}% "
            f"{row['avg_pnl_pct']:>9.2f}% "
            f"{row['total_pnl_pct']:>9.2f}%"
        )


def load_v5_data(universe: dict[str, str], days: int) -> dict[str, pd.DataFrame]:
    print(f"\nLoading 15m OHLCV from Alpaca ({len(universe)} symbols)...")
    data: dict[str, pd.DataFrame] = {}
    for label, alpaca_symbol in universe.items():
        df = load_coin_data_15m(alpaca_symbol, days=days)
        if df is None or df.empty:
            print(f"  WARNING: skipping {label}")
            continue
        data[label] = df
    return data


def run_v4_hourly_baseline(data_hourly: dict[str, pd.DataFrame]) -> dict:
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
    set_entry_params(DROP_PCT, RSI_MAX_V4, RSI_MIN_V4)
    v4_data = {k: v for k, v in data_hourly.items() if k in UNIVERSE_V4}
    _, _, _, _, metrics = run_version_backtest(v4_data, v4_config)
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Vol Backtest v5 (15m + breakout)")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS, help="Lookback days")
    parser.add_argument(
        "--no-expanded-universe",
        action="store_true",
        help="Use config.base_crypto_universe() only (ignore expanded flag)",
    )
    args = parser.parse_args()

    _load_env()
    resolve_virtual_equity()

    expanded = not args.no_expanded_universe and config.effective_crypto_universe_expanded()
    v5_universe = build_v5_universe(expanded=expanded)
    print(
        f"\nv5 universe: {len(v5_universe)} symbols | "
        f"PAPER_CRYPTO_UNIVERSE_EXPANDED={'on' if expanded else 'off'}"
    )
    print(
        "v5 filters: SPY gate, UTC hours 13-16 & 18-22, 48h loss cooldown, "
        f"{POSITION_PCT:.0%}/trade max {MAX_POSITIONS} slots, "
        f"TP +{TAKE_PROFIT_PCT:.1%} SL -{STOP_LOSS_PCT:.1%} timeout 48h"
    )

    # --- v4 hourly baseline (v4 coin set) ---
    print("\n" + "=" * 72)
    print("v4 baseline — hourly mean-reversion (v4 universe, v4 filters)")
    print("=" * 72)
    hourly_data: dict[str, pd.DataFrame] = {}

    for label, sym in UNIVERSE_V3.items():
        if label not in UNIVERSE_V4:
            continue
        df = load_coin_data(sym, days=args.days)
        if df is not None and not df.empty:
            hourly_data[label] = df
    v4_metrics = run_v4_hourly_baseline(hourly_data)

    # --- v5 data ---
    v5_data = load_v5_data(v5_universe, days=args.days)
    if not v5_data:
        raise SystemExit("No v5 coins with sufficient 15m Alpaca history.")

    print("\n" + "=" * 72)
    print("v5 MR-only — 15m bars, mean-reversion entries only")
    print("=" * 72)
    mr_trades, mr_equity, _, _ = run_backtest_v5(
        v5_data, v5_universe, allow_breakout=False, label="v5_mr_only"
    )
    mr_metrics = compute_metrics_v5(mr_trades, mr_equity)
    mr_sig_df = signal_type_breakdown(mr_trades)
    print_signal_breakdown(mr_sig_df, title="v5 MR-only")

    print("\n" + "=" * 72)
    print("v5 MR+Breakout — 15m bars, mean-reversion + breakout (one position per coin)")
    print("=" * 72)
    both_trades, both_equity, _, filter_skips = run_backtest_v5(
        v5_data, v5_universe, allow_breakout=True, label="v5_mr_breakout"
    )
    both_metrics = compute_metrics_v5(both_trades, both_equity)
    both_sig_df = signal_type_breakdown(both_trades)
    print_signal_breakdown(both_sig_df, title="v5 MR+Breakout")

    print_comparison_table(
        [
            ("v4 hourly MR", v4_metrics),
            ("v5 MR-only (15m)", mr_metrics),
            ("v5 MR+Breakout (15m)", both_metrics),
        ]
    )

    print("\n--- v5 MR+Breakout filter skips ---")
    print(f"  SPY gate:          {filter_skips.spy_gate:>5d}")
    print(f"  Hour filter:       {filter_skips.hour_filter:>5d}")
    print(f"  Loss cooldown:     {filter_skips.loss_cooldown:>5d}")
    print(f"  Max positions:     {filter_skips.max_positions:>5d}")
    print(f"  Insufficient cash: {filter_skips.insufficient_cash:>5d}")

    save_trades_csv_v5(both_trades, OUT_CSV)

    print("\n--- Notes ---")
    print("  v4 uses WIF/BONK/RENDER/SOL/AVAX hourly universe (production v4 sleeve).")
    print("  v5 uses config crypto universe on 15m bars; breakout adds momentum entries.")
    print("  Live $300 Profile A: crypto vol sleeve is paper-only — research script only.")


if __name__ == "__main__":
    main()
