"""Open markdown reports in PyCharm when available (Windows Task Scheduler safe).

``os.startfile`` / file-association can fail silently under scheduled tasks or when
another editor (e.g. Cursor) has touched the shell. Prefer launching ``pycharm64.exe``
directly, then fall back to the OS association.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def find_pycharm_exe() -> Path | None:
    """Resolve PyCharm binary: env override, then newest JetBrains install."""
    for key in ("PYCHARM_EXE", "WEEKLY_REVIEW_PYCHARM", "FREEZE_OPS_PYCHARM"):
        raw = (os.getenv(key) or "").strip().strip('"')
        if raw:
            p = Path(raw)
            if p.is_file():
                return p
    jet = Path(r"C:\Program Files\JetBrains")
    if jet.is_dir():
        found = sorted(jet.glob("PyCharm */bin/pycharm64.exe"), reverse=True)
        if found:
            return found[0]
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "PyCharm" / "bin" / "pycharm64.exe"
    if local.is_file():
        return local
    return None


def open_markdown_report(path: Path, *, log_prefix: str = "[open_markdown]") -> None:
    """Open ``path`` in PyCharm if found; else OS default association."""
    abs_path = path.resolve()
    if not abs_path.is_file():
        print(f"{log_prefix} Missing file: {abs_path}", flush=True)
        return
    try:
        if sys.platform == "win32":
            pycharm = find_pycharm_exe()
            if pycharm is not None:
                flags = 0
                flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
                flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                subprocess.Popen(
                    [str(pycharm), str(abs_path)],
                    cwd=str(abs_path.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=flags,
                )
                print(f"{log_prefix} Opened via PyCharm: {abs_path}", flush=True)
                return
            os.startfile(str(abs_path))  # type: ignore[attr-defined]
            print(f"{log_prefix} Opened via startfile: {abs_path}", flush=True)
            return
        if sys.platform == "darwin":
            subprocess.run(["open", str(abs_path)], check=False)
        else:
            subprocess.run(["xdg-open", str(abs_path)], check=False)
        print(f"{log_prefix} Opened {abs_path}", flush=True)
    except Exception as exc:
        print(f"{log_prefix} Could not open report: {exc}", flush=True)
