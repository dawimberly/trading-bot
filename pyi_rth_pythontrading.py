"""PyInstaller runtime hook — set PYTHONTRADING_ROOT before any portal imports."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_project_root() -> Path:
    candidate = Path(sys.executable).resolve().parent
    for _ in range(6):
        if (candidate / "run_all.py").is_file():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return Path(sys.executable).resolve().parent


if getattr(sys, "frozen", False):
    os.environ.setdefault("PYTHONTRADING_ROOT", str(_find_project_root()))
