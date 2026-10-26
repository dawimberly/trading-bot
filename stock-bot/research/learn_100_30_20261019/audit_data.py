"""Read-only audit of the frozen daily cash tape. Writes only this directory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
ROOT = HERE.parents[1]
sys.path.insert(0, str(ABC))
sys.path.insert(0, str(ROOT))

from engine import Bar, usable  # noqa: E402
from membership import research_membership  # noqa: E402
from run_abc import member_on, membership_index  # noqa: E402

DB = ABC / "data" / "bars.sqlite"
MANIFEST = ABC / "data" / "manifest.json"
UNAPPLIED = ABC / "data" / "unapplied_actions.json"
SPY_DIST = ABC / "data" / "spy_distributions.json"
INTRADAY = ROOT / "data" / "intraday_cache"
SIM_V2 = ROOT / "research" / "strategy_lab" / "lab_data" / "snapshots" / "sim_2021_2026_v2" / "limitations.json"
CUTOFF = "2026-10-14"
WARMUP = 115
TRAIN = 100
EVAL = 30


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def weekday(day: str) -> bool:
    return date.fromisoformat(day).weekday() < 5


def main() -> int:
    OUT = HERE / "data_audit.json"
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    symbols = [row[0] for row in conn.execute("SELECT DISTINCT symbol FROM bars ORDER BY symbol")]
    spy_rows = conn.execute(
        "SELECT session, open, high, low, close FROM bars WHERE symbol = 'SPY' AND session <= ? ORDER BY session",
        (CUTOFF,),
    ).fetchall()
    calendar = []
    spy_bad = []
    for row in spy_rows:
        if not weekday(row["session"]):
            continue
        bar = Bar(row["open"], row["high"], row["low"], row["close"])
        if usable(bar):
            calendar.append(row["session"])
        else:
            spy_bad.append(row["session"])
    gaps = []
    for prev, cur in zip(calendar, calendar[1:]):
        delta = (date.fromisoformat(cur) - date.fromisoformat(prev)).days
        if delta > 4:
            gaps.append({"from": prev, "to": cur, "calendar_days": delta})
    invalid = conn.execute(
        """
        SELECT COUNT(*) FROM bars
        WHERE session <= ?
          AND NOT (
            open > 0 AND high > 0 AND low > 0 AND close > 0
            AND high + 1e-8 >= open AND high + 1e-8 >= close AND high + 1e-8 >= low
            AND low - 1e-8 <= open AND low - 1e-8 <= close AND low - 1e-8 <= high
          )
        """,
        (CUTOFF,),
    ).fetchone()[0]
    dupes = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT symbol, session FROM bars WHERE session <= ?
          GROUP BY symbol, session HAVING COUNT(*) > 1
        )
        """,
        (CUTOFF,),
    ).fetchone()[0]
    bar_rows = conn.execute(
        "SELECT COUNT(*) FROM bars WHERE session <= ?",
        (CUTOFF,),
    ).fetchone()[0]
    div_rows = conn.execute("SELECT COUNT(*) FROM cash_dividends").fetchone()[0]
    split_rows = conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0]
    vti_bar = conn.execute(
        "SELECT COUNT(*), MIN(session), MAX(session) FROM bars WHERE symbol = 'VTI' AND session <= ?",
        (CUTOFF,),
    ).fetchone()
    vti_div = conn.execute("SELECT COUNT(*) FROM cash_dividends WHERE symbol = 'VTI'").fetchone()[0]
    para = conn.execute(
        "SELECT COUNT(*), MIN(session), MAX(session) FROM bars WHERE symbol = 'PARA'"
    ).fetchone()
    nulls = conn.execute(
        "SELECT COUNT(*) FROM bars WHERE session <= ? AND (open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL)",
        (CUTOFF,),
    ).fetchone()[0]
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    membership = membership_index(research_membership())
    # Coverage on the full SPY calendar. Missing stays missing.
    member_days = 0
    covered = 0
    interior = 0
    no_bar_symbols = []
    tape_ends_while_member = []
    cursor = conn.cursor()
    for sym in symbols:
        if sym == "SPY":
            continue
        series = {
            row["session"]: Bar(row["open"], row["high"], row["low"], row["close"])
            for row in cursor.execute(
                "SELECT session, open, high, low, close FROM bars WHERE symbol = ? AND session <= ? ORDER BY session",
                (sym, CUTOFF),
            )
        }
        flags = []
        local_member = 0
        local_covered = 0
        last_member = None
        for day in calendar:
            if not member_on(membership, sym, day):
                flags.append(None)
                continue
            last_member = day
            local_member += 1
            ok = usable(series.get(day))
            flags.append(ok)
            local_covered += int(ok)
        if local_member == 0:
            continue
        member_days += local_member
        covered += local_covered
        if not any(flag is True for flag in flags):
            no_bar_symbols.append(sym)
        for i, flag in enumerate(flags):
            if flag is not False:
                continue
            prev = next((flags[j] for j in range(i - 1, -1, -1) if flags[j] is not None), None)
            nxt = next((flags[j] for j in range(i + 1, len(flags)) if flags[j] is not None), None)
            if prev is True and nxt is True:
                interior += 1
        usable_days = [day for day, bar in series.items() if usable(bar)]
        if last_member and usable_days and max(usable_days) < last_member:
            tape_ends_while_member.append(
                {"symbol": sym, "last_usable": max(usable_days), "last_member_session": last_member}
            )
    conn.close()

    need = WARMUP + TRAIN
    eval_room = max(0, len(calendar) - need)
    folds_n = eval_room // EVAL
    eval_sessions = folds_n * EVAL
    plan = None
    if folds_n >= 1:
        span_start = len(calendar) - (need + eval_sessions)
        folds = []
        for i in range(folds_n):
            ev0 = span_start + need + i * EVAL
            ev = calendar[ev0 : ev0 + EVAL]
            scored = calendar[ev0 - TRAIN : ev0]
            hist = calendar[ev0 - TRAIN - WARMUP : ev0]
            folds.append(
                {
                    "fold": i + 1,
                    "train_start": scored[0],
                    "train_end": scored[-1],
                    "eval_start": ev[0],
                    "eval_end": ev[-1],
                    "warmup_start": hist[0],
                }
            )
        plan = {
            "warmup_sessions": WARMUP,
            "train_sessions": TRAIN,
            "eval_sessions": EVAL,
            "folds": folds_n,
            "evaluation_sessions": eval_sessions,
            "calendar_sessions_used": need + eval_sessions,
            "calendar_sessions_unused_prefix": span_start,
            "first_warmup": folds[0]["warmup_start"],
            "first_train": folds[0]["train_start"],
            "first_eval": folds[0]["eval_start"],
            "last_eval": folds[-1]["eval_end"],
            "fold_bounds": folds,
        }

    actions = json.loads(UNAPPLIED.read_text(encoding="utf-8"))
    kinds = Counter(row.get("kind") or row.get("corporate_action_type") for row in actions.get("rows", []))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    spy_dist = json.loads(SPY_DIST.read_text(encoding="utf-8"))
    v2 = json.loads(SIM_V2.read_text(encoding="utf-8"))

    intraday = {"present": INTRADAY.is_dir(), "pkl_files": 0, "meta_files": 0, "notes": []}
    if INTRADAY.is_dir():
        pkls = list(INTRADAY.glob("*.pkl"))
        metas = list(INTRADAY.glob("*.meta.json"))
        intraday["pkl_files"] = len(pkls)
        intraday["meta_files"] = len(metas)
        starts = []
        ends = []
        for path in metas[:40]:
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for key in ("start", "first", "min_timestamp", "from"):
                if key in meta:
                    starts.append(str(meta[key]))
                    break
            for key in ("end", "last", "max_timestamp", "to"):
                if key in meta:
                    ends.append(str(meta[key]))
                    break
        intraday["meta_start_sample"] = sorted(starts)[:3]
        intraday["meta_end_sample"] = sorted(ends)[-3:]
        intraday["notes"].append(
            "Saved intraday cache is a limited configured universe, not a historical S&P membership tape. It is not combined with the daily study."
        )

    started = time.perf_counter()
    # Timing only. The return is discarded and is not a study result.
    from run_abc import dividend_map  # noqa: E402
    import pandas as pd

    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    bars = pd.read_sql("SELECT symbol, session, open, high, low, close FROM bars WHERE session <= ?", conn, params=(CUTOFF,))
    divs = pd.read_sql("SELECT symbol, ex_date, rate FROM cash_dividends", conn)
    splits = pd.read_sql("SELECT symbol, ex_date, kind, new_rate, old_rate FROM splits", conn)
    conn.close()
    lookup: dict[str, dict[str, Bar]] = {}
    for rec in bars.itertuples(index=False):
        lookup.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
    if not lookup.get("FI") and lookup.get("FISV"):
        lookup["FI"] = dict(lookup["FISV"])
    load_s = time.perf_counter() - started
    bench_s = None
    if plan is not None:
        from engine import simulate

        fold = plan["fold_bounds"][0]
        dates = calendar[calendar.index(fold["warmup_start"]) : calendar.index(fold["train_end"]) + 1]
        symbols_m = sorted(
            s for s in lookup if s != "SPY" and any(member_on(membership, s, day) for day in dates)
        )
        panel = {}
        member = {}
        for sym in symbols_m:
            series = lookup.get(sym, {})
            panel[sym] = [series.get(day) if usable(series.get(day)) else None for day in dates]
            member[sym] = [member_on(membership, sym, day) for day in dates]
        spy = []
        for day in dates:
            raw = lookup.get("SPY", {}).get(day)
            spy.append(raw.close if usable(raw) else None)
        mapped = dividend_map(divs, splits, dates)
        t0 = time.perf_counter()
        simulate(
            panel,
            member,
            dates,
            spy,
            arm="A",
            decision_start=WARMUP,
            dividends=mapped,
            ma_win=70,
            max_hold=30,
            atr_mult=2.0,
        )
        bench_s = time.perf_counter() - t0

    payload = {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(DB),
            "bytes": DB.stat().st_size,
            "sha256": sha256(DB),
            "tables": tables,
            "adjustment": manifest.get("adjustment"),
            "feed": manifest.get("feed"),
            "dividend_policy": manifest.get("dividend_policy"),
            "manifest_calendar_sessions": manifest.get("calendar_sessions"),
            "manifest_calendar_start": manifest.get("calendar_start"),
            "manifest_calendar_end": manifest.get("calendar_end"),
            "session_cutoff": CUTOFF,
            "prices_are_dividend_adjusted": False,
        },
        "spy_calendar": {
            "usable_weekday_sessions": len(calendar),
            "start": calendar[0] if calendar else None,
            "end": calendar[-1] if calendar else None,
            "invalid_weekday_bars": spy_bad,
            "gaps_longer_than_4_calendar_days": gaps,
        },
        "ohlc": {
            "bar_rows_through_cutoff": bar_rows,
            "invalid_ohlc_rows": invalid,
            "null_ohlc_rows": nulls,
            "duplicate_symbol_sessions": dupes,
            "symbols": len(symbols),
        },
        "membership_coverage": {
            "member_sessions": member_days,
            "covered_member_sessions": covered,
            "coverage": (covered / member_days) if member_days else None,
            "interior_holes": interior,
            "symbols_with_membership_and_no_usable_bar": no_bar_symbols,
            "tape_ends_while_still_a_member": tape_ends_while_member[:40],
            "tape_end_count": len(tape_ends_while_member),
        },
        "corporate_actions_stored": {
            "cash_dividend_rows": div_rows,
            "split_rows": split_rows,
            "unapplied_action_kinds": dict(kinds),
            "spy_cash_dividend_rows": len(spy_dist.get("cash_dividends", [])),
            "applied_by_frozen_engine": [
                "GE to GEV 0.25 distribution on 2024-04-02",
                "FI renamed to FISV on 2025-11-11 using the FISV tape",
            ],
            "note": "Other mergers, spins, and stock dividends stay in unapplied_actions.json. This audit does not invent values for them. A later simulation that holds across one remains a visible defect.",
        },
        "para": {
            "rows": para[0],
            "start": para[1],
            "end": para[2],
            "note": "PARA unusability blocks sim_2021_2026_v2. It is not a reason to switch this study onto that bundle.",
        },
        "vti": {
            "bar_rows": vti_bar[0],
            "start": vti_bar[1],
            "end": vti_bar[2],
            "cash_dividend_rows": vti_div,
            "usable_for_total_return": bool(vti_bar[0]) and vti_div > 0 and vti_bar[1] <= calendar[0] and vti_bar[2] >= calendar[-1],
        },
        "sim_2021_2026_v2": {
            "search_blocked": v2.get("search_blocked"),
            "reasons": v2.get("search_block_reasons") or v2,
            "used": False,
        },
        "intraday_cache": intraday,
        "duration": {
            "requested_target_sessions": 1000,
            "usable_spy_sessions": len(calendar),
            "sessions_reserved_for_first_warmup_and_training": need,
            "maximum_nonoverlapping_30_session_evals": folds_n,
            "evaluation_sessions": eval_sessions,
            "is_1000_session_study": False,
            "plan": plan,
        },
        "timing": {
            "market_load_seconds": round(load_s, 3),
            "one_baseline_training_simulate_seconds": None if bench_s is None else round(bench_s, 3),
            "return_discarded": True,
        },
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "sessions": len(calendar),
                "start": calendar[0] if calendar else None,
                "end": calendar[-1] if calendar else None,
                "coverage": payload["membership_coverage"]["coverage"],
                "interior_holes": interior,
                "invalid_ohlc": invalid,
                "eval_sessions": eval_sessions,
                "folds": folds_n,
                "vti_bars": vti_bar[0],
                "vti_divs": vti_div,
                "bench_s": bench_s,
                "load_s": load_s,
                "tape_ends": len(tape_ends_while_member),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
