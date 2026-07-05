#!/usr/bin/env python3
"""Autonomous overnight paper-bot startup + 9:00 AM ET pre-market Telegram summary.

Paper book (alpaca_paper) only — does not start or stop the live bot.

Run from stock-bot/:
  python scripts/autostart_paper_bot.py
  python scripts/autostart_paper_bot.py --send-now
  python scripts/autostart_paper_bot.py --no-telegram

Double-click: Start_Autonomous.bat (pythonw, logs/autostart_paper.log)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONTRADING_ROOT", str(ROOT))
_ET = ZoneInfo("America/New_York")
_STATE_PATH = ROOT / "data" / "autostart_paper_state.json"
_LOG_PATH = ROOT / "logs" / "autostart_paper.log"
_REPORT_DIR = ROOT / "reports" / "premarket"
_HEARTBEAT_WAIT_SEC = 75
_HEARTBEAT_EXTRA_POLL_SEC = 120
_HEARTBEAT_POLL_SEC = 5
_PREMARKET_HOUR = 9
_PREMARKET_MINUTE = 0


def _log(msg: str) -> None:
    ts = datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _default_username() -> str:
    prefs = ROOT / "data" / "portal" / "desktop_prefs.json"
    if prefs.is_file():
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
            name = str(data.get("last_username") or "").strip().lower()
            if name:
                return name
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return "dawimberly"


def _load_state() -> dict:
    if not _STATE_PATH.is_file():
        return {}
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _load_heartbeat(username: str) -> dict | None:
    from modules.portal_paths import book_heartbeat_path

    path = book_heartbeat_path(username, "alpaca_paper")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _heartbeat_age_sec(hb: dict | None) -> float | None:
    if not hb or not hb.get("timestamp"):
        return None
    try:
        ts = datetime.fromisoformat(str(hb["timestamp"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_ET)
        else:
            ts = ts.astimezone(_ET)
        return max(0.0, (datetime.now(_ET) - ts).total_seconds())
    except (TypeError, ValueError):
        return None


def wait_for_heartbeat(
    username: str,
    *,
    min_wait: int = _HEARTBEAT_WAIT_SEC,
    extra_poll: int = _HEARTBEAT_EXTRA_POLL_SEC,
    restart_after: datetime | None = None,
) -> dict | None:
    """Wait min_wait seconds, then poll for a fresh paper heartbeat."""
    _log(f"Waiting {min_wait}s for heartbeats and first cycle...")
    time.sleep(min_wait)
    _log(f"Polling up to {extra_poll}s for fresh paper heartbeat...")
    deadline = time.monotonic() + extra_poll
    last: dict | None = None

    while time.monotonic() < deadline:
        hb = _load_heartbeat(username)
        if hb:
            last = hb
            if restart_after and not _heartbeat_after(hb, restart_after):
                time.sleep(_HEARTBEAT_POLL_SEC)
                continue
            age = _heartbeat_age_sec(hb)
            if age is not None and age <= 180 and hb.get("equity") is not None:
                _log(
                    f"Heartbeat OK (age {age:.0f}s, equity ${float(hb.get('equity') or 0):,.2f})"
                )
                return hb
        time.sleep(_HEARTBEAT_POLL_SEC)

    if last:
        age = _heartbeat_age_sec(last)
        _log(f"Heartbeat timeout — using last snapshot (age {age:.0f}s if known)")
        return last
    _log("No heartbeat received within timeout.")
    return None


def _heartbeat_after(hb: dict, cutoff: datetime) -> bool:
    if not hb.get("timestamp"):
        return False
    try:
        ts = datetime.fromisoformat(str(hb["timestamp"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_ET)
        else:
            ts = ts.astimezone(_ET)
        return ts >= cutoff.astimezone(_ET)
    except (TypeError, ValueError):
        return False


def run_insider_verification() -> tuple[int, str]:
    script = ROOT / "scripts" / "verify_insider_integration.py"
    _log(f"Running {script.name}...")
    proc = subprocess.run(
        [sys.executable, "-u", str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    status = "PASSED" if proc.returncode == 0 else f"FAILED (exit {proc.returncode})"
    _log(f"Insider verification {status}")
    _print_verify_summary(output.strip())
    return proc.returncode, output.strip()


def _print_verify_summary(output: str) -> None:
    if not output:
        return
    marker = "=== Summary ==="
    if marker in output:
        summary = output[output.index(marker) :].strip()
    else:
        lines = [ln for ln in output.splitlines() if ln.strip()]
        summary = "\n".join(lines[-6:])
    _log("--- Insider verification summary ---")
    for line in summary.splitlines():
        _log(line)


def _health_indicator(color: str) -> str:
    return {"green": "Green", "yellow": "Yellow", "red": "Red"}.get(color or "", "—")


def _top_insider_lines(*, limit: int = 3) -> list[str]:
    from modules.insider_monitor import _format_value, _sig_type, get_recent_insider_signals

    lines: list[str] = []
    for sig in get_recent_insider_signals(days=7, min_score=60)[:limit]:
        tk = sig.get("ticker") or sig.get("company") or "?"
        st = _sig_type(sig)
        score = int(sig.get("score") or 0)
        val = _format_value(sig.get("value"))
        val_s = f" {val}" if val else ""
        desc = str(sig.get("description") or "")[:55]
        lines.append(f"• {tk} | {st} | s{score}{val_s} — {desc}")
    return lines


def _short_unrealized_pnl(hb: dict | None) -> float | None:
    sp = (hb or {}).get("sleeve_pnl") or {}
    row = sp.get("SHORT") or sp.get("short") or {}
    if not row:
        return None
    try:
        return float(row.get("unrealized_pnl"))
    except (TypeError, ValueError):
        return None


def _short_status_line(hb: dict | None) -> str:
    from modules.short_activity import gather_short_activity, format_short_activity_status

    regime = str((hb or {}).get("regime") or "")
    snap = gather_short_activity(regime=regime)
    if not snap.get("enabled"):
        return "Protective shorts: OFF (paper only)"
    detail = format_short_activity_status(snap)
    unreal = _short_unrealized_pnl(hb)
    if unreal is not None:
        detail = f"{detail} · uPnL ${unreal:+,.2f}"
    banner = snap.get("banner") or ""
    if banner:
        return f"{banner} | {detail}"
    return detail


def _short_telegram_block(hb: dict | None) -> str:
    from modules.short_activity import format_shorts_telegram_block

    regime = str((hb or {}).get("regime") or "")
    equity = float((hb or {}).get("equity") or 0) or None
    block = format_shorts_telegram_block(regime=regime, equity=equity)
    unreal = _short_unrealized_pnl(hb)
    if unreal is not None and block:
        block = f"{block}\nUnrealized PnL: ${unreal:+,.2f}"
    return block


def _bubble_line(hb: dict | None) -> str:
    bubble_100: float | None = None
    buffett: str = ""
    try:
        from modules.insider_signal_handler import get_boost_snapshot

        ins = get_boost_snapshot()
        b = ins.get("bubble_score_100")
        if b is not None:
            bubble_100 = float(b)
    except Exception:
        pass
    if bubble_100 is None:
        try:
            from modules.bubble_risk import compute_bubble_risk_from_live_context

            regime = str((hb or {}).get("regime") or "")
            ctx = compute_bubble_risk_from_live_context(regime=regime, hb=hb)
            if ctx:
                bubble_100 = float(ctx.get("score_100") or 0)
                bi = ctx.get("buffett") or {}
                if bi.get("ratio_pct") is not None:
                    buffett = f" | Buffett {float(bi['ratio_pct']):.0f}% GDP"
        except Exception:
            pass
    if bubble_100 is None:
        return "Bubble: n/a"
    return f"Bubble Risk: {bubble_100:.0f}/100{buffett}"


def build_premarket_snapshot(hb: dict | None) -> dict:
    from modules.bot_health import calculate_health_score, gather_health_context

    ctx = gather_health_context(hb)
    health = calculate_health_score(**ctx)
    regime = str((hb or {}).get("regime") or "—")
    equity = float((hb or {}).get("equity") or 0)
    return {
        "health": health,
        "regime": regime,
        "equity": equity,
        "insider_lines": _top_insider_lines(limit=3),
        "short_status": _short_status_line(hb),
        "short_telegram": _short_telegram_block(hb),
        "bubble_line": _bubble_line(hb),
        "heartbeat_age_sec": _heartbeat_age_sec(hb),
    }


def format_premarket_report_text(
    snap: dict,
    *,
    verify_rc: int,
    verify_output: str,
    username: str,
    start_msg: str,
) -> str:
    import config

    health = snap.get("health") or {}
    score = health.get("score", "n/a")
    grade = health.get("grade", "n/a")
    color = health.get("color", "")
    now = datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S ET")

    lines = [
        f"Pre-Market Report — {now}",
        f"User: {username} | Book: alpaca_paper | RR v{config.REALISTIC_RESEARCH_VERSION}",
        "",
        "=== Startup ===",
        start_msg,
        f"Heartbeat age: {snap.get('heartbeat_age_sec')}s",
        f"Equity: ${float(snap.get('equity') or 0):,.2f}",
        "",
        "=== Bot Health ===",
        f"Score: {score}/100 ({grade}) — {_health_indicator(color)}",
    ]
    for note in health.get("notes") or []:
        lines.append(f"  • {note}")

    lines.extend(["", "=== Regime & Bubble ===", f"Regime: {snap.get('regime')}", snap.get("bubble_line", "")])

    lines.extend(["", "=== Insider Signals (top 3) ==="])
    insider = snap.get("insider_lines") or []
    lines.extend(insider if insider else ["  (none this week)"])

    lines.extend(["", "=== Short Activity ===", snap.get("short_status") or "—"])

    lines.extend(["", "=== Insider Verification ===", f"Exit code: {verify_rc}"])
    if verify_output:
        lines.append(verify_output)

    lines.extend(["", "Ready for market open."])
    return "\n".join(lines)


def write_premarket_report(text: str) -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    day = _today_et()
    path = _REPORT_DIR / f"{day}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    _log(f"Pre-market report saved: {path}")
    return path


def format_premarket_telegram(snap: dict) -> str:
    import config

    health = snap.get("health") or {}
    score = int(health.get("score") or 0)
    grade = str(health.get("grade") or "")
    color = str(health.get("color") or "yellow")
    indicator = _health_indicator(color)

    lines = [
        f"Pre-Market · RR v{config.REALISTIC_RESEARCH_VERSION}",
        f"{datetime.now(_ET).strftime('%A %b %d · %I:%M %p ET').lstrip('0')}",
        "",
        f"Bot Health: {score}/100 ({grade}) — {indicator}",
    ]
    for note in (health.get("notes") or [])[:2]:
        lines.append(f"  {note}")

    lines.append("")
    lines.append("Insider (top 3):")
    insider = snap.get("insider_lines") or []
    if insider:
        lines.extend(insider)
    else:
        lines.append("  (none this week)")

    short_block = snap.get("short_telegram") or snap.get("short_status") or "—"
    lines.extend(["", short_block])
    lines.append(f"Regime: {snap.get('regime') or '—'}")
    lines.append(snap.get("bubble_line") or "Bubble: n/a")
    lines.append("")
    lines.append("Ready for market open.")
    return "\n".join(lines)


def _seconds_until_premarket_send() -> float:
    now = datetime.now(_ET)
    target = now.replace(
        hour=_PREMARKET_HOUR,
        minute=_PREMARKET_MINUTE,
        second=0,
        microsecond=0,
    )
    if now >= target:
        return 0.0
    return (target - now).total_seconds()


def wait_until_premarket_send() -> None:
    delay = _seconds_until_premarket_send()
    if delay <= 0:
        _log("Past 9:00 AM ET — sending pre-market Telegram now.")
        return
    mins = delay / 60.0
    _log(f"Waiting until 9:00 AM ET ({mins:.0f} min)...")
    while delay > 0:
        chunk = min(delay, 300.0)
        time.sleep(chunk)
        delay = _seconds_until_premarket_send()


def send_premarket_telegram(message: str) -> bool:
    import config
    from modules import alerts

    if not config.get_telegram_config():
        _log("Telegram skipped: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
        return False
    ok = alerts.send_telegram(message[:4000])
    if ok:
        state = _load_state()
        state["premarket_telegram_date"] = _today_et()
        state["premarket_telegram_at"] = datetime.now(_ET).isoformat()
        _save_state(state)
        _log("Pre-market Telegram sent.")
    else:
        _log("Pre-market Telegram FAILED.")
    return ok


def telegram_already_sent_today() -> bool:
    return _load_state().get("premarket_telegram_date") == _today_et()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Autonomous paper bot overnight startup + 9 AM ET Telegram"
    )
    parser.add_argument("--username", default=os.getenv("PORTAL_USERNAME") or _default_username())
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram send")
    parser.add_argument(
        "--send-now",
        action="store_true",
        help="Send Telegram immediately after report (skip 9 AM wait)",
    )
    parser.add_argument(
        "--skip-start",
        action="store_true",
        help="Skip bot restart; use existing heartbeat for report/Telegram",
    )
    args = parser.parse_args()
    username = args.username.strip().lower()

    _log("=== Autostart Paper Bot (paper only) ===")
    _log(f"Project root: {ROOT}")

    os.environ.setdefault("PAPER_TRADING", "true")
    os.environ.setdefault("PAPER_CHASE_MODE", "1")

    import config

    config.init_paper_chase_if_enabled()
    config.enforce_realistic_research_profile()

    start_msg = "Skipped startup (--skip-start)"
    restart_after: datetime | None = None
    if not args.skip_start:
        from scripts.owner_reset import clean_restart_paper_only

        _log("Paper-only clean restart (owner_reset logic, live preserved)...")
        ok, start_msg = clean_restart_paper_only(username)
        _log(start_msg)
        if not ok:
            _log(f"[ERROR] Paper bot failed to start: {start_msg}")
            return 1
        restart_after = datetime.now(_ET)

    hb = (
        wait_for_heartbeat(username, restart_after=restart_after)
        if not args.skip_start
        else _load_heartbeat(username)
    )
    if hb is None and not args.skip_start:
        _log("[WARN] Proceeding without fresh heartbeat.")

    verify_rc, verify_output = run_insider_verification()
    snap = build_premarket_snapshot(hb)
    report_text = format_premarket_report_text(
        snap,
        verify_rc=verify_rc,
        verify_output=verify_output,
        username=username,
        start_msg=start_msg,
    )
    write_premarket_report(report_text)

    state = _load_state()
    state["last_run_at"] = datetime.now(_ET).isoformat()
    state["last_report_date"] = _today_et()
    state["verify_rc"] = verify_rc
    _save_state(state)

    if args.no_telegram:
        _log("Telegram disabled (--no-telegram). Done.")
        return 0 if verify_rc == 0 else 2

    if telegram_already_sent_today() and not args.send_now:
        _log("Pre-market Telegram already sent today — skipping.")
        return 0 if verify_rc == 0 else 2

    if not args.send_now:
        wait_until_premarket_send()

    if telegram_already_sent_today() and not args.send_now:
        _log("Pre-market Telegram already sent today (after wait) — skipping.")
        return 0 if verify_rc == 0 else 2

    hb_fresh = _load_heartbeat(username) or hb
    snap_send = build_premarket_snapshot(hb_fresh)
    tg_msg = format_premarket_telegram(snap_send)
    send_premarket_telegram(tg_msg)

    _log("=== Autostart complete ===")
    return 0 if verify_rc == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
