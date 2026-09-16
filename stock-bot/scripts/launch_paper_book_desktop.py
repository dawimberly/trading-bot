"""Tiny desktop launcher: open Paper SoT dashboard with no console window."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _stock_bot_root() -> Path:
    env = (os.environ.get("PYTHONTRADING_ROOT") or "").strip()
    if env:
        p = Path(env)
        if (p / "dashboard_app.py").is_file():
            return p
    known = Path(r"C:\Users\Owner\PythonTrading\stock-bot")
    if (known / "dashboard_app.py").is_file():
        return known
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    for c in (here.parent, here.parent.parent):
        if (c / "dashboard_app.py").is_file():
            return c
    return known


def _pythonw(root: Path) -> Path:
    """Prefer real pythonw.exe (base install), not a console python stub."""
    venv_py = None
    for cand in (
        root.parent / "venv311" / "Scripts" / "python.exe",
        root / "venv311" / "Scripts" / "python.exe",
        root / ".venv" / "Scripts" / "python.exe",
        root.parent / ".venv" / "Scripts" / "python.exe",
    ):
        if cand.is_file():
            venv_py = cand
            break
    if venv_py is not None:
        try:
            out = subprocess.check_output(
                [
                    str(venv_py),
                    "-c",
                    "import sys; from pathlib import Path; print(Path(sys.base_prefix) / 'pythonw.exe')",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).strip()
            base_w = Path(out)
            if base_w.is_file():
                return base_w
        except (OSError, subprocess.CalledProcessError):
            pass
        sibling = venv_py.with_name("pythonw.exe")
        if sibling.is_file():
            return sibling
    found = shutil.which("pythonw.exe") or shutil.which("pythonw")
    if found:
        return Path(found)
    return Path("pythonw.exe")


def main() -> int:
    root = _stock_bot_root()
    script = root / "dashboard_app.py"
    log = Path(sys.executable).resolve().parent / "PythonTradingPaper_launch_error.txt"
    if not script.is_file():
        log.write_text(f"Missing dashboard_app.py under {root}\n", encoding="utf-8")
        return 1

    pyw = _pythonw(root)
    env = os.environ.copy()
    env["PYTHONTRADING_ROOT"] = str(root)
    env["DASHBOARD_USE_FROZEN"] = "false"
    # Site-packages from the project venv when using base pythonw.
    for site in (
        root.parent / "venv311" / "Lib" / "site-packages",
        root / "venv311" / "Lib" / "site-packages",
        root / ".venv" / "Lib" / "site-packages",
    ):
        if site.is_dir():
            env["PYTHONPATH"] = str(site)
            env["VIRTUAL_ENV"] = str(site.parent.parent)
            break

    flags = 0
    if sys.platform == "win32":
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen(
            [str(pyw), str(script), "--book", "alpaca_paper_v2"],
            cwd=str(root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        log.write_text(f"Failed to start {pyw} {script}: {exc}\n", encoding="utf-8")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
