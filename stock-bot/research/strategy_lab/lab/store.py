"""SQLite registry. One transaction owns a running job."""

from __future__ import annotations

import socket
import sqlite3
from datetime import datetime, timezone

from lab.paths import database_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    data_role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    status TEXT NOT NULL,
    owner TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    heartbeat_at TEXT,
    budget_seconds REAL NOT NULL,
    seed INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    code_version TEXT NOT NULL,
    data_snapshot_id TEXT NOT NULL,
    candidates_total INTEGER NOT NULL,
    candidates_done INTEGER NOT NULL DEFAULT 0,
    pilot_seconds REAL,
    eta_seconds REAL,
    checkpoint_json TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candidates (
    job_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    phase TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,
    comparable INTEGER NOT NULL,
    folds_completed INTEGER NOT NULL,
    folds_required INTEGER NOT NULL,
    metrics_json TEXT,
    error TEXT,
    PRIMARY KEY (job_id, ordinal, phase)
);
CREATE TABLE IF NOT EXISTS artifacts (
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (job_id, kind)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def owner_id() -> str:
    return f"{socket.gethostname()}:{__import__('os').getpid()}"


def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    return conn
