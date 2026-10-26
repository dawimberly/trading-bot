"""Post-sale prices on the exchange calendar. Not a verdict that the sale was right."""
from __future__ import annotations

import json

from .calendar import bar_starts, close_verified, sessions_after
from .engine import timestamp


def build(connection, state: dict) -> list[dict]:
    frames = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM frames ORDER BY rowid")]
    by_day: dict[str, dict] = {}
    for frame in frames:
        by_day.setdefault(timestamp(frame["timestamp"]).date().isoformat(), []).append(frame)
    last_day = state["last"][:10] if state.get("last") else ""
    output = []
    for book, account in state["books"].items():
        for trade in account["closed"]:
            future = sessions_after(trade["exit_session"], 30)
            paths = []
            for day in future:
                if day > last_day:
                    paths.append({"session": day, "status": "not_mature"})
                    continue
                if not close_verified(day):
                    paths.append({"session": day, "status": "close_unverified"})
                    continue
                day_frames = by_day.get(day) or []
                if day not in state["completed_sessions"]:
                    paths.append({"session": day, "status": "incomplete_session"})
                    continue
                closing = bar_starts(day)[-1]
                last = next(
                    (
                        frame
                        for frame in day_frames
                        if timestamp(frame["timestamp"]).strftime("%H:%M") == closing
                    ),
                    None,
                )
                if last is None:
                    paths.append({"session": day, "status": "incomplete_session"})
                    continue
                bar = (last.get("bars") or {}).get(trade["symbol"])
                if not bar:
                    paths.append({"session": day, "status": "missing_symbol_price"})
                    continue
                ratio = 1.0
                for other in frames:
                    other_time = timestamp(other["timestamp"])
                    if timestamp(trade["exit_time"]) < other_time <= timestamp(last["timestamp"]):
                        for event in (other.get("action_audit") or {}).get("events") or []:
                            if event.get("symbol") == trade["symbol"] and event.get("type") in {"split", "stock_dividend"}:
                                ratio *= float(event["ratio"])
                sale_open = trade.get("exit_open")
                if not sale_open:
                    paths.append({"session": day, "status": "missing_exit_open"})
                    continue
                paths.append(
                    {
                        "session": day,
                        "status": "priced",
                        "price_return": bar["close"] * ratio / sale_open - 1,
                    }
                )
            horizons = {}
            for horizon in (1, 5, 10, 20, 30):
                if len(future) < horizon:
                    horizons[str(horizon)] = {"status": "calendar_ended"}
                else:
                    horizons[str(horizon)] = paths[horizon - 1]
            output.append(
                {
                    "book": book,
                    "symbol": trade["symbol"],
                    "exit_time": trade["exit_time"],
                    "plan_id": trade["plan_id"],
                    "observed_complete_sessions": sum(1 for day in future if day in state["completed_sessions"]),
                    "horizons": horizons,
                    "label": (
                        "PRICE ONLY on the verified exchange calendar. Dividends excluded. "
                        "Incomplete and missing paths are labeled. This is not a verdict on the sale."
                    ),
                }
            )
    return output
