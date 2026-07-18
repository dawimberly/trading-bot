"""RHYME regime sizing multipliers (paper aggressive) — replaces hard entry pauses."""

from __future__ import annotations

import logging

import config

logger = logging.getLogger(__name__)

_WEAK_REGIME_LETTERS = frozenset({"B", "D", "E"})


def _regime_letter(regime: str) -> str:
    reg = str(regime or "")
    if ":" in reg:
        reg = reg.split(":")[0].strip()
    if reg.startswith("RHYME_") and len(reg) >= 7:
        return reg[6:7]
    return ""


def is_weak_regime(regime: str) -> bool:
    """Bearish / panic / neutral-weak regimes — tighter sleeve caps."""
    return _regime_letter(regime) in _WEAK_REGIME_LETTERS


def regime_dynamic_sizing_multiplier(regime: str) -> float:
    """Per-RHYME sizing scale for paper aggressive (no hard blocks)."""
    letter = _regime_letter(regime)
    env_map = {
        "A": config.PAPER_REGIME_A_SIZING_MULT,
        "B": config.PAPER_REGIME_B_SIZING_MULT,
        "C": config.PAPER_REGIME_C_SIZING_MULT,
        "D": config.PAPER_REGIME_D_SIZING_MULT,
        "E": config.PAPER_REGIME_E_SIZING_MULT,
    }
    if letter in env_map:
        return float(env_map[letter])
    return 1.0


def regime_sleeve_exposure_ceiling(regime: str) -> float | None:
    """Max per-sleeve exposure in weak regimes; None when no extra cap."""
    if not (
        config.paper_aggressive_context() or config.backtest_paper_sleeves_context()
    ):
        return None
    if not config.effective_regime_dynamic_sizing():
        return None
    if is_weak_regime(regime):
        return float(config.PAPER_REGIME_WEAK_SLEEVE_MAX_PCT)
    return None


def effective_regime_sizing_multiplier(
    regime: str,
    *,
    wisdom_paused: bool = False,
) -> float:
    """Sizing multiplier applied to executor orders (replaces soft-pause blocks)."""
    if config.effective_regime_dynamic_sizing():
        mult = regime_dynamic_sizing_multiplier(regime)
        if wisdom_paused:
            mult = round(mult * config.PAPER_WISDOM_SIZING_FLOOR, 3)
        if config.effective_positioning_overlay_enabled():
            from modules.positioning_overlay import positioning_risk_multiplier

            mult = round(mult * positioning_risk_multiplier(), 3)
        if config.effective_markov_hmm_enabled():
            try:
                from modules.markov_regime import hmm_sizing_multiplier

                mult = round(mult * float(hmm_sizing_multiplier()), 3)
            except Exception as exc:
                logger.debug("regime sizing soft-fail: %s", exc)
        return mult
    from modules.pipeline_strategies import regime_soft_pause_sizing_multiplier

    return regime_soft_pause_sizing_multiplier(regime, wisdom_paused=wisdom_paused)


def format_regime_sizing_line(regime: str, *, wisdom_paused: bool = False) -> str:
    mult = effective_regime_sizing_multiplier(regime, wisdom_paused=wisdom_paused)
    short = str(regime or "unknown").split(":")[0].strip()
    label = ""
    if ":" in str(regime):
        label = str(regime).split(":", 1)[1].strip()
    pause = " | wisdom sizing floor" if wisdom_paused else ""
    weak = ""
    ceil = regime_sleeve_exposure_ceiling(regime)
    if ceil is not None:
        weak = f" | sleeve cap {ceil:.0%}"
    if label:
        return f"Regime: {short} ({label}) | sizing x{mult:.2f}{weak}{pause}"
    return f"Regime: {short} | sizing x{mult:.2f}{weak}{pause}"
