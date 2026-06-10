"""Macro Regime Adaptor — oil/gold/VIX/yield/geo signals for paper aggressive profile."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

import config

OIL_PROXIES = ("XOM", "CVX")
GLD_SYMBOL = "GLD"
ENERGY_TARGET = "XOM"
SAFE_HAVEN_TARGET = "GLD"
MACRO_SLEEVE_SYMBOLS = frozenset({GLD_SYMBOL, ENERGY_TARGET, "XLE"})

GEO_KEYWORDS = (
    "iran",
    "israel",
    "conflict",
    "war",
    "sanctions",
    "strait of hormuz",
    "hormuz",
    "geopolitical",
    "missile",
    "invasion",
)

LOOKBACK_DAYS = 5


def _load_daily_close(col: str) -> pd.Series:
    table = f"{col}_daily"
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


def _fetch_and_store_daily(yf_ticker: str, col: str) -> None:
    try:
        raw = yf.download(
            yf_ticker, period="2y", interval="1d", progress=False, auto_adjust=True
        )
        if raw is None or raw.empty:
            return
        df = raw.reset_index()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        rename = {c: "Close" for c in df.columns if str(c).lower() == "close"}
        df = df.rename(columns=rename)
        if "Date" not in df.columns or "Close" not in df.columns:
            return
        conn = sqlite3.connect(config.DB_PATH)
        df[["Date", "Close"]].to_sql(f"{col}_daily", conn, if_exists="replace", index=False)
        conn.close()
    except Exception:
        pass


def ensure_macro_regime_daily() -> None:
    """Ensure VIX/TLT/TNX daily tables exist for regime detection."""
    needed = (("VIX", "^VIX"), ("TLT", "TLT"), ("TNX", "^TNX"), ("GLD", "GLD"))
    for col, yf_sym in needed:
        if len(_load_daily_close(col)) >= LOOKBACK_DAYS + 5:
            continue
        _fetch_and_store_daily(yf_sym, col)


def _pct_return(series: pd.Series, days: int = LOOKBACK_DAYS) -> float | None:
    if series is None or len(series) < days + 1:
        return None
    a = float(series.iloc[-days - 1])
    b = float(series.iloc[-1])
    if a <= 0:
        return None
    return (b / a) - 1.0


def _oil_return_from_window(window: pd.DataFrame) -> float | None:
    rets = []
    for sym in OIL_PROXIES:
        if sym not in window.columns:
            continue
        s = window[sym].dropna()
        if len(s) < LOOKBACK_DAYS + 1:
            continue
        r = _pct_return(s, LOOKBACK_DAYS)
        if r is not None:
            rets.append(r)
    if not rets:
        return None
    return float(np.mean(rets))


def _gld_return(window: pd.DataFrame, gld_daily: pd.Series | None) -> float | None:
    if GLD_SYMBOL in window.columns:
        s = window[GLD_SYMBOL].dropna()
        if len(s) >= LOOKBACK_DAYS + 1:
            return _pct_return(s, LOOKBACK_DAYS)
    if gld_daily is not None and len(gld_daily) >= LOOKBACK_DAYS + 1:
        return _pct_return(gld_daily, LOOKBACK_DAYS)
    return None


def _vix_spike(vix: pd.Series) -> bool:
    if vix is None or len(vix) < LOOKBACK_DAYS + 1:
        return False
    ret = _pct_return(vix, LOOKBACK_DAYS)
    return ret is not None and ret >= config.MACRO_VIX_SPIKE_PCT


def _tlt_yield_stress(tlt: pd.Series, tnx: pd.Series) -> bool:
    if tlt is None or len(tlt) < 25:
        return False
    tlt_weak = float(tlt.iloc[-1]) < float(tlt.rolling(20).mean().iloc[-1])
    tnx_rising = False
    if tnx is not None and len(tnx) >= LOOKBACK_DAYS + 1:
        tr = _pct_return(tnx, LOOKBACK_DAYS)
        tnx_rising = tr is not None and tr > 0.02
    return tlt_weak and tnx_rising


def _detect_geo_risk(wisdom: dict | None) -> tuple[bool, str | None]:
    if not wisdom:
        return False, None
    chunks = [
        str(wisdom.get("felix_video_title") or ""),
        str(wisdom.get("macro_event_guard") or ""),
        str(wisdom.get("regime") or ""),
    ]
    text = " ".join(chunks).lower()
    for kw in GEO_KEYWORDS:
        if kw in text:
            return True, kw
    return False, None


def evaluate_macro_regime(
    window: pd.DataFrame,
    *,
    daily_macro: pd.DataFrame | None = None,
    wisdom: dict | None = None,
    ts=None,
) -> dict:
    """
    Detect macro regime shifts from market data (+ optional wisdom geo keywords).
    Paper aggressive only at integration layer; this function is data-only.
    """
    ensure_macro_regime_daily()
    vix = _load_daily_close("VIX")
    tlt = _load_daily_close("TLT")
    tnx = _load_daily_close("TNX")
    gld_daily = _load_daily_close("GLD")

    if daily_macro is not None and not daily_macro.empty:
        bar = daily_macro.iloc[-1] if ts is None else daily_macro.loc[:ts].iloc[-1:]
        if "TLT" in daily_macro.columns:
            tlt_slice = daily_macro["TLT"].dropna()
            if len(tlt_slice) >= 25:
                tlt = tlt_slice
        if "TNX" in daily_macro.columns:
            tnx_slice = daily_macro["TNX"].dropna()
            if len(tnx_slice) >= LOOKBACK_DAYS + 1:
                tnx = tnx_slice

    oil_ret = _oil_return_from_window(window)
    gld_ret = _gld_return(window, gld_daily)
    oil_shock = oil_ret is not None and oil_ret >= config.MACRO_OIL_SURGE_PCT
    vix_spike = _vix_spike(vix)
    safe_haven = (
        gld_ret is not None
        and gld_ret >= config.MACRO_GLD_SURGE_PCT
        and vix_spike
    )
    geo_risk, geo_kw = _detect_geo_risk(wisdom)
    tlt_yield_stress = _tlt_yield_stress(tlt, tnx)

    messages: list[str] = []
    target = None
    macro_cap_pct = 0.0
    spy_scale = 1.0
    nyse_scale = 1.0
    vti_delta = 0.0
    yield_gate_boost = False

    if oil_shock:
        messages.append("Oil surge detected -> Energy tilt")
        target = ENERGY_TARGET
        macro_cap_pct = max(macro_cap_pct, float(config.MACRO_ENERGY_CAP_PCT))
        nyse_scale = min(1.15, nyse_scale + float(config.MACRO_ENERGY_SLEEVE_BOOST))

    if geo_risk:
        label = geo_kw.replace("strait of hormuz", "Hormuz") if geo_kw else "geo"
        if oil_shock:
            messages.append(f"{label.title()} tensions detected -> Energy tilt")
        else:
            messages.append(f"{label.title()} tensions detected -> Risk-off tilt")
            spy_scale *= 0.90
            nyse_scale *= 0.90
            vti_delta += 0.05
            macro_cap_pct = max(macro_cap_pct, float(config.MACRO_SAFE_HAVEN_CAP_PCT))
            if target is None:
                target = SAFE_HAVEN_TARGET

    if safe_haven:
        messages.append("Safe-haven flow (GLD + VIX) -> GLD allocation")
        target = SAFE_HAVEN_TARGET
        macro_cap_pct = max(macro_cap_pct, float(config.MACRO_SAFE_HAVEN_CAP_PCT))
        spy_scale *= 0.85
        nyse_scale *= 0.85
        vti_delta += 0.05

    if tlt_yield_stress:
        messages.append("TLT weakness + rising yields -> Yield gate strengthened")
        yield_gate_boost = True
        spy_scale *= 0.92
        nyse_scale *= 0.92

    active = bool(messages)
    return {
        "active": active,
        "signals": messages,
        "messages": messages,
        "oil_shock": oil_shock,
        "safe_haven": safe_haven,
        "geo_risk": geo_risk,
        "geo_keyword": geo_kw,
        "tlt_yield_stress": tlt_yield_stress,
        "target": target,
        "macro_cap_pct": round(macro_cap_pct, 4) if active else 0.0,
        "spy_scale": round(spy_scale, 4),
        "nyse_scale": round(nyse_scale, 4),
        "vti_delta": round(vti_delta, 4),
        "yield_gate_boost": yield_gate_boost,
        "oil_ret_5d": round(oil_ret, 4) if oil_ret is not None else None,
        "gld_ret_5d": round(gld_ret, 4) if gld_ret is not None else None,
    }


def merge_regime_sleeve_caps(base_caps: dict[str, float], regime: dict) -> dict[str, float]:
    """Apply macro regime scaling to dynamic sleeve caps."""
    if not regime.get("active"):
        return dict(base_caps)
    caps = dict(base_caps)
    caps["spy"] = round(caps.get("spy", 0.0) * regime.get("spy_scale", 1.0), 6)
    caps["nyse"] = round(caps.get("nyse", 0.0) * regime.get("nyse_scale", 1.0), 6)
    if regime.get("vti_delta", 0) > 0:
        caps["vti_core"] = round(
            min(0.90, caps.get("vti_core", 0.0) + regime["vti_delta"]), 6
        )
    metal = caps.get("metal", 0.0)
    long_sum = caps.get("spy", 0) + caps.get("crypto", 0) + caps.get("nyse", 0)
    caps["cash_buffer"] = round(1.0 - metal - caps.get("vti_core", 0) - long_sum, 6)
    return caps


def apply_yield_gate_boost(yield_gated: bool, regime: dict) -> bool:
    if regime.get("yield_gate_boost"):
        return True
    return yield_gated


def log_regime_messages(regime: dict) -> None:
    for msg in regime.get("messages") or []:
        print(f"--- Macro Regime: {msg} ---")


def run_macro_regime_backtest_day(
    portfolio,
    prices,
    regime: dict,
    *,
    market_open: bool = True,
) -> tuple[list[dict], dict]:
    """Parallel macro sleeve book (GLD / XOM energy proxy) for backtests."""
    meta = {"target": None, "active": False, "cap_pct": 0.0}
    if not config.effective_macro_regime_adaptor_enabled() or not market_open:
        return [], meta
    if not regime.get("active") or not regime.get("target"):
        return [], meta

    target = regime["target"]
    if target not in prices.index:
        return [], meta
    price = prices.get(target)
    if price is None or not np.isfinite(price) or float(price) <= 0:
        return [], meta

    cap_pct = float(regime.get("macro_cap_pct") or config.MACRO_SAFE_HAVEN_CAP_PCT)
    equity = portfolio.equity(prices)
    cap = round(equity * cap_pct, 2)
    min_n = config.effective_min_notional(equity)

    held = [s for s in MACRO_SLEEVE_SYMBOLS if portfolio.positions.get(s, 0) > 0]
    for sym in held:
        if sym == target:
            continue
        qty = portfolio.positions.get(sym, 0)
        if qty <= 0 or sym not in prices.index:
            continue
        sell_n = round(float(qty) * float(prices[sym]), 2)
        if sell_n >= min_n:
            portfolio.trade(sym, "sell", float(prices[sym]), tx_cost=0.0, notional=sell_n)

    current = 0.0
    qty = portfolio.positions.get(target, 0)
    if qty > 0:
        current = round(float(qty) * float(price), 2)
    room = round(cap - current, 2)
    actions: list[dict] = []
    if room >= min_n:
        order = portfolio.trade(target, "buy", float(price), tx_cost=0.0, notional=room)
        if order:
            actions.append({"action": "buy", "symbol": target, "notional": room})

    meta = {"target": target, "active": True, "cap_pct": cap_pct, "signals": regime.get("signals")}
    return actions, meta
