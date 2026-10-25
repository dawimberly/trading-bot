"""Single-knob PIT test: take half at +5% vs the Lab default +20%.

Pre-registered hypothesis: realizing half the position at a smaller gain can
reduce giveback while retaining the other half for trend continuation.

Baseline: current Lab 8-name close-to-close book (MA70 rank, 15% per name,
-10% disaster stop, half exit at +20%, trail arms at +10%, 8% trail, 30 bars,
and Lab's idle-cash add policy).
Treatment changes only the half-exit threshold to +5%.

Two disjoint 365-session evaluation windows, S&P 500 membership by date,
interior-only forward fill, 10 bps round-trip cost, marked-to-market drawdown.
Research only: reads local SQLite prices and membership; no broker/API, orders,
environment changes, or runtime configuration writes.

From stock-bot/:
  py -3 scripts/analysis/profit_capture_pit_backtest.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from modules import pit_universe as pit  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from one_r_hit_backtest import SKIP  # noqa: E402
from paper_lab_concentrated_backtest import run_lab  # noqa: E402

OUT_JSON = Path(__file__).with_name("profit_capture_pit_backtest_last.json")
OUT_MD = Path(__file__).with_name("profit_capture_pit_backtest_last.md")
MA_WIN = int(getattr(config, "PAPER_NYSE_MA_WINDOW", 70))
WARMUP = MA_WIN + 15
ROUND_TRIP_BPS = 10.0
SLEEVE = 0.95
BASELINE_HALF_GAIN = 0.20
TREATMENT_HALF_GAIN = 0.05
DD_GATE_PP = 2.0
MIN_DAILY_PIT_COVERAGE = 0.90
EVAL_SESSIONS = 365


def _book_universe(data, membership):
    in_window = pit.sp500_tickers_in_window(membership, data.index[0], data.index[-1])
    symbols = []
    for ticker in in_window:
        col = pit.resolve_column(ticker, data.columns)
        if col and col not in SKIP and not config.is_crypto(col):
            symbols.append(col)
    eligibility = pit.membership_matrix(data.index, membership, tickers=symbols)
    return symbols, eligibility, pit.coverage_report(
        eligibility, data.columns, members_in_window=in_window
    )


def _run(data, symbols, eligible, half_gain):
    result, _trades = run_lab(
        data,
        ma_win=MA_WIN,
        warmup=WARMUP,
        max_names=8,
        name_pct=0.15,
        disaster=0.10,
        half_gain=half_gain,
        trail_arm=0.10,
        trail_pct=0.08,
        max_hold=30,
        # Match Lab's actual idle-cash top-up policy and second-ticket guard.
        add_policy="idle",
        add_max_mult=2.0,
        universe=symbols,
        eligible=eligible,
        cost_rt_bps=ROUND_TRIP_BPS,
    )
    return {k: result.get(k) for k in (
        "start", "end", "bars", "symbols", "trades", "win_rate",
        "avg_ret_pct", "med_ret_pct", "equity_end", "book_ret_pct",
        "max_dd_pct", "max_dd_booked_pct", "sharpe", "exit_reasons",
        "adds", "cost_rt_bps", "pit_gated",
    )}


def _window_coverage(index, membership, available_columns):
    """Measure observed data against all PIT members, not just loaded columns."""
    expected = pit.membership_matrix(index, membership)
    cols = [c for c in expected.columns
            if pit.resolve_column(c, available_columns) is not None]
    expected_n = expected.sum(axis=1).astype(float)
    covered_n = expected[cols].sum(axis=1).astype(float) if cols else expected_n * 0
    daily = covered_n / expected_n.replace(0, float("nan"))
    member_names = pit.sp500_tickers_in_window(membership, index[0], index[-1])
    with_prices = [s for s in member_names if pit.resolve_column(s, available_columns) is not None]
    return {
        "eval_sessions": len(index),
        "avg_pit_members_per_session": round(float(expected_n.mean()), 1),
        "avg_members_with_local_prices_per_session": round(float(covered_n.mean()), 1),
        "avg_daily_pit_coverage_pct": round(float(daily.mean() * 100.0), 1),
        "members_during_window": len(member_names),
        "members_with_local_prices": len(with_prices),
        "unique_member_coverage_pct": round(100 * len(with_prices) / len(member_names), 1) if member_names else 0.0,
    }


def _two_disjoint_windows(data):
    """Return two exact 365-session tests, each with its own MA/ATR warmup."""
    required = 2 * EVAL_SESSIONS + WARMUP
    if len(data) < required:
        raise RuntimeError(f"Need {required} local sessions; found {len(data)}.")
    recent_eval_start = len(data) - EVAL_SESSIONS
    prior_eval_end = recent_eval_start
    prior_eval_start = prior_eval_end - EVAL_SESSIONS
    return [
        ("365_sessions_recent", data.iloc[recent_eval_start - WARMUP:]),
        ("365_sessions_prior", data.iloc[prior_eval_start - WARMUP:prior_eval_end]),
    ]


def main() -> int:
    membership = pit.load_sp500_membership()
    # Explicitly request all constituent tables; default bot fetch list is narrower.
    requested = sorted(set(membership["ticker"].astype(str).str.upper()))
    data = load_close_matrix(interval="1d", tickers=requested, ffill=False, force_refresh=True)
    if data is None or data.empty:
        raise RuntimeError("Local daily-close database is empty; no test run.")
    data = data.where(data > 0).dropna(axis=1, how="all")
    data = pit.ffill_interior_only(pit.session_bars(data))
    data = data.tail(2 * EVAL_SESSIONS + WARMUP)
    if len(data) < 2 * EVAL_SESSIONS + WARMUP:
        raise RuntimeError(f"Not enough local sessions ({len(data)}) for two disjoint 365-session tests.")

    symbols, eligible, coverage = _book_universe(data, membership)
    if not symbols:
        raise RuntimeError("No PIT S&P 500 names overlap local prices; no test run.")
    windows = _two_disjoint_windows(data)
    if len(windows) != 2:
        raise RuntimeError("Local history cannot produce two disjoint 365-session windows.")

    report = {
        "research_only": True,
        "hypothesis": "Taking half off at +5% instead of +20% improves net book return or drawdown while retaining the other half for trend continuation.",
        "one_knob": "half_gain: 20% -> 5%",
        "baseline": "Lab 8-name, MA70, 15% per name, -10% disaster, 10% trail-arm, 8% trail, 30-session max hold, actual Lab idle-cash adds (top-up under cap; second ticket only when all slots are full and the add guard passes).",
        "execution": "Daily close fills; 10 bps round trip; no open/high/low data in this close-only walk. Costs charged on entries, partial sales, and final exits.",
        "gate": f"Treatment must beat baseline book return in both disjoint windows, with MTM max drawdown no worse by more than {DD_GATE_PP:.1f} percentage points in either window.",
        "data": {"source": "existing local SQLite daily closes; all S&P constituent tables requested; no download", "start": str(data.index[0].date()), "end": str(data.index[-1].date()), "sessions_including_warmup": len(data), "symbols_with_any_data": len(data.columns), "pit_sp500_coverage_over_both_windows_and_warmups": coverage},
        "windows": {},
        "overall_verdict": None,
    }
    md = [
        "# Partial profit capture: S&P 500 PIT test (research only)",
        "",
        f"**Hypothesis:** {report['hypothesis']}",
        "",
        f"**One knob:** half-exit threshold {BASELINE_HALF_GAIN:.0%} → {TREATMENT_HALF_GAIN:.0%}. All other Lab-style rules are held fixed.",
        "",
        f"**Protocol:** two disjoint 365-session windows, S&P 500 membership by date, {ROUND_TRIP_BPS:.0f} bps round trip, interior-only price fill, marked-to-market drawdown. Treatment must improve return in both windows and stay within {DD_GATE_PP:.1f} pp of baseline drawdown. Promotion-quality interpretation also requires at least {MIN_DAILY_PIT_COVERAGE:.0%} average daily PIT coverage.",
        "",
        f"**Data:** local daily closes {data.index[0].date()} → {data.index[-1].date()}, {len(data.columns)} names with price history. PIT window membership coverage: {coverage['coverage_pct']:.1f}% ({coverage['members_with_data']}/{coverage['members_in_window']}).",
        "",
        "Fills use daily closes because the local PIT matrix has close-only history. It cannot model intraday target touches, open gaps through stops, spread changes, or market impact. This is a research screen, not a live-setting change.",
        "",
    ]
    all_pass = True
    for label, frame in windows:
        elig_window = eligible.reindex(frame.index)
        eval_index = frame.index[WARMUP:]
        coverage_window = _window_coverage(eval_index, membership, data.columns)
        base = _run(frame, symbols, elig_window, BASELINE_HALF_GAIN)
        treatment = _run(frame, symbols, elig_window, TREATMENT_HALF_GAIN)
        d_return = round(treatment["book_ret_pct"] - base["book_ret_pct"], 2)
        d_dd = round(treatment["max_dd_pct"] - base["max_dd_pct"], 2)
        economic_pass = d_return > 0 and d_dd <= DD_GATE_PP
        coverage_pass = coverage_window["avg_daily_pit_coverage_pct"] >= MIN_DAILY_PIT_COVERAGE * 100
        passed = economic_pass and coverage_pass
        all_pass &= passed
        report["windows"][label] = {
            "coverage": coverage_window,
            "baseline_half_at_20pct": base,
            "treatment_half_at_5pct": treatment,
            "delta_return_pp": d_return,
            "delta_max_dd_pp": d_dd,
            "economic_gate": "PASS" if economic_pass else "FAIL",
            "coverage_gate": "PASS" if coverage_pass else "INSUFFICIENT DATA",
            "gate": "PASS" if passed else ("FAIL" if not economic_pass else "INCONCLUSIVE"),
        }
        md.extend([
            f"## {label}: {base['start']} → {base['end']}",
            "",
            "| Rule | Return | MTM max DD | Sharpe | Trades | Win rate | Median trade | Exit counts |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ])
        for name, result in (("Half at +20% (baseline)", base), ("Half at +5%", treatment)):
            md.append(
                f"| {name} | {result['book_ret_pct']:+.2f}% | {result['max_dd_pct']:.2f}% | {result['sharpe']} | {result['trades']} | {result['win_rate']:.1f}% | {result['med_ret_pct']:+.2f}% | `{result['exit_reasons']}` |"
            )
        md.extend([
            "",
            f"Treatment delta: return {d_return:+.2f} pp; MTM max-DD {d_dd:+.2f} pp. Economics **{report['windows'][label]['economic_gate']}**; daily PIT coverage {coverage_window['avg_daily_pit_coverage_pct']:.1f}% (**{report['windows'][label]['coverage_gate']}**). Window gate: **{report['windows'][label]['gate']}**.",
            "",
        ])
        print(f"{label}: base {base['book_ret_pct']:+.2f}% / DD {base['max_dd_pct']:.2f}% / Sharpe {base['sharpe']}; "
              f"half+5 {treatment['book_ret_pct']:+.2f}% / DD {treatment['max_dd_pct']:.2f}% / Sharpe {treatment['sharpe']}; "
              f"delta {d_return:+.2f}pp, coverage {coverage_window['avg_daily_pit_coverage_pct']:.1f}%, {report['windows'][label]['gate']}", flush=True)

    report["overall_verdict"] = "CANDIDATE" if all_pass else "HOLD: the pre-registered two-window gate did not pass."
    md.extend([
        "## Overall verdict",
        "",
        f"**{report['overall_verdict']}**. A pass here would justify a separate Lab forward-shadow step only; it would not authorize Medium or live changes.",
    ])
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(report["overall_verdict"], flush=True)
    print(f"Saved {OUT_MD}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
