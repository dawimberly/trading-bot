"""24/7 paper Sharpe-chase bot — aggressive profile, isolated from live ~$100.

Uses run_all.py with PAPER_CHASE_MODE:
  - 20% VTI / 80% active (PAPER_VTI_CORE_PCT)
  - Full active sleeve deployment (PAPER_ACTIVE_SLEEVE_BOOST)
  - Wisdom sizing floor 1.0 (no defensive shrink on paper)
  - Optional wider crypto (PAPER_CRYPTO_VOL_ONLY=false)

Run:
    python run_paper_bot.py

Requires Alpaca **paper** keys (APCA_* + PAPER_TRADING=true) or research book
(PAPER_APCA_* + PAPER_CHASE_USE_RESEARCH_KEYS=yes).

Backtests peak ~1.0–1.8 Sharpe by window — 3.0 is the chase target, not proven.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_ALL = ROOT / "run_all.py"


def main() -> None:
    env = os.environ.copy()
    env.setdefault("PAPER_CHASE_MODE", "1")
    env.setdefault("PAPER_TRADING", "true")
    env.setdefault("PAPER_AGGRESSIVE", "true")
    env.setdefault("HEARTBEAT_FILE", "paper_chase_heartbeat.json")
    env.setdefault("PAPER_JOURNAL_CSV", "paper_chase_journal.csv")

    python = sys.executable
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.is_file():
        python = str(venv_py)

    print("--- Paper Sharpe chase (run_all.py + PAPER_CHASE_MODE) ---")
    print(f"--- Heartbeat: {env['HEARTBEAT_FILE']} | Journal: {env['PAPER_JOURNAL_CSV']} ---")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.run(
        [python, str(RUN_ALL)],
        cwd=str(ROOT),
        env=env,
        creationflags=flags,
    )
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
