"""Tiny desktop launcher: open Paper SoT dashboard (no .lnk arrow).

Must match launch_paper_book.bat exactly so the window looks the same.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _stock_bot_root() -> Path:
    env = (os.environ.get("PYTHONTRADING_ROOT") or "").strip()
    if env:
        p = Path(env)
        if (p / "dashboard_app.py").is_file() and (p / "launch_paper_book.bat").is_file():
            return p
    known = Path(r"C:\Users\Owner\PythonTrading\stock-bot")
    if (known / "dashboard_app.py").is_file():
        return known
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    for c in (here.parent, here.parent.parent):
        if (c / "dashboard_app.py").is_file():
            return c
    return known


def main() -> int:
    root = _stock_bot_root()
    bat = root / "launch_paper_book.bat"
    log = Path(sys.executable).resolve().parent / "PythonTradingPaper_launch_error.txt"
    if not bat.is_file():
        log.write_text(f"Missing launch_paper_book.bat under {root}\n", encoding="utf-8")
        return 1

    # Same as double-clicking the bat: `start` + launch_paper_book.bat
    # (avoids DETACHED_PROCESS / wrong-python differences that change the UI).
    subprocess.Popen(
        ["cmd.exe", "/c", "start", "", str(bat)],
        cwd=str(root),
        env={**os.environ, "PYTHONTRADING_ROOT": str(root), "DASHBOARD_USE_FROZEN": "false"},
        shell=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
