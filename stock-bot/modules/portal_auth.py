"""Simple user accounts for the shared Streamlit portal (trusted friends / self-hosted)."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from modules.portal_paths import PORTAL_ROOT, resolve_project_root


def db_path() -> Path:
    return PORTAL_ROOT / "users.db"


def _legacy_user_db_paths() -> list[Path]:
    """Older layouts before run_all.py moved under stock-bot/."""
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots.append(exe_dir / "data" / "portal" / "users.db")
    root = resolve_project_root()
    roots.extend(
        [
            root.parent / "data" / "portal" / "users.db",
            root.parent / "stock-bot" / "data" / "portal" / "users.db",
        ]
    )
    seen: set[Path] = set()
    out: list[Path] = []
    for path in roots:
        resolved = path.resolve()
        if resolved not in seen and resolved.is_file():
            seen.add(resolved)
            out.append(resolved)
    return out


def _user_count(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _maybe_migrate_users_db() -> None:
    """If current users.db is empty, copy from a legacy portal database."""
    target = db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        try:
            with _connect() as conn:
                if _user_count(conn) > 0:
                    return
        except sqlite3.Error:
            pass
    for legacy in _legacy_user_db_paths():
        if legacy.resolve() == target.resolve():
            continue
        try:
            with sqlite3.connect(legacy) as conn:
                if _user_count(conn) <= 0:
                    continue
        except sqlite3.Error:
            continue
        shutil.copy2(legacy, target)
        return


def _connect() -> sqlite3.Connection:
    PORTAL_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    _maybe_migrate_users_db()
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 260_000
    ).hex()
    return f"{salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = _hash_password(password, salt).split("$", 1)[1]
    return secrets.compare_digest(digest, check)


def registration_allowed(invite_code: str) -> bool:
    if os.getenv("PORTAL_ALLOW_OPEN_REGISTRATION", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        return True
    required = os.getenv("PORTAL_INVITE_CODE", "").strip()
    if not required:
        return False
    return secrets.compare_digest(invite_code.strip(), required)


def register_user(username: str, password: str, *, invite_code: str = "") -> tuple[bool, str]:
    init_db()
    username = (username or "").strip().lower()
    if not username or len(username) < 3:
        return False, "Username must be at least 3 characters."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if not registration_allowed(invite_code):
        return False, "Invalid invite code."
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (
                    username,
                    _hash_password(password),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return True, "Account created."
    except sqlite3.IntegrityError:
        return False, "Username already exists."


def authenticate(username: str, password: str) -> tuple[bool, str | None]:
    init_db()
    username = (username or "").strip().lower()
    with _connect() as conn:
        row = conn.execute(
            "SELECT username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not _verify_password(password, row["password_hash"]):
        return False, None
    return True, row["username"]
