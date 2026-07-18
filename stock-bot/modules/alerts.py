"""Email and Telegram alerts — high-signal events only (policy in config.py)."""

from __future__ import annotations

import json
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import config

STATE_FILE = "alert_state.json"
_ET = ZoneInfo("America/New_York")
_GMAIL_APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_YIELD_GATE_ALERT_PATH = _DATA_DIR / "yield_gate_last_alert.json"
_YIELD_GATE_COOLDOWN = timedelta(minutes=30)

# Internal categories -> config flags
_CATEGORY_FLAGS: dict[str, str] = {
    "halt": "TELEGRAM_ALERT_HALT",
    "resume": "TELEGRAM_ALERT_HALT",
    "drawdown_major": "TELEGRAM_ALERT_DRAWDOWN_MAJOR",
    "yield_gate": "TELEGRAM_ALERT_YIELD_GATE",
    "daily_summary": "TELEGRAM_ALERT_DAILY_SUMMARY",
    "periodic_summary": "TELEGRAM_ALERT_PERIODIC_SUMMARY",
    "live_daily_summary": "TELEGRAM_ALERT_LIVE_DAILY_SUMMARY",
    "live_fill": "TELEGRAM_ALERT_LIVE_FILLS",
    "spacex": "TELEGRAM_ALERT_SPACEX",
    "btc": "TELEGRAM_ALERT_BTC",
    "social": "TELEGRAM_ALERT_SOCIAL",
}


@dataclass
class EmailResult:
    ok: bool
    error_code: str = ""
    message: str = ""
    hints: list[str] = field(default_factory=list)


def _is_gmail_smtp(host: str | None) -> bool:
    h = (host or "").strip().lower()
    return "gmail.com" in h or h == "smtp.googlemail.com"


def _smtp_error_hints(exc: BaseException) -> tuple[str, list[str]]:
    """Map SMTP failures to a short code and user-facing hints (Gmail-focused)."""
    msg = str(exc).strip()
    lower = msg.lower()
    code = type(exc).__name__
    hints: list[str] = []

    auth_failed = (
        isinstance(exc, smtplib.SMTPAuthenticationError)
        or "535" in msg
        or "badcredentials" in lower
        or "username and password not accepted" in lower
        or "authentication failed" in lower
    )
    if auth_failed:
        code = "BadCredentials"
        hints.extend(
            [
                "This is usually caused by using your normal Gmail password.",
                f"Use a 16-character App Password (no spaces) from {_GMAIL_APP_PASSWORD_URL}",
                "Turn on 2-Step Verification on your Google account, then create the App Password.",
                "Set SMTP_USER to your full @gmail.com address; SMTP_PASSWORD is only the App Password.",
            ]
        )
    elif isinstance(exc, smtplib.SMTPConnectError) or "connect" in lower:
        code = "ConnectError"
        hints.append("Check SMTP_HOST and SMTP_PORT (Gmail: smtp.gmail.com, port 587 with STARTTLS).")
    elif isinstance(exc, TimeoutError) or "timed out" in lower:
        code = "Timeout"
        hints.append("SMTP server did not respond — check firewall, VPN, or host/port.")
    elif "starttls" in lower:
        code = "StartTLS"
        hints.append("TLS handshake failed — for Gmail use port 587 (not 465 unless you change the client).")

    return code, hints


def print_email_failure(result: EmailResult) -> None:
    """Print a concise failure line plus troubleshooting bullets."""
    label = result.error_code or "Error"
    detail = f": {result.message}" if result.message and result.error_code != result.message else ""
    print(f"Email alert failed: {label}{detail}")
    for hint in result.hints:
        print(f"  → {hint}")


def check_email_config(*, test_login: bool = False, verbose: bool = True) -> bool:
    """Validate SMTP settings and print Gmail-friendly troubleshooting steps."""
    smtp = config.get_smtp_config()
    host = (smtp.get("host") or "").strip()
    to_addr = (smtp.get("to") or "").strip()
    user = (smtp.get("user") or "").strip()
    password = smtp.get("password") or ""
    port = int(smtp.get("port") or 587)
    issues: list[str] = []
    notes: list[str] = []

    if not host:
        issues.append("SMTP_HOST is not set.")
    if not to_addr:
        issues.append("ALERT_EMAIL_TO is not set (delivery address).")
    if not user:
        issues.append("SMTP_USER is not set (login address — for Gmail, your full @gmail.com).")
    if not password:
        issues.append("SMTP_PASSWORD is empty.")

    if _is_gmail_smtp(host):
        notes.append(
            "Gmail detected — use an App Password (16 characters, no spaces), not your login password."
        )
        notes.append(f"Create one at: {_GMAIL_APP_PASSWORD_URL}")
        pwd_compact = password.replace(" ", "")
        if password and " " in password.strip():
            issues.append("SMTP_PASSWORD contains spaces — paste the App Password without spaces.")
        if password and len(pwd_compact) != 16 and not issues:
            notes.append(
                f"SMTP_PASSWORD length is {len(pwd_compact)} chars (App Passwords are usually 16)."
            )

    if verbose:
        if host and to_addr:
            print(f"SMTP: {host}:{port} | login: {user or '(none)'} | to: {to_addr}")
        for note in notes:
            print(f"  Note: {note}")
        for issue in issues:
            print(f"  [!!] {issue}")

    if issues:
        if verbose:
            print("\nEmail troubleshooting:")
            print("  1. Copy SMTP_* and ALERT_EMAIL_TO from .env.example into your book .env")
            print("  2. Gmail: enable 2-Step Verification, then create an App Password")
            print(f"  3. Set SMTP_PASSWORD to the 16-character App Password (no spaces)")
            print("  4. Re-run: python scripts/account/test_alerts.py")
        return False

    if not test_login:
        return True

    result = _smtp_login_probe(host, port, user, password)
    if not result.ok and verbose:
        print(f"\n[!!] SMTP login test failed: {result.error_code or result.message}")
        for hint in result.hints:
            print(f"  → {hint}")
    return result.ok


def _smtp_login_probe(host: str, port: int, user: str, password: str) -> EmailResult:
    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            if port == 587:
                server.starttls()
                server.ehlo()
            if user and password:
                server.login(user, password)
        return EmailResult(ok=True)
    except (smtplib.SMTPException, OSError) as exc:
        code, hints = _smtp_error_hints(exc)
        return EmailResult(ok=False, error_code=code, message=str(exc), hints=hints)


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _category_enabled(category: str) -> bool:
    flag = _CATEGORY_FLAGS.get(category)
    if not flag:
        return False
    return bool(getattr(config, flag, False))


def _mode_label() -> str:
    return "PAPER" if config.PAPER_TRADING else "LIVE"


def alerts_configured() -> bool:
    tg = config.get_telegram_config()
    smtp = config.get_smtp_config()
    return bool(tg) or bool(smtp.get("host") and smtp.get("to"))


def send_telegram(text: str) -> bool:
    """Low-level send (bypasses policy) — use for manual tests only."""
    tg = config.get_telegram_config()
    if not tg:
        return False
    token, chat_id = tg
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"Telegram alert failed ({e.code}): {body[:200]}")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"Telegram alert failed: {e}")
        return False


def send_email(subject: str, body: str) -> bool:
    return send_email_alert(subject, body, html_body=None).ok


def send_email_html(subject: str, text_body: str, html_body: str | None = None) -> bool:
    result = send_email_alert(subject, text_body, html_body=html_body)
    if not result.ok:
        print_email_failure(result)
    return result.ok


def send_email_alert(
    subject: str, text_body: str, html_body: str | None = None
) -> EmailResult:
    """Send email via SMTP; returns structured result for tests and diagnostics."""
    smtp = config.get_smtp_config()
    host = smtp.get("host")
    to_addr = smtp.get("to")
    if not host or not to_addr:
        return EmailResult(
            ok=False,
            error_code="NotConfigured",
            message="SMTP_HOST and ALERT_EMAIL_TO are required",
            hints=[
                "Add SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and ALERT_EMAIL_TO to .env",
                f"Gmail users: App Password from {_GMAIL_APP_PASSWORD_URL}",
            ],
        )
    from_addr = smtp.get("from") or smtp.get("user") or to_addr
    port = int(smtp.get("port") or 587)
    user = smtp.get("user")
    password = smtp.get("password")

    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(text_body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            if port == 587:
                server.starttls()
                server.ehlo()
            if user and password:
                server.login(user, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        return EmailResult(ok=True)
    except (smtplib.SMTPException, OSError) as exc:
        code, hints = _smtp_error_hints(exc)
        if _is_gmail_smtp(host) and code == "BadCredentials":
            pass  # hints already Gmail-specific
        elif _is_gmail_smtp(host) and not hints:
            hints.append(f"Gmail: try an App Password from {_GMAIL_APP_PASSWORD_URL}")
        return EmailResult(ok=False, error_code=code, message=str(exc), hints=hints)


def broadcast(subject: str, body: str, *, category: str = "general") -> bool:
    """Send to configured channels when the alert category is enabled."""
    if not alerts_configured():
        return False
    if not _category_enabled(category):
        return False
    ok = False
    try:
        if send_telegram(f"{subject}\n\n{body}"):
            ok = True
    except Exception as e:
        print(f"Telegram alert error: {e}")
    try:
        result = send_email_alert(subject, body)
        if result.ok:
            ok = True
        elif result.error_code:
            print_email_failure(result)
    except Exception as e:
        print(f"Email alert error: {e}")
    return ok


def _parse_summary_time_et() -> tuple[int, int]:
    raw = (config.TELEGRAM_DAILY_SUMMARY_TIME or "16:30").strip()
    try:
        hour_s, minute_s = raw.split(":", 1)
        return int(hour_s), int(minute_s)
    except (ValueError, TypeError):
        return 16, 30


def _daily_summary_due() -> bool:
    """True once per ET calendar day after TELEGRAM_DAILY_SUMMARY_TIME."""
    if not config.TELEGRAM_ALERT_DAILY_SUMMARY:
        return False
    now_et = datetime.now(_ET)
    target_h, target_m = _parse_summary_time_et()
    if (now_et.hour, now_et.minute) < (target_h, target_m):
        return False
    today = now_et.date().isoformat()
    state = _load_state()
    return state.get("last_daily_summary") != today


def notify_halt(equity: float, peak_equity: float, drawdown_pct: float) -> None:
    """Alert once when drawdown halt first triggers."""
    state = _load_state()
    if state.get("halt_notified"):
        return

    mode = _mode_label()
    subject = f"[PythonTrading {mode}] RISK HALT"
    body = (
        f"Trading paused: max drawdown reached.\n\n"
        f"Equity:     ${equity:,.2f}\n"
        f"Peak:       ${peak_equity:,.2f}\n"
        f"Drawdown:   {drawdown_pct:.2%}\n"
        f"Limit:      {config.MAX_DRAWDOWN_PCT:.0%}\n"
        f"Time:       {datetime.now(_ET):%Y-%m-%d %H:%M:%S} ET\n\n"
        f"Review risk_events.log and Alpaca dashboard."
    )
    if broadcast(subject, body, category="halt"):
        state["halt_notified"] = True
        state["halt_notified_at"] = datetime.now().isoformat()
        _save_state(state)


def notify_resume(equity: float, drawdown_pct: float) -> None:
    """Alert when trading resumes after a risk halt."""
    mode = _mode_label()
    subject = f"[PythonTrading {mode}] RISK RESUME"
    body = (
        f"Trading resumed after drawdown recovered.\n\n"
        f"Equity:     ${equity:,.2f}\n"
        f"Drawdown:   {drawdown_pct:.2%}\n"
        f"Resume below: {config.HALT_RESUME_DRAWDOWN_PCT:.0%}\n"
        f"Time:       {datetime.now(_ET):%Y-%m-%d %H:%M:%S} ET"
    )
    if broadcast(subject, body, category="resume"):
        state = _load_state()
        state["halt_notified"] = False
        _save_state(state)


def clear_halt_flag() -> None:
    """Reset halt notification latch when trading is healthy."""
    state = _load_state()
    if state.get("halt_notified"):
        state["halt_notified"] = False
        _save_state(state)


def maybe_major_drawdown_alert(
    equity: float, peak_equity: float, drawdown_pct: float
) -> None:
    """Warn once when drawdown crosses TELEGRAM_MAJOR_DRAWDOWN_PCT (below halt limit)."""
    threshold = float(config.TELEGRAM_MAJOR_DRAWDOWN_PCT)
    state = _load_state()
    if drawdown_pct < threshold * 0.85:
        if state.pop("major_dd_notified", None) is not None:
            _save_state(state)
        return
    if drawdown_pct < threshold or state.get("major_dd_notified"):
        return

    mode = _mode_label()
    subject = f"[PythonTrading {mode}] Drawdown warning (>{threshold:.0%})"
    body = (
        f"Account drawdown crossed {threshold:.0%} (halt at {config.MAX_DRAWDOWN_PCT:.0%}).\n\n"
        f"Equity:     ${equity:,.2f}\n"
        f"Peak:       ${peak_equity:,.2f}\n"
        f"Drawdown:   {drawdown_pct:.2%}\n"
        f"Time:       {datetime.now(_ET):%Y-%m-%d %H:%M:%S} ET"
    )
    if broadcast(subject, body, category="drawdown_major"):
        state["major_dd_notified"] = True
        state["major_dd_notified_at"] = datetime.now().isoformat()
        _save_state(state)


def maybe_yield_gate_alert(active: bool) -> None:
    """Alert on yield-gate ON/OFF transitions with a 30-minute cooldown.

    Always keeps the first ON and first OFF alerts; later flips are throttled
    via ``data/yield_gate_last_alert.json`` so paper override chatter does not spam.
    """
    state = _load_state()
    prev = state.get("yield_gate_active")
    if prev is None:
        state["yield_gate_active"] = bool(active)
        _save_state(state)
        return
    if bool(prev) == bool(active):
        return

    state["yield_gate_active"] = bool(active)
    _save_state(state)

    if not _yield_gate_alert_allowed(bool(active)):
        return

    mode = _mode_label()
    label = "ACTIVATED" if active else "DEACTIVATED"
    subject = f"[PythonTrading {mode}] Yield gate {label}"
    body = (
        f"Yield gate is now {'ON' if active else 'OFF'}.\n"
        f"New SPY/equity entries are {'blocked' if active else 'allowed'}.\n\n"
        f"Time: {datetime.now(_ET):%Y-%m-%d %H:%M:%S} ET"
    )
    if broadcast(subject, body, category="yield_gate"):
        _mark_yield_gate_alert_sent(bool(active))


def _load_yield_gate_alert_cache() -> dict:
    if not _YIELD_GATE_ALERT_PATH.is_file():
        return {}
    try:
        raw = json.loads(_YIELD_GATE_ALERT_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_yield_gate_alert_cache(payload: dict) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _YIELD_GATE_ALERT_PATH.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"Yield gate alert cache write failed: {exc}")


def _yield_gate_alert_allowed(active: bool) -> bool:
    """True for first ON/OFF ever, or when 30+ minutes since last yield-gate alert."""
    cache = _load_yield_gate_alert_cache()
    first_key = "first_on_sent" if active else "first_off_sent"
    if not cache.get(first_key):
        return True
    raw = cache.get("last_alert_at")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=_ET)
        return datetime.now(_ET) - last.astimezone(_ET) >= _YIELD_GATE_COOLDOWN
    except (TypeError, ValueError):
        return True


def _mark_yield_gate_alert_sent(active: bool) -> None:
    cache = _load_yield_gate_alert_cache()
    if active:
        cache["first_on_sent"] = True
    else:
        cache["first_off_sent"] = True
    cache["last_alert_at"] = datetime.now(_ET).isoformat()
    cache["last_state"] = bool(active)
    _save_yield_gate_alert_cache(cache)

def maybe_monthly_wisdom_summary(rollup: dict) -> None:
    """Disabled unless TELEGRAM_ALERT_SOCIAL=true (noisy / research-only)."""
    if not rollup or not alerts_configured():
        return
    if not config.TELEGRAM_ALERT_SOCIAL:
        return

    month_key = rollup.get("month", "")
    state = _load_state()
    if state.get("last_monthly_wisdom_alert") == month_key:
        return

    live = rollup.get("live") or {}
    best = rollup.get("best_sim_mode", "n/a")
    rec = rollup.get("recommendation", "")
    mode = live.get("mode", config.WISDOM_MODE)
    live_ret = live.get("return_pct")
    ret_s = f"{live_ret:+.2f}%" if live_ret is not None else "n/a (no journal data)"

    mode_label = _mode_label()
    subject = f"[PythonTrading {mode_label}] Wisdom month {month_key}"
    body = (
        f"Month:          {month_key}\n"
        f"Active mode:    {mode}\n"
        f"Live return:    {ret_s}\n"
        f"Best sim mode:  {best}\n"
        f"Pause cycles:   {live.get('pause_cycles', 'n/a')}\n\n"
        f"Recommendation:\n{rec}\n\n"
        f"File: wisdom_monthly_{month_key}.json\n"
        f"Change WISDOM_MODE in .env manually if you agree."
    )
    if broadcast(subject, body, category="social"):
        state = _load_state()
        state["last_monthly_wisdom_alert"] = month_key
        _save_state(state)


def maybe_daily_summary(equity: float, cash: float, regime: str, halted: bool) -> None:
    """One summary per ET day after market close (TELEGRAM_DAILY_SUMMARY_TIME)."""
    if not _daily_summary_due():
        return

    now_et = datetime.now(_ET)
    today = now_et.date().isoformat()
    mode = _mode_label()
    status = "HALTED" if halted else "RUNNING"
    subject = f"[PythonTrading {mode}] Daily summary"
    body = (
        f"Status:     {status}\n"
        f"Regime:     {regime}\n"
        f"Equity:     ${equity:,.2f}\n"
        f"Cash:       ${cash:,.2f}\n"
        f"Date:       {today} (ET)\n"
        f"Sent:       {now_et:%H:%M} ET\n"
    )
    try:
        from modules.entry_skip_tracker import format_daily_summary

        skip_line = format_daily_summary()
        if "cycles=0" not in skip_line:
            body += f"\n{skip_line}\n"
    except ImportError:
        pass
    body += f"\nLogs: {config.PAPER_JOURNAL_CSV}, {config.HEARTBEAT_FILE}"
    if broadcast(subject, body, category="daily_summary"):
        state = _load_state()
        state["last_daily_summary"] = today
        _save_state(state)
    else:
        print("Daily summary alert failed or disabled (will retry next cycle).")


def maybe_spacex_ipo_alert(snapshot: dict) -> None:
    """SpaceX/BTC narrative headlines — off unless TELEGRAM_ALERT_BTC=true."""
    if not snapshot or not snapshot.get("alert") or not alerts_configured():
        return
    if not config.TELEGRAM_ALERT_BTC:
        return

    fetched_at = snapshot.get("fetched_at", "")
    state = _load_state()
    if state.get("last_spacex_ipo_alert") == fetched_at:
        return

    s = snapshot.get("summary") or {}
    top = (s.get("top_headlines") or [{}])[0].get("title", "n/a")
    mode = _mode_label()
    subject = f"[PythonTrading {mode}] SpaceX IPO ↔ BTC narrative active"
    body = (
        f"Narrative:      {s.get('narrative', 'n/a')}\n"
        f"Headlines:      {s.get('headline_count', 0)}\n"
        f"BTC-linked:     {s.get('btc_linked_count', 0)}\n"
        f"Avg sentiment:  {s.get('avg_sentiment', 0):+.2f}\n"
        f"Top headline:   {top}\n\n"
        f"Monitor file: {config.SPACEX_IPO_CACHE_FILE}"
    )
    if broadcast(subject, body, category="btc"):
        state = _load_state()
        state["last_spacex_ipo_alert"] = fetched_at
        _save_state(state)


def maybe_spacex_listing_alert(listing: dict) -> None:
    """IPO listing milestones — off unless TELEGRAM_ALERT_SPACEX=true."""
    if not listing or not alerts_configured() or not config.TELEGRAM_ALERT_SPACEX:
        return

    stage = listing.get("stage", "")
    state = _load_state()
    mode = _mode_label()
    ticker = listing.get("ticker", config.SPACEX_IPO_TICKER)
    kraken = listing.get("kraken") or {}
    alpaca = listing.get("alpaca") or {}

    if listing.get("became_tradable_kraken"):
        key = f"kraken_tradable_{listing.get('fetched_at', '')[:16]}"
        if state.get("last_spacex_kraken_alert_key") != key:
            subject = f"[PythonTrading] {ticker} LIVE ON KRAKEN — {kraken.get('wsname') or kraken.get('pair')}"
            body = (
                f"Kraken pair:  {kraken.get('wsname') or kraken.get('pair')}\n"
                f"Kind:         {kraken.get('kind')}\n"
                f"Stage:        {stage}\n\n"
                f"File: {config.SPACEX_IPO_LISTING_CACHE_FILE}"
            )
            if broadcast(subject, body, category="spacex"):
                state = _load_state()
                state["last_spacex_kraken_alert_key"] = key
                _save_state(state)

    if listing.get("became_tradable_alpaca"):
        key = f"alpaca_tradable_{listing.get('fetched_at', '')[:16]}"
        if state.get("last_spacex_listing_alert_key") == key:
            return
        subject = f"[PythonTrading {mode}] {ticker} IS TRADABLE ON ALPACA — IPO LIVE"
        body = (
            f"Ticker:     {ticker}\n"
            f"Stage:      {stage}\n"
            f"Tradable:   YES (Alpaca)\n"
            f"Status:     {alpaca.get('status', 'n/a')}\n"
            f"Expected:   {listing.get('expected_listing_date')} "
            f"({listing.get('days_until_expected')} days)\n\n"
            f"File: {config.SPACEX_IPO_LISTING_CACHE_FILE}"
        )
        if broadcast(subject, body, category="spacex"):
            state = _load_state()
            state["last_spacex_listing_alert_key"] = key
            _save_state(state)
        return

    if listing.get("became_tradable"):
        return

    if not listing.get("stage_changed"):
        return

    stage_key = f"stage_{stage}_{listing.get('fetched_at', '')[:10]}"
    if state.get("last_spacex_listing_alert_key") == stage_key:
        return

    sec = listing.get("sec") or {}
    milestones = sec.get("milestones") or []
    latest = milestones[0] if milestones else {}
    days = listing.get("days_until_expected")
    days_s = f"{days} days" if days is not None else "n/a"

    subject = f"[PythonTrading {mode}] SpaceX IPO milestone: {stage}"
    body = (
        f"Ticker:           {ticker}\n"
        f"Stage:            {stage}\n"
        f"Expected listing: {listing.get('expected_listing_date')} ({days_s})\n"
        f"Alpaca tradable:  {alpaca.get('tradable', False)}\n"
        f"Kraken tradable:  {kraken.get('tradable', False)} "
        f"({kraken.get('wsname') or 'not listed'})\n"
        f"Latest SEC:       {latest.get('form', 'n/a')} ({latest.get('date', 'n/a')})\n\n"
        f"File: {config.SPACEX_IPO_LISTING_CACHE_FILE}"
    )
    if broadcast(subject, body, category="spacex"):
        state = _load_state()
        state["last_spacex_listing_alert_key"] = stage_key
        _save_state(state)


def maybe_spacex_ipo_countdown_alert(listing: dict) -> None:
    """IPO countdown — off unless TELEGRAM_ALERT_SPACEX=true."""
    if not listing or not alerts_configured() or not config.TELEGRAM_ALERT_SPACEX:
        return
    days = listing.get("days_until_expected")
    if days is None or days < 0 or days > 14:
        return

    today = date.today().isoformat()
    state = _load_state()
    if state.get("last_spacex_countdown_day") == today:
        return

    mode = _mode_label()
    subject = f"[PythonTrading {mode}] SpaceX IPO in {days} day(s) — watch {listing.get('ticker')}"
    body = (
        f"Expected:  {listing.get('expected_listing_date')}\n"
        f"Stage:     {listing.get('stage')}\n"
        f"Alpaca:    {(listing.get('alpaca') or {}).get('tradable', False)}\n"
        f"Kraken:    {(listing.get('kraken') or {}).get('tradable', False)}"
    )
    if broadcast(subject, body, category="spacex"):
        state = _load_state()
        state["last_spacex_countdown_day"] = today
        _save_state(state)
