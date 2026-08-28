"""Research: crypto 24h z-score fade (BTC/ETH/SOL) — not a production sleeve.

Daily bars (1d return ≈ 24h). z = r_1d / rolling_std(r, 20).
z > +1.5 → fade (short, inverse-ATR size). z < −1.5 → small long. else cash.
Flatten if daily ATR% > 3.0% (same as crypto_invvol_note.md).
Fee 0.25%/leg from backtest_crypto_vol. No ARIMA. No HMM-primary.

Run (from stock-bot/):
  python scripts/research/backtest_crypto_fade_z.py --days 90
  python scripts/research/backtest_crypto_fade_z.py --days 365
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
from backtest_crypto_invvol import (  # noqa: E402
    ATR_PCT_FLAT,
    GROSS_CAP,
    SHARPE_SCALE,
    START_EQUITY,
    TARGET_DAILY_SIG,
    UNIVERSE,
    W_CAP,
    _bh_metrics,
    _fmt_pct,
    _garch_ann_vol,
    _hourly_to_daily,
    GARCH_ANN_TARGET,
    GARCH_MULT_MAX,
    GARCH_MULT_MIN,
)

Z_ABS = 1.5
VOL_WINDOW = 20
# Allow signed shorts in research only (not a live sleeve).
ALLOW_SHORT = True


def _add_z(daily: pd.DataFrame) -> pd.DataFrame:
    out = daily.copy()
    r = out["Close"].pct_change()
    vol = r.rolling(VOL_WINDOW).std()
    out["ret_1d"] = r
    out["vol20"] = vol
    out["z"] = r / vol.replace(0, np.nan)
    return out


def run_days(days: int) -> dict:
    books: dict[str, pd.DataFrame] = {}
    for sym in UNIVERSE:
        raw = load_coin_data(sym, days=days)
        if raw is None or raw.empty:
            print(f"  no data {sym}")
            continue
        books[sym] = _add_z(_hourly_to_daily(raw))
        print(f"  {sym}: {len(books[sym])} daily bars")
    if not books:
        return {"error": "no coin data"}

    idx = None
    for df in books.values():
        idx = df.index if idx is None else idx.intersection(df.index)
    idx = idx.sort_values()
    warmup = max(21, VOL_WINDOW + 2)
    idx = idx[warmup:]
    if len(idx) < 10:
        return {"error": "short overlap"}

    cash = START_EQUITY
    pos_qty = {s: 0.0 for s in books}
    equity_path = []
    cash_flags = []
    trades = 0
    gmult = 1.0

    for ts in idx:
        px = {s: float(books[s].loc[ts, "Close"]) for s in books}
        mv = sum(pos_qty[s] * px[s] for s in books)
        equity = cash + mv
        if equity <= 0:
            break

        w_raw = {}
        for s in books:
            a = float(books[s].loc[ts, "atr_pct"])
            z = float(books[s].loc[ts, "z"])
            if not np.isfinite(a) or a <= 1e-8 or a > ATR_PCT_FLAT:
                w_raw[s] = 0.0
                continue
            if not np.isfinite(z):
                w_raw[s] = 0.0
                continue
            mag = float(np.clip(TARGET_DAILY_SIG / a, 0.0, W_CAP))
            if z > Z_ABS:
                w_raw[s] = -mag if ALLOW_SHORT else 0.0
            elif z < -Z_ABS:
                w_raw[s] = mag
            else:
                w_raw[s] = 0.0

        if "BTC/USD" in books and (len(equity_path) % 5 == 0):
            gvol = _garch_ann_vol(books["BTC/USD"]["Close"].loc[:ts])
            if gvol and gvol > 1e-8:
                gmult = float(np.clip(GARCH_ANN_TARGET / gvol, GARCH_MULT_MIN, GARCH_MULT_MAX))
        for s in w_raw:
            w_raw[s] *= gmult
        gross = sum(abs(w) for w in w_raw.values())
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
                    delta = max(0.0, (cash / (1.0 + FEE_PCT)) - 1e-9)
                    fee = abs(delta) * FEE_PCT
                    spend = delta + fee
                if delta <= 0:
                    continue
                cash -= spend
                pos_qty[s] += delta / px[s]
            else:
                proceeds = -delta - fee
                cash += proceeds
                pos_qty[s] += delta / px[s]
            trades += 1

        mv = sum(pos_qty[s] * px[s] for s in books)
        equity = cash + mv
        equity_path.append(equity)
        invested = abs(mv) / equity if equity > 0 else 0.0
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
        "ret": tot,
        "maxdd": maxdd,
        "sharpe": sharpe,
        "trades": trades,
        "pct_cash": pct_cash,
        "bh": bh,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    print("RESEARCH ONLY — z-fade. Not a promote. Production crypto sleeve 0%.")
    print("Bars: DAILY (1d return / 20d realized vol). Flatten ATR%>3.0%. Fee 0.25%/leg.")
    print(f"z>{Z_ABS} short (research); z<-{Z_ABS} long; else cash. Size clip({TARGET_DAILY_SIG}/ATR%,0,{W_CAP}).")
    print("No ARIMA. No HMM-primary. Inv-vol not rerun.")
    res = run_days(int(args.days))
    if res.get("error"):
        print("FAIL", res["error"])
        return 1
    sh = f"{res['sharpe']:.2f}" if res["sharpe"] is not None else "n/a"
    print(
        f"\n{args.days}d z-fade | ret={_fmt_pct(res['ret'])} maxDD={_fmt_pct(res['maxdd'])} "
        f"Sharpe={sh} trades={res['trades']} %cash={_fmt_pct(res['pct_cash'])}"
    )
    print("Buy-and-hold (same window, 2-leg fee):")
    for s, m in res["bh"].items():
        print(f"  {s}: ret={_fmt_pct(m['ret'])} maxDD={_fmt_pct(m['maxdd'])} Sharpe={m['sharpe']}")
    print("Verdict: not a promote unless 365d DD beats both BH and inv-vol 3%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
