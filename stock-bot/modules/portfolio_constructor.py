"""Realistic Research v1.5+ sector-aware portfolio constructor.

Wires the Dynamic Sector Screener's ``sector_regime_score`` together with
bubble risk, regime conviction, and insider context into ONE per-cycle
decision covering three tilts:

  - ``active_sleeve_mult``    — scales the SPY + NYSE sleeve caps together
  - ``stat_arb_mult``         — scales the dedicated stat-arb sleeve cap
  - ``short_willingness_mult`` — scales protective-short sizing within its
                                  own existing min/max % bounds

Core VTI-vs-SPY % is intentionally NOT recomputed here — it already
incorporates the same signals (plus a sector_regime driver) via
``dynamic_vti_allocator.compute_smart_vti_core_pct()``. This module only owns
the sleeve/short *tilts* on top of that, to avoid double-counting the same
bubble/insider/regime inputs in two places.

Paper-research only: every call site is gated by
``config.effective_portfolio_constructor_enabled()`` (requires
``PORTFOLIO_CONSTRUCTOR_ENABLED`` AND ``paper_aggressive_context()``). Hard
floors/ceilings (``DYNAMIC_VTI_PAPER_FLOOR/CEILING``,
``PROTECTIVE_SHORT_MIN/MAX_PCT``) are enforced by their owning modules
regardless of this module's output — the multipliers below only tilt
*within* those existing bounds, never past them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import config

logger = logging.getLogger(__name__)

_last_decision: dict[str, Any] | None = None


@dataclass
class PortfolioConstructorContext:
    sector_regime_score: float | None = None
    bubble_score_100: float | None = None
    regime_conviction: float | None = None
    insider_cluster_buys: int = 0
    insider_exec_sells: int = 0
    cash_buffer_pct: float | None = None


@dataclass
class PortfolioDecision:
    active_sleeve_mult: float
    stat_arb_mult: float
    short_willingness_mult: float
    drivers: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


def get_last_portfolio_decision() -> dict[str, Any] | None:
    return dict(_last_decision) if _last_decision else None


def build_portfolio_context(
    *,
    data=None,
    regime: str | None = None,
    bubble_score_100: float | None = None,
    insider_state: dict[str, Any] | None = None,
) -> PortfolioConstructorContext:
    ctx = PortfolioConstructorContext(bubble_score_100=bubble_score_100)

    if data is not None and config.effective_dynamic_sector_screener():
        try:
            from modules.sector_screener import compute_sector_regime_score

            ctx.sector_regime_score = compute_sector_regime_score(data)
        except Exception as exc:
            logger.debug("sector regime score unavailable for portfolio constructor: %s", exc)

    if regime:
        try:
            from modules.risk_management import _conviction_regime_component

            ctx.regime_conviction = _conviction_regime_component(regime)
        except Exception as exc:
            logger.debug("regime conviction unavailable for portfolio constructor: %s", exc)

    if insider_state:
        ctx.insider_cluster_buys = int(
            insider_state.get("cluster_count")
            or len(insider_state.get("strong_clusters") or [])
        )
        ctx.insider_exec_sells = int(
            insider_state.get("short_count")
            or len(insider_state.get("short_candidates") or [])
        )

    try:
        ctx.cash_buffer_pct = float(config.fund_allocation_pct().get("cash_buffer", 0.0))
    except Exception as exc:
        logger.debug("cash buffer lookup failed for portfolio constructor: %s", exc)

    return ctx


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_portfolio_decision(ctx: PortfolioConstructorContext) -> PortfolioDecision:
    """Blend sector/bubble/insider/regime context into sleeve + short tilts.

    EXTENSION POINT — adding a new signal to the portfolio constructor: read
    the raw value onto ``PortfolioConstructorContext`` in
    ``build_portfolio_context`` (guard the fetch and log on failure), then
    add a driver adjustment below to whichever of the three tilts it should
    influence. Each tilt is clamped to its own configured floor/ceiling and
    the underlying hard limits (VTI floor/ceiling, short min/max %) are
    enforced independently by their owning modules.
    """
    drivers: list[str] = []
    sector = ctx.sector_regime_score
    bubble = ctx.bubble_score_100

    active_mult = 1.0
    if sector is not None:
        if sector >= 0.70:
            active_mult += 0.10
            drivers.append("broad sector rotation")
        elif sector >= 0.60:
            active_mult += 0.05
            drivers.append("firm sector breadth")
        elif sector <= 0.30:
            active_mult -= 0.10
            drivers.append("narrow sector breadth")
        elif sector <= 0.40:
            active_mult -= 0.05
            drivers.append("soft sector breadth")

    if ctx.insider_cluster_buys >= 2:
        active_mult += 0.05
        drivers.append("insider cluster buys")

    if bubble is not None and bubble >= 80 and ctx.insider_exec_sells >= 1:
        active_mult -= 0.08
        drivers.append("exec sells + high bubble")
    elif bubble is not None and bubble >= 80:
        active_mult -= 0.05
        drivers.append("high bubble risk")

    active_mult = _clamp(
        active_mult,
        float(config.PORTFOLIO_ACTIVE_SLEEVE_MULT_FLOOR),
        float(config.PORTFOLIO_ACTIVE_SLEEVE_MULT_CEILING),
    )

    # Stat arb (pairs mean reversion) tends to work best when sector rotation is
    # narrow/choppy and worse during a broad trending rotation.
    stat_arb_mult = 1.0
    if sector is not None:
        if sector <= 0.35:
            stat_arb_mult += 0.15
            drivers.append("choppy sectors favor pairs")
        elif sector >= 0.75:
            stat_arb_mult -= 0.15
            drivers.append("trending sectors, less mean reversion")
    conv = ctx.regime_conviction
    if conv is not None and 0.42 < conv < 0.58:
        stat_arb_mult += 0.05
        drivers.append("range-bound regime")

    actual_cash = config.account_cash_pct()
    deploy_threshold = config.effective_excess_cash_deploy_threshold_pct()
    if actual_cash is not None and actual_cash >= deploy_threshold:
        cash_boost = config.effective_excess_cash_sleeve_mult(actual_cash)
        active_mult = _clamp(
            active_mult * cash_boost,
            float(config.PORTFOLIO_ACTIVE_SLEEVE_MULT_FLOOR),
            float(config.PORTFOLIO_ACTIVE_SLEEVE_MULT_CEILING),
        )
        drivers.append("excess cash deploy boost")
        deploy_tilt = 1.0 + min(
            0.35,
            (actual_cash - deploy_threshold) * 1.0,
        )
        active_mult = _clamp(
            active_mult * deploy_tilt,
            float(config.PORTFOLIO_ACTIVE_SLEEVE_MULT_FLOOR),
            float(config.PORTFOLIO_ACTIVE_SLEEVE_MULT_CEILING),
        )
        drivers.append("aggressive sleeve fill")
        if actual_cash >= config.effective_excess_cash_high_threshold_pct():
            active_mult = _clamp(
                max(active_mult, float(config.PORTFOLIO_ACTIVE_SLEEVE_MULT_CEILING)),
                float(config.PORTFOLIO_ACTIVE_SLEEVE_MULT_FLOOR),
                float(config.PORTFOLIO_ACTIVE_SLEEVE_MULT_CEILING),
            )
            stat_arb_mult = _clamp(
                stat_arb_mult * 1.20,
                float(config.PORTFOLIO_STAT_ARB_MULT_FLOOR),
                float(config.PORTFOLIO_STAT_ARB_MULT_CEILING),
            )
            drivers.append("excess cash stat arb boost")
        elif actual_cash >= deploy_threshold:
            stat_arb_mult = _clamp(
                stat_arb_mult * 1.10,
                float(config.PORTFOLIO_STAT_ARB_MULT_FLOOR),
                float(config.PORTFOLIO_STAT_ARB_MULT_CEILING),
            )
            drivers.append("fill stat arb sleeve")
    elif (
        active_mult > 1.0
        and ctx.cash_buffer_pct is not None
        and ctx.cash_buffer_pct < 0.03
    ):
        # Theoretical fund model has no headroom — but don't block when broker cash is high.
        active_mult = 1.0

    stat_arb_mult = _clamp(
        stat_arb_mult,
        float(config.PORTFOLIO_STAT_ARB_MULT_FLOOR),
        float(config.PORTFOLIO_STAT_ARB_MULT_CEILING),
    )

    # Protective short willingness — bubble + exec sells + weak breadth raise it,
    # broad strong rotation with low bubble lowers it. Consumed by
    # opportunistic_short_sleeve.short_target_gross_pct() as a tilt *within* its
    # own min/max % hard bounds, never past them.
    short_mult = 1.0
    if bubble is not None:
        if bubble >= 75:
            short_mult += 0.20
            drivers.append("elevated bubble risk")
        elif bubble <= 30:
            short_mult -= 0.15
            drivers.append("low bubble risk")
    if ctx.insider_exec_sells >= 2:
        short_mult += 0.15
        drivers.append("exec selling cluster")
    if sector is not None and sector >= 0.70 and (bubble is None or bubble < 60):
        short_mult -= 0.10
        drivers.append("broad rotation, low hedge need")
    short_mult = _clamp(
        short_mult,
        float(config.PORTFOLIO_SHORT_WILLINGNESS_FLOOR),
        float(config.PORTFOLIO_SHORT_WILLINGNESS_CEILING),
    )
    if (
        actual_cash is not None
        and actual_cash >= config.effective_excess_cash_high_threshold_pct()
    ):
        short_mult = _clamp(
            short_mult * 0.85,
            float(config.PORTFOLIO_SHORT_WILLINGNESS_FLOOR),
            float(config.PORTFOLIO_SHORT_WILLINGNESS_CEILING),
        )
        drivers.append("prioritize active sleeves")

    decision = PortfolioDecision(
        active_sleeve_mult=round(active_mult, 4),
        stat_arb_mult=round(stat_arb_mult, 4),
        short_willingness_mult=round(short_mult, 4),
        drivers=drivers[:4],
        detail={
            "sector_regime_score": sector,
            "bubble_score_100": bubble,
            "regime_conviction": ctx.regime_conviction,
            "insider_cluster_buys": ctx.insider_cluster_buys,
            "insider_exec_sells": ctx.insider_exec_sells,
            "cash_buffer_pct": ctx.cash_buffer_pct,
        },
    )
    global _last_decision
    _last_decision = {
        "active_sleeve_mult": decision.active_sleeve_mult,
        "stat_arb_mult": decision.stat_arb_mult,
        "short_willingness_mult": decision.short_willingness_mult,
        "drivers": list(decision.drivers),
        "detail": dict(decision.detail),
    }
    return decision


def merge_portfolio_sleeve_caps(
    base_caps: dict[str, float], decision: PortfolioDecision
) -> dict[str, float]:
    """Apply the active-sleeve tilt to SPY/NYSE caps; recompute cash_buffer residual.

    Stat-arb is deliberately excluded here — it is sized from the dedicated
    ``config.effective_stat_arb_cap()`` path (not this dict) so its tilt is
    applied there instead, avoiding a double-apply.

    When paper high-cash deploy is active, allow a temporary total of up to
    1.02 (slight over-deploy); clamp sleeves to that ceiling instead of
    discarding the tilt. Rebalance can unwind the excess next cycle.
    """
    caps = dict(base_caps)
    caps["spy"] = round(caps.get("spy", 0.0) * decision.active_sleeve_mult, 6)
    caps["nyse"] = round(caps.get("nyse", 0.0) * decision.active_sleeve_mult, 6)

    metal = caps.get("metal", 0.0)
    vti = caps.get("vti_core", 0.0)
    # Paper research: allow slight temporary over-deploy when cash is high (or
    # always in paper-aggressive context so tilts aren't discarded before
    # account profile is wired). Rebalance can unwind next cycle.
    high_cash = False
    try:
        high_cash = bool(config.paper_deploy_aggressive())
    except Exception:
        high_cash = False
    if not high_cash and config.paper_aggressive_context():
        # Fallback: paper bot always gets the 2% buffer so constructor tilts apply.
        high_cash = True
    max_total = 1.02 if high_cash else 1.0
    long_budget = max(0.0, max_total - metal - vti)
    long_sum = caps.get("spy", 0.0) + caps.get("crypto", 0.0) + caps.get("nyse", 0.0)
    if long_sum > long_budget + 1e-9 and long_sum > 0:
        scale = long_budget / long_sum
        caps["spy"] = round(caps.get("spy", 0.0) * scale, 6)
        caps["nyse"] = round(caps.get("nyse", 0.0) * scale, 6)
        caps["crypto"] = round(caps.get("crypto", 0.0) * scale, 6)
        long_sum = caps.get("spy", 0.0) + caps.get("crypto", 0.0) + caps.get("nyse", 0.0)
        logger.info(
            "portfolio_constructor clamped sleeves to %.0f%% total "
            "(high-cash over-deploy buffer); scale=%.3f",
            max_total * 100.0,
            scale,
        )

    cash_buffer = round(1.0 - metal - vti - long_sum, 6)
    # Slight negative cash_buffer is OK under the 1.02 high-cash ceiling.
    if cash_buffer < -0.021:
        logger.warning(
            "portfolio_constructor sleeve tilt would over-allocate fund (cash=%.4f); "
            "keeping base caps unscaled this cycle",
            cash_buffer,
        )
        return dict(base_caps)
    caps["cash_buffer"] = max(0.0, cash_buffer)
    return caps


def format_portfolio_constructor_banner(decision: PortfolioDecision) -> str:
    labels = [d for d in decision.drivers if d]
    driver_text = " + ".join(labels[:3]) if labels else "neutral sector regime"
    return (
        f"Portfolio Constructor — active x{decision.active_sleeve_mult:.2f} | "
        f"stat arb x{decision.stat_arb_mult:.2f} | "
        f"short willingness x{decision.short_willingness_mult:.2f} — {driver_text}"
    )
