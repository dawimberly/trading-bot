"""Cloud bot stack definition — Best Paper Bot v2.1 (matches main repo profile)."""

from __future__ import annotations

STACK_FEATURES_ON = (
    "Dynamic VTI (40–75%)",
    "Dynamic risk (1–3%)",
    "Statistical arbitrage (cointegration, both legs)",
    "Volatility overlay (VIX regime; log-only PnL on live/cloud)",
    "Options income (covered calls)",
    "NYSE overlap filter",
    "Adaptive chunk sizing",
    "Co-fire budget",
    "Dynamic universe (screener refresh)",
    "Thinking engine (opt-in via PAPER_THINKING_ENGINE_ENABLED=true)",
)

STACK_SAFETY_GUARDS = (
    "Paper trading only (PAPER_TRADING=true, ALLOW_LIVE_TRADING=false)",
    "Daily loss circuit breaker (4% paper / blocks entries + tilts)",
    "Thinking tilt cap ±6% per sleeve (when engine enabled)",
    "Paper manual approval off; live keys rejected on cloud",
)

STACK_FEATURES_LOCKED_OFF = (
    "Macro regime adaptor",
    "Risk parity",
    "Stat arb optimized",
    "Social / Felix sleeve",
    "Equity pairs",
    "SPY MA exit",
)

# Backward compat alias
STACK_FEATURES = STACK_FEATURES_ON


def describe_stack() -> str:
    lines = ["ON:"] + [f"  + {f}" for f in STACK_FEATURES_ON]
    lines += ["SAFETY:"] + [f"  * {g}" for g in STACK_SAFETY_GUARDS]
    lines += ["LOCKED OFF:"] + [f"  - {f}" for f in STACK_FEATURES_LOCKED_OFF]
    return "\n".join(lines)
