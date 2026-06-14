"""Cloud bot settings — paths, logging, deployment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent


@dataclass(frozen=True)
class CloudSettings:
    profile: str
    dry_run: bool
    paper_trading: bool
    data_dir: Path
    log_dir: Path
    heartbeat_file: Path
    journal_csv: Path
    repo_root: Path
    cycle_sec: int
    run_all_script: Path
    pid_file: Path

    @property
    def enabled(self) -> bool:
        return os.getenv("CLOUD_BOT_DRY_RUN", "true").lower() not in (
            "1",
            "true",
            "yes",
        )


def load_settings() -> CloudSettings:
    data = ROOT / "data"
    data.mkdir(parents=True, exist_ok=True)
    log_dir = data / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return CloudSettings(
        profile=os.getenv("CLOUD_BOT_PROFILE", "paper_aggressive"),
        dry_run=os.getenv("CLOUD_BOT_DRY_RUN", "true").lower() in ("1", "true", "yes"),
        paper_trading=os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes"),
        data_dir=data,
        log_dir=log_dir,
        heartbeat_file=data / "cloud_bot_heartbeat.json",
        journal_csv=data / "cloud_bot_journal.csv",
        pid_file=data / "cloud_bot.pid",
        repo_root=REPO_ROOT,
        cycle_sec=int(os.getenv("CLOUD_BOT_CYCLE_SEC", os.getenv("PAPER_CHASE_CYCLE_SEC", "45"))),
        run_all_script=REPO_ROOT / "run_all.py",
    )
