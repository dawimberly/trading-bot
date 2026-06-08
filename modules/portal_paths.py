"""Per-user directories for portal (credentials, bot state, logs)."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PORTAL_ROOT = PROJECT_ROOT / "data" / "portal"
USERS_ROOT = PORTAL_ROOT / "users"


def user_dir(username: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in username.lower())
    path = USERS_ROOT / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_env_path(username: str) -> Path:
    return user_dir(username) / ".env"


def user_heartbeat_path(username: str) -> Path:
    return user_dir(username) / "bot_heartbeat.json"


def user_journal_path(username: str) -> Path:
    return user_dir(username) / "paper_journal.csv"


def user_pid_path(username: str) -> Path:
    return user_dir(username) / "bot.pid"


def has_alpaca_config(username: str) -> bool:
    path = user_env_path(username)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    return "APCA_API_KEY_ID=" in text and "APCA_API_SECRET_KEY=" in text


def write_user_env(
    username: str,
    *,
    api_key: str,
    api_secret: str,
    paper: bool,
    allow_live: bool,
    telegram_token: str = "",
    telegram_chat: str = "",
) -> None:
    lines = [
        f"# Portal user: {username}",
        f"APCA_API_KEY_ID={api_key.strip()}",
        f"APCA_API_SECRET_KEY={api_secret.strip()}",
        f"PAPER_TRADING={'true' if paper else 'false'}",
    ]
    if allow_live:
        lines.append("ALLOW_LIVE_TRADING=yes")
    if telegram_token.strip():
        lines.append(f"TELEGRAM_BOT_TOKEN={telegram_token.strip()}")
    if telegram_chat.strip():
        lines.append(f"TELEGRAM_CHAT_ID={telegram_chat.strip()}")
    lines.append("")
    user_env_path(username).write_text("\n".join(lines), encoding="utf-8")
