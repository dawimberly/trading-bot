"""Telegram phone commands: slash helpers + paper-only freeze CONFIRM/DENY/HOLD.

Read-only on live (status / positions / help). Never starts or stops trading.
Live and paper share one getUpdates offset + lock so dual bots do not steal replies.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import config
from modules import alerts

_ROOT = Path(__file__).resolve().parents[1]
_ATTRIBUTION_SCRIPT = _ROOT / "scripts" / "analysis" / "forward_sleeve_attribution.py"
_FREEZE_CONFIRM_RUNNERS = {
    "attribution_stale": _ATTRIBUTION_SCRIPT,
}

logger = logging.getLogger(__name__)

_OFFSET_KEY = "telegram_update_offset"
_REGISTERED_KEY = "telegram_commands_registered"
_POLLER_THREAD_NAME = "telegram-commands"
_LOCK_STALE_SEC = 45.0
_poller_started = False
_poller_guard = threading.Lock()

_BOT_COMMANDS = (
    ("status", "Live + paper equity and heartbeat"),
    ("positions", "Sleeve exposure from last cycle"),
    ("signals", "Top insider signals (paper)"),
    ("insider", "Signals + short watch"),
    ("boosts", "Insider boosts"),
    ("shorts", "Protective shorts (paper)"),
    ("help", "Command list"),
)


def _logs_dir() -> Path:
    path = _ROOT / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lock_path() -> Path:
    return _logs_dir() / "telegram_poll.lock"


def _state_path() -> Path:
    return _logs_dir() / "telegram_command_state.json"


def telegram_commands_configured() -> bool:
    return bool(config.get_telegram_config())


def effective_telegram_commands_enabled() -> bool:
    """Slash commands for the authorized Telegram chat (read-only on live)."""
    if not telegram_commands_configured():
        return False
    if not bool(getattr(config, "TELEGRAM_COMMANDS_ENABLED", True)):
        return False
    if not config.PAPER_TRADING and not bool(
        getattr(config, "TELEGRAM_COMMANDS_LIVE", True)
    ):
        return False
    return True


def freeze_commands_allowed() -> bool:
    """HOLD/CONFIRM/DENY can run measure-only scripts — paper book only."""
    return bool(config.PAPER_TRADING) and effective_telegram_commands_enabled()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_poll_state() -> dict[str, Any]:
    state = _load_json(_state_path()) or {}
    if _OFFSET_KEY not in state:
        try:
            old = alerts._load_state().get(_OFFSET_KEY)
            if old:
                state[_OFFSET_KEY] = int(old)
        except (TypeError, ValueError, OSError):
            pass
    return state


def _save_poll_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(path)


def _try_acquire_lock(path: Path, *, stale_sec: float = _LOCK_STALE_SEC) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = stale_sec + 1
        if age <= stale_sec:
            return False
        try:
            shutil.rmtree(path)
            path.mkdir()
        except OSError:
            return False
    try:
        (path / "pid").write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return True


def _release_lock(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def _api(method: str, *, params: dict[str, Any] | None = None, timeout: float = 12) -> dict | None:
    tg = config.get_telegram_config()
    if not tg:
        return None
    token, _chat_id = tg
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {"User-Agent": "PythonTradingBot/1.0"}
    if params:
        body = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        ).encode()
        data = body
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("Telegram API %s failed: %s", method, exc)
        return None


def register_bot_commands(*, force: bool = False) -> bool:
    """Install the Telegram app command menu (setMyCommands)."""
    if not telegram_commands_configured():
        return False
    state = _load_poll_state()
    if state.get(_REGISTERED_KEY) and not force:
        return True
    payload = json.dumps(
        [{"command": name, "description": desc} for name, desc in _BOT_COMMANDS]
    )
    data = _api("setMyCommands", params={"commands": payload}, timeout=15)
    if not data or not data.get("ok"):
        return False
    state[_REGISTERED_KEY] = True
    _save_poll_state(state)
    return True


def _send_reply(chat_id: str, text: str) -> bool:
    tg = config.get_telegram_config()
    if not tg:
        return False
    _token, chat_id_cfg = tg
    if str(chat_id) != str(chat_id_cfg):
        logger.warning("Telegram command ignored from unauthorized chat %s", chat_id)
        return False
    return alerts.send_telegram(text[:4000])


def _normalize_command(text: str) -> str:
    raw = (text or "").strip().split()[0] if text else ""
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    return raw.lower()


def _fmt_money(val: Any) -> str:
    try:
        return f"${float(val):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _hb_age_label(hb: dict[str, Any] | None) -> str:
    if not hb or not hb.get("timestamp"):
        return "no heartbeat"
    raw = str(hb["timestamp"])
    try:
        from datetime import datetime

        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        age_min = max(0.0, (datetime.now(ts.tzinfo) - ts).total_seconds() / 60.0)
    except (TypeError, ValueError):
        return raw[:19]
    stale = " STALE" if age_min > 90 else ""
    if age_min < 1.5:
        return f"{age_min * 60:.0f}s ago{stale}"
    return f"{age_min:.0f}m ago{stale}"


def _load_book_heartbeat(*, paper: bool) -> dict[str, Any] | None:
    try:
        from modules.health_check import resolve_live_heartbeat_path, resolve_paper_heartbeat_path

        path = resolve_paper_heartbeat_path() if paper else resolve_live_heartbeat_path()
    except Exception:
        path = config.resolve_heartbeat_file(paper=paper)
    return _load_json(Path(path))


def _book_status_line(
    label: str,
    hb: dict[str, Any] | None,
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
) -> str:
    eq = equity
    if eq is None and hb is not None:
        try:
            eq = float(hb.get("equity"))
        except (TypeError, ValueError):
            eq = None
    cash_v = cash
    if cash_v is None and hb is not None:
        try:
            cash_v = float(hb.get("cash"))
        except (TypeError, ValueError):
            cash_v = None
    regime_v = regime or (str(hb.get("regime")) if hb and hb.get("regime") else "n/a")
    halted = " HALTED" if hb and hb.get("halted") else ""
    err = ""
    if hb and hb.get("last_cycle_error"):
        err = f" err={str(hb['last_cycle_error'])[:60]}"
    return (
        f"{label}  eq {_fmt_money(eq)}  cash {_fmt_money(cash_v)}  "
        f"{regime_v}{halted}  {_hb_age_label(hb)}{err}"
    )


def format_signals_command(*, limit: int = 5) -> str:
    from modules.insider_monitor import _format_value, _sig_type, get_recent_insider_signals

    if not config.effective_insider_monitor_enabled():
        return "Insider monitor is off (paper only)."
    signals = get_recent_insider_signals(days=7, min_score=60)[:limit]
    if not signals:
        return "No high-quality insider signals (score >= 60)."
    lines = ["Insider signals (top 5):"]
    for sig in signals:
        tk = sig.get("ticker") or sig.get("company") or "?"
        st = _sig_type(sig)
        val = _format_value(sig.get("value"))
        val_s = f" {val}" if val else ""
        desc = str(sig.get("description") or "")[:55]
        lines.append(f"• {tk} | {st} | s{sig.get('score', 0)}{val_s}")
        lines.append(f"  {desc}")
    return "\n".join(lines)


def format_insider_command() -> str:
    from modules.insider_signal_handler import get_boost_snapshot

    body = format_signals_command(limit=5)
    snap = get_boost_snapshot()
    shorts = snap.get("short_candidates") or []
    clusters = snap.get("strong_clusters") or []
    extra: list[str] = []
    if clusters:
        extra.append(f"Strong clusters: {', '.join(clusters)}")
    if shorts:
        extra.append(f"Short watch: {', '.join(shorts)}")
    if extra:
        return body + "\n\n" + "\n".join(extra)
    return body


def format_boosts_command() -> str:
    from modules.insider_signal_handler import get_boost_snapshot

    if not config.effective_insider_signal_boost_enabled():
        return "Insider boosts off (paper only)."
    snap = get_boost_snapshot()
    if not snap.get("enabled"):
        return "No boost state yet — wait for next bot cycle."
    lines = ["Insider boosts:"]
    mom = snap.get("momentum_boosts") or {}
    sa = snap.get("stat_arb_boosts") or {}
    sb = snap.get("short_boosts") or {}
    if mom:
        lines.append("Momentum:")
        for sym, val in sorted(mom.items(), key=lambda x: -x[1])[:6]:
            if val > 0:
                lines.append(f"  +{sym}: +{val:.3f}")
    if sa:
        lines.append("Stat arb long:")
        for sym, val in sorted(sa.items(), key=lambda x: -x[1])[:6]:
            if val > 1.0:
                lines.append(f"  +{sym}: x{val:.3f}")
    if sb:
        lines.append("Short priority:")
        for sym, meta in sb.items():
            lines.append(
                f"  -{sym}: {meta.get('base', 0):.3f} ({meta.get('role', '?')})"
            )
    guard = snap.get("risk_guard_notes") or []
    if guard:
        lines.append("Risk guard: " + "; ".join(guard))
    if len(lines) == 1:
        lines.append("  (none active)")
    return "\n".join(lines)


def format_shorts_command(
    *,
    equity: float | None = None,
    regime: str = "",
) -> str:
    from modules.short_activity import format_shorts_telegram_block

    return format_shorts_telegram_block(regime=regime, equity=equity)


def _sleeve_exposure_lines(hb: dict[str, Any] | None) -> list[str]:
    exposure = (hb or {}).get("sleeve_exposure") or {}
    if not isinstance(exposure, dict) or not exposure:
        return []
    rows: list[str] = []
    mapping = (
        ("VTI", "vti_core_value"),
        ("SPY", "spy_value"),
        ("Crypto", "crypto_value"),
        ("NYSE", "nyse_value"),
        ("Metal", "metal_value"),
    )
    for label, key in mapping:
        try:
            val = float(exposure.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val > 0:
            rows.append(f"  {label}: {_fmt_money(val)}")
    return rows


def format_positions_command() -> str:
    live_hb = _load_book_heartbeat(paper=False)
    paper_hb = _load_book_heartbeat(paper=True)
    lines = ["Positions (heartbeat sleeves):"]
    live_rows = _sleeve_exposure_lines(live_hb)
    paper_rows = _sleeve_exposure_lines(paper_hb)
    lines.append("LIVE")
    lines.extend(live_rows or ["  (none / no heartbeat)"])
    lines.append("PAPER")
    lines.extend(paper_rows or ["  (none / no heartbeat)"])
    return "\n".join(lines)


def format_status_command(
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
) -> str:
    live_hb = _load_book_heartbeat(paper=False)
    paper_hb = _load_book_heartbeat(paper=True)
    live_eq = live_cash = live_regime = None
    paper_eq = paper_cash = paper_regime = None
    if config.PAPER_TRADING:
        paper_eq, paper_cash, paper_regime = equity, cash, regime
    else:
        live_eq, live_cash, live_regime = equity, cash, regime

    lines = [
        "PythonTrading phone status",
        _book_status_line(
            "LIVE ",
            live_hb,
            equity=live_eq,
            cash=live_cash,
            regime=live_regime or "",
        ),
        _book_status_line(
            "PAPER",
            paper_hb,
            equity=paper_eq,
            cash=paper_cash,
            regime=paper_regime or "",
        ),
    ]
    proc = "Paper" if config.PAPER_TRADING else "Live"
    lines.append(f"This process: {proc} (read-only commands)")
    try:
        from modules.insider_signal_handler import get_boost_snapshot

        lines.append(
            f"Insider monitor: {'ON' if config.effective_insider_monitor_enabled() else 'OFF'}"
        )
        snap = get_boost_snapshot()
        summary = snap.get("summary") or "n/a"
        lines.append(f"Insider: {str(summary)[:200]}")
    except Exception as exc:
        logger.debug("status insider block skipped: %s", exc)
    if config.paper_chase_mode_enabled() or config.paper_aggressive_context():
        try:
            from modules.bot_health import (
                calculate_health_score,
                format_health_telegram,
                gather_health_context,
            )

            hctx = gather_health_context({"regime": regime})
            health = calculate_health_score(**hctx)
            lines.append(format_health_telegram(health))
        except Exception as exc:
            logger.debug("status health block skipped: %s", exc)
    return "\n".join(lines)


def format_help_command() -> str:
    extra = ""
    if freeze_commands_allowed():
        extra = "\nFreeze (paper): HOLD <id> / DENY <id> / CONFIRM <id>"
    return (
        "Phone commands (authorized chat only):\n"
        "/status — live + paper equity / heartbeat\n"
        "/positions — sleeve exposure\n"
        "/signals — top 5 insider signals\n"
        "/insider — signals + short watchlist\n"
        "/boosts — momentum / stat arb / short boosts\n"
        "/shorts — protective short exposure\n"
        "/help — this list\n"
        "Read-only: commands never start/stop trading."
        f"{extra}"
    )


def parse_freeze_command(text: str) -> tuple[str, str] | None:
    """Parse `HOLD id` / `CONFIRM id` / `DENY id` (optional `FREEZE` prefix)."""
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return None
    lowered = raw.lower()
    if lowered.startswith("freeze "):
        raw = raw[7:].strip()
    parts = raw.split(None, 1)
    if len(parts) < 2:
        return None
    action = parts[0].strip().upper()
    if action not in ("CONFIRM", "DENY", "HOLD"):
        return None
    finding = parts[1].strip().split()[0].strip("`\"'")
    if not finding:
        return None
    return action, finding


def _run_whitelisted_freeze_script(script: Path) -> str:
    """Measure-only scripts; never places orders."""
    py = sys.executable
    try:
        proc = subprocess.run(
            [py, "-u", str(script)],
            cwd=str(_ROOT),
            timeout=180,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"timeout after 180s: {script.name}"
    except OSError as exc:
        return f"failed to start {script.name}: {exc}"
    if proc.returncode == 0:
        return f"ok {script.name}"
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = err[-1] if err else f"exit {proc.returncode}"
    return f"{script.name} exit {proc.returncode}: {tail[:180]}"


def handle_freeze_command(text: str) -> str | None:
    parsed = parse_freeze_command(text)
    if not parsed:
        return None
    action, finding = parsed
    if action == "HOLD":
        return f"HOLD {finding} — no-op. Logged."
    if action == "DENY":
        return f"DENY {finding} — no script. Logged."
    runner = _FREEZE_CONFIRM_RUNNERS.get(finding)
    if runner is None:
        return f"CONFIRM {finding} — no handler; treated as HOLD."
    result = _run_whitelisted_freeze_script(runner)
    return f"CONFIRM {finding} — {result}"


def handle_telegram_command(
    text: str,
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
) -> str | None:
    if freeze_commands_allowed():
        freeze_reply = handle_freeze_command(text)
        if freeze_reply:
            return freeze_reply
    elif parse_freeze_command(text):
        return "Freeze commands are paper-only."
    cmd = _normalize_command(text)
    if cmd == "/signals":
        return format_signals_command()
    if cmd == "/insider":
        return format_insider_command()
    if cmd == "/boosts":
        return format_boosts_command()
    if cmd == "/shorts":
        return format_shorts_command(equity=equity, regime=regime)
    if cmd in ("/status", "/live", "/paper"):
        return format_status_command(equity=equity, cash=cash, regime=regime)
    if cmd == "/positions":
        return format_positions_command()
    if cmd in ("/start", "/help"):
        return format_help_command()
    return None


def maybe_poll_telegram_commands(
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
    long_poll_sec: int = 0,
) -> int:
    """Poll Telegram getUpdates; reply to the authorized chat only.

    Live + paper share logs/telegram_command_state.json so only one process
    consumes each update. A mkdir lock prevents overlapping polls.
    """
    if not effective_telegram_commands_enabled():
        return 0
    if _poller_started and threading.current_thread().name != _POLLER_THREAD_NAME:
        return 0
    lock = _lock_path()
    if not _try_acquire_lock(lock):
        return 0
    updates: list[Any] = []
    try:
        state = _load_poll_state()
        offset = int(state.get(_OFFSET_KEY) or 0)
        http_timeout = max(12.0, float(long_poll_sec) + 5.0)
        data = _api(
            "getUpdates",
            params={
                "offset": offset,
                "timeout": int(long_poll_sec),
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=http_timeout,
        )
        if not data or not data.get("ok"):
            return 0
        updates = list(data.get("result") or [])
        max_update_id = offset
        for upd in updates:
            try:
                max_update_id = max(max_update_id, int(upd.get("update_id", 0)) + 1)
            except (TypeError, ValueError):
                continue
        if max_update_id > offset:
            state[_OFFSET_KEY] = max_update_id
            _save_poll_state(state)
    finally:
        _release_lock(lock)

    handled = 0
    for upd in updates:
        try:
            msg = upd.get("message") or upd.get("edited_message") or {}
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            reply = handle_telegram_command(
                text,
                equity=equity,
                cash=cash,
                regime=regime,
            )
            if reply and _send_reply(chat_id, reply):
                handled += 1
        except Exception as exc:
            logger.debug("Telegram command skip: %s", exc)
    return handled


def _poller_loop() -> None:
    register_bot_commands()
    while True:
        try:
            if not effective_telegram_commands_enabled():
                time.sleep(15)
                continue
            t0 = time.time()
            maybe_poll_telegram_commands(long_poll_sec=8)
            if time.time() - t0 < 1.0:
                time.sleep(1.5)
        except Exception as exc:
            logger.debug("Telegram command poller: %s", exc)
            time.sleep(3)


def ensure_telegram_command_poller() -> bool:
    """Start a daemon getUpdates thread so phone commands do not wait for a cycle."""
    global _poller_started
    if not effective_telegram_commands_enabled():
        return False
    with _poller_guard:
        if _poller_started:
            return True
        thread = threading.Thread(
            target=_poller_loop,
            name=_POLLER_THREAD_NAME,
            daemon=True,
        )
        thread.start()
        _poller_started = True
        return True
