"""Single-knob A/B of momentum crash fixes on the PIT S&P book (research only).

Baseline: NYSE MA70 pick_15, 30d hold, 2× ATR, sleeve 0.95, 10 bps RT,
S&P 500 membership by date, drawdown marked to market.

Knobs (one at a time):

  risk_adj   rank by (price/MA − 1) / 20d vol  (GRJMOM-style)
  vol_12     scale the sleeve to a 12% annual vol target using the prior
             126-bar book path (Barroso & Santa-Clara)

Usage (from stock-bot/):
  python scripts/analysis/vol_momentum_ab.py
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

OUT_JSON = Path(__file__).with_name("vol_momentum_ab_last.json")
OUT_MD = Path(__file__).with_name("vol_momentum_ab_last.md")
FAMILY = "nyse_ma70_walk"
COST_BPS = 10.0
SLEEVE = 0.95
DD_GATE_PP = 2.0
VOL_TARGET = 0.12
VOL_LOOKBACK = 126
RANK_VOL = 20


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
        "vol_scale_avg": r.get("vol_scale_avg"),
        "vol_days_scaled_pct": r.get("vol_days_scaled_pct"),
    }


def run_window(data, *, label: str, ma_win: int) -> dict:
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
    policies = {
        "pick_15": {},
        "risk_adj": {"rank_mode": "risk_adj", "rank_vol_lookback": RANK_VOL},
        "vol_12": {"vol_target": VOL_TARGET, "vol_target_lookback": VOL_LOOKBACK},
    }
    runs = {name: run_backtest(data, **common, **kw) for name, kw in policies.items()}
    base = runs["pick_15"]
    out = {
        "label": label,
        "start": base["start"],
        "end": base["end"],
        "coverage": coverage,
        "policies": {},
    }
    for name, r in runs.items():
        row = _slim(r)
        if name != "pick_15":
            row["gate_vs_pick_15"] = _gate(r, base)
            row["d_book_pp"] = round(r["book_ret_pct"] - base["book_ret_pct"], 2)
            row["d_dd_pp"] = round(r["max_dd_pct"] - base["max_dd_pct"], 2)
        out["policies"][name] = row
        record_trial(
            script="vol_momentum_ab",
            family=FAMILY,
            label=f"{name}/sp500",
            window=label,
            params={
                "universe": "sp500",
                "policy": name,
                "cost_rt_bps": COST_BPS,
                "sleeve": SLEEVE,
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

    windows = pit.disjoint_session_windows(data, days=365, ma_win=ma_win)
    results = []
    for label, frame in windows:
        print(f"\n== {label}: {len(frame)} bars ==")
        res = run_window(frame, label=label, ma_win=ma_win)
        results.append(res)
        for name, row in res["policies"].items():
            extra = ""
            if name != "pick_15":
                extra = f"  {row['gate_vs_pick_15']}  dRet {row['d_book_pp']:+.2f}pp  dDD {row['d_dd_pp']:+.2f}pp"
            scale = ""
            if row.get("vol_scale_avg") is not None:
                scale = f"  scale {row['vol_scale_avg']:.2f} ({row['vol_days_scaled_pct']}% days)"
            print(
                f"  {name:<10} book {row['book_ret_pct']:7.2f}%  DD {row['max_dd_pct']:6.2f}%  "
                f"sharpe {row['sharpe']}  trades {row['trades']:4d}{scale}{extra}"
            )

    trials = load_trials(family=FAMILY)
    dsr_rows = []
    for res in results:
        for name in ("pick_15", "risk_adj", "vol_12"):
            match = [t for t in trials if t["window"] == res["label"] and t["label"] == f"{name}/sp500"]
            if not match:
                continue
            d = deflate_trial(match[-1], trials)
            if d:
                d.update({"window": res["label"], "policy": name})
                dsr_rows.append(d)

    payload = {
        "research_only": True,
        "cost_rt_bps": COST_BPS,
        "sleeve": SLEEVE,
        "vol_target": VOL_TARGET,
        "windows": results,
        "deflated_sharpe": dsr_rows,
        "ledger_trials_in_family": len(trials),
        "note": "No .env / restart. One knob vs pick_15 on S&P PIT. Promote only if both windows PASS.",
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Momentum crash knobs on S&P PIT (research only)",
        "",
        f"No orders, no `.env`. {COST_BPS:.0f} bps RT, sleeve {SLEEVE:.2f}, pick_15, marked-to-market DD.",
        "Universe is S&P 500 membership by date.",
        "",
        f"`risk_adj` = rank by distance-to-MA / {RANK_VOL}d vol. "
        f"`vol_12` = sleeve × min(1, {VOL_TARGET:.0%} / {VOL_LOOKBACK}-bar book vol).",
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
        lines.append("| policy | book% | maxDD% | Sharpe | trades | vs pick_15 |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for name, row in res["policies"].items():
            gate = row.get("gate_vs_pick_15", "base")
            if name != "pick_15":
                gate = f"{gate} ({row['d_book_pp']:+.1f}pp ret, {row['d_dd_pp']:+.1f}pp DD)"
            lines.append(
                f"| {name} | {row['book_ret_pct']:+.2f} | {row['max_dd_pct']:.2f} | "
                f"{row['sharpe']} | {row['trades']} | {gate} |"
            )
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
            f"- Promote a knob only if book% > pick_15 **and** maxDD not worse by > {DD_GATE_PP:.1f} pp, both windows.",
            "- One knob. Do not combine risk_adj + vol_12 from this file.",
            "",
        ]
    )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {OUT_JSON}\nWrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
