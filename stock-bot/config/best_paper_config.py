"""Best Paper Bot v3.3 — Realistic Research v1.5.4 (locked default for alpaca_paper)."""



from __future__ import annotations



import os



BEST_PAPER_VERSION = "3.3"

REALISTIC_RESEARCH_VERSION = "1.5.4"

BEST_PAPER_PROFILE = "realistic_research_v1.5.4"

BEST_PAPER_DISPLAY_NAME = f"Best Paper Bot v{BEST_PAPER_VERSION} (Realistic Research v{REALISTIC_RESEARCH_VERSION})"

BEST_PAPER_LOCKED = True  # stack enforced via enforce_best_paper_stack() on every paper path



# Production safety — always on (see modules/trading_safety.py)

PRODUCTION_SAFETY = {

    "daily_loss_limit_live_pct": 2.0,

    "daily_loss_limit_paper_pct": 4.0,

    "thinking_tilt_cap_pp": 6.0,

    "live_thinking_manual_approval": True,

    "daily_loss_blocks_entries": True,

}



# Core ON — Realistic Research v1.5 (v1.4 stack + RVOL/ORB/Catalyst/ATR scanners)

BEST_PAPER_CORE_ON: dict[str, bool] = {

    "dynamic_vti": True,

    "dynamic_risk": True,

    "stat_arb": True,

    "vol_overlay": True,

    "tail_risk_controls": True,

    "options_income": True,

    "thinking_engine": False,

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

    "tail_risk_controls": "BEST_PAPER_TAIL_RISK",

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

    """Features that must stay off under Best Paper Bot v3.1."""

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

    """Validate runtime config.py against best paper v3.1 expectations."""

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

    if not config.TAIL_RISK_CONTROLS_ENABLED:

        warnings.append("TAIL_RISK_CONTROLS_ENABLED should be on (Realistic Research v1.3)")

    return len(warnings) == 0, warnings





def apply_best_paper_config() -> None:

    """Apply best paper v3.1 flags to config module (paper startup / chase mode)."""

    import config



    if os.getenv("BEST_PAPER_SKIP_DEFAULTS"):

        config.enforce_best_paper_stack()

        config.enforce_realistic_research_profile()

        return



    flags = get_best_paper_feature_flags()

    config.PAPER_DYNAMIC_VTI_ENABLED = flags["dynamic_vti"]

    config.PAPER_DYNAMIC_RISK_ENABLED = flags["dynamic_risk"]

    config.PAPER_STAT_ARB_ENABLED = flags["stat_arb"]

    config.PAPER_VOL_TRADING_ENABLED = flags["vol_overlay"]

    if flags["tail_risk_controls"]:

        config.TAIL_RISK_CONTROLS_ENABLED = True

        config.VOL_CEILING_ENABLED = True

    config.PAPER_OPTIONS_SLEEVE_ENABLED = flags["options_income"]

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

    config.enforce_realistic_research_profile()


def get_validated_defaults_line() -> str:
    return (
        f"Validated defaults: Realistic Research v{REALISTIC_RESEARCH_VERSION} | "
        "Smart Dynamic VTI 35-75% | RVOL + ORB + Catalyst + ATR | "
        "Tuned Shorts + Sector + Insider | Stat Arb 10-14p | tail risk ON"
    )


def get_final_lock_banner() -> str:
    return (
        f"FINAL CONFIG: {BEST_PAPER_DISPLAY_NAME} locked "
        f"(alpaca_paper default via enforce_realistic_research_profile)"
    )


def get_locked_stack_header() -> str:
    return f"LOCKED {BEST_PAPER_DISPLAY_NAME}"


def get_v22_config_summary_lines() -> list[str]:
    flags = get_best_paper_feature_flags()
    on = [k for k, v in flags.items() if v]
    off = [k for k in get_locked_off_flags()]
    return [
        f"=== Realistic Research v{REALISTIC_RESEARCH_VERSION} (paper aggressive) ===",
        f"  ON: {', '.join(on) if on else '—'}",
        f"  OFF: {', '.join(off) if off else '—'}",
        "  Core: Smart Dynamic VTI 35-75% (NYSE/metals, insider, bubble, regime) | "
        "Stat Arb v1.5 | RVOL/ORB/Catalyst/ATR | tuned shorts 8-18% + sector + insider",
    ]


def get_restart_commands_block() -> list[str]:
    return [
        "=== Restart bots ===",
        "Live: python run_all.py",
        "Paper: python run_paper_bot.py  (Realistic Research v1.5.4 — Smart Dynamic VTI + Portfolio Constructor + RVOL/ORB/Catalyst/ATR)",
    ]


def get_live_profile_summary() -> str:
    import config

    return config.get_live_profile_summary()


if __name__ == "__main__":

    print("=" * 70)

    print(f"BEST PAPER BOT v{BEST_PAPER_VERSION} (Realistic Research v{REALISTIC_RESEARCH_VERSION})")

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


