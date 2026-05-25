"""Email and Telegram alerts for halts and daily paper-trading summaries."""

import json
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from email.mime.text import MIMEText

import config

STATE_FILE = "alert_state.json"


def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def alerts_configured():
    tg = config.get_telegram_config()
    smtp = config.get_smtp_config()
    return bool(tg) or bool(smtp.get("host") and smtp.get("to"))


def send_telegram(text):
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


def send_email(subject, body):
    smtp = config.get_smtp_config()
    host = smtp.get("host")
    to_addr = smtp.get("to")
    if not host or not to_addr:
        return False
    from_addr = smtp.get("from") or smtp.get("user") or to_addr
    port = smtp.get("port", 587)
    user = smtp.get("user")
    password = smtp.get("password")

    msg = MIMEText(body)
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
        return True
    except (smtplib.SMTPException, OSError) as e:
        print(f"Email alert failed: {e}")
        return False


def broadcast(subject, body):
    """Send to every configured channel; never raises."""
    if not alerts_configured():
        return False
    ok = False
    try:
        if send_telegram(f"{subject}\n\n{body}"):
            ok = True
    except Exception as e:
        print(f"Telegram alert error: {e}")
    try:
        if send_email(subject, body):
            ok = True
    except Exception as e:
        print(f"Email alert error: {e}")
    return ok


def notify_halt(equity, peak_equity, drawdown_pct):
    """Alert once when drawdown halt first triggers; reset when trading resumes."""
    state = _load_state()
    if state.get("halt_notified"):
        return

    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    subject = f"[PythonTrading {mode}] RISK HALT"
    body = (
        f"Trading paused: max drawdown reached.\n\n"
        f"Equity:     ${equity:,.2f}\n"
        f"Peak:       ${peak_equity:,.2f}\n"
        f"Drawdown:   {drawdown_pct:.2%}\n"
        f"Limit:      {config.MAX_DRAWDOWN_PCT:.0%}\n"
        f"Time:       {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        f"Review risk_events.log and Alpaca dashboard."
    )
    broadcast(subject, body)
    state["halt_notified"] = True
    state["halt_notified_at"] = datetime.now().isoformat()
    _save_state(state)


def clear_halt_flag():
    state = _load_state()
    if state.get("halt_notified"):
        state["halt_notified"] = False
        _save_state(state)


def maybe_monthly_wisdom_summary(rollup: dict) -> None:
    """One alert per rolled-up calendar month (recommendation only; no auto-switch)."""
    if not rollup or not alerts_configured():
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

    mode_label = "PAPER" if config.PAPER_TRADING else "LIVE"
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
    if broadcast(subject, body):
        state = _load_state()
        state["last_monthly_wisdom_alert"] = month_key
        _save_state(state)


def maybe_daily_summary(equity, cash, regime, halted):
    """One summary per calendar day (UTC-local date on machine)."""
    state = _load_state()
    today = date.today().isoformat()
    if state.get("last_daily_summary") == today:
        return

    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    status = "HALTED" if halted else "RUNNING"
    subject = f"[PythonTrading {mode}] Daily summary"
    body = (
        f"Status:     {status}\n"
        f"Regime:     {regime}\n"
        f"Equity:     ${equity:,.2f}\n"
        f"Cash:       ${cash:,.2f}\n"
        f"Date:       {today}\n\n"
        f"Logs: {config.PAPER_JOURNAL_CSV}, {config.HEARTBEAT_FILE}"
    )
    if broadcast(subject, body):
        state["last_daily_summary"] = today
        _save_state(state)
    else:
        print("Daily summary alert failed (will retry next cycle).")
