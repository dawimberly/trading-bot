"""Lab-only paper forward trail. Fixed thresholds. Not a backtest result.

Owner-authorized experiment for alpaca_paper. Medium and Live do not enter.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import config

logger = logging.getLogger(__name__)

BOOK = "alpaca_paper"
POLICY = "lab_forward_5pct_arm_2pct_peak"
EXIT_REASON = "lab_forward_peak_exit"
ARM_MULTIPLE = Decimal("1.05")
EXIT_MULTIPLE = Decimal("0.98")
COOLDOWN = timedelta(minutes=30)
STATE_NAME = "lab_forward_trail.json"
EVENT_NAME = "lab_forward_trail.jsonl"

_lock = threading.Lock()
_TEST_NOW: datetime | None = None


class CorruptState(Exception):
    """Visible failure. Callers must not overwrite the file or skip protective exits."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def enabled() -> bool:
    return config.paper_lab_concentrated_enabled()


def now() -> datetime:
    if _TEST_NOW is not None:
        return _TEST_NOW if _TEST_NOW.tzinfo else _TEST_NOW.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def arms(price, average) -> bool:
    return _decimal(price) >= _decimal(average) * ARM_MULTIPLE


def breaches(price, peak) -> bool:
    """Exactly 2.00% of the peak triggers. 1.99% does not."""
    return _decimal(price) <= _decimal(peak) * EXIT_MULTIPLE


def records_same_day_rebuy(reason: str) -> bool:
    """This policy must not turn its 30-minute cooldown into an all-day block."""
    return str(reason or "") != EXIT_REASON


def book_dir() -> Path | None:
    """Lab book directory only. None when this process is not that book."""
    if not enabled():
        return None
    marker = os.getenv("PAPER_JOURNAL_CSV") or os.getenv("HEARTBEAT_FILE")
    if not marker:
        logger.error("LAB_FORWARD state path unavailable: no Lab journal or heartbeat")
        return None
    parent = Path(marker).resolve().parent
    if parent.name != BOOK:
        logger.error(
            "LAB_FORWARD refused path %s because it is not the alpaca_paper book",
            parent,
        )
        return None
    return parent


def _state_path() -> Path | None:
    folder = book_dir()
    if folder is None:
        return None
    return folder / STATE_NAME


def _event_path() -> Path | None:
    folder = book_dir()
    if folder is None:
        return None
    return folder / EVENT_NAME


def _empty() -> dict:
    return {"schema": 1, "book": BOOK, "policy": POLICY, "positions": {}, "cooldowns": {}}


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_state() -> dict:
    path = _state_path()
    if path is None:
        raise CorruptState("out_of_scope")
    if not path.is_file():
        return _empty()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        logger.error("LAB_FORWARD STATE CORRUPT: %s", type(exc).__name__)
        raise CorruptState(type(exc).__name__) from exc
    if not isinstance(payload, dict) or payload.get("book") != BOOK:
        logger.error("LAB_FORWARD STATE CORRUPT: book_mismatch")
        raise CorruptState("book_mismatch")
    if not isinstance(payload.get("positions"), dict) or not isinstance(payload.get("cooldowns"), dict):
        logger.error("LAB_FORWARD STATE CORRUPT: shape")
        raise CorruptState("shape")
    payload.setdefault("policy", POLICY)
    payload.setdefault("schema", 1)
    return payload


def save_state(state: dict) -> None:
    path = _state_path()
    if path is None:
        raise CorruptState("out_of_scope")
    if state.get("book") != BOOK:
        raise CorruptState("refusing_cross_book_write")
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(state, indent=2, allow_nan=False, default=str)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(blob, encoding="utf-8")
    os.replace(temporary, path)


def record_event(event: str, **fields) -> None:
    path = _event_path()
    if path is None:
        logger.error("LAB_FORWARD event dropped: %s", event)
        return
    row = {
        "schema": 1,
        "book": BOOK,
        "policy": POLICY,
        "event": event,
        "observed_at": _stamp(now()),
        **fields,
    }
    try:
        blob = json.dumps(row, allow_nan=False, default=str, separators=(",", ":")) + "\n"
    except (TypeError, ValueError) as exc:
        logger.error("LAB_FORWARD event unreadable: %s", type(exc).__name__)
        return
    logger.info("LAB_FORWARD %s", blob.strip())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(blob)
            handle.flush()
    except OSError as exc:
        logger.error("LAB_FORWARD event write failed: %s", type(exc).__name__)


def _symbol(symbol: str) -> str:
    return config.normalize_symbol(symbol)


def _new_episode(stamp: str, *, initialization: str) -> dict:
    return {
        "episode_id": uuid.uuid4().hex,
        "armed": False,
        "armed_at": None,
        "average_entry_at_arm": None,
        "peak": None,
        "peak_at": None,
        "exit_pending": False,
        "trigger": None,
        "initialized_at": stamp,
        "initialization": initialization,
    }


def observe(state: dict, symbol: str, average, price, moment: datetime, *, initialization: str) -> list[dict]:
    """Update one open episode. A latch ignores later prices and a new average."""
    sym = _symbol(symbol)
    episode = state["positions"].get(sym)
    events: list[dict] = []
    stamp = _stamp(moment)
    if episode is None:
        episode = _new_episode(stamp, initialization=initialization)
        state["positions"][sym] = episode
        events.append(
            {
                "event": "initialize",
                "symbol": sym,
                "episode_id": episode["episode_id"],
                "average_entry": str(average),
                "mark": str(price),
                "historical_peak_used": False,
                "initialization": initialization,
            }
        )
    if episode.get("exit_pending"):
        return events
    if not episode.get("armed"):
        if average is None or price is None or not arms(price, average):
            return events
        episode["armed"] = True
        episode["armed_at"] = stamp
        episode["average_entry_at_arm"] = str(average)
        episode["peak"] = str(price)
        episode["peak_at"] = stamp
        events.append(
            {
                "event": "arm",
                "symbol": sym,
                "episode_id": episode["episode_id"],
                "average_entry": str(average),
                "mark": str(price),
                "peak": str(price),
                "historical_peak_used": False,
            }
        )
        return events
    peak = _decimal(episode["peak"])
    observed = _decimal(price)
    if observed > peak:
        episode["peak"] = str(price)
        episode["peak_at"] = stamp
        events.append(
            {
                "event": "peak",
                "symbol": sym,
                "episode_id": episode["episode_id"],
                "peak": str(price),
            }
        )
    if breaches(price, episode["peak"]):
        threshold = str(_decimal(episode["peak"]) * EXIT_MULTIPLE)
        episode["exit_pending"] = True
        episode["trigger"] = {
            "mark": str(price),
            "peak": episode["peak"],
            "threshold": threshold,
            "at": stamp,
        }
        events.append(
            {
                "event": "breach",
                "symbol": sym,
                "episode_id": episode["episode_id"],
                "mark": str(price),
                "peak": episode["peak"],
                "threshold": threshold,
            }
        )
    return events


def submission_action(episode: dict | None, open_orders) -> str:
    """submit, wait, or none. Unreadable orders wait so a latch is not duplicated."""
    if not episode or not episode.get("exit_pending"):
        return "none"
    if open_orders is None:
        return "wait"
    sells = [
        row
        for row in open_orders
        if "sell" in str(row.get("side", "")).lower()
    ]
    if sells:
        return "wait"
    return "submit"


def confirm_flat(state: dict, symbol: str, moment: datetime) -> dict | None:
    """Broker-flat only. Clears the episode and keeps the cooldown."""
    sym = _symbol(symbol)
    episode = state["positions"].pop(sym, None)
    if not episode or not episode.get("exit_pending"):
        return None
    expires = moment + COOLDOWN
    state["cooldowns"][sym] = {
        "episode_id": episode["episode_id"],
        "policy": POLICY,
        "flat_at": _stamp(moment),
        "expires_at": _stamp(expires),
        "expiry_logged": False,
        "reentry_consumed": False,
        "trigger": episode.get("trigger"),
    }
    return {
        "event": "flat",
        "symbol": sym,
        "episode_id": episode["episode_id"],
        "flat_at": _stamp(moment),
        "cooldown_expires_at": _stamp(expires),
        "confirmed_fill": True,
    }


def drop_unlatched(state: dict, symbol: str) -> None:
    """Position closed by another exit. Do not invent this policy's cooldown."""
    sym = _symbol(symbol)
    episode = state["positions"].get(sym)
    if episode and not episode.get("exit_pending"):
        state["positions"].pop(sym, None)


def cooldown_blocks(state: dict, symbol: str, moment: datetime) -> bool:
    row = state["cooldowns"].get(_symbol(symbol))
    if not row:
        return False
    return moment < _parse(row["expires_at"])


def note_expiries(state: dict, moment: datetime) -> list[dict]:
    events = []
    for sym, row in state["cooldowns"].items():
        if row.get("expiry_logged"):
            continue
        if moment < _parse(row["expires_at"]):
            continue
        row["expiry_logged"] = True
        events.append(
            {
                "event": "cooldown_expired",
                "symbol": sym,
                "episode_id": row.get("episode_id"),
                "cooldown_expires_at": row.get("expires_at"),
            }
        )
    return events


def buy_block_reason(symbol: str, moment: datetime | None = None) -> str | None:
    """None when a Lab buy may continue into the ordinary guards."""
    if not enabled():
        return None
    moment = moment or now()
    try:
        with _lock:
            state = load_state()
    except CorruptState:
        return "lab_forward_state_corrupt"
    sym = _symbol(symbol)
    episode = state["positions"].get(sym)
    if episode and episode.get("exit_pending"):
        record_event("add_blocked_exit_pending", symbol=sym, episode_id=episode.get("episode_id"))
        return "lab_forward_exit_pending"
    if cooldown_blocks(state, sym, moment):
        row = state["cooldowns"][sym]
        record_event(
            "cooldown_block",
            symbol=sym,
            episode_id=row.get("episode_id"),
            cooldown_expires_at=row.get("expires_at"),
        )
        return "lab_forward_cooldown"
    return None


def ma50_pair_cooldown_exempt(symbol: str, moment) -> bool:
    """First re-entry only, after this policy's 30 minutes, until that fill."""
    if not isinstance(moment, datetime) or not enabled():
        return False
    try:
        with _lock:
            state = load_state()
    except CorruptState:
        return False
    row = state["cooldowns"].get(_symbol(symbol))
    if not row or row.get("reentry_consumed"):
        return False
    return moment >= _parse(row["expires_at"])


def mark_reentry_filled(symbol: str, moment: datetime | None = None) -> None:
    if not enabled():
        return
    moment = moment or now()
    try:
        with _lock:
            state = load_state()
            row = state["cooldowns"].get(_symbol(symbol))
            if not row or row.get("reentry_consumed"):
                return
            if moment < _parse(row["expires_at"]):
                return
            row["reentry_consumed"] = True
            row["reentry_filled_at"] = _stamp(moment)
            save_state(state)
    except CorruptState:
        logger.error("LAB_FORWARD reentry not marked: state corrupt")
        return
    record_event("reentry_filled", symbol=_symbol(symbol), confirmed_fill=True)


def reconcile_absent(open_symbols: set[str], moment: datetime | None = None) -> None:
    """Flat confirmation for latched names the broker no longer shows."""
    if not enabled():
        return
    moment = moment or now()
    try:
        with _lock:
            state = load_state()
            changed = False
            for sym in list(state["positions"]):
                if sym in open_symbols:
                    continue
                episode = state["positions"][sym]
                if episode.get("exit_pending"):
                    event = confirm_flat(state, sym, moment)
                    if event:
                        record_event(**event)
                    changed = True
                else:
                    drop_unlatched(state, sym)
                    changed = True
            for event in note_expiries(state, moment):
                record_event(**event)
                changed = True
            if changed:
                save_state(state)
    except CorruptState:
        return


def on_position(symbol: str, average, price, *, open_orders) -> str:
    """Persist the observation and return submit, wait, none, or corrupt."""
    if not enabled():
        return "none"
    moment = now()
    try:
        with _lock:
            state = load_state()
            sym = _symbol(symbol)
            initialization = (
                "new_episode" if sym in state["cooldowns"] else "first_observed_mark"
            )
            events = observe(
                state,
                symbol,
                average,
                price,
                moment,
                initialization=initialization,
            )
            action = submission_action(state["positions"].get(_symbol(symbol)), open_orders)
            save_state(state)
    except CorruptState:
        return "corrupt"
    for event in events:
        record_event(**event)
    if action == "wait":
        record_event(
            "exit_wait_open_order",
            symbol=_symbol(symbol),
            open_orders=open_orders,
            confirmed_fill=False,
        )
    return action


def note_exit_attempt(symbol: str, order, *, detail: dict | None = None) -> None:
    order_id = getattr(order, "id", None) if order is not None else None
    status = None if order is None else str(getattr(order, "status", "unknown"))
    record_event(
        "exit_attempt",
        symbol=_symbol(symbol),
        order_id=str(order_id) if order_id else None,
        status=status,
        confirmed_fill=False,
        submission_is_not_a_fill=True,
        same_day_rebuy_recorded=False,
        detail=detail,
    )


def note_flat_from_order(symbol: str) -> None:
    if not enabled():
        return
    moment = now()
    try:
        with _lock:
            state = load_state()
            event = confirm_flat(state, symbol, moment)
            if event:
                save_state(state)
            else:
                event = None
    except CorruptState:
        return
    if event:
        record_event(**event)
