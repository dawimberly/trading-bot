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
    lines += ["LOCKED OFF:"] + [f"  - {f}" for f in STACK_FEATURES_LOCKED_OFF]
    return "\n".join(lines)
