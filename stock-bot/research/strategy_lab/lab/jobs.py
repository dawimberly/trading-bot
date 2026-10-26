"""Job registry: one owner, pause and cancel between candidates, no partial rankings."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lab import paths
from lab.duplicates import DuplicateSearch, find_duplicate
from lab.specs import spec_hash, validate_spec
from lab.store import connect, owner_id, transaction, utcnow

ACTIVE = ("queued", "running", "paused")


def _rollback(conn) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def code_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(paths.LAB / "lab").glob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def enqueue(spec: dict, *, calendar: list[str], snapshot_id: str, candidates_total: int) -> str:
    normalized = validate_spec(spec, calendar)
    duplicate = find_duplicate(normalized, calendar)
    if duplicate is not None:
        raise DuplicateSearch(duplicate)
    digest = spec_hash(normalized)
    conn = connect()
    try:
        transaction(conn)
        clash = conn.execute(
            "SELECT id, status FROM jobs WHERE config_hash = ? AND status IN ('queued', 'running', 'paused')",
            (digest,),
        ).fetchone()
        if clash:
            raise RuntimeError(f"duplicate job {clash['id']} is already {clash['status']}")
        experiment_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        now = utcnow()
        conn.execute(
            "INSERT INTO experiments (id, name, created_at, spec_json, spec_hash, data_role) VALUES (?, ?, ?, ?, ?, ?)",
            (experiment_id, normalized.get("name") or experiment_id, now, json.dumps(normalized), digest, "development"),
        )
        conn.execute(
            """INSERT INTO jobs (
                id, experiment_id, status, created_at, budget_seconds, seed, config_hash,
                code_version, data_snapshot_id, candidates_total, candidates_done
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                job_id,
                experiment_id,
                now,
                float(normalized["budget_hours"]) * 3600.0,
                int(normalized.get("seed") or 1),
                digest,
                code_version(),
                snapshot_id,
                int(candidates_total),
            ),
        )
        conn.execute("COMMIT")
        return job_id
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def set_control(job_id: str, action: str) -> None:
    conn = connect()
    try:
        transaction(conn)
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            _rollback(conn)
            raise KeyError(job_id)
        if action == "pause" and row["status"] in ("queued", "running"):
            conn.execute("UPDATE jobs SET status = 'paused' WHERE id = ?", (job_id,))
        elif action == "resume" and row["status"] == "paused":
            conn.execute("UPDATE jobs SET status = 'queued', owner = NULL WHERE id = ?", (job_id,))
        elif action == "cancel" and row["status"] in ACTIVE:
            conn.execute("UPDATE jobs SET cancel_requested = 1, status = 'cancelled' WHERE id = ? AND status = 'queued'", (job_id,))
            conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ? AND status IN ('running', 'paused')", (job_id,))
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _stale_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()


def claim(owner: str | None = None) -> dict | None:
    owner = owner or owner_id()
    conn = connect()
    try:
        transaction(conn)
        stale = conn.execute(
            "SELECT id, heartbeat_at FROM jobs WHERE status = 'running'"
        ).fetchall()
        for item in stale:
            stamp = item["heartbeat_at"]
            old = stamp is None or stamp < _stale_cutoff()
            if old:
                conn.execute("UPDATE jobs SET status = 'queued', owner = NULL WHERE id = ? AND status = 'running'", (item["id"],))
        busy = conn.execute("SELECT id FROM jobs WHERE status IN ('running', 'paused')").fetchone()
        if busy is not None:
            conn.execute("COMMIT")
            return None
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            _rollback(conn)
            return None
        now = utcnow()
        updated = conn.execute(
            "UPDATE jobs SET status = 'running', owner = ?, started_at = COALESCE(started_at, ?), heartbeat_at = ? WHERE id = ? AND status = 'queued'",
            (owner, now, now, row["id"]),
        ).rowcount
        conn.execute("COMMIT")
        if updated != 1:
            return None
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def heartbeat(job_id: str, owner: str) -> dict | None:
    conn = connect()
    try:
        transaction(conn)
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND owner = ?", (job_id, owner)).fetchone()
        if row is None:
            _rollback(conn)
            return None
        conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (utcnow(), job_id))
        conn.execute("COMMIT")
        return dict(row)
    finally:
        conn.close()


def record_candidate(
    job_id: str,
    ordinal: int,
    phase: str,
    params: dict,
    status: str,
    metrics: dict | None,
    error: str | None,
    *,
    comparable: bool,
    folds_completed: int,
    folds_required: int,
    pilot_seconds: float | None = None,
    eta_seconds: float | None = None,
) -> None:
    conn = connect()
    try:
        transaction(conn)
        conn.execute(
            """INSERT INTO candidates (
                job_id, ordinal, phase, params_json, status, comparable, folds_completed, folds_required, metrics_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, ordinal, phase) DO UPDATE SET
                status = excluded.status,
                comparable = excluded.comparable,
                folds_completed = excluded.folds_completed,
                folds_required = excluded.folds_required,
                metrics_json = excluded.metrics_json,
                error = excluded.error
            """,
            (
                job_id,
                ordinal,
                phase,
                json.dumps(params),
                status,
                1 if comparable else 0,
                folds_completed,
                folds_required,
                json.dumps(metrics) if metrics is not None else None,
                error,
            ),
        )
        done = conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE job_id = ? AND status IN ('complete', 'failed')",
            (job_id,),
        ).fetchone()["n"]
        conn.execute(
            """UPDATE jobs SET candidates_done = ?, checkpoint_json = ?, heartbeat_at = ?,
               pilot_seconds = COALESCE(?, pilot_seconds), eta_seconds = COALESCE(?, eta_seconds)
               WHERE id = ?""",
            (done, json.dumps({"last_ordinal": ordinal, "phase": phase}), utcnow(), pilot_seconds, eta_seconds, job_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def finish(job_id: str, status: str, error: str | None = None, *, pilot_seconds: float | None = None, eta_seconds: float | None = None) -> None:
    conn = connect()
    try:
        transaction(conn)
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, error = ?, owner = NULL, pilot_seconds = COALESCE(?, pilot_seconds), eta_seconds = COALESCE(?, eta_seconds) WHERE id = ?",
            (status, utcnow(), error, pilot_seconds, eta_seconds, job_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def mark_incomplete(job_id: str, ordinal: int, phase: str, params: dict, reason: str, folds_required: int) -> None:
    record_candidate(
        job_id,
        ordinal,
        phase,
        params,
        "incomplete",
        None,
        reason,
        comparable=False,
        folds_completed=0,
        folds_required=folds_required,
    )


def list_jobs() -> list[dict]:
    conn = connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY created_at DESC")]
    finally:
        conn.close()


def job_detail(job_id: str) -> dict | None:
    conn = connect()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return None
        experiment = conn.execute("SELECT * FROM experiments WHERE id = ?", (job["experiment_id"],)).fetchone()
        rows = [dict(row) for row in conn.execute("SELECT * FROM candidates WHERE job_id = ? ORDER BY phase, ordinal", (job_id,))]
        arts = [dict(row) for row in conn.execute("SELECT * FROM artifacts WHERE job_id = ?", (job_id,))]
        return {"job": dict(job), "experiment": dict(experiment), "candidates": rows, "artifacts": arts}
    finally:
        conn.close()


def add_artifact(job_id: str, kind: str, path: str) -> None:
    conn = connect()
    try:
        transaction(conn)
        conn.execute(
            "INSERT INTO artifacts (job_id, kind, path) VALUES (?, ?, ?) ON CONFLICT(job_id, kind) DO UPDATE SET path = excluded.path",
            (job_id, kind, path),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
