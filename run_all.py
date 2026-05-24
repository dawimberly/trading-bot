import os
import sqlite3
import pandas as pd
import datetime
import time
from dotenv import load_dotenv, find_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from modules.risk_management import RiskManager
from modules.portfolio_manager import PortfolioManager

load_dotenv(find_dotenv())
pair_cooldown = {}
last_data_refresh = None
REFRESH_INTERVAL = 900
risk_manager = RiskManager(max_drawdown_pct=0.10)
portfolio_manager = PortfolioManager()

class AlpacaExecutor:
    def __init__(self):
        self.api_key = os.getenv('ALPACA_API_KEY')
        self.secret_key = os.getenv('ALPACA_SECRET_KEY')
        self.client = TradingClient(self.api_key, self.secret_key, paper=True)

    def get_order_params(self, symbol):
        crypto_keywords = ['BTC', 'ETH', 'SOL', 'DOGE', 'ADA', 'USD', 'AVAX', 'LINK']
        is_crypto = any(coin in symbol for coin in crypto_keywords)
        formatted_symbol = symbol.replace('-', '/') if is_crypto else symbol
        tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY
        return formatted_symbol, tif, is_crypto

    def execute_order(self, symbol, side):
        formatted_symbol, tif, is_crypto = self.get_order_params(symbol)
        request_params = GetOrdersRequest(status='open')
        orders = self.client.get_orders(filter=request_params)
        for o in orders:
            if o.symbol == formatted_symbol:
                self.client.cancel_order_by_id(o.id)
                time.sleep(0.5)
        account = self.client.get_account()
        available_cash = float(account.cash)
        target_notional = round(min(available_cash * 0.10, 10000.0), 2)
        order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL
        order = MarketOrderRequest(symbol=formatted_symbol, notional=target_notional, side=order_side, time_in_force=tif)
        return self.client.submit_order(order_data=order)

def get_market_regime(sentiment, volatility):
    if sentiment > 0.5 and volatility == 'High': return 'RHYME_A: Euphoric_Volatility'
    elif sentiment < -0.5 and volatility == 'High': return 'RHYME_B: Panic_Volatility'
    elif sentiment > 0.5 and volatility == 'Low': return 'RHYME_C: Steady_Bullish_Growth'
    elif sentiment < -0.5 and volatility == 'Low': return 'RHYME_E: Steady_Bearish_Decline'
    else: return 'RHYME_D: Range_Bound_Neutral'

def log_trade(symbol, side, regime):
    with open('trade_history.log', 'a') as f:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write(ts + ' | ' + side.upper() + ' | ' + symbol + ' | Regime: ' + regime + '\n')

def get_volatility(data):
    vol = data.pct_change().dropna().std().mean()
    return 'High' if vol > 0.02 else 'Low'

def get_sentiment(data):
    try:
        import tavily
        client = tavily.TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))
        results = client.search('stock market crypto sentiment today', max_results=5)
        text = ' '.join([r.get('content','') for r in results.get('results',[])]).lower()
        bullish = text.count('bullish') + text.count('rally') + text.count('surge') + text.count('gains') + text.count('upbeat')
        bearish = text.count('bearish') + text.count('crash') + text.count('plunge') + text.count('decline') + text.count('fear')
        total = bullish + bearish
        if total == 0: return 0.0
        return round((bullish - bearish) / total, 2)
    except Exception as e:
        print('Tavily error: ' + str(e))
        recent = data.iloc[-5:].mean()
        older = data.iloc[-20:-5].mean()
        return float((recent / older).mean() - 1.0)

def is_crypto(symbol):
    return any(coin in symbol for coin in ['BTC','ETH','SOL','DOGE','ADA','USD','AVAX','LINK'])

def run_crypto_strategy(data, executor, regime, now):
    crypto_cols = [c for c in data.columns if is_crypto(c)]
    if len(crypto_cols) < 2: return 0
    if regime in ['RHYME_B: Panic_Volatility', 'RHYME_E: Steady_Bearish_Decline']:
        print('--- Crypto strategy paused: ' + regime + ' ---')
        return 0
    fired = set()
    trades = 0
    for i in range(len(crypto_cols)):
        for j in range(i+1, len(crypto_cols)):
            if trades >= 2: return trades
            t1, t2 = crypto_cols[i], crypto_cols[j]
            if t1 in fired or t2 in fired: continue
            spread = data[t1] - data[t2]
            z = (spread.iloc[-1] - spread.mean()) / (spread.std() + 1e-9)
            if abs(z) > 2.0:
                pair_key = t1 + '/' + t2
                last = pair_cooldown.get(pair_key)
                if last and (now - last).total_seconds() < 3600: continue
                side = 'sell' if z > 0 else 'buy'
                try:
                    executor.execute_order(t1, side)
                    pair_cooldown[pair_key] = now
                    fired.add(t1); fired.add(t2)
                    trades += 1
                    portfolio_manager.open_position(pair_key, z)
                    print('!!! CRYPTO SIGNAL: ' + pair_key + ' | Z=' + str(round(z,2)) + ' | ' + side.upper() + ' | Regime: ' + regime)
                    log_trade(t1, side, regime)
                except Exception as e:
                    if 'insufficient' not in str(e).lower():
                        print('Crypto Error on ' + pair_key + ': ' + str(e))
    return trades

def run_equity_strategy(data, executor, regime, now):
    if regime in ['RHYME_B: Panic_Volatility', 'RHYME_E: Steady_Bearish_Decline']:
        print('--- Equity strategy paused: ' + regime + ' ---')
        return 0
    equity_cols = [c for c in data.columns if not is_crypto(c)]
    if len(equity_cols) < 1: return 0
    trades = 0
    for symbol in equity_cols:
        if trades >= 1: return trades
        prices = data[symbol]
        ma50 = prices.rolling(window=min(50, len(prices))).mean().iloc[-1]
        current_price = prices.iloc[-1]
        if current_price > ma50:
            pair_key = symbol + '/MA50'
            last = pair_cooldown.get(pair_key)
            if last and (now - last).total_seconds() < 3600: continue
            try:
                executor.execute_order(symbol, 'buy')
                pair_cooldown[pair_key] = now
                trades += 1
                portfolio_manager.open_position(pair_key, 0)
                print('!!! EQUITY SIGNAL: ' + symbol + ' above MA50 | BUY | Regime: ' + regime)
                log_trade(symbol, 'buy', regime)
            except Exception as e:
                if 'insufficient' not in str(e).lower():
                    print('Equity Error on ' + symbol + ': ' + str(e))
    return trades

def main():
    global last_data_refresh
    now_ts = datetime.datetime.now()
    if last_data_refresh is None or (now_ts - last_data_refresh).total_seconds() >= REFRESH_INTERVAL:
        from fetch_data import fetch_and_store
        print('--- Refreshing market data ---')
        fetch_and_store()
        last_data_refresh = now_ts
    executor = AlpacaExecutor()
    account = executor.client.get_account()
    equity = float(account.equity)
    if not risk_manager.check_drawdown(equity):
        print('!!! RISK HALT: Max drawdown reached. Skipping cycle. !!!')
        return
    print('--- Pipeline Cycle: ' + str(datetime.datetime.now()) + ' ---')
    conn = sqlite3.connect('market_data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [t[0] for t in cursor.fetchall()]
    clean_tables = [t for t in tables if '_5m' not in t and '_daily' not in t]
    data = pd.DataFrame()
    for table in clean_tables:
        df = pd.read_sql("SELECT * FROM '" + table + "'", conn)
        target_col = next((c for c in df.columns if 'close' in c.lower()), None)
        if not target_col: continue
        data[table] = df.set_index('Date')[target_col]
    conn.close()
    data = data.ffill().dropna(how='all')
    regime = get_market_regime(get_sentiment(data), get_volatility(data))
    print('--- Regime: ' + regime + ' ---')
    now = datetime.datetime.now()
    c = run_crypto_strategy(data, executor, regime, now)
    e = run_equity_strategy(data, executor, regime, now)
    print('--- Crypto trades: ' + str(c) + ' | Equity trades: ' + str(e) + ' ---')

if __name__ == '__main__':
    print('--- Starting 24/7 Weinstein-Iteration Engine ---')
    while True:
        try:
            main()
        except Exception as e:
            print('Cycle Error: ' + str(e))
        time.sleep(60)
