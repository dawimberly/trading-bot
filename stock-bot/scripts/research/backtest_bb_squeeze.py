#!/usr/bin/env python3
"""BB Squeeze + ADX trend filter backtest — RENDER/USD (research only).

Standalone research script. Does not modify production modules.

Strategy (1h execution, 4h ADX/MA50 filter):
  - Bollinger Bands(20, 2.0) + Keltner Channel(20, 1.5x ATR) on 1h
  - Squeeze = BB inside KC (compression)
  - Long: squeeze in last 5 bars, close breaks above KC upper,
          4h ADX(14) > 19, 4h close > 4h MA50
  - Short: squeeze in last 5 bars, close breaks below KC lower,
           4h ADX(14) > 19, 4h close < 4h MA50
  - Stop 2.5% / take profit 5% / max hold 48h
  - Size 5% equity, max 2 concurrent

Compares vs current v4 mean-reversion on RENDER/USD.

Run:
  python scripts/research/backtest_bb_squeeze.py
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

OUT_CSV = Path(__file__).resolve().parent / "bb_squeeze_results.csv"
SYMBOL = "RENDER/USD"
LOOKBACK_DAYS = 180
VIRTUAL_EQUITY = 100_000.0

# BB / KC / ADX
BB_PERIOD = 20
BB_STD = 2.0
KC_PERIOD = 20
KC_ATR_MULT = 1.5
ADX_PERIOD = 14
ADX_MIN = 19.0
MA50_PERIOD = 50
SQUEEZE_LOOKBACK = 5

# Trade management
STOP_LOSS_PCT = 0.025
TAKE_PROFIT_PCT = 0.05
MAX_HOLD_BARS = 48  # 1h bars
POSITION_PCT = 0.05
MAX_POSITIONS = 2
FEE_PCT = 0.0025  # match v4 crypto taker fee per leg
SHARPE_SCALE = np.sqrt(365 * 24)


@dataclass
class Position:
    side: str  # "long" | "short"
    entry_date: pd.Timestamp
    entry_idx: int
    entry_price: float
    shares: float
    cash_in: float


@dataclass
class Trade:
    date: pd.Timestamp
    side: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str
    hold_hours: float


def _load_env() -> None:
    env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if env_override and os.path.isfile(env_override):
        load_dotenv(env_override, override=True)
    else:
        # Prefer portal paper book keys when present
        portal = (
            ROOT
            / "data"
            / "portal"
            / "users"
            / "dawimberly"
            / "books"
            / "alpaca_paper"
            / ".env"
        )
        if portal.is_file():
            load_dotenv(portal, override=False)
        load_dotenv(find_dotenv(), override=False)


def _paper_alpaca_keys() -> tuple[str, str]:
    key = (
        os.getenv("PAPER_APCA_API_KEY_ID", "").strip()
        or os.getenv("APCA_API_KEY_ID", "").strip()
    )
    secret = (
        os.getenv("PAPER_APCA_API_SECRET_KEY", "").strip()
        or os.getenv("APCA_API_SECRET_KEY", "").strip()
    )
    if not key or not secret:
        raise ValueError(
            "Missing PAPER_APCA_API_KEY_ID / PAPER_APCA_API_SECRET_KEY "
            "(or APCA_* fallbacks) in environment."
        )
    return key, secret


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


def fetch_crypto_bars(symbol: str, days: int, *, hours: int) -> pd.DataFrame:
    api_key, secret_key = _paper_alpaca_keys()
    client = CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days + 10)  # buffer for warmup
    tf = TimeFrame(hours, TimeFrameUnit.Hour)
    try:
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
        )
        bars = client.get_crypto_bars(request)
    except Exception as exc:
        print(f"  {symbol} {hours}h: Alpaca API error — {exc}")
        return pd.DataFrame()
    if bars is None or bars.df is None or bars.df.empty:
        print(f"  {symbol} {hours}h: no bars returned")
        return pd.DataFrame()
    df = _normalize_ohlcv(bars.df.reset_index())
    cutoff = df["Date"].max() - pd.Timedelta(days=days)
    df = df[df["Date"] >= cutoff].reset_index(drop=True)
    print(f"  {symbol}: fetched {len(df)} x {hours}h bars")
    return df


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    ranges = pd.concat(
        [
            (high - low).abs(),
            (high - prev).abs(),
            (low - prev).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = ADX_PERIOD
) -> pd.Series:
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = _true_range(high, low, close)
    atr = _wilder_smooth(tr, period)
    plus_di = 100 * _wilder_smooth(pd.Series(plus_dm, index=close.index), period) / atr.replace(
        0, np.nan
    )
    minus_di = 100 * _wilder_smooth(
        pd.Series(minus_dm, index=close.index), period
    ) / atr.replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    return _wilder_smooth(dx, period)


def enrich_1h(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> pd.DataFrame:
    out = df_1h.copy()
    mid = out["Close"].rolling(BB_PERIOD).mean()
    std = out["Close"].rolling(BB_PERIOD).std(ddof=0)
    out["bb_mid"] = mid
    out["bb_upper"] = mid + BB_STD * std
    out["bb_lower"] = mid - BB_STD * std

    tr = _true_range(out["High"], out["Low"], out["Close"])
    atr = _wilder_smooth(tr, KC_PERIOD)
    kc_mid = out["Close"].rolling(KC_PERIOD).mean()
    out["kc_mid"] = kc_mid
    out["kc_upper"] = kc_mid + KC_ATR_MULT * atr
    out["kc_lower"] = kc_mid - KC_ATR_MULT * atr

    out["squeeze_on"] = (out["bb_upper"] < out["kc_upper"]) & (
        out["bb_lower"] > out["kc_lower"]
    )
    # Any squeeze in the last SQUEEZE_LOOKBACK bars (including current)
    out["squeeze_recent"] = (
        out["squeeze_on"].astype(float).rolling(SQUEEZE_LOOKBACK, min_periods=1).max().astype(bool)
    )

    # Breakout: close crosses outside Keltner
    prev_close = out["Close"].shift(1)
    out["break_up"] = (prev_close <= out["kc_upper"].shift(1)) & (out["Close"] > out["kc_upper"])
    out["break_down"] = (prev_close >= out["kc_lower"].shift(1)) & (out["Close"] < out["kc_lower"])

    # 4h ADX + MA50, aligned onto 1h via asof merge
    h4 = df_4h.copy()
    h4["adx14"] = compute_adx(h4["High"], h4["Low"], h4["Close"], ADX_PERIOD)
    h4["ma50"] = h4["Close"].rolling(MA50_PERIOD).mean()
    h4 = h4.rename(columns={"Close": "close_4h", "Date": "Date"})
    align = h4[["Date", "adx14", "ma50", "close_4h"]].sort_values("Date")
    out = pd.merge_asof(
        out.sort_values("Date"),
        align,
        on="Date",
        direction="backward",
    )
    out["adx_ok"] = out["adx14"] > ADX_MIN
    out["trend_long"] = out["close_4h"] > out["ma50"]
    out["trend_short"] = out["close_4h"] < out["ma50"]

    out["long_signal"] = (
        out["squeeze_recent"] & out["break_up"] & out["adx_ok"] & out["trend_long"]
    )
    out["short_signal"] = (
        out["squeeze_recent"] & out["break_down"] & out["adx_ok"] & out["trend_short"]
    )
    return out.reset_index(drop=True)


def _exit_check(pos: Position, row: pd.Series, bar_idx: int) -> tuple[bool, str, float]:
    px = float(row["Close"])
    held = bar_idx - pos.entry_idx
    if pos.side == "long":
        stop = pos.entry_price * (1 - STOP_LOSS_PCT)
        target = pos.entry_price * (1 + TAKE_PROFIT_PCT)
        if px <= stop:
            return True, "stop", px
        if px >= target:
            return True, "take_profit", px
    else:
        stop = pos.entry_price * (1 + STOP_LOSS_PCT)
        target = pos.entry_price * (1 - TAKE_PROFIT_PCT)
        if px >= stop:
            return True, "stop", px
        if px <= target:
            return True, "take_profit", px
    if held >= MAX_HOLD_BARS:
        return True, "timeout", px
    return False, "", px


def _pnl_pct(side: str, entry: float, exit_px: float) -> float:
    if side == "long":
        gross = (exit_px / entry) - 1.0
    else:
        gross = (entry / exit_px) - 1.0
    # Round-trip fees
    return gross - 2 * FEE_PCT


def run_bb_squeeze(df: pd.DataFrame) -> tuple[list[Trade], pd.Series]:
    warmup = max(BB_PERIOD, KC_PERIOD, MA50_PERIOD * 4)  # MA50 on 4h ≈ 200 1h bars
    equity = VIRTUAL_EQUITY
    cash = VIRTUAL_EQUITY
    positions: list[Position] = []
    trades: list[Trade] = []
    equity_curve: list[tuple[pd.Timestamp, float]] = []

    for i in range(warmup, len(df)):
        row = df.iloc[i]
        ts = row["Date"]
        px = float(row["Close"])

        # Exits first
        still: list[Position] = []
        for pos in positions:
            done, reason, exit_px = _exit_check(pos, row, i)
            if done:
                pnl = _pnl_pct(pos.side, pos.entry_price, exit_px)
                proceeds = pos.cash_in * (1.0 + pnl)
                cash += proceeds
                trades.append(
                    Trade(
                        date=ts,
                        side=pos.side,
                        entry_price=pos.entry_price,
                        exit_price=exit_px,
                        pnl_pct=pnl * 100.0,
                        exit_reason=reason,
                        hold_hours=float(i - pos.entry_idx),
                    )
                )
            else:
                still.append(pos)
        positions = still

        # Mark equity
        mtm = cash
        for pos in positions:
            if pos.side == "long":
                mtm += pos.shares * px
            else:
                # Short: cash_in already reserved; mark PnL vs entry
                mtm += pos.cash_in * (1.0 + (pos.entry_price / px - 1.0))
        equity = mtm
        equity_curve.append((ts, equity))

        # Entries
        if len(positions) >= MAX_POSITIONS:
            continue
        long_sig = bool(row.get("long_signal"))
        short_sig = bool(row.get("short_signal"))
        if not long_sig and not short_sig:
            continue
        # Prefer long if both (rare)
        side = "long" if long_sig else "short"
        notional = equity * POSITION_PCT
        if notional <= 0 or cash < notional * 0.5:
            continue
        shares = notional / px
        cash -= notional
        positions.append(
            Position(
                side=side,
                entry_date=ts,
                entry_idx=i,
                entry_price=px,
                shares=shares,
                cash_in=notional,
            )
        )

    # Force-close remaining
    if positions and len(df):
        last = df.iloc[-1]
        px = float(last["Close"])
        ts = last["Date"]
        for pos in positions:
            pnl = _pnl_pct(pos.side, pos.entry_price, px)
            cash += pos.cash_in * (1.0 + pnl)
            trades.append(
                Trade(
                    date=ts,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    exit_price=px,
                    pnl_pct=pnl * 100.0,
                    exit_reason="eod",
                    hold_hours=float(len(df) - 1 - pos.entry_idx),
                )
            )
        equity_curve.append((ts, cash))

    eq = pd.Series(
        {t: e for t, e in equity_curve},
        dtype=float,
    ).sort_index()
    return trades, eq


def metrics_from_trades(trades: list[Trade], equity: pd.Series) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "total_return_pct": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate_pct": 0.0,
            "avg_hold_hours": 0.0,
        }
    pnls = [t.pnl_pct for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    if equity is not None and len(equity) > 2:
        rets = equity.pct_change().dropna()
        vol = float(rets.std()) if len(rets) else 0.0
        sharpe = float(rets.mean() / vol * SHARPE_SCALE) if vol > 1e-12 else 0.0
        peak = equity.cummax()
        dd = ((equity - peak) / peak * 100.0).min()
        total_ret = (float(equity.iloc[-1]) / float(equity.iloc[0]) - 1.0) * 100.0
    else:
        # Fallback: compound trade returns
        wealth = 1.0
        for p in pnls:
            wealth *= 1.0 + p / 100.0
        total_ret = (wealth - 1.0) * 100.0
        sharpe = 0.0
        dd = 0.0
    return {
        "trades": n,
        "total_return_pct": float(total_ret),
        "sharpe": float(sharpe),
        "max_drawdown_pct": float(dd) if equity is not None and len(equity) > 2 else 0.0,
        "win_rate_pct": 100.0 * wins / n,
        "avg_hold_hours": float(np.mean([t.hold_hours for t in trades])),
    }


def run_v4_render_baseline(days: int) -> dict:
    """Reuse production v4 MR backtest on RENDER-only for apples-to-apples compare."""
    from backtest_crypto_vol import (
        UNIVERSE_RENDER_ONLY,
        BacktestConfig,
        DROP_PCT,
        RSI_MAX_V4,
        RSI_MIN_V4,
        compute_metrics,
        load_coin_data,
        resolve_virtual_equity,
        run_backtest,
        set_entry_params,
    )

    resolve_virtual_equity()
    set_entry_params(DROP_PCT, RSI_MAX_V4, RSI_MIN_V4)
    frames: dict[str, pd.DataFrame] = {}
    for label, sym in UNIVERSE_RENDER_ONLY.items():
        df = load_coin_data(sym, days=days)
        if df is not None and not df.empty:
            frames[label] = df
    if not frames:
        print("  v4 MR: no RENDER data — skipping baseline")
        return {
            "trades": 0,
            "total_return_pct": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate_pct": 0.0,
            "avg_hold_hours": 0.0,
        }
    cfg = BacktestConfig(
        label="v4 MR (RENDER)",
        universe=UNIVERSE_RENDER_ONLY,
        drop_pct=DROP_PCT,
        rsi_min=RSI_MIN_V4,
        rsi_max=RSI_MAX_V4,
        spy_gate=True,
        hour_filter=True,
        loss_cooldown=True,
        allow_relaxed_retry=False,
    )
    trades, _eq, _signals, _skips = run_backtest(frames, cfg)
    metrics = compute_metrics(trades, _eq)
    avg_hold = float(np.mean([t.hold_hours for t in trades])) if trades else 0.0
    return {
        "trades": int(metrics.get("total_trades") or 0),
        "total_return_pct": float(metrics.get("total_return_pct") or 0.0),
        "sharpe": float(metrics.get("sharpe") or 0.0),
        "max_drawdown_pct": float(metrics.get("max_drawdown_pct") or 0.0),
        "win_rate_pct": float(metrics.get("win_rate_pct") or 0.0),
        "avg_hold_hours": avg_hold,
    }


def print_comparison(v4: dict, squeeze: dict) -> None:
    print("\n=== RENDER/USD — v4 Mean Reversion vs BB Squeeze + ADX ===")
    header = (
        f"{'Metric':<18} {'v4 MR (RENDER)':>16} {'BB Squeeze':>14}"
    )
    print(header)
    print("-" * len(header))
    rows = [
        ("Trades", "trades", "d"),
        ("Return", "total_return_pct", ".2f"),
        ("Sharpe", "sharpe", ".2f"),
        ("Max DD", "max_drawdown_pct", ".2f"),
        ("Win rate", "win_rate_pct", ".2f"),
        ("Avg hold (hours)", "avg_hold_hours", ".1f"),
    ]
    for label, key, fmt in rows:
        a, b = v4.get(key, 0), squeeze.get(key, 0)
        if fmt == "d":
            print(f"{label:<18} {int(a):>16d} {int(b):>14d}")
        elif key in ("total_return_pct", "max_drawdown_pct", "win_rate_pct"):
            print(f"{label:<18} {a:>15.2f}% {b:>13.2f}%")
        else:
            print(f"{label:<18} {a:>16.2f} {b:>14.2f}")


def save_results(v4: dict, squeeze: dict, path: Path) -> None:
    rows = [
        {
            "strategy": "v4_MR_RENDER",
            "trades": v4["trades"],
            "return_pct": round(v4["total_return_pct"], 4),
            "sharpe": round(v4["sharpe"], 4),
            "max_dd_pct": round(v4["max_drawdown_pct"], 4),
            "win_rate_pct": round(v4["win_rate_pct"], 4),
            "avg_hold_hours": round(v4["avg_hold_hours"], 4),
        },
        {
            "strategy": "bb_squeeze_adx",
            "trades": squeeze["trades"],
            "return_pct": round(squeeze["total_return_pct"], 4),
            "sharpe": round(squeeze["sharpe"], 4),
            "max_dd_pct": round(squeeze["max_drawdown_pct"], 4),
            "win_rate_pct": round(squeeze["win_rate_pct"], 4),
            "avg_hold_hours": round(squeeze["avg_hold_hours"], 4),
        },
    ]
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"\nSaved: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="BB Squeeze + ADX backtest on RENDER/USD")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    args = parser.parse_args()

    print("=== BB Squeeze + ADX — RENDER/USD (research) ===")
    print(f"Lookback: {args.days}d | 1h execution + 4h ADX/MA50 filter")
    print(
        f"BB({BB_PERIOD},{BB_STD}) KC({KC_PERIOD},{KC_ATR_MULT}xATR) "
        f"ADX>{ADX_MIN} squeeze_lookback={SQUEEZE_LOOKBACK}"
    )
    print(
        f"Stops: SL {STOP_LOSS_PCT:.1%} / TP {TAKE_PROFIT_PCT:.1%} / "
        f"max hold {MAX_HOLD_BARS}h | size {POSITION_PCT:.0%} max {MAX_POSITIONS}"
    )

    _load_env()

    print("\nFetching Alpaca crypto bars...")
    df_1h = fetch_crypto_bars(SYMBOL, args.days, hours=1)
    df_4h = fetch_crypto_bars(SYMBOL, args.days + 40, hours=4)
    if df_1h.empty or df_4h.empty:
        print("ERROR: insufficient bar data")
        return 1

    # Align 4h to cover 1h history
    df_4h = df_4h[df_4h["Date"] <= df_1h["Date"].max()].reset_index(drop=True)
    enriched = enrich_1h(df_1h, df_4h)
    n_sq = int(enriched["squeeze_on"].fillna(False).sum())
    n_long = int(enriched["long_signal"].fillna(False).sum())
    n_short = int(enriched["short_signal"].fillna(False).sum())
    print(f"\nSignals: squeeze bars={n_sq} | long={n_long} | short={n_short}")

    trades, equity = run_bb_squeeze(enriched)
    squeeze_m = metrics_from_trades(trades, equity)
    print(f"BB Squeeze trades: {len(trades)}")
    if trades:
        by_side = {}
        for t in trades:
            by_side.setdefault(t.side, []).append(t)
        for side, rows in by_side.items():
            print(
                f"  {side}: {len(rows)} trades, "
                f"avg pnl {np.mean([t.pnl_pct for t in rows]):+.2f}%, "
                f"avg hold {np.mean([t.hold_hours for t in rows]):.1f}h"
            )
        reasons: dict[str, int] = {}
        for t in trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print("  exits:", reasons)

    print("\nRunning v4 mean-reversion baseline (RENDER-only)...")
    v4_m = run_v4_render_baseline(args.days)

    print_comparison(v4_m, squeeze_m)
    save_results(v4_m, squeeze_m, OUT_CSV)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
