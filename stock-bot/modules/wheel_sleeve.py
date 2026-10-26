"""Wheel strategy sleeve — cash-secured puts → assignment → covered calls (paper only).

Simulates the classic wheel on liquid underlyings (default SPY/QQQ):
1. Sell OTM cash-secured puts when calm.
2. On put assignment (spot <= strike at expiry), hold shares.
3. Sell OTM covered calls on assigned shares until called away or rolled.

Premium is estimated from VIX (same family as options_sleeve). No live options
broker orders — paper / research book only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

import config
from modules.safe_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_STATE_PATH = ROOT / "data" / "wheel_sleeve_state.json"
_SLEEVE = "WHEEL"


def _state_path() -> Path:
    raw = getattr(config, "WHEEL_STATE_FILE", None)
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else ROOT / p
    return _STATE_PATH


def _load_state() -> dict[str, Any]:
    raw = read_json_file(_state_path()) or {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(path, state)


def underlyings() -> list[str]:
    raw = getattr(config, "WHEEL_UNDERLYINGS", "SPY,QQQ") or "SPY,QQQ"
    return [config.normalize_symbol(s) for s in str(raw).split(",") if str(s).strip()]


def _cap_pct() -> float:
    return max(0.02, min(0.20, float(getattr(config, "WHEEL_SLEEVE_CAP_PCT", 0.08))))


def _otm_pct() -> float:
    return max(0.03, min(0.12, float(getattr(config, "WHEEL_OTM_PCT", 0.05))))


def _manage_bars() -> int:
    return max(1, int(getattr(config, "WHEEL_MANAGE_BARS", 5)))


def _dte_bars() -> int:
    return max(5, int(getattr(config, "WHEEL_DTE_BARS", 21)))


def _vix_calm_max() -> float:
    return float(getattr(config, "WHEEL_VIX_CALM_MAX", 22.0))


def put_premium_rate(vix: float, otm_pct: float | None = None) -> float:
    """Estimated put premium as fraction of cash-secured notional."""
    otm = float(otm_pct if otm_pct is not None else _otm_pct())
    vix_factor = max(0.55, min(1.45, float(vix) / 18.0))
    otm_factor = max(0.60, 1.0 - otm * 2.2)
    return round(0.011 * vix_factor * otm_factor, 6)


def call_premium_rate(vix: float, otm_pct: float | None = None) -> float:
    from modules.options_sleeve import monthly_premium_rate

    return monthly_premium_rate(float(vix), otm_pct if otm_pct is not None else _otm_pct())


def put_strike(spot: float, otm_pct: float | None = None) -> float:
    otm = float(otm_pct if otm_pct is not None else _otm_pct())
    return round(float(spot) * (1.0 - otm), 2)


def call_strike(spot: float, otm_pct: float | None = None) -> float:
    otm = float(otm_pct if otm_pct is not None else _otm_pct())
    return round(float(spot) * (1.0 + otm), 2)


def _is_calm(vix: float | None, volatility: str | None) -> bool:
    if vix is not None and np.isfinite(vix):
        return float(vix) <= _vix_calm_max()
    if volatility and str(volatility).lower() == "high":
        return False
    return True


def _resolve_vix(
    *,
    ts=None,
    volatility: str | None = None,
    vol_score: float | None = None,
    vix: float | None = None,
) -> float:
    from modules.options_sleeve import resolve_vix

    return resolve_vix(ts=ts, volatility=volatility, vol_score=vol_score, vix=vix)


def _holding_value(portfolio, prices, symbol: str) -> float:
    if symbol not in getattr(prices, "index", []):
        return 0.0
    price = prices.get(symbol)
    if price is None or not np.isfinite(price) or float(price) <= 0:
        return 0.0
    qty = portfolio.positions.get(symbol, 0)
    if qty <= 0:
        return 0.0
    return round(float(qty) * float(price), 2)


def _expire_puts(
    portfolio, prices, puts: list[dict], bar_i: int
) -> tuple[list[dict], list[dict], float]:
    """Expire CSPs. Assigned puts become share lots; expired OTM frees cash."""
    remaining: list[dict] = []
    assigned: list[dict] = []
    premium_kept = 0.0
    for p in puts:
        if int(p.get("expiry_i", -1)) > bar_i:
            remaining.append(p)
            continue
        sym = str(p.get("symbol") or "")
        strike = float(p.get("strike") or 0)
        notional = float(p.get("secured_notional") or 0)
        if not sym or strike <= 0 or notional <= 0 or sym not in prices.index:
            continue
        spot = float(prices[sym])
        # Cash was reserved in secured_cash; release / convert on expiry
        reserved = float(p.get("secured_notional") or 0)
        if spot <= strike:
            # Assignment: buy shares at strike using reserved cash
            qty = reserved / strike if strike > 0 else 0.0
            if qty > 0:
                portfolio.positions[sym] = float(portfolio.positions.get(sym, 0) or 0) + qty
                assigned.append(
                    {
                        "symbol": sym,
                        "qty": qty,
                        "cost_basis": strike,
                        "assigned_i": bar_i,
                        "call": None,
                    }
                )
            # reserved cash already held aside — consume it (already not in free cash)
        else:
            # OTM expire: return reserved cash to free cash
            portfolio.cash = round(float(portfolio.cash) + reserved, 2)
            premium_kept += float(p.get("premium") or 0)
    return remaining, assigned, premium_kept


def _expire_calls(
    portfolio, prices, shares: list[dict], bar_i: int
) -> tuple[list[dict], float]:
    """Expire covered calls on wheel share lots."""
    remaining: list[dict] = []
    drag = 0.0
    for lot in shares:
        call = lot.get("call")
        if not call:
            remaining.append(lot)
            continue
        if int(call.get("expiry_i", -1)) > bar_i:
            remaining.append(lot)
            continue
        sym = str(lot.get("symbol") or "")
        strike = float(call.get("strike") or 0)
        qty = float(lot.get("qty") or 0)
        if not sym or qty <= 0 or strike <= 0 or sym not in prices.index:
            continue
        spot = float(prices[sym])
        held = float(portfolio.positions.get(sym, 0) or 0)
        sell_qty = min(held, qty)
        if spot >= strike and sell_qty > 0:
            # Called away at strike
            portfolio.cash = round(float(portfolio.cash) + sell_qty * strike, 2)
            portfolio.positions[sym] = held - sell_qty
            if portfolio.positions[sym] < 1e-9:
                del portfolio.positions[sym]
            drag += round(sell_qty * max(0.0, spot - strike), 2)
            # lot closed — do not keep
        else:
            # Call expires OTM — keep shares, clear call
            lot["call"] = None
            remaining.append(lot)
    return remaining, drag


def run_wheel_backtest_day(
    portfolio,
    prices,
    *,
    bar_i: int,
    state: dict,
    volatility: str | None = None,
    vol_score: float | None = None,
    vix: float | None = None,
    ts=None,
    market_open: bool = True,
) -> tuple[list[dict], dict]:
    """Daily wheel management for paper backtests."""
    meta = {
        "active": False,
        "premium": 0.0,
        "assignment_drag": 0.0,
        "puts_sold": 0,
        "calls_sold": 0,
        "calm": False,
    }
    actions: list[dict] = []
    if not config.effective_wheel_sleeve_enabled() or not market_open:
        return actions, meta

    vix_level = _resolve_vix(ts=ts, volatility=volatility, vol_score=vol_score, vix=vix)
    calm = _is_calm(vix_level, volatility)
    meta["calm"] = calm
    meta["vix"] = round(vix_level, 2)

    puts = list(state.get("puts") or [])
    shares = list(state.get("shares") or [])

    puts, newly_assigned, _ = _expire_puts(portfolio, prices, puts, bar_i)
    if newly_assigned:
        shares.extend(newly_assigned)
        for a in newly_assigned:
            actions.append({"action": "put_assigned", "symbol": a["symbol"], "qty": a["qty"]})

    shares, drag = _expire_calls(portfolio, prices, shares, bar_i)
    if drag > 0:
        meta["assignment_drag"] = drag
        state["assignment_drag"] = round(float(state.get("assignment_drag", 0)) + drag, 2)

    last_manage = int(state.get("last_manage_i", -999))
    if bar_i - last_manage < _manage_bars():
        state["puts"] = puts
        state["shares"] = shares
        return actions, meta

    eq = float(portfolio.equity(prices))
    cap = round(eq * _cap_pct(), 2)
    secured = sum(float(p.get("secured_notional") or 0) for p in puts)
    share_val = sum(_holding_value(portfolio, prices, str(s.get("symbol"))) for s in shares)
    used = secured + share_val
    room = max(0.0, cap - used)

    premium_total = 0.0

    # Sell covered calls on naked (no call) assigned lots
    if calm:
        for lot in shares:
            if lot.get("call"):
                continue
            sym = str(lot.get("symbol") or "")
            if sym not in prices.index:
                continue
            spot = float(prices[sym])
            qty = float(lot.get("qty") or 0)
            if spot <= 0 or qty <= 0:
                continue
            notional = round(qty * spot, 2)
            rate = call_premium_rate(vix_level)
            prem = round(notional * rate, 2)
            if prem < 0.5:
                continue
            portfolio.cash = round(float(portfolio.cash) + prem, 2)
            premium_total += prem
            stk = call_strike(spot)
            lot["call"] = {
                "strike": stk,
                "premium": prem,
                "expiry_i": bar_i + _dte_bars(),
                "notional": notional,
            }
            meta["calls_sold"] += 1
            actions.append(
                {"action": "sell_call", "symbol": sym, "strike": stk, "premium": prem}
            )

    # Sell new CSPs if room and calm
    if calm and room >= max(50.0, float(getattr(config, "WHEEL_MIN_NOTIONAL", 100.0))):
        per = round(room / max(1, len(underlyings())), 2)
        for sym in underlyings():
            if sym not in prices.index or per < 50:
                continue
            # Skip if already have open put or shares on this name
            if any(str(p.get("symbol")) == sym for p in puts):
                continue
            if any(str(s.get("symbol")) == sym for s in shares):
                continue
            spot = float(prices[sym])
            if spot <= 0:
                continue
            stk = put_strike(spot)
            notional = min(per, round(spot * max(1.0, 100 * (per / (spot * 100))), 2))
            notional = round(min(notional, room), 2)
            if notional < 50 or float(portfolio.cash) < notional:
                continue
            rate = put_premium_rate(vix_level)
            prem = round(notional * rate, 2)
            if prem < 0.5:
                continue
            # Reserve cash for CSP collateral
            portfolio.cash = round(float(portfolio.cash) - notional + prem, 2)
            premium_total += prem
            room = max(0.0, room - notional)
            puts.append(
                {
                    "symbol": sym,
                    "strike": stk,
                    "secured_notional": notional,
                    "premium": prem,
                    "expiry_i": bar_i + _dte_bars(),
                }
            )
            meta["puts_sold"] += 1
            actions.append(
                {"action": "sell_put", "symbol": sym, "strike": stk, "premium": prem}
            )

    if premium_total > 0 or meta["puts_sold"] or meta["calls_sold"]:
        state["last_manage_i"] = bar_i
        state["total_premium"] = round(
            float(state.get("total_premium", 0)) + premium_total, 2
        )
        state["manage_cycles"] = int(state.get("manage_cycles", 0)) + 1
        meta["active"] = True
        meta["premium"] = premium_total

    state["puts"] = puts
    state["shares"] = shares
    return actions, meta


def run_wheel_sleeve_cycle(
    executor,
    *,
    volatility: str | None = None,
    vix: float | None = None,
    market_open: bool = True,
) -> dict:
    """Live/paper cycle — logs intent; does not place broker options orders."""
    result: dict[str, Any] = {"enabled": False, "actions": [], "premium": 0.0}
    if not config.effective_wheel_sleeve_enabled() or not market_open:
        return result
    result["enabled"] = True
    vix_level = _resolve_vix(volatility=volatility, vix=vix)
    if not _is_calm(vix_level, volatility):
        result["skipped"] = True
        result["reason"] = f"not calm (vix={vix_level:.1f})"
        return result

    state = _load_state()
    last = state.get("last_manage_utc")
    if last:
        try:
            prev = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - prev).days
            if days < max(1, _manage_bars() // 5):
                result["skipped"] = True
                result["reason"] = "within manage cadence"
                return result
        except (TypeError, ValueError):
            pass

    try:
        acct = executor._get_account()
        equity = float(acct.equity)
    except Exception:
        equity = 0.0
    cap = round(equity * _cap_pct(), 2)
    actions = []
    for sym in underlyings():
        actions.append(
            {
                "action": "plan_csp",
                "symbol": sym,
                "cap_slice": round(cap / max(1, len(underlyings())), 2),
                "otm_pct": _otm_pct(),
                "vix": round(vix_level, 2),
            }
        )
    state["last_manage_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_plan"] = actions
    state["vix"] = round(vix_level, 2)
    _save_state(state)
    result["actions"] = actions
    result["cap"] = cap
    result["vix"] = round(vix_level, 2)
    logger.info(
        "[WHEEL] manage cycle: cap=$%.0f vix=%.1f underlyings=%s",
        cap,
        vix_level,
        ",".join(underlyings()),
    )
    print(
        f"--- Wheel sleeve: plan CSPs on {', '.join(underlyings())} "
        f"(cap ${cap:,.0f}, VIX {vix_level:.1f}) ---"
    )
    return result


def format_wheel_banner() -> str | None:
    if not config.effective_wheel_sleeve_enabled():
        return ">>> Wheel Strategy: OFF (paper opt-in)"
    return (
        f">>> Wheel Strategy: ON (CSP→CC on {', '.join(underlyings())}, "
        f"cap {_cap_pct():.0%}, OTM {_otm_pct():.0%}, manage every {_manage_bars()} bars, "
        f"paper-only) <<<"
    )


def format_weekly_wheel_note() -> str:
    if not config.effective_wheel_sleeve_enabled():
        return ""
    state = _load_state()
    prem = float(state.get("total_premium", 0) or 0)
    drag = float(state.get("assignment_drag", 0) or 0)
    puts = len(state.get("puts") or state.get("last_plan") or [])
    return (
        f"Wheel: ON | underlyings {', '.join(underlyings())} | "
        f"cap {_cap_pct():.0%} | premium ${prem:.0f} | drag ${drag:.0f} | "
        f"open plans/puts {puts}"
    )


def format_telegram_weekly_wheel_block() -> str:
    note = format_weekly_wheel_note()
    if not note:
        return ""
    return f"\n\n{note}"
