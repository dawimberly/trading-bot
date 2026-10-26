"""Research-only: mean % price move by time-of-day bucket and ET clock hour.

Complements modules/time_of_day.py with a plain % table (not Sharpe-first).
Uses yfinance hourly RTH bars. No .env, no restart, no orders.

Usage (from stock-bot/):
  python scripts/research/tod_pct_move_report.py
  python scripts/research/tod_pct_move_report.py --symbols GOLD,SPY,VTI --days 120
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

from modules.time_of_day import (  # noqa: E402
    TOD_BUCKETS,
    analyze_hourly_grid,
    analyze_symbol_tod,
    fetch_hourly_bars,
)

OUT_MD = Path(__file__).with_name("tod_pct_move_last.md")
OUT_JSON = Path(__file__).with_name("tod_pct_move_last.json")

# Paper-ish names + benchmarks (GOLD was the Sunday ask)
DEFAULT_SYMBOLS = (
    "GOLD",
    "SPY",
    "VTI",
    "FCX",
    "SMCI",
    "ELF",
    "HALO",
    "EL",
    "DINO",
)


def _pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{100.0 * float(x):+.3f}%"


def _row_bucket(stats: dict) -> list[str]:
    cells = []
    for b in TOD_BUCKETS:
        st = stats.get(b) or {}
        cells.append(_pct(st.get("mean_ret")))
    return cells


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=120)
    args = ap.parse_args()
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())

    print(f"Fetching hourly bars ({args.days}d) for {', '.join(symbols)}…", flush=True)
    bars_map = fetch_hourly_bars(symbols, days=int(args.days))
    if not bars_map:
        print("No bars returned")
        return 1

    by_symbol_buckets: dict[str, dict] = {}
    by_symbol_hourly: dict[str, dict] = {}
    for sym, bars in bars_map.items():
        bstats = analyze_symbol_tod(bars)
        hstats = analyze_hourly_grid(bars)
        by_symbol_buckets[sym] = {
            b: {
                "n": st.n,
                "mean_ret": st.mean_ret,
                "median_ret": st.median_ret,
                "win_rate": st.win_rate,
                "sharpe": st.sharpe,
            }
            for b, st in bstats.items()
        }
        by_symbol_hourly[sym] = {
            str(h): {
                "n": st.n,
                "mean_ret": st.mean_ret,
                "median_ret": st.median_ret,
                "win_rate": st.win_rate,
            }
            for h, st in sorted(hstats.items())
        }
        print(f"  {sym}: {len(bars)} hourly bars", flush=True)

    payload = {
        "research_only": True,
        "days": args.days,
        "symbols": list(bars_map.keys()),
        "metric": "forward_1h_pct_return_from_bar",
        "buckets": list(TOD_BUCKETS),
        "by_symbol_buckets": by_symbol_buckets,
        "by_symbol_hourly": by_symbol_hourly,
        "note": (
            "mean_ret is average forward 1-hour % move starting in that bucket/hour "
            "(RTH only). Not a prediction of the cash open."
        ),
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Time-of-day % move report (research only)",
        "",
        "No orders, no `.env`, no restart.",
        "",
        f"**Window:** ~{args.days} calendar days of hourly RTH bars (yfinance).  ",
        f"**Metric:** mean **forward 1-hour % return** from bars that start in each bucket/hour.  ",
        f"**Symbols:** {', '.join(bars_map.keys())}",
        "",
        "Same vocabulary as `modules/time_of_day.py`: "
        "`open` 9:30–9:45 · `first_30m` 9:30–10:00 · `mid_morning` 10:00–11:30 · "
        "`midday` 11:30–14:00 · `last_hour` 15:00–16:00 · `close` 15:45–16:00.",
        "",
        "## Mean forward-1h % by session bucket",
        "",
        "| Symbol | " + " | ".join(TOD_BUCKETS) + " |",
        "|---|" + "|".join(["---:"] * len(TOD_BUCKETS)) + "|",
    ]
    for sym in bars_map:
        cells = _row_bucket(by_symbol_buckets[sym])
        lines.append(f"| {sym} | " + " | ".join(cells) + " |")

    lines.extend(["", "## Mean forward-1h % by ET clock hour", ""])
    hours = [str(h) for h in range(9, 16)]
    lines.append("| Symbol | " + " | ".join(f"{h}:00" for h in hours) + " |")
    lines.append("|---|" + "|".join(["---:"] * len(hours)) + "|")
    for sym in bars_map:
        cells = []
        for h in hours:
            st = (by_symbol_hourly[sym] or {}).get(h) or {}
            cells.append(_pct(st.get("mean_ret")))
        lines.append(f"| {sym} | " + " | ".join(cells) + " |")

    # GOLD focus block if present
    if "GOLD" in by_symbol_buckets:
        lines.extend(["", "## GOLD focus", ""])
        g = by_symbol_buckets["GOLD"]
        ranked = sorted(
            ((b, g[b]["mean_ret"], g[b]["n"]) for b in TOD_BUCKETS if g[b]["n"] > 0),
            key=lambda t: t[1],
            reverse=True,
        )
        if ranked:
            best_b, best_r, best_n = ranked[0]
            worst_b, worst_r, worst_n = ranked[-1]
            lines.append(
                f"- Best bucket: **{best_b}** ({_pct(best_r)}, n={best_n})"
            )
            lines.append(
                f"- Worst bucket: **{worst_b}** ({_pct(worst_r)}, n={worst_n})"
            )
        lines.append("")
        lines.append("| Bucket | n | mean 1h % | median | win% |")
        lines.append("|---|---:|---:|---:|---:|")
        for b in TOD_BUCKETS:
            st = g[b]
            lines.append(
                f"| {b} | {st['n']} | {_pct(st['mean_ret'])} | "
                f"{_pct(st['median_ret'])} | {100*st['win_rate']:.1f}% |"
            )

    lines.extend(
        [
            "",
            "## How to use (research)",
            "",
            "- Track which hours/buckets carry drift vs chop — not a Monday-open predictor.",
            "- Paper already soft-uses TOD via `TIME_OF_DAY_ANALYSIS` / Markov blend; "
            "this report is the plain % view.",
            "- Re-run anytime: `python scripts/research/tod_pct_move_report.py --days 120`",
            "",
            "## Full stack TOD",
            "",
            "Broader 365d study (Sharpe + recommendations): "
            "`python scripts/analysis/run_tod_analysis.py --days 365`",
            "",
        ]
    )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
