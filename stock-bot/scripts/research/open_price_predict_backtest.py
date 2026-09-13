"""Research-only: predict next-session Open vs prior Close baseline.

No .env writes, no restart, no orders. Uses yfinance daily OHLC.

Models
------
1) baseline:      pred_open = prior_close
2) gap_persist:   pred_open = prior_close * (1 + a * prior_gap)
                  a fit on expanding window (walk-forward)
3) ridge_lag:     ridge on [prior_close, ret_1d, prior_gap, range_pct, spy_gap]
                  walk-forward expanding window

Also scores a simple *decision* backtest: if |pred_gap| > 2%, would a
gap-skip (block new buys when open gaps >2%) have been correct that day?

Usage (from stock-bot/):
  python scripts/research/open_price_predict_backtest.py
  python scripts/research/open_price_predict_backtest.py --symbols GOLD,FCX,SMCI --days 252
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT_MD = Path(__file__).with_name("open_price_predict_last.md")
OUT_JSON = Path(__file__).with_name("open_price_predict_last.json")

DEFAULT_SYMBOLS = ("GOLD", "FCX", "SMCI", "ELF", "HALO", "SPY")
MIN_TRAIN = 40
GAP_SKIP_PCT = 0.02


def _fetch_ohlc(symbol: str, days: int) -> pd.DataFrame:
    import yfinance as yf

    # Pad calendar days so we get ~days trading bars.
    period = f"{max(days + 40, 60)}d"
    hist = yf.Ticker(symbol).history(period=period, auto_adjust=True)
    if hist is None or hist.empty:
        raise RuntimeError(f"No yfinance history for {symbol}")
    hist = hist.rename(columns=str.title)
    need = {"Open", "High", "Low", "Close", "Volume"}
    if not need.issubset(set(hist.columns)):
        raise RuntimeError(f"{symbol}: missing OHLC columns {hist.columns.tolist()}")
    out = hist[list(need)].dropna().copy()
    out.index = pd.to_datetime(out.index).tz_localize(None)
    if len(out) > days:
        out = out.iloc[-days:].copy()
    return out


def _build_frame(sym_ohlc: pd.DataFrame, spy_ohlc: pd.DataFrame) -> pd.DataFrame:
    df = sym_ohlc.copy()
    df["prior_close"] = df["Close"].shift(1)
    df["prior_open"] = df["Open"].shift(1)
    df["prior_high"] = df["High"].shift(1)
    df["prior_low"] = df["Low"].shift(1)
    df["ret_1d"] = df["Close"].pct_change(1).shift(1)  # known before today open
    df["prior_gap"] = (df["prior_open"] / df["Close"].shift(2) - 1.0)
    df["range_pct"] = ((df["prior_high"] - df["prior_low"]) / df["prior_close"]).clip(
        lower=0
    )
    spy = spy_ohlc.copy()
    spy["spy_prior_close"] = spy["Close"].shift(1)
    spy["spy_gap"] = spy["Open"] / spy["spy_prior_close"] - 1.0
    # For predicting *today's* open we only know SPY's prior day — use lag of spy_gap
    # (yesterday's SPY open gap) as a weak overnight proxy available at prior close.
    spy["spy_gap_lag1"] = spy["spy_gap"].shift(1)
    df = df.join(spy[["spy_gap_lag1"]], how="left")
    df["actual_open"] = df["Open"]
    df["actual_gap"] = df["actual_open"] / df["prior_close"] - 1.0
    df = df.dropna(
        subset=["prior_close", "ret_1d", "prior_gap", "range_pct", "actual_open"]
    )
    return df


def _mae(y: np.ndarray, yhat: np.ndarray) -> float:
    return float(np.mean(np.abs(y - yhat)))


def _mape(y: np.ndarray, yhat: np.ndarray) -> float:
    mask = np.abs(y) > 1e-9
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs((y[mask] - yhat[mask]) / y[mask])))


def _median_ae(y: np.ndarray, yhat: np.ndarray) -> float:
    return float(np.median(np.abs(y - yhat)))


def _score(y: np.ndarray, yhat: np.ndarray) -> dict[str, float]:
    return {
        "n": int(len(y)),
        "mae": round(_mae(y, yhat), 4),
        "median_ae": round(_median_ae(y, yhat), 4),
        "mape": round(_mape(y, yhat), 6),
    }


def _score_gap(actual_gap: np.ndarray, pred_gap: np.ndarray) -> dict[str, float]:
    corr = float("nan")
    if (
        len(actual_gap) > 2
        and float(np.std(actual_gap)) > 1e-12
        and float(np.std(pred_gap)) > 1e-12
    ):
        corr = float(np.corrcoef(actual_gap, pred_gap)[0, 1])
    return {
        "mae_gap_pp": round(float(np.mean(np.abs(actual_gap - pred_gap)) * 100), 4),
        "median_ae_gap_pp": round(
            float(np.median(np.abs(actual_gap - pred_gap)) * 100), 4
        ),
        "corr": round(corr, 4),
    }


def _walk_gap_persist(df: pd.DataFrame) -> np.ndarray:
    """pred_open = prior_close * (1 + a * prior_gap); a from expanding OLS."""
    preds = np.full(len(df), np.nan)
    prior_close = df["prior_close"].to_numpy(dtype=float)
    prior_gap = df["prior_gap"].to_numpy(dtype=float)
    actual = df["actual_open"].to_numpy(dtype=float)
    for i in range(MIN_TRAIN, len(df)):
        # Fit a on days where we already know (open, prior_gap):
        # open ≈ prior_close * (1 + a * prior_gap) => (open/prior_close - 1) = a * prior_gap
        y = actual[:i] / prior_close[:i] - 1.0
        x = prior_gap[:i]
        denom = float(np.dot(x, x))
        a = float(np.dot(x, y) / denom) if denom > 1e-12 else 0.0
        a = float(np.clip(a, -2.0, 2.0))
        preds[i] = prior_close[i] * (1.0 + a * prior_gap[i])
    return preds


def _walk_ridge(df: pd.DataFrame) -> np.ndarray:
    """Ridge on standardized features → predict open/prior_close - 1, then open."""
    feats = ["ret_1d", "prior_gap", "range_pct", "spy_gap_lag1"]
    X_raw = df[feats].fillna(0.0).to_numpy(dtype=float)
    prior_close = df["prior_close"].to_numpy(dtype=float)
    y_gap = (df["actual_open"] / df["prior_close"] - 1.0).to_numpy(dtype=float)
    preds = np.full(len(df), np.nan)
    lam = 1.0
    for i in range(MIN_TRAIN, len(df)):
        Xtr = X_raw[:i]
        ytr = y_gap[:i]
        mu = Xtr.mean(axis=0)
        sd = Xtr.std(axis=0)
        sd = np.where(sd < 1e-9, 1.0, sd)
        Xs = (Xtr - mu) / sd
        xtx = Xs.T @ Xs + lam * np.eye(Xs.shape[1])
        xty = Xs.T @ ytr
        try:
            beta = np.linalg.solve(xtx, xty)
        except np.linalg.LinAlgError:
            beta = np.zeros(Xs.shape[1])
        x_i = (X_raw[i] - mu) / sd
        gap_hat = float(x_i @ beta)
        gap_hat = float(np.clip(gap_hat, -0.15, 0.15))
        preds[i] = prior_close[i] * (1.0 + gap_hat)
    return preds


def _gap_skip_decision(actual_gap: np.ndarray, pred_gap: np.ndarray) -> dict:
    """When |pred| > 2%, did |actual| also exceed 2%? (precision of skip signal)."""
    pred_flag = np.abs(pred_gap) > GAP_SKIP_PCT
    actual_flag = np.abs(actual_gap) > GAP_SKIP_PCT
    n_pred = int(pred_flag.sum())
    if n_pred == 0:
        return {
            "n_skip_signals": 0,
            "precision": None,
            "recall": None,
            "note": "no |pred_gap|>2% signals in OOS window",
        }
    tp = int((pred_flag & actual_flag).sum())
    fp = int((pred_flag & ~actual_flag).sum())
    fn = int((~pred_flag & actual_flag).sum())
    return {
        "n_skip_signals": n_pred,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": round(tp / n_pred, 4),
        "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "threshold": GAP_SKIP_PCT,
    }


def evaluate_symbol(symbol: str, days: int, spy: pd.DataFrame) -> dict:
    ohlc = _fetch_ohlc(symbol, days)
    df = _build_frame(ohlc, spy)
    if len(df) < MIN_TRAIN + 20:
        return {"symbol": symbol, "error": f"too few bars ({len(df)})"}

    baseline = df["prior_close"].to_numpy(dtype=float)
    gap_persist = _walk_gap_persist(df)
    ridge = _walk_ridge(df)
    actual = df["actual_open"].to_numpy(dtype=float)
    prior = df["prior_close"].to_numpy(dtype=float)
    actual_gap = df["actual_gap"].to_numpy(dtype=float)

    mask = np.isfinite(gap_persist) & np.isfinite(ridge)
    y = actual[mask]
    b = baseline[mask]
    g = gap_persist[mask]
    r = ridge[mask]
    pclose = prior[mask]
    ag = actual_gap[mask]

    def gap_from(pred_open: np.ndarray) -> np.ndarray:
        return pred_open / pclose - 1.0

    scores = {
        "baseline_prior_close": {
            **_score(y, b),
            **_score_gap(ag, gap_from(b)),
            "gap_skip": _gap_skip_decision(ag, gap_from(b)),
        },
        "gap_persist": {
            **_score(y, g),
            **_score_gap(ag, gap_from(g)),
            "gap_skip": _gap_skip_decision(ag, gap_from(g)),
        },
        "ridge_lag": {
            **_score(y, r),
            **_score_gap(ag, gap_from(r)),
            "gap_skip": _gap_skip_decision(ag, gap_from(r)),
        },
    }

    # Latest row (last completed session) — for curiosity, not a trade.
    last = df.iloc[-1]
    last_pred = {
        "date": str(df.index[-1].date()),
        "prior_close": round(float(last["prior_close"]), 4),
        "actual_open": round(float(last["actual_open"]), 4),
        "actual_gap_pct": round(float(last["actual_gap"]) * 100, 3),
        "baseline": round(float(baseline[-1]), 4),
        "gap_persist": round(float(gap_persist[-1]), 4)
        if np.isfinite(gap_persist[-1])
        else None,
        "ridge_lag": round(float(ridge[-1]), 4) if np.isfinite(ridge[-1]) else None,
    }

    # Beat baseline? lower MAE wins.
    base_mae = scores["baseline_prior_close"]["mae"]
    for name in ("gap_persist", "ridge_lag"):
        scores[name]["beats_baseline_mae"] = bool(scores[name]["mae"] < base_mae - 1e-6)

    return {
        "symbol": symbol,
        "bars_total": int(len(ohlc)),
        "oos_n": int(mask.sum()),
        "window_start": str(df.index[mask][0].date()) if mask.any() else None,
        "window_end": str(df.index[mask][-1].date()) if mask.any() else None,
        "models": scores,
        "last_completed_session": last_pred,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated tickers (include SPY for market feature only if listed)",
    )
    ap.add_argument("--days", type=int, default=252, help="Trading days of history")
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("No symbols")
        return 1

    spy = _fetch_ohlc("SPY", args.days)
    results = []
    for sym in symbols:
        if sym == "SPY":
            # Still evaluate SPY as a name (uses its own spy_gap_lag1).
            pass
        print(f"Evaluating {sym}…", flush=True)
        try:
            results.append(evaluate_symbol(sym, args.days, spy))
        except Exception as exc:
            results.append({"symbol": sym, "error": str(exc)})

    payload = {
        "research_only": True,
        "days": args.days,
        "symbols": symbols,
        "min_train": MIN_TRAIN,
        "gap_skip_pct": GAP_SKIP_PCT,
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    OUT_MD.write_text(_render_md_clean(payload), encoding="utf-8")
    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_JSON}")

    # Console summary
    for row in results:
        if row.get("error"):
            print(f"{row['symbol']}: ERROR {row['error']}")
            continue
        base = row["models"]["baseline_prior_close"]["mae"]
        gp = row["models"]["gap_persist"]
        rr = row["models"]["ridge_lag"]
        print(
            f"{row['symbol']}: baseline MAE ${base:.3f} | "
            f"gap_persist ${gp['mae']:.3f} ({'BEAT' if gp.get('beats_baseline_mae') else 'no'}) | "
            f"ridge ${rr['mae']:.3f} ({'BEAT' if rr.get('beats_baseline_mae') else 'no'})"
        )
    return 0


def _render_md_clean(payload: dict) -> str:
    lines = [
        "# Open-price predict backtest (research only)",
        "",
        "No orders, no `.env`, no restart.",
        "",
        f"**Days requested:** {payload['days']}  ",
        f"**Symbols:** {', '.join(payload['symbols'])}  ",
        f"**Train min (walk-forward):** {MIN_TRAIN} bars  ",
        f"**Gap-skip threshold:** {GAP_SKIP_PCT:.0%} (matches paper quality skip)",
        "",
        "## Models",
        "",
        "| Name | Rule |",
        "|---|---|",
        "| baseline | `pred_open = prior_close` |",
        "| gap_persist | `prior_close * (1 + a * prior_gap)` — `a` expanding OLS |",
        "| ridge_lag | ridge on ret_1d, prior_gap, range_pct, spy_gap_lag1 → gap → open |",
        "",
        "## Results (lower MAE wins; must beat baseline)",
        "",
    ]
    beats = 0
    compared = 0
    for row in payload["results"]:
        if row.get("error"):
            lines.append(f"### {row['symbol']}: ERROR — {row['error']}\n")
            continue
        lines.append(
            f"### {row['symbol']}  "
            f"(OOS n={row['oos_n']}, {row['window_start']} → {row['window_end']})"
        )
        lines.append("")
        lines.append(
            "| Model | MAE $ | MedAE $ | MAPE | Gap MAE (pp) | Gap corr | Beats baseline? |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|:---:|")
        for name, m in row["models"].items():
            if name != "baseline_prior_close":
                compared += 1
                if m.get("beats_baseline_mae"):
                    beats += 1
            beat_s = (
                "—"
                if name == "baseline_prior_close"
                else ("YES" if m.get("beats_baseline_mae") else "no")
            )
            gmae = m.get("mae_gap_pp", float("nan"))
            gcorr = m.get("corr", float("nan"))
            lines.append(
                f"| `{name}` | {m['mae']:.4f} | {m['median_ae']:.4f} | "
                f"{m['mape']*100:.3f}% | {gmae} | {gcorr} | {beat_s} |"
            )
        last = row["last_completed_session"]
        lines.append("")
        lines.append(
            f"Last completed session **{last['date']}**: "
            f"prior_close={last['prior_close']}, actual_open={last['actual_open']} "
            f"(gap {last['actual_gap_pct']:+.2f}%). "
            f"Preds — baseline={last['baseline']}, "
            f"gap_persist={last['gap_persist']}, ridge={last['ridge_lag']}."
        )
        gs = row["models"]["ridge_lag"]["gap_skip"]
        lines.append("")
        lines.append(
            f"Gap-skip decision (ridge, `|pred|>{GAP_SKIP_PCT:.0%}`): "
            f"signals={gs.get('n_skip_signals')}, precision={gs.get('precision')}, "
            f"recall={gs.get('recall')}."
        )
        lines.append("")

    lines.extend(
        [
            "## Roll-up",
            "",
            f"Model variants that beat baseline MAE: **{beats} / {compared}** "
            f"(across gap_persist + ridge × names).",
            "",
            "## Verdict rule",
            "",
            "Promote nothing. If models rarely beat `prior_close`, leave paper alone.",
            "",
            "## Non-goals",
            "",
            "- No after-hours feed (yfinance daily only).",
            "- No live Telegram / no paper gate change.",
            "- Does not predict the *close* — only the next open.",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
