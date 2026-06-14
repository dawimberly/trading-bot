"""Send Kraken phone playbook (Telegram + email).

  python scripts/kraken_send_playbook.py --dry-run
  python scripts/kraken_send_playbook.py --slot morning --force
  python scripts/kraken_send_playbook.py --slot evening --force
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from modules import alerts
from modules.kraken_advisor import build_playbook

STATE_FILE = ROOT / "kraken_playbook_state.json"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _already_sent(slot: str) -> bool:
    state = _load_state()
    return state.get(f"last_{slot}_date") == date.today().isoformat()


def _mark_sent(slot: str) -> None:
    state = _load_state()
    state[f"last_{slot}_date"] = date.today().isoformat()
    state[f"last_{slot}_at"] = datetime.now().isoformat(timespec="seconds")
    _save_state(state)


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Kraken phone playbook")
    parser.add_argument("--slot", choices=("morning", "evening"), default="morning")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Send even if already sent today")
    args = parser.parse_args()

    if not args.force and _already_sent(args.slot):
        print(f"Already sent {args.slot} playbook today. Use --force to resend.")
        return

    subject, body = build_playbook(slot=args.slot)
    _safe_print(subject)
    _safe_print("=" * 40)
    _safe_print(body)

    if args.dry_run:
        print("\n(dry-run - not sent)")
        return

    if not alerts.alerts_configured():
        print("\nNo alerts configured. Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID or SMTP in .env")
        return

    ok = alerts.broadcast(subject, body)
    if ok:
        _mark_sent(args.slot)
        print("\nSent via Telegram and/or email.")
    else:
        print("\nSend failed - check Telegram/email settings.")


if __name__ == "__main__":
    main()
