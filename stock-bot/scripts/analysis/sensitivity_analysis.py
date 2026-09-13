#!/usr/bin/env python3
"""One-at-a-time parameter sensitivity / robustness analysis.

Sweeps each strategy parameter while holding others at base values.
Re-simulates on already-loaded or cached bars (no --refresh downloads).
Trade CSVs are the strategy baseline; crypto CSV is written if missing.

Run (from stock-bot/):
  python scripts/analysis/sensitivity_analysis.py --strategy crypto
  python scripts/analysis/sensitivity_analysis.py --strategy nyse
  python scripts/analysis/sensitivity_analysis.py --strategy all
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CRYPTO_CSV = ROOT / "scripts" / "research" / "crypto_vol_backtest_v4_results.csv"
CRYPTO_CSV_ALT = ROOT / "crypto_vol_backtest_v4_results.csv"
NYSE_CSV = ROOT / "scripts" / "research" / "intraday_backtest_results.csv"
OUT_JSON = Path(__file__).resolve().parent / "sensitivity_results.json"

CRYPTO_PARAMS: dict[str, dict[str, Any]] = {
    "rsi_lower": {"base": 32, "range": list(range(25, 45, 1))},
    "rsi_upper": {"base": 42, "range": list(range(35, 55, 1))},
    "entry_drop_pct": {
        "base": 3.0,
        "range": [x / 10 for x in range(15, 55, 5)],
    },
    "take_profit_pct": {
        "base": 3.5,
        "range": [x / 10 for x in range(20, 70, 5)],
    },
    "stop_loss_pct": {
        "base": 2.5,
        "range": [x / 10 for x in range(10, 50, 5)],
    },
    "cooldown_hours": {"base": 48, "range": list(range(12, 120, 12))},
}

NYSE_PARAMS: dict[str, dict[str, Any]] = {
    "gap_filter_pct": {
        "base": 2.0,
        "range": [x / 10 for x in range(5, 50, 5)],
    },
    "cooldown_minutes": {"base": 30, "range": list(range(0, 90, 15))},
    "rsi_threshold": {"base": 70, "range": list(range(60, 85, 5))},
}


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------


def metrics_from_trade_pnl(
    pnl_pct: np.ndarray | list[float],
    *,
    start_equity: float = 100_000.0,
    bars_per_year: float = 252.0,
) -> dict[str, float]:
    """Compound trade returns into total return, Sharpe, max DD, win rate."""
    rets = np.asarray(pnl_pct, dtype=float)
    if len(rets) and np.nanmedian(np.abs(rets)) > 0.5:
        rets = rets / 100.0
    if len(rets) == 0:
        return {
            "total_return_pct": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate_pct": 0.0,
            "total_trades": 0,
        }

    equity = start_equity
    peak = start_equity
    max_dd = 0.0
    path = [equity]
    for r in rets:
        equity *= 1.0 + float(r)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
        path.append(equity)

    eq = pd.Series(path)
    bar_rets = eq.pct_change().dropna()
    if len(bar_rets) and bar_rets.std() != 0:
        sharpe = float((bar_rets.mean() / bar_rets.std()) * np.sqrt(bars_per_year))
    else:
        sharpe = 0.0

    wins = float(np.sum(rets > 0))
    return {
        "total_return_pct": round((equity / start_equity - 1.0) * 100.0, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(-max_dd * 100.0, 4),
        "win_rate_pct": round(wins / len(rets) * 100.0, 4),
        "total_trades": int(len(rets)),
    }


def robustness_score(sharpes: list[float]) -> float:
    """Score from coefficient of variation of Sharpe: higher = more stable."""
    arr = np.asarray(sharpes, dtype=float)
    if len(arr) < 2:
        return 100.0
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=0))
    cv = std / max(abs(mean), 1e-9)
    return float(np.clip(100.0 * (1.0 - cv), 0.0, 100.0))


def verdict_from_score(score: float) -> str:
    if score > 70:
        return "Robust"
    if score >= 40:
        return "Moderate"
    return "Fragile"


def verdict_emoji(verdict: str) -> str:
    return {"Robust": "✅", "Moderate": "⚠️", "Fragile": "❌"}.get(verdict, "")


def overall_label(score: float) -> str:
    if score > 70:
        return "ROBUST"
    if score >= 40:
        return "MODERATE"
    return "FRAGILE"


def detect_cliff_edges(
    param_name: str,
    values: list[Any],
    sharpes: list[float],
    base: Any,
) -> list[dict[str, Any]]:
    """Flag Sharpe drop >50% within one step of the base value."""
    if not values or base not in values:
        # nearest base
        try:
            fvals = [float(v) for v in values]
            fbase = float(base)
            idx = int(np.argmin(np.abs(np.asarray(fvals) - fbase)))
        except (TypeError, ValueError):
            return []
    else:
        idx = values.index(base)

    base_s = float(sharpes[idx])
    warnings_out: list[dict[str, Any]] = []
    for j in (idx - 1, idx + 1):
        if j < 0 or j >= len(sharpes):
            continue
        neigh_s = float(sharpes[j])
        if abs(base_s) < 1e-12:
            drop_frac = 1.0 if neigh_s < base_s else 0.0
        else:
            drop_frac = (base_s - neigh_s) / abs(base_s)
        if drop_frac > 0.5:
            warnings_out.append(
                {
                    "parameter": param_name,
                    "base_value": base,
                    "base_sharpe": round(base_s, 4),
                    "neighbor_value": values[j],
                    "neighbor_sharpe": round(neigh_s, 4),
                    "drop_pct": round(drop_frac * 100.0, 1),
                    "message": (
                        f"{param_name}: Sharpe drops {drop_frac*100:.0f}% "
                        f"from {base_s:.2f} at base={base} to {neigh_s:.2f} "
                        f"at {values[j]} (cliff-edge overfit warning)"
                    ),
                }
            )
    return warnings_out


# ---------------------------------------------------------------------------
# Trade-log resimulation (exit / cooldown filters)
# ---------------------------------------------------------------------------


def _resim_exit_params(
    df: pd.DataFrame,
    *,
    take_profit_pct: float,
    stop_loss_pct: float,
    base_tp: float,
    base_sl: float,
) -> np.ndarray:
    """Adjust observed trade PnL for alternate TP/SL (percent points)."""
    pnls: list[float] = []
    for _, row in df.iterrows():
        pnl = float(row["pnl_pct"])
        # Normalize to percent points
        if abs(pnl) <= 0.5 and abs(pnl) > 0:
            # likely already fraction — convert
            pnl = pnl * 100.0
        reason = str(row.get("exit_reason", "")).lower()

        if reason == "take_profit":
            if take_profit_pct <= base_tp:
                pnl = take_profit_pct
            # else: path stopped at old TP; keep observed
        elif reason == "stop_loss":
            # Tighter stop: smaller loss magnitude; looser: assume hit new stop
            pnl = -stop_loss_pct
        else:
            if pnl >= take_profit_pct:
                pnl = take_profit_pct
            elif pnl <= -stop_loss_pct:
                pnl = -stop_loss_pct
        pnls.append(pnl)
    return np.asarray(pnls, dtype=float)


def _resim_cooldown_filter(
    df: pd.DataFrame,
    cooldown_hours: float,
    *,
    symbol_col: str,
    time_col: str,
) -> pd.DataFrame:
    """Drop trades that re-enter a symbol within cooldown after a stop-loss."""
    work = df.copy()
    work["_ts"] = pd.to_datetime(work[time_col], errors="coerce", utc=True)
    work = work.sort_values("_ts")
    cooldown_until: dict[str, pd.Timestamp] = {}
    keep_idx: list[Any] = []
    for idx, row in work.iterrows():
        sym = str(row[symbol_col])
        ts = row["_ts"]
        if pd.isna(ts):
            keep_idx.append(idx)
            continue
        until = cooldown_until.get(sym)
        if until is not None and ts < until:
            continue
        keep_idx.append(idx)
        if str(row.get("exit_reason", "")).lower() == "stop_loss":
            cooldown_until[sym] = ts + pd.Timedelta(hours=float(cooldown_hours))
    return work.loc[keep_idx].drop(columns=["_ts"], errors="ignore")


# ---------------------------------------------------------------------------
# Crypto engine (bar re-sim, one data load)
# ---------------------------------------------------------------------------


def _ensure_crypto_csv_and_data() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Load Alpaca hourly once (disk-cached); ensure trade CSV exists."""
    import pickle

    import backtest_crypto_vol as bcv

    bcv._load_env()
    bcv.VIRTUAL_EQUITY = bcv.DEFAULT_VIRTUAL_EQUITY

    cache_dir = Path(__file__).resolve().parent / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "crypto_v4_1h.pkl"

    data: dict[str, pd.DataFrame]
    if cache_path.is_file():
        print(f"Loading crypto 1h bars from local cache: {cache_path}")
        with cache_path.open("rb") as f:
            data = pickle.load(f)
        print(f"  Cached coins: {list(data.keys())}")
    else:
        print("Loading crypto 1h bars once from Alpaca (saving local cache)...")
        data = bcv.load_universe_data(bcv.UNIVERSE_V4)
        with cache_path.open("wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"  Saved cache: {cache_path}")

    csv_path = CRYPTO_CSV if CRYPTO_CSV.is_file() else CRYPTO_CSV_ALT
    if csv_path.is_file():
        trades_df = pd.read_csv(csv_path)
        print(f"Loaded trade log: {csv_path} ({len(trades_df)} trades)")
    else:
        print("Crypto trade CSV missing — running baseline v4 once to create it...")
        cfg = bcv._v4_config(bcv.UNIVERSE_V4, "v4-baseline")
        bcv.set_entry_params(cfg.drop_pct, cfg.rsi_max, cfg.rsi_min)
        trades, equity, _, _ = bcv.run_backtest(data, cfg)
        metrics = bcv.compute_metrics(trades, equity)
        CRYPTO_CSV.parent.mkdir(parents=True, exist_ok=True)
        bcv.save_trades_csv(trades, CRYPTO_CSV)
        print(f"Baseline metrics: {metrics}")
        trades_df = pd.read_csv(CRYPTO_CSV)

    return trades_df, data


def _crypto_base_params() -> dict[str, Any]:
    return {k: v["base"] for k, v in CRYPTO_PARAMS.items()}


def _run_crypto_sim(
    data: dict[str, pd.DataFrame],
    params: dict[str, Any],
) -> dict[str, float]:
    import backtest_crypto_vol as bcv

    bcv.TAKE_PROFIT_PCT = float(params["take_profit_pct"]) / 100.0
    bcv.STOP_LOSS_PCT = float(params["stop_loss_pct"]) / 100.0
    bcv.LOSS_COOLDOWN_HOURS = int(params["cooldown_hours"])
    drop = -abs(float(params["entry_drop_pct"])) / 100.0
    rsi_lo = float(params["rsi_lower"])
    rsi_hi = float(params["rsi_upper"])
    if rsi_lo > rsi_hi:
        # Invalid band — no trades
        return metrics_from_trade_pnl([], bars_per_year=365 * 24)

    cfg = bcv.BacktestConfig(
        label="sensitivity",
        universe=bcv.UNIVERSE_V4,
        drop_pct=drop,
        rsi_min=rsi_lo,
        rsi_max=rsi_hi,
        spy_gate=True,
        hour_filter=True,
        loss_cooldown=True,
        allow_relaxed_retry=False,
    )
    bcv.set_entry_params(drop, rsi_hi, rsi_lo)
    trades, equity, _, _ = bcv.run_backtest(data, cfg)
    m = bcv.compute_metrics(trades, equity)
    return {
        "total_return_pct": float(m["total_return_pct"]),
        "sharpe": float(m["sharpe"]),
        "max_drawdown_pct": float(m["max_drawdown_pct"]),
        "win_rate_pct": float(m["win_rate_pct"]),
        "total_trades": int(m["total_trades"]),
    }


def _crypto_trade_log_fallback(
    trades_df: pd.DataFrame,
    param_name: str,
    value: Any,
    base_params: dict[str, Any],
) -> dict[str, float]:
    """Approximate exit/cooldown sweeps from the trade log when bar sim unavailable."""
    df = trades_df.copy()
    params = dict(base_params)
    params[param_name] = value

    if param_name in ("take_profit_pct", "stop_loss_pct"):
        pnls = _resim_exit_params(
            df,
            take_profit_pct=float(params["take_profit_pct"]),
            stop_loss_pct=float(params["stop_loss_pct"]),
            base_tp=float(base_params["take_profit_pct"]),
            base_sl=float(base_params["stop_loss_pct"]),
        )
        return metrics_from_trade_pnl(pnls, bars_per_year=365 * 24)

    if param_name == "cooldown_hours":
        # Longer than base: extra filter. Shorter: cannot recover blocked trades.
        cd = float(params["cooldown_hours"])
        if cd > float(base_params["cooldown_hours"]):
            filtered = _resim_cooldown_filter(
                df, cd, symbol_col="coin", time_col="date"
            )
        else:
            filtered = df
        return metrics_from_trade_pnl(
            filtered["pnl_pct"].to_numpy(), bars_per_year=365 * 24
        )

    # Entry params cannot be recovered from trade log alone — return base metrics
    return metrics_from_trade_pnl(df["pnl_pct"].to_numpy(), bars_per_year=365 * 24)


# ---------------------------------------------------------------------------
# NYSE engine (cache re-sim, no refresh)
# ---------------------------------------------------------------------------


def _import_intraday():
    """Load backtest_intraday by path (avoids package layout issues)."""
    import importlib.util

    path = ROOT / "scripts" / "research" / "backtest_intraday.py"
    spec = importlib.util.spec_from_file_location("backtest_intraday_sens", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backtest_intraday_sens"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_nyse_raw_frames(
    bi: Any | None = None,
    extra_tickers: list[str] | None = None,
    window_days: int | None = None,
) -> dict[str, pd.DataFrame]:
    bi = bi or _import_intraday()
    import config

    bi._load_env()
    # Sensitivity universe = trade-log tickers only (fast, matches research evidence)
    if extra_tickers:
        universe = [config.normalize_symbol(t) for t in extra_tickers]
    else:
        universe = [config.normalize_symbol(t) for t in config.get_nyse_universe()]
    universe = sorted({t for t in universe if t})
    days = int(window_days) if window_days else min(400, bi.MAX_HISTORY_DAYS)

    cache_dir = bi.CACHE_DIR
    cached_syms = {p.stem.upper() for p in cache_dir.glob("*.pkl")} if cache_dir.is_dir() else set()
    if cached_syms:
        universe = [t for t in universe if t.upper() in cached_syms]
    print(
        f"Using {len(universe)} trade-log tickers from cache "
        f"(window={days}d, no downloads).",
        flush=True,
    )

    print(f"Loading NYSE 5m bars from cache...", flush=True)
    raw_frames: dict[str, pd.DataFrame] = {}
    for i, ticker in enumerate(universe, 1):
        sym = config.normalize_symbol(ticker)
        if i % 10 == 0 or i == 1 or i == len(universe):
            print(f"  [{i}/{len(universe)}] {sym}", flush=True)
        path = bi._cache_path(sym)
        raw = bi._load_cache(sym) if path.is_file() else None
        if raw is None or raw.empty:
            continue
        sliced = bi._slice_window(raw, days=days)
        if len(sliced) < bi.MA_WINDOW + bi.RSI_PERIOD:
            continue
        raw_frames[sym] = sliced.reset_index(drop=True)
    print(f"Loaded {len(raw_frames)} tickers from cache.", flush=True)
    if not raw_frames:
        raise SystemExit("No NYSE cache data — run scripts/research/backtest_intraday.py first.")
    return raw_frames


def _nyse_base_params() -> dict[str, Any]:
    return {k: v["base"] for k, v in NYSE_PARAMS.items()}


def _run_nyse_sim(
    raw_frames: dict[str, pd.DataFrame],
    params: dict[str, Any],
    bi_mod: Any | None = None,
) -> dict[str, float]:
    bi = bi_mod or _import_intraday()

    gap = float(params["gap_filter_pct"]) / 100.0
    rsi = float(params["rsi_threshold"])
    cd_min = int(params["cooldown_minutes"])

    bi.GAP_THRESHOLD = gap
    bi.RSI_MAX = rsi
    start = datetime.combine(datetime.today().date(), bi.COOLDOWN_START)
    bi.COOLDOWN_END = (start + timedelta(minutes=cd_min)).time()

    frames: dict[str, pd.DataFrame] = {}
    for sym, raw in raw_frames.items():
        enriched = bi.enrich_bars(raw)
        enriched = enriched.reset_index(drop=True)
        enriched["bar_idx"] = np.arange(len(enriched), dtype=np.int64)
        frames[sym] = enriched

    # quality_fixes=True applies gap + cooldown + one-per-day (strategy baseline)
    result = bi.simulate(frames, quality_fixes=True)
    m = bi._metrics(result)
    return {
        "total_return_pct": float(m["total_return_pct"]),
        "sharpe": float(m["sharpe"]),
        "max_drawdown_pct": float(m["max_dd_pct"]),
        "win_rate_pct": float(m["win_rate_pct"]),
        "total_trades": int(m["total_trades"]),
    }


def _nyse_trade_log_fallback(
    trades_df: pd.DataFrame,
    param_name: str,
    value: Any,
    base_params: dict[str, Any],
) -> dict[str, float]:
    """Limited trade-log approx when cache is unavailable."""
    df = trades_df.copy()
    # Without gap size / RSI in the log, only cooldown lengthening can drop trades
    if param_name == "cooldown_minutes" and float(value) > float(
        base_params["cooldown_minutes"]
    ):
        # Drop was_cooldown rows if present; otherwise no change
        if "was_cooldown" in df.columns:
            df = df.loc[~df["was_cooldown"].astype(bool)]
    return metrics_from_trade_pnl(df["pnl_pct"].to_numpy(), bars_per_year=252 * 78)


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------


def sweep_parameter(
    param_name: str,
    spec: dict[str, Any],
    base_params: dict[str, Any],
    run_fn: Callable[[dict[str, Any]], dict[str, float]],
) -> dict[str, Any]:
    base = spec["base"]
    values = list(spec["range"])
    if base not in values:
        values = sorted(set(values) | {base}, key=lambda x: float(x))

    rows: list[dict[str, Any]] = []
    sharpes: list[float] = []
    print(f"\n--- Sweeping {param_name} ({len(values)} values) ---")
    for val in values:
        params = dict(base_params)
        params[param_name] = val
        m = run_fn(params)
        sharpes.append(float(m["sharpe"]))
        rows.append(
            {
                "value": val,
                "total_return_pct": m["total_return_pct"],
                "sharpe": m["sharpe"],
                "max_drawdown_pct": m["max_drawdown_pct"],
                "win_rate_pct": m["win_rate_pct"],
                "total_trades": m.get("total_trades", 0),
            }
        )
        marker = " ← base" if val == base else ""
        print(
            f"  {param_name}={val}: ret={m['total_return_pct']:.2f}% "
            f"Sharpe={m['sharpe']:.2f} DD={m['max_drawdown_pct']:.2f}% "
            f"WR={m['win_rate_pct']:.1f}% n={m.get('total_trades', 0)}{marker}"
        )

    score = robustness_score(sharpes)
    verdict = verdict_from_score(score)
    best_i = int(np.argmax(sharpes))
    worst_i = int(np.argmin(sharpes))
    cliffs = detect_cliff_edges(param_name, values, sharpes, base)

    return {
        "parameter": param_name,
        "base_value": base,
        "score": round(score, 2),
        "verdict": verdict,
        "best_value": values[best_i],
        "best_sharpe": round(sharpes[best_i], 4),
        "worst_value": values[worst_i],
        "worst_sharpe": round(sharpes[worst_i], 4),
        "cliff_edges": cliffs,
        "sweep": rows,
    }


PLAIN_ENGLISH = {
    "rsi_lower": (
        "Lower RSI floor for crypto mean-reversion entries. If the score is fragile, "
        "the edge depends heavily on exactly where oversold is defined."
    ),
    "rsi_upper": (
        "Upper RSI cap for the entry band. Wide swings in Sharpe across nearby values "
        "mean the band may be overfit to this sample."
    ),
    "entry_drop_pct": (
        "Required 4h percentage drop before entry. Robustness here means the dip-buy "
        "trigger works across a band of drop sizes, not only the base 3%."
    ),
    "take_profit_pct": (
        "Profit target. A cliff near base suggests the strategy only works if exits "
        "hit a narrow TP; moderate/robust means nearby targets behave similarly."
    ),
    "stop_loss_pct": (
        "Stop distance. Fragile stops often mean the historical edge relied on a "
        "very specific loss cutoff rather than a broad risk rule."
    ),
    "cooldown_hours": (
        "Hours to block a coin after a stop. Robust cooldown means waiting a bit more "
        "or less does not destroy the edge."
    ),
    "gap_filter_pct": (
        "Minimum open gap that blocks NYSE entries. Fragile gap filters mean results "
        "hinge on excluding/including a thin set of gap days."
    ),
    "cooldown_minutes": (
        "Morning open cooldown length (from 9:30 ET). Robustness means the open "
        "noise filter is not razor-thin around 30 minutes."
    ),
    "rsi_threshold": (
        "Max RSI allowed on NYSE momentum entries. If Sharpe collapses one step from "
        "70, the momentum filter is likely overfit."
    ),
}


def print_summary(strategy: str, result: dict[str, Any]) -> None:
    print("\n" + "=" * 78)
    print(f"SENSITIVITY SUMMARY — {strategy}")
    print("=" * 78)
    header = (
        f"{'Parameter':<18} {'Score':>6} {'Base':>8} {'Best':>8} "
        f"{'Worst':>8} {'Verdict':<12}"
    )
    print(header)
    print("-" * len(header))
    for p in result["parameters"]:
        print(
            f"{p['parameter']:<18} {p['score']:>6.1f} {p['base_value']!s:>8} "
            f"{p['best_value']!s:>8} {p['worst_value']!s:>8} "
            f"{p['verdict']} {verdict_emoji(p['verdict'])}"
        )
    overall = result["overall_score"]
    label = result["overall_verdict"]
    print("-" * len(header))
    print(f"Overall robustness score: {overall:.1f} → {label}")

    cliffs = result.get("cliff_edge_warnings") or []
    if cliffs:
        print("\nCliff-edge warnings:")
        for c in cliffs:
            print(f"  ⚠ {c['message']}")
    else:
        print("\nCliff-edge warnings: none")

    print("\nPlain English:")
    for p in result["parameters"]:
        note = PLAIN_ENGLISH.get(p["parameter"], "")
        print(
            f"  • {p['parameter']}: score {p['score']:.0f} ({p['verdict']}). "
            f"Base={p['base_value']}, best Sharpe at {p['best_value']}, "
            f"worst at {p['worst_value']}. {note}"
        )
    print(
        f"\nBottom line: across all swept parameters the strategy looks "
        f"{label}. "
        + (
            "Parameter choices are stable nearby — less likely pure overfit."
            if label == "ROBUST"
            else (
                "Some parameters matter a lot; keep them under watch and avoid "
                "treating the base as sacred."
                if label == "MODERATE"
                else "Results hinge on narrow parameter choices — treat live "
                "expectations cautiously."
            )
        )
    )


def run_crypto() -> dict[str, Any]:
    trades_df, data = _ensure_crypto_csv_and_data()
    base_params = _crypto_base_params()
    use_bars = bool(data)

    def run_fn(params: dict[str, Any]) -> dict[str, float]:
        if use_bars:
            return _run_crypto_sim(data, params)
        # Should not happen if load succeeded
        return _crypto_trade_log_fallback(
            trades_df, "take_profit_pct", params["take_profit_pct"], base_params
        )

    param_results = []
    cliffs: list[dict[str, Any]] = []
    for name, spec in CRYPTO_PARAMS.items():
        pr = sweep_parameter(name, spec, base_params, run_fn)
        param_results.append(pr)
        cliffs.extend(pr["cliff_edges"])

    scores = [p["score"] for p in param_results]
    overall = float(np.mean(scores)) if scores else 0.0
    overall_r = round(overall, 2)
    result = {
        "strategy": "crypto_v4_mr",
        "method": "bar_resim_one_load" if use_bars else "trade_log_approx",
        "trade_csv": str(CRYPTO_CSV if CRYPTO_CSV.is_file() else CRYPTO_CSV_ALT),
        "n_baseline_trades": int(len(trades_df)),
        "parameters": param_results,
        "overall_score": overall_r,
        "overall_verdict": overall_label(overall_r),
        "cliff_edge_warnings": cliffs,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    print_summary("Crypto v4 MR", result)
    return result


def run_nyse() -> dict[str, Any]:
    if not NYSE_CSV.is_file():
        raise SystemExit(f"NYSE trade CSV not found: {NYSE_CSV}")
    trades_df = pd.read_csv(NYSE_CSV)
    print(f"Loaded trade log: {NYSE_CSV} ({len(trades_df)} trades)")

    bi = _import_intraday()
    trade_tickers = (
        trades_df["ticker"].astype(str).unique().tolist()
        if "ticker" in trades_df.columns
        else []
    )
    try:
        # ~365d window matching the trade-log span; tickers from the log only
        raw_frames = _load_nyse_raw_frames(
            bi, extra_tickers=trade_tickers, window_days=400
        )
        use_cache = True
    except SystemExit as exc:
        print(f"WARNING: {exc} — falling back to trade-log approximations.")
        raw_frames = {}
        use_cache = False

    base_params = _nyse_base_params()

    def run_fn(params: dict[str, Any]) -> dict[str, float]:
        if use_cache:
            return _run_nyse_sim(raw_frames, params, bi_mod=bi)
        for k, v in params.items():
            if v != base_params[k]:
                return _nyse_trade_log_fallback(trades_df, k, v, base_params)
        return metrics_from_trade_pnl(
            trades_df["pnl_pct"].to_numpy(), bars_per_year=252 * 78
        )

    param_results = []
    cliffs: list[dict[str, Any]] = []
    for name, spec in NYSE_PARAMS.items():
        pr = sweep_parameter(name, spec, base_params, run_fn)
        param_results.append(pr)
        cliffs.extend(pr["cliff_edges"])

    scores = [p["score"] for p in param_results]
    overall = float(np.mean(scores)) if scores else 0.0
    overall_r = round(overall, 2)
    result = {
        "strategy": "nyse_momentum",
        "method": "cache_resim" if use_cache else "trade_log_approx",
        "trade_csv": str(NYSE_CSV),
        "n_baseline_trades": int(len(trades_df)),
        "parameters": param_results,
        "overall_score": overall_r,
        "overall_verdict": overall_label(overall_r),
        "cliff_edge_warnings": cliffs,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    print_summary("NYSE momentum", result)
    return result


def save_results(payload: dict[str, Any]) -> None:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"\nSaved: {OUT_JSON}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parameter sensitivity / robustness analysis")
    parser.add_argument(
        "--strategy",
        choices=("crypto", "nyse", "all"),
        default="all",
        help="Which strategy to sweep (default: all)",
    )
    args = parser.parse_args()

    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "strategies": {},
    }

    if args.strategy in ("crypto", "all"):
        payload["strategies"]["crypto"] = run_crypto()
    if args.strategy in ("nyse", "all"):
        payload["strategies"]["nyse"] = run_nyse()

    # Overall across requested strategies
    strat_scores = [
        s["overall_score"] for s in payload["strategies"].values() if "overall_score" in s
    ]
    if strat_scores:
        combined = float(np.mean(strat_scores))
        payload["combined_overall_score"] = round(combined, 2)
        payload["combined_overall_verdict"] = overall_label(combined)
        print(
            f"\n*** Combined overall: {combined:.1f} → "
            f"{payload['combined_overall_verdict']} ***"
        )

    # Merge into existing JSON if running a single strategy
    if args.strategy != "all" and OUT_JSON.is_file():
        try:
            prev = json.loads(OUT_JSON.read_text(encoding="utf-8"))
            merged = prev.get("strategies", {})
            merged.update(payload["strategies"])
            payload["strategies"] = merged
            all_scores = [
                s["overall_score"]
                for s in merged.values()
                if isinstance(s, dict) and "overall_score" in s
            ]
            if all_scores:
                combined = float(np.mean(all_scores))
                payload["combined_overall_score"] = round(combined, 2)
                payload["combined_overall_verdict"] = overall_label(combined)
        except Exception:
            pass

    save_results(payload)


if __name__ == "__main__":
    main()
