"""Read-only load of the frozen NYSE cash snapshot. No broker client."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
WFS = HERE.parent / "walk_forward_select"
ROOT = HERE.parents[1]
sys.path.insert(0, str(ABC))
sys.path.insert(0, str(WFS))
sys.path.insert(0, str(ROOT))

from data_access import actions_for, panel_for, spy_dividend_map  # noqa: E402
from engine import Bar, usable  # noqa: E402
from membership import research_membership  # noqa: E402
from plan import CUTOFF, build_plan  # noqa: E402
from run_abc import membership_index  # noqa: E402
from run_rotation import alias_fi, deduped_actions  # noqa: E402

DB = ABC / "data" / "bars.sqlite"
UNAPPLIED = ABC / "data" / "unapplied_actions.json"


def spy_calendar(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT session, open, high, low, close FROM bars WHERE symbol = 'SPY' AND session <= ? ORDER BY session",
        (CUTOFF,),
    )
    calendar = []
    for session, open_, high, low, close in rows:
        if date.fromisoformat(session).weekday() >= 5:
            continue
        if usable(Bar(open_, high, low, close)):
            calendar.append(session)
    return calendar


def load_market() -> dict:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    calendar = spy_calendar(conn)
    bars = pd.read_sql(
        "SELECT symbol, session, open, high, low, close FROM bars WHERE session <= ?",
        conn,
        params=(CUTOFF,),
    )
    divs = pd.read_sql("SELECT symbol, ex_date, rate FROM cash_dividends", conn)
    splits = pd.read_sql("SELECT symbol, ex_date, kind, new_rate, old_rate FROM splits", conn)
    conn.close()
    lookup: dict[str, dict[str, Bar]] = {}
    for rec in bars.itertuples(index=False):
        lookup.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
    alias_fi(lookup)
    del bars
    membership = membership_index(research_membership())
    plan = build_plan(calendar)
    actions = json_actions()
    return {
        "calendar": calendar,
        "plan": plan,
        "lookup": lookup,
        "membership": membership,
        "divs": divs,
        "splits": splits,
        "actions": actions,
        "db": str(DB),
    }


def json_actions() -> list[dict]:
    import json

    payload = json.loads(UNAPPLIED.read_text(encoding="utf-8"))
    return deduped_actions(payload["rows"])


def slice_market(market: dict, dates: list[str]) -> tuple[dict, dict, list, dict]:
    bars, member, spy = panel_for(dates, market["lookup"], market["membership"])
    actions = actions_for(dates, market["divs"], market["splits"])
    return bars, member, spy, actions


def spy_bars(market: dict, dates: list[str]) -> dict:
    series = market["lookup"].get("SPY", {})
    out = {}
    for day in dates:
        raw = series.get(day)
        if not usable(raw):
            raise RuntimeError(f"SPY bar missing on {day}")
        out[day] = raw
    return out


def spy_dividends(dates: list[str]) -> dict[str, float]:
    return spy_dividend_map(dates)


def assert_member_isolated(dates: list[str], cutoff: str) -> None:
    if dates[-1] != cutoff or any(day > cutoff for day in dates):
        raise RuntimeError("training dates pass the cutoff")
