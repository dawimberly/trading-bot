"""Re-run the NYSE MA70 walk on honest universes (research only).

Three candidate lists, same rules, same dates, 10 bps round trip:

  current  runtime list (fixed + sector pools + today's screener JSON)
  frozen   static lists only — no screener-only names
  sp500    S&P 500 membership by date (point-in-time; names that left the
           index stop being candidates on their exit date)

Policies: pick 8 / 15 / 25 (hold), A2B1 (8% skip-Monday fade, Alpaca open
gaps), Lab 8 stack, and the in-harness reject rows (Friday flatten, take_1r,
fat-loser drip). Every run is appended to the research trial ledger.

Usage (from stock-bot/):
  python scripts/analysis/pit_universe_rerun.py            # 365d + full DB window
  python scripts/analysis/pit_universe_rerun.py --no-gaps  # skip Alpaca open gaps
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules import pit_universe as pit  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from modules.trial_ledger import deflate_trial, load_trials, record_trial  # noqa: E402
from one_r_hit_backtest import SKIP, _universe, run_backtest  # noqa: E402
from paper_lab_concentrated_backtest import run_lab  # noqa: E402

OUT_JSON = Path(__file__).with_name("pit_universe_rerun_last.json")
OUT_MD = Path(__file__).with_name("pit_universe_rerun_last.md")
FAMILY = "nyse_ma70_walk"
COST_BPS = 10.0
SLEEVE = 0.95
DD_GATE_PP = 2.0


def load_alpaca_open_gaps(symbols: list[str], index: pd.DatetimeIndex) -> pd.DataFrame | None:
    """open[t] / close[t-1] from Alpaca daily bars, aligned to ``index``."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        api_key, secret_key = config.get_alpaca_credentials(paper=True)
        client = StockHistoricalDataClient(api_key, secret_key)
    except Exception as exc:  # noqa: BLE001
        print(f"Alpaca open gaps unavailable: {exc}")
        return None
    start = pd.Timestamp(index[0]).to_pydatetime() - timedelta(days=7)
    end = pd.Timestamp(index[-1]).to_pydatetime() + timedelta(days=1)
    opens: dict[str, pd.Series] = {}
    closes: dict[str, pd.Series] = {}
    chunk = 150
    for k in range(0, len(symbols), chunk):
        batch = symbols[k : k + chunk]
        try:
            bars = client.get_stock_bars(
                StockBarsRequest(symbol_or_symbols=batch, timeframe=TimeFrame.Day, start=start, end=end)
            )
            df = getattr(bars, "df", None)
        except Exception as exc:  # noqa: BLE001
            print(f"Alpaca bars failed for batch {k // chunk}: {exc}")
            continue
        if df is None or df.empty:
            continue
        for sym, sub in df.groupby(level="symbol"):
            sub = sub.droplevel("symbol")
            idx = pd.to_datetime(sub.index, utc=True).tz_convert(None).normalize()
            opens[sym] = pd.Series(pd.to_numeric(sub["open"], errors="coerce").to_numpy(), index=idx)
            closes[sym] = pd.Series(pd.to_numeric(sub["close"], errors="coerce").to_numpy(), index=idx)
    if not opens:
        return None
    o = pd.DataFrame(opens).sort_index()
    c = pd.DataFrame(closes).sort_index()
    gaps = o / c.shift(1)
    gaps = gaps[~gaps.index.duplicated(keep="last")]
    return gaps.reindex(pd.DatetimeIndex(index).normalize())


def _symbols_for(mode: str, data: pd.DataFrame, runtime_cols: set[str], membership: pd.DataFrame):
    """(symbols, eligible-or-None) for one universe mode."""
    current = [c for c in _universe(data.columns) if c in runtime_cols]
    if mode == "current":
        return current, None
    if mode == "frozen":
        return pit.frozen_universe(current), None
    if mode == "sp500":
        in_window = pit.sp500_tickers_in_window(membership, data.index[0], data.index[-1])
        syms = [
            s
            for s in in_window
            if s in data.columns and s not in SKIP and not config.is_crypto(s)
        ]
        elig = pit.membership_matrix(data.index, membership, tickers=syms)
        return syms, elig
    raise ValueError(mode)


def _row(label: str, r: dict) -> dict:
    return {
        "label": label,
        "book_ret_pct": r["book_ret_pct"],
        "max_dd_pct": r["max_dd_pct"],
        "max_dd_booked_pct": r.get("max_dd_booked_pct"),
        "sharpe": r.get("sharpe"),
        "trades": r["trades"],
        "symbols": r["symbols"],
        "win_rate": r.get("win_rate"),
    }


def _gate(cand: dict, base: dict) -> str:
    better = cand["book_ret_pct"] > base["book_ret_pct"]
    dd_ok = cand["max_dd_pct"] <= base["max_dd_pct"] + DD_GATE_PP
    return "PASS" if better and dd_ok else "FAIL"


def run_window(
    data: pd.DataFrame,
    *,
    label: str,
    ma_win: int,
    runtime_cols: set[str],
    membership: pd.DataFrame,
    gaps: pd.DataFrame | None,
) -> dict:
    warmup = ma_win + 15
    common = dict(ma_win=ma_win, warmup=warmup, max_hold=30, take_1r=False, sleeve=SLEEVE, cost_rt_bps=COST_BPS)
    out: dict = {"label": label, "start": None, "end": None, "modes": {}}
    for mode in pit.MODES:
        syms, elig = _symbols_for(mode, data, runtime_cols, membership)
        block: dict = {"symbols": len(syms), "policies": {}}
        if elig is not None:
            block["coverage"] = pit.coverage_report(elig, data.columns)
        kw = dict(universe=syms, eligible=elig)
        runs: dict[str, dict] = {}
        for n in (8, 15, 25):
            runs[f"pick_{n}"] = run_backtest(data, max_active=n, **common, **kw)
        if gaps is not None:
            runs["a2b1_skip_mon"] = run_backtest(
                data, max_active=15, gain_exit_pct=8.0, gain_exit_mode="skip_monday", open_gaps=gaps, **common, **kw
            )
            runs["fat_loser_drip"] = run_backtest(data, max_active=15, fat_loser=True, open_gaps=gaps, **common, **kw)
        runs["friday_flatten"] = run_backtest(data, max_active=15, friday_flatten=True, **common, **kw)
        take = dict(common)
        take["take_1r"] = True
        runs["take_1r"] = run_backtest(data, max_active=15, **take, **kw)
        lab, _trades = run_lab(data, ma_win=ma_win, warmup=warmup, add_policy="room", cost_rt_bps=COST_BPS, **kw)
        runs["lab_8_room"] = lab
        out["start"] = out["start"] or runs["pick_15"]["start"]
        out["end"] = out["end"] or runs["pick_15"]["end"]
        base = runs["pick_15"]
        for name, r in runs.items():
            row = _row(name, r)
            if name != "pick_15":
                row["gate_vs_pick_15"] = _gate(r, base)
            block["policies"][name] = row
            record_trial(
                script="pit_universe_rerun",
                family=FAMILY,
                label=f"{name}/{mode}",
                window=label,
                params={"universe": mode, "policy": name, "cost_rt_bps": COST_BPS, "sleeve": SLEEVE},
                result=r,
            )
        out["modes"][mode] = block
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--no-gaps", action="store_true", help="skip Alpaca open gaps (no A2B1 / fat-loser rows)")
    args = ap.parse_args()
    ma_win = int(getattr(config, "PAPER_NYSE_MA_WINDOW", 70))

    pit.ensure_sp500_membership()
    membership = pit.load_sp500_membership()
    runtime = list(config.backtest_fetch_tickers())
    runtime_cols = set(runtime)
    wanted = sorted(set(runtime) | set(membership["ticker"].unique()))
    print(f"Loading daily closes for up to {len(wanted)} names...")
    data = load_close_matrix(interval="1d", tickers=wanted)
    if data is None or data.empty or len(data) < ma_win + 60:
        print("Not enough daily history")
        return 1
    print(f"Matrix {data.shape[0]} bars x {data.shape[1]} names, {data.index[0].date()} -> {data.index[-1].date()}")

    gaps = None
    if not args.no_gaps:
        gap_syms = sorted(c for c in data.columns if not config.is_crypto(c))
        gaps = load_alpaca_open_gaps(gap_syms, data.index)
        if gaps is not None:
            print(f"Open gaps: {gaps.shape[1]} names, {int(gaps.notna().any(axis=1).sum())} sessions")

    windows = [(f"{args.days}d", data.tail(args.days + ma_win + 40)), (f"full_db_{len(data)}bars", data)]
    results = []
    for label, frame in windows:
        print(f"\n== window {label}: {len(frame)} bars ==")
        res = run_window(frame, label=label, ma_win=ma_win, runtime_cols=runtime_cols, membership=membership, gaps=gaps)
        results.append(res)
        for mode, block in res["modes"].items():
            cov = block.get("coverage")
            cov_txt = f"  coverage {cov['coverage_pct']}% ({cov['members_with_data']}/{cov['members_in_window']})" if cov else ""
            print(f"-- {mode}: {block['symbols']} names{cov_txt}")
            for name, row in block["policies"].items():
                gate = row.get("gate_vs_pick_15", "")
                print(
                    f"   {name:<16} book {row['book_ret_pct']:8.2f}%  DD {row['max_dd_pct']:6.2f}%  "
                    f"(booked {row['max_dd_booked_pct']:5.2f}%)  sharpe {row['sharpe']}  trades {row['trades']:4d}  {gate}"
                )

    # Deflated Sharpe for the headline cell against everything in the family ledger.
    trials = load_trials(family=FAMILY)
    dsr_rows = []
    for res in results:
        for mode in pit.MODES:
            match = [t for t in trials if t["window"] == res["label"] and t["label"] == f"pick_15/{mode}"]
            if match:
                d = deflate_trial(match[-1], trials)
                if d:
                    d.update({"window": res["label"], "universe": mode})
                    dsr_rows.append(d)

    payload = {
        "research_only": True,
        "cost_rt_bps": COST_BPS,
        "sleeve": SLEEVE,
        "membership_file": str(pit.SP500_MEMBERSHIP_PATH.relative_to(ROOT)),
        "windows": results,
        "deflated_sharpe_pick_15": dsr_rows,
        "ledger_trials_in_family": len(trials),
        "note": "No .env / restart. Universe = current is the look-ahead list older findings used.",
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    lines = [
        "# NYSE MA70 walk on honest universes (research only)",
        "",
        f"No orders, no `.env`, no restart. {COST_BPS:.0f} bps round trip, sleeve {SLEEVE:.2f}, drawdown marked to market.",
        "",
        "`current` = runtime list incl. today's screener JSON (look-ahead). `frozen` = static lists only. "
        "`sp500` = S&P 500 membership by date (`data/sp500_ticker_start_end.csv`).",
        "",
    ]
    for res in results:
        lines.append(f"## Window {res['label']}: {res['start']} -> {res['end']}")
        lines.append("")
        for mode, block in res["modes"].items():
            cov = block.get("coverage")
            if cov:
                lines.append(
                    f"`{mode}`: {block['symbols']} names with data; PIT members in window {cov['members_in_window']}, "
                    f"with price data {cov['members_with_data']} ({cov['coverage_pct']}%). "
                    f"Missing names are the remaining survivorship gap: {', '.join(cov['missing_sample'][:12])}..."
                )
            else:
                lines.append(f"`{mode}`: {block['symbols']} names")
        lines.append("")
        lines.append("| policy | " + " | ".join(f"{m} book% / DD% / Sharpe / trades" for m in pit.MODES) + " | gate (sp500) |")
        lines.append("|---|" + "---|" * len(pit.MODES) + "---|")
        names = list(res["modes"]["current"]["policies"])
        for name in names:
            cells = []
            for m in pit.MODES:
                r = res["modes"][m]["policies"].get(name)
                cells.append(
                    f"{r['book_ret_pct']:+.1f} / {r['max_dd_pct']:.1f} / {r['sharpe']} / {r['trades']}" if r else "—"
                )
            gate = res["modes"]["sp500"]["policies"].get(name, {}).get("gate_vs_pick_15", "base")
            lines.append(f"| {name} | " + " | ".join(cells) + f" | {gate} |")
        lines.append("")
    if dsr_rows:
        lines.append("## Deflated Sharpe, pick_15 (vs all trials in family ledger)")
        lines.append("")
        lines.append("| window | universe | Sharpe (ann) | E[max SR] (ann) | trials | DSR | PSR>0 |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for d in dsr_rows:
            lines.append(
                f"| {d['window']} | {d['universe']} | {d['sharpe_annual']} | {d['sr0_annual']} | {d['n_trials']} | {d['dsr']} | {d['psr_vs_zero']} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Read",
            "",
            "- Promote rule unchanged: book% > pick_15 **and** maxDD not worse by > 2 pp, on the `sp500` column, both windows.",
            "- `current` numbers are not evidence. They are what the screener looked like on the day the JSON was written.",
            "- DSR < 0.95 means the Sharpe is not distinguishable from the best of N lucky trials.",
            "",
        ]
    )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {OUT_JSON}\nWrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
