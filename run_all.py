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
from modules.pipeline_strategies import run_crypto_strategy, run_equity_strategy
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


def _crypto_log(symbol, side, regime, pair_key, z):
    print(
        f"!!! CRYPTO SIGNAL: {pair_key} | Z={round(z, 2)} | "
        f"{side.upper()} | Regime: {regime}"
    )
    log_trade(symbol, side, regime)


def _equity_log(symbol, side, regime, pair_key, _z):
    print(f"!!! EQUITY SIGNAL: {symbol} above MA50 | BUY | Regime: {regime}")
    log_trade(symbol, side, regime)


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
    c = run_crypto_strategy(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        log_fn=_crypto_log,
        portfolio_manager=portfolio_manager,
    )
    e = run_equity_strategy(
        data,
        executor,
        regime,
        now,
        pair_cooldown,
        log_fn=_equity_log,
        portfolio_manager=portfolio_manager,
    )
    print(f"--- Crypto trades: {c} | Equity trades: {e} ---")


if __name__ == "__main__":
    print("--- Starting 24/7 Weinstein-Iteration Engine ---")
    while True:
        try:
            main()
        except Exception as e:
            print("Cycle Error: " + str(e))
        time.sleep(60)
