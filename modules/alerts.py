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


def maybe_spacex_ipo_alert(snapshot: dict) -> None:
    """Alert once per cache refresh when BTC-linked SpaceX IPO headlines spike."""
    if not snapshot or not snapshot.get("alert") or not alerts_configured():
        return
    fetched_at = snapshot.get("fetched_at", "")
    state = _load_state()
    if state.get("last_spacex_ipo_alert") == fetched_at:
        return

    s = snapshot.get("summary") or {}
    top = (s.get("top_headlines") or [{}])[0].get("title", "n/a")
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    subject = f"[PythonTrading {mode}] SpaceX IPO ↔ BTC narrative active"
    body = (
        f"Narrative:      {s.get('narrative', 'n/a')}\n"
        f"Headlines:      {s.get('headline_count', 0)}\n"
        f"BTC-linked:     {s.get('btc_linked_count', 0)}\n"
        f"Avg sentiment:  {s.get('avg_sentiment', 0):+.2f}\n"
        f"Top headline:   {top}\n\n"
        f"S-1 context: SpaceX disclosed ~18,712 BTC treasury.\n"
        f"SPCX perp: synthetic pre-IPO contract on Hyperliquid (not Alpaca).\n"
        f"Override: SPACEX_IPO_CRYPTO_OVERRIDE opens BTC pairs when narrative hot.\n"
        f"Monitor file: {config.SPACEX_IPO_CACHE_FILE}"
    )
    if broadcast(subject, body):
        state = _load_state()
        state["last_spacex_ipo_alert"] = fetched_at
        _save_state(state)


def maybe_spacex_listing_alert(listing: dict) -> None:
    """Alert on SEC stage changes and when SPCX becomes tradable on Alpaca or Kraken."""
    if not listing or not alerts_configured():
        return

    stage = listing.get("stage", "")
    state = _load_state()
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
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
                f"Auto-buy:     {'on' if config.KRAKEN_SPCX_BUY_ENABLED else 'off'} "
                f"(${config.KRAKEN_SPCX_BUY_USD:,.0f})\n"
                f"Requires:     ALLOW_KRAKEN_TRADING=yes + API keys in .env\n\n"
                f"US equities on Kraken Pro may also appear in-app before API xStock pairs.\n"
                f"File: {config.SPACEX_IPO_LISTING_CACHE_FILE}"
            )
            if broadcast(subject, body):
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
            f"PAPER auto-buy: ${config.SPACEX_IPO_BUY_NOTIONAL:,.0f} "
            f"({'on' if config.SPACEX_IPO_AUTO_BUY and config.PAPER_TRADING else 'off'})\n\n"
            f"Kraken:     {'tradable' if kraken.get('tradable') else 'scanning for SPCXx/USD'}\n"
            f"File: {config.SPACEX_IPO_LISTING_CACHE_FILE}"
        )
        if broadcast(subject, body):
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
        f"Tracked: SEC → Alpaca paper → Kraken Pro (SPCX / SPCXx API scan)\n"
        f"File: {config.SPACEX_IPO_LISTING_CACHE_FILE}"
    )
    if broadcast(subject, body):
        state = _load_state()
        state["last_spacex_listing_alert_key"] = stage_key
        _save_state(state)


def maybe_spacex_ipo_countdown_alert(listing: dict) -> None:
    """One alert per day when within 14 days of expected listing."""
    if not listing or not alerts_configured():
        return
    days = listing.get("days_until_expected")
    if days is None or days < 0 or days > 14:
        return

    today = date.today().isoformat()
    state = _load_state()
    if state.get("last_spacex_countdown_day") == today:
        return

    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    subject = f"[PythonTrading {mode}] SpaceX IPO in {days} day(s) — watch {listing.get('ticker')}"
    body = (
        f"Expected:  {listing.get('expected_listing_date')}\n"
        f"Stage:     {listing.get('stage')}\n"
        f"Alpaca:    {(listing.get('alpaca') or {}).get('tradable', False)}\n"
        f"Kraken:    {(listing.get('kraken') or {}).get('tradable', False)}\n\n"
        f"Bot scans Alpaca + Kraken Pro API every cycle for {listing.get('ticker')}.\n"
        f"Paper Alpaca auto-buy: on by default. Kraken: set KRAKEN_SPCX_BUY_ENABLED=true."
    )
    if broadcast(subject, body):
        state = _load_state()
        state["last_spacex_countdown_day"] = today
        _save_state(state)
