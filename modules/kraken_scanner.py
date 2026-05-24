"""Scan Kraken pairs for SMA-20 trend via ccxt."""

import ccxt

def run_scan(ticker_list):
    """
    Scans crypto tickers for SMA-20 trend alignment.
    Expects ticker_list in format ['BTC/USD', 'ETH/USD']
    """
    exchange = ccxt.kraken({'enableRateLimit': True})
    print(f"\n--- Starting Kraken Scan ---")
    
    for symbol in ticker_list:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
            if not ohlcv or len(ohlcv) < 20:
                continue
                
            closes = [candle[4] for candle in ohlcv]
            sma_20 = sum(closes[-20:]) / 20
            
            if closes[-1] > sma_20:
                print(f"[SIGNAL] {symbol} is bullish (Price > SMA-20)")
            else:
                print(f"[IGNORE] {symbol} is bearish (Price < SMA-20)")
        except Exception as e:
            print(f"Error scanning {symbol}: {e}")