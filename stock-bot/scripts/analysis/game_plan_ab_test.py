"""A/B test: baseline vs full game plan vs yield-gate-only.

Run:
  python scripts/analysis/game_plan_ab_test.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backtester import MIN_HISTORY
from backtester_metals import (
    load_fund_with_metals,
    run_fresh_capital_backtest,
    run_metals_backtest,
)
from backtester_wisdom import _slice_data

STRATEGIES = ("baseline", "game_plan_gld_slv_cper", "yield_gate_only")
FULL_LIVE = "game_plan_gld_slv_cper"
OUT_PATH = Path(__file__).with_name("game_plan_ab_results.md")


def _slice_recent(data: pd.DataFrame, days: int = 750) -> pd.DataFrame:
    """Last N trading days with MIN_HISTORY warmup retained."""
    if len(data) <= days + MIN_HISTORY:
        return data
    return data.iloc[-(days + MIN_HISTORY) :]


def _run_window(
    data: pd.DataFrame,
    label: str,
    *,
    fresh: bool = False,
    reset_date: str = "2022-01-01",
    end_date: str = "2022-12-31",
) -> list[dict]:
    rows = []
    for name in STRATEGIES:
        if fresh:
            row = run_fresh_capital_backtest(
                data,
                name,
                reset_date=reset_date,
                end_date=end_date,
                initial_capital=10_000.0,
            )
        else:
            row = run_metals_backtest(data, name, initial_capital=10_000.0)
        row["window"] = label
        rows.append(row)
    return rows


def _fmt_table(rows: list[dict]) -> str:
    lines = [
        "| Strategy | Return | Sharpe | Max DD | Gate days | Cash trims | Metal $ |",
        "|----------|--------|--------|--------|-----------|------------|---------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['strategy']} "
            f"| {r['total_return_pct']:+.2f}% "
            f"| {r['sharpe']:.2f} "
            f"| {r['max_drawdown_pct']:.2f}% "
            f"| {r.get('yield_gate_days', 0)} "
            f"| {r.get('cash_trims', 0)} "
            f"| ${r.get('metal_final', 0):,.0f} |"
        )
    return "\n".join(lines)


def _delta_note(rows: list[dict], variant: str, baseline: str = "baseline") -> str:
    base = next(r for r in rows if r["strategy"] == baseline)
    var = next(r for r in rows if r["strategy"] == variant)
    return (
        f"**{variant}** vs baseline: "
        f"return {var['total_return_pct'] - base['total_return_pct']:+.2f} pp, "
        f"Sharpe {var['sharpe'] - base['sharpe']:+.2f}, "
        f"MaxDD {var['max_drawdown_pct'] - base['max_drawdown_pct']:+.2f} pp"
    )


def _recommend(all_rows: list[dict]) -> str:
    """Pick best variant by Sharpe across windows (tie-break: return)."""
    scores: dict[str, list[float]] = {s: [] for s in STRATEGIES}
    for r in all_rows:
        scores[r["strategy"]].append(r["sharpe"])
    avg_sharpe = {s: sum(v) / len(v) for s, v in scores.items()}
    best = max(STRATEGIES, key=lambda s: (avg_sharpe[s], scores[s][-1]))
    lines = ["## Recommendation", ""]
    lines.append(f"Average Sharpe across windows: " + ", ".join(
        f"{s}={avg_sharpe[s]:.2f}" for s in STRATEGIES
    ))
    if best == "yield_gate_only":
        lines.append(
            "\n**Adopt yield-gate-only.** It keeps the macro SPY filter without metal "
            "sleeve drag, stress cash trims, or 0.9 long scaling. Set "
            "`GAME_PLAN_YIELD_GATE_ONLY=true` and `GAME_PLAN_ENABLED=false` (or disable "
            "metal/stress in live via the flag)."
        )
    elif best == FULL_LIVE:
        lines.append(
            "\n**Keep full game plan** (metals + stress cash + yield gate + 0.9 scale). "
            "The simplified yield-gate-only variant did not improve risk-adjusted returns."
        )
    else:
        lines.append(
            "\n**Disable game plan** — baseline outperforms both variants on average Sharpe."
        )
    return "\n".join(lines)


def main() -> None:
    raw = load_fund_with_metals(refresh=False)
    all_rows: list[dict] = []

    # Full history 2017-2026
    full = _slice_data(raw, 2017, 2026)
    all_rows.extend(_run_window(full, "full_2017_2026"))

    # Fresh 2022 stress
    fresh_data = _slice_data(raw, 2017, 2022)
    all_rows.extend(
        _run_window(
            fresh_data,
            "fresh_2022",
            fresh=True,
            reset_date="2022-01-01",
            end_date="2022-12-31",
        )
    )

    # Recent ~750 days
    recent = _slice_recent(raw, days=750)
    all_rows.extend(_run_window(recent, "recent_750d"))

    sections = []
    for label in ("full_2017_2026", "fresh_2022", "recent_750d"):
        window_rows = [r for r in all_rows if r["window"] == label]
        start = window_rows[0]["start"]
        end = window_rows[0]["end"]
        sections.append(f"### {label} ({start} to {end})\n")
        sections.append(_fmt_table(window_rows))
        sections.append("")
        sections.append(_delta_note(window_rows, FULL_LIVE))
        sections.append(_delta_note(window_rows, "yield_gate_only"))
        sections.append("")

    md = "\n".join([
        "# Game Plan A/B Results",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Variants:",
        "- **baseline** — no game plan (full caps: SPY 45%, crypto 20%, NYSE 20%, 15% cash)",
        "- **game_plan_gld_slv_cper** — full plan (yield gate + 10% metal + stress cash + 0.9 long scale)",
        "- **yield_gate_only** — yield gate only (full caps, no metal, no stress cash)",
        "",
        *sections,
        _recommend(all_rows),
        "",
    ])

    OUT_PATH.write_text(md, encoding="utf-8")
    pd.DataFrame(all_rows).to_csv(OUT_PATH.with_suffix(".csv"), index=False)
    print(md)
    print(f"\nSaved -> {OUT_PATH}")
    print(f"Saved -> {OUT_PATH.with_suffix('.csv')}")


if __name__ == "__main__":
    main()
