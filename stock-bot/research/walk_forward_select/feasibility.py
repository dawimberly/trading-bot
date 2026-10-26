"""Read-only coverage audit for the walk-forward selection experiment.

Does not write the NYSE cash research database or its result files.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
ROOT = HERE.parents[1]
sys.path.insert(0, str(ABC))
sys.path.insert(0, str(ROOT))

from engine import Bar, usable  # noqa: E402
from membership import research_membership  # noqa: E402
from run_abc import member_on, membership_index  # noqa: E402

DB = ABC / "data" / "bars.sqlite"
WARMUP = 100 + 15  # longest MA lookback plus the engine's 15-session buffer
TRAIN = 504
EVAL = 63
FOLDS = 4
NEED = WARMUP + TRAIN + FOLDS * EVAL


def calendar_from_spy(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT session FROM bars WHERE symbol = 'SPY' AND session <= '2026-10-14' ORDER BY session"
    ).fetchall()
    return [r[0] for r in rows if date.fromisoformat(r[0]).weekday() < 5]


def cover(dates: list[str], lookup: dict, membership: dict) -> dict:
    start = pd.Timestamp(dates[0])
    end = pd.Timestamp(dates[-1])
    symbols = []
    for ticker, spans in membership.items():
        if ticker == "SPY":
            continue
        if any(s <= end and (pd.isna(e) or pd.Timestamp(e) > start) for s, e in spans):
            symbols.append(ticker)
    member_days = 0
    covered = 0
    missing_symbols = []
    for sym in symbols:
        series = lookup.get(sym, {})
        any_bar = False
        for day in dates:
            if not member_on(membership, sym, day):
                continue
            member_days += 1
            ok = usable(series.get(day))
            any_bar = any_bar or ok
            covered += int(ok)
        if not any_bar:
            missing_symbols.append(sym)
    return {
        "start": dates[0],
        "end": dates[-1],
        "sessions": len(dates),
        "members": len(symbols),
        "member_days": member_days,
        "covered_member_days": covered,
        "coverage": covered / member_days if member_days else 0.0,
        "symbols_with_no_usable_member_bar": missing_symbols,
    }


def main() -> int:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    calendar = calendar_from_spy(conn)
    bars = pd.read_sql("SELECT symbol, session, open, high, low, close FROM bars", conn)
    conn.close()
    lookup: dict[str, dict] = {}
    for rec in bars.itertuples(index=False):
        lookup.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
    if not lookup.get("FI") and lookup.get("FISV"):
        lookup["FI"] = dict(lookup["FISV"])
    membership = membership_index(research_membership())
    out = {
        "spy_sessions": len(calendar),
        "spy_start": calendar[0],
        "spy_end": calendar[-1],
        "required_sessions": NEED,
        "warmup_sessions": WARMUP,
        "train_sessions": TRAIN,
        "eval_sessions": EVAL,
        "folds": FOLDS,
        "surplus_sessions": len(calendar) - NEED,
    }
    if len(calendar) < NEED:
        out["status"] = "BLOCKED"
        out["missing"] = {
            "sessions": NEED - len(calendar),
            "requirement": "warmup 115 + training 504 + 4 non-overlapping evaluation folds of 63",
        }
    else:
        span = calendar[-NEED:]
        train0 = span[WARMUP : WARMUP + TRAIN]
        evals = [span[WARMUP + TRAIN + i * EVAL : WARMUP + TRAIN + (i + 1) * EVAL] for i in range(FOLDS)]
        unused = calendar[: len(calendar) - NEED]
        windows = {"first_training": cover(train0, lookup, membership)}
        for i, dates in enumerate(evals, start=1):
            windows[f"eval_{i}"] = cover(dates, lookup, membership)
            train = calendar[calendar.index(dates[0]) - TRAIN : calendar.index(dates[0])]
            windows[f"train_{i}"] = cover(train, lookup, membership)
        spy_missing = [d for d in span if not usable(lookup.get("SPY", {}).get(d))]
        out.update(
            {
                "status": "ENOUGH_HISTORY",
                "unused_prefix_sessions": len(unused),
                "unused_prefix_end": unused[-1] if unused else None,
                "experiment_start": span[0],
                "first_training_start": train0[0],
                "evaluation_start": evals[0][0],
                "evaluation_end": evals[-1][-1],
                "folds_detail": [
                    {"fold": i + 1, "start": dates[0], "end": dates[-1], "sessions": len(dates)}
                    for i, dates in enumerate(evals)
                ],
                "spy_missing_on_required_span": spy_missing,
                "windows": windows,
                "coverage_gate": 0.95,
            }
        )
        low = {
            name: row["coverage"]
            for name, row in windows.items()
            if row["coverage"] < 0.95
        }
        if spy_missing or low:
            out["status"] = "BLOCKED"
            out["missing"] = {"spy_sessions": spy_missing, "windows_below_0.95": low}
    dest = HERE / "results"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "feasibility.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(out["status"], out.get("spy_sessions"), out.get("required_sessions"))
    if "windows" in out:
        for name, row in out["windows"].items():
            print(f"{name} {row['start']} {row['end']} coverage={row['coverage']:.6f} missing_symbols={len(row['symbols_with_no_usable_member_bar'])}")
    return 0 if out["status"] == "ENOUGH_HISTORY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
