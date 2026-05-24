def get_stock_signal(data):
    # Calculate 45-day Moving Average
    ma45 = data['Close'].rolling(window=45).mean()
    
    # Extract last values as scalars
    current_price = data['Close'].iloc[-1].item()
    ma_val = ma45.iloc[-1].item()
    
    return current_price > ma_val

def get_crypto_signal(data):
    # Calculate 10-period Exponential Moving Average
    ema10 = data['Close'].ewm(span=10, adjust=False).mean()
    
    # Extract last values as scalars
    current_price = data['Close'].iloc[-1].item()
    ema_val = ema10.iloc[-1].item()
    
    return current_price > ema_val