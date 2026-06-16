"""Improve crypto mean-reversion sleeve v3 — softer filters vs v2.

Standalone research — does not modify production modules.

v3 adjustments:
  1. Volume > 1.2× 20-bar avg (last 15m of hour)
  2. Momentum: 2 of 3 rising closes (not 3/3)
  3. Sizing: RSI 34-41 → 7%; RSI 30-34 or 41-45 → 5%; else 3.5%
  4. Per-coin tuning for WIF, BONK, RENDER, SOL, AVAX

Run:
  python scripts/research/improve_crypto_sleeve_v3.py
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

from backtest_crypto_vol import (  # noqa: E402
    DROP_PCT,
    FEE_PCT,
    FOUR_H_BARS,
    BacktestConfig,
    FilterSkipCounts,
    LOSS_COOLDOWN_HOURS,
    MAX_POSITIONS,
    POSITION_PCT,
    Position,
    RSI_MAX_V4,
    RSI_MIN_V4,
    RSI_PERIOD,
    SHARPE_SCALE,
    SMA_PERIOD,
    SPY_GATE_PCT,
    UNIVERSE_V4,
    VIRTUAL_EQUITY,
    check_exit,
    enrich,
    hour_allowed_utc,
    load_coin_data,
    load_spy_daily_returns,
    resolve_virtual_equity,
    run_backtest,
    set_entry_params,
)

OUT_CSV = Path(__file__).resolve().parent / "crypto_sleeve_v3_results.csv"
DEFAULT_LOOKBACK_DAYS = 365
VARIANT_V3 = "vImproved_v3"

DEFAULT_TUNING = {"drop_pct": DROP_PCT, "rsi_min": RSI_MIN_V4, "rsi_max": RSI_MAX_V4}

COIN_TUNING: dict[str, dict[str, float]] = {
    "WIF/USD": {"drop_pct": -0.035, "rsi_min": 30, "rsi_max": 44},
    "BONK/USD": {"drop_pct": -0.040, "rsi_min": 28, "rsi_max": 45},
    "RENDER/USD": {"drop_pct": -0.032, "rsi_min": 31, "rsi_max": 43},
    "SOL/USD": {"drop_pct": -0.028, "rsi_min": 33, "rsi_max": 41},
    "AVAX/USD": {"drop_pct": -0.028, "rsi_min": 33, "rsi_max": 41},
}

VOL_CONFIRM_MULT = 1.2
VOL_MA_PERIOD = 20


@dataclass
class TradeRow:
    date: pd.Timestamp
    coin: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str
    hold_hours: float
    variant: str
    rsi_at_entry: float
    position_pct: float


@dataclass
class PositionRow:
    coin: str
    entry_date: pd.Timestamp
    entry_idx: int
    entry_price: float
    shares: float
    cash_in: float
    rsi_at_entry: float
    position_pct: float


@dataclass
class ImprovedFilterSkips(FilterSkipCounts):
    volume_filter: int = 0
    momentum_filter: int = 0


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


def coin_tuning(coin: str) -> dict[str, float]:
    return {**DEFAULT_TUNING, **COIN_TUNING.get(coin, {})}


def conviction_position_pct(rsi: float) -> float:
    """v3 sizing: 34-41 → 7%; 30-34 or 41-45 → 5%; else 3.5%."""
    if 34 <= rsi <= 41:
        return 0.07
    if (30 <= rsi < 34) or (41 < rsi <= 45):
        return POSITION_PCT
    return 0.035


def _normalize_15m(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [str(c[0]) for c in out.columns]
    rename: dict[str, str] = {}
    for col in out.columns:
        low = str(col).lower()
        if low == "close":
            rename[col] = "Close"
        elif low == "volume":
            rename[col] = "Volume"
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
    out["Date"] = pd.to_datetime(out["Date"], utc=True).dt.tz_localize(None)
    out = out.sort_values("Date").drop_duplicates("Date", keep="last")
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce").fillna(0.0)
    return out.reset_index(drop=True)


def fetch_alpaca_15m(symbol: str, days: int) -> pd.DataFrame:
    api_key, secret_key = _paper_alpaca_keys()
    client = CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days)
    try:
        bars = client.get_crypto_bars(
            CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                start=start,
                end=end,
            )
        )
    except Exception as exc:
        print(f"  {symbol} 15m: API error — {exc}")
        return pd.DataFrame()
    if bars is None or bars.df is None or bars.df.empty:
        return pd.DataFrame()
    df = _normalize_15m(bars.df.reset_index())
    df["vol_ma20"] = df["Volume"].rolling(VOL_MA_PERIOD).mean()
    return df


def build_volume_confirmation_map(
    hourly_df: pd.DataFrame, bars_15m: pd.DataFrame
) -> dict[pd.Timestamp, bool]:
    confirmed: dict[pd.Timestamp, bool] = {}
    if bars_15m.empty:
        return {ts: False for ts in hourly_df["Date"]}
    for ht in hourly_df["Date"]:
        hour_floor = ht.floor("h")
        mask = (bars_15m["Date"] > hour_floor) & (bars_15m["Date"] <= ht)
        chunk = bars_15m.loc[mask]
        if chunk.empty:
            mask = (bars_15m["Date"] > ht - pd.Timedelta(hours=1)) & (
                bars_15m["Date"] <= ht
            )
            chunk = bars_15m.loc[mask]
        if chunk.empty:
            confirmed[ht] = False
            continue
        last = chunk.iloc[-1]
        vma = last.get("vol_ma20")
        if pd.isna(vma) or float(vma) <= 0:
            confirmed[ht] = False
        else:
            confirmed[ht] = float(last["Volume"]) > VOL_CONFIRM_MULT * float(vma)
    return confirmed


def momentum_ok(df: pd.DataFrame, idx: int) -> bool:
    """2 of 3 rising closes: at least 2 upward hourly transitions in last 4 bars.

    v2 required c0 > c1 > c2 (both transitions). v3 allows any 2 of the 3
    transitions (c0>c1, c1>c2, c2>c3) — genuinely softer.
    """
    if idx < 3:
        return False
    c0 = float(df.loc[idx, "Close"])
    c1 = float(df.loc[idx - 1, "Close"])
    c2 = float(df.loc[idx - 2, "Close"])
    c3 = float(df.loc[idx - 3, "Close"])
    rising = int(c0 > c1) + int(c1 > c2) + int(c2 > c3)
    return rising >= 2


def run_improved_v3_backtest(
    hourly_data: dict[str, pd.DataFrame],
    bars_15m: dict[str, pd.DataFrame],
    universe: dict[str, str],
) -> tuple[list[TradeRow], pd.Series, ImprovedFilterSkips]:
    frames = {
        coin: enrich(df)
        for coin, df in hourly_data.items()
        if coin in universe and not df.empty
    }
    vol_maps = {
        coin: build_volume_confirmation_map(frames[coin], bars_15m.get(coin, pd.DataFrame()))
        for coin in frames
    }
    spy_returns = load_spy_daily_returns()
    warmup = max(SMA_PERIOD, RSI_PERIOD, FOUR_H_BARS)
    date_index = sorted({d for df in frames.values() for d in df["Date"]})
    date_to_idx = {coin: dict(zip(df["Date"], df.index)) for coin, df in frames.items()}

    cash = VIRTUAL_EQUITY
    positions: dict[str, PositionRow] = {}
    trades: list[TradeRow] = []
    equity_rows: list[tuple[pd.Timestamp, float]] = []
    skips = ImprovedFilterSkips()
    cooldown_until: dict[str, pd.Timestamp] = {}

    for dt in date_index:
        for coin in list(positions.keys()):
            if coin not in frames or dt not in date_to_idx[coin]:
                continue
            idx = date_to_idx[coin][dt]
            if idx <= positions[coin].entry_idx:
                continue
            row = frames[coin].loc[idx]
            hit = check_exit(
                Position(
                    coin=positions[coin].coin,
                    entry_date=positions[coin].entry_date,
                    entry_idx=positions[coin].entry_idx,
                    entry_price=positions[coin].entry_price,
                    shares=positions[coin].shares,
                    cash_in=positions[coin].cash_in,
                ),
                idx,
                row,
            )
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
                TradeRow(
                    date=dt,
                    coin=coin,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    exit_reason=reason,
                    hold_hours=hold_hours,
                    variant=VARIANT_V3,
                    rsi_at_entry=pos.rsi_at_entry,
                    position_pct=pos.position_pct,
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
            tune = coin_tuning(coin)
            rsi = float(row["rsi14"]) if not pd.isna(row["rsi14"]) else float("nan")
            drop_ok = (
                not pd.isna(row["ret_4h"]) and float(row["ret_4h"]) < tune["drop_pct"]
            )
            rsi_ok = not pd.isna(rsi) and tune["rsi_min"] <= rsi <= tune["rsi_max"]
            sma_ok = not pd.isna(row["sma10"]) and float(row["Close"]) < float(row["sma10"])
            if not (drop_ok and rsi_ok and sma_ok):
                continue

            if not vol_maps[coin].get(dt, False):
                skips.volume_filter += 1
                continue
            if not momentum_ok(df, idx):
                skips.momentum_filter += 1
                continue
            if not hour_allowed_utc(dt):
                skips.hour_filter += 1
                continue
            if spy_returns is not None:
                spy_ret = spy_returns.get(pd.Timestamp(dt).normalize(), np.nan)
                if not pd.isna(spy_ret) and spy_ret < SPY_GATE_PCT:
                    skips.spy_gate += 1
                    continue
            if coin in cooldown_until and dt < cooldown_until[coin]:
                skips.loss_cooldown += 1
                continue
            if len(positions) >= MAX_POSITIONS:
                skips.max_positions += 1
                continue

            pct = conviction_position_pct(rsi)
            notional = mtm * pct
            cost = notional * (1 + FEE_PCT)
            if cost > cash or notional < 1:
                skips.insufficient_cash += 1
                continue

            entry_price = float(row["Close"])
            shares = notional / entry_price
            cash -= cost
            positions[coin] = PositionRow(
                coin=coin,
                entry_date=dt,
                entry_idx=idx,
                entry_price=entry_price,
                shares=shares,
                cash_in=cost,
                rsi_at_entry=rsi,
                position_pct=pct,
            )

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
            TradeRow(
                date=dt,
                coin=coin,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                pnl_pct=pnl_pct,
                exit_reason="eod_liquidation",
                hold_hours=hold_hours,
                variant=VARIANT_V3,
                rsi_at_entry=pos.rsi_at_entry,
                position_pct=pos.position_pct,
            )
        )

    equity = pd.Series(
        [e for _, e in equity_rows],
        index=pd.DatetimeIndex([d for d, _ in equity_rows]),
        name="equity",
    )
    return trades, equity, skips


def run_current_backtest(
    hourly_data: dict[str, pd.DataFrame],
) -> tuple[list[TradeRow], pd.Series]:
    set_entry_params(DROP_PCT, RSI_MAX_V4, RSI_MIN_V4)
    cfg = BacktestConfig(
        label="vCurrent",
        universe=UNIVERSE_V4,
        drop_pct=DROP_PCT,
        rsi_min=RSI_MIN_V4,
        rsi_max=RSI_MAX_V4,
        spy_gate=True,
        hour_filter=True,
        loss_cooldown=True,
        allow_relaxed_retry=False,
    )
    subset = {k: v for k, v in hourly_data.items() if k in cfg.universe}
    raw_trades, equity, _, _ = run_backtest(subset, cfg)
    trades = [
        TradeRow(
            date=t.date,
            coin=t.coin,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            pnl_pct=t.pnl_pct,
            exit_reason=t.exit_reason,
            hold_hours=t.hold_hours,
            variant="vCurrent",
            rsi_at_entry=float("nan"),
            position_pct=POSITION_PCT,
        )
        for t in raw_trades
    ]
    return trades, equity


def compute_full_metrics(trades: list[TradeRow], equity: pd.Series) -> dict:
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
    avg_pnl = (sum(t.pnl_pct for t in trades) / len(trades)) if trades else 0.0
    return {
        "total_return_pct": round(total_ret, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "win_rate_pct": round(win_rate, 2),
        "total_trades": len(trades),
        "avg_pnl_pct": round(avg_pnl, 2),
    }


def per_coin_table(trades: list[TradeRow]) -> pd.DataFrame:
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


def print_comparison(current: dict, v3: dict, *, days: int) -> None:
    print(f"\n=== A/B Comparison ({days}d hourly mean-reversion) ===")
    header = (
        f"{'Variant':<16} {'Return':>10} {'Sharpe':>8} {'Max DD':>9} "
        f"{'Win Rate':>10} {'Trades':>8} {'Avg PnL%':>9}"
    )
    print(header)
    print("-" * len(header))
    for label, m in (("vCurrent", current), ("vImproved v3", v3)):
        print(
            f"{label:<16} "
            f"{m['total_return_pct']:>9.2f}% "
            f"{m['sharpe']:>8.2f} "
            f"{m['max_drawdown_pct']:>8.2f}% "
            f"{m['win_rate_pct']:>9.2f}% "
            f"{m['total_trades']:>8d} "
            f"{m['avg_pnl_pct']:>8.2f}%"
        )


def print_per_coin(title: str, table: pd.DataFrame) -> None:
    print(f"\n--- Per-coin breakdown ({title}) ---")
    if table.empty:
        print("  (no trades)")
        return
    hdr = f"{'Coin':<12} {'Trades':>7} {'Win%':>8} {'Avg PnL%':>10} {'Sum PnL%':>10}"
    print(hdr)
    print("-" * len(hdr))
    for _, row in table.iterrows():
        print(
            f"{row['coin']:<12} "
            f"{int(row['trades']):>7d} "
            f"{row['win_rate_pct']:>7.2f}% "
            f"{row['avg_pnl_pct']:>9.2f}% "
            f"{row['total_pnl_pct']:>9.2f}%"
        )


def recommend_paper_config(current: dict, v3: dict) -> None:
    print("\n=== Paper bot recommendation ===")
    sharpe_delta = v3["sharpe"] - current["sharpe"]
    ret_delta = v3["total_return_pct"] - current["total_return_pct"]
    dd_delta = v3["max_drawdown_pct"] - current["max_drawdown_pct"]
    trade_delta = v3["total_trades"] - current["total_trades"]

    if v3["total_trades"] < 15:
        print(
            "KEEP vCurrent — v3 still too selective "
            f"({v3['total_trades']} trades vs {current['total_trades']} vCurrent)."
        )
        print("  Next tweak: volume-only confirm (drop momentum) or 1.15× volume threshold.")
        return

    if (
        v3["sharpe"] >= current["sharpe"] + 0.05
        and v3["total_return_pct"] >= current["total_return_pct"] - 0.25
        and v3["max_drawdown_pct"] >= current["max_drawdown_pct"] - 0.5
    ):
        print("ADOPT v3 on paper crypto vol sleeve (research → staging first):")
        print("  • Volume confirm 1.2× on last 15m of hour")
        print("  • Momentum: 2/3 rising hourly closes")
        print("  • Conviction sizing: RSI 34-41 → 7%, 30-34/41-45 → 5%, else 3.5%")
        print("  • Keep COIN_TUNING per WIF/BONK/RENDER/SOL/AVAX")
        print(
            f"  Edge vs vCurrent: Sharpe {sharpe_delta:+.2f}, return {ret_delta:+.2f}pp, "
            f"MaxDD {dd_delta:+.2f}pp, trades {trade_delta:+d}"
        )
    elif v3["avg_pnl_pct"] > current["avg_pnl_pct"] and v3["win_rate_pct"] >= current["win_rate_pct"]:
        print("PARTIAL ADOPT: wire v3 conviction sizing + coin tuning; keep vCurrent entry filters.")
        print(
            f"  v3 avg trade {v3['avg_pnl_pct']:+.2f}% vs vCurrent {current['avg_pnl_pct']:+.2f}%"
        )
    else:
        print("KEEP vCurrent on paper bot — v3 did not improve risk-adjusted results.")
        print(
            f"  v3: return {v3['total_return_pct']:+.2f}%, Sharpe {v3['sharpe']:.2f}, "
            f"{v3['total_trades']} trades"
        )


def save_csv(trades: list[TradeRow], path: Path) -> None:
    rows = [
        {
            "date": t.date.strftime("%Y-%m-%d %H:%M"),
            "variant": t.variant,
            "coin": t.coin,
            "rsi_at_entry": round(t.rsi_at_entry, 2) if t.rsi_at_entry == t.rsi_at_entry else "",
            "position_pct": round(t.position_pct * 100, 2),
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


def load_datasets(days: int) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    hourly: dict[str, pd.DataFrame] = {}
    bars_15m: dict[str, pd.DataFrame] = {}
    print(f"\nLoading hourly + 15m data ({days}d) for v4 universe...")
    for label, sym in UNIVERSE_V4.items():
        h = load_coin_data(sym, days=days)
        if h is None or h.empty:
            print(f"  WARNING: no hourly data for {label}")
            continue
        hourly[label] = h
        m15 = fetch_alpaca_15m(sym, days=days)
        if not m15.empty:
            bars_15m[label] = m15
            print(f"  {label}: {len(h)} hourly / {len(m15)} 15m bars")
        else:
            print(f"  WARNING: no 15m data for {label}")
    return hourly, bars_15m


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto MR sleeve v3 (softer filters) A/B")
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = parser.parse_args()

    _load_env()
    resolve_virtual_equity()

    print("\n=== Crypto Mean-Reversion Sleeve v3 (research) ===")
    print(f"Lookback: {args.days} days | Universe: v4 ({', '.join(UNIVERSE_V4)})")
    print(
        "v3 filters: vol 1.2× | momentum 2/3 rising | sizing 7%/5%/3.5% | coin tuning"
    )
    print("Shared: SPY gate, UTC hours, 48h cooldown, max 3, TP/SL/48h timeout")

    hourly, bars_15m = load_datasets(args.days)
    if not hourly:
        raise SystemExit("No hourly data loaded.")

    print("\n" + "=" * 72)
    print("vCurrent — original hourly MR (flat 5%, RSI 32-42)")
    print("=" * 72)
    current_trades, current_equity = run_current_backtest(hourly)
    current_metrics = compute_full_metrics(current_trades, current_equity)
    print_per_coin("vCurrent", per_coin_table(current_trades))

    print("\n" + "=" * 72)
    print("vImproved v3 — softer volume/momentum + conviction sizing + coin tuning")
    print("=" * 72)
    v3_trades, v3_equity, skips = run_improved_v3_backtest(hourly, bars_15m, UNIVERSE_V4)
    v3_metrics = compute_full_metrics(v3_trades, v3_equity)
    print_per_coin("vImproved v3", per_coin_table(v3_trades))

    print("\n--- v3 extra filter skips ---")
    print(f"  Volume confirm:    {skips.volume_filter:>5d}")
    print(f"  Momentum filter:   {skips.momentum_filter:>5d}")
    print(f"  SPY gate:          {skips.spy_gate:>5d}")
    print(f"  Hour filter:       {skips.hour_filter:>5d}")
    print(f"  Loss cooldown:     {skips.loss_cooldown:>5d}")
    print(f"  Max positions:     {skips.max_positions:>5d}")

    print_comparison(current_metrics, v3_metrics, days=args.days)
    recommend_paper_config(current_metrics, v3_metrics)
    save_csv(current_trades + v3_trades, OUT_CSV)


if __name__ == "__main__":
    main()
