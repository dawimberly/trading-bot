"""A2B1 overlay on the S&P PIT pick_15 book (research only).

One knob: flatten after a prior-session close ≥8% vs prior-prior, at the
next open, skip Monday. Exit price is Alpaca open[t] / sqlite close[t-1].

Baseline: NYSE MA70 pick_15, 30d hold, 2× ATR, sleeve 0.95, 10 bps RT,
S&P 500 membership by date, drawdown marked to market.

Usage (from stock-bot/):
  python scripts/analysis/a2b1_pit_ab.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules import pit_universe as pit  # noqa: E402
from modules.trial_ledger import deflate_trial, load_trials, record_trial  # noqa: E402
from one_r_hit_backtest import SKIP, run_backtest  # noqa: E402
from pit_universe_rerun import load_alpaca_open_gaps  # noqa: E402

OUT_JSON = Path(__file__).with_name("a2b1_pit_ab_last.json")
OUT_MD = Path(__file__).with_name("a2b1_pit_ab_last.md")
FAMILY = "nyse_ma70_walk"
COST_BPS = 10.0
SLEEVE = 0.95
DD_GATE_PP = 2.0
GAIN_PCT = 8.0


def _sp500_book(data):
    membership = pit.load_sp500_membership()
    in_window = pit.sp500_tickers_in_window(membership, data.index[0], data.index[-1])
    symbols = []
    for s in in_window:
        col = pit.resolve_column(s, data.columns)
        if col and col not in SKIP and not config.is_crypto(col):
            symbols.append(col)
    elig = pit.membership_matrix(data.index, membership, tickers=symbols)
    return symbols, elig, pit.coverage_report(elig, data.columns, members_in_window=in_window)


def _gate(cand: dict, base: dict) -> str:
    better = cand["book_ret_pct"] > base["book_ret_pct"]
    dd_ok = cand["max_dd_pct"] <= base["max_dd_pct"] + DD_GATE_PP
    return "PASS" if better and dd_ok else "FAIL"


def _slim(r: dict) -> dict:
    fades = (r.get("exit_reasons") or {}).get("gain_fade", 0)
    return {
        "book_ret_pct": r["book_ret_pct"],
        "max_dd_pct": r["max_dd_pct"],
        "max_dd_booked_pct": r.get("max_dd_booked_pct"),
        "sharpe": r.get("sharpe"),
        "sharpe_daily": r.get("sharpe_daily"),
        "n_days": r.get("n_days"),
        "daily_skew": r.get("daily_skew"),
        "daily_kurt": r.get("daily_kurt"),
        "trades": r["trades"],
        "win_rate": r.get("win_rate"),
        "symbols": r["symbols"],
        "start": r.get("start"),
        "end": r.get("end"),
        "gain_fades": fades,
        "gap_skips": r.get("gap_skips", 0),
        "weekend_fades": r.get("weekend_fades", 0),
        "fade_by_dow": r.get("fade_by_dow"),
        "exit_reasons": r.get("exit_reasons"),
    }


def run_window(data, *, label: str, ma_win: int, gaps) -> dict:
    warmup = ma_win + 15
    symbols, elig, coverage = _sp500_book(data)
    common = dict(
        ma_win=ma_win,
        warmup=warmup,
        max_hold=30,
        max_active=15,
        take_1r=False,
        sleeve=SLEEVE,
        cost_rt_bps=COST_BPS,
        universe=symbols,
        eligible=elig,
    )
    hold = run_backtest(data, **common)
    a2b1 = run_backtest(
        data,
        gain_exit_pct=GAIN_PCT,
        gain_exit_mode="skip_monday",
        open_gaps=gaps,
        **common,
    )
    out = {
        "label": label,
        "start": hold["start"],
        "end": hold["end"],
        "coverage": coverage,
        "policies": {
            "pick_15": _slim(hold),
            "a2b1_skip_mon": {
                **_slim(a2b1),
                "gate_vs_pick_15": _gate(a2b1, hold),
                "d_book_pp": round(a2b1["book_ret_pct"] - hold["book_ret_pct"], 2),
                "d_dd_pp": round(a2b1["max_dd_pct"] - hold["max_dd_pct"], 2),
            },
        },
    }
    for name, r in (("pick_15", hold), ("a2b1_skip_mon", a2b1)):
        record_trial(
            script="a2b1_pit_ab",
            family=FAMILY,
            label=f"{name}/sp500",
            window=label,
            params={
                "universe": "sp500",
                "policy": name,
                "cost_rt_bps": COST_BPS,
                "sleeve": SLEEVE,
                "gain_exit_pct": GAIN_PCT if name.startswith("a2b1") else None,
            },
            result=r,
        )
    return out


def main() -> int:
    pit.ensure_sp500_membership()
    membership = pit.load_sp500_membership()
    print("Loading research closes (prefetch missing S&P members)...")
    data, pref = pit.load_research_closes(membership, extra_tickers=config.backtest_fetch_tickers())
    print(
        f"Prefetch: already {pref['already']}, fetched {pref['fetched']}, "
        f"failed {len(pref['failed'])}"
    )
    if data is None or data.empty or len(data) < 200:
        print("Not enough daily history")
        return 1
    ma_win = int(getattr(config, "PAPER_NYSE_MA_WINDOW", 70))
    print(f"Matrix {data.shape[0]} session bars x {data.shape[1]} names, {data.index[0].date()} -> {data.index[-1].date()}")

    symbols = [
        s
        for s in pit.sp500_tickers_in_window(membership, data.index[0], data.index[-1])
        if s in data.columns and s not in SKIP and not config.is_crypto(s)
    ]
    print(f"Fetching Alpaca open gaps for {len(symbols)} S&P names...")
    gaps = load_alpaca_open_gaps(symbols, data.index)
    if gaps is None or gaps.empty:
        print("Alpaca open gaps unavailable — cannot price A2B1. Abort.")
        return 1
    sessions = int(gaps.notna().any(axis=1).sum())
    print(f"Open gaps: {gaps.shape[1]} names, {sessions} sessions")

    windows = pit.disjoint_session_windows(data, days=365, ma_win=ma_win)
    results = []
    for label, frame in windows:
        print(f"\n== {label}: {len(frame)} bars ==")
        res = run_window(frame, label=label, ma_win=ma_win, gaps=gaps)
        results.append(res)
        for name, row in res["policies"].items():
            extra = ""
            if name != "pick_15":
                extra = (
                    f"  {row['gate_vs_pick_15']}  dRet {row['d_book_pp']:+.2f}pp  "
                    f"dDD {row['d_dd_pp']:+.2f}pp  fades {row.get('gain_fades', 0)}"
                )
            print(
                f"  {name:<16} book {row['book_ret_pct']:7.2f}%  DD {row['max_dd_pct']:6.2f}%  "
                f"sharpe {row['sharpe']}  trades {row['trades']:4d}{extra}"
            )

    trials = load_trials(family=FAMILY)
    dsr_rows = []
    for res in results:
        for name in ("pick_15", "a2b1_skip_mon"):
            match = [t for t in trials if t["window"] == res["label"] and t["label"] == f"{name}/sp500"]
            if not match:
                continue
            d = deflate_trial(match[-1], trials)
            if d:
                d.update({"window": res["label"], "policy": name})
                dsr_rows.append(d)

    both_pass = all(
        res["policies"]["a2b1_skip_mon"]["gate_vs_pick_15"] == "PASS" for res in results
    )
    payload = {
        "research_only": True,
        "cost_rt_bps": COST_BPS,
        "sleeve": SLEEVE,
        "gain_exit_pct": GAIN_PCT,
        "gain_exit_mode": "skip_monday",
        "windows": results,
        "deflated_sharpe": dsr_rows,
        "ledger_trials_in_family": len(trials),
        "verdict": "CANDIDATE" if both_pass else "HOLD",
        "note": "No .env / restart. Promote only if both windows PASS. Not Medium/live.",
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    lines = [
        "# A2B1 on S&P PIT pick_15 (research only)",
        "",
        f"No orders, no `.env`. {COST_BPS:.0f} bps RT, sleeve {SLEEVE:.2f}, marked-to-market DD.",
        f"Knob: flatten after prior close ≥{GAIN_PCT:.0f}%, next open, skip Monday. "
        "Open gap is open[t]/close[t-1] from one OHLC source (Alpaca, else yfinance).",
        "",
        f"**Verdict:** {payload['verdict']}",
        "",
    ]
    for res in results:
        cov = res["coverage"]
        lines.append(f"## Window {res['label']}: {res['start']} → {res['end']}")
        lines.append("")
        lines.append(
            f"Coverage {cov['coverage_pct']}% ({cov['members_with_data']}/{cov['members_in_window']} members)."
        )
        lines.append("")
        lines.append("| policy | book% | maxDD% | Sharpe | trades | fades | vs pick_15 |")
        lines.append("|---|---:|---:|---:|---:|---:|---|")
        for name, row in res["policies"].items():
            gate = row.get("gate_vs_pick_15", "base")
            if name != "pick_15":
                gate = f"{gate} ({row['d_book_pp']:+.1f}pp ret, {row['d_dd_pp']:+.1f}pp DD)"
            lines.append(
                f"| {name} | {row['book_ret_pct']:+.2f} | {row['max_dd_pct']:.2f} | "
                f"{row['sharpe']} | {row['trades']} | {row.get('gain_fades', 0)} | {gate} |"
            )
        fade_dow = res["policies"]["a2b1_skip_mon"].get("fade_by_dow") or {}
        if fade_dow:
            dow = ", ".join(f"{k}={v}" for k, v in fade_dow.items())
            lines.append("")
            lines.append(f"Fades by weekday: {dow}")
        lines.append("")
    if dsr_rows:
        lines.append("## Deflated Sharpe (family ledger)")
        lines.append("")
        lines.append("| window | policy | Sharpe (ann) | E[max SR] | trials | DSR | PSR>0 |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for d in dsr_rows:
            lines.append(
                f"| {d['window']} | {d['policy']} | {d['sharpe_annual']} | {d['sr0_annual']} | "
                f"{d['n_trials']} | {d['dsr']} | {d['psr_vs_zero']} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Verdict rule",
            "",
            f"- Promote only if book% > pick_15 **and** maxDD not worse by > {DD_GATE_PP:.1f} pp, both windows.",
            "- Already wired on Lab 8. A PASS here is the isolated-walk second window, not a Medium/live promote.",
            "- A FAIL keeps Lab as-is (do not silently turn A2B1 off from this file).",
            "",
        ]
    )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nVerdict: {payload['verdict']}")
    print(f"Wrote {OUT_JSON}\nWrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
