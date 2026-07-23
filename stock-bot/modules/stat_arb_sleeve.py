"""Statistical arbitrage sleeve — cointegration + z-score (paper aggressive only)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)
from modules.logging_utils import log_event
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.pipeline_strategies import (
    _leg_has_exposure,
    _momentum_score,
    _on_cooldown,
    execute_atomic_pair_entry,
    execute_atomic_pair_exit,
    regime_entries_paused,
)
from modules.safe_io import read_json_file, write_json_file

BOOK_FILE = Path(os.getenv("STAT_ARB_BOOK_FILE", "stat_arb_open_book.json"))


def _coerce_bar_index(value) -> int:
    """Normalize live datetime / backtest bar index for hold tracking."""
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
            logger.debug("stat arb soft-fail: %s", exc)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def stat_arb_pair_symbols(executor) -> set[str]:
    """All symbols currently registered as stat-arb pair legs."""
    syms: set[str] = set()
    book = getattr(executor, "_stat_arb_open", None)
    if book is None:
        book = _load_disk_book()
    for pos in (book or {}).values():
        for key in ("long_symbol", "short_symbol"):
            sym = pos.get(key)
            if sym:
                syms.add(config.normalize_symbol(sym))
    reg = getattr(executor, "_pair_symbols", None)
    if reg:
        syms |= {config.normalize_symbol(s) for s in reg}
    return syms


def stat_arb_sleeve_gross_value(executor, prices=None) -> float:
    """Gross market value of stat-arb pair legs (dedicated sleeve cap)."""
    pair_syms = stat_arb_pair_symbols(executor)
    if not pair_syms:
        return 0.0
    if prices is None:
        prices = getattr(executor, "prices", {})
    if hasattr(prices, "to_dict"):
        prices = prices.to_dict()

    def _price(symbol: str) -> float:
        if symbol in prices:
            return float(prices[symbol])
        norm = config.normalize_symbol(symbol)
        for key, val in prices.items():
            if config.normalize_symbol(str(key)) == norm:
                return float(val)
        return 0.0

    total = 0.0
    portfolio = getattr(executor, "portfolio", None)
    if portfolio is not None:
        for symbol, qty in portfolio.positions.items():
            sym = config.normalize_symbol(symbol)
            if sym not in pair_syms:
                continue
            px = _price(symbol)
            if px > 0:
                total += abs(float(qty) * px)
        return total
    if hasattr(executor, "_get_positions"):
        for pos in executor._get_positions():
            sym = config.normalize_symbol(pos.symbol)
            if sym not in pair_syms:
                continue
            px = float(getattr(pos, "current_price", 0) or _price(pos.symbol))
            if px > 0:
                total += abs(float(pos.qty) * px)
    return total


def stat_arb_vol_notional_scale(executor) -> float:
    """Scale stat-arb leg size when 20d portfolio vol exceeds 18% (50–70% cut)."""
    if not config.effective_stat_arb_vol_scaling_enabled():
        return 1.0
    from modules.risk_management import portfolio_vol_risk_multiplier

    hist = list(config._dynamic_risk_ctx.get("equity_history") or [])
    if len(hist) < 6 and hasattr(executor, "portfolio"):
        prices = getattr(executor, "prices", {})
        eq = float(executor.portfolio.equity(prices))
        if eq > 0:
            hist.append(eq)
    if len(hist) < 6:
        return 1.0
    return float(
        portfolio_vol_risk_multiplier(
            hist,
            ceiling=config.effective_stat_arb_vol_ceiling_pct(),
            window=config.PORTFOLIO_VOL_WINDOW,
            min_mult=float(config.STAT_ARB_VOL_MIN_NOTIONAL_SCALE),
        )
    )


def _stat_arb_cap_utilization(executor) -> float | None:
    """Fraction of dedicated stat-arb sleeve cap in use (0–1+)."""
    if not config.effective_stat_arb_sleeve_cap_enabled():
        return None
    equity_fn = getattr(executor, "_get_account", None)
    equity = None
    if equity_fn:
        try:
            equity = float(equity_fn().equity)
        except (TypeError, ValueError, AttributeError):
            equity = None
    if equity is None and hasattr(executor, "portfolio"):
        prices = getattr(executor, "prices", {})
        equity = float(executor.portfolio.equity(prices))
    if not equity or equity <= 0:
        return None
    cap_usd = equity * config.effective_stat_arb_cap()
    if cap_usd <= 0:
        return None
    return stat_arb_sleeve_gross_value(executor) / cap_usd


def _load_disk_book() -> dict:
    return read_json_file(BOOK_FILE)


def _persist_book(book: dict) -> None:
    write_json_file(BOOK_FILE, book)


def _open_book(executor) -> dict:
    book = getattr(executor, "_stat_arb_open", None)
    if book is None:
        book = _load_disk_book()
        executor._stat_arb_open = book
    return book


def _save_book(executor) -> None:
    book = getattr(executor, "_stat_arb_open", None)
    if book is not None and _is_alpaca_live_executor(executor):
        _persist_book(book)


def _is_alpaca_live_executor(executor) -> bool:
    """True only for live AlpacaExecutor (has API client), not BacktestExecutor."""
    return hasattr(executor, "client")


def _position_exclusion_reason(symbol: str, *, crypto_enabled: bool) -> str | None:
    """Classify holdings that are not stat-arb pair legs (not orphans)."""
    sym = config.normalize_symbol(symbol)
    if sym == "VTI":
        return "vti_core"
    if sym == "SPY":
        return "spy"
    if config.is_crypto(sym):
        return None if crypto_enabled else "crypto_sleeve_disabled"
    return "nyse_long_single"


def _prune_stale_pair_symbols(executor, tracked_symbols: set[str]) -> list[str]:
    """Drop pair-registry symbols with no open book legs."""
    resolved: list[str] = []
    pair_syms = getattr(executor, "_pair_symbols", None)
    if not isinstance(pair_syms, set):
        return resolved
    tracked_norm = {config.normalize_symbol(s) for s in tracked_symbols}
    for sym in list(pair_syms):
        norm = config.normalize_symbol(sym)
        if norm not in tracked_norm:
            pair_syms.discard(sym)
            resolved.append(norm)
    return resolved


def reconcile_stat_arb_book(executor) -> dict:
    """Sync in-memory/disk pair book with Alpaca positions after restart."""
    book = _open_book(executor)
    kept: list[str] = []
    removed: list[str] = []
    tracked_symbols: set[str] = set()
    crypto_enabled = config.crypto_sleeve_enabled()

    for pair_key, position in list(book.items()):
        long_sym = position.get("long_symbol")
        short_sym = position.get("short_symbol")
        if not long_sym or not short_sym:
            book.pop(pair_key, None)
            removed.append(pair_key)
            continue
        if not crypto_enabled and (
            config.is_crypto(long_sym) or config.is_crypto(short_sym)
        ):
            book.pop(pair_key, None)
            removed.append(pair_key)
            continue
        long_exp = _leg_has_exposure(executor, long_sym)
        short_exp = _leg_has_exposure(executor, short_sym)
        if not long_exp and not short_exp:
            book.pop(pair_key, None)
            removed.append(pair_key)
            continue
        kept.append(pair_key)
        tracked_symbols.add(config.normalize_symbol(long_sym))
        tracked_symbols.add(config.normalize_symbol(short_sym))
        if hasattr(executor, "register_pair_symbols"):
            executor.register_pair_symbols(long_sym, short_sym)

    ignored: dict[str, list[str]] = {}
    orphans: list[str] = []
    if hasattr(executor, "_get_positions"):
        for pos in executor._get_positions():
            sym = config.normalize_symbol(pos.symbol)
            if sym in tracked_symbols:
                continue
            if abs(float(pos.qty)) <= 1e-9:
                continue
            reason = _position_exclusion_reason(sym, crypto_enabled=crypto_enabled)
            if reason:
                ignored.setdefault(reason, []).append(sym)
            else:
                orphans.append(sym)

    resolved = _prune_stale_pair_symbols(executor, tracked_symbols)
    if not crypto_enabled:
        crypto_orphans = [s for s in orphans if config.is_crypto(s)]
        if crypto_orphans:
            logger.info(
                "reconcile_stat_arb_book ignoring crypto orphans (sleeve disabled)",
                extra={"orphans": crypto_orphans},
            )
            orphans = [s for s in orphans if not config.is_crypto(s)]

    if removed or orphans:
        _save_book(executor)
    if removed:
        logger.info("reconcile_stat_arb_book removed entries", extra={"removed": removed})
        log_event(
            "stat_arb_reconcile",
            removed_count=len(removed),
            orphans_count=len(orphans),
        )
    if orphans:
        logger.info(
            "reconcile_stat_arb_book untracked pair legs",
            extra={"orphans": orphans},
        )
    if ignored:
        logger.info("reconcile_stat_arb_book ignored non-pair holdings", extra={"ignored": ignored})
    if resolved:
        logger.info("reconcile_stat_arb_book cleared stale pair registry", extra={"resolved": resolved})

    return {
        "kept": kept,
        "removed": removed,
        # Orphans are logged for visibility but intentionally NOT surfaced here:
        # callers must never auto-close untracked legs (see test_stat_arb_reconcile).
        "orphans": [],
        "ignored": ignored,
        "resolved": resolved,
    }


def _alpaca_crypto_short_blocked(executor, short_sym: str) -> bool:
    """Alpaca spot crypto cannot open short legs for stat-arb hedges (live only)."""
    if not _is_alpaca_live_executor(executor):
        return False
    if not config.is_crypto(short_sym):
        return False
    pos = executor._find_position(short_sym)
    return pos is None


def hedge_ratio(x: pd.Series, y: pd.Series) -> float:
    xv = x.astype(float).values
    yv = y.astype(float).values
    return float(np.cov(xv, yv)[0, 1] / (np.var(xv) + 1e-9))


def cointegration_test(
    y: pd.Series,
    x: pd.Series,
    *,
    min_corr: float,
    lookback: int,
) -> tuple[bool, float]:
    """Engle-Granger cointegration via statsmodels.tsa.stattools.coint (v1.1).

    Falls back to the lightweight residual-slope test when statsmodels is
    unavailable or the ADF/coint call fails — never silently reject all pairs.
    """
    sub = pd.concat([y, x], axis=1).dropna().tail(lookback)
    if len(sub) < 30:
        return False, 1.0
    yv, xv = sub.iloc[:, 0], sub.iloc[:, 1]
    corr = float(yv.corr(xv))
    if corr < min_corr:
        return False, 1.0
    beta = hedge_ratio(xv, yv)
    if config.PAPER_STAT_ARB_USE_COINT and config.effective_stat_arb_enabled():
        try:
            from statsmodels.tsa.stattools import coint

            _score, pvalue, _crit = coint(yv, xv)
            if float(pvalue) > float(config.PAPER_STAT_ARB_COINT_PVALUE):
                return False, beta
            return True, beta
        except Exception as exc:
            logger.debug(
                "coint test unavailable/failed — Engle-Granger fallback: %s", exc
            )
            return engle_granger_cointegrated(
                y, x, min_corr=min_corr, lookback=lookback
            )
    return engle_granger_cointegrated(y, x, min_corr=min_corr, lookback=lookback)


def engle_granger_cointegrated(
    y: pd.Series,
    x: pd.Series,
    *,
    min_corr: float,
    lookback: int,
    coint_slope: float | None = None,
) -> tuple[bool, float]:
    """Lightweight Engle-Granger: OLS hedge ratio + mean-reverting residual slope."""
    slope_cut = coint_slope if coint_slope is not None else config.effective_pair_coint_slope()
    sub = pd.concat([y, x], axis=1).dropna().tail(lookback)
    if len(sub) < 30:
        return False, 1.0
    yv, xv = sub.iloc[:, 0], sub.iloc[:, 1]
    corr = float(yv.corr(xv))
    if corr < min_corr:
        return False, 1.0
    beta = hedge_ratio(xv, yv)
    spread = yv - beta * xv
    s_lag = spread.iloc[:-1].values
    delta = spread.diff().iloc[1:].values
    if len(s_lag) < 15:
        return False, beta
    slope = float(np.cov(s_lag, delta)[0, 1] / (np.var(s_lag) + 1e-9))
    return slope < slope_cut, beta


def spread_zscore(y: pd.Series, x: pd.Series, beta: float, *, lookback: int) -> float:
    sub_y = y.tail(lookback)
    sub_x = x.tail(lookback)
    spread = sub_y - beta * sub_x
    return float((spread.iloc[-1] - spread.mean()) / (spread.std() + 1e-9))


def _z_reverted_toward_zero(entry_z: float, z: float) -> float:
    """Positive when z moved toward 0 from entry_z."""
    if entry_z > 0:
        return entry_z - z
    if entry_z < 0:
        return z - entry_z
    return 0.0


def _no_room_reject_rate(executor) -> float | None:
    att = getattr(executor, "_attribution", None)
    if att is None:
        return None
    rejects = getattr(att, "stat_arb_rejects", None) or {}
    no_room = int(rejects.get("no_room", 0))
    intents = int(getattr(att, "intents", {}).get("stat_arb", 0))
    attempts = no_room + intents
    if attempts < 3:
        return None
    return no_room / attempts


def _dynamic_equity_max_pairs(executor) -> int:
    """Base cap (8); expand toward 12 when sleeve room is plentiful (low no_room rate)."""
    base = config.effective_stat_arb_max_pairs()
    rate = _no_room_reject_rate(executor)
    if rate is None:
        return base
    if rate < 0.15:
        return config.effective_stat_arb_max_pairs_ceiling()
    if rate < 0.30:
        return config.effective_stat_arb_max_pairs_expanded()
    return base


def _symbol_liquidity_ok(symbol: str, data: pd.DataFrame | None = None) -> bool:
    """Skip illiquid names for stat-arb legs (screener $vol or core-universe fallback)."""
    sym = config.normalize_symbol(symbol)
    min_adv = config.effective_stat_arb_min_dollar_volume()
    min_px = float(getattr(config, "PAPER_UNIVERSE_MIN_PRICE", 5) or 5)
    try:
        from modules.dynamic_universe import load_screener_ticker_meta

        meta = load_screener_ticker_meta().get(sym, {})
    except ImportError:
        meta = {}
    adv = float(
        meta.get("avg_dollar_volume")
        or meta.get("dollar_volume")
        or meta.get("avg_dollar_vol")
        or 0
    )
    if adv >= min_adv:
        return True

    prices = None
    if data is not None and sym in getattr(data, "columns", []):
        prices = data[sym].dropna().tail(20)
        if len(prices) >= 5 and float(prices.iloc[-1]) < min_px:
            return False

    # Core static universe: allow when screener ADV is missing (offline / sparse meta).
    # No price-history fallback — stronger liquidity gate for quality tune.
    if adv <= 0 and sym in config.UNIVERSE:
        return True

    return False


def _leg_volatility_ok(symbol: str, data, *, lookback: int, max_vol: float) -> bool:
    """Skip a leg whose recent daily-return std exceeds *max_vol* (0 disables)."""
    if max_vol <= 0 or data is None or symbol not in getattr(data, "columns", []):
        return True
    prices = data[symbol].dropna().tail(max(20, lookback))
    if len(prices) < 20:
        return True  # insufficient history — don't reject on unknown vol
    rets = prices.pct_change().dropna()
    if rets.empty:
        return True
    return float(rets.std()) <= max_vol


def _equity_exit_decision(
    pos: dict,
    z: float,
    *,
    now: int | None,
) -> tuple[bool, str, bool]:
    """Return (should_exit, reason, force_close) for NYSE stat-arb pairs."""
    entry_z = float(pos.get("entry_z", 0.0))
    z_exit = config.effective_stat_arb_z_exit()
    profit_delta = config.effective_stat_arb_profit_z_delta()
    stop_delta = profit_delta / config.effective_stat_arb_risk_reward()
    min_revert = max(
        profit_delta * float(config.PAPER_STAT_ARB_MIN_REVERT_FRAC),
        abs(entry_z) * 0.35,
    )

    favorable = _z_reverted_toward_zero(entry_z, z)
    best_fav = max(float(pos.get("best_favorable", 0.0)), favorable)
    pos["best_favorable"] = best_fav
    # Quality: arm trail at 45% of profit-z; exit on 30% pullback from best.
    profit_gate = profit_delta * config.effective_stat_arb_trail_min_profit_frac()
    trail_arm = max(profit_delta * config.effective_stat_arb_trailing_arm_frac(), profit_gate)
    trail_pull = config.effective_stat_arb_trailing_pullback_frac()
    if config.effective_exit_optimization_enabled():
        from modules.exit_management import record_exit_event

        if (
            config.effective_stat_arb_partial_exit_enabled()
            and not pos.get("partial_taken")
        ):
            # Partial at N:1 vs stop distance (not full profit target).
            partial_rr = config.effective_stat_arb_partial_exit_rr()
            if favorable >= stop_delta * partial_rr:
                pos["partial_taken"] = True
                record_exit_event("partial", pos.get("pair_key", ""), sleeve="stat_arb", partial=True)
                return True, "partial_1r", False
    if best_fav >= trail_arm and favorable < best_fav * (1.0 - trail_pull):
        if config.effective_exit_optimization_enabled():
            from modules.exit_management import record_exit_event

            record_exit_event("trail", pos.get("pair_key", ""), sleeve="stat_arb")
        return True, "trailing_stop", False

    if entry_z:
        if entry_z > 0:
            if z <= entry_z - profit_delta:
                return True, "profit_target", False
            if z >= entry_z + stop_delta:
                return True, "stop_loss", True
        elif entry_z < 0:
            if z >= entry_z + profit_delta:
                return True, "profit_target", False
            if z <= entry_z - stop_delta:
                return True, "stop_loss", True

    reverted = _z_reverted_toward_zero(entry_z, z)
    if abs(z) <= z_exit and reverted >= min_revert:
        return True, "mean_revert", False

    if now is not None:
        entry_bar = pos.get("entry_bar")
        if entry_bar is not None:
            held = _coerce_bar_index(now) - _coerce_bar_index(entry_bar)
            # Soft time exit — close aging pairs with partial reversion
            # before max hold / EOD force-close bleed.
            soft_hold = max(15, config.effective_stat_arb_equity_max_hold_bars() - 5)
            if held >= soft_hold:
                reverted_now = _z_reverted_toward_zero(entry_z, z)
                if reverted_now >= profit_delta * 0.35 and abs(z) <= abs(entry_z) * 0.90:
                    return True, "time_soft", False
            # Equity pairs: dedicated max hold (aligned with shared 35-bar fill-rate default).
            max_hold = config.effective_stat_arb_equity_max_hold_bars()
            if config.effective_exit_optimization_enabled():
                from modules.exit_management import get_time_based_exit, record_exit_event

                if get_time_based_exit(held, max_hold=max_hold):
                    record_exit_event("time", pos.get("pair_key", ""), sleeve="stat_arb")
                    return True, "max_hold", True
            elif held >= max_hold:
                return True, "max_hold", True

    return False, "", False


def _sleeve_crowding_scale(executor) -> float:
    """Shrink leg size when stat-arb cap is crowded or its own no_room rejects are high."""
    if config.paper_deploy_aggressive():
        return 1.0
    if not config.effective_stat_arb_sleeve_cap_enabled():
        att = getattr(executor, "_attribution", None)
        if att is None:
            return 1.0
        rejects = getattr(att, "stat_arb_rejects", None) or {}
        no_room = int(rejects.get("no_room", 0))
        if no_room < 3:
            return 1.0
        intents = int(getattr(att, "intents", {}).get("stat_arb", 0))
        denom = max(no_room + intents, 1)
        rate = no_room / denom
        if no_room >= 15 or rate >= 0.45:
            return 0.55
        if no_room >= 8 or rate >= 0.30:
            return 0.70
        return 0.85

    util = _stat_arb_cap_utilization(executor)
    att = getattr(executor, "_attribution", None)
    rejects = getattr(att, "stat_arb_rejects", None) or {} if att else {}
    cap_full = int(rejects.get("cap_full", 0))
    no_room = int(rejects.get("no_room", 0))

    scale = 1.0
    if util is not None:
        if util >= 0.95 or cap_full >= 3:
            scale = min(scale, 0.55)
        elif util >= 0.85:
            scale = min(scale, 0.70)
        elif util >= 0.70:
            scale = min(scale, 0.85)
    if no_room >= 5:
        intents = int(getattr(att, "intents", {}).get("stat_arb", 0)) if att else 0
        rate = no_room / max(no_room + intents, 1)
        if rate >= 0.40:
            scale = min(scale, 0.65)
        elif rate >= 0.25:
            scale = min(scale, 0.80)
    return scale


def pair_leg_notional(
    executor,
    total_notional=None,
    *,
    sleeve_attempted: bool = False,
    is_crypto: bool = False,
    long_symbol: str | None = None,
    data=None,
    z_score: float | None = None,
    regime: str | None = None,
) -> tuple[float | None, float | None]:
    """Half notional per leg, scaled by dynamic risk."""
    equity_fn = getattr(executor, "_get_account", None)
    equity = None
    if equity_fn:
        try:
            equity = float(equity_fn().equity)
        except (TypeError, ValueError, AttributeError):
            equity = None
    if total_notional is None:
        if sleeve_attempted:
            return None, None
        if equity_fn and equity is not None:
            total_notional = round(equity * config.effective_risk_per_trade(equity), 2)
    if total_notional is None:
        return None, None
    if is_crypto:
        reg_mult = config.effective_crypto_regime_sizing_mult(
            getattr(executor, "_last_regime", None)
        )
        if reg_mult <= 0:
            return None, None
        total_notional = config.scale_crypto_pair_notional(total_notional, equity)
        if total_notional is None:
            return None, None
        if reg_mult < 0.999:
            total_notional = round(float(total_notional) * reg_mult, 2)
            if total_notional < config.effective_crypto_min_notional(equity) * 2:
                return None, None
    leg = round(float(total_notional) / 2, 2)
    if is_crypto:
        min_n = config.effective_crypto_min_notional(equity)
    else:
        min_n = config.effective_min_notional(equity)
    if leg < min_n:
        return None, None
    pod_scale = float(getattr(executor, "pod_risk_scale", lambda _p: 1.0)("stat_arb"))
    if pod_scale <= config.POD_PAUSE_SCALE + 0.05:
        return None, None
    leg = round(leg * pod_scale, 2)
    if not is_crypto:
        vol_scale = stat_arb_vol_notional_scale(executor)
        if vol_scale < 0.999:
            leg = round(leg * vol_scale, 2)
        crowd = _sleeve_crowding_scale(executor)
        if crowd < 0.999:
            leg = round(leg * crowd, 2)
        sizing_data = data if data is not None else getattr(executor, "_sizing_data", None)
        if long_symbol and equity is not None:
            from modules.risk_management import atr_adjust_notional

            leg = atr_adjust_notional(leg, equity, long_symbol, sizing_data, sleeve_key="stat_arb")
    if leg is not None and config.effective_conviction_sizing_enabled() and equity is not None:
        from modules.risk_management import compute_conviction_score, scale_notional_by_conviction

        sleeve = "stat_arb_crypto" if is_crypto else "stat_arb_equity"
        conviction = compute_conviction_score(
            long_symbol,
            sizing_data if sizing_data is not None else data,
            regime or getattr(executor, "_last_regime", None),
            sleeve=sleeve,
            z_score=z_score,
        )
        leg = scale_notional_by_conviction(
            leg,
            equity,
            conviction,
            symbol=long_symbol,
            data=sizing_data if sizing_data is not None else data,
            scale_band=(
                None if is_crypto else config.effective_stat_arb_conviction_scale_band()
            ),
            sleeve="STAT_ARB",
            strategy_id="stat_arb",
        )
    # Markov × time-of-day Stat Arb soft boost (paper research)
    try:
        from modules.markov_regime import hmm_stat_arb_boost

        sa_boost = float(hmm_stat_arb_boost())
        if leg is not None and abs(sa_boost - 1.0) > 1e-6:
            leg = round(float(leg) * sa_boost, 2)
    except Exception as exc:
        logger.debug("stat arb soft-fail: %s", exc)
    if leg is not None and config.effective_correlation_guard_enabled() and equity is not None:
        from modules.risk_management import apply_correlation_guard_notional

        leg = apply_correlation_guard_notional(
            leg,
            equity,
            executor,
            sizing_data if sizing_data is not None else data,
            symbol=long_symbol,
        )
    if leg is None or leg < min_n:
        return None, None
    return leg, leg


def _execute_entry(executor, intent, *, log_fn=None, regime: str = "", now: int | None = None) -> int:
    long_sym = intent["long_symbol"]
    short_sym = intent["short_symbol"]
    z = intent["z_score"]
    pair_key = intent["pair_key"]
    book = _open_book(executor)
    if pair_key in book:
        if intent.get("phase") == "stat_arb_crypto":
            att = getattr(executor, "_attribution", None)
            if att:
                att.record_crypto_reject("in_book")
        else:
            _stat_arb_reject(executor, "in_book")
        return 0
    if _alpaca_crypto_short_blocked(executor, short_sym):
        _stat_arb_reject(executor, "crypto_short_blocked")
        return 0
    leg_n, _ = pair_leg_notional(
        executor,
        intent.get("notional"),
        sleeve_attempted="notional" in intent,
        is_crypto=intent.get("phase") == "stat_arb_crypto",
        long_symbol=long_sym,
        data=getattr(executor, "_sizing_data", None),
        z_score=intent.get("z_score"),
        regime=regime,
    )
    if leg_n is None:
        if intent.get("phase") == "stat_arb_crypto":
            att = getattr(executor, "_attribution", None)
            if att:
                att.record_crypto_reject("min_notional")
        else:
            _stat_arb_reject(executor, "leg_notional")
        return 0

    leg_strategy = "crypto" if intent.get("phase") == "stat_arb_crypto" else "stat_arb"
    ok, long_fill_n, short_fill_n = execute_atomic_pair_entry(
        executor, long_sym, short_sym, leg_n, pair_key=pair_key, strategy=leg_strategy
    )
    if not ok:
        if intent.get("phase") == "stat_arb_crypto":
            att = getattr(executor, "_attribution", None)
            if att:
                att.record_crypto_reject("atomic_fail")
        else:
            _stat_arb_reject(executor, "atomic_fail")
        return 0

    book = _open_book(executor)
    book[pair_key] = {
        "long_symbol": long_sym,
        "short_symbol": short_sym,
        "y_symbol": intent.get("y_symbol", long_sym),
        "x_symbol": intent.get("x_symbol", short_sym),
        "beta": intent.get("beta", 1.0),
        "leg_notional": leg_n,
        "long_filled_notional": long_fill_n or leg_n,
        "short_filled_notional": short_fill_n or leg_n,
        "entry_z": z,
        "entry_bar": _coerce_bar_index(intent.get("entry_bar", now)),
        "entry_regime": regime,
    }
    _save_book(executor)
    if hasattr(executor, "register_pair_symbols"):
        executor.register_pair_symbols(long_sym, short_sym)
    att = getattr(executor, "_attribution", None)
    if att:
        if intent.get("phase") == "stat_arb_crypto":
            att.on_crypto_entry(pair_key, symbol=long_sym, regime=regime, entry_bar=now)
        else:
            att.on_stat_arb_pair_entry(
                pair_key, long_sym, short_sym, entry_z=float(z)
            )
    if config.effective_insider_signal_boost_enabled():
        try:
            from modules.insider_signal_handler import get_boost_snapshot, record_insider_boost_trade

            snap = get_boost_snapshot()
            if float((snap.get("stat_arb_boosts") or {}).get(long_sym, 1.0)) > 1.0:
                record_insider_boost_trade("stat_arb")
        except Exception as exc:
            logger.debug("stat arb soft-fail: %s", exc)

    msg = (
        f"Stat arb: LONG {long_sym} / SHORT {short_sym}, "
        f"Z={round(z, 1)} (cointegrated)"
    )
    if log_fn:
        log_fn(long_sym, "buy", regime, pair_key, z, leg_n, pair_msg=msg)
        log_fn(short_sym, "sell", regime, pair_key, z, leg_n, pair_msg=msg)
    logger.info("stat arb entry executed", extra={"pair": pair_key, "long": long_sym, "short": short_sym, "z": round(z,1)})
    log_event("stat_arb_entry", pair=pair_key, long_sym=long_sym, short_sym=short_sym, z_score=round(z, 1))
    return 1


def _close_position(
    executor,
    pair_key: str,
    position: dict,
    *,
    log_fn=None,
    regime: str = "",
    now: int | None = None,
    force: bool = False,
) -> int:
    long_sym = position["long_symbol"]
    short_sym = position["short_symbol"]
    leg_n = position.get("leg_notional")
    if (
        not force
        and position.get("exit_reason") == "partial_1r"
        and config.effective_exit_optimization_enabled()
    ):
        from modules.exit_management import partial_exit_fraction

        reduce = round(float(leg_n or 0) * partial_exit_fraction(), 2)
        if reduce > 0:
            executor.execute_reduce_notional(
                long_sym, reduce, reason="partial_1r", sleeve="stat_arb"
            )
            executor.execute_reduce_notional(
                short_sym, reduce, reason="partial_1r", sleeve="stat_arb"
            )
            position["leg_notional"] = round(max(0.0, float(leg_n or 0) - reduce), 2)
        return 1
    att = getattr(executor, "_attribution", None)
    mark_pnl = None
    is_crypto = _is_crypto_pair_position(position)
    if att:
        mark_pnl = att.pair_mark_pnl(executor, position, getattr(executor, "prices", {}))
    exited = execute_atomic_pair_exit(
        executor, long_sym, short_sym, pair_key=pair_key
    )
    if not exited:
        if not force:
            return 0
        leg_strat = "crypto" if is_crypto else "stat_arb"
        for sym in (long_sym, short_sym):
            if _leg_has_exposure(executor, sym):
                executor.execute_full_exit(
                    sym, strategy=leg_strat, pair_key=pair_key
                )
        if att:
            mark_pnl = att.pair_mark_pnl(executor, position, getattr(executor, "prices", {}))

    book = _open_book(executor)
    book.pop(pair_key, None)
    _save_book(executor)
    if att:
        if mark_pnl is not None:
            if is_crypto:
                att.record_crypto_pair_realized(
                    mark_pnl,
                    pair_key,
                    leg_notional=float(leg_n or 0),
                    regime=position.get("entry_regime") or regime,
                    entry_bar=position.get("entry_bar"),
                    exit_bar=now,
                )
            else:
                att.record_stat_arb_realized(
                    mark_pnl,
                    pair_key,
                    leg_notional=float(leg_n or 0),
                    exit_reason=position.get("exit_reason", ""),
                    entry_bar=position.get("entry_bar"),
                    exit_bar=now,
                )
        elif is_crypto and force:
            att.record_crypto_pair_realized(
                0.0,
                pair_key,
                leg_notional=float(leg_n or 0),
                regime=position.get("entry_regime") or regime,
                entry_bar=position.get("entry_bar"),
                exit_bar=now,
            )
        elif not is_crypto and force and mark_pnl is None:
            att.record_stat_arb_realized(
                0.0,
                pair_key,
                leg_notional=float(leg_n or 0),
                exit_reason=position.get("exit_reason", "force_close"),
                entry_bar=position.get("entry_bar"),
                exit_bar=now,
            )
        if is_crypto:
            att.on_crypto_pair_exit(pair_key)
        else:
            att.on_stat_arb_pair_exit(pair_key, long_sym=long_sym, short_sym=short_sym)
    if log_fn:
        log_fn(
            long_sym,
            "exit",
            regime,
            pair_key,
            0.0,
            leg_n or "",
            pair_msg=f"Stat arb exit: {pair_key} (Z mean-reverted)",
        )
    logger.info("stat arb exit executed", extra={"pair": pair_key, "long": long_sym, "short": short_sym})
    log_event("stat_arb_exit", pair=pair_key, long_sym=long_sym, short_sym=short_sym)
    return 1


def _is_crypto_pair_position(position: dict) -> bool:
    long_sym = position.get("long_symbol") or position.get("y_symbol") or ""
    return config.is_crypto(long_sym)


def _pair_has_exposure(executor, position: dict) -> bool:
    long_sym = position.get("long_symbol")
    short_sym = position.get("short_symbol")
    if not long_sym or not short_sym:
        return False
    return _leg_has_exposure(executor, long_sym) or _leg_has_exposure(executor, short_sym)


def _prune_ghost_book_entries(executor, *, crypto_only: bool = False) -> int:
    """Drop book rows with no live legs (stale disk / partial unwind)."""
    book = _open_book(executor)
    removed = 0
    for pair_key, pos in list(book.items()):
        if crypto_only and not _is_crypto_pair_position(pos):
            continue
        if not _pair_has_exposure(executor, pos):
            book.pop(pair_key, None)
            removed += 1
    if removed:
        _save_book(executor)
    return removed


def _open_crypto_pair_keys(executor) -> set[str]:
    book = _open_book(executor)
    return {
        k
        for k, p in book.items()
        if _is_crypto_pair_position(p) and _pair_has_exposure(executor, p)
    }


def _crypto_pair_slots(executor) -> int:
    cap = config.effective_crypto_max_pairs()
    open_n = len(_open_crypto_pair_keys(executor))
    return max(0, cap - open_n)


def process_exits(
    data,
    executor,
    *,
    log_fn=None,
    regime: str = "",
    lookback: int | None = None,
    now: int | None = None,
) -> int:
    """Close cointegrated pairs when |Z| falls to exit threshold."""
    book = _open_book(executor)
    if not book:
        return 0
    lb = lookback or config.STAT_ARB_LOOKBACK
    exits = 0
    for pair_key, pos in list(book.items()):
        y_sym = pos.get("y_symbol", pos["long_symbol"])
        x_sym = pos.get("x_symbol", pos["short_symbol"])
        if y_sym not in data.columns or x_sym not in data.columns:
            continue
        beta = float(pos.get("beta", 1.0))
        z = spread_zscore(data[y_sym], data[x_sym], beta, lookback=lb)
        is_crypto = _is_crypto_pair_position(pos)
        should_exit = False
        exit_reason = ""
        force_close = False
        if is_crypto:
            z_exit = config.effective_crypto_z_exit()
            should_exit = abs(z) <= z_exit
            exit_reason = "mean_revert" if should_exit else ""
            if not should_exit and now is not None:
                entry_bar = pos.get("entry_bar")
                if entry_bar is not None:
                    held = _coerce_bar_index(now) - _coerce_bar_index(entry_bar)
                    if held >= config.effective_crypto_max_hold_bars():
                        should_exit = True
                        exit_reason = "max_hold"
                        force_close = True
        else:
            should_exit, exit_reason, force_close = _equity_exit_decision(
                pos, z, now=now
            )
        if should_exit:
            if exit_reason and not is_crypto:
                pos["exit_reason"] = exit_reason
            exits += _close_position(
                executor,
                pair_key,
                pos,
                log_fn=log_fn,
                regime=regime,
                now=now,
                force=force_close,
            )
    return exits


def force_close_all_pairs(executor, *, regime: str = "", now: int | None = None) -> int:
    """Close every open pair in the book (end-of-backtest cleanup).

    Positions are closed at the executor's current ``prices``; no price frame is
    needed here (the caller sets ``executor.prices`` before invoking).
    """
    book = _open_book(executor)
    closed = 0
    for pair_key, pos in list(book.items()):
        if not pos.get("exit_reason"):
            pos["exit_reason"] = "force_close"
        closed += _close_position(
            executor,
            pair_key,
            pos,
            log_fn=None,
            regime=regime,
            now=now,
            force=True,
        )
    return closed


def _stat_arb_reject(executor, reason: str) -> None:
    att = getattr(executor, "_attribution", None)
    if att and hasattr(att, "record_stat_arb_reject"):
        att.record_stat_arb_reject(reason)


def _symbols_in_use(executor) -> set[str]:
    used: set[str] = set()
    book = getattr(executor, "_stat_arb_open", None) or _load_disk_book()
    for pos in book.values():
        for key in ("long_symbol", "short_symbol"):
            sym = pos.get(key)
            if sym:
                used.add(config.normalize_symbol(sym))
    if hasattr(executor, "_get_positions"):
        for pos in executor._get_positions():
            sym = config.normalize_symbol(pos.symbol)
            if sym in ("VTI", config.SPY_BOT_SYMBOL) or config.is_crypto(sym):
                continue
            if abs(float(pos.qty)) > 1e-9:
                used.add(sym)
    return used


def _nyse_momentum_symbols(executor) -> set[str]:
    """Single-leg NYSE longs held by MA50 momentum (exclude stat-arb pair legs)."""
    pair_legs: set[str] = set()
    book = getattr(executor, "_stat_arb_open", None) or _load_disk_book()
    for pos in book.values():
        if _is_crypto_pair_position(pos):
            continue
        for key in ("long_symbol", "short_symbol"):
            sym = pos.get(key)
            if sym:
                pair_legs.add(config.normalize_symbol(sym))
    held: set[str] = set()
    if not hasattr(executor, "_get_positions"):
        return held
    for pos in executor._get_positions():
        sym = config.normalize_symbol(pos.symbol)
        if sym in ("VTI", config.SPY_BOT_SYMBOL) or config.is_crypto(sym):
            continue
        if sym in pair_legs:
            continue
        if float(pos.qty) > 1e-9:
            held.add(sym)
    return held


def _equity_exclude_symbols(executor, data=None, regime=None, yield_gated: bool = False) -> set[str]:
    """Symbols blocked for stat-arb pair legs (in use + NYSE momentum overlap)."""
    blocked = _symbols_in_use(executor) if executor is not None else set()
    if executor is not None:
        blocked |= _nyse_momentum_symbols(executor)
    if data is not None:
        try:
            from modules.pipeline_strategies import _equity_momentum_ranked, _nyse_equity_columns

            cols = _nyse_equity_columns(data)
            ranked = _equity_momentum_ranked(
                data, cols, yield_gated=yield_gated, regime=regime or ""
            )
            block_n = config.effective_stat_arb_max_trades()
            if config.effective_stat_arb_sleeve_cap_enabled():
                block_n = max(1, min(block_n, 2))
            overlap_mult = max(1, int(getattr(config, "STAT_ARB_NYSE_OVERLAP_BLOCK_MULT", 2)))
            for sym in ranked[: block_n * overlap_mult]:
                blocked.add(config.normalize_symbol(sym))
        except Exception as exc:
            logger.debug("NYSE overlap exclude scan failed: %s", exc)
    return blocked


def _open_equity_pair_keys(executor) -> set[str]:
    book = _open_book(executor)
    return {
        k
        for k, p in book.items()
        if not _is_crypto_pair_position(p) and _pair_has_exposure(executor, p)
    }


def _equity_pair_slots(executor) -> int:
    cap = _dynamic_equity_max_pairs(executor)
    open_n = len(_open_equity_pair_keys(executor))
    return max(0, cap - open_n)


def _scan_pair_candidates(
    data,
    symbols: list[str],
    *,
    lookback: int,
    min_corr: float,
    z_entry: float,
    momentum_pick: bool = False,
    exclude_symbols: set[str] | None = None,
    regime: str = "",
    max_leg_vol: float = 0.0,
    reject_counter: dict[str, int] | None = None,
    near_miss: list[tuple] | None = None,
) -> list[tuple[float, float, str, str, float, str, str, float]]:
    """Return sorted (score, z, long, short, beta, y_sym, x_sym, corr) candidates.

    When ``reject_counter`` is provided it is populated with per-reason drop
    counts (illiquid/blocked/short_history/low_corr/coint_fail/low_z/pairs_seen)
    for funnel diagnostics. ``near_miss`` collects sample pairs that passed
    correlation but failed cointegration/z so we can inspect what is close.
    """

    def _bump(reason: str) -> None:
        if reject_counter is not None:
            reject_counter[reason] = reject_counter.get(reason, 0) + 1

    blocked = exclude_symbols or set()
    out: list[tuple[float, float, str, str, float, str, str, float]] = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            t1, t2 = symbols[i], symbols[j]
            _bump("pairs_seen")
            if not _symbol_liquidity_ok(t1, data) or not _symbol_liquidity_ok(t2, data):
                _bump("illiquid")
                continue
            if config.normalize_symbol(t1) in blocked or config.normalize_symbol(t2) in blocked:
                _bump("blocked")
                continue
            if max_leg_vol > 0 and (
                not _leg_volatility_ok(t1, data, lookback=lookback, max_vol=max_leg_vol)
                or not _leg_volatility_ok(t2, data, lookback=lookback, max_vol=max_leg_vol)
            ):
                _bump("high_vol")
                continue
            pair = data[[t1, t2]].dropna().tail(lookback)
            if len(pair) < 30:
                _bump("short_history")
                continue
            corr = float(pair[t1].corr(pair[t2]))
            if corr < min_corr:
                _bump("low_corr")
                continue
            ok, beta = cointegration_test(
                data[t1], data[t2], min_corr=min_corr, lookback=lookback
            )
            if not ok:
                ok, beta = cointegration_test(
                    data[t2], data[t1], min_corr=min_corr, lookback=lookback
                )
                if not ok:
                    _bump("coint_fail")
                    if near_miss is not None and len(near_miss) < 8:
                        near_miss.append((round(corr, 3), t1, t2, "coint_fail"))
                    continue
                y_sym, x_sym = t2, t1
            else:
                y_sym, x_sym = t1, t2
            z = spread_zscore(data[y_sym], data[x_sym], beta, lookback=lookback)
            if abs(z) < z_entry:
                _bump("low_z")
                if near_miss is not None and len(near_miss) < 8:
                    near_miss.append((round(corr, 3), y_sym, x_sym, f"z={round(z, 2)}"))
                continue
            if momentum_pick:
                mom1 = _momentum_score(data, t1)
                mom2 = _momentum_score(data, t2)
                if mom1 is not None and mom2 is not None:
                    long_sym, short_sym = (t1, t2) if mom1 >= mom2 else (t2, t1)
                else:
                    long_sym = x_sym if z > 0 else y_sym
                    short_sym = y_sym if z > 0 else x_sym
            else:
                long_sym = x_sym if z > 0 else y_sym
                short_sym = y_sym if z > 0 else x_sym
            if config.normalize_symbol(long_sym) in blocked or config.normalize_symbol(
                short_sym
            ) in blocked:
                _bump("blocked_dir")
                continue
            _bump("kept")
            score = abs(z) * corr
            if regime:
                from modules.opportunistic_short_sleeve import stat_arb_short_bias_score_boost

                score = stat_arb_short_bias_score_boost(data, regime, short_sym, score)
            if config.effective_insider_monitor_enabled():
                try:
                    from modules.insider_monitor import stat_arb_long_boost

                    score *= stat_arb_long_boost(long_sym)
                except Exception as exc:
                    logger.debug("insider stat-arb boost skipped for %s: %s", long_sym, exc)
            if config.effective_rvol_scanner_enabled():
                try:
                    from modules.volume_analysis import stat_arb_long_rvol_mult

                    score *= stat_arb_long_rvol_mult(long_sym, data)
                except Exception as exc:
                    logger.debug("RVOL stat-arb boost skipped for %s: %s", long_sym, exc)
            if config.effective_catalyst_scoring_enabled():
                try:
                    from modules.catalyst_scoring import catalyst_stat_arb_long_mult

                    score *= catalyst_stat_arb_long_mult(long_sym, data)
                except Exception as exc:
                    logger.debug("catalyst stat-arb boost skipped for %s: %s", long_sym, exc)
            if config.PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF:
                try:
                    from modules.dynamic_universe import sector_for_symbol

                    s1 = sector_for_symbol(t1)
                    s2 = sector_for_symbol(t2)
                    if s1 and s2 and s1 != s2 and s1 != "Other" and s2 != "Other":
                        score *= float(config.PAPER_STAT_ARB_SECTOR_NEUTRAL_BOOST)
                except Exception as exc:
                    logger.debug("sector-neutral boost skipped: %s", exc)
            out.append((score, z, long_sym, short_sym, beta, y_sym, x_sym, corr))
    out.sort(reverse=True)
    return out


def crypto_stat_arb_intents(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    max_trades: int | None = None,
    volatility=None,
    spacex_snapshot=None,
    notional=None,
    vol_gate_checked: bool = False,
    executor=None,
):
    if not config.effective_stat_arb_enabled():
        return []
    crypto_cols = [c for c in data.columns if config.is_crypto(c)]
    if len(crypto_cols) < 2:
        return []

    try:
        from modules.crypto_universe import crypto_trading_columns

        tradable = crypto_trading_columns(data)
        if tradable:
            crypto_cols = [c for c in crypto_cols if c in set(tradable)]
    except Exception as exc:
        logger.debug("crypto_trading_columns filter skipped: %s", exc)

    if len(crypto_cols) < 2:
        return []

    open_keys: set[str] = set()
    slots = config.effective_crypto_max_pairs()
    if executor is not None:
        open_keys = _open_crypto_pair_keys(executor)
        slots = _crypto_pair_slots(executor)

    trade_cap = max_trades if max_trades is not None else config.effective_crypto_max_trades_per_cycle()
    if slots <= 0:
        trade_cap = 0
    else:
        trade_cap = min(trade_cap, slots)

    if not vol_gate_checked:
        gate = crypto_trading_allowed(
            volatility or "Low",
            regime,
            spacex_snapshot=spacex_snapshot,
            data=data,
        )
        if not gate["allowed"]:
            return []

    candidates = _scan_pair_candidates(
        data,
        crypto_cols,
        lookback=config.STAT_ARB_LOOKBACK,
        min_corr=config.effective_pair_min_correlation(),
        z_entry=config.effective_crypto_z_entry(
            volatility=volatility, regime=regime
        ),
        momentum_pick=False,
    )
    intents = []
    fired: set[str] = set()
    for _score, z, long_sym, short_sym, beta, y_sym, x_sym, _corr in candidates:
        if len(intents) >= trade_cap:
            break
        if long_sym in fired or short_sym in fired:
            continue
        pair_key = f"{long_sym}/{short_sym}"
        if pair_key in open_keys:
            continue
        if _on_cooldown(pair_cooldown, pair_key, now, cooldown_bars=cooldown_bars):
            continue
        intents.append(
            {
                "long_symbol": long_sym,
                "short_symbol": short_sym,
                "y_symbol": y_sym,
                "x_symbol": x_sym,
                "pair_key": pair_key,
                "z_score": z,
                "beta": beta,
                "notional": notional,
                "phase": "stat_arb_crypto",
            }
        )
        fired.add(long_sym)
        fired.add(short_sym)
    return intents


_stat_arb_universe_logged = False


def _nyse_stat_arb_columns(data) -> list[str]:
    """Pair pool for stat arb: eligible NYSE/dynamic names WITHOUT the RVOL
    momentum filter.

    Stat arb needs a broad, stable universe to find cointegrated pairs. The
    momentum sleeve's RVOL screen (``_nyse_equity_columns``) drops any name whose
    relative volume is below threshold, which in practice collapses the pool to a
    handful of high-RVOL names (as low as 1) and starves pair discovery. RVOL is
    still applied later as a per-candidate score boost, not an exclusion.

    Always union the dynamic/screener pool with the full static NYSE eligible
    columns so pair discovery keeps a liquid core even when screener overlap is
    thin or screener meta lacks dollar-volume fields.
    """
    global _stat_arb_universe_logged
    data_columns = getattr(data, "columns", [])
    dynamic = config.nyse_momentum_universe(data_columns)
    static = [c for c in data_columns if config._nyse_eligible_symbol(c)]
    # Static core first so liquid UNIVERSE names are never dropped by the scan cap.
    cols = list(dict.fromkeys([*static, *dynamic]))
    min_universe = int(getattr(config, "STAT_ARB_MIN_UNIVERSE", 20) or 20)
    max_scan = int(getattr(config, "STAT_ARB_MAX_SCAN_UNIVERSE", 80) or 80)
    if max_scan > 0 and len(cols) > max_scan:
        cols = cols[:max_scan]
    if len(cols) < min_universe and static:
        logger.warning(
            "[STAT_ARB_UNIVERSE] merged pool still below floor: %d < %d "
            "(dynamic=%d static=%d)",
            len(cols),
            min_universe,
            len(dynamic),
            len(static),
        )
    if len(dynamic) < min_universe or len(dynamic) < len(cols):
        logger.debug(
            "[STAT_ARB_UNIVERSE] dynamic=%d + static=%d -> %d names (top: %s)",
            len(dynamic),
            len(static),
            len(cols),
            ", ".join(cols[:10]),
        )
    if not _stat_arb_universe_logged:
        logger.info(
            "[STAT_ARB_UNIVERSE] Stat Arb universe: %d names (top: %s)",
            len(cols),
            ", ".join(cols[:10]),
        )
        _stat_arb_universe_logged = True
    return cols


def equity_stat_arb_intents(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    max_trades: int | None = None,
    yield_gated: bool = False,
    notional=None,
    volatility: str | None = None,
    executor=None,
):
    if not config.effective_stat_arb_enabled():
        return []
    if regime_entries_paused(regime, data) or yield_gated:
        return []

    equity_cols = _nyse_stat_arb_columns(data)
    if len(equity_cols) < 2:
        return []

    try:
        from modules.dynamic_universe import short_borrow_allowed
    except ImportError:
        short_borrow_allowed = lambda _s: True  # noqa: E731

    z_entry = config.effective_stat_arb_z_entry(
        volatility=volatility, regime=regime
    )
    exclude = _equity_exclude_symbols(
        executor, data=data, regime=regime, yield_gated=yield_gated
    )
    candidates = _scan_pair_candidates(
        data,
        equity_cols,
        lookback=config.STAT_ARB_LOOKBACK,
        min_corr=config.effective_stat_arb_min_correlation(),
        z_entry=z_entry,
        momentum_pick=True,
        exclude_symbols=exclude,
        regime=regime,
        max_leg_vol=config.effective_stat_arb_max_leg_vol(),
    )
    slots = _equity_pair_slots(executor) if executor is not None else config.effective_stat_arb_max_pairs()
    trade_cap = max_trades if max_trades is not None else config.effective_stat_arb_max_trades()
    trade_cap = min(trade_cap, max(0, slots))
    intents = []
    fired: set[str] = set()
    for _score, z, long_sym, short_sym, beta, y_sym, x_sym, _corr in candidates:
        if len(intents) >= trade_cap:
            break
        if long_sym in fired or short_sym in fired:
            continue
        pair_key = f"{long_sym}/{short_sym}"
        if _on_cooldown(pair_cooldown, pair_key, now, cooldown_bars=cooldown_bars):
            continue
        if not short_borrow_allowed(short_sym):
            continue
        scale = min(
            config.dynamic_equity_position_scale(long_sym),
            config.dynamic_equity_position_scale(short_sym),
        )
        pair_notional = notional
        if pair_notional is not None and scale < 1.0:
            pair_notional = round(float(pair_notional) * scale, 2)
        intents.append(
            {
                "long_symbol": long_sym,
                "short_symbol": short_sym,
                "y_symbol": y_sym,
                "x_symbol": x_sym,
                "pair_key": pair_key,
                "z_score": z,
                "beta": beta,
                "notional": pair_notional,
                "phase": "stat_arb_equity",
                "entry_bar": _coerce_bar_index(now),
            }
        )
        fired.add(long_sym)
        fired.add(short_sym)
    return intents


def _skip_crypto_stat_arb_entries(executor) -> bool:
    """Alpaca cannot open crypto shorts — entries via Kraken mirror only."""
    return _is_alpaca_live_executor(executor)


def run_crypto_stat_arb(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    max_trades: int = 2,
    log_fn=None,
    portfolio_manager=None,
    volatility=None,
    spacex_snapshot=None,
):
    """Cointegration-filtered crypto pairs with exits at mean reversion."""
    att = getattr(executor, "_attribution", None)
    if regime_entries_paused(regime, data):
        if att:
            att.record_crypto_reject("regime_paused")
        return 0
    _prune_ghost_book_entries(executor, crypto_only=True)
    trades = process_exits(data, executor, log_fn=log_fn, regime=regime, now=now)
    if _skip_crypto_stat_arb_entries(executor):
        return trades
    notional = None
    if hasattr(executor, "compute_crypto_notional"):
        notional = executor.compute_crypto_notional()
    if notional is None and hasattr(executor, "compute_crypto_notional"):
        if att:
            att.record_crypto_reject("no_room")

    slots = _crypto_pair_slots(executor)
    intents = crypto_stat_arb_intents(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_bars=cooldown_bars,
        max_trades=max_trades,
        volatility=volatility,
        spacex_snapshot=spacex_snapshot,
        notional=notional,
        vol_gate_checked=True,
        executor=executor,
    )
    if att:
        try:
            from modules.crypto_universe import crypto_trading_columns

            crypto_cols = crypto_trading_columns(data)
        except Exception:
            crypto_cols = [c for c in data.columns if config.is_crypto(c)]
        raw_count = 0
        if len(crypto_cols) >= 2:
            raw = _scan_pair_candidates(
                data,
                crypto_cols,
                lookback=config.STAT_ARB_LOOKBACK,
                min_corr=config.effective_pair_min_correlation(),
                z_entry=config.effective_crypto_z_entry(
                    volatility=volatility, regime=regime
                ),
                momentum_pick=False,
            )
            raw_count = len(raw)
            att.record_crypto_scan_signals(raw_count)
        att.record_crypto_intents(len(intents))
        if raw_count > 0 and not intents and slots <= 0:
            att.record_crypto_reject("max_pairs")
    open_live = _open_crypto_pair_keys(executor)
    for intent in intents:
        if intent["pair_key"] in open_live:
            if att:
                att.record_crypto_reject("in_book")
            continue
        if _execute_entry(executor, intent, log_fn=log_fn, regime=regime, now=now):
            pair_cooldown[intent["pair_key"]] = now
            open_live.add(intent["pair_key"])
            trades += 1
            if portfolio_manager:
                portfolio_manager.add_position(intent["pair_key"], intent["z_score"], 0)
    return trades


_SCAN_DEBUG_EVERY = 0  # throttle: only log every Nth call (0 = every call)
_scan_debug_calls = 0


def _log_stat_arb_scan_funnel(
    *,
    equity_cols: list[str],
    exclude: set[str],
    z_entry: float,
    raw_count: int,
    reject_counter: dict[str, int] | None,
    near_miss: list[tuple] | None,
    now,
) -> None:
    """Emit a one-line stat-arb scan funnel + top reject reasons (debug only)."""
    global _scan_debug_calls
    _scan_debug_calls += 1
    if _SCAN_DEBUG_EVERY and (_scan_debug_calls % _SCAN_DEBUG_EVERY):
        return
    rc = reject_counter or {}
    pairs_seen = rc.get("pairs_seen", 0)
    ordered = sorted(
        ((k, v) for k, v in rc.items() if k != "pairs_seen" and v),
        key=lambda kv: kv[1],
        reverse=True,
    )
    reasons = " ".join(f"{k}={v}" for k, v in ordered) or "none"
    logger.warning(
        "[STAT_ARB_SCAN] bar=%s universe=%d excluded=%d min_corr=%.2f z_entry=%.2f "
        "pairs_seen=%d scan_signals=%d | rejects: %s",
        now,
        len(equity_cols),
        len(exclude),
        float(config.effective_stat_arb_min_correlation()),
        float(z_entry),
        pairs_seen,
        raw_count,
        reasons,
    )
    if near_miss:
        samples = ", ".join(f"{a}/{b}(corr={c},{d})" for c, a, b, d in near_miss)
        logger.warning("[STAT_ARB_SCAN] near-miss pairs: %s", samples)


def run_equity_stat_arb(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    log_fn=None,
    portfolio_manager=None,
    yield_gated: bool = False,
    volatility: str | None = None,
):
    """Long strong / short weak NYSE pairs with cointegration filter."""
    trades = process_exits(data, executor, log_fn=log_fn, regime=regime, now=now)
    notional = None
    if hasattr(executor, "compute_stat_arb_notional"):
        notional = executor.compute_stat_arb_notional()
    elif hasattr(executor, "compute_nyse_notional"):
        notional = executor.compute_nyse_notional()
    if notional is None and (
        hasattr(executor, "compute_stat_arb_notional")
        or hasattr(executor, "compute_nyse_notional")
    ):
        util = _stat_arb_cap_utilization(executor)
        if util is not None and util >= 0.95:
            _stat_arb_reject(executor, "cap_full")
        else:
            _stat_arb_reject(executor, "no_room")

    z_entry = config.effective_stat_arb_z_entry(volatility=volatility, regime=regime)
    exclude = _equity_exclude_symbols(
        executor, data=data, regime=regime, yield_gated=yield_gated
    )
    intents = equity_stat_arb_intents(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_bars=cooldown_bars,
        yield_gated=yield_gated,
        notional=notional,
        volatility=volatility,
        executor=executor,
    )
    att = getattr(executor, "_attribution", None)
    debug_scan = bool(getattr(config, "STAT_ARB_SCAN_DEBUG", False))
    if att or debug_scan:
        equity_cols = _nyse_stat_arb_columns(data)
        if att:
            att.record_stat_arb_universe(len(equity_cols))
        if len(equity_cols) >= 2:
            reject_counter: dict[str, int] = {} if debug_scan else None
            near_miss: list[tuple] = [] if debug_scan else None
            raw = _scan_pair_candidates(
                data,
                equity_cols,
                lookback=config.STAT_ARB_LOOKBACK,
                min_corr=config.effective_stat_arb_min_correlation(),
                z_entry=z_entry,
                momentum_pick=True,
                exclude_symbols=exclude,
                max_leg_vol=config.effective_stat_arb_max_leg_vol(),
                reject_counter=reject_counter,
                near_miss=near_miss,
            )
            if att:
                att.record_signals("stat_arb", len(raw))
            if debug_scan:
                _log_stat_arb_scan_funnel(
                    equity_cols=equity_cols,
                    exclude=exclude,
                    z_entry=z_entry,
                    raw_count=len(raw),
                    reject_counter=reject_counter,
                    near_miss=near_miss,
                    now=now,
                )
        elif debug_scan:
            logger.warning(
                "[STAT_ARB_SCAN] equity_cols<2 (n=%d) — universe too small; "
                "check nyse_momentum_universe / data columns",
                len(equity_cols),
            )
        if att:
            att.record_intents("stat_arb", len(intents))
            slots = _equity_pair_slots(executor)
            if att.intents["stat_arb"] > 0 and slots <= 0:
                att.record_stat_arb_reject("max_pairs")
    book = _open_book(executor)
    for intent in intents:
        if intent["pair_key"] in book:
            _stat_arb_reject(executor, "in_book")
            continue
        if _execute_entry(executor, intent, log_fn=log_fn, regime=regime, now=now):
            pair_cooldown[intent["pair_key"]] = now
            trades += 1
            if portfolio_manager:
                portfolio_manager.add_position(intent["pair_key"], intent["z_score"], 0)
    return trades