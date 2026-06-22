"""Best Paper Bot v2 — locked Profile B stack for paper aggressive trading.

Single source of truth for the simplified paper research profile. Live Profile A is
unchanged; call ``config.enforce_best_paper_stack()`` on every paper-aggressive path.
"""

from __future__ import annotations

import os

BEST_PAPER_VERSION = "2.1"
BEST_PAPER_LOCKED = True  # stack enforced via enforce_best_paper_stack() on every paper path

# Production safety — always on (see modules/trading_safety.py)
PRODUCTION_SAFETY = {
    "daily_loss_limit_live_pct": 2.0,
    "daily_loss_limit_paper_pct": 4.0,
    "thinking_tilt_cap_pp": 6.0,
    "live_thinking_manual_approval": True,
    "daily_loss_blocks_entries": True,
}

# Core ON — beat mutual-fund Sharpe with systematic sleeves
BEST_PAPER_CORE_ON: dict[str, bool] = {
    "dynamic_vti": True,
    "dynamic_risk": True,
    "stat_arb": True,
    "vol_overlay": True,
    "options_income": True,
    "thinking_engine": False,  # opt-in via PAPER_THINKING_ENGINE_ENABLED / BEST_PAPER_THINKING_ENGINE
    "nyse_overlap": True,
    "nyse_conditional": True,
    "adaptive_chunk": True,
    "cofire_budget": True,
    "dynamic_universe": True,
}

# Locked OFF — weak or redundant vs v2 stack
BEST_PAPER_LOCKED_OFF: dict[str, bool] = {
    "macro_regime": False,
    "risk_parity": False,
    "stat_arb_optimized": False,
    "social_sleeve": False,
    "equity_pairs": False,
    "spy_exit": False,
    "crypto_v2": False,  # experimental dual-entry; stat arb remains default
}

BEST_PAPER_ENV_MAP: dict[str, str] = {
    "dynamic_vti": "BEST_PAPER_DYNAMIC_VTI",
    "dynamic_risk": "BEST_PAPER_DYNAMIC_RISK",
    "stat_arb": "BEST_PAPER_STAT_ARB",
    "vol_overlay": "BEST_PAPER_VOL_OVERLAY",
    "options_income": "BEST_PAPER_OPTIONS",
    "thinking_engine": "BEST_PAPER_THINKING_ENGINE",
    "nyse_overlap": "BEST_PAPER_NYSE_OVERLAP",
    "nyse_conditional": "BEST_PAPER_NYSE_CONDITIONAL",
    "adaptive_chunk": "BEST_PAPER_ADAPTIVE_CHUNK",
    "cofire_budget": "BEST_PAPER_COFIRE_BUDGET",
    "dynamic_universe": "BEST_PAPER_DYNAMIC_UNIVERSE",
}


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


def get_best_paper_feature_flags() -> dict[str, bool]:
    """Recommended ON flags; each overridable via BEST_PAPER_* env vars."""
    out: dict[str, bool] = {}
    for name, default in BEST_PAPER_CORE_ON.items():
        env_key = BEST_PAPER_ENV_MAP.get(name, f"BEST_PAPER_{name.upper()}")
        out[name] = _env_bool(env_key, default)
    return out


def get_locked_off_flags() -> dict[str, bool]:
    """Features that must stay off under Best Paper Bot v2."""
    return dict(BEST_PAPER_LOCKED_OFF)


def get_production_safety() -> dict:
    """Locked production safety limits (live vs paper)."""
    return dict(PRODUCTION_SAFETY)


def get_full_stack() -> dict[str, bool]:
    """Merged ON + locked-OFF snapshot for status.py / docs."""
    stack = get_best_paper_feature_flags()
    stack.update(get_locked_off_flags())
    return stack


def validate_best_paper_config() -> tuple[bool, list[str]]:
    """Validate runtime config.py against best paper v2 expectations."""
    import config

    warnings: list[str] = []
    if config.PAPER_RISK_PARITY_ENABLED:
        warnings.append("PAPER_RISK_PARITY_ENABLED should be off (locked OFF)")
    if config.PAPER_STAT_ARB_OPTIMIZED:
        warnings.append("PAPER_STAT_ARB_OPTIMIZED should be off (original stat arb only)")
    if config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED:
        warnings.append("PAPER_MACRO_REGIME_ADAPTOR_ENABLED should be off")
    if config.PAPER_SOCIAL_SLEEVE_ENABLED:
        warnings.append("PAPER_SOCIAL_SLEEVE_ENABLED should be off")
    if config.PAPER_EQUITY_PAIRS:
        warnings.append("PAPER_EQUITY_PAIRS should be off")
    if config.PAPER_SPY_EXIT_ON_MA_BREAK:
        warnings.append("PAPER_SPY_EXIT_ON_MA_BREAK should be off")
    return len(warnings) == 0, warnings


def apply_best_paper_config() -> None:
    """Apply best paper v2 flags to config module (paper startup / chase mode)."""
    import config

    if os.getenv("BEST_PAPER_SKIP_DEFAULTS"):
        config.enforce_best_paper_stack()
        return

    flags = get_best_paper_feature_flags()
    config.PAPER_DYNAMIC_VTI_ENABLED = flags["dynamic_vti"]
    config.PAPER_DYNAMIC_RISK_ENABLED = flags["dynamic_risk"]
    config.PAPER_STAT_ARB_ENABLED = flags["stat_arb"]
    config.PAPER_VOL_TRADING_ENABLED = flags["vol_overlay"]
    config.PAPER_OPTIONS_SLEEVE_ENABLED = flags["options_income"]
    # Opt-in: explicit PAPER_THINKING_ENGINE_ENABLED in .env wins over BEST_PAPER_THINKING_ENGINE default
    explicit_te = os.getenv("PAPER_THINKING_ENGINE_ENABLED")
    if explicit_te is not None:
        config.PAPER_THINKING_ENGINE_ENABLED = explicit_te.lower() in ("1", "true", "yes")
    else:
        config.PAPER_THINKING_ENGINE_ENABLED = flags["thinking_engine"]
    config.PAPER_NYSE_OVERLAP_FILTER_ENABLED = flags["nyse_overlap"]
    config.PAPER_NYSE_CONDITIONAL_ON_SPY = flags["nyse_conditional"]
    config.PAPER_ADAPTIVE_CHUNK_ENABLED = flags["adaptive_chunk"]
    config.PAPER_COFIRE_BUDGET_ENABLED = flags["cofire_budget"]
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = flags["dynamic_universe"]
    config.enforce_best_paper_stack()


if __name__ == "__main__":
    print("=" * 70)
    print(f"BEST PAPER BOT v{BEST_PAPER_VERSION}")
    print("=" * 70)
    print("\nCore ON:")
    for k, v in get_best_paper_feature_flags().items():
        print(f"  {'✓' if v else '✗'} {k}")
    print("\nLocked OFF:")
    for k in BEST_PAPER_LOCKED_OFF:
        print(f"  ✗ {k}")
    _, warns = validate_best_paper_config()
    if warns:
        print("\nWarnings:")
        for w in warns:
            print(f"  ⚠ {w}")
