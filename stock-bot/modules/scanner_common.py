"""Shared helpers for RVOL / ORB / catalyst scanner modules."""

from __future__ import annotations

import logging
from typing import Any, Callable

import config

logger = logging.getLogger(__name__)

DEFAULT_SCAN_UNIVERSE_CAP = 50


def nyse_scan_universe(data, *, limit_scan: int = DEFAULT_SCAN_UNIVERSE_CAP) -> list[str]:
    """NYSE momentum universe capped for on-demand scanner scans."""
    if data is None or not hasattr(data, "columns"):
        return []
    try:
        primary = config.nyse_momentum_universe(data.columns)
        return list(primary[:limit_scan])
    except Exception as exc:
        logger.debug("nyse momentum universe failed, using column fallback: %s", exc)
        cols = [str(c) for c in data.columns if config._nyse_eligible_symbol(str(c))]
        return cols[:limit_scan]


def bump_boost_for_insider_cluster(
    base_boost: float,
    symbol: str,
    *,
    add: float = 0.05,
    cap_mult: float,
) -> float:
    """Raise a rank boost when insider cluster momentum is active for symbol."""
    try:
        from modules.insider_monitor import momentum_rank_boost

        if momentum_rank_boost(symbol) > 0:
            return round(min(base_boost + add, base_boost * cap_mult), 4)
    except Exception as exc:
        logger.debug("insider cluster rank bump skipped for %s: %s", symbol, exc)
    return base_boost


def append_tag_if_boost(
    tags: list[str],
    tag: str,
    *,
    enabled: bool,
    boost_fn: Callable[..., float],
    symbol: str,
    data: Any = None,
    with_data: bool = True,
) -> None:
    """Append tag when a scanner boost function returns > 0 (graceful on failure)."""
    if not enabled:
        return
    try:
        val = float(boost_fn(symbol, data) if with_data else boost_fn(symbol))
        if val > 0:
            tags.append(tag)
    except Exception as exc:
        logger.debug("scanner tag check failed for %s/%s: %s", tag, symbol, exc)
