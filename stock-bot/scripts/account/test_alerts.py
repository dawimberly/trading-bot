"""Send a test alert to configured Telegram and/or email channels.

Run:
  python scripts/account/test_alerts.py
  python scripts/account/test_alerts.py --weekly-test
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules import alerts


def main():
    parser = argparse.ArgumentParser(description="Test Telegram/email alerts")
    parser.add_argument(
        "--weekly-test",
        action="store_true",
        help="Send weekly Telegram summary now (test_mode=True)",
    )
    args = parser.parse_args()

    if args.weekly_test:
        print("=== WEEKLY TELEGRAM TEST ===\n")
        tg = config.get_telegram_config()
        if not tg:
            print("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            sys.exit(1)
        from modules.weekly_telegram_summary import send_weekly_telegram_summary

        ok = send_weekly_telegram_summary(test_mode=True)
        sys.exit(0 if ok else 1)

    print("=== ALERT TEST ===\n")
    tg = config.get_telegram_config()
    smtp = config.get_smtp_config()
    email_wanted = bool(smtp.get("host") and smtp.get("to"))

    if tg:
        print("[OK] Telegram configured")
        print(f"     Policy: {config.telegram_alert_policy_summary()}")
    else:
        print("[--] Telegram not set (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")

    if email_wanted:
        print(f"[OK] Email target -> {smtp['to']}")
        if alerts._is_gmail_smtp(smtp.get("host")):
            print(
                "     Gmail: use a 16-character App Password (no spaces), "
                "not your normal password — https://myaccount.google.com/apppasswords"
            )
        print()
        if not alerts.check_email_config(test_login=True):
            print("\nFix SMTP settings in .env, then re-run this script.")
            if not tg:
                sys.exit(1)
    else:
        print("[--] Email not set (SMTP_HOST, ALERT_EMAIL_TO, SMTP_USER, SMTP_PASSWORD)")

    if config.telegram_weekly_summary_enabled():
        print(
            f"[OK] Weekly Telegram enabled — Fridays after "
            f"{config.TELEGRAM_WEEKLY_SUMMARY_TIME} ET (market closed)"
        )
    else:
        print("[--] Weekly Telegram disabled (TELEGRAM_WEEKLY_SUMMARY_ENABLED / live book)")

    if not alerts.alerts_configured():
        print("\nNo alert channels configured. Add vars to .env and retry.")
        sys.exit(1)

    subject = "[PythonTrading] Test alert"
    body = "If you received this, alerts are working."
    tg_ok = alerts.send_telegram(f"{subject}\n\n{body}") if tg else None
    email_ok = None
    if email_wanted:
        result = alerts.send_email_alert(subject, body)
        email_ok = result.ok
        if not result.ok:
            print(f"\n[!!] Email test failed: {result.error_code or 'Error'}")
            for hint in result.hints:
                print(f"  -> {hint}")
            alerts.check_email_config(test_login=False, verbose=True)

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
    if email_wanted and not email_ok:
        sys.exit(1)
    print("\nTest alert sent. Check Telegram and/or inbox.")
    print("Weekly summary test: python scripts/weekly_telegram_summary.py --test")


if __name__ == "__main__":
    main()
