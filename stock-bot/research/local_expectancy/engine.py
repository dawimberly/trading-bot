"""Daily replay of the frozen local_expectancy book.

One knob versus fixed ma50-extension rank. Fills are next-session opens.
A quote older than seven minutes is refused. NAV starts at 1.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from features import entry_quote_is_fresh, features
from learner import learn, predict

ET = ZoneInfo("America/New_York")

SPEC = {
    "max_names": 10,
    "name_cap": 0.10,
    "initial_clip": 0.095,
    "sleeve": 0.95,
    "hard_stop": 0.05,
    "arm": 0.05,
    "trail": 0.02,
    "max_hold_sessions": 30,
    "reentry_minutes": 30,
    "max_daily_loss": 0.02,
    "max_book_drawdown": 0.15,
    "training_sessions": 100,
    "freeze_sessions": 30,
    "initial_nav": 1.0,
}

# Recovering halt only. Sticky mode ignores this. Not scanned on the market panel.
HALT_COOLDOWN_SESSIONS = 21


def session_open(day: str) -> datetime:
    year, month, dom = (int(part) for part in day.split("-"))
    return datetime(year, month, dom, 9, 30, tzinfo=ET)


def session_close(day: str) -> datetime:
    year, month, dom = (int(part) for part in day.split("-"))
    return datetime(year, month, dom, 16, 0, tzinfo=ET)


def _new_book() -> dict:
    nav = SPEC["initial_nav"]
    return {
        "cash": nav,
        "positions": {},
        "pending": [],
        "closed": [],
        "peak": nav,
        "anchor": nav,
        "entry_halt": False,
        "halt_index": None,
        "halt_triggers": 0,
        "halt_trigger_days": [],
        "rearms": 0,
        "rearm_days": [],
        "cooldown": {},
    }


def _arm_halt(book: dict, index: int, day: str) -> None:
    """Start the entry halt once. A later breach does not extend the clock."""
    if book["entry_halt"]:
        return
    book["entry_halt"] = True
    book["halt_index"] = index
    book["halt_triggers"] += 1
    book["halt_trigger_days"].append(day)


def _maybe_rearm(book: dict, index: int, day: str, equity: float) -> None:
    """After 21 sessions, allow entries and measure the next 15% from this equity.

    A flat cash book never climbs back to the old peak, so a drawdown-recovery
    rule would stay halted. The peak reset is what makes the cooldown a re-arm.
    """
    if not book["entry_halt"] or book["halt_index"] is None:
        return
    if index - book["halt_index"] < HALT_COOLDOWN_SESSIONS:
        return
    book["entry_halt"] = False
    book["halt_index"] = None
    book["peak"] = equity
    book["rearms"] += 1
    book["rearm_days"].append(day)


def _equity(book: dict, price_of) -> float:
    gross = 0.0
    for symbol, position in book["positions"].items():
        gross += position["qty"] * price_of(symbol)
    return book["cash"] + gross


def _sell(book: dict, symbol: str, raw: float, slip: float, day: str, index: int, reason: str, when: datetime) -> None:
    position = book["positions"].pop(symbol)
    fill = raw * (1 - slip)
    proceeds = position["qty"] * fill
    book["cash"] += proceeds
    book["closed"].append(
        {
            "symbol": symbol,
            "entry_session": position["entry_session"],
            "entry_index": position["entry_index"],
            "entry_time": position["entry_time"],
            "exit_session": day,
            "exit_index": index,
            "exit_time": when.isoformat(),
            "x": position["x"],
            "y": proceeds / position["cost"] - 1,
            "pnl": proceeds - position["cost"],
            "reason": reason,
            "weekday": position["weekday"],
            "entry_fill": position["entry"],
        }
    )
    book["cooldown"][symbol] = when


def _eligible(values: list[float]) -> bool:
    return values[0] > 0 and values[2] < 0.70


def _score(name: str, model: dict | None, values: list[float]) -> float:
    if name == "learner" and model is not None:
        return float(predict(model, values))
    return values[0]


def _size(book: dict, equity: float) -> float:
    gross = equity - book["cash"]
    dollars = min(
        book["cash"],
        equity * SPEC["initial_clip"],
        max(0.0, equity * SPEC["sleeve"] - gross),
        equity * SPEC["name_cap"],
    )
    return dollars if dollars > 1e-8 else 0.0


def path_stats(nav: list[float]) -> dict:
    if len(nav) < 2:
        return {
            "return": 0.0,
            "max_drawdown": 0.0,
            "first_half": 0.0,
            "second_half": 0.0,
            "daily_returns": [],
            "sharpe": None,
            "sessions": 0,
        }
    daily = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]
    peak = nav[0]
    max_dd = 0.0
    for value in nav:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    mid = len(daily) // 2
    mean = sum(daily) / len(daily)
    var = sum((item - mean) ** 2 for item in daily) / max(1, len(daily) - 1)
    sharpe = None if var <= 0 else mean / (var ** 0.5) * (252 ** 0.5)
    return {
        "return": nav[-1] / nav[0] - 1,
        "max_drawdown": max_dd,
        "first_half": nav[mid] / nav[0] - 1,
        "second_half": nav[-1] / nav[mid] - 1,
        "daily_returns": daily,
        "sharpe": sharpe,
        "sessions": len(daily),
    }


def hold_nav(
    sessions: list[str],
    bars: dict[str, dict[str, tuple[float, float]]],
    symbols: list[str],
    start: int,
    end: int,
    slip: float,
) -> list[float]:
    """Buy equal weight at the first open and liquidate at the last close."""
    first = sessions[start]
    last = sessions[end]
    names = [
        symbol
        for symbol in symbols
        if first in bars[symbol] and last in bars[symbol] and all(sessions[i] in bars[symbol] for i in range(start, end + 1))
    ]
    if not names:
        return [1.0, 1.0]
    slip_in = 1 + slip
    nav = [1.0]
    for index in range(start, end + 1):
        day = sessions[index]
        gross = 0.0
        for symbol in names:
            opened = bars[symbol][first][0] * slip_in
            gross += bars[symbol][day][1] / opened
        marked = gross / len(names)
        if index == end:
            marked *= 1 - slip
        nav.append(marked)
    return nav


def simulate(
    sessions: list[str],
    bars: dict[str, dict[str, tuple[float, float]]],
    symbols: list[str],
    *,
    window_start: str,
    window_end: str,
    slip_bps: float,
    quote_age_seconds: float = 0.0,
    learner_skip_weekdays: set[int] | None = None,
    momentum_at=None,
    halt_mode: str = "sticky",
) -> dict:
    """Replay both books. Sessions after window_end are not in the list the caller passes."""
    if window_start not in sessions or window_end not in sessions:
        raise ValueError("window bounds are not sessions")
    start = sessions.index(window_start)
    end = sessions.index(window_end)
    if start > end:
        raise ValueError("empty window")
    if halt_mode not in ("sticky", "cooldown_21"):
        raise ValueError("halt_mode must be sticky or cooldown_21")
    if sessions[end] != window_end or end != len(sessions) - 1:
        sessions = sessions[: end + 1]
        end = len(sessions) - 1
    slip = slip_bps / 10000
    fresh = entry_quote_is_fresh(quote_age_seconds)
    books = {"baseline": _new_book(), "learner": _new_book()}
    history: dict[str, list[float]] = {symbol: [] for symbol in symbols}
    feat: dict[str, list[float] | None] = {symbol: None for symbol in symbols}
    model = None
    last_train_index = None
    accepted_refits = 0
    rejected_refits = 0
    nav = {"baseline": [], "learner": []}
    buys = {"baseline": 0.0, "learner": 0.0}
    in_window = False

    def open_price(day: str, symbol: str) -> float | None:
        bar = bars[symbol].get(day)
        return None if bar is None else bar[0]

    def close_price(day: str, symbol: str) -> float | None:
        bar = bars[symbol].get(day)
        return None if bar is None else bar[1]

    def liquidate(day: str, index: int, field: str, reason: str, when: datetime) -> None:
        getter = open_price if field == "open" else close_price
        for book in books.values():
            for symbol in list(book["positions"]):
                raw = getter(day, symbol)
                if raw is None:
                    book["positions"].pop(symbol, None)
                    continue
                _sell(book, symbol, raw, slip, day, index, reason, when)

    for index, day in enumerate(sessions):
        opened = session_open(day)
        closed_at = session_close(day)

        if day == window_start:
            if index > 0:
                liquidate(sessions[index - 1], index - 1, "close", "window_boundary", session_close(sessions[index - 1]))
            pending = {}
            for name, book in books.items():
                pending[name] = [
                    order
                    for order in book["pending"]
                    if order["side"] == "buy" and order["decision_index"] == index - 1
                ]
            for name, book in books.items():
                closed = book["closed"]
                book.clear()
                book.update(_new_book())
                book["closed"] = closed
                book["pending"] = pending[name]
            in_window = True
            buys = {"baseline": 0.0, "learner": 0.0}
            nav = {"baseline": [SPEC["initial_nav"]], "learner": [SPEC["initial_nav"]]}

        if momentum_at is None and index > 0:
            train_lo = max(0, index - SPEC["training_sessions"])
            completed = set(sessions[train_lo:index])
            due = last_train_index is None or index - last_train_index >= SPEC["freeze_sessions"]
            if len(sessions[:index]) >= SPEC["training_sessions"] and due:
                rows = [
                    trade
                    for trade in books["baseline"]["closed"]
                    if trade["exit_session"] in completed
                    and datetime.fromisoformat(trade["exit_time"]) < opened
                ]
                fitted, info = learn(rows)
                last_train_index = index
                if info["status"] == "accepted_prediction_model":
                    accepted_refits += 1
                    model = fitted
                else:
                    if info["status"] == "rejected_prediction_model":
                        rejected_refits += 1
                    model = None

        for name, book in books.items():
            prior_prices = {}
            for symbol in book["positions"]:
                raw = close_price(sessions[index - 1], symbol) if index else None
                if raw is None:
                    raw = book["positions"][symbol]["entry"]
                prior_prices[symbol] = raw
            book["anchor"] = _equity(book, lambda symbol: prior_prices[symbol]) if book["positions"] else book["cash"]

            waiting = book["pending"]
            book["pending"] = []
            for order in waiting:
                symbol = order["symbol"]
                raw = open_price(day, symbol)
                if order["side"] == "sell":
                    if symbol not in book["positions"]:
                        continue
                    if raw is None:
                        book["pending"].append(order)
                        continue
                    _sell(book, symbol, raw, slip, day, index, order["reason"], opened)
                    continue
                if index != order["decision_index"] + 1 or not fresh:
                    continue
                if name == "learner" and learner_skip_weekdays and opened.weekday() in learner_skip_weekdays:
                    continue
                if raw is None or symbol in book["positions"] or book["entry_halt"]:
                    continue
                cooled = book["cooldown"].get(symbol)
                if cooled is not None and opened < cooled + timedelta(minutes=SPEC["reentry_minutes"]):
                    continue
                equity = _equity(book, lambda sym, raw_open=raw, held=symbol: open_price(day, sym) or book["positions"][sym]["entry"])
                if equity <= book["peak"] * (1 - SPEC["max_book_drawdown"]):
                    _arm_halt(book, index, day)
                    continue
                if equity <= book["anchor"] * (1 - SPEC["max_daily_loss"]):
                    continue
                if len(book["positions"]) >= SPEC["max_names"]:
                    continue
                dollars = _size(book, equity)
                if dollars <= 0:
                    continue
                fill = raw * (1 + slip)
                book["cash"] -= dollars
                if in_window:
                    buys[name] += dollars
                book["positions"][symbol] = {
                    "qty": dollars / fill,
                    "cost": dollars,
                    "entry": fill,
                    "peak": fill,
                    "armed": False,
                    "entry_session": day,
                    "entry_index": index,
                    "entry_time": opened.isoformat(),
                    "x": order["x"],
                    "weekday": opened.weekday(),
                }
            book["peak"] = max(book["peak"], book["anchor"])

        for symbol in symbols:
            raw = close_price(day, symbol)
            if raw is None:
                history[symbol] = []
                feat[symbol] = None
                continue
            history[symbol].append(raw)
            if len(history[symbol]) > 51:
                history[symbol] = history[symbol][-51:]
            feat[symbol] = features(history[symbol])

        for name, book in books.items():
            close_prices = {}
            for symbol in list(book["positions"]):
                raw = close_price(day, symbol)
                if raw is None:
                    _sell(book, symbol, book["positions"][symbol]["entry"], slip, day, index, "data_gap", closed_at)
                    continue
                close_prices[symbol] = raw
            equity = _equity(book, lambda symbol: close_prices[symbol]) if book["positions"] else book["cash"]
            if in_window:
                nav[name].append(equity)
            for symbol, position in list(book["positions"].items()):
                close = close_prices[symbol]
                position["peak"] = max(position["peak"], close)
                if close >= position["entry"] * (1 + SPEC["arm"]):
                    position["armed"] = True
                reason = None
                if close <= position["entry"] * (1 - SPEC["hard_stop"]):
                    reason = "hard_stop"
                elif position["armed"] and close <= position["peak"] * (1 - SPEC["trail"]):
                    reason = "profit_trail"
                elif index - position["entry_index"] >= SPEC["max_hold_sessions"]:
                    reason = "time_exit"
                if reason and not any(order["side"] == "sell" and order["symbol"] == symbol for order in book["pending"]):
                    book["pending"].append(
                        {"side": "sell", "symbol": symbol, "reason": reason, "decision_index": index}
                    )
            if halt_mode == "cooldown_21":
                _maybe_rearm(book, index, day, equity)
            book["peak"] = max(book["peak"], equity)
            if equity <= book["peak"] * (1 - SPEC["max_book_drawdown"]):
                _arm_halt(book, index, day)
            daily_block = equity <= book["anchor"] * (1 - SPEC["max_daily_loss"])
            if index >= end or book["entry_halt"] or daily_block:
                continue
            candidates = []
            for symbol in symbols:
                values = feat[symbol]
                if values is None or not _eligible(values) or symbol in book["positions"]:
                    continue
                if momentum_at is None:
                    score = _score(name, model, values)
                else:
                    momentum = momentum_at(symbol, day)
                    if momentum is None:
                        continue
                    score = momentum if name == "learner" else values[0]
                candidates.append((score, symbol, values))
            candidates.sort(key=lambda row: (-row[0], row[1]))
            if not candidates or len(book["positions"]) >= SPEC["max_names"]:
                continue
            _picked, symbol, values = candidates[0]
            book["pending"].append(
                {"side": "buy", "symbol": symbol, "x": list(values), "decision_index": index}
            )

    last = sessions[end]
    for book in books.values():
        book["pending"] = []
    liquidate(last, end, "close", "window_end", session_close(last))
    for name, book in books.items():
        if nav[name]:
            nav[name][-1] = book["cash"]

    def pack(name: str) -> dict:
        stats = path_stats(nav[name])
        trades = [trade for trade in books[name]["closed"] if trade["entry_index"] >= start]
        pnls = [trade["pnl"] for trade in trades]
        monday = [trade["pnl"] for trade in trades if trade["weekday"] == 0]
        mean_nav = sum(nav[name]) / len(nav[name]) if nav[name] else 1.0
        return {
            "return": stats["return"],
            "max_drawdown": stats["max_drawdown"],
            "first_half": stats["first_half"],
            "second_half": stats["second_half"],
            "sharpe": stats["sharpe"],
            "sessions": stats["sessions"],
            "daily_returns": stats["daily_returns"],
            "trades": len(trades),
            "turnover": buys[name] / mean_nav,
            "mean_trade_pnl": (sum(pnls) / len(pnls)) if pnls else 0.0,
            "monday_trade_pnl_share": (sum(monday) / sum(pnls)) if pnls and sum(pnls) != 0 else None,
            "halt_triggers": books[name]["halt_triggers"],
            "rearms": books[name]["rearms"],
            "halt_trigger_days": list(books[name]["halt_trigger_days"]),
            "rearm_days": list(books[name]["rearm_days"]),
        }

    def trade_log(name: str) -> list[dict]:
        return [
            {
                "symbol": trade["symbol"],
                "entry_session": trade["entry_session"],
                "exit_session": trade["exit_session"],
                "entry_fill": trade["entry_fill"],
                "reason": trade["reason"],
            }
            for trade in books[name]["closed"]
            if trade["entry_index"] >= start
        ]

    return {
        "baseline": pack("baseline"),
        "learner": pack("learner"),
        "trade_log": {"baseline": trade_log("baseline"), "learner": trade_log("learner")},
        "accepted_refits": accepted_refits,
        "rejected_refits": rejected_refits,
        "model_alive_at_end": model is not None,
        "quote_age_seconds": quote_age_seconds,
        "fresh_quotes": fresh,
        "halt_mode": halt_mode,
    }
