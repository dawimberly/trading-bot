import pandas as pd

def get_full_market_universe():
    # You can download a CSV of all tickers from nasdaq.com/market-activity/stocks/screener
    # Or use a library like 'yfinance' to fetch a broad index list
    # For now, let's assume you have a file 'all_tickers.csv'
    try:
        df = pd.read_csv('all_tickers.csv')
        return df['Symbol'].tolist()
    except FileNotFoundError:
        # Fallback to a core list if file missing
        return ['VTI', 'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMD', 'GOOGL', 'AMZN']