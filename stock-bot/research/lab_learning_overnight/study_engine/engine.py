"""Virtual paper engine. File frames are the only market input. No broker client."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .calendar import (
    CALENDAR_HASH,
    ET,
    bar_starts,
    close_verified,
    first_open_after,
    hold_sessions,
    is_session,
    next_bar,
    prior_bar,
    session_bounds,
)
from .model import FEATURE_DEF, FEATURE_HASH, features, learn, predict

SPEC = {
    "schema": 2,
    "account": "lab_next_virtual",
    "initial_cash": 100000.0,
    "training_sessions": 100,
    "freeze_sessions": 30,
    "min_closed_trades": 30,
    "max_names": 10,
    "name_cap": 0.10,
    "initial_clip": 0.095,
    "sleeve": 0.95,
    "hard_stop": 0.05,
    "arm": 0.05,
    "trail": 0.02,
    "reentry_minutes": 30,
    "max_hold_sessions": 30,
    "slip_bps": 5,
    "entry_start": "10:15",
    "max_daily_loss": 0.02,
    "max_book_drawdown": 0.15,
    "price_observation": "completed five-minute regular-session CLOSES, not ticks or intrabar highs",
    "calendar_hash": CALENDAR_HASH,
}
SUPPORTED_ACTIONS = {"split", "cash_dividend", "stock_dividend"}
BLOCKED_ACTIONS = {
    "merger",
    "cash_merger",
    "stock_merger",
    "stock_and_cash_merger",
    "rename",
    "name_change",
    "spin_off",
    "spinoff",
    "redemption",
    "worthless_removal",
    "unit_split",
    "split_unparsed",
    "cash_dividend_unparsed",
}


def spec_hash() -> str:
    return hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()


def code_hash() -> str:
    folder = Path(__file__).resolve().parent
    blob = b"".join((folder / name).read_bytes() for name in ("engine.py", "model.py", "calendar.py"))
    return hashlib.sha256(blob).hexdigest()


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(ET)


def fresh() -> dict:
    def book():
        return {
            "cash": 100000.0,
            "positions": {},
            "pending": [],
            "cooldown": {},
            "closed": [],
            "plans": [],
            "skips": [],
            "equity": 100000.0,
            "peak": 100000.0,
            "anchor": 100000.0,
            "entry_halt": False,
            "costs": 0.0,
            "bought": 0.0,
            "sold": 0.0,
            "dividends": 0.0,
            "receivable": 0.0,
            "dividend_receipts": [],
            "external_flows": 0.0,
        }

    return {
        "spec_hash": spec_hash(),
        "code_hash": code_hash(),
        "last": None,
        "frames": {},
        "sessions": [],
        "completed_sessions": [],
        "blocked_symbols": {},
        "history": {},
        "last_prices": {},
        "model": None,
        "model_id": None,
        "learning": [],
        "last_training_count": None,
        "books": {name: book() for name in ("baseline", "learner")},
        "events": [],
        "marks": [],
    }


def _money(value) -> Decimal:
    return Decimal(str(value))


MARKET_FEED_BLOCK = (
    "historical_replay and forward_observation are blocked until a "
    "source-verifying market-data adapter exists"
)


def reject_unverified_market_feed(frame: dict) -> None:
    """Self-declared provenance cannot admit a market feed."""
    if frame.get("feed_mode") in {"historical_replay", "forward_observation", "verified_adapter"}:
        raise ValueError(MARKET_FEED_BLOCK)


def _audit_events(frame: dict, day: str, *, verified: bool = False) -> list[dict]:
    if not verified:
        reject_unverified_market_feed(frame)
    if frame.get("actions_checked") is True and "action_audit" not in frame:
        raise ValueError("actions_checked boolean is not an independent corporate-action audit")
    audit = frame.get("action_audit")
    if not isinstance(audit, dict):
        raise ValueError("action_audit is required; a caller boolean is not an audit")
    mode = frame.get("feed_mode")
    source = audit.get("source")
    independent = audit.get("independent")
    if mode == "synthetic":
        if source != "synthetic_declared" or independent is not False:
            raise ValueError("synthetic frames must declare an unaudited synthetic action list")
    elif mode == 'offline_research':
        if source != 'pinned_research_manifest' or independent is not True:
            raise ValueError('offline study requires a pinned manifest audit')
    elif verified and mode == "verified_adapter":
        if source != "alpaca_data_v1_corporate_actions" or independent is not True:
            raise ValueError("verified adapter evidence is incomplete")
    else:
        raise ValueError("feed_mode must be synthetic until a source-verifying adapter exists")
    if str(audit.get("coverage_from")) > day or str(audit.get("coverage_through")) < day:
        raise ValueError("action audit does not cover this session")
    covered = set(audit.get("symbols") or [])
    events = audit.get("events")
    if not isinstance(events, list):
        raise ValueError("action audit events must be a list")
    for event in events:
        kind = event.get("type")
        symbol = event.get("symbol")
        if symbol not in covered:
            raise ValueError("action symbol is outside the audit coverage")
        if kind not in SUPPORTED_ACTIONS and kind not in BLOCKED_ACTIONS:
            raise ValueError(
                f"unsupported corporate action {kind}; processing blocked until it is accounted for"
            )
    if not set(frame["bars"]).issubset(covered):
        raise ValueError("bar symbol is outside the action-audit coverage")
    return events


def validate(frame: dict, state: dict, *, verified: bool = False) -> datetime:
    moment = timestamp(frame["timestamp"])
    if moment.second or moment.microsecond or moment.minute % 5:
        raise ValueError("requires a five-minute bar START")
    day = moment.date().isoformat()
    if not is_session(day):
        raise ValueError("not an exchange session")
    start, end = session_bounds(day)
    if moment < start or moment >= end:
        raise ValueError("bar is outside the session, including an early close")
    if frame.get("complete") is not True or frame.get("price_basis") != "raw":
        raise ValueError("only completed raw OHLC bars are tradable")
    as_of = timestamp(frame["as_of"])
    if as_of < moment + timedelta(minutes=5):
        raise ValueError("bar is not completed relative to as_of")
    if state["last"] and moment <= timestamp(state["last"]):
        raise ValueError("duplicate or out-of-order frame")
    bars = frame.get("bars") or {}
    if not bars:
        raise ValueError("empty frame")
    for symbol, bar in bars.items():
        if not isinstance(symbol, str) or not symbol or not isinstance(bar.get("eligible"), bool):
            raise ValueError("explicit point-in-time eligibility required")
        values = [bar[key] for key in ("open", "high", "low", "close")]
        if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0 for value in values):
            raise ValueError("invalid OHLC")
        open_, high, low, close = values
        if high < max(open_, low, close) or low > min(open_, high, close):
            raise ValueError("invalid OHLC range")
    for book in state["books"].values():
        if not set(book["positions"]).issubset(bars):
            raise ValueError("missing held-symbol price; cannot mark at a stale close")
    return moment, _audit_events(frame, day, verified=verified)


def _train(state: dict, moment: datetime) -> None:
    completed = state["completed_sessions"]
    if len(completed) < SPEC["training_sessions"]:
        return
    if state["last_training_count"] is not None and len(completed) - state["last_training_count"] < SPEC["freeze_sessions"]:
        return
    window = completed[-SPEC["training_sessions"]:]
    start, end = window[0], window[-1]
    rows = []
    for trade in state["books"]["baseline"]["closed"]:
        if not (start <= trade["exit_session"] <= end):
            continue
        if timestamp(trade["exit_time"]) >= moment:
            continue
        rows.append(trade)
    model, info = learn(rows)
    state["model"] = model
    state["model_id"] = (
        hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()[:16] if model else None
    )
    info.update(
        asof=moment.isoformat(),
        training_start=start,
        training_end=end,
        model_id=state["model_id"],
        source="baseline closed virtual round trips only",
        provenance=(
            "exit timestamp before this decision; exit session inside the last 100 "
            "complete sessions; entry may be earlier. Learner trades are not labels."
        ),
        feature_hash=FEATURE_HASH,
        feature_def=FEATURE_DEF,
        activated_at=moment.isoformat() if model else None,
        freeze_complete_sessions=SPEC["freeze_sessions"],
        frozen_until_completed_count=len(completed) + SPEC["freeze_sessions"],
        fallback=model is None,
        portfolio_promotion=False,
    )
    state["learning"].append(info)
    state["last_training_count"] = len(completed)


def _apply_actions(state: dict, moment: datetime, events: list[dict]) -> None:
    if moment.hour != 9 or moment.minute != 30:
        if events:
            raise ValueError("corporate action must be declared on the 09:30 bar")
        return
    day = moment.date().isoformat()
    for name, book in state["books"].items():
        pending = []
        for item in book.get("dividend_receipts") or []:
            if item["payable_date"] <= day:
                book["cash"] += item["cash"]
                book["receivable"] = book.get("receivable", 0.0) - item["cash"]
                state["events"].append(
                    {
                        "time": moment.isoformat(),
                        "book": name,
                        "event": "dividend_payable",
                        "symbol": item["symbol"],
                        "cash": item["cash"],
                        "ex_date": item["ex_date"],
                        "payable_date": item["payable_date"],
                    }
                )
            else:
                pending.append(item)
        book["dividend_receipts"] = pending
    seen = set()
    for event in events:
        symbol = event["symbol"]
        if symbol in seen:
            raise ValueError("duplicate corporate action on one open")
        seen.add(symbol)
        if event.get("ex_date") and str(event["ex_date"])[:10] != day:
            raise ValueError("corporate action ex_date does not match the session")
        kind = event["type"]
        if kind in BLOCKED_ACTIONS:
            state.setdefault("blocked_symbols", {})[symbol] = {
                "action": kind,
                "time": moment.isoformat(),
                "preserved": True,
            }
            state["events"].append(
                {
                    "time": moment.isoformat(),
                    "event": "symbol_blocked",
                    "symbol": symbol,
                    "action": kind,
                    "preserved": True,
                }
            )
            continue
        if kind in {"split", "stock_dividend"}:
            ratio = event.get("ratio")
            if not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio <= 0:
                raise ValueError("split ratio must be new/old and positive")
            history = state["history"].get(symbol)
            if history:
                state["history"][symbol] = [[item[0], item[1] / ratio] for item in history]
            for book in state["books"].values():
                position = book["positions"].get(symbol)
                if position:
                    position["qty"] *= ratio
                    position["entry"] /= ratio
                    position["peak"] /= ratio
            state["events"].append(
                {"time": moment.isoformat(), "event": kind, "symbol": symbol, "ratio": ratio, "price_basis": "raw"}
            )
        else:
            amount = event.get("amount")
            if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
                raise ValueError("cash dividend amount must be positive per share")
            payable = str(event.get("payable_date") or day)[:10]
            for name, book in state["books"].items():
                position = book["positions"].get(symbol)
                if not position:
                    continue
                cash = position["qty"] * amount
                book["dividends"] += cash
                record = {
                    "time": moment.isoformat(),
                    "book": name,
                    "event": "cash_dividend",
                    "symbol": symbol,
                    "cash": cash,
                    "ex_date": day,
                    "payable_date": payable,
                    "cash_posted": payable <= day,
                }
                if payable <= day:
                    book["cash"] += cash
                else:
                    book["receivable"] = book.get("receivable", 0.0) + cash
                    book.setdefault("dividend_receipts", []).append(
                        {"symbol": symbol, "cash": cash, "ex_date": day, "payable_date": payable}
                    )
                state["events"].append(record)


def _timing(order: dict, moment: datetime, received_at: str | None, adjacent: bool) -> dict:
    note = {"gap_fill": not adjacent}
    if order.get("decision_at"):
        note.update(
            decision_at=order["decision_at"],
            bar_end=(moment + timedelta(minutes=5)).isoformat(),
            execution_observed_at=received_at,
            modeled_fill=True,
            slippage_bps=SPEC["slip_bps"],
        )
    return note


def _execute(state: dict, moment: datetime, bars: dict, received_at: str | None = None) -> None:
    slip = SPEC["slip_bps"] / 10000
    blocked = state.get("blocked_symbols") or {}
    for name, book in state["books"].items():
        waiting = book["pending"]
        book["pending"] = []
        for order in waiting:
            symbol = order["symbol"]
            if symbol in blocked:
                book["skips"].append(
                    {"time": moment.isoformat(), "symbol": symbol, "reason": "unsupported_corporate_action_preserved"}
                )
                continue
            if order.get("decision_at"):
                due = first_open_after(timestamp(order["decision_at"]))
                if moment < due:
                    book["pending"].append(order)
                    continue
            else:
                due = next_bar(timestamp(order["decision_time"]))
            adjacent = moment == due
            if symbol not in bars:
                book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "entry_symbol_absent"})
                continue
            if timestamp(order["decision_time"]) >= moment:
                raise ValueError("same-bar execution forbidden")
            raw = bars[symbol]["open"]
            if order["side"] == "sell":
                position = book["positions"].pop(symbol, None)
                if position is None:
                    continue
                fill = raw * (1 - slip)
                proceeds = position["qty"] * fill
                book["cash"] += proceeds
                book["sold"] += proceeds
                book["costs"] += position["qty"] * raw - proceeds
                trade = {
                    "symbol": symbol,
                    "entry_time": position["entry_time"],
                    "entry_session": position["entry_session"],
                    "exit_time": moment.isoformat(),
                    "exit_session": moment.date().isoformat(),
                    "entry": position["entry"],
                    "exit": fill,
                    "exit_open": raw,
                    "qty": position["qty"],
                    "pnl": proceeds - position["cost"],
                    "y": proceeds / position["cost"] - 1,
                    "x": position["x"],
                    "reason": order["reason"],
                    "plan_id": position["plan_id"],
                    **_timing(order, moment, received_at, adjacent),
                }
                book["closed"].append(trade)
                book["cooldown"][symbol] = (moment + timedelta(minutes=SPEC["reentry_minutes"])).isoformat()
                state["events"].append({"book": name, "event": "virtual_sell", **trade})
                continue
            if not adjacent:
                book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "non_adjacent_entry_cancelled"})
                continue
            if symbol in book["positions"] or not bars[symbol]["eligible"] or book["entry_halt"]:
                book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "entry_blocked_at_open"})
                continue
            if symbol in book["cooldown"] and moment < timestamp(book["cooldown"][symbol]):
                book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "cooldown"})
                continue
            equity = book["cash"] + book.get("receivable", 0.0) + sum(
                pos["qty"] * bars[held]["open"] for held, pos in book["positions"].items()
            )
            if equity <= book["peak"] * (1 - SPEC["max_book_drawdown"]):
                book["entry_halt"] = True
                book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "drawdown_halt"})
                continue
            if equity <= book["anchor"] * (1 - SPEC["max_daily_loss"]):
                book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "daily_loss"})
                continue
            gross = equity - book["cash"] - book.get("receivable", 0.0)
            dollars = min(order["dollars"], book["cash"], max(0, equity * SPEC["sleeve"] - gross), equity * SPEC["name_cap"])
            if dollars < 1 or len(book["positions"]) >= SPEC["max_names"]:
                book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "size_or_slot"})
                continue
            fill = raw * (1 + slip)
            qty = dollars / fill
            book["cash"] -= dollars
            book["bought"] += dollars
            book["costs"] += dollars - qty * raw
            book["positions"][symbol] = {
                "qty": qty,
                "cost": dollars,
                "entry": fill,
                "peak": fill,
                "armed": False,
                "entry_time": moment.isoformat(),
                "entry_session": moment.date().isoformat(),
                "x": order["x"],
                "plan_id": order["plan_id"],
            }
            state["events"].append(
                {
                    "book": name,
                    "time": moment.isoformat(),
                    "event": "virtual_buy",
                    "symbol": symbol,
                    "qty": qty,
                    "fill": fill,
                    "plan_id": order["plan_id"],
                    **_timing(order, moment, received_at, adjacent),
                }
            )


def _append_history(state: dict, moment: datetime, bars: dict, *, store: str = "history") -> None:
    iso = moment.isoformat()
    expected = prior_bar(moment)
    root = state.setdefault(store, {})
    for symbol, bar in bars.items():
        history = root.get(symbol) or []
        if not history or not isinstance(history[-1], list) or timestamp(history[-1][0]) != expected:
            history = []
        history.append([iso, bar["close"]])
        root[symbol] = history[-51:]
        if store == "history":
            state["last_prices"][symbol] = bar["close"]


def warm_indicators(previous: dict, sequence: list[tuple[datetime, dict]]) -> dict:
    """Record completed closes only. This does not decide, fill, or complete a session."""
    state = copy.deepcopy(previous)
    if state["completed_sessions"]:
        raise ValueError("warmup cannot follow a completed forward session")
    for book in state["books"].values():
        if book["positions"] or book["pending"] or book["plans"] or book["closed"]:
            raise ValueError("warmup cannot create or follow a virtual trade")
    lengths: dict[str, int] = {}
    for moment, bars in sequence:
        day = moment.date().isoformat()
        if not is_session(day):
            raise ValueError("warmup bar is not a session")
        start, end = session_bounds(day)
        if moment < start or moment >= end:
            raise ValueError("warmup bar is outside the session")
        for symbol in bars:
            lengths[symbol] = len(state["history"].get(symbol) or [])
        _append_history(state, moment, bars)
        for symbol in bars:
            after = len(state["history"][symbol])
            if lengths[symbol] and after < min(lengths[symbol] + 1, 51):
                raise ValueError("warmup gap resets indicator history")
    state["events"] = []
    state["marks"] = []
    for book in state["books"].values():
        book["plans"] = []
        book["pending"] = []
        book["skips"] = []
    if sequence:
        state["last"] = sequence[-1][0].isoformat()
        state["indicator_warmup"] = {"through": state["last"], "forward_sessions": []}
    if state["completed_sessions"]:
        raise AssertionError("warmup marked a forward session")
    return state


def _decide(state: dict, moment: datetime, bars: dict, received_at: str | None = None) -> None:
    minute = moment.hour * 60 + moment.minute
    same_day_follow = next_bar(moment).date() == moment.date()
    entry_open = same_day_follow and minute >= 10 * 60 + 15
    blocked = state.get("blocked_symbols") or {}
    for name, book in state["books"].items():
        equity = book["cash"] + book.get("receivable", 0.0) + sum(
            pos["qty"] * bars[symbol]["close"] for symbol, pos in book["positions"].items()
        )
        for symbol, position in book["positions"].items():
            if symbol in blocked:
                book["skips"].append(
                    {"time": moment.isoformat(), "symbol": symbol, "reason": "unsupported_corporate_action_preserved"}
                )
                continue
            close = bars[symbol]["close"]
            position["peak"] = max(position["peak"], close)
            if _money(close) >= _money(position["entry"]) * Decimal("1.05"):
                position["armed"] = True
            reason = None
            if _money(close) <= _money(position["entry"]) * Decimal("0.95"):
                reason = "hard_stop"
            elif position["armed"] and _money(close) <= _money(position["peak"]) * Decimal("0.98"):
                reason = "profit_trail"
            elif hold_sessions(position["entry_session"], moment.date().isoformat()) >= SPEC["max_hold_sessions"]:
                reason = "time_exit"
            queued = any(order["side"] == "sell" and order["symbol"] == symbol for order in book["pending"])
            if reason and not queued:
                sell = {"side": "sell", "symbol": symbol, "reason": reason, "decision_time": moment.isoformat()}
                if received_at:
                    sell["decision_at"] = received_at
                book["pending"].append(sell)
                state["events"].append(
                    {"book": name, "time": moment.isoformat(), "event": "exit_latched", "symbol": symbol, "reason": reason, "peak": position["peak"], "close": close}
                )
        book["equity"] = equity
        book["peak"] = max(book["peak"], equity)
        if equity <= book["peak"] * (1 - SPEC["max_book_drawdown"]):
            book["entry_halt"] = True
        daily_block = equity <= book["anchor"] * (1 - SPEC["max_daily_loss"])
        candidates = []
        if entry_open and not book["entry_halt"] and not daily_block:
            for symbol, bar in bars.items():
                if symbol in blocked:
                    book["skips"].append(
                        {"time": moment.isoformat(), "symbol": symbol, "reason": "unsupported_corporate_action_preserved"}
                    )
                    continue
                if symbol in book["positions"]:
                    book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "already_held"})
                    continue
                if not bar["eligible"]:
                    book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "ineligible"})
                    continue
                if symbol in book["cooldown"] and moment < timestamp(book["cooldown"][symbol]):
                    book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "cooldown"})
                    continue
                score_x = features(state["history"][symbol])
                if score_x is None:
                    book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "indicator_gap"})
                    continue
                if score_x[0] <= 0:
                    book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "extension_not_positive"})
                    continue
                if score_x[2] >= 0.70:
                    book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "rsi_at_or_above_70"})
                    continue
                score = predict(state["model"], score_x) if name == "learner" and state["model"] else score_x[0]
                candidates.append((score, symbol, score_x))
        candidates.sort(key=lambda row: (-row[0], row[1]))
        if len(book["positions"]) >= SPEC["max_names"]:
            for _score, symbol, _values in candidates:
                book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "max_names"})
        elif candidates:
            for index, (score, symbol, values) in enumerate(candidates):
                if index:
                    book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "not_top_rank"})
                    continue
                gross = equity - book["cash"] - book.get("receivable", 0.0)
                dollars = min(book["cash"], equity * SPEC["initial_clip"], max(0, equity * SPEC["sleeve"] - gross))
                if dollars < 1:
                    book["skips"].append({"time": moment.isoformat(), "symbol": symbol, "reason": "below_min_notional"})
                    continue
                plan_id = hashlib.sha256(f"{name}:{moment.isoformat()}:{symbol}".encode()).hexdigest()[:20]
                plan = {
                    "id": plan_id,
                    "book": name,
                    "symbol": symbol,
                    "decision_time": moment.isoformat(),
                    "features": values,
                    "rank_score": score,
                    "model_id": state["model_id"] if name == "learner" else None,
                    "ranking": "learned" if name == "learner" and state["model"] else "fixed_ma50_extension",
                    "entry_fill": "next scheduled five-minute OPEN plus 5 bps; a later bar cancels the buy",
                    "notional_budget": dollars,
                    "hard_stop_fraction": SPEC["hard_stop"],
                    "arm_fraction": SPEC["arm"],
                    "trail_fraction": SPEC["trail"],
                    "peak_definition": "highest observed five-minute CLOSE since fill; not an intrabar high",
                    "sell_fill": "next scheduled OPEN minus 5 bps; a missed bar is a labeled gap fill",
                    "max_hold_sessions": SPEC["max_hold_sessions"],
                    "reentry_minutes": SPEC["reentry_minutes"],
                }
                book["plans"].append(plan)
                buy = {
                    "side": "buy",
                    "symbol": symbol,
                    "dollars": dollars,
                    "x": values,
                    "plan_id": plan_id,
                    "decision_time": moment.isoformat(),
                }
                if received_at:
                    buy["decision_at"] = received_at
                book["pending"].append(buy)
        state["marks"].append(
            {
                "time": moment.isoformat(),
                "book": name,
                "equity": equity,
                "cash": book["cash"],
                "gross": equity - book["cash"] - book.get("receivable", 0.0),
                "positions": len(book["positions"]),
                "entry_halt": book["entry_halt"],
                "daily_entry_block": daily_block,
                "model_id": state["model_id"] if name == "learner" else None,
            }
        )
        if book["cash"] < -1e-6:
            raise AssertionError("negative cash")


def observe_only(previous: dict, frame: dict) -> dict:
    """Keep indicator continuity for a late bar without a decision, fill, or completed session."""
    moment, _events = validate(frame, previous, verified=True)
    state = copy.deepcopy(previous)
    state["events"] = []
    state["marks"] = []
    for book in state["books"].values():
        book["plans"] = []
        book["skips"] = []
    _append_history(state, moment, frame["bars"])
    state["last"] = moment.isoformat()
    return state


def advance(previous: dict, frame: dict, *, verified: bool = False) -> dict:
    moment, events = validate(frame, previous, verified=verified)
    state = previous  # Offline runner owns this state; no external observers.
    state["events"] = []
    state["marks"] = []
    for book in state["books"].values():
        book["plans"] = []
        book["skips"] = []
    day = moment.date().isoformat()
    received = frame.get("received_at") if verified else None
    if verified:
        if not received:
            raise ValueError("verified observation needs received_at")
        if timestamp(received) >= session_bounds(day)[1]:
            raise ValueError("after-close receipt cannot create a forward decision")
    new_day = day not in state["sessions"]
    if new_day:
        state["sessions"].append(day)
        for book in state["books"].values():
            book["anchor"] = book["equity"]
        _train(state, moment)
    state["frames"].setdefault(day, []).append(moment.strftime("%H:%M"))
    _apply_actions(state, moment, events)
    _execute(state, moment, frame["bars"], received)
    _append_history(state, moment, frame["bars"])
    _decide(state, moment, frame["bars"], received)
    if close_verified(day) and state["frames"][day] == bar_starts(day):
        state["completed_sessions"].append(day)
    state["last"] = moment.isoformat()
    return state


def advance_verified(previous: dict, proof) -> dict:
    """Apply one observation only after the adapter has bound it to raw evidence."""
    from .adapter import assert_proof

    frame = assert_proof(proof)
    return advance(previous, frame, verified=True)
