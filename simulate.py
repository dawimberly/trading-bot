import pandas as pd
import sqlite3
import numpy as np
from modules.universe_manager import get_full_market_universe

def get_historical_data(tickers):
    conn = sqlite3.connect('market_data.db')
    combined = pd.DataFrame()
    for ticker in tickers:
        try:
            df = pd.read_sql(f"SELECT * FROM '{ticker}'", conn)
            # Dynamic column finding for MultiIndex tuples
            price_col = next((c for c in df.columns if 'Close' in c and ticker in c), None)
            if price_col:
                df['Date'] = pd.to_datetime(df['Date'])
                df.set_index('Date', inplace=True)
                combined[ticker] = df[price_col]
        except: continue
    conn.close()
    return combined.ffill().dropna()

def run_backtest(data):
    tickers = data.columns
    results = []
    
    # Iterate through each day to simulate history
    for i in range(30, len(data)):
        current_date = data.index[i]
        window = data.iloc[i-30:i+1]
        
        for idx_a in range(len(tickers)):
            for idx_b in range(idx_a + 1, len(tickers)):
                t1, t2 = tickers[idx_a], tickers[idx_b]
                
                spread = window[t1] - window[t2]
                z = (spread.iloc[-1] - spread.mean()) / spread.std()
                
                # Signal: Z > 2.0 (Divergence)
                if abs(z) > 2.0:
                    # Calculate if it reverts within 5 days
                    pnl = 0
                    for day_offset in range(1, 6):
                        if i + day_offset < len(data):
                            future_spread = (data.iloc[i+day_offset][t1] - data.iloc[i+day_offset][t2])
                            if abs(future_spread) < abs(spread.iloc[-1]):
                                pnl = 1 # Reverted
                                break
                    results.append({'Date': current_date, 'Pair': f"{t1}/{t2}", 'Z': z, 'Success': pnl})
    return pd.DataFrame(results)

if __name__ == "__main__":
    tickers = get_full_market_universe()
    data = get_historical_data(tickers)
    
    print("--- Running 365-Day Backtest Simulation ---")
    history = run_backtest(data)
    
    success_rate = history['Success'].mean() * 100
    print(f"Total signals: {len(history)}")
    print(f"Strategy Mean Reversion Success Rate: {success_rate:.2f}%")
    print("\nSample of signals:")
    print(history.head(10))