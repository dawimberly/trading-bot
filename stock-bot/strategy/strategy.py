# strategy.py

def check_signal(df):
    """
    Returns a signal and the investment percentage 
    based on the 10-day MA trend following strategy.
    """
    if len(df) < 10: 
        return "HOLD", 0.0
    
    # Calculate indicators
    current_price = df['close'].iloc[-1]
    ma10 = df['close'].rolling(window=10).mean().iloc[-1]
    
    # Strategy: 1% if bullish, 0.4% if cautious
    if current_price > ma10:
        return "BUY", 0.01
    else:
        return "HOLD", 0.004