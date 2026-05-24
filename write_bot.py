code = ""
code += "import os\n"
code += "import sqlite3\n"
code += "import pandas as pd\n"
code += "import datetime\n"
code += "import time\n"
code += "from dotenv import load_dotenv, find_dotenv\n"
code += "from alpaca.trading.client import TradingClient\n"
code += "from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest\n"
code += "from alpaca.trading.enums import OrderSide, TimeInForce\n"
code += "from modules.risk_management import RiskManager\n"
code += "from modules.portfolio_manager import PortfolioManager\n"
code += "\n"
code += "load_dotenv(find_dotenv())\n"
code += "pair_cooldown = {}\n"
code += "last_data_refresh = None\n"
code += "REFRESH_INTERVAL = 900\n"
code += "risk_manager = RiskManager(max_drawdown_pct=0.10)\n"
code += "portfolio_manager = PortfolioManager()\n"
code += "\n"
code += "class AlpacaExecutor:\n"
code += "    def __init__(self):\n"
code += "        self.api_key = os.getenv('ALPACA_API_KEY')\n"
code += "        self.secret_key = os.getenv('ALPACA_SECRET_KEY')\n"
code += "        self.client = TradingClient(self.api_key, self.secret_key, paper=True)\n"
code += "\n"
code += "    def get_order_params(self, symbol):\n"
code += "        crypto_keywords = ['BTC', 'ETH', 'SOL', 'DOGE', 'ADA', 'USD', 'AVAX', 'LINK']\n"
code += "        is_crypto = any(coin in symbol for coin in crypto_keywords)\n"
code += "        formatted_symbol = symbol.replace('-', '/') if is_crypto else symbol\n"
code += "        tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY\n"
code += "        return formatted_symbol, tif, is_crypto\n"
code += "\n"
code += "    def execute_order(self, symbol, side):\n"
code += "        formatted_symbol, tif, is_crypto = self.get_order_params(symbol)\n"
code += "        request_params = GetOrdersRequest(status='open')\n"
code += "        orders = self.client.get_orders(filter=request_params)\n"
code += "        for o in orders:\n"
code += "            if o.symbol == formatted_symbol:\n"
code += "                self.client.cancel_order_by_id(o.id)\n"
code += "                time.sleep(0.5)\n"
code += "        account = self.client.get_account()\n"
code += "        available_cash = float(account.cash)\n"
code += "        target_notional = round(min(available_cash * 0.10, 10000.0), 2)\n"
code += "        order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL\n"
code += "        order = MarketOrderRequest(symbol=formatted_symbol, notional=target_notional, side=order_side, time_in_force=tif)\n"
code += "        return self.client.submit_order(order_data=order)\n"
code += "\n"
code += "def get_market_regime(sentiment, volatility):\n"
code += "    if sentiment > 0.5 and volatility == 'High': return 'RHYME_A: Euphoric_Volatility'\n"
code += "    elif sentiment < -0.5 and volatility == 'High': return 'RHYME_B: Panic_Volatility'\n"
code += "    elif sentiment > 0.5 and volatility == 'Low': return 'RHYME_C: Steady_Bullish_Growth'\n"
code += "    elif sentiment < -0.5 and volatility == 'Low': return 'RHYME_E: Steady_Bearish_Decline'\n"
code += "    else: return 'RHYME_D: Range_Bound_Neutral'\n"
code += "\n"
code += "def log_trade(symbol, side, regime):\n"
code += "    with open('trade_history.log', 'a') as f:\n"
code += "        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')\n"
code += "        f.write(ts + ' | ' + side.upper() + ' | ' + symbol + ' | Regime: ' + regime + '\\n')\n"
code += "\n"
code += "def get_volatility(data):\n"
code += "    vol = data.pct_change().dropna().std().mean()\n"
code += "    return 'High' if vol > 0.02 else 'Low'\n"
code += "\n"
code += "def get_sentiment(data):\n"
code += "    try:\n"
code += "        import tavily\n"
code += "        client = tavily.TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))\n"
code += "        results = client.search('stock market crypto sentiment today', max_results=5)\n"
code += "        text = ' '.join([r.get('content','') for r in results.get('results',[])]).lower()\n"
code += "        bullish = text.count('bullish') + text.count('rally') + text.count('surge') + text.count('gains') + text.count('upbeat')\n"
code += "        bearish = text.count('bearish') + text.count('crash') + text.count('plunge') + text.count('decline') + text.count('fear')\n"
code += "        total = bullish + bearish\n"
code += "        if total == 0: return 0.0\n"
code += "        return round((bullish - bearish) / total, 2)\n"
code += "    except Exception as e:\n"
code += "        print('Tavily error: ' + str(e))\n"
code += "        recent = data.iloc[-5:].mean()\n"
code += "        older = data.iloc[-20:-5].mean()\n"
code += "        return float((recent / older).mean() - 1.0)\n"
code += "\n"
code += "def is_crypto(symbol):\n"
code += "    return any(coin in symbol for coin in ['BTC','ETH','SOL','DOGE','ADA','USD','AVAX','LINK'])\n"
code += "\n"
code += "def run_crypto_strategy(data, executor, regime, now):\n"
code += "    crypto_cols = [c for c in data.columns if is_crypto(c)]\n"
code += "    if len(crypto_cols) < 2: return 0\n"
code += "    if regime in ['RHYME_B: Panic_Volatility', 'RHYME_E: Steady_Bearish_Decline']:\n"
code += "        print('--- Crypto strategy paused: ' + regime + ' ---')\n"
code += "        return 0\n"
code += "    fired = set()\n"
code += "    trades = 0\n"
code += "    for i in range(len(crypto_cols)):\n"
code += "        for j in range(i+1, len(crypto_cols)):\n"
code += "            if trades >= 2: return trades\n"
code += "            t1, t2 = crypto_cols[i], crypto_cols[j]\n"
code += "            if t1 in fired or t2 in fired: continue\n"
code += "            spread = data[t1] - data[t2]\n"
code += "            z = (spread.iloc[-1] - spread.mean()) / (spread.std() + 1e-9)\n"
code += "            if abs(z) > 2.0:\n"
code += "                pair_key = t1 + '/' + t2\n"
code += "                last = pair_cooldown.get(pair_key)\n"
code += "                if last and (now - last).total_seconds() < 3600: continue\n"
code += "                side = 'sell' if z > 0 else 'buy'\n"
code += "                try:\n"
code += "                    executor.execute_order(t1, side)\n"
code += "                    pair_cooldown[pair_key] = now\n"
code += "                    fired.add(t1); fired.add(t2)\n"
code += "                    trades += 1\n"
code += "                    portfolio_manager.open_position(pair_key, z)\n"
code += "                    print('!!! CRYPTO SIGNAL: ' + pair_key + ' | Z=' + str(round(z,2)) + ' | ' + side.upper() + ' | Regime: ' + regime)\n"
code += "                    log_trade(t1, side, regime)\n"
code += "                except Exception as e:\n"
code += "                    if 'insufficient' not in str(e).lower():\n"
code += "                        print('Crypto Error on ' + pair_key + ': ' + str(e))\n"
code += "    return trades\n"
code += "\n"
code += "def run_equity_strategy(data, executor, regime, now):\n"
code += "    if regime in ['RHYME_B: Panic_Volatility', 'RHYME_E: Steady_Bearish_Decline']:\n"
code += "        print('--- Equity strategy paused: ' + regime + ' ---')\n"
code += "        return 0\n"
code += "    equity_cols = [c for c in data.columns if not is_crypto(c)]\n"
code += "    if len(equity_cols) < 1: return 0\n"
code += "    trades = 0\n"
code += "    for symbol in equity_cols:\n"
code += "        if trades >= 1: return trades\n"
code += "        prices = data[symbol]\n"
code += "        ma50 = prices.rolling(window=min(50, len(prices))).mean().iloc[-1]\n"
code += "        current_price = prices.iloc[-1]\n"
code += "        if current_price > ma50:\n"
code += "            pair_key = symbol + '/MA50'\n"
code += "            last = pair_cooldown.get(pair_key)\n"
code += "            if last and (now - last).total_seconds() < 3600: continue\n"
code += "            try:\n"
code += "                executor.execute_order(symbol, 'buy')\n"
code += "                pair_cooldown[pair_key] = now\n"
code += "                trades += 1\n"
code += "                portfolio_manager.open_position(pair_key, 0)\n"
code += "                print('!!! EQUITY SIGNAL: ' + symbol + ' above MA50 | BUY | Regime: ' + regime)\n"
code += "                log_trade(symbol, 'buy', regime)\n"
code += "            except Exception as e:\n"
code += "                if 'insufficient' not in str(e).lower():\n"
code += "                    print('Equity Error on ' + symbol + ': ' + str(e))\n"
code += "    return trades\n"
code += "\n"
code += "def main():\n"
code += "    global last_data_refresh\n"
code += "    now_ts = datetime.datetime.now()\n"
code += "    if last_data_refresh is None or (now_ts - last_data_refresh).total_seconds() >= REFRESH_INTERVAL:\n"
code += "        from fetch_data import fetch_and_store\n"
code += "        print('--- Refreshing market data ---')\n"
code += "        fetch_and_store()\n"
code += "        last_data_refresh = now_ts\n"
code += "    executor = AlpacaExecutor()\n"
code += "    account = executor.client.get_account()\n"
code += "    equity = float(account.equity)\n"
code += "    if not risk_manager.check_drawdown(equity):\n"
code += "        print('!!! RISK HALT: Max drawdown reached. Skipping cycle. !!!')\n"
code += "        return\n"
code += "    print('--- Pipeline Cycle: ' + str(datetime.datetime.now()) + ' ---')\n"
code += "    conn = sqlite3.connect('market_data.db')\n"
code += "    cursor = conn.cursor()\n"
code += "    cursor.execute(\"SELECT name FROM sqlite_master WHERE type='table';\")\n"
code += "    tables = [t[0] for t in cursor.fetchall()]\n"
code += "    clean_tables = [t for t in tables if '_5m' not in t and '_daily' not in t]\n"
code += "    data = pd.DataFrame()\n"
code += "    for table in clean_tables:\n"
code += "        df = pd.read_sql(\"SELECT * FROM '\" + table + \"'\", conn)\n"
code += "        target_col = next((c for c in df.columns if 'close' in c.lower()), None)\n"
code += "        if not target_col: continue\n"
code += "        data[table] = df.set_index('Date')[target_col]\n"
code += "    conn.close()\n"
code += "    data = data.ffill().dropna(how='all')\n"
code += "    regime = get_market_regime(get_sentiment(data), get_volatility(data))\n"
code += "    print('--- Regime: ' + regime + ' ---')\n"
code += "    now = datetime.datetime.now()\n"
code += "    c = run_crypto_strategy(data, executor, regime, now)\n"
code += "    e = run_equity_strategy(data, executor, regime, now)\n"
code += "    print('--- Crypto trades: ' + str(c) + ' | Equity trades: ' + str(e) + ' ---')\n"
code += "\n"
code += "if __name__ == '__main__':\n"
code += "    print('--- Starting 24/7 Weinstein-Iteration Engine ---')\n"
code += "    while True:\n"
code += "        try:\n"
code += "            main()\n"
code += "        except Exception as e:\n"
code += "            print('Cycle Error: ' + str(e))\n"
code += "        time.sleep(60)\n"

with open('run_all.py', 'w', encoding='utf-8') as f:
    f.write(code)
print('run_all.py written successfully')

with open('fetch_data.py', 'w', encoding='utf-8') as f:
    f.write("import yfinance as yf\n")
    f.write("import sqlite3\n")
    f.write("import pandas as pd\n")
    f.write("\n")
    f.write("UNIVERSE = [\n")
    f.write("    'BTC-USD', 'ETH-USD', 'SOL-USD', 'ADA-USD', 'AVAX-USD', 'LINK-USD',\n")
    f.write("    'AAPL', 'MSFT', 'NVDA', 'AMD', 'GOOGL', 'AMZN', 'TSLA', 'META',\n")
    f.write("    'VTI', 'QQQ', 'SPY', 'IWM',\n")
    f.write("    'XOM', 'CVX', 'LNG',\n")
    f.write("    'RTX', 'LMT', 'KTOS',\n")
    f.write("    'JPM', 'BAC', 'GS',\n")
    f.write("    'JNJ', 'UNH', 'PFE',\n")
    f.write("]\n")
    f.write("\n")
    f.write("def fetch_and_store():\n")
    f.write("    conn = sqlite3.connect('market_data.db')\n")
    f.write("    print('Fetching 5-minute data for ' + str(len(UNIVERSE)) + ' tickers...')\n")
    f.write("    for ticker in UNIVERSE:\n")
    f.write("        try:\n")
    f.write("            df = yf.download(ticker, period='5d', interval='5m', progress=False)\n")
    f.write("            if df.empty:\n")
    f.write("                print('No data for ' + ticker)\n")
    f.write("                continue\n")
    f.write("            if isinstance(df.columns, pd.MultiIndex):\n")
    f.write("                df.columns = df.columns.get_level_values(0)\n")
    f.write("            df = df[['Close']].copy()\n")
    f.write("            df.index.name = 'Date'\n")
    f.write("            df.reset_index(inplace=True)\n")
    f.write("            df.to_sql(ticker, conn, if_exists='replace', index=False)\n")
    f.write("            print('Stored: ' + ticker)\n")
    f.write("        except Exception as e:\n")
    f.write("            print('Failed: ' + ticker + ' - ' + str(e))\n")
    f.write("    conn.close()\n")
    f.write("    print('Done. Database updated.')\n")
    f.write("\n")
    f.write("if __name__ == '__main__':\n")
    f.write("    fetch_and_store()\n")
print('fetch_data.py written successfully')

with open('check_tables.py', 'w', encoding='utf-8') as f:
    f.write("import sqlite3\n")
    f.write("conn = sqlite3.connect('market_data.db')\n")
    f.write("cursor = conn.cursor()\n")
    f.write("cursor.execute(\"SELECT name FROM sqlite_master WHERE type='table';\")\n")
    f.write("tables = [t[0] for t in cursor.fetchall()]\n")
    f.write("print(tables)\n")
    f.write("conn.close()\n")
print('check_tables.py written successfully')

with open('backtester.py', 'w', encoding='utf-8') as f:
    f.write("import sqlite3\n")
    f.write("import pandas as pd\n")
    f.write("import numpy as np\n")
    f.write("import warnings\n")
    f.write("warnings.filterwarnings('ignore', category=RuntimeWarning)\n")
    f.write("from modules.advisor_ranker import get_ranked_targets\n")
    f.write("\n")
    f.write("def run_performance_test():\n")
    f.write("    print('--- STARTING Z-SCORE BACKTEST ---')\n")
    f.write("    try:\n")
    f.write("        conn = sqlite3.connect('market_data.db')\n")
    f.write("        cursor = conn.cursor()\n")
    f.write("        cursor.execute(\"SELECT name FROM sqlite_master WHERE type='table';\")\n")
    f.write("        all_tables = [t[0] for t in cursor.fetchall()]\n")
    f.write("        clean_tables = [t for t in all_tables if '_5m' not in t and '_daily' not in t]\n")
    f.write("        data = pd.DataFrame()\n")
    f.write("        for t in clean_tables:\n")
    f.write("            try:\n")
    f.write("                df = pd.read_sql(\"SELECT * FROM '\" + t + \"'\", conn)\n")
    f.write("                target_col = next((c for c in df.columns if 'close' in c.lower()), None)\n")
    f.write("                if not target_col: continue\n")
    f.write("                df['Date'] = pd.to_datetime(df['Date'])\n")
    f.write("                data[t] = df.set_index('Date')[target_col]\n")
    f.write("            except: continue\n")
    f.write("        conn.close()\n")
    f.write("    except Exception as e:\n")
    f.write("        print('Database error: ' + str(e))\n")
    f.write("        return\n")
    f.write("    data = data.ffill().dropna(how='all')\n")
    f.write("    print('Loaded ' + str(len(data.columns)) + ' tickers over ' + str(len(data)) + ' rows.')\n")
    f.write("    initial_capital = 10000.0\n")
    f.write("    capital = initial_capital\n")
    f.write("    equity_curve = [initial_capital]\n")
    f.write("    tx_cost = 0.001\n")
    f.write("    step = 5\n")
    f.write("    for i in range(30, len(data), step):\n")
    f.write("        window = data.iloc[i-30:i]\n")
    f.write("        current_prices = data.iloc[i]\n")
    f.write("        prev_prices = data.iloc[i-1]\n")
    f.write("        targets = get_ranked_targets(data.columns.tolist(), window)\n")
    f.write("        if i % 200 == 0:\n")
    f.write("            print('Row ' + str(i) + ' of ' + str(len(data)) + '...')\n")
    f.write("        if targets:\n")
    f.write("            top_assets = list(set([t[0] for t in targets[:5]] + [t[1] for t in targets[:5]]))\n")
    f.write("            valid_targets = [t for t in top_assets if t in data.columns]\n")
    f.write("            if valid_targets:\n")
    f.write("                alloc = (capital * 0.95) / len(valid_targets)\n")
    f.write("                daily_pnl = 0\n")
    f.write("                for ticker in valid_targets:\n")
    f.write("                    price_now = current_prices[ticker]\n")
    f.write("                    price_prev = prev_prices[ticker]\n")
    f.write("                    rolling_mean = window[ticker].mean()\n")
    f.write("                    if price_prev > 0 and pd.notnull(price_now) and pd.notnull(price_prev):\n")
    f.write("                        if price_now > rolling_mean:\n")
    f.write("                            pct_change = (price_now - price_prev) / price_prev\n")
    f.write("                            daily_pnl += (alloc * pct_change) - (alloc * tx_cost)\n")
    f.write("                capital += daily_pnl\n")
    f.write("        equity_curve.append(capital)\n")
    f.write("    curve = pd.Series(equity_curve)\n")
    f.write("    returns = curve.pct_change().dropna()\n")
    f.write("    total_ret = (curve.iloc[-1] / initial_capital - 1) * 100\n")
    f.write("    sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0\n")
    f.write("    max_dd = ((curve / curve.cummax()) - 1).min() * 100\n")
    f.write("    print('--- Z-SCORE STRATEGY REPORT ---')\n")
    f.write("    print('Total Return:   ' + str(round(total_ret, 2)) + '%')\n")
    f.write("    print('Sharpe Ratio:   ' + str(round(sharpe, 2)))\n")
    f.write("    print('Max Drawdown:   ' + str(round(max_dd, 2)) + '%')\n")
    f.write("    print('-------------------------------')\n")
    f.write("\n")
    f.write("if __name__ == '__main__':\n")
    f.write("    run_performance_test()\n")
print('backtester.py written successfully')

with open('cleanup_db.py', 'w', encoding='utf-8') as f:
    f.write("import sqlite3\n")
    f.write("conn = sqlite3.connect('market_data.db')\n")
    f.write("cursor = conn.cursor()\n")
    f.write("cursor.execute(\"SELECT name FROM sqlite_master WHERE type='table';\")\n")
    f.write("all_tables = [t[0] for t in cursor.fetchall()]\n")
    f.write("junk = [t for t in all_tables if '_5m' in t or '_daily' in t]\n")
    f.write("for t in junk:\n")
    f.write("    cursor.execute(\"DROP TABLE IF EXISTS '\" + t + \"'\")\n")
    f.write("    print('Dropped: ' + t)\n")
    f.write("conn.commit()\n")
    f.write("conn.close()\n")
    f.write("print('Database cleaned.')\n")
print('cleanup_db.py written successfully')