"""A/B: NYSE anti-overlap vs SPY sleeve (corr/beta filter, sector tech cap).

Run from repo root:
  python scripts/analysis/nyse_anti_overlap_ab.py
  python scripts/analysis/nyse_anti_overlap_ab.py --max
  python scripts/analysis/nyse_anti_overlap_ab.py --days 750
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import (
    BENCHMARK,
    DAILY_COOLDOWN_BARS,
    MIN_HISTORY,
    BacktestExecutor,
    BacktestPortfolio,
    _benchmark_return,
    _ensure_daily_data,
)
from modules.market_context import (
    get_market_regime,
    get_price_sentiment,
    get_volatility,
)
from modules.pipeline_strategies import (
    NYSE_SECTOR_MAP,
    PAUSED_REGIMES,
    _equity_momentum_candidates,
    _equity_momentum_ranked,
    _on_cooldown,
    _spy_sleeve_active,
    _spy_vs_equity_metrics,
    run_crypto_strategy,
    run_equity_strategy,
    run_spy_strategy,
)
from modules.risk_management import RiskManager

OUT_MD = Path(__file__).with_name("nyse_anti_overlap_results.md")

VARIANTS = [
    ("baseline", False, 0.80, 0),
    ("corr_0.75", True, 0.75, 0),
    ("corr_0.80", True, 0.80, 0),
    ("corr_0.85", True, 0.85, 0),
    ("sector_tech_cap", False, 0.80, 1),
]


@dataclass
class VariantConfig:
    anti_overlap: bool
    corr_max: float
    sector_tech_cap: int


@contextlib.contextmanager
def _config_patch(v: VariantConfig):
    saved = (
        config.NYSE_ANTI_OVERLAP_ENABLED,
        config.NYSE_SPY_CORR_MAX,
        config.NYSE_SECTOR_TECH_CAP,
    )
    config.NYSE_ANTI_OVERLAP_ENABLED = v.anti_overlap
    config.NYSE_SPY_CORR_MAX = v.corr_max
    config.NYSE_SECTOR_TECH_CAP = v.sector_tech_cap
    try:
        yield
    finally:
        (
            config.NYSE_ANTI_OVERLAP_ENABLED,
            config.NYSE_SPY_CORR_MAX,
            config.NYSE_SECTOR_TECH_CAP,
        ) = saved


def _nyse_universe_columns(data: pd.DataFrame) -> list[str]:
    return [
        c
        for c in data.columns
        if not config.is_crypto(c)
        and c != config.SPY_BOT_SYMBOL
        and not config.is_metal_symbol(c)
    ]


def run_backtest(
    data: pd.DataFrame,
    variant: VariantConfig,
    *,
    initial_capital: float = 10_000.0,
) -> dict:
    with _config_patch(variant):
        portfolio = BacktestPortfolio(initial_capital)
        pair_cooldown = {}
        risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
        equity_curve = []
        total_crypto = total_equity = total_spy = 0
        halted = False
        cofire_days = 0
        pick_changes = 0
        spy_active_days = 0
        filt_corrs: list[float] = []
        tech_picks = 0
        tech_pick_bars = 0
        equity_cols = _nyse_universe_columns(data)
        metrics_cooldown: dict = {}

        for i in range(MIN_HISTORY, len(data)):
            window = data.iloc[: i + 1]
            prices = window.iloc[-1]
            eq = portfolio.equity(prices)
            equity_curve.append(eq)

            if halted or not risk_manager.check_drawdown(eq):
                halted = True
                continue

            sentiment = get_price_sentiment(window)
            vol = get_volatility(window)
            regime = get_market_regime(sentiment, vol)
            executor = BacktestExecutor(portfolio, prices)

            total_crypto += run_crypto_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=DAILY_COOLDOWN_BARS,
                volatility=vol,
            )
            total_spy += run_spy_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=DAILY_COOLDOWN_BARS,
            )
            total_equity += run_equity_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=DAILY_COOLDOWN_BARS,
            )

            spy_sym = config.SPY_BOT_SYMBOL
            spy_active = _spy_sleeve_active(
                window, yield_gated=False, regime=regime
            )
            if spy_active:
                spy_active_days += 1
            spy_key = f"{spy_sym}/MA{config.SPY_MA_WINDOW}"
            spy_fires = spy_active and not _on_cooldown(
                metrics_cooldown, spy_key, i, cooldown_bars=DAILY_COOLDOWN_BARS
            )
            if spy_fires:
                metrics_cooldown[spy_key] = i

            raw_ranked = _equity_momentum_candidates(window, equity_cols)
            raw_top = raw_ranked[0] if raw_ranked else None
            filtered_ranked = _equity_momentum_ranked(
                window, equity_cols, yield_gated=False, regime=regime
            )
            filt_top = filtered_ranked[0] if filtered_ranked else None
            if spy_active and raw_top and filt_top and raw_top != filt_top:
                pick_changes += 1
            if spy_active and filt_top:
                c, _ = _spy_vs_equity_metrics(window, filt_top)
                filt_corrs.append(c)
                if NYSE_SECTOR_MAP.get(filt_top, "") == "Tech":
                    tech_picks += 1
                tech_pick_bars += 1

            nyse_key = f"{filt_top}/MA50" if filt_top else None
            nyse_fires = (
                bool(filtered_ranked)
                and regime not in PAUSED_REGIMES
                and nyse_key
                and not _on_cooldown(
                    metrics_cooldown, nyse_key, i, cooldown_bars=DAILY_COOLDOWN_BARS
                )
            )
            if nyse_fires:
                metrics_cooldown[nyse_key] = i
            if spy_fires and nyse_fires:
                cofire_days += 1

        curve = pd.Series(equity_curve)
        returns = curve.pct_change().dropna()
        total_ret = (curve.iloc[-1] / portfolio.initial_capital - 1) * 100
        sharpe = (
            (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0
        )
        max_dd = ((curve / curve.cummax()) - 1).min() * 100
        bench = _benchmark_return(data, MIN_HISTORY)

        tech_pct = (
            100.0 * tech_picks / tech_pick_bars if tech_pick_bars else 0.0
        )

        return {
            "total_return_pct": round(total_ret, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "benchmark_pct": round(bench, 2) if bench is not None else None,
            "spy_signals": total_spy,
            "nyse_signals": total_equity,
            "crypto_signals": total_crypto,
            "cofire_days": cofire_days,
            "spy_active_days": spy_active_days,
            "pick_changes_when_spy": pick_changes,
            "avg_filt_corr_when_spy": round(float(np.mean(filt_corrs)), 3)
            if filt_corrs
            else None,
            "tech_pick_pct_when_spy": round(tech_pct, 1),
            "start": str(data.index[MIN_HISTORY].date()),
            "end": str(data.index[-1].date()),
        }


def _slice_recent(data: pd.DataFrame, days: int) -> pd.DataFrame:
    if len(data) <= days + MIN_HISTORY:
        return data
    return data.iloc[-(days + MIN_HISTORY) :]


def _fmt_row(label: str, r: dict) -> str:
    return (
        f"| {label} "
        f"| {r['total_return_pct']:+.2f}% "
        f"| {r['sharpe']:.2f} "
        f"| {r['max_drawdown_pct']:.2f}% "
        f"| {r['cofire_days']} "
        f"| {r['pick_changes_when_spy']} "
        f"| {r.get('avg_filt_corr_when_spy', '—')} "
        f"| {r['tech_pick_pct_when_spy']:.1f}% |"
    )


def _recommend(all_rows: list[dict]) -> str:
    win = "recent_750d" if any(r["window"] == "recent_750d" for r in all_rows) else "full"
    by_name = {r["variant"]: r for r in all_rows if r["window"] == win}
    if "baseline" not in by_name:
        return "Insufficient results for recommendation."
    base = by_name["baseline"]
    corr_vars = [k for k in by_name if k.startswith("corr_")]
    best_corr = max(
        corr_vars,
        key=lambda k: (by_name[k]["sharpe"], by_name[k]["total_return_pct"]),
    )
    lines = [
        f"Window: **{win}**. Best corr-threshold variant: **{best_corr}** "
        f"(Sharpe {by_name[best_corr]['sharpe']:.2f} vs baseline {base['sharpe']:.2f}).",
    ]
    if "sector_tech_cap" in by_name:
        sec = by_name["sector_tech_cap"]
        lines.append(
            f"Sector tech-cap: Sharpe {sec['sharpe']:.2f}, tech picks {sec['tech_pick_pct_when_spy']:.1f}% "
            f"(baseline {base['tech_pick_pct_when_spy']:.1f}%)."
        )
    bc = by_name[best_corr]
    if bc["sharpe"] > base["sharpe"] + 0.02:
        thresh = best_corr.replace("corr_", "")
        lines.append(
            f"\n**Recommend NYSE_SPY_CORR_MAX={thresh}** with NYSE_ANTI_OVERLAP_ENABLED=true "
            f"(beta cap {config.NYSE_SPY_BETA_MAX} unchanged)."
        )
    elif by_name.get("sector_tech_cap", {}).get("sharpe", 0) > base["sharpe"] + 0.02:
        lines.append(
            "\n**Recommend sector variant** NYSE_SECTOR_TECH_CAP=1 (corr filter optional)."
        )
    else:
        d_ret = base["total_return_pct"] - bc["total_return_pct"]
        lines.append(
            f"\n**Recommend baseline (no corr filter).** Corr filter costs ~{d_ret:.0f} pp return "
            f"and {base['sharpe'] - bc['sharpe']:.2f} Sharpe on {win}; co-fire days unchanged "
            f"({base['cofire_days']}). Lower avg NYSE-SPY corr ({bc['avg_filt_corr_when_spy']} vs "
            f"{base['avg_filt_corr_when_spy']}) but worse portfolio metrics. "
            "NYSE_SECTOR_TECH_CAP=1 has no effect with max_trades=1 — test via rebalance top-3."
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=0, help="Recent window (0 = full only)")
    parser.add_argument("--max", action="store_true", help="Use max daily history")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    if args.max:
        data = _ensure_daily_data(0, refresh=args.refresh, use_max=True)
    else:
        days = args.days or 2000
        data = _ensure_daily_data(days, refresh=args.refresh, use_max=False)

    windows = [("full", data)]
    if len(data) > MIN_HISTORY + 750:
        windows.append(("recent_750d", _slice_recent(data, 750)))

    all_rows: list[dict] = []
    for win_label, win_data in windows:
        for name, anti, corr, tech_cap in VARIANTS:
            row = run_backtest(
                win_data,
                VariantConfig(anti, corr, tech_cap),
            )
            row["variant"] = name
            row["window"] = win_label
            all_rows.append(row)
            print(
                f"{win_label} {name}: ret {row['total_return_pct']:+.2f}% "
                f"Sharpe {row['sharpe']:.2f} cofire {row['cofire_days']}"
            )

    header = (
        "| Variant | Return | Sharpe | Max DD | Co-fire days | Pick swaps | "
        "Avg NYSE-SPY corr | Tech % (SPY on) |"
    )
    sep = "|" + "---|" * 8

    sections = [
        "# NYSE anti-overlap A/B",
        "",
        f"Benchmark ({BENCHMARK}) on full window: "
        f"{all_rows[0].get('benchmark_pct', 'n/a')}%",
        "",
        "Co-fire = SPY and NYSE both fired on same bar (cooldown-aware). "
        "Pick swaps = filtered top ≠ raw top when SPY sleeve active.",
        "",
    ]
    for win_label, _ in windows:
        win_rows = [r for r in all_rows if r["window"] == win_label]
        sections.append(f"## {win_label} ({win_rows[0]['start']} → {win_rows[0]['end']})")
        sections.append("")
        sections.append(header)
        sections.append(sep)
        for r in win_rows:
            sections.append(_fmt_row(r["variant"], r))
        sections.append("")

    sections.append("## Recommendation")
    sections.append("")
    sections.append(_recommend(all_rows))

    text = "\n".join(sections)
    OUT_MD.write_text(text, encoding="utf-8")
    print(f"\nWrote {OUT_MD}")


if __name__ == "__main__":
    main()
