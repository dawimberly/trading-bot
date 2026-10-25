"""Read-only access to the validated NYSE cash snapshot."""

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
from run_abc import dividend_map, member_on, membership_index  # noqa: E402
from run_rotation import GEV_DISTRIBUTION  # noqa: E402

DB = ABC / "data" / "bars.sqlite"
SPY_DIST = ABC / "data" / "spy_distributions.json"


def load_config() -> dict:
    return json.loads((HERE / "config.json").read_text(encoding="utf-8"))


def spy_calendar(conn: sqlite3.Connection, anchor: str) -> list[str]:
    rows = conn.execute(
        "SELECT session FROM bars WHERE symbol = 'SPY' AND session <= ? ORDER BY session",
        (anchor,),
    ).fetchall()
    return [r[0] for r in rows if date.fromisoformat(r[0]).weekday() < 5]


def schedule(calendar: list[str], cfg: dict) -> dict:
    warmup = int(cfg["warmup_sessions"])
    train = int(cfg["train_sessions"])
    step = int(cfg["eval_sessions"])
    folds_n = int(cfg["folds"])
    need = warmup + train + folds_n * step
    if len(calendar) < need:
        raise RuntimeError(f"calendar {len(calendar)} < {need}")
    span = calendar[-need:]
    eval_start_i = warmup + train
    folds = []
    for i in range(folds_n):
        ev = span[eval_start_i + i * step : eval_start_i + (i + 1) * step]
        origin = calendar.index(ev[0])
        scored = calendar[origin - train : origin]
        hist = calendar[origin - train - warmup : origin]
        if len(scored) != train or len(hist) != warmup + train or len(ev) != step:
            raise RuntimeError("fold length drifted")
        if scored[-1] >= ev[0] or hist[-1] != scored[-1]:
            raise RuntimeError("training overlaps its evaluation fold")
        if any(day >= ev[0] for day in hist):
            raise RuntimeError("training dates reach the evaluation fold")
        folds.append(
            {
                "fold": i + 1,
                "eval": ev,
                "train_scored": scored,
                "train_dates": hist,
                "cutoff": scored[-1],
            }
        )
    first_eval = folds[0]["eval"][0]
    last_eval = folds[-1]["eval"][-1]
    return {
        "span": span,
        "folds": folds,
        "eval_dates": [day for fold in folds for day in fold["eval"]],
        "eval_history": calendar[calendar.index(first_eval) - warmup : calendar.index(last_eval) + 1],
    }


def load_market() -> dict:
    cfg = load_config()
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    calendar = spy_calendar(conn, cfg["anchor_session"])
    bars = pd.read_sql("SELECT symbol, session, open, high, low, close FROM bars", conn)
    divs = pd.read_sql("SELECT symbol, ex_date, rate FROM cash_dividends", conn)
    splits = pd.read_sql("SELECT symbol, ex_date, kind, new_rate, old_rate FROM splits", conn)
    conn.close()
    lookup: dict[str, dict[str, Bar]] = {}
    for rec in bars.itertuples(index=False):
        lookup.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
    if not lookup.get("FI") and lookup.get("FISV"):
        lookup["FI"] = dict(lookup["FISV"])
    plan = schedule(calendar, cfg)
    membership = membership_index(research_membership())
    return {
        "config": cfg,
        "calendar": calendar,
        "plan": plan,
        "lookup": lookup,
        "membership": membership,
        "divs": divs,
        "splits": splits,
    }


def panel_for(dates: list[str], lookup: dict, membership: dict) -> tuple[dict, dict, list]:
    symbols = sorted(
        s for s in lookup if s != "SPY" and any(member_on(membership, s, day) for day in dates)
    )
    bars: dict[str, list] = {}
    member: dict[str, list] = {}
    for sym in symbols:
        series = lookup.get(sym, {})
        bars[sym] = []
        member[sym] = []
        for day in dates:
            raw = series.get(day)
            bars[sym].append(raw if usable(raw) else None)
            member[sym].append(member_on(membership, sym, day))
    spy = []
    series = lookup.get("SPY", {})
    for day in dates:
        raw = series.get(day)
        spy.append(raw.close if usable(raw) else None)
    return bars, member, spy


def actions_for(dates: list[str], divs: pd.DataFrame, splits: pd.DataFrame) -> dict:
    """Corporate-action inputs whose sessions are inside `dates` only."""
    mapped = dividend_map(divs, splits, dates)
    allowed = set(dates)
    dividends = {
        sym: {day: rate for day, rate in days.items() if day in allowed}
        for sym, days in mapped.items()
    }
    distributions = [GEV_DISTRIBUTION] if GEV_DISTRIBUTION.session in allowed else []
    renames = {"2025-11-11": [("FI", "FISV")]} if "2025-11-11" in allowed else {}
    return {"dividends": dividends, "distributions": distributions, "renames": renames}


def spy_dividend_map(dates: list[str]) -> dict[str, float]:
    payload = json.loads(SPY_DIST.read_text(encoding="utf-8"))
    allowed = set(dates)
    out: dict[str, float] = {}
    for row in payload["cash_dividends"]:
        ex = row["ex_date"]
        day = ex if ex in allowed else next((d for d in dates if d >= ex), None)
        if day is None:
            continue
        out[day] = out.get(day, 0.0) + float(row["rate"])
    return out


def assert_training_isolated(fold: dict) -> None:
    cutoff = fold["cutoff"]
    if fold["train_dates"][-1] != cutoff:
        raise RuntimeError("cutoff is not the training terminal session")
    if any(day > cutoff for day in fold["train_dates"]):
        raise RuntimeError("training consumes a session after the cutoff")
    if fold["eval"][0] <= cutoff:
        raise RuntimeError("evaluation starts on or before the cutoff")
