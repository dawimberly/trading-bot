"""PIT A/B: equal Lab sizing vs bounded momentum-rank sizing (research only).

Only position sizing changes. Both arms use the same MA70 momentum ranks,
eight-name limit, Lab exits, idle-cash adds, PIT S&P membership, daily-close
fills, and 10 bps round-trip costs. The treatment tilts the same average
15%-per-name budget across the current top eight ranks, from 22.5% at rank 1
down to 7.5% at rank 8. It does not use future returns or retune by window.
Open positions are not force-rebalanced when their rank changes; rank sizing
sets the initial ticket and the cap used for later allowed adds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from paper_lab_concentrated_backtest import run_lab  # noqa: E402
from profit_capture_pit_backtest import (  # noqa: E402
    EVAL_SESSIONS,
    MA_WIN,
    ROUND_TRIP_BPS,
    WARMUP,
    _book_universe,
    _two_disjoint_windows,
    _window_coverage,
)
from modules import pit_universe as pit  # noqa: E402
from one_r_hit_backtest import SKIP  # noqa: E402

OUT_JSON = Path(__file__).with_name("lab_conviction_sizing_pit_backtest_last.json")
OUT_MD = Path(__file__).with_name("lab_conviction_sizing_pit_backtest_last.md")
DD_GATE_PP = 2.0


def _run(data, symbols, eligible, conviction_sizing: bool) -> dict:
    result, _trades = run_lab(
        data,
        ma_win=MA_WIN,
        warmup=WARMUP,
        max_names=8,
        name_pct=0.15,
        disaster=0.10,
        half_gain=0.20,
        trail_arm=0.10,
        trail_pct=0.08,
        max_hold=30,
        add_policy="idle",
        add_max_mult=2.0,
        universe=symbols,
        eligible=eligible,
        cost_rt_bps=ROUND_TRIP_BPS,
        conviction_sizing=conviction_sizing,
    )
    return result


def main() -> int:
    membership = pit.load_sp500_membership()
    requested = sorted(set(membership["ticker"].astype(str).str.upper()))
    data = load_close_matrix(
        interval="1d", tickers=requested, ffill=False, force_refresh=True
    )
    if data is None or data.empty:
        raise RuntimeError("Local daily-close database is empty; no test run.")
    data = data.where(data > 0).dropna(axis=1, how="all")
    data = pit.ffill_interior_only(pit.session_bars(data))
    data = data.tail(2 * EVAL_SESSIONS + WARMUP)
    if len(data) < 2 * EVAL_SESSIONS + WARMUP:
        raise RuntimeError(f"Not enough local sessions ({len(data)}) for two windows.")

    symbols, eligible, coverage = _book_universe(data, membership)
    if not symbols:
        raise RuntimeError("No PIT S&P 500 names overlap local prices; no test run.")

    report = {
        "research_only": True,
        "hypothesis": "The current MA70 momentum rank contains useful conviction information, so allocating more to higher-ranked names improves net book return without materially increasing drawdown.",
        "one_knob": "Enable rank-based position sizing for the current top eight momentum names.",
        "sizing_rule": {
            "rank_multipliers": [1.50, 1.35, 1.20, 1.05, 0.90, 0.80, 0.70, 0.50],
            "rank_1_pct_equity": 22.5,
            "rank_8_pct_equity": 7.5,
            "average_pct_equity": 15.0,
            "max_idle_add_pct_equity": 30.0,
        "rebalancing": "No forced trims/rebalances; current rank sets new-entry sizing and the add cap.",
        },
        "baseline": "Current Lab: equal 15% target per name; 8 names; MA70 momentum rank; disaster -10%; half at +20%; trail arms at +10% and trails 8%; max hold 30 sessions; idle-cash add policy.",
        "execution": "Daily-close fills, 10 bps round trip, PIT S&P membership, interior-only forward fill. Close-only data cannot model intraday fills, open gaps, spread changes, or market impact.",
        "gate": f"Treatment must beat baseline return in both disjoint 365-session windows and have MTM drawdown no worse by more than {DD_GATE_PP:.1f} pp in either.",
        "data": {
            "source": "local SQLite daily closes; PIT constituents",
            "start": str(data.index[0].date()),
            "end": str(data.index[-1].date()),
            "sessions_including_warmup": len(data),
            "symbols_with_any_data": len(data.columns),
            "pit_coverage": coverage,
        },
        "windows": {},
        "overall_verdict": None,
    }

    md = [
        "# Lab conviction sizing: S&P 500 PIT test (research only)",
        "",
        f"**Hypothesis:** {report['hypothesis']}",
        "",
        "**Treatment:** rank multipliers 1.50, 1.35, 1.20, 1.05, 0.90, 0.80, 0.70, 0.50 for the current top eight names. That maps a 15% average target to 22.5% for rank 1 and 7.5% for rank 8; the existing idle-add hard limit remains 30% per name. Rank affects new-entry size and the cap for allowed adds; open positions are not force-rebalanced when their rank changes.",
        "",
        f"**Protocol:** two disjoint 365-session windows, PIT S&P membership, {ROUND_TRIP_BPS:.0f} bps round-trip cost. Entries, ranking, exits, add policy, and total cash limit match between arms. Pass requires higher return in both windows and no more than {DD_GATE_PP:.1f} pp additional MTM drawdown in either.",
        "",
        f"**Data:** {len(data.columns)} names with local prices from {data.index[0].date()} through {data.index[-1].date()}; PIT membership coverage across windows and warmups {coverage['coverage_pct']:.1f}% ({coverage['members_with_data']}/{coverage['members_in_window']}).",
        "",
        "This is a historical screen with daily-close fills. It does not model intraday highs/lows, gaps, spread changes, or market impact. A pass would support only a Lab paper trial.",
        "",
    ]

    all_pass = True
    for label, frame in _two_disjoint_windows(data):
        elig_window = eligible.reindex(frame.index)
        eval_index = frame.index[WARMUP:]
        coverage_window = _window_coverage(eval_index, membership, data.columns)
        base = _run(frame, symbols, elig_window, False)
        treatment = _run(frame, symbols, elig_window, True)
        delta_return = round(treatment["book_ret_pct"] - base["book_ret_pct"], 2)
        delta_dd = round(treatment["max_dd_pct"] - base["max_dd_pct"], 2)
        passed = delta_return > 0 and delta_dd <= DD_GATE_PP
        all_pass &= passed
        report["windows"][label] = {
            "coverage": coverage_window,
            "baseline_equal": base,
            "treatment_rank_sized": treatment,
            "delta_return_pp": delta_return,
            "delta_max_dd_pp": delta_dd,
            "gate": "PASS" if passed else "FAIL",
        }
        md.extend([
            f"## {label}: {base['start']} → {base['end']}",
            "",
            "| Rule | Return | MTM max DD | Sharpe | Trades | Win rate | Median trade | Adds |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for name, result in (("Equal 15% (baseline)", base), ("Rank-sized", treatment)):
            md.append(
                f"| {name} | {result['book_ret_pct']:+.2f}% | {result['max_dd_pct']:.2f}% | {result['sharpe']} | {result['trades']} | {result['win_rate']:.1f}% | {result['med_ret_pct']:+.2f}% | {result['adds']} |"
            )
        md.extend([
            "",
            f"Treatment delta: return {delta_return:+.2f} pp; MTM max-DD {delta_dd:+.2f} pp. Gate: **{'PASS' if passed else 'FAIL'}**. Daily PIT coverage: {coverage_window['avg_daily_pit_coverage_pct']:.1f}%.",
            "",
        ])
        print(
            f"{label}: equal {base['book_ret_pct']:+.2f}% / DD {base['max_dd_pct']:.2f}%; "
            f"rank-sized {treatment['book_ret_pct']:+.2f}% / DD {treatment['max_dd_pct']:.2f}%; "
            f"delta {delta_return:+.2f}pp, gate {'PASS' if passed else 'FAIL'}",
            flush=True,
        )

    report["overall_verdict"] = "CANDIDATE" if all_pass else "HOLD: two-window sizing gate did not pass."
    md.extend(["## Overall verdict", "", f"**{report['overall_verdict']}**"])
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(report["overall_verdict"], flush=True)
    print(f"Saved {OUT_MD}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
