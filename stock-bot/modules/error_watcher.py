"""Structured action/error watcher — JSONL logs, Telegram, Cursor fix queue.

Safe when Ollama/Kimi are offline. Never raises into the trading loop.
Also maintains logs/daily_errors_YYYY-MM-DD.md and an optional end-of-day digest.
"""

from __future__ import annotations

import json
import traceback
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

_ROOT = Path(__file__).resolve().parents[1]
_LOG_DIR = _ROOT / "logs"
_DATA_DIR = _ROOT / "data"
_ACTIONS_PATH = _LOG_DIR / "bot_actions.jsonl"
_ERRORS_PATH = _LOG_DIR / "bot_errors.jsonl"
_CURSOR_QUEUE = _LOG_DIR / "cursor_fix_queue.md"
_STATE_PATH = _DATA_DIR / "error_watcher_state.json"
_ET = ZoneInfo("America/New_York")
_TG_COOLDOWN_SEC = 60.0
_TG_NETWORK_COOLDOWN_SEC = 45 * 60.0  # at most one network alert per ~45 min
_last_tg_at: float = 0.0
_last_tg_fingerprint: str = ""
_last_network_log_at: float = 0.0
_NETWORK_LOG_COOLDOWN_SEC = 60.0  # one structured log line / queue entry per minute


def _enabled() -> bool:
    return bool(getattr(config, "ERROR_WATCHER_ENABLED", True))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_et() -> datetime:
    return datetime.now(_ET)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"error_watcher write failed ({path.name}): {exc}")


def _load_state() -> dict[str, Any]:
    try:
        if _STATE_PATH.is_file():
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        tmp.replace(_STATE_PATH)
    except OSError as exc:
        print(f"error_watcher state write failed: {exc}")


def log_action(event: str, **fields: Any) -> None:
    """Append a structured action event (order attempt, cycle, etc.)."""
    if not _enabled():
        return
    row = {
        "ts": _now_iso(),
        "event": event,
        "mode": "paper" if config.PAPER_TRADING else "live",
        **fields,
    }
    _append_jsonl(_ACTIONS_PATH, row)


def _frame_file_line(tb: traceback.StackSummary | None = None) -> str:
    frames = tb or traceback.extract_stack()
    for fr in reversed(frames):
        path = str(fr.filename or "").replace("\\", "/")
        # Skip this module only (not scripts/error_watcher_loop.py).
        if path.endswith("/modules/error_watcher.py"):
            continue
        if path.endswith(".py"):
            name = Path(path).name
            return f"{name}:{fr.lineno}"
    return "unknown:0"


def _suggested_fix_area(exc: BaseException | None, where: str) -> str:
    text = f"{type(exc).__name__ if exc else ''} {where}".lower()
    if "alpaca" in text or "order" in text or "submit" in text:
        return "modules/alpaca_executor.py / modules/alpaca_client.py"
    if "yield" in text or "macro" in text:
        return "modules/macro_signals.py / modules/game_plan.py"
    if "telegram" in text or "alert" in text:
        return "modules/alerts.py / modules/trade_notifier.py"
    if "dust" in text or "concentration" in text:
        return "modules/alpaca_executor.py (portfolio guards)"
    if where and where != "unknown:0":
        return where.split(":")[0]
    return "run_all.py / nearest caller"


def _append_cursor_prompt(
    *,
    problem: str,
    stack: str,
    fix_area: str,
    file_line: str,
    event_id: str,
) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        block = (
            f"\n## Error {event_id} — {_now_iso()}\n\n"
            f"**Problem:** {problem}\n\n"
            f"**Location:** `{file_line}`\n\n"
            f"**Suggested fix area:** `{fix_area}`\n\n"
            f"**Stack:**\n```\n{stack.strip()[:4000]}\n```\n\n"
            f"**Cursor prompt:**\n"
            f"> Investigate and fix: {problem} at {file_line}. "
            f"Focus on {fix_area}. Keep paper/live safety. "
            f"Add a regression test if practical.\n"
        )
        with _CURSOR_QUEUE.open("a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError as exc:
        print(f"error_watcher cursor queue write failed: {exc}")


def classify_error_class(
    error: str | BaseException | None = None,
    *,
    error_type: str | None = None,
    reason: str | None = None,
) -> str:
    """Bucket: transient_network | auth | order_reject | other."""
    if error is not None and not isinstance(error, (str, bytes)):
        try:
            from modules.alpaca_client import (
                AlpacaAuthError,
                AlpacaTransientNetworkError,
                AlpacaValidationError,
                is_auth_alpaca_error,
                is_skippable_order_error,
                is_transient_network_error,
            )

            if isinstance(error, AlpacaTransientNetworkError) or is_transient_network_error(
                error
            ):
                return "transient_network"
            if isinstance(error, AlpacaAuthError) or is_auth_alpaca_error(error):
                return "auth"
            if isinstance(error, AlpacaValidationError) or is_skippable_order_error(error):
                return "order_reject"
        except Exception:
            pass
        text = f"{type(error).__name__}: {error}"
        etype = type(error).__name__
    else:
        text = str(error or "")
        etype = str(error_type or "")
    low = f"{etype} {text} {reason or ''}".lower()
    if any(
        m in low
        for m in (
            "nameresolution",
            "getaddrinfo",
            "failed to resolve",
            "max retries exceeded",
            "winerror 11001",
            "transient_network",
            "connectionerror",
            "timed out",
            "timeout",
        )
    ):
        return "transient_network"
    if any(m in low for m in ("unauthorized", "401", "403", "invalid credentials", "auth")):
        if "insufficient qty" not in low and "asset" not in low:
            return "auth"
    if any(
        m in low
        for m in (
            "422",
            "insufficient qty",
            "order reject",
            "validation",
            "notional",
            "asset not found",
        )
    ):
        return "order_reject"
    return "other"


def _maybe_telegram_error(
    short: str,
    file_line: str,
    fingerprint: str,
    *,
    error_class: str = "other",
) -> None:
    if not getattr(config, "TELEGRAM_ALERT_ERRORS", True):
        return
    if not config.get_telegram_config():
        return
    import time

    global _last_tg_at, _last_tg_fingerprint
    now = time.time()
    cooldown = (
        _TG_NETWORK_COOLDOWN_SEC
        if error_class == "transient_network"
        else _TG_COOLDOWN_SEC
    )
    if fingerprint == _last_tg_fingerprint and now - _last_tg_at < cooldown:
        return
    if error_class == "transient_network" and now - _last_tg_at < cooldown:
        # Separate network floods even if fingerprint differs slightly per cycle.
        if _last_tg_fingerprint.startswith("network:"):
            return
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    if error_class == "transient_network":
        text = (
            f"[PythonTrading {mode}] Network/DNS (transient)\n"
            f"{short[:350]}\n"
            f"DNS/network — not a strategy error. Bot keeps running; orders skipped this cycle.\n"
            f"At: {file_line}"
        )
        subject = f"[PythonTrading {mode}] Network/DNS"
        fp = f"network:{fingerprint}"
    else:
        text = (
            f"[PythonTrading {mode}] Error\n"
            f"{short[:400]}\n"
            f"At: {file_line}\n"
            f"See logs/bot_errors.jsonl + logs/cursor_fix_queue.md"
        )
        subject = f"[PythonTrading {mode}] Error"
        fp = fingerprint
    try:
        from modules.alerts import broadcast

        if broadcast(subject, text, category="error"):
            _last_tg_at = now
            _last_tg_fingerprint = fp
    except Exception as exc:
        print(f"error_watcher telegram failed: {exc}")


def log_exception(
    exc: BaseException,
    *,
    context: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    """Log exception to JSONL + Cursor queue; optional Telegram. Returns event id."""
    event_id = uuid.uuid4().hex[:12]
    if not _enabled():
        return event_id

    error_class = classify_error_class(exc)
    if error_class == "transient_network":
        return log_transient_network(str(exc), context=context or "exception", extra=extra)

    tb_str = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    # Prefer exception traceback frames for file:line
    extracted = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    file_line = _frame_file_line(extracted) if extracted else _frame_file_line()
    problem = f"{type(exc).__name__}: {exc}"
    if context:
        problem = f"{context} — {problem}"
    fix_area = _suggested_fix_area(exc, file_line)

    row = {
        "ts": _now_iso(),
        "id": event_id,
        "event": "exception",
        "mode": "paper" if config.PAPER_TRADING else "live",
        "context": context,
        "error_type": type(exc).__name__,
        "error_class": error_class,
        "error": str(exc)[:1000],
        "file_line": file_line,
        "fix_area": fix_area,
        "stack": tb_str[:8000],
        **(extra or {}),
    }
    _append_jsonl(_ERRORS_PATH, row)
    _append_cursor_prompt(
        problem=problem,
        stack=tb_str,
        fix_area=fix_area,
        file_line=file_line,
        event_id=event_id,
    )
    _maybe_telegram_error(
        problem, file_line, f"{type(exc).__name__}:{file_line}", error_class=error_class
    )
    return event_id


def log_transient_network(
    error: str,
    *,
    context: str = "cycle",
    extra: dict[str, Any] | None = None,
) -> str:
    """Rate-limited DNS/network log — no Cursor spam, soft Telegram."""
    import time

    global _last_network_log_at
    event_id = uuid.uuid4().hex[:12]
    if not _enabled():
        return event_id
    now = time.time()
    skip_queue = now - _last_network_log_at < _NETWORK_LOG_COOLDOWN_SEC
    file_line = _frame_file_line()
    problem = f"TRANSIENT_NETWORK ({context}): {error}"
    row = {
        "ts": _now_iso(),
        "id": event_id,
        "event": "transient_network",
        "mode": "paper" if config.PAPER_TRADING else "live",
        "context": context,
        "error_type": "AlpacaTransientNetworkError",
        "error_class": "transient_network",
        "error": str(error)[:1000],
        "file_line": file_line,
        "fix_area": "modules/alpaca_client.py (DNS/network — not strategy)",
        "stack": "(transient network — no strategy fix)",
        **(extra or {}),
    }
    if not skip_queue:
        _last_network_log_at = now
        _append_jsonl(_ERRORS_PATH, row)
        _append_jsonl(_ACTIONS_PATH, {**row, "event": "network_skip"})
        # Cursor queue: tag clearly so agents do not treat as strategy bugs.
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            block = (
                f"\n## Network {event_id} — {_now_iso()}\n\n"
                f"**Problem:** {problem[:500]}\n\n"
                f"**error_class:** `transient_network`\n\n"
                f"**Note:** DNS/network — not a strategy error. "
                f"Bot skipped orders this cycle and continues.\n\n"
                f"**Location:** `{file_line}`\n"
            )
            with _CURSOR_QUEUE.open("a", encoding="utf-8") as fh:
                fh.write(block)
        except OSError as exc:
            print(f"error_watcher cursor queue write failed: {exc}")
        _maybe_telegram_error(
            problem,
            file_line,
            "transient_network",
            error_class="transient_network",
        )
    return event_id


def log_failed_order(
    *,
    symbol: str,
    side: str,
    reason: str = "",
    error: str = "",
    notional: float | None = None,
    extra: dict[str, Any] | None = None,
    error_class: str | None = None,
) -> str:
    """Record a failed / rejected order attempt."""
    event_id = uuid.uuid4().hex[:12]
    if not _enabled():
        return event_id
    klass = error_class or classify_error_class(error, reason=reason)
    if klass == "transient_network":
        return log_transient_network(
            error or reason or f"{side} {symbol}",
            context="order",
            extra={"symbol": symbol, "side": side, "reason": reason, "notional": notional},
        )
    file_line = _frame_file_line()
    problem = f"Order failed: {side} {symbol}" + (f" — {error}" if error else "")
    fix_area = _suggested_fix_area(None, "alpaca_executor.py")
    row = {
        "ts": _now_iso(),
        "id": event_id,
        "event": "order_failed",
        "mode": "paper" if config.PAPER_TRADING else "live",
        "symbol": symbol,
        "side": side,
        "reason": reason,
        "error": error[:1000],
        "notional": notional,
        "file_line": file_line,
        "fix_area": fix_area,
        "error_type": "OrderFailed",
        "error_class": klass,
        **(extra or {}),
    }
    _append_jsonl(_ERRORS_PATH, row)
    _append_jsonl(_ACTIONS_PATH, {**row, "event": "order_attempt_failed"})
    _append_cursor_prompt(
        problem=problem,
        stack=error or "(no stack)",
        fix_area=fix_area,
        file_line=file_line,
        event_id=event_id,
    )
    _maybe_telegram_error(
        problem, file_line, f"order:{symbol}:{error[:80]}", error_class=klass
    )
    return event_id


def _parse_row_ts(row: dict[str, Any]) -> datetime | None:
    raw = row.get("ts")
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def error_label(row: dict[str, Any]) -> str:
    """Human bucket for digests (ImportError, insufficient qty, …)."""
    klass = str(row.get("error_class") or "").strip()
    if klass == "transient_network":
        return "transient_network"
    if klass == "auth":
        return "auth"
    if klass == "order_reject":
        return "order_reject"
    err = str(row.get("error") or "")
    etype = str(row.get("error_type") or "").strip()
    low = err.lower()
    if "insufficient qty" in low or "insufficient quantity" in low:
        return "insufficient qty"
    if "importerror" in low or etype == "ImportError":
        return "ImportError"
    if row.get("event") == "order_failed":
        if etype and etype != "OrderFailed":
            return etype
        reason = str(row.get("reason") or "").strip()
        if reason:
            return f"order_failed:{reason}"[:40]
        return "order_failed"
    if etype:
        return etype
    return str(row.get("event") or "error")


def daily_errors_path(day: date | None = None) -> Path:
    d = day or _now_et().date()
    return _LOG_DIR / f"daily_errors_{d.isoformat()}.md"


def load_errors_for_et_date(
    day: date | None = None,
    *,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return bot_errors.jsonl rows whose ts falls on the given ET calendar day."""
    target = day or _now_et().date()
    src = path or _ERRORS_PATH
    out: list[dict[str, Any]] = []
    if not src.is_file():
        return out
    try:
        with src.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                ts = _parse_row_ts(row)
                if ts is None:
                    continue
                if ts.astimezone(_ET).date() == target:
                    out.append(row)
    except OSError as exc:
        print(f"error_watcher read failed ({src.name}): {exc}")
    return out


def summarize_errors(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = Counter(error_label(r) for r in rows)
    top = labels.most_common(10)
    return {
        "count": len(rows),
        "unique_types": dict(top),
        "top3": top[:3],
    }


def _cursor_prompt_for_issue(label: str, sample: dict[str, Any], count: int) -> str:
    file_line = str(sample.get("file_line") or "unknown:0")
    fix_area = str(sample.get("fix_area") or "run_all.py / nearest caller")
    msg = str(sample.get("error") or sample.get("context") or label)[:240]
    sym = sample.get("symbol")
    sym_bit = f" symbol={sym}" if sym else ""
    return (
        f"> Investigate and fix top issue **{label}** (x{count}){sym_bit}: {msg} "
        f"at `{file_line}`. Focus on `{fix_area}`. "
        f"Keep paper/live safety. Add a regression test if practical."
    )


def format_daily_errors_markdown(
    rows: list[dict[str, Any]],
    *,
    day: date | None = None,
) -> str:
    """Build the daily_errors_YYYY-MM-DD.md body."""
    d = day or _now_et().date()
    stats = summarize_errors(rows)
    mode = "paper" if config.PAPER_TRADING else "live"
    lines: list[str] = [
        f"# Daily errors — {d.isoformat()} ({mode})",
        "",
        f"Generated: {_now_et():%Y-%m-%d %H:%M:%S} ET",
        "",
        f"**Count today:** {stats['count']}",
        "",
        "## Unique error types",
        "",
    ]
    if not stats["unique_types"]:
        lines.append("_None._")
    else:
        for label, n in stats["unique_types"].items():
            lines.append(f"- `{label}` × {n}")
    lines.extend(["", "## Last 10 entries", ""])
    last10 = rows[-10:]
    if not last10:
        lines.append("_No errors logged today._")
    else:
        lines.append("| id | time (ET) | symbol | message |")
        lines.append("|---|---|---|---|")
        for row in last10:
            ts = _parse_row_ts(row)
            t_et = ts.astimezone(_ET).strftime("%H:%M:%S") if ts else "?"
            eid = str(row.get("id") or "")[:12]
            sym = str(row.get("symbol") or "—")
            msg = str(row.get("error") or row.get("context") or row.get("event") or "")
            msg = msg.replace("|", "/").replace("\n", " ")[:120]
            lines.append(f"| `{eid}` | {t_et} | {sym} | {msg} |")

    lines.extend(["", "## Cursor prompts (top 3)", ""])
    if not stats["top3"]:
        lines.append("_No issues to paste._")
    else:
        # Newest sample per label for context.
        by_label: dict[str, dict[str, Any]] = {}
        for row in rows:
            by_label[error_label(row)] = row
        for i, (label, n) in enumerate(stats["top3"], start=1):
            sample = by_label.get(label) or {}
            lines.append(f"### {i}. {label} (x{n})")
            lines.append("")
            lines.append(_cursor_prompt_for_issue(label, sample, n))
            lines.append("")
    lines.append("")
    lines.append(
        f"_Source: `{_ERRORS_PATH.name}` · also see `cursor_fix_queue.md`_"
    )
    lines.append("")
    return "\n".join(lines)


def write_daily_error_summary(
    *,
    day: date | None = None,
    errors_path: Path | None = None,
    out_path: Path | None = None,
) -> Path | None:
    """Rewrite logs/daily_errors_YYYY-MM-DD.md for the ET day. Safe no-op if disabled."""
    if not _enabled():
        return None
    try:
        d = day or _now_et().date()
        rows = load_errors_for_et_date(d, path=errors_path)
        text = format_daily_errors_markdown(rows, day=d)
        dest = out_path or daily_errors_path(d)
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        return dest
    except Exception as exc:
        print(f"error_watcher daily summary write failed: {exc}")
        return None


def format_daily_digest_message(rows: list[dict[str, Any]]) -> str:
    stats = summarize_errors(rows)
    n = stats["count"]
    if n <= 0:
        return "Today's errors: 0"
    top_bits = []
    for label, c in stats["top3"]:
        top_bits.append(f"{label} (x{c})")
    top_s = ", ".join(top_bits) if top_bits else "—"
    return f"Today's errors: {n} | top: {top_s}"


def _parse_digest_time_et() -> tuple[int, int]:
    raw = (
        getattr(config, "TELEGRAM_DAILY_ERROR_DIGEST_TIME", None) or "16:45"
    ).strip()
    try:
        hour_s, minute_s = raw.split(":", 1)
        return int(hour_s), int(minute_s)
    except (ValueError, TypeError):
        return 16, 45


def daily_error_digest_due(*, now_et: datetime | None = None) -> bool:
    """True once per ET day after TELEGRAM_DAILY_ERROR_DIGEST_TIME when flag is on."""
    if not getattr(config, "TELEGRAM_DAILY_ERROR_DIGEST", False):
        return False
    now = now_et or _now_et()
    th, tm = _parse_digest_time_et()
    if (now.hour, now.minute) < (th, tm):
        return False
    today = now.date().isoformat()
    state = _load_state()
    return state.get("last_daily_error_digest") != today


def maybe_send_daily_error_digest(
    *,
    force: bool = False,
    now_et: datetime | None = None,
    errors_path: Path | None = None,
) -> bool:
    """Send one Telegram digest after market close / digest time if N > 0."""
    if not _enabled():
        return False
    now = now_et or _now_et()
    if not force and not daily_error_digest_due(now_et=now):
        return False
    if not force and not getattr(config, "TELEGRAM_DAILY_ERROR_DIGEST", False):
        return False
    try:
        rows = load_errors_for_et_date(now.date(), path=errors_path)
        if not rows:
            # Still latch the day so we don't re-check forever with empty reads.
            state = _load_state()
            state["last_daily_error_digest"] = now.date().isoformat()
            _save_state(state)
            return False
        body = format_daily_digest_message(rows)
        mode = "PAPER" if config.PAPER_TRADING else "LIVE"
        subject = f"[PythonTrading {mode}] Daily error digest"
        from modules.alerts import broadcast

        ok = broadcast(subject, body, category="daily_error_digest")
        if ok or force:
            state = _load_state()
            state["last_daily_error_digest"] = now.date().isoformat()
            _save_state(state)
        return bool(ok)
    except Exception as exc:
        print(f"error_watcher daily digest failed: {exc}")
        return False


def cycle_tick(*, cycle: int | None = None, equity: float | None = None) -> None:
    """Per-cycle heartbeat + refresh daily error markdown + optional digest."""
    log_action("cycle", cycle=cycle, equity=equity)
    try:
        write_daily_error_summary()
    except Exception:
        pass
    try:
        maybe_send_daily_error_digest()
    except Exception:
        pass


def watcher_banner() -> str:
    on = "ON" if _enabled() else "OFF"
    tg = "ON" if getattr(config, "TELEGRAM_ALERT_ERRORS", False) else "OFF"
    digest = (
        "ON"
        if getattr(config, "TELEGRAM_DAILY_ERROR_DIGEST", False)
        else "OFF"
    )
    return (
        f"Error watcher={on} | daily log + TG per error={tg} | daily digest={digest}"
    )
