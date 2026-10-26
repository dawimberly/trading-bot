"""Observational NYSE tape. No broker calls, environment dumps, or strategy changes."""
import contextvars
import functools
import json
import logging
import math
import os
from pathlib import Path
import threading
import uuid
from datetime import datetime, timezone

_log = logging.getLogger(__name__)
_active = contextvars.ContextVar("nyse_decision_tape", default=None)
_lock = threading.Lock()
_LIMIT = 512 * 1024 * 1024
_BOOKS = frozenset({"alpaca_live", "alpaca_paper_v2", "alpaca_paper"})


def _root() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "decision_tape"


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _mark_failed(state):
    try:
        if isinstance(state, dict):
            state["failed"] = True
    except Exception:
        return


def _log_incomplete(exc):
    try:
        _log.error("NYSE DECISION TAPE INCOMPLETE: %s", type(exc).__name__)
    except Exception:
        return


def _write_blob(path: Path, blob: str) -> None:
    data = blob.encode("utf8")
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        size = path.stat().st_size if path.exists() else 0
        if size + len(data) > _LIMIT:
            raise OSError("daily tape size limit reached")
        with path.open("a", encoding="utf8") as handle:
            handle.write(blob)
            handle.flush()


def emit(event, **fields):
    state = _active.get()
    if state is None:
        return
    try:
        payload = dict(
            schema=1,
            decision_id=state["id"],
            book=state["book"],
            observed_at=datetime.now(timezone.utc).isoformat(),
            event=event,
            **fields,
        )
        encoded = json.dumps(_clean(payload), allow_nan=False, separators=(",", ":")) + "\n"
        buffer = state.get("buffer") if isinstance(state, dict) else None
        if isinstance(buffer, list):
            buffer.append(encoded)
        else:
            _write_blob(state["path"], encoded)
        state["events"] = int(state.get("events") or 0) + 1
    except Exception as exc:
        _mark_failed(state)
        _log_incomplete(exc)


def _flush(state) -> None:
    if not isinstance(state, dict):
        return
    buffer = state.get("buffer")
    if not isinstance(buffer, list) or not buffer:
        return
    try:
        _write_blob(state["path"], "".join(buffer))
        buffer.clear()
    except Exception as exc:
        _mark_failed(state)
        _log_incomplete(exc)


def audited(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        # Historical simulations are excluded. Portal book identity is mandatory.
        try:
            book = os.environ.get("TRADING_BOOK_ID", "")
            record = book in _BOOKS and kwargs.get("bar_idx") is None
        except Exception:
            return fn(*args, **kwargs)
        if not record:
            return fn(*args, **kwargs)
        day = datetime.now(timezone.utc).date().isoformat()
        state = dict(
            id=uuid.uuid4().hex,
            book=book,
            path=_root() / book / (day + ".jsonl"),
            events=0,
            failed=False,
            buffer=[],
        )
        try:
            token = _active.set(state)
        except Exception:
            return fn(*args, **kwargs)
        raised = False
        result = None
        try:
            emit(
                "cycle_start",
                decision_time=args[3] if len(args) > 3 else kwargs.get("now"),
                regime=args[2] if len(args) > 2 else kwargs.get("regime"),
                external_inputs_complete=False,
                external_input_asof={"volume": None, "headlines": None, "filings": None},
                limitation=(
                    "External source timestamps/payloads not captured; "
                    "paper boost replay is incomplete"
                ),
            )
            result = fn(*args, **kwargs)
            return result
        except Exception:
            raised = True
            raise
        finally:
            try:
                emit(
                    "cycle_end",
                    trades=None if raised else result,
                    strategy_error=raised,
                    incomplete=bool(state.get("failed") or raised),
                )
            except Exception as exc:
                _log_incomplete(exc)
            _flush(state)
            try:
                _active.reset(token)
            except Exception:
                pass

    return wrapped


def candidate(symbol, prices, window, eligible, reason=None):
    if _active.get() is None:
        return
    try:
        used = prices.tail(window)
        average = float(used.mean()) if len(used) else None
        last = float(used.iloc[-1]) if len(used) else None
        emit(
            "candidate",
            symbol=symbol,
            window=window,
            eligible=eligible,
            reason=reason,
            base_score=(last / average - 1) if average and last is not None else None,
            closes=[[str(t), float(v)] for t, v in used.items()],
            mtf_input_kind="five_minute_matrix",
        )
    except Exception as exc:
        emit("capture_error", symbol=symbol, error_type=type(exc).__name__)


def boost(component, symbol, value):
    # Return the original value unchanged; never recompute a scanner.
    try:
        emit(
            "boost",
            component=component,
            symbol=symbol,
            applied_value=value,
            source_asof=None,
        )
    except Exception as exc:
        _log_incomplete(exc)
    return value


def describe_order(order):
    """Id and status only. An order object is not a confirmed fill."""
    try:
        if isinstance(order, dict):
            order_id = order.get("id")
            if order_id is None:
                order_id = order.get("order_id")
            status = order.get("status")
            queued = bool(order.get("queued"))
        else:
            order_id = getattr(order, "id", None)
            status = getattr(order, "status", None)
            queued = bool(getattr(order, "queued", False))
        return {
            "order_id": None if order_id is None else str(order_id),
            "status": None if status is None else str(status),
            "queued": queued,
        }
    except Exception:
        return {"order_id": None, "status": None, "queued": None, "snapshot_error": True}


def note_returned(symbol, order):
    try:
        emit(
            "order_returned",
            symbol=symbol,
            confirmed_fill=False,
            **describe_order(order),
        )
    except Exception as exc:
        _log_incomplete(exc)


def note_unfilled(symbol, order, executor):
    """Deferred and unconfirmed returns are not fills and not entry rejections."""
    try:
        fields = describe_order(order)
        deferred = bool(fields.get("queued")) and hasattr(executor, "defer_until_fill")
        emit(
            "order_deferred" if deferred else "order_unconfirmed",
            symbol=symbol,
            confirmed_fill=False,
            **fields,
        )
    except Exception as exc:
        _log_incomplete(exc)


def note_fill_callback(symbol, reason, executor):
    try:
        has_check = hasattr(executor, "order_filled")
        emit(
            "fill_callback",
            symbol=symbol,
            reason=reason,
            confirmed_fill=bool(has_check),
            fill_basis=(
                "order_filled_method"
                if has_check
                else "legacy_executor_counts_return_as_fill"
            ),
        )
    except Exception as exc:
        _log_incomplete(exc)


def band(score):
    if score < -0.01:
        return "outside"
    if score < 0:
        return "B0"
    if score < 0.01:
        return "B1"
    if score < 0.03:
        return "B2"
    if score <= 0.06:
        return "B3"
    return "B4"
