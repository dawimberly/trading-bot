# PyInstaller spec — PythonTrading desktop monitor
#
# Build (from project root, venv active):
#   pip install pyinstaller pillow customtkinter
#   python scripts/generate_dashboard_icon.py
#   python -m PyInstaller dashboard.spec --noconfirm
#
# Output: dist/PythonTradingMonitor/PythonTradingMonitor.exe
# Copy the .exe to the project root (next to .env, run_all.py) for --launch-bot to work.

import sys
from pathlib import Path

try:
    import certifi
except ImportError:
    certifi = None

try:
    from PyInstaller.utils.hooks import collect_data_files, collect_submodules
except ImportError:
    collect_data_files = None
    collect_submodules = None

block_cipher = None
root = Path(SPECPATH)
icon_path = root / "assets" / "dashboard.ico"

datas = []
binaries = []
if certifi is not None:
    cacert = Path(certifi.where())
    if cacert.is_file():
        datas.append((str(cacert), "certifi"))
if collect_data_files is not None:
    try:
        datas += collect_data_files("customtkinter")
    except Exception:
        pass
icon_arg = str(icon_path) if icon_path.is_file() else None

hiddenimports = [
    "customtkinter",
    "customtkinter.windows",
    "customtkinter.windows.widgets",
    "matplotlib.backends.backend_tkagg",
    "PIL",
    "PIL.Image",
    "pystray",
    "certifi",
    "alpaca",
    "alpaca.trading",
    "dotenv",
    "modules.portal_auth",
    "modules.portal_bot",
    "modules.portal_paths",
    "modules.trading_books",
]
if collect_submodules is not None:
    try:
        hiddenimports += collect_submodules("customtkinter")
    except Exception:
        pass

a = Analysis(
    ["dashboard_app.py"],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / "pyi_rth_pythontrading.py")],
    excludes=["streamlit", "plotly"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PythonTradingMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PythonTradingMonitor",
)
