"""Opportunistic directional shorts — Realistic Research v1.1c / paper only."""

from __future__ import annotations

import logging
from typing import Callable

import config
from modules.pipeline_strategies import (
    COOLDOWN_SECONDS,
    _on_cooldown,
)

logger = logging.getLogger(__name__)

BEARISH_REGIMES = (
    "RHYME_B: Panic_Volatility",
    "RHYME_E: Steady_Bearish_Decline",
)


def _short_meta(executor) -> dict:
    portfolio = getattr(executor, "portfolio", None)
    if portfolio is None:
        book = getattr(executor, "_short_position_meta", None)
        if book is None:
            book = {}
            executor._short_position_meta = book
        return book
    book = getattr(portfolio, "short_position_meta", None)
    if book is None:
        book = {}
        portfolio.short_position_meta = book
    return book


def _spy_market_down_signal(data, symbol: str, ma_window: int) -> tuple[bool, float]:
    """True when price is below MA; depth = fractional distance below MA."""
    if symbol not in data.columns:
        return False, 0.0
    prices = data[symbol].dropna()
    if len(prices) < ma_window:
        return False, 0.0
    window = min(ma_window, len(prices))
    ma = prices.rolling(window=window).mean().iloc[-1]
    current = prices.iloc[-1]
    if ma <= 0 or current >= ma:
        return False, 0.0
    return True, float(1.0 - current / ma)


def _resolve_vix_level(
    data,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
) -> float | None:
    if "VIX" in data.columns:
        series = data["VIX"].dropna()
        if len(series):
            val = float(series.iloc[-1])
            if val > 0:
                return val
    try:
        from modules.options_sleeve import resolve_vix

        ts = data.index[-1] if hasattr(data, "index") and len(data.index) else None
        return resolve_vix(ts=ts, volatility=volatility, vol_score=vol_score)
    except Exception:
        return None


def _vix_change_pct(data) -> float | None:
    if "VIX" not in data.columns:
        try:
            from modules.volatility_sleeve import vix_change_pct

            ts = data.index[-1] if hasattr(data, "index") and len(data.index) else None
            if ts is not None:
                return vix_change_pct(ts, lookback=5)
        except Exception:
            pass
        return None
    series = data["VIX"].dropna()
    if len(series) < 6:
        return None
    cur = float(series.iloc[-1])
    prev = float(series.iloc[-6])
    if prev <= 0:
        return None
    return (cur - prev) / prev


def vix_confirms_short(
    data,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
) -> tuple[bool, str]:
    from modules.pipeline_strategies import short_vix_spike_confirmed

    ok, reason, _ = short_vix_spike_confirmed(
        data, volatility=volatility, vol_score=vol_score
    )
    return ok, reason


def _momentum_score(data, symbol: str, lookback: int = 20) -> float | None:
    if symbol not in data.columns:
        return None
    series = data[symbol].dropna()
    if len(series) < lookback:
        return None
    start = series.iloc[-lookback]
    end = series.iloc[-1]
    if start <= 0:
        return None
    return float(end / start - 1.0)


def momentum_exhaustion(data, symbol: str) -> tuple[bool, float]:
    """Prior rally rolling over — late-cycle / exhaustion filter."""
    lb = max(5, int(config.SHORT_MOMENTUM_EXHAUSTION_LOOKBACK))
    if symbol not in data.columns:
        return False, 0.0
    series = data[symbol].dropna()
    if len(series) < lb + 5:
        return False, 0.0
    prior = float(series.iloc[-lb] / series.iloc[-lb - 5] - 1.0) if len(series) >= lb + 5 else 0.0
    recent = float(series.iloc[-1] / series.iloc[-5] - 1.0) if len(series) >= 5 else 0.0
    score = max(0.0, prior) * max(0.0, -recent)
    exhausted = prior >= config.SHORT_MOMENTUM_EXHAUSTION_MIN and recent < 0
    return exhausted, score


def bubble_risk_score(
    data,
    regime: str,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
) -> float:
    """0–1 score: euphoria unwind + vol expansion + breakdown from extension."""
    spy = config.SPY_BOT_SYMBOL
    score = 0.0
    reg = str(regime or "")
    if "RHYME_B" in reg:
        score += 0.35
    elif "RHYME_E" in reg:
        score += 0.20
    if "RHYME_A" in reg:
        score += 0.15

    ma_window = config.effective_spy_ma_window()
    bearish, depth = _spy_market_down_signal(data, spy, ma_window)
    if bearish:
        score += min(0.25, depth * 5.0)

    exhausted, exh_score = momentum_exhaustion(data, spy)
    if exhausted:
        score += min(0.20, 0.10 + exh_score * 2.0)

    vix = _resolve_vix_level(data, volatility=volatility, vol_score=vol_score)
    chg = _vix_change_pct(data)
    if vix is not None and vix >= config.SHORT_VIX_MIN_LEVEL:
        score += 0.15
    if chg is not None and chg >= config.VOL_VIX_SPIKE_PCT:
        score += 0.15

    mom20 = _momentum_score(data, spy, 20)
    mom5 = _momentum_score(data, spy, 5)
    if mom20 is not None and mom5 is not None and mom20 > 0.03 and mom5 < -0.01:
        score += 0.10

    return round(min(1.0, max(0.0, score)), 3)


def short_regime_active(regime: str) -> bool:
    reg = str(regime or "")
    return any(token in reg for token in BEARISH_REGIMES)


def opportunistic_short_allowed(
    regime: str,
    data,
    *,
    volatility: str | None = None,
    vol_score: float | None = None,
) -> bool:
    from modules.pipeline_strategies import evaluate_short_entry_triggers

    return bool(
        evaluate_short_entry_triggers(
            data, regime, volatility=volatility, vol_score=vol_score
        ).get("allowed")
    )


def _equity(prices, executor) -> float:
    if hasattr(executor, "portfolio"):
        return float(executor.portfolio.equity(prices))
    return float(executor._get_account().equity)


def _position_qty(executor, symbol: str) -> float:
    target = config.normalize_symbol(symbol)
    if hasattr(executor, "portfolio"):
        for sym, qty in executor.portfolio.positions.items():
            if config.normalize_symbol(sym) == target:
                return float(qty)
        return 0.0
    try:
        for pos in executor.client.get_all_positions():
            if config.normalize_symbol(pos.symbol) == target:
                return float(pos.qty)
    except Exception:
        return 0.0
    return 0.0


def short_sleeve_gross_exposure(executor, prices) -> float:
    """Sum of absolute market value on naked short legs (qty < 0)."""
    total = 0.0
    if hasattr(executor, "portfolio"):
        for sym, qty in executor.portfolio.positions.items():
            if float(qty) >= 0:
                continue
            px = prices.get(sym)
            if px is None and hasattr(prices, "get"):
                px = prices.get(config.normalize_symbol(sym))
            if px is not None and float(px) > 0:
                total += abs(float(qty) * float(px))
        return total
    try:
        for pos in executor.client.get_all_positions():
            qty = float(pos.qty)
            if qty >= 0:
                continue
            px = float(getattr(pos, "current_price", 0) or getattr(pos, "avg_entry_price", 0) or 0)
            if px > 0:
                total += abs(qty * px)
    except Exception:
        pass
    return total


def _regime_size_multiplier(regime: str, bubble_score: float) -> float:
    reg = str(regime or "")
    if "RHYME_B" in reg:
        base = config.SHORT_REGIME_B_SIZE_MULT
    elif "RHYME_E" in reg:
        base = config.SHORT_REGIME_E_SIZE_MULT
    else:
        base = 0.85
    bubble_adj = 0.75 + 0.50 * max(0.0, min(1.0, bubble_score))
    return base * bubble_adj


def short_target_gross_pct(regime: str, bubble_score: float) -> float:
    """Dynamic gross short sleeve target between min and max % of equity."""
    lo = config.effective_protective_short_min_pct()
    hi = config.effective_protective_short_max_pct()
    mid = (lo + hi) / 2.0
    reg = str(regime or "")
    reg_boost = 1.0 if "RHYME_B" in reg else 0.85 if "RHYME_E" in reg else 0.70
    bubble = max(0.0, min(1.0, float(bubble_score)))
    tilt = (bubble - 0.45) / 0.55 if bubble > 0.45 else (bubble - 0.45) / 0.45
    tilt = max(-1.0, min(1.0, tilt))
    half = (hi - lo) / 2.0
    pct = mid + half * tilt * reg_boost
    return round(max(lo, min(hi, pct)), 4)


def update_long_hedge_multiplier(executor, prices) -> float:
    """Scale down long entries when protective shorts are active (hedging effect)."""
    mult = 1.0
    if config.SHORT_LONG_HEDGE_ENABLED and config.effective_opportunistic_short_enabled():
        equity = _equity(prices, executor)
        gross = short_sleeve_gross_exposure(executor, prices)
        if equity > 0 and gross > 0:
            util = gross / equity
            cap = max(config.effective_protective_short_max_pct(), 0.01)
            strength = min(1.0, util / cap)
            floor = float(config.SHORT_LONG_HEDGE_FLOOR)
            mult = 1.0 - strength * (1.0 - floor)
    executor._short_long_hedge_mult = mult
    return mult


def compute_short_notional(
    executor,
    prices,
    *,
    regime: str = "",
    vol_score: float | None = None,
    bubble_score: float = 0.0,
    drawdown: float = 0.0,
    leg_max_pct: float | None = None,
    leg_count: int | None = None,
) -> float | None:
    equity = _equity(prices, executor)
    if equity <= 0:
        return None
    target_pct = short_target_gross_pct(regime, bubble_score)
    cap = equity * target_pct
    gross = short_sleeve_gross_exposure(executor, prices)
    room = round(cap - gross, 2)
    min_n = config.effective_min_notional(equity)
    if room < min_n:
        return None

    n_legs = leg_count or max(1, len(config.short_broad_symbols()))
    if config.effective_sector_short_enabled():
        n_legs += int(config.SECTOR_SHORT_MAX_POSITIONS)
    per_leg_room = room / max(1, n_legs)
    max_order = config.effective_max_notional_per_order(equity)
    notional = min(per_leg_room, max_order)
    if leg_max_pct is not None:
        notional = min(notional, equity * float(leg_max_pct))

    mult = 1.0
    try:
        from modules.regime_sizing import effective_regime_sizing_multiplier

        mult = effective_regime_sizing_multiplier(regime)
    except Exception:
        mult = 1.0
    notional *= max(0.0, float(mult))
    notional *= _regime_size_multiplier(regime, bubble_score)

    if config.effective_tail_risk_controls():
        try:
            from modules.paper_risk_controls import (
                portfolio_vol_risk_multiplier,
                regime_dd_risk_multiplier,
                vol_ceiling_risk_multiplier,
            )

            vol_mult = portfolio_vol_risk_multiplier(
                equity_history=getattr(executor, "_equity_history", None),
                vol_score=vol_score,
            )
            vol_mult *= vol_ceiling_risk_multiplier(vol_score)
            floor = float(config.SHORT_VOL_SIZE_FLOOR)
            notional *= max(floor, vol_mult)
            notional *= regime_dd_risk_multiplier(regime, drawdown)
        except Exception:
            pass

    notional = round(notional, 2)
    if notional < min_n:
        return None
    return notional


def _weak_nyse_candidates(data, *, limit: int = 8) -> list[str]:
    symbols = config.nyse_momentum_universe(data.columns)
    scored: list[tuple[float, str]] = []
    for sym in symbols:
        if sym in (config.SPY_BOT_SYMBOL, config.VTI_CORE_SYMBOL):
            continue
        mom = _momentum_score(data, sym)
        if mom is None:
            continue
        scored.append((mom, sym))
    scored.sort(key=lambda x: x[0])
    return [sym for _, sym in scored[:limit]]


def _exit_move_z(data, sym: str, window: int = 20) -> float:
    if sym not in data.columns:
        return 0.0
    rets = data[sym].pct_change().dropna()
    if len(rets) < window + 1:
        return 0.0
    tail = rets.iloc[-window:]
    last = float(rets.iloc[-1])
    std = float(tail.std())
    if std < 1e-9:
        return 0.0
    return abs(last / std)


def _short_exit_reason(
    sym: str,
    data,
    executor,
    regime: str,
    bar_index: int,
    *,
    is_broad: bool,
) -> str | None:
    qty = _position_qty(executor, sym)
    if qty >= 0:
        return None
    meta = _short_meta(executor).get(config.normalize_symbol(sym))
    if not meta:
        return None

    prices = data.iloc[-1] if hasattr(data, "iloc") else data
    px = float(prices.get(sym) or 0)
    if px <= 0:
        return None

    entry = float(meta.get("entry_price") or px)
    entry_bar = int(meta.get("entry_bar") or 0)
    held = max(0, int(bar_index) - entry_bar)
    low_water = float(meta.get("low_water") or entry)
    low_water = min(low_water, px)
    meta["low_water"] = low_water

    gain_pct = (entry - px) / entry if entry > 0 else 0.0
    loss_pct = (px - entry) / entry if entry > 0 else 0.0

    if loss_pct >= config.SHORT_STOP_LOSS_PCT:
        return "stop_loss"
    if gain_pct >= config.SHORT_PROFIT_TARGET_PCT:
        return "profit_target"
    best_gain = (entry - low_water) / entry if entry > 0 else 0.0
    arm = config.SHORT_PROFIT_TARGET_PCT * config.SHORT_TRAILING_ARM_FRAC
    pullback = config.SHORT_TRAILING_PULLBACK_FRAC
    if best_gain >= arm and gain_pct <= best_gain * (1.0 - pullback):
        return "trailing_profit"

    if held >= config.SHORT_MAX_HOLD_BARS:
        return "max_hold"

    move_z = _exit_move_z(data, sym)
    if held >= config.SHORT_TIME_EXIT_BARS:
        if gain_pct > 0 or move_z >= config.SHORT_EXIT_MIN_Z:
            return "time_exit"
        if move_z >= config.SHORT_EXIT_MIN_Z and gain_pct >= -config.SHORT_STOP_LOSS_PCT * 0.5:
            return "reversion_exit"

    if held < config.SHORT_MIN_HOLD_BARS:
        return None

    if not opportunistic_short_allowed(
        regime,
        data,
        volatility=getattr(executor, "_last_volatility", None),
        vol_score=getattr(executor, "_last_vol_score", None),
    ):
        if move_z >= config.SHORT_EXIT_MIN_Z:
            return "regime_exit"
        return None

    if is_broad:
        ma_window = config.effective_spy_ma_window()
        if sym not in data.columns:
            return None
        series = data[sym].dropna()
        if len(series) < ma_window:
            return None
        ma = series.rolling(window=min(ma_window, len(series))).mean().iloc[-1]
        buffer = 1.0 + config.SHORT_MA_EXIT_BUFFER
        if ma > 0 and px >= ma * buffer and move_z >= config.SHORT_EXIT_MIN_Z:
            return "ma_reclaim"
    else:
        mom = _momentum_score(data, sym, 10)
        if mom is not None and mom > -0.003 and move_z >= config.SHORT_EXIT_MIN_Z:
            return "momentum_recovery"

    return None


def _cover_short(
    executor,
    sym: str,
    reason: str,
    regime: str,
    *,
    log_fn: Callable | None = None,
    bar_index: int | None = None,
) -> bool:
    order = executor.execute_order(
        sym,
        "buy",
        reduce_only=True,
        reason=f"{sym}/SHORT/{reason.upper()}",
        sleeve="SHORT",
        strategy="opportunistic_short",
        naked_cover=True,
        pair_cover=False,
        exit_reason=reason,
        entry_bar=(_short_meta(executor).get(config.normalize_symbol(sym)) or {}).get("entry_bar"),
        exit_bar=bar_index,
    )
    if not order:
        return False
    meta = _short_meta(executor).pop(config.normalize_symbol(sym), None)
    notional = order.get("notional", "")
    logger.info(
        "SHORT CLOSE %s reason=%s regime=%s notional=%s hold=%s",
        sym,
        reason,
        regime,
        notional,
        (bar_index - meta.get("entry_bar")) if meta and bar_index is not None else "?",
    )
    if log_fn:
        log_fn(sym, "buy", regime, f"SHORT/{reason.upper()}", 0, notional)
    return True


def run_opportunistic_short_exits(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    log_fn: Callable | None = None,
    volatility: str | None = None,
) -> int:
    if not config.effective_opportunistic_short_enabled():
        return 0
    executor._last_volatility = volatility
    exits = 0
    bar_index = int(now) if now is not None else 0

    for sym in config.short_broad_symbols():
        if _position_qty(executor, sym) >= 0:
            continue
        reason = _short_exit_reason(
            sym, data, executor, regime, bar_index, is_broad=True
        )
        if reason and _cover_short(
            executor, sym, reason, regime, log_fn=log_fn, bar_index=bar_index
        ):
            exits += 1

    if config.effective_short_opportunistic_single_names():
        for sym in _weak_nyse_candidates(data, limit=20):
            if _position_qty(executor, sym) >= 0:
                continue
            reason = _short_exit_reason(
                sym, data, executor, regime, bar_index, is_broad=False
            )
            if reason and _cover_short(
                executor, sym, reason, regime, log_fn=log_fn, bar_index=bar_index
            ):
                exits += 1
    return exits


def _open_short(
    executor,
    data,
    prices,
    regime,
    now,
    pair_cooldown,
    symbol: str,
    *,
    pair_key: str,
    cooldown_bars=None,
    log_fn: Callable | None = None,
    vol_score: float | None = None,
    bubble_score: float = 0.0,
    vix_reason: str = "",
    trigger_reason: str = "",
    leg_max_pct: float | None = None,
) -> int:
    qty = _position_qty(executor, symbol)
    if qty < 0 or qty > 0:
        return 0

    drawdown = 0.0
    hist = getattr(executor, "_equity_history", None)
    if hist and len(hist) >= 2:
        peak = max(hist)
        if peak > 0:
            drawdown = max(0.0, (peak - hist[-1]) / peak)

    notional = compute_short_notional(
        executor,
        prices,
        regime=regime,
        vol_score=vol_score,
        bubble_score=bubble_score,
        drawdown=drawdown,
        leg_max_pct=leg_max_pct,
    )
    if notional is None:
        return 0
    if _on_cooldown(
        pair_cooldown,
        pair_key,
        now,
        cooldown_seconds=COOLDOWN_SECONDS,
        cooldown_bars=cooldown_bars,
    ):
        return 0
    att = getattr(executor, "_attribution", None)
    if att:
        att.record_signals("opportunistic_short", 1)
        att.record_intents("opportunistic_short", 1)

    bar_index = int(now) if now is not None else 0
    px = float(prices.get(symbol) or 0)
    order = executor.execute_order(
        symbol,
        "sell",
        notional=notional,
        reason=pair_key,
        sleeve="SHORT",
        strategy="opportunistic_short",
        naked_short=True,
        entry_bar=bar_index,
        bubble_score=bubble_score,
    )
    from modules.pipeline_strategies import _count_if_filled

    if not _count_if_filled(executor, order):
        return 0
    pair_cooldown[pair_key] = now
    att = getattr(executor, "_attribution", None)
    if att and hasattr(att, "record_short_entry") and trigger_reason:
        att.record_short_entry(trigger_reason)
        if hasattr(att, "record_short_trigger"):
            att.record_short_trigger(
                {"trigger_reason": trigger_reason, "allowed": True}, opened=True
            )
    if px > 0:
        _short_meta(executor)[config.normalize_symbol(symbol)] = {
            "entry_price": px,
            "entry_bar": bar_index,
            "low_water": px,
            "regime": str(regime),
            "bubble_score": bubble_score,
            "trigger_reason": trigger_reason,
        }
    vix = _resolve_vix_level(data, vol_score=vol_score)
    logger.info(
        "SHORT OPEN %s $%.0f regime=%s bubble=%.2f vix=%s trigger=%s",
        symbol,
        notional,
        regime,
        bubble_score,
        f"{vix:.1f}" if vix is not None else "n/a",
        trigger_reason or vix_reason,
    )
    if log_fn:
        log_fn(symbol, "sell", regime, pair_key, bubble_score, notional)
    return 1


def run_broad_market_shorts(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    log_fn: Callable | None = None,
    vol_score: float | None = None,
    bubble_score: float = 0.0,
    vix_reason: str = "",
    trigger_reason: str = "",
) -> int:
    if not config.effective_protective_short_enabled():
        return 0
    trades = 0
    ma_window = config.effective_spy_ma_window()
    prices = data.iloc[-1] if hasattr(data, "iloc") else data
    for sym in config.short_broad_symbols():
        if sym not in data.columns:
            continue
        bearish, depth = _spy_market_down_signal(data, sym, ma_window)
        if not bearish:
            continue
        if sym == config.SPY_BOT_SYMBOL and "RHYME_E" in str(regime):
            if depth < config.SHORT_DEEP_BEAR_MIN_DEPTH:
                continue
        if "RHYME_B" in str(regime) and depth < config.SHORT_RHYME_B_MIN_DEPTH:
            continue
        pair_key = f"{sym}/SHORT/MA{ma_window}"
        trades += _open_short(
            executor,
            data,
            prices,
            regime,
            now,
            pair_cooldown,
            sym,
            pair_key=pair_key,
            cooldown_bars=cooldown_bars,
            log_fn=log_fn,
            vol_score=vol_score,
            bubble_score=bubble_score,
            vix_reason=vix_reason,
            trigger_reason=trigger_reason,
        )
    return trades


def run_single_name_shorts(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    log_fn: Callable | None = None,
    vol_score: float | None = None,
    bubble_score: float = 0.0,
    vix_reason: str = "",
    trigger_reason: str = "",
) -> int:
    if not config.effective_short_opportunistic_single_names():
        return 0
    if "RHYME_B" not in str(regime or ""):
        return 0
    if bubble_score < config.SHORT_BUBBLE_SCORE_MIN:
        return 0
    trades = 0
    prices = data.iloc[-1] if hasattr(data, "iloc") else data
    max_trades = max(1, int(config.SHORT_SINGLE_NAME_MAX_TRADES))
    for sym in _weak_nyse_candidates(data, limit=max_trades * 3):
        if trades >= max_trades:
            break
        mom = _momentum_score(data, sym)
        if mom is None or mom > config.SHORT_WEAK_MOMENTUM_MAX:
            continue
        exhausted, _ = momentum_exhaustion(data, sym)
        if not exhausted and mom > config.SHORT_WEAK_MOMENTUM_MAX * 1.5:
            continue
        try:
            from modules.dynamic_universe import short_borrow_allowed

            if not short_borrow_allowed(sym):
                continue
        except ImportError:
            pass
        pair_key = f"{sym}/SHORT/WEAK"
        trades += _open_short(
            executor,
            data,
            prices,
            regime,
            now,
            pair_cooldown,
            sym,
            pair_key=pair_key,
            cooldown_bars=cooldown_bars,
            log_fn=log_fn,
            vol_score=vol_score,
            bubble_score=bubble_score,
            vix_reason=vix_reason,
            trigger_reason=trigger_reason,
        )
    return trades


def run_opportunistic_short_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    log_fn: Callable | None = None,
    volatility: str | None = None,
) -> int:
    """Directional shorts on broad indices + weak single names (paper only)."""
    if not config.effective_opportunistic_short_enabled():
        return 0
    vol_score = 0.02 if volatility == "High" else 0.01 if volatility == "Low" else None
    executor._last_vol_score = vol_score
    executor._last_volatility = volatility
    prices = data.iloc[-1] if hasattr(data, "iloc") else data
    update_long_hedge_multiplier(executor, prices)

    trades = run_opportunistic_short_exits(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        cooldown_bars=cooldown_bars,
        log_fn=log_fn,
        volatility=volatility,
    )
    from modules.pipeline_strategies import evaluate_short_entry_triggers

    trigger = evaluate_short_entry_triggers(
        data, regime, volatility=volatility, vol_score=vol_score
    )
    att = getattr(executor, "_attribution", None)
    if att and hasattr(att, "record_short_trigger") and short_regime_active(regime):
        att.record_short_trigger(trigger)
        from modules.backtest_attribution import format_short_scan_log

        scan_log = format_short_scan_log(trigger)
        if scan_log:
            if log_fn:
                log_fn(scan_log)
            else:
                import logging

                logging.getLogger(__name__).info(scan_log)
    if not trigger.get("allowed"):
        return trades

    bubble = float(trigger.get("bubble_score") or 0.0)
    vix_reason = str(trigger.get("vix_reason") or "")
    trigger_reason = str(trigger.get("trigger_reason") or "")

    trades += run_broad_market_shorts(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        cooldown_bars=cooldown_bars,
        log_fn=log_fn,
        vol_score=vol_score,
        bubble_score=bubble,
        vix_reason=vix_reason,
        trigger_reason=trigger_reason,
    )
    trades += run_single_name_shorts(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        cooldown_bars=cooldown_bars,
        log_fn=log_fn,
        vol_score=vol_score,
        bubble_score=bubble,
        vix_reason=vix_reason,
        trigger_reason=trigger_reason,
    )
    from modules.sector_short_sleeve import run_sector_shorts

    trades += run_sector_shorts(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        cooldown_bars=cooldown_bars,
        log_fn=log_fn,
        vol_score=vol_score,
        bubble_score=bubble,
        vix_reason=vix_reason,
        trigger_reason=trigger_reason,
    )
    return trades


def stat_arb_short_bias_score_boost(
    data,
    regime: str,
    short_sym: str,
    base_score: float,
) -> float:
    """Boost stat-arb candidates that short weak names in bear regimes."""
    if not config.effective_opportunistic_short_enabled():
        return base_score
    if not short_regime_active(regime):
        return base_score
    mom = _momentum_score(data, short_sym)
    exhausted, _ = momentum_exhaustion(data, short_sym)
    if mom is not None and mom < 0:
        boost = 1.15
        if exhausted:
            boost = 1.22
        return base_score * boost
    return base_score


run_protective_short_strategy = run_opportunistic_short_strategy
