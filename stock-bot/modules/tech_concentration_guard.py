"""Tech concentration guard — limits tech/semiconductor over-exposure (paper-first)."""

from __future__ import annotations

from typing import Any

import config

# Approximate tech weight inside passive sleeves
SPY_TECH_SHARE = 0.31
VTI_TECH_SHARE = 0.27
DEFAULT_NYSE_TECH_FRAC = 0.50

_GUARD_STATS = {"rank_reorders": 0, "nyse_skips": 0, "tilt_clamps": 0}

_SEMIS_EXTRA = frozenset({"SMCI", "NVDA", "AMD", "MU", "AVGO", "QCOM", "MRVL", "ASML"})


def _prices_mapping(prices) -> dict:
    if isinstance(prices, dict):
        return prices
    if prices is None:
        return {}
    if hasattr(prices, "to_dict"):
        return prices.to_dict()
    return {}


def is_tech_equity(symbol: str) -> bool:
    """Tech or semiconductor single-name (includes SPY/QQQ as tech-beta proxies)."""
    sym = config.normalize_symbol(symbol)
    if sym in ("SPY", "QQQ", "SMH", "XLK", "IGV"):
        return True
    if sym in _SEMIS_EXTRA:
        return True
    try:
        from modules.pipeline_strategies import _is_nyse_tech

        if _is_nyse_tech(sym):
            return True
    except ImportError:
        pass
    try:
        from modules.sector_rotation import TECH, ticker_sector

        return ticker_sector(sym) == TECH
    except ImportError:
        return False


def nyse_tech_fraction(positions: dict, prices: dict) -> float:
    """Share of held equity notional in tech/semis (0–1)."""
    total = 0.0
    tech = 0.0
    for sym, qty in (positions or {}).items():
        q = float(qty or 0.0)
        if q <= 0:
            continue
        price = prices.get(sym)
        if price is None:
            continue
        val = abs(q * float(price))
        total += val
        if is_tech_equity(sym):
            tech += val
    if total < 1.0:
        return DEFAULT_NYSE_TECH_FRAC
    return tech / total


def tech_weight(
    caps: dict[str, float],
    *,
    positions: dict | None = None,
    prices: dict | None = None,
    cap_deltas: dict[str, float] | None = None,
    nyse_tech_frac: float | None = None,
) -> float:
    """Total portfolio tech exposure (includes VTI passive tech component)."""
    projected = {
        k: float(caps.get(k, 0.0)) + float((cap_deltas or {}).get(k, 0.0))
        for k in ("spy", "vti_core", "nyse", "crypto", "metal", "cash_buffer")
    }
    frac = nyse_tech_frac
    if frac is None and positions:
        if prices is not None:
            frac = nyse_tech_fraction(positions, _prices_mapping(prices))
        else:
            frac = DEFAULT_NYSE_TECH_FRAC
    elif frac is None:
        frac = DEFAULT_NYSE_TECH_FRAC
    return (
        projected.get("spy", 0.0) * SPY_TECH_SHARE
        + projected.get("vti_core", 0.0) * VTI_TECH_SHARE
        + projected.get("nyse", 0.0) * frac
    )


def active_tech_concentration(
    caps: dict[str, float],
    *,
    positions: dict | None = None,
    prices: dict | None = None,
    cap_deltas: dict[str, float] | None = None,
    nyse_tech_frac: float | None = None,
) -> float:
    """Tech share within active sleeves (SPY + NYSE) — drives guard when > limit."""
    projected = {
        k: float(caps.get(k, 0.0)) + float((cap_deltas or {}).get(k, 0.0))
        for k in ("spy", "nyse")
    }
    frac = nyse_tech_frac
    if frac is None and positions:
        frac = nyse_tech_fraction(
            positions, _prices_mapping(prices) if prices is not None else {}
        )
    elif frac is None:
        frac = DEFAULT_NYSE_TECH_FRAC
    active = projected.get("spy", 0.0) + projected.get("nyse", 0.0)
    if active < 0.05:
        return 0.0
    tech_active = projected.get("spy", 0.0) * SPY_TECH_SHARE + projected.get(
        "nyse", 0.0
    ) * frac
    return tech_active / active


def _guard_triggered(
    caps: dict[str, float],
    *,
    positions: dict | None = None,
    prices: dict | None = None,
    cap_deltas: dict[str, float] | None = None,
) -> bool:
    total = tech_weight(
        caps, positions=positions, prices=prices, cap_deltas=cap_deltas
    )
    active = active_tech_concentration(
        caps, positions=positions, prices=prices, cap_deltas=cap_deltas
    )
    limit = config.TECH_CONCENTRATION_LIMIT
    if total > limit or active > limit:
        return True
    if positions:
        frac = nyse_tech_fraction(
            positions, _prices_mapping(prices) if prices is not None else {}
        )
        nyse_cap = float(caps.get("nyse", 0.0)) + float(
            (cap_deltas or {}).get("nyse", 0.0)
        )
        if frac > 0.55 and nyse_cap > 0.06:
            return True
    return False


def exposure_summary(
    caps: dict[str, float],
    *,
    positions: dict | None = None,
    prices: dict | None = None,
    cap_deltas: dict[str, float] | None = None,
) -> dict[str, Any]:
    current = tech_weight(caps, positions=positions, prices=prices)
    projected = tech_weight(
        caps, positions=positions, prices=prices, cap_deltas=cap_deltas
    )
    active = active_tech_concentration(
        caps, positions=positions, prices=prices, cap_deltas=cap_deltas
    )
    return {
        "tech_weight": round(current, 4),
        "projected_tech_weight": round(projected, 4),
        "active_tech_concentration": round(active, 4),
        "limit": config.TECH_CONCENTRATION_LIMIT,
        "guard_active": _guard_triggered(
            caps, positions=positions, prices=prices, cap_deltas=cap_deltas
        ),
    }


def apply_guard_to_cap_deltas(
    deltas: dict[str, float],
    base_caps: dict[str, float],
    *,
    positions: dict | None = None,
    prices: dict | None = None,
) -> dict[str, float]:
    """Cap tech tilts (+4% SPY max when heavy); boost cash/NYSE non-tech sleeves."""
    if not config.effective_tech_guard_enabled():
        return deltas
    if not _guard_triggered(
        base_caps, positions=positions, prices=prices, cap_deltas=deltas
    ):
        return deltas

    out = dict(deltas)
    sleeve_cap = config.effective_thinking_max_sleeve_delta()
    max_spy = config.TECH_GUARD_MAX_SPY_TILT
    active = active_tech_concentration(
        base_caps, positions=positions, prices=prices, cap_deltas=deltas
    )
    total = tech_weight(
        base_caps, positions=positions, prices=prices, cap_deltas=deltas
    )
    excess = max(active, total) - config.TECH_CONCENTRATION_LIMIT
    trim = min(0.05, max(0.01, excess * 0.6))
    _GUARD_STATS["tilt_clamps"] += 1

    if out.get("spy", 0.0) > max_spy:
        out["spy"] = max_spy
    out["spy"] = out.get("spy", 0.0) - trim
    out["nyse"] = out.get("nyse", 0.0) - trim * 0.25
    out["cash_buffer"] = out.get("cash_buffer", 0.0) + trim * 0.45
    out["nyse"] = out.get("nyse", 0.0) + trim * 0.35
    out["metal"] = out.get("metal", 0.0) + trim * 0.10

    return {
        k: round(max(-sleeve_cap, min(sleeve_cap, float(v))), 6)
        for k, v in out.items()
    }


def apply_guard_to_ranked(
    ranked: list[str],
    caps: dict[str, float],
    *,
    positions: dict | None = None,
    prices: dict | None = None,
) -> list[str]:
    """Deprioritize tech names when projected exposure exceeds limit."""
    if not ranked or not config.effective_tech_guard_enabled():
        return ranked
    if not _guard_triggered(caps, positions=positions, prices=prices):
        return ranked
    non_tech = [s for s in ranked if not is_tech_equity(s)]
    tech = [s for s in ranked if is_tech_equity(s)]
    if non_tech and tech and non_tech[0] != ranked[0]:
        _GUARD_STATS["rank_reorders"] += 1
    return non_tech + tech


def should_skip_tech_nyse_buy(
    symbol: str,
    caps: dict[str, float],
    *,
    positions: dict | None = None,
    prices: dict | None = None,
) -> bool:
    """Block new tech momentum buys when already tech-heavy."""
    if not is_tech_equity(symbol) or not config.effective_tech_guard_enabled():
        return False
    if _guard_triggered(caps, positions=positions, prices=prices):
        _GUARD_STATS["nyse_skips"] += 1
        return True
    return False


def reset_guard_stats() -> None:
    _GUARD_STATS.update(rank_reorders=0, nyse_skips=0, tilt_clamps=0)


def guard_stats() -> dict[str, int]:
    return dict(_GUARD_STATS)
