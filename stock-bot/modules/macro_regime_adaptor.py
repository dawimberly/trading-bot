"""Regime Shift Detector — oil/gold/VIX/geo/yield signals (paper aggressive only)."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

import config

logger = logging.getLogger(__name__)

OIL_SYMBOLS = ("XOM", "USO")
GLD_SYMBOL = "GLD"
ENERGY_TARGET = "XLE"
ENERGY_FALLBACK = "XOM"
SAFE_HAVEN_TARGET = "GLD"
MACRO_SLEEVE_SYMBOLS = frozenset({GLD_SYMBOL, ENERGY_TARGET, ENERGY_FALLBACK})

GEO_KEYWORDS = (
    "iran",
    "israel",
    "middle east",
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
ROOT = Path(__file__).resolve().parents[1]


def _clamp_scale(scale: float) -> float:
    lo = 1.0 - config.MACRO_SLEEVE_ADJUST_MAX_PCT
    hi = 1.0 + config.MACRO_SLEEVE_ADJUST_MAX_PCT
    return round(max(lo, min(hi, scale)), 4)


def _load_daily_close(col: str) -> pd.Series:
    table = f"{col}_daily"
    try:
        conn = sqlite3.connect(config.DB_PATH)
        df = pd.read_sql(f'SELECT * FROM "{table}"', conn)
        conn.close()
    except Exception as exc:
        logger.debug("macro daily series read failed for %s: %s", col, exc)
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
        if "Date" not in df.columns:
            date_col = "index" if "index" in df.columns else df.columns[0]
            df = df.rename(columns={date_col: "Date"})
        if "Date" not in df.columns or "Close" not in df.columns:
            return
        conn = sqlite3.connect(config.DB_PATH)
        df[["Date", "Close"]].to_sql(f"{col}_daily", conn, if_exists="replace", index=False)
        conn.close()
    except Exception as exc:
        logger.warning("macro daily series persist failed for %s: %s", col, exc)


def ensure_macro_regime_daily() -> None:
    """Ensure VIX/TLT/TNX/GLD/USO daily tables exist."""
    needed = (
        ("VIX", "^VIX"),
        ("TLT", "TLT"),
        ("TNX", "^TNX"),
        ("GLD", "GLD"),
        ("USO", "USO"),
    )
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


def _series_as_of(series: pd.Series, ts) -> pd.Series:
    if series is None or series.empty:
        return series
    ts = pd.Timestamp(ts)
    return series.loc[:ts]


def _oil_surge(window: pd.DataFrame) -> tuple[bool, float | None, str | None]:
    best_ret = None
    best_sym = None
    for sym in OIL_SYMBOLS:
        if sym not in window.columns:
            continue
        s = window[sym].dropna()
        if len(s) < LOOKBACK_DAYS + 1:
            continue
        r = _pct_return(s, LOOKBACK_DAYS)
        if r is not None and r >= config.MACRO_OIL_SURGE_PCT:
            if best_ret is None or r > best_ret:
                best_ret = r
                best_sym = sym
    return best_ret is not None, best_ret, best_sym


def _gld_return(window: pd.DataFrame, gld_daily: pd.Series | None) -> float | None:
    if GLD_SYMBOL in window.columns:
        s = window[GLD_SYMBOL].dropna()
        if len(s) >= LOOKBACK_DAYS + 1:
            return _pct_return(s, LOOKBACK_DAYS)
    if gld_daily is not None and len(gld_daily) >= LOOKBACK_DAYS + 1:
        return _pct_return(gld_daily, LOOKBACK_DAYS)
    return None


def _vix_level(vix: pd.Series) -> float | None:
    if vix is None or vix.empty:
        return None
    val = float(vix.iloc[-1])
    return val if np.isfinite(val) else None


def _tlt_yield_stress(tlt: pd.Series, tnx: pd.Series) -> bool:
    if tlt is None or len(tlt) < 25:
        return False
    tlt_weak = float(tlt.iloc[-1]) < float(tlt.rolling(20).mean().iloc[-1])
    tnx_rising = False
    if tnx is not None and len(tnx) >= LOOKBACK_DAYS + 1:
        tr = _pct_return(tnx, LOOKBACK_DAYS)
        tnx_rising = tr is not None and tr > 0.02
    return tlt_weak and tnx_rising


def _load_news_text() -> str:
    path = ROOT / config.WEB_SENTIMENT_CACHE_FILE
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    parts = [str(data.get("headline_text") or "")]
    for src in data.get("sources") or []:
        if isinstance(src, dict):
            parts.append(str(src.get("headline_text") or ""))
    return " ".join(parts).lower()


def _detect_geo_risk(wisdom: dict | None, news_text: str | None = None) -> tuple[bool, str | None]:
    chunks = []
    if wisdom:
        chunks.extend(
            [
                str(wisdom.get("felix_video_title") or ""),
                str(wisdom.get("macro_event_guard") or ""),
                str(wisdom.get("regime") or ""),
            ]
        )
    if news_text:
        chunks.append(news_text)
    else:
        chunks.append(_load_news_text())
    text = " ".join(chunks).lower()
    for kw in GEO_KEYWORDS:
        if kw in text:
            return True, kw
    return False, None


def _energy_target(prices) -> str:
    if ENERGY_TARGET in prices.index:
        return ENERGY_TARGET
    return ENERGY_FALLBACK


def evaluate_macro_regime(
    window: pd.DataFrame,
    *,
    daily_macro: pd.DataFrame | None = None,
    wisdom: dict | None = None,
    news_text: str | None = None,
    ts=None,
) -> dict:
    """Detect regime shifts from market data + optional news/wisdom geo keywords."""
    if not config.paper_aggressive_context() and not config.PAPER_AGGRESSIVE_ENABLED:
        return {"active": False, "signals": [], "messages": []}

    ensure_macro_regime_daily()
    vix = _load_daily_close("VIX")
    tlt = _load_daily_close("TLT")
    tnx = _load_daily_close("TNX")
    gld_daily = _load_daily_close("GLD")

    if ts is not None:
        vix = _series_as_of(vix, ts)
        tlt = _series_as_of(tlt, ts)
        tnx = _series_as_of(tnx, ts)
        gld_daily = _series_as_of(gld_daily, ts)

    if daily_macro is not None and not daily_macro.empty:
        if ts is not None:
            macro_slice = daily_macro.loc[:ts]
        else:
            macro_slice = daily_macro
        if "TLT" in macro_slice.columns:
            tlt_slice = macro_slice["TLT"].dropna()
            if len(tlt_slice) >= 25:
                tlt = tlt_slice
        if "TNX" in macro_slice.columns:
            tnx_slice = macro_slice["TNX"].dropna()
            if len(tnx_slice) >= LOOKBACK_DAYS + 1:
                tnx = tnx_slice

    oil_shock, oil_ret, oil_sym = _oil_surge(window)
    gld_ret = _gld_return(window, gld_daily)
    vix_val = _vix_level(vix)
    safe_haven = (
        gld_ret is not None
        and gld_ret >= config.MACRO_GLD_SURGE_PCT
        and vix_val is not None
        and vix_val >= config.MACRO_VIX_SAFE_HAVEN_MIN
    )
    geo_risk, geo_kw = _detect_geo_risk(wisdom, news_text=news_text)
    tlt_yield_stress = _tlt_yield_stress(tlt, tnx)

    messages: list[str] = []
    target = None
    macro_cap_pct = 0.0
    spy_scale = 1.0
    nyse_scale = 1.0
    energy_scale = 1.0
    vti_delta = 0.0
    yield_gate_boost = False

    boost = float(config.MACRO_ENERGY_SLEEVE_BOOST)
    boost = max(0.05, min(0.10, boost))

    if oil_shock:
        messages.append(f"Oil shock detected ({oil_sym} +{oil_ret:.1%}) -> Energy tilt")
        target = ENERGY_TARGET
        macro_cap_pct = max(macro_cap_pct, float(config.MACRO_ENERGY_CAP_PCT))
        energy_scale = _clamp_scale(1.0 + boost)
        nyse_scale = _clamp_scale(nyse_scale + boost * 0.5)

    if geo_risk:
        label = (geo_kw or "geo").replace("strait of hormuz", "Hormuz").title()
        if oil_shock:
            messages.append(f"{label} tensions detected -> Energy tilt")
        else:
            messages.append(f"{label} tensions detected -> Risk-off tilt")
            spy_scale = _clamp_scale(spy_scale * 0.90)
            nyse_scale = _clamp_scale(nyse_scale * 0.90)
            vti_delta = min(0.05, config.MACRO_SLEEVE_ADJUST_MAX_PCT)
            macro_cap_pct = max(macro_cap_pct, float(config.MACRO_SAFE_HAVEN_CAP_PCT))
            if target is None:
                target = SAFE_HAVEN_TARGET

    if safe_haven:
        messages.append(
            f"Safe-haven flow (GLD +{gld_ret:.1%}, VIX {vix_val:.1f}) -> GLD allocation"
        )
        target = SAFE_HAVEN_TARGET
        macro_cap_pct = max(macro_cap_pct, float(config.MACRO_SAFE_HAVEN_CAP_PCT))
        spy_scale = _clamp_scale(spy_scale * 0.90)
        nyse_scale = _clamp_scale(nyse_scale * 0.90)
        vti_delta = min(vti_delta + 0.05, config.MACRO_SLEEVE_ADJUST_MAX_PCT)

    if tlt_yield_stress:
        messages.append("Yield shock (TNX rising + TLT weak) -> Yield gate strengthened")
        yield_gate_boost = True
        spy_scale = _clamp_scale(spy_scale * 0.92)
        nyse_scale = _clamp_scale(nyse_scale * 0.92)

    active = bool(messages)
    return {
        "active": active,
        "signals": messages,
        "messages": messages,
        "oil_shock": oil_shock,
        "oil_symbol": oil_sym,
        "safe_haven": safe_haven,
        "geo_risk": geo_risk,
        "geo_keyword": geo_kw,
        "tlt_yield_stress": tlt_yield_stress,
        "target": target,
        "macro_cap_pct": round(macro_cap_pct, 4) if active else 0.0,
        "spy_scale": spy_scale,
        "nyse_scale": nyse_scale,
        "energy_scale": energy_scale,
        "vti_delta": round(vti_delta, 4),
        "yield_gate_boost": yield_gate_boost,
        "oil_ret_5d": round(oil_ret, 4) if oil_ret is not None else None,
        "gld_ret_5d": round(gld_ret, 4) if gld_ret is not None else None,
        "vix_level": round(vix_val, 2) if vix_val is not None else None,
    }


def merge_regime_sleeve_caps(base_caps: dict[str, float], regime: dict) -> dict[str, float]:
    """Apply regime scaling to dynamic sleeve caps (±15% max per sleeve)."""
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
        print(f"--- Regime Shift: {msg} ---")


def run_macro_regime_backtest_day(
    portfolio,
    prices,
    regime: dict,
    *,
    market_open: bool = True,
) -> tuple[list[dict], dict]:
    """Parallel regime sleeve book (GLD / XLE / XOM) for backtests."""
    meta = {"target": None, "active": False, "cap_pct": 0.0}
    if not config.effective_macro_regime_adaptor_enabled() or not market_open:
        return [], meta
    if not regime.get("active") or not regime.get("target"):
        return [], meta

    target = regime["target"]
    if target == ENERGY_TARGET and target not in prices.index:
        target = ENERGY_FALLBACK
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
