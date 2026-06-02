"""A/B: drawdown halt resume/liquidation vs regime pause calibration.

Run from repo root:
  python scripts/analysis/risk_layer_ab.py
  python scripts/analysis/risk_layer_ab.py --days 500
  python scripts/analysis/risk_layer_ab.py --max
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import _ensure_daily_data, run_backtest

OUT_MD = Path(__file__).with_name("risk_layer_results.md")


@dataclass
class Variant:
    name: str
    halt_resume_pct: float | None = None
    liquidate: bool = False
    regime_thresh: float | None = None
    derived_bear: bool = False
    derived_bear_thresh: float | None = None


VARIANTS = [
    Variant("baseline", halt_resume_pct=0.0),
    Variant("halt_resume", halt_resume_pct=0.08),
    Variant("halt_resume_liquidate", halt_resume_pct=0.08, liquidate=True),
    Variant("derived_bear_pause", derived_bear=True, derived_bear_thresh=0.10),
    Variant("regime_thresh_0.10", regime_thresh=0.10),
    Variant(
        "combined_best",
        halt_resume_pct=0.08,
        liquidate=True,
        regime_thresh=0.10,
        derived_bear=True,
        derived_bear_thresh=0.10,
    ),
]


@contextlib.contextmanager
def _patch(v: Variant):
    saved = (
        config.HALT_RESUME_DRAWDOWN_PCT,
        config.HALT_LIQUIDATE_ON_BREACH,
        config.REGIME_SENTIMENT_THRESHOLD,
        config.DERIVED_BEAR_PAUSE_ENABLED,
        config.DERIVED_BEAR_SENTIMENT_THRESHOLD,
    )
    if v.halt_resume_pct is not None:
        config.HALT_RESUME_DRAWDOWN_PCT = v.halt_resume_pct
    config.HALT_LIQUIDATE_ON_BREACH = v.liquidate
    if v.regime_thresh is not None:
        config.REGIME_SENTIMENT_THRESHOLD = v.regime_thresh
    config.DERIVED_BEAR_PAUSE_ENABLED = v.derived_bear
    if v.derived_bear_thresh is not None:
        config.DERIVED_BEAR_SENTIMENT_THRESHOLD = v.derived_bear_thresh
    try:
        yield
    finally:
        (
            config.HALT_RESUME_DRAWDOWN_PCT,
            config.HALT_LIQUIDATE_ON_BREACH,
            config.REGIME_SENTIMENT_THRESHOLD,
            config.DERIVED_BEAR_PAUSE_ENABLED,
            config.DERIVED_BEAR_SENTIMENT_THRESHOLD,
        ) = saved


def run_variant(data, v: Variant) -> dict:
    with _patch(v):
        return run_backtest(data, track_spy_fill=False, verbose=False)


def _md_table(rows: list[dict]) -> str:
    hdr = (
        "| Variant | Return % | Sharpe | Max DD % | Halt | Resume | Pause days "
        "| Liq trims | Orders |"
    )
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    lines = [hdr, sep]
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['total_return_pct']:.2f} | {r['sharpe']:.2f} | "
            f"{r['max_drawdown_pct']:.2f} | {r['halt_events']} | {r['resume_events']} | "
            f"{r['pause_days']} | {r['halt_liquidations']} | {r['total_orders']} |"
        )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--max", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    data = _ensure_daily_data(0 if args.max else args.days, refresh=args.refresh, use_max=args.max)
    label = "max" if args.max else str(args.days)
    print(f"Risk layer A/B on {len(data)} daily rows ({label})")

    rows = []
    for v in VARIANTS:
        r = run_variant(data, v)
        r["name"] = v.name
        rows.append(r)
        print(
            f"  {v.name}: ret {r['total_return_pct']:.2f}% sharpe {r['sharpe']:.2f} "
            f"maxDD {r['max_drawdown_pct']:.2f}% halt {r['halt_events']}/{r['resume_events']} "
            f"pause {r['pause_days']}d"
        )

    baseline = rows[0]
    best_sharpe = max(rows, key=lambda x: x["sharpe"])
    best_dd = max(rows, key=lambda x: x["max_drawdown_pct"])

    rec_lines = []
    if best_sharpe["name"] != "baseline":
        rec_lines.append(
            f"Best Sharpe: **{best_sharpe['name']}** "
            f"({best_sharpe['sharpe']:.2f} vs baseline {baseline['sharpe']:.2f})."
        )
    if best_dd["name"] != "baseline":
        rec_lines.append(
            f"Smallest max DD: **{best_dd['name']}** "
            f"({best_dd['max_drawdown_pct']:.2f}% vs {baseline['max_drawdown_pct']:.2f}%)."
        )
    combined = next(r for r in rows if r["name"] == "combined_best")
    if (
        combined["sharpe"] >= baseline["sharpe"] - 0.05
        and combined["max_drawdown_pct"] >= baseline["max_drawdown_pct"] - 1.0
    ):
        rec_lines.append(
            "Enable combined stack: halt resume 8%, liquidation on breach, "
            "REGIME_SENTIMENT_THRESHOLD=0.10, DERIVED_BEAR_PAUSE with threshold 0.10."
        )
    elif rows[1]["halt_events"] > 0 and rows[1]["resume_events"] > 0:
        rec_lines.append(
            "At minimum enable HALT_RESUME_DRAWDOWN_PCT=0.08 (avoids permanent halt lockout)."
        )
    if not rec_lines:
        rec_lines.append("Keep baseline defaults; improvements did not beat baseline on this window.")

    md = [
        "# Risk layer A/B results",
        "",
        f"Window: {rows[0]['start_date']} → {rows[0]['end_date']} ({label} daily bars)",
        "",
        _md_table(rows),
        "",
        "## Recommendation",
        "",
        *rec_lines,
        "",
        "## Config env vars",
        "",
        "- `MAX_DRAWDOWN_PCT` (default 0.10)",
        "- `HALT_RESUME_DRAWDOWN_PCT` (default 0.08; set 0 for legacy never-resume)",
        "- `HALT_LIQUIDATE_ON_BREACH` (default false)",
        "- `HALT_TARGET_CASH_PCT` (default 0.25)",
        "- `REGIME_SENTIMENT_THRESHOLD` (default 0.5 legacy; try 0.10)",
        "- `DERIVED_BEAR_PAUSE_ENABLED` (default false)",
        "- `DERIVED_BEAR_SENTIMENT_THRESHOLD` (default 0.10)",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(f"\nWrote {OUT_MD}")


if __name__ == "__main__":
    main()
