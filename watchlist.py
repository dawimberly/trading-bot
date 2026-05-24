def get_full_watchlist():
    # 1. Market Benchmarks (To gauge overall market health)
    benchmarks = ["SPY", "QQQ", "BTC/USD", "ETH/USD"]
    
    # 2. High-Conviction Stocks (Diversified by sector)
    stocks = ["AAPL", "NVDA", "AMD", "JPM", "JNJ", "XOM"]
    
    # 3. High-Conviction Crypto (Volatility plays)
    crypto = ["SOL/USD", "ADA/USD", "LINK/USD"]
    
    # Combine them all into one master list
    return benchmarks + stocks + crypto