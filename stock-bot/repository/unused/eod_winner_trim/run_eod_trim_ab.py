#!/usr/bin/env python3
"""Quick A/B: alpaca_paper_v2 vs v2 + EOD fade trim. Writes markdown summary."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from backtester import (  # noqa: E402
    MIN_HISTORY,
    _ensure_daily_data,
    release_backtest_memory,
    run_backtest,
    run_eod_winner_trim_compare,
    _apply_v2_paper_config_overrides,
    _restore_v2_paper_config_overrides,
    _v2_paper_backtest_kwargs,
    _benchmark_return,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--out", type=Path, default=ROOT / "scripts" / "analysis" / "eod_trim_ab_latest.md")
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.ERROR)
    for name in ("modules", "backtester", "urllib3", "yfinance"):
        logging.getLogger(name).setLevel(logging.ERROR)

    data = _ensure_daily_data(args.days, refresh=False, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need {MIN_HISTORY}+ bars; got {len(data)}")
        return 1

    bench = _benchmark_return(data, MIN_HISTORY)
    v2_saved = _apply_v2_paper_config_overrides()
    base = _v2_paper_backtest_kwargs()
    arms = [
        ("baseline", {**base, "paper_eod_winner_trim": False}),
        ("eod_trim", {**base, "paper_eod_winner_trim": True}),
    ]
    rows: list[dict] = []
    try:
        for label, kw in arms:
            r = run_backtest(data, **kw)
            rows.append({"label": label, **r})
            release_backtest_memory()
    finally:
        _restore_v2_paper_config_overrides(v2_saved)

    off, on = rows[0], rows[1]
    eod = on.get("eod_winner_trim") or {}
    lines = [
        f"# EOD fade trim A/B ({args.days}d, paper v2 profile)",
        "",
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()}",
        f"VTI B&H: {bench:+.2f}%" if bench is not None else "",
        "",
        "| Arm | Return | Sharpe | MaxDD | NYSE signals | EOD trims | Held strong |",
        "|-----|--------|--------|-------|--------------|-----------|-------------|",
    ]
    for row in rows:
        e = row.get("eod_winner_trim") or {}
        lines.append(
            f"| {row['label']} | {row['total_return_pct']:+.2f}% | {row['sharpe']:.2f} | "
            f"{row['max_drawdown_pct']:.2f}% | {row.get('nyse_signals', 0)} | "
            f"{e.get('trims', '-')} | {e.get('skipped_strong', '-')} |"
        )
    lines += [
        "",
        f"Delta return: {on['total_return_pct'] - off['total_return_pct']:+.2f}pp",
        f"Delta Sharpe: {on['sharpe'] - off['sharpe']:+.2f}",
        f"Delta MaxDD: {on['max_drawdown_pct'] - off['max_drawdown_pct']:+.2f}pp",
        f"EOD trims fired: {eod.get('trims', 0)}",
    ]
    text = "\n".join(lines)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
