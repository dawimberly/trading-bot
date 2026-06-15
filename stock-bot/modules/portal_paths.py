"""Per-user directories for portal (credentials, bot state, logs)."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from modules.trading_books import BOOKS, DEFAULT_BOOK_ID


def _has_run_all(path: Path) -> bool:
    return (path / "run_all.py").is_file()


def _find_run_all_root(start: Path, *, max_depth: int = 8) -> Path | None:
    """Walk parents from start until run_all.py or stock-bot/run_all.py is found."""
    candidate = start.resolve()
    for _ in range(max_depth):
        if _has_run_all(candidate):
            return candidate
        nested = candidate / "stock-bot"
        if _has_run_all(nested):
            return nested
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None


def resolve_project_root() -> Path:
    """Project root for writable data (users.db, desktop_prefs, per-user .env)."""
    override = os.getenv("PYTHONTRADING_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        found = _find_run_all_root(Path(sys.executable).resolve().parent)
        if found is not None:
            return found
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _sync_roots() -> None:
    global PROJECT_ROOT, PORTAL_ROOT, USERS_ROOT, DESKTOP_PREFS_FILE
    PROJECT_ROOT = resolve_project_root()
    PORTAL_ROOT = PROJECT_ROOT / "data" / "portal"
    USERS_ROOT = PORTAL_ROOT / "users"
    DESKTOP_PREFS_FILE = PORTAL_ROOT / "desktop_prefs.json"


def bind_project_root(path: Path | str) -> Path:
    """Pin portal data under this folder (call at app startup before DB/prefs I/O)."""
    resolved = Path(path).resolve()
    os.environ["PYTHONTRADING_ROOT"] = str(resolved)
    _sync_roots()
    return resolved


_sync_roots()


def user_dir(username: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in username.lower())
    path = USERS_ROOT / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def book_dir(username: str, book_id: str) -> Path:
    safe_book = "".join(c if c.isalnum() or c in "-_" else "_" for c in book_id.lower())
    path = user_dir(username) / "books" / safe_book
    path.mkdir(parents=True, exist_ok=True)
    return path


def _legacy_env_prefs(path: Path) -> dict[str, bool]:
    paper, allow_live = True, False
    if not path.is_file():
        return {"paper": paper, "allow_live": allow_live}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("PAPER_TRADING="):
            paper = line.split("=", 1)[1].strip().lower() in ("1", "true", "yes")
        elif line.startswith("ALLOW_LIVE_TRADING="):
            allow_live = line.split("=", 1)[1].strip().lower() in ("1", "true", "yes")
    return {"paper": paper, "allow_live": allow_live}


def _safe_username(username: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in username.lower())


def _env_has_alpaca_keys(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "APCA_API_KEY_ID=" in text and "APCA_API_SECRET_KEY=" in text


def _portal_root_candidates() -> list[Path]:
    """Portal data dirs to search when exe layout differs from stock-bot source tree."""
    seen: set[Path] = set()
    out: list[Path] = []
    candidates: list[Path] = [PORTAL_ROOT]

    if getattr(sys, "frozen", False):
        exe_parent = Path(sys.executable).resolve().parent
        candidates.append(exe_parent / "data" / "portal")
        run_all_root = _find_run_all_root(exe_parent)
        if run_all_root is not None:
            candidates.append(run_all_root / "data" / "portal")

    candidates.extend(
        (
            PROJECT_ROOT.parent / "stock-bot" / "data" / "portal",
            PROJECT_ROOT.parent / "data" / "portal",
            PROJECT_ROOT.parent.parent / "stock-bot" / "data" / "portal",
        )
    )
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _find_legacy_book_env(username: str, book_id: str) -> Path | None:
    safe = _safe_username(username)
    for portal in _portal_root_candidates():
        book_env = portal / "users" / safe / "books" / book_id / ".env"
        if _env_has_alpaca_keys(book_env):
            return book_env
    for portal in _portal_root_candidates():
        flat = portal / "users" / safe / ".env"
        if not _env_has_alpaca_keys(flat):
            continue
        prefs = _legacy_env_prefs(flat)
        expected = "alpaca_paper" if prefs["paper"] else "alpaca_live"
        if expected == book_id:
            return flat
    return None


def ensure_book_env(username: str, book_id: str) -> Path:
    """Ensure per-book .env exists under PORTAL_ROOT; copy from stock-bot/legacy if missing."""
    migrate_user_to_books(username)
    target = book_env_path(username, book_id)
    if _env_has_alpaca_keys(target):
        return target
    legacy = _find_legacy_book_env(username, book_id)
    if legacy is not None and legacy.resolve() != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
    return target


def migrate_user_to_books(username: str) -> None:
    """Move legacy flat user files into books/alpaca_live or books/alpaca_paper."""
    ud = user_dir(username)
    legacy_env = ud / ".env"
    if not legacy_env.is_file():
        return
    prefs = _legacy_env_prefs(legacy_env)
    book_id = "alpaca_paper" if prefs["paper"] else "alpaca_live"
    bd = book_dir(username, book_id)
    if (bd / ".env").is_file():
        return
    for name in (
        ".env",
        "bot_heartbeat.json",
        "paper_journal.csv",
        "bot.pid",
        "bot.log",
        "wisdom_scorecard.json",
        "wisdom_journal.csv",
    ):
        src = ud / name
        if src.is_file():
            shutil.copy2(src, bd / name)


def book_env_path(username: str, book_id: str) -> Path:
    return book_dir(username, book_id) / ".env"


def book_heartbeat_path(username: str, book_id: str) -> Path:
    return book_dir(username, book_id) / "bot_heartbeat.json"


def book_journal_path(username: str, book_id: str) -> Path:
    return book_dir(username, book_id) / "paper_journal.csv"


def book_scorecard_path(username: str, book_id: str) -> Path:
    return book_dir(username, book_id) / "wisdom_scorecard.json"


def legacy_scorecard_path() -> Path:
    return PROJECT_ROOT / "wisdom_scorecard.json"


def legacy_journal_path() -> Path:
    return PROJECT_ROOT / "paper_journal.csv"


def ensure_book_journal(username: str, book_id: str) -> Path:
    """Create per-book trade journal with CSV header if missing."""
    path = book_journal_path(username, book_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 0:
        return path
    from modules.trade_journal import JOURNAL_FIELDS

    with open(path, "w", newline="", encoding="utf-8") as f:
        import csv

        csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writeheader()
    return path


def book_pid_path(username: str, book_id: str) -> Path:
    return book_dir(username, book_id) / "bot.pid"


def book_bot_log_path(username: str, book_id: str) -> Path:
    return book_dir(username, book_id) / "bot.log"


def user_env_path(username: str) -> Path:
    """Legacy path — portal web UI default book (paper)."""
    return book_env_path(username, "alpaca_paper")


def user_heartbeat_path(username: str) -> Path:
    return book_heartbeat_path(username, "alpaca_paper")


def user_journal_path(username: str) -> Path:
    return book_journal_path(username, "alpaca_paper")


def user_pid_path(username: str) -> Path:
    return book_pid_path(username, "alpaca_paper")


def user_bot_log_path(username: str) -> Path:
    return book_bot_log_path(username, "alpaca_paper")


def has_alpaca_config(username: str, book_id: str | None = None) -> bool:
    migrate_user_to_books(username)
    if book_id:
        return _env_has_alpaca_keys(ensure_book_env(username, book_id))
    for bid in BOOKS:
        if BOOKS[bid].get("enabled") and has_alpaca_config(username, bid):
            return True
    legacy = user_dir(username) / ".env"
    return _env_has_alpaca_keys(legacy)


def read_desktop_prefs() -> dict:
    PORTAL_ROOT.mkdir(parents=True, exist_ok=True)
    if not DESKTOP_PREFS_FILE.is_file():
        return {}
    try:
        with open(DESKTOP_PREFS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def write_desktop_prefs(prefs: dict) -> None:
    PORTAL_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = DESKTOP_PREFS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)
        f.write("\n")
    tmp.replace(DESKTOP_PREFS_FILE)


def remember_username_enabled() -> bool:
    return read_desktop_prefs().get("remember_username") is not False


def get_last_username() -> str:
    prefs = read_desktop_prefs()
    if prefs.get("remember_username") is False:
        return ""
    return str(prefs.get("last_username") or "").strip()


def save_last_username(username: str, *, remember: bool = True) -> None:
    prefs = read_desktop_prefs()
    if remember:
        prefs["last_username"] = username.strip().lower()
        prefs["remember_username"] = True
    else:
        prefs.pop("last_username", None)
        prefs["remember_username"] = False
    write_desktop_prefs(prefs)


def get_last_book_id() -> str:
    prefs = read_desktop_prefs()
    book_id = str(prefs.get("last_book_id") or DEFAULT_BOOK_ID).strip()
    if book_id in BOOKS and BOOKS[book_id].get("enabled"):
        return book_id
    return DEFAULT_BOOK_ID


def save_last_book_id(book_id: str) -> None:
    if book_id not in BOOKS:
        return
    prefs = read_desktop_prefs()
    prefs["last_book_id"] = book_id
    write_desktop_prefs(prefs)


def env_flags_for_book(book_id: str) -> dict[str, bool]:
    """Fixed paper/live flags per trading book — not user-toggleable."""
    from modules.trading_books import default_env_prefs

    return default_env_prefs(book_id)


def read_user_env_key_hint(username: str, book_id: str) -> str:
    """Last 4 chars of saved API key id (for edit screen confirmation)."""
    migrate_user_to_books(username)
    path = book_env_path(username, book_id)
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("APCA_API_KEY_ID="):
            key_id = line.split("=", 1)[1].strip()
            if len(key_id) >= 4:
                return f"…{key_id[-4:]}"
            return "…" if key_id else ""
    return ""


def read_user_env_prefs(username: str, book_id: str | None = None) -> dict[str, bool]:
    """Paper vs live flags from saved .env (no secrets)."""
    migrate_user_to_books(username)
    if book_id is None:
        book_id = "alpaca_paper"
    path = book_env_path(username, book_id)
    if path.is_file():
        return _legacy_env_prefs(path)
    return env_flags_for_book(book_id)


def write_user_env(
    username: str,
    *,
    api_key: str,
    api_secret: str,
    paper: bool,
    allow_live: bool,
    telegram_token: str = "",
    telegram_chat: str = "",
    book_id: str = "alpaca_paper",
) -> None:
    lines = [
        f"# Portal user: {username} | book: {book_id}",
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
    book_env_path(username, book_id).write_text("\n".join(lines), encoding="utf-8")
