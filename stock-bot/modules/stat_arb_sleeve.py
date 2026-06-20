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
    _momentum_score,
    _nyse_equity_columns,
    _on_cooldown,
    execute_atomic_pair_entry,
    execute_atomic_pair_exit,
    regime_entries_paused,
)
from modules.safe_io import read_json_file, write_json_file

LOOKBACK_DEFAULT = 60

BOOK_FILE = Path(os.getenv("STAT_ARB_BOOK_FILE", "stat_arb_open_book.json"))


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
    if book is not None:
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
    from modules.pipeline_strategies import _leg_has_exposure

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


def engle_granger_cointegrated(
    y: pd.Series,
    x: pd.Series,
    *,
    min_corr: float,
    lookback: int,
) -> tuple[bool, float]:
    """Lightweight Engle-Granger: OLS hedge ratio + mean-reverting residual slope."""
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
    return slope < -0.02, beta


def spread_zscore(y: pd.Series, x: pd.Series, beta: float, *, lookback: int) -> float:
    sub_y = y.tail(lookback)
    sub_x = x.tail(lookback)
    spread = sub_y - beta * sub_x
    return float((spread.iloc[-1] - spread.mean()) / (spread.std() + 1e-9))


def pair_leg_notional(
    executor,
    total_notional=None,
    *,
    sleeve_attempted: bool = False,
) -> tuple[float | None, float | None]:
    """Half notional per leg, scaled by dynamic risk."""
    if total_notional is None:
        if sleeve_attempted:
            return None, None
        equity_fn = getattr(executor, "_get_account", None)
        if equity_fn:
            equity = float(equity_fn().equity)
            total_notional = round(equity * config.effective_risk_per_trade(equity), 2)
    if total_notional is None:
        return None, None
    leg = round(float(total_notional) / 2, 2)
    min_n = config.MIN_NOTIONAL
    if equity_fn := getattr(executor, "_get_account", None):
        try:
            min_n = config.effective_min_notional(float(equity_fn().equity))
        except (TypeError, ValueError, AttributeError):
            pass
    if leg < min_n:
        return None, None
    pod_scale = float(getattr(executor, "pod_risk_scale", lambda _p: 1.0)("stat_arb"))
    if pod_scale <= config.POD_PAUSE_SCALE + 0.05:
        return None, None
    leg = round(leg * pod_scale, 2)
    if leg < min_n:
        return None, None
    return leg, leg


def _execute_entry(executor, intent, *, log_fn=None, regime: str = "") -> int:
    long_sym = intent["long_symbol"]
    short_sym = intent["short_symbol"]
    z = intent["z_score"]
    pair_key = intent["pair_key"]
    if _alpaca_crypto_short_blocked(executor, short_sym):
        return 0
    leg_n, _ = pair_leg_notional(
        executor,
        intent.get("notional"),
        sleeve_attempted="notional" in intent,
    )
    if leg_n is None:
        return 0

    ok, long_fill_n, short_fill_n = execute_atomic_pair_entry(
        executor, long_sym, short_sym, leg_n
    )
    if not ok:
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
    }
    _save_book(executor)
    if hasattr(executor, "register_pair_symbols"):
        executor.register_pair_symbols(long_sym, short_sym)

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


def _close_position(executor, pair_key: str, position: dict, *, log_fn=None, regime: str = "") -> int:
    long_sym = position["long_symbol"]
    short_sym = position["short_symbol"]
    leg_n = position.get("leg_notional")
    if not execute_atomic_pair_exit(executor, long_sym, short_sym):
        return 0

    book = _open_book(executor)
    book.pop(pair_key, None)
    _save_book(executor)
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


def process_exits(
    data,
    executor,
    *,
    log_fn=None,
    regime: str = "",
    lookback: int | None = None,
) -> int:
    """Close cointegrated pairs when |Z| falls to exit threshold."""
    book = _open_book(executor)
    if not book:
        return 0
    lb = lookback or config.STAT_ARB_LOOKBACK
    z_exit = config.effective_pair_z_exit()
    exits = 0
    for pair_key, pos in list(book.items()):
        y_sym = pos.get("y_symbol", pos["long_symbol"])
        x_sym = pos.get("x_symbol", pos["short_symbol"])
        if y_sym not in data.columns or x_sym not in data.columns:
            continue
        beta = float(pos.get("beta", 1.0))
        z = spread_zscore(data[y_sym], data[x_sym], beta, lookback=lb)
        if abs(z) <= z_exit:
            exits += _close_position(executor, pair_key, pos, log_fn=log_fn, regime=regime)
    return exits


def _scan_pair_candidates(
    data,
    symbols: list[str],
    *,
    lookback: int,
    min_corr: float,
    z_entry: float,
    momentum_pick: bool = False,
) -> list[tuple[float, float, str, str, float, str, str]]:
    """Return sorted (|z|, z, long, short, beta, y_sym, x_sym) candidates."""
    out: list[tuple[float, float, str, str, float, str, str]] = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            t1, t2 = symbols[i], symbols[j]
            pair = data[[t1, t2]].dropna().tail(lookback)
            if len(pair) < 30:
                continue
            if float(pair[t1].corr(pair[t2])) < min_corr:
                continue
            ok, beta = engle_granger_cointegrated(
                data[t1], data[t2], min_corr=min_corr, lookback=lookback
            )
            if not ok:
                ok, beta = engle_granger_cointegrated(
                    data[t2], data[t1], min_corr=min_corr, lookback=lookback
                )
                if not ok:
                    continue
                y_sym, x_sym = t2, t1
            else:
                y_sym, x_sym = t1, t2
            z = spread_zscore(data[y_sym], data[x_sym], beta, lookback=lookback)
            if abs(z) < z_entry:
                continue
            if momentum_pick:
                mom1 = _momentum_score(data, t1)
                mom2 = _momentum_score(data, t2)
                if mom1 is None or mom2 is None:
                    continue
                long_sym, short_sym = (t1, t2) if mom1 >= mom2 else (t2, t1)
            else:
                long_sym = x_sym if z > 0 else y_sym
                short_sym = y_sym if z > 0 else x_sym
            out.append((abs(z), z, long_sym, short_sym, beta, y_sym, x_sym))
    out.sort(reverse=True)
    return out


def crypto_stat_arb_intents(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    max_trades: int = 2,
    volatility=None,
    spacex_snapshot=None,
    notional=None,
):
    if not config.effective_stat_arb_enabled():
        return []
    crypto_cols = [c for c in data.columns if config.is_crypto(c)]
    if len(crypto_cols) < 2:
        return []
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
        z_entry=config.effective_pair_z_threshold(),
        momentum_pick=False,
    )
    intents = []
    fired: set[str] = set()
    for _abs_z, z, long_sym, short_sym, beta, y_sym, x_sym in candidates:
        if len(intents) >= max_trades:
            break
        if long_sym in fired or short_sym in fired:
            continue
        pair_key = f"{long_sym}/{short_sym}"
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


def equity_stat_arb_intents(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_bars=None,
    max_trades: int = 1,
    yield_gated: bool = False,
    notional=None,
):
    if not config.effective_stat_arb_enabled():
        return []
    if regime_entries_paused(regime, data) or yield_gated:
        return []

    equity_cols = _nyse_equity_columns(data)
    if len(equity_cols) < 2:
        return []

    try:
        from modules.dynamic_universe import short_borrow_allowed
    except ImportError:
        short_borrow_allowed = lambda _s: True  # noqa: E731

    candidates = _scan_pair_candidates(
        data,
        equity_cols,
        lookback=config.STAT_ARB_LOOKBACK,
        min_corr=config.effective_pair_min_correlation(),
        z_entry=config.effective_pair_z_threshold(),
        momentum_pick=True,
    )
    intents = []
    for _abs_z, z, long_sym, short_sym, beta, y_sym, x_sym in candidates:
        if len(intents) >= max_trades:
            break
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
            }
        )
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
    if regime_entries_paused(regime, data):
        return 0
    trades = process_exits(data, executor, log_fn=log_fn, regime=regime)
    if _skip_crypto_stat_arb_entries(executor):
        return trades
    notional = None
    if hasattr(executor, "compute_crypto_notional"):
        notional = executor.compute_crypto_notional()

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
    )
    book = _open_book(executor)
    for intent in intents:
        if intent["pair_key"] in book:
            continue
        if _execute_entry(executor, intent, log_fn=log_fn, regime=regime):
            pair_cooldown[intent["pair_key"]] = now
            trades += 1
            if portfolio_manager:
                portfolio_manager.add_position(intent["pair_key"], intent["z_score"], 0)
    return trades


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
):
    """Long strong / short weak NYSE pairs with cointegration filter."""
    trades = process_exits(data, executor, log_fn=log_fn, regime=regime)
    notional = None
    if hasattr(executor, "compute_nyse_notional"):
        notional = executor.compute_nyse_notional()

    intents = equity_stat_arb_intents(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_bars=cooldown_bars,
        yield_gated=yield_gated,
        notional=notional,
    )
    book = _open_book(executor)
    for intent in intents:
        if intent["pair_key"] in book:
            continue
        if _execute_entry(executor, intent, log_fn=log_fn, regime=regime):
            pair_cooldown[intent["pair_key"]] = now
            trades += 1
            if portfolio_manager:
                portfolio_manager.add_position(intent["pair_key"], intent["z_score"], 0)
    return trades