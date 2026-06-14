"""Detect stale models vs refreshed features / ufcstats enrichment."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)


def _file_mtime(path: Path) -> float | None:
    if not path.is_file():
        return None
    return path.stat().st_mtime


def features_fingerprint(features_path: Path | None = None) -> str:
    """Hash features CSV size + mtime for change detection."""
    path = features_path or config.PROCESSED_FEATURES_CSV
    if not path.is_file():
        return ""
    stat = path.stat()
    payload = f"{path.name}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def enrichment_timestamp() -> datetime | None:
    meta_path = config.UFCSTATS_ENRICH_META_PATH
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        ts = meta.get("updated_at")
        if ts:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    fights_mtime = _file_mtime(config.RAW_FIGHTS_CSV)
    if fights_mtime:
        return datetime.fromtimestamp(fights_mtime, tz=timezone.utc)
    return None


def model_artifact_path() -> Path | None:
    if config.DEFAULT_MODEL_PATH.is_file():
        return config.DEFAULT_MODEL_PATH
    if config.LEGACY_MODEL_PATH.is_file():
        return config.LEGACY_MODEL_PATH
    return None


def load_model_metadata() -> dict[str, Any]:
    path = model_artifact_path()
    if path is None:
        return {}
    try:
        import joblib

        artifact = joblib.load(path)
        if isinstance(artifact, dict):
            return {
                "trained_at": artifact.get("trained_at"),
                "features_fingerprint": artifact.get("features_fingerprint"),
                "feature_rows": artifact.get("feature_rows"),
                "enrichment_at": artifact.get("enrichment_at"),
            }
    except Exception as exc:
        logger.debug("Could not read model metadata: %s", exc)
    mtime = _file_mtime(path)
    return {
        "trained_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        if mtime
        else None
    }


def model_training_timestamp() -> datetime | None:
    meta = load_model_metadata()
    raw = meta.get("trained_at")
    if not raw:
        path = model_artifact_path()
        mtime = _file_mtime(path) if path else None
        return datetime.fromtimestamp(mtime, tz=timezone.utc) if mtime else None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def model_needs_retrain(
    *,
    force: bool = False,
    features_path: Path | None = None,
) -> tuple[bool, str]:
    """Return (should_retrain, human-readable reason)."""
    if force:
        return True, "--force-retrain requested"

    if model_artifact_path() is None:
        return True, "no trained model on disk"

    current_fp = features_fingerprint(features_path)
    meta = load_model_metadata()
    saved_fp = str(meta.get("features_fingerprint") or "")

    if current_fp and saved_fp and current_fp != saved_fp:
        return True, "feature matrix changed since last train"

    features_mtime = _file_mtime(features_path or config.PROCESSED_FEATURES_CSV)
    model_mtime = _file_mtime(model_artifact_path() or config.DEFAULT_MODEL_PATH)
    if features_mtime and model_mtime and features_mtime > model_mtime:
        return True, "features newer than model file"

    enrich_ts = enrichment_timestamp()
    train_ts = model_training_timestamp()
    if enrich_ts and train_ts and enrich_ts > train_ts:
        return True, "ufcstats enrichment newer than model"

    fights_mtime = _file_mtime(config.RAW_FIGHTS_CSV)
    if fights_mtime and model_mtime and fights_mtime > model_mtime:
        return True, "fights.csv newer than model"

    return False, ""


def stale_model_warning() -> str | None:
    """Warn when model predates enrichment but caller did not retrain."""
    needs, reason = model_needs_retrain()
    if not needs:
        return None
    train_ts = model_training_timestamp()
    enrich_ts = enrichment_timestamp()
    parts = [f"Model may be stale: {reason}."]
    if train_ts:
        parts.append(f"Last trained: {train_ts.date().isoformat()}.")
    if enrich_ts:
        parts.append(f"Last enrichment: {enrich_ts.date().isoformat()}.")
    parts.append("Re-run with --train or --force-retrain.")
    return " ".join(parts)
