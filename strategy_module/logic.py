"""Moving-average crossover signal helper (BUY/SELL/HOLD)."""

import pandas as pd


def check_signal(data, asset_type, window):
    """
    Analyzes the price data and returns a signal (BUY/SELL/HOLD) 
    and the calculated moving average value.
    """
    
    # Calculate the moving average
    ma = data.rolling(window=window).mean()
    
    current_price = data.iloc[-1]
    current_ma = ma.iloc[-1]
    
    # Logic
    if current_price > current_ma:
        signal = "BUY"
    elif current_price < current_ma:
        signal = "SELL"
    else:
        signal = "HOLD"
        
    # FIX: Use 'current_ma' instead of 'ma_value'
    return signal, current_ma