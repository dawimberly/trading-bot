"""Reconcile exit orders before they hit the broker.

Exits were resubmitted every cycle while a prior sell/stop still held the
shares, and again for leftovers Alpaca will not accept (sub-$1 notional,
non-fractionable fractional qty). Decisions here are broker constraints:
they do not change stop levels, signals, or target size.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

# Alpaca's hard minimum. Strategy floors can be higher; this is only the
# broker reject line for leftovers we must stop retrying.
DEFAULT_BROKER_MIN_NOTIONAL = 1.0

_WORKING_BAD = ("cancel", "reject", "expire", "fail")
_CODE_TYPES = (AttributeError, TypeError, KeyError, IndexError, RuntimeError, ValueError)


@dataclass
class ExitPlan:
    action: str
    qty: float = 0.0
    note: str = ""
    existing: Any = None
    cancel_ids: list = field(default_factory=list)


class ExitRetryBook:
    """Per-symbol backoff. Code bugs and unsellable dust lock; broker holds retry."""

    def __init__(
        self,
        now=None,
        max_attempts: int = 3,
        base_delay: float = 60.0,
        max_delay: float = 900.0,
    ):
        self._now = now or time.time
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)
        self.state: dict[str, dict] = {}

    def now(self) -> float:
        return float(self._now())

    def allow(self, symbol: str, fingerprint: str) -> bool:
        st = self.state.get(symbol)
        if not st:
            return True
        if st.get("lock_symbol"):
            return False
        if st.get("fingerprint") != fingerprint:
            self.state.pop(symbol, None)
            return True
        if st.get("terminal"):
            return False
        if self.now() < float(st.get("next_ok") or 0):
            return False
        return True

    def record(
        self,
        symbol: str,
        note: str,
        *,
        fingerprint: str,
        terminal: bool = False,
        lock_symbol: bool = False,
    ) -> None:
        st = self.state.get(symbol)
        if st is None or (st.get("fingerprint") != fingerprint and not st.get("lock_symbol")):
            st = {"attempts": 0, "lock_symbol": False, "terminal": False}
        st["attempts"] = int(st.get("attempts") or 0) + 1
        st["fingerprint"] = fingerprint
        st["note"] = note
        if lock_symbol:
            st["lock_symbol"] = True
            st["terminal"] = True
            st["next_ok"] = self.now() + 10**12
        elif terminal:
            st["terminal"] = True
            st["lock_symbol"] = False
            st["next_ok"] = self.now() + 10**12
        else:
            delay = min(self.max_delay, self.base_delay * (2 ** max(0, st["attempts"] - 1)))
            st["next_ok"] = self.now() + delay
            st["terminal"] = st["attempts"] >= self.max_attempts
            st["lock_symbol"] = False
            if st["terminal"]:
                st["next_ok"] = self.now() + 10**12
        self.state[symbol] = st

    def clear(self, symbol: str) -> None:
        self.state.pop(symbol, None)


def exit_fingerprint(
    position_qty: float,
    available_qty: float,
    price: float,
    min_notional: float = DEFAULT_BROKER_MIN_NOTIONAL,
) -> str:
    """Stable while the unsellable leftover is unchanged. A real size change retries."""
    pos_abs = abs(float(position_qty or 0))
    avail_abs = abs(float(available_qty or 0))
    try:
        px = float(price or 0)
    except (TypeError, ValueError):
        px = 0.0
    dust = "d" if pos_abs * px < float(min_notional) else "o"
    return f"{pos_abs:.4f}|{avail_abs:.4f}|{dust}"


def _order_attr(order, name: str) -> str:
    raw = getattr(order, name, "") or ""
    return str(raw).split(".")[-1].strip().lower()


def order_remaining_qty(order) -> float:
    try:
        qty = float(getattr(order, "qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        filled = float(getattr(order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        filled = 0.0
    if qty <= 0:
        return 0.0
    return max(0.0, qty - filled)


def _is_working(order) -> bool:
    status = _order_attr(order, "status")
    if not status:
        return True
    if "partial" in status:
        return True
    if status == "filled":
        return False
    return not any(bad in status for bad in _WORKING_BAD)


def _add_note(parts: list[str], seen: set[str], text) -> None:
    if text is None:
        return
    cleaned = " ".join(str(text).split())
    if not cleaned or cleaned.lower() in {"none", "null"}:
        return
    if cleaned in seen:
        return
    seen.add(cleaned)
    parts.append(cleaned)


def _note_from_exception(exc: BaseException, parts: list[str], seen: set[str]) -> None:
    raw = getattr(exc, "_error", None)
    parsed = None
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.strip():
        if raw.strip().startswith("{") or raw.strip().startswith("["):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                _add_note(parts, seen, raw)
        else:
            _add_note(parts, seen, raw)
    if isinstance(parsed, dict):
        _add_note(parts, seen, parsed.get("message") or parsed.get("error"))
        code = parsed.get("code")
        if code is not None:
            _add_note(parts, seen, f"code={code}")
    else:
        for attr in ("message", "code"):
            try:
                _add_note(parts, seen, getattr(exc, attr))
            except Exception:
                pass

    status = None
    try:
        status = getattr(exc, "status_code", None)
    except Exception:
        status = None
    http = getattr(exc, "_http_error", None)
    resp = getattr(http, "response", None) if http is not None else None
    if resp is None:
        try:
            resp = getattr(exc, "response", None)
        except Exception:
            resp = None
    if status is None and resp is not None:
        status = getattr(resp, "status_code", None)
    reason = getattr(resp, "reason", None) if resp is not None else None
    if status:
        _add_note(parts, seen, f"HTTP {status}" + (f" {reason}" if reason else ""))
    if resp is not None:
        body = getattr(resp, "text", None)
        if isinstance(body, str) and body.strip() and not body.strip().startswith("{"):
            _add_note(parts, seen, body[:300])

    try:
        text = str(exc).strip()
    except Exception:
        text = ""
    if text and not (text.startswith("{") and isinstance(parsed, dict)):
        _add_note(parts, seen, text)


def broker_error_note(exc: BaseException) -> str:
    """Journal text for a broker or code failure. Never empty."""
    if exc is None:
        return "exit failed (no exception)"
    note = getattr(exc, "note", None)
    if note and type(exc).__name__ == "ExitOrderBlocked":
        return str(note)[:500]
    parts: list[str] = []
    seen: set[str] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and depth < 4:
        _note_from_exception(current, parts, seen)
        current = current.__cause__
        depth += 1
    if not parts:
        _add_note(parts, seen, type(exc).__name__)
    return " | ".join(parts)[:500]


def classify_failure(note: str, exc: BaseException | None = None) -> str:
    """held | dust | fractional | code | retry."""
    text = (note or "").lower()
    if "held_for_order" in text or "insufficient qty" in text or "qty available" in text:
        return "held"
    if (
        "not fractionable" in text
        or "not_fractionable" in text
        or "no whole shares" in text
    ):
        return "fractional"
    if "leaving dust" in text or "below alpaca" in text:
        return "dust"
    if ("notional" in text or "cost basis" in text) and any(
        tok in text for tok in ("minimum", "must be", ">=", "at least", "below alpaca")
    ):
        return "dust"
    if exc is not None:
        name = type(exc).__name__
        if name.startswith("Alpaca") or name in {"APIError"}:
            return "retry"
        if isinstance(exc, _CODE_TYPES):
            return "code"
    return "retry"


def plan_exit_submission(
    *,
    position_qty: float,
    available_qty: float,
    requested_qty: float,
    price: float,
    open_orders: list,
    fractionable: bool | None,
    min_notional: float = DEFAULT_BROKER_MIN_NOTIONAL,
) -> ExitPlan:
    """Decide whether to submit, skip a working exit, or give up on unsellable qty.

    ``requested_qty`` is the strategy size (positive shares). The plan never
    exceeds shares the broker currently has free.
    """
    pos_abs = abs(float(position_qty or 0))
    avail_abs = abs(float(available_qty or 0))
    try:
        req = abs(float(requested_qty or 0))
    except (TypeError, ValueError):
        req = 0.0
    if req <= 0:
        req = pos_abs
    try:
        px = float(price or 0)
    except (TypeError, ValueError):
        px = 0.0
    close_side = "sell" if float(position_qty or 0) > 0 else "buy"
    target = min(req, pos_abs) if pos_abs > 0 else req

    working = []
    for order in open_orders or []:
        if _order_attr(order, "side") != close_side:
            continue
        if not _is_working(order):
            continue
        working.append(order)

    cover = 0.0
    for order in working:
        rem = order_remaining_qty(order)
        if rem <= 0:
            rem = pos_abs
        cover += rem

    tol = 1e-4
    if working and cover + tol >= target > 0:
        best = max(working, key=lambda o: order_remaining_qty(o) or pos_abs)
        return ExitPlan(
            "skip_equivalent",
            qty=0.0,
            note="equivalent exit already working",
            existing=best,
        )

    need = max(0.0, target - cover) if working else target
    send = min(avail_abs, need)

    if fractionable is False:
        whole = int(send + 1e-9)
        if whole < 1:
            if working and avail_abs < 1:
                return ExitPlan(
                    "skip_held",
                    note="shares held for open orders; not fractionable remainder",
                )
            return ExitPlan(
                "skip_unsellable",
                note="not fractionable; no whole shares available",
            )
        send = float(whole)

    if send <= 0 or px <= 0:
        if working and cover + tol < target:
            ids = [getattr(o, "id", None) for o in working if getattr(o, "id", None)]
            return ExitPlan(
                "skip_held",
                note="shares held for open orders; not resubmitting",
                cancel_ids=ids,
            )
        if avail_abs + tol < target or working:
            return ExitPlan(
                "skip_held",
                note="shares held for open orders; not resubmitting",
            )
        return ExitPlan("skip_dust", note="no sellable quantity")

    notional = send * px
    if notional + 1e-9 < float(min_notional):
        if pos_abs * px + 1e-9 < float(min_notional):
            return ExitPlan(
                "skip_dust",
                note=(
                    f"below alpaca ${float(min_notional):.2f} minimum "
                    f"(notional ${pos_abs * px:.2f}); leaving dust"
                ),
            )
        return ExitPlan(
            "skip_held",
            note=(
                f"available notional ${notional:.2f} below alpaca "
                f"${float(min_notional):.2f} minimum; shares held or too small to sell"
            ),
        )

    return ExitPlan("submit", qty=send)
