"""Telegram bot commands: slash helpers + freeze CONFIRM/DENY/HOLD lines."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
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


def effective_telegram_commands_enabled() -> bool:
    """Paper/research only — never on live money book."""
    if not config.get_telegram_config():
        return False
    if not config.PAPER_TRADING:
        return False
    if config.ALLOW_LIVE_TRADING and not config.PAPER_TRADING:
        return False
    return bool(
        config.paper_chase_mode_enabled()
        or config.effective_insider_monitor_enabled()
        or config.paper_aggressive_context()
    )


def _api_get(method: str, **params: Any) -> dict | None:
    tg = config.get_telegram_config()
    if not tg:
        return None
    token, chat_id_cfg = tg
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"https://api.telegram.org/bot{token}/{method}"
    if qs:
        url = f"{url}?{qs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PythonTradingBot/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("Telegram API %s failed: %s", method, exc)
        return None


def _send_reply(chat_id: str, text: str) -> bool:
    tg = config.get_telegram_config()
    if not tg:
        return False
    token, chat_id_cfg = tg
    if str(chat_id) != str(chat_id_cfg):
        logger.warning("Telegram command ignored from unauthorized chat %s", chat_id)
        return False
    return alerts.send_telegram(text[:4000])


def _normalize_command(text: str) -> str:
    raw = (text or "").strip().split()[0] if text else ""
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    return raw.lower()


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


def format_status_command(
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
) -> str:
    from modules.insider_signal_handler import get_boost_snapshot
    from modules.short_activity import format_shorts_telegram_block
    from modules.bot_health import format_health_telegram, gather_health_context, calculate_health_score

    label = "Paper" if config.PAPER_TRADING else "Live"
    lines = [f"PythonTrading {label} status · RR v{config.REALISTIC_RESEARCH_VERSION}"]
    if equity is not None:
        lines.append(f"Equity: ${equity:,.2f}")
    if cash is not None:
        lines.append(f"Cash: ${cash:,.2f}")
    if regime:
        lines.append(f"Regime: {regime}")
    if config.paper_chase_mode_enabled() or config.paper_aggressive_context():
        hctx = gather_health_context({"regime": regime})
        health = calculate_health_score(**hctx)
        lines.append(format_health_telegram(health))
    lines.append(
        f"Insider monitor: {'ON' if config.effective_insider_monitor_enabled() else 'OFF'}"
    )
    lines.append(
        f"Insider boosts: {'ON' if config.effective_insider_signal_boost_enabled() else 'OFF'}"
    )
    snap = get_boost_snapshot()
    summary = snap.get("summary") or "n/a"
    lines.append(f"Insider: {summary[:200]}")
    short_block = format_shorts_telegram_block(regime=regime, equity=equity)
    if short_block:
        lines.append("")
        lines.append(short_block)
    return "\n".join(lines)


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
    freeze_reply = handle_freeze_command(text)
    if freeze_reply:
        return freeze_reply
    cmd = _normalize_command(text)
    if cmd == "/signals":
        return format_signals_command()
    if cmd == "/insider":
        return format_insider_command()
    if cmd == "/boosts":
        return format_boosts_command()
    if cmd == "/shorts":
        return format_shorts_command(equity=equity, regime=regime)
    if cmd == "/status":
        return format_status_command(equity=equity, cash=cash, regime=regime)
    if cmd in ("/start", "/help"):
        return (
            "Commands:\n"
            "/signals — top 5 insider signals\n"
            "/insider — signals + short watchlist\n"
            "/boosts — momentum / stat arb / short boosts\n"
            "/shorts — protective short exposure + fires\n"
            "/status — bot + insider + shorts summary\n"
            "Freeze: HOLD <id> / DENY <id> / CONFIRM <id>"
        )
    return None


def maybe_poll_telegram_commands(
    *,
    equity: float | None = None,
    cash: float | None = None,
    regime: str = "",
) -> int:
    """Poll Telegram getUpdates once per cycle; reply to authorized chat only."""
    if not effective_telegram_commands_enabled():
        return 0
    state = alerts._load_state()
    offset = int(state.get(_OFFSET_KEY) or 0)
    data = _api_get("getUpdates", offset=offset, timeout=0, allowed_updates=json.dumps(["message"]))
    if not data or not data.get("ok"):
        return 0
    updates = data.get("result") or []
    handled = 0
    max_update_id = offset
    for upd in updates:
        try:
            uid = int(upd.get("update_id", 0))
            max_update_id = max(max_update_id, uid + 1)
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
    if max_update_id > offset:
        state[_OFFSET_KEY] = max_update_id
        alerts._save_state(state)
    return handled
