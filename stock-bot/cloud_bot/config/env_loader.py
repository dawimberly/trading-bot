"""Load cloud_bot/.env with parent-repo fallback and apply best-paper profile."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from cloud_bot.config.profile import apply_best_paper_profile
from cloud_bot.config.settings import CloudSettings

CLOUD_BOT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CLOUD_BOT_DIR.parent


def load_cloud_dotenv() -> Path | None:
    """
    Load env files in order (later wins on keys):
      1. repo root `.env` — shared Alpaca keys, optional
      2. `cloud_bot/.env` — cloud overrides (required on VPS)
    """
    root_env = REPO_ROOT / ".env"
    cloud_env = CLOUD_BOT_DIR / ".env"
    if root_env.is_file():
        load_dotenv(root_env)
    if cloud_env.is_file():
        load_dotenv(cloud_env, override=True)
        return cloud_env
    return root_env if root_env.is_file() else None


def build_runtime_env(settings: CloudSettings) -> dict[str, str]:
    """Merge process env with best-paper profile and cloud path overrides."""
    return apply_best_paper_profile(
        overrides={
            "CLOUD_BOT_MODE": "1",
            "HEARTBEAT_FILE": str(settings.heartbeat_file),
            "PAPER_JOURNAL_CSV": str(settings.journal_csv),
            "STAT_ARB_BOOK_FILE": str(settings.data_dir / "stat_arb_open_book.json"),
            "PYTHONUNBUFFERED": "1",
        }
    )


def apply_runtime_env(env: dict[str, str]) -> None:
    """Push merged env into os.environ for subprocesses and in-process imports."""
    for key, value in env.items():
        os.environ[key] = value
