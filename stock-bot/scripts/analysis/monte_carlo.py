#!/usr/bin/env python3
"""Monte Carlo shuffle+noise stress test on trade-level PnL CSVs.

Standalone analysis — does not modify production modules.

Reads trade returns from a CSV with a pnl_pct column (default:
scripts/research/intraday_backtest_results.csv), then runs 1,000
shuffle + noise simulations from $100,000 starting capital.

Run:
  python scripts/analysis/monte_carlo.py
  python scripts/analysis/monte_carlo.py --csv scripts/research/crypto_vol_backtest_v4_results.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = ROOT / "scripts" / "research" / "intraday_backtest_results.csv"
OUT_JSON = Path(__file__).resolve().parent / "monte_carlo_results.json"

N_ITERS = 1_000
START_EQUITY = 100_000.0
NOISE_PCT = 0.003  # ±0.3% absolute noise on each trade return
RNG_SEED = 42


def load_trade_returns(csv_path: Path) -> np.ndarray:
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    df = pd.read_csv(csv_path)
    col = None
    for candidate in ("pnl_pct", "pnl%", "return_pct", "pnl"):
        if candidate in df.columns:
            col = candidate
            break
    if col is None:
        raise ValueError(
            f"No pnl_pct column in {csv_path}. Columns: {list(df.columns)}"
        )
    rets = pd.to_numeric(df[col], errors="coerce").dropna().to_numpy(dtype=float)
    # Accept percent units (e.g. 1.5 = +1.5%) — convert to fraction
    # Heuristic: if median abs > 0.5, treat as percent points
    if len(rets) and np.nanmedian(np.abs(rets)) > 0.5:
        rets = rets / 100.0
    if len(rets) < 2:
        raise ValueError(f"Need at least 2 trades with valid pnl; got {len(rets)}")
    return rets


def compound_path(returns: np.ndarray, start: float = START_EQUITY) -> tuple[float, float, np.ndarray]:
    """Compound trade returns; return final equity, max DD (fraction), equity path."""
    equity = start
    peak = start
    max_dd = 0.0
    path = np.empty(len(returns) + 1, dtype=float)
    path[0] = start
    for i, r in enumerate(returns):
        equity *= 1.0 + float(r)
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
        path[i + 1] = equity
    return equity, max_dd, path


def run_monte_carlo(
    base_returns: np.ndarray,
    *,
    n_iters: int = N_ITERS,
    noise_pct: float = NOISE_PCT,
    seed: int = RNG_SEED,
) -> dict:
    rng = np.random.default_rng(seed)
    n = len(base_returns)

    orig_final, orig_dd, orig_path = compound_path(base_returns)

    finals = np.empty(n_iters, dtype=float)
    max_dds = np.empty(n_iters, dtype=float)
    # Fan chart: store equity at each trade index for percentile bands
    # Downsample path storage: keep every path's endpoint series aligned by trade #
    paths = np.empty((n_iters, n + 1), dtype=float)

    for i in range(n_iters):
        order = rng.permutation(n)
        shuffled = base_returns[order]
        noise = rng.uniform(-noise_pct, noise_pct, size=n)
        noisy = shuffled + noise
        # Floor at -99% so a single trade can't go negative equity
        noisy = np.maximum(noisy, -0.99)
        final, dd, path = compound_path(noisy)
        finals[i] = final
        max_dds[i] = dd
        paths[i] = path

    median_final = float(np.median(finals))
    p5 = float(np.percentile(finals, 5))
    p95 = float(np.percentile(finals, 95))
    p_loss = float(np.mean(finals < START_EQUITY))
    p_dd20 = float(np.mean(max_dds > 0.20))
    p_dd30 = float(np.mean(max_dds > 0.30))

    # Percentile rank of original final equity among simulations
    # (fraction of sims with final <= original) * 100
    pct_rank = float(np.mean(finals <= orig_final) * 100.0)

    if pct_rank >= 90:
        verdict = "OVERFITTING RISK"
        verdict_emoji = "⚠️"
    elif pct_rank >= 50:
        verdict = "EDGE CONFIRMED"
        verdict_emoji = "✅"
    else:
        verdict = "WEAK EDGE"
        verdict_emoji = "❌"

    # Fan chart percentiles across iterations at each trade step
    fan_p5 = np.percentile(paths, 5, axis=0).tolist()
    fan_p50 = np.percentile(paths, 50, axis=0).tolist()
    fan_p95 = np.percentile(paths, 95, axis=0).tolist()

    return {
        "n_trades": n,
        "n_iters": n_iters,
        "start_equity": START_EQUITY,
        "noise_pct": noise_pct,
        "original": {
            "final_equity": round(orig_final, 2),
            "max_drawdown_pct": round(orig_dd * 100.0, 2),
            "total_return_pct": round((orig_final / START_EQUITY - 1.0) * 100.0, 2),
            "equity_path": [round(float(x), 2) for x in orig_path.tolist()],
        },
        "simulations": {
            "median_final_equity": round(median_final, 2),
            "p5_final_equity": round(p5, 2),
            "p95_final_equity": round(p95, 2),
            "prob_loss": round(p_loss, 4),
            "prob_dd_gt_20pct": round(p_dd20, 4),
            "prob_dd_gt_30pct": round(p_dd30, 4),
            "original_percentile_rank": round(pct_rank, 2),
        },
        "verdict": verdict,
        "verdict_emoji": verdict_emoji,
        "fan_chart": {
            "trade_index": list(range(n + 1)),
            "p5": [round(float(x), 2) for x in fan_p5],
            "p50": [round(float(x), 2) for x in fan_p50],
            "p95": [round(float(x), 2) for x in fan_p95],
        },
    }


def print_summary(result: dict, csv_path: Path) -> None:
    sim = result["simulations"]
    orig = result["original"]
    print("\n=== Monte Carlo Trade Shuffle + Noise ===")
    print(f"Source:     {csv_path}")
    print(f"Trades:     {result['n_trades']}")
    print(f"Iterations: {result['n_iters']:,}")
    print(f"Start:      ${result['start_equity']:,.0f}")
    print(f"Noise:      ±{result['noise_pct']*100:.1f}% per trade")
    print()
    header = f"{'Metric':<32} {'Value':>18}"
    print(header)
    print("-" * len(header))
    rows = [
        ("Original final equity", f"${orig['final_equity']:,.2f}"),
        ("Original total return", f"{orig['total_return_pct']:+.2f}%"),
        ("Original max DD", f"{orig['max_drawdown_pct']:.2f}%"),
        ("Median final equity", f"${sim['median_final_equity']:,.2f}"),
        ("5th pct (worst case)", f"${sim['p5_final_equity']:,.2f}"),
        ("95th pct (best case)", f"${sim['p95_final_equity']:,.2f}"),
        ("P(loss) equity < start", f"{sim['prob_loss']*100:.1f}%"),
        ("P(max DD > 20%)", f"{sim['prob_dd_gt_20pct']*100:.1f}%"),
        ("P(max DD > 30%)", f"{sim['prob_dd_gt_30pct']*100:.1f}%"),
        ("Original percentile rank", f"{sim['original_percentile_rank']:.1f}th"),
        ("Verdict", f"{result['verdict_emoji']} {result['verdict']}"),
    ]
    for label, val in rows:
        print(f"{label:<32} {val:>18}")

    print("\n--- Plain English ---")
    print(_plain_english(result))


def _ordinal(n: float) -> str:
    i = int(round(n))
    if 10 <= (i % 100) <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(i % 10, "th")
    return f"{n:.0f}{suf}"


def _plain_english(result: dict) -> str:
    sim = result["simulations"]
    orig = result["original"]
    verdict = result["verdict"]
    n = result["n_trades"]
    med = sim["median_final_equity"]
    p5 = sim["p5_final_equity"]
    p95 = sim["p95_final_equity"]
    ploss = sim["prob_loss"] * 100
    rank = sim["original_percentile_rank"]
    start = result["start_equity"]
    rank_ord = _ordinal(rank)

    if verdict == "OVERFITTING RISK":
        edge_line = (
            f"The original backtest finished in the top {100 - rank:.0f}% of random "
            f"reshuffles (≈{rank_ord} percentile), which is unusually lucky — treat "
            f"live expectations with caution; the edge may be overfit to trade order."
        )
    elif verdict == "EDGE CONFIRMED":
        edge_line = (
            f"The original result sits around the {rank_ord} percentile of reshuffles — "
            f"better than a coin-flip but not an extreme outlier, which is consistent "
            f"with a real (modest) edge rather than pure curve-fitting."
        )
    else:
        edge_line = (
            f"The original result lands only at the {rank_ord} percentile — worse than "
            f"most random reorderings — so the historical path may have been unlucky, "
            f"or the strategy edge is weak once trade order is scrambled."
        )

    return (
        f"Starting from ${start:,.0f} and replaying {n} trades 1,000 times (shuffled order "
        f"plus tiny ±0.3% noise), the typical ending pile was about ${med:,.0f}. "
        f"In a bad run (5th percentile) you finish near ${p5:,.0f}; in a good run "
        f"(95th percentile) around ${p95:,.0f}. About {ploss:.0f}% of simulations ended "
        f"below the starting capital. "
        f"The actual backtest closed at ${orig['final_equity']:,.0f} "
        f"({orig['total_return_pct']:+.1f}%, max DD {orig['max_drawdown_pct']:.1f}%). "
        f"{edge_line}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Monte Carlo shuffle+noise on trade PnL CSV")
    parser.add_argument(
        "--csv",
        type=str,
        default=str(DEFAULT_CSV),
        help="Trade log CSV with pnl_pct column",
    )
    parser.add_argument("--iters", type=int, default=N_ITERS)
    parser.add_argument("--seed", type=int, default=RNG_SEED)
    parser.add_argument(
        "--out",
        type=str,
        default=str(OUT_JSON),
        help="Output JSON path for fan chart + summary",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        csv_path = (Path.cwd() / csv_path).resolve()
        if not csv_path.is_file():
            alt = (ROOT / args.csv).resolve()
            if alt.is_file():
                csv_path = alt

    print(f"Loading trades from {csv_path} ...")
    try:
        returns = load_trade_returns(csv_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Running {args.iters:,} Monte Carlo iterations on {len(returns)} trades ...")
    result = run_monte_carlo(returns, n_iters=args.iters, seed=args.seed)
    result["source_csv"] = str(csv_path)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print_summary(result, csv_path)
    print(f"\nSaved fan chart + stats: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
