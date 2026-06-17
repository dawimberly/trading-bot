"""Paper-only treasury bond ETF sleeve (TLT or GOVT).

Defensive allocation 0–15% in risk-off regimes or elevated VIX.
Not a forex/currency product — USD-denominated rate/duration hedge only.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import config

logger = logging.getLogger(__name__)

BOND_RISK_NOTE = (
    "Bond sleeve uses TLT/GOVT duration exposure — rises when rates fall / risk-off; "
    "can draw down in rising-rate selloffs (same as yield-gate TLT stress signal)."
)

BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"
BOND_FALLBACK_SYMBOLS = ("TLT", "GOVT")


def is_bond_symbol(symbol: str) -> bool:
    sym = config.normalize_symbol(symbol)
    return sym in BOND_FALLBACK_SYMBOLS or sym == config.normalize_symbol(
        config.BOND_SLEEVE_SYMBOL
    )


def resolve_bond_symbol(data_columns) -> str:
    """Prefer configured symbol, then GOVT, then TLT."""
    cols = {config.normalize_symbol(c) for c in data_columns}
    preferred = config.normalize_symbol(config.BOND_SLEEVE_SYMBOL)
    if preferred in cols:
        return preferred
    for sym in BOND_FALLBACK_SYMBOLS:
        if sym in cols:
            return sym
    return preferred


def _vix_from_context(
    *,
    vix: float | None,
    volatility: str | None,
    vol_score: float | None,
) -> float | None:
    if vix is not None:
        try:
            val = float(vix)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    try:
        from modules.options_sleeve import estimate_vix_from_vol

        est = estimate_vix_from_vol(volatility, vol_score)
        return float(est) if est else None
    except ImportError:
        return None


def bond_trigger_context(
    *,
    window,
    regime: str = "",
    volatility: str | None = None,
    vol_score: float | None = None,
    vix: float | None = None,
    macro_stress: bool = False,
    thinking_scales: dict | None = None,
) -> tuple[bool, float, str]:
    """
    Return (active, cap_pct 0..BOND_SLEEVE_CAP_PCT, reason).

    Triggers: bear/panic regime, high VIX, bond stress (TLT below MA50), macro stress.
    """
    from modules.macro_signals import bond_stress

    cap_max = config.BOND_SLEEVE_CAP_PCT
    reasons: list[str] = []
    score = 0.0

    vix_f = _vix_from_context(vix=vix, volatility=volatility, vol_score=vol_score)
    if regime in (BEAR_REGIME, PANIC_REGIME):
        score += 0.45
        reasons.append("risk-off regime")

    if vix_f is not None and vix_f >= config.BOND_VIX_TRIGGER_MIN:
        score += 0.40
        reasons.append(f"VIX {vix_f:.1f}")
        if vix_f >= config.BOND_VIX_HIGH_MIN:
            score += 0.15
            reasons.append("elevated vol")

    if window is not None and bond_stress(window):
        if vix_f is not None and vix_f >= 20:
            score += 0.20
            reasons.append("TLT below MA50")
        elif regime in (BEAR_REGIME, PANIC_REGIME):
            score += 0.15
            reasons.append("TLT stress")

    if macro_stress and vix_f is not None and vix_f >= config.BOND_VIX_TRIGGER_MIN:
        score += 0.15
        reasons.append("macro stress")

    if volatility and str(volatility).lower() == "high" and vix_f is not None and vix_f >= config.BOND_VIX_TRIGGER_MIN:
        score += 0.10
        reasons.append("cross-asset vol High")

    if thinking_scales and config.effective_thinking_engine_enabled():
        bond_tilt = float(
            thinking_scales.get("bonds")
            or thinking_scales.get("bond")
            or thinking_scales.get("cash_buffer")
            or 0.0
        )
        if bond_tilt > 0.08:
            score += 0.20
            reasons.append("thinking defensive tilt")

    if score < 0.40:
        return False, 0.0, ""

    cap = round(min(cap_max, max(0.05, cap_max * min(1.0, score))), 4)
    reason = "; ".join(reasons[:3]) if reasons else "risk-off / high VIX"
    return True, cap, reason


def _portfolio_stats(executor) -> dict:
    portfolio = getattr(executor, "portfolio", None)
    if portfolio is not None:
        stats = getattr(portfolio, "bond_stats", None)
        if stats is None:
            stats = {
                "trades": 0,
                "buys": 0,
                "sells": 0,
                "symbols": Counter(),
                "active_bars": 0,
                "max_cap_pct": 0.0,
            }
            portfolio.bond_stats = stats
        return stats
    stats = getattr(executor, "bond_stats", None)
    if stats is None:
        stats = {
            "trades": 0,
            "buys": 0,
            "sells": 0,
            "symbols": Counter(),
            "active_bars": 0,
            "max_cap_pct": 0.0,
        }
        executor.bond_stats = stats
    return stats


def note_bond_active_bar(executor, cap_pct: float) -> None:
    stats = _portfolio_stats(executor)
    stats["active_bars"] = int(stats.get("active_bars", 0) or 0) + 1
    stats["max_cap_pct"] = max(float(stats.get("max_cap_pct", 0) or 0), float(cap_pct))


def record_bond_trade(executor, symbol: str, *, side: str) -> None:
    stats = _portfolio_stats(executor)
    stats["trades"] += 1
    if side == "buy":
        stats["buys"] += 1
    else:
        stats["sells"] += 1
    stats["symbols"][config.normalize_symbol(symbol)] += 1


def bond_stats_summary(executor) -> dict[str, Any]:
    stats = _portfolio_stats(executor)
    symbols = stats.get("symbols") or Counter()
    return {
        "trades": int(stats.get("trades") or 0),
        "buys": int(stats.get("buys") or 0),
        "sells": int(stats.get("sells") or 0),
        "top_symbols": symbols.most_common(4),
        "active_bars": int(stats.get("active_bars") or 0),
        "max_cap_pct": round(float(stats.get("max_cap_pct") or 0), 4),
    }


_bond_note_logged = False


def log_bond_note_once() -> None:
    global _bond_note_logged
    if _bond_note_logged:
        return
    _bond_note_logged = True
    logger.info(BOND_RISK_NOTE)
