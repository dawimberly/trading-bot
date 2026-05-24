"""Scan daily tables for price above 50-day moving average."""

import sqlite3

import pandas as pd

def get_trending_tickers(db_path='market_data.db'):
    """
    Scans the database for assets whose current price is 
    above their 50-day moving average.
    """
    conn = sqlite3.connect(db_path)
    try:
        # Fetch only daily tables to calculate trends
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_daily'")
        tables = [t[0] for t in cursor.fetchall()]
        
        trending = []
        for t in tables:
            # Query the most recent 50 days of data
            df = pd.read_sql(f"SELECT Close FROM '{t}' ORDER BY Date DESC LIMIT 50", conn)
            
            # Trend logic: Current price > 50-period average
            if not df.empty and len(df) == 50:
                current_price = df['Close'].iloc[0]
                moving_average = df['Close'].mean()
                
                if current_price > moving_average:
                    ticker = t.replace('_daily', '')
                    trending.append(ticker)
                    
        return trending
    except Exception as e:
        print(f"Error scanning trends: {e}")
        return []
    finally:
        conn.close()

def run_stock_scan(stocks):
    """Placeholder for stock-specific scanning logic."""
    print(f"Scanning stocks: {stocks}")

def run_crypto_scan(crypto):
    """Placeholder for crypto-specific scanning logic."""
    print(f"Scanning crypto: {crypto}")