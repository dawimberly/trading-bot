"""Correlate crypto vol backtest trades with timing, market regime, and coin profile.

Reads crypto_vol_backtest_results.csv from project root, enriches with SPY daily
returns from market_data.db, and optionally RSI at entry (Alpaca hourly bars).

Run from repo root:
  python scripts/analysis/crypto_vol_correlation.py
"""

from __future__ import annotations

import os
import sys
import warnings
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backtest_crypto_vol import compute_rsi, load_spy_daily_returns as _load_spy_series
CSV_PATH = ROOT / "crypto_vol_backtest_results.csv"
REPORT_PATH = Path(__file__).resolve().parent / "crypto_vol_correlation_report.txt"
CHARTS_PATH = Path(__file__).resolve().parent / "crypto_vol_charts.html"

RSI_PERIOD = 14
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Alpaca symbol map (display label in CSV → Alpaca symbol)
COIN_ALPACA = {
    "WIF/USD": "WIF/USD",
    "BONK/USD": "BONK/USD",
    "RENDER/USD": "RENDER/USD",
    "ARB/USD": "ARB/USD",
    "SOL/USD": "SOL/USD",
    "AVAX/USD": "AVAX/USD",
}


def _log(buf: StringIO, msg: str = "", end: str = "\n") -> None:
    buf.write(msg + end)


def load_trades() -> pd.DataFrame:
    if not CSV_PATH.is_file():
        raise FileNotFoundError(f"Missing trade log: {CSV_PATH}")
    df = pd.read_csv(CSV_PATH)
    df.columns = [c.strip().lower() for c in df.columns]
    required = {"date", "coin", "pnl_pct", "hold_hours"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing columns: {sorted(missing)}")
    df["entry_ts"] = pd.to_datetime(df["date"], utc=True)
    df["win"] = df["pnl_pct"] > 0
    df["entry_hour_utc"] = df["entry_ts"].dt.hour
    df["entry_dow"] = df["entry_ts"].dt.dayofweek
    df["entry_dow_name"] = df["entry_dow"].map(dict(enumerate(DAY_NAMES)))
    entry_naive = df["entry_ts"].dt.tz_convert("UTC").dt.tz_localize(None)
    df["entry_date"] = entry_naive.dt.normalize()
    df["entry_week"] = entry_naive.dt.to_period("W").astype(str)
    return df


def _try_fetch_coin_hourly(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None:
    """Fetch 1h bars from Alpaca when paper keys are configured."""
    key = os.getenv("PAPER_APCA_API_KEY_ID", "").strip()
    secret = os.getenv("PAPER_APCA_API_SECRET_KEY", "").strip()
    if not key or not secret:
        return None
    try:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from dotenv import find_dotenv, load_dotenv

        env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
        if env_override and os.path.isfile(env_override):
            load_dotenv(env_override, override=True)
        else:
            load_dotenv(find_dotenv())

        client = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
        req = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=start.to_pydatetime().replace(tzinfo=timezone.utc),
            end=(end + pd.Timedelta(hours=1)).to_pydatetime().replace(tzinfo=timezone.utc),
        )
        bars = client.get_crypto_bars(req)
        if bars is None or bars.df is None or bars.df.empty:
            return None
        raw = bars.df.reset_index()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [str(c[0]) for c in raw.columns]
        date_col = next(
            (c for c in raw.columns if str(c).lower() in ("timestamp", "date", "datetime")),
            raw.columns[0],
        )
        close_col = next((c for c in raw.columns if str(c).lower() == "close"), None)
        if close_col is None:
            return None
        out = raw[[date_col, close_col]].rename(columns={date_col: "Date", close_col: "Close"})
        out["Date"] = pd.to_datetime(out["Date"], utc=True).dt.tz_localize(None)
        out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
        out = out.dropna().sort_values("Date").drop_duplicates("Date", keep="last")
        out["rsi14"] = compute_rsi(out["Close"], RSI_PERIOD)
        return out
    except Exception:
        return None


def enrich_rsi(df: pd.DataFrame, buf: StringIO) -> pd.DataFrame:
    if "rsi" in df.columns or "rsi14" in df.columns:
        col = "rsi" if "rsi" in df.columns else "rsi14"
        df["entry_rsi"] = pd.to_numeric(df[col], errors="coerce")
        _log(buf, f"RSI: using column '{col}' from CSV.")
        return df

    _log(buf, "RSI: not present in CSV — attempting Alpaca hourly recompute (backtest logic).")
    if not os.getenv("PAPER_APCA_API_KEY_ID", "").strip():
        _log(buf, "RSI: skipped — PAPER_APCA_API_KEY_ID not set; cannot fetch hourly bars.")
        df["entry_rsi"] = np.nan
        return df

    start = df["entry_ts"].min() - pd.Timedelta(days=5)
    end = df["entry_ts"].max()
    coin_frames: dict[str, pd.DataFrame] = {}
    for coin in df["coin"].unique():
        sym = COIN_ALPACA.get(coin, coin)
        frame = _try_fetch_coin_hourly(sym, start, end)
        if frame is not None and not frame.empty:
            coin_frames[coin] = frame.set_index("Date")["rsi14"]

    if not coin_frames:
        _log(buf, "RSI: skipped — Alpaca fetch failed or returned no bars.")
        df["entry_rsi"] = np.nan
        return df

    rsi_vals = []
    for _, row in df.iterrows():
        series = coin_frames.get(row["coin"])
        if series is None:
            rsi_vals.append(np.nan)
            continue
        ts = row["entry_ts"].tz_localize(None) if row["entry_ts"].tzinfo else row["entry_ts"]
        if ts in series.index:
            rsi_vals.append(float(series.loc[ts]))
        else:
            nearest = series.index.get_indexer([ts], method="nearest")
            rsi_vals.append(float(series.iloc[nearest[0]]) if len(nearest) and nearest[0] >= 0 else np.nan)
    df["entry_rsi"] = rsi_vals
    matched = df["entry_rsi"].notna().sum()
    _log(buf, f"RSI: enriched {matched}/{len(df)} trades from Alpaca hourly bars.")
    return df


def load_spy_daily_returns() -> pd.DataFrame | None:
    series = _load_spy_series()
    if series is None:
        return None
    return series.reset_index()


def classify_spy_return(ret: float) -> str:
    if pd.isna(ret):
        return "unknown"
    if ret > 1.0:
        return "strong_up (>1%)"
    if ret > 0:
        return "mild_up (0-1%)"
    if ret >= -1.0:
        return "flat (-1% to 0%)"
    return "down (<-1%)"


def analysis_win_vs_loss(df: pd.DataFrame, buf: StringIO) -> None:
    _log(buf, "\n" + "=" * 72)
    _log(buf, "ANALYSIS 1 — Win vs Loss profile")
    _log(buf, "=" * 72)

    wins = df[df["win"]]
    losses = df[~df["win"]]
    _log(buf, f"\nTrades: {len(df)} total | {len(wins)} wins | {len(losses)} losses")
    _log(buf, f"Overall win rate: {df['win'].mean() * 100:.2f}%")

    _log(buf, "\n--- Hold time (hours) ---")
    _log(buf, f"  Wins avg:   {wins['hold_hours'].mean():.2f}h")
    _log(buf, f"  Losses avg: {losses['hold_hours'].mean():.2f}h")

    _log(buf, "\n--- Entry hour UTC (mean / median) ---")
    _log(buf, f"  Wins:   mean {wins['entry_hour_utc'].mean():.1f}, median {wins['entry_hour_utc'].median():.0f}")
    _log(buf, f"  Losses: mean {losses['entry_hour_utc'].mean():.1f}, median {losses['entry_hour_utc'].median():.0f}")

    _log(buf, "\n--- Day of week distribution (count) ---")
    for label, subset in [("Wins", wins), ("Losses", losses)]:
        dist = subset["entry_dow_name"].value_counts().reindex(DAY_NAMES, fill_value=0)
        parts = ", ".join(f"{d}={int(dist[d])}" for d in DAY_NAMES)
        _log(buf, f"  {label}: {parts}")

    _log(buf, "\n--- Win rate by coin (sorted best to worst) ---")
    by_coin = (
        df.groupby("coin")
        .agg(trades=("win", "count"), win_rate=("win", "mean"), avg_pnl=("pnl_pct", "mean"))
        .sort_values("win_rate", ascending=False)
    )
    by_coin["win_rate"] = (by_coin["win_rate"] * 100).round(2)
    by_coin["avg_pnl"] = by_coin["avg_pnl"].round(3)
    _log(buf, by_coin.to_string())
    if not by_coin.empty:
        best = by_coin.index[0]
        _log(buf, f"\nHighest win-rate coin: {best} ({by_coin.loc[best, 'win_rate']:.2f}%)")

    if df["entry_rsi"].notna().any():
        _log(buf, "\n--- RSI at entry (RSI-14, backtest method) ---")
        _log(buf, f"  Wins avg RSI:   {wins['entry_rsi'].mean():.2f}")
        _log(buf, f"  Losses avg RSI: {losses['entry_rsi'].mean():.2f}")
    else:
        _log(buf, "\n--- RSI at entry ---")
        _log(buf, "  Not available (see note above).")


def analysis_market_condition(df: pd.DataFrame, spy: pd.DataFrame | None, buf: StringIO) -> pd.DataFrame:
    _log(buf, "\n" + "=" * 72)
    _log(buf, "ANALYSIS 2 — Market condition correlation (SPY daily return on entry date)")
    _log(buf, "=" * 72)

    if spy is None or spy.empty:
        _log(buf, "\nSPY daily data unavailable — ensure market_data.db has SPY_daily (run fetch_data.py --daily).")
        df["market_condition"] = "unknown"
        return df

    merged = df.merge(spy, left_on="entry_date", right_on="Date", how="left")
    merged["market_condition"] = merged["spy_return_pct"].map(classify_spy_return)
    missing = merged["spy_return_pct"].isna().sum()
    if missing:
        _log(buf, f"\nWarning: {missing} trades have no matching SPY daily bar for entry date.")

    order = ["strong_up (>1%)", "mild_up (0-1%)", "flat (-1% to 0%)", "down (<-1%)", "unknown"]
    grp = (
        merged.groupby("market_condition", observed=True)
        .agg(trades=("win", "count"), win_rate=("win", "mean"), avg_pnl=("pnl_pct", "mean"))
        .reindex(order)
        .dropna(how="all")
    )
    grp["win_rate"] = (grp["win_rate"] * 100).round(2)
    grp["avg_pnl"] = grp["avg_pnl"].round(3)
    _log(buf, "\nWin rate by SPY day return bucket:")
    _log(buf, grp.to_string())
    return merged


def analysis_time_of_day(df: pd.DataFrame, buf: StringIO) -> pd.DataFrame:
    _log(buf, "\n" + "=" * 72)
    _log(buf, "ANALYSIS 3 — Time of day edge (UTC hour 0–23)")
    _log(buf, "=" * 72)

    hourly = (
        df.groupby("entry_hour_utc")
        .agg(trades=("win", "count"), win_rate=("win", "mean"), avg_pnl=("pnl_pct", "mean"))
        .reindex(range(24), fill_value=0)
    )
    hourly.loc[hourly["trades"] == 0, ["win_rate", "avg_pnl"]] = np.nan
    hourly["win_rate"] = (hourly["win_rate"] * 100).round(2)
    hourly["avg_pnl"] = hourly["avg_pnl"].round(3)
    _log(buf, "\n" + hourly.to_string())
    active = hourly[hourly["trades"] > 0].copy()
    if not active.empty:
        best_hr = active["win_rate"].idxmax()
        worst_hr = active["win_rate"].idxmin()
        _log(
            buf,
            f"\nBest hour (win rate, n>=1): {int(best_hr):02d}:00 UTC "
            f"({active.loc[best_hr, 'win_rate']:.2f}%, n={int(active.loc[best_hr, 'trades'])})",
        )
        _log(
            buf,
            f"Worst hour: {int(worst_hr):02d}:00 UTC "
            f"({active.loc[worst_hr, 'win_rate']:.2f}%, n={int(active.loc[worst_hr, 'trades'])})",
        )
    return hourly


def analysis_drawdown_clustering(df: pd.DataFrame, buf: StringIO) -> None:
    _log(buf, "\n" + "=" * 72)
    _log(buf, "ANALYSIS 4 — Drawdown clustering (losses in time)")
    _log(buf, "=" * 72)

    losses = df[~df["win"]].copy()
    if losses.empty:
        _log(buf, "\nNo losing trades to cluster.")
        return

    _log(buf, "\n--- Losses by ISO week (top 10) ---")
    week_losses = losses.groupby("entry_week").size().sort_values(ascending=False)
    _log(buf, week_losses.head(10).to_string())

    _log(buf, "\n--- Loss rate by week (% of all trades that week) ---")
    week_all = df.groupby("entry_week").size()
    week_loss_pct = (week_losses / week_all * 100).sort_values(ascending=False)
    _log(buf, week_loss_pct.head(10).round(1).to_string())

    losses_sorted = losses.sort_values("entry_ts")
    gaps = losses_sorted["entry_ts"].diff().dt.total_seconds() / 3600
    clustered = (gaps <= 48).sum()
    _log(buf, f"\nLosses within 48h of prior loss: {int(clustered)} / {len(losses)} ({clustered / len(losses) * 100:.1f}%)")

    if "market_condition" in df.columns:
        _log(buf, "\n--- Loss count by SPY market condition ---")
        loss_by_mkt = losses.groupby("market_condition", observed=True).size()
        _log(buf, loss_by_mkt.sort_values(ascending=False).to_string())


def build_equity_curve(df: pd.DataFrame) -> pd.DataFrame:
    ordered = df.sort_values("entry_ts").reset_index(drop=True)
    ordered["cum_pnl_pct"] = ordered["pnl_pct"].cumsum()
    ordered["equity_index"] = (1 + ordered["pnl_pct"] / 100).cumprod()
    return ordered


def save_charts(df: pd.DataFrame, hourly: pd.DataFrame, by_coin: pd.DataFrame) -> bool:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        try:
            import matplotlib.pyplot as plt

            eq = build_equity_curve(df)
            fig, axes = plt.subplots(3, 1, figsize=(10, 12))
            axes[0].plot(eq["entry_ts"], eq["cum_pnl_pct"])
            axes[0].set_title("Cumulative PnL %")
            axes[0].set_ylabel("Cum PnL %")

            h = hourly[hourly["trades"] > 0]
            axes[1].bar(h.index, h["trades"], color=["green" if wr >= 50 else "red" for wr in h["win_rate"]])
            axes[1].set_title("Trades by entry hour UTC")
            axes[1].set_xlabel("Hour UTC")

            axes[2].bar(by_coin.index, by_coin["win_rate"])
            axes[2].set_title("Win rate by coin (%)")
            axes[2].tick_params(axis="x", rotation=45)
            plt.tight_layout()
            fallback = CHARTS_PATH.with_suffix(".png")
            plt.savefig(fallback, dpi=120)
            plt.close()
            print(f"Plotly unavailable — saved matplotlib chart: {fallback}")
            return False
        except ImportError:
            print("Neither plotly nor matplotlib available — skipping charts.")
            return False

    eq = build_equity_curve(df)
    fig = make_subplots(
        rows=3,
        cols=1,
        subplot_titles=("Equity curve (compounded per-trade)", "Trades by entry hour UTC", "Win rate by coin (%)"),
        vertical_spacing=0.08,
    )
    fig.add_trace(
        go.Scatter(x=eq["entry_ts"], y=eq["equity_index"], mode="lines", name="Equity index"),
        row=1,
        col=1,
    )
    h = hourly[hourly["trades"] > 0]
    colors = ["#2ecc71" if wr >= 50 else "#e74c3c" for wr in h["win_rate"]]
    fig.add_trace(
        go.Bar(x=h.index, y=h["trades"], marker_color=colors, name="Trades/hour", showlegend=False),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Bar(x=by_coin.index, y=by_coin["win_rate"], name="Win %", showlegend=False),
        row=3,
        col=1,
    )
    fig.update_xaxes(title_text="Entry time", row=1, col=1)
    fig.update_xaxes(title_text="Hour UTC", row=2, col=1)
    fig.update_xaxes(title_text="Coin", row=3, col=1)
    fig.update_yaxes(title_text="Equity (1 = start)", row=1, col=1)
    fig.update_yaxes(title_text="Trade count", row=2, col=1)
    fig.update_yaxes(title_text="Win rate %", row=3, col=1)
    fig.update_layout(height=900, title_text="Crypto Vol Backtest — Correlation Charts")
    fig.write_html(str(CHARTS_PATH))
    return True


def _load_env() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
        if env_override and os.path.isfile(env_override):
            load_dotenv(env_override, override=True)
        else:
            load_dotenv(find_dotenv())
    except ImportError:
        pass


def main() -> int:
    _load_env()
    buf = StringIO()
    _log(buf, "Crypto Vol Correlation Analysis")
    _log(buf, f"Source: {CSV_PATH}")
    _log(buf, f"Generated: {datetime.now().isoformat(timespec='seconds')}")

    df = load_trades()
    _log(buf, f"\nLoaded {len(df)} trades from {df['entry_ts'].min()} to {df['entry_ts'].max()}.")
    _log(buf, f"Coins: {', '.join(sorted(df['coin'].unique()))}")

    df = enrich_rsi(df, buf)
    analysis_win_vs_loss(df, buf)

    spy = load_spy_daily_returns()
    df = analysis_market_condition(df, spy, buf)
    hourly = analysis_time_of_day(df, buf)
    analysis_drawdown_clustering(df, buf)

    by_coin = (
        df.groupby("coin")
        .agg(trades=("win", "count"), win_rate=("win", "mean"))
        .sort_values("win_rate", ascending=False)
    )
    by_coin["win_rate"] = (by_coin["win_rate"] * 100).round(2)

    report = buf.getvalue()
    print(report)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"\nSaved report: {REPORT_PATH}")

    if save_charts(df, hourly, by_coin):
        print(f"Saved charts: {CHARTS_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
