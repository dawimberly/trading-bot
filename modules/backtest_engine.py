"""Multi-asset momentum backtest using the backtesting library.

Run: python modules/backtest_engine.py
"""

import yfinance as yf
import pandas as pd
from backtesting import Backtest, Strategy

# Universe of assets to simulate
tickers = ['VTI', 'BTC-USD', 'GLD', 'QQQ']

class MomentumRotation(Strategy):
    def init(self):
        # 90-day momentum lookback
        self.momentum = self.I(lambda x: pd.Series(x).pct_change(90), self.data.Close)

    def next(self):
        # Buy if momentum is positive, close if it turns negative
        if self.momentum[-1] > 0 and not self.position:
            self.buy()
        elif self.momentum[-1] < 0 and self.position:
            self.position.close()

# --- Run for multiple assets ---
print(f"--- Running Multi-Asset Momentum Simulation ---")
results = []

for ticker in tickers:
    # Fetch data
    df = yf.download(ticker, start="2025-05-21", end="2026-05-21")
    
    # Clean data structure
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={'Adj Close': 'Close'})
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
    
    # Run backtest
    bt = Backtest(df, MomentumRotation, cash=10000, commission=.002)
    stats = bt.run()
    
    results.append({
        'Ticker': ticker, 
        'Return': stats['Return [%]'], 
        'Sharpe': stats['Sharpe Ratio'],
        'Max Drawdown': stats['Max. Drawdown [%]']
    })

# Display Results
print("\n--- Strategy Results Summary ---")
df_results = pd.DataFrame(results).sort_values(by='Sharpe', ascending=False)
print(df_results.to_string(index=False))