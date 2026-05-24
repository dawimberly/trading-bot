"""24/7 live trading loop: refresh data, detect regime, run crypto and equity strategies.

Run: python run_all.py
"""

import datetime
import time

import config
from fetch_data import fetch_and_store
from modules.alpaca_executor import AlpacaExecutor
from modules.data_loader import load_close_matrix
from modules.market_context import get_market_regime, get_sentiment, get_volatility
from modules.portfolio_manager import PortfolioManager
from modules.risk_management import RiskManager

pair_cooldown = {}
last_data_refresh = None
risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
portfolio_manager = PortfolioManager(ledger_file=config.LEDGER_PATH)


def log_trade(symbol, side, regime):
    with open(config.TRADE_HISTORY_LOG, "a", encoding="utf-8") as f:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts} | {side.upper()} | {symbol} | Regime: {regime}\n")


def run_crypto_strategy(data, executor, regime, now):
    crypto_cols = [c for c in data.columns if config.is_crypto(c)]
    if len(crypto_cols) < 2:
        return 0
    if regime in ("RHYME_B: Panic_Volatility", "RHYME_E: Steady_Bearish_Decline"):
        print("--- Crypto strategy paused: " + regime + " ---")
        return 0
    fired = set()
    trades = 0
    for i in range(len(crypto_cols)):
        for j in range(i + 1, len(crypto_cols)):
            if trades >= 2:
                return trades
            t1, t2 = crypto_cols[i], crypto_cols[j]
            if t1 in fired or t2 in fired:
                continue
            spread = data[t1] - data[t2]
            z = (spread.iloc[-1] - spread.mean()) / (spread.std() + 1e-9)
            if abs(z) > 2.0:
                pair_key = t1 + "/" + t2
                last = pair_cooldown.get(pair_key)
                if last and (now - last).total_seconds() < 3600:
                    continue
                side = "sell" if z > 0 else "buy"
                try:
                    executor.execute_order(t1, side)
                    pair_cooldown[pair_key] = now
                    fired.add(t1)
                    fired.add(t2)
                    trades += 1
                    portfolio_manager.add_position(pair_key, z, 0)
                    print(
                        f"!!! CRYPTO SIGNAL: {pair_key} | Z={round(z, 2)} | "
                        f"{side.upper()} | Regime: {regime}"
                    )
                    log_trade(t1, side, regime)
                except Exception as e:
                    if "insufficient" not in str(e).lower():
                        print("Crypto Error on " + pair_key + ": " + str(e))
    return trades


def run_equity_strategy(data, executor, regime, now):
    if regime in ("RHYME_B: Panic_Volatility", "RHYME_E: Steady_Bearish_Decline"):
        print("--- Equity strategy paused: " + regime + " ---")
        return 0
    equity_cols = [c for c in data.columns if not config.is_crypto(c)]
    if len(equity_cols) < 1:
        return 0
    trades = 0
    for symbol in equity_cols:
        if trades >= 1:
            return trades
        prices = data[symbol]
        ma50 = prices.rolling(window=min(50, len(prices))).mean().iloc[-1]
        current_price = prices.iloc[-1]
        if current_price > ma50:
            pair_key = symbol + "/MA50"
            last = pair_cooldown.get(pair_key)
            if last and (now - last).total_seconds() < 3600:
                continue
            try:
                executor.execute_order(symbol, "buy")
                pair_cooldown[pair_key] = now
                trades += 1
                portfolio_manager.add_position(pair_key, 0, 0)
                print(
                    f"!!! EQUITY SIGNAL: {symbol} above MA50 | BUY | Regime: {regime}"
                )
                log_trade(symbol, "buy", regime)
            except Exception as e:
                if "insufficient" not in str(e).lower():
                    print("Equity Error on " + symbol + ": " + str(e))
    return trades


def main():
    global last_data_refresh
    now_ts = datetime.datetime.now()
    if (
        last_data_refresh is None
        or (now_ts - last_data_refresh).total_seconds() >= config.REFRESH_INTERVAL
    ):
        print("--- Refreshing market data ---")
        fetch_and_store()
        last_data_refresh = now_ts
    executor = AlpacaExecutor()
    account = executor.client.get_account()
    equity = float(account.equity)
    if not risk_manager.check_drawdown(equity):
        print("!!! RISK HALT: Max drawdown reached. Skipping cycle. !!!")
        return
    print("--- Pipeline Cycle: " + str(datetime.datetime.now()) + " ---")
    data = load_close_matrix()
    regime = get_market_regime(get_sentiment(data), get_volatility(data))
    print("--- Regime: " + regime + " ---")
    now = datetime.datetime.now()
    c = run_crypto_strategy(data, executor, regime, now)
    e = run_equity_strategy(data, executor, regime, now)
    print(f"--- Crypto trades: {c} | Equity trades: {e} ---")


if __name__ == "__main__":
    print("--- Starting 24/7 Weinstein-Iteration Engine ---")
    while True:
        try:
            main()
        except Exception as e:
            print("Cycle Error: " + str(e))
        time.sleep(60)
