#!/usr/bin/env python3
"""Build Weinstein-Trading-Bot.exe — PyInstaller onefile console trading bot.

Run from stock-bot/ (venv active):
    pip install pyinstaller
    python build_exe.py

Output:
    dist/Weinstein-Trading-Bot.exe
    dist/data/, dist/sentiment/, dist/config/, dist/reference/  (runtime assets)
    dist/.env.example
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXE_NAME = "Weinstein-Trading-Bot"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
ENTRY = ROOT / "run_all.py"
RUNTIME_HOOK = ROOT / "pyi_rth_pythontrading.py"
MONITOR_DIR_NAME = "PythonTradingMonitor"
MONITOR_EXE_NAME = "PythonTradingMonitor.exe"
DASHBOARD_SCRIPT = "dashboard_app.py"

ICON_CANDIDATES = (
    ROOT / "assets" / "trading-bot.ico",
    ROOT / "assets" / "dashboard.ico",
    ROOT / "assets" / "bot.ico",
)

# Bundled into _MEIPASS and copied beside the EXE after build.
DATA_TREE_DIRS = (
    "sentiment/sources",
    "sentiment/archive",
    "config",
    "reference",
)

DATA_FILES = (
    "data/screener_universe.json",
    "data/alpaca_crypto_universe.json",
    "data/bot_manifest.txt",
    "data/portal/fund_pair.json.example",
)

RUNTIME_EMPTY_DIRS = (
    "sentiment/live",
    "logs",
    "data/portal",
    "data/cache",
)

COPY_SKIP_NAMES = frozenset({"users.db", "__pycache__", ".git"})
COPY_SKIP_SUFFIXES = (".pyc", ".pyo", ".pkl")

HIDDEN_IMPORTS = (
    "alpaca.trading.client",
    "alpaca.trading.requests",
    "alpaca.trading.enums",
    "alpaca.trading.models",
    "alpaca.data.historical",
    "alpaca.data.requests",
    "alpaca.data.timeframe",
    "dotenv",
    "certifi",
    "yfinance",
    "ccxt",
    "pandas",
    "numpy",
    "sqlite3",
    "modules.ssl_certs",
    "modules.runtime_paths",
    "modules.dashboard_launcher",
    "fetch_data",
    "modules.thinking_engine",
    "modules.data_refresh",
    "modules.data_loader",
    "modules.vol_position_sizing",
    "modules.profit_target",
    "modules.csv_utils",
)

EXCLUDES = (
    "streamlit",
    "plotly",
    "customtkinter",
    "matplotlib",
    "tkinter",
    "_tkinter",
    "PIL",
    "pystray",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "dashboard_app",
    "portal",
    "cloud_bot",
    "yt_dlp",
    "youtube_transcript_api",
)


def _parse_version(raw: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in raw.strip().split(".") if p.isdigit()]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])  # type: ignore[return-value]


def _resolve_icon() -> Path | None:
    for path in ICON_CANDIDATES:
        if path.is_file():
            return path
    return None


def _write_version_file(path: Path, version: str) -> None:
    filevers = _parse_version(version)
    year = datetime.now(timezone.utc).year
    path.write_text(
        f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={filevers},
    prodvers={filevers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'Weinstein Trading'),
        StringStruct(u'FileDescription', u'Weinstein Trading Bot'),
        StringStruct(u'FileVersion', u'{version}'),
        StringStruct(u'InternalName', u'{EXE_NAME}'),
        StringStruct(u'LegalCopyright', u'Copyright (c) {year}'),
        StringStruct(u'OriginalFilename', u'{EXE_NAME}.exe'),
        StringStruct(u'ProductName', u'Weinstein Trading Bot'),
        StringStruct(u'ProductVersion', u'{version}')])
      ]
    ),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


def _should_skip_copy(name: str) -> bool:
    if name in COPY_SKIP_NAMES:
        return True
    return name.endswith(COPY_SKIP_SUFFIXES)


def _copytree_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _should_skip_copy(name)}


def _add_data_args() -> list[str]:
    args: list[str] = []
    sep = os.pathsep

    try:
        import certifi

        ca = Path(certifi.where())
        if ca.is_file():
            args.append(f"--add-data={ca}{sep}certifi")
    except ImportError:
        pass

    for rel in DATA_TREE_DIRS:
        src = ROOT / rel
        if src.is_dir():
            args.append(f"--add-data={src}{sep}{rel.replace('/', os.sep)}")

    for rel in DATA_FILES:
        src = ROOT / rel
        if src.is_file():
            dest_dir = str(Path(rel).parent).replace("/", os.sep)
            args.append(f"--add-data={src}{sep}{dest_dir}")

    return args


def clean_build_artifacts(*, dist: bool = True) -> None:
    """Remove PyInstaller build/ and dist/ (best effort if files are locked)."""
    targets = [BUILD_DIR]
    if dist:
        targets.append(DIST_DIR)

    for target in targets:
        if not target.exists():
            continue
        print(f"Cleaning {target} ...")
        try:
            if target == DIST_DIR:
                _clean_dist_dir_preserve_monitor(target)
            else:
                shutil.rmtree(target)
        except PermissionError as exc:
            print(f"  [WARN] Could not remove {target}: {exc}")
            print("       Quit Weinstein-Trading-Bot.exe and retry.")


def _clean_dist_dir_preserve_monitor(dist_dir: Path) -> None:
    """Remove dist/ contents but keep PythonTradingMonitor/ for AUTO_LAUNCH_DASHBOARD."""
    monitor_dir = dist_dir / MONITOR_DIR_NAME
    monitor_backup: Path | None = None
    if monitor_dir.is_dir():
        monitor_backup = BUILD_DIR / "_monitor_backup"
        if monitor_backup.exists():
            shutil.rmtree(monitor_backup, ignore_errors=True)
        monitor_backup.mkdir(parents=True, exist_ok=True)
        shutil.copytree(monitor_dir, monitor_backup / MONITOR_DIR_NAME)
        print(f"  preserved {MONITOR_DIR_NAME}/ during clean")

    shutil.rmtree(dist_dir, ignore_errors=False)

    if monitor_backup is not None and (monitor_backup / MONITOR_DIR_NAME).is_dir():
        dist_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(monitor_backup / MONITOR_DIR_NAME, monitor_dir)
        shutil.rmtree(monitor_backup, ignore_errors=True)
        print(f"  restored {MONITOR_DIR_NAME}/")


def _copy_certifi_bundle(dist_dir: Path) -> None:
    """Copy cacert.pem beside the EXE for stable TLS in dist/ and frozen mode."""
    try:
        import certifi

        src = Path(certifi.where())
    except ImportError:
        print("  [SKIP] certifi not installed — cannot copy cacert.pem")
        return
    if not src.is_file():
        print(f"  [SKIP] certifi bundle missing: {src}")
        return
    dest = dist_dir / "certifi" / "cacert.pem"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    print(f"  copied file: certifi/cacert.pem")


def copy_runtime_assets(dist_dir: Path) -> None:
    """Copy seed data beside the EXE (writable runtime layout)."""
    _copy_certifi_bundle(dist_dir)
    for rel in DATA_TREE_DIRS:
        src = ROOT / rel
        dest = dist_dir / rel
        if not src.is_dir():
            print(f"  [SKIP] missing source dir: {rel}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=_copytree_ignore)
        print(f"  copied tree: {rel}")

    for rel in DATA_FILES:
        src = ROOT / rel
        dest = dist_dir / rel
        if not src.is_file():
            print(f"  [SKIP] missing source file: {rel}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"  copied file: {rel}")

    for rel in RUNTIME_EMPTY_DIRS:
        (dist_dir / rel).mkdir(parents=True, exist_ok=True)

    env_example = ROOT / ".env.example"
    if env_example.is_file():
        shutil.copy2(env_example, dist_dir / ".env.example")
        print("  copied file: .env.example")

    env_file = ROOT / ".env"
    if env_file.is_file():
        shutil.copy2(env_file, dist_dir / ".env")
        print("  synced file: .env -> dist/.env (fallback copy; stock-bot/.env wins at runtime)")

    launch_bat = dist_dir / "Start Weinstein Trading Bot.bat"
    launch_bat.write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        "if not exist \".env\" (\r\n"
        "  echo [ERROR] No .env in this folder.\r\n"
        "  echo Copy .env.example to .env and add your Alpaca API keys.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "if not exist \"logs\" mkdir logs\r\n"
        "echo Starting Weinstein Trading Bot...\r\n"
        "echo Logs: %CD%\\logs\\run_all.log\r\n"
        "\"%~dp0Weinstein-Trading-Bot.exe\"\r\n"
        "if errorlevel 1 (\r\n"
        "  echo.\r\n"
        "  echo Bot exited with an error. See logs\\startup_fatal.log and logs\\run_all.log\r\n"
        "  pause\r\n"
        ")\r\n",
        encoding="utf-8",
    )
    print("  wrote: Start Weinstein Trading Bot.bat")
    _report_dashboard_layout(dist_dir)


def _report_dashboard_layout(dist_dir: Path) -> None:
    """Print AUTO_LAUNCH_DASHBOARD targets available beside the bot EXE."""
    monitor = dist_dir / MONITOR_DIR_NAME / MONITOR_EXE_NAME
    script = ROOT / DASHBOARD_SCRIPT
    print("\n=== Dashboard auto-launch (AUTO_LAUNCH_DASHBOARD=true) ===")
    if monitor.is_file():
        print(f"  Frozen:  {monitor}")
    else:
        print(
            f"  Frozen:  (missing) build with build_dashboard.bat -> "
            f"dist\\{MONITOR_DIR_NAME}\\{MONITOR_EXE_NAME}"
        )
    if script.is_file():
        print(f"  Source:  pythonw {script.name} (when running python run_all.py)")
    else:
        print(f"  Source:  (missing) {DASHBOARD_SCRIPT}")


def build_pyinstaller(*, version: str, clean: bool, windowed: bool = False) -> int:
    if not ENTRY.is_file():
        print(f"[FAIL] Entry script not found: {ENTRY}")
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[FAIL] PyInstaller not installed. Run: pip install pyinstaller")
        return 1

    if clean:
        clean_build_artifacts(dist=True)

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    version_file = BUILD_DIR / "version_info.txt"
    _write_version_file(version_file, version)

    icon = _resolve_icon()
    if icon:
        print(f"Using icon: {icon}")
    else:
        print("No icon found (checked assets/trading-bot.ico, dashboard.ico, bot.ico)")

    cmd: list[str] = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        f"--{'windowed' if windowed else 'console'}",
        f"--name={EXE_NAME}",
        f"--paths={ROOT}",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={BUILD_DIR}",
        f"--version-file={version_file}",
    ]

    if RUNTIME_HOOK.is_file():
        cmd.append(f"--runtime-hook={RUNTIME_HOOK}")

    if icon:
        cmd.append(f"--icon={icon}")

    for hidden in HIDDEN_IMPORTS:
        cmd.append(f"--hidden-import={hidden}")

    for module in EXCLUDES:
        cmd.append(f"--exclude-module={module}")

    cmd.extend(_add_data_args())
    cmd.append(str(ENTRY))

    print("\n=== PyInstaller build ===")
    print(f"  Root:    {ROOT}")
    print(f"  Version: {version}")
    print(f"  Output:  {DIST_DIR / (EXE_NAME + '.exe')}")
    print("\nRunning:")
    print("  " + " ".join(f'"{part}"' if " " in part else part for part in cmd))
    print()

    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n[FAIL] PyInstaller build failed.")
        return result.returncode

    exe_path = DIST_DIR / f"{EXE_NAME}.exe"
    if not exe_path.is_file():
        print(f"\n[FAIL] Expected EXE not found: {exe_path}")
        return 1

    print("\n=== Copying runtime assets to dist/ ===")
    copy_runtime_assets(DIST_DIR)

    print("\n=== Build complete ===")
    print(f"  {exe_path}")
    print(f"  {DIST_DIR / 'data'}")
    print(f"  {DIST_DIR / 'sentiment'}")
    print(f"  {DIST_DIR / '.env.example'}")
    print("\nDeploy dist/ as a folder. Copy .env (from .env.example) next to the EXE.")
    print("Run from dist/ so logs/, market_data.db, and heartbeats stay beside the EXE.")
    print("Optional: AUTO_LAUNCH_DASHBOARD=true in .env opens dashboard_app.py on bot start.")
    print("Build monitor: python -m PyInstaller dashboard.spec (see build_dashboard.bat)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Weinstein-Trading-Bot.exe")
    parser.add_argument(
        "--version",
        default=os.getenv("BOT_EXE_VERSION", "1.0.0.0"),
        help="Windows file/product version (default: 1.0.0.0 or BOT_EXE_VERSION env)",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not delete build/ and dist/ before building",
    )
    parser.add_argument(
        "--clean-only",
        action="store_true",
        help="Only remove build/ and dist/, then exit",
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="Build without a console window (default: --console so startup errors are visible)",
    )
    args = parser.parse_args()

    if args.clean_only:
        clean_build_artifacts(dist=True)
        print("Clean complete.")
        return 0

    return build_pyinstaller(
        version=args.version, clean=not args.no_clean, windowed=args.windowed
    )


if __name__ == "__main__":
    raise SystemExit(main())
