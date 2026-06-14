"""Production safety: daily loss circuit breaker (blocks new entries + thinking tilts).

Live book: 2% daily loss limit (config.THINKING_DAILY_LOSS_LIMIT_LIVE).
Paper book: 4% daily loss limit (config.THINKING_DAILY_LOSS_LIMIT_PAPER).
Thinking tilts: +/-6% per sleeve cap; live requires manual approval (config).
"""

from __future__ import annotations

import datetime
import logging

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

STATE_FILE = "trading_safety_state.json"
_entry_block_reason: str | None = None


def _state_path() -> str:
    return config.TRADING_SAFETY_STATE_FILE if hasattr(config, "TRADING_SAFETY_STATE_FILE") else STATE_FILE


def is_paper_trading_book() -> bool:
    """True for paper aggressive / paper chase book (4% daily loss limit)."""
    if config.paper_aggressive_context() or config.paper_only_sleeves_active():
        return True
    return bool(config.PAPER_TRADING and config.paper_chase_mode_enabled())


def daily_loss_limit_pct(*, paper: bool | None = None) -> float:
    if paper if paper is not None else is_paper_trading_book():
        return config.THINKING_DAILY_LOSS_LIMIT_PAPER
    return config.THINKING_DAILY_LOSS_LIMIT_LIVE


def _book_key(*, paper: bool) -> str:
    return "paper" if paper else "live"


def update_daily_equity_anchor(equity: float | None, *, paper: bool | None = None) -> None:
    """Record opening equity for the current session (resets daily)."""
    if equity is None or equity <= 0:
        return
    paper = is_paper_trading_book() if paper is None else paper
    today = datetime.date.today().isoformat()
    key = _book_key(paper=paper)
    state = read_json_file(_state_path())
    book = state.setdefault(key, {})
    if book.get("daily_equity_date") != today:
        book["daily_equity_date"] = today
        book["daily_equity_open"] = round(float(equity), 4)
        book.pop("circuit_tripped", None)
        state[key] = book
        write_json_file(_state_path(), state)


def daily_loss_circuit_tripped(
    equity: float | None,
    *,
    paper: bool | None = None,
) -> tuple[bool, str, float]:
    """
    True when intraday loss exceeds limit (2% live, 4% paper).
    Returns (tripped, reason, loss_pct).
    """
    if equity is None or equity <= 0:
        return False, "", 0.0
    if not config.DAILY_LOSS_CIRCUIT_BREAKER_ENABLED:
        return False, "", 0.0

    paper = is_paper_trading_book() if paper is None else paper
    update_daily_equity_anchor(equity, paper=paper)
    state = read_json_file(_state_path())
    book = state.get(_book_key(paper=paper), {})
    open_eq = float(book.get("daily_equity_open") or equity)
    if open_eq <= 0:
        return False, "", 0.0

    loss_pct = (open_eq - float(equity)) / open_eq
    limit = daily_loss_limit_pct(paper=paper)
    if loss_pct < limit - 1e-9:
        return False, "", loss_pct

    reason = (
        f"daily loss circuit breaker ({loss_pct:.2%} >= {limit:.2%} limit, "
        f"{'paper' if paper else 'live'} book)"
    )
    book["circuit_tripped"] = True
    book["circuit_tripped_at"] = datetime.datetime.now().isoformat()
    book["loss_pct"] = round(loss_pct, 6)
    state[_book_key(paper=paper)] = book
    write_json_file(_state_path(), state)
    return True, reason, loss_pct


def set_entry_block_for_cycle(reason: str | None) -> None:
    """Set/clear per-cycle entry block (read by regime_entries_paused)."""
    global _entry_block_reason
    _entry_block_reason = reason


def entry_block_active() -> bool:
    return bool(_entry_block_reason)


def entry_block_reason() -> str:
    return _entry_block_reason or ""


def get_daily_loss_status(*, paper: bool | None = None) -> dict:
    """Snapshot for status.py / heartbeat."""
    paper = is_paper_trading_book() if paper is None else paper
    state = read_json_file(_state_path())
    book = state.get(_book_key(paper=paper), {})
    limit = daily_loss_limit_pct(paper=paper)
    open_eq = book.get("daily_equity_open")
    loss_pct = book.get("loss_pct")
    return {
        "book": "paper" if paper else "live",
        "limit_pct": round(limit * 100, 1),
        "open_equity": open_eq,
        "loss_pct": round(float(loss_pct) * 100, 2) if loss_pct is not None else None,
        "tripped": bool(book.get("circuit_tripped")),
        "date": book.get("daily_equity_date"),
    }
