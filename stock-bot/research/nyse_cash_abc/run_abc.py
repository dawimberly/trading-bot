"""Audit the research OHLC panel and run arms A, B, and C if the gates pass."""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from engine import (  # noqa: E402
    EVAL_SESSIONS,
    INITIAL_CASH,
    SLEEVE,
    Bar,
    WARMUP,
    adjusted_dividend_rate,
    simulate,
    usable,
)
from membership import research_membership  # noqa: E402

DATA = HERE / "data"
RESULTS = HERE / "results"
DB_PATH = DATA / "bars.sqlite"
COVERAGE_GATE = 0.95


def load_tables(conn: sqlite3.Connection):
    bars = pd.read_sql("SELECT symbol, session, open, high, low, close FROM bars", conn)
    divs = pd.read_sql("SELECT symbol, ex_date, rate FROM cash_dividends", conn)
    splits = pd.read_sql("SELECT symbol, ex_date, kind, new_rate, old_rate FROM splits", conn)
    notes = pd.read_sql("SELECT symbol, status, detail FROM fetch_notes", conn)
    return bars, divs, splits, notes


def membership_index(membership: pd.DataFrame) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for rec in membership.itertuples(index=False):
        out.setdefault(rec.ticker, []).append((pd.Timestamp(rec.start), rec.end))
    return out


def member_on(index: dict[str, list[tuple]], ticker: str, session: str) -> bool:
    day = pd.Timestamp(session)
    for start, end in index.get(ticker, []):
        if start <= day and (pd.isna(end) or day < pd.Timestamp(end)):
            return True
    return False


def build_windows(calendar: list[str]) -> dict:
    need = WARMUP + 2 * EVAL_SESSIONS
    if len(calendar) < need:
        raise RuntimeError(f"calendar {len(calendar)} < {need}")
    span = calendar[-need:]
    prior = span[: WARMUP + EVAL_SESSIONS]
    recent = span[-(WARMUP + EVAL_SESSIONS) :]
    prior_eval = prior[WARMUP:]
    recent_eval = recent[WARMUP:]
    if prior_eval[-1] >= recent_eval[0]:
        raise RuntimeError("evaluation windows overlap")
    if len(prior_eval) != EVAL_SESSIONS or len(recent_eval) != EVAL_SESSIONS:
        raise RuntimeError("evaluation window length is not 365")
    return {"span": span, "prior": prior, "recent": recent}


def audit(calendar_span, frames, bars, membership_idx) -> dict:
    lookup = {}
    for rec in bars.itertuples(index=False):
        lookup.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
    spy = lookup.get("SPY", {})
    spy_missing = [d for d in calendar_span if d not in spy or not usable(spy[d])]
    report = {"spy_missing_sessions": spy_missing, "windows": {}}
    for label, dates in frames.items():
        eval_dates = dates[WARMUP:]
        eval_start = pd.Timestamp(eval_dates[0])
        eval_end = pd.Timestamp(eval_dates[-1])
        symbols = []
        for ticker, spans in membership_idx.items():
            if ticker == "SPY":
                continue
            if any(start <= eval_end and (pd.isna(end) or pd.Timestamp(end) > eval_start) for start, end in spans):
                symbols.append(ticker)
        symbols.sort()
        member_days = 0
        covered = 0
        interior = 0
        missing_symbols = []
        for sym in symbols:
            series = lookup.get(sym, {})
            flags = []
            for day in eval_dates:
                if not member_on(membership_idx, sym, day):
                    flags.append(None)
                    continue
                member_days += 1
                ok = usable(series.get(day))
                flags.append(ok)
                covered += int(ok)
            if not any(flags):
                missing_symbols.append(sym)
            for i, flag in enumerate(flags):
                if flag is not False:
                    continue
                prev = next((flags[j] for j in range(i - 1, -1, -1) if flags[j] is not None), None)
                nxt = next((flags[j] for j in range(i + 1, len(flags)) if flags[j] is not None), None)
                if prev is True and nxt is True:
                    interior += 1
        report["windows"][label] = {
            "eval_start": eval_dates[0],
            "eval_end": eval_dates[-1],
            "eval_sessions": len(eval_dates),
            "members": len(symbols),
            "member_days": member_days,
            "covered_member_days": covered,
            "coverage": covered / member_days if member_days else 0.0,
            "interior_holes": interior,
            "symbols_with_no_usable_member_bar": missing_symbols,
        }
    return report


def dividend_map(divs: pd.DataFrame, splits: pd.DataFrame, dates: list[str]) -> dict[str, dict[str, float]]:
    split_map: dict[str, list[tuple[str, float]]] = {}
    for rec in splits.itertuples(index=False):
        if not rec.old_rate:
            continue
        split_map.setdefault(rec.symbol, []).append((rec.ex_date, float(rec.new_rate) / float(rec.old_rate)))
    session_set = set(dates)
    ordered = dates
    out: dict[str, dict[str, float]] = {}
    for rec in divs.itertuples(index=False):
        rate = adjusted_dividend_rate(float(rec.rate), rec.ex_date, split_map.get(rec.symbol, []))
        if rate <= 0:
            continue
        day = rec.ex_date if rec.ex_date in session_set else next((d for d in ordered if d >= rec.ex_date), None)
        if day is None:
            continue
        out.setdefault(rec.symbol, {}).setdefault(day, 0.0)
        out[rec.symbol][day] += rate
    return out


def panel_for(dates, symbols, lookup, membership_idx):
    bars = {}
    member = {}
    for sym in symbols:
        series = lookup.get(sym, {})
        bars[sym] = []
        member[sym] = []
        for day in dates:
            raw = series.get(day)
            bars[sym].append(raw if usable(raw) else None)
            member[sym].append(member_on(membership_idx, sym, day))
    spy = []
    spy_series = lookup.get("SPY", {})
    for day in dates:
        raw = spy_series.get(day)
        spy.append(raw.close if usable(raw) else None)
    return bars, member, spy


def slim(result: dict) -> dict:
    keep = (
        "arm", "start", "end", "sessions", "net_return", "ending_equity", "ending_cash",
        "max_drawdown", "avg_exposure", "turnover", "trades", "cost_dollars",
        "dividend_dollars", "vol_blocks", "stale_terminals", "sharpe", "reconciled", "reasons",
    )
    return {k: result[k] for k in keep}


def pct(value: float) -> str:
    return f"{100.0 * value:.2f}%"


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromName("test_engine")
    outcome = unittest.TextTestRunner(verbosity=1).run(suite)
    if not outcome.wasSuccessful():
        payload = {"status": "BLOCKED", "reason": "synthetic tests failed", "tests_failed": len(outcome.failures) + len(outcome.errors)}
        (RESULTS / "abc_results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print("BLOCKED synthetic tests", flush=True)
        return 1
    if not DB_PATH.is_file():
        print("BLOCKED no research database", flush=True)
        return 1
    calendar = json.loads((DATA / "calendar.json").read_text(encoding="utf-8"))
    windows = build_windows(calendar)
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    bars, divs, splits, notes = load_tables(conn)
    conn.close()
    from run_rotation import (  # local import: run_rotation already imports this module
        GEV_DISTRIBUTION,
        action_hits,
        alias_fi,
        deduped_actions,
        material_block,
    )

    membership = membership_index(research_membership())
    lookup_fi = {}
    for rec in bars.itertuples(index=False):
        lookup_fi.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
    alias_fi(lookup_fi)
    audit_bars = bars
    if "FI" not in set(bars["symbol"]) and "FISV" in lookup_fi:
        extra = [
            {"symbol": "FI", "session": day, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
            for day, b in lookup_fi["FI"].items()
        ]
        audit_bars = pd.concat([bars, pd.DataFrame(extra)], ignore_index=True)
    coverage = audit(windows["span"], {"prior": windows["prior"], "recent": windows["recent"]}, audit_bars, membership)
    coverage["gate"] = COVERAGE_GATE
    coverage["notes"] = {
        "ok": int((notes["status"] == "ok").sum()) if not notes.empty else 0,
        "no_bars": int((notes["status"] == "no_bars").sum()) if not notes.empty else 0,
        "error": int((notes["status"] == "error").sum()) if not notes.empty else 0,
    }
    (RESULTS / "coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    spy_ok = not coverage["spy_missing_sessions"]
    cov_ok = all(row["coverage"] >= COVERAGE_GATE for row in coverage["windows"].values())
    if not spy_ok or not cov_ok:
        status = {
            "status": "BLOCKED",
            "reason": "coverage gate failed" if not cov_ok else "SPY calendar incomplete",
            "coverage": coverage,
            "portfolio_runs": False,
        }
        (RESULTS / "abc_results.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        print("BLOCKED coverage", json.dumps({k: coverage["windows"][k]["coverage"] for k in coverage["windows"]}), flush=True)
        return 1

    lookup = lookup_fi
    div_all = dividend_map(divs, splits, windows["span"])
    actions = deduped_actions(json.loads((DATA / "unapplied_actions.json").read_text(encoding="utf-8"))["rows"])
    results = {
        "status": "COMPLETED",
        "arms": {},
        "comparisons": {},
        "validation": {},
        "book": "research membership, FI aliased to the FISV tape, GE Vernova distribution, corrected NVDA high, Paramount PARA tape",
        "limitations": [],
    }
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
        results["arms"][label] = {}
        results["validation"][label] = {}
        for arm in ("A", "B", "C"):
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
            block = material_block(action_hits(out["trade_log"], actions, dates, lookup))
            results["arms"][label][arm] = slim(out)
            results["validation"][label][arm] = {
                "reconciled": out["reconciled"],
                "distribution_unpriced": out["distribution_unpriced"],
                "blocked": bool(block["blocked"] or not out["reconciled"] or out["distribution_unpriced"]),
                "priced_total": block["priced_total"],
                "hit_count": len(block["hits"]),
                "hits": block["hits"],
            }
            if results["validation"][label][arm]["blocked"]:
                results["status"] = "BLOCKED"
                results["reason"] = f"{label} {arm} failed cash reconciliation or the corporate-action gate"
        base = results["arms"][label]["A"]
        results["comparisons"][label] = {}
        for arm in ("B", "C"):
            other = results["arms"][label][arm]
            base_clean = not results["validation"][label]["A"]["blocked"]
            other_clean = not results["validation"][label][arm]["blocked"]
            results["comparisons"][label][arm] = {
                "return_minus_A": other["net_return"] - base["net_return"],
                "max_drawdown_minus_A": other["max_drawdown"] - base["max_drawdown"],
                "return_gate": other["net_return"] > base["net_return"],
                "drawdown_gate": other["max_drawdown"] <= base["max_drawdown"] + 0.020 + 1e-12,
                "validation_clean": bool(base_clean and other_clean),
            }
            row = results["comparisons"][label][arm]
            row["pass"] = bool(row["return_gate"] and row["drawdown_gate"] and row["validation_clean"])
    results["same_close_booked_equity_results"] = "not used; those historical figures are a different engine"
    results["generated_at"] = datetime.now(timezone.utc).isoformat()
    results["initial_cash"] = INITIAL_CASH
    results["sleeve"] = SLEEVE
    results["cost_model"] = "5 bps adverse slippage per side, once; no separate fee"
    (RESULTS / "abc_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    write_md(results, coverage)
    print(results["status"], flush=True)
    return 0 if results["status"] == "COMPLETED" else 1


def write_md(results: dict, coverage: dict) -> None:
    lines = [
        "# Corrected NYSE cash walk A/B/C",
        "",
        f"Status: **{results['status']}**.",
        "",
        "These figures use next-open fills and a cash ledger. They are not the historical same-close booked-equity results.",
        "",
        f"Costs: {results['cost_model']}. Starting cash ${results['initial_cash']:,.0f}. Sleeve {results['sleeve']:.2f}.",
        "",
        "## Coverage",
        "",
    ]
    for label, row in coverage["windows"].items():
        lines.append(
            f"- {label} {row['eval_start']} → {row['eval_end']}: "
            f"{row['covered_member_days']}/{row['member_days']} member-sessions "
            f"({100 * row['coverage']:.2f}%), interior holes {row['interior_holes']}, "
            f"symbols with no usable bar {len(row['symbols_with_no_usable_member_bar'])}."
        )
    lines.extend(["", "## Results", ""])
    for label, arms in results["arms"].items():
        lines.append(f"### {label}")
        lines.append("")
        lines.append("| Arm | Net return | Max DD | Exposure | Turnover | Trades | Costs | End cash | End equity |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for arm, row in arms.items():
            lines.append(
                f"| {arm} | {pct(row['net_return'])} | {pct(row['max_drawdown'])} | {pct(row['avg_exposure'])} | "
                f"{row['turnover']:.2f}x | {row['trades']} | ${row['cost_dollars']:,.0f} | "
                f"${row['ending_cash']:,.0f} | ${row['ending_equity']:,.0f} |"
            )
        lines.append("")
        for arm, diff in results["comparisons"][label].items():
            verdict = "PASS" if diff.get("pass") else "FAIL"
            lines.append(
                f"- {arm} minus A: return {100 * diff['return_minus_A']:+.2f} pp, "
                f"max drawdown {100 * diff['max_drawdown_minus_A']:+.2f} pp, "
                f"validation clean {diff.get('validation_clean')}, protocol {verdict}."
            )
        lines.append("")
    lines.append("Protocol pass rule was not used to change any parameter after these numbers were computed.")
    (RESULTS / "abc_results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
