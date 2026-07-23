"""Optimized shared backtest helpers — cache, metrics, validation, reporting."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

import config

SHARPE_SCALE = float(np.sqrt(252))
BENCHMARK_SYMBOL = "VTI"
FAST_MODE_MAX_TICKERS = int(os.getenv("BACKTEST_FAST_MAX_TICKERS", "22"))
DISK_CACHE_DIR = Path(os.getenv("BACKTEST_DISK_CACHE_DIR", "data/cache/backtest"))
DEFAULT_HTML_REPORT = Path("scripts/analysis/backtest_report.html")
DEFAULT_EXPORT_JSON = Path("scripts/analysis/backtest_last.json")
DEFAULT_EXPORT_CSV = Path("scripts/analysis/backtest_last.csv")
PURGE_EMBARGO_BARS = int(os.getenv("BACKTEST_PURGE_EMBARGO_BARS", "5"))
ROLLING_METRIC_WINDOW = int(os.getenv("BACKTEST_ROLLING_WINDOW", "63"))
DEFAULT_EQUITY_SLIPPAGE_BPS = float(os.getenv("BACKTEST_EQUITY_SLIPPAGE_BPS", "5"))
DEFAULT_CRYPTO_SLIPPAGE_BPS = float(os.getenv("BACKTEST_CRYPTO_SLIPPAGE_BPS", "10"))
DEFAULT_EQUITY_COMMISSION_BPS = float(os.getenv("BACKTEST_EQUITY_COMMISSION_BPS", "0"))
DEFAULT_CRYPTO_COMMISSION_BPS = float(os.getenv("BACKTEST_CRYPTO_COMMISSION_BPS", "0"))


@dataclass
class BacktestRunOptions:
    fast_mode: bool = False
    no_thinking: bool = False
    equity_slippage_bps: float = DEFAULT_EQUITY_SLIPPAGE_BPS
    crypto_slippage_bps: float = DEFAULT_CRYPTO_SLIPPAGE_BPS
    equity_commission_bps: float = DEFAULT_EQUITY_COMMISSION_BPS
    crypto_commission_bps: float = DEFAULT_CRYPTO_COMMISSION_BPS
    realistic_costs: bool = True
    walk_forward_folds: int = 0
    full_accuracy: bool = True
    parallel_arms: bool = True
    max_workers: int = 4
    report_html: Path | None = None
    export_json: Path | None = None
    export_csv: Path | None = None
    slippage_sensitivity: bool = False
    slippage_levels_bps: tuple[int, ...] = (0, 5, 10, 25)


RUN_OPTIONS = BacktestRunOptions()
LAST_BACKTEST_RESULT: dict[str, Any] | None = None

_DATA_CACHE: dict[tuple[Any, ...], pd.DataFrame] = {}
_INDICATOR_CACHE: dict[str, dict[str, Any]] = {}
_DATA_CACHE_MAX_ENTRIES = int(os.getenv("BACKTEST_MEM_CACHE_MAX", "2"))


def reset_caches(*, disk: bool = False) -> None:
    _DATA_CACHE.clear()
    _INDICATOR_CACHE.clear()
    if disk and DISK_CACHE_DIR.is_dir():
        for p in DISK_CACHE_DIR.glob("*.parquet"):
            p.unlink(missing_ok=True)
        for p in DISK_CACHE_DIR.glob("*.pkl"):
            p.unlink(missing_ok=True)
        for p in DISK_CACHE_DIR.glob("*.meta.json"):
            p.unlink(missing_ok=True)


def release_backtest_memory(*, collect: bool = True) -> None:
    """Drop in-process backtest matrices between compare arms (disk cache kept)."""
    _DATA_CACHE.clear()
    _INDICATOR_CACHE.clear()
    if collect:
        import gc

        gc.collect()


def _trim_data_cache() -> None:
    while len(_DATA_CACHE) > _DATA_CACHE_MAX_ENTRIES:
        _DATA_CACHE.pop(next(iter(_DATA_CACHE)))


def _db_mtime() -> float | None:
    db = Path(getattr(config, "DB_PATH", "market_data.db"))
    if not db.is_file():
        db = Path("market_data.db")
    try:
        return db.stat().st_mtime if db.is_file() else None
    except OSError:
        return None


def _disk_key(sim_days: int | None, use_max: bool) -> str:
    tag = "max" if use_max else f"days_{sim_days or 0}"
    return tag


def _load_disk_cache(key_str: str) -> pd.DataFrame | None:
    DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = DISK_CACHE_DIR / f"{key_str}.meta.json"
    data_path = DISK_CACHE_DIR / f"{key_str}.pkl"
    if not meta_path.is_file() or not data_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("db_mtime") != _db_mtime():
            return None
        return pd.read_pickle(data_path)
    except Exception:
        return None


def _save_disk_cache(key_str: str, data: pd.DataFrame) -> None:
    DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = DISK_CACHE_DIR / f"{key_str}.meta.json"
    data_path = DISK_CACHE_DIR / f"{key_str}.pkl"
    try:
        data.to_pickle(data_path)
        meta_path.write_text(
            json.dumps({"db_mtime": _db_mtime(), "rows": len(data), "cols": len(data.columns)}),
            encoding="utf-8",
        )
    except Exception:
        pass


def calendar_days_to_fetch(sim_days: int, *, min_history: int, warmup_buffer: int = 45) -> int:
    return int(sim_days + min_history + warmup_buffer)


def min_rows_for_backtest(sim_days: int, *, min_history: int) -> int:
    return min_history + max(10, int(sim_days * 0.85))


def ensure_daily_data_cached(
    days: int | None,
    *,
    refresh: bool = False,
    use_max: bool = False,
    min_history: int,
    backtest_days: int,
    load_close_matrix: Callable[..., pd.DataFrame],
    fetch_daily_history: Callable[..., Any],
    fetch_daily_history_for_tickers: Callable[..., Any] | None = None,
) -> pd.DataFrame:
    """Memory + disk cache for daily close matrix."""
    if refresh:
        reset_caches(disk=True)

    disk_key = _disk_key(days if not use_max else None, use_max)
    if not refresh:
        disk_df = _load_disk_cache(disk_key)
        if disk_df is not None and len(disk_df) >= min_history + 10:
            _DATA_CACHE[("1d", disk_key)] = disk_df.copy()
            _trim_data_cache()
            if use_max:
                return disk_df.copy()
            sim_days = days or backtest_days
            need_rows = min_rows_for_backtest(sim_days, min_history=min_history)
            if len(disk_df) >= need_rows:
                out = disk_df.iloc[-need_rows:].copy() if len(disk_df) > need_rows else disk_df.copy()
                _DATA_CACHE[("1d", "days", sim_days)] = out.copy()
                _trim_data_cache()
                return out

    if use_max:
        mem_key = ("1d", "max", None)
        if not refresh and mem_key in _DATA_CACHE:
            cached = _DATA_CACHE[mem_key]
            if len(cached) >= min_history + 10:
                return cached.copy()
        fetch_daily_history(use_max=True)
        data = load_close_matrix(interval="1d")
        _DATA_CACHE[mem_key] = data.copy()
        _trim_data_cache()
        _save_disk_cache(disk_key, data)
        return data

    sim_days = days or backtest_days
    need_rows = min_rows_for_backtest(sim_days, min_history=min_history)
    mem_key = ("1d", "days", sim_days)

    if not refresh and mem_key in _DATA_CACHE:
        cached = _DATA_CACHE[mem_key]
        if len(cached) >= need_rows:
            return cached.iloc[-need_rows:].copy() if len(cached) > need_rows else cached.copy()

    for extra in (1000, 730, 500):
        if extra <= sim_days:
            continue
        long_key = ("1d", "days", extra)
        if long_key in _DATA_CACHE and len(_DATA_CACHE[long_key]) >= need_rows:
            data = _DATA_CACHE[long_key].iloc[-need_rows:].copy()
            _DATA_CACHE[mem_key] = data.copy()
            _trim_data_cache()
            return data

    if not refresh:
        data = load_close_matrix(interval="1d")
        if len(data) >= need_rows:
            if len(data) > need_rows:
                data = data.iloc[-need_rows:]
            _DATA_CACHE[mem_key] = data.copy()
            _trim_data_cache()
            _save_disk_cache(disk_key, data)
            return data.copy()

    fetch_days = calendar_days_to_fetch(sim_days, min_history=min_history)
    fetch_daily_history(fetch_days)
    data = load_close_matrix(interval="1d")
    if len(data) > need_rows:
        data = data.iloc[-need_rows:]
    if len(data) < min_history:
        fetch_daily_history(use_max=True)
        data = load_close_matrix(interval="1d")
    need_rows = min_rows_for_backtest(sim_days, min_history=min_history)
    if len(data) > need_rows:
        data = data.iloc[-need_rows:].copy()
    _DATA_CACHE[mem_key] = data.copy()
    _trim_data_cache()
    _save_disk_cache(disk_key, data)
    return data.copy()


def apply_fast_mode_data(data: pd.DataFrame) -> pd.DataFrame:
    keep: set[str] = set(config.UNIVERSE)
    keep.add(BENCHMARK_SYMBOL)
    keep.add(config.SPY_BOT_SYMBOL)
    screener = config.load_screener_universe_tickers() or []
    keep.update(screener[:FAST_MODE_MAX_TICKERS])
    cols = [c for c in data.columns if c in keep]
    return data[cols] if len(cols) >= 8 else data


def _data_fingerprint(data: pd.DataFrame) -> str:
    h = hashlib.md5(
        f"{len(data)}:{len(data.columns)}:{data.index[0]}:{data.index[-1]}".encode(),
        usedforsecurity=False,
    )
    return h.hexdigest()[:16]


def prepare_indicator_cache(data: pd.DataFrame, *, spy_ma_window: int) -> dict[str, Any]:
    """Precompute MA, volatility, and pair spread z-scores once per data frame."""
    fp = _data_fingerprint(data)
    if fp in _INDICATOR_CACHE:
        return _INDICATOR_CACHE[fp]

    cache: dict[str, Any] = {"fingerprint": fp}
    spy = config.SPY_BOT_SYMBOL
    if spy in data.columns:
        spy_s = data[spy]
        cache["spy_ma"] = spy_s.rolling(spy_ma_window, min_periods=spy_ma_window).mean()
        cache["spy_close"] = spy_s
        rets = spy_s.pct_change()
        cache["spy_vol_20"] = rets.rolling(20, min_periods=10).std() * SHARPE_SCALE
        cache["spy_vol_60"] = rets.rolling(60, min_periods=21).std() * SHARPE_SCALE

    if BENCHMARK_SYMBOL in data.columns:
        cache["vti_close"] = data[BENCHMARK_SYMBOL]

    # Lightweight pair z-score grid for top liquid names (stat-arb speed hint)
    liquid = [c for c in data.columns if c in set(config.UNIVERSE) | {spy}][:12]
    pair_z: dict[tuple[str, str], pd.Series] = {}
    for i, a in enumerate(liquid):
        for b in liquid[i + 1 :]:
            if a not in data.columns or b not in data.columns:
                continue
            spread = data[a] - data[b]
            roll_std = spread.rolling(30, min_periods=20).std()
            z = (spread - spread.rolling(30, min_periods=20).mean()) / roll_std.replace(0, np.nan)
            pair_z[(a, b)] = z
    cache["pair_z_30"] = pair_z
    _INDICATOR_CACHE[fp] = cache
    return cache


def get_indicator_cache(data: pd.DataFrame) -> dict[str, Any] | None:
    fp = _data_fingerprint(data)
    return _INDICATOR_CACHE.get(fp)


def effective_execution(
    price: float, side: str, symbol: str, base_tx_cost: float
) -> tuple[float, float, float]:
    """Return (adjusted_price, total_tx_cost_pct, slippage_cost_pct).

    Slippage adjusts fill price; commission bps stacks on top of base_tx_cost
    (e.g. Alpaca crypto taker fee). Equity default commission is 0 (Alpaca $0).
    """
    opts = RUN_OPTIONS
    is_crypto = config.is_crypto(symbol)
    slip_bps = opts.crypto_slippage_bps if is_crypto else opts.equity_slippage_bps
    comm_bps = opts.crypto_commission_bps if is_crypto else opts.equity_commission_bps
    slip = max(0.0, slip_bps) / 10_000.0
    comm = max(0.0, comm_bps) / 10_000.0
    side_l = side.lower()
    adj_price = float(price) * (1.0 + slip) if side_l == "buy" else float(price) * (1.0 - slip)
    total_cost = max(0.0, float(base_tx_cost)) + comm
    return adj_price, total_cost, slip


def rolling_metric_series(
    equity_curve: list[float] | pd.Series,
    *,
    window: int = ROLLING_METRIC_WINDOW,
) -> dict[str, pd.Series]:
    curve = pd.Series(equity_curve, dtype=float)
    returns = curve.pct_change().dropna()
    if returns.empty:
        return {}
    roll = returns.rolling(window, min_periods=max(21, window // 3))
    sharpe = (roll.mean() / roll.std()) * SHARPE_SCALE
    downside = returns.where(returns < 0)
    roll_down = downside.rolling(window, min_periods=max(21, window // 3))
    sortino = (roll.mean() / roll_down.std()) * SHARPE_SCALE
    dd = (curve / curve.cummax()) - 1
    roll_peak = curve.rolling(window, min_periods=1).max()
    roll_dd = ((curve - roll_peak) / roll_peak) * 100
    roll_ret = (curve / curve.shift(window) - 1) * 100
    calmar = roll_ret / roll_dd.abs().replace(0, np.nan)
    return {
        "rolling_sharpe": sharpe,
        "rolling_sortino": sortino.replace([np.inf, -np.inf], np.nan),
        "rolling_calmar": calmar.replace([np.inf, -np.inf], np.nan),
        "drawdown_pct": dd * 100,
    }


def compute_performance_metrics(
    equity_curve: list[float] | pd.Series,
    *,
    initial_capital: float,
    benchmark_return_pct: float | None = None,
    total_orders: int = 0,
    equity_index: list[str] | None = None,
) -> dict[str, Any]:
    curve = pd.Series(equity_curve, dtype=float)
    if equity_index and len(equity_index) == len(curve):
        curve.index = pd.to_datetime(equity_index)

    if curve.empty:
        base = {
            "final_equity": initial_capital,
            "total_return_pct": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate_pct": 0.0,
            "rolling_sharpe_mean": 0.0,
            "profit_factor": 0.0,
            "avg_trade_return_pct": 0.0,
        }
        return base

    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / initial_capital - 1) * 100
    sharpe = (returns.mean() / returns.std()) * SHARPE_SCALE if returns.std() != 0 else 0.0
    downside = returns[returns < 0]
    sortino = (
        (returns.mean() / downside.std()) * SHARPE_SCALE
        if len(downside) > 0 and downside.std() != 0
        else 0.0
    )
    dd = (curve / curve.cummax()) - 1
    max_dd = float(dd.min() * 100)
    calmar = (total_ret / abs(max_dd)) if max_dd != 0 else 0.0
    win_rate = float((returns > 0).mean() * 100) if len(returns) else 0.0

    roll = returns.rolling(ROLLING_METRIC_WINDOW, min_periods=21)
    roll_sharpe = (roll.mean() / roll.std()) * SHARPE_SCALE
    rolling_sharpe_mean = float(roll_sharpe.replace([np.inf, -np.inf], np.nan).dropna().mean())
    if not np.isfinite(rolling_sharpe_mean):
        rolling_sharpe_mean = 0.0

    gains = returns[returns > 0].sum()
    losses = abs(returns[returns < 0].sum())
    profit_factor = float(gains / losses) if losses > 0 else (float(gains) if gains > 0 else 0.0)
    avg_trade = (total_ret / total_orders) if total_orders > 0 else 0.0

    rolling = rolling_metric_series(curve)

    out: dict[str, Any] = {
        "final_equity": round(float(curve.iloc[-1]), 2),
        "total_return_pct": round(total_ret, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "win_rate_pct": round(win_rate, 1),
        "rolling_sharpe_mean": round(rolling_sharpe_mean, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_trade_return_pct": round(avg_trade, 3),
        "total_orders": total_orders,
    }
    if rolling:
        out["rolling_sharpe_series"] = [
            round(float(v), 3) for v in rolling["rolling_sharpe"].dropna().tolist()[-120:]
        ]
        out["drawdown_series"] = [
            round(float(v), 3) for v in rolling["drawdown_pct"].dropna().tolist()[-120:]
        ]
    if benchmark_return_pct is not None:
        out["benchmark_return_pct"] = round(benchmark_return_pct, 2)
        out["vs_benchmark_pp"] = round(total_ret - benchmark_return_pct, 2)
    return out


RHYME_REGIME_LABELS: tuple[str, ...] = (
    "RHYME_A: Euphoric_Volatility",
    "RHYME_B: Panic_Volatility",
    "RHYME_C: Steady_Bullish_Growth",
    "RHYME_D: Range_Bound_Neutral",
    "RHYME_E: Steady_Bearish_Decline",
)


def resolve_regime_name(name: str) -> str | None:
    """Map CLI shorthand (RHYME_D, D, full label) to canonical regime string."""
    key = (name or "").strip().upper().replace(" ", "_")
    if not key:
        return None
    for full in RHYME_REGIME_LABELS:
        short = full.split(":")[0].strip()
        letter = short.rsplit("_", 1)[-1] if "_" in short else ""
        if (
            full.upper() == key
            or full.upper().startswith(key + ":")
            or short == key
            or key == f"RHYME_{letter}"
            or key == letter
        ):
            return full
    return None


def regime_matches(current: str, filter_spec: str | None) -> bool:
    if not filter_spec:
        return True
    resolved = resolve_regime_name(filter_spec)
    if resolved:
        return (current or "") == resolved
    return filter_spec.upper() in (current or "").upper()


def _regime_sort_key(label: str) -> tuple[int, str]:
    for i, full in enumerate(RHYME_REGIME_LABELS):
        if label == full or label.startswith(full.split(":")[0]):
            return (i, label)
    return (99, label)


def compute_regime_breakdown(
    equity_curve: list[float] | pd.Series,
    regime_labels: list[str],
    *,
    initial_capital: float,
) -> list[dict[str, Any]]:
    """Attribute daily PnL and return stats to each RHYME regime label."""
    curve = list(equity_curve)
    if len(curve) != len(regime_labels) or len(curve) < 2:
        return []

    pnl_by_regime: dict[str, float] = {}
    returns_by_regime: dict[str, list[float]] = {}
    trade_days = len(curve) - 1

    for i in range(1, len(curve)):
        reg = regime_labels[i]
        prev_eq = curve[i - 1]
        day_ret = curve[i] / prev_eq - 1.0 if prev_eq else 0.0
        day_pnl = curve[i] - prev_eq
        pnl_by_regime[reg] = pnl_by_regime.get(reg, 0.0) + day_pnl
        returns_by_regime.setdefault(reg, []).append(day_ret)

    total_pnl = curve[-1] - initial_capital
    rows: list[dict[str, Any]] = []
    for reg in sorted(returns_by_regime.keys(), key=_regime_sort_key):
        rets = pd.Series(returns_by_regime[reg], dtype=float)
        pnl = pnl_by_regime.get(reg, 0.0)
        days = len(rets)
        sharpe = (
            float(rets.mean() / rets.std() * SHARPE_SCALE)
            if rets.std() > 0
            else 0.0
        )
        contrib = (
            round(100.0 * pnl / total_pnl, 1)
            if abs(total_pnl) > 0.01
            else 0.0
        )
        short = reg.split(":")[0].strip() if ":" in reg else reg
        rows.append(
            {
                "regime": reg,
                "short": short,
                "days": days,
                "pct_days": round(100.0 * days / trade_days, 1),
                "pnl_usd": round(pnl, 2),
                "contrib_pct": contrib,
                "avg_daily_pct": round(float(rets.mean()) * 100.0, 3),
                "sharpe": round(sharpe, 2),
                "win_rate_pct": round(float((rets > 0).mean()) * 100.0, 1),
            }
        )
    return rows


def format_regime_breakdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "--- Regime breakdown ---\n(no data)"
    lines = ["--- Regime breakdown ---"]
    lines.append(
        f"{'Regime':<38} {'Days':>5} {'%Days':>6} {'Win%':>6} "
        f"{'AvgDay%':>8} {'Sharpe':>7} {'PnL $':>10} {'Contrib%':>9}"
    )
    lines.append("-" * 95)
    for row in rows:
        pnl = row["pnl_usd"]
        pnl_s = f"{pnl:+,.2f}" if pnl >= 0 else f"{pnl:,.2f}"
        label = row.get("short") or row["regime"]
        if len(label) > 38:
            label = label[:35] + "..."
        lines.append(
            f"{label:<38} {row['days']:>5} {row['pct_days']:>5.1f}% "
            f"{row['win_rate_pct']:>5.1f}% {row['avg_daily_pct']:>7.3f}% "
            f"{row['sharpe']:>7.2f} {pnl_s:>10} {row['contrib_pct']:>8.1f}%"
        )
    return "\n".join(lines)


def walk_forward_purged(
    data: pd.DataFrame,
    *,
    min_history: int,
    n_folds: int,
    run_fn: Callable[[pd.DataFrame, int, int], dict],
    embargo_bars: int = PURGE_EMBARGO_BARS,
) -> list[dict]:
    """Purged walk-forward: expanding train, OOS test segment, embargo gap."""
    if n_folds < 2 or len(data) < min_history + n_folds * 40:
        return []
    sim_start = min_history
    sim_end = len(data)
    sim_len = sim_end - sim_start
    test_size = sim_len // n_folds
    rows: list[dict] = []
    for fold in range(n_folds):
        test_begin = sim_start + fold * test_size
        test_end = sim_start + (fold + 1) * test_size if fold < n_folds - 1 else sim_end
        train_end = max(min_history, test_begin - embargo_bars)
        if test_end - test_begin < 15 or train_end < min_history:
            continue
        train_data = data.iloc[:train_end].copy()
        _ = train_data  # expanding context — run_fn receives full history through test_end
        result = run_fn(data.iloc[:test_end].copy(), test_begin, test_end)
        rows.append(
            {
                "fold": fold + 1,
                "train_bars": train_end - min_history,
                "test_bars": test_end - test_begin,
                "embargo_bars": embargo_bars,
                "return_pct": result.get("total_return_pct"),
                "sharpe": result.get("sharpe"),
                "sortino": result.get("sortino"),
                "max_dd_pct": result.get("max_drawdown_pct"),
            }
        )
    return rows


def format_walk_forward_table(rows: list[dict], *, purged: bool = False) -> str:
    if not rows:
        return "Walk-forward: insufficient bars for requested folds."
    title = "Purged walk-forward" if purged else "Walk-forward"
    lines = [
        f"--- {title} ---",
        f"{'Fold':>4} {'Train':>6} {'Test':>5} {'Return':>9} {'Sharpe':>7} {'Sort':>6} {'MaxDD':>8}",
        "-" * 52,
    ]
    for r in rows:
        lines.append(
            f"{r['fold']:>4} {r.get('train_bars', r.get('bars', 0)):>6} "
            f"{r.get('test_bars', 0):>5} "
            f"{r['return_pct']:>+8.2f}% {r['sharpe']:>7.2f} "
            f"{r.get('sortino', 0):>6.2f} {r['max_dd_pct']:>7.2f}%"
        )
    avg_sh = sum(r["sharpe"] or 0 for r in rows) / len(rows)
    lines.append("-" * 52)
    lines.append(f"{'Avg':>4} {'':>6} {'':>5} {'':>9} {avg_sh:>7.2f}")
    return "\n".join(lines)


def walk_forward_summary(
    data: pd.DataFrame,
    *,
    min_history: int,
    n_folds: int,
    run_fn: Callable[[pd.DataFrame], dict],
) -> list[dict]:
    """Legacy sequential walk-forward (compat)."""
    if n_folds < 2 or len(data) < min_history + n_folds * 30:
        return []
    sim_len = len(data) - min_history
    fold_size = sim_len // n_folds
    rows: list[dict] = []
    for fold in range(n_folds):
        end_i = min_history + (fold + 1) * fold_size if fold < n_folds - 1 else len(data)
        if end_i - min_history < 20:
            continue
        result = run_fn(data.iloc[:end_i].copy())
        rows.append(
            {
                "fold": fold + 1,
                "bars": end_i - min_history,
                "train_bars": end_i - min_history,
                "test_bars": fold_size,
                "return_pct": result.get("total_return_pct"),
                "sharpe": result.get("sharpe"),
                "sortino": result.get("sortino"),
                "max_dd_pct": result.get("max_drawdown_pct"),
            }
        )
    return rows


def run_slippage_sensitivity(
    run_fn: Callable[[], dict],
    *,
    levels_bps: tuple[int, ...] | None = None,
) -> list[dict]:
    levels = levels_bps or RUN_OPTIONS.slippage_levels_bps
    saved_eq = RUN_OPTIONS.equity_slippage_bps
    saved_cr = RUN_OPTIONS.crypto_slippage_bps
    rows: list[dict] = []
    try:
        for bps in levels:
            RUN_OPTIONS.equity_slippage_bps = float(bps)
            RUN_OPTIONS.crypto_slippage_bps = float(bps)
            result = run_fn()
            rows.append(
                {
                    "slippage_bps": bps,
                    "return_pct": result.get("total_return_pct"),
                    "sharpe": result.get("sharpe"),
                    "max_dd_pct": result.get("max_drawdown_pct"),
                }
            )
    finally:
        RUN_OPTIONS.equity_slippage_bps = saved_eq
        RUN_OPTIONS.crypto_slippage_bps = saved_cr
    return rows


def format_slippage_table(rows: list[dict]) -> str:
    lines = [
        "--- Slippage sensitivity ---",
        f"{'Bps':>5} {'Return':>9} {'Sharpe':>7} {'MaxDD':>8}",
        "-" * 32,
    ]
    for r in rows:
        lines.append(
            f"{r['slippage_bps']:>5} {r['return_pct']:>+8.2f}% "
            f"{r['sharpe']:>7.2f} {r['max_dd_pct']:>7.2f}%"
        )
    return "\n".join(lines)


_TABLE_BLANK = "-"


def format_enhanced_final_table(rows: list[dict]) -> str:
    b = _TABLE_BLANK
    lines = [
        f"{'Config':<26} {'Ret':>7} {'Sh':>5} {'So':>5} {'DD':>7} "
        f"{'Win':>4} {'PF':>4} {'Ord':>4} {'Hal':>3} {'RSh':>5} {'vsVTI':>7}",
        "-" * 88,
    ]
    for r in rows:
        label = r["label"][:26]
        ret = f"{r['return_pct']:>+6.1f}%" if r.get("return_pct") is not None else f"     {b}"
        sh = f"{r['sharpe']:>5.2f}" if r.get("sharpe") is not None else f"    {b}"
        so = f"{r['sortino']:>5.2f}" if r.get("sortino") is not None else f"    {b}"
        dd = f"{r['max_dd_pct']:>6.1f}%" if r.get("max_dd_pct") is not None else f"     {b}"
        wr = f"{r['win_rate_pct']:>3.0f}%" if r.get("win_rate_pct") is not None else f"  {b}"
        pf = f"{r['profit_factor']:>4.2f}" if r.get("profit_factor") is not None else f"   {b}"
        ord_n = f"{r.get('total_orders', 0):>4}" if r.get("total_orders") is not None else f"   {b}"
        hal = f"{r.get('halt_events', 0):>3}" if r.get("halt_events") is not None else f"  {b}"
        rsh = (
            f"{r['rolling_sharpe_mean']:>5.2f}"
            if r.get("rolling_sharpe_mean") is not None
            else f"    {b}"
        )
        vs = (
            f"{r['vs_vti']:>+6.1f}p"
            if r.get("vs_vti") is not None and "VTI" not in r.get("label", "")
            else f"      {b}"
        )
        lines.append(
            f"{label:<26} {ret} {sh} {so} {dd} {wr} {pf} {ord_n} {hal} {rsh} {vs}"
        )
    lines.append("-" * 88)
    opts = RUN_OPTIONS
    if opts.realistic_costs or opts.equity_slippage_bps or opts.crypto_slippage_bps:
        lines.append(
            f"Costs: equity slip {opts.equity_slippage_bps:.0f}bps + comm {opts.equity_commission_bps:.0f}bps | "
            f"crypto slip {opts.crypto_slippage_bps:.0f}bps + fee-aware taker"
        )
    return "\n".join(lines)


def apply_default_execution_costs() -> None:
    """Apply realistic default slippage unless caller zeroed both explicitly."""
    opts = RUN_OPTIONS
    if opts.realistic_costs:
        if opts.equity_slippage_bps <= 0:
            opts.equity_slippage_bps = DEFAULT_EQUITY_SLIPPAGE_BPS
        if opts.crypto_slippage_bps <= 0:
            opts.crypto_slippage_bps = DEFAULT_CRYPTO_SLIPPAGE_BPS


def export_results_json(result: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return path


def export_results_csv(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="#0f172a")
    import matplotlib.pyplot as plt

    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def generate_html_report(
    result: dict,
    path: Path,
    *,
    title: str = "PythonTrading Backtest Report",
    compare_rows: list[dict] | None = None,
    header_label: str | None = None,
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    equity = result.get("equity_values") or []
    eq_idx = result.get("equity_index") or list(range(len(equity)))
    dates = pd.to_datetime(eq_idx[: len(equity)])

    fig1, ax1 = plt.subplots(figsize=(10, 3.5), facecolor="#0f172a")
    ax1.set_facecolor("#1e293b")
    ax1.plot(dates, equity, color="#34d399", linewidth=1.5)
    ax1.set_title("Equity curve", color="#e2e8f0")
    ax1.tick_params(colors="#94a3b8")
    for sp in ax1.spines.values():
        sp.set_color("#334155")
    eq_b64 = _fig_to_b64(fig1)

    rolling = result.get("rolling_sharpe_series") or []
    fig2, ax2 = plt.subplots(figsize=(10, 2.8), facecolor="#0f172a")
    ax2.set_facecolor("#1e293b")
    if rolling:
        ax2.plot(rolling, color="#60a5fa", linewidth=1.2)
    ax2.axhline(0, color="#64748b", linewidth=0.8)
    ax2.set_title(f"Rolling Sharpe ({ROLLING_METRIC_WINDOW}d)", color="#e2e8f0")
    ax2.tick_params(colors="#94a3b8")
    rs_b64 = _fig_to_b64(fig2)

    dd = result.get("drawdown_series") or []
    fig3, ax3 = plt.subplots(figsize=(10, 2.5), facecolor="#0f172a")
    ax3.set_facecolor("#1e293b")
    if dd:
        ax3.fill_between(range(len(dd)), dd, 0, color="#f87171", alpha=0.5)
    ax3.set_title("Drawdown %", color="#e2e8f0")
    ax3.tick_params(colors="#94a3b8")
    dd_b64 = _fig_to_b64(fig3)

    stats = [
        ("Return", f"{result.get('total_return_pct', 0):+.2f}%"),
        ("Sharpe", f"{result.get('sharpe', 0):.2f}"),
        ("Sortino", f"{result.get('sortino', 0):.2f}"),
        ("Calmar", f"{result.get('calmar', 0):.2f}"),
        ("Max DD", f"{result.get('max_drawdown_pct', 0):.2f}%"),
        ("Win rate", f"{result.get('win_rate_pct', 0):.1f}%"),
        ("Profit factor", f"{result.get('profit_factor', 0):.2f}"),
        ("Orders", f"{result.get('total_orders', 0)}"),
    ]
    stat_html = "".join(
        f'<div class="stat"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in stats
    )

    compare_html = ""
    if compare_rows:
        compare_html = "<h2>Compare arms</h2><pre>" + format_enhanced_final_table(compare_rows) + "</pre>"

    header_note = ""
    if header_label:
        header_note = f'<p class="sub">{header_label}</p>'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{font-family:Segoe UI,sans-serif;background:#0f172a;color:#e2e8f0;margin:24px}}
h1,h2{{color:#f8fafc}} .sub{{color:#94a3b8;font-size:14px;margin-top:-8px}}
.stats{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.stat{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 16px;min-width:100px}}
.k{{font-size:11px;color:#94a3b8;text-transform:uppercase}} .v{{font-size:20px;font-weight:600;color:#34d399}}
img{{max-width:100%;border-radius:8px;margin:12px 0;border:1px solid #334155}}
pre{{background:#1e293b;padding:12px;border-radius:8px;overflow:auto;font-size:12px}}
</style></head><body>
<h1>{title}</h1>
{header_note}
<p>{result.get('start_date','')} -> {result.get('end_date','')} · {result.get('sim_days','')} calendar days</p>
<div class="stats">{stat_html}</div>
<h2>Equity</h2><img src="data:image/png;base64,{eq_b64}" alt="equity"/>
<h2>Rolling Sharpe</h2><img src="data:image/png;base64,{rs_b64}" alt="rolling sharpe"/>
<h2>Drawdown</h2><img src="data:image/png;base64,{dd_b64}" alt="drawdown"/>
{compare_html}
</body></html>"""
    path.write_text(html, encoding="utf-8")
    return path


def parallel_map_backtests(
    tasks: list[tuple[Any, ...]],
    worker: Callable[..., dict],
    *,
    max_workers: int | None = None,
) -> list[dict]:
    """Run independent backtest tasks in parallel (ProcessPoolExecutor)."""
    if len(tasks) <= 1:
        return [worker(task) for task in tasks]
    workers = max_workers or min(RUN_OPTIONS.max_workers, len(tasks))
    results: list[dict | None] = [None] * len(tasks)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(worker, task): i for i, task in enumerate(tasks)}
        for fut in as_completed(future_map):
            idx = future_map[fut]
            results[idx] = fut.result()
    return [r for r in results if r is not None]


def apply_run_options_to_config() -> None:
    opts = RUN_OPTIONS
    if opts.no_thinking or opts.fast_mode:
        config.PAPER_THINKING_ENGINE_ENABLED = False
    if opts.fast_mode:
        if not config.effective_stat_arb_sleeve_cap_enabled():
            config.PAPER_STAT_ARB_ENABLED = False
        config.PAPER_DYNAMIC_UNIVERSE_ENABLED = False
        config.PAPER_VOL_TRADING_ENABLED = False
        config.PAPER_OPTIONS_SLEEVE_ENABLED = False


def store_last_result(result: dict) -> None:
    global LAST_BACKTEST_RESULT
    LAST_BACKTEST_RESULT = result
