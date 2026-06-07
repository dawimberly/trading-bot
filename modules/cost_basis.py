"""Cost-basis awareness from Alpaca positions (avg_entry_price)."""

from __future__ import annotations

import config

SLEEVE_KEYS = ("spy", "crypto", "nyse", "metal")


def _empty_sleeve() -> dict:
    return {
        "cost": 0.0,
        "value": 0.0,
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
        "underwater": False,
        "positions": 0,
    }


def sleeve_for_symbol(symbol: str) -> str:
    sym = config.normalize_symbol(symbol)
    if config.is_crypto(sym):
        return "crypto"
    if sym == config.SPY_BOT_SYMBOL:
        return "spy"
    if config.is_metal_symbol(sym):
        return "metal"
    return "nyse"


def sleeve_for_position(pos) -> str:
    return sleeve_for_symbol(pos.symbol)


def _position_cost(pos) -> tuple[float, float, float]:
    """Return (cost_basis, market_value, unrealized_pnl) for a long position."""
    qty = abs(float(pos.qty or 0))
    if qty <= 0:
        return 0.0, 0.0, 0.0

    entry = float(pos.avg_entry_price or 0)
    current = float(pos.current_price or 0)
    upl = getattr(pos, "unrealized_pl", None)
    mv = getattr(pos, "market_value", None)

    value = abs(float(mv)) if mv is not None else abs(qty * current)
    if upl is not None:
        unrealized = float(upl)
        cost = value - unrealized
    elif entry > 0:
        cost = qty * entry
        unrealized = value - cost
    else:
        cost = value
        unrealized = 0.0

    return cost, value, unrealized


def compute_sleeve_pnl(executor) -> dict[str, dict]:
    """Aggregate unrealized P&L per sleeve from Alpaca positions."""
    sleeves = {k: _empty_sleeve() for k in SLEEVE_KEYS}
    try:
        positions = executor._get_positions()
    except Exception:
        return sleeves

    for pos in positions:
        sleeve = sleeve_for_position(pos)
        if sleeve not in sleeves:
            continue
        cost, value, unrealized = _position_cost(pos)
        if value <= 0 and cost <= 0:
            continue
        row = sleeves[sleeve]
        row["cost"] += cost
        row["value"] += value
        row["unrealized_pnl"] += unrealized
        row["positions"] += 1

    for row in sleeves.values():
        if row["cost"] > 0:
            row["unrealized_pnl_pct"] = row["unrealized_pnl"] / row["cost"]
        row["underwater"] = row["unrealized_pnl"] < -1e-6
        for key in ("cost", "value", "unrealized_pnl", "unrealized_pnl_pct"):
            row[key] = round(row[key], 4)
    return sleeves


def underwater_sizing_scale(sleeve_key: str, sleeve_pnl: dict[str, dict] | None) -> float:
    """Reduce buy sizing when a sleeve is underwater on cost basis."""
    if not config.COST_BASIS_AWARE_ENABLED or not sleeve_pnl:
        return 1.0
    row = sleeve_pnl.get(sleeve_key) or {}
    if row.get("underwater"):
        return config.UNDERWATER_SIZING_SCALE
    return 1.0


def position_below_cost(executor, symbol: str) -> bool:
    """True when current price is below avg_entry_price."""
    if not config.COST_BASIS_AWARE_ENABLED:
        return False
    pos = executor._find_position(symbol)
    if pos is None:
        return False
    entry = float(pos.avg_entry_price or 0)
    current = float(pos.current_price or 0)
    if entry <= 0 or current <= 0:
        return False
    return current < entry


def format_sleeve_pnl_line(sleeve_pnl: dict[str, dict]) -> str:
    parts = []
    for key in ("spy", "crypto", "nyse"):
        row = sleeve_pnl.get(key) or {}
        if row.get("positions", 0) <= 0:
            continue
        pct = row.get("unrealized_pnl_pct", 0) * 100
        tag = " UW" if row.get("underwater") else ""
        parts.append(f"{key.upper()} {pct:+.1f}%{tag}")
    return " | ".join(parts) if parts else "flat"
