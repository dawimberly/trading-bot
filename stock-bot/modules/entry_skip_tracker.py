"""Per-cycle entry skip reasons — daily rollups for live paper debugging."""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from modules.safe_io import read_json_file, write_json_file

_ET = ZoneInfo("America/New_York")
_STATE_NAME = os.getenv("ENTRY_SKIP_STATE_FILE", "entry_skip_daily.json")

_PAUSE_TOKENS = frozenset({"wisdom_paused", "regime_paused"})


def _state_path() -> str:
    from modules.runtime_paths import resolve_data_root, resolve_runtime_root

    root = resolve_data_root(resolve_runtime_root())
    root.mkdir(parents=True, exist_ok=True)
    return str(root / _STATE_NAME)


def _parse_summary_time_et() -> tuple[int, int]:
    raw = (os.getenv("ENTRY_SKIP_SUMMARY_TIME_ET") or "16:00").strip()
    try:
        hour_s, minute_s = raw.split(":", 1)
        return int(hour_s), int(minute_s)
    except (ValueError, TypeError):
        return 16, 0


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _empty_day(day: str | None = None) -> dict:
    return {
        "date": day or _today_et(),
        "cycles": 0,
        "traded_cycles": 0,
        "by_token": {},
        "by_category": {},
        "last_reason": "",
        "summary_printed": None,
    }


def _load() -> dict:
    raw = read_json_file(_state_path())
    if not isinstance(raw, dict):
        return _empty_day()
    if raw.get("date") != _today_et():
        return _empty_day()
    return raw


def _save(state: dict) -> None:
    write_json_file(_state_path(), state)


def categorize_token(token: str) -> str:
    t = (token or "").strip().lower()
    if not t or t == "traded":
        return "traded"
    if t in _PAUSE_TOKENS:
        return "pause"
    if "no_room" in t:
        return "no_room"
    if t == "yield_gated":
        return "yield"
    if t == "equity_session_closed":
        return "session_closed"
    if "cooldown" in t:
        return "cooldown"
    if t.startswith("crypto_"):
        return "crypto"
    if t in ("no_ma50_candidates", "no_stat_arb_signal", "spy_no_ma200", "signals_ok"):
        return "no_signal"
    return "other"


def bucket_reason(reason: str) -> dict[str, int]:
    """Map one cycle reason string to category counts."""
    out: dict[str, int] = {}
    if not reason or reason == "traded":
        out["traded"] = 1
        return out
    for token in reason.split("|"):
        cat = categorize_token(token)
        out[cat] = out.get(cat, 0) + 1
    return out


def _merge_counts(target: dict[str, int], delta: dict[str, int]) -> None:
    for key, n in delta.items():
        target[key] = int(target.get(key, 0)) + int(n)


def record_cycle(reason: str) -> dict:
    """Increment today's skip stats; return snapshot for heartbeat."""
    state = _load()
    state["cycles"] = int(state.get("cycles", 0)) + 1
    state["last_reason"] = str(reason or "")
    if reason == "traded":
        state["traded_cycles"] = int(state.get("traded_cycles", 0)) + 1

    by_token: dict[str, int] = dict(state.get("by_token") or {})
    if reason and reason != "traded":
        for token in reason.split("|"):
            tok = token.strip()
            if tok:
                by_token[tok] = by_token.get(tok, 0) + 1
    state["by_token"] = by_token

    by_cat: dict[str, int] = dict(state.get("by_category") or {})
    _merge_counts(by_cat, bucket_reason(reason))
    state["by_category"] = by_cat
    _save(state)
    return snapshot_from_state(state)


def snapshot_from_state(state: dict) -> dict:
    cycles = int(state.get("cycles") or 0)
    traded = int(state.get("traded_cycles") or 0)
    by_cat = dict(state.get("by_category") or {})
    skipped = cycles - traded
    return {
        "date": state.get("date"),
        "cycles": cycles,
        "traded_cycles": traded,
        "skipped_cycles": max(0, skipped),
        "by_category": by_cat,
        "by_token": dict(state.get("by_token") or {}),
        "last_reason": state.get("last_reason") or "",
        "top_skip": _top_skip_label(by_cat),
    }


def _top_skip_label(by_cat: dict[str, int]) -> str:
    items = [
        (k, v)
        for k, v in by_cat.items()
        if k != "traded" and int(v) > 0
    ]
    if not items:
        return "—"
    items.sort(key=lambda x: -x[1])
    return f"{items[0][0]} ({items[0][1]})"


def _top_blockers(by_cat: dict, by_token: dict, n: int = 2) -> list[str]:
    """Human-readable top skip blockers for EOD summary."""
    lines: list[str] = []
    cats = sorted(
        (
            (k, int(v))
            for k, v in (by_cat or {}).items()
            if k != "traded" and int(v) > 0
        ),
        key=lambda x: -x[1],
    )[:n]
    if cats:
        lines.append(
            "TOP BLOCKERS: " + " | ".join(f"{k}={v}" for k, v in cats)
        )
    toks = sorted(
        ((k, int(v)) for k, v in (by_token or {}).items() if int(v) > 0),
        key=lambda x: -x[1],
    )[:n]
    if toks:
        lines.append(
            "TOP TOKENS: " + " | ".join(f"{k}={v}" for k, v in toks)
        )
    return lines


def format_daily_summary(state: dict | None = None) -> str:
    snap = snapshot_from_state(state or _load())
    by_cat = snap.get("by_category") or {}
    by_token = snap.get("by_token") or {}
    parts = [
        f"Entry skip EOD ({snap.get('date', '?')})",
        f"cycles={snap.get('cycles', 0)}",
        f"traded={snap.get('traded_cycles', 0)}",
        f"skipped={snap.get('skipped_cycles', 0)}",
    ]
    blocker_lines = _top_blockers(by_cat, by_token, n=2)
    if blocker_lines:
        parts.extend(blocker_lines)
    elif by_cat:
        cat_bits = ", ".join(
            f"{k}={v}" for k, v in sorted(by_cat.items(), key=lambda x: -x[1])
        )
        parts.append(cat_bits)
    top_tokens = sorted(by_token.items(), key=lambda x: -x[1])[:5]
    if top_tokens and not blocker_lines:
        tok_bits = ", ".join(f"{k}={v}" for k, v in top_tokens)
        parts.append(f"detail: {tok_bits}")
    return " | ".join(parts)


def maybe_emit_daily_summary(now: datetime | None = None) -> str | None:
    """Print once per ET day after ENTRY_SKIP_SUMMARY_TIME_ET (default 16:00)."""
    now = now or datetime.now(_ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    else:
        now = now.astimezone(_ET)
    state = _load()
    if int(state.get("cycles") or 0) <= 0:
        return None
    target_h, target_m = _parse_summary_time_et()
    if (now.hour, now.minute) < (target_h, target_m):
        return None
    today = now.date().isoformat()
    if state.get("summary_printed") == today:
        return None
    text = format_daily_summary(state)
    print(f"--- {text} ---")
    state["summary_printed"] = today
    _save(state)
    return text


def accumulate_backtest(reason: str, acc: dict) -> None:
    """In-memory accumulator for backtest skip breakdown."""
    acc["cycles"] = int(acc.get("cycles", 0)) + 1
    if reason == "traded":
        acc["traded_cycles"] = int(acc.get("traded_cycles", 0)) + 1
    by_token: dict[str, int] = acc.setdefault("by_token", {})
    if reason and reason != "traded":
        for token in reason.split("|"):
            tok = token.strip()
            if tok:
                by_token[tok] = by_token.get(tok, 0) + 1
    by_cat: dict[str, int] = acc.setdefault("by_category", {})
    _merge_counts(by_cat, bucket_reason(reason))


def finalize_backtest_accumulator(acc: dict) -> dict:
    cycles = int(acc.get("cycles", 0))
    traded = int(acc.get("traded_cycles", 0))
    return {
        "cycles": cycles,
        "traded_cycles": traded,
        "skipped_cycles": max(0, cycles - traded),
        "by_category": dict(acc.get("by_category") or {}),
        "by_token": dict(acc.get("by_token") or {}),
    }
