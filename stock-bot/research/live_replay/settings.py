"""Effective Live settings and precedence. Values are non-secret configuration only."""

from __future__ import annotations

SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "APCA", "API")

# Keys restored from the process environment after the book file loads.
# Source: config._load_project_dotenv overlay_keys.
LAUNCH_OVERLAY_KEYS = (
    "TRADING_BOOK_ID",
    "PAPER_MEDIUM_STRATEGY",
    "PAPER_LAB_CONCENTRATED",
    "PAPER_LAB_MAX_NAMES",
    "PAPER_LAB_MAX_HOLD_DAYS",
    "PAPER_LAB_HALF_GAIN_PCT",
    "PAPER_LAB_TRAIL_ARM_PCT",
    "PAPER_LAB_TRAIL_PCT",
    "PAPER_LAB_DISASTER_PCT",
    "PAPER_LAB_ADD_POLICY",
    "PAPER_LAB_ADD_MAX_MULT",
    "MAX_ACTIVE_TICKERS",
    "ATR_STOP_MULTIPLIER",
    "PAPER_POSITION_MAX_HOLD_BARS",
    "EXIT_OPTIMIZATION_ENABLED",
    "EXIT_OPTIMIZATION_MAX_HOLD_BARS",
    "PAPER_SMART_STOPS",
    "STOP_LOSS_REEVAL_PCTS",
    "CONCENTRATION_TRIM_MIN_PCT",
    "PAPER_NYSE_FAT_LOSER_ENABLED",
    "PAPER_NYSE_A2B1_ENABLED",
    "PAPER_NYSE_GAIN_EXIT_PCT",
    "PAPER_NYSE_GAIN_EXIT_MODE",
    "PAPER_MAX_POSITION_PCT",
    "PER_NAME_MAX_PCT",
    "STOP_LOSS_PCT",
    "PAPER_DYNAMIC_VTI",
    "PAPER_DYNAMIC_VTI_ENABLED",
    "PAPER_VTI_CORE_PCT",
    "VTI_CORE_PCT",
    "DYNAMIC_VTI_PAPER_FLOOR",
    "DYNAMIC_VTI_PAPER_CEILING",
    "DYNAMIC_VTI_FLOOR_MIN",
    "DYNAMIC_VTI_DEFAULT_PCT",
    "DYNAMIC_VTI_STRESS_PCT",
    "DYNAMIC_VTI_CALM_PCT",
    "DYNAMIC_VTI_OPTIONAL_ENABLED",
    "PAPER_ACTIVE_SLEEVE_BOOST",
    "NYSE_SLEEVE_CAP_PCT",
    "PAPER_NYSE_SLEEVE_CAP_PCT",
    "PAPER_NYSE_HIGH_CASH_CAP_PCT",
    "PAPER_NYSE_MAX_EXPOSURE_PCT",
    "PAPER_REGIME_WEAK_SLEEVE_MAX_PCT",
    "METAL_SLEEVE_ENABLED",
    "LIVE_VTI_CORE_PCT",
    "LIVE_SMALL_ACTIVE_SLEEVE_PCT",
    "LIVE_ACTIVE_SLEEVE_CHOICE",
    "SMALL_ACCOUNT_VTI_CORE_PCT",
    "VTI_REBALANCE_CADENCE",
)

# modules/portal_bot.py user_bot_env for book_id alpaca_live, after clear_paper_research_env.
LIVE_PORTAL_OVERLAY = {
    "CONCENTRATION_TRIM_MIN_PCT": "0.005",
    "VTI_CORE_ENABLED": "false",
    "LIVE_VTI_CORE_PCT": "0",
    "LIVE_SMALL_ACTIVE_SLEEVE_PCT": "0.95",
    "LIVE_ACTIVE_SLEEVE_CHOICE": "nyse",
    "SMALL_ACCOUNT_VTI_CORE_PCT": "0",
    "VTI_CORE_PCT": "0",
    "DYNAMIC_CORE_ENABLED": "false",
    "DYNAMIC_VTI_ALLOW_ZERO": "true",
    "NYSE_SLEEVE_CAP_PCT": "0.95",
    "VTI_REBALANCE_CADENCE": "drift",
    "PAPER_NYSE_A2B1_ENABLED": "false",
}

# Code defaults used when neither the portal overlay nor a file sets the key.
# STOP_LOSS_PCT is assigned as a constant in config.py and is not read from the environment.
CODE_DEFAULTS = {
    "STOP_LOSS_PCT": "0.05",
    "MAX_ACTIVE_TICKERS": "25",
    "NYSE_MA_WINDOW": "50",
    "PAPER_SMART_STOPS": "true",
    "SMART_STOPS_LIVE_ENABLED": "false",
    "EXIT_OPTIMIZATION_ENABLED": "true",
    "LIVE_CONSERVATIVE_ENABLED": "true",
    "ORB_MOMENTUM_LIVE_SLEEVE": "false",
    "AUTO_DUST_CLEANER_ENABLED": "true",
    "AUTO_DUST_MAX_NOTIONAL": "10",
    "REAL_TIME_WEBSOCKET_ENABLED": "off on live unless set",
}

UNREAD_BOOK_KEYS = (
    "LIVE_MAX_ACTIVE_TICKERS_MIN",
    "LIVE_MAX_ACTIVE_TICKERS_MAX",
)

PATH = (
    "portal user_bot_env builds the child environment",
    "run_all.py imports config; PYTHONTRADING_ENV_FILE loads the book .env with override",
    "launch overlay keys captured before that load are written back, so they beat the book file",
    "stock-bot/.env then fills keys that are still missing (override false)",
    "isolate_book_alpaca_env replaces only Alpaca credential keys from the book file",
    "enforce_live_conservative_profile returns immediately when LIVE_CONSERVATIVE_ENABLED is false",
    "the cycle is portfolio guards, then position exits, then NYSE MA50 momentum",
)


def is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def parse_env_file(text: str) -> dict[str, str]:
    """Return non-secret KEY=value pairs. Secret keys are recorded as present only."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if is_secret_key(key):
            values[key] = "<set>"
        else:
            values[key] = raw.strip().strip('"').strip("'")
    return values


def resolve_effective(
    *,
    parent_env: dict[str, str],
    book_env: dict[str, str],
    stock_env: dict[str, str],
    portal_overlay: dict[str, str] | None = None,
) -> dict[str, str]:
    """Apply the Live child-process precedence and return non-secret values."""
    portal = dict(LIVE_PORTAL_OVERLAY if portal_overlay is None else portal_overlay)
    child = {k: v for k, v in parent_env.items() if not is_secret_key(k)}
    child.update({k: v for k, v in portal.items() if not is_secret_key(k)})
    launch = {k: child[k] for k in LAUNCH_OVERLAY_KEYS if k in child}
    merged = dict(child)
    for key, value in book_env.items():
        if is_secret_key(key):
            continue
        merged[key] = value
    merged.update(launch)
    for key, value in stock_env.items():
        if is_secret_key(key) or key in merged:
            continue
        merged[key] = value
    return merged


def live_decision_flags(effective: dict[str, str]) -> dict[str, object]:
    """Flags that select the Live entry and exit branch. Conservative lock stays off."""
    conservative = _truthy(effective.get("LIVE_CONSERVATIVE_ENABLED", "true"))
    vti_on = _truthy(effective.get("VTI_CORE_ENABLED", "false"))
    lock = conservative and vti_on and not _truthy(effective.get("PAPER_TRADING", "false"))
    smart_live = _truthy(effective.get("SMART_STOPS_LIVE_ENABLED", "false"))
    paper_smart = _truthy(effective.get("PAPER_SMART_STOPS", "true"))
    exit_opt_raw = _truthy(effective.get("EXIT_OPTIMIZATION_ENABLED", "true"))
    return {
        "paper_trading": _truthy(effective.get("PAPER_TRADING", "false")),
        "vti_core_enabled": vti_on,
        "live_conservative_lock": lock,
        "nyse_sleeve_cap_pct": float(effective.get("NYSE_SLEEVE_CAP_PCT", "0.95")),
        "per_name_max_pct": float(effective.get("PER_NAME_MAX_PCT", "0.08")),
        "max_active_tickers": int(float(effective.get("MAX_ACTIVE_TICKERS", "25"))),
        "concentration_trim_min_pct": float(effective.get("CONCENTRATION_TRIM_MIN_PCT", "0")),
        "stop_loss_pct": 0.05,
        "stop_loss_env_ignored": True,
        "fixed_stop_branch": not lock and not smart_live,
        "smart_stops_effective": bool(paper_smart and smart_live),
        "exit_optimization_effective": bool(lock and exit_opt_raw),
        "equity_pairs": False,
        "stat_arb": False,
        "idle_cash_scale_in": False,
        "a2b1": _truthy(effective.get("PAPER_NYSE_A2B1_ENABLED", "false")),
        "close_matrix": "5m",
        "nyse_ma_window": 50,
        "scanner_boosts": False,
        "momentum_quality_skip": False,
        "unread_book_keys": list(UNREAD_BOOK_KEYS),
        "auto_dust_max_notional": float(effective.get("AUTO_DUST_MAX_NOTIONAL", "10")),
    }


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
