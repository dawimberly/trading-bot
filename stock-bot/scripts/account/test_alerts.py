"""Send a test alert to configured Telegram and/or email channels.

Run: python scripts/account/test_alerts.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules import alerts


def main():
    print("=== ALERT TEST ===\n")
    tg = config.get_telegram_config()
    smtp = config.get_smtp_config()

    if tg:
        print("[OK] Telegram configured")
    else:
        print("[--] Telegram not set (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")

    if smtp.get("host") and smtp.get("to"):
        print(f"[OK] Email configured -> {smtp['to']}")
    else:
        print("[--] Email not set (SMTP_HOST, ALERT_EMAIL_TO, ...)")

    if not alerts.alerts_configured():
        print("\nNo alert channels configured. Add vars to .env and retry.")
        sys.exit(1)

    subject = "[PythonTrading] Test alert"
    body = "If you received this, alerts are working."
    tg_ok = alerts.send_telegram(f"{subject}\n\n{body}") if tg else None
    email_ok = alerts.send_email(subject, body) if smtp.get("host") and smtp.get("to") else None

    trade_ok = None
    if tg:
        from modules.trade_notifier import send_trade_notification

        trade_ok = send_trade_notification(
            {
                "symbol": "SPY",
                "side": "Buy",
                "quantity": 0.01,
                "price": 550.25,
                "notional": 5.50,
                "sleeve": "SPY",
                "reason": "test_alert",
                "account_type": "Paper" if config.PAPER_TRADING else "Live",
            }
        )
        if trade_ok:
            print("[OK] Trade fill notification test sent")
        else:
            print("[!!] Trade fill notification test failed")

    if tg and not tg_ok:
        print("\nTelegram failed. Fix TELEGRAM_CHAT_ID (see get_telegram_chat_id.py).")
        sys.exit(1)
    if smtp.get("host") and smtp.get("to") and not email_ok:
        sys.exit(1)
    print("\nTest alert sent. Check Telegram and/or inbox.")


if __name__ == "__main__":
    main()
