"""Crypto pair and equity MA50 strategies shared by run_all.py and backtester.py."""

import numpy as np

import config
from modules import deployment_sizing
from modules.crypto_universe import crypto_trading_columns

PAUSED_REGIMES = ("RHYME_B: Panic_Volatility", "RHYME_E: Steady_Bearish_Decline")


def regime_entries_paused(regime, data=None, sentiment=None):
    """True when new entries should be blocked (rhyme pause, bear, or daily loss circuit)."""
    try:
        from modules.trading_safety import entry_block_active

        if entry_block_active():
            return True
    except ImportError:
        pass
    if regime in PAUSED_REGIMES:
        return True
    if not config.DERIVED_BEAR_PAUSE_ENABLED or data is None:
        return False
    if sentiment is None:
        from modules.market_context import get_price_sentiment

        sentiment = get_price_sentiment(data)
    bullish, _ = _spy_market_up_signal(data, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    if not bullish and sentiment < config.DERIVED_BEAR_SENTIMENT_THRESHOLD:
        return True
    return False
# Sector tags for NYSE anti-overlap tests (subset of equity universe)
NYSE_SECTOR_MAP = {
    "AAPL": "Tech",
    "MSFT": "Tech",
    "NVDA": "Tech",
    "AMD": "Tech",
    "GOOGL": "Tech",
    "AMZN": "Tech",
    "TSLA": "Tech",
    "META": "Tech",
    "XOM": "Energy",
    "CVX": "Energy",
    "LNG": "Energy",
    "RTX": "Defense",
    "LMT": "Defense",
    "KTOS": "Defense",
    "JPM": "Financials",
    "BAC": "Financials",
    "GS": "Financials",
    "JNJ": "Healthcare",
    "UNH": "Healthcare",
    "PFE": "Healthcare",
}
CRYPTO_Z_THRESHOLD = 2.0
MAX_CRYPTO_TRADES = 2
MAX_EQUITY_TRADES = 1
COOLDOWN_SECONDS = 3600


PAIR_FILL_WAIT = 5.0


def _count_if_filled(executor, order, *, max_wait=PAIR_FILL_WAIT):
    """Return 1 only when Alpaca confirms a fill (not a queued accept)."""
    if order is None:
        return 0
    if hasattr(executor, "order_filled"):
        return 1 if executor.order_filled(order, max_wait=max_wait) else 0
    return 1


def _order_fill_notional(executor, order, *, max_wait=PAIR_FILL_WAIT) -> float | None:
    if order is None:
        return None
    if hasattr(executor, "order_fill_details"):
        details = executor.order_fill_details(order, max_wait=max_wait)
        if details and details.get("filled"):
            notional = float(details.get("notional") or 0)
            return notional if notional > 0 else None
    return None


def _leg_has_exposure(executor, symbol) -> bool:
    if not hasattr(executor, "_find_position"):
        return False
    pos = executor._find_position(symbol)
    if pos is None:
        return False
    return abs(float(pos.qty)) > 1e-9


def _unwind_pair_leg(executor, symbol, *, max_wait=PAIR_FILL_WAIT) -> None:
    if not _leg_has_exposure(executor, symbol):
        return
    order = executor.execute_full_exit(symbol)
    if order is not None and hasattr(executor, "order_filled"):
        executor.order_filled(order, max_wait=max_wait)


def execute_atomic_pair_entry(
    executor,
    long_sym: str,
    short_sym: str,
    leg_n: float,
    *,
    max_wait: float = PAIR_FILL_WAIT,
) -> tuple[bool, float | None, float | None]:
    """Both legs must fill; unwind any single-leg fill immediately."""
    long_order = executor.execute_order(long_sym, "buy", notional=leg_n)
    long_ok = bool(_count_if_filled(executor, long_order, max_wait=max_wait))
    short_order = executor.execute_order(short_sym, "sell", notional=leg_n)
    short_ok = bool(_count_if_filled(executor, short_order, max_wait=max_wait))
    if long_ok and short_ok:
        long_n = _order_fill_notional(executor, long_order, max_wait=0) or leg_n
        short_n = _order_fill_notional(executor, short_order, max_wait=0) or leg_n
        return True, long_n, short_n
    if long_ok:
        _unwind_pair_leg(executor, long_sym, max_wait=max_wait)
    if short_ok:
        _unwind_pair_leg(executor, short_sym, max_wait=max_wait)
    return False, None, None


def execute_atomic_pair_exit(
    executor,
    long_sym: str,
    short_sym: str,
    *,
    max_wait: float = PAIR_FILL_WAIT,
) -> bool:
    """Close both legs; return True only when neither has exposure."""
    if _leg_has_exposure(executor, long_sym):
        order = executor.execute_full_exit(long_sym)
        if order is None or not _count_if_filled(executor, order, max_wait=max_wait):
            return False
    if _leg_has_exposure(executor, short_sym):
        order = executor.execute_full_exit(short_sym)
        if order is None or not _count_if_filled(executor, order, max_wait=max_wait):
            return False
    return not _leg_has_exposure(executor, long_sym) and not _leg_has_exposure(
        executor, short_sym
    )


def _on_cooldown(pair_cooldown, key, now, cooldown_seconds=COOLDOWN_SECONDS, cooldown_bars=None):
    last = pair_cooldown.get(key)
    if last is None:
        return False
    if cooldown_bars is not None:
        return (now - last) < cooldown_bars
    return (now - last).total_seconds() < cooldown_seconds


def _crypto_pair_z(data, t1, t2):
    spread = data[t1] - data[t2]
    return (spread.iloc[-1] - spread.mean()) / (spread.std() + 1e-9)


def _pair_leg_notional(total_notional, executor, *, sleeve_attempted: bool = False):
    """Split sleeve notional across two market-neutral legs; scale by dynamic risk."""
    if total_notional is None:
        if sleeve_attempted:
            return None, None
        if hasattr(executor, "compute_notional"):
            equity_fn = getattr(executor, "_get_account", None)
            if equity_fn:
                equity = float(equity_fn().equity)
                total_notional = round(
                    equity * config.effective_risk_per_trade(equity), 2
                )
        if total_notional is None:
            return None, None
    leg = round(float(total_notional) / 2, 2)
    min_n = config.MIN_NOTIONAL
    if hasattr(executor, "_get_account"):
        try:
            min_n = config.effective_min_notional(float(executor._get_account().equity))
        except Exception:
            pass
    if leg < min_n:
        return None, None
    return leg, leg


def _momentum_score(data, symbol):
    prices = data[symbol].dropna()
    if len(prices) < 20:
        return None
    ma50 = prices.rolling(window=min(50, len(prices))).mean().iloc[-1]
    current = prices.iloc[-1]
    if ma50 <= 0:
        return None
    return current / ma50 - 1


def crypto_trade_intents(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=MAX_CRYPTO_TRADES,
    z_threshold=CRYPTO_Z_THRESHOLD,
    volatility=None,
    spacex_snapshot=None,
    notional=None,
):
    """Same logic as crypto strategy but returns intents for Kraken mirror (no Alpaca orders)."""
    from modules.crypto_vol_gate import crypto_trading_allowed

    crypto_cols = crypto_trading_columns(data)
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

    min_corr = config.effective_pair_min_correlation()
    z_threshold = config.effective_pair_z_threshold(z_threshold)
    market_neutral = config.effective_market_neutral_pairs_enabled()

    candidates = []
    for i in range(len(crypto_cols)):
        for j in range(i + 1, len(crypto_cols)):
            t1, t2 = crypto_cols[i], crypto_cols[j]
            if data[t1].corr(data[t2]) < min_corr:
                continue
            z = _crypto_pair_z(data, t1, t2)
            if abs(z) > z_threshold:
                candidates.append((abs(z), z, t1, t2))

    candidates.sort(reverse=True)
    fired = set()
    intents = []
    for _abs_z, z, t1, t2 in candidates:
        if len(intents) >= max_trades:
            break
        if t1 in fired or t2 in fired:
            continue
        pair_key = t1 + "/" + t2
        if _on_cooldown(
            pair_cooldown,
            pair_key,
            now,
            cooldown_seconds=cooldown_seconds,
            cooldown_bars=cooldown_bars,
        ):
            continue
        if market_neutral:
            long_sym = t2 if z > 0 else t1
            short_sym = t1 if z > 0 else t2
            intents.append(
                {
                    "market_neutral": True,
                    "long_symbol": long_sym,
                    "short_symbol": short_sym,
                    "pair_key": pair_key,
                    "z_score": z,
                    "notional": notional,
                    "phase": "crypto_pair",
                }
            )
        else:
            side = "sell" if z > 0 else "buy"
            symbol = t1
            intents.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "pair_key": pair_key,
                    "z_score": z,
                    "notional": notional,
                    "phase": "crypto_mirror",
                }
            )
        fired.add(t1)
        fired.add(t2)
    return intents


def _execute_market_neutral_legs(executor, intent, *, log_fn=None, regime="", portfolio_manager=None):
    """Buy long leg and sell short leg; both must fill or neither is kept."""
    long_sym = intent["long_symbol"]
    short_sym = intent["short_symbol"]
    z = intent["z_score"]
    pair_key = intent["pair_key"]
    leg_n, _ = _pair_leg_notional(
        intent.get("notional"),
        executor,
        sleeve_attempted="notional" in intent,
    )
    if leg_n is None:
        return 0, False

    ok, _, _ = execute_atomic_pair_entry(executor, long_sym, short_sym, leg_n)
    if not ok:
        return 0, False

    msg = f"Market-neutral pair: LONG {long_sym} / SHORT {short_sym}, Z={round(z, 1)}"
    if log_fn:
        log_fn(long_sym, "buy", regime, pair_key, z, leg_n, pair_msg=msg)
        log_fn(short_sym, "sell", regime, pair_key, z, leg_n, pair_msg=msg)
    if hasattr(executor, "register_pair_symbols"):
        executor.register_pair_symbols(long_sym, short_sym)
    if portfolio_manager:
        portfolio_manager.add_position(pair_key, z, 0)
    return 1, True


def equity_pair_trade_intents(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=1,
    yield_gated=False,
    notional=None,
):
    """Long strongest / short weakest NYSE name when spread z-score fires (paper only)."""
    if not config.effective_equity_pairs_enabled():
        return []
    if regime_entries_paused(regime, data) or yield_gated:
        return []

    equity_cols = _nyse_equity_columns(data)
    if len(equity_cols) < 2:
        return []

    min_corr = config.effective_pair_min_correlation()
    z_threshold = config.effective_pair_z_threshold()

    candidates = []
    for i in range(len(equity_cols)):
        for j in range(i + 1, len(equity_cols)):
            t1, t2 = equity_cols[i], equity_cols[j]
            if data[t1].corr(data[t2]) < min_corr:
                continue
            z = _crypto_pair_z(data, t1, t2)
            if abs(z) <= z_threshold:
                continue
            mom1 = _momentum_score(data, t1)
            mom2 = _momentum_score(data, t2)
            if mom1 is None or mom2 is None:
                continue
            if mom1 >= mom2:
                long_sym, short_sym = t1, t2
            else:
                long_sym, short_sym = t2, t1
            candidates.append((abs(z), z, long_sym, short_sym))

    candidates.sort(reverse=True)
    intents = []
    for _abs_z, z, long_sym, short_sym in candidates:
        if len(intents) >= max_trades:
            break
        pair_key = f"{long_sym}/{short_sym}"
        if _on_cooldown(
            pair_cooldown,
            pair_key,
            now,
            cooldown_seconds=cooldown_seconds,
            cooldown_bars=cooldown_bars,
        ):
            continue
        intents.append(
            {
                "market_neutral": True,
                "long_symbol": long_sym,
                "short_symbol": short_sym,
                "pair_key": pair_key,
                "z_score": z,
                "notional": notional,
                "phase": "equity_pair",
            }
        )
    return intents


def run_equity_pairs_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    log_fn=None,
    portfolio_manager=None,
    yield_gated=False,
):
    """Market-neutral NYSE pair sleeve — paper aggressive + PAPER_EQUITY_PAIRS only."""
    if config.effective_stat_arb_enabled():
        from modules.stat_arb_sleeve import run_equity_stat_arb

        return run_equity_stat_arb(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            log_fn=log_fn,
            portfolio_manager=portfolio_manager,
            yield_gated=yield_gated,
        )

    notional = None
    if hasattr(executor, "compute_nyse_notional"):
        notional = executor.compute_nyse_notional()

    intents = equity_pair_trade_intents(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        yield_gated=yield_gated,
        notional=notional,
    )
    trades = 0
    for intent in intents:
        n, ok = _execute_market_neutral_legs(
            executor,
            intent,
            log_fn=log_fn,
            regime=regime,
            portfolio_manager=portfolio_manager,
        )
        if ok:
            pair_cooldown[intent["pair_key"]] = now
            trades += n
    return trades


def run_crypto_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=MAX_CRYPTO_TRADES,
    z_threshold=CRYPTO_Z_THRESHOLD,
    log_fn=None,
    portfolio_manager=None,
    volatility=None,
    spacex_snapshot=None,
):
    """Z-score pairs; paper aggressive uses cointegration stat arb when enabled."""
    if not config.effective_crypto_enabled():
        return 0

    if config.effective_crypto_v2_enabled():
        from modules.crypto_dual_sleeve import run_crypto_dual_sleeve

        return run_crypto_dual_sleeve(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            max_trades=max_trades,
            log_fn=log_fn,
            portfolio_manager=portfolio_manager,
            volatility=volatility,
            spacex_snapshot=spacex_snapshot,
        )

    if config.effective_stat_arb_enabled():
        from modules.stat_arb_sleeve import run_crypto_stat_arb

        return run_crypto_stat_arb(
            data,
            executor,
            regime,
            now,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            max_trades=max_trades,
            log_fn=log_fn,
            portfolio_manager=portfolio_manager,
            volatility=volatility,
            spacex_snapshot=spacex_snapshot,
        )

    notional = None
    if hasattr(executor, "compute_crypto_notional"):
        notional = executor.compute_crypto_notional()

    intents = crypto_trade_intents(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        max_trades=max_trades,
        z_threshold=z_threshold,
        volatility=volatility,
        spacex_snapshot=spacex_snapshot,
        notional=notional,
    )
    trades = 0
    for intent in intents:
        if intent.get("market_neutral"):
            n, ok = _execute_market_neutral_legs(
                executor,
                intent,
                log_fn=log_fn,
                regime=regime,
                portfolio_manager=portfolio_manager,
            )
            if ok:
                pair_cooldown[intent["pair_key"]] = now
                trades += n
            continue

        t1 = intent["symbol"]
        side = intent["side"]
        pair_key = intent["pair_key"]
        z = intent["z_score"]
        trade_notional = intent.get("notional")
        if side == "buy" and trade_notional is None:
            continue
        order = executor.execute_order(
            t1, side, notional=trade_notional, reason=pair_key, sleeve="Crypto"
        )
        if not _count_if_filled(executor, order, max_wait=3.0):
            continue
        pair_cooldown[pair_key] = now
        trades += 1
        if portfolio_manager:
            portfolio_manager.add_position(pair_key, z, 0)
        if log_fn:
            if trade_notional is None:
                trade_notional = getattr(executor, "compute_notional", lambda: "")()
            log_fn(t1, side, regime, pair_key, z, trade_notional)
    return trades


def _nyse_equity_columns(data):
    """NYSE momentum sleeve symbols (static columns or dynamic screener)."""
    return config.nyse_momentum_universe(data.columns)


def _equity_momentum_candidates(data, equity_cols, *, bar_idx: int | None = None, full_data=None):
    from modules.dynamic_universe import IPO_MIN_TRADING_DAYS, is_ipo_symbol, is_ipo_trading_days

    src = full_data if full_data is not None else data
    rows = []
    for symbol in equity_cols:
        if bar_idx is not None and full_data is not None and symbol in full_data.columns:
            prices = full_data[symbol].iloc[: bar_idx + 1].dropna()
            ipo = is_ipo_symbol(symbol, data=full_data, bar_idx=bar_idx)
        elif full_data is not None and symbol in full_data.columns:
            first = full_data[symbol].first_valid_index()
            end = data.index[-1] if len(data.index) else None
            if first is not None and end is not None:
                prices = full_data[symbol].loc[first:end].dropna()
                ipo = is_ipo_trading_days(len(prices))
            else:
                prices = data[symbol].dropna()
                ipo = is_ipo_symbol(symbol, data=src)
        else:
            prices = data[symbol].dropna()
            ipo = is_ipo_symbol(symbol, data=src)
        min_bars = IPO_MIN_TRADING_DAYS if ipo else 20
        ma_window = 20 if ipo else 50
        if len(prices) < min_bars:
            continue
        window = min(ma_window, len(prices))
        ma = prices.rolling(window=window).mean().iloc[-1]
        current = prices.iloc[-1]
        if current > ma and ma > 0:
            rows.append((current / ma - 1, symbol))
    rows.sort(reverse=True)
    return [s for _, s in rows]


def _is_nyse_tech(symbol):
    return NYSE_SECTOR_MAP.get(symbol) == "Tech"


def _spy_vs_equity_metrics(data, symbol, lookback=None):
    """60d return correlation and beta vs SPY (0, 0 if insufficient data)."""
    lookback = lookback or config.NYSE_SPY_CORR_LOOKBACK
    spy = config.SPY_BOT_SYMBOL
    if symbol not in data.columns or spy not in data.columns:
        return 0.0, 0.0
    rets = data[[symbol, spy]].pct_change().dropna().tail(lookback)
    if len(rets) < 20:
        return 0.0, 0.0
    corr = float(rets[symbol].corr(rets[spy]))
    if not np.isfinite(corr):
        corr = 0.0
    spy_var = float(rets[spy].var())
    if spy_var < 1e-12:
        return corr, 0.0
    beta = float(rets[symbol].cov(rets[spy]) / spy_var)
    if not np.isfinite(beta):
        beta = 0.0
    return corr, beta


def _spy_sleeve_active(data, *, yield_gated=False, regime=None):
    if regime_entries_paused(regime, data) or yield_gated:
        return False
    bullish, _ = _spy_market_up_signal(data, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    return bullish


def _filter_nyse_anti_overlap(data, ranked):
    """Drop names too correlated / high-beta vs SPY; keep momentum order."""
    if not ranked:
        return ranked
    out = []
    for symbol in ranked:
        corr, beta = _spy_vs_equity_metrics(data, symbol)
        if corr > config.NYSE_SPY_CORR_MAX or beta > config.NYSE_SPY_BETA_MAX:
            continue
        out.append(symbol)
    return out


def _apply_sector_tech_cap(ranked, *, top_n=3, max_tech=None):
    """At most max_tech Tech names in the first top_n momentum slots."""
    max_tech = config.NYSE_SECTOR_TECH_CAP if max_tech is None else max_tech
    if max_tech <= 0 or not ranked:
        return ranked
    primary = []
    deferred = []
    tech_count = 0
    for sym in ranked:
        if len(primary) < top_n:
            if _is_nyse_tech(sym) and tech_count >= max_tech:
                deferred.append(sym)
                continue
            if _is_nyse_tech(sym):
                tech_count += 1
            primary.append(sym)
        else:
            deferred.append(sym)
    fill = []
    remaining = []
    for sym in deferred:
        if len(primary) + len(fill) < top_n:
            if _is_nyse_tech(sym) and tech_count >= max_tech:
                remaining.append(sym)
                continue
            if _is_nyse_tech(sym):
                tech_count += 1
            fill.append(sym)
        else:
            remaining.append(sym)
    return primary + fill + remaining


def _apply_screener_momentum_order(ranked):
    """Strict dynamic universe: prefer screener 30d momentum rank among MA50 picks."""
    try:
        from modules.dynamic_universe import screener_momentum_order

        screener_order = screener_momentum_order(ranked)
    except ImportError:
        return ranked
    if not screener_order:
        return ranked
    order_index = {sym: i for i, sym in enumerate(screener_order)}
    return sorted(ranked, key=lambda s: order_index.get(s, len(order_index)))


def _executor_equity(executor) -> float:
    if hasattr(executor, "portfolio"):
        return float(executor.portfolio.equity(executor.prices))
    return float(executor._get_account().equity)


def _apply_ipo_buy_notional(
    symbol: str,
    notional: float,
    equity: float,
    *,
    data=None,
    bar_idx: int | None = None,
) -> float:
    from modules.dynamic_universe import cap_ipo_buy_notional

    return cap_ipo_buy_notional(symbol, notional, equity, data=data, bar_idx=bar_idx)


def _record_ipo_buy(executor, symbol, *, data=None, bar_idx: int | None = None) -> None:
    from modules.dynamic_universe import is_ipo_symbol

    if not is_ipo_symbol(symbol, data=data, bar_idx=bar_idx):
        return
    stats = getattr(executor, "ipo_stats", None)
    if stats is None:
        executor.ipo_stats = {"buys": 0, "trims": 0, "trim_notional": 0.0}
        stats = executor.ipo_stats
    stats["buys"] += 1


def _is_nyse_momentum_position(symbol: str) -> bool:
    sym = config.normalize_symbol(symbol)
    if config.is_international_adr(sym):
        return False
    if config.is_bond_symbol(sym):
        return False
    if config.is_crypto(sym):
        return False
    if sym == config.SPY_BOT_SYMBOL:
        return False
    if config.is_metal_symbol(sym):
        return False
    if sym == config.VTI_CORE_SYMBOL:
        return False
    return True


def run_ipo_safety_trims(
    data,
    executor,
    *,
    log_fn=None,
    bar_idx: int | None = None,
) -> int:
    """Trim IPO positions at +20% unrealized gain down to 1% of equity."""
    from modules.cost_basis import _position_cost
    from modules.dynamic_universe import ipo_safety_enabled, ipo_trim_reduce_notional, is_ipo_symbol

    if not ipo_safety_enabled():
        return 0

    equity = _executor_equity(executor)
    min_n = config.effective_min_notional(equity)
    trims = 0

    if hasattr(executor, "portfolio"):
        symbols = [
            sym
            for sym, qty in executor.portfolio.positions.items()
            if float(qty) > 0 and _is_nyse_momentum_position(sym)
        ]
        positions = [executor._find_position(sym) for sym in symbols]
    else:
        positions = [
            pos
            for pos in executor._get_positions()
            if float(pos.qty) > 0 and _is_nyse_momentum_position(pos.symbol)
        ]

    for pos in positions:
        if pos is None:
            continue
        sym = config.normalize_symbol(pos.symbol)
        if not is_ipo_symbol(sym, data=data, bar_idx=bar_idx):
            continue
        cost, value, _ = _position_cost(pos)
        reduce_n = ipo_trim_reduce_notional(equity, cost, value)
        if reduce_n is None:
            continue
        reduce_n = round(float(reduce_n), 2)
        if reduce_n <= 0 or reduce_n < min_n:
            continue
        if hasattr(executor, "execute_reduce_notional"):
            order = executor.execute_reduce_notional(
                sym, reduce_n, reason="ipo_trim", sleeve="NYSE"
            )
        else:
            order = executor.execute_order(
                sym, "sell", notional=reduce_n, reason="ipo_trim", sleeve="NYSE"
            )
        if not _count_if_filled(executor, order):
            continue
        trims += 1
        stats = getattr(executor, "ipo_stats", None)
        if stats is None:
            executor.ipo_stats = {"buys": 0, "trims": 0, "trim_notional": 0.0}
            stats = executor.ipo_stats
        stats["trims"] += 1
        stats["trim_notional"] = round(float(stats["trim_notional"]) + reduce_n, 2)
        if log_fn:
            log_fn(sym, "ipo_trim", "", "ipo_trim", 0.0, reduce_n)

    return trims


def _equity_momentum_ranked(
    data,
    equity_cols,
    *,
    yield_gated=False,
    regime=None,
    bar_idx: int | None = None,
    full_data=None,
):
    ranked = _equity_momentum_candidates(
        data, equity_cols, bar_idx=bar_idx, full_data=full_data
    )
    if not ranked:
        return ranked
    if config.effective_paper_dynamic_universe_strict():
        ranked = _apply_screener_momentum_order(ranked)
    if _spy_sleeve_active(data, yield_gated=yield_gated, regime=regime):
        if config.NYSE_SECTOR_TECH_CAP > 0:
            ranked = _apply_sector_tech_cap(ranked)
        if config.effective_nyse_overlap_filter_enabled():
            ranked = _filter_nyse_anti_overlap(data, ranked)
    return ranked


def _spy_market_up_signal(data, symbol, ma_window):
    """True when price is above the moving average (market-up bet)."""
    if symbol not in data.columns:
        return False, 0.0
    prices = data[symbol].dropna()
    if len(prices) < ma_window:
        return False, 0.0
    window = min(ma_window, len(prices))
    ma = prices.rolling(window=window).mean().iloc[-1]
    current = prices.iloc[-1]
    if ma <= 0 or current <= ma:
        return False, 0.0
    return True, current / ma - 1


def _holds_symbol(executor, symbol):
    target = config.normalize_symbol(symbol)
    if hasattr(executor, "portfolio"):
        for sym, qty in executor.portfolio.positions.items():
            if config.normalize_symbol(sym) == target and float(qty) > 0:
                return True
        return False
    try:
        return any(
            config.normalize_symbol(p.symbol) == target
            for p in executor.client.get_all_positions()
        )
    except Exception:
        return False


def _sleeve_room(executor, cap_pct, value_fn):
    if hasattr(executor, "portfolio"):
        equity = executor.portfolio.equity(executor.prices)
    else:
        account = executor._get_account()
        equity = float(account.equity)
    cap = round(equity * cap_pct, 2)
    return round(cap - value_fn(), 2)


def _spy_buy_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    yield_gated=False,
    symbol=None,
    ma_window=None,
):
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if regime_entries_paused(regime, data) or yield_gated:
        return False
    bullish, _ = _spy_market_up_signal(data, symbol, ma_window)
    if not bullish:
        return False
    pair_key = f"{symbol}/MA{ma_window}"
    return not _on_cooldown(
        pair_cooldown,
        pair_key,
        now,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
    )


def _nyse_buy_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    yield_gated=False,
):
    if regime_entries_paused(regime, data):
        return False
    equity_cols = _nyse_equity_columns(data)
    ranked = _equity_momentum_ranked(
        data, equity_cols, yield_gated=yield_gated, regime=regime
    )
    if not ranked:
        return False
    pair_key = ranked[0] + "/MA50"
    return not _on_cooldown(
        pair_cooldown,
        pair_key,
        now,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
    )


def _crypto_buy_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    volatility=None,
    spacex_snapshot=None,
):
    intents = crypto_trade_intents(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        volatility=volatility,
        spacex_snapshot=spacex_snapshot,
    )
    return any(
        i.get("side") == "buy" or i.get("market_neutral")
        for i in intents
    )


def resolve_cycle_deploy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    volatility=None,
    spacex_snapshot=None,
    yield_gated=False,
    market_open=True,
):
    """Pre-compute co-fire sleeve notionals when 2+ sleeves want to buy."""
    if hasattr(executor, "begin_deployment_cycle"):
        executor.begin_deployment_cycle()
    else:
        executor.set_cofire_allocations({})
        return

    if hasattr(executor, "set_sizing_context"):
        executor.set_sizing_context(data)

    if not config.effective_cofire_budget_enabled():
        return

    rooms = {}
    spy_cap = config.effective_sleeve_cap(config.SPY_SLEEVE_CAP_PCT)
    crypto_cap = config.effective_sleeve_cap(config.CRYPTO_SLEEVE_CAP_PCT)
    nyse_cap = config.effective_sleeve_cap(config.NYSE_SLEEVE_CAP_PCT)

    if hasattr(executor, "portfolio"):
        equity = executor.portfolio.equity(executor.prices)
    else:
        equity = float(executor._get_account().equity)
    min_n = config.effective_min_notional(equity)

    if market_open and _spy_buy_intent(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        yield_gated=yield_gated,
    ):
        room = _sleeve_room(executor, spy_cap, executor.spy_sleeve_value)
        if room >= min_n:
            rooms["spy"] = room

    if (
        config.effective_crypto_enabled()
        and _crypto_buy_intent(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        volatility=volatility,
        spacex_snapshot=spacex_snapshot,
    )
    ):
        room = _sleeve_room(executor, crypto_cap, executor.crypto_sleeve_value)
        if room >= min_n:
            rooms["crypto"] = room

    if market_open and _nyse_buy_intent(
        data,
        regime,
        now,
        pair_cooldown,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
        yield_gated=yield_gated,
    ):
        room = _sleeve_room(executor, nyse_cap, executor.nyse_sleeve_value)
        if room >= min_n:
            rooms["nyse"] = room

    if len(rooms) < 2:
        return

    if hasattr(executor, "portfolio"):
        cash = executor.portfolio.cash
    else:
        account = executor._get_account()
        cash = float(account.cash)

    allocations = deployment_sizing.compute_cofire_allocations(equity, cash, rooms)
    executor.set_cofire_allocations(allocations)


def run_spy_exits(
    data,
    executor,
    regime="",
    *,
    symbol=None,
    ma_window=None,
    log_fn=None,
):
    """Sell full SPY position when price closes below the moving average."""
    if not config.effective_spy_exit_on_ma_break():
        return 0
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if not _holds_symbol(executor, symbol):
        return 0
    bullish, momentum = _spy_market_up_signal(data, symbol, ma_window)
    if bullish:
        return 0

    if (
        config.COST_BASIS_AWARE_ENABLED
        and config.DISCRETIONARY_SELL_BELOW_COST
        and hasattr(executor, "_find_position")
    ):
        from modules.cost_basis import position_below_cost

        if position_below_cost(executor, symbol):
            return 0

    pair_key = f"{symbol}/MA{ma_window}"
    if hasattr(executor, "execute_full_exit"):
        order = executor.execute_full_exit(symbol, reason=pair_key, sleeve="SPY")
    else:
        order = executor.execute_order(
            symbol, "sell", reduce_only=True, reason=pair_key, sleeve="SPY"
        )
    if not _count_if_filled(executor, order):
        return 0
    if log_fn:
        notional = ""
        if isinstance(order, dict):
            notional = order.get("notional", "")
        log_fn(symbol, "sell", regime, pair_key, momentum, notional)
    return 1


def run_spy_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    symbol=None,
    ma_window=None,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    log_fn=None,
    portfolio_manager=None,
    yield_gated=False,
):
    """Buy SPY when above MA — a simple bet that the broad market keeps rising."""
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if regime_entries_paused(regime, data):
        return 0
    if yield_gated:
        return 0
    bullish, momentum = _spy_market_up_signal(data, symbol, ma_window)
    if not bullish:
        return 0

    pair_key = f"{symbol}/MA{ma_window}"
    if _on_cooldown(
        pair_cooldown,
        pair_key,
        now,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
    ):
        return 0

    notional = None
    if hasattr(executor, "compute_spy_notional"):
        notional = executor.compute_spy_notional()
        if notional is None:
            return 0
    order = executor.execute_order(
        symbol, "buy", notional=notional, reason=pair_key, sleeve="SPY"
    )
    if not _count_if_filled(executor, order):
        return 0
    pair_cooldown[pair_key] = now
    if portfolio_manager:
        portfolio_manager.add_position(pair_key, momentum, 0)
    if log_fn:
        if notional is None:
            notional = getattr(executor, "compute_notional", lambda: "")()
        log_fn(symbol, "buy", regime, pair_key, momentum, notional)
    return 1


def run_equity_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=MAX_EQUITY_TRADES,
    log_fn=None,
    portfolio_manager=None,
    yield_gated=False,
    full_data=None,
    bar_idx: int | None = None,
):
    """Buy the equity with the strongest momentum above MA50 (not arbitrary column order)."""
    if regime_entries_paused(regime, data):
        return 0
    ipo_data = full_data if full_data is not None else data
    equity_cols = _nyse_equity_columns(data)
    ranked = _equity_momentum_ranked(
        data,
        equity_cols,
        yield_gated=yield_gated,
        regime=regime,
        bar_idx=bar_idx,
        full_data=ipo_data,
    )
    if not ranked:
        return 0

    trades = 0
    equity = _executor_equity(executor)
    min_n = config.effective_min_notional(equity)
    for symbol in ranked:
        if trades >= max_trades:
            break
        pair_key = symbol + "/MA50"
        if _on_cooldown(
            pair_cooldown,
            pair_key,
            now,
            cooldown_seconds=cooldown_seconds,
            cooldown_bars=cooldown_bars,
        ):
            continue
        notional = None
        if hasattr(executor, "compute_nyse_notional"):
            notional = executor.compute_nyse_notional()
            if notional is None:
                continue
            if config.NYSE_BETA_SCALING_ENABLED:
                _, beta = _spy_vs_equity_metrics(data, symbol)
                scaled = round(notional * deployment_sizing.nyse_beta_scale(beta), 2)
                if scaled < min_n:
                    continue
                notional = scaled
            vol_scale = config.dynamic_equity_position_scale(
                symbol, data=ipo_data, bar_idx=bar_idx
            )
            if vol_scale < 1.0:
                notional = round(float(notional) * vol_scale, 2)
                if notional < min_n:
                    continue
            notional = _apply_ipo_buy_notional(
                symbol, notional, equity, data=ipo_data, bar_idx=bar_idx
            )
            if notional < min_n:
                continue
        order = executor.execute_order(
            symbol, "buy", notional=notional, reason=pair_key, sleeve="NYSE"
        )
        if not _count_if_filled(executor, order):
            continue
        _record_ipo_buy(executor, symbol, data=ipo_data, bar_idx=bar_idx)
        pair_cooldown[pair_key] = now
        trades += 1
        if portfolio_manager:
            portfolio_manager.add_position(pair_key, 0, 0)
        if log_fn:
            if notional is None:
                notional = getattr(executor, "compute_notional", lambda: "")()
            log_fn(symbol, "buy", regime, pair_key, 0.0, notional)
    return trades


def spy_mirror_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    yield_gated=False,
    cooldown_seconds=COOLDOWN_SECONDS,
    symbol=None,
    ma_window=None,
) -> dict | None:
    """Intent to mirror SPY sleeve buy on Kraken (QQQ/SPY .EQ), or None."""
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if regime_entries_paused(regime, data) or yield_gated:
        return None
    bullish, momentum = _spy_market_up_signal(data, symbol, ma_window)
    if not bullish:
        return None
    pair_key = f"{symbol}/MA{ma_window}"
    if _on_cooldown(pair_cooldown, pair_key, now, cooldown_seconds=cooldown_seconds):
        return None
    return {
        "symbol": symbol,
        "side": "buy",
        "pair_key": pair_key,
        "phase": "spy_mirror",
        "momentum": momentum,
    }


def nyse_mirror_intent(
    data,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    yield_gated=False,
) -> dict | None:
    """Top MA50 momentum equity intent for Kraken mirror."""
    if regime_entries_paused(regime, data):
        return None
    equity_cols = _nyse_equity_columns(data)
    ranked = _equity_momentum_ranked(
        data, equity_cols, yield_gated=yield_gated, regime=regime
    )
    if not ranked:
        return None
    symbol = ranked[0]
    pair_key = symbol + "/MA50"
    if _on_cooldown(pair_cooldown, pair_key, now, cooldown_seconds=cooldown_seconds):
        return None
    return {
        "symbol": symbol,
        "side": "buy",
        "pair_key": pair_key,
        "phase": "nyse_mirror",
    }


def run_international_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    max_trades=MAX_EQUITY_TRADES,
    log_fn=None,
    portfolio_manager=None,
    yield_gated=False,
    full_data=None,
    bar_idx: int | None = None,
    cap_pct: float | None = None,
    trigger_reason: str = "",
    thinking_scales: dict | None = None,
    market_summary: dict | None = None,
) -> int:
    """Buy top MA50 ADR momentum when international macro/thinking trigger is active."""
    from modules.international_sleeve import (
        ADR_RISK_NOTE,
        international_trigger_context,
        international_universe,
        log_adr_risk_once,
        record_international_trade,
    )

    if not config.effective_international_sleeve_enabled():
        return 0
    if regime_entries_paused(regime, data):
        return 0

    ipo_data = full_data if full_data is not None else data
    active, resolved_cap, reason = international_trigger_context(
        market_summary=market_summary,
        thinking_scales=thinking_scales,
        regime=regime,
        data=ipo_data,
        bar_idx=bar_idx,
    )
    if cap_pct is not None and cap_pct > 0:
        active = True
        resolved_cap = min(float(cap_pct), config.INTERNATIONAL_SLEEVE_CAP_PCT)
    if not active or resolved_cap <= 0:
        executor.international_cap_pct = 0.0
        return 0

    executor.international_cap_pct = resolved_cap
    from modules.international_sleeve import note_international_active_bar

    note_international_active_bar(executor)
    log_adr_risk_once()

    equity_cols = international_universe(data.columns)
    ranked = _equity_momentum_ranked(
        data,
        equity_cols,
        yield_gated=yield_gated,
        regime=regime,
        bar_idx=bar_idx,
        full_data=ipo_data,
    )
    if config.effective_nyse_overlap_filter_enabled():
        ranked = _filter_nyse_anti_overlap(data, ranked)
    if not ranked:
        return 0

    trades = 0
    equity = _executor_equity(executor)
    min_n = config.effective_min_notional(equity)
    note = trigger_reason or reason
    for symbol in ranked:
        if trades >= max_trades:
            break
        pair_key = symbol + "/MA50/ADR"
        if _on_cooldown(
            pair_cooldown,
            pair_key,
            now,
            cooldown_seconds=cooldown_seconds,
            cooldown_bars=cooldown_bars,
        ):
            continue
        notional = None
        if hasattr(executor, "compute_international_notional"):
            notional = executor.compute_international_notional()
            if notional is None:
                continue
            if config.NYSE_BETA_SCALING_ENABLED:
                _, beta = _spy_vs_equity_metrics(data, symbol)
                scaled = round(notional * deployment_sizing.nyse_beta_scale(beta), 2)
                if scaled < min_n:
                    continue
                notional = scaled
            vol_scale = config.dynamic_equity_position_scale(
                symbol, data=ipo_data, bar_idx=bar_idx
            )
            if vol_scale < 1.0:
                notional = round(float(notional) * vol_scale, 2)
                if notional < min_n:
                    continue
        order = executor.execute_order(
            symbol,
            "buy",
            notional=notional,
            reason=pair_key,
            sleeve="INTL",
        )
        if not _count_if_filled(executor, order):
            continue
        record_international_trade(executor, symbol)
        pair_cooldown[pair_key] = now
        trades += 1
        if portfolio_manager:
            portfolio_manager.add_position(pair_key, 0, 0)
        if log_fn:
            if notional is None:
                notional = getattr(executor, "compute_notional", lambda: "")()
            log_fn(symbol, "buy", regime, pair_key, 0.0, notional)
        if trades == 1 and note:
            import logging

            logging.getLogger(__name__).info(
                "International sleeve (%s cap %.1f%%): %s | %s",
                symbol,
                resolved_cap * 100,
                note,
                ADR_RISK_NOTE,
            )
    return trades


def run_bond_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    log_fn=None,
    volatility: str | None = None,
    vol_score: float | None = None,
    vix: float | None = None,
    macro_stress: bool = False,
    macro_window=None,
    thinking_scales: dict | None = None,
) -> int:
    """Deploy TLT/GOVT up to cap in risk-off / high-VIX; exit when triggers clear."""
    from modules.bond_sleeve import (
        bond_trigger_context,
        log_bond_note_once,
        note_bond_active_bar,
        record_bond_trade,
        resolve_bond_symbol,
    )

    if not config.effective_bond_sleeve_enabled():
        return 0

    window = macro_window if macro_window is not None else data
    active, resolved_cap, reason = bond_trigger_context(
        window=window,
        regime=regime,
        volatility=volatility,
        vol_score=vol_score,
        vix=vix,
        macro_stress=macro_stress,
        thinking_scales=thinking_scales,
    )
    symbol = resolve_bond_symbol(data.columns)
    pair_key = f"{symbol}/risk_off"

    if not active:
        executor.bond_cap_pct = 0.0
        if _holds_symbol(executor, symbol):
            if hasattr(executor, "execute_full_exit"):
                order = executor.execute_full_exit(
                    symbol, reason="bond_risk_on_exit", sleeve="BOND"
                )
            else:
                order = executor.execute_order(
                    symbol, "sell", reduce_only=True, reason="bond_risk_on_exit", sleeve="BOND"
                )
            if _count_if_filled(executor, order):
                record_bond_trade(executor, symbol, side="sell")
                if log_fn:
                    log_fn(symbol, "sell", regime, "bond_risk_on_exit", 0.0, "")
                return 1
        return 0

    executor.bond_cap_pct = resolved_cap
    note_bond_active_bar(executor, resolved_cap)
    log_bond_note_once()

    if _on_cooldown(
        pair_cooldown,
        pair_key,
        now,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
    ):
        return 0

    equity = _executor_equity(executor)
    min_n = config.effective_min_notional(equity)
    notional = None
    if hasattr(executor, "compute_bond_notional"):
        notional = executor.compute_bond_notional()
    if notional is None or notional < min_n:
        return 0

    order = executor.execute_order(
        symbol, "buy", notional=notional, reason=pair_key, sleeve="BOND"
    )
    if not _count_if_filled(executor, order):
        return 0
    record_bond_trade(executor, symbol, side="buy")
    pair_cooldown[pair_key] = now
    if log_fn:
        log_fn(symbol, "buy", regime, pair_key, 0.0, notional)
    if reason:
        import logging

        logging.getLogger(__name__).info(
            "Bond sleeve (%s cap %.1f%%): %s",
            symbol,
            resolved_cap * 100,
            reason,
        )
    return 1


def run_bond_strategy(
    data,
    executor,
    regime,
    now,
    pair_cooldown,
    *,
    cooldown_seconds=COOLDOWN_SECONDS,
    cooldown_bars=None,
    log_fn=None,
    volatility: str | None = None,
    vol_score: float | None = None,
    vix: float | None = None,
    macro_stress: bool = False,
    macro_window=None,
    thinking_scales: dict | None = None,
) -> int:
    """Deploy TLT/GOVT up to cap in risk-off / high-VIX; exit when triggers clear."""
    from modules.bond_sleeve import (
        bond_trigger_context,
        log_bond_note_once,
        note_bond_active_bar,
        record_bond_trade,
        resolve_bond_symbol,
    )

    if not config.effective_bond_sleeve_enabled():
        return 0

    window = macro_window if macro_window is not None else data
    active, resolved_cap, reason = bond_trigger_context(
        window=window,
        regime=regime,
        volatility=volatility,
        vol_score=vol_score,
        vix=vix,
        macro_stress=macro_stress,
        thinking_scales=thinking_scales,
    )
    symbol = resolve_bond_symbol(data.columns)
    pair_key = f"{symbol}/risk_off"

    if not active:
        executor.bond_cap_pct = 0.0
        if _holds_symbol(executor, symbol):
            if hasattr(executor, "execute_full_exit"):
                order = executor.execute_full_exit(
                    symbol, reason="bond_risk_on_exit", sleeve="BOND"
                )
            else:
                order = executor.execute_order(
                    symbol, "sell", reduce_only=True, reason="bond_risk_on_exit", sleeve="BOND"
                )
            if _count_if_filled(executor, order):
                record_bond_trade(executor, symbol, side="sell")
                if log_fn:
                    log_fn(symbol, "sell", regime, "bond_risk_on_exit", 0.0, "")
                return 1
        return 0

    executor.bond_cap_pct = resolved_cap
    note_bond_active_bar(executor, resolved_cap)
    log_bond_note_once()

    if _on_cooldown(
        pair_cooldown,
        pair_key,
        now,
        cooldown_seconds=cooldown_seconds,
        cooldown_bars=cooldown_bars,
    ):
        return 0

    equity = _executor_equity(executor)
    min_n = config.effective_min_notional(equity)
    notional = None
    if hasattr(executor, "compute_bond_notional"):
        notional = executor.compute_bond_notional()
    if notional is None or notional < min_n:
        return 0

    order = executor.execute_order(
        symbol, "buy", notional=notional, reason=pair_key, sleeve="BOND"
    )
    if not _count_if_filled(executor, order):
        return 0
    record_bond_trade(executor, symbol, side="buy")
    pair_cooldown[pair_key] = now
    if log_fn:
        log_fn(symbol, "buy", regime, pair_key, 0.0, notional)
    if reason:
        import logging

        logging.getLogger(__name__).info(
            "Bond sleeve (%s cap %.1f%%): %s",
            symbol,
            resolved_cap * 100,
            reason,
        )
    return 1
