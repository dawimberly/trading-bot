"""Find your Telegram chat id for alert setup.

EASIEST (no script): In Telegram, message @userinfobot — it replies with your Id.
                     Use that number as TELEGRAM_CHAT_ID in .env.

Or:
  1. Message YOUR bot (not BotFather) — tap Start or send hello
  2. python scripts/account/get_telegram_chat_id.py
  3. python scripts/account/get_telegram_chat_id.py --wait   # polls 90 seconds
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config


def _api(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _load_token():
    token = __import__("os").getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        tg = config.get_telegram_config()
        token = tg[0] if tg else ""
    return token


def _extract_chats(updates):
    chats = []
    for u in updates:
        for key in ("message", "edited_message", "channel_post", "callback_query"):
            block = u.get(key)
            if not block:
                continue
            chat = block.get("chat") if key != "callback_query" else block.get("message", {}).get("chat")
            if not chat:
                if key == "callback_query":
                    user = block.get("from", {})
                    if user.get("id"):
                        chats.append(
                            {
                                "id": user["id"],
                                "name": user.get("first_name", "?"),
                                "type": "private (from button)",
                                "text": "(button press)",
                            }
                        )
                continue
            text = (block.get("text") or "")[:40]
            chats.append(
                {
                    "id": chat["id"],
                    "name": chat.get("first_name") or chat.get("title") or chat.get("username") or "?",
                    "type": chat.get("type", "?"),
                    "text": text,
                }
            )
    return chats


def fetch_updates(token):
    return _api(token, "getUpdates", {"limit": 20})


def print_banner():
    print()
    print("=" * 60)
    print("  NOT BotFather's /mybots list — that is your bot list.")
    print("  You need the numeric CHAT ID from messaging YOUR bot.")
    print("=" * 60)
    print()
    print("Easiest shortcut: Telegram -> search @userinfobot -> Start")
    print("It replies with 'Id: 123456789' — use that in .env")
    print()


def main():
    parser = argparse.ArgumentParser(description="Get Telegram chat id for alerts")
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait up to 90s while you message your bot",
    )
    args = parser.parse_args()

    print_banner()

    token = _load_token()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN missing in .env")
        sys.exit(1)
    if token.count(":") != 1:
        print("ERROR: Token must be bot_id:secret (ONE colon only, from BotFather API Token)")
        sys.exit(1)

    try:
        me = _api(token, "getMe")
    except urllib.error.URLError as e:
        print(f"ERROR: Bad token or network issue: {e}")
        sys.exit(1)

    if not me.get("ok"):
        print("ERROR: Telegram rejected token:", me)
        sys.exit(1)

    username = me["result"].get("username", "?")
    print(f"Your bot: @{username}")
    print(f"Now open Telegram, search @{username}, tap Start, send: hello")
    print()

    attempts = 30 if args.wait else 1
    for attempt in range(attempts):
        if args.wait and attempt > 0:
            print(f"  waiting... ({attempt * 3}s) — message @{username} if you have not yet")
            time.sleep(3)

        try:
            data = fetch_updates(token)
        except urllib.error.URLError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

        updates = data.get("result", [])
        chats = _extract_chats(updates)

        if chats:
            latest = chats[-1]
            print()
            print("*" * 60)
            print("  COPY THIS INTO .env")
            print()
            print(f"  TELEGRAM_CHAT_ID={latest['id']}")
            print()
            print(f"  ({latest['type']} chat with {latest['name']})")
            if latest.get("text"):
                print(f"  last message: {latest['text']!r}")
            print("*" * 60)
            print()
            print("Then run: python scripts/account/test_alerts.py")
            return

    print("No messages received by your bot yet.")
    print()
    print("Common mistakes:")
    print("  - Messaging @BotFather instead of @" + username)
    print("  - Token in .env is wrong (re-copy from BotFather -> API Token)")
    print()
    print("Try again with wait mode:")
    print("  python scripts/account/get_telegram_chat_id.py --wait")
    print()
    print("Or use @userinfobot in Telegram and copy the Id number.")


if __name__ == "__main__":
    main()
