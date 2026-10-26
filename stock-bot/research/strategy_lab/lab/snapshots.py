"""Immutable snapshot identity. The database file is opened read-only."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from lab.paths import ABC_MANIFEST, BARS, LAB_DATA, PAYMENTS, SNAPSHOT_CACHE


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identify(refresh: bool = False) -> dict:
    """Hash the bar snapshot. Reuse the hash when size and mtime are unchanged."""
    stat = BARS.stat()
    LAB_DATA.mkdir(parents=True, exist_ok=True)
    if SNAPSHOT_CACHE.is_file() and not refresh:
        cached = json.loads(SNAPSHOT_CACHE.read_text(encoding="utf-8"))
        if cached.get("size") == stat.st_size and cached.get("mtime_ns") == stat.st_mtime_ns:
            return cached
    manifest = json.loads(ABC_MANIFEST.read_text(encoding="utf-8"))
    payload = {
        "snapshot_id": file_sha256(BARS),
        "path": str(BARS),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "payments_sha256": file_sha256(PAYMENTS),
        "provenance": {
            "source": manifest.get("source"),
            "feed": manifest.get("feed"),
            "adjustment": manifest.get("adjustment"),
            "dividend_policy": manifest.get("dividend_policy"),
            "retrieved_at": manifest.get("retrieved_at"),
            "session_cutoff": manifest.get("session_cutoff"),
            "market_data_db_written": manifest.get("market_data_db_written"),
        },
    }
    SNAPSHOT_CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def connect_readonly() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{BARS.as_posix()}?mode=ro", uri=True)
