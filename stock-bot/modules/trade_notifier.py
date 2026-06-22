"""Telegram notifications for confirmed Alpaca order fills (live account, policy-gated)."""

from __future__ import annotations

from datetime import datetime

import config
from modules.alerts import broadcast


def _format_side(side: str) -> str:
    raw = str(side or "").strip()
    if not raw:
        return "?"
    cleaned = raw.replace("OrderSide.", "").replace("orderside.", "")
    return cleaned.capitalize()


def format_trade_message(trade_details: dict) -> str:
    """Human-readable fill summary for Telegram."""
    account = trade_details.get("account_type") or ("Paper" if config.PAPER_TRADING else "Live")
    symbol = trade_details.get("symbol", "?")
    side = _format_side(trade_details.get("side", "?"))
    qty = trade_details.get("quantity")
    price = trade_details.get("price")
    notional = trade_details.get("notional")
    sleeve = trade_details.get("sleeve", "")
    reason = trade_details.get("reason", "")
    ts = trade_details.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"[PythonTrading {account}] Trade filled",
        f"Time:     {ts}",
        f"Symbol:   {symbol}",
        f"Side:     {side}",
    ]
    if qty is not None:
        lines.append(f"Qty:      {qty:g}")
    if price is not None:
        lines.append(f"Price:    ${float(price):,.4f}")
    if notional is not None:
        lines.append(f"Notional: ${float(notional):,.2f}")
    if sleeve:
        lines.append(f"Sleeve:   {sleeve}")
    if reason:
        lines.append(f"Reason:   {reason}")
    return "\n".join(lines)


def should_notify_fill(trade_details: dict) -> bool:
    """Live fills only, above minimum notional (paper fills never alert)."""
    if config.PAPER_TRADING:
        return False
    if not config.TELEGRAM_ALERT_LIVE_FILLS:
        return False
    if not config.get_telegram_config():
        return False
    notional = float(trade_details.get("notional") or 0)
    if notional <= 0:
        qty = float(trade_details.get("quantity") or 0)
        price = float(trade_details.get("price") or 0)
        if qty > 0 and price > 0:
            notional = qty * price
    return notional >= float(config.TELEGRAM_LIVE_FILL_MIN_USD)


def send_trade_notification(trade_details: dict) -> bool:
    """Send Telegram alert for a confirmed live fill; never raises."""
    if not should_notify_fill(trade_details):
        return False
    try:
        msg = format_trade_message(trade_details)
        subject = msg.split("\n", 1)[0]
        body = msg.split("\n", 1)[1] if "\n" in msg else ""
        return broadcast(subject, body, category="live_fill")
    except Exception as exc:
        print(f"Trade notification failed: {exc}")
        return False
