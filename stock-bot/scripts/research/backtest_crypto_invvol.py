"""Research: inverse-vol crypto (BTC/ETH/SOL) — not a production sleeve.

Documented flatten (2026-08-26): ATR% **3.0%** (`--atr-flat 0.03`).
See ``scripts/research/crypto_invvol_note.md``. Production crypto stays 0%.

Reuses Alpaca hourly fetch + 0.25% fee from ``backtest_crypto_vol.py``.
Vol: daily ATR% plus GARCH(1,1) shrink (``garch_sizer`` math, no env flip).
Size ∝ 1/σ; flatten coin when ATR% > threshold. No ARIMA. No HMM-primary.

Run (from stock-bot/):
  python scripts/research/backtest_crypto_invvol.py --days 365 --atr-flat 0.03
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from backtest_crypto_vol import FEE_PCT, load_coin_data  # noqa: E402
from modules.garch_sizer import _daily_closes, _to_price_series  # noqa: E402

# Alpaca crypto symbols (repo UNIVERSE uses BTC-USD; this harness uses slash form)
UNIVERSE = ("BTC/USD", "ETH/USD", "SOL/USD")
ATR_PERIOD = 14
# Daily ATR/close above this → that coin is cash. Documented research default 3.0%.
ATR_PCT_FLAT = 0.03
# Target daily move funded: size_i = clip(TARGET_DAILY_SIG / atr_pct, 0, W_CAP)
TARGET_DAILY_SIG = 0.015
W_CAP = 0.40
GROSS_CAP = 1.0
START_EQUITY = 100_000.0
# GARCH annual target (same default as garch_sizer); used only as extra shrink
GARCH_ANN_TARGET = 0.15
GARCH_MULT_MIN = 0.25
GARCH_MULT_MAX = 1.0
SHARPE_SCALE = float(np.sqrt(365))


def _atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr / close.replace(0, np.nan)


def _garch_ann_vol(close: pd.Series) -> float | None:
    try:
        series = _to_price_series(close)
        if series is None:
            return None
        series = _daily_closes(series)
        returns = series.pct_change().dropna()
        if len(returns) < 60:
            return None
        from arch import arch_model

        model = arch_model(returns, vol="Garch", p=1, q=1, rescale=False)
        result = model.fit(disp="off")
        var = float(result.forecast(horizon=1).variance.values[-1][0])
        if not np.isfinite(var) or var <= 0:
            return None
        return float(np.sqrt(var) * np.sqrt(365))
    except Exception:
        return None


def _hourly_to_daily(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["Date"] = pd.to_datetime(x["Date"], utc=True)
    x = x.set_index("Date").sort_index()
    daily = pd.DataFrame(
        {
            "Open": x["Open"].resample("1D").first(),
            "High": x["High"].resample("1D").max(),
            "Low": x["Low"].resample("1D").min(),
            "Close": x["Close"].resample("1D").last(),
        }
    ).dropna()
    daily["atr_pct"] = _atr_pct(daily["High"], daily["Low"], daily["Close"])
    return daily


def _bh_metrics(close: pd.Series, fee: float) -> dict:
    if close is None or len(close) < 3:
        return {"ret": None, "maxdd": None, "sharpe": None}
    r = close.pct_change().fillna(0.0)
    # one-time round-trip fee on BH (enter + exit)
    eq = (1.0 + r).cumprod() * (1.0 - fee) * (1.0 - fee)
    tot = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    peak = eq.cummax()
    dd = float((eq / peak - 1.0).min())
    mu, sd = float(r.mean()), float(r.std())
    sharpe = float(mu / sd * SHARPE_SCALE) if sd > 1e-12 else None
    return {"ret": tot, "maxdd": dd, "sharpe": sharpe}


def run_days(days: int) -> dict:
    books: dict[str, pd.DataFrame] = {}
    for sym in UNIVERSE:
        raw = load_coin_data(sym, days=days)
        if raw is None or raw.empty:
            print(f"  no data {sym}")
            continue
        books[sym] = _hourly_to_daily(raw)
        print(f"  {sym}: {len(books[sym])} daily bars")

    if len(books) < 1:
        return {"error": "no coin data"}

    idx = None
    for df in books.values():
        idx = df.index if idx is None else idx.intersection(df.index)
    idx = idx.sort_values()
    warmup = ATR_PERIOD + 2
    idx = idx[warmup:]
    if len(idx) < 10:
        return {"error": "short overlap"}

    cash = START_EQUITY
    pos_qty = {s: 0.0 for s in books}
    equity_path = []
    cash_flags = []
    trades = 0
    prev_w = {s: 0.0 for s in books}
    gmult = 1.0

    for ts in idx:
        px = {s: float(books[s].loc[ts, "Close"]) for s in books}
        atrp = {s: float(books[s].loc[ts, "atr_pct"]) for s in books}
        mv = sum(pos_qty[s] * px[s] for s in books)
        equity = cash + mv

        w_raw = {}
        for s in books:
            a = atrp[s]
            if not np.isfinite(a) or a <= 1e-8 or a > ATR_PCT_FLAT:
                w_raw[s] = 0.0
            else:
                w_raw[s] = float(np.clip(TARGET_DAILY_SIG / a, 0.0, W_CAP))
        # Optional GARCH shrink on BTC as book vol proxy
        if "BTC/USD" in books and (len(equity_path) % 5 == 0):
            gvol = _garch_ann_vol(books["BTC/USD"]["Close"].loc[:ts])
            if gvol and gvol > 1e-8:
                gmult = float(np.clip(GARCH_ANN_TARGET / gvol, GARCH_MULT_MIN, GARCH_MULT_MAX))
        for s in w_raw:
            w_raw[s] *= gmult
        gross = sum(w_raw.values())
        if gross > GROSS_CAP and gross > 0:
            scale = GROSS_CAP / gross
            w_raw = {s: w * scale for s, w in w_raw.items()}

        for s in books:
            target_val = equity * w_raw[s]
            cur_val = pos_qty[s] * px[s]
            delta = target_val - cur_val
            if abs(delta) / max(equity, 1.0) < 0.002:
                continue
            fee = abs(delta) * FEE_PCT
            if delta > 0:
                spend = delta + fee
                if spend > cash:
                    delta = max(0.0, cash / (1.0 + FEE_PCT) - 1e-9)
                    fee = abs(delta) * FEE_PCT
                    spend = delta + fee
                if delta <= 0:
                    continue
                cash -= spend
                pos_qty[s] += delta / px[s]
            else:
                proceeds = -delta - fee
                cash += max(0.0, proceeds)
                pos_qty[s] += delta / px[s]
                if pos_qty[s] < 0:
                    pos_qty[s] = 0.0
            trades += 1
            prev_w[s] = w_raw[s]

        mv = sum(pos_qty[s] * px[s] for s in books)
        equity = cash + mv
        equity_path.append(equity)
        invested = mv / equity if equity > 0 else 0.0
        cash_flags.append(1.0 if invested < 0.05 else 0.0)

    eq = pd.Series(equity_path, index=idx[: len(equity_path)])
    tot = float(eq.iloc[-1] / START_EQUITY - 1.0)
    peak = eq.cummax()
    maxdd = float((eq / peak - 1.0).min())
    rets = eq.pct_change().dropna()
    sharpe = (
        float(rets.mean() / rets.std() * SHARPE_SCALE) if float(rets.std() or 0) > 1e-12 else None
    )
    pct_cash = float(np.mean(cash_flags)) if cash_flags else None
    bh = {s: _bh_metrics(books[s]["Close"].reindex(idx).dropna(), FEE_PCT) for s in books}
    return {
        "days": days,
        "bars": len(eq),
        "ret": tot,
        "maxdd": maxdd,
        "sharpe": sharpe,
        "trades": trades,
        "pct_cash": pct_cash,
        "end_equity": float(eq.iloc[-1]),
        "bh": bh,
        "fee_pct_leg": FEE_PCT,
        "params": {
            "atr_period": ATR_PERIOD,
            "atr_pct_flat": ATR_PCT_FLAT,
            "target_daily_sig": TARGET_DAILY_SIG,
            "w_cap": W_CAP,
            "garch_ann_target": GARCH_ANN_TARGET,
        },
    }


def _fmt_pct(x) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * float(x):.2f}%"


def main() -> int:
    global ATR_PCT_FLAT
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--atr-flat", type=float, default=None, help="Flatten if daily ATR/close exceeds this")
    args = ap.parse_args()
    if args.atr_flat is not None:
        ATR_PCT_FLAT = float(args.atr_flat)
    print("RESEARCH ONLY — crypto inverse-vol. Not a promote. Crypto production sleeve unchanged.")
    print(f"Universe Alpaca: {UNIVERSE} (config UNIVERSE also lists BTC-USD ETH-USD SOL-USD + many alts; production crypto_vol = RENDER+SOL)")
    print(
        f"Params: ATR{ATR_PERIOD} flatten if atr_pct>{ATR_PCT_FLAT}; "
        f"size=clip({TARGET_DAILY_SIG}/atr_pct,0,{W_CAP}); GARCH shrink vs {GARCH_ANN_TARGET:.0%} ann; "
        f"fee {FEE_PCT:.2%} per leg (Alpaca crypto taker from backtest_crypto_vol)"
    )
    print("No ARIMA. No HMM-primary. MC helper: none for this sleeve (monte_carlo_backtest.py is NYSE paper path).")
    res = run_days(int(args.days))
    if res.get("error"):
        print("FAIL", res["error"])
        return 1
    print(
        f"\n{args.days}d inv-vol | ret={_fmt_pct(res['ret'])} maxDD={_fmt_pct(res['maxdd'])} "
        f"Sharpe={res['sharpe']:.2f} trades={res['trades']} %cash={_fmt_pct(res['pct_cash'])}"
    )
    print("Buy-and-hold (same window, 2-leg fee):")
    for s, m in res["bh"].items():
        print(f"  {s}: ret={_fmt_pct(m['ret'])} maxDD={_fmt_pct(m['maxdd'])} Sharpe={m['sharpe']}")
    print("Verdict: not a promote — research harness only; production crypto stays 0%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
