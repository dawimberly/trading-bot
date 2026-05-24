import sqlite3
import pandas as pd
from modules.advisor_ranker import get_ranked_targets

def run_test():
    conn = sqlite3.connect('market_data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_daily'")
    tables = [row[0] for row in cursor.fetchall()]
    
    if not tables:
        print("No daily tables found.")
        return

    data_dict = {}
    for table in tables:
        ticker = table.replace('_daily', '')
        df = pd.read_sql_query(f"SELECT * FROM '{table}'", conn)
        
        # Check column existence
        if 'Close' in df.columns:
            data_dict[ticker] = df.set_index('Date')['Close']
        else:
            print(f"Error: 'Close' column missing in {table}. Found: {df.columns.tolist()}")
            
    conn.close()
    
    if not data_dict:
        return

    combined_data = pd.DataFrame(data_dict).dropna()
    print(f"Testing with {len(combined_data.columns)} assets...")
    
    results = get_ranked_targets(combined_data.columns.tolist(), combined_data)
    
    if results:
        print(f"Success! Found {len(results)} pairs.")
        for res in results[:3]:
            print(f" - {res[0]} vs {res[1]} | Z-Score: {res[2]:.4f}")
    else:
        print("No pairs found with current settings.")

if __name__ == "__main__":
    run_test()