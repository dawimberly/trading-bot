# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

xgboost_datas = collect_data_files("xgboost")
xgboost_binaries = collect_dynamic_libs("xgboost")
lightgbm_binaries = collect_dynamic_libs("lightgbm")

a = Analysis(
    ["src\\cli_entry.py"],
    pathex=["."],
    binaries=xgboost_binaries + lightgbm_binaries,
    datas=xgboost_datas,
    hiddenimports=["lightgbm", "xgboost", "shap", "rich", "dotenv", "joblib"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tensorflow", "torch", "plotly"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ufc-predict",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
