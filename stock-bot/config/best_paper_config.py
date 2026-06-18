"""Best Paper Bot v2.2 — locked Profile B stack for paper aggressive trading.

Single source of truth for the validated paper research profile. Applied automatically
on ``PAPER_CHASE_MODE=1`` via ``apply_best_paper_config()`` (called from
``config.init_paper_chase_if_enabled()`` and ``run_paper_bot.py`` → ``run_all.py``).

Live Profile A (~$300) is unchanged — 90% VTI, crypto OFF, thinking OFF, sector rotation OFF.

Validated stack (365d, ``--paper-aggressive --strict-pit``, 2026-06-18):
  Conservative blend + thinking+news: +59.60% return | Sharpe 2.05 | 45 quality tilts
  Sector rotation: OFF by default (-17.8pp vs blend)
  Dynamic universe: ON (sticky screener, $7 min price, liquidity filters)
  Compare: ``backtester.py --days 365 --paper-aggressive --strict-pit --compare-blended-conservative``
"""

from __future__ import annotations

import os
from pathlib import Path

BEST_PAPER_VERSION = "2.2"
BEST_PAPER_DISPLAY_NAME = (
    f"Best Paper Bot v{BEST_PAPER_VERSION} (conservative blend + thinking ON)"
)
BEST_PAPER_LOCKED = True  # enforced via enforce_best_paper_stack() on every paper path
BEST_PAPER_FINAL_LOCKED = True  # final validated state — do not change without 365d re-backtest
BEST_PAPER_LOCK_DATE = "2026-06-18"
BEST_PAPER_LOCK_NOTE = (
    "v2.2 lock — strict PIT, conservative Top1 blend, quality thinking tilts, stable dyn_univ."
)

# Canonical Profile B .env (opt-in flags only when experimenting)
BEST_PAPER_RECOMMENDED_ENV: dict[str, str] = {
    "PAPER_CHASE_MODE": "1",
    "PAPER_TRADING": "true",
    "PAPER_AGGRESSIVE": "true",
    "PAPER_CRYPTO_ENABLED": "false",
    "PAPER_INTERNATIONAL_SLEEVE_ENABLED": "false",
    "PAPER_BOND_SLEEVE_ENABLED": "false",
    "PAPER_THINKING_ENGINE_ENABLED": "true",
    "PAPER_DYNAMIC_UNIVERSE_ENABLED": "true",
    "PAPER_IPO_SAFETY_ENABLED": "true",
    "PAPER_TECH_GUARD_ENABLED": "true",
    "PAPER_SECTOR_ROTATION_ENABLED": "false",
    "SECTOR_ROTATION_HYBRID_MODE": "false",
    "PAPER_VOL_POSITION_SIZING_ENABLED": "true",
    "PAPER_LOSS_CUTTING_ENABLED": "true",
    "TOP1_VOL_SIZING_CONSERVATIVE": "true",
    "TOP1_LOSS_CUT_CONSERVATIVE": "true",
    "STRICT_PIT_BACKTEST": "true",
    "PAPER_SCALING_STRATEGY_ENABLED": "false",
    "PAPER_PATTERN_AWARENESS_ENABLED": "false",
    "PAPER_PROFIT_TARGET_ENABLED": "false",
}

# --- 365d validation snapshot (Best Paper v2.2 conservative blend, strict PIT) ---
BEST_PAPER_VALIDATION = {
    "window": "2025-08-13 → 2026-06-18 (~310 bars)",
    "vti_buy_hold_pct": 16.15,
    "baseline_return_pct": 59.60,
    "baseline_sharpe": 2.05,
    "baseline_max_dd_pct": -7.12,
    "thinking_tilt_events": 45,
    "validated_at": "2026-06-18",
}

# --- Why defaults stay OFF (long-term policy; do not enable without re-backtest) ---
BEST_PAPER_OFF_RATIONALE: dict[str, str] = {
    "crypto_sleeve": (
        "Crypto sleeve and stat-arb crypto pairs did not improve Sharpe or MaxDD vs "
        "equity-only across all 365d grids. Fee/slippage drag on small notionals; "
        "correlated beta without consistent alpha. Locked OFF by enforce_best_paper_stack()."
    ),
    "international_adr": (
        "International ADR sleeve (365d final): -0.38pp return, -0.05 Sharpe vs baseline, "
        "deeper drawdown (14 ADR trades). Macro/thinking triggers add turnover without "
        "validated edge. Default OFF; explicit PAPER_INTERNATIONAL_SLEEVE_ENABLED=true for research."
    ),
    "bond_sleeve": (
        "Bond sleeve (TLT/GOVT) is flat on return/Sharpe vs baseline (+0.04pp MaxDD help only). "
        "Default OFF; optional PAPER_BOND_SLEEVE_ENABLED=true for risk-off hedge experiments."
    ),
    "sector_rotation": (
        "Inter-sector rotation (365d): rules-only -7.9pp vs no rotation; hybrid -21.5pp. "
        "Trimmed tech during a tech-led window without validated edge. Default OFF; "
        "opt-in via PAPER_SECTOR_ROTATION_ENABLED=true after rule tuning."
    ),
    "tech_guard": (
        "Tech concentration guard ON by default on paper — zero performance drag when "
        "exposure stays below 45%; blocks tech pile-on in momentum rallies. "
        "Live $300 stays OFF (TECH_CONCENTRATION_LIVE_ENABLED=false)."
    ),
}

# Production safety — always on (see modules/trading_safety.py)
PRODUCTION_SAFETY = {
    "daily_loss_limit_live_pct": 2.0,
    "daily_loss_limit_paper_pct": 4.0,
    "thinking_tilt_cap_pp": 6.0,
    "live_thinking_manual_approval": True,
    "daily_loss_blocks_entries": True,
}

# Core ON — validated winners (beat mutual-fund Sharpe with systematic sleeves)
BEST_PAPER_CORE_ON: dict[str, bool] = {
    "dynamic_vti": True,
    "dynamic_risk": True,
    "stat_arb": True,
    "vol_overlay": True,
    "options_income": True,
    "thinking_engine": True,  # quality tilts ON (cooldown + deadband)
    "nyse_overlap": True,
    "adaptive_chunk": True,
    "cofire_budget": True,
    "dynamic_universe": True,
    "ipo_safety": True,
    "tech_guard": True,  # PAPER_TECH_GUARD_ENABLED default true
}

# Locked OFF — weak, redundant, or underperforming vs validated v2.1 baseline
BEST_PAPER_LOCKED_OFF: dict[str, bool] = {
    "macro_regime": False,
    "risk_parity": False,
    "stat_arb_optimized": False,
    "social_sleeve": False,
    "equity_pairs": False,
    "spy_exit": False,
    "crypto_v2": False,
    "crypto_sleeve": False,
    "crypto_expanded": False,
    "profit_target": False,
    "scaling_strategy": False,
    "pattern_awareness": False,
    "international_adr": False,
    "bond_sleeve": False,
    "sector_rotation": False,
}

# Opt-in features — never forced ON by apply_best_paper_config()
BEST_PAPER_OPT_IN: dict[str, str] = {
    "thinking_engine": "PAPER_THINKING_ENGINE_ENABLED",
    "sector_rotation": "PAPER_SECTOR_ROTATION_ENABLED",
}

# Opt-in new-market sleeves — default false
BEST_PAPER_OPT_IN_SLEEVES: dict[str, str] = {
    "international_adr": "PAPER_INTERNATIONAL_SLEEVE_ENABLED",
    "bond_sleeve": "PAPER_BOND_SLEEVE_ENABLED",
}

BEST_PAPER_ENV_MAP: dict[str, str] = {
    "dynamic_vti": "BEST_PAPER_DYNAMIC_VTI",
    "dynamic_risk": "BEST_PAPER_DYNAMIC_RISK",
    "stat_arb": "BEST_PAPER_STAT_ARB",
    "vol_overlay": "BEST_PAPER_VOL_OVERLAY",
    "options_income": "BEST_PAPER_OPTIONS",
    "thinking_engine": "BEST_PAPER_THINKING_ENGINE",
    "nyse_overlap": "BEST_PAPER_NYSE_OVERLAP",
    "adaptive_chunk": "BEST_PAPER_ADAPTIVE_CHUNK",
    "cofire_budget": "BEST_PAPER_COFIRE_BUDGET",
    "dynamic_universe": "BEST_PAPER_DYNAMIC_UNIVERSE",
    "ipo_safety": "BEST_PAPER_IPO_SAFETY",
    "tech_guard": "BEST_PAPER_TECH_GUARD",
}


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


def get_best_paper_feature_flags() -> dict[str, bool]:
    """Core ON flags; each overridable via BEST_PAPER_* env vars."""
    out: dict[str, bool] = {}
    for name, default in BEST_PAPER_CORE_ON.items():
        env_key = BEST_PAPER_ENV_MAP.get(name, f"BEST_PAPER_{name.upper()}")
        out[name] = _env_bool(env_key, default)
    return out


def get_locked_off_flags() -> dict[str, bool]:
    """Features that must stay off under Best Paper Bot v2.1."""
    return dict(BEST_PAPER_LOCKED_OFF)


def get_opt_in_sleeve_defaults() -> dict[str, bool]:
    """New-market sleeves — default OFF unless explicit .env opt-in."""
    return {
        name: _env_bool(env_key, False)
        for name, env_key in BEST_PAPER_OPT_IN_SLEEVES.items()
    }


def get_production_safety() -> dict:
    """Locked production safety limits (live vs paper)."""
    return dict(PRODUCTION_SAFETY)


def get_full_stack() -> dict[str, bool]:
    """Merged ON + locked-OFF snapshot for status.py / docs."""
    stack = get_best_paper_feature_flags()
    stack.update(get_locked_off_flags())
    return stack


def get_validated_defaults_line() -> str:
    """One-line summary for status.py — validated v2.2 defaults."""
    v = BEST_PAPER_VALIDATION
    return (
        f"v2.2 ({BEST_PAPER_LOCK_DATE}): strict PIT | conservative Top1 blend | "
        f"thinking ON | dyn_univ ON | rotation/ADR/bond/crypto OFF | "
        f"365d {v['baseline_return_pct']:+.2f}% Sharpe {v['baseline_sharpe']:.2f}"
    )


def get_v22_config_summary_lines() -> list[str]:
    """Multi-line Best Paper v2.2 config summary for status.py."""
    v = BEST_PAPER_VALIDATION
    return [
        "=== Best Paper v2.2 (locked defaults) ===",
        "Research validation: 365d strict PIT | conservative blend + thinking+news",
        (
            f"  Return {v['baseline_return_pct']:+.2f}% | Sharpe {v['baseline_sharpe']:.2f} | "
            f"MaxDD {v['baseline_max_dd_pct']:.2f}% | tilts {v.get('thinking_tilt_events', 45)}"
        ),
        "  ON:  strict PIT | spec 0.5% vol cap + mild ATR | spec -4% stop only",
        "       thinking (cooldown/deadband) | dyn_univ (sticky, $7+, liquidity)",
        "       dyn VTI/risk | stat arb | vol overlay | options | tech guard | IPO safety",
        "  OFF: crypto | sector rotation | ADR | bond | scaling | patterns | profit target",
        "       macro | social | risk parity | equity pairs | SPY MA exit",
    ]


def get_final_lock_banner() -> str:
    """Status.py header — post-UFC cleanup final configuration."""
    return f"FINAL CONFIG: {BEST_PAPER_DISPLAY_NAME} locked {BEST_PAPER_LOCK_DATE} | {BEST_PAPER_LOCK_NOTE}"


def get_restart_commands_block() -> list[str]:
    """Copy-paste restart lines for live + paper bots."""
    return [
        "=== Restart bots ===",
        "Live Profile A (~$300):",
        "  cd stock-bot",
        "  python scripts\\account\\preflight.py",
        "  python run_all.py",
        "",
        "Paper Best Paper v2.2:",
        "  cd stock-bot",
        "  python status.py",
        "  python run_paper_bot.py",
        "",
        "Both (separate processes): python launch_bots.py  or  ..\\launch_both.bat",
        "Smoke test (3 cycles): python run_paper_bot.py --cycles 3",
    ]


def get_live_profile_summary() -> str:
    """One-line Live Profile A policy."""
    return (
        "Live Profile A: 90% VTI (<$500) | crypto OFF | thinking OFF | "
        "static universe | overlap/chunk/co-fire OFF"
    )


def get_locked_stack_header() -> str:
    """Status.py header — final Profile B lock."""
    return (
        f"LOCKED {BEST_PAPER_DISPLAY_NAME} — final {BEST_PAPER_LOCK_DATE} "
        "(apply_best_paper_config on paper chase)"
    )


def get_core_on_summary() -> str:
    """Compact ON features for status/docs."""
    flags = get_best_paper_feature_flags()
    on = [k for k, v in flags.items() if v and k != "thinking_engine"]
    return "core ON: " + ", ".join(on)


def get_recommended_env_block() -> str:
    """Copy-paste .env block for Profile B defaults."""
    lines = ["# Best Paper Bot v2.2 — validated defaults"]
    for key, val in BEST_PAPER_RECOMMENDED_ENV.items():
        lines.append(f"{key}={val}")
    return "\n".join(lines)


def validate_best_paper_config() -> tuple[bool, list[str]]:
    """Validate runtime config.py against best paper v2.1 expectations."""
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
    if config.PAPER_CRYPTO_ENABLED or config.effective_crypto_enabled():
        warnings.append("PAPER_CRYPTO_ENABLED / crypto sleeve should be off (locked OFF)")
    if config.PAPER_CRYPTO_UNIVERSE_EXPANDED:
        warnings.append("PAPER_CRYPTO_UNIVERSE_EXPANDED should be off (research only)")
    if config.PAPER_PROFIT_TARGET_ENABLED:
        warnings.append("PAPER_PROFIT_TARGET_ENABLED should be off (research only)")
    if config.PAPER_INTERNATIONAL_SLEEVE_ENABLED:
        warnings.append(
            "PAPER_INTERNATIONAL_SLEEVE_ENABLED=true overrides validated default "
            "(365d final: -0.38pp vs baseline — research only)"
        )
    if config.PAPER_BOND_SLEEVE_ENABLED:
        warnings.append(
            "PAPER_BOND_SLEEVE_ENABLED=true overrides validated default "
            "(365d: flat return — defensive hedge experiments only)"
        )
    if not config.PAPER_THINKING_ENGINE_ENABLED:
        warnings.append(
            "PAPER_THINKING_ENGINE_ENABLED=false overrides v2.2 default "
            "(thinking ON validated +6pp vs OFF on conservative blend)"
        )
    if config.PAPER_SECTOR_ROTATION_ENABLED:
        warnings.append(
            "PAPER_SECTOR_ROTATION_ENABLED=true overrides validated default "
            "(365d: -7.9pp rules-only vs baseline — opt-in research only)"
        )
    if config.PAPER_SCALING_STRATEGY_ENABLED:
        warnings.append("PAPER_SCALING_STRATEGY_ENABLED should be off (locked OFF v2.2)")
    if config.PAPER_PATTERN_AWARENESS_ENABLED:
        warnings.append("PAPER_PATTERN_AWARENESS_ENABLED should be off (locked OFF v2.2)")
    if not config.PAPER_TECH_GUARD_ENABLED:
        warnings.append(
            "PAPER_TECH_GUARD_ENABLED=false overrides validated default "
            "(tech guard ON is zero-cost safety net on paper)"
        )
    return len(warnings) == 0, warnings


def _apply_core_on_flags(config, flags: dict[str, bool]) -> None:
    config.PAPER_DYNAMIC_VTI_ENABLED = flags["dynamic_vti"]
    config.PAPER_DYNAMIC_RISK_ENABLED = flags["dynamic_risk"]
    config.PAPER_STAT_ARB_ENABLED = flags["stat_arb"]
    config.PAPER_VOL_TRADING_ENABLED = flags["vol_overlay"]
    config.PAPER_OPTIONS_SLEEVE_ENABLED = flags["options_income"]
    explicit_te = os.getenv("PAPER_THINKING_ENGINE_ENABLED")
    if explicit_te is not None:
        config.PAPER_THINKING_ENGINE_ENABLED = explicit_te.lower() in ("1", "true", "yes")
    else:
        config.PAPER_THINKING_ENGINE_ENABLED = flags["thinking_engine"]
    config.PAPER_NYSE_OVERLAP_FILTER_ENABLED = flags["nyse_overlap"]
    config.PAPER_ADAPTIVE_CHUNK_ENABLED = flags["adaptive_chunk"]
    config.PAPER_COFIRE_BUDGET_ENABLED = flags["cofire_budget"]
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = flags["dynamic_universe"]
    config.PAPER_IPO_SAFETY_ENABLED = flags["ipo_safety"]
    config.PAPER_TECH_GUARD_ENABLED = _env_bool(
        "PAPER_TECH_GUARD_ENABLED", flags.get("tech_guard", True)
    )


def _apply_rotation_and_guard_defaults(config) -> None:
    """Sector rotation OFF by default; hybrid mode OFF; tech guard ON on paper."""
    config.PAPER_SECTOR_ROTATION_ENABLED = _env_bool(
        "PAPER_SECTOR_ROTATION_ENABLED", False
    )
    config.SECTOR_ROTATION_HYBRID_MODE = _env_bool("SECTOR_ROTATION_HYBRID_MODE", False)
    if os.getenv("PAPER_TECH_GUARD_ENABLED") is None:
        config.PAPER_TECH_GUARD_ENABLED = True


def _apply_locked_off_flags(config) -> None:
    """Hard-disable research losers; crypto cannot be re-enabled via .env on bots."""
    config.PAPER_CRYPTO_ENABLED = False
    config.PAPER_CRYPTO_UNIVERSE_EXPANDED = False
    config.PAPER_PROFIT_TARGET_ENABLED = False
    config.CRYPTO_SLEEVE_ENABLED = False
    config.enforce_best_paper_stack()


def _apply_opt_in_sleeves(config) -> None:
    """New-market sleeves: default OFF; honor explicit .env opt-in only."""
    config.refresh_paper_new_markets_flags_from_env()
    config.PAPER_INTERNATIONAL_SLEEVE_ENABLED = _env_bool(
        "PAPER_INTERNATIONAL_SLEEVE_ENABLED", False
    )
    config.PAPER_BOND_SLEEVE_ENABLED = _env_bool("PAPER_BOND_SLEEVE_ENABLED", False)


def apply_best_paper_config() -> None:
    """Apply validated Best Paper Bot v2.2 stack to config module (paper chase startup)."""
    import config

    if os.getenv("BEST_PAPER_SKIP_DEFAULTS"):
        config.enforce_best_paper_stack()
        return

    flags = get_best_paper_feature_flags()
    _apply_core_on_flags(config, flags)
    _apply_locked_off_flags(config)
    _apply_opt_in_sleeves(config)
    _apply_rotation_and_guard_defaults(config)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    print("=" * 70)
    print(BEST_PAPER_DISPLAY_NAME.upper())
    print("=" * 70)
    print(get_validated_defaults_line())
    print(get_live_profile_summary())
    print("\nCore ON:")
    for k, v in get_best_paper_feature_flags().items():
        mark = "Y" if v else "N"
        print(f"  [{mark}] {k}")
    print("\nLocked OFF:")
    for k in BEST_PAPER_LOCKED_OFF:
        print(f"  [ ] {k}")
    print("\nOff-by-default rationale (crypto / ADR / bond / rotation):")
    for key in ("crypto_sleeve", "international_adr", "bond_sleeve", "sector_rotation"):
        print(f"  {key}: {BEST_PAPER_OFF_RATIONALE[key][:72]}...")
    print(f"  tech_guard: {BEST_PAPER_OFF_RATIONALE['tech_guard'][:72]}...")
    _, warns = validate_best_paper_config()
    if warns:
        print("\nWarnings:")
        for w in warns:
            print(f"  ! {w}")
