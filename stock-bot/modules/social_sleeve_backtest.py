"""Social sleeve simulation on daily backtests (paper book parallel to main fund)."""

from __future__ import annotations

import numpy as np
import pandas as pd

import config
from modules.felix_sentiment import felix_sentiment_as_of
from modules.social_sleeve import (
    SOCIAL_SYMBOLS,
    aggregate_social_score,
    resolve_social_target,
)
from modules.wayback_sentiment import web_sentiment_for_date


def social_score_for_backtest(
    ts,
    data: pd.DataFrame,
    monthly_web: pd.Series | None = None,
) -> dict:
    """Historical social score: Wayback headline + Felix as-of bar (no lookahead)."""
    headline = None
    if monthly_web is not None and not monthly_web.empty:
        w = web_sentiment_for_date(monthly_web, ts)
        if w is not None and not np.isnan(w):
            headline = float(w)
    if headline is None and data is not None and len(data) > 0:
        from modules.market_context import get_price_sentiment
        from modules.wisdom_sentiment import normalize_price_sentiment

        headline = float(normalize_price_sentiment(get_price_sentiment(data)))

    felix = felix_sentiment_as_of(ts)
    felix_score = (
        float(felix["sentiment"])
        if felix and felix.get("sentiment") is not None
        else None
    )
    return aggregate_social_score(
        {"headline_web_sentiment": headline} if headline is not None else None,
        felix_score=felix_score,
        headline=headline,
    )


def _price_ok(prices, symbol: str) -> bool:
    if symbol not in prices.index:
        return False
    val = prices.get(symbol)
    return val is not None and np.isfinite(val) and float(val) > 0


# XLE is tradable on Alpaca live but not in UNIVERSE daily tables; use XOM for sim.
XLE_BACKTEST_PROXY = "XOM"


def _resolve_target(target: str | None, prices) -> str | None:
    if target is None:
        return None
    if target == "XLE" and not _price_ok(prices, target):
        target = XLE_BACKTEST_PROXY
    if _price_ok(prices, target):
        return target
    return None


def _social_symbols_held(portfolio) -> list[str]:
    syms = set(SOCIAL_SYMBOLS) | {XLE_BACKTEST_PROXY}
    return [s for s in syms if portfolio.positions.get(s, 0) > 0]


def _social_value(portfolio, prices) -> float:
    total = 0.0
    for sym in _social_symbols_held(portfolio):
        qty = portfolio.positions.get(sym, 0)
        if qty <= 0:
            continue
        price = prices.get(sym)
        if price is not None and np.isfinite(price):
            total += float(qty) * float(price)
    return round(total, 2)


def run_social_backtest_day(
    portfolio,
    prices,
    agg: dict,
    *,
    market_open: bool = True,
) -> tuple[list[dict], dict]:
    """
    Rebalance paper social sleeve (GLD / XLE / SPY / cash) on one daily bar.
    Uses zero equity commission; separate portfolio from main fund.
    When dynamically disabled, flat to cash (sell held sleeve symbols).
    """
    actions: list[dict] = []
    meta = {"target": None, "reason": "disabled", "score": agg.get("score")}
    if not market_open:
        return actions, meta

    if not config.effective_social_sleeve_enabled():
        # Wind down residual sleeve inventory when dynamic gate turns off.
        for sym in _social_symbols_held(portfolio):
            qty = portfolio.positions.get(sym, 0)
            if qty <= 0:
                continue
            price = float(prices[sym])
            sell_n = round(qty * price, 2)
            if sell_n > 0:
                order = portfolio.trade(sym, "sell", price, tx_cost=0.0, notional=sell_n)
                if order:
                    actions.append({"action": "sell", "symbol": sym, "notional": sell_n})
        meta["reason"] = "dynamic_off_flatten"
        return actions, meta

    target_raw, reason = resolve_social_target(agg, log=False)
    target = _resolve_target(target_raw, prices)
    meta = {
        "target": target,
        "reason": reason,
        "score": agg.get("score"),
        "macro_bearish_hits": agg.get("macro_bearish_hits"),
    }
    equity = portfolio.equity(prices)
    cap = round(equity * config.effective_social_sleeve_cap_pct(), 2)
    min_n = config.effective_min_notional(equity)

    held = _social_symbols_held(portfolio)
    for sym in held:
        qty = portfolio.positions.get(sym, 0)
        if qty <= 0:
            continue
        if target is None or sym != target:
            price = float(prices[sym])
            sell_n = round(min(qty * price, cap), 2)
            if sell_n >= min_n:
                order = portfolio.trade(sym, "sell", price, tx_cost=0.0, notional=sell_n)
                if order:
                    actions.append({"action": "sell", "symbol": sym, "notional": sell_n})

    if not target:
        return actions, meta

    current = _social_value(portfolio, prices)
    room = round(cap - current, 2)
    if room < min_n:
        return actions, meta

    buy_n = round(min(room, cap), 2)
    if buy_n < min_n:
        return actions, meta

    price = float(prices[target])
    order = portfolio.trade(target, "buy", price, tx_cost=0.0, notional=buy_n)
    if order:
        actions.append({"action": "buy", "symbol": target, "notional": buy_n})
    return actions, meta
