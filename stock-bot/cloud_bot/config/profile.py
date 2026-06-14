"""Best Paper Bot v2.1 profile — aligned with main repo FINAL_PAPER_BOT_KWARGS."""

from __future__ import annotations

import os
from typing import Any, Mapping

# Cannot be overridden on cloud VPS (paper-only).
CLOUD_FORCED_ENV: dict[str, str] = {
    "PAPER_TRADING": "true",
    "ALLOW_LIVE_TRADING": "false",
}

BEST_PAPER_ENV: dict[str, str] = {
    "CLOUD_BOT_MODE": "1",
    "PAPER_TRADING": "true",
    "PAPER_CHASE_MODE": "1",
    "PAPER_AGGRESSIVE": "true",
    "PAPER_DYNAMIC_VTI": "true",
    "PAPER_DYNAMIC_RISK_ENABLED": "true",
    "PAPER_STAT_ARB_ENABLED": "true",
    "PAPER_VOL_TRADING_ENABLED": "true",
    "PAPER_OPTIONS_SLEEVE_ENABLED": "true",
    "PAPER_DYNAMIC_UNIVERSE": "true",
    "PAPER_MACRO_REGIME_ADAPTOR_ENABLED": "false",
    "PAPER_RISK_PARITY_ENABLED": "false",
    "PAPER_STAT_ARB_OPTIMIZED": "false",
    "PAPER_THINKING_ENGINE_ENABLED": "false",
    "PAPER_NYSE_OVERLAP_FILTER_ENABLED": "true",
    "PAPER_ADAPTIVE_CHUNK_ENABLED": "true",
    "PAPER_COFIRE_BUDGET_ENABLED": "true",
    "PAPER_SPY_EXIT_ON_MA_BREAK": "false",
    "PAPER_SOCIAL_SLEEVE_ENABLED": "false",
    "PAPER_MARKET_NEUTRAL_PAIRS": "true",
    "PAPER_EQUITY_PAIRS": "false",
    "DAILY_LOSS_CIRCUIT_BREAKER_ENABLED": "true",
    "ALLOW_LIVE_TRADING": "false",
}

CLOUD_BACKTEST_KWARGS: dict[str, Any] = {
    "paper_aggressive": True,
    "paper_sleeve_features": True,
    "paper_dynamic_vti": True,
    "paper_dynamic_risk": True,
    "paper_stat_arb": True,
    "paper_vol_trading": True,
    "paper_options_sleeve": True,
    "paper_macro_regime": False,
}


def final_paper_backtest_kwargs() -> dict[str, Any]:
    """Return kwargs aligned with main repo FINAL_PAPER_BOT_KWARGS."""
    try:
        from backtester import FINAL_PAPER_BOT_KWARGS

        return dict(FINAL_PAPER_BOT_KWARGS)
    except ImportError:
        return dict(CLOUD_BACKTEST_KWARGS)


def apply_best_paper_profile(
    env: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge best-paper defaults; cloud .env may opt into thinking engine."""
    out = dict(os.environ)
    if env is not None:
        out.update(env)
    for key, val in BEST_PAPER_ENV.items():
        out.setdefault(key, val)
    for key, val in CLOUD_FORCED_ENV.items():
        out[key] = val
    if overrides:
        out.update(overrides)
    return out


def apply_to_config_module() -> None:
    """Set config.py module flags for in-process backtests and validation."""
    import config

    config.PAPER_TRADING = True
    config.ALLOW_LIVE_TRADING = False
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)
    config.enforce_best_paper_stack()
    try:
        from config.best_paper_config import apply_best_paper_config

        apply_best_paper_config()
    except ImportError:
        pass

    thinking_on = os.getenv("PAPER_THINKING_ENGINE_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    config.PAPER_AGGRESSIVE_ENABLED = True
    config.PAPER_DYNAMIC_VTI_ENABLED = True
    config.PAPER_DYNAMIC_RISK_ENABLED = True
    config.PAPER_STAT_ARB_ENABLED = True
    config.PAPER_VOL_TRADING_ENABLED = True
    config.PAPER_OPTIONS_SLEEVE_ENABLED = True
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = True
    config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED = False
    config.PAPER_RISK_PARITY_ENABLED = False
    config.PAPER_STAT_ARB_OPTIMIZED = False
    config.PAPER_THINKING_ENGINE_ENABLED = thinking_on
    config.PAPER_MARKET_NEUTRAL_PAIRS = True
    config.PAPER_EQUITY_PAIRS = False
    config.apply_paper_sleeve_flags(
        {
            "nyse_overlap": True,
            "adaptive_chunk": True,
            "cofire_budget": True,
            "spy_exit_on_ma_break": False,
        }
    )
    config.PAPER_SOCIAL_SLEEVE_ENABLED = False
    config.SOCIAL_SLEEVE_ENABLED = False
