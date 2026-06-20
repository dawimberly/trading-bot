"""Production safety: daily loss circuit breaker (blocks new entries + thinking tilts).

Live book: 2% daily loss limit (config.THINKING_DAILY_LOSS_LIMIT_LIVE).
Paper book: 4% daily loss limit (config.THINKING_DAILY_LOSS_LIMIT_PAPER).
Thinking tilts: +/-6% per sleeve cap; live requires manual approval (config).
"""

from __future__ import annotations

import datetime
import logging
import os

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

STATE_FILE = "trading_safety_state.json"
_entry_block_reason: str | None = None
LIVE_ANCHOR_CEILING = float(os.getenv("LIVE_DAILY_LOSS_ANCHOR_CEILING", "1500"))


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


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def _intraday_loss_ratio(open_eq: float, equity: float) -> float:
    if open_eq <= 0 or equity <= 0:
        return 0.0
    return (float(open_eq) - float(equity)) / float(open_eq)


def _clear_circuit_trip(book: dict) -> dict:
    book = dict(book)
    book.pop("circuit_tripped", None)
    book.pop("circuit_tripped_at", None)
    book.pop("loss_pct", None)
    return book


def _live_anchor_contaminated(open_eq: float, current_eq: float | None) -> bool:
    """Live Profile A anchors should not be paper-scale (~$98k) while equity is ~$300."""
    if open_eq <= LIVE_ANCHOR_CEILING:
        return False
    if current_eq is None:
        return True
    return float(current_eq) < LIVE_ANCHOR_CEILING


def _persist_book(state: dict, key: str, book: dict) -> None:
    state[key] = book
    write_json_file(_state_path(), state)


def _paper_anchor_contaminated(open_eq: float, current_eq: float | None) -> bool:
    """Paper book anchor should not be live-scale (~$300) while equity is paper-scale."""
    if current_eq is None or float(current_eq) < 5000:
        return False
    return float(open_eq) < 1000


def _normalize_book_for_session(
    book: dict,
    *,
    paper: bool,
    current_equity: float | None,
    today: str,
) -> tuple[dict, bool]:
    """Repair stale day / contaminated anchor; return (book, changed)."""
    book = dict(book or {})
    changed = False

    if book.get("daily_equity_date") != today:
        book = _clear_circuit_trip(book)
        book.pop("daily_equity_open", None)
        book.pop("daily_equity_date", None)
        changed = True

    open_eq = book.get("daily_equity_open")
    if open_eq is None:
        return book, changed

    contaminated = (
        _live_anchor_contaminated(float(open_eq), current_equity)
        if not paper
        else _paper_anchor_contaminated(float(open_eq), current_equity)
    )
    if contaminated:
        logger.warning(
            "%s daily-loss anchor contaminated (open=%.2f, current=%s) — resetting",
            "paper" if paper else "live",
            float(open_eq),
            current_equity,
        )
        book = _clear_circuit_trip(book)
        if current_equity is not None and float(current_equity) > 0:
            book["daily_equity_date"] = today
            book["daily_equity_open"] = round(float(current_equity), 4)
        else:
            book.pop("daily_equity_open", None)
            book.pop("daily_equity_date", None)
        changed = True

    return book, changed


def update_daily_equity_anchor(equity: float | None, *, paper: bool | None = None) -> None:
    """Record opening equity for the current session (resets daily)."""
    if equity is None or equity <= 0:
        return
    paper = is_paper_trading_book() if paper is None else paper
    if not paper and float(equity) > LIVE_ANCHOR_CEILING:
        logger.warning(
            "skip live daily-loss anchor: equity %.2f exceeds live ceiling %.0f",
            float(equity),
            LIVE_ANCHOR_CEILING,
        )
        return
    if paper and float(equity) < 1000:
        logger.warning(
            "skip paper daily-loss anchor: equity %.2f looks like live-scale",
            float(equity),
        )
        return
    today = _today_iso()
    key = _book_key(paper=paper)
    state = read_json_file(_state_path())
    book, _ = _normalize_book_for_session(
        state.get(key) or {},
        paper=paper,
        current_equity=float(equity),
        today=today,
    )
    if book.get("daily_equity_date") != today:
        book["daily_equity_date"] = today
        book["daily_equity_open"] = round(float(equity), 4)
        book = _clear_circuit_trip(book)
        _persist_book(state, key, book)


def refresh_daily_loss_session(
    equity: float | None,
    *,
    paper: bool | None = None,
    startup: bool = False,
) -> None:
    """On bot restart / first cycle: repair live anchor and clear false trips."""
    if equity is None or equity <= 0:
        return
    paper = is_paper_trading_book() if paper is None else paper
    today = _today_iso()
    key = _book_key(paper=paper)
    state = read_json_file(_state_path())
    book, changed = _normalize_book_for_session(
        state.get(key) or {},
        paper=paper,
        current_equity=float(equity),
        today=today,
    )

    limit = daily_loss_limit_pct(paper=paper)
    open_eq = book.get("daily_equity_open")
    if open_eq is None:
        book["daily_equity_date"] = today
        book["daily_equity_open"] = round(float(equity), 4)
        book = _clear_circuit_trip(book)
        changed = True
    elif startup and not paper:
        book["daily_equity_date"] = today
        book["daily_equity_open"] = round(float(equity), 4)
        book = _clear_circuit_trip(book)
        changed = True
    elif float(open_eq) > 0:
        loss_ratio = _intraday_loss_ratio(float(open_eq), float(equity))
        if book.get("circuit_tripped") and loss_ratio < limit - 1e-9:
            book = _clear_circuit_trip(book)
            changed = True

    if changed:
        _persist_book(state, key, book)
        if startup:
            logger.info(
                "daily loss session primed (%s book open=%.2f)",
                key,
                float(book.get("daily_equity_open") or equity),
            )


def daily_loss_circuit_tripped(
    equity: float | None,
    *,
    paper: bool | None = None,
) -> tuple[bool, str, float]:
    """
    True when intraday loss exceeds limit (2% live, 4% paper).
    Returns (tripped, reason, loss_ratio).
    """
    if equity is None or equity <= 0:
        return False, "", 0.0
    if not config.DAILY_LOSS_CIRCUIT_BREAKER_ENABLED:
        return False, "", 0.0

    paper = is_paper_trading_book() if paper is None else paper
    refresh_daily_loss_session(equity, paper=paper, startup=False)
    update_daily_equity_anchor(equity, paper=paper)
    state = read_json_file(_state_path())
    key = _book_key(paper=paper)
    book = dict(state.get(key) or {})
    open_eq = float(book.get("daily_equity_open") or equity)
    if open_eq <= 0:
        return False, "", 0.0

    loss_ratio = _intraday_loss_ratio(open_eq, float(equity))
    limit = daily_loss_limit_pct(paper=paper)

    if loss_ratio < limit - 1e-9:
        if book.get("circuit_tripped"):
            book = _clear_circuit_trip(book)
            _persist_book(state, key, book)
            logger.info(
                "daily loss circuit auto-cleared (%s book loss %.2f%% below %.0f%% limit)",
                key,
                loss_ratio * 100,
                limit * 100,
            )
        return False, "", loss_ratio

    reason = (
        f"daily loss circuit breaker ({loss_ratio:.2%} >= {limit:.2%} limit, "
        f"{'paper' if paper else 'live'} book)"
    )
    book["circuit_tripped"] = True
    book["circuit_tripped_at"] = datetime.datetime.now().isoformat()
    book["loss_pct"] = round(loss_ratio, 6)
    _persist_book(state, key, book)
    return True, reason, loss_ratio


def set_entry_block_for_cycle(reason: str | None) -> None:
    """Set/clear per-cycle entry block (read by regime_entries_paused)."""
    global _entry_block_reason
    _entry_block_reason = reason


def entry_block_active() -> bool:
    return bool(_entry_block_reason)


def entry_block_reason() -> str:
    return _entry_block_reason or ""


def get_daily_loss_status(
    *,
    paper: bool | None = None,
    current_equity: float | None = None,
) -> dict:
    """Snapshot for status.py / dashboard — always recompute from session open vs current."""
    paper = is_paper_trading_book() if paper is None else paper
    today = _today_iso()
    state = read_json_file(_state_path())
    key = _book_key(paper=paper)
    book, changed = _normalize_book_for_session(
        state.get(key) or {},
        paper=paper,
        current_equity=current_equity,
        today=today,
    )
    limit_ratio = daily_loss_limit_pct(paper=paper)
    limit_pct = round(limit_ratio * 100, 1)

    open_eq = book.get("daily_equity_open")
    loss_display_pct: float | None = None
    tripped = False

    if (
        open_eq is not None
        and current_equity is not None
        and float(open_eq) > 0
        and float(current_equity) > 0
    ):
        loss_ratio = _intraday_loss_ratio(float(open_eq), float(current_equity))
        loss_display_pct = round(loss_ratio * 100, 2)
        tripped = loss_ratio >= limit_ratio - 1e-9
        if book.get("circuit_tripped") != tripped:
            book = _clear_circuit_trip(book) if not tripped else book
            if tripped:
                book["circuit_tripped"] = True
                book["loss_pct"] = round(loss_ratio, 6)
            changed = True
    elif book.get("circuit_tripped"):
        tripped = True
        if book.get("loss_pct") is not None:
            loss_display_pct = round(float(book["loss_pct"]) * 100, 2)

    if changed:
        _persist_book(state, key, book)

    return {
        "book": "paper" if paper else "live",
        "limit_pct": limit_pct,
        "open_equity": book.get("daily_equity_open"),
        "current_equity": current_equity,
        "loss_pct": loss_display_pct,
        "tripped": tripped,
        "date": book.get("daily_equity_date"),
    }
