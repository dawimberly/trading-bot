import sqlite3
import pandas as pd
from modules.advisor_ranker import get_ranked_targets
from modules.mock_executor import MockExecutor

def get_historical_data(conn, start_date, end_date):
    # Fetch all data from your SQLite db
    # Assumes table names are the ticker symbols
    query = f"SELECT * FROM BTC_USD WHERE timestamp BETWEEN '{start_date}' AND '{end_date}'"
    return pd.read_sql(query, conn)

def run_backtest():
    conn = sqlite3.connect("market_data.db")
    executor = MockExecutor()
    
    # 1. Get your data
    data = get_historical_data(conn, "2026-01-01", "2026-05-24")
    
    # 2. Iterate through time slices (5-min intervals)
    for i in range(30, len(data)):
        window = data.iloc[i-30:i]
        
        # 3. Use your existing Brain
        rankings = get_ranked_targets(data.columns.tolist(), window)
        
        for symbol, signal, score in rankings:
            # Re-use your existing logic here
            if score > 1.5:
                executor.execute_order(symbol, "buy", qty=1)

if __name__ == "__main__":
    run_backtest()