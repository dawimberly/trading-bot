"""Daily-model arm P versus baseline A. Does not deploy or touch Live.

The pre-registration in results/profit_protection_prereg.md is the frozen rule.
This script does not retune it. sim_2021_2026_v2 is not selected.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd

from engine import EVAL_SESSIONS, INITIAL_CASH, WARMUP, Bar, simulate
from run_abc import (
    COVERAGE_GATE,
    DATA,
    DB_PATH,
    RESULTS,
    audit,
    build_windows,
    dividend_map,
    load_tables,
    member_on,
    membership_index,
    panel_for,
)
from run_rotation import GEV_DISTRIBUTION, alias_fi
from membership import research_membership

OUT = RESULTS / "profit_protection.json"
COMPLETED = "distribution_fraction"


def completed_trades(result: dict) -> int:
    reasons = result.get("exit_reasons", result.get("reasons", {}))
    return sum(count for reason, count in reasons.items() if reason != COMPLETED)


def gate(base: dict, other: dict) -> dict:
    trades = completed_trades(other)
    ret_ok = other["net_return"] > base["net_return"]
    dd_ok = other["max_drawdown"] <= base["max_drawdown"] + 1e-12
    trade_ok = trades >= 30
    return {
        "return_beats_baseline": ret_ok,
        "drawdown_not_worse": dd_ok,
        "completed_trades": trades,
        "at_least_30_trades": trade_ok,
        "pass": bool(ret_ok and dd_ok and trade_ok),
    }


def slim(result: dict) -> dict:
    reasons = dict(result["reasons"])
    return {
        "arm": result["arm"],
        "start": result["start"],
        "end": result["end"],
        "sessions": result["sessions"],
        "net_return": result["net_return"],
        "ending_equity": result["ending_equity"],
        "ending_cash": result["ending_cash"],
        "max_drawdown": result["max_drawdown"],
        "avg_exposure": result["avg_exposure"],
        "turnover": result["turnover"],
        "trades": result["trades"],
        "completed_trades": completed_trades(result),
        "cost_dollars": result["cost_dollars"],
        "dividend_dollars": result["dividend_dollars"],
        "stale_terminals": result["stale_terminals"],
        "reconciled": result["reconciled"],
        "distribution_unpriced": result["distribution_unpriced"],
        "exit_reasons": reasons,
    }


def main() -> int:
    if not DB_PATH.is_file():
        payload = {"status": "BLOCKED", "reason": "frozen research panel is missing", "portfolio_runs": False}
        OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print("BLOCKED no research database", flush=True)
        return 1
    calendar = json.loads((DATA / "calendar.json").read_text(encoding="utf-8"))
    windows = build_windows(calendar)
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    bars, divs, splits, notes = load_tables(conn)
    conn.close()
    membership = membership_index(research_membership())
    lookup = {}
    for rec in bars.itertuples(index=False):
        lookup.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
    alias_fi(lookup)
    audit_bars = bars
    if "FI" not in set(bars["symbol"]) and "FISV" in lookup:
        extra = [
            {"symbol": "FI", "session": day, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
            for day, b in lookup["FI"].items()
        ]
        audit_bars = pd.concat([bars, pd.DataFrame(extra)], ignore_index=True)
    coverage = audit(windows["span"], {"prior": windows["prior"], "recent": windows["recent"]}, audit_bars, membership)
    spy_ok = not coverage["spy_missing_sessions"]
    cov_ok = all(row["coverage"] >= COVERAGE_GATE for row in coverage["windows"].values())
    if not spy_ok or not cov_ok:
        payload = {
            "status": "BLOCKED",
            "reason": "coverage gate failed" if not cov_ok else "SPY calendar incomplete",
            "portfolio_runs": False,
            "coverage": {k: coverage["windows"][k]["coverage"] for k in coverage["windows"]},
            "label": "daily-model experiment",
        }
        OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print("BLOCKED coverage", flush=True)
        return 1

    div_all = dividend_map(divs, splits, windows["span"])
    report = {
        "status": "COMPLETED",
        "label": "daily-model experiment",
        "live_validation": False,
        "fidelity": (
            "Daily baseline A only: MA70 rank, 15 names, 30-session hold, 2x ATR stop, "
            "next-open fills, 5 bps per side. This is not the Live stack."
        ),
        "preregistration": "results/profit_protection_prereg.md",
        "slip_bps_per_side": 5,
        "eval_sessions": EVAL_SESSIONS,
        "warmup_sessions": WARMUP,
        "initial_cash": INITIAL_CASH,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows": {},
        "pass": False,
    }
    blocked = False
    for label, dates in (("prior", windows["prior"]), ("recent", windows["recent"])):
        symbols = sorted(
            s for s in set(lookup) - {"SPY"} if any(member_on(membership, s, d) for d in dates[WARMUP:])
        )
        for needed in ("FI", "FISV"):
            if needed in lookup and needed not in symbols and any(member_on(membership, needed, d) for d in dates):
                symbols.append(needed)
        symbols = sorted(set(symbols))
        panel, member, spy = panel_for(dates, symbols, lookup, membership)
        div_window = {s: {d: v for d, v in days.items() if d in set(dates)} for s, days in div_all.items()}
        renames = {"2025-11-11": [("FI", "FISV")]} if "2025-11-11" in dates else {}
        arms = {}
        for arm in ("A", "P"):
            print(f"run {label} {arm}", flush=True)
            out = simulate(
                panel,
                member,
                dates,
                spy,
                arm=arm,
                decision_start=WARMUP,
                dividends=div_window,
                renames=renames,
                distributions=[GEV_DISTRIBUTION],
            )
            arms[arm] = slim(out)
            if not out["reconciled"] or out["distribution_unpriced"]:
                blocked = True
        comparison = gate(arms["A"], arms["P"])
        report["windows"][label] = {
            "eval_start": dates[WARMUP],
            "eval_end": dates[-1],
            "sessions": EVAL_SESSIONS,
            "coverage": coverage["windows"][label]["coverage"],
            "A": arms["A"],
            "P": arms["P"],
            "gate": comparison,
        }
        print(
            f"{label} A {arms['A']['net_return']:.4f} dd {arms['A']['max_drawdown']:.4f} "
            f"P {arms['P']['net_return']:.4f} dd {arms['P']['max_drawdown']:.4f} "
            f"trades {comparison['completed_trades']} pass {comparison['pass']}",
            flush=True,
        )
    if blocked:
        report["status"] = "BLOCKED"
        report["reason"] = "cash reconciliation failed or a distribution was unpriced"
        report["pass"] = False
    else:
        report["pass"] = all(row["gate"]["pass"] for row in report["windows"].values())
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("wrote", OUT, "pass", report["pass"], flush=True)
    return 0 if report["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
