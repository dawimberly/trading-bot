"""Shared sleeve deployment sizing (adaptive chunks + co-fire budget pooling)."""

import numpy as np
import pandas as pd

import config

SLEEVE_KEYS = ("spy", "crypto", "nyse")

# MA200 → 10% chunk; +MA100 → 25%; +MA50 → 50%; +MA20 → 100%
_SPY_LADDER_TIERS = tuple(
    zip(reversed(config.SPY_MA_WINDOWS), reversed(config.SPY_ALLOCATIONS))
)


def spy_ladder_scale(data: pd.DataFrame | None) -> float:
    """Scale SPY order size by how many ladder MAs price clears (min 10%, max 100%)."""
    if not config.SPY_LADDER_SIZING_ENABLED or data is None:
        return 1.0
    symbol = config.SPY_BOT_SYMBOL
    if symbol not in data.columns:
        return 1.0
    prices = data[symbol].dropna()
    if len(prices) < 20:
        return config.SPY_ALLOCATIONS[0]
    current = float(prices.iloc[-1])
    if not np.isfinite(current) or current <= 0:
        return config.SPY_ALLOCATIONS[0]

    scale = config.SPY_ALLOCATIONS[0]
    for window, alloc in _SPY_LADDER_TIERS:
        ma = prices.rolling(window=min(window, len(prices))).mean().iloc[-1]
        if np.isfinite(ma) and ma > 0 and current > ma:
            scale = alloc
    return float(scale)


def apply_spy_ladder(notional: float | None, data: pd.DataFrame | None) -> float | None:
    if notional is None:
        return None
    scaled = round(notional * spy_ladder_scale(data), 2)
    if scaled < config.MIN_NOTIONAL:
        return None
    return scaled


def nyse_beta_scale(beta: float) -> float:
    """Reduce NYSE order size on high-beta names; no reduction at or below beta 1.0."""
    if not config.NYSE_BETA_SCALING_ENABLED:
        return 1.0
    if not np.isfinite(beta) or beta <= 1.0:
        return 1.0
    return min(1.0, 1.0 / beta)


def per_trade_chunk(equity: float, room: float) -> float:
    """Base 2% chunk; larger when sleeve has headroom (>5× per_trade)."""
    per_trade = round(equity * config.RISK_PER_TRADE, 2)
    if (
        config.ADAPTIVE_CHUNK_ENABLED
        and room > 5 * per_trade
    ):
        return round(equity * config.ADAPTIVE_CHUNK_MAX_PCT, 2)
    return per_trade


def compute_sleeve_notional(
    equity: float,
    cash: float,
    sleeve_cap_pct: float,
    sleeve_value: float,
    *,
    chunk: float | None = None,
) -> float | None:
    cap = round(equity * sleeve_cap_pct, 2)
    room = round(cap - sleeve_value, 2)
    if room < config.MIN_NOTIONAL:
        return None
    trade_chunk = chunk if chunk is not None else per_trade_chunk(equity, room)
    raw = min(
        room,
        trade_chunk,
        config.MAX_NOTIONAL_PER_ORDER,
        round(cash * 0.95, 2),
    )
    if raw < config.MIN_NOTIONAL:
        return None
    return round(raw, 2)


def compute_cofire_allocations(
    equity: float,
    cash: float,
    sleeve_rooms: dict[str, float],
) -> dict[str, float]:
    """Split COFIRE_BUDGET_PCT across sleeves proportional to remaining room."""
    active = {
        k: round(v, 2)
        for k, v in sleeve_rooms.items()
        if v >= config.MIN_NOTIONAL
    }
    if not config.COFIRE_BUDGET_ENABLED or len(active) < 2:
        return {}

    pool = round(equity * config.COFIRE_BUDGET_PCT, 2)
    pool = min(pool, round(cash * 0.95, 2))
    total_room = sum(active.values())
    if total_room <= 0 or pool < config.MIN_NOTIONAL:
        return {}

    out: dict[str, float] = {}
    remaining = pool
    items = sorted(active.items(), key=lambda x: -x[1])
    for i, (name, room) in enumerate(items):
        if i == len(items) - 1:
            share = round(remaining, 2)
        else:
            share = round(pool * room / total_room, 2)
            remaining = round(remaining - share, 2)
        share = min(share, room, config.MAX_NOTIONAL_PER_ORDER)
        if share >= config.MIN_NOTIONAL:
            out[name] = share
    return out


def resolve_sleeve_notional(
    equity: float,
    cash: float,
    sleeve_cap_pct: float,
    sleeve_value: float,
    sleeve_key: str,
    cofire_notionals: dict[str, float] | None,
) -> float | None:
    """Per-sleeve notional: co-fire override when set, else adaptive chunk rules."""
    cap = round(equity * sleeve_cap_pct, 2)
    room = round(cap - sleeve_value, 2)
    if room < config.MIN_NOTIONAL:
        return None

    cofire = cofire_notionals or {}
    if sleeve_key in cofire:
        raw = min(
            cofire[sleeve_key],
            room,
            config.MAX_NOTIONAL_PER_ORDER,
            round(cash * 0.95, 2),
        )
        if raw < config.MIN_NOTIONAL:
            return None
        return round(raw, 2)

    return compute_sleeve_notional(equity, cash, sleeve_cap_pct, sleeve_value)
