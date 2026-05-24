import sqlite3
import pandas as pd

def get_5m_data():
    """Fetches the latest 5-minute data from market_data.db using correct column names."""
    conn = sqlite3.connect('market_data.db')
    cursor = conn.cursor()
    # Find all 5m tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_5m';")
    tables = [t[0] for t in cursor.fetchall()]
    
    if not tables:
        return pd.DataFrame()
        
    data = pd.DataFrame()
    for t in tables:
        # We now specifically target the columns we confirmed: 'Datetime' and 'Close'
        df = pd.read_sql(f"SELECT Datetime, Close FROM '{t}'", conn)
        df['Datetime'] = pd.to_datetime(df['Datetime'])
        ticker = t.replace('_5m', '')
        data[ticker] = df.set_index('Datetime')['Close']
    
    conn.close()
    return data.ffill().dropna(how='all')