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


def _coerce_bar_index(value) -> int:
    """Normalize live datetime / backtest bar index for hold tracking.

    Backtests pass integer bar indices. Live cycles pass ``datetime``; convert
    those to epoch-day so SHORT_*_HOLD_BARS stays day-scale (matches daily bars).
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        try:
            return int(ts() // 86400)
        except Exception as exc:
            logger.debug("short sleeve soft-fail: %s", exc)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
    except Exception as exc:
        logger.debug("VIX level resolve failed: %s", exc)
        return None


def _vix_change_pct(data) -> float | None:
    if "VIX" not in data.columns:
        try:
            from modules.volatility_sleeve import vix_change_pct

            ts = data.index[-1] if hasattr(data, "index") and len(data.index) else None
            if ts is not None:
                return vix_change_pct(ts, lookback=5)
        except Exception as exc:
            logger.debug("VIX change lookup failed: %s", exc)
        return None
    series = data["VIX"].dropna()
    if len(series) < 6:
        return None
    cur = float(series.iloc[-1])
    prev = float(series.iloc[-6])
    if prev <= 0:
        return None
    return (cur - prev) / prev


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
    except Exception as exc:
        logger.debug("short qty lookup via broker failed for %s: %s", target, exc)
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
    except Exception as exc:
        logger.debug("short gross exposure via broker failed: %s", exc)
    return total


def _regime_size_multiplier(regime: str, bubble_score: float) -> float:
    reg = str(regime or "")
    if "RHYME_B" in reg:
        base = config.SHORT_REGIME_B_SIZE_MULT
    elif "RHYME_E" in reg:
        base = config.SHORT_REGIME_E_SIZE_MULT
    else:
        base = 0.85
    bub = float(bubble_score)
    if bub <= 1.0:
        bub_norm = max(0.0, min(1.0, bub))
    else:
        bub_norm = max(0.0, min(1.0, bub / 100.0))
    power = float(config.SHORT_BUBBLE_SIZE_POWER)
    bubble_adj = 0.70 + 0.55 * (bub_norm**power)
    mult = base * bubble_adj
    try:
        from modules.markov_regime import hmm_short_boost

        mult *= float(hmm_short_boost())
    except Exception as exc:
        logger.debug("short sleeve soft-fail: %s", exc)
    return mult


def _portfolio_constructor_short_willingness_mult() -> float:
    """Sector-aware short-willingness tilt from portfolio_constructor (1.0 = no-op)."""
    if not config.effective_portfolio_constructor_enabled():
        return 1.0
    try:
        from modules.portfolio_constructor import get_last_portfolio_decision

        decision = get_last_portfolio_decision()
        return float(decision.get("short_willingness_mult", 1.0)) if decision else 1.0
    except Exception as exc:
        logger.debug("portfolio constructor short willingness unavailable: %s", exc)
        return 1.0


def short_target_gross_pct(regime: str, bubble_score: float) -> float:
    """Dynamic gross short sleeve target between min and regime max % of equity."""
    lo = config.effective_protective_short_min_pct()
    hi = config.effective_protective_short_max_pct(regime)
    mid = (lo + hi) / 2.0
    reg = str(regime or "")
    reg_boost = 1.0 if "RHYME_B" in reg else 0.90 if "RHYME_E" in reg else 0.70
    bub = float(bubble_score)
    bub_norm = max(0.0, min(1.0, bub if bub <= 1.0 else bub / 100.0))
    tilt = (bub_norm - 0.50) / 0.50
    tilt = max(-1.0, min(1.0, tilt))
    half = (hi - lo) / 2.0
    pct = mid + half * tilt * reg_boost
    # Tilt within [lo, hi] only — willingness never overrides the sleeve's own hard bounds.
    willingness = _portfolio_constructor_short_willingness_mult()
    if willingness != 1.0:
        pct = mid + (pct - mid) * willingness
    try:
        from modules.markov_regime import hmm_short_boost

        boost = float(hmm_short_boost())
        if boost != 1.0:
            pct = mid + (pct - mid) * boost
    except Exception as exc:
        logger.debug("short sleeve soft-fail: %s", exc)
    return round(max(lo, min(hi, pct)), 4)


def _effective_short_stop_pct(
    data,
    *,
    vol_score: float | None = None,
    volatility: str | None = None,
) -> float:
    stop = float(config.SHORT_STOP_LOSS_PCT)
    vix = _resolve_vix_level(data, volatility=volatility, vol_score=vol_score)
    if vix is not None and vix >= float(config.SHORT_HIGH_VOL_VIX_THRESHOLD):
        panic = float(config.SHORT_RHYME_E_BEAR_STREAK_VIX_WAIVER)
        # Panic VIX (≥28): keep full stop — 1.5% is too tight for spike volatility.
        if vix < panic:
            stop *= float(config.SHORT_HIGH_VOL_STOP_MULT)
    return stop


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
    size_mult: float = 1.0,
    symbol: str | None = None,
    data=None,
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
    except Exception as exc:
        logger.debug("regime sizing multiplier unavailable, using 1.0: %s", exc)
        mult = 1.0
    notional *= max(0.0, float(mult))
    notional *= _regime_size_multiplier(regime, bubble_score)
    notional *= max(0.25, min(1.0, float(size_mult)))

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
        except Exception as exc:
            logger.debug("tail-risk short sizing controls skipped: %s", exc)

    notional = round(notional, 2)
    if notional < min_n:
        return None
    if config.effective_conviction_sizing_enabled():
        from modules.risk_management import compute_conviction_score, scale_notional_by_conviction

        conviction = compute_conviction_score(
            symbol,
            data,
            regime,
            sleeve="short",
            bubble_score=bubble_score,
        )
        notional = scale_notional_by_conviction(
            notional,
            equity,
            conviction,
            symbol=symbol,
            data=data,
        )
        if notional is None or notional < min_n:
            return None
    if config.effective_correlation_guard_enabled():
        from modules.risk_management import apply_correlation_guard_notional

        notional = apply_correlation_guard_notional(
            notional,
            equity,
            executor,
            data,
            symbol=symbol,
        )
        if notional is None or notional < min_n:
            return None
    if symbol and config.effective_atr_sizing_enabled():
        from modules.risk_management import atr_adjust_notional

        sizing_data = data if data is not None else getattr(executor, "_sizing_data", None)
        notional = atr_adjust_notional(notional, equity, symbol, sizing_data, sleeve_key="short")
        if notional is None or notional < min_n:
            return None
    return notional


def _weak_nyse_candidates(data, *, limit: int = 8) -> list[str]:
    symbols = config.nyse_momentum_universe(data.columns)
    scored: list[tuple[float, str]] = []
    bubble = 0.0
    if config.effective_insider_monitor_enabled():
        try:
            from modules.pipeline_strategies import evaluate_short_entry_triggers

            bubble = float(
                (evaluate_short_entry_triggers(data, "") or {}).get("bubble_score") or 0.0
            )
        except Exception as exc:
            logger.debug("bubble score for short ranking unavailable: %s", exc)
            bubble = 0.0
    for sym in symbols:
        if sym in (config.SPY_BOT_SYMBOL, config.VTI_CORE_SYMBOL):
            continue
        mom = _momentum_score(data, sym)
        if mom is None:
            continue
        score = mom
        if config.effective_insider_monitor_enabled():
            try:
                from modules.insider_monitor import short_candidate_boost

                score -= short_candidate_boost(sym, bubble)
            except Exception as exc:
                logger.debug("short candidate insider boost skipped for %s: %s", sym, exc)
        scored.append((score, sym))
    scored.sort(key=lambda x: x[0])
    base = [sym for _, sym in scored[:limit]]
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import get_short_candidate_tickers

            priority = [
                s for s in get_short_candidate_tickers() if s in symbols and s not in base[:2]
            ]
            if priority:
                merged = list(priority)
                for sym in base:
                    if sym not in merged:
                        merged.append(sym)
                return merged[: max(limit, len(priority) + 2)]
        except Exception as exc:
            logger.debug("insider short-candidate priority merge skipped: %s", exc)
    return base


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
    vol_score: float | None = None,
    volatility: str | None = None,
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
    entry_bar = _coerce_bar_index(meta.get("entry_bar"))
    held = max(0, int(bar_index) - entry_bar)
    low_water = float(meta.get("low_water") or entry)
    low_water = min(low_water, px)
    meta["low_water"] = low_water

    gain_pct = (entry - px) / entry if entry > 0 else 0.0
    loss_pct = (px - entry) / entry if entry > 0 else 0.0
    stop_pct = _effective_short_stop_pct(
        data, vol_score=vol_score, volatility=volatility
    )

    if config.SHORT_PARTIAL_PROFIT_ENABLED and not meta.get("partial_taken"):
        rr = float(config.PARTIAL_EXIT_RR if config.effective_exit_optimization_enabled() else config.SHORT_PARTIAL_PROFIT_RR)
        if config.effective_exit_optimization_enabled():
            from modules.exit_management import should_partial_exit

            stop_px = entry * (1.0 + stop_pct)
            if should_partial_exit(
                {
                    "entry_price": entry,
                    "stop_price": stop_px,
                    "partial_taken": meta.get("partial_taken"),
                    "qty": qty,
                },
                px,
                held,
                entry - stop_pct * entry * rr,
            ):
                return "partial_1r"
        elif gain_pct >= stop_pct * rr:
            return "partial_1r"

    if loss_pct >= stop_pct:
        return "stop_loss"
    if gain_pct >= config.SHORT_PROFIT_TARGET_PCT:
        return "profit_target"
    best_gain = (entry - low_water) / entry if entry > 0 else 0.0
    if config.effective_exit_optimization_enabled():
        from modules.exit_management import trailing_stop_triggered

        arm = float(config.TRAIL_ARM_PCT)
        if trailing_stop_triggered(
            entry,
            low_water,
            px,
            symbol=sym,
            atr=entry * stop_pct,
            regime=regime,
            conviction=0.5,
            side="short",
        ):
            return "trailing_profit"
    else:
        arm = config.SHORT_PROFIT_TARGET_PCT * config.SHORT_TRAILING_ARM_FRAC
        pullback = config.SHORT_TRAILING_PULLBACK_FRAC
        if best_gain >= arm and gain_pct <= best_gain * (1.0 - pullback):
            return "trailing_profit"

    max_hold = (
        int(config.EXIT_OPTIMIZATION_MAX_HOLD_BARS)
        if config.effective_exit_optimization_enabled()
        else int(config.SHORT_MAX_HOLD_BARS)
    )
    if config.effective_exit_optimization_enabled():
        from modules.exit_management import get_time_based_exit

        if get_time_based_exit(held, max_hold=max_hold):
            return "max_hold"
    elif held >= config.SHORT_MAX_HOLD_BARS:
        return "max_hold"

    move_z = _exit_move_z(data, sym)
    if held >= config.SHORT_TIME_EXIT_BARS:
        if gain_pct > 0 or move_z >= config.SHORT_EXIT_MIN_Z:
            return "time_exit"
        if move_z >= config.SHORT_EXIT_MIN_Z and gain_pct >= -stop_pct * 0.5:
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
    cover_fraction: float = 1.0,
) -> bool:
    qty = _position_qty(executor, sym)
    if qty >= 0:
        return False
    raw_prices = getattr(executor, "prices", None)
    px = 0.0
    if raw_prices is not None:
        if hasattr(raw_prices, "get"):
            val = raw_prices.get(sym)
            if val is not None:
                px = float(val)
        elif hasattr(raw_prices, "__getitem__"):
            try:
                px = float(raw_prices[sym])
            except (KeyError, TypeError, ValueError):
                px = 0.0
    if px <= 0:
        return False
    frac = max(0.05, min(1.0, float(cover_fraction)))
    notional = abs(qty) * px * frac
    order = executor.execute_order(
        sym,
        "buy",
        notional=notional,
        reduce_only=True,
        reason=f"{sym}/SHORT/{reason.upper()}",
        sleeve="SHORT",
        strategy="opportunistic_short",
        naked_cover=True,
        pair_cover=False,
        exit_reason=reason,
        entry_bar=(_short_meta(executor).get(config.normalize_symbol(sym)) or {}).get("entry_bar"),
        exit_bar=bar_index,
        partial_exit=frac < 1.0,
    )
    if not order:
        return False
    meta = _short_meta(executor).get(config.normalize_symbol(sym))
    if frac < 1.0 and meta is not None:
        meta["partial_taken"] = True
    elif frac >= 1.0:
        _short_meta(executor).pop(config.normalize_symbol(sym), None)
    notional_filled = order.get("notional", "")
    if config.effective_exit_optimization_enabled():
        from modules.exit_management import record_exit_event

        bucket = "partial" if frac < 1.0 else reason
        record_exit_event(
            bucket,
            sym,
            sleeve="SHORT",
            partial=frac < 1.0,
            notional=float(notional_filled or notional or 0),
        )
    logger.info(
        "SHORT CLOSE %s reason=%s regime=%s notional=%s hold=%s frac=%.0f%%",
        sym,
        reason,
        regime,
        notional_filled,
        (bar_index - meta.get("entry_bar")) if meta and bar_index is not None else "?",
        frac * 100.0,
    )
    if log_fn:
        log_fn(sym, "buy", regime, f"SHORT/{reason.upper()}", 0, notional_filled)
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
    executor._last_vol_score = getattr(executor, "_last_vol_score", None)
    exits = 0
    bar_index = _coerce_bar_index(now)
    vol_score = getattr(executor, "_last_vol_score", None)

    for sym in config.short_broad_symbols():
        if _position_qty(executor, sym) >= 0:
            continue
        reason = _short_exit_reason(
            sym,
            data,
            executor,
            regime,
            bar_index,
            is_broad=True,
            vol_score=vol_score,
            volatility=volatility,
        )
        if reason:
            frac = (
                config.SHORT_PARTIAL_PROFIT_FRAC
                if reason == "partial_1r"
                else 1.0
            )
            if _cover_short(
                executor,
                sym,
                reason,
                regime,
                log_fn=log_fn,
                bar_index=bar_index,
                cover_fraction=frac,
            ):
                exits += 1

    if config.effective_short_opportunistic_single_names():
        for sym in _weak_nyse_candidates(data, limit=20):
            if _position_qty(executor, sym) >= 0:
                continue
            reason = _short_exit_reason(
                sym,
                data,
                executor,
                regime,
                bar_index,
                is_broad=False,
                vol_score=vol_score,
                volatility=volatility,
            )
            if reason:
                frac = (
                    config.SHORT_PARTIAL_PROFIT_FRAC
                    if reason == "partial_1r"
                    else 1.0
                )
                if _cover_short(
                    executor,
                    sym,
                    reason,
                    regime,
                    log_fn=log_fn,
                    bar_index=bar_index,
                    cover_fraction=frac,
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
    size_mult: float = 1.0,
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
        size_mult=size_mult,
        symbol=symbol,
        data=data,
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

    bar_index = _coerce_bar_index(now)
    px = float(prices.get(symbol) or 0)
    entry_z = _exit_move_z(data, symbol)
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
        entry_z=entry_z,
        trigger_reason=trigger_reason,
    )
    from modules.pipeline_strategies import _count_if_filled

    if not _count_if_filled(executor, order):
        return 0
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import get_boost_snapshot, record_insider_boost_trade

            sym = config.normalize_symbol(symbol)
            snap = get_boost_snapshot()
            if sym in (snap.get("short_boosts") or {}):
                record_insider_boost_trade("short")
        except Exception as exc:
            logger.debug("short sleeve soft-fail: %s", exc)
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
            "entry_z": entry_z,
            "partial_taken": False,
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
    size_mult: float = 1.0,
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
            size_mult=size_mult,
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
                logger.info(scan_log)
    if not trigger.get("allowed"):
        return trades

    bubble = float(trigger.get("bubble_score") or 0.0)
    vix_reason = str(trigger.get("vix_reason") or "")
    trigger_reason = str(trigger.get("trigger_reason") or "")
    size_mult = (
        float(config.SHORT_WAIVER_SIZE_MULT)
        if trigger.get("vix_waiver_active")
        else 1.0
    )

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
        size_mult=size_mult,
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
