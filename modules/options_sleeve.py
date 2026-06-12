"""Options income sleeve — monthly covered calls on VTI/SPY (paper aggressive only)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config

VTI_SYMBOL = config.VTI_CORE_SYMBOL
SPY_SYMBOL = config.SPY_BOT_SYMBOL
UNDERLYINGS = (VTI_SYMBOL, SPY_SYMBOL)
STATE_FILE = Path("options_sleeve_state.json")


def is_calm_regime(*, volatility: str | None, vix: float | None) -> bool:
    """Calm = low VIX when available; else fall back to cross-asset vol Low."""
    if vix is not None and np.isfinite(vix):
        return float(vix) <= config.OPTIONS_VIX_CALM_MAX
    if volatility and str(volatility).lower() == "high":
        return False
    return True


def _load_vix_daily() -> pd.Series:
    from modules.data_loader import safe_sql_table

    table = safe_sql_table("VIX_daily")
    try:
        conn = sqlite3.connect(config.DB_PATH)
        df = pd.read_sql(f'SELECT * FROM "{table}"', conn)
        conn.close()
    except Exception:
        return pd.Series(dtype=float)
    if df.empty:
        return pd.Series(dtype=float)
    target = next((c for c in df.columns if "close" in c.lower()), None)
    date_col = "Date" if "Date" in df.columns else None
    if target is None or date_col is None:
        return pd.Series(dtype=float)
    s = pd.to_numeric(df.set_index(date_col)[target], errors="coerce")
    s.index = pd.to_datetime(s.index, errors="coerce")
    return s.sort_index().dropna()


def ensure_vix_daily() -> None:
    if len(_load_vix_daily()) >= 30:
        return
    try:
        import yfinance as yf

        raw = yf.download("^VIX", period="2y", interval="1d", progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return
        df = raw.reset_index()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        rename = {c: "Close" for c in df.columns if str(c).lower() == "close"}
        df = df.rename(columns=rename)
        if "Date" not in df.columns:
            date_col = "index" if "index" in df.columns else df.columns[0]
            df = df.rename(columns={date_col: "Date"})
        if "Date" not in df.columns or "Close" not in df.columns:
            return
        conn = sqlite3.connect(config.DB_PATH)
        df[["Date", "Close"]].to_sql("VIX_daily", conn, if_exists="replace", index=False)
        conn.close()
    except Exception:
        pass


def current_vix_level() -> float | None:
    ensure_vix_daily()
    s = _load_vix_daily()
    if s.empty:
        return None
    val = float(s.iloc[-1])
    return val if np.isfinite(val) else None


def vix_as_of(ts) -> float | None:
    ensure_vix_daily()
    s = _load_vix_daily()
    if s.empty:
        return None
    ts = pd.Timestamp(ts)
    sub = s.loc[:ts]
    if sub.empty:
        return None
    val = float(sub.iloc[-1])
    return val if np.isfinite(val) else None


def estimate_vix_from_vol(volatility: str | None, vol_score: float | None = None) -> float:
    if volatility and str(volatility).lower() == "high":
        return 26.0
    if vol_score is not None and vol_score > 0.02:
        return 24.0
    return 17.0


def resolve_vix(
    *,
    ts=None,
    volatility: str | None = None,
    vol_score: float | None = None,
    vix: float | None = None,
) -> float:
    if vix is not None and np.isfinite(vix):
        return float(vix)
    if ts is not None:
        level = vix_as_of(ts)
        if level is not None:
            return level
    return estimate_vix_from_vol(volatility, vol_score)


def monthly_premium_rate(vix: float, otm_pct: float | None = None) -> float:
    """Estimated monthly premium as fraction of covered notional."""
    otm = float(otm_pct if otm_pct is not None else config.OPTIONS_OTM_PCT)
    otm = max(config.OPTIONS_OTM_PCT_MIN, min(config.OPTIONS_OTM_PCT_MAX, otm))
    vix_factor = max(0.55, min(1.35, vix / 18.0))
    otm_factor = max(0.65, 1.0 - otm * 2.5)
    return round(0.009 * vix_factor * otm_factor, 6)


def strike_price(spot: float, otm_pct: float | None = None) -> float:
    otm = float(otm_pct if otm_pct is not None else config.OPTIONS_OTM_PCT)
    otm = max(config.OPTIONS_OTM_PCT_MIN, min(config.OPTIONS_OTM_PCT_MAX, otm))
    return round(spot * (1.0 + otm), 2)


def _holding_value(portfolio, prices, symbol: str) -> float:
    if symbol not in prices.index:
        return 0.0
    price = prices.get(symbol)
    if price is None or not np.isfinite(price) or float(price) <= 0:
        return 0.0
    qty = portfolio.positions.get(symbol, 0)
    if qty <= 0:
        return 0.0
    return round(float(qty) * float(price), 2)


def _executor_holding_value(executor, symbol: str) -> float:
    pos = executor._find_position(symbol)
    if pos is None:
        return 0.0
    return round(float(executor._position_market_value(pos)), 2)


def allocate_coverage(
    equity: float,
    holdings: dict[str, float],
) -> dict[str, float]:
    """Split options cap across VTI (primary) and SPY."""
    cap = round(equity * config.OPTIONS_SLEEVE_CAP_PCT, 2)
    if cap <= 0:
        return {}
    vti_hold = holdings.get(VTI_SYMBOL, 0.0)
    spy_hold = holdings.get(SPY_SYMBOL, 0.0)
    if vti_hold <= 0 and spy_hold <= 0:
        return {}

    vti_share = float(config.OPTIONS_VTI_ALLOC_PCT)
    out: dict[str, float] = {}
    vti_cap = round(cap * vti_share, 2)
    spy_cap = round(cap - vti_cap, 2)
    if vti_hold > 0 and vti_cap > 0:
        out[VTI_SYMBOL] = round(min(vti_hold, vti_cap), 2)
    if spy_hold > 0 and spy_cap > 0:
        out[SPY_SYMBOL] = round(min(spy_hold, spy_cap), 2)
    return out


def _load_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def _should_roll_live(state: dict) -> bool:
    last = state.get("last_roll_utc")
    if not last:
        return True
    try:
        prev = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - prev).days >= 28
    except (TypeError, ValueError):
        return True


def _expire_contracts(portfolio, prices, contracts: list, bar_i: int) -> tuple[list, float]:
    """Expire open calls; return remaining contracts and assignment drag (cash debit)."""
    drag = 0.0
    remaining = []
    for c in contracts:
        if int(c.get("expiry_i", -1)) > bar_i:
            remaining.append(c)
            continue
        sym = c.get("symbol")
        strike = float(c.get("strike", 0))
        covered = float(c.get("covered_notional", 0))
        if sym not in prices.index or covered <= 0 or strike <= 0:
            continue
        spot = float(prices[sym])
        if spot > strike:
            loss = round(covered * (spot - strike) / spot, 2)
            drag += loss
            qty = portfolio.positions.get(sym, 0)
            if qty > 0 and spot > 0:
                sell_qty = min(qty, covered / spot)
                portfolio.cash += sell_qty * strike
                portfolio.positions[sym] = qty - sell_qty
                if portfolio.positions[sym] < 1e-9:
                    del portfolio.positions[sym]
    return remaining, drag


def run_options_backtest_day(
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
    """Simulate monthly covered calls on VTI/SPY holdings."""
    meta = {
        "active": False,
        "premium": 0.0,
        "assignment_drag": 0.0,
        "rolls": 0,
        "calm": False,
    }
    actions: list[dict] = []
    if not config.effective_options_sleeve_enabled() or not market_open:
        return actions, meta

    vix_level = resolve_vix(ts=ts, volatility=volatility, vol_score=vol_score, vix=vix)
    calm = is_calm_regime(volatility=volatility, vix=vix_level)
    meta["calm"] = calm
    meta["vix"] = round(vix_level, 2)

    contracts = list(state.get("contracts") or [])
    contracts, drag = _expire_contracts(portfolio, prices, contracts, bar_i)
    if drag > 0:
        portfolio.cash = max(0.0, portfolio.cash - drag)
        state["assignment_drag"] = round(float(state.get("assignment_drag", 0)) + drag, 2)
        meta["assignment_drag"] = drag

    last_roll = int(state.get("last_roll_i", -999))
    if bar_i - last_roll < config.OPTIONS_MONTHLY_BARS:
        state["contracts"] = contracts
        return actions, meta

    if not calm:
        state["contracts"] = contracts
        return actions, meta

    eq = portfolio.equity(prices)
    holdings = {sym: _holding_value(portfolio, prices, sym) for sym in UNDERLYINGS}
    alloc = allocate_coverage(eq, holdings)
    if not alloc:
        state["contracts"] = contracts
        return actions, meta

    premium_total = 0.0
    rate = monthly_premium_rate(vix_level)
    new_contracts = []
    for sym, notional in alloc.items():
        spot = float(prices[sym])
        prem = round(notional * rate, 2)
        if prem < 1.0:
            continue
        portfolio.cash += prem
        premium_total += prem
        stk = strike_price(spot)
        new_contracts.append(
            {
                "symbol": sym,
                "strike": stk,
                "covered_notional": notional,
                "premium": prem,
                "expiry_i": bar_i + config.OPTIONS_MONTHLY_BARS,
            }
        )
        actions.append(
            {
                "action": "sell_call",
                "symbol": sym,
                "strike": stk,
                "notional": notional,
                "premium": prem,
            }
        )

    if premium_total > 0:
        state["last_roll_i"] = bar_i
        state["rolls"] = int(state.get("rolls", 0)) + 1
        state["total_premium"] = round(float(state.get("total_premium", 0)) + premium_total, 2)
        contracts.extend(new_contracts)
        meta["active"] = True
        meta["premium"] = premium_total
        meta["rolls"] = 1

    state["contracts"] = contracts
    return actions, meta


def run_options_sleeve_cycle(
    executor,
    *,
    volatility: str | None = None,
    vix: float | None = None,
    market_open: bool = True,
) -> dict:
    """Paper-only covered call cycle — logs premium; no live options orders."""
    result = {"enabled": False, "actions": [], "premium": 0.0}
    if not config.effective_options_sleeve_enabled() or not market_open:
        return result

    result["enabled"] = True
    vix_level = resolve_vix(volatility=volatility, vix=vix)
    if not is_calm_regime(volatility=volatility, vix=vix_level):
        result["skipped"] = True
        result["reason"] = f"not calm (vix={vix_level:.1f}, vol={volatility})"
        return result

    state = _load_state()
    if not _should_roll_live(state):
        result["skipped"] = True
        result["reason"] = "monthly roll not due"
        return result

    account = executor._get_account()
    equity = float(account.equity)
    holdings = {sym: _executor_holding_value(executor, sym) for sym in UNDERLYINGS}
    alloc = allocate_coverage(equity, holdings)
    if not alloc:
        result["skipped"] = True
        result["reason"] = "no VTI/SPY coverage"
        return result

    rate = monthly_premium_rate(vix_level)
    premium_total = 0.0
    actions = []
    for sym, notional in alloc.items():
        pos = executor._find_position(sym)
        if pos is None:
            continue
        spot = float(getattr(pos, "current_price", 0) or getattr(pos, "market_value", 0) / max(float(getattr(pos, "qty", 1)), 1))
        if spot <= 0:
            continue
        prem = round(notional * rate, 2)
        if prem < 1.0:
            continue
        stk = strike_price(spot)
        premium_total += prem
        label = "VTI" if sym == VTI_SYMBOL else "SPY"
        print(f"--- Sold {label} covered call, collected ${prem:,.2f} premium ---")
        print(f"    strike ${stk:,.2f} ({config.OPTIONS_OTM_PCT:.0%} OTM) | cover ${notional:,.2f}")
        actions.append(
            {
                "symbol": sym,
                "strike": stk,
                "notional": notional,
                "premium": prem,
            }
        )

    if premium_total > 0:
        state["last_roll_utc"] = datetime.now(timezone.utc).isoformat()
        state["total_premium"] = round(float(state.get("total_premium", 0)) + premium_total, 2)
        state["last_premium"] = premium_total
        state["contracts"] = actions
        _save_state(state)
        result["premium"] = premium_total
        result["actions"] = actions
        result["vix"] = round(vix_level, 2)
    else:
        result["skipped"] = True
        result["reason"] = "premium below threshold"

    return result
