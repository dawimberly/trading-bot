"""Robust .env discovery for stock-bot (CWD-independent)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
LOCAL_ENV = PROJECT_ROOT / ".env"
REPO_ENV = REPO_ROOT / ".env"

_loaded_paths: list[Path] = []


def load_project_dotenv(*, force: bool = False) -> list[Path]:
    """Load stock-bot/.env then repo-root .env (fill missing keys only).

    Order:
      1. PYTHONTRADING_ENV_FILE (if set)
      2. stock-bot/.env (override)
      3. repo-root .env (no override — fills gaps)
    """
    global _loaded_paths
    if _loaded_paths and not force:
        return list(_loaded_paths)

    paths: list[Path] = []
    override_file = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if override_file and os.path.isfile(override_file):
        load_dotenv(override_file, override=True)
        paths.append(Path(override_file))

    if LOCAL_ENV.is_file():
        load_dotenv(LOCAL_ENV, override=True)
        paths.append(LOCAL_ENV)

    if REPO_ENV.is_file() and REPO_ENV not in paths:
        load_dotenv(REPO_ENV, override=False)
        paths.append(REPO_ENV)

    _loaded_paths = paths
    return list(paths)


def ensure_dotenv_loaded(*, force: bool = False) -> list[Path]:
    """Idempotent wrapper used by entry points and alpaca_client."""
    return load_project_dotenv(force=force)


def dotenv_search_paths() -> list[Path]:
    """Paths checked for .env (for diagnostics)."""
    out: list[Path] = []
    override_file = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if override_file:
        out.append(Path(override_file))
    out.extend([LOCAL_ENV, REPO_ENV])
    return out
