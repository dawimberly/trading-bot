import yfinance as yf
import sqlite3
import pandas as pd

UNIVERSE = [
    'BTC-USD', 'ETH-USD', 'SOL-USD', 'ADA-USD', 'AVAX-USD', 'LINK-USD',
    'AAPL', 'MSFT', 'NVDA', 'AMD', 'GOOGL', 'AMZN', 'TSLA', 'META',
    'VTI', 'QQQ', 'SPY', 'IWM',
    'XOM', 'CVX', 'LNG',
    'RTX', 'LMT', 'KTOS',
    'JPM', 'BAC', 'GS',
    'JNJ', 'UNH', 'PFE',
]

def fetch_and_store():
    conn = sqlite3.connect('market_data.db')
    print('Fetching 5-minute data for ' + str(len(UNIVERSE)) + ' tickers...')
    for ticker in UNIVERSE:
        try:
            df = yf.download(ticker, period='5d', interval='5m', progress=False)
            if df.empty:
                print('No data for ' + ticker)
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df[['Close']].copy()
            df.index.name = 'Date'
            df.reset_index(inplace=True)
            df.to_sql(ticker, conn, if_exists='replace', index=False)
            print('Stored: ' + ticker)
        except Exception as e:
            print('Failed: ' + ticker + ' - ' + str(e))
    conn.close()
    print('Done. Database updated.')

if __name__ == '__main__':
    fetch_and_store()
