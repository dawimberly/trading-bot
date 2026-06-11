"""Volatility trading overlay — VIX/VXX regime sleeve (paper aggressive only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import config
from modules.options_sleeve import ensure_vix_daily, resolve_vix, vix_as_of

STATE_FILE = Path("volatility_sleeve_state.json")
VXX_SYMBOL = "VXX"


def _load_vix_series():
    ensure_vix_daily()
    from modules.options_sleeve import _load_vix_daily

    return _load_vix_daily()


def vix_change_pct(ts, lookback: int = 5) -> float | None:
    s = _load_vix_series()
    if s.empty:
        return None
    ts = __import__("pandas").Timestamp(ts)
    sub = s.loc[:ts]
    if len(sub) < lookback + 1:
        return None
    cur = float(sub.iloc[-1])
    prev = float(sub.iloc[-1 - lookback])
    if prev <= 0 or not np.isfinite(cur):
        return None
    return (cur - prev) / prev


def classify_vol_regime(
    *,
    vix: float,
    volatility: str | None = None,
    vol_score: float | None = None,
    vix_chg: float | None = None,
) -> str:
    """Return calm | stress | neutral."""
    level = resolve_vix(volatility=volatility, vol_score=vol_score, vix=vix)
    chg = vix_chg
    if chg is None:
        chg = 0.0
    rising = chg >= config.VOL_VIX_SPIKE_PCT
    if level > config.VOL_VIX_HIGH_THRESHOLD or rising:
        return "stress"
    if level < config.VOL_VIX_CALM_THRESHOLD:
        return "calm"
    return "neutral"


def is_contango(*, vix: float, vix_chg: float | None = None) -> bool:
    """Lightweight proxy: stable/low VIX favors short-VXX (contango decay)."""
    chg = vix_chg if vix_chg is not None else 0.0
    return vix < config.VOL_CONTANGO_VIX_MAX and chg < config.VOL_VIX_SPIKE_PCT


def sleeve_notional(equity: float) -> float:
    cap = float(config.VOL_SLEEVE_CAP_PCT)
    cap = max(config.VOL_SLEEVE_CAP_MIN_PCT, min(config.VOL_SLEEVE_CAP_MAX_PCT, cap))
    return round(max(0.0, equity) * cap, 2)


def calm_premium_rate(vix: float) -> float:
    """Monthly premium fraction for short-vol overlay."""
    vix_factor = max(0.5, min(1.4, vix / 16.0))
    return round(0.012 * vix_factor, 6)


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


def _vix_daily_return(ts) -> float:
    s = _load_vix_series()
    if s.empty:
        return 0.0
    ts = __import__("pandas").Timestamp(ts)
    sub = s.loc[:ts]
    if len(sub) < 2:
        return 0.0
    cur = float(sub.iloc[-1])
    prev = float(sub.iloc[-2])
    if prev <= 0:
        return 0.0
    return (cur - prev) / prev


def _position_pnl(mode: str, notional: float, vix_ret: float, *, calm: bool) -> float:
    beta = config.VOL_VXX_BETA
    if mode == "short_vxx":
        move = -notional * vix_ret * beta
        if calm:
            move += notional * config.VOL_CONTANGO_DECAY_DAILY
        return round(move, 2)
    if mode == "long_protection":
        return round(notional * vix_ret * beta, 2)
    return 0.0


def _target_mode(regime: str, *, vix: float, contango: bool) -> str:
    if regime == "calm":
        return "short_vxx"
    if regime == "stress":
        return "long_protection" if not contango else "long_protection"
    return "flat"


def _log_action(action: str, premium: float = 0.0, *, vix: float | None = None) -> None:
    if action == "short_vxx":
        print(
            f"Vol overlay: Short VXX in calm regime, collected ${premium:,.2f} premium"
        )
    elif action == "long_protection":
        v = f", VIX={vix:.1f}" if vix is not None else ""
        print(f"Vol overlay: Long protection in stress regime{ v}")
    elif action == "close":
        print("Vol overlay: Closed vol position (regime shift)")


def run_volatility_backtest_day(
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
    portfolio_peak: float | None = None,
) -> tuple[list[dict], dict]:
    """Simulate daily vol overlay P&L and regime-based rolls."""
    meta = {
        "active": False,
        "regime": "neutral",
        "premium": 0.0,
        "protection_pnl": 0.0,
        "daily_pnl": 0.0,
        "trades": 0,
        "in_drawdown": False,
    }
    actions: list[dict] = []
    if not config.effective_vol_trading_enabled() or not market_open:
        return actions, meta

    vix_level = resolve_vix(ts=ts, volatility=volatility, vol_score=vol_score, vix=vix)
    vix_chg = vix_change_pct(ts) if ts is not None else None
    regime = classify_vol_regime(
        vix=vix_level, volatility=volatility, vol_score=vol_score, vix_chg=vix_chg
    )
    contango = is_contango(vix=vix_level, vix_chg=vix_chg)
    meta["regime"] = regime
    meta["vix"] = round(vix_level, 2)

    eq = portfolio.equity(prices)

    notional = sleeve_notional(eq)
    mode = state.get("mode") or "flat"
    pos_n = float(state.get("notional", 0))
    vix_ret = _vix_daily_return(ts) if ts is not None else 0.0

    if portfolio_peak and eq < portfolio_peak * 0.995:
        meta["in_drawdown"] = True

    if mode != "flat" and pos_n > 0:
        daily = _position_pnl(mode, pos_n, vix_ret, calm=(regime == "calm"))
        portfolio.cash += daily
        state["cum_pnl"] = round(float(state.get("cum_pnl", 0)) + daily, 2)
        meta["daily_pnl"] = daily
        meta["active"] = True
        if mode == "long_protection" and daily > 0 and meta["in_drawdown"]:
            state["protection_pnl"] = round(float(state.get("protection_pnl", 0)) + daily, 2)
            meta["protection_pnl"] = daily

    target = _target_mode(regime, vix=vix_level, contango=contango)
    last_roll = int(state.get("last_roll_i", -999))
    due_roll = bar_i - last_roll >= config.VOL_MONTHLY_BARS
    regime_changed = (mode == "short_vxx" and target != "short_vxx") or (
        mode == "long_protection" and target != "long_protection"
    )

    if target == "flat":
        if mode != "flat" and pos_n > 0:
            actions.append({"action": "close", "from": mode})
            state["trades"] = int(state.get("trades", 0)) + 1
            meta["trades"] = 1
        state["mode"] = "flat"
        state["notional"] = 0.0
        return actions, meta

    if mode != target or (due_roll and target == "short_vxx"):
        if mode != "flat" and pos_n > 0:
            actions.append({"action": "close", "from": mode})
        state["mode"] = target
        state["notional"] = notional
        state["last_roll_i"] = bar_i
        state["trades"] = int(state.get("trades", 0)) + 1
        meta["trades"] = int(meta.get("trades", 0)) + 1

        if target == "short_vxx":
            prem = round(notional * calm_premium_rate(vix_level), 2)
            if prem >= 1.0:
                portfolio.cash += prem
                state["premium_collected"] = round(
                    float(state.get("premium_collected", 0)) + prem, 2
                )
                meta["premium"] = prem
                actions.append(
                    {"action": "short_vxx", "notional": notional, "premium": prem}
                )
        elif target == "long_protection":
            label = "VXX puts" if contango else "VIX calls"
            actions.append(
                {
                    "action": "long_protection",
                    "notional": notional,
                    "instrument": label,
                }
            )

    return actions, meta


def run_volatility_sleeve_cycle(
    executor,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
    vix: float | None = None,
    market_open: bool = True,
) -> dict:
    """Paper-only vol overlay — logs actions; no live vol orders (synthetic PnL in backtest)."""
    result = {
        "enabled": False,
        "regime": "neutral",
        "actions": [],
        "live_log_only": True,
    }
    if not config.effective_vol_trading_enabled() or not market_open:
        return result

    result["enabled"] = True
    vix_level = resolve_vix(volatility=volatility, vol_score=vol_score, vix=vix)
    vix_chg = vix_change_pct(__import__("pandas").Timestamp.now())
    regime = classify_vol_regime(
        vix=vix_level, volatility=volatility, vol_score=vol_score, vix_chg=vix_chg
    )
    contango = is_contango(vix=vix_level, vix_chg=vix_chg)
    result["regime"] = regime
    result["vix"] = round(vix_level, 2)

    account = executor._get_account()
    equity = float(account.equity)
    notional = sleeve_notional(equity)
    state = _load_state()
    prev_mode = state.get("mode", "flat")
    target = _target_mode(regime, vix=vix_level, contango=contango)

    if target == "short_vxx" and prev_mode != "short_vxx":
        prem = round(notional * calm_premium_rate(vix_level), 2)
        if prem >= 1.0:
            _log_action("short_vxx", prem)
            state["mode"] = "short_vxx"
            state["notional"] = notional
            state["premium_collected"] = round(
                float(state.get("premium_collected", 0)) + prem, 2
            )
            state["last_roll_utc"] = datetime.now(timezone.utc).isoformat()
            result["premium"] = prem
            result["actions"].append({"action": "short_vxx", "premium": prem})
            _save_state(state)
    elif target == "long_protection" and prev_mode != "long_protection":
        _log_action("long_protection", vix=vix_level)
        state["mode"] = "long_protection"
        state["notional"] = notional
        state["last_roll_utc"] = datetime.now(timezone.utc).isoformat()
        result["actions"].append({"action": "long_protection", "notional": notional})
        _save_state(state)
    elif target == "flat" and prev_mode != "flat":
        _log_action("close")
        state["mode"] = "flat"
        state["notional"] = 0.0
        _save_state(state)

    return result
