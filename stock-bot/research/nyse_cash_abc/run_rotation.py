"""Corrected baseline A versus A plus the fixed rotation rule.

Research folder only. Does not write market_data.db, commit, or send orders.
Rotation is not started when a held name crosses an unapplied corporate action
worth at least the materiality limits below.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from engine import INITIAL_CASH, SLEEVE, WARMUP, Bar, Distribution, adjusted_dividend_rate, simulate  # noqa: E402
from membership import research_membership  # noqa: E402
from run_abc import (  # noqa: E402
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
    pct,
)

# Fixed before either window is scored. One percent and two percent of starting cash.
MATERIAL_EVENT = 1_000.0
MATERIAL_TOTAL = 2_000.0
APPLIED_SPINS = {("GE", "GEV", "2024-04-02")}
GEV_DISTRIBUTION = Distribution("2024-04-02", "GE", "GEV", 0.25)


def alias_fi(lookup: dict) -> None:
    """Fiserv's SIP tape is continuous under FISV. FI itself returned no bars."""
    if lookup.get("FI"):
        return
    donor = lookup.get("FISV")
    if donor:
        lookup["FI"] = dict(donor)


def nvda_example(conn: sqlite3.Connection) -> dict:
    splits = conn.execute(
        "SELECT ex_date, new_rate, old_rate FROM splits WHERE symbol='NVDA' ORDER BY ex_date"
    ).fetchall()
    divs = conn.execute(
        "SELECT ex_date, rate FROM cash_dividends WHERE symbol='NVDA' AND ex_date BETWEEN '2024-01-01' AND '2024-12-31' ORDER BY ex_date"
    ).fetchall()
    bars = conn.execute(
        "SELECT session, open, high, low, close FROM bars WHERE symbol='NVDA' AND session BETWEEN '2024-06-07' AND '2024-06-11' ORDER BY session"
    ).fetchall()
    factors = [(ex, new / old) for ex, new, old in splits if old]
    credited = [
        {"ex_date": ex, "raw_rate": rate, "split_adjusted_rate": adjusted_dividend_rate(rate, ex, factors)}
        for ex, rate in divs
    ]
    return {"splits": [{"ex_date": ex, "new_rate": new, "old_rate": old} for ex, new, old in splits], "dividends": credited, "bars": [
        {"session": s, "open": o, "high": h, "low": l, "close": c} for s, o, h, l, c in bars
    ]}


def tdg_example(conn: sqlite3.Connection) -> dict:
    rate = conn.execute(
        "SELECT rate FROM cash_dividends WHERE symbol='TDG' AND ex_date='2025-09-02'"
    ).fetchone()
    bars = conn.execute(
        "SELECT session, close FROM bars WHERE symbol='TDG' AND session IN ('2025-08-29', '2025-09-02') ORDER BY session"
    ).fetchall()
    return {"cash_rate": None if rate is None else rate[0], "closes": [{"session": s, "close": c} for s, c in bars]}


def map_session(day: str, dates: list[str]) -> str | None:
    if day < dates[0] or day > dates[-1]:
        return None
    if day in dates:
        return day
    return next((d for d in dates if d >= day), None)


def spans(trade: dict, day: str) -> bool:
    return trade["entry_date"] < day <= trade["exit_date"]


def deduped_actions(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        if row["kind"] == "spin_offs":
            key = ("spin", row.get("source_symbol"), row.get("new_symbol"), row.get("ex_date"), round(float(row.get("new_rate") or 0), 6))
        elif row["kind"] in {"cash_mergers", "stock_mergers", "stock_and_cash_mergers"}:
            key = (row["kind"], row.get("acquiree_symbol"), row.get("acquirer_symbol"), row.get("effective_date"), round(float(row.get("rate") or row.get("cash_rate") or 0), 6))
        elif row["kind"] in {"stock_dividends", "unit_splits"}:
            key = (row["kind"], row.get("symbol") or row.get("old_symbol"), row.get("ex_date") or row.get("effective_date"), round(float(row.get("rate") or row.get("new_rate") or 0), 6))
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def open_on(lookup, symbol: str, day: str) -> float | None:
    bar = lookup.get(symbol, {}).get(day)
    if bar is None:
        return None
    return float(bar.open)


def close_on(lookup, symbol: str, day: str) -> float | None:
    bar = lookup.get(symbol, {}).get(day)
    if bar is None:
        return None
    return float(bar.close)


def action_hits(trades: list[dict], actions: list[dict], dates: list[str], lookup) -> list[dict]:
    hits = []
    for row in actions:
        kind = row["kind"]
        if kind == "spin_offs":
            symbol = row.get("source_symbol")
            raw_day = row.get("ex_date")
            if (symbol, row.get("new_symbol"), str(raw_day)) in APPLIED_SPINS:
                continue
        elif kind in {"cash_mergers", "stock_mergers", "stock_and_cash_mergers"}:
            symbol = row.get("acquiree_symbol")
            raw_day = row.get("effective_date")
        elif kind == "stock_dividends":
            symbol = row.get("symbol")
            raw_day = row.get("ex_date")
        elif kind == "unit_splits":
            symbol = row.get("old_symbol")
            raw_day = row.get("effective_date")
        else:
            continue
        if not symbol or not raw_day:
            continue
        day = map_session(str(raw_day), dates)
        if day is None:
            continue
        covered = [t for t in trades if t["symbol"] == symbol and spans(t, day)]
        if not covered:
            continue
        shares = sum(float(t["shares"]) for t in covered)
        event_close = close_on(lookup, symbol, day)
        exited_today = [t for t in covered if t["exit_date"] == day]
        if exited_today:
            ref_px = float(exited_today[-1]["exit_fill"])
        elif event_close is not None:
            ref_px = event_close
        else:
            ref_px = float(covered[-1]["exit_fill"])
        missed = None
        priced = True
        detail = ""
        if kind == "spin_offs":
            source_rate = float(row.get("source_rate") or 0)
            new_rate = float(row.get("new_rate") or 0)
            child = open_on(lookup, str(row.get("new_symbol")), day)
            if source_rate <= 0 or new_rate <= 0 or child is None:
                priced = False
                detail = "child price or rate missing"
            else:
                missed = shares * (new_rate / source_rate) * child
                detail = f"{row.get('new_symbol')} open {child:.4f}"
        elif kind == "cash_mergers":
            rate = float(row.get("rate") or 0)
            missed = abs(rate - ref_px) * shares
            detail = f"cash {rate:.4f} vs mark {ref_px:.4f}"
        elif kind == "stock_mergers":
            acq = open_on(lookup, str(row.get("acquirer_symbol")), day)
            acq_rate = float(row.get("acquirer_rate") or 0)
            old_rate = float(row.get("acquiree_rate") or 0)
            if acq is None or old_rate <= 0:
                priced = False
                detail = "acquirer price missing"
            else:
                fair = acq * acq_rate / old_rate
                missed = abs(fair - ref_px) * shares
                detail = f"{row.get('acquirer_symbol')} consideration {fair:.4f} vs mark {ref_px:.4f}"
        elif kind == "stock_and_cash_mergers":
            acq = open_on(lookup, str(row.get("acquirer_symbol")), day)
            acq_rate = float(row.get("acquirer_rate") or 0)
            old_rate = float(row.get("acquiree_rate") or 0)
            cash_rate = float(row.get("cash_rate") or 0)
            if old_rate <= 0 or (acq_rate > 0 and acq is None):
                priced = False
                detail = "merger terms not priced"
            else:
                fair = cash_rate + (0.0 if acq is None else acq * acq_rate / old_rate)
                missed = abs(fair - ref_px) * shares
                detail = f"consideration {fair:.4f} vs mark {ref_px:.4f}"
        else:
            priced = False
            detail = kind
        hits.append(
            {
                "kind": kind,
                "symbol": symbol,
                "session": day,
                "shares": shares,
                "priced": priced,
                "missed_dollars": missed,
                "detail": detail,
            }
        )
    return hits


def material_block(hits: list[dict]) -> dict:
    unpriced = [h for h in hits if not h["priced"]]
    priced = [h for h in hits if h["priced"] and h["missed_dollars"] is not None]
    total = sum(float(h["missed_dollars"]) for h in priced)
    large = [h for h in priced if float(h["missed_dollars"]) >= MATERIAL_EVENT]
    blocked = bool(unpriced or large or total >= MATERIAL_TOTAL)
    return {
        "blocked": blocked,
        "event_limit": MATERIAL_EVENT,
        "total_limit": MATERIAL_TOTAL,
        "priced_total": total,
        "large_events": large,
        "unpriced_events": unpriced,
        "hits": hits,
    }


def run_arm(panel, member, dates, spy, div_window, arm: str, journal: list | None = None, distributions=None) -> dict:
    renames = {}
    if "2025-11-11" in dates:
        renames["2025-11-11"] = [("FI", "FISV")]
    return simulate(
        panel,
        member,
        dates,
        spy,
        arm=arm,
        decision_start=WARMUP,
        dividends=div_window,
        renames=renames,
        distributions=[GEV_DISTRIBUTION] if distributions is None else distributions,
        journal=journal,
    )


def slim(result: dict) -> dict:
    keep = (
        "arm", "start", "end", "sessions", "net_return", "ending_equity", "ending_cash",
        "max_drawdown", "avg_exposure", "turnover", "trades", "cost_dollars",
        "dividend_dollars", "replacements", "rotation_buys", "rotation_preempted",
        "distribution_unpriced", "stale_terminals", "reconciled", "reasons",
    )
    return {k: result[k] for k in keep}


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromName("test_engine")
    outcome = unittest.TextTestRunner(verbosity=1).run(suite)
    if not outcome.wasSuccessful():
        print("BLOCKED synthetic tests", flush=True)
        return 1
    calendar = json.loads((DATA / "calendar.json").read_text(encoding="utf-8"))
    windows = build_windows(calendar)
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    bars, divs, splits, notes = load_tables(conn)
    example = {"nvda": nvda_example(conn), "tdg": tdg_example(conn)}
    conn.close()
    membership = membership_index(research_membership())
    lookup = {}
    for rec in bars.itertuples(index=False):
        lookup.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
    alias_fi(lookup)
    fi_bars = bars
    if "FI" not in set(bars["symbol"]) and "FISV" in lookup:
        extra = [
            {"symbol": "FI", "session": day, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
            for day, b in lookup["FI"].items()
        ]
        fi_bars = __import__("pandas").concat([bars, __import__("pandas").DataFrame(extra)], ignore_index=True)
    coverage = audit(windows["span"], {"prior": windows["prior"], "recent": windows["recent"]}, fi_bars, membership)
    coverage["gate"] = COVERAGE_GATE
    coverage["membership"] = "production file through 2026-08-18, plus S&P DJI changes effective 2026-09-21, 2026-10-01, and 2026-10-06"
    (RESULTS / "coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    if coverage["spy_missing_sessions"] or any(row["coverage"] < COVERAGE_GATE for row in coverage["windows"].values()):
        print("BLOCKED coverage", flush=True)
        write_validation(example, coverage, {"status": "BLOCKED", "reason": "coverage gate"}, None)
        return 1

    actions = deduped_actions(json.loads((DATA / "unapplied_actions.json").read_text(encoding="utf-8"))["rows"])
    div_all = dividend_map(divs, splits, windows["span"])
    arms = {}
    blocks = {}
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
        print(f"run {label} A", flush=True)
        out = run_arm(panel, member, dates, spy, div_window, "A")
        arms[label] = {"A": out, "panel": panel, "member": member, "spy": spy, "div": div_window}
        blocks[label] = material_block(action_hits(out["trade_log"], actions, dates, lookup))
        print(
            f"audit {label} hits={len(blocks[label]['hits'])} "
            f"missed={blocks[label]['priced_total']:.0f} blocked={blocks[label]['blocked']}",
            flush=True,
        )
    blocked = any(
        not arms[k]["A"]["reconciled"] or arms[k]["A"]["distribution_unpriced"] or blocks[k]["blocked"]
        for k in arms
    )
    validation = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED" if blocked else "PASS",
        "nvda": example["nvda"],
        "tdg": example["tdg"],
        "fi": "FI has no SIP bars. Prices are the continuous FISV tape. A position still named FI on 2025-11-11 is renamed to FISV.",
        "windows": {k: {"start": v[WARMUP], "end": v[-1]} for k, v in (("prior", windows["prior"]), ("recent", windows["recent"]))},
        "coverage": {k: coverage["windows"][k]["coverage"] for k in coverage["windows"]},
        "materiality": {k: {kk: vv for kk, vv in blocks[k].items() if kk != "hits"} | {"hit_count": len(blocks[k]["hits"]), "hits": blocks[k]["hits"]} for k in blocks},
        "corrected_A": {k: slim(arms[k]["A"]) for k in arms},
    }
    if blocked:
        if any(blocks[k]["blocked"] for k in blocks):
            reason = "unapplied corporate action is material for a held name"
        elif any(arms[k]["A"]["distribution_unpriced"] for k in arms):
            reason = "GE Vernova distribution had no regular-way open"
        else:
            reason = "cash ledger did not reconcile"
        validation["reason"] = reason
        write_validation(example, coverage, validation, None)
        (RESULTS / "rotation_results.json").write_text(json.dumps({"status": "BLOCKED", "reason": reason}, indent=2), encoding="utf-8")
        print("BLOCKED before rotation", reason, flush=True)
        return 2

    comparison = {"status": "COMPLETED", "arms": {}, "verdict": {}, "generated_at": datetime.now(timezone.utc).isoformat()}
    for label in ("prior", "recent"):
        print(f"run {label} R", flush=True)
        out = run_arm(arms[label]["panel"], arms[label]["member"], windows[label], arms[label]["spy"], arms[label]["div"], "R")
        base = slim(arms[label]["A"])
        other = slim(out)
        gates = {
            "return_minus_A": other["net_return"] - base["net_return"],
            "max_drawdown_minus_A": other["max_drawdown"] - base["max_drawdown"],
            "return_gate": other["net_return"] > base["net_return"],
            "drawdown_gate": other["max_drawdown"] <= base["max_drawdown"] + 0.020 + 1e-12,
        }
        gates["pass"] = bool(gates["return_gate"] and gates["drawdown_gate"] and other["reconciled"] and base["reconciled"])
        comparison["arms"][label] = {"A": base, "R": other, "gates": gates}
    comparison["verdict"]["pass"] = all(comparison["arms"][k]["gates"]["pass"] for k in comparison["arms"])
    comparison["verdict"]["rule"] = "R beats A on net return in both windows, and max drawdown is not worse by more than 2.0 percentage points in either window"
    (RESULTS / "rotation_results.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    write_validation(example, coverage, validation, comparison)
    print("VERDICT", "PASS" if comparison["verdict"]["pass"] else "FAIL", flush=True)
    return 0


def write_validation(example: dict, coverage: dict, validation: dict, comparison: dict | None) -> None:
    lines = [
        "# Baseline validation and rotation",
        "",
        f"Validation status: **{validation.get('status', 'BLOCKED')}**.",
        "",
        "## Dividend accounting",
        "",
        "Prices are split-adjusted. Cash dividends are credited once, on the split-adjusted share count. Share count is not changed on a split date.",
        "",
    ]
    nvda = example["nvda"]
    lines.append("NVDA forward split 10-for-1, ex-date 2024-06-10. Closes around that session:")
    lines.append("")
    for row in nvda["bars"]:
        lines.append(f"- {row['session']}: open {row['open']:.2f}, high {row['high']:.2f}, low {row['low']:.2f}, close {row['close']:.2f}")
    lines.append("")
    lines.append("The close on the split session stays near the prior close, so the tape is already on post-split shares. Crediting the raw pre-split dividend on those shares would pay ten times the cash.")
    lines.append("")
    for row in nvda["dividends"]:
        lines.append(
            f"- {row['ex_date']}: Alpaca rate {row['raw_rate']:.4f}, credited rate {row['split_adjusted_rate']:.4f}"
        )
    tdg = example["tdg"]
    lines.extend(["", "TDG special dividend, ex-date 2025-09-02:"])
    if tdg["closes"]:
        lines.append("")
        for row in tdg["closes"]:
            lines.append(f"- {row['session']} close {row['close']:.2f}")
        lines.append("")
        lines.append(
            f"The close falls by about the ${tdg['cash_rate']:.2f} cash dividend, so that cash is not already inside the price. Crediting it once is the accounting, not a second copy."
        )
    lines.extend(["", "## Membership and FI", ""])
    lines.append(validation.get("fi", ""))
    lines.append("")
    lines.append(str(coverage.get("membership", "")))
    lines.append("")
    for label, row in coverage.get("windows", {}).items():
        lines.append(
            f"- {label} {row['eval_start']} → {row['eval_end']}: "
            f"{row['covered_member_days']}/{row['member_days']} ({100 * row['coverage']:.2f}%), "
            f"interior holes {row['interior_holes']}, "
            f"no usable bar {row['symbols_with_no_usable_member_bar']}."
        )
    lines.extend(["", "## Unapplied actions", ""])
    lines.append(
        "GE Vernova: GE's 2 April 2024 Form 8-K states the distribution at 12:10 a.m. Eastern Time that day, "
        "one GE Vernova share for every four GE shares held of record on 19 March 2024, with cash instead of fractional shares. "
        "The research tape's first GE Vernova bar is the 2 April regular-way session. "
        "GE's 1 April close is 175.36 and its 2 April open is 140.53. One quarter of GE Vernova's 142.85 open is 35.71. "
        "That gap is the distribution leaving GE's price, so the shares are credited on top of the ex-distribution GE bar and are not also a cash plug. "
        "Holders at the 1 April close are entitled. A GE purchase at the 2 April open is not. "
        "Whole shares are a cost-zero holding. The fractional remainder is cash at the 2 April GE Vernova open, less the book's 5 bps sale slippage, "
        "because the distribution agent sells fractions in the market and the 8-K does not publish the agent's net price. "
        "The holding does not have to pass the entry screen. It has no stop until GE Vernova's own 14-session ATR exists, and the 30-session hold starts on 2 April. "
        "Baseline and rotation use that same rule. The previously estimated $1,452 is not booked."
    )
    lines.append("")
    lines.append(
        "NVDA 10 June 2024: the stored daily high was 195.95. Alpaca SIP minute bars for that session, 960 minutes, "
        "have a maximum high of 123.10 and no minute at or above 150. Yahoo Finance's daily bar matches the stored open, low, and close "
        "and records the high as 123.10. The research daily high is replaced with 123.10. The open, low, and close are unchanged. "
        "Massive's daily bars were not available on this account. No split-day bar in this tape has a high more than 15 percent above its close."
    )
    lines.append("")
    lines.append(
        "PARA: Alpaca's split-adjusted ticker history does not match Paramount's traded OHLC. "
        "The research bars from 2024-04-24 through 2025-08-06 are the ChartExchange traded opens, highs, lows, and closes. "
        "Earlier PARA rows and rows after 2025-08-06 were removed. "
        "GL on 2024-04-11 matches the published 49.17 close and was left unchanged."
    )
    lines.append("")
    lines.append(
        "The RJF stock dividend of 1.3 on 13 April 2023 is before this calendar, and that close does not jump. "
        "Rights distributions in the Alpaca response: none. "
        "Honeywell's 1-for-2 reverse-split record falls on the Honeywell Aerospace spin-off session, and the close ratio that day is about one half, so Honeywell shares are not doubled."
    )
    lines.append("")
    materiality = validation.get("materiality") or {}
    for label, row in materiality.items():
        lines.append(
            f"- {label}: held-name events {row['hit_count']}, priced gap ${row['priced_total']:,.0f}, "
            f"blocked {row['blocked']}."
        )
        for hit in row.get("hits", []):
            missed = "unpriced" if hit.get("missed_dollars") is None else f"${hit['missed_dollars']:,.0f}"
            lines.append(f"  - {hit['session']} {hit['symbol']} {hit['kind']}: {missed}. {hit['detail']}")
    if validation.get("reason"):
        lines.extend(["", f"Rotation was not run. {validation['reason']}"])
    lines.extend(["", "## Corrected A", ""])
    for label, row in (validation.get("corrected_A") or {}).items():
        lines.append(
            f"- {label}: return {pct(row['net_return'])}, max drawdown {pct(row['max_drawdown'])}, "
            f"exposure {pct(row['avg_exposure'])}, turnover {row['turnover']:.2f}x, "
            f"costs ${row['cost_dollars']:,.0f}, replacements {row['replacements']}, "
            f"end equity ${row['ending_equity']:,.0f}."
        )
    if comparison:
        lines.extend(["", "## A versus rotation", ""])
        lines.append("The rotation rule is `ROTATION_SPEC.md`. It was not edited after these numbers.")
        lines.append("")
        lines.append("| Window | Arm | Return | Max DD | Exposure | Turnover | Costs | Replacements | End equity |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for label, row in comparison["arms"].items():
            for arm in ("A", "R"):
                item = row[arm]
                lines.append(
                    f"| {label} | {arm} | {pct(item['net_return'])} | {pct(item['max_drawdown'])} | "
                    f"{pct(item['avg_exposure'])} | {item['turnover']:.2f}x | ${item['cost_dollars']:,.0f} | "
                    f"{item['replacements']} | ${item['ending_equity']:,.0f} |"
                )
            gate = row["gates"]
            lines.append(
                f"| {label} | R−A | {100 * gate['return_minus_A']:+.2f} pp | "
                f"{100 * gate['max_drawdown_minus_A']:+.2f} pp |  |  |  |  |  |"
            )
        verdict = "PASS" if comparison["verdict"]["pass"] else "FAIL"
        lines.extend(["", f"Protocol verdict: **{verdict}**. {comparison['verdict']['rule']}"])
    text = "\n".join(lines) + "\n"
    (RESULTS / "baseline_validation.md").write_text(text, encoding="utf-8")
    (RESULTS / "baseline_validation.json").write_text(json.dumps(validation, indent=2, default=str), encoding="utf-8")
    if comparison:
        (RESULTS / "rotation_results.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
