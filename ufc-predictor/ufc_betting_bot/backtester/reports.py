"""Console and file reports for backtests."""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

logger = logging.getLogger(__name__)


def print_backtest_summary(result) -> None:
    m = result.overall_metrics
    print(f"\n  UFC BETTING BOT — BACKTEST {result.target_year}")
    print("  " + "=" * 72)
    print(f"  Fights scored     {int(m.get('n_fights', 0))}")
    print(f"  Events            {len(result.per_event)}")
    print(f"  Accuracy          {m.get('accuracy', 0):.1%}")
    print(f"  Log loss          {m.get('log_loss', 0):.3f}")
    print(f"  Brier score       {m.get('brier_score', 0):.3f}")
    print(f"  ROC AUC           {m.get('roc_auc', float('nan')):.3f}")
    print(f"  Fights w/ odds    {result.fights_with_odds}")

    print("\n  SEGMENT ACCURACY")
    print("  " + "-" * 40)
    for seg, sm in result.segment_metrics.items():
        print(
            f"  {seg:<14} {int(sm.get('n_fights', 0)):>4} fights  "
            f"acc {sm.get('accuracy', 0):.1%}"
        )

    staking = getattr(result, "staking_modes", pd.DataFrame())
    if isinstance(staking, pd.DataFrame) and not staking.empty:
        print("\n  STAKING MODES (min edge from settings)")
        print("  " + "-" * 72)
        for _, row in staking.iterrows():
            warn = ""
            if row.get("roi_warning"):
                warn = "  <<< check"
                logger.warning("%s: %s", row.get("staking"), row["roi_warning"])
            mc_dd = row.get("mc_expected_max_drawdown_pct")
            mc_var = row.get("mc_var_max_drawdown_pct")
            mc_ruin = row.get("mc_ruin_probability")
            mc_extra = ""
            if pd.notna(mc_dd):
                mc_extra = (
                    f"  MC expDD {float(mc_dd):.1f}%"
                    f"  VaR {float(mc_var):.1f}%"
                    f"  ruin {float(mc_ruin):.1%}"
                )
            print(
                f"  {str(row.get('staking', '')):<14} "
                f"trades {int(row.get('trades', 0)):>4}  "
                f"hit {row.get('hit_rate', 0):.1%}  "
                f"ROI {row.get('roi_pct', 0):>7.1f}%  "
                f"maxDD {row.get('max_drawdown_pct', 0):.1f}%  "
                f"winStk {int(row.get('max_win_streak', 0))}{warn}{mc_extra}"
            )

    mc = getattr(result, "monte_carlo", None)
    if mc is not None and getattr(mc, "staking_summaries", None):
        print("\n  MONTE CARLO RISK ({:,} sims, {} outcomes)".format(
            mc.n_simulations, mc.outcome_mode
        ))
        print("  " + "-" * 72)
        for mode, stats in mc.staking_summaries.items():
            print(
                f"  {mode:<14} "
                f"expDD {stats.get('expected_max_drawdown_pct', 0):.1f}%  "
                f"VaR DD {stats.get('var_max_drawdown_pct', 0):.1f}%  "
                f"CVaR DD {stats.get('cvar_max_drawdown_pct', 0):.1f}%  "
                f"ruin {stats.get('ruin_probability', 0):.1%}  "
                f"VaR ret {stats.get('var_return_pct', 0):+.1f}%"
            )
        for w in getattr(mc, "warnings", []) or []:
            print(f"  WARNING: {w}")
            logger.warning("Monte Carlo: %s", w)

    if not result.bankroll_sweep.empty:
        print("\n  FRACTIONAL KELLY SWEEP")
        print("  " + "-" * 56)
        for _, row in result.bankroll_sweep.iterrows():
            roi = row.get("roi_pct", 0)
            flag = ""
            if roi > 500:
                flag = "  <<< unrealistic?"
                logger.warning(
                    "Kelly sweep edge>=%s%% ROI %.1f%% looks unrealistically high",
                    int(row.get("min_edge", 0) * 100),
                    roi,
                )
            print(
                f"  edge >= {row['min_edge']:.0%}  "
                f"trades {int(row.get('trades', 0)):>4}  "
                f"hit {row.get('hit_rate', 0):.1%}  "
                f"ROI {roi:>7.1f}%  "
                f"maxDD {row.get('max_drawdown_pct', 0):.1f}%{flag}"
            )

    if not result.per_event.empty:
        print("\n  PER-EVENT (top 10 by fights)")
        print("  " + "-" * 72)
        top = result.per_event.sort_values("fights", ascending=False).head(10)
        for _, row in top.iterrows():
            odds_n = int(row.get("fights_with_odds", 0))
            print(
                f"  {str(row['event'])[:38]:<38} "
                f"{int(row['fights']):>3} fights  "
                f"acc {row['accuracy']:.1%}  "
                f"odds {odds_n}"
            )

    print(f"\n  Reports -> {result.report_dir}")
    print()


def save_calibration_plot(bins: pd.DataFrame, path: Path) -> None:
    if bins.empty:
        return
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.plot([0, 1], [0, 1], "k--", alpha=0.4, label="Perfect")
    ax.scatter(bins["mean_predicted"], bins["fraction_positive"], s=bins["count"] * 3)
    ax.set_xlabel("Mean predicted probability")
    ax.set_ylabel("Observed win rate")
    ax.set_title("Calibration reliability")
    ax.legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def save_roi_plot(sweep: pd.DataFrame, path: Path) -> None:
    if sweep.empty:
        return
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(sweep["min_edge"] * 100, sweep["roi_pct"], width=1.5)
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xlabel("Min edge (%)")
    ax.set_ylabel("ROI (%)")
    ax.set_title("Fractional Kelly ROI by edge threshold")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
