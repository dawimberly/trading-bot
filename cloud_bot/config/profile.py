"""Best Paper Bot profile — same stack as final paper backtest (cloud default)."""

from __future__ import annotations

import os
from typing import Any, Mapping

# Matches backtester.FINAL_PAPER_BOT_KWARGS + config defaults for paper aggressive.
# Always overwrite on cloud — host .env must not enable live trading.
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
    "ALLOW_LIVE_TRADING": "false",
}

# Backtest kwargs — aligned with backtester.FINAL_PAPER_BOT_KWARGS (import at runtime).
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
    """Merge best-paper defaults into env (does not overwrite existing keys)."""
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
    """Set config.py module flags for in-process backtests and live loop."""
    import config

    config.PAPER_TRADING = True
    config.ALLOW_LIVE_TRADING = False
    config.set_paper_aggressive_context(True)
    config.PAPER_AGGRESSIVE_ENABLED = True
    config.PAPER_DYNAMIC_VTI_ENABLED = True
    config.PAPER_DYNAMIC_RISK_ENABLED = True
    config.PAPER_STAT_ARB_ENABLED = True
    config.PAPER_VOL_TRADING_ENABLED = True
    config.PAPER_OPTIONS_SLEEVE_ENABLED = True
    config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED = False
    config.PAPER_RISK_PARITY_ENABLED = False
    config.PAPER_STAT_ARB_OPTIMIZED = False
    config.PAPER_THINKING_ENGINE_ENABLED = False
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