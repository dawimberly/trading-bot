"""Resolve stock-bot source vs dist/ EXE runtime layout (shared by bot + dashboard)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BOT_EXE_NAME = "Weinstein-Trading-Bot.exe"
BOT_EXE_STEM = "Weinstein-Trading-Bot"
MONITOR_EXE_NAME = "PythonTradingMonitor.exe"
MONITOR_DIR_NAME = "PythonTradingMonitor"
DASHBOARD_SCRIPT = "dashboard_app.py"

# Canonical layout: stock-bot/ with dist/ for frozen EXEs and runtime data.
STOCK_BOT_DIRNAME = "stock-bot"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _has_run_all(path: Path) -> bool:
    return (path / "run_all.py").is_file()


def _has_bot_exe(path: Path) -> bool:
    return (path / BOT_EXE_NAME).is_file()


def _is_runtime_root(path: Path) -> bool:
    return _has_run_all(path)


def _stock_bot_from_path(path: Path) -> Path | None:
    """Return stock-bot/ when path is stock-bot or stock-bot/dist."""
    resolved = path.resolve()
    if _has_run_all(resolved):
        return resolved
    if _has_run_all(resolved.parent):
        return resolved.parent
    return None


def _resolve_root_path(path: Path) -> Path:
    return (_stock_bot_from_path(path) or path).resolve()


def _find_runtime_root(start: Path, *, max_depth: int = 8) -> Path | None:
    found = _stock_bot_from_path(start)
    if found is not None:
        return found
    candidate = start.resolve()
    for _ in range(max_depth):
        if _is_runtime_root(candidate):
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None


def ensure_pythontrading_root(start: Path | None = None) -> Path:
    """Resolve and export PYTHONTRADING_ROOT (always stock-bot/ when possible)."""
    root = resolve_runtime_root(start)
    os.environ["PYTHONTRADING_ROOT"] = str(root)
    return root


def resolve_runtime_root(start: Path | None = None) -> Path:
    """Project home: stock-bot/ (source tree, not repo root or old dist/)."""
    override = os.getenv("PYTHONTRADING_ROOT", "").strip()
    if override:
        found = _stock_bot_from_path(Path(override))
        return (found or Path(override)).resolve()

    if start is not None:
        found = _stock_bot_from_path(start)
        return (found or start).resolve()

    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        found = _stock_bot_from_path(exe_dir)
        if found is not None:
            return found
        return exe_dir.resolve()

    return Path(__file__).resolve().parents[1]


def runtime_layout(root: Path | None = None) -> str:
    """Return 'source', 'dist', 'frozen', or 'source+exe'."""
    root = root or resolve_runtime_root()
    if is_frozen():
        return "frozen"
    dist = root / "dist"
    if _has_bot_exe(dist) and _has_run_all(root):
        return "source+exe"
    if _has_bot_exe(dist):
        return "dist"
    if _has_run_all(root):
        return "source"
    return "unknown"


def runtime_layout_label(root: Path | None = None) -> str:
    mapping = {
        "source": "Source",
        "dist": "Dist EXE",
        "frozen": "Frozen EXE",
        "source+exe": "Source + EXE",
        "unknown": "Unknown",
    }
    return mapping.get(runtime_layout(root), "Unknown")


def resolve_data_root(root: Path | None = None) -> Path:
    """Writable runtime dir: stock-bot/dist/ when the bot EXE lives there, else stock-bot/."""
    root = root or resolve_runtime_root()
    dist = root / "dist"
    if _has_bot_exe(dist):
        return dist.resolve()
    if is_frozen() and _has_bot_exe(Path(sys.executable).resolve().parent):
        return Path(sys.executable).resolve().parent
    return root.resolve()


def resolve_bot_executable(root: Path | None = None) -> Path | None:
    root = root or resolve_runtime_root()
    exe = root / "dist" / BOT_EXE_NAME
    return exe.resolve() if exe.is_file() else None


def resolve_bot_workdir(root: Path | None = None) -> Path:
    """Directory used as cwd when launching the trading bot EXE."""
    return resolve_data_root(root)


def monitor_executable_path(root: Path | None = None) -> Path:
    """Canonical monitor path under stock-bot/dist/."""
    root = root or resolve_runtime_root()
    return (root / "dist" / MONITOR_DIR_NAME / MONITOR_EXE_NAME).resolve()


def resolve_dashboard_executable(root: Path | None = None) -> Path | None:
    """
    PyInstaller monitor: stock-bot/dist/PythonTradingMonitor/PythonTradingMonitor.exe
    When frozen, prefer monitor beside the bot EXE in dist/.
    """
    root = root or resolve_runtime_root()
    if is_frozen():
        beside_bot = (
            Path(sys.executable).resolve().parent
            / MONITOR_DIR_NAME
            / MONITOR_EXE_NAME
        )
        if beside_bot.is_file():
            return beside_bot.resolve()
    path = monitor_executable_path(root)
    return path if path.is_file() else None


def resolve_dashboard_script(root: Path | None = None) -> Path | None:
    """Source fallback: stock-bot/dashboard_app.py (pythonw)."""
    root = root or resolve_runtime_root()
    script = (root / DASHBOARD_SCRIPT).resolve()
    return script if script.is_file() else None


def _project_root_prefix(root: Path) -> str:
    return str(root.resolve()).lower()


def find_dashboard_script_pids(root: Path | None = None) -> list[int]:
    root = (root or resolve_runtime_root()).resolve()
    root_s = _project_root_prefix(root)
    if sys.platform == "win32":
        for exe_name in ("pythonw.exe", "python.exe"):
            try:
                cmd = (
                    f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" | "
                    f"Where-Object {{ $_.CommandLine -like '*{DASHBOARD_SCRIPT}*' "
                    f"-and $_.ExecutablePath -and $_.ExecutablePath.StartsWith('"
                    + root_s.replace("'", "''")
                    + "') }} | Select-Object -ExpandProperty ProcessId"
                )
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", cmd],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                pids = [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
                if pids:
                    return pids
            except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
                continue
        return []
    try:
        out = subprocess.check_output(["pgrep", "-f", DASHBOARD_SCRIPT], text=True)
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return []


def find_dashboard_monitor_pids(root: Path | None = None) -> list[int]:
    root = (root or resolve_runtime_root()).resolve()
    root_s = _project_root_prefix(root)
    if sys.platform == "win32":
        try:
            cmd = (
                f"Get-CimInstance Win32_Process -Filter \"Name='{MONITOR_EXE_NAME}'\" | "
                f"Where-Object {{ $_.ExecutablePath -and $_.ExecutablePath.StartsWith('"
                + root_s.replace("'", "''")
                + "') }} | Select-Object -ExpandProperty ProcessId"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
        except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
            return []
    try:
        out = subprocess.check_output(["pgrep", "-f", "PythonTradingMonitor"], text=True)
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return []


def dashboard_process_running(root: Path | None = None) -> bool:
    """True when PythonTradingMonitor.exe or dashboard_app.py is running under stock-bot/."""
    root = root or resolve_runtime_root()
    return bool(find_dashboard_monitor_pids(root) or find_dashboard_script_pids(root))


def resolve_ca_bundle_path(root: Path | None = None) -> Path | None:
    """Return a readable certifi cacert.pem for source, dist/, or frozen EXE."""
    root = root or resolve_runtime_root()
    data_root = resolve_data_root(root)
    candidates: list[Path] = [
        data_root / "certifi" / "cacert.pem",
        root / "certifi" / "cacert.pem",
    ]

    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            base = Path(meipass)
            candidates.extend([base / "certifi" / "cacert.pem", base / "cacert.pem"])
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                exe_dir / "certifi" / "cacert.pem",
                exe_dir / "_internal" / "certifi" / "cacert.pem",
            ]
        )

    try:
        import certifi

        candidates.append(Path(certifi.where()))
    except ImportError:
        pass

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def runtime_log_dir(root: Path | None = None) -> Path:
    return resolve_data_root(root) / "logs"


def resolve_heartbeat_path(
    root: Path | None = None,
    *,
    relative: str | None = None,
    paper: bool = False,
) -> Path:
    """Best-effort heartbeat file for dashboard / health checks."""
    root = resolve_data_root(root)
    if relative:
        path = Path(relative)
        return path if path.is_absolute() else root / path

    env_raw = (os.getenv("HEARTBEAT_FILE") or "").strip()
    if env_raw:
        env_path = Path(env_raw)
        return env_path if env_path.is_absolute() else root / env_path

    if paper:
        from modules.health_check import resolve_paper_heartbeat_path

        return resolve_paper_heartbeat_path(root=root)

    from modules.health_check import resolve_live_heartbeat_path

    return resolve_live_heartbeat_path(root=root)


def _win_pids_for_filter(filter_expr: str) -> list[int]:
    if sys.platform != "win32":
        return []
    try:
        cmd = (
            f"Get-CimInstance Win32_Process -Filter \"{filter_expr}\" | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", cmd],
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return []


def find_script_pids(script_name: str) -> list[int]:
    try:
        if sys.platform == "win32":
            cmd = (
                f"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{script_name}*' }} | "
                "Select-Object -ExpandProperty ProcessId"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
        out = subprocess.check_output(["pgrep", "-f", script_name], text=True)
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return []


def find_bot_exe_pids() -> list[int]:
    if sys.platform == "win32":
        return _win_pids_for_filter(f"Name='{BOT_EXE_NAME}'")
    try:
        out = subprocess.check_output(["pgrep", "-f", BOT_EXE_STEM], text=True)
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return []


def bot_process_running(*, paper: bool = False) -> bool:
    if paper:
        return bool(find_script_pids("run_paper_bot.py"))
    if find_bot_exe_pids():
        return True
    return bool(find_script_pids("run_all.py"))


def live_bot_pids() -> list[int]:
    pids = find_bot_exe_pids() + find_script_pids("run_all.py")
    return sorted(set(pids))
