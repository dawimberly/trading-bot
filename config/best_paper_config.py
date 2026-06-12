"""Best Paper Bot Configuration: recommended simplified stack for paper aggressive trading.

This module defines the "Best Paper Bot" configuration that reduces complexity while keeping
the strongest performing features. The goal is beat-mutual-funds performance through:

1. Dynamic VTI core (40-75% allocation, responsive to regime)
2. Dynamic risk sizing (1-3% per market condition)
3. Statistical arbitrage (cointegration pairs trading, original algorithm)
4. Volatility overlay (VIX regime adaptive)
5. Options income (covered calls, 12% cap)
6. Thinking engine (local LLM market reasoning, non-blocking)
7. Advanced flags (NYSE overlap filter, adaptive chunking, co-fire budget)

Deprecated/Disabled Features:
- Risk Parity (All Weather pod drawdown tracking) → Low signal-to-noise
- Stat Arb Optimized (custom z-score, max-hold bars) → Original algorithm proven better
- Macro Regime Adaptor → Redundant with thinking engine
- Social/Felix Sleeve (Paper) → Focus on systematic, not sentiment
- Equity Pairs → Low liquidity, high slippage
- SPY exit on MA break → Reduces participation in good trends
- Derived Bear Pause → Over-conservative, misses recoveries
"""

import os


def get_best_paper_feature_flags() -> dict[str, bool]:
    """Return recommended feature flags for best paper bot.
    
    All flags default to best_paper recommended values but can be overridden
    via .env for experimentation. Use judiciously.
    """
    return {
        # === CORE ALLOCATION ===
        # Dynamic VTI core responsive to regime (40-75%)
        "dynamic_vti": os.getenv("BEST_PAPER_DYNAMIC_VTI", "true").lower() in ("1", "true", "yes"),
        
        # Dynamic risk sizing per market condition (1-3% of equity)
        "dynamic_risk": os.getenv("BEST_PAPER_DYNAMIC_RISK", "true").lower() in ("1", "true", "yes"),
        
        # === TACTICAL SLEEVES ===
        # Market-neutral pairs (BTC-ETH, GLD-SLV style cointegration)
        "stat_arb": os.getenv("BEST_PAPER_STAT_ARB", "true").lower() in ("1", "true", "yes"),
        
        # Volatility overlay (VIX-regime adaptive)
        "vol_overlay": os.getenv("BEST_PAPER_VOL_OVERLAY", "true").lower() in ("1", "true", "yes"),
        
        # Options income (covered calls, 12% equity cap)
        "options": os.getenv("BEST_PAPER_OPTIONS", "true").lower() in ("1", "true", "yes"),
        
        # === INTELLIGENCE ===
        # Local LLM thinking engine (Ollama) for market reasoning
        "thinking_engine": os.getenv("BEST_PAPER_THINKING_ENGINE", "false").lower() in ("1", "true", "yes"),
        
        # === ADVANCED EXECUTION ===
        # Filter NYSE picks to avoid high-corr names when SPY active
        "nyse_overlap_filter": os.getenv("BEST_PAPER_NYSE_OVERLAP", "true").lower() in ("1", "true", "yes"),
        
        # Adaptive position sizing per regime volatility
        "adaptive_chunk": os.getenv("BEST_PAPER_ADAPTIVE_CHUNK", "true").lower() in ("1", "true", "yes"),
        
        # Co-fire budget: allow simultaneous entries from multiple sleeves
        "cofire_budget": os.getenv("BEST_PAPER_COFIRE_BUDGET", "true").lower() in ("1", "true", "yes"),
    }


def validate_best_paper_config() -> tuple[bool, list[str]]:
    """Validate best paper config against main config.py constraints.
    
    Returns (is_valid, list_of_warnings).
    """
    import config
    
    warnings = []
    
    # If risk parity is on, warn (deprecated)
    if config.PAPER_RISK_PARITY_ENABLED:
        warnings.append(
            "PAPER_RISK_PARITY_ENABLED is deprecated in best_paper_config; consider disabling"
        )
    
    # If stat arb optimized is on, warn (original performs better)
    if config.PAPER_STAT_ARB_OPTIMIZED:
        warnings.append(
            "PAPER_STAT_ARB_OPTIMIZED is deprecated; original algorithm outperforms in backtests"
        )
    
    # If macro regime adaptor is on, warn (redundant with thinking engine)
    if config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED:
        warnings.append(
            "PAPER_MACRO_REGIME_ADAPTOR_ENABLED: consider thinking_engine instead for market reasoning"
        )
    
    # If social sleeve is on for paper, warn (focus on systematic)
    if config.PAPER_SOCIAL_SLEEVE_ENABLED:
        warnings.append(
            "PAPER_SOCIAL_SLEEVE_ENABLED: best_paper_config focuses on systematic, not sentiment"
        )
    
    # If equity pairs enabled, warn (low liquidity)
    if config.PAPER_EQUITY_PAIRS:
        warnings.append(
            "PAPER_EQUITY_PAIRS enabled: low liquidity and high slippage, consider disabling"
        )
    
    # If SPY exit on MA break is on, warn (reduces participation)
    if config.PAPER_SPY_EXIT_ON_MA_BREAK:
        warnings.append(
            "PAPER_SPY_EXIT_ON_MA_BREAK: reduces participation in good trends"
        )
    
    return len(warnings) == 0, warnings


def apply_best_paper_config() -> None:
    """Apply best paper config recommendations to config module.
    
    This function modifies config's PAPER_*_ENABLED flags based on best_paper
    recommendations. Call during startup if using best_paper_config mode.
    """
    import config
    
    flags = get_best_paper_feature_flags()
    
    # Apply core allocation
    if not os.getenv("BEST_PAPER_SKIP_DEFAULTS"):
        config.PAPER_DYNAMIC_VTI_ENABLED = flags["dynamic_vti"]
        config.PAPER_DYNAMIC_RISK_ENABLED = flags["dynamic_risk"]
    
    # Apply tactical sleeves
    if not os.getenv("BEST_PAPER_SKIP_DEFAULTS"):
        config.PAPER_STAT_ARB_ENABLED = flags["stat_arb"]
        config.PAPER_VOL_TRADING_ENABLED = flags["vol_overlay"]
        config.PAPER_OPTIONS_SLEEVE_ENABLED = flags["options"]
    
    # Apply intelligence
    if not os.getenv("BEST_PAPER_SKIP_DEFAULTS"):
        config.PAPER_THINKING_ENGINE_ENABLED = flags["thinking_engine"]
    
    # Apply advanced execution
    if not os.getenv("BEST_PAPER_SKIP_DEFAULTS"):
        config.PAPER_NYSE_OVERLAP_FILTER_ENABLED = flags["nyse_overlap_filter"]
        config.PAPER_ADAPTIVE_CHUNK_ENABLED = flags["adaptive_chunk"]
        config.PAPER_COFIRE_BUDGET_ENABLED = flags["cofire_budget"]
    
    # Ensure deprecated features are disabled
    config.PAPER_RISK_PARITY_ENABLED = False
    config.PAPER_STAT_ARB_OPTIMIZED = False
    config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED = False
    config.PAPER_SOCIAL_SLEEVE_ENABLED = False
    config.PAPER_EQUITY_PAIRS = False
    config.PAPER_SPY_EXIT_ON_MA_BREAK = False


if __name__ == "__main__":
    # Quick validation check
    import config
    
    print("=" * 70)
    print("BEST PAPER BOT CONFIGURATION")
    print("=" * 70)
    print()
    print("Recommended Features (enabled by default):")
    flags = get_best_paper_feature_flags()
    for feature, enabled in flags.items():
        status = "✓" if enabled else "✗"
        print(f"  {status} {feature}")
    print()
    
    is_valid, warnings = validate_best_paper_config()
    if warnings:
        print("Warnings (current config.py conflicts):")
        for w in warnings:
            print(f"  ⚠ {w}")
        print()
    
    print("To apply best paper config in run_all.py startup:")
    print("  from config.best_paper_config import apply_best_paper_config")
    print("  apply_best_paper_config()")
