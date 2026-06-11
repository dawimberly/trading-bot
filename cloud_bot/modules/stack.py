"""Cloud bot stack definition — mirrors best paper bot (final backtest profile)."""

from __future__ import annotations

STACK_FEATURES = (
    "Dynamic VTI (40–75%)",
    "Dynamic risk (1–3%)",
    "Statistical arbitrage (cointegration, both legs)",
    "Volatility overlay (VIX regime)",
    "Options income (covered calls)",
    "Macro regime adaptor",
    "NYSE overlap filter",
    "Adaptive chunk sizing",
    "Co-fire budget",
)


def describe_stack() -> str:
    return "\n".join(f"  - {f}" for f in STACK_FEATURES)
