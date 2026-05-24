import yfinance as yf
import sqlite3

def update_database(tickers):
    conn = sqlite3.connect('market_data.db')
    for ticker in tickers:
        print(f"Syncing {ticker}...")
        df = yf.download(ticker, period='1y', interval='1d', progress=False)
        df.to_sql(ticker, conn, if_exists='replace')
    conn.close()