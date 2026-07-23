"""Smart Dynamic VTI core allocator for Realistic Research v1.5+.

Floor/ceiling are sourced from ``config`` (``DYNAMIC_VTI_PAPER_FLOOR`` /
``DYNAMIC_VTI_PAPER_CEILING``) at decision time so runtime enforcement and
env overrides are always respected.

Optional VTI (paper-first): when SPY-like confluence is strong, the effective
floor may drop to ``DYNAMIC_VTI_FLOOR_MIN`` (~20%) or 0% if allow-zero is on.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import config

logger = logging.getLogger(__name__)

_MOM_LOOKBACK = int(os.getenv("DYNAMIC_VTI_MOM_LOOKBACK", "20"))
_EXCLUDE_MOM = frozenset(
    {
        "VTI",
        "SPY",
        "GLD",
        "SLV",
        "CPER",
        "BIL",
        "TLT",
        "IEF",
        "SHY",
        "BTC-USD",
        "ETH-USD",
    }
)

_last_decision: dict[str, Any] | None = None


@dataclass
class VtiAllocatorContext:
    vol_score: float | None = None
    volatility: str | None = None
    macro_stress: bool = False
    regime: str | None = None
    data: Any = None
    bubble_score_100: float | None = None
    buffett_ratio_pct: float | None = None
    buffett_signal: str | None = None
    insider_cluster_buys: int = 0
    insider_exec_sells: int = 0
    nyse_momentum: float | None = None
    metal_momentum: float | None = None
    vti_vs_spy_momentum: float | None = None
    regime_conviction: float | None = None
    sector_regime_score: float | None = None
    hmm_vti_adj_pp: float | None = None
    hmm_predicted: str | None = None
    hmm_confidence: float | None = None
    garch_vti_adj_pp: float | None = None
    garch_size_mult: float | None = None
    garch_ratio: float | None = None
    spy_like_strength: float | None = None
    spy_like_hits: int = 0
    spy_like_scored: int = 0


@dataclass
class VtiAllocationDecision:
    pct: float
    base_pct: float
    adjustment_pp: float
    drivers: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    floor: float = 0.35


def get_last_vti_allocation_decision() -> dict[str, Any] | None:
    return dict(_last_decision) if _last_decision else None


def _resolve_vol_score(vol_score: float | None, volatility: str | None) -> float | None:
    if vol_score is not None:
        return float(vol_score)
    if volatility == "High":
        return 0.02
    if volatility == "Low":
        return 0.01
    return None


def _vol_stress_base_pct(
    vol_score: float | None,
    volatility: str | None,
    macro_stress: bool,
) -> float:
    from modules.fund_config import (
        _VTI_CALM,
        _VTI_DEFAULT_AGGRESSIVE,
        _VTI_STRESS,
        _VTI_VOL_CALM,
        _VTI_VOL_STRESS,
    )

    score = _resolve_vol_score(vol_score, volatility)
    if macro_stress or (score is not None and score > _VTI_VOL_STRESS):
        return _VTI_STRESS
    if score is not None and score < _VTI_VOL_CALM:
        return _VTI_CALM
    return _VTI_DEFAULT_AGGRESSIVE


def _symbol_momentum(data, symbol: str, lookback: int = _MOM_LOOKBACK) -> float | None:
    if data is None or getattr(data, "empty", True):
        return None
    sym = str(symbol or "").upper()
    col = None
    for name in (sym, f"{sym}-USD"):
        if name in data.columns:
            col = name
            break
    if col is None:
        return None
    series = data[col].dropna()
    if len(series) < lookback + 1:
        return None
    start = float(series.iloc[-(lookback + 1)])
    end = float(series.iloc[-1])
    if start <= 0:
        return None
    return (end / start) - 1.0


def _cross_section_momentum(data, lookback: int = _MOM_LOOKBACK) -> float | None:
    if data is None or getattr(data, "empty", True) or len(data) < lookback + 1:
        return None
    import numpy as np

    sub = data.iloc[-(lookback + 1) :]
    rets: list[float] = []
    for col in sub.columns:
        key = str(col).upper().split("/")[0]
        if key in _EXCLUDE_MOM:
            continue
        series = sub[col].dropna()
        if len(series) < lookback + 1:
            continue
        start = float(series.iloc[0])
        end = float(series.iloc[-1])
        if start <= 0:
            continue
        rets.append((end / start) - 1.0)
    if not rets:
        return None
    return float(np.median(rets))


def _regime_conviction(regime: str | None) -> float | None:
    if not regime:
        return None
    try:
        from modules.risk_management import _conviction_regime_component

        return _conviction_regime_component(regime)
    except Exception:
        reg = str(regime).upper()
        if "RHYME_C" in reg or "BULL" in reg:
            return 0.72
        if "RHYME_E" in reg or "BEAR" in reg:
            return 0.28
        if "RHYME_B" in reg or "PANIC" in reg:
            return 0.15
        return 0.50


def _strong_rvol(symbol: str, data) -> bool:
    try:
        from modules.volume_analysis import calculate_rvol

        rvol = calculate_rvol(data, symbol)
        if rvol is None:
            return False
        return float(rvol) >= float(config.RVOL_MOMENTUM_BOOST_THRESHOLD)
    except Exception:
        return False


def _strong_orb(symbol: str, data) -> bool:
    try:
        from modules.orb_strategy import calculate_opening_range
        from modules.volume_analysis import calculate_rvol

        or_info = calculate_opening_range(
            data, symbol, minutes=int(getattr(config, "ORB_BREAKOUT_MINUTES", 30))
        )
        if not or_info or not or_info.get("breakout_up"):
            return False
        rvol = calculate_rvol(data, symbol)
        return rvol is not None and float(rvol) >= float(config.ORB_RVOL_MIN)
    except Exception:
        return False


def _strong_catalyst(symbol: str, data) -> bool:
    try:
        from modules.catalyst_scoring import score_catalysts

        row = score_catalysts(data, symbol)
        score = float(row.get("score") or 0)
        boost_at = float(getattr(config, "CATALYST_MIN_SCORE", 65))
        try:
            boost_at = max(boost_at, float(os.getenv("CATALYST_BOOST_SCORE", "70")))
        except Exception:
            pass
        return score >= boost_at
    except Exception:
        return False


def _strong_insider(symbol: str) -> bool:
    if not config.effective_insider_signal_boost_enabled():
        return False
    try:
        from modules.insider_signal_handler import momentum_rank_boost

        return float(momentum_rank_boost(symbol)) > 0.0
    except Exception:
        return False


def score_spy_like_confluence(symbol: str, data=None) -> dict[str, Any]:
    """RVOL + ORB + Catalyst + insider confluence for one SPY-like name (0–1)."""
    sym = config.normalize_symbol(symbol)
    flags = {
        "rvol": bool(data is not None and _strong_rvol(sym, data)),
        "orb": bool(data is not None and _strong_orb(sym, data)),
        "catalyst": bool(data is not None and _strong_catalyst(sym, data)),
        "insider": _strong_insider(sym),
    }
    hits = sum(1 for v in flags.values() if v)
    need = max(1, int(getattr(config, "SPY_LIKE_CONFLUENCE_MIN", 3)))
    strength = round(hits / 4.0, 4)
    return {
        "symbol": sym,
        "flags": flags,
        "hits": hits,
        "strength": strength,
        "confluent": hits >= need,
    }


def compute_spy_like_universe_strength(data=None) -> dict[str, Any]:
    """Portfolio-level SPY-like strength (mean of per-name confluence scores)."""
    universe = list(config.spy_like_universe())
    if not universe:
        return {"strength": 0.0, "hits": 0, "scored": 0, "names": []}
    rows: list[dict[str, Any]] = []
    for sym in universe:
        if data is not None and not getattr(data, "empty", True):
            cols = {str(c).upper() for c in data.columns}
            if sym not in cols and f"{sym}-USD" not in cols:
                continue
        row = score_spy_like_confluence(sym, data)
        rows.append(row)
    if not rows:
        return {"strength": 0.0, "hits": 0, "scored": 0, "names": []}
    strength = sum(float(r["strength"]) for r in rows) / len(rows)
    confluent = [r for r in rows if r.get("confluent")]
    if confluent:
        strength = max(strength, sum(float(r["strength"]) for r in confluent) / len(rows))
    return {
        "strength": round(min(1.0, strength), 4),
        "hits": len(confluent),
        "scored": len(rows),
        "names": [r["symbol"] for r in confluent[:5]],
    }


def spy_like_size_boost(symbol: str | None, data=None) -> float:
    """Conservative 1.05–1.2x size mult for SPY-like names with strong confluence."""
    if not symbol or not config.effective_spy_like_boost_enabled():
        return 1.0
    if not config.is_spy_like_symbol(symbol):
        return 1.0
    row = score_spy_like_confluence(symbol, data)
    if not row.get("confluent"):
        return 1.0
    base = float(getattr(config, "SPY_LIKE_BOOST_MULT", 1.10))
    hits = int(row.get("hits") or 0)
    lo = float(getattr(config, "SPY_LIKE_BOOST_MULT_MIN", 1.05))
    hi = float(getattr(config, "SPY_LIKE_BOOST_MULT_MAX", 1.20))
    span = max(0.01, hi - lo)
    t = min(1.0, max(0.0, (hits - 3) / 1.0))
    mult = lo + span * (0.5 + 0.5 * t) if hits >= 3 else 1.0
    if hits >= 4:
        mult = max(mult, base)
    return config.clamp_spy_like_boost_mult(mult)


def build_vti_allocator_context(
    *,
    data=None,
    regime: str | None = None,
    vol_score: float | None = None,
    volatility: str | None = None,
    macro_stress: bool = False,
    bubble_score_100: float | None = None,
    insider_state: dict[str, Any] | None = None,
) -> VtiAllocatorContext:
    ctx = VtiAllocatorContext(
        data=data,
        regime=regime,
        vol_score=vol_score,
        volatility=volatility,
        macro_stress=macro_stress,
        regime_conviction=_regime_conviction(regime),
        nyse_momentum=_cross_section_momentum(data),
        metal_momentum=_symbol_momentum(data, "GLD"),
    )
    vti_mom = _symbol_momentum(data, config.VTI_CORE_SYMBOL)
    spy_mom = _symbol_momentum(data, config.SPY_BOT_SYMBOL)
    if vti_mom is not None and spy_mom is not None:
        ctx.vti_vs_spy_momentum = round(vti_mom - spy_mom, 4)

    # Sector regime driver ships alongside the portfolio constructor (v1.5.4) — gating both
    # on the same flag keeps a single on/off switch for A/B comparisons against pre-v1.5.4.
    if (
        data is not None
        and config.effective_dynamic_sector_screener()
        and config.effective_portfolio_constructor_enabled()
    ):
        try:
            from modules.sector_screener import compute_sector_regime_score

            ctx.sector_regime_score = compute_sector_regime_score(data)
        except Exception as exc:
            logger.debug("sector regime score unavailable for VTI allocator: %s", exc)

    if config.effective_markov_hmm_enabled():
        try:
            from modules.markov_regime import get_last_hmm_prediction, hmm_vti_adjustment_pp

            pred = get_last_hmm_prediction()
            if pred and pred.get("ok"):
                ctx.hmm_vti_adj_pp = float(pred.get("vti_adj_pp") or hmm_vti_adjustment_pp())
                ctx.hmm_predicted = str(pred.get("predicted_next") or "")
                ctx.hmm_confidence = float(pred.get("confidence") or 0.0)
            else:
                ctx.hmm_vti_adj_pp = float(hmm_vti_adjustment_pp())
        except Exception as exc:
            logger.debug("HMM VTI driver unavailable: %s", exc)

    if config.effective_garch_vol_enabled():
        try:
            from modules.garch_vol import (
                get_garch_vol_state,
                garch_vol_vti_adjustment_pp,
                update_garch_vol,
            )

            st = get_garch_vol_state()
            if not st.ok and data is not None:
                st = update_garch_vol(data)
            if st.ok:
                ctx.garch_vti_adj_pp = float(
                    st.vti_adj_pp if st.vti_adj_pp is not None else garch_vol_vti_adjustment_pp()
                )
                ctx.garch_size_mult = float(st.size_mult)
                ctx.garch_ratio = float(st.ratio) if st.ratio is not None else None
            else:
                ctx.garch_vti_adj_pp = float(garch_vol_vti_adjustment_pp())
        except Exception as exc:
            logger.debug("GARCH VTI driver unavailable: %s", exc)

    if bubble_score_100 is None and data is not None and regime:
        try:
            from modules.bubble_risk import compute_bubble_risk

            bubble = compute_bubble_risk(
                data,
                regime,
                volatility=volatility,
                vol_score=vol_score,
            )
            bubble_score_100 = float(bubble.get("score_100") or 0.0)
            buff = bubble.get("buffett") or {}
            ctx.buffett_ratio_pct = buff.get("ratio_pct")
            ctx.buffett_signal = buff.get("signal")
        except Exception as exc:
            logger.debug("bubble/Buffett context unavailable for VTI allocator: %s", exc)
    ctx.bubble_score_100 = bubble_score_100

    if insider_state:
        ctx.insider_cluster_buys = int(
            insider_state.get("cluster_count")
            or len(insider_state.get("strong_clusters") or [])
        )
        ctx.insider_exec_sells = int(
            insider_state.get("short_count")
            or len(insider_state.get("short_candidates") or [])
        )
    elif config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import get_boost_snapshot

            state = get_boost_snapshot() or {}
            ctx.insider_cluster_buys = int(
                state.get("cluster_count") or len(state.get("strong_clusters") or [])
            )
            ctx.insider_exec_sells = int(
                state.get("short_count") or len(state.get("short_candidates") or [])
            )
        except Exception as exc:
            logger.debug("insider boost snapshot unavailable for VTI allocator: %s", exc)

    if config.effective_dynamic_vti_optional() or config.effective_spy_like_boost_enabled():
        try:
            spy_like = compute_spy_like_universe_strength(data)
            ctx.spy_like_strength = float(spy_like.get("strength") or 0.0)
            ctx.spy_like_hits = int(spy_like.get("hits") or 0)
            ctx.spy_like_scored = int(spy_like.get("scored") or 0)
        except Exception as exc:
            logger.debug("SPY-like strength unavailable for VTI allocator: %s", exc)

    return ctx


def _driver_points(ctx: VtiAllocatorContext) -> tuple[float, list[tuple[float, str]]]:
    """Return net adjustment in percentage points and ranked driver labels.

    EXTENSION POINT — adding a new signal to the Smart Dynamic VTI core:
      1. Add the raw field to ``VtiAllocatorContext`` and populate it in
         ``build_vti_allocator_context`` (guard the fetch and log on failure).
      2. Append ``(points, "label")`` tuples below. Sign convention:
         POSITIVE points push toward MORE VTI (defensive/passive), NEGATIVE
         points push toward LESS VTI (favor active sleeves). Magnitude = conviction.
      3. Nothing else changes: ``net`` sums all points and the final pct is clamped
         to [effective floor, DYNAMIC_VTI_PAPER_CEILING] by the caller, and
         the top-3 labels by |points| are surfaced as the banner drivers.
    """
    scored: list[tuple[float, str]] = []

    if ctx.macro_stress:
        scored.append((8.0, "macro stress"))

    score = _resolve_vol_score(ctx.vol_score, ctx.volatility)
    if score is not None and score > 0.022:
        scored.append((6.0, "elevated vol"))
    elif score is not None and score < 0.014:
        scored.append((-4.0, "calm vol"))

    nyse = ctx.nyse_momentum
    if nyse is not None:
        if nyse >= 0.05:
            scored.append((-16.0, "strong NYSE momentum"))
        elif nyse >= 0.02:
            scored.append((-9.0, "firm NYSE momentum"))
        elif nyse <= -0.03:
            scored.append((5.0, "weak NYSE momentum"))

    metal = ctx.metal_momentum
    if metal is not None:
        if metal >= 0.04:
            scored.append((-12.0, "gold strength"))
        elif metal >= 0.015:
            scored.append((-7.0, "metals bid"))
        elif metal <= -0.02:
            scored.append((3.0, "metals soft"))

    if ctx.insider_cluster_buys >= 2:
        scored.append((-14.0, "insider cluster buys"))
    elif ctx.insider_cluster_buys == 1:
        scored.append((-8.0, "insider buying"))

    if ctx.insider_exec_sells >= 2:
        scored.append((16.0, "exec selling"))
    elif ctx.insider_exec_sells == 1:
        scored.append((9.0, "insider sells"))

    bubble = ctx.bubble_score_100
    if bubble is not None:
        if bubble >= 80:
            scored.append((14.0, "high bubble risk"))
        elif bubble >= 65:
            scored.append((8.0, "elevated bubble"))
        elif bubble <= 35:
            scored.append((-5.0, "low bubble"))

    if ctx.insider_exec_sells >= 1 and bubble is not None and bubble >= 65:
        combo = 10.0 + 4.0 * min(ctx.insider_exec_sells - 1, 1)
        scored.append((combo, "exec sells + high bubble"))

    buff = ctx.buffett_ratio_pct
    if buff is not None:
        if buff >= 200:
            scored.append((8.0, "Buffett overvalued"))
        elif buff >= 180:
            scored.append((4.0, "Buffett elevated"))
        elif buff < 120:
            scored.append((-4.0, "Buffett fair/undervalued"))

    conv = ctx.regime_conviction
    if conv is not None:
        if conv >= 0.68:
            scored.append((-8.0, "bullish regime"))
        elif conv <= 0.32:
            scored.append((10.0, "bearish regime"))
        elif conv <= 0.42:
            scored.append((5.0, "weak regime"))

    rel = ctx.vti_vs_spy_momentum
    if rel is not None:
        if rel >= 0.025:
            scored.append((6.0, "VTI leading SPY"))
        elif rel >= 0.01:
            scored.append((3.0, "VTI momentum edge"))
        elif rel <= -0.025:
            scored.append((-8.0, "SPY/active leading"))
        elif rel <= -0.01:
            scored.append((-4.0, "active momentum edge"))

    sector = ctx.sector_regime_score
    if sector is not None:
        if sector >= 0.70:
            scored.append((-10.0, "broad sector rotation"))
        elif sector >= 0.60:
            scored.append((-5.0, "firm sector breadth"))
        elif sector <= 0.30:
            scored.append((8.0, "narrow sector breadth"))
        elif sector <= 0.40:
            scored.append((4.0, "soft sector breadth"))

    hmm_adj = ctx.hmm_vti_adj_pp
    if hmm_adj is None:
        try:
            from modules.markov_regime import hmm_vti_adjustment_pp

            hmm_adj = hmm_vti_adjustment_pp()
        except Exception as exc:
            logger.debug("HMM VTI soft-signal skipped: %s", exc)
            hmm_adj = None
    if hmm_adj is not None and abs(float(hmm_adj)) >= 0.5:
        scored.append((float(hmm_adj), "HMM next-regime"))

    garch_adj = ctx.garch_vti_adj_pp
    if garch_adj is None:
        try:
            from modules.garch_vol import garch_vol_vti_adjustment_pp

            garch_adj = garch_vol_vti_adjustment_pp()
        except Exception as exc:
            logger.debug("GARCH VTI soft-signal skipped: %s", exc)
            garch_adj = None
    if garch_adj is not None and abs(float(garch_adj)) >= 0.5:
        scored.append((float(garch_adj), "GARCH vol forecast"))

    # SPY-like confluence: favor active sleeves / optional lower VTI floor.
    spy_str = ctx.spy_like_strength
    if spy_str is not None and config.effective_dynamic_vti_optional():
        if spy_str >= float(getattr(config, "SPY_LIKE_STRENGTH_ALLOW_ZERO", 0.85)):
            scored.append((-18.0, "SPY-like confluence (optional VTI)"))
        elif spy_str >= float(getattr(config, "SPY_LIKE_STRENGTH_REDUCE_FLOOR", 0.60)):
            scored.append((-12.0, "strong SPY-like signals"))
        elif spy_str >= 0.40:
            scored.append((-6.0, "firm SPY-like signals"))

    net = sum(pt for pt, _ in scored)
    ranked = sorted(scored, key=lambda row: abs(row[0]), reverse=True)
    return net, ranked


def compute_smart_vti_core_pct(
    equity: float,
    ctx: VtiAllocatorContext | None = None,
    *,
    vol_score: float | None = None,
    macro_stress: bool = False,
    volatility: str | None = None,
    regime: str | None = None,
    data=None,
    bubble_score_100: float | None = None,
    insider_state: dict[str, Any] | None = None,
) -> VtiAllocationDecision:
    # `equity` is currently unused: the allocator returns a target VTI *fraction*,
    # not a dollar amount. It is kept as the leading positional arg because every
    # caller has equity on hand and to reserve the hook for future equity-scaled
    # sizing without another signature break.
    if ctx is None:
        ctx = build_vti_allocator_context(
            data=data,
            regime=regime,
            vol_score=vol_score,
            volatility=volatility,
            macro_stress=macro_stress,
            bubble_score_100=bubble_score_100,
            insider_state=insider_state,
        )

    base = _vol_stress_base_pct(ctx.vol_score, ctx.volatility, ctx.macro_stress)
    adj_pp, ranked = _driver_points(ctx)
    raw = base + (adj_pp / 100.0)
    # Daily profit banking: park more in VTI/cash for the rest of the day.
    try:
        from modules.daily_profit_banking import daily_bank_vti_boost_pp

        bank_pp = float(daily_bank_vti_boost_pp())
        if bank_pp:
            raw += bank_pp / 100.0
            ranked = list(ranked) + [(bank_pp, f"banked +{bank_pp:.0f}pp VTI")]
    except Exception as exc:
        logger.debug("daily bank VTI boost skipped: %s", exc)
    floor = float(config.resolve_dynamic_vti_floor(ctx.spy_like_strength))
    ceiling = float(config.DYNAMIC_VTI_PAPER_CEILING)
    pct = max(floor, min(ceiling, raw))

    drivers: list[str] = [label for _pt, label in ranked[:3]]
    if not drivers:
        if ctx.macro_stress:
            drivers.append("stress defensive")
        elif base <= 0.52:
            drivers.append("calm vol")
        else:
            drivers.append("balanced vol")

    decision = VtiAllocationDecision(
        pct=round(pct, 4),
        base_pct=round(base, 4),
        adjustment_pp=round(adj_pp, 2),
        drivers=drivers[:3],
        floor=round(floor, 4),
        detail={
            "nyse_momentum": ctx.nyse_momentum,
            "metal_momentum": ctx.metal_momentum,
            "bubble_score_100": ctx.bubble_score_100,
            "buffett_ratio_pct": ctx.buffett_ratio_pct,
            "insider_cluster_buys": ctx.insider_cluster_buys,
            "insider_exec_sells": ctx.insider_exec_sells,
            "regime_conviction": ctx.regime_conviction,
            "vti_vs_spy_momentum": ctx.vti_vs_spy_momentum,
            "sector_regime_score": ctx.sector_regime_score,
            "spy_like_strength": ctx.spy_like_strength,
            "spy_like_hits": ctx.spy_like_hits,
            "spy_like_scored": ctx.spy_like_scored,
            "effective_floor": round(floor, 4),
            "optional_vti": config.effective_dynamic_vti_optional(),
        },
    )
    global _last_decision
    _last_decision = {
        "pct": decision.pct,
        "base_pct": decision.base_pct,
        "adjustment_pp": decision.adjustment_pp,
        "drivers": list(decision.drivers),
        "floor": decision.floor,
        "detail": dict(decision.detail),
    }
    return decision


def format_dynamic_vti_banner(pct: float, drivers: list[str] | None = None) -> str:
    labels = [d for d in (drivers or []) if d]
    floor_note = ""
    last = _last_decision or {}
    eff_floor = last.get("floor")
    if (
        eff_floor is not None
        and float(eff_floor) < float(config.DYNAMIC_VTI_PAPER_FLOOR) - 1e-9
    ):
        floor_note = f" [floor {float(eff_floor):.0%}]"
    if labels:
        driver_text = " + ".join(labels[:3])
        return f"Smart Dynamic VTI {pct:.0%}{floor_note} — {driver_text}"
    return f"Smart Dynamic VTI {pct:.0%}{floor_note} — vol/stress baseline"


def format_startup_smart_vti_banner(
    *,
    data=None,
    equity: float | None = None,
    regime: str | None = None,
    vol_score: float | None = None,
    volatility: str | None = None,
    macro_stress: bool = False,
    insider_state: dict[str, Any] | None = None,
) -> str | None:
    """Live/paper startup line with current target % and top drivers."""
    if not (
        config.paper_aggressive_context() or config.backtest_paper_sleeves_context()
    ):
        return None
    if not config.PAPER_DYNAMIC_VTI_ENABLED:
        return None
    eq = float(equity or config._account_equity or 0.0)
    if eq <= 0:
        eq = float(config.SMALL_ACCOUNT_BACKTEST_EQUITY)

    ctx = build_vti_allocator_context(
        data=data,
        regime=regime,
        vol_score=vol_score,
        volatility=volatility,
        macro_stress=macro_stress,
        insider_state=insider_state,
    )
    decision = compute_smart_vti_core_pct(eq, ctx)
    return format_dynamic_vti_banner(decision.pct, decision.drivers)
