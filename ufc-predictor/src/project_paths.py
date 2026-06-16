"""Standalone project root — all runtime paths relative to UFC-Predictor/."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path


def bundle_root() -> Path | None:
    """PyInstaller onefile extraction dir (_MEIPASS), if running frozen."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def resolve_root(entry_file: Path | None = None) -> Path:
    """Return writable project root (EXE directory when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    if entry_file is not None:
        entry = entry_file.resolve()
        if entry.parent.name == "src":
            return entry.parents[1]
        return entry.parent
    return Path(__file__).resolve().parents[1]


def ensure_runtime_assets(root: Path) -> None:
    """
    Ensure data/ and models/ exist beside the EXE.

    PyInstaller --add-data bundles copies under _MEIPASS; copy to dist/ on first run.
    """
    bundle = bundle_root()
    if bundle is None:
        return

    for folder in ("data", "models"):
        src = bundle / folder
        dest = root / folder
        if not src.is_dir():
            continue
        if not dest.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
            continue
        # Fill in missing files without overwriting user cache
        for path in src.rglob("*"):
            if path.is_file():
                rel = path.relative_to(src)
                target = dest / rel
                if not target.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)


def setup_frozen_env(root: Path) -> None:
    """Matplotlib / DLL / GUI asset dirs for frozen onefile EXE."""
    cache = root / "data" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    mpl_dir = cache / "matplotlib"
    mpl_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(mpl_dir))
    (root / "data" / "logs").mkdir(parents=True, exist_ok=True)

    if not getattr(sys, "frozen", False):
        return

    bundle = bundle_root()
    if bundle is None:
        return

    # XGBoost / LightGBM native DLLs live under _MEIPASS after collect_dynamic_libs
    dll_dirs: list[Path] = []
    for rel in (
        "xgboost/lib",
        "xgboost",
        "lightgbm/bin",
        "lightgbm",
    ):
        candidate = bundle / rel.replace("/", os.sep)
        if candidate.is_dir():
            dll_dirs.append(candidate)

    path_parts = [str(d) for d in dll_dirs]
    if path_parts:
        os.environ["PATH"] = os.pathsep.join(path_parts + [os.environ.get("PATH", "")])
        if hasattr(os, "add_dll_directory"):
            for dll_dir in dll_dirs:
                try:
                    os.add_dll_directory(str(dll_dir))
                except OSError:
                    pass

    # customtkinter assets (themes, fonts) from collect-all
    for rel in ("customtkinter", "customtkinter/assets"):
        asset_dir = bundle / rel.replace("/", os.sep)
        if asset_dir.is_dir():
            os.environ.setdefault("CUSTOMTKINTER_ASSETS", str(asset_dir))
            break


def setup_sys_path(root: Path) -> None:
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    bundle = bundle_root()
    if bundle is not None:
        bundle_s = str(bundle)
        if bundle_s not in sys.path:
            sys.path.insert(0, bundle_s)


def patch_config(root: Path) -> None:
    """Point config.* at data/models/cache/logs under project root."""
    import config

    config.ROOT_DIR = root
    config.DATA_DIR = root / "data"
    config.RAW_DIR = config.DATA_DIR / "raw"
    config.PROCESSED_DIR = config.DATA_DIR / "processed"
    config.CACHE_DIR = config.DATA_DIR / "cache"
    config.MODELS_DIR = root / "models"
    config.RAW_FIGHTS_CSV = config.RAW_DIR / "fights.csv"
    config.PROCESSED_FEATURES_CSV = config.PROCESSED_DIR / "fight_features.csv"
    config.DEFAULT_MODEL_PATH = config.MODELS_DIR / "ensemble_winner.joblib"
    config.LEGACY_MODEL_PATH = config.MODELS_DIR / "lgbm_winner.joblib"
    config.METRICS_PATH = config.MODELS_DIR / "training_metrics.json"
    config.FEATURE_IMPORTANCE_PATH = config.MODELS_DIR / "feature_importance.json"
    config.BACKTEST_DIR = config.MODELS_DIR / "backtest"
    config.BACKTEST_SUMMARY_CSV = config.BACKTEST_DIR / "backtest_summary.csv"
    config.BACKTEST_PREDICTIONS_CSV = config.BACKTEST_DIR / "walk_forward_predictions.csv"
    config.BACKTEST_THRESHOLD_CSV = config.BACKTEST_DIR / "threshold_roi.csv"
    config.BACKTEST_IMPORTANCE_CSV = config.BACKTEST_DIR / "importance_timeline.csv"
    config.BACKTEST_METRICS_BY_YEAR_CSV = config.BACKTEST_DIR / "metrics_by_year.csv"
    config.BACKTEST_CALIBRATION_PNG = config.BACKTEST_DIR / "calibration_plot.png"
    config.BACKTEST_ROI_PNG = config.BACKTEST_DIR / "roi_threshold_plot.png"
    config.PLOTS_DIR = config.DATA_DIR / "plots"
    config.BACKTEST_2025_CSV = config.DATA_DIR / "backtest_2025_results.csv"
    config.HISTORICAL_META_PATH = config.CACHE_DIR / "historical_meta.json"
    config.UPCOMING_CARD_CACHE = config.CACHE_DIR / "upcoming_card.csv"
    config.HISTORICAL_ODDS_CACHE = config.CACHE_DIR / "historical_odds_unified.csv"
    config.ODDS_API_CACHE_PATH = config.CACHE_DIR / "ufc_odds_api.csv"
    config.ODDS_CACHE_PATH = config.CACHE_DIR / "ufc_odds_api.csv"
    config.UFCSTATS_GRECO_CACHE_DIR = config.CACHE_DIR / "ufcstats_greco"
    config.UFCSTATS_ENRICH_META_PATH = config.CACHE_DIR / "ufcstats_enrich_meta.json"
    config.ALERT_STATE_PATH = config.CACHE_DIR / "alert_state.json"
    config.LOG_DIR = config.DATA_DIR / "logs"
    config.BET_JOURNAL_CSV = config.DATA_DIR / "bet_journal.csv"
    config.HEARTBEAT_PATH = config.CACHE_DIR / "heartbeat.json"
    config.CIRCUIT_BREAKER_STATE_PATH = config.CACHE_DIR / "circuit_breaker_state.json"
    config.DRAWDOWN_STATE_PATH = config.CACHE_DIR / "drawdown_state.json"
    config.RISK_EVENTS_LOG = config.LOG_DIR / "risk_events.log"
    config.BUDGET_JSON_PATH = config.DATA_DIR / "budget.json"
    config.BET_JOURNAL_CSV = config.DATA_DIR / "bet_journal.csv"
    config.HEARTBEAT_PATH = config.CACHE_DIR / "heartbeat.json"
    config.CIRCUIT_BREAKER_STATE_PATH = config.CACHE_DIR / "circuit_breaker_state.json"
    config.DRAWDOWN_STATE_PATH = config.CACHE_DIR / "drawdown_state.json"
    config.ALERT_STATE_PATH = config.CACHE_DIR / "alert_state.json"


def env_file_load_order(root: Path) -> list[Path]:
    """
    .env paths in load order (lowest priority first; each file overrides the previous).

    Frozen EXE (root = dist/): project root ../.env wins over dist/.env.
    """
    if getattr(sys, "frozen", False):
        return [
            root / "ufc_betting_bot" / ".env",
            Path.cwd() / ".env",
            root / ".env",
            root.parent / ".env",
        ]
    return [
        root / "ufc_betting_bot" / ".env",
        Path.cwd() / ".env",
        root / ".env",
    ]


def load_env_files(root: Path, *, log: Callable[[str], None] | None = None) -> list[Path]:
    """Load .env files; later files override earlier (fixes stale OS env blocking .env)."""
    from dotenv import load_dotenv

    loaded: list[Path] = []
    for env_path in env_file_load_order(root):
        if log:
            log(f"Looking for .env at: {env_path}")
        if not env_path.is_file():
            continue
        load_dotenv(env_path, override=True)
        loaded.append(env_path)
        if log:
            log(f"Loaded .env from: {env_path}")
    return loaded


def reload_runtime_env(
    root: Path | None = None,
    *,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Re-load .env from disk and refresh config flags (call before odds/props refresh)."""
    root = root or resolve_root()
    load_env_files(root, log=log)
    import config

    config.refresh_runtime_env()
    return root


def bootstrap(*, entry_file: Path | None = None, env_log: Callable[[str], None] | None = None) -> Path:
    """
    Initialize cwd, sys.path, config paths, and .env for dev or frozen EXE.

    Layout:
        UFC-Predictor/
        ├── src/
        ├── data/          (raw, processed, cache, logs)
        ├── models/
        ├── dist/          (ufc-predict.exe, ufc-dashboard.exe)
        └── ufc_betting_bot/
            └── .env
    """
    root = resolve_root(entry_file)
    if getattr(sys, "frozen", False):
        os.chdir(root)
    setup_sys_path(root)
    ensure_runtime_assets(root)
    setup_frozen_env(root)
    patch_config(root)
    load_env_files(root, log=env_log)

    import config

    config.refresh_runtime_env()
    if env_log:
        env_log(f"ENABLE_PROPS loaded as: {config.ENABLE_PROPS}")
        env_log(f"Loaded MYBOOKIE_ENABLED = {config.MYBOOKIE_ENABLED}")
    return root
