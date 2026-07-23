"""Shared sleeve deployment sizing (adaptive chunks + co-fire budget pooling)."""

import logging

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

SLEEVE_KEYS = ("spy", "crypto", "nyse")


def _broker_cash_pct(equity: float, cash: float) -> float | None:
    if equity <= 0:
        return None
    return round(float(cash) / float(equity), 6)


def crypto_cash_cap(cash: float, *, cash_use: float | None = None) -> float:
    """Cash budget for crypto buys (fee + buffer). Equities use cash * cash_use."""
    use = cash_use if cash_use is not None else 0.95
    buffer = round(cash * use, 2)
    if not config.ALPACA_CRYPTO_FEE_AWARE:
        return buffer
    fee = config.ALPACA_CRYPTO_TAKER_FEE_PCT
    if fee <= 0:
        return buffer
    return round(buffer / (1 + fee), 2)


def apply_alpaca_crypto_fee_reserve(
    notional: float | None,
    *,
    equity: float | None = None,
) -> float | None:
    """Haircut crypto buy notional so order + taker fee fits cash and min order rules."""
    if notional is None:
        return None
    if not config.ALPACA_CRYPTO_FEE_AWARE:
        return notional
    fee = config.ALPACA_CRYPTO_TAKER_FEE_PCT
    if fee <= 0:
        return notional
    adjusted = round(notional / (1 + fee), 2)
    if adjusted < config.effective_min_notional(equity):
        return None
    return adjusted

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


def apply_spy_ladder(
    notional: float | None,
    data: pd.DataFrame | None,
    *,
    equity: float | None = None,
) -> float | None:
    if notional is None:
        return None
    scaled = round(notional * spy_ladder_scale(data), 2)
    if scaled < config.effective_min_notional(equity):
        return None
    return scaled


def nyse_beta_scale(beta: float) -> float:
    """Reduce NYSE order size on high-beta names; no reduction at or below beta 1.0."""
    if not config.NYSE_BETA_SCALING_ENABLED:
        return 1.0
    if not np.isfinite(beta) or beta <= 1.0:
        return 1.0
    return min(1.0, 1.0 / beta)


def high_cash_nyse_top_up_notional(
    equity: float,
    cash: float,
    sleeve_cap_pct: float,
    sleeve_value: float,
    *,
    cash_pct: float | None = None,
) -> float | None:
    """When NYSE room is dust but paper cash is high, allow a small top-up order.

    Uses the expanded high-cash NYSE cap. Returns None if cash/cap still cannot
    support Alpaca's minimum order.
    """
    broker_pct = cash_pct if cash_pct is not None else _broker_cash_pct(equity, cash)
    if not config.paper_deploy_aggressive(broker_pct, equity=equity, cash=cash):
        return None
    expanded = config.effective_nyse_sleeve_cap_pct(
        broker_pct,
        equity=equity,
        cash=cash,
        base_pct=sleeve_cap_pct,
    )
    cap = round(equity * expanded, 2)
    room = round(cap - sleeve_value, 2)
    order_min = config.ALPACA_MIN_NOTIONAL
    cash_cap = round(cash * config.PAPER_AGGRESSIVE_CASH_USE_PCT, 2)
    if room < order_min and cash_cap >= order_min:
        # Cap fully used under expanded pct — still allow a dust top-up from cash.
        top = min(order_min * 2.0, cash_cap, config.effective_max_notional_per_order(equity))
        if top >= order_min:
            if config.PAPER_DEPLOY_DEBUG:
                logger.info(
                    "NYSE cap expansion due to high cash (top-up) cap_pct=%.1f%% "
                    "room=%.2f -> top_up=%.2f",
                    expanded * 100.0,
                    room,
                    top,
                )
            return round(top, 2)
        return None
    if room < order_min:
        return None
    top = min(
        room,
        max(order_min, round(equity * config.effective_risk_per_trade(equity) * 0.5, 2)),
        cash_cap,
        config.effective_max_notional_per_order(equity),
    )
    if top < order_min:
        return None
    if config.PAPER_DEPLOY_DEBUG:
        logger.info(
            "NYSE cap expansion due to high cash (top-up) cap_pct=%.1f%% room=%.2f top_up=%.2f",
            expanded * 100.0,
            room,
            top,
        )
    return round(top, 2)


def per_trade_chunk(
    equity: float,
    room: float,
    *,
    cash_pct: float | None = None,
    equity_cash: float | None = None,
    cash: float | None = None,
) -> float:
    """Base 2% chunk; larger when sleeve has headroom (>5× per_trade)."""
    per_trade = round(equity * config.effective_risk_per_trade(equity), 2)
    aggressive = config.paper_deploy_aggressive(
        cash_pct, equity=equity_cash or equity, cash=cash
    )
    if aggressive:
        per_trade = round(per_trade * 1.50, 2)
    if (
        config.effective_adaptive_chunk_enabled()
        and room > 5 * per_trade
    ):
        chunk = round(equity * config.ADAPTIVE_CHUNK_MAX_PCT, 2)
        if aggressive:
            chunk = round(chunk * 1.35, 2)
        return chunk
    return per_trade


def compute_sleeve_notional(
    equity: float,
    cash: float,
    sleeve_cap_pct: float,
    sleeve_value: float,
    *,
    chunk: float | None = None,
    cash_cap: float | None = None,
    cash_pct: float | None = None,
) -> float | None:
    min_n = config.effective_min_notional(equity)
    broker_pct = cash_pct if cash_pct is not None else _broker_cash_pct(equity, cash)
    aggressive = config.paper_deploy_aggressive(
        broker_pct, equity=equity, cash=cash
    )
    room_min = config.effective_no_room_min_notional(
        equity, cash_pct=broker_pct, cash=cash
    )
    order_min = config.ALPACA_MIN_NOTIONAL if aggressive else min_n
    max_n = config.effective_max_notional_per_order(equity)
    cap = round(equity * sleeve_cap_pct, 2)
    room = round(cap - sleeve_value, 2)
    if room < room_min:
        return None
    trade_chunk = chunk if chunk is not None else per_trade_chunk(
        equity, room, cash_pct=broker_pct, equity_cash=equity, cash=cash
    )
    if aggressive:
        trade_chunk = round(max(trade_chunk, order_min), 2)
    raw = min(
        room,
        trade_chunk,
        max_n,
        cash_cap if cash_cap is not None else round(cash * 0.95, 2),
    )
    if raw < order_min:
        return None
    return round(raw, 2)


def _paper_scrape_room_skip(
    room: float, equity: float, *, cash_pct: float | None = None
) -> bool:
    """Paper aggressive: skip orders that only top up sleeve dust (room << normal chunk)."""
    if not config.paper_aggressive_context():
        return False
    if config.paper_deploy_aggressive(cash_pct):
        return False
    min_n = config.effective_min_notional(equity)
    chunk = round(equity * config.effective_risk_per_trade(equity), 2)
    if config.effective_adaptive_chunk_enabled() and room > 5 * chunk:
        chunk = round(equity * config.ADAPTIVE_CHUNK_MAX_PCT, 2)
    dust_frac = config.effective_dust_skip_chunk_frac()
    threshold = max(min_n * 2, chunk * dust_frac)
    return room >= min_n and room < threshold


def resolve_sleeve_notional(
    equity: float,
    cash: float,
    sleeve_cap_pct: float,
    sleeve_value: float,
    sleeve_key: str,
    cofire_notionals: dict[str, float] | None,
    *,
    regime: str | None = None,
) -> float | None:
    """Per-sleeve notional: co-fire override when set, else adaptive chunk rules."""
    notional, _ = resolve_sleeve_notional_detail(
        equity,
        cash,
        sleeve_cap_pct,
        sleeve_value,
        sleeve_key,
        cofire_notionals,
        regime=regime,
    )
    return notional


def resolve_sleeve_notional_detail(
    equity: float,
    cash: float,
    sleeve_cap_pct: float,
    sleeve_value: float,
    sleeve_key: str,
    cofire_notionals: dict[str, float] | None,
    *,
    regime: str | None = None,
) -> tuple[float | None, str | None]:
    """Like resolve_sleeve_notional but returns (notional, skip_reason)."""
    from modules.paper_risk_controls import effective_sleeve_cap_pct

    broker_pct = _broker_cash_pct(equity, cash)
    sleeve_cap_pct = effective_sleeve_cap_pct(
        sleeve_key,
        sleeve_cap_pct,
        regime=regime,
        cash_pct=broker_pct,
        equity=equity,
        cash=cash,
    )
    min_n = config.effective_min_notional(equity)
    room_min = config.effective_no_room_min_notional(
        equity, cash_pct=broker_pct, cash=cash
    )
    max_n = config.effective_max_notional_per_order(equity)
    cap = round(equity * sleeve_cap_pct, 2)
    room = round(cap - sleeve_value, 2)
    if room < room_min:
        return None, f"no_room cap={cap:.0f} sleeve={sleeve_value:.0f} room={room:.2f} min={room_min:.2f}"

    if _paper_scrape_room_skip(room, equity, cash_pct=broker_pct):
        return None, f"dust_skip room={room:.2f}"

    cash_use = 0.95
    aggressive = False
    if config.paper_aggressive_context():
        threshold = config.effective_excess_cash_threshold_pct()
        if broker_pct is not None and broker_pct > threshold:
            cash_use = min(0.99, 0.95 + (broker_pct - threshold))
        aggressive = config.paper_deploy_aggressive(
            broker_pct, equity=equity, cash=cash
        )
        if aggressive:
            cash_use = config.PAPER_AGGRESSIVE_CASH_USE_PCT
            if config.PAPER_DEPLOY_DEBUG:
                logger.info(
                    "aggressive deploy mode activated sleeve=%s equity=%.0f cash=%.0f "
                    "cash_pct=%.1f%% cash_use=%.0f%% boost=%.2fx",
                    sleeve_key,
                    equity,
                    cash,
                    (broker_pct or 0.0) * 100.0,
                    cash_use * 100.0,
                    config.effective_excess_cash_sleeve_mult(
                        broker_pct, equity=equity, cash=cash
                    ),
                )
    cash_cap = (
        crypto_cash_cap(cash, cash_use=cash_use)
        if sleeve_key == "crypto"
        else round(cash * cash_use, 2)
    )

    cofire = cofire_notionals or {}
    if sleeve_key in cofire:
        raw = min(cofire[sleeve_key], room, max_n, cash_cap)
        if raw < min_n:
            return None, f"min_notional cofire raw={raw:.2f} min={min_n:.2f}"
        out = round(raw, 2)
        if sleeve_key == "crypto":
            out = apply_alpaca_crypto_fee_reserve(out, equity=equity)
            if out is None:
                return None, "crypto_fee_reserve"
            return out, None
        return out, None

    out = compute_sleeve_notional(
        equity,
        cash,
        sleeve_cap_pct,
        sleeve_value,
        cash_cap=cash_cap,
        cash_pct=broker_pct,
    )
    if out is None:
        return None, f"chunk_cap room={room:.2f} cash_cap={cash_cap:.2f} min={min_n:.2f}"
    if sleeve_key == "crypto":
        out = apply_alpaca_crypto_fee_reserve(out, equity=equity)
        if out is None:
            return None, "crypto_fee_reserve"
    return out, None


def compute_cofire_allocations(
    equity: float,
    cash: float,
    sleeve_rooms: dict[str, float],
) -> dict[str, float]:
    """Split COFIRE_BUDGET_PCT across sleeves proportional to remaining room."""
    broker_pct = _broker_cash_pct(equity, cash)
    min_n = config.effective_min_notional(equity)
    room_min = config.effective_no_room_min_notional(
        equity, cash_pct=broker_pct, cash=cash
    )
    max_n = config.effective_max_notional_per_order(equity)
    active = {
        k: round(v, 2)
        for k, v in sleeve_rooms.items()
        if v >= room_min
    }
    if not config.effective_cofire_budget_enabled() or len(active) < 2:
        return {}

    pool = round(equity * config.COFIRE_BUDGET_PCT, 2)
    cash_use = (
        config.PAPER_AGGRESSIVE_CASH_USE_PCT
        if config.paper_deploy_aggressive(broker_pct, equity=equity, cash=cash)
        else 0.95
    )
    room_min = config.effective_no_room_min_notional(
        equity, cash_pct=broker_pct, cash=cash
    )
    pool = min(pool, round(cash * cash_use, 2))
    total_room = sum(active.values())
    if total_room <= 0 or pool < min_n:
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
        share = min(share, room, max_n)
        if share >= min_n:
            out[name] = share
    return out
