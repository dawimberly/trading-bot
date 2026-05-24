"""Crypto pair and equity MA50 strategies shared by run_all.py and backtester.py."""

import config

PAUSED_REGIMES = ("RHYME_B: Panic_Volatility", "RHYME_E: Steady_Bearish_Decline")
CRYPTO_Z_THRESHOLD = 2.0
MAX_CRYPTO_TRADES = 2
MAX_EQUITY_TRADES = 1
COOLDOWN_SECONDS = 3600


def _on_cooldown(pair_cooldown, key, now, cooldown_seconds=COOLDOWN_SECONDS, cooldown_bars=None):
    last = pair_cooldown.get(key)
    if last is None:
        return False
    if cooldown_bars is not None:
        return (now - last) < cooldown_bars
    return (now - last).total_seconds() < cooldown_seconds


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
):
    """Same logic as run_all.py: z-score on raw spread, trade t1 only."""
    crypto_cols = [c for c in data.columns if config.is_crypto(c)]
    if len(crypto_cols) < 2:
        return 0
    if regime in PAUSED_REGIMES:
        return 0
    fired = set()
    trades = 0
    for i in range(len(crypto_cols)):
        for j in range(i + 1, len(crypto_cols)):
            if trades >= max_trades:
                return trades
            t1, t2 = crypto_cols[i], crypto_cols[j]
            if t1 in fired or t2 in fired:
                continue
            spread = data[t1] - data[t2]
            z = (spread.iloc[-1] - spread.mean()) / (spread.std() + 1e-9)
            if abs(z) > z_threshold:
                pair_key = t1 + "/" + t2
                if _on_cooldown(
                    pair_cooldown,
                    pair_key,
                    now,
                    cooldown_seconds=cooldown_seconds,
                    cooldown_bars=cooldown_bars,
                ):
                    continue
                side = "sell" if z > 0 else "buy"
                executor.execute_order(t1, side)
                pair_cooldown[pair_key] = now
                fired.add(t1)
                fired.add(t2)
                trades += 1
                if portfolio_manager:
                    portfolio_manager.add_position(pair_key, z, 0)
                if log_fn:
                    log_fn(t1, side, regime, pair_key, z)
    return trades


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
):
    """Same logic as run_all.py: buy first equity above MA50."""
    if regime in PAUSED_REGIMES:
        return 0
    equity_cols = [c for c in data.columns if not config.is_crypto(c)]
    if len(equity_cols) < 1:
        return 0
    trades = 0
    for symbol in equity_cols:
        if trades >= max_trades:
            return trades
        prices = data[symbol]
        ma50 = prices.rolling(window=min(50, len(prices))).mean().iloc[-1]
        current_price = prices.iloc[-1]
        if current_price > ma50:
            pair_key = symbol + "/MA50"
            if _on_cooldown(
                pair_cooldown,
                pair_key,
                now,
                cooldown_seconds=cooldown_seconds,
                cooldown_bars=cooldown_bars,
            ):
                continue
            executor.execute_order(symbol, "buy")
            pair_cooldown[pair_key] = now
            trades += 1
            if portfolio_manager:
                portfolio_manager.add_position(pair_key, 0, 0)
            if log_fn:
                log_fn(symbol, "buy", regime, pair_key, 0.0)
    return trades
