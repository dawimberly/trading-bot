"""Monte Carlo robustness testing via block-bootstrap synthetic price paths."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Callable

import numpy as np
import pandas as pd

DEFAULT_MC_RUNS = int(__import__("os").getenv("BACKTEST_MC_RUNS", "200"))
DEFAULT_MC_BLOCK_SIZE = int(__import__("os").getenv("BACKTEST_MC_BLOCK_SIZE", "10"))
BENCHMARK_SYMBOL = "VTI"

_MC_BACKTEST_KWARGS: dict[str, Any] | None = None


def set_mc_backtest_kwargs(kwargs: dict[str, Any]) -> None:
    global _MC_BACKTEST_KWARGS
    _MC_BACKTEST_KWARGS = dict(kwargs)


def block_bootstrap_returns(
    returns: pd.DataFrame,
    n_samples: int,
    block_size: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Joint block bootstrap: same block boundaries for all columns (preserves cross-asset structure)."""
    if n_samples <= 0 or returns.empty:
        return returns.iloc[:0].copy()
    block_size = max(1, min(int(block_size), len(returns)))
    max_start = max(0, len(returns) - block_size)
    rows: list[pd.Series] = []
    while len(rows) < n_samples:
        start = int(rng.integers(0, max_start + 1)) if max_start > 0 else 0
        block = returns.iloc[start : start + block_size]
        for _, row in block.iterrows():
            rows.append(row)
            if len(rows) >= n_samples:
                break
    return pd.DataFrame(rows[:n_samples], columns=returns.columns)


def synthetic_price_matrix(
    data: pd.DataFrame,
    *,
    min_history: int,
    block_size: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Rebuild the simulation segment from bootstrapped daily returns; warmup bars stay as-is."""
    min_history = max(2, int(min_history))
    sim_len = len(data) - min_history
    if sim_len <= 0:
        return data.copy()

    rets = data.pct_change().iloc[min_history:].copy()
    rets = rets.fillna(0.0).clip(lower=-0.95, upper=3.0)
    boot = block_bootstrap_returns(rets, sim_len, block_size, rng)

    anchor = data.iloc[min_history - 1].astype(float)
    synth_rows: list[pd.Series] = []
    level = anchor.copy()
    for i in range(sim_len):
        level = level * (1.0 + boot.iloc[i].astype(float))
        level = level.where(np.isfinite(level), np.nan)
        synth_rows.append(level)

    synth_df = pd.DataFrame(
        synth_rows,
        index=data.index[min_history : min_history + sim_len],
        columns=data.columns,
    )
    warmup = data.iloc[:min_history].copy()
    return pd.concat([warmup, synth_df])


def _benchmark_return_on_data(data: pd.DataFrame, start_idx: int) -> float | None:
    if BENCHMARK_SYMBOL not in data.columns:
        return None
    col = data[BENCHMARK_SYMBOL].iloc[start_idx:].dropna()
    if len(col) < 2 or float(col.iloc[0]) <= 0:
        return None
    return float((col.iloc[-1] / col.iloc[0] - 1) * 100)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(values, pct))


def _ascii_histogram(values: list[float], *, bins: int = 12, width: int = 40) -> str:
    if not values:
        return "(no data)"
    arr = np.asarray(values, dtype=float)
    lo, hi = float(np.min(arr)), float(np.max(arr))
    if lo == hi:
        return f"  {lo:+.1f}% | {'#' * min(width, len(arr))}"
    edges = np.linspace(lo, hi, bins + 1)
    counts, _ = np.histogram(arr, bins=edges)
    max_c = int(counts.max()) or 1
    lines: list[str] = []
    for i, c in enumerate(counts):
        if c == 0:
            continue
        left, right = edges[i], edges[i + 1]
        mid = (left + right) / 2
        bar = "#" * max(1, int(c / max_c * width))
        lines.append(f"  {mid:>+7.1f}% | {bar} ({int(c)})")
    return "\n".join(lines) if lines else "(flat distribution)"


def aggregate_monte_carlo_runs(
    runs: list[dict[str, Any]],
    *,
    full_period: dict[str, Any] | None = None,
) -> dict[str, Any]:
    returns = [float(r.get("total_return_pct") or 0) for r in runs]
    sharpes = [float(r.get("sharpe") or 0) for r in runs]
    max_dds = [float(r.get("max_drawdown_pct") or 0) for r in runs]
    beat_vti = 0
    for r in runs:
        strat = float(r.get("total_return_pct") or 0)
        bench = r.get("benchmark_return_pct")
        if bench is not None and strat > float(bench):
            beat_vti += 1
    n = len(runs)
    prob_loss = sum(1 for x in returns if x < 0) / n if n else 0.0
    prob_breakeven = sum(1 for x in returns if -1.0 <= x <= 1.0) / n if n else 0.0
    prob_beat_vti = beat_vti / n if n else 0.0
    success_rate = sum(1 for x in returns if x > 0) / n if n else 0.0

    summary = {
        "n_runs": n,
        "return_mean": round(float(np.mean(returns)), 2) if n else 0.0,
        "return_median": round(float(np.median(returns)), 2) if n else 0.0,
        "return_p5": round(_percentile(returns, 5), 2),
        "return_p10": round(_percentile(returns, 10), 2),
        "return_p50": round(_percentile(returns, 50), 2),
        "return_p90": round(_percentile(returns, 90), 2),
        "return_p95": round(_percentile(returns, 95), 2),
        "sharpe_mean": round(float(np.mean(sharpes)), 2) if n else 0.0,
        "sharpe_median": round(float(np.median(sharpes)), 2) if n else 0.0,
        "sharpe_p5": round(_percentile(sharpes, 5), 2),
        "sharpe_p95": round(_percentile(sharpes, 95), 2),
        "max_dd_mean": round(float(np.mean(max_dds)), 2) if n else 0.0,
        "max_dd_median": round(float(np.median(max_dds)), 2) if n else 0.0,
        "max_dd_p5": round(_percentile(max_dds, 5), 2),
        "max_dd_p95": round(_percentile(max_dds, 95), 2),
        "prob_loss": round(prob_loss * 100, 1),
        "prob_breakeven": round(prob_breakeven * 100, 1),
        "prob_beat_vti": round(prob_beat_vti * 100, 1),
        "success_rate": round(success_rate * 100, 1),
        "returns": returns,
        "sharpes": sharpes,
        "max_drawdowns": max_dds,
    }
    if full_period:
        summary["full_period_return_pct"] = full_period.get("total_return_pct")
        summary["full_period_sharpe"] = full_period.get("sharpe")
        summary["full_period_max_dd_pct"] = full_period.get("max_drawdown_pct")
        if full_period.get("total_return_pct") is not None:
            fp = float(full_period["total_return_pct"])
            summary["full_period_return_percentile"] = round(
                100.0 * sum(1 for x in returns if x <= fp) / n, 1
            ) if n else None
    return summary


def format_monte_carlo_report(
    summary: dict[str, Any],
    *,
    block_size: int,
    seed: int | None,
) -> str:
    n = summary.get("n_runs", 0)
    lines = [
        "--- Monte Carlo robustness (block bootstrap) ---",
        f"Runs: {n} | block size: {block_size}d | seed: {seed if seed is not None else 'random'}",
        "Method: joint block-bootstrap of daily returns → synthetic prices → full strategy replay",
        "",
        "Return distribution (%):",
        f"  Mean / Median:  {summary.get('return_mean', 0):+.2f}% / {summary.get('return_median', 0):+.2f}%",
        f"  P5 / P10:       {summary.get('return_p5', 0):+.2f}% / {summary.get('return_p10', 0):+.2f}%",
        f"  P50:            {summary.get('return_p50', 0):+.2f}%",
        f"  P90 / P95:      {summary.get('return_p90', 0):+.2f}% / {summary.get('return_p95', 0):+.2f}%",
        "",
        "Sharpe distribution:",
        f"  Mean / Median:  {summary.get('sharpe_mean', 0):.2f} / {summary.get('sharpe_median', 0):.2f}",
        f"  P5 / P95:       {summary.get('sharpe_p5', 0):.2f} / {summary.get('sharpe_p95', 0):.2f}",
        "",
        "Max drawdown distribution (%):",
        f"  Mean / Median:  {summary.get('max_dd_mean', 0):.2f}% / {summary.get('max_dd_median', 0):.2f}%",
        f"  Worst 5% (P5):  {summary.get('max_dd_p5', 0):.2f}%",
        f"  P95:            {summary.get('max_dd_p95', 0):.2f}%",
        "",
        "Probabilities:",
        f"  Loss (<0%):     {summary.get('prob_loss', 0):.1f}%",
        f"  Breakeven (±1%):{summary.get('prob_breakeven', 0):.1f}%",
        f"  Beat VTI:       {summary.get('prob_beat_vti', 0):.1f}%",
        f"  Success (>0%):  {summary.get('success_rate', 0):.1f}%",
    ]
    if summary.get("full_period_return_pct") is not None:
        lines.extend(
            [
                "",
                "Vs full-period backtest:",
                f"  Actual return:  {summary.get('full_period_return_pct', 0):+.2f}% "
                f"(percentile {summary.get('full_period_return_percentile', 0):.0f} in MC dist)",
                f"  Actual Sharpe:  {summary.get('full_period_sharpe', 0):.2f}",
                f"  Actual Max DD:  {summary.get('full_period_max_dd_pct', 0):.2f}%",
            ]
        )
    lines.extend(
        [
            "",
            "Return histogram (mid-bin | bar (count)):",
            _ascii_histogram(summary.get("returns") or []),
        ]
    )
    return "\n".join(lines)


def _mc_worker_run(
    run_id: int,
    data: pd.DataFrame,
    min_history: int,
    block_size: int,
    seed: int | None,
    backtest_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Process-pool worker: regenerate synthetic path and run backtest."""
    rng = np.random.default_rng((seed or 0) + int(run_id))
    synth = synthetic_price_matrix(
        data,
        min_history=min_history,
        block_size=block_size,
        rng=rng,
    )
    from backtester import run_backtest

    result = run_backtest(synth, **backtest_kwargs)
    bench = result.get("benchmark_return_pct")
    if bench is None:
        bench = _benchmark_return_on_data(synth, min_history)
        result["benchmark_return_pct"] = bench
    return {
        "run_id": run_id,
        "total_return_pct": result.get("total_return_pct"),
        "sharpe": result.get("sharpe"),
        "max_drawdown_pct": result.get("max_drawdown_pct"),
        "benchmark_return_pct": bench,
        "profit_factor": result.get("profit_factor"),
    }


def _single_mc_run(
    run_id: int,
    data: pd.DataFrame,
    *,
    min_history: int,
    block_size: int,
    seed: int | None,
    run_fn: Callable[[pd.DataFrame], dict],
) -> dict[str, Any]:
    rng = np.random.default_rng((seed or 0) + int(run_id))
    synth = synthetic_price_matrix(
        data,
        min_history=min_history,
        block_size=block_size,
        rng=rng,
    )
    result = run_fn(synth)
    bench = result.get("benchmark_return_pct")
    if bench is None:
        bench = _benchmark_return_on_data(synth, min_history)
    return {
        "run_id": run_id,
        "total_return_pct": result.get("total_return_pct"),
        "sharpe": result.get("sharpe"),
        "max_drawdown_pct": result.get("max_drawdown_pct"),
        "benchmark_return_pct": bench,
        "profit_factor": result.get("profit_factor"),
    }


def run_monte_carlo_analysis(
    data: pd.DataFrame,
    *,
    min_history: int,
    n_runs: int = DEFAULT_MC_RUNS,
    run_fn: Callable[[pd.DataFrame], dict] | None = None,
    backtest_kwargs: dict[str, Any] | None = None,
    seed: int | None = None,
    block_size: int = DEFAULT_MC_BLOCK_SIZE,
    full_period_result: dict[str, Any] | None = None,
    parallel: bool = False,
    max_workers: int = 4,
    progress_fn: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Run N Monte Carlo simulations with block-bootstrapped synthetic price paths."""
    n_runs = max(1, int(n_runs))
    block_size = max(1, int(block_size))
    sim_len = len(data) - min_history
    if sim_len < 20:
        return {
            "ok": False,
            "error": f"Need at least 20 simulation bars; got {sim_len}",
            "n_runs": 0,
        }

    bt_kwargs = backtest_kwargs or _MC_BACKTEST_KWARGS or {}
    use_pool = bool(parallel and max_workers > 1 and n_runs >= 4 and bt_kwargs)
    runs: list[dict[str, Any]] = []
    data_copy = data.copy()

    if use_pool:
        workers = min(max_workers, n_runs)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _mc_worker_run,
                    i,
                    data_copy,
                    min_history,
                    block_size,
                    seed,
                    bt_kwargs,
                ): i
                for i in range(n_runs)
            }
            done = 0
            for fut in as_completed(futures):
                runs.append(fut.result())
                done += 1
                if progress_fn:
                    progress_fn(done, n_runs)
        runs.sort(key=lambda r: r.get("run_id", 0))
    else:
        if run_fn is None:
            from backtester import run_backtest

            run_fn = lambda d: run_backtest(d, **bt_kwargs)
        for i in range(n_runs):
            runs.append(
                _single_mc_run(
                    i,
                    data_copy,
                    min_history=min_history,
                    block_size=block_size,
                    seed=seed,
                    run_fn=run_fn,
                )
            )
            if progress_fn and ((i + 1) % 25 == 0 or i + 1 == n_runs):
                progress_fn(i + 1, n_runs)

    summary = aggregate_monte_carlo_runs(runs, full_period=full_period_result)
    report = format_monte_carlo_report(summary, block_size=block_size, seed=seed)
    return {
        "ok": True,
        "n_runs": n_runs,
        "block_size": block_size,
        "seed": seed,
        "runs": runs,
        "summary": summary,
        "report": report,
    }
